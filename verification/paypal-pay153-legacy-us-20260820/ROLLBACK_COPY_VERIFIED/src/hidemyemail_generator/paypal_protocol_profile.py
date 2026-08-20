"""Immutable strategies for regional two-proxy PayPal extraction flows."""

from __future__ import annotations

from dataclasses import dataclass

from ._card_link_payment_modes import (
    PAYPAL_GB_TWO_PROXY_FLOW,
    PAYPAL_PAY153_PROTOCOL_FLOW,
)


@dataclass(frozen=True, slots=True)
class PayPalProtocolProfile:
    """Country-specific policy consumed by the shared protocol pipeline."""

    country: str
    currency: str
    request_locale: str
    payment_locale: str
    browser_timezone: str
    standard_amounts: tuple[str, ...]
    flow_name: str
    flow_label: str
    error_prefix: str
    promotion_strategy: str
    promotion_proof: str
    billing_city_hint: str = ""
    billing_state_hint: str = ""

    @property
    def billing_location_kwargs(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "city_hint": self.billing_city_hint,
                "state_hint": self.billing_state_hint,
            }.items()
            if value
        }


PAYPAL_US_PROTOCOL_PROFILE = PayPalProtocolProfile(
    country="US",
    currency="USD",
    request_locale="en-US",
    payment_locale="en",
    browser_timezone="America/New_York",
    standard_amounts=("2000",),
    flow_name=PAYPAL_PAY153_PROTOCOL_FLOW,
    flow_label="PayPal US/PAY153",
    error_prefix="PAY153_PROTOCOL",
    promotion_strategy="checkout_check_then_us_update",
    promotion_proof=(
        "pool1_checkout_check_pool2_us_update_then_stripe_validation"
    ),
    billing_city_hint="New York",
    billing_state_hint="NY",
)


PAYPAL_GB_PROTOCOL_PROFILE = PayPalProtocolProfile(
    country="GB",
    currency="GBP",
    request_locale="en-GB",
    payment_locale="en",
    browser_timezone="Europe/London",
    # Hosted and OAICS payloads may expose pre-tax, tax-inclusive, or
    # USD-parity snapshots before the mandatory GB Update/Taxes sequence.
    standard_amounts=("1667", "1917", "2000"),
    flow_name=PAYPAL_GB_TWO_PROXY_FLOW,
    flow_label="PayPal GB/GBP",
    error_prefix="PAYPAL_GB_PROTOCOL",
    promotion_strategy="checkout_check_then_gb_update",
    promotion_proof=(
        "pool1_checkout_check_pool2_gb_update_then_stripe_validation"
    ),
)
