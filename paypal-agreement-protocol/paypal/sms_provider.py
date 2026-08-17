"""Provider Strategy contract and registry for automatic PayPal SMS."""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from paypal.smsbower import SMSBowerPhoneActivation


@runtime_checkable
class PhoneProviderStrategy(Protocol):
    provider_id: str
    provider_label: str
    otp_timeout_seconds: float

    def configured(self) -> bool: ...

    def public_status(self, *, country: str, probe: bool = True) -> dict[str, Any]: ...

    def acquire_phone(
        self, country: str, *, max_price: Any
    ) -> SMSBowerPhoneActivation: ...

    def mark_sent(self, activation: SMSBowerPhoneActivation) -> None: ...

    def request_another(self, activation: SMSBowerPhoneActivation) -> None: ...

    def wait_for_code(
        self,
        activation: SMSBowerPhoneActivation,
        *,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
    ) -> str: ...

    def complete(self, activation: SMSBowerPhoneActivation) -> None: ...

    def cancel(self, activation: SMSBowerPhoneActivation) -> None: ...


StrategyResolver = Callable[[], PhoneProviderStrategy]


class SmsProviderRegistry:
    """Registry pattern that resolves strategies lazily for easy substitution."""

    def __init__(self) -> None:
        self._resolvers: dict[str, StrategyResolver] = {}

    def register(self, provider: str, resolver: StrategyResolver) -> None:
        normalized = str(provider or "").strip().lower()
        if not normalized:
            raise ValueError("接码平台标识不能为空")
        self._resolvers[normalized] = resolver

    def resolve(self, provider: str) -> PhoneProviderStrategy:
        normalized = str(provider or "").strip().lower()
        resolver = self._resolvers.get(normalized)
        if resolver is None:
            raise ValueError("接码平台参数不正确")
        return resolver()

    def __contains__(self, provider: object) -> bool:
        return str(provider or "").strip().lower() in self._resolvers

    def ids(self) -> tuple[str, ...]:
        return tuple(self._resolvers)


__all__ = ["PhoneProviderStrategy", "SmsProviderRegistry"]
