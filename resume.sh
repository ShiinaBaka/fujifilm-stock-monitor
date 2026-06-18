#!/usr/bin/env bash
set -euo pipefail

APP_NAME="fujifilm-stock-monitor"
CONFIG_DIR="${CONFIG_DIR:-$HOME/.config/$APP_NAME}"
ENV_FILE="$CONFIG_DIR/env"
STATE_FILE="${STATE_FILE:-$CONFIG_DIR/state.json}"
SERVICE_NAME="$APP_NAME.service"
STAMP="$(date +%Y%m%d-%H%M%S)"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

backup_and_remove() {
  local path="$1"
  if [ -e "$path" ]; then
    cp -a "$path" "$path.backup-$STAMP"
    rm -f "$path"
  fi
}

if [ -n "${STOP_MARKER:-}" ]; then
  backup_and_remove "$STOP_MARKER"
fi

if [ -n "${MONTHLY_MARKER_DIR:-}" ]; then
  backup_and_remove "$MONTHLY_MARKER_DIR/$(date +%Y-%m).done"
fi

backup_and_remove "$STATE_FILE"

systemctl --user reset-failed "$SERVICE_NAME"
systemctl --user restart "$SERVICE_NAME"

echo "监控已恢复，并已启动一次即时检查。"
echo "旧状态和标记已用这个后缀备份：backup-$STAMP"
