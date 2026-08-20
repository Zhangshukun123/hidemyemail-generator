"""HeroSMS strategy for PayPal phone activations.

HeroSMS exposes the SMS-Activate-compatible endpoint documented at
https://hero-sms.com/cn/api.  The shared client template keeps lifecycle and
error handling identical to SMSBower while this module supplies HeroSMS's
endpoint, API key, country IDs, and provider label.
"""
from __future__ import annotations

from paypal.sms_config import SmsSettingsModel
from paypal.smsbower import (
    DEFAULT_MAX_PRICE,
    DEFAULT_OTP_TIMEOUT_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    ApiKeyResolver,
    Requester,
    SMSBowerPhoneActivation,
    SMSBowerPhoneCancelled,
    SMSBowerPhoneClient,
    SMSBowerPhoneError,
    SmsActivateProviderSpec,
)


HERO_SMS_API_URL = "https://hero-sms.com/stubs/handler_api.php"
HERO_SMS_API_DOCS_URL = "https://hero-sms.com/cn/api"
HERO_SMS_PAYPAL_SERVICE = "paypal"
HERO_SMS_PAYPAL_SERVICE_CODE = "ts"

# Verified against HeroSMS's public getCountries endpoint.  HeroSMS uses Japan
# 182 and does not expose SMSBower's provider-specific virtual-US route 12.
HERO_SMS_COUNTRY_IDS: dict[str, int] = {
    "BR": 73,
    "DE": 43,
    "GB": 16,
    "US": 187,
    "JP": 182,
    "TH": 52,
    "ID": 6,
    "PH": 4,
    "TW": 55,
    "MX": 54,
    "AE": 95,
    "AU": 175,
    "CA": 36,
}

HERO_SMS_PROVIDER_SPEC = SmsActivateProviderSpec(
    provider_id="hero-sms",
    label="HeroSMS",
    api_url=HERO_SMS_API_URL,
    docs_url=HERO_SMS_API_DOCS_URL,
    country_ids=HERO_SMS_COUNTRY_IDS,
    country_purchase_ids={},
    service=HERO_SMS_PAYPAL_SERVICE,
    service_code=HERO_SMS_PAYPAL_SERVICE_CODE,
    # HeroSMS documents lifecycle transitions 3, 6, and 8.  Polling starts
    # immediately after PayPal accepts its send request, so status 1 is omitted.
    mark_sent_status=None,
)


def resolve_api_key() -> str:
    return SmsSettingsModel().api_key("hero-sms")


class HeroSmsPhoneClient(SMSBowerPhoneClient):
    """Concrete Provider Strategy backed by HeroSMS."""

    def __init__(
        self,
        *,
        api_url: str = HERO_SMS_API_URL,
        api_key: str = "",
        api_key_resolver: ApiKeyResolver | None = None,
        requester: Requester | None = None,
        timeout_seconds: float = 20.0,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        otp_timeout_seconds: float = DEFAULT_OTP_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            api_url=api_url,
            api_key=api_key,
            api_key_resolver=api_key_resolver or resolve_api_key,
            requester=requester,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            otp_timeout_seconds=otp_timeout_seconds,
            provider_spec=HERO_SMS_PROVIDER_SPEC,
        )


HeroSmsPhoneActivation = SMSBowerPhoneActivation
HeroSmsPhoneCancelled = SMSBowerPhoneCancelled
HeroSmsPhoneError = SMSBowerPhoneError


__all__ = [
    "DEFAULT_MAX_PRICE",
    "HERO_SMS_API_DOCS_URL",
    "HERO_SMS_API_URL",
    "HERO_SMS_COUNTRY_IDS",
    "HERO_SMS_PAYPAL_SERVICE",
    "HERO_SMS_PAYPAL_SERVICE_CODE",
    "HERO_SMS_PROVIDER_SPEC",
    "HeroSmsPhoneActivation",
    "HeroSmsPhoneCancelled",
    "HeroSmsPhoneClient",
    "HeroSmsPhoneError",
    "resolve_api_key",
]
