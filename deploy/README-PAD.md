# Pad Pi — RS-485 smart bridge

The pad-mounted Pi Zero 2 W owns the direct-serial RS-485 link to the AquaLogic
panel and exposes a tiny HTTP API (`sidecar/rs485_bridge.py`) that the main
sidecar consumes over Tailscale. This is the write-reliability fix: direct
serial + `latency_timer=1` gives **100% keypress landing** vs ~60% over the old
TCP/WiFi bridge.

## Re-image from scratch (disaster recovery)

1. Flash **Raspberry Pi OS Lite**, boot headless, enable SSH, set hostname `pool`.
2. Install Tailscale and join the tailnet:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
3. Clone this repo to `$HOME`:
   ```bash
   git clone <repo-url> ~/homebridge-prologic
   ```
4. Run the installer:
   ```bash
   bash ~/homebridge-prologic/deploy/install-pad.sh
   ```
5. Copy the **token** it prints into the Homebridge plugin / sidecar config so
   the hop can authenticate to the bridge.

That's it — the service is enabled, starts on boot, and survives USB replug
(the udev rule re-pins `latency_timer=1`).

## What the installer sets up

| Piece | Where | Why |
|---|---|---|
| `aqualogic==3.4`, `pyserial` | pip (`--break-system-packages`) | the serial protocol lib (version pinned — 3.4's `_write_to_serial` bug is patched in the daemon) |
| `dialout` group | the run user | serial port access |
| `99-ftdi-low-latency.rules` | `/etc/udev/rules.d/` | pins FTDI `latency_timer=1` across reboot/replug — **the 100%-write fix** |
| `/etc/pool-bridge.env` | root-only (0600) | holds the bearer token + bind address (NOT committed) |
| `pool-bridge.service` | `/etc/systemd/system/` | runs the daemon, binds the tailnet IP, restarts on failure |

## Day-to-day

```bash
# update after a git pull on the pad
cd ~/homebridge-prologic && git pull && sudo systemctl restart pool-bridge

# logs
journalctl -u pool-bridge -f

# health (no token needed)
curl -s http://$(tailscale ip -4 | head -1):8899/health

# re-run install-pad.sh any time — it's idempotent and preserves the token
```

## Security posture

- Daemon binds the **tailnet IP only** — nothing on the local Wi-Fi/LAN can open
  the socket. (The Pi runs on the **main** network; no guest-VLAN isolation is
  needed because the tailnet bind + token already remove the local attack
  surface.)
- **Bearer token** required on `/state` and `/key`; `/health` is open for
  liveness. Token lives in `/etc/pool-bridge.env` (0600) and is echoed to the
  sidecar config side.
- Tailnet IP is stable across network changes, so moving the Pi between Wi-Fi
  networks needs no reconfig.
