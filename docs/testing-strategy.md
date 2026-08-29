# Testing strategy — design sketch

> **Status:** design sketch, not built. Written to size the work and pick an
> order. Independent of the Tier 1 proposal
> ([`tier1-direct-mode-design.md`](tier1-direct-mode-design.md)) — this is
> worth doing whether or not Tier 1 ever ships.

## Why now

With one user, a regression means "I notice the pool acting weird and go fix
it." The feedback loop is the owner's own hardware, and it works.

With external users that loop breaks in specific ways:

- **Silent regressions.** A parse change that breaks salt readings on
  someone else's panel produces a bug report weeks later, described as
  "salt shows blank," with no frames to look at.
- **Configs you can't inspect.** Different `circuits`, different
  `activeBodies`, different `enable*` flags — combinations never run here.
- **Hardware you don't own.** PS-4 / PS-16, spillover systems, solar, two
  heaters, a different chlorinator. The panel's LCD text differs, and every
  parser in this codebase is regex-against-LCD-text.

The third one is the real risk, and it's the one testing addresses least
obviously — so it's worth being precise about what a test suite can and
cannot do for it. See "The multi-user payoff" below; it's the strongest
argument here and it's time-sensitive.

## What exists today

Honest baseline — **there is no automated test suite**:

- No `.github/workflows/`, no CI of any kind.
- No test runner in `package.json` (no Jest/Vitest), none in
  `sidecar/requirements.txt` (no pytest).
- `SimPanel` (`sidecar/pool_service.py`) is a genuinely good panel simulator
  — menu rings, heater states, VSP slots, the FILTER-activation window — but
  it's driven by hand via `--simulate` and read with your eyes. No
  assertions.
- `sidecar/serial_smoketest.py` is a manual script you run and eyeball.

So: real simulation infrastructure already exists, with no assertion layer
on top of it. That's a much better starting position than it sounds.

## The shape of the pyramid for *this* project

This codebase is three things stacked, and they have very different
testability:

| Layer | What it is | Testable? |
|---|---|---|
| **Parsers** | Regex tables + LED nibble decode turning LCD text into state | **Yes — trivially.** Pure string→data |
| **State machine** | `MenuNavigator`, menu rings, key sequences | **Yes — via `SimPanel`**, already built |
| **Timing / transport** | Min-gap enforcement, wedge detection, does a keypress actually land | **No.** Needs the real box, forever |

Almost every bug this project has actually hit lives in the top two layers —
the Super Chlorinate regex, the WBON `<span>` stripping, the VSP slot format
variants, the LED map, the heater setpoint parse, the `/status` deadlock.
That's where the value is, and it's the cheap part.

The bottom layer stays manual no matter how much is invested. Saying so up
front keeps the suite honest instead of growing mocks that assert our own
assumptions back at us.

---

## Tier A — pure-function tests (start here)

**Effort: small. Value: immediate.**

Some of this is testable *today with zero refactoring*:

- `_decode_ac_led(line3)` and `_ac_led_nibbles(c)` — already pure
  `str -> dict` / `str -> tuple`, no globals, no locks. Independently
  cross-confirmed against `homebridge-aqua-connect-lite`'s identical table,
  so the expected values are known-good.
- Every regex in `_AC_SCROLL_PATTERNS`, plus `_AC_HEATER_STATE_RE`,
  `_AC_HEATER_SETPOINT_RE`, `_AC_VSP_SLOT_RE`, `_SUPERCHLOR_RE`,
  `_FAULT_HINT_RE` — each is a pattern with known real input, testable in
  isolation.
- `_norm()`, the WBON tag stripping, `celsiusToFahrenheit` /
  `fahrenheitToCelsius` (TS side).

### The one refactor worth doing first

`_apply_ac_scroll_to_state(lcd)` is where most parsing lives, but it isn't
testable as written — it mutates module-global `state` under `state_lock`
and calls `_check_faults()`. Splitting parse from apply is a small,
mechanical change that unlocks the highest-value test surface in the
codebase:

```python
def parse_ac_scroll(lcd: str) -> dict:      # pure — trivially testable
    """Extract every field this frame carries. No globals, no locks."""
    ...

def _apply_ac_scroll_to_state(lcd: str) -> None:   # thin, unchanged behavior
    _check_faults(lcd)
    with state_lock:
        _merge_into_state(parse_ac_scroll(lcd))
```

Same for `_apply_ac_led_to_state`. This is maybe a ~40-line refactor that
turns the single most bug-prone part of the project into something a test
can assert on directly. **If only one thing on this page gets done, it's
this one.**

---

## Tier B — a real-frame corpus (highest value, and time-sensitive)

**Effort: small to capture, small to assert. Value: very high.**

This project is fundamentally a parser of LCD frames, and it already has the
capture path built: `GET /display/history` returns
`[{ts, text}, …]` straight from `lcd.snapshot()`, and `AquaConnectBackend`
keeps `_last_raw` for debug.

So the corpus is a `curl` away from the live system:

```
sidecar/tests/frames/
  idle_scroll_pool_mode.txt
  idle_scroll_spa_mode.txt
  heater_setpoint_pool_85.txt
  vsp_slot_selection_window.txt
  superchlor_countdown.txt
  fault_check_system_inspect_cell.txt
  ...
```

Then the tests are just: `parse_ac_scroll(frame) == expected_dict`. Fast,
hermetic, no hardware, and they catch precisely the class of bug that
actually occurs here.

**Why this is time-sensitive:** capturing real frames requires a working
panel. Doing it now is a `curl` and a commit. Doing it after a hardware
change, a move, or a failed AquaConnect box is impossible — and every parser
regression after that point becomes guesswork. This is the cheapest,
most-perishable win available.

Worth capturing deliberately: each idle-scroll variant, each menu screen the
navigator visits, both VSP formats, the startup window, a fault frame, and
anything with WBON markup.

---

## Tier C — `SimPanel` as an assertion-based suite

**Effort: medium. Value: high — it covers `MenuNavigator`, the biggest and
trickiest code in the project.**

`SimPanel` already has a clean drive surface — `send_key(name)`,
`set_circuit(name, on)`, `_render() -> (line, line)` — and already encodes
correct panel semantics learned from live observation. That's the expensive
half, already paid for. What's missing is assertions on top.

Tests this enables, none needing hardware:

- Navigate to Pool Heater from Default Menu; assert the rendered LCD matches
  the expected screen.
- Drive a setpoint step-up; assert the value moves by the right increment
  and lands.
- Assert overshoot recovery (`_press_back`) actually backs up correctly.
- Assert the FILTER-off→on VSP activation window opens and closes as
  documented.
- Assert a read never leaves the heater enabled (the `_restore_heater_off`
  invariant — a bug that actually happened once on hardware).

That last category matters most: these are **invariants**, and invariant
tests are what stop a refactor from quietly reintroducing a bug that took
live debugging to find the first time.

---

## Tier D — the plugin ↔ sidecar contract

**Effort: small. Value: high, and there's already measurable drift.**

`src/sidecarClient.ts` is a thin `axios` wrapper over the sidecar's REST
API. The TS `PoolStatus` interface (`src/settings.ts`) and the Python
`/status` payload (`pool_service.py`) are written independently, and
**nothing checks they agree**.

They have already diverged. `/status` returns these, which `PoolStatus`
doesn't declare:

`pump_startup`, `super_chlor_remaining`, `wedge_cooldown_remaining_s`,
`backend`, `circuit_labels`, `faults`, `alerts`

Today that's harmless — TS ignoring extra JSON fields is safe, and the
cockpit consumes several of them directly. But it shows the two definitions
drift freely, and the *dangerous* direction is unguarded: rename
`pool_temp` in Python and the plugin still compiles, still runs, and
silently reads `undefined` forever. With one user that's a puzzling evening.
With many, it's a wave of "temperature stopped working" reports.

Cheap fix: have the Python suite dump a `/status` payload (via Flask's test
client — no server, no hardware) to a golden JSON fixture, and have the TS
suite type-check that fixture against `PoolStatus`. Any field the plugin
depends on that the sidecar stops sending fails CI on both sides.

---

## Tier E — plugin-side (TypeScript) tests

**Effort: medium. Value: moderate.**

`src/` is small (~800 lines) and mostly HomeKit glue, but a few things are
worth pinning:

- **Accessory registration/gating** — which accessories `platform.ts`
  registers for a given config. This is pure logic over a config object and
  is exactly where the `enable*` flags (and, if built, Tier 1's backend
  gating) can silently misbehave. Testable with a mocked `api`.
- **`SidecarClient`** against a mocked HTTP layer (`nock` / `msw`) — URL
  shapes, body encoding, error handling.
- **Value mapping** — °F/°C conversion, the salt PPM → `VOCDensity` mapping,
  chlorinator % → `RotationSpeed`, the setpoint clamp. Small pure functions
  with real edge cases (the 4000 PPM `maxValue` fix lives here).

Testing accessories against a real HAP stack is possible but heavy;
`homebridge`'s own test helpers are limited. Mocking the narrow slice of
`api`/`hap` this plugin touches is the pragmatic call.

---

## What stays manual — permanently

No amount of investment automates these, and the suite shouldn't pretend
otherwise:

- Whether a keypress **physically lands** on the panel.
- The **minimum inter-request gap** and what actually wedges the box.
- **Wedge detection and recovery**, including the power-cycle path.
- RS-485 **serial timing** and the FTDI `latency_timer` behavior.
- Anything about the real AquaConnect firmware's tolerance for header
  variations.

These remain `serial_smoketest.py`-style manual verification against real
hardware. That's inherent to a reverse-engineered hardware protocol, not a
gap in the plan.

---

## The multi-user payoff

This is the part that actually addresses "hardware you don't own," and it's
the strongest argument for Tier B specifically.

A frame corpus turns the biggest multi-user risk into a testable asset. When
someone with a PS-16, a spillover system, or solar reports "salt shows
blank," the useful reply isn't a debugging back-and-forth — it's:

> Hit `http://<your-sidecar>:5757/display/history` and paste the output.

That gives real frames from hardware that will never exist here. They go
straight into `sidecar/tests/frames/` as a fixture, the parser gets fixed
against them, and **that user's configuration is regression-protected from
then on** — permanently, without owning their panel.

That converts every bug report into a durable test asset, and it's the only
mechanism here that scales support across hardware variants. It's also the
concrete reason to build Tier A + Tier B before going wide, rather than
after.

---

## CI shape

Everything in Tiers A–E runs without hardware, so CI is straightforward:

```yaml
# .github/workflows/ci.yml (sketch)
- npm ci && npm run build && npm run lint && npm test      # TS
- pip install -r sidecar/requirements-dev.txt && pytest    # Python
```

Worth adding regardless: `npm run build` and `npm run lint` are *already*
defined and nothing runs them automatically. A CI job that only did that
would have caught real breakage for free — a compile-checking job is worth
setting up on day one even before the first test exists.

Node matrix: v20/v22/v24 (Homebridge verified-plugin requirements name the
current LTS set).

---

## Suggested order

Ordered by value-per-effort, not by tier letter:

1. **CI running `build` + `lint`.** Hours. Catches real breakage today.
2. **The `parse_ac_scroll` extraction refactor** (Tier A). Small, unlocks
   everything else.
3. **Capture the frame corpus** (Tier B). Do this while the hardware is
   live — it's the perishable one.
4. **Pure-function tests** over the corpus + LED decode (Tier A/B). This is
   where most of the real bug-catching value lands.
5. **The `/status` contract fixture** (Tier D). Small, closes a live drift.
6. **`SimPanel` assertions** (Tier C). The big one — worth doing before any
   significant `MenuNavigator` refactor.
7. **Plugin-side tests** (Tier E). Do alongside Tier 1 if that gets built,
   since new backend gating is exactly what wants coverage.

Steps 1–5 are individually small and land real value on their own; none
requires finishing the ones after it. Step 6 is the largest single piece and
is the one that would make a `MenuNavigator` change safe to accept from an
outside contributor.

## Open questions

1. Is the goal "protect against my own regressions" or "let outside people
   contribute safely"? The second raises the bar — an outside PR touching
   `MenuNavigator` is only reviewable with Tier C in place.
2. Do we want the frame corpus in this repo, or a separate one? Real frames
   are low-sensitivity (LCD text — no credentials, no network detail), but
   user-contributed dumps should be eyeballed before committing.
3. Python test deps need a `requirements-dev.txt` — worth keeping separate
   so the pad Pi and sidecar installs stay lean.
