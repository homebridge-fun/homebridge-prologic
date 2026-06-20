# v0.2.0

Running on hardware (Hayward AquaPlus PS-8, AquaConnect backend). Focus of this
release: heater clarity, sensor accuracy, write-path safety, and a faster,
lower-load menu navigation engine.

## Highlights

### Heater — two-switch model
Replaced the unreliable three-state heater fan (Apple Home ignores
`CurrentFanState` and spins any active fan) with two clear tiles:
- **Heater Auto** — tappable; reflects the armed / Auto-vs-Manual-Off state.
- **Heater Running** — read-only; lit only when the relay is actually firing
  (`heater_active`, read from the LED broadcast on both backends).

### Salt sensor — reads the real value
`VOCDensity` defaults to a HAP max of 1000, which was silently clamping the
~3200 PPM reading. Raised to **4000** so the true salt level shows, with
headroom. (HomeKit limitations noted: the unit renders as µg/m³ and the
"Excellent" air-quality label can't be hidden — no standard HomeKit sensor
offers a "ppm" unit.)

### VSP speed slot tiles — robust and safe
- **Pre-fetch on startup** so Speed 1–4 show real values instead of a blank 0%.
- **Debounced writes** — dragging the slider commits once on release, not a
  menu navigation per intermediate step.
- **Zero guard** — a tap without a drag can no longer set a slot to 0% and stop
  the pump.
- **Per-slot floor** (`vspSlotMinPct`, slot 1 defaults to 35%) so the slider
  can't target a speed the panel clamps.

### Chlorinator % — now writable
The Chlorinator tile writes the current body's output % (`/chlorinator/{pool|spa}`,
valve-mode aware), debounced 600 ms, reverting on failure. Sidecar snaps to the
panel grid (1% below 10%, 5% at/above).

### Thermostat — debounced setpoint
Dragging the temperature dial previously fired a full menu navigation per
0.5 °C step. Now debounced 600 ms (commit final value only, revert on failure)
— this closed the last drag-driven wedge vector.

### Faster navigation: frame-reader + 0.6 s gap
- **Frame-reader architecture**: a single shared poll-loop reader delivers
  confirmation frames via a condition variable. `send_nav_key` presses, wakes
  the reader, and waits for one confirming frame — **N+1 requests** per N
  keypresses, down from N×5.
- **Inter-request gap lowered 0.9 s → 0.6 s** (empirically validated; ~0.5 s is
  the wedge floor).

### Wedge-risk audit
Every slider-driven `onSet` that triggers menu navigation is now debounced
(chlorinator, pump, VSP slots, thermostat setpoint). Discrete toggles are
single, gap-protected keypresses. The sidecar's stepping loops stop at hardware
floors/ceilings instead of hammering the panel.

## New debug/tuning endpoints
- `POST /debug/nav-benchmark` — time a menu read over N laps under chosen timing
  params; reports wall-time and dropped/re-pressed counts.
- `POST /debug/nav-sweep` — sweep `min_gaps` (and `post_menu_settles`) in one
  call; aborts early and returns partial results on a wedge; ranks clean runs
  fastest-first.
- `GET /debug/nav-trace`, `POST /debug/nav-trace/clear`.

## Config additions
- `vspSlotMinPct` — per-slot speed floor (e.g. `{ "1": 35 }`).

## Notes
- Both backends (AquaConnect HTTP, RS-485) build; AquaConnect is the validated
  path. RS-485 nav parity is implemented but not end-to-end re-verified.
- Backlog remaining: external frame-watcher service, FILTER-as-Fanv2, spa heater
  setpoint confirmation, system-fault indicator. See `docs/plugin-spec.md` §10.

**Full spec:** `docs/plugin-spec.md` (v3.2).
