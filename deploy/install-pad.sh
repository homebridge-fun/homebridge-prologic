#!/usr/bin/env bash
#
# install-pad.sh — one-shot, idempotent setup for the RS-485 pad Pi.
#
# Turns a fresh Raspberry Pi OS image + this git checkout into a running,
# reboot-surviving bridge. Safe to re-run: it only changes what's out of date.
#
# Re-image runbook (after nuking the Pi):
#   1. Flash Raspberry Pi OS Lite, boot, enable SSH, set hostname 'pool'.
#   2. Install + log into Tailscale:   curl -fsSL https://tailscale.com/install.sh | sh
#                                       sudo tailscale up
#   3. Clone this repo to $HOME:        git clone https://github.com/homebridge-fun/homebridge-prologic.git ~/homebridge-prologic
#   4. Run this:                        bash ~/homebridge-prologic/deploy/install-pad.sh
#   5. Copy the printed token into the Homebridge plugin / sidecar config.
#
# What it does (each step idempotent):
#   - installs python deps (aqualogic==3.4, pyserial)
#   - adds you to the 'dialout' group (serial access)
#   - installs the FTDI low-latency udev rule (latency_timer=1, the 100%-write fix)
#   - writes /etc/pool-bridge.env with a generated token (kept if already present)
#   - installs + enables the pool-bridge systemd service
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
ENV_FILE="/etc/pool-bridge.env"
PORT="${RS485_BRIDGE_PORT:-/dev/ttyUSB0}"

echo "==> pad bridge install (repo: $REPO, user: $RUN_USER)"

# 0. System prerequisites --------------------------------------------------
# Raspberry Pi OS Lite ships without pip (and sometimes without git). Bootstrap
# them via apt before any pip use, so a clean Lite image installs end-to-end.
if ! command -v pip3 >/dev/null 2>&1; then
  echo "==> installing python3-pip (missing on Lite)"
  sudo apt-get update -qq && sudo apt-get install -y python3-pip
fi

# 1. Python deps -----------------------------------------------------------
echo "==> installing python deps (aqualogic==3.4, pyserial)"
pip3 install --break-system-packages --quiet 'aqualogic==3.4' pyserial

# 2. Serial group ----------------------------------------------------------
if id -nG "$RUN_USER" | grep -qw dialout; then
  echo "==> $RUN_USER already in 'dialout'"
else
  echo "==> adding $RUN_USER to 'dialout' (log out/in to take effect)"
  sudo usermod -aG dialout "$RUN_USER"
fi

# 3. FTDI low-latency udev rule -------------------------------------------
echo "==> installing FTDI latency_timer=1 udev rule"
sudo cp "$REPO/deploy/99-ftdi-low-latency.rules" /etc/udev/rules.d/
sudo udevadm control --reload-rules
# --action=add: the rule applies on the "add" event; a bare `udevadm trigger`
# defaults to action=change and would silently no-op for an add-scoped rule.
sudo udevadm trigger --action=add --attr-match=subsystem=usb-serial || true
if [ -e "/sys/bus/usb-serial/devices/$(basename "$PORT")/latency_timer" ]; then
  echo -n "    latency_timer now: "
  cat "/sys/bus/usb-serial/devices/$(basename "$PORT")/latency_timer"
fi

# 4. Environment file (bind address + optional token) ---------------------
# Tailnet IP is stable across networks; discover it so a re-image picks up the
# node's current address automatically.
TAILNET_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
LISTEN="${TAILNET_IP:-0.0.0.0}:8899"

# Auth model, in order of preference:
#   1. Tailscale ACL (recommended default): restrict src->pool:8899 in the
#      Tailscale admin console. NO shared secret — nothing to store, echo, or
#      copy on re-image. Leave the token empty here.
#   2. Bearer token (optional defense-in-depth): pre-seed it yourself by
#      exporting RS485_BRIDGE_TOKEN before running this script, or by keeping
#      the existing value in $ENV_FILE. This script NEVER generates or prints a
#      token — secrets are yours to manage (e.g. a password manager).
TOKEN="${RS485_BRIDGE_TOKEN:-}"
if [ -z "$TOKEN" ] && sudo test -f "$ENV_FILE"; then
  # Preserve an existing token across re-runs without ever echoing it.
  TOKEN="$(sudo grep -oP '^RS485_BRIDGE_TOKEN=\K.*' "$ENV_FILE" 2>/dev/null || true)"
fi

sudo tee "$ENV_FILE" >/dev/null <<EOF
# RS-485 pad bridge config. If RS485_BRIDGE_TOKEN is set it is a secret — do
# NOT commit this file (it lives only here, root-only 0600).
RS485_BRIDGE_TOKEN=$TOKEN
RS485_BRIDGE_LISTEN=$LISTEN
RS485_BRIDGE_PORT=$PORT
RS485_BRIDGE_SCRIPT=$REPO/sidecar/rs485_bridge.py
EOF
sudo chmod 600 "$ENV_FILE"

if [ -n "$TOKEN" ]; then
  echo "==> $ENV_FILE written WITH a bearer token (value not shown)"
else
  echo "==> $ENV_FILE written token-less — relying on a Tailscale ACL for auth"
  echo "    (restrict src -> pool:8899 in the Tailscale admin console; see README-PAD.md)"
fi

if [ -z "$TAILNET_IP" ]; then
  echo "    WARNING: no tailnet IP found (is tailscaled up?). Bound $LISTEN —"
  echo "    re-run this script after 'tailscale up' to bind the tailnet IP."
fi

# 5. systemd service -------------------------------------------------------
echo "==> installing + enabling pool-bridge.service"
sudo cp "$REPO/deploy/pool-bridge.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable pool-bridge.service
sudo systemctl restart pool-bridge.service
sleep 3
sudo systemctl --no-pager --lines=15 status pool-bridge.service || true

# 6. Memory-pressure hardening --------------------------------------------
# The Pi Zero 2 W has 512MB. Three field freezes traced to swap-thrash under
# memory pressure (the hardware watchdog can't catch a livelock — systemd stays
# alive petting it). Lite avoids the desktop RAM hogs, but keep the guards so a
# spike can't wedge the whole Pi again. All idempotent.
echo "==> memory-pressure hardening (persistent journal + earlyoom + swappiness)"

# Persistent journal (capped) so the NEXT freeze leaves a readable `-b -1` trail.
#
# This MUST be a drop-in, not an edit to /etc/systemd/journald.conf. Raspberry
# Pi OS ships /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf,
# which sets Storage=volatile to spare the SD card -- and drop-ins override the
# main config file, so editing that file can never win. An earlier version of
# this script did exactly that and silently had no effect for months: journald
# kept logging to tmpfs and every reboot wiped the evidence, which is precisely
# the failure this section exists to prevent.
#
# Ours sorts after 40-* and lives in /etc (higher precedence than /usr/lib, and
# untouched by package updates). Size is capped and compressed: a few MB/day is
# negligible SD wear, and unclean shutdowns are the real card killer anyway.
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/50-pad-persistent.conf >/dev/null <<'JCONF'
# Managed by deploy/install-pad.sh -- overrides 40-rpi-volatile-storage.conf.
[Journal]
Storage=persistent
Compress=yes
SystemMaxUse=64M
SystemMaxFileSize=8M
SystemMaxFiles=8
RuntimeMaxUse=16M
JCONF
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo systemctl restart systemd-journald
sudo journalctl --flush
# Verify rather than assume -- this is the step that silently failed before.
if journalctl --header 2>/dev/null | grep -q 'File path: /var/log/journal'; then
  echo "    persistent journal active (/var/log/journal)"
else
  echo "    WARNING: journal is still volatile — crash logs will NOT survive a reboot." >&2
  echo "    Check: systemd-analyze cat-config systemd/journald.conf | grep -i '^Storage='" >&2
fi

# Wi-Fi power-save OFF. The Pi Zero 2 W's brcmfmac drops off the network (and can
# wedge) when power-save idles the radio — a healthy, idle box just goes
# unreachable. `2` = disabled in NetworkManager; also set it live on wlan0.
echo "==> disabling Wi-Fi power-save (brcmfmac idle-drop fix)"
sudo cp "$REPO/deploy/wifi-powersave-off.conf" /etc/NetworkManager/conf.d/wifi-powersave-off.conf
sudo iw dev wlan0 set power_save off 2>/dev/null || true

# earlyoom: kill the worst hog BEFORE the kernel thrashes to a freeze. The
# bridge (its likely target) has Restart=on-failure, so it self-recovers.
if ! command -v earlyoom >/dev/null 2>&1; then
  sudo apt-get update -qq && sudo apt-get install -y earlyoom
fi
sudo systemctl enable --now earlyoom

# Prefer OOM-kill over grinding the SD card to a halt on swap.
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-pad-swappiness.conf >/dev/null
sudo sysctl -p /etc/sysctl.d/99-pad-swappiness.conf >/dev/null

# 7. Health sampler --------------------------------------------------------
# 5-min CSV samples (memory/swap trend + Pi under-voltage) kept 30 days, so an
# intermittent freeze or a power/brownout issue is diagnosable after the fact
# instead of leaving us blind like the first three freezes did.
echo "==> installing pad health sampler (5-min samples, 30-day CSV)"
sudo install -m 0755 "$REPO/deploy/pad-healthlog.sh" /usr/local/bin/pad-healthlog.sh
sudo cp "$REPO/deploy/pool-healthlog.service" /etc/systemd/system/
sudo cp "$REPO/deploy/pool-healthlog.timer"   /etc/systemd/system/
sudo cp "$REPO/deploy/pad-health.logrotate"   /etc/logrotate.d/pad-health
sudo systemctl daemon-reload
sudo systemctl enable --now pool-healthlog.timer
sudo systemctl start pool-healthlog.service   # write the first row now

# 8. Network watchdog ------------------------------------------------------
# The Pi Zero 2 W's brcmfmac Wi-Fi can crash (radio dead, CPU alive) and not
# rejoin without a reboot — the box stays up but goes network-dark, which used
# to mean a physical power-cycle. This watchdog detects that and self-recovers
# (restart Wi-Fi -> reload module -> reboot), so it heals unattended.
echo "==> installing network watchdog (self-heal crashed Wi-Fi)"
sudo install -m 0755 "$REPO/deploy/pad-netwatch.sh" /usr/local/bin/pad-netwatch.sh
sudo cp "$REPO/deploy/pool-netwatch.service" /etc/systemd/system/
sudo cp "$REPO/deploy/pool-netwatch.timer"   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pool-netwatch.timer

# 9. Bridge serial watchdog ------------------------------------------------
# Covers the case pad-netwatch can't: the daemon is up + reachable but its
# SERIAL link to the panel dropped (USB re-enumeration / loose cable), which
# strands the cockpit at "no panel". Restarts pool-bridge to reopen the port.
echo "==> installing bridge serial watchdog (reopen serial on drop)"
sudo install -m 0755 "$REPO/deploy/pad-serialwatch.sh" /usr/local/bin/pad-serialwatch.sh
sudo cp "$REPO/deploy/pool-serialwatch.service" /etc/systemd/system/
sudo cp "$REPO/deploy/pool-serialwatch.timer"   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pool-serialwatch.timer

echo
echo "==================================================================="
echo " Pad bridge installed. Bound: $LISTEN"
if [ -n "$TOKEN" ]; then
  echo " Auth: bearer token (already in $ENV_FILE — not shown here)."
  echo "       Ensure the SAME token is set in the hop-side sidecar config."
else
  echo " Auth: Tailscale ACL (no shared secret). Restrict src -> pool:8899"
  echo "       in the Tailscale admin console — see deploy/README-PAD.md."
fi
echo " Health: curl -s http://${TAILNET_IP:-<tailnet-ip>}:8899/health"
echo "==================================================================="
