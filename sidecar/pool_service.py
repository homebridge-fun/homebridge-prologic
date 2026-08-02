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
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Tuple
from types import SimpleNamespace

from flask import Flask, jsonify, request, Response, send_from_directory

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


class _AlertBuffer(logging.Handler):
    """Captures WARNING+ log records into a bounded ring so the cockpit can
    surface every sidecar error/warning (heater-not-confirmed, wedge, unconfirmed
    writes, prefetch failures, …) without instrumenting each call site."""

    # Repeats of the SAME message within this window collapse into one entry
    # with a bumped count (e.g. a menu pass that fires MENU 3× and each times
    # out becomes one "…timed out (×3)" alert instead of three identical rows).
    _COALESCE_WINDOW_S = 120

    def __init__(self, maxlen: int = 60):
        super().__init__(level=logging.WARNING)
        self._buf = []
        self._maxlen = maxlen
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            msg = record.getMessage()
            with self._lock:
                # Coalesce with the most recent same-message entry still inside
                # the window: bump its count and slide its timestamp forward so
                # the cockpit shows one row with a live "last seen" + a ×N badge.
                for e in reversed(self._buf):
                    if e['msg'] == msg:
                        if record.created - e['t'] <= self._COALESCE_WINDOW_S:
                            e['count'] += 1
                            e['t'] = record.created
                            e['level'] = record.levelname
                            return
                        break  # newest match is stale — fall through to append
                self._buf.append({'t': record.created,
                                  'level': record.levelname,
                                  'msg': msg,
                                  'count': 1})
                if len(self._buf) > self._maxlen:
                    del self._buf[:-self._maxlen]
        except Exception:
            pass

    def recent(self, window_s=None, limit=None):
        with self._lock:
            items = list(self._buf)
        if window_s:
            cutoff = time.time() - window_s
            items = [a for a in items if a['t'] >= cutoff]
        if limit:
            items = items[-limit:]
        return items

    def clear(self):
        with self._lock:
            self._buf.clear()


_alert_buffer = _AlertBuffer()
log.addHandler(_alert_buffer)

# ---------------------------------------------------------------------------
# Rotating debug log — /tmp/pool_sidecar_debug.log, replaced every hour.
# Keeps 1 backup so the previous hour is still readable while the new one grows.
#
# The file log is best-effort: a failure to open it (e.g. a stale
# /tmp/pool_sidecar_debug.log owned by a different user from an earlier run)
# must NEVER crash the sidecar — losing debug logging is acceptable, taking
# down pool control because a log file isn't writable is not. We try the
# default path, then a per-uid fallback the current user can always create,
# then give up on file logging entirely. Override the path with
# POOL_SIDECAR_DEBUG_LOG if you want it somewhere specific.
# ---------------------------------------------------------------------------
_DEBUG_LOG_PATH = os.environ.get('POOL_SIDECAR_DEBUG_LOG', '/tmp/pool_sidecar_debug.log')


def _make_debug_handler(path: str) -> logging.Handler:
    h = logging.handlers.TimedRotatingFileHandler(
        path, when='h', interval=1, backupCount=1, encoding='utf-8')
    h.setLevel(logging.DEBUG)
    h.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    return h


log.setLevel(logging.DEBUG)   # file gets DEBUG; console handler keeps INFO via basicConfig root level
_debug_file_handler = None
for _candidate in (_DEBUG_LOG_PATH, f'/tmp/pool_sidecar_debug_{os.getuid()}.log'):
    try:
        _debug_file_handler = _make_debug_handler(_candidate)
        log.addHandler(_debug_file_handler)
        if _candidate != _DEBUG_LOG_PATH:
            log.warning('Debug log %r not writable; using fallback %r',
                        _DEBUG_LOG_PATH, _candidate)
        break
    except OSError as _e:
        continue
else:
    # No file handler could be opened — continue with console logging only.
    logging.getLogger('pool_sidecar').warning(
        'Could not open any debug log file (%s); continuing without file logging',
        _DEBUG_LOG_PATH)


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
    chlorinator_percent: Optional[float] = None      # pool chlorinator %
    spa_chlorinator_percent: Optional[float] = None  # spa chlorinator %
    pump_speed: Optional[int] = None
    spa_speed: Optional[int] = None                  # VSP Spa Speed setting %
    # True during the post-power-up pump prime (panel runs 100% on a "start
    # delay" before resuming schedule) — so the cockpit shows "100% startup".
    pump_startup: bool = False
    # populated by menu navigator reads; None = not yet read
    pool_setpoint_f: Optional[int] = None
    spa_setpoint_f: Optional[int] = None
    pool_heater_enabled: Optional[bool] = None
    spa_heater_enabled: Optional[bool] = None
    heater_active: Optional[bool] = None   # relay firing right now (States.HEATER_1)
    valve_mode: Optional[str] = None   # 'pool' | 'spa'
    vsp_slot_pct: dict = field(default_factory=dict)  # {1: pct, 2: pct, 3: pct, 4: pct}
    vsp_active_slot: Optional[int] = None  # 1-4; set on activate, None = unknown
    # Last ColorLogic scene selected per body ({'pool': n, 'spa': n}); open-loop.
    light_program: dict = field(default_factory=dict)
    connected: bool = False
    last_update: float = 0.0
    # True when the AquaConnect box has entered read-only mode (commands ACKed
    # but silently dropped at the RS-485 relay). Cleared by any confirmed write.
    bridge_wedged: bool = False
    # Epoch time when wedge was first detected; used to enforce the power-cycle
    # cooldown window before commands are retried. None = not currently wedged.
    wedge_detected_at: Optional[float] = None

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

# rs485bridge only: consecutive failed /state polls before flagging the bridge
# 'offline'. At the 0.5s poll interval + a 5s per-poll timeout, 3 misses is a
# few seconds of genuine unreachability — enough to ignore a single dropped
# packet but fast enough to reflect a real outage. Reachability-driven and
# self-clearing; there is no power-cycle cooldown for direct serial.
_BRIDGE_OFFLINE_MISSES = 3


# Key code for the canary output (AUX2 = 0B; confirmed inert on this system).
_WEDGE_CANARY_KEY = 'AUX2'
# How often (seconds) to run the active canary probe while healthy. Each probe
# is a real AUX2 write to the box, so a short interval adds command-path load;
# we keep it long and rely mostly on the reactive on-failure probe. Set to 0 to
# disable the proactive probe entirely (reactive-only): wedges are then detected
# when a real command fails to confirm. Overridable via backend.json
# ('wedge_probe_interval_s') or POST /wedge-probe.
_WEDGE_PROBE_INTERVAL_S = 1800.0
# Faster probe cadence while wedged, so recovery after a power-cycle shows up
# quickly (the box stays wedged until power-cycled, so frequent probing is safe).
_WEDGE_RECOVERY_INTERVAL_S = 30.0
# After wedge detection the HomeKit automation power-cycles the AquaConnect box.
# Block all write commands for this window so we don't hammer the box while it
# is booting, then immediately probe to confirm recovery.
_WEDGE_POWERCYCLE_COOLDOWN_S = 120.0
# Edge-triggered HomeKit automations only fire on the sensor going Off->On, so a
# single stuck-On won't retry if the first power-cycle doesn't take. When still
# wedged after the reboot window, cycle the sensor Off->On (long enough for the
# 5s poll to see the edge) to re-fire the automation — up to _WEDGE_MAX_REARMS.
_WEDGE_MAX_REARMS = 3
_WEDGE_REARM_PULSE_S = 12.0
_wedge_rearm_count = 0


def _record_command_success() -> None:
    """Call after any confirmed write. Clears the wedge flag immediately."""
    global _wedge_fail_streak, _wedge_rearm_count
    with _wedge_lock:
        _wedge_fail_streak = 0
        changed = state.bridge_wedged
    if changed:
        with state_lock:
            state.bridge_wedged = False
            state.wedge_detected_at = None
        _wedge_rearm_count = 0   # fresh retry budget for the next wedge
        log.info('Bridge command path recovered — clearing wedge flag')


def _rearm_wedge() -> bool:
    """Re-fire the power-cycle automation by cycling the sensor Off->On, up to
    _WEDGE_MAX_REARMS times. Returns True if it re-armed, False if exhausted."""
    global _wedge_rearm_count
    if _wedge_rearm_count >= _WEDGE_MAX_REARMS:
        return False
    _wedge_rearm_count += 1
    log.warning('Wedge persists after power-cycle — re-arming automation '
                '(attempt %d/%d)', _wedge_rearm_count, _WEDGE_MAX_REARMS)
    with state_lock:
        state.bridge_wedged = False           # Off edge for the poll to catch
    time.sleep(_WEDGE_REARM_PULSE_S)
    with state_lock:
        state.bridge_wedged = True            # On edge re-fires the automation
        state.wedge_detected_at = time.time()  # restart the reboot cooldown
    return True


def _record_command_failure() -> None:
    """Call when a command was sent but produced no confirmed state change."""
    global _wedge_fail_streak
    # Direct-serial bridge: a failed command means the pad was briefly
    # unreachable (weak Wi-Fi), NOT that a box wedged and needs a power-cycle.
    # There's nothing to power-cycle and no read-only mode to escape. Offline
    # detection is owned by RS485BridgeBackend._poll_loop (reachability-driven,
    # self-clearing). So don't engage the AquaConnect cooldown/block machinery
    # here — it would turn a 2-second network blip into a sticky 120s wedge.
    if _active_backend == 'rs485bridge':
        return
    with _wedge_lock:
        _wedge_fail_streak += 1
        streak = _wedge_fail_streak
        already = state.bridge_wedged
    if streak >= _WEDGE_FAIL_THRESHOLD and not already:
        now = time.time()
        with state_lock:
            state.bridge_wedged = True
            state.wedge_detected_at = now
        log.warning(
            'Bridge command path appears wedged (%d consecutive unconfirmed writes). '
            'Recovery: %s. Commands blocked for %.0fs cooldown.',
            streak, _wedge_recovery_hint(), _WEDGE_POWERCYCLE_COOLDOWN_S)


def _wedge_cooling_down() -> Optional[float]:
    """Return seconds remaining in the power-cycle cooldown, or None if clear."""
    with state_lock:
        if not state.bridge_wedged or state.wedge_detected_at is None:
            return None
        remaining = _WEDGE_POWERCYCLE_COOLDOWN_S - (time.time() - state.wedge_detected_at)
        return remaining if remaining > 0 else None


def _wedge_block_response() -> Optional[tuple]:
    """Return a (Response, status) 503 tuple if commands should be blocked, else None.

    Two cases:
    - Still in power-cycle cooldown: box is rebooting, don't send anything yet.
    - Wedged but cooldown elapsed: still blocked until the probe confirms recovery.
    """
    # Direct-serial bridge: never BLOCK commands. There's no power-cycle cooldown
    # to wait out — the bridge is stateless, so a command during an outage just
    # fails fast and the caller retries. Blocking here (the AquaConnect behavior)
    # only compounds a transient blip. The 'offline' flag is informational.
    if _active_backend == 'rs485bridge':
        return None
    with state_lock:
        wedged = state.bridge_wedged
        detected_at = state.wedge_detected_at
    if not wedged:
        return None
    remaining = None
    if detected_at is not None:
        remaining = _WEDGE_POWERCYCLE_COOLDOWN_S - (time.time() - detected_at)
    if remaining is not None and remaining > 0:
        return jsonify({
            'error': f'{_wedge_subject()} recovering — commands blocked for {remaining:.0f}s cooldown',
            'bridge_wedged': True,
            'cooling_down': True,
            'cooldown_remaining_s': round(remaining),
        }), 503
    return jsonify({
        'error': f'Command path wedged — {_wedge_recovery_hint()}',
        'bridge_wedged': True,
        'cooling_down': False,
    }), 503


def _wedge_subject() -> str:
    """Backend-appropriate name for the thing that's wedged."""
    return 'RS-485 bridge' if _active_backend == 'rs485bridge' else 'AquaConnect box'


def _wedge_recovery_hint() -> str:
    """Backend-appropriate recovery instruction shown in errors/logs."""
    if _active_backend == 'rs485bridge':
        return 'check the pad Pi / pool-bridge daemon (it clears itself once reachable)'
    return 'power-cycle the AquaConnect box'

def _immediate_wedge_probe() -> None:
    """Spawn a background daemon thread to probe wedge state right now.

    Called after any HomeKit-driven write fails so the bridge_wedged flag
    updates within seconds rather than waiting for the 300s/30s probe loop.
    """
    # Direct-serial bridge owns its own reachability signal in the poll loop —
    # the AquaConnect canary (which declares a wedge + cooldown on one failed
    # probe) does not apply and would re-arm the sticky wedge we're removing.
    if _active_backend == 'rs485bridge':
        return
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
# Defaults to backend.json next to this script; override with SIDECAR_CONFIG so
# a test instance can use an isolated config (and never read/clobber the
# production one at /opt/pool-sidecar/backend.json).
_BACKEND_CONFIG_PATH = os.environ.get('SIDECAR_CONFIG') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'backend.json')
# Which backend is live in this process (set in main()).
_active_backend: Optional[str] = None

# UI config mirrored from the Homebridge plugin (POST /config/ui): which
# circuits the user enabled and any display-label overrides. The web cockpit
# reads these from /status so it shows the same switches/labels as HomeKit.
_ui_circuits: list = []
_ui_circuit_labels: dict = {}


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


# ── Bus-state persistence ────────────────────────────────────────────────────
# Cache everything we read off the bus so the cockpit/HomeKit show last-known
# values immediately on restart (instead of '—' until the menu sweep finishes),
# and so we can later make the startup sweep conditional (only re-read what
# differs). A background thread flushes on change; the cache is loaded into
# `state` at startup before the prefetch runs.
_STATE_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'state_cache.json')
_STATE_CACHE_FLUSH_S = 30.0
_PERSIST_FIELDS = (
    'pool_temp', 'air_temp', 'spa_temp', 'salt_level',
    'chlorinator_percent', 'spa_chlorinator_percent',
    'pump_speed', 'spa_speed',
    'pool_setpoint_f', 'spa_setpoint_f',
    'pool_heater_enabled', 'spa_heater_enabled', 'heater_active',
    'valve_mode', 'vsp_active_slot',
    'vsp_slot_pct', 'circuits',   # dicts
)


def _snapshot_state_cache() -> dict:
    with state_lock:
        snap = {}
        for f in _PERSIST_FIELDS:
            v = getattr(state, f, None)
            snap[f] = dict(v) if isinstance(v, dict) else v
    return snap


def _load_state_cache() -> None:
    """Restore persisted bus state into `state` at startup (last-known values)."""
    global _cache_saved_at
    try:
        with open(_STATE_CACHE_PATH) as fh:
            snap = json.load(fh)
    except FileNotFoundError:
        return
    except Exception as e:
        log.warning('State cache load failed: %s', e)
        return
    _cache_saved_at = snap.get('_saved_at')
    n = 0
    with state_lock:
        for f in _PERSIST_FIELDS:
            if snap.get(f) is None:
                continue
            cur = getattr(state, f, None)
            if isinstance(cur, dict) and isinstance(snap[f], dict):
                incoming = snap[f]
                # JSON forces dict keys to strings; vsp_slot_pct is keyed by int
                # slot number in the live code, so restore int keys — otherwise
                # the dict ends up with both "1" and 1 and jsonify can't sort it.
                if f == 'vsp_slot_pct':
                    incoming = {(int(k) if str(k).isdigit() else k): v
                                for k, v in incoming.items()}
                cur.update(incoming)
            else:
                setattr(state, f, snap[f])
            n += 1
    log.info('Restored %d persisted state fields from %s', n, _STATE_CACHE_PATH)


def _state_cache_thread() -> None:
    """Flush the bus-state cache to disk when it changes, plus a ~60s heartbeat
    so `_saved_at` reflects how recently the sidecar was alive — startup uses
    that to decide whether the persisted values are fresh enough to skip the
    menu sweep."""
    last = None
    last_write = 0.0
    while True:
        time.sleep(_STATE_CACHE_FLUSH_S)
        snap = _snapshot_state_cache()
        now = time.time()
        if snap == last and (now - last_write) < 60:
            continue
        payload = dict(snap)
        payload['_saved_at'] = round(now)
        try:
            tmp = _STATE_CACHE_PATH + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump(payload, fh)
            os.replace(tmp, _STATE_CACHE_PATH)   # atomic
            last = snap
            last_write = now
        except Exception as e:
            log.debug('State cache save failed: %s', e)


# ── Temperature history ──────────────────────────────────────────────────────
# A rolling time-series of pool/spa/air temps for the cockpit chart. Two-tier
# retention: full 5-min detail for the last day, thinned to 15-min buckets
# beyond that, dropped after 90 days. Persisted so it survives restarts. Each
# sample is [epoch_s, pool, spa, air] (any temp may be null if not known).
_TEMP_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'temp_history.json')
_TEMP_SAMPLE_INTERVAL_S = 300.0        # 5 min live sampling
_TEMP_FINE_WINDOW_S = 86400            # keep full 5-min detail for the last day
_TEMP_COARSE_BUCKET_S = 900            # 15-min buckets beyond the fine window
_TEMP_RETENTION_S = 90 * 86400         # drop samples older than 90 days
_TEMP_HISTORY_MAX = 15000              # hard backstop (~8.8k expected at steady state)
_temp_history: list = []
_temp_history_lock = threading.Lock()


def _compact_history(hist: list, now: float) -> list:
    """Apply two-tier retention: keep every sample within the last day, thin to
    one-per-15-min beyond that, drop older than 90 days. Order-preserving and
    idempotent, so it can run on every append."""
    fine_cutoff = now - _TEMP_FINE_WINDOW_S
    drop_cutoff = now - _TEMP_RETENTION_S
    out = []
    last_bucket = None
    for s in hist:
        t = s[0]
        if t < drop_cutoff:
            continue
        if t >= fine_cutoff:
            out.append(s)                       # recent: full 5-min detail
        else:
            b = int(t // _TEMP_COARSE_BUCKET_S)  # aged: one per 15-min bucket
            if b != last_bucket:
                out.append(s)
                last_bucket = b
    return out

# When the state cache was last flushed (epoch), read back on load so startup
# can decide whether the persisted values are fresh enough to skip the sweep.
_cache_saved_at: Optional[float] = None


def _load_temp_history() -> None:
    global _temp_history
    try:
        with open(_TEMP_HISTORY_PATH) as fh:
            data = json.load(fh)
        if isinstance(data, list):
            with _temp_history_lock:
                _temp_history = _compact_history(data, time.time())[-_TEMP_HISTORY_MAX:]
            log.info('Restored %d temperature history samples', len(_temp_history))
    except FileNotFoundError:
        return
    except Exception as e:
        log.warning('Temp history load failed: %s', e)


def _temp_history_thread(now_fn=time.time) -> None:
    """Append a temp sample every interval and persist. Skips samples where all
    three temps are unknown (nothing to plot)."""
    while True:
        time.sleep(_TEMP_SAMPLE_INTERVAL_S)
        with state_lock:
            pool, spa, air = state.pool_temp, state.spa_temp, state.air_temp
        if pool is None and spa is None and air is None:
            continue
        sample = [round(now_fn()), pool, spa, air]
        with _temp_history_lock:
            _temp_history.append(sample)
            _temp_history[:] = _compact_history(_temp_history, sample[0])
            if len(_temp_history) > _TEMP_HISTORY_MAX:
                del _temp_history[:-_TEMP_HISTORY_MAX]
            snapshot = list(_temp_history)
        try:
            tmp = _TEMP_HISTORY_PATH + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump(snapshot, fh)
            os.replace(tmp, _TEMP_HISTORY_PATH)
        except Exception as e:
            log.debug('Temp history save failed: %s', e)


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

# Hayward Universal ColorLogic (UCL) light programs, in the panel's power-cycle
# order (1..17). 'show' = color-changing/moving, 'fixed' = static color — the
# only distinction reliably observable without color vision. Selected by the
# absolute reset procedure (full off -> N power-restores) via the pad daemon's
# /program. LIGHT_PROGRAM_OFFSET calibrates the count-to-program mapping if the
# panel's first-restore isn't program 1 (see /lights/programs + calibration).
LIGHT_PROGRAMS_POOL = [
    ('Voodoo Lounge', 'show'),
    ('Deep Blue Sea', 'fixed'),
    ('Royal Blue', 'fixed'),
    ('Afternoon Skies', 'fixed'),
    ('Aqua Green', 'fixed'),
    ('Emerald', 'fixed'),
    ('Cloud White', 'fixed'),
    ('Warm Red', 'fixed'),
    ('Flamingo', 'fixed'),
    ('Vivid Violet', 'fixed'),
    ('Sangria', 'fixed'),
    ('Twilight', 'show'),
    ('Tranquility', 'show'),
    ('Gemstone', 'show'),
    ('USA', 'show'),
    ('Mardi Gras', 'show'),
    ('Cool Cabaret', 'show'),
]

# Spa light = Pentair IntelliBrite 5G (on AUX_1). Absolute count per the
# IntelliBrite manual: from power-on, off/on N times selects mode N. 1-7 are
# color shows, 8-12 fixed colors. (13 Hold / 14 Recall are save/recall
# functions, not selectable modes, so not listed here.)
LIGHT_PROGRAMS_SPA = [
    ('SAm', 'show'),
    ('Party', 'show'),
    ('Romance', 'show'),
    ('Caribbean', 'show'),
    ('American', 'show'),
    ('California Sunset', 'show'),
    ('Royal', 'show'),
    ('Blue', 'fixed'),
    ('Green', 'fixed'),
    ('Red', 'fixed'),
    ('White', 'fixed'),
    ('Magenta', 'fixed'),
]

LIGHT_PROGRAMS_BY_BODY = {'pool': LIGHT_PROGRAMS_POOL, 'spa': LIGHT_PROGRAMS_SPA}
# Back-compat default (pool) for any caller that doesn't specify a body.
LIGHT_PROGRAMS = LIGHT_PROGRAMS_POOL


def _light_programs(body: str):
    """Program list for a body: Pentair (spa) vs Hayward UCL (pool)."""
    return LIGHT_PROGRAMS_BY_BODY.get(body, LIGHT_PROGRAMS_POOL)

# Which circuit each body's programmable light is on (pool light = LIGHTS,
# spa light = AUX_1). Power-cycle timing + count offset are calibratable and
# persisted in backend.json (key 'light_config').
LIGHT_CIRCUITS = {'pool': 'LIGHTS', 'spa': 'AUX_1'}
# The two lights select programs DIFFERENTLY (see docs/colorlogic-research.md):
#   spa  = Pentair IntelliBrite -> ABSOLUTE (off/on N times = program N).
#   pool = Hayward ColorLogic   -> RELATIVE (each off/on <10s = +1 from current),
#          with NO absolute color reset — so we track position and step
#          (target - current) mod count.
LIGHT_MECHANIC = {'pool': 'relative', 'spa': 'absolute'}


def _light_mechanic(body: str) -> str:
    return LIGHT_MECHANIC.get(body, 'absolute')
# Power-cycle timing is PER BODY — the pool (Hayward UCL) and spa (Pentair
# IntelliBrite) lights use different reset/pulse timing, so calibrating one must
# not disturb the other. Defaults; overridden by backend.json 'light_config'.
_LIGHT_CFG_DEFAULTS = {
    'offset': 0,        # daemon restore-count = program_number + offset
    'reset_ms': 2000,   # full-off hold that resets the light to baseline (~2s)
    'off_ms': 120,      # rapid off pulse between restores
    'on_ms': 120,       # rapid on dwell between restores
    'local': True,      # LOCAL_WIRED frames (replicate the physical keypad)
}
LIGHT_CFG_BY_BODY = {
    'pool': dict(_LIGHT_CFG_DEFAULTS),
    'spa':  dict(_LIGHT_CFG_DEFAULTS),
}
# Back-compat alias (pool) for any caller not yet body-aware.
LIGHT_CFG = LIGHT_CFG_BY_BODY['pool']


def _light_cfg(body: str) -> dict:
    """Power-cycle calibration for a body (pool vs spa)."""
    return LIGHT_CFG_BY_BODY.get(body, LIGHT_CFG_BY_BODY['pool'])

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
    def __init__(self, maxhist: int = 60, hub: 'Optional[FrameHub]' = None):
        self._lock = threading.Lock()
        self._latest: Optional[str] = None
        self._ts: float = 0.0
        self._event = threading.Event()
        self.history: deque = deque(maxlen=maxhist)
        # Optional per-backend FrameHub: every captured frame is republished here
        # for the live /stream feed. None = not streamed (e.g. simulation).
        self._hub = hub

    def text_updated(self, text: str) -> None:
        with self._lock:
            self._latest = text
            self._ts = time.time()
            self.history.append((self._ts, text))
        if self._hub is not None:
            self._hub.publish(_norm(text), raw=text)
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


# ---------------------------------------------------------------------------
# FrameHub — backend-agnostic live frame feed (§ parallel-backend work)
#
# Each backend (aquaconnect, rs485) owns one hub. As LCD frames arrive they are
# published here; the /stream SSE endpoints subscribe to get a continuous tail
# without polling — the same "watch the bus" model the AquaConnect frame-reader
# uses internally, now exposed as a first-class read surface. The hub name is
# the only backend-specific detail upstream ever sees; /stream (active backend)
# and /stream/<name> (a specific one) both resolve to a hub here.
# ---------------------------------------------------------------------------
class FrameHub:
    def __init__(self, name: str, maxlen: int = 200):
        self.name = name
        self._cond = threading.Condition()
        self._ring: deque = deque(maxlen=maxlen)
        self._seq = 0
        self.last_publish = 0.0

    def publish(self, text: str, raw: Optional[str] = None,
                extra: Optional[dict] = None) -> None:
        with self._cond:
            self._seq += 1
            frame = {'seq': self._seq, 'ts': round(time.time(), 3), 'text': text}
            if raw is not None:
                frame['raw'] = raw
            if extra:
                frame.update(extra)
            self._ring.append(frame)
            self.last_publish = frame['ts']
            self._cond.notify_all()

    def recent(self, limit: int = 50) -> list:
        with self._cond:
            return list(self._ring)[-limit:]

    def follow(self, timeout: float = 20.0):
        """Generator yielding each newly-published frame (live tail).

        Starts from the next frame after subscription. Yields None on idle
        timeout so the caller can emit an SSE heartbeat and detect disconnects.
        """
        with self._cond:
            last = self._seq
        while True:
            with self._cond:
                if self._seq <= last:
                    self._cond.wait(timeout=timeout)
                pending = [f for f in self._ring if f['seq'] > last]
                if pending:
                    last = pending[-1]['seq']
            if pending:
                yield from pending
            else:
                yield None


_frame_hubs: "dict[str, FrameHub]" = {}
_frame_hubs_lock = threading.Lock()


def _get_hub(name: str) -> FrameHub:
    with _frame_hubs_lock:
        h = _frame_hubs.get(name)
        if h is None:
            h = FrameHub(name)
            _frame_hubs[name] = h
        return h


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
# Minimum gap between ANY two requests (press or read). Empirically confirmed
# via nav-sweep (2026-06-20): 0.6s on all requests gives avg 15s/lap and ~2
# drops per 3-lap run; 0.9s (original) gave 27s/lap. Applying the gap to
# keypresses only (skipping reads) proved worse — 19 drops, 28s/lap — because
# the AquaConnect box itself needs the inter-request spacing, not just the panel.
_AC_MIN_GAP_S = 0.6

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
        self.lcd = LcdCapture(hub=_get_hub('aquaconnect'))
        self._http_lock = threading.Lock()   # serializes all socket access
        self._last_req = 0.0                  # ts of last request (gap enforcement)
        # Monotonic count of every HTTP request (press + read), for timing tests:
        # lets a benchmark measure total request volume per operation, validating
        # the frame-reader's N+1-requests-per-N-keys behavior.
        self._req_count = 0
        self._last_raw: Optional[str] = None  # last full body, for /debug calibration
        self._last_led: dict = {}
        self._poll_s = poll_s
        self._poll_stop = threading.Event()
        # Shared frame notification: signaled after every successful read so
        # send_nav_key can wait for confirmation instead of doing burst reads.
        self._frame_cond = threading.Condition()
        # Set by send_nav_key to wake the poll loop immediately after a keypress
        # so confirmation arrives in ~1 read latency rather than up to poll_s.
        self._read_wake = threading.Event()
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

        Use this everywhere we only want the current state (poll loop / frame
        reader). Use _post(code) only when we actually intend a keypress.
        """
        return self._request('Update Local Server&')

    def _request(self, body: str) -> Optional[str]:
        """Send a POST /WNewSt.htm with the given body and return the response."""
        self._req_count += 1
        now = time.time()
        wait = _AC_MIN_GAP_S - (now - self._last_req)
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
                # The AquaConnect box wraps highlighted/flashing values in HTML
                # (e.g. <span class="WBON">97°F</span>); strip the tags so the
                # raw markup doesn't leak into the displayed LCD text.
                txt = re.sub(r'<[^>]+>', '', ln)
                lcd_lines.append(html.unescape(txt.replace('&#176', '°')))
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
        """Send one navigation key and wait for the frame reader to confirm.

        Sends the keypress under _http_lock, then releases it and signals the
        frame reader to run immediately. Waits on _frame_cond for the next
        frame — the panel shows the confirmation state right away, so one read
        is enough. The frame reader (not this method) applies the frame to state,
        so self.lcd reflects the post-keypress screen before we return.
        """
        code = _AC_KEY_CODES.get(key_name.upper().replace('_', ''))
        if code is None:
            raise ValueError(f'No AquaConnect code for key: {key_name}')
        with self._http_lock:
            self._apply(self._post(code))
        # Wake the frame reader so it reads confirmation immediately rather than
        # waiting up to poll_s seconds.
        self._read_wake.set()
        with self._frame_cond:
            self._frame_cond.wait(timeout=3.0)

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
            before = canary_bit(self._led_line(self._read()))
            self._apply(self._post(code))   # press canary
            after = before
            attempts = 0
            for _ in range(retries):
                attempts += 1
                time.sleep(gap_s)
                body = self._read()
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
            # Reads use 'Update Local Server&' which carries no keypad-event
            # side-effect, so they are safe to interleave between nav keypresses.
            # _http_lock serializes access to the socket; nav holds it only for
            # the duration of each individual keypress, not the whole sequence.
            with self._http_lock:
                body = self._read()
            if body:
                self._apply(body)
                with self._frame_cond:
                    self._frame_cond.notify_all()
            # Sleep for poll_s, but wake early if send_nav_key signals us.
            self._read_wake.wait(timeout=self._poll_s)
            self._read_wake.clear()

    def stop(self) -> None:
        self._poll_stop.set()


# Sidecar key names -> aqualogic Keys enum names the pad daemon feeds to
# getattr(Keys, name). The sidecar uses compact names ('AUX2', 'HEATER1') and
# per-body mode names ('POOL'/'SPA'/'SPILLOVER') that all map to the single
# mode-cycle key. Anything not listed passes through upper-cased (MENU, RIGHT,
# LEFT, PLUS, MINUS, FILTER, LIGHTS already match the enum).
_BRIDGE_KEY_ALIASES = {
    'AUX1': 'AUX_1', 'AUX2': 'AUX_2', 'AUX3': 'AUX_3',
    'AUX4': 'AUX_4', 'AUX5': 'AUX_5', 'AUX6': 'AUX_6',
    'HEATER1': 'HEATER_1', 'HEATER2': 'HEATER_2',
    'VALVE3': 'VALVE_3', 'VALVE4': 'VALVE_4',
    'POOL': 'POOL_SPA', 'SPA': 'POOL_SPA', 'SPILLOVER': 'POOL_SPA',
}


def _bridge_key_name(name: str) -> str:
    """Normalize a sidecar key name to the aqualogic Keys enum name the daemon
    expects. Verified against the daemon's /keys endpoint."""
    n = name.upper()
    return _BRIDGE_KEY_ALIASES.get(n.replace('_', ''), n)


class RS485BridgeBackend:
    """Navigation backend that drives the panel through the pad-Pi RS-485 smart
    bridge (sidecar/rs485_bridge.py) over HTTP/Tailscale.

    Exposes the same surface the MenuNavigator and _ac_set_circuit already use
    for AquaConnect — an `lcd` LcdCapture the navigator reads, and
    `send_nav_key()` — plus a background poll that maps the daemon's decoded
    /state snapshot straight into the global PoolState. The daemon owns serial
    timing and does the aqualogic decode (100% keypress landing, no box to
    wedge), so this class is a thin, stateless HTTP client.
    """

    def __init__(self, host: str, port: int = 8899, token: Optional[str] = None,
                 poll_s: float = 0.5):
        self._base = f'http://{host}:{port}'
        self._token = token or None
        # Publish to the hub named after the ACTIVE backend ('rs485bridge') so
        # the cockpit's /stream (which reads _get_hub(_active_backend)) shows the
        # panel LCD. Using 'rs485' here left the Panel Display blank in bridge mode.
        self.lcd = LcdCapture(hub=_get_hub('rs485bridge'))
        self._http_lock = threading.Lock()   # parity: nav-sweep/debug serialize here
        self._req_count = 0
        self._last_led: dict = {}
        self._last_raw = None
        self._poll_s = poll_s
        self._poll_stop = threading.Event()
        self._frame_cond = threading.Condition()
        self._read_wake = threading.Event()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name='rs485bridge-poll')
        self._poll_thread.start()

    # ── HTTP ──────────────────────────────────────────────────────────────────
    def _headers(self, extra=None) -> dict:
        h = dict(extra or {})
        if self._token:
            h['Authorization'] = f'Bearer {self._token}'
        return h

    def _get_state(self) -> Optional[dict]:
        self._req_count += 1
        req = urllib.request.Request(self._base + '/state', headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except Exception as e:
            log.debug('rs485bridge /state error: %s', e)
            return None

    def _post_key(self, key_name: str, settle: float = 0.35) -> Optional[dict]:
        self._req_count += 1
        body = json.dumps({'key': key_name, 'settle': settle}).encode()
        req = urllib.request.Request(
            self._base + '/key', data=body,
            headers=self._headers({'Content-Type': 'application/json'}))
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read())
        except Exception as e:
            log.warning('rs485bridge /key(%s) error: %s', key_name, e)
            return None

    def select_program(self, key: str, n: int, reset_ms: float,
                       off_ms: float, on_ms: float,
                       start_on: Optional[bool] = None,
                       local: bool = False) -> Optional[dict]:
        """Drive the daemon's /program (absolute ColorLogic select) for a light
        circuit. The daemon does the full reset + N-restore power-cycle; this is
        a thin pass-through. `start_on` is our settled poll of the circuit so the
        daemon's reset is deterministic (not a racy read). Longer HTTP timeout
        since the sequence itself takes several seconds."""
        self._req_count += 1
        payload = {'key': key, 'n': n, 'reset_ms': reset_ms,
                   'off_ms': off_ms, 'on_ms': on_ms, 'local': bool(local)}
        if start_on is not None:
            payload['start_on'] = bool(start_on)
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._base + '/program', data=body,
            headers=self._headers({'Content-Type': 'application/json'}))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            log.warning('rs485bridge /program(%s n=%s) error: %s', key, n, e)
            return None

    def cycle(self, key: str, count: int, off_ms: float, on_ms: float) -> Optional[dict]:
        """Drive the daemon's /cycle: `count` blind off/on power-cycles of a
        toggle circuit (no reset). For the Hayward pool light's RELATIVE advance
        — each off/on (<10s) steps one program. Assumes the light is already ON."""
        self._req_count += 1
        payload = {'key': key, 'count': int(count), 'off_ms': off_ms, 'on_ms': on_ms}
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._base + '/cycle', data=body,
            headers=self._headers({'Content-Type': 'application/json'}))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            log.warning('rs485bridge /cycle(%s x%s) error: %s', key, count, e)
            return None

    # ── State mapping ─────────────────────────────────────────────────────────
    def _apply(self, snap: Optional[dict]) -> None:
        """Map a daemon /state snapshot into self.lcd + the global PoolState.

        Only the broadcast fields the daemon decodes (temps, salt, chlorinator,
        pump, circuits, valve mode, heater relay) are written here; menu-only
        values (setpoints, VSP slot speeds) are read by the prefetch navigating
        menus, exactly as in AquaConnect mode."""
        if not snap:
            return
        self._last_raw = snap
        lcd = snap.get('lcd')
        if lcd:
            self.lcd.text_updated(lcd)
        with state_lock:
            state.connected = bool(snap.get('connected'))
            state.last_update = time.time()
            for f in ('pool_temp', 'air_temp', 'spa_temp', 'salt_level',
                      'pump_speed', 'chlorinator_percent'):
                v = snap.get(f)
                if v is not None:
                    setattr(state, f, v)
            for name, val in (snap.get('circuits') or {}).items():
                state.circuits[name] = bool(val)
            if snap.get('heater_active') is not None:
                state.heater_active = bool(snap['heater_active'])
            su = _pump_startup_from_lcd(lcd)
            if su is not None:
                state.pump_startup = su
            vm = snap.get('valve_mode')
            if vm is not None:               # None = current frame isn't a mode
                state.valve_mode = vm        # screen; keep last-known otherwise

    # ── Public navigator surface ──────────────────────────────────────────────
    def send_nav_key(self, key_name: str) -> None:
        """Send one key through the daemon and wait for a settled confirmation.

        Mirrors AquaConnectBackend.send_nav_key: land the key, apply the daemon's
        post-press snapshot (so self.lcd + state reflect it), then wake the poll
        loop and wait one frame for a settled re-read — the layer above
        (MenuNavigator._send / _ac_set_circuit) decides whether the change was
        meaningful and re-presses if not."""
        self._apply(self._post_key(_bridge_key_name(key_name)))
        self._read_wake.set()
        with self._frame_cond:
            self._frame_cond.wait(timeout=3.0)

    def probe_wedge(self, retries: int = 3, gap_s: float = 1.0) -> dict:
        """Direct serial has no AquaConnect box to wedge; the daemon's own
        connectivity is the liveness signal. Kept for API parity with the wedge
        machinery so shared control paths work unchanged."""
        snap = self._get_state()
        return {'alive': bool(snap and snap.get('connected')), 'attempts': 1}

    # ── Background state poll ──────────────────────────────────────────────────
    def _poll_loop(self) -> None:
        # Reachability-driven offline flag. For direct serial the daemon's
        # reachability IS the liveness signal: a healthy /state = online, a run
        # of failed polls = offline. This OWNS state.bridge_wedged for the
        # bridge (nothing else sets it now) — no power-cycle cooldown, no command
        # blocking, self-clearing on the first good poll. A weak-Wi-Fi blip shows
        # a brief 'offline' that heals itself instead of a sticky wedge.
        fails = 0
        while not self._poll_stop.is_set():
            try:
                snap = self._get_state()
                if snap:
                    fails = 0
                    self._apply(snap)
                    if snap.get('connected') and state.bridge_wedged:
                        _record_command_success()   # clears the offline flag
                    with self._frame_cond:
                        self._frame_cond.notify_all()
                else:
                    fails += 1
                    # ~3 misses (poll interval + timeouts ≈ several seconds) before
                    # flagging, so a single dropped packet doesn't flap the banner.
                    if fails >= _BRIDGE_OFFLINE_MISSES and not state.bridge_wedged:
                        with state_lock:
                            state.bridge_wedged = True
                            state.wedge_detected_at = None   # NO power-cycle cooldown
                        log.warning('RS-485 bridge unreachable (%d consecutive polls) — '
                                    'marking offline; self-clears on reconnect', fails)
            except Exception as e:  # noqa: BLE001
                # A malformed snapshot must NEVER kill this thread — a dead poll
                # loop was what left the flag stuck with no way to self-heal.
                log.debug('rs485bridge poll loop error (continuing): %s', e)
            self._read_wake.wait(timeout=self._poll_s)
            self._read_wake.clear()

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
    ('chlorinator_percent',     re.compile(r'Pool Chlorinator\s+(\d+)\s*%', re.I)),
    ('spa_chlorinator_percent', re.compile(r'Spa Chlorinator\s+(\d+)\s*%', re.I)),
    ('pump_speed',              re.compile(r'Filter Speed\s+(\d+)\s*%', re.I)),
    ('spa_speed',               re.compile(r'Spa Speed\s+(\d+)\s*%', re.I)),
    # The active VSP slot shows up two ways: the steady idle scroll line
    # 'Filter Speed 50% Speed2', and the brief slot-selection window that opens
    # when the filter starts, 'Filter On:Spd2 +/- to change' (the WBON <span>
    # around 'Spd2' is stripped before we get here). Parse both.
    ('vsp_active_slot',         re.compile(r'Filter Speed\s+\d+\s*%\s+Speed(\d)', re.I)),
    ('vsp_active_slot',         re.compile(r'Filter On:Spd(\d)', re.I)),
)


# Heater enable state appears in the idle scroll ('Heater1 Auto Control' /
# 'Heater1 Manual Off') and in the Settings menu with a Pool/Spa prefix. The
# scroll form has no prefix, so it applies to whichever heater the active valve
# mode selects. (When enabled, the menu shows the setpoint °F instead of 'Auto
# Control', so this only flips the flag on the explicit Auto/Manual screens.)
_AC_HEATER_STATE_RE = re.compile(
    r'(Pool |Spa )?Heater1\s+(Auto Control|Manual Off)', re.I)

# Passive capture of MENU-only values whenever the panel displays them — whether
# from our own nav OR the owner changing them by hand at the panel (the physical
# LCD shows the menu, and our poll reads it). The setpoint °F only appears when
# the heater is enabled, so seeing it also confirms Auto. Slot label is
# 'Filter Speed1 90%' (digit adjacent, so it never collides with the scroll's
# 'Filter Speed 50%' pump-speed reading).
_AC_HEATER_SETPOINT_RE = re.compile(r'(Pool|Spa) Heater1[^0-9]*(\d{2,3})\s*\xb0?\s*F', re.I)
_AC_VSP_SLOT_RE = re.compile(r'Filter Speed([1-4])[^0-9]+(\d{1,3})\s*%', re.I)

# Post-power-up pump prime: the panel runs the filter at 100% for a "start
# delay" ("Filter Speed 100% St dly M:SS") before resuming the schedule. Detect
# it so the cockpit shows "100% startup" instead of mislabeling the off-slot
# 100% as a heater/override speed. Set/cleared only on filter-speed frames: a
# filter-speed frame with "St dly" -> priming; one without -> not priming.
_AC_FILTER_SPEED_RE = re.compile(r'Filter Speed\s+\d+\s*%', re.I)
_AC_STARTUP_RE = re.compile(r'St\s*dly', re.I)


def _pump_startup_from_lcd(lcd: str) -> Optional[bool]:
    """Return True/False if `lcd` is a filter-speed frame (priming or not), else
    None to leave the flag unchanged (this frame doesn't speak to it)."""
    if not lcd or not _AC_FILTER_SPEED_RE.search(lcd):
        return None
    return bool(_AC_STARTUP_RE.search(lcd))

# Fault/alert phrases the panel interleaves into the status scroll. Matched
# case-insensitively as substrings. Each match records a last-seen time; a fault
# is considered active until it stops appearing for _FAULT_TTL_S (it resolves by
# ceasing to scroll). Surfaced in /status['faults'] for the cockpit banner (not
# HomeKit). Curated for this AquaLogic/ProLogic panel; extend as new alerts show.
_FAULT_PHRASES = (
    'Check System', 'Inspect Cell', 'No Flow', 'Check Flow', 'Low Salt',
    'High Salt', 'Very Low Salt', 'Check PCB', 'Cold Water', 'Sensor Error',
    'Service Mode', 'Check AC', 'Comm Error', 'Low Temp',
)
_FAULT_TTL_S = 300.0
_active_faults: dict = {}          # phrase -> last_seen epoch
_faults_lock = threading.Lock()

# Discovery: frames that LOOK like an alert (contain one of these words) but are
# not a known reading or known fault get logged + persisted so we can learn this
# panel's exact alert wording and promote real ones into _FAULT_PHRASES.
_FAULT_HINT_RE = re.compile(
    r'\b(check|inspect|error|fault|fail|alarm|alert|service|warning|replace|'
    r'freeze|sensor|no flow|clean|low|high)\b', re.I)
_FAULT_CANDIDATES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'fault_candidates.json')
_fault_candidates: dict = {}       # frame text -> {first, last, count}
_fault_cand_lock = threading.Lock()


def _is_known_reading(lcd: str) -> bool:
    """True if the frame is a recognized status reading or heater-state screen."""
    if _AC_HEATER_STATE_RE.search(lcd):
        return True
    return any(pat.search(lcd) for _f, pat in _AC_SCROLL_PATTERNS)


def _load_fault_candidates() -> None:
    global _fault_candidates
    try:
        with open(_FAULT_CANDIDATES_PATH) as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _fault_candidates = data
    except FileNotFoundError:
        return
    except Exception as e:
        log.warning('Fault candidates load failed: %s', e)


def _record_fault_candidate(frame: str, now: float) -> None:
    if not frame:
        return
    is_new = False
    with _fault_cand_lock:
        e = _fault_candidates.get(frame)
        if e is None:
            _fault_candidates[frame] = {'first': now, 'last': now, 'count': 1}
            is_new = True
        else:
            e['last'] = now
            e['count'] = e.get('count', 0) + 1
        snapshot = dict(_fault_candidates)
    if is_new:
        log.warning('FAULT-CANDIDATE (unknown alert-like frame): %r', frame)
        try:
            tmp = _FAULT_CANDIDATES_PATH + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump(snapshot, fh, indent=2)
            os.replace(tmp, _FAULT_CANDIDATES_PATH)
        except Exception as e:
            log.debug('Fault candidates save failed: %s', e)


def _check_faults(lcd: str) -> None:
    """Record any known fault phrase with a timestamp; log unknown alert-like
    frames as candidates for the discovery backlog."""
    low = lcd.lower()
    now = time.time()
    hits = [p for p in _FAULT_PHRASES if p.lower() in low]
    if hits:
        with _faults_lock:
            for p in hits:
                _active_faults[p] = now
        return  # known fault — no need to flag as a candidate
    # Discovery: alert-looking but unrecognized frame → log for review.
    if _FAULT_HINT_RE.search(lcd) and not _is_known_reading(lcd):
        _record_fault_candidate(lcd.strip(), now)


def _current_faults() -> list:
    """Faults seen within the TTL window (i.e. still scrolling = still active)."""
    cutoff = time.time() - _FAULT_TTL_S
    with _faults_lock:
        return sorted(p for p, ts in _active_faults.items() if ts >= cutoff)


def _apply_ac_scroll_to_state(lcd: str) -> None:
    """Pull numeric readings + heater enable out of a scroll/menu LCD screen."""
    _check_faults(lcd)
    with state_lock:
        for field, pat in _AC_SCROLL_PATTERNS:
            m = pat.search(lcd)
            if m:
                setattr(state, field, int(m.group(1)))
                state.last_update = time.time()
        su = _pump_startup_from_lcd(lcd)
        if su is not None:
            state.pump_startup = su
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
        # Menu-only values captured passively when their frame is on screen.
        sm = _AC_HEATER_SETPOINT_RE.search(lcd)
        if sm:
            sp = int(sm.group(2))
            if 40 <= sp <= 110:   # sanity-bound a real setpoint
                if sm.group(1).lower() == 'spa':
                    state.spa_setpoint_f = sp
                    state.spa_heater_enabled = True
                else:
                    state.pool_setpoint_f = sp
                    state.pool_heater_enabled = True
                state.last_update = time.time()
        vm = _AC_VSP_SLOT_RE.search(lcd)
        if vm:
            pct = int(vm.group(2))
            if 0 <= pct <= 100:
                state.vsp_slot_pct[int(vm.group(1))] = pct
                state.last_update = time.time()


def _apply_ac_led_to_state(led: dict) -> None:
    """Fold decoded AquaConnect LED state into the shared PoolState (§13.2)."""
    with state_lock:
        # Active body/valve mode — also mirror into circuits so the plugin's
        # status poll sees circuits['POOL']/circuits['SPA'] without needing
        # to know about valve_mode separately.
        if led.get('pool_mode') in ('on', 'blink'):
            state.valve_mode = 'pool'
            state.circuits['POOL'] = True
            state.circuits['SPA'] = False
        elif led.get('spa_mode') in ('on', 'blink'):
            state.valve_mode = 'spa'
            state.circuits['POOL'] = False
            state.circuits['SPA'] = True
        # Equipment on/off → circuits dict (absent stays out of the map)
        for name, key in (('filter', 'FILTER'), ('lights', 'LIGHTS'),
                          ('aux1', 'AUX_1'), ('aux2', 'AUX_2')):
            st = led.get(name)
            if st in ('on', 'off', 'blink'):
                state.circuits[key] = (st != 'off')
        # Heater LED: 'on'/'blink' means the relay is actually firing right now.
        # HEATER_AUTO_MODE (armed/Auto) overwrites circuits['HEATER_1'] later in
        # on_change, so capture the relay bit separately here as heater_active.
        heater_st = led.get('heater')
        if heater_st in ('on', 'off', 'blink'):
            state.heater_active = (heater_st != 'off')
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


# RS-485 observe-only listener (parallel-backend work). When the active backend
# is AquaConnect, this runs alongside it: it streams frames into the 'rs485'
# FrameHub and parses an ISOLATED state snapshot (never the global `state`), so
# the two backends never stomp each other. It sends no keys except when a
# benchmark explicitly drives _rs485_observer.nav. Promoting this observer to a
# co-equal active backend later (option 2) only needs its snapshot to also
# answer /status/rs485 — the streaming/nav plumbing is already here.
_rs485_observer: Optional[SimpleNamespace] = None  # (aq, panel, nav, lcd) when up
_rs485_obs_state: dict = {}
_rs485_obs_lock = threading.Lock()


def rs485_observer_thread(host: str, port: int) -> None:
    global _rs485_observer

    from aqualogic.core import AquaLogic
    from aqualogic.states import States
    from aqualogic.keys import Keys

    _install_key_burst(AquaLogic)
    obs_lcd = LcdCapture(hub=_get_hub('rs485'))
    smap = {n: getattr(States, n) for n in CIRCUIT_NAMES}

    def on_change(aq) -> None:
        snap: dict = {}
        try:
            snap['pool_temp'] = _read_property(aq, 'pool_temp')
            snap['air_temp'] = _read_property(aq, 'air_temp')
            snap['spa_temp'] = _read_property(aq, 'spa_temp')
            snap['salt_level'] = _read_property(aq, 'salt_level')
            snap['chlorinator_percent'] = _read_property(aq, 'pool_chlorinator')
            snap['pump_speed'] = _read_property(aq, 'pump_speed')
            circ: dict = {}
            for name, s in smap.items():
                try:
                    circ[name] = bool(aq.get_state(s))
                except Exception:
                    pass
            snap['circuits'] = circ
        except Exception:
            pass
        with _rs485_obs_lock:
            _rs485_obs_state.clear()
            _rs485_obs_state.update(snap)
            _rs485_obs_state['connected'] = True
            _rs485_obs_state['last_update'] = time.time()

    while True:
        try:
            log.info('RS-485 OBSERVER (observe-only) connecting to %s:%s', host, port)
            aq = AquaLogic(web_port=0)
            aq._web = obs_lcd
            aq.connect(host, port)
            try:
                import socket as _socket
                aq._socket.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
            except Exception as e:
                log.warning('Observer: could not set TCP_NODELAY: %s', e)
            obs_panel = RealPanel(aq, States, Keys)
            obs_nav = MenuNavigator(obs_panel, obs_lcd)
            _rs485_observer = SimpleNamespace(
                aq=aq, panel=obs_panel, nav=obs_nav, lcd=obs_lcd)
            aq.process(on_change)        # blocks until the connection drops
        except Exception as e:
            log.error('RS-485 observer lost: %s', e)
        finally:
            _rs485_observer = None
            with _rs485_obs_lock:
                _rs485_obs_state['connected'] = False
        log.info('RS-485 observer reconnecting in 5 seconds...')
        time.sleep(5)


def panel_thread(host: str, port: int) -> None:
    global panel

    from aqualogic.core import AquaLogic
    from aqualogic.states import States
    from aqualogic.keys import Keys

    # Always install: even at burst=1 we need the keep-alive window targeting
    # (predelay) and the get_state patch that tolerates raw send_key frames.
    _install_key_burst(AquaLogic)

    # In RS-485-active mode the global lcd carries the panel's frames; route them
    # into the 'rs485' hub so /stream works the same as it does for AquaConnect.
    lcd._hub = _get_hub('rs485')

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
            # circuits['HEATER_1'] = armed/Auto mode (not the firing relay).
            # heater_active is set by _apply_ac_led_to_state from the LED field.
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
    # Seconds to wait for the frame to change after a press before treating it
    # as dropped and re-pressing. AquaConnect confirms in ~1.3s, so 3.0 gives a
    # >2x margin while recovering dropped presses ~1s faster than the old 4.0.
    # Benchmarks/taptests still override this explicitly when probing the lossy
    # RS-485 path.
    _KEY_TIMEOUT = 3.0   # seconds to wait for the frame to change after a press
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

    # Reverse direction for ring navigation, used by the overshoot back-up
    # recovery in _press_until. Only RIGHT/LEFT are reversible; PLUS/HEATER_1
    # etc. are actions, not ring moves, so they have no opposite here.
    _OPPOSITE = {'RIGHT': 'LEFT', 'LEFT': 'RIGHT'}

    def _press_until(self, key: str, ok, budget: int, what: str) -> str:
        """Press `key` until ok(normalized_text) is True, re-pressing on misses.

        Each landed press should advance one menu position. If _send times out
        (key dropped or value-flash stuck), we re-press. We stop the instant
        the target appears, so this never overshoots a distinct, named target.

        Special case for RIGHT: when stuck on the same item for two consecutive
        presses, send an extra RIGHT — the panel may have the value cursor
        selected (flashing), requiring one RIGHT to dismiss before another to
        advance.

        Overshoot back-up: a dropped-press flagged late can double-advance and
        skip the target, leaving it just BEHIND us. On a RIGHT/LEFT walk, before
        failing (guard abort or budget exhaustion) we press the opposite
        direction a few times to catch a skipped target — far cheaper than the
        old wrap-all-the-way-around behavior.
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
            # Foreign-submenu guard: if we're pressing RIGHT inside what should
            # be the Settings Menu ring but land on an item that doesn't belong
            # there (e.g. 'Wireless Channel:', 'Diagnostics'), stop pressing
            # further. But first try backing up — an overshoot may have skipped
            # the target and walked us off the end of the expected items.
            if key == 'RIGHT' and txt and txt[0:1].isupper() and not self._in_settings(txt):
                backed = self._press_back(key, ok, what)
                if backed is not None:
                    return backed
                raise RuntimeError(
                    f'Navigation left Settings Menu; at {txt!r} (expected {what}); aborting')
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
        # Budget exhausted walking one way — the target may have been skipped by
        # an overshoot and be sitting just behind us; try the other direction.
        backed = self._press_back(key, ok, what)
        if backed is not None:
            return backed
        raise RuntimeError(f'Could not reach {what}; stuck at {self._lcd.text()!r}')

    def _press_back(self, key: str, ok, what: str, budget: int = 3) -> Optional[str]:
        """Overshoot recovery: press the opposite ring direction up to `budget`
        times to catch a target a dropped-press double-advance skipped past.

        Only meaningful for RIGHT/LEFT walks (returns None otherwise, so the
        caller falls through to its original error). Stops the instant `ok`
        matches, so it can't itself overshoot. Worst case adds a few presses
        before the caller gives up.
        """
        opp = self._OPPOSITE.get(key)
        if opp is None:
            return None
        for _ in range(budget):
            txt = self._send(opp)
            if ok(txt):
                log.info('Overshoot recovery: backed up %s to reach %s', opp, what)
                return txt
        return None

    def _step_to(self, parser, target: int, up_key: str, down_key: str,
                 budget: int, what: str) -> int:
        """Drive a numeric setting to `target`, re-reading after every press.

        `parser` extracts the current value from the LCD frame; we read it
        through the value-flash with _read_value so a blanked frame never
        reads as None mid-step. Robust to dropped presses (value unchanged ->
        re-press) and overshoot (direction chosen fresh each iteration);
        converges because we stop on equality.
        """
        stalled = 0
        prev = None
        for _ in range(budget):
            cur = self._read_value(parser)
            if cur is None:
                raise RuntimeError(f'Cannot read {what} value at {self._lcd.text()!r}')
            if cur == target:
                return cur
            # Detect a hardware floor/ceiling: if two consecutive presses in the
            # same direction don't move the value, the panel won't go further.
            # Stop here and accept the clamped value rather than burning the
            # whole budget hammering a limit.
            if prev is not None and cur == prev:
                stalled += 1
                if stalled >= 2:
                    log.info('%s clamped at %s (target %s unreachable)', what, cur, target)
                    return cur
            else:
                stalled = 0
            prev = cur
            self._send(up_key if target > cur else down_key)
        cur = self._read_value(parser)
        if cur != target:
            raise RuntimeError(f'Could not set {what} to {target}; at {cur} ({self._lcd.text()!r})')
        return cur

    # Known Settings Menu item prefixes — any RIGHT-landed frame that does NOT
    # start with one of these is a foreign submenu (Diagnostics, Network, etc.)
    # and we abort immediately rather than pressing further into it.
    _SETTINGS_ITEM_PREFIXES = (
        'Settings Menu', 'Default Menu', 'Pool Heater', 'Spa Heater',
        'Pool Chlorinator', 'Spa Chlorinator', 'Super Chlorinate',
        'Pool Setpoint', 'Spa Setpoint',
        'VSP Speed', 'Filter Speed', 'Spa Speed', 'Filter Pump', 'Cleaner Pump',
        'Light Show', 'Color Swim', 'Water Feature',
        'Delay Cancel', 'Freeze Protect', 'Valve Delay',
        'Clock', 'Date', 'Time',
    )

    def _in_settings(self, txt: str) -> bool:
        """True if the current frame looks like a normal Settings Menu item."""
        return any(txt.startswith(p) for p in self._SETTINGS_ITEM_PREFIXES)

    # MENU cycles through the top-level menus in a ring:
    #   Default → Settings → Timers → Diagnostic → Configuration(locked) → …
    # So 'Settings Menu' is always reachable by pressing MENU until it appears.
    # With ~70% keypress drop on the WiFi bridge, a landed press may overshoot
    # Settings (e.g. land on Timers); the budget must cover several full ring
    # traversals so we cycle back around to Settings rather than stranding.
    _ANCHOR_MENU_MAX = 30

    def _anchor(self) -> None:
        """Drive MENU until the normalized frame is exactly 'Settings Menu'.

        MENU walks the top-level menu ring (see _ANCHOR_MENU_MAX); we stop the
        instant 'Settings Menu' appears. A generous budget tolerates dropped
        presses and overshoot without straying into any submenu's live settings.

        If the panel is in the status cycle the display changes on its own every
        few seconds — a MENU press that lands looks identical to a spontaneous
        cycle advance. We first wait for two consecutive identical frames
        (≤1.5s apart) to confirm the display is static (i.e. we are inside a
        menu, not the status cycle) before starting to press MENU. If we are
        already at 'Settings Menu', skip immediately.

        After landing on the header, wait _POST_MENU_SETTLE_S: the panel needs
        ~300ms before it will accept RIGHT.
        """
        # Fast path: already there.
        if self._lcd.text() == self._SETTINGS_HDR:
            time.sleep(self._POST_MENU_SETTLE_S)
            return

        # Wait up to 6s for the display to stop cycling (two identical reads).
        # If the panel is in a menu the display is already static so this
        # returns immediately. If it's in the status cycle it settles once the
        # current item holds for one read interval (~1.5s max).
        deadline = time.time() + 6.0
        prev = self._lcd.text()
        while time.time() < deadline:
            self._lcd._event.clear()
            self._lcd._event.wait(min(1.5, max(0.0, deadline - time.time())))
            cur = self._lcd.text()
            if cur == prev and cur:
                break  # display is static — we're in a menu
            prev = cur

        self._press_until('MENU', lambda t: t == self._SETTINGS_HDR,
                          self._ANCHOR_MENU_MAX, self._SETTINGS_HDR)
        time.sleep(self._POST_MENU_SETTLE_S)

    # Status-cycle prefixes — any of these means we're back in the default display.
    _STATUS_PREFIXES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
                        'Saturday', 'Sunday', 'Pool Temp', 'Air Temp', 'Spa Temp',
                        'Pool Chlorinator', 'Spa Chlorinator', 'Salt Level',
                        'Heater1', 'Filter Speed', 'Filter On')

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

    def _restore_heater_off(self, label: str) -> str:
        """Toggle the currently-selected heater item back to Manual Off.

        Called after a read pressed PLUS (which enabled the heater). Presses
        HEATER_1 only while the frame is NOT yet Manual Off, re-reading after
        each press so a dropped toggle simply re-presses and a landed toggle
        stops immediately (never double-toggles back on). Returns the final
        frame. Logs an error — but does not raise — if it cannot confirm Manual
        Off, so a read never leaves the heater silently enabled.
        """
        _RESTORE_MAX = 10
        txt = self._lcd.text()
        for _ in range(_RESTORE_MAX):
            if 'Manual Off' in txt:
                return txt
            txt = self._send('HEATER_1')
        if 'Manual Off' not in txt:
            log.error('Heater restore FAILED for %s — heater left ENABLED after '
                      'read; manual intervention may be needed (frame=%r)',
                      label, txt)
        return txt

    def _enable_heater(self, label: str) -> str:
        """Toggle the selected heater item ON (out of Manual Off) via the
        HEATER_1 switch ONLY — never +/-. Presses HEATER_1 while still Manual
        Off, re-reading after each press so a dropped toggle re-presses. Enabling
        reveals the stored °F. Mirror of _restore_heater_off."""
        _MAX = 10
        txt = self._lcd.text()
        for _ in range(_MAX):
            if 'Manual Off' not in txt:
                return txt
            txt = self._send('HEATER_1')
        if 'Manual Off' in txt:
            log.error('Heater enable FAILED for %s (still Manual Off, frame=%r)',
                      label, txt)
        return txt

    def read_heater(self, which: str) -> dict:
        """Read a heater item's state WITHOUT changing it — purely passive,
        never presses +/- or the HEATER_1 switch.

        The stored setpoint is only visible when the heater is enabled (Auto).
        When the item shows 'Manual Off' the panel displays no temperature, and
        the rule is to never scroll/toggle while Off — so we report enabled=False
        and KEEP the last-known setpoint rather than toggling the heater on just
        to peek at it. The setpoint refreshes whenever the heater is on.
        """
        if which not in ('pool', 'spa'):
            raise ValueError('which must be "pool" or "spa"')
        label = self._HEATER_LABEL[which]
        try:
            with _nav_lock:
                self._anchor()
                txt = self._press_until('RIGHT', lambda t: label in t,
                                        self._NAV_MAX, label)
                enabled = 'Manual Off' not in txt
                setpoint_f = self._degf(txt) if enabled else None
                with state_lock:
                    if which == 'pool':
                        state.pool_heater_enabled = enabled
                        if setpoint_f is not None:
                            state.pool_setpoint_f = setpoint_f
                    else:
                        state.spa_heater_enabled = enabled
                        if setpoint_f is not None:
                            state.spa_setpoint_f = setpoint_f
                return {'which': which, 'enabled': enabled,
                        'setpoint_f': setpoint_f, 'raw': txt}
        finally:
            self.fast_exit()

    def _read_heater_in_menu(self, read_which: str, direction: str) -> Optional[dict]:
        """Passively read a heater item while ALREADY in the Settings menu
        (caller holds _nav_lock and is anchored). Navigates `direction` to the
        item and captures its setpoint if enabled — never toggles. Used to grab
        the OTHER body's target opportunistically whenever we're in the menu for
        an explicit heater action, so both setpoints populate from one trip.
        Best-effort: returns None on failure.
        """
        label = self._HEATER_LABEL[read_which]
        try:
            txt = self._press_until(direction, lambda t: label in t,
                                    self._NAV_MAX, label)
            enabled = 'Manual Off' not in txt
            setpoint_f = self._degf(txt) if enabled else None
            with state_lock:
                if read_which == 'pool':
                    state.pool_heater_enabled = enabled
                    if setpoint_f is not None:
                        state.pool_setpoint_f = setpoint_f
                else:
                    state.spa_heater_enabled = enabled
                    if setpoint_f is not None:
                        state.spa_setpoint_f = setpoint_f
            return {'which': read_which, 'enabled': enabled, 'setpoint_f': setpoint_f}
        except Exception as e:
            log.debug('opportunistic %s-heater read failed: %s', read_which, e)
            return None

    def _read_other_heater(self, primary: str) -> None:
        """Grab the non-primary body's heater item in the same menu session.
        Spa Heater1 and Pool Heater1 are adjacent (Spa then Pool), so the other
        item is one step away: from Pool go LEFT to Spa, from Spa go RIGHT to
        Pool."""
        other = 'spa' if primary == 'pool' else 'pool'
        direction = 'LEFT' if primary == 'pool' else 'RIGHT'
        self._read_heater_in_menu(other, direction)

    def set_heater_enabled(self, which: str, on: bool) -> dict:
        """Enable/disable a heater using the HEATER_1 switch ONLY — never +/-.

        Auto <-> Manual Off is always toggled with the HEATER_1 key on the menu
        item (the switch). Enabling reveals the stored °F, which we capture into
        state. Idempotent: if already in the requested state, nothing happens.
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
                setpoint_f = None
                if on and was_off:
                    txt = self._enable_heater(label)          # switch ON
                    setpoint_f = self._degf(txt)
                elif on and not was_off:
                    setpoint_f = self._degf(txt)              # already on; read °F
                elif not on and not was_off:
                    txt = self._restore_heater_off(label)     # switch OFF
                # else: not on and was_off — already off, nothing to do.
                end_enabled = 'Manual Off' not in txt
                with state_lock:
                    if which == 'pool':
                        state.pool_heater_enabled = end_enabled
                        if setpoint_f is not None:
                            state.pool_setpoint_f = setpoint_f
                    else:
                        state.spa_heater_enabled = end_enabled
                        if setpoint_f is not None:
                            state.spa_setpoint_f = setpoint_f
                # Already in the menu — passively grab the other body's target too.
                self._read_other_heater(which)
                return {'which': which, 'enabled': end_enabled, 'was_off': was_off,
                        'setpoint_f': setpoint_f}
        finally:
            self.fast_exit()

    def set_heater(self, which: str, target_f: int) -> dict:
        """Write a heater setpoint with +/- — but NEVER while the heater is Off.

        The setpoint is only adjustable when the heater is on (Auto). If it's
        'Manual Off', turn it on with the HEATER_1 switch first (setting a temp
        implies wanting heat), then step to the target, and LEAVE it on. If
        already on, just adjust. 1°F steps, clamped [65, 104].
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
                if 'Manual Off' in txt:
                    # Never +/- while Off — switch it on first, then adjust.
                    self._enable_heater(label)
                # Store the value the panel ACTUALLY confirmed, not the requested
                # target — if keypresses drop, _step_to returns the unchanged
                # value (a stall), and recording the target would make the sidecar
                # believe a write landed when it didn't.
                final = self._step_to(self._degf, target_f,
                                      'PLUS', 'MINUS', self._STEP_MAX, label)
                reached = (final == target_f)
                with state_lock:
                    if which == 'pool':
                        state.pool_setpoint_f = final
                        state.pool_heater_enabled = True
                    else:
                        state.spa_setpoint_f = final
                        state.spa_heater_enabled = True
                if not reached:
                    log.warning('Heater %s setpoint reached %s°F, requested %s°F '
                                '— keypresses may be dropping', which, final, target_f)
                    if _ac_backend is not None:
                        _immediate_wedge_probe()
                # Already in the menu — passively grab the other body's target too.
                self._read_other_heater(which)
                return {'which': which, 'target_f': target_f,
                        'actual_f': final, 'reached': reached}
        finally:
            self.fast_exit()

    # ── Chlorinator output % ─────────────────────────────────────────────────

    def read_chlorinator(self, which: str) -> dict:
        """Navigate to a chlorinator item and read its current % (non-mutating)."""
        if which not in ('pool', 'spa'):
            raise ValueError('which must be "pool" or "spa"')
        label = self._CHLOR_LABEL[which]
        try:
            with _nav_lock:
                self._anchor()
                txt = self._press_until('RIGHT', lambda t: label in t,
                                        self._NAV_MAX, label)
                pct = self._pct(txt)
                if pct is None:
                    raise RuntimeError(f'Cannot parse {label}: {txt!r}')
                with state_lock:
                    if which == 'pool':
                        state.chlorinator_percent = float(pct)
                    else:
                        state.spa_chlorinator_percent = float(pct)
                return {'which': which, 'percent': pct}
        finally:
            self.fast_exit()

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
        """Toggle Super Chlorinate on/off via Settings menu navigation.

        Frame format (verified on hardware):
          'Super Chlorinate <span class="WBON">Off</span>'
          'Super Chlorinate <span class="WBON">On</span>'
        PLUS = Off→On, MINUS = On→Off.
        """
        try:
            with _nav_lock:
                self._anchor()
                txt = self._press_until('RIGHT', lambda t: 'Super Chlorinate' in t,
                                        self._NAV_MAX, 'Super Chlorinate')
                # '>On<' is unambiguous; '>Off<' also matches. 'WBON' contains 'on'
                # so a plain substring check would always be True.
                current = bool(re.search(r'>\s*On\s*<', txt, re.I))
                if current != on:
                    key = 'PLUS' if on else 'MINUS'
                    self._send(key)
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

    def _goto_spa_speed(self) -> str:
        """Anchor, enter the VSP submenu, and land on 'Spa Speed'. Returns frame."""
        self._anchor()
        self._press_until('RIGHT', lambda t: 'VSP Speed Settings' in t,
                          self._NAV_MAX, 'VSP Speed Settings')
        self._press_until('PLUS', lambda t: 'Filter Speed' in t, 6, 'VSP submenu')
        # Walk RIGHT past Filter Speed1–4 to reach Spa Speed.
        return self._press_until('RIGHT', lambda t: 'Spa Speed' in t, 8, 'Spa Speed')

    def read_spa_speed(self) -> dict:
        """Read the Spa Speed setting from the VSP submenu (non-mutating)."""
        try:
            with _nav_lock:
                txt = self._goto_spa_speed()
                pct = self._pct(txt)
                if pct is None:
                    raise RuntimeError(f'Cannot parse Spa Speed: {txt!r}')
                with state_lock:
                    state.spa_speed = pct
                return {'spa_speed': pct}
        finally:
            self.fast_exit()

    def set_spa_speed(self, target_pct: int) -> dict:
        """Write the Spa Speed setting. Snaps to 5% grid (same step size as slots)."""
        target_pct = int(_clamp(round(target_pct / 5) * 5, 0, 100))
        try:
            with _nav_lock:
                self._goto_spa_speed()
                self._step_to(self._pct, target_pct,
                              'PLUS', 'MINUS', self._STEP_MAX, 'Spa Speed')
                with state_lock:
                    state.spa_speed = target_pct
                return {'spa_speed': target_pct}
        finally:
            self.fast_exit()

    # ── Consolidated startup pre-fetch ───────────────────────────────────────

    def read_all_settings(self) -> dict:
        """Read every menu-navigable value in ONE menu session.

        Replaces the 5–6 separate anchor/read/exit trips the plugin made at
        startup (heater ×2, chlorinator ×2, VSP slots, spa speed). ONE anchor,
        a single pass around the ring, one exit. The Settings ring navigates
        both directions, so:

          1. Walk RIGHT reading Spa Heater1, Pool Heater1, then Spa Chlorinator,
             Pool Chlorinator. `_press_until` walks straight past VSP Speed
             Settings + Super Chlorinate (both allow-listed) to the next target.
          2. VSP submenu LAST: walk **LEFT** from Pool Chlorinator back to VSP
             Speed Settings (rather than re-anchoring), descend, read Filter
             Speed1–4 then Spa Speed. Leaving VSP for last means we exit the
             submenu straight to fast_exit — no fragile submenu→ring return.

        Best-effort: a failure on one item is logged and the pass continues, so
        a single bad read doesn't cost every value. Heater reads reveal the
        stored °F via PLUS and restore Manual Off (hardened, never left on).
        Always fast_exits.
        """
        out = {'heaters': {}, 'chlorinators': {}, 'vsp_slots': {}, 'spa_speed': None}
        try:
            with _nav_lock:
                # ── Single pass: heaters + chlorinators going RIGHT ─────────
                self._anchor()
                for which in ('spa', 'pool'):
                    label = self._HEATER_LABEL[which]
                    try:
                        txt = self._press_until('RIGHT', lambda t, l=label: l in t,
                                                self._NAV_MAX, label)
                        # Pure read: never +/- or toggle. Setpoint only visible
                        # when on; keep the last-known value when Manual Off.
                        enabled = 'Manual Off' not in txt
                        setpoint_f = self._degf(txt) if enabled else None
                        with state_lock:
                            if which == 'pool':
                                state.pool_heater_enabled = enabled
                                if setpoint_f is not None:
                                    state.pool_setpoint_f = setpoint_f
                            else:
                                state.spa_heater_enabled = enabled
                                if setpoint_f is not None:
                                    state.spa_setpoint_f = setpoint_f
                        out['heaters'][which] = {'enabled': enabled,
                                                 'setpoint_f': setpoint_f}
                    except Exception as e:
                        log.warning('read_all_settings heater %s: %s', which, e)
                for which in ('spa', 'pool'):
                    label = self._CHLOR_LABEL[which]
                    try:
                        txt = self._press_until('RIGHT', lambda t, l=label: l in t,
                                                self._NAV_MAX, label)
                        pct = self._pct(txt)
                        if pct is not None:
                            with state_lock:
                                if which == 'pool':
                                    state.chlorinator_percent = float(pct)
                                else:
                                    state.spa_chlorinator_percent = float(pct)
                            out['chlorinators'][which] = pct
                    except Exception as e:
                        log.warning('read_all_settings chlorinator %s: %s', which, e)

                # ── VSP submenu LAST: walk LEFT back to it, no re-anchor ────
                try:
                    self._press_until('LEFT', lambda t: 'VSP Speed Settings' in t,
                                      self._NAV_MAX, 'VSP Speed Settings')
                    self._press_until('PLUS', lambda t: 'Filter Speed' in t,
                                      6, 'VSP submenu')
                    txt = self._press_until('RIGHT', lambda t: 'Filter Speed1' in t,
                                            2, 'Filter Speed1')
                    slots = {}
                    for slot in (1, 2, 3, 4):
                        if slot > 1:
                            txt = self._press_until(
                                'RIGHT', lambda t, s=slot: f'Filter Speed{s}' in t,
                                4, f'Filter Speed{slot}')
                        pct = self._pct(txt)
                        if pct is not None:
                            slots[slot] = pct
                    with state_lock:
                        state.vsp_slot_pct.update(slots)
                    out['vsp_slots'] = slots
                    # Spa Speed is the sub-item after Filter Speed4.
                    txt = self._press_until('RIGHT', lambda t: 'Spa Speed' in t,
                                            4, 'Spa Speed')
                    spa_pct = self._pct(txt)
                    if spa_pct is not None:
                        with state_lock:
                            state.spa_speed = spa_pct
                        out['spa_speed'] = spa_pct
                except Exception as e:
                    log.warning('read_all_settings vsp/spa-speed: %s', e)
        finally:
            self.fast_exit()
        return out

    def sweep_scroll(self, max_presses: int = 24) -> dict:
        """Actively advance the idle status scroll with RIGHT to capture every
        reading at normal key timing, instead of waiting ~6s per item for the
        natural cycle. Each frame is applied to state via the scroll parser.
        Stops on a full cycle (a frame repeats) or the press budget. Stays in
        the status display; if a press drifts into a menu, exits cleanly.

        Returns the frames seen + total elapsed, so a caller/tester can tell
        whether RIGHT is actually advancing the scroll (fast) vs the press doing
        nothing and us just riding the ~6s natural cycle (slow).
        """
        seen = set()
        frames = []
        t0 = time.time()
        drifted = False
        with _nav_lock:
            txt = self._lcd.text()
            for _ in range(max(1, max_presses)):
                if txt and txt in seen:
                    break  # wrapped — full cycle captured
                if txt:
                    _apply_ac_scroll_to_state(txt)
                    frames.append({'frame': txt, 'status': self._is_status(txt)})
                    seen.add(txt)
                txt = self._send('RIGHT')
            drifted = bool(txt) and not self._is_status(txt)
        if drifted:
            self.fast_exit()   # re-acquires _nav_lock; call outside the block
        return {'frames': frames, 'count': len(frames),
                'elapsed_s': round(time.time() - t0, 1)}


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
# Don't sort JSON keys: sorting a dict with mixed int/str keys raises TypeError
# and 500s /status (which takes the whole plugin + cockpit offline). Insertion
# order is fine for our consumers.
try:
    app.json.sort_keys = False        # Flask 2.3+
except AttributeError:
    app.config['JSON_SORT_KEYS'] = False  # older Flask


@app.route('/faults/candidates', methods=['GET', 'POST'])
def fault_candidates() -> Response:
    """Discovery log of alert-looking frames we don't yet recognize, for building
    the fault backlog. GET returns them (frame -> {first,last,count}); once a
    real one is triaged into _FAULT_PHRASES, POST {"clear": true} to reset."""
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        if body.get('clear'):
            with _fault_cand_lock:
                _fault_candidates.clear()
            try:
                with open(_FAULT_CANDIDATES_PATH, 'w') as fh:
                    json.dump({}, fh)
            except Exception:
                pass
            return jsonify({'ok': True, 'cleared': True})
    with _fault_cand_lock:
        return jsonify({'candidates': dict(_fault_candidates)})


@app.route('/alerts', methods=['GET', 'POST'])
def alerts_route() -> Response:
    """Recent sidecar warnings/errors for the cockpit. GET returns them (with
    timestamps); POST {"clear": true} dismisses the current set."""
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        if body.get('clear'):
            _alert_buffer.clear()
            return jsonify({'ok': True, 'cleared': True})
    return jsonify({'alerts': _alert_buffer.recent()})


@app.route('/history')
def get_history() -> Response:
    """Rolling pool/spa/air temperature history for the cockpit chart.
    Optional ?hours=N trims to the last N hours. Returns
    {"samples": [[epoch_s, pool, spa, air], ...]}."""
    with _temp_history_lock:
        samples = list(_temp_history)
    try:
        hours = float(request.args.get('hours', 0))
    except (TypeError, ValueError):
        hours = 0
    if hours > 0 and samples:
        cutoff = time.time() - hours * 3600
        samples = [s for s in samples if s and s[0] >= cutoff]
    return jsonify({'samples': samples})


@app.route('/status')
def get_status() -> Response:
    # Compute the cooldown remainder BEFORE taking state_lock: _wedge_cooling_down()
    # acquires state_lock itself, and state_lock is non-reentrant, so calling it
    # inside the `with state_lock:` block below self-deadlocks — pinning state_lock
    # forever and taking every status poll (and the whole accessory set) offline.
    cooldown = _wedge_cooling_down()
    with state_lock:
        return jsonify({
            'circuits':            dict(state.circuits),
            'pool_temp':           state.pool_temp,
            'air_temp':            state.air_temp,
            'spa_temp':            state.spa_temp,
            'salt_level':          state.salt_level,
            'chlorinator_percent':     state.chlorinator_percent,
            'spa_chlorinator_percent': state.spa_chlorinator_percent,
            'pump_speed':              state.pump_speed,
            'pump_startup':            state.pump_startup,
            'spa_speed':               state.spa_speed,
            'light_program':           dict(state.light_program),
            # heater setpoints are read via menu navigation, cached here after reads
            'pool_setpoint_f':     state.pool_setpoint_f,
            'spa_setpoint_f':      state.spa_setpoint_f,
            'pool_heater_enabled': state.pool_heater_enabled,
            'spa_heater_enabled':  state.spa_heater_enabled,
            'heater_active':       state.heater_active,
            'valve_mode':          state.valve_mode,
            'vsp_slot_pct':        dict(state.vsp_slot_pct),
            'vsp_active_slot':     state.vsp_active_slot,
            'connected':           state.connected,
            'last_update':         state.last_update,
            'bridge_wedged':       state.bridge_wedged,
            'wedge_cooldown_remaining_s': round(cooldown) if cooldown else 0,
            'backend':             _active_backend,
            'ui_circuits':         list(_ui_circuits),
            'circuit_labels':      dict(_ui_circuit_labels),
            'faults':              _current_faults(),
            # Rolling 10-min window so transient alerts auto-age off the cockpit
            # (a flaky link shows current activity, not a growing pile needing a
            # manual Dismiss). Coalescing bumps a repeat's timestamp, so an
            # actively-recurring alert stays visible until it actually stops.
            'alerts':              _alert_buffer.recent(window_s=600, limit=8),
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
    if backend not in ('aquaconnect', 'rs485', 'rs485bridge'):
        return jsonify({'error': "backend must be 'aquaconnect', 'rs485', or 'rs485bridge'"}), 400

    cfg = _load_backend_config()
    cfg['backend'] = backend
    for k in ('aquaconnect_host', 'rs485_host', 'observe_rs485_host', 'rs485bridge_host'):
        if body.get(k):
            cfg[k] = body[k]
    if body.get('rs485_port'):
        cfg['rs485_port'] = int(body['rs485_port'])
    if body.get('rs485bridge_port'):
        cfg['rs485bridge_port'] = int(body['rs485bridge_port'])
    if body.get('observe_rs485_port'):
        cfg['observe_rs485_port'] = int(body['observe_rs485_port'])
    if 'observe_rs485' in body:
        cfg['observe_rs485'] = bool(body['observe_rs485'])

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


@app.route('/backend/toggle', methods=['POST'])
def toggle_backend() -> Response:
    """Flip the active backend to the other one and restart into it.

    The whole-sidecar switch is the 'fully silent idle' toggle: only the active
    backend's process paths run, so the other bridge never touches the panel —
    every read/write/benchmark on the new active backend is single-transport.
    Clears observe_rs485 so the clean (no parallel observer) mode is used.
    """
    other = 'rs485' if _active_backend == 'aquaconnect' else 'aquaconnect'
    cfg = _load_backend_config()
    cfg['backend'] = other
    cfg['observe_rs485'] = False   # single-transport: no parallel observer
    try:
        _save_backend_config(cfg)
    except Exception as e:
        log.error('toggle_backend persist failed: %s', e)
        return jsonify({'error': f'persist failed: {e}'}), 500
    log.info('Backend TOGGLE %s -> %s; restarting.', _active_backend, other)

    def _restart() -> None:
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_restart, daemon=True, name='backend-toggle').start()
    return jsonify({'ok': True, 'restarting': True,
                    'from': _active_backend, 'to': other})


# ── Backend-agnostic live frame stream + per-backend benchmark ───────────────
# Upstream consumes /stream (active backend) and never names a backend. The
# /stream/<name> and /benchmark/<name> forms target a specific backend by name
# — needed for the parallel RS-485 validation where both buses run at once.

_STREAM_BACKENDS = ('aquaconnect', 'rs485', 'rs485bridge')


def _sse_response(hub: FrameHub) -> Response:
    """Server-Sent Events feed of a hub's frames (recent tail, then live)."""
    def gen():
        yield 'retry: 3000\n\n'
        for f in hub.recent(10):
            yield f'data: {json.dumps(f)}\n\n'
        for f in hub.follow(timeout=20.0):
            if f is None:
                yield ': heartbeat\n\n'        # keep the connection alive
            else:
                yield f'data: {json.dumps(f)}\n\n'
    return Response(gen(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


@app.route('/stream')
def stream_active() -> Response:
    """Live LCD frame stream from whichever backend is active (SSE)."""
    return _sse_response(_get_hub(_active_backend or 'aquaconnect'))


@app.route('/stream/<name>')
def stream_named(name: str) -> Response:
    """Live LCD frame stream from a named backend, even if it's only observing."""
    if name not in _STREAM_BACKENDS:
        return jsonify({'error': f'unknown backend: {name}'}), 404
    return _sse_response(_get_hub(name))


@app.route('/backends')
def list_backends() -> Response:
    """List known backends, their role (active/observer/inactive), and liveness."""
    out = []
    for name in _STREAM_BACKENDS:
        hub = _frame_hubs.get(name)
        if name == _active_backend:
            role = 'active'
        elif name == 'rs485' and _rs485_observer is not None:
            role = 'observer'
        else:
            role = 'inactive'
        info = {
            'name': name,
            'role': role,
            'frames_seen': hub._seq if hub else 0,
            'last_frame_ts': hub.last_publish if hub else None,
        }
        if name == 'rs485' and (_rs485_observer is not None or _rs485_obs_state):
            with _rs485_obs_lock:
                info['connected'] = bool(_rs485_obs_state.get('connected'))
                info['observed_state'] = dict(_rs485_obs_state)
        out.append(info)
    return jsonify({'active': _active_backend, 'backends': out})


@app.route('/benchmark/<name>', methods=['POST'])
def benchmark_named(name: str) -> Response:
    """Run a navigation speed test on a named backend.

    Body: {"laps"?: int=3, "slot"?: int=1, "min_gap"?, "post_menu_settle"?,
           "key_timeout"?}. Walks read_vsp_slot(slot) for `laps` laps and reports
    per-lap wall time, keypresses, drops, and (per key) the wait latency from
    the nav trace. Comparable across backends — for RS-485 the latency is the
    bus round-trip; for AquaConnect it's the HTTP frame-reader confirm time.

    Prefers the ACTIVE backend (single-transport, no cross-bridge contention).
    Falls back to the observe-only RS-485 listener only if rs485 is requested
    while AquaConnect is active.
    """
    if name not in _STREAM_BACKENDS:
        return jsonify({'error': f'unknown backend: {name}'}), 404
    body = request.get_json(silent=True) or {}
    laps = max(1, int(body.get('laps', 3)))
    slot = int(body.get('slot', 1))
    applied = _apply_overrides(_nav_timing_defaults(), body)

    nav, mode, err, code = _resolve_benchmark_nav(name)
    if nav is None:
        return jsonify({'error': err}), code

    result = _run_nav_benchmark(nav, laps, slot, applied, is_rs485=(name == 'rs485'))
    result['backend'] = name
    result['mode'] = mode   # 'active' (single-transport) or 'observer'
    return jsonify(result)


@app.route('/benchmark/rs485/sweep', methods=['POST'])
def rs485_sweep() -> Response:
    """Sweep key_predelay_ms values to find the RS-485 panel's accept window.

    Body: {"predelays_ms": [30,50,70,100,150], "laps"?: 2, "slot"?: 1,
           "key_burst"?: 1, "key_timeout"?: 4.0, "post_menu_settle"?: 0.35}

    Runs a benchmark lap for each predelay in order. Returns each run's drop
    rate and avg key latency so you can pick the timing that minimises drops.
    Aborts early if a run produces 0 successful keys (panel stuck).
    """
    nav, mode, err, code = _resolve_benchmark_nav('rs485')
    if nav is None:
        return jsonify({'error': err}), code

    body = request.get_json(force=True) or {}
    predelays = body.get('predelays_ms',
                         [20, 30, 50, 70, 100, 130, 160, 200])
    laps = max(1, int(body.get('laps', 2)))
    slot = int(body.get('slot', 1))
    base = _apply_overrides(_nav_timing_defaults(), body)

    runs = []
    for pd in predelays:
        applied = dict(base)
        applied['key_predelay_ms'] = float(pd)
        result = _run_nav_benchmark(nav, laps, slot, applied, is_rs485=True)
        s = result['summary']
        entry = {
            'key_predelay_ms': pd,
            'ok_laps': s['ok_laps'],
            'laps': s['laps'],
            'avg_s': s['avg_s'],
            'drop_rate_pct': s['drop_rate_pct'],
            'avg_key_latency_ms': s.get('avg_key_latency_ms'),
            'total_drops': s['total_drops'],
            'total_presses': s['total_presses'],
        }
        runs.append(entry)
        # If every lap failed with this predelay, the panel may be stuck in a
        # menu — abort rather than compounding errors.
        if s['ok_laps'] == 0 and s['laps'] >= 2:
            log.warning('rs485_sweep: all laps failed at predelay=%sms — aborting', pd)
            break

    # Rank by drop rate (ascending), then avg_s
    clean = [r for r in runs if r['ok_laps'] > 0]
    clean.sort(key=lambda r: (r['drop_rate_pct'] or 999, r['avg_s'] or 999))
    return jsonify({
        'runs': runs,
        'best': clean[0] if clean else None,
        'mode': mode,   # 'active' (single-transport) or 'observer'
        'params': {'laps': laps, 'slot': slot},
    })


def _percentile(sorted_vals: list, p: float):
    """Simple percentile (nearest-rank) of a pre-sorted list.
    (Named _percentile, not _pct, to avoid confusion with MenuNavigator._pct,
    the LCD value parser.)"""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, int(round(p / 100.0 * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


@app.route('/debug/rs485/taptest', methods=['POST'])
def rs485_taptest() -> Response:
    """Fast keypress-landing probe for tuning the RS-485/WiFi-bridge timing.

    Body: {"presses"?: 20, "timeout"?: 1.5, "predelay_ms"?: <set live first>}

    Enters the Settings menu and fires single, benign RIGHT/LEFT presses (cursor
    moves only — no equipment changes), measuring for EACH press whether it
    landed (display changed) and how long it took. Reports landing rate plus the
    latency distribution (min/p50/p90/max) of landed presses — the long tail is
    the signature of bridge buffering/jitter. Much faster than the full nav
    benchmark, so it's the loop to use while changing bridge settings.

    Single presses with NO re-press, so the landing rate is the raw per-window
    hit probability (unlike the benchmark, whose navigator re-presses drops).
    """
    nav, mode, err, code = _resolve_benchmark_nav('rs485')
    if nav is None:
        return jsonify({'error': err}), code

    body = request.get_json(silent=True) or {}
    presses = max(1, int(body.get('presses', 20)))
    timeout = float(body.get('timeout', 1.5))

    global KEY_PREDELAY_MS
    saved_predelay = KEY_PREDELAY_MS
    if body.get('predelay_ms') is not None:
        KEY_PREDELAY_MS = float(body['predelay_ms'])
    saved_timeout = MenuNavigator._KEY_TIMEOUT
    MenuNavigator._KEY_TIMEOUT = timeout

    results = []
    anchor_error = None
    try:
        with _nav_lock:
            nav._anchor()
            for i in range(presses):
                key = 'RIGHT' if i % 2 == 0 else 'LEFT'
                before = nav.text()
                t0 = time.time()
                after = nav._send(key)
                dt_ms = round((time.time() - t0) * 1000, 1)
                # A true landing means display changed AND we're still in the
                # Settings Menu ring (not a panel-auto-exit to status display).
                # Without this check, panel timeouts that change the LCD text
                # look like landed presses (false positives at slow timeouts).
                real_nav = (after != before and nav._in_settings(after))
                if not real_nav and after != before:
                    # Panel escaped the menu — re-anchor before continuing.
                    try:
                        nav._anchor()
                    except RuntimeError:
                        break
                results.append({'landed': real_nav, 'latency_ms': dt_ms})
    except RuntimeError as e:
        anchor_error = str(e)
    finally:
        MenuNavigator._KEY_TIMEOUT = saved_timeout
        KEY_PREDELAY_MS = saved_predelay
        try:
            nav.fast_exit()
        except Exception:
            pass

    if anchor_error and not results:
        return jsonify({'error': anchor_error}), 503

    landed = [r for r in results if r['landed']]
    lat = sorted(r['latency_ms'] for r in landed)
    return jsonify({
        'mode': mode,
        'presses': presses,
        'landed': len(landed),
        'landing_rate_pct': round(100 * len(landed) / presses, 1),
        'predelay_ms': saved_predelay if body.get('predelay_ms') is None
                       else float(body['predelay_ms']),
        'latency_ms': {
            'min': lat[0] if lat else None,
            'p50': _percentile(lat, 50),
            'p90': _percentile(lat, 90),
            'max': lat[-1] if lat else None,
        },
        'detail': results,
    })


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


def _nav_timing_defaults() -> dict:
    """Snapshot the current navigation timing tunables."""
    return {
        'min_gap': _AC_MIN_GAP_S,                        # AC only
        'post_menu_settle': MenuNavigator._POST_MENU_SETTLE_S,
        'key_timeout': MenuNavigator._KEY_TIMEOUT,
        'key_predelay_ms': KEY_PREDELAY_MS,              # RS-485 only
        'key_burst': KEY_BURST,                          # RS-485 only
    }


def _apply_overrides(base: dict, body: dict) -> dict:
    """Merge requested timing overrides onto a base snapshot, coercing types."""
    applied = dict(base)
    for k in ('post_menu_settle', 'key_timeout'):
        if body.get(k) is not None:
            applied[k] = float(body[k])
    if body.get('min_gap') is not None:
        applied['min_gap'] = float(body['min_gap'])
    if body.get('key_predelay_ms') is not None:
        applied['key_predelay_ms'] = float(body['key_predelay_ms'])
    if body.get('key_burst') is not None:
        applied['key_burst'] = int(body['key_burst'])
    return applied


def _resolve_benchmark_nav(name: str):
    """Pick the navigator to benchmark for `name`.

    Returns (nav, mode, err, http_code). Prefers the ACTIVE backend so the run
    is single-transport (reads, nav and confirmation all on one bridge — the
    true 'is this backend viable on its own' test). Only falls back to the
    observe-only RS-485 listener when rs485 is asked for while AC is active.
    """
    if name == _active_backend:
        nav = _get_navigator()
        if nav is None:
            return None, None, f'{name} active but navigator unavailable', 503
        return nav, 'active', None, 200
    if name == 'rs485' and _rs485_observer is not None:
        return _rs485_observer.nav, 'observer', None, 200
    return None, None, (f'{name} is not the active backend and has no observer; '
                        f'toggle to it first (POST /backend/toggle)'), 503


def _run_nav_benchmark(nav, laps: int, slot: int, applied: dict,
                       is_rs485: bool = False) -> dict:
    """Apply `applied` tunables, time read_vsp_slot(slot) over `laps`, restore.

    read_vsp_slot is pure navigate-and-read — no panel state changes. Returns a
    dict with per-lap detail and an aggregate summary. Always restores the
    previous tunables, even on error.

    For RS-485 benchmarks (is_rs485=True) the relevant tunables are
    key_predelay_ms, key_burst, key_timeout, and post_menu_settle.  The AC
    min_gap is not applicable. The 'requests' metric counts AC HTTP requests
    that happened alongside (the AC poll loop keeps running), so it's omitted
    from RS-485 results to avoid confusion.
    """
    global _AC_MIN_GAP_S, KEY_PREDELAY_MS, KEY_BURST
    saved = _nav_timing_defaults()
    laps_out: list = []
    try:
        _AC_MIN_GAP_S = applied['min_gap']
        MenuNavigator._POST_MENU_SETTLE_S = applied['post_menu_settle']
        MenuNavigator._KEY_TIMEOUT = applied['key_timeout']
        if is_rs485:
            KEY_PREDELAY_MS = applied['key_predelay_ms']
            KEY_BURST = applied['key_burst']

        for i in range(laps):
            seq0 = _NAV_SEQ[0]
            req0 = _ac_backend._req_count if (_ac_backend and not is_rs485) else 0
            t0 = time.time()
            ok, err = True, None
            try:
                nav.read_vsp_slot(slot)
            except Exception as e:
                ok, err = False, str(e)
            dt = time.time() - t0
            with _NAV_TRACE_LOCK:
                lap_keys = [e for e in _NAV_TRACE if e['seq'] > seq0]
            presses = len(lap_keys)
            drops = sum(1 for e in lap_keys if not e['changed'])
            lap: dict = {
                'lap': i + 1, 'ok': ok, 'seconds': round(dt, 2),
                'presses': presses, 'drops': drops,
                **({'error': err} if err else {}),
            }
            if not is_rs485:
                # AC: count HTTP requests (presses + frame-reader reads);
                # validates the N+1-per-N-keys frame-reader design.
                lap['requests'] = (_ac_backend._req_count - req0
                                   if _ac_backend else 0)
            else:
                # RS-485: report avg per-key latency from the trace (bus
                # round-trip from send_key → on_change confirmation).
                key_latencies = [e['wait_s'] for e in lap_keys if e['changed']]
                lap['avg_key_latency_ms'] = (
                    round(sum(key_latencies) / len(key_latencies) * 1000, 1)
                    if key_latencies else None)
            laps_out.append(lap)
    finally:
        _AC_MIN_GAP_S = saved['min_gap']
        MenuNavigator._POST_MENU_SETTLE_S = saved['post_menu_settle']
        MenuNavigator._KEY_TIMEOUT = saved['key_timeout']
        if is_rs485:
            KEY_PREDELAY_MS = saved['key_predelay_ms']
            KEY_BURST = saved['key_burst']

    ok_laps = [l for l in laps_out if l['ok']]
    times = [l['seconds'] for l in ok_laps]
    total_presses = sum(l['presses'] for l in laps_out)
    summary: dict = {
        'laps': laps, 'ok_laps': len(ok_laps),
        'total_s': round(sum(l['seconds'] for l in laps_out), 2),
        'avg_s': round(sum(times) / len(times), 2) if times else None,
        'min_s': min(times) if times else None,
        'max_s': max(times) if times else None,
        'total_presses': total_presses,
        'total_drops': sum(l['drops'] for l in laps_out),
        'drop_rate_pct': round(
            100 * sum(l['drops'] for l in laps_out) / total_presses, 1
        ) if total_presses else None,
    }
    if not is_rs485:
        total_requests = sum(l.get('requests', 0) for l in laps_out)
        summary['total_requests'] = total_requests
        summary['requests_per_press'] = (
            round(total_requests / total_presses, 2) if total_presses else None)
    else:
        valid_avgs = [l['avg_key_latency_ms'] for l in laps_out
                      if l.get('avg_key_latency_ms') is not None]
        summary['avg_key_latency_ms'] = (
            round(sum(valid_avgs) / len(valid_avgs), 1) if valid_avgs else None)
    return {'summary': summary, 'laps_detail': laps_out, 'applied': applied}


@app.route('/debug/nav-benchmark', methods=['POST'])
def nav_benchmark() -> Response:
    """Time a real menu read under chosen timing params, to find the safe floor.

    Runs read_vsp_slot(slot) `laps` times (pure navigate-and-read — no panel
    state is changed) and reports wall-time per lap plus how many keypresses
    were dropped and re-pressed. Temporarily overrides the timing tunables for
    the run and restores them after, so you can sweep without editing code:

      POST /debug/nav-benchmark
        {"laps":5, "slot":1,
         "min_gap":0.6, "post_menu_settle":0.25, "key_timeout":3.0}

    Omitted params keep their current value. Compare total_s / drops across runs
    to find the fastest settings that still complete every lap with few drops.
    """
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    body = request.get_json(force=True, silent=True) or {}
    laps = max(1, int(body.get('laps', 5)))
    slot = int(body.get('slot', 1))
    saved = _nav_timing_defaults()
    applied = _apply_overrides(saved, body)
    result = _run_nav_benchmark(nav, laps, slot, applied)
    return jsonify({'applied': applied, 'saved_defaults': saved, **result})


@app.route('/debug/nav-sweep', methods=['POST'])
def nav_sweep() -> Response:
    """Sweep one or more timing params in a single call and rank the results.

    Runs _run_nav_benchmark once per combination of the swept values, holding
    every other tunable at its current default (or at a fixed override you
    supply). Designed to find the key-press gap floor in one shot:

      POST /debug/nav-sweep
        {"laps":4, "slot":1,
         "min_gaps":[0.9,0.8,0.7,0.6,0.5,0.4,0.3],
         "settle_between_s":2.0}        # pause between runs so the box recovers

    You may also sweep post_menu_settles:[...]; every combination is run. Each
    row reports total_s/avg_s and drops so you can see where drops start
    climbing — that gap is the floor; pick one step above it. The 'ranking'
    lists clean runs (all laps ok) fastest first.
    """
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    body = request.get_json(force=True, silent=True) or {}
    laps = max(1, int(body.get('laps', 4)))
    slot = int(body.get('slot', 1))
    settle = float(body.get('settle_between_s', 2.0))

    # Swept axes (lists). Default to a min_gap sweep if none given. The
    # post_menu_settle axis defaults to a single [None] entry meaning "leave at
    # the fixed value" — don't coerce that None to float.
    min_gaps = [float(x) for x in body.get('min_gaps', [_AC_MIN_GAP_S])]
    settles = [None if x is None else float(x) for x in body.get('post_menu_settles', [None])]

    base = _nav_timing_defaults()
    fixed = _apply_overrides(base, body)  # apply any scalar fixed overrides

    rows: list = []
    aborted = None
    first = True
    for mg in min_gaps:
        for pm in settles:
            if not first:
                time.sleep(settle)
            first = False
            applied = dict(fixed)
            applied['min_gap'] = mg
            if pm is not None:
                applied['post_menu_settle'] = pm
            res = _run_nav_benchmark(nav, laps, slot, applied)
            s = res['summary']
            rows.append({
                'min_gap': mg,
                'post_menu_settle': applied['post_menu_settle'],
                'total_s': s['total_s'], 'avg_s': s['avg_s'],
                'drops': s['total_drops'], 'presses': s['total_presses'],
                'requests': s['total_requests'],
                'requests_per_press': s['requests_per_press'],
                'ok_laps': s['ok_laps'], 'laps': s['laps'],
            })
            # A run where EVERY lap failed is the wedge signature: presses
            # are being dropped (ACKed but ignored at the RS-485 relay) and
            # continuing would only grind out more failures while keeping the
            # box hammered. Stop here and return what we have — the last
            # clean run before this is the real floor.
            if s['ok_laps'] == 0:
                aborted = {
                    'at_min_gap': mg,
                    'reason': 'all laps failed at this gap — likely the box '
                              'command path wedged; aborting sweep. '
                              'Power-cycle the AquaConnect box before retrying.',
                }
                break
        if aborted:
            break

    # "Clean" = all laps completed successfully. Drops (re-pressed keys) are
    # baseline WiFi-bridge noise unrelated to gap size; requiring zero drops
    # would never yield a winner. What matters is that every lap finished.
    clean = [r for r in rows
             if r['ok_laps'] == r['laps'] and r['avg_s'] is not None]
    ranking = sorted(clean, key=lambda r: r['avg_s'])
    return jsonify({
        'laps': laps, 'slot': slot, 'defaults': base,
        'fixed_overrides': {k: fixed[k] for k in fixed if fixed[k] != base[k]},
        'rows': rows,
        'aborted': aborted,
        'fastest_clean': ranking[0] if ranking else None,
        'ranking': ranking,
    })


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
                    # Also stamp wedge_detected_at so the 120s power-cycle
                    # cooldown engages (the "2 unconfirmed writes" path does this
                    # too). Without it the sidecar keeps probing every 30s during
                    # the box's reboot window and a fast "recovery" races the plug.
                    state.wedge_detected_at = time.time()
                log.warning('Bridge command path wedged (active canary probe). '
                            'Recovery: %s.', _wedge_recovery_hint())
        return result
    except Exception as e:
        log.error('Canary probe error: %s', e)
        return {'alive': False, 'error': str(e)}


def _canary_probe_loop() -> None:
    """Background thread: periodically probe the command path when idle.

    Healthy:  probe every _WEDGE_PROBE_INTERVAL_S (cheap liveness check).
    Wedged:   wait out the power-cycle cooldown window (_WEDGE_POWERCYCLE_COOLDOWN_S)
              so the box has time to reboot, then probe immediately. After that
              probe every _WEDGE_RECOVERY_INTERVAL_S until recovery confirmed.
    """
    # Stagger first probe so it doesn't fire at startup during initial connect.
    time.sleep(60 + random.uniform(0, 30))
    while True:
        try:
            remaining = _wedge_cooling_down()
            if remaining is not None:
                # Still in the power-cycle cooldown — sleep out the remainder
                # then probe immediately on wake so recovery is detected fast.
                log.debug('Wedge cooldown: %.0fs remaining — deferring probe', remaining)
                time.sleep(remaining + 2)   # +2s margin for box to finish booting
                if _ac_backend is not None and not _nav_lock.busy():
                    log.info('Wedge cooldown elapsed — probing for recovery')
                    _ac_canary_probe()
                # Still wedged after the reboot window → re-arm the power-cycle
                # automation (edge-triggered, so a stuck-On won't retry), up to 3x.
                with state_lock:
                    still_wedged = state.bridge_wedged
                if still_wedged:
                    if not _rearm_wedge():
                        log.error('Wedge persists after %d power-cycle attempts — '
                                  'manual intervention needed; commands stay blocked.',
                                  _WEDGE_MAX_REARMS)
                continue
            with state_lock:
                wedged = state.bridge_wedged
            # Probe when wedged (to detect recovery) always; when healthy, only
            # if the proactive probe is enabled (interval > 0). Reactive-only
            # mode (interval 0) skips idle probing — a wedge is then caught when
            # a real command fails. Defer to any real action running OR queued so
            # the canary presses never stomp on a user command.
            proactive = _WEDGE_PROBE_INTERVAL_S > 0
            if _ac_backend is not None and not _nav_lock.busy() and (wedged or proactive):
                _ac_canary_probe()
        except Exception as e:
            log.error('Canary probe loop error: %s', e)
        with state_lock:
            wedged = state.bridge_wedged
        if wedged:
            time.sleep(_WEDGE_RECOVERY_INTERVAL_S)
        elif _WEDGE_PROBE_INTERVAL_S > 0:
            time.sleep(_WEDGE_PROBE_INTERVAL_S)
        else:
            time.sleep(60)   # reactive-only idle tick; reactive failures set the flag


@app.route('/config/ui', methods=['POST'])
def set_ui_config() -> Response:
    """
    Mirror the Homebridge plugin's UI config so the web cockpit shows the same
    switches and labels as HomeKit.  Body:
        {"circuits": ["LIGHTS", "AUX_1", ...], "labels": {"AUX_1": "Waterfall"}}
    Best-effort: stored in-memory and surfaced via /status.
    """
    global _ui_circuits, _ui_circuit_labels
    body = request.get_json(force=True) or {}
    circuits = body.get('circuits')
    labels = body.get('labels')
    if isinstance(circuits, list):
        _ui_circuits = [str(c).upper() for c in circuits]
    if isinstance(labels, dict):
        _ui_circuit_labels = {str(k).upper(): str(v) for k, v in labels.items()}
    # Persist so the config survives a sidecar restart (the plugin only re-pushes
    # on a Homebridge restart). Otherwise the cockpit falls back to panel-reported
    # circuits, which include the AUX2 canary.
    try:
        cfg = _load_backend_config()
        cfg['ui_circuits'] = _ui_circuits
        cfg['ui_circuit_labels'] = _ui_circuit_labels
        _save_backend_config(cfg)
    except Exception as e:
        log.warning('Could not persist UI config: %s', e)
    return jsonify({'ok': True, 'circuits': _ui_circuits, 'labels': _ui_circuit_labels})


@app.route('/mode', methods=['POST'])
def set_mode() -> Response:
    """
    Set pool/spa valve mode.  Body: {"mode": "pool"|"spa"}

    For pool+spa-only systems this is a single cycle-key press whenever the
    current mode differs from the target.
    """
    body = request.get_json(force=True)
    target = body.get('mode', '').lower()
    if target not in ('pool', 'spa'):
        return jsonify({'error': 'mode must be "pool" or "spa"'}), 400

    with state_lock:
        current = state.valve_mode
        wedged = state.bridge_wedged
    if current == target:
        return jsonify({'ok': True, 'mode': target, 'changed': False})

    if _ac_backend is not None:
        block = _wedge_block_response()
        if block:
            return block
        # SPA and POOL both share key code '07' (the panel's pool/spa cycle key).
        # We only press when not already in the target state (idempotency guard).
        key_name = 'SPA' if target == 'spa' else 'POOL'
        try:
            with _nav_lock:
                _ac_backend.send_nav_key(key_name)
                with state_lock:
                    new_mode = state.valve_mode
        except Exception as e:
            # An exception here means the keypress itself failed (socket/HTTP) —
            # that IS a command-path failure worth a wedge probe.
            log.error(f'set_mode (AC) {target}: {e}')
            _record_command_failure()
            _immediate_wedge_probe()
            return jsonify({'error': str(e), 'bridge_wedged': state.bridge_wedged}), 502
        # The keypress landed. We do NOT hard-fail when valve_mode hasn't flipped
        # yet: the valve actuates over ~10-30s and the mode LEDs lag during the
        # transition, so a single confirming frame often still shows the old mode.
        # Report success — the poll loop reconciles valve_mode as the valve
        # finishes — and only note in the log whether it confirmed immediately.
        _record_command_success()
        confirmed = (new_mode == target)
        log.info('Mode -> %s (AquaConnect)%s', target,
                 '' if confirmed else ' (actuating, not yet confirmed)')
        return jsonify({'ok': True, 'mode': target, 'changed': True,
                        'confirmed': confirmed})

    p = _get_panel()
    if p is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        p.set_circuit('POOL' if target == 'pool' else 'SPA', True)
        with state_lock:
            state.valve_mode = target
        log.info(f'Mode -> {target}')
        return jsonify({'ok': True, 'mode': target, 'changed': True})
    except Exception as e:
        log.error(f'set_mode {target}: {e}')
        return jsonify({'error': str(e)}), 500


# Equipment circuits that toggle with a single AquaConnect keypad key. Maps the
# circuit name to its _AC_KEY_CODES entry. POOL/SPA are handled above (shared
# key 07, mode cycle). HEATER_1 routes through the navigator's Settings menu.
_AC_CIRCUIT_KEYS = {
    'FILTER': 'FILTER', 'LIGHTS': 'LIGHTS', 'AUX_1': 'AUX1', 'AUX_2': 'AUX2',
}


def _ac_heater_enable(which: str, on: bool) -> Response:
    """Toggle a heater on/off via AquaConnect keypad press, then read the setpoint.

    Two-phase:
    1. Press HEATER1 key (code 13) directly if `which` matches the current active
       body — the LCD response immediately shows 'Heater1 Auto Control' or
       'Heater1 Manual Off' which _apply_ac_scroll_to_state captures, giving
       HomeKit a sub-second confirmation.  If `which` is the non-active body
       (e.g. pool heater while in spa mode), fall back to menu navigation.
    2. If enabling: spawn a background thread to nav.read_heater(which) so the
       thermostat's target-temperature field is populated from the Settings menu.
    """
    block = _wedge_block_response()
    if block:
        return block

    with state_lock:
        cur = state.spa_heater_enabled if which == 'spa' else state.pool_heater_enabled
        cur_mode = state.valve_mode

    if cur == on:
        return jsonify({'ok': True, 'already': True, 'which': which})

    if cur_mode == which:
        # Active body → direct keypad press; LCD response confirms the new state.
        with _nav_lock:
            _ac_backend.send_nav_key('HEATER1')   # key code '13'
            with state_lock:
                new = state.spa_heater_enabled if which == 'spa' else state.pool_heater_enabled
        if new != on:
            log.warning('Heater %s -> %s not confirmed from LCD (still %s)', which, on, new)
            _record_command_failure()
            _immediate_wedge_probe()
            return jsonify({
                'error': f'Heater {which} enable={on} not confirmed',
                'bridge_wedged': state.bridge_wedged,
            }), 502
    else:
        # Non-active body → must navigate Settings menu to reach the right heater.
        nav = _get_navigator()
        if nav is None:
            return jsonify({'error': 'Not connected'}), 503
        try:
            # set_heater_enabled acquires _nav_lock itself — do NOT wrap it in
            # another `with _nav_lock:` here. _nav_lock is non-reentrant, so a
            # nested acquire deadlocks the thread (and pins the lock forever,
            # wedging every later command).
            nav.set_heater_enabled(which, on)
        except Exception as e:
            log.error('Heater %s enable via nav failed: %s', which, e)
            _record_command_failure()
            _immediate_wedge_probe()
            return jsonify({'error': str(e), 'bridge_wedged': state.bridge_wedged}), 502

    _record_command_success()
    log.info('Heater %s -> %s (AquaConnect)', which, 'ON' if on else 'OFF')

    # Phase 2: if enabling, read the setpoint from the Settings menu in the
    # background so the thermostat tile shows the target temperature without
    # blocking the HomeKit response.
    if on:
        def _bg_read_setpoint():
            nav = _get_navigator()
            if nav is None:
                return
            try:
                # read_heater acquires _nav_lock itself — wrapping it in another
                # `with _nav_lock:` here self-deadlocks (non-reentrant lock) and
                # pins the lock forever, wedging every later command.
                nav.read_heater(which)
                log.debug('bg heater read %s: setpoint updated', which)
            except Exception as exc:
                log.debug('bg heater read %s: %s', which, exc)
        threading.Thread(target=_bg_read_setpoint, daemon=True,
                         name=f'bg-heater-{which}').start()

    return jsonify({'ok': True, 'which': which})


def _ac_set_circuit(key: str, on: bool) -> Response:
    """Drive a circuit on/off through the AquaConnect backend.

    HEATER_1 routes through _ac_heater_enable (direct keypress + background
    setpoint read). POOL/SPA are body-mode cycle presses. Simple equipment
    circuits send their keypad key once and confirm via the re-read LED state.
    """
    log.info('HomeKit action: circuit %s -> %s', key, 'ON' if on else 'OFF')
    block = _wedge_block_response()
    if block:
        return block

    if key == 'HEATER_1':
        with state_lock:
            which = 'spa' if state.valve_mode == 'spa' else 'pool'
        return _ac_heater_enable(which, on)

    # POOL and SPA share key '07' (mode cycle: POOL → SPA → SPILLOVER → POOL).
    # Turning one on turns the other off (mutually exclusive body modes).
    # Turning one OFF means switching to the other body.
    if key in ('POOL', 'SPA'):
        dest = key.lower() if on else ('pool' if key == 'SPA' else 'spa')
        with state_lock:
            cur_mode = state.valve_mode
        if cur_mode == dest:
            return jsonify({'ok': True, 'already': True})
        # Press the shared mode key up to 3 times (full cycle length) until
        # the LED poll confirms the target mode. No menu navigation needed —
        # this is a direct keypad press that the next read cycle confirms.
        with _nav_lock:
            for _ in range(3):
                _ac_backend.send_nav_key('POOL')  # 'POOL'/'SPA'/'SPILLOVER' all → key 07
                with state_lock:
                    if state.valve_mode == dest:
                        break
            with state_lock:
                confirmed = state.valve_mode == dest
        if confirmed:
            _record_command_success()
            log.info('Body mode -> %s (AquaConnect circuit %s -> %s)', dest, key, 'ON' if on else 'OFF')
            return jsonify({'ok': True, 'valve_mode': dest})
        log.warning('Body mode switch to %s not confirmed (still %s) — probing bridge', dest, state.valve_mode)
        _record_command_failure()
        _immediate_wedge_probe()
        return jsonify({
            'error': f'Body mode switch to {dest} not confirmed',
            'bridge_wedged': state.bridge_wedged,
        }), 502

    keypad = _AC_CIRCUIT_KEYS.get(key)
    if keypad is None:
        return jsonify({'error': f'{key} cannot be toggled in AquaConnect mode'}), 422

    # Idempotent: only press if not already where we want it (the key is a
    # toggle, so a redundant press would flip us away from the target).
    with _nav_lock:
        with state_lock:
            cur = state.circuits.get(key)
            heater_was_active = state.heater_active
        if cur == on:
            return jsonify({'ok': True, 'already': True})
        _ac_backend.send_nav_key(keypad)   # press + settle + re-read (updates state)
        with state_lock:
            new = state.circuits.get(key)
    if new == on:
        _record_command_success()
        log.info('Circuit %s -> %s (AquaConnect)', key, 'ON' if on else 'OFF')
        return jsonify({'ok': True})
    # Expected non-confirm: turning FILTER off while a heater is running. The
    # control unit's cooldown keeps the pump running (the first FILTER press
    # stops only the heater; a second press after cooldown stops the pump). This
    # is the unit protecting itself, NOT a wedge — do not cry wolf.
    if key == 'FILTER' and not on and heater_was_active:
        log.info('Filter off deferred by heater cooldown — pump keeps running (expected, not wedged)')
        return jsonify({'ok': True, 'deferred': 'heater_cooldown',
                        'note': 'Filter stays on during heater cooldown; the unit '
                                'stops the pump after cooldown or on a second off press'})
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
      frametype: "local"    -> 00 02 LOCAL_WIRED_KEY_EVENT (what send_key uses
                                for keys <= 0xffff)
                 "remote"   -> 00 03 REMOTE_WIRED_KEY_EVENT (what a wired remote
                                like the AquaConnect box emits)
                 "wireless" -> 00 83 WIRELESS_KEY_EVENT (what send_key uses for
                                keys > 0xffff, e.g. HEATER_1; 4-byte key layout)
    Builds the frame exactly like aqualogic._get_key_event_frame but lets us
    pick the frame type, so we can prove whether menu-scroll keys (or the
    heater key) need a different event type. Returns the hex frame queued.
    """
    p = _get_panel()
    if p is None:
        return jsonify({'error': 'Not connected'}), 503
    body = request.get_json(force=True) or {}
    name = body.get('key', '')
    ftype = body.get('frametype', 'remote').lower()
    if ftype not in ('local', 'remote', 'wireless'):
        return jsonify({'error': f'frametype must be local|remote|wireless, got {ftype!r}'}), 400
    try:
        aq = p._aq
        Keys = p._Keys
        k = getattr(Keys, name, None)
        if k is None:
            return jsonify({'error': f'Unknown key {name!r}'}), 400
        frame = bytearray()
        frame.append(aq.FRAME_DLE)
        frame.append(aq.FRAME_STX)
        if ftype == 'wireless':
            # Wireless layout differs: 01 marker, then the key value as 4 bytes
            # twice, then a trailing 00 (mirrors _get_key_event_frame's >0xffff
            # branch). Lets us test HEATER_1 and the like as wireless events.
            aq._append_data(frame, aq.FRAME_TYPE_WIRELESS_KEY_EVENT)
            aq._append_data(frame, b'\x01')
            aq._append_data(frame, int(k.value).to_bytes(4, byteorder='little'))
            aq._append_data(frame, int(k.value).to_bytes(4, byteorder='little'))
            aq._append_data(frame, b'\x00')
        else:
            type_bytes = aq.FRAME_TYPE_REMOTE_WIRED_KEY_EVENT if ftype == 'remote' \
                else aq.FRAME_TYPE_LOCAL_WIRED_KEY_EVENT
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


def _current_key_timing() -> dict:
    return {'burst': KEY_BURST, 'predelay_ms': KEY_PREDELAY_MS,
            'gap_ms': KEY_GAP_MS, 'max_retries': KEY_MAX_RETRIES,
            'verify_delay_s': KEY_VERIFY_DELAY_S, 'pad_bytes': KEY_PAD_BYTES}


@app.route('/keytiming', methods=['GET', 'POST'])
@app.route('/debug/keyburst', methods=['GET', 'POST'])
def debug_keyburst() -> Response:
    """Live-tune the RS-485 key timing without a restart.

    POST JSON any of: burst (int), predelay_ms (float), gap_ms (float),
    max_retries (int), verify_delay_s (float), pad_bytes (int). The send path
    reads these globals on each keypress, so changes take effect immediately.

    Pass "persist": true to save the resulting values to backend.json so they
    survive sidecar restarts/reboots — useful when dialing in timing over many
    debug runs. The persisted values override the CLI --key-* args at startup.
    """
    global KEY_BURST, KEY_PREDELAY_MS, KEY_GAP_MS, KEY_MAX_RETRIES, KEY_VERIFY_DELAY_S, KEY_PAD_BYTES
    persisted = False
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
        log.info('Key timing retuned: burst=%d predelay=%.0fms gap=%.0fms '
                 'retries=%d verify=%.1fs pad=%d', KEY_BURST, KEY_PREDELAY_MS,
                 KEY_GAP_MS, KEY_MAX_RETRIES, KEY_VERIFY_DELAY_S, KEY_PAD_BYTES)
        if body.get('persist'):
            try:
                cfg = _load_backend_config()
                cfg['key_timing'] = _current_key_timing()
                _save_backend_config(cfg)
                persisted = True
                log.info('Key timing persisted to backend.json')
            except Exception as e:
                log.error('Could not persist key timing: %s', e)
    out = _current_key_timing()
    out['persisted'] = persisted
    return jsonify(out)


@app.route('/debug/scroll-sweep', methods=['POST'])
def debug_scroll_sweep() -> Response:
    """Test: actively advance the idle status scroll with RIGHT and capture each
    reading, instead of waiting ~6s per item. Body (optional): {"max_presses":24}.
    Returns the frames seen + elapsed_s — if elapsed is ~1s/frame, RIGHT advances
    the scroll (fast); if ~6s/frame, the press is a no-op and we're just riding
    the natural cycle."""
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    body = request.get_json(silent=True) or {}
    try:
        n = int(body.get('max_presses', 24))
    except (TypeError, ValueError):
        n = 24
    try:
        return jsonify(nav.sweep_scroll(max_presses=n))
    except Exception as e:
        log.error(f'scroll-sweep: {e}')
        if _ac_backend is not None:
            _immediate_wedge_probe()
        return jsonify({'error': str(e)}), 500


@app.route('/wedge-probe', methods=['GET', 'POST'])
def wedge_probe_config() -> Response:
    """Get/set the proactive wedge-probe interval without a restart.

    POST JSON: {"interval_s": 1800}   (0 = reactive-only, no idle probing)
    Pass "persist": true to save it to backend.json so it survives restarts.

    The reactive on-failure probe and the 30s re-probe-while-wedged are always
    active regardless of this setting; this only controls the idle/proactive
    canary cadence.
    """
    global _WEDGE_PROBE_INTERVAL_S
    persisted = False
    if request.method == 'POST':
        body = request.get_json(force=True) or {}
        if 'interval_s' in body:
            try:
                _WEDGE_PROBE_INTERVAL_S = max(0.0, float(body['interval_s']))
            except (TypeError, ValueError):
                return jsonify({'error': 'interval_s must be a number >= 0'}), 400
            log.info('Wedge probe interval set to %.0fs%s', _WEDGE_PROBE_INTERVAL_S,
                     ' (reactive-only)' if _WEDGE_PROBE_INTERVAL_S == 0 else '')
        if body.get('persist'):
            try:
                cfg = _load_backend_config()
                cfg['wedge_probe_interval_s'] = _WEDGE_PROBE_INTERVAL_S
                _save_backend_config(cfg)
                persisted = True
                log.info('Wedge probe interval persisted to backend.json')
            except Exception as e:
                log.error('Could not persist wedge probe interval: %s', e)
                return jsonify({'error': f'persist failed: {e}'}), 500
    return jsonify({
        'interval_s': _WEDGE_PROBE_INTERVAL_S,
        'reactive_only': _WEDGE_PROBE_INTERVAL_S == 0,
        'recovery_interval_s': _WEDGE_RECOVERY_INTERVAL_S,
        'persisted': persisted,
    })


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
            body = _ac_backend._read()
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
    Enable/disable a heater (Auto vs Manual Off).
    Body: {"on": true|false}
    AquaConnect: direct keypad press + background setpoint read (see _ac_heater_enable).
    RS-485: menu navigation via navigator.
    """
    body = request.get_json(force=True)
    on = bool(body.get('on', False))
    if which not in ('pool', 'spa'):
        return jsonify({'error': 'which must be pool or spa'}), 400
    if _ac_backend is not None:
        return _ac_heater_enable(which, on)
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


@app.route('/lights/tester', methods=['GET'])
def lights_tester() -> Response:
    """Self-contained scene-tester page (served same-origin so it can call the
    /lights API without CORS). Buttons for all 17 named scenes, a raw-count
    calibration tester, timing controls, and a log — so lights can be tested by
    clicking + watching, no curl."""
    html = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Pool Light Scenes</title><style>
body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#111;color:#eee}
h2{margin:16px 0 8px}.row{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}
button{padding:12px 14px;font-size:15px;border:0;border-radius:10px;background:#2b6cb0;color:#fff}
button:active{opacity:.6}.fixed{background:#4a5568}.show{background:#6b46c1}
.cal button{background:#2f855a}input{width:70px;padding:8px;border-radius:8px;border:1px solid #444;background:#222;color:#eee}
label{font-size:13px;margin-right:4px}#log{white-space:pre-wrap;background:#000;padding:10px;border-radius:8px;
font-family:ui-monospace,monospace;font-size:12px;max-height:38vh;overflow:auto;margin-top:8px}
.pill{font-size:11px;opacity:.7;margin-left:6px}
</style></head><body>
<h2>Light target: <span id=bodylbl>spa</span></h2>
<div class=row>
<button id=bpool onclick="setBody('pool')">Pool light (LIGHTS)</button>
<button id=bspa onclick="setBody('spa')">Spa light (AUX_1)</button>
</div>
<h2>Named scenes <span class=pill>Hayward UCL names = POOL light. Spa is Pentair — use Raw count below.</span></h2>
<div id=scenes class=row>loading…</div>
<h2>Calibration</h2>
<div class="row cal">
<label>reset ms<input id=reset value=4000></label>
<label>off ms<input id=off value=250></label>
<label>on ms<input id=on value=250></label>
<label>offset<input id=offset value=0></label>
<button onclick=saveCal()>Save calibration</button>
</div>
<h2>Raw count (calibration)</h2>
<div class=row>
<label>count<input id=rawn value=3></label>
<button onclick=fireRaw()>Fire raw count → watch light</button>
</div>
<h2>Log</h2><div id=log></div>
<script>
const B='/lights';let body='spa';
function setBody(b){body=b;document.getElementById('bodylbl').textContent=b;
 document.getElementById('bpool').style.opacity=(b==='pool')?1:.45;
 document.getElementById('bspa').style.opacity=(b==='spa')?1:.45;
 log('target = '+b+' light ('+(b==='spa'?'AUX_1 · Pentair':'LIGHTS · Hayward UCL')+')');
 loadPrograms();}
function loadPrograms(){
 fetch(B+'/programs?body='+body).then(r=>r.json()).then(d=>{
  const c=d.calibration||{};reset.value=c.reset_ms;off.value=c.off_ms;on.value=c.on_ms;offset.value=c.offset;
  const s=document.getElementById('scenes');s.innerHTML='';
  d.programs.forEach(p=>{const btn=document.createElement('button');btn.className=p.type;
   btn.textContent=p.n+'. '+p.name;btn.onclick=()=>fire(p.n,p.name);s.appendChild(btn)})
 }).catch(e=>document.getElementById('scenes').textContent='load failed: '+e)}
const timing=()=>({reset_ms:+reset.value,off_ms:+off.value,on_ms:+on.value});
function log(m){const l=document.getElementById('log');l.textContent=new Date().toLocaleTimeString()+'  '+m+'\\n'+l.textContent}
async function post(path,obj){log('→ '+path+' '+JSON.stringify(obj));
 try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});
  const j=await r.json();log('← '+JSON.stringify(j));return j}catch(e){log('!! '+e)}}
function fire(n,name){post(B+'/'+body+'/program',Object.assign({program:n},timing())).then(()=>log('fired scene '+n+' '+name+' — WATCH: moving or static?'))}
function fireRaw(){post(B+'/'+body+'/program',Object.assign({count:+rawn.value},timing())).then(()=>log('fired RAW count '+rawn.value+' — WATCH: moving or static?'))}
function saveCal(){post(B+'/calibration',{body:body,offset:+offset.value,reset_ms:+reset.value,off_ms:+off.value,on_ms:+on.value})}
setBody(body);   // inits highlight + loads this body's program list
</script></body></html>"""
    return Response(html, mimetype='text/html')


@app.route('/lights/programs', methods=['GET'])
def lights_programs() -> Response:
    """Named light scenes with type (show=moving, fixed=static) + calibration.
    Per-body: ?body=spa -> Pentair IntelliBrite (12), else Hayward UCL pool (17).
    Also returns both lists under 'by_body' so a UI can render each."""
    body = request.args.get('body', 'pool')

    def _fmt(progs):
        return [{'n': i + 1, 'name': name, 'type': typ}
                for i, (name, typ) in enumerate(progs)]

    with state_lock:
        current = state.light_program.get(body)
    return jsonify({
        'body': body,
        'programs': _fmt(_light_programs(body)),
        'by_body': {b: _fmt(p) for b, p in LIGHT_PROGRAMS_BY_BODY.items()},
        'circuits': LIGHT_CIRCUITS,
        'calibration': dict(_light_cfg(body)),
        'mechanic': _light_mechanic(body),   # 'relative' (pool) | 'absolute' (spa)
        'current_program': current,          # last-known position (relative light)
    })


@app.route('/lights/<body>/program', methods=['POST'])
def lights_select(body: str) -> Response:
    """Switch a body's light to a scene by number or name. Body:
    {"program": 1..17} or {"name": "USA"}. Drives the pad daemon's absolute
    reset+restore power-cycle. Requires the rs485bridge backend."""
    body = body.lower()
    circuit = LIGHT_CIRCUITS.get(body)
    if circuit is None:
        return jsonify({'error': f'unknown body: {body}'}), 400
    if _ac_backend is None or not hasattr(_ac_backend, 'select_program'):
        return jsonify({'error': 'light programming needs the rs485bridge backend'}), 501

    req = request.get_json(force=True) or {}
    cfg = _light_cfg(body)
    reset_ms = float(req.get('reset_ms', cfg['reset_ms']))
    off_ms = float(req.get('off_ms', cfg['off_ms']))
    on_ms = float(req.get('on_ms', cfg['on_ms']))
    local = bool(req['local']) if 'local' in req else bool(cfg['local'])

    # The light's settled on/off state (our steady poll, not a racy read) makes
    # the daemon's reset deterministic.
    with state_lock:
        start_on = state.circuits.get(circuit)

    # Calibration mode: fire an explicit daemon restore-count, bypassing the
    # name/offset mapping, so we can find which count lands on which scene.
    if req.get('count') is not None:
        raw = _clamp(int(req['count']), 1, 17)
        with _nav_lock:
            res = _ac_backend.select_program(circuit, raw, reset_ms, off_ms, on_ms,
                                             start_on=start_on, local=local)
        if res is None:
            return jsonify({'error': 'bridge /program failed'}), 502
        return jsonify({'ok': True, 'body': body, 'raw_count': raw, 'bridge': res})

    progs = _light_programs(body)
    nprog = len(progs)
    n = req.get('program')
    if n is None and req.get('name'):
        want = str(req['name']).strip().lower()
        for i, (name, _t) in enumerate(progs):
            if name.lower() == want:
                n = i + 1
                break
        if n is None:
            return jsonify({'error': f'unknown scene name: {req["name"]}'}), 400
    try:
        n = int(n)
    except (TypeError, ValueError):
        return jsonify({'error': f'program must be 1..{nprog} or a valid name'}), 400
    if not (1 <= n <= nprog):
        return jsonify({'error': f'program must be 1..{nprog}'}), 400

    name = progs[n - 1][0]

    # RELATIVE (Hayward pool): step (target - current) mod nprog quick off/on
    # cycles from the tracked current program. Hayward has no absolute color
    # reset, so the position must be known — synced once via
    # POST /lights/<body>/sync — and the light must be ON to step it.
    if _light_mechanic(body) == 'relative':
        with state_lock:
            current = state.light_program.get(body)
        if current is None:
            return jsonify({
                'error': (f'{body} light position unknown — sync the current program '
                          f'first: POST /lights/{body}/sync {{"program": N}}'),
                'needs_sync': True}), 409
        if not start_on:
            return jsonify({
                'error': f'{body} light must be ON to change its scene (relative advance)',
                'needs_on': True}), 409
        steps = (n - current) % nprog
        if steps == 0:
            return jsonify({'ok': True, 'body': body, 'program': n, 'name': name,
                            'steps': 0, 'note': 'already on this program'})
        log.info('Light %s -> program %d (%s): relative +%d from %d',
                 body, n, name, steps, current)
        with _nav_lock:      # serialize against menu nav — both drive the panel
            res = _ac_backend.cycle(circuit, steps, off_ms, on_ms)
        if res is None:
            return jsonify({'error': 'bridge /cycle failed'}), 502
        with state_lock:
            state.light_program[body] = n
        return jsonify({'ok': True, 'body': body, 'program': n, 'name': name,
                        'steps': steps, 'from': current, 'bridge': res})

    # ABSOLUTE (Pentair spa): reset + N restores. Offset can push the count past
    # nprog (spa needs mode+1), so allow headroom for the daemon's raw count.
    count = _clamp(n + int(cfg['offset']), 1, nprog + 5)
    log.info('Light %s -> program %d (%s), daemon count=%d', body, n, name, count)
    with _nav_lock:      # serialize against menu nav — both drive the panel
        res = _ac_backend.select_program(circuit, count, reset_ms, off_ms, on_ms,
                                         start_on=start_on, local=local)
    if res is None:
        return jsonify({'error': 'bridge /program failed'}), 502
    # Track last-selected scene per body (best-effort, open-loop).
    with state_lock:
        state.light_program[body] = n
    corrected = _ensure_light_on_after_program(circuit, body)
    return jsonify({'ok': True, 'body': body, 'program': n, 'name': name,
                    'daemon_count': count, 're_asserted_on': corrected, 'bridge': res})


def _ensure_light_on_after_program(circuit: str, body: str) -> bool:
    """Parity guard for ABSOLUTE (spa) lights: a dropped/added toggle can leave
    the light OFF when a program change should end ON. Poll the settled circuit
    state and, if it ended off, do one CONFIRMED toggle to turn it back on
    (Pentair resumes the last program on power-up). Returns True if corrected.

    NOT applied to relative (pool) lights: turning a Hayward light on within
    ~10 s ADVANCES a program, which would desync the tracked position."""
    if _light_mechanic(body) != 'absolute' or _ac_backend is None:
        return False
    time.sleep(1.0)   # let the relay settle + the background poll reflect it
    with state_lock:
        on = state.circuits.get(circuit)
    if on is not False:
        return False
    try:
        with _nav_lock:
            _ac_backend.send_nav_key(_bridge_key_name(circuit))
        log.info('Light %s ended OFF after program change — re-asserted ON', body)
        return True
    except Exception as e:   # noqa: BLE001
        log.warning('re-assert light on (%s) failed: %s', body, e)
        return False


@app.route('/lights/<body>/step', methods=['POST'])
def lights_step(body: str) -> Response:
    """Relative-advance a light by `steps` (default 1) quick off/on cycles, blind
    — used to walk a Hayward pool light to a recognizable landmark (e.g. plain
    White) so it can then be synced. Requires the light ON. Body: {"steps": 1}."""
    body = body.lower()
    circuit = LIGHT_CIRCUITS.get(body)
    if circuit is None:
        return jsonify({'error': f'unknown body: {body}'}), 400
    if _ac_backend is None or not hasattr(_ac_backend, 'cycle'):
        return jsonify({'error': 'light stepping needs the rs485bridge backend'}), 501
    req = request.get_json(force=True) or {}
    steps = _clamp(int(req.get('steps', 1)), 1, 30)
    cfg = _light_cfg(body)
    with state_lock:
        start_on = state.circuits.get(circuit)
    if not start_on:
        return jsonify({'error': f'{body} light must be ON to step it', 'needs_on': True}), 409
    with _nav_lock:
        res = _ac_backend.cycle(circuit, steps, float(cfg['off_ms']), float(cfg['on_ms']))
    if res is None:
        return jsonify({'error': 'bridge /cycle failed'}), 502
    # Advance the tracked position too, if we had one.
    with state_lock:
        cur = state.light_program.get(body)
        if cur is not None:
            nprog = len(_light_programs(body))
            state.light_program[body] = ((cur - 1 + steps) % nprog) + 1
        newpos = state.light_program.get(body)
    return jsonify({'ok': True, 'body': body, 'steps': steps, 'current_program': newpos})


_ucl_reset_state: dict = {}   # body -> {'running': bool, 'done': bool}
_ucl_reset_lock = threading.Lock()


@app.route('/lights/<body>/mode-reset', methods=['GET', 'POST'])
def lights_mode_reset(body: str) -> Response:
    """Force a Hayward ColorLogic light back into UCL compatibility mode:
    4× [off ~12s, on], then leave it OFF (the caller waits ~2 min to save). Runs
    in the background (~1 min) with CONFIRMED on/off toggles so a dropped press
    can't skew the sequence. Clears the tracked position (unknown after a mode
    change). Only meaningful for the relative (Hayward pool) light.

    GET returns {'running', 'done'} so a UI can show progress without watching."""
    body = body.lower()
    if request.method == 'GET':
        with _ucl_reset_lock:
            return jsonify(_ucl_reset_state.get(body, {'running': False, 'done': False}))
    circuit = LIGHT_CIRCUITS.get(body)
    if circuit is None:
        return jsonify({'error': f'unknown body: {body}'}), 400
    if _ac_backend is None or not hasattr(_ac_backend, 'send_nav_key'):
        return jsonify({'error': 'needs the rs485bridge backend'}), 501
    key = _bridge_key_name(circuit)

    def _set(target: bool) -> bool:
        """Toggle until the circuit reaches `target` (confirmed via the poll)."""
        for _ in range(4):
            with state_lock:
                if state.circuits.get(circuit) == target:
                    return True
            _ac_backend.send_nav_key(key)
            time.sleep(1.0)
        with state_lock:
            return state.circuits.get(circuit) == target

    def _run() -> None:
        with _ucl_reset_lock:
            _ucl_reset_state[body] = {'running': True, 'done': False}
        try:
            with _nav_lock:      # serialize the whole ~1-min sequence
                _set(True)                    # ensure ON
                time.sleep(0.5)
                for i in range(4):            # 4× [off ~12s, on] -> UCL
                    _set(False)
                    time.sleep(12.0)
                    _set(True)
                    # Hold the LAST power-up long enough to read the mode-ID
                    # blink (UCL = red+white). ON duration doesn't affect the
                    # mode; only the OFF holds do, so a long dwell here is safe.
                    time.sleep(20.0 if i == 3 else 1.0)
                _set(False)                   # OFF -> begins the 2-min UCL save
            with state_lock:
                state.light_program.pop(body, None)   # position unknown now
            log.info('UCL mode-reset done for %s — leave off ~2 min to save, then sync', body)
        except Exception as e:   # noqa: BLE001
            log.warning('UCL mode-reset (%s) failed: %s', body, e)
        finally:
            with _ucl_reset_lock:
                _ucl_reset_state[body] = {'running': False, 'done': True}

    threading.Thread(target=_run, daemon=True, name=f'ucl-reset-{body}').start()
    return jsonify({'ok': True, 'body': body, 'started': True,
                    'note': ('Resetting to UCL (~1.5 min). WATCH the final power-up: '
                             'UCL blinks RED+WHITE (any other color = not UCL, run again). '
                             'It then ends OFF — leave it off ~2 min to save, then sync.')})


@app.route('/lights/<body>/sync', methods=['POST'])
def lights_sync(body: str) -> Response:
    """Tell the sidecar which program a light is CURRENTLY on, without moving it.
    Needed for the relative (Hayward pool) light so absolute selection can step
    from a known position — the user reads the light and syncs once. Body:
    {"program": N}."""
    body = body.lower()
    if body not in LIGHT_CIRCUITS:
        return jsonify({'error': f'unknown body: {body}'}), 400
    req = request.get_json(force=True) or {}
    nprog = len(_light_programs(body))
    try:
        n = int(req.get('program'))
    except (TypeError, ValueError):
        return jsonify({'error': f'program must be 1..{nprog}'}), 400
    if not (1 <= n <= nprog):
        return jsonify({'error': f'program must be 1..{nprog}'}), 400
    with state_lock:
        state.light_program[body] = n
    log.info('Light %s position synced to program %d (no move)', body, n)
    return jsonify({'ok': True, 'body': body, 'program': n, 'synced': True})


@app.route('/lights/calibration', methods=['GET', 'POST'])
def lights_calibration() -> Response:
    """Per-body light power-cycle calibration. GET ?body=spa returns that body's
    values; POST {"body":"spa", ...} updates offset/reset_ms/off_ms/on_ms/local
    for that body and persists to backend.json. Body defaults to 'pool'."""
    if request.method == 'POST':
        req = request.get_json(force=True) or {}
        which = str(req.get('body', 'pool')).lower()
        cfg = _light_cfg(which)
        for k, lo, hi in (('offset', -17, 17), ('reset_ms', 500, 20000),
                          ('off_ms', 20, 3000), ('on_ms', 20, 3000)):
            if k in req:
                cfg[k] = _clamp(float(req[k]) if 'ms' in k else int(req[k]), lo, hi)
        if 'local' in req:
            cfg['local'] = bool(req['local'])
        try:
            bconf = _load_backend_config()
            bconf['light_config'] = {b: dict(c) for b, c in LIGHT_CFG_BY_BODY.items()}
            _save_backend_config(bconf)
        except Exception as e:
            log.warning('persist light_config: %s', e)
        log.info('Light calibration (%s) updated: %s', which, cfg)
        return jsonify({'body': which, 'calibration': dict(cfg)})
    which = str(request.args.get('body', 'pool')).lower()
    return jsonify({'body': which, 'calibration': dict(_light_cfg(which))})


@app.route('/prefetch', methods=['POST'])
def prefetch_all() -> Response:
    """Read every menu-navigable value (heater setpoints, chlorinator %, VSP
    slot speeds, spa speed) in a SINGLE menu session. The plugin calls this
    once at startup instead of 5–6 separate read endpoints — far fewer menu
    entries/exits, so it's faster and a lot gentler on the bridge."""
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    # First sweep the status scroll for the live readings (fast), then the menu
    # for the deep values (setpoints, slot speeds). Scroll sweep is best-effort.
    try:
        nav.sweep_scroll()
    except Exception as e:
        log.warning(f'prefetch scroll sweep: {e}')
    try:
        return jsonify(nav.read_all_settings())
    except Exception as e:
        log.error(f'prefetch: {e}')
        # A failure to even reach the Settings menu almost always means the
        # command path is wedged. Trigger an immediate wedge probe so the
        # HomeKit sensor/plug can power-cycle the box, instead of it sitting
        # wedged until the next (now 30-min) proactive canary.
        if _ac_backend is not None:
            _immediate_wedge_probe()
        return jsonify({'error': str(e)}), 500


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


@app.route('/vsp/spa')
def get_vsp_spa() -> Response:
    """Read the Spa Speed setting from the VSP submenu."""
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        return jsonify(nav.read_spa_speed())
    except Exception as e:
        log.error(f'read_spa_speed: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/vsp/spa', methods=['POST'])
def set_vsp_spa() -> Response:
    """Set the Spa Speed setting. Body: {"speed_pct": 80}. Snaps to 5% grid."""
    body = request.get_json(force=True)
    pct = body.get('speed_pct')
    if pct is None:
        return jsonify({'error': 'speed_pct is required'}), 400
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        result = nav.set_spa_speed(int(pct))
        log.info(f'VSP spa speed -> {result["spa_speed"]}%')
        return jsonify(result)
    except Exception as e:
        log.error(f'set_spa_speed: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/chlorinator/<which>')
def get_chlorinator_which(which: str) -> Response:
    """Read pool or spa chlorinator output % via menu navigation."""
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    try:
        return jsonify(nav.read_chlorinator(which))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        log.error(f'read_chlorinator {which}: {e}')
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


@app.route('/superchlorinate/inspect')
def inspect_super_chlorinate() -> Response:
    """Navigate to Super Chlorinate in the Settings menu and return the raw frames
    seen at each step — read-only, no changes made. Used to verify frame text
    before wiring the toggle logic."""
    nav = _get_navigator()
    if nav is None:
        return jsonify({'error': 'Not connected'}), 503
    frames = []
    try:
        with _nav_lock:
            nav._anchor()
            frames.append({'key': 'anchor', 'frame': nav.text()})
            # Walk RIGHT until we land on Super Chlorinate, recording each frame.
            for i in range(nav._NAV_MAX):
                txt = nav._send('RIGHT')
                frames.append({'key': f'RIGHT x{i+1}', 'frame': txt})
                if 'Super Chlorinate' in txt:
                    # Record one PLUS press (what toggle would do) then MINUS to undo,
                    # so we can see both states without leaving a change.
                    after_plus = nav._send('PLUS')
                    frames.append({'key': 'PLUS (toggled)', 'frame': after_plus})
                    after_minus = nav._send('MINUS')
                    frames.append({'key': 'MINUS (restored)', 'frame': after_minus})
                    break
            else:
                frames.append({'key': 'error', 'frame': 'Super Chlorinate not found in Settings ring'})
        return jsonify({'frames': frames})
    except Exception as e:
        log.error(f'inspect_super_chlorinate: {e}')
        return jsonify({'error': str(e), 'frames_so_far': frames}), 500
    finally:
        try:
            nav.fast_exit()
        except Exception:
            pass


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


# Manual navigation: send ONE menu-navigation keypress on the active backend
# and return the resulting LCD text. Limited to the five nav keys so the manual
# keypad in the cockpit can't fire an equipment circuit by accident (circuits,
# heater, etc. have their own dedicated, guarded endpoints).
_MANUAL_NAV_KEYS = {'MENU', 'RIGHT', 'LEFT', 'PLUS', 'MINUS'}


@app.route('/key/<name>', methods=['POST'])
def send_key(name: str) -> Response:
    key = name.upper()
    if key not in _MANUAL_NAV_KEYS:
        return jsonify({'error': f'{name!r} is not a manual-nav key; '
                                 f'allowed: {sorted(_MANUAL_NAV_KEYS)}'}), 400
    block = _wedge_block_response()
    if block:
        return block
    try:
        if _ac_backend is not None:
            with _nav_lock:
                _ac_backend.send_nav_key(key)
                lcd_txt = lcd.text()
        else:
            nav = _get_navigator()
            if nav is None:
                return jsonify({'error': 'Not connected'}), 503
            with _nav_lock:
                lcd_txt = nav._send(key)
        _record_command_success()
        return jsonify({'ok': True, 'key': key, 'lcd': lcd_txt})
    except Exception as e:
        log.error('manual key %s: %s', key, e)
        _record_command_failure()
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Local web UI (read-only cockpit — Screen 1). Static single-page app served
# from sidecar/web/, consuming the existing /status and /stream endpoints. No
# control endpoints are called from here, so it cannot touch the panel.
# ---------------------------------------------------------------------------
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')


def _no_cache(resp: Response) -> Response:
    # The cockpit is iterated on often; without this the browser serves a stale
    # cached copy after a deploy ("I don't see my changes"). no-cache forces a
    # revalidate every load (cheap — it 304s when unchanged).
    resp.headers['Cache-Control'] = 'no-cache, must-revalidate'
    return resp


@app.route('/')
@app.route('/ui')
def web_index() -> Response:
    return _no_cache(send_from_directory(_WEB_DIR, 'index.html'))


@app.route('/ui/<path:filename>')
def web_asset(filename: str) -> Response:
    return _no_cache(send_from_directory(_WEB_DIR, filename))


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


# Seconds to let the backend settle (link up + first frames) before the
# one-shot startup pre-fetch navigates the menu.
_STARTUP_PREFETCH_SETTLE_S = 10.0
# Skip the startup menu sweep if the persisted state cache was flushed within
# this window — the menu-only values (setpoints, slot speeds) can't have gone
# stale in that time, so we avoid the menu-nav load/wedge risk on a quick restart.
_STARTUP_SKIP_SWEEP_S = 180.0


def startup_prefetch_thread() -> None:
    """One-shot: once the backend is up, read every menu-navigable value
    (heater setpoints, chlorinator %, VSP slot speeds, spa speed) in a single
    pass so they populate on EVERY sidecar restart — deploy, crash, reboot —
    not just when the Homebridge plugin restarts. Best-effort; never raises.
    """
    # Wait for a navigator (immediate on AquaConnect; after the panel connects
    # on RS-485), then let the first frames settle before navigating.
    for _ in range(120):
        if _get_navigator() is not None:
            break
        time.sleep(1)
    nav = _get_navigator()
    if nav is None:
        log.warning('Startup pre-fetch skipped: navigator unavailable')
        return
    time.sleep(_STARTUP_PREFETCH_SETTLE_S)
    # Fast: actively advance the status scroll to grab the live readings (temps,
    # salt, chlorinator, pump speed, heater auto/off, active slot) at ~1s/item
    # instead of waiting out the ~6s natural cycle.
    try:
        sweep = nav.sweep_scroll()
        log.info('Startup scroll sweep: %d frames in %.1fs',
                 sweep.get('count', 0), sweep.get('elapsed_s', 0))
    except Exception as e:
        log.warning('Startup scroll sweep failed: %s', e)
    # Conditional: skip the (expensive, wedge-prone) menu sweep when the persisted
    # cache is fresh — the menu-only values can't have changed in <3 min, and any
    # panel-side change is captured passively anyway (setpoint/slot parsers).
    if _cache_saved_at is not None:
        age = time.time() - _cache_saved_at
        if age < _STARTUP_SKIP_SWEEP_S:
            log.info('Startup menu sweep skipped — state cache is %.0fs old (<%.0fs)',
                     age, _STARTUP_SKIP_SWEEP_S)
            return
    try:
        result = nav.read_all_settings()
        got = {k: v for k, v in result.items() if v}
        log.info('Startup pre-fetch complete: %s', got)
    except Exception as e:
        log.warning('Startup pre-fetch failed: %s', e)
        # Couldn't navigate the menu at startup — probe so a wedged box is
        # flagged (sensor/plug) rather than waiting for the proactive canary.
        if _ac_backend is not None:
            _immediate_wedge_probe()


def setpoint_backfill_thread() -> None:
    """Self-heal a missing heater setpoint.

    The heater's on/off (Auto) state updates passively from the idle scroll, but
    the target °F is only readable by navigating the Settings menu. So a heater
    can read 'enabled' with a null setpoint — e.g. turned on AT THE PANEL (no
    HomeKit action to trigger a read), at startup before the sweep reached it,
    or after a failed background read. This loop notices that gap and navigates
    once to read the °F; once the setpoint is known the condition clears, so it
    stops on its own (no constant menu churn). Defers while the nav lock is busy
    or the bridge is wedged.
    """
    while True:
        time.sleep(45)
        with state_lock:
            wedged = state.bridge_wedged
            need = []
            if state.pool_heater_enabled and state.pool_setpoint_f is None:
                need.append('pool')
            if state.spa_heater_enabled and state.spa_setpoint_f is None:
                need.append('spa')
        if wedged or not need or _nav_lock.busy():
            continue
        nav = _get_navigator()
        if nav is None:
            continue
        for which in need:
            try:
                nav.read_heater(which)
                log.info('Backfilled %s heater setpoint (was on with no target)', which)
            except Exception as e:
                log.debug('setpoint backfill %s: %s', which, e)


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
    parser.add_argument('--backend', choices=['rs485', 'aquaconnect', 'rs485bridge'],
                        default='rs485',
                        help='Navigation backend: rs485 (default), aquaconnect (HTTP), '
                             'or rs485bridge (pad-Pi smart bridge over HTTP/Tailscale).')
    parser.add_argument('--aquaconnect-host', default='192.168.50.100',
                        help='AquaConnect box IP for --backend aquaconnect. Default 192.168.50.100.')
    parser.add_argument('--rs485bridge-host', default=None,
                        help='Pad-Pi bridge host for --backend rs485bridge (e.g. its '
                             'tailnet IP or "pool").')
    parser.add_argument('--rs485bridge-port', type=int, default=8899,
                        help='Pad-Pi bridge port. Default 8899.')
    parser.add_argument('--rs485bridge-token', default=os.environ.get('RS485_BRIDGE_TOKEN'),
                        help='Bearer token if the bridge requires auth (default: '
                             'RS485_BRIDGE_TOKEN env var).')
    parser.add_argument('--observe-rs485', action='store_true',
                        help='In aquaconnect mode, also run an observe-only RS-485 '
                             'listener in parallel (streams to /stream/rs485, '
                             'benchmarkable via /benchmark/rs485). Sends no keys '
                             'except during a benchmark.')
    parser.add_argument('--observe-rs485-host', default=None,
                        help='RS-485 bridge host for the parallel observer '
                             '(default: --host).')
    parser.add_argument('--observe-rs485-port', type=int, default=8899,
                        help='RS-485 bridge port for the parallel observer. Default 8899.')
    args = parser.parse_args()

    # Persisted backend selection (written by POST /backend) overrides CLI args,
    # so the plugin can switch backends and the choice survives restarts. The
    # CLI args act as the initial defaults when no config file exists yet.
    cfg = _load_backend_config()
    backend = cfg.get('backend', args.backend)
    aquaconnect_host = cfg.get('aquaconnect_host', args.aquaconnect_host)
    rs485_host = cfg.get('rs485_host', args.host)
    rs485_port = cfg.get('rs485_port', args.port)
    rs485bridge_host = cfg.get('rs485bridge_host', args.rs485bridge_host)
    rs485bridge_port = cfg.get('rs485bridge_port', args.rs485bridge_port)
    # Token is a secret — accept it from the env/CLI only, never persisted to
    # backend.json (which the plugin reads/writes).
    rs485bridge_token = args.rs485bridge_token
    # The CLI flag force-enables the observer even if backend.json persisted
    # observe_rs485=false (e.g. from a prior /backend/toggle single-transport
    # run). Persisted true also enables it. Otherwise off.
    observe_rs485 = bool(args.observe_rs485 or cfg.get('observe_rs485', False))
    observe_rs485_host = (cfg.get('observe_rs485_host')
                          or args.observe_rs485_host or rs485_host or args.host)
    observe_rs485_port = cfg.get('observe_rs485_port', args.observe_rs485_port)

    global KEY_MAX_RETRIES, KEY_VERIFY_DELAY_S, KEY_PAD_BYTES
    KEY_BURST = args.key_burst
    KEY_PREDELAY_MS = args.key_predelay_ms
    KEY_GAP_MS = args.key_gap_ms
    # Persisted key timing (POST /keytiming {persist:true}) overrides the CLI
    # --key-* args, so a value dialed in over many debug runs survives restarts.
    kt = cfg.get('key_timing') or {}
    if 'burst' in kt:         KEY_BURST = max(1, int(kt['burst']))
    if 'predelay_ms' in kt:   KEY_PREDELAY_MS = float(kt['predelay_ms'])
    if 'gap_ms' in kt:        KEY_GAP_MS = float(kt['gap_ms'])
    if 'max_retries' in kt:   KEY_MAX_RETRIES = max(1, int(kt['max_retries']))
    if 'verify_delay_s' in kt: KEY_VERIFY_DELAY_S = float(kt['verify_delay_s'])
    if 'pad_bytes' in kt:     KEY_PAD_BYTES = max(0, int(kt['pad_bytes']))
    if kt:
        log.info('Loaded persisted key timing: %s', kt)

    # Proactive wedge-probe interval (seconds; 0 = reactive-only). Persisted
    # value overrides the default.
    global _WEDGE_PROBE_INTERVAL_S
    if 'wedge_probe_interval_s' in cfg:
        try:
            _WEDGE_PROBE_INTERVAL_S = max(0.0, float(cfg['wedge_probe_interval_s']))
            log.info('Loaded persisted wedge probe interval: %.0fs%s',
                     _WEDGE_PROBE_INTERVAL_S,
                     ' (reactive-only)' if _WEDGE_PROBE_INTERVAL_S == 0 else '')
        except (TypeError, ValueError):
            log.warning('Bad wedge_probe_interval_s in backend.json: %r', cfg['wedge_probe_interval_s'])

    # Persisted cockpit UI config (enabled circuits + label overrides). The
    # plugin re-pushes on Homebridge start, but loading here means a sidecar-only
    # restart keeps the right switches instead of falling back to panel circuits.
    global _ui_circuits, _ui_circuit_labels
    if isinstance(cfg.get('ui_circuits'), list):
        _ui_circuits = [str(c).upper() for c in cfg['ui_circuits']]
    if isinstance(cfg.get('ui_circuit_labels'), dict):
        _ui_circuit_labels = {str(k).upper(): str(v) for k, v in cfg['ui_circuit_labels'].items()}
    if _ui_circuits:
        log.info('Loaded persisted UI circuits: %s', _ui_circuits)

    # Persisted light calibration. New form is per-body {'pool':{...},'spa':{...}};
    # the old flat form {offset,reset_ms,...} is migrated onto BOTH bodies.
    lc = cfg.get('light_config')
    if isinstance(lc, dict):
        keys = ('offset', 'reset_ms', 'off_ms', 'on_ms', 'local')
        if 'pool' in lc or 'spa' in lc:                 # new per-body form
            for b in ('pool', 'spa'):
                if isinstance(lc.get(b), dict):
                    for k in keys:
                        if k in lc[b]:
                            LIGHT_CFG_BY_BODY[b][k] = lc[b][k]
        else:                                            # old flat form -> both
            for b in ('pool', 'spa'):
                for k in keys:
                    if k in lc:
                        LIGHT_CFG_BY_BODY[b][k] = lc[k]
        log.info('Loaded persisted light calibration: %s', LIGHT_CFG_BY_BODY)

    global _ac_backend, _setpoint_debouncer, _active_backend
    _active_backend = backend
    # Coalesce bursts of HomeKit setpoint writes; apply only the final value.
    _setpoint_debouncer = WriteDebouncer(
        lambda which, temp_f: _apply_setpoint(which, temp_f))

    # Restore last-known bus state so the cockpit/HomeKit show real values
    # immediately on restart, then keep the cache flushed as state changes.
    _load_state_cache()
    threading.Thread(target=_state_cache_thread, daemon=True,
                     name='state-cache').start()

    # Temperature history for the cockpit chart (persisted across restarts).
    _load_temp_history()
    threading.Thread(target=_temp_history_thread, daemon=True,
                     name='temp-history').start()

    # Fault-discovery candidates persist across restarts so the backlog accrues.
    _load_fault_candidates()

    if backend == 'aquaconnect':
        _ac_backend = AquaConnectBackend(host=aquaconnect_host)
        log.info('AquaConnect backend: http://%s/WNewSt.htm', aquaconnect_host)
        threading.Thread(target=_canary_probe_loop, daemon=True,
                         name='ac-canary').start()
        # Optional parallel RS-485 observer (observe-only; never touches the
        # global state or sends keys outside a benchmark).
        if observe_rs485:
            if observe_rs485_host:
                threading.Thread(
                    target=rs485_observer_thread,
                    args=(observe_rs485_host, observe_rs485_port),
                    daemon=True, name='rs485-observer').start()
                log.info('RS-485 observer (observe-only) -> %s:%s',
                         observe_rs485_host, observe_rs485_port)
            else:
                log.warning('--observe-rs485 set but no RS-485 host given; '
                            'observer not started')
        # Read all menu-navigable values once on startup (every sidecar
        # restart), so heater setpoints / chlorinator % / VSP speeds populate
        # without waiting for a Homebridge restart.
        threading.Thread(target=startup_prefetch_thread, daemon=True,
                         name='startup-prefetch').start()
        # Self-heal a heater enabled-but-no-setpoint gap (e.g. enabled at the panel).
        threading.Thread(target=setpoint_backfill_thread, daemon=True,
                         name='setpoint-backfill').start()
        # AquaConnect mode: no RS-485 panel thread needed
        app.run(host=args.api_host, port=args.api_port, threaded=True)
        return

    if backend == 'rs485bridge':
        if not rs485bridge_host:
            parser.error('--rs485bridge-host is required for --backend rs485bridge')
        _ac_backend = RS485BridgeBackend(
            host=rs485bridge_host, port=rs485bridge_port, token=rs485bridge_token)
        log.info('RS-485 bridge backend: http://%s:%s (token=%s)',
                 rs485bridge_host, rs485bridge_port,
                 'yes' if rs485bridge_token else 'no')
        # Same menu-value prefetch + heater-setpoint backfill as AquaConnect —
        # both drive menu navigation through _ac_backend.send_nav_key, which now
        # routes to the pad bridge. No AquaConnect-box canary (nothing to wedge;
        # the daemon auto-reconnects and reports liveness via probe_wedge).
        threading.Thread(target=startup_prefetch_thread, daemon=True,
                         name='startup-prefetch').start()
        threading.Thread(target=setpoint_backfill_thread, daemon=True,
                         name='setpoint-backfill').start()
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

    # One-shot startup pre-fetch + heater setpoint backfill (skip in simulation
    # — SimPanel has no _aq for the navigator key-sends).
    if not args.simulate:
        threading.Thread(target=startup_prefetch_thread, daemon=True,
                         name='startup-prefetch').start()
        threading.Thread(target=setpoint_backfill_thread, daemon=True,
                         name='setpoint-backfill').start()

    log.info('REST API listening on %s:%s (key-burst=%d)',
             args.api_host, args.api_port, KEY_BURST)
    app.run(host=args.api_host, port=args.api_port, threaded=True)


if __name__ == '__main__':
    main()
