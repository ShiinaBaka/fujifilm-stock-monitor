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
import shutil
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
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
WEBAUTHN_CHALLENGE_SECONDS = 5 * 60


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


def short_datetime(value: object) -> str:
    text = str(value or "").strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", text)
    if match:
        return f"{match.group(2)}-{match.group(3)} {match.group(4)}:{match.group(5)}"
    return text or "尚无"


def short_link_label(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) >= 3:
        return parts[-1]
    return parsed.netloc or "打开"


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


class CborReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.index = 0

    def read(self):
        if self.index >= len(self.data):
            raise ValueError("Unexpected end of CBOR data.")
        first = self.data[self.index]
        self.index += 1
        major = first >> 5
        info = first & 0x1F
        value = self.read_length(info)
        if major == 0:
            return value
        if major == 1:
            return -1 - value
        if major == 2:
            chunk = self.data[self.index : self.index + value]
            self.index += value
            return chunk
        if major == 3:
            chunk = self.data[self.index : self.index + value]
            self.index += value
            return chunk.decode("utf-8")
        if major == 4:
            return [self.read() for _ in range(value)]
        if major == 5:
            return {self.read(): self.read() for _ in range(value)}
        if major == 7:
            if info == 20:
                return False
            if info == 21:
                return True
            if info == 22:
                return None
        raise ValueError(f"Unsupported CBOR major type: {major}")

    def read_length(self, info: int) -> int:
        if info < 24:
            return info
        sizes = {24: 1, 25: 2, 26: 4, 27: 8}
        if info not in sizes:
            raise ValueError("Unsupported CBOR length.")
        size = sizes[info]
        if self.index + size > len(self.data):
            raise ValueError("Unexpected end of CBOR length.")
        value = int.from_bytes(self.data[self.index : self.index + size], "big")
        self.index += size
        return value


def cbor_decode(data: bytes):
    reader = CborReader(data)
    value = reader.read()
    if reader.index != len(data):
        raise ValueError("Trailing CBOR data.")
    return value


def der_len(length: int) -> bytes:
    if length < 128:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def der(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + der_len(len(body)) + body


def der_seq(*items: bytes) -> bytes:
    return der(0x30, b"".join(items))


def der_oid(oid: str) -> bytes:
    parts = [int(part) for part in oid.split(".")]
    body = bytes([parts[0] * 40 + parts[1]])
    for part in parts[2:]:
        encoded = [part & 0x7F]
        part >>= 7
        while part:
            encoded.append(0x80 | (part & 0x7F))
            part >>= 7
        body += bytes(reversed(encoded))
    return der(0x06, body)


def der_int(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return der(0x02, raw)


def der_bit_string(body: bytes) -> bytes:
    return der(0x03, b"\x00" + body)


def pem_public_key_from_cose(cose_key: dict) -> str:
    kty = cose_key.get(1)
    alg = cose_key.get(3)
    if kty == 2 and alg == -7:
        crv = cose_key.get(-1)
        x = cose_key.get(-2)
        y = cose_key.get(-3)
        if crv != 1 or not isinstance(x, bytes) or not isinstance(y, bytes) or len(x) != 32 or len(y) != 32:
            raise ValueError("Unsupported EC2 public key.")
        spki = der_seq(
            der_seq(der_oid("1.2.840.10045.2.1"), der_oid("1.2.840.10045.3.1.7")),
            der_bit_string(b"\x04" + x + y),
        )
    elif kty == 3 and alg == -257:
        n = cose_key.get(-1)
        e = cose_key.get(-2)
        if not isinstance(n, bytes) or not isinstance(e, bytes):
            raise ValueError("Unsupported RSA public key.")
        rsa_public = der_seq(der_int(int.from_bytes(n, "big")), der_int(int.from_bytes(e, "big")))
        spki = der_seq(
            der_seq(der_oid("1.2.840.113549.1.1.1"), der(0x05, b"")),
            der_bit_string(rsa_public),
        )
    else:
        raise ValueError("Unsupported WebAuthn public key algorithm.")

    body = base64.encodebytes(spki).decode("ascii").replace("\n", "")
    lines = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN PUBLIC KEY-----\n{lines}\n-----END PUBLIC KEY-----\n"


def verify_signature_with_openssl(pem: str, data: bytes, signature: bytes) -> bool:
    if not shutil.which("openssl"):
        raise RuntimeError("openssl is required for passkey signature verification.")
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        public_key = root / "public.pem"
        payload = root / "payload.bin"
        sig = root / "signature.bin"
        public_key.write_text(pem, encoding="utf-8")
        payload.write_bytes(data)
        sig.write_bytes(signature)
        completed = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(public_key), "-signature", str(sig), str(payload)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return completed.returncode == 0


def parse_authenticator_data(auth_data: bytes) -> dict:
    if len(auth_data) < 37:
        raise ValueError("Authenticator data is too short.")
    rp_id_hash = auth_data[:32]
    flags = auth_data[32]
    sign_count = struct.unpack(">I", auth_data[33:37])[0]
    result = {"rp_id_hash": rp_id_hash, "flags": flags, "sign_count": sign_count}
    if flags & 0x40:
        if len(auth_data) < 55:
            raise ValueError("Attested credential data is too short.")
        credential_id_length = struct.unpack(">H", auth_data[53:55])[0]
        credential_start = 55
        credential_end = credential_start + credential_id_length
        credential_id = auth_data[credential_start:credential_end]
        cose_key = cbor_decode(auth_data[credential_end:])
        if not isinstance(cose_key, dict):
            raise ValueError("Invalid COSE public key.")
        result.update({"credential_id": credential_id, "cose_key": cose_key})
    return result


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
        self.rp_id = args.rp_id or os.environ.get("FUJIFILM_WEBAUTHN_RP_ID", "")
        self.rp_name = args.rp_name or os.environ.get("FUJIFILM_WEBAUTHN_RP_NAME", "Fujifilm Stock Monitor")
        self.allowed_origins = [
            item.strip()
            for item in (args.allowed_origin or os.environ.get("FUJIFILM_WEBAUTHN_ORIGIN", "")).split(",")
            if item.strip()
        ]
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

    @property
    def credentials_path(self) -> Path:
        return self.config_dir / "webauthn_credentials.json"

    def effective_rp_id(self, host: str) -> str:
        if self.rp_id:
            return self.rp_id
        return host.split(":", 1)[0]

    def effective_origin(self, host: str) -> str:
        if self.allowed_origins:
            return self.allowed_origins[0]
        scheme = "https" if self.secure_cookie else "http"
        return f"{scheme}://{host}"

    def origin_allowed(self, origin: str, host: str) -> bool:
        allowed = self.allowed_origins or [self.effective_origin(host)]
        return origin in allowed

    def credentials(self) -> dict:
        data = load_json(self.credentials_path)
        credentials = data.get("credentials", {})
        return credentials if isinstance(credentials, dict) else {}

    def save_credentials(self, credentials: dict) -> None:
        write_json(self.credentials_path, {"credentials": credentials})

    def make_challenge_token(self, purpose: str, challenge: str) -> str:
        payload = {
            "purpose": purpose,
            "challenge": challenge,
            "expires": int(time.time()) + WEBAUTHN_CHALLENGE_SECONDS,
        }
        raw = b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = hmac.new(self.session_secret.encode("utf-8"), raw.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{raw}.{signature}"

    def verify_challenge_token(self, token: str, purpose: str) -> str:
        try:
            raw, signature = token.split(".", 1)
            expected = hmac.new(self.session_secret.encode("utf-8"), raw.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError("Challenge signature mismatch.")
            payload = json.loads(b64decode(raw).decode("utf-8"))
            if payload.get("purpose") != purpose or int(payload.get("expires", 0)) < int(time.time()):
                raise ValueError("Challenge expired.")
            return str(payload["challenge"])
        except (ValueError, KeyError, json.JSONDecodeError):
            raise ValueError("Passkey challenge 已过期，请刷新重试。") from None

    def passkey_register_options(self, host: str) -> dict:
        challenge = b64encode(secrets.token_bytes(32))
        credentials = self.credentials()
        return {
            "token": self.make_challenge_token("register", challenge),
            "publicKey": {
                "challenge": challenge,
                "rp": {"name": self.rp_name, "id": self.effective_rp_id(host)},
                "user": {
                    "id": b64encode(hashlib.sha256((self.effective_rp_id(host) + ":admin").encode()).digest()[:16]),
                    "name": "admin",
                    "displayName": "Fujifilm Admin",
                },
                "pubKeyCredParams": [{"type": "public-key", "alg": -7}, {"type": "public-key", "alg": -257}],
                "authenticatorSelection": {"residentKey": "preferred", "userVerification": "required"},
                "attestation": "none",
                "timeout": 60000,
                "excludeCredentials": [{"type": "public-key", "id": credential_id} for credential_id in credentials],
            },
        }

    def passkey_login_options(self, host: str) -> dict:
        challenge = b64encode(secrets.token_bytes(32))
        credentials = self.credentials()
        return {
            "token": self.make_challenge_token("login", challenge),
            "publicKey": {
                "challenge": challenge,
                "rpId": self.effective_rp_id(host),
                "allowCredentials": [{"type": "public-key", "id": credential_id} for credential_id in credentials],
                "userVerification": "required",
                "timeout": 60000,
            },
            "hasCredentials": bool(credentials),
        }

    def verify_registration(self, host: str, payload: dict) -> str:
        expected_challenge = self.verify_challenge_token(str(payload.get("token", "")), "register")
        response = payload.get("response", {})
        client_data = json.loads(b64decode(str(response.get("clientDataJSON", ""))).decode("utf-8"))
        if client_data.get("type") != "webauthn.create":
            raise ValueError("Passkey 注册类型无效。")
        if client_data.get("challenge") != expected_challenge:
            raise ValueError("Passkey challenge 不匹配。")
        if not self.origin_allowed(str(client_data.get("origin", "")), host):
            raise ValueError("Passkey origin 不允许。")
        attestation = cbor_decode(b64decode(str(response.get("attestationObject", ""))))
        if not isinstance(attestation, dict) or not isinstance(attestation.get("authData"), bytes):
            raise ValueError("Passkey attestation 无效。")
        auth_data = parse_authenticator_data(attestation["authData"])
        rp_hash = hashlib.sha256(self.effective_rp_id(host).encode("utf-8")).digest()
        if auth_data["rp_id_hash"] != rp_hash:
            raise ValueError("Passkey RP ID 不匹配。")
        if not (auth_data["flags"] & 0x01) or not (auth_data["flags"] & 0x04):
            raise ValueError("Passkey 需要用户在场和本地验证。")
        credential_id = b64encode(auth_data["credential_id"])
        public_key_pem = pem_public_key_from_cose(auth_data["cose_key"])
        credentials = self.credentials()
        credentials[credential_id] = {
            "name": str(payload.get("name") or f"Passkey {len(credentials) + 1}"),
            "public_key_pem": public_key_pem,
            "sign_count": int(auth_data["sign_count"]),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.save_credentials(credentials)
        return credential_id

    def verify_login(self, host: str, payload: dict) -> str:
        expected_challenge = self.verify_challenge_token(str(payload.get("token", "")), "login")
        credential_id = str(payload.get("id", ""))
        credentials = self.credentials()
        credential = credentials.get(credential_id)
        if not isinstance(credential, dict):
            raise ValueError("Passkey 未注册。")
        response = payload.get("response", {})
        client_data_raw = b64decode(str(response.get("clientDataJSON", "")))
        client_data = json.loads(client_data_raw.decode("utf-8"))
        if client_data.get("type") != "webauthn.get":
            raise ValueError("Passkey 登录类型无效。")
        if client_data.get("challenge") != expected_challenge:
            raise ValueError("Passkey challenge 不匹配。")
        if not self.origin_allowed(str(client_data.get("origin", "")), host):
            raise ValueError("Passkey origin 不允许。")
        authenticator_data = b64decode(str(response.get("authenticatorData", "")))
        parsed_auth = parse_authenticator_data(authenticator_data)
        rp_hash = hashlib.sha256(self.effective_rp_id(host).encode("utf-8")).digest()
        if parsed_auth["rp_id_hash"] != rp_hash:
            raise ValueError("Passkey RP ID 不匹配。")
        if not (parsed_auth["flags"] & 0x01) or not (parsed_auth["flags"] & 0x04):
            raise ValueError("Passkey 需要用户在场和本地验证。")
        signature_base = authenticator_data + hashlib.sha256(client_data_raw).digest()
        signature = b64decode(str(response.get("signature", "")))
        if not verify_signature_with_openssl(str(credential.get("public_key_pem", "")), signature_base, signature):
            raise ValueError("Passkey 签名验证失败。")
        if int(parsed_auth["sign_count"]) > 0:
            credential["sign_count"] = int(parsed_auth["sign_count"])
            self.save_credentials(credentials)
        return credential_id

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

    def notification_settings(self) -> dict[str, str]:
        notifications = self.config().get("notifications", {})
        if not isinstance(notifications, dict):
            notifications = {}
        return {
            "serverchan_sendkey": str(notifications.get("serverchan_sendkey") or ""),
            "ntfy_topic": str(notifications.get("ntfy_topic") or ""),
            "webhook_url": str(notifications.get("webhook_url") or ""),
        }

    def update_notifications(self, form: dict[str, str]) -> str:
        config = self.config()
        notifications = config.setdefault("notifications", {})
        if not isinstance(notifications, dict):
            notifications = {}
            config["notifications"] = notifications
        values = {
            "serverchan_sendkey": form.get("serverchan_sendkey", "").strip(),
            "ntfy_topic": form.get("ntfy_topic", "").strip(),
            "webhook_url": form.get("webhook_url", "").strip(),
        }
        if values["webhook_url"]:
            parsed = urllib.parse.urlparse(values["webhook_url"])
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Webhook URL 必须是 http 或 https 链接。")
        for key, value in values.items():
            if value:
                notifications[key] = value
            else:
                notifications.pop(key, None)
        write_json(self.config_path, config)
        self.restart_service()
        enabled = sum(1 for value in values.values() if value)
        return f"推送设置已保存，已启用 {enabled} 个渠道。"

    def test_notifications(self) -> str:
        settings = self.notification_settings()
        title = "Fujifilm 推送测试"
        message = f"后台推送测试成功。\n\n时间：{datetime.now().isoformat(timespec='seconds')}"
        first_url = "https://mall-jp.fujifilm.com/"
        results = []
        if settings["serverchan_sendkey"]:
            try:
                self.send_serverchan(settings["serverchan_sendkey"], title, message, first_url)
                results.append("Server 酱：成功")
            except Exception as exc:  # noqa: BLE001
                results.append(f"Server 酱：失败 {exc}")
        if settings["ntfy_topic"]:
            try:
                self.send_ntfy(settings["ntfy_topic"], title, message, first_url)
                results.append("ntfy：成功")
            except Exception as exc:  # noqa: BLE001
                results.append(f"ntfy：失败 {exc}")
        if settings["webhook_url"]:
            try:
                self.send_webhook(settings["webhook_url"], title, message)
                results.append("Webhook：成功")
            except Exception as exc:  # noqa: BLE001
                results.append(f"Webhook：失败 {exc}")
        if not results:
            raise ValueError("还没有配置推送渠道。")
        return "；".join(results)

    def send_serverchan(self, sendkey: str, title: str, message: str, first_url: str) -> None:
        body = urllib.parse.urlencode({"title": title, "desp": message + "\n\n" + first_url}).encode("utf-8")
        request = urllib.request.Request(
            f"https://sctapi.ftqq.com/{sendkey}.send",
            data=body,
            headers={"User-Agent": "FujifilmStockWeb/1.0", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15):
            pass

    def send_ntfy(self, topic_or_url: str, title: str, message: str, first_url: str) -> None:
        url = topic_or_url if topic_or_url.startswith(("http://", "https://")) else "https://ntfy.sh/" + topic_or_url.strip("/")
        request = urllib.request.Request(
            url,
            data=(message + "\n" + first_url).encode("utf-8"),
            headers={"User-Agent": "FujifilmStockWeb/1.0", "Title": title, "Tags": "shopping_cart", "Click": first_url},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15):
            pass

    def send_webhook(self, webhook_url: str, title: str, message: str) -> None:
        payload = json.dumps({"title": title, "message": message}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"User-Agent": "FujifilmStockWeb/1.0", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15):
            pass

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

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.end_headers()
        self.wfile.write(encoded)

    def send_javascript(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
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

    def read_json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0") or "0")
        if size > 32768:
            raise ValueError("请求太大。")
        raw = self.rfile.read(size).decode("utf-8", "replace")
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise ValueError("请求格式无效。")
        return data

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
        if parsed.path == "/app.js":
            self.send_javascript(self.app_script())
            return
        if parsed.path == "/webauthn/login/options":
            self.send_json(self.app.passkey_login_options(self.headers.get("Host", "")))
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
            if self.path == "/webauthn/login/verify":
                payload = self.read_json()
                self.app.verify_login(self.headers.get("Host", ""), payload)
                session_cookie = self.app.make_session_cookie()
                secure = "; Secure" if self.app.secure_cookie else ""
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header(
                    "Set-Cookie",
                    f"fujifilm_session={session_cookie}; Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Strict; Path=/{secure}",
                )
                self.end_headers()
                self.wfile.write(b'{"ok":true,"redirect":"/admin"}')
                return
            if self.path == "/webauthn/register/options":
                if not self.is_authenticated():
                    self.send_json({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                    return
                self.send_json(self.app.passkey_register_options(self.headers.get("Host", "")))
                return
            if self.path == "/webauthn/register/verify":
                if not self.is_authenticated():
                    self.send_json({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                    return
                credential_id = self.app.verify_registration(self.headers.get("Host", ""), self.read_json())
                self.send_json({"ok": True, "credential_id": credential_id})
                return
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
            elif action == "save-notifications":
                msg = self.app.update_notifications(form)
                self.redirect("/admin?msg=" + urllib.parse.quote(msg))
            elif action == "test-notifications":
                msg = self.app.test_notifications()
                self.redirect("/admin?msg=" + urllib.parse.quote(msg))
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
            if self.path.startswith("/webauthn/"):
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_html(self.page("操作失败", f"<p class='error'>{html.escape(str(exc))}</p><p><a href='/admin'>返回后台</a></p>"), HTTPStatus.BAD_REQUEST)

    def login_page(self, error: str) -> str:
        error_html = f"<p class='notice error-box'>{html.escape(error)}</p>" if error else ""
        return self.page(
            "登录",
            f"""
            {error_html}
            <section class="auth-shell">
              <div class="hero auth-hero">
                <div>
                  <p class="eyebrow">Admin Access</p>
                  <h2>登录后台</h2>
                  <p>优先使用 Passkey，备用密钥只用于首次注册或应急登录。</p>
                </div>
                <a class="button ghost" href="/">查看公开页</a>
              </div>
              <div class="auth-grid">
                <section class="panel primary-panel">
                  <div class="section-head">
                    <div>
                      <p class="eyebrow">Recommended</p>
                      <h2>Passkey</h2>
                    </div>
                    <span class="badge okb">更方便</span>
                  </div>
                  <p class="muted">使用设备上的 Touch ID、Face ID、Windows Hello 或安全密钥登录。</p>
                  <button type="button" id="passkey-login" class="wide-button">使用 Passkey 登录</button>
                  <p id="passkey-status" class="small muted status-line"></p>
                </section>
                <form method="post" action="/login" class="panel">
                  <div class="section-head">
                    <div>
                      <p class="eyebrow">Fallback</p>
                      <h2>备用密钥</h2>
                    </div>
                  </div>
                  <label>后台备用密钥<input type="password" name="admin_key" autocomplete="current-password" autofocus></label>
                  <button type="submit" class="wide-button secondary">使用备用密钥登录</button>
                </form>
              </div>
            </section>
            """,
        )

    def dashboard(self, admin: bool) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        msg = query.get("msg", [""])[0]
        msg_html = f"<p class='notice ok'>{html.escape(msg)}</p>" if msg else ""
        tasks = self.app.tasks()
        total_products = sum(int(task["total"]) for task in tasks)
        total_in_stock = sum(len(task["in_stock"]) for task in tasks)
        last_checks = [str(task["checked_at"]) for task in tasks if task["checked_at"]]
        latest_check = short_datetime(max(last_checks)) if last_checks else "尚无"
        service_status = self.app.service_status() if admin else ""
        service_badge = ""
        if admin:
            service_class = "okb" if "active" in service_status else "warn"
            service_badge = f"<span class='badge {service_class}'>{html.escape(service_status)}</span>"
        summary_class = "summary admin-summary" if admin else "summary public-summary"
        summary = f"""
        <section class="{summary_class}">
          <div><span>任务</span><strong>{len(tasks)}</strong>{'<small>正在跟踪</small>' if admin else ''}</div>
          <div><span>商品</span><strong>{total_products}</strong>{'<small>已解析</small>' if admin else ''}</div>
          <div><span>有货</span><strong>{total_in_stock}</strong>{'<small>可购买</small>' if admin else ''}</div>
          <div><span>检查</span><strong>{html.escape(latest_check)}</strong>{'<small>最近一次</small>' if admin else ''}</div>
          {f"<div><span>服务</span><strong>{service_badge}</strong><small>systemd 运行状态</small></div>" if admin else ""}
        </section>
        """
        rows = []
        for task in tasks:
            stock_links = "".join(
                f"<a class='stock-chip' href='{html.escape(url)}'>商品 {html.escape(short_link_label(url))}</a>"
                for url in task["in_stock"]
            )
            stock_html = f"<div class='stock-chips'>{stock_links}</div>" if stock_links else "<span class='empty-stock'>暂无有货</span>"
            paused = "<span class='badge warn'>已暂停</span>" if task["paused"] else "<span class='badge okb'>运行中</span>"
            error = f"<div class='task-error'>{html.escape(str(task['last_error']))}</div>" if task["last_error"] else ""
            in_count = len(task["in_stock"])
            total_count = int(task["total"])
            card_class = "card stock-card has-stock" if in_count else "card stock-card"
            admin_metrics = (
                f"""
                  <dl class="metric-list">
                    <dt>本月已推送</dt><dd>{task['notified_count']} 个</dd>
                    <dt>连续失败</dt><dd>{html.escape(str(task['consecutive_failures']))}</dd>
                    <dt>校验文本</dt><dd>{html.escape(str(task['require_text'] or '不校验'))}</dd>
                    <dt>间隔</dt><dd>{html.escape(str(task['interval']))} 秒 + 随机 {html.escape(str(task['jitter']))} 秒</dd>
                  </dl>
                """
                if admin
                else ""
            )
            rows.append(
                f"""
                <section class="{card_class}">
                  <div class="card-head">
                    <div>
                      <h2>{html.escape(task['name'])}</h2>
                      <a class="task-url" href="{html.escape(task['url'])}">来源：{html.escape(short_link_label(task['url']))}</a>
                    </div>
                    {paused}
                  </div>
                  <div class="stock-meter">
                    <div><span>有货</span><strong>{in_count}</strong></div>
                    <div><span>总数</span><strong>{total_count}</strong></div>
                    <div><span>检查</span><strong>{html.escape(short_datetime(task['checked_at']))}</strong></div>
                  </div>
                  {admin_metrics}
                  {error if admin else ""}
                  <div class="stock-block"><strong>当前有货</strong>{stock_html}</div>
                  {self.task_admin_buttons(task) if admin else ""}
                </section>
                """
            )
        csrf_field = html.escape(self.csrf_token())
        if not admin:
            return self.page(
                "Fujifilm 库存状态",
                f"""
                <section class="hero compact-hero">
                  <div>
                    <h2>Fujifilm 库存</h2>
                    <p>{total_in_stock} 个有货 · {len(tasks)} 个任务 · {html.escape(latest_check)} 更新</p>
                  </div>
                  <a class="button" href="/admin">后台管理</a>
                </section>
                {summary}
                <section class="task-grid public-task-grid">{''.join(rows) or "<p class='muted'>还没有公开库存数据。</p>"}</section>
                """,
            )
        notifications = self.app.notification_settings()
        notification_count = sum(1 for value in notifications.values() if value)
        notification_badge = (
            f"<span class='badge okb'>{notification_count} 个已启用</span>"
            if notification_count
            else "<span class='badge warn'>未配置</span>"
        )
        return self.page(
            "Fujifilm 后台管理",
            f"""
            {msg_html}
            <section class="hero">
              <div>
                <p class="eyebrow">Admin Console</p>
                <h2>后台管理</h2>
                <p>先看状态，再做操作。常用操作集中在下面的工具栏里。</p>
              </div>
              <div class="hero-actions">
                <a class="button ghost" href="/">公开页</a>
                <a class="button secondary" href="/logout">退出登录</a>
              </div>
            </section>
            {summary}
            <section class="panel toolbar-panel">
              <div class="section-head">
                <div>
                  <h2>常用操作</h2>
                  <p class="muted">检查库存、查看日志或控制后台服务。</p>
                </div>
                {service_badge}
              </div>
              <form method="post" class="action-bar">
                <input type="hidden" name="auth_token" value="{csrf_field}">
                <button name="action" value="check">立即检查</button>
                <a class="button" href="/logs">查看日志</a>
                <button name="action" value="restart" class="secondary">重启服务</button>
                <button name="action" value="stop" class="danger-light">暂停服务</button>
                <button name="action" value="start" class="success">恢复服务</button>
              </form>
            </section>
            <section class="admin-grid">
            <section class="panel">
              <div class="section-head">
                <div>
                  <h2>添加监控</h2>
                  <p class="muted">粘贴 Fujifilm 商品页或分类页链接即可。</p>
                </div>
              </div>
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
                <button type="submit" class="wide-button">开始监控</button>
              </form>
            </section>
            <section class="panel">
              <div class="section-head">
                <div>
                  <h2>推送服务</h2>
                  <p class="muted">补货、失败报警和测试推送都会使用这里的渠道。</p>
                </div>
                {notification_badge}
              </div>
              <form method="post" class="settings-form">
                <input type="hidden" name="auth_token" value="{csrf_field}">
                <input type="hidden" name="action" value="save-notifications">
                <label>Server 酱 SendKey<input type="password" name="serverchan_sendkey" value="{html.escape(notifications['serverchan_sendkey'])}" autocomplete="off" placeholder="SCT..."></label>
                <label>ntfy 主题或 URL<input name="ntfy_topic" value="{html.escape(notifications['ntfy_topic'])}" placeholder="例如 fujifilm-stock 或 https://ntfy.sh/..."></label>
                <label>Webhook URL<input name="webhook_url" value="{html.escape(notifications['webhook_url'])}" placeholder="https://example.com/webhook"></label>
                <button type="submit" class="wide-button">保存推送设置</button>
              </form>
              <form method="post" class="inline-actions">
                <input type="hidden" name="auth_token" value="{csrf_field}">
                <button name="action" value="test-notifications" class="secondary wide-button">发送测试推送</button>
              </form>
            </section>
            <section class="panel">
              <div class="section-head">
                <div>
                  <h2>Passkey</h2>
                  <p class="muted">把当前设备注册为后台 Passkey。</p>
                </div>
                <span class="badge okb">推荐</span>
              </div>
              <button type="button" id="passkey-register" class="wide-button">注册此设备</button>
              <p id="passkey-status" class="small muted status-line"></p>
            </section>
            </section>
            <div class="section-title"><h2>监控任务</h2><p class="muted">每张卡片都可以单独清空本月去重或删除。</p></div>
            <section class="task-grid">{''.join(rows) or "<p class='muted'>还没有监控任务。</p>"}</section>
            """,
        )

    def task_admin_buttons(self, task: dict) -> str:
        csrf_field = html.escape(self.csrf_token())
        index = html.escape(str(task["index"]))
        return f"""
        <form method="post" class="inline-actions" data-confirm-danger="删除任务后需要重新添加，确定继续吗？">
          <input type="hidden" name="auth_token" value="{csrf_field}">
          <input type="hidden" name="index" value="{index}">
          <button name="action" value="clear-notified" class="secondary">清空本月去重</button>
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
    :root {{ color-scheme: light; --bg: #f4f6f8; --panel: #ffffff; --ink: #17202a; --muted: #64748b; --line: #d8dee8; --blue: #1463ff; --blue-strong: #0b4cc4; --green: #12805c; --red: #b42318; --amber: #a15c00; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; overflow-x: hidden; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); }}
    header {{ background: #111827; color: white; padding: 18px max(20px, calc((100% - 1120px) / 2)); border-bottom: 4px solid #2f80ed; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 22px 20px 34px; display: grid; gap: 18px; }}
    h1 {{ margin: 0; font-size: 23px; }} h2 {{ margin: 0; font-size: 18px; }} p {{ line-height: 1.55; }}
    .panel, .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 18px; box-shadow: 0 1px 2px rgba(16, 24, 40, .04); }}
    .hero {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; background: #17202a; color: white; border-radius: 8px; padding: 20px; }}
    .compact-hero {{ padding: 16px 18px; }}
    .hero h2 {{ margin: 2px 0 6px; font-size: 24px; }} .compact-hero h2 {{ margin: 0; font-size: 22px; }} .hero p {{ margin: 0; color: #d7dde8; }}
    .hero-actions, .action-bar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
    .eyebrow {{ margin: 0 0 4px; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: #86b7ff !important; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }}
    .public-summary {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .summary div {{ background: white; border: 1px solid var(--line); border-radius: 8px; padding: 13px 14px; min-width: 0; }}
    .summary span {{ display: block; color: var(--muted); font-size: 13px; margin-bottom: 6px; }}
    .summary strong {{ display: block; font-size: 20px; overflow-wrap: anywhere; line-height: 1.15; }}
    .public-summary strong {{ font-size: 24px; }}
    .summary small {{ display: block; color: var(--muted); margin-top: 6px; font-size: 12px; }}
    .admin-grid, .auth-grid {{ display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(280px, .85fr); gap: 18px; align-items: start; }}
    .auth-shell {{ display: grid; gap: 18px; }} .auth-hero {{ min-height: 150px; }}
    .task-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }}
    .public-task-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .card-head, .section-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 14px; }}
    .stock-card {{ display: grid; gap: 14px; align-content: start; }}
    .stock-card .card-head {{ margin-bottom: 0; }}
    .stock-card.has-stock {{ border-color: #9ad8ae; box-shadow: 0 0 0 3px rgba(18,128,92,.08); }}
    .task-url {{ display: inline-flex; width: fit-content; margin-top: 8px; border: 1px solid var(--line); border-radius: 999px; padding: 4px 9px; color: #475467; font-size: 12px; text-decoration: none; background: #f8fafc; }}
    .task-url:hover {{ border-color: #b8c0cc; background: #eef4ff; color: #0b4cc4; }}
    form {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: end; }}
    label {{ display: grid; gap: 6px; flex: 1 1 240px; font-size: 14px; color: #374151; }}
    input, select {{ width: 100%; border: 1px solid #b8c0cc; border-radius: 6px; padding: 11px 12px; font-size: 15px; background: white; }}
    input:focus, select:focus {{ outline: 3px solid rgba(20, 99, 255, .18); border-color: var(--blue); }}
    button, .button {{ display: inline-flex; align-items: center; justify-content: center; min-height: 40px; border: 0; border-radius: 6px; background: var(--blue); color: white; padding: 10px 14px; font-size: 14px; font-weight: 650; text-decoration: none; cursor: pointer; }}
    button:hover, .button:hover {{ background: var(--blue-strong); }}
    .secondary {{ background: #475467; }} .secondary:hover {{ background: #344054; }}
    .ghost {{ background: rgba(255,255,255,.14); border: 1px solid rgba(255,255,255,.24); }} .ghost:hover {{ background: rgba(255,255,255,.22); }}
    .success {{ background: var(--green); }} .success:hover {{ background: #0f684d; }}
    .danger, button[value="delete"] {{ background: var(--red); }} .danger:hover, button[value="delete"]:hover {{ background: #8f1c13; }}
    .danger-light {{ background: #fff1ef; color: var(--red); border: 1px solid #f4b8b0; }} .danger-light:hover {{ background: #ffe4e0; }}
    .wide-button {{ width: 100%; }}
    .inline-actions {{ margin-top: 2px; padding-top: 14px; border-top: 1px solid var(--line); }}
    .toolbar-panel form {{ margin-top: 12px; }}
    .public-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
    a {{ color: #0969da; word-break: break-all; }} .metric-list {{ display: grid; grid-template-columns: 112px 1fr; gap: 8px 12px; margin: 0; padding-top: 12px; border-top: 1px solid var(--line); }}
    dt {{ color: #5f6b7a; }} dd {{ margin: 0; min-width: 0; }}
    pre {{ white-space: pre-wrap; background: #111827; color: #e5e7eb; border-radius: 8px; padding: 14px; overflow: auto; }}
    .badge {{ display: inline-flex; align-items: center; min-height: 26px; border-radius: 999px; padding: 4px 9px; font-size: 13px; white-space: nowrap; }} .warn {{ background: #fff0c2; color: #7a4b00; }} .okb {{ background: #dff8e7; color: #116329; }}
    .notice {{ margin: 0; padding: 12px 14px; border-radius: 8px; }} .ok {{ background: #dff8e7; border: 1px solid #9ad8ae; }} .error, .error-box {{ color: var(--red); }} .error-box {{ background: #fff1ef; border: 1px solid #f4b8b0; }}
    .task-error {{ margin: 12px 0; color: var(--red); background: #fff1ef; border: 1px solid #f4b8b0; border-radius: 8px; padding: 10px; font-size: 13px; }}
    .stock-meter {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }}
    .stock-meter div {{ border-radius: 8px; background: #f8fafc; border: 1px solid #edf1f7; padding: 10px; min-width: 0; }}
    .stock-meter span {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 4px; }}
    .stock-meter strong {{ display: block; font-size: 18px; line-height: 1.15; overflow-wrap: anywhere; }}
    .has-stock .stock-meter div:first-child {{ background: #eaf8ef; border-color: #bee8cc; color: #116329; }}
    .stock-block {{ display: grid; gap: 8px; }}
    .stock-block > strong {{ font-size: 14px; }}
    .stock-chips {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .stock-chip {{ display: inline-flex; align-items: center; min-height: 34px; border-radius: 999px; padding: 7px 11px; background: #eaf2ff; color: #0b4cc4; text-decoration: none; font-weight: 650; word-break: normal; }}
    .empty-stock {{ color: var(--muted); background: #f8fafc; border: 1px dashed #cfd7e3; border-radius: 8px; padding: 12px; text-align: center; }}
    .section-title {{ display: flex; justify-content: space-between; align-items: end; gap: 12px; flex-wrap: wrap; }}
    .section-title p, .section-head p {{ margin: 4px 0 0; }}
    .small {{ margin-top: 8px; font-size: 13px; }} .status-line {{ min-height: 18px; }}
    .muted {{ color: var(--muted); }}
    @media (max-width: 900px) {{ .public-summary, .public-task-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
    @media (max-width: 760px) {{ main {{ padding: 16px 12px 28px; }} .admin-grid, .auth-grid, .public-summary, .task-grid, .public-task-grid {{ grid-template-columns: 1fr; }} .metric-list {{ grid-template-columns: 1fr; }} button, .button {{ width: 100%; }} .hero {{ align-items: stretch; }} .hero-actions {{ width: 100%; }} .stock-meter {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }} }}
  </style>
</head>
<body>
  <header><h1>{html.escape(title)}</h1></header>
  <main>{body}</main>
  <script src="/app.js"></script>
</body>
</html>"""

    def app_script(self) -> str:
        return r"""
function b64ToBuf(value) {
  const padded = value + "=".repeat((4 - value.length % 4) % 4);
  const raw = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes.buffer;
}

function bufToB64(buffer) {
  const bytes = new Uint8Array(buffer);
  let raw = "";
  for (const byte of bytes) raw += String.fromCharCode(byte);
  return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function setPasskeyStatus(text, isError) {
  const el = document.getElementById("passkey-status");
  if (!el) return;
  el.textContent = text;
  el.className = isError ? "small error" : "small muted";
}

async function passkeyLogin() {
  try {
    if (!window.PublicKeyCredential) throw new Error("当前浏览器不支持 Passkey。");
    setPasskeyStatus("正在请求 Passkey...", false);
    const options = await (await fetch("/webauthn/login/options")).json();
    if (!options.hasCredentials) throw new Error("还没有注册 Passkey，请先用备用密钥登录后台注册。");
    const publicKey = options.publicKey;
    publicKey.challenge = b64ToBuf(publicKey.challenge);
    publicKey.allowCredentials = publicKey.allowCredentials.map((item) => ({...item, id: b64ToBuf(item.id)}));
    const credential = await navigator.credentials.get({publicKey});
    const payload = {
      token: options.token,
      id: credential.id,
      response: {
        authenticatorData: bufToB64(credential.response.authenticatorData),
        clientDataJSON: bufToB64(credential.response.clientDataJSON),
        signature: bufToB64(credential.response.signature),
        userHandle: credential.response.userHandle ? bufToB64(credential.response.userHandle) : ""
      }
    };
    const result = await (await fetch("/webauthn/login/verify", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    })).json();
    if (!result.ok) throw new Error(result.error || "Passkey 登录失败。");
    location.href = result.redirect || "/admin";
  } catch (error) {
    setPasskeyStatus(error.message || String(error), true);
  }
}

async function passkeyRegister() {
  try {
    if (!window.PublicKeyCredential) throw new Error("当前浏览器不支持 Passkey。");
    setPasskeyStatus("正在创建 Passkey...", false);
    const options = await (await fetch("/webauthn/register/options", {method: "POST"})).json();
    const publicKey = options.publicKey;
    publicKey.challenge = b64ToBuf(publicKey.challenge);
    publicKey.user.id = b64ToBuf(publicKey.user.id);
    publicKey.excludeCredentials = publicKey.excludeCredentials.map((item) => ({...item, id: b64ToBuf(item.id)}));
    const credential = await navigator.credentials.create({publicKey});
    const payload = {
      token: options.token,
      id: credential.id,
      name: navigator.platform || "Passkey",
      response: {
        attestationObject: bufToB64(credential.response.attestationObject),
        clientDataJSON: bufToB64(credential.response.clientDataJSON)
      }
    };
    const result = await (await fetch("/webauthn/register/verify", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    })).json();
    if (!result.ok) throw new Error(result.error || "Passkey 注册失败。");
    setPasskeyStatus("Passkey 已注册。下次可以直接用 Passkey 登录。", false);
  } catch (error) {
    setPasskeyStatus(error.message || String(error), true);
  }
}

document.getElementById("passkey-login")?.addEventListener("click", passkeyLogin);
document.getElementById("passkey-register")?.addEventListener("click", passkeyRegister);

document.querySelectorAll("[data-confirm-danger]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    const submitter = event.submitter;
    if (!submitter || submitter.value !== "delete") return;
    if (!confirm(form.dataset.confirmDanger || "确定继续吗？")) event.preventDefault();
  });
});
"""


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
    parser.add_argument("--rp-id", default="")
    parser.add_argument("--rp-name", default="")
    parser.add_argument("--allowed-origin", default="")
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
