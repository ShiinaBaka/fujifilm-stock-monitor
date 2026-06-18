#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFIG_FILE="${CONFIG_FILE:-$HOME/.config/fujifilm-stock-monitor/config.json}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-3600}"
JITTER_SECONDS="${JITTER_SECONDS:-600}"
FAILURE_ALERT_AFTER="${FAILURE_ALERT_AFTER:-3}"
FAILURE_ALERT_REPEAT="${FAILURE_ALERT_REPEAT:-24}"
STATE_FILE="${STATE_FILE:-$HOME/.config/fujifilm-stock-monitor/state.json}"
URL="${URL:-https://mall-jp.fujifilm.com/shop/c/c306010/}"
REQUIRE_TEXT="${REQUIRE_TEXT:-チェキ用フィルム}"
STOP_MARKER="${STOP_MARKER:-}"
MONTHLY_MARKER_DIR="${MONTHLY_MARKER_DIR:-}"

if [ -f "$CONFIG_FILE" ]; then
  exec /usr/bin/python3 "$SCRIPT_DIR/fujifilm_stock_monitor.py" --config "$CONFIG_FILE"
fi

EXTRA_ARGS=()
if [ -n "$STOP_MARKER" ]; then
  EXTRA_ARGS+=(--stop-marker "$STOP_MARKER")
fi
if [ -n "$MONTHLY_MARKER_DIR" ]; then
  EXTRA_ARGS+=(--monthly-marker-dir "$MONTHLY_MARKER_DIR")
fi

exec /usr/bin/python3 "$SCRIPT_DIR/fujifilm_stock_monitor.py" \
  --url "$URL" \
  --require-text "$REQUIRE_TEXT" \
  --interval "$INTERVAL_SECONDS" \
  --jitter "$JITTER_SECONDS" \
  --failure-alert-after "$FAILURE_ALERT_AFTER" \
  --failure-alert-repeat "$FAILURE_ALERT_REPEAT" \
  --state-file "$STATE_FILE" \
  --ipv4 \
  "${EXTRA_ARGS[@]}"
