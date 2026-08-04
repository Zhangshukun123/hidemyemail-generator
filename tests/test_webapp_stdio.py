import asyncio
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer
from rich.console import Console

from hidemyemail_generator.browser_tasks import _save_account_record
from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.webapp import (
    WORKBENCH_OPENAI_CODE_PATH,
    _configured_workbench_import_token,
    _configure_utf8_stdio,
    _generation_failure_message,
    _latest_code_for_email,
    _load_local_env_file,
    create_app,
)


class WebAppStdioTests(unittest.TestCase):
    def test_workbench_import_uses_the_token_checked_by_workbench(self):
        with mock.patch.dict(
            os.environ,
            {
                "HME_IMPORT_TOKEN": "canonical-workbench-token",
                "ACCOUNT_WORKBENCH_IMPORT_TOKEN": "stale-client-token",
            },
            clear=False,
        ):
            self.assertEqual(
                _configured_workbench_import_token(), "canonical-workbench-token"
            )

    def test_workbench_import_token_keeps_legacy_alias_fallback(self):
        with mock.patch.dict(
            os.environ,
            {"ACCOUNT_WORKBENCH_IMPORT_TOKEN": "legacy-client-token"},
            clear=False,
        ):
            os.environ.pop("HME_IMPORT_TOKEN", None)
            self.assertEqual(
                _configured_workbench_import_token(), "legacy-client-token"
            )

    def test_loads_local_workbench_settings_without_overriding_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "ACCOUNT_WORKBENCH_URL=http://127.0.0.1:3000\n"
                "ACCOUNT_WORKBENCH_IMPORT_TOKEN=local-token\n"
                "UNRELATED=value\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"ACCOUNT_WORKBENCH_URL": "http://existing:3000"},
                clear=False,
            ):
                os.environ.pop("ACCOUNT_WORKBENCH_IMPORT_TOKEN", None)
                os.environ.pop("UNRELATED", None)
                _load_local_env_file(env_file)

                self.assertEqual(
                    os.environ["ACCOUNT_WORKBENCH_URL"], "http://existing:3000"
                )
                self.assertEqual(
                    os.environ["ACCOUNT_WORKBENCH_IMPORT_TOKEN"], "local-token"
                )
                self.assertNotIn("UNRELATED", os.environ)

    def test_generation_error_preserves_icloud_detail(self):
        message = _generation_failure_message(
            {
                "error": {
                    "code": "HME_RESERVE_FAILED",
                    "message": "Unable to reserve generated address",
                    "retry_after": 12,
                }
            }
        )

        self.assertIn("Unable to reserve generated address", message)
        self.assertIn("HME_RESERVE_FAILED", message)
        self.assertIn("12 秒后重试", message)

    def test_reconfigures_gbk_streams_before_rich_writes_unicode(self):
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        try:
            sys.stdout = stdout
            sys.stderr = stderr
            _configure_utf8_stdio()

            self.assertEqual(stdout.encoding.lower(), "utf-8")
            self.assertEqual(stderr.encoding.lower(), "utf-8")
            Console(file=stdout, force_terminal=False).print(":star:")
            stdout.flush()
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr

    def test_default_openai_runtime_uses_current_sibling_checkout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir) / "hidemyemail-generator"
            app = create_app(base_dir=base_dir)

            self.assertEqual(
                app["browser_manager"].target_project_dir,
                (base_dir.parent / "openai-register-paylink").resolve(),
            )

    def test_default_openai_runtime_falls_back_to_packaged_sibling(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir) / "hidemyemail-generator"
            packaged = (
                base_dir.parent
                / "openai-register-paylink-ui-dist-20260706-README-deploy"
            )
            packaged.mkdir()
            (packaged / "app_backend.py").write_text(
                "# packaged runtime\n", encoding="utf-8"
            )

            app = create_app(base_dir=base_dir)

            self.assertEqual(
                app["browser_manager"].target_project_dir,
                packaged.resolve(),
            )


class WorkbenchOpenAICodeEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        app = create_app(
            base_dir=Path(self.temp_dir.name),
            web_password="web-password",
            workbench_import_token="shared-workbench-token",
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_shared_token_bypasses_web_login_but_still_validates_email(self):
        response = await self.client.post(
            WORKBENCH_OPENAI_CODE_PATH,
            json={"email": "not-an-icloud-address"},
            headers={"X-HME-Import-Token": "shared-workbench-token"},
        )

        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"], "邮箱地址无效")

    async def test_missing_shared_token_cannot_use_workbench_endpoint(self):
        response = await self.client.post(
            WORKBENCH_OPENAI_CODE_PATH,
            json={"email": "one@icloud.com"},
        )

        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())["error"], "请先登录")


class CodePortalTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_link_opens_alias_only_portal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(
                base_dir=Path(temp_dir),
                web_password="private-token",
                workbench_import_token="private-token",
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                blocked = await client.get("/code", allow_redirects=False)
                invalid = await client.get(
                    "/access?token=wrong", allow_redirects=False
                )
                granted = await client.get(
                    "/access?token=private-token", allow_redirects=False
                )
                session = granted.cookies["hme_session"].value
                page = await client.get(
                    "/code", headers={"Cookie": f"hme_session={session}"}
                )
                html = await page.text()
            finally:
                await client.close()

        self.assertEqual(blocked.status, 302)
        self.assertEqual(invalid.status, 404)
        self.assertEqual(granted.status, 302)
        self.assertEqual(granted.headers["Location"], "/code")
        self.assertEqual(page.status, 200)
        self.assertIn("输入“隐藏我的邮箱”子邮箱", html)
        self.assertIn("/api/code/latest", html)
        self.assertNotIn("password", html.lower())

    async def test_concurrent_alias_lookups_remain_isolated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hidemyemail.db"
            conn = connect_db(str(db_file))
            try:
                messages = [
                    ("one@icloud.com", "111111", "2026-08-04T01:00:00+00:00", "u1"),
                    ("two@icloud.com", "222222", "2026-08-04T02:00:00+00:00", "u2"),
                    ("one@icloud.com", "333333", "2026-08-04T03:00:00+00:00", "u3"),
                ]
                for address, code, received_at, uid in messages:
                    conn.execute(
                        """
                        INSERT INTO messages(
                            account_key, folder, uid, sender, hme_address,
                            subject, code, body_preview, received_at, created_at
                        ) VALUES (?, 'INBOX', ?, 'sender@example.com', ?,
                                  'Verification code', ?, '', ?, ?)
                        """,
                        ("icloud", uid, address, code, received_at, received_at),
                    )
                conn.commit()
            finally:
                conn.close()
            identities = [
                {"hme": "one@icloud.com", "anonymousId": "one"},
                {"hme": "two@icloud.com", "anonymousId": "two"},
            ]
            first, second = await asyncio.gather(
                asyncio.to_thread(
                    _latest_code_for_email,
                    db_file,
                    "one@icloud.com",
                    identities,
                ),
                asyncio.to_thread(
                    _latest_code_for_email,
                    db_file,
                    "two@icloud.com",
                    identities,
                ),
            )

        self.assertEqual(first["code"], "333333")
        self.assertEqual(second["code"], "222222")


class VerifyAccountEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_password_reset_never_enables_two_factor(self):
        class BrowserManagerStub:
            def __init__(self):
                self.starts = []

            def snapshot(self):
                return {"running": False}

            def start(self, accounts, **options):
                self.starts.append({"accounts": accounts, **options})
                return {"running": True, "accounts": accounts}

            async def close(self):
                return None

        class FakeHideMyEmail:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def list_email(self):
                return {
                    "success": True,
                    "result": {
                        "hmeEmails": [
                            {
                                "hme": "reset@icloud.com",
                                "anonymousId": "reset-id",
                                "isActive": True,
                            }
                        ]
                    },
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            (base_dir / "cookies.txt").write_text("cookie", encoding="utf-8")
            (base_dir / "inbox_config.json").write_text("{}\n", encoding="utf-8")
            app = create_app(base_dir=base_dir)
            _save_account_record(
                app["db_file"],
                "reset@icloud.com",
                password="Existing!Password123",
                password_confirmed=True,
                result={"access_token": "existing-token"},
            )
            browser_manager = BrowserManagerStub()
            app["browser_manager"] = browser_manager
            client = TestClient(TestServer(app))
            with mock.patch(
                "hidemyemail_generator.webapp.RichHideMyEmail", FakeHideMyEmail
            ):
                await client.start_server()
                try:
                    response = await client.post(
                        "/api/account/verify-or-register",
                        json={
                            "email": "reset@icloud.com",
                            "headless": True,
                            "reset_password": True,
                        },
                        headers={"X-Local-Token": app["local_token"]},
                    )
                    payload = await response.json()
                finally:
                    await client.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["mode"], "set_password")
        account = browser_manager.starts[0]["accounts"][0]
        self.assertTrue(account["ensure_password"])
        self.assertFalse(account["enable_2fa"])

    async def test_browser_endpoint_rejects_two_factor_activation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/browser/fetch-selected",
                    json={
                        "emails": ["one@icloud.com"],
                        "concurrency": 1,
                        "enable_2fa": True,
                    },
                    headers={"X-Local-Token": app["local_token"]},
                )
                payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 400)
        self.assertEqual(payload["error"], "已停用新账号的 2FA 设置")

    async def test_bulk_verification_uses_headless_browser_batch(self):
        class VerificationManagerStub:
            def __init__(self):
                self.starts = []

            def snapshot(self):
                return {"running": False}

            def start_with_browser(self, *, emails, concurrency):
                self.starts.append({"emails": emails, "concurrency": concurrency})
                return {
                    "running": True,
                    "headless": True,
                    "concurrency": concurrency,
                    "accounts": [{"email": email} for email in emails],
                }

            async def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            manager = VerificationManagerStub()
            app["verification_manager"] = manager
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/account-verification/start",
                    json={
                        "concurrency": 4,
                        "emails": ["ONE@icloud.com", "two@icloud.com"],
                    },
                    headers={"X-Local-Token": app["local_token"]},
                )
                payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["task"]["headless"])
        self.assertEqual(
            manager.starts,
            [
                {
                    "emails": ["one@icloud.com", "two@icloud.com"],
                    "concurrency": 4,
                }
            ],
        )

    async def test_verify_reuses_valid_session_or_relogs_when_missing(self):
        class ManagerStub:
            def __init__(self, *, allow_protocol=False, allow_verify=False):
                self.allow_protocol = allow_protocol
                self.allow_verify = allow_verify
                self.protocol_emails = []
                self.browser_refresh_starts = []
                self.verify_starts = []
                self.browser_starts = 0

            def snapshot(self):
                return {"running": False}

            def start_protocol_relogin(self, *, email, headless=False):
                if not self.allow_protocol:
                    raise AssertionError("protocol relogin called on wrong manager")
                self.protocol_emails.append(email)
                return {"running": True, "accounts": [{"email": email}]}

            def start_with_browser(self, *, emails, concurrency):
                if not self.allow_protocol:
                    raise AssertionError("browser refresh called on wrong manager")
                self.browser_refresh_starts.append(
                    {"emails": emails, "concurrency": concurrency}
                )
                return {"running": True, "accounts": [{"email": emails[0]}]}

            def start(self, *_args, **kwargs):
                if self.allow_verify:
                    self.verify_starts.append(kwargs)
                    return {"running": True, "accounts": []}
                self.browser_starts += 1
                raise AssertionError("browser must not start during account verification")

            async def close(self):
                return None

        class FakeHideMyEmail:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def list_email(self):
                return {
                    "success": True,
                    "result": {
                        "hmeEmails": [
                            {
                                "hme": "protocol@icloud.com",
                                "anonymousId": "protocol-id",
                                "isActive": True,
                            }
                        ]
                    },
                }

        async def run_case(*, has_valid_session):
            with tempfile.TemporaryDirectory() as temp_dir:
                base_dir = Path(temp_dir)
                (base_dir / "cookies.txt").write_text("cookie", encoding="utf-8")
                (base_dir / "inbox_config.json").write_text("{}\n", encoding="utf-8")
                app = create_app(base_dir=base_dir)
                if has_valid_session:
                    _save_account_record(
                        app["db_file"],
                        "protocol@icloud.com",
                        result={
                            "access_token": "valid-session-token",
                            "session_json": '{"accessToken":"valid-session-token"}',
                        },
                    )
                verification_manager = ManagerStub(
                    allow_protocol=True,
                    allow_verify=True,
                )
                browser_manager = ManagerStub()
                app["verification_manager"] = verification_manager
                app["browser_manager"] = browser_manager
                client = TestClient(TestServer(app))
                with (
                    mock.patch(
                        "hidemyemail_generator.webapp.RichHideMyEmail",
                        FakeHideMyEmail,
                    ),
                    mock.patch(
                        "hidemyemail_generator.webapp.access_token_is_expired",
                        return_value=False,
                    ),
                ):
                    await client.start_server()
                    try:
                        response = await client.post(
                            "/api/account/verify-or-register",
                            json={
                                "email": "protocol@icloud.com",
                                "headless": False,
                                "reset_password": False,
                            },
                            headers={"X-Local-Token": app["local_token"]},
                        )
                        payload = await response.json()
                    finally:
                        await client.close()
                return response, payload, verification_manager, browser_manager

        response, payload, verification_manager, browser_manager = await run_case(
            has_valid_session=True
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["mode"], "verify")
        self.assertEqual(
            verification_manager.verify_starts,
            [{"concurrency": 1, "emails": ["protocol@icloud.com"]}],
        )
        self.assertEqual(verification_manager.protocol_emails, [])
        self.assertEqual(verification_manager.browser_refresh_starts, [])
        self.assertEqual(browser_manager.browser_starts, 0)

        response, payload, verification_manager, browser_manager = await run_case(
            has_valid_session=False
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["mode"], "refresh_session")
        self.assertEqual(verification_manager.protocol_emails, [])
        self.assertEqual(
            verification_manager.browser_refresh_starts,
            [{"emails": ["protocol@icloud.com"], "concurrency": 1}],
        )
        self.assertEqual(verification_manager.verify_starts, [])
        self.assertEqual(browser_manager.browser_starts, 0)


if __name__ == "__main__":
    unittest.main()
