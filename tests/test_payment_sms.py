import json

from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.payment_sms import (
    GLOBAL_SMS_ROUTING_SETTING_KEY,
    GlobalSmsRoutingConfigStore,
    PaymentSmsProviderResolver,
)


def save_setting(database, key, payload):
    connection = connect_db(str(database))
    try:
        connection.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, json.dumps(payload)),
        )
        connection.commit()
    finally:
        connection.close()


def test_resolver_prefers_configured_hero_sms(tmp_path, monkeypatch):
    database = tmp_path / "settings.db"
    monkeypatch.delenv("HERO_SMS_API_KEY", raising=False)
    save_setting(database, "paypal_sms_config_v1", {"defaultProvider": "hero-sms"})
    save_setting(database, "hero_sms_phone_config_v1", {"apiKey": "hero-key"})
    resolver = PaymentSmsProviderResolver(
        database, smsbower_configured=lambda: True
    )

    selected = resolver.resolve()

    assert selected is not None
    assert selected.provider == "hero-sms"
    assert selected.virtual_us is False


def test_resolver_uses_default_smsbower_instead_of_legacy_manual(
    tmp_path, monkeypatch
):
    database = tmp_path / "settings.db"
    monkeypatch.delenv("HERO_SMS_API_KEY", raising=False)
    save_setting(database, "paypal_sms_config_v1", {"defaultProvider": "manual"})
    save_setting(database, "hero_sms_phone_config_v1", {"apiKey": "hero-key"})
    resolver = PaymentSmsProviderResolver(
        database, smsbower_configured=lambda: False
    )

    selected = resolver.resolve()

    assert selected is None
    assert resolver.preferred_provider() == "smsbower"


def test_resolver_never_falls_back_when_selected_provider_is_not_configured(
    tmp_path, monkeypatch
):
    database = tmp_path / "settings.db"
    monkeypatch.delenv("HERO_SMS_API_KEY", raising=False)
    save_setting(database, "paypal_sms_config_v1", {"defaultProvider": "hero-sms"})
    resolver = PaymentSmsProviderResolver(
        database, smsbower_configured=lambda: True
    )

    selected = resolver.resolve()

    assert selected is None
    assert resolver.preferred_provider() == "hero-sms"


def test_resolver_returns_none_without_an_automatic_provider(tmp_path, monkeypatch):
    database = tmp_path / "settings.db"
    monkeypatch.delenv("HERO_SMS_API_KEY", raising=False)
    save_setting(database, "paypal_sms_config_v1", {"defaultProvider": "manual"})
    resolver = PaymentSmsProviderResolver(
        database, smsbower_configured=lambda: False
    )

    assert resolver.resolve() is None


def test_global_store_persists_both_purposes_and_never_exposes_api_keys(
    tmp_path, monkeypatch
):
    database = tmp_path / "settings.db"
    monkeypatch.delenv("SMSBOWER_API_KEY", raising=False)
    monkeypatch.delenv("HERO_SMS_API_KEY", raising=False)
    store = GlobalSmsRoutingConfigStore(database)

    state = store.configure(
        {
            "binding": {
                "provider": "hero-sms",
                "maxPrice": 0.071,
                "countries": ["US", "CL"],
            },
            "paypal": {
                "provider": "smsbower",
                "maxPrice": 0.123,
                "countries": ["GB", "US"],
            },
            "apiKeys": {
                "smsbower": "smsbower-secret-key",
                "hero-sms": "hero-secret-key",
            },
        }
    )

    assert state["binding"]["provider"] == "hero-sms"
    assert state["binding"]["maxPrice"] == 0.071
    assert state["binding"]["countries"] == ["US", "CL"]
    assert state["paypal"]["provider"] == "smsbower"
    assert state["paypal"]["maxPrice"] == 0.123
    assert state["paypal"]["countries"] == ["GB", "US"]
    assert all(item["configured"] for item in state["providers"])
    assert "secret-key" not in json.dumps(state)
    assert store.provider_api_key("smsbower") == "smsbower-secret-key"
    assert store.provider_api_key("hero-sms") == "hero-secret-key"

    saved = store._setting(GLOBAL_SMS_ROUTING_SETTING_KEY)
    assert saved["binding"]["countries"] == ["US", "CL"]
    assert store._setting("paypal_sms_config_v1")["defaultProvider"] == "smsbower"


def test_global_store_requires_at_least_one_supported_country(tmp_path):
    store = GlobalSmsRoutingConfigStore(tmp_path / "settings.db")

    try:
        store.configure(
            {
                "binding": {
                    "provider": "smsbower",
                    "maxPrice": 0.064,
                    "countries": [],
                }
            }
        )
    except ValueError as error:
        assert "至少选择一个国家" in str(error)
    else:
        raise AssertionError("empty country selection should be rejected")
