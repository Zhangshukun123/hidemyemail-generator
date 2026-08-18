import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hidemyemail_generator import protocol_credentials as protocol_credentials_module
from hidemyemail_generator.browser_tasks import load_account_record
from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.protocol_credentials import (
    MFA_BASE_URL,
    complete_protocol_credentials,
)
from hidemyemail_generator.protocol_registration import (
    ConcurrentProtocolRegistrationManager,
    ProtocolRegistrationManager,
    _protocol_failure_details,
)
from hidemyemail_generator.protocol_registration_worker import (
    CHROME_IMPERSONATE_PROFILES,
    FIREFOX_IMPERSONATE_PROFILES,
    PROTOCOL_IMPERSONATE_PROFILES,
    _choose_protocol_impersonate,
    _configure_utf8_stdio,
    _load_core,
    _proxy_fingerprint_profile,
    _tls_fallback_impersonate,
    run,
)
from hidemyemail_generator.protocol_browser import ProtocolBrowserPersona


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
        self.redirect_flags = []

    def post(self, url, *, headers, data, timeout, allow_redirects=True):
        payload = json.loads(data)
        self.posts.append((url, payload, headers, timeout))
        self.redirect_flags.append(allow_redirects)
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
        confirmed = []

        result = complete_protocol_credentials(
            email="protocol@icloud.com",
            access_token="access-token",
            generated_password="GeneratedPassword!1",
            password_set=False,
            request_session=session,
            log=logs.append,
            on_password_confirmed=lambda: confirmed.append(True),
            password_verifier=lambda: {
                "verified": True,
                "access_token": "verified-access-token",
                "reuse_registration_session": False,
            },
            language="ja-JP",
            now=lambda: 1_700_000_010.0,
        )

        self.assertEqual(result["password"], "GeneratedPassword!1")
        self.assertTrue(result["password_set"])
        self.assertEqual(result["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertTrue(result["two_factor"]["enabled"])
        self.assertEqual(confirmed, [True])
        self.assertEqual(
            session.posts[0][2]["Accept-Language"],
            "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
        )
        self.assertEqual(
            [url for url, *_ in session.posts],
            [
                f"{MFA_BASE_URL}/enroll",
                f"{MFA_BASE_URL}/user/activate_enrollment",
            ],
        )
        activation = session.posts[-1][1]
        self.assertRegex(activation["code"], r"^\d{6}$")
        self.assertIn(
            "账号尚无密码；将使用当前注册 Session 直接添加密码",
            logs,
        )
        self.assertIn("当前认证会话已确认密码添加成功", logs)

    def test_totp_activation_retries_once_after_code_window_failure(self):
        class RetrySession(FakeSession):
            def __init__(self):
                super().__init__()
                self.activation_attempts = 0

            def post(self, url, *, headers, data, timeout, allow_redirects=True):
                if url == f"{MFA_BASE_URL}/user/activate_enrollment":
                    payload = json.loads(data)
                    self.posts.append((url, payload, headers, timeout))
                    self.redirect_flags.append(allow_redirects)
                    self.activation_attempts += 1
                    if self.activation_attempts == 1:
                        return FakeResponse(400, {"detail": "invalid totp code"})
                    return FakeResponse(200, {"recovery_codes": ["RECOVERY-1"]})
                return super().post(
                    url,
                    headers=headers,
                    data=data,
                    timeout=timeout,
                    allow_redirects=allow_redirects,
                )

        session = RetrySession()
        times = iter((10.0, 10.0, 10.0, 40.0))
        sleeps = []

        result = complete_protocol_credentials(
            email="protocol@icloud.com",
            access_token="access-token",
            generated_password="GeneratedPassword!1",
            password_set=True,
            request_session=session,
            now=lambda: next(times),
            sleep=sleeps.append,
        )

        self.assertTrue(result["two_factor"]["server_verified"])
        self.assertEqual(session.activation_attempts, 2)
        self.assertEqual(sleeps, [20.25])

    def test_password_add_reuses_registration_session_without_login_token(self):
        class RegistrationSession(FakeSession):
            def __init__(self):
                super().__init__()
                self.gets = []

            def get(self, url, **kwargs):
                self.gets.append((url, kwargs))
                return FakeResponse(200, {"accessToken": "refreshed-registration-token"})

        session = RegistrationSession()
        logs = []

        result = complete_protocol_credentials(
            email="protocol@icloud.com",
            access_token="registration-access-token",
            generated_password="GeneratedPassword!1",
            password_set=False,
            session_token="registration-session-token",
            request_session=session,
            password_verifier=lambda: {
                "verified": True,
                "password_added": True,
                "reuse_registration_session": True,
            },
            log=logs.append,
            now=lambda: 1_700_000_010.0,
        )

        self.assertEqual(len(session.gets), 1)
        self.assertEqual(result["access_token"], "refreshed-registration-token")
        self.assertEqual(
            [headers["Authorization"] for _, _, headers, _ in session.posts],
            [
                "Bearer refreshed-registration-token",
                "Bearer refreshed-registration-token",
            ],
        )
        self.assertIn("添加密码后已刷新当前注册 Session 的 Access Token", logs)
        self.assertNotIn("全新认证会话", " ".join(logs))

    def test_verified_password_token_is_not_replaced_by_old_session_refresh(self):
        class StaleRegistrationSession(FakeSession):
            def __init__(self):
                super().__init__()
                self.gets = []

            def get(self, url, **kwargs):
                self.gets.append((url, kwargs))
                return FakeResponse(200, {"accessToken": "invalidated-old-token"})

        session = StaleRegistrationSession()

        result = complete_protocol_credentials(
            email="protocol@icloud.com",
            access_token="registration-access-token",
            generated_password="GeneratedPassword!1",
            password_set=False,
            session_token="old-registration-session",
            request_session=session,
            password_verifier=lambda: {
                "verified": True,
                "access_token": "verified-password-login-token",
            },
            now=lambda: 1_700_000_010.0,
        )

        self.assertEqual(session.gets, [])
        self.assertEqual(result["access_token"], "verified-password-login-token")
        self.assertEqual(
            [headers["Authorization"] for _, _, headers, _ in session.posts],
            [
                "Bearer verified-password-login-token",
                "Bearer verified-password-login-token",
            ],
        )

    def test_existing_totp_still_adds_missing_password_without_reenrolling(self):
        class RefreshTrackingSession(FakeSession):
            def __init__(self):
                super().__init__()
                self.gets = []

            def get(self, url, **kwargs):
                self.gets.append((url, kwargs))
                return FakeResponse(200, {"accessToken": "stale-token"})

        session = RefreshTrackingSession()

        result = complete_protocol_credentials(
            email="partial@icloud.com",
            access_token="access-token",
            generated_password="GeneratedPassword!1",
            password_set=False,
            existing_totp_secret="JBSWY3DPEHPK3PXP",
            session_token="old-registration-session",
            password_verifier=lambda: {"verified": True},
            request_session=session,
        )

        self.assertEqual(session.gets, [])
        self.assertEqual(session.posts, [])
        self.assertTrue(result["password_set"])
        self.assertTrue(result["two_factor"]["enabled"])
        self.assertFalse(result["two_factor"]["server_verified"])
        self.assertEqual(
            result["two_factor"]["verification_source"],
            "saved_record",
        )

    def test_verifier_without_token_stops_before_new_totp_enrollment(self):
        class RefreshTrackingSession(FakeSession):
            def __init__(self):
                super().__init__()
                self.gets = []

            def get(self, url, **kwargs):
                self.gets.append((url, kwargs))
                return FakeResponse(200, {"accessToken": "stale-token"})

        session = RefreshTrackingSession()

        with self.assertRaisesRegex(RuntimeError, "未返回可用于创建 2FA"):
            complete_protocol_credentials(
                email="protocol@icloud.com",
                access_token="registration-access-token",
                generated_password="GeneratedPassword!1",
                password_set=True,
                session_token="old-registration-session",
                password_verifier=lambda: {"verified": True},
                request_session=session,
            )

        self.assertEqual(session.gets, [])
        self.assertEqual(session.posts, [])

    def test_passwordless_account_stops_when_password_add_fails(self):
        session = FakeSession()
        confirmed = []

        with self.assertRaisesRegex(RuntimeError, "当前认证会话添加密码失败"):
            complete_protocol_credentials(
                email="protocol@icloud.com",
                access_token="access-token",
                generated_password="GeneratedPassword!1",
                password_set=False,
                request_session=session,
                password_verifier=lambda: {
                    "verified": False,
                    "error": "invalid_username_or_password",
                },
                on_password_confirmed=lambda: confirmed.append(True),
            )

        self.assertEqual(confirmed, [])
        self.assertEqual(session.posts, [])

    def test_explicit_password_add_failure_is_not_ignored(self):
        session = FakeSession()

        with self.assertRaisesRegex(RuntimeError, "当前认证会话添加密码失败"):
            complete_protocol_credentials(
                email="protocol@icloud.com",
                access_token="access-token",
                generated_password="GeneratedPassword!1",
                password_set=True,
                existing_totp_secret="JBSWY3DPEHPK3PXP",
                request_session=session,
                password_verifier=lambda: {
                    "verified": False,
                    "error": "invalid_username_or_password",
                },
            )

        self.assertEqual(session.posts, [])

    def test_mfa_requests_keep_registration_device_and_frontend_context(self):
        session = FakeSession()

        result = complete_protocol_credentials(
            email="protocol@icloud.com",
            access_token="access-token",
            generated_password="GeneratedPassword!1",
            password_set=True,
            device_id="registration-device-id",
            request_session=session,
            now=lambda: 1_700_000_010.0,
        )

        self.assertTrue(result["two_factor"]["enabled"])
        self.assertEqual(session.redirect_flags, [False, False])
        for url, _payload, headers, _timeout in session.posts:
            self.assertEqual(headers["oai-device-id"], "registration-device-id")
            self.assertEqual(headers["oai-language"], "en-US")
            self.assertEqual(headers["sec-fetch-site"], "same-origin")
            self.assertEqual(headers["x-openai-target-path"], url.removeprefix("https://chatgpt.com"))
            self.assertEqual(headers["x-openai-target-route"], url.removeprefix("https://chatgpt.com"))
            self.assertNotIn("User-Agent", headers)

    def test_new_mfa_session_reuses_registration_cookies_and_fingerprint(self):
        class CookieStore:
            def __init__(self):
                self.values = []

            def set(self, name, value, **options):
                self.values.append((name, value, options))

        fake_session = SimpleNamespace(cookies=CookieStore(), proxies={})
        with patch(
            "curl_cffi.requests.Session",
            return_value=fake_session,
        ) as constructor:
            session = protocol_credentials_module._new_session(
                proxy_url="",
                session_token="session-token",
                device_id="device-id",
                session_cookies=[
                    {
                        "name": "__cf_bm",
                        "value": "cf-cookie",
                        "domain": ".chatgpt.com",
                        "path": "/",
                    }
                ],
                impersonate="firefox144",
            )

        self.assertIs(session, fake_session)
        constructor.assert_called_once_with(impersonate="firefox144")
        self.assertIn(
            (
                "__cf_bm",
                "cf-cookie",
                {"domain": ".chatgpt.com", "path": "/"},
            ),
            fake_session.cookies.values,
        )
        self.assertIn(
            (
                "__Secure-next-auth.session-token",
                "session-token",
                {"domain": "chatgpt.com", "path": "/"},
            ),
            fake_session.cookies.values,
        )


class ProtocolRegistrationWorkerTests(unittest.TestCase):
    def test_terminal_protocol_failures_are_not_marked_retryable(self):
        details = _protocol_failure_details(
            "RuntimeError: account_unusable: account_deactivated"
        )

        self.assertEqual(details["code"], "account_unusable")
        self.assertFalse(details["retryable"])
        self.assertIn("隔离", details["next_action"])

    def test_every_protocol_persona_uses_matching_windows_headers(self):
        for impersonate in PROTOCOL_IMPERSONATE_PROFILES:
            with self.subTest(impersonate=impersonate):
                persona = ProtocolBrowserPersona.from_impersonate(impersonate)
                headers = persona.session_headers("en-US")
                self.assertIn("Windows NT 10.0", headers["user-agent"])
                self.assertNotIn("Macintosh", headers["user-agent"])
                if impersonate.startswith("chrome"):
                    self.assertEqual(headers["sec-ch-ua-platform"], '"Windows"')
                    self.assertIn(
                        f'"Google Chrome";v="{persona.major_version}"',
                        headers["sec-ch-ua"],
                    )
                else:
                    self.assertNotIn("sec-ch-ua", headers)

    def test_new_registration_device_profile_is_randomly_selected(self):
        with patch(
            "hidemyemail_generator.protocol_registration_worker.secrets.choice",
            return_value="chrome136",
        ) as choice:
            selected = _choose_protocol_impersonate()

        self.assertEqual(selected, "chrome136")
        choice.assert_called_once_with(PROTOCOL_IMPERSONATE_PROFILES)
        self.assertEqual(len(PROTOCOL_IMPERSONATE_PROFILES), 8)
        self.assertEqual(len(set(PROTOCOL_IMPERSONATE_PROFILES)), 8)

    def test_tls_fallback_switches_to_the_other_browser_family(self):
        self.assertIn(
            _tls_fallback_impersonate("firefox144"),
            CHROME_IMPERSONATE_PROFILES,
        )
        self.assertIn(
            _tls_fallback_impersonate("chrome136"),
            FIREFOX_IMPERSONATE_PROFILES,
        )

    def test_proxy_country_selects_matching_language_and_timezone(self):
        self.assertEqual(
            _proxy_fingerprint_profile("JP"),
            {
                "country": "JP",
                "language": "ja-JP",
                "timezone": "Asia/Tokyo",
            },
        )
        self.assertEqual(
            _proxy_fingerprint_profile("DE")["timezone"],
            "Europe/Berlin",
        )
        self.assertEqual(
            _proxy_fingerprint_profile("TH"),
            {
                "country": "TH",
                "language": "th-TH",
                "timezone": "Asia/Bangkok",
            },
        )
        self.assertEqual(
            _proxy_fingerprint_profile("BR"),
            {
                "country": "BR",
                "language": "pt-BR",
                "timezone": "America/Sao_Paulo",
            },
        )

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

    def test_registration_sets_account_password_before_otp_then_enables_totp(self):
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
                    "session_cookies": [
                        {
                            "name": "__cf_bm",
                            "value": "registration-cookie",
                            "domain": ".chatgpt.com",
                            "path": "/",
                        }
                    ],
                    "password": "GeneratedPassword!1",
                    "password_set": True,
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
            patch(
                "hidemyemail_generator.protocol_registration_worker._choose_protocol_impersonate",
                return_value="chrome136",
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

        self.assertTrue(constructor["with_password"])
        self.assertTrue(credential_call["password_set"])
        self.assertEqual(
            credential_call["generated_password"], "GeneratedPassword!1"
        )
        self.assertEqual(
            credential_call["session_cookies"][0]["name"], "__cf_bm"
        )
        self.assertEqual(constructor["impersonate"], "chrome136")
        self.assertEqual(credential_call["impersonate"], "chrome136")
        self.assertEqual(
            result["registration_diagnostics"]["impersonate"], "chrome136"
        )
        self.assertEqual(result["password"], "GeneratedPassword!1")

    def test_registration_can_stop_after_passwordless_session(self):
        constructor = {}

        class FakeRegister:
            def __init__(self, account, **kwargs):
                constructor["account"] = dict(account)
                constructor.update(kwargs)
                self.password = "ShouldNotBeSaved!1"

            def register(self):
                return {
                    "status": "success",
                    "access_token": "passwordless-access-token",
                    "session_token": "passwordless-session-token",
                    "session_json": {"sessionToken": "passwordless-session-token"},
                    "session_cookies": [
                        {
                            "name": "__Secure-next-auth.session-token",
                            "value": "passwordless-session-token",
                            "domain": "chatgpt.com",
                            "path": "/",
                        }
                    ],
                    "device_id": "passwordless-device",
                    "password": "ShouldNotBeSaved!1",
                    "password_set": False,
                }

        module = SimpleNamespace(ChatGPTRegister=FakeRegister)
        with (
            patch(
                "hidemyemail_generator.protocol_registration_worker._load_core",
                return_value=module,
            ),
            patch(
                "hidemyemail_generator.protocol_credentials.complete_protocol_credentials"
            ) as complete,
        ):
            result = run(
                {
                    "email": "session-only@icloud.com",
                    "code_url": "http://127.0.0.1/code",
                    "project_root": ".",
                    "source_root": ".",
                    "setup_credentials": False,
                }
            )

        self.assertFalse(constructor["with_password"])
        self.assertEqual(constructor["account"]["password"], "")
        complete.assert_not_called()
        self.assertEqual(result["access_token"], "passwordless-access-token")
        self.assertNotIn("password", result)
        self.assertNotIn("two_factor", result)
        self.assertFalse(result["registration_diagnostics"]["setup_credentials"])

    def test_password_checkpoint_waits_for_login_proof_before_2fa(self):
        events = []

        class FakeRegister:
            def __init__(self, account, **kwargs):
                self.password = "GeneratedPassword!1"

            def register(self):
                return {
                    "status": "success",
                    "access_token": "access-token",
                    "session_token": "session-token",
                    "session_json": {"sessionToken": "session-token"},
                    "session_cookies": [
                        {
                            "name": "__Secure-next-auth.session-token",
                            "value": "session-token",
                            "domain": "chatgpt.com",
                            "path": "/",
                        }
                    ],
                    "device_id": "device-id",
                    "impersonate": "firefox144",
                    "password": self.password,
                    "password_set": True,
                }

        def emit(stage, message, status="active", **details):
            events.append(
                {
                    "stage": stage,
                    "message": message,
                    "status": status,
                    **details,
                }
            )

        module = SimpleNamespace(ChatGPTRegister=FakeRegister)
        with (
            patch(
                "hidemyemail_generator.protocol_registration_worker._load_core",
                return_value=module,
            ),
            patch(
                "hidemyemail_generator.protocol_registration_worker._emit_event",
                side_effect=emit,
            ),
            patch(
                "hidemyemail_generator.protocol_credentials.complete_protocol_credentials",
                side_effect=RuntimeError("2FA setup failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "2FA setup failed"):
                run(
                    {
                        "email": "protocol@icloud.com",
                        "code_url": "http://127.0.0.1/code",
                        "project_root": ".",
                        "source_root": ".",
                    }
                )

        self.assertFalse(
            any(event["stage"] == "password_checkpoint" for event in events)
        )

    def test_saved_complete_credentials_resume_skips_password_login(self):
        constructor_calls = []
        credential_call = {}
        events = []

        class FakeRegister:
            def __init__(self, account, **kwargs):
                constructor_calls.append((account, kwargs))
                self.password = account["password"]
                self.password_checkpoint = kwargs["password_checkpoint_fn"]

            def register(self):
                self.password_checkpoint(self.password, False)
                return {
                    "status": "failed",
                    "error": "account_password_verified_mfa_required",
                }

        def complete(**kwargs):
            credential_call.update(kwargs)
            self.assertIsNone(kwargs["password_verifier"])
            kwargs["on_password_confirmed"]()
            return {
                "access_token": "refreshed-access-token",
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
                side_effect=lambda stage, message, status="active", **details: events.append(
                    (stage, message, status, details)
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
                    "existing_password": "ExistingPassword!1",
                    "existing_password_confirmed": True,
                    "existing_access_token": "saved-access-token",
                    "existing_session_token": "saved-session-token",
                    "existing_session_json": {
                        "accessToken": "saved-access-token",
                        "sessionToken": "saved-session-token",
                    },
                    "existing_session_cookies": [
                        {
                            "name": "__Secure-next-auth.session-token",
                            "value": "saved-session-token",
                        }
                    ],
                    "existing_device_id": "saved-device-id",
                    "existing_impersonate": "chrome136",
                    "existing_totp_secret": "JBSWY3DPEHPK3PXP",
                }
            )

        self.assertEqual(constructor_calls, [])
        self.assertTrue(credential_call["password_set"])
        self.assertEqual(credential_call["generated_password"], "ExistingPassword!1")
        self.assertEqual(credential_call["session_token"], "saved-session-token")
        self.assertEqual(credential_call["device_id"], "saved-device-id")
        self.assertEqual(credential_call["impersonate"], "chrome136")
        self.assertTrue(
            result["registration_diagnostics"]["resumed_from_password_checkpoint"]
        )
        self.assertFalse(result["registration_diagnostics"]["password_login_verified"])
        self.assertEqual(
            result["registration_diagnostics"][
                "password_login_verification_attempts"
            ],
            0,
        )
        self.assertEqual(
            sum(stage == "password_checkpoint" for stage, *_rest in events),
            1,
        )
        self.assertTrue(any(stage == "two_factor" for stage, *_rest in events))

    def test_saved_password_account_skips_add_and_login_reauth(self):
        constructor_accounts = []
        events = []

        class FakeRegister:
            def __init__(self, account, **kwargs):
                constructor_accounts.append(dict(account))
                self.password = account["password"]
                self.password_checkpoint = kwargs["password_checkpoint_fn"]
                self.index = len(constructor_accounts)

            def register(self):
                self.password_checkpoint(self.password, False)
                if self.index == 1:
                    return {
                        "status": "failed",
                        "error": "account_password_add_reauth_required",
                    }
                if self.index == 2:
                    self.password_checkpoint(self.password, True)
                    return {
                        "status": "failed",
                        "error": "account_password_add_completed_retry_login",
                    }
                return {
                    "status": "failed",
                    "error": "account_password_verified_mfa_required",
                }

        def complete(**kwargs):
            self.assertIsNone(kwargs["password_verifier"])
            kwargs["on_password_confirmed"]()
            return {
                "access_token": kwargs["access_token"],
                "password": kwargs["generated_password"],
                "two_factor": {
                    "enabled": True,
                    "secret": kwargs["existing_totp_secret"],
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
                side_effect=lambda stage, message, status="active", **details: events.append(
                    (stage, message, status, details)
                ),
            ),
            patch(
                "hidemyemail_generator.protocol_credentials.complete_protocol_credentials",
                side_effect=complete,
            ),
            patch(
                "hidemyemail_generator.protocol_registration_worker.time.sleep"
            ) as sleep_mock,
        ):
            result = run(
                {
                    "email": "protocol@icloud.com",
                    "code_url": "http://127.0.0.1/code",
                    "project_root": ".",
                    "source_root": ".",
                    "existing_password": "ExistingPassword!1",
                    "existing_password_confirmed": True,
                    "existing_access_token": "saved-access-token",
                    "existing_session_token": "saved-session-token",
                    "existing_session_json": {
                        "accessToken": "saved-access-token",
                        "sessionToken": "saved-session-token",
                    },
                    "existing_session_cookies": [
                        {
                            "name": "__Secure-next-auth.session-token",
                            "value": "saved-session-token",
                            "domain": "chatgpt.com",
                            "path": "/",
                        },
                        {
                            "name": "oai-did",
                            "value": "saved-device-id",
                            "domain": "chatgpt.com",
                            "path": "/",
                        },
                    ],
                    "existing_device_id": "saved-device-id",
                    "existing_totp_secret": "JBSWY3DPEHPK3PXP",
                }
            )

        self.assertEqual(constructor_accounts, [])
        diagnostics = result["registration_diagnostics"]
        self.assertFalse(diagnostics["password_login_verified"])
        self.assertFalse(diagnostics["password_add_reauth_recovered"])
        self.assertEqual(diagnostics["password_add_attempts"], 0)
        self.assertEqual(diagnostics["password_login_verification_attempts"], 0)
        self.assertEqual(
            sum(stage == "password_checkpoint" for stage, *_rest in events),
            1,
        )
        sleep_mock.assert_not_called()

    def test_password_add_reset_completion_reuses_current_session_without_login(self):
        constructor_accounts = []

        class FakeRegister:
            def __init__(self, account, **_kwargs):
                constructor_accounts.append(dict(account))
                self.password = account["password"]
                self.index = len(constructor_accounts)

            def register(self):
                return {
                    "status": "failed",
                    "error": "account_password_reset_completed_use_current_session",
                }

        def complete(**kwargs):
            verification = kwargs["password_verifier"]()
            self.assertTrue(verification["verified"])
            self.assertTrue(verification["reuse_registration_session"])
            kwargs["on_password_confirmed"]()
            return {
                "access_token": kwargs["access_token"],
                "password": kwargs["generated_password"],
                "two_factor": {
                    "enabled": True,
                    "secret": kwargs["existing_totp_secret"],
                },
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
                    "existing_password": "ExistingPassword!1",
                    "existing_password_confirmed": False,
                    "existing_access_token": "saved-access-token",
                    "existing_session_token": "saved-session-token",
                    "existing_session_json": {
                        "accessToken": "saved-access-token",
                        "sessionToken": "saved-session-token",
                    },
                    "existing_session_cookies": [
                        {
                            "name": "__Secure-next-auth.session-token",
                            "value": "saved-session-token",
                            "domain": "chatgpt.com",
                            "path": "/",
                        }
                    ],
                    "existing_device_id": "saved-device-id",
                    "existing_totp_secret": "JBSWY3DPEHPK3PXP",
                }
            )

        self.assertEqual(
            [item["password_add_reauth"] for item in constructor_accounts],
            [True],
        )
        self.assertEqual(
            [bool(item["reauth_session_token"]) for item in constructor_accounts],
            [True],
        )
        self.assertEqual(
            [bool(item["reauth_session_cookies"]) for item in constructor_accounts],
            [True],
        )
        self.assertEqual(
            [bool(item["reauth_device_id"]) for item in constructor_accounts],
            [True],
        )
        diagnostics = result["registration_diagnostics"]
        self.assertFalse(diagnostics["password_login_verified"])
        self.assertTrue(diagnostics["password_add_reauth_recovered"])
        self.assertEqual(diagnostics["password_add_attempts"], 1)
        self.assertEqual(diagnostics["password_login_verification_attempts"], 0)

    def test_saved_password_session_reauthenticates_before_missing_totp(self):
        constructor_calls = []
        credential_call = {}

        class FakeRegister:
            def __init__(self, account, **kwargs):
                constructor_calls.append(dict(account))
                self.password = account["password"]

            def register(self):
                return {
                    "status": "success",
                    "access_token": "recent-auth-access-token",
                    "session_token": "recent-auth-session-token",
                    "session_json": {"sessionToken": "recent-auth-session-token"},
                    "session_cookies": [],
                    "device_id": "recent-auth-device",
                    "password": self.password,
                    "password_set": True,
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

        with (
            patch(
                "hidemyemail_generator.protocol_registration_worker._load_core",
                return_value=SimpleNamespace(ChatGPTRegister=FakeRegister),
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
                    "existing_password": "ExistingPassword!1",
                    "existing_password_confirmed": True,
                    "existing_access_token": "saved-access-token",
                    "existing_session_token": "saved-session-token",
                }
            )

        self.assertEqual(len(constructor_calls), 1)
        self.assertTrue(constructor_calls[0]["password_confirmed"])
        self.assertEqual(
            credential_call["access_token"], "recent-auth-access-token"
        )
        self.assertTrue(result["registration_diagnostics"]["recent_auth_login"])
        self.assertFalse(
            result["registration_diagnostics"]["resumed_from_password_checkpoint"]
        )

    def test_invalid_auth_step_switches_to_one_login_recovery(self):
        constructor_accounts = []

        class FakeRegister:
            def __init__(self, account, **kwargs):
                constructor_accounts.append(dict(account))
                self.password_checkpoint = kwargs["password_checkpoint_fn"]
                self.password = account["password"] or "StableCandidate!1"

            def register(self):
                if len(constructor_accounts) == 1:
                    self.password_checkpoint(self.password, False)
                    return {
                        "status": "failed",
                        "error": (
                            "account_password_register_failed: invalid_auth_step "
                            "(Invalid authorization step.)"
                        ),
                    }
                return {
                    "status": "success",
                    "access_token": "access-token",
                    "session_json": {},
                    "password": self.password,
                    "password_set": True,
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
                "hidemyemail_generator.protocol_credentials.complete_protocol_credentials",
                side_effect=complete,
            ),
            patch(
                "hidemyemail_generator.protocol_registration_worker._emit_event"
            ) as emit,
        ):
            result = run(
                {
                    "email": "protocol@icloud.com",
                    "code_url": "http://127.0.0.1/code",
                    "project_root": ".",
                    "source_root": ".",
                }
            )

        self.assertEqual(len(constructor_accounts), 2)
        self.assertFalse(constructor_accounts[0]["password_verification_only"])
        self.assertTrue(constructor_accounts[1]["password_verification_only"])
        self.assertTrue(constructor_accounts[1]["password_confirmed"])
        self.assertTrue(
            result["registration_diagnostics"]["invalid_auth_step_recovered"]
        )
        self.assertEqual(
            result["registration_diagnostics"][
                "invalid_auth_step_recovery_attempts"
            ],
            1,
        )
        self.assertTrue(
            any(
                len(call.args) > 1 and "登录/密码重置恢复" in str(call.args[1])
                for call in emit.call_args_list
            )
        )

    def test_password_reset_completion_restarts_login_with_same_password(self):
        constructor_passwords = []
        events = []

        class FakeRegister:
            def __init__(self, account, **_kwargs):
                constructor_passwords.append(account["password"])
                self.password = account["password"]

            def register(self):
                if len(constructor_passwords) == 1:
                    return {
                        "status": "failed",
                        "error": "account_password_reset_completed_retry_login",
                    }
                return {
                    "status": "success",
                    "access_token": "access-token",
                    "session_json": {},
                    "password": self.password,
                    "password_set": True,
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
                side_effect=lambda stage, message, status="active", **_details: events.append(
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
                    "existing_password": "StableCandidate!1",
                    "existing_password_confirmed": True,
                }
            )

        self.assertEqual(
            constructor_passwords,
            ["StableCandidate!1", "StableCandidate!1"],
        )
        self.assertTrue(
            result["registration_diagnostics"]["password_reset_recovered"]
        )
        self.assertTrue(
            any("重新登录" in message for _stage, message, _status in events)
        )

    def test_server_selected_otp_session_adds_password_once_without_login(self):
        constructor_accounts = []
        complete_calls = []
        events = []

        class FakeRegister:
            def __init__(self, account, **kwargs):
                constructor_accounts.append(dict(account))
                self.instance_index = len(constructor_accounts)
                self.password = account["password"] or "StableCandidate!1"
                self.password_checkpoint = kwargs["password_checkpoint_fn"]

            def register(self):
                if self.instance_index > 1:
                    self.password_checkpoint(self.password, True)
                    return {
                        "status": "success",
                        "access_token": "fresh-password-add-token",
                        "session_token": "fresh-password-add-session-token",
                        "session_json": {
                            "accessToken": "fresh-password-add-token",
                            "sessionToken": "fresh-password-add-session-token",
                        },
                        "session_cookies": [
                            {
                                "name": "__Secure-next-auth.session-token",
                                "value": "fresh-password-add-session-token",
                            }
                        ],
                        "password": self.password,
                        "password_set": True,
                    }
                self.password_checkpoint(self.password, False)
                return {
                    "status": "success",
                    "access_token": "passwordless-access-token",
                    "session_token": "passwordless-session-token",
                    "session_json": {
                        "sessionToken": "passwordless-session-token"
                    },
                    "session_cookies": [],
                    "device_id": "passwordless-device",
                    "password": "",
                    "password_set": False,
                }

        def complete(**kwargs):
            complete_calls.append(kwargs)
            verification = kwargs["password_verifier"]()
            self.assertTrue(verification["verified"])
            self.assertFalse(verification["reuse_registration_session"])
            self.assertEqual(
                verification["access_token"], "fresh-password-add-token"
            )
            on_password_confirmed = kwargs.get("on_password_confirmed")
            if callable(on_password_confirmed):
                on_password_confirmed()
            return {
                "access_token": verification["access_token"],
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
                side_effect=lambda stage, message, status="active", **details: events.append(
                    (stage, message, status, details)
                ),
            ),
            patch(
                "hidemyemail_generator.protocol_credentials.complete_protocol_credentials",
                side_effect=complete,
            ),
            patch(
                "hidemyemail_generator.protocol_registration_worker.time.sleep"
            ) as sleep_mock,
        ):
            result = run(
                {
                    "email": "protocol@icloud.com",
                    "code_url": "http://127.0.0.1/code",
                    "project_root": ".",
                    "source_root": ".",
                    "proxy_country": "JP",
                }
            )

        self.assertEqual(len(constructor_accounts), 2)
        self.assertEqual(constructor_accounts[0]["fingerprint_country"], "JP")
        self.assertEqual(constructor_accounts[0]["language"], "ja-JP")
        self.assertEqual(constructor_accounts[0]["timezone"], "Asia/Tokyo")
        self.assertEqual(
            [item["password"] for item in constructor_accounts],
            ["", "StableCandidate!1"],
        )
        self.assertTrue(constructor_accounts[1]["password_verification_only"])
        self.assertTrue(constructor_accounts[1]["password_add_reauth"])
        self.assertEqual(
            constructor_accounts[1]["reauth_access_token"],
            "passwordless-access-token",
        )
        self.assertFalse(constructor_accounts[1]["password_confirmed"])
        self.assertEqual(len(complete_calls), 1)
        self.assertFalse(complete_calls[0]["password_set"])
        self.assertEqual(
            complete_calls[0]["generated_password"],
            "StableCandidate!1",
        )
        self.assertEqual(result["password"], "StableCandidate!1")
        self.assertEqual(result["access_token"], "fresh-password-add-token")
        self.assertEqual(
            result["session_token"], "fresh-password-add-session-token"
        )
        self.assertTrue(
            result["registration_diagnostics"]["password_add_from_session"]
        )
        self.assertFalse(result["registration_diagnostics"]["password_login_verified"])
        self.assertEqual(
            result["registration_diagnostics"]["password_add_attempts"],
            1,
        )
        self.assertEqual(
            result["registration_diagnostics"][
                "password_login_verification_attempts"
            ],
            0,
        )
        sleep_mock.assert_not_called()
        self.assertTrue(
            any(
                stage == "password_checkpoint"
                and status == "success"
                and "账号密码和 Session 已确认并保存" in message
                for stage, message, status, _details in events
            )
        )

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
                    "password": self.password,
                    "password_set": True,
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
                side_effect=lambda stage, message, status="active", **_details: events.append(
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

    def test_init_page_tls_failure_restarts_with_fresh_session_and_fallback_profile(self):
        instances = []
        events = []

        class FakeRegister:
            def __init__(self, account, **kwargs):
                self.password = f"GeneratedPassword!{len(instances) + 1}"
                self.impersonate = kwargs["impersonate"]
                instances.append(self)

            def register(self):
                if len(instances) <= 2:
                    return {
                        "status": "failed",
                        "error": (
                            "init_page_email: SSLError: Failed to perform, "
                            "curl: (35) BoringSSL SSL_connect: Connection closed "
                            "abruptly (SSL_ERROR_SYSCALL; error queue empty) in "
                            "connection to chatgpt.com:443"
                        ),
                    }
                return {
                    "status": "success",
                    "access_token": "access-token",
                    "session_json": {},
                    "password": self.password,
                    "password_set": True,
                    "impersonate": self.impersonate,
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
                side_effect=lambda stage, message, status="active", **_details: events.append(
                    (stage, message, status)
                ),
            ),
            patch(
                "hidemyemail_generator.protocol_credentials.complete_protocol_credentials",
                side_effect=complete,
            ),
            patch(
                "hidemyemail_generator.protocol_registration_worker._choose_protocol_impersonate",
                return_value="firefox144",
            ),
            patch("hidemyemail_generator.protocol_registration_worker.time.sleep") as sleep,
        ):
            result = run(
                {
                    "email": "protocol@icloud.com",
                    "code_url": "http://127.0.0.1/code",
                    "project_root": ".",
                    "source_root": ".",
                }
            )

        self.assertEqual(len(instances), 3)
        self.assertEqual(
            [instance.impersonate for instance in instances],
            ["firefox144", "chrome136", "chrome136"],
        )
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0])
        self.assertEqual(result["password"], "GeneratedPassword!3")
        self.assertEqual(
            result["registration_diagnostics"]["impersonate"], "chrome136"
        )
        self.assertTrue(
            result["registration_diagnostics"]["transient_init_recovered"]
        )
        self.assertEqual(
            [status for stage, _message, status in events if stage == "network"],
            ["active", "warning", "warning"],
        )

    def test_init_page_tls_full_registration_retry_is_bounded(self):
        instances = []

        class FakeRegister:
            def __init__(self, account, **kwargs):
                self.password = "GeneratedPassword!1"
                instances.append(self)

            def register(self):
                return {
                    "status": "failed",
                    "error": (
                        "init_page_email: SSLError: curl: (35) "
                        "BoringSSL SSL_connect: SSL_ERROR_SYSCALL"
                    ),
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
            patch("hidemyemail_generator.protocol_registration_worker.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, r"curl: \(35\)"):
                run(
                    {
                        "email": "protocol@icloud.com",
                        "code_url": "http://127.0.0.1/code",
                        "project_root": ".",
                        "source_root": ".",
                    }
                )

        self.assertEqual(len(instances), 3)

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

    async def test_auth_and_sentinel_fingerprint_follow_proxy_locale(self):
        module = self._core_module()
        provider = module._SentinelWithProxy(
            impersonate="firefox144",
            language="ja-JP",
            timezone_name="Asia/Tokyo",
        )
        profile = provider._impl._browser_profile("device-id")
        self.assertEqual(profile.language, "ja-JP")
        self.assertEqual(profile.timezone, "Asia/Tokyo")
        self.assertIn("Firefox/144.0", profile.user_agent)
        self.assertNotIn("sec-ch-ua", profile.browser_headers())

        client = module.OpenAIAuthClient(
            sentinel=SimpleNamespace(),
            language="ja-JP",
            timezone_name="Asia/Tokyo",
        )
        headers = client._common_headers()
        self.assertEqual(
            headers["accept-language"],
            "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
        )
        self.assertEqual(headers["oai-language"], "ja-JP")

    async def test_sso_step_is_classified_as_terminal_sso_requirement(self):
        module = self._core_module()

        self.assertTrue(module.auth_step_requires_sso("/sso", "sso"))
        self.assertFalse(
            module.auth_step_requires_sso(
                "/email-verification",
                "email_otp_verification",
            )
        )

    async def test_sentinel_firefox_profile_tracks_selected_version(self):
        module = self._core_module()
        provider = module._SentinelWithProxy(impersonate="firefox147")

        profile = provider._impl._browser_profile("device-id")

        self.assertIn("Firefox/147.0", profile.user_agent)
        self.assertIn("rv:147.0", profile.user_agent)
        self.assertNotIn("sec-ch-ua", profile.browser_headers())

    async def test_sentinel_chrome_profile_tracks_selected_version(self):
        module = self._core_module()
        provider = module._SentinelWithProxy(impersonate="chrome145")

        profile = provider._impl._browser_profile("device-id")
        headers = profile.browser_headers()

        self.assertIn("Chrome/145.0.0.0", profile.user_agent)
        self.assertIn('"Chromium";v="145"', headers["sec-ch-ua"])
        self.assertIn('"Google Chrome";v="145"', headers["sec-ch-ua"])
        self.assertEqual(headers["sec-ch-ua-platform"], '"Windows"')

    async def test_register_stages_supplied_password_before_network_flow(self):
        checkpoints = []
        module = self._core_module()
        bot = module.ChatGPTRegister(
            {
                "email": "protocol@icloud.com",
                "password": "StableCandidate!1",
                "password_confirmed": False,
                "refresh_token": "http://127.0.0.1/code",
            },
            password_checkpoint_fn=lambda password, confirmed: checkpoints.append(
                (password, confirmed)
            ),
        )

        async def failed_flow(**_kwargs):
            return {"status": "failed", "error": "stopped-for-test"}

        with patch.object(bot, "_register_async", side_effect=failed_flow):
            result = await asyncio.to_thread(bot.register)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(checkpoints[0], ("StableCandidate!1", False))

    async def test_authorize_existing_email_selects_login_flow_before_verify(self):
        module = self._core_module()

        class FakeResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {
                    "continue_url": "/log-in/password",
                    "page": {"type": "password"},
                }

        class FakeSession:
            def __init__(self):
                self.posts = []

            async def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                return FakeResponse()

        class FakeSentinel:
            def __init__(self):
                self.flows = []

            async def get_token(self, flow, _device_id, **_kwargs):
                self.flows.append(flow)
                return {"p": "proof", "t": "turnstile"}

            async def get_so_token(self, _flow, _device_id):
                return None

        sentinel = FakeSentinel()
        session = FakeSession()
        client = module.OpenAIAuthClient(sentinel=sentinel)
        client._session = session

        result = await client.authorize_existing_email(
            "protocol@icloud.com"
        )

        self.assertEqual(result["_http_status"], 200)
        self.assertEqual(sentinel.flows, ["authorize_continue"])
        url, kwargs = session.posts[0]
        self.assertEqual(
            url,
            "https://auth.openai.com/api/accounts/authorize/continue",
        )
        self.assertEqual(
            kwargs["json"],
            {
                "connection": "password",
                "username": {
                    "kind": "email",
                    "value": "protocol@icloud.com",
                },
                "screen_hint": "login",
            },
        )
        self.assertEqual(
            kwargs["headers"]["referer"],
            "https://auth.openai.com/log-in",
        )

    async def test_authorize_signup_email_submits_account_before_password_choice(self):
        module = self._core_module()

        class FakeResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {
                    "continue_url": "/create-account/password",
                    "page": {"type": "create_account_password"},
                }

        class FakeSession:
            def __init__(self):
                self.posts = []

            async def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                return FakeResponse()

        class FakeSentinel:
            async def get_token(self, flow, _device_id, **_kwargs):
                self.flow = flow
                return {"p": "proof", "t": "turnstile"}

            async def get_so_token(self, _flow, _device_id):
                return None

        sentinel = FakeSentinel()
        session = FakeSession()
        client = module.OpenAIAuthClient(sentinel=sentinel)
        client._session = session

        result = await client.authorize_signup_email(
            "protocol@icloud.com",
            current_url="https://auth.openai.com/api/accounts/authorize",
        )

        self.assertEqual(result["_http_status"], 200)
        self.assertEqual(sentinel.flow, "authorize_continue")
        url, kwargs = session.posts[0]
        self.assertEqual(
            url,
            "https://auth.openai.com/api/accounts/authorize/continue",
        )
        self.assertEqual(
            kwargs["json"],
            {
                "username": {
                    "kind": "email",
                    "value": "protocol@icloud.com",
                },
                "screen_hint": "login_or_signup",
            },
        )
        self.assertEqual(
            kwargs["headers"]["referer"],
            "https://auth.openai.com/api/accounts/authorize",
        )

    async def test_password_reset_protocol_uses_reset_endpoints_and_sentinel_flow(self):
        module = self._core_module()

        class FakeResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"continue_url": "/reset-password/success"}

        class FakeSession:
            def __init__(self):
                self.gets = []
                self.posts = []

            async def get(self, url, **kwargs):
                self.gets.append((url, kwargs))
                return FakeResponse()

            async def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                return FakeResponse()

        class FakeSentinel:
            def __init__(self):
                self.flows = []

            async def get_token(self, flow, _device_id, **_kwargs):
                self.flows.append(flow)
                return {"p": "proof", "t": "turnstile"}

            async def get_so_token(self, _flow, _device_id):
                return None

        sentinel = FakeSentinel()
        session = FakeSession()
        client = module.OpenAIAuthClient(sentinel=sentinel)
        client._session = session

        sent = await client.send_password_reset_otp()
        changed = await client.reset_password("StableCandidate!1")

        self.assertEqual(sent["_http_status"], 200)
        self.assertEqual(changed["_http_status"], 200)
        self.assertEqual(
            session.gets[0][0],
            "https://auth.openai.com/reset-password",
        )
        self.assertEqual(
            session.posts[0][0],
            "https://auth.openai.com/api/accounts/password/send-otp",
        )
        self.assertEqual(
            session.posts[1][0],
            "https://auth.openai.com/api/accounts/password/reset",
        )
        self.assertEqual(
            session.posts[1][1]["json"],
            {"password": "StableCandidate!1"},
        )
        self.assertEqual(sentinel.flows, ["password_reset"])

    async def test_password_add_protocol_uses_auth_endpoint_and_sentinel_flow(self):
        module = self._core_module()

        class FakeResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"continue_url": "/reset-password/success"}

        class FakeSession:
            def __init__(self):
                self.posts = []

            async def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                return FakeResponse()

        class FakeSentinel:
            def __init__(self):
                self.flows = []

            async def get_token(self, flow, _device_id, **_kwargs):
                self.flows.append(flow)
                return {"p": "proof", "t": "turnstile"}

            async def get_so_token(self, _flow, _device_id):
                return None

        sentinel = FakeSentinel()
        session = FakeSession()
        client = module.OpenAIAuthClient(sentinel=sentinel)
        client._session = session

        result = await client.add_password_after_reauth("StableCandidate!1")

        self.assertEqual(result["_http_status"], 200)
        self.assertEqual(sentinel.flows, ["password_reset"])
        url, kwargs = session.posts[0]
        self.assertEqual(
            url,
            "https://auth.openai.com/api/accounts/password/add",
        )
        self.assertEqual(kwargs["json"], {"password": "StableCandidate!1"})
        self.assertEqual(
            kwargs["headers"]["referer"],
            "https://auth.openai.com/reset-password/new-password",
        )
        self.assertIn("openai-sentinel-token", kwargs["headers"])

    async def test_password_login_mfa_challenge_proves_saved_password(self):
        checkpoints = []
        created = []

        class FakeResponse:
            status_code = 200
            url = "https://auth.openai.com/log-in/password"
            headers = {}
            text = ""

            @staticmethod
            def json():
                return {}

        class FakeSession:
            def __init__(self):
                self.cookies = SimpleNamespace(jar=[])
                self.gets = []

            async def get(self, url, **kwargs):
                self.gets.append((url, kwargs))
                if url.endswith("/api/auth/session"):
                    raise AssertionError("MFA proof must stop before Session lookup")
                return FakeResponse()

        class FakeAuth:
            BASE_URL = "https://auth.openai.com"
            CHATGPT_URL = "https://chatgpt.com"

            def __init__(self, **_kwargs):
                self.device_id = "device-id"
                self.session = FakeSession()
                self.sentinel = SimpleNamespace(set_cookies=lambda _cookies: None)
                created.append(self)

            async def share_session_with_sentinel(self):
                return None

            async def init_page_email(self, _email, **kwargs):
                self.init_kwargs = kwargs
                return {
                    "device_id": self.device_id,
                    "cookies": {},
                    "page_path": "/email-verification",
                }

            async def _get_session(self):
                return self.session

            async def authorize_existing_email(self, _email, **_kwargs):
                return {
                    "continue_url": "/email-verification",
                    "page": {"type": "email_otp_verification"},
                    "_http_status": 200,
                }

            async def verify_password_email(self, _password, **_kwargs):
                return {
                    "continue_url": "/mfa-challenge",
                    "page": {"type": "mfa_challenge"},
                    "_http_status": 200,
                }

            async def close(self):
                return None

        module = self._core_module()
        bot = module.ChatGPTRegister(
            {
                "email": "protocol@icloud.com",
                "password": "StableCandidate!1",
                "password_verification_only": True,
                "refresh_token": "http://127.0.0.1/code",
            },
            password_checkpoint_fn=lambda password, confirmed: checkpoints.append(
                (password, confirmed)
            ),
        )

        with (
            patch.object(module, "OpenAIAuthClient", FakeAuth),
            patch.object(
                module,
                "_fetch_otp_sync",
                side_effect=AssertionError(
                    "password verifier must not select passwordless email OTP"
                ),
            ),
        ):
            result = await bot._register_async(
                password="StableCandidate!1",
                name="Test User",
                birthdate="1995-01-01",
                client_id="",
                refresh_token="http://127.0.0.1/code",
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "account_password_verified_mfa_required")
        self.assertEqual(checkpoints, [("StableCandidate!1", True)])
        self.assertEqual(bot.password_confirmed, True)
        self.assertEqual(created[0].init_kwargs, {"prefer_login": True})
        self.assertEqual(
            created[0].session.gets[0][0],
            "https://auth.openai.com/log-in/password",
        )

    async def test_passwordless_account_requests_add_password_reauth_when_fallback_is_unavailable(self):
        reset_calls = []

        class FakeResponse:
            status_code = 400
            url = "https://auth.openai.com/log-in/password"
            headers = {}
            text = ""

            @staticmethod
            def json():
                return {}

        class FakeSession:
            def __init__(self):
                self.cookies = SimpleNamespace(jar=[])

            async def get(self, url, **_kwargs):
                return FakeResponse()

        class FakeAuth:
            BASE_URL = "https://auth.openai.com"
            CHATGPT_URL = "https://chatgpt.com"

            def __init__(self, **_kwargs):
                self.device_id = "device-id"
                self.session = FakeSession()
                self.sentinel = SimpleNamespace(set_cookies=lambda _cookies: None)

            async def share_session_with_sentinel(self):
                return None

            async def init_page_email(self, _email, **_kwargs):
                return {
                    "device_id": self.device_id,
                    "cookies": {},
                    "page_path": "/email-verification",
                }

            async def _get_session(self):
                return self.session

            async def authorize_existing_email(self, _email, **_kwargs):
                return {
                    "continue_url": "/email-verification",
                    "page": {"type": "email_otp_verification"},
                    "_http_status": 200,
                }

            async def verify_password_email(self, *_args, **_kwargs):
                raise AssertionError("password verify must wait until reset completes")

            async def close(self):
                return None

        module = self._core_module()
        bot = module.ChatGPTRegister(
            {
                "email": "protocol@icloud.com",
                "password": "StableCandidate!1",
                "password_verification_only": True,
                "refresh_token": "http://127.0.0.1/code",
            },
        )

        async def reset_password(_auth, *, password, client_id, refresh_token):
            reset_calls.append((password, client_id, refresh_token))
            return True, ""

        bot._reset_existing_password = reset_password
        with (
            patch.object(module, "OpenAIAuthClient", FakeAuth),
            patch.object(
                module,
                "_fetch_otp_sync",
                side_effect=AssertionError("login OTP must not be selected"),
            ),
        ):
            result = await bot._register_async(
                password="StableCandidate!1",
                name="Test User",
                birthdate="1995-01-01",
                client_id="",
                refresh_token="http://127.0.0.1/code",
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["error"],
            "account_password_add_reauth_required",
        )
        self.assertEqual(reset_calls, [])

    async def test_add_password_reauth_completes_email_otp_totp_and_password_add(self):
        created = []

        class FakeResponse:
            def __init__(self, payload=None, *, url="https://auth.openai.com/"):
                self.status_code = 200
                self.url = url
                self.headers = {}
                self.text = ""
                self.payload = payload or {}

            def json(self):
                return self.payload

        class FakeSession:
            def __init__(self):
                self.cookies = SimpleNamespace(jar=[])
                self.gets = []

            async def get(self, url, **kwargs):
                self.gets.append((url, kwargs))
                if url == "https://chatgpt.com/api/auth/session":
                    return FakeResponse(
                        {
                            "accessToken": "fresh-password-add-token",
                            "sessionToken": "fresh-session-token",
                        },
                        url=url,
                    )
                return FakeResponse(url=url)

        class FakeAuth:
            BASE_URL = "https://auth.openai.com"
            CHATGPT_URL = "https://chatgpt.com"

            def __init__(self, **_kwargs):
                self.device_id = "device-id"
                self.session = FakeSession()
                self.sentinel = SimpleNamespace(set_cookies=lambda _cookies: None)
                self.init_kwargs = {}
                self.validated_codes = []
                self.issued = []
                self.verified = []
                self.added_passwords = []
                created.append(self)

            async def share_session_with_sentinel(self):
                return None

            async def init_page_email(self, _email, **kwargs):
                self.init_kwargs = kwargs
                return {
                    "device_id": self.device_id,
                    "cookies": {},
                    "page_path": "/email-verification",
                }

            async def _get_session(self):
                return self.session

            async def authorize_existing_email(self, _email, **_kwargs):
                return {
                    "continue_url": "/email-verification",
                    "page": {"type": "email_otp_verification"},
                    "_http_status": 200,
                }

            async def validate_email_otp(self, code):
                self.validated_codes.append(code)
                return {
                    "continue_url": "/mfa-challenge/factor-id",
                    "page": {
                        "type": "mfa_challenge",
                        "payload": {
                            "factor_id": "factor-id",
                            "factors": [
                                {"id": "factor-id", "factor_type": "totp"}
                            ],
                        },
                    },
                    "_http_status": 200,
                }

            async def issue_mfa_challenge(
                self, factor_id, *, factor_type, mfa_request_id
            ):
                self.issued.append((factor_id, factor_type, mfa_request_id))
                return {"_http_status": 200}

            async def verify_mfa_challenge(
                self, factor_id, code, *, factor_type, mfa_request_id
            ):
                self.verified.append(
                    (factor_id, code, factor_type, mfa_request_id)
                )
                return {
                    "continue_url": "/reset-password/new-password",
                    "page": {"type": "password_reset"},
                    "_http_status": 200,
                }

            async def add_password_after_reauth(self, password, **_kwargs):
                self.added_passwords.append(password)
                return {
                    "continue_url": "https://auth.openai.com/authorize/continue",
                    "page": {"type": "external_url"},
                    "_http_status": 200,
                }

            async def close(self):
                return None

        module = self._core_module()
        bot = module.ChatGPTRegister(
            {
                "email": "protocol@icloud.com",
                "password": "StableCandidate!1",
                "password_verification_only": True,
                "password_add_reauth": True,
                "reauth_access_token": "stale-passwordless-token",
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "refresh_token": "http://127.0.0.1/code",
            },
        )
        with (
            patch.object(module, "OpenAIAuthClient", FakeAuth),
            patch.object(module, "_fetch_otp_sync", return_value="123456"),
        ):
            result = await bot._register_async(
                password="StableCandidate!1",
                name="Test User",
                birthdate="1995-01-01",
                client_id="",
                refresh_token="http://127.0.0.1/code",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["access_token"], "fresh-password-add-token")
        self.assertEqual(result["session_token"], "fresh-session-token")
        self.assertTrue(result["password_set"])
        self.assertEqual(len(created), 1)
        self.assertEqual(
            created[0].init_kwargs,
            {"prefer_login": True, "post_login_add_password": True},
        )
        self.assertEqual(created[0].validated_codes, ["123456"])
        self.assertEqual(created[0].issued, [("factor-id", "totp", "")])
        self.assertEqual(len(created[0].verified), 1)
        self.assertEqual(created[0].added_passwords, ["StableCandidate!1"])

    async def test_password_add_reauth_409_falls_back_to_email_reset_strategy(self):
        class FakeResponse:
            status_code = 200

        class FakeSession:
            async def get(self, _url, **_kwargs):
                return FakeResponse()

        class FakeAuth:
            BASE_URL = "https://auth.openai.com"

            def __init__(self):
                self.session = FakeSession()
                self.add_force_refresh_flags = []

            async def _get_session(self):
                return self.session

            async def add_password_after_reauth(
                self, _password, *, force_refresh_sentinel, log_fn
            ):
                self.add_force_refresh_flags.append(force_refresh_sentinel)
                self.assert_log = log_fn
                return {"_http_status": 409}

        module = self._core_module()
        bot = module.ChatGPTRegister(
            {
                "email": "protocol@zkgmail.com",
                "password": "StableCandidate!1",
                "refresh_token": "zkg-code-url",
            }
        )
        auth = FakeAuth()
        reset_calls = []

        async def reset_password(
            _auth,
            *,
            password,
            client_id,
            refresh_token,
            recovery_reason="password_mismatch",
        ):
            reset_calls.append(
                (password, client_id, refresh_token, recovery_reason)
            )
            return True, ""

        bot._reset_existing_password = reset_password
        strategy, error, add_result = await bot._complete_password_add_reauth(
            auth,
            auth_result={
                "continue_url": "/reset-password/new-password",
                "page": {"type": "password_reset"},
            },
            password="StableCandidate!1",
            client_id="zkg-client",
            refresh_token="zkg-code-url",
        )

        self.assertEqual(strategy, module.PASSWORD_WRITE_RESET)
        self.assertEqual(error, "")
        self.assertEqual(add_result, {"_http_status": 409})
        self.assertEqual(auth.add_force_refresh_flags, [False])
        self.assertEqual(
            reset_calls,
            [
                (
                    "StableCandidate!1",
                    "zkg-client",
                    "zkg-code-url",
                    "password_add_conflict",
                )
            ],
        )
        self.assertTrue(bot.password_confirmed)

    async def test_password_add_reauth_401_and_403_do_not_fallback_to_reset(self):
        class FakeResponse:
            status_code = 200

        class FakeSession:
            async def get(self, _url, **_kwargs):
                return FakeResponse()

        async def no_sleep(_seconds):
            return None

        module = self._core_module()
        for status in (401, 403):
            with self.subTest(status=status):
                class FakeAuth:
                    BASE_URL = "https://auth.openai.com"

                    def __init__(self):
                        self.session = FakeSession()
                        self.add_force_refresh_flags = []

                    async def _get_session(self):
                        return self.session

                    async def add_password_after_reauth(
                        self, _password, *, force_refresh_sentinel, log_fn
                    ):
                        self.add_force_refresh_flags.append(force_refresh_sentinel)
                        self.assert_log = log_fn
                        return {
                            "_http_status": status,
                            "error": {
                                "code": "unauthorized",
                                "message": "password already exists",
                            },
                        }

                bot = module.ChatGPTRegister(
                    {
                        "email": "protocol@zkgmail.com",
                        "password": "StableCandidate!1",
                    }
                )
                auth = FakeAuth()
                reset_calls = []

                async def reset_password(*_args, **_kwargs):
                    reset_calls.append(True)
                    return True, ""

                bot._reset_existing_password = reset_password
                with patch.object(module.asyncio, "sleep", new=no_sleep):
                    strategy, error, add_result = (
                        await bot._complete_password_add_reauth(
                        auth,
                        auth_result={
                            "continue_url": "/reset-password/new-password",
                            "page": {"type": "password_reset"},
                        },
                        password="StableCandidate!1",
                        client_id="",
                        refresh_token="zkg-code-url",
                        )
                    )

                self.assertEqual(strategy, "")
                self.assertEqual(reset_calls, [])
                self.assertFalse(bot.password_confirmed)
                self.assertIn("password_add_reauth_failed", error)
                self.assertIn(str(status), error)
                self.assertEqual(add_result["_http_status"], status)
                self.assertEqual(
                    auth.add_force_refresh_flags,
                    [False, True] if status == 403 else [False],
                )

    async def test_password_add_409_reset_failure_preserves_detail(self):
        class FakeResponse:
            status_code = 200

        class FakeSession:
            async def get(self, _url, **_kwargs):
                return FakeResponse()

        class FakeAuth:
            BASE_URL = "https://auth.openai.com"

            async def _get_session(self):
                return FakeSession()

            async def add_password_after_reauth(self, _password, **_kwargs):
                return {"_http_status": 409}

        module = self._core_module()
        bot = module.ChatGPTRegister(
            {
                "email": "protocol@zkgmail.com",
                "password": "StableCandidate!1",
            }
        )
        reset_detail = (
            "password_reset_failed: password_policy_violation "
            "(Password rejected by server)"
        )

        async def reset_password(*_args, **_kwargs):
            return False, reset_detail

        bot._reset_existing_password = reset_password
        strategy, error, add_result = await bot._complete_password_add_reauth(
            FakeAuth(),
            auth_result={
                "continue_url": "/reset-password/new-password",
                "page": {"type": "password_reset"},
            },
            password="StableCandidate!1",
            client_id="",
            refresh_token="zkg-code-url",
        )

        self.assertEqual(strategy, "")
        self.assertEqual(error, reset_detail)
        self.assertEqual(add_result, {"_http_status": 409})
        self.assertFalse(bot.password_confirmed)

    async def test_existing_password_page_verifies_saved_candidate_instead_of_registering(self):
        checkpoints = []

        class FakeResponse:
            def __init__(self, payload=None, *, url="https://chatgpt.com/"):
                self.status_code = 200
                self.url = url
                self.headers = {}
                self._payload = payload or {}
                self.text = json.dumps(self._payload)

            def json(self):
                return self._payload

        class FakeCookies:
            jar = []

        class FakeSession:
            def __init__(self):
                self.cookies = FakeCookies()
                self.urls = []

            async def get(self, url, **_kwargs):
                self.urls.append(url)
                if url.endswith("/api/auth/session"):
                    return FakeResponse(
                        {
                            "accessToken": "saved-access-token",
                            "sessionToken": "saved-session-token",
                        },
                        url=url,
                    )
                return FakeResponse(url=url)

        class FakeAuth:
            BASE_URL = "https://auth.openai.com"
            CHATGPT_URL = "https://chatgpt.com"

            def __init__(self, **_kwargs):
                self.device_id = "device-id"
                self.session = FakeSession()
                self.sentinel = SimpleNamespace(set_cookies=lambda _cookies: None)
                self.authorized_emails = []
                self.verified_passwords = []
                self.init_kwargs = {}

            async def share_session_with_sentinel(self):
                return None

            async def init_page_email(self, _email, **kwargs):
                self.init_kwargs = kwargs
                return {
                    "device_id": self.device_id,
                    "cookies": {},
                    # The first redirect is not a reliable account-state signal.
                    "page_path": "/email-verification",
                }

            async def _get_session(self):
                return self.session

            async def authorize_existing_email(self, email, **_kwargs):
                self.authorized_emails.append(email)
                return {
                    "continue_url": "/log-in/password",
                    "page": {"type": "password"},
                    "_http_status": 200,
                }

            async def verify_password_email(self, password, **_kwargs):
                self.verified_passwords.append(password)
                return {
                    "continue_url": "https://chatgpt.com/api/auth/callback/openai",
                    "page": {"type": "external_url"},
                    "_http_status": 200,
                }

            async def navigate_password_registration(self):
                raise AssertionError("existing login must not enter create-account/password")

            async def register_password_email(self, *_args, **_kwargs):
                raise AssertionError("existing login must not register a new password")

            async def close(self):
                return None

        created = []

        class RecordingAuth(FakeAuth):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                created.append(self)

        module = self._core_module()
        bot = module.ChatGPTRegister(
            {
                "email": "protocol@icloud.com",
                "password": "StableCandidate!1",
                "password_confirmed": True,
                "refresh_token": "http://127.0.0.1/code",
            },
            password_checkpoint_fn=lambda password, confirmed: checkpoints.append(
                (password, confirmed)
            ),
        )

        with patch.object(module, "OpenAIAuthClient", RecordingAuth):
            result = await bot._register_async(
                password="StableCandidate!1",
                name="Test User",
                birthdate="1995-01-01",
                client_id="",
                refresh_token="http://127.0.0.1/code",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["password"], "StableCandidate!1")
        self.assertTrue(result["password_set"])
        self.assertEqual(result["raw"]["recovered_existing_session"], "saved_password_login")
        self.assertEqual(created[0].authorized_emails, ["protocol@icloud.com"])
        self.assertEqual(created[0].verified_passwords, ["StableCandidate!1"])
        self.assertTrue(created[0].init_kwargs["prefer_login"])
        self.assertIn(
            created[0].init_kwargs.get("post_login_add_password"),
            {None, False},
        )
        self.assertEqual(checkpoints[-1], ("StableCandidate!1", True))

    async def test_wrong_saved_password_runs_reset_before_fresh_login_retry(self):
        checkpoints = []

        class FakeResponse:
            status_code = 200
            url = "https://auth.openai.com/reset-password/new-password"
            headers = {}
            text = ""

            @staticmethod
            def json():
                return {}

        class FakeSession:
            cookies = SimpleNamespace(jar=[])

            def __init__(self):
                self.urls = []

            async def get(self, url, **_kwargs):
                self.urls.append(url)
                return FakeResponse()

        class FakeAuth:
            BASE_URL = "https://auth.openai.com"
            CHATGPT_URL = "https://chatgpt.com"

            def __init__(self, **_kwargs):
                self.device_id = "device-id"
                self.session = FakeSession()
                self.sentinel = SimpleNamespace(set_cookies=lambda _cookies: None)
                self.reset_passwords = []
                self.verify_count = 0

            async def share_session_with_sentinel(self):
                return None

            async def init_page_email(self, _email, **_kwargs):
                return {
                    "device_id": self.device_id,
                    "cookies": {},
                    "page_path": "/email-verification",
                }

            async def _get_session(self):
                return self.session

            async def authorize_existing_email(self, _email, **_kwargs):
                return {
                    "continue_url": "/email-verification",
                    "page": {"type": "email_otp_verification"},
                    "_http_status": 200,
                }

            async def verify_password_email(self, _password, **_kwargs):
                self.verify_count += 1
                return {
                    "error": {
                        "code": "invalid_username_or_password",
                        "message": "Login failed.",
                    },
                    "_http_status": 400,
                }

            async def send_password_reset_otp(self):
                return {"_http_status": 200}

            async def validate_email_otp(self, code):
                self.validated_code = code
                return {
                    "continue_url": "/reset-password/new-password",
                    "_http_status": 200,
                }

            async def reset_password(self, password, **_kwargs):
                self.reset_passwords.append(password)
                return {
                    "continue_url": "/reset-password/success",
                    "_http_status": 200,
                }

            async def close(self):
                return None

        created = []

        class RecordingAuth(FakeAuth):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                created.append(self)

        module = self._core_module()
        bot = module.ChatGPTRegister(
            {
                "email": "protocol@icloud.com",
                "password": "StableCandidate!1",
                "password_confirmed": True,
                "refresh_token": "http://127.0.0.1/code",
            },
            password_checkpoint_fn=lambda password, confirmed: checkpoints.append(
                (password, confirmed)
            ),
        )

        with (
            patch.object(module, "OpenAIAuthClient", RecordingAuth),
            patch.object(module, "_fetch_otp_sync", return_value="123456"),
        ):
            result = await bot._register_async(
                password="StableCandidate!1",
                name="Test User",
                birthdate="1995-01-01",
                client_id="",
                refresh_token="http://127.0.0.1/code",
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "account_password_reset_completed_retry_login",
            result["error"],
        )
        self.assertEqual(created[0].verify_count, 2)
        self.assertEqual(created[0].validated_code, "123456")
        self.assertEqual(created[0].reset_passwords, ["StableCandidate!1"])
        self.assertEqual(checkpoints[-1], ("StableCandidate!1", True))

    async def test_existing_login_completes_server_selected_email_otp_before_session(self):
        checkpoints = []

        class FakeResponse:
            def __init__(self, payload=None, *, url="https://chatgpt.com/"):
                self.status_code = 200
                self.url = url
                self.headers = {}
                self._payload = payload or {}
                self.text = json.dumps(self._payload)

            def json(self):
                return self._payload

        class FakeSession:
            cookies = SimpleNamespace(jar=[])

            async def get(self, url, **_kwargs):
                if url.endswith("/api/auth/session"):
                    return FakeResponse(
                        {
                            "accessToken": "recent-auth-access-token",
                            "sessionToken": "recent-auth-session-token",
                        },
                        url=url,
                    )
                return FakeResponse(url=url)

        class FakeAuth:
            BASE_URL = "https://auth.openai.com"
            CHATGPT_URL = "https://chatgpt.com"

            def __init__(self, **_kwargs):
                self.device_id = "device-id"
                self.session = FakeSession()
                self.sentinel = SimpleNamespace(set_cookies=lambda _cookies: None)
                self.validated_codes = []
                self.resend_count = 0

            async def share_session_with_sentinel(self):
                return None

            async def init_page_email(self, _email, **_kwargs):
                return {
                    "device_id": self.device_id,
                    "cookies": {},
                    "page_path": "/email-verification",
                }

            async def _get_session(self):
                return self.session

            async def authorize_existing_email(self, _email, **_kwargs):
                return {
                    "continue_url": "/email-verification",
                    "page": {"type": "email_otp_verification"},
                    "_http_status": 200,
                }

            async def validate_email_otp(self, code):
                self.validated_codes.append(code)
                if len(self.validated_codes) == 1:
                    return {
                        "error": {
                            "code": "invalid_otp",
                            "message": "The email OTP is invalid",
                        },
                        "_http_status": 400,
                    }
                return {
                    "continue_url": "https://chatgpt.com/api/auth/callback/openai",
                    "page": {"type": "external_url"},
                    "_http_status": 200,
                }

            async def send_email_otp(self):
                self.resend_count += 1
                return {"_http_status": 200}

            async def verify_password_email(self, *_args, **_kwargs):
                raise AssertionError("email OTP callback must finish before password verify")

            async def close(self):
                return None

        created = []

        class RecordingAuth(FakeAuth):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                created.append(self)

        module = self._core_module()
        bot = module.ChatGPTRegister(
            {
                "email": "protocol@icloud.com",
                "password": "StableCandidate!1",
                "password_confirmed": True,
                "refresh_token": "http://127.0.0.1/code",
            },
            password_checkpoint_fn=lambda password, confirmed: checkpoints.append(
                (password, confirmed)
            ),
        )

        with (
            patch.object(module, "OpenAIAuthClient", RecordingAuth),
            patch.object(
                module,
                "_fetch_otp_sync",
                side_effect=["111111", "111111", "222222"],
            ),
        ):
            result = await bot._register_async(
                password="StableCandidate!1",
                name="Test User",
                birthdate="1995-01-01",
                client_id="",
                refresh_token="http://127.0.0.1/code",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            result["raw"]["recovered_existing_session"],
            "saved_password_email_otp",
        )
        self.assertEqual(created[0].validated_codes, ["111111", "222222"])
        self.assertEqual(created[0].resend_count, 1)
        self.assertEqual(checkpoints[-1], ("StableCandidate!1", True))

    def test_auth_next_step_accepts_current_response_shapes(self):
        module = self._core_module()
        cases = (
            (
                {"external_url": "https://auth.openai.com/authorize/continue"},
                "https://auth.openai.com/authorize/continue",
            ),
            (
                {"page": {"url": "https://auth.openai.com/about-you"}},
                "https://auth.openai.com/about-you",
            ),
            (
                {
                    "page": {
                        "type": "external_url",
                        "payload": {
                            "url": "https://chatgpt.com/api/auth/callback/openai"
                        },
                    }
                },
                "https://chatgpt.com/api/auth/callback/openai",
            ),
        )
        for payload, expected_url in cases:
            with self.subTest(payload=payload):
                continue_url, _page_type = module.auth_next_step(payload)
                self.assertEqual(continue_url, expected_url)

        self.assertTrue(
            module.auth_step_is_direct_oauth(
                "https://auth.openai.com/authorize/continue", ""
            )
        )
        self.assertFalse(
            module.auth_step_is_direct_oauth(
                "https://auth.openai.com/about-you", "external_url"
            )
        )

    def test_registration_cookie_export_preserves_cloudflare_context(self):
        module = self._core_module()
        session = SimpleNamespace(
            cookies=SimpleNamespace(
                jar=[
                    SimpleNamespace(
                        name="__cf_bm",
                        value="registration-cookie",
                        domain=".chatgpt.com",
                        path="/",
                        secure=True,
                    )
                ]
            )
        )

        self.assertEqual(
            module.session_cookie_records(session),
            [
                {
                    "name": "__cf_bm",
                    "value": "registration-cookie",
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "secure": True,
                }
            ],
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
        self.assertIn("login_hint=protocol%40icloud.com", signin_url)
        self.assertEqual(signin_kwargs["headers"]["origin"], "https://chatgpt.com")
        self.assertEqual(result["page_path"], "/email-verification")
        self.assertEqual(session.gets[-1][0], "https://auth.openai.com/email-verification")

        password_first_client = module.OpenAIAuthClient()
        password_first_session = FakeSession()
        password_first_client._session = password_first_session
        await password_first_client.init_page_email(
            "protocol@icloud.com",
            prefer_password_signup=True,
        )
        password_first_url = password_first_session.posts[0][0]
        self.assertNotIn("login_hint=", password_first_url)
        self.assertIn("screen_hint=login_or_signup", password_first_url)

        reauth_client = module.OpenAIAuthClient()
        reauth_session = FakeSession()
        reauth_client._session = reauth_session
        await reauth_client.init_page_email(
            "protocol@icloud.com",
            prefer_login=True,
            post_login_add_password=True,
        )
        reauth_url = reauth_session.posts[0][0]
        self.assertIn("post_login_add_password=true", reauth_url)
        self.assertIn("connection=password", reauth_url)
        self.assertIn("login_hint=protocol%40icloud.com", reauth_url)
        self.assertIn("reauth=password", reauth_url)
        self.assertIn("max_age=0", reauth_url)
        self.assertIn("screen_hint=login", reauth_url)

    async def test_add_password_reauth_seeds_logged_in_session_cookies(self):
        class CookieStore:
            def __init__(self):
                self.values = []

            def set(self, name, value, **options):
                self.values.append((name, value, options))

        fake_session = SimpleNamespace(cookies=CookieStore())
        module = self._core_module()
        client = module.OpenAIAuthClient(
            initial_session_token="saved-session-token",
            initial_session_cookies=[
                {
                    "name": "oai-did",
                    "value": "saved-device-id",
                    "domain": "chatgpt.com",
                    "path": "/",
                }
            ],
            device_id="saved-device-id",
        )
        with patch(
            "curl_cffi.requests.AsyncSession",
            return_value=fake_session,
        ) as constructor:
            session = await client._get_session()

        self.assertIs(session, fake_session)
        constructor_headers = constructor.call_args.kwargs["headers"]
        self.assertIn("Windows NT 10.0", constructor_headers["user-agent"])
        self.assertEqual(
            constructor_headers["sec-ch-ua-platform"],
            '"Windows"',
        )
        self.assertEqual(client.device_id, "saved-device-id")
        self.assertIn(
            (
                "oai-did",
                "saved-device-id",
                {"domain": "chatgpt.com", "path": "/"},
            ),
            fake_session.cookies.values,
        )
        self.assertIn(
            (
                "__Secure-next-auth.session-token",
                "saved-session-token",
                {"domain": "chatgpt.com", "path": "/"},
            ),
            fake_session.cookies.values,
        )

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

    async def test_account_chooser_honors_server_email_otp_without_password_post(self):
        events = []

        class FakeResponse:
            def __init__(self, *, url, payload=None, status=200):
                self.url = url
                self._payload = payload or {}
                self.status_code = status
                self.text = ""

            def json(self):
                return self._payload

        class FakeSession:
            def __init__(self):
                self.cookies = SimpleNamespace(jar=[])

            async def get(self, url, **_kwargs):
                if url.endswith("/create-account/password"):
                    events.append("password_page")
                    return FakeResponse(url=url)
                if url.endswith("/email-verification"):
                    events.append("otp_page")
                    return FakeResponse(url=url)
                if url.endswith("/api/accounts/email-otp/send"):
                    events.append("otp_send")
                    return FakeResponse(
                        url="https://auth.openai.com/email-verification"
                    )
                if url.endswith("/authorize/continue"):
                    events.append("oauth_continue")
                    return FakeResponse(url="https://chatgpt.com/")
                if url.endswith("/api/auth/session"):
                    events.append("session")
                    return FakeResponse(
                        url=url,
                        payload={
                            "accessToken": "header.payload.signature",
                            "sessionToken": "password-session-token",
                        },
                    )
                return FakeResponse(url=url)

        class FakeAuth:
            BASE_URL = "https://auth.openai.com"
            CHATGPT_URL = "https://chatgpt.com"

            def __init__(self, **_kwargs):
                self.device_id = "password-device"
                self.sentinel = SimpleNamespace(set_cookies=lambda _cookies: None)
                self.session = FakeSession()

            async def share_session_with_sentinel(self):
                return None

            async def init_page_email(self, _email, **_kwargs):
                events.append("init")
                return {
                    "device_id": self.device_id,
                    "cookies": {},
                    "page_path": "/log-in-or-create-account",
                    "page_url": (
                        "https://auth.openai.com/log-in-or-create-account"
                    ),
                }

            async def _get_session(self):
                return self.session

            async def authorize_signup_email(
                self,
                email,
                *,
                current_url,
                **_kwargs,
            ):
                events.append(("account_next", email, current_url))
                return {
                    "continue_url": "/email-verification",
                    "page": {"type": "email_otp_verification"},
                    "_http_status": 200,
                }

            async def navigate_password_registration(self):
                raise AssertionError("email OTP state must not force password navigation")

            async def register_password_email(self, email, password, **_kwargs):
                raise AssertionError("email OTP state must not replay password register")

            async def validate_email_otp(self, code):
                events.append(("otp_validate", code))
                return {
                    "external_url": "https://auth.openai.com/authorize/continue",
                    "page": {"type": "external_url"},
                }

            async def send_email_otp(self):
                raise AssertionError("password register continue_url must send the OTP")

            async def create_account(self, *_args, **_kwargs):
                raise AssertionError("direct OAuth must skip create_account")

            async def close(self):
                return None

        def receive_otp(*_args, **_kwargs):
            events.append("otp_receive")
            return "123456"

        module = self._core_module()
        bot = module.ChatGPTRegister(
            {
                "email": "protocol@icloud.com",
                "refresh_token": "http://127.0.0.1/code",
            },
            with_password=True,
        )
        with (
            patch.object(module, "OpenAIAuthClient", FakeAuth),
            patch.object(module, "_fetch_otp_sync", side_effect=receive_otp),
        ):
            result = await bot._register_async(
                password="GeneratedPassword!1",
                name="Test User",
                birthdate="1995-01-01",
                client_id="",
                refresh_token="http://127.0.0.1/code",
            )

        account_next_event = (
            "account_next",
            "protocol@icloud.com",
            "https://auth.openai.com/log-in-or-create-account",
        )
        self.assertLess(events.index(account_next_event), events.index("otp_page"))
        self.assertLess(events.index("otp_page"), events.index("otp_receive"))
        self.assertLess(events.index("otp_receive"), events.index(("otp_validate", "123456")))
        self.assertLess(events.index(("otp_validate", "123456")), events.index("oauth_continue"))
        self.assertNotIn("password_page", events)
        self.assertFalse(
            any(
                isinstance(event, tuple) and event[0] == "password_register"
                for event in events
            )
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["password"], "")
        self.assertFalse(result["password_set"])

    async def test_email_otp_initial_page_is_honored_without_password_replay(self):
        logs = []
        events = []

        class FakeResponse:
            def __init__(self, url, payload=None):
                self.url = url
                self.status_code = 200
                self._payload = payload or {}
                self.text = ""

            def json(self):
                return self._payload

        class FakeSession:
            cookies = SimpleNamespace(jar=[])

            async def get(self, url, **_kwargs):
                if url.endswith("/api/auth/session"):
                    return FakeResponse(
                        url,
                        {
                            "accessToken": "header.payload.signature",
                            "sessionToken": "passwordless-session",
                        },
                    )
                return FakeResponse("https://chatgpt.com/")

        class FakeAuth:
            BASE_URL = "https://auth.openai.com"
            CHATGPT_URL = "https://chatgpt.com"

            def __init__(self, **_kwargs):
                self.device_id = "passwordless-device"
                self.sentinel = SimpleNamespace(set_cookies=lambda _cookies: None)
                self.session = FakeSession()

            async def share_session_with_sentinel(self):
                return None

            async def init_page_email(self, _email, **kwargs):
                events.append(("init", kwargs))
                return {
                    "device_id": self.device_id,
                    "cookies": {},
                    "page_path": "/email-verification",
                }

            async def navigate_password_registration(self):
                raise AssertionError("email OTP state must not force password navigation")

            async def register_password_email(self, email, password, **_kwargs):
                raise AssertionError("email OTP state must not replay password register")

            async def validate_email_otp(self, _code):
                events.append("otp_validate")
                return {
                    "page": {
                        "type": "external_url",
                        "payload": {
                            "url": "https://auth.openai.com/authorize/continue"
                        },
                    }
                }

            async def _get_session(self):
                return self.session

            async def send_email_otp(self):
                raise AssertionError("password response URL must send the OTP")

            async def create_account(self, *_args, **_kwargs):
                raise AssertionError("direct OAuth must skip create_account")

            async def close(self):
                return None

        module = self._core_module()
        bot = module.ChatGPTRegister(
            {
                "email": "protocol@icloud.com",
                "refresh_token": "http://127.0.0.1/code",
            },
            log_fn=logs.append,
            with_password=True,
        )
        with (
            patch.object(module, "OpenAIAuthClient", FakeAuth),
            patch.object(
                module,
                "_fetch_otp_sync",
                side_effect=lambda *_args, **_kwargs: events.append("otp_receive")
                or "123456",
            ),
        ):
            result = await bot._register_async(
                password="GeneratedPassword!1",
                name="Test User",
                birthdate="1995-01-01",
                client_id="",
                refresh_token="http://127.0.0.1/code",
            )

        self.assertEqual(result["status"], "success")
        self.assertFalse(result["password_set"])
        self.assertEqual(result["password"], "")
        self.assertTrue(events[0][1]["prefer_password_signup"])
        self.assertLess(events.index("otp_receive"), events.index("otp_validate"))
        self.assertNotIn("password_page", events)
        self.assertFalse(
            any(
                isinstance(event, tuple) and event[0] == "password_register"
                for event in events
            )
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

            async def init_page_email(self, _email, **_kwargs):
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

    async def test_invalid_otp_resends_then_follows_authorize_continue(self):
        created = []

        class FakeResponse:
            def __init__(self, *, url, payload=None, status=200):
                self.url = url
                self._payload = payload or {}
                self.status_code = status
                self.text = ""

            def json(self):
                return self._payload

        class FakeSession:
            def __init__(self):
                self.cookies = SimpleNamespace(jar=[])
                self.gets = []

            async def get(self, url, **kwargs):
                self.gets.append((url, kwargs))
                if url.endswith("/api/auth/session"):
                    return FakeResponse(
                        url=url,
                        payload={
                            "accessToken": "header.payload.signature",
                            "sessionToken": "retry-session-token",
                        },
                    )
                return FakeResponse(url="https://chatgpt.com/")

        class FakeAuth:
            BASE_URL = "https://auth.openai.com"
            CHATGPT_URL = "https://chatgpt.com"

            def __init__(self, **_kwargs):
                self.device_id = "retry-device"
                self.sentinel = SimpleNamespace(set_cookies=lambda _cookies: None)
                self.session = FakeSession()
                self.validated_codes = []
                self.resend_count = 0
                created.append(self)

            async def share_session_with_sentinel(self):
                return None

            async def init_page_email(self, _email, **_kwargs):
                return {
                    "device_id": self.device_id,
                    "cookies": {},
                    "page_path": "/email-verification",
                }

            async def validate_email_otp(self, code):
                self.validated_codes.append(code)
                if len(self.validated_codes) == 1:
                    return {
                        "error": {
                            "code": "invalid_otp",
                            "message": "The email OTP is invalid",
                        }
                    }
                return {
                    "external_url": "https://auth.openai.com/authorize/continue"
                }

            async def send_email_otp(self):
                self.resend_count += 1
                return {"_http_status": 200}

            async def _get_session(self):
                return self.session

            async def create_account(self, *_args, **_kwargs):
                raise AssertionError("create_account must not run after direct OAuth")

            async def close(self):
                return None

        module = self._core_module()
        bot = module.ChatGPTRegister(
            {
                "email": "protocol@icloud.com",
                "refresh_token": "http://127.0.0.1/code",
            },
            with_password=False,
        )
        with (
            patch.object(module, "OpenAIAuthClient", FakeAuth),
            patch.object(
                module,
                "_fetch_otp_sync",
                side_effect=["111111", "222222"],
            ),
        ):
            result = await bot._register_async(
                password="GeneratedPassword!1",
                name="Test User",
                birthdate="1995-01-01",
                client_id="",
                refresh_token="http://127.0.0.1/code",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(created[0].validated_codes, ["111111", "222222"])
        self.assertEqual(created[0].resend_count, 1)
        self.assertEqual(
            created[0].session.gets[0][0],
            "https://auth.openai.com/authorize/continue",
        )

    async def test_deactivated_account_stops_before_resend_or_create(self):
        created = []

        class FakeAuth:
            def __init__(self, **_kwargs):
                self.device_id = "dead-device"
                self.sentinel = SimpleNamespace(set_cookies=lambda _cookies: None)
                self.resend_count = 0
                created.append(self)

            async def share_session_with_sentinel(self):
                return None

            async def init_page_email(self, _email, **_kwargs):
                return {
                    "device_id": self.device_id,
                    "cookies": {},
                    "page_path": "/email-verification",
                }

            async def validate_email_otp(self, _code):
                return {
                    "error": {
                        "code": "account_deactivated",
                        "message": "Account has been deactivated",
                    }
                }

            async def send_email_otp(self):
                self.resend_count += 1
                raise AssertionError("deactivated account must not resend OTP")

            async def create_account(self, *_args, **_kwargs):
                raise AssertionError("deactivated account must not create an account")

            async def close(self):
                return None

        module = self._core_module()
        bot = module.ChatGPTRegister(
            {
                "email": "protocol@icloud.com",
                "refresh_token": "http://127.0.0.1/code",
            },
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

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "account_unusable: account_deactivated")
        self.assertEqual(created[0].resend_count, 0)


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

    async def test_protocol_worker_timeout_terminates_child_process(self):
        worker_script = self.base_dir / "slow_worker.py"
        worker_script.write_text(
            "import time\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=lambda payload, on_event: None,
        )
        manager.worker_script = worker_script
        manager.python_executable = Path(sys.executable)

        with patch(
            "hidemyemail_generator.protocol_registration."
            "PROTOCOL_WORKER_TIMEOUT_SECONDS",
            0.05,
        ):
            with self.assertRaisesRegex(RuntimeError, "已安全终止"):
                await manager._run_worker({}, lambda _event: None)

        self.assertEqual(manager._active_processes, set())

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

    async def test_session_only_success_does_not_require_or_persist_credentials(self):
        captured = []

        async def runner(payload, on_event):
            captured.append(payload)
            on_event(
                {
                    "stage": "session",
                    "message": "Session/Cookie 已获取",
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
                        "sessionToken": "session-only-token",
                    }
                ),
                "storage_state_json": json.dumps({"cookies": [], "origins": []}),
                "session_acquisition_method": "gptfree_mail_auth",
            }

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=runner,
        )
        initial = manager.start(
            emails=["session-only@icloud.com"],
            base_url="http://127.0.0.1:8080",
            concurrency=1,
            setup_credentials=False,
        )
        final = await manager.wait()

        self.assertFalse(initial["setupCredentials"])
        self.assertFalse(captured[0]["setup_credentials"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["succeeded"], 1)
        self.assertIn("Session/Cookie", final["accounts"][0]["message"])
        record = load_account_record(self.db_file, "session-only@icloud.com")
        self.assertEqual(record["session"]["sessionToken"], "session-only-token")
        self.assertNotIn("password", record)
        self.assertNotIn("two_factor", record)

    async def test_protocol_success_queries_plan_with_saved_at_and_persists_proxy(self):
        checked = []

        class ProxyStore:
            def next_proxy(self):
                return "http://127.0.0.1:19011", {
                    "mode": "clash",
                    "country": "JP",
                    "countryLabel": "日本",
                    "endpoint": "127.0.0.1:19011",
                }

        async def runner(payload, on_event):
            del on_event
            token = "header.fresh-free.signature"
            return {
                "status": "success",
                "email": payload["email"],
                "access_token": token,
                "session_json": json.dumps(
                    {
                        "accessToken": token,
                        "sessionToken": "session-token",
                        "account": {"planType": "free"},
                    }
                ),
                "cookies_json": json.dumps([]),
                "storage_state_json": json.dumps({"cookies": [], "origins": []}),
                "session_acquisition_method": "gptfree_mail_auth",
            }

        async def verify_account_plan(email):
            record = load_account_record(self.db_file, email)
            checked.append((email, record["access_token"]))
            return {
                "status": "free",
                "source": "access_token_online",
                "detail": "AT 在线套餐查询完成",
            }

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            proxy_store=ProxyStore(),
            worker_runner=runner,
            verify_account_plan=verify_account_plan,
        )
        manager.start(
            emails=["plan-check@icloud.com"],
            base_url="http://127.0.0.1:8080",
            concurrency=1,
            setup_credentials=False,
        )

        final = await manager.wait()

        self.assertEqual(
            checked,
            [("plan-check@icloud.com", "header.fresh-free.signature")],
        )
        record = load_account_record(self.db_file, "plan-check@icloud.com")
        self.assertEqual(
            record["registration_proxy_url"], "http://127.0.0.1:19011"
        )
        self.assertEqual(final["accounts"][0]["accountType"], "free")
        self.assertTrue(
            any("AT 套餐查询完成：Free" in item["message"] for item in final["logs"])
        )

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

    async def test_confirmed_password_without_session_is_not_reported_as_2fa_pending(self):
        async def failing_runner(payload, on_event):
            on_event(
                {
                    "stage": "password_checkpoint",
                    "message": "账号密码已由 OpenAI 确认并保存",
                    "status": "success",
                    "password_checkpoint": {
                        "password": "StableCandidate!1",
                        "password_confirmed": True,
                        "result": {},
                    },
                }
            )
            raise RuntimeError(
                "account_password_register_failed: invalid_auth_step"
            )

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=failing_runner,
        )
        manager.start(
            emails=["password-only@icloud.com"],
            base_url="http://127.0.0.1:8080",
            concurrency=1,
        )
        failed = await manager.wait()

        account = failed["accounts"][0]
        self.assertEqual(account["stage"], "password")
        self.assertIn("不会重新生成", account["message"])
        self.assertNotIn("TOTP 2FA 待补跑", account["message"])

    async def test_unconfirmed_passwordless_session_is_not_reported_as_2fa_pending(self):
        async def failing_runner(payload, on_event):
            on_event(
                {
                    "stage": "password_checkpoint",
                    "message": "账号与 Session 已创建；保留候选密码",
                    "status": "active",
                    "password_checkpoint": {
                        "password": "StableCandidate!1",
                        "password_confirmed": False,
                        "result": {
                            "access_token": "passwordless-access-token",
                            "session_json": json.dumps(
                                {"accessToken": "passwordless-access-token"}
                            ),
                        },
                    },
                }
            )
            raise RuntimeError("password_reset_send_failed: rate_limit_exceeded")

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=failing_runner,
        )
        manager.start(
            emails=["passwordless@icloud.com"],
            base_url="http://127.0.0.1:8080",
            concurrency=1,
        )
        failed = await manager.wait()

        account = failed["accounts"][0]
        self.assertEqual(account["stage"], "password")
        self.assertIn("不会重新生成", account["message"])
        self.assertNotIn("TOTP 2FA 待补跑", account["message"])
        record = load_account_record(self.db_file, "passwordless@icloud.com")
        self.assertFalse(record["password_confirmed"])
        self.assertEqual(record["access_token"], "passwordless-access-token")

    async def test_2fa_failure_keeps_password_session_checkpoint_for_resume(self):
        async def failing_runner(payload, on_event):
            on_event(
                {
                    "stage": "password_checkpoint",
                    "message": "账号密码和 Session 已确认并保存",
                    "status": "success",
                    "password_checkpoint": {
                        "password": "GeneratedPassword!1",
                        "password_confirmed": True,
                        "result": {
                            "status": "partial",
                            "email": payload["email"],
                            "access_token": "saved-access-token",
                            "session_json": json.dumps(
                                {
                                    "accessToken": "saved-access-token",
                                    "sessionToken": "saved-session-token",
                                }
                            ),
                            "storage_state_json": json.dumps(
                                {
                                    "cookies": [
                                        {
                                            "name": "oai-did",
                                            "value": "saved-device-id",
                                            "domain": "chatgpt.com",
                                            "path": "/",
                                        }
                                    ],
                                    "origins": [],
                                }
                            ),
                            "cookies_json": json.dumps(
                                [
                                    {
                                        "name": "oai-did",
                                        "value": "saved-device-id",
                                        "domain": "chatgpt.com",
                                        "path": "/",
                                    },
                                    {
                                        "name": "__Secure-next-auth.session-token",
                                        "value": "saved-session-token",
                                        "domain": "chatgpt.com",
                                        "path": "/",
                                    },
                                ]
                            ),
                            "session_acquisition_method": "gptfree_mail_auth",
                            "registration_diagnostics": {
                                "impersonate": "chrome136",
                            },
                        },
                    },
                }
            )
            raise RuntimeError("TOTP 2FA setup failed")

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=failing_runner,
        )
        manager.start(
            emails=["checkpoint@icloud.com"],
            base_url="http://127.0.0.1:8080",
            concurrency=1,
        )
        failed = await manager.wait()

        self.assertEqual(failed["failed"], 1)
        self.assertEqual(failed["accounts"][0]["stage"], "two_factor")
        self.assertIn("账号注册和密码已保存", failed["accounts"][0]["message"])
        self.assertTrue(
            any(
                log["status"] == "warning" and "TOTP 2FA 待补跑" in log["message"]
                for log in failed["logs"]
            )
        )
        record = load_account_record(self.db_file, "checkpoint@icloud.com")
        self.assertEqual(record["password"], "GeneratedPassword!1")
        self.assertTrue(record["password_confirmed"])
        self.assertEqual(record["access_token"], "saved-access-token")
        self.assertEqual(record["session"]["sessionToken"], "saved-session-token")

        resumed_payloads = []

        async def resumed_runner(payload, on_event):
            resumed_payloads.append(payload)
            return {
                "status": "success",
                "email": payload["email"],
                "access_token": payload["existing_access_token"],
                "session_json": json.dumps(payload["existing_session_json"]),
                "storage_state_json": json.dumps({"cookies": [], "origins": []}),
                "session_acquisition_method": "gptfree_mail_auth",
                "password": payload["existing_password"],
                "two_factor": {
                    "enabled": True,
                    "status": "enabled",
                    "type": "totp",
                    "secret": "JBSWY3DPEHPK3PXP",
                },
            }

        resumed_manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=resumed_runner,
        )
        resumed_manager.start(
            emails=["checkpoint@icloud.com"],
            base_url="http://127.0.0.1:8080",
            concurrency=1,
        )
        resumed = await resumed_manager.wait()

        self.assertEqual(resumed["succeeded"], 1)
        self.assertEqual(len(resumed_payloads), 1)
        resume_payload = resumed_payloads[0]
        self.assertTrue(resume_payload["existing_password_confirmed"])
        self.assertEqual(resume_payload["existing_password"], "GeneratedPassword!1")
        self.assertEqual(resume_payload["existing_access_token"], "saved-access-token")
        self.assertEqual(resume_payload["existing_session_token"], "saved-session-token")
        self.assertEqual(resume_payload["existing_device_id"], "saved-device-id")
        self.assertEqual(resume_payload["existing_impersonate"], "chrome136")

    async def test_unconfirmed_password_candidate_is_saved_and_reused(self):
        async def first_runner(payload, on_event):
            on_event(
                {
                    "stage": "password_checkpoint",
                    "message": "注册密码候选值已保存；后续重试将复用同一密码",
                    "status": "active",
                    "password_checkpoint": {
                        "password": "StableCandidate!1",
                        "password_confirmed": False,
                        "result": {},
                    },
                }
            )
            raise RuntimeError(
                "account_password_register_failed: invalid_auth_step"
            )

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=first_runner,
        )
        manager.start(
            emails=["candidate@icloud.com"],
            base_url="http://127.0.0.1:8080",
            concurrency=1,
        )
        first = await manager.wait()

        self.assertEqual(first["failed"], 1)
        self.assertEqual(first["accounts"][0]["stage"], "password")
        self.assertIn("不会重新生成", first["accounts"][0]["message"])
        record = load_account_record(self.db_file, "candidate@icloud.com")
        self.assertEqual(record["password"], "StableCandidate!1")
        self.assertFalse(record["password_confirmed"])

        captured = []

        async def retry_runner(payload, on_event):
            captured.append(payload)
            raise RuntimeError("stopped-after-capture")

        retry_manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=retry_runner,
        )
        retry_manager.start(
            emails=["candidate@icloud.com"],
            base_url="http://127.0.0.1:8080",
            concurrency=1,
        )
        await retry_manager.wait()

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["existing_password"], "StableCandidate!1")
        self.assertFalse(captured[0]["existing_password_confirmed"])

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
        self.assertEqual(captured[0]["proxy_country"], "JP")
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
        failures = []

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
            record_failure=failures.append,
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
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["mode"], "protocol")
        self.assertEqual(failures[0]["email"], "failed@icloud.com")
        self.assertEqual(failures[0]["status"], "failed")
        self.assertIn("protocol rejected", failures[0]["failureContext"]["message"])
        self.assertTrue(failures[0]["logs"])

    async def test_terminal_account_failure_is_not_offered_for_retry(self):
        failures = []

        async def runner(_payload, on_event):
            on_event(
                {
                    "stage": "password_checkpoint",
                    "message": "注册密码候选值已保存",
                    "password_checkpoint": {
                        "password": "GeneratedPassword!1",
                        "password_confirmed": False,
                        "result": {},
                    },
                }
            )
            raise RuntimeError("account_unusable: account_deactivated")

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=runner,
            record_failure=failures.append,
        )
        manager.start(
            emails=["deactivated@icloud.com"],
            base_url="http://127.0.0.1:8080",
        )

        final = await manager.wait()
        account = final["accounts"][0]

        self.assertFalse(account["retryable"])
        self.assertEqual(account["failureCode"], "account_unusable")
        self.assertIn("不可自动重试", account["message"])
        self.assertNotIn("下次将复用", account["message"])
        self.assertFalse(failures[0]["failureContext"]["retryable"])

    async def test_proxy_setup_failure_reaches_monitor_and_terminal_state(self):
        failures = []

        class BadProxyStore:
            def next_proxy(self):
                raise OSError("proxy database unavailable")

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            proxy_store=BadProxyStore(),
            worker_runner=lambda _payload, _on_event: asyncio.sleep(0),
            record_failure=failures.append,
        )
        manager.start(
            emails=["setup-failed@icloud.com"],
            base_url="http://127.0.0.1:8080",
        )

        final = await manager.wait()

        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["failed"], 1)
        self.assertEqual(final["completed"], 1)
        self.assertEqual(final["accounts"][0]["status"], "failed")
        self.assertEqual(final["accounts"][0]["stage"], "proxy")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["failureContext"]["failedStage"], "proxy")

    async def test_failure_monitor_retries_transient_storage_error(self):
        attempts = 0
        failures = []

        async def runner(_payload, _on_event):
            raise RuntimeError("protocol rejected")

        def flaky_recorder(failure):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("database is busy")
            failures.append(failure)

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=runner,
            record_failure=flaky_recorder,
        )
        manager.start(
            emails=["retry-monitor@icloud.com"],
            base_url="http://127.0.0.1:8080",
        )

        final = await manager.wait()

        self.assertEqual(attempts, 2)
        self.assertEqual(len(failures), 1)
        self.assertNotIn("monitorError", final["accounts"][0])


class ConcurrentProtocolRegistrationManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_protocol_process_can_be_stopped_independently(self):
        processes = []

        class FakeProtocolProcess:
            def __init__(self, number):
                self.number = number
                self.on_account_saved = None
                self.state = {
                    "id": "",
                    "running": False,
                    "status": "idle",
                    "phase": "idle",
                    "message": "等待协议注册任务",
                    "total": 0,
                    "completed": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "accounts": [],
                    "logs": [],
                }

            def start(self, *, emails, base_url, concurrency=1, **kwargs):
                del base_url, concurrency, kwargs
                email = emails[0]
                self.state.update(
                    id=f"protocol-{self.number}",
                    running=True,
                    status="running",
                    phase="prepare",
                    message=f"正在注册 {email}",
                    total=1,
                    accounts=[{"email": email, "status": "queued"}],
                    logs=[{"at": f"2026-08-14T00:00:0{self.number}+00:00", "message": "开始"}],
                )
                return self.snapshot()

            def snapshot(self):
                return {**self.state, "accounts": list(self.state["accounts"]), "logs": list(self.state["logs"])}

            def refresh_runtime(self):
                return {"available": True, "error": ""}

            def token_record(self, token):
                return {"token": token} if token == f"token-{self.number}" else None

            async def stop(self):
                self.state.update(running=False, status="cancelled", phase="cancelled")
                return self.snapshot()

        def process_factory():
            process = FakeProtocolProcess(len(processes) + 1)
            processes.append(process)
            return process

        coordinator = ConcurrentProtocolRegistrationManager(
            process_factory=process_factory,
            max_processes=3,
        )
        first = coordinator.start(
            emails=["first@icloud.com"],
            base_url="http://127.0.0.1:8080",
        )
        second = coordinator.start(
            emails=["second@icloud.com"],
            base_url="http://127.0.0.1:8080",
        )

        with self.assertRaisesRegex(RuntimeError, "邮箱正在协议注册中"):
            coordinator.start(
                emails=["first@icloud.com"],
                base_url="http://127.0.0.1:8080",
            )

        self.assertEqual(second["runningCount"], 2)
        self.assertEqual(second["processCount"], 2)
        self.assertEqual(
            [task["processId"] for task in second["tasks"]],
            ["protocol-1", "protocol-2"],
        )
        self.assertTrue(coordinator.valid_code_token("token-2"))

        state = await coordinator.stop(process_id=first["id"])

        self.assertEqual(processes[0].snapshot()["status"], "cancelled")
        self.assertTrue(processes[1].snapshot()["running"])
        self.assertEqual(state["runningCount"], 1)
        with self.assertRaisesRegex(ValueError, "不存在或已归档"):
            await coordinator.stop(process_id="missing")
        await coordinator.close()


if __name__ == "__main__":
    unittest.main()
