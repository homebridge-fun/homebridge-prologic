# AquaLogic / AquaConnect Automation Spec
Target: RS-485 navigation of a Hayward AquaLogic/ProLogic PS controller, fronted by an
AquaConnect web box, exposed to HomeKit as heater + VSP control.

**Provenance tags used throughout:**
- `[VERIFIED]` — observed live by walking the AquaConnect web UI at `http://192.168.50.100`.
- `[MANUAL]` — from the Hayward AquaLogic PS4/PS8/PS16 Operation Manual; not independently re-verified on this unit.
- `[OWNER]` — operational knowledge provided by the system owner; treat as authoritative requirements.
- `[PENDING]` — must be confirmed live (read-only) before implementation relies on it.

---

## 0. Verified physical bridge connection `[VERIFIED]`

Established live on the actual hardware (ProLogic PS, main board sticker `G1--11049F-1`):

| Item | Value |
|---|---|
| Bridge | **USR-W610** (RS-485 mode), TCP **Server**, port **8899**, IP `192.168.68.101` |
| Serial params | **19200 / 8 / None / 2** (8N2) — must match the aqualogic library, which hardcodes 19200 + `STOPBITS_TWO` |
| Panel header | **J2** |
| RS-485 data pair | **pin 2 + pin 4** (two-wire A/B) |
| Pin 1 | NOT a data line (measured ~-2.2 V; red herring during bring-up) |
| Pin 3 | ~7.6 V bus power — **do not connect to the bridge** |
| Ground | not required on short runs (two-wire worked) |

**Bring-up gotchas (all hit during the first install):**
- The Waveshare UART-WIFI232-B2 produced identical garbage to the W610 — both bridges are fine; the problems were pin selection and framing.
- A correct connection shows a clean **8-byte repeating frame** in a raw TCP capture. If you see that but **no `10 02` frame starts**, A/B polarity is reversed — swap A and B on the bridge.
- Symptom map: `socket timeout` = no bytes at all (wrong pins / no link); `Frame timeout` / `Bad CRC` = bytes arriving but mis-framed (wrong pair, polarity, or stop bits); clean `10 02 … 10 03` with ASCII payload = success.

---

## 1. System facts

| Item | Value | Source |
|---|---|---|
| Controller family | Hayward AquaLogic / ProLogic PS | [VERIFIED] |
| Web front-end | AquaConnect ("Aqua Connect Local", page title `WebsR2-1.01`) at `http://192.168.50.100` | [VERIFIED] |
| Pool/Spa config | Pool **and** Spa (standard), **no spillover** | [VERIFIED]/[OWNER] |
| Solar | **None** | [OWNER] |
| Heaters | Single heater, generic name `Heater1`; no Heater2 | [VERIFIED] |
| Chlorination | Salt water generator (SWG) enabled | [VERIFIED] |
| Pump | Variable speed pump (VSP), schedule-driven | [VERIFIED]/[OWNER] |
| Wireless | Wireless base present (Teach Wireless + Wireless Channel items appear) | [VERIFIED] |

> Note: the `.100` AquaConnect box mirrors the panel LCD. Item **sequence** and **navigation paths** match the raw panel; some value fields render as web-only placeholders (e.g. `Display Light → "No Backlight Present"`, `Beeper → "Not Used Here"`).

---

## 2. Web UI key codes `[VERIFIED]`

Each on-screen button fires `WebsProcessKey("NN")`:

| Button | Code | Notes |
|---|---|---|
| RIGHT (`>`) | `01` | navigation |
| MENU | `02` | navigation |
| LEFT (`<`) | `03` | navigation |
| MINUS (`−`) | `05` | value down |
| PLUS (`+`) | `06` | value up / enter inline submenu |
| POOL | `07` | body/mode select |
| SPA | `07` | body/mode select |
| SPILLOVER | `07` | body/mode select |
| FILTER | `08` | pump on/off (used in VSP activation cycle, §6.2) |
| LIGHTS | `09` | toggle |
| AUX1 | `0A` | toggle |
| AUX2 | `0B` | toggle (unused/inert on this system) |
| VALVE3 | `11` | toggle |
| HEATER1 | `13` | heater enable toggle: `Manual Off` ↔ `Auto Control` |
| (blank pads) | `00` | no-op |

Codes appear to be hex byte values. **POOL / SPA / SPILLOVER all share `07`** — it is a single body/mode-select key; the web UI exposes three buttons that emit the same code, so selecting a specific mode means pressing `07` and reading the resulting mode off the LCD (it cycles), not addressing a mode directly.

> These are AquaConnect *web* key codes. Whether they map 1:1 to raw RS-485 keypad frame codes must be cross-checked against the AQ-CO-SERIAL protocol and the `swilson/aqualogic` reference implementation.

---

## 3. Navigation model `[VERIFIED]` / `[MANUAL]`

Two-level structure. The two cursor keys do different jobs:

- **MENU (`02`)** rotates the **top-level menus only** and wraps. It does **not** step items.
- **RIGHT/LEFT (`01`/`03`)** scroll **items within** the current menu and wrap. Only items applicable to this configuration appear.
- **PLUS/MINUS (`06`/`05`)** edit the selected item's value (shown highlighted/flashing). Hold >1 s to auto-scroll a numeric value; two-option items toggle. A change is "locked in" by moving off the item with RIGHT.

### 3.1 Top-level MENU cycle `[VERIFIED]`
```
Settings Menu → Timers Menu → Diagnostic Menu → Configuration Menu-Locked → Default Menu → (wraps to Settings Menu)
```
- `Configuration Menu-Locked` appears but is locked (installer only).
- `Default Menu` is the idle auto-scroll (clock, equipment status, temps). Display auto-returns here after ~2 min idle.

### 3.2 Default (idle) status cycle `[VERIFIED]`

Observed live with `lcd-watch` (120s, 2 full laps). Each item holds for **~6 seconds** before advancing automatically. Total cycle: 7 items × 6s = 42s.

| Order | Normalized text (navigator sees) | Notes |
|---|---|---|
| 1 | `Thursday HH:MMA` | Date/time. Colon `:` oscillates to space in LONG frame (same item — canonical tokens match). |
| 2 | `Pool Temp 77°F` | Degree char oscillates: SHORT frame sends `_`, LONG sends `°` (same item — canonical tokens `['Pool','Temp','77','F']` match). |
| 3 | `Air Temp 70°F` | Same SHORT/LONG oscillation as Pool Temp. |
| 4 | `Pool Chlorinator 40%` | LONG frame only; value on line 2, label on line 1. |
| 5 | `Salt Level 3200 PPM` | LONG frame only. |
| 6 | `Heater1 Manual Off` | LONG frame only. `Manual Off` = force-off state (see §5.2). |
| 7 | `Filter Speed 50% Speed2` | LONG frame only. Speed name (`Speed2`) is part of the normalized text. |

Occasional genuinely corrupted SHORT frames appear (~once per 60s); they contain control characters and start with a lowercase letter after normalization — the garbled-frame guard in `_same_item` ignores them.

Pressing **MENU** from any point in this cycle should exit it and show `Settings Menu` (§3.1 top-level ring). [PENDING — not yet confirmed; map-menu test needed]

### 3.2 Navigation rules (for the state machine)
1. **Anchor on text, not counts.** Drive `MENU` until LCD line 1 == `Settings Menu`, then count RIGHT from there. Do not trust blind key-counts past the first two items (conditional items + the Set Day/Time trap make counts fragile).
2. **The header is a stop.** RIGHT cycles: header → item 1 → … → item 11 → header.
3. **`Set Day and Time` is a multi-field trap.** RIGHT moves through sub-fields day → hour → minute *before* advancing to the next item. A naive RIGHT-counter will miscount unless it reads the text. (We do not navigate into/through this item — see scope §8.)
4. Always read both LCD lines back after each keypress and decide the next key from the text. The bus is slow; wait for the screen to settle.

---

## 4. Settings menu ring `[VERIFIED]`

Verbatim LCD, in order, with current observed values:

| # | Line 1 (verbatim) | Line 2 / value | Type |
|---|---|---|---|
| — | `Settings Menu` | (header) | header / anchor |
| 1 | `Spa Heater1` | `Manual Off` | heater setpoint (see §5) |
| 2 | `Pool Heater1` | `Manual Off` | heater setpoint (see §5) |
| 3 | `VSP Speed Settings` | `+ to enter` | **submenu** (PLUS descends) — see §6 |
| 4 | `Super Chlorinate` | `Off` | toggle |
| 5 | `Spa Chlorinator` | `1%` | numeric value |
| 6 | `Pool Chlorinator` | `30%` | numeric value |
| 7 | `Set Day and Time` | `Saturday 10:07P` | 3 sub-fields (day/hour/min) |
| 8 | `Display Light` | `No Backlight Present` | informational (web mirror) |
| 9 | `Beeper` | `Not Used Here` | informational (web mirror) |
| 10 | `Teach Wireless:` | `+ to start` | PLUS-action process |
| 11 | `Wireless Channel:` | `3` | numeric value |
| | (RIGHT from #11 wraps to `Settings Menu` header) | | |

**Confirmed absent** (matches config): no Heater2, no Spa/Pool Solar.

---

## 5. Heater setpoints

### 5.1 Navigation paths `[VERIFIED]`
Anchoring on the `Settings Menu` header:
- **Spa heater:** MENU until `Settings Menu` → RIGHT ×1 → `Spa Heater1` → PLUS/MINUS to adjust
- **Pool heater:** MENU until `Settings Menu` → RIGHT ×2 → `Pool Heater1` → PLUS/MINUS to adjust

These two items are first in the ring, before any conditional/variable items, so the RIGHT-count from the header is stable. Still text-match (`Pool Heater1` / `Spa Heater1`) to be safe.

### 5.2 Heater-active precondition `[OWNER]`
The `Manual Off` shown on both heater items is the **forced-off** state set by the `HEATER1` button (code `13`) — it is **not** a temperature. The setpoint only becomes an adjustable temperature **after the heater is activated** out of forced-off.

**Guard:** before treating a heater setpoint as dialable, confirm the heater is active (not forced-off). A write to a forced-off heater either (a) must first activate the heater, or (b) is rejected/queued. The exact rule depends on the writable-timing check in §9 [PENDING].

### 5.3 Mode drives the active setpoint `[MANUAL]`/`[OWNER]`
One physical heater obeys one setpoint at a time, selected by valve mode:
- **Pool mode** → Pool setpoint (`Pool Heater1`) is live.
- **Spa-only mode** → Spa setpoint (`Spa Heater1`) is live.

(No spillover on this system.) The **current mode is reported on the Default cycling screen.** The non-active setpoint persists but is inert in the current mode.

---

## 6. VSP speed override `[OWNER]` / `[VERIFIED]`

VSP speeds 1–3 are the owner's **scheduled** modes. The active speed/mode is reported on the Default cycling screen.

### 6.1 Hard rule: slot 4 only
**Any speed change Claude makes goes to slot 4. Never write slots 1–3.** Slot 4 is the designated override lane so automation never disturbs the schedule.

### 6.2 Activation procedure `[VERIFIED]`
A new slot-4 speed does **not** take effect on its own. Full sequence:
1. Enter `VSP Speed Settings` (PLUS `06`), navigate RIGHT to `Filter Speed4`, set the speed with `+`/`-` (5% steps).
2. Cycle **FILTER (`08`) off → on**. After pressing off, wait for the pump to fully stop (LCD shows `Filter / Off`) before pressing on.
3. On FILTER-on, the **slot-selection window** appears (see §6.4): `Filter On:SpdN` / `+/- to change`.
4. While that window is showing, press `+`/`-` to land on **`Spd4`**, then **do nothing** — let it time out. At timeout the displayed slot is committed and the pump runs at that slot's speed.

### 6.3 Conditional / verify
The temporary slot-4 override only engages **in limited situations**. Do **not** fire-and-forget: after the sequence, verify via the Default cycling screen (`Filter Speed / NN% SpeedN`) that the pump actually landed on the intended slot/speed.

### 6.4 Slot-selection window `[VERIFIED]`
Observed verbatim after a FILTER off→on cycle:
```
Line 1:  Filter On:SpdN      (N = selected slot, shown highlighted/inverted)
Line 2:  +/- to change
```
- **`+`/`-` cycle the slot NUMBER** (`Spd1…Spd4`), not a percentage. Confirmed `+` : `Spd3 → Spd4`; `-` decrements. (Format is `SpdN`, so it is slot selection, not value editing.)
- **Default pre-selection = the last-committed slot**, NOT necessarily the scheduled one. Observed: first cycle defaulted to `Spd3` (the running slot); after `Spd4` was committed, the next cycle defaulted to `Spd4`. So to re-trigger a slot-4 override that's already active, the window may already show `Spd4`; otherwise step to it with `+`/`-`.
- **Timeout: persistent for at least ~5 s** (observed still open at 1 s, 3 s, and 5 s after FILTER-on), then closes on its own. Treat the window as open for roughly 5–10 s; do not assume it's brief. Exact value not pinned (slow bus), but it is comfortably long enough to read the LCD and send one slot key.
- **After timeout the displayed slot is committed** and the pump runs at it.

> ⚠️ **Post-timeout hazard (critical for the navigator).** When the window times out, the display returns to **whatever menu item was parked underneath** — which, in this flow, is the `Filter Speed4` *value* item. Any `+`/`-` sent *after* the window has closed will then edit **that speed value**, not the slot. (Observed live: a `-` meant for the slot instead changed `Filter Speed4 50% → 45%`.) The window (`Filter On:SpdN`) and the speed-value item (`Filter SpeedN  NN%`) look superficially similar but are different contexts. **Rule:** gate every slot-select `+`/`-` on the LCD line 1 actually reading `Filter On:` — if it doesn't, the window is gone and `+`/`-` must not be sent.

---

## 7. Guard rules (hard constraints)

1. **Cooldown is sacrosanct `[OWNER]`.** Never cycle the filter or take any action that overrides or runs during a heater **cooldown** cycle. If a heater is running / cooldown is active or would be triggered, **abort** the operation. Speed changes are reportedly ignored in this state anyway, so it is both unsafe and futile.
   - Mechanism note `[MANUAL]`: if a heater is running with Heater Cooldown enabled, the **first** FILTER press stops only the heater and keeps the pump running ~5 min for cooldown; a **second** press is needed to actually stop the pump. A naive "one off, one on" cycle therefore breaks. Before any filter off→on, **verify the pump actually stopped** (FILTER LED / cycling screen) before pressing on.
2. **Heater-active precondition `[OWNER]`** (see §5.2): no setpoint write to a forced-off heater until activated.
3. **Slot-4-only for VSP `[OWNER]`** (see §6.1).
4. **Verify conditional overrides `[OWNER]`** (see §6.3): never assume success.
5. **Scope boundary** (see §8).

---

## 8. Scope boundary `[OWNER]`

Claude operates at the **top-level Settings menu**, plus exactly two permitted one-level-deep branches:
- **Heater temperature setpoints** — `Spa Heater1`, `Pool Heater1`.
- **VSP Speed Settings** — slot 4 only.

**Do NOT:**
- Enter `Teach Wireless`.
- Navigate into the day/hour/minute sub-fields of `Set Day and Time`.
- Touch `Timers`, `Diagnostic`, or `Configuration` menus.
- Write VSP slots 1–3.

Everything outside the permitted branches the owner handles manually.

---

## 9. Pending live confirmations

All items resolved. No remaining `[PENDING]` items.

1. ~~**FILTER button key code**~~ — **DONE**: `FILTER = 08` (§2).
2. ~~**VSP submenu**~~ — **DONE** (§12.4): Filter Speed1–4 + Spa Speed; PLUS to enter; 5% steps.
3. ~~**Post-filter-cycle window**~~ — **DONE** (§6.4): text `Filter On:SpdN` / `+/- to change`; slot-number cycling; defaults to last-committed slot; open ~5–10 s; post-timeout +/- hazard documented.
4. ~~**Forced-off → writable timing**~~ — **DONE** (§12.2): setpoint is revealed/writable the instant the heater leaves force-off (PLUS reveals stored °F immediately); enable/disable is the HEATER1 toggle, not a setpoint key. No remaining lag question.

---

## 10. HomeKit integration model

One physical heater with two mode-driven setpoints is exposed as **three thermostat accessories**, but with **only two pieces of stored state** (B and C). A is a read/write **passthrough**, not a third value.

### 10.1 Accessory A — mode-following mirror (favorite on Home tab)
- **Reads:** mirrors whichever of B/C is active per current valve mode; shows current water temp + active target.
- **Writes:** routes the write to whichever setpoint is currently active (Pool in pool mode, Spa in spa mode). Stores nothing itself.
- **Mode label:** the active mode must be legible. Since HomeKit thermostats have little free text, carry it in the **dynamic accessory/service name**, e.g. `Pool/Spa Heat — Pool` / `— Spa`. When mode flips, A repoints, the displayed target jumps to the new setpoint, and the name jumps with it (so the change reads as intentional, not a glitch).
- **Forced-off:** when the active heater is forced-off (`Manual Off`), A shows OFF regardless of mode.

### 10.2 Accessories B & C — fixed-mode controls (in Pool room detail)
- **B = `Pool Heat`** (always the Pool setpoint), **C = `Spa Heat`** (always the Spa setpoint). Authoritative; both always present.
- Each **flags its own state** (the spot where the owner is deliberately dialing):
  - **Forced-off** — heater in `Manual Off`.
  - **Standby** — heater active but its mode is not the current mode (stored target, inert right now).
  - **Heating** — active and current mode.
  - Convey via name + the OFF/HEAT current-state field, e.g. `Spa Heat — Off (forced)` / `Spa Heat — Standby (pool active)` / `Spa Heat — Heating`.

### 10.3 Consistency
- B and C are the single source of truth per mode. A forwards. Editing A in the active mode == editing B-or-C; editing B/C directly is reflected by A because A mirrors them. **No divergence** because there is no third stored value.
- Adjustments are allowed from **both** surfaces: A (mode-labeled, live) and B/C (fixed, hidden in the room).

### 10.4 Known HomeKit rough edges
- **Dynamic accessory names** are not perfectly idiomatic; some clients cache names. Pick one representation and stay consistent.
- There is **no native "enabled but inactive" thermostat state.** "Standby" must be approximated — likely OFF with the distinction carried in the name.
- Three thermostat accessories for one physical heater is unusual; **naming must make roles obvious** (`Pool/Spa Heat — <mode>` for A vs. `Pool Heat` / `Spa Heat` for B/C) to stay legible long-term.

---

## 11. Implementation notes for the navigator

- Build a **closed-loop state machine**: send key → read both LCD lines → decide next key from text. Never blind-count past the first two Settings items.
- Canonical anchor routine: `MENU` until line 1 == `Settings Menu`.
- All setpoint reads/writes go through the §5 paths; all VSP through §6; all gated by §7 guards and the §8 scope.
- VSP activation: gate every slot-select `+`/`-` on LCD line 1 reading `Filter On:` (§6.4 post-timeout hazard).

### 11.1 RS-485 frame type carries the menu display `[VERIFIED]`
The single most important undocumented detail, found by byte-level bus tracing:

- The panel sends the LCD over **two different frame types**:
  - `DISPLAY_UPDATE` (`0x01 0x03`) — the short single-line frames used by the
    **Default cycling screen** (clock, temps, equipment status). The
    `swilson/aqualogic` library parses these and calls `text_updated()`.
  - `LONG_DISPLAY_UPDATE` (`0x04 0x0a`) — the full 2×16 frame used by **all
    menu navigation** (Settings ring, submenus, value items). The library
    **ignores this frame type** (`# Not currently parsed / pass`) and never
    calls `text_updated()` for it.
- **Consequence:** an unpatched library is *blind during menu navigation* — the
  closed-loop read in §11 never sees the screen change, every keypress looks
  dropped, and the navigator spins until it exhausts its retry budget. This
  masqueraded as "the panel ignores RIGHT" for a long time. The keypress was
  landing fine; we just couldn't see the result.
- **Fix:** parse `LONG_DISPLAY_UPDATE` with the same decode the short handler
  uses and forward it to the LCD capture.

### 11.2 LONG frame decoding quirks `[VERIFIED]`
- **Bit 7 = flashing.** Characters shown flashing on the physical display
  arrive with bit 7 set (`char | 0x80`); mask it off (`& 0x7f`) or text won't
  match (upstream `swilson/aqualogic` PR #11).
- **Degree symbol** is `0xdf` → render as `°` (same as the short handler).
- **Frame payload structure (verified by raw hex dump):**
  - Two LONG frame variants exist with headers of different lengths (3 bytes: `83 00 03`; or 12 bytes: `83 00 02 28 00 00 00 00 00 00 00 03`).
  - Both variants end identically: **40 LCD bytes** (20-char line 1 + 20-char line 2, bit-7 flashing on editable values) followed by a `0x00` null terminator.
  - The panel uses a **20-character-wide** LCD (not 16), centring label text with leading spaces.
  - Short frames (len < 41, e.g. 11-byte cursor/blink control packets) must be **skipped** — they do not contain LCD text. Previously they decoded as garbage like `'ju %'` which locked the navigator.
  - **Correct extraction:** `frame[-41:-1]` always yields the 40 LCD bytes regardless of header length.
- **SHORT vs LONG character differences.** Items that appear on both frame types show slight differences that are the **same screen content**:
  - Degree symbol: SHORT frame sends `0x5F` (`_`), LONG frame sends `0xDF` (`°`). Tokens `['77','F']` match after stripping non-alphanumeric.
  - Colon in time: SHORT frame sends `:`, LONG frame sends ` ` (space). Tokens `['9','26A']` match.
  - Fix: `_same_item` compares canonical alphanumeric token lists, so these oscillations are invisible to the navigator.
- **Occasional garbled SHORT frames** appear ~once per 60s on this bus; they
  contain control characters and start with a lowercase letter after
  normalization. The `_same_item` garbled-frame guard (non-uppercase first char
  → same item) prevents the navigator from acting on them.

---

## 12. Live menu examples & behavior `[VERIFIED]`

Captured live with single PLUS / single MINUS (revert) probes. Use these as the canonical
before/after examples for each value type.

### 12.1 Editing model (general)
- **No "enter"/"exit" key.** PLUS on a `+ to enter` item expands its sub-items **inline** into the ring; you then walk them with RIGHT/LEFT like any other item, and leaving is just continuing past them (or pressing MENU, which drops to the next Settings item). Confirmed: walking past the last VSP sub-item (`Spa Speed`) lands on `Super Chlorinate`, the next main-ring item.
- **Numeric step size = 5%** for chlorinator and pump-speed items (observed 30→35, 50→55).
- **A change takes effect immediately** as you press +/-; there is no separate commit.

### 12.2 Temperature (heater) — NON-symmetric, special revert
`Pool Heater1` (and `Spa Heater1`) display `Manual Off` when the heater is force-off via the HEATER1 button.

| Action | LCD line 1 | LCD line 2 |
|---|---|---|
| start | `Pool Heater1` | `Manual Off` |
| PLUS ×1 | `Pool Heater1` | `87°F` |

- **PLUS does NOT increment a temperature ladder from "Manual Off."** It clears the force-off and reveals the **stored setpoint** (here 87°F), i.e. it effectively enables the heater. A single MINUS does **not** undo this (it would give 86°F).
- **To restore `Manual Off`:** toggle the **HEATER1 button (code `13`)**. HEATER1 is a 2-state toggle: `Auto Control` ↔ `Manual Off`. From the PLUS-enabled state it took **two** HEATER1 presses to return to `Manual Off` (→ `Heater1 / Auto Control` → `Heater1 / Manual Off`).
- **Implication for automation:** never probe a force-off heater with +/- expecting a reversible nudge. To set a temperature you must first bring the heater out of force-off (HEATER1), then +/- adjusts °F normally; to disable, HEATER1 back to `Manual Off`. This is the concrete form of the §5.2 heater-active precondition.

### 12.3 Chlorinator — symmetric, fully reversible
| Action | LCD line 1 | LCD line 2 |
|---|---|---|
| start | `Pool Chlorinator` | `30%` |
| PLUS ×1 | `Pool Chlorinator` | `35%` |
| MINUS ×1 | `Pool Chlorinator` | `30%` |

Clean 5% ladder; up-once-back fully restores. (`Spa Chlorinator` observed at `1%`, same control type.)

### 12.4 VSP Speed Settings — inline sub-items
PLUS on `VSP Speed Settings / + to enter` expands these, walked with RIGHT:

| Order | LCD line 1 | LCD line 2 (current) | Notes |
|---|---|---|---|
| 1 | `Filter Speed1` | `95%` | **scheduled — do not change** |
| 2 | `Filter Speed2` | `60%` | **scheduled — do not change** |
| 3 | `Filter Speed3` | `50%` | **scheduled — do not change** |
| 4 | `Filter Speed4` | `50%` | **override lane — Claude may write this only** |
| 5 | `Spa Speed` | `80%` | not a filter slot; do not change |
| → | (`Super Chlorinate` …) | | RIGHT past `Spa Speed` exits to main ring |

Slot-4 probe (the only writable slot): `Filter Speed4 50%` → PLUS → `55%` → MINUS → `50%`. Symmetric 5% step, reversible. **Slots 1–3 and Spa Speed were navigated read-only.**

### 12.5 Default-screen indicators (for closed-loop reads)
While idle/cycling, the Default menu surfaces live state used by the guards:
- `Filter Speed / Off` — current pump speed/mode (here Off; pump not running).
- `Heater1 / Auto Control` or `Heater1 / Manual Off` — current heater enable state.
- POOL button highlighted = pool mode active (drives which heater setpoint is live, §5.3).

### 12.6 Status of prior [PENDING] items (§9)
All resolved. See §9.

---

## 13. Menu exit & state management `[OWNER]`

### 13.1 Fast menu exit (return to Default display)
To leave the menu quickly when done:
- **MENU until `Default Menu`, then RIGHT once.** Drops straight to the idle/cycling Default display. **Non-mutating** (no equipment state changes). This is the exit method to use.

(Idle timeout also auto-returns to Default after ~2 min, but don't rely on the wait.)

> Note: toggling an equipment button does **not** exit the menu — it briefly flashes that output's status on the LCD, then the display returns to the menu. Do not use a toggle as an exit.

### 13.2 Maintain a cached state model (do not rely on bus refresh)
The controller is only observable through the **slow RS-485/LCD bus**, and values appear only as the Default screen cycles. The navigator must **continuously read the bus and cache the full current state** (heater enable + setpoints, pump speed/mode, chlorinator %, valve mode, etc.) so it always knows the complete current state **without waiting for the next refresh**. Update the cache whenever a value scrolls by or a change is made.

### 13.3 Restore-to-prior-state requirement (critical)
Before any change, **snapshot the relevant prior state**; after the operation, **return the system to that exact state**. Several settings have *coupled* state that a single value write does not capture.

**Heater is the canonical case** (couples enable-state + setpoint — see §12.2). If the heater is **Off** and the user sets a target:
1. **Cache** that the heater was Off (`Manual Off`).
2. **Enable** the heater so the setpoint is adjustable (HEATER1 toggle; PLUS in the setpoint item reveals/engages the stored °F).
3. Navigate to the setpoint and set the target with `+`/`-`.
4. **Toggle HEATER1 back to `Manual Off`** to restore the original disabled state.

Without the cached "was Off" fact, the navigator would leave the heater **enabled** — a real, unintended change to the pool. The cached snapshot is what tells it to re-disable. Apply the same snapshot→change→restore discipline to any coupled or mode-dependent setting.

---

## 14. Plugin configuration `[OWNER]`

The plugin must be configurable to match the panel's actual setup and the user's preferences, rather than assuming a fixed layout.

### 14.1 Active bodies / modes (POOL / SPA / SPILLOVER)
POOL, SPA, and SPILLOVER are **one cycling button** (shared key `07`, §2): each press advances to the next body, wrapping. The cycle only includes the bodies the **panel** is configured for.

- **Config item:** which bodies are active (`pool`, `spa`, `spillover`). **This system: `pool` + `spa` only** (no spillover).
- **Behavior driven by it:**
  - Mode selection by key `07` must expect/produce only the active bodies (here pool↔spa, a 2-state cycle), reading the LCD to confirm the landed mode — never blind-counting.
  - The mode-driven active-setpoint logic (§5.3) keys off this set: with spillover disabled, the live heater setpoint is simply Pool (pool mode) or Spa (spa mode).
  - HomeKit accessory A's mode label (§10.1) only ever shows configured modes.

### 14.2 Hideable toggles / accessories
The user can choose which equipment toggles are exposed (as HomeKit accessories and/or as automation targets). Hiding is a **plugin-presentation choice only** — it does not change the panel; the button still exists on the panel/web UI and may still be driven internally if needed.

- **Config item:** per-toggle visibility for `FILTER`, `LIGHTS`, `AUX1`, `AUX2`, `VALVE3` (and the body button group).
- **This system — hide: `AUX2` (`0B`) and `VALVE3` (`11`)**, both unused. In use / exposed: `POOL`/`SPA` (mode), `FILTER` (`08`), `HEATER1` (`13`); `LIGHTS`/`AUX1` per user preference.
- **Behavior:** hidden toggles are omitted from the HomeKit bridge and from any UI lists. Guards still apply to anything the plugin does drive internally (e.g. FILTER for the VSP activation cycle stays functional even if not surfaced as a user toggle).

### 14.3 Bridge address (connection)
The AquaConnect bridge's network address must be a **user setting**, not hardcoded.

- **Config item:** bridge host/IP (and port if non-default). **This system: `192.168.50.100`** — use as the default but keep it user-editable.
- Used as the base for the AquaConnect web/local interface (`http://<host>/`) and for reaching the controller over the bus via that box.
- The plugin should fail gracefully / surface a clear connection error if the bridge is unreachable, rather than silently stalling on the slow bus.

> Default the config to this system's values (pool+spa active; AUX2 + VALVE3 hidden; bridge `192.168.50.100`) but keep every item user-overridable so the plugin ports to other AquaLogic/ProLogic setups.
