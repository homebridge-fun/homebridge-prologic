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
import json
import logging
import logging.handlers
import os
import random
import re
import socket
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
# Rotating debug log — /tmp/pool_sidecar_debug.log, replaced every hour.
# Keeps 1 backup so the previous hour is still readable while the new one grows.
# ---------------------------------------------------------------------------
_DEBUG_LOG_PATH = '/tmp/pool_sidecar_debug.log'
_debug_file_handler = logging.handlers.TimedRotatingFileHandler(
    _DEBUG_LOG_PATH,
    when='h',         # rotate every hour
    interval=1,
    backupCount=1,    # keep one previous hour
    encoding='utf-8',
)
_debug_file_handler.setLevel(logging.DEBUG)
_debug_file_handler.setFormatter(
    logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
)
log.addHandler(_debug_file_handler)
log.setLevel(logging.DEBUG)   # file gets DEBUG; console handler keeps INFO via basicConfig root level


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
    vsp_slot_pct: dict = field(default_factory=dict)  # {1: pct, 2: pct, 3: pct, 4: pct}
    vsp_active_slot: Optional[int] = None  # 1-4; set on activate, None = unknown
    connected: bool = False
    last_update: float = 0.0
    # True when the AquaConnect box has entered read-only mode (commands ACKed
    # but silently dropped at the RS-485 relay). Cleared by any confirmed write.
    bridge_wedged: bool = False

state = PoolState()
state_lock = threading.Lock()

# ---------------------------------------------------------------------------
# AquaConnect command-path health tracking (§bridge-health-spec)
#
# The box can enter a silent failure mode: POSTs return 200, reads stay live,
# but keypresses are never relayed to the panel. A power-cycle of the box
# clears it. We detect it passively (confirmed-write failures) and actively
# (periodic canary probe on AUX2, which is unused/inert on this system).
#
# Debounce: require _WEDGE_FAIL_THRESHOLD consecutive failures before flagging;
# clear immediately on any confirmed success.
_WEDGE_FAIL_THRESHOLD = 2
_wedge_fail_streak: int = 0
_wedge_lock = threading.Lock()


# Key code for the canary output (AUX2 = 0B; confirmed inert on this system).
_WEDGE_CANARY_KEY = 'AUX2'
# How often (seconds) to run the active canary probe while healthy.
_WEDGE_PROBE_INTERVAL_S = 300.0
# Faster probe cadence while wedged, so recovery after a power-cycle shows up
# quickly (the box stays wedged until power-cycled, so frequent probing is safe).
_WEDGE_RECOVERY_INTERVAL_S = 30.0


def _record_command_success() -> None:
    """Call after any confirmed write. Clears the wedge flag immediately."""
    global _wedge_fail_streak
    with _wedge_lock:
        _wedge_fail_streak = 0
        changed = state.bridge_wedged
    if changed:
        with state_lock:
            state.bridge_wedged = False
        log.info('Bridge command path recovered — clearing wedge flag')


def _record_command_failure() -> None:
    """Call when a command was sent but produced no confirmed state change."""
    global _wedge_fail_streak
    with _wedge_lock:
        _wedge_fail_streak += 1
        streak = _wedge_fail_streak
        already = state.bridge_wedged
    if streak >= _WEDGE_FAIL_THRESHOLD and not already:
        with state_lock:
            state.bridge_wedged = True
        log.warning(
            'Bridge command path appears wedged (%d consecutive unconfirmed writes). '
            'Power-cycle the AquaConnect box to recover.',
            streak)

def _immediate_wedge_probe() -> None:
    """Spawn a background daemon thread to probe wedge state right now.

    Called after any HomeKit-driven write fails so the bridge_wedged flag
    updates within seconds rather than waiting for the 300s/30s probe loop.
    """
    threading.Thread(target=_ac_canary_probe, daemon=True,
                     name='wedge-probe-on-failure').start()


# ---------------------------------------------------------------------------
# Backend selection persistence (§selectable-backend)
#
# The active navigation backend (aquaconnect | rs485) is chosen at startup from
# this config file if present, else from CLI args. POST /backend rewrites the
# file and exits the process so systemd restarts into the new backend. This
# lets the Homebridge plugin switch backends without sudo or relaunching the
# service directly. The file lives next to the script (homebridge-owned).
_BACKEND_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'backend.json')
# Which backend is live in this process (set in main()).
_active_backend: Optional[str] = None


def _load_backend_config() -> dict:
    """Read the persisted backend selection, or {} if none/unreadable."""
    try:
        with open(_BACKEND_CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning('Could not read backend config %s: %s', _BACKEND_CONFIG_PATH, e)
        return {}


def _save_backend_config(cfg: dict) -> None:
    """Persist the backend selection for the next startup."""
    with open(_BACKEND_CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)


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
        '                    # LONG frame: variable-length header + 40 LCD bytes\n'
        '                    # (20-char line 1 + 20-char line 2) + 0x00 null.\n'
        '                    # Short frames (len<41) are cursor/blink control\n'
        '                    # packets, not text updates — skip them or they\n'
        '                    # decode as garbage (e.g. "ju %").\n'
        '                    if len(frame) >= 41:\n'
        '                        lcd = frame[-41:-1]  # drop header + null\n'
        '                        raw = bytes(b if b == 0xdf else (b & 0x7f) for b in lcd)\n'
        '                        text = raw.replace(b\'\\xdf\', b\'\\xc2\\xb0\').decode(\'utf-8\', errors=\'replace\')\n'
        '                        self._web.text_updated(text)'
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
class _PriorityLock:
    """A mutex that tracks how many threads are blocked waiting on it.

    Real actions (circuit/heater/setpoint writes) acquire it normally. Low-
    priority background work — the canary probe and the KeyId=00 read poll —
    checks `waiters`/`locked()` first and defers when a real action is queued,
    so background traffic never stomps on user commands. Supports the same
    `with`/`.locked()` surface as threading.Lock so existing call sites are
    unchanged.
    """
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters = 0
        self._wlock = threading.Lock()

    def acquire(self) -> bool:
        with self._wlock:
            self._waiters += 1
        try:
            return self._lock.acquire()
        finally:
            with self._wlock:
                self._waiters -= 1

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    @property
    def waiters(self) -> int:
        with self._wlock:
            return self._waiters

    def busy(self) -> bool:
        """True if held or if any thread is waiting to acquire it."""
        return self._lock.locked() or self.waiters > 0

    def __enter__(self) -> '_PriorityLock':
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


_nav_lock = _PriorityLock()


# ---------------------------------------------------------------------------
# AquaConnect HTTP backend (§2 key codes; protocol verified against the
# SteveTheGeekHA/AquaConnectDeviceHandler reference implementation)
#
# Sends keypad presses via POST /WNewSt.htm  body "KeyId=NN&".
# Status reads use body "Update Local Server&" — the native UI's refresh body
# (WebsFuncs.js:ReqWebsData). Unlike "KeyId=00&", this never touches
# WebsProcessKey() so it carries no keypad-event side-effect.
# Feeds the standard LcdCapture so the MenuNavigator sees no difference.
#
# Response format (WNewSt.htm):
#   HTML page; the interesting content is between <body> and </body>.
#   Tokenized on '\n', the FIRST line is a header/cursor line and is dropped;
#   the next three lines are:
#     line[0]  LCD display line 1   (e.g. "Pool Chlorinator")
#     line[1]  LCD display line 2   (e.g. "35%")  — the value field
#     line[2]  LED/equipment state  (6 ASCII chars; see _decode_ac_led)
#   The degree symbol arrives HTML-encoded as "&#176".
# ---------------------------------------------------------------------------

# AquaConnect web key codes (§2)
_AC_KEY_CODES = {
    'RIGHT': '01',
    'MENU':  '02',
    'LEFT':  '03',
    'MINUS': '05',
    'PLUS':  '06',
    'POOL':  '07',
    'SPA':   '07',
    'SPILLOVER': '07',
    'FILTER': '08',
    'LIGHTS': '09',
    'AUX1':  '0A',
    'AUX2':  '0B',
    'VALVE3': '11',
    'HEATER1': '13',
}

# Minimum gap (seconds) between ANY two requests to the box — reads included.
# Characterization (2026-06-19): bare press threshold ~0.5s; read-then-press
# threshold ~0.9s (reads count as events). 0.9s sits reliably above the edge.
_AC_MIN_GAP_S = 0.9

# How long to wait after a key before reading back the settled screen. The box
# mirrors the panel over the slow RS-485 bus, so the immediate response can
# still show the pre-keypress screen. 1.0s is well above the ~230ms read RTT.
_AC_SETTLE_S = 1.0

# After a toggle the panel briefly flashes the new state before reverting to the
# idle scroll. Sample a few frames across that window so we catch the flash
# whichever moment it lands on, rather than waiting for the passive cycle. The
# total span (READS × GAP) covers ~1.6s, comfortably wider than a typical flash.
_AC_CONFIRM_READS = 4
_AC_CONFIRM_GAP_S = 0.4

# LED nibble decode (each equipment LED is one 4-bit nibble in the state line).
#   3 = absent / no key on this panel
#   4 = off
#   5 = on
#   6 = blink (transitioning / attention)
_AC_LED_MAP = {0x3: 'absent', 0x4: 'off', 0x5: 'on', 0x6: 'blink'}
# Valid LED-state characters: a byte whose BOTH nibbles are in {3,4,5,6}.
# The line is >= 6 chars (this firmware sends 12: 6 populated + 6 absent slots).
_AC_LED_RE = re.compile(r'^[3-6CDEFcdefSTUVstuv]{6,}$')


def _ac_led_nibbles(c: str) -> Tuple[Optional[str], Optional[str]]:
    """Decode one LED-state character into (first-LED, second-LED) states.

    The character's byte value carries two nibbles; the high nibble is the
    'first' LED at that position, the low nibble the 'second'.
    """
    b = ord(c)
    return _AC_LED_MAP.get((b >> 4) & 0xF), _AC_LED_MAP.get(b & 0xF)


def _decode_ac_led(line3: str) -> dict:
    """Decode the 6-char LED/equipment-state line into named states (§13.2).

    Layout (from the reference handler):
      char[0]: first=Pool mode,  second=Spa mode
      char[1]: first=Spillover,  second=Filter
      char[2]: first=Lights
      char[3]: first=Heater
      char[4]: second=Aux1
      char[5]: first=Aux2
    Each value is one of 'absent' | 'off' | 'on' | 'blink' (or None if unknown).
    """
    out: dict = {}
    if not line3 or len(line3) < 6:
        return out
    pool_m, spa_m = _ac_led_nibbles(line3[0])
    spill_m, filt = _ac_led_nibbles(line3[1])
    lights, _ = _ac_led_nibbles(line3[2])
    heater, _ = _ac_led_nibbles(line3[3])
    _, aux1 = _ac_led_nibbles(line3[4])
    aux2, _ = _ac_led_nibbles(line3[5])
    out['pool_mode'] = pool_m
    out['spa_mode'] = spa_m
    out['spillover_mode'] = spill_m
    out['filter'] = filt
    out['lights'] = lights
    out['heater'] = heater
    out['aux1'] = aux1
    out['aux2'] = aux2
    return out


class AquaConnectBackend:
    """HTTP backend that drives menu navigation through the AquaConnect box.

    Provides the same LCD + key-send surface as the RS-485 path but over
    POST /WNewSt.htm instead of raw RS-485 frames. The `lcd` attribute is an
    LcdCapture that the MenuNavigator reads exactly as it reads the RS-485 one.

    All HTTP is serialized through `_http_lock` so the background poller never
    overlaps a key-send (two concurrent POSTs confuse the box).
    """

    def __init__(self, host: str = '192.168.50.100', poll_s: float = 3.0):
        self._host = host
        self.lcd = LcdCapture()
        self._http_lock = threading.Lock()   # serializes press+settle+read units
        self._last_req = 0.0                  # ts of last request (gap enforcement)
        self._last_raw: Optional[str] = None  # last full body, for /debug calibration
        self._last_led: dict = {}
        self._poll_s = poll_s
        self._poll_stop = threading.Event()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name='ac-poll')
        self._poll_thread.start()

    # ── HTTP ────────────────────────────────────────────────────────────────
    def _post(self, key_code: str) -> Optional[str]:
        """Send 'KeyId=NN&' and return the response body.

        Two hard-won, live-verified constraints:

        - Transport: a byte-identical curl request over a RAW SOCKET. urllib's
          extra headers (Accept-Encoding: identity, Connection, Python UA) make
          the GoAhead 'Webs' server silently ignore the key — 0/20 landed —
          while curl's lean header set works every time. So we hand-build the
          exact request curl sends.
        - Body: exactly "KeyId=NN&", verbatim from the panel's WebsProcessKey()
          (WebsFuncs.js:690). The firmware scans "KeyId=" up to the trailing
          '&'; without it the key is dropped.
        - Timing: the panel ignores any key within ~0.5-1s of the previous
          keypress event. We enforce _AC_MIN_GAP_S before EVERY request here.
          Callers hold _http_lock, so _last_req is single-threaded.

        NOTE: use _read() for status reads. _post('00') goes through the
        firmware's WebsProcessKey() handler and counts as a keypad event.
        """
        return self._request(f'KeyId={key_code}&')

    def _read(self) -> Optional[str]:
        """Fetch the current screen state WITHOUT injecting a keypad event.

        The native web UI uses 'Update Local Server&' (not 'KeyId=00&') for
        its 300ms screen-refresh loop (WebsFuncs.js:ReqWebsData). The firmware
        routes these two body strings to different handlers: 'Update Local
        Server&' never touches WebsProcessKey(), so it is a pure read with no
        side-effects on the keypad event queue. Using 'KeyId=00&' for reads
        injects ~29,000 phantom keypad events/day and wedges the box.

        Use this everywhere we only want the current state (poll, confirm burst).
        Use _post(code) only when we actually intend a keypress.
        """
        return self._request('Update Local Server&')

    def _request(self, body: str) -> Optional[str]:
        """Send a POST /WNewSt.htm with the given body and return the response."""
        now = time.time()
        elapsed = now - self._last_req
        wait = _AC_MIN_GAP_S - elapsed
        if wait > 0:
            time.sleep(wait)
        t_send = time.time()
        req = (f'POST /WNewSt.htm HTTP/1.1\r\n'
               f'Host: {self._host}\r\n'
               f'User-Agent: curl/7.88.1\r\n'
               f'Accept: */*\r\n'
               f'Content-Type: application/x-www-form-urlencoded\r\n'
               f'Content-Length: {len(body)}\r\n\r\n{body}')
        try:
            s = socket.create_connection((self._host, 80), timeout=5)
            try:
                s.sendall(req.encode('latin-1'))
                s.settimeout(3)
                buf = b''
                while b'\r\n\r\n' not in buf:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                head, _, rest = buf.partition(b'\r\n\r\n')
                m = re.search(rb'Content-Length:\s*(\d+)', head, re.I)
                if m:
                    need = int(m.group(1))
                    while len(rest) < need:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        rest += chunk
                else:
                    try:
                        while True:
                            chunk = s.recv(4096)
                            if not chunk:
                                break
                            rest += chunk
                    except socket.timeout:
                        pass
                full = (head + b'\r\n\r\n' + rest).decode('latin-1', errors='replace')
                status_line = head.decode('latin-1', errors='replace').split('\r\n')[0]
                led_line = 'none'
                for ln in self._body_lines(full):
                    if _AC_LED_RE.match(ln):
                        led_line = ln
                        break
                gap_actual = t_send - self._last_req if self._last_req else 0.0
                rtt = time.time() - t_send
                log.debug(
                    'AC POST body=%r gap=%.3fs rtt=%.3fs thread=%s http=%s led=%s',
                    body[:30], gap_actual, rtt,
                    threading.current_thread().name,
                    status_line.split(' ', 2)[1] if ' ' in status_line else status_line,
                    led_line,
                )
                return full
            finally:
                s.close()
        except Exception as e:
            log.warning('AquaConnect socket error body=%r gap=%.3fs thread=%s: %s',
                        body[:30],
                        t_send - self._last_req if self._last_req else 0.0,
                        threading.current_thread().name,
                        e)
            return None
        finally:
            self._last_req = time.time()

    # ── Parsing ───────────────────────────────────────────────────────────────
    @staticmethod
    def _body_lines(body: str):
        """Return the meaningful lines inside <body>…</body>.

        Verified live (firmware WebsR2-1.x): lines are CRLF-separated and each
        is terminated by a literal 'xxx' marker which is stripped. Empty lines
        are dropped. No leading line is dropped — on this firmware the first
        line is real LCD content (e.g. the weekday on the idle clock screen).
        Example body lines: ['Thursday', '5:47P', 'TECD4C333333'].
        """
        if not body:
            return []
        start = body.find('<body>')
        end = body.find('</body>')
        inner = body[start + 6:end] if (start != -1 and end != -1) else body
        out = []
        for ln in inner.replace('\r', '').split('\n'):
            ln = ln.strip()
            if ln.endswith('xxx'):          # firmware line terminator
                ln = ln[:-3].strip()
            if ln:
                out.append(ln)
        return out

    def _parse(self, body: str):
        """Parse a response body into (lcd_text, led_dict).

        lcd_text is the LCD lines combined (matching the RS-485 40-char frame),
        with the &#176 degree entity rendered as '°'. led_dict is the decoded
        equipment state, or {} if the LED line is absent/unrecognised.

        The LED-state line is found by PATTERN, not position: when LCD line 2
        is blank (common on the idle temp screen) the empty-line filter shifts
        it up, so a positional read would leak the LED bytes into the LCD text.
        """
        lines = self._body_lines(body)
        if not lines:
            return None, {}
        import html
        # Pull out the LED-state line (6 chars, both nibbles in {3,4,5,6}).
        led, lcd_lines = {}, []
        for ln in lines:
            if not led and _AC_LED_RE.match(ln):
                led = _decode_ac_led(ln)
            else:
                lcd_lines.append(html.unescape(ln.replace('&#176', '°')))
        lcd = ' '.join(lcd_lines[:2]).strip()
        return (lcd or None), led

    def _apply(self, body: str) -> None:
        """Update LCD capture + cached LED state + global PoolState from a body."""
        if not body:
            return
        self._last_raw = body
        lcd, led = self._parse(body)
        if lcd:
            self.lcd.text_updated(lcd)
            _apply_ac_scroll_to_state(lcd)
        if led:
            self._last_led = led
            _apply_ac_led_to_state(led)

    # ── Public navigator surface ──────────────────────────────────────────────
    def send_nav_key(self, key_name: str) -> None:
        """Send one navigation key and block until the box has settled.

        Holds _http_lock across post → settle → reread so the poller cannot
        interleave a second POST. Returns only once self.lcd reflects the
        post-keypress screen, so the navigator's _send sees the change at once.
        """
        # Normalize underscores so navigator names (HEATER_1, AUX_1, …) match
        # the underscore-free table keys (HEATER1, AUX1, …).
        code = _AC_KEY_CODES.get(key_name.upper().replace('_', ''))
        if code is None:
            raise ValueError(f'No AquaConnect code for key: {key_name}')
        with self._http_lock:
            self._apply(self._post(code))
            # The panel flashes a transient confirmation of the new state right
            # after a toggle (e.g. 'Filter ON', 'Heater1 Auto Control') before
            # reverting to the idle scroll. A single read at the settle mark can
            # land before or after that flash, so sample a short burst and apply
            # each frame; whichever one carries the confirmation updates state at
            # once instead of waiting for the passive scroll to come back around.
            for _ in range(_AC_CONFIRM_READS):
                time.sleep(_AC_CONFIRM_GAP_S)
                self._apply(self._read())

    def _led_line(self, body: Optional[str]) -> Optional[str]:
        """Extract the raw field-3 LED/equipment-state line from a body."""
        if not body:
            return None
        for ln in self._body_lines(body):
            if _AC_LED_RE.match(ln):
                return ln
        return None

    def probe_wedge(self, retries: int = 3, gap_s: float = 3.0) -> dict:
        """Active command-path test: press the canary key and check whether the
        canary's OWN equipment bit flips.

        Compares only the AUX2 nibble of the field-3 LED line, NOT the whole
        string: the full string changes on its own during routine operation
        (e.g. a digit cycling), which would falsely read as 'recovered'. Only
        the canary's own bit moving proves the keypress landed. Restores the
        canary on success. Returns {'alive', 'before', 'after', 'attempts'}
        where before/after are the canary bit ('on'|'off'|...).
        """
        code = _AC_KEY_CODES.get(_WEDGE_CANARY_KEY.replace('_', ''))

        def canary_bit(line: Optional[str]):
            return _decode_ac_led(line).get('aux2') if line else None

        with self._http_lock:
            before = canary_bit(self._led_line(self._post('00')))
            self._apply(self._post(code))   # press canary
            after = before
            attempts = 0
            for _ in range(retries):
                attempts += 1
                time.sleep(gap_s)
                body = self._post('00')
                self._apply(body)
                after = canary_bit(self._led_line(body))
                if after is not None and before is not None and after != before:
                    # Path alive — toggle the canary back to leave it unchanged.
                    self._apply(self._post(code))
                    return {'alive': True, 'before': before, 'after': after,
                            'attempts': attempts}
        return {'alive': False, 'before': before, 'after': after,
                'attempts': attempts}

    # ── Background state poll ─────────────────────────────────────────────────
    def _poll_loop(self) -> None:
        while not self._poll_stop.is_set():
            # Skip during navigation ops (or when one is queued) so we don't
            # compete for the single GoAhead connection slot mid-sequence.
            if not _nav_lock.busy():
                with self._http_lock:
                    self._apply(self._read())  # pure status read, no keypad event
            self._poll_stop.wait(self._poll_s)

    def stop(self) -> None:
        self._poll_stop.set()


# Idle-scroll screens carry the numeric readings the LED line can't: the panel
# cycles through these one at a time (~every few seconds), so the background
# poll captures them over successive reads. Each pattern pulls one PoolState
# field. Temps may carry a degree entity ('77°F') and trailing NBSP; \d+ skips
# both. These are anchored on the label so heater/menu screens never match.
_AC_SCROLL_PATTERNS = (
    ('pool_temp',           re.compile(r'Pool Temp\s+(-?\d+)', re.I)),
    ('air_temp',            re.compile(r'Air Temp\s+(-?\d+)', re.I)),
    ('spa_temp',            re.compile(r'Spa Temp\s+(-?\d+)', re.I)),
    ('salt_level',          re.compile(r'Salt Level\s+(\d+)', re.I)),
    ('chlorinator_percent', re.compile(r'Pool Chlorinator\s+(\d+)\s*%', re.I)),
    ('pump_speed',          re.compile(r'Filter Speed\s+(\d+)\s*%', re.I)),
    ('vsp_active_slot',     re.compile(r'Filter On:Spd(\d)', re.I)),
)


# Heater enable state appears in the idle scroll ('Heater1 Auto Control' /
# 'Heater1 Manual Off') and in the Settings menu with a Pool/Spa prefix. The
# scroll form has no prefix, so it applies to whichever heater the active valve
# mode selects. (When enabled, the menu shows the setpoint °F instead of 'Auto
# Control', so this only flips the flag on the explicit Auto/Manual screens.)
_AC_HEATER_STATE_RE = re.compile(
    r'(Pool |Spa )?Heater1\s+(Auto Control|Manual Off)', re.I)


def _apply_ac_scroll_to_state(lcd: str) -> None:
    """Pull numeric readings + heater enable out of a scroll/menu LCD screen."""
    with state_lock:
        for field, pat in _AC_SCROLL_PATTERNS:
            m = pat.search(lcd)
            if m:
                setattr(state, field, int(m.group(1)))
                state.last_update = time.time()
        hm = _AC_HEATER_STATE_RE.search(lcd)
        if hm:
            prefix = (hm.group(1) or '').strip().lower()
            which = prefix or state.valve_mode or 'pool'
            enabled = 'auto' in hm.group(2).lower()
            if which == 'spa':
                state.spa_heater_enabled = enabled
            else:
                state.pool_heater_enabled = enabled
            state.last_update = time.time()


def _apply_ac_led_to_state(led: dict) -> None:
    """Fold decoded AquaConnect LED state into the shared PoolState (§13.2)."""
    with state_lock:
        # Active body/valve mode
        if led.get('pool_mode') in ('on', 'blink'):
            state.valve_mode = 'pool'
        elif led.get('spa_mode') in ('on', 'blink'):
            state.valve_mode = 'spa'
        # Equipment on/off → circuits dict (absent stays out of the map)
        for name, key in (('filter', 'FILTER'), ('lights', 'LIGHTS'),
                          ('heater', 'HEATER_1'), ('aux1', 'AUX_1'),
                          ('aux2', 'AUX_2')):
            st = led.get(name)
            if st in ('on', 'off', 'blink'):
                state.circuits[key] = (st != 'off')
        state.connected = True
        state.last_update = time.time()


_ac_backend: Optional['AquaConnectBackend'] = None


# How long the controller-write debouncer waits for quiet before applying a
# value. HomeKit emits a burst of setpoint writes as the slider is dragged;
# each one is a ~15s menu navigation, so we coalesce the burst and apply only
# the final value once the user stops moving the slider.
_WRITE_DEBOUNCE_S = 5.0


class WriteDebouncer:
    """Coalesce rapid writes per key; apply only the latest after a quiet window.

    Each submit(key, target) replaces any pending value for that key and resets
    its quiet timer, so the LAST value submitted always wins. A single worker
    thread applies values once they have been quiet for `quiet_s`, calling
    apply_fn(key, target). Exceptions in apply_fn are logged, never raised.
    """

    def __init__(self, apply_fn, quiet_s: float = _WRITE_DEBOUNCE_S):
        self._apply = apply_fn
        self._quiet = quiet_s
        self._cv = threading.Condition()
        self._pending: dict = {}   # key -> (target, deadline)
        threading.Thread(target=self._run, daemon=True,
                         name='write-debounce').start()

    def submit(self, key, target) -> None:
        with self._cv:
            self._pending[key] = (target, time.time() + self._quiet)
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._pending:
                    self._cv.wait()
                now = time.time()
                ready = {k: v[0] for k, v in self._pending.items()
                         if v[1] <= now}
                if not ready:
                    nxt = min(v[1] for v in self._pending.values())
                    self._cv.wait(timeout=max(0.01, nxt - now))
                    continue
                for k in ready:
                    del self._pending[k]
            # Apply outside the lock so a long navigation doesn't block new
            # submits (which keep coalescing into the next quiet window).
            for key, target in ready.items():
                try:
                    self._apply(key, target)
                except Exception as e:
                    log.error('debounced write %s=%s failed: %s', key, target, e)


_setpoint_debouncer: Optional[WriteDebouncer] = None


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
    _MENU_MAX = 10       # MENU presses to find Settings Menu header
    _NAV_MAX = 30        # RIGHT presses to walk the Settings ring (11 items + margin)
    _STEP_MAX = 90       # +/- presses before aborting a value adjust
    # Settle delay after MENU before first RIGHT: the panel needs ~300ms after
    # showing the Settings Menu header before it will accept RIGHT.
    _POST_MENU_SETTLE_S = 0.35

    def __init__(self, p, l: LcdCapture, backend=None):
        self._panel = p
        self._lcd = l
        # If an AquaConnectBackend is provided, key sends go through it and
        # its own LCD capture is used instead of the RS-485 one.
        self._ac_backend = backend
        if backend is not None:
            self._lcd = backend.lcd

    def text(self) -> str:
        """Current normalized LCD frame."""
        return self._lcd.text()

    def _send_key_remote(self, key: str) -> None:
        """Queue a key via the active backend.

        RS-485 backend: REMOTE_WIRED frame (verified: RIGHT/LEFT/PLUS/MINUS only
        work with REMOTE, not LOCAL frames).
        AquaConnect backend: HTTP POST KeyId=NN (§2 key codes).
        """
        if self._ac_backend is not None:
            self._ac_backend.send_nav_key(key)
            return
        aq = self._panel._aq
        Keys = self._panel._Keys
        k = getattr(Keys, key)
        frame = bytearray()
        frame.append(aq.FRAME_DLE)
        frame.append(aq.FRAME_STX)
        aq._append_data(frame, aq.FRAME_TYPE_REMOTE_WIRED_KEY_EVENT)
        aq._append_data(frame, int(k.value).to_bytes(2, byteorder='little'))
        aq._append_data(frame, int(k.value).to_bytes(2, byteorder='little'))
        crc = sum(frame)
        aq._append_data(frame, crc.to_bytes(2, byteorder='big'))
        frame.append(aq.FRAME_DLE)
        frame.append(aq.FRAME_ETX)
        aq._send_queue.put({'frame': frame})

    def _send(self, key: str, expect_change: bool = True) -> str:
        """Send one key and return the resulting normalized frame.

        Uses REMOTE_WIRED frame type for all navigation keys — confirmed
        empirically: RIGHT/LEFT/PLUS/MINUS are dead with LOCAL frames.

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
        self._send_key_remote(key)
        if not expect_change:
            after = self._lcd.text()
            _trace_key(key, before, after, time.time() - t0, expect_change)
            return after
        deadline = t0 + self._KEY_TIMEOUT
        while time.time() < deadline:
            cur = self._lcd.text()
            # Exit as soon as the display is a genuinely different item.
            # _same_item absorbs value-blank flash and SHORT/LONG oscillation
            # so we don't misfire on those, but we DO exit if we've arrived at
            # a completely new item (e.g. "VSP Speed Settings" after "Pool Heater1").
            if not self._same_item(cur, before):
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

        Handles three oscillation sources:
        1. Value flash: item shows as 'Label Value' and 'Label' alternately.
           One is a prefix of the other.
        2. SHORT vs LONG frame character differences: SHORT frame uses '_' for
           degree symbol and ' ' for ':', LONG frame uses '°' and ':'.
           Canonical alphanumeric-token comparison normalises these away.
        3. Garbled RS-485 noise frames never start with an uppercase letter.
        """
        a, b = (a or '').strip(), (b or '').strip()
        if a == b:
            return True
        # Garbled bus frames never start with uppercase; ignore them.
        if not a or not a[0].isupper():
            return True
        if not b or not b[0].isupper():
            return True
        # Canonical form: only alphanumeric tokens (strips °/_/:/%/spaces).
        # 'Pool Temp 77°F' and 'Pool Temp 77_F' both become ['Pool','Temp','77','F'].
        # 'Thursday 8:02A' and 'Thursday 8 02A' both become ['Thursday','8','02A'].
        canon_a = re.findall(r'[A-Za-z0-9]+', a)
        canon_b = re.findall(r'[A-Za-z0-9]+', b)
        if canon_a == canon_b:
            return True
        # Value-blank flash: label tokens are a prefix of label+value tokens.
        short, lng = (canon_a, canon_b) if len(canon_a) <= len(canon_b) else (canon_b, canon_a)
        return bool(short) and lng[:len(short)] == short

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

        Each landed press should advance one menu position. If _send times out
        (key dropped or value-flash stuck), we re-press. We stop the instant
        the target appears, so this never overshoots a distinct, named target.

        Special case for RIGHT: when stuck on the same item for two consecutive
        presses, send an extra RIGHT — the panel may have the value cursor
        selected (flashing), requiring one RIGHT to dismiss before another to
        advance.
        """
        txt = self._lcd.text()
        if ok(txt):
            return txt
        last_item = txt
        stuck_count = 0
        for _ in range(budget):
            txt = self._send(key)
            if ok(txt):
                return txt
            # Detect stuck: if we land on the same item twice in a row,
            # send one extra press to dismiss any value-cursor selection.
            if key == 'RIGHT' and self._same_item(txt, last_item):
                stuck_count += 1
                if stuck_count >= 2:
                    self._send(key)  # dismissal press; ignore result
                    stuck_count = 0
            else:
                stuck_count = 0
            last_item = txt
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
        """Drive MENU until the normalized frame is exactly 'Settings Menu'.

        After landing on the header, wait _POST_MENU_SETTLE_S before returning:
        the panel needs ~300ms to be ready to accept RIGHT after a MENU press.
        """
        self._press_until('MENU', lambda t: t == self._SETTINGS_HDR,
                          self._MENU_MAX, self._SETTINGS_HDR)
        time.sleep(self._POST_MENU_SETTLE_S)

    # Status-cycle prefixes — any of these means we're back in the default display.
    _STATUS_PREFIXES = ('Thursday', 'Pool Temp', 'Air Temp', 'Pool Chlorinator',
                        'Salt Level', 'Heater1', 'Filter Speed', 'Spa Temp')

    def _is_status(self, norm: str) -> bool:
        return any(norm.startswith(p) for p in self._STATUS_PREFIXES)

    def fast_exit(self) -> None:
        """Return to the Default (status-cycle) display via MENU until 'Default Menu'.

        'Default Menu' IS a real screen on this panel (verified seq 37 in trace).
        After reaching it, one more MENU press enters the status cycle.
        Holds _nav_lock so it does not race the next operation.
        """
        with _nav_lock:
            try:
                self._press_until('MENU', lambda t: t == self._DEFAULT_MENU_HDR,
                                  self._MENU_MAX, self._DEFAULT_MENU_HDR)
                self._send('RIGHT', expect_change=False)  # §13.1: RIGHT once exits to status cycle
            except RuntimeError:
                return  # best-effort; never raise out of a finally cleanup

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

    # ── Super Chlorinate ─────────────────────────────────────────────────────

    def set_super_chlorinate(self, on: bool) -> dict:
        """Toggle Super Chlorinate on/off via Settings menu navigation."""
        target = 'On' if on else 'Off'
        try:
            with _nav_lock:
                self._anchor()
                txt = self._press_until('RIGHT', lambda t: 'Super Chlorinate' in t,
                                        self._NAV_MAX, 'Super Chlorinate')
                current = 'on' in txt.lower().split('super chlorinate')[-1].lower()
                if current != on:
                    self._send('PLUS')   # PLUS toggles On/Off on this item
                with state_lock:
                    state.circuits['SUPER_CHLORINATE'] = on
                return {'ok': True, 'super_chlorinate': on, 'was': current}
        finally:
            self.fast_exit()

    # ── VSP slots 1–4 ────────────────────────────────────────────────────────

    def _goto_vsp_slot(self, slot: int) -> str:
        """Anchor, enter the VSP submenu, and land on Filter Speed{slot}. Returns frame."""
        if slot not in (1, 2, 3, 4):
            raise ValueError(f'VSP slot must be 1–4, got {slot}')
        label = f'Filter Speed{slot}'
        self._anchor()
        self._press_until('RIGHT', lambda t: 'VSP Speed Settings' in t,
                          self._NAV_MAX, 'VSP Speed Settings')
        # PLUS enters the inline sub-items (-> 'Filter Speed1 ..%'); re-press
        # until we're actually inside, so a dropped PLUS doesn't leave us in the
        # main ring (which RIGHT would then walk the wrong way).
        self._press_until('PLUS', lambda t: 'Filter Speed' in t, 6, 'VSP submenu')
        # Slot 1 is the entry point; RIGHT walks forward to higher slots.
        if slot == 1:
            return self._press_until('RIGHT', lambda t: 'Filter Speed1' in t,
                                     2, 'Filter Speed1')
        return self._press_until('RIGHT', lambda t: label in t, 8, label)

    def read_vsp_slot(self, slot: int) -> dict:
        """Read Filter Speed{slot} without changing it."""
        try:
            with _nav_lock:
                txt = self._goto_vsp_slot(slot)
                pct = self._pct(txt)
                if pct is None:
                    raise RuntimeError(f'Cannot parse Filter Speed{slot}: {txt!r}')
                with state_lock:
                    state.vsp_slot_pct[slot] = pct
                return {'slot': slot, 'speed_pct': pct}
        finally:
            self.fast_exit()

    def read_vsp_all_slots(self) -> dict:
        """Read all four VSP slot speeds in one menu session."""
        results = {}
        try:
            with _nav_lock:
                # Enter submenu once, walk RIGHT through all four slots.
                self._anchor()
                self._press_until('RIGHT', lambda t: 'VSP Speed Settings' in t,
                                  self._NAV_MAX, 'VSP Speed Settings')
                self._press_until('PLUS', lambda t: 'Filter Speed' in t, 6, 'VSP submenu')
                txt = self._press_until('RIGHT', lambda t: 'Filter Speed1' in t,
                                        2, 'Filter Speed1')
                for slot in (1, 2, 3, 4):
                    if slot > 1:
                        txt = self._press_until(
                            'RIGHT', lambda t, s=slot: f'Filter Speed{s}' in t,
                            4, f'Filter Speed{slot}')
                    pct = self._pct(txt)
                    if pct is not None:
                        results[slot] = pct
                with state_lock:
                    state.vsp_slot_pct.update(results)
        finally:
            self.fast_exit()
        return {'slots': results}

    def set_vsp_slot(self, slot: int, target_pct: int) -> dict:
        """Write Filter Speed{slot}. Snaps to 5% grid (verified step size, §12.4)."""
        if slot not in (1, 2, 3, 4):
            raise ValueError(f'VSP slot must be 1–4, got {slot}')
        target_pct = int(_clamp(round(target_pct / 5) * 5, 0, 100))
        try:
            with _nav_lock:
                self._goto_vsp_slot(slot)
                self._step_to(self._pct, target_pct,
                              'PLUS', 'MINUS', self._STEP_MAX, f'Filter Speed{slot}')
                with state_lock:
                    state.vsp_slot_pct[slot] = target_pct
                return {'slot': slot, 'target_pct': target_pct, 'result': self.text()}
        finally:
            self.fast_exit()

    def activate_vsp_slot(self, slot: int) -> dict:
        """
        Make slot {slot} the running VSP slot by cycling FILTER off→on to open
        the slot-selection window (§6.2), then using +/- to reach the target slot.

        Gate contract (§6.4 / §11): every +/- press verifies 'Filter On:' is
        still present; if the window closed early an error is raised.
        Does NOT navigate the Settings menu; holds _nav_lock throughout.
        """
        if slot not in (1, 2, 3, 4):
            raise ValueError(f'VSP slot must be 1–4, got {slot}')
        target_label = f'Spd{slot}'
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

            # Cycle +/- until we see the target slot label.
            for step in range(_SLOT_MAX_STEPS):
                if target_label in txt:
                    break
                txt = self._send('PLUS')
                if 'Filter On:' not in txt:
                    raise RuntimeError(
                        f'Slot-selection window closed early at step {step}: {txt!r}')
            else:
                raise RuntimeError(f'Could not find {target_label} in slot-selection window')

            with state_lock:
                state.circuits['FILTER'] = True
                state.vsp_active_slot = slot
            return {'activated_slot': slot, 'frame': txt}

    # Convenience aliases kept for any callers that used the slot-4 specific names.
    def read_vsp_slot4(self) -> dict:
        return self.read_vsp_slot(4)

    def set_vsp_slot4(self, target_pct: int) -> dict:
        return self.set_vsp_slot(4, target_pct)

    def activate_vsp_slot4(self) -> dict:
        return self.activate_vsp_slot(4)


def _get_panel():
    with panel_lock:
        return panel


def _get_navigator() -> Optional[MenuNavigator]:
    if _ac_backend is not None:
        # AquaConnect mode: no RS-485 panel needed
        return MenuNavigator(None, lcd, backend=_ac_backend)
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
            'vsp_slot_pct':        dict(state.vsp_slot_pct),
            'vsp_active_slot':     state.vsp_active_slot,
            'connected':           state.connected,
            'last_update':         state.last_update,
            'bridge_wedged':       state.bridge_wedged,
            'backend':             _active_backend,
        })


@app.route('/display')
def get_display() -> Response:
    l1, l2 = lcd.lines()
    return jsonify({'line1': l1, 'line2': l2})


@app.route('/display/history')
def get_display_history() -> Response:
    entries = [{'ts': ts, 'text': t} for ts, t in lcd.snapshot()]
    return jsonify({'history': entries})


@app.route('/backend')
def get_backend() -> Response:
    """Report the active navigation backend and its persisted config."""
    cfg = _load_backend_config()
    return jsonify({
        'active': _active_backend,
        'config': cfg,
    })


@app.route('/backend', methods=['POST'])
def set_backend() -> Response:
    """Switch the navigation backend.

    Body: {"backend": "aquaconnect"|"rs485",
           "aquaconnect_host"?: str, "rs485_host"?: str, "rs485_port"?: int}

    Persists the choice and exits the process so systemd restarts into the new
    backend. If the requested backend already matches the active one (and hosts
    are unchanged), this is a no-op.
    """
    body = request.get_json(force=True)
    backend = body.get('backend')
    if backend not in ('aquaconnect', 'rs485'):
        return jsonify({'error': "backend must be 'aquaconnect' or 'rs485'"}), 400

    cfg = _load_backend_config()
    cfg['backend'] = backend
    for k in ('aquaconnect_host', 'rs485_host'):
        if body.get(k):
            cfg[k] = body[k]
    if body.get('rs485_port'):
        cfg['rs485_port'] = int(body['rs485_port'])

    # No-op if nothing actually changes (avoid a needless restart loop).
    if backend == _active_backend and cfg == _load_backend_config():
        return jsonify({'ok': True, 'unchanged': True, 'active': _active_backend})

    try:
        _save_backend_config(cfg)
    except Exception as e:
        log.error('Could not persist backend config: %s', e)
        return jsonify({'error': f'persist failed: {e}'}), 500

    log.info('Backend switch requested -> %s; restarting to apply.', backend)

    # Exit shortly after responding so systemd (Restart=always) relaunches us
    # reading the new config. Daemon timer lets the HTTP response flush first.
    def _restart() -> None:
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_restart, daemon=True, name='backend-restart').start()
    return jsonify({'ok': True, 'restarting': True, 'backend': backend})


@app.route('/bridge/health')
def get_bridge_health() -> Response:
    """Return the AquaConnect command-path health status.

    By default reports the cached flag (cheap). Pass ?probe=1 to actively run
    the canary command-path test right now — this physically toggles the canary
    output and confirms the equipment-state field changes, giving a true live
    answer instead of the cached flag.
    """
    do_probe = request.args.get('probe') in ('1', 'true', 'yes')
    probe_result = None
    if do_probe and _ac_backend is not None:
        probe_result = _ac_canary_probe()  # updates the flag as a side effect

    with state_lock:
        wedged = state.bridge_wedged
    with _wedge_lock:
        streak = _wedge_fail_streak
    status = 'wedged' if wedged else 'ok'
    code = 503 if wedged else 200
    body = {
        'status': status,
        'bridge_wedged': wedged,
        'consecutive_failures': streak,
    }
    if probe_result is not None:
        body['probe'] = probe_result
    return jsonify(body), code


@app.route('/debug/log')
def get_debug_log() -> Response:
    """Return the current debug log file as plain text for download.

    Also includes the previous hour's rotated file (if present) so you get
    full context across a rotation boundary.
    GET /debug/log          → current hour
    GET /debug/log?all=1    → current + previous hour concatenated
    """
    import glob as _glob
    want_all = request.args.get('all') in ('1', 'true', 'yes')
    paths = [_DEBUG_LOG_PATH]
    if want_all:
        # TimedRotatingFileHandler appends a timestamp suffix to rotated files
        rotated = sorted(_glob.glob(_DEBUG_LOG_PATH + '.*'))
        paths = rotated + [_DEBUG_LOG_PATH]
    content_parts = []
    for p in paths:
        try:
            with open(p, 'r', encoding='utf-8', errors='replace') as f:
                content_parts.append(f.read())
        except FileNotFoundError:
            pass
    content = '\n'.join(content_parts) if content_parts else '(no log yet)\n'
    return Response(content, mimetype='text/plain',
                    headers={'Content-Disposition': 'attachment; filename="pool_sidecar_debug.log"'})


# ---------------------------------------------------------------------------
# Wedge-test harness  (GET/POST /debug/wedge-test)
#
# Goal: isolate WHICH stimulus pattern wedges the AquaConnect box. The wedge is
# sticky until power-cycle, so each test is ONE-SHOT: run one scenario, probe,
# record the verdict, then POWER-CYCLE before the next scenario. The harness
# refuses to run if the box is already wedged (the result would be meaningless).
#
# Timing/volume/concurrency scenarios use the inert AUX2 canary so the box stays
# on the idle screen and the post-test probe stays valid. The cross-function and
# menu scenarios are opt-in and DO actuate equipment / navigate menus.
# ---------------------------------------------------------------------------

# Each scenario isolates ONE variable. Defaults are chosen to be aggressive
# enough to provoke a wedge while staying recoverable.
_WEDGE_SCENARIOS = {
    'control':   'Sanity: 1 canary press, generous 2.0s gaps. Should NEVER wedge.',
    'volume':    'Volume on one function: N canary presses at normal cadence.',
    'tight_gap': 'Timing edge: N canary presses at a shortened gap (default 0.4s).',
    'read_flood':'Reads alone: N "Update Local Server&" reads at a shortened gap (should never wedge).',
    'race':      'Concurrency: canary presses + reads from 2 threads, no _http_lock (overlapping sockets).',
    'cross_fn':  'DISRUPTIVE: cycle different equipment keys in even pairs (nets to no change).',
    'menu_churn':'Deep nav: enable→disable a heater N times (multi-key Settings nav).',
}


def _wt_banner(msg: str) -> None:
    """Stamp a clearly-greppable boundary line into the debug log."""
    log.debug('===== WEDGE-TEST %s =====', msg)


def _wt_raw_post(key_code: str) -> Optional[str]:
    """Single raw POST through the backend, bypassing the high-level helpers.

    Used by scenarios that need to drive the socket directly (tight_gap, race).
    Gap enforcement still happens inside _post via self._last_req.
    """
    return _ac_backend._post(key_code)


def _run_wedge_scenario(name: str, count: int, gap: float,
                        keys: Optional[list] = None) -> dict:
    """Run one scenario synchronously and return its verdict dict."""
    global _AC_MIN_GAP_S
    canary = _AC_KEY_CODES['AUX2']
    steps = 0
    t0 = time.time()
    _wt_banner(f'scenario={name} count={count} gap={gap} START')

    if name == 'control':
        with _ac_backend._http_lock:
            saved = _AC_MIN_GAP_S
            _AC_MIN_GAP_S = 2.0
            try:
                _wt_raw_post(canary)
                steps += 1
            finally:
                _AC_MIN_GAP_S = saved

    elif name == 'volume':
        # Real action path (press + 4 confirm reads) on the inert canary.
        for _ in range(count):
            _ac_backend.send_nav_key('AUX2')
            steps += 1

    elif name == 'tight_gap':
        with _ac_backend._http_lock:
            saved = _AC_MIN_GAP_S
            _AC_MIN_GAP_S = gap
            try:
                for _ in range(count):
                    _wt_raw_post(canary)
                    steps += 1
            finally:
                _AC_MIN_GAP_S = saved

    elif name == 'read_flood':
        with _ac_backend._http_lock:
            saved = _AC_MIN_GAP_S
            _AC_MIN_GAP_S = gap
            try:
                for _ in range(count):
                    _ac_backend._request('Update Local Server&')
                    steps += 1
            finally:
                _AC_MIN_GAP_S = saved

    elif name == 'race':
        # Deliberately violate the locking discipline: two threads hammer the
        # socket with NO _http_lock, so presses and reads genuinely overlap.
        # Reproduces the worst-case race the production locks normally prevent.
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    _wt_raw_post('00')
                except Exception as e:
                    log.debug('race reader error: %s', e)

        rt = threading.Thread(target=reader, daemon=True, name='wt-race-reader')
        rt.start()
        try:
            for _ in range(count):
                _wt_raw_post(canary)
                steps += 1
        finally:
            stop.set()
            rt.join(timeout=5)

    elif name == 'cross_fn':
        # Even pairs per key → net-zero equipment change. DISRUPTIVE.
        seq = keys or ['LIGHTS', 'FILTER', 'AUX1', 'AUX2']
        for _ in range(count):
            for k in seq:
                _ac_backend.send_nav_key(k)
                _ac_backend.send_nav_key(k)   # press twice → back to original
                steps += 2

    elif name == 'menu_churn':
        nav = _get_navigator()
        if nav is None:
            raise RuntimeError('no navigator for menu_churn')
        which = 'pool'
        for _ in range(count):
            nav.set_heater_enabled(which, True)
            nav.set_heater_enabled(which, False)
            steps += 2

    else:
        raise ValueError(f'unknown scenario: {name}')

    elapsed = round(time.time() - t0, 1)
    _wt_banner(f'scenario={name} STIMULUS DONE steps={steps} elapsed={elapsed}s — probing')

    # Verdict: probe the command path now.
    with _nav_lock:
        probe = _ac_backend.probe_wedge()
    wedged = not probe.get('alive', False)
    _wt_banner(f'scenario={name} VERDICT wedged={wedged} probe={probe}')

    return {
        'scenario': name,
        'wedged': wedged,
        'steps': steps,
        'elapsed_s': elapsed,
        'probe': probe,
        'params': {'count': count, 'gap': gap, 'keys': keys},
    }


@app.route('/debug/wedge-test', methods=['GET'])
def list_wedge_scenarios() -> Response:
    """List available wedge-test scenarios and how to run them."""
    return jsonify({
        'usage': "POST /debug/wedge-test  body: {\"scenario\":\"volume\",\"count\":30,\"gap\":0.4}",
        'note': ('Wedge is sticky until power-cycle: run ONE scenario, read the '
                 'verdict + /debug/log, then power-cycle before the next. Refuses '
                 'to run if already wedged (pass force=true to override).'),
        'scenarios': _WEDGE_SCENARIOS,
        'defaults': {'count': 30, 'gap': 0.4},
    })


@app.route('/debug/wedge-test', methods=['POST'])
def run_wedge_test() -> Response:
    """Run a single wedge-test scenario synchronously and return the verdict."""
    if _ac_backend is None:
        return jsonify({'error': 'wedge-test requires the aquaconnect backend'}), 400
    body = request.get_json(force=True, silent=True) or {}
    name = body.get('scenario')
    if name not in _WEDGE_SCENARIOS:
        return jsonify({'error': f'unknown scenario: {name}',
                        'scenarios': list(_WEDGE_SCENARIOS)}), 400
    count = int(body.get('count', 30))
    gap = float(body.get('gap', 0.4))
    keys = body.get('keys')
    force = body.get('force') in (True, '1', 'true', 'yes')

    # Refuse if already wedged — a poisoned starting state makes the result lie.
    with state_lock:
        already = state.bridge_wedged
    if already and not force:
        return jsonify({
            'error': 'box is already wedged — power-cycle first (or pass force=true)',
            'bridge_wedged': True,
        }), 409

    try:
        result = _run_wedge_scenario(name, count, gap, keys)
    except Exception as e:
        log.error('wedge-test %s failed: %s', name, e)
        _wt_banner(f'scenario={name} ERROR {e}')
        return jsonify({'error': str(e), 'scenario': name}), 500
    code = 200 if not result['wedged'] else 503
    return jsonify(result), code


@app.route('/bridge/health/reset', methods=['POST'])
def reset_bridge_wedge() -> Response:
    """Manually clear the wedge flag after power-cycling the box."""
    global _wedge_fail_streak
    with _wedge_lock:
        _wedge_fail_streak = 0
    with state_lock:
        state.bridge_wedged = False
    log.info('Bridge wedge flag manually cleared')
    return jsonify({'ok': True, 'bridge_wedged': False})


def _ac_canary_probe() -> dict:
    """Active command-path probe via the canary output (AUX2, inert here).

    Presses the canary and checks the raw equipment-state field actually
    changes. Records success/failure (debounced flag) and returns the probe
    detail dict {'alive', 'before', 'after', 'attempts'}.
    """
    global _wedge_fail_streak
    if _ac_backend is None:
        return {'alive': True, 'skipped': 'no aquaconnect backend'}
    try:
        with _nav_lock:
            result = _ac_backend.probe_wedge()
        if result.get('alive'):
            log.debug('Canary probe: path OK (%s→%s)',
                      result.get('before'), result.get('after'))
            _record_command_success()
        else:
            # An active canary probe is deterministic: N presses with zero
            # equipment-state change is conclusive, so flag immediately rather
            # than waiting for the debounce threshold (that guards the flaky
            # passive signal, not this).
            log.warning('Canary probe: equipment-state field did not change '
                        '(field=%s) — command path wedged', result.get('before'))
            with _wedge_lock:
                _wedge_fail_streak = max(_wedge_fail_streak + 1, _WEDGE_FAIL_THRESHOLD)
                already = state.bridge_wedged
            if not already:
                with state_lock:
                    state.bridge_wedged = True
                log.warning('Bridge command path wedged (active canary probe). '
                            'Power-cycle the AquaConnect box to recover.')
        return result
    except Exception as e:
        log.error('Canary probe error: %s', e)
        return {'alive': False, 'error': str(e)}


def _canary_probe_loop() -> None:
    """Background thread: periodically probe the command path when idle.

    Healthy: probe every _WEDGE_PROBE_INTERVAL_S (cheap liveness check).
    Wedged:  probe every _WEDGE_RECOVERY_INTERVAL_S so recovery after a
             power-cycle is reflected quickly instead of waiting a full cycle.
    """
    # Stagger first probe so it doesn't fire at startup during initial connect.
    time.sleep(60 + random.uniform(0, 30))
    while True:
        try:
            # Defer to any real action that is running OR queued so the probe's
            # canary presses never stomp on a user command (busy() covers both).
            if _ac_backend is not None and not _nav_lock.busy():
                _ac_canary_probe()
        except Exception as e:
            log.error('Canary probe loop error: %s', e)
        with state_lock:
            wedged = state.bridge_wedged
        time.sleep(_WEDGE_RECOVERY_INTERVAL_S if wedged else _WEDGE_PROBE_INTERVAL_S)


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


# Equipment circuits that toggle with a single AquaConnect keypad key. Maps the
# circuit name to its _AC_KEY_CODES entry. POOL/SPA/SPILLOVER are valve-mode
# controls (handled via /mode) and HEATER_1 routes through the navigator below,
# so neither appears here.
_AC_CIRCUIT_KEYS = {
    'FILTER': 'FILTER', 'LIGHTS': 'LIGHTS', 'AUX_1': 'AUX1', 'AUX_2': 'AUX2',
}


def _ac_set_circuit(key: str, on: bool) -> Response:
    """Drive a circuit on/off through the AquaConnect backend.

    HEATER_1 is the shared heater: it follows the active valve mode (pool heater
    in pool mode, spa heater in spa mode) and goes through the navigator's
    Settings-menu enable toggle — the keypad HEATER_1 key is unreliable from the
    idle screen. The simple equipment circuits send their keypad key once (only
    if not already in the desired state) and confirm via the re-read LED state.
    """
    log.info('HomeKit action: circuit %s -> %s', key, 'ON' if on else 'OFF')
    with state_lock:
        wedged = state.bridge_wedged
    if wedged:
        return jsonify({
            'error': 'Bridge command path wedged — power-cycle the AquaConnect box',
            'bridge_wedged': True,
        }), 503

    if key == 'HEATER_1':
        nav = _get_navigator()
        if nav is None:
            return jsonify({'error': 'Not connected'}), 503
        with state_lock:
            which = 'spa' if state.valve_mode == 'spa' else 'pool'
        try:
            result = nav.set_heater_enabled(which, on)
        except Exception as e:
            log.error('HomeKit action HEATER_1 failed: %s — probing bridge', e)
            _record_command_failure()
            _immediate_wedge_probe()
            return jsonify({'error': str(e), 'bridge_wedged': state.bridge_wedged}), 502
        # The panel immediately shows "Heater1 Auto Control" / "Heater1 Manual Off"
        # after the nav completes. Read it now so the confirmation lands in
        # pool/spa_heater_enabled before any poll can see a stale scroll frame.
        with _ac_backend._http_lock:
            _ac_backend._apply(_ac_backend._read())
        _record_command_success()
        log.info('AquaConnect heater (%s) -> %s', which, 'ON' if on else 'OFF')
        return jsonify({'ok': True, 'which': which, **result})

    keypad = _AC_CIRCUIT_KEYS.get(key)
    if keypad is None:
        return jsonify({'error': f'{key} cannot be toggled in AquaConnect mode'}), 422

    # Idempotent: only press if not already where we want it (the key is a
    # toggle, so a redundant press would flip us away from the target).
    with _nav_lock:
        with state_lock:
            cur = state.circuits.get(key)
        if cur == on:
            return jsonify({'ok': True, 'already': True})
        _ac_backend.send_nav_key(keypad)   # press + settle + re-read (updates state)
        with state_lock:
            new = state.circuits.get(key)
    if new == on:
        _record_command_success()
        log.info('Circuit %s -> %s (AquaConnect)', key, 'ON' if on else 'OFF')
        return jsonify({'ok': True})
    log.warning('HomeKit action: circuit %s -> %s NOT CONFIRMED (now=%s) — probing bridge',
                key, 'ON' if on else 'OFF', new)
    _record_command_failure()
    _immediate_wedge_probe()
    return jsonify({
        'error': f'{key} toggle not confirmed (now={new})',
        'bridge_wedged': state.bridge_wedged,
    }), 502


@app.route('/circuit/<name>', methods=['POST'])
def set_circuit(name: str) -> Response:
    body = request.get_json(force=True)
    on: bool = bool(body.get('on', False))
    key = name.upper()
    if key not in CIRCUIT_NAMES:
        return jsonify({'error': f'Unknown circuit: {name}'}), 400
    if _ac_backend is not None:
        try:
            return _ac_set_circuit(key, on)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            log.error(f'ac set_circuit {key}: {e}')
            return jsonify({'error': str(e)}), 500
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


@app.route('/debug/aquaconnect', methods=['GET', 'POST'])
def debug_aquaconnect() -> Response:
    """Inspect/test the AquaConnect HTTP backend directly.

    GET  — do one no-op read and return the FULL raw body plus the parsed LCD
           text, decoded equipment state, and the 3 extracted body lines. Use
           this to calibrate the parser against the live box (?raw=1 includes
           the entire HTML body verbatim).
    POST — send a sequence of keys. Body: {"keys": ["MENU", "RIGHT", ...]}.
           Each key uses the configured settle delay; the LCD after each press
           is returned along with the running equipment-state decode.
    """
    if _ac_backend is None:
        return jsonify({'error': 'AquaConnect backend not active '
                                 '(start with --backend aquaconnect)'}), 400
    if request.method == 'GET':
        with _ac_backend._http_lock:
            body = _ac_backend._post('00')
        lcd, led = _ac_backend._parse(body) if body else (None, {})
        lines = _ac_backend._body_lines(body) if body else []
        out = {
            'lcd_parsed': lcd,
            'lcd_norm': _norm(lcd) if lcd else None,
            'lcd_cached': _ac_backend.lcd.text(),
            'led_state': led,
            'body_lines': lines[:4],
        }
        if request.args.get('raw'):
            out['raw_body'] = body
        return jsonify(out)
    # POST — key sequence
    data = request.get_json(force=True) or {}
    keys = data.get('keys', [])
    steps = []
    for key in keys:
        before = _ac_backend.lcd.text()
        try:
            _ac_backend.send_nav_key(key)
            steps.append({'key': key, 'before': before,
                          'after': _ac_backend.lcd.text(),
                          'changed': before != _ac_backend.lcd.text(),
                          'led_state': dict(_ac_backend._last_led)})
        except ValueError as e:
            steps.append({'key': key, 'error': str(e)})
    return jsonify({'steps': steps})


@app.route('/debug/lcd-watch')
def debug_lcd_watch() -> Response:
    """Watch the LCD in real time and return one entry per distinct display change.

    Query:
      ?secs=S   — how long to watch (default 90, max 300)
      ?interval=I — sample period in seconds (default 1.0)

    Polls once per interval and records an entry whenever the normalized text
    changes. This de-duplicates the ~2 Hz bus frames and shows only real
    status-cycle transitions, giving a clean picture of the display ring.

    Each entry: {elapsed_s, norm, raw_stripped}
    """
    try:
        secs = min(float(request.args.get('secs', 90)), 300.0)
        interval = max(0.2, float(request.args.get('interval', 1.0)))
    except (TypeError, ValueError):
        secs, interval = 90.0, 1.0

    entries = []
    t0 = time.time()
    deadline = t0 + secs
    last_norm = None

    while time.time() < deadline:
        cur_norm = lcd.text()
        if cur_norm != last_norm:
            with lcd._lock:
                raw = (lcd._latest or '').strip()
            entries.append({
                'elapsed_s': round(time.time() - t0, 2),
                'norm': cur_norm,
                'raw_stripped': raw,
            })
            last_norm = cur_norm
        lcd._event.clear()
        lcd._event.wait(min(interval, max(0.0, deadline - time.time())))

    return jsonify({'watch_secs': secs, 'count': len(entries), 'frames': entries})


@app.route('/debug/map-menu', methods=['POST'])
def debug_map_menu() -> Response:
    """Discovery: press MENU then RIGHT to map the top-level and Settings rings.

    Body:
      frametype: "remote" (default) or "local" — RS-485 frame type for key sends.
                 AquaConnect uses "remote"; the library default is "local".
                 If MENU is not entering the menu, try switching this.
      menu_budget: max MENU presses to find a menu header (default 6)
      right_presses: RIGHT presses to make once in the menu (default 15)
      exit: if true (default), attempt to exit back to Default display after

    Each press is validated: after MENU we confirm we left the status cycle
    (got a menu header line, not a status item). RIGHT presses log before/after
    individually. Returns partial + diagnosis immediately if MENU fails.
    """
    body = request.get_json(force=True) or {}
    frametype = body.get('frametype', 'remote').lower()
    menu_budget = max(1, int(body.get('menu_budget', 6)))
    right_presses = max(1, int(body.get('right_presses', 15)))
    do_exit = bool(body.get('exit', True))

    p = _get_panel()
    if p is None:
        return jsonify({'error': 'Not connected'}), 503

    aq = p._aq
    Keys = p._Keys

    # Known status-cycle item prefixes — if we land on one of these after
    # pressing MENU, we're still in the status cycle, not in a menu.
    STATUS_PREFIXES = ('Thursday', 'Pool Temp', 'Air Temp', 'Pool Chlorinator',
                       'Salt Level', 'Heater1', 'Filter Speed', 'Spa Temp')

    def _is_status(norm: str) -> bool:
        return any(norm.startswith(p) for p in STATUS_PREFIXES)

    def _send_raw(key_name: str) -> None:
        """Send key using the chosen frame type (remote or local)."""
        k = getattr(Keys, key_name)
        type_bytes = aq.FRAME_TYPE_REMOTE_WIRED_KEY_EVENT if frametype == 'remote' \
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
        aq._send_queue.put({'frame': frame})

    def _press(key_name: str, wait: float = 5.0):
        before = lcd.text()
        t0 = time.time()
        _send_raw(key_name)
        deadline = t0 + wait
        while time.time() < deadline:
            # Use canonical same-item check so SHORT/LONG oscillation doesn't
            # count as a real change.
            if not MenuNavigator._same_item(lcd.text(), before):
                break
            lcd._event.clear()
            lcd._event.wait(min(0.5, max(0.0, deadline - time.time())))
        after = lcd.text()
        return {
            'key': key_name,
            'frametype': frametype,
            'before': before,
            'after': after,
            'changed': not MenuNavigator._same_item(before, after),
            'is_status': _is_status(after),
            'wait_s': round(time.time() - t0, 3),
        }

    steps = []
    try:
        with _nav_lock:
            # Phase 1: press MENU until we land on a non-status item (menu header)
            in_menu = False
            for i in range(menu_budget):
                r = _press('MENU')
                steps.append(r)
                if not _is_status(r['after']):
                    in_menu = True
                    break

            if not in_menu:
                return jsonify({
                    'diagnosis': f'MENU never left status cycle after {menu_budget} presses '
                                 f'(frametype={frametype!r}). Try frametype="local".',
                    'count': len(steps), 'steps': steps,
                })

            # Phase 2: walk RIGHT through the menu ring
            for _ in range(right_presses):
                r = _press('RIGHT')
                steps.append(r)
                # Detect full-ring wrap: back to same header we started from
                if (len(steps) > 2
                        and steps[-1]['after'] == steps[0]['after']
                        and not _is_status(steps[-1]['after'])):
                    break

            if do_exit:
                # Press MENU until we see a status item (back in Default display),
                # then stop — crude but safe when we don't know which menu we're in.
                for _ in range(10):
                    r = _press('MENU', wait=3.0)
                    if _is_status(r['after']):
                        break
    except Exception as e:
        log.error('map-menu: %s', e)
        return jsonify({'error': str(e), 'partial': steps}), 500

    return jsonify({'count': len(steps), 'steps': steps})


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


def _apply_setpoint(which: str, temp_f: int) -> None:
    """Debounced setpoint application — runs on the WriteDebouncer worker."""
    log.info('HomeKit action: heater %s setpoint -> %s°F', which, temp_f)
    nav = _get_navigator()
    if nav is None:
        log.warning('setpoint %s=%s dropped: not connected', which, temp_f)
        return
    try:
        result = nav.set_heater(which, int(temp_f))
    except Exception as e:
        log.error('HomeKit action heater %s setpoint failed: %s — probing bridge', which, e)
        _record_command_failure()
        _immediate_wedge_probe()
        return
    log.info('Heater %s setpoint -> %s°F (was_off=%s)',
             which, temp_f, result['was_off'])


@app.route('/heater/<which>/setpoint', methods=['POST'])
def set_heater_setpoint(which: str) -> Response:
    """
    Set a heater temperature setpoint via menu navigation.
    Body: {"temp_f": 88}
    Handles the forced-off enable/restore cycle automatically (§13.3).

    Debounced: HomeKit emits a burst of writes while the slider is dragged and
    each one is a ~15s navigation. We update the optimistic state immediately,
    coalesce the burst, and apply only the final value after _WRITE_DEBOUNCE_S
    of quiet. Returns 202 (accepted) — the physical write happens shortly after.
    """
    body = request.get_json(force=True)
    temp_f = body.get('temp_f')
    if temp_f is None:
        return jsonify({'error': 'temp_f is required'}), 400
    if which not in ('pool', 'spa'):
        return jsonify({'error': 'which must be "pool" or "spa"'}), 400
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    temp_f = int(_clamp(int(temp_f), 65, 104))
    # Optimistic state so /status (and HomeKit's read-back) reflect the target
    # immediately, before the slow navigation runs.
    with state_lock:
        if which == 'pool':
            state.pool_setpoint_f = temp_f
        else:
            state.spa_setpoint_f = temp_f
    _setpoint_debouncer.submit(which, temp_f)
    return jsonify({'ok': True, 'queued': True, 'which': which,
                    'target_f': temp_f}), 202


# Legacy alias kept for backwards compat — routes to pool heater
@app.route('/heater/setpoint', methods=['POST'])
def set_heater_setpoint_legacy() -> Response:
    body = request.get_json(force=True)
    temp_f = body.get('temp_f')
    which = body.get('which', 'pool')
    if temp_f is None:
        return jsonify({'error': 'temp_f and which ("pool"|"spa") are required'}), 400
    return set_heater_setpoint(which)


@app.route('/vsp/slots')
def get_vsp_all_slots() -> Response:
    """Read all four VSP slot speeds via menu navigation."""
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        return jsonify(nav.read_vsp_all_slots())
    except Exception as e:
        log.error(f'read_vsp_all_slots: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/vsp/slot/<int:slot>')
def get_vsp_slot(slot: int) -> Response:
    """Read Filter Speed{slot} (1-4) via menu navigation."""
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        return jsonify(nav.read_vsp_slot(slot))
    except (ValueError, Exception) as e:
        log.error(f'read_vsp_slot {slot}: {e}')
        return jsonify({'error': str(e)}), (400 if isinstance(e, ValueError) else 500)


@app.route('/vsp/slot/<int:slot>', methods=['POST'])
def set_vsp_slot(slot: int) -> Response:
    """Set Filter Speed{slot} (1-4). Body: {"speed_pct": 75}. Snaps to 5% grid."""
    body = request.get_json(force=True)
    pct = body.get('speed_pct')
    if pct is None:
        return jsonify({'error': 'speed_pct is required'}), 400
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        result = nav.set_vsp_slot(slot, int(pct))
        log.info(f'VSP slot{slot} -> {result["target_pct"]}%')
        return jsonify(result)
    except (ValueError, Exception) as e:
        log.error(f'set_vsp_slot {slot}: {e}')
        return jsonify({'error': str(e)}), (400 if isinstance(e, ValueError) else 500)


@app.route('/vsp/slot/<int:slot>/activate', methods=['POST'])
def activate_vsp_slot(slot: int) -> Response:
    """
    Activate slot {slot} (1-4) as the running VSP slot by cycling FILTER off→on.
    No body required. Filter is left ON after the call.
    """
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        result = nav.activate_vsp_slot(slot)
        log.info(f'VSP slot{slot} activated (filter on)')
        return jsonify(result)
    except (ValueError, Exception) as e:
        log.error(f'activate_vsp_slot {slot}: {e}')
        return jsonify({'error': str(e)}), (400 if isinstance(e, ValueError) else 500)


# Legacy slot-4 aliases — kept for backwards compatibility with existing callers.
@app.route('/vsp/slot4')
def get_vsp_slot4_compat() -> Response:
    return get_vsp_slot(4)

@app.route('/vsp/slot4', methods=['POST'])
def set_vsp_slot4_compat() -> Response:
    return set_vsp_slot(4)

@app.route('/vsp/slot4/activate', methods=['POST'])
def activate_vsp_slot4_compat() -> Response:
    return activate_vsp_slot(4)


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
    nav = _get_navigator()
    if nav is not None:
        # AquaConnect and RS-485 (via navigator): use Settings menu navigation.
        try:
            result = nav.set_super_chlorinate(on)
            log.info(f'Super-chlorinate -> {"ON" if on else "OFF"} (menu nav)')
            return jsonify(result)
        except Exception as e:
            log.error(f'set_super_chlorinate (nav): {e}')
            return jsonify({'error': str(e)}), 500
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
    parser.add_argument('--heater-refresh', type=float, default=0.0,
                        help='Seconds between background heater-state reads '
                             '(menu navigation). 0 disables (default).')
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
    parser.add_argument('--backend', choices=['rs485', 'aquaconnect'], default='rs485',
                        help='Navigation backend: rs485 (default) or aquaconnect (HTTP).')
    parser.add_argument('--aquaconnect-host', default='192.168.50.100',
                        help='AquaConnect box IP for --backend aquaconnect. Default 192.168.50.100.')
    args = parser.parse_args()

    # Persisted backend selection (written by POST /backend) overrides CLI args,
    # so the plugin can switch backends and the choice survives restarts. The
    # CLI args act as the initial defaults when no config file exists yet.
    cfg = _load_backend_config()
    backend = cfg.get('backend', args.backend)
    aquaconnect_host = cfg.get('aquaconnect_host', args.aquaconnect_host)
    rs485_host = cfg.get('rs485_host', args.host)
    rs485_port = cfg.get('rs485_port', args.port)

    KEY_BURST = args.key_burst
    KEY_PREDELAY_MS = args.key_predelay_ms
    KEY_GAP_MS = args.key_gap_ms

    global _ac_backend, _setpoint_debouncer, _active_backend
    _active_backend = backend
    # Coalesce bursts of HomeKit setpoint writes; apply only the final value.
    _setpoint_debouncer = WriteDebouncer(
        lambda which, temp_f: _apply_setpoint(which, temp_f))

    if backend == 'aquaconnect':
        _ac_backend = AquaConnectBackend(host=aquaconnect_host)
        log.info('AquaConnect backend: http://%s/WNewSt.htm', aquaconnect_host)
        threading.Thread(target=_canary_probe_loop, daemon=True,
                         name='ac-canary').start()
        # AquaConnect mode: no RS-485 panel thread needed
        app.run(host=args.api_host, port=args.api_port, threaded=True)
        return

    if args.simulate:
        t = threading.Thread(target=simulate_thread, daemon=True, name='simulate')
    else:
        if not rs485_host:
            parser.error('--host is required unless --simulate is given or --backend aquaconnect')
        t = threading.Thread(target=panel_thread, args=(rs485_host, rs485_port), daemon=True, name='aqualogic')
    t.start()

    if args.heater_refresh > 0:
        threading.Thread(target=refresher_thread, args=(args.heater_refresh,),
                         daemon=True, name='refresher').start()

    log.info('REST API listening on %s:%s (key-burst=%d)',
             args.api_host, args.api_port, KEY_BURST)
    app.run(host=args.api_host, port=args.api_port, threaded=True)


if __name__ == '__main__':
    main()
