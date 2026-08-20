from unittest.mock import patch

import web
from paypal.models import CardInfo
from paypal.payment_completion import PaymentCompletionPresenter
from paypal.proxy import ProxyConfig, ProxyEntry


OPENAI_FINAL_URL = (
    "https://pay.openai.com/payments/complete?redirect_status=succeeded&"
    "success_return_url=https%3A%2F%2Fchatgpt.com%2Fcheckout%2Fverify%3Fplan_type%3Dplus"
)
OPENAI_VERIFY_URL = "https://chatgpt.com/checkout/verify?plan_type=plus"


class OpenAIPlusFlow:
    def __init__(self, **kwargs):
        self.job = kwargs["job"]

    def run(self):
        return {
            "status": "success",
            "user_id": "payer-fixture",
            "return_url": "https://merchant.example/return",
            "final_redirect_url": OPENAI_FINAL_URL,
            "verification_url": OPENAI_VERIFY_URL,
            "pending_url": OPENAI_FINAL_URL,
            "redirect_status": "succeeded",
            "settlement_status": "confirmed",
        }


class ForbiddenBraintreeClient:
    calls = 0

    def __init__(self, *args, **kwargs):
        type(self).calls += 1
        raise ConnectionError("Grok bridge is unavailable")


class Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload
        self.text = str(payload)

    def json(self):
        return self.payload


class FailingGrokClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        type(self).calls.append((url, kwargs))
        if url.endswith("/braintree-context"):
            return Response(
                200,
                {
                    "result": {
                        "account_id": "grok-account-fixture",
                        "region": "SG",
                    }
                },
            )
        return Response(502, {"ok": False, "error": "completion rejected"})


def make_job() -> tuple[web.WebJob, ProxyConfig]:
    proxy = ProxyConfig(True, ProxyEntry("127.0.0.1", 9999, "", ""))
    return (
        web.WebJob(
            id="openai-plus-routing",
            owner_device_id="fixture-device",
            ba_token="BA-OPENAIPLUS123456",
            phone="+6581234567",
            country="SG",
            buyer_mode="identity_elevation",
            max_card_attempts=1,
            proxy_enabled=True,
            _proxy_config=proxy,
            _proxy_pool=["http://127.0.0.1:9999"],
        ),
        proxy,
    )


def test_openai_plus_success_skips_grok_bridge_and_stays_confirmed():
    job, proxy = make_job()
    ForbiddenBraintreeClient.calls = 0
    audit_results = []

    with (
        patch("web.select_working_proxy", return_value=proxy),
        patch(
            "web.generate_card",
            return_value=CardInfo("4111111111111111", "12/2030", "123"),
        ),
        patch("web.WebIdentityElevationPayPalFlow", OpenAIPlusFlow),
        patch("web.httpx.Client", ForbiddenBraintreeClient),
        patch(
            "web.record_payment_audit",
            side_effect=lambda _job, result: audit_results.append(dict(result)),
        ),
        patch("web.record_protocol_metric"),
    ):
        web.run_job(job)

    assert ForbiddenBraintreeClient.calls == 0
    assert job.status == "completed"
    assert job.result["status"] == "success"
    assert job.result["settlement_status"] == "confirmed"
    assert job.result["braintree_bridge_status"] == "not_applicable"
    assert job.result["openai_checkout_confirmed"] is True
    assert audit_results == [job.result]


def test_explicit_openai_target_is_enough_when_the_return_follow_fails():
    result = {
        "status": "success",
        "redirect_status": "succeeded",
        "settlement_status": "authorization_only",
    }

    decision = PaymentCompletionPresenter().present(result, target="openai_plus")

    assert decision.requires_braintree_bridge is False
    assert result["status"] == "success"
    assert result["settlement_status"] == "authorization_only"
    assert result["braintree_bridge_status"] == "not_applicable"
    assert result["openai_checkout_confirmed"] is False


def test_openai_text_inside_an_untrusted_host_does_not_match():
    result = {
        "status": "success",
        "final_redirect_url": (
            "https://merchant.example/finish?redirect_status=succeeded&"
            "success_return_url=https%3A%2F%2Fchatgpt.com%2Fcheckout%2Fverify"
        ),
        # This mirrors the legacy flow parser output derived from the untrusted
        # final URL and must not become an independent trust anchor.
        "verification_url": "https://chatgpt.com/checkout/verify",
        "pending_url": "https://chatgpt.com/checkout/verify",
        "redirect_status": "succeeded",
    }

    decision = PaymentCompletionPresenter().present(result)

    assert decision.requires_braintree_bridge is True
    assert "braintree_bridge_status" not in result


def test_browser_parser_differential_url_is_rejected_everywhere():
    spoof = (
        "https://evil.example\\@chatgpt.com/checkout/verify?redirect_status=succeeded"
    )
    result = {
        "status": "success",
        "final_redirect_url": spoof,
        "verification_url": spoof,
        "pending_url": spoof,
        "redirect_status": "succeeded",
    }

    decision = PaymentCompletionPresenter().present(result)
    public = web.safe_result_payload(result)

    assert decision.requires_braintree_bridge is True
    assert public["verification_url"] != spoof
    assert public["pending_url"] != spoof


def test_nested_redirect_status_cannot_confirm_the_outer_openai_return():
    result = {
        "status": "success",
        "settlement_status": "authorization_only",
        "redirect_status": "succeeded",
        "final_redirect_url": (
            "https://pay.openai.com/complete?success_return_url="
            "https%3A%2F%2Fchatgpt.com%2Fcheckout%2Fverify%3F"
            "redirect_status%3Dsucceeded"
        ),
        "verification_url": (
            "https://chatgpt.com/checkout/verify?redirect_status=succeeded"
        ),
    }

    decision = PaymentCompletionPresenter().present(result)

    assert decision.requires_braintree_bridge is False
    assert result["settlement_status"] == "authorization_only"
    assert result["openai_checkout_confirmed"] is False


def test_explicit_openai_target_downgrades_unbound_confirmed_status():
    result = {
        "status": "success",
        "settlement_status": "confirmed",
        "redirect_status": "succeeded",
        "final_redirect_url": "https://merchant.example/complete",
    }

    decision = PaymentCompletionPresenter().present(result, target="openai_plus")

    assert decision.requires_braintree_bridge is False
    assert result["redirect_status"] == ""
    assert result["settlement_status"] == "authorization_only"
    assert result["openai_checkout_confirmed"] is False


def test_explicit_grok_completion_failure_remains_a_terminal_failure():
    class GrokFlow(OpenAIPlusFlow):
        def run(self):
            return {
                "status": "success",
                "user_id": "payer-fixture",
                "return_url": "https://grok.com/payments/return",
                "settlement_status": "authorization_only",
            }

    job, proxy = make_job()
    job.completion_target = "grok_braintree"
    FailingGrokClient.calls = []

    with (
        patch("web.select_working_proxy", return_value=proxy),
        patch(
            "web.generate_card",
            return_value=CardInfo("4111111111111111", "12/2030", "123"),
        ),
        patch("web.WebIdentityElevationPayPalFlow", GrokFlow),
        patch("web.httpx.Client", FailingGrokClient),
        patch("web.record_payment_audit"),
        patch("web.record_protocol_metric"),
    ):
        web.run_job(job)

    assert len(FailingGrokClient.calls) == 2
    assert job.status == "failed"
    assert job.result["status"] == "error"
    assert job.result["error_code"] == "BRAINTREE_VAULT_FAILED"
    assert job.result["settlement_status"] == "vault_failed"
