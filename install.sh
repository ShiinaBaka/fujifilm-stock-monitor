#!/usr/bin/env bash
set -euo pipefail

APP_NAME="fujifilm-stock-monitor"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/share/$APP_NAME}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/.config/$APP_NAME}"
SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
SERVICE_NAME="$APP_NAME.service"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

need_cmd python3
need_cmd systemctl

mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$SYSTEMD_USER_DIR"
install -m 0755 "$SCRIPT_DIR/fujifilm_stock_monitor.py" "$INSTALL_DIR/fujifilm_stock_monitor.py"
install -m 0755 "$SCRIPT_DIR/run.sh" "$INSTALL_DIR/run.sh"
install -m 0755 "$SCRIPT_DIR/resume.sh" "$INSTALL_DIR/resume.sh"

if [ ! -f "$CONFIG_DIR/env" ]; then
  install -m 0600 "$SCRIPT_DIR/env.example" "$CONFIG_DIR/env"
  echo "Created config: $CONFIG_DIR/env"
  echo "Edit it before starting if you want ServerChan/ntfy/webhook notifications."
fi

sed \
  -e "s#__INSTALL_DIR__#$INSTALL_DIR#g" \
  -e "s#__CONFIG_DIR__#$CONFIG_DIR#g" \
  "$SCRIPT_DIR/fujifilm-stock-monitor.service.template" \
  > "$SYSTEMD_USER_DIR/$SERVICE_NAME"

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"

if command -v loginctl >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
  sudo loginctl enable-linger "$USER" >/dev/null 2>&1 || true
fi

echo
echo "Installed $APP_NAME."
echo
echo "Run one check:"
echo "  $INSTALL_DIR/fujifilm_stock_monitor.py --once --print-products --ipv4"
echo
echo "Start service:"
echo "  systemctl --user start $SERVICE_NAME"
echo
echo "View logs:"
echo "  journalctl --user -u $SERVICE_NAME -f"
echo
echo "Resume after missing a restock:"
echo "  $INSTALL_DIR/resume.sh"
