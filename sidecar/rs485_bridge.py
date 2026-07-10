#!/usr/bin/env python3
"""
RS-485 smart-bridge daemon — runs on the pad-mounted Pi Zero 2 W.

Piece 1 of the smart-bridge split (see docs/plugin-spec.md §2). This daemon
OWNS the physical serial link and its timing: it opens the USB-RS485 adapter
with aqualogic's connect_serial() and runs process() locally, so a queued
keypress is transmitted in the panel's narrow sub-100ms post-keep-alive window
with zero network latency — the exact thing every TCP/WiFi bridge missed
(~40% drops from Nagle + relay jitter).

It exposes a tiny stdlib HTTP API (no Flask — keep the Zero 2 W light) that the
main sidecar's RS485BridgeBackend consumes over Tailscale:

    GET  /state   → JSON snapshot: LCD text + decoded circuits/temps/LEDs.
    POST /key     → {"key":"RIGHT"} queue one REMOTE_WIRED nav key, then
                    return a fresh /state after a short settle so the caller's
                    frame-reader gets immediate feedback.
    GET  /health  → {"ok": true} liveness (systemd / manual check).

The daemon does NOT know the menu structure — all navigation logic stays in the
sidecar's MenuNavigator. This is the "smart bridge, thin protocol" split: the
bridge owns serial + timing, the sidecar owns semantics.

Usage:
    pip3 install --break-system-packages 'aqualogic==3.4' pyserial
    python3 rs485_bridge.py --port /dev/ttyUSB0 --listen 0.0.0.0:8899

Run it under systemd on the pad (see deploy/ for the unit). Point the sidecar's
rs485bridge backend at http://<pad-tailscale-ip>:8899.
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from aqualogic.core import AquaLogic, States
    from aqualogic.keys import Keys
except ImportError:
    raise SystemExit(
        "Missing deps. Run: pip3 install --break-system-packages 'aqualogic==3.4' pyserial")

# Circuits the sidecar tracks; mirrors CIRCUIT_NAMES in pool_service.py.
CIRCUIT_NAMES = [
    'POOL', 'SPA', 'FILTER', 'LIGHTS',
    'SPILLOVER', 'AUX_1', 'AUX_2', 'HEATER_1', 'SUPER_CHLORINATE',
]

# Properties read straight off the aqualogic object each snapshot.
PROPERTIES = ['pool_temp', 'air_temp', 'spa_temp', 'salt_level',
              'pool_chlorinator', 'pump_speed']


# --- aqualogic 3.4 bug fix -------------------------------------------------
# _write_to_serial() calls self._serial.send(data), but pyserial's Serial has
# no .send() — only .write(). Confirmed via serial_smoketest.py on hardware
# (reads worked; the first write raised AttributeError). Patch at class level.
def _write_to_serial_fixed(self, data):
    self._serial.write(data)
    self._serial.flush()


AquaLogic._write_to_serial = _write_to_serial_fixed


# --- write-window timing ---------------------------------------------------
# aqualogic's default _send_frame() transmits a queued key the instant process()
# hands it over, which only coincides with the panel's wired-remote accept slot
# ~1-in-3 keep-alive cycles (observed as a deterministic period-3 drop pattern
# on the bench). pool_service.py fixes this by waiting KEY_PREDELAY_MS into the
# panel's post-keep-alive accept window before writing once. We port just that
# timing here (the daemon owns serial, so no network jitter competes with it).
# The default 70ms is the WiFi-bridge center; direct serial may want re-tuning,
# so it's a CLI knob (--predelay-ms) swept via rs485_bench.py.
_PREDELAY_S = 0.070


def _install_write_timing(predelay_s):
    global _PREDELAY_S
    _PREDELAY_S = predelay_s

    def _send_frame_timed(self):
        if self._send_queue.empty():
            return
        data = self._send_queue.get(block=False)
        frame = data['frame']
        # Wait for the panel's post-keep-alive accept window, then write once.
        time.sleep(_PREDELAY_S)
        self._write(frame)

    AquaLogic._send_frame = _send_frame_timed


class _LcdStub:
    """Captures every LCD frame aqualogic decodes. aqualogic >=3.x calls
    self._web.text_updated(text) on every frame even with web_port=0, so a
    real object (not None) must be assigned to aq._web before process() runs;
    without it the background thread dies on the first frame."""
    def __init__(self):
        self._lock = threading.Lock()
        self._latest = ''
        self._ts = 0.0
        self._event = threading.Event()

    def text_updated(self, text):
        with self._lock:
            self._latest = text
            self._ts = time.time()
        self._event.set()

    # aqualogic may call other no-op methods on its _web object; absorb them.
    def __getattr__(self, name):
        return lambda *a, **k: None

    def latest(self):
        with self._lock:
            return self._latest, self._ts

    def wait_for_change(self, timeout):
        self._event.clear()
        return self._event.wait(timeout)


class Bridge:
    def __init__(self, port):
        self._port = port
        self._lcd = _LcdStub()
        self._aq = None
        self._connected = False
        self._smap = {n: getattr(States, n) for n in CIRCUIT_NAMES}

    # --- connection / read loop -------------------------------------------
    def run_forever(self):
        """Reconnect loop: open the port, run process() until it returns/raises,
        then retry. process() blocks the calling thread reading frames."""
        while True:
            try:
                print(f'Opening {self._port} at 19200/8N2 ...', flush=True)
                aq = AquaLogic(web_port=0)
                aq._web = self._lcd
                aq.connect_serial(self._port)
                self._aq = aq
                self._connected = True
                print('Connected. Reading frames (process loop).', flush=True)
                aq.process(self._on_change)
            except Exception as e:  # noqa: BLE001 — daemon must survive any read error
                print(f'process() loop ended: {e!r}; reconnecting in 5s', flush=True)
            self._connected = False
            time.sleep(5)

    def _on_change(self, aq):
        # State is read on demand in snapshot(); nothing to do per-frame beyond
        # the LcdStub capture that already happened. Kept as the process()
        # callback so aqualogic has a sink.
        pass

    # --- decode -----------------------------------------------------------
    def _get_state_safe(self, state):
        """aqualogic's get_state() scans the send queue and can KeyError if a
        send_key-queued frame (no 'desired_states' key) hasn't been sent yet —
        a narrow library race, not a real error. Mirrors pool_service.py."""
        try:
            return self._aq.get_state(state)
        except Exception:
            return None

    def _read_prop(self, name):
        try:
            return getattr(self._aq, name)
        except Exception:
            return None

    def snapshot(self):
        aq = self._aq
        text, ts = self._lcd.latest()
        snap = {
            'connected': self._connected and aq is not None,
            'ts': ts,
            'lcd': text,
            'circuits': {},
            'valve_mode': None,
        }
        if aq is None:
            return snap
        for name in PROPERTIES:
            snap[name] = self._read_prop(name)
        # Normalize the chlorinator field name to what the sidecar expects.
        snap['chlorinator_percent'] = snap.pop('pool_chlorinator', None)
        for name, s in self._smap.items():
            v = self._get_state_safe(s)
            if v is not None:
                snap['circuits'][name] = bool(v)
        # circuits['HEATER_1'] = armed/Auto mode (not the firing relay).
        auto = self._get_state_safe(States.HEATER_AUTO_MODE)
        if auto is not None:
            snap['circuits']['HEATER_1'] = bool(auto)
        # heater_active = the firing relay (States.HEATER_1), distinct from armed.
        heater_active = self._get_state_safe(States.HEATER_1)
        if heater_active is not None:
            snap['heater_active'] = bool(heater_active)
        # Valve mode from the cycling default display. On this hardware the LCD
        # has no newline, so the whole frame is one string — substring match is
        # safe ("Spa Mode"/"Pool Mode" ≠ "Spa Temp"/"Spa-CountDn").
        if 'Pool Mode' in text:
            snap['valve_mode'] = 'pool'
        elif 'Spa Mode' in text:
            snap['valve_mode'] = 'spa'
        return snap

    # --- write ------------------------------------------------------------
    def send_key(self, key_name, settle=0.35):
        """Queue one REMOTE_WIRED key-event frame, then wait briefly for the LCD
        to change so the caller's frame-reader gets immediate feedback.

        REMOTE_WIRED (not LOCAL_WIRED) is required: RIGHT/LEFT/PLUS/MINUS are
        dead with LOCAL frames on this panel (confirmed empirically over the TCP
        bridge and re-confirmed on direct serial). Mirrors pool_service.py's
        MenuNavigator._send_key_remote exactly."""
        aq = self._aq
        if aq is None:
            raise RuntimeError('not connected')
        k = getattr(Keys, key_name, None)
        if k is None:
            raise ValueError(f'unknown key: {key_name}')
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
        # Give the panel a moment to transmit + reflect the change.
        self._lcd.wait_for_change(settle)


class Handler(BaseHTTPRequestHandler):
    bridge = None  # set on the class before serving

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            self._send_json(200, {'ok': True, 'connected': self.bridge._connected})
        elif self.path == '/state':
            self._send_json(200, self.bridge.snapshot())
        else:
            self._send_json(404, {'error': 'not found'})

    def do_POST(self):
        if self.path != '/key':
            self._send_json(404, {'error': 'not found'})
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length) or b'{}')
            key = data['key']
            settle = float(data.get('settle', 0.35))
        except (ValueError, KeyError, TypeError) as e:
            self._send_json(400, {'error': f'bad request: {e}'})
            return
        try:
            self.bridge.send_key(key, settle=settle)
        except (ValueError, RuntimeError) as e:
            self._send_json(400, {'error': str(e)})
            return
        # Return a fresh snapshot so the caller sees the post-press frame.
        self._send_json(200, self.bridge.snapshot())

    def log_message(self, fmt, *args):
        pass  # quiet — systemd journal would double-log otherwise


def main():
    ap = argparse.ArgumentParser(description='RS-485 smart-bridge daemon')
    ap.add_argument('--port', default='/dev/ttyUSB0', help='serial device')
    ap.add_argument('--listen', default='0.0.0.0:8899', help='host:port to bind')
    ap.add_argument('--predelay-ms', type=float, default=70.0,
                    help='ms to wait into the panel post-keep-alive accept '
                         'window before writing a key (sweep with rs485_bench)')
    args = ap.parse_args()

    _install_write_timing(args.predelay_ms / 1000.0)
    print(f'Write timing: predelay={args.predelay_ms:.0f}ms', flush=True)

    host, _, port = args.listen.partition(':')
    bridge = Bridge(args.port)
    threading.Thread(target=bridge.run_forever, daemon=True).start()

    Handler.bridge = bridge
    server = ThreadingHTTPServer((host, int(port)), Handler)
    print(f'HTTP API listening on {args.listen}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
