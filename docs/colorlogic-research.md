# Pool + spa light-program research (authoritative)

**The two lights are DIFFERENT products and program differently** — do not share
one routine:
- **Pool light** = `LIGHTS` circuit = **Hayward Universal ColorLogic** (17/27
  programs, relative advance, compatibility modes, 15s startup). §"Hayward" below.
- **Spa light** = `AUX_1` circuit = **Pentair IntelliBrite 5G** (12 programs,
  absolute count, much simpler). §"Spa" at the bottom.

Sidecar implication: per-circuit config — different program lists, counts, reset
thresholds, and advance mechanics. `LIGHT_PROGRAMS` (currently Hayward-only)
needs a spa/Pentair list too, keyed by body.

---

# Hayward ColorLogic — pool light (authoritative)

Sourced from the actual Hayward docs the user supplied (2026-07-11):
- **Hayward Tech Service QRG — "Universal ColorLogic & CrystaLogic: Changing
  Modes and Transformer Chart"** (the definitive mode-change doc)
- **Royal Swimming Pools — "Operating the Hayward ColorLogic Light"** (advance
  timing)
- ColorLogic Family Brochure (show descriptions); PoolDial troubleshooter.

## The critical distinction: there are TWO kinds of "mode"

The user is exactly right, and conflating these caused all our confusion:

1. **Compatibility mode** — which controller/protocol the light *emulates*
   (so it works with OmniLogic, AquaLogic, Pentair, etc.). **5 of these.** Set by
   **long (11–15 s) power interruptions**, identified by a **blink color** on
   power-up.
2. **Color program / show** — the actual color (the 17 or 27 list). Advanced by
   a **quick power-cycle (off→on within 10 s)**.

**Changing #1 vs #2 use different power-cycle timings.** Mixing them up (and
using ~10–15 s resets during our testing) is almost certainly why the colors
stopped matching the numbers — **we likely changed the compatibility mode.**

## Compatibility modes (the 5, and how to identify)

Do: **turn ON, then OFF for 11–15 s, repeat 3×; turn back ON** → the light blinks
**one of five** colors identifying its mode:

| Mode | Blink | Program set |
|---|---|---|
| **Universal ColorLogic (UCL)** | **red + white** | 17 programs (our list) |
| ColorLogic 4.0 | green + white | different |
| ColorLogic 2.5 | blue + white | different |
| Pentair SAM | white + white | different |
| **Omni Direct** | purple + white | **27 programs** (adds 18–27 fixed colors) |

- **To toggle modes:** after it blinks, turn OFF then back ON *immediately*,
  repeat until the mode you want. **Turn off for 2 minutes to save.**
- **CVD note:** UCL(red)/CL4.0(green) is the hard pair for red-green CVD, but
  blue / white-only / purple are distinguishable — and the reset below *forces*
  UCL, so you don't have to read red-vs-green.

### Reset to UCL mode (the clean fix)
1. Turn ON, then **OFF for 11–14 s, repeat 4×**.
2. Turn back ON → blinks **red + white** (= UCL mode).
3. **Turn off for 2 minutes to save** in UCL mode.

There's also an OmniLogic *service-menu* path (Service Mode → Light Mode → set
UCL switching vs Omni Direct) — **but that needs OmniLogic R3.2.0+. This system
is AquaLogic, so we use the power-cycle method above.**

## Color-program advance (within a mode)

From the operating guide, the **authoritative** advance action:

| Action | What to do |
|---|---|
| Turn on | switch on |
| Turn off | switch off |
| **Advance to next program** | **turn the switch OFF, then back ON within 10 s** |
| **Save / lock in current program** | **leave off (or on) >10 s** — the light saves the current program |

The three timing bands that matter (all on the same LIGHTS relay — this is why
it's easy to trip the wrong one):
- **off < 10 s → on:** advance one program (relative +1).
- **off > 10 s:** locks in / saves the current program (no advance).
- **off 11–15 s, repeated 3–4×:** **changes the COMPATIBILITY mode** (the maze).
  Note this OVERLAPS the ">10 s save" band — a repeated 11–15 s off is the mode
  trigger, which is exactly the trap our long resets fell into.
- **off > 60 s → on:** **cold-start** white for 15 s, then last color.

Key facts that reshape our approach:

- **It's RELATIVE, not absolute.** Each off→on(<10s) advances **one** program
  from the current one. No documented "reset to program 1" for the *color* (the
  mode reset resets the *compatibility* mode, not the color index). Absolute
  selection needs state tracking or a known start.
- **Program counts:** **17** in UCL/switched mode; **27** in Omni Direct.
- **The 15 s white startup is a COLD-START thing only** (after a >60 s off) — it
  does **not** gate every advance. So: cold-start once, let the 15 s finish,
  *then* step with off/on-<10s repeatedly. (Earlier I over-applied this as a
  per-cycle gate — corrected.) The initial white is not a program; don't count it.

## The 17 UCL programs (switched mode) + appearance

`(S)` = show/moving, `(F)` = fixed/static. Advance order per the QRG:

| # | Name | Type | Looks like |
|---|---|---|---|
| 1 | Voodoo Lounge | S | psychedelic, 1,500+ hues |
| 2 | Deep Blue Sea | F | deep blue |
| 3 | Royal Blue | F | blue |
| 4 | Afternoon Skies | F | sky blue |
| 5 | Aqua Green | F | aqua/teal |
| 6 | Emerald | F | green |
| 7 | Cloud White | F | **white** (landmark) |
| 8 | Warm Red | F | red |
| 9 | Flamingo | F | pink |
| 10 | Vivid Violet | F | purple |
| 11 | Sangria | F | deep red/magenta |
| 12 | Twilight | S | 1,500+ ever-changing, relaxing |
| 13 | Tranquility | S | calming blues + white |
| 14 | Gemstone | S | blue, green, magenta |
| 15 | USA | S | **red, white, blue** (recognizable) |
| 16 | Mardi Gras | S | fast, 32 colors |
| 17 | Cool Cabaret | S | 100+ colors, vibrant |

**Omni Direct mode adds (18–27, all fixed):** Yellow, Orange, Gold, Mint, Teal,
Burnt Orange, Pure White, Crisp White, Warm White, Bright Yellow.

**CVD-safe landmarks:** motion = show (1, 12–17) vs static = fixed (2–11);
Cloud White #7 = plainly white; USA #15 = moving with white flashes.

## What this means for our sidecar feature (rework needed)

Our current `/program` assumes **absolute reset + count N power-ons**. The real
mechanic is different, so:

1. **First, get the light back into UCL mode** (compatibility-mode reset:
   11–14 s off ×4, then 2 min off). Until then, no mapping will match — this is
   the most likely cause of "colors don't match the numbers."
2. **Advance is relative** (off→on within 10 s = +1). To hit an absolute
   program we must **track the current program** and step `(target − current)
   mod count`, OR establish a known start. There is no documented color reset to
   #1 — investigate whether a fresh mode-save lands on a known program **[VERIFY
   on hardware]**.
3. **Never cycle during the 15 s white startup** after a >60 s off. Our reset
   must be followed by a **≥15 s settle** before the first advance.
4. **Don't use 11–15 s offs during normal advancing** — that's the *mode-change*
   trigger and will silently switch compatibility modes (what bit us).
5. Advancing is **slow and deliberate** (one off→on within-10s cycle, then let
   it settle), not a rapid burst — good news: no keep-alive-sync heroics needed.

## Open questions (hardware verification)

1. After a UCL mode-save (2 min off), what color program does it land on — is
   there a reliable known start (program 1)? If yes, absolute selection = save →
   settle → advance N−1.
2. Confirm this exact light is currently in **which** compatibility mode (blink
   check) — quite possibly *not* UCL after our testing.
3. Exact "advance" off/on window that reliably registers (docs say "within
   10 s"; the physical experience is faster) and the minimum settle after
   startup.
4. Does advance wrap (17→1)?

---

# Spa light — Pentair IntelliBrite 5G (authoritative)

Different manufacturer/model from the pool light. On the `AUX_1` circuit.
Source: user-supplied Pentair IntelliBrite 5G operating instructions.

## Mechanic — simpler than the Hayward: ABSOLUTE count

- **Power-on:** momentary white, then the previously-selected color.
- **Off > 5 seconds** → on restores the **last saved** show/color (short off, not
  a mode reset like Hayward's).
- **Select program N (1–12):** with the light on, **turn the wall switch off/on
  N times** — N cycles = program N (**absolute**, not relative). Example: turn
  on, then off/on **6×** → program 6 (California Sunset).
- **No illumination during the off/on switching** — it stays dark while counting,
  then shows the selected program. (This matches the "you shouldn't see it
  flashing" behavior the user described — that behavior belongs to the SPA light,
  not necessarily the pool one.)
- **Hold / Recall** (details on operating-instructions p.4, not yet captured):
  #13 "Hold" saves the current color mid-show. **[GET p.4]**

Because it's absolute (N cycles = program N) with only a 5s save threshold and no
compatibility-mode maze, the spa light is likely the **easier of the two to
automate** — reliable count is all that's needed.

## Programs (1–12)

`(S)` = show/moving, `(F)` = fixed/static.

| # | Name | Type | Looks like |
|---|---|---|---|
| 1 | SAm Mode | S | cycles white, magenta, blue, green (emulates Pentair SAm) |
| 2 | Party | S | rapid color changing |
| 3 | Romance | S | slow, calming transitions |
| 4 | Caribbean | S | blues & greens |
| 5 | American | S | **red, white, blue** |
| 6 | California Sunset | S | orange, red, magenta |
| 7 | Royal | S | richer/deeper tones |
| 8 | Blue | F | blue |
| 9 | Green | F | green |
| 10 | Red | F | red |
| 11 | White | F | **white** (landmark) |
| 12 | Magenta | F | magenta/pink |
| 13 | Hold | — | (feature: save current show color, not a program) |

**CVD-safe landmarks:** motion = show (1–7) vs static = fixed (8–12); White #11
is plainly white; SAm #1 and American #5 include white in their cycle.

## Sidecar implications (spa)

- Absolute selection: `AUX_1` off/on N times = program N. No long reset; just
  ensure the light is on, then N clean off/on cycles.
- Still open-loop (no feedback), but absolute count means **no position
  tracking** needed — big advantage over the pool light.
- Reuse the daemon's power-cycle primitive on `AUX_1`; only the count semantics
  and program list differ.

## Per-mode program sets (PRELIMINARY — [VERIFY] from the all-models guide)

Each compatibility mode exposes a **different, usually smaller** program set than
UCL's 17 — so if the light drifted into CL 4.0 / CL 2.5, the number→color mapping
is entirely different and counts wrap sooner. Definitive lists are in Hayward's
**"ColorLogic Programming Guide (all models)"**
(`https://hayward.com/media/wysiwyg/pdf/lighting/ColorLogic-ProgramingGuideallmodels.pdf`
— 403'd the fetcher; pull manually). From web-search summaries (imperfect):

- **UCL** — 17 (10 fixed + 7 shows). CONFIRMED (QRG).
- **ColorLogic 4.0** — ~5 fixed (Afternoon Skies, Deep Blue Sea, Emerald,
  Sangria, Cloud White) + shows. Extra shows (Harmony, Rainbow, Custom Fade,
  Custom Chase) may be the standalone CL4.0 *light*, not the compat *mode*.
  **[VERIFY]**
- **ColorLogic 2.5** — ~12 (≈5 fixed + 7 shows); fixed-color list came back
  inconsistent. **[VERIFY]**

Practical note: the **blink-color check** (red/green/blue/white/purple) is the
reliable way to identify the current mode — not the color set — and the
reset-to-UCL sequence forces a known mode without needing to read red-vs-green.

## Controller-menu programming — CONFIRMED NOT AVAILABLE on this system

**2026-07-11: user confirmed the ProLogic panel does NOT expose light-program
menus for these lights — they are switched relays, so the feature MUST use
power-cycling (the relay).** The menu path below is reference-only (it applies to
networked/OmniDirect installs, not this one).

Source: Hayward "ColorLogic 4.0 Lights Programming" (user-supplied, 1 page).
It programs the light **through the controller's menu**, NOT by power-cycling:

`MENU (repeat) → Settings Menu → "Aux1 Settings" → + to view/change →
 "Aux1 Program" → +/- to pick Show (1, 7-16) or Fixed color (2-6) →
 Speed (x1/16..x16) → Motion (-1.2..+1.2) → Brightness (20-100%)`

**Why this matters hugely:** menu navigation is exactly what the sidecar bridge
already does at **100% reliability** (heater setpoints, chlorinator %). If the
ProLogic panel exposes a light-program menu, we set colors by **navigating the
menu** — no power-cycling, no mode drift, no counting, no capacitor timing. It
would make the whole feature a straightforward MenuNavigator extension.

**Caveats / must-verify on hardware:**
- The menu controls Speed/Motion/Brightness → implies a **data-connected /
  OmniDirect (networked)** light, not a plain switched relay. The user's lights
  appear **switched** (programmed by hand power-cycling), so this exact menu may
  not be present.
- BUT some Hayward controllers offer a color menu that **automates the
  power-cycling** for a switched light — same reliable outcome via menu nav.
- **ACTION:** on the ProLogic panel, MENU → look for Settings → (Lights/Aux)
  Settings → Program/Color for the pool (`LIGHTS`) and/or spa (`AUX_1`) circuits.
  If present, this is the path — drive it via the bridge and skip power-cycling
  entirely. Also check the sidecar's own menu sweeps/`/debug` for a light-program
  screen. **[VERIFY]**

CL 4.0 menu numbering seen in the guide: fixed colors **2-6** (2 = Deep Blue
Sea), shows **1** and **7-16** (7 = Twilight; 15 = Custom Fade, 16 = Custom
Chase). Full program table is on "page 1 of the manual" (not in this 1-page
guide) — **[GET]**.
