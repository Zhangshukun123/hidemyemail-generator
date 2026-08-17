"""Global SMS routing settings shared by phone binding and PayPal."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .inbox import connect_db


PAYMENT_SMS_SETTING_KEY = "paypal_sms_config_v1"
GLOBAL_SMS_ROUTING_SETTING_KEY = "global_sms_routing_config_v1"
SMSBOWER_SETTING_KEY = "smsbower_mail_config_v1"
HERO_SMS_SETTING_KEY = "hero_sms_phone_config_v1"
AUTOMATIC_PAYMENT_SMS_PROVIDERS = ("smsbower", "hero-sms")
SMS_PURPOSES = ("binding", "paypal")

SMS_COUNTRY_CATALOG: tuple[tuple[str, str], ...] = (
    ("CL", "智利"),
    ("US", "美国"),
    ("BR", "巴西"),
    ("DE", "德国"),
    ("GB", "英国"),
    ("JP", "日本"),
    ("TH", "泰国"),
    ("ID", "印度尼西亚"),
    ("PH", "菲律宾"),
    ("TW", "中国台湾"),
    ("MX", "墨西哥"),
    ("AE", "阿联酋"),
    ("AU", "澳大利亚"),
    ("CA", "加拿大"),
)
BINDING_SMS_COUNTRIES = tuple(code for code, _ in SMS_COUNTRY_CATALOG)
PAYPAL_SMS_COUNTRIES = tuple(
    code for code in BINDING_SMS_COUNTRIES if code != "CL"
)

DEFAULT_SMS_ROUTING: dict[str, dict[str, Any]] = {
    "binding": {
        "provider": "smsbower",
        "maxPrice": 0.064,
        "countries": ["CL", "US"],
    },
    "paypal": {
        "provider": "smsbower",
        "maxPrice": 0.30,
        "countries": list(PAYPAL_SMS_COUNTRIES),
    },
}

PROVIDER_METADATA = {
    "smsbower": {
        "label": "SMSBower",
        "env": "SMSBOWER_API_KEY",
        "setting": SMSBOWER_SETTING_KEY,
    },
    "hero-sms": {
        "label": "HeroSMS",
        "env": "HERO_SMS_API_KEY",
        "setting": HERO_SMS_SETTING_KEY,
    },
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


@dataclass(frozen=True)
class ResolvedPaymentSmsProvider:
    provider: str
    label: str
    virtual_us: bool


class GlobalSmsRoutingConfigStore:
    """Persist one global policy per SMS purpose without exposing API keys."""

    def __init__(self, db_file: Path) -> None:
        self.db_file = Path(db_file)

    def _setting(self, key: str) -> dict[str, Any]:
        if not self.db_file.is_file():
            return {}
        connection = connect_db(str(self.db_file))
        try:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return {}
        value = row["value"] if hasattr(row, "keys") else row[0]
        return _json_object(value)

    def _save_setting(self, key: str, payload: dict[str, Any]) -> None:
        connection = connect_db(str(self.db_file))
        try:
            connection.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _purpose(value: str) -> str:
        purpose = str(value or "").strip().lower()
        if purpose not in SMS_PURPOSES:
            raise ValueError("接码用途参数不正确")
        return purpose

    @staticmethod
    def _provider(value: Any) -> str:
        provider = str(value or "").strip().lower()
        if provider not in AUTOMATIC_PAYMENT_SMS_PROVIDERS:
            raise ValueError("接码平台参数不正确")
        return provider

    @staticmethod
    def _max_price(value: Any) -> float:
        try:
            price = round(float(value), 4)
        except (TypeError, ValueError):
            raise ValueError("接码最高价格式无效") from None
        if not 0.001 <= price <= 50:
            raise ValueError("接码最高价必须在 0.001–50 美元之间")
        return price

    @staticmethod
    def _countries(purpose: str, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("接码国家必须是多选列表")
        allowed = (
            BINDING_SMS_COUNTRIES if purpose == "binding" else PAYPAL_SMS_COUNTRIES
        )
        selected: list[str] = []
        for item in value:
            country = str(item or "").strip().upper()
            if country not in allowed:
                raise ValueError(f"{purpose} 接码国家 {country or '?'} 不受支持")
            if country not in selected:
                selected.append(country)
        if not selected:
            raise ValueError("每个接码用途至少选择一个国家")
        return selected

    def provider_api_key(self, provider: str) -> str:
        selected = self._provider(provider)
        metadata = PROVIDER_METADATA[selected]
        explicit = str(os.getenv(metadata["env"]) or "").strip()
        if explicit:
            return explicit
        return str(self._setting(metadata["setting"]).get("apiKey") or "").strip()

    def provider_configured(self, provider: str) -> bool:
        return bool(self.provider_api_key(provider))

    def configure_provider_key(self, provider: str, api_key: Any) -> None:
        selected = self._provider(provider)
        value = str(api_key or "").strip()
        if not value:
            return
        if not 8 <= len(value) <= 512:
            raise ValueError(f"{PROVIDER_METADATA[selected]['label']} API Key 长度无效")
        metadata = PROVIDER_METADATA[selected]
        state = self._setting(metadata["setting"])
        state.update(apiKey=value, updatedAt=_utc_now())
        self._save_setting(metadata["setting"], state)

    def purpose(self, purpose: str) -> dict[str, Any]:
        selected_purpose = self._purpose(purpose)
        raw = self._setting(GLOBAL_SMS_ROUTING_SETTING_KEY)
        stored = raw.get(selected_purpose)
        stored = stored if isinstance(stored, dict) else {}
        defaults = DEFAULT_SMS_ROUTING[selected_purpose]
        provider = str(stored.get("provider") or "").strip().lower()
        if provider not in AUTOMATIC_PAYMENT_SMS_PROVIDERS:
            if selected_purpose == "paypal":
                legacy = str(
                    self._setting(PAYMENT_SMS_SETTING_KEY).get("defaultProvider") or ""
                ).strip().lower()
                provider = (
                    legacy
                    if legacy in AUTOMATIC_PAYMENT_SMS_PROVIDERS
                    else str(defaults["provider"])
                )
            else:
                provider = str(defaults["provider"])
        try:
            max_price = self._max_price(stored.get("maxPrice", defaults["maxPrice"]))
        except ValueError:
            max_price = float(defaults["maxPrice"])
        try:
            countries = self._countries(
                selected_purpose, stored.get("countries", defaults["countries"])
            )
        except ValueError:
            countries = list(defaults["countries"])
        return {
            "provider": provider,
            "providerLabel": PROVIDER_METADATA[provider]["label"],
            "configured": self.provider_configured(provider),
            "maxPrice": max_price,
            "countries": countries,
        }

    def public_state(self) -> dict[str, Any]:
        state = self._setting(GLOBAL_SMS_ROUTING_SETTING_KEY)
        return {
            "binding": self.purpose("binding"),
            "paypal": self.purpose("paypal"),
            "providers": [
                {
                    "provider": provider,
                    "label": metadata["label"],
                    "configured": self.provider_configured(provider),
                }
                for provider, metadata in PROVIDER_METADATA.items()
            ],
            "countryOptions": [
                {
                    "code": code,
                    "label": label,
                    "binding": code in BINDING_SMS_COUNTRIES,
                    "paypal": code in PAYPAL_SMS_COUNTRIES,
                }
                for code, label in SMS_COUNTRY_CATALOG
            ],
            "updatedAt": str(state.get("updatedAt") or ""),
        }

    def configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("请求格式无效")
        api_keys = payload.get("apiKeys")
        if api_keys is not None and not isinstance(api_keys, dict):
            raise ValueError("接码密钥配置格式无效")
        for provider, api_key in dict(api_keys or {}).items():
            self.configure_provider_key(provider, api_key)
        current = self._setting(GLOBAL_SMS_ROUTING_SETTING_KEY)
        for purpose in SMS_PURPOSES:
            candidate = payload.get(purpose)
            if candidate is None:
                continue
            if not isinstance(candidate, dict):
                raise ValueError(f"{purpose} 接码配置格式无效")
            current[purpose] = {
                "provider": self._provider(candidate.get("provider")),
                "maxPrice": self._max_price(candidate.get("maxPrice")),
                "countries": self._countries(purpose, candidate.get("countries")),
            }
        current.update(version=1, updatedAt=_utc_now())
        self._save_setting(GLOBAL_SMS_ROUTING_SETTING_KEY, current)
        paypal = self.purpose("paypal")
        legacy = self._setting(PAYMENT_SMS_SETTING_KEY)
        legacy.update(defaultProvider=paypal["provider"], updatedAt=current["updatedAt"])
        self._save_setting(PAYMENT_SMS_SETTING_KEY, legacy)
        return self.public_state()


class PaymentSmsProviderResolver:
    """Resolve the one provider selected by the global purpose policy."""

    def __init__(
        self,
        db_file: Path,
        *,
        smsbower_configured: Callable[[], bool],
        routing_store: GlobalSmsRoutingConfigStore | None = None,
    ) -> None:
        self.db_file = Path(db_file)
        self._smsbower_configured = smsbower_configured
        self.routing_store = routing_store or GlobalSmsRoutingConfigStore(db_file)

    def preferred_provider(self) -> str:
        return str(self.routing_store.purpose("paypal")["provider"])

    def policy(self, purpose: str = "paypal") -> dict[str, Any]:
        return self.routing_store.purpose(purpose)

    def resolve(self, purpose: str = "paypal") -> ResolvedPaymentSmsProvider | None:
        policy = self.routing_store.purpose(purpose)
        selected = str(policy["provider"])
        configured = (
            bool(self._smsbower_configured())
            if selected == "smsbower"
            else self.routing_store.provider_configured(selected)
        )
        if not configured:
            return None
        return ResolvedPaymentSmsProvider(
            provider=selected,
            label=PROVIDER_METADATA[selected]["label"],
            virtual_us=selected == "smsbower",
        )


__all__ = [
    "AUTOMATIC_PAYMENT_SMS_PROVIDERS",
    "BINDING_SMS_COUNTRIES",
    "DEFAULT_SMS_ROUTING",
    "GLOBAL_SMS_ROUTING_SETTING_KEY",
    "GlobalSmsRoutingConfigStore",
    "HERO_SMS_SETTING_KEY",
    "PAYMENT_SMS_SETTING_KEY",
    "PAYPAL_SMS_COUNTRIES",
    "PROVIDER_METADATA",
    "PaymentSmsProviderResolver",
    "ResolvedPaymentSmsProvider",
    "SMSBOWER_SETTING_KEY",
    "SMS_COUNTRY_CATALOG",
    "SMS_PURPOSES",
]
