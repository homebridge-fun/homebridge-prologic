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
| 1.1 | **Extract `parse_ac_scroll`** from `_apply_ac_scroll_to_state` | ~40 lines | Parsing currently can't be tested — it mutates module-global `state` under a lock. Nothing else in the test plan is possible until this is split. |
| 1.2 | **Frame-corpus plumbing** — `scripts/harvest_frames.py` (dedupes against the corpus), `sidecar/tests/frames.jsonl`, coverage report | Small | Start early: some frames only appear seasonally or during rare conditions, so the corpus accrues over months rather than in one sitting. See [testing-strategy § Tier B](testing-strategy.md). |
| 1.3 | **Pure-function tests** — LED nibble decode, every scroll regex, WBON stripping, °F/°C | Small | Where most bugs this project has actually shipped have lived. |
| 1.4 | **`/status` contract fixture** | Small | There is *already* drift: `/status` returns `pump_startup`, `super_chlor_remaining`, `wedge_cooldown_remaining_s`, `backend`, `circuit_labels`, `faults`, `alerts` — none declared in the TS `PoolStatus`. Harmless in that direction; a rename in the other direction breaks the plugin silently. |
| 1.5 | **`@types/node` `^20` → `^22`** | Trivial | We now declare `engines.node: ^22 \|\| ^24`. Types for a runtime we no longer support can mask or invent API differences. |
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
| 4.3 | **Spillover mode** | Untested — not present on this installation, and can't be without hardware that has it |
| 4.4 | **Valve-mode detection lag (~10–30 s)** | Scroll-dependent; no event-driven update |
| 4.5 | **Fault-phrase discovery** | Ongoing, manual. Unrecognised alert-looking frames are logged `FAULT-CANDIDATE` and persisted; periodically pull `GET /faults/candidates` and promote real wording into `_FAULT_PHRASES`. |
| 4.6 | **Plugin-side (TypeScript) tests** | Accessory gating, `SidecarClient` against a mocked HTTP layer, value mapping. Worth doing alongside 3.1 if that's built. |

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
- **Coverage targets**, and testing the cockpit HTML.
- **Recommending `homebridge-aqua-connect-lite`** as a lighter alternative in
  the README. Last published April 2023, declares `homebridge-config-ui-x` as
  a runtime dependency, ships axios 0.27, and predates Homebridge 2 — which is
  what this plugin requires, so our readers are exactly who it's least
  verified for. It stays credited as prior art, not as a redirect.
