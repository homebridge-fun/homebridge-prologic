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

- **Byte-exact raw socket request.** `urllib`'s default headers make the
  GoAhead "Webs" firmware silently ignore the key (0/20 landed in testing).
  Node's `http`/`fetch` would need the same lean, curl-equivalent header set
  — this needs verifying against Node's HTTP client, not assumed to carry
  over from the Python fix.
- **Exact request bodies.** `KeyId=NN&` for a keypress vs. `Update Local
  Server&` for a read — using `KeyId=00&` for reads injects ~29,000 phantom
  keypad events/day and wedges the box (learned the hard way, see
  `pool_service.py` around `_read()`).
- **Minimum inter-request gap** (`_AC_MIN_GAP_S`) — the panel ignores a key
  sent within ~0.5–1s of the previous one.
- **LED-nibble decode** (`_AC_LED_MAP`) for confirming a toggle landed.

None of this is hard to port — it's well understood and already
Python-tested — but it's real code, not a stub.

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

- **Protocol port** (raw-socket POST, read/write body formats, LED decode,
  timing gate): well-understood, already solved once in Python. Mechanical
  but not quick — probably the single biggest chunk.
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
