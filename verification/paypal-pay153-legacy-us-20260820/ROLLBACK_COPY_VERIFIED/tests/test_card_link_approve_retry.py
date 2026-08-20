from __future__ import annotations

import json
import unittest
from unittest import mock

from hidemyemail_generator import card_link_runtime


CHECKOUT_ID = "oaics_clean_retry"
DEVICE_ID = "device-clean-retry"
STICKY_PROXY = "http://sticky.example:8000"
CHECKOUT = {
    "cs_id": CHECKOUT_ID,
    "billing_country": "US",
    "currency": "USD",
    "processor_entity": "openai_llc",
    "oai_device_id": DEVICE_ID,
}


class _Response:
    def __init__(self, payload: dict, *, status_code: int = 200) -> None:
        self._payload = dict(payload)
        self.status_code = status_code
        self.text = json.dumps(payload)
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return dict(self._payload)


class _ApproveSession:
    def __init__(self, approve_result: str) -> None:
        self.approve_result = approve_result
        self.calls: list[tuple[str, str, dict]] = []
        self.closed = False

    def get(self, url: str, **options) -> _Response:
        self.calls.append(("GET", url, options))
        return _Response({})

    def post(self, url: str, **options) -> _Response:
        self.calls.append(("POST", url, options))
        if url.endswith("/backend-api/sentinel/ping"):
            return _Response({"result": "ok"})
        if url.endswith("/backend-api/payments/checkout/approve"):
            return _Response({"result": self.approve_result})
        raise AssertionError(f"unexpected POST: {url}")

    def close(self) -> None:
        self.closed = True


class CardLinkApproveRetryTests(unittest.TestCase):
    def assert_approve_request_order(self, session: _ApproveSession) -> None:
        self.assertEqual(
            [(method, url) for method, url, _options in session.calls],
            [
                (
                    "GET",
                    f"https://chatgpt.com/checkout/openai_llc/{CHECKOUT_ID}",
                ),
                ("POST", "https://chatgpt.com/backend-api/sentinel/ping"),
                (
                    "POST",
                    "https://chatgpt.com/backend-api/payments/checkout/approve",
                ),
            ],
        )
        self.assertEqual(
            session.calls[-1][2]["json"],
            {
                "checkout_session_id": CHECKOUT_ID,
                "processor_entity": "openai_llc",
            },
        )

    def run_approve(self, sessions: list[_ApproveSession], *, attempts: int):
        built: list[dict[str, object]] = []
        self.last_built_sessions = built

        def build_session(
            access_token: str,
            proxy_url: str = "",
            request_locale: str = "en-US",
            *,
            device_id: str = "",
            **options,
        ) -> _ApproveSession:
            built.append(
                {
                    "access_token": access_token,
                    "proxy_url": proxy_url,
                    "request_locale": request_locale,
                    "device_id": device_id,
                    "options": options,
                }
            )
            return sessions[len(built) - 1]

        with mock.patch.object(
            card_link_runtime,
            "opll_build_chatgpt_session",
            side_effect=build_session,
        ), mock.patch.object(card_link_runtime.time, "sleep"):
            result = card_link_runtime.opll_chatgpt_approve_with_retry(
                "at-clean-retry",
                CHECKOUT_ID,
                CHECKOUT,
                STICKY_PROXY,
                request_locale="en-US",
                attempts=attempts,
                interval_seconds=0,
                rotate_ip_each_attempt=False,
            )
        return result, built

    def test_blocked_retries_in_clean_session_on_same_checkout_proxy_and_device(self):
        first = _ApproveSession("blocked")
        second = _ApproveSession("approved")

        result, built = self.run_approve([first, second], attempts=3)

        self.assertIs(result, second)
        self.assertIsNot(first, second)
        self.assertEqual(len(built), 2)
        self.assertEqual(
            [item["proxy_url"] for item in built],
            [STICKY_PROXY, STICKY_PROXY],
        )
        self.assertEqual(
            [item["device_id"] for item in built],
            [DEVICE_ID, DEVICE_ID],
        )
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)
        self.assert_approve_request_order(first)
        self.assert_approve_request_order(second)

    def test_two_blocked_sessions_stop_without_changing_proxy(self):
        first = _ApproveSession("blocked")
        second = _ApproveSession("blocked")

        with self.assertRaises(card_link_runtime.OpllChatgptApproveBlocked):
            self.run_approve([first, second], attempts=4)
        built = self.last_built_sessions

        self.assertEqual(len(built), 2)
        self.assertEqual(
            [item["proxy_url"] for item in built],
            [STICKY_PROXY, STICKY_PROXY],
        )
        self.assertEqual(
            [item["device_id"] for item in built],
            [DEVICE_ID, DEVICE_ID],
        )
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assert_approve_request_order(first)
        self.assert_approve_request_order(second)


if __name__ == "__main__":
    unittest.main()
