# Pool + spa light control — how they work (authoritative)

**The two lights are DIFFERENT products with DIFFERENT control mechanics.** They
do not share a routine, a program list, or a calibration. This doc is split into
one self-contained section per light. Read the section for the light you're
touching.

| | **Pool light** | **Spa light** |
|---|---|---|
| Product | Hayward ColorLogic (12-program, "CL 4.0") | Pentair IntelliBrite 5G |
| Circuit | `LIGHTS` | `AUX_1` |
| Programs | 12 | 12 |
| Mechanic | **Relative** (each off/on = +1) | **Absolute** (off/on N times = program N) |
| Absolute anchor | **Resync to #1** via one 11–14 s off | Not needed (count is absolute) |
| Sidecar offset | n/a (relative) | **+1**, baked in |
| Status | ✅ Working, hardware-confirmed 2026-08 | ✅ Working, hardware-confirmed |

Both lights are **open-loop**: the panel never reports which program is showing.
The sidecar tracks position optimistically in `state.light_program[body]` and
displays it as a badge on the cockpit card and the HomeKit tile.

---

# 1. Pool light — Hayward ColorLogic (12-program / CL 4.0)

## What it is (and how we know)

The user's actual light manual (photographed 2026-08, IMG_2927/2928) is the
**12-program Hayward ColorLogic** — NOT the 17-program Universal ColorLogic
(UCL) we originally assumed. How we know:

- The manual lists **exactly 12 programs** (below).
- On the hardware, stepping a full loop shows **~5 fixed then ~7 shows**, with
  **only one green** (Emerald). UCL has 17 programs and *two* greens (Aqua Green
  + Emerald). The single green is the decisive tell — confirmed by the user.
- The earlier "UCL mode maze" theory (compatibility modes, red+white blink) does
  not apply to this light. See the historical appendix for why we chased it.

## The 12 programs (advance order)

`(S)` = show/moving, `(F)` = fixed/static.

| # | Name | Type | Looks like |
|---|---|---|---|
| 1 | Voodoo Lounge | S | fast color wash (the **resync anchor**) |
| 2 | Deep Blue Sea | F | deep blue |
| 3 | Afternoon Skies | F | sky blue |
| 4 | Emerald | F | green (**the only green**) |
| 5 | Sangria | F | deep red/magenta |
| 6 | Cloud White | F | white |
| 7 | Twilight | S | slow color wash |
| 8 | Tranquility | S | blue/cyan/white fade |
| 9 | Gemstone | S | blue/green/magenta fade |
| 10 | USA! | S | red/white/blue switch |
| 11 | Mardi Gras | S | fast random fade |
| 12 | Cool Cabaret | S | random fade |

CVD-safe landmark: **motion** (show vs fixed). A fixed→show or show→fixed
boundary is unambiguous even for a red-green-colorblind viewer with a blue liner.

## The control mechanic (from the manual, hardware-confirmed)

Power-cycle timing bands on the `LIGHTS` relay:

| Off duration | Effect |
|---|---|
| **< 10 s** → on | **Advance one program** (relative +1) |
| **> 10 s** | Save/lock the current program (no advance) |
| **11–14 s** → on | **Re-synchronize to program 1** (Voodoo Lounge) — the absolute anchor |
| **> 60 s** → on | Cold-start: 15 s white, then the last color |

Two facts make reliable absolute selection possible:

1. **It's relative** — each quick off/on advances +1 from wherever it is.
2. **There is an absolute anchor** — a *single* 11–14 s off jumps to **program 1**.
   (The manual documents this as multi-light "Light Synchronization"; it works on
   a single light too.) **Confirmed on hardware 2026-08:** a 12 s off lands on
   Voodoo Lounge every time.

> Note: repeating an 11–14 s off **3–4 times in a row** changes the light's
> *compatibility mode* — a different, unwanted action. The sidecar therefore does
> **exactly one** resync per operation, never a burst.

## How the sidecar operates it

Timing config (per body, `light_config.pool` in `backend.json`): `off_ms`,
`on_ms` only. **`offset` and `reset_ms` do not apply to the relative path.**
Deployed timing is `off_ms = on_ms = 400` (well above the ~200 ms keep-alive so
no press is dropped). `POOL_RESYNC_OFF_S = 12.0`.

**Scene selection — `POST /lights/pool/program {program|name}`:**
- **Position known AND light on** → minimal forward steps: `(n − current) mod 12`
  quick off/on cycles via the daemon `/cycle`. E.g. #2→#3 = a single off/on.
- **Position unknown OR light off** → anchor first: one 11–14 s off (resync to
  #1), then step `(n − 1)`. Slower (~13 s + steps) but works with no known state.
- Either way, `state.light_program['pool']` is set to `n`.

**Resync — `POST /lights/pool/resync`:** one 11–14 s off → on, sets tracked
position to 1 (Voodoo Lounge). ~13 s, blocking. Exposed in the cockpit pool
light settings as the **Resync colors** button. Use it if selection ever drifts;
after it, every selection steps minimally from the known #1.

Because the first selection after a restart auto-anchors and then tracks, the
manual "set current / step +1" tools were removed — they're no longer needed.
(The `/lights/<body>/sync`, `/step`, and `/mode-reset` endpoints still exist but
are unused by the UI.)

## Verification status

- ✅ Resync-to-#1 lands on Voodoo Lounge — user-confirmed 2026-08.
- ✅ Minimal-step selection from a synced position (e.g. Deep Blue Sea → Afternoon
  Skies = one off/on).
- ⏳ Full-range step accuracy (e.g. resync → Cool Cabaret #12) — worth a spot check.

---

# 2. Spa light — Pentair IntelliBrite 5G

## What it is

Pentair IntelliBrite 5G on the `AUX_1` circuit. Source: user-supplied Pentair
operating instructions. Simpler than the pool light — **absolute count, no
compatibility modes, no anchor needed.**

## The 12 programs

`(S)` = show/moving, `(F)` = fixed/static.

| # | Name | Type | Looks like |
|---|---|---|---|
| 1 | SAm | S | white/magenta/blue/green cycle |
| 2 | Party | S | rapid color change |
| 3 | Romance | S | slow calming transitions |
| 4 | Caribbean | S | blues & greens |
| 5 | American | S | red/white/blue |
| 6 | California Sunset | S | orange/red/magenta |
| 7 | Royal | S | richer/deeper tones |
| 8 | Blue | F | blue |
| 9 | Green | F | green |
| 10 | Red | F | red |
| 11 | White | F | white |
| 12 | Magenta | F | magenta/pink |

## The control mechanic

- Power-on shows a momentary white, then the previously-selected color.
- **Off > 5 s → on restores the last saved color** (short off, not a mode reset).
- **Select program N:** with the light on, turn off/on **N times** — N cycles =
  program N (**absolute**). No position tracking is theoretically required, but we
  still track it for the display badge.

## How the sidecar operates it

Timing config (`light_config.spa`): `offset`, `reset_ms`, `off_ms`, `on_ms`,
`local`. **`offset = +1` is baked into the sidecar default** (program N needs
daemon count N+1) — it's a fixed, one-time calibration and is no longer a UI
knob.

**Scene selection — `POST /lights/spa/program {program|name}`** →
`select_program(count = n + offset, reset_ms, off_ms, on_ms)`:
1. Ensure the light is **fully off**, verified against the sidecar's settled poll
   (an unreliable off made every count start from a random program).
2. Hold off for **`reset_ms`** (default 2000 ms) to establish the baseline.
3. Do `count` power-restores, ending on → lands on program N.

`reset_ms` is meaningful **only here** (the spa's baseline hold); the pool never
uses it. After selection, a parity guard (`_ensure_light_on_after_program`)
re-asserts ON if a dropped press left the spa light off — spa-only; the pool must
NOT do this (turning a pool light on within 10 s advances it).

## Verification status

- ✅ Absolute count works with `offset = 1` — user-confirmed ("functional").

---

# 3. Shared: HomeKit + cockpit surfaces

- **HomeKit:** each enabled light is a **Television** accessory (published
  external — HomeKit shows one TV per bridge), power = the circuit, input picker =
  the scenes. `ActiveIdentifier` onSet is fire-and-forget (awaiting the multi-second
  power-cycle times out and reverts the pick). Scene order is pinned via a
  `DisplayOrder` TLV and is editable/reorderable in plugin config
  (`spaLightSceneList` / `poolLightSceneList`).
- **Cockpit:** each light card has a scene dropdown that **stages into the shared
  Apply bar** (nothing fires until Apply) and a **current-program badge** in the
  header driven by the status poll's `light_program`. The ⚙ settings modal is
  body-aware:
  - **Pool:** Resync colors · off ms · on ms · LOCAL · Save.
  - **Spa:** off ms · on ms · reset ms · LOCAL · Save.
- **`GET /lights/programs?body=`** returns the program list, mechanic, calibration,
  and `current_program`. `/status` exposes `light_program` (dict) for the badges.

---

# Appendix: the UCL / compatibility-mode investigation (superseded)

We initially modeled the pool light as the **17-program Universal ColorLogic
(UCL)** with five "compatibility modes" selected by repeated 11–15 s power
interruptions and identified by a red+white power-up blink. Considerable effort
went into a "Reset to UCL" ritual (4× 12 s offs) and reading the blink.

**Why it was a dead end for this install:**
- The light is the **12-program ColorLogic**, not UCL — so no 17-program list ever
  matched, and there was no "mode" to reach.
- The UCL-vs-CL4.0 identifier is a **red+white vs green+white** blink — the exact
  red-green pair the user (colorblind, blue liner) cannot distinguish — so even if
  it *were* UCL, the confirmation was unreadable.
- The real absolute anchor turned out to be the manual's **single 11–14 s resync
  to program 1**, which needs no blink-reading and no mode changes.

Kept here so we don't re-derive the same wrong path. The Hayward 17-program UCL
list and mode table are preserved in git history (pre-2026-08 versions of this
file) if a *different* install ever needs them.
