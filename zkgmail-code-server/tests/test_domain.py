import pytest

from zkgmail_code_server.domain import InvalidAddressError, normalize_zkgmail_address


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" User.Name+tag@ZKGMAIL.COM ", "user.name+tag@zkgmail.com"),
        ("a@zkgmail.com", "a@zkgmail.com"),
        ("zk-123_abc@zkgmail.com", "zk-123_abc@zkgmail.com"),
    ],
)
def test_normalize_exact_zkgmail_address(raw, expected):
    assert normalize_zkgmail_address(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "user@icloud.com",
        "user@sub.zkgmail.com",
        ".user@zkgmail.com",
        "user.@zkgmail.com",
        "user@@zkgmail.com",
        f"{'a' * 65}@zkgmail.com",
    ],
)
def test_reject_non_zkgmail_or_invalid_address(raw):
    with pytest.raises(InvalidAddressError):
        normalize_zkgmail_address(raw)
