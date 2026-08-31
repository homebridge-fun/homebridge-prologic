# Sidecar tests

```bash
pip install -r sidecar/requirements-dev.txt
python -m pytest sidecar/tests -q
```

Runs in CI on every push. No hardware, no network.

- **`test_parse_scroll.py`** — the pure LCD parsers: scroll readings, heater
  body-routing, setpoint bounds, LED nibble decode.
- **`test_frame_corpus.py`** — replays `frames.jsonl` through the parser.

## The frame corpus

`frames.jsonl` holds frames **actually captured from a panel**, one JSON
object per line:

```json
{"name": "superchlor_countdown", "text": "Super Chlorinate 12:34 remaining", "expect": {"super_chlor_remaining": 754}, "valve_mode": null, "reviewed": true}
```

| Field | Meaning |
|---|---|
| `name` | Unique, descriptive. Shows up as the pytest test id |
| `text` | The frame verbatim, as `/display/history` returned it |
| `expect` | What `parse_ac_scroll` should produce |
| `valve_mode` | Optional — only needed for unprefixed heater lines, which route by active body |
| `reviewed` | `false` until a human has confirmed `expect` is *correct*, not merely current |

It starts empty and grows over time. Some frames only appear under conditions
you have to wait for — a real fault, a Super Chlorinate countdown, freeze
protection in winter — which is why this accrues over months rather than in
one sitting. See [`docs/testing-strategy.md`](../../docs/testing-strategy.md).

## Harvesting

**On the HOP** (where the sidecar runs — use the venv python, it has Flask):

```bash
cd /home/greg/development/homebridge-prologic
/opt/pool-sidecar/venv/bin/python scripts/harvest_frames.py
/opt/pool-sidecar/venv/bin/python scripts/harvest_frames.py --append
/opt/pool-sidecar/venv/bin/python scripts/harvest_frames.py --coverage
```

Frames are deduped by *shape* — digits tokenised, so `Pool Temp 78` and
`Pool Temp 79` are the same shape and only one is worth keeping. Only shapes
the corpus has never seen are reported. The known-conditions list used by
`--coverage` is a scorecard only; it never filters what gets captured, so a
frame nobody anticipated is still caught.

> **You don't have to be there when a rare frame appears.** The sidecar
> records every distinct shape as frames arrive and serves the ledger at
> `/display/shapes`, which survives both the 60-frame LCD ring and a restart.
> A fault that scrolled past at 3 a.m. is still listed in the morning. The
> harvester falls back to `/display/history` (last 60 frames only) if the
> sidecar predates the ledger.

**`--append` writes `"reviewed": false` on purpose.** The suggested `expect`
is a snapshot of what the parser does today, so an unreviewed entry proves
only that behaviour hasn't *changed* — not that it was ever right. Read each
one and fix the `expect` where the parser is wrong; the test then fails until
the parser is fixed. That is how a bug like the 0.8.6 Super Chlorinate
countdown gets caught rather than enshrined.

`--coverage` lists which known conditions are still missing, and how to
provoke the ones you can.

## Finding frames that need attention

`--anomalies` answers "did the panel show us something we don't understand?"
The ledger records every shape, but a shape the parser reads correctly isn't
interesting — the residue is:

| Bucket | Meaning |
|---|---|
| **NEEDS PARSER** | Matches a condition we claim to support, yet parses to nothing. A parser bug — this is the 0.8.6 Super Chlorinate class, where the frame was on screen and we silently read nothing from it |
| **UNKNOWN** | Parses to nothing, matches no known condition. Often benign (a menu header carries no data), sometimes a panel feature we don't support |
| **HANDLED ELSEWHERE** | Read by a different path — the Super Chlorinate detector, the fault detector, or the fault-candidate discovery queue. Not a gap |

That last bucket matters: `parse_ac_scroll` is not the only reader, and
without it the report would cry wolf on frames that are handled perfectly
well. A tool that cries wolf gets ignored.

Rarity is shown alongside, because it's a triage signal — a shape seen once,
days ago, is far more likely to be an unhandled condition than one seen ten
thousand times.

### The three stores, and which to look in

| File | Holds |
|---|---|
| `frame_shapes.json` | Every distinct LCD shape ever seen, with one example. Feeds `--anomalies` and harvesting |
| `fault_candidates.json` | Alert-looking frames that aren't yet known faults, awaiting promotion into `_FAULT_PHRASES` (`GET /faults/candidates`) |
| `frames.jsonl` | The curated corpus — the committed subset with reviewed expectations |

Both JSON files live beside `pool_service.py`, so in production
`/opt/pool-sidecar/`. Neither is committed.
