# Changelog

All notable releases of `homebridge-prologic` (Homebridge plugin + Python
sidecar + web cockpit for a Hayward AquaPlus / ProLogic pool controller).

## 0.5.0 — RS-485 smart bridge in production

The big one: menu control moved off the AquaConnect HTTP box onto a **direct
serial link**, ~2.5× faster and 100% reliable.

- **Pad-Pi smart bridge.** A thin daemon (`sidecar/rs485_bridge.py`, systemd
  `pool-bridge`) on a pool-side Pi Zero 2 W owns the USB-RS485 serial link and
  its timing, exposing a small HTTP API (`/state`, `/key`, `/keys`,
  `/health`). The hop's sidecar drives it over Tailscale.
- **100% keypress landing.** Root-caused the residual ~33% drop to the **FTDI
  USB `latency_timer` (16 ms default)**; pinned to 1 ms via a udev rule → 30/30
  and 20/20 presses land vs the old TCP bridge's ~60% single-shot. `aqualogic`
  3.4's `_write_to_serial` `.send()`→`.write()` bug patched; `LONG_DISPLAY_UPDATE`
  frames captured so menu-nav LCD works.
- **`rs485bridge` sidecar backend.** `RS485BridgeBackend` mirrors
  `AquaConnectBackend` (`lcd` + `send_nav_key`) so the existing `MenuNavigator`,
  circuit-set, and prefetch paths work unchanged. **~2.5× faster** menu sweeps
  than AquaConnect (15.9 s vs 39.4 s), no wedging. Cut over to production.
- **Plugin backend selection.** `rs485bridge` selectable in the Homebridge UI
  (`rs485bridgeHost`/`rs485bridgePort`); AquaConnect kept as fallback.
- **Security / deploy.** Daemon binds the tailnet IP; optional bearer token
  (`RS485_BRIDGE_TOKEN`) — no secrets in git. One-command re-image installer
  (`deploy/install-pad.sh`) + runbook (`deploy/README-PAD.md`).
- **Cleanup.** Removed the Cloudflare-Tunnel-to-pad plumbing (Tailscale is the
  path) and the legacy `rs485` TCP-bridge backend from the plugin UI/config.
  Heater switch ⇄ thermostat sync; pump/VSP speeds removed from HomeKit
  (cockpit-only). Fixed a blank cockpit Panel Display on `rs485bridge` (LCD
  stream hub name mismatch).

## 0.4.2

- Wedge auto-re-arm; cockpit alerts pane.
- Heater write-confirm (immediate re-read after a heater toggle).
- Removed the legacy ContactSensor wedge form (now a Switch).
- Temperature-history chart with two-tier retention (5-min ≤1 day, 15-min
  beyond, 90-day cap).
- Security/robustness: HTML-escaping of fault/label text (XSS), and a
  FILTER-off-during-heater-cooldown "cry wolf" guard (no false wedge report).

## 0.4.0

- Version numbering realigned to the owner's scheme (the prior batch was
  effectively 0.3). No functional change beyond the version bump.

## 0.3.0

- **Interactive web cockpit** — control circuits, heater, chlorinator, VSP, and
  navigate the panel from the browser; live LCD stream (SSE).
- **Temperature history chart** in the cockpit.
- **Fault detection** — passive frame capture surfaces panel alerts; discovery
  log for unrecognized alert frames.
- **Caddy + HTTP Basic auth** for LAN cockpit access (`deploy/Caddyfile`,
  `deploy/set-cockpit-password.sh`); sidecar stays localhost-bound.
- Wedge/startup improvements.

## 0.2.1

- Cockpit and sidecar reliability/UX improvements.

## 0.2.0

Heater clarity, sensor accuracy, write-path safety, faster navigation.

- **Heater two-switch model** — "Heater Auto" (tappable, armed/Auto state) +
  "Heater Running" (read-only relay-firing), replacing the unreliable
  three-state fan.
- **Salt sensor** reads the real value (raised `VOCDensity` max 1000→4000 so the
  ~3200 PPM reading isn't clamped).
- **VSP speed slot tiles** — startup pre-fetch, debounced writes, zero-guard,
  per-slot floor (`vspSlotMinPct`). *(These pump/VSP HomeKit tiles were later
  removed in 0.5.0 — VSP is cockpit-only now.)*
- **Chlorinator %** writable (valve-mode aware, 600 ms debounce, snaps to panel
  grid).
- **Thermostat** setpoint debounced 600 ms (was a menu-nav per 0.5 °C step).
- **Frame-reader navigation** — N+1 requests per N keypresses (was N×5);
  inter-request gap lowered 0.9 s → 0.6 s.
- Wedge-risk audit of every slider-driven write path.

See `docs/RELEASE-v0.2.0.md` for the full 0.2.0 detail.

## 0.1.0

First tagged release; running on hardware (Hayward AquaPlus PS-8).

- **AquaConnect HTTP backend** alongside RS-485, selectable from plugin config
  (persisted to `backend.json`).
- Poll body switched `KeyId=00&` → `Update Local Server&`, eliminating ~29k
  phantom keypad events/day that caused the wedge condition.
- **Bridge wedge detection** — active AUX2 canary probe + a HomeKit tile that
  highlights when wedged and runs a live test on tap; immediate probe on any
  write failure.
- Heater enable/disable via Settings-menu nav with immediate confirm read;
  HEATER_1 switch + thermostats driven from one state field (no lag).
- Thermostats (pool/spa dedicated + mode-following auto), temperature sensors,
  chlorinator fan, VSP pump fan, spa-mode switch.
- Hourly rotating debug log + wedge-test harness.
