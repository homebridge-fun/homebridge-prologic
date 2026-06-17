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
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Tuple

from flask import Flask, jsonify, request, Response

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
log = logging.getLogger('pool_service')


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
        """Return current (line1, line2) from the latest LCD frame."""
        with self._lock:
            text = self._latest or ''
        parts = text.split('\n', 1)
        l1 = parts[0].strip()
        l2 = parts[1].strip() if len(parts) > 1 else ''
        return l1, l2

    def snapshot(self):
        with self._lock:
            return [(ts, t) for ts, t in self.history]


lcd = LcdCapture()

# One menu operation at a time — keypad navigation is not re-entrant.
_nav_lock = threading.Lock()


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
        # HEATER_1 set_state always returns False; HEATER_AUTO_MODE is the
        # only library-supported heater on/off control.
        if s == self._States.HEATER_1:
            s = self._States.HEATER_AUTO_MODE
        return bool(self._aq.set_state(s, on))

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

class MenuNavigator:
    _SETTINGS_HDR = 'Settings Menu'
    _DEFAULT_MENU_HDR = 'Default Menu'
    _KEY_TIMEOUT = 4.0   # seconds; bus is slow (§3.2 rule 4)
    _MENU_MAX = 8        # MENU presses before aborting anchor

    def __init__(self, p, l: LcdCapture):
        self._panel = p
        self._lcd = l

    def _send(self, key: str) -> Tuple[str, str]:
        # Clear the event BEFORE sending so we capture the update that follows,
        # whether it arrives synchronously (sim) or async (real RS-485 bus).
        self._lcd._event.clear()
        self._panel.send_key(key)
        self._lcd._event.wait(self._KEY_TIMEOUT)
        return self._lcd.lines()

    def _anchor(self) -> None:
        """Drive MENU until line 1 == 'Settings Menu' (§3.2 rule 1)."""
        for _ in range(self._MENU_MAX):
            l1, _ = self._lcd.lines()
            if l1 == self._SETTINGS_HDR:
                return
            self._send('MENU')
        raise RuntimeError('Could not anchor to Settings Menu')

    def fast_exit(self) -> None:
        """Return to Default display: MENU until 'Default Menu', then RIGHT (§13.1)."""
        for _ in range(self._MENU_MAX):
            l1, _ = self._lcd.lines()
            if l1 == self._DEFAULT_MENU_HDR:
                break
            self._send('MENU')
        self._send('RIGHT')

    # ── Heater setpoints ─────────────────────────────────────────────────────

    def read_heater(self, which: str) -> dict:
        """Navigate to a heater item and read its state without changing anything.

        When the heater is 'Manual Off' the panel shows no temperature.  We
        press PLUS to reveal the stored setpoint, record it, then re-disable
        via HEATER_1 toggle so the state is unchanged on exit.
        """
        if which not in ('pool', 'spa'):
            raise ValueError(f'which must be "pool" or "spa"')
        try:
            with _nav_lock:
                self._anchor()
                presses = 1 if which == 'spa' else 2
                for _ in range(presses):
                    self._send('RIGHT')
                l1, l2 = self._lcd.lines()
                was_off = l2.strip() == 'Manual Off'
                enabled = not was_off
                setpoint_f = None

                if was_off:
                    # PLUS from Manual Off reveals the stored °F without a
                    # visible confirmation step — panel immediately shows temp.
                    l1, l2 = self._send('PLUS')

                try:
                    setpoint_f = int(l2.replace('\xb0F', '').replace('°F', '').strip())
                except ValueError:
                    pass

                if was_off:
                    # Restore Manual Off: navigate back to this item and toggle
                    # HEATER_1 (same path as set_heater §13.3 restore).
                    self._send('RIGHT')   # lock in (move off item)
                    self._send('LEFT')    # return to item
                    for _ in range(3):
                        l1, l2 = self._send('HEATER_1')
                        if 'Manual Off' in l2:
                            break

                with state_lock:
                    if which == 'pool':
                        state.pool_heater_enabled = enabled
                        state.pool_setpoint_f = setpoint_f
                    else:
                        state.spa_heater_enabled = enabled
                        state.spa_setpoint_f = setpoint_f
                return {'which': which, 'enabled': enabled, 'setpoint_f': setpoint_f, 'raw': l2}
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
            raise ValueError(f'which must be "pool" or "spa"')
        target_f = int(_clamp(target_f, 65, 104))
        try:
            with _nav_lock:
                self._anchor()
                presses = 1 if which == 'spa' else 2
                for _ in range(presses):
                    self._send('RIGHT')
                l1, l2 = self._lcd.lines()
                was_off = l2.strip() == 'Manual Off'

                if was_off:
                    # PLUS from 'Manual Off' enables the heater and reveals the stored °F.
                    # Per §12.2 this is non-symmetric — MINUS would not undo it.
                    l1, l2 = self._send('PLUS')

                try:
                    current_f = int(l2.replace('\xb0F', '').replace('°F', '').strip())
                except ValueError:
                    raise ValueError(f'Cannot parse heater setpoint: {l2!r}')

                diff = target_f - current_f
                key = 'PLUS' if diff > 0 else 'MINUS'
                for _ in range(abs(diff)):
                    l1, l2 = self._send(key)

                # Move off the item to lock in the value (§3).
                self._send('RIGHT')

                if was_off:
                    # Restore Manual Off.  Navigate back to the heater item, then
                    # toggle HEATER_1 until the display confirms 'Manual Off'.
                    # §12.2: may require up to 2 presses from PLUS-enabled state.
                    self._send('LEFT')
                    for _ in range(3):
                        l1, l2 = self._send('HEATER_1')
                        if 'Manual Off' in l2:
                            break

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
            raise ValueError(f'which must be "pool" or "spa"')
        target_pct = _chlor_snap(int(target_pct))
        # Settings ring offsets from anchor: spa_chlorinator=5, pool_chlorinator=6
        nav_presses = 5 if which == 'spa' else 6
        try:
            with _nav_lock:
                self._anchor()
                for _ in range(nav_presses):
                    self._send('RIGHT')
                l1, l2 = self._lcd.lines()
                label = 'Spa Chlorinator' if which == 'spa' else 'Pool Chlorinator'
                if label not in l1:
                    raise RuntimeError(f'Expected {label}, got: {l1!r}')
                try:
                    current_pct = int(l2.replace('%', '').strip())
                except ValueError:
                    raise ValueError(f'Cannot parse chlorinator %: {l2!r}')
                key, n = _chlor_presses(current_pct, target_pct)
                for _ in range(n):
                    self._send(key)
                with state_lock:
                    state.chlorinator_percent = float(target_pct)
                return {'which': which, 'target_pct': target_pct, 'previous_pct': current_pct}
        finally:
            self.fast_exit()

    # ── VSP slot 4 ───────────────────────────────────────────────────────────

    def read_vsp_slot4(self) -> dict:
        """Read Filter Speed4 (slot 4) without changing it."""
        try:
            with _nav_lock:
                self._anchor()
                for _ in range(3):     # RIGHT ×3 → VSP Speed Settings
                    self._send('RIGHT')
                self._send('PLUS')     # enter inline sub-items
                for _ in range(3):     # RIGHT ×3 → past Speed1/2/3 → Speed4
                    self._send('RIGHT')
                l1, l2 = self._lcd.lines()
                if 'Filter Speed4' not in l1:
                    raise RuntimeError(f'Expected Filter Speed4, got: {l1!r}')
                pct = int(l2.replace('%', '').strip())
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
                self._anchor()
                for _ in range(3):
                    self._send('RIGHT')
                self._send('PLUS')
                for _ in range(3):
                    self._send('RIGHT')
                l1, l2 = self._lcd.lines()
                if 'Filter Speed4' not in l1:
                    raise RuntimeError(f'Expected Filter Speed4, got: {l1!r}')
                current_pct = int(l2.replace('%', '').strip())
                diff = (target_pct - current_pct) // 5
                key = 'PLUS' if diff > 0 else 'MINUS'
                for _ in range(abs(diff)):
                    self._send(key)
                _, l2_after = self._lcd.lines()
                with state_lock:
                    state.vsp_slot4_pct = target_pct
                return {'slot': 4, 'target_pct': target_pct, 'result': l2_after}
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
        try:
            with _nav_lock:
                # Ensure filter is off first so the next FILTER press turns it on.
                with state_lock:
                    filter_on = state.circuits.get('FILTER', False)
                if filter_on:
                    self._send('FILTER')   # turn off
                    l1, _ = self._lcd.lines()
                    # Confirm off (default display or normal menu - no 'Filter On:')
                    if 'Filter On:' in l1:
                        raise RuntimeError(f'FILTER off did not clear window: {l1!r}')

                # Turn filter on → opens slot-selection window
                l1, l2 = self._send('FILTER')
                if 'Filter On:' not in l1:
                    raise RuntimeError(
                        f'Expected slot-selection window after FILTER on, got: {l1!r}')

                # Cycle +/- until we see Spd4 in l1.
                for step in range(_SLOT_MAX_STEPS):
                    if 'Spd4' in l1:
                        break
                    l1, l2 = self._send('PLUS')
                    if 'Filter On:' not in l1:
                        raise RuntimeError(
                            f'Slot-selection window closed early at step {step}: {l1!r}')
                else:
                    raise RuntimeError('Could not find slot 4 in slot-selection window')

                with state_lock:
                    state.circuits['FILTER'] = True
                return {'activated_slot': 4, 'line1': l1, 'line2': l2}
        except Exception:
            raise


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
            return jsonify({'error': f'{key} cannot be toggled (no keypad key)'}), 422
        log.info(f'Circuit {key} -> {"ON" if on else "OFF"}')
        return jsonify({'ok': True})
    except Exception as e:
        log.error(f'set_circuit {key}: {e}')
        return jsonify({'error': str(e)}), 500


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
    args = parser.parse_args()

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

    log.info(f'REST API listening on {args.api_host}:{args.api_port}')
    app.run(host=args.api_host, port=args.api_port, threaded=True)


if __name__ == '__main__':
    main()
