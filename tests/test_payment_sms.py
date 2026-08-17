import json

from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.payment_sms import PaymentSmsProviderResolver


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


def test_resolver_never_sends_manual_to_one_click_payment(tmp_path, monkeypatch):
    database = tmp_path / "settings.db"
    monkeypatch.delenv("HERO_SMS_API_KEY", raising=False)
    save_setting(database, "paypal_sms_config_v1", {"defaultProvider": "manual"})
    save_setting(database, "hero_sms_phone_config_v1", {"apiKey": "hero-key"})
    resolver = PaymentSmsProviderResolver(
        database, smsbower_configured=lambda: False
    )

    selected = resolver.resolve()

    assert selected is not None
    assert selected.provider == "hero-sms"


def test_resolver_falls_back_when_preferred_provider_is_not_configured(
    tmp_path, monkeypatch
):
    database = tmp_path / "settings.db"
    monkeypatch.delenv("HERO_SMS_API_KEY", raising=False)
    save_setting(database, "paypal_sms_config_v1", {"defaultProvider": "hero-sms"})
    resolver = PaymentSmsProviderResolver(
        database, smsbower_configured=lambda: True
    )

    selected = resolver.resolve()

    assert selected is not None
    assert selected.provider == "smsbower"
    assert selected.virtual_us is True


def test_resolver_returns_none_without_an_automatic_provider(tmp_path, monkeypatch):
    database = tmp_path / "settings.db"
    monkeypatch.delenv("HERO_SMS_API_KEY", raising=False)
    save_setting(database, "paypal_sms_config_v1", {"defaultProvider": "manual"})
    resolver = PaymentSmsProviderResolver(
        database, smsbower_configured=lambda: False
    )

    assert resolver.resolve() is None
