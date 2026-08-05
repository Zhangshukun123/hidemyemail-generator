from __future__ import annotations

import json
import re
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .inbox import connect_db


REGISTRATION_PROXY_SETTING_KEY = "registration_proxy_config_v1"
DEFAULT_PROXY_COUNTRY = "NL"
DEFAULT_PROXY_DURATION_MINUTES = 5
PROXY_COUNTRIES = {
    "NL": "荷兰",
    "US": "美国",
    "JP": "日本",
    "DE": "德国",
    "GB": "英国",
    "FR": "法国",
    "CA": "加拿大",
    "AU": "澳大利亚",
    "SG": "新加坡",
    "HK": "中国香港",
    "TW": "中国台湾",
    "KR": "韩国",
    "BR": "巴西",
    "IN": "印度",
    "TR": "土耳其",
}

_STICKY_SUFFIX_RE = re.compile(
    r"-region-[A-Za-z]{2}-sid-[A-Za-z0-9]{4,32}-t-\d+$", re.IGNORECASE
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_country(value: Any) -> str:
    country = str(value or "").strip().upper()
    if country not in PROXY_COUNTRIES:
        raise ValueError("不支持的代理国家")
    return country


def _base_username(value: str) -> str:
    return _STICKY_SUFFIX_RE.sub("", str(value or "").strip())


def _validate_endpoint(value: str) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text if "://" in text else f"http://{text}")
    if not parsed.hostname or parsed.port is None:
        raise ValueError("代理主机格式应为 hostname:port")
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        raise ValueError("代理主机不能包含凭据或路径")
    if parsed.query or parsed.fragment:
        raise ValueError("代理主机不能包含参数")
    host = parsed.hostname
    host_text = f"[{host}]" if ":" in host else host
    return f"{host_text}:{parsed.port}"


def parse_proxy_credential(value: str) -> dict[str, str]:
    """Parse URL or host:port:username:password without retaining a fixed SID."""

    text = str(value or "").strip()
    if not text:
        raise ValueError("请输入代理连接信息")
    if "://" in text:
        parsed = urlsplit(text)
        if not parsed.hostname or parsed.port is None:
            raise ValueError("代理连接格式无效")
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        endpoint = _validate_endpoint(f"{parsed.hostname}:{parsed.port}")
    else:
        parts = text.split(":", 3)
        if len(parts) != 4:
            raise ValueError("代理格式应为 host:port:username:password")
        host, port, username, password = parts
        endpoint = _validate_endpoint(f"{host}:{port}")
    username = _base_username(username)
    if not username:
        raise ValueError("代理用户名不能为空")
    if not password:
        raise ValueError("代理密码不能为空")
    return {"endpoint": endpoint, "username": username, "password": password}


class RegistrationProxyStore:
    """Persist registration-only proxy settings without returning credentials."""

    def __init__(self, db_file: Path) -> None:
        self.db_file = Path(db_file)

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "enabled": False,
            "country": DEFAULT_PROXY_COUNTRY,
            "endpoint": "",
            "username": "",
            "password": "",
            "duration": DEFAULT_PROXY_DURATION_MINUTES,
            "updatedAt": "",
        }

    def load(self) -> dict[str, Any]:
        conn = connect_db(str(self.db_file))
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (REGISTRATION_PROXY_SETTING_KEY,),
            ).fetchone()
        finally:
            conn.close()
        state = self._defaults()
        if row:
            try:
                stored = json.loads(str(row["value"] or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                stored = {}
            if isinstance(stored, dict):
                state.update(
                    {
                        key: stored[key]
                        for key in state
                        if key in stored
                    }
                )
        state["country"] = _normalize_country(state.get("country") or DEFAULT_PROXY_COUNTRY)
        try:
            state["duration"] = min(120, max(1, int(state.get("duration") or 5)))
        except (TypeError, ValueError):
            state["duration"] = DEFAULT_PROXY_DURATION_MINUTES
        state["enabled"] = bool(state.get("enabled"))
        return state

    def _save(self, state: dict[str, Any]) -> None:
        conn = connect_db(str(self.db_file))
        try:
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (
                    REGISTRATION_PROXY_SETTING_KEY,
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _configured(state: dict[str, Any]) -> bool:
        return bool(
            str(state.get("endpoint") or "").strip()
            and str(state.get("username") or "").strip()
            and str(state.get("password") or "")
        )

    def public_state(self) -> dict[str, Any]:
        state = self.load()
        country = state["country"]
        return {
            "enabled": bool(state["enabled"]),
            "configured": self._configured(state),
            "country": country,
            "countryLabel": PROXY_COUNTRIES[country],
            "endpoint": str(state.get("endpoint") or ""),
            "durationMinutes": int(state["duration"]),
            "updatedAt": str(state.get("updatedAt") or ""),
            "countries": [
                {"code": code, "label": label}
                for code, label in PROXY_COUNTRIES.items()
            ],
        }

    def configure(
        self,
        *,
        enabled: bool | None = None,
        country: str | None = None,
        proxy_line: str | None = None,
    ) -> dict[str, Any]:
        state = self.load()
        if proxy_line is not None and str(proxy_line).strip():
            state.update(parse_proxy_credential(proxy_line))
        if country is not None:
            state["country"] = _normalize_country(country)
        if enabled is not None:
            state["enabled"] = bool(enabled)
        if state["enabled"] and not self._configured(state):
            raise ValueError("请先保存动态代理连接信息")
        state["updatedAt"] = _utc_now()
        self._save(state)
        return self.public_state()

    def next_proxy(self, *, force: bool = False) -> tuple[str, dict[str, Any]]:
        state = self.load()
        if not self._configured(state) or (not state["enabled"] and not force):
            return "", self.public_state()
        country = _normalize_country(state["country"])
        sid = "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(8)
        )
        endpoint = _validate_endpoint(str(state["endpoint"]))
        parsed = urlsplit(f"http://{endpoint}")
        host = parsed.hostname or ""
        host_text = f"[{host}]" if ":" in host else host
        username = quote(str(state["username"]), safe="")
        password = quote(str(state["password"]), safe="")
        proxy_username = (
            f"{username}-region-{country}-sid-{sid}-t-{int(state['duration'])}"
        )
        url = f"http://{proxy_username}:{password}@{host_text}:{parsed.port}"
        return url, self.public_state()


__all__ = [
    "DEFAULT_PROXY_COUNTRY",
    "PROXY_COUNTRIES",
    "REGISTRATION_PROXY_SETTING_KEY",
    "RegistrationProxyStore",
    "parse_proxy_credential",
]
