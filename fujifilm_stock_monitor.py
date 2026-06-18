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
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
COOKIE_JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIE_JAR))


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
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = getaddrinfo_ipv4


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
    script = 'display notification ' f'{json.dumps(message)} ' f'with title {json.dumps(title)}'
    if sound:
        script += f" sound name {json.dumps(sound)}"
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
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

    body = (message + "\n" + first_url).encode("utf-8")
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
    body = urllib.parse.urlencode(
        {
            "title": title,
            "desp": message + "\n\n" + first_url,
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
    products: list[Product],
    webhook_url: str | None,
    ntfy_topic: str | None,
    serverchan_sendkey: str | None,
    open_first: bool,
) -> None:
    first = products[0]
    title = "Fujifilm stock available"
    lines = [f"{p.name} {p.price}".strip() for p in products[:5]]
    if len(products) > 5:
        lines.append(f"...and {len(products) - 5} more")
    message = "\n".join(lines)

    print(f"[{now()}] RESTOCK: {message}", flush=True)
    print(first.url, flush=True)
    mac_notify(title, message)

    if webhook_url:
        try:
            webhook_notify(webhook_url, title, message + "\n" + first.url)
        except Exception as exc:  # noqa: BLE001
            print(f"[{now()}] Webhook notification failed: {exc}", file=sys.stderr, flush=True)

    if ntfy_topic:
        try:
            ntfy_notify(ntfy_topic, title, message, first.url)
        except Exception as exc:  # noqa: BLE001
            print(f"[{now()}] ntfy notification failed: {exc}", file=sys.stderr, flush=True)

    if serverchan_sendkey:
        try:
            serverchan_notify(serverchan_sendkey, title, message, first.url)
        except Exception as exc:  # noqa: BLE001
            print(f"[{now()}] ServerChan notification failed: {exc}", file=sys.stderr, flush=True)

    if open_first:
        try:
            subprocess.run(["open", first.url], check=False, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass


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

    title = "Fujifilm monitor failure"
    message = f"Consecutive failures: {failures}\nError: {error}\nURL: {args.url}"
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
        f"[{now()}] checked {len(products)} products: "
        f"{len(in_stock)} in stock, {sold_out_count} sold out",
        flush=True,
    )

    if args.print_products:
        for product in products:
            status = "IN STOCK" if product.in_stock else "sold out"
            print(f"  - {status}: {product.name} {product.price}".strip(), flush=True)

    if restocked:
        alert(restocked, args.webhook_url, args.ntfy_topic, args.serverchan_sendkey, args.open)
        if args.stop_marker or args.monthly_marker_dir:
            mark_restock_notified(args, restocked)
        if args.stop_marker:
            raise MonitoringComplete("Restock notified; monitoring paused by policy.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor Fujifilm Mall category stock and notify on restock."
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--require-text", default=DEFAULT_REQUIRE_TEXT)
    parser.add_argument("--interval", type=int, default=3600, help="Seconds between checks.")
    parser.add_argument("--jitter", type=int, default=600, help="Random extra seconds added between checks.")
    parser.add_argument("--once", action="store_true", help="Run one check and exit.")
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

    while True:
        skip_reason = should_skip_monitoring(args)
        if skip_reason:
            print(f"[{now()}] monitoring skipped: {skip_reason}", flush=True)
            return 0
        failed = False
        try:
            run_check(args)
        except MonitoringComplete as exc:
            print(f"[{now()}] {exc}", flush=True)
            return 0
        except KeyboardInterrupt:
            print("\nStopped.", flush=True)
            return 130
        except (RuntimeError, urllib.error.URLError, TimeoutError) as exc:
            failed = True
            error = str(exc)
            failures, last_alert_count = save_failure(args.state_file, error)
            print(
                f"[{now()}] check failed ({failures} consecutive): {error}",
                file=sys.stderr,
                flush=True,
            )
            notify_failure(args, error, failures, last_alert_count)

        if args.once:
            return 1 if failed else 0
        delay = max(args.interval, 10) + (random.randint(0, args.jitter) if args.jitter > 0 else 0)
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
