#!/usr/bin/env python3
"""One-time cleanup: turn frozen-feed runs in temp_history.json into gaps.

A run where all three temps (pool, spa, air) are IDENTICAL for longer than the
threshold cannot be real data — air temperature always moves — so it's a
stale-repeat recorded during a feed outage (e.g. the pad unreachable). For each
such run we keep the first (real, last-good) sample, drop the stale repeats, and
insert a single null marker so the cockpit chart draws an honest GAP instead of
a flat line.

Dry-run by default (prints what it WOULD do). Pass --apply to write; it backs up
the original to <file>.bak first.

Usage:
  /opt/pool-sidecar/venv/bin/python sidecar/clean_temp_history.py            # dry run
  /opt/pool-sidecar/venv/bin/python sidecar/clean_temp_history.py --apply    # write

IMPORTANT: the running sidecar holds the history in memory and re-saves it every
few minutes, which would clobber the cleaned file. Restart pool-sidecar right
after --apply so it reloads the cleaned file:
  sudo systemctl restart pool-sidecar
"""
import json
import sys
import shutil
import datetime as dt

DEFAULT_PATH = '/opt/pool-sidecar/temp_history.json'
# All-three-temps-identical longer than this => frozen feed (an outage). Set at
# 6h: real overnight stability holds each integer only ~1-2h (air temp keeps
# ticking), while genuine outages run many hours to days — there's a clean gap
# between the two, so 6h removes only the true outages. Override with --hours N.
THRESHOLD_S = 6 * 3600


def main() -> None:
    apply = '--apply' in sys.argv
    threshold = THRESHOLD_S
    for a in sys.argv[1:]:
        if a.startswith('--hours='):
            threshold = float(a.split('=', 1)[1]) * 3600
    positional = [a for a in sys.argv[1:] if not a.startswith('--')]
    path = positional[0] if positional else DEFAULT_PATH

    with open(path) as fh:
        data = json.load(fh)
    fmt = lambda t: dt.datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M')

    out, runs, removed = [], [], 0
    n, k = len(data), 0
    while k < n:
        s = data[k]
        j = k
        while j + 1 < n and data[j + 1][1:4] == s[1:4]:
            j += 1
        span = data[j][0] - data[k][0]
        if j > k and span > threshold and s[1:4] != [None, None, None]:
            out.append(data[k])                              # keep last-good sample
            out.append([data[k][0] + 1, None, None, None])   # gap marker (breaks the line)
            removed += j - k
            runs.append((data[k][0], data[j][0], span, s[1:4], j - k))
            k = j + 1
        else:
            out.append(s)
            k += 1

    print(f"samples: {n} -> {len(out)}  (dropped {removed} stale rows)")
    print(f"frozen runs found: {len(runs)}")
    for t0, t1, span, vals, cnt in runs:
        print(f"  {fmt(t0)} .. {fmt(t1)}  {round(span / 3600, 1)}h  vals={vals}  dropped={cnt}")

    if not apply:
        print("\nDRY RUN — re-run with --apply to write (backs up to .bak first),"
              "\nthen: sudo systemctl restart pool-sidecar")
        return

    shutil.copy(path, path + '.bak')
    with open(path, 'w') as fh:
        json.dump(out, fh)
    print(f"\nWrote {path} (backup at {path}.bak)."
          "\nNow restart the sidecar so it reloads: sudo systemctl restart pool-sidecar")


if __name__ == '__main__':
    main()
