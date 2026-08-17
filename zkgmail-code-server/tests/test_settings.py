from __future__ import annotations

from zkgmail_code_server.settings import Settings


def test_from_env_unwraps_values_from_raw_docker_env_file(monkeypatch):
    quoted_values = {
        "ZKGMAIL_IMAP_HOST": '"imap.qq.com"',
        "ZKGMAIL_IMAP_PORT": '"993"',
        "ZKGMAIL_IMAP_USERNAME": '"receiver@example.test"',
        "ZKGMAIL_IMAP_PASSWORD": '"test-password"',
        "ZKGMAIL_IMAP_FOLDER": '"INBOX"',
        "ZKGMAIL_LOOKBACK_MINUTES": '"45"',
        "ZKGMAIL_CACHE_TTL_SECONDS": '"1.5"',
        "ZKGMAIL_TRUSTED_RECIPIENT_HEADER": '"X-Original-To"',
        "ZKGMAIL_ACCESS_TOKEN": f'"{"a" * 64}"',
    }
    for name, value in quoted_values.items():
        monkeypatch.setenv(name, value)

    current = Settings.from_env()

    assert current.imap_host == "imap.qq.com"
    assert current.imap_port == 993
    assert current.imap_username == "receiver@example.test"
    assert current.imap_password == "test-password"
    assert current.imap_folder == "INBOX"
    assert current.lookback_minutes == 45
    assert current.cache_ttl_seconds == 1.5
    assert current.trusted_recipient_header == "X-Original-To"
    assert current.configured is True
    assert current.access_protected is True


def test_from_env_preserves_unmatched_quote_characters(monkeypatch):
    monkeypatch.setenv("ZKGMAIL_IMAP_USERNAME", 'receiver"@example.test')
    monkeypatch.setenv("ZKGMAIL_IMAP_PASSWORD", "test-password'")

    current = Settings.from_env()

    assert current.imap_username == 'receiver"@example.test'
    assert current.imap_password == "test-password'"
