#!/usr/bin/env bash
#
# pad-netwatch.sh — self-healing network watchdog for the pad Pi.
#
# The Pi Zero 2 W's brcmfmac Wi-Fi firmware can CRASH: the radio dies and won't
# rejoin, but the CPU keeps running (the health sampler logs right through it).
# Weak signal makes it frequent. Because the box is alive, it can recover itself
# instead of needing a physical power-cycle — that's what this does.
#
# Driven by pad-netwatch.timer every 1 min. Escalates:
#   3 consecutive misses (~3 min) -> restart NetworkManager (bounces Wi-Fi)
#   5 consecutive misses          -> reload the brcmfmac module (recovers a
#                                     crashed firmware without a full reboot)
#   7 consecutive misses (~7 min) -> reboot (last resort; the bridge auto-starts)
# A single good check resets the counter, so brief blips don't trigger anything.
#
# "Reachable" = gateway OR a public IP responds, so it works even if the guest
# network blocks ICMP to the gateway (it still has external access).
set -uo pipefail

STATE=/run/pad-netwatch.fails
GW="$(ip route 2>/dev/null | awk '/default/{print $3; exit}')"
PUB="${PAD_NETWATCH_PUBLIC:-1.1.1.1}"

reachable() {
  [ -n "$GW" ] && ping -c1 -W2 "$GW"  >/dev/null 2>&1 && return 0
  ping -c1 -W2 "$PUB" >/dev/null 2>&1 && return 0
  return 1
}

if reachable; then
  echo 0 >"$STATE" 2>/dev/null || true
  exit 0
fi

fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" >"$STATE" 2>/dev/null || true
logger -t pad-netwatch "network unreachable (gw=${GW:-none}) — consecutive miss #$fails"

if [ "$fails" -eq 3 ]; then
  logger -t pad-netwatch "restarting NetworkManager to recover Wi-Fi"
  systemctl restart NetworkManager || true
elif [ "$fails" -eq 5 ]; then
  logger -t pad-netwatch "reloading brcmfmac (crashed-firmware recovery)"
  modprobe -r brcmfmac 2>/dev/null || true
  sleep 2
  modprobe brcmfmac 2>/dev/null || true
elif [ "$fails" -ge 7 ]; then
  logger -t pad-netwatch "still down after Wi-Fi restart + module reload — rebooting"
  systemctl reboot
fi
