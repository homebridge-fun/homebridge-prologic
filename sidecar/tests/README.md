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
the corpus has never seen are reported.

**`--append` writes `"reviewed": false` on purpose.** The suggested `expect`
is a snapshot of what the parser does today, so an unreviewed entry proves
only that behaviour hasn't *changed* — not that it was ever right. Read each
one and fix the `expect` where the parser is wrong; the test then fails until
the parser is fixed. That is how a bug like the 0.8.6 Super Chlorinate
countdown gets caught rather than enshrined.

`--coverage` lists which known conditions are still missing, and how to
provoke the ones you can.
