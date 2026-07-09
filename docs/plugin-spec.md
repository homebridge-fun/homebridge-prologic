# Homebridge ProLogic Plugin — Authoritative Specification

> **Version**: 3.2 — heater two-switch model, salt sensor max 4000, VSP slot tile
> robustness (pre-fetch/debounce/zero-guard/floor), nav timing optimization (0.6s gap +
> adaptive reads), `/debug/nav-sweep` + `/debug/nav-benchmark` harnesses
> **Updated**: 2026-06-20
> **Status**: Implemented and running on hardware (v0.2.0)

---

## 1. Project Goal

A Homebridge 2.0 platform plugin that controls a **Hayward Goldline AquaPlus PS-8** pool
controller via a Python sidecar that supports two interchangeable backends:

- **AquaConnect** (primary): Hayward's own local web interface on a ACHN box at
  `192.168.50.100`. The embedded GoAhead "Webs" HTTP server handles polling reads and
  keypress commands over TCP/IP. No cloud dependency; all traffic is LAN-only.
- **RS-485** (alternative): Direct serial protocol via a USR-W610 WiFi-to-RS-485 bridge
  connected to the panel's J2/J4 RS-485 port, using the `aqualogic` Python library.

The active backend is selected in plugin config and persisted by the sidecar. Both backends
expose the same REST API to the Homebridge plugin. Switching backends restarts the sidecar
automatically via systemd.

---

## 2. System Architecture

```
                ┌─ AquaConnect backend ──────────────────────────────────┐
                │  POST /WNewSt.htm (Update Local Server&)  ←read poll   │
[Hayward ACHN]  │  POST /WNewSt.htm (KeyId=NN&)            ←keypress     │
[192.168.50.100]└────────────────────────────────────────────────────────┘
       OR                           ↕ TCP/HTTP (picky GoAhead server)
                ┌─ RS-485 backend ────────────────────────────────────────┐
[AquaPlus panel]│  RS-485 broadcast frames (status)                       │
[J2/J4]         │  RS-485 key commands (circuit toggles, menu nav)        │
[USR-W610 bridge└────────────────────────────────────────────────────────┘
@ 192.168.68.101]           ↕ TCP (raw RS-485 byte stream)

                     [Pi 4: pool_service.py  ← systemd: pool-sidecar]
                              ↕ localhost:5757 REST
                     [Pi 4: Homebridge + TypeScript plugin]
                              ↕ HAP
                     [HomeKit / iPhone]
```

The Python sidecar (`pool_service.py`) runs as a systemd service. The Homebridge TypeScript
plugin polls the sidecar's `/status` REST endpoint every `pollInterval` ms (default 5000).

---

## 3. AquaConnect Backend

### 3.1 The Two POST Bodies — Critical Distinction

The AquaConnect embedded firmware (`WebsFuncs.js`) routes `POST /WNewSt.htm` through
**two entirely separate code paths** depending on the POST body:

| Body | Firmware path | Effect |
|---|---|---|
| `KeyId=NN&` | `WebsProcessKey()` | Registers a keypad event; controls circuits/menu |
| `Update Local Server&` | `ReqWebsData()` | Pure read; returns current LCD frame and LED state |

**`Update Local Server&` is how the native web UI refreshes its live LCD display.**
Discovered by inspecting the firmware's JavaScript (`WebsFuncs.js`, `setInterval` calling
`ReqWebsData()`). The sidecar enforces this via two separate methods:

```python
def _read(self) -> Optional[str]:
    return self._request('Update Local Server&')   # pure read, no keypad event

def _post(self, key_code: str) -> Optional[str]:
    return self._request(f'KeyId={key_code}&')     # keypress event
```

### 3.2 Why `KeyId=00&` Was Causing the Wedge

Previous versions polled with `KeyId=00&` as a "no-op read." This was wrong: `KeyId=00`
goes through `WebsProcessKey()` like any other key, injecting ~29,000 phantom keypad events
per day. After several hours the firmware's event queue wedged: HTTP returned 200, reads
worked, but keypresses were silently dropped until power-cycle. Switching to
`Update Local Server&` eliminates phantom event injection entirely.

### 3.3 Request Format

The GoAhead server is picky about headers — extra headers cause it to silently ignore the
request. The sidecar hand-builds the raw HTTP request over a plain socket:

```
POST /WNewSt.htm HTTP/1.1\r\n
Host: {host}\r\n
Content-Type: application/x-www-form-urlencoded\r\n
Content-Length: {len}\r\n
\r\n
{body}
```

Minimum gap of **0.6s** between any two requests enforced (`_AC_MIN_GAP_S`). Each request
opens a fresh connection (no pipelining). The gap applies to **all** requests (reads and
keypresses) — empirically the AquaConnect box itself needs the inter-request spacing, not
just the panel: a press-only gap (skipping reads) was measured *worse* (19 drops/3-lap run
vs 2). Characterized via `/debug/nav-sweep` on 2026-06-20: 0.6s gives ~15s per slot read
and ~2 drops; the original 0.9s gave ~27s. Below ~0.5s the box wedges (presses ACKed but
dropped at the RS-485 relay; only a power-cycle clears it).

### 3.4 Response Format

Meaningful data lives inside `<body>…</body>`, CRLF-separated, each line terminated with
the literal `xxx`. The LCD text lines rotate through Pool Temp / Air Temp / Salt Level /
Chlorinator % / Filter Speed / etc. The final line is the 12-character equipment-state
field, e.g. `TECD4C333333` — each LED is one 4-bit nibble: `3`=absent, `4`=off, `5`=on,
`6`=blink.

HTML span tags appear in some frames: `Super Chlorinate <span class="WBON">Off</span>`.
State detection must match `>\s*On\s*<` with regex — plain `'on'` substring falsely matches
`'WBON'`.

### 3.5 AquaConnect Key Codes

| Button | KeyId |
|---|---|
| RIGHT | 01 |
| MENU | 02 |
| LEFT | 03 |
| MINUS | 05 |
| PLUS | 06 |
| POOL/SPA | 07 |
| FILTER | 08 |
| LIGHTS | 09 |
| AUX_1 | 0A |
| AUX_2 | 0B |
| HEATER_1 | 0D |

### 3.6 Scroll Patterns

The background poll parses numeric values from the cycling LCD text via `_AC_SCROLL_PATTERNS`:

| Field | Pattern |
|---|---|
| `pool_temp` | `Pool Temp  NN` |
| `air_temp` | `Air Temp  NN` |
| `spa_temp` | `Spa Temp  NN` |
| `salt_level` | `Salt Level  NNNN` |
| `chlorinator_percent` | `Pool Chlorinator  NN%` |
| `pump_speed` | `Filter Speed  NN%` |
| `vsp_active_slot` | `Filter On:SpdN` (appears during slot-selection window) |

### 3.7 Settings Menu (AquaConnect, verified on hardware)

```
Settings Menu → Spa Heater1 [°F] → Pool Heater1 [°F] → VSP Speed Settings [+ to enter]
→ Super Chlorinate [On/Off] → Spa Chlorinator → Pool Chlorinator → … (wraps)
```

Frame format for Super Chlorinate: `Super Chlorinate <span class="WBON">Off</span>` /
`On</span>`. PLUS = Off→On, MINUS = On→Off.

VSP submenu entered via PLUS from `VSP Speed Settings`; items are `Filter Speed1` through
`Filter Speed4`. The slot-selection window (`Filter On:SpdN` / `+/- to change`) appears
transiently when FILTER turns on; PLUS cycles slots.

---

## 4. RS-485 Backend

### 4.1 Hardware Connection

- **Connector**: J2 or J4 on the AquaPlus main PCB (parallel, either works)

| J2/J4 pin | Label | Wire color | USR-W610 terminal |
|---|---|---|---|
| Pin 2 | DATA+ | black | A+ |
| Pin 3 | DATA− | yellow | B− |
| Pin 4 | GND | green | GND |

Pin 1 is not a data line. If CRC errors persist, swap A and B at bridge terminals.

### 4.2 USR-W610 Configuration

| Setting | Value |
|---|---|
| Mode | STA |
| Network protocol | TCP Server |
| Local port | 8899 |
| Baud rate | 19200 |
| Stop bits | **2** (8N2) ← critical |
| Transparent mode | Enabled |

### 4.3 What the `aqualogic` Library Provides

Available from RS-485 broadcast frames (always current):
pool/air/spa temp, salt level, chlorinator %, circuit states (POOL, SPA, FILTER, LIGHTS,
AUX1, AUX2, SPILLOVER, HEATER1).

**Not available from broadcasts** (menu navigation required):
heater setpoints, heater enable/disable state, chlorinator % writes, VSP slot speeds.

---

## 5. Sidecar Architecture (`sidecar/pool_service.py`)

### 5.1 Backend Selectability

Stored in `backend.json` adjacent to the script. `POST /backend` persists and calls
`os._exit(0)`; systemd restarts into the new backend. Plugin's `reconcileBackend()` checks
and switches on launch.

### 5.2 Shared State

```python
@dataclass
class PoolState:
    circuits: dict              # {circuit_name: bool}  includes SUPER_CHLORINATE
    pool_temp: float | None
    air_temp: float | None
    spa_temp: float | None
    salt_level: float | None
    chlorinator_percent: float | None
    pump_speed: int | None       # live running filter speed from scroll
    pool_setpoint_f: int | None  # null until menu read
    spa_setpoint_f: int | None
    pool_heater_enabled: bool | None   # True=Auto mode, False=Manual Off
    spa_heater_enabled: bool | None
    heater_active: bool | None   # relay firing right now (from led['heater'])
    valve_mode: str | None       # 'pool' | 'spa'
    vsp_slot_pct: dict           # {1: pct, 2: pct, 3: pct, 4: pct}
    vsp_active_slot: int | None  # 1–4; set on activate or from scroll
    bridge_wedged: bool
    connected: bool
    last_update: float
```

### 5.3 AquaConnect Poll Loop

**Frame-reader architecture** (single shared reader). The background `_poll_loop` is the
*only* thing that reads frames. It calls `_read()` (`Update Local Server&`) in a loop and,
after each successful frame, `notify_all()`s a shared `_frame_cond`. Reads carry no keypad
side-effect, so they are safe to interleave between keypresses — the poll loop no longer
skips during navigation.

`send_nav_key` presses the key under `_http_lock`, releases it, sets `_read_wake` to wake
the poll loop immediately (instead of waiting up to `poll_s`), then blocks on `_frame_cond`
for the next frame (3s timeout). The panel shows the confirmation state right away, so one
read is enough. Net: **N keypresses make N+1 requests** (1 per key + 1 confirming read
each), down from the old N×5 (1 press + 4 confirm reads each). This replaced the previous
confirm-burst/adaptive-read approach. The explicit post-`set_heater_enabled` read is gone —
the frame reader delivers the confirmation naturally.

### 5.4 `_PriorityLock`

Wraps `threading.Lock`, tracking queued waiters. `busy()` = held OR queued. Poll loop
defers when busy so navigation sequences aren't interrupted by concurrent reads.

### 5.5 Heater Enabled vs Heater Active

| Field | Meaning | Source |
|---|---|---|
| `pool/spa_heater_enabled` | Heater armed (Auto mode) | Menu navigation / scroll |
| `circuits['HEATER_1']` | Heater armed (Auto mode), LED-derived fallback | `HEATER_AUTO_MODE` LED bit |
| `heater_active` | Relay firing right now (element on) | `led['heater']` nibble (on/blink = firing) |

Three distinct signals. **Armed** (`heater_enabled` / `circuits['HEATER_1']`) means the
heater is in Auto mode and will call for heat when the pump runs and temp is below setpoint.
**Active** (`heater_active`) means the relay is firing *right now*. `heater_active` is read
from the LED broadcast on both backends (`led['heater']`) — note `get_state(States.HEATER_1)`
throws on AquaConnect because the `_states` bitmap is RS-485-only.

The plugin surfaces these as two switches (see §7.4): "Heater Auto" (armed) and
"Heater Running" (active).

### 5.6 Bridge Wedge Detection

**Passive**: `_record_command_failure()` debounced; after threshold=2, fires active probe.
**Active** (`_ac_canary_probe`): presses AUX2 (inert on this system), checks AUX2 LED
nibble flips. Sets `bridge_wedged=True` immediately on failure. Probe runs every 300s
(healthy) / 30s (wedged).

### 5.7 VSP Slot Navigation

`MenuNavigator` supports all 4 slots via `_goto_vsp_slot(n)`, `read_vsp_slot(n)`,
`set_vsp_slot(n, pct)`, `activate_vsp_slot(n)`. `read_vsp_all_slots()` reads all 4 in one
menu session. Activation cycles FILTER off→on to open the slot-selection window, then
cycles PLUS until the target `SpdN` label appears. `vsp_active_slot` is set in state on
activation success and also parsed passively from `Filter On:SpdN` scroll frames.

### 5.8 Super Chlorinate Navigation

`set_super_chlorinate(on)` navigates to the Settings menu item, detects current state via
`re.search(r'>\s*On\s*<', txt)`, and presses PLUS (Off→On) or MINUS (On→Off) only if
the state needs to change. Updates `state.circuits['SUPER_CHLORINATE']`.

### 5.9 Debug

All requests traced to `/tmp/pool_sidecar_debug.log` (hourly rotation, 1 backup).
`GET /debug/log` downloads; `?all=1` includes previous hour.
`GET /superchlorinate/inspect` navigates to that item and returns raw frames — read-only.

---

## 6. REST API (`localhost:5757`)

### 6.1 Status

| Method | Path | Description |
|---|---|---|
| GET | `/status` | Full pool state JSON |
| GET | `/display` | Current LCD line1, line2 (RS-485) |
| GET | `/display/history` | Last 60 LCD frames (RS-485) |

### 6.2 Circuit Control

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/circuit/{name}` | `{"on": bool}` | FILTER, LIGHTS, AUX_1, AUX_2, HEATER_1 |
| POST | `/mode` | `{"mode": "pool"\|"spa"}` | Valve mode switch |
| POST | `/superchlorinate` | `{"on": bool}` | Settings menu nav; updates `circuits['SUPER_CHLORINATE']` |

HEATER_1 routes through `set_heater_enabled()`. SUPER_CHLORINATE has no keypad key —
uses Settings menu navigation on both backends.

### 6.3 Heater

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/heater/{which}/state` | — | Read setpoint + enabled state |
| POST | `/heater/{which}/setpoint` | `{"temp_f": int}` | Write setpoint [65–104°F] |
| POST | `/heater/{which}/enable` | `{"on": bool}` | Auto / Manual Off |

### 6.4 VSP Slots

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/vsp/slots` | — | Read all 4 slot speeds in one menu session |
| GET | `/vsp/slot/<n>` | — | Read slot n (1–4) |
| POST | `/vsp/slot/<n>` | `{"speed_pct": int}` | Write slot n; snaps to 5% grid |
| POST | `/vsp/slot/<n>/activate` | — | Activate slot n via FILTER off→on window |
| GET | `/vsp/slot4` | — | Legacy alias → `/vsp/slot/4` |
| POST | `/vsp/slot4` | — | Legacy alias |
| POST | `/vsp/slot4/activate` | — | Legacy alias |

### 6.5 Chlorinator

| Method | Path | Body |
|---|---|---|
| POST | `/chlorinator/{which}` | `{"percent": int}` |
| POST | `/superchlorinate` | `{"on": bool}` |

### 6.6 Bridge Health

| Method | Path | Notes |
|---|---|---|
| GET | `/bridge/health` | Cached wedge state |
| GET | `/bridge/health?probe=1` | Live canary probe |
| POST | `/bridge/health/reset` | Clear wedge flag manually |

### 6.7 Backend

| Method | Path | Notes |
|---|---|---|
| GET | `/backend` | `{"active": "aquaconnect"\|"rs485", "config": {...}}` |
| POST | `/backend` | `{"backend", "aquaconnect_host"?, "rs485_host"?, "rs485_port"?}` — persists + restarts |

### 6.8 Backend toggle, live stream, and benchmark

The two backends are **complete, isolated interfaces**; exactly one is active at
a time (the idle one is fully silent — its process paths don't run, so its
bridge never touches the panel). Validation is **single-transport**: toggle the
whole sidecar to a backend, then read/write/benchmark entirely on that bridge —
no cross-bridge contention. Toggling RS-485 → permanent is the production swap.

| Method | Path | Notes |
|---|---|---|
| POST | `/backend/toggle` | Flip active backend to the other and restart into it (clears `observe_rs485` for clean single-transport). Returns `{from, to, restarting}` |
| GET | `/stream` | SSE feed of LCD frames from the **active** backend (recent tail, then live). `data: {seq, ts, text, raw}`; `: heartbeat` on idle |
| GET | `/stream/<name>` | SSE feed from a named backend's `FrameHub` (`aquaconnect`\|`rs485`) |
| GET | `/backends` | List backends: `{name, role: active\|observer\|inactive, frames_seen, last_frame_ts}` |
| POST | `/benchmark/<name>` | Nav speed test. Body `{laps?=3, slot?=1, key_predelay_ms?, key_burst?, key_timeout?, post_menu_settle?, min_gap?}`. Reports per-lap wall time, presses, drops, `drop_rate_pct`; RS-485 adds `avg_key_latency_ms`, AC adds `requests_per_press`. **Prefers the active backend** (`mode: active`, single-transport); falls back to the observer (`mode: observer`) only if rs485 is requested while AC is active |
| POST | `/benchmark/rs485/sweep` | Sweep `predelays_ms` (default `[20,30,50,70,100,130,160,200]`) to find the panel's post-keep-alive accept window; ranks by drop rate; aborts early if the panel gets stuck |

**RS-485 keypress timing.** The WiFi serial bridge has a narrow accept window
after each keep-alive frame; `key_predelay_ms` (default 70) targets it. Missing
it drops the key. Use `/benchmark/rs485/sweep` to find the lowest-drop predelay
for the panel, then bake it into the sidecar's `--key-predelay-ms` startup arg.

**Parallel observer (optional, off by default).** `--observe-rs485` runs an
observe-only RS-485 listener alongside an active AquaConnect backend, streaming
to `/stream/rs485` with an isolated state snapshot. It exists for a future live
side-by-side dashboard, but is **not** the validation path — single-transport
toggling is, because mixing a read on one bridge with a write on the other
muddies measurements and doesn't reflect the swapped end-state.

### 6.9 Debug

| Method | Path | Notes |
|---|---|---|
| GET | `/debug/log` | Trace log; `?all=1` includes previous hour |
| GET | `/superchlorinate/inspect` | Read-only frame capture for that menu item |
| POST | `/debug/wedge-test` | One-shot wedge scenario harness |
| GET | `/debug/wedge-test` | Poll wedge-test result |
| GET | `/debug/nav-trace` | Last N keypress traces (`?n=60`), newest last |
| POST | `/debug/nav-trace/clear` | Clear the nav trace ring buffer |
| POST | `/debug/nav-benchmark` | Time `read_vsp_slot` over N laps under chosen timing params; reports wall-time + dropped/re-pressed counts |
| POST | `/debug/nav-sweep` | Sweep `min_gaps` (and optionally `nav_gaps`/`post_menu_settles`) in one call; aborts early + returns partial results on a wedge; ranks clean runs fastest-first |

---

## 7. Homebridge Plugin (TypeScript)

### 7.1 Platform Config

```json
{
  "platform": "ProLogic",
  "name": "ProLogic",
  "sidecarHost": "127.0.0.1",
  "sidecarPort": 5757,
  "pollInterval": 5000,
  "backend": "aquaconnect",
  "aquaconnectHost": "192.168.50.100",
  "rs485Host": "192.168.68.101",
  "rs485Port": 8899,
  "circuits": ["FILTER", "LIGHTS", "HEATER_1", "AUX_1", "AUX_2", "SUPER_CHLORINATE"],
  "activeBodies": ["pool", "spa"],
  "enableActiveHeaterThermostat": true,
  "enablePoolHeaterThermostat": true,
  "enableSpaHeaterThermostat": true,
  "enableTemperatureSensors": true,
  "enableSpaModeSwitch": true,
  "enableChlorinatorFan": true,
  "enablePumpSpeedFan": true,
  "enableSaltSensor": true,
  "enableVspSlotTiles": false,
  "vspSlotMinPct": { "1": 35 },
  "circuitLabels": {
    "AUX_1": "Spa Light"
  }
}
```

### 7.2 HomeKit Accessories

| HomeKit type | Name | Enabled by |
|---|---|---|
| Switch | Spa | `enableSpaModeSwitch` — On=spa, Off=pool |
| Switch | Filter | `circuits` includes FILTER |
| Switch | Lights | `circuits` includes LIGHTS |
| Switch | Heater Auto | `circuits` includes HEATER_1 — armed/Auto-mode, tappable |
| Switch | Heater Running | registered with HEATER_1 — read-only relay-firing indicator (`heater_active`) |
| Switch | Aux 1 | `circuits` includes AUX_1 (this system: spa light) |
| Switch | Aux 2 | `circuits` includes AUX_2 |
| Switch | Super Chlorinate | `circuits` includes SUPER_CHLORINATE |
| Thermostat | Active Heat | `enableActiveHeaterThermostat` — mode-following |
| Thermostat | Pool Heat | `enablePoolHeaterThermostat` + pool in `activeBodies` |
| Thermostat | Spa Heat | `enableSpaHeaterThermostat` + spa in `activeBodies` |
| TemperatureSensor | Pool Temperature | `enableTemperatureSensors` |
| TemperatureSensor | Air Temperature | `enableTemperatureSensors` |
| AirQualitySensor | Salt Level | `enableSaltSensor` — VOCDensity = raw PPM, quality pinned to Excellent |
| Fan | Chlorinator | `enableChlorinatorFan` — spins when filter on AND chlorinator % > 0 |
| Fan | Filter Speed | `enablePumpSpeedFan` — live `pump_speed` from scroll; spins when filter on |
| Fan | Speed 1–4 | `enableVspSlotTiles` — spins when filter on AND that slot is active |
| Switch | Bridge Needs Rebooting | Always registered |

### 7.3 Circuit Label Overrides

Any circuit switch can be renamed in config without changing the sidecar or protocol layer:

```json
"circuitLabels": { "AUX_1": "Spa Light", "AUX_2": "Water Feature" }
```

Defaults: Pool, Spa, Filter, Lights, Spillover, Aux 1, Aux 2, Heater, Super Chlorinate.
Editable per-field in the Homebridge config UI.

### 7.4 HEATER_1 Switch Semantics — Two-Switch Model

When `circuits` includes HEATER_1, two tiles are registered instead of one:

**"Heater Auto"** (`SwitchAccessory`, tappable) — shows `pool_heater_enabled` or
`spa_heater_enabled` based on valve mode, i.e. the **armed** state (Auto vs Manual Off).
Falls back to the LED bit until the first menu read confirms the enabled state.
ON = armed, will heat when pump runs and temp is below setpoint. OFF = Manual Off.

**"Heater Running"** (`HeaterRunningAccessory`, read-only) — shows `heater_active`, i.e.
whether the relay is **firing right now**. Any user toggle snaps back to the real state.

Rationale: an earlier attempt used a single three-state Fanv2 (grayed/armed/spinning), but
Apple Home ignores `CurrentFanState` and spins any `Active=1` fan, so the "firing" state
could not be shown reliably. The two-switch split is unambiguous. The thermostat's
`CurrentHeatingCoolingState` also reflects active heating.

> **Implementation note:** `SwitchAccessory` evicts any stale `Fanv2` service left on the
> accessory by the abandoned three-state design before adding its `Switch` service.

### 7.5 Three-Thermostat Model

**Accessory A — "Active Heat"**: follows `valve_mode`; shows active body's temp/setpoint.
**Accessory B — "Pool Heat"**: always pool setpoint, regardless of mode.
**Accessory C — "Spa Heat"**: always spa setpoint.

`TargetHeatingCoolingState` pinned to Heat (1) so the temperature dial stays visible even
when not actively heating. Real enabled/disabled state conveyed by `CurrentHeatingCoolingState`
and dynamic tile name.

Setpoint range: 65–104°F. Display units: Fahrenheit.

Setpoint writes are **debounced 600ms** (`handleSetTarget` → `commitSetpoint`): dragging the
temperature dial fires `onSet` per step and each write is a menu navigation, so only the
final value is committed. `targetTempC` updates only on a confirmed write; a failed write
reverts the dial to the last known value. This matches the fan/VSP debounce and closes the
last drag-driven wedge vector.

### 7.6 Fan Accessories — Spinning Logic

`CurrentFanState` is set explicitly on every poll so HomeKit always shows the correct
animation rather than picking randomly:

| Tile | `CurrentFanState = BLOWING_AIR` when |
|---|---|
| Filter Speed | `circuits['FILTER'] == true` |
| Chlorinator | filter on **AND** `chlorinator_percent > 0` |
| Speed 1–4 | filter on **AND** `vsp_active_slot == this slot` |

`Active` is always 1 (tile stays visible even when not spinning).

**Writable tiles**: setting `RotationSpeed` on the Chlorinator tile writes the current
body's chlorinator output % (`/chlorinator/{pool|spa}`, chosen by valve mode). Setting the
Filter Speed tile writes VSP slot 4 and activates it. Both writes are debounced 600ms so a
slider drag commits once (each commit is a menu navigation), and the ring reverts to the
last known value on failure.

### 7.7 VSP Slot Tiles

When `enableVspSlotTiles: true`, four Fan tiles are registered (Speed 1–Speed 4). Each
shows that slot's configured speed % from `vsp_slot_pct`. Setting the speed writes the
new value to that slot and immediately activates it (FILTER off→on).

Robustness behaviors (added 2026-06-20):

- **Startup pre-fetch**: the plugin calls `GET /vsp/slots` once on launch and populates the
  tiles, so they show real values instead of a blank 0% before any interaction. (Previously
  `vsp_slot_pct` was null until a manual menu read.)
- **Debounced writes**: HomeKit fires `onSet` repeatedly while the user drags the speed
  ring. The accessory debounces 600 ms and commits only the final value, so a drag from
  90→40 is **one** menu navigation, not one per intermediate step.
- **Zero guard**: `onSet(0)` (sent when the user taps a tile without dragging) is a no-op
  that reverts to the current value — it must not write 0% and stop the pump.
- **Per-slot floor** (`vspSlotMinPct`): sets the `RotationSpeed` `minValue` so the slider
  can't target below the panel's hardware floor. Slot 1 defaults to 35%. The sidecar's
  `_step_to` independently stops once a value stalls against a floor/ceiling instead of
  burning the full press budget.

### 7.8 Salt Level Sensor

`SaltSensorAccessory` uses `AirQualitySensor` service, `AirQuality` pinned to Excellent
(no warning colours), `VOCDensity` showing raw PPM. `VOCDensity` defaults to a HAP `maxValue`
of **1000**, which silently clamped the ~3200 PPM reading; `setProps({ minValue: 0,
maxValue: 4000 })` raises it with headroom for typical saltwater levels (2700–3500 PPM).
Enabled by `enableSaltSensor` (default true).

> HomeKit limitations (confirmed, not fixable on the Air Quality service): the unit renders
> as µg/m³ — there is no "ppm" unit for any standard HomeKit sensor — and the mandatory
> `AirQuality` characteristic always shows a qualitative label ("Excellent"), which cannot
> be hidden. A Light Sensor (lux) would drop the label but still not show "ppm"; kept on
> Air Quality by preference.

### 7.9 BridgeHealthAccessory

Switch tile: Off = healthy, On = wedged. Tapping runs a live canary probe and snaps tile
to true result. Updated passively on every poll.

### 7.10 Polling

On each poll cycle:
1. Valve mode → Spa Mode switch
2. Circuit switches (HEATER_1 "Heater Auto" uses enabled state; all others use LED bit)
3. "Heater Running" switch ← `heater_active`
4. Thermostat state → all three thermostat accessories
5. Pool + air temp sensors
6. Chlorinator fan speed + running state (filter on AND % > 0)
7. Filter Speed fan speed + running state (filter on AND `pump_speed` > 0)
8. VSP slot tiles speed + running state (filter on AND slot matches)
9. Salt level sensor
10. Bridge health wedge state

### 7.11 Backend Reconciliation

On `didFinishLaunching`, plugin checks active sidecar backend and switches if it differs
from config. Sidecar restarts via systemd; plugin tolerates a few failed polls during restart.

---

## 8. File Structure

```
homebridge-prologic/
├── package.json                    (version 0.1.0)
├── config.schema.json
├── CLAUDE.md                       ← deploy instructions, response style
├── docs/
│   ├── plugin-spec.md              ← this file
│   ├── aquaconnect-screen-refresh-handoff.md   ← research handoff (archived)
│   └── aquaconnect-screen-refresh-findings.md  ← Update Local Server& discovery
├── sidecar/
│   ├── pool_service.py             ← dual-backend sidecar + Flask REST API
│   ├── requirements.txt
│   └── install.sh
└── src/
    ├── index.ts
    ├── platform.ts                 ← accessory registration, reconcileBackend, poll loop
    ├── switchAccessory.ts          ← generic circuit switch (SUPER_CHLORINATE special-cased)
    ├── spaModeAccessory.ts         ← Spa Mode switch (On=spa, Off=pool)
    ├── thermostatAccessory.ts      ← three-body thermostat model
    ├── heaterRunningAccessory.ts   ← read-only "Heater Running" relay-firing switch
    ├── temperatureAccessory.ts     ← read-only temperature sensor
    ├── fanAccessory.ts             ← chlorinator % / filter speed fan tiles + CurrentFanState
    ├── vspSlotAccessory.ts         ← VSP slot 1–4 fan tiles
    ├── saltSensorAccessory.ts      ← AirQualitySensor/VOCDensity for salt PPM
    ├── bridgeHealthAccessory.ts    ← wedge indicator + live test button
    ├── sidecarClient.ts            ← HTTP client for sidecar REST API
    └── settings.ts                 ← constants, types, PoolStatus interface
```

---

## 9. Confirmed Hardware Observations

| Field | Observed value |
|---|---|
| Pool water temp | 76–79°F |
| Air temp | 66–75°F |
| Salt level | 3100–3200 PPM |
| Chlorinator | 50% (pool mode) |
| Filter speed | 50–80% depending on active VSP slot |
| Pool heater | Enabled (Auto mode), setpoint ~70°F |
| Spa heater | Not yet confirmed |
| AUX_1 | Spa light |
| AUX_2 | Inert — safe for wedge canary probe |
| Super Chlorinate | Verified toggle: PLUS=Off→On, MINUS=On→Off |
| AquaConnect scroll rate | One frame rotation ~10–30s |
| AquaConnect LED nibbles | 3=absent, 4=off, 5=on, 6=blink |
| Slot-selection window frame | `Filter On:SpdN` / `+/- to change` |
| Settings menu Super Chlorinate frame | `Super Chlorinate <span class="WBON">Off/On</span>` |
| Fault observed | "Check System / Inspect Cell" — T-Cell-15 salt cell comm fault |

---

## 10. Known Limitations and Future Work

### 10.0 Recent changes (week of 2026-06-29)

- **Cockpit moves to interactive (stage-then-Apply).** The web cockpit is no longer read-only. Controls stage locally and **nothing is sent until Apply** (single busy-lock mirroring the panel's one-lane nav). Tap-to-edit ± steppers (the +/- only appear once you tap a controllable item), a lockable manual-nav D-pad, and a single Apply/Cancel bar. The in-flight progress now shows **in the top header** instead of a sticky banner.
- **Cockpit layout.** Single "Water" temp tagged with the live mode (°F shown inline with the number); Heat card shows **both setpoints with the active body flagged**; Chlorinator likewise; Speeds merged into the Water card as **"Pump Speed"** with the running slot highlighted; Pool/Spa speed slots share one labeled row with the Spa tile sized like the Pool slots.
- **Heater switch-only model.** Auto/Off is only ever changed via the heater switch (`_enable_heater` / `_restore_heater_off`); reads are pure (never scroll when "Manual Off"). A 45s **setpoint-backfill** thread fills a missing °F when a heater is enabled. Fixes "heater active but no target temp" and HomeKit changes not reflecting in the cockpit.
- **Both heater targets read per explicit action.** An explicit heater enable/setpoint write already navigates the menu, so it now also passively reads the *other* body's heater item in the same trip (`_read_other_heater`, one adjacent step Spa↔Pool) — capturing that body's °F if it's enabled, without ever toggling an off heater. So enabling pool heat also records the spa target when the spa heater is on (an off heater still shows no temperature, so it keeps its last-known value).
- **Active VSP slot parsing.** `vsp_active_slot` is now read from **both** panel formats — the idle scroll `Filter Speed 50% Speed2` and the startup window `Filter On:Spd2 +/- to change`. The cockpit highlight is decoupled from the FILTER circuit flag (spa runs on its own pump line).
- **AquaConnect LCD tag stripping.** The box wraps highlighted/flashing values in HTML (`<span class="WBON">..</span>`); the parser now strips tags so raw markup no longer leaks onto the panel display, and the scroll-pattern regexes match cleanly.
- **Temperature history chart.** The sidecar records a rolling pool/spa/air temp series (`_temp_history_thread`, sampled every 5 min, capped 48h, persisted to `sidecar/temp_history.json`, restored on startup) served at `GET /history?hours=N`. Tapping **Water** or **Air** in the cockpit opens an interactive canvas chart (Pool/Spa/Air lines, 6h/24h/48h ranges, drag to read values at a point).
- **Active scroll sweep at startup.** The idle status scroll (temps, salt, chlorinator, pump speed, heater Auto/Off, active slot) can be advanced with RIGHT at normal key timing instead of waiting ~6s per item for the natural cycle — verified 2026-06-30: 6 frames in 7.3s (~1.2s/item). `sweep_scroll()` runs first in the startup pre-fetch and on the cockpit Refresh button (`/prefetch`), then `read_all_settings()` handles the menu-only values. Test endpoint: `POST /debug/scroll-sweep`.
- **Bus-state persistence across restarts.** Everything read off the bus (setpoints, heater enabled, chlorinator %, VSP slot speeds, spa speed, temps, salt, valve mode, circuits) is cached to `sidecar/state_cache.json` (atomic write, flushed on change every 30s) and **restored into `state` at startup before the prefetch runs** — so the cockpit/HomeKit show last-known values immediately instead of "—" until the menu sweep finishes. Foundation for making the startup sweep conditional later (only re-read what differs). See backlog.
- **Single-pass startup pre-fetch.** `read_all_settings()` sweeps the Settings ring in one menu session (RIGHT through the heaters/chlorinators, LEFT back to VSP) instead of re-entering the menu per value; triggers on every **sidecar** restart. Key timeout lowered 4s→3s with overshoot "two keys → back up" recovery (`_press_back`).
- **Remote/LAN access architecture (decided).** See §10.0a. Chose **stock Caddy (apt) + HTTP Basic auth on the LAN**, sidecar kept localhost-bound; **Tailscale** retained for remote.
- **Wedge probe scaled back.** The proactive AUX2 canary (a real write to the box) went from every **5 min → 30 min** to cut self-inflicted command-path load; it's now configurable via `backend.json` `wedge_probe_interval_s` or `POST /wedge-probe` (`0` = reactive-only). The reactive on-failure probe and 30s re-probe-while-wedged are unaffected. Diagnostic: `deploy/wedge-report.sh` summarizes sensor-firing wedge episodes + recovery durations. See §10.0c for the tuning commands.
- **Wedge auto-re-arm (3 retries).** If the box is still wedged after the power-cycle cooldown, the sidecar cycles the "Bridge Needs Rebooting" sensor Off→On (12s pulse, spans the 5s poll) to re-fire the edge-triggered HomeKit power-cycle automation — up to `_WEDGE_MAX_REARMS` (3) times, restarting the cooldown each attempt. After 3 failed attempts it logs an ERROR (surfaces in the cockpit alerts) and leaves commands blocked for manual intervention. Retry budget resets on recovery. Also: all sidecar WARNING/ERROR logs now surface in a dismissible cockpit **alerts banner** (`/status['alerts']`, `/alerts`), and heater setpoint writes store the panel-**confirmed** value (not the requested target) and flag/probe when a write doesn't land.
- **Canary-probe wedge now engages the power-cycle cooldown.** The active-canary-probe wedge path also sets `wedge_detected_at` now, so a canary-detected wedge triggers the 120s `_WEDGE_POWERCYCLE_COOLDOWN_S` (blocks commands, defers the recovery probe to the reboot window) instead of re-probing every 30s and racing the auto power-cycle plug — matching the "2 unconfirmed writes" path.
- **Menu-nav failure now trips the wedge probe.** A failed `read_all_settings()` ("Could not reach Settings Menu") — manual `/prefetch` or startup pre-fetch — now fires `_immediate_wedge_probe()`, so a wedged box gets flagged (sensor/plug) within seconds instead of sitting wedged until the next 30-min canary. Closes the detection gap the longer proactive interval opened (a real wedge sat undetected for ~an hour on 2026-06-30).
- **Cockpit "Lights" / switches driven by Homebridge config.** A Lights card (placed right after "At a glance") renders a staged On/Off toggle per controllable circuit (LIGHTS/AUX_1/AUX_2/SPILLOVER). The plugin POSTs its enabled `circuits` + `circuitLabels` to the sidecar (`/config/ui`); the sidecar surfaces them in `/status` (`ui_circuits`, `circuit_labels`); the cockpit shows exactly those with override labels (falls back to panel-reported circuits if the plugin hasn't pushed yet).
- **Cockpit "At a glance" card.** Conditions renamed; temps (Water/Air/Salt) on one row with units (`°F`/`ppm`) rendered small and inline; a divided list below for Filter (first), Heater mode, and Calling-for-heat with larger pills.
- **Cockpit polish.** Apply bar is now a **fixed floating bottom bar** (overlays content, never reflows the page); highlight colors unified (**green = live/active**, **cyan = staged/selected**); pill toggles enlarged for touch.
- **`circuitLabels` rename fix.** Renaming a circuit (e.g. Spa → "Spa Mode") was silently ignored for cached accessories (UUID is stable; displayName was only set at creation). The plugin now updates `displayName` + `updatePlatformAccessories` when the configured label changes. (Note: a name set in the Apple Home app still wins over the plugin's.)
- **RS-485 key-event frame type — finding + harness.** The `aqualogic` library transmits **LOCAL_WIRED** (`00 02`) for keys ≤ 0xffff and **WIRELESS** (`00 83`) for keys > 0xffff (incl. `HEATER_1`); it **never** transmits **REMOTE_WIRED** (`00 03`). Untested whether a bus-attached remote needs REMOTE_WIRED. `POST /debug/rawkey` now supports `local|remote|wireless` to sweep this on direct serial. See automation-spec §0.
- **Caddy password helper.** `deploy/set-cockpit-password.sh` sets the Basic-auth credential in one sudo step (hidden prompt → hash → update Caddyfile → validate → reload → verify). Kept out of the Homebridge UI on purpose (no sudoers/escalation path for the plugin).

### 10.0a Access architecture

The sidecar (cockpit + control API) binds to `127.0.0.1:5757` and is **never**
exposed to the network directly — the bespoke Flask app stays off the LAN to
avoid turning the Pi into an attack/pivot surface. Two front-ends sit in front:

| Path | Front-end | Auth | Notes |
|---|---|---|---|
| **Home / LAN** | **Caddy** (stock, official apt repo) reverse-proxying `127.0.0.1:5757` | HTTP Basic (bcrypt) | Hardened, apt-maintained binary is the only LAN listener. Config: `deploy/Caddyfile` + `deploy/CADDY.md`. Basic auth has no session, so an iOS pinned web app re-prompts on cold launches |
| **Remote** | **Tailscale** (`tailscale serve`) | WireGuard device identity | No inbound port; reachable only from the tailnet |

Rationale (evaluated 2026-06-29): opening the sidecar's own port was rejected as
the only option that grows the Pi's inbound surface. Cloudflare Tunnel + Access
(outbound-only, revocable service tokens, apt `cloudflared`) was the runner-up
and remains the upgrade path if persistent no-login access or remote-without-VPN
is wanted; caddy-security (self-hosted persistent sessions) was rejected for its
out-of-apt binary lifecycle and single-maintainer plugin. Revocable per-key auth
was deemed over-engineering for a single user; "rotate the one password" suffices.

### 10.0c Operational reference — wedge probe tuning

The proactive AUX2 canary is a real write to the AquaConnect box, so its cadence
trades wedge-detection latency against self-inflicted command-path load. Default
is **1800s (30 min)**. `0` = **reactive-only** (no idle probing; a wedge is then
caught when a real command fails to confirm). The reactive on-failure probe and
the 30s re-probe-while-wedged always run regardless of this setting, so the
HomeKit "Bridge Needs Rebooting" sensor + auto power-cycle plug keep working.

```bash
# Inspect the current setting
curl -s http://127.0.0.1:5757/wedge-probe

# Set to 30 min and persist across restarts
curl -s -X POST http://127.0.0.1:5757/wedge-probe \
  -H 'Content-Type: application/json' -d '{"interval_s": 1800, "persist": true}'

# Go fully reactive-only (no idle probing) and persist
curl -s -X POST http://127.0.0.1:5757/wedge-probe \
  -H 'Content-Type: application/json' -d '{"interval_s": 0, "persist": true}'
```

Persisted value lives in `backend.json` as `wedge_probe_interval_s` and overrides
the code default at startup. Review wedge history any time with:

```bash
./deploy/wedge-report.sh                 # all history
./deploy/wedge-report.sh "2026-06-30"    # since a date
```

It pairs each sensor-firing wedge with its recovery, tags recoveries faster than
a reboot (~60s) as **self-healed** vs **possibly the power-cycle**, and counts
per day. (Wedge paths: a canary-probe wedge sets `bridge_wedged` but historically
did **not** set `wedge_detected_at`, so the 120s power-cycle cooldown only
engaged on the "2 unconfirmed writes" path — see backlog.)

### 10.0b Recent changes (week of 2026-06-28)

- **Critical deadlock fixes.** `/status` self-deadlocked on the non-reentrant `state_lock` (it called `_wedge_cooling_down()` while already holding the lock) — this took the **whole plugin offline** (all tiles unresponsive). Separately, `_ac_heater_enable` self-deadlocked on `_nav_lock` via a nested acquire on the heater enable/setpoint-read path. Both fixed and are the most important changes of the week.
- **Spa support.** Added spa chlorinator % (`spa_chlorinator_percent`) and a **Spa Speed** VSP tile; the chlorinator fan is now **valve-mode aware** (shows pool % in pool mode, spa % in spa mode). Both bodies' chlorinator % and heater setpoints are pre-fetched on startup.
- **Speed slider fix.** A non-zero `RotationSpeed` `minValue` made the Home app render speed sliders as **0–65% instead of 35–100%**. All speed fans now use `minValue: 0` (honest 0–100%) and **snap up to the floor on commit** instead. The per-slot floor still applies, just enforced in software rather than via `minValue`.
- **Heater restore hardening.** A setpoint read briefly enables the heater to reveal the stored °F; the restore-to-Manual-Off now **verifies and retries** (`_restore_heater_off`) so a read can never leave the heater on (it did once, on the lossy RS-485 path).
- **Read-only web cockpit (Screen 1).** Self-contained SPA served by the sidecar at `/` and `/ui`, consuming `/status` + `/stream` (SSE) + `/display`. **No control endpoints** — cannot touch the panel. Designed to be exposed via a Tailscale `tailscale serve` proxy so the sidecar stays localhost-bound.
- **Log hardening.** The debug-log handler is now best-effort with a per-uid fallback; a non-writable `/tmp/pool_sidecar_debug.log` (stale owner from a prior run) **no longer crashes sidecar startup**.
- **Wedge power-cycle cooldown.** 2-minute command block after wedge detection, then an immediate recovery probe — pairs with a HomeKit smart-plug auto-power-cycle automation.

### 10.1 Completed

| Item | Status | Notes |
|---|---|---|
| VSP slot tiles | Done | `enableVspSlotTiles`; generalized sidecar nav for slots 1–4 |
| VSP slot tile robustness | Done | Pre-fetch `/vsp/slots` on startup so tiles show real values; 600ms debounce on the speed slider (one menu walk per drag, not per intermediate value); `onSet(0)` treated as no-op so a tap-without-drag can't set the slot to 0% and stop the pump |
| VSP slot floor guard | Done | Per-slot `vspSlotMinPct` (slot 1 defaults to 35%) sets the HomeKit slider's `minValue` so it can't target a speed the panel clamps; sidecar `_step_to` also stops stepping once a value stalls at a hardware floor/ceiling instead of burning the full press budget |
| Pump tile live speed | Done | Reads `pump_speed` (scroll); labeled "Filter Speed" |
| LIGHTS / AUX_1 write | Verified | Tested on hardware; keycodes and LED confirmation correct |
| Super Chlorinate | Done | Settings menu nav; add to `circuits` config to expose |
| Salt level sensor | Done | `enableSaltSensor`; AirQualitySensor/VOCDensity, `maxValue` raised to 4000 PPM (HAP default of 1000 was clamping the ~3200 reading) |
| Fan spinning | Done | `CurrentFanState` set explicitly per tile on each poll; `Active=1` re-pushed every poll so HomeKit's reconnect reset doesn't stop the animation |
| Heater two-switch model | Done | "Heater Auto" (armed/Auto-mode, tappable) + "Heater Running" (read-only relay-firing indicator from `led['heater']`). Replaced the abandoned three-state Fanv2 — Apple Home ignores `CurrentFanState` and spins any `Active=1` fan |
| Circuit label overrides | Done | `circuitLabels` config object, editable in Homebridge UI |
| Nav timing optimization | Done | `_AC_MIN_GAP_S` 0.9→0.6s. Floor is ~0.5s; below that the box wedges. Combined with the frame-reader (§5.3) for faster, lower-request-count menu reads |
| Frame-reader architecture | Done | Single shared poll-loop reader + `_frame_cond`/`_read_wake`; `send_nav_key` waits for one confirming frame instead of a 4× burst (N+1 requests vs N×5). Replaced the confirm-burst/adaptive-read design |
| Chlorinator % HomeKit write | Done | Fan tile `RotationSpeed` writes `/chlorinator/{which}`; valve-mode aware (pool vs spa); 600ms debounce so a drag is one menu write, not one per step; reverts the ring on failure. Sidecar snaps to the panel grid (1% below 10%, 5% at/above) |
| Wedge-risk audit of write paths | Done | Every slider-driven `onSet` that triggers menu navigation is now debounced 600ms: fan (chlorinator/pump), VSP slot tiles, **and the thermostat setpoint** (was the last unguarded drag→nav path). Discrete toggles (circuit switches, spa mode, heater enable) are single-press and need no debounce |
| `/debug/nav-sweep` harness | Done | Server-side timing sweep over `min_gaps` (and optionally `nav_gaps`/`post_menu_settles`), aborts early + returns partial results on a wedge, ranks clean runs fastest-first |

### 10.2 Backlog (open)

| Item | Priority | Notes |
|---|---|---|
| **Code-review follow-ups (in progress, paused 2026-06-30)** | High | Full 3-way code review done (sidecar/plugin/cockpit). **DONE:** HTML-escaping of fault/label innerHTML; FILTER-off-during-heater-cooldown cry-wolf guard; #1 removed pump/VSP speeds from HomeKit (plugin side — commit `366d259`). **STILL TODO** below. |
| ↳ Sidecar dead-code cleanup | Med | Now that HomeKit VSP tiles are gone, these are orphaned in `pool_service.py`: `activate_vsp_slot` + the `/vsp/slot4*` compat routes + `read_vsp_slot4`/`set_vsp_slot4`/`activate_vsp_slot4` aliases (the FILTER-cycling activation path — no caller now); plus `keypad_press` (no `@app.route`, unreachable), `_wait_key_sent` (unused), `_AC_SETTLE_S` (unused const). Rename module-level `_pct` → `_percentile` (collides with `MenuNavigator._pct`). KEEP `/vsp/slot/<n>` set + `/vsp/spa` (cockpit uses them). |
| ↳ Spec fixes | Med | (#3) Document spa mode = the **SPA circuit switch** (renameable via `circuitLabels`); remove stale `spaModeAccessory.ts` / `enableSpaModeSwitch` references — the review wrongly thought spa mode was gone; it works via the renamed SPA switch. (#4) Update thermostat section to match live behavior (`TargetHeatingCoolingState` driven from `heater_enabled`, not "pinned to Heat"). Fix `HEATER_1` keycode `0D`→`13`. automation-spec §15.4 (3s settle + `KeyId=00` reread) is stale (frame-reader replaced it). |
| ↳ Heater switch ⇄ thermostat sync (#4) | Med | The "Heater Auto" switch and the Pool/Spa Heat thermostats' off/heat should stay in sync — both map to `heater_enabled`; currently up to 3 tiles drive `HEATER_1` with independent optimistic state that only reconciles on poll. |
| ↳ Cockpit dead code | Low | Remove unused `num()` helper and the never-applied `.pill.ctl` CSS rule (overridden by `.card .condlist .pill`). |
| ↳ Optional hardening | Low | Pin `aqualogic` version in requirements + make `_install_key_burst` source-string monkeypatch fail loudly if upstream changes (currently degrades to silent write-blindness). Hoist magic numbers (600ms debounce, 35% floor, timers) to named constants. Body-aware heater enable. Normalize `chlorinator_percent` int/float. |
| **Dedicated LCD frame-watcher** | Done (in-process) | Exists: `_poll_loop` continuously reads the LCD (single shared reader), `_frame_cond` is the in-process pub/sub, and external consumers already get change-notification + last-known-per-field via `/stream` (SSE), `/status`, `/display`, `/display/history`. The originally-scoped *separate standalone process* offers no functional gain — the sidecar is already the always-on watcher and exposes the pub/sub API — so it's not planned unless a concrete external need appears. Cannot speed navigation (0.6s/request box limit) |
| FILTER circuit as Fanv2 | Won't do | Redundant: FILTER *is* the pump on this system, and on/off (Filter pill / circuit switch) + speed (VSP slot tiles) already exist. A single Fanv2 0–100% slider also misrepresents the panel's slot-based speed model (4 discrete presets, one active). Dropped 2026-06-30 |
| Conditional startup sweep | Done | The state cache stamps `_saved_at` on each flush (with a ~60s heartbeat so it reflects "sidecar alive"); on startup, if the cache is <`_STARTUP_SKIP_SWEEP_S` (3 min) old the menu sweep is skipped entirely (values can't have gone stale). Complemented by passive menu-value capture below, so panel-side changes land without a sweep |
| Passive menu-value capture | Done | Menu-only values (heater setpoint °F `Pool/Spa Heater1 85°F`, VSP slot speeds `Filter Speed1 90%`) are now parsed from any LCD frame — so when the **owner changes them by hand at the panel**, the physical display shows the menu, our poll reads it, and state updates with no active nav. (Chlorinator % and spa speed were already caught via scroll patterns.) Heater setpoints and slot speeds were the only blind spots; now closed |
| Canary-probe wedge skips power-cycle cooldown | Done | Fixed 2026-06-30: the active-canary-probe wedge path now also sets `wedge_detected_at`, so the 120s `_WEDGE_POWERCYCLE_COOLDOWN_S` engages (matching the "2 unconfirmed writes" path). The sidecar now blocks commands + defers probing for the reboot window instead of re-probing every 30s and racing the auto power-cycle plug |
| Pool active-slot highlight | Done | Cockpit highlights the running pool speed from `vsp_active_slot`. Parsed from two confirmed panel formats: the idle scroll line `Filter Speed 50% Speed2` and the brief startup slot-selection window `Filter On:Spd2 +/- to change` (the WBON `<span>` is stripped first). Right after a restart the field is `None` until one of those scrolls past |
| RS-485 backend: reads | Done | Verified on three TCP bridges; live state decodes cleanly (observer-confirmed) |
| RS-485 backend: writes | Blocked on hardware | **Writes do NOT work over a TCP serial bridge** — the bridge's network latency misses the panel's keypress-response window (see automation-spec §0). Reliable writes need a **direct serial** connection (isolated USB-RS485 on the Pi). The `--serial-device` sidecar option for this is not yet implemented |
| Spillover mode | Not tested | Not present on this installation |
| Valve mode detection lag | ~10–30s | Scroll-dependent; no event-driven update (would benefit from the frame-watcher) |
| System fault indicator | Done (cockpit) | Fault/alert phrases the panel interleaves into the status scroll (`_FAULT_PHRASES`: Check System, Inspect Cell, No/Check Flow, Low/High Salt, Check PCB, etc.) are tracked with a last-seen time (`_active_faults`, `_FAULT_TTL_S` = 5 min = "still scrolling = still active") and surfaced in `/status['faults']`. Cockpit shows a red banner at the very top listing active faults. **Not** surfaced to HomeKit (owner preference) |
| Fault-phrase discovery log | Ongoing | Alert-looking frames (`_FAULT_HINT_RE`) that aren't a known reading or known fault are logged (`FAULT-CANDIDATE`) and persisted to `fault_candidates.json`; view via `GET /faults/candidates`, reset with `POST {"clear":true}`. **Periodically pull this to find real alert wording on the panel and promote it into `_FAULT_PHRASES`.** Grep the journal: `journalctl -u pool-sidecar \| grep FAULT-CANDIDATE` |
| Spa heater setpoint | Done | Spa heater enable + setpoint work on AquaConnect; both bodies' setpoints pre-fetched on startup so the thermostats show real values |
| `/debug/aquaconnect` GET uses `_post('00')` | Done | Verified 2026-06-30: the GET already uses `_read()` (`'Update Local Server&'`), a pure read with no phantom keypress. No `_post('00')` read paths remain (only a docstring reference and an intentional RS-485 wedge-test keypress) |
