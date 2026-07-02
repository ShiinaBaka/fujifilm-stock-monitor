#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fujifilm_web as web


class WebTests(unittest.TestCase):
    def test_write_json_preserves_existing_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text("{}")
            os.chmod(path, 0o640)
            web.write_json(path, {"tasks": []})
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

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
                    rp_id="",
                    rp_name="",
                    allowed_origin="",
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
                    rp_id="",
                    rp_name="",
                    allowed_origin="",
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

    def test_update_notifications_writes_and_clears_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "config.json"
            config.write_text(json.dumps({"notifications": {"serverchan_sendkey": "old"}, "tasks": []}))
            app = web.WebApp(
                argparse.Namespace(
                    config=config,
                    service_name="fujifilm-stock-monitor.service",
                    install_dir=root,
                    token="",
                    admin_key_hash=web.make_admin_key_hash("secret"),
                    session_secret="session-secret",
                    secure_cookie=False,
                    rp_id="",
                    rp_name="",
                    allowed_origin="",
                    allow_no_auth=False,
                    systemctl_scope="user",
                )
            )
            with mock.patch.object(app, "restart_service", return_value=(0, "")):
                app.update_notifications(
                    {
                        "serverchan_sendkey": "SCT123",
                        "ntfy_topic": "fujifilm-topic",
                        "webhook_url": "https://example.com/hook",
                    }
                )
                settings = app.notification_settings()
                self.assertEqual(settings["serverchan_sendkey"], "SCT123")
                self.assertEqual(settings["ntfy_topic"], "fujifilm-topic")
                self.assertEqual(settings["webhook_url"], "https://example.com/hook")

                app.update_notifications({"serverchan_sendkey": "", "ntfy_topic": "", "webhook_url": ""})
                data = json.loads(config.read_text())
                self.assertEqual(data["notifications"], {})

    def test_update_notifications_rejects_invalid_webhook(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app = web.WebApp(
                argparse.Namespace(
                    config=root / "config.json",
                    service_name="fujifilm-stock-monitor.service",
                    install_dir=root,
                    token="",
                    admin_key_hash=web.make_admin_key_hash("secret"),
                    session_secret="session-secret",
                    secure_cookie=False,
                    rp_id="",
                    rp_name="",
                    allowed_origin="",
                    allow_no_auth=False,
                    systemctl_scope="user",
                )
            )
            with self.assertRaises(ValueError):
                app.update_notifications({"serverchan_sendkey": "", "ntfy_topic": "", "webhook_url": "not-a-url"})

    def test_tasks_exposes_cached_product_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state.json"
            state.write_text(json.dumps({
                "stock": {"https://mall-jp.fujifilm.com/shop/g/g16587294/": True},
                "products": {"https://mall-jp.fujifilm.com/shop/g/g16587294/": {
                    "name": "instax mini", "price": "990円", "image_url": "https://mall-jp.fujifilm.com/img/goods/S/16587294.jpg"
                }},
                "stock_history": [{
                    "detected_at": "2026-07-02T12:00:00", "task": "mini", "name": "instax mini",
                    "price": "990円", "url": "https://mall-jp.fujifilm.com/shop/g/g16587294/",
                    "image_url": "https://mall-jp.fujifilm.com/img/goods/S/16587294.jpg"
                }],
            }))
            config = root / "config.json"
            config.write_text(json.dumps({"tasks": [{"name": "mini", "state_file": "state.json"}]}))
            app = web.WebApp(argparse.Namespace(
                config=config, service_name="test.service", install_dir=root, token="", admin_key_hash="", session_secret="test",
                secure_cookie=False, rp_id="", rp_name="", allowed_origin="", allow_no_auth=True, systemctl_scope="user",
            ))
            product = app.tasks()[0]["in_stock"][0]
            self.assertEqual(product["name"], "instax mini")
            self.assertEqual(product["price"], "990円")
            self.assertEqual(product["image_url"], "https://mall-jp.fujifilm.com/img/goods/S/16587294.jpg")
            self.assertEqual(app.tasks()[0]["history"][0]["name"], "instax mini")

    def test_tasks_derives_thumbnail_for_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state.json"
            state.write_text(json.dumps({"stock": {"https://mall-jp.fujifilm.com/shop/g/g16587294/": True}}))
            config = root / "config.json"
            config.write_text(json.dumps({"tasks": [{"state_file": "state.json"}]}))
            app = web.WebApp(argparse.Namespace(
                config=config, service_name="test.service", install_dir=root, token="", admin_key_hash="", session_secret="test",
                secure_cookie=False, rp_id="", rp_name="", allowed_origin="", allow_no_auth=True, systemctl_scope="user",
            ))
            self.assertEqual(
                app.tasks()[0]["in_stock"][0]["image_url"],
                "https://mall-jp.fujifilm.com/img/goods/S/16587294.jpg",
            )

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
                    rp_id="",
                    rp_name="",
                    allowed_origin="",
                    allow_no_auth=False,
                    systemctl_scope="user",
                )
            )
            cookie = app.make_session_cookie()
            self.assertTrue(app.verify_session_cookie(cookie))
            self.assertFalse(app.verify_session_cookie(cookie + "x"))

    def test_challenge_token_roundtrip(self) -> None:
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
                    rp_id="",
                    rp_name="",
                    allowed_origin="",
                    allow_no_auth=False,
                    systemctl_scope="user",
                )
            )
            token = app.make_challenge_token("login", "abc")
            self.assertEqual(app.verify_challenge_token(token, "login"), "abc")
            with self.assertRaises(ValueError):
                app.verify_challenge_token(token, "register")

    def test_cbor_decode_simple_map(self) -> None:
        self.assertEqual(web.cbor_decode(bytes.fromhex("a201020363616263")), {1: 2, 3: "abc"})


if __name__ == "__main__":
    unittest.main()
