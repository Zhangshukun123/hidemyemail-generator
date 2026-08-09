import unittest

from hidemyemail_generator.browser_diagnostics import (
    BrowserDiagnostic,
    BrowserDiagnosticCode,
    browser_diagnostic_context,
    emit_browser_diagnostic,
)


class BrowserDiagnosticTests(unittest.TestCase):
    def test_rendered_code_is_stable_and_searchable(self):
        event = BrowserDiagnostic(
            BrowserDiagnosticCode.AUTH_EMAIL_PASTE,
            "邮箱粘贴完成",
            {"attempt": 1},
        )

        self.assertEqual(event.render(), "[AUTH_EMAIL_PASTE] 邮箱粘贴完成")
        self.assertEqual(event.details, {"attempt": 1})

    def test_emit_returns_event_and_sends_rendered_line(self):
        lines = []

        event = emit_browser_diagnostic(
            lines.append,
            BrowserDiagnosticCode.WINDOW_SINGLE_STABLE,
            "单窗口稳定启动",
            slots=1,
        )

        self.assertEqual(lines, ["[WINDOW_SINGLE_STABLE] 单窗口稳定启动"])
        self.assertEqual(event.details, {"slots": 1})

    def test_context_uses_code_instead_of_localized_keyword_guessing(self):
        context = browser_diagnostic_context(
            "[AUTH_DIRECT_NAV_BLOCKED] [认证] 保留当前页面"
        )

        self.assertEqual(context["diagnosticCode"], "AUTH_DIRECT_NAV_BLOCKED")
        self.assertEqual(context["stage"], "openai_auth")
        self.assertEqual(context["location"], "OpenAI 邮箱认证页")
        self.assertEqual(context["action"], "保留当前页面")
        self.assertEqual(context["status"], "success")

    def test_unknown_or_legacy_log_falls_back_to_existing_parser(self):
        self.assertIsNone(browser_diagnostic_context("[认证] 旧格式日志"))
        self.assertIsNone(browser_diagnostic_context("普通日志"))

    def test_password_timeout_code_maps_to_password_stage(self):
        context = browser_diagnostic_context(
            "[AUTH_PASSWORD_TIMEOUT] 密码控件等待超时"
        )

        self.assertEqual(context["diagnosticCode"], "AUTH_PASSWORD_TIMEOUT")
        self.assertEqual(context["stage"], "password")
        self.assertEqual(context["status"], "error")


if __name__ == "__main__":
    unittest.main()
