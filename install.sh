#!/usr/bin/env bash
set -euo pipefail

APP_NAME="fujifilm-stock-monitor"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/share/$APP_NAME}"
CONFIG_DIR="${CONFIG_DIR:-$HOME/.config/$APP_NAME}"
SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
SERVICE_NAME="$APP_NAME.service"
WEB_SERVICE_NAME="$APP_NAME-web.service"

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
install -m 0755 "$SCRIPT_DIR/fujifilm_web.py" "$INSTALL_DIR/fujifilm_web.py"
install -m 0755 "$SCRIPT_DIR/run.sh" "$INSTALL_DIR/run.sh"
install -m 0755 "$SCRIPT_DIR/resume.sh" "$INSTALL_DIR/resume.sh"
install -m 0755 "$SCRIPT_DIR/fujifilmctl.sh" "$INSTALL_DIR/fujifilmctl"

if [ ! -f "$CONFIG_DIR/env" ]; then
  install -m 0600 "$SCRIPT_DIR/env.example" "$CONFIG_DIR/env"
  echo "已创建配置文件：$CONFIG_DIR/env"
  echo "如果需要 Server 酱、ntfy 或 Webhook 推送，请先编辑这个文件。"
fi

if ! grep -q '^FUJIFILM_ADMIN_KEY_HASH=.' "$CONFIG_DIR/env"; then
  python3 - "$CONFIG_DIR/env" "$CONFIG_DIR/admin-key.txt" <<'PY'
import base64
import hashlib
import secrets
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
key_path = Path(sys.argv[2])
admin_key = secrets.token_urlsafe(24)
session_secret = secrets.token_urlsafe(32)
salt = secrets.token_bytes(16)
iterations = 260000
digest = hashlib.pbkdf2_hmac("sha256", admin_key.encode(), salt, iterations)

def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

admin_hash = f"pbkdf2_sha256${iterations}${b64(salt)}${b64(digest)}"
lines = env_path.read_text().splitlines()
seen_hash = False
seen_session = False
out = []
for line in lines:
    if line.startswith("FUJIFILM_ADMIN_KEY_HASH="):
        out.append("FUJIFILM_ADMIN_KEY_HASH=" + admin_hash)
        seen_hash = True
    elif line.startswith("FUJIFILM_SESSION_SECRET="):
        out.append("FUJIFILM_SESSION_SECRET=" + session_secret)
        seen_session = True
    else:
        out.append(line)
if not seen_hash:
    out.extend(["", "# Web 后台通行密钥哈希。请不要手动改成明文。", "FUJIFILM_ADMIN_KEY_HASH=" + admin_hash])
if not seen_session:
    out.append("FUJIFILM_SESSION_SECRET=" + session_secret)
env_path.write_text("\n".join(out) + "\n")
key_path.write_text(admin_key + "\n")
PY
  chmod 600 "$CONFIG_DIR/env"
  chmod 600 "$CONFIG_DIR/admin-key.txt"
fi

ask() {
  local prompt="$1"
  local default="$2"
  local answer
  if [ -t 0 ]; then
    read -r -p "$prompt [$default]: " answer || true
    printf '%s\n' "${answer:-$default}"
  else
    printf '%s\n' "$default"
  fi
}

ask_secret() {
  local prompt="$1"
  local answer=""
  if [ -t 0 ]; then
    read -r -s -p "$prompt（留空表示不配置）: " answer || true
    printf '\n' >&2
  fi
  printf '%s\n' "$answer"
}

json_string() {
  python3 -c 'import json, sys; print(json.dumps(sys.argv[1], ensure_ascii=False))' "$1"
}

write_json_config() {
  local config_file="$CONFIG_DIR/config.json"
  local serverchan="$1"
  local interval="$2"
  local jitter="$3"
  local monthly="$4"
  local stop_after_first="$5"
  local task_name="$6"
  local url="$7"
  local require_text="$8"
  local state_file="$CONFIG_DIR/state.json"
  local monthly_dir=""
  local stop_marker=""

  if [ "$monthly" = "yes" ]; then
    monthly_dir="$CONFIG_DIR/monthly"
  fi
  if [ "$stop_after_first" = "yes" ]; then
    stop_marker="$CONFIG_DIR/stopped.json"
  fi

  {
    echo "{"
    echo "  \"notifications\": {"
    echo "    \"serverchan_sendkey\": $(json_string "$serverchan")"
    echo "  },"
    echo "  \"defaults\": {"
    echo "    \"interval\": $interval,"
    echo "    \"jitter\": $jitter,"
    echo "    \"failure_alert_after\": 3,"
    echo "    \"failure_alert_repeat\": 24,"
    echo "    \"ipv4\": true"
    echo "  },"
    echo "  \"tasks\": ["
    echo "    {"
    echo "      \"name\": $(json_string "$task_name"),"
    echo "      \"url\": $(json_string "$url"),"
    echo "      \"require_text\": $(json_string "$require_text"),"
    echo "      \"state_file\": $(json_string "$state_file")"
    if [ -n "$monthly_dir" ]; then
      echo "      ,\"monthly_marker_dir\": $(json_string "$monthly_dir")"
    fi
    if [ -n "$stop_marker" ]; then
      echo "      ,\"stop_marker\": $(json_string "$stop_marker")"
    fi
    echo "    }"
    echo "  ]"
    echo "}"
  } > "$config_file"
  chmod 600 "$config_file"
  echo "已生成多任务配置：$config_file"
}

if [ ! -f "$CONFIG_DIR/config.json" ]; then
  echo
  echo "安装向导：生成监控配置"
  task_name="$(ask '任务名称' 'mini 相纸')"
  url="$(ask '监控 URL' 'https://mall-jp.fujifilm.com/shop/c/c306010/')"
  require_text="$(ask '分类标题校验文本' 'チェキ用フィルム')"
  interval="$(ask '检查间隔秒数' '3600')"
  jitter="$(ask '随机延迟秒数' '600')"
  monthly="$(ask '是否每款商品每月最多推送一次？yes/no' 'yes')"
  stop_after_first="$(ask '是否补货后永久停止整个任务？yes/no' 'no')"
  serverchan="$(ask_secret 'Server 酱 SendKey')"
  write_json_config "$serverchan" "$interval" "$jitter" "$monthly" "$stop_after_first" "$task_name" "$url" "$require_text"
fi

sed \
  -e "s#__INSTALL_DIR__#$INSTALL_DIR#g" \
  -e "s#__CONFIG_DIR__#$CONFIG_DIR#g" \
  "$SCRIPT_DIR/fujifilm-stock-monitor.service.template" \
  > "$SYSTEMD_USER_DIR/$SERVICE_NAME"

sed \
  -e "s#__INSTALL_DIR__#$INSTALL_DIR#g" \
  -e "s#__CONFIG_DIR__#$CONFIG_DIR#g" \
  "$SCRIPT_DIR/fujifilm-stock-web.service.template" \
  > "$SYSTEMD_USER_DIR/$WEB_SERVICE_NAME"

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user enable "$WEB_SERVICE_NAME"

if command -v loginctl >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
  sudo loginctl enable-linger "$USER" >/dev/null 2>&1 || true
fi

echo
echo "已安装 ${APP_NAME}。"
echo
echo "运行一次检查："
echo "  $INSTALL_DIR/fujifilm_stock_monitor.py --once --print-products --ipv4"
echo
echo "启动服务："
echo "  systemctl --user start $SERVICE_NAME"
echo "  systemctl --user start $WEB_SERVICE_NAME"
echo
echo "打开 Web 控制台："
echo "  ssh -L 8765:127.0.0.1:8765 user@your-server"
echo "  http://127.0.0.1:8765"
echo "  后台通行密钥在 $CONFIG_DIR/admin-key.txt"
echo
echo "查看日志："
echo "  journalctl --user -u $SERVICE_NAME -f"
echo
echo "维护工具："
echo "  $INSTALL_DIR/fujifilmctl status"
echo
echo "没抢到时恢复监控："
echo "  $INSTALL_DIR/resume.sh"
