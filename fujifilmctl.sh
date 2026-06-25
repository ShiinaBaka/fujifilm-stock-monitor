#!/usr/bin/env bash
set -euo pipefail

APP_NAME="fujifilm-stock-monitor"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/share/$APP_NAME}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/.config/$APP_NAME}"
ENV_FILE="$CONFIG_DIR/env"
CONFIG_FILE="${CONFIG_FILE:-$CONFIG_DIR/config.json}"
STATE_FILE="${STATE_FILE:-$CONFIG_DIR/state.json}"
SERVICE_NAME="$APP_NAME.service"
WEB_SERVICE_NAME="$APP_NAME-web.service"

usage() {
  cat <<'EOF'
用法：fujifilmctl 命令 [参数]

命令：
  status            查看服务、状态文件和暂停/去重标记
  logs              实时查看服务日志
  check             运行一次真实检查，不改正式状态
  config            查看当前配置摘要，不显示密钥明文
  notified          查看本月已经推送过的商品
  clear-notified    清空本月商品去重记录
  test-push         发送一条 Server 酱维护测试推送
  restart           重启监控服务
  pause             暂停监控服务
  resume            清理状态/标记并恢复监控
  restore ITEM      只恢复某个商品本月再次推送
  web-status        查看 Web 控制台状态
  web-start         启动 Web 控制台
  web-stop          停止 Web 控制台
  web-key           显示 Web 后台通行密钥
  health            运行一轮紧凑健康检查

ITEM 可以是完整商品链接，也可以是 g16587294 这样的商品 ID。
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
    echo "  无"
    return
  fi
  python3 - "$path" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text())
except Exception as exc:
    print(f"  读取失败：{exc}")
    raise SystemExit(0)
print(f"  文件：{path}")
labels = {
    "checked_at": "上次检查",
    "last_failed_at": "上次失败",
    "consecutive_failures": "连续失败",
    "last_error": "最近错误",
}
for key, label in labels.items():
    if key in data:
        print(f"  {label}：{data[key]}")
stock = data.get("stock", {})
if isinstance(stock, dict):
    in_stock = [url for url, value in stock.items() if value]
    print(f"  商品：共 {len(stock)} 个，当前有货 {len(in_stock)} 个")
PY
}

current_month_marker() {
  load_env
  if [ -f "$CONFIG_FILE" ]; then
    python3 - "$CONFIG_FILE" "$(date +%Y-%m)" <<'PY'
import json, sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text())
month = sys.argv[2]
base = Path(sys.argv[1]).parent
for task in config.get("tasks", []):
    marker_dir = task.get("monthly_marker_dir") or config.get("defaults", {}).get("monthly_marker_dir")
    if marker_dir:
        p = Path(marker_dir).expanduser()
        if not p.is_absolute():
            p = base / p
        print(str(p / f"{month}.done"))
PY
  elif [ -n "${MONTHLY_MARKER_DIR:-}" ]; then
    printf '%s/%s.done\n' "$MONTHLY_MARKER_DIR" "$(date +%Y-%m)"
  fi
}

state_files() {
  if [ -f "$CONFIG_FILE" ]; then
    python3 - "$CONFIG_FILE" <<'PY'
import json, re, sys
from pathlib import Path

path = Path(sys.argv[1])
config = json.loads(path.read_text())
base = path.parent
defaults = config.get("defaults", {})
for task in config.get("tasks", []):
    name = str(task.get("name") or "task")
    fallback = "state/" + (re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "task") + ".json"
    state = task.get("state_file") or defaults.get("state_file") or fallback
    p = Path(state).expanduser()
    if not p.is_absolute():
        p = base / p
    print(p)
PY
  else
    printf '%s\n' "$STATE_FILE"
  fi
}

status() {
  echo "== 服务 =="
  systemctl --user --no-pager --full status "$SERVICE_NAME" | sed -n '1,60p' || true
  echo
  echo "== 状态 =="
  state_files | while IFS= read -r state; do
    show_state "$state"
  done
  echo
  echo "== 标记 =="
  load_env
  if [ -n "${STOP_MARKER:-}" ]; then
    echo "永久暂停标记："
    ls -l "$STOP_MARKER" 2>/dev/null || echo "  无"
  fi
  markers="$(current_month_marker || true)"
  if [ -n "${markers:-}" ]; then
    echo "本月商品去重标记："
    printf '%s\n' "$markers" | while IFS= read -r marker; do
      echo "  $marker"
      show_notified_file "$marker"
    done
  fi
}

show_notified_file() {
  local marker="$1"
  if [ -f "$marker" ]; then
    python3 - "$marker" <<'PY'
import json, sys
from pathlib import Path
try:
    data = json.loads(Path(sys.argv[1]).read_text())
except Exception as exc:
    print(f"  读取失败：{exc}")
    raise SystemExit(0)
notified = data.get("notified", {})
print(f"  本月已通知 {len(notified)} 个商品")
for url, info in notified.items():
    name = info.get("name", "") if isinstance(info, dict) else ""
    at = info.get("notified_at", "") if isinstance(info, dict) else ""
    suffix = f" ({at})" if at else ""
    if name:
        print(f"  - {name}{suffix}\n    {url}")
    else:
        print(f"  - {url}{suffix}")
PY
  else
    echo "  无"
  fi
}

config_summary() {
  load_env
  echo "== 配置 =="
  echo "配置文件：$ENV_FILE"
  echo "多任务配置：$CONFIG_FILE"
  echo "服务名：$SERVICE_NAME"
  if [ -f "$CONFIG_FILE" ]; then
    python3 - "$CONFIG_FILE" <<'PY'
import json, sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text())
print(f"任务数量：{len(config.get('tasks', []))}")
for task in config.get("tasks", []):
    print(f"- {task.get('name', '未命名')}：{task.get('url', '')}")
PY
    return
  fi
  echo "监控 URL：${URL:-https://mall-jp.fujifilm.com/shop/c/c306010/}"
  echo "页面标题校验：${REQUIRE_TEXT:-チェキ用フィルム}"
  echo "检查间隔：${INTERVAL_SECONDS:-3600} 秒"
  echo "随机延迟：${JITTER_SECONDS:-600} 秒"
  echo "失败报警：连续 ${FAILURE_ALERT_AFTER:-3} 次后报警，每 ${FAILURE_ALERT_REPEAT:-24} 次重复"
  echo "状态文件：$STATE_FILE"
  echo "永久暂停标记：${STOP_MARKER:-未启用}"
  echo "月度去重目录：${MONTHLY_MARKER_DIR:-未启用}"
  [ -n "${SERVERCHAN_SENDKEY:-}" ] && echo "Server 酱：已配置" || echo "Server 酱：未配置"
  [ -n "${STOCK_NTFY_TOPIC:-}" ] && echo "ntfy：已配置" || echo "ntfy：未配置"
  [ -n "${STOCK_WEBHOOK_URL:-}" ] && echo "Webhook：已配置" || echo "Webhook：未配置"
}

notified() {
  markers="$(current_month_marker || true)"
  if [ -z "${markers:-}" ]; then
    echo "未启用 MONTHLY_MARKER_DIR。"
    return
  fi
  printf '%s\n' "$markers" | while IFS= read -r marker; do
    echo "$marker"
    show_notified_file "$marker"
  done
}

clear_notified() {
  markers="$(current_month_marker || true)"
  if [ -z "${markers:-}" ]; then
    echo "本月没有商品去重记录。"
    return
  fi
  found=0
  printf '%s\n' "$markers" | while IFS= read -r marker; do
    if [ -f "$marker" ]; then
      cp -a "$marker" "$marker.backup-$(date +%Y%m%d-%H%M%S)"
      rm -f "$marker"
      found=1
    fi
  done
  systemctl --user restart "$SERVICE_NAME"
  echo "已清空本月商品去重记录，并重启监控。"
}

check_once() {
  load_env
  if [ -f "$CONFIG_FILE" ]; then
    "$INSTALL_DIR/fujifilm_stock_monitor.py" --config "$CONFIG_FILE" --once --print-products
    return
  fi
  "$INSTALL_DIR/fujifilm_stock_monitor.py" \
    --once --print-products --ipv4 \
    --url "${URL:-https://mall-jp.fujifilm.com/shop/c/c306010/}" \
    --require-text "${REQUIRE_TEXT:-チェキ用フィルム}" \
    --state-file /tmp/fujifilmctl-check-state.json
}

check_summary() {
  load_env
  if [ -f "$CONFIG_FILE" ]; then
    "$INSTALL_DIR/fujifilm_stock_monitor.py" --config "$CONFIG_FILE" --once
    return
  fi
  "$INSTALL_DIR/fujifilm_stock_monitor.py" \
    --once --ipv4 \
    --url "${URL:-https://mall-jp.fujifilm.com/shop/c/c306010/}" \
    --require-text "${REQUIRE_TEXT:-チェキ用フィルム}" \
    --state-file /tmp/fujifilmctl-check-state.json
}

test_push() {
  load_env
  if [ -z "${SERVERCHAN_SENDKEY:-}" ]; then
    echo "未在 $ENV_FILE 配置 SERVERCHAN_SENDKEY。" >&2
    exit 1
  fi
  python3 - "$SERVERCHAN_SENDKEY" <<'PY'
import sys, urllib.parse, urllib.request
from datetime import datetime

sendkey = sys.argv[1]
body = urllib.parse.urlencode({
    "title": "Fujifilm 维护测试",
    "desp": f"维护测试推送成功。\n\n时间：{datetime.now().isoformat(timespec='seconds')}",
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
  markers="$(current_month_marker || true)"
  if [ -z "${markers:-}" ]; then
    echo "本月没有商品去重记录，无需恢复。"
    return
  fi
  printf '%s\n' "$markers" | while IFS= read -r marker; do
    [ -f "$marker" ] || continue
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
    print("已恢复这些商品本月再次推送：")
    for url in removed:
        print(f"  {url}")
else:
    print(f"没有找到匹配商品：{item}")
PY
  done
  state_files | while IFS= read -r state; do
    python3 - "$state" "$item" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
item = sys.argv[2].rstrip("/")
if not path.exists():
    raise SystemExit(0)
try:
    data = json.loads(path.read_text())
except Exception:
    raise SystemExit(0)
stock = data.get("stock")
if not isinstance(stock, dict):
    raise SystemExit(0)
changed = False
for url in list(stock):
    if url.rstrip("/") == item or item in url:
        stock[url] = False
        changed = True
if changed:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print("已同步重置状态文件，下一轮可再次触发该商品。")
PY
  done
  systemctl --user restart "$SERVICE_NAME"
}

health() {
  failed=0
  state="$(systemctl --user is-active "$SERVICE_NAME" || true)"
  echo "$SERVICE_NAME: $state"
  [ "$state" = "active" ] || failed=1
  python3 -m py_compile "$INSTALL_DIR/fujifilm_stock_monitor.py"
  test -f "$ENV_FILE" && echo "配置文件：存在" || { echo "配置文件：缺失"; failed=1; }
  stat -c '配置权限：%a %U:%G %n' "$ENV_FILE" 2>/dev/null || true
  check_summary >/tmp/fujifilmctl-check.log
  tail -n 1 /tmp/fujifilmctl-check.log
  return "$failed"
}

web_key() {
  local key_file="$CONFIG_DIR/admin-key.txt"
  if [ ! -f "$key_file" ]; then
    echo "未找到 $key_file。若已改为只保存哈希，请重置后台通行密钥。" >&2
    exit 1
  fi
  cat "$key_file"
}

case "${1:-}" in
  status) status ;;
  logs) journalctl --user -u "$SERVICE_NAME" -n 120 -f ;;
  check) check_once ;;
  config) config_summary ;;
  notified) notified ;;
  clear-notified) clear_notified ;;
  test-push) test_push ;;
  restart) systemctl --user restart "$SERVICE_NAME" ;;
  pause) systemctl --user stop "$SERVICE_NAME" ;;
  resume) "$INSTALL_DIR/resume.sh" ;;
  restore) shift; restore_item "${1:-}" ;;
  web-status) systemctl --user --no-pager --full status "$WEB_SERVICE_NAME" ;;
  web-start) systemctl --user start "$WEB_SERVICE_NAME" ;;
  web-stop) systemctl --user stop "$WEB_SERVICE_NAME" ;;
  web-key) web_key ;;
  health) health ;;
  -h|--help|help|"") usage ;;
  *) usage; exit 2 ;;
esac
