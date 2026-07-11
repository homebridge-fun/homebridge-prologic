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
import hmac
import json
import os
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


# --- menu-navigation LCD frames --------------------------------------------
# aqualogic's process() drops LONG_DISPLAY_UPDATE (0x04 0x0a) frames as
# "Not currently parsed". Those are the frames the panel sends DURING MENU
# NAVIGATION (full 2x16 LCD); the short DISPLAY_UPDATE (0x01 0x03) frames only
# appear on the idle scroll. Without this, _web.text_updated() never fires while
# a menu is open, so the sidecar navigator would be blind and every keypress
# would look dropped. Mirror pool_service.py's proven source-patch exactly.
def _install_long_display_patch():
    import inspect
    import textwrap
    import aqualogic.core as _aq_core

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
        '                    # packets, not text updates — skip them.\n'
        '                    if len(frame) >= 41:\n'
        '                        lcd = frame[-41:-1]  # drop header + null\n'
        '                        raw = bytes(b if b == 0xdf else (b & 0x7f) for b in lcd)\n'
        '                        text = raw.replace(b\'\\xdf\', b\'\\xc2\\xb0\').decode(\'utf-8\', errors=\'replace\')\n'
        '                        self._web.text_updated(text)'
    )
    if old_stub not in src:
        print('WARNING: LONG_DISPLAY_UPDATE stub not found in aqualogic '
              'process() — menu-nav LCD will not update. Check aqualogic==3.4.',
              flush=True)
        return
    globs = vars(_aq_core).copy()
    globs['__name__'] = _aq_core.__name__
    exec(compile(textwrap.dedent(src.replace(old_stub, new_body)),
                 inspect.getfile(AquaLogic), 'exec'), globs)
    AquaLogic.process = globs['process']
    print('LONG_DISPLAY_UPDATE patch applied: menu-nav LCD frames captured.',
          flush=True)


_install_long_display_patch()


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
        # Fast-cycle experiment: a burst-pair writes a SECOND frame `pulse_s`
        # later, in the SAME keep-alive window — a tight off->on power pulse for
        # ColorLogic (instead of one press per keep-alive, which is too slow and
        # lets the light fully power up between cycles). Inert for normal sends.
        partner = data.get('burst_partner')
        if partner is not None:
            time.sleep(data.get('pulse_s', 0.04))
            self._write(partner)

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
        self._check_latency_timer()
        self._lcd = _LcdStub()
        self._aq = None
        self._connected = False
        self._smap = {n: getattr(States, n) for n in CIRCUIT_NAMES}

    def _check_latency_timer(self):
        """The FTDI USB-serial latency timer defaults to 16ms, which buffers
        both reads and writes and makes ~2/3 of keypresses miss the panel's
        post-keep-alive accept window (33% landing). At 1ms landing is 100%.
        It resets on reboot/replug — deploy/99-ftdi-low-latency.rules makes it
        stick. Read it here (sysfs is world-readable) and warn LOUDLY if wrong,
        so a silent regression back to 16ms is caught immediately."""
        dev = os.path.basename(self._port)
        path = f'/sys/bus/usb-serial/devices/{dev}/latency_timer'
        try:
            with open(path) as f:
                val = int(f.read().strip())
        except Exception as e:  # noqa: BLE001 — non-FTDI or missing sysfs
            print(f'WARNING: could not read {path}: {e}', flush=True)
            return
        if val <= 1:
            print(f'FTDI latency_timer = {val}ms (good).', flush=True)
        else:
            print('*' * 68, flush=True)
            print(f'WARNING: FTDI latency_timer = {val}ms (should be 1). Expect '
                  '~33% keypress\n         landing until fixed. Run:\n'
                  f'         echo 1 | sudo tee {path}\n'
                  '         and install deploy/99-ftdi-low-latency.rules to persist it.',
                  flush=True)
            print('*' * 68, flush=True)

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
        if int(k.value) > 0xffff:
            # Wireless-frame key (e.g. HEATER_1): the 4-byte key value doesn't
            # fit the 2-byte REMOTE_WIRED key field. Let aqualogic build the
            # WIRELESS_KEY_EVENT frame; it queues to the same _send_queue our
            # keep-alive timing patch drains, so it gets the same accept-window
            # targeting. NOTE: HEATER_1 over direct serial is UNVERIFIED — the
            # AquaConnect path uses KeyId=13 instead; test deliberately.
            aq.send_key(k)
            self._lcd.wait_for_change(settle)
            return
        aq._send_queue.put({'frame': self._remote_frame(k)})
        # Give the panel a moment to transmit + reflect the change.
        self._lcd.wait_for_change(settle)

    def _remote_frame(self, k):
        """Build a REMOTE_WIRED key-event frame for key `k` (value <= 0xffff)."""
        return self._key_frame(k, self._aq.FRAME_TYPE_REMOTE_WIRED_KEY_EVENT)

    def _local_frame(self, k):
        """Build a LOCAL_WIRED key-event frame (what the physical keypad sends)."""
        return self._key_frame(k, self._aq.FRAME_TYPE_LOCAL_WIRED_KEY_EVENT)

    def _key_frame(self, k, frame_type):
        aq = self._aq
        frame = bytearray()
        frame.append(aq.FRAME_DLE)
        frame.append(aq.FRAME_STX)
        aq._append_data(frame, frame_type)
        aq._append_data(frame, int(k.value).to_bytes(2, byteorder='little'))
        aq._append_data(frame, int(k.value).to_bytes(2, byteorder='little'))
        crc = sum(frame)
        aq._append_data(frame, crc.to_bytes(2, byteorder='big'))
        frame.append(aq.FRAME_DLE)
        frame.append(aq.FRAME_ETX)
        return bytes(frame)

    def fast_cycle(self, key_name, count, pulse_ms, on_ms, local=False):
        """EXPERIMENT: power-cycle a toggle circuit with a TIGHT off->on pulse.

        Each cycle queues one burst-pair so the sender writes the toggle twice
        (off, then on) only `pulse_ms` apart in a SINGLE keep-alive window —
        instead of one press per keep-alive (~150ms, slow enough that the light
        fully powers up between cycles). `on_ms` is the dwell before the next
        cycle. `local` uses LOCAL_WIRED frames (what the physical keypad sends)
        vs REMOTE_WIRED. Assumes the circuit starts ON; leaves it ON. Open-loop.

        This tests whether the panel accepts two key events back-to-back in one
        window — if it does, we can cycle far faster than the keep-alive floor."""
        aq = self._aq
        if aq is None:
            raise RuntimeError('not connected')
        k = getattr(Keys, key_name, None)
        if k is None:
            raise ValueError(f'unknown key: {key_name}')
        if int(k.value) > 0xffff:
            raise ValueError(f'{key_name} is a wireless key; fast-cycle only '
                             'supports wired toggle circuits')
        frame = self._local_frame(k) if local else self._remote_frame(k)
        pulse_s = pulse_ms / 1000.0
        pairs = 0
        for i in range(count):
            aq._send_queue.put({'frame': frame, 'burst_partner': frame,
                                'pulse_s': pulse_s})
            pairs += 1
            if i < count - 1:
                time.sleep(on_ms / 1000.0)
        return {'pairs': pairs, 'frame': 'local' if local else 'remote'}

    def cycle_key(self, key_name, count, off_ms, on_ms):
        """Blind, precisely-timed power-cycle of a TOGGLE circuit (e.g. LIGHTS)
        for ColorLogic-style program advancing: press (->off), wait off_ms,
        press (->on), wait on_ms — `count` times — and leave it ON.

        Open-loop: no per-press confirmation (that's the point — it must be
        fast). ASSUMES the circuit starts ON. Each press is a REMOTE_WIRED
        toggle queued to the keep-alive-timed send path, so the *effective*
        off/on durations are quantized to ~1 keep-alive (~150ms) at minimum —
        the whole reason we calibrate off_ms/on_ms against the real light.

        Returns the number of presses queued. Only handles <=0xffff keys."""
        aq = self._aq
        if aq is None:
            raise RuntimeError('not connected')
        k = getattr(Keys, key_name, None)
        if k is None:
            raise ValueError(f'unknown key: {key_name}')
        if int(k.value) > 0xffff:
            raise ValueError(f'{key_name} is a wireless key; cycle only supports '
                             'wired toggle circuits (LIGHTS, AUX_x)')
        frame = self._remote_frame(k)
        presses = 0
        for i in range(count):
            aq._send_queue.put({'frame': frame})   # -> OFF
            presses += 1
            time.sleep(off_ms / 1000.0)
            aq._send_queue.put({'frame': frame})   # -> ON
            presses += 1
            if i < count - 1:
                time.sleep(on_ms / 1000.0)
        return presses

    def select_program(self, key_name, n, reset_ms=4000, off_ms=120, on_ms=250,
                       start_on=None):
        """Select ColorLogic program `n` (1..17) by the ABSOLUTE reset procedure:
        ensure the light is fully OFF, hold it off `reset_ms` (resets to
        baseline), then do `n` power-restores (end ON) so it lands on program n.

        The reset is now VERIFIED, not a single racy read: `start_on` (the
        sidecar's settled poll of the LIGHTS circuit) seeds the current state,
        and we confirm the light is actually off (re-reading between presses)
        before the reset hold — an unreliable reset made every count start from
        a random program. Returns timing + how the reset resolved."""
        aq = self._aq
        if aq is None:
            raise RuntimeError('not connected')
        k = getattr(Keys, key_name, None)
        if k is None:
            raise ValueError(f'unknown key: {key_name}')
        if int(k.value) > 0xffff:
            raise ValueError(f'{key_name} is a wireless key; program-cycle only '
                             'supports wired toggle circuits (LIGHTS, AUX_x)')
        frame = self._remote_frame(k)
        st = getattr(States, key_name, None)
        presses = 0

        def read_on():
            return self._get_state_safe(st) if st is not None else None

        # 1. Verified ensure-OFF. Seed from the caller's settled state; fall back
        #    to our own (settled) read. Then toggle+confirm up to 3 times.
        on = start_on
        if on is None:
            time.sleep(0.3)
            on = read_on()
        confirmed_off = (on is False)
        for _ in range(3):
            if on is False:
                confirmed_off = True
                break
            aq._send_queue.put({'frame': frame})   # toggle (expected -> OFF)
            presses += 1
            time.sleep(0.8)
            on = read_on()
            if on is None:        # can't confirm; trust the toggle
                confirmed_off = False
                break
        # 2. Hold off for the reset window.
        time.sleep(reset_ms / 1000.0)
        # 3. n power-restores, ending ON. First restore = baseline program.
        aq._send_queue.put({'frame': frame})   # -> ON (program 1)
        presses += 1
        for _ in range(n - 1):
            time.sleep(on_ms / 1000.0)
            aq._send_queue.put({'frame': frame})   # -> OFF
            presses += 1
            time.sleep(off_ms / 1000.0)
            aq._send_queue.put({'frame': frame})   # -> ON (advance)
            presses += 1
        return {'presses': presses, 'reset_confirmed_off': confirmed_off,
                'start_on': start_on}


class Handler(BaseHTTPRequestHandler):
    bridge = None  # set on the class before serving
    token = None   # optional shared secret; None = auth disabled

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        """Constant-time bearer-token check. Defense-in-depth behind the
        tailnet: even if something on the local L2 can open the socket, it
        can't drive the panel without the shared secret. /health is exempt so
        liveness probes need no secret. No token configured = open (dev only)."""
        if self.token is None:
            return True
        header = self.headers.get('Authorization', '')
        prefix = 'Bearer '
        got = header[len(prefix):] if header.startswith(prefix) else ''
        if hmac.compare_digest(got, self.token):
            return True
        self._send_json(401, {'error': 'unauthorized'})
        return False

    def do_GET(self):
        if self.path == '/health':
            self._send_json(200, {'ok': True, 'connected': self.bridge._connected})
        elif self.path == '/keys':
            # Valid aqualogic Keys enum names, so the sidecar backend can send
            # exact names (getattr(Keys, name)) instead of guessing. Open like
            # /health — it's static, non-sensitive reference data.
            self._send_json(200, {'keys': sorted(k.name for k in Keys)})
        elif self.path == '/state':
            if not self._authed():
                return
            self._send_json(200, self.bridge.snapshot())
        else:
            self._send_json(404, {'error': 'not found'})

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length) or b'{}')

    def do_POST(self):
        if self.path not in ('/key', '/cycle', '/program', '/fastcycle'):
            self._send_json(404, {'error': 'not found'})
            return
        if not self._authed():
            return
        if self.path == '/cycle':
            return self._do_cycle()
        if self.path == '/program':
            return self._do_program()
        if self.path == '/fastcycle':
            return self._do_fastcycle()
        try:
            data = self._read_body()
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

    def _do_cycle(self):
        """POST /cycle {key, count, off_ms, on_ms} — blind power-cycle a toggle
        circuit for ColorLogic light-program advancing. Bounded to avoid a
        runaway relay-mash. The circuit must already be ON."""
        try:
            data = self._read_body()
            key = data.get('key', 'LIGHTS')
            count = int(data['count'])
            off_ms = float(data['off_ms'])
            on_ms = float(data['on_ms'])
        except (ValueError, KeyError, TypeError) as e:
            self._send_json(400, {'error': f'bad request: {e}'})
            return
        # Safety bounds: cap cycles (17 programs + slack) and pulse widths.
        if not (1 <= count <= 40):
            self._send_json(400, {'error': 'count must be 1..40'})
            return
        if not (20 <= off_ms <= 5000) or not (20 <= on_ms <= 5000):
            self._send_json(400, {'error': 'off_ms/on_ms must be 20..5000'})
            return
        try:
            t0 = time.monotonic()
            presses = self.bridge.cycle_key(key, count, off_ms, on_ms)
        except (ValueError, RuntimeError) as e:
            self._send_json(400, {'error': str(e)})
            return
        self._send_json(200, {'ok': True, 'key': key, 'cycles': count,
                              'presses': presses,
                              'elapsed_ms': round((time.monotonic() - t0) * 1000)})

    def _do_program(self):
        """POST /program {key, n, reset_ms, off_ms, on_ms} — select ColorLogic
        program `n` by the absolute reset procedure (full off -> hold -> n
        restores -> leave on). Bounded to avoid a runaway relay-mash."""
        try:
            data = self._read_body()
            key = data.get('key', 'LIGHTS')
            n = int(data['n'])
            reset_ms = float(data.get('reset_ms', 4000))
            off_ms = float(data.get('off_ms', 120))
            on_ms = float(data.get('on_ms', 250))
            start_on = data.get('start_on')  # sidecar's settled LIGHTS state
        except (ValueError, KeyError, TypeError) as e:
            self._send_json(400, {'error': f'bad request: {e}'})
            return
        if not (1 <= n <= 17):
            self._send_json(400, {'error': 'n (program) must be 1..17'})
            return
        if not (500 <= reset_ms <= 20000):
            self._send_json(400, {'error': 'reset_ms must be 500..20000'})
            return
        if not (20 <= off_ms <= 3000) or not (20 <= on_ms <= 3000):
            self._send_json(400, {'error': 'off_ms/on_ms must be 20..3000'})
            return
        try:
            t0 = time.monotonic()
            info = self.bridge.select_program(key, n, reset_ms, off_ms, on_ms,
                                              start_on=start_on)
        except (ValueError, RuntimeError) as e:
            self._send_json(400, {'error': str(e)})
            return
        self._send_json(200, dict({'ok': True, 'key': key, 'program': n,
                                   'elapsed_ms': round((time.monotonic() - t0) * 1000)},
                                  **info))

    def _do_fastcycle(self):
        """POST /fastcycle {key, count, pulse_ms, on_ms, local} — EXPERIMENT:
        tight off->on power pulses (both in one keep-alive window) to cycle the
        light faster than the keep-alive floor. Circuit must start ON."""
        try:
            data = self._read_body()
            key = data.get('key', 'LIGHTS')
            count = int(data['count'])
            pulse_ms = float(data.get('pulse_ms', 40))
            on_ms = float(data.get('on_ms', 250))
            local = bool(data.get('local', False))
        except (ValueError, KeyError, TypeError) as e:
            self._send_json(400, {'error': f'bad request: {e}'})
            return
        if not (1 <= count <= 40):
            self._send_json(400, {'error': 'count must be 1..40'})
            return
        if not (2 <= pulse_ms <= 500) or not (20 <= on_ms <= 5000):
            self._send_json(400, {'error': 'pulse_ms 2..500, on_ms 20..5000'})
            return
        try:
            t0 = time.monotonic()
            info = self.bridge.fast_cycle(key, count, pulse_ms, on_ms, local)
        except (ValueError, RuntimeError) as e:
            self._send_json(400, {'error': str(e)})
            return
        self._send_json(200, dict({'ok': True, 'key': key, 'cycles': count,
                                   'pulse_ms': pulse_ms, 'on_ms': on_ms,
                                   'elapsed_ms': round((time.monotonic() - t0) * 1000)},
                                  **info))

    def log_message(self, fmt, *args):
        pass  # quiet — systemd journal would double-log otherwise


def main():
    ap = argparse.ArgumentParser(description='RS-485 smart-bridge daemon')
    ap.add_argument('--port', default='/dev/ttyUSB0', help='serial device')
    ap.add_argument('--listen', default='0.0.0.0:8899',
                    help='host:port to bind. Prefer the tailnet IP (e.g. '
                         '100.x.y.z:8899) so only authenticated tailnet peers '
                         'can reach the API; 0.0.0.0 also exposes it to the '
                         'local Wi-Fi/LAN (mitigated by --token).')
    ap.add_argument('--token', default=os.environ.get('RS485_BRIDGE_TOKEN'),
                    help='shared secret required on /state and /key as '
                         '"Authorization: Bearer <token>". Defaults to the '
                         'RS485_BRIDGE_TOKEN env var. Omit to disable auth '
                         '(dev only). /health is always open.')
    ap.add_argument('--predelay-ms', type=float, default=0.0,
                    help='DIAGNOSTIC ONLY. ms to wait after the keep-alive '
                         'before writing a key. Proven 0 is optimal on direct '
                         'serial once the FTDI latency_timer is 1ms (see '
                         'deploy/99-ftdi-low-latency.rules); >0 only delays the '
                         'write out of the accept window.')
    args = ap.parse_args()

    _install_write_timing(args.predelay_ms / 1000.0)
    print(f'Write timing: predelay={args.predelay_ms:.0f}ms', flush=True)

    host, _, port = args.listen.partition(':')
    bridge = Bridge(args.port)
    threading.Thread(target=bridge.run_forever, daemon=True).start()

    Handler.bridge = bridge
    Handler.token = args.token or None
    if Handler.token:
        print('Auth: bearer token REQUIRED on /state and /key.', flush=True)
    else:
        print('Auth: DISABLED (no --token / RS485_BRIDGE_TOKEN) — dev mode.', flush=True)
    server = ThreadingHTTPServer((host, int(port)), Handler)
    print(f'HTTP API listening on {args.listen}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
