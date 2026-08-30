#!/usr/bin/env python3
"""Harvest new LCD frame shapes from a running sidecar into the test corpus.

The corpus (`sidecar/tests/frames.jsonl`) pairs real panel frames with what
parsing them should produce. Its value depends entirely on containing frames
that were actually seen on hardware -- a hand-invented frame produces a test
that passes against fiction. See docs/testing-strategy.md, "Tier B".

Why this script exists: `/display/history` returns raw frames, and the panel
emits the same handful of *shapes* over and over with different numbers in
them. Without dedupe, harvesting means eyeballing hundreds of near-identical
lines and it won't get done. So frames are reduced to a SHAPE -- digits
replaced with a token -- and only shapes the corpus has never seen are shown.

    Pool Temp  78                     -> Pool Temp <N>
    Pool Temp  79                     -> Pool Temp <N>        (same shape)
    Super Chlorinate 12:34 remaining  -> Super Chlorinate <N>:<N> remaining

Usage (on the HOP, where the sidecar runs):

    # what's new since the last harvest
    python3 scripts/harvest_frames.py

    # append the new ones to the corpus, marked for review
    python3 scripts/harvest_frames.py --append

    # what conditions are still missing (no sidecar needed)
    python3 scripts/harvest_frames.py --coverage

The suggested `expect` for a new frame is a SNAPSHOT of what the parser
currently produces -- not an oracle. Review it: if the parser is wrong for
that frame, correct the `expect` and the test will fail until the parser is
fixed. That is exactly how a bug like the 0.8.6 Super Chlorinate countdown
would have been caught.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = ROOT / 'sidecar' / 'tests' / 'frames.jsonl'

# Conditions worth having in the corpus. Patterns are matched against the
# frame text with markup stripped but DIGITS INTACT -- not against shape(),
# whose <N> tokens would swallow the '1' in 'Heater1'. Some conditions can be
# provoked on demand; others only appear when the panel decides to, which is
# why the corpus accrues over months rather than in one sitting.
KNOWN_CONDITIONS = [
    ('pool_temp',            r'Pool Temp',                    'idle scroll'),
    ('air_temp',             r'Air Temp',                     'idle scroll'),
    ('spa_temp',             r'Spa Temp',                     'idle scroll'),
    ('salt_level',           r'Salt Level',                   'idle scroll'),
    ('pool_chlorinator',     r'Pool Chlorinator',             'idle scroll'),
    ('spa_chlorinator',      r'Spa Chlorinator',              'switch to spa mode'),
    ('pump_speed',           r'Filter Speed\s+\d+\s*%',       'idle scroll'),
    ('vsp_active_scroll',    r'Filter Speed\s+\d+\s*%\s+Speed\d', 'idle scroll'),
    ('vsp_active_window',    r'Filter On:Spd\d',              'toggle FILTER off then on'),
    ('vsp_slot_pct',         r'Filter Speed[1-4]\D+\d+\s*%',  'Settings > VSP'),
    ('heater_auto',          r'Heater1\s+Auto Control',       'enable the heater'),
    ('heater_manual_off',    r'Heater1\s+Manual Off',         'disable the heater'),
    ('heater_setpoint',      r'Heater1\D*\d{2,3}\s*\xb0?\s*F', 'Settings > Pool/Spa Heater'),
    ('superchlor_countdown', r'Super\s*Chlorinate\s+\d{1,2}:\d{2}', 'run Super Chlorinate'),
    ('pump_startup',         r'St\s?dly',                     'start the pump'),
    ('fault',                r'Check System|Inspect Cell',    'wait for a real fault'),
    ('freeze_protect',       r'Freeze',                       'wait for cold weather'),
]

_WBON = re.compile(r'<[^>]+>')
_DIGITS = re.compile(r'\d+')
_WS = re.compile(r'\s+')


def plain(text: str) -> str:
    """Strip markup and collapse whitespace, keeping digits.

    Used for matching the KNOWN_CONDITIONS patterns, which need to see real
    numbers ('Heater1', '12:34'). Distinct from shape(), which tokenises
    digits away for dedupe.
    """
    return _WS.sub(' ', _WBON.sub('', text or '')).strip()


def shape(text: str) -> str:
    """Reduce a frame to its shape: markup stripped, numbers tokenised.

    This is the dedupe key. Two frames that differ only in their readings are
    the same shape and only the first is worth capturing.
    """
    t = _WBON.sub('', text or '')
    t = _DIGITS.sub('<N>', t)
    return _WS.sub(' ', t).strip()


def load_corpus() -> list[dict]:
    if not CORPUS.exists():
        return []
    out = []
    for n, line in enumerate(CORPUS.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f'{CORPUS.name}:{n}: not valid JSON -- {e}', file=sys.stderr)
    return out


def fetch(base: str) -> list[str]:
    url = base.rstrip('/') + '/display/history'
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            body = json.load(r)
    except Exception as e:                                    # noqa: BLE001
        print(f'could not reach {url}: {e}\n\n'
              f'Run this on the host where the sidecar is running, or pass\n'
              f'--url http://<sidecar-host>:5757', file=sys.stderr)
        raise SystemExit(2)
    return [e.get('text', '') for e in body.get('history', []) if e.get('text')]


def try_parse(text: str):
    """Best-effort: what does the current parser make of this frame?

    Importing pool_service needs Flask, which the system python on the pad
    may not have. Without it we still harvest, just without a suggested
    `expect` -- so the script stays useful anywhere.
    """
    try:
        sys.path.insert(0, str(ROOT / 'sidecar'))
        import pool_service as ps                             # noqa: PLC0415
        return ps.parse_ac_scroll(text)
    except Exception:                                         # noqa: BLE001
        return None


def cmd_coverage(corpus: list[dict]) -> int:
    texts = [plain(e.get('text', '')) for e in corpus]
    shapes = [shape(e.get('text', '')) for e in corpus]
    have, missing = [], []
    for cid, pattern, provoke in KNOWN_CONDITIONS:
        rx = re.compile(pattern, re.I)
        (have if any(rx.search(t) for t in texts) else missing).append((cid, provoke))

    print(f'corpus: {len(corpus)} frames, {len(set(shapes))} distinct shapes')
    print(f'covered: {len(have)}/{len(KNOWN_CONDITIONS)}\n')
    if have:
        print('captured:')
        for cid, _ in have:
            print(f'  [x] {cid}')
    if missing:
        print('\nstill needed:')
        for cid, provoke in missing:
            print(f'  [ ] {cid:22} {provoke}')

    unreviewed = [e for e in corpus if e.get('reviewed') is False]
    if unreviewed:
        print(f'\n{len(unreviewed)} entr{"y" if len(unreviewed) == 1 else "ies"} '
              f'still marked "reviewed": false — check the expect values:')
        for e in unreviewed:
            print(f'  - {e.get("name", "?")}')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', default='http://127.0.0.1:5757',
                    help='sidecar base URL (default: %(default)s)')
    ap.add_argument('--append', action='store_true',
                    help='append new frames to the corpus, marked reviewed:false')
    ap.add_argument('--coverage', action='store_true',
                    help='report which known conditions are captured; no network')
    args = ap.parse_args()

    corpus = load_corpus()
    if args.coverage:
        return cmd_coverage(corpus)

    seen = {shape(e.get('text', '')) for e in corpus}
    new: dict[str, str] = {}
    for text in fetch(args.url):
        s = shape(text)
        if s and s not in seen and s not in new:
            new[s] = text

    if not new:
        print(f'no new frame shapes ({len(seen)} already in the corpus)')
        return 0

    print(f'{len(new)} new frame shape(s):\n')
    entries = []
    for i, (s, text) in enumerate(sorted(new.items()), 1):
        parsed = try_parse(text)
        entry = {
            'name': f'TODO_{i}',
            'text': text,
            'expect': parsed if parsed is not None else {},
            'reviewed': False,
        }
        entries.append(entry)
        print(f'  shape:  {s}')
        print(f'  text:   {text!r}')
        print(f'  parses: {parsed if parsed is not None else "(parser unavailable)"}')
        print()

    if args.append:
        with CORPUS.open('a') as fh:
            for e in entries:
                fh.write(json.dumps(e) + '\n')
        print(f'appended {len(entries)} to {CORPUS.relative_to(ROOT)}')
        print('Now: give each a real name, and REVIEW the expect values — they '
              'are a snapshot of\ncurrent parser output, not proof it is right. '
              'Then set "reviewed": true.')
    else:
        print('Re-run with --append to add these, or paste them into '
              f'{CORPUS.relative_to(ROOT)} by hand.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
