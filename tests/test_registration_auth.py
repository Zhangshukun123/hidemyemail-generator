import threading
import unittest
from unittest.mock import patch

from hidemyemail_generator import openai_registration_navigation
from hidemyemail_generator.registration_auth import (
    CHATGPT_HOME_LOGIN_SELECTORS,
    CHATGPT_HOME_SIGNUP_SELECTORS,
    CHATGPT_HOME_INTERACTIVE_SELECTOR,
    OPENAI_EMAIL_LOGIN_INPUT_SELECTORS,
    OPENAI_EMAIL_LOGIN_SUBMIT_SELECTORS,
    OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS,
    OPENAI_EMAIL_SUBMIT_SELECTORS,
    click_email_submit,
    click_chatgpt_home_login,
    click_chatgpt_home_signup,
    is_chatgpt_auth_entry_url,
    paste_email_and_submit,
)


class RegistrationAuthTests(unittest.TestCase):
    def test_chatgpt_direct_auth_entry_url_is_recognized(self):
        self.assertTrue(
            is_chatgpt_auth_entry_url("https://chatgpt.com/auth/login")
        )
        self.assertTrue(
            is_chatgpt_auth_entry_url(
                "https://chatgpt.com/auth/create-account?locale=ja-JP"
            )
        )
        self.assertFalse(is_chatgpt_auth_entry_url("https://chatgpt.com/"))
        self.assertFalse(
            is_chatgpt_auth_entry_url("https://accounts.google.com/login")
        )

    def test_selectors_never_include_signup_entry(self):
        normalized = " ".join(CHATGPT_HOME_LOGIN_SELECTORS).casefold()

        self.assertNotIn("sign up", normalized)
        self.assertNotIn("免费注册", normalized)

    def test_registration_email_submit_never_contains_login_controls(self):
        normalized = " ".join(
            OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS
        ).casefold()

        for label in ("log in", "登录", "登入", "ログイン"):
            self.assertNotIn(label, normalized)
        self.assertNotIn('button[type="submit"]', normalized)
        self.assertTrue(
            any(
                ':is(form, [role="dialog"]):has(' in selector
                for selector in OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS
            )
        )
        self.assertTrue(
            any(
                selector.endswith('button:text-is("続行")')
                for selector in OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS
            )
        )

    def test_home_auth_drawer_email_placeholder_and_japanese_continue_are_supported(self):
        normalized_inputs = " ".join(OPENAI_EMAIL_LOGIN_INPUT_SELECTORS)
        normalized_submits = " ".join(OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS)

        self.assertIn('input[placeholder="Email address" i]', normalized_inputs)
        self.assertIn('input[placeholder*="メールアドレス"]', normalized_inputs)
        self.assertIn("aside", normalized_submits)
        self.assertIn('button:text-is("続行")', normalized_submits)

    def test_japanese_home_auth_drawer_fills_email_and_clicks_continue(self):
        events = []
        clipboard = {"value": ""}

        class EmailInput:
            value = ""

            def click(self, **_kwargs):
                events.append("email-focus")

            def press(self, key, **_kwargs):
                events.append(key)
                if key == "Control+A":
                    self.value = ""
                elif key == "Control+V":
                    self.value = clipboard["value"]

            def input_value(self, **_kwargs):
                return self.value

        class ContinueButton:
            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                events.append("続行-click")

        page = type(
            "Page",
            (),
            {"url": "https://chatgpt.com/"},
        )()
        email_input = EmailInput()
        continue_button = ContinueButton()

        def first_visible(_page, selectors, **_kwargs):
            if any("aside" in selector and "続行" in selector for selector in selectors):
                return continue_button
            return None

        def clipboard_write(value):
            clipboard["value"] = value
            events.append(("clipboard", value))

        paste_email_and_submit(
            page,
            email_input,
            "drawer@example.com",
            log=lambda message: events.append(("log", message)),
            activate=lambda _page: events.append("activate"),
            wait=lambda _page, _milliseconds: None,
            first_visible=first_visible,
            clipboard_write=clipboard_write,
            clipboard_lock=threading.Lock(),
            submit_selectors=OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS,
            submit_allowed_labels=("Continue", "继续", "続行"),
            allow_enter_submit=False,
        )

        self.assertEqual(email_input.value, "drawer@example.com")
        self.assertIn("続行-click", events)
        self.assertNotIn("Enter", events)

    def test_existing_login_submit_still_contains_login_controls(self):
        normalized = " ".join(OPENAI_EMAIL_LOGIN_SUBMIT_SELECTORS).casefold()

        self.assertIn("log in", normalized)
        self.assertIn("登录", normalized)
        self.assertIs(OPENAI_EMAIL_SUBMIT_SELECTORS, OPENAI_EMAIL_LOGIN_SUBMIT_SELECTORS)

    def test_registration_submission_clicks_only_strict_continue_without_enter(self):
        events = []
        clipboard = {"value": ""}

        class Input:
            value = ""

            def click(self, **_kwargs):
                events.append("focus")

            def press(self, key, **_kwargs):
                events.append(key)
                if key == "Control+A":
                    self.value = ""
                elif key == "Control+V":
                    self.value = clipboard["value"]

            def input_value(self, **_kwargs):
                return self.value

        class Submit:
            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                events.append("registration-submit")

        observed_selectors = []

        def first_visible(_page, selectors, **_kwargs):
            observed_selectors.extend(selectors)
            if selectors[0] == OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS[0]:
                return Submit()
            return None

        logs = []
        paste_email_and_submit(
            object(),
            Input(),
            "new-account@example.com",
            log=logs.append,
            activate=lambda _page: events.append("activate"),
            wait=lambda _page, milliseconds: events.append(("wait", milliseconds)),
            first_visible=first_visible,
            clipboard_write=lambda value: clipboard.update(value=value),
            clipboard_lock=threading.RLock(),
            submit_selectors=OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS,
            allow_enter_submit=False,
            submit_diagnostic_message="registration-only",
        )

        self.assertNotIn("Enter", events)
        self.assertIn("registration-submit", events)
        self.assertTrue(
            set(observed_selectors).issubset(
                set(OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS)
            )
        )
        self.assertTrue(any("registration-only" in line for line in logs))

    def test_existing_login_submit_can_click_login_control(self):
        login_selector = next(
            selector
            for selector in OPENAI_EMAIL_LOGIN_SUBMIT_SELECTORS
            if 'Log in' in selector
        )
        events = []

        class Submit:
            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                events.append("login-submit")

        self.assertTrue(
            click_email_submit(
                object(),
                first_visible=lambda _page, selectors, **_kwargs: (
                    Submit() if selectors == (login_selector,) else None
                ),
                submit_selectors=OPENAI_EMAIL_LOGIN_SUBMIT_SELECTORS,
            )
        )
        self.assertEqual(events, ["login-submit"])

    def test_registration_semantic_fallback_clicks_continue_below_email_only(self):
        events = []

        class Candidate:
            def __init__(self, label, box):
                self.label = label
                self.box = box

            def is_visible(self, **_kwargs):
                return True

            def inner_text(self, **_kwargs):
                return self.label

            def text_content(self, **_kwargs):
                return self.label

            def get_attribute(self, _name, **_kwargs):
                return ""

            def bounding_box(self, **_kwargs):
                return self.box

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                events.append(self.label)

        class Collection:
            def __init__(self):
                self.items = [
                    Candidate(
                        "電話番号で続行",
                        {"x": 100, "y": 100, "width": 300, "height": 50},
                    ),
                    Candidate(
                        "ログイン",
                        {"x": 700, "y": 20, "width": 100, "height": 40},
                    ),
                    Candidate(
                        "続行",
                        {"x": 100, "y": 360, "width": 300, "height": 50},
                    ),
                ]

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        class Page:
            def locator(self, selector):
                self.assert_selector = selector
                return Collection()

        class EmailInput:
            def bounding_box(self, **_kwargs):
                return {"x": 100, "y": 280, "width": 300, "height": 50}

        page = Page()
        self.assertTrue(
            click_email_submit(
                page,
                first_visible=lambda *_args, **_kwargs: None,
                submit_selectors=OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS,
                allowed_labels=("Continue", "继续", "続行"),
                anchor_input=EmailInput(),
            )
        )
        self.assertEqual(events, ["続行"])

    def test_email_submission_is_click_paste_verify_then_submit(self):
        events = []
        clipboard = {"value": ""}

        class Input:
            value = ""

            def click(self, **_kwargs):
                events.append("focus")

            def press(self, key, **_kwargs):
                events.append(key)
                if key == "Control+A":
                    self.value = ""
                elif key == "Control+V":
                    self.value = clipboard["value"]

            def input_value(self, **_kwargs):
                return self.value

        class Submit:
            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                events.append("submit")

        def copy(value):
            clipboard["value"] = value
            events.append(("clipboard", value))

        logs = []
        paste_email_and_submit(
            object(),
            Input(),
            "person@example.com",
            log=logs.append,
            activate=lambda _page: events.append("activate"),
            wait=lambda _page, milliseconds: events.append(("wait", milliseconds)),
            first_visible=lambda *_args, **_kwargs: Submit(),
            clipboard_write=copy,
            clipboard_lock=threading.RLock(),
        )

        self.assertEqual(
            events,
            [
                "activate",
                "focus",
                "Control+A",
                ("clipboard", "person@example.com"),
                "Control+V",
                ("wait", 250),
                ("clipboard", ""),
                "activate",
                "Enter",
                ("wait", 500),
                ("wait", 500),
                ("wait", 500),
                "submit",
                ("wait", 400),
            ],
        )
        self.assertTrue(any(line.startswith("[AUTH_EMAIL_FOCUS]") for line in logs))
        self.assertTrue(any(line.startswith("[AUTH_EMAIL_PASTE]") for line in logs))
        self.assertTrue(any(line.startswith("[AUTH_EMAIL_SUBMIT]") for line in logs))

    def test_headless_email_submission_falls_back_to_dom_fill(self):
        events = []
        clipboard = {"value": ""}

        class Input:
            value = ""

            def click(self, **_kwargs):
                return None

            def press(self, key, **_kwargs):
                events.append(key)
                if key == "Control+A":
                    self.value = ""

            def fill(self, value, **_kwargs):
                events.append("dom-fill")
                self.value = value

            def input_value(self, **_kwargs):
                return self.value

        logs = []
        paste_email_and_submit(
            object(),
            Input(),
            "headless@example.com",
            log=logs.append,
            activate=lambda _page: None,
            wait=lambda _page, _milliseconds: None,
            first_visible=lambda *_args, **_kwargs: None,
            clipboard_write=lambda value: clipboard.update(value=value),
            clipboard_lock=threading.RLock(),
        )

        self.assertIn("dom-fill", events)
        self.assertIn("Enter", events)
        self.assertTrue(any("DOM 填写并校验" in line for line in logs))

    def test_email_submission_force_focuses_and_fills_obscured_mobile_input(self):
        events = []

        class Input:
            value = ""

            def click(self, **kwargs):
                events.append(("click", bool(kwargs.get("force"))))
                if not kwargs.get("force"):
                    raise RuntimeError("element is not stable")

            def press(self, key, **_kwargs):
                events.append(key)
                if key == "Control+A":
                    raise RuntimeError("keyboard focus unavailable")

            def fill(self, value, **kwargs):
                events.append(("fill", bool(kwargs.get("force"))))
                self.value = value

            def input_value(self, **_kwargs):
                return self.value

        logs = []
        paste_email_and_submit(
            object(),
            Input(),
            "mobile@example.com",
            log=logs.append,
            activate=lambda _page: None,
            wait=lambda _page, _milliseconds: None,
            first_visible=lambda *_args, **_kwargs: None,
            clipboard_write=lambda _value: None,
            clipboard_lock=threading.RLock(),
        )

        self.assertEqual(
            events[:4],
            [("click", False), ("click", True), "Control+A", ("fill", True)],
        )
        self.assertIn("Enter", events)
        self.assertTrue(any(line.startswith("[AUTH_EMAIL_PASTE]") for line in logs))

    def test_email_submit_uses_dom_click_when_mobile_button_is_obscured(self):
        events = []

        class Candidate:
            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **kwargs):
                events.append(("click", bool(kwargs.get("force"))))
                raise RuntimeError("button is obscured")

            def evaluate(self, _script):
                events.append("dom-click")

            def inner_text(self, **_kwargs):
                return "続行"

        candidate = Candidate()
        self.assertTrue(
            click_email_submit(
                object(),
                first_visible=lambda *_args, **_kwargs: candidate,
                submit_selectors=("button[type=submit]",),
            )
        )
        self.assertEqual(events, [("click", False), ("click", True), "dom-click"])

    def test_home_entry_activates_background_tab_before_both_click_attempts(self):
        events = []

        class Candidate:
            clicks = 0

            def is_enabled(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                self.clicks += 1
                events.append(("click", self.clicks))
                if self.clicks == 2:
                    page.url = "https://auth.openai.com/log-in"

        class Page:
            url = "https://chatgpt.com/"

            def wait_for_load_state(self, _state, **_kwargs):
                return None

        page = Page()
        candidate = Candidate()

        def first_visible(_page, selectors, **_kwargs):
            if selectors == CHATGPT_HOME_LOGIN_SELECTORS:
                return candidate
            if selectors == OPENAI_EMAIL_LOGIN_INPUT_SELECTORS:
                return None
            return None

        self.assertTrue(
            click_chatgpt_home_login(
                page,
                lambda _message: None,
                first_visible=first_visible,
                wait=lambda _page, _milliseconds: None,
                activate=lambda _page: events.append("activate"),
                timeout_seconds=0.01,
                transition_timeout_seconds=0.01,
            )
        )
        self.assertEqual(
            events,
            ["activate", ("click", 1), "activate", ("click", 2)],
        )

    def test_home_entry_reloads_once_when_signup_control_is_missing(self):
        events = []

        class Candidate:
            def is_enabled(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                events.append("click")
                page.url = "https://auth.openai.com/create-account"

        class Page:
            url = "https://chatgpt.com/"
            reloaded = False

            def wait_for_load_state(self, _state, **_kwargs):
                return None

            def reload(self, **_kwargs):
                self.reloaded = True
                events.append("reload")

        page = Page()
        candidate = Candidate()

        def first_visible(_page, selectors, **_kwargs):
            if page.reloaded and selectors == CHATGPT_HOME_LOGIN_SELECTORS:
                return candidate
            return None

        self.assertTrue(
            click_chatgpt_home_login(
                page,
                lambda _message: None,
                first_visible=first_visible,
                wait=lambda _page, _milliseconds: None,
                activate=lambda _page: events.append("activate"),
                timeout_seconds=0.01,
                transition_timeout_seconds=0.01,
            )
        )
        self.assertEqual(events, ["activate", "reload", "activate", "click"])

    def test_home_signup_uses_semantic_fallback_for_current_japanese_label(self):
        events = []

        class Candidate:
            def is_visible(self, **_kwargs):
                return True

            def is_enabled(self, **_kwargs):
                return True

            def inner_text(self, **_kwargs):
                return "無料で登録"

            def text_content(self, **_kwargs):
                return "無料で登録"

            def get_attribute(self, name, **_kwargs):
                if name == "href":
                    return "/auth/login?auth_flow=signup"
                return ""

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **_kwargs):
                events.append("signup-click")
                page.url = "https://auth.openai.com/create-account"

        candidate = Candidate()

        class Collection:
            def count(self):
                return 1

            def nth(self, index):
                if index != 0:
                    raise AssertionError(index)
                return candidate

        class Page:
            url = "https://chatgpt.com/"

            def wait_for_load_state(self, _state, **_kwargs):
                return None

            def locator(self, selector):
                self.observed_selector = selector
                return Collection()

        page = Page()
        logs = []
        self.assertTrue(
            click_chatgpt_home_signup(
                page,
                logs.append,
                first_visible=lambda *_args, **_kwargs: None,
                wait=lambda _page, _milliseconds: None,
                timeout_seconds=0.01,
                transition_timeout_seconds=0.01,
            )
        )
        self.assertEqual(events, ["signup-click"])
        self.assertEqual(
            page.observed_selector,
            CHATGPT_HOME_INTERACTIVE_SELECTOR,
        )
        self.assertTrue(any("可见控件文字或链接识别" in line for line in logs))

    def test_home_signup_uses_dom_click_when_pointer_click_is_intercepted(self):
        events = []

        class Candidate:
            def is_enabled(self, **_kwargs):
                return True

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **kwargs):
                events.append(("click", bool(kwargs.get("force"))))
                raise RuntimeError("pointer click intercepted")

            def evaluate(self, _script):
                events.append("dom-click")
                page.url = "https://auth.openai.com/create-account"

        class Page:
            url = "https://chatgpt.com/"

            def wait_for_load_state(self, _state, **_kwargs):
                return None

        page = Page()
        candidate = Candidate()

        def first_visible(_page, selectors, **_kwargs):
            if selectors == CHATGPT_HOME_SIGNUP_SELECTORS:
                return candidate
            if selectors == OPENAI_EMAIL_LOGIN_INPUT_SELECTORS:
                return None
            return None

        self.assertTrue(
            click_chatgpt_home_signup(
                page,
                lambda _message: None,
                first_visible=first_visible,
                wait=lambda _page, _milliseconds: None,
                timeout_seconds=0.01,
                transition_timeout_seconds=0.01,
            )
        )
        self.assertEqual(events, [("click", False), ("click", True), "dom-click"])

    def test_home_signup_semantic_fallback_does_not_click_login(self):
        events = []

        class Candidate:
            def is_visible(self, **_kwargs):
                return True

            def is_enabled(self, **_kwargs):
                return True

            def inner_text(self, **_kwargs):
                return "ログイン"

            def text_content(self, **_kwargs):
                return "ログイン"

            def get_attribute(self, _name, **_kwargs):
                return ""

            def click(self, **_kwargs):
                events.append("login-click")

        class Collection:
            def count(self):
                return 1

            def nth(self, _index):
                return Candidate()

        class Page:
            url = "https://chatgpt.com/"
            reloads = 0

            def wait_for_load_state(self, _state, **_kwargs):
                return None

            def locator(self, _selector):
                return Collection()

            def reload(self, **_kwargs):
                self.reloads += 1

        page = Page()
        with self.assertRaisesRegex(RuntimeError, "免费注册"):
            click_chatgpt_home_signup(
                page,
                lambda _message: None,
                first_visible=lambda *_args, **_kwargs: None,
                wait=lambda _page, _milliseconds: None,
                timeout_seconds=0.01,
                transition_timeout_seconds=0.01,
            )
        self.assertEqual(events, [])
        self.assertEqual(page.reloads, 1)

    def test_home_email_modal_waits_for_slow_network_without_reclicking(self):
        clock = [0.0]
        logs = []
        interactions = []

        class Page:
            url = "https://chatgpt.com/"

            @staticmethod
            def reload(**_kwargs):
                interactions.append("reload")

            @staticmethod
            def click(**_kwargs):
                interactions.append("click")

        def first_visible(_page, selectors, **_kwargs):
            if selectors == OPENAI_EMAIL_LOGIN_INPUT_SELECTORS:
                return object()
            if (
                selectors
                == openai_registration_navigation.HOME_EMAIL_MODAL_PROGRESS_SELECTORS
                and clock[0] >= 45.0
            ):
                return object()
            return None

        def wait(_page, milliseconds):
            self.assertEqual(milliseconds, 250)
            clock[0] += milliseconds / 1000

        with patch.object(
            openai_registration_navigation.time,
            "monotonic",
            side_effect=lambda: clock[0],
        ):
            self.assertTrue(
                openai_registration_navigation._wait_for_home_email_modal_transition(
                    Page(),
                    logs.append,
                    first_visible=first_visible,
                    wait=wait,
                )
            )

        self.assertGreaterEqual(clock[0], 45.0)
        self.assertEqual(interactions, [])
        self.assertTrue(any("已等待 10 秒" in line for line in logs))
        self.assertTrue(any("不刷新、不重复点击" in line for line in logs))
        self.assertIn("正在继续认证流程", logs[-1])

    def test_home_email_modal_times_out_after_120_seconds_without_refresh(self):
        clock = [0.0]
        logs = []

        class Page:
            url = "https://chatgpt.com/"

        def first_visible(_page, selectors, **_kwargs):
            if selectors == OPENAI_EMAIL_LOGIN_INPUT_SELECTORS:
                return object()
            return None

        def wait(_page, milliseconds):
            clock[0] += milliseconds / 1000

        with patch.object(
            openai_registration_navigation.time,
            "monotonic",
            side_effect=lambda: clock[0],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "120 秒内没有变化.*未刷新或重复点击",
            ):
                openai_registration_navigation._wait_for_home_email_modal_transition(
                    Page(),
                    logs.append,
                    first_visible=first_visible,
                    wait=wait,
                )

        self.assertGreaterEqual(clock[0], 120.0)
        self.assertTrue(any("已等待 110 秒" in line for line in logs))

    def test_home_navigation_activates_tab_before_and_after_goto(self):
        events = []

        class Page:
            def __init__(self):
                self.url = "about:blank"

            def goto(self, url, **_kwargs):
                events.append(("goto", url))
                self.url = url
                return "response"

        class Worker:
            existing_login_only = False

            def __init__(self):
                self.logs = []

            def log(self, message):
                self.logs.append(message)

            def _register(self, page, _context, **_kwargs):
                return page.goto("https://chatgpt.com/")

            def _create_openai_signin_url(self, _context):
                return "https://auth.openai.com/create-account"

            def _create_login_url(self, _context):
                return "https://auth.openai.com/log-in"

            def _goto_auth_page(self, page, url):
                return page.goto(url)

        worker = Worker()
        page = Page()

        def activate(_worker, target_page):
            events.append(("activate", target_page.url))
            return True

        def click_signup(target_page, _worker, **_kwargs):
            events.append(("click", target_page.url))
            target_page.url = "https://auth.openai.com/create-account"
            return True

        with patch.object(
            openai_registration_navigation,
            "_click_chatgpt_home_signup",
            side_effect=click_signup,
        ):
            self.assertTrue(
                openai_registration_navigation.configure_chatgpt_home_login_entry(
                    worker,
                    activate_page=activate,
                )
            )
            self.assertEqual(worker._register(page, object()), "response")

        self.assertEqual(
            events[:4],
            [
                ("activate", "about:blank"),
                ("goto", "https://chatgpt.com/"),
                ("activate", "https://chatgpt.com/"),
                ("click", "https://chatgpt.com/"),
            ],
        )

    def test_home_redirect_to_direct_auth_entry_fills_email_without_signup_click(self):
        events = []

        class Page:
            def __init__(self):
                self.url = "about:blank"
                self.email_visible = False

            def goto(self, url, **_kwargs):
                events.append(("goto", url))
                self.url = "https://chatgpt.com/auth/login"
                self.email_visible = True
                return "response"

        class Worker:
            existing_login_only = False

            def __init__(self):
                self.logs = []
                self.fill_calls = 0

            def log(self, message):
                self.logs.append(message)

            def _register(self, page, _context, **_kwargs):
                return page.goto("https://chatgpt.com/")

            def _create_openai_signin_url(self, _context):
                return "https://auth.openai.com/create-account"

            def _create_login_url(self, _context):
                return "https://auth.openai.com/log-in"

            def _goto_auth_page(self, page, url):
                return page.goto(url)

            def _fill_email_if_visible(self, page):
                self.fill_calls += 1
                page.email_visible = False
                return True

        worker = Worker()
        page = Page()

        def first_visible(target_page, selectors, **_kwargs):
            if (
                selectors == OPENAI_EMAIL_LOGIN_INPUT_SELECTORS
                and target_page.email_visible
            ):
                return object()
            return None

        with (
            patch.object(
                openai_registration_navigation,
                "_first_visible",
                side_effect=first_visible,
            ),
            patch.object(
                openai_registration_navigation,
                "_click_chatgpt_home_signup",
                side_effect=AssertionError(
                    "direct auth entry must skip homepage signup click"
                ),
            ),
        ):
            self.assertTrue(
                openai_registration_navigation.configure_chatgpt_home_login_entry(
                    worker,
                    activate_page=lambda _worker, _page: True,
                )
            )
            self.assertEqual(worker._register(page, object()), "response")

        self.assertEqual(worker.fill_calls, 1)
        self.assertEqual(events, [("goto", "https://chatgpt.com/")])
        self.assertTrue(any("直接跳入登录或新注册页" in line for line in worker.logs))
        self.assertTrue(any("跳过首页注册按钮" in line for line in worker.logs))

    def test_home_with_preopened_auth_drawer_fills_email_without_signup_click(self):
        events = []

        class Page:
            def __init__(self):
                self.url = "about:blank"
                self.email_visible = False

            def goto(self, url, **_kwargs):
                events.append(("goto", url))
                self.url = "https://chatgpt.com/"
                self.email_visible = True
                return "response"

        class Worker:
            existing_login_only = False

            def __init__(self):
                self.logs = []
                self.fill_calls = 0

            def log(self, message):
                self.logs.append(message)

            def _register(self, page, _context, **_kwargs):
                return page.goto("https://chatgpt.com/")

            def _create_openai_signin_url(self, _context):
                return "https://auth.openai.com/create-account"

            def _create_login_url(self, _context):
                return "https://auth.openai.com/log-in"

            def _goto_auth_page(self, page, url):
                return page.goto(url)

            def _fill_email_if_visible(self, page):
                self.fill_calls += 1
                page.email_visible = False
                return True

        worker = Worker()
        page = Page()

        def first_visible(target_page, selectors, **_kwargs):
            if (
                selectors == OPENAI_EMAIL_LOGIN_INPUT_SELECTORS
                and target_page.email_visible
            ):
                return object()
            return None

        with (
            patch.object(
                openai_registration_navigation,
                "_first_visible",
                side_effect=first_visible,
            ),
            patch.object(
                openai_registration_navigation,
                "_click_chatgpt_home_signup",
                side_effect=AssertionError(
                    "preopened auth drawer must skip homepage signup click"
                ),
            ),
        ):
            self.assertTrue(
                openai_registration_navigation.configure_chatgpt_home_login_entry(
                    worker,
                    activate_page=lambda _worker, _page: True,
                )
            )
            self.assertEqual(worker._register(page, object()), "response")

        self.assertEqual(worker.fill_calls, 1)
        self.assertEqual(events, [("goto", "https://chatgpt.com/")])
        self.assertTrue(any("预先打开登录或新注册抽屉" in line for line in worker.logs))
        self.assertTrue(any("跳过首页注册按钮" in line for line in worker.logs))

    def test_home_entry_only_blocks_the_initial_generated_auth_navigation(self):
        events = []

        class Page:
            def __init__(self):
                self.url = "about:blank"

            def goto(self, url, **_kwargs):
                events.append(("goto", url))
                self.url = url
                return url

        class Worker:
            existing_login_only = False

            def log(self, message):
                events.append(("log", message))

            def _register(self, page, context, **_kwargs):
                page.goto("https://chatgpt.com/")
                initial_url = self._create_openai_signin_url(context)
                self._goto_auth_page(page, initial_url)
                recovery_url = self._create_openai_signin_url(context)
                self._goto_auth_page(page, recovery_url)
                return recovery_url

            def _create_openai_signin_url(self, _context):
                events.append("create-fresh-auth-url")
                return "https://auth.openai.com/fresh-recovery"

            def _create_login_url(self, _context):
                return "https://auth.openai.com/log-in"

            def _goto_auth_page(self, page, url):
                events.append(("direct-auth-goto", url))
                return page.goto(url)

        worker = Worker()
        page = Page()

        def click_signup(target_page, _worker, **_kwargs):
            target_page.url = "https://auth.openai.com/create-account"
            return True

        with patch.object(
            openai_registration_navigation,
            "_click_chatgpt_home_signup",
            side_effect=click_signup,
        ):
            self.assertTrue(
                openai_registration_navigation.configure_chatgpt_home_login_entry(
                    worker,
                    activate_page=lambda _worker, _page: True,
                )
            )
            self.assertEqual(
                worker._register(page, object()),
                "https://auth.openai.com/fresh-recovery",
            )

        self.assertEqual(events.count("create-fresh-auth-url"), 1)
        self.assertEqual(
            [event for event in events if isinstance(event, tuple) and event[0] == "direct-auth-goto"],
            [("direct-auth-goto", "https://auth.openai.com/fresh-recovery")],
        )


if __name__ == "__main__":
    unittest.main()
