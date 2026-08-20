import unittest
from types import SimpleNamespace

from hidemyemail_generator.registration_activity import (
    CLICK_RESPONSE_SECONDS,
    MAX_NO_RESPONSE_CLICK_ATTEMPTS,
    begin_registration_step,
    configure_request_driven_registration,
    ensure_registration_activity_monitor,
    finalize_registration_chain,
    mark_registration_chain,
    registration_activity_changed,
    registration_activity_snapshot,
    registration_chain_snapshot,
    wait_for_registration_activity,
)


class RegistrationActivityTests(unittest.TestCase):
    def test_activity_wait_is_silent_for_one_second_then_checks_once(self):
        handlers = {}
        waits = []

        class Page:
            url = "https://chatgpt.com/"

            def on(self, name, callback):
                handlers[name] = callback

            def evaluate(self, _script):
                return {
                    "readyState": "complete",
                    "email": False,
                    "password": False,
                    "otp": False,
                    "profile": False,
                    "inputCount": 0,
                    "buttonCount": 2,
                    "busyCount": 0,
                }

        class Request:
            url = "https://auth.openai.com/api/accounts/email-otp/send"
            method = "POST"
            resource_type = "fetch"

        page = Page()
        ensure_registration_activity_monitor(page)
        before = registration_activity_snapshot(page)

        def wait(_page, milliseconds):
            waits.append(milliseconds)
            handlers["request"](Request())

        result = wait_for_registration_activity(
            page,
            before,
            wait=wait,
        )

        self.assertEqual(CLICK_RESPONSE_SECONDS, 1.0)
        self.assertEqual(waits, [1000])
        self.assertTrue(result["changed"])
        self.assertEqual(result["reason"], "request")

    def test_irrelevant_static_request_does_not_count_as_step_response(self):
        handlers = {}

        class Page:
            url = "https://chatgpt.com/"

            def on(self, name, callback):
                handlers[name] = callback

            def evaluate(self, _script):
                return {}

        class Request:
            url = "https://cdn.example.com/app.css"
            method = "GET"
            resource_type = "stylesheet"

        page = Page()
        ensure_registration_activity_monitor(page)
        handlers["request"](Request())

        self.assertEqual(registration_activity_snapshot(page)["requestCount"], 0)

    def test_background_session_response_is_not_registration_entry_activity(self):
        handlers = {}

        class Page:
            url = "https://chatgpt.com/"

            def on(self, name, callback):
                handlers[name] = callback

            def evaluate(self, _script):
                return {}

        request = SimpleNamespace(
            url="https://chatgpt.com/api/auth/session?token=secret",
            method="GET",
            resource_type="fetch",
        )
        response = SimpleNamespace(request=request, status=200)
        page = Page()
        ensure_registration_activity_monitor(page)
        before = registration_activity_snapshot(page)

        def wait(_page, _milliseconds):
            handlers["request"](request)
            handlers["response"](response)

        result = wait_for_registration_activity(
            page,
            before,
            wait=wait,
            signal="registration_entry",
        )

        self.assertFalse(result["changed"])
        self.assertTrue(result["ignoredActivity"])
        self.assertEqual(result["ignoredReason"], "request")
        self.assertEqual(result["activity"]["entryResponseCount"], 0)
        self.assertEqual(
            result["ignoredEvidence"]["route"],
            "chatgpt.com/api/auth/session",
        )
        self.assertEqual(result["ignoredEvidence"]["status"], 200)

    def test_auth_document_response_is_correlated_and_sanitized(self):
        handlers = {}

        class Page:
            url = "https://chatgpt.com/"

            def on(self, name, callback):
                handlers[name] = callback

            def evaluate(self, _script):
                return {}

        request = SimpleNamespace(
            url=(
                "https://auth.openai.com/create-account"
                "?state=secret&email=person@example.com"
            ),
            method="GET",
            resource_type="document",
        )
        response = SimpleNamespace(request=request, status=302)
        page = Page()
        ensure_registration_activity_monitor(page)
        before = registration_activity_snapshot(page)

        def wait(_page, _milliseconds):
            handlers["request"](request)
            handlers["response"](response)

        result = wait_for_registration_activity(
            page,
            before,
            wait=wait,
            signal="registration_entry",
        )

        self.assertTrue(result["changed"])
        self.assertEqual(result["reason"], "registration_entry_response")
        self.assertEqual(
            result["evidence"],
            {
                "event": "response",
                "method": "GET",
                "route": "auth.openai.com/create-account",
                "resourceType": "document",
                "status": 302,
                "at": result["evidence"]["at"],
            },
        )
        self.assertNotIn("secret", result["evidence"]["route"])
        self.assertNotIn("person@example.com", result["evidence"]["route"])

    def test_new_request_clears_stale_response_status(self):
        handlers = {}

        class Page:
            url = "https://chatgpt.com/"

            def on(self, name, callback):
                handlers[name] = callback

            def evaluate(self, _script):
                return {}

        page = Page()
        ensure_registration_activity_monitor(page)
        first = SimpleNamespace(
            url="https://chatgpt.com/api/first",
            method="POST",
            resource_type="fetch",
        )
        handlers["response"](SimpleNamespace(request=first, status=200))
        second = SimpleNamespace(
            url="https://chatgpt.com/api/second",
            method="POST",
            resource_type="fetch",
        )
        handlers["request"](second)

        snapshot = registration_activity_snapshot(page)

        self.assertEqual(snapshot["lastRoute"], "chatgpt.com/api/second")
        self.assertEqual(snapshot["lastStatus"], 0)
        self.assertEqual(snapshot["lastEvent"], "request")

    def test_registration_entry_dom_signal_ignores_noise_but_accepts_email(self):
        before = (
            "chatgpt.com",
            "/",
            "complete",
            False,
            False,
            False,
            False,
            0,
            2,
            0,
        )
        noisy = (*before[:8], 3, 1)
        email = (*before[:3], True, *before[4:])

        self.assertEqual(
            registration_activity_changed(
                {"dom": before},
                {"dom": noisy},
                signal="registration_entry",
            ),
            (False, ""),
        )
        self.assertEqual(
            registration_activity_changed(
                {"dom": before},
                {"dom": email},
                signal="registration_entry",
            ),
            (True, "page"),
        )

    def test_network_signal_ignores_late_load_and_dom_reflow(self):
        before = {
            "requestCount": 2,
            "responseCount": 2,
            "failedCount": 0,
            "loadCount": 0,
            "dom": ("auth.openai.com", "/email-verification", "loading", False),
        }
        css_finished = {
            **before,
            "loadCount": 1,
            "dom": ("auth.openai.com", "/email-verification", "complete", False),
        }
        auth_requested = {**css_finished, "requestCount": 3}

        self.assertEqual(
            registration_activity_changed(before, css_finished, signal="network"),
            (False, ""),
        )
        self.assertEqual(
            registration_activity_changed(
                css_finished,
                auth_requested,
                signal="network",
            ),
            (True, "request"),
        )

    def test_step_ledger_blocks_next_step_until_current_is_completed(self):
        emitted = []
        worker = SimpleNamespace(
            _hme_chain_require_password=True,
            _hme_chain_enable_two_factor=True,
            _hme_registration_chain_emitter=emitted.append,
        )

        begin_registration_step(
            worker,
            "site_requested",
            value="正在等待 document 请求",
        )
        running = registration_chain_snapshot(worker)

        self.assertEqual(running["currentCode"], "site_requested")
        self.assertEqual(running["currentValue"], "正在等待 document 请求")
        self.assertFalse(running["currentCompleted"])
        self.assertFalse(running["canAdvance"])
        self.assertEqual(running["nextCode"], "site_requested")
        with self.assertRaisesRegex(RuntimeError, "步骤顺序错误"):
            begin_registration_step(worker, "site_loaded")

        mark_registration_chain(
            worker,
            "site_requested",
            detail="检测到 ChatGPT document 请求",
        )
        begin_registration_step(
            worker,
            "site_loaded",
            value="等待页面 load 完成",
        )
        current = registration_chain_snapshot(worker)

        self.assertEqual(current["currentCode"], "site_loaded")
        self.assertFalse(current["currentCompleted"])
        self.assertEqual(current["steps"][0]["status"], "completed")
        self.assertEqual(current["steps"][1]["status"], "running")
        self.assertTrue(emitted)

    def test_existing_session_marks_unseen_steps_as_skipped_before_session(self):
        emitted = []

        class Worker:
            existing_login_only = True

            def _register(self, _page, _context, **_kwargs):
                return None

            def _has_chatgpt_session(self, _page):
                return True

        class Page:
            url = "https://chatgpt.com/"

            def on(self, _name, _callback):
                return None

            def evaluate(self, _script):
                return {}

        worker = Worker()
        self.assertTrue(
            configure_request_driven_registration(
                worker,
                emit_state=emitted.append,
                require_password=False,
                enable_two_factor=False,
            )
        )
        page = Page()
        self.assertTrue(worker._has_chatgpt_session(page))
        snapshot = registration_chain_snapshot(worker, page)

        self.assertTrue(snapshot["sessionReady"])
        skipped = {
            step["code"]
            for step in snapshot["steps"]
            if step["status"] == "skipped"
        }
        self.assertIn("site_requested", skipped)
        self.assertIn("verification_page", skipped)

    def test_password_and_two_factor_finish_one_complete_registration_chain(self):
        worker = SimpleNamespace(
            _hme_chain_require_password=True,
            _hme_chain_enable_two_factor=True,
            _hme_registration_chain_emitter=lambda _state: None,
        )
        through_session = (
            "site_requested",
            "site_loaded",
            "registration_clicked",
            "registration_entry_ready",
            "email_entered",
            "email_submitted",
            "email_responded",
            "verification_page",
            "verification_requested",
            "verification_code_received",
            "verification_code_entered",
            "verification_submitted",
            "registration_created",
            "profile_verified",
            "profile_submitted",
            "session_ready",
        )
        for code in through_session:
            begin_registration_step(worker, code, value=f"running:{code}")
            mark_registration_chain(worker, code, detail=f"done:{code}")

        complete = finalize_registration_chain(
            worker,
            password_confirmed=True,
            two_factor_enabled=True,
        )

        self.assertEqual(complete["status"], "success")
        self.assertTrue(complete["registrationCreated"])
        self.assertTrue(complete["sessionReady"])
        self.assertTrue(complete["passwordConfirmed"])
        self.assertTrue(complete["twoFactorEnabled"])
        self.assertTrue(complete["fullRegistrationComplete"])
        self.assertEqual(complete["nextCode"], "complete")
        self.assertTrue(all(step["completed"] for step in complete["steps"]))

    def test_click_retry_limit_is_five(self):
        self.assertEqual(MAX_NO_RESPONSE_CLICK_ATTEMPTS, 5)

    def test_otp_submit_has_no_one_second_focus_gap_after_stable_fill(self):
        waits = []
        submitted_values = []

        class Field:
            def __init__(self):
                self.value = ""

            @staticmethod
            def evaluate(_script):
                return "stable-otp-field"

            def input_value(self, **_kwargs):
                return self.value

            def fill(self, value):
                self.value = value

        class Page:
            url = "https://auth.openai.com/email-verification"

            def __init__(self):
                self.field = Field()

            @staticmethod
            def on(_name, _callback):
                return None

            @staticmethod
            def evaluate(_script):
                return {}

            @staticmethod
            def locator(_selector):
                return SimpleNamespace(count=lambda: 0)

            @staticmethod
            def wait_for_timeout(milliseconds):
                waits.append(milliseconds)

        class Worker:
            def __init__(self, page):
                self.page = page
                self.logs = []

            @staticmethod
            def _register(_page, _context, **_kwargs):
                return None

            def _visible_inputs(self, _page, _selectors):
                return [self.page.field]

            def _validate_email_code_api(self, _page, _code):
                submitted_values.append(self.page.field.value)
                return "submitted"

            def log(self, message):
                self.logs.append(message)

        page = Page()
        worker = Worker(page)
        self.assertTrue(
            configure_request_driven_registration(
                worker,
                emit_state=lambda _state: None,
                require_password=False,
                enable_two_factor=False,
            )
        )
        for code in (
            "site_requested",
            "site_loaded",
            "registration_clicked",
            "registration_entry_ready",
            "email_entered",
            "email_submitted",
            "email_responded",
            "verification_page",
            "verification_requested",
            "verification_code_received",
        ):
            begin_registration_step(worker, code, page=page)
            mark_registration_chain(worker, code, page=page)

        result = worker._validate_email_code_api(page, "123456")

        self.assertEqual(result, "submitted")
        self.assertEqual(submitted_values, ["123456"])
        self.assertNotIn(1000, waits)
        self.assertTrue(waits)
        self.assertLessEqual(max(waits), 200)


if __name__ == "__main__":
    unittest.main()
