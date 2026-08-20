from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .inbox import connect_db


INVENTORY_AUTH_SETTING_KEY = "registration_inventory_auth_v1"
PASSWORD_SCHEME = "scrypt-v1"
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _password_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=64 * 1024 * 1024,
    )


def _decode_record(value: Any) -> dict[str, Any] | None:
    try:
        record = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or record.get("scheme") != PASSWORD_SCHEME:
        return None
    return record


class InventoryCredentialStore:
    """Persist one server login while keeping only a salted password hash."""

    def __init__(self, db_file: Path) -> None:
        self.db_file = Path(db_file)

    def _load(self) -> dict[str, Any] | None:
        if not self.db_file.parent.is_dir():
            return None
        conn = connect_db(str(self.db_file))
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (INVENTORY_AUTH_SETTING_KEY,),
            ).fetchone()
        finally:
            conn.close()
        return _decode_record(row["value"]) if row else None

    @property
    def configured(self) -> bool:
        return self._load() is not None

    def configure(self, username: str, password: str) -> None:
        normalized_username = str(username or "").strip()
        supplied_password = str(password or "")
        if not normalized_username or len(normalized_username) > 200:
            raise ValueError("库存登录账号不能为空且不能超过 200 个字符")
        if len(supplied_password) < 8 or len(supplied_password) > 1024:
            raise ValueError("库存登录密码长度必须为 8–1024 个字符")
        current = self._load()
        if current and self.verify(normalized_username, supplied_password):
            return
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        salt = secrets.token_bytes(16)
        record = {
            "version": 1,
            "scheme": PASSWORD_SCHEME,
            "username": normalized_username,
            "salt": base64.b64encode(salt).decode("ascii"),
            "passwordHash": base64.b64encode(
                _password_hash(supplied_password, salt)
            ).decode("ascii"),
            "updatedAt": _utc_now(),
        }
        conn = connect_db(str(self.db_file))
        try:
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (
                    INVENTORY_AUTH_SETTING_KEY,
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def verify(self, username: str, password: str) -> bool:
        record = self._load()
        if record is None:
            return False
        supplied_username = str(username or "").strip()
        stored_username = str(record.get("username") or "")
        try:
            salt = base64.b64decode(str(record.get("salt") or ""), validate=True)
            stored_hash = base64.b64decode(
                str(record.get("passwordHash") or ""), validate=True
            )
            supplied_hash = _password_hash(str(password or ""), salt)
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(
            supplied_username.encode("utf-8"), stored_username.encode("utf-8")
        ) and hmac.compare_digest(supplied_hash, stored_hash)


def access_token_digest(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


__all__ = [
    "INVENTORY_AUTH_SETTING_KEY",
    "InventoryCredentialStore",
    "access_token_digest",
]
