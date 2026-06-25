#!/usr/bin/env python3
"""Small authenticated web console for Fujifilm Stock Monitor."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
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
SESSION_SECONDS = 12 * 60 * 60
PBKDF2_ITERATIONS = 260000


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


def b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_admin_key_hash(admin_key: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", admin_key.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${b64encode(salt)}${b64encode(digest)}"


def verify_admin_key_hash(admin_key: str, encoded_hash: str) -> bool:
    try:
        algorithm, iterations, salt, digest = encoded_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac("sha256", admin_key.encode("utf-8"), b64decode(salt), int(iterations))
        return hmac.compare_digest(b64encode(expected), digest)
    except (ValueError, TypeError):
        return False


class WebApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.config_path = args.config.expanduser()
        self.service_name = args.service_name
        self.install_dir = args.install_dir.expanduser()
        self.admin_key_hash = args.admin_key_hash or os.environ.get("FUJIFILM_ADMIN_KEY_HASH", "")
        self.legacy_token = args.token or os.environ.get("FUJIFILM_WEB_TOKEN", "")
        self.session_secret = (
            args.session_secret
            or os.environ.get("FUJIFILM_SESSION_SECRET")
            or self.legacy_token
            or self.admin_key_hash
        )
        self.secure_cookie = args.secure_cookie or os.environ.get("FUJIFILM_COOKIE_SECURE") == "1"
        self.allow_no_auth = args.allow_no_auth
        self.monitor_script = self.install_dir / "fujifilm_stock_monitor.py"
        self.systemctl_scope = args.systemctl_scope

    def has_login_secret(self) -> bool:
        return bool(self.admin_key_hash or self.legacy_token)

    def verify_admin_key(self, admin_key: str) -> bool:
        if self.admin_key_hash:
            return verify_admin_key_hash(admin_key, self.admin_key_hash)
        return bool(self.legacy_token and hmac.compare_digest(admin_key, self.legacy_token))

    def make_session_cookie(self) -> str:
        expires = str(int(time.time()) + SESSION_SECONDS)
        nonce = secrets.token_urlsafe(18)
        payload = f"{expires}.{nonce}"
        signature = hmac.new(self.session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def verify_session_cookie(self, cookie_value: str) -> bool:
        try:
            expires_text, nonce, signature = cookie_value.split(".", 2)
            payload = f"{expires_text}.{nonce}"
            if int(expires_text) < int(time.time()):
                return False
        except (ValueError, TypeError):
            return False
        expected = hmac.new(self.session_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def csrf_token(self, session_cookie: str) -> str:
        return hmac.new(self.session_secret.encode("utf-8"), session_cookie.encode("utf-8"), hashlib.sha256).hexdigest()

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
        for index, task in enumerate(config.get("tasks", [])):
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
                    "index": index,
                    "name": name,
                    "url": str(task.get("url") or ""),
                    "require_text": str(task.get("require_text", "")),
                    "interval": task.get("interval", defaults.get("interval", "")),
                    "jitter": task.get("jitter", defaults.get("jitter", "")),
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

    def add_url(
        self,
        raw_url: str,
        raw_name: str,
        require_text: str | None = None,
        interval: str | None = None,
        jitter: str | None = None,
        policy: str = "monthly",
    ) -> str:
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
            default_require_text = ""
        else:
            default_name = f"分类 {parsed.path.rstrip('/').split('/')[-1]}"
            default_require_text = ""
        name = raw_name.strip() or default_name
        slug = safe_task_slug(name)
        task = {
            "name": name,
            "url": url,
            "require_text": default_require_text if require_text is None else require_text.strip(),
            "state_file": f"state/{slug}.json",
            "alert_on_first_run": True,
        }
        if interval and interval.strip():
            task["interval"] = max(int(interval), 600)
        if jitter and jitter.strip():
            task["jitter"] = max(int(jitter), 0)
        if policy == "stop":
            task["stop_marker"] = f"stopped/{slug}.json"
        elif policy == "none":
            pass
        else:
            task["monthly_marker_dir"] = f"monthly/{slug}"
        tasks.append(task)
        write_json(self.config_path, config)
        self.restart_service()
        return name

    def delete_task(self, raw_index: str) -> str:
        index = int(raw_index)
        config = self.config()
        tasks = config.get("tasks", [])
        if not isinstance(tasks, list) or index < 0 or index >= len(tasks):
            raise ValueError("任务不存在。")
        task = tasks.pop(index)
        name = task.get("name", f"#{index}") if isinstance(task, dict) else f"#{index}"
        write_json(self.config_path, config)
        self.restart_service()
        return str(name)

    def clear_notified(self, raw_index: str) -> str:
        index = int(raw_index)
        config = self.config()
        tasks = config.get("tasks", [])
        defaults = config.get("defaults", {}) if isinstance(config.get("defaults"), dict) else {}
        if not isinstance(tasks, list) or index < 0 or index >= len(tasks) or not isinstance(tasks[index], dict):
            raise ValueError("任务不存在。")
        task = tasks[index]
        name = str(task.get("name") or f"#{index}")
        marker_dir = resolve_path(task.get("monthly_marker_dir") or defaults.get("monthly_marker_dir"), self.config_dir)
        if not marker_dir:
            raise ValueError("这个任务没有启用本月去重。")
        marker = marker_dir / f"{datetime.now():%Y-%m}.done"
        if marker.exists():
            backup = marker.with_suffix(marker.suffix + f".backup-{datetime.now():%Y%m%d-%H%M%S}")
            marker.replace(backup)
        self.restart_service()
        return name

    def systemctl(self, action: str) -> tuple[int, str]:
        if action not in {"start", "stop", "restart"}:
            return 2, "不支持的操作。"
        if self.systemctl_scope == "system":
            command = ["sudo", "-n", "systemctl", action, self.service_name]
        else:
            command = ["systemctl", "--user", action, self.service_name]
        return run_command(command, timeout=30)

    def restart_service(self) -> tuple[int, str]:
        return self.systemctl("restart")

    def check_once(self) -> tuple[int, str]:
        script = self.monitor_script if self.monitor_script.exists() else Path(__file__).with_name("fujifilm_stock_monitor.py")
        return run_command(
            [sys.executable, str(script), "--config", str(self.config_path), "--once", "--print-products"],
            timeout=90,
        )

    def service_status(self) -> str:
        if self.systemctl_scope == "system":
            command = ["systemctl", "is-active", self.service_name]
        else:
            command = ["systemctl", "--user", "is-active", self.service_name]
        code, output = run_command(command, timeout=10)
        return output if code == 0 else output or "unknown"

    def logs(self) -> str:
        if self.systemctl_scope == "system":
            command = ["journalctl", "-u", self.service_name, "-n", "80", "--no-pager"]
        else:
            command = ["journalctl", "--user", "-u", self.service_name, "-n", "80", "--no-pager"]
        _, output = run_command(command, timeout=20)
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
        if not self.app.has_login_secret():
            return False
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        value = cookie.get("fujifilm_session")
        return bool(value and self.app.verify_session_cookie(value.value))

    def session_cookie_value(self) -> str:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        value = cookie.get("fujifilm_session")
        return value.value if value else ""

    def csrf_token(self) -> str:
        session_cookie = self.session_cookie_value()
        return self.app.csrf_token(session_cookie) if session_cookie else ""

    def send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def send_text(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
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
        if not hmac.compare_digest(form.get("auth_token", ""), self.csrf_token()):
            raise ValueError("表单已过期，请刷新页面后重试。")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/robots.txt":
            self.send_text("User-agent: *\nDisallow: /\n")
            return
        if parsed.path == "/logout":
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            secure = "; Secure" if self.app.secure_cookie else ""
            self.send_header("Set-Cookie", f"fujifilm_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/{secure}")
            self.end_headers()
            return
        if self.path.startswith("/login"):
            self.send_html(self.login_page(""))
            return
        if parsed.path in {"/admin", "/logs"} and not self.is_authenticated():
            self.redirect("/login")
            return
        if parsed.path == "/logs":
            self.send_html(self.page("日志", f"<pre>{html.escape(self.app.logs())}</pre><p><a href='/admin'>返回后台</a></p>"))
            return
        if parsed.path == "/admin":
            self.send_html(self.dashboard(admin=True))
            return
        self.send_html(self.dashboard(admin=False))

    def do_POST(self) -> None:  # noqa: N802
        try:
            form = self.read_form()
            if self.path.startswith("/login"):
                admin_key = form.get("admin_key", "")
                if self.app.verify_admin_key(admin_key):
                    session_cookie = self.app.make_session_cookie()
                    secure = "; Secure" if self.app.secure_cookie else ""
                    self.send_response(HTTPStatus.SEE_OTHER)
                    self.send_header("Location", "/admin")
                    self.send_header(
                        "Set-Cookie",
                        f"fujifilm_session={session_cookie}; Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Strict; Path=/{secure}",
                    )
                    self.end_headers()
                    return
                self.send_html(self.login_page("通行密钥不正确。"), HTTPStatus.UNAUTHORIZED)
                return
            if not self.is_authenticated():
                self.redirect("/login")
                return
            self.require_post_token(form)
            action = form.get("action", "")
            if action == "add":
                name = self.app.add_url(
                    form.get("url", ""),
                    form.get("name", ""),
                    form.get("require_text", ""),
                    form.get("interval", ""),
                    form.get("jitter", ""),
                    form.get("policy", "monthly"),
                )
                self.redirect("/admin?msg=" + urllib.parse.quote(f"已添加：{name}"))
            elif action == "delete":
                name = self.app.delete_task(form.get("index", ""))
                self.redirect("/admin?msg=" + urllib.parse.quote(f"已删除任务：{name}"))
            elif action == "clear-notified":
                name = self.app.clear_notified(form.get("index", ""))
                self.redirect("/admin?msg=" + urllib.parse.quote(f"已清空本月去重：{name}"))
            elif action in {"start", "stop", "restart"}:
                code, output = self.app.systemctl(action)
                msg = f"{action}: {'成功' if code == 0 else '失败'} {output}"
                self.redirect("/admin?msg=" + urllib.parse.quote(msg))
            elif action == "check":
                code, output = self.app.check_once()
                title = "检查完成" if code == 0 else "检查失败"
                self.send_html(self.page(title, f"<pre>{html.escape(output)}</pre><p><a href='/admin'>返回后台</a></p>"))
            else:
                raise ValueError("未知操作。")
        except ValueError as exc:
            self.send_html(self.page("操作失败", f"<p class='error'>{html.escape(str(exc))}</p><p><a href='/admin'>返回后台</a></p>"), HTTPStatus.BAD_REQUEST)

    def login_page(self, error: str) -> str:
        error_html = f"<p class='error'>{html.escape(error)}</p>" if error else ""
        return self.page(
            "登录",
            f"""
            {error_html}
            <form method="post" action="/login" class="panel">
              <label>后台通行密钥<input type="password" name="admin_key" autocomplete="current-password" autofocus></label>
              <button type="submit">登录</button>
            </form>
            """,
        )

    def dashboard(self, admin: bool) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        msg = query.get("msg", [""])[0]
        msg_html = f"<p class='ok'>{html.escape(msg)}</p>" if msg else ""
        tasks = self.app.tasks()
        total_products = sum(int(task["total"]) for task in tasks)
        total_in_stock = sum(len(task["in_stock"]) for task in tasks)
        last_checks = [str(task["checked_at"]) for task in tasks if task["checked_at"]]
        latest_check = max(last_checks) if last_checks else "尚无"
        summary = f"""
        <section class="summary">
          <div><span>任务</span><strong>{len(tasks)}</strong></div>
          <div><span>商品</span><strong>{total_products}</strong></div>
          <div><span>有货</span><strong>{total_in_stock}</strong></div>
          <div><span>最近检查</span><strong>{html.escape(latest_check)}</strong></div>
          {f"<div><span>服务</span><strong>{html.escape(self.app.service_status())}</strong></div>" if admin else ""}
        </section>
        """
        rows = []
        for task in tasks:
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
                    {f"<dt>本月已推送</dt><dd>{task['notified_count']} 个</dd>" if admin else ""}
                    {f"<dt>连续失败</dt><dd>{html.escape(str(task['consecutive_failures']))}</dd>" if admin else ""}
                    {f"<dt>校验文本</dt><dd>{html.escape(str(task['require_text'] or '不校验'))}</dd>" if admin else ""}
                    {f"<dt>间隔</dt><dd>{html.escape(str(task['interval']))} 秒 + 随机 {html.escape(str(task['jitter']))} 秒</dd>" if admin else ""}
                  </dl>
                  {error if admin else ""}
                  <div>当前有货：{stock_html}</div>
                  {self.task_admin_buttons(task) if admin else ""}
                </section>
                """
            )
        csrf_field = html.escape(self.csrf_token())
        if not admin:
            return self.page(
                "Fujifilm 库存状态",
                f"""
                <section class="hero">
                  <div>
                    <p class="eyebrow">Public Stock Board</p>
                    <h2>Fujifilm Mall 库存状态</h2>
                    <p>这里公开显示当前监控结果，不开放后台操作。</p>
                  </div>
                  <a class="button" href="/admin">后台管理</a>
                </section>
                {summary}
                <section class="task-grid">{''.join(rows) or "<p class='muted'>还没有公开库存数据。</p>"}</section>
                """,
            )
        return self.page(
            "Fujifilm 后台管理",
            f"""
            {msg_html}
            <section class="hero">
              <div>
                <p class="eyebrow">Admin Console</p>
                <h2>后台管理</h2>
                <p>添加监控、调整任务、查看日志和控制服务。</p>
              </div>
              <a class="button secondary" href="/logout">退出登录</a>
            </section>
            {summary}
            <section class="panel">
              <h2>添加监控</h2>
              <form method="post">
                <input type="hidden" name="auth_token" value="{csrf_field}">
                <input type="hidden" name="action" value="add">
                <label>商品或分类链接<input name="url" placeholder="https://mall-jp.fujifilm.com/shop/g/g16587294/" required></label>
                <label>名称<input name="name" placeholder="可选，例如 MINI13"></label>
                <label>分类标题校验<input name="require_text" placeholder="留空表示不校验"></label>
                <label>检查间隔秒数<input name="interval" inputmode="numeric" placeholder="默认"></label>
                <label>随机延迟秒数<input name="jitter" inputmode="numeric" placeholder="默认"></label>
                <label>推送策略
                  <select name="policy">
                    <option value="monthly">每款每月最多一次</option>
                    <option value="stop">首次补货后停止任务</option>
                    <option value="none">不做去重/停止</option>
                  </select>
                </label>
                <button type="submit">开始监控</button>
              </form>
            </section>
            <section class="panel actions">
              <h2>服务控制</h2>
              <p>服务状态：<strong>{html.escape(self.app.service_status())}</strong></p>
              <form method="post">
                <input type="hidden" name="auth_token" value="{csrf_field}">
                <button name="action" value="check">立即检查</button>
                <button name="action" value="restart">重启</button>
                <button name="action" value="stop">暂停</button>
                <button name="action" value="start">恢复</button>
                <a class="button" href="/logs">查看日志</a>
                <a class="button secondary" href="/">公开页</a>
              </form>
            </section>
            <section class="task-grid">{''.join(rows) or "<p class='muted'>还没有监控任务。</p>"}</section>
            """,
        )

    def task_admin_buttons(self, task: dict) -> str:
        csrf_field = html.escape(self.csrf_token())
        index = html.escape(str(task["index"]))
        return f"""
        <form method="post" class="inline-actions">
          <input type="hidden" name="auth_token" value="{csrf_field}">
          <input type="hidden" name="index" value="{index}">
          <button name="action" value="clear-notified">清空本月去重</button>
          <button name="action" value="delete" class="danger">删除任务</button>
        </form>
        """

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
    .hero {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; background: #17202a; color: white; border-radius: 8px; padding: 18px; }}
    .hero h2 {{ margin: 2px 0 6px; font-size: 22px; }} .hero p {{ margin: 0; color: #d7dde8; }}
    .eyebrow {{ font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: #9fb4d8 !important; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }}
    .summary div {{ background: white; border: 1px solid #d9dee7; border-radius: 8px; padding: 14px; }}
    .summary span {{ display: block; color: #667085; font-size: 13px; margin-bottom: 6px; }}
    .summary strong {{ display: block; font-size: 20px; overflow-wrap: anywhere; }}
    .task-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }}
    .card-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
    form {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: end; }}
    label {{ display: grid; gap: 6px; flex: 1 1 260px; font-size: 14px; color: #374151; }}
    input, select {{ border: 1px solid #b8c0cc; border-radius: 6px; padding: 10px 12px; font-size: 15px; background: white; }}
    button, .button {{ border: 0; border-radius: 6px; background: #1f6feb; color: white; padding: 10px 14px; font-size: 14px; text-decoration: none; cursor: pointer; }}
    button[value="stop"] {{ background: #b42318; }} button[value="restart"] {{ background: #875bf7; }}
    .secondary {{ background: #4b5563; }} .danger, button[value="delete"] {{ background: #b42318; }}
    .inline-actions {{ margin-top: 12px; }}
    .public-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
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
    parser.add_argument("--systemctl-scope", choices=("user", "system"), default="user")
    parser.add_argument("--admin-key-hash", default="")
    parser.add_argument("--session-secret", default="")
    parser.add_argument("--secure-cookie", action="store_true")
    parser.add_argument("--token", default="", help=argparse.SUPPRESS)
    parser.add_argument("--allow-no-auth", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    app = WebApp(args)
    if not app.has_login_secret() and not app.allow_no_auth:
        print("FUJIFILM_ADMIN_KEY_HASH is required.", file=sys.stderr)
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
