#!/usr/bin/env bash
set -euo pipefail

APP_NAME="fujifilm-stock-monitor"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/share/$APP_NAME}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/.config/$APP_NAME}"
ENV_FILE="$CONFIG_DIR/env"
STATE_FILE="${STATE_FILE:-$CONFIG_DIR/state.json}"
SERVICE_NAME="$APP_NAME.service"

usage() {
  cat <<'EOF'
Usage: fujifilmctl COMMAND [ARGS]

Commands:
  status            Show service, timer, marker, and state summary
  logs              Follow service logs
  check             Run one live check without changing production state
  test-push         Send a ServerChan maintenance test message
  restart           Restart the monitor
  pause             Stop the monitor service
  resume            Clear active marker/state and restart the monitor
  restore ITEM      Allow one monthly-notified product to notify again
  health            Run a compact health check

ITEM can be a full product URL or a product id such as g16587294.
EOF
}

load_env() {
  if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
  fi
}

show_state() {
  local path="$1"
  if [ ! -f "$path" ]; then
    echo "  none"
    return
  fi
  python3 - "$path" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text())
except Exception as exc:
    print(f"  unreadable: {exc}")
    raise SystemExit(0)
print(f"  file: {path}")
for key in ("checked_at", "last_failed_at", "consecutive_failures", "last_error"):
    if key in data:
        print(f"  {key}: {data[key]}")
stock = data.get("stock", {})
if isinstance(stock, dict):
    in_stock = [url for url, value in stock.items() if value]
    print(f"  products: {len(stock)} total, {len(in_stock)} in stock")
PY
}

current_month_marker() {
  load_env
  if [ -n "${MONTHLY_MARKER_DIR:-}" ]; then
    printf '%s/%s.done\n' "$MONTHLY_MARKER_DIR" "$(date +%Y-%m)"
  fi
}

status() {
  echo "== Service =="
  systemctl --user --no-pager --full status "$SERVICE_NAME" | sed -n '1,60p' || true
  echo
  echo "== State =="
  show_state "$STATE_FILE"
  echo
  echo "== Markers =="
  load_env
  if [ -n "${STOP_MARKER:-}" ]; then
    echo "stop marker:"
    ls -l "$STOP_MARKER" 2>/dev/null || echo "  none"
  fi
  marker="$(current_month_marker || true)"
  if [ -n "${marker:-}" ]; then
    echo "monthly marker:"
    if [ -f "$marker" ]; then
      python3 - "$marker" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
notified = data.get("notified", {})
print(f"  {len(notified)} product(s) notified this month")
for url, info in notified.items():
    name = info.get("name", "") if isinstance(info, dict) else ""
    print(f"  - {name} {url}".strip())
PY
    else
      echo "  none"
    fi
  fi
}

check_once() {
  load_env
  "$INSTALL_DIR/fujifilm_stock_monitor.py" \
    --once --print-products --ipv4 \
    --url "${URL:-https://mall-jp.fujifilm.com/shop/c/c306010/}" \
    --require-text "${REQUIRE_TEXT:-チェキ用フィルム}" \
    --state-file /tmp/fujifilmctl-check-state.json
}

check_summary() {
  load_env
  "$INSTALL_DIR/fujifilm_stock_monitor.py" \
    --once --ipv4 \
    --url "${URL:-https://mall-jp.fujifilm.com/shop/c/c306010/}" \
    --require-text "${REQUIRE_TEXT:-チェキ用フィルム}" \
    --state-file /tmp/fujifilmctl-check-state.json
}

test_push() {
  load_env
  if [ -z "${SERVERCHAN_SENDKEY:-}" ]; then
    echo "SERVERCHAN_SENDKEY is not configured in $ENV_FILE" >&2
    exit 1
  fi
  python3 - "$SERVERCHAN_SENDKEY" <<'PY'
import sys, urllib.parse, urllib.request
from datetime import datetime

sendkey = sys.argv[1]
body = urllib.parse.urlencode({
    "title": "Fujifilm monitor test",
    "desp": f"Maintenance notification test succeeded.\n\nTime: {datetime.now().isoformat(timespec='seconds')}",
}).encode()
req = urllib.request.Request(
    f"https://sctapi.ftqq.com/{sendkey}.send",
    data=body,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=15) as resp:
    print(resp.read().decode("utf-8", "replace"))
PY
}

restore_item() {
  local item="${1:-}"
  if [ -z "$item" ]; then
    usage
    exit 2
  fi
  marker="$(current_month_marker || true)"
  if [ -z "${marker:-}" ] || [ ! -f "$marker" ]; then
    echo "No monthly marker exists; nothing to restore."
    return
  fi
  cp -a "$marker" "$marker.backup-$(date +%Y%m%d-%H%M%S)"
  python3 - "$marker" "$item" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
item = sys.argv[2].rstrip("/")
data = json.loads(path.read_text())
notified = data.get("notified", {})
if not isinstance(notified, dict):
    notified = {}

removed = [url for url in list(notified) if url.rstrip("/") == item or item in url]
for url in removed:
    del notified[url]
data["notified"] = notified
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
if removed:
    print("Restored:")
    for url in removed:
        print(f"  {url}")
else:
    print(f"No matching product found in marker: {item}")
PY
  systemctl --user restart "$SERVICE_NAME"
}

health() {
  failed=0
  state="$(systemctl --user is-active "$SERVICE_NAME" || true)"
  echo "$SERVICE_NAME: $state"
  [ "$state" = "active" ] || failed=1
  python3 -m py_compile "$INSTALL_DIR/fujifilm_stock_monitor.py"
  test -f "$ENV_FILE" && echo "env: present" || { echo "env: missing"; failed=1; }
  stat -c 'env permissions: %a %U:%G %n' "$ENV_FILE" 2>/dev/null || true
  check_summary >/tmp/fujifilmctl-check.log
  tail -n 1 /tmp/fujifilmctl-check.log
  return "$failed"
}

case "${1:-}" in
  status) status ;;
  logs) journalctl --user -u "$SERVICE_NAME" -n 120 -f ;;
  check) check_once ;;
  test-push) test_push ;;
  restart) systemctl --user restart "$SERVICE_NAME" ;;
  pause) systemctl --user stop "$SERVICE_NAME" ;;
  resume) "$INSTALL_DIR/resume.sh" ;;
  restore) shift; restore_item "${1:-}" ;;
  health) health ;;
  -h|--help|help|"") usage ;;
  *) usage; exit 2 ;;
esac
