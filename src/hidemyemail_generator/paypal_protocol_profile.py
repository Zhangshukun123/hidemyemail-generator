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
    promotion_timing: str = "pre_confirm"
    checkout_includes_trial_promo: bool = True
    primary_proxy_finishes_checkout: bool = False
    requires_same_checkout_post_approve: bool = False
    browser_http_policy: str = "bound_when_supplied"
    elements_locale: str = ""
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
    promotion_timing="pre_confirm",
    checkout_includes_trial_promo=True,
    primary_proxy_finishes_checkout=False,
    requires_same_checkout_post_approve=False,
    browser_http_policy="bound_when_supplied",
    elements_locale="en",
    billing_city_hint="New York",
    billing_state_hint="NY",
)


PAYPAL_GB_PROTOCOL_PROFILE = PayPalProtocolProfile(
    country="GB",
    currency="GBP",
    request_locale="en-GB",
    payment_locale="en-GB",
    browser_timezone="Europe/London",
    # Standard Checkout may expose pre-tax or tax-inclusive snapshots before
    # approval.  Any positive GBP amount is accepted by the Presenter; these
    # values remain documented fixtures for regression tests.
    standard_amounts=("1667", "1917", "2000"),
    flow_name=PAYPAL_GB_TWO_PROXY_FLOW,
    flow_label="PayPal GB/GBP",
    error_prefix="PAYPAL_GB_PROTOCOL",
    promotion_strategy=(
        "standard_checkout_confirm_approve_then_same_checkout_update"
    ),
    promotion_proof=(
        "ba_approved_then_checkout_update_then_stripe_zero"
    ),
    promotion_timing="post_approve",
    checkout_includes_trial_promo=False,
    primary_proxy_finishes_checkout=True,
    requires_same_checkout_post_approve=True,
    browser_http_policy="bound_when_supplied",
    elements_locale="en",
)
