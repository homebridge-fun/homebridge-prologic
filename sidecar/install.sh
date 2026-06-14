#!/usr/bin/env bash
# install.sh — Install and register the AquaPlus/ProLogic sidecar service
#
# Usage:
#   sudo bash install.sh --bridge-host 192.168.50.XXX [--bridge-port 8899] [--api-port 5757]
#
# Prerequisites: Python 3.9+ (with python3-venv), systemd
#
# On Raspberry Pi OS Bookworm and other PEP 668 "externally-managed" systems,
# system-wide `pip install` is blocked, so this installs into a dedicated
# virtualenv at ${INSTALL_DIR}/venv.

set -euo pipefail

BRIDGE_HOST=""
BRIDGE_PORT="8899"
API_PORT="5757"
SERVICE_USER="$(logname 2>/dev/null || echo homebridge)"
INSTALL_DIR="/opt/pool-sidecar"
SERVICE_NAME="pool-sidecar"
DRY_RUN=0

usage() {
  echo "Usage: sudo bash install.sh --bridge-host <IP> [--bridge-port 8899] [--api-port 5757] [--user homebridge] [--dry-run]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --bridge-host) BRIDGE_HOST="$2"; shift 2 ;;
    --bridge-port) BRIDGE_PORT="$2"; shift 2 ;;
    --api-port)    API_PORT="$2";    shift 2 ;;
    --user)        SERVICE_USER="$2"; shift 2 ;;
    --dry-run)     DRY_RUN=1; shift ;;
    *) usage ;;
  esac
done

# --dry-run lets you generate and inspect the systemd unit before you have the
# bridge wired up. It uses a placeholder host if none was given and skips all
# privileged/network steps.
if [[ "$DRY_RUN" -eq 1 ]]; then
  BRIDGE_HOST="${BRIDGE_HOST:-192.168.50.XXX}"
  echo "==> DRY RUN — no changes will be made."
  echo "==> Would create venv at ${INSTALL_DIR}/venv and install: aqualogic, flask"
  echo "==> Would install pool_service.py to ${INSTALL_DIR}/pool_service.py"
  echo "==> systemd unit that would be written to /etc/systemd/system/${SERVICE_NAME}.service:"
  echo "----------------------------------------------------------------------"
  cat <<EOF
[Unit]
Description=AquaPlus/ProLogic RS-485 sidecar (aqualogic)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/pool_service.py \\
    --host ${BRIDGE_HOST} \\
    --port ${BRIDGE_PORT} \\
    --api-port ${API_PORT}
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  echo "----------------------------------------------------------------------"
  exit 0
fi

[[ -z "$BRIDGE_HOST" ]] && { echo "ERROR: --bridge-host is required"; usage; }
[[ $EUID -ne 0 ]] && { echo "ERROR: run with sudo"; exit 1; }

echo "==> Creating install directory at $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp "$(dirname "$0")/pool_service.py" "$INSTALL_DIR/pool_service.py"
cp "$(dirname "$0")/requirements.txt" "$INSTALL_DIR/requirements.txt" 2>/dev/null || true

echo "==> Creating Python virtualenv at $INSTALL_DIR/venv..."
if ! python3 -m venv "$INSTALL_DIR/venv"; then
  echo "ERROR: failed to create venv. On Debian/Raspberry Pi OS, install the"
  echo "       venv package first:  sudo apt-get install -y python3-venv"
  exit 1
fi

echo "==> Installing Python dependencies into the venv..."
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade aqualogic flask

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "==> Writing systemd unit /etc/systemd/system/${SERVICE_NAME}.service..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=AquaPlus/ProLogic RS-485 sidecar (aqualogic)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/pool_service.py \\
    --host ${BRIDGE_HOST} \\
    --port ${BRIDGE_PORT} \\
    --api-port ${API_PORT}
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "==> Enabling and starting ${SERVICE_NAME}..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo ""
echo "Done! Check status with:"
echo "  sudo systemctl status ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "Sidecar REST API will be at http://127.0.0.1:${API_PORT}/status"
