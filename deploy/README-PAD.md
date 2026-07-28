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
3. Clone this repo to `$HOME` (check out the working branch if not yet merged):
   ```bash
   git clone <repo-url> ~/homebridge-prologic
   cd ~/homebridge-prologic && git checkout claude/gracious-planck-1yz8v9
   ```
4. Run the installer:
   ```bash
   bash ~/homebridge-prologic/deploy/install-pad.sh
   ```
5. **Update the hop.** A fresh Tailscale node gets a **new tailnet IP**. Note it
   (`tailscale ip -4` on the pad) and set `rs485bridgeHost` in the Homebridge
   plugin config on the hop to match. If you use a bearer token, put the same
   value in the hop-side sidecar config too.

That's it — the service is enabled, starts on boot, survives USB replug (the
udev rule re-pins `latency_timer=1`), and the installer also applies the
memory-pressure hardening (persistent journal, earlyoom, `swappiness=10`) so a
spike can't wedge the Pi.

> **Tailnet IP tip:** to avoid re-editing the hop after every re-image, you can
> give the pad a stable name/tag in the Tailscale admin console and point the
> hop at that instead of the raw `100.x` — but MagicDNS may not resolve on the
> hop, so the raw IP is the reliable default.

## What the installer sets up

| Piece | Where | Why |
|---|---|---|
| `aqualogic==3.4`, `pyserial` | pip (`--break-system-packages`) | the serial protocol lib (version pinned — 3.4's `_write_to_serial` bug is patched in the daemon) |
| `dialout` group | the run user | serial port access |
| `99-ftdi-low-latency.rules` | `/etc/udev/rules.d/` | pins FTDI `latency_timer=1` across reboot/replug — **the 100%-write fix** |
| `/etc/pool-bridge.env` | root-only (0600) | holds the bearer token + bind address (NOT committed) |
| `pool-bridge.service` | `/etc/systemd/system/` | runs the daemon (MemoryMax cap), binds the tailnet IP, restarts on failure |
| persistent journal + `earlyoom` + `swappiness=10` | `/etc/systemd/journald.conf`, `earlyoom.service`, `/etc/sysctl.d/` | 512MB memory-pressure guards — kill a hog before a swap-thrash freeze, and keep a readable crash trail |
| health sampler (`pool-healthlog.timer`) | `/usr/local/bin/pad-healthlog.sh`, `/var/log/pad-health.csv` | 5-min CSV of memory/swap + Pi under-voltage, kept 30 days (logrotate) — makes an intermittent freeze or a power/brownout issue diagnosable after the fact |

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

## Health history (memory + power)

A systemd timer samples the Pi every 5 min into `/var/log/pad-health.csv`
(rotated daily, 30 kept). Columns: memory/swap MB, load, `throttled` bitmask,
under-voltage flags, SoC temp, and the bridge daemon's RSS.

```bash
# last ~2 hours at a glance
column -t -s, /var/log/pad-health.csv | tail -25

# any under-voltage EVER since the log started? (uv_now / uv_since_boot columns)
awk -F, 'NR>1 && ($8==1 || $9==1){print $1, "throttled="$7}' /var/log/pad-health.csv

# lowest available-memory samples (spot a leak or a pressure spike)
tail -n +2 /var/log/pad-health.csv | sort -t, -k4 -n | head

# is the bridge daemon RSS creeping up over days? (last column)
awk -F, 'NR>1{print $1, $11" MB"}' /var/log/pad-health.csv | tail -20
```

`throttled=0x0` and `uv_*` staying `0` means clean power. A non-zero
under-voltage flag points at the USB supply / pad circuit, not software.

## Security posture

- Daemon binds the **tailnet IP only** — nothing on the local Wi-Fi/LAN can open
  the socket. The Pi runs on the **main** network; no guest-VLAN isolation is
  needed because the tailnet bind removes the local attack surface.
- Tailnet IP is stable across network changes, so moving the Pi between Wi-Fi
  networks needs no reconfig.

### Auth: Tailscale ACL (recommended default — no secrets)

Restrict who on the tailnet may reach the bridge, in the Tailscale admin console
(**Access controls**). This needs **no shared secret** — auth rides on the
WireGuard identities Tailscale already manages, the policy lives in the admin
console (not this repo, not the Pi), and it **survives any Pi re-image with
nothing to copy**:

```jsonc
"acls": [
  // only the homebridge hop may reach the pad bridge
  { "action": "accept", "src": ["<hop-tailnet-name-or-tag>"], "dst": ["pool:8899"] }
]
```

With an ACL in place, run the bridge **token-less** — `install-pad.sh` leaves
`RS485_BRIDGE_TOKEN` empty by default and the daemon serves `/state` + `/key`
open (reachable only by the hop the ACL allows). No secret to store or leak.

### Optional: bearer token (defense-in-depth)

If you want app-level auth *in addition to* the ACL, pre-seed a token yourself —
the installer never generates or prints one:

```bash
# keep the value in a password manager; pass it at install time
RS485_BRIDGE_TOKEN='<your-secret>' bash deploy/install-pad.sh
```

It's written to `/etc/pool-bridge.env` (root-only, 0600) and required on `/state`
and `/key` (`/health` stays open). Put the **same** value in the hop-side sidecar
config. Nothing secret is ever committed or echoed. To rotate, edit the env file
directly and `sudo systemctl restart pool-bridge` (don't echo it to the terminal).

**No secrets belong in git.** The repo contains only variable *names*
(`${RS485_BRIDGE_TOKEN}`); values live solely in `/etc/pool-bridge.env` on the
pad and the sidecar config on the hop.
