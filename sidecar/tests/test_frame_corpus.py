"""Replay the captured-frame corpus through the parser.

Every entry in `frames.jsonl` is a frame that was actually seen on hardware,
paired with what parsing it should produce. This is the regression net for
the LCD parsers: it grows as `scripts/harvest_frames.py` finds new frame
shapes, and it is the only part of the suite grounded in real panel output
rather than in what we believe the panel emits.

The corpus starts empty. That is expected -- see docs/testing-strategy.md,
"Tier B", for why it accrues over months.
"""
import json
import pathlib

import pytest

import pool_service as ps

CORPUS = pathlib.Path(__file__).parent / 'frames.jsonl'


def _load():
    if not CORPUS.exists():
        return []
    entries = []
    for n, line in enumerate(CORPUS.read_text().splitlines(), 1):
        line = line.strip()
        if line:
            entries.append((n, json.loads(line)))
    return entries


ENTRIES = _load()


def _normalise_expect(expect: dict) -> dict:
    """JSON object keys are always strings, but the parser keys vsp_slot_pct
    by int slot number. Coerce so a round-tripped corpus entry compares equal
    -- otherwise every slot frame fails on {"1": 90} != {1: 90}."""
    out = dict(expect)
    if 'vsp_slot_pct' in out:
        out['vsp_slot_pct'] = {int(k): v for k, v in out['vsp_slot_pct'].items()}
    return out


def test_corpus_is_valid_jsonl():
    """A malformed line should fail loudly here, not silently shrink the net."""
    for n, entry in ENTRIES:
        assert isinstance(entry, dict), f'line {n} is not an object'
        assert 'text' in entry, f'line {n} has no "text"'
        assert 'expect' in entry, f'line {n} has no "expect"'
        assert 'name' in entry, f'line {n} has no "name"'


def test_corpus_names_are_unique():
    names = [e['name'] for _, e in ENTRIES]
    assert len(names) == len(set(names)), 'duplicate frame names in the corpus'


@pytest.mark.skipif(not ENTRIES, reason='frame corpus is empty; nothing captured yet')
@pytest.mark.parametrize('entry', [e for _, e in ENTRIES],
                         ids=[e.get('name', '?') for _, e in ENTRIES])
def test_frame_parses_as_expected(entry):
    got = ps.parse_ac_scroll(entry['text'], entry.get('valve_mode'))
    assert got == _normalise_expect(entry['expect']), (
        f'\nframe:  {entry["text"]!r}'
        f'\nparsed: {got}'
        f'\nexpect: {entry["expect"]}'
        f'\n\nIf the parser is right, update the corpus entry. If the corpus is '
        f'right, this is the bug the corpus exists to catch.')
