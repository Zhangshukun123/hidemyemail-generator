import pytest

from paypal.us_email_first import (
    EVENT_BIND_CARD,
    EVENT_CONFIRM_PHONE_OTP,
    EVENT_CREATE_EMAIL_ACCOUNT,
    EVENT_HYDRATE_AUTHENTICATED_BUYER,
    EVENT_REQUEST_PHONE_OTP,
    FLOW_EVENT_ORDER,
    MemberAuthenticationResult,
    PREPARATION_EVENT_ORDER,
    STAGE_AUTHORIZATION_READY,
    STAGE_AUTHORIZED,
    US_AUTHORIZATION_NOT_READY,
    US_BUYER_USER_ID_MISSING,
    US_EMAIL_NOT_SUBMITTED,
    US_EUAT_TOKEN_MISSING,
    US_FUNDING_INSTRUMENT_MISSING,
    US_PHONE_NOT_VERIFIED,
    UsEmailFirstFlowError,
    UsEmailFirstModel,
    UsEmailFirstPresenter,
)


SIGNUP_URL = "https://www.paypal.com/checkoutweb/signup?token=EC-fixture"
REVIEW_URL = "https://www.paypal.com/webapps/hermes#/billingweb/review"


class RecordingView:
    def __init__(
        self,
        *,
        authentication: MemberAuthenticationResult | None = None,
        funding_results: list[str] | None = None,
    ) -> None:
        self.authentication = authentication or MemberAuthenticationResult(
            phone_verified=True,
            user_id="fixture-user",
            euat_token="fixture-euat",
        )
        self.funding_results = list(funding_results or ["CARD-fixture"])
        self.calls: list[tuple[str, str]] = []

    def acquire_phone_after_email(self) -> str:
        self.calls.append(("acquire_phone_after_email", ""))
        return "+12025550123"

    def authenticate_member_with_phone(
        self, signup_url: str
    ) -> MemberAuthenticationResult:
        self.calls.append(("authenticate_member_with_phone", signup_url))
        return self.authentication

    def bind_card_in_authenticated_session(self, review_url: str) -> str:
        self.calls.append(("bind_card_in_authenticated_session", review_url))
        return self.funding_results.pop(0) if self.funding_results else ""


def presenter_for(
    *,
    email_submitted: bool = True,
    authentication: MemberAuthenticationResult | None = None,
    funding_results: list[str] | None = None,
) -> tuple[UsEmailFirstPresenter, RecordingView]:
    view = RecordingView(
        authentication=authentication,
        funding_results=funding_results,
    )
    presenter = UsEmailFirstPresenter(
        UsEmailFirstModel(email_submitted=email_submitted),
        view,
    )
    return presenter, view


def test_us_flow_records_email_phone_login_card_authorize_in_order():
    presenter, view = presenter_for()

    prepared = presenter.prepare_authorization(
        signup_url=SIGNUP_URL,
        review_url=REVIEW_URL,
    )

    assert prepared.events == list(PREPARATION_EVENT_ORDER)
    assert prepared.stage == STAGE_AUTHORIZATION_READY
    assert prepared.phone == "+12025550123"
    assert prepared.phone_verified is True
    assert prepared.buyer_user_id == "fixture-user"
    assert prepared.euat_token == "fixture-euat"
    assert prepared.funding_instrument_id == "CARD-fixture"
    assert view.calls == [
        ("acquire_phone_after_email", ""),
        ("authenticate_member_with_phone", SIGNUP_URL),
        ("bind_card_in_authenticated_session", REVIEW_URL),
    ]

    authorized = presenter.mark_authorized()

    assert authorized.events == list(FLOW_EVENT_ORDER)
    assert authorized.stage == STAGE_AUTHORIZED


def test_missing_email_stops_before_phone_or_card_side_effects():
    presenter, view = presenter_for(email_submitted=False)

    with pytest.raises(UsEmailFirstFlowError) as captured:
        presenter.prepare_authorization(
            signup_url=SIGNUP_URL,
            review_url=REVIEW_URL,
        )

    assert captured.value.code == US_EMAIL_NOT_SUBMITTED
    assert captured.value.stage == EVENT_CREATE_EMAIL_ACCOUNT
    assert presenter.model.events == []
    assert view.calls == []


@pytest.mark.parametrize(
    ("authentication", "error_code", "expected_events"),
    [
        (
            MemberAuthenticationResult(False, "fixture-user", "fixture-euat"),
            US_PHONE_NOT_VERIFIED,
            [EVENT_CREATE_EMAIL_ACCOUNT, EVENT_REQUEST_PHONE_OTP],
        ),
        (
            MemberAuthenticationResult(True, "", "fixture-euat"),
            US_BUYER_USER_ID_MISSING,
            [
                EVENT_CREATE_EMAIL_ACCOUNT,
                EVENT_REQUEST_PHONE_OTP,
                EVENT_CONFIRM_PHONE_OTP,
            ],
        ),
        (
            MemberAuthenticationResult(True, "fixture-user", ""),
            US_EUAT_TOKEN_MISSING,
            [
                EVENT_CREATE_EMAIL_ACCOUNT,
                EVENT_REQUEST_PHONE_OTP,
                EVENT_CONFIRM_PHONE_OTP,
            ],
        ),
    ],
)
def test_authentication_gates_block_card_binding(
    authentication, error_code, expected_events
):
    presenter, view = presenter_for(authentication=authentication)

    with pytest.raises(UsEmailFirstFlowError) as captured:
        presenter.prepare_authorization(
            signup_url=SIGNUP_URL,
            review_url=REVIEW_URL,
        )

    assert captured.value.code == error_code
    assert presenter.model.events == expected_events
    assert not any(call[0] == "bind_card_in_authenticated_session" for call in view.calls)


def test_missing_funding_blocks_authorization():
    presenter, view = presenter_for(funding_results=[""])

    with pytest.raises(UsEmailFirstFlowError) as captured:
        presenter.prepare_authorization(
            signup_url=SIGNUP_URL,
            review_url=REVIEW_URL,
        )

    assert captured.value.code == US_FUNDING_INSTRUMENT_MISSING
    assert captured.value.stage == EVENT_BIND_CARD
    assert presenter.model.events == [
        EVENT_CREATE_EMAIL_ACCOUNT,
        EVENT_REQUEST_PHONE_OTP,
        EVENT_CONFIRM_PHONE_OTP,
        EVENT_HYDRATE_AUTHENTICATED_BUYER,
    ]
    with pytest.raises(UsEmailFirstFlowError) as authorize_error:
        presenter.mark_authorized()
    assert authorize_error.value.code == US_AUTHORIZATION_NOT_READY
    assert [call[0] for call in view.calls].count(
        "bind_card_in_authenticated_session"
    ) == 1


def test_card_retry_does_not_repeat_email_account_or_phone_otp():
    presenter, view = presenter_for(funding_results=["", "CARD-retry"])

    with pytest.raises(UsEmailFirstFlowError) as first_attempt:
        presenter.prepare_authorization(
            signup_url=SIGNUP_URL,
            review_url=REVIEW_URL,
        )
    assert first_attempt.value.code == US_FUNDING_INSTRUMENT_MISSING

    prepared = presenter.prepare_authorization(
        signup_url=SIGNUP_URL,
        review_url=REVIEW_URL,
    )

    call_names = [call[0] for call in view.calls]
    assert call_names.count("acquire_phone_after_email") == 1
    assert call_names.count("authenticate_member_with_phone") == 1
    assert call_names.count("bind_card_in_authenticated_session") == 2
    assert prepared.funding_instrument_id == "CARD-retry"
    assert prepared.events == list(PREPARATION_EVENT_ORDER)
    assert len(prepared.events) == len(set(prepared.events))


def test_mark_authorized_is_idempotent_after_success():
    presenter, _view = presenter_for()
    presenter.prepare_authorization(signup_url=SIGNUP_URL, review_url=REVIEW_URL)

    presenter.mark_authorized()
    presenter.mark_authorized()

    assert presenter.model.events == list(FLOW_EVENT_ORDER)
    assert presenter.model.stage == STAGE_AUTHORIZED
