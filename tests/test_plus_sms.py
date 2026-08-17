import json
import sqlite3

import pytest

from hidemyemail_generator.plus_sms import (
    HERO_SMS_SETTING_KEY,
    PAYMENT_SMS_SETTING_KEY,
    PLUS_CODEX_SMS_MAX_PRICE_USD,
    PLUS_CODEX_SMS_SERVICE_CODE,
    PlusSmsActivation,
    PlusSmsError,
    PlusSmsProviderFactory,
    SmsActivateCodexAdapter,
)


class ScriptedRequester:
    """Record SMS-Activate requests and return responses in order."""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def __call__(self, query: dict[str, object]) -> str:
        self.requests.append(dict(query))
        if not self.responses:
            raise AssertionError(f"unexpected provider request: {query['action']}")
        return self.responses.pop(0)


def _adapter(
    provider: str,
    requester: ScriptedRequester,
    **options: object,
) -> SmsActivateCodexAdapter:
    return SmsActivateCodexAdapter(
        provider=provider,
        api_key="unit-test-key",
        requester=requester,
        poll_interval_seconds=0.01,
        **options,
    )


def _save_setting(database, key: str, payload: dict[str, object]) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS settings "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            (key, json.dumps(payload)),
        )
        connection.commit()
    finally:
        connection.close()


def test_openai_number_uses_dr_and_exact_ten_cent_limit_for_each_country():
    requester = ScriptedRequester(
        "NO_NUMBERS",
        "ACCESS_NUMBER:activation-2:+1 (202) 555-0187",
    )
    adapter = _adapter(
        "smsbower",
        requester,
        country_ids=(("FIRST", 10), ("SECOND", 20), ("UNUSED", 30)),
    )

    activation = adapter.request_phone()

    assert activation is not None
    assert activation.phone == "+12025550187"
    assert activation.activation_id == "activation-2"
    assert activation.raw == {
        "country": "SECOND",
        "country_id": 20,
        "service": "openai",
        "service_code": "dr",
        "max_price": 0.1,
    }
    assert [request["country"] for request in requester.requests] == [10, 20]
    assert all(
        request["action"] == "getNumber"
        and request["service"] == PLUS_CODEX_SMS_SERVICE_CODE == "dr"
        and request["maxPrice"] == "0.1"
        for request in requester.requests
    )
    assert PLUS_CODEX_SMS_MAX_PRICE_USD == 0.1


def test_all_candidate_countries_are_tried_before_returning_no_number():
    requester = ScriptedRequester("NO_NUMBERS", "NO_NUMBERS", "NO_NUMBERS")
    adapter = _adapter(
        "smsbower",
        requester,
        country_ids=(("ONE", 1), ("TWO", 2), ("THREE", 3)),
    )

    assert adapter.request_phone() is None
    assert [request["country"] for request in requester.requests] == [1, 2, 3]


def test_smsbower_sends_ready_resend_complete_and_cancel_statuses():
    requester = ScriptedRequester(
        "ACCESS_READY",
        "ACCESS_RETRY_GET",
        "ACCESS_ACTIVATION",
        "ACCESS_CANCEL",
    )
    adapter = _adapter("smsbower", requester)
    activation = PlusSmsActivation(
        phone="+12025550187",
        activation_id="activation-1",
        provider="smsbower",
    )

    assert adapter.mark_sent(activation) is True
    assert adapter.request_resend(activation) is True
    assert adapter.complete(activation) is True
    assert adapter.cancel(activation) is True

    assert [request["status"] for request in requester.requests] == [1, 3, 6, 8]
    assert all(request["action"] == "setStatus" for request in requester.requests)
    assert all(request["id"] == "activation-1" for request in requester.requests)


def test_hero_does_not_send_ready_status_one():
    requester = ScriptedRequester()
    adapter = _adapter("hero-sms", requester)
    activation = PlusSmsActivation(
        phone="+628123456789",
        activation_id="hero-1",
        provider="hero-sms",
    )

    assert adapter.mark_sent(activation) is True
    assert requester.requests == []


def test_wait_for_otp_ignores_excluded_code_and_returns_new_code():
    requester = ScriptedRequester(
        "STATUS_OK:123456",
        "STATUS_WAIT_RESEND",
        "STATUS_OK:'654321'",
    )
    adapter = _adapter("smsbower", requester)
    activation = PlusSmsActivation(
        phone="+12025550187",
        activation_id="activation-1",
        provider="smsbower",
    )

    code = adapter.wait_for_otp(
        activation,
        timeout=0.5,
        exclude_codes={"123456"},
    )

    assert code == "654321"
    assert [request["action"] for request in requester.requests] == [
        "getStatus",
        "getStatus",
        "getStatus",
    ]


def test_provider_error_never_leaks_api_key():
    api_key = "provider-super-secret"

    def requester(_query: dict[str, object]) -> str:
        return f"UNKNOWN_PROVIDER_ERROR:{api_key}"

    adapter = SmsActivateCodexAdapter(
        provider="smsbower",
        api_key=api_key,
        requester=requester,
        country_ids=(("ONLY", 1),),
    )

    with pytest.raises(PlusSmsError) as caught:
        adapter.request_phone()

    assert api_key not in str(caught.value)
    assert api_key not in repr(caught.value)
    assert "UNKNOWN_PROVIDER_ERROR" in str(caught.value)


def test_factory_selects_provider_and_key_from_database(tmp_path, monkeypatch):
    database = tmp_path / "settings.db"
    monkeypatch.delenv("HERO_SMS_API_KEY", raising=False)
    _save_setting(
        database,
        PAYMENT_SMS_SETTING_KEY,
        {"defaultProvider": "hero-sms"},
    )
    _save_setting(
        database,
        HERO_SMS_SETTING_KEY,
        {"apiKey": "hero-database-key"},
    )
    requester = ScriptedRequester("ACCESS_NUMBER:hero-42:+628123456789")

    adapter = PlusSmsProviderFactory(database).create(requester=requester)
    activation = adapter.request_phone()

    assert adapter.name == "hero-sms"
    assert activation is not None
    assert activation.provider == "hero-sms"
    assert requester.requests[0]["api_key"] == "hero-database-key"
