from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from .inbox import connect_db


SMSBOWER_SETTING_KEY = "smsbower_mail_config_v1"
SMSBOWER_ACTIVATIONS_SETTING_KEY = "smsbower_mail_activations_v1"
SMSBOWER_API_BASE_URL = "https://smsbower.page"
SMSBOWER_API_DOCS_URL = "https://smsbower.app/cn/api?page=mails"
DEFAULT_SMSBOWER_SERVICE = "dr"
DEFAULT_SMSBOWER_DOMAIN = "gmail.com"
DEFAULT_SMSBOWER_MAX_PRICE = 0.05
_WAITING_CODE_ERRORS = {
    "code has not been received yet, please try again later",
    "code has not been received yet",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _successful(payload: dict[str, Any]) -> bool:
    value = payload.get("status")
    return value is True or str(value or "").strip().lower() in {"1", "success"}


def _api_error(payload: dict[str, Any], fallback: str) -> str:
    return str(payload.get("error") or payload.get("message") or fallback).strip()


def _normalize_service(value: Any) -> str:
    service = str(value or DEFAULT_SMSBOWER_SERVICE).strip().lower()
    if not service or len(service) > 32 or not service.replace("_", "").isalnum():
        raise ValueError("SMSBower 服务代码格式无效")
    return service


def _normalize_domain(value: Any) -> str:
    domain = str(value or DEFAULT_SMSBOWER_DOMAIN).strip().lower()
    if domain != DEFAULT_SMSBOWER_DOMAIN:
        raise ValueError("自动注册当前仅支持 SMSBower Gmail")
    return domain


def _normalize_max_price(value: Any) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError):
        raise ValueError("Gmail 最高价格式无效") from None
    if not 0.001 <= price <= 10:
        raise ValueError("Gmail 最高价必须在 0.001–10 美元之间")
    return round(price, 4)


class SMSBowerConfigStore:
    """Persist the SMSBower mail API key without returning it to the browser."""

    def __init__(
        self,
        db_file: Path,
        *,
        api_key: str = "",
        service: str = DEFAULT_SMSBOWER_SERVICE,
        domain: str = DEFAULT_SMSBOWER_DOMAIN,
        max_price: float = DEFAULT_SMSBOWER_MAX_PRICE,
    ) -> None:
        self.db_file = Path(db_file)
        self.initial_api_key = str(api_key or "").strip()
        self.initial_service = _normalize_service(service)
        self.initial_domain = _normalize_domain(domain)
        self.initial_max_price = _normalize_max_price(max_price)

    def _defaults(self) -> dict[str, Any]:
        return {
            "apiKey": self.initial_api_key,
            "service": self.initial_service,
            "domain": self.initial_domain,
            "maxPrice": self.initial_max_price,
            "updatedAt": "",
        }

    def load(self) -> dict[str, Any]:
        conn = connect_db(str(self.db_file))
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (SMSBOWER_SETTING_KEY,)
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
                for key in state:
                    if key in stored:
                        state[key] = stored[key]
        state["apiKey"] = str(state.get("apiKey") or "").strip()
        state["service"] = _normalize_service(state.get("service"))
        state["domain"] = _normalize_domain(state.get("domain"))
        state["maxPrice"] = _normalize_max_price(state.get("maxPrice"))
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
                    SMSBOWER_SETTING_KEY,
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def configure(
        self,
        *,
        api_key: str | None = None,
        service: str | None = None,
        max_price: Any | None = None,
    ) -> dict[str, Any]:
        state = self.load()
        if api_key is not None:
            normalized_key = str(api_key or "").strip()
            if normalized_key and len(normalized_key) < 8:
                raise ValueError("SMSBower API Key 长度无效")
            state["apiKey"] = normalized_key
        if service is not None:
            state["service"] = _normalize_service(service)
        if max_price is not None:
            state["maxPrice"] = _normalize_max_price(max_price)
        state["updatedAt"] = _utc_now()
        self._save(state)
        return self.public_state()

    def public_state(self) -> dict[str, Any]:
        state = self.load()
        return {
            "configured": bool(state["apiKey"]),
            "service": state["service"],
            "domain": state["domain"],
            "maxPrice": state["maxPrice"],
            "updatedAt": state["updatedAt"],
            "docsUrl": SMSBOWER_API_DOCS_URL,
        }


@dataclass
class SMSBowerMailActivation:
    email: str
    mail_id: str
    code_received: bool = False
    last_code: str = ""
    last_code_at: str = ""
    waiting_next_code: bool = False


class SMSBowerMailClient:
    """Acquire Gmail activations and relay verification codes from SMSBower."""

    def __init__(
        self,
        config_store: SMSBowerConfigStore,
        *,
        base_url: str = SMSBOWER_API_BASE_URL,
        timeout_seconds: float = 20,
    ) -> None:
        self.config_store = config_store
        self.base_url = str(base_url or SMSBOWER_API_BASE_URL).strip().rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._activations = self._load_activations()
        self._lock = asyncio.Lock()

    def _load_activations(self) -> dict[str, SMSBowerMailActivation]:
        self.config_store.db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = connect_db(str(self.config_store.db_file))
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (SMSBOWER_ACTIVATIONS_SETTING_KEY,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {}
        try:
            payload = json.loads(str(row["value"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        activations: dict[str, SMSBowerMailActivation] = {}
        for email, item in payload.items():
            target = str(email or "").strip().lower()
            state = item if isinstance(item, dict) else {}
            mail_id = str(state.get("mailId") or "").strip()
            if not target.endswith("@gmail.com") or not mail_id:
                continue
            activations[target] = SMSBowerMailActivation(
                email=target,
                mail_id=mail_id,
                code_received=bool(state.get("codeReceived")),
                last_code=str(state.get("lastCode") or "").strip(),
                last_code_at=str(state.get("lastCodeAt") or "").strip(),
                waiting_next_code=bool(state.get("waitingNextCode")),
            )
        return activations

    def _save_activations(self) -> None:
        payload = {
            email: {
                "mailId": item.mail_id,
                "codeReceived": item.code_received,
                "lastCode": item.last_code,
                "lastCodeAt": item.last_code_at,
                "waitingNextCode": item.waiting_next_code,
            }
            for email, item in self._activations.items()
        }
        conn = connect_db(str(self.config_store.db_file))
        try:
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (
                    SMSBOWER_ACTIVATIONS_SETTING_KEY,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def public_state(self) -> dict[str, Any]:
        return {
            **self.config_store.public_state(),
            "active": len(self._activations),
        }

    async def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{self.base_url}{path}", params=params
                ) as response:
                    body = await response.text()
                    if response.status >= 400:
                        raise RuntimeError(f"SMSBower API HTTP {response.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise RuntimeError(f"连接 SMSBower API 失败：{type(error).__name__}") from None
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError("SMSBower API 返回了无效 JSON") from None
        if not isinstance(payload, dict):
            raise RuntimeError("SMSBower API 返回格式无效")
        return payload

    def _config(self) -> dict[str, Any]:
        config = self.config_store.load()
        if not config["apiKey"]:
            raise RuntimeError("请先设置 SMSBower API Key")
        return config

    async def acquire_email(self, _label: str = "") -> str:
        config = self._config()
        payload = await self._request(
            "/api/mail/getActivation",
            {
                "api_key": config["apiKey"],
                "service": config["service"],
                "domain": config["domain"],
                "maxPrice": config["maxPrice"],
                "alias": 0,
            },
        )
        if not _successful(payload):
            raise RuntimeError(
                "SMSBower Gmail 获取失败："
                + _api_error(payload, "服务未返回邮箱")
            )
        email = str(payload.get("mail") or "").strip().lower()
        mail_id = str(payload.get("mailId") or "").strip()
        if not email.endswith("@gmail.com") or not mail_id:
            raise RuntimeError("SMSBower API 未返回有效的 Gmail 激活记录")
        async with self._lock:
            self._activations[email] = SMSBowerMailActivation(email, mail_id)
            self._save_activations()
        return email

    async def poll_code(self, email: str) -> str:
        return await self._poll_code(email, accept_previous=True)

    async def _poll_code(self, email: str, *, accept_previous: bool) -> str:
        target = str(email or "").strip().lower()
        async with self._lock:
            activation = self._activations.get(target)
        if activation is None:
            raise RuntimeError("未找到该 Gmail 的 SMSBower 激活记录")
        config = self._config()
        payload = await self._request(
            "/api/mail/getCode",
            {"api_key": config["apiKey"], "mailId": activation.mail_id},
        )
        if not _successful(payload):
            error = _api_error(payload, "验证码尚未到达")
            if error.strip().lower() in _WAITING_CODE_ERRORS:
                return ""
            raise RuntimeError(f"SMSBower Gmail 验证码获取失败：{error}")
        code = "".join(
            character
            for character in str(payload.get("code") or "")
            if character.isalnum()
        )
        if not 4 <= len(code) <= 10:
            raise RuntimeError("SMSBower API 返回的验证码格式无效")
        async with self._lock:
            current = self._activations.get(target)
            if current is not None:
                if not accept_previous and current.last_code == code:
                    return ""
                current.code_received = True
                current.last_code = code
                current.last_code_at = _utc_now()
                current.waiting_next_code = False
                self._save_activations()
        return code

    async def poll_next_code(self, email: str) -> str:
        """Poll a persisted Gmail activation for a code after registration."""

        target = str(email or "").strip().lower()
        async with self._lock:
            activation = self._activations.get(target)
        if activation is None:
            raise RuntimeError(
                "该 Gmail 是旧记录，未保存 SMSBower mailId；请使用新注册账号获取验证码"
            )
        if not activation.waiting_next_code:
            config = self._config()
            payload = await self._request(
                "/api/mail/setStatus",
                {
                    "api_key": config["apiKey"],
                    "id": activation.mail_id,
                    "status": 5,
                },
            )
            if not _successful(payload):
                raise RuntimeError(
                    "SMSBower 等待下一验证码失败："
                    + _api_error(payload, "状态更新失败")
                )
            async with self._lock:
                current = self._activations.get(target)
                if current is not None:
                    current.waiting_next_code = True
                    self._save_activations()
        return await self._poll_code(target, accept_previous=False)

    async def complete_email(self, email: str, success: bool, _message: str) -> None:
        target = str(email or "").strip().lower()
        async with self._lock:
            activation = self._activations.get(target)
        if activation is None:
            return
        config = self._config()
        keep_for_next_code = bool(success and activation.code_received)
        status = 5 if keep_for_next_code else 3 if activation.code_received else 2
        payload = await self._request(
            "/api/mail/setStatus",
            {
                "api_key": config["apiKey"],
                "id": activation.mail_id,
                "status": status,
            },
        )
        if not _successful(payload):
            raise RuntimeError(
                "SMSBower 激活回执失败：" + _api_error(payload, "状态更新失败")
            )
        async with self._lock:
            current = self._activations.get(target)
            if keep_for_next_code and current is not None:
                current.waiting_next_code = True
            else:
                self._activations.pop(target, None)
            self._save_activations()


__all__ = [
    "DEFAULT_SMSBOWER_DOMAIN",
    "DEFAULT_SMSBOWER_MAX_PRICE",
    "DEFAULT_SMSBOWER_SERVICE",
    "SMSBOWER_API_BASE_URL",
    "SMSBOWER_API_DOCS_URL",
    "SMSBowerConfigStore",
    "SMSBowerMailClient",
]
