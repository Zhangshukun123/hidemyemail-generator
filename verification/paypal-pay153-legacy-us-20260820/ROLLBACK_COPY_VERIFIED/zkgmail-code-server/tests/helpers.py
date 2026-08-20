from __future__ import annotations

from zkgmail_code_server.settings import Settings


TEST_ACCESS_TOKEN = "11" * 32


def settings(**overrides) -> Settings:
    values = {
        "imap_host": "imap.example.test",
        "imap_port": 993,
        "imap_username": "receiver@example.test",
        "imap_password": "test-password",
        "imap_folder": "INBOX",
        "imap_timeout_seconds": 5,
        "fetch_limit": 20,
        "lookback_minutes": 30,
        "cache_ttl_seconds": 0,
        "rate_limit_requests": 30,
        "rate_limit_window_seconds": 60,
        "access_token": TEST_ACCESS_TOKEN,
        "session_max_age_seconds": 600,
        "access_rate_limit_requests": 10,
        "access_rate_limit_window_seconds": 600,
    }
    values.update(overrides)
    return Settings(**values)
