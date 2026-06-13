#!/usr/bin/env bash
# install.sh — Install and register the AquaPlus/ProLogic sidecar service
#
# Usage:
#   sudo bash install.sh --bridge-host 192.168.50.XXX [--bridge-port 8899] [--api-port 5757]
#
# Prerequisites: Python 3.8+, pip3, systemd

set -euo pipefail

BRIDGE_HOST=""
BRIDGE_PORT="8899"
API_PORT="5757"
SERVICE_USER="$(logname 2>/dev/null || echo homebridge)"
INSTALL_DIR="/opt/pool-sidecar"
SERVICE_NAME="pool-sidecar"

usage() {
  echo "Usage: sudo bash install.sh --bridge-host <IP> [--bridge-port 8899] [--api-port 5757] [--user homebridge]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --bridge-host) BRIDGE_HOST="$2"; shift 2 ;;
    --bridge-port) BRIDGE_PORT="$2"; shift 2 ;;
    --api-port)    API_PORT="$2";    shift 2 ;;
    --user)        SERVICE_USER="$2"; shift 2 ;;
    *) usage ;;
  esac
done

[[ -z "$BRIDGE_HOST" ]] && { echo "ERROR: --bridge-host is required"; usage; }
[[ $EUID -ne 0 ]] && { echo "ERROR: run with sudo"; exit 1; }

echo "==> Installing Python dependencies..."
python3 -m pip install --quiet --upgrade aqualogic flask

echo "==> Creating install directory at $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp "$(dirname "$0")/pool_service.py" "$INSTALL_DIR/pool_service.py"
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
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/pool_service.py \\
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
