import json
import sqlite3

import pytest

from hidemyemail_generator.payment_sms import GLOBAL_SMS_ROUTING_SETTING_KEY
from hidemyemail_generator.plus_sms import (
    HERO_SMS_SETTING_KEY,
    PLUS_CODEX_SMS_CHILE_MAX_PRICE_USD,
    PLUS_CODEX_SMS_MAX_PRICE_USD,
    PLUS_CODEX_SMS_SERVICE_CODE,
    PLUS_CODEX_SMS_US_MAX_PRICE_USD,
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


def test_smsbower_chile_success_never_requests_us():
    requester = ScriptedRequester("ACCESS_NUMBER:activation-cl:+56 9 1234 5678")
    logs = []
    adapter = _adapter("smsbower", requester, on_log=logs.append)

    activation = adapter.request_phone()

    assert activation is not None
    assert activation.phone == "+56912345678"
    assert activation.activation_id == "activation-cl"
    assert activation.raw == {
        "country": "CL",
        "country_id": 151,
        "service": "openai",
        "service_code": "dr",
        "max_price": 0.054,
    }
    assert [request["country"] for request in requester.requests] == [151]
    assert requester.requests[0]["service"] == PLUS_CODEX_SMS_SERVICE_CODE == "dr"
    assert requester.requests[0]["maxPrice"] == "0.054"
    assert any("正在尝试智利" in item["message"] for item in logs)
    assert any(item["level"] == "success" for item in logs)
    assert PLUS_CODEX_SMS_CHILE_MAX_PRICE_USD == 0.054
    assert PLUS_CODEX_SMS_US_MAX_PRICE_USD == PLUS_CODEX_SMS_MAX_PRICE_USD == 0.064


def test_smsbower_only_falls_back_to_us_after_chile_no_numbers():
    requester = ScriptedRequester(
        "NO_NUMBERS", "ACCESS_NUMBER:activation-us:+1 202 555 0187"
    )
    logs = []
    adapter = _adapter("smsbower", requester, on_log=logs.append)

    activation = adapter.request_phone()

    assert activation is not None
    assert activation.raw["country"] == "US"
    assert activation.raw["max_price"] == 0.064
    assert [request["country"] for request in requester.requests] == [151, 187]
    assert [request["maxPrice"] for request in requester.requests] == [
        "0.054",
        "0.064",
    ]
    assert any("智利线路无库存；仅因此回退美国" in item["message"] for item in logs)


def test_smsbower_non_inventory_error_never_requests_us():
    requester = ScriptedRequester("NO_BALANCE")
    adapter = _adapter("smsbower", requester)

    with pytest.raises(PlusSmsError, match="余额不足"):
        adapter.request_phone()

    assert [request["country"] for request in requester.requests] == [151]


def test_smsbower_returns_none_after_chile_and_us_are_both_out_of_stock():
    requester = ScriptedRequester("NO_NUMBERS", "NO_NUMBERS")
    adapter = _adapter("smsbower", requester)

    assert adapter.request_phone() is None
    assert [request["country"] for request in requester.requests] == [151, 187]


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
    monkeypatch.delenv("SMSBOWER_API_KEY", raising=False)
    _save_setting(
        database,
        GLOBAL_SMS_ROUTING_SETTING_KEY,
        {
            "binding": {
                "provider": "hero-sms",
                "maxPrice": 0.123,
                "countries": ["ID", "US"],
            }
        },
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
    assert activation.raw["country"] == "ID"
    assert activation.raw["max_price"] == 0.123
    assert requester.requests[0]["api_key"] == "hero-database-key"
    assert requester.requests[0]["country"] == 6
    assert requester.requests[0]["maxPrice"] == "0.123"
