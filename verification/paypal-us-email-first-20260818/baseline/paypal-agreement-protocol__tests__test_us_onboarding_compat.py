from types import SimpleNamespace

import web as paypal_web
from paypal.flow import PayPalFlow
from paypal.manual_browser import ManualBrowserController
from paypal.models import SessionState
from paypal.onboarding_compat import (
    OnboardingCompatibilityPresenter,
    OnboardingFailureModel,
    US_MEMBER_RISK_REVIEW,
)


US_OAS_ERRORS = [
    {
        "message": "OAS_ERROR",
        "checkpoints": ["createMemberAccount"],
        "path": ["onboardAccount"],
    }
]


class RecoveryView:
    def __init__(self, recovered=True):
        self.recovered = recovered
        self.signup_urls = []

    def complete_member_onboarding(self, signup_url):
        self.signup_urls.append(signup_url)
        return self.recovered


def test_us_oas_model_matches_member_risk_review():
    model = OnboardingFailureModel.from_errors("us", US_OAS_ERRORS)

    assert model.country == "US"
    assert model.messages == frozenset({"OAS_ERROR"})
    assert model.checkpoints == frozenset({"createMemberAccount"})


def test_presenter_routes_us_oas_to_official_member_handoff():
    view = RecoveryView()

    decision = OnboardingCompatibilityPresenter().recover(
        country="US",
        errors=US_OAS_ERRORS,
        signup_url="https://www.paypal.com/checkoutweb/signup?token=EC-test",
        view=view,
    )

    assert decision.matched is True
    assert decision.recovered is True
    assert decision.code == US_MEMBER_RISK_REVIEW
    assert view.signup_urls == [
        "https://www.paypal.com/checkoutweb/signup?token=EC-test"
    ]


def test_presenter_does_not_route_non_us_oas():
    view = RecoveryView()

    decision = OnboardingCompatibilityPresenter().recover(
        country="GB",
        errors=US_OAS_ERRORS,
        signup_url="https://www.paypal.com/checkoutweb/signup?token=EC-test",
        view=view,
    )

    assert decision.matched is False
    assert decision.recovered is False
    assert view.signup_urls == []


class FlowSessionStub:
    def __init__(self):
        self.euat_tokens = []

    def set_euat_token(self, value):
        self.euat_tokens.append(value)

    def diagnostic_snapshot(self):
        return {
            "engine": "fixture",
            "cookie_names": ["nsid"],
            "last_graphql": {"paypal_debug_id": "fixture-debug-id"},
        }


class RecoveringPayPalFlow(PayPalFlow):
    def __init__(self):
        self.country = "US"
        self.max_card_attempts = 1
        self.runtime_form_schema = None
        self.session = FlowSessionStub()
        self.state = SessionState(
            ba_token="BA-fixture",
            ec_token="EC-fixture",
            ctx_id="ctx-fixture",
            ssrt="ssrt-fixture",
            content_identifier="US:en:fixture:compliance.signupTerms",
        )
        self.handoff_urls = []

    def _send_signup_attempt(self, token, signup_url):
        return {"data": {"onboardAccount": None}, "errors": US_OAS_ERRORS}

    def complete_member_onboarding(self, signup_url):
        self.handoff_urls.append(signup_url)
        self.state.euat_token = "fixture-euat"
        self.state.user_id = "fixture-user"
        return True


def test_signup_flow_continues_after_us_official_member_handoff():
    flow = RecoveringPayPalFlow()
    signup_url = "https://www.paypal.com/checkoutweb/signup?token=EC-fixture"

    flow._signup_with_card_retry("EC-fixture", signup_url)

    assert flow.handoff_urls == [signup_url]
    assert flow.state.euat_token == "fixture-euat"
    assert flow.state.user_id == "fixture-user"


def test_member_completion_requires_euat_and_paypal_review_route():
    controller = ManualBrowserController(
        proxy_config=None,
        user_agent="fixture",
        cookies=[],
        start_url="https://www.paypal.com/checkoutweb/signup",
        completion_mode="member",
    )
    cookies = [
        {
            "name": "AV894Kt2TSumQQrJwe-8mzmyREO",
            "value": "fixture-euat",
        }
    ]

    assert controller._completion_ready(
        cookies,
        "https://www.paypal.com/webapps/hermes#/billingweb/review",
        200,
    )
    assert not controller._completion_ready(
        [],
        "https://www.paypal.com/webapps/hermes#/billingweb/review",
        200,
    )
    assert not controller._completion_ready(
        cookies,
        "https://www.paypal.com/signin",
        200,
    )


def test_web_flow_member_handoff_syncs_browser_buyer_context(monkeypatch):
    observed = {}

    class ControllerStub:
        def __init__(self, **kwargs):
            observed["controller"] = kwargs

        def state(self):
            return SimpleNamespace(
                current_url="https://www.paypal.com/webapps/hermes#/billingweb/review"
            )

    class JobStub:
        def wait_for_browser(self, controller, *, stage, prompt):
            observed["stage"] = stage
            observed["prompt"] = prompt
            return [
                {
                    "name": "AV894Kt2TSumQQrJwe-8mzmyREO",
                    "value": "fixture-euat",
                    "domain": ".paypal.com",
                }
            ]

    monkeypatch.setattr(paypal_web, "ManualBrowserController", ControllerStub)
    flow = paypal_web.WebPayPalFlow.__new__(paypal_web.WebPayPalFlow)
    flow.country = "US"
    flow.proxy_config = SimpleNamespace()
    flow.state = SessionState(ba_token="BA-fixture", ec_token="EC-fixture")
    flow.ba_token = "BA-fixture"
    flow.job = JobStub()
    flow._browser_cookie_snapshot = lambda: []
    flow._sync_browser_cookies = lambda cookies: observed.update(cookies=cookies)
    flow._sync_buyer_context = (
        lambda token, referer: observed.update(token=token, referer=referer) or True
    )

    recovered = flow.complete_member_onboarding(
        "https://www.paypal.com/checkoutweb/signup?token=EC-fixture"
    )

    assert recovered is True
    assert observed["controller"]["completion_mode"] == "member"
    assert observed["token"] == "EC-fixture"
    assert "新版 US PayPal" in observed["stage"]
    assert "官方页面" in observed["prompt"]


def test_wait_for_browser_uses_presented_stage_and_prompt():
    job = paypal_web.WebJob(
        id="fixturejob",
        owner_device_id="fixture-device",
        ba_token="BA-fixture",
        phone="+12025550123",
    )
    observed = {}

    class BrowserStub:
        def start(self):
            observed["started"] = True

        def wait(self, timeout, cancel_event):
            observed["stage"] = job.stage
            observed["prompt"] = job.awaiting_prompt
            return [{"name": "fixture", "value": "cookie"}]

        def stop(self):
            observed["stopped"] = True

    job.release_execution_slot = lambda: None
    job.acquire_execution_slot = lambda: None

    cookies = job.wait_for_browser(
        BrowserStub(),
        stage="等待新版会员验证",
        prompt="请完成 PayPal 官方页面验证",
    )

    assert cookies == [{"name": "fixture", "value": "cookie"}]
    assert observed["stage"] == "等待新版会员验证"
    assert observed["prompt"] == "请完成 PayPal 官方页面验证"
    assert observed["started"] is True
    assert observed["stopped"] is True
