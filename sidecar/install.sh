#!/usr/bin/env bash
# install.sh — install and register the ProLogic/AquaLogic sidecar service on the
# Homebridge HOST. The sidecar is the Python REST service the plugin (and the web
# cockpit) talk to; it reaches the panel through one of two backends:
#
#   AquaConnect (local HTTP to the AquaConnect box):
#     sudo bash install.sh --backend aquaconnect --aquaconnect-host <aquaconnect-ip>
#
#   RS-485 pad bridge (a Raspberry Pi at the pad running deploy/install-pad.sh,
#   reached over Tailscale — set that up first, see deploy/README-PAD.md):
#     sudo bash install.sh --backend rs485bridge --rs485bridge-host <pad-tailnet-ip> \
#         [--rs485bridge-token <token>]
#
# Common options: [--api-port 5757] [--user homebridge] [--dry-run]
#
# Prereqs: Python 3.9+ (with python3-venv) and systemd. On PEP 668
# "externally-managed" systems (Raspberry Pi OS Bookworm, recent Debian) a
# system-wide pip install is blocked, so this installs into a dedicated venv at
# ${INSTALL_DIR}/venv. The host sidecar's only dependency is Flask — the
# aqualogic RS-485 decode lives on the pad bridge, not here.

set -euo pipefail

BACKEND=""
AQUACONNECT_HOST=""
RS485BRIDGE_HOST=""
RS485BRIDGE_PORT="8899"
RS485BRIDGE_TOKEN=""
API_PORT="5757"
SERVICE_USER="$(logname 2>/dev/null || echo homebridge)"
INSTALL_DIR="/opt/pool-sidecar"
SERVICE_NAME="pool-sidecar"
DRY_RUN=0

usage() {
  cat >&2 <<'EOF'
Usage:
  sudo bash install.sh --backend aquaconnect  --aquaconnect-host <IP> [opts]
  sudo bash install.sh --backend rs485bridge  --rs485bridge-host <IP> [--rs485bridge-port 8899]
                                              [--rs485bridge-token <tok>] [opts]
  opts: [--api-port 5757] [--user <systemd-user>] [--dry-run]
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --backend)            BACKEND="$2"; shift 2 ;;
    --aquaconnect-host)   AQUACONNECT_HOST="$2"; shift 2 ;;
    --rs485bridge-host)   RS485BRIDGE_HOST="$2"; shift 2 ;;
    --rs485bridge-port)   RS485BRIDGE_PORT="$2"; shift 2 ;;
    --rs485bridge-token)  RS485BRIDGE_TOKEN="$2"; shift 2 ;;
    --api-port)           API_PORT="$2"; shift 2 ;;
    --user)               SERVICE_USER="$2"; shift 2 ;;
    --dry-run)            DRY_RUN=1; shift ;;
    -h|--help)            usage ;;
    *) echo "Unknown arg: $1" >&2; usage ;;
  esac
done

# Validate backend + its required host, and build the ExecStart args + any env.
case "$BACKEND" in
  aquaconnect)
    [[ -z "$AQUACONNECT_HOST" ]] && { echo "ERROR: --aquaconnect-host is required for --backend aquaconnect" >&2; usage; }
    EXEC_ARGS="--backend aquaconnect --aquaconnect-host ${AQUACONNECT_HOST} --api-port ${API_PORT}"
    ENV_LINE=""
    ;;
  rs485bridge)
    [[ -z "$RS485BRIDGE_HOST" ]] && { echo "ERROR: --rs485bridge-host is required for --backend rs485bridge" >&2; usage; }
    EXEC_ARGS="--backend rs485bridge --rs485bridge-host ${RS485BRIDGE_HOST} --rs485bridge-port ${RS485BRIDGE_PORT} --api-port ${API_PORT}"
    # Token is passed via the environment (never on the command line / in ps).
    ENV_LINE=""
    [[ -n "$RS485BRIDGE_TOKEN" ]] && ENV_LINE="Environment=RS485_BRIDGE_TOKEN=${RS485BRIDGE_TOKEN}"
    ;;
  *)
    echo "ERROR: --backend must be 'aquaconnect' or 'rs485bridge'" >&2
    usage
    ;;
esac

make_unit() {
  cat <<EOF
[Unit]
Description=ProLogic/AquaLogic pool sidecar (${BACKEND})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
${ENV_LINE}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/pool_service.py ${EXEC_ARGS}
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "==> DRY RUN — no changes will be made."
  echo "==> Would install pool_service.py + web/ to ${INSTALL_DIR}, venv with Flask."
  echo "==> systemd unit /etc/systemd/system/${SERVICE_NAME}.service:"
  echo "----------------------------------------------------------------------"
  make_unit
  echo "----------------------------------------------------------------------"
  exit 0
fi

[[ $EUID -ne 0 ]] && { echo "ERROR: run with sudo" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing to ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR"
cp "$SRC/pool_service.py" "$INSTALL_DIR/pool_service.py"
cp "$SRC/requirements.txt" "$INSTALL_DIR/requirements.txt" 2>/dev/null || true
# The sidecar serves the web cockpit from web/index.html — ship it too.
mkdir -p "$INSTALL_DIR/web"
cp -r "$SRC/web/." "$INSTALL_DIR/web/"

echo "==> Creating Python virtualenv at ${INSTALL_DIR}/venv..."
if ! python3 -m venv "$INSTALL_DIR/venv"; then
  echo "ERROR: could not create venv. On Debian/Raspberry Pi OS install it first:" >&2
  echo "       sudo apt-get install -y python3-venv" >&2
  exit 1
fi

echo "==> Installing dependencies (Flask)..."
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade flask

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "==> Writing systemd unit /etc/systemd/system/${SERVICE_NAME}.service..."
make_unit > "/etc/systemd/system/${SERVICE_NAME}.service"

echo "==> Enabling and starting ${SERVICE_NAME}..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo ""
echo "Done (backend: ${BACKEND}). Check it with:"
echo "  sudo systemctl status ${SERVICE_NAME}"
echo "  curl http://127.0.0.1:${API_PORT}/status"
echo "  journalctl -u ${SERVICE_NAME} -f"
