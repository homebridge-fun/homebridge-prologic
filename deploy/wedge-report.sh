#!/usr/bin/env bash
#
# wedge-report.sh — summarize AquaConnect command-path wedge episodes from the
# pool-sidecar journal.
#
# A "wedge" here means the line `Bridge command path wedged` (or `appears
# wedged`), which is exactly when the sidecar sets bridge_wedged=True — i.e.
# when the HomeKit "Bridge Needs Rebooting" sensor flips on and your auto
# power-cycle plug fires. Each episode is paired with the next
# `Bridge command path recovered` line and its duration is shown, so you can
# tell a quick power-cycle recovery from a long lockup.
#
# Soft "Heater ... not confirmed" write failures are counted separately: those
# do NOT set the sensor and would NOT trigger the plug.
#
# Usage:
#   ./deploy/wedge-report.sh                 # all history in the journal
#   ./deploy/wedge-report.sh "2026-06-21"    # only since a date/time
set -euo pipefail

SINCE="${1:-}"
JARGS=(-u pool-sidecar --no-pager -o short-iso)
[ -n "$SINCE" ] && JARGS+=(--since "$SINCE")

LINES="$(journalctl "${JARGS[@]}" 2>/dev/null || true)"
if [ -z "$LINES" ]; then
	echo "No pool-sidecar journal entries found${SINCE:+ since $SINCE}." >&2
	exit 0
fi

echo "=== Wedge episodes (sensor fired → auto power-cycle trigger) ==="
# Pair each wedge transition with the next recovery. Collapse repeated wedge
# lines (the canary re-fires every ~40s while stuck) into one episode.
printf '%s\n' "$LINES" | awk '
	/Bridge command path (wedged|appears wedged)/ {
		if (!inw) { inw=1; start=$1 }
		next
	}
	/Bridge command path recovered/ {
		if (inw) { print start"\t"$1; inw=0 }
		next
	}
	END { if (inw) print start"\t-" }
' | while IFS=$'\t' read -r s r; do
	day="${s%%T*}"
	if [ "$r" = "-" ]; then
		printf '  %s  ->  (no recovery logged — still wedged?)\n' "$s"
		echo "$day" >> /tmp/.wedge_days.$$
		continue
	fi
	ss=$(date -d "$s" +%s 2>/dev/null || echo 0)
	rr=$(date -d "$r" +%s 2>/dev/null || echo 0)
	dur=$(( rr - ss ))
	# The AquaConnect takes >~60s to reboot, so a recovery faster than that
	# can't be a completed power-cycle — it self-healed. Flag accordingly.
	if [ "$dur" -lt "${REBOOT_MIN_S:-60}" ]; then
		tag="self-healed (too fast for a reboot)"
	else
		tag="possibly the auto power-cycle"
	fi
	printf '  %s  ->  recovered %s   (%ss — %s)\n' "$s" "$r" "$dur" "$tag"
	echo "$day" >> /tmp/.wedge_days.$$
done

echo
echo "=== Per-day sensor-firing wedge count ==="
if [ -f /tmp/.wedge_days.$$ ]; then
	sort /tmp/.wedge_days.$$ | uniq -c | awk '{printf "  %s: %d\n", $2, $1}'
	total=$(wc -l < /tmp/.wedge_days.$$)
	rm -f /tmp/.wedge_days.$$
	echo "  ----"
	echo "  total: $total"
else
	echo "  none"
fi

echo
echo "=== Soft write failures (NOT sensor/plug — self-probed) ==="
soft=$(printf '%s\n' "$LINES" | grep -cE "not confirmed" || true)
echo "  '... not confirmed' lines: $soft"
printf '%s\n' "$LINES" | grep -E "not confirmed" | sed 's/^/  /' || true
