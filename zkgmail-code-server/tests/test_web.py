import time
import unittest

from aiohttp.test_utils import TestClient, TestServer

from tests.helpers import TEST_ACCESS_TOKEN, settings
from zkgmail_code_server.app import (
    ACCESS_COOKIE_NAME,
    INVITE_SERVICE_KEY,
    create_app,
)
from zkgmail_code_server.domain import CodeMessage
from zkgmail_code_server.invite import InviteTokenService


class RepositoryStub:
    def __init__(self, result=None):
        self.result = result
        self.requested = []

    async def latest_for(self, recipient):
        self.requested.append(recipient)
        return self.result


class PortalRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository = RepositoryStub(
            CodeMessage("246810", "2026-08-17T01:59:00+00:00", cursor="42")
        )
        self.app = create_app(repository=self.repository, settings=settings())
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.origin = str(self.client.make_url("/")).rstrip("/")

    async def asyncTearDown(self):
        await self.client.close()

    def _same_origin_headers(self, cookie: str = "") -> dict[str, str]:
        headers = {"Origin": self.origin}
        if cookie:
            headers["Cookie"] = f"{ACCESS_COOKIE_NAME}={cookie}"
        return headers

    async def _authorize(self, email: str = "alias@zkgmail.com") -> str:
        token = self.app[INVITE_SERVICE_KEY].issue(email, ttl_seconds=600)
        response = await self.client.post(
            "/api/access",
            json={"token": token},
            headers=self._same_origin_headers(),
        )
        payload = await response.json()
        self.assertEqual(response.status, 200, payload)
        self.assertEqual(payload["email"], email)
        return response.cookies[ACCESS_COOKIE_NAME].value

    async def test_page_identifies_zkgmail_and_loads_same_origin_assets(self):
        response = await self.client.get("/")
        page = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("ZKG Mail", page)
        self.assertIn("yourname@zkgmail.com", page)
        self.assertNotIn("邮件转发到 QQ 收件箱后", page)
        self.assertNotIn("生成新地址", page)
        self.assertNotIn("复制邮箱", page)
        self.assertNotIn("完整地址就是查询凭证", page)
        self.assertNotIn('class="steps"', page)
        self.assertNotIn("QQ 邮箱收件", page)
        self.assertNotIn("重复接码", page)
        self.assertIn("/assets/app.js?v=1.3.3", page)
        script = await (await self.client.get("/assets/app.js")).text()
        self.assertIn("/api/code/latest", script)
        self.assertIn("timeoutMs: 180 * 1000", script)
        self.assertIn('timeoutLabel: "3 分钟"', script)
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])

    async def test_lookup_returns_non_consuming_code_payload(self):
        cookie = await self._authorize()
        response = await self.client.post(
            "/api/code/latest",
            json={"email": " Alias@ZKGMAIL.COM "},
            headers=self._same_origin_headers(cookie),
        )
        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["code"], "246810")
        self.assertEqual(payload["email"], "alias@zkgmail.com")
        self.assertEqual(payload["receivedAt"], "2026-08-17T01:59:00+00:00")
        self.assertEqual(payload["cursor"], "42")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.repository.requested, ["alias@zkgmail.com"])

    async def test_after_cursor_waits_for_a_second_message(self):
        cookie = await self._authorize()
        response = await self.client.post(
            "/api/code/latest",
            json={"email": "alias@zkgmail.com", "afterCursor": "42"},
            headers=self._same_origin_headers(cookie),
        )
        payload = await response.json()
        self.assertEqual(response.status, 404)
        self.assertEqual(payload["state"], "waiting")
        self.assertIn("新的验证码", payload["message"])

    async def test_lookup_rejects_other_domain(self):
        cookie = await self._authorize()
        response = await self.client.post(
            "/api/code/latest",
            json={"email": "alias@icloud.com"},
            headers=self._same_origin_headers(cookie),
        )
        payload = await response.json()
        self.assertEqual(response.status, 400)
        self.assertFalse(payload["ok"])
        self.assertEqual(self.repository.requested, [])

    async def test_admin_style_paths_are_not_exposed(self):
        for path in ("/api/zkgmail/config", "/api/inbox/status", "/login"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 404)

    async def test_health_discloses_only_service_state(self):
        response = await self.client.get("/healthz")
        payload = await response.json()
        self.assertTrue(payload["ok"])
        self.assertNotIn("password", str(payload).lower())

    async def test_valid_invite_sets_scoped_hardened_cookie(self):
        token = self.app[INVITE_SERVICE_KEY].issue(
            "alias@zkgmail.com",
            ttl_seconds=600,
        )
        response = await self.client.post(
            "/api/access",
            json={"token": token},
            headers=self._same_origin_headers(),
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["email"], "alias@zkgmail.com")
        set_cookie = response.headers["Set-Cookie"]
        self.assertIn(f"{ACCESS_COOKIE_NAME}=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("Secure", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertIn("Path=/", set_cookie)

    async def test_expired_and_tampered_invites_never_set_a_session_cookie(self):
        expired_issuer = InviteTokenService(
            TEST_ACCESS_TOKEN,
            clock=lambda: time.time() - 601,
        )
        expired = expired_issuer.issue(
            "alias@zkgmail.com",
            ttl_seconds=300,
        )
        valid = self.app[INVITE_SERVICE_KEY].issue(
            "alias@zkgmail.com",
            ttl_seconds=600,
        )
        encoded, signature = valid.rsplit(".", 1)
        replacement = "0" if signature[-1] != "0" else "1"
        tampered = f"{encoded}.{signature[:-1]}{replacement}"

        for case, token in (("expired", expired), ("tampered", tampered)):
            with self.subTest(case=case):
                response = await self.client.post(
                    "/api/access",
                    json={"token": token},
                    headers=self._same_origin_headers(),
                )
                payload = await response.json()
                self.assertEqual(response.status, 403)
                self.assertEqual(payload["state"], "unauthorized")
                self.assertNotIn(ACCESS_COOKIE_NAME, response.cookies)

    async def test_invite_for_email_a_cannot_query_email_b(self):
        cookie = await self._authorize("a@zkgmail.com")

        response = await self.client.post(
            "/api/code/latest",
            json={"email": "b@zkgmail.com"},
            headers=self._same_origin_headers(cookie),
        )
        payload = await response.json()

        self.assertEqual(response.status, 403)
        self.assertEqual(payload["state"], "forbidden")
        self.assertEqual(self.repository.requested, [])

    async def test_cross_origin_and_null_origin_are_rejected_before_lookup(self):
        cookie = await self._authorize()
        token = self.app[INVITE_SERVICE_KEY].issue(
            "alias@zkgmail.com",
            ttl_seconds=600,
        )

        for origin in ("https://attacker.example", "null"):
            for path, body, request_cookie in (
                ("/api/access", {"token": token}, ""),
                ("/api/code/latest", {"email": "alias@zkgmail.com"}, cookie),
            ):
                with self.subTest(origin=origin, path=path):
                    headers = {"Origin": origin}
                    if request_cookie:
                        headers["Cookie"] = (
                            f"{ACCESS_COOKIE_NAME}={request_cookie}"
                        )
                    response = await self.client.post(
                        path,
                        json=body,
                        headers=headers,
                    )
                    self.assertEqual(response.status, 403)

        self.assertEqual(self.repository.requested, [])

    async def test_wrong_content_type_is_rejected_before_auth_or_lookup(self):
        cookie = await self._authorize()
        token = self.app[INVITE_SERVICE_KEY].issue(
            "alias@zkgmail.com",
            ttl_seconds=600,
        )

        for path, body, request_cookie in (
            ("/api/access", '{"token":"%s"}' % token, ""),
            (
                "/api/code/latest",
                '{"email":"alias@zkgmail.com"}',
                cookie,
            ),
        ):
            with self.subTest(path=path):
                headers = self._same_origin_headers(request_cookie)
                headers["Content-Type"] = "text/plain"
                response = await self.client.post(
                    path,
                    data=body,
                    headers=headers,
                )
                self.assertEqual(response.status, 403)

        self.assertEqual(self.repository.requested, [])

    async def test_missing_and_forged_cookies_never_reach_repository(self):
        for cookie in ("", "forged-session-value"):
            with self.subTest(cookie=bool(cookie)):
                response = await self.client.post(
                    "/api/code/latest",
                    json={"email": "alias@zkgmail.com"},
                    headers=self._same_origin_headers(cookie),
                )
                payload = await response.json()
                self.assertEqual(response.status, 401)
                self.assertEqual(payload["state"], "unauthorized")

        self.assertEqual(self.repository.requested, [])

    async def test_explicit_public_compatibility_mode_allows_direct_lookup(self):
        public_app = create_app(
            repository=self.repository,
            settings=settings(access_token="", require_invite=False),
        )
        public_client = TestClient(TestServer(public_app))
        await public_client.start_server()
        try:
            public_origin = str(public_client.make_url("/")).rstrip("/")
            response = await public_client.post(
                "/api/code/latest",
                json={"email": "public@zkgmail.com"},
                headers={"Origin": public_origin},
            )
            payload = await response.json()

            self.assertEqual(response.status, 200, payload)
            self.assertEqual(payload["code"], "246810")
            self.assertEqual(self.repository.requested, ["public@zkgmail.com"])
        finally:
            await public_client.close()
