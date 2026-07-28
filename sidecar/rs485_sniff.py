#!/usr/bin/env python3
"""
RS-485 raw bus sniffer — decides whether exactly-once keypress targeting is
possible (see the period-3 accept-slot finding in the bench).

Reads the panel's RS-485 bus directly (pyserial only, no aqualogic, no daemon)
and dumps every framed packet: DLE STX <type> <data...> <crc> DLE ETX, with
DLE-stuffing (0x10 0x00 -> literal 0x10) undone. Prints each frame's 2-byte
type + hex payload, then a per-type histogram and, for the most common
(keep-alive) frame type, whether any byte position VARIES across frames.

Why: the bench showed the panel accepts a wired-remote keypress only ~1 in 3
keep-alive slots (a hard ~33% single-shot ceiling). If the keep-alive frames
carry a rotating device/address byte, we can lock onto the wired-remote slot
and transmit exactly once for ~100% landing. If every keep-alive is byte-for-
byte identical, the slot rotation is timing-only and we need a different fix.

Usage:
    python3 rs485_sniff.py /dev/ttyUSB0 --seconds 6

IMPORTANT: stop the bridge daemon first (only one reader per serial port):
    pkill -f rs485_bridge.py
"""
import argparse
import collections
import sys
import time

try:
    import serial
except ImportError:
    raise SystemExit("Missing pyserial. Run: pip3 install --break-system-packages pyserial")

DLE, STX, ETX = 0x10, 0x02, 0x03


def frames(ser, seconds):
    """Yield raw unstuffed frame payloads (bytes between STX and ETX,
    type bytes included) seen within `seconds`."""
    deadline = time.time() + seconds
    buf = bytearray()
    in_frame = False
    prev_dle = False
    while time.time() < deadline:
        chunk = ser.read(64)
        if not chunk:
            continue
        for b in chunk:
            if prev_dle:
                prev_dle = False
                if b == STX:
                    buf = bytearray()
                    in_frame = True
                elif b == ETX:
                    if in_frame:
                        yield bytes(buf)
                    in_frame = False
                elif b == 0x00:
                    if in_frame:
                        buf.append(DLE)  # stuffed literal DLE
                # else: other control after DLE, ignore
                continue
            if b == DLE:
                prev_dle = True
                continue
            if in_frame:
                buf.append(b)


def main():
    ap = argparse.ArgumentParser(description='RS-485 raw bus sniffer')
    ap.add_argument('port', help='serial device, e.g. /dev/ttyUSB0')
    ap.add_argument('--seconds', type=float, default=6.0)
    ap.add_argument('--show', type=int, default=40, help='max frames to print live')
    args = ap.parse_args()

    ser = serial.Serial(args.port, 19200, bytesize=8, parity='N',
                        stopbits=2, timeout=0.2)
    print(f'Sniffing {args.port} at 19200/8N2 for {args.seconds}s ...\n')

    by_type = collections.defaultdict(list)
    ka_times = []          # arrival times of keep-alive (0101) frames
    printed = 0
    t0 = time.time()
    for fr in frames(ser, args.seconds):
        if len(fr) < 2:
            continue
        now = time.time()
        ftype = fr[:2]
        by_type[ftype].append(fr)
        if ftype == b'\x01\x01':
            ka_times.append(now)
        if printed < args.show:
            dt = (now - t0) * 1000
            print(f'  +{dt:7.1f}ms  type={ftype.hex()}  len={len(fr):2}  {fr.hex()}')
            printed += 1

    print('\n' + '=' * 60)
    print('Frame-type histogram (type = first 2 bytes):')
    total = sum(len(v) for v in by_type.values())
    for ftype, frs in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        n = len(frs)
        # crude keep-alive cadence: mean gap if this is the dominant type
        print(f'  type={ftype.hex()}  count={n:4}  ({100.0 * n / total:.1f}%)')

    # For the dominant (likely keep-alive) type, report per-byte variation.
    if by_type:
        dom = max(by_type, key=lambda k: len(by_type[k]))
        frs = by_type[dom]
        print(f'\nMost common type {dom.hex()} ({len(frs)} frames) — per-byte variation:')
        maxlen = max(len(f) for f in frs)
        distinct = set(f for f in frs)
        print(f'  distinct full-frame values: {len(distinct)}')
        if len(distinct) == 1:
            print('  -> every keep-alive is IDENTICAL: no address byte to target.')
            print('     Slot rotation is timing-only; exactly-once needs a')
            print('     different approach (option B, closed-loop resend).')
        else:
            print('  -> keep-alives DIFFER. Varying byte positions (index: values):')
            for i in range(maxlen):
                vals = collections.Counter(f[i] for f in frs if len(f) > i)
                if len(vals) > 1:
                    shown = ', '.join(f'{v:#04x}x{c}' for v, c in vals.most_common(8))
                    print(f'     byte[{i}]: {shown}')
            print('     -> if a byte cycles through a small fixed set, that is the')
            print('        device-slot address; lock onto the wired-remote value.')

    # Keep-alive cadence — the REAL send-rate floor (one queued frame per KA).
    # Run with FTDI latency_timer=1 for accurate timestamps; at the 16ms default
    # read-buffering batches frames and distorts these gaps.
    if len(ka_times) > 3:
        gaps = sorted((ka_times[i + 1] - ka_times[i]) * 1000
                      for i in range(len(ka_times) - 1))
        n = len(gaps)
        med = gaps[n // 2]
        p10 = gaps[max(0, n // 10)]
        p90 = gaps[min(n - 1, (9 * n) // 10)]
        print(f'\nKeep-alive interval (n={n + 1} KA frames over {args.seconds}s):')
        print(f'  min={gaps[0]:.1f}ms  p10={p10:.1f}ms  median={med:.1f}ms  '
              f'p90={p90:.1f}ms  max={gaps[-1]:.1f}ms')
        print(f'  -> rate ~ {1000.0 / med:.0f} keep-alives/sec = the true'
              ' one-press-per-KA send floor.')
        print(f'  (Read with latency_timer='
              f'{_read_latency(args.port)}ms — must be 1 for these to be accurate.)')
    print('=' * 60)


def _read_latency(port):
    import os
    try:
        with open(f'/sys/bus/usb-serial/devices/{os.path.basename(port)}/latency_timer') as f:
            return f.read().strip()
    except Exception:
        return '?'


if __name__ == '__main__':
    main()
