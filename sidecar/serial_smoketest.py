#!/usr/bin/env python3
"""
Standalone RS-485 direct-serial smoke test — deliberately bypasses the sidecar
entirely. Run this on the pad-mounted Pi Zero 2 W once the isolated USB-RS485
adapter is wired to the panel, BEFORE touching pool_service.py at all.

Goal: prove the physical link (adapter + wiring + isolation) works, using only
the aqualogic library's OWN built-in connect_serial() and get_state()/property
reads — nothing sidecar-specific (no frame-reader monkeypatch, no LCD text
capture). This isolates "hardware problem" from "sidecar problem": if this
script works, the wiring/adapter/isolation are good and any later issue is in
the sidecar's own logic, not the physical link.

Usage:
    pip3 install aqualogic pyserial
    python3 serial_smoketest.py /dev/ttyUSB0

What it does:
    1. Opens the serial port at 19200/8N2 (aqualogic's connect_serial default
       — matches the confirmed panel protocol, see docs/aqualogic-automation-spec.md).
    2. Runs process() in the background; prints pool/air temp and a few circuit
       states as they're decoded. Real numbers within a few seconds = READ path
       confirmed (adapter, wiring, isolation are good).
    3. After 10s of clean reads, toggles AUX_2 — the same inert/documented
       canary output the sidecar itself uses for wedge probes (confirmed safe:
       unused on this system) — and checks whether States.AUX_2 actually
       flipped. That's a real WRITE confirmation: the thing that never worked
       over the TCP bridge.
    4. Restores AUX_2 to its original state before exiting.

This script intentionally skips the sidecar's timing tuning, retries, and
frame-type experiments — just the raw library — so a failure here points at
hardware, not software.
"""
import sys
import time
import threading

try:
    from aqualogic.core import AquaLogic, States
    from aqualogic.keys import Keys
except ImportError:
    print("Missing deps. Run: pip3 install aqualogic pyserial")
    sys.exit(1)

if len(sys.argv) != 2:
    print(f"Usage: {sys.argv[0]} /dev/ttyUSB0")
    sys.exit(1)

port = sys.argv[1]
frame_count = 0


def on_change(aq):
    global frame_count
    frame_count += 1
    if frame_count <= 20 or frame_count % 10 == 0:
        print(f"[{time.strftime('%H:%M:%S')}] frame #{frame_count}  "
              f"pool_temp={aq.pool_temp}  air_temp={aq.air_temp}  "
              f"AUX_2={aq.get_state(States.AUX_2)}")


print(f"Opening {port} at 19200/8N2 ...")
aq = AquaLogic(web_port=0)
aq.connect_serial(port)
print("Connected. Reading frames for 10s (Ctrl+C to stop)...")

t = threading.Thread(target=aq.process, args=(on_change,), daemon=True)
t.start()

time.sleep(10)

if frame_count == 0:
    print("\n*** NO FRAMES RECEIVED in 10s. ***")
    print("Check: correct /dev/ttyUSBx, A/B wiring not swapped, adapter power,")
    print("baud/parity (should be 19200 8N2 — this is what connect_serial uses).")
    sys.exit(1)

print(f"\n{frame_count} frames received. READ path confirmed working "
      f"(pool_temp={aq.pool_temp}, air_temp={aq.air_temp}).")

before = aq.get_state(States.AUX_2)
print(f"\nAUX_2 currently: {before}. Sending AUX_2 keypress to test WRITE path...")
aq.send_key(Keys.AUX_2)
time.sleep(2)
after = aq.get_state(States.AUX_2)

if after != before:
    print(f"\n*** WRITE CONFIRMED. AUX_2 changed: {before} -> {after} ***")
    print("Direct serial writes are working. Safe to proceed to sidecar integration.")
    print("Restoring AUX_2 to original state...")
    aq.send_key(Keys.AUX_2)
    time.sleep(2)
    print(f"AUX_2 now: {aq.get_state(States.AUX_2)}")
else:
    print(f"\n*** WRITE DID NOT REGISTER. AUX_2 unchanged: {after} ***")
    print("Read path is fine but the keypress didn't land. This is the scenario")
    print("the frame-type sweep (local/remote/wireless) was built to diagnose —")
    print("see docs/aqualogic-automation-spec.md, key-event FRAME TYPE section.")
