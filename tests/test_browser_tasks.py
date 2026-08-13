import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hidemyemail_generator.browser_tasks import (
    BrowserTaskManager,
    _save_account_record,
    account_saved_cookies,
    access_token_is_expired,
    browser_log_context,
    jwt_account_type,
    load_account_record,
    set_manual_account_type,
)
from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.openai_browser_bridge import (
    ADD_PASSWORD_SELECTORS,
    CHATGPT_HOME_LOGIN_SELECTORS,
    CHATGPT_HOME_SIGNUP_SELECTORS,
    OPENAI_EMAIL_LOGIN_INPUT_SELECTORS,
    OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS,
    PROFILE_MENU_STRICT_SELECTORS,
    FreshFingerprintRequiredError,
    ManualOtpReader,
    _activate_visible_registration_page,
    _click_add_password,
    _click_chatgpt_home_login,
    _click_password_add_by_geometry,
    _click_profile_name_by_dom,
    _camoufox_window_layout,
    _configure_camoufox_runtime_cache,
    _dismiss_completed_onboarding,
    _click_first_visible,
    _fontconfig_generator_with_home,
    _mfa_token_was_invalidated,
    _open_settings_from_profile,
    _password_confirmed_for_two_factor,
    add_password_via_account_api,
    configure_chatgpt_home_login_entry,
    configure_email_verification_priority,
    configure_password_first_login,
    configure_password_readiness_diagnostics,
    configure_email_password_only_registration,
    configure_existing_account_two_factor,
    configure_manual_browser_verification,
    configure_resilient_about_you_input,
    configure_direct_registration_browser,
    configure_post_registration_password_setup,
    configure_registration_otp_reader,
    configure_registration_profile_capture,
    configure_registration_state_recognition,
    configure_resilient_registration_navigation,
    configure_security_challenge_monitoring,
    configure_windowed_camoufox,
    ensure_password_in_security_settings,
    detect_direct_registration_location,
    extract_session_without_navigation,
    load_saved_storage_state,
    require_registration_proxy_country,
    resilient_force_fill_locator,
    safe_log_message,
    recognize_registration_page,
)
from hidemyemail_generator.openai_account_security import (
    _enable_two_factor_before_browser_closes,
    add_password_via_account_api as add_password_via_account_api_impl,
)
from hidemyemail_generator.openai_browser_cli import prepare_registration_proxy
from hidemyemail_generator.openai_mfa import MfaSetupError
from hidemyemail_generator.openai_registration_otp import EmailVerificationPageAdvanced
from hidemyemail_generator.openai_registration_flow import (
    configure_lightweight_registration_resources,
)
from hidemyemail_generator.registration_activity import (
    ensure_email_verification_code_entered,
)
from hidemyemail_generator.registration_proxy import RegistrationProxyStore


def token_with_exp(expires_at: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


class BrowserTaskHelperTests(unittest.TestCase):
    def test_lightweight_registration_blocks_visual_resources_only(self):
        handlers = []
        events = []

        class Context:
            @staticmethod
            def route(pattern, handler):
                handlers.append((pattern, handler))

        class Worker:
            def __init__(self):
                self.logs = []

            @staticmethod
            def _new_browser_context(*_args, **_kwargs):
                return "browser", Context()

            def log(self, message):
                self.logs.append(message)

        class Request:
            def __init__(self, resource_type):
                self.resource_type = resource_type

        class Route:
            def abort(self, **kwargs):
                events.append(("abort", kwargs.get("error_code")))

            def fallback(self):
                events.append(("fallback", None))

        worker = Worker()
        self.assertTrue(configure_lightweight_registration_resources(worker))
        browser, context = worker._new_browser_context(None, None)

        self.assertEqual(browser, "browser")
        self.assertIsInstance(context, Context)
        self.assertEqual(handlers[0][0], "**/*")
        handler = handlers[0][1]
        for resource_type in ("image", "media", "font"):
            handler(Route(), Request(resource_type))
        for resource_type in ("document", "script", "stylesheet", "fetch", "xhr"):
            handler(Route(), Request(resource_type))

        self.assertEqual(
            events,
            [("abort", "blockedbyclient")] * 3 + [("fallback", None)] * 5,
        )
        self.assertTrue(any("图片、媒体和网页字体" in item for item in worker.logs))

    def test_email_otp_submit_is_blocked_while_visible_field_is_blank(self):
        class Field:
            @staticmethod
            def input_value(**_kwargs):
                return ""

            @staticmethod
            def click(**_kwargs):
                return None

            @staticmethod
            def fill(_value):
                return None

            @staticmethod
            def type(_value, **_kwargs):
                return None

        class Page:
            @staticmethod
            def wait_for_timeout(_milliseconds):
                return None

        class Worker:
            def __init__(self):
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            @staticmethod
            def _visible_inputs(_page, _selectors):
                return [Field()]

        with self.assertRaisesRegex(RuntimeError, "已阻止提交"):
            ensure_email_verification_code_entered(Worker(), Page(), "123456")

    def test_email_otp_is_typed_and_read_back_before_submit_gate_opens(self):
        class Field:
            def __init__(self):
                self.value = ""

            def input_value(self, **_kwargs):
                return self.value

            @staticmethod
            def click(**_kwargs):
                return None

            def fill(self, value):
                self.value = value

            def type(self, value, **_kwargs):
                self.value += value

        class Page:
            @staticmethod
            def wait_for_timeout(_milliseconds):
                return None

        class Worker:
            def __init__(self):
                self.field = Field()
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _visible_inputs(self, _page, _selectors):
                return [self.field]

        worker = Worker()
        ensure_email_verification_code_entered(worker, Page(), "123456")

        self.assertEqual(worker.field.value, "123456")
        self.assertIn("输入检查通过", worker.logs[-1])
        self.assertIn("现在点击继续", worker.logs[-1])

    def test_email_otp_deduplicates_locators_for_the_same_dom_input(self):
        state = {"value": ""}

        class Field:
            @staticmethod
            def evaluate(_script):
                return "same-dom-input"

            @staticmethod
            def input_value(**_kwargs):
                return state["value"]

            @staticmethod
            def click(**_kwargs):
                return None

            @staticmethod
            def fill(value):
                state["value"] = value

            @staticmethod
            def type(value, **_kwargs):
                state["value"] += value

        class Page:
            @staticmethod
            def wait_for_timeout(_milliseconds):
                return None

        class Worker:
            def __init__(self):
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            @staticmethod
            def _visible_inputs(_page, _selectors):
                return [Field() for _index in range(6)]

        worker = Worker()
        ensure_email_verification_code_entered(worker, Page(), "123456")

        self.assertEqual(state["value"], "123456")
        self.assertIn("输入检查通过", worker.logs[-1])
        self.assertIn("现在点击继续", worker.logs[-1])

    def test_registration_page_recognition_reports_done_and_next_action(self):
        class Candidate:
            def is_visible(self, **_kwargs):
                return True

        class Collection:
            def __init__(self, items=(), text=""):
                self.items = list(items)
                self.text = text

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

            def inner_text(self, **_kwargs):
                return self.text

        class Page:
            url = "https://auth.openai.com/email-verification?secret=hidden"

            def locator(self, selector):
                if selector == "body":
                    return Collection(
                        text="受信箱を確認してください パスワードで続行"
                    )
                if selector == 'input[autocomplete="one-time-code"]':
                    return Collection([Candidate()])
                return Collection()

            def evaluate(self, _script):
                return "complete"

        worker = SimpleNamespace(
            _password_step_submitted=False,
            _hme_manual_otp_entry=False,
        )
        state = recognize_registration_page(worker, Page())

        self.assertEqual(state["code"], "email_verification")
        self.assertEqual(state["currentPage"], "邮箱验证码页")
        self.assertIn("邮箱已提交", state["completedSteps"])
        self.assertIn("使用密码继续", state["nextAction"])
        self.assertEqual(state["source"], "dom")
        self.assertNotIn("secret", state["route"])

    def test_registration_state_monitor_warns_and_saves_screenshot_after_five_seconds(self):
        class Collection:
            def count(self):
                return 0

            def nth(self, _index):
                raise IndexError

            def inner_text(self, **_kwargs):
                return "Check your inbox verification code"

        class Page:
            url = "https://auth.openai.com/email-verification"

            def __init__(self):
                self.screenshots = []

            def locator(self, _selector):
                return Collection()

            def evaluate(self, _script):
                return "complete"

            def screenshot(self, *, path, **_kwargs):
                self.screenshots.append(path)
                Path(path).write_bytes(b"state-png")

        class Worker:
            _password_step_submitted = True
            _hme_manual_otp_entry = False

            def __init__(self):
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _has_otp_input(self, _page):
                return True

        ticks = iter((0.0, 5.1))
        emitted = []
        with tempfile.TemporaryDirectory() as temp_dir:
            worker = Worker()
            page = Page()
            self.assertTrue(
                configure_registration_state_recognition(
                    worker,
                    emit_state=emitted.append,
                    diagnostics_dir=temp_dir,
                    monotonic=lambda: next(ticks),
                )
            )
            worker._recognize_registration_state(page)
            worker._recognize_registration_state(page)

            self.assertEqual(len(emitted), 2)
            self.assertFalse(emitted[0]["stalled"])
            self.assertTrue(emitted[1]["stalled"])
            self.assertEqual(emitted[1]["stalledSeconds"], 5)
            self.assertTrue(Path(emitted[1]["diagnosticScreenshot"]).is_file())
            self.assertEqual(len(page.screenshots), 1)
            self.assertTrue(any("下一步=" in line for line in worker.logs))

    def test_post_registration_password_uses_account_api(self):
        calls = []

        class Response:
            status = 200

            @staticmethod
            def json():
                return {"success": True}

        class Request:
            @staticmethod
            def post(url, **kwargs):
                calls.append((url, kwargs))
                return Response()

        worker = SimpleNamespace(
            _password_step_submitted=False,
            logs=[],
        )
        worker.log = worker.logs.append

        confirmed = add_password_via_account_api(
            worker,
            SimpleNamespace(request=Request()),
            "at-registration",
            "Strong!Password123",
        )

        self.assertTrue(confirmed)
        self.assertTrue(worker._password_step_submitted)
        self.assertEqual(
            calls[0][0],
            "https://chatgpt.com/api/accounts/password/add",
        )
        self.assertEqual(
            json.loads(calls[0][1]["data"]),
            {"password": "Strong!Password123"},
        )
        self.assertEqual(
            calls[0][1]["headers"]["Authorization"],
            "Bearer at-registration",
        )
        self.assertIn("POST /api/accounts/password/add", worker.logs[-1])

    def test_post_registration_password_retries_pre_tls_disconnect(self):
        calls = []
        delays = []

        class Response:
            status = 200

            @staticmethod
            def json():
                return {"success": True}

        class Request:
            @staticmethod
            def post(url, **kwargs):
                calls.append((url, kwargs))
                if len(calls) < 3:
                    raise RuntimeError(
                        "Client network socket disconnected before secure TLS "
                        "connection was established"
                    )
                return Response()

        worker = SimpleNamespace(
            _password_step_submitted=False,
            logs=[],
        )
        worker.log = worker.logs.append

        confirmed = add_password_via_account_api_impl(
            worker,
            SimpleNamespace(request=Request()),
            "at-registration",
            "Strong!Password123",
            retry_delays=(2.0, 5.0),
            sleep=delays.append,
        )

        self.assertTrue(confirmed)
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [2.0, 5.0])
        self.assertEqual(
            [message for message in worker.logs if "重试" in message],
            [
                "[密码] POST /api/accounts/password/add 在 TLS 建连前断开；"
                "2 秒后重试（2/3）",
                "[密码] POST /api/accounts/password/add 在 TLS 建连前断开；"
                "5 秒后重试（3/3）",
            ],
        )

    def test_post_registration_password_does_not_retry_ambiguous_failure(self):
        calls = []
        delays = []
        secret = "header.payload.signature-secret"

        class Request:
            @staticmethod
            def post(_url, **_kwargs):
                calls.append("post")
                raise RuntimeError(
                    "response body read failed\nAuthorization: Bearer " + secret
                )

        worker = SimpleNamespace(
            _password_step_submitted=False,
            logs=[],
        )
        worker.log = worker.logs.append

        with self.assertRaisesRegex(RuntimeError, "网络连接失败") as raised:
            add_password_via_account_api_impl(
                worker,
                SimpleNamespace(request=Request()),
                "at-registration",
                "Strong!Password123",
                retry_delays=(2.0, 5.0),
                sleep=delays.append,
            )

        self.assertEqual(calls, ["post"])
        self.assertEqual(delays, [])
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, " ".join(worker.logs))

    def test_password_readiness_timeout_logs_dom_state_and_screenshot(self):
        class Candidate:
            def is_visible(self, **_kwargs):
                return False

        class Collection:
            def __init__(self, items=()):
                self.items = list(items)

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

            def inner_text(self, **_kwargs):
                return ""

        class Page:
            url = "https://auth.openai.com/log-in/password?secret=hidden"

            def __init__(self):
                self.screenshots = []

            def wait_for_load_state(self, _state, **_kwargs):
                return None

            def evaluate(self, _script):
                return {
                    "readyState": "complete",
                    "styleSheetCount": 2,
                    "loadedStyleLinkCount": 1,
                }

            def locator(self, selector):
                if selector == "body":
                    return Collection()
                if selector == 'input[type="password"]':
                    return Collection([Candidate()])
                return Collection()

            def screenshot(self, *, path, **_kwargs):
                self.screenshots.append(path)
                Path(path).write_bytes(b"test-png")

        class Worker:
            def __init__(self):
                self.logs = []

            def _wait_for_auth_page_ready(self, *_args, **_kwargs):
                raise AssertionError("password diagnostics wrapper was not used")

            def log(self, message):
                self.logs.append(message)

        monotonic = [0.0]

        def next_monotonic():
            monotonic[0] += 0.6
            return monotonic[0]

        with tempfile.TemporaryDirectory() as temp_dir:
            worker = Worker()
            page = Page()
            self.assertTrue(
                configure_password_readiness_diagnostics(
                    worker,
                    diagnostics_dir=Path(temp_dir),
                )
            )
            with (
                patch(
                    "hidemyemail_generator.openai_registration_flow.time.monotonic",
                    side_effect=next_monotonic,
                ),
                patch(
                    "hidemyemail_generator.openai_registration_flow.time.sleep"
                ),
                self.assertRaisesRegex(RuntimeError, "密码匹配/可见"),
            ):
                worker._wait_for_auth_page_ready(
                    page,
                    "OpenAI 密码",
                    ready_selectors=('input[type="password"]',),
                    require_editable=True,
                    timeout_seconds=1,
                )

            self.assertEqual(len(page.screenshots), 1)
            self.assertTrue(Path(page.screenshots[0]).is_file())
            self.assertTrue(any(line.startswith("[AUTH_PASSWORD_WAIT]") for line in worker.logs))
            self.assertTrue(any(line.startswith("[AUTH_PASSWORD_TIMEOUT]") for line in worker.logs))
            self.assertTrue(any(line.startswith("[AUTH_PASSWORD_SCREENSHOT]") for line in worker.logs))
            self.assertNotIn("secret=hidden", "\n".join(worker.logs))

    def test_japanese_cloudflare_challenge_waits_for_manual_click(self):
        original_calls = []
        focus_events = []

        class Body:
            def __init__(self, page):
                self.page = page

            def inner_text(self, **_kwargs):
                if self.page.challenge:
                    return (
                        "私はロボットではありません "
                        "Cloudflare セキュリティチャレンジを含むウィジェット"
                    )
                return "OpenAI login"

        class Page:
            url = "https://auth.openai.com/log-in"

            def __init__(self):
                self.challenge = True

            def locator(self, selector):
                if selector == "body":
                    return Body(self)
                raise AssertionError("challenge monitor must only inspect page text")

            def bring_to_front(self):
                focus_events.append("front")

            def evaluate(self, _script):
                focus_events.append("focus")

        class Worker:
            headless = False

            def __init__(self):
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _continue_chatgpt_registration_complete(self, _page):
                original_calls.append("continue")
                return "continued"

            def _fill_email_if_visible(self, _page):
                original_calls.append("email")
                return True

            def _has_visible_password(self, _page):
                original_calls.append("password")
                return True

            def _has_otp_input(self, _page):
                original_calls.append("otp")
                return True

            def _has_about_you_form(self, _page):
                original_calls.append("profile")
                return True

        worker = Worker()
        page = Page()
        self.assertTrue(configure_security_challenge_monitoring(worker))

        with (
            patch.dict(os.environ, {"HME_BROWSER_FOREGROUND_REQUIRED": ""}),
            patch(
                "hidemyemail_generator.openai_browser_bridge."
                "_focus_camoufox_window_once",
                return_value=True,
            ) as focus_window,
        ):
            self.assertTrue(worker._continue_chatgpt_registration_complete(page))
            self.assertFalse(worker._fill_email_if_visible(page))
            self.assertFalse(worker._has_visible_password(page))
            self.assertFalse(worker._has_otp_input(page))
            self.assertFalse(worker._has_about_you_form(page))

        self.assertEqual(original_calls, [])
        self.assertEqual(focus_events, [])
        focus_window.assert_not_called()
        self.assertTrue(any("请在当前浏览器手动点击" in log for log in worker.logs))
        self.assertTrue(any("程序不会代点" in log for log in worker.logs))

        page.challenge = False
        self.assertEqual(
            worker._continue_chatgpt_registration_complete(page),
            "continued",
        )
        self.assertEqual(original_calls, ["continue"])
        self.assertIn("安全验证已完成", worker.logs[-1])

    def test_registration_homepage_clicks_signup_then_uses_registration_flow(self):
        actions = []
        clipboard = {"value": ""}

        class Candidate:
            def __init__(self, page, kind):
                self.page = page
                self.kind = kind

            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                actions.append(("click", self.kind))
                if self.kind == "login":
                    self.page.home_modal = True
                elif self.kind == "signup":
                    self.page.home_modal = True
                elif self.kind == "submit":
                    self.page.url = "https://auth.openai.com/email-verification"
                    self.page.home_modal = False

        class EmailInput:
            def __init__(self):
                self.value = ""

            def is_visible(self, **_kwargs):
                return True

            def click(self, **_kwargs):
                actions.append(("click", "email-input"))

            def press(self, key, **_kwargs):
                actions.append(("press", key))
                if key == "Control+A":
                    self.value = ""
                elif key == "Control+V":
                    self.value = clipboard["value"]

            def input_value(self, **_kwargs):
                return self.value

        class Collection:
            def __init__(self, items):
                self.items = items

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        class Page:
            def __init__(self):
                self.url = "about:blank"
                self.home_modal = False
                self.email_input = EmailInput()

            def goto(self, url, **_kwargs):
                actions.append(("goto", url))
                self.url = url
                return "response"

            def wait_for_load_state(self, *_args, **_kwargs):
                actions.append("loaded")

            def wait_for_timeout(self, _milliseconds):
                return None

            def locator(self, selector):
                if (
                    selector in CHATGPT_HOME_LOGIN_SELECTORS
                    and self.url == "https://chatgpt.com/"
                    and not self.home_modal
                ):
                    return Collection([Candidate(self, "login")])
                if (
                    selector in OPENAI_EMAIL_LOGIN_INPUT_SELECTORS
                    and (
                        self.home_modal
                        or self.url.startswith("https://auth.openai.com/")
                    )
                ):
                    return Collection([self.email_input])
                if (
                    selector in OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS
                    and (
                        self.home_modal
                        or self.url == "https://auth.openai.com/log-in"
                    )
                ):
                    return Collection([Candidate(self, "submit")])
                if selector in CHATGPT_HOME_SIGNUP_SELECTORS:
                    return Collection([Candidate(self, "signup")])
                return Collection([])

        class Context:
            def __init__(self):
                self.routes = []

            def route(self, pattern, handler):
                self.routes.append((pattern, handler))

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="password.only@gmail.com")
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _fill_email_if_visible(self, _page):
                return False

            def _visible_inputs(self, page, _selectors):
                if page.home_modal or page.url == "https://auth.openai.com/log-in":
                    return [page.email_input]
                return []

            def _create_openai_signin_url(self, _context):
                actions.append("signup-url")
                return "https://auth.openai.com/create-account"

            def _create_login_url(self, _context):
                actions.append("login-url")
                return "https://auth.openai.com/log-in"

            def _goto_auth_page(self, page, url):
                actions.append(("direct-auth-goto", url))
                return page.goto(url)

            def _register(self, page, context, *, existing_login_only=False):
                actions.append(("existing-login-only", existing_login_only))
                page.goto("https://chatgpt.com/")
                if page.home_modal:
                    actions.append("home-modal-was-not-filled")
                    return False
                signin_url = self._create_openai_signin_url(context)
                self._goto_auth_page(page, signin_url)
                return page.url == "https://auth.openai.com/email-verification"

        worker = Worker()
        page = Page()
        context = Context()

        self.assertTrue(
            configure_email_password_only_registration(worker, enabled=True)
        )
        self.assertTrue(configure_chatgpt_home_login_entry(worker))
        self.assertTrue(configure_resilient_registration_navigation(worker))
        def copy_clipboard(value):
            clipboard["value"] = value
            actions.append(("clipboard", value))

        with patch(
            "hidemyemail_generator.openai_browser_bridge."
            "_copy_registration_clipboard_text",
            side_effect=copy_clipboard,
        ):
            self.assertTrue(
                worker._register(page, context, existing_login_only=False)
            )

        self.assertIn(("click", "signup"), actions)
        self.assertNotIn(("click", "login"), actions)
        self.assertIn(("existing-login-only", False), actions)
        self.assertIn(("click", "email-input"), actions)
        self.assertIn(("clipboard", "password.only@gmail.com"), actions)
        self.assertIn(("press", "Control+V"), actions)
        self.assertIn(("click", "submit"), actions)
        self.assertNotIn("home-modal-was-not-filled", actions)
        self.assertNotIn("signup-url", actions)
        self.assertNotIn("login-url", actions)
        self.assertFalse(
            any(
                action[0] == "direct-auth-goto"
                for action in actions
                if isinstance(action, tuple)
            )
        )
        self.assertTrue(any("免费注册" in log for log in worker.logs))
        self.assertTrue(any("不会匹配登录按钮" in log for log in worker.logs))
        self.assertEqual(len(context.routes), 2)
        self.assertFalse(
            any(
                "sign up" in selector.casefold() or "免费注册" in selector
                for selector in CHATGPT_HOME_LOGIN_SELECTORS
            )
        )

    def test_home_email_drawer_still_on_home_uses_real_auth_url(self):
        actions = []

        class Page:
            def __init__(self):
                self.url = "about:blank"

            def goto(self, url, **_kwargs):
                actions.append(("goto", url))
                self.url = url

        class Worker:
            def __init__(self):
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _fill_email_if_visible(self, _page):
                return False

            def _create_openai_signin_url(self, _context):
                actions.append("signup-url")
                return "https://auth.openai.com/create-account"

            def _create_login_url(self, _context):
                return "https://auth.openai.com/log-in"

            def _goto_auth_page(self, page, url):
                actions.append(("auth", url))
                page.goto(url)

            def _register(self, page, context, **_kwargs):
                page.goto("https://chatgpt.com/")
                # The home drawer was clicked and submitted, but ChatGPT kept
                # the URL on its homepage while OpenAI auth state loaded.
                signin_url = self._create_openai_signin_url(context)
                self._goto_auth_page(page, signin_url)
                return page.url

        worker = Worker()
        page = Page()
        self.assertTrue(configure_chatgpt_home_login_entry(worker))

        # Exercise the post-click branch without depending on the home-button
        # selectors, whose behavior is covered by the integration-style test.
        original_register = worker._register
        page.url = "https://chatgpt.com/"

        class ClickedWorker(Worker):
            pass

        # Reproduce a drawer click using the normal registration wrapper: the
        # first homepage goto triggers signup, while the fake page remains on
        # the homepage after the email drawer submission.
        with patch(
            "hidemyemail_generator.openai_registration_navigation."
            "_click_chatgpt_home_signup",
            side_effect=lambda _page, _worker, **_kwargs: None,
        ):
            # The wrapper records the click before it calls the patched helper.
            result = original_register(page, object(), existing_login_only=False)

        self.assertEqual(result, "https://auth.openai.com/create-account")
        self.assertIn("signup-url", actions)
        self.assertIn(("auth", "https://auth.openai.com/create-account"), actions)
        self.assertTrue(any("避免把首页误判" in log for log in worker.logs))

    def test_home_login_waits_for_full_load_then_retries_once(self):
        events = []

        class Candidate:
            def __init__(self, page, transition_on_click):
                self.page = page
                self.transition_on_click = transition_on_click
                self.clicks = 0

            def is_visible(self, **_kwargs):
                return True

            def is_enabled(self, **_kwargs):
                events.append("enabled")
                return self.page.loaded

            def scroll_into_view_if_needed(self, **_kwargs):
                events.append("scroll")

            def click(self, **_kwargs):
                self.clicks += 1
                events.append(("click", self.clicks, self.page.loaded))
                if self.clicks == self.transition_on_click:
                    self.page.url = "https://auth.openai.com/log-in"
                    if self.page.raise_after_transition:
                        raise RuntimeError("navigation wait timed out")

        class Collection:
            def __init__(self, items):
                self.items = items

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        class Page:
            def __init__(self, transition_on_click=2, raise_after_transition=False):
                self.url = "https://chatgpt.com/"
                self.loaded = False
                self.raise_after_transition = raise_after_transition
                self.login = Candidate(self, transition_on_click)

            def wait_for_load_state(self, state, **_kwargs):
                events.append(("load", state))
                if state == "load":
                    self.loaded = True

            def wait_for_timeout(self, _milliseconds):
                return None

            def locator(self, selector):
                if selector in CHATGPT_HOME_LOGIN_SELECTORS:
                    return Collection([self.login])
                return Collection([])

        page = Page()
        logs = []
        worker = SimpleNamespace(log=logs.append)

        self.assertTrue(
            _click_chatgpt_home_login(
                page,
                worker,
                timeout_seconds=0.1,
                transition_timeout_seconds=0.01,
            )
        )

        self.assertEqual(events[:2], [("load", "domcontentloaded"), ("load", "load")])
        self.assertEqual(
            [event for event in events if isinstance(event, tuple) and event[0] == "click"],
            [("click", 1, True), ("click", 2, True)],
        )
        self.assertTrue(any("第 1 次点击登录后" in log for log in logs))
        self.assertTrue(any("第 2 次点击登录后页面已响应" in log for log in logs))

        stuck_page = Page(transition_on_click=0)
        with self.assertRaisesRegex(RuntimeError, "最多点击 5 次登录"):
            _click_chatgpt_home_login(
                stuck_page,
                SimpleNamespace(log=lambda _message: None),
                timeout_seconds=0.1,
                transition_timeout_seconds=0.01,
            )
        self.assertEqual(stuck_page.login.clicks, 5)

        timeout_page = Page(
            transition_on_click=1,
            raise_after_transition=True,
        )
        self.assertTrue(
            _click_chatgpt_home_login(
                timeout_page,
                SimpleNamespace(log=lambda _message: None),
                timeout_seconds=0.1,
                transition_timeout_seconds=0.01,
            )
        )
        self.assertEqual(timeout_page.login.clicks, 1)

    def test_saved_confirmed_password_allows_existing_account_two_factor(self):
        worker = SimpleNamespace(_password_step_submitted=False)

        self.assertTrue(_password_confirmed_for_two_factor(worker, True))
        self.assertFalse(_password_confirmed_for_two_factor(worker, False))
        worker._password_step_submitted = True
        self.assertTrue(_password_confirmed_for_two_factor(worker, False))

    def test_registration_code_reader_uses_one_request_id_per_wait(self):
        class Response:
            status_code = 200
            ok = True

            @staticmethod
            def json():
                return {"ok": True, "code": "123456"}

        class Session:
            def __init__(self):
                self.payloads = []

            def post(self, _url, *, headers, json, timeout):
                del headers, timeout
                self.payloads.append(json)
                return Response()

        reader = ManualOtpReader.__new__(ManualOtpReader)
        reader.email = "bought@gmail.com"
        reader.log = lambda _message: None
        reader.service_url = "http://127.0.0.1:8765"
        session = Session()
        reader.session = session
        reader.token = "worker-token"

        self.assertEqual(reader.wait_for_code(123.5), "123456")
        self.assertEqual(reader.wait_for_code(456.5), "123456")

        self.assertEqual(
            [payload["email"] for payload in session.payloads],
            ["bought@gmail.com", "bought@gmail.com"],
        )
        self.assertEqual(
            [payload["minTimestamp"] for payload in session.payloads],
            [123.5, 456.5],
        )
        self.assertNotEqual(
            session.payloads[0]["requestId"], session.payloads[1]["requestId"]
        )

    def test_registration_code_reader_stops_immediately_after_provider_timeout(self):
        class Response:
            status_code = 409
            ok = False

            @staticmethod
            def json():
                return {
                    "ok": False,
                    "error": (
                        "SMSBower Gmail 验证码等待超过 30 秒，"
                        "已取消邮箱激活并判定注册失败"
                    ),
                }

        class Session:
            def post(self, _url, **_kwargs):
                return Response()

        reader = ManualOtpReader.__new__(ManualOtpReader)
        reader.email = "timeout@gmail.com"
        reader.log = lambda _message: None
        reader.service_url = "http://127.0.0.1:8765"
        reader.session = Session()
        reader.token = "worker-token"

        with self.assertRaisesRegex(RuntimeError, "已取消邮箱激活"):
            reader.wait_for_code(123.5)

    def test_registration_code_reader_stops_when_browser_passes_otp_page(self):
        calls = []

        class Session:
            def post(self, *_args, **_kwargs):
                calls.append("post")
                raise AssertionError("mail API must not be polled after page advance")

        reader = ManualOtpReader.__new__(ManualOtpReader)
        reader.email = "advanced@icloud.com"
        reader.icloud_inbox = True
        reader.log = lambda _message: None
        reader.service_url = "http://127.0.0.1:8765"
        reader.session = Session()
        reader.token = "worker-token"
        reader.page_advanced = lambda: True

        with self.assertRaises(EmailVerificationPageAdvanced):
            reader.wait_for_code(123.5)
        self.assertEqual(calls, [])

    def test_gmail_registration_submits_email_without_google_oauth(self):
        events = []
        clipboard = {"value": ""}

        class Route:
            def abort(self):
                events.append("abort-google")

        class Context:
            def __init__(self):
                self.routes = []

            def route(self, pattern, handler):
                self.routes.append((pattern, handler))

        class EmailInput:
            def __init__(self):
                self.value = ""

            def click(self, **_kwargs):
                events.append(("click", "email-input"))

            def press(self, key, **_kwargs):
                events.append(("press", key))
                if key == "Control+A":
                    self.value = ""
                elif key == "Control+V":
                    self.value = clipboard["value"]

            def input_value(self, **_kwargs):
                return self.value

        class SubmitButton:
            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                events.append(("click", "registration-submit"))

        class Collection:
            def __init__(self, items):
                self.items = items

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        class Page:
            url = "https://auth.openai.com/log-in-or-create-account"

            def bring_to_front(self):
                return None

            def evaluate(self, _script):
                return None

            def wait_for_timeout(self, _milliseconds):
                return None

            def locator(self, selector):
                if selector in OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS:
                    return Collection([SubmitButton()])
                return Collection([])

        class Worker:
            headless = False

            def __init__(self):
                self.account = SimpleNamespace(email="password.only@gmail.com")
                self.logs = []
                self.original_register_calls = 0
                self.original_fill_calls = 0

            def _register(self, _page, _context):
                self.original_register_calls += 1
                return "registered"

            def _fill_email_if_visible(self, _page):
                self.original_fill_calls += 1
                return False

            def _visible_inputs(self, _page, _selectors):
                return [EmailInput()]

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        page = Page()
        context = Context()
        self.assertTrue(
            configure_email_password_only_registration(worker, enabled=True)
        )
        self.assertEqual(worker._register(page, context), "registered")
        def copy_clipboard(value):
            clipboard["value"] = value
            events.append(("clipboard", value))

        with patch(
            "hidemyemail_generator.openai_browser_bridge."
            "_copy_registration_clipboard_text",
            side_effect=copy_clipboard,
        ):
            self.assertTrue(worker._fill_email_if_visible(page))

        self.assertEqual(worker.original_register_calls, 1)
        self.assertEqual(worker.original_fill_calls, 0)
        self.assertEqual(
            events,
            [
                ("click", "email-input"),
                ("press", "Control+A"),
                ("clipboard", "password.only@gmail.com"),
                ("press", "Control+V"),
                ("clipboard", ""),
                ("click", "registration-submit"),
            ],
        )
        self.assertEqual(len(context.routes), 2)
        context.routes[0][1](Route())
        self.assertEqual(events[-1], "abort-google")
        self.assertTrue(
            any(
                "邮箱输入检查通过：password.only@gmail.com" in message
                for message in worker.logs
            )
        )
        self.assertIn("不会匹配登录按钮", worker.logs[-1])

    def test_gmail_registration_requests_fresh_fingerprint_on_google_page(self):
        class Page:
            url = "https://accounts.google.com/v3/signin/identifier"

            @staticmethod
            def go_back(**_kwargs):
                raise AssertionError("must close this browser, not navigate back")

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="password.only@gmail.com")
                self.logs = []

            def _register(self, _page, _context):
                return None

            def _fill_email_if_visible(self, _page):
                raise AssertionError("must not fill the Google account form")

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        page = Page()
        self.assertTrue(
            configure_email_password_only_registration(worker, enabled=True)
        )
        with self.assertRaisesRegex(
            FreshFingerprintRequiredError,
            "关闭当前浏览器并更换全新指纹",
        ):
            worker._fill_email_if_visible(page)
        self.assertIn("本轮注册立即判定失败", "\n".join(worker.logs))

    def test_google_monitor_stops_current_browser_without_same_context_recovery(self):
        class Page:
            url = "https://accounts.google.com/v3/signin/identifier"

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="password.only@gmail.com")
                self.logs = []
                self.continue_calls = 0

            def _register(self, _page, _context):
                return None

            def _fill_email_if_visible(self, _page):
                return False

            def _continue_chatgpt_registration_complete(self, _page):
                self.continue_calls += 1
                return False

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        page = Page()
        self.assertTrue(
            configure_email_password_only_registration(worker, enabled=True)
        )

        with self.assertRaises(FreshFingerprintRequiredError):
            worker._continue_chatgpt_registration_complete(page)
        self.assertEqual(worker.continue_calls, 0)
        self.assertIn("请求生成全新指纹", worker.logs[-1])

    def test_background_registration_never_activates_page_or_os_window(self):
        events = []

        class Page:
            def bring_to_front(self):
                events.append("page")

            def evaluate(self, _script):
                events.append("dom")

        worker = SimpleNamespace(headless=False, log=lambda _message: None)
        with (
            patch.dict(os.environ, {"HME_BROWSER_FOREGROUND_REQUIRED": ""}),
            patch(
                "hidemyemail_generator.openai_browser_bridge."
                "_focus_camoufox_window_once",
                return_value=True,
            ) as focus_window,
        ):
            self.assertFalse(_activate_visible_registration_page(worker, Page()))

        self.assertEqual(events, [])
        focus_window.assert_not_called()

    def test_manual_registration_uses_one_shot_window_focus(self):
        events = []

        class Page:
            def bring_to_front(self):
                events.append("page")

            def evaluate(self, _script):
                events.append("dom")

        worker = SimpleNamespace(headless=False, log=lambda _message: None)
        with (
            patch.dict(os.environ, {"HME_BROWSER_FOREGROUND_REQUIRED": "1"}),
            patch(
                "hidemyemail_generator.openai_browser_bridge."
                "_focus_camoufox_window_once",
                return_value=True,
            ) as focus_window,
        ):
            self.assertTrue(_activate_visible_registration_page(worker, Page()))
            self.assertFalse(_activate_visible_registration_page(worker, Page()))

        focus_window.assert_called_once_with()
        self.assertEqual(events, ["page", "dom"])

    def test_smsbower_gmail_uses_local_registration_code_reader(self):
        events = []

        class Reader:
            def __init__(self, account, log, proxy_url):
                events.append(("reader", account.email, proxy_url))

            def connect(self):
                events.append(("connect",))

            def wait_for_code(self, min_timestamp):
                events.append(("wait", min_timestamp))
                return "654321"

        class Worker:
            def __init__(self, email):
                self.account = SimpleNamespace(email=email)
                self.otp_reader = None
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _preconnect_otp_reader(self):
                events.append(("original-preconnect", self.account.email))

            def _wait_for_openai_email_code(self, min_timestamp):
                events.append(("original-wait", min_timestamp))
                return "original"

        backend = SimpleNamespace(
            OpenAIRegisterPayLinkWorker=Worker,
            HotmailOtpReader=Reader,
        )
        self.assertTrue(
            configure_registration_otp_reader(backend, "Bought@gmail.com")
        )

        gmail_worker = Worker("bought@gmail.com")
        gmail_worker._preconnect_otp_reader()
        self.assertEqual(
            gmail_worker._wait_for_openai_email_code(123.0),
            "654321",
        )
        self.assertEqual(
            events,
            [
                ("reader", "bought@gmail.com", ""),
                ("connect",),
                ("wait", 123.0),
            ],
        )
        self.assertIn("SMSBower API 自动取码", gmail_worker.logs[0])

        icloud_worker = Worker("stock@icloud.com")
        icloud_worker._preconnect_otp_reader()
        self.assertEqual(
            icloud_worker._wait_for_openai_email_code(456.0),
            "original",
        )
        self.assertEqual(
            events[-2:],
            [
                ("original-preconnect", "stock@icloud.com"),
                ("original-wait", 456.0),
            ],
        )

    def test_standalone_gmail_2fa_falls_back_to_next_smsbower_code(self):
        class Response:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self.payload = payload
                self.ok = 200 <= status_code < 300

            def json(self):
                return dict(self.payload)

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs.get("json") or {}))
                if url.endswith("/api/registration/code/poll"):
                    return Response(
                        409,
                        {
                            "ok": False,
                            "error": "该邮箱当前没有正在运行的注册任务",
                        },
                    )
                if url.endswith("/api/gpt-code"):
                    return Response(200, {"ok": True, "code": "654321"})
                raise AssertionError(url)

            def close(self):
                return None

        logs = []
        with patch.dict(
            sys.modules,
            {"requests": SimpleNamespace(Session=Session)},
        ):
            reader = ManualOtpReader(
                SimpleNamespace(email="standalone@gmail.com"), logs.append, ""
            )
        reader.token = "test-token"

        code = reader.wait_for_code(123.0)

        self.assertEqual(code, "654321")
        self.assertEqual(
            [url.rsplit("/", 2)[-2:] for url, _payload in reader.session.calls],
            [["code", "poll"], ["api", "gpt-code"]],
        )
        self.assertEqual(
            reader.session.calls[1][1]["email"], "standalone@gmail.com"
        )
        self.assertIn("SMSBower 邮件历史", logs[0])
        self.assertNotIn("654321", " ".join(logs))

    def test_icloud_reader_uses_gpt_code_route_directly(self):
        class Response:
            status_code = 200
            ok = True

            @staticmethod
            def json():
                return {"ok": True, "code": "938388"}

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs.get("json") or {}))
                return Response()

            def close(self):
                return None

        with patch.dict(sys.modules, {"requests": SimpleNamespace(Session=Session)}):
            reader = ManualOtpReader(
                SimpleNamespace(email="relay@icloud.com"), lambda _message: None, ""
            )
        reader.token = "test-token"

        self.assertEqual(reader.wait_for_code(123.0), "938388")
        self.assertEqual(len(reader.session.calls), 1)
        url, payload = reader.session.calls[0]
        self.assertTrue(url.endswith("/api/gpt-code"))
        self.assertEqual(payload["email"], "relay@icloud.com")
        self.assertEqual(
            payload["since"],
            "1970-01-01T00:01:33+00:00",
        )

    def test_smsbower_backend_code_fills_japanese_code_field(self):
        class Reader:
            def __init__(self, _account, _log, _proxy_url):
                pass

            def connect(self):
                return None

            def wait_for_code(self, _min_timestamp):
                return "654321"

        class Field:
            def __init__(self):
                self.value = ""

            def fill(self, value):
                self.value = value

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="bought@gmail.com")
                self.otp_reader = None
                self.field = Field()
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _preconnect_otp_reader(self):
                return None

            def _wait_for_openai_email_code(self, _min_timestamp):
                return "original"

            def _visible_inputs(self, _page, selectors):
                if 'input[id="code"]' in selectors:
                    return [self.field]
                return []

            def _submit_email_code(
                self, page, min_timestamp, *, wait_for_session=True
            ):
                del wait_for_session
                code = self._wait_for_openai_email_code(min_timestamp)
                inputs = self._visible_inputs(
                    page,
                    [
                        'input[autocomplete="one-time-code"]',
                        'input[inputmode="numeric"]',
                        'input[type="tel"]',
                        'input[name="code"]',
                    ],
                )
                if not inputs:
                    raise RuntimeError("本地化 Code 输入框未识别")
                inputs[0].fill(code)

        backend = SimpleNamespace(
            OpenAIRegisterPayLinkWorker=Worker,
            HotmailOtpReader=Reader,
        )
        self.assertTrue(
            configure_registration_otp_reader(backend, "bought@gmail.com")
        )

        worker = Worker()
        worker._submit_email_code(object(), 0)

        self.assertEqual(worker.field.value, "654321")
        self.assertIn("已识别本地化 Code 输入框", worker.logs[-1])

    def test_smsbower_code_waits_for_verification_input_rerender(self):
        class Reader:
            def __init__(self, _account, _log, _proxy_url):
                pass

            def connect(self):
                return None

            def wait_for_code(self, _min_timestamp):
                return "654321"

        class Field:
            def __init__(self):
                self.value = ""

            def fill(self, value):
                self.value = value

        class Page:
            url = "https://auth.openai.com/email-verification"

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="bought@gmail.com")
                self.otp_reader = None
                self.field = Field()
                self.localized_lookups = 0
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _preconnect_otp_reader(self):
                return None

            def _wait_for_openai_email_code(self, _min_timestamp):
                return "original"

            def _visible_inputs(self, _page, selectors):
                if 'input[id="code"]' in selectors:
                    self.localized_lookups += 1
                    if self.localized_lookups >= 2:
                        return [self.field]
                return []

            def _submit_email_code(
                self, page, min_timestamp, *, wait_for_session=True
            ):
                del wait_for_session
                code = self._wait_for_openai_email_code(min_timestamp)
                inputs = self._visible_inputs(
                    page,
                    [
                        'input[autocomplete="one-time-code"]',
                        'input[inputmode="numeric"]',
                        'input[type="tel"]',
                        'input[name="code"]',
                    ],
                )
                if not inputs:
                    raise RuntimeError("验证码输入框未恢复")
                inputs[0].fill(code)

        backend = SimpleNamespace(
            OpenAIRegisterPayLinkWorker=Worker,
            HotmailOtpReader=Reader,
        )
        self.assertTrue(
            configure_registration_otp_reader(backend, "bought@gmail.com")
        )

        worker = Worker()
        with patch(
            "hidemyemail_generator.openai_browser_bridge.time.sleep"
        ) as sleep:
            worker._submit_email_code(Page(), 0)

        self.assertEqual(worker.field.value, "654321")
        self.assertGreaterEqual(worker.localized_lookups, 2)
        sleep.assert_called_once_with(0.25)

    def test_icloud_japanese_verification_ui_is_identified_before_code_fill(self):
        class Reader:
            def __init__(self, _account, _log, _proxy_url):
                pass

            def connect(self):
                return None

            def wait_for_code(self, _min_timestamp):
                return "938388"

        class Field:
            def __init__(self):
                self.value = ""

            def fill(self, value):
                self.value = value

        class Body:
            @staticmethod
            def inner_text(**_kwargs):
                return (
                    "受信箱を確認してください "
                    "lager.inviter-1v@icloud.com にお送りした検証コードを"
                    "入力してください。コード 続行 メールを再送信する"
                )

        class Page:
            url = "https://auth.openai.com/email-verification"

            @staticmethod
            def locator(selector):
                if selector == "body":
                    return Body()
                raise AssertionError(selector)

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="lager.inviter-1v@icloud.com")
                self.otp_reader = None
                self.field = Field()
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _preconnect_otp_reader(self):
                return None

            def _wait_for_openai_email_code(self, _min_timestamp):
                return "original"

            def _visible_inputs(self, _page, selectors):
                if any(
                    selector
                    in {
                        'input:not([type])',
                        'input[type="text"]',
                        'input[type="number"]',
                        'input[role="textbox"]',
                    }
                    for selector in selectors
                ):
                    return [self.field]
                return []

            def _submit_email_code(
                self, page, min_timestamp, *, wait_for_session=True
            ):
                del wait_for_session
                code = self._wait_for_openai_email_code(min_timestamp)
                inputs = self._visible_inputs(
                    page,
                    [
                        'input[autocomplete="one-time-code"]',
                        'input[inputmode="numeric"]',
                        'input[type="tel"]',
                        'input[name="code"]',
                    ],
                )
                if not inputs:
                    raise RuntimeError("验证码输入框未识别")
                inputs[0].fill(code)

        backend = SimpleNamespace(
            OpenAIRegisterPayLinkWorker=Worker,
            HotmailOtpReader=Reader,
        )
        self.assertTrue(
            configure_registration_otp_reader(
                backend, "lager.inviter-1v@icloud.com"
            )
        )

        worker = Worker()
        worker._submit_email_code(Page(), 0)

        self.assertEqual(worker.field.value, "938388")
        self.assertTrue(
            any("已结合当前注册上下文" in message for message in worker.logs)
        )
        self.assertTrue(
            any("准备填写本轮验证码" in message for message in worker.logs)
        )

    def test_icloud_code_wait_stops_after_browser_leaves_verification_page(self):
        events = []

        class Reader:
            def __init__(self, _account, _log, _proxy_url):
                self.page_advanced = None

            def connect(self):
                return None

            def wait_for_code(self, _min_timestamp):
                events.append("wait")
                if callable(self.page_advanced) and self.page_advanced():
                    raise EmailVerificationPageAdvanced
                raise AssertionError("page advance was not detected")

        class Body:
            @staticmethod
            def inner_text(**_kwargs):
                return ""

        class Page:
            url = "https://auth.openai.com/about-you"

            @staticmethod
            def locator(selector):
                if selector == "body":
                    return Body()
                raise AssertionError(selector)

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="advanced@icloud.com")
                self.otp_reader = None
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _preconnect_otp_reader(self):
                return None

            def _wait_for_openai_email_code(self, _min_timestamp):
                return "original"

            def _visible_inputs(self, _page, _selectors):
                return []

            def _submit_email_code(
                self, page, min_timestamp, *, wait_for_session=True
            ):
                del page, wait_for_session
                events.append("submit")
                self._wait_for_openai_email_code(min_timestamp)
                events.append("fill")

        backend = SimpleNamespace(
            OpenAIRegisterPayLinkWorker=Worker,
            HotmailOtpReader=Reader,
        )
        self.assertTrue(
            configure_registration_otp_reader(backend, "advanced@icloud.com")
        )

        worker = Worker()
        worker._preconnect_otp_reader()
        self.assertIsNone(worker._submit_email_code(Page(), 0))

        self.assertEqual(events, ["submit", "wait"])
        self.assertTrue(
            any("页面已离开邮箱验证码页" in message for message in worker.logs)
        )

    def test_icloud_code_network_error_falls_back_to_visible_continue(self):
        actions = []

        class Reader:
            def __init__(self, _account, _log, _proxy_url):
                self.page_advanced = None

            def connect(self):
                return None

        class Page:
            url = "https://auth.openai.com/email-verification"

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="fallback@icloud.com")
                self.otp_reader = None
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _preconnect_otp_reader(self):
                return None

            def _wait_for_openai_email_code(self, _min_timestamp):
                return "123456"

            def _visible_inputs(self, _page, _selectors):
                return []

            def _click_continue(self, _page):
                actions.append("click-continue")
                return True

            def _wait_after_otp_submit(self, _page):
                actions.append("wait-after-submit")

            def _submit_email_code(
                self, page, min_timestamp, *, wait_for_session=True
            ):
                del page, min_timestamp, wait_for_session
                raise RuntimeError(
                    "Page.evaluate: NetworkError when attempting to fetch resource."
                )

        backend = SimpleNamespace(
            OpenAIRegisterPayLinkWorker=Worker,
            HotmailOtpReader=Reader,
        )
        self.assertTrue(
            configure_registration_otp_reader(backend, "fallback@icloud.com")
        )

        worker = Worker()
        self.assertIsNone(worker._submit_email_code(Page(), 0))

        self.assertEqual(actions, ["click-continue", "wait-after-submit"])
        self.assertTrue(
            any("已改用页面可见的继续按钮提交" in message for message in worker.logs)
        )

    def test_icloud_code_uses_visible_submit_without_refresh(self):
        actions = []

        class Reader:
            def __init__(self, _account, _log, _proxy_url):
                self.page_advanced = None

            def connect(self):
                return None

            def wait_for_code(self, _min_timestamp):
                return "123456"

        class Field:
            value = ""

            def input_value(self, **_kwargs):
                return self.value

            def fill(self, value):
                self.value = value

        class Button:
            @staticmethod
            def is_visible(**_kwargs):
                return True

            @staticmethod
            def is_enabled(**_kwargs):
                return True

            @staticmethod
            def click(**_kwargs):
                actions.append("click-visible-continue")

        class Collection:
            def __init__(self, items=()):
                self.items = list(items)

            def count(self):
                return len(self.items)

            @property
            def first(self):
                return self.items[0]

        class Body:
            @staticmethod
            def inner_text(**_kwargs):
                return (
                    "受信箱を確認してください visible@icloud.com にお送りした"
                    "検証コードを入力してください。コード 続行"
                )

        class Page:
            url = "https://auth.openai.com/email-verification"

            def __init__(self):
                self.field = Field()

            def locator(self, selector):
                if selector == "body":
                    return Body()
                if "続行" in selector or 'button[type="submit"]' == selector:
                    return Collection([Button()])
                return Collection()

            @staticmethod
            def wait_for_timeout(_milliseconds):
                return None

            @staticmethod
            def reload(**_kwargs):
                raise AssertionError("验证码页不得刷新")

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="visible@icloud.com")
                self.otp_reader = None
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _preconnect_otp_reader(self):
                return None

            def _wait_for_openai_email_code(self, _min_timestamp):
                return "original"

            def _visible_inputs(self, page, selectors):
                if any(
                    marker in selector
                    for selector in selectors
                    for marker in ("one-time-code", 'id="code"', 'name="code"')
                ):
                    return [page.field]
                if any(
                    selector
                    in {
                        'input:not([type])',
                        'input[type="text"]',
                        'input[type="number"]',
                        'input[type="tel"]',
                        'input[role="textbox"]',
                    }
                    for selector in selectors
                ):
                    return [page.field]
                return []

            def _validate_email_code_api(self, _page, _code):
                actions.append("api-validate")
                return ""

            def _wait_after_otp_submit(self, _page):
                actions.append("wait-after-submit")

            def _submit_email_code(
                self, page, min_timestamp, *, wait_for_session=True
            ):
                code = self._wait_for_openai_email_code(min_timestamp)
                page.field.fill(code)
                ensure_email_verification_code_entered(self, page, code)
                self._validate_email_code_api(page, code)
                self.log("已通过接口提交邮箱验证码")
                if wait_for_session:
                    self._wait_after_otp_submit(page)

        backend = SimpleNamespace(
            OpenAIRegisterPayLinkWorker=Worker,
            HotmailOtpReader=Reader,
        )
        self.assertTrue(
            configure_registration_otp_reader(backend, "visible@icloud.com")
        )

        page = Page()
        worker = Worker()
        worker._submit_email_code(page, 0)

        self.assertEqual(page.field.value, "123456")
        self.assertEqual(
            actions,
            ["click-visible-continue", "wait-after-submit"],
        )
        self.assertNotIn("api-validate", actions)
        self.assertTrue(worker._hme_otp_visible_submit)
        self.assertTrue(any("未刷新验证码页面" in line for line in worker.logs))

    def test_icloud_visible_submit_is_blocked_without_input_attestation(self):
        actions = []

        class Field:
            @staticmethod
            def input_value(**_kwargs):
                return "123456"

        class Button:
            @staticmethod
            def is_visible(**_kwargs):
                return True

            @staticmethod
            def is_enabled(**_kwargs):
                return True

            @staticmethod
            def click(**_kwargs):
                actions.append("clicked")

        class Collection:
            def __init__(self, items=()):
                self.items = list(items)

            def count(self):
                return len(self.items)

            @property
            def first(self):
                return self.items[0]

        class Body:
            @staticmethod
            def inner_text(**_kwargs):
                return (
                    "检查收件箱 blocked@icloud.com 验证码已经发送，"
                    "请输入验证码，然后继续"
                )

        class Page:
            url = "https://auth.openai.com/email-verification"

            def __init__(self):
                self.field = Field()

            def locator(self, selector):
                if selector == "body":
                    return Body()
                if "继续" in selector or selector == 'button[type="submit"]':
                    return Collection([Button()])
                return Collection()

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="blocked@icloud.com")

            def log(self, _message):
                return None

            def _preconnect_otp_reader(self):
                return None

            def _wait_for_openai_email_code(self, _min_timestamp):
                return "123456"

            def _visible_inputs(self, page, selectors):
                if any("input" in selector for selector in selectors):
                    return [page.field]
                return []

            def _validate_email_code_api(self, _page, _code):
                return ""

        backend = SimpleNamespace(
            OpenAIRegisterPayLinkWorker=Worker,
            HotmailOtpReader=object,
        )
        self.assertTrue(
            configure_registration_otp_reader(backend, "blocked@icloud.com")
        )

        with self.assertRaisesRegex(RuntimeError, "尚未完成回读确认"):
            Worker()._validate_email_code_api(Page(), "123456")
        self.assertEqual(actions, [])

    def test_icloud_code_fetch_is_checked_before_browser_input(self):
        class Reader:
            def __init__(self, _account, _log, _proxy_url):
                pass

            def connect(self):
                return None

            def wait_for_code(self, _min_timestamp):
                return "123456"

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="checked@icloud.com")
                self.otp_reader = None
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _preconnect_otp_reader(self):
                return None

            def _wait_for_openai_email_code(self, _min_timestamp):
                return "original"

            def _visible_inputs(self, _page, _selectors):
                return []

        backend = SimpleNamespace(
            OpenAIRegisterPayLinkWorker=Worker,
            HotmailOtpReader=Reader,
        )
        self.assertTrue(
            configure_registration_otp_reader(backend, "checked@icloud.com")
        )

        worker = Worker()
        self.assertEqual(worker._wait_for_openai_email_code(0), "123456")
        self.assertTrue(any("获取检查通过" in line for line in worker.logs))

    def test_icloud_code_fetch_blocks_non_numeric_result(self):
        class Reader:
            def __init__(self, _account, _log, _proxy_url):
                pass

            def connect(self):
                return None

            def wait_for_code(self, _min_timestamp):
                return "12AB56"

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="numeric@icloud.com")
                self.otp_reader = None

            def log(self, _message):
                return None

            def _preconnect_otp_reader(self):
                return None

            def _wait_for_openai_email_code(self, _min_timestamp):
                return "original"

            def _visible_inputs(self, _page, _selectors):
                return []

        backend = SimpleNamespace(
            OpenAIRegisterPayLinkWorker=Worker,
            HotmailOtpReader=Reader,
        )
        self.assertTrue(
            configure_registration_otp_reader(backend, "numeric@icloud.com")
        )

        with self.assertRaisesRegex(RuntimeError, "不是 6 位数字"):
            Worker()._wait_for_openai_email_code(0)

    def test_icloud_code_resend_accepts_mail_header_clock_skew(self):
        actions = []
        submitted_timestamps = []

        class Reader:
            def __init__(self, _account, _log, _proxy_url):
                self.page_advanced = None

            def connect(self):
                return None

        class Candidate:
            def is_visible(self, **_kwargs):
                return True

            def click(self, **_kwargs):
                actions.append("resend")

        class Collection:
            def __init__(self, items=()):
                self.items = list(items)

            def count(self):
                return len(self.items)

            @property
            def first(self):
                return self.items[0]

        class Page:
            url = "https://auth.openai.com/email-verification"

            def locator(self, selector):
                if "Resend email" in selector:
                    return Collection([Candidate()])
                return Collection()

            def wait_for_timeout(self, _milliseconds):
                return None

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="skew@icloud.com")
                self.otp_reader = None
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _preconnect_otp_reader(self):
                return None

            def _wait_for_openai_email_code(self, _min_timestamp):
                return "123456"

            def _visible_inputs(self, _page, _selectors):
                return []

            def _submit_email_code(
                self, _page, min_timestamp, *, wait_for_session=True
            ):
                del wait_for_session
                submitted_timestamps.append(min_timestamp)

        backend = SimpleNamespace(
            OpenAIRegisterPayLinkWorker=Worker,
            HotmailOtpReader=Reader,
        )
        self.assertTrue(configure_registration_otp_reader(backend, "skew@icloud.com"))

        worker = Worker()
        worker._submit_email_code(Page(), 0)

        self.assertEqual(actions, ["resend"])
        self.assertGreater(submitted_timestamps[0], time.time() - 40)
        self.assertLess(submitted_timestamps[0], time.time() - 20)
        self.assertTrue(any("本次任务的新验证码" in line for line in worker.logs))

    def test_stalled_otp_session_reenters_without_closing_visible_pages(self):
        actions = []

        class Candidate:
            @staticmethod
            def is_visible(**_kwargs):
                return True

        class Collection:
            def __init__(self, items=()):
                self.items = list(items)

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        class OtherPage:
            def close(self):
                actions.append("closed-other-page")

        class Context:
            def __init__(self):
                self.pages = [OtherPage()]

            def clear_cookies(self):
                actions.append("clear-cookies")

        class Page:
            def __init__(self):
                self.url = "https://auth.openai.com/email-verification"

            def is_closed(self):
                return False

            def goto(self, url, **_kwargs):
                actions.append(("goto", url))
                if url == "https://chatgpt.com/":
                    self.url = "https://chatgpt.com/auth/signup"
                else:
                    self.url = url

            def wait_for_timeout(self, _milliseconds):
                return None

            def locator(self, selector):
                if (
                    selector in OPENAI_EMAIL_LOGIN_INPUT_SELECTORS
                    and self.url == "https://chatgpt.com/auth/signup"
                ):
                    return Collection([Candidate()])
                return Collection()

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="lager.inviter-1v@icloud.com")
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _fill_email_if_visible(self, page):
                actions.append("fill-current-email")
                page.url = "https://auth.openai.com/email-verification"
                return True

            def _create_openai_signin_url(self, _context):
                return "https://auth.openai.com/create-account"

            def _create_login_url(self, _context):
                return "https://auth.openai.com/log-in"

            def _goto_auth_page(self, page, url):
                actions.append(("direct-auth", url))
                page.goto(url)

            def _restart_login_after_stalled_session(self, page, context):
                for candidate in context.pages:
                    candidate.close()
                page.goto("https://chatgpt.com/")

            def _register(self, page, context, **_kwargs):
                self._restart_login_after_stalled_session(page, context)
                return page.url

        worker = Worker()
        page = Page()
        context = Context()
        self.assertTrue(configure_chatgpt_home_login_entry(worker))

        result = worker._register(page, context, existing_login_only=False)

        self.assertEqual(result, "https://auth.openai.com/email-verification")
        self.assertIn("clear-cookies", actions)
        self.assertIn("fill-current-email", actions)
        self.assertNotIn("closed-other-page", actions)
        self.assertFalse(
            any(action[0] == "direct-auth" for action in actions if isinstance(action, tuple))
        )
        self.assertTrue(any("不关闭可见验证码窗口" in log for log in worker.logs))
        self.assertTrue(any("重新建立同一邮箱登录流程" in log for log in worker.logs))

    def test_manual_email_waits_for_browser_submission_without_code_reader(self):
        events = []

        class Page:
            url = "https://auth.openai.com/email-verification"

            def bring_to_front(self):
                events.append("front")

            def evaluate(self, _script):
                events.append("focus")

        class Worker:
            headless = False
            otp_reader = object()

            def __init__(self):
                self.logs = []
                self.otp_states = [True, False]

            def log(self, message):
                self.logs.append(message)

            def _preconnect_otp_reader(self):
                events.append("original-preconnect")

            def _submit_email_code(self, *_args, **_kwargs):
                events.append("original-submit")

            def _raise_if_page_closed(self, _page, _action):
                return None

            def _has_chatgpt_session(self, _page):
                return False

            def _has_otp_input(self, _page):
                return self.otp_states.pop(0)

        worker = Worker()
        self.assertTrue(configure_manual_browser_verification(worker, enabled=True))

        with (
            patch.dict(os.environ, {"HME_BROWSER_FOREGROUND_REQUIRED": "1"}),
            patch(
                "hidemyemail_generator.openai_browser_bridge.time.sleep",
                return_value=None,
            ),
        ):
            worker._preconnect_otp_reader()
            worker._submit_email_code(Page(), 0)

        self.assertIsNone(worker.otp_reader)
        self.assertNotIn("original-preconnect", events)
        self.assertNotIn("original-submit", events)
        self.assertEqual(events.count("front"), 1)
        self.assertEqual(events.count("focus"), 1)
        self.assertTrue(any("不连接 IMAP" in item for item in worker.logs))
        self.assertTrue(any("继续完成后续注册操作" in item for item in worker.logs))

    def test_registration_proxy_country_must_match_detected_exit(self):
        self.assertEqual(
            require_registration_proxy_country(
                SimpleNamespace(country="NL", chatgpt_status=200), "NL"
            ),
            "NL",
        )
        with self.assertRaisesRegex(RuntimeError, "NL"):
            require_registration_proxy_country(
                SimpleNamespace(country="BR", chatgpt_status=200), "NL"
            )

    def test_registration_proxy_accepts_chatgpt_403_for_browser_verification(self):
        self.assertEqual(
            require_registration_proxy_country(
                SimpleNamespace(country="JP", chatgpt_status=403), "JP"
            ),
            "JP",
        )

    def test_registration_proxy_rejects_unknown_chatgpt_status(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 未知"):
            require_registration_proxy_country(SimpleNamespace(country="JP"), "JP")

    def test_registration_proxy_accepts_403_without_retry(self):
        calls = []
        logs = []
        delays = []

        def prepare(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(country="JP", chatgpt_status=403)

        health, country = prepare_registration_proxy(
            SimpleNamespace(_prepare_fingerprint_for_proxy=prepare),
            SimpleNamespace(
                require_registration_proxy_country=require_registration_proxy_country,
                emit=lambda kind, **payload: logs.append((kind, payload)),
            ),
            "proxy",
            "JP",
            sleep=delays.append,
        )

        self.assertEqual(health.chatgpt_status, 403)
        self.assertEqual(country, "JP")
        self.assertEqual(len(calls), 1)
        self.assertEqual(delays, [])
        self.assertEqual(len(logs), 1)
        self.assertIn("不再拦截", logs[0][1]["message"])

    def test_registration_proxy_does_not_repeat_403_probe(self):
        calls = []
        delays = []

        def prepare(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(country="JP", chatgpt_status=403)

        health, country = prepare_registration_proxy(
            SimpleNamespace(_prepare_fingerprint_for_proxy=prepare),
            SimpleNamespace(
                require_registration_proxy_country=require_registration_proxy_country,
                emit=lambda *_args, **_kwargs: None,
            ),
            "proxy",
            "JP",
            sleep=delays.append,
        )

        self.assertEqual(health.chatgpt_status, 403)
        self.assertEqual(country, "JP")
        self.assertEqual(len(calls), 1)
        self.assertEqual(delays, [])

    def test_registration_proxy_retries_probe_timeout_until_200(self):
        outcomes = iter(
            (
                RuntimeError("curl: (28) Connection timed out"),
                SimpleNamespace(country="JP", chatgpt_status=403),
                SimpleNamespace(country="JP", chatgpt_status=200),
            )
        )
        calls = []
        logs = []
        delays = []

        def prepare(*args, **kwargs):
            calls.append((args, kwargs))
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        health, country = prepare_registration_proxy(
            SimpleNamespace(_prepare_fingerprint_for_proxy=prepare),
            SimpleNamespace(
                require_registration_proxy_country=require_registration_proxy_country,
                emit=lambda kind, **payload: logs.append((kind, payload)),
            ),
            "proxy",
            "JP",
            sleep=delays.append,
        )

        self.assertEqual(health.chatgpt_status, 403)
        self.assertEqual(country, "JP")
        self.assertEqual(len(calls), 2)
        self.assertEqual(delays, [1.0])
        self.assertIn("探测请求暂时失败", logs[0][1]["message"])
        self.assertIn("HTTP 403", logs[1][1]["message"])

    def test_reads_chatgpt_plan_from_jwt_without_a_request(self):
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "exp": int(time.time()) + 3600,
                    "https://api.openai.com/auth": {
                        "chatgpt_plan_type": "plus"
                    },
                }
            ).encode("utf-8")
        ).decode("ascii").rstrip("=")

        self.assertEqual(
            jwt_account_type(f"header.{payload}.signature"),
            ("plus", "plus"),
        )

    def test_reads_top_level_free_plan_and_ignores_invalid_tokens(self):
        payload = base64.urlsafe_b64encode(
            json.dumps({"chatgpt_plan_type": "free"}).encode("utf-8")
        ).decode("ascii").rstrip("=")

        self.assertEqual(
            jwt_account_type(f"header.{payload}.signature"),
            ("free", "free"),
        )
        self.assertEqual(jwt_account_type("not-a-jwt"), ("", ""))

    def test_japanese_completed_onboarding_is_dismissed_before_settings(self):
        actions = []
        logs = []

        class Candidate:
            def __init__(self, page, kind):
                self.page = page
                self.kind = kind

            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                actions.append(self.kind)
                if self.kind == "continue":
                    self.page.state = "home"

        class Collection:
            def __init__(self, candidates):
                self.candidates = candidates

            def count(self):
                return len(self.candidates)

            def nth(self, index):
                return self.candidates[index]

        class Page:
            def __init__(self):
                self.state = "onboarding"

            def wait_for_timeout(self, _milliseconds):
                return None

            def locator(self, selector):
                candidates = []
                if (
                    self.state == "onboarding"
                    and selector == 'text="準備が完了しました"'
                ):
                    candidates = [Candidate(self, "marker")]
                elif (
                    self.state == "onboarding"
                    and selector == 'button:has-text("続行")'
                ):
                    candidates = [Candidate(self, "continue")]
                return Collection(candidates)

        page = Page()
        worker = SimpleNamespace(log=logs.append)

        self.assertTrue(_dismiss_completed_onboarding(page, worker))
        self.assertEqual(page.state, "home")
        self.assertEqual(actions, ["continue"])
        self.assertIn("首次使用欢迎页", logs[-1])

    def test_mfa_invalidated_token_error_is_retryable(self):
        self.assertTrue(
            _mfa_token_was_invalidated(
                RuntimeError(
                    "创建 2FA 验证器失败：HTTP 401 · "
                    "Your authentication token has been invalidated."
                )
            )
        )
        self.assertFalse(
            _mfa_token_was_invalidated(RuntimeError("创建 2FA 验证器失败：HTTP 500"))
        )
        self.assertTrue(
            _mfa_token_was_invalidated(
                RuntimeError(
                    "创建 2FA 验证器失败：HTTP 401 · "
                    "User must re-authenticate to enroll/disable a factor · "
                    "recent_auth_required"
                )
            )
        )

    def test_japanese_email_code_page_switches_to_password(self):
        actions = []

        class Candidate:
            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                actions.append("password")
                self.page.state = "password"
                self.page.url = "https://auth.openai.com/sign-up/password"

            def __init__(self, page):
                self.page = page

        class Collection:
            def __init__(self, items, text=""):
                self.items = items
                self.text = text

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

            def inner_text(self, **_kwargs):
                return self.text

        class Page:
            def __init__(self):
                self.state = "otp"
                self.url = "https://auth.openai.com/email-verification"

            def locator(self, selector):
                if selector == "body":
                    return Collection(
                        [],
                        "受信箱を確認してください "
                        "lager.inviter-1v@icloud.com に送信した6桁のコード "
                        "パスワードで続行",
                    )
                if self.state == "otp" and "パスワードで続行" in selector:
                    return Collection([Candidate(self)])
                if self.state == "password" and selector == 'input[type="password"]':
                    return Collection([Candidate(self)])
                return Collection([])

        class Worker:
            def __init__(self):
                self.original_calls = 0
                self.continue_calls = 0
                self.logs = []
                self.account = SimpleNamespace(
                    email="lager.inviter-1v@icloud.com"
                )

            def _continue_chatgpt_registration_complete(self, _page):
                self.continue_calls += 1
                return False

            def _has_otp_input(self, page):
                return page.state in {"otp", "post_password_otp"}

            def _has_visible_password(self, page):
                return page.state == "password"

            def _fill_password_step(self, page):
                self.original_calls += 1
                page.state = "post_password_otp"
                page.url = "https://auth.openai.com/email-verification"

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        page = Page()
        self.assertTrue(
            configure_password_first_login(worker, enabled=True, required=True)
        )
        worker._continue_chatgpt_registration_complete(page)

        self.assertEqual(actions, ["password"])
        self.assertEqual(worker.continue_calls, 0)
        self.assertEqual(worker.original_calls, 1)
        self.assertTrue(worker._password_step_submitted)
        self.assertTrue(any("唯一密码" in line for line in worker.logs))
        self.assertTrue(any("Session 判定=暂缓" in line for line in worker.logs))
        recognition = next(
            line for line in worker.logs if line.startswith("[界面识别]")
        )
        self.assertIn("语言=日文", recognition)
        self.assertIn("目标邮箱=匹配", recognition)
        self.assertIn("使用密码继续=可见", recognition)
        self.assertIn("决策=点击", recognition)

        page.state = "post_password_otp"
        worker._continue_chatgpt_registration_complete(page)
        self.assertTrue(worker._has_otp_input(page))
        self.assertEqual(actions, ["password"])
        self.assertEqual(worker.continue_calls, 1)

    def test_direct_email_code_submit_forces_password_then_uses_new_code_window(self):
        actions = []
        submitted_timestamps = []

        class Candidate:
            def __init__(self, page):
                self.page = page

            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                actions.append("password-choice")
                self.page.state = "password"
                self.page.url = "https://auth.openai.com/create-account/password"

        class Collection:
            def __init__(self, items=(), text=""):
                self.items = list(items)
                self.text = text

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

            def inner_text(self, **_kwargs):
                return self.text

        class Page:
            def __init__(self):
                self.state = "otp"
                self.url = "https://auth.openai.com/email-verification"

            def locator(self, selector):
                if selector == "body":
                    return Collection(text="受信箱を確認してください パスワードで続行")
                if self.state == "otp" and "パスワードで続行" in selector:
                    return Collection([Candidate(self)])
                return Collection()

            def wait_for_timeout(self, _milliseconds):
                return None

        class Worker:
            headless = False

            def __init__(self):
                self.logs = []

            def _continue_chatgpt_registration_complete(self, _page):
                return False

            def _has_otp_input(self, page):
                return page.state == "otp"

            def _has_visible_password(self, page):
                return page.state == "password"

            def _fill_password_step(self, page):
                actions.append("password-submit")
                page.state = "post_password_otp"
                page.url = "https://auth.openai.com/email-verification"

            def _submit_email_code(self, _page, min_timestamp, **_kwargs):
                actions.append("otp-submit")
                submitted_timestamps.append(min_timestamp)

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        page = Page()
        self.assertTrue(
            configure_password_first_login(worker, enabled=True, required=True)
        )

        worker._submit_email_code(page, 100.0, wait_for_session=True)

        self.assertEqual(
            actions, ["password-choice", "password-submit", "otp-submit"]
        )
        self.assertTrue(worker._password_step_submitted)
        self.assertGreater(submitted_timestamps[0], 100.0)
        self.assertTrue(
            any("密码提交后发送的本轮新验证码" in line for line in worker.logs)
        )

    def test_password_submission_resends_code_before_mailbox_read(self):
        events = []
        submitted_timestamps = []

        class Candidate:
            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                events.append("resend")

        class Collection:
            def __init__(self, items=(), text=""):
                self.items = list(items)
                self.text = text

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

            def inner_text(self, **_kwargs):
                return self.text

        class Page:
            url = "https://auth.openai.com/email-verification"

            def locator(self, selector):
                if selector == "body":
                    return Collection(text="Check your inbox Resend email")
                if "Resend email" in selector:
                    return Collection([Candidate()])
                return Collection()

            def wait_for_timeout(self, _milliseconds):
                return None

        class Worker:
            def __init__(self):
                self.logs = []

            def _continue_chatgpt_registration_complete(self, _page):
                return False

            def _has_otp_input(self, _page):
                return True

            def _has_visible_password(self, _page):
                return False

            def _fill_password_step(self, _page):
                return None

            def _submit_email_code(self, _page, min_timestamp, **_kwargs):
                events.append("mailbox")
                submitted_timestamps.append(min_timestamp)

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        self.assertTrue(configure_password_first_login(worker, enabled=True))
        worker._password_step_submitted = True
        worker._hme_password_submitted_at_epoch = 1.0

        worker._submit_email_code(Page(), 0)

        self.assertEqual(events, ["resend", "mailbox"])
        self.assertGreater(submitted_timestamps[0], time.time() - 40)
        self.assertLess(submitted_timestamps[0], time.time() - 20)
        self.assertTrue(any("已单次点击重新发送" in line for line in worker.logs))

    def test_password_entry_waits_through_security_verification_past_15_seconds(self):
        actions = []

        class Candidate:
            def __init__(self, page):
                self.page = page

            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                actions.append("password")
                self.page.state = "security"
                self.page.url = "https://auth.openai.com/security-check/challenge"

        class Body:
            def __init__(self, page):
                self.page = page

            def inner_text(self, **_kwargs):
                if self.page.state == "security":
                    return "Security verification - verify you are human"
                return ""

        class Collection:
            def __init__(self, items):
                self.items = items

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

            def inner_text(self, **_kwargs):
                return self.items[0].inner_text(**_kwargs)

        class Page:
            def __init__(self):
                self.state = "otp"
                self.url = "https://auth.openai.com/email-verification"
                self.wait_calls = 0

            def locator(self, selector):
                if selector == "body":
                    return Collection([Body(self)])
                if self.state == "otp" and "Continue with password" in selector:
                    return Collection([Candidate(self)])
                return Collection([])

            def evaluate(self, _script):
                return "complete"

            def wait_for_timeout(self, _milliseconds):
                self.wait_calls += 1
                if self.wait_calls >= 2:
                    self.state = "password"
                    self.url = "https://chatgpt.com/create-account/password"

        class Worker:
            headless = True

            def __init__(self):
                self.logs = []

            def _continue_chatgpt_registration_complete(self, _page):
                return False

            def _has_otp_input(self, page):
                return page.state == "otp"

            def _has_visible_password(self, page):
                return page.state == "password"

            def _fill_password_step(self, page):
                actions.append("password-submit")
                page.state = "post_password_otp"
                page.url = "https://auth.openai.com/email-verification"

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        page = Page()
        self.assertTrue(
            configure_password_first_login(worker, enabled=True, required=True)
        )
        self.assertTrue(worker._continue_chatgpt_registration_complete(page))
        self.assertFalse(worker._has_otp_input(page))

        self.assertEqual(actions, ["password", "password-submit"])
        self.assertFalse(worker._hme_password_entry_pending)
        self.assertTrue(worker._password_step_submitted)
        self.assertTrue(
            any(
                message.startswith("[AUTH_PASSWORD_ROUTE_TRANSITION]")
                for message in worker.logs
            )
        )
        self.assertTrue(any("Session 判定=暂缓" in message for message in worker.logs))
        self.assertTrue(any("期间不读取 Session" in message for message in worker.logs))

    def test_password_choice_waits_for_route_transition_before_otp_flow(self):
        actions = []

        class Candidate:
            def __init__(self, page):
                self.page = page

            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                actions.append("password-click")

        class Collection:
            def __init__(self, items=(), text=""):
                self.items = list(items)
                self.text = text

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

            def inner_text(self, **_kwargs):
                return self.text

        class Page:
            url = "https://auth.openai.com/email-verification"
            state = "otp"

            def locator(self, selector):
                if selector == "body":
                    return Collection(text="Email verification")
                if self.state == "otp" and "Continue with password" in selector:
                    return Collection([Candidate(self)])
                return Collection()

            def evaluate(self, _script):
                return "complete"

            def wait_for_timeout(self, _milliseconds):
                actions.append("wait")
                if self.state == "otp":
                    self.state = "password"
                    self.url = "https://chatgpt.com/create-account/password"

        class Worker:
            headless = False

            def __init__(self):
                self.logs = []
                self.otp_checks = 0

            def _continue_chatgpt_registration_complete(self, _page):
                return False

            def _has_otp_input(self, page):
                self.otp_checks += 1
                return page.state == "otp"

            def _has_visible_password(self, page):
                return page.state == "password"

            def _fill_password_step(self, page):
                actions.append("password-submit")
                page.state = "post_password_otp"
                page.url = "https://auth.openai.com/email-verification"

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        page = Page()
        self.assertTrue(
            configure_password_first_login(worker, enabled=True, required=True)
        )

        self.assertTrue(worker._continue_chatgpt_registration_complete(page))

        self.assertEqual(actions, ["password-click", "wait", "password-submit"])
        self.assertEqual(page.url, "https://auth.openai.com/email-verification")
        self.assertTrue(worker._password_step_submitted)
        self.assertFalse(worker._hme_password_entry_pending)
        self.assertTrue(
            any(
                line.startswith("[AUTH_PASSWORD_ROUTE_TRANSITION]")
                for line in worker.logs
            )
        )
        self.assertTrue(any("密码已提交并完成页面切换" in line for line in worker.logs))
        self.assertTrue(any("期间不读取 Session" in line for line in worker.logs))

    def test_new_gmail_rejects_existing_account_instead_of_resetting_password(self):
        class Worker:
            existing_login_only = False

            def __init__(self):
                self.logs = []

            def _has_otp_input(self, _page):
                return False

            def _fill_password_step(self, _page):
                return None

            def _has_password_auth_error(self, _page):
                return True

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        page = SimpleNamespace(url="https://auth.openai.com/log-in/password")
        self.assertTrue(
            configure_password_first_login(worker, enabled=True, required=True)
        )

        with self.assertRaisesRegex(RuntimeError, "已存在 OpenAI 账号"):
            worker._has_password_auth_error(page)
        self.assertTrue(
            any(
                line.startswith("[AUTH_EXISTING_ACCOUNT_REJECTED]")
                for line in worker.logs
            )
        )

        worker.existing_login_only = True
        self.assertTrue(worker._has_password_auth_error(page))

    def test_existing_account_reset_confirmation_clicks_continue_once(self):
        actions = []

        class Candidate:
            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                actions.append("continue")
                page.url = "https://auth.openai.com/email-verification"

        class Collection:
            def __init__(self, items=(), text=""):
                self.items = list(items)
                self.text = text

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

            def inner_text(self, **_kwargs):
                return self.text

        class Page:
            url = "https://auth.openai.com/reset-password"

            def locator(self, selector):
                if selector == "body":
                    return Collection(text="パスワードのリセット 続行")
                if "続行" in selector:
                    return Collection([Candidate()])
                return Collection()

        class Worker:
            headless = False

            def __init__(self):
                self.logs = []

            def _has_otp_input(self, _page):
                return False

            def _fill_password_step(self, _page):
                return None

            def log(self, message):
                self.logs.append(message)

        page = Page()
        worker = Worker()
        self.assertTrue(configure_password_first_login(worker, enabled=True))

        self.assertFalse(worker._has_otp_input(page))
        self.assertEqual(actions, ["continue"])
        self.assertTrue(
            any(
                line.startswith("[AUTH_PASSWORD_RESET_CONTINUE]")
                for line in worker.logs
            )
        )

    def test_email_code_page_without_password_option_uses_email_code(self):
        class Collection:
            def count(self):
                return 0

            def nth(self, _index):
                raise IndexError

        class Page:
            def locator(self, _selector):
                return Collection()

        class Worker:
            def __init__(self):
                self.logs = []

            def _has_otp_input(self, _page):
                return True

            def _fill_password_step(self, _page):
                raise AssertionError("password form was not reached")

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        self.assertTrue(configure_password_first_login(worker, enabled=True))
        self.assertTrue(worker._has_otp_input(Page()))
        self.assertIn("继续读取邮箱验证码", worker.logs[-1])

    def test_email_verification_route_is_not_treated_as_password_form(self):
        class Worker:
            def __init__(self):
                self.password_checks = 0

            def _has_visible_password(self, _page):
                self.password_checks += 1
                return True

        worker = Worker()
        self.assertTrue(configure_email_verification_priority(worker))

        verification_page = SimpleNamespace(
            url="https://auth.openai.com/email-verification"
        )
        self.assertFalse(worker._has_visible_password(verification_page))
        self.assertEqual(worker.password_checks, 0)

        reset_page = SimpleNamespace(url="https://auth.openai.com/reset-password")
        self.assertTrue(worker._has_visible_password(reset_page))
        self.assertEqual(worker.password_checks, 1)

    def test_chatgpt_home_otp_is_submitted_before_session_wait(self):
        class Worker:
            def __init__(self):
                self.events = []
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _has_visible_password(self, _page):
                return False

            def _has_otp_input(self, _page):
                return True

            def _submit_email_code(self, _page, min_timestamp, wait_for_session=True):
                self.events.append(("submit", wait_for_session, min_timestamp))

            def _wait_after_otp_submit(self, _page, timeout=45):
                self.events.append(("wait", timeout))
                return "session-ready"

        worker = Worker()
        self.assertTrue(configure_email_verification_priority(worker))
        before = time.time()

        result = worker._wait_after_otp_submit(
            SimpleNamespace(url="https://chatgpt.com/"),
            timeout=12,
        )

        self.assertEqual(result, "session-ready")
        self.assertEqual(worker.events[0][0:2], ("submit", False))
        self.assertGreaterEqual(worker.events[0][2], before - 10.1)
        self.assertEqual(worker.events[1], ("wait", 12))
        self.assertIn("先读取并提交本轮邮箱验证码", worker.logs[-1])

    def test_non_chatgpt_otp_wait_keeps_original_order(self):
        class Worker:
            def __init__(self):
                self.events = []

            def log(self, _message):
                return None

            def _has_visible_password(self, _page):
                return False

            def _has_otp_input(self, _page):
                return True

            def _submit_email_code(self, *_args, **_kwargs):
                self.events.append("submit")

            def _wait_after_otp_submit(self, _page, timeout=45):
                self.events.append(("wait", timeout))

        worker = Worker()
        self.assertTrue(configure_email_verification_priority(worker))

        worker._wait_after_otp_submit(
            SimpleNamespace(url="https://auth.openai.com/email-verification"),
            timeout=9,
        )

        self.assertEqual(worker.events, [("wait", 9)])

    def test_chatgpt_home_about_you_is_completed_before_session_success(self):
        class Worker:
            def __init__(self):
                self.events = []
                self.logs = []
                self.session_ready = False

            def log(self, message):
                self.logs.append(message)

            def _has_visible_password(self, _page):
                return False

            def _has_otp_input(self, _page):
                return False

            def _has_chatgpt_session(self, _page):
                return self.session_ready

            def _has_about_you_form(self, _page):
                return not self.session_ready

            def _fill_about_you(self, _page):
                self.events.append("fill-about-you")
                self.session_ready = True

            def _wait_after_otp_submit(self, _page, timeout=45):
                self.events.append(("wait", timeout))
                return "intermediate-page"

        worker = Worker()
        self.assertTrue(configure_email_verification_priority(worker))

        with patch(
            "hidemyemail_generator.openai_registration_flow._page_wait",
            return_value=None,
        ):
            result = worker._wait_after_otp_submit(
                SimpleNamespace(url="https://chatgpt.com/"),
                timeout=12,
            )

        self.assertEqual(result, "intermediate-page")
        self.assertEqual(worker.events, [("wait", 12), "fill-about-you"])
        self.assertTrue(
            any("确认 Access Token 后才会判定注册成功" in log for log in worker.logs)
        )

    def test_chatgpt_home_about_you_waits_again_for_real_session(self):
        class Worker:
            def __init__(self):
                self.events = []
                self.session_ready = False
                self.about_you_ready = True

            def log(self, _message):
                return None

            def _has_visible_password(self, _page):
                return False

            def _has_otp_input(self, _page):
                return False

            def _has_chatgpt_session(self, _page):
                return self.session_ready

            def _has_about_you_form(self, _page):
                return self.about_you_ready

            def _fill_about_you(self, _page):
                self.events.append("fill-about-you")
                self.about_you_ready = False

            def _wait_after_otp_submit(self, _page, timeout=45):
                self.events.append(("wait", timeout))
                if self.events.count(("wait", timeout)) == 2:
                    self.session_ready = True
                return "session-wait"

        worker = Worker()
        self.assertTrue(configure_email_verification_priority(worker))

        with patch(
            "hidemyemail_generator.openai_registration_flow._page_wait",
            return_value=None,
        ):
            result = worker._wait_after_otp_submit(
                SimpleNamespace(url="https://chatgpt.com/"),
                timeout=7,
            )

        self.assertEqual(result, "session-wait")
        self.assertEqual(
            worker.events,
            [("wait", 7), "fill-about-you", ("wait", 7)],
        )

    def test_chatgpt_home_waits_for_delayed_otp_control(self):
        class Page:
            url = "https://chatgpt.com/"

            def __init__(self):
                self.waits = []

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

        class Worker:
            def __init__(self):
                self.events = []
                self.otp_checks = 0

            def log(self, _message):
                return None

            def _has_visible_password(self, _page):
                return False

            def _has_otp_input(self, _page):
                self.otp_checks += 1
                return self.otp_checks >= 3

            def _has_chatgpt_session(self, _page):
                return False

            def _submit_email_code(self, *_args, **kwargs):
                self.events.append(("submit", kwargs.get("wait_for_session")))

            def _wait_after_otp_submit(self, _page, timeout=45):
                self.events.append(("wait", timeout))

        page = Page()
        worker = Worker()
        self.assertTrue(configure_email_verification_priority(worker))

        worker._wait_after_otp_submit(page, timeout=7)

        self.assertEqual(page.waits, [500, 500])
        self.assertEqual(worker.events, [("submit", False), ("wait", 7)])

    def test_required_password_first_never_falls_back_to_email_code(self):
        class Collection:
            def count(self):
                return 0

            def nth(self, _index):
                raise IndexError

        class Page:
            def locator(self, _selector):
                return Collection()

        class Worker:
            headless = False

            def __init__(self):
                self.continue_calls = 0
                self.logs = []

            def _continue_chatgpt_registration_complete(self, _page):
                self.continue_calls += 1
                return False

            def _has_otp_input(self, _page):
                return True

            def _fill_password_step(self, _page):
                raise AssertionError("password form was not reached")

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        self.assertTrue(
            configure_password_first_login(worker, enabled=True, required=True)
        )
        with self.assertRaisesRegex(RuntimeError, "必须先选择使用密码继续"):
            worker._continue_chatgpt_registration_complete(Page())
        self.assertEqual(worker.continue_calls, 0)

    def test_required_gmail_waits_for_password_choice_and_rejects_otp_only_page(self):
        class Collection:
            def count(self):
                return 0

            def nth(self, _index):
                raise IndexError

        class Page:
            url = "https://auth.openai.com/email-verification"

            def __init__(self):
                self.otp_ready = False

            def locator(self, _selector):
                return Collection()

            def wait_for_timeout(self, _milliseconds):
                self.otp_ready = True

        class Worker:
            headless = True

            def __init__(self):
                self.continue_calls = 0
                self.logs = []

            def _continue_chatgpt_registration_complete(self, _page):
                self.continue_calls += 1
                return False

            def _has_otp_input(self, page):
                return page.otp_ready

            def _fill_password_step(self, _page):
                raise AssertionError("password form was not reached")

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        page = Page()
        self.assertTrue(
            configure_password_first_login(
                worker,
                enabled=True,
                required=True,
                password_choice_timeout_seconds=30,
            )
        )

        with (
            patch(
                "hidemyemail_generator.openai_registration_flow.time.monotonic",
                side_effect=[0.0, 0.0, 0.25, 30.1],
            ),
            self.assertRaisesRegex(RuntimeError, "完整等待 30 秒"),
        ):
            worker._continue_chatgpt_registration_complete(page)

        self.assertEqual(worker.continue_calls, 0)
        self.assertTrue(page.otp_ready)
        self.assertTrue(any("期间不读取邮箱验证码" in line for line in worker.logs))

    def test_required_gmail_keeps_waiting_when_otp_appears_before_password_choice(self):
        actions = []

        class Candidate:
            def __init__(self, page):
                self.page = page

            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                actions.append("password")
                self.page.url = "https://auth.openai.com/sign-up/password"

        class Collection:
            def __init__(self, items=()):
                self.items = list(items)

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        class Page:
            def __init__(self):
                self.url = "https://auth.openai.com/email-verification"
                self.wait_calls = 0
                self.password_submits = 0

            def locator(self, selector):
                if self.wait_calls >= 2 and "Continue with password" in selector:
                    return Collection([Candidate(self)])
                return Collection()

            def wait_for_timeout(self, _milliseconds):
                self.wait_calls += 1

        class Worker:
            headless = True

            def __init__(self):
                self.continue_calls = 0
                self.logs = []
                self.otp_checks = 0

            def _continue_chatgpt_registration_complete(self, _page):
                self.continue_calls += 1
                return False

            def _has_otp_input(self, _page):
                self.otp_checks += 1
                return True

            def _has_visible_password(self, page):
                return "password" in page.url

            def _fill_password_step(self, page):
                page.password_submits += 1
                page.url = "https://auth.openai.com/email-verification"

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        page = Page()
        self.assertTrue(
            configure_password_first_login(worker, enabled=True, required=True)
        )

        self.assertTrue(worker._continue_chatgpt_registration_complete(page))

        self.assertEqual(actions, ["password"])
        self.assertEqual(page.wait_calls, 2)
        self.assertEqual(page.password_submits, 1)
        self.assertGreaterEqual(worker.otp_checks, 2)
        self.assertEqual(worker.continue_calls, 0)
        self.assertFalse(worker._hme_password_entry_pending)
        self.assertTrue(worker._password_step_submitted)
        self.assertTrue(any("期间不读取邮箱验证码" in line for line in worker.logs))

    def test_submitted_password_marker_allows_follow_up_email_code(self):
        class Collection:
            def count(self):
                return 0

            def nth(self, _index):
                raise IndexError

        class Page:
            def locator(self, _selector):
                return Collection()

        class Worker:
            headless = False

            def __init__(self):
                self.continue_calls = 0
                self.logs = []

            def _continue_chatgpt_registration_complete(self, _page):
                self.continue_calls += 1
                return True

            def _has_otp_input(self, _page):
                return True

            def _fill_password_step(self, _page):
                return None

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        self.assertTrue(
            configure_password_first_login(worker, enabled=True, required=True)
        )
        # The upstream worker may rebuild its auth-page state after password
        # submission.  The durable submission marker must still authorize the
        # follow-up Gmail verification-code page.
        worker._hme_password_entry_selected = False
        worker._hme_password_entry_pending = False
        worker._password_step_submitted = True

        self.assertTrue(worker._continue_chatgpt_registration_complete(Page()))
        self.assertTrue(worker._has_otp_input(Page()))
        self.assertEqual(worker.continue_calls, 1)

    def test_registration_profile_name_is_captured_for_account_menu(self):
        backend = SimpleNamespace(
            random_profile=lambda: ("Mia Brown", "1997-09-18")
        )
        worker = SimpleNamespace()

        self.assertTrue(configure_registration_profile_capture(backend, worker))
        self.assertEqual(
            backend.random_profile(), ("Mia Brown", "1997-09-18")
        )
        self.assertEqual(worker.registration_profile_name, "Mia Brown")

    def test_japanese_password_row_add_action_is_selected(self):
        actions = []

        class Candidate:
            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                actions.append("password_add")

        class Collection:
            def __init__(self, candidates):
                self.candidates = candidates

            def count(self):
                return len(self.candidates)

            def nth(self, index):
                return self.candidates[index]

        class Page:
            def locator(self, selector):
                if (
                    selector.startswith("xpath=")
                    and "normalize-space(.)='パスワード'" in selector
                ):
                    return Collection([Candidate()])
                return Collection([])

        self.assertTrue(_click_first_visible(Page(), ADD_PASSWORD_SELECTORS))
        self.assertEqual(actions, ["password_add"])

    def test_custom_japanese_password_row_is_clicked_through_dom(self):
        calls = []

        class Candidate:
            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                calls.append("trusted_click")

        class Page:
            def locator(self, selector):
                if selector == '[data-hme-password-action="add"]':
                    return SimpleNamespace(
                        count=lambda: 1,
                        nth=lambda _index: Candidate(),
                    )
                return SimpleNamespace(count=lambda: 0)

            def evaluate(self, script, options):
                self.assert_script = script
                calls.append(options)
                return {"state": "add", "marked": options["markAdd"]}

        page = Page()
        self.assertTrue(_click_add_password(page))
        self.assertIn("パスワード", page.assert_script)
        self.assertEqual(calls, [{"markAdd": True}, "trusted_click"])

    def test_recorded_profile_name_is_clicked_through_dom(self):
        calls = []

        class Page:
            def evaluate(self, script, name):
                calls.append((script, name))
                return True

        self.assertTrue(_click_profile_name_by_dom(Page(), "Noah Allen"))
        self.assertEqual(calls[0][1], "Noah Allen")
        self.assertIn("aria-haspopup", calls[0][0])

    def test_japanese_profile_menu_matches_current_accessible_label(self):
        self.assertIn(
            'button[aria-label*="プロファイルメニューを開く" i]',
            PROFILE_MENU_STRICT_SELECTORS,
        )

    def test_current_profile_selector_runs_before_dom_fallback(self):
        profile_candidate = object()
        settings_candidate = object()
        target_selector = (
            'button[aria-label*="プロファイルメニューを開く" i]'
        )
        worker = SimpleNamespace(
            registration_profile_name="Ava Brown",
            log=lambda _message: None,
        )

        def visible_candidates(_page, selector, **_kwargs):
            return [profile_candidate] if selector == target_selector else []

        with (
            patch(
                "hidemyemail_generator.openai_browser_bridge._visible_locators",
                side_effect=visible_candidates,
            ),
            patch(
                "hidemyemail_generator.openai_browser_bridge._first_visible",
                return_value=settings_candidate,
            ),
            patch(
                "hidemyemail_generator.openai_browser_bridge._click_locator",
                return_value=True,
            ),
            patch(
                "hidemyemail_generator.openai_browser_bridge._click_profile_name_by_dom"
            ) as dom_fallback,
        ):
            self.assertTrue(_open_settings_from_profile(object(), worker))

        dom_fallback.assert_not_called()

    def test_password_add_geometry_chooses_topmost_add_action(self):
        clicked = []

        class Page:
            mouse = SimpleNamespace(
                click=lambda x, y: clicked.append((x, y))
            )

            def evaluate(self, script):
                self.script = script
                return {"x": 1145.0, "y": 183.0, "top": 170.0, "right": 1190.0}

        page = Page()
        self.assertTrue(_click_password_add_by_geometry(page))
        self.assertIn("passwordLabel", page.script)
        self.assertEqual(clicked, [(1145.0, 183.0)])

    def test_registration_retries_firefox_aborted_navigation(self):
        calls = []
        logs = []

        class Page:
            def goto(self, url, **_kwargs):
                calls.append(url)
                if len(calls) == 1:
                    raise RuntimeError("Page.goto: NS_BINDING_ABORTED")
                return "response"

        class Worker:
            def __init__(self):
                self.log = logs.append

            def _register(self, page, _context):
                return page.goto(
                    "https://auth.openai.com/api/accounts/authorize?secret=value"
                )

        worker = Worker()
        page = Page()

        self.assertTrue(configure_resilient_registration_navigation(worker))
        self.assertEqual(worker._register(page, object()), "response")
        self.assertEqual(len(calls), 2)
        self.assertIn("自动重定向打断", logs[0])
        self.assertNotIn("secret=value", logs[0])

    def test_registration_does_not_retry_real_navigation_error(self):
        calls = []

        class Page:
            def goto(self, url, **_kwargs):
                calls.append(url)
                raise RuntimeError("Page.goto: net::ERR_PROXY_CONNECTION_FAILED")

        class Worker:
            log = staticmethod(lambda _message: None)

            def _register(self, page, _context):
                return page.goto("https://auth.openai.com/")

        worker = Worker()
        configure_resilient_registration_navigation(worker)

        with self.assertRaisesRegex(RuntimeError, "ERR_PROXY_CONNECTION_FAILED"):
            worker._register(Page(), object())
        self.assertEqual(len(calls), 1)

    def test_session_is_saved_before_password_setup(self):
        events = []

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(email="new@icloud.com")
                self._password_step_submitted = False

            def _extract_session_info(self, context):
                events.append(("session", context))
                return {"access_token": "at-test"}

        worker = Worker()

        def add_password(target_worker, context, access_token, password):
            self.assertEqual(access_token, "at-test")
            self.assertEqual(password, "Strong!Password123")
            events.append(("password", context))
            target_worker._password_step_submitted = True
            return True

        def extract_session(_worker, context):
            events.append(("session_api", context))
            return {"access_token": "at-test"}

        with (
            patch(
                "hidemyemail_generator.openai_browser_bridge."
                "add_password_via_account_api",
                side_effect=add_password,
            ),
            patch(
                "hidemyemail_generator.openai_browser_bridge."
                "extract_session_without_navigation",
                side_effect=extract_session,
            ),
            patch("hidemyemail_generator.openai_browser_bridge.emit"),
        ):
            configured = configure_post_registration_password_setup(
                SimpleNamespace(),
                worker,
                "Strong!Password123",
                enabled=True,
            )
            result = worker._extract_session_info("browser-context")

        self.assertTrue(configured)
        self.assertEqual(result, {"access_token": "at-test"})
        self.assertEqual(
            events,
            [
                ("session_api", "browser-context"),
                ("password", "browser-context"),
                ("session_api", "browser-context"),
            ],
        )

    def test_password_setup_failure_keeps_registered_session(self):
        logs = []
        emitted = []

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(
                    email="new@icloud.com", password="Generated!Password123"
                )
                self._password_step_submitted = False
                self.require_password_setup = True

            def _extract_session_info(self, _context):
                return {}

            def log(self, message):
                logs.append(message)

        worker = Worker()
        with (
            patch(
                "hidemyemail_generator.openai_browser_bridge."
                "add_password_via_account_api",
                side_effect=RuntimeError("settings unavailable"),
            ),
            patch(
                "hidemyemail_generator.openai_browser_bridge."
                "extract_session_without_navigation",
                return_value={"access_token": "at-registered", "session_json": "{}"},
            ),
            patch(
                "hidemyemail_generator.openai_browser_bridge.emit",
                side_effect=lambda kind, **payload: emitted.append((kind, payload)),
            ),
            patch(
                "hidemyemail_generator.openai_browser_bridge.enable_totp_mfa"
            ) as enable_mfa,
        ):
            configure_post_registration_password_setup(
                SimpleNamespace(),
                worker,
                "Generated!Password123",
                enabled=True,
                enable_2fa=True,
            )
            result = worker._extract_session_info("browser-context")

        self.assertEqual(result["access_token"], "at-registered")
        self.assertFalse(worker.require_password_setup)
        self.assertFalse(worker._password_step_submitted)
        self.assertTrue(any(kind == "account_registered" for kind, _ in emitted))
        self.assertTrue(any("Session 已保存" in message for message in logs))
        self.assertTrue(any("已跳过开启 2FA" in message for message in logs))
        self.assertNotIn("two_factor_start", [kind for kind, _ in emitted])
        enable_mfa.assert_not_called()

    def test_two_factor_refreshes_invalidated_token_before_browser_closes(self):
        class Page:
            url = "https://chatgpt.com/"

            def __init__(self):
                self.goto_calls = []

            def goto(self, url, **_kwargs):
                self.url = url
                self.goto_calls.append(url)

            def wait_for_timeout(self, _milliseconds):
                return None

            def bring_to_front(self):
                return None

            def is_closed(self):
                return False

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(
                    email="new@icloud.com",
                    password="Strong!Password123",
                )
                self._password_step_submitted = False
                self.logs = []
                self.reauthentication_calls = []

            def _extract_session_info(self, _context):
                self.fail("the original extractor must stay wrapped")

            def log(self, message):
                self.logs.append(message)

            def _register(self, page, received_context, *, existing_login_only=False):
                self.reauthentication_calls.append(
                    (page, received_context, existing_login_only)
                )

        page = Page()
        context = SimpleNamespace(
            pages=[page],
            clear_cookies=lambda: setattr(context, "cookies_cleared", True),
            cookies_cleared=False,
        )
        worker = Worker()
        emitted = []
        session_results = [
            {"access_token": "at-registered"},
            {"access_token": "at-before-password"},
            {"access_token": "at-after-password"},
        ]
        mfa_tokens = []
        mfa_client = SimpleNamespace(close=lambda: None)

        def add_password(target_worker, context, access_token, password):
            self.assertIsNotNone(context)
            self.assertEqual(access_token, "at-registered")
            self.assertEqual(password, "Strong!Password123")
            target_worker._password_step_submitted = True
            return True

        def enable_mfa(
            _client,
            *,
            access_token,
            email,
            pending,
            on_enrolled,
        ):
            self.assertEqual(email, "new@icloud.com")
            self.assertEqual(pending, {})
            mfa_tokens.append(access_token)
            if len(mfa_tokens) == 1:
                raise MfaSetupError(
                    "HTTP 401: Your authentication token has been invalidated"
                )
            state = {"secret": "ABCDEFGHIJKLMNOP", "enabled": True}
            on_enrolled(state)
            return state

        with (
            patch(
                "hidemyemail_generator.openai_browser_bridge."
                "add_password_via_account_api",
                side_effect=add_password,
            ),
            patch(
                "hidemyemail_generator.openai_browser_bridge."
                "extract_session_without_navigation",
                side_effect=session_results,
            ),
            patch(
                "hidemyemail_generator.openai_browser_bridge.enable_totp_mfa",
                side_effect=enable_mfa,
            ),
            patch(
                "hidemyemail_generator.openai_browser_bridge.MfaHttpClient",
                return_value=mfa_client,
            ),
            patch(
                "hidemyemail_generator.openai_browser_bridge.emit",
                side_effect=lambda kind, **payload: emitted.append((kind, payload)),
            ),
        ):
            configure_post_registration_password_setup(
                SimpleNamespace(),
                worker,
                "Strong!Password123",
                enabled=True,
                enable_2fa=True,
                pending_two_factor={},
            )
            result = worker._extract_session_info(context)

        self.assertEqual(
            mfa_tokens,
            ["at-before-password", "at-after-password"],
        )
        self.assertEqual(result["access_token"], "at-after-password")
        self.assertTrue(result["two_factor"]["enabled"])
        self.assertTrue(context.cookies_cleared)
        self.assertEqual(
            worker.reauthentication_calls,
            [(page, context, True)],
        )
        self.assertTrue(any("完整重新登录" in message for message in worker.logs))
        self.assertTrue(worker._hme_two_factor_completed)
        self.assertIn("two_factor_start", [kind for kind, _payload in emitted])
        self.assertIn("two_factor_enabled", [kind for kind, _payload in emitted])

    def test_existing_account_two_factor_reauthenticates_before_playwright_stops(self):
        class Page:
            url = "https://chatgpt.com/"

            def bring_to_front(self):
                return None

            def is_closed(self):
                return False

        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(
                    email="existing@gmail.com",
                    password="Strong!Password123",
                )
                self.logs = []
                self.reauthentication_calls = []

            def _extract_session_info(self, _context):
                self.fail("the closed-context extractor must be replaced")

            def _register(self, page, context, *, existing_login_only=False):
                self.reauthentication_calls.append(
                    (page, context, existing_login_only)
                )

            def log(self, message):
                self.logs.append(message)

        page = Page()
        context = SimpleNamespace(
            pages=[page],
            clear_cookies=lambda: setattr(context, "cookies_cleared", True),
            cookies_cleared=False,
        )
        worker = Worker()
        emitted = []
        session_results = [
            {"access_token": "at-cookie"},
            {"access_token": "at-recent"},
        ]
        mfa_tokens = []
        mfa_client = SimpleNamespace(close=lambda: None)

        def enable_mfa(
            _client,
            *,
            access_token,
            email,
            pending,
            on_enrolled,
        ):
            self.assertEqual(email, "existing@gmail.com")
            self.assertEqual(pending, {})
            mfa_tokens.append(access_token)
            if len(mfa_tokens) == 1:
                raise MfaSetupError(
                    "HTTP 401: recent_auth_required; user must re-authenticate"
                )
            state = {"secret": "ABCDEFGHIJKLMNOP", "enabled": True}
            on_enrolled(state)
            return state

        with (
            patch(
                "hidemyemail_generator.openai_browser_bridge."
                "extract_session_without_navigation",
                side_effect=session_results,
            ),
            patch(
                "hidemyemail_generator.openai_browser_bridge.enable_totp_mfa",
                side_effect=enable_mfa,
            ),
            patch(
                "hidemyemail_generator.openai_browser_bridge.MfaHttpClient",
                return_value=mfa_client,
            ),
            patch(
                "hidemyemail_generator.openai_browser_bridge.emit",
                side_effect=lambda kind, **payload: emitted.append((kind, payload)),
            ),
        ):
            self.assertTrue(
                configure_existing_account_two_factor(
                    worker,
                    enabled=True,
                    pending_two_factor={},
                )
            )
            result = worker._extract_session_info(context)

        self.assertEqual(mfa_tokens, ["at-cookie", "at-recent"])
        self.assertEqual(result["access_token"], "at-recent")
        self.assertTrue(result["two_factor"]["enabled"])
        self.assertTrue(context.cookies_cleared)
        self.assertEqual(
            worker.reauthentication_calls,
            [(page, context, True)],
        )
        self.assertTrue(worker._hme_two_factor_completed)
        self.assertIn("two_factor_enabled", [kind for kind, _payload in emitted])

    def test_existing_login_discards_stale_pending_totp_before_activation(self):
        class Worker:
            def __init__(self):
                self.account = SimpleNamespace(
                    email="retry@icloud.com",
                    password="Strong!Password123",
                )
                self._hme_login_totp_submitted = True
                self.logs = []

            def _extract_session_info(self, _context):
                self.fail("the original extractor must stay wrapped")

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        emitted = []
        received_pending = []
        stale = {
            "enabled": False,
            "status": "enrolled",
            "secret": "JBSWY3DPEHPK3PXP",
            "factor_id": "stale-factor",
            "session_id": "stale-session",
        }

        def enable_mfa(
            _client,
            *,
            access_token,
            email,
            pending,
            on_enrolled,
        ):
            self.assertEqual(access_token, "at-current")
            self.assertEqual(email, "retry@icloud.com")
            received_pending.append(pending)
            state = {
                "enabled": True,
                "status": "enabled",
                "secret": "ABCDEFGHIJKLMNOP",
            }
            on_enrolled(state)
            return state

        self.assertTrue(
            configure_existing_account_two_factor(
                worker,
                enabled=True,
                pending_two_factor=stale,
            )
        )
        with (
            patch(
                "hidemyemail_generator.openai_account_security."
                "extract_session_without_navigation",
                return_value={"access_token": "at-current"},
            ),
            patch(
                "hidemyemail_generator.openai_account_security.MfaHttpClient",
                return_value=SimpleNamespace(close=lambda: None),
            ),
            patch(
                "hidemyemail_generator.openai_account_security.enable_totp_mfa",
                side_effect=enable_mfa,
            ),
            patch(
                "hidemyemail_generator.openai_account_security.emit",
                side_effect=lambda kind, **payload: emitted.append((kind, payload)),
            ),
        ):
            # The wrapper's dependencies were bound when it was configured, so
            # exercise the helper directly with the same current-session result.
            result = _enable_two_factor_before_browser_closes(
                worker,
                SimpleNamespace(),
                {"access_token": "at-current"},
                stale,
                password_confirmed=True,
                emit_event=lambda kind, **payload: emitted.append((kind, payload)),
                mfa_client_factory=lambda: SimpleNamespace(close=lambda: None),
                enable_mfa=enable_mfa,
            )

        self.assertEqual(received_pending, [{}])
        self.assertTrue(result["two_factor"]["enabled"])
        self.assertTrue(worker._hme_two_factor_completed)
        self.assertTrue(any("旧待激活登记已失效" in line for line in worker.logs))
        self.assertIn("two_factor_enabled", [kind for kind, _payload in emitted])

    def test_session_is_read_without_opening_a_new_page(self):
        class Response:
            ok = True
            status = 200

            def json(self):
                return {
                    "accessToken": "at-test",
                    "user": {"email": "new@icloud.com"},
                }

        class Request:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        request = Request()
        context = SimpleNamespace(request=request)
        logs = []
        worker = SimpleNamespace(
            account=SimpleNamespace(email="new@icloud.com"),
            skip_storage_state_capture=True,
            _chatgpt_session_email=lambda session: session["user"]["email"],
            log=logs.append,
        )

        result = extract_session_without_navigation(worker, context)

        self.assertEqual(result["access_token"], "at-test")
        self.assertNotIn("storage_state_json", result)
        self.assertEqual(len(request.calls), 1)
        self.assertIn("/api/auth/session", request.calls[0][0])
        self.assertIn("后台获取 Session", logs[-1])

    def test_registration_session_saves_browser_cookies_without_full_snapshot(self):
        class Response:
            ok = True
            status = 200

            def json(self):
                return {
                    "accessToken": "at-cookie-test",
                    "user": {"email": "manual@qq.com"},
                }

        context = SimpleNamespace(
            request=SimpleNamespace(get=lambda *_args, **_kwargs: Response()),
            cookies=lambda: [
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": "cookie-value",
                    "domain": "chatgpt.com",
                    "path": "/",
                }
            ],
        )
        worker = SimpleNamespace(
            account=SimpleNamespace(email="manual@qq.com"),
            skip_storage_state_capture=True,
            _chatgpt_session_email=lambda session: session["user"]["email"],
            log=lambda _message: None,
        )

        result = extract_session_without_navigation(worker, context)

        self.assertEqual(json.loads(result["cookies_json"])[0]["value"], "cookie-value")
        self.assertEqual(
            json.loads(result["storage_state_json"])["cookies"][0]["name"],
            "__Secure-next-auth.session-token",
        )

    def test_account_record_persists_registration_cookies(self):
        cookies = [
            {
                "name": "session",
                "value": "saved-cookie",
                "domain": "chatgpt.com",
                "path": "/",
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            _save_account_record(
                db_file,
                "manual@qq.com",
                result={
                    "access_token": "at-cookie-test",
                    "cookies_json": json.dumps(cookies),
                    "storage_state_json": json.dumps(
                        {"cookies": cookies, "origins": []}
                    ),
                },
            )

            record = load_account_record(db_file, "manual@qq.com")

        self.assertEqual(record["cookies"][0]["value"], "saved-cookie")
        self.assertEqual(json.loads(record["cookies_json"]), cookies)
        self.assertEqual(account_saved_cookies(record), cookies)

    def test_session_token_is_persisted_as_cookie_without_browser_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            _save_account_record(
                db_file,
                "gmail-cookie@gmail.com",
                result={
                    "access_token": "at-session-cookie",
                    "session_json": json.dumps(
                        {
                            "accessToken": "at-session-cookie",
                            "sessionToken": "saved-session-cookie",
                            "user": {"email": "gmail-cookie@gmail.com"},
                        }
                    ),
                },
            )

            record = load_account_record(db_file, "gmail-cookie@gmail.com")
            cookies = account_saved_cookies(record)
            storage_state = load_saved_storage_state(
                str(db_file), "gmail-cookie@gmail.com"
            )

        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["name"], "__Secure-next-auth.session-token")
        self.assertEqual(cookies[0]["value"], "saved-session-cookie")
        self.assertTrue(cookies[0]["httpOnly"])
        self.assertTrue(cookies[0]["secure"])
        self.assertEqual(json.loads(record["cookies_json"]), cookies)
        self.assertEqual(
            json.loads(record["storage_state_json"])["cookies"],
            cookies,
        )
        self.assertEqual(storage_state["cookies"], cookies)

    def test_saved_storage_state_falls_back_to_legacy_cookie_fields(self):
        cookies = [
            {
                "name": "__Secure-next-auth.session-token",
                "value": "legacy-cookie",
                "domain": "chatgpt.com",
                "path": "/",
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            conn = connect_db(str(db_file))
            try:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    (
                        "gpt_account:legacy-cookie@icloud.com",
                        json.dumps({"cookies_json": json.dumps(cookies)}),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            state = load_saved_storage_state(
                str(db_file), "legacy-cookie@icloud.com"
            )

        self.assertEqual(state, {"cookies": cookies, "origins": []})

    def test_camoufox_bridge_uses_randomized_non_fullscreen_window(self):
        calls = []

        def original(playwright, *args, **kwargs):
            calls.append((playwright, args, kwargs))
            return "browser"

        backend = SimpleNamespace(CamoufoxNewBrowser=original)

        self.assertTrue(configure_windowed_camoufox(backend))
        self.assertTrue(configure_windowed_camoufox(backend))
        self.assertEqual(backend.CamoufoxNewBrowser("playwright"), "browser")
        width, height = calls[0][2]["window"]
        self.assertGreaterEqual(width, 960)
        self.assertLessEqual(width, 1500)
        self.assertGreaterEqual(height, 600)
        self.assertLessEqual(height, 960)
        self.assertTrue(calls[0][2]["enable_cache"])
        self.assertTrue(
            calls[0][2]["firefox_user_prefs"]["browser.cache.memory.enable"]
        )
        self.assertTrue(
            calls[0][2]["firefox_user_prefs"]["network.http.use-cache"]
        )
        self.assertTrue(
            calls[0][2]["firefox_user_prefs"][
                "dom.storageManager.prompt.testing"
            ]
        )
        self.assertTrue(
            calls[0][2]["firefox_user_prefs"][
                "dom.storageManager.prompt.testing.allow"
            ]
        )
        self.assertFalse(
            calls[0][2]["firefox_user_prefs"][
                "widget.windows.window_occlusion_tracking.enabled"
            ]
        )
        self.assertFalse(
            calls[0][2]["firefox_user_prefs"][
                "dom.timeout.enable_budget_timer_throttling"
            ]
        )

        foreground_backend = SimpleNamespace(CamoufoxNewBrowser=original)
        with (
            patch.dict(
                os.environ,
                {"HME_BROWSER_FOREGROUND_REQUIRED": "1"},
            ),
            patch(
                "hidemyemail_generator.browser_platform.move_camoufox_window"
            ) as move_window,
        ):
            self.assertTrue(configure_windowed_camoufox(foreground_backend))
            foreground_backend.CamoufoxNewBrowser("playwright", headless=False)
            for _ in range(20):
                if move_window.called:
                    break
                time.sleep(0.01)
            move_window.assert_called_once()
            self.assertFalse(move_window.call_args.kwargs["apply_layout"])

        concurrent_backend = SimpleNamespace(CamoufoxNewBrowser=original)
        with (
            patch.dict(
                os.environ,
                {"HME_BROWSER_WINDOW_SLOT": "0", "HME_BROWSER_WINDOW_SLOTS": "2"},
            ),
            patch(
                "hidemyemail_generator.browser_platform.move_camoufox_window"
            ) as move_window,
        ):
            self.assertTrue(configure_windowed_camoufox(concurrent_backend))
            concurrent_backend.CamoufoxNewBrowser("playwright", headless=False)
            for _ in range(20):
                if move_window.called:
                    break
                time.sleep(0.01)
            move_window.assert_called_once()
            self.assertTrue(move_window.call_args.kwargs["apply_layout"])

        backend.CamoufoxNewBrowser(
            "playwright",
            window=(1024, 700),
            firefox_user_prefs={"browser.cache.disk.enable": False},
        )
        self.assertEqual(calls[-1][2]["window"], (1024, 700))
        self.assertFalse(
            calls[-1][2]["firefox_user_prefs"]["browser.cache.disk.enable"]
        )

        proxy_calls = []

        def proxy_original(playwright, *args, **kwargs):
            proxy_calls.append((playwright, args, kwargs))
            return "browser"

        proxy_backend = SimpleNamespace(CamoufoxNewBrowser=proxy_original)
        with patch.dict(
            os.environ,
            {"HME_REGISTRATION_PROXY_URL": "http://proxy.example:8080"},
        ):
            self.assertTrue(configure_windowed_camoufox(proxy_backend))
            proxy_backend.CamoufoxNewBrowser(
                "playwright",
                enable_cache=False,
                firefox_user_prefs={"browser.cache.memory.enable": False},
            )
        self.assertFalse(proxy_calls[0][2]["enable_cache"])
        self.assertFalse(
            proxy_calls[0][2]["firefox_user_prefs"][
                "browser.cache.memory.enable"
            ]
        )

    def test_direct_registration_uses_detected_exit_locale(self):
        calls = []

        class Worker:
            def __init__(self):
                self.logs = []

            def _new_browser_context(
                self, playwright, proxy, storage_state=None, **kwargs
            ):
                calls.append((playwright, proxy, storage_state, kwargs))
                return "browser", "context"

            def _fill_password_step(self, _page):
                calls.append("password")

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        self.assertTrue(
            configure_direct_registration_browser(
                worker, enabled=True, locale="ja-JP"
            )
        )
        worker._new_browser_context("playwright", SimpleNamespace(), None)

        self.assertEqual(calls[0][3]["locale_override"], "ja-JP")
        self.assertNotIn("proxy", calls[0][3])

    def test_direct_registration_location_uses_real_exit_country(self):
        backend = SimpleNamespace(
            detect_proxy_health=lambda *_args, **_kwargs: SimpleNamespace(
                country="JP", timezone="Asia/Tokyo"
            ),
            country_browser_locale=lambda country: {"JP": "ja-JP"}[country],
        )

        location = detect_direct_registration_location(backend, lambda _line: None)

        self.assertEqual(
            location,
            {"country": "JP", "locale": "ja-JP", "timezone": "Asia/Tokyo"},
        )

    def test_three_browser_windows_use_distinct_screen_slots(self):
        layouts = [
            _camoufox_window_layout(
                index,
                3,
                screen_size=(3200, 1800),
                randomizer=lambda lower, upper: (lower + upper) // 2,
            )
            for index in range(3)
        ]

        self.assertEqual([item["slot"] for item in layouts], [0, 1, 2])
        self.assertEqual(len({item["x"] for item in layouts}), 3)
        self.assertTrue(all(815 <= item["width"] <= 1046 for item in layouts))

    def test_browser_window_size_changes_with_each_random_choice(self):
        smallest = _camoufox_window_layout(
            0,
            1,
            screen_size=(2560, 1440),
            randomizer=lambda lower, _upper: lower,
        )
        largest = _camoufox_window_layout(
            0,
            1,
            screen_size=(2560, 1440),
            randomizer=lambda _lower, upper: upper,
        )

        self.assertNotEqual(
            (smallest["width"], smallest["height"]),
            (largest["width"], largest["height"]),
        )
        self.assertGreaterEqual(smallest["width"], 960)
        self.assertLessEqual(largest["width"], 1500)
        self.assertGreaterEqual(smallest["height"], 600)
        self.assertLessEqual(largest["height"], 960)

    def test_direct_registration_reloads_unstyled_password_page(self):
        class Page:
            def __init__(self):
                self.checks = 0
                self.reloads = 0

            def evaluate(self, _script):
                self.checks += 1
                return {
                    "isAuthPage": True,
                    "styleSheetCount": 1 if self.reloads else 0,
                    "loadedStyleLinkCount": 1 if self.reloads else 0,
                }

            def wait_for_timeout(self, _milliseconds):
                return None

            def reload(self, **_kwargs):
                self.reloads += 1

        class Worker:
            def __init__(self):
                self.logs = []
                self.password_fills = 0

            def _new_browser_context(self, *_args, **_kwargs):
                return "browser", "context"

            def _fill_password_step(self, _page):
                self.password_fills += 1

            def log(self, message):
                self.logs.append(message)

        page = Page()
        worker = Worker()
        self.assertTrue(configure_direct_registration_browser(worker, enabled=True))
        worker._fill_password_step(page)

        self.assertEqual(page.reloads, 1)
        self.assertEqual(worker.password_fills, 1)
        self.assertTrue(any("本机 IP 直连" in line for line in worker.logs))

    def test_password_is_added_from_main_settings_security_flow(self):
        class Candidate:
            def __init__(self, page, kind, index=0):
                self.page = page
                self.kind = kind
                self.index = index

            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                self.page.actions.append(self.kind)
                transitions = {
                    "profile": "menu",
                    "settings": "settings",
                    "security": "security",
                    "add_password": "password_form",
                    "submit_password": "password_set",
                }
                self.page.state = transitions.get(self.kind, self.page.state)

            def fill(self, value, **_kwargs):
                self.page.password_values[self.index] = value

            def input_value(self, **_kwargs):
                return self.page.password_values[self.index]

        class LocatorCollection:
            def __init__(self, candidates):
                self.candidates = candidates

            def count(self):
                return len(self.candidates)

            def nth(self, index):
                return self.candidates[index]

        class Page:
            def __init__(self):
                self.state = "new"
                self.url = ""
                self.actions = []
                self.password_values = ["", ""]

            def goto(self, url, **_kwargs):
                self.url = url
                self.state = "home" if url == "https://chatgpt.com/" else "security"

            def wait_for_timeout(self, _milliseconds):
                return None

            def locator(self, selector):
                candidates = []
                if self.state == "home" and selector == '[data-testid="profile-button"]':
                    candidates = [Candidate(self, "profile")]
                elif self.state == "menu" and selector == '[data-testid="settings-menu-item"]':
                    candidates = [Candidate(self, "settings")]
                elif self.state == "settings" and selector == '[data-testid="security-tab"]':
                    candidates = [Candidate(self, "security")]
                elif self.state == "security" and selector == '[data-testid="add-password-button"]':
                    candidates = [Candidate(self, "add_password")]
                elif self.state == "password_form" and selector == 'input[type="password"]':
                    candidates = [
                        Candidate(self, "password_input", 0),
                        Candidate(self, "password_input", 1),
                    ]
                elif self.state == "password_form" and selector.startswith(
                    'button[type="submit"]'
                ):
                    candidates = [Candidate(self, "submit_password")]
                elif self.state == "password_set" and selector == (
                    'button:has-text("Change password")'
                ):
                    candidates = [Candidate(self, "password_present")]
                return LocatorCollection(candidates)

        page = Page()
        context = SimpleNamespace(
            pages=[page],
            new_page=lambda: self.fail("password setup must reuse the registration page"),
        )
        backend = SimpleNamespace(
            KEPT_REGISTER_BROWSER_SESSIONS={
                "new@icloud.com": (context, object(), "")
            }
        )
        worker = SimpleNamespace(
            account=SimpleNamespace(email="new@icloud.com"),
            log=lambda message: None,
        )

        confirmed = ensure_password_in_security_settings(
            backend,
            worker,
            "Strong!Password123",
        )

        self.assertTrue(confirmed)
        self.assertTrue(worker._password_step_submitted)
        self.assertEqual(
            page.actions[:4],
            ["profile", "settings", "security", "add_password"],
        )
        self.assertIn("submit_password", page.actions)
        self.assertEqual(page.actions.count("profile"), 1)
        self.assertEqual(
            page.password_values,
            ["Strong!Password123", "Strong!Password123"],
        )

    def test_password_login_page_uses_forgot_password_before_setting_password(self):
        class Candidate:
            def __init__(self, page, kind, index=0):
                self.page = page
                self.kind = kind
                self.index = index

            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                self.page.actions.append(self.kind)
                transitions = {
                    "profile": "menu",
                    "settings": "settings",
                    "security": "security",
                    "add_password": "auth_password",
                    "forgot_password": "otp",
                    "submit_otp": "reset_form",
                    "submit_password": "password_success",
                }
                self.page.state = transitions.get(self.kind, self.page.state)

            def fill(self, value, **_kwargs):
                if self.kind == "otp_input":
                    self.page.otp_value = value
                else:
                    self.page.filled_password_kinds.append(self.kind)
                    self.page.password_values[self.index] = value

            def input_value(self, **_kwargs):
                return self.page.password_values[self.index]

        class Collection:
            def __init__(self, candidates):
                self.candidates = candidates

            def count(self):
                return len(self.candidates)

            def nth(self, index):
                return self.candidates[index]

        class Page:
            def __init__(self):
                self.state = "new"
                self.url = ""
                self.actions = []
                self.password_values = ["", ""]
                self.filled_password_kinds = []
                self.otp_value = ""

            def goto(self, url, **_kwargs):
                self.url = url
                self.state = "home" if url == "https://chatgpt.com/" else "security"

            def wait_for_timeout(self, _milliseconds):
                return None

            def locator(self, selector):
                candidates = []
                if self.state == "home" and selector == '[data-testid="profile-button"]':
                    candidates = [Candidate(self, "profile")]
                elif self.state == "menu" and selector == '[data-testid="settings-menu-item"]':
                    candidates = [Candidate(self, "settings")]
                elif self.state == "settings" and selector == '[data-testid="security-tab"]':
                    candidates = [Candidate(self, "security")]
                elif self.state == "security" and selector == '[data-testid="add-password-button"]':
                    candidates = [Candidate(self, "add_password")]
                elif self.state == "auth_password" and selector == 'input[type="password"]':
                    candidates = [Candidate(self, "login_password")]
                elif self.state == "auth_password" and selector == 'a:has-text("Forgot password")':
                    candidates = [Candidate(self, "forgot_password")]
                elif self.state == "otp" and selector == 'input[autocomplete="one-time-code"]':
                    candidates = [Candidate(self, "otp_input")]
                elif self.state == "otp" and selector == 'button[type="submit"]:has-text("Continue")':
                    candidates = [Candidate(self, "submit_otp")]
                elif self.state == "reset_form" and selector == 'input[type="password"]':
                    candidates = [
                        Candidate(self, "new_password", 0),
                        Candidate(self, "confirm_password", 1),
                    ]
                elif self.state == "reset_form" and selector.startswith('button[type="submit"]'):
                    candidates = [Candidate(self, "submit_password")]
                elif self.state == "password_success" and selector == 'text="Password added"':
                    candidates = [Candidate(self, "password_success")]
                return Collection(candidates)

        class OtpReader:
            def __init__(self, *_args):
                pass

            def connect(self):
                return None

            def wait_for_code(self, _min_timestamp):
                return "123456"

            def close(self):
                return None

        page = Page()
        context = SimpleNamespace(pages=[page])
        worker = SimpleNamespace(
            account=SimpleNamespace(email="new@icloud.com"),
            log=lambda _message: None,
        )

        with patch(
            "hidemyemail_generator.openai_browser_bridge.ICloudOtpReader",
            OtpReader,
        ):
            confirmed = ensure_password_in_security_settings(
                SimpleNamespace(),
                worker,
                "Strong!Password123",
                context=context,
            )

        self.assertTrue(confirmed)
        self.assertEqual(page.otp_value, "123456")
        self.assertNotIn("login_password", page.filled_password_kinds)
        self.assertEqual(
            page.filled_password_kinds,
            ["new_password", "confirm_password"],
        )
        self.assertLess(
            page.actions.index("forgot_password"),
            page.actions.index("submit_password"),
        )

    def test_password_submission_returns_without_polling_confirmation(self):
        class Candidate:
            def __init__(self, page, kind, index=0):
                self.page = page
                self.kind = kind
                self.index = index

            def is_visible(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                transitions = {
                    "profile": "menu",
                    "settings": "settings",
                    "security": "security",
                    "add_password": "password_form",
                    "submit_password": "password_failure",
                }
                self.page.state = transitions.get(self.kind, self.page.state)

            def fill(self, value, **_kwargs):
                self.page.password_values[self.index] = value

            def input_value(self, **_kwargs):
                return self.page.password_values[self.index]

        class Collection:
            def __init__(self, candidates):
                self.candidates = candidates

            def count(self):
                return len(self.candidates)

            def nth(self, index):
                return self.candidates[index]

        class Page:
            def __init__(self):
                self.state = "new"
                self.url = ""
                self.password_values = ["", ""]

            def goto(self, url, **_kwargs):
                self.url = url
                self.state = "home" if url == "https://chatgpt.com/" else "security"

            def wait_for_timeout(self, _milliseconds):
                return None

            def locator(self, selector):
                candidates = []
                if self.state == "home" and selector == '[data-testid="profile-button"]':
                    candidates = [Candidate(self, "profile")]
                elif self.state == "menu" and selector == '[data-testid="settings-menu-item"]':
                    candidates = [Candidate(self, "settings")]
                elif self.state == "settings" and selector == '[data-testid="security-tab"]':
                    candidates = [Candidate(self, "security")]
                elif self.state == "security" and selector == '[data-testid="add-password-button"]':
                    candidates = [Candidate(self, "add_password")]
                elif self.state == "password_form" and selector == 'input[type="password"]':
                    candidates = [
                        Candidate(self, "password_input", 0),
                        Candidate(self, "password_input", 1),
                    ]
                elif self.state == "password_form" and selector.startswith('button[type="submit"]'):
                    candidates = [Candidate(self, "submit_password")]
                elif self.state == "password_failure":
                    raise AssertionError("password submission must not poll the page")
                return Collection(candidates)

        page = Page()
        worker = SimpleNamespace(
            account=SimpleNamespace(email="new@icloud.com"),
            log=lambda _message: None,
        )

        submitted = ensure_password_in_security_settings(
            SimpleNamespace(),
            worker,
            "Strong!Password123",
            context=SimpleNamespace(pages=[page]),
        )

        self.assertTrue(submitted)
        self.assertTrue(worker._password_step_submitted)

    def test_password_setup_rejects_account_upgrade_offer(self):
        class Candidate:
            def __init__(self, page, kind, aria_label):
                self.page = page
                self.kind = kind
                self.aria_label = aria_label

            def is_visible(self, **_kwargs):
                return True

            def get_attribute(self, name, **_kwargs):
                return self.aria_label if name == "aria-label" else ""

            def inner_text(self, **_kwargs):
                return self.aria_label

            def text_content(self, **_kwargs):
                return self.aria_label

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                self.page.actions.append(self.kind)
                if self.kind == "profile":
                    self.page.state = "menu"
                elif self.kind == "settings":
                    self.page.state = "settings"
                elif self.kind == "security":
                    self.page.state = "security"

        class Collection:
            def __init__(self, candidates):
                self.candidates = candidates

            def count(self):
                return len(self.candidates)

            def nth(self, index):
                return self.candidates[index]

        class Page:
            def __init__(self):
                self.url = "https://chatgpt.com/"
                self.state = "home"
                self.actions = []
                self.keyboard = SimpleNamespace(press=lambda _key: None)

            def goto(self, url, **_kwargs):
                self.url = url
                self.state = "home"

            def wait_for_timeout(self, _milliseconds):
                return None

            def locator(self, selector):
                candidates = []
                if self.state == "home" and selector == 'button[aria-haspopup="menu"]':
                    candidates = [
                        Candidate(self, "offer", "Upgrade account special offer"),
                        Candidate(self, "profile", "Account menu"),
                    ]
                elif self.state == "menu" and selector == '[data-testid="settings-menu-item"]':
                    candidates = [Candidate(self, "settings", "Settings")]
                elif self.state == "settings" and selector == '[data-testid="security-tab"]':
                    candidates = [Candidate(self, "security", "Security")]
                elif self.state == "security" and selector == '[data-testid="add-password-button"]':
                    candidates = [Candidate(self, "add_password", "Add password")]
                return Collection(candidates)

        page = Page()
        worker = SimpleNamespace(log=lambda _message: None)

        from hidemyemail_generator.openai_browser_bridge import (
            _open_security_settings,
        )

        self.assertTrue(_open_security_settings(page, worker))
        self.assertNotIn("offer", page.actions)
        self.assertEqual(page.actions[:3], ["profile", "settings", "security"])

    def test_password_settings_prefers_current_account_tab(self):
        class Candidate:
            def __init__(self, page, kind):
                self.page = page
                self.kind = kind

            def is_visible(self, **_kwargs):
                return True

            def get_attribute(self, name, **_kwargs):
                return "Account menu" if name == "aria-label" else ""

            def inner_text(self, **_kwargs):
                return "Account menu"

            def text_content(self, **_kwargs):
                return "Account menu"

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                self.page.actions.append(self.kind)
                self.page.state = self.kind

        class Collection:
            def __init__(self, candidates):
                self.candidates = candidates

            def count(self):
                return len(self.candidates)

            def nth(self, index):
                return self.candidates[index]

        class Page:
            def __init__(self):
                self.url = "https://chatgpt.com/"
                self.state = "home"
                self.actions = []

            def goto(self, url, **_kwargs):
                self.url = url
                self.state = "home"

            def wait_for_timeout(self, _milliseconds):
                return None

            def locator(self, selector):
                candidates = []
                if self.state == "home" and selector == '[data-testid="profile-button"]':
                    candidates = [Candidate(self, "profile")]
                elif self.state == "profile" and selector == '[data-testid="settings-menu-item"]':
                    candidates = [Candidate(self, "settings")]
                elif self.state == "settings" and selector == '[data-testid="account-tab"]':
                    candidates = [Candidate(self, "account")]
                elif self.state == "account" and selector == '[data-testid="add-password-button"]':
                    candidates = [Candidate(self, "add_password")]
                return Collection(candidates)

        page = Page()
        worker = SimpleNamespace(log=lambda _message: None)

        from hidemyemail_generator.openai_browser_bridge import (
            _open_security_settings,
        )

        self.assertTrue(_open_security_settings(page, worker))
        self.assertEqual(page.actions[:3], ["profile", "settings", "account"])

    def test_password_setup_closes_extra_tabs_and_keeps_one_chatgpt_page(self):
        class Page:
            def __init__(self, url):
                self.url = url
                self.closed = False
                self.front = False

            def is_closed(self):
                return self.closed

            def close(self):
                self.closed = True

            def bring_to_front(self):
                self.front = True

            def goto(self, url, **_kwargs):
                self.url = url

            def wait_for_timeout(self, _milliseconds):
                return None

            def locator(self, _selector):
                return SimpleNamespace(count=lambda: 0)

        auth_page = Page("https://auth.openai.com/email-verification")
        old_chatgpt_page = Page("https://chatgpt.com/")
        active_chatgpt_page = Page("https://chatgpt.com/?model=auto")
        context = SimpleNamespace(
            pages=[auth_page, old_chatgpt_page, active_chatgpt_page],
            new_page=lambda: self.fail("an existing ChatGPT page must be reused"),
        )
        worker = SimpleNamespace(
            account=SimpleNamespace(email="new@icloud.com"),
            log=lambda _message: None,
        )

        with self.assertRaisesRegex(RuntimeError, "账户密码设置"):
            ensure_password_in_security_settings(
                SimpleNamespace(),
                worker,
                "Strong!Password123",
                context=context,
            )

        self.assertTrue(auth_page.closed)
        self.assertTrue(old_chatgpt_page.closed)
        self.assertFalse(active_chatgpt_page.closed)
        self.assertTrue(active_chatgpt_page.front)

    def test_manual_account_type_is_not_overwritten_by_session_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            set_manual_account_type(db_file, "manual@icloud.com", "plus")

            _save_account_record(
                db_file,
                "manual@icloud.com",
                result={
                    "session_json": json.dumps(
                        {"account": {"planType": "free"}}
                    )
                },
            )

            record = load_account_record(db_file, "manual@icloud.com")
            self.assertEqual(record["account_type"], "plus")
            self.assertEqual(record["account_type_source"], "manual")

            set_manual_account_type(db_file, "manual@icloud.com", "unverified")
            record = load_account_record(db_file, "manual@icloud.com")
            self.assertNotIn("account_type", record)
            self.assertNotIn("account_type_source", record)

    def test_unconfirmed_password_is_saved_as_pending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            _save_account_record(
                db_file,
                "passwordless@icloud.com",
                password="Generated!A7",
                password_confirmed=False,
            )
            pending = load_account_record(db_file, "passwordless@icloud.com")
            self.assertEqual(pending["password"], "Generated!A7")
            self.assertFalse(pending["password_confirmed"])

            _save_account_record(
                db_file,
                "passwordless@icloud.com",
                password="Generated!A7",
                password_confirmed=True,
            )
            record = load_account_record(db_file, "passwordless@icloud.com")
            self.assertEqual(record["password"], "Generated!A7")
            self.assertTrue(record["password_confirmed"])

    def test_fresh_session_plan_updates_saved_account_classification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"

            _save_account_record(
                db_file,
                "plus@icloud.com",
                result={
                    "session_json": json.dumps(
                        {
                            "user": {"email": "plus@icloud.com"},
                            "account": {"planType": "plus"},
                        }
                    )
                },
            )

            record = load_account_record(db_file, "plus@icloud.com")
            self.assertEqual(record["account_type"], "plus")
            self.assertIn("account.planType=plus", record["verification_detail"])

    def test_camoufox_runtime_cache_is_writable_and_uses_xdg(self):
        previous_cache = os.environ.get("XDG_CACHE_HOME")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                runtime_home = Path(temp_dir)
                runtime_cache = _configure_camoufox_runtime_cache(runtime_home)

                self.assertEqual(runtime_cache, runtime_home / ".cache")
                self.assertEqual(os.environ["XDG_CACHE_HOME"], str(runtime_cache))
                self.assertTrue((runtime_cache / "fontconfig").is_dir())
                self.assertTrue(
                    (runtime_cache / "camoufox" / "fontconfig").is_dir()
                )
        finally:
            if previous_cache is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = previous_cache

    def test_fontconfig_generator_uses_writable_home_and_restores_environment(self):
        observed = []

        def generator(fontconfig_path):
            observed.append((fontconfig_path, os.environ.get("HOME")))
            return "runtime-fonts.conf"

        runtime_home = Path("/tmp/hidemyemail-camoufox-test")
        redirected = _fontconfig_generator_with_home(generator, runtime_home)
        previous_home = os.environ.get("HOME")
        os.environ["HOME"] = "original-home"
        try:
            self.assertEqual(redirected("bundled-fonts"), "runtime-fonts.conf")
            self.assertEqual(
                observed,
                [("bundled-fonts", str(runtime_home))],
            )
            self.assertEqual(os.environ.get("HOME"), "original-home")
        finally:
            if previous_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous_home

    def test_access_token_expiration(self):
        now = time.time()
        self.assertFalse(
            access_token_is_expired(token_with_exp(int(now + 3600)), now=now)
        )
        self.assertTrue(
            access_token_is_expired(token_with_exp(int(now - 1)), now=now)
        )
        self.assertTrue(access_token_is_expired("not-a-jwt", now=now))

    def test_generated_password_is_redacted_from_worker_log(self):
        message = safe_log_message("账户需要密码步骤，已生成密码: Secret123!A7")
        self.assertNotIn("Secret123", message)
        self.assertIn("已安全保存", message)

    def test_bearer_token_and_cookie_are_redacted_from_worker_log(self):
        token = "header.payload.signature-secret"
        message = safe_log_message(
            "Authorization: Bearer " + token + "\nCookie: session=private-value"
        )

        self.assertNotIn(token, message)
        self.assertNotIn("private-value", message)
        self.assertGreaterEqual(message.count("[已隐藏]"), 2)

    def test_password_fill_falls_back_to_native_input_event(self):
        class Worker:
            def __init__(self):
                self.logs = []

            def log(self, message):
                self.logs.append(message)

        class Locator:
            def __init__(self):
                self.value = ""

            def click(self, **_kwargs):
                return None

            def fill(self, *_args, **_kwargs):
                raise RuntimeError("controlled input rejected fill")

            def evaluate(self, _script, value):
                self.value = value

            def input_value(self, **_kwargs):
                return self.value

        worker = Worker()
        locator = Locator()

        self.assertTrue(resilient_force_fill_locator(worker, locator, "Strong!Pass123"))
        self.assertEqual(locator.value, "Strong!Pass123")
        self.assertEqual(worker.logs, ["[认证] 已使用兼容输入方式填写密码"])

    def test_password_fill_falls_back_to_keyboard_input(self):
        class Worker:
            def __init__(self):
                self.logs = []

            def log(self, message):
                self.logs.append(message)

        class Locator:
            def __init__(self):
                self.value = ""

            def click(self, **_kwargs):
                return None

            def fill(self, *_args, **_kwargs):
                return None

            def evaluate(self, *_args):
                return None

            def press(self, key, **_kwargs):
                if key == "Backspace":
                    self.value = ""

            def type(self, value, **_kwargs):
                self.value += value

            def input_value(self, **_kwargs):
                return self.value

        worker = Worker()
        locator = Locator()

        self.assertTrue(resilient_force_fill_locator(worker, locator, "Strong!Pass123"))
        self.assertEqual(locator.value, "Strong!Pass123")
        self.assertEqual(worker.logs, ["[认证] 已使用键盘输入方式填写密码"])

    def test_visible_about_you_input_is_activated_and_dom_repaired(self):
        events = []

        class Page:
            def __init__(self):
                self.values = ["Wrong Name", "27"]

            def bring_to_front(self):
                events.append("front")

            def evaluate(self, script, *_args):
                if "window.focus" in script:
                    events.append("window-focus")
                return None

        class Worker:
            headless = False

            def __init__(self):
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _fill_visible_input_by_keyboard(self, _page, index, value):
                events.append(f"keyboard:{index}:{value}")

            def _fill_about_you_inputs(
                self, page, name, _birthdate, _birth_year, age
            ):
                self._fill_visible_input_by_keyboard(page, 0, name)
                self._fill_visible_input_by_keyboard(page, 1, age)

            def _fill_about_you_inputs_by_dom(
                self, page, name, second_value, _second_kind
            ):
                events.append("dom-repair")
                page.values = [name, second_value]
                return list(page.values)

            def _visible_input_values(self, page):
                return list(page.values)

            def _about_you_second_field_context(self, _page):
                return "Age"

            def _about_you_second_field_kind_from_context(self, _context):
                return "age"

            def _about_you_second_field_value(
                self, _kind, _birth_year, age, _birthdate, _context
            ):
                return age

            def _about_you_values_ok(self, values, _kind):
                return len(values) >= 2 and values[0] == "Noah Scott" and values[1] == "27"

            def _focus_about_you_submit_or_body(self, _page):
                events.append("focus-submit")

        worker = Worker()
        page = Page()

        self.assertTrue(configure_resilient_about_you_input(worker))
        worker._fill_about_you_inputs(page, "Noah Scott", "1999-01-01", "1999", "27")

        self.assertEqual(page.values, ["Noah Scott", "27"])
        self.assertNotIn("front", events)
        self.assertNotIn("window-focus", events)
        self.assertIn("dom-repair", events)
        self.assertTrue(any("DOM 重填后回读校验通过" in item for item in worker.logs))

    def test_about_you_ready_skips_stalled_full_load_wait(self):
        class Candidate:
            def is_visible(self, **_kwargs):
                return True

            def is_enabled(self, **_kwargs):
                return True

            def is_editable(self, **_kwargs):
                return True

        class Collection:
            def count(self):
                return 1

            def nth(self, _index):
                return Candidate()

        class Page:
            def wait_for_load_state(self, state, **_kwargs):
                self.state = state

            def locator(self, _selector):
                return Collection()

            def wait_for_timeout(self, _milliseconds):
                return None

        class Worker:
            headless = True

            def __init__(self):
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _wait_for_auth_page_ready(self, *_args, **_kwargs):
                raise AssertionError("full load wait must not run for about-you")

            def _fill_visible_input_by_keyboard(self, *_args):
                return None

            def _fill_about_you_inputs(self, *_args):
                return None

        worker = Worker()
        page = Page()
        self.assertTrue(configure_resilient_about_you_input(worker))

        worker._wait_for_auth_page_ready(
            page,
            "基础资料",
            ready_selectors=("input",),
            require_editable=True,
        )

        self.assertEqual(page.state, "domcontentloaded")
        self.assertTrue(any("跳过会卡住的完整 load 等待" in line for line in worker.logs))

    def test_about_you_input_stops_before_submit_when_readback_stays_wrong(self):
        class Page:
            values = ["Wrong Name", "27"]

            def bring_to_front(self):
                return None

            def evaluate(self, *_args):
                return None

        class Worker:
            headless = False
            log = staticmethod(lambda _message: None)
            _fill_visible_input_by_keyboard = staticmethod(
                lambda _page, _index, _value: None
            )
            _fill_about_you_inputs = staticmethod(
                lambda _page, _name, _birthdate, _birth_year, _age: None
            )
            _fill_about_you_inputs_by_dom = staticmethod(
                lambda _page, _name, _second, _kind: None
            )
            _visible_input_values = staticmethod(lambda page: list(page.values))
            _about_you_second_field_context = staticmethod(lambda _page: "Age")
            _about_you_second_field_kind_from_context = staticmethod(
                lambda _context: "age"
            )
            _about_you_second_field_value = staticmethod(
                lambda _kind, _birth_year, age, _birthdate, _context: age
            )
            _about_you_values_ok = staticmethod(lambda _values, _kind: True)
            _focus_about_you_submit_or_body = staticmethod(lambda _page: None)

        worker = Worker()
        self.assertTrue(configure_resilient_about_you_input(worker))

        with self.assertRaisesRegex(RuntimeError, "已停止提交"):
            worker._fill_about_you_inputs(
                Page(), "Noah Scott", "1999-01-01", "1999", "27"
            )

    def test_about_you_submit_does_not_repeat_a_stalled_click(self):
        class Page:
            def wait_for_load_state(self, _state, **_kwargs):
                return None

            def wait_for_timeout(self, _milliseconds):
                return None

        class Worker:
            headless = True

            def __init__(self):
                self.logs = []
                self.submit_calls = 0

            def log(self, message):
                self.logs.append(message)

            def _fill_visible_input_by_keyboard(self, _page, _index, _value):
                return None

            def _fill_about_you_inputs(
                self, _page, _name, _birthdate, _birth_year, _age
            ):
                return None

            def _visible_input_values(self, _page):
                return ["Noah Scott", "27"]

            def _about_you_second_field_context(self, _page):
                return "Age"

            def _about_you_second_field_kind_from_context(self, _context):
                return "age"

            def _about_you_second_field_value(
                self, _kind, _birth_year, age, _birthdate, _context
            ):
                return age

            def _about_you_values_ok(self, values, _kind):
                return values == ["Noah Scott", "27"]

            def _submit_about_you(self, _page):
                self.submit_calls += 1
                raise RuntimeError(
                    "基础资料按钮点击后 120 秒内页面未响应；未自动重新提交"
                )

        worker = Worker()
        self.assertTrue(configure_resilient_about_you_input(worker))

        with self.assertRaisesRegex(RuntimeError, "120 秒"):
            worker._submit_about_you(Page())
        self.assertEqual(worker.submit_calls, 1)

    def test_about_you_finish_click_waits_for_stable_values_and_button(self):
        events = []

        class Candidate:
            def is_visible(self, **_kwargs):
                return True

            def is_enabled(self, **_kwargs):
                return True

            def is_editable(self, **_kwargs):
                return False

        class Collection:
            def count(self):
                return 1

            def nth(self, _index):
                return Candidate()

        class Page:
            def wait_for_load_state(self, state, **_kwargs):
                events.append(("load", state))

            def wait_for_timeout(self, milliseconds):
                events.append(("wait", milliseconds))

            def locator(self, _selector):
                return Collection()

        class Worker:
            headless = True

            def __init__(self):
                self.logs = []
                self.value_checks = 0
                self.clicks = 0

            def log(self, message):
                self.logs.append(message)

            def _fill_visible_input_by_keyboard(self, *_args):
                return None

            def _fill_about_you_inputs(self, *_args):
                return None

            def _about_you_current_values_ok(self, _page):
                self.value_checks += 1
                return self.value_checks > 1

            def _click_finish_creating_account(self, _page):
                self.clicks += 1
                events.append("click")
                return True

        worker = Worker()
        self.assertTrue(configure_resilient_about_you_input(worker))

        self.assertTrue(worker._click_finish_creating_account(Page()))

        self.assertEqual(worker.clicks, 1)
        self.assertEqual(worker.value_checks, 7)
        self.assertEqual(events.count("click"), 1)
        self.assertGreaterEqual(events.count(("wait", 400)), 6)
        self.assertTrue(any("连续稳定约 2 秒" in line for line in worker.logs))


class BrowserTaskManagerTests(unittest.IsolatedAsyncioTestCase):
    def test_browser_log_context_tracks_page_location_and_action(self):
        google = browser_log_context(
            "[认证] 检测到 Google 登录要求；关闭当前浏览器并请求生成全新指纹"
        )
        security = browser_log_context(
            "[认证] 已检测到安全验证，请在当前浏览器完成；程序保持登录流程并继续监测"
        )
        password = browser_log_context("[认证] 已提交创建邮箱时保存的唯一密码")
        session = browser_log_context("OpenAI 注册成功且 Session 已保存")
        openai_email = browser_log_context(
            "[认证] 填写 OpenAI 邮箱注册字段（不使用 Google 账号登录）"
        )
        two_factor = browser_log_context("OpenAI 注册成功，开始开启 2FA")

        self.assertEqual(google["stage"], "google_oauth")
        self.assertEqual(google["location"], "Google 登录页")
        self.assertEqual(google["action"], "关闭当前浏览器并更换指纹")
        self.assertEqual(security["status"], "waiting")
        self.assertEqual(security["location"], "安全验证页")
        self.assertEqual(password["location"], "OpenAI 密码页")
        self.assertEqual(session["stage"], "completed")
        self.assertEqual(openai_email["stage"], "openai_auth")
        self.assertEqual(two_factor["stage"], "two_factor")

    def test_browser_log_context_treats_zero_failures_as_success(self):
        context = browser_log_context(
            "浏览器取全部完成：成功 1，失败 0，跳过 0"
        )

        self.assertEqual(context["status"], "success")

    def test_browser_log_context_does_not_report_otp_submitted_before_input(self):
        waiting = browser_log_context(
            "[状态识别] 邮箱已提交；当前页面=邮箱验证码页；下一步=读取本轮最新邮箱验证码"
        )
        checked = browser_log_context(
            "[验证码] 输入检查通过：浏览器可见输入框已包含 6 位验证码；现在点击继续"
        )

        self.assertEqual(waiting["action"], "等待并监测邮箱验证码")
        self.assertEqual(checked["action"], "校验验证码输入并点击继续")

    def test_browser_log_context_keeps_otp_submit_as_completed_step(self):
        clicked = browser_log_context(
            "[验证码] 输入框已回读一致；已直接点击当前页面的继续按钮，未刷新验证码页面"
        )
        upstream = browser_log_context("已通过接口提交邮箱验证码")
        acquired = browser_log_context("已安全取得本次所需的邮箱验证码")
        fallback = browser_log_context("正在确认邮箱验证码输入界面")

        self.assertEqual(clicked["action"], "验证码已提交，等待页面跳转")
        self.assertEqual(upstream["action"], "验证码已提交，等待页面跳转")
        self.assertEqual(acquired["action"], "验证码已获取，准备写入输入框")
        self.assertEqual(fallback["action"], "核对邮箱验证码页面状态")

    def test_browser_log_context_uses_selected_roxy_engine(self):
        context = browser_log_context(
            "开始浏览器注册或登录",
            browser_engine="roxy",
        )

        self.assertEqual(context["stage"], "browser")
        self.assertEqual(context["action"], "启动并监测 Roxy 浏览器")

    def test_append_log_uses_roxy_in_current_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = BrowserTaskManager(
                target_project_dir=root,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=root / "hme.db",
                python_executable=Path(sys.executable),
                bridge_file=root / "bridge.py",
            )
            manager._state["browserEngine"] = "roxy"

            manager._append_log("开始浏览器注册或登录")
            state = manager.snapshot()

            self.assertEqual(state["currentAction"], "启动并监测 Roxy 浏览器")
            self.assertNotIn("Camoufox", state["currentAction"])

    def test_append_log_publishes_current_execution_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = BrowserTaskManager(
                target_project_dir=root,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=root / "hme.db",
                python_executable=Path(sys.executable),
                bridge_file=root / "bridge.py",
            )

            manager._state["accounts"] = [
                {"email": "person@gmail.com", "status": "running", "latestLog": ""}
            ]
            manager._append_log(
                "[认证] 检测到 Google 登录要求；关闭当前浏览器并生成全新指纹",
                email="person@gmail.com",
            )
            state = manager.snapshot()

            self.assertEqual(state["currentLocation"], "Google 登录页")
            self.assertEqual(state["currentStatus"], "active")
            self.assertEqual(state["logs"][-1]["stage"], "google_oauth")
            self.assertEqual(
                state["accounts"][0]["action"],
                "关闭当前浏览器并更换指纹",
            )
            self.assertEqual(state["accounts"][0]["status"], "running")
            self.assertEqual(state["accounts"][0]["logStatus"], "active")

    async def test_worker_page_state_event_is_exposed_to_the_ui(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "state = {\n"
                "  'code':'password','currentPage':'OpenAI 密码页',\n"
                "  'stage':'password','completedSteps':['浏览器与网络已准备','邮箱已提交'],\n"
                "  'nextAction':'填写唯一密码并提交','actionMode':'automatic',\n"
                "  'confidence':97,'source':'dom','route':'auth.openai.com/password',\n"
                "  'readyState':'complete','stalled':False,'stalledSeconds':0\n"
                "}\n"
                "chain = {\n"
                "  'status':'running','currentCode':'verification_page',\n"
                "  'currentStep':'已进入邮箱验证码界面','currentValue':'已识别验证码输入控件',\n"
                "  'currentCompleted':True,'nextCode':'verification_requested','canAdvance':True,\n"
                "  'steps':[{'index':8,'code':'verification_page','label':'已进入邮箱验证码界面','status':'completed','value':'已识别验证码输入控件','completed':True}],\n"
                "  'completedSteps':['已进入邮箱验证码界面'],'nextAction':'请求本轮最新验证码',\n"
                "  'registrationCreated':False,'sessionReady':False,'passwordConfirmed':False,'twoFactorEnabled':False,'fullRegistrationComplete':False,\n"
                "  'requestActivity':{'requestCount':4,'responseCount':3,'failedCount':0,'loadCount':2,'lastMethod':'POST','lastRoute':'auth.openai.com/api/accounts/email-otp/send','lastStatus':200}\n"
                "}\n"
                "print(prefix + json.dumps({'type':'registration_chain','state':chain}, ensure_ascii=False), flush=True)\n"
                "print(prefix + json.dumps({'type':'page_state','state':state}, ensure_ascii=False), flush=True)\n"
                "print(prefix + json.dumps({'type':'result','result':{'access_token':'at-state'}}, ensure_ascii=False), flush=True)\n",
                encoding="utf-8",
            )
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=root / "hme.db",
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            manager.start(
                [{"email": "state@icloud.com"}],
                headless=True,
                concurrency=1,
            )
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            page_state = snapshot["accounts"][0]["pageState"]
            self.assertEqual(page_state["currentPage"], "OpenAI 密码页")
            self.assertEqual(page_state["completedSteps"][-1], "邮箱已提交")
            self.assertEqual(page_state["nextAction"], "填写唯一密码并提交")
            self.assertEqual(snapshot["pageState"]["email"], "state@icloud.com")
            chain = snapshot["registrationChain"]
            self.assertEqual(chain["currentCode"], "verification_page")
            self.assertTrue(chain["currentCompleted"])
            self.assertEqual(chain["nextCode"], "verification_requested")
            self.assertEqual(chain["requestActivity"]["requestCount"], 4)
            self.assertEqual(chain["steps"][0]["status"], "completed")

    async def test_google_login_restarts_worker_with_fresh_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json, os, sys\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "attempt = int(os.environ.get('HME_BROWSER_FINGERPRINT_ATTEMPT', '0'))\n"
                "print(prefix + json.dumps({'type':'log','message':'fingerprint-attempt=' + str(attempt)}), flush=True)\n"
                "if attempt == 0:\n"
                "    print(prefix + json.dumps({'type':'fresh_fingerprint_required','reason':'google-login'}), flush=True)\n"
                "    sys.exit(75)\n"
                "result = {'access_token':'at-fresh','session_json':'{}','two_factor':{'enabled':True}}\n"
                "print(prefix + json.dumps({'type':'result','result':result,'password':'Strong!Pass123','password_confirmed':True}), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            manager.start(
                [
                    {
                        "email": "fresh@gmail.com",
                        "password": "Strong!Pass123",
                        "ensure_password": True,
                        "enable_2fa": True,
                    }
                ],
                headless=True,
                concurrency=1,
            )
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            messages = [entry["message"] for entry in snapshot["logs"]]
            self.assertEqual(snapshot["succeeded"], 1)
            self.assertEqual(snapshot["failed"], 0)
            self.assertEqual(snapshot["accounts"][0]["fingerprintRetries"], 1)
            self.assertEqual(
                [message for message in messages if "fingerprint-attempt=" in message],
                ["fingerprint-attempt=0", "fingerprint-attempt=1"],
            )
            self.assertTrue(
                any("当前浏览器已关闭" in message and "全新指纹" in message for message in messages)
            )
            self.assertEqual(
                load_account_record(db_file, "fresh@gmail.com")["access_token"],
                "at-fresh",
            )

    async def test_repeated_google_login_stops_after_fingerprint_retry_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json, os, sys\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "attempt = os.environ.get('HME_BROWSER_FINGERPRINT_ATTEMPT', '')\n"
                "print(prefix + json.dumps({'type':'log','message':'fingerprint-attempt=' + attempt}), flush=True)\n"
                "print(prefix + json.dumps({'type':'fresh_fingerprint_required','reason':'google-login'}), flush=True)\n"
                "sys.exit(75)\n",
                encoding="utf-8",
            )
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=root / "hme.db",
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            manager.start(
                [
                    {
                        "email": "repeat@gmail.com",
                        "password": "Strong!Pass123",
                        "ensure_password": True,
                    }
                ],
                headless=True,
                concurrency=1,
            )
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            messages = [entry["message"] for entry in snapshot["logs"]]
            self.assertEqual(snapshot["succeeded"], 0)
            self.assertEqual(snapshot["failed"], 1)
            self.assertEqual(snapshot["accounts"][0]["fingerprintRetries"], 1)
            self.assertEqual(
                [message for message in messages if "fingerprint-attempt=" in message],
                ["fingerprint-attempt=0", "fingerprint-attempt=1"],
            )
            self.assertIn(
                "第二个独立指纹仍被要求 Google 登录",
                snapshot["accounts"][0]["message"],
            )
            self.assertIn("已放弃该 Gmail", snapshot["accounts"][0]["message"])

    async def test_roxy_registration_opens_five_distinct_profiles_with_clash_ports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json, os, sys\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "message = ';'.join([\n"
                "    'engine=' + os.environ.get('HME_BROWSER_ENGINE', ''),\n"
                "    'workspace=' + os.environ.get('HME_ROXY_WORKSPACE_ID', ''),\n"
                "    'profile=' + os.environ.get('HME_ROXY_PROFILE_ID', ''),\n"
                "    'proxy=' + os.environ.get('HME_REGISTRATION_PROXY_URL', ''),\n"
                "    'headless=' + str('--headless' in sys.argv),\n"
                "])\n"
                "print(prefix + json.dumps({'type':'log','message':message}), flush=True)\n"
                "print(prefix + json.dumps({'type':'result','result':{'access_token':'at-roxy'}}), flush=True)\n",
                encoding="utf-8",
            )

            class RoxyStore:
                def runtime_config(self, profile_count=1):
                    profile_ids = [
                        f"dedicated-{index}" for index in range(1, 6)
                    ][:profile_count]
                    return {
                        "apiUrl": "http://127.0.0.1:50000",
                        "workspaceId": "136502",
                        "profileId": profile_ids[0],
                        "profileIds": profile_ids,
                    }

                def load(self):
                    return self.runtime_config()

            class ProxyStore:
                def __init__(self):
                    self.index = 0

                def public_state(self):
                    return {
                        "enabled": True,
                        "configured": True,
                        "mode": "clash",
                        "country": "JP",
                        "fixedPortsEnabled": True,
                        "fixedPortCount": 21,
                        "maxLatencyMs": 900,
                    }

                def next_proxy(self):
                    self.index += 1
                    port = 18000 + self.index
                    return f"http://127.0.0.1:{port}", {
                        **self.public_state(),
                        "endpoint": f"127.0.0.1:{port}",
                        "currentNode": f"JP-{self.index}",
                        "lastLatencyMs": 50 + self.index,
                    }

            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=root / "hme.db",
                python_executable=Path(sys.executable),
                bridge_file=bridge,
                registration_proxy_store=ProxyStore(),
                roxy_registration_store=RoxyStore(),
            )
            accounts = [
                {"email": f"roxy-{index}@icloud.com"}
                for index in range(1, 6)
            ]
            state = manager.start(
                accounts,
                headless=True,
                concurrency=5,
                use_registration_proxy=True,
                browser_engine="roxy",
            )
            await asyncio.wait_for(manager._batch_task, timeout=30)

            messages = [entry["message"] for entry in manager.snapshot()["logs"]]
            self.assertEqual(state["browserEngine"], "roxy")
            self.assertEqual(state["concurrency"], 5)
            self.assertEqual(
                state["roxyProfileIds"],
                [f"dedicated-{index}" for index in range(1, 6)],
            )
            self.assertEqual(
                {item["roxyProfileId"] for item in state["accounts"]},
                {f"dedicated-{index}" for index in range(1, 6)},
            )
            for index in range(1, 6):
                self.assertTrue(
                    any(
                        f"profile=dedicated-{index}" in message
                        and "proxy=http://127.0.0.1:18" in message
                        and "headless=True" in message
                        for message in messages
                    )
                )

    async def test_roxy_target_accounts_reuse_each_profile_serially(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "fake_bridge.py"
            bridge.write_text("# replaced by test coroutine\n", encoding="utf-8")

            class RoxyStore:
                def runtime_config(self, profile_count=1):
                    profile_ids = [
                        f"dedicated-{index}" for index in range(1, 6)
                    ][:profile_count]
                    return {
                        "apiUrl": "http://127.0.0.1:50000",
                        "workspaceId": "136502",
                        "profileId": profile_ids[0],
                        "profileIds": profile_ids,
                    }

            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=root / "hme.db",
                python_executable=Path(sys.executable),
                bridge_file=bridge,
                roxy_registration_store=RoxyStore(),
            )
            active_profiles = set()
            overlaps = []
            profile_loops = {}

            async def fake_run_account(item, *, semaphore, headless):
                del headless
                async with semaphore:
                    profile_id = item["_roxy_profile_id"]
                    if profile_id in active_profiles:
                        overlaps.append(profile_id)
                    active_profiles.add(profile_id)
                    profile_loops.setdefault(profile_id, []).append(item["loopIndex"])
                    await asyncio.sleep(
                        0.08
                        if profile_id == "dedicated-1" and item["loopIndex"] == 1
                        else 0.005
                    )
                    active_profiles.remove(profile_id)
                    item["status"] = "success"
                    manager._state["succeeded"] += 1
                    manager._state["completed"] += 1

            manager._run_account = fake_run_account
            state = manager.start(
                [
                    {"email": f"target-{index}@icloud.com"}
                    for index in range(1, 13)
                ],
                headless=True,
                concurrency=5,
                browser_engine="roxy",
            )
            await asyncio.wait_for(manager._batch_task, timeout=5)

            self.assertEqual(state["targetCount"], 12)
            self.assertEqual(state["loopCount"], 3)
            self.assertEqual(overlaps, [])
            self.assertEqual(profile_loops["dedicated-1"], [1, 2, 3])
            self.assertEqual(profile_loops["dedicated-2"], [1, 2, 3])
            self.assertEqual(profile_loops["dedicated-3"], [1, 2])
            self.assertEqual(profile_loops["dedicated-4"], [1, 2])
            self.assertEqual(profile_loops["dedicated-5"], [1, 2])

    async def test_manual_otp_entry_forces_visible_browser_without_password_first(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json, os, sys\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "message = ';'.join([\n"
                "    'manual=' + os.environ.get('HME_MANUAL_OTP_ENTRY', ''),\n"
                "    'foreground=' + os.environ.get('HME_BROWSER_FOREGROUND_REQUIRED', ''),\n"
                "    'password=' + os.environ.get('HME_PASSWORD_FIRST_REQUIRED', ''),\n"
                "    'headless=' + str('--headless' in sys.argv),\n"
                "])\n"
                "print(prefix + json.dumps({'type':'log','message':message}), flush=True)\n"
                "print(prefix + json.dumps({'type':'result','result':{'access_token':'at-test'}}), flush=True)\n",
                encoding="utf-8",
            )
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=root / "hme.db",
                python_executable=Path(sys.executable),
                bridge_file=bridge,
                force_headless=True,
            )

            state = manager.start(
                [
                    {
                        "email": "my.address@gmail.com",
                        "password": "Generated!A7",
                        "ensure_password": True,
                        "manual_otp_entry": True,
                    }
                ],
                headless=True,
                concurrency=4,
            )
            await asyncio.wait_for(manager._batch_task, timeout=30)

            messages = [entry["message"] for entry in manager.snapshot()["logs"]]
            self.assertFalse(state["headless"])
            self.assertTrue(state["foregroundRequired"])
            self.assertTrue(state["accounts"][0]["manualOtpEntry"])
            self.assertIn(
                "manual=1;foreground=1;password=0;headless=False",
                messages,
            )

    async def test_gmail_registration_runs_in_background_with_requested_concurrency(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json, os, sys\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "message = ';'.join([\n"
                "    'foreground=' + os.environ.get('HME_BROWSER_FOREGROUND_REQUIRED', ''),\n"
                "    'password=' + os.environ.get('HME_PASSWORD_FIRST_REQUIRED', ''),\n"
                "    'headless=' + str('--headless' in sys.argv),\n"
                "    'slots=' + os.environ.get('HME_BROWSER_WINDOW_SLOTS', ''),\n"
                "])\n"
                "print(prefix + json.dumps({'type':'log','message':message}), flush=True)\n"
                "print(prefix + json.dumps({'type':'result','result':{'access_token':'at-test'}}), flush=True)\n",
                encoding="utf-8",
            )
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=root / "hme.db",
                python_executable=Path(sys.executable),
                bridge_file=bridge,
                force_headless=True,
            )

            state = manager.start(
                [
                    {
                        "email": "gmail.foreground@gmail.com",
                        "password": "Generated!A7",
                        "ensure_password": True,
                    },
                    {
                        "email": "gmail.second@gmail.com",
                        "password": "Generated!B8",
                        "ensure_password": True,
                    },
                ],
                headless=True,
                concurrency=2,
            )
            await asyncio.wait_for(manager._batch_task, timeout=30)

            messages = [entry["message"] for entry in manager.snapshot()["logs"]]
            self.assertTrue(state["headless"])
            self.assertFalse(state["foregroundRequired"])
            self.assertEqual(state["concurrency"], 2)
            self.assertEqual(
                messages.count("foreground=;password=1;headless=True;slots=2"),
                2,
            )
            self.assertTrue(any("后台交互已启用" in item for item in messages))

    async def test_concurrent_workers_receive_distinct_window_slots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json, os\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "slot = os.environ.get('HME_BROWSER_WINDOW_SLOT', '')\n"
                "slots = os.environ.get('HME_BROWSER_WINDOW_SLOTS', '')\n"
                "print(prefix + json.dumps({'type':'log','message':'window-slot=' + slot + '/' + slots}), flush=True)\n"
                "print(prefix + json.dumps({'type':'result','result':{'access_token':'at-test'}}), flush=True)\n",
                encoding="utf-8",
            )
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=root / "hme.db",
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            manager.start(
                [
                    {"email": f"slot-{index}@icloud.com", "password": ""}
                    for index in range(3)
                ],
                headless=False,
                concurrency=3,
            )
            await asyncio.wait_for(manager._batch_task, timeout=10)

            logs = {
                entry["message"]
                for entry in manager.snapshot()["logs"]
                if "window-slot=" in entry["message"]
            }
            self.assertEqual(
                logs,
                {"window-slot=0/3", "window-slot=1/3", "window-slot=2/3"},
            )

    async def test_registration_uses_unique_country_proxy_without_exposing_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json, os\n"
                "from urllib.parse import unquote, urlsplit\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "proxy = urlsplit(os.environ['HME_REGISTRATION_PROXY_URL'])\n"
                "username = unquote(proxy.username or '')\n"
                "marker = username.rsplit('-sid-', 1)[-1].split('-t-', 1)[0]\n"
                "print(prefix + json.dumps({'type':'log','message':'proxy-country=' + os.environ.get('HME_REGISTRATION_PROXY_COUNTRY','') + ';sid=' + marker}), flush=True)\n"
                "result = {'access_token':'at-test','session_json':'{}'}\n"
                "print(prefix + json.dumps({'type':'result','result':result}), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            proxy_store = RegistrationProxyStore(db_file)
            proxy_store.configure(
                enabled=True,
                country="NL",
                proxy_line="proxy.example:3010:private-user:private-password",
            )
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
                registration_proxy_store=proxy_store,
            )
            manager.start(
                [
                    {"email": "one@icloud.com", "password": ""},
                    {"email": "two@icloud.com", "password": ""},
                ],
                headless=True,
                concurrency=2,
                use_registration_proxy=True,
            )
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            serialized = json.dumps(snapshot)
            proxy_logs = [
                entry["message"]
                for entry in snapshot["logs"]
                if "proxy-country=" in entry["message"]
            ]
            sids = {message.rsplit("sid=", 1)[-1] for message in proxy_logs}

            self.assertEqual(snapshot["succeeded"], 2)
            self.assertTrue(snapshot["useRegistrationProxy"])
            self.assertEqual(snapshot["registrationProxy"]["country"], "NL")
            self.assertEqual(len(sids), 2)
            self.assertNotIn("private-user", serialized)
            self.assertNotIn("private-password", serialized)
            self.assertNotIn("HME_REGISTRATION_PROXY_URL", serialized)

    async def test_clash_registration_is_serial_and_keeps_one_node_per_account(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json, os\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "message = 'node=' + os.environ.get('HME_REGISTRATION_PROXY_URL', '')\n"
                "print(prefix + json.dumps({'type':'log','message':message}), flush=True)\n"
                "print(prefix + json.dumps({'type':'result','result':{'access_token':'at-test','session_json':'{}'}}), flush=True)\n",
                encoding="utf-8",
            )

            class FakeClashStore:
                def __init__(self):
                    self.index = 0

                def public_state(self):
                    return {
                        "enabled": True,
                        "configured": True,
                        "mode": "clash",
                        "country": "JP",
                        "countryLabel": "日本",
                        "maxLatencyMs": 900,
                    }

                def next_proxy(self):
                    self.index += 1
                    return "http://127.0.0.1:7897", {
                        **self.public_state(),
                        "endpoint": "127.0.0.1:7897",
                        "currentNode": f"日本节点-{self.index}",
                        "lastLatencyMs": 100 + self.index,
                    }

            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=root / "hme.db",
                python_executable=Path(sys.executable),
                bridge_file=bridge,
                registration_proxy_store=FakeClashStore(),
            )
            started = manager.start(
                [
                    {"email": "one@icloud.com", "password": ""},
                    {"email": "two@icloud.com", "password": ""},
                ],
                headless=True,
                concurrency=4,
                use_registration_proxy=True,
            )
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(started["concurrency"], 1)
            self.assertEqual(snapshot["succeeded"], 2)
            self.assertEqual(
                [item["proxyNode"] for item in snapshot["accounts"]],
                ["日本节点-1", "日本节点-2"],
            )
            self.assertTrue(
                any("本账号注册、2FA 与 Session 获取结束前不再切换" in item["message"] for item in snapshot["logs"])
            )

    async def test_unconfirmed_password_result_still_saves_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "result = {'access_token':'at-test','session_json':'{}'}\n"
                "event = {'type':'result','result':result,'password':'LocalOnly!A7','password_confirmed':False}\n"
                "print(prefix + json.dumps(event), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            manager.start(
                [
                    {
                        "email": "passwordless@icloud.com",
                        "password": "LocalOnly!A7",
                        "ensure_password": True,
                    }
                ],
                headless=True,
                concurrency=1,
            )
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["succeeded"], 1)
            self.assertEqual(snapshot["failed"], 0)
            self.assertIn("OpenAI 免密码注册", snapshot["accounts"][0]["message"])
            self.assertNotIn("密码待设置", snapshot["accounts"][0]["message"])
            self.assertFalse(snapshot["accounts"][0]["passwordConfirmed"])
            record = load_account_record(db_file, "passwordless@icloud.com")
            self.assertEqual(record["access_token"], "at-test")
            self.assertEqual(record["password"], "LocalOnly!A7")
            self.assertFalse(record["password_confirmed"])

    async def test_required_icloud_password_result_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "result = {'access_token':'at-rejected','session_json':'{}'}\n"
                "event = {'type':'result','result':result,'password':'LocalOnly!A7','password_confirmed':False}\n"
                "print(prefix + json.dumps(event), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            manager.start(
                [
                    {
                        "email": "required-password@icloud.com",
                        "password": "LocalOnly!A7",
                        "ensure_password": True,
                        "password_first_required": True,
                    }
                ],
                headless=True,
                concurrency=1,
            )
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["succeeded"], 0)
            self.assertEqual(snapshot["failed"], 1)
            self.assertIn(
                "拒绝保存免密码账号", snapshot["accounts"][0]["message"]
            )
            record = load_account_record(db_file, "required-password@icloud.com")
            self.assertFalse(record.get("access_token"))

    async def test_gmail_result_without_confirmed_password_and_two_factor_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "result = {'access_token':'at-rejected','session_json':'{}'}\n"
                "event = {'type':'result','result':result,'password':'LocalOnly!A7','password_confirmed':False}\n"
                "print(prefix + json.dumps(event), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            manager.start(
                [
                    {
                        "email": "strict@gmail.com",
                        "password": "LocalOnly!A7",
                        "ensure_password": True,
                        "enable_2fa": True,
                    }
                ],
                headless=True,
                concurrency=1,
            )
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["succeeded"], 0)
            self.assertEqual(snapshot["failed"], 1)
            self.assertIn("拒绝保存免密码账号", snapshot["accounts"][0]["message"])
            record = load_account_record(db_file, "strict@gmail.com")
            self.assertFalse(record.get("access_token"))

    async def test_required_icloud_result_without_enabled_two_factor_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "result = {'access_token':'at-rejected','session_json':'{}'}\n"
                "event = {'type':'result','result':result,'password':'LocalOnly!A7','password_confirmed':True}\n"
                "print(prefix + json.dumps(event), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            manager.start(
                [
                    {
                        "email": "strict@icloud.com",
                        "password": "LocalOnly!A7",
                        "password_confirmed": True,
                        "ensure_password": True,
                        "password_first_required": True,
                        "enable_2fa": True,
                    }
                ],
                headless=True,
                concurrency=1,
            )
            await asyncio.wait_for(manager._batch_task, timeout=30)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["succeeded"], 0)
            self.assertEqual(snapshot["failed"], 1)
            self.assertIn("TOTP 2FA", snapshot["accounts"][0]["message"])
            record = load_account_record(db_file, "strict@icloud.com")
            self.assertFalse(record.get("access_token"))

    async def test_worker_result_is_saved_without_exposing_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json, os\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "print(prefix + json.dumps({'type':'log','message':'started'}), flush=True)\n"
                "result = {'access_token':'at-test','session_json':json.dumps({'user':{'email':'one@icloud.com'}}),'storage_state_json':'{}'}\n"
                "print(prefix + json.dumps({'type':'result','result':result,'password':'Generated!A7'}), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            state = manager.start(
                [{"email": "one@icloud.com", "password": ""}],
                headless=True,
                concurrency=1,
            )
            self.assertTrue(state["running"])
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["succeeded"], 1)
            self.assertNotIn("_result", snapshot["accounts"][0])
            self.assertNotIn("access_token", json.dumps(snapshot))

            record = load_account_record(db_file, "one@icloud.com")
            self.assertEqual(record["access_token"], "at-test")
            self.assertEqual(record["password"], "Generated!A7")
            conn = connect_db(str(db_file))
            try:
                row = conn.execute(
                    "SELECT state FROM addresses WHERE email = ?",
                    ("one@icloud.com",),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row["state"], "used")

    async def test_partial_two_factor_state_is_saved_before_activation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json, sys\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "result = {'access_token':'at-partial','session_json':'{}'}\n"
                "print(prefix + json.dumps({'type':'account_registered','result':result,'password':'Strong!Pass123','password_confirmed':True}), flush=True)\n"
                "two_factor = {'enabled':False,'status':'enrolled','secret':'JBSWY3DPEHPK3PXP','factor_id':'factor-1','session_id':'session-1'}\n"
                "print(prefix + json.dumps({'type':'two_factor_enrolled','two_factor':two_factor}), flush=True)\n"
                "print(prefix + json.dumps({'type':'error','error':'activation failed','password':'Strong!Pass123'}), flush=True)\n"
                "sys.exit(1)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            manager.start(
                [
                    {
                        "email": "partial@icloud.com",
                        "password": "Strong!Pass123",
                        "enable_2fa": True,
                    }
                ],
                headless=True,
                concurrency=1,
            )
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["succeeded"], 1)
            self.assertEqual(snapshot["failed"], 0)
            self.assertIn("2FA 待开启", snapshot["accounts"][0]["message"])
            self.assertNotIn("JBSWY3D", json.dumps(snapshot))
            record = load_account_record(db_file, "partial@icloud.com")
            self.assertEqual(record["access_token"], "at-partial")
            self.assertEqual(record["two_factor"]["status"], "enrolled")
            self.assertEqual(record["two_factor"]["secret"], "JBSWY3DPEHPK3PXP")


if __name__ == "__main__":
    unittest.main()
