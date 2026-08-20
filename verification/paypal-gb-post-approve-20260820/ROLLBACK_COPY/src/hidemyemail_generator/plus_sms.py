"""SMS-Activate adapter for the post-payment Codex add-phone step.

This module intentionally owns the OpenAI service code and lifecycle while its
provider, maximum price, and ordered country fallback come from global config.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from .payment_sms import (
    GlobalSmsRoutingConfigStore,
    PROVIDER_METADATA,
    SMS_COUNTRY_CATALOG,
)


PLUS_CODEX_SMS_PROVIDER = "smsbower"
PLUS_CODEX_SMS_CHILE_MAX_PRICE_USD = 0.054
PLUS_CODEX_SMS_US_MAX_PRICE_USD = 0.064
PLUS_CODEX_SMS_MAX_PRICE_USD = PLUS_CODEX_SMS_US_MAX_PRICE_USD
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

# SMS-Activate-compatible country identifiers exposed by the configuration UI.
PROVIDER_COUNTRY_IDS: dict[str, tuple[tuple[str, int], ...]] = {
    "smsbower": (
        ("CL", 151),
        ("US", 187),
        ("BR", 73),
        ("DE", 43),
        ("GB", 16),
        ("JP", 1001),
        ("TH", 52),
        ("ID", 6),
        ("PH", 4),
        ("TW", 55),
        ("MX", 54),
        ("AE", 95),
        ("AU", 175),
        ("CA", 36),
    ),
    "hero-sms": (
        ("CL", 151),
        ("US", 187),
        ("BR", 73),
        ("DE", 43),
        ("GB", 16),
        ("JP", 182),
        ("TH", 52),
        ("ID", 6),
        ("PH", 4),
        ("TW", 55),
        ("MX", 54),
        ("AE", 95),
        ("AU", 175),
        ("CA", 36),
    ),
}

SMSBOWER_CODEX_ROUTES: tuple[tuple[str, int, float], ...] = (
    ("CL", 151, PLUS_CODEX_SMS_CHILE_MAX_PRICE_USD),
    ("US", 187, PLUS_CODEX_SMS_US_MAX_PRICE_USD),
)
_COUNTRY_NAMES = dict(SMS_COUNTRY_CATALOG)

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
LogCallback = Callable[[dict[str, str]], None]


class PlusSmsCredentialModel:
    """Model: resolve the selected platform and its secret from local storage."""

    def __init__(self, db_file: Path) -> None:
        self.db_file = Path(db_file)
        self.routing_store = GlobalSmsRoutingConfigStore(self.db_file)

    def preferred_provider(self) -> str:
        return str(self.routing_store.purpose("binding")["provider"])

    def routing(self) -> dict[str, Any]:
        return self.routing_store.purpose("binding")

    def api_key(self, provider: str) -> str:
        normalized = str(provider or "").strip().lower()
        metadata = PROVIDER_SETTINGS.get(normalized)
        if metadata is None:
            raise ValueError("Plus 接码平台参数不正确")
        return self.routing_store.provider_api_key(normalized)


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
        country_routes: tuple[tuple[str, int, float], ...] | None = None,
        on_log: LogCallback | None = None,
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
        if country_routes is not None:
            routes = tuple(country_routes)
        elif country_ids is not None:
            routes = tuple(
                (country, country_id, PLUS_CODEX_SMS_MAX_PRICE_USD)
                for country, country_id in country_ids
            )
        elif normalized == PLUS_CODEX_SMS_PROVIDER:
            routes = SMSBOWER_CODEX_ROUTES
        else:
            routes = tuple(
                (country, country_id, PLUS_CODEX_SMS_MAX_PRICE_USD)
                for country, country_id in PROVIDER_COUNTRY_IDS[normalized]
            )
        self.country_routes = routes
        self.country_ids = tuple((country, country_id) for country, country_id, _ in routes)
        self._on_log = on_log
        self.last_activation: PlusSmsActivation | None = None

    def _log(self, message: str, *, level: str = "info") -> None:
        if self._on_log is None:
            return
        self._on_log(
            {
                "stage": "sms_route",
                "level": level,
                "message": str(message or "")[:800],
            }
        )

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
            "NO_NUMBERS": f"{self.label} 所选国家线路均无库存",
            "NO_BALANCE": f"{self.label} 余额不足",
            "NO_ACTIVATION": f"{self.label} 激活记录不存在",
            "STATUS_CANCEL": f"{self.label} 激活已取消",
        }
        return PlusSmsError(messages.get(code) or f"{self.label} {action}失败：{code}")

    def request_phone(self) -> PlusSmsActivation | None:
        body = "NO_NUMBERS"
        for route_index, (country, country_id, max_price) in enumerate(
            self.country_routes
        ):
            country_name = _COUNTRY_NAMES.get(country, country)
            self._log(
                f"{self.label} 取号：正在尝试{country_name}（country={country_id}），"
                f"最高 ${max_price:g}"
            )
            body = self._request(
                "getNumber",
                service=PLUS_CODEX_SMS_SERVICE_CODE,
                country=country_id,
                maxPrice=f"{max_price:g}",
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
                        "max_price": max_price,
                    },
                )
                self.last_activation = activation
                self._log(
                    f"{self.label} {country_name}线路取号成功，号码已脱敏保存",
                    level="success",
                )
                return activation
            code = body.split(":", 1)[0].upper()
            if code != "NO_NUMBERS":
                self._log(
                    f"{self.label} {country_name}线路取号失败：{code}",
                    level="error",
                )
                raise self._error(body, "取号")
            if route_index + 1 < len(self.country_routes):
                next_country, next_country_id, next_max_price = self.country_routes[
                    route_index + 1
                ]
                next_name = _COUNTRY_NAMES.get(next_country, next_country)
                self._log(
                    f"{self.label} {country_name}线路无库存；仅因此回退"
                    f"{next_name}（country={next_country_id}），最高 ${next_max_price:g}",
                    level="warning",
                )
            else:
                self._log(
                    f"{self.label} {country_name}线路无库存，本次取号结束",
                    level="warning",
                )
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
        self,
        provider: str = "",
        *,
        requester: Requester | None = None,
        on_log: LogCallback | None = None,
        purpose: str = "binding",
        countries: Iterable[str] | None = None,
        max_price: float | None = None,
    ) -> SmsActivateCodexAdapter:
        policy = self.model.routing() if purpose == "binding" else {}
        selected = str(
            policy.get("provider") or provider or self.model.preferred_provider()
        ).strip().lower()
        selected_countries = [
            str(country or "").strip().upper()
            for country in (countries or policy.get("countries") or ())
        ]
        route_price = float(
            max_price
            if max_price is not None
            else policy.get("maxPrice") or PLUS_CODEX_SMS_MAX_PRICE_USD
        )
        country_ids = dict(PROVIDER_COUNTRY_IDS.get(selected) or ())
        routes: list[tuple[str, int, float]] = []
        for country in selected_countries:
            country_id = country_ids.get(country)
            if country_id is None:
                raise PlusSmsError(
                    f"{PROVIDER_METADATA.get(selected, {}).get('label', selected)} "
                    f"不支持所选国家 {country}"
                )
            routes.append((country, country_id, route_price))
        if not routes:
            raise PlusSmsError("绑定手机号接码国家未配置")
        return SmsActivateCodexAdapter(
            provider=selected,
            api_key=self.model.api_key(selected),
            requester=requester,
            on_log=on_log,
            country_routes=tuple(routes),
        )


def mask_phone(value: str) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return f"+***{digits[-4:]}" if len(digits) > 4 else "****"


__all__ = [
    "HERO_SMS_API_URL",
    "PLUS_CODEX_SMS_CHILE_MAX_PRICE_USD",
    "PLUS_CODEX_SMS_MAX_PRICE_USD",
    "PLUS_CODEX_SMS_PROVIDER",
    "PLUS_CODEX_SMS_SERVICE",
    "PLUS_CODEX_SMS_SERVICE_CODE",
    "PLUS_CODEX_SMS_US_MAX_PRICE_USD",
    "PlusSmsActivation",
    "PlusSmsCredentialModel",
    "PlusSmsError",
    "PlusSmsProviderFactory",
    "SMSBOWER_API_URL",
    "SMSBOWER_CODEX_ROUTES",
    "SmsActivateCodexAdapter",
    "mask_phone",
]
