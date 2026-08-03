# homebridge-prologic

Homebridge platform plugin (plus a Python sidecar and a web cockpit) for
**Hayward ProLogic / AquaLogic / AquaPlus** pool controllers. It goes well beyond
the simple on/off switches earlier plugins offered — it drives the panel's deeper
settings: adjustable **heat setpoints**, named **light scene / color-program
selection**, **chlorinator output %**, **variable-speed pump** presets, and full
circuit control, in both HomeKit and a browser cockpit.

## Two connection modes

The sidecar can reach the panel through **either** backend — you choose one in
config:

- **AquaConnect (ACHN) local HTTP** — the AquaConnect box's local network
  interface. Fully functional today.
- **Direct RS-485** — talks straight to the RS-485 bus on the controller PCB via
  a small Raspberry Pi **"pad bridge"** (a Pi Zero at the equipment pad running
  `sidecar/rs485_bridge.py` over a USB-RS485 adapter), reached over Tailscale.

The direct RS-485 path is a deliberate **hedge**: Hayward has a pending change
that could remove AquaConnect's local access, and the RS-485 backend keeps
everything working if that lands (it also supports panels with no AquaConnect box
at all). Both backends expose the same features through the same REST API.

## What it controls

Earlier Hayward/AquaLogic plugins mostly surfaced a handful of on/off circuits.
This one exposes the settings you'd normally have to walk the panel's menus for:

- **Heat** — per-body **setpoints you can change**, armed/enabled state, and live
  Running/Idle heating status.
- **Lights** — **named scene / color-program selection** for Hayward ColorLogic
  (pool) and Pentair IntelliBrite (spa) lights, plus on/off, exposed as a HomeKit
  Television tile and a cockpit picker.
- **Chlorinator** — salt-cell output percentage per body, plus super-chlorinate.
- **Pump** — variable-speed presets (VSP slots) and live speed.
- **Circuits** — filter, spa mode, aux, spillover, heater enable, and more.
- Temperature/salt sensors, plus a **web cockpit** with live control and a
  temperature-history chart.

## Architecture

```
                    ┌── AquaConnect box (local HTTP) ─────────────┐
[AquaLogic panel] ──┤                                             ├── [Python sidecar] ── localhost:5757
                    └── RS-485 bus → Pi pad bridge (Tailscale) ──┘        ↕
                                                          [Homebridge + this plugin] ↔ HAP ↔ [HomeKit]
                                                          [web cockpit]
```

The **Python sidecar** (`sidecar/pool_service.py`) maintains the connection to the
chosen backend (via HTTP to the AquaConnect box, or to the Pi pad bridge which
uses the [`aqualogic`](https://github.com/swilson/aqualogic) library on the wire)
and exposes pool state + control via a local REST API. The Homebridge plugin
polls this API every 5 seconds; the web cockpit talks to it directly.

## Hardware Setup

### RS-485 backend — Raspberry Pi pad bridge

The direct-RS-485 backend runs a small **Pi Zero at the equipment pad** with a
USB-RS485 adapter on the controller bus, running `sidecar/rs485_bridge.py` as a
systemd service (`pool-bridge`) and reachable over Tailscale. Full build and
re-image runbook: [`deploy/README-PAD.md`](deploy/README-PAD.md); provisioning
script: [`deploy/install-pad.sh`](deploy/install-pad.sh).

> Earlier prototypes used a WiFi/ethernet serial bridge (transparent TCP↔serial)
> instead of the Pi. That approach **did not work** — the panel only accepts a
> keypress in a narrow window after each keep-alive, which a transparent bridge
> can't hit reliably, so reads worked but writes were dropped. The Pi pad bridge
> replaced it and is the only supported RS-485 path.

RS-485 wiring to the AquaPlus PCB (adapter to **J2**/**J4** on the main board):
A+ → Pin 2 (DATA+), B− → Pin 3 (DATA−), GND → Pin 4 (GND).

## Installation

### 1. Install the Python sidecar (on the Homebridge host)

Install the sidecar service, then point it at your backend (AquaConnect box IP,
or the pad bridge host). See `sidecar/` for the service unit and config. Verify
it's running:

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

## Acknowledgments

- **[cupshir](https://github.com/cupshir)** — whose AquaConnect Homebridge plugin
  I ran before building this one. It's the prior art and inspiration for the
  AquaConnect approach here; this project grew out of wanting deeper control and a
  fallback path.
- **[`swilson/aqualogic`](https://github.com/swilson/aqualogic)** — the Python
  RS-485 protocol library that decodes the AquaLogic bus. It runs on the Pi pad
  bridge and is what makes the direct-RS-485 backend possible.
- **[`SteveTheGeekHA/AquaConnectDeviceHandler`](https://github.com/SteveTheGeekHA/AquaConnectDeviceHandler)**
  — reference implementation used to verify the AquaConnect web key codes and
  protocol for the AquaConnect backend.

## References

- [`aqualogic` library](https://github.com/swilson/aqualogic)
- [Home Assistant AquaLogic integration](https://www.home-assistant.io/integrations/aqualogic/)
- [Homebridge platform plugin docs](https://developers.homebridge.io/#/api/platform-plugins)
