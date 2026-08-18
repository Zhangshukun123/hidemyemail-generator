from types import MethodType, SimpleNamespace

import pytest

import web as paypal_web
from paypal.elevation_flow import IdentityElevationPayPalFlow
from paypal.flow import PayPalFlow
from paypal.models import BillingAddress, CardInfo, SessionState, UserInfo
from paypal.us_email_first import (
    MemberAuthenticationResult,
    PREPARATION_EVENT_ORDER,
    UsEmailFirstModel,
    UsEmailFirstPresenter,
)


SIGNUP_URL = "https://www.paypal.com/checkoutweb/signup?token=EC-fixture"
REVIEW_URL = "https://www.paypal.com/webapps/hermes#/billingweb/review"


class ResponseStub:
    def __init__(self, *, status=200, text="", headers=None, url="https://www.paypal.com/pay"):
        self.status_code = status
        self.text = text
        self.headers = dict(headers or {})
        self.url = url


class StateSessionStub:
    def __init__(self, state: SessionState, graphql_result=None):
        self.state = state
        self.graphql_result = graphql_result
        self.graphql_calls = []
        self.get_calls = []

    def set_euat_token(self, value):
        self.state.euat_token = str(value or "")

    def graphql(self, operation, query, variables, **kwargs):
        self.graphql_calls.append((operation, variables, kwargs))
        return self.graphql_result

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        raise AssertionError("this integration fixture must not perform HTTP GET")


def test_us_email_server_action_posts_only_email_and_replaces_bootstrap_ec():
    pay_response = ResponseStub(status=200)
    email_response = ResponseStub(
        status=200,
        text='{"ecToken":"EC-email-first"}',
    )

    class ActionSession:
        def __init__(self):
            self.posts = []
            self.responses = [pay_response, email_response]

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return self.responses.pop(0)

    flow = PayPalFlow.__new__(PayPalFlow)
    flow.ba_token = "BA-fixture"
    flow.user = fixture_user(
        phone="+12025550123",
        email="email-first@example.com",
    )
    flow.state = SessionState(
        ba_token=flow.ba_token,
        ec_token="EC-bootstrap",
        ctx_id="ctx-fixture",
        show_create_account_action_id="a" * 32,
        submit_email_action_id="b" * 32,
    )
    flow.session = ActionSession()

    response = flow._submit_us_email_server_actions("https://www.paypal.com/pay")

    assert response is email_response
    assert flow.state.email_submitted is True
    assert flow.state.ec_token == "EC-email-first"
    assert len(flow.session.posts) == 2
    email_files = flow.session.posts[1][1]["files"]
    email_fields = {name: value[1] for name, value in email_files}
    assert email_fields["_1_login_email"] == "email-first@example.com"
    serialized_fields = repr(email_fields).lower()
    assert "phone" not in serialized_fields
    assert "card" not in serialized_fields


def test_us_email_action_evidence_rejects_failed_http_even_with_ec():
    evidence = PayPalFlow._email_action_evidence(
        ResponseStub(status=500, text='{"ecToken":"EC-rejected"}')
    )

    assert evidence["ec_token"] == "EC-rejected"
    assert evidence["verified"] is False


def fixture_user(phone="", email="generated@example.com", country="US"):
    country_code = "+1" if country == "US" else "+44"
    digits = "".join(character for character in phone if character.isdigit())
    calling_digits = country_code.lstrip("+")
    local = digits[len(calling_digits) :] if digits.startswith(calling_digits) else digits
    return UserInfo(
        first_name="Fixture",
        last_name="Buyer",
        email=email,
        phone=phone,
        phone_local=local,
        phone_country_code=country_code,
        password="Fixture-Password-1!",
        dob="01/01/1990",
        cpf="",
    )


def fixture_address(country="US"):
    return BillingAddress(
        street="Main Street",
        house_number="1",
        district="",
        city="New York" if country == "US" else "London",
        state="NY" if country == "US" else "",
        postal_code="10001" if country == "US" else "SW1A 1AA",
        country=country,
    )


def test_paypal_flow_us_phase3_uses_presenter_and_never_legacy_steps(monkeypatch):
    calls = []

    class WiredUsFlow(PayPalFlow):
        def __init__(self):
            self.country = "US"
            self.ba_token = "BA-fixture"
            self.user = fixture_user()
            self.address = fixture_address()
            self.state = SessionState(
                ba_token=self.ba_token,
                ec_token="EC-fixture",
                email_submitted=True,
                signup_url=SIGNUP_URL,
            )
            self.session = StateSessionStub(self.state)
            self.us_email_first_model = UsEmailFirstModel()
            self.us_email_first_presenter = UsEmailFirstPresenter(
                self.us_email_first_model, self
            )

        def acquire_phone_after_email(self):
            calls.append("acquire_phone_after_email")
            return "+12025550123"

        def authenticate_member_with_phone(self, signup_url):
            calls.append(("authenticate_member_with_phone", signup_url))
            return MemberAuthenticationResult(True, "buyer-US", "euat-US")

        def bind_card_in_authenticated_session(self, review_url):
            calls.append(("bind_card_in_authenticated_session", review_url))
            return "CARD-US"

        def _build_member_review_url(self):
            return REVIEW_URL

        def _confirm_phone_with_retry(self, *_args):
            pytest.fail("US email-first Phase3 must not call legacy phone confirmation")

        def _signup_with_card_retry(self, *_args):
            pytest.fail("US email-first Phase3 must not call legacy signup")

        def _us_onboarding_updated(self, model):
            calls.append(("published", tuple(model.events)))

    monkeypatch.setattr("paypal.flow.send_analytics_ts", lambda *_args, **_kwargs: None)
    flow = WiredUsFlow()

    flow._phase3_signup_and_2fa()

    assert isinstance(flow.us_email_first_presenter, UsEmailFirstPresenter)
    assert flow.us_email_first_model.events == list(PREPARATION_EVENT_ORDER)
    assert calls[:3] == [
        "acquire_phone_after_email",
        ("authenticate_member_with_phone", SIGNUP_URL),
        ("bind_card_in_authenticated_session", REVIEW_URL),
    ]
    assert flow.state.phone_login_verified is True
    assert flow.state.member_authenticated is True
    assert flow.state.funding_selected is True
    assert flow.state.funding_instrument_id == "CARD-US"


def test_paypal_flow_non_us_phase3_keeps_legacy_two_step_order(monkeypatch):
    calls = []

    class LegacyFlow(PayPalFlow):
        def _confirm_phone_with_retry(self, token, signup_url):
            calls.append(("confirm_phone", token, signup_url))

        def _signup_with_card_retry(self, token, signup_url):
            calls.append(("signup", token, signup_url))
            self.state.euat_token = "legacy-euat"

    flow = LegacyFlow.__new__(LegacyFlow)
    flow.country = "GB"
    flow.ba_token = "BA-legacy"
    flow.state = SessionState(
        ba_token=flow.ba_token,
        ec_token="EC-legacy",
        signup_url="https://www.paypal.com/checkoutweb/signup?token=EC-legacy",
    )
    flow.session = StateSessionStub(flow.state)
    monkeypatch.setattr("paypal.flow.send_tealeaf_data", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("paypal.flow.send_analytics_ts", lambda *_args, **_kwargs: None)

    flow._phase3_signup_and_2fa()

    assert calls == [
        ("confirm_phone", "EC-legacy", flow.state.signup_url),
        ("signup", "EC-legacy", flow.state.signup_url),
    ]
    assert flow.state.euat_token == "legacy-euat"


@pytest.mark.parametrize(
    ("selected", "expected_selected", "expected_id"),
    [
        ({"lastDigits": "1111", "type": "CARD"}, False, ""),
        ({"id": "CARD-selected", "lastDigits": "2222", "type": "CARD"}, True, "CARD-selected"),
    ],
)
def test_buyer_funding_context_requires_selected_instrument_id(
    selected, expected_selected, expected_id
):
    state = SessionState(euat_token="fixture-euat")
    graphql_result = {
        "data": {
            "checkoutSession": {
                "buyer": {
                    "userId": "buyer-US",
                    "auth": {"accessToken": "refreshed-euat"},
                },
                "fundingOptions": {
                    "fundingInstrument": selected,
                    "allPlans": [
                        {
                            "fundingSources": [
                                {"fundingInstrument": {"id": "CARD-available"}}
                            ]
                        }
                    ],
                },
            }
        }
    }
    flow = PayPalFlow.__new__(PayPalFlow)
    flow.state = state
    flow.session = StateSessionStub(state, graphql_result)
    flow.address = fixture_address()
    flow.locale = "en_US"

    context = flow._sync_buyer_funding_context("EC-fixture", REVIEW_URL)

    assert context["funding_selected"] is expected_selected
    assert context["funding_instrument_id"] == expected_id
    assert context["available_instrument_ids"] == ["CARD-available"]
    assert state.funding_selected is expected_selected
    assert state.funding_instrument_id == expected_id


def test_web_phone_provider_is_blocked_until_email_submission(monkeypatch):
    provider_calls = []
    flow = paypal_web.WebPayPalFlow.__new__(paypal_web.WebPayPalFlow)
    flow.country = "US"
    flow.state = SessionState(email_submitted=False)
    flow.user = fixture_user()
    flow.card = CardInfo("4111111111111111", "12/2030", "123")
    flow.address = fixture_address()
    flow.runtime_form_schema = {}
    flow.job = SimpleNamespace(
        sms_provider="smsbower",
        runtime_schema=None,
        set_status=lambda status, stage: None,
        set_generated=lambda payload: None,
    )

    monkeypatch.setattr(
        paypal_web,
        "sms_provider_client",
        lambda provider: provider_calls.append(("resolve", provider)) or object(),
    )
    monkeypatch.setattr(paypal_web, "sms_provider_label", lambda _provider: "SMSBower")
    monkeypatch.setattr(
        paypal_web,
        "acquire_job_sms_activation",
        lambda job, client, *, country: provider_calls.append(("acquire", country))
        or SimpleNamespace(phone="+12025550123"),
    )

    with pytest.raises(RuntimeError, match="US_EMAIL_NOT_SUBMITTED"):
        flow.acquire_phone_after_email()
    assert provider_calls == []

    flow.state.email_submitted = True
    phone = flow.acquire_phone_after_email()

    assert phone == "+12025550123"
    assert provider_calls == [("resolve", "smsbower"), ("acquire", "US")]


@pytest.mark.parametrize(
    ("country", "acquire_after_phase2", "expected_email"),
    [
        ("US", True, "source-account@example.com"),
        ("GB", False, "generated-gb@example.com"),
    ],
)
def test_run_job_automatic_sms_order_and_us_source_email(
    monkeypatch, country, acquire_after_phase2, expected_email
):
    events = []
    proxy = SimpleNamespace(url="", enabled=False, label="fixture")
    provider_phone = "+12025550123" if country == "US" else "+447700900123"

    def generate_user_stub(phone, country):
        events.append(("generate_user", country, phone))
        return fixture_user(
            phone=phone,
            email=f"generated-{country.lower()}@example.com",
            country=country,
        )

    def acquire_stub(job, _client, *, country):
        events.append(("acquire", country))
        job.phone = provider_phone
        return SimpleNamespace(phone=provider_phone)

    class FlowStub:
        acquire_phone_after_email = paypal_web.WebPayPalFlow.acquire_phone_after_email

        def __init__(self, **kwargs):
            self.job = kwargs["job"]
            self.user = kwargs["user"]
            self.card = kwargs["card"]
            self.address = kwargs["address"]
            self.country = self.job.country
            self.state = SessionState()
            self.runtime_form_schema = {}
            events.append(("flow_init", self.user.email, self.user.phone))

        def run(self):
            events.append(("phase2_complete", self.country))
            if self.country == "US":
                self.state.email_submitted = True
                self.acquire_phone_after_email()
            return {"status": "success", "return_url": "https://merchant.test/return"}

    monkeypatch.setattr(paypal_web, "select_working_proxy", lambda *_args, **_kwargs: proxy)
    monkeypatch.setattr(paypal_web, "generate_user", generate_user_stub)
    monkeypatch.setattr(
        paypal_web,
        "generate_card",
        lambda **_kwargs: CardInfo("4111111111111111", "12/2030", "123"),
    )
    monkeypatch.setattr(paypal_web, "generate_address", fixture_address)
    monkeypatch.setattr(paypal_web, "sms_provider_client", lambda _provider: object())
    monkeypatch.setattr(paypal_web, "sms_provider_label", lambda _provider: "SMSBower")
    monkeypatch.setattr(paypal_web, "acquire_job_sms_activation", acquire_stub)
    monkeypatch.setattr(paypal_web, "WebPayPalFlow", FlowStub)
    monkeypatch.setattr(
        paypal_web,
        "PAYMENT_COMPLETION_PRESENTER",
        SimpleNamespace(
            present=lambda result, target: SimpleNamespace(
                requires_braintree_bridge=False, route="fixture"
            )
        ),
    )
    monkeypatch.setattr(paypal_web, "record_payment_audit", lambda *_args: None)
    monkeypatch.setattr(paypal_web, "record_protocol_metric", lambda *_args: None)

    job = paypal_web.WebJob(
        id=f"fixture-{country.lower()}",
        owner_device_id="fixture-device",
        ba_token="BA-ABCDEFGH123456",
        phone="",
        country=country,
        sms_provider="smsbower",
        sms_country=country,
        buyer_mode="original",
        source_account_email="source-account@example.com",
        _proxy_config=proxy,
    )
    job.acquire_execution_slot = lambda: None
    job.release_execution_slot = lambda: None
    job.finalize_sms_activation = lambda **_kwargs: None

    paypal_web.run_job(job)

    event_names = [event[0] for event in events]
    acquire_index = event_names.index("acquire")
    phase2_index = event_names.index("phase2_complete")
    assert (acquire_index > phase2_index) is acquire_after_phase2
    flow_init = next(event for event in events if event[0] == "flow_init")
    assert flow_init[1] == expected_email
    assert job.status == "completed"


def test_identity_elevation_us_authenticated_selected_skips_signup_get():
    state = SessionState(
        ba_token="BA-fixture",
        ec_token="EC-fixture",
        euat_token="fixture-euat",
        signup_context_ready=True,
        member_authenticated=True,
        funding_selected=True,
    )
    session = StateSessionStub(state)
    flow = IdentityElevationPayPalFlow.__new__(IdentityElevationPayPalFlow)
    flow.state = state
    flow.session = session
    flow.address = fixture_address()
    flow.ba_token = "BA-fixture"
    flow.locale = "en_US"
    query_calls = []
    expected = {
        "buyer_ready": True,
        "funding_selected": True,
        "funding_available": True,
        "funding_available_count": 1,
        "funding_errors": [],
        "funding_checkpoints": [],
        "fatal_contingency": "",
    }
    flow._build_member_review_url = MethodType(lambda self: REVIEW_URL, flow)
    flow._query_elevated_context = MethodType(
        lambda self, token, referer: query_calls.append((token, referer)) or expected,
        flow,
    )

    result = flow._protocol_identity_elevation()

    assert result is expected
    assert query_calls == [("EC-fixture", REVIEW_URL)]
    assert session.get_calls == []
