# Hayward ColorLogic — light-program research

Research brief for the "select a light scene from the sidecar" feature. Goal:
understand what the 17 programs actually look like, how the power-cycle
programming really works, and **whether our heavy relay-cycling knocked the
light into a different mode** (the strong suspicion — colors stopped matching
the numbers). Compiled 2026-07-11 from Hayward docs + reseller guides (sources
at the bottom). Anything marked **[VERIFY]** needs a primary-doc or on-hardware
check.

## TL;DR / the leading hypothesis

Switched (power-cycle) ColorLogic lights run in **one of four modes**, and
**each mode has a different color/show set**. Mode is itself changed by power
interruptions — which we've done hundreds of during testing. So "colors don't
line up with the numbers anymore" is most likely **the light is no longer in
UCL mode** (or its baseline drifted). The fix is a known reset, and the mode is
checkable by a flash color.

## The four modes (and how to tell which you're in)

Do a **3-cycle power-interruption check** and watch the brief flash color:

| Mode | Flash color | Notes |
|---|---|---|
| **UCL** (Universal ColorLogic) | **red** | the 17-program set our spec assumes |
| **CL 4.0** | **green** | different (fewer) colors/shows |
| **CL 2.5** | **blue** | different set |
| **SaM** (Spa-a-Mode / sync) | **white** | different set |

Red-green colorblindness caveat: UCL=red vs CL 4.0=green is exactly the pair
that's hard to distinguish — but **blue (CL 2.5)** and **white (SaM)** are
distinguishable, and the reset below forces UCL, so you can get to a known mode
without having to read the red/green flash.

## Reset to a known state (do this first)

- **Off for 2 minutes → saves the light in UCL mode.** This is the clean reset
  to the 17-program set. **[VERIFY exact duration]** (one source says 2 min).
- **Off > 60 seconds, then on → the light comes on WHITE for ~15 seconds** (a
  deliberate "see the pool" flash), *then* returns to the last fixed color/show.
  This 15s-white startup is very likely something you saw during testing and is
  **not** a program — don't count it.

> Implication for our sidecar: a proper "reset to baseline" may need to hold the
> light **off for minutes**, not the ~2–10s we've been using — and the board is
> capacitor-backed (runs a while after power is cut), so a short off doesn't
> truly reset. This lines up with your observation that a longer off was needed.

## Advance timing — CONFLICTING, needs resolution **[VERIFY]**

Two different mechanics show up in the docs and they may be generation- or
mode-specific. This is the crux of why our cycling has been unreliable:

- **Quick off→on (within ~10s)** — the manual you have says the 17 programs are
  "advanced using power-cycling (quickly powering the lights on/off/on)."
- **Off for exactly 11–13 seconds, then on → advances to the next *show*** —
  "too short → restarts the same show; too long → resync." (reseller guide)

So there may be a distinction between **advancing within/among programs (quick)**
vs **advancing shows / changing modes (long, 11–13s or 2min)**. **We need to
nail which applies to this exact light**, because we've been doing rapid quick
cycles — which may be the *program* advance, the *mode* change trigger, or
neither, depending on generation. **This is the #1 open question.**

## The 17 UCL programs (with what they look like)

Order/numbering per the spec sheet you provided; **[VERIFY]** the actual advance
order on-hardware (it may not match this list 1:1).

**Fixed colors (static — 10):**
| # | Name | Appearance |
|---|---|---|
| 2 | Deep Blue Sea | deep blue |
| 3 | Royal Blue | blue |
| 4 | Afternoon Skies | lighter/sky blue |
| 5 | Aqua Green | aqua/teal |
| 6 | Emerald | green |
| 7 | Cloud White | **white** (easy landmark) |
| 8 | Warm Red | red |
| 9 | Flamingo | pink |
| 10 | Vivid Violet | purple/violet |
| 11 | Sangria | deep red/magenta |

**Color-changing shows (moving — 7):**
| # | Name | Appearance |
|---|---|---|
| 1 | Voodoo Lounge | psychedelic, 1,500+ hues, hypnotic |
| 12 | Twilight | 1,500+ ever-changing colors, relaxing |
| 13 | Tranquility | calming blues + white |
| 14 | Gemstone | blue, green, magenta |
| 15 | USA | **red, white, blue** (recognizable — flag-like) |
| 16 | Mardi Gras | fast-paced, 32 colors |
| 17 | Cool Cabaret | 100+ colors, vibrant |

**Color-blind-safe landmarks** (no red/green call needed): motion = show
(1, 12–17) vs static = fixed (2–11); **Cloud White (#7)** is unmistakably white;
**USA (#15)** flashes white in its cycle and is a moving show; **Tranquility
(#13)** is blues+white.

## What this means for our sidecar feature

1. **First, confirm the mode is UCL** (the 3-cycle flash check, or just reset:
   off ≥2 min). If we've been operating in CL 4.0 / CL 2.5 / SaM, the whole
   17-program mapping is wrong — which fully explains "colors don't match."
2. **Reset probably needs a *long* off** (minutes), because the board is
   capacitor-backed. Our `/program reset_ms` may need to go far higher than the
   current 20s cap — or the reset needs a genuinely different mechanic.
3. **Resolve the quick-vs-11–13s advance question** for this specific light
   before trusting any count.
4. Ignore the **15s white startup** after a long off — it's not program 1.

## Open questions (for deeper research / another chat)

1. For *this* UCL light, does a **quick** off/on advance the program, or is the
   advance the **11–13s** off? Is "quick cycling" instead the **mode-change**
   trigger (which would explain us drifting out of UCL)?
2. Exact **reset** duration/behavior to force UCL mode and land on program 1.
3. Does the advance **wrap** 17→1, and is program 1 really Voodoo Lounge (the
   first on after reset)?
4. Do the mode differences (CL 4.0 / 2.5 / SaM) change the **program count**, and
   what are their lists?
5. Is there a **non-power-cycle** control path (e.g., the AquaLogic panel's own
   lights menu, or an OmniLogic ColorLogic screen) that sets color directly —
   which would sidestep all of this?

## Source PDFs to pull (some 403'd my fetcher — grab these and paste back)

- **Hayward — "2020 QRG: UCL How to Change Modes & Transformer Chart"** *(the
  key mode-change doc)*:
  `https://commerce.hayward-pool-assets.com/magento/2026/LSCUS22030/2020_QRG_UCL_How_to_Change_Modes_and_Transformer_Chart_c3eb.pdf`
  (also mirrored at `hayward.com/media/akeneo_connector/asset_files/2/0/2020_QRG_UCL_How_to_Change_Modes_and_Transformer_Chart_c3eb.pdf`)
- **UCL & CrystaLogic Troubleshooting Guide (residential)**:
  `https://hayward.com/media/wysiwyg/pdf/heaters/universal-colorlogic-crystallogic-consumer-troubleshooting-guide.pdf`
- **UCL & CrystaLogic TSG (UCL100e)**:
  `https://hayward.com/media/akeneo_connector/asset_files/U/C/UCL100e_Universal_Color_CrystaLogic_TSG_0ec8.pdf`
- **UCL overview / quick reference**:
  `https://hayward.com/media/wysiwyg/pdf/lighting/ucl-overview.pdf`
- **ColorLogic Family Brochure (LITLEDFAM16)** *(show descriptions)*:
  `https://hayward.com/media/akeneo_connector/asset_files/c/o/colorlogic_family_brochure_LITLEDFAM16_b662.pdf`
- **Universal ColorLogic Install & Operation Manual** (ManualsLib, incl.
  troubleshooting p.13): `https://www.manualslib.com/manual/410451/Hayward-Universal-Colorlogic.html`
- **UCL & CL 4.0 Installation Guide**:
  `https://artisticpoolandspa.com/wp-content/uploads/2024/02/ColorLogic-Installation-Guide.pdf`
- **PoolDial ColorLogic Troubleshooting Guide** (mode flash colors):
  `https://www.pooldial.com/resources/articles/hayward/colorlogic/hayward-colorlogic-troubleshooting-guide`
- **Royal Swimming Pools — Operating the ColorLogic Light** (timing):
  `https://knowledgebase.royalswimmingpools.com/en/operating-the-hayward-colorlogic-light`
