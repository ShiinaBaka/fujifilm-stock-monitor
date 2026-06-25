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
import fujifilm_web as web


class WebTests(unittest.TestCase):
    def test_validate_fujifilm_url(self) -> None:
        self.assertEqual(
            web.validate_fujifilm_url("https://mall-jp.fujifilm.com/shop/g/g16587294/?x=1"),
            "https://mall-jp.fujifilm.com/shop/g/g16587294/",
        )
        with self.assertRaises(ValueError):
            web.validate_fujifilm_url("https://example.com/shop/g/g16587294/")
        with self.assertRaises(ValueError):
            web.validate_fujifilm_url("http://mall-jp.fujifilm.com/shop/g/g16587294/")

    def test_add_url_writes_config_task(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "config.json"
            config.write_text(json.dumps({"defaults": {"interval": 3600}, "tasks": []}))
            app = web.WebApp(
                argparse.Namespace(
                    config=config,
                    service_name="fujifilm-stock-monitor.service",
                    install_dir=root,
                    token="",
                    admin_key_hash=web.make_admin_key_hash("secret"),
                    session_secret="session-secret",
                    secure_cookie=False,
                    allow_no_auth=False,
                    systemctl_scope="user",
                )
            )
            with mock.patch.object(app, "restart_service", return_value=(0, "")):
                name = app.add_url("https://mall-jp.fujifilm.com/shop/g/g16587294/", "mini 1P")
            self.assertEqual(name, "mini 1P")
            data = json.loads(config.read_text())
            self.assertEqual(data["tasks"][0]["url"], "https://mall-jp.fujifilm.com/shop/g/g16587294/")
            self.assertEqual(data["tasks"][0]["require_text"], "")
            self.assertTrue(data["tasks"][0]["alert_on_first_run"])

    def test_add_url_can_use_stop_policy_and_custom_interval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "config.json"
            config.write_text(json.dumps({"tasks": []}))
            app = web.WebApp(
                argparse.Namespace(
                    config=config,
                    service_name="fujifilm-stock-monitor.service",
                    install_dir=root,
                    token="",
                    admin_key_hash=web.make_admin_key_hash("secret"),
                    session_secret="session-secret",
                    secure_cookie=False,
                    allow_no_auth=False,
                    systemctl_scope="user",
                )
            )
            with mock.patch.object(app, "restart_service", return_value=(0, "")):
                app.add_url(
                    "https://mall-jp.fujifilm.com/shop/c/cmini13/",
                    "MINI13",
                    "",
                    "7200",
                    "900",
                    "stop",
                )
            task = json.loads(config.read_text())["tasks"][0]
            self.assertEqual(task["interval"], 7200)
            self.assertEqual(task["jitter"], 900)
            self.assertEqual(task["require_text"], "")
            self.assertIn("stop_marker", task)
            self.assertNotIn("monthly_marker_dir", task)

    def test_admin_key_hash_verification(self) -> None:
        encoded = web.make_admin_key_hash("open sesame")
        self.assertTrue(web.verify_admin_key_hash("open sesame", encoded))
        self.assertFalse(web.verify_admin_key_hash("wrong", encoded))

    def test_session_cookie_verification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app = web.WebApp(
                argparse.Namespace(
                    config=Path(td) / "config.json",
                    service_name="fujifilm-stock-monitor.service",
                    install_dir=Path(td),
                    token="",
                    admin_key_hash=web.make_admin_key_hash("secret"),
                    session_secret="session-secret",
                    secure_cookie=False,
                    allow_no_auth=False,
                    systemctl_scope="user",
                )
            )
            cookie = app.make_session_cookie()
            self.assertTrue(app.verify_session_cookie(cookie))
            self.assertFalse(app.verify_session_cookie(cookie + "x"))


if __name__ == "__main__":
    unittest.main()
