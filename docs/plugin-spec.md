# Homebridge ProLogic Plugin — Authoritative Specification

> **Version**: 2.0 — unified from original brief + all hardware bring-up learnings  
> **Updated**: 2026-06-17  
> **Status**: Implemented and running on hardware

---

## 1. Project Goal

A Homebridge 2.0 platform plugin that controls a **Hayward Goldline AquaPlus PS-8** pool
controller via direct **RS-485 serial protocol** through a **USR-W610 WiFi-to-RS485 bridge**.

The existing Hayward ACHN (AquaConnect) local HTTP interface is end-of-life and control
writes are permanently disabled — Hayward inserted a bare `return;` in `WebsFuncs.js` before
the `xhr.send()` call, and the Webster embedded server discards all `KeyId` POSTs regardless
of format. The ACHN is bypassed entirely. Cloud support ends December 31, 2026.

---

## 2. System Architecture

```
[AquaPlus panel] ←RS-485→ [USR-W610 @ pool pad] ←WiFi/TCP→ [Pi 4: pool_service.py]
                                                                      ↕ localhost:5757 REST
                                                         [Pi 4: Homebridge + TS plugin]
                                                                      ↕ HAP
                                                              [HomeKit / iPhone]
```

The Python sidecar (`pool_service.py`) runs as a systemd service and maintains a persistent
TCP connection to the USR-W610. The Homebridge TypeScript plugin polls the sidecar's `/status`
REST endpoint. This separation avoids Python↔Node.js process lifetime coupling and keeps
the RS-485 protocol handling in Python where the `aqualogic` library lives.

---

## 3. Hardware

### 3.1 RS-485 Physical Connection

- **Connector**: J2 or J4 on the AquaPlus main PCB (parallel, either works)
- **PCB label**: "Remote DSP comm (RS485 – 10VDC)"
- **Pins used** (two-wire differential + ground):

| J2/J4 pin | Label | Wire color | USR-W610 terminal |
|---|---|---|---|
| Pin 2 | DATA+ | black | A+ |
| Pin 3 | DATA− | yellow | B− |
| Pin 4 | GND | green | GND |

**⚠ Critical wiring notes from bring-up:**
- Pin 1 is **not a data line**. Wiring A/B to pin 1 and pin 4 produces ~8.5 B/s of garbage (oversampled noise), not real RS-485 data.
- Pin 3 carries **7.6 V DC power** on some variants — never connect it to the USR-W610.
- If frames arrive but CRC errors persist, **swap A and B** at the bridge terminals. RS-485 polarity is system-dependent; one of the two orientations will produce valid frames.
- The USR-W610 is USB-powered. Do not draw panel power.

### 3.2 USR-W610 Configuration

| Setting | Value |
|---|---|
| Mode | STA (joins existing WiFi) |
| Network protocol | TCP Server |
| Local port | 8899 |
| Baud rate | 19200 |
| Data bits | 8 |
| Parity | None |
| Stop bits | **2** (8N2) ← critical; 1 stop bit produces framing errors |
| Transparent mode | Enabled |
| 485 Switch interval | 100 µs (verified working) |

The Pi connects to the USR-W610 at its DHCP-assigned IP + port 8899 and treats the
TCP stream as a raw RS-485 byte stream.

### 3.3 Bring-Up Diagnostic Procedure

Use a raw socket capture to verify signal health before running the sidecar:

```python
# /tmp/cap.py — measure byte rate and count valid frame starts
import socket, time
s = socket.create_connection(('192.168.68.101', 8899), timeout=15)
buf, t0 = b'', time.time()
while time.time() - t0 < 12:
    buf += s.recv(4096)
elapsed = time.time() - t0
print(f'{len(buf)} bytes  {len(buf)/elapsed:.1f} B/s  10 02 count = {buf.count(bytes([0x10, 0x02]))}')
```

Expected result with correct wiring and 19200 8N2: ~190–290 B/s with `10 02 count > 100`.
- **< 10 B/s**: wrong pins (noise, not signal)
- **~190 B/s but 0 frame starts**: polarity inverted — swap A and B
- **frames but bad CRC**: wrong stop bits — set to 2
- **socket timeout, 0 bytes**: bridge not reachable (check DHCP / power)

---

## 4. RS-485 Protocol

### 4.1 Frame Format

```
DLE(0x10) STX(0x02) [command] [data] [cksum_hi] [cksum_lo] DLE(0x10) ETX(0x03)
```

Checksum = sum of all bytes from DLE through end of data, inclusive.

### 4.2 Key ID Map

| Button | KeyId (hex) | KeyId (dec) |
|---|---|---|
| RIGHT | 0x01 | 1 |
| MENU | 0x02 | 2 |
| LEFT | 0x03 | 3 |
| MINUS | 0x05 | 5 |
| PLUS | 0x06 | 6 |
| POOL/SPA | 0x07 | 7 |
| FILTER | 0x08 | 8 |
| LIGHTS | 0x09 | 9 |
| VALVE3 | 0x0B | 11 |
| HEATER1 | 0x0D | 13 |

Press + release are both required. Do not re-implement framing — use the `aqualogic` library.

---

## 5. Python Library — `aqualogic`

```bash
pip3 install aqualogic
```

### 5.1 What the Library Provides from Broadcast Frames (No Menu Navigation)

These values are decoded from the continuous RS-485 status broadcast and available
immediately after connect:

**Read (broadcast, always current):**
- Pool water temperature
- Air temperature
- Salt level (ppm)
- Current chlorinator output (%)
- Pump speed / mode (read-only from broadcast)
- State of every circuit (POOL, SPA, FILTER, LIGHTS, AUX1, AUX2, SPILLOVER, HEATER1, VALVE3)

**Write (direct key commands, no menu navigation):**
- Toggle any named circuit on/off (`aq.set_state(States.X, bool)`)
- HEATER1 is routed through `HEATER_AUTO_MODE` internally by the library

### 5.2 ⚠ What the Library Does NOT Provide from Broadcasts

**Critical correction from empirical testing:**

The following values are **NOT** available from RS-485 broadcast frames and **require
menu navigation** to read or write:

- **Heater setpoints** (pool °F target, spa °F target) — the panel does not broadcast these
- **Individual heater enable/disable state** — "Manual Off" vs enabled is a menu state, not a circuit bit
- **Chlorinator % target writes** — read-only from broadcast; setting requires menu navigation
- **Pump speed slot selection** (which VSP slot is active) — requires FILTER off→on activation window
- **VSP slot speed % values** — stored in Settings menu, require navigation to read/write

Any spec or documentation claiming heater setpoints are available from broadcast frames is
incorrect for this controller. If `pool_setpoint_f` and `spa_setpoint_f` are null, the sidecar
has not yet completed a menu navigation read — this is expected until the background refresher
fires (approximately 10 seconds after connect).

### 5.3 LCD Text Access (LcdCapture)

The `aqualogic` library calls `self._web.text_updated(text)` on every display frame but does
not store the text. We construct `AquaLogic(web_port=0)` (suppresses the built-in web server)
then set `aq._web = lcd` to intercept every frame via `LcdCapture`.

`LcdCapture` stores the latest text, fires a threading `Event` on each update (used by
`MenuNavigator._send()` to wait for panel responses), and keeps a 60-frame rolling history
accessible at `GET /display/history`.

**Important**: On this hardware the LCD text arrives as a single 32-character string with no
newline — the entire frame lands in `lines()[0]` and `lines()[1]` is empty. Match patterns
against `f'{l1} {l2}'` (the joined frame), not `l2` alone.

---

## 6. Sidecar Architecture (`sidecar/pool_service.py`)

### 6.1 Shared State

```python
@dataclass
class PoolState:
    circuits: dict            # {circuit_name: bool}
    pool_temp: float | None
    air_temp: float | None
    spa_temp: float | None
    salt_level: float | None
    chlorinator_percent: float | None
    pump_speed: int | None
    pool_setpoint_f: int | None   # null until menu read
    spa_setpoint_f: int | None    # null until menu read
    pool_heater_enabled: bool | None
    spa_heater_enabled: bool | None
    valve_mode: str | None        # 'pool' | 'spa' — from cycling display
    vsp_slot4_pct: int | None
    connected: bool
    last_update: float
```

All mutations go through `state_lock`. `panel_lock` guards the `panel` object (real or sim).

### 6.2 Valve Mode Detection

The panel's cycling default display includes a "Filter Speed  NN% Spa Mode" or
"Filter Speed  NN% Pool Mode" frame. The `on_change()` callback matches this against
the joined frame (`f'{l1} {l2}'`) because this hardware sends LCD text without newlines,
leaving `l2` empty.

```python
frame = f'{l1} {l2}'
if 'Pool Mode' in frame:
    state.valve_mode = 'pool'
elif 'Spa Mode' in frame:
    state.valve_mode = 'spa'
```

This updates every time that frame rotates through the cycling display (~every 10–30 seconds),
and immediately after any POOL/SPA mode switch that the next rotation catches.

### 6.3 Menu Navigator

`MenuNavigator` drives keypad keys over RS-485 and reads the LCD after each press.
All operations are text-anchored (never blind key-counts). A single `_nav_lock` serializes
all menu operations so they cannot interleave.

**Entry/exit discipline:**
- `_anchor()`: press MENU until `line1 == 'Settings Menu'` (up to 8 presses)
- `fast_exit()`: press MENU until `'Default Menu'`, then RIGHT — always called in `finally`

**Settings menu ring** (verified on hardware):

```
Settings Menu → Spa Heater1 → Pool Heater1 → VSP Speed Settings → Super Chlorinate →
Spa Chlorinator → Pool Chlorinator → Configuration Menu-Locked → (wraps)
```

Navigation: RIGHT advances, LEFT retreats. PLUS/MINUS adjust values while on an item.

### 6.4 Heater Read (Menu Navigation)

When heater is "Manual Off", the panel shows no temperature. To read the stored setpoint:

1. Navigate to the heater item (RIGHT ×1 for spa, RIGHT ×2 for pool from Settings Menu anchor)
2. If display shows "Manual Off": press PLUS to enable and reveal the stored °F
3. Parse the setpoint from `l2` (or joined frame)
4. If we enabled it to peek: RIGHT (lock in), LEFT (back to item), then toggle HEATER_1
   until "Manual Off" re-appears (restores prior state)
5. `fast_exit()`

### 6.5 Heater Write (Menu Navigation — §13.3 Restore Discipline)

1. Navigate to heater item
2. If "Manual Off": PLUS to enable (reveals stored °F)
3. Adjust in 1°F steps with PLUS/MINUS to reach `target_f` (clamped to [65, 104])
4. RIGHT to lock in value
5. If heater was off before: LEFT back to item, toggle HEATER_1 until "Manual Off" (restore)
6. `fast_exit()`

### 6.6 VSP Slot 4 Activation (§6.2/§6.4)

Activating VSP slot 4 uses a transient panel window that appears only when FILTER turns on:

1. Ensure FILTER is off (send FILTER key if currently on)
2. Send FILTER to turn on → panel shows `Filter On:SpdN / +/- to change` (~5-10s window)
3. Press PLUS until `Spd4` appears in `l1`; gate every press on `'Filter On:' in l1`
4. Window closes automatically; FILTER remains on in slot 4

This is the **only** way to change the active VSP slot. It is not a Settings menu operation.

### 6.7 Background Refresher Thread

A daemon thread reads heater state for both pool and spa bodies after connect, then
periodically thereafter:

- **Initial read**: 10 seconds after first connect (waits for `state.connected`)
- **Subsequent reads**: every 600 seconds (configurable via `--heater-refresh`, 0 disables)
- Logs `'Initial heater state read complete.'` after the first successful read

This ensures `pool_setpoint_f`, `spa_setpoint_f`, `pool_heater_enabled`, and
`spa_heater_enabled` are populated at startup without requiring HomeKit interaction.

### 6.8 Simulation Mode

`--simulate` skips the `aqualogic` library entirely and instantiates `SimPanel`, a full
state machine emulating the verified Settings menu ring and VSP activation window.
Only Flask is required. All navigator code paths exercise identically to real hardware.

SimPanel defaults: pool heater off (Manual Off, 87°F stored), spa heater off (Manual Off,
99°F stored), VSP slots [95%, 60%, 50%, 50%], mode=pool.

---

## 7. REST API (`localhost:5757`)

### 7.1 Status and Display

| Method | Path | Description |
|---|---|---|
| GET | `/status` | Full pool state JSON (all fields from PoolState) |
| GET | `/display` | Current LCD line1, line2 |
| GET | `/display/history` | Last 60 LCD frames with timestamps |

### 7.2 Circuit Control

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/circuit/{name}` | `{"on": bool}` | Circuit names: POOL, SPA, FILTER, LIGHTS, AUX_1, AUX_2, HEATER_1 |

SUPER_CHLORINATE and SPILLOVER have no keypad key and return 422.
HEATER_1 is routed through `HEATER_AUTO_MODE` internally by the library.

### 7.3 Heater (Menu Navigation)

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/heater/{which}/state` | — | Read setpoint via menu; `which` = pool\|spa |
| POST | `/heater/{which}/setpoint` | `{"temp_f": int}` | Write setpoint; handles Manual Off restore |

### 7.4 VSP Slot 4

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/vsp/slot4` | — | Read Speed4 % via menu navigation |
| POST | `/vsp/slot4` | `{"speed_pct": int}` | Write Speed4 %; snaps to 5% grid |
| POST | `/vsp/slot4/activate` | — | Activate slot 4 via FILTER off→on window |

### 7.5 Diagnostics

| Method | Path | Notes |
|---|---|---|
| POST | `/keypad/{key}` | Send raw key (diagnostics only; do not use from automation) |

---

## 8. Homebridge Plugin (TypeScript)

### 8.1 Platform Config

```json
{
  "platform": "ProLogic",
  "name": "ProLogic",
  "sidecarHost": "127.0.0.1",
  "sidecarPort": 5757,
  "pollInterval": 5000,
  "circuits": ["POOL", "SPA", "FILTER", "LIGHTS", "HEATER_1"],
  "activeBodies": ["pool", "spa"],
  "enableActiveHeaterThermostat": true,
  "enablePoolHeaterThermostat": true,
  "enableSpaHeaterThermostat": true,
  "enableTemperatureSensors": true
}
```

### 8.2 HomeKit Accessories

| HomeKit type | Name | Notes |
|---|---|---|
| Switch | Pool | Mutually exclusive with Spa/Spillover |
| Switch | Spa | Mutually exclusive with Pool/Spillover |
| Switch | Filter | FILTER circuit |
| Switch | Lights | LIGHTS circuit |
| Switch | Aux 1 | AUX_1 circuit |
| Switch | Aux 2 | AUX_2 circuit |
| Switch | Heater | HEATER_1 — gates `pool_heater_enabled` / `spa_heater_enabled` for active body |
| Switch | Super Chlorinate | SUPER_CHLORINATE (no keypad key — 422; remove or mark read-only) |
| Thermostat | Active Heat | Accessory A — mode-following; see §8.4 |
| Thermostat | Pool Heat | Accessory B — always pool setpoint; see §8.4 |
| Thermostat | Spa Heat | Accessory C — always spa setpoint; see §8.4 |
| TemperatureSensor | Pool Temperature | Read-only; current pool water temp |
| TemperatureSensor | Air Temperature | Read-only; current air temp |

### 8.3 Pool/Spa Mode Selection

The physical panel has **one** POOL/SPA/SPILLOVER cycle button — not independent switches.
Pressing it advances through mutually exclusive states. The RS-485 library `set_state`
for POOL (0x07) cycles; there is no separate "Spa on" command.

In HomeKit: expose Pool, Spa (and optionally Spillover) as three Switch accessories.
When one is turned on, calculate how many cycle presses are needed to reach the target
mode from the current `valve_mode`, fire them, then reflect the other two as OFF on the
next poll. Turning a switch off with no other mode specified defaults to Pool.

Valve mode is read from `status.valve_mode` (`'pool'` | `'spa'` | `null`), which the
sidecar updates from the LCD cycling display. See §6.2 for detection.

### 8.4 Three-Thermostat Model

One physical heater serves both pool and spa bodies, with two independent setpoints.
HomeKit has no native "dual-setpoint" thermostat, so three accessories are used:

**Accessory A — "Active Heat"** (`body = 'auto'`)
- Always reflects whichever body is active per `valve_mode`
- Dynamic name: "Heat — Pool" when valve_mode=pool, "Heat — Spa" when valve_mode=spa
- `CurrentTemperature`: temp of the active body
- `TargetTemperature`: setpoint of the active body
- Setpoint writes go to the active body's menu slot

**Accessory B — "Pool Heat"** (`body = 'pool'`)
- Always reflects the pool setpoint, regardless of current valve mode
- Dynamic name: "Pool Heat — Heating" | "— Standby" | "— Off"
- "Heating" = heater enabled AND valve_mode=pool
- "Standby" = heater enabled but valve_mode=spa (pool setpoint armed but not active)
- "Off" = heater disabled (Manual Off)

**Accessory C — "Spa Heat"** (`body = 'spa'`)
- Mirror of B for the spa setpoint

**TargetHeatingCoolingState pinned to Heat:**
HomeKit collapses the temperature dial when `TargetHeatingCoolingState = 0` (Off),
hiding the setpoint even when it's populated. To keep the dial always visible, pin
`TargetHeatingCoolingState` getter to `1` (Heat). The real on/off state is truthfully
conveyed by `CurrentHeatingCoolingState` (0 when not actively heating) and the dynamic
accessory name. The mode toggle still writes HEATER_1.

**Setpoint range**: 65°F–104°F (18.3°C–40.0°C), 0.5°C step.

**Display units**: set `TemperatureDisplayUnits = 1` (Fahrenheit). HomeKit internally
always stores Celsius; convert with `C = round(((F - 32) * 5/9) * 10) / 10`.

### 8.5 Temperature Conversion

```typescript
export function fahrenheitToCelsius(f: number): number {
  return Math.round(((f - 32) * 5 / 9) * 10) / 10;
}
export function celsiusToFahrenheit(c: number): number {
  return c * 9 / 5 + 32;
}
```

### 8.6 Polling

Default 5000 ms poll interval. The sidecar maintains the persistent RS-485 connection;
Homebridge polls `/status` via `SidecarClient` (HTTP, 30s timeout). On each poll:

1. Update all circuit switch states from `status.circuits`
2. Update `currentValveMode` (used by Accessory A setpoint writes)
3. Push `ThermostatState` to all three thermostat accessories
4. Update temperature sensors

State reflects confirmed sidecar values; commands are fire-and-confirm (no optimistic updates).

---

## 9. File Structure

```
homebridge-prologic/
├── package.json
├── config.schema.json
├── docs/
│   ├── plugin-spec.md              ← this file
│   └── aqualogic-automation-spec.md  ← detailed menu navigation spec
├── sidecar/
│   ├── pool_service.py             ← aqualogic wrapper + Flask REST API
│   ├── requirements.txt            ← aqualogic, flask
│   └── install.sh                  ← pip venv + systemd service setup
└── src/
    ├── index.ts
    ├── platform.ts                 ← accessory registration + poll loop
    ├── switchAccessory.ts          ← generic circuit switch
    ├── thermostatAccessory.ts      ← three-body thermostat model
    ├── temperatureAccessory.ts     ← read-only sensor
    ├── sidecarClient.ts            ← HTTP client for sidecar REST API
    └── settings.ts                 ← constants, types, unit conversion
```

---

## 10. Confirmed Observations on Target Hardware

| Field | Observed value |
|---|---|
| Pool water temp | 77–79°F |
| Air temp | 66–75°F |
| Salt level | 3100 PPM |
| Spa chlorinator | 1% |
| Filter speed | 80% (Spa mode) / 60% Speed 2 (Pool mode) |
| Pool heater | Manual Off (default); stored setpoint readable via PLUS peek |
| Spa heater | Manual Off (default); stored setpoint readable via PLUS peek |
| LCD frame format | 32-char string, **no newline** — entire frame in line1, line2 empty |
| Valve mode frame | "Filter Speed  NN% Spa/Pool Mode" (appears in cycling display) |
| Fault observed | "Check System / Inspect Cell" — T-Cell-15 salt cell comm fault |

---

## 11. Known Limitations and Future Work

| Item | Status | Notes |
|---|---|---|
| Pump speed display | Not implemented | `pump_speed` in `/status` but no HomeKit accessory yet |
| VSP speed override | Endpoint exists (`/vsp/slot4`) | No HomeKit Fan accessory yet |
| Chlorinator % write | Not implemented | Requires menu navigation; read-only for now |
| System fault indicator | Not implemented | "Check System" frames appear in LCD history; could surface as ContactSensor |
| Spillover mode | Config option exists | Not tested on this system (pool+spa only) |
| Valve mode lag | ~10–30s | Updates only when "Filter Speed … Mode" frame rotates into view |
| Signature on commits | Environment limitation | SSH signing key is 0-byte in this container; commits show Unverified on GitHub |
