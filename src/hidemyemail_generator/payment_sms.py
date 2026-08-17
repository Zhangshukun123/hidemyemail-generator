"""Resolve the automatic SMS strategy used by one-click PayPal payments."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .inbox import connect_db


PAYMENT_SMS_SETTING_KEY = "paypal_sms_config_v1"
HERO_SMS_SETTING_KEY = "hero_sms_phone_config_v1"
AUTOMATIC_PAYMENT_SMS_PROVIDERS = ("smsbower", "hero-sms")


@dataclass(frozen=True)
class ResolvedPaymentSmsProvider:
    provider: str
    label: str
    virtual_us: bool


class PaymentSmsProviderResolver:
    """Strategy resolver backed by the payment SMS configuration model."""

    def __init__(
        self,
        db_file: Path,
        *,
        smsbower_configured: Callable[[], bool],
    ) -> None:
        self.db_file = Path(db_file)
        self._smsbower_configured = smsbower_configured

    def _setting(self, key: str) -> dict:
        connection = connect_db(str(self.db_file))
        try:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return {}
        try:
            value = row["value"] if hasattr(row, "keys") else row[0]
            payload = json.loads(str(value or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def preferred_provider(self) -> str:
        state = self._setting(PAYMENT_SMS_SETTING_KEY)
        return str(state.get("defaultProvider") or "smsbower").strip().lower()

    def _hero_sms_configured(self) -> bool:
        if str(os.getenv("HERO_SMS_API_KEY") or "").strip():
            return True
        return bool(str(self._setting(HERO_SMS_SETTING_KEY).get("apiKey") or "").strip())

    def resolve(self) -> ResolvedPaymentSmsProvider | None:
        configured = {
            "smsbower": bool(self._smsbower_configured()),
            "hero-sms": self._hero_sms_configured(),
        }
        preferred = self.preferred_provider()
        candidates = [
            *([preferred] if preferred in AUTOMATIC_PAYMENT_SMS_PROVIDERS else []),
            *(
                provider
                for provider in AUTOMATIC_PAYMENT_SMS_PROVIDERS
                if provider != preferred
            ),
        ]
        selected = next(
            (provider for provider in candidates if configured.get(provider)), ""
        )
        if selected == "smsbower":
            return ResolvedPaymentSmsProvider("smsbower", "SMSBower", True)
        if selected == "hero-sms":
            return ResolvedPaymentSmsProvider("hero-sms", "HeroSMS", False)
        return None


__all__ = [
    "AUTOMATIC_PAYMENT_SMS_PROVIDERS",
    "HERO_SMS_SETTING_KEY",
    "PAYMENT_SMS_SETTING_KEY",
    "PaymentSmsProviderResolver",
    "ResolvedPaymentSmsProvider",
]
