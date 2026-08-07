import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer
from rich.console import Console

from hidemyemail_generator.account_verifier import mark_account_session_invalid
from hidemyemail_generator.browser_tasks import (
    _save_account_record,
    load_account_record,
)
from hidemyemail_generator.inbox import InboxConfig, connect_db, save_config
from hidemyemail_generator.webapp import (
    WORKBENCH_OPENAI_CODE_PATH,
    _configured_inventory_service_token,
    _configured_workbench_import_token,
    _configure_utf8_stdio,
    _generation_failure_message,
    _latest_code_for_email,
    _load_local_env_file,
    create_app,
)


class WebAppStdioTests(unittest.TestCase):
    def test_workbench_import_uses_dedicated_local_token(self):
        with mock.patch.dict(
            os.environ,
            {
                "HME_IMPORT_TOKEN": "unrelated-remote-token",
                "ACCOUNT_WORKBENCH_IMPORT_TOKEN": "local-workbench-token",
            },
            clear=False,
        ):
            self.assertEqual(
                _configured_workbench_import_token(), "local-workbench-token"
            )

    def test_workbench_import_does_not_fall_back_to_remote_token(self):
        with mock.patch.dict(
            os.environ,
            {"HME_IMPORT_TOKEN": "unrelated-remote-token"},
            clear=False,
        ):
            os.environ.pop("ACCOUNT_WORKBENCH_IMPORT_TOKEN", None)
            self.assertEqual(_configured_workbench_import_token(), "")

    def test_inventory_does_not_fall_back_to_local_workbench_token(self):
        with mock.patch.dict(
            os.environ,
            {"ACCOUNT_WORKBENCH_IMPORT_TOKEN": "unrelated-local-token"},
            clear=False,
        ):
            os.environ.pop("HIDEMYEMAIL_INVENTORY_TOKEN", None)
            self.assertEqual(_configured_inventory_service_token(), "")

    def test_inventory_uses_its_dedicated_remote_token(self):
        with mock.patch.dict(
            os.environ,
            {"HIDEMYEMAIL_INVENTORY_TOKEN": "inventory-token"},
            clear=False,
        ):
            self.assertEqual(
                _configured_inventory_service_token(), "inventory-token"
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

            self.assertNotIn("inbox_sync_interval", app)
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


class CardLinkEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        target = root / "openai-runtime"
        target.mkdir()
        (target / "app_backend.py").write_text("# fake runtime\n", encoding="utf-8")
        self.app = create_app(
            base_dir=root,
            target_project_dir=str(target),
            target_python=sys.executable,
        )
        _save_account_record(
            self.app["db_file"],
            "card-link@icloud.com",
            result={
                "access_token": "at-card-link",
                "session_json": '{"accessToken":"at-card-link"}',
            },
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_generates_and_persists_card_link(self):
        generated = {
            "status": "success",
            "url": "https://chatgpt.com/checkout/openai_llc/cs_test_endpoint",
            "country": "JP",
            "currency": "JPY",
        }
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(return_value=generated),
            ) as bridge,
        ):
            response = await self.client.post(
                "/api/account/card-link",
                json={"email": "card-link@icloud.com", "country": "JP"},
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["currency"], "JPY")
        self.assertEqual(
            load_account_record(
                self.app["db_file"], "card-link@icloud.com"
            )["card_link"]["url"],
            generated["url"],
        )
        self.assertEqual(bridge.await_args.kwargs["locale"], "ja-JP")

    async def test_rejects_unsupported_card_region(self):
        response = await self.client.post(
            "/api/account/card-link",
            json={"email": "card-link@icloud.com", "country": "ZZ"},
            headers={"X-Local-Token": self.app["local_token"]},
        )

        self.assertEqual(response.status, 400)
        self.assertIn("不支持", (await response.json())["error"])

    async def test_generates_ph_hosted_strict_zero_link_with_two_proxies(self):
        generated = {
            "status": "success",
            "url": "https://chatgpt.com/checkout/openai_ie/oaics_test_ph_hosted",
            "method": "ph_hosted",
            "country": "PH",
            "currency": "PHP",
            "payment_link_type": "chatgpt_checkout_short",
            "checkout_ui_mode": "hosted",
            "amount": "0",
            "amount_currency": "PHP",
            "amount_verification": "checkout_update",
            "promotion_applied": True,
            "promotion_strategy": "gpt_link_hosted_create_and_update",
        }
        with (
            mock.patch(
                "hidemyemail_generator.webapp.access_token_is_expired",
                return_value=False,
            ),
            mock.patch(
                "hidemyemail_generator.webapp._run_card_link_bridge",
                new=mock.AsyncMock(return_value=generated),
            ) as bridge,
        ):
            response = await self.client.post(
                "/api/account/card-link",
                json={
                    "email": "card-link@icloud.com",
                    "method": "ph_hosted",
                    "create_proxy": "create.example:8000:user:pass",
                    "promotion_proxy": "socks5://promo.example:9000",
                },
                headers={"X-Local-Token": self.app["local_token"]},
            )

        payload = await response.json()
        saved = load_account_record(
            self.app["db_file"], "card-link@icloud.com"
        )["card_link"]
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["method"], "ph_hosted")
        self.assertEqual(saved["amount"], "0")
        self.assertEqual(saved["checkout_ui_mode"], "hosted")
        self.assertNotIn("proxy", saved)
        self.assertEqual(bridge.await_args.kwargs["country"], "PH")
        self.assertEqual(bridge.await_args.kwargs["currency"], "PHP")
        self.assertEqual(
            bridge.await_args.kwargs["create_proxy_url"],
            "http://user:pass@create.example:8000",
        )
        self.assertEqual(
            bridge.await_args.kwargs["promotion_proxy_url"],
            "socks5://promo.example:9000",
        )

    async def test_rejects_invalid_card_link_proxy(self):
        response = await self.client.post(
            "/api/account/card-link",
            json={
                "email": "card-link@icloud.com",
                "method": "ph_hosted",
                "create_proxy": "file:///tmp/not-a-proxy",
            },
            headers={"X-Local-Token": self.app["local_token"]},
        )

        self.assertEqual(response.status, 400)
        self.assertIn("代理", (await response.json())["error"])


class CodePortalTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def fake_hide_my_email():
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
                                "hme": "one@icloud.com",
                                "anonymousId": "one",
                                "isActive": True,
                            }
                        ]
                    },
                }

        return FakeHideMyEmail

    async def test_account_list_uses_local_records_when_icloud_session_is_invalid(self):
        class InvalidSessionHideMyEmail:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def list_email(self):
                return {"success": False, "error": "Invalid global session"}

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            app["cookie_file"].write_text("fake-cookie", encoding="utf-8")
            conn = connect_db(str(app["db_file"]))
            try:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    (
                        "gpt_account:352121354@qq.com",
                        json.dumps(
                            {
                                "password": "Manual!Password123",
                                "password_confirmed": True,
                            }
                        ),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                with mock.patch(
                    "hidemyemail_generator.webapp.RichHideMyEmail",
                    InvalidSessionHideMyEmail,
                ):
                    response = await client.get("/api/gpt-emails")
                    payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["items"][0]["email"], "352121354@qq.com")
        self.assertEqual(payload["identityWarning"], "Invalid global session")

    async def test_public_alias_only_portal_keeps_admin_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(
                base_dir=Path(temp_dir),
                web_password="private-token",
                workbench_import_token="private-token",
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                page = await client.get("/code", allow_redirects=False)
                html = await page.text()
                invalid_lookup = await client.post(
                    "/api/code/latest", json={"email": "invalid"}
                )
                private_admin = await client.get(
                    "/api/gpt-emails", allow_redirects=False
                )
                invalid = await client.get(
                    "/access?token=wrong", allow_redirects=False
                )
                granted = await client.get(
                    "/access?token=private-token", allow_redirects=False
                )
            finally:
                await client.close()

        self.assertEqual(page.status, 200)
        self.assertEqual(invalid_lookup.status, 400)
        self.assertEqual(private_admin.status, 401)
        self.assertEqual(invalid.status, 404)
        self.assertEqual(granted.status, 302)
        self.assertEqual(granted.headers["Location"], "/code")
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

    async def test_code_lookup_syncs_only_on_demand_and_shares_cooldown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "cookies.txt").write_text("cookie", encoding="utf-8")
            save_config(
                InboxConfig(
                    host="imap.example.com",
                    port=993,
                    username="inbox@example.com",
                    password="app-password",
                ),
                str(root / "inbox_config.json"),
            )
            app = create_app(base_dir=root)
            client = TestClient(TestServer(app))
            with (
                mock.patch(
                    "hidemyemail_generator.webapp.RichHideMyEmail",
                    self.fake_hide_my_email(),
                ),
                mock.patch(
                    "hidemyemail_generator.webapp.sync_inbox",
                    return_value=[],
                ) as sync,
            ):
                await client.start_server()
                try:
                    first = await client.post(
                        "/api/code/latest", json={"email": "one@icloud.com"}
                    )
                    second = await client.post(
                        "/api/code/latest", json={"email": "one@icloud.com"}
                    )
                finally:
                    await client.close()

        self.assertEqual(first.status, 404)
        self.assertEqual(second.status, 404)
        self.assertEqual(sync.call_count, 1)

    async def test_authentication_failure_enters_backoff(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "cookies.txt").write_text("cookie", encoding="utf-8")
            save_config(
                InboxConfig(
                    host="imap.example.com",
                    port=993,
                    username="inbox@example.com",
                    password="app-password",
                ),
                str(root / "inbox_config.json"),
            )
            app = create_app(base_dir=root)
            client = TestClient(TestServer(app))
            with (
                mock.patch(
                    "hidemyemail_generator.webapp.RichHideMyEmail",
                    self.fake_hide_my_email(),
                ),
                mock.patch(
                    "hidemyemail_generator.webapp.sync_inbox",
                    side_effect=RuntimeError("AUTHENTICATIONFAILED"),
                ) as sync,
            ):
                await client.start_server()
                try:
                    first = await client.post(
                        "/api/code/latest", json={"email": "one@icloud.com"}
                    )
                    second = await client.post(
                        "/api/code/latest", json={"email": "one@icloud.com"}
                    )
                    first_payload = await first.json()
                    second_payload = await second.json()
                finally:
                    await client.close()

        self.assertEqual(first.status, 502)
        self.assertEqual(second.status, 502)
        self.assertIn("IMAP 登录失败", first_payload["error"])
        self.assertEqual(second_payload["error"], first_payload["error"])
        self.assertEqual(sync.call_count, 1)


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

            def start_with_browser(
                self, *, emails, concurrency, force_refresh=False
            ):
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

            def start_with_browser(
                self, *, emails, concurrency, force_refresh=False
            ):
                if not self.allow_protocol:
                    raise AssertionError("browser refresh called on wrong manager")
                self.browser_refresh_starts.append(
                    {
                        "emails": emails,
                        "concurrency": concurrency,
                        "force_refresh": force_refresh,
                    }
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

        async def run_case(
            *,
            has_valid_session,
            marked_invalid=False,
            refresh_with_cookie=False,
        ):
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
                            "cookies_json": json.dumps(
                                [
                                    {
                                        "name": "session",
                                        "value": "saved-cookie",
                                        "domain": "chatgpt.com",
                                        "path": "/",
                                    }
                                ]
                            ),
                        },
                    )
                    if marked_invalid:
                        mark_account_session_invalid(
                            app["db_file"],
                            "protocol@icloud.com",
                            "online endpoint returned 401",
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
                                "refresh_with_cookie": refresh_with_cookie,
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
            [
                {
                    "emails": ["protocol@icloud.com"],
                    "concurrency": 1,
                    "force_refresh": False,
                }
            ],
        )
        self.assertEqual(verification_manager.verify_starts, [])
        self.assertEqual(browser_manager.browser_starts, 0)

        response, payload, verification_manager, browser_manager = await run_case(
            has_valid_session=True,
            marked_invalid=True,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["mode"], "refresh_session")
        self.assertEqual(
            verification_manager.browser_refresh_starts,
            [
                {
                    "emails": ["protocol@icloud.com"],
                    "concurrency": 1,
                    "force_refresh": False,
                }
            ],
        )
        self.assertEqual(verification_manager.verify_starts, [])
        self.assertEqual(browser_manager.browser_starts, 0)

        response, payload, verification_manager, browser_manager = await run_case(
            has_valid_session=True,
            refresh_with_cookie=True,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["mode"], "refresh_cookie")
        self.assertEqual(
            verification_manager.browser_refresh_starts,
            [
                {
                    "emails": ["protocol@icloud.com"],
                    "concurrency": 1,
                    "force_refresh": True,
                }
            ],
        )
        self.assertEqual(verification_manager.verify_starts, [])
        self.assertEqual(browser_manager.browser_starts, 0)


if __name__ == "__main__":
    unittest.main()
