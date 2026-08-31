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
   git clone https://github.com/homebridge-fun/homebridge-prologic.git ~/homebridge-prologic
   ```
4. Run the installer:
   ```bash
   bash ~/homebridge-prologic/deploy/install-pad.sh
   ```
5. **Point the hop at the pad.** Set `rs485bridgeHost` in the Homebridge plugin
   config on the hop to the pad's **MagicDNS name** (`pool`) — stable across
   re-images, no re-editing — or its tailnet IP (`tailscale ip -4` on the pad).
   The name needs the hop to accept tailnet DNS (`tailscale set --accept-dns=true`;
   verify `getent hosts pool`). If you use a bearer token, put the same value in
   the hop-side sidecar config too.
6. **(Recommended) Harden the pad** once it's on the main LAN and Tailscale is
   direct (`tailscale ping pool` says `direct`): `sudo bash deploy/harden-pad.sh`
   locks it to tailnet-only access. Read the safety block first — SSH becomes
   tailnet-only. See "Network placement" below.

That's it — the service is enabled, starts on boot, survives USB replug (the
udev rule re-pins `latency_timer=1`), and the installer also applies the
memory-pressure hardening (persistent journal, earlyoom, `swappiness=10`) so a
spike can't wedge the Pi.

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
under-voltage flags, SoC temp, the bridge daemon's RSS, Wi-Fi dBm, and whether
the systemd journal is actually persisting (see below).

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

A clean series that simply *stops*, with no degradation before it, is an
external power cut (a tripped breaker or GFCI) rather than a fault on the Pi.

### Persistent journal — verify it, don't assume it

The last column reports `persistent` or `VOLATILE`. It exists because this has
silently failed twice.

Raspberry Pi OS ships
`/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf`, which sets
`Storage=volatile` to spare the SD card. **Drop-ins override
`/etc/systemd/journald.conf`**, so editing that file has no effect — an earlier
version of `install-pad.sh` did exactly that and appeared to work while
journald kept writing to tmpfs. Every reboot wiped the evidence, and it was
only discovered when a crash trail was actually needed and `-b -1` came back
empty.

`install-pad.sh` now writes `/etc/systemd/journald.conf.d/50-pad-persistent.conf`
(sorts after `40-*`, lives in `/etc` so package updates don't touch it) and
verifies the result rather than assuming it. To check by hand:

```bash
# Storage must resolve to persistent LAST -- later drop-ins win
systemd-analyze cat-config systemd/journald.conf | grep -i '^Storage='
# and the journal must be on disk, not in /run
journalctl --header | grep -i 'file path' | head -3
journalctl --list-boots
```

If any row of the health CSV says `VOLATILE`, crash forensics are not being
kept and `journalctl -b -1` will be empty after the next reboot. Re-run
`install-pad.sh` to repair it.

## Security posture

- Daemon binds the **tailnet IP only** — nothing on the local Wi-Fi/LAN can open
  the socket. This closes the **inbound** local attack surface regardless of
  which network the Pi is on.
- Tailnet IP is stable across network changes, so moving the Pi between Wi-Fi
  networks needs no reconfig.

### Network placement — main LAN + host firewall (current)

The Pi runs on the **main Wi-Fi** with a **host firewall** (`deploy/harden-pad.sh`)
that makes it **tailnet-only reachable**. This replaced an earlier guest-network
setup, for a concrete reason:

- **Guest-network isolation forced a DERP relay.** A guest network's
  client-to-client isolation blocks the pad and the hop from reaching each other
  directly, so Tailscale can't hole-punch and falls back to relaying every packet
  through a public **DERP** server. That adds latency + a public-relay dependency
  → intermittent brief timeouts (self-healing, but real), *despite* a strong
  Wi-Fi signal. The relay never leaks anything (it only sees encrypted
  WireGuard), so it's a **reliability** cost, not a security one.
- **On the main LAN, Tailscale connects DIRECT** — verify from the hop:
  `tailscale ping pool` should say `direct`, not `via DERP` — which removes the
  relay and the blips.

`harden-pad.sh` then locks the host: `ufw default deny incoming` + allow only
`tailscale0`, so SSH and the bridge API (:8899) are reachable **only over the
tailnet**; the LAN sees nothing. Tailscale needs no inbound ports (its outbound
hole-punch + the established/related rule carry the return traffic). Run it
**only after** confirming tailnet SSH — see the safety block in the script.

> **Tradeoff vs. the guest network.** Guest isolation also gave *outbound*
> containment — a compromised Pi couldn't pivot to trusted LAN devices. On the
> main LAN it can (the firewall restricts **inbound**, not outbound). This is the
> inherent tension: **any isolation that contains the Pi's outbound also blocks
> the direct Tailscale path and forces DERP.** We chose direct + tailnet-only
> inbound; if outbound containment matters more to you, keep it isolated and
> accept the DERP relay (it works fine). A middle path — outbound rules allowing
> internet/tailnet but not other LAN hosts — is possible but router/DNS-dependent.

> **Access:** reach the Pi **only over the tailnet** — `ssh <user>@pool` (MagicDNS
> name) or its tailnet IP, from the hop or any Tailscale device. Keep a local
> keyboard/monitor as the console fallback the first time you enable the firewall.
> Switching Wi-Fi networks (Pi OS uses NetworkManager; the SSH session drops as
> it switches, then reconnect over the tailnet):
> ```bash
> nmcli connection show                          # saved profiles
> sudo nmcli connection up "<MAIN_SSID>"         # or: device wifi connect ... password ...
> # reconnect over tailnet, then verify direct from the hop: tailscale ping pool
> ```

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
