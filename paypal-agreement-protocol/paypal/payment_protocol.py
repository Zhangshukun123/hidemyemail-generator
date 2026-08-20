"""MVP/Strategy profiles for versioned PayPal payment behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


CURRENT_PAYMENT_PROTOCOL = "current"
PAY153_LEGACY_US_PAYMENT_PROTOCOL = "pay153_legacy_us"


@dataclass(frozen=True, slots=True)
class PaymentProtocolModel:
    """Normalized protocol decisions consumed by the flow view."""

    name: str
    country: str
    legacy_pay153_us: bool = False

    @property
    def uses_us_email_first(self) -> bool:
        return self.country == "US" and not self.legacy_pay153_us

    @property
    def acquires_phone_before_flow(self) -> bool:
        return not self.uses_us_email_first

    @property
    def uses_source_account_email(self) -> bool:
        return self.uses_us_email_first

    @property
    def allows_us_member_recovery(self) -> bool:
        return self.uses_us_email_first


class PaymentProtocolStrategy(Protocol):
    """Factory strategy for one named protocol."""

    def supports(self, requested: str) -> bool: ...

    def build(self, country: str) -> PaymentProtocolModel: ...


class CurrentPaymentProtocolStrategy:
    def supports(self, requested: str) -> bool:
        return requested == CURRENT_PAYMENT_PROTOCOL

    def build(self, country: str) -> PaymentProtocolModel:
        return PaymentProtocolModel(
            name=CURRENT_PAYMENT_PROTOCOL,
            country=country,
        )


class LegacyPay153UsPaymentProtocolStrategy:
    def supports(self, requested: str) -> bool:
        return requested == PAY153_LEGACY_US_PAYMENT_PROTOCOL

    def build(self, country: str) -> PaymentProtocolModel:
        if country != "US":
            raise ValueError("旧 PAY.153 支付协议仅支持 US")
        return PaymentProtocolModel(
            name=PAY153_LEGACY_US_PAYMENT_PROTOCOL,
            country=country,
            legacy_pay153_us=True,
        )


class PaymentProtocolPresenter:
    """Validate the API field and present one immutable flow profile."""

    def __init__(
        self,
        strategies: tuple[PaymentProtocolStrategy, ...] | None = None,
    ) -> None:
        self._strategies = strategies or (
            LegacyPay153UsPaymentProtocolStrategy(),
            CurrentPaymentProtocolStrategy(),
        )

    def present(self, requested: str, country: str) -> PaymentProtocolModel:
        normalized_name = str(requested or CURRENT_PAYMENT_PROTOCOL).strip().lower()
        normalized_country = str(country or "BR").strip().upper()
        for strategy in self._strategies:
            if strategy.supports(normalized_name):
                return strategy.build(normalized_country)
        raise ValueError("支付协议参数不正确")


PAYMENT_PROTOCOL_PRESENTER = PaymentProtocolPresenter()


__all__ = [
    "CURRENT_PAYMENT_PROTOCOL",
    "PAY153_LEGACY_US_PAYMENT_PROTOCOL",
    "PAYMENT_PROTOCOL_PRESENTER",
    "PaymentProtocolModel",
    "PaymentProtocolPresenter",
]
