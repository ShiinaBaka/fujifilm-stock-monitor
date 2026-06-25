#!/usr/bin/env python3
"""
Monitor Fujifilm Mall film stock and notify when items restock.

Default target:
https://mall-jp.fujifilm.com/shop/c/c306010/
"""

from __future__ import annotations

import argparse
import http.cookiejar
import html
import json
import os
import platform
import random
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


DEFAULT_URL = "https://mall-jp.fujifilm.com/shop/c/c306010/"
DEFAULT_REQUIRE_TEXT = "チェキ用フィルム"
DEFAULT_STATE = Path(__file__).with_name(".fujifilm_stock_state.json")
DEFAULT_TASK_NAME = "Fujifilm"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
COOKIE_JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIE_JAR))
IPV4_FORCED = False


@dataclass
class Product:
    name: str
    url: str
    price: str
    sold_out: bool

    @property
    def in_stock(self) -> bool:
        return not self.sold_out


class MonitoringComplete(Exception):
    pass


def get_attr(tag: str, attr_name: str) -> str:
    match = re.search(rf'\b{re.escape(attr_name)}\s*=\s*(["\'])(.*?)\1', tag, re.I | re.S)
    return html.unescape(match.group(2)).strip() if match else ""


def strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return " ".join(html.unescape(text).split())


def has_category_heading(page_html: str, expected: str) -> bool:
    pattern = re.compile(
        r'<h1\b[^>]*\bclass\s*=\s*(["\'])[^"\']*\bcategory_name_\b[^"\']*\1[^>]*>(.*?)</h1>',
        re.I | re.S,
    )
    for match in pattern.finditer(page_html):
        if strip_tags(match.group(2)) == expected:
            return True
    return False


def fetch_page(url: str, timeout: int) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://mall-jp.fujifilm.com/shop/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Upgrade-Insecure-Requests": "1",
        },
    )
    with OPENER.open(request, timeout=timeout) as response:
        raw = response.read()
        content_type = response.headers.get("content-type", "")

    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    encoding = match.group(1) if match else "shift_jis"
    return raw.decode(encoding, errors="replace")


def force_ipv4() -> None:
    global IPV4_FORCED
    if IPV4_FORCED:
        return
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = getaddrinfo_ipv4
    IPV4_FORCED = True


def parse_products(page_html: str, base_url: str) -> list[Product]:
    products: list[Product] = []
    pattern = re.compile(
        r'(<a\b(?=[^>]*\bclass\s*=\s*(["\'])[^"\']*\bgoodsitem_\b[^"\']*\2)[^>]*>)(.*?)</a>',
        re.I | re.S,
    )

    for match in pattern.finditer(page_html):
        start_tag, _, body = match.groups()
        name = get_attr(start_tag, "title")
        href = get_attr(start_tag, "href")
        price_match = re.search(
            r'<p\b[^>]*\bclass\s*=\s*(["\'])[^"\']*\bprice_\b[^"\']*\1[^>]*>(.*?)</p>',
            body,
            re.I | re.S,
        )
        price = strip_tags(price_match.group(2)) if price_match else ""
        sold_out = bool(re.search(r'\bclass\s*=\s*(["\'])[^"\']*\bsoldout_\b', body, re.I))

        if name:
            products.append(
                Product(
                    name=name,
                    url=urllib.request.urljoin(base_url, href),
                    price=price,
                    sold_out=sold_out,
                )
            )

    return products


def is_product_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc == "mall-jp.fujifilm.com" and parsed.path.startswith("/shop/g/")


def parse_single_product(page_html: str, url: str) -> Product:
    name = ""
    for pattern in (
        r'<meta\b[^>]*\bproperty\s*=\s*(["\'])og:title\1[^>]*>',
        r'<meta\b[^>]*\bname\s*=\s*(["\'])twitter:title\1[^>]*>',
    ):
        match = re.search(pattern, page_html, re.I | re.S)
        if match:
            name = get_attr(match.group(0), "content")
            if name:
                break
    if not name:
        title_match = re.search(r"<title\b[^>]*>(.*?)</title>", page_html, re.I | re.S)
        if title_match:
            name = strip_tags(title_match.group(1))
    if not name:
        heading_match = re.search(r"<h1\b[^>]*>(.*?)</h1>", page_html, re.I | re.S)
        if heading_match:
            name = strip_tags(heading_match.group(1))
    if not name:
        name = url.rstrip("/").split("/")[-1]
    name = re.sub(r"\s*\|\s*FUJIFILM.*$", "", name).strip()

    price = ""
    price_match = re.search(
        r'<[^>]*\bclass\s*=\s*(["\'])[^"\']*\bprice_\b[^"\']*\1[^>]*>(.*?)</[^>]+>',
        page_html,
        re.I | re.S,
    )
    if price_match:
        price = strip_tags(price_match.group(2))

    sold_out = bool(
        re.search(r'\bclass\s*=\s*(["\'])[^"\']*\bsoldout_\b', page_html, re.I)
        or re.search(r"在庫なし|品切れ|販売終了|SOLD\s*OUT", strip_tags(page_html), re.I)
    )
    return Product(name=name, url=url, price=price, sold_out=sold_out)


def load_state(path: Path) -> dict[str, bool]:
    data = load_full_state(path)
    return {str(k): bool(v) for k, v in data.get("stock", {}).items()}


def load_full_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def save_state(path: Path, products: Iterable[Product]) -> None:
    previous = load_full_state(path)
    payload = {
        **previous,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "consecutive_failures": 0,
        "last_failure_alert_count": 0,
        "last_error": "",
        "stock": {product.url: product.in_stock for product in products},
    }
    write_state(path, payload)


def save_failure(path: Path, error: str) -> tuple[int, int]:
    previous = load_full_state(path)
    failures = int(previous.get("consecutive_failures", 0)) + 1
    last_alert_count = int(previous.get("last_failure_alert_count", 0))
    payload = {
        **previous,
        "last_failed_at": datetime.now().isoformat(timespec="seconds"),
        "consecutive_failures": failures,
        "last_error": error,
    }
    write_state(path, payload)
    return failures, last_alert_count


def mark_failure_alerted(path: Path, failures: int) -> None:
    previous = load_full_state(path)
    previous["last_failure_alert_count"] = failures
    write_state(path, previous)


def current_month() -> str:
    return datetime.now().strftime("%Y-%m")


def monthly_marker_path(directory: Path) -> Path:
    return directory / f"{current_month()}.done"


def load_monthly_notified(marker: Path) -> set[str]:
    data = load_full_state(marker)
    notified = data.get("notified", {})
    if isinstance(notified, dict):
        return {str(url) for url in notified}
    if isinstance(notified, list):
        return {str(url) for url in notified}
    legacy_in_stock = data.get("in_stock", [])
    if isinstance(legacy_in_stock, list):
        return {str(url) for url in legacy_in_stock}
    return set()


def should_skip_monitoring(args: argparse.Namespace) -> str | None:
    if args.stop_marker and args.stop_marker.exists():
        return f"Permanent stop marker exists: {args.stop_marker}"
    return None


def mark_restock_notified(args: argparse.Namespace, products: list[Product]) -> None:
    notified_at = datetime.now().isoformat(timespec="seconds")
    payload = {
        "updated_at": notified_at,
        "url": args.url,
        "task": args.task_name,
        "notified": {
            product.url: {
                "name": product.name,
                "price": product.price,
                "notified_at": notified_at,
            }
            for product in products
        },
    }
    if args.stop_marker:
        write_state(args.stop_marker, payload)
    if args.monthly_marker_dir:
        marker = monthly_marker_path(args.monthly_marker_dir)
        previous = load_full_state(marker)
        previous_notified = previous.get("notified", {})
        if not isinstance(previous_notified, dict):
            previous_notified = {
                url: {"notified_at": str(previous.get("completed_at", ""))}
                for url in load_monthly_notified(marker)
            }
        previous_notified.update(payload["notified"])
        payload["notified"] = previous_notified
        write_state(marker, payload)


def mac_notify(title: str, message: str, sound: str | None = "Glass") -> None:
    if platform.system() != "Darwin" or not os.environ.get("DISPLAY"):
        return
    script = 'display notification ' f'{json.dumps(message)} ' f'with title {json.dumps(title)}'
    if sound:
        script += f" sound name {json.dumps(sound)}"
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def webhook_notify(webhook_url: str, title: str, message: str) -> None:
    body = json.dumps({"text": f"{title}\n{message}"}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15):
        pass


def ntfy_notify(topic_or_url: str, title: str, message: str, first_url: str) -> None:
    if topic_or_url.startswith("http://") or topic_or_url.startswith("https://"):
        url = topic_or_url
    else:
        url = "https://ntfy.sh/" + topic_or_url.strip("/")

    body_text = message if first_url in message else message + "\n" + first_url
    body = body_text.encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Title": title,
            "Tags": "shopping_cart",
            "Click": first_url,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15):
        pass


def serverchan_notify(sendkey: str, title: str, message: str, first_url: str) -> None:
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    desp = message if first_url in message else message + "\n\n" + first_url
    body = urllib.parse.urlencode(
        {
            "title": title,
            "desp": desp,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15):
        pass


def alert(
    task_name: str,
    products: list[Product],
    webhook_url: str | None,
    ntfy_topic: str | None,
    serverchan_sendkey: str | None,
    open_first: bool,
) -> bool:
    first = products[0]
    title = f"Fujifilm 补货：{task_name}"
    lines = [
        f"监控任务：{task_name}",
        f"发现时间：{now()}",
        f"有货数量：{len(products)}",
        "",
    ]
    lines.extend(f"{p.name} {p.price}\n{p.url}".strip() for p in products[:5])
    if len(products) > 5:
        lines.append(f"另有 {len(products) - 5} 个商品有货。")
    message = "\n".join(lines)

    print(f"[{now()}] RESTOCK: {message}", flush=True)
    print(first.url, flush=True)
    mac_notify(title, message)
    remote_channels = 0
    remote_successes = 0

    if webhook_url:
        remote_channels += 1
        try:
            webhook_notify(webhook_url, title, message if first.url in message else message + "\n" + first.url)
            remote_successes += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[{now()}] Webhook notification failed: {exc}", file=sys.stderr, flush=True)

    if ntfy_topic:
        remote_channels += 1
        try:
            ntfy_notify(ntfy_topic, title, message, first.url)
            remote_successes += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[{now()}] ntfy notification failed: {exc}", file=sys.stderr, flush=True)

    if serverchan_sendkey:
        remote_channels += 1
        try:
            serverchan_notify(serverchan_sendkey, title, message, first.url)
            remote_successes += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[{now()}] ServerChan notification failed: {exc}", file=sys.stderr, flush=True)

    if open_first:
        try:
            subprocess.run(["open", first.url], check=False, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
    if remote_channels and not remote_successes:
        print(
            f"[{now()}] all configured restock notification channels failed; "
            "restock marker was not updated",
            file=sys.stderr,
            flush=True,
        )
        return False
    return True


def notify_failure(
    args: argparse.Namespace,
    error: str,
    failures: int,
    last_alert_count: int,
) -> None:
    if failures < args.failure_alert_after:
        return
    if last_alert_count and failures < last_alert_count + args.failure_alert_repeat:
        return

    title = f"Fujifilm 监控可能失效：{args.task_name}"
    message = (
        f"监控任务：{args.task_name}\n"
        f"连续失败：{failures} 次\n"
        f"错误：{error}\n"
        f"监控 URL：{args.url}"
    )
    print(f"[{now()}] FAILURE ALERT: {message}", file=sys.stderr, flush=True)

    if args.webhook_url:
        try:
            webhook_notify(args.webhook_url, title, message)
        except Exception as exc:  # noqa: BLE001
            print(f"[{now()}] Webhook failure notification failed: {exc}", file=sys.stderr, flush=True)

    if args.ntfy_topic:
        try:
            ntfy_notify(args.ntfy_topic, title, message, args.url)
        except Exception as exc:  # noqa: BLE001
            print(f"[{now()}] ntfy failure notification failed: {exc}", file=sys.stderr, flush=True)

    if args.serverchan_sendkey:
        try:
            serverchan_notify(args.serverchan_sendkey, title, message, args.url)
        except Exception as exc:  # noqa: BLE001
            print(f"[{now()}] ServerChan failure notification failed: {exc}", file=sys.stderr, flush=True)

    mark_failure_alerted(args.state_file, failures)


def notify_status(args: argparse.Namespace, products: list[Product]) -> None:
    in_stock = [product for product in products if product.in_stock]
    title = f"Fujifilm 每日运行状态：{args.task_name}"
    lines = [
        f"监控任务：{args.task_name}",
        f"检查时间：{now()}",
        f"商品总数：{len(products)}",
        f"当前有货：{len(in_stock)}",
        f"当前缺货：{len(products) - len(in_stock)}",
        f"监控 URL：{args.url}",
    ]
    if in_stock:
        lines.append("")
        lines.extend(f"{product.name} {product.price}\n{product.url}".strip() for product in in_stock[:5])
    message = "\n".join(lines)
    first_url = in_stock[0].url if in_stock else args.url

    if args.webhook_url:
        webhook_notify(args.webhook_url, title, message)
    if args.ntfy_topic:
        ntfy_notify(args.ntfy_topic, title, message, first_url)
    if args.serverchan_sendkey:
        serverchan_notify(args.serverchan_sendkey, title, message, first_url)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_check(args: argparse.Namespace) -> int:
    if args.ipv4:
        force_ipv4()

    if args.html_file:
        page_html = Path(args.html_file).read_text(encoding=args.html_encoding)
    else:
        page_html = fetch_page(args.url, args.timeout)

    if args.require_text and not has_category_heading(page_html, args.require_text):
        raise RuntimeError(f"Required category heading not found: {args.require_text}")

    products = parse_products(page_html, args.url)
    if not products and is_product_url(args.url):
        products = [parse_single_product(page_html, args.url)]
    if args.name_regex:
        pattern = re.compile(args.name_regex)
        products = [product for product in products if pattern.search(product.name)]

    if not products:
        raise RuntimeError("No products found. The page layout may have changed.")

    previous = load_state(args.state_file)
    monthly_notified: set[str] = set()
    if args.monthly_marker_dir:
        monthly_notified = load_monthly_notified(monthly_marker_path(args.monthly_marker_dir))
        previous_state = load_full_state(args.state_file)
        checked_at = str(previous_state.get("checked_at", ""))
        if not checked_at.startswith(current_month()):
            previous = {product.url: False for product in products}
    restocked = [
        product
        for product in products
        if product.in_stock and (args.alert_on_first_run or previous.get(product.url) is False)
        and product.url not in monthly_notified
    ]

    save_state(args.state_file, products)

    in_stock = [product for product in products if product.in_stock]
    sold_out_count = len(products) - len(in_stock)
    print(
        f"[{now()}] {args.task_name}: checked {len(products)} products: "
        f"{len(in_stock)} in stock, {sold_out_count} sold out",
        flush=True,
    )

    if args.print_products:
        for product in products:
            status = "IN STOCK" if product.in_stock else "sold out"
            print(f"  - {status}: {product.name} {product.price}".strip(), flush=True)

    if restocked:
        notified = alert(
            args.task_name,
            restocked,
            args.webhook_url,
            args.ntfy_topic,
            args.serverchan_sendkey,
            args.open,
        )
        if notified and (args.stop_marker or args.monthly_marker_dir):
            mark_restock_notified(args, restocked)
        if notified and args.stop_marker:
            raise MonitoringComplete("Restock notified; monitoring paused by policy.")
    if args.status_notify:
        notify_status(args, products)
    return 0


def run_once_with_handling(args: argparse.Namespace) -> int:
    skip_reason = should_skip_monitoring(args)
    if skip_reason:
        print(f"[{now()}] {args.task_name}: monitoring skipped: {skip_reason}", flush=True)
        return 0
    try:
        run_check(args)
        return 0
    except MonitoringComplete as exc:
        print(f"[{now()}] {args.task_name}: {exc}", flush=True)
        return 0
    except KeyboardInterrupt:
        raise
    except (RuntimeError, urllib.error.URLError, TimeoutError) as exc:
        error = str(exc)
        failures, last_alert_count = save_failure(args.state_file, error)
        print(
            f"[{now()}] {args.task_name}: check failed ({failures} consecutive): {error}",
            file=sys.stderr,
            flush=True,
        )
        notify_failure(args, error, failures, last_alert_count)
        return 1


def load_config(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Config file must contain a JSON object.")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise RuntimeError("Config file must contain a non-empty tasks list.")
    return data


def path_from_config(value: str | None, base_dir: Path) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def task_args_from_config(config: dict, task: dict, config_path: Path, cli_args: argparse.Namespace) -> argparse.Namespace:
    if not isinstance(task, dict):
        raise RuntimeError("Each task in config must be an object.")
    config_dir = config_path.parent
    defaults = config.get("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
    notifications = config.get("notifications", {})
    if not isinstance(notifications, dict):
        notifications = {}

    name = str(task.get("name") or DEFAULT_TASK_NAME)
    state_default = f"state/{re.sub(r'[^A-Za-z0-9_.-]+', '_', name).strip('_') or 'task'}.json"
    state_file = path_from_config(str(task.get("state_file") or defaults.get("state_file") or state_default), config_dir)
    monthly_marker_dir = path_from_config(task.get("monthly_marker_dir") or defaults.get("monthly_marker_dir"), config_dir)
    stop_marker = path_from_config(task.get("stop_marker") or defaults.get("stop_marker"), config_dir)

    return argparse.Namespace(
        task_name=name,
        url=str(task.get("url") or defaults.get("url") or DEFAULT_URL),
        require_text=str(task.get("require_text") or defaults.get("require_text") or DEFAULT_REQUIRE_TEXT),
        interval=int(task.get("interval", defaults.get("interval", 3600))),
        jitter=int(task.get("jitter", defaults.get("jitter", 600))),
        once=cli_args.once,
        state_file=state_file or DEFAULT_STATE,
        stop_marker=stop_marker,
        monthly_marker_dir=monthly_marker_dir,
        timeout=int(task.get("timeout", defaults.get("timeout", 20))),
        name_regex=task.get("name_regex") or defaults.get("name_regex"),
        webhook_url=notifications.get("webhook_url") or os.environ.get("STOCK_WEBHOOK_URL"),
        ntfy_topic=notifications.get("ntfy_topic") or os.environ.get("STOCK_NTFY_TOPIC"),
        serverchan_sendkey=notifications.get("serverchan_sendkey") or os.environ.get("SERVERCHAN_SENDKEY"),
        ipv4=bool(task.get("ipv4", defaults.get("ipv4", True))),
        failure_alert_after=int(task.get("failure_alert_after", defaults.get("failure_alert_after", 3))),
        failure_alert_repeat=int(task.get("failure_alert_repeat", defaults.get("failure_alert_repeat", 24))),
        open=bool(task.get("open", defaults.get("open", False))),
        status_notify=bool(task.get("status_notify", defaults.get("status_notify", False))),
        alert_on_first_run=bool(task.get("alert_on_first_run", defaults.get("alert_on_first_run", False))),
        print_products=cli_args.print_products,
        html_file=task.get("html_file"),
        html_encoding=str(task.get("html_encoding", defaults.get("html_encoding", "shift_jis"))),
    )


def run_config(config_path: Path, cli_args: argparse.Namespace) -> int:
    config = load_config(config_path)
    tasks = [task_args_from_config(config, task, config_path, cli_args) for task in config["tasks"]]
    if cli_args.once:
        return 1 if any(run_once_with_handling(task) for task in tasks) else 0

    next_runs = {i: 0.0 for i in range(len(tasks))}
    while True:
        now_ts = time.time()
        ran = False
        for i, task in enumerate(tasks):
            if now_ts < next_runs[i]:
                continue
            try:
                run_once_with_handling(task)
            except KeyboardInterrupt:
                print("\nStopped.", flush=True)
                return 130
            delay = max(task.interval, 10) + (random.randint(0, task.jitter) if task.jitter > 0 else 0)
            next_runs[i] = time.time() + delay
            ran = True
        if not ran:
            time.sleep(max(min(next_runs.values()) - time.time(), 1))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor Fujifilm Mall category stock and notify on restock."
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--task-name", default=DEFAULT_TASK_NAME)
    parser.add_argument("--config", type=Path, help="JSON config file with one or more monitor tasks.")
    parser.add_argument("--require-text", default=DEFAULT_REQUIRE_TEXT)
    parser.add_argument("--interval", type=int, default=3600, help="Seconds between checks.")
    parser.add_argument("--jitter", type=int, default=600, help="Random extra seconds added between checks.")
    parser.add_argument("--once", action="store_true", help="Run one check and exit.")
    parser.add_argument("--status-notify", action="store_true", help="Send a status summary after a successful check.")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--stop-marker",
        type=Path,
        help="Permanently stop after the first restock notification.",
    )
    parser.add_argument(
        "--monthly-marker-dir",
        type=Path,
        help="Send at most one restock notification per product per calendar month.",
    )
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--name-regex", help="Only monitor products whose name matches this regex.")
    parser.add_argument("--webhook-url", default=os.environ.get("STOCK_WEBHOOK_URL"))
    parser.add_argument("--ntfy-topic", default=os.environ.get("STOCK_NTFY_TOPIC"))
    parser.add_argument("--serverchan-sendkey", default=os.environ.get("SERVERCHAN_SENDKEY"))
    parser.add_argument("--ipv4", action="store_true", help="Force IPv4 DNS resolution.")
    parser.add_argument(
        "--failure-alert-after",
        type=int,
        default=3,
        help="Notify after this many consecutive failed checks.",
    )
    parser.add_argument(
        "--failure-alert-repeat",
        type=int,
        default=24,
        help="After the first failure alert, notify again every N more failed checks.",
    )
    parser.add_argument("--open", action="store_true", help="Open the first restocked product page.")
    parser.add_argument(
        "--alert-on-first-run",
        action="store_true",
        help="Notify if an item is already in stock and no previous state exists.",
    )
    parser.add_argument("--print-products", action="store_true")
    parser.add_argument("--html-file", help=argparse.SUPPRESS)
    parser.add_argument("--html-encoding", default="shift_jis", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.config:
        try:
            return run_config(args.config.expanduser(), args)
        except RuntimeError as exc:
            print(f"[{now()}] config error: {exc}", file=sys.stderr, flush=True)
            return 2

    while True:
        try:
            failed = bool(run_once_with_handling(args))
        except KeyboardInterrupt:
            print("\nStopped.", flush=True)
            return 130

        if args.once:
            return 1 if failed else 0
        delay = max(args.interval, 10) + (random.randint(0, args.jitter) if args.jitter > 0 else 0)
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
