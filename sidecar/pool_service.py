#!/usr/bin/env python3
"""
AquaPlus/ProLogic RS-485 sidecar service.

Connects to the USR-W610 (or Waveshare UART-WIFI232-B2) WiFi serial bridge via TCP,
maintains a persistent aqualogic session, and exposes pool state + control via a
local REST API that the Homebridge plugin polls.

Usage:
    python3 pool_service.py --host 192.168.50.XXX --port 8899 --api-port 5757
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
    heater_setpoint: Optional[float] = None
    salt_level: Optional[float] = None
    chlorinator_percent: Optional[float] = None
    connected: bool = False
    last_update: float = 0.0

state = PoolState()
state_lock = threading.Lock()
panel: Optional[AquaLogic] = None
panel_lock = threading.Lock()


# ---------------------------------------------------------------------------
# aqualogic circuit name → our canonical names (and reverse)
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


# ---------------------------------------------------------------------------
# aqualogic connection thread
# ---------------------------------------------------------------------------

def panel_thread(host: str, port: int) -> None:
    global panel

    def on_state_change(aq: AquaLogic) -> None:
        with state_lock:
            state.connected = True
            state.last_update = time.time()
            state.pool_temp = aq.pool_temp
            state.air_temp = aq.air_temp
            state.heater_setpoint = aq.pool_heater_set_point
            state.salt_level = aq.salt_level
            state.chlorinator_percent = aq.chlorinator
            for name, s in CIRCUIT_MAP.items():
                try:
                    state.circuits[name] = bool(aq.get_state(s))
                except Exception:
                    pass

    while True:
        try:
            log.info(f'Connecting to serial bridge at {host}:{port}')
            aq = AquaLogic()
            with panel_lock:
                panel = aq
            aq.connect(host, port)
            aq.add_callback(on_state_change)
            aq.process()   # blocks until connection drops
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


@app.route('/status')
def get_status() -> Response:
    with state_lock:
        return jsonify({
            'circuits':            dict(state.circuits),
            'pool_temp':           state.pool_temp,
            'air_temp':            state.air_temp,
            'heater_setpoint':     state.heater_setpoint,
            'salt_level':          state.salt_level,
            'chlorinator_percent': state.chlorinator_percent,
            'connected':           state.connected,
            'last_update':         state.last_update,
        })


@app.route('/circuit/<name>', methods=['POST'])
def set_circuit(name: str) -> Response:
    body = request.get_json(force=True)
    on: bool = bool(body.get('on', False))

    state_enum = CIRCUIT_MAP.get(name.upper())
    if state_enum is None:
        return jsonify({'error': f'Unknown circuit: {name}'}), 400

    with panel_lock:
        aq = panel
    if aq is None:
        return jsonify({'error': 'Not connected'}), 503

    try:
        aq.set_state(state_enum, on)
        log.info(f'Circuit {name} → {"ON" if on else "OFF"}')
        return jsonify({'ok': True})
    except Exception as e:
        log.error(f'set_circuit {name} failed: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/heater/setpoint', methods=['POST'])
def set_heater_setpoint() -> Response:
    body = request.get_json(force=True)
    temp_f = float(body.get('temp', 80))

    with panel_lock:
        aq = panel
    if aq is None:
        return jsonify({'error': 'Not connected'}), 503

    try:
        aq.set_pool_heater_set_point(temp_f)
        log.info(f'Heater setpoint → {temp_f}°F')
        return jsonify({'ok': True})
    except Exception as e:
        log.error(f'set_heater_setpoint failed: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/chlorinator', methods=['POST'])
def set_chlorinator() -> Response:
    body = request.get_json(force=True)
    percent = int(body.get('percent', 50))

    with panel_lock:
        aq = panel
    if aq is None:
        return jsonify({'error': 'Not connected'}), 503

    try:
        aq.set_chlorinator(percent)
        log.info(f'Chlorinator → {percent}%')
        return jsonify({'ok': True})
    except Exception as e:
        log.error(f'set_chlorinator failed: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/superchlorinate', methods=['POST'])
def set_super_chlorinate() -> Response:
    body = request.get_json(force=True)
    on: bool = bool(body.get('on', False))

    with panel_lock:
        aq = panel
    if aq is None:
        return jsonify({'error': 'Not connected'}), 503

    try:
        aq.set_state(States.SUPER_CHLORINATE, on)
        log.info(f'Super-chlorinate → {"ON" if on else "OFF"}')
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
