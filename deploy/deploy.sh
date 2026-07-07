#!/usr/bin/env bash
#
# deploy.sh — one command to deploy the latest changes on the Pi, so you don't
# have to paste a multi-line block (handy from the iPhone Homebridge terminal).
#
#   ./deploy/deploy.sh          # pull + sidecar + web, restart sidecar (default)
#   ./deploy/deploy.sh full     # also rebuild the plugin + restart Homebridge
#   ./deploy/deploy.sh web      # cockpit HTML only (no restarts)
#
# Run it from anywhere; it cd's to the checkout itself.
set -e

REPO=/home/greg/development/homebridge-prologic
BRANCH=claude/gracious-planck-1yz8v9
MODE="${1:-sidecar}"

cd "$REPO"
echo "→ pulling $BRANCH"
git pull --rebase origin "$BRANCH"

# Cockpit HTML (always safe, no restart needed)
sudo cp sidecar/web/index.html /opt/pool-sidecar/web/index.html
echo "→ cockpit HTML updated"

if [ "$MODE" = "web" ]; then
	echo "✓ web-only deploy done"
	exit 0
fi

# Sidecar
sudo cp sidecar/pool_service.py /opt/pool-sidecar/pool_service.py
sudo systemctl restart pool-sidecar
echo "→ sidecar updated + restarted"

if [ "$MODE" = "full" ]; then
	echo "→ building plugin"
	npm run build
	echo "→ restarting Homebridge (this ends the terminal session; that's expected)"
	sudo systemctl restart homebridge
fi

echo "✓ deploy done ($MODE)"
