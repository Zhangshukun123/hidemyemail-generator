"""MVP/Strategy compatibility layer for PayPal onboarding changes.

The protocol flow owns transport and state.  This module only classifies a
PayPal response (Model), selects a recovery strategy (Presenter), and invokes
the adapter supplied by the active UI (View).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


US_MEMBER_RISK_REVIEW = "US_MEMBER_RISK_REVIEW"


@dataclass(frozen=True)
class OnboardingFailureModel:
    """Normalized facts from one failed SignUpNewMember response."""

    country: str
    messages: frozenset[str]
    checkpoints: frozenset[str]

    @classmethod
    def from_errors(
        cls, country: str, errors: list[dict]
    ) -> "OnboardingFailureModel":
        messages: set[str] = set()
        checkpoints: set[str] = set()
        for error in errors or []:
            if not isinstance(error, dict):
                continue
            message = str(error.get("message") or error.get("_name") or "")
            if message:
                messages.add(message.upper())
            checkpoints.update(
                str(item)
                for item in (error.get("checkpoints") or [])
                if str(item)
            )
        return cls(
            country=str(country or "").upper(),
            messages=frozenset(messages),
            checkpoints=frozenset(checkpoints),
        )


@dataclass(frozen=True)
class OnboardingRecoveryDecision:
    """Presenter result consumed by the protocol orchestrator."""

    matched: bool
    recovered: bool = False
    code: str = ""
    message: str = ""


class OnboardingCompatibilityView(Protocol):
    """UI adapter capable of completing PayPal's official member handoff."""

    def complete_member_onboarding(self, signup_url: str) -> bool: ...


class OnboardingRecoveryStrategy(Protocol):
    def matches(self, model: OnboardingFailureModel) -> bool: ...

    def recover(
        self,
        model: OnboardingFailureModel,
        signup_url: str,
        view: OnboardingCompatibilityView,
    ) -> OnboardingRecoveryDecision: ...


class UsMemberRiskReviewStrategy:
    """Route current US createMemberAccount review to the official UI."""

    def matches(self, model: OnboardingFailureModel) -> bool:
        return bool(
            model.country == "US"
            and "OAS_ERROR" in model.messages
            and "createMemberAccount" in model.checkpoints
        )

    def recover(
        self,
        model: OnboardingFailureModel,
        signup_url: str,
        view: OnboardingCompatibilityView,
    ) -> OnboardingRecoveryDecision:
        recovered = bool(view.complete_member_onboarding(signup_url))
        return OnboardingRecoveryDecision(
            matched=True,
            recovered=recovered,
            code=US_MEMBER_RISK_REVIEW,
            message=(
                "US PayPal member onboarding requires completion in the official "
                "PayPal page before protocol authorization can continue."
            ),
        )


class OnboardingCompatibilityPresenter:
    """Choose the first matching recovery strategy."""

    def __init__(
        self,
        strategies: tuple[OnboardingRecoveryStrategy, ...] | None = None,
    ) -> None:
        self.strategies = strategies or (UsMemberRiskReviewStrategy(),)

    def recover(
        self,
        *,
        country: str,
        errors: list[dict],
        signup_url: str,
        view: OnboardingCompatibilityView,
    ) -> OnboardingRecoveryDecision:
        model = OnboardingFailureModel.from_errors(country, errors)
        for strategy in self.strategies:
            if strategy.matches(model):
                return strategy.recover(model, signup_url, view)
        return OnboardingRecoveryDecision(matched=False)


__all__ = [
    "OnboardingCompatibilityPresenter",
    "OnboardingCompatibilityView",
    "OnboardingFailureModel",
    "OnboardingRecoveryDecision",
    "US_MEMBER_RISK_REVIEW",
    "UsMemberRiskReviewStrategy",
]
