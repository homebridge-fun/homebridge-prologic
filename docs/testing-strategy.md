# Testing strategy — design sketch

> **Status:** design sketch, not built. Written to size the work and pick an
> order. Independent of the proposed "Tier 1" direct-mode backend — this is
> worth doing whether or not that ever ships.

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
argument here, and it's the one that takes calendar time rather than effort.

## What exists today

Honest baseline — **there is CI, but no automated test suite**:

- `.github/workflows/ci.yml` exists as of 0.9.2: lint + build on Node 22/24,
  a Python syntax check, and `scripts/check_docs.py`. That catches broken
  builds and stale docs — it asserts nothing about behaviour.
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

## Tier B — a real-frame corpus

**Effort: small per frame, spread over months. Value: very high.**

This project is fundamentally a parser of LCD text, and the capture path
already exists: `GET /display/history` returns `[{ts, text}, …]` straight
from `lcd.snapshot()`, and `AquaConnectBackend` keeps `_last_raw`.

A corpus entry pairs a real frame with what parsing it should produce:

```jsonl
{"name": "superchlor_countdown", "text": "Super Chlorinate  12:34 remaining",
 "expect": {"super_chlor_remaining_s": 45240},
 "captured": "2026-08-30", "note": "while SC actively running"}
```

One appendable `frames.jsonl`, one test that iterates it asserting
`parse_ac_scroll(text) == expect`. Fast, hermetic, no hardware.

### Why this can't be done in one sitting

The reason to start early is **not** that the hardware might disappear —
that's possible but not imminent, and it's the wrong way to think about it.
The real constraint is that **many frames only exist under conditions you
have to wait for or deliberately create:**

| Frame | When it appears |
|---|---|
| Super Chlorinate countdown | Only while SC is running |
| `Check System` / `Inspect Cell` fault | Only during an actual fault |
| VSP slot-selection window | Only in the ~5–10 s after FILTER off→on |
| Startup delay (`St dly`) | Only while the pump is spinning up |
| Spa-mode scroll variants | Only while in spa mode |
| Heater actively firing | Only while calling for heat |
| **Freeze protection** | **Only in cold weather** |
| Low/high salt warnings | Only at those salt levels |

You cannot capture a freeze-protection frame in August, and you cannot
convincingly invent one — the exact text is what the parser matches on, and
guessing it produces a test that passes against fiction. A complete corpus
**accrues over seasons**, which is the actual argument for starting now.

> **As-is caveat — the current tooling does not actually deliver this.**
> `/display/history` returns `lcd.snapshot()`, a `deque(maxlen=60)`: the last
> 60 frames, on the order of a few minutes. Harvesting is a manual one-shot
> pull with nothing capturing in the background, so catching a rare frame
> requires running the harvest within minutes of it appearing. The common
> idle-scroll frames are unaffected — they recur constantly — but the rare
> ones this section argues are most valuable are precisely the ones that will
> be missed. "Accrues over months" describes the intent, not today's
> behaviour. Backlog 1.7 closes the gap by capturing novel shapes in the
> sidecar as frames arrive.

This has already bitten us. The Super Chlorinate OFF bug (0.8.6) was
precisely a frame that only appears while the countdown is running: the
`HH:MM remaining` text wasn't recognised as "on", so OFF silently sent no
keypress. A corpus frame captured during a countdown would have caught it
before it shipped.

### What's actually needed

1. **A harvest script** — `scripts/harvest_frames.py`: pull
   `/display/history`, normalise, drop anything already in the corpus, and
   print only genuinely new frame *shapes* for labelling. Without dedupe this
   is drudgery and won't get done; with it, harvesting is a minute's work.
2. **The corpus file** — `sidecar/tests/frames.jsonl`, append-only.
3. **A coverage report** — the same script listing which known conditions are
   still uncaptured, so "what's missing" is visible rather than remembered.
4. **The parser test** — one loop over the corpus.

The workflow that follows is: leave the sidecar running as it already does,
harvest occasionally, and deliberately provoke the cheap conditions (toggle
FILTER to catch the VSP window, run SC for a minute, switch to spa mode).
The rare ones — faults, freeze protection — get picked up whenever they
happen naturally, which is exactly why the harvest step needs to be low
friction.

### Seeding it from bugs we've already had

The CHANGELOG is a list of **confirmed reachable failure states**, which is
much stronger evidence than invented test cases. Writing one test per
historical bug is probably the highest-yield hour available here:

| Bug | Version | What would have caught it |
|---|---|---|
| Super Chlorinate OFF was a silent no-op — matched `>On<` markup that's stripped upstream | 0.8.3 | Parser test on a real captured frame |
| SC OFF failed while counting down — `HH:MM remaining` not recognised as "on" | 0.8.6 | Corpus frame captured *during* a countdown |
| Setting spa chlorinator % overwrote the **pool's** cached value | 0.8.9 | Pure unit test on the state-write |
| Hitting a hardware floor/ceiling recorded the *requested* value, not the clamped one | 0.8.9 | `SimPanel` write test |
| Heater setpoint write threw `KeyError: 'was_off'` on the success path | 0.8.5 | Unit test of the write's return shape |
| Salt reading clamped at 1000 PPM by the HAP default | — | Value-mapping unit test |
| Two different VSP slot text formats | — | Two corpus frames |
| `/status` self-deadlocked on `state_lock`, taking every tile offline | — | Concurrency test (see below) |
| "Active Heat" tile name stuck showing the wrong body | 0.9.0 | **Nothing** — HAP-side behaviour; correctly out of scope |

The pattern is worth noting: almost every one is a parser or
state-recording bug. That is the evidence for Tier A + Tier B being the
priority, rather than a judgement call.

### The deadlock class deserves its own tests

The `/status` self-deadlock — it called `_wedge_cooling_down()` while
already holding the non-reentrant `state_lock` — was the most severe outage
this project has had: every accessory unresponsive, not one wrong reading.
That class is testable without hardware:

- Hit `/status` from several threads via Flask's test client and assert it
  always returns.
- Assert no route handler acquires `state_lock` reentrantly (a debug wrapper
  that records the owning thread makes this a direct assertion).

Cheap, and it covers the failure mode with the worst blast radius.

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

> These are folded into [`backlog.md`](backlog.md) alongside the non-testing
> work; that page is the master ordering.

Ordered by value-per-effort, not by tier letter:

1. **CI running `build` + `lint`.** Hours. Catches real breakage today.
2. **The `parse_ac_scroll` extraction refactor** (Tier A). Small, unlocks
   everything else.
3. **Start harvesting the frame corpus** (Tier B). Begin early not because
   the hardware may vanish, but because some frames only appear seasonally
   or during rare conditions — the corpus accrues over months.
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
