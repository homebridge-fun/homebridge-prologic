#!/usr/bin/env python3
"""
AquaPlus/ProLogic RS-485 sidecar service.

Connects to the USR-W610 (or Waveshare UART-WIFI232-B2) WiFi serial bridge via TCP,
maintains a persistent aqualogic session, and exposes pool state + control via a
local REST API that the Homebridge plugin polls.

Usage:
    python3 pool_service.py --host 192.168.50.XXX --port 8899 --api-port 5757

NOTE ON CAPABILITIES (verified against swilson/aqualogic):
  * Reads available: pool/air/spa temp, salt, chlorinator %, pump speed, and the
    boolean state of every circuit.
  * Circuit ON/OFF works for circuits that have a corresponding keypad key:
    POOL/SPA (shared POOL_SPA toggle, mutually exclusive), FILTER, LIGHTS,
    AUX_1, AUX_2.
  * The heater cannot be toggled via HEATER_1 directly. The only heater control
    the library exposes is HEATER_AUTO_MODE, so "heater on/off" is routed there.
  * The library exposes NO heater set-point (neither read nor write) and NO
    chlorinator-percent setter, and there is no key for SUPER_CHLORINATE or
    SPILLOVER. Those write endpoints return 501/422 rather than silently lying.
"""

import argparse
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from flask import Flask, jsonify, request, Response
from aqualogic.core import AquaLogic
from aqualogic.states import States

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
log = logging.getLogger('pool_service')


# ---------------------------------------------------------------------------
# State container shared between the aqualogic thread and Flask handlers
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
    connected: bool = False
    last_update: float = 0.0

state = PoolState()
state_lock = threading.Lock()
panel: Optional[AquaLogic] = None
panel_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Circuit name -> aqualogic States enum.
#
# These are the states we READ. Writes are handled separately because not every
# state is writable (see set_circuit / SETTABLE_VIA_AUTO_MODE below).
# ---------------------------------------------------------------------------

CIRCUIT_MAP = {
    'POOL':             States.POOL,
    'SPA':              States.SPA,
    'FILTER':           States.FILTER,
    'LIGHTS':           States.LIGHTS,
    'SPILLOVER':        States.SPILLOVER,
    'AUX_1':            States.AUX_1,
    'AUX_2':            States.AUX_2,
    'HEATER_1':         States.HEATER_1,
    'SUPER_CHLORINATE': States.SUPER_CHLORINATE,
}


def _read_property(aq, name, default=None):
    """Safely read an aqualogic property; never let one bad read abort the rest."""
    try:
        return getattr(aq, name)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# aqualogic connection thread
# ---------------------------------------------------------------------------

def panel_thread(host: str, port: int) -> None:
    global panel

    def on_state_change(aq: AquaLogic) -> None:
        with state_lock:
            state.connected = True
            state.last_update = time.time()
            state.pool_temp = _read_property(aq, 'pool_temp')
            state.air_temp = _read_property(aq, 'air_temp')
            state.spa_temp = _read_property(aq, 'spa_temp')
            state.salt_level = _read_property(aq, 'salt_level')
            state.chlorinator_percent = _read_property(aq, 'pool_chlorinator')
            state.pump_speed = _read_property(aq, 'pump_speed')
            for name, s in CIRCUIT_MAP.items():
                try:
                    state.circuits[name] = bool(aq.get_state(s))
                except Exception:
                    pass

    while True:
        try:
            log.info(f'Connecting to serial bridge at {host}:{port}')
            aq = AquaLogic()
            aq.connect(host, port)
            with panel_lock:
                panel = aq
            # process() requires the data-changed callback and blocks until EOF.
            aq.process(on_state_change)
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
# Flask REST API
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.logger.setLevel(logging.WARNING)  # suppress Flask request logs


def _get_panel():
    with panel_lock:
        return panel


@app.route('/status')
def get_status() -> Response:
    with state_lock:
        return jsonify({
            'circuits':            dict(state.circuits),
            'pool_temp':           state.pool_temp,
            'air_temp':            state.air_temp,
            'spa_temp':            state.spa_temp,
            # No heater set-point is available from the controller via aqualogic.
            'heater_setpoint':     None,
            'salt_level':          state.salt_level,
            'chlorinator_percent': state.chlorinator_percent,
            'pump_speed':          state.pump_speed,
            'connected':           state.connected,
            'last_update':         state.last_update,
        })


@app.route('/circuit/<name>', methods=['POST'])
def set_circuit(name: str) -> Response:
    body = request.get_json(force=True)
    on: bool = bool(body.get('on', False))
    key = name.upper()

    state_enum = CIRCUIT_MAP.get(key)
    if state_enum is None:
        return jsonify({'error': f'Unknown circuit: {name}'}), 400

    # The heater cannot be driven via HEATER_1 (set_state returns False).
    # The only library-supported heater control is HEATER_AUTO_MODE.
    if state_enum == States.HEATER_1:
        state_enum = States.HEATER_AUTO_MODE

    aq = _get_panel()
    if aq is None:
        return jsonify({'error': 'Not connected'}), 503

    try:
        ok = aq.set_state(state_enum, on)
        if not ok:
            log.warning(f'Circuit {key} is not controllable on this system')
            return jsonify({
                'error': f'{key} cannot be toggled on this controller '
                         f'(no corresponding keypad key).'
            }), 422
        log.info(f'Circuit {key} -> {"ON" if on else "OFF"}')
        return jsonify({'ok': True})
    except Exception as e:
        log.error(f'set_circuit {key} failed: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/heater/setpoint', methods=['POST'])
def set_heater_setpoint() -> Response:
    # The aqualogic library exposes no heater set-point read or write. Changing
    # the target temperature requires menu navigation (PLUS/MINUS keys), which is
    # intentionally not implemented. Fail honestly instead of pretending.
    return jsonify({
        'error': 'Heater set-point control is not supported. The aqualogic '
                 'library cannot read or set the target temperature; adjust it '
                 'at the physical panel.'
    }), 501


@app.route('/chlorinator', methods=['POST'])
def set_chlorinator() -> Response:
    # Chlorinator output % is readable (see /status) but the library has no
    # setter for it.
    return jsonify({
        'error': 'Setting chlorinator output is not supported by the aqualogic '
                 'library. The current output % is available in /status.'
    }), 501


@app.route('/superchlorinate', methods=['POST'])
def set_super_chlorinate() -> Response:
    body = request.get_json(force=True)
    on: bool = bool(body.get('on', False))

    aq = _get_panel()
    if aq is None:
        return jsonify({'error': 'Not connected'}), 503

    try:
        ok = aq.set_state(States.SUPER_CHLORINATE, on)
        if not ok:
            return jsonify({
                'error': 'Super-chlorinate cannot be toggled on this controller '
                         '(no corresponding keypad key).'
            }), 422
        log.info(f'Super-chlorinate -> {"ON" if on else "OFF"}')
        return jsonify({'ok': True})
    except Exception as e:
        log.error(f'set_super_chlorinate failed: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health() -> Response:
    with state_lock:
        connected = state.connected
        age = time.time() - state.last_update if state.last_update else None
    status_code = 200 if connected else 503
    return jsonify({'connected': connected, 'data_age_seconds': age}), status_code


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description='AquaPlus/ProLogic RS-485 sidecar service')
    parser.add_argument('--host', required=True, help='IP address of the WiFi serial bridge')
    parser.add_argument('--port', type=int, default=8899, help='TCP port of the serial bridge')
    parser.add_argument('--api-port', type=int, default=5757, help='Port for the local REST API')
    parser.add_argument('--api-host', default='127.0.0.1', help='Bind address for REST API')
    args = parser.parse_args()

    t = threading.Thread(target=panel_thread, args=(args.host, args.port), daemon=True, name='aqualogic')
    t.start()

    log.info(f'REST API listening on {args.api_host}:{args.api_port}')
    app.run(host=args.api_host, port=args.api_port, threaded=True)


if __name__ == '__main__':
    main()
