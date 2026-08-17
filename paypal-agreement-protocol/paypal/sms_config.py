"""Persistent, provider-neutral SMS settings for the PayPal web flow.

The module is the Model/Presenter half of the small MVP used by the browser
settings dialog.  API keys remain in the local SQLite database (or process
environment) and are never included in presenter output.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


PAYMENT_SMS_SETTING_KEY = "paypal_sms_config_v1"
SMSBOWER_SETTING_KEY = "smsbower_mail_config_v1"
HERO_SMS_SETTING_KEY = "hero_sms_phone_config_v1"
AUTOMATIC_SMS_PROVIDERS = ("smsbower", "hero-sms")
SMS_PROVIDERS = ("manual", *AUTOMATIC_SMS_PROVIDERS)

PROVIDER_SETTINGS: dict[str, dict[str, str]] = {
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


def default_db_file() -> Path:
    configured = str(os.getenv("HME_DB_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "hidemyemail.db"


class SmsSettingsModel:
    """Model: store provider keys and the preferred automatic provider."""

    def __init__(self, db_file: Path | None = None) -> None:
        self.db_file = Path(db_file or default_db_file())

    def _read_setting(self, key: str) -> dict[str, Any]:
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
        except (OSError, sqlite3.Error):
            return {}
        if not row:
            return {}
        try:
            payload = json.loads(str(row[0] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_setting(self, key: str, payload: dict[str, Any]) -> None:
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.db_file))
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS settings "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (
                    key,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _provider(provider: str) -> str:
        normalized = str(provider or "").strip().lower()
        if normalized not in PROVIDER_SETTINGS:
            raise ValueError("接码平台参数不正确")
        return normalized

    def api_key(self, provider: str) -> str:
        normalized = self._provider(provider)
        metadata = PROVIDER_SETTINGS[normalized]
        explicit = str(os.getenv(metadata["env"]) or "").strip()
        if explicit:
            return explicit
        state = self._read_setting(metadata["setting"])
        return str(state.get("apiKey") or "").strip()

    def configured(self, provider: str) -> bool:
        return bool(self.api_key(provider))

    def save_api_key(self, provider: str, api_key: Any) -> None:
        normalized = self._provider(provider)
        value = str(api_key or "").strip()
        if value and not 8 <= len(value) <= 512:
            raise ValueError(f"{PROVIDER_SETTINGS[normalized]['label']} API Key 长度无效")
        setting_key = PROVIDER_SETTINGS[normalized]["setting"]
        state = self._read_setting(setting_key)
        state["apiKey"] = value
        state["updatedAt"] = _utc_now()
        self._write_setting(setting_key, state)

    def default_provider(self) -> str:
        state = self._read_setting(PAYMENT_SMS_SETTING_KEY)
        selected = str(state.get("defaultProvider") or "smsbower").strip().lower()
        return selected if selected in SMS_PROVIDERS else "smsbower"

    def save_default_provider(self, provider: str) -> None:
        normalized = str(provider or "").strip().lower()
        if normalized not in SMS_PROVIDERS:
            raise ValueError("默认接码平台参数不正确")
        state = self._read_setting(PAYMENT_SMS_SETTING_KEY)
        state.update({"defaultProvider": normalized, "updatedAt": _utc_now()})
        self._write_setting(PAYMENT_SMS_SETTING_KEY, state)


ClientResolver = Callable[[str], Any]


@dataclass
class SmsSettingsPresenter:
    """Presenter: validate UI commands and expose only sanitized provider state."""

    model: SmsSettingsModel
    client_resolver: ClientResolver
    timeout_seconds: int = 60
    max_phone_attempts: int = 3

    def present(self, *, country: str, probe: bool = True) -> dict[str, Any]:
        normalized_country = str(country or "BR").strip().upper()
        providers: list[dict[str, Any]] = []
        for provider in AUTOMATIC_SMS_PROVIDERS:
            client = self.client_resolver(provider)
            state = client.public_status(country=normalized_country, probe=probe)
            state["configured"] = self.model.configured(provider)
            state.pop("apiKey", None)
            providers.append(state)
        return {
            "defaultProvider": self.model.default_provider(),
            "timeoutSeconds": int(self.timeout_seconds),
            "maxPhoneAttempts": int(self.max_phone_attempts),
            "providers": providers,
        }

    def configure(self, payload: dict[str, Any], *, country: str) -> dict[str, Any]:
        provider = str(payload.get("provider") or "").strip().lower()
        default_provider = str(
            payload.get("defaultProvider") or provider or ""
        ).strip().lower()
        if "apiKey" in payload and payload.get("apiKey") is not None:
            self.model.save_api_key(provider, payload.get("apiKey"))
        if default_provider:
            self.model.save_default_provider(default_provider)
        return self.present(country=country, probe=False)


__all__ = [
    "AUTOMATIC_SMS_PROVIDERS",
    "HERO_SMS_SETTING_KEY",
    "PAYMENT_SMS_SETTING_KEY",
    "PROVIDER_SETTINGS",
    "SMSBOWER_SETTING_KEY",
    "SMS_PROVIDERS",
    "SmsSettingsModel",
    "SmsSettingsPresenter",
    "default_db_file",
]
