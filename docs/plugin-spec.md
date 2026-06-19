# Homebridge ProLogic Plugin — Authoritative Specification

> **Version**: 3.0 — full rewrite reflecting dual-backend model, AquaConnect polling fix,
> frame reader architecture, wedge detection, and current HomeKit accessory set
> **Updated**: 2026-06-19
> **Status**: Implemented and running on hardware (v0.1.0)

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
This separation avoids Python↔Node.js process lifetime coupling and keeps protocol handling
in Python.

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
It was discovered by inspecting the firmware's JavaScript (`WebsFuncs.js`, `setInterval`
calling `ReqWebsData()`). The response format is identical to a `KeyId=` POST.

This distinction is not documented by Hayward. The sidecar enforces it via two separate
methods:

```python
def _read(self) -> Optional[str]:
    return self._request('Update Local Server&')   # pure read, no event

def _post(self, key_code: str) -> Optional[str]:
    return self._request(f'KeyId={key_code}&')     # keypress event
```

### 3.2 Why `KeyId=00&` Was Causing the Wedge

Previous versions of the sidecar polled with `KeyId=00&` as a "no-op read." This was wrong:
`KeyId=00` is processed by `WebsProcessKey()` like any other key, injecting a phantom
keypad event into the firmware's event queue. At a 3-second poll interval, this produced
~29,000 phantom events per day. The firmware's event queue was never designed for this volume;
after several hours it would enter a stuck state where:

- HTTP requests continued returning `200 OK`
- Read responses (`Update Local Server&`) continued working
- Keypress events (`KeyId=NN&`) were silently dropped (no RS-485 relay)
- Only a power-cycle restored command function

Switching the poll to `Update Local Server&` eliminates phantom event injection entirely.

### 3.3 Request Format

The GoAhead server is picky about headers. Extra headers (`Accept-Encoding`, `Connection`,
Python user-agent strings) cause it to silently ignore the request. The sidecar hand-builds
the raw HTTP request over a plain socket with a minimal header set:

```
POST /WNewSt.htm HTTP/1.1\r\n
Host: {host}\r\n
Content-Type: application/x-www-form-urlencoded\r\n
Content-Length: {len}\r\n
\r\n
{body}
```

Requests are not pipelined; each opens a fresh connection. A minimum gap of ~0.9 seconds
between requests is enforced (`_last_request_time` + `_AC_MIN_GAP_S = 0.9`).

### 3.4 Response Format

The response body contains an HTML-ish block. Meaningful data lives inside `<body>…</body>`,
CRLF-separated, each line terminated with the literal string `xxx`. Example:

```
Thursday
5:47P
TECD4C333333
```

- **Lines 1–N-1**: LCD text (panel screen content; rotates through Pool Temp / Air Temp /
  Salt Level / Chlorinator % / etc.)
- **Last line before closing tags**: 12-character equipment-state field (e.g. `TECD4C333333`)
  — each LED is one 4-bit nibble: `3`=absent, `4`=off, `5`=on, `6`=blink

The sidecar's `_apply()` method parses this response and updates shared `PoolState`.

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

### 3.6 Settings Menu (AquaConnect)

The AquaConnect navigates the same physical panel Settings menu as the RS-485 backend,
but via HTTP keypresses instead of RS-485 frames. The same menu ring applies (§4.3).
After any keypress that changes state (heater enable/setpoint, etc.), the frame reader
delivers the confirmation screen within one poll cycle (~0.9s gap).

---

## 4. RS-485 Backend

### 4.1 Hardware Connection

- **Connector**: J2 or J4 on the AquaPlus main PCB (parallel, either works)
- **PCB label**: "Remote DSP comm (RS485 – 10VDC)"

| J2/J4 pin | Label | Wire color | USR-W610 terminal |
|---|---|---|---|
| Pin 2 | DATA+ | black | A+ |
| Pin 3 | DATA− | yellow | B− |
| Pin 4 | GND | green | GND |

**⚠ Wiring notes:**
- Pin 1 is not a data line. Wiring A/B to pin 1 produces garbage (~8.5 B/s noise).
- If frames arrive but CRC errors persist, swap A and B at the bridge terminals.
- The USR-W610 is USB-powered. Do not draw panel power.

### 4.2 USR-W610 Configuration

| Setting | Value |
|---|---|
| Mode | STA (joins existing WiFi) |
| Network protocol | TCP Server |
| Local port | 8899 |
| Baud rate | 19200 |
| Data bits | 8 |
| Parity | None |
| Stop bits | **2** (8N2) ← critical |
| Transparent mode | Enabled |
| 485 Switch interval | 100 µs |

### 4.3 RS-485 Frame Format

```
DLE(0x10) STX(0x02) [command] [data] [cksum_hi] [cksum_lo] DLE(0x10) ETX(0x03)
```

Do not re-implement framing — use the `aqualogic` Python library.

### 4.4 What the `aqualogic` Library Provides from Broadcasts

These values are decoded from the continuous RS-485 status broadcast:

- Pool water temp, air temp, salt level, chlorinator output %
- Pump speed / mode (read-only from broadcast)
- State of every circuit: POOL, SPA, FILTER, LIGHTS, AUX1, AUX2, SPILLOVER, HEATER1

**NOT available from broadcasts** (require menu navigation):
- Heater setpoints (pool °F target, spa °F target)
- Heater enable/disable state (Auto vs Manual Off)
- Chlorinator % writes
- VSP slot speed % values and active slot selection

### 4.5 Settings Menu Ring (RS-485, verified on hardware)

```
Settings Menu → Spa Heater1 → Pool Heater1 → VSP Speed Settings → Super Chlorinate →
Spa Chlorinator → Pool Chlorinator → Configuration Menu-Locked → (wraps)
```

RIGHT advances, LEFT retreats. PLUS/MINUS adjust values.

---

## 5. Sidecar Architecture (`sidecar/pool_service.py`)

### 5.1 Backend Selectability

The active backend is stored in `backend.json` adjacent to the script:

```json
{"backend": "aquaconnect", "aquaconnect_host": "192.168.50.100"}
```

`POST /backend` persists the new config and calls `os._exit(0)`; systemd restarts the
sidecar into the new backend. The Homebridge plugin's `reconcileBackend()` checks
`GET /backend` on launch and switches if the active backend doesn't match config.

### 5.2 Shared State

```python
@dataclass
class PoolState:
    circuits: dict            # {circuit_name: bool}
    pool_temp: float | None
    air_temp: float | None
    spa_temp: float | None
    salt_level: float | None
    chlorinator_percent: float | None
    vsp_slot4_pct: int | None
    pool_setpoint_f: int | None   # null until menu read
    spa_setpoint_f: int | None    # null until menu read
    pool_heater_enabled: bool | None  # True=Auto, False=Manual Off
    spa_heater_enabled: bool | None
    valve_mode: str | None        # 'pool' | 'spa'
    bridge_wedged: bool           # AquaConnect command path stuck
    connected: bool
    last_update: float
```

### 5.3 AquaConnect Frame Reader Architecture

The AquaConnect backend runs a single background `_poll_loop` thread that calls
`_read()` (`Update Local Server&`) continuously. This is the **only** thread that
ever reads from the box during idle operation.

Two threading primitives coordinate keypress confirmation:

- **`_frame_cond`** (`threading.Condition`): notified by `_poll_loop` after each
  successful frame parse. Writers wait on this for confirmation.
- **`_read_wake`** (`threading.Event`): set by `send_nav_key()` after each keypress.
  Wakes `_poll_loop` early to fetch the confirmation frame without waiting for the
  full poll interval.

**`_poll_loop`:**
```python
def _poll_loop(self) -> None:
    while not self._poll_stop.is_set():
        with self._http_lock:
            body = self._read()
        if body:
            self._apply(body)
            with self._frame_cond:
                self._frame_cond.notify_all()
        self._read_wake.wait(timeout=self._poll_s)
        self._read_wake.clear()
```

**`send_nav_key()` (one keypress):**
```python
def send_nav_key(self, key_name: str) -> None:
    code = _AC_KEY_CODES[key_name.upper()]
    with self._http_lock:
        self._apply(self._post(code))
    self._read_wake.set()          # wake poll loop early
    with self._frame_cond:
        self._frame_cond.wait(timeout=3.0)   # wait for confirmation frame
```

This replaces the previous pattern of 4 explicit "confirm burst" reads per keypress
(5 requests, ~1.6s blocked). Now each keypress costs 2 requests (~0.9s): the keypress
itself plus one subsequent `_read()` from the poll loop.

### 5.4 `_PriorityLock`

Wraps `threading.Lock`, tracking queued waiters. `busy()` returns `True` when the lock
is held OR has queued waiters. Background loops call `busy()` to defer non-urgent work
when a navigation sequence is in progress.

### 5.5 Heater Enabled vs Heater Active

Two distinct concepts, both tracked in `PoolState`:

| Field | What it means | Source |
|---|---|---|
| `pool_heater_enabled` | Heater is in Auto mode (armed) | Menu navigation / scroll parsing |
| `spa_heater_enabled` | Heater is in Auto mode (armed) | Menu navigation / scroll parsing |
| `circuits['HEATER_1']` | Heater is actively calling for heat (LED lit) | LED field nibble |

The **HEATER_1 switch** in HomeKit shows `pool_heater_enabled` or `spa_heater_enabled`
depending on the current valve mode. This is the correct field: the switch should be on
when the heater is armed (Auto), off when disarmed (Manual Off), regardless of whether
it is currently calling for heat.

The **thermostat** `CurrentHeatingCoolingState` uses `circuits['HEATER_1']` to indicate
active heating. `TargetHeatingCoolingState` reflects `pool/spa_heater_enabled`.

### 5.6 Bridge Wedge Detection

The AquaConnect command path can enter a stuck state where reads return normally but
keypresses are silently dropped (see §3.2). Two detection paths:

**Passive** (`_record_command_failure`): called when a write (keypress response) looks
wrong. Increments a failure counter; after `_WEDGE_FAIL_THRESHOLD=2` failures, fires an
immediate active probe via a daemon thread.

**Active** (`_ac_canary_probe`): sends a keypress on a known-inert circuit (AUX2 on this
system), then checks whether the AUX2 nibble in the LED field actually flips. Compares
only the specific nibble position, not the whole LED string, to avoid false recovery
detection during normal state changes. Sets `state.bridge_wedged = True` immediately on
failure (no debounce).

**Probe scheduling**: healthy state → probe every 300s. Wedged state → probe every 30s for
fast recovery detection. On recovery, `bridge_wedged` clears automatically.

**Manual probe**: `GET /bridge/health?probe=1` runs `_ac_canary_probe()` synchronously and
returns the result. Used by the `BridgeHealthAccessory` tile.

### 5.7 Debug Logging

All POST/read requests are traced to `/tmp/pool_sidecar_debug.log` with body prefix,
response length, timing, and any errors. The file rotates hourly with 1 backup kept
(`TimedRotatingFileHandler`).

`GET /debug/log` downloads the current log; `?all=1` includes the previous hour's file.

### 5.8 RS-485 Backend Menu Navigation

The RS-485 backend navigates the physical panel Settings menu via RS-485 key commands.
A single `_nav_lock` serializes all navigation. `LcdCapture` intercepts every LCD frame
(fires a `threading.Event` on each update) and is used by `MenuNavigator._send()` to
wait for panel responses.

**Heater read/write follows §13.3 restore discipline:**
1. Navigate to heater item
2. If "Manual Off": PLUS to enable (reveals stored °F)
3. Adjust setpoint with PLUS/MINUS, RIGHT to lock in
4. If heater was off before: restore "Manual Off" state before exiting
5. `fast_exit()` (MENU until default display, then RIGHT)

**VSP slot 4 activation** uses the FILTER off→on transient window (§6.2 of RS-485 spec).

---

## 6. REST API (`localhost:5757`)

### 6.1 Status and Display

| Method | Path | Description |
|---|---|---|
| GET | `/status` | Full pool state JSON |
| GET | `/display` | Current LCD line1, line2 (RS-485 backend) |
| GET | `/display/history` | Last 60 LCD frames (RS-485 backend) |

### 6.2 Circuit Control

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/circuit/{name}` | `{"on": bool}` | POOL, SPA, FILTER, LIGHTS, AUX_1, AUX_2, HEATER_1 |

HEATER_1 routes through `set_heater_enabled()` (menu navigation). For other circuits,
the AquaConnect backend sends the matching `KeyId`, the RS-485 backend sends the key command.

### 6.3 Heater (Menu Navigation)

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/heater/{which}/state` | — | Read setpoint + enabled state via menu |
| POST | `/heater/{which}/setpoint` | `{"temp_f": int}` | Write setpoint [65–104°F] |
| POST | `/heater/{which}/enable` | `{"on": bool}` | Enable/disable (Auto / Manual Off) |

### 6.4 VSP Slot 4

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/vsp/slot4` | — | Read slot 4 speed % |
| POST | `/vsp/slot4` | `{"speed_pct": int}` | Write slot 4 speed % |
| POST | `/vsp/slot4/activate` | — | Activate slot 4 via FILTER off→on window |

### 6.5 Chlorinator

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/chlorinator/{which}` | `{"percent": int}` | Set pool or spa chlorinator % |

### 6.6 Super Chlorinate

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/superchlorinate` | `{"on": bool}` | Enable/disable super chlorinate |

### 6.7 Bridge Health

| Method | Path | Notes |
|---|---|---|
| GET | `/bridge/health` | Returns `{"bridge_wedged": bool}` (cached state) |
| GET | `/bridge/health?probe=1` | Runs live canary probe, returns result |
| POST | `/bridge/health/reset` | Clears wedge flag manually |

### 6.8 Backend Selection

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/backend` | — | Returns `{"active": "aquaconnect"\|"rs485", "config": {...}}` |
| POST | `/backend` | `{"backend": str, "aquaconnect_host"?: str, "rs485_host"?: str, "rs485_port"?: int}` | Persists and self-exits (systemd restarts) |

### 6.9 Mode

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/mode` | `{"mode": "pool"\|"spa"}` | Switch valve mode |

### 6.10 Debug

| Method | Path | Notes |
|---|---|---|
| GET | `/debug/log` | Download current trace log; `?all=1` includes previous hour |
| POST | `/debug/wedge-test` | One-shot wedge scenario test harness |
| GET | `/debug/wedge-test` | Poll result of running wedge test |

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
  "circuits": ["FILTER", "LIGHTS", "HEATER_1"],
  "activeBodies": ["pool", "spa"],
  "enableActiveHeaterThermostat": true,
  "enablePoolHeaterThermostat": true,
  "enableSpaHeaterThermostat": true,
  "enableTemperatureSensors": true,
  "enableSpaModeSwitch": true,
  "enableChlorinatorFan": true,
  "enablePumpSpeedFan": true,
  "enableVspSlotTiles": false
}
```

`backend` is pushed to the sidecar via `reconcileBackend()` on launch. If the sidecar is
already on the right backend, this is a no-op. If it differs, the sidecar restarts.

### 7.2 HomeKit Accessories

| HomeKit type | Name | Enabled by |
|---|---|---|
| Switch | Spa | `enableSpaModeSwitch` — On=spa, Off=pool |
| Switch | Filter | `circuits` includes FILTER |
| Switch | Lights | `circuits` includes LIGHTS |
| Switch | Heater | `circuits` includes HEATER_1 |
| Switch | Aux 1 | `circuits` includes AUX_1 |
| Switch | Aux 2 | `circuits` includes AUX_2 |
| Thermostat | Active Heat | `enableActiveHeaterThermostat` — mode-following |
| Thermostat | Pool Heat | `enablePoolHeaterThermostat` + pool in `activeBodies` |
| Thermostat | Spa Heat | `enableSpaHeaterThermostat` + spa in `activeBodies` |
| TemperatureSensor | Pool Temperature | `enableTemperatureSensors` |
| TemperatureSensor | Air Temperature | `enableTemperatureSensors` |
| Fan | Chlorinator | `enableChlorinatorFan` — rotation speed = chlorinator % |
| Fan | Filter Speed | `enablePumpSpeedFan` — rotation speed = live `pump_speed` from scroll |
| Fan | Speed 1–4 | `enableVspSlotTiles` — 4× Fan tiles, not shown on Home tab; see §10.1 |
| Switch | Bridge Needs Rebooting | Always registered |

### 7.3 HEATER_1 Switch Semantics

The Heater switch reflects **enabled state** (Auto vs Manual Off), not active-heating state.
It shows the correct body's enabled field based on current valve mode:

```typescript
const heaterEnabled = status.valve_mode === 'spa'
  ? (status.spa_heater_enabled ?? status.circuits['HEATER_1'] ?? false)
  : (status.pool_heater_enabled ?? status.circuits['HEATER_1'] ?? false);
```

Falls back to the `HEATER_1` LED bit only until the first menu navigation confirms the
enabled state. This means: switch on = heater armed and will heat when the pump is running;
switch off = heater disarmed (Manual Off). The switch does not pulse when actively heating —
that is shown by the thermostat tile's `CurrentHeatingCoolingState`.

### 7.4 Spa Mode Switch

`SpaModeAccessory` is a dedicated switch (On=spa, Off=pool). Toggling it calls
`POST /mode`. On poll, it reads `status.valve_mode` and updates accordingly.

This is separate from the Spa circuit switch. The Spa circuit switch (if configured)
controls the POOL/SPA/SPILLOVER cycle key; the Spa Mode switch provides a cleaner
HomeKit affordance for toggling between pool and spa heating contexts.

### 7.5 Three-Thermostat Model

One physical heater serves both pool and spa bodies, with two independent setpoints.
Three thermostat accessories provide full control:

**Accessory A — "Active Heat"** (`body = 'auto'`): follows whichever body is active per
`valve_mode`. Shows the active body's temp and setpoint. Setpoint writes go to the active
body's menu slot. Useful for Siri/automation ("set pool heat to 82") without specifying
pool vs spa.

**Accessory B — "Pool Heat"** (`body = 'pool'`): always shows pool setpoint, regardless
of current valve mode. Shows "Heating" when pool heater is enabled AND in pool mode.

**Accessory C — "Spa Heat"** (`body = 'spa'`): always shows spa setpoint.

`TargetHeatingCoolingState` is **pinned to Heat (1)** — setting it to 0 (Off) collapses
the temperature dial in HomeKit, hiding the setpoint even when populated. The actual
enabled/disabled state is conveyed by `CurrentHeatingCoolingState` (0=off, 1=heating)
and the tile's dynamic name. The mode toggle on the tile writes `HEATER_1`.

Setpoint range: 65°F–104°F (18.3°C–40.0°C). All HomeKit temperatures are Celsius internally;
the sidecar speaks Fahrenheit. Display units set to Fahrenheit.

### 7.6 Fan Accessories

Two `FanAccessory` instances (`FanRole = 'chlorinator' | 'pump'`):
- `RotationSpeed` = current % (chlorinator output or VSP slot4 speed)
- `Active` = 1 always (tile is always shown)
- Setting speed: chlorinator → `POST /chlorinator/pool`, pump → `POST /vsp/slot4`

The **Pump Speed** tile currently shows slot 4's configured speed %. It should instead show
the **currently running filter speed** (the speed the pump is actually operating at now),
which appears in the AquaConnect scroll as "Filter Speed  NN%". This value is already parsed
into `PoolState` from the scroll and available in `/status`. The fan tile should read this
field rather than `vsp_slot4_pct`. See §10 backlog.

### 7.7 BridgeHealthAccessory

A Switch tile that surfaces the AquaConnect command-path wedge state:
- **Off** = command path healthy
- **On** = command path wedged; power-cycle needed

Tapping the tile in either direction runs a live canary probe (`GET /bridge/health?probe=1`)
and snaps the tile to the true result. This makes it a "test button": tap it, and the tile
reflects the real bridge health. A `testing` flag prevents concurrent probes.

The tile state is also updated passively on every poll from `status.bridge_wedged`.

### 7.8 Polling

On each poll (`/status`, default 5000ms interval):

1. Update valve mode (`currentValveMode`), push to Spa Mode switch
2. Update circuit switches — HEATER_1 uses enabled state logic (§7.3), others use LED bit
3. Push `ThermostatState` to all three thermostat accessories
4. Update pool and air temperature sensors
5. Update chlorinator fan speed
6. Update pump speed fan
7. Update bridge health wedge state

No optimistic updates. All state reflects confirmed sidecar values.

### 7.9 Backend Reconciliation

On `didFinishLaunching`, before polling starts:

```typescript
const cur = await this.sidecar.getBackend();
if (cur.active !== this.cfg.backend) {
  await this.sidecar.setBackend({ backend: this.cfg.backend, ... });
}
```

If the sidecar is already on the right backend, no-op. If it switches, the sidecar
restarts (systemd); the plugin will fail a few polls while it comes back up, then
resume normally.

---

## 8. File Structure

```
homebridge-prologic/
├── package.json                    (version 0.1.0)
├── config.schema.json
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
    ├── switchAccessory.ts          ← generic circuit switch
    ├── spaModeAccessory.ts         ← Spa Mode switch (On=spa, Off=pool)
    ├── thermostatAccessory.ts      ← three-body thermostat model
    ├── temperatureAccessory.ts     ← read-only temperature sensor
    ├── fanAccessory.ts             ← chlorinator % / pump speed % fan tiles
    ├── bridgeHealthAccessory.ts    ← wedge indicator + live test button
    ├── sidecarClient.ts            ← HTTP client for sidecar REST API
    └── settings.ts                 ← constants, types, PoolStatus interface
```

---

## 9. Confirmed Hardware Observations

| Field | Observed value |
|---|---|
| Pool water temp | 77–79°F |
| Air temp | 66–75°F |
| Salt level | 3100 PPM |
| Spa chlorinator | 1% |
| Filter speed | 80% (Spa mode) / 60% Speed 2 (Pool mode) |
| Pool heater | Manual Off default; stored setpoint readable via PLUS peek |
| Spa heater | Manual Off default |
| AquaConnect LED field | 12-char alphanumeric (e.g. `TECD4C333333`), nibbles: 3=absent, 4=off, 5=on, 6=blink |
| AquaConnect scroll rate | One frame rotation ~10–30s on default cycling display |
| AUX_2 (canary circuit) | Inert on this installation; safe for wedge probe |
| RS-485 LCD frame format | 32-char string, no newline — entire frame in line1, line2 empty |
| Fault observed | "Check System / Inspect Cell" — T-Cell-15 salt cell comm fault |

---

## 10. Known Limitations and Future Work

| Item | Status | Notes |
|---|---|---|
| Pump tile shows live speed | Done | Fan tile reads `pump_speed` (live scroll value); labeled "Filter Speed" |
| VSP slot tiles | Not implemented | See §10.1 below |
| FILTER circuit as Fanv2 | Not implemented | Could expose pump on/off + rotation speed read-only alongside slot tiles |
| RS-485 backend parity | Partial | Navigation exists but AquaConnect is primary; RS-485 not verified end-to-end in current codebase |
| LIGHTS / AUX_1 write verify | Not tested | Never tested on healthy bridge; keycode table should be correct |
| Chlorinator % HomeKit write | Not wired | Endpoint exists; no HomeKit affordance yet |
| Super Chlorinate | Not wired | Endpoint exists; no HomeKit switch wired |
| Spillover mode | Not tested | POOL/SPA/SPILLOVER cycle not present on this installation |
| Valve mode detection lag | ~10–30s on AquaConnect | Depends on scroll position when mode changes |
| System fault indicator | Not implemented | "Check System" LCD frames not surfaced to HomeKit |
| Salt level sensor | Not wired | `salt_level` present in `/status`; no HomeKit sensor (no native salt type; could use AirQuality or custom) |
| `pump_speed` scroll parsing | Done | Already parsed via `_AC_SCROLL_PATTERNS` → `state.pump_speed`; present in `/status` |

### 10.1 VSP Filter Speed Slots

The variable-speed pump supports up to 4 named speed slots (Speed1–Speed4), each with an
independently configurable % target stored in the panel's Settings menu. Currently only
slot 4 is read/writable via the sidecar (`/vsp/slot4`).

**Desired behavior:**

- **Pump Speed tile** (existing): show `filter_speed_pct` — the speed the pump is actually
  running at right now, as read from the AquaConnect scroll frame. Not shown on the Home
  tab; visible when tapping into the accessory detail. Setting the slider activates slot 4
  (the "override" slot) at that speed.

- **Speed slot tiles** (new, 4×): one Fan tile per slot (Speed1–Speed4), each showing that
  slot's configured % and allowing it to be adjusted via menu navigation. Not shown on the
  Home tab (`addCategory: BRIDGE` or similar). Tapping a slot tile "activates" that slot
  (runs `/vsp/slot4/activate` equivalent for that slot number).

**Sidecar work required:**
- Read all 4 slot speeds from VSP Speed Settings menu (currently only slot 4 is read)
- Expose `/vsp/slot{1-4}` endpoints for read/write/activate
- Parse `filter_speed_pct` from AquaConnect scroll and add to `PoolStatus`

**HomeKit work required:**
- Add `filter_speed_pct` to `PoolStatus` TypeScript interface in `settings.ts`
- Update Pump Speed Fan tile to read `filter_speed_pct` instead of `vsp_slot4_pct`
- Add 4× `VspSlotAccessory` tiles (Fan service, read-only speed + activate button)
- Config option `enableVspSlotTiles: bool` (default `false`); when `true`, registers 4×
  `VspSlotAccessory` tiles hidden from the Home tab but visible in the accessory detail view
