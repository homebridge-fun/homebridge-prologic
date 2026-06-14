# AquaLogic / AquaConnect Automation Spec

Target: RS-485 navigation of a Hayward AquaLogic/ProLogic PS controller, fronted by an
AquaConnect web box, exposed to HomeKit as heater + VSP control.

**Provenance tags used throughout:**
- `[VERIFIED]` — observed live by walking the AquaConnect web UI at `http://192.168.50.100`.
- `[MANUAL]` — from the Hayward AquaLogic PS4/PS8/PS16 Operation Manual; not independently re-verified on this unit.
- `[OWNER]` — operational knowledge provided by the system owner; treat as authoritative requirements.
- `[PENDING]` — must be confirmed live (read-only) before implementation relies on it.

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

| Button | Code |
|---|---|
| RIGHT (`>`) | `01` |
| MENU | `02` |
| LEFT (`<`) | `03` |
| MINUS (`−`) | `05` |
| PLUS (`+`) | `06` |
| POOL / SPA / SPILLOVER | `07` |
| VALVE3 | `11` |
| HEATER1 | `13` |
| (blank pads) | `00` |

**FILTER button key code: NOT captured — see §9 [PENDING].** Required for the VSP activation cycle.

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

## 6. VSP speed override `[OWNER]` / `[PENDING]`

VSP speeds 1–3 are the owner's **scheduled** modes. The active speed/mode is reported on the Default cycling screen.

### 6.1 Hard rule: slot 4 only
**Any speed change Claude makes goes to slot 4. Never write slots 1–3.** Slot 4 is the designated override lane so automation never disturbs the schedule.

### 6.2 Activation procedure
A new slot-4 speed does **not** take effect on its own. Full sequence:
1. Enter `VSP Speed Settings` (PLUS `06` to descend).
2. Set the desired speed on **slot 4**. `[PENDING exact submenu strings/layout]`
3. Back out of the submenu.
4. Cycle **FILTER off → FILTER on**. `[PENDING FILTER key code — §9]`
5. Immediately, within a **limited time window** after the filter cycle, the display shows the default/current speed state; press **PLUS/MINUS to select the speed slot (1–4)** and choose **slot 4**. `[PENDING exact window text, slot-increment behavior, window duration]`

### 6.3 Conditional / verify
The temporary slot-4 override only engages **in limited situations**. Do **not** fire-and-forget: after the sequence, verify via the Default cycling screen that the pump actually landed on the new speed.

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

## 9. Pending live confirmations (read-only)

All confirmable without changing a value or cycling the filter:

1. **FILTER button key code** — scan the AquaConnect DOM for the `WebsProcessKey("NN")` on the FILTER cell. Required for §6 activation.
2. **VSP submenu** — exact LCD strings, layout, and how to reach/read **slot 4** (one PLUS to enter, then navigation).
3. **Post-filter-cycle window** — exact display text during the limited selection window, how the slot increments with PLUS/MINUS, and the window duration.
4. **Forced-off → writable timing** — whether a heater setpoint becomes writable the instant the heater leaves forced-off, or with lag (determines the §5.2 write rule: activate-then-write vs. reject/queue).

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
- Resolve every `[PENDING]` item (read-only) before enabling write paths that depend on it.
