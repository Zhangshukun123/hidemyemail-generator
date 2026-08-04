import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer
from rich.console import Console

from hidemyemail_generator.webapp import (
    WORKBENCH_OPENAI_CODE_PATH,
    _configure_utf8_stdio,
    _generation_failure_message,
    _load_local_env_file,
    create_app,
)


class WebAppStdioTests(unittest.TestCase):
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


class VerifyAccountEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_verify_uses_protocol_relogin_and_never_starts_browser(self):
        class ManagerStub:
            def __init__(self, *, allow_protocol=False):
                self.allow_protocol = allow_protocol
                self.protocol_emails = []
                self.browser_starts = 0

            def snapshot(self):
                return {"running": False}

            def start_protocol_relogin(self, *, email):
                if not self.allow_protocol:
                    raise AssertionError("protocol relogin called on wrong manager")
                self.protocol_emails.append(email)
                return {"running": True, "accounts": [{"email": email}]}

            def start(self, *_args, **_kwargs):
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

        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            (base_dir / "cookies.txt").write_text("cookie", encoding="utf-8")
            (base_dir / "inbox_config.json").write_text("{}\n", encoding="utf-8")
            app = create_app(base_dir=base_dir)
            protocol_manager = ManagerStub(allow_protocol=True)
            browser_manager = ManagerStub()
            app["verification_manager"] = protocol_manager
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
                            "email": "protocol@icloud.com",
                            "headless": False,
                            "reset_password": False,
                        },
                        headers={"X-Local-Token": app["local_token"]},
                    )
                    payload = await response.json()
                finally:
                    await client.close()

            self.assertEqual(response.status, 200)
            self.assertEqual(payload["mode"], "verify")
            self.assertEqual(protocol_manager.protocol_emails, ["protocol@icloud.com"])
            self.assertEqual(browser_manager.browser_starts, 0)


if __name__ == "__main__":
    unittest.main()
