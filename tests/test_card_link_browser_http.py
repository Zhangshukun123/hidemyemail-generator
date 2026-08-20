from __future__ import annotations

import unittest

from hidemyemail_generator import card_link_runtime


class _FakeBrowserContext:
    def __init__(self):
        self.added_cookies = []

    def add_cookies(self, cookies):
        self.added_cookies.extend(cookies)


class _FakePage:
    def __init__(self, result=None, *, closed=False):
        self.context = _FakeBrowserContext()
        self.result = result or {
            "status": 200,
            "text": '{"ok":true}',
            "headers": {"content-type": "application/json"},
            "url": "https://chatgpt.com/backend-api/payments/checkout/fixture",
        }
        self.closed = closed
        self.evaluations = []

    def is_closed(self):
        return self.closed

    def evaluate(self, script, payload):
        self.evaluations.append((script, payload))
        return self.result


class CardLinkBrowserHttpTests(unittest.TestCase):
    proxy_url = "http://gb-sticky.example:8000"

    def test_cookie_jar_and_device_identity_are_synced_into_page_context(self):
        page = _FakePage()
        browser_http = card_link_runtime.OpllBrowserFetchSession(
            page,
            label="GB Checkout",
        )
        session = card_link_runtime.opll_build_chatgpt_session(
            "access-token-fixture",
            self.proxy_url,
            request_locale="en-GB",
            session=browser_http,
            session_context={
                "device_id": "device-gb-fixture",
                "storage_state": {
                    "cookies": [
                        {
                            "name": "__Secure-next-auth.session-token",
                            "value": "session-cookie-fixture",
                            "domain": ".chatgpt.com",
                        },
                        {
                            "name": "oai-sc",
                            "value": "sentinel-cookie-fixture",
                            "domain": "chatgpt.com",
                        },
                    ]
                },
            },
        )

        response = session.post(
            "https://chatgpt.com/backend-api/payments/checkout/fixture",
            json={"promotion": "trial"},
            timeout=(5, 9),
            allow_redirects=False,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        synced = {
            (item["name"], item["value"])
            for item in page.context.added_cookies
        }
        self.assertIn(
            (
                "__Secure-next-auth.session-token",
                "session-cookie-fixture",
            ),
            synced,
        )
        self.assertIn(("oai-sc", "sentinel-cookie-fixture"), synced)
        self.assertIn(("oai-did", "device-gb-fixture"), synced)
        self.assertEqual(
            session.proxies,
            {"http": self.proxy_url, "https": self.proxy_url},
        )
        self.assertEqual(session.opll_oai_device_id, "device-gb-fixture")

        request_payload = page.evaluations[0][1]
        normalized_headers = {
            key.lower(): value
            for key, value in request_payload["headers"].items()
        }
        self.assertEqual(
            normalized_headers["authorization"],
            "Bearer access-token-fixture",
        )
        self.assertEqual(
            normalized_headers["oai-device-id"],
            "device-gb-fixture",
        )
        self.assertEqual(
            normalized_headers["content-type"],
            "application/json",
        )
        for browser_owned_header in (
            "cookie",
            "origin",
            "referer",
            "user-agent",
            "accept-language",
        ):
            self.assertNotIn(browser_owned_header, normalized_headers)
        self.assertEqual(request_payload["timeoutMs"], 9000)
        self.assertEqual(request_payload["redirect"], "manual")

    def test_fetch_error_and_closed_page_are_hard_failures(self):
        page = _FakePage({"fetchError": "TypeError: Failed to fetch"})
        session = card_link_runtime.OpllBrowserFetchSession(
            page,
            label="GB Checkout",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "GB Checkout 页面内 fetch 失败: TypeError: Failed to fetch",
        ):
            session.get("https://chatgpt.com/backend-api/sentinel/ping")

        closed = card_link_runtime.OpllBrowserFetchSession(
            _FakePage(closed=True),
            label="GB Checkout",
        )
        with self.assertRaisesRegex(RuntimeError, "GB Checkout 页面已关闭"):
            closed.get("https://chatgpt.com/backend-api/sentinel/ping")

    def test_bound_browser_http_rejects_proxy_drift(self):
        session = card_link_runtime.OpllBrowserFetchSession(
            _FakePage(),
            label="GB Checkout",
            proxy_url=self.proxy_url,
        )

        with self.assertRaisesRegex(RuntimeError, "禁止从已绑定代理切换"):
            card_link_runtime.opll_build_chatgpt_session(
                "access-token-fixture",
                "http://different-gb-sticky.example:8000",
                request_locale="en-GB",
                session=session,
                device_id="device-gb-fixture",
            )

    def test_pool2_update_copies_only_durable_chatgpt_identity(self):
        source = card_link_runtime.opll_new_http_session()
        target = card_link_runtime.opll_new_http_session()
        source.cookies.set(
            "__Secure-next-auth.session-token",
            "session-fixture",
            domain="chatgpt.com",
            path="/",
            secure=True,
        )
        source.cookies.set(
            "oai-did",
            "device-fixture",
            domain="chatgpt.com",
            path="/",
        )
        source.cookies.set(
            "cf_clearance",
            "edge-cookie-fixture",
            domain="chatgpt.com",
            path="/",
        )

        copied = card_link_runtime.opll_copy_chatgpt_identity_cookies(
            source,
            target,
        )

        self.assertEqual(
            copied,
            ("__Secure-next-auth.session-token", "oai-did"),
        )
        self.assertEqual(
            target.cookies.get("__Secure-next-auth.session-token"),
            "session-fixture",
        )
        self.assertEqual(target.cookies.get("oai-did"), "device-fixture")
        self.assertIsNone(target.cookies.get("cf_clearance"))


if __name__ == "__main__":
    unittest.main()
