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

    # which frames the parser does NOT understand -- the ones to look at
    python3 scripts/harvest_frames.py --anomalies

Nothing has to be captured live by hand: the sidecar keeps a ledger of every
distinct shape it has ever seen (`/display/shapes`), which survives both the
60-frame LCD ring and a restart. A frame the panel showed at 3am is still
there in the morning. This falls back to `/display/history` -- the last 60
frames only -- when talking to a sidecar too old to have the ledger.

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
import time
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

# Control characters that are transport artifacts, not panel content: the
# serial path appends a trailing NUL to some frames and not others, which
# would otherwise split one logical frame into two shapes. Tab/newline/CR
# are excluded -- the LCD is two lines and \n separates them.
_CTRL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_WBON = re.compile(r'<[^>]+>')
_DIGITS = re.compile(r'\d+')
_WS = re.compile(r'\s+')


def plain(text: str) -> str:
    """Strip markup and collapse whitespace, keeping digits.

    Used for matching the KNOWN_CONDITIONS patterns, which need to see real
    numbers ('Heater1', '12:34'). Distinct from shape(), which tokenises
    digits away for dedupe.
    """
    return _WS.sub(' ', _WBON.sub('', _CTRL.sub('', text or ''))).strip()


def shape(text: str) -> str:
    """Reduce a frame to its shape: normalised, then numbers tokenised.

    Delegates to the sidecar's frame_shape so the two can never disagree -- if
    they did, the harvester would re-report shapes the corpus already holds.
    The local fallback only matters when pool_service can't be imported.
    """
    try:
        sys.path.insert(0, str(ROOT / 'sidecar'))
        import pool_service as ps                             # noqa: PLC0415
        return ps.frame_shape(text)
    except Exception:                                         # noqa: BLE001
        t = _CTRL.sub('', text or '')
        t = _WBON.sub('', t)
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


def _get(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def fetch_entries(base: str) -> list[dict]:
    """Full ledger entries: text plus how often and how recently seen.

    Rarity is a triage signal -- a shape seen once, days ago, is far more
    likely to be an unhandled condition than one seen ten thousand times.
    """
    try:
        return _get(base.rstrip('/') + '/display/shapes').get('shapes', [])
    except Exception as e:                                    # noqa: BLE001
        print(f'could not read the shape ledger from {base}: {e}\n'
              f'(needs a sidecar with /display/shapes)', file=sys.stderr)
        raise SystemExit(2)


# Mis-decoded serial data arrives looking like 'sw|hhhhKEQF><G' -- a single
# run of characters with no spaces, often sharing a constant tail. Real LCD
# content is a 16x2 screen of words, so it always normalises to several
# space-separated tokens. Judged at report time, never at capture: dropping
# frames on a heuristic risks discarding a genuine screen we've never seen,
# and the ledger is cheap.
_NOISE_CHARS = re.compile(r'[|<>^\\]')


def looks_like_noise(text: str) -> str:
    norm = plain(text)
    if not norm:
        return 'empty'
    if ' ' not in norm:
        return 'single unbroken token — no LCD screen looks like this'
    if _NOISE_CHARS.search(norm):
        return 'contains characters the LCD cannot display'
    return ''


def is_partial_of(a: str, b: str) -> bool:
    """Is shape `a` the same screen as `b`, caught mid-repaint?

    The LCD repaints field by field, so a screen is captured several times
    with different fields still blank:

        'Filter T<N>-all <N>:<N>A to'            <- end time not drawn yet
        'Filter T<N>-all to <N>:<N>P'            <- start time already cleared
        'Filter T<N>-all <N>:<N>A to <N>:<N>P'   <- the actual screen

    Those fragments are real frames but not distinct screens, and they bury
    the genuine discoveries. A fragment's tokens always appear in the complete
    screen, in order -- a subsequence -- so that is the test.
    """
    ta, tb = a.split(), b.split()
    if len(ta) >= len(tb) or not ta:
        return False
    # The label is drawn first and persists through the repaint, so a genuine
    # fragment always starts with the same token as the complete screen.
    # Without this, any short screen whose tokens happen to appear inside a
    # longer one gets absorbed -- the idle clock '<DAY> <N>:<N>P' was being
    # hidden as a fragment of 'Set Day and Time <DAY> <N>:<N>P', which is a
    # different screen entirely, and the most-shown one on the panel.
    if ta[0] != tb[0]:
        return False
    it = iter(tb)
    return all(tok in it for tok in ta)


def group_partials(rows):
    """Split rows into (complete, partials-of-something-complete)."""
    shapes = [(r, shape(r[0].get('text', ''))) for r in rows]
    complete, partial = [], []
    for row, sh in shapes:
        if any(sh != other and is_partial_of(sh, other) for _, other in shapes):
            partial.append(row)
        else:
            complete.append(row)
    return complete, partial


def handled_elsewhere(text: str) -> str:
    """Is this frame consumed by something other than parse_ac_scroll?

    parse_ac_scroll is not the only reader. Super Chlorinate is detected
    passively by _note_super_chlor_frame, and faults by _check_faults. Without
    this check those show up as unparsed and the triage cries wolf on frames
    that are handled perfectly well -- and a tool that cries wolf gets ignored.
    """
    try:
        sys.path.insert(0, str(ROOT / 'sidecar'))
        import pool_service as ps                             # noqa: PLC0415
        if ps._SUPERCHLOR_RE.search(text):
            return 'super-chlorinate detector'
        for phrase in getattr(ps, '_FAULT_PHRASES', ()):
            if phrase.lower() in text.lower():
                return 'fault detector (known phrase)'
        # Alert-looking but not a known phrase: the discovery path already
        # captures this to fault_candidates.json for promotion into
        # _FAULT_PHRASES. Not unhandled -- it is mid-workflow. See backlog 4.5.
        if ps._FAULT_HINT_RE.search(text):
            return 'fault-candidate discovery — see GET /faults/candidates'
    except Exception:                                         # noqa: BLE001
        pass
    return ''


def cmd_anomalies(base: str, corpus: list[dict], show_noise: bool = False) -> int:
    """Which frames does the parser not understand?

    The ledger records every shape, but a shape the parser reads correctly is
    not interesting. What matters is the residue:

      NEEDS PARSER  matches a condition we claim to support, yet parses to
                    nothing -- a parser bug. This is exactly the 0.8.6 Super
                    Chlorinate countdown class: the frame was on screen and we
                    silently read nothing from it.
      UNKNOWN       parses to nothing and matches no known condition. Often
                    benign (a menu header carries no data), sometimes a panel
                    feature we don't support yet. Worth eyeballing.
      understood    parser extracts fields -- nothing to do.
    """
    entries = fetch_entries(base)
    if not entries:
        print('shape ledger is empty — has the sidecar seen any frames yet?')
        return 0

    known = [(cid, re.compile(pat, re.I)) for cid, pat, _ in KNOWN_CONDITIONS]
    in_corpus = {shape(e.get('text', '')) for e in corpus}

    needs_parser, unknown, understood, elsewhere, noise = [], [], [], [], []
    for e in entries:
        text = e.get('text', '')
        parsed = try_parse(text)
        hits = [cid for cid, rx in known if rx.search(plain(text))]
        junk = looks_like_noise(text)
        other = handled_elsewhere(text)
        if parsed:
            understood.append((e, hits))
        elif other:
            elsewhere.append((e, [other]))
        elif junk and not hits:
            noise.append((e, [junk]))
        elif hits:
            needs_parser.append((e, hits))
        else:
            unknown.append((e, hits))

    def show(rows, label):
        if not rows:
            return
        print(f'{label} ({len(rows)})')
        for e, hits in sorted(rows, key=lambda r: r[0].get('count', 0)):
            age = ''
            last = e.get('last_seen')
            if last:
                mins = max(0, int((time.time() - last) / 60))
                age = f', last seen {mins}m ago' if mins < 1440 else \
                      f', last seen {mins // 1440}d ago'
            mark = '' if shape(e.get('text', '')) in in_corpus else '  [not in corpus]'
            print(f"  {e.get('text','')!r}")
            print(f"      seen x{e.get('count', 0)}{age}"
                  f"{'  matches: ' + ','.join(hits) if hits else ''}{mark}")
        print()

    print(f'{len(entries)} distinct shapes in the ledger\n')
    # A header caught before its value arrived is not a parser bug.
    needs_parser, np_partial = group_partials(needs_parser)
    unknown, un_partial = group_partials(unknown)
    partials = np_partial + un_partial

    show(needs_parser, 'NEEDS PARSER — recognised condition, but nothing parsed')
    show(unknown, 'UNKNOWN — nothing parsed, no known condition')
    if partials:
        print(f'PARTIAL RENDERS — same screen caught mid-repaint ({len(partials)})')
        print(f"  e.g. {partials[0][0].get('text','')!r}")
        print('  Fragments of a complete screen listed above; not separate '
              'findings.\n')
    show(elsewhere, 'HANDLED ELSEWHERE — read by another path, not parse_ac_scroll')
    if noise:
        print(f'LIKELY NOISE — mis-decoded serial, not LCD content ({len(noise)})')
        print(f"  e.g. {noise[0][0].get('text','')!r}  ({noise[0][1][0]})")
        if show_noise:
            print()
            show(noise, '  all noise frames')
        else:
            print('  Not worth capturing. Re-run with --show-noise to list '
                  'them all.\n')
    if not needs_parser and not unknown:
        print('every shape the panel has shown is understood by the parser.')
    else:
        print('Add anything worth pinning to the corpus with --append, then fix '
              'the parser\nand correct the expect. See sidecar/tests/README.md.')
    print(f'\nunderstood by parse_ac_scroll: {len(understood)}'
          f'   handled by another reader: {len(elsewhere)}')
    return 0


def fetch(base: str) -> list[str]:
    """Frames to consider, newest source first.

    /display/shapes is the ledger the sidecar builds as frames arrive: every
    distinct shape it has ever seen, surviving both the 60-frame ring and a
    restart. /display/history is the fallback for a sidecar too old to have
    the ledger -- it only holds the last 60 frames, so rare frames will have
    aged out of it.
    """
    base = base.rstrip('/')
    try:
        body = _get(base + '/display/shapes')
        shapes = body.get('shapes', [])
        if shapes:
            print(f'source: /display/shapes ({len(shapes)} shapes ever seen)\n')
            return [e.get('text', '') for e in shapes if e.get('text')]
        print('source: /display/shapes (ledger empty — sidecar restarted?)\n')
        return []
    except Exception:                                         # noqa: BLE001
        pass                                                  # fall back below

    try:
        body = _get(base + '/display/history')
    except Exception as e:                                    # noqa: BLE001
        print(f'could not reach {base}: {e}\n\n'
              f'Run this on the host where the sidecar is running, or pass\n'
              f'--url http://<sidecar-host>:5757', file=sys.stderr)
        raise SystemExit(2)
    print('source: /display/history — only the last 60 frames. Update the '
          'sidecar for\nthe full shape ledger.\n', file=sys.stderr)
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
    ap.add_argument('--show-noise', action='store_true',
                    help='list the frames classified as mis-decoded noise')
    ap.add_argument('--anomalies', action='store_true',
                    help='shapes the parser does not understand — the ones '
                         'that may need attention')
    ap.add_argument('--coverage', action='store_true',
                    help='report which known conditions are captured; no network')
    args = ap.parse_args()

    corpus = load_corpus()
    if args.coverage:
        return cmd_coverage(corpus)
    if args.anomalies:
        return cmd_anomalies(args.url, corpus, args.show_noise)

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
