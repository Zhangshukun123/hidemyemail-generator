from __future__ import annotations

import json
import re
import secrets
import string
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .clash_proxy import (
    DEFAULT_MAX_LATENCY_MS,
    JP_FIXED_PORT_MAP_NAME,
    ClashConnection,
    ClashController,
    ClashControllerError,
    _normalize_controller_url,
    _normalize_local_proxy_url,
    discover_clash_connection,
    load_fixed_port_proxies,
)
from .inbox import connect_db


REGISTRATION_PROXY_SETTING_KEY = "registration_proxy_config_v1"
DEFAULT_PROXY_COUNTRY = "NL"
DEFAULT_PROXY_DURATION_MINUTES = 5
DEFAULT_PROXY_MODE = "dynamic"
PROXY_MODES = {"dynamic", "clash"}
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

    def __init__(
        self,
        db_file: Path,
        *,
        clash_client_factory=None,
    ) -> None:
        self.db_file = Path(db_file)
        self.clash_client_factory = clash_client_factory or ClashController
        self._rotation_lock = threading.RLock()

    @property
    def fixed_port_map_file(self) -> Path:
        return self.db_file.parent / "output" / JP_FIXED_PORT_MAP_NAME

    def _fixed_port_proxies(self) -> dict[str, str]:
        return load_fixed_port_proxies(self.fixed_port_map_file)

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "enabled": False,
            "mode": DEFAULT_PROXY_MODE,
            "country": DEFAULT_PROXY_COUNTRY,
            "endpoint": "",
            "username": "",
            "password": "",
            "duration": DEFAULT_PROXY_DURATION_MINUTES,
            "clashController": "",
            "clashSecret": "",
            "clashSelector": "",
            "clashProxyUrl": "",
            "clashPipePath": "",
            "maxLatencyMs": DEFAULT_MAX_LATENCY_MS,
            "rotationCursor": 0,
            "lastNode": "",
            "lastSelector": "",
            "lastLatencyMs": 0,
            "lastSkipped": 0,
            "candidateCount": 0,
            "lastExitIp": "",
            "lastExitCountry": "",
            "lastProxyUrl": "",
            "lastSwitchedAt": "",
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
        mode = str(state.get("mode") or DEFAULT_PROXY_MODE).strip().casefold()
        state["mode"] = mode if mode in PROXY_MODES else DEFAULT_PROXY_MODE
        try:
            state["duration"] = min(120, max(1, int(state.get("duration") or 5)))
        except (TypeError, ValueError):
            state["duration"] = DEFAULT_PROXY_DURATION_MINUTES
        try:
            state["maxLatencyMs"] = min(
                10000,
                max(50, int(state.get("maxLatencyMs") or DEFAULT_MAX_LATENCY_MS)),
            )
        except (TypeError, ValueError):
            state["maxLatencyMs"] = DEFAULT_MAX_LATENCY_MS
        try:
            state["rotationCursor"] = max(0, int(state.get("rotationCursor") or 0))
        except (TypeError, ValueError):
            state["rotationCursor"] = 0
        state["enabled"] = bool(state.get("enabled"))
        if state["mode"] == "clash":
            state["country"] = "JP"
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
    def _dynamic_configured(state: dict[str, Any]) -> bool:
        return bool(
            str(state.get("endpoint") or "").strip()
            and str(state.get("username") or "").strip()
            and str(state.get("password") or "")
        )

    @staticmethod
    def _clash_connection(state: dict[str, Any]) -> ClashConnection:
        return discover_clash_connection(
            controller_url=str(state.get("clashController") or ""),
            secret=str(state.get("clashSecret") or ""),
            proxy_url=str(state.get("clashProxyUrl") or ""),
            pipe_path=str(state.get("clashPipePath") or ""),
        )

    @classmethod
    def _configured(cls, state: dict[str, Any]) -> bool:
        if state.get("mode") == "clash":
            try:
                return cls._clash_connection(state).configured
            except ValueError:
                return False
        return cls._dynamic_configured(state)

    def _public_state_from(self, state: dict[str, Any]) -> dict[str, Any]:
        mode = str(state.get("mode") or DEFAULT_PROXY_MODE)
        country = "JP" if mode == "clash" else str(state["country"])
        connection = None
        if mode == "clash":
            try:
                connection = self._clash_connection(state)
            except ValueError:
                pass
        endpoint = (
            connection.proxy_url
            if connection is not None
            else str(state.get("endpoint") or "")
        )
        fixed_ports = self._fixed_port_proxies() if mode == "clash" else {}
        normal_endpoint = endpoint
        if fixed_ports:
            last_proxy_url = str(state.get("lastProxyUrl") or "").strip()
            endpoint = (
                last_proxy_url
                if last_proxy_url in fixed_ports.values()
                else next(iter(fixed_ports.values()))
            )
        fixed_port_numbers = sorted(
            int(urlsplit(url).port or 0) for url in fixed_ports.values()
        )
        return {
            "enabled": bool(state["enabled"]),
            "configured": self._configured(state),
            "mode": mode,
            "country": country,
            "countryLabel": PROXY_COUNTRIES[country],
            "endpoint": endpoint,
            "normalEndpoint": normal_endpoint if mode == "clash" else "",
            "fixedPortsEnabled": bool(fixed_ports),
            "fixedPortBase": (
                fixed_port_numbers[0] - 1 if fixed_port_numbers else 0
            ),
            "fixedPortCount": len(fixed_ports),
            "durationMinutes": int(state["duration"]),
            "maxLatencyMs": int(state["maxLatencyMs"]),
            "selector": str(
                state.get("lastSelector") or state.get("clashSelector") or ""
            ),
            "currentNode": str(state.get("lastNode") or ""),
            "lastLatencyMs": int(state.get("lastLatencyMs") or 0),
            "lastSkipped": int(state.get("lastSkipped") or 0),
            "candidateCount": int(state.get("candidateCount") or 0),
            "exitCountry": str(state.get("lastExitCountry") or ""),
            "exitIpVerified": bool(state.get("lastExitIp")),
            "lastSwitchedAt": str(state.get("lastSwitchedAt") or ""),
            "updatedAt": str(state.get("updatedAt") or ""),
            "countries": [
                {"code": code, "label": label}
                for code, label in PROXY_COUNTRIES.items()
            ],
            "modes": [
                {"code": "dynamic", "label": "动态代理 SID"},
                {"code": "clash", "label": "Clash 日本固定端口轮询"},
            ],
        }

    def public_state(self) -> dict[str, Any]:
        return self._public_state_from(self.load())

    def configure(
        self,
        *,
        enabled: bool | None = None,
        mode: str | None = None,
        country: str | None = None,
        proxy_line: str | None = None,
        clash_controller: str | None = None,
        clash_secret: str | None = None,
        clash_selector: str | None = None,
        clash_proxy_url: str | None = None,
        max_latency_ms: int | None = None,
    ) -> dict[str, Any]:
        with self._rotation_lock:
            state = self.load()
            if mode is not None:
                normalized_mode = str(mode or "").strip().casefold()
                if normalized_mode not in PROXY_MODES:
                    raise ValueError("注册代理模式无效")
                state["mode"] = normalized_mode
            if proxy_line is not None and str(proxy_line).strip():
                state.update(parse_proxy_credential(proxy_line))
            if country is not None:
                state["country"] = _normalize_country(country)
            if clash_controller is not None:
                state["clashController"] = _normalize_controller_url(
                    clash_controller
                ) if str(clash_controller).strip() else ""
            if clash_secret is not None and str(clash_secret):
                state["clashSecret"] = str(clash_secret).strip()
            if clash_selector is not None:
                state["clashSelector"] = str(clash_selector).strip()
            if clash_proxy_url is not None:
                state["clashProxyUrl"] = _normalize_local_proxy_url(
                    clash_proxy_url
                ) if str(clash_proxy_url).strip() else ""
            if max_latency_ms is not None:
                if isinstance(max_latency_ms, bool):
                    raise ValueError("Clash 最大延迟必须是整数")
                latency = int(max_latency_ms)
                if not 50 <= latency <= 10000:
                    raise ValueError("Clash 最大延迟必须在 50–10000 ms 之间")
                state["maxLatencyMs"] = latency
            if state.get("mode") == "clash":
                state["country"] = "JP"
            if enabled is not None:
                state["enabled"] = bool(enabled)
            if state["enabled"] and not self._configured(state):
                if state.get("mode") == "clash":
                    raise ValueError("未发现可用的 Clash Controller 与本地代理端口")
                raise ValueError("请先保存动态代理连接信息")
            state["updatedAt"] = _utc_now()
            self._save(state)
            return self._public_state_from(state)

    def next_proxy(self, *, force: bool = False) -> tuple[str, dict[str, Any]]:
        with self._rotation_lock:
            state = self.load()
            if not self._configured(state) or (not state["enabled"] and not force):
                return "", self._public_state_from(state)
            if state.get("mode") == "clash":
                connection = self._clash_connection(state)
                client = self.clash_client_factory(connection)
                fixed_ports = self._fixed_port_proxies()
                try:
                    result = client.rotate_japanese_proxy(
                        selector=str(state.get("clashSelector") or ""),
                        max_latency_ms=int(state["maxLatencyMs"]),
                        cursor=int(state.get("rotationCursor") or 0),
                        previous_node=str(state.get("lastNode") or ""),
                        previous_exit_ip=str(state.get("lastExitIp") or ""),
                        fixed_ports=fixed_ports or None,
                    )
                except ClashControllerError:
                    raise
                state.update(
                    {
                        "country": "JP",
                        "rotationCursor": result.next_cursor,
                        "lastNode": result.node,
                        "lastSelector": result.selector,
                        "lastLatencyMs": result.latency_ms,
                        "lastSkipped": result.skipped,
                        "candidateCount": result.candidate_count,
                        "lastExitIp": result.exit_ip,
                        "lastExitCountry": result.exit_country,
                        "lastProxyUrl": result.proxy_url,
                        "lastSwitchedAt": _utc_now(),
                    }
                )
                self._save(state)
                return result.proxy_url, self._public_state_from(state)

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
            return url, self._public_state_from(state)


__all__ = [
    "DEFAULT_PROXY_COUNTRY",
    "DEFAULT_PROXY_MODE",
    "PROXY_COUNTRIES",
    "PROXY_MODES",
    "REGISTRATION_PROXY_SETTING_KEY",
    "RegistrationProxyStore",
    "parse_proxy_credential",
]
