#!/usr/bin/env python3
"""Small authenticated web console for Fujifilm Stock Monitor."""

from __future__ import annotations

import argparse
import html
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import urllib.parse
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


APP_NAME = "fujifilm-stock-monitor"
DEFAULT_CONFIG = Path.home() / ".config" / APP_NAME / "config.json"
DEFAULT_SERVICE = f"{APP_NAME}.service"
FUJIFILM_HOST = "mall-jp.fujifilm.com"


def safe_task_slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")
    return slug or "task"


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def resolve_path(value: str | None, base: Path) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path


def validate_fujifilm_url(raw_url: str) -> str:
    url = raw_url.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != FUJIFILM_HOST:
        raise ValueError("只允许 https://mall-jp.fujifilm.com 的链接。")
    if not (parsed.path.startswith("/shop/g/") or parsed.path.startswith("/shop/c/")):
        raise ValueError("只支持 Fujifilm 商品页 /shop/g/ 或分类页 /shop/c/。")
    cleaned = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    return cleaned if cleaned.endswith("/") else cleaned + "/"


def product_id_from_url(url: str) -> str:
    match = re.search(r"/shop/g/(g[0-9A-Za-z_-]+)/?", urllib.parse.urlparse(url).path)
    return match.group(1) if match else safe_task_slug(urllib.parse.urlparse(url).path.strip("/"))


def run_command(args: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            args,
            check=False,
            timeout=timeout,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return completed.returncode, completed.stdout.strip()


class WebApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.config_path = args.config.expanduser()
        self.service_name = args.service_name
        self.install_dir = args.install_dir.expanduser()
        self.token = args.token or os.environ.get("FUJIFILM_WEB_TOKEN", "")
        self.allow_no_auth = args.allow_no_auth
        self.monitor_script = self.install_dir / "fujifilm_stock_monitor.py"

    @property
    def config_dir(self) -> Path:
        return self.config_path.parent

    def config(self) -> dict:
        data = load_json(self.config_path)
        if not data:
            data = {"notifications": {}, "defaults": {"interval": 3600, "jitter": 600, "ipv4": True}, "tasks": []}
        data.setdefault("notifications", {})
        data.setdefault("defaults", {})
        data.setdefault("tasks", [])
        return data

    def tasks(self) -> list[dict]:
        config = self.config()
        defaults = config.get("defaults", {}) if isinstance(config.get("defaults"), dict) else {}
        result = []
        for task in config.get("tasks", []):
            if not isinstance(task, dict):
                continue
            name = str(task.get("name") or "未命名")
            state_default = f"state/{safe_task_slug(name)}.json"
            state_file = resolve_path(str(task.get("state_file") or defaults.get("state_file") or state_default), self.config_dir)
            stop_marker = resolve_path(task.get("stop_marker") or defaults.get("stop_marker"), self.config_dir)
            monthly_dir = resolve_path(task.get("monthly_marker_dir") or defaults.get("monthly_marker_dir"), self.config_dir)
            state = load_json(state_file) if state_file else {}
            stock = state.get("stock", {}) if isinstance(state.get("stock"), dict) else {}
            in_stock = [url for url, value in stock.items() if value]
            marker_file = monthly_dir / f"{datetime.now():%Y-%m}.done" if monthly_dir else None
            notified = load_json(marker_file).get("notified", {}) if marker_file else {}
            result.append(
                {
                    "name": name,
                    "url": str(task.get("url") or ""),
                    "state_file": str(state_file or ""),
                    "checked_at": state.get("checked_at", ""),
                    "last_failed_at": state.get("last_failed_at", ""),
                    "consecutive_failures": state.get("consecutive_failures", 0),
                    "last_error": state.get("last_error", ""),
                    "total": len(stock),
                    "in_stock": in_stock,
                    "notified_count": len(notified) if isinstance(notified, dict) else 0,
                    "paused": bool(stop_marker and stop_marker.exists()),
                }
            )
        return result

    def add_url(self, raw_url: str, raw_name: str) -> str:
        url = validate_fujifilm_url(raw_url)
        config = self.config()
        tasks = config.setdefault("tasks", [])
        if not isinstance(tasks, list):
            raise ValueError("config.json 里的 tasks 必须是数组。")
        if any(isinstance(task, dict) and str(task.get("url", "")).rstrip("/") == url.rstrip("/") for task in tasks):
            raise ValueError("这个链接已经在监控列表里。")

        parsed = urllib.parse.urlparse(url)
        if parsed.path.startswith("/shop/g/"):
            default_name = f"商品 {product_id_from_url(url)}"
            require_text = ""
        else:
            default_name = f"分类 {parsed.path.rstrip('/').split('/')[-1]}"
            require_text = ""
        name = raw_name.strip() or default_name
        slug = safe_task_slug(name)
        tasks.append(
            {
                "name": name,
                "url": url,
                "require_text": require_text,
                "state_file": f"state/{slug}.json",
                "monthly_marker_dir": f"monthly/{slug}",
                "alert_on_first_run": True,
            }
        )
        write_json(self.config_path, config)
        self.restart_service()
        return name

    def systemctl(self, action: str) -> tuple[int, str]:
        if action not in {"start", "stop", "restart"}:
            return 2, "不支持的操作。"
        return run_command(["systemctl", "--user", action, self.service_name], timeout=30)

    def restart_service(self) -> tuple[int, str]:
        return self.systemctl("restart")

    def check_once(self) -> tuple[int, str]:
        script = self.monitor_script if self.monitor_script.exists() else Path(__file__).with_name("fujifilm_stock_monitor.py")
        return run_command(
            [sys.executable, str(script), "--config", str(self.config_path), "--once", "--print-products"],
            timeout=90,
        )

    def service_status(self) -> str:
        code, output = run_command(["systemctl", "--user", "is-active", self.service_name], timeout=10)
        return output if code == 0 else output or "unknown"

    def logs(self) -> str:
        _, output = run_command(["journalctl", "--user", "-u", self.service_name, "-n", "80", "--no-pager"], timeout=20)
        return output


class Handler(BaseHTTPRequestHandler):
    server_version = "FujifilmStockWeb/1.0"

    @property
    def app(self) -> WebApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def is_authenticated(self) -> bool:
        if self.app.allow_no_auth:
            return True
        if not self.app.token:
            return False
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], self.app.token):
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        value = cookie.get("fujifilm_token")
        return bool(value and hmac.compare_digest(value.value, self.app.token))

    def send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def read_form(self) -> dict[str, str]:
        size = int(self.headers.get("Content-Length", "0") or "0")
        if size > 16384:
            raise ValueError("请求太大。")
        raw = self.rfile.read(size).decode("utf-8", "replace")
        values = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {key: values[key][0] for key in values}

    def require_post_token(self, form: dict[str, str]) -> None:
        if self.app.allow_no_auth:
            return
        if not hmac.compare_digest(form.get("auth_token", ""), self.app.token):
            raise ValueError("表单已过期，请刷新页面后重试。")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/login"):
            self.send_html(self.login_page(""))
            return
        if not self.is_authenticated():
            self.redirect("/login")
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/logs":
            self.send_html(self.page("日志", f"<pre>{html.escape(self.app.logs())}</pre>"))
            return
        self.send_html(self.dashboard())

    def do_POST(self) -> None:  # noqa: N802
        try:
            form = self.read_form()
            if self.path.startswith("/login"):
                token = form.get("token", "")
                if self.app.token and hmac.compare_digest(token, self.app.token):
                    self.send_response(HTTPStatus.SEE_OTHER)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", "fujifilm_token=%s; HttpOnly; SameSite=Strict; Path=/" % token)
                    self.end_headers()
                    return
                self.send_html(self.login_page("Token 不正确。"), HTTPStatus.UNAUTHORIZED)
                return
            if not self.is_authenticated():
                self.redirect("/login")
                return
            self.require_post_token(form)
            action = form.get("action", "")
            if action == "add":
                name = self.app.add_url(form.get("url", ""), form.get("name", ""))
                self.redirect("/?msg=" + urllib.parse.quote(f"已添加：{name}"))
            elif action in {"start", "stop", "restart"}:
                code, output = self.app.systemctl(action)
                msg = f"{action}: {'成功' if code == 0 else '失败'} {output}"
                self.redirect("/?msg=" + urllib.parse.quote(msg))
            elif action == "check":
                code, output = self.app.check_once()
                title = "检查完成" if code == 0 else "检查失败"
                self.send_html(self.page(title, f"<pre>{html.escape(output)}</pre><p><a href='/'>返回</a></p>"))
            else:
                raise ValueError("未知操作。")
        except ValueError as exc:
            self.send_html(self.page("操作失败", f"<p class='error'>{html.escape(str(exc))}</p><p><a href='/'>返回</a></p>"), HTTPStatus.BAD_REQUEST)

    def login_page(self, error: str) -> str:
        error_html = f"<p class='error'>{html.escape(error)}</p>" if error else ""
        return self.page(
            "登录",
            f"""
            {error_html}
            <form method="post" action="/login" class="panel">
              <label>访问 Token<input type="password" name="token" autofocus></label>
              <button type="submit">登录</button>
            </form>
            """,
        )

    def dashboard(self) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        msg = query.get("msg", [""])[0]
        msg_html = f"<p class='ok'>{html.escape(msg)}</p>" if msg else ""
        rows = []
        for task in self.app.tasks():
            stock_links = "".join(f"<li><a href='{html.escape(url)}'>{html.escape(url)}</a></li>" for url in task["in_stock"])
            stock_html = f"<ul>{stock_links}</ul>" if stock_links else "<span class='muted'>暂无</span>"
            paused = "<span class='badge warn'>已暂停</span>" if task["paused"] else "<span class='badge okb'>运行中</span>"
            error = f"<div class='error small'>{html.escape(str(task['last_error']))}</div>" if task["last_error"] else ""
            rows.append(
                f"""
                <section class="card">
                  <div class="card-head">
                    <h2>{html.escape(task['name'])}</h2>
                    {paused}
                  </div>
                  <a href="{html.escape(task['url'])}">{html.escape(task['url'])}</a>
                  <dl>
                    <dt>上次检查</dt><dd>{html.escape(str(task['checked_at'] or '尚无'))}</dd>
                    <dt>商品数量</dt><dd>{task['total']} 个，当前有货 {len(task['in_stock'])} 个</dd>
                    <dt>本月已推送</dt><dd>{task['notified_count']} 个</dd>
                    <dt>连续失败</dt><dd>{html.escape(str(task['consecutive_failures']))}</dd>
                  </dl>
                  {error}
                  <div>当前有货：{stock_html}</div>
                </section>
                """
            )
        token_field = html.escape(self.app.token)
        return self.page(
            "Fujifilm 监控控制台",
            f"""
            {msg_html}
            <section class="panel">
              <h2>添加监控</h2>
              <form method="post">
                <input type="hidden" name="auth_token" value="{token_field}">
                <input type="hidden" name="action" value="add">
                <label>商品或分类链接<input name="url" placeholder="https://mall-jp.fujifilm.com/shop/g/g16587294/" required></label>
                <label>名称<input name="name" placeholder="可选，例如 mini 白边 1P"></label>
                <button type="submit">开始监控</button>
              </form>
            </section>
            <section class="panel actions">
              <h2>服务控制</h2>
              <p>服务状态：<strong>{html.escape(self.app.service_status())}</strong></p>
              <form method="post">
                <input type="hidden" name="auth_token" value="{token_field}">
                <button name="action" value="check">立即检查</button>
                <button name="action" value="restart">重启</button>
                <button name="action" value="stop">暂停</button>
                <button name="action" value="start">恢复</button>
                <a class="button" href="/logs">查看日志</a>
              </form>
            </section>
            {''.join(rows) or "<p class='muted'>还没有监控任务。</p>"}
            """,
        )

    def page(self, title: str, body: str) -> str:
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #14171f; }}
    header {{ background: #17202a; color: white; padding: 20px max(20px, calc((100vw - 1040px) / 2)); }}
    main {{ max-width: 1040px; margin: 0 auto; padding: 20px; display: grid; gap: 16px; }}
    h1 {{ margin: 0; font-size: 24px; }} h2 {{ margin: 0 0 12px; font-size: 18px; }}
    .panel, .card {{ background: white; border: 1px solid #d9dee7; border-radius: 8px; padding: 16px; }}
    .card-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
    form {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: end; }}
    label {{ display: grid; gap: 6px; flex: 1 1 260px; font-size: 14px; color: #374151; }}
    input {{ border: 1px solid #b8c0cc; border-radius: 6px; padding: 10px 12px; font-size: 15px; }}
    button, .button {{ border: 0; border-radius: 6px; background: #1f6feb; color: white; padding: 10px 14px; font-size: 14px; text-decoration: none; cursor: pointer; }}
    button[value="stop"] {{ background: #b42318; }} button[value="restart"] {{ background: #875bf7; }}
    a {{ color: #0969da; word-break: break-all; }} dl {{ display: grid; grid-template-columns: 110px 1fr; gap: 6px 12px; }}
    dt {{ color: #5f6b7a; }} dd {{ margin: 0; }}
    pre {{ white-space: pre-wrap; background: #111827; color: #e5e7eb; border-radius: 8px; padding: 14px; overflow: auto; }}
    .badge {{ border-radius: 999px; padding: 4px 9px; font-size: 13px; }} .warn {{ background: #fff0c2; color: #7a4b00; }} .okb {{ background: #dff8e7; color: #116329; }}
    .ok {{ background: #dff8e7; border: 1px solid #9ad8ae; padding: 10px; border-radius: 8px; }} .error {{ color: #b42318; }} .small {{ margin-top: 8px; font-size: 13px; }}
    .muted {{ color: #697586; }}
  </style>
</head>
<body>
  <header><h1>{html.escape(title)}</h1></header>
  <main>{body}</main>
</body>
</html>"""


class Server(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], app: WebApp) -> None:
        super().__init__(address, Handler)
        self.app = app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Authenticated web console for Fujifilm Stock Monitor.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--install-dir", type=Path, default=Path.home() / ".local" / "share" / APP_NAME)
    parser.add_argument("--service-name", default=DEFAULT_SERVICE)
    parser.add_argument("--token", default="")
    parser.add_argument("--allow-no-auth", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    app = WebApp(args)
    if not app.token and not app.allow_no_auth:
        print("FUJIFILM_WEB_TOKEN is required.", file=sys.stderr)
        return 2
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        print("Warning: exposing this console beyond localhost is not recommended.", file=sys.stderr)
    httpd = Server((args.host, args.port), app)
    print(f"Fujifilm web console listening on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
