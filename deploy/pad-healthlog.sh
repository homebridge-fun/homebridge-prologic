#!/usr/bin/env bash
#
# pad-healthlog.sh — append one health sample (CSV row) to $PAD_HEALTH_LOG.
#
# Driven by pool-healthlog.timer every 5 min. Captures the two things that made
# the field freezes hard to diagnose: the memory/swap trend (slow leak vs. sharp
# spike), and Pi under-voltage / throttling (the fingerprint of a marginal power
# supply or a sagging pad circuit — `vcgencmd get_throttled`). Also logs the
# bridge daemon's RSS so a daemon leak would be visible separately from the OS.
#
# Runs as root (writes /var/log), so no sudo here. Kept dependency-free (procfs
# + vcgencmd) so it can't itself add memory pressure.
set -euo pipefail

LOG="${PAD_HEALTH_LOG:-/var/log/pad-health.csv}"

now_iso="$(date -Is)"
now_epoch="$(date +%s)"

# Memory + swap in MB from /proc/meminfo (values there are KB).
read -r mem_total mem_avail <<EOF
$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{printf "%d %d", t/1024, a/1024}' /proc/meminfo)
EOF
swap_used="$(awk '/^SwapTotal:/{t=$2} /^SwapFree:/{f=$2} END{printf "%d", (t-f)/1024}' /proc/meminfo)"
load1="$(awk '{print $1}' /proc/loadavg)"

# Throttle/under-voltage bitmask. bit0=under-voltage now, bit16=under-voltage
# has occurred since boot, bit2/bit18=throttled now/since boot.
thr="$(vcgencmd get_throttled 2>/dev/null | sed 's/.*=//')"; thr="${thr:-NA}"
uv_now=0; uv_ever=0
if [ "$thr" != "NA" ]; then
  n=$(( thr ))
  (( n & 1 ))        && uv_now=1
  (( (n >> 16) & 1 )) && uv_ever=1
fi
temp="$(vcgencmd measure_temp 2>/dev/null | sed 's/[^0-9.]//g')"; temp="${temp:-NA}"

# Bridge daemon footprint (sum of python3 RSS, MB) — catches a daemon-side leak.
py_rss="$(ps -C python3 -o rss= 2>/dev/null | awk '{s+=$1} END{printf "%d", s/1024}')"
py_rss="${py_rss:-0}"

# Wi-Fi signal (dBm) — the metric that matters after the plastic-box relocation.
# Lets the 30-day CSV show the signal trend and how often it dips toward -70.
wifi="$(iw dev wlan0 link 2>/dev/null | awk -F': ' '/signal/{gsub(/ ?dBm/,"",$2); gsub(/ /,"",$2); print $2}')"
wifi="${wifi:-NA}"

HEADER="iso,epoch,mem_total_mb,mem_avail_mb,swap_used_mb,load1,throttled,uv_now,uv_since_boot,soc_temp_c,py_rss_mb,wifi_dbm,journal,journal_mb"

# Header on a fresh/truncated file (logrotate copytruncate empties it).
#
# Also handles a header that predates a new column: appending wider rows under
# a narrower header would silently misalign every column-index query in
# README-PAD.md. Rather than corrupt the series, retire the old file and start
# a clean one -- the history is preserved beside it.
if [ ! -s "$LOG" ]; then
  echo "$HEADER" >"$LOG"
elif [ "$(head -n1 "$LOG")" != "$HEADER" ]; then
  mv "$LOG" "$LOG.$(date +%Y%m%d%H%M%S).oldformat"
  echo "$HEADER" >"$LOG"
fi
# Is the journal actually persisting? Raspberry Pi OS ships a drop-in forcing
# Storage=volatile, and it has silently re-won before -- leaving us with no
# crash forensics for months without anyone noticing. Recording it here means a
# regression shows up in the trend instead of being discovered during an
# incident, when it is too late to matter.
if journalctl --header 2>/dev/null | grep -q 'File path: /var/log/journal'; then
  journal="persistent"
else
  journal="VOLATILE"
fi

# How much SD the journal is actually consuming. Persisting the journal is a
# deliberate trade -- crash forensics in exchange for some card writes -- and
# this turns "is that trade still reasonable?" into a trend you can see rather
# than a question someone has to remember to ask. Capped by SystemMaxUse, so
# it should plateau; a number that keeps climbing means the cap isn't applying.
journal_mb="$(du -sm /var/log/journal 2>/dev/null | awk '{print $1}')"
journal_mb="${journal_mb:-NA}"

echo "$now_iso,$now_epoch,$mem_total,$mem_avail,$swap_used,$load1,$thr,$uv_now,$uv_ever,$temp,$py_rss,$wifi,$journal,$journal_mb" >>"$LOG"
