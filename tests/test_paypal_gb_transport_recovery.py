from __future__ import annotations

import unittest
from unittest.mock import patch

from hidemyemail_generator import card_link_runtime


class _Response:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = {"cf-ray": f"fixture-{status_code}"}

    def json(self):
        return self._payload


class _WarmupSession:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **options):
        self.calls.append((url, options))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _StripeSession:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **options):
        self.calls.append((url, options))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class PayPalGbTransportRecoveryTests(unittest.TestCase):
    checkout = {
        "cs_id": "cs_gb_retry_fixture",
        "billing_country": "GB",
        "currency": "GBP",
        "processor_entity": "openai_llc",
        "stripe_publishable_key": "pk_test_fixture",
    }

    def test_checkout_warmup_retries_http_500_in_same_session(self):
        session = _WarmupSession(
            [_Response(500, text="Application Error"), _Response(200)]
        )
        logs = []

        with patch.object(card_link_runtime.time, "sleep") as sleep:
            card_link_runtime.opll_chatgpt_checkout_warmup(
                session,
                self.checkout["cs_id"],
                self.checkout,
                diagnostic_log=logs.append,
                network_attempts=2,
                network_retry_delays=(1.0,),
            )

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0][0], session.calls[1][0])
        self.assertTrue(any("暂时返回 HTTP 500" in item for item in logs))
        sleep.assert_called_once_with(1.0)

    def test_gb_checkout_warmup_continues_after_repeated_http_500(self):
        session = _WarmupSession(
            [
                _Response(500, text="Application Error"),
                _Response(500, text="Application Error"),
            ]
        )
        logs = []

        with patch.object(card_link_runtime.time, "sleep"):
            warmed = card_link_runtime.opll_chatgpt_checkout_warmup(
                session,
                self.checkout["cs_id"],
                self.checkout,
                diagnostic_log=logs.append,
                network_attempts=2,
                network_retry_delays=(1.0,),
                allow_retryable_status_failure=True,
            )

        self.assertFalse(warmed)
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(
            any(
                "Stripe Init 与 Confirm 前 Sentinel 硬校验" in item
                for item in logs
            )
        )

    def test_repeated_http_500_remains_hard_failure_by_default(self):
        session = _WarmupSession(
            [
                _Response(500, text="Application Error"),
                _Response(500, text="Application Error"),
            ]
        )

        with (
            patch.object(card_link_runtime.time, "sleep"),
            self.assertRaisesRegex(
                card_link_runtime.OpllChatgptSentinelError,
                "HTTP 500",
            ),
        ):
            card_link_runtime.opll_chatgpt_checkout_warmup(
                session,
                self.checkout["cs_id"],
                self.checkout,
                network_attempts=2,
            )

        self.assertEqual(len(session.calls), 2)

    def test_stripe_init_retries_wrong_version_transport_with_same_context(self):
        error = RuntimeError(
            "curl: (56) BoringSSL SSL_read: WRONG_VERSION_NUMBER"
        )
        payload = {
            "stripe_hosted_url": (
                "https://checkout.stripe.com/c/pay/cs_gb_retry_fixture"
            ),
            "currency": "gbp",
            "total_summary": {"due": 2000},
        }
        session = _StripeSession([error, _Response(200, payload)])
        context = {"stripe_js_id": "stable-stripe-js-id"}
        logs = []

        with patch.object(card_link_runtime.time, "sleep") as sleep:
            result = card_link_runtime.opll_stripe_init(
                self.checkout["cs_id"],
                "GB",
                "GBP",
                "http://pool1.example:8000",
                stripe=session,
                ctx=context,
                checkout=self.checkout,
                network_attempts=2,
                network_retry_delays=(1.0,),
                diagnostic_log=logs.append,
            )

        self.assertEqual(result, payload)
        self.assertEqual(len(session.calls), 2)
        first_data = session.calls[0][1]["data"]
        second_data = session.calls[1][1]["data"]
        self.assertEqual(first_data, second_data)
        self.assertEqual(
            first_data["elements_session_client[stripe_js_id]"],
            "stable-stripe-js-id",
        )
        self.assertTrue(any("Stripe Init 连接异常" in item for item in logs))
        sleep.assert_called_once_with(1.0)

    def test_checkout_warmup_does_not_retry_http_403(self):
        session = _WarmupSession([_Response(403, text="forbidden")])

        with self.assertRaisesRegex(
            card_link_runtime.OpllChatgptSentinelError,
            "HTTP 403",
        ):
            card_link_runtime.opll_chatgpt_checkout_warmup(
                session,
                self.checkout["cs_id"],
                self.checkout,
                network_attempts=2,
            )

        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
