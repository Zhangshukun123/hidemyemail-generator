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
CARD_LINK_PROXY_SETTING_KEY = "card_link_proxy_config_v1"
DEFAULT_PROXY_COUNTRY = "NL"
DEFAULT_PROXY_DURATION_MINUTES = 5
DEFAULT_PROXY_MODE = "dynamic"
PROXY_MODE_DYNAMIC = "dynamic"
PROXY_MODE_KOOKEEY = "kookeey"
PROXY_MODE_CLASH = "clash"
PROXY_MODES = {PROXY_MODE_DYNAMIC, PROXY_MODE_KOOKEEY, PROXY_MODE_CLASH}
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
    "ES": "西班牙",
    "ID": "印度尼西亚",
    "IT": "意大利",
    "MX": "墨西哥",
    "NZ": "新西兰",
    "PT": "葡萄牙",
    "TH": "泰国",
    "VN": "越南",
    "AT": "奥地利",
    "BE": "比利时",
    "CH": "瑞士",
    "CN": "中国",
    "IE": "爱尔兰",
    "MY": "马来西亚",
    "PH": "菲律宾",
    "PL": "波兰",
    "SE": "瑞典",
    "AE": "阿联酋",
    "AR": "阿根廷",
    "CL": "智利",
    "CO": "哥伦比亚",
    "ZA": "南非",
}
CARD_LINK_PROXY_COUNTRY_DEFAULTS = {
    "phCreate": "US",
    "phPromotion": "TR",
    "de": "DE",
    "paypalUsCreate": "US",
    "paypalUsFollowup": "US",
    "paypalGbCreate": "GB",
}
CARD_LINK_PROXY_MODE_KEYS = {
    "ph_hosted",
    "de_oaics_paypal",
    "paypal_us",
    "paypal_gb",
}

_STICKY_SUFFIX_RE = re.compile(
    r"-region-[A-Za-z]{2}-sid-[A-Za-z0-9]{4,32}-t-\d+$", re.IGNORECASE
)
_KOOKEEY_PASSWORD_SUFFIX_RE = re.compile(
    r"(?P<base>.+)-(?:[A-Za-z]{2}|global)-[A-Za-z0-9]{8}(?:-\d+[mh])?$",
    re.IGNORECASE,
)
_KOOKEEY_USERNAME_RE = re.compile(r"\d+-[A-Za-z0-9]{6,16}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_country(value: Any) -> str:
    country = str(value or "").strip().upper()
    if country not in PROXY_COUNTRIES:
        raise ValueError("不支持的代理国家")
    return country


def _normalize_card_link_countries(value: Any) -> dict[str, str]:
    selections = dict(CARD_LINK_PROXY_COUNTRY_DEFAULTS)
    if not isinstance(value, dict):
        return selections
    for key, default_country in CARD_LINK_PROXY_COUNTRY_DEFAULTS.items():
        try:
            selections[key] = _normalize_country(value.get(key) or default_country)
        except ValueError:
            selections[key] = default_country
    return selections


def _normalize_card_link_modes(value: Any) -> dict[str, str]:
    selections = {key: "" for key in CARD_LINK_PROXY_MODE_KEYS}
    if not isinstance(value, dict):
        return selections
    for key in CARD_LINK_PROXY_MODE_KEYS:
        mode = str(value.get(key) or "").strip().casefold()
        selections[key] = mode if mode in PROXY_MODES else ""
    return selections


def _base_username(value: str) -> str:
    return _STICKY_SUFFIX_RE.sub("", str(value or "").strip())


def _base_kookeey_password(value: str) -> str:
    password = str(value or "")
    match = _KOOKEEY_PASSWORD_SUFFIX_RE.fullmatch(password)
    return match.group("base") if match else password


def _detect_proxy_mode(endpoint: str) -> str:
    try:
        parsed = urlsplit(
            endpoint if "://" in str(endpoint or "") else f"http://{endpoint}"
        )
        hostname = str(parsed.hostname or "").lower()
    except ValueError:
        hostname = ""
    if hostname == "kookeey.info" or hostname.endswith(".kookeey.info"):
        return PROXY_MODE_KOOKEEY
    return PROXY_MODE_DYNAMIC


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


def parse_proxy_credential(
    value: str, *, mode: str = DEFAULT_PROXY_MODE
) -> dict[str, str]:
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
    normalized_mode = str(mode or DEFAULT_PROXY_MODE).strip().casefold()
    if normalized_mode not in PROXY_MODES:
        raise ValueError("注册代理模式无效")
    if normalized_mode == PROXY_MODE_DYNAMIC and _detect_proxy_mode(endpoint) == PROXY_MODE_KOOKEEY:
        normalized_mode = PROXY_MODE_KOOKEEY
    username = _base_username(username)
    if normalized_mode == PROXY_MODE_KOOKEEY:
        password = _base_kookeey_password(password)
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
        setting_key: str = REGISTRATION_PROXY_SETTING_KEY,
    ) -> None:
        self.db_file = Path(db_file)
        self.clash_client_factory = clash_client_factory or ClashController
        self.setting_key = str(setting_key or REGISTRATION_PROXY_SETTING_KEY).strip()
        if not self.setting_key:
            raise ValueError("代理配置存储键不能为空")
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
            "cardLinkCountries": dict(CARD_LINK_PROXY_COUNTRY_DEFAULTS),
            "cardLinkModes": _normalize_card_link_modes({}),
        }

    def load(self) -> dict[str, Any]:
        conn = connect_db(str(self.db_file))
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (self.setting_key,),
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
        state["cardLinkCountries"] = _normalize_card_link_countries(
            state.get("cardLinkCountries")
        )
        state["cardLinkModes"] = _normalize_card_link_modes(
            state.get("cardLinkModes")
        )
        if state["mode"] != PROXY_MODE_CLASH and _detect_proxy_mode(
            str(state.get("endpoint") or "")
        ) == PROXY_MODE_KOOKEEY:
            state["mode"] = PROXY_MODE_KOOKEEY
        if state["mode"] == PROXY_MODE_CLASH:
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
                    self.setting_key,
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

    @classmethod
    def _kookeey_configured(cls, state: dict[str, Any]) -> bool:
        return cls._dynamic_configured(state) and bool(
            _KOOKEEY_USERNAME_RE.fullmatch(
                str(state.get("username") or "").strip()
            )
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
    def _clash_mode_configured(cls, state: dict[str, Any]) -> bool:
        try:
            return cls._clash_connection(state).configured
        except ValueError:
            return False

    @classmethod
    def _configured(cls, state: dict[str, Any]) -> bool:
        if state.get("mode") == PROXY_MODE_CLASH:
            return cls._clash_mode_configured(state)
        return cls._dynamic_configured(state)

    def _public_state_from(self, state: dict[str, Any]) -> dict[str, Any]:
        mode = str(state.get("mode") or DEFAULT_PROXY_MODE)
        country = "JP" if mode == PROXY_MODE_CLASH else str(state["country"])
        connection = None
        if mode == PROXY_MODE_CLASH:
            try:
                connection = self._clash_connection(state)
            except ValueError:
                pass
        endpoint = (
            connection.proxy_url
            if connection is not None
            else str(state.get("endpoint") or "")
        )
        fixed_ports = self._fixed_port_proxies() if mode == PROXY_MODE_CLASH else {}
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
            "normalEndpoint": normal_endpoint if mode == PROXY_MODE_CLASH else "",
            "dynamicEndpoint": str(state.get("endpoint") or ""),
            "usernameConfigured": bool(str(state.get("username") or "").strip()),
            "passwordConfigured": bool(str(state.get("password") or "")),
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
            "cardLinkCountries": dict(state["cardLinkCountries"]),
            "cardLinkModes": dict(state["cardLinkModes"]),
            "countries": [
                {"code": code, "label": label}
                for code, label in PROXY_COUNTRIES.items()
            ],
            "modes": [
                {
                    "code": PROXY_MODE_KOOKEEY,
                    "label": "Kookeey 动态住宅",
                    "configured": self._kookeey_configured(state),
                },
                {
                    "code": PROXY_MODE_DYNAMIC,
                    "label": "通用 region/SID",
                    "configured": self._dynamic_configured(state),
                },
                {
                    "code": PROXY_MODE_CLASH,
                    "label": "Clash 日本固定端口轮询",
                    "configured": self._clash_mode_configured(state),
                },
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
        proxy_endpoint: str | None = None,
        proxy_username: str | None = None,
        proxy_password: str | None = None,
        card_link_countries: dict[str, str] | None = None,
        card_link_modes: dict[str, str] | None = None,
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
                state.update(
                    parse_proxy_credential(
                        proxy_line, mode=str(state.get("mode") or DEFAULT_PROXY_MODE)
                    )
                )
            if proxy_endpoint is not None and str(proxy_endpoint).strip():
                state["endpoint"] = _validate_endpoint(proxy_endpoint)
            if (
                state.get("mode") != PROXY_MODE_CLASH
                and _detect_proxy_mode(str(state.get("endpoint") or ""))
                == PROXY_MODE_KOOKEEY
            ):
                state["mode"] = PROXY_MODE_KOOKEEY
            if proxy_username is not None and str(proxy_username).strip():
                state["username"] = _base_username(proxy_username)
            if proxy_password is not None and str(proxy_password):
                state["password"] = (
                    _base_kookeey_password(proxy_password)
                    if state.get("mode") == PROXY_MODE_KOOKEEY
                    else str(proxy_password)
                )
            if country is not None:
                state["country"] = _normalize_country(country)
            if card_link_countries is not None:
                if not isinstance(card_link_countries, dict):
                    raise ValueError("提链代理国家配置无效")
                selections = _normalize_card_link_countries(
                    state.get("cardLinkCountries")
                )
                for key, selected_country in card_link_countries.items():
                    if key not in CARD_LINK_PROXY_COUNTRY_DEFAULTS:
                        raise ValueError("提链代理国家配置无效")
                    selections[key] = _normalize_country(selected_country)
                state["cardLinkCountries"] = selections
            if card_link_modes is not None:
                if not isinstance(card_link_modes, dict):
                    raise ValueError("提链代理模式配置无效")
                selections = _normalize_card_link_modes(state.get("cardLinkModes"))
                for key, selected_mode in card_link_modes.items():
                    if key not in CARD_LINK_PROXY_MODE_KEYS:
                        raise ValueError("提链代理模式配置无效")
                    mode_value = str(selected_mode or "").strip().casefold()
                    if mode_value not in PROXY_MODES:
                        raise ValueError("提链代理模式配置无效")
                    selections[key] = mode_value
                state["cardLinkModes"] = selections
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
            if state.get("mode") == PROXY_MODE_CLASH:
                state["country"] = "JP"
            if enabled is not None:
                state["enabled"] = bool(enabled)
            if state["enabled"] and not self._configured(state):
                if state.get("mode") == PROXY_MODE_CLASH:
                    raise ValueError("未发现可用的 Clash Controller 与本地代理端口")
                raise ValueError("请先保存动态代理连接信息")
            if (
                state["enabled"]
                and state.get("mode") == PROXY_MODE_KOOKEEY
                and not _KOOKEEY_USERNAME_RE.fullmatch(
                    str(state.get("username") or "").strip()
                )
            ):
                raise ValueError(
                    "Kookeey 用户名应填写完整线路连接用户名（用户ID-安全策略用户名）"
                )
            state["updatedAt"] = _utc_now()
            self._save(state)
            return self._public_state_from(state)

    def next_proxy(self, *, force: bool = False) -> tuple[str, dict[str, Any]]:
        with self._rotation_lock:
            state = self.load()
            if not self._configured(state) or (not state["enabled"] and not force):
                return "", self._public_state_from(state)
            if state.get("mode") == PROXY_MODE_CLASH:
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

            url = self._dynamic_proxy_url(state, _normalize_country(state["country"]))
            return url, self._public_state_from(state)

    @staticmethod
    def _dynamic_proxy_url(state: dict[str, Any], country: str) -> str:
        sid = "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(8)
        )
        endpoint = _validate_endpoint(str(state["endpoint"]))
        parsed = urlsplit(f"http://{endpoint}")
        host = parsed.hostname or ""
        host_text = f"[{host}]" if ":" in host else host
        username = quote(str(state["username"]), safe="")
        password = quote(str(state["password"]), safe="")
        if state.get("mode") == PROXY_MODE_KOOKEEY:
            proxy_password = quote(
                f"{_base_kookeey_password(str(state['password']))}-{country}-{sid}-{int(state['duration'])}m",
                safe="",
            )
            return f"http://{username}:{proxy_password}@{host_text}:{parsed.port}"
        proxy_username = (
            f"{username}-region-{country}-sid-{sid}-t-{int(state['duration'])}"
        )
        return f"http://{proxy_username}:{password}@{host_text}:{parsed.port}"

    def proxy_for_country(
        self, country: str, *, mode: str = ""
    ) -> tuple[str, dict[str, Any]]:
        """Build a fresh proxy for one card-link country without changing registration."""

        with self._rotation_lock:
            state = self.load()
            selected_country = _normalize_country(country)
            selected_mode = str(mode or state.get("mode") or "").strip().casefold()
            if selected_mode not in PROXY_MODES:
                raise ValueError("提链代理模式无效")
            configured = (
                self._clash_mode_configured(state)
                if selected_mode == PROXY_MODE_CLASH
                else (
                    self._kookeey_configured(state)
                    if selected_mode == PROXY_MODE_KOOKEEY
                    else self._dynamic_configured(state)
                )
            )
            if not configured:
                return "", self._public_state_from(state)
            if selected_mode == PROXY_MODE_CLASH:
                if selected_country != "JP":
                    raise ValueError("Clash 提链代理仅支持日本（JP）")
                connection = self._clash_connection(state)
                fixed_ports = self._fixed_port_proxies()
                proxy_url = str(state.get("lastProxyUrl") or "").strip()
                if fixed_ports and proxy_url not in fixed_ports.values():
                    proxy_url = next(iter(fixed_ports.values()))
                return proxy_url or connection.proxy_url, self._public_state_from(state)
            proxy_state = dict(state)
            proxy_state["mode"] = selected_mode
            return (
                self._dynamic_proxy_url(proxy_state, selected_country),
                self._public_state_from(state),
            )

    def proxy_for_test(self) -> tuple[str, dict[str, Any]]:
        """Return a configured proxy URL for diagnostics without rotating Clash."""

        with self._rotation_lock:
            state = self.load()
            if not self._configured(state):
                return "", self._public_state_from(state)
            if state.get("mode") == PROXY_MODE_CLASH:
                connection = self._clash_connection(state)
                proxy_url = str(state.get("lastProxyUrl") or "").strip()
                return proxy_url or connection.proxy_url, self._public_state_from(state)
        return self.next_proxy(force=True)


__all__ = [
    "CARD_LINK_PROXY_SETTING_KEY",
    "CARD_LINK_PROXY_MODE_KEYS",
    "CARD_LINK_PROXY_COUNTRY_DEFAULTS",
    "DEFAULT_PROXY_COUNTRY",
    "DEFAULT_PROXY_MODE",
    "PROXY_MODE_CLASH",
    "PROXY_MODE_DYNAMIC",
    "PROXY_MODE_KOOKEEY",
    "PROXY_COUNTRIES",
    "PROXY_MODES",
    "REGISTRATION_PROXY_SETTING_KEY",
    "RegistrationProxyStore",
    "parse_proxy_credential",
]
