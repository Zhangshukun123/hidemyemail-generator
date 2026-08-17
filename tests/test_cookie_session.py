import json
import time
import unittest

from curl_cffi.requests.cookies import Cookies

from hidemyemail_generator.cookie_session import (
    CHATGPT_SESSION_REFRESH_URL,
    CHATGPT_SESSION_URL,
    CurlCffiCookieSessionGateway,
    request_cookie_session,
)


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Session:
    instances = []
    response_tokens = []

    def __init__(self, **options):
        self.options = options
        self.cookies = Cookies()
        self.calls = []
        self.closed = False
        type(self).instances.append(self)

    def get(self, url, **options):
        loaded = [
            {
                "name": cookie.name,
                "domain": cookie.domain,
                "path": cookie.path,
                "expires": cookie.expires,
            }
            for cookie in self.cookies.jar
        ]
        self.calls.append((url, options, loaded))
        token = type(self).response_tokens[len(self.calls) - 1]
        self.cookies.set(
            "session",
            "refreshed" if len(self.calls) > 1 else "standard",
            domain="chatgpt.com",
            path="/",
            secure=True,
        )
        return _Response({"accessToken": token})

    def close(self):
        self.closed = True


class CookieSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _Session.instances.clear()
        _Session.response_tokens = []

    async def test_same_token_refreshes_in_same_cookie_jar_and_exports_updates(self):
        old_token = "old-access-token"
        new_token = "new-access-token"
        _Session.response_tokens = [old_token, new_token]
        future = int(time.time()) + 3600
        gateway = CurlCffiCookieSessionGateway(
            session_factory=_Session,
            timeout_seconds=17,
        )

        result = await request_cookie_session(
            cookies=[
                {
                    "name": "session",
                    "value": "saved",
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "expires": future,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                },
                {
                    "name": "path-cookie",
                    "value": "path-value",
                    "domain": "chatgpt.com",
                    "path": "/api",
                    "expires": future,
                },
                {
                    "name": "expired",
                    "value": "ignored",
                    "domain": "chatgpt.com",
                    "path": "/",
                    "expires": int(time.time()) - 1,
                },
                {
                    "name": "foreign",
                    "value": "ignored",
                    "domain": "example.com",
                    "path": "/",
                },
            ],
            previous_token=old_token,
            proxy_url="http://127.0.0.1:19002",
            storage_state={"origins": [{"origin": "https://chatgpt.com"}]},
            impersonate="chrome136",
            language="ja-JP",
            gateway=gateway,
        )

        client = _Session.instances[0]
        self.assertEqual(
            [call[0] for call in client.calls],
            [CHATGPT_SESSION_URL, CHATGPT_SESSION_REFRESH_URL],
        )
        self.assertEqual(
            {cookie["name"] for cookie in client.calls[0][2]},
            {"session", "path-cookie"},
        )
        path_cookie = next(
            cookie
            for cookie in client.calls[0][2]
            if cookie["name"] == "path-cookie"
        )
        self.assertEqual(path_cookie["path"], "/api")
        self.assertEqual(path_cookie["expires"], future)
        self.assertEqual(client.options["impersonate"], "chrome136")
        self.assertEqual(
            client.options["proxies"],
            {
                "http": "http://127.0.0.1:19002",
                "https": "http://127.0.0.1:19002",
            },
        )
        self.assertEqual(client.options["headers"]["Accept-Language"], "ja-JP,ja;q=0.9")
        self.assertEqual(client.options["headers"]["oai-language"], "ja-JP")
        self.assertNotIn("Cookie", client.options["headers"])
        self.assertTrue(all("headers" not in call[1] for call in client.calls))
        self.assertEqual(result["access_token"], new_token)
        cookies = json.loads(result["cookies_json"])
        self.assertEqual(
            next(cookie for cookie in cookies if cookie["name"] == "session")["value"],
            "refreshed",
        )
        self.assertEqual(
            json.loads(result["storage_state_json"])["origins"],
            [{"origin": "https://chatgpt.com"}],
        )
        self.assertTrue(client.closed)

    async def test_refresh_response_may_keep_the_same_access_token(self):
        token = "same-access-token"
        _Session.response_tokens = [token, token]
        gateway = CurlCffiCookieSessionGateway(session_factory=_Session)

        result = await request_cookie_session(
            cookies=[
                {
                    "name": "session",
                    "value": "saved",
                    "domain": "chatgpt.com",
                    "path": "/",
                }
            ],
            previous_token=token,
            gateway=gateway,
        )

        self.assertEqual(result["access_token"], token)
        self.assertEqual(len(_Session.instances[0].calls), 2)


if __name__ == "__main__":
    unittest.main()
