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
    access_token_is_expired,
    jwt_account_type,
    load_account_record,
    set_manual_account_type,
)
from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.openai_browser_bridge import (
    ADD_PASSWORD_SELECTORS,
    PROFILE_MENU_STRICT_SELECTORS,
    _click_add_password,
    _click_password_add_by_geometry,
    _click_profile_name_by_dom,
    _camoufox_window_layout,
    _configure_camoufox_runtime_cache,
    _dismiss_completed_onboarding,
    _click_first_visible,
    _fontconfig_generator_with_home,
    _mfa_token_was_invalidated,
    _open_settings_from_profile,
    configure_password_first_login,
    configure_direct_registration_browser,
    configure_post_registration_password_setup,
    configure_registration_profile_capture,
    configure_resilient_registration_navigation,
    configure_windowed_camoufox,
    ensure_password_in_security_settings,
    detect_direct_registration_location,
    extract_session_without_navigation,
    require_registration_proxy_country,
    resilient_force_fill_locator,
    safe_log_message,
)
from hidemyemail_generator.openai_mfa import MfaSetupError
from hidemyemail_generator.registration_proxy import RegistrationProxyStore


def token_with_exp(expires_at: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


class BrowserTaskHelperTests(unittest.TestCase):
    def test_registration_proxy_country_must_match_detected_exit(self):
        self.assertEqual(
            require_registration_proxy_country(SimpleNamespace(country="NL"), "NL"),
            "NL",
        )
        with self.assertRaisesRegex(RuntimeError, "NL"):
            require_registration_proxy_country(SimpleNamespace(country="BR"), "NL")

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

            def __init__(self, page):
                self.page = page

        class Collection:
            def __init__(self, items):
                self.items = items

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        class Page:
            def __init__(self):
                self.state = "otp"

            def locator(self, selector):
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

            def _continue_chatgpt_registration_complete(self, _page):
                self.continue_calls += 1
                return False

            def _has_otp_input(self, page):
                return page.state in {"otp", "post_password_otp"}

            def _fill_password_step(self, _page):
                self.original_calls += 1

            def log(self, message):
                self.logs.append(message)

        worker = Worker()
        page = Page()
        self.assertTrue(configure_password_first_login(worker, enabled=True))
        worker._continue_chatgpt_registration_complete(page)
        worker._fill_password_step(page)

        self.assertEqual(actions, ["password"])
        self.assertEqual(worker.continue_calls, 1)
        self.assertEqual(worker.original_calls, 1)
        self.assertTrue(worker._password_step_submitted)
        self.assertIn("唯一密码", worker.logs[-1])

        page.state = "post_password_otp"
        worker._continue_chatgpt_registration_complete(page)
        self.assertTrue(worker._has_otp_input(page))
        self.assertEqual(actions, ["password"])

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

        def ensure_password(
            _backend,
            target_worker,
            _password,
            *,
            context,
            force_reset_password=False,
        ):
            self.assertFalse(force_reset_password)
            events.append(("password", context))
            target_worker._password_step_submitted = True
            return True

        def extract_session(_worker, context):
            events.append(("session_api", context))
            return {"access_token": "at-test"}

        with (
            patch(
                "hidemyemail_generator.openai_browser_bridge."
                "ensure_password_in_security_settings",
                side_effect=ensure_password,
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
                "ensure_password_in_security_settings",
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

            def _extract_session_info(self, _context):
                self.fail("the original extractor must stay wrapped")

            def log(self, message):
                self.logs.append(message)

        page = Page()
        context = SimpleNamespace(pages=[page])
        worker = Worker()
        emitted = []
        session_results = [
            {"access_token": "at-registered"},
            {"access_token": "at-before-password"},
            {"access_token": "at-after-password"},
        ]
        mfa_tokens = []
        mfa_client = SimpleNamespace(close=lambda: None)

        def ensure_password(
            _backend,
            target_worker,
            _password,
            *,
            context,
            force_reset_password=False,
        ):
            self.assertIsNotNone(context)
            self.assertFalse(force_reset_password)
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
                "ensure_password_in_security_settings",
                side_effect=ensure_password,
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
        self.assertEqual(page.goto_calls, ["https://chatgpt.com/"])
        self.assertTrue(worker._hme_two_factor_completed)
        self.assertIn("two_factor_start", [kind for kind, _payload in emitted])
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

    def test_camoufox_bridge_forces_non_fullscreen_window(self):
        calls = []

        def original(playwright, *args, **kwargs):
            calls.append((playwright, args, kwargs))
            return "browser"

        backend = SimpleNamespace(CamoufoxNewBrowser=original)

        self.assertTrue(configure_windowed_camoufox(backend))
        self.assertTrue(configure_windowed_camoufox(backend))
        self.assertEqual(backend.CamoufoxNewBrowser("playwright"), "browser")
        self.assertEqual(calls[0][2]["window"], (1280, 800))
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

        backend.CamoufoxNewBrowser(
            "playwright",
            window=(1024, 700),
            firefox_user_prefs={"browser.cache.disk.enable": False},
        )
        self.assertEqual(calls[1][2]["window"], (1024, 700))
        self.assertFalse(
            calls[1][2]["firefox_user_prefs"]["browser.cache.disk.enable"]
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
            _camoufox_window_layout(index, 3, screen_size=(3200, 1800))
            for index in range(3)
        ]

        self.assertEqual([item["slot"] for item in layouts], [0, 1, 2])
        self.assertEqual(len({item["x"] for item in layouts}), 3)
        self.assertTrue(all(item["width"] >= 1000 for item in layouts))

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


class BrowserTaskManagerTests(unittest.IsolatedAsyncioTestCase):
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
            self.assertIn("密码待设置", snapshot["accounts"][0]["message"])
            self.assertFalse(snapshot["accounts"][0]["passwordConfirmed"])
            record = load_account_record(db_file, "passwordless@icloud.com")
            self.assertEqual(record["access_token"], "at-test")
            self.assertEqual(record["password"], "LocalOnly!A7")
            self.assertFalse(record["password_confirmed"])

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
