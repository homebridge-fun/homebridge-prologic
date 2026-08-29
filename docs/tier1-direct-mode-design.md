# Tier 1 "Direct Mode" — design sketch

> **Status:** design sketch, not built. Written to size the work for a
> possible 2.0. Nothing here is committed to; treat it as a starting point
> for a go/no-go decision, not a spec to implement as-is.

## Why

Today the plugin has exactly one operating mode: it talks to the Python
sidecar over REST, and the sidecar talks to either an AquaConnect box or the
pad-Pi RS-485 bridge. There is no path that doesn't require standing up and
maintaining a separate service. That's a real barrier to a casual install —
"clone a repo, run a Python installer, manage a systemd unit" is a lot to
ask before someone even knows if the plugin is useful to them.

The three-tier framing:

| Tier | What runs | What you get |
|---|---|---|
| **1 — Direct** *(new)* | Homebridge plugin only, talking straight to the AquaConnect box's local HTTP interface | Basic on/off control + status, zero extra infra |
| **2 — Sidecar (AquaConnect)** *(exists)* | Plugin + Python sidecar + cockpit, sidecar drives an AquaConnect box | Full feature set — heater setpoints, light scenes, chlorinator %, cockpit UI |
| **3 — Sidecar (pad Pi)** *(exists)* | Plugin + sidecar + a dedicated pad-Pi RS-485 bridge | Same as Tier 2, plus faster/100%-reliable writes and no AquaConnect box dependency |

Tiers 2 and 3 are just the existing `backend: aquaconnect` / `backend:
rs485bridge` switch — no new work. **Tier 1 is the new piece**, and it's a
real feature, not a docs reframe: today there is no code path that skips the
sidecar at all.

## What Tier 1 would actually need to do

The plugin would gain a third backend mode — call it `direct` — implemented
entirely in TypeScript inside `src/`, with **no Python, no sidecar, no
cockpit**. It talks straight to the AquaConnect box's `POST /WNewSt.htm`
over local HTTP, same wire protocol the sidecar's `AquaConnectBackend`
already uses (`sidecar/pool_service.py`, documented in
`docs/aqualogic-automation-spec.md` §15).

### In scope for Tier 1 — genuinely feasible

These are all single-keypress-and-read operations; no menu navigation:

- **Circuit on/off**: pool, spa, filter, lights, aux1/aux2, spillover — one
  `KeyId=NN&` POST, LED-nibble read-back to confirm.
- **Heater Auto/Off** (the armed-state toggle, not the setpoint) — same
  single-key model as circuits.
- **Read-only status**: pool/spa/air temp, salt PPM, chlorinator %,
  heater-running indicator, active body (pool/spa/spillover) — all parsed
  from the idle LCD scroll, no navigation required, exactly what the
  sidecar's passive-scroll parsing already does.
- **Bridge health**: a simple reachability/LED-sanity check, mirroring
  today's "Bridge Offline" switch.

### Out of scope for Tier 1 — needs menu navigation

These all require `MenuNavigator`'s multi-step "enter Settings → arrow to
item → confirm" state machine, which only exists in Python today:

- Heater **setpoint** writes (only Auto/Off would work in Tier 1).
- Light **scene** selection (ColorLogic/IntelliBrite program stepping).
- Chlorinator **%** writes and VSP pump-speed control.
- Super Chlorinate start/stop (also menu-driven).

Porting `MenuNavigator` to TypeScript would eliminate the "limited" in
"limited functionality" — but it's also most of the actual complexity in
this codebase (the multi-step timing-sensitive nav state machine,
`sidecar/pool_service.py`'s single largest piece of logic) duplicated into a
second language, doubling the maintenance surface for the trickiest code in
the project. That tradeoff is why Tier 1 should stay deliberately
**read-mostly + simple-toggle-only** rather than trying to reach full parity.

### Protocol details that have to be ported, not just referenced

The AquaConnect HTTP quirks the sidecar had to hand-discover are not
optional politeness — skip them and keypresses silently drop:

- **Request transport.** The sidecar's `_post`/`_read` hand-build a raw
  socket request because `urllib`'s default headers made the GoAhead "Webs"
  firmware silently ignore the key (0/20 landed in testing) — but this looks
  like a `urllib`-specific problem, not a fact about the box. See
  "Comparison to `homebridge-aqua-connect-lite`" below: that plugin talks to
  the same box reliably using plain **`axios`** with just `Content-Type:
  application/x-www-form-urlencoded`, `Content-Length`, and `Connection:
  close`. `axios` is already a dependency here (`sidecarClient.ts` uses it),
  so Tier 1 likely does **not** need a raw-socket workaround at all — worth
  confirming against real hardware, but this de-risks what was the biggest
  unknown in the port.
- **Exact request bodies.** `KeyId=NN&` for a keypress vs. `Update Local
  Server&` for a read — using `KeyId=00&` for reads injects ~29,000 phantom
  keypad events/day and wedges the box (learned the hard way, see
  `pool_service.py` around `_read()`). `aqua-connect-lite` uses the same two
  body strings.
- **Minimum inter-request gap** (`_AC_MIN_GAP_S`) — the panel ignores a key
  sent within ~0.5–1s of the previous one. Notably, `aqua-connect-lite` has
  **no** gap enforcement at all — Tier 1 should keep ours rather than follow
  that example, since our own experience says skipping it risks wedging the
  box under repeated presses.
- **LED-nibble decode** (`_AC_LED_MAP`) for confirming a toggle landed —
  identical table to `aqua-connect-lite`'s (`3`=no-key, `4`=off, `5`=on,
  `6`=blink), independent confirmation this mapping is a fact about the
  firmware, not something we got wrong.

None of this is hard to port — it's well understood, already Python-tested,
and now cross-confirmed by a second independent implementation — but it's
real code, not a stub.

## Comparison to `homebridge-aqua-connect-lite`

[`homebridge-aqua-connect-lite`](https://github.com/cupshir/homebridge-aqua-connect-lite)
(cupshir — already credited in the README's Acknowledgments as prior art) is
worth comparing directly, since it's an existing, shipping plugin doing
almost exactly what Tier 1 proposes: no sidecar, plugin talks straight to
the AquaConnect box.

| | AquaConnect Lite | Tier 1 (proposed) |
|---|---|---|
| External service | None | None (same) |
| HTTP client | `axios` | `axios` (already a dependency here) |
| Request bodies | Same `KeyId=NN&` / `Update Local Server&` pair | Same — identical firmware, identical protocol |
| LED decode | Same nibble table | Same |
| Timing/gap enforcement | None | Keep `_AC_MIN_GAP_S`-equivalent (Lite's lack of this is a gap, not a model to copy) |
| Accessories | Pool Light, Aux 1, Aux 2 (3 toggles) | Circuits + heater Auto/Off + read-only temps/salt/chlorinator%/active-body |
| Heater/spa | Explicitly unsupported by upstream ("I do not have a spa or heater... no plan to add support") | In scope for Auto/Off |

**The owner's own fork of AquaConnect Lite already added heat control** on
top of upstream — independent real-world precedent (beyond our own sidecar)
that heater control is achievable via this same direct-HTTP model, not just
a theoretical extrapolation from the sidecar's Python code. Worth
confirming, when scoping this for real: whether that fork's heat control
covers just the Auto/Off toggle (matches Tier 1's proposed scope exactly)
or also setpoint writes (which our sidecar's own experience says needs
menu-navigation — if the fork found a simpler path, that would change this
design's "setpoint is out of scope" conclusion and is worth digging into
before finalizing scope).

### What would NOT need to exist in Tier 1

- Python, `sidecar/`, `pip`, systemd, the cockpit web UI.
- The RS-485/pad-Pi path (Tier 1 is AquaConnect-only — direct RS-485 needs
  the pad-Pi's precise write timing, which is the sidecar/bridge's whole
  reason to exist; there's no "direct RS-485 from Homebridge" tier).
- Config for `sidecarHost`/`sidecarPort` — `direct` mode would just need the
  AquaConnect box's IP.

## Sketch of the plugin-side shape

- A new `src/directBackend.ts` (or similar) implementing the same narrow
  interface `platform.ts` currently expects from `sidecarClient.ts` — poll
  for status, send a circuit/heater toggle — so `platform.ts` and the
  accessory classes (`switchAccessory.ts`, `heaterRunningAccessory.ts`,
  `temperatureAccessory.ts`, etc.) don't need to know which mode is active.
- `config.schema.json` gains a `backend: direct` option alongside
  `aquaconnect`/`rs485bridge`, with an `aquaconnectHost` field (reusing the
  existing key) and no sidecar-related fields required.
- Accessories gated on sidecar-only features (light-scene Television tiles,
  chlorinator Fan tile, VSP anything) simply aren't registered when
  `backend === 'direct'` — same pattern `platform.ts` already uses for the
  `enable*` feature flags.

## Rough sizing

This is a real, scoped feature — not a trivial docs change — but it's also
not open-ended:

- **Protocol port** (POST via `axios`, read/write body formats, LED decode,
  timing gate): well-understood, solved once in Python, and now
  cross-confirmed by a second independent plugin (`aqua-connect-lite`) using
  the exact HTTP client this repo already depends on. Smaller/lower-risk
  than originally estimated — the raw-socket workaround the sidecar needed
  looks like it was a `urllib`-specific problem, not something Node/`axios`
  will also hit.
- **New backend + accessory gating in the plugin**: moderate — mostly
  wiring, following the existing `sidecarClient.ts`/`platform.ts` pattern.
- **Testing against real hardware**: the biggest unknown/risk, same as
  every other hardware-facing change in this project — needs real
  keypress-landing verification, not just "compiles."
- **Docs**: a new README tier section + config example.

Feels like a 2.0-scale feature (new backend mode + a config-schema change +
new accessory-gating logic + hardware validation), not a patch release. It's
also fully additive — Tiers 2/3 are untouched — so it carries low regression
risk to what already works.

## Open questions before committing to this for 2.0

1. Is "toggle switches + read-only sensors, no heater setpoint, no light
   scenes" actually useful enough to justify building and maintaining a
   second, permanently-limited code path? Or does it mostly serve as a
   "try before you commit to the sidecar" on-ramp rather than a mode people
   stay on?
2. Do we want `direct` mode to auto-detect and suggest upgrading to Tier
   2/3 once someone's used it a while (e.g. a log line / README callout),
   or leave it fully silent?
3. Is there real demand for this, or is it worth shipping 1.0 first and
   gauging whether "I don't want to run a sidecar" is an actual recurring
   ask before investing here?
4. What does the owner's AquaConnect Lite fork's heat control actually
   cover — Auto/Off only, or setpoint too? If setpoint turns out to be
   reachable without full menu-navigation, that changes what "limited" means
   for Tier 1 and is worth investigating before finalizing scope.
