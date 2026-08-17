import pytest

from zkgmail_code_server.strategies.keyword_code_extractor import KeywordCodeExtractor


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("OpenAI verification code", "Your code is 246810", "246810"),
        ("登录验证", "验证码：4827，请在十分钟内使用", "4827"),
        ("Verify your account", "Security code AB12CD34", "AB12CD34"),
        ("ChatGPT", "Il tuo codice temporaneo è 739201", "739201"),
        ("Invoice 2026", "Order number 123456", ""),
        ("Verification", "Copyright 2026", ""),
    ],
)
def test_extracts_only_keyword_adjacent_code(subject, body, expected):
    assert KeywordCodeExtractor().extract(subject, body) == expected


def test_six_digit_openai_code_supports_language_neutral_fallback():
    assert KeywordCodeExtractor().extract("OpenAI", "PIN: 135790") == "135790"
