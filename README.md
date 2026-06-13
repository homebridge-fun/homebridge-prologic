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
| Pool | Switch | POOL circuit |
| Spa | Switch | SPA circuit |
| Filter | Switch | FILTER pump |
| Lights | Switch | LIGHTS circuit |
| Heater | Switch | HEATER_1 on/off |
| Pool Heater | Thermostat | Current temp + setpoint (Heat/Off only) |
| Pool Temperature | Temperature Sensor | Read-only, °F→°C converted |
| Air Temperature | Temperature Sensor | Read-only, °F→°C converted |

## References

- [`aqualogic` library](https://github.com/swilson/aqualogic)
- [Home Assistant AquaLogic integration](https://www.home-assistant.io/integrations/aqualogic/)
- [Homebridge platform plugin docs](https://developers.homebridge.io/#/api/platform-plugins)
