import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hidemyemail_generator.browser_tasks import load_account_record
from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.protocol_credentials import (
    MFA_BASE_URL,
    PASSWORD_ADD_URL,
    complete_protocol_credentials,
)
from hidemyemail_generator.protocol_registration import ProtocolRegistrationManager
from hidemyemail_generator.protocol_registration_worker import (
    _configure_utf8_stdio,
    _load_core,
    run,
)


class FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.posts = []
        self.closed = False

    def post(self, url, *, headers, data, timeout):
        payload = json.loads(data)
        self.posts.append((url, payload, headers, timeout))
        if url == PASSWORD_ADD_URL:
            return FakeResponse(200, {"ok": True})
        if url == f"{MFA_BASE_URL}/enroll":
            return FakeResponse(
                200,
                {
                    "secret": "JBSWY3DPEHPK3PXP",
                    "session_id": "mfa-session",
                    "factor": {"id": "factor-1"},
                },
            )
        if url == f"{MFA_BASE_URL}/user/activate_enrollment":
            return FakeResponse(200, {"recovery_codes": ["RECOVERY-1"]})
        return FakeResponse(404, {})

    def close(self):
        self.closed = True


class ProtocolCredentialTests(unittest.TestCase):
    def test_passwordless_result_adds_password_and_activates_totp(self):
        session = FakeSession()
        logs = []

        result = complete_protocol_credentials(
            email="protocol@icloud.com",
            access_token="access-token",
            generated_password="GeneratedPassword!1",
            password_set=False,
            request_session=session,
            log=logs.append,
            now=lambda: 1_700_000_010.0,
        )

        self.assertEqual(result["password"], "GeneratedPassword!1")
        self.assertTrue(result["password_set"])
        self.assertEqual(result["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertTrue(result["two_factor"]["enabled"])
        self.assertEqual(
            [url for url, *_ in session.posts],
            [
                PASSWORD_ADD_URL,
                f"{MFA_BASE_URL}/enroll",
                f"{MFA_BASE_URL}/user/activate_enrollment",
            ],
        )
        activation = session.posts[-1][1]
        self.assertRegex(activation["code"], r"^\d{6}$")
        self.assertIn(
            "POST /api/accounts/password/add 已确认后置密码成功",
            logs,
        )

    def test_existing_totp_still_adds_missing_password_without_reenrolling(self):
        session = FakeSession()

        result = complete_protocol_credentials(
            email="partial@icloud.com",
            access_token="access-token",
            generated_password="GeneratedPassword!1",
            password_set=False,
            existing_totp_secret="JBSWY3DPEHPK3PXP",
            request_session=session,
        )

        self.assertEqual([url for url, *_ in session.posts], [PASSWORD_ADD_URL])
        self.assertTrue(result["password_set"])
        self.assertTrue(result["two_factor"]["enabled"])


class ProtocolRegistrationWorkerTests(unittest.TestCase):
    def test_worker_reconfigures_all_standard_streams_to_utf8(self):
        class FakeStream:
            def __init__(self):
                self.options = None

            def reconfigure(self, **options):
                self.options = options

        streams = tuple(FakeStream() for _ in range(3))

        _configure_utf8_stdio(streams)

        for stream in streams:
            self.assertEqual(stream.options["encoding"], "utf-8")
            self.assertEqual(stream.options["errors"], "replace")
            self.assertTrue(stream.options["line_buffering"])

    def test_registration_is_passwordless_then_uses_post_account_setup(self):
        constructor = {}
        credential_call = {}

        class FakeRegister:
            def __init__(self, account, **kwargs):
                constructor.update(kwargs)
                self.password = "GeneratedPassword!1"

            def register(self):
                return {
                    "status": "success",
                    "access_token": "access-token",
                    "session_json": {},
                    "password": "",
                    "password_set": False,
                }

        def complete(**kwargs):
            credential_call.update(kwargs)
            return {
                "access_token": kwargs["access_token"],
                "password": kwargs["generated_password"],
                "two_factor": {
                    "enabled": True,
                    "secret": "JBSWY3DPEHPK3PXP",
                },
            }

        module = SimpleNamespace(ChatGPTRegister=FakeRegister)
        with (
            patch(
                "hidemyemail_generator.protocol_registration_worker._load_core",
                return_value=module,
            ),
            patch(
                "hidemyemail_generator.protocol_credentials.complete_protocol_credentials",
                side_effect=complete,
            ),
        ):
            result = run(
                {
                    "email": "protocol@icloud.com",
                    "code_url": "http://127.0.0.1/code",
                    "project_root": ".",
                    "source_root": ".",
                }
            )

        self.assertFalse(constructor["with_password"])
        self.assertFalse(credential_call["password_set"])
        self.assertEqual(
            credential_call["generated_password"], "GeneratedPassword!1"
        )
        self.assertEqual(result["password"], "GeneratedPassword!1")

    def test_invalid_state_restarts_full_registration_once(self):
        instances = []
        events = []

        class FakeRegister:
            def __init__(self, account, **kwargs):
                self.password = f"GeneratedPassword!{len(instances) + 1}"
                instances.append(self)

            def register(self):
                if len(instances) == 1:
                    return {
                        "status": "failed",
                        "error": (
                            "create_account_failed: invalid_state "
                            "(Your sign-in session is no longer valid. "
                            "Please start over to continue.)"
                        ),
                    }
                return {
                    "status": "success",
                    "access_token": "access-token",
                    "session_json": {},
                    "password": "",
                    "password_set": False,
                }

        def complete(**kwargs):
            return {
                "access_token": kwargs["access_token"],
                "password": kwargs["generated_password"],
                "two_factor": {
                    "enabled": True,
                    "secret": "JBSWY3DPEHPK3PXP",
                },
            }

        module = SimpleNamespace(ChatGPTRegister=FakeRegister)
        with (
            patch(
                "hidemyemail_generator.protocol_registration_worker._load_core",
                return_value=module,
            ),
            patch(
                "hidemyemail_generator.protocol_registration_worker._emit_event",
                side_effect=lambda stage, message, status="active": events.append(
                    (stage, message, status)
                ),
            ),
            patch(
                "hidemyemail_generator.protocol_credentials.complete_protocol_credentials",
                side_effect=complete,
            ),
        ):
            result = run(
                {
                    "email": "protocol@icloud.com",
                    "code_url": "http://127.0.0.1/code",
                    "project_root": ".",
                    "source_root": ".",
                }
            )

        self.assertEqual(len(instances), 2)
        self.assertEqual(result["password"], "GeneratedPassword!2")
        self.assertTrue(
            any(
                status == "warning" and "创建新会话" in message
                for _stage, message, status in events
            )
        )

    def test_invalid_state_full_registration_retry_is_bounded(self):
        instances = []

        class FakeRegister:
            def __init__(self, account, **kwargs):
                self.password = "GeneratedPassword!1"
                instances.append(self)

            def register(self):
                return {
                    "status": "failed",
                    "error": "create_account_failed: invalid_state",
                }

        module = SimpleNamespace(ChatGPTRegister=FakeRegister)
        with (
            patch(
                "hidemyemail_generator.protocol_registration_worker._load_core",
                return_value=module,
            ),
            patch(
                "hidemyemail_generator.protocol_registration_worker._emit_event"
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid_state"):
                run(
                    {
                        "email": "protocol@icloud.com",
                        "code_url": "http://127.0.0.1/code",
                        "project_root": ".",
                        "source_root": ".",
                    }
                )

        self.assertEqual(len(instances), 2)

    def test_other_registration_failures_are_not_restarted(self):
        instances = []

        class FakeRegister:
            def __init__(self, account, **kwargs):
                self.password = "GeneratedPassword!1"
                instances.append(self)

            def register(self):
                return {
                    "status": "failed",
                    "error": "create_account_failed: account_not_allowed",
                }

        module = SimpleNamespace(ChatGPTRegister=FakeRegister)
        with (
            patch(
                "hidemyemail_generator.protocol_registration_worker._load_core",
                return_value=module,
            ),
            patch(
                "hidemyemail_generator.protocol_registration_worker._emit_event"
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "account_not_allowed"):
                run(
                    {
                        "email": "protocol@icloud.com",
                        "code_url": "http://127.0.0.1/code",
                        "project_root": ".",
                        "source_root": ".",
                    }
                )

        self.assertEqual(len(instances), 1)


class ProtocolAuthStateTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _core_module():
        return _load_core(
            Path(__file__).resolve().parents[1]
            / "src"
            / "hidemyemail_generator"
            / "vendor"
            / "gptfree_register"
        )

    async def test_signin_keeps_device_and_logging_ids_in_oauth_state(self):
        class FakeCookies:
            def __init__(self):
                self.jar = []

            def set(self, name, value, *, domain):
                self.jar = [item for item in self.jar if item.name != name]
                self.jar.append(
                    SimpleNamespace(name=name, value=value, domain=domain)
                )

        class FakeResponse:
            def __init__(self, *, status=200, payload=None, headers=None, url=""):
                self.status_code = status
                self._payload = payload or {}
                self.headers = headers or {}
                self.url = url

            def json(self):
                return self._payload

        class FakeSession:
            def __init__(self):
                self.cookies = FakeCookies()
                self.gets = []
                self.posts = []

            async def get(self, url, **kwargs):
                self.gets.append((url, kwargs))
                if url.endswith("/api/auth/csrf"):
                    return FakeResponse(
                        payload={"csrfToken": "csrf-token"}, url=url
                    )
                if url.endswith("/authorize/start"):
                    return FakeResponse(
                        status=302,
                        headers={"location": "/email-verification"},
                        url=url,
                    )
                return FakeResponse(url=url)

            async def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                return FakeResponse(
                    payload={"url": "https://auth.openai.com/authorize/start"},
                    url=url,
                )

        module = self._core_module()
        client = module.OpenAIAuthClient()
        session = FakeSession()
        client._session = session

        result = await client.init_page_email("protocol@icloud.com")

        signin_url, signin_kwargs = session.posts[0]
        self.assertIn(f"ext-oai-did={client.device_id}", signin_url)
        self.assertIn(
            f"auth_session_logging_id={client.auth_session_logging_id}",
            signin_url,
        )
        self.assertIn("ext-passkey-client-capabilities=1111", signin_url)
        self.assertEqual(signin_kwargs["headers"]["origin"], "https://chatgpt.com")
        self.assertEqual(result["page_path"], "/email-verification")
        self.assertEqual(session.gets[-1][0], "https://auth.openai.com/email-verification")

    async def test_create_account_sends_same_device_and_auth_origin(self):
        class FakeResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"continue_url": "https://chatgpt.com/callback"}

        class FakeSession:
            def __init__(self):
                self.posts = []

            async def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                return FakeResponse()

        class FakeSentinel:
            def __init__(self):
                self.calls = []

            async def get_token(self, flow, device_id, *, force_refresh=False):
                self.calls.append((flow, device_id, force_refresh))
                return {
                    "p": "proof",
                    "t": "turnstile",
                    "c": "challenge",
                    "id": device_id,
                    "flow": flow,
                }

            async def get_so_token(self, flow, device_id):
                return ""

        module = self._core_module()
        sentinel = FakeSentinel()
        session = FakeSession()
        client = module.OpenAIAuthClient(sentinel=sentinel)
        client._session = session
        client.device_id = "device-for-whole-oauth-transaction"

        await client.create_account("Test User", "1995-01-01")

        _url, kwargs = session.posts[0]
        headers = kwargs["headers"]
        self.assertEqual(headers["origin"], "https://auth.openai.com")
        self.assertEqual(
            headers["oai-device-id"], "device-for-whole-oauth-transaction"
        )
        self.assertEqual(
            sentinel.calls[0][:2],
            ("oauth_create_account", "device-for-whole-oauth-transaction"),
        )

    async def test_otp_oauth_callback_session_skips_create_account(self):
        logs = []

        class FakeResponse:
            def __init__(self, *, url, payload=None, status=200):
                self.url = url
                self._payload = payload or {}
                self.status_code = status
                self.text = ""

            def json(self):
                return self._payload

        class FakeCookies:
            jar = []

        class FakeSession:
            def __init__(self):
                self.cookies = FakeCookies()
                self.gets = []

            async def get(self, url, **kwargs):
                self.gets.append((url, kwargs))
                if url.endswith("/api/auth/session"):
                    return FakeResponse(
                        url=url,
                        payload={
                            "accessToken": "header.payload.signature",
                            "sessionToken": "callback-session-token",
                        },
                    )
                return FakeResponse(url="https://chatgpt.com/")

        class FakeAuth:
            BASE_URL = "https://auth.openai.com"
            CHATGPT_URL = "https://chatgpt.com"

            def __init__(self, **_kwargs):
                self.device_id = "callback-device"
                self.sentinel = SimpleNamespace(set_cookies=lambda _cookies: None)
                self.session = FakeSession()

            async def share_session_with_sentinel(self):
                return None

            async def init_page_email(self, _email):
                return {
                    "device_id": self.device_id,
                    "cookies": {},
                    "page_path": "/email-verification",
                }

            async def validate_email_otp(self, _code):
                return {
                    "page": {
                        "type": "external_url",
                        "payload": {
                            "url": (
                                "https://auth.openai.com/api/auth/callback/openai"
                            )
                        },
                    }
                }

            async def _get_session(self):
                return self.session

            async def create_account(self, *_args, **_kwargs):
                raise AssertionError("create_account must not run after callback Session")

            async def close(self):
                return None

        module = self._core_module()
        bot = module.ChatGPTRegister(
            {
                "email": "protocol@icloud.com",
                "refresh_token": "http://127.0.0.1/code",
            },
            log_fn=logs.append,
            with_password=False,
        )
        with (
            patch.object(module, "OpenAIAuthClient", FakeAuth),
            patch.object(module, "_fetch_otp_sync", return_value="123456"),
        ):
            result = await bot._register_async(
                password="GeneratedPassword!1",
                name="Test User",
                birthdate="1995-01-01",
                client_id="",
                refresh_token="http://127.0.0.1/code",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["access_token"], "header.payload.signature")
        self.assertEqual(
            result["raw"]["recovered_existing_session"],
            "otp_oauth_callback_session",
        )
        self.assertTrue(any("账号尚未完成" in item for item in logs))
        self.assertTrue(any("OAuth callback" in item for item in logs))


class ProtocolRegistrationManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.db_file = self.base_dir / "hidemyemail.db"
        connection = connect_db(str(self.db_file))
        connection.close()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_defaults_to_bundled_gptfree_register_module(self):
        async def runner(payload, on_event):
            return {"status": "failed", "error": "unused"}

        with patch.dict(
            "os.environ",
            {"GPTFREE_REGISTER_ROOT": "", "GPTFREE_REGISTER_PYTHON": ""},
        ):
            manager = ProtocolRegistrationManager(
                base_dir=self.base_dir,
                db_file=self.db_file,
                worker_runner=runner,
            )

        self.assertEqual(manager.gptfree_root.name, "gptfree_register")
        self.assertTrue(
            (manager.gptfree_root / "core" / "chatgpt_register.py").is_file()
        )
        self.assertTrue(manager.snapshot()["runtime"]["available"])

    async def test_background_service_uses_console_python_for_worker_pipes(self):
        runtime_dir = self.base_dir / "runtime"
        runtime_dir.mkdir()
        pythonw = runtime_dir / "pythonw.exe"
        python = runtime_dir / "python.exe"
        pythonw.touch()
        python.touch()

        with patch("hidemyemail_generator.protocol_registration.sys.executable", str(pythonw)):
            manager = ProtocolRegistrationManager(
                base_dir=self.base_dir,
                db_file=self.db_file,
                worker_runner=lambda payload, on_event: None,
            )

        self.assertEqual(manager.python_executable, python.resolve())

    async def test_protocol_worker_forces_utf8_for_streamed_chinese_logs(self):
        worker_script = self.base_dir / "utf8_worker.py"
        worker_script.write_text(
            "import json, os, sys\n"
            "print('中文协议日志', flush=True)\n"
            "print('HME_PROTOCOL_RESULT:' + json.dumps({\n"
            "    'status': 'success',\n"
            "    'stdout_encoding': sys.stdout.encoding,\n"
            "    'pythonioencoding': os.environ.get('PYTHONIOENCODING'),\n"
            "    'pythonutf8': os.environ.get('PYTHONUTF8'),\n"
            "}, ensure_ascii=False), flush=True)\n",
            encoding="utf-8",
        )
        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=lambda payload, on_event: None,
        )
        manager.worker_script = worker_script
        manager.python_executable = Path(sys.executable)
        events = []

        result = await manager._run_worker({}, events.append)

        self.assertEqual(events[0]["message"], "中文协议日志")
        self.assertEqual(result["stdout_encoding"].lower().replace("_", "-"), "utf-8")
        self.assertEqual(result["pythonioencoding"], "utf-8")
        self.assertEqual(result["pythonutf8"], "1")

    async def test_bundled_core_loads_dynamic_sentinel_protocol_modules(self):
        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=lambda payload, on_event: None,
        )

        module = _load_core(manager.gptfree_root)
        provider = module._SentinelWithProxy()
        profile = provider._impl._browser_profile("dynamic-import-check")

        self.assertEqual(profile.device_id, "dynamic-import-check")

    async def test_success_requires_and_persists_password_two_factor_and_session(self):
        captured = []

        async def runner(payload, on_event):
            captured.append(payload)
            on_event(
                {
                    "stage": "email_verification",
                    "message": "已收到邮箱验证码",
                    "status": "success",
                }
            )
            return {
                "status": "success",
                "email": payload["email"],
                "access_token": "header.payload.signature",
                "session_json": json.dumps(
                    {
                        "accessToken": "header.payload.signature",
                        "sessionToken": "session-token",
                    }
                ),
                "storage_state_json": json.dumps({"cookies": [], "origins": []}),
                "session_acquisition_method": "gptfree_mail_auth",
                "password": "GeneratedPassword!1",
                "two_factor": {
                    "enabled": True,
                    "status": "enabled",
                    "type": "totp",
                    "secret": "JBSWY3DPEHPK3PXP",
                },
            }

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=runner,
        )
        initial = manager.start(
            emails=["protocol@icloud.com"],
            base_url="http://127.0.0.1:8080",
            concurrency=1,
        )
        self.assertTrue(initial["running"])
        final = await manager.wait()

        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["succeeded"], 1)
        self.assertEqual(final["failed"], 0)
        self.assertIn("/api/protocol-registration/code/", captured[0]["code_url"])
        record = load_account_record(self.db_file, "protocol@icloud.com")
        self.assertEqual(record["password"], "GeneratedPassword!1")
        self.assertTrue(record["password_confirmed"])
        self.assertTrue(record["two_factor"]["enabled"])
        self.assertEqual(record["session_acquisition_method"], "gptfree_mail_auth")
        self.assertEqual(record["session"]["sessionToken"], "session-token")
        self.assertEqual(
            record["registration_environment"]["registration_mode"], "protocol"
        )
        self.assertEqual(
            record["registration_environment"]["email_type"],
            "icloud_hide_my_email",
        )
        self.assertEqual(record["registration_environment"]["proxy_mode"], "direct")

    async def test_protocol_registration_uses_enabled_shared_proxy_module(self):
        captured = []

        class ProxyStore:
            def next_proxy(self):
                return "http://proxy.example:8080", {
                    "country": "JP",
                    "countryLabel": "日本",
                }

        async def runner(payload, on_event):
            captured.append(payload)
            return {"status": "failed", "error": "captured"}

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=runner,
            proxy_store=ProxyStore(),
        )
        manager.start(
            emails=["proxy-protocol@icloud.com"],
            base_url="http://127.0.0.1:8080",
        )
        final = await manager.wait()

        self.assertEqual(final["status"], "failed")
        self.assertEqual(captured[0]["proxy_url"], "http://proxy.example:8080")
        self.assertTrue(
            any("已分配注册代理：日本" in item["message"] for item in final["logs"])
        )

    async def test_incomplete_worker_result_is_failed_and_not_persisted(self):
        async def runner(payload, on_event):
            return {
                "status": "success",
                "email": payload["email"],
                "access_token": "access-token",
                "password": "GeneratedPassword!1",
                "two_factor": {"enabled": False},
            }

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=runner,
        )
        manager.start(
            emails=["incomplete@icloud.com"],
            base_url="http://127.0.0.1:8080",
        )
        final = await manager.wait()

        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["failed"], 1)
        self.assertEqual(load_account_record(self.db_file, "incomplete@icloud.com"), {})
        self.assertIn("TOTP 2FA", final["accounts"][0]["message"])

    async def test_task_specific_completion_callback_reports_success_and_failure(self):
        completions = []

        async def runner(payload, on_event):
            if payload["email"].startswith("failed"):
                raise RuntimeError("protocol rejected")
            return {
                "status": "success",
                "email": payload["email"],
                "access_token": "header.payload.signature",
                "session_json": json.dumps(
                    {"accessToken": "header.payload.signature"}
                ),
                "password": "GeneratedPassword!1",
                "two_factor": {
                    "enabled": True,
                    "secret": "JBSWY3DPEHPK3PXP",
                },
            }

        async def finished(email, success, message):
            completions.append((email, success, message))

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=runner,
        )
        manager.start(
            emails=["success@icloud.com", "failed@icloud.com"],
            base_url="http://127.0.0.1:8080",
            concurrency=2,
            on_account_finished=finished,
        )
        final = await manager.wait()

        self.assertEqual(final["succeeded"], 1)
        self.assertEqual(final["failed"], 1)
        by_email = {email: (success, message) for email, success, message in completions}
        self.assertTrue(by_email["success@icloud.com"][0])
        self.assertFalse(by_email["failed@icloud.com"][0])
        self.assertIn("protocol rejected", by_email["failed@icloud.com"][1])


if __name__ == "__main__":
    unittest.main()
