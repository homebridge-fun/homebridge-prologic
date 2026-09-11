#!/usr/bin/env bash
#
# hop-dnswatch.sh — keep the hop able to RESOLVE the pad bridge.
#
# The pad has pad-netwatch and pad-serialwatch; the hop had nothing. It cost a
# 45-hour outage: Tailscale stayed connected and every peer was reachable, but
# `--accept-dns` had been switched off on the hop — most likely something
# rewriting /etc/resolv.conf, which Tailscale manages directly here rather than
# through systemd-resolved. MagicDNS stopped resolving, the sidecar could not
# look up the bridge host at all, and the only signal was a cockpit banner
# nobody was watching. `tailscale status` looked perfect throughout, which is
# exactly why this went unnoticed: connectivity was never the problem.
#
# So this checks the one thing that actually broke — can we resolve and reach
# the configured bridge host — and repairs the known cause.
#
# The host is READ FROM THE SIDECAR'S CONFIG, never hardcoded. That is the
# whole point: the pad is named in the Homebridge UI (`rs485bridgeHost`), which
# reaches the sidecar via POST /backend and lands in backend.json. A literal
# here would be one more install-time value that goes stale, which is the
# family of bug this project has already hit twice (the pad's frozen bind
# address, and a hostname baked into the docs).
#
# Driven by pool-dnswatch.timer every 5 min. Acts after 2 consecutive misses so
# a momentary blip doesn't trigger a repair. A single good check resets.
set -uo pipefail

STATE=/run/hop-dnswatch.fails
CONF="${SIDECAR_CONFIG:-/opt/pool-sidecar/backend.json}"

# Only meaningful for the rs485bridge backend; the aquaconnect path is a LAN
# IP and never involves MagicDNS.
backend="$(python3 -c "
import json,sys
try:
    print(json.load(open('$CONF')).get('backend',''))
except Exception:
    print('')
" 2>/dev/null)"
[ "$backend" = "rs485bridge" ] || exit 0

host="$(python3 -c "
import json,sys
try:
    print(json.load(open('$CONF')).get('rs485bridge_host','') or '')
except Exception:
    print('')
" 2>/dev/null)"
[ -n "$host" ] || exit 0

# A configured IP address needs no DNS; nothing here applies.
case "$host" in
  [0-9]*.[0-9]*.[0-9]*.[0-9]*) exit 0 ;;
esac

port="$(python3 -c "
import json
try:
    print(json.load(open('$CONF')).get('rs485bridge_port',8899))
except Exception:
    print(8899)
" 2>/dev/null)"
port="${port:-8899}"

# --- drift: does the effective host still match what the UI declares? --------
#
# backend.json is the EFFECTIVE value and the right thing to test above -- it
# is what the sidecar actually dials. The Homebridge UI's `rs485bridgeHost` is
# the DECLARED value, and the two only reconcile when Homebridge restarts
# (platform.ts reconcileBackend -> POST /backend). backend.json is also
# writable by the cockpit and by hand, so the UI can say one thing while the
# sidecar has been using another for weeks.
#
# Reported, never repaired: the plugin owns that value and will push it on its
# next restart. Silently rewriting the running config from a watchdog would be
# a worse bug than the drift.
HB_CONF="${HOMEBRIDGE_CONFIG:-/var/lib/homebridge/config.json}"
if [ -r "$HB_CONF" ]; then
  declared="$(python3 -c "
import json
try:
    cfg = json.load(open('$HB_CONF'))
    for pl in cfg.get('platforms', []):
        if pl.get('backend') == 'rs485bridge' and pl.get('rs485bridgeHost'):
            print(pl['rs485bridgeHost']); break
except Exception:
    pass
" 2>/dev/null)"
  if [ -n "$declared" ] && [ "$declared" != "$host" ]; then
    logger -t hop-dnswatch "config drift: Homebridge declares '${declared}' but the sidecar is using '${host}' — restart homebridge to reconcile, or fix the UI"
  fi
fi

ok=0
if getent hosts "$host" >/dev/null 2>&1; then
  # Resolution alone is not enough: a stale answer resolves fine and goes
  # nowhere. Reaching /health is the real check.
  curl -sS -m 8 "http://${host}:${port}/health" 2>/dev/null | grep -q '"ok"' && ok=1
fi

if [ "$ok" = 1 ]; then
  echo 0 >"$STATE" 2>/dev/null || true
  exit 0
fi

fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" >"$STATE" 2>/dev/null || true
logger -t hop-dnswatch "cannot resolve/reach bridge host '${host}:${port}' — miss #$fails"

[ "$fails" -ge 2 ] || exit 0

# Repair only the cause we have actually seen, and only when the evidence fits:
# resolution failing while Tailscale itself is up. If the name resolves and the
# host still isn't answering, the fault is at the pad and re-applying DNS
# settings would be noise.
if getent hosts "$host" >/dev/null 2>&1; then
  logger -t hop-dnswatch "'${host}' resolves but is not answering — not a DNS fault; leaving it to the pad-side watchdogs"
  echo 0 >"$STATE" 2>/dev/null || true
  exit 0
fi

if ! tailscale status >/dev/null 2>&1; then
  logger -t hop-dnswatch "tailscale itself is down — not repairing DNS; that is a separate fault"
  exit 0
fi

logger -t hop-dnswatch "re-enabling MagicDNS (tailscale set --accept-dns=true)"
tailscale set --accept-dns=true 2>/dev/null || \
  logger -t hop-dnswatch "could not apply --accept-dns=true (needs root?)"
sleep 3

if getent hosts "$host" >/dev/null 2>&1; then
  logger -t hop-dnswatch "repaired: '${host}' resolves again"
  # The sidecar's urllib caches nothing, but it is mid-backoff; a restart makes
  # recovery immediate rather than waiting out the poll loop.
  systemctl restart pool-sidecar 2>/dev/null || true
  echo 0 >"$STATE" 2>/dev/null || true
else
  logger -t hop-dnswatch "STILL cannot resolve '${host}' after re-enabling MagicDNS — needs a look"
fi
