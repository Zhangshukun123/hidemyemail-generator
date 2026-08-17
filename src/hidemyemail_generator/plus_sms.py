"""SMS-Activate adapter for the post-payment Codex add-phone step.

This module intentionally owns a separate service and budget from the PayPal
SMS flow.  PayPal uses service ``ts`` and its country-specific limits; Codex
add-phone uses OpenAI service ``dr`` and a hard per-account budget of $0.10.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx


PLUS_CODEX_SMS_MAX_PRICE_USD = 0.10
PLUS_CODEX_SMS_SERVICE = "openai"
PLUS_CODEX_SMS_SERVICE_CODE = "dr"
SMSBOWER_API_URL = "https://smsbower.page/stubs/handler_api.php"
HERO_SMS_API_URL = "https://hero-sms.com/stubs/handler_api.php"

PAYMENT_SMS_SETTING_KEY = "paypal_sms_config_v1"
SMSBOWER_SETTING_KEY = "smsbower_mail_config_v1"
HERO_SMS_SETTING_KEY = "hero_sms_phone_config_v1"

PROVIDER_SETTINGS: dict[str, dict[str, str]] = {
    "smsbower": {
        "label": "SMSBower",
        "env": "SMSBOWER_API_KEY",
        "setting": SMSBOWER_SETTING_KEY,
        "url": SMSBOWER_API_URL,
    },
    "hero-sms": {
        "label": "HeroSMS",
        "env": "HERO_SMS_API_KEY",
        "setting": HERO_SMS_SETTING_KEY,
        "url": HERO_SMS_API_URL,
    },
}

# SMS-Activate-compatible country identifiers.  Cheap pools are attempted
# first; getNumber does not create a lease when it returns NO_NUMBERS.
PROVIDER_COUNTRY_IDS: dict[str, tuple[tuple[str, int], ...]] = {
    "smsbower": (
        ("US-VIRTUAL", 12),
        ("ID", 6),
        ("PH", 4),
        ("TH", 52),
        ("BR", 73),
        ("GB", 16),
        ("DE", 43),
        ("CA", 36),
        ("MX", 54),
        ("AE", 95),
        ("AU", 175),
        ("US", 187),
    ),
    "hero-sms": (
        ("ID", 6),
        ("PH", 4),
        ("TH", 52),
        ("BR", 73),
        ("GB", 16),
        ("DE", 43),
        ("CA", 36),
        ("MX", 54),
        ("AE", 95),
        ("AU", 175),
        ("US", 187),
    ),
}

_CODE_RE = re.compile(r"^[A-Za-z0-9]{4,10}$")
_WAITING_STATUSES = {"STATUS_WAIT_CODE", "STATUS_WAIT_RESEND"}


class PlusSmsError(RuntimeError):
    """Provider error with API credentials removed from its message."""


@dataclass
class PlusSmsActivation:
    """Duck-typed implementation of gpt_trial_protocol.sms.SmsActivation."""

    phone: str
    activation_id: str | None = None
    provider: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


Requester = Callable[[dict[str, Any]], str]


class PlusSmsCredentialModel:
    """Model: resolve the selected platform and its secret from local storage."""

    def __init__(self, db_file: Path) -> None:
        self.db_file = Path(db_file)

    def _setting(self, key: str) -> dict[str, Any]:
        if not self.db_file.is_file():
            return {}
        try:
            connection = sqlite3.connect(str(self.db_file))
            try:
                row = connection.execute(
                    "SELECT value FROM settings WHERE key = ?", (key,)
                ).fetchone()
            finally:
                connection.close()
            payload = json.loads(str(row[0] or "{}")) if row else {}
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def preferred_provider(self) -> str:
        state = self._setting(PAYMENT_SMS_SETTING_KEY)
        provider = str(state.get("defaultProvider") or "smsbower").strip().lower()
        return provider if provider in PROVIDER_SETTINGS else "smsbower"

    def api_key(self, provider: str) -> str:
        normalized = str(provider or "").strip().lower()
        metadata = PROVIDER_SETTINGS.get(normalized)
        if metadata is None:
            raise ValueError("Plus 接码平台参数不正确")
        explicit = str(os.getenv(metadata["env"]) or "").strip()
        if explicit:
            return explicit
        return str(self._setting(metadata["setting"]).get("apiKey") or "").strip()


class SmsActivateCodexAdapter:
    """Strategy/Adapter: expose one SMS-Activate platform to Codex OAuth."""

    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        requester: Requester | None = None,
        poll_interval_seconds: float = 4.0,
        request_timeout_seconds: float = 20.0,
        country_ids: tuple[tuple[str, int], ...] | None = None,
    ) -> None:
        normalized = str(provider or "").strip().lower()
        metadata = PROVIDER_SETTINGS.get(normalized)
        if metadata is None:
            raise ValueError("Plus 接码平台参数不正确")
        self.name = normalized
        self.label = metadata["label"]
        self.api_url = str(
            os.getenv(f"{normalized.replace('-', '_').upper()}_API_URL")
            or metadata["url"]
        )
        self._api_key = str(api_key or "").strip()
        if not self._api_key:
            raise PlusSmsError(f"请先配置 {self.label} API Key")
        self._requester = requester
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self.request_timeout_seconds = max(1.0, float(request_timeout_seconds))
        self.country_ids = tuple(country_ids or PROVIDER_COUNTRY_IDS[normalized])
        self.last_activation: PlusSmsActivation | None = None

    def _request(self, action: str, **params: Any) -> str:
        query = {"api_key": self._api_key, "action": action, **params}
        if self._requester is not None:
            body = self._requester(query)
        else:
            try:
                response = httpx.get(
                    self.api_url,
                    params=query,
                    headers={"User-Agent": "HME Plus Codex SMS/1.0"},
                    timeout=self.request_timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                )
                response.raise_for_status()
                body = response.text
            except httpx.HTTPError as error:
                raise PlusSmsError(
                    f"{self.label} API 连接失败：{type(error).__name__}"
                ) from None
        text = str(body or "").strip()
        if not text:
            raise PlusSmsError(f"{self.label} API 返回空响应")
        if self._api_key in text:
            text = text.replace(self._api_key, "[REDACTED]")
        return text

    def _error(self, body: str, action: str) -> PlusSmsError:
        code = str(body or "").split(":", 1)[0].strip().upper()[:80]
        messages = {
            "BAD_KEY": f"{self.label} API Key 无效",
            "BAD_SERVICE": f"{self.label} 不支持 OpenAI 接码服务",
            "NO_NUMBERS": f"{self.label} 暂无 $0.10 内的 OpenAI 号码",
            "NO_BALANCE": f"{self.label} 余额不足",
            "NO_ACTIVATION": f"{self.label} 激活记录不存在",
            "STATUS_CANCEL": f"{self.label} 激活已取消",
        }
        return PlusSmsError(messages.get(code) or f"{self.label} {action}失败：{code}")

    def request_phone(self) -> PlusSmsActivation | None:
        body = "NO_NUMBERS"
        for country, country_id in self.country_ids:
            body = self._request(
                "getNumber",
                service=PLUS_CODEX_SMS_SERVICE_CODE,
                country=country_id,
                maxPrice=f"{PLUS_CODEX_SMS_MAX_PRICE_USD:g}",
            )
            if body.startswith("ACCESS_NUMBER:"):
                parts = body.split(":", 2)
                if len(parts) != 3:
                    raise PlusSmsError(f"{self.label} 取号响应格式无效")
                activation_id = parts[1].strip()
                digits = "".join(
                    character for character in parts[2] if character.isdigit()
                )
                if not activation_id or not 8 <= len(digits) <= 20:
                    raise PlusSmsError(f"{self.label} 未返回有效号码")
                activation = PlusSmsActivation(
                    phone=f"+{digits}",
                    activation_id=activation_id,
                    provider=self.name,
                    raw={
                        "country": country,
                        "country_id": country_id,
                        "service": PLUS_CODEX_SMS_SERVICE,
                        "service_code": PLUS_CODEX_SMS_SERVICE_CODE,
                        "max_price": PLUS_CODEX_SMS_MAX_PRICE_USD,
                    },
                )
                self.last_activation = activation
                return activation
            if body.split(":", 1)[0].upper() != "NO_NUMBERS":
                raise self._error(body, "取号")
        if body.split(":", 1)[0].upper() == "NO_NUMBERS":
            return None
        raise self._error(body, "取号")

    def _set_status(self, activation: PlusSmsActivation, status: int) -> bool:
        if not activation.activation_id:
            return False
        body = self._request("setStatus", id=activation.activation_id, status=status)
        expected = {
            1: {"ACCESS_READY"},
            3: {"ACCESS_RETRY_GET", "ACCESS_READY"},
            6: {"ACCESS_ACTIVATION"},
            8: {"ACCESS_CANCEL"},
        }[status]
        if body.split(":", 1)[0] not in expected:
            raise self._error(body, "状态更新")
        return True

    def mark_sent(self, activation: PlusSmsActivation) -> bool:
        # HeroSMS documents polling immediately after send and does not require
        # status=1; SMSBower benefits from the readiness transition.
        return True if self.name == "hero-sms" else self._set_status(activation, 1)

    def request_resend(self, activation: PlusSmsActivation) -> bool:
        return self._set_status(activation, 3)

    def wait_for_otp(
        self,
        activation: PlusSmsActivation,
        *,
        timeout: float = 30.0,
        exclude_codes: Iterable[str] | None = None,
    ) -> str | None:
        excluded = {str(item).strip() for item in (exclude_codes or ())}
        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline:
            body = self._request("getStatus", id=activation.activation_id or "")
            if body.startswith("STATUS_OK:"):
                code = body.split(":", 1)[1].strip().strip("'\"")
                if not _CODE_RE.fullmatch(code):
                    raise PlusSmsError(f"{self.label} 返回的验证码格式无效")
                if code not in excluded:
                    return code
            elif body not in _WAITING_STATUSES:
                raise self._error(body, "取码")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self.poll_interval_seconds, remaining))
        return None

    def complete(self, activation: PlusSmsActivation) -> bool:
        try:
            return self._set_status(activation, 6)
        except PlusSmsError:
            return False

    def cancel(self, activation: PlusSmsActivation) -> bool:
        try:
            return self._set_status(activation, 8)
        except PlusSmsError:
            return False


class PlusSmsProviderFactory:
    """Factory selecting the configured Strategy without exposing its key."""

    def __init__(self, db_file: Path) -> None:
        self.model = PlusSmsCredentialModel(db_file)

    def create(
        self, provider: str = "", *, requester: Requester | None = None
    ) -> SmsActivateCodexAdapter:
        selected = str(provider or self.model.preferred_provider()).strip().lower()
        return SmsActivateCodexAdapter(
            provider=selected,
            api_key=self.model.api_key(selected),
            requester=requester,
        )


def mask_phone(value: str) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return f"+***{digits[-4:]}" if len(digits) > 4 else "****"


__all__ = [
    "HERO_SMS_API_URL",
    "PLUS_CODEX_SMS_MAX_PRICE_USD",
    "PLUS_CODEX_SMS_SERVICE",
    "PLUS_CODEX_SMS_SERVICE_CODE",
    "PlusSmsActivation",
    "PlusSmsCredentialModel",
    "PlusSmsError",
    "PlusSmsProviderFactory",
    "SMSBOWER_API_URL",
    "SmsActivateCodexAdapter",
    "mask_phone",
]
