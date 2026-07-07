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
    existing_stat = path.stat() if path.exists() else None
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    if existing_stat:
        os.chmod(tmp, existing_stat.st_mode & 0o777)
        os.chown(tmp, existing_stat.st_uid, existing_stat.st_gid)
    else:
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


def trusted_image_url(url: object) -> str:
    value = str(url or "").strip()
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme == "https" and parsed.netloc == FUJIFILM_HOST:
        return value
    return ""


def proxied_image_url(url: object) -> str:
    trusted = trusted_image_url(url)
    if not trusted:
        return ""
    return "/image?u=" + urllib.parse.quote(trusted, safe="")


def fallback_product_image_url(url: str) -> str:
    match = re.search(r"/shop/g/g(\d+)/?", urllib.parse.urlparse(url).path)
    return f"https://{FUJIFILM_HOST}/img/goods/S/{match.group(1)}.jpg" if match else ""


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
            catalog = state.get("products", {}) if isinstance(state.get("products"), dict) else {}
            history_data = state.get("stock_history", []) if isinstance(state.get("stock_history"), list) else []
            history = []
            for item in history_data:
                if not isinstance(item, dict) or not item.get("url"):
                    continue
                item_url = str(item["url"])
                history.append(
                    {
                        "detected_at": str(item.get("detected_at") or ""),
                        "task": str(item.get("task") or name),
                        "name": str(item.get("name") or f"商品 {product_id_from_url(item_url)}"),
                        "price": str(item.get("price") or ""),
                        "url": item_url,
                        "image_url": trusted_image_url(item.get("image_url")) or fallback_product_image_url(item_url),
                    }
                )
            in_stock = []
            for url, value in stock.items():
                if not value:
                    continue
                details = catalog.get(url, {}) if isinstance(catalog.get(url), dict) else {}
                in_stock.append(
                    {
                        "url": str(url),
                        "name": str(details.get("name") or f"商品 {product_id_from_url(str(url))}"),
                        "price": str(details.get("price") or ""),
                        "image_url": trusted_image_url(details.get("image_url")) or fallback_product_image_url(str(url)),
                    }
                )
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
                    "history": history,
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

    def send_image_proxy(self, image_url: str) -> None:
        url = trusted_image_url(image_url)
        if not url:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 FujifilmStockMonitor/1.0",
                "Referer": f"https://{FUJIFILM_HOST}/",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                content_type = response.headers.get_content_type()
                if not content_type.startswith("image/"):
                    raise ValueError("unexpected image content type")
                data = response.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError("image too large")
        except (OSError, ValueError):
            data = (
                b'<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">'
                b'<rect width="96" height="96" rx="10" fill="#f4f5f6"/>'
                b'<path d="M28 58h40L57 43 48 54l-7-8-13 12Z" fill="#c7ccd3"/>'
                b'<circle cx="35" cy="35" r="6" fill="#c7ccd3"/>'
                b'</svg>'
            )
            content_type = "image/svg+xml"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

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
        if parsed.path == "/image":
            image_url = urllib.parse.parse_qs(parsed.query).get("u", [""])[0]
            self.send_image_proxy(image_url)
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
            self.send_html(
                self.page(
                    "运行日志",
                    f"""
                    <section class="page-heading">
                      <div><p class="page-kicker">系统记录</p><h1>运行日志</h1></div>
                      <a class="button secondary" href="/admin">返回控制台</a>
                    </section>
                    <section class="panel console-panel"><pre>{html.escape(self.app.logs())}</pre></section>
                    """,
                )
            )
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
                self.send_html(self.login_page("备用密钥不正确。"), HTTPStatus.UNAUTHORIZED)
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
                action_name = {"start": "恢复服务", "stop": "暂停服务", "restart": "重启服务"}[action]
                msg = f"{action_name}{'成功' if code == 0 else '失败'}：{output}"
                self.redirect("/admin?msg=" + urllib.parse.quote(msg))
            elif action == "check":
                code, output = self.app.check_once()
                title = "检查完成" if code == 0 else "检查失败"
                self.send_html(
                    self.page(
                        title,
                        f"""
                        <section class="page-heading">
                          <div><p class="page-kicker">即时检查</p><h1>{title}</h1></div>
                          <a class="button secondary" href="/admin">返回控制台</a>
                        </section>
                        <section class="panel console-panel"><pre>{html.escape(output)}</pre></section>
                        """,
                    )
                )
            else:
                raise ValueError("未知操作。")
        except ValueError as exc:
            if self.path.startswith("/webauthn/"):
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_html(
                self.page(
                    "操作失败",
                    f"""
                    <section class="page-heading">
                      <div><p class="page-kicker">请求未完成</p><h1>操作失败</h1></div>
                      <a class="button secondary" href="/admin">返回控制台</a>
                    </section>
                    <p class="notice error-box">{html.escape(str(exc))}</p>
                    """,
                ),
                HTTPStatus.BAD_REQUEST,
            )

    def login_page(self, error: str) -> str:
        error_html = f"<p class='notice error-box'>{html.escape(error)}</p>" if error else ""
        return self.page(
            "后台登录",
            f"""
            <section class="auth-shell">
              <section class="panel auth-panel">
                <div class="auth-title">
                  <span class="auth-symbol" aria-hidden="true">P</span>
                  <p class="page-kicker">安全访问</p>
                  <h1>监控控制台</h1>
                  <p>使用 Passkey 登录</p>
                </div>
                {error_html}
                <button type="button" id="passkey-login" class="wide-button auth-primary">继续使用 Passkey</button>
                <p id="passkey-status" class="small muted status-line"></p>
                <details class="backup-access">
                  <summary>使用备用密钥</summary>
                  <form method="post" action="/login" class="backup-form">
                    <label>备用密钥<input type="password" name="admin_key" autocomplete="current-password"></label>
                    <button type="submit" class="wide-button secondary">登录</button>
                  </form>
                </details>
                <a class="auth-back" href="/">返回库存页</a>
              </section>
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
        stock_history = sorted(
            (item for task in tasks for item in task["history"]),
            key=lambda item: item["detected_at"],
            reverse=True,
        )[:12]
        last_checks = [str(task["checked_at"]) for task in tasks if task["checked_at"]]
        latest_check = short_datetime(max(last_checks)) if last_checks else "尚无"
        service_status = self.app.service_status() if admin else ""
        service_badge = ""
        service_label = ""
        if admin:
            service_ok = "active" in service_status
            service_class = "okb" if service_ok else "warn"
            service_label = "正常" if service_ok else "异常"
            service_badge = f"<span class='badge {service_class}'><i></i>{service_label}</span>"
        summary = (
            f"""
            <section class="summary admin-summary" aria-label="运行概览">
              <div class="stock-summary"><span>当前有货</span><strong>{total_in_stock}</strong></div>
              <div><span>监控服务</span><strong class="service-value {service_class}">{service_label}</strong></div>
              <div><span>最近更新</span><strong class="time-value">{html.escape(latest_check)}</strong></div>
            </section>
            """
            if admin
            else ""
        )
        rows = []
        for task in tasks:
            stock_items = []
            for product in task["in_stock"]:
                product_image = proxied_image_url(product["image_url"])
                image_html = (
                    f"<img src='{html.escape(product_image)}' alt='' loading='lazy' decoding='async' onerror='this.remove()'>"
                    if product_image
                    else "<span class='product-placeholder' aria-hidden='true'>商品</span>"
                )
                price_html = f"<small>{html.escape(product['price'])}</small>" if product["price"] else ""
                stock_items.append(
                    f"""
                    <a class='stock-item' href='{html.escape(product['url'])}' target='_blank' rel='noopener'>
                      <span class='product-thumb'>{image_html}</span>
                      <span class='product-copy'><strong>{html.escape(product['name'])}</strong>{price_html}</span>
                      <span class='open-mark' aria-hidden='true'>↗</span>
                    </a>
                    """
                )
            stock_html = (
                f"<div class='stock-block-title'>有货商品</div><div class='stock-items'>{''.join(stock_items)}</div>"
                if stock_items
                else "<div class='availability-line'><i></i><span>暂未补货</span></div>"
            )
            paused = (
                "<span class='badge warn'><i></i>已暂停</span>"
                if task["paused"]
                else "<span class='badge okb'><i></i>监控中</span>"
            )
            error = f"<div class='task-error'>{html.escape(str(task['last_error']))}</div>" if task["last_error"] else ""
            in_count = len(task["in_stock"])
            total_count = int(task["total"])
            card_class = "card stock-card has-stock" if in_count else "card stock-card"
            admin_metrics = (
                f"""
                <details class="task-details">
                  <summary>任务设置</summary>
                  <dl class="metric-list">
                    <dt>本月提醒</dt><dd>{task['notified_count']} 次</dd>
                    <dt>连续失败</dt><dd>{html.escape(str(task['consecutive_failures']))}</dd>
                    <dt>标题校验</dt><dd>{html.escape(str(task['require_text'] or '关闭'))}</dd>
                    <dt>检查周期</dt><dd>{html.escape(str(task['interval']))} 秒，浮动 {html.escape(str(task['jitter']))} 秒</dd>
                  </dl>
                </details>
                """
                if admin
                else ""
            )
            if not admin:
                inventory_class = "available" if in_count else "empty"
                inventory_text = f"{in_count} 件有货" if in_count else "暂无有货"
                public_products = f"<div class='monitor-products'>{stock_html}</div>" if in_count else ""
                rows.append(
                    f"""
                    <section class="monitor-row {inventory_class}">
                      <div class="monitor-identity">
                        <h2>{html.escape(task['name'])}</h2>
                        <a class="task-url" href="{html.escape(task['url'])}" target="_blank" rel="noopener">查看分类 ↗</a>
                      </div>
                      <div class="monitor-state">
                        <strong>{inventory_text}</strong>
                        <small>{total_count} 件商品 · {html.escape(short_datetime(task['checked_at']))} 更新</small>
                      </div>
                      {"<span class='badge warn'><i></i>已暂停</span>" if task["paused"] else ""}
                      {public_products}
                    </section>
                    """
                )
                continue
            rows.append(
                f"""
                <section class="{card_class}">
                  <div class="card-head">
                    <div>
                      <h2>{html.escape(task['name'])}</h2>
                      <a class="task-url" href="{html.escape(task['url'])}" target="_blank" rel="noopener">打开分类 <span>{html.escape(short_link_label(task['url']))}</span> ↗</a>
                    </div>
                    {paused}
                  </div>
                  <div class="stock-meter">
                    <div><span>有货</span><strong>{in_count}</strong></div>
                    <div><span>商品</span><strong>{total_count}</strong></div>
                    <div><span>更新</span><strong>{html.escape(short_datetime(task['checked_at']))}</strong></div>
                  </div>
                  {admin_metrics}
                  {error if admin else ""}
                  <div class="stock-block">{stock_html}</div>
                  {self.task_admin_buttons(task) if admin else ""}
                </section>
                """
            )
        history_rows = []
        for item in stock_history:
            history_image = proxied_image_url(item["image_url"])
            image_html = (
                f"<img src='{html.escape(history_image)}' alt='' loading='lazy' decoding='async' onerror='this.remove()'>"
                if history_image
                else "<span class='product-placeholder' aria-hidden='true'>商品</span>"
            )
            meta = html.escape(item["task"])
            if item["price"]:
                meta += f" · {html.escape(item['price'])}"
            history_rows.append(
                f"""
                <a class="history-item" href="{html.escape(item['url'])}" target="_blank" rel="noopener">
                  <span class="product-thumb">{image_html}</span>
                  <span class="history-copy">
                    <strong>{html.escape(item['name'])}</strong>
                    <small>{meta}</small>
                  </span>
                  <time>{html.escape(short_datetime(item['detected_at']))}</time>
                </a>
                """
            )
        history_html = f"""
        <section class="panel history-panel">
          <div class="section-head">
            <div><p class="section-kicker">库存变化</p><h2>补货记录</h2></div>
            <span class="count-label">{len(stock_history)} 条</span>
          </div>
          <div class="history-list">{''.join(history_rows) if history_rows else "<div class='empty-state'><strong>暂无补货记录</strong></div>"}</div>
        </section>
        """
        csrf_field = html.escape(self.csrf_token())
        if not admin:
            availability_class = "available" if total_in_stock else "quiet"
            availability_text = f"{total_in_stock} 件有货" if total_in_stock else "暂无补货"
            return self.page(
                "Fujifilm 库存监控",
                f"""
                <section class="page-heading public-heading">
                  <div>
                    <h1>库存状态</h1>
                    <p>{len(tasks)} 个分类 · {total_products} 件商品 · {html.escape(latest_check)} 更新</p>
                  </div>
                  <span class="availability-status {availability_class}"><i></i>{availability_text}</span>
                </section>
                <section class="monitor-grid">{''.join(rows) or "<div class='panel empty-state'><strong>暂无监控任务</strong></div>"}</section>
                {history_html if stock_history else ""}
                """,
            )
        notifications = self.app.notification_settings()
        notification_count = sum(1 for value in notifications.values() if value)
        notification_badge = (
            f"<span class='badge okb'>已启用 {notification_count} 个</span>"
            if notification_count
            else "<span class='badge warn'>未配置</span>"
        )
        return self.page(
            "Fujifilm 监控控制台",
            f"""
            {msg_html}
            <section class="page-heading admin-heading">
              <div>
                <p class="page-kicker">管理中心</p>
                <h1>监控控制台</h1>
                <p>{len(tasks)} 个任务 · 更新于 {html.escape(latest_check)}</p>
              </div>
              <div class="page-actions">
                <a class="button secondary" href="/">库存页</a>
                <a class="text-action" href="/logout">退出</a>
              </div>
            </section>
            {summary}
            <section class="panel toolbar-panel">
              <div class="section-head">
                <div><p class="section-kicker">运行状态</p><h2>服务控制</h2></div>
                {service_badge}
              </div>
              <div class="action-bar">
                <form method="post">
                  <input type="hidden" name="auth_token" value="{csrf_field}">
                  <button name="action" value="check">立即检查</button>
                </form>
                <a class="button secondary" href="/logs">运行日志</a>
                <details class="service-actions">
                  <summary class="button secondary">更多操作</summary>
                  <form method="post">
                    <input type="hidden" name="auth_token" value="{csrf_field}">
                    <button name="action" value="restart" class="secondary">重启服务</button>
                    <button name="action" value="stop" class="danger-light">暂停服务</button>
                    <button name="action" value="start" class="success-light">恢复服务</button>
                  </form>
                </details>
              </div>
            </section>
            <section class="admin-simple-grid">
            <section class="panel add-panel" id="new-task">
              <div class="section-head">
                <div><p class="section-kicker">任务配置</p><h2>新建监控</h2></div>
              </div>
              <form method="post" class="form-grid">
                <input type="hidden" name="auth_token" value="{csrf_field}">
                <input type="hidden" name="action" value="add">
                <label class="full-field">Fujifilm 链接<input name="url" placeholder="https://mall-jp.fujifilm.com/shop/c/..." required></label>
                <label>任务名称<input name="name" placeholder="例如 MINI 99"></label>
                <label>补货后
                  <select name="policy">
                    <option value="monthly">每款商品每月提醒一次</option>
                    <option value="stop">提醒后永久停止</option>
                    <option value="none">每次补货都提醒</option>
                  </select>
                </label>
                <details class="advanced-options full-field">
                  <summary>高级设置</summary>
                  <div class="advanced-grid">
                    <label>分类标题<input name="require_text" placeholder="可选"></label>
                    <label>检查间隔<input type="number" min="600" step="60" name="interval" inputmode="numeric" placeholder="3600 秒"></label>
                    <label>随机延迟<input type="number" min="0" step="60" name="jitter" inputmode="numeric" placeholder="600 秒"></label>
                  </div>
                </details>
                <button type="submit" class="wide-button full-field">添加任务</button>
              </form>
            </section>
            <details class="panel management-settings">
              <summary>通知与登录</summary>
              <div class="management-grid">
            <section class="settings-section push-panel">
              <div class="section-head">
                <div><p class="section-kicker">消息通知</p><h2>推送渠道</h2></div>
                {notification_badge}
              </div>
              <form method="post" class="settings-form">
                <input type="hidden" name="auth_token" value="{csrf_field}">
                <input type="hidden" name="action" value="save-notifications">
                <label>Server 酱<input type="password" name="serverchan_sendkey" value="{html.escape(notifications['serverchan_sendkey'])}" autocomplete="off" placeholder="SendKey"></label>
                <label>ntfy<input name="ntfy_topic" value="{html.escape(notifications['ntfy_topic'])}" placeholder="主题或 URL"></label>
                <label>Webhook URL<input name="webhook_url" value="{html.escape(notifications['webhook_url'])}" placeholder="https://example.com/webhook"></label>
                <button type="submit" class="wide-button">保存渠道</button>
              </form>
              <form method="post" class="inline-actions">
                <input type="hidden" name="auth_token" value="{csrf_field}">
                <button name="action" value="test-notifications" class="secondary wide-button">发送测试</button>
              </form>
            </section>
            <section class="settings-section passkey-panel">
              <div class="section-head">
                <div><p class="section-kicker">安全访问</p><h2>登录设备</h2></div>
                <span class="count-label">Passkey</span>
              </div>
              <button type="button" id="passkey-register" class="wide-button secondary">添加当前设备</button>
              <p id="passkey-status" class="small muted status-line"></p>
            </section>
              </div>
            </details>
            </section>
            <div class="section-title"><div><p class="section-kicker">运行任务</p><h2>监控任务</h2></div><span class="count-label">{len(tasks)} 个</span></div>
            <section class="task-grid">{''.join(rows) or "<p class='muted'>还没有监控任务。</p>"}</section>
            {history_html}
            """,
        )

    def task_admin_buttons(self, task: dict) -> str:
        csrf_field = html.escape(self.csrf_token())
        index = html.escape(str(task["index"]))
        return f"""
        <form method="post" class="task-actions" data-confirm-danger="删除后需要重新添加。确认删除此任务？">
          <input type="hidden" name="auth_token" value="{csrf_field}">
          <input type="hidden" name="index" value="{index}">
          <button name="action" value="clear-notified" class="secondary">重置本月提醒</button>
          <button name="action" value="delete" class="danger-link">删除</button>
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
    :root {{ color-scheme: light; --bg: #f5f6f7; --panel: #ffffff; --ink: #171a1f; --muted: #69717d; --line: #dfe2e6; --line-strong: #c7ccd3; --blue: #1769e0; --blue-strong: #1057bd; --green: #087a57; --green-soft: #e8f5ef; --red: #bd1e2d; --red-soft: #fff0f1; --amber: #8a5a00; --amber-soft: #fff5d8; }}
    * {{ box-sizing: border-box; }}
    html {{ background: var(--bg); }}
    body {{ min-width: 320px; min-height: 100vh; margin: 0; overflow-x: hidden; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC", sans-serif; background: var(--bg); color: var(--ink); font-size: 15px; }}
    a {{ color: var(--blue); }}
    button, input, select {{ font: inherit; }}
    header {{ height: 62px; display: flex; align-items: center; background: #fff; border-bottom: 1px solid var(--line); }}
    .topbar {{ width: min(1160px, calc(100% - 48px)); margin: 0 auto; }}
    .brand {{ width: fit-content; display: flex; align-items: center; gap: 10px; color: var(--ink); text-decoration: none; font-weight: 760; }}
    .brand-mark {{ width: 5px; height: 25px; background: var(--red); }}
    .brand-name {{ font-size: 18px; }}
    .brand-product {{ padding-left: 10px; border-left: 1px solid var(--line-strong); color: var(--muted); font-size: 12px; font-weight: 650; }}
    main {{ width: min(1160px, calc(100% - 48px)); margin: 0 auto; padding: 38px 0 60px; display: grid; gap: 24px; }}
    h1, h2, p {{ margin-top: 0; }}
    h1 {{ margin-bottom: 7px; font-size: 32px; line-height: 1.2; font-weight: 740; }}
    h2 {{ margin: 0; font-size: 18px; line-height: 1.3; font-weight: 720; }}
    p {{ line-height: 1.55; }}
    .page-heading {{ min-height: 82px; display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; padding-bottom: 4px; }}
    .page-heading > div:first-child {{ min-width: 0; }}
    .page-heading p:last-child {{ margin: 0; color: var(--muted); font-size: 14px; }}
    .page-kicker, .section-kicker {{ margin: 0 0 6px; color: var(--red); font-size: 11px; font-weight: 760; text-transform: uppercase; letter-spacing: 0; }}
    .page-actions {{ display: flex; align-items: center; gap: 16px; padding-bottom: 4px; }}
    .text-action, .auth-back {{ color: var(--muted); font-size: 14px; text-decoration: none; }}
    .text-action:hover, .auth-back:hover {{ color: var(--ink); }}
    .availability-status {{ display: inline-flex; align-items: center; gap: 8px; min-height: 36px; border: 1px solid var(--line); border-radius: 999px; padding: 7px 13px; background: #fff; color: #505866; font-size: 13px; font-weight: 680; white-space: nowrap; }}
    .availability-status i, .badge i, .availability-line i {{ width: 7px; height: 7px; border-radius: 50%; background: #8b929c; }}
    .availability-status.available {{ border-color: #a8d7c4; color: #075f45; background: var(--green-soft); }}
    .availability-status.available i, .okb i {{ background: var(--green); }}
    .panel, .card {{ min-width: 0; background: var(--panel); border: 1px solid var(--line); border-radius: 6px; box-shadow: 0 1px 2px rgba(20, 24, 30, .035); }}
    .panel {{ padding: 20px; }}
    .card {{ padding: 19px; }}
    .summary {{ display: grid; overflow: hidden; background: #fff; border: 1px solid var(--line); border-radius: 6px; box-shadow: 0 1px 2px rgba(20, 24, 30, .035); }}
    .public-summary {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .admin-summary {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .summary div {{ min-width: 0; padding: 17px 20px; border-right: 1px solid var(--line); }}
    .summary div:last-child {{ border-right: 0; }}
    .summary span {{ display: block; margin-bottom: 7px; color: var(--muted); font-size: 12px; }}
    .summary strong {{ display: block; font-size: 26px; line-height: 1.1; font-weight: 730; overflow-wrap: anywhere; }}
    .summary .time-value {{ font-size: 19px; }}
    .summary .service-value {{ width: fit-content; font-size: 18px; }}
    .summary .service-value.okb {{ color: var(--green); }}
    .summary .service-value.warn {{ color: var(--amber); }}
    .stock-summary strong {{ color: var(--green); }}
    .section-title, .section-head, .card-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }}
    .section-title {{ align-items: flex-end; margin-top: 4px; }}
    .section-head, .card-head {{ margin-bottom: 18px; }}
    .count-label {{ color: var(--muted); font-size: 12px; font-weight: 650; white-space: nowrap; }}
    .monitor-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); align-items: start; gap: 14px; }}
    .monitor-grid > .empty-state {{ grid-column: 1 / -1; }}
    .monitor-row {{ min-width: 0; display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start; gap: 14px; padding: 18px; border: 1px solid var(--line); border-radius: 6px; background: #fff; box-shadow: 0 1px 2px rgba(20, 24, 30, .035); }}
    .monitor-row.available {{ border-color: #9ccfba; box-shadow: inset 0 3px 0 var(--green); }}
    .monitor-identity {{ min-width: 0; }}
    .monitor-identity h2 {{ font-size: 16px; }}
    .monitor-state {{ min-width: 0; display: grid; justify-items: end; gap: 4px; text-align: right; }}
    .monitor-state strong {{ font-size: 14px; }}
    .monitor-state small {{ max-width: 150px; color: var(--muted); font-size: 12px; }}
    .monitor-row.available .monitor-state strong {{ color: var(--green); }}
    .monitor-products {{ grid-column: 1 / -1; padding-top: 4px; }}
    .task-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }}
    .public-task-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .stock-card {{ display: grid; gap: 16px; align-content: start; }}
    .stock-card.has-stock {{ border-color: #9ccfba; box-shadow: inset 0 3px 0 var(--green); }}
    .stock-card .card-head {{ margin-bottom: 0; }}
    .task-url {{ display: inline-flex; align-items: center; gap: 5px; width: fit-content; margin-top: 7px; color: var(--muted); font-size: 12px; text-decoration: none; }}
    .task-url span {{ color: #8a919b; }}
    .task-url:hover, .task-url:hover span {{ color: var(--blue); }}
    .badge {{ display: inline-flex; align-items: center; gap: 7px; min-height: 26px; border-radius: 999px; padding: 4px 9px; font-size: 12px; font-weight: 680; white-space: nowrap; }}
    .badge.okb {{ background: var(--green-soft); color: #075f45; }}
    .badge.warn {{ background: var(--amber-soft); color: var(--amber); }}
    .badge.warn i {{ background: #b57900; }}
    .stock-meter {{ display: grid; grid-template-columns: 72px 72px minmax(0, 1fr); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }}
    .stock-meter div {{ min-width: 0; padding: 12px 10px 12px 0; }}
    .stock-meter div + div {{ padding-left: 12px; border-left: 1px solid var(--line); }}
    .stock-meter span {{ display: block; margin-bottom: 5px; color: var(--muted); font-size: 11px; }}
    .stock-meter strong {{ display: block; font-size: 16px; line-height: 1.2; font-weight: 720; overflow-wrap: anywhere; }}
    .has-stock .stock-meter div:first-child strong {{ color: var(--green); }}
    .stock-block {{ min-width: 0; }}
    .stock-block-title {{ margin-bottom: 7px; font-size: 12px; font-weight: 700; }}
    .availability-line {{ min-height: 36px; display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }}
    .stock-items {{ display: grid; }}
    .stock-item {{ display: grid; grid-template-columns: 44px minmax(0, 1fr) auto; align-items: center; gap: 10px; min-height: 62px; padding: 8px 0; border-top: 1px solid var(--line); color: var(--ink); text-decoration: none; }}
    .stock-item:last-child {{ border-bottom: 1px solid var(--line); }}
    .stock-item:hover {{ color: var(--blue); }}
    .product-thumb {{ width: 44px; height: 44px; display: grid; place-items: center; overflow: hidden; border: 1px solid var(--line); border-radius: 4px; background: #fff; }}
    .product-thumb img {{ width: 100%; height: 100%; object-fit: contain; }}
    .product-thumb:empty::before {{ content: "商品"; color: var(--muted); font-size: 10px; }}
    .product-placeholder {{ color: var(--muted); font-size: 10px; }}
    .product-copy, .history-copy {{ min-width: 0; display: grid; gap: 3px; }}
    .product-copy strong, .history-copy strong {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }}
    .product-copy small, .history-copy small, .history-item time {{ color: var(--muted); font-size: 12px; }}
    .open-mark {{ color: var(--blue); font-size: 15px; }}
    .task-details {{ margin-top: -2px; color: var(--muted); font-size: 12px; }}
    .task-details summary, .backup-access summary {{ width: fit-content; cursor: pointer; color: var(--muted); }}
    .task-details summary:hover, .backup-access summary:hover {{ color: var(--ink); }}
    .metric-list {{ display: grid; grid-template-columns: 96px 1fr; gap: 8px 12px; margin: 12px 0 0; padding: 12px 0 0; border-top: 1px solid var(--line); }}
    dt {{ color: var(--muted); }}
    dd {{ min-width: 0; margin: 0; color: #3d444e; overflow-wrap: anywhere; }}
    .history-panel {{ padding: 0; overflow: hidden; }}
    .history-panel .section-head {{ align-items: center; margin: 0; padding: 18px 20px; border-bottom: 1px solid var(--line); }}
    .history-list {{ display: grid; }}
    .history-item {{ display: grid; grid-template-columns: 44px minmax(0, 1fr) auto; align-items: center; gap: 12px; padding: 10px 20px; color: var(--ink); text-decoration: none; border-bottom: 1px solid #eceef1; }}
    .history-item:last-child {{ border-bottom: 0; }}
    .history-item:hover {{ background: #f8f9fa; }}
    .history-item time {{ white-space: nowrap; }}
    .empty-state {{ min-height: 104px; display: grid; place-content: center; gap: 5px; text-align: center; color: var(--muted); }}
    .empty-state strong {{ color: #4d5561; font-size: 14px; }}
    .empty-state span {{ font-size: 12px; }}
    form {{ margin: 0; }}
    label {{ display: grid; gap: 7px; min-width: 0; color: #3e454f; font-size: 12px; font-weight: 670; }}
    input, select {{ width: 100%; min-height: 42px; border: 1px solid var(--line-strong); border-radius: 5px; padding: 9px 11px; background: #fff; color: var(--ink); font-size: 14px; }}
    input::placeholder {{ color: #969da7; }}
    input:focus, select:focus {{ outline: 3px solid rgba(23, 105, 224, .13); border-color: var(--blue); }}
    button, .button {{ min-height: 40px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--blue); border-radius: 5px; padding: 9px 14px; background: var(--blue); color: #fff; font-size: 13px; font-weight: 700; text-decoration: none; cursor: pointer; }}
    button:hover, .button:hover {{ border-color: var(--blue-strong); background: var(--blue-strong); }}
    .secondary {{ border-color: var(--line-strong); background: #fff; color: #353c46; }}
    .secondary:hover {{ border-color: #9da4ad; background: #f4f5f6; color: var(--ink); }}
    .danger-light {{ border-color: #e4aab0; background: #fff; color: var(--red); }}
    .danger-light:hover {{ border-color: var(--red); background: var(--red-soft); }}
    .success-light {{ border-color: #9bcbb8; background: #fff; color: var(--green); }}
    .success-light:hover {{ border-color: var(--green); background: var(--green-soft); }}
    .danger-link {{ min-height: 36px; border-color: transparent; background: transparent; color: var(--red); }}
    .danger-link:hover {{ border-color: transparent; background: var(--red-soft); }}
    .wide-button {{ width: 100%; }}
    .action-bar, .task-actions {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }}
    .service-actions {{ position: relative; }}
    .service-actions > summary, .advanced-options > summary, .management-settings > summary {{ list-style: none; cursor: pointer; }}
    .service-actions > summary::-webkit-details-marker, .advanced-options > summary::-webkit-details-marker, .management-settings > summary::-webkit-details-marker {{ display: none; }}
    .service-actions > form {{ position: absolute; z-index: 5; top: 46px; right: 0; width: 150px; display: grid; gap: 7px; padding: 9px; border: 1px solid var(--line); border-radius: 6px; background: #fff; box-shadow: 0 10px 30px rgba(20, 24, 30, .12); }}
    .service-actions > form button {{ width: 100%; }}
    .toolbar-panel .section-head {{ align-items: center; }}
    .admin-simple-grid {{ display: grid; gap: 16px; }}
    .form-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .full-field {{ grid-column: 1 / -1; }}
    .advanced-options {{ color: var(--muted); font-size: 12px; }}
    .advanced-options > summary {{ width: fit-content; }}
    .advanced-options > summary:hover {{ color: var(--ink); }}
    .advanced-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line); }}
    .management-settings {{ padding: 0; }}
    .management-settings > summary {{ padding: 18px 20px; font-weight: 700; }}
    .management-settings[open] > summary {{ border-bottom: 1px solid var(--line); }}
    .management-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .settings-section {{ min-width: 0; padding: 20px; }}
    .settings-section + .settings-section {{ border-left: 1px solid var(--line); }}
    .management-grid .passkey-panel {{ display: block; }}
    .management-grid .passkey-panel .section-head {{ margin-bottom: 18px; }}
    .management-grid .passkey-panel .status-line {{ margin-top: 8px; }}
    .settings-form {{ display: grid; gap: 12px; }}
    .inline-actions {{ margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--line); }}
    .task-actions {{ padding-top: 4px; }}
    .task-actions .secondary, .task-actions .danger-link {{ flex: 1 1 auto; }}
    .notice {{ margin: 0; padding: 12px 14px; border: 1px solid; border-radius: 5px; font-size: 13px; }}
    .notice.ok {{ border-color: #a8d7c4; background: var(--green-soft); color: #075f45; }}
    .error, .error-box {{ color: var(--red); }}
    .error-box {{ border-color: #e6b0b5; background: var(--red-soft); }}
    .task-error {{ margin: 0; padding: 10px 11px; border-left: 3px solid var(--red); background: var(--red-soft); color: var(--red); font-size: 12px; }}
    .auth-shell {{ width: min(440px, 100%); margin: 38px auto 0; }}
    .auth-panel {{ padding: 28px; }}
    .auth-title {{ display: grid; justify-items: center; margin-bottom: 24px; text-align: center; }}
    .auth-title h1 {{ margin-bottom: 6px; font-size: 27px; }}
    .auth-title > p:last-child {{ margin: 0; color: var(--muted); font-size: 14px; }}
    .auth-symbol {{ width: 44px; height: 44px; display: grid; place-items: center; margin-bottom: 18px; border: 1px solid #282d34; border-bottom: 4px solid var(--red); border-radius: 5px; background: #20242a; color: #fff; font-size: 18px; font-weight: 800; }}
    .auth-primary {{ min-height: 46px; }}
    .backup-access {{ margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--line); font-size: 13px; }}
    .backup-form {{ display: grid; gap: 12px; margin-top: 14px; }}
    .auth-back {{ display: block; width: fit-content; margin: 22px auto 0; }}
    .small {{ margin: 8px 0 0; font-size: 12px; }}
    .status-line {{ min-height: 17px; text-align: center; }}
    .console-panel {{ padding: 0; overflow: hidden; }}
    pre {{ max-height: 70vh; margin: 0; padding: 18px; overflow: auto; white-space: pre-wrap; background: #20242a; color: #eef0f3; font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .muted {{ color: var(--muted); }}
    @media (max-width: 940px) {{
      .public-task-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .monitor-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .management-grid {{ grid-template-columns: 1fr; }}
      .settings-section + .settings-section {{ border-left: 0; border-top: 1px solid var(--line); }}
    }}
    @media (max-width: 680px) {{
      header {{ height: 56px; }}
      .topbar, main {{ width: calc(100% - 28px); }}
      .brand-product {{ display: none; }}
      main {{ padding: 26px 0 42px; gap: 18px; }}
      h1 {{ font-size: 27px; }}
      .page-heading {{ min-height: 72px; align-items: flex-start; }}
      .page-heading p:last-child {{ overflow-wrap: anywhere; }}
      .page-heading, .section-title {{ flex-wrap: wrap; }}
      .page-actions {{ width: 100%; justify-content: space-between; }}
      .public-summary, .admin-summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .summary div {{ padding: 14px; border-bottom: 1px solid var(--line); }}
      .summary div:nth-child(2n) {{ border-right: 0; }}
      .summary div:nth-last-child(-n+2) {{ border-bottom: 0; }}
      .admin-summary div:nth-child(3) {{ border-right: 0; }}
      .admin-summary div:last-child {{ grid-column: 1 / -1; border-top: 1px solid var(--line); border-right: 0; }}
      .summary strong {{ font-size: 23px; }}
      .summary .time-value {{ font-size: 17px; }}
      .task-grid, .public-task-grid, .form-grid {{ grid-template-columns: 1fr; }}
      .monitor-grid {{ grid-template-columns: 1fr; }}
      .full-field {{ grid-column: auto; }}
      .advanced-grid {{ grid-template-columns: 1fr; }}
      .monitor-row {{ grid-template-columns: minmax(0, 1fr) auto; gap: 10px; padding: 15px; }}
      .monitor-state {{ min-width: 0; }}
      .monitor-state small {{ white-space: normal; }}
      .monitor-row > .badge {{ grid-column: 1 / -1; width: fit-content; }}
      .monitor-products {{ grid-column: 1 / -1; }}
      .stock-meter {{ grid-template-columns: 64px 64px minmax(0, 1fr); }}
      .action-bar {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .action-bar > * {{ width: 100%; }}
      .history-item {{ grid-template-columns: 40px minmax(0, 1fr); padding: 10px 14px; }}
      .history-item time {{ grid-column: 2; }}
      .product-thumb {{ width: 40px; height: 40px; }}
      .auth-shell {{ margin-top: 12px; }}
      .auth-panel {{ padding: 24px 20px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <a class="brand" href="/">
        <span class="brand-mark" aria-hidden="true"></span>
        <span class="brand-name">FUJIFILM</span>
        <span class="brand-product">STOCK MONITOR</span>
      </a>
    </div>
  </header>
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
    setPasskeyStatus("正在验证...", false);
    const options = await (await fetch("/webauthn/login/options")).json();
    if (!options.hasCredentials) throw new Error("尚未添加 Passkey，请使用备用密钥登录。");
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
    setPasskeyStatus("正在添加设备...", false);
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
    setPasskeyStatus("设备已添加。", false);
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
