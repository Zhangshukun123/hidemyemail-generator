"""Pure in-memory MVP/Strategy state machine for the US email-first flow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


EVENT_CREATE_EMAIL_ACCOUNT = "create_email_account"
EVENT_REQUEST_PHONE_OTP = "request_phone_otp"
EVENT_CONFIRM_PHONE_OTP = "confirm_phone_otp"
EVENT_HYDRATE_AUTHENTICATED_BUYER = "hydrate_authenticated_buyer"
EVENT_BIND_CARD = "bind_card"
EVENT_AUTHORIZE = "authorize"

PREPARATION_EVENT_ORDER = (
    EVENT_CREATE_EMAIL_ACCOUNT,
    EVENT_REQUEST_PHONE_OTP,
    EVENT_CONFIRM_PHONE_OTP,
    EVENT_HYDRATE_AUTHENTICATED_BUYER,
    EVENT_BIND_CARD,
)
FLOW_EVENT_ORDER = (*PREPARATION_EVENT_ORDER, EVENT_AUTHORIZE)

STAGE_IDLE = "idle"
STAGE_AUTHORIZATION_READY = "authorization_ready"
STAGE_AUTHORIZED = "authorized"

US_EMAIL_NOT_SUBMITTED = "US_EMAIL_NOT_SUBMITTED"
US_PHONE_UNAVAILABLE = "US_PHONE_UNAVAILABLE"
US_PHONE_NOT_VERIFIED = "US_PHONE_NOT_VERIFIED"
US_BUYER_USER_ID_MISSING = "US_BUYER_USER_ID_MISSING"
US_EUAT_TOKEN_MISSING = "US_EUAT_TOKEN_MISSING"
US_FUNDING_INSTRUMENT_MISSING = "US_FUNDING_INSTRUMENT_MISSING"
US_AUTHENTICATION_RESULT_INVALID = "US_AUTHENTICATION_RESULT_INVALID"
US_FLOW_EVENT_ORDER_INVALID = "US_FLOW_EVENT_ORDER_INVALID"
US_AUTHORIZATION_NOT_READY = "US_AUTHORIZATION_NOT_READY"


@dataclass
class UsEmailFirstModel:
    """Model: durable facts and completed events for one US onboarding run."""

    email_submitted: bool = False
    phone: str = ""
    phone_verified: bool = False
    buyer_user_id: str = ""
    euat_token: str = ""
    funding_instrument_id: str = ""
    stage: str = STAGE_IDLE
    events: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MemberAuthenticationResult:
    """Authenticated-member facts returned by the View adapter."""

    phone_verified: bool
    user_id: str
    euat_token: str


class UsEmailFirstView(Protocol):
    """View: side-effect adapter implemented by the active transport/UI."""

    def acquire_phone_after_email(self) -> str: ...

    def authenticate_member_with_phone(
        self, signup_url: str
    ) -> MemberAuthenticationResult: ...

    def bind_card_in_authenticated_session(self, review_url: str) -> str: ...


class UsEmailFirstFlowError(RuntimeError):
    """Stable, machine-readable failure raised at a gated flow stage."""

    def __init__(self, code: str, stage: str) -> None:
        self.code = code
        self.stage = stage
        super().__init__(f"{code}: stage={stage}")


class UsEmailFirstStrategy(Protocol):
    """Strategy selected by the Presenter for a particular US flow version."""

    def prepare(
        self,
        model: UsEmailFirstModel,
        view: UsEmailFirstView,
        *,
        signup_url: str,
        review_url: str,
    ) -> UsEmailFirstModel: ...

    def mark_authorized(self, model: UsEmailFirstModel) -> UsEmailFirstModel: ...


class EmailFirstAuthorizationStrategy:
    """Prepare an authenticated US buyer without repeating completed steps."""

    @staticmethod
    def _fail(model: UsEmailFirstModel, code: str, stage: str) -> None:
        model.stage = stage
        raise UsEmailFirstFlowError(code, stage)

    def _validate_event_prefix(self, model: UsEmailFirstModel) -> None:
        events = list(model.events)
        if len(events) > len(FLOW_EVENT_ORDER) or events != list(
            FLOW_EVENT_ORDER[: len(events)]
        ):
            self._fail(
                model,
                US_FLOW_EVENT_ORDER_INVALID,
                str(events[-1] if events else STAGE_IDLE),
            )

    def _record(self, model: UsEmailFirstModel, event: str) -> None:
        self._validate_event_prefix(model)
        event_index = FLOW_EVENT_ORDER.index(event)
        if len(model.events) > event_index:
            return
        if len(model.events) != event_index:
            self._fail(model, US_FLOW_EVENT_ORDER_INVALID, event)
        model.events.append(event)

    @staticmethod
    def _authentication_ready(model: UsEmailFirstModel) -> bool:
        return bool(
            model.phone_verified
            and model.buyer_user_id.strip()
            and model.euat_token.strip()
        )

    def prepare(
        self,
        model: UsEmailFirstModel,
        view: UsEmailFirstView,
        *,
        signup_url: str,
        review_url: str,
    ) -> UsEmailFirstModel:
        self._validate_event_prefix(model)
        if model.events == list(FLOW_EVENT_ORDER):
            return model

        model.stage = EVENT_CREATE_EMAIL_ACCOUNT
        if not model.email_submitted:
            self._fail(model, US_EMAIL_NOT_SUBMITTED, EVENT_CREATE_EMAIL_ACCOUNT)
        self._record(model, EVENT_CREATE_EMAIL_ACCOUNT)

        if not model.phone.strip():
            model.stage = EVENT_REQUEST_PHONE_OTP
            model.phone = str(view.acquire_phone_after_email() or "").strip()
            if not model.phone:
                self._fail(model, US_PHONE_UNAVAILABLE, EVENT_REQUEST_PHONE_OTP)
        self._record(model, EVENT_REQUEST_PHONE_OTP)

        if not self._authentication_ready(model):
            model.stage = EVENT_CONFIRM_PHONE_OTP
            authentication = view.authenticate_member_with_phone(signup_url)
            if not isinstance(authentication, MemberAuthenticationResult):
                self._fail(
                    model,
                    US_AUTHENTICATION_RESULT_INVALID,
                    EVENT_CONFIRM_PHONE_OTP,
                )
            model.phone_verified = bool(authentication.phone_verified)
            model.buyer_user_id = str(authentication.user_id or "").strip()
            model.euat_token = str(authentication.euat_token or "").strip()

        if not model.phone_verified:
            self._fail(model, US_PHONE_NOT_VERIFIED, EVENT_CONFIRM_PHONE_OTP)
        self._record(model, EVENT_CONFIRM_PHONE_OTP)

        model.stage = EVENT_HYDRATE_AUTHENTICATED_BUYER
        if not model.buyer_user_id:
            self._fail(
                model,
                US_BUYER_USER_ID_MISSING,
                EVENT_HYDRATE_AUTHENTICATED_BUYER,
            )
        if not model.euat_token:
            self._fail(
                model,
                US_EUAT_TOKEN_MISSING,
                EVENT_HYDRATE_AUTHENTICATED_BUYER,
            )
        self._record(model, EVENT_HYDRATE_AUTHENTICATED_BUYER)

        if not model.funding_instrument_id.strip():
            model.stage = EVENT_BIND_CARD
            model.funding_instrument_id = str(
                view.bind_card_in_authenticated_session(review_url) or ""
            ).strip()
            if not model.funding_instrument_id:
                self._fail(
                    model,
                    US_FUNDING_INSTRUMENT_MISSING,
                    EVENT_BIND_CARD,
                )
        self._record(model, EVENT_BIND_CARD)
        model.stage = STAGE_AUTHORIZATION_READY
        return model

    def mark_authorized(self, model: UsEmailFirstModel) -> UsEmailFirstModel:
        self._validate_event_prefix(model)
        prepared = bool(
            model.email_submitted
            and model.phone.strip()
            and self._authentication_ready(model)
            and model.funding_instrument_id.strip()
            and model.events[: len(PREPARATION_EVENT_ORDER)]
            == list(PREPARATION_EVENT_ORDER)
        )
        if not prepared:
            self._fail(model, US_AUTHORIZATION_NOT_READY, EVENT_AUTHORIZE)
        self._record(model, EVENT_AUTHORIZE)
        model.stage = STAGE_AUTHORIZED
        return model


class UsEmailFirstPresenter:
    """Presenter: expose the state machine while keeping effects in the View."""

    def __init__(
        self,
        model: UsEmailFirstModel,
        view: UsEmailFirstView,
        strategy: UsEmailFirstStrategy | None = None,
    ) -> None:
        self.model = model
        self.view = view
        self.strategy = strategy or EmailFirstAuthorizationStrategy()

    def prepare_authorization(
        self, *, signup_url: str, review_url: str
    ) -> UsEmailFirstModel:
        return self.strategy.prepare(
            self.model,
            self.view,
            signup_url=signup_url,
            review_url=review_url,
        )

    def mark_authorized(self) -> UsEmailFirstModel:
        return self.strategy.mark_authorized(self.model)


__all__ = [
    "EVENT_AUTHORIZE",
    "EVENT_BIND_CARD",
    "EVENT_CONFIRM_PHONE_OTP",
    "EVENT_CREATE_EMAIL_ACCOUNT",
    "EVENT_HYDRATE_AUTHENTICATED_BUYER",
    "EVENT_REQUEST_PHONE_OTP",
    "EmailFirstAuthorizationStrategy",
    "FLOW_EVENT_ORDER",
    "MemberAuthenticationResult",
    "PREPARATION_EVENT_ORDER",
    "STAGE_AUTHORIZATION_READY",
    "STAGE_AUTHORIZED",
    "US_AUTHENTICATION_RESULT_INVALID",
    "US_AUTHORIZATION_NOT_READY",
    "US_BUYER_USER_ID_MISSING",
    "US_EMAIL_NOT_SUBMITTED",
    "US_EUAT_TOKEN_MISSING",
    "US_FLOW_EVENT_ORDER_INVALID",
    "US_FUNDING_INSTRUMENT_MISSING",
    "US_PHONE_NOT_VERIFIED",
    "US_PHONE_UNAVAILABLE",
    "UsEmailFirstFlowError",
    "UsEmailFirstModel",
    "UsEmailFirstPresenter",
    "UsEmailFirstStrategy",
    "UsEmailFirstView",
]
