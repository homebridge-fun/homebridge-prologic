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
#   3. Clone this repo to $HOME:        git clone <repo-url> ~/homebridge-prologic
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
sudo udevadm trigger --attr-match=subsystem=usb-serial || true
if [ -e "/sys/bus/usb-serial/devices/$(basename "$PORT")/latency_timer" ]; then
  echo -n "    latency_timer now: "
  cat "/sys/bus/usb-serial/devices/$(basename "$PORT")/latency_timer"
fi

# 4. Environment file (token + bind address) ------------------------------
# Tailnet IP is stable across networks; discover it so a re-image picks up the
# node's current address automatically.
TAILNET_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
LISTEN="${TAILNET_IP:-0.0.0.0}:8899"

if sudo test -f "$ENV_FILE"; then
  echo "==> keeping existing $ENV_FILE (token preserved)"
  # Refresh the bind/port lines in case the tailnet IP changed, keep the token.
  TOKEN="$(sudo grep -oP '^RS485_BRIDGE_TOKEN=\K.*' "$ENV_FILE" || true)"
else
  echo "==> generating new token + writing $ENV_FILE"
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
fi

sudo tee "$ENV_FILE" >/dev/null <<EOF
# RS-485 pad bridge config. Holds the shared secret — do NOT commit.
RS485_BRIDGE_TOKEN=$TOKEN
RS485_BRIDGE_LISTEN=$LISTEN
RS485_BRIDGE_PORT=$PORT
EOF
sudo chmod 600 "$ENV_FILE"

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

echo
echo "==================================================================="
echo " Pad bridge installed. Bound: $LISTEN"
echo " TOKEN (put this in the Homebridge plugin / sidecar config):"
echo "     $TOKEN"
echo " Health: curl -s http://${TAILNET_IP:-<tailnet-ip>}:8899/health"
echo "==================================================================="
