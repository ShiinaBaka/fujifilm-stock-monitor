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

echo "已移除程序文件。"
echo "配置仍保留在：$CONFIG_DIR"
echo "如果不再需要状态和通知密钥，请手动删除该目录。"
