# homebridge-prologic

Homebridge platform plugin for **Hayward ProLogic / AquaPlus** pool controllers via direct RS-485 serial communication through a WiFi serial bridge (USR-W610, Waveshare UART-WIFI232-B2, or similar).

This plugin **replaces** the AquaConnect (ACHN) local HTTP interface, which Hayward has permanently disabled for control commands. It communicates directly with the RS-485 bus on the pool controller PCB.

## Architecture

```
[AquaPlus panel] ←RS-485→ [WiFi serial bridge] ←WiFi/TCP→ [Pi 4: Python sidecar]
                                                                   ↕ localhost:5757
                                                       [Pi 4: Homebridge + this plugin]
                                                                   ↕ HAP
                                                              [HomeKit / iPhone]
```

The **Python sidecar** (`sidecar/pool_service.py`) uses the [`aqualogic`](https://github.com/swilson/aqualogic) library to maintain a persistent TCP connection to the serial bridge and exposes pool state + control via a local REST API. The Homebridge plugin polls this API every 5 seconds.

## Hardware Setup

### RS-485 wiring to AquaPlus PCB

Connect to **J2** or **J4** on the main board:

| Bridge terminal | PCB pin | Wire |
|---|---|---|
| A+ | Pin 2 (DATA+) | Black |
| B- | Pin 3 (DATA-) | Yellow |
| GND | Pin 4 (GND) | Green |

### WiFi serial bridge configuration

| Setting | Value |
|---|---|
| Mode | STA (station — joins your WiFi) |
| Protocol | TCP Server |
| Local port | 8899 |
| Baud | 19200 |
| Data bits | 8 |
| Parity | None |
| Stop bits | 2 |
| Transparent mode | Enabled |

## Installation

### 1. Install the Python sidecar

```bash
cd /path/to/homebridge-prologic/sidecar
sudo bash install.sh --bridge-host 192.168.50.XXX
```

Replace `192.168.50.XXX` with the IP address your serial bridge received from DHCP. The script installs `aqualogic` + `flask`, copies the service file, and registers a systemd unit that starts on boot.

Verify it's running:

```bash
sudo systemctl status pool-sidecar
curl http://127.0.0.1:5757/status
```

### 2. Install the Homebridge plugin

In the Homebridge UI, search for `homebridge-prologic` and install it, or:

```bash
npm install -g homebridge-prologic
```

### 3. Configure

Add to your Homebridge `config.json`:

```json
{
  "platform": "ProLogic",
  "name": "ProLogic",
  "sidecarHost": "127.0.0.1",
  "sidecarPort": 5757,
  "pollInterval": 5000,
  "circuits": ["POOL", "SPA", "FILTER", "LIGHTS", "HEATER_1"],
  "enablePoolHeaterThermostat": true,
  "enableTemperatureSensors": true
}
```

Available circuits: `POOL`, `SPA`, `FILTER`, `LIGHTS`, `SPILLOVER`, `AUX_1`, `AUX_2`, `HEATER_1`, `SUPER_CHLORINATE`

## HomeKit Accessories

| Accessory | Type | Notes |
|---|---|---|
| Pool | Switch | POOL circuit (shares the POOL/SPA toggle) |
| Spa | Switch | SPA circuit (shares the POOL/SPA toggle) |
| Filter | Switch | FILTER pump |
| Lights | Switch | LIGHTS circuit |
| Aux 1 / Aux 2 | Switch | AUX_1 / AUX_2 circuits |
| Heater | Switch | Toggles heater auto-mode (see limitations) |
| Pool Heater | Thermostat | Current temp + heating state; on/off via auto-mode |
| Pool Temperature | Temperature Sensor | Read-only, °F→°C converted |
| Air Temperature | Temperature Sensor | Read-only, °F→°C converted |

## Capabilities & Limitations

These reflect what the `aqualogic` library actually exposes (verified against
the library source), which is narrower than some third-party docs suggest:

**Works:**
- Reading pool/air/spa temperature, salt level, chlorinator output %, pump speed,
  and the on/off state of every circuit.
- Turning POOL, SPA, FILTER, LIGHTS, AUX_1 and AUX_2 on/off.
- Turning the heater on/off — routed through the heater **auto-mode** toggle,
  which is the only heater control the protocol exposes.

**Not supported by the hardware/library (these fail honestly rather than
silently doing nothing):**
- **Heater set-point** — the controller exposes no way to read *or* set the
  target temperature remotely. The Thermostat's target is display-only; change
  the real set-point at the physical panel.
- **Chlorinator output %** is read-only — there is no setter.
- **Super-chlorinate** and **Spillover** have no corresponding keypad key, so
  they cannot be toggled remotely.

Rare configuration changes (set-point, freeze protection, timers, relay config)
are handled at the physical panel at the equipment pad.

## Bridge health & wedge recovery

The sidecar tracks command-path health as `bridge_wedged`, surfaced as the
"Bridge Needs Rebooting" switch and a cockpit banner. **Recovery differs by
backend — this is a setup requirement, not just an internal detail:**

- **AquaConnect backend** — the web box can enter a silent read-only mode that
  **only a physical power-cycle clears**. The sidecar's recovery logic is written
  around that: on wedge it starts a 120s cooldown and blocks commands, expecting
  the box to reboot in that window. **This presumes you have set up an automated
  power-cycle** — a HomeKit automation that cuts power to a smart plug feeding the
  box when "Bridge Needs Rebooting" turns on. **Without that automation,
  AquaConnect wedge recovery is manual** (you must power-cycle the box yourself);
  the flag will not clear on its own.

- **RS-485 smart bridge backend** — no box to power-cycle. A "wedge" here just
  means the pad daemon was briefly unreachable (usually weak Wi-Fi). It shows a
  mild, **self-clearing "offline — reconnecting"** state driven purely by
  reachability — no cooldown, no command blocking, no automation required. The
  fix for repeated offline blips is physical (improve the pad's Wi-Fi signal),
  not a recovery automation. See `deploy/README-PAD.md`.

## References

- [`aqualogic` library](https://github.com/swilson/aqualogic)
- [Home Assistant AquaLogic integration](https://www.home-assistant.io/integrations/aqualogic/)
- [Homebridge platform plugin docs](https://developers.homebridge.io/#/api/platform-plugins)
