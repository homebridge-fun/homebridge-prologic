#!/usr/bin/env bash
#
# set-cockpit-password.sh — set the cockpit's Caddy Basic-auth credential.
#
# Hashes a password with `caddy hash-password`, writes it into the Caddyfile's
# basic_auth block, validates, and reloads Caddy — in one step, so you don't
# have to hand-run hash-password + edit the file + reload.
#
# Usage (run with sudo — it edits /etc/caddy and reloads the service):
#     sudo ./deploy/set-cockpit-password.sh [username]
#
# username defaults to 'greg'. The password is prompted for (hidden) so it
# never lands in your shell history.
#
# Override the Caddyfile path with CADDYFILE=/path if it isn't the default.
set -euo pipefail

CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"
USER_NAME="${1:-greg}"

if [[ $EUID -ne 0 ]]; then
	echo "This edits ${CADDYFILE} and reloads Caddy — re-run with sudo:" >&2
	echo "    sudo $0 ${1:-}" >&2
	exit 1
fi

if ! command -v caddy >/dev/null 2>&1; then
	echo "caddy not found in PATH. Install it first (see deploy/CADDY.md)." >&2
	exit 1
fi

if [[ ! -f "$CADDYFILE" ]]; then
	echo "Caddyfile not found at ${CADDYFILE}." >&2
	echo "Copy deploy/Caddyfile there first, then re-run." >&2
	exit 1
fi

# Prompt for the password twice (hidden), confirm they match.
read -r -s -p "New password for '${USER_NAME}': " PASS; echo
read -r -s -p "Confirm password: " PASS2; echo
if [[ "$PASS" != "$PASS2" ]]; then
	echo "Passwords did not match — nothing changed." >&2
	exit 1
fi
if [[ -z "$PASS" ]]; then
	echo "Empty password — nothing changed." >&2
	exit 1
fi

HASH="$(caddy hash-password --plaintext "$PASS")"

# Replace the single credential line inside basic_auth. We match a line that
# carries a bcrypt hash ($2a$/$2b$/$2y$) and is NOT a comment, so the example
# hash in the file's comments is left alone. Use awk (not sed) so the hash —
# which is full of $, / and . — is never interpreted as a replacement pattern.
TMP="$(mktemp)"
awk -v user="$USER_NAME" -v hash="$HASH" '
	/[$]2[aby][$]/ && $0 !~ /^[[:space:]]*#/ { print "\t\t" user " " hash; next }
	{ print }
' "$CADDYFILE" > "$TMP"

if ! grep -q "$HASH" "$TMP"; then
	rm -f "$TMP"
	echo "Could not find a credential line to update in ${CADDYFILE}." >&2
	echo "Expected a 'username \$2a\$...' line inside a basic_auth block." >&2
	exit 1
fi

# Validate before we overwrite the live file.
if ! caddy validate --adapter caddyfile --config "$TMP" >/dev/null 2>&1; then
	echo "New Caddyfile failed validation — leaving the current one in place:" >&2
	caddy validate --adapter caddyfile --config "$TMP" || true
	rm -f "$TMP"
	exit 1
fi

cp "$CADDYFILE" "${CADDYFILE}.bak"
mv "$TMP" "$CADDYFILE"
chown root:caddy "$CADDYFILE" 2>/dev/null || true
chmod 0640 "$CADDYFILE" 2>/dev/null || true

systemctl reload caddy

# Confirm auth actually works against the live server. 200 (cockpit up) or 502
# (cockpit down but auth accepted) both mean the credential is good; 401 means
# it isn't.
CODE="$(curl -s -o /dev/null -w '%{http_code}' -u "${USER_NAME}:${PASS}" http://127.0.0.1/ || echo "000")"
case "$CODE" in
	200|502) echo "✓ Password updated for '${USER_NAME}'. (backup: ${CADDYFILE}.bak)" ;;
	401)     echo "⚠ Reloaded, but a test login returned 401 — the credential may not have applied. Check ${CADDYFILE}." >&2; exit 1 ;;
	*)       echo "✓ Password updated for '${USER_NAME}'. (auth test got HTTP ${CODE}; verify in a browser.)" ;;
esac
