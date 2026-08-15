from types import SimpleNamespace

import pytest

from hidemyemail_generator.openai_browser_selectors import (
    COMPLETED_ONBOARDING_CONTINUE_SELECTORS,
    COMPLETED_ONBOARDING_MARKERS,
    PASSWORD_CONTINUE_SELECTORS,
)
from hidemyemail_generator.openai_registration_flow import (
    PASSWORD_OTP_RESEND_SELECTORS,
    _detect_verification_language,
)
from hidemyemail_generator.openai_registration_otp import (
    EMAIL_VERIFICATION_RESEND_SELECTORS,
    EMAIL_VERIFICATION_SUBMIT_SELECTORS,
    _email_verification_ui_state,
)
from hidemyemail_generator.openai_registration_state import (
    recognize_registration_page,
)
from hidemyemail_generator.registration_auth import (
    OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS,
)
from hidemyemail_generator.registration_locale import (
    detect_registration_locale,
    normalize_registration_locale,
    supported_registration_locales,
)


SUPPORTED_LOCALES = (
    "en-US",
    "zh-CN",
    "zh-TW",
    "ja-JP",
    "de-DE",
    "fr-FR",
    "es-ES",
    "pt-BR",
    "th-TH",
    "ko-KR",
)

OTP_SAMPLES = {
    "en-US": "Check your inbox. Enter the verification code.",
    "zh-CN": "检查收件箱，输入验证码。",
    "zh-TW": "檢查收件匣，輸入驗證碼。",
    "ja-JP": "受信箱を確認して、確認コードを入力してください。",
    "de-DE": "Posteingang überprüfen und Bestätigungscode eingeben.",
    "fr-FR": "Vérifiez votre boîte de réception. Code de vérification.",
    "es-ES": "Revisa tu bandeja de entrada. Código de verificación.",
    "pt-BR": "Confira sua caixa de entrada. Código de verificação.",
    "th-TH": "ตรวจสอบกล่องข้อความของคุณ แล้วป้อนรหัสยืนยัน",
    "ko-KR": "받은편지함을 확인하고 인증 코드를 입력하세요.",
}


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


class _LocalizedPage:
    def __init__(
        self,
        text,
        *,
        locale="",
        url="https://auth.openai.com/email-verification",
        semantic_otp=True,
        state_input="otp",
    ):
        self.text = text
        self.locale = locale
        self.url = url
        self.semantic_otp = semantic_otp
        self.state_input = state_input

    def locator(self, selector):
        if selector == "body":
            return _Collection(text=self.text)
        semantic = (
            selector == 'input[autocomplete="one-time-code"]'
            and self.semantic_otp
        )
        state_match = {
            'input[type="email"]': "email",
            'input[type="password"]': "password",
            'input[autocomplete="one-time-code"]': "otp",
            'input[name="name"]': "profile",
        }.get(selector) == self.state_input
        return _Collection([_Candidate()]) if semantic or state_match else _Collection()

    def evaluate(self, script):
        if "document.documentElement.lang" in script:
            return self.locale
        return "complete"


def test_catalog_has_ten_complete_registration_locale_models():
    profiles = supported_registration_locales()

    assert tuple(profile.code for profile in profiles) == SUPPORTED_LOCALES
    for profile in profiles:
        assert profile.email
        assert profile.password
        assert profile.otp
        assert profile.profile
        assert profile.completed
        assert profile.security
        assert profile.error
        assert profile.email_submit_labels
        assert profile.password_continue_labels
        assert profile.resend_labels
        assert profile.finish_labels


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("auto", "auto"),
        ("EN_gb", "en-US"),
        ("zh-Hans", "zh-CN"),
        ("zh-HK", "zh-TW"),
        ("pt-PT", "pt-BR"),
        ("fr-CA", "fr-FR"),
        ("xx-ZZ", "auto"),
    ),
)
def test_locale_aliases_are_normalized_without_unsafe_free_form_values(source, expected):
    assert normalize_registration_locale(source) == expected


@pytest.mark.parametrize(("locale", "body_text"), OTP_SAMPLES.items())
def test_detects_each_supported_localized_verification_page(locale, body_text):
    profile = detect_registration_locale(body_text)

    assert profile is not None
    assert profile.code == locale


def test_french_verification_code_is_not_misclassified_as_english():
    assert _detect_verification_language("Code de vérification") == "法文"


def test_all_locale_actions_are_connected_to_scoped_registration_selectors():
    for profile in supported_registration_locales():
        for label in profile.email_submit_labels:
            assert any(
                selector.endswith(f':text-is("{label}")')
                for selector in OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS
            )
        for label in profile.password_continue_labels:
            assert f'button:has-text("{label}")' in PASSWORD_CONTINUE_SELECTORS
        for label in profile.resend_labels:
            selector = f'button:has-text("{label}")'
            assert selector in PASSWORD_OTP_RESEND_SELECTORS
            assert selector in EMAIL_VERIFICATION_RESEND_SELECTORS
        for label in profile.continue_labels:
            assert (
                f'button[type="submit"]:has-text("{label}")'
                in EMAIL_VERIFICATION_SUBMIT_SELECTORS
            )
            assert (
                f'button:has-text("{label}")'
                in COMPLETED_ONBOARDING_CONTINUE_SELECTORS
            )
        for marker in profile.completed:
            assert f'text="{marker}"' in COMPLETED_ONBOARDING_MARKERS


@pytest.mark.parametrize(("locale", "body_text"), OTP_SAMPLES.items())
def test_strict_otp_context_accepts_route_plus_semantic_input_in_every_locale(
    locale,
    body_text,
):
    page = _LocalizedPage(body_text, locale=locale)

    state = _email_verification_ui_state(
        page,
        "hidden@example.com",
        lambda _page, _selectors: [_Candidate()],
    )

    assert state["recognized"] is True
    assert state["semanticInput"] is True
    assert state["locale"] == detect_registration_locale(body_text).label


def test_unknown_text_with_only_generic_input_stays_blocked():
    page = _LocalizedPage(
        "unrelated form",
        locale="xx-ZZ",
        semantic_otp=False,
        state_input="generic",
    )

    state = _email_verification_ui_state(
        page,
        "hidden@example.com",
        lambda _page, _selectors: [_Candidate()],
    )

    assert state["recognized"] is False
    assert state["semanticInput"] is False


@pytest.mark.parametrize(
    ("locale", "text", "state_input", "expected_code"),
    (
        ("fr-FR", "Une erreur s'est produite. Réessayez plus tard.", "generic", "error"),
        ("de-DE", "Sicherheitsüberprüfung. Browser wird überprüft.", "generic", "security"),
        ("es-ES", "Cuéntanos sobre ti. Fecha de nacimiento.", "profile", "profile"),
        ("ko-KR", "모든 준비가 완료되었습니다", "generic", "completed"),
    ),
)
def test_page_state_presenter_reports_localized_state_and_locale(
    locale,
    text,
    state_input,
    expected_code,
):
    state = recognize_registration_page(
        SimpleNamespace(),
        _LocalizedPage(
            text,
            locale=locale,
            url="https://auth.openai.com/",
            semantic_otp=False,
            state_input=state_input,
        ),
    )

    assert state["code"] == expected_code
    assert state["locale"] == locale
    assert state["localeSource"] == "text"
    assert state["declaredLocale"] == locale
