#!/usr/bin/env bash
set -euo pipefail

APP_NAME="fujifilm-stock-monitor"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/share/$APP_NAME}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/.config/$APP_NAME}"
SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
SERVICE_NAME="$APP_NAME.service"

systemctl --user disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
rm -f "$SYSTEMD_USER_DIR/$SERVICE_NAME"
systemctl --user daemon-reload || true

rm -rf "$INSTALL_DIR"

echo "Removed program files."
echo "Config kept at: $CONFIG_DIR"
echo "Remove it manually if you no longer need state or notification keys."
