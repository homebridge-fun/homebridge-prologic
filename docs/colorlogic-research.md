# Hayward ColorLogic — light-program research (authoritative)

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

Key facts that reshape our whole approach:

- **It's RELATIVE, not absolute.** Each off→on(within 10s) advances **one**
  program *from the current one*. The docs describe **no "reset to program 1"**
  for the *color* (the mode reset resets the *compatibility mode*, not
  necessarily the color index). So absolute selection needs either state
  tracking or a reliable known-start.
- **Program counts:** **17** in UCL/switched mode; **27** in Omni Direct
  (18–27 = extra fixed colors, Omni-Direct-only).
- **Startup guard (this likely wrecked our counts):** *"When the light has been
  off >60 s and turned on, it comes on **white for 15 s**, then returns to the
  last color. **Avoid rapidly cycling the switch during the first 15 s** — wait
  for the startup sequence to finish before advancing."* → **We were almost
  certainly cycling during that 15 s startup**, so the advances didn't register
  cleanly. And the initial white is *not* a program — don't count it.

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
