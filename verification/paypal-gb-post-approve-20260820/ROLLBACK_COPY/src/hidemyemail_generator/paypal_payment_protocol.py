"""MVP/Strategy selector for the regional PayPal payment protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


PAYPAL_PAYMENT_PROTOCOL_CURRENT = "current"
PAYPAL_PAYMENT_PROTOCOL_PAY153_LEGACY_US = "pay153_legacy_us"


@dataclass(frozen=True, slots=True)
class PayPalPaymentProtocolModel:
    """Protocol fields presented to the PAY.153 service."""

    link_method: str
    payment_protocol: str
    buyer_mode: str = "identity_elevation"


class PayPalPaymentProtocolStrategy(Protocol):
    """Strategy used by the presenter to select one payment protocol."""

    def supports(self, link_method: str) -> bool: ...

    def build(self, link_method: str) -> PayPalPaymentProtocolModel: ...


class LegacyPay153UsStrategy:
    """Restore the combined-signup PAY.153 protocol for US links."""

    def supports(self, link_method: str) -> bool:
        return link_method == "paypal_us"

    def build(self, link_method: str) -> PayPalPaymentProtocolModel:
        return PayPalPaymentProtocolModel(
            link_method=link_method,
            payment_protocol=PAYPAL_PAYMENT_PROTOCOL_PAY153_LEGACY_US,
        )


class CurrentPayPalPaymentStrategy:
    """Keep the current protocol for every non-US PayPal link."""

    def supports(self, link_method: str) -> bool:
        return True

    def build(self, link_method: str) -> PayPalPaymentProtocolModel:
        return PayPalPaymentProtocolModel(
            link_method=link_method,
            payment_protocol=PAYPAL_PAYMENT_PROTOCOL_CURRENT,
        )


class PayPalPaymentProtocolPresenter:
    """Presenter that exposes the selected strategy to the WebApp view."""

    def __init__(
        self,
        strategies: tuple[PayPalPaymentProtocolStrategy, ...] | None = None,
    ) -> None:
        self._strategies = strategies or (
            LegacyPay153UsStrategy(),
            CurrentPayPalPaymentStrategy(),
        )

    def present(self, link_method: str) -> PayPalPaymentProtocolModel:
        normalized_method = str(link_method or "").strip().lower()
        for strategy in self._strategies:
            if strategy.supports(normalized_method):
                return strategy.build(normalized_method)
        raise ValueError("PayPal 支付协议无法匹配")


PAYPAL_PAYMENT_PROTOCOL_PRESENTER = PayPalPaymentProtocolPresenter()


__all__ = [
    "PAYPAL_PAYMENT_PROTOCOL_CURRENT",
    "PAYPAL_PAYMENT_PROTOCOL_PAY153_LEGACY_US",
    "PAYPAL_PAYMENT_PROTOCOL_PRESENTER",
    "PayPalPaymentProtocolModel",
    "PayPalPaymentProtocolPresenter",
]
