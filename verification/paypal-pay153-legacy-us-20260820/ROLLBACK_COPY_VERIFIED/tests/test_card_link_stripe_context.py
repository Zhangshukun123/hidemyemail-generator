from __future__ import annotations

import json
import unittest

from hidemyemail_generator import card_link_runtime


CS_ID = "cs_test_stripe_context"
PM_ID = "pm_test_stripe_context"
STRIPE_PK = "pk_test_stripe_context"
STRIPE_JS_ID = "stripe-js-context-id"
ELEMENTS_SESSION_ID = "elements_session_context"
CONFIG_ID = "config_context"
RUNTIME_VERSION = "runtime-context-version"
STRIPE_VERSION = "2025-03-31.basil; context_test=v1"
STRIPE_HOSTED_URL = f"https://checkout.stripe.com/c/pay/{CS_ID}"
PAYPAL_REDIRECT_URL = "https://pm-redirects.stripe.com/authorize/context"


class _Response:
    def __init__(self, payload: dict, *, status_code: int = 200) -> None:
        self._payload = dict(payload)
        self.status_code = status_code
        self.text = json.dumps(payload)
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return dict(self._payload)


class _StripeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, url: str, **options) -> _Response:
        self.calls.append(("POST", url, options))
        if url.endswith(f"/v1/payment_pages/{CS_ID}/init"):
            return _Response(
                {
                    "stripe_hosted_url": STRIPE_HOSTED_URL,
                    "stripe_js_id": STRIPE_JS_ID,
                    "elements_session_id": ELEMENTS_SESSION_ID,
                    "config_id": CONFIG_ID,
                    "init_checksum": "checksum-context",
                    "currency": "usd",
                    "total_summary": {"due": 0},
                    "payment_method_types": ["card", "paypal"],
                }
            )
        if url.endswith("/v1/payment_methods"):
            return _Response({"id": PM_ID})
        if url.endswith(f"/v1/payment_pages/{CS_ID}/confirm"):
            return _Response(
                {"submission_attempt": {"state": "requires_approval"}}
            )
        raise AssertionError(f"unexpected POST: {url}")

    def get(self, url: str, **options) -> _Response:
        self.calls.append(("GET", url, options))
        if url.endswith(f"/v1/payment_pages/{CS_ID}"):
            return _Response(
                {
                    "next_action": {
                        "type": "redirect_to_url",
                        "redirect_to_url": {"url": PAYPAL_REDIRECT_URL},
                    }
                }
            )
        raise AssertionError(f"unexpected GET: {url}")


class CardLinkStripeContextTests(unittest.TestCase):
    def test_stripe_context_is_reused_from_init_through_payment_confirm_and_poll(self):
        stripe = _StripeSession()
        checkout = {
            "cs_id": CS_ID,
            "billing_country": "US",
            "currency": "USD",
            "processor_entity": "openai_llc",
            "stripe_publishable_key": STRIPE_PK,
        }
        seed_context = {
            "stripe_js_id": STRIPE_JS_ID,
            "runtime_version": RUNTIME_VERSION,
            "stripe_version": STRIPE_VERSION,
        }

        init_payload = card_link_runtime.opll_stripe_init(
            CS_ID,
            "US",
            "USD",
            "http://sticky-us.example:8000",
            payment_locale="en",
            stripe=stripe,
            ctx=seed_context,
            checkout=checkout,
            browser_timezone="America/New_York",
            stripe_version=STRIPE_VERSION,
        )
        ctx = card_link_runtime.opll_stripe_context(
            init_payload,
            payment_locale="en",
            ctx=seed_context,
        )
        billing = card_link_runtime.opll_billing_for_country(
            "US",
            account_email="member@icloud.com",
            city_hint="New York",
            state_hint="NY",
        )
        pm_id = card_link_runtime.opll_stripe_create_paypal_method(
            stripe,
            CS_ID,
            ctx,
            billing,
            STRIPE_PK,
        )
        confirm_payload = card_link_runtime.opll_stripe_confirm(
            stripe,
            CS_ID,
            pm_id,
            STRIPE_PK,
            init_payload,
            ctx,
            checkout,
            STRIPE_HOSTED_URL,
        )
        redirect_url = card_link_runtime.opll_stripe_payment_page_redirect_url(
            stripe,
            CS_ID,
            STRIPE_PK,
            payment_locale="en",
            timeout_seconds=1,
            ctx=ctx,
        )

        self.assertEqual(pm_id, PM_ID)
        self.assertEqual(
            confirm_payload["submission_attempt"]["state"],
            "requires_approval",
        )
        self.assertEqual(redirect_url, PAYPAL_REDIRECT_URL)
        self.assertEqual(ctx["stripe_js_id"], STRIPE_JS_ID)
        self.assertEqual(ctx["elements_session_id"], ELEMENTS_SESSION_ID)
        self.assertEqual(ctx["runtime_version"], RUNTIME_VERSION)
        self.assertEqual(ctx["stripe_version"], STRIPE_VERSION)

        init_data = stripe.calls[0][2]["data"]
        payment_method_data = stripe.calls[1][2]["data"]
        confirm_data = stripe.calls[2][2]["data"]
        poll_params = stripe.calls[3][2]["params"]

        self.assertEqual(init_data["browser_timezone"], "America/New_York")
        self.assertEqual(
            init_data["elements_session_client[stripe_js_id]"],
            ctx["stripe_js_id"],
        )
        self.assertEqual(init_data["_stripe_version"], ctx["stripe_version"])

        for identifier in ("guid", "muid", "sid"):
            self.assertTrue(ctx[identifier])
            self.assertEqual(payment_method_data[identifier], ctx[identifier])
            self.assertEqual(confirm_data[identifier], ctx[identifier])

        self.assertEqual(
            payment_method_data[
                "client_attribution_metadata[client_session_id]"
            ],
            ctx["stripe_js_id"],
        )
        self.assertEqual(
            payment_method_data[
                "client_attribution_metadata[elements_session_id]"
            ],
            ctx["elements_session_id"],
        )
        self.assertIn(ctx["runtime_version"], payment_method_data["payment_user_agent"])
        self.assertEqual(
            payment_method_data["_stripe_version"], ctx["stripe_version"]
        )

        self.assertEqual(confirm_data["version"], ctx["runtime_version"])
        self.assertEqual(
            confirm_data["elements_session_client[stripe_js_id]"],
            ctx["stripe_js_id"],
        )
        self.assertEqual(
            confirm_data["elements_session_client[session_id]"],
            ctx["elements_session_id"],
        )
        self.assertEqual(confirm_data["_stripe_version"], ctx["stripe_version"])

        self.assertEqual(
            poll_params["elements_session_client[stripe_js_id]"],
            ctx["stripe_js_id"],
        )
        self.assertEqual(
            poll_params["elements_session_client[session_id]"],
            ctx["elements_session_id"],
        )
        self.assertEqual(poll_params["_stripe_version"], ctx["stripe_version"])

    def test_us_billing_hints_select_new_york_metadata(self):
        billing = card_link_runtime.opll_billing_for_country(
            "US",
            account_email="member@icloud.com",
            city_hint="New York",
            state_hint="NY",
        )

        self.assertEqual(billing["country"], "US")
        self.assertEqual(billing["city"], "New York")
        self.assertEqual(billing["state"], "NY")
        self.assertEqual(billing["postal_code"], "10007")


if __name__ == "__main__":
    unittest.main()
