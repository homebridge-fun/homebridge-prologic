# Backlog

The single prioritized list of open work. Detail lives in the linked docs;
this page is the ordering.

- **Design sketches:** [`testing-strategy.md`](testing-strategy.md)
- **Deep reference:** [`plugin-spec.md`](plugin-spec.md) §10.2 is the historical
  record of what's been *done*; open items moved here.

Ordered by value-per-effort within each group. Nothing below is committed to.

---

## 1 — Next up

Small, high-leverage, each lands value on its own.

| # | Item | Size | Why now |
|---|---|---|---|
| ~~1.1~~ | ~~**Extract `parse_ac_scroll`**~~ | — | **Done.** Split into a pure `parse_ac_scroll(lcd, valve_mode) -> dict` plus a thin fold. Behaviour-preserving, including the quirk that `pump_startup` alone doesn't bump `last_update` (pinned by a test rather than silently changed — see 4.7). |
| ~~1.2~~ | ~~**Frame-corpus plumbing**~~ | — | **Done.** `scripts/harvest_frames.py` (shape-based dedupe, `--append`, `--coverage`), empty `sidecar/tests/frames.jsonl`, and `test_frame_corpus.py` which replays it. **The corpus itself is still empty** — that is the ongoing part: harvest on the hop, review each `expect`, and let rare/seasonal frames accrue. Coverage currently 0/17. Rare frames are now caught by the shape ledger (1.7). |
| 1.3 | **Pure-function tests** — remaining scroll regexes, WBON stripping, °F/°C (TS side) | Small | **Started:** `sidecar/tests/test_parse_scroll.py` covers the scroll parser, heater body-routing, setpoint bounds and LED nibble decode (19 tests, running in CI). Extend as the corpus grows. |
| 1.4 | **`/status` contract fixture** | Small | There is *already* drift: `/status` returns `pump_startup`, `super_chlor_remaining`, `wedge_cooldown_remaining_s`, `backend`, `circuit_labels`, `faults`, `alerts` — none declared in the TS `PoolStatus`. Harmless in that direction; a rename in the other direction breaks the plugin silently. |
| 1.5 | **`@types/node` `^20` → `^22`** | Trivial | We now declare `engines.node: ^22 \|\| ^24`. Types for a runtime we no longer support can mask or invent API differences. |
| ~~1.7~~ | ~~**Continuous frame capture in the sidecar**~~ | — | **Done.** `_note_frame_shape` records every distinct LCD shape as frames arrive, with one example of each, persisted across restarts and served at `/display/shapes`. Replaces sampling the 60-frame ring, so a rare frame no longer has to coincide with a manual harvest. Guarded so it cannot disturb frame processing: every entry point catches, the ledger is capped, and writes are throttled and best-effort. The harvester prefers the ledger and falls back to `/display/history`. |
| 1.8 | **Salt-cell health from the diagnostic screen** | Small | `-25.31V -5.81A 76°F 2900 PPM` is cell voltage, current, cell temp and instant salt. Reference values for the **T-Cell-15** on this install: **22.0–25.0 V while generating** (30–35 V idle; above 32 V while generating suggests a damaged board) and **3.1–8.0 A**. A useful derived check: while generating, voltage drops roughly **1 V per amp**, so expected ≈ 31 − amps. The captured sample sits right on that line (31 − 5.81 ≈ 25.2 vs 25.31 actual) with current mid-range — a healthy cell. **The value is the trend, not any single reading**: amps falling over time at comparable salt and temperature is how cell ageing shows, and it shows here before it shows anywhere else. The negative sign is just polarity — the cell reverses periodically to self-clean, so both signs are normal and **both polarities should read within ~200 PPM of each other**; a widening gap is the classic end-of-life signal. Only one polarity was captured, so the ledger needs the other. |
| 1.8b | **Other menu-walk data worth reading** | Small each | **Flow Switch** state; firmware revisions (Main 4.46, RF Base r3.00 ID:203A, Display, VSP) — useful in bug reports from other installs. **Instant Salt is NOT a competing display value**: it is a point-in-time reading, while the scroll's `Salt Level` is time-averaged, and the average is the right thing to show. Instant salt earns its place as a *diagnostic* input alongside the V/A figures above (the per-polarity comparison), not on a tile. |
| 1.9 | **Filter timers are readable** | Medium | `Filter T1-all 07:00A to 08:00A`, `T2` 8:00A–9:30P, `T3` 9:30P–10:30P, `T4 --- Off ---`, plus `Spa-all` and `Valve3-all`, and `Filter T1-Spd1 90%` linking a timer to a VSP speed. These screens were always reachable — the sidecar walks the same menus a person uses at the keypad — so this is about **choosing to expose them**, not about gaining access. Reading first (not writing) would let HomeKit and the cockpit show what the pool is actually scheduled to do, which is the low-risk half. Writing schedules is a separate, more careful decision. |
| 1.10 | **Mis-decoded serial frames reach the LCD path** | Investigate | ~25 shapes like `sw\|hhhhKEQF><G`, all sharing a constant tail, arriving 1–3 times each over hours. Not LCD content — something non-display is being handed to `text_updated`. Harmless today (the shape ledger classifies them as noise and nothing else matches them), but it means a frame type is being decoded wrongly, and it's worth knowing which. |
| 1.6 | **`shellcheck` job in CI** for `deploy/*.sh` | Small | Deliberately left out of the first CI pass because shellcheck wasn't available locally to confirm it'd be green. `deploy/deploy.sh` has already shipped one real bug (a hardcoded deleted branch). |

## 2 — Before opening to outside users

The suite's real job is making changes safe that can't be hardware-verified.
That matters most the moment someone else sends a PR.

| # | Item | Size | Why |
|---|---|---|---|
| 2.1 | **Tests seeded from historical bugs** | Small–medium | The CHANGELOG is a list of *confirmed reachable* failure states — better evidence than invented cases. Mapping table in [testing-strategy](testing-strategy.md). |
| 2.2 | **Deadlock-class tests** — concurrent `/status`, no reentrant `state_lock` | Small | The `/status` self-deadlock is the worst outage this project has had: every accessory unresponsive, not one wrong reading. |
| 2.3 | **`SimPanel` assertions** (menu nav, write invariants) | **Largest single piece** | The gate for accepting a `MenuNavigator` change from anyone. Covers the write paths, where severity is highest — a read once left the heater *on*. |
| 2.4 | **`CONTRIBUTING.md`** | Small | Referenced as missing in the README. Needs the branch/PR flow and "run `check_docs.py` before pushing". |
| 2.5 | **Publish to npm** | Small | Name is available; `package.json` metadata and `files` allowlist are already done. Removes the from-source-only install friction. |

## 3 — Decisions pending

Not work items yet — they need a call before they become one.

| # | Decision | Notes |
|---|---|---|
| 3.1 | **Tier 1 "direct mode" backend** — build for 2.0, or not? | Plugin-only, no sidecar: circuit toggles + heater Auto/Off + read-only status. ~400–600 lines, fully additive. Design sketch on the `claude/tier1-design` branch (unmerged). Confirmed against `homebridge-aqua-connect-lite` + its heat-control fork that this scope is reachable via plain HTTP, and that setpoints genuinely are not. |
| 3.2 | **Homebridge verified-plugin submission?** | Nothing in the written requirements forbids this architecture, but the whole spirit is single-`npm install` + UI config. Requiring a Python sidecar (and for RS-485, a second Pi) makes this an outlier. Expect pushback. |
| 3.3 | **Keep the standalone architecture artifact?** | The README's generated SVG is now CI-verified. The separate hosted artifact page is richer but has no staleness guard — decide whether it's worth maintaining or should be dropped. |

## 4 — Known-open, low priority

Carried over from `plugin-spec.md` §10.2; unchanged in substance.

| # | Item | Status |
|---|---|---|
| 4.1 | **Heater setpoint dial: no instant revert on a failed write** | Open — deliberately deferred. Every *other* write confirms and reverts on failure; the setpoint dial is debounced and returns `202` before the physical write starts, so there's no response left to carry a failure. The sidecar still records what actually landed, so it self-corrects on the next poll rather than staying wrong. Fixing it means restructuring the debounce architecture. Revisit only if it's observed to feel wrong. |
| 4.2 | **Hoist magic numbers to named constants** (600 ms debounce, 35 % floor, timers) | Cosmetic, low |
| 4.8 | **OmniDirect / OmniLogic lighting** | Not supported and not planned. Networked ColorLogic on Hayward's OmniLogic platform selects colours directly (plus dimming and show-speed) instead of power cycling — a different platform and wire protocol, not a gap configuration can close. This plugin targets ProLogic/AquaLogic. |
| 4.3 | **Spillover mode** | Untested — not present on this installation, and can't be without hardware that has it |
| 4.4 | **Valve-mode detection lag (~10–30 s)** | Scroll-dependent; no event-driven update |
| 4.5 | **Fault-phrase discovery** | Ongoing, manual. `harvest_frames.py --anomalies` now surfaces these alongside other unhandled frames, so they're visible without remembering to check. Unrecognised alert-looking frames are logged `FAULT-CANDIDATE` and persisted; periodically pull `GET /faults/candidates` and promote real wording into `_FAULT_PHRASES`. |
| 4.6 | **Plugin-side (TypeScript) tests** | Accessory gating, `SidecarClient` against a mocked HTTP layer, value mapping. Worth doing alongside 3.1 if that's built. |
| 4.7 | **`pump_startup` doesn't bump `last_update`** | Pre-existing quirk, now pinned by a test. Looks like an oversight rather than intent; fixing it is a deliberate behaviour change, not a refactor. |

---

## Explicitly not doing

Recording these so they don't get re-proposed:

- **Mocking the AquaConnect box** to unit-test the backend. It would test our
  assumptions about the firmware, not the firmware. Timing gates, wedge
  behaviour and whether a keypress lands are only knowable on hardware — a
  green mock test would be actively misleading.
- **Heavy HomeKit/HAP accessory tests.** Expensive to mock, and the failure
  mode (a tile looks wrong) is immediately visible and cheap to fix. The stuck
  "Active Heat" tile name is a real bug that no reasonable test would have
  caught, and that's an accepted boundary.
- **Sweeping the idle scroll to refresh a reading after a write.** Tempting,
  because a value read only from the scroll can take most of a minute to
  appear — the Super Chlorinate countdown after a toggle, for instance. It was
  built and reverted: on-demand sweeping is what used to **lock up the
  AquaConnect box**, since the extra keypresses land while the panel is still
  settling from the write and wedge it, costing a power-cycle to clear.
  `sweep_scroll` stays safe where it is used today — startup and an explicit
  Refresh, neither following a write. Waiting out the ~6s-per-item natural
  cycle is the correct trade.
- **Coverage targets**, and testing the cockpit HTML.
- **Recommending `homebridge-aqua-connect-lite`** as a lighter alternative in
  the README. Last published April 2023, declares `homebridge-config-ui-x` as
  a runtime dependency, ships axios 0.27, and predates Homebridge 2 — which is
  what this plugin requires, so our readers are exactly who it's least
  verified for. It stays credited as prior art, not as a redirect.
