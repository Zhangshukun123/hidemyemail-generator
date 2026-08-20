"""Route successful PayPal agreements to the correct merchant completion path.

The model extracts trustworthy URL facts, strategies choose a completion route,
and the presenter renders that decision into the public result mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping, Protocol
from urllib.parse import parse_qs, urlparse


SUCCESS_REDIRECT_STATUSES = frozenset({"success", "succeeded"})
OPENAI_CHECKOUT_HOSTS = frozenset({"pay.openai.com", "chatgpt.com", "chat.openai.com"})
RESULT_URL_FIELDS = ("verification_url", "pending_url", "final_redirect_url")


def _normalized_url(value: Any) -> str:
    candidate = str(value or "").strip()
    if (
        not candidate
        or "\\" in candidate
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        return ""
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return ""
    return candidate


def is_trusted_openai_checkout_url(value: Any) -> bool:
    """Apply one strict parser contract before a URL is exposed or trusted."""

    candidate = _normalized_url(value)
    if not candidate:
        return False
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").lower()
    if host == "pay.openai.com":
        return True
    return host in {"chatgpt.com", "chat.openai.com"} and path.startswith(
        "/checkout/verify"
    )


def is_strict_https_url(value: Any) -> bool:
    """Return whether Python and browser URL parsers share a safe HTTPS shape."""

    return bool(_normalized_url(value))


def _query_value(url: str, name: str) -> str:
    try:
        values = parse_qs(urlparse(url).query).get(name) or []
    except ValueError:
        return ""
    return str(values[0] if values else "").strip()


@dataclass(frozen=True)
class PaymentCompletionModel:
    """Immutable facts used by completion strategies."""

    urls: tuple[str, ...]
    redirect_status: str
    settlement_status: str
    plan_type: str
    target: str
    final_redirect_url: str

    @classmethod
    def from_result(
        cls, result: Mapping[str, Any], *, target: str = "auto"
    ) -> "PaymentCompletionModel":
        urls: list[str] = []
        for field in RESULT_URL_FIELDS:
            candidate = _normalized_url(result.get(field))
            if candidate and candidate not in urls:
                urls.append(candidate)
        # pay.openai.com commonly carries the ChatGPT verification URL as an
        # encoded query value. Include it as a separate, validated fact.
        for candidate in tuple(urls):
            if (urlparse(candidate).hostname or "").lower() != "pay.openai.com":
                continue
            nested = _normalized_url(_query_value(candidate, "success_return_url"))
            if nested and nested not in urls:
                urls.append(nested)
        final_redirect_url = _normalized_url(result.get("final_redirect_url"))
        redirect_status = (
            _query_value(final_redirect_url, "redirect_status").lower()
            if is_trusted_openai_checkout_url(final_redirect_url)
            else ""
        )
        plan_type = next(
            (
                value.lower()
                for value in (_query_value(url, "plan_type") for url in urls)
                if value
            ),
            "",
        )
        return cls(
            urls=tuple(urls),
            redirect_status=redirect_status,
            settlement_status=str(result.get("settlement_status") or "").lower(),
            plan_type=plan_type,
            target=str(target or "auto").strip().lower(),
            final_redirect_url=final_redirect_url,
        )

    @property
    def is_openai_checkout(self) -> bool:
        # Only the actual terminal merchant URL is an automatic trust anchor.
        # verification_url/pending_url can be derived from query parameters and
        # therefore cannot establish provenance by themselves.
        return is_trusted_openai_checkout_url(self.final_redirect_url)


@dataclass(frozen=True)
class PaymentCompletionDecision:
    """Strategy output consumed by the payment runner."""

    route: str
    requires_braintree_bridge: bool
    updates: Mapping[str, Any]


class PaymentCompletionStrategy(Protocol):
    def matches(self, model: PaymentCompletionModel) -> bool: ...

    def decide(self, model: PaymentCompletionModel) -> PaymentCompletionDecision: ...


class OpenAICompletionStrategy:
    """Keep OpenAI/ChatGPT checkout confirmation independent of Grok."""

    def matches(self, model: PaymentCompletionModel) -> bool:
        if model.target == "grok_braintree":
            return False
        return model.target == "openai_plus" or model.is_openai_checkout

    def decide(self, model: PaymentCompletionModel) -> PaymentCompletionDecision:
        confirmed = model.redirect_status in SUCCESS_REDIRECT_STATUSES
        updates: dict[str, Any] = {
            "completion_provider": "openai",
            "braintree_bridge_status": "not_applicable",
            "openai_checkout_confirmed": confirmed,
            "redirect_status": model.redirect_status,
        }
        if confirmed:
            updates.update({"status": "success", "settlement_status": "confirmed"})
        elif model.settlement_status == "confirmed":
            updates["settlement_status"] = "authorization_only"
        return PaymentCompletionDecision(
            route="openai_checkout",
            requires_braintree_bridge=False,
            updates=updates,
        )


class GrokBraintreeCompletionStrategy:
    """Fallback strategy preserving registered Grok BA auto-detection."""

    def matches(self, model: PaymentCompletionModel) -> bool:
        return True

    def decide(self, model: PaymentCompletionModel) -> PaymentCompletionDecision:
        return PaymentCompletionDecision(
            route="grok_braintree_probe",
            requires_braintree_bridge=True,
            updates={},
        )


class PaymentResultView:
    """Mutable view over the flow result returned to WebJob."""

    def __init__(self, result: MutableMapping[str, Any]) -> None:
        self.result = result

    def render(self, decision: PaymentCompletionDecision) -> None:
        self.result.update(decision.updates)


class PaymentCompletionPresenter:
    """Select a strategy from model facts and render its decision."""

    def __init__(
        self, strategies: tuple[PaymentCompletionStrategy, ...] | None = None
    ) -> None:
        self.strategies = strategies or (
            OpenAICompletionStrategy(),
            GrokBraintreeCompletionStrategy(),
        )

    def present(
        self, result: MutableMapping[str, Any], *, target: str = "auto"
    ) -> PaymentCompletionDecision:
        model = PaymentCompletionModel.from_result(result, target=target)
        decision = next(
            strategy.decide(model)
            for strategy in self.strategies
            if strategy.matches(model)
        )
        PaymentResultView(result).render(decision)
        return decision


__all__ = [
    "PaymentCompletionDecision",
    "PaymentCompletionModel",
    "PaymentCompletionPresenter",
    "PaymentResultView",
    "is_strict_https_url",
    "is_trusted_openai_checkout_url",
]
