#!/usr/bin/env bash
# harden-pad.sh — lock the RS-485 pad Pi down to TAILNET-ONLY access.
#
# Run this AFTER the pad is on the main LAN and Tailscale is connecting DIRECT
# (verify from the hop: `tailscale ping pool` says "direct", not "via DERP").
# It sets a host firewall (ufw) whose only reachable surface is the Tailscale
# interface: SSH and the bridge API (:8899) are reachable ONLY over tailscale0;
# the LAN sees nothing. This is a tighter posture than a guest network (which
# still exposes the pad to other guest devices) and keeps the direct connection.
#
# Tailscale needs NO inbound service ports: its outbound hole-punch plus the
# firewall's default established/related rule carry the return traffic. (If, and
# only if, `tailscale ping pool` falls back to "via DERP" AFTER hardening, see
# the note at the bottom to allow UDP 41641 inbound and restore direct.)
#
# Idempotent — safe to re-run. Override interfaces with LAN_IFACE=/TS_IFACE= env.
#
# ── SAFETY ─────────────────────────────────────────────────────────────────────
# After this, SSH works ONLY over the tailnet. BEFORE running:
#   1. Confirm you can reach the pad over Tailscale — either `ssh <user>@pool`
#      over the tailnet, or enable Tailscale SSH: `sudo tailscale up --ssh`.
#   2. Ideally keep a local keyboard/monitor handy the first time.
# The script refuses to run if the Tailscale interface is missing, so it won't
# lock you out of a pad that isn't on the tailnet.

set -euo pipefail

LAN_IFACE="${LAN_IFACE:-wlan0}"
TS_IFACE="${TS_IFACE:-tailscale0}"

[[ $EUID -ne 0 ]] && { echo "ERROR: run as root (sudo)." >&2; exit 1; }

# Guard: never enable a deny-by-default firewall if Tailscale isn't up — that
# would leave the pad reachable by nothing.
if ! ip link show "$TS_IFACE" >/dev/null 2>&1; then
  echo "ERROR: interface '$TS_IFACE' not found — is Tailscale up? Aborting so" >&2
  echo "       you don't lock yourself out. Run 'sudo tailscale up' first." >&2
  exit 1
fi

if ! command -v ufw >/dev/null 2>&1; then
  echo "==> Installing ufw..."
  apt-get update -qq
  apt-get install -y ufw
fi

echo "==> Applying tailnet-only firewall (LAN=$LAN_IFACE, tailnet=$TS_IFACE)..."
ufw default deny incoming        # deny everything inbound by default
ufw default allow outgoing       # pad still reaches Tailscale/DERP, DNS, NTP, apt
ufw allow in on "$TS_IFACE"      # SSH + bridge API (:8899) — tailnet only
# DHCP client lease renewals on the LAN (this is the client port, not a service):
ufw allow in on "$LAN_IFACE" proto udp from port 67 to any port 68
ufw --force enable

echo
echo "Done. Reachable surface:"
ufw status verbose
echo
echo "Next:"
echo "  • From the HOP:  tailscale ping pool   → should still say 'direct'."
echo "  • The bridge API and SSH are now reachable ONLY over the tailnet."
echo
echo "If (and only if) 'tailscale ping pool' now shows 'via DERP', the deny-all"
echo "broke the direct hole-punch on this NAT. Restore direct with:"
echo "  sudo ufw allow in on ${LAN_IFACE} proto udp to any port 41641"
