"""SMSBower phone activation client used by the PayPal web flow.

The client speaks SMSBower's SMS-Activate-compatible HTTP API and deliberately
keeps the API key out of public state, exceptions, and logs.  It can reuse the
key already stored by the parent Hide My Email application or read a dedicated
``SMSBOWER_API_KEY`` environment variable when this project runs standalone.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from paypal.sms_config import SmsSettingsModel


SMSBOWER_API_URL = "https://smsbower.page/stubs/handler_api.php"
SMSBOWER_API_DOCS_URL = "https://smsbower.app/cn/api?page=client"
SMSBOWER_PAYPAL_SERVICE = "paypal"
SMSBOWER_PAYPAL_SERVICE_CODE = "ts"
SMSBOWER_SETTING_KEY = "smsbower_mail_config_v1"
DEFAULT_MAX_PRICE = 3.0
DEFAULT_POLL_INTERVAL_SECONDS = 4.0
DEFAULT_OTP_TIMEOUT_SECONDS = 60.0

# SMSBower uses the established SMS-Activate numeric country identifiers.
# These are the PayPal countries exposed by the verified web workflow.
COUNTRY_IDS: dict[str, int] = {
    "BR": 73,
    "DE": 43,
    "GB": 16,
    "US": 187,
    "JP": 1001,
    "TH": 52,
    "ID": 6,
    "PH": 4,
    "TW": 55,
    "MX": 54,
    "AE": 95,
    "AU": 175,
    "CA": 36,
}
SMSBOWER_US_VIRTUAL_COUNTRY_ID = 12
COUNTRY_PURCHASE_IDS: dict[str, tuple[int, ...]] = {
    # Prefer the inexpensive US virtual pool shown by SMSBower, while keeping
    # the ordinary United States pool as a bounded fallback.
    "US": (SMSBOWER_US_VIRTUAL_COUNTRY_ID, COUNTRY_IDS["US"]),
}

_WAITING_STATUSES = {"STATUS_WAIT_CODE", "STATUS_WAIT_RESEND"}
_TERMINAL_ERRORS = {"NO_ACTIVATION", "STATUS_CANCEL"}
_CODE_RE = re.compile(r"^[A-Za-z0-9]{4,10}$")


class SMSBowerPhoneError(RuntimeError):
    """A normalized provider or configuration failure."""


class SMSBowerPhoneCancelled(SMSBowerPhoneError):
    """The local PayPal task stopped while waiting for SMS."""


@dataclass(frozen=True)
class SMSBowerPhoneActivation:
    activation_id: str
    phone: str
    country: str
    service: str = SMSBOWER_PAYPAL_SERVICE
    is_virtual: bool = False
    provider: str = "smsbower"


@dataclass(frozen=True)
class SmsActivateProviderSpec:
    """Provider metadata injected into the SMS-Activate client strategy."""

    provider_id: str
    label: str
    api_url: str
    docs_url: str
    country_ids: dict[str, int]
    country_purchase_ids: dict[str, tuple[int, ...]]
    virtual_country_ids: frozenset[int] = frozenset()
    service: str = SMSBOWER_PAYPAL_SERVICE
    service_code: str = SMSBOWER_PAYPAL_SERVICE_CODE
    mark_sent_status: int | None = 1


SMSBOWER_PROVIDER_SPEC = SmsActivateProviderSpec(
    provider_id="smsbower",
    label="SMSBower",
    api_url=SMSBOWER_API_URL,
    docs_url=SMSBOWER_API_DOCS_URL,
    country_ids=COUNTRY_IDS,
    country_purchase_ids=COUNTRY_PURCHASE_IDS,
    virtual_country_ids=frozenset({SMSBOWER_US_VIRTUAL_COUNTRY_ID}),
)


Requester = Callable[[dict[str, Any]], str]
ApiKeyResolver = Callable[[], str]


def _positive_float(value: Any, *, label: str, minimum: float, maximum: float) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}格式无效") from None
    if not minimum <= normalized <= maximum:
        raise ValueError(f"{label}必须在 {minimum:g}–{maximum:g} 美元之间")
    return round(normalized, 4)


def _setting_api_key(db_file: Path) -> str:
    if not db_file.is_file():
        return ""
    try:
        connection = sqlite3.connect(str(db_file))
        try:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (SMSBOWER_SETTING_KEY,)
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return ""
        payload = json.loads(str(row[0] or "{}"))
        return str(payload.get("apiKey") or "").strip() if isinstance(payload, dict) else ""
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return ""


def resolve_api_key() -> str:
    """Resolve a dedicated key first, then reuse the parent app's local key."""

    return SmsSettingsModel().api_key("smsbower")


class SMSBowerPhoneClient:
    """Buy PayPal numbers, poll OTPs, and finalize SMSBower activations."""

    def __init__(
        self,
        *,
        api_url: str = SMSBOWER_API_URL,
        api_key: str = "",
        api_key_resolver: ApiKeyResolver | None = None,
        requester: Requester | None = None,
        timeout_seconds: float = 20.0,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        otp_timeout_seconds: float = DEFAULT_OTP_TIMEOUT_SECONDS,
        provider_spec: SmsActivateProviderSpec = SMSBOWER_PROVIDER_SPEC,
    ) -> None:
        self.spec = provider_spec
        self.provider_id = provider_spec.provider_id
        self.provider_label = provider_spec.label
        self.api_url = str(api_url or provider_spec.api_url).strip()
        self._api_key = str(api_key or "").strip()
        self._api_key_resolver = api_key_resolver or resolve_api_key
        self._requester = requester
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self.otp_timeout_seconds = max(1.0, float(otp_timeout_seconds))

    def _key(self) -> str:
        key = self._api_key or str(self._api_key_resolver() or "").strip()
        if not key:
            raise SMSBowerPhoneError(
                f"请先在接码配置中设置 {self.provider_label} API Key"
            )
        return key

    def configured(self) -> bool:
        try:
            return bool(self._api_key or self._api_key_resolver())
        except Exception:
            return False

    def _request(self, action: str, **params: Any) -> str:
        query = {"api_key": self._key(), "action": action, **params}
        if self._requester is not None:
            body = self._requester(query)
        else:
            try:
                response = httpx.get(
                    self.api_url,
                    params=query,
                    headers={"User-Agent": f"PAY.153 {self.provider_label}/1.0"},
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                )
                response.raise_for_status()
                body = response.text
            except httpx.HTTPError as error:
                raise SMSBowerPhoneError(
                    f"{self.provider_label} API 连接失败：{type(error).__name__}"
                ) from None
        text = str(body or "").strip()
        if not text:
            raise SMSBowerPhoneError(f"{self.provider_label} API 返回空响应")
        return text

    def _provider_error(self, body: str, *, action: str) -> SMSBowerPhoneError:
        code = str(body or "").strip().split(":", 1)[0].upper()
        messages = {
            "BAD_KEY": f"{self.provider_label} API Key 无效",
            "BAD_ACTION": f"{self.provider_label} API 操作无效",
            "BAD_SERVICE": f"{self.provider_label} 不支持 PayPal 服务代码",
            "BAD_COUNTRY": f"{self.provider_label} 不支持所选国家",
            "NO_NUMBERS": f"{self.provider_label} 当前没有符合条件的 PayPal 号码",
            "NO_BALANCE": f"{self.provider_label} 余额不足",
            "NO_ACTIVATION": f"{self.provider_label} 激活记录不存在",
            "STATUS_CANCEL": f"{self.provider_label} 激活已取消",
            "EARLY_CANCEL_DENIED": f"{self.provider_label} 购买后暂不可取消，请稍后在号码历史中处理",
        }
        message = messages.get(code) or f"{self.provider_label} {action}失败：{code[:80]}"
        return SMSBowerPhoneError(message)

    def balance(self) -> float:
        body = self._request("getBalance")
        if not body.startswith("ACCESS_BALANCE:"):
            raise self._provider_error(body, action="余额查询")
        try:
            return round(float(body.split(":", 1)[1]), 4)
        except (TypeError, ValueError):
            raise SMSBowerPhoneError(
                f"{self.provider_label} 余额响应格式无效"
            ) from None

    def price(self, country: str) -> dict[str, Any]:
        normalized_country = str(country or "").strip().upper()
        country_id = self.spec.country_ids.get(normalized_country)
        if country_id is None:
            return {"supported": False, "country": normalized_country, "price": None, "count": 0}
        routes: list[dict[str, Any]] = []
        for candidate_id in self.spec.country_purchase_ids.get(
            normalized_country, (country_id,)
        ):
            body = self._request(
                "getPrices",
                service=self.spec.service_code,
                country=candidate_id,
            )
            try:
                payload = json.loads(body)
                country_state = payload.get(str(candidate_id), {})
                service_state = country_state.get(
                    self.spec.service_code, {}
                )
                routes.append(
                    {
                        "countryId": candidate_id,
                        "price": round(float(service_state.get("cost")), 4),
                        "count": max(0, int(service_state.get("count") or 0)),
                        "virtual": candidate_id in self.spec.virtual_country_ids,
                    }
                )
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                raise self._provider_error(body, action="价格查询") from None
        available_routes = [item for item in routes if item["count"] > 0]
        selected = min(available_routes or routes, key=lambda item: item["price"])
        return {
            "supported": True,
            "country": normalized_country,
            **selected,
            "routes": routes,
        }

    def public_status(self, *, country: str = "BR", probe: bool = True) -> dict[str, Any]:
        configured = self.configured()
        state: dict[str, Any] = {
            "configured": configured,
            "provider": self.provider_id,
            "label": self.provider_label,
            "service": self.spec.service,
            "serviceCode": self.spec.service_code,
            "country": str(country or "BR").strip().upper(),
            "supportedCountries": sorted(self.spec.country_ids),
            "defaultMaxPrice": DEFAULT_MAX_PRICE,
            "docsUrl": self.spec.docs_url,
            "balance": None,
            "price": None,
            "count": 0,
            "error": "",
        }
        if not configured or not probe:
            return state
        try:
            state["balance"] = self.balance()
            state.update(self.price(state["country"]))
        except SMSBowerPhoneError as error:
            state["error"] = str(error)
        return state

    def acquire_phone(
        self,
        country: str,
        *,
        max_price: Any = DEFAULT_MAX_PRICE,
    ) -> SMSBowerPhoneActivation:
        normalized_country = str(country or "").strip().upper()
        country_id = self.spec.country_ids.get(normalized_country)
        if country_id is None:
            raise SMSBowerPhoneError(
                f"{self.provider_label} 自动取号暂不支持所选 PayPal 国家"
            )
        normalized_price = _positive_float(
            max_price,
            label=f"{self.provider_label} PayPal 最高价",
            minimum=0.001,
            maximum=50,
        )
        selected_country_id = country_id
        body = ""
        for candidate_id in self.spec.country_purchase_ids.get(
            normalized_country, (country_id,)
        ):
            selected_country_id = candidate_id
            body = self._request(
                "getNumber",
                service=self.spec.service_code,
                country=candidate_id,
                maxPrice=f"{normalized_price:g}",
            )
            if body.startswith("ACCESS_NUMBER:"):
                break
            if body.split(":", 1)[0].upper() != "NO_NUMBERS":
                raise self._provider_error(body, action="取号")
        if not body.startswith("ACCESS_NUMBER:"):
            raise self._provider_error(body, action="取号")
        parts = body.split(":", 2)
        if len(parts) != 3:
            raise SMSBowerPhoneError(f"{self.provider_label} 取号响应格式无效")
        activation_id = parts[1].strip()
        phone_digits = "".join(character for character in parts[2] if character.isdigit())
        if not activation_id or not 8 <= len(phone_digits) <= 20:
            raise SMSBowerPhoneError(
                f"{self.provider_label} 未返回有效的激活 ID 或手机号"
            )
        return SMSBowerPhoneActivation(
            activation_id=activation_id,
            phone=f"+{phone_digits}",
            country=normalized_country,
            service=self.spec.service,
            is_virtual=(selected_country_id in self.spec.virtual_country_ids),
            provider=self.provider_id,
        )

    def set_status(self, activation: SMSBowerPhoneActivation, status: int) -> str:
        body = self._request(
            "setStatus", id=activation.activation_id, status=int(status)
        )
        expected = {
            1: {"ACCESS_READY"},
            3: {"ACCESS_RETRY_GET", "ACCESS_READY"},
            6: {"ACCESS_ACTIVATION"},
            8: {"ACCESS_CANCEL"},
        }.get(int(status), set())
        if body.split(":", 1)[0] not in expected:
            raise self._provider_error(body, action="状态更新")
        return body

    def mark_sent(self, activation: SMSBowerPhoneActivation) -> None:
        if self.spec.mark_sent_status is not None:
            self.set_status(activation, self.spec.mark_sent_status)

    def request_another(self, activation: SMSBowerPhoneActivation) -> None:
        self.set_status(activation, 3)

    def complete(self, activation: SMSBowerPhoneActivation) -> None:
        self.set_status(activation, 6)

    def cancel(self, activation: SMSBowerPhoneActivation) -> None:
        self.set_status(activation, 8)

    def poll_code(self, activation: SMSBowerPhoneActivation) -> str:
        body = self._request("getStatus", id=activation.activation_id)
        if body.startswith("STATUS_OK:"):
            code = body.split(":", 1)[1].strip().strip("'\"")
            if not _CODE_RE.fullmatch(code):
                raise SMSBowerPhoneError(
                    f"{self.provider_label} 返回的验证码格式无效"
                )
            return code
        prefix = body.split(":", 1)[0]
        if prefix in _WAITING_STATUSES or prefix == "STATUS_WAIT_RETRY":
            return ""
        if prefix in _TERMINAL_ERRORS:
            raise self._provider_error(prefix, action="取码")
        raise self._provider_error(body, action="取码")

    def wait_for_code(
        self,
        activation: SMSBowerPhoneActivation,
        *,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        timeout = max(1.0, float(timeout_seconds or self.otp_timeout_seconds))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise SMSBowerPhoneCancelled("短信等待已随任务停止")
            code = self.poll_code(activation)
            if code:
                return code
            remaining = max(0.0, deadline - time.monotonic())
            wait_time = min(self.poll_interval_seconds, remaining)
            if wait_time <= 0:
                break
            if cancel_event is not None:
                if cancel_event.wait(wait_time):
                    raise SMSBowerPhoneCancelled("短信等待已随任务停止")
            else:
                time.sleep(wait_time)
        raise SMSBowerPhoneError(
            f"{self.provider_label} PayPal 短信验证码等待超时"
        )


__all__ = [
    "COUNTRY_IDS",
    "COUNTRY_PURCHASE_IDS",
    "DEFAULT_MAX_PRICE",
    "SMSBOWER_API_DOCS_URL",
    "SMSBOWER_API_URL",
    "SMSBOWER_PAYPAL_SERVICE",
    "SMSBOWER_PAYPAL_SERVICE_CODE",
    "SMSBOWER_US_VIRTUAL_COUNTRY_ID",
    "SMSBOWER_PROVIDER_SPEC",
    "SmsActivateProviderSpec",
    "SMSBowerPhoneActivation",
    "SMSBowerPhoneCancelled",
    "SMSBowerPhoneClient",
    "SMSBowerPhoneError",
    "resolve_api_key",
]
