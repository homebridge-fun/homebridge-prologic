#!/usr/bin/env python3
"""
RS-485 smart-bridge timing + reliability benchmark.

Measures the two things the pad-Pi rewrite is meant to fix, against the
rs485_bridge.py daemon's HTTP API — with NO sidecar involved:

  1. RELIABILITY — what fraction of writes actually land? The TCP/WiFi bridge
     dropped ~40% of keypresses (Nagle + relay jitter missing the panel's
     narrow post-keep-alive window). Direct serial should be ~100%.
  2. SPEED — press → confirmed-state round-trip latency.

Method (default, menu-agnostic and safe): repeatedly toggle the AUX_2 canary —
the same inert/unused output the sidecar uses for wedge probes — and confirm
each press by reading circuits['AUX_2'] back from /state. A press "landed" iff
the relay actually flipped to the expected value within --confirm-timeout.
Latency is the press→confirmed round-trip.

Stdlib only (urllib) so it runs from the pad OR the homebridge hop with no deps.

Run it from BOTH vantage points to decompose the result:
    # on the pad — pure direct-serial ceiling, no network
    python3 rs485_bench.py --url http://localhost:8899 --laps 50

    # from the hop — real deployment path (serial + Tailscale/DERP)
    python3 rs485_bench.py --url http://pool:8899 --laps 50

Compare against the documented TCP-bridge baseline in
docs/plugin-spec.md (~40% drop rate, 15-27s per nav-sweep lap).
"""
import argparse
import json
import os
import statistics
import time
import urllib.request

TOKEN = None  # set from --token / env in main()


def _auth_headers(extra=None):
    h = dict(extra or {})
    if TOKEN:
        h['Authorization'] = f'Bearer {TOKEN}'
    return h


def _get(url, path, timeout=10):
    req = urllib.request.Request(url + path, headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url, path, body, timeout=10):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url + path, data=data,
        headers=_auth_headers({'Content-Type': 'application/json'}))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _read_aux(url, key, retries=20, gap=0.1):
    """Poll /state until circuits[key] is a known bool; return it or None."""
    for _ in range(retries):
        snap = _get(url, '/state')
        v = snap.get('circuits', {}).get(key)
        if v is not None:
            return bool(v)
        time.sleep(gap)
    return None


def _pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[idx]


def main():
    ap = argparse.ArgumentParser(description='RS-485 bridge timing/reliability bench')
    ap.add_argument('--url', default='http://localhost:8899', help='bridge base URL')
    ap.add_argument('--laps', type=int, default=50, help='number of presses')
    ap.add_argument('--key', default='AUX_2', help='inert circuit to toggle')
    ap.add_argument('--settle', type=float, default=0.4,
                    help='daemon-side settle wait passed to POST /key')
    ap.add_argument('--gap', type=float, default=0.6,
                    help='delay between presses (matches sidecar min-gap)')
    ap.add_argument('--confirm-timeout', type=float, default=2.5,
                    help='max seconds to wait for the relay to reflect the press')
    ap.add_argument('--token', default=os.environ.get('RS485_BRIDGE_TOKEN'),
                    help='bearer token if the bridge requires auth '
                         '(defaults to RS485_BRIDGE_TOKEN env var)')
    args = ap.parse_args()

    global TOKEN
    TOKEN = args.token or None

    print(f'Bench target: {args.url}  key={args.key}  laps={args.laps}')
    health = _get(args.url, '/health')
    if not health.get('connected'):
        print(f'*** bridge reports not connected: {health} ***')
        return
    print(f'Health: {health}')

    prior = _read_aux(args.url, args.key)
    if prior is None:
        print(f'*** could not read initial {args.key} state — is it a valid circuit? ***')
        return
    print(f'Initial {args.key} = {prior}. Starting...\n')

    latencies = []          # round-trip seconds for LANDED presses
    landed = 0
    dropped = 0
    t_start = time.time()

    for i in range(1, args.laps + 1):
        expected = not prior
        t0 = time.time()
        try:
            _post(args.url, '/key', {'key': args.key, 'settle': args.settle})
        except Exception as e:  # noqa: BLE001
            print(f'[{i:3}] POST failed: {e!r}')
            dropped += 1
            time.sleep(args.gap)
            continue

        # Confirm the relay actually flipped.
        deadline = time.time() + args.confirm_timeout
        got = None
        while time.time() < deadline:
            got = _read_aux(args.url, args.key, retries=1)
            if got == expected:
                break
            time.sleep(0.1)
        t1 = time.time()

        if got == expected:
            landed += 1
            latencies.append(t1 - t0)
            prior = expected
            print(f'[{i:3}] LANDED  {not expected}->{expected}  {(t1 - t0) * 1000:6.0f} ms')
        else:
            dropped += 1
            # Resync prior to the true state after a miss.
            actual = _read_aux(args.url, args.key)
            prior = actual if actual is not None else prior
            print(f'[{i:3}] DROPPED (still {got})')
        time.sleep(args.gap)

    elapsed = time.time() - t_start
    total = landed + dropped

    print('\n' + '=' * 52)
    print(f'  target        : {args.url}')
    print(f'  presses       : {total}')
    print(f'  landed        : {landed}  ({100.0 * landed / total:.1f}%)')
    print(f'  dropped       : {dropped}  ({100.0 * dropped / total:.1f}%)')
    if latencies:
        print(f'  latency (landed) ms:')
        print(f'      min   {min(latencies) * 1000:7.0f}')
        print(f'      avg   {statistics.mean(latencies) * 1000:7.0f}')
        print(f'      p50   {_pct(latencies, 50) * 1000:7.0f}')
        print(f'      p90   {_pct(latencies, 90) * 1000:7.0f}')
        print(f'      max   {max(latencies) * 1000:7.0f}')
    print(f'  wall clock    : {elapsed:.1f}s  ({elapsed / total:.2f}s/press incl. {args.gap}s gap)')
    print('=' * 52)
    print('\nCompare vs TCP-bridge baseline: ~40% drop rate. Direct serial should')
    print('approach ~100% landed. Run again from the hop to add the Tailscale hop.')


if __name__ == '__main__':
    main()
