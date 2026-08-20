import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.browser_tasks import (
    _save_account_record,
    load_account_record,
)
from hidemyemail_generator.smsbower import SMSBowerConfigStore, SMSBowerMailClient
from hidemyemail_generator.webapp import create_app


class SMSBowerMailClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.requests = []
        self.code_attempts = 0
        self.code_error = ""
        self.status_error = ""

        async def get_activation(request):
            self.requests.append(("activation", dict(request.query)))
            return web.json_response(
                {"status": 1, "mail": "fresh.account@gmail.com", "mailId": 41}
            )

        async def get_code(request):
            self.requests.append(("code", dict(request.query)))
            if self.code_error:
                return web.json_response({"status": 0, "error": self.code_error})
            self.code_attempts += 1
            if self.code_attempts in {1, 3}:
                return web.json_response(
                    {
                        "status": 0,
                        "error": "Code has not been received yet, please try again later",
                    }
                )
            code = "123456" if self.code_attempts == 2 else "654321"
            return web.json_response({"status": 1, "code": code})

        async def set_status(request):
            self.requests.append(("status", dict(request.query)))
            if self.status_error:
                return web.json_response({"status": 0, "error": self.status_error})
            return web.json_response({"status": 1, "message": "Success"})

        api = web.Application()
        api.router.add_get("/api/mail/getActivation", get_activation)
        api.router.add_get("/api/mail/getCode", get_code)
        api.router.add_get("/api/mail/setStatus", set_status)
        self.server = TestServer(api)
        await self.server.start_server()

        self.store = SMSBowerConfigStore(
            Path(self.temp_dir.name) / "smsbower.db",
            api_key="test-api-key-123",
            service="dr",
            max_price=0.05,
        )
        self.client = SMSBowerMailClient(
            self.store, base_url=str(self.server.make_url("/")).rstrip("/")
        )

    async def asyncTearDown(self):
        await self.server.close()
        self.temp_dir.cleanup()

    async def test_gmail_activation_code_and_completion_lifecycle(self):
        email = await self.client.acquire_email("OpenAI")

        self.assertEqual(email, "fresh.account@gmail.com")
        self.assertEqual(await self.client.poll_code(email), "")
        self.assertEqual(await self.client.poll_code(email), "123456")
        await self.client.complete_email(email, True, "registered")

        activation_query = self.requests[0][1]
        self.assertEqual(activation_query["service"], "dr")
        self.assertEqual(activation_query["domain"], "gmail.com")
        self.assertEqual(activation_query["alias"], "0")
        self.assertNotIn("duration", activation_query)
        self.assertNotIn("hours", activation_query)
        self.assertEqual(self.requests[-1][1]["id"], "41")
        self.assertEqual(self.requests[-1][1]["status"], "5")
        state = self.client.public_state()
        self.assertEqual(state["active"], 1)
        self.assertEqual(state["expired"], 0)
        self.assertEqual(state["retentionHours"], 24)
        self.assertFalse(state["providerGuaranteesRetention"])

        restarted = SMSBowerMailClient(
            self.store, base_url=str(self.server.make_url("/")).rstrip("/")
        )
        self.assertEqual(restarted.public_state()["active"], 1)
        self.assertEqual(await restarted.poll_next_code(email), "")
        self.assertEqual(await restarted.poll_next_code(email), "654321")
        self.assertEqual(await restarted.poll_next_code(email), "")
        status_requests = [query for kind, query in self.requests if kind == "status"]
        self.assertEqual([item["status"] for item in status_requests], ["5", "5"])

    async def test_activation_is_retained_locally_for_at_most_24_hours(self):
        now = [datetime(2026, 8, 9, 1, 2, 3, tzinfo=timezone.utc)]
        client = SMSBowerMailClient(
            self.store,
            base_url=str(self.server.make_url("/")).rstrip("/"),
            clock=lambda: now[0],
        )
        email = await client.acquire_email("OpenAI")

        self.assertEqual(client.public_state()["active"], 1)
        now[0] += timedelta(hours=23, minutes=59)
        self.assertEqual(await client.poll_code(email), "")

        requests_before_expiry = len(self.requests)
        now[0] += timedelta(minutes=2)
        with self.assertRaisesRegex(RuntimeError, "24 小时保留期限"):
            await client.poll_next_code(email)
        self.assertEqual(len(self.requests), requests_before_expiry)

    async def test_explicit_cancellation_always_sends_status_two(self):
        email = await self.client.acquire_email("OpenAI")
        self.assertEqual(await self.client.poll_code(email), "")
        self.assertEqual(await self.client.poll_code(email), "123456")

        await self.client.cancel_email(email, "code timeout")

        status_requests = [query for kind, query in self.requests if kind == "status"]
        self.assertEqual(status_requests[-1]["status"], "2")
        state = self.client.public_state()
        self.assertEqual(state["active"], 0)
        self.assertEqual(state["expired"], 0)

        restarted = SMSBowerMailClient(
            self.store,
            base_url=str(self.server.make_url("/")).rstrip("/"),
        )
        requests_after_cancellation = len(self.requests)
        with self.assertRaisesRegex(RuntimeError, "未保存 SMSBower mailId"):
            await restarted.poll_next_code(email)
        self.assertEqual(restarted.public_state()["active"], 0)
        self.assertEqual(len(self.requests), requests_after_cancellation)

    async def test_provider_cancellation_marks_activation_expired_immediately(self):
        email = await self.client.acquire_email("OpenAI")
        self.code_error = "Activation is already canceled"

        with self.assertRaisesRegex(RuntimeError, "激活已失效"):
            await self.client.poll_code(email)
        self.assertEqual(self.client.public_state()["active"], 0)
        self.assertEqual(self.client.public_state()["expired"], 1)

        requests_after_cancellation = len(self.requests)
        with self.assertRaisesRegex(RuntimeError, "Activation is already canceled"):
            await self.client.poll_next_code(email)
        self.assertEqual(len(self.requests), requests_after_cancellation)

    async def test_old_gmail_without_persisted_mail_id_is_explicit(self):
        with self.assertRaisesRegex(RuntimeError, "未保存 SMSBower mailId"):
            await self.client.poll_next_code("old.record@gmail.com")

    async def test_api_key_is_not_exposed_in_public_state(self):
        state = self.client.public_state()

        self.assertTrue(state["configured"])
        self.assertNotIn("apiKey", state)
        self.assertNotIn("test-api-key-123", str(state))

    async def test_forget_email_removes_persisted_activation(self):
        email = await self.client.acquire_email("OpenAI")

        self.assertTrue(await self.client.forget_email(email.upper()))
        self.assertEqual(self.client.public_state()["active"], 0)
        self.assertFalse(await self.client.forget_email(email))

        restarted = SMSBowerMailClient(
            self.store, base_url=str(self.server.make_url("/")).rstrip("/")
        )
        self.assertEqual(restarted.public_state()["active"], 0)


class SMSBowerWebRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_gmail_delete_removes_local_account_without_icloud_lookup(self):
        class SMSBowerClientStub:
            def __init__(self):
                self.forgotten = []

            async def forget_email(self, email):
                self.forgotten.append(email)
                return True

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            smsbower = SMSBowerClientStub()
            app["smsbower_client"] = smsbower
            email = "saved.account@gmail.com"
            _save_account_record(
                app["db_file"],
                email,
                result={
                    "access_token": "gmail-access-token",
                    "session_json": '{"accessToken":"gmail-access-token"}',
                },
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/gpt-email/delete",
                    json={"email": email},
                    headers={"X-Local-Token": app["local_token"]},
                )
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(load_account_record(app["db_file"], email), {})

        self.assertEqual(response.status, 200)
        self.assertEqual(smsbower.forgotten, [email])
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["deleted"])
        self.assertFalse(payload["deactivated"])
        self.assertIn("Gmail", payload["message"])

    async def test_gmail_code_route_uses_smsbower_activation(self):
        class SMSBowerClientStub:
            def __init__(self):
                self.emails = []

            async def poll_next_code(self, email):
                self.emails.append(email)
                return "654321"

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            smsbower = SMSBowerClientStub()
            app["smsbower_client"] = smsbower
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/gpt-code",
                    json={"email": "fresh.account@gmail.com"},
                    headers={"X-Local-Token": app["local_token"]},
                )
                payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["code"], "654321")
        self.assertEqual(smsbower.emails, ["fresh.account@gmail.com"])

    async def test_gmail_code_route_reports_expired_activation_as_gone(self):
        class SMSBowerClientStub:
            async def poll_next_code(self, _email):
                raise RuntimeError("SMSBower Gmail 激活已超过本机 24 小时保留期限")

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            app["smsbower_client"] = SMSBowerClientStub()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/gpt-code",
                    json={"email": "expired.account@gmail.com"},
                    headers={"X-Local-Token": app["local_token"]},
                )
                payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 410)
        self.assertFalse(payload["ok"])
        self.assertIn("24 小时", payload["error"])

    async def test_config_and_provider_registration_routes(self):
        class RegistrationManagerStub:
            def __init__(self):
                self.starts = []

            def snapshot(self):
                return {"status": "idle", "running": False, "provider": "manual"}

            def start(self, **options):
                self.starts.append(options)
                return {
                    "status": "running",
                    "running": True,
                    "provider": options["provider"],
                    "requested": options["concurrency"],
                }

            async def stop(self):
                return self.snapshot()

            async def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            manager = RegistrationManagerStub()
            app["registration_manager"] = manager
            client = TestClient(TestServer(app))
            await client.start_server()
            headers = {"X-Local-Token": app["local_token"]}
            try:
                configured = await client.post(
                    "/api/smsbower/config",
                    json={
                        "apiKey": "route-test-key-123",
                        "service": "dr",
                        "maxPrice": 0.075,
                    },
                    headers=headers,
                )
                configured_payload = await configured.json()
                status = await client.get("/api/smsbower/status")
                status_payload = await status.json()
                started = await client.post(
                    "/api/registration/start",
                    json={"provider": "smsbower", "headless": False},
                    headers=headers,
                )
                inventory_started = await client.post(
                    "/api/registration/start",
                    json={"provider": "inventory", "headless": False},
                    headers=headers,
                )
            finally:
                await client.close()

        self.assertEqual(configured.status, 200)
        self.assertTrue(status_payload["configured"])
        self.assertEqual(status_payload["domain"], "gmail.com")
        self.assertEqual(status_payload["maxPrice"], 0.075)
        self.assertNotIn("apiKey", configured_payload)
        self.assertEqual(started.status, 200)
        self.assertEqual(manager.starts[0]["provider"], "smsbower")
        self.assertEqual(manager.starts[0]["email"], "")
        self.assertEqual(manager.starts[0]["concurrency"], 1)
        self.assertFalse(manager.starts[0]["headless"])
        self.assertEqual(inventory_started.status, 200)
        self.assertEqual(manager.starts[1]["provider"], "inventory")
        self.assertEqual(manager.starts[1]["email"], "")


if __name__ == "__main__":
    unittest.main()
