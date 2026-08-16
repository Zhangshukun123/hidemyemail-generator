from types import SimpleNamespace

import pytest

from hidemyemail_generator.openai_registration_state import (
    RegistrationDomStateMismatch,
    configure_registration_state_recognition,
    recognize_registration_page,
)
from hidemyemail_generator.openai_registration_flow import (
    configure_password_first_login,
)


class _Candidate:
    @staticmethod
    def is_visible(**_kwargs):
        return True


class _Collection:
    def __init__(self, items=(), text=""):
        self.items = list(items)
        self.text = text

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]

    def inner_text(self, **_kwargs):
        return self.text


class _StatePage:
    url = "https://auth.openai.com/log-in"

    def __init__(self, state, *, expose_inputs=True, body_text=""):
        self.state = state
        self.expose_inputs = expose_inputs
        self.body_text = body_text

    def locator(self, selector):
        body_text = self.body_text or {
            "email": "Log in or sign up Email address Continue",
            "password": "Enter your password Continue",
            "email_verification": "Check your inbox verification code Continue",
            "profile": "Tell us about you Full name Date of birth",
            "security": "Security verification Verify you are human",
        }.get(self.state, "")
        if selector == "body":
            return _Collection(text=body_text)
        selector_states = {
            'input[type="email"]': "email",
            'input[type="password"]': "password",
            'input[autocomplete="one-time-code"]': "email_verification",
            'input[name="name"]': "profile",
        }
        if self.expose_inputs and selector_states.get(selector) == self.state:
            return _Collection([_Candidate()])
        return _Collection()

    @staticmethod
    def evaluate(_script):
        return "complete"

    @staticmethod
    def wait_for_timeout(_milliseconds):
        return None


def _configure(worker):
    emitted = []
    assert configure_registration_state_recognition(
        worker,
        emit_state=emitted.append,
        diagnostics_dir=".",
    )
    return emitted


def test_recognition_includes_dom_evidence_for_detailed_logs():
    state = recognize_registration_page(SimpleNamespace(), _StatePage("email"))

    assert state["code"] == "email"
    assert state["domSignals"]["emailInput"] is True
    assert state["domSignals"]["passwordInput"] is False
    assert state["domEvidence"] == ["body-text", "email-input"]


def test_passwordless_verification_ignores_optional_password_control():
    worker = SimpleNamespace(_hme_password_first_login_enabled=False)
    page = _StatePage(
        "email_verification",
        body_text=(
            "Check your inbox verification code Continue "
            "Continue with password"
        ),
    )

    state = recognize_registration_page(worker, page)

    assert state["code"] == "email_verification"
    assert state["stage"] == "email_verification"
    assert state["nextAction"] == "读取本轮最新邮箱验证码并提交"


def test_password_required_verification_reports_password_choice_as_next_action():
    worker = SimpleNamespace(_hme_password_first_login_enabled=True)
    page = _StatePage(
        "email_verification",
        body_text=(
            "Check your inbox verification code Continue "
            "Continue with password"
        ),
    )

    state = recognize_registration_page(worker, page)

    assert state["code"] == "email_verification"
    assert state["stage"] == "email_verification"
    assert state["nextAction"] == "选择“使用密码继续”，先完成密码设置"


def test_japanese_password_form_overrides_stale_email_verification_route():
    class JapanesePasswordPage(_StatePage):
        url = "https://auth.openai.com/email-verification"

        def locator(self, selector):
            if selector == "body":
                return _Collection(
                    text=(
                        "パスワードの作成 ChatGPT と他のOpenAI製品へのログイン時に、"
                        "このパスワードを使用してください。メールアドレス パスワード 続行"
                    )
                )
            if selector == 'input[aria-label*="パスワード" i]':
                return _Collection([_Candidate()])
            return _Collection()

    state = recognize_registration_page(
        SimpleNamespace(_hme_password_first_login_enabled=True),
        JapanesePasswordPage("password"),
    )

    assert state["code"] == "password"
    assert state["stage"] == "password"
    assert state["currentPage"] == "OpenAI 密码页"
    assert state["nextAction"] == "填写已保存的唯一密码并提交一次"
    assert state["domSignals"]["passwordInput"] is True
    assert state["locale"] == "ja-JP"


def test_japanese_password_form_allows_password_fill_on_stale_verification_route():
    class JapanesePasswordPage(_StatePage):
        url = "https://auth.openai.com/email-verification"

        def locator(self, selector):
            if selector == "body":
                return _Collection(text="パスワードの作成 パスワード 続行")
            if selector == 'input[type="password"]':
                return _Collection([_Candidate()])
            return _Collection()

    class Worker:
        def __init__(self):
            self.logs = []
            self.password_fills = 0

        def log(self, message):
            self.logs.append(message)

        def _fill_password_step(self, _page):
            self.password_fills += 1

    worker = Worker()
    _configure(worker)

    assert worker._fill_password_step(JapanesePasswordPage("password")) is None
    assert worker.password_fills == 1
    assert any(
        "当前=OpenAI 密码页" in line
        and "必要DOM=passwordInput" in line
        and "判定=符合" in line
        for line in worker.logs
    )


def test_disabled_password_first_hook_marks_worker_as_passwordless():
    worker = SimpleNamespace()

    assert configure_password_first_login(worker, enabled=False) is False
    assert worker._hme_password_first_login_enabled is False
    assert worker._hme_password_first_login_required is False


def test_matching_dom_executes_action_and_logs_before_and_after_state():
    class Worker:
        def __init__(self):
            self.logs = []
            self.action_count = 0

        def log(self, message):
            self.logs.append(message)

        def _fill_email_if_visible(self, page):
            self.action_count += 1
            page.state = "password"
            page.url = "https://auth.openai.com/password"
            return True

    worker = Worker()
    emitted = _configure(worker)
    result = worker._fill_email_if_visible(_StatePage("email"))

    assert result is True
    assert worker.action_count == 1
    assert any("执行前=填写邮箱并提交" in line and "判定=符合" in line for line in worker.logs)
    assert any("界面变化=email→password" in line for line in worker.logs)
    assert emitted[-1]["code"] == "password"


def test_mismatched_dom_skips_mutating_action_and_returns_false():
    class Worker:
        def __init__(self):
            self.logs = []
            self.action_count = 0

        def log(self, message):
            self.logs.append(message)

        def _fill_email_if_visible(self, _page):
            self.action_count += 1
            return True

    worker = Worker()
    _configure(worker)
    result = worker._fill_email_if_visible(_StatePage("profile"))

    assert result is False
    assert worker.action_count == 0
    assert any(
        "当前=姓名与出生信息页" in line
        and "判定=不符合" in line
        and "响应=跳过填写邮箱并提交" in line
        for line in worker.logs
    )


def test_matching_text_without_required_dom_input_still_skips_action():
    class Worker:
        def __init__(self):
            self.logs = []
            self.action_count = 0

        def log(self, message):
            self.logs.append(message)

        def _fill_email_if_visible(self, _page):
            self.action_count += 1
            return True

    worker = Worker()
    _configure(worker)
    result = worker._fill_email_if_visible(
        _StatePage("email", expose_inputs=False)
    )

    assert result is False
    assert worker.action_count == 0
    assert any(
        "必要DOM=emailInput" in line and "判定=不符合" in line
        for line in worker.logs
    )


def test_wrong_page_never_reports_otp_submission_success():
    class Worker:
        def __init__(self):
            self.logs = []
            self.action_count = 0

        def log(self, message):
            self.logs.append(message)

        def _submit_email_code(self, _page, _timestamp):
            self.action_count += 1

    worker = Worker()
    _configure(worker)

    with pytest.raises(RegistrationDomStateMismatch, match="DOM 检测阻止错误操作"):
        worker._submit_email_code(_StatePage("password"), 0)
    assert worker.action_count == 0
    assert any("期望=email_verification" in line for line in worker.logs)


def test_security_page_keeps_form_action_paused_for_manual_completion():
    class Worker:
        def __init__(self):
            self.logs = []
            self.action_count = 0

        def log(self, message):
            self.logs.append(message)

        def _fill_email_if_visible(self, _page):
            self.action_count += 1
            return True

    worker = Worker()
    _configure(worker)
    result = worker._fill_email_if_visible(_StatePage("security"))

    assert result is False
    assert worker.action_count == 0
    assert any("等待手动完成安全验证" in line for line in worker.logs)
