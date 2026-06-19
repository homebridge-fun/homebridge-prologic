#!/usr/bin/env python3
"""
AquaConnect timing characterization tests.

Runs three independent experiments against the AquaConnect box at
192.168.50.100 to determine safe timing parameters for _AC_MIN_GAP_S and
_AC_SETTLE_S.

  Test 1 – Press-drop threshold
    Send MENU at varying gaps, check whether the panel changed screens.
    Determines the minimum inter-press gap below which presses are dropped.

  Test 2 – Read-before-press impact
    Read (KeyId=00), wait varying delays, then press MENU.
    Determines how long after a read we must wait before a press lands.

  Test 3 – Read throughput / overload
    Send rapid KeyId=00 reads at short intervals for 30 s.
    Confirms reads don't overload the box and measures latency distribution.

Usage:
    python3 ac_characterize.py [--host 192.168.50.100] [--test 1|2|3|all]

All timings are printed as a summary table at the end. Run this while the
pool controller is idle (showing the default time/temperature screen) so the
LCD baseline is stable. The test temporarily navigates away from idle and
restores it — don't run other navigation simultaneously.
"""

import argparse
import re
import socket
import statistics
import time
from typing import Optional

HOST = '192.168.50.100'
PORT = 80
MIN_GAP = 1.8   # enforced gap before every request (this is what we're testing)

# ── Low-level transport ───────────────────────────────────────────────────────

_last_req = 0.0


def _req(key_code: str, gap: float = MIN_GAP) -> Optional[str]:
    """Send KeyId=NN& with at least `gap` seconds since the last request."""
    global _last_req
    wait = gap - (time.time() - _last_req)
    if wait > 0:
        time.sleep(wait)
    body = f'KeyId={key_code}&'
    raw = (f'POST /WNewSt.htm HTTP/1.1\r\n'
           f'Host: {HOST}\r\n'
           f'User-Agent: curl/7.88.1\r\n'
           f'Accept: */*\r\n'
           f'Content-Type: application/x-www-form-urlencoded\r\n'
           f'Content-Length: {len(body)}\r\n\r\n{body}')
    try:
        s = socket.create_connection((HOST, PORT), timeout=5)
        s.settimeout(4)
        try:
            s.sendall(raw.encode('latin-1'))
            buf = b''
            while b'\r\n\r\n' not in buf:
                c = s.recv(4096)
                if not c:
                    break
                buf += c
            head, _, rest = buf.partition(b'\r\n\r\n')
            m = re.search(rb'Content-Length:\s*(\d+)', head, re.I)
            if m:
                need = int(m.group(1))
                while len(rest) < need:
                    c = s.recv(4096)
                    if not c:
                        break
                    rest += c
            else:
                try:
                    while True:
                        c = s.recv(4096)
                        if not c:
                            break
                        rest += c
                except socket.timeout:
                    pass
            return (head + b'\r\n\r\n' + rest).decode('latin-1', errors='replace')
        finally:
            s.close()
    except Exception as e:
        print(f'  [ERROR] socket: {e}')
        return None
    finally:
        _last_req = time.time()


def _lines(body: Optional[str]) -> list:
    if not body:
        return []
    start = body.find('<body>')
    end = body.find('</body>')
    inner = body[start + 6:end] if (start != -1 and end != -1) else body
    out = []
    for ln in inner.replace('\r', '').split('\n'):
        ln = ln.strip()
        if ln.endswith('xxx'):
            ln = ln[:-3].strip()
        if ln:
            out.append(ln)
    return out


def _lcd(body: Optional[str]) -> str:
    """Return the LCD text (non-LED lines joined)."""
    led_re = re.compile(r'^[3-6CDEFcdefSTUVstuv]{6,}$')
    parts = [ln for ln in _lines(body) if not led_re.match(ln)]
    return ' '.join(parts[:2]).strip()


def rd(gap: float = MIN_GAP) -> str:
    """Read current screen with enforced gap."""
    return _lcd(_req('00', gap=gap))


def press(code: str, gap: float = MIN_GAP) -> str:
    """Send a key press with enforced gap, return resulting LCD."""
    return _lcd(_req(code, gap=gap))


# ── Anchor: navigate to Default Menu then RIGHT to idle (§13.1 fast_exit) ────

# Known menu-ring screens — anything matching this is NOT idle
MENU_RE = re.compile(
    r'(Settings Menu|Timers Menu|Diagnostic Menu|Configuration Menu|Default Menu)',
    re.I)

# Idle screens on this panel: status scroll ("Pool Chlorinator 40%",
# "Salt Level 3100 PPM", "Air: 72", weekday name, etc.)
# Anything that is NOT a named menu item is considered idle.
def _looks_idle(text: str) -> bool:
    return bool(text) and not MENU_RE.search(text)


def anchor() -> bool:
    """Navigate to Default Menu then send RIGHT to drop back to idle (§13.1).

    The panel cycles: Settings → Timers → Diagnostic → Configuration-Locked →
    Default Menu. Once we see 'Default Menu' we send RIGHT (not MENU) which
    exits the ring and returns to the status display.
    """
    print('  Anchoring to idle screen …')
    # First check: already idle?
    text = rd()
    print(f'    [check] LCD: {text!r}')
    if _looks_idle(text):
        print('    → already idle')
        return True
    # Drive to Default Menu then exit with RIGHT
    for attempt in range(1, 13):
        text = rd()
        print(f'    [{attempt}] LCD: {text!r}')
        if 'Default Menu' in text:
            # §13.1: one RIGHT exits the menu ring back to idle
            after = press('01')   # RIGHT
            print(f'    → RIGHT after Default Menu → {after!r}')
            time.sleep(0.5)
            final = rd()
            print(f'    → final: {final!r}')
            if _looks_idle(final):
                print('    → idle confirmed')
                return True
            # If RIGHT didn't drop to idle, keep looping
        else:
            press('02')   # MENU — advance toward Default Menu
    print('    → anchor failed after 12 attempts')
    return False


# ── Test 1: press-drop threshold ──────────────────────────────────────────────

def test1_press_gap(gaps_s: list) -> dict:
    """
    For each gap in gaps_s, send MENU at exactly `gap` seconds after the prior
    request, then settle+reread. A landed press shows a menu screen (idle never
    shows one on its own). The immediate POST response returns the PRE-press
    frame, so we must reread after a settle to observe the effect (this is why
    the real send_nav_key does post→settle→reread).

    Returns {gap_s: True/False} (True = press landed).
    """
    print('\n=== TEST 1: press-drop threshold ===')
    results = {}
    for gap in sorted(gaps_s):
        if not anchor():
            print(f'  gap={gap:.2f}s  ANCHOR_FAIL — skipped')
            results[gap] = None
            continue
        # Read idle baseline, press MENU at exactly `gap` s after that read,
        # then settle and reread to see if the screen entered the menu ring.
        baseline = rd()
        resp = press('02', gap=gap)          # MENU; waits exactly `gap`
        time.sleep(2.0)                       # settle (≥ debounce window)
        after = rd()                          # reread the settled screen
        landed = bool(MENU_RE.search(after))  # menu screen ⇒ press landed
        results[gap] = landed
        sym = '✓ landed' if landed else '✗ dropped'
        print(f'  gap={gap:.2f}s  base={baseline!r}  resp={resp!r}  '
              f'after={after!r}  {sym}')
    return results


# ── Test 2: read-before-press impact ─────────────────────────────────────────

def test2_read_then_press(delays_s: list) -> dict:
    """
    For each delay, do: READ → sleep(delay) → MENU.

    Returns {delay_s: True/False} (True = press landed).
    """
    print('\n=== TEST 2: read-before-press impact ===')
    results = {}
    for delay in sorted(delays_s):
        if not anchor():
            print(f'  delay={delay:.2f}s  ANCHOR_FAIL — skipped')
            results[delay] = None
            continue
        # Read, then press MENU exactly `delay` s after the read, settle, reread.
        baseline = rd()
        resp = press('02', gap=delay)
        time.sleep(2.0)
        after = rd()
        landed = bool(MENU_RE.search(after))
        results[delay] = landed
        sym = '✓ landed' if landed else '✗ dropped'
        print(f'  delay={delay:.2f}s  base={baseline!r}  resp={resp!r}  '
              f'after={after!r}  {sym}')
    return results


# ── Test 3: read throughput ───────────────────────────────────────────────────

def test3_read_flood(interval_s: float, duration_s: float = 20.0) -> dict:
    """
    Send KeyId=00 reads at `interval_s` for `duration_s` seconds.

    Returns {ok, errors, latencies_ms, p50, p95, p99, min, max}.
    """
    print(f'\n=== TEST 3: read flood (interval={interval_s:.2f}s, duration={duration_s:.0f}s) ===')
    latencies = []
    errors = 0
    deadline = time.time() + duration_s
    global _last_req
    _last_req = 0.0  # reset so first read fires immediately

    while time.time() < deadline:
        t0 = time.time()
        body = _req('00', gap=interval_s)
        elapsed_ms = (time.time() - t0) * 1000
        if body and '<body>' in body:
            latencies.append(elapsed_ms)
            lcd = _lcd(body)
            if len(latencies) % 10 == 1:
                print(f'  [{len(latencies):3d}] {elapsed_ms:.0f}ms  {lcd!r}')
        else:
            errors += 1
            print(f'  [ERR] no body, elapsed={elapsed_ms:.0f}ms')

    if latencies:
        p = lambda pct: statistics.quantiles(latencies, n=100)[pct - 1]
        result = {
            'ok': len(latencies), 'errors': errors,
            'min_ms': round(min(latencies), 1),
            'max_ms': round(max(latencies), 1),
            'p50_ms': round(p(50), 1),
            'p95_ms': round(p(95), 1),
            'p99_ms': round(p(99), 1),
        }
    else:
        result = {'ok': 0, 'errors': errors}
    print(f'  Result: {result}')
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global HOST
    ap = argparse.ArgumentParser(description='AquaConnect timing characterization')
    ap.add_argument('--host', default=HOST)
    ap.add_argument('--test', default='all', choices=['1', '2', '3', 'all'])
    args = ap.parse_args()
    HOST = args.host

    print(f'AquaConnect characterization against {HOST}')
    print(f'MIN_GAP enforcement: {MIN_GAP}s before every request in _req()')

    # Verify connectivity
    print('\nVerifying connectivity …')
    body = _req('00')
    if not body:
        print('FATAL: no response from AquaConnect box. Aborting.')
        return
    print(f'  Connected. LCD: {_lcd(body)!r}')

    summary = {}

    if args.test in ('1', 'all'):
        # Gaps to test: 0.3, 0.6, 0.9, 1.2, 1.5, 1.8, 2.1, 2.5
        # (These are the time between the previous request completion and the press)
        gaps = [0.3, 0.6, 0.9, 1.2, 1.5, 1.8, 2.1, 2.5]
        summary['test1'] = test1_press_gap(gaps)

    if args.test in ('2', 'all'):
        delays = [0.3, 0.6, 0.9, 1.2, 1.5, 1.8, 2.1, 2.5]
        summary['test2'] = test2_read_then_press(delays)

    if args.test in ('3', 'all'):
        # Read at 0.5s interval for 30 s
        summary['test3_0.5'] = test3_read_flood(interval_s=0.5, duration_s=30)
        # Read at 0.2s interval for 30 s
        summary['test3_0.2'] = test3_read_flood(interval_s=0.2, duration_s=30)

    print('\n\n=== SUMMARY ===')
    if 'test1' in summary:
        print('\nTest 1 – Press-drop threshold (gap from last request to press):')
        print(f'  {"gap_s":>6}  result')
        for g, v in sorted(summary['test1'].items()):
            s = '✓ landed' if v else ('✗ dropped' if v is False else 'SKIP')
            print(f'  {g:6.2f}  {s}')

    if 'test2' in summary:
        print('\nTest 2 – Read-before-press (delay after read before press):')
        print(f'  {"delay_s":>7}  result')
        for d, v in sorted(summary['test2'].items()):
            s = '✓ landed' if v else ('✗ dropped' if v is False else 'SKIP')
            print(f'  {d:7.2f}  {s}')

    for k in ('test3_0.5', 'test3_0.2'):
        if k in summary:
            print(f'\nTest 3 ({k}) read flood: {summary[k]}')

    print('\nDone.')


if __name__ == '__main__':
    main()
