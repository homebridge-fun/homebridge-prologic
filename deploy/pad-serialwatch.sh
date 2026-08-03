#!/usr/bin/env bash
#
# pad-serialwatch.sh — restart the bridge if its SERIAL link to the panel drops.
#
# The daemon's HTTP API can be up (/health ok:true) while its serial connection
# to the panel is down (connected:false) — e.g. a USB re-enumeration or a loose
# cable. The daemon doesn't always reopen /dev/ttyUSB0 on its own, so a serial
# drop strands the cockpit at "no panel" until a manual restart. This detects a
# SUSTAINED connected:false (daemon up, serial down) and restarts pool-bridge to
# reopen the port. Network drops are handled separately by pad-netwatch.
#
# Driven by pool-serialwatch.timer every 1 min; acts after 2 consecutive misses
# (~2 min) so a brief reconnect blip — or a panel power-cycle's own boot/prime
# window — doesn't trigger an unnecessary restart. A single good check resets.
set -uo pipefail

STATE=/run/pad-serialwatch.fails

# The daemon binds the tailnet IP, not loopback — health check must hit that
# address. Prefer the env file's bind address; fall back to the tailnet IP.
LISTEN="$(grep -oP '^RS485_BRIDGE_LISTEN=\K.*' /etc/pool-bridge.env 2>/dev/null)"
if [ -z "${LISTEN:-}" ]; then
  ip="$(tailscale ip -4 2>/dev/null | head -1)"
  LISTEN="${ip:-127.0.0.1}:8899"
fi

resp="$(curl -sS -m5 "http://${LISTEN}/health" 2>/dev/null || true)"

# Daemon not answering at all → let systemd's Restart= handle the process; this
# watchdog only covers the "up but serial-dead" case. Reset and bail.
echo "$resp" | grep -q '"ok" *: *true' || { echo 0 >"$STATE" 2>/dev/null || true; exit 0; }

# Serial link healthy → clear the counter.
if echo "$resp" | grep -q '"connected" *: *true'; then
  echo 0 >"$STATE" 2>/dev/null || true
  exit 0
fi

# Daemon up but serial down.
fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" >"$STATE" 2>/dev/null || true
logger -t pad-serialwatch "bridge serial down (connected=false) while daemon up — miss #$fails"

if [ "$fails" -ge 2 ]; then
  logger -t pad-serialwatch "restarting pool-bridge to reopen the serial port"
  systemctl restart pool-bridge || true
  echo 0 >"$STATE" 2>/dev/null || true
fi
