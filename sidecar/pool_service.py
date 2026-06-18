#!/usr/bin/env python3
"""
AquaPlus/ProLogic RS-485 sidecar service.

Connects to a Waveshare/USR WiFi serial bridge via TCP, maintains a persistent
aqualogic session, and exposes pool state + control via a local REST API that
the Homebridge plugin polls.

Usage:
    python3 pool_service.py --host 192.168.50.XXX --port 8899 --api-port 5757
    python3 pool_service.py --simulate          # full menu sim, no bridge needed

CIRCUIT CONTROL
  Circuit ON/OFF works for circuits that have a corresponding keypad key:
  POOL/SPA (shared POOL_SPA toggle, mutually exclusive), FILTER, LIGHTS,
  AUX_1, AUX_2.  HEATER_1 is routed through HEATER_AUTO_MODE (the only
  library-supported heater control).  SUPER_CHLORINATE and SPILLOVER have
  no keypad key and return 422.  Chlorinator % is read-only (501 on write).

MENU NAVIGATION  (docs/aqualogic-automation-spec.md)
  The MenuNavigator drives keypad keys over RS-485 and reads the LCD after
  each press.  Every operation anchors on text, never blind key-counts.
  Heater setpoints and VSP slot-4 speed are read and written via the
  Settings menu with full restore-to-prior-state discipline (§13.3).

SIMULATION MODE
  --simulate skips aqualogic entirely and runs a SimPanel that emulates the
  full verified Settings menu ring + VSP submenu.  Only flask is required.
  Navigator endpoints exercise the same code paths as real hardware.
"""

import argparse
import logging
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Tuple

from flask import Flask, jsonify, request, Response

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    force=True,  # override any handler Flask/werkzeug installed at import time,
                 # otherwise basicConfig is a no-op and our INFO logs are dropped
)
# NB: our logger must NOT be named 'pool_service'. Flask(__name__) resolves its
# app name to 'pool_service' (the script's module name), so app.logger IS
# logging.getLogger('pool_service'). The `app.logger.setLevel(WARNING)` below
# would then clobber *our* logger to WARNING and silently drop every INFO line.
# Use a distinct name so the two loggers never collide.
log = logging.getLogger('pool_sidecar')
log.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Shared pool state
# ---------------------------------------------------------------------------

@dataclass
class PoolState:
    circuits: dict = field(default_factory=dict)
    pool_temp: Optional[float] = None
    air_temp: Optional[float] = None
    spa_temp: Optional[float] = None
    salt_level: Optional[float] = None
    chlorinator_percent: Optional[float] = None
    pump_speed: Optional[int] = None
    # populated by menu navigator reads; None = not yet read
    pool_setpoint_f: Optional[int] = None
    spa_setpoint_f: Optional[int] = None
    pool_heater_enabled: Optional[bool] = None
    spa_heater_enabled: Optional[bool] = None
    valve_mode: Optional[str] = None   # 'pool' | 'spa'
    vsp_slot4_pct: Optional[int] = None
    connected: bool = False
    last_update: float = 0.0

state = PoolState()
state_lock = threading.Lock()

panel = None
panel_lock = threading.Lock()

# ---------------------------------------------------------------------------
# WiFi-bridge write reliability (keep-alive window targeting)
#
# The aqualogic library sends a queued key frame once, right after it reads a
# keep-alive. The AquaLogic panel only accepts a keypress in a narrow window
# that follows each keep-alive. Over a direct serial wire the reactive send
# lands; over the USR-W610 WiFi serial bridge it arrives too late and the panel
# silently drops it (Bad CRC), so writes fail while reads work.
#
# Empirically (10ms-resolution sweep on real hardware, see commit history) the
# panel's accept window for THIS bridge sits ~70ms after we receive a
# keep-alive: per-frame landing rate peaks sharply at predelay=70ms (~60%) and
# is near-zero at 0-50ms and 90ms+. So we delay KEY_PREDELAY_MS after the
# keep-alive, then write the frame ONCE.
#
# Bursting (writing the frame several times in quick succession) was tried and
# is actively HARMFUL on this bridge: the W610 packs the rapid writes into a
# merged serial run that the panel rejects wholesale — a 3x burst spanning the
# 70ms peak scored 0/10 where a single frame at 70ms scored 6/10. RS-485
# turnaround padding was likewise a dead end (the W610 turnaround is ~75µs,
# ~0.1 of a byte time, far too small to corrupt the frame header). Both are
# kept as no-default tunables (burst=1, pad=0) purely for diagnostics.
#
# Per-frame reliability is ~60%, so set_circuit re-presses up to KEY_MAX_RETRIES
# times, checking the real panel state before each press and stopping the
# instant it lands. 8 independent 60% shots => ~99.9% — measured 10/10 in situ.
KEY_BURST = 1            # single frame; >1 is harmful on the W610 (diagnostics only)
KEY_PREDELAY_MS = 70.0   # measured center of the panel's post-keep-alive window
KEY_GAP_MS = 10.0        # only used when KEY_BURST > 1 (diagnostics)
KEY_PAD_BYTES = 0        # RS-485 turnaround padding; 0 = off (diagnostics only)
# Max seconds to wait after a press for a clean LEDs frame to confirm the toggle.
# Measured: a press that lands confirms in ~0.43s (one LEDs broadcast). Setting
# this to 1.0s gives 2.3x margin with no false-re-press risk, and cuts the
# per-miss retry cost from 3.0s to 1.0s — driving mean latency down significantly.
KEY_VERIFY_DELAY_S = 1.0
# Re-press on a genuine miss. At ~60%/press, 12 retries gives 1-(0.4^12) > 99.99%.
# set_circuit checks real panel state before every press and stops immediately on
# success, so extra retries cannot overshoot. Validated 100/100 in situ:
# median=0.43s, mean=1.91s, p90=5.63s, max=12.1s, zero failures.
KEY_MAX_RETRIES = 12


def _install_key_burst(AquaLogic) -> None:
    """Monkeypatch AquaLogic._send_frame and get_state."""
    import binascii

    def _send_frame_burst(self) -> None:
        if self._send_queue.empty():
            return
        data = self._send_queue.get(block=False)
        frame = data['frame']
        # Optional RS-485 turnaround padding (default off). Proven unnecessary
        # on the W610 (~75µs turnaround) but kept as a diagnostic lever.
        out = bytes(KEY_PAD_BYTES) + bytes(frame) if KEY_PAD_BYTES else frame
        # Wait for the panel's post-keep-alive accept window, then write once.
        # KEY_BURST > 1 writes repeatedly — proven HARMFUL on this bridge (the
        # W610 merges rapid writes); kept only for diagnostics.
        time.sleep(KEY_PREDELAY_MS / 1000.0)
        for _ in range(max(1, KEY_BURST)):
            self._write(out)
            if KEY_BURST > 1:
                time.sleep(KEY_GAP_MS / 1000.0)
        log.info('Sent (x%d pad=%d predelay=%.0fms): %s', KEY_BURST,
                 KEY_PAD_BYTES, KEY_PREDELAY_MS, binascii.hexlify(frame).decode())
        # No async _check_state requeue. Verification is done synchronously in
        # RealPanel.set_circuit under _nav_lock using _actual_state(), which
        # reads _states directly and avoids the optimistic queue-peek problem.

    def _get_state_safe(self, state):
        """get_state patched to handle raw send_key frames (no desired_states)."""
        for data in list(self._send_queue.queue):
            desired_states = data.get('desired_states')
            if desired_states is None:
                continue
            for desired_state in desired_states:
                if desired_state['state'] == state:
                    return desired_state['enabled']
        if state.value == 0x80000000:  # States.FILTER_LOW_SPEED
            return (0x20 & self._flashing_states) != 0  # FILTER bit
        return (state.value & self._states) != 0

    # Patch LONG_DISPLAY_UPDATE handling in process().
    # The library's process() ignores LONG_DISPLAY_UPDATE (0x04 0x0a) with
    # '# Not currently parsed / pass'. But during menu navigation the panel
    # sends LONG frames (full 2×16 LCD), NOT short DISPLAY_UPDATE (0x01 0x03)
    # frames. Without this patch, lcd.text_updated() is never called during
    # menu navigation, so _send() always times out and _press_until() burns
    # its entire budget thinking every RIGHT/PLUS/MINUS was dropped.
    #
    # Fix: replace the body of the LONG branch with the same decode+callback
    # that the short branch uses, plus bit-7 stripping for flashing chars
    # (characters with bit 7 set blink on the physical display per PR #11).
    import inspect, textwrap, types

    src = inspect.getsource(AquaLogic.process)
    old_stub = (
        'elif frame_type == self.FRAME_TYPE_LONG_DISPLAY_UPDATE:\n'
        '                    # Not currently parsed\n'
        '                    pass'
    )
    new_body = (
        'elif frame_type == self.FRAME_TYPE_LONG_DISPLAY_UPDATE:\n'
        '                    # Preserve 0xDF (LCD degree char) before masking\n'
        '                    # bit 7; otherwise 0xDF & 0x7F = 0x5F = "_".\n'
        '                    raw = bytes(b if b == 0xdf else (b & 0x7f) for b in frame)\n'
        '                    text = raw.replace(b\'\\xdf\', b\'\\xc2\\xb0\').decode(\'utf-8\', errors=\'replace\')\n'
        '                    self._web.text_updated(text)'
    )
    if old_stub not in src:
        log.warning('LONG_DISPLAY_UPDATE patch: expected stub not found in '
                    'aqualogic.core.AquaLogic.process — menu LCD updates will '
                    'not fire. Check library version.')
    else:
        import aqualogic.core as _aq_core
        patched_src = textwrap.dedent(src.replace(old_stub, new_body))
        globs = vars(_aq_core).copy()
        globs['__name__'] = _aq_core.__name__
        exec(compile(patched_src, inspect.getfile(AquaLogic), 'exec'), globs)
        AquaLogic.process = globs['process']
        log.info('LONG_DISPLAY_UPDATE patch applied: menu navigation LCD '
                 'updates will now reach LcdCapture.')

    AquaLogic._send_frame = _send_frame_burst
    AquaLogic.get_state = _get_state_safe
    log.info('Key-burst send enabled: burst=%d predelay=%.0fms gap=%.0fms pad=%d',
             KEY_BURST, KEY_PREDELAY_MS, KEY_GAP_MS, KEY_PAD_BYTES)

CIRCUIT_NAMES = [
    'POOL', 'SPA', 'FILTER', 'LIGHTS',
    'SPILLOVER', 'AUX_1', 'AUX_2', 'HEATER_1', 'SUPER_CHLORINATE',
]


def _read_property(obj, name, default=None):
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _norm(text: str) -> str:
    """Normalize a raw LCD frame for matching.

    Real hardware sends the 16x2 display as one ~32-char string: the two
    visual lines packed side by side, padded with spaces, no newline, often
    with a trailing NUL. (Simulation emits clean 'Line1\\nLine2', which is why
    the old two-line l1/l2 split worked in sim but never on hardware.) The
    aqualogic library itself tokenizes with text.split(); we mirror that:
    drop NULs and collapse every whitespace run to a single space. So
    '     Pool Chlorinator        30%   \\x00' -> 'Pool Chlorinator 30%'.

    LONG_DISPLAY_UPDATE frames also carry LCD cursor-position control bytes
    (e.g. a leading '\\x03\\x03' or '\\x03\\x02(\\x03') that appear on the
    fresh frame and vanish on the next rebroadcast of the same screen. Left
    in, the identical screen reads as two different strings and _send() sees
    a spurious 'change' on every press. Strip every control char (< 0x20).

    Some LCD highlight/cursor bytes have values 0x80-0xFF; after the LONG
    frame's bit-7 mask they become printable ASCII (e.g. 0xAD->'−', 0xA9→')').
    These appear at the START of the frame, before the actual content. Strip
    any leading non-alphanumeric characters so '-  Super Chlorinate Off' and
    ') Pool Chlorinator 50%' normalize identically to their undecorated forms.
    All valid menu item names start with a letter or digit (§4); the '+' in
    '+ to enter' is always mid-string after the item label.
    """
    if not text:
        return ''
    cleaned = ''.join(c if ord(c) >= 0x20 else ' ' for c in text)
    result = ' '.join(cleaned.split())
    # Strip leading non-alphanumeric chars (masked cursor/highlight bytes).
    result = re.sub(r'^[^A-Za-z0-9]*', '', result)
    return result


# ---------------------------------------------------------------------------
# LCD capture
#
# aqualogic never stores the raw LCD text — on every display frame it calls
# self._web.text_updated(text) and pushes to a WebSocket, then forgets it.
# We construct AquaLogic(web_port=0) (suppresses the built-in web server) and
# then set aq._web = lcd so every frame lands here instead.
# ---------------------------------------------------------------------------

class LcdCapture:
    def __init__(self, maxhist: int = 60):
        self._lock = threading.Lock()
        self._latest: Optional[str] = None
        self._ts: float = 0.0
        self._event = threading.Event()
        self.history: deque = deque(maxlen=maxhist)

    def text_updated(self, text: str) -> None:
        with self._lock:
            self._latest = text
            self._ts = time.time()
            self.history.append((self._ts, text))
        self._event.set()

    # aqualogic may call other no-op methods on its _web object; absorb them.
    def __getattr__(self, name):
        return lambda *a, **k: None

    def wait_for_change(self, timeout: float = 4.0) -> bool:
        """Block until the next text_updated(), up to timeout seconds."""
        self._event.clear()
        return self._event.wait(timeout)

    def lines(self) -> Tuple[str, str]:
        """Return current (line1, line2) from the latest LCD frame.

        Kept for the simulation path (which emits a real newline). On hardware
        there is no newline so l2 is empty — navigator code must use text().
        """
        with self._lock:
            text = self._latest or ''
        parts = text.split('\n', 1)
        l1 = parts[0].strip()
        l2 = parts[1].strip() if len(parts) > 1 else ''
        return l1, l2

    def text(self) -> str:
        """Return the latest LCD frame normalized to a single matchable string."""
        with self._lock:
            return _norm(self._latest or '')

    def snapshot(self):
        with self._lock:
            return [(ts, t) for ts, t in self.history]


lcd = LcdCapture()

# One menu operation at a time — keypad navigation is not re-entrant.
_nav_lock = threading.Lock()


class UnsupportedCircuit(Exception):
    """Raised when a circuit has no corresponding keypad key to toggle it."""


# ---------------------------------------------------------------------------
# Real panel adapter
# ---------------------------------------------------------------------------

class RealPanel:
    def __init__(self, aq, States, Keys):
        self._aq = aq
        self._States = States
        self._Keys = Keys
        self._smap = {n: getattr(States, n) for n in CIRCUIT_NAMES}

    def set_circuit(self, name: str, on: bool) -> bool:
        s = self._smap.get(name)
        if s is None:
            raise KeyError(name)
        # set_state(States.HEATER_1, on) is a no-op stub in the aqualogic lib
        # (it never sends a key). The keypad HEATER_1 button toggles Auto vs
        # Manual Off, which the lib models as HEATER_AUTO_MODE — that path
        # actually sends Keys.HEATER_1. Route the heater enable through it.
        target = self._States.HEATER_AUTO_MODE if s == self._States.HEATER_1 else s

        # Serialize against the menu navigator (heater refresher) under
        # _nav_lock: both feed the same RS-485 send queue, and interleaved
        # keypresses corrupt each other (Bad CRC) and lose the toggle.
        #
        # Verify using _actual_state(), NOT get_state(). get_state() is
        # optimistic — it peeks at the send queue and returns the *desired*
        # state the instant set_state() queues the frame, so our verify loop
        # would declare success before the burst even fires. _actual_state()
        # reads aq._states (the raw LEDs bit-field) directly, which only
        # updates when the panel broadcasts a clean LEDs frame after the press.
        with _nav_lock:
            for _ in range(max(1, KEY_MAX_RETRIES)):
                if self._actual_state(target) == on:
                    return True  # already in / reached desired state
                # set_state queues one burst toggle, or returns False if this
                # state has no keypad key (unsupported circuit).
                if not self._aq.set_state(target, on):
                    raise UnsupportedCircuit(name)
                # Wait for the burst to fire (queue drains in the process loop).
                t0 = time.time()
                while not self._aq._send_queue.empty() and time.time() - t0 < 3.0:
                    time.sleep(0.1)
                # Poll the real panel state; return the instant a clean LEDs
                # frame confirms the toggle, else re-press after the ceiling.
                deadline = time.time() + KEY_VERIFY_DELAY_S
                while time.time() < deadline:
                    time.sleep(0.3)
                    if self._actual_state(target) == on:
                        return True
            return self._actual_state(target) == on  # unconfirmed after retries

    def _actual_state(self, state) -> bool:
        """Read the panel's confirmed bit-field, bypassing get_state's optimism."""
        if state == self._States.HEATER_AUTO_MODE:
            return bool(self._aq._heater_auto_mode)
        return bool(state.value & self._aq._states)

    def send_key(self, name: str) -> None:
        k = getattr(self._Keys, name, None)
        if k is None:
            raise ValueError(f'Unknown key: {name!r}')
        self._aq.send_key(k)


def panel_thread(host: str, port: int) -> None:
    global panel

    from aqualogic.core import AquaLogic
    from aqualogic.states import States
    from aqualogic.keys import Keys

    # Always install: even at burst=1 we need the keep-alive window targeting
    # (predelay) and the get_state patch that tolerates raw send_key frames.
    _install_key_burst(AquaLogic)

    smap = {n: getattr(States, n) for n in CIRCUIT_NAMES}

    def on_change(aq) -> None:
        l1, l2 = lcd.lines()
        with state_lock:
            state.connected = True
            state.last_update = time.time()
            state.pool_temp = _read_property(aq, 'pool_temp')
            state.air_temp = _read_property(aq, 'air_temp')
            state.spa_temp = _read_property(aq, 'spa_temp')
            state.salt_level = _read_property(aq, 'salt_level')
            state.chlorinator_percent = _read_property(aq, 'pool_chlorinator')
            state.pump_speed = _read_property(aq, 'pump_speed')
            for name, s in smap.items():
                try:
                    state.circuits[name] = bool(aq.get_state(s))
                except Exception:
                    pass
            # HEATER_1's broadcast bit (States.HEATER_1) is the *relay* — true
            # only while actively calling for heat. The enable state the keypad
            # HEATER_1 button toggles is Auto vs Manual Off = HEATER_AUTO_MODE.
            # Report the enable bit so the switch/thermostat tiles track it.
            try:
                state.circuits['HEATER_1'] = bool(aq.get_state(States.HEATER_AUTO_MODE))
            except Exception:
                pass
            # Parse valve mode from default cycling display (§10).
            # The panel's "Filter Speed  NN% Pool/Spa Mode" frame carries the
            # active mode. On this hardware the LCD text has no newline, so the
            # whole 32-char frame lands in l1 and l2 is empty — match against
            # the joined frame, not l2 alone. "Spa Mode"/"Pool Mode" are
            # distinct from "Spa Temp"/"Spa-CountDn" so this won't false-match.
            frame = f'{l1} {l2}'
            if 'Pool Mode' in frame:
                state.valve_mode = 'pool'
            elif 'Spa Mode' in frame:
                state.valve_mode = 'spa'

    while True:
        try:
            log.info(f'Connecting to serial bridge at {host}:{port}')
            aq = AquaLogic(web_port=0)  # suppress built-in web server
            aq._web = lcd               # intercept every LCD frame
            aq.connect(host, port)
            # CRITICAL for WiFi-bridge write reliability: disable Nagle's
            # algorithm on the socket. The aqualogic lib opens a raw TCP socket
            # and never sets TCP_NODELAY, so our tiny key frames (~15 bytes) get
            # held by Nagle until the previous segment is ACKed. Combined with
            # the bridge's delayed-ACK this is the classic ~40ms Nagle stall —
            # the key frame leaves at a random 0-40ms offset and almost always
            # misses the panel's narrow post-keep-alive poll window (Bad CRC).
            # With Nagle off the frame goes on the wire immediately, so the
            # reactive send lands in the window far more reliably.
            try:
                import socket as _socket
                aq._socket.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
                log.info('TCP_NODELAY set on bridge socket (Nagle disabled)')
            except Exception as e:
                log.warning('Could not set TCP_NODELAY: %s', e)
            with panel_lock:
                panel = RealPanel(aq, States, Keys)
            aq.process(on_change)       # blocks until connection drops
        except Exception as e:
            log.error(f'Connection lost: {e}')
        finally:
            with panel_lock:
                panel = None
            with state_lock:
                state.connected = False
        log.info('Reconnecting in 5 seconds...')
        time.sleep(5)


# ---------------------------------------------------------------------------
# Simulation panel + menu state machine
#
# Implements the verified Settings menu ring and VSP submenu (§4, §6, §12 of
# the automation spec) so the full navigator code path is exercisable without
# any hardware.  Values and menu strings match what was observed live.
# ---------------------------------------------------------------------------
# Chlorinator step helpers
#
# Below 10%: 1% increments.  At 10% and above: 5% increments.
# Valid positions: 0–9 (1% steps) then 10, 15, 20 … 100 (5% steps).
# ---------------------------------------------------------------------------

def _chlor_step_up(pct: int) -> int:
    if pct < 10:
        return pct + 1
    return min(100, (pct // 5 + 1) * 5)


def _chlor_step_down(pct: int) -> int:
    if pct <= 10:
        return max(0, pct - 1)
    return max(10, (pct - 1) // 5 * 5)


def _chlor_snap(pct: int) -> int:
    """Snap an arbitrary % to the nearest valid chlorinator position."""
    pct = int(_clamp(pct, 0, 100))
    if pct <= 9:
        return pct
    return round(pct / 5) * 5


def _chlor_presses(current: int, target: int):
    """Return (key, n_presses) to move chlorinator from current to target."""
    if current == target:
        return 'PLUS', 0
    going_up = target > current
    key = 'PLUS' if going_up else 'MINUS'
    pct = current
    n = 0
    while pct != target:
        pct = _chlor_step_up(pct) if going_up else _chlor_step_down(pct)
        n += 1
        if n > 200:
            raise RuntimeError(f'Chlorinator step loop did not reach {target} from {current}')
    return key, n


# ---------------------------------------------------------------------------

class SimPanel:
    _MENU_RING = [
        'settings_menu', 'timers_menu', 'diagnostic_menu',
        'config_menu_locked', 'default_menu',
    ]
    _SETTINGS_RING = [
        'settings_menu', 'spa_heater', 'pool_heater',
        'vsp_settings',
        'super_chlorinate', 'spa_chlorinator', 'pool_chlorinator',
        'set_day_time', 'display_light', 'beeper',
        'teach_wireless', 'wireless_channel',
    ]
    _VSP_ITEMS = ['vsp_speed1', 'vsp_speed2', 'vsp_speed3', 'vsp_speed4', 'spa_speed']

    def __init__(self):
        self._ms = 'default'
        self._in_vsp = False
        # Values matching verified live observations
        self._pool_heater_on = False      # Manual Off
        self._spa_heater_on = False       # Manual Off
        self._pool_setpoint = 87
        self._spa_setpoint = 99
        self._vsp = [95, 60, 50, 50]     # slots 1-4
        self._spa_speed = 80
        self._pool_chlor = 30
        self._spa_chlor = 1
        self._super_chlor = False
        self._mode = 'pool'               # 'pool' | 'spa'
        # VSP activation window (§6.2/§6.4): FILTER off→on triggers a
        # ~5-10s slot-selection window.  In simulation it stays open until
        # another key exits it (matches the real panel UX).
        self._filter_window = False
        self._active_slot = 1             # currently-running VSP slot (1-5)
        self._push_lcd()

    def _render(self) -> Tuple[str, str]:
        # §6.4: slot-selection window overrides all other display states.
        if self._filter_window:
            return f'Filter On:Spd{self._active_slot}', '+/- to change'
        s = self._ms
        if s == 'default':
            return 'Pool 82\xb0F  Air 75\xb0F', f'Filter Speed / Off  {self._mode.title()} Mode'
        if s == 'settings_menu':          return 'Settings Menu', ''
        if s == 'timers_menu':            return 'Timers Menu', ''
        if s == 'diagnostic_menu':        return 'Diagnostic Menu', ''
        if s == 'config_menu_locked':     return 'Configuration Menu-Locked', ''
        if s == 'default_menu':           return 'Default Menu', ''
        if s == 'spa_heater':
            v = f'{self._spa_setpoint}\xb0F' if self._spa_heater_on else 'Manual Off'
            return 'Spa Heater1', v
        if s == 'pool_heater':
            v = f'{self._pool_setpoint}\xb0F' if self._pool_heater_on else 'Manual Off'
            return 'Pool Heater1', v
        if s == 'vsp_settings':           return 'VSP Speed Settings', '+ to enter'
        if s == 'vsp_speed1':             return 'Filter Speed1', f'{self._vsp[0]}%'
        if s == 'vsp_speed2':             return 'Filter Speed2', f'{self._vsp[1]}%'
        if s == 'vsp_speed3':             return 'Filter Speed3', f'{self._vsp[2]}%'
        if s == 'vsp_speed4':             return 'Filter Speed4', f'{self._vsp[3]}%'
        if s == 'spa_speed':              return 'Spa Speed', f'{self._spa_speed}%'
        if s == 'super_chlorinate':       return 'Super Chlorinate', 'On' if self._super_chlor else 'Off'
        if s == 'spa_chlorinator':        return 'Spa Chlorinator', f'{self._spa_chlor}%'
        if s == 'pool_chlorinator':       return 'Pool Chlorinator', f'{self._pool_chlor}%'
        if s == 'set_day_time':           return 'Set Day and Time', 'Saturday 10:07P'
        if s == 'display_light':          return 'Display Light', 'No Backlight Present'
        if s == 'beeper':                 return 'Beeper', 'Not Used Here'
        if s == 'teach_wireless':         return 'Teach Wireless:', '+ to start'
        if s == 'wireless_channel':       return 'Wireless Channel:', '3'
        return s, ''

    def _push_lcd(self) -> None:
        l1, l2 = self._render()
        lcd.text_updated(f'{l1}\n{l2}')

    def send_key(self, name: str) -> None:
        # §6.4: while the slot-selection window is open, only PLUS/MINUS cycle
        # slots; any other key (except another FILTER toggle) closes the window.
        if self._filter_window:
            if name == 'PLUS':
                self._active_slot = (self._active_slot % 5) + 1
                self._push_lcd()
                return
            elif name == 'MINUS':
                self._active_slot = ((self._active_slot - 2) % 5) + 1
                self._push_lcd()
                return
            elif name != 'FILTER':
                self._filter_window = False
                # fall through to normal key handling for the received key

        s = self._ms
        if name == 'MENU':
            self._in_vsp = False
            ring = self._MENU_RING
            self._ms = ring[(ring.index(s) + 1) % len(ring)] if s in ring else 'settings_menu'
        elif name == 'RIGHT':
            if self._in_vsp:
                vsp = self._VSP_ITEMS
                if s in vsp:
                    i = vsp.index(s)
                    if i + 1 >= len(vsp):
                        self._ms = 'super_chlorinate'  # exit VSP; next main-ring item
                        self._in_vsp = False
                    else:
                        self._ms = vsp[i + 1]
                else:
                    self._ms = 'super_chlorinate'
                    self._in_vsp = False
            elif s in self._SETTINGS_RING:
                self._ms = self._SETTINGS_RING[
                    (self._SETTINGS_RING.index(s) + 1) % len(self._SETTINGS_RING)]
        elif name == 'LEFT':
            if s in self._SETTINGS_RING:
                self._ms = self._SETTINGS_RING[
                    (self._SETTINGS_RING.index(s) - 1) % len(self._SETTINGS_RING)]
        elif name == 'PLUS':
            if s == 'vsp_settings':
                self._in_vsp = True
                self._ms = 'vsp_speed1'
            elif s == 'pool_heater':
                if not self._pool_heater_on:
                    self._pool_heater_on = True   # reveal stored setpoint
                else:
                    self._pool_setpoint = min(104, self._pool_setpoint + 1)
            elif s == 'spa_heater':
                if not self._spa_heater_on:
                    self._spa_heater_on = True
                else:
                    self._spa_setpoint = min(104, self._spa_setpoint + 1)
            elif s == 'vsp_speed4':
                self._vsp[3] = min(100, self._vsp[3] + 5)
            elif s == 'pool_chlorinator':
                self._pool_chlor = _chlor_step_up(self._pool_chlor)
            elif s == 'spa_chlorinator':
                self._spa_chlor = _chlor_step_up(self._spa_chlor)
        elif name == 'MINUS':
            if s == 'pool_heater' and self._pool_heater_on:
                self._pool_setpoint = max(65, self._pool_setpoint - 1)
            elif s == 'spa_heater' and self._spa_heater_on:
                self._spa_setpoint = max(65, self._spa_setpoint - 1)
            elif s == 'vsp_speed4':
                self._vsp[3] = max(0, self._vsp[3] - 5)
            elif s == 'pool_chlorinator':
                self._pool_chlor = _chlor_step_down(self._pool_chlor)
            elif s == 'spa_chlorinator':
                self._spa_chlor = _chlor_step_down(self._spa_chlor)
        elif name == 'HEATER_1':
            # Toggle heater enable. In sim, the toggle applies to whichever heater
            # item is currently displayed (or the mode-active one from Default).
            if s == 'pool_heater':
                self._pool_heater_on = not self._pool_heater_on
            elif s == 'spa_heater':
                self._spa_heater_on = not self._spa_heater_on
            else:
                if self._mode == 'pool':
                    self._pool_heater_on = not self._pool_heater_on
                else:
                    self._spa_heater_on = not self._spa_heater_on
        elif name == 'POOL_SPA':
            modes = ['pool', 'spa']
            self._mode = modes[(modes.index(self._mode) + 1) % len(modes)]
        elif name == 'FILTER':
            with state_lock:
                was_on = state.circuits.get('FILTER', False)
                state.circuits['FILTER'] = not was_on
            if not was_on:
                # Filter just turned ON → enter slot-selection window (§6.2)
                self._filter_window = True
            else:
                self._filter_window = False
        self._push_lcd()

    def set_circuit(self, name: str, on: bool) -> bool:
        if name not in CIRCUIT_NAMES:
            raise KeyError(name)
        if name in ('SUPER_CHLORINATE', 'SPILLOVER'):
            return False
        with state_lock:
            state.circuits[name] = on
            if name == 'POOL' and on:
                state.circuits['SPA'] = False
            elif name == 'SPA' and on:
                state.circuits['POOL'] = False
            state.last_update = time.time()
        return True


def simulate_thread() -> None:
    global panel

    with state_lock:
        state.connected = True
        state.last_update = time.time()
        state.pool_temp = 82.0
        state.air_temp = 75.0
        state.spa_temp = 99.0
        state.salt_level = 3200.0
        state.chlorinator_percent = 30.0
        state.pump_speed = 0
        state.valve_mode = 'pool'
        for name in CIRCUIT_NAMES:
            state.circuits[name] = False
        state.circuits['POOL'] = True

    sim = SimPanel()
    with panel_lock:
        panel = sim

    log.info('SIMULATION mode — full menu state machine active.')

    while True:
        time.sleep(5)
        with state_lock:
            state.pool_temp = round(_clamp(state.pool_temp + random.uniform(-0.3, 0.3), 70, 92), 1)
            state.air_temp = round(_clamp(state.air_temp + random.uniform(-0.5, 0.5), 50, 100), 1)
            state.last_update = time.time()


# ---------------------------------------------------------------------------
# Menu navigator
#
# Closed-loop state machine per docs/aqualogic-automation-spec.md §3/§11.
# Every public method holds _nav_lock for its full duration so operations
# cannot interleave.  fast_exit() is always called in a finally block so a
# mid-operation error does not leave the panel stuck in a menu.
# ---------------------------------------------------------------------------

# Per-keypress navigation trace (most recent first via reversed view). Each
# entry: {seq, ts, key, before, after, changed, expect_change, wait_s}.
# Read it back over /debug/nav-trace to replay a failed walk against the spec.
_NAV_TRACE: "deque[dict]" = deque(maxlen=400)
_NAV_TRACE_LOCK = threading.Lock()
_NAV_SEQ = [0]


def _trace_key(key: str, before: str, after: str, wait_s: float,
               expect_change: bool) -> None:
    with _NAV_TRACE_LOCK:
        _NAV_SEQ[0] += 1
        seq = _NAV_SEQ[0]
        entry = {
            'seq': seq,
            'ts': round(time.time(), 3),
            'key': key,
            'before': before,
            'after': after,
            'changed': after != before,
            'expect_change': expect_change,
            'wait_s': round(wait_s, 3),
        }
        _NAV_TRACE.append(entry)
    log.info('NAV #%d %-7s %4.0fms %s | %r -> %r', seq, key, wait_s * 1000,
             'CHG' if entry['changed'] else 'same', before, after)


class MenuNavigator:
    _SETTINGS_HDR = 'Settings Menu'
    _DEFAULT_MENU_HDR = 'Default Menu'
    _KEY_TIMEOUT = 4.0   # seconds to wait for the frame to change after a press
    _MENU_MAX = 30       # MENU presses before aborting anchor (×2 due to SHORT/LONG oscillation)
    _NAV_MAX = 100       # ring RIGHT presses; oscillation burns ~7 per item, 11 items × 7 = 77 worst case
    _STEP_MAX = 90       # +/- presses before aborting a value adjust

    def __init__(self, p, l: LcdCapture):
        self._panel = p
        self._lcd = l

    def text(self) -> str:
        """Current normalized LCD frame."""
        return self._lcd.text()

    def _send(self, key: str, expect_change: bool = True) -> str:
        """Send one key and return the resulting normalized frame.

        Waits for a *meaningful* display change — one that represents a real
        navigation step or value change — and ignores transient noise:

        • Garbled frames (not starting with an uppercase letter) are transient
          bus artifacts; we treat them as the same item and keep waiting.
        • Value-flash frames (value field blanks, label stays — one is a prefix
          of the other) are the same menu item; we keep waiting.
        • A real change: navigation to a different item, or a value digit change.

        A dropped press (~40% on the WiFi bridge) causes KEY_TIMEOUT with the
        text unchanged; callers re-press. Every press is appended to _NAV_TRACE.
        """
        before = self._lcd.text()
        t0 = time.time()
        self._panel.send_key(key)
        if not expect_change:
            after = self._lcd.text()
            _trace_key(key, before, after, time.time() - t0, expect_change)
            return after
        deadline = t0 + self._KEY_TIMEOUT
        while time.time() < deadline:
            if not self._same_item(self._lcd.text(), before):
                break
            self._lcd._event.clear()
            self._lcd._event.wait(min(0.5, max(0.0, deadline - time.time())))
        after = self._lcd.text()
        _trace_key(key, before, after, time.time() - t0, expect_change)
        return after

    def _wait_key_sent(self, timeout: float = 2.5) -> None:
        """Block until a queued keypress has been transmitted on the bus.

        The real panel queues frames and writes them on the next keep-alive;
        we wait for the send queue to drain, then a short settle so the write
        (which the burst does ~predelay after dequeue) has completed. The
        simulator is synchronous and has no queue, so this is a no-op there.
        """
        aq = getattr(self._panel, '_aq', None)
        q = getattr(aq, '_send_queue', None) if aq is not None else None
        if q is None:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            if q.empty():
                break
            time.sleep(0.05)
        # Burst sleeps KEY_PREDELAY_MS after dequeue before the actual write;
        # add that plus a small margin so the press has truly landed.
        time.sleep(KEY_PREDELAY_MS / 1000.0 + 0.05)

    @staticmethod
    def _same_item(a: str, b: str) -> bool:
        """True if two frames are the same menu item (ignoring a value flash).

        The selected value flashes, so an item shows as both 'Label Value' and
        'Label' (value blanked). One is a prefix of the other. A real
        navigation step or a value change (50% -> 45%) is NOT a prefix match.
        """
        a, b = (a or '').strip(), (b or '').strip()
        if a == b:
            return True
        # Garbled bus frames (transient RS-485 noise) never start with an
        # uppercase letter. Treat them as same-item so _send doesn't exit
        # on a garbled frame before the panel processes the keypress.
        if not a or not a[0].isupper():
            return True
        if not b or not b[0].isupper():
            return True
        short, lng = (a, b) if len(a) <= len(b) else (b, a)
        return bool(short) and lng.startswith(short)

    def _read_value(self, parser, timeout: float = 1.6):
        """Read a parseable value, waiting through value-flash blank frames.

        The value field flashes, so a single read can catch the blanked frame
        (parser -> None). Poll across the flash until the value is visible.
        """
        val = parser(self._lcd.text())
        deadline = time.time() + timeout
        while val is None and time.time() < deadline:
            self._lcd._event.clear()
            self._lcd._event.wait(min(0.4, max(0.0, deadline - time.time())))
            val = parser(self._lcd.text())
        return val

    def _press_until(self, key: str, ok, budget: int, what: str) -> str:
        """Press `key` until ok(normalized_text) is True, re-pressing on misses.

        Each landed press advances one menu position; a dropped press leaves us
        where we were and is simply re-pressed. We stop the instant the target
        appears, so this never overshoots a distinct, named target.
        """
        txt = self._lcd.text()
        if ok(txt):
            return txt
        for _ in range(budget):
            txt = self._send(key)
            if ok(txt):
                return txt
        raise RuntimeError(f'Could not reach {what}; stuck at {self._lcd.text()!r}')

    def _step_to(self, parser, target: int, up_key: str, down_key: str,
                 budget: int, what: str) -> int:
        """Drive a numeric setting to `target`, re-reading after every press.

        `parser` extracts the current value from the LCD frame; we read it
        through the value-flash with _read_value so a blanked frame never
        reads as None mid-step. Robust to dropped presses (value unchanged ->
        re-press) and overshoot (direction chosen fresh each iteration);
        converges because we stop on equality.
        """
        for _ in range(budget):
            cur = self._read_value(parser)
            if cur is None:
                raise RuntimeError(f'Cannot read {what} value at {self._lcd.text()!r}')
            if cur == target:
                return cur
            self._send(up_key if target > cur else down_key)
        cur = self._read_value(parser)
        if cur != target:
            raise RuntimeError(f'Could not set {what} to {target}; at {cur} ({self._lcd.text()!r})')
        return cur

    def _anchor(self) -> None:
        """Drive MENU until the normalized frame is exactly 'Settings Menu'."""
        self._press_until('MENU', lambda t: t == self._SETTINGS_HDR,
                          self._MENU_MAX, self._SETTINGS_HDR)

    def fast_exit(self) -> None:
        """Return to the Default display: MENU until 'Default Menu', then RIGHT.

        Holds _nav_lock for its duration. fast_exit runs from each operation's
        `finally`, which executes *after* the operation's own `with _nav_lock`
        block has released — so without re-acquiring here, two operations'
        exits (or an exit racing the next operation) press keys concurrently
        and corrupt each other's navigation. Acquire the lock to serialize.
        """
        with _nav_lock:
            try:
                self._press_until('MENU', lambda t: t == self._DEFAULT_MENU_HDR,
                                  self._MENU_MAX, self._DEFAULT_MENU_HDR)
            except RuntimeError:
                return  # best-effort; never raise out of a finally cleanup
            self._send('RIGHT', expect_change=False)

    # ── Value parsers (operate on the normalized full frame) ─────────────────

    _HEATER_LABEL = {'pool': 'Pool Heater1', 'spa': 'Spa Heater1'}
    _CHLOR_LABEL = {'pool': 'Pool Chlorinator', 'spa': 'Spa Chlorinator'}

    @staticmethod
    def _pct(text: str):
        """Extract an integer percent from a normalized frame, or None."""
        m = re.search(r'(\d+)\s*%', text)
        return int(m.group(1)) if m else None

    @staticmethod
    def _degf(text: str):
        """Extract a heater setpoint °F, or None if Manual Off / unreadable.

        Matches the digits immediately before the degree/F marker so it never
        picks up the '1' in 'Heater1'.
        """
        if 'Manual Off' in text:
            return None
        m = re.search(r'(\d+)\s*(?:°|\xb0)?\s*F', text)
        return int(m.group(1)) if m else None

    # ── Heater setpoints ─────────────────────────────────────────────────────

    def read_heater(self, which: str) -> dict:
        """Navigate to a heater item and read its state without changing it.

        When the heater is 'Manual Off' the panel shows no temperature, so we
        press PLUS to reveal the stored setpoint, record it, then re-disable via
        the HEATER_1 toggle so the state is unchanged on exit.
        """
        if which not in ('pool', 'spa'):
            raise ValueError('which must be "pool" or "spa"')
        label = self._HEATER_LABEL[which]
        try:
            with _nav_lock:
                self._anchor()
                txt = self._press_until('RIGHT', lambda t: label in t,
                                        self._NAV_MAX, label)
                was_off = 'Manual Off' in txt
                enabled = not was_off
                if was_off:
                    # PLUS from Manual Off reveals (and enables at) the stored °F.
                    txt = self._send('PLUS')
                setpoint_f = self._degf(txt)
                if was_off:
                    # Restore Manual Off: toggle HEATER_1 on the item until it shows.
                    self._press_until('HEATER_1', lambda t: 'Manual Off' in t,
                                      4, 'Manual Off (restore)')
                with state_lock:
                    if which == 'pool':
                        state.pool_heater_enabled = enabled
                        state.pool_setpoint_f = setpoint_f
                    else:
                        state.spa_heater_enabled = enabled
                        state.spa_setpoint_f = setpoint_f
                return {'which': which, 'enabled': enabled,
                        'setpoint_f': setpoint_f, 'raw': txt}
        finally:
            self.fast_exit()

    def set_heater_enabled(self, which: str, on: bool) -> dict:
        """
        Enable or disable a heater (Auto vs Manual Off) via menu navigation.

        This is the authoritative way to change heater enable state — the
        HEATER_1 keypad key from the default display does not reliably toggle
        the Manual Off menu state, and the HEATER_1 broadcast circuit reflects
        'actively calling for heat', not 'enabled'.

        - Enable from Manual Off: PLUS reveals the stored °F (heater now Auto),
          then RIGHT to lock in.
        - Disable from enabled: press HEATER_1 on the item until 'Manual Off'.
        Idempotent: if already in the requested state, does nothing.
        """
        if which not in ('pool', 'spa'):
            raise ValueError('which must be "pool" or "spa"')
        label = self._HEATER_LABEL[which]
        try:
            with _nav_lock:
                self._anchor()
                txt = self._press_until('RIGHT', lambda t: label in t,
                                        self._NAV_MAX, label)
                was_off = 'Manual Off' in txt
                if on and was_off:
                    # PLUS from Manual Off enables at the stored setpoint.
                    self._send('PLUS')
                elif not on and not was_off:
                    # Toggle HEATER_1 on the item until 'Manual Off' appears.
                    self._press_until('HEATER_1', lambda t: 'Manual Off' in t,
                                      4, 'Manual Off')
                with state_lock:
                    if which == 'pool':
                        state.pool_heater_enabled = on
                    else:
                        state.spa_heater_enabled = on
                return {'which': which, 'enabled': on, 'was_off': was_off}
        finally:
            self.fast_exit()

    def set_heater(self, which: str, target_f: int) -> dict:
        """
        Write a heater setpoint with restore-to-prior-state discipline (§13.3):
        - If heater is 'Manual Off': enable it (PLUS reveals stored °F), set temp,
          then re-disable via HEATER_1 toggle.
        - If already enabled: adjust temp only; leave enable state unchanged.
        Adjusts in 1°F steps.  target_f is clamped to [65, 104].
        """
        if which not in ('pool', 'spa'):
            raise ValueError('which must be "pool" or "spa"')
        target_f = int(_clamp(target_f, 65, 104))
        label = self._HEATER_LABEL[which]
        try:
            with _nav_lock:
                self._anchor()
                txt = self._press_until('RIGHT', lambda t: label in t,
                                        self._NAV_MAX, label)
                was_off = 'Manual Off' in txt
                if was_off:
                    # PLUS from 'Manual Off' enables the heater and reveals the
                    # stored °F (§12.2: non-symmetric — MINUS would not undo it).
                    self._send('PLUS')

                self._step_to(self._degf, target_f,
                              'PLUS', 'MINUS', self._STEP_MAX, label)

                if was_off:
                    # Restore Manual Off: toggle HEATER_1 on the item until shown.
                    self._press_until('HEATER_1', lambda t: 'Manual Off' in t,
                                      4, 'Manual Off (restore)')

                with state_lock:
                    if which == 'pool':
                        state.pool_setpoint_f = target_f
                        state.pool_heater_enabled = not was_off
                    else:
                        state.spa_setpoint_f = target_f
                        state.spa_heater_enabled = not was_off

                return {'which': which, 'target_f': target_f, 'was_off': was_off}
        finally:
            self.fast_exit()

    # ── Chlorinator output % ─────────────────────────────────────────────────

    def set_chlorinator(self, which: str, target_pct: int) -> dict:
        """
        Write a chlorinator output % via menu navigation.
        Step size is variable: 1% below 10%, 5% at 10% and above.
        target_pct is snapped to the nearest valid position before navigation.
        which = 'pool' | 'spa'
        """
        if which not in ('pool', 'spa'):
            raise ValueError('which must be "pool" or "spa"')
        target_pct = _chlor_snap(int(target_pct))
        label = self._CHLOR_LABEL[which]
        try:
            with _nav_lock:
                self._anchor()
                # Walk the Settings ring to the item by name (self-correcting
                # against dropped RIGHT presses), not by a fixed press count.
                txt = self._press_until('RIGHT', lambda t: label in t,
                                        self._NAV_MAX, label)
                previous_pct = self._pct(txt)
                # Step PLUS/MINUS to the target, re-reading after each press so
                # dropped presses re-press and any overshoot corrects itself.
                self._step_to(self._pct, target_pct,
                              'PLUS', 'MINUS', self._STEP_MAX, label)
                with state_lock:
                    state.chlorinator_percent = float(target_pct)
                return {'which': which, 'target_pct': target_pct,
                        'previous_pct': previous_pct}
        finally:
            self.fast_exit()

    # ── VSP slot 4 ───────────────────────────────────────────────────────────

    def _goto_vsp_slot4(self) -> str:
        """Anchor, enter the VSP submenu, and land on Filter Speed4. Returns frame."""
        self._anchor()
        self._press_until('RIGHT', lambda t: 'VSP Speed Settings' in t,
                          self._NAV_MAX, 'VSP Speed Settings')
        # PLUS enters the inline sub-items (-> 'Filter Speed1 ..%'); re-press
        # until we're actually inside, so a dropped PLUS doesn't leave us in the
        # main ring (which RIGHT would then walk the wrong way).
        self._press_until('PLUS', lambda t: 'Filter Speed' in t, 6, 'VSP submenu')
        return self._press_until('RIGHT', lambda t: 'Filter Speed4' in t,
                                 8, 'Filter Speed4')

    def read_vsp_slot4(self) -> dict:
        """Read Filter Speed4 (slot 4) without changing it."""
        try:
            with _nav_lock:
                txt = self._goto_vsp_slot4()
                pct = self._pct(txt)
                if pct is None:
                    raise RuntimeError(f'Cannot parse Filter Speed4: {txt!r}')
                with state_lock:
                    state.vsp_slot4_pct = pct
                return {'slot': 4, 'speed_pct': pct}
        finally:
            self.fast_exit()

    def set_vsp_slot4(self, target_pct: int) -> dict:
        """
        Write Filter Speed4.  Snaps to 5% grid (verified step size, §12.4).
        Hard scope limit: never writes slots 1-3 or Spa Speed (§6.1 / §8).
        """
        target_pct = int(_clamp(round(target_pct / 5) * 5, 0, 100))
        try:
            with _nav_lock:
                self._goto_vsp_slot4()
                self._step_to(self._pct, target_pct,
                              'PLUS', 'MINUS', self._STEP_MAX, 'Filter Speed4')
                with state_lock:
                    state.vsp_slot4_pct = target_pct
                return {'slot': 4, 'target_pct': target_pct, 'result': self.text()}
        finally:
            self.fast_exit()


    def activate_vsp_slot4(self) -> dict:
        """
        Make slot 4 the running VSP slot by cycling FILTER off→on to open the
        slot-selection window (§6.2), then using +/- to reach slot 4 (§6.4).

        Gate contract (§6.4 / §11): every +/- press verifies 'Filter On:' is
        still in l1 before proceeding; if the window closed early an error is
        raised without further action.

        Does NOT navigate the Settings menu; holds _nav_lock the full time to
        prevent any concurrent keypad use during the activation window.
        """
        _SLOT_MAX_STEPS = 8   # 5 slots × up to 1 wrap = 5; 8 is generous
        with _nav_lock:
            # Ensure filter is off first so the next FILTER press turns it on.
            with state_lock:
                filter_on = state.circuits.get('FILTER', False)
            if filter_on:
                txt = self._send('FILTER')   # turn off
                if 'Filter On:' in txt:
                    raise RuntimeError(f'FILTER off did not clear window: {txt!r}')

            # Turn filter on → opens slot-selection window
            txt = self._send('FILTER')
            if 'Filter On:' not in txt:
                raise RuntimeError(
                    f'Expected slot-selection window after FILTER on, got: {txt!r}')

            # Cycle +/- until we see Spd4. The window can close on its own, so
            # this is gated rather than retried: each PLUS must keep us in it.
            for step in range(_SLOT_MAX_STEPS):
                if 'Spd4' in txt:
                    break
                txt = self._send('PLUS')
                if 'Filter On:' not in txt:
                    raise RuntimeError(
                        f'Slot-selection window closed early at step {step}: {txt!r}')
            else:
                raise RuntimeError('Could not find slot 4 in slot-selection window')

            with state_lock:
                state.circuits['FILTER'] = True
            return {'activated_slot': 4, 'frame': txt}


def _get_panel():
    with panel_lock:
        return panel


def _get_navigator() -> Optional[MenuNavigator]:
    p = _get_panel()
    return MenuNavigator(p, lcd) if p is not None else None


# ---------------------------------------------------------------------------
# Flask REST API
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.logger.setLevel(logging.WARNING)


@app.route('/status')
def get_status() -> Response:
    with state_lock:
        return jsonify({
            'circuits':            dict(state.circuits),
            'pool_temp':           state.pool_temp,
            'air_temp':            state.air_temp,
            'spa_temp':            state.spa_temp,
            'salt_level':          state.salt_level,
            'chlorinator_percent': state.chlorinator_percent,
            'pump_speed':          state.pump_speed,
            # heater setpoints are read via menu navigation, cached here after reads
            'pool_setpoint_f':     state.pool_setpoint_f,
            'spa_setpoint_f':      state.spa_setpoint_f,
            'pool_heater_enabled': state.pool_heater_enabled,
            'spa_heater_enabled':  state.spa_heater_enabled,
            'valve_mode':          state.valve_mode,
            'vsp_slot4_pct':       state.vsp_slot4_pct,
            'connected':           state.connected,
            'last_update':         state.last_update,
        })


@app.route('/display')
def get_display() -> Response:
    l1, l2 = lcd.lines()
    return jsonify({'line1': l1, 'line2': l2})


@app.route('/display/history')
def get_display_history() -> Response:
    entries = [{'ts': ts, 'text': t} for ts, t in lcd.snapshot()]
    return jsonify({'history': entries})


@app.route('/mode', methods=['POST'])
def set_mode() -> Response:
    """
    Set pool/spa valve mode.  Body: {"mode": "pool"|"spa"}

    For pool+spa-only systems this is a single cycle-key press whenever the
    current mode differs from the target.  Optimistically updates valve_mode
    so the next /status poll reflects the change immediately.
    """
    body = request.get_json(force=True)
    target = body.get('mode', '').lower()
    if target not in ('pool', 'spa'):
        return jsonify({'error': 'mode must be "pool" or "spa"'}), 400
    p = _get_panel()
    if p is None:
        return jsonify({'error': 'Not connected'}), 503
    with state_lock:
        current = state.valve_mode
    if current == target:
        return jsonify({'ok': True, 'mode': target, 'changed': False})
    try:
        # Send the POOL/SPA cycle key via the existing circuit path.
        # For pool+spa-only systems one press always toggles.
        p.set_circuit('POOL' if target == 'pool' else 'SPA', True)
        with state_lock:
            state.valve_mode = target
        log.info(f'Mode -> {target}')
        return jsonify({'ok': True, 'mode': target, 'changed': True})
    except Exception as e:
        log.error(f'set_mode {target}: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/circuit/<name>', methods=['POST'])
def set_circuit(name: str) -> Response:
    body = request.get_json(force=True)
    on: bool = bool(body.get('on', False))
    key = name.upper()
    if key not in CIRCUIT_NAMES:
        return jsonify({'error': f'Unknown circuit: {name}'}), 400
    p = _get_panel()
    if p is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        ok = p.set_circuit(key, on)
        if not ok:
            # Press was sent but the panel state never confirmed the change.
            return jsonify({'error': f'{key} toggle not confirmed by panel'}), 502
        log.info(f'Circuit {key} -> {"ON" if on else "OFF"}')
        return jsonify({'ok': True})
    except UnsupportedCircuit:
        return jsonify({'error': f'{key} cannot be toggled (no keypad key)'}), 422
    except Exception as e:
        log.error(f'set_circuit {key}: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/debug/nav-trace')
def debug_nav_trace() -> Response:
    """Replay the most recent keypress trace, newest last.

    Query: ?n=N limits to the last N entries (default 60). Each entry shows the
    key, the normalized LCD frame before/after, whether it changed, and how long
    _send waited. Use this to validate a walk against the spec ring (§3/§4)
    instead of inferring from a one-line error.
    """
    try:
        n = int(request.args.get('n', 60))
    except (TypeError, ValueError):
        n = 60
    with _NAV_TRACE_LOCK:
        items = list(_NAV_TRACE)
    if n > 0:
        items = items[-n:]
    return jsonify({'count': len(items), 'trace': items})


@app.route('/debug/nav-trace/clear', methods=['POST'])
def debug_nav_trace_clear() -> Response:
    with _NAV_TRACE_LOCK:
        _NAV_TRACE.clear()
    return jsonify({'ok': True})


@app.route('/debug/rawkey', methods=['POST'])
def debug_rawkey() -> Response:
    """Send one key under a chosen RS-485 frame type, bypassing send_key.

    Body: {"key": "RIGHT", "frametype": "remote"}
      frametype: "local"  -> 00 02 LOCAL_WIRED_KEY_EVENT (what send_key uses)
                 "remote" -> 00 03 REMOTE_WIRED_KEY_EVENT (what a wired remote
                              like the AquaConnect box emits)
    Builds the frame exactly like aqualogic._get_key_event_frame but lets us
    pick the frame type, so we can prove whether menu-scroll keys need the
    remote event type. Returns the hex frame queued.
    """
    p = _get_panel()
    if p is None:
        return jsonify({'error': 'Not connected'}), 503
    body = request.get_json(force=True) or {}
    name = body.get('key', '')
    ftype = body.get('frametype', 'remote').lower()
    try:
        aq = p._aq
        Keys = p._Keys
        k = getattr(Keys, name, None)
        if k is None:
            return jsonify({'error': f'Unknown key {name!r}'}), 400
        type_bytes = aq.FRAME_TYPE_REMOTE_WIRED_KEY_EVENT if ftype == 'remote' \
            else aq.FRAME_TYPE_LOCAL_WIRED_KEY_EVENT
        frame = bytearray()
        frame.append(aq.FRAME_DLE)
        frame.append(aq.FRAME_STX)
        aq._append_data(frame, type_bytes)
        aq._append_data(frame, int(k.value).to_bytes(2, byteorder='little'))
        aq._append_data(frame, int(k.value).to_bytes(2, byteorder='little'))
        crc = sum(frame)
        aq._append_data(frame, crc.to_bytes(2, byteorder='big'))
        frame.append(aq.FRAME_DLE)
        frame.append(aq.FRAME_ETX)
        import binascii
        hexf = binascii.hexlify(bytes(frame)).decode()
        aq._send_queue.put({'frame': frame})
        log.info('rawkey %s type=%s queued: %s', name, ftype, hexf)
        return jsonify({'ok': True, 'key': name, 'frametype': ftype, 'frame': hexf})
    except Exception as e:
        log.error(f'rawkey: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/debug/keyburst', methods=['GET', 'POST'])
def debug_keyburst() -> Response:
    """Live-tune the key-burst timing without a restart (diagnostics).

    POST JSON any of: burst (int), predelay_ms (float), gap_ms (float).
    The _send_frame_burst closure reads these globals on each send, so changes
    take effect on the next keypress.
    """
    global KEY_BURST, KEY_PREDELAY_MS, KEY_GAP_MS, KEY_MAX_RETRIES, KEY_VERIFY_DELAY_S, KEY_PAD_BYTES
    if request.method == 'POST':
        body = request.get_json(force=True) or {}
        if 'burst' in body:
            KEY_BURST = max(1, int(body['burst']))
        if 'predelay_ms' in body:
            KEY_PREDELAY_MS = float(body['predelay_ms'])
        if 'gap_ms' in body:
            KEY_GAP_MS = float(body['gap_ms'])
        if 'max_retries' in body:
            KEY_MAX_RETRIES = max(1, int(body['max_retries']))
        if 'verify_delay_s' in body:
            KEY_VERIFY_DELAY_S = float(body['verify_delay_s'])
        if 'pad_bytes' in body:
            KEY_PAD_BYTES = max(0, int(body['pad_bytes']))
        log.info('Key-burst retuned: burst=%d predelay=%.0fms gap=%.0fms '
                 'retries=%d verify=%.1fs pad=%d', KEY_BURST, KEY_PREDELAY_MS,
                 KEY_GAP_MS, KEY_MAX_RETRIES, KEY_VERIFY_DELAY_S, KEY_PAD_BYTES)
    return jsonify({'burst': KEY_BURST,
                    'predelay_ms': KEY_PREDELAY_MS,
                    'gap_ms': KEY_GAP_MS,
                    'max_retries': KEY_MAX_RETRIES,
                    'verify_delay_s': KEY_VERIFY_DELAY_S,
                    'pad_bytes': KEY_PAD_BYTES})


@app.route('/keypad/<key>', methods=['POST'])
def keypad_press(key: str) -> Response:
    """
    Send a single raw keypad key.  Intended for diagnostics and manual
    navigation; do not use this from HomeKit automation paths (use the
    higher-level navigator endpoints instead).
    """
    key = key.upper()
    p = _get_panel()
    if p is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        p.send_key(key)
        l1, l2 = lcd.lines()
        return jsonify({'ok': True, 'line1': l1, 'line2': l2})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        log.error(f'keypad {key}: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/heater/<which>/state')
def get_heater_state(which: str) -> Response:
    """Read a heater setpoint via menu navigation (non-mutating)."""
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        result = nav.read_heater(which)
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        log.error(f'read_heater {which}: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/heater/<which>/enable', methods=['POST'])
def set_heater_enable(which: str) -> Response:
    """
    Enable/disable a heater (Auto vs Manual Off) via menu navigation.
    Body: {"on": true|false}
    """
    body = request.get_json(force=True)
    on = bool(body.get('on', False))
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        result = nav.set_heater_enabled(which, on)
        log.info(f'Heater {which} enable -> {on} (was_off={result["was_off"]})')
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        log.error(f'set_heater_enabled {which}: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/heater/<which>/setpoint', methods=['POST'])
def set_heater_setpoint(which: str) -> Response:
    """
    Set a heater temperature setpoint via menu navigation.
    Body: {"temp_f": 88}
    Handles the forced-off enable/restore cycle automatically (§13.3).
    """
    body = request.get_json(force=True)
    temp_f = body.get('temp_f')
    if temp_f is None:
        return jsonify({'error': 'temp_f is required'}), 400
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        result = nav.set_heater(which, int(temp_f))
        log.info(f'Heater {which} setpoint -> {temp_f}°F (was_off={result["was_off"]})')
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        log.error(f'set_heater {which}: {e}')
        return jsonify({'error': str(e)}), 500


# Legacy alias kept for backwards compat — routes to pool heater
@app.route('/heater/setpoint', methods=['POST'])
def set_heater_setpoint_legacy() -> Response:
    body = request.get_json(force=True)
    temp_f = body.get('temp_f')
    which = body.get('which', 'pool')
    if temp_f is None:
        return jsonify({'error': 'temp_f and which ("pool"|"spa") are required'}), 400
    return set_heater_setpoint(which)


@app.route('/vsp/slot4')
def get_vsp_slot4() -> Response:
    """Read Filter Speed4 via menu navigation."""
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        return jsonify(nav.read_vsp_slot4())
    except Exception as e:
        log.error(f'read_vsp_slot4: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/vsp/slot4', methods=['POST'])
def set_vsp_slot4() -> Response:
    """
    Set Filter Speed4 (slot 4 only).  Body: {"speed_pct": 75}
    Speed is snapped to 5% grid.  Slots 1-3 and Spa Speed are never written.
    """
    body = request.get_json(force=True)
    pct = body.get('speed_pct')
    if pct is None:
        return jsonify({'error': 'speed_pct is required'}), 400
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        result = nav.set_vsp_slot4(int(pct))
        log.info(f'VSP slot4 -> {result["target_pct"]}%')
        return jsonify(result)
    except Exception as e:
        log.error(f'set_vsp_slot4: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/vsp/slot4/activate', methods=['POST'])
def activate_vsp_slot4() -> Response:
    """
    Activate slot 4 as the running VSP slot by cycling FILTER off→on (§6.2).
    No body required.  The filter is left ON after the call.
    Combine with POST /vsp/slot4 to set the speed value first, then activate.
    """
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        result = nav.activate_vsp_slot4()
        log.info(f'VSP slot4 activated (filter on)')
        return jsonify(result)
    except Exception as e:
        log.error(f'activate_vsp_slot4: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/chlorinator/<which>', methods=['POST'])
def set_chlorinator_which(which: str) -> Response:
    """Set pool or spa chlorinator output % via menu navigation. Body: {"percent": 50}"""
    body = request.get_json(force=True)
    pct = body.get('percent')
    if pct is None:
        return jsonify({'error': 'percent is required'}), 400
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        result = nav.set_chlorinator(which, int(pct))
        log.info(f'Chlorinator {which} -> {pct}%')
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        log.error(f'set_chlorinator {which}: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/chlorinator', methods=['POST'])
def set_chlorinator_legacy() -> Response:
    """Legacy: routes to pool chlorinator. Prefer /chlorinator/pool."""
    body = request.get_json(force=True)
    pct = body.get('percent')
    which = body.get('which', 'pool')
    if pct is None:
        return jsonify({'error': 'percent and optionally which ("pool"|"spa") are required'}), 400
    return set_chlorinator_which(which)


@app.route('/superchlorinate', methods=['POST'])
def set_super_chlorinate() -> Response:
    body = request.get_json(force=True)
    on: bool = bool(body.get('on', False))
    p = _get_panel()
    if p is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        ok = p.set_circuit('SUPER_CHLORINATE', on)
        if not ok:
            return jsonify({'error': 'Super-chlorinate cannot be toggled (no keypad key)'}), 422
        log.info(f'Super-chlorinate -> {"ON" if on else "OFF"}')
        return jsonify({'ok': True})
    except Exception as e:
        log.error(f'set_super_chlorinate: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health() -> Response:
    with state_lock:
        connected = state.connected
        age = time.time() - state.last_update if state.last_update else None
    return jsonify({'connected': connected, 'data_age_seconds': age}), (200 if connected else 503)


# ---------------------------------------------------------------------------
# Background heater refresher
#
# Heater setpoints and enable-state can only be read by navigating the Settings
# menu, so they cannot ride the passive /status poll. This thread does one read
# shortly after the bus connects (so HomeKit shows real values on startup),
# then refreshes on a slow interval to catch changes made at the panel.
# Valve mode is updated passively in on_change and needs no navigation.
# ---------------------------------------------------------------------------

def refresher_thread(interval: float) -> None:
    did_initial = False
    while True:
        with state_lock:
            connected = state.connected
        nav = _get_navigator()
        if nav is not None and connected:
            for which in ('pool', 'spa'):
                try:
                    nav.read_heater(which)
                except Exception as e:
                    log.warning(f'heater refresh ({which}) failed: {e}')
            if not did_initial:
                log.info('Initial heater state read complete.')
                did_initial = True
        # Retry quickly until the first successful read, then settle to interval.
        time.sleep(interval if did_initial else 10)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global KEY_BURST, KEY_PREDELAY_MS, KEY_GAP_MS
    parser = argparse.ArgumentParser(description='AquaPlus/ProLogic RS-485 sidecar')
    parser.add_argument('--host', help='WiFi serial bridge IP (required unless --simulate)')
    parser.add_argument('--port', type=int, default=8899)
    parser.add_argument('--api-port', type=int, default=5757)
    parser.add_argument('--api-host', default='127.0.0.1')
    parser.add_argument('--simulate', action='store_true',
                        help='Run full menu simulation without any hardware')
    parser.add_argument('--heater-refresh', type=float, default=600.0,
                        help='Seconds between background heater-state reads '
                             '(menu navigation). 0 disables. Default 600.')
    parser.add_argument('--key-burst', type=int, default=KEY_BURST,
                        help='Times to write each key frame per send. 1 = single '
                             'shot (correct for the W610; >1 is harmful — it '
                             'merges rapid writes). Default 1.')
    parser.add_argument('--key-predelay-ms', type=float, default=KEY_PREDELAY_MS,
                        help='Delay after keep-alive before writing the frame '
                             '(ms). Targets the panel accept window. Default 70.')
    parser.add_argument('--key-gap-ms', type=float, default=KEY_GAP_MS,
                        help='Gap between writes when --key-burst > 1 (ms, '
                             'diagnostics only). Default 10.')
    args = parser.parse_args()

    KEY_BURST = args.key_burst
    KEY_PREDELAY_MS = args.key_predelay_ms
    KEY_GAP_MS = args.key_gap_ms

    if args.simulate:
        t = threading.Thread(target=simulate_thread, daemon=True, name='simulate')
    else:
        if not args.host:
            parser.error('--host is required unless --simulate is given')
        t = threading.Thread(target=panel_thread, args=(args.host, args.port), daemon=True, name='aqualogic')
    t.start()

    if args.heater_refresh > 0:
        threading.Thread(target=refresher_thread, args=(args.heater_refresh,),
                         daemon=True, name='refresher').start()

    log.info('REST API listening on %s:%s (key-burst=%d)',
             args.api_host, args.api_port, KEY_BURST)
    app.run(host=args.api_host, port=args.api_port, threaded=True)


if __name__ == '__main__':
    main()
