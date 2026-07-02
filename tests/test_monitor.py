#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fujifilm_stock_monitor as monitor


ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
BASE_URL = "https://mall-jp.fujifilm.com/shop/c/c306010/"


def args(tmp: Path, html_name: str, **overrides) -> argparse.Namespace:
    data = {
        "task_name": "mini 相纸",
        "url": BASE_URL,
        "require_text": "チェキ用フィルム",
        "interval": 3600,
        "jitter": 0,
        "once": True,
        "state_file": tmp / "state.json",
        "stop_marker": None,
        "monthly_marker_dir": None,
        "timeout": 20,
        "name_regex": None,
        "webhook_url": None,
        "ntfy_topic": None,
        "serverchan_sendkey": None,
        "ipv4": False,
        "failure_alert_after": 3,
        "failure_alert_repeat": 24,
        "open": False,
        "status_notify": False,
        "alert_on_first_run": False,
        "print_products": False,
        "html_file": str(FIXTURES / html_name),
        "html_encoding": "utf-8",
    }
    data.update(overrides)
    return argparse.Namespace(**data)


class MonitorTests(unittest.TestCase):
    def test_parse_soldout_fixture(self) -> None:
        html = (FIXTURES / "mini_soldout.html").read_text()
        products = monitor.parse_products(html, BASE_URL)
        self.assertEqual(len(products), 2)
        self.assertFalse(products[0].in_stock)

    def test_product_metadata_is_saved_for_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            products = monitor.parse_products((FIXTURES / "mini_one_stock.html").read_text(), BASE_URL)
            monitor.save_state(path, products)
            state = json.loads(path.read_text())
            metadata = state["products"]["https://mall-jp.fujifilm.com/shop/g/g16587294/"]
            self.assertEqual(metadata["name"], "チェキ専用フィルム 1パック")
            self.assertEqual(metadata["image_url"], "https://mall-jp.fujifilm.com/img/goods/S/16587294.jpg")

    def test_stock_history_records_transition_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            check = args(tmp, "mini_one_stock.html", alert_on_first_run=True)
            monitor.run_check(check)
            monitor.run_check(check)
            history = json.loads(check.state_file.read_text())["stock_history"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["task"], "mini 相纸")
            self.assertEqual(history[0]["name"], "チェキ専用フィルム 1パック")

    def test_alert_on_first_run_does_not_repeat_without_policy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            check = args(tmp, "mini_one_stock.html", alert_on_first_run=True)
            with mock.patch.object(monitor, "alert", return_value=True) as mocked_alert:
                monitor.run_check(check)
                monitor.run_check(check)
            self.assertEqual(mocked_alert.call_count, 1)

    def test_parse_single_product_page(self) -> None:
        in_stock = monitor.parse_single_product(
            (FIXTURES / "product_in_stock.html").read_text(),
            "https://mall-jp.fujifilm.com/shop/g/g16587294/",
        )
        self.assertEqual(in_stock.name, "チェキ専用フィルム 1パック")
        self.assertTrue(in_stock.in_stock)
        self.assertEqual(in_stock.image_url, "https://mall-jp.fujifilm.com/img/goods/L/16587294.jpg")

        sold_out = monitor.parse_single_product(
            (FIXTURES / "product_soldout.html").read_text(),
            "https://mall-jp.fujifilm.com/shop/g/g16587295/",
        )
        self.assertFalse(sold_out.in_stock)

    def test_monthly_marker_suppresses_same_product_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            monthly = tmp / "monthly"
            first = args(tmp, "mini_one_stock.html", monthly_marker_dir=monthly, alert_on_first_run=True)
            monitor.run_check(first)
            marker = monitor.monthly_marker_path(monthly)
            self.assertTrue(marker.exists())

            duplicate = args(tmp, "mini_one_stock.html", state_file=tmp / "fresh.json", monthly_marker_dir=monthly, alert_on_first_run=True)
            with mock.patch.object(monitor, "alert", wraps=monitor.alert) as mocked_alert:
                monitor.run_check(duplicate)
            mocked_alert.assert_not_called()

            second = args(tmp, "mini_two_stock.html", state_file=tmp / "second.json", monthly_marker_dir=monthly, alert_on_first_run=True)
            with mock.patch.object(monitor, "alert", wraps=monitor.alert) as mocked_alert:
                monitor.run_check(second)
            mocked_alert.assert_called_once()
            notified = json.loads(marker.read_text())["notified"]
            self.assertIn("https://mall-jp.fujifilm.com/shop/g/g16531958/", notified)

    def test_failed_remote_notification_does_not_write_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            monthly = tmp / "monthly"
            item = args(
                tmp,
                "mini_one_stock.html",
                monthly_marker_dir=monthly,
                alert_on_first_run=True,
                webhook_url="http://127.0.0.1:1/fail",
            )
            with mock.patch.object(monitor, "webhook_notify", side_effect=OSError("boom")):
                monitor.run_check(item)
            self.assertFalse(monitor.monthly_marker_path(monthly).exists())

    def test_config_runs_multiple_tasks_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = tmp / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "name": "mini-a",
                                "html_file": str(FIXTURES / "mini_soldout.html"),
                                "url": BASE_URL,
                                "require_text": "チェキ用フィルム",
                                "state_file": "a.json",
                                "html_encoding": "utf-8",
                            },
                            {
                                "name": "mini-b",
                                "html_file": str(FIXTURES / "mini_one_stock.html"),
                                "url": BASE_URL,
                                "require_text": "チェキ用フィルム",
                                "state_file": "b.json",
                                "html_encoding": "utf-8",
                            },
                        ]
                    },
                    ensure_ascii=False,
                )
            )
            cli = argparse.Namespace(once=True, print_products=False)
            self.assertEqual(monitor.run_config(config, cli), 0)
            self.assertTrue((tmp / "a.json").exists())
            self.assertTrue((tmp / "b.json").exists())

    def test_config_empty_require_text_is_respected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            config = {
                "defaults": {"require_text": "チェキ用フィルム"},
                "tasks": [
                    {
                        "name": "product",
                        "url": "https://mall-jp.fujifilm.com/shop/g/g16587294/",
                        "require_text": "",
                        "state_file": "product.json",
                    }
                ],
            }
            cli = argparse.Namespace(once=True, print_products=False)
            task = monitor.task_args_from_config(config, config["tasks"][0], tmp / "config.json", cli)
            self.assertEqual(task.require_text, "")


if __name__ == "__main__":
    unittest.main()
