import json
import sqlite3
import threading
from unittest.mock import patch

import httpx
import pytest

import web
from paypal.hero_sms import (
    HERO_SMS_API_URL,
    HeroSmsPhoneClient,
    HeroSmsPhoneError,
    resolve_api_key,
)
from paypal.sms_config import SmsSettingsModel, SmsSettingsPresenter
from paypal.smsbower import SMSBowerPhoneActivation


class ScriptedRequester:
    def __init__(self, responses):
        self.responses = {key: list(values) for key, values in responses.items()}
        self.calls = []

    def __call__(self, query):
        self.calls.append(dict(query))
        values = self.responses[query["action"]]
        return values.pop(0) if len(values) > 1 else values[0]


def test_hero_sms_uses_official_endpoint_paypal_service_and_country_ids():
    requester = ScriptedRequester(
        {
            "getNumber": ["ACCESS_NUMBER:hero-182:+819012345678"],
            "setStatus": ["ACCESS_ACTIVATION", "ACCESS_CANCEL"],
        }
    )
    client = HeroSmsPhoneClient(api_key="hero-test-key", requester=requester)

    activation = client.acquire_phone("JP", max_price=1.25)
    client.mark_sent(activation)
    client.complete(activation)
    client.cancel(activation)

    assert client.api_url == HERO_SMS_API_URL
    assert activation.provider == "hero-sms"
    assert activation.phone == "+819012345678"
    assert requester.calls[0] == {
        "api_key": "hero-test-key",
        "action": "getNumber",
        "service": "ts",
        "country": 182,
        "maxPrice": "1.25",
    }
    # HeroSMS documents status 3/6/8; mark_sent intentionally performs no call.
    assert [call["status"] for call in requester.calls[1:]] == [6, 8]


def test_hero_sms_polls_code_and_exposes_sanitized_status():
    requester = ScriptedRequester(
        {
            "getStatus": ["STATUS_WAIT_CODE", "STATUS_OK:'739104'"],
            "getBalance": ["ACCESS_BALANCE:8.50"],
            "getPrices": [json.dumps({"16": {"ts": {"cost": 0.12, "count": 7}}})],
        }
    )
    client = HeroSmsPhoneClient(
        api_key="hero-secret-key",
        requester=requester,
        poll_interval_seconds=0.01,
        otp_timeout_seconds=1,
    )
    activation = SMSBowerPhoneActivation(
        "hero-code", "+447700900123", "GB", provider="hero-sms"
    )

    assert client.wait_for_code(activation) == "739104"
    status = client.public_status(country="GB")

    assert status["provider"] == "hero-sms"
    assert status["label"] == "HeroSMS"
    assert status["serviceCode"] == "ts"
    assert status["balance"] == 8.5
    assert status["price"] == 0.12
    assert status["count"] == 7
    assert "apiKey" not in status
    assert "hero-secret-key" not in json.dumps(status)


def test_hero_sms_errors_never_include_api_key():
    client = HeroSmsPhoneClient(
        api_key="hero-secret-key", requester=ScriptedRequester({"getNumber": ["NO_BALANCE"]})
    )

    with pytest.raises(HeroSmsPhoneError, match="HeroSMS 余额不足") as error:
        client.acquire_phone("BR")

    assert "hero-secret-key" not in str(error.value)


def test_sms_settings_model_persists_keys_and_manual_default(tmp_path, monkeypatch):
    database = tmp_path / "hidemyemail.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?)",
        (
            "smsbower_mail_config_v1",
            json.dumps({"apiKey": "old-smsbower-key", "service": "dr"}),
        ),
    )
    connection.commit()
    connection.close()
    monkeypatch.delenv("SMSBOWER_API_KEY", raising=False)
    monkeypatch.delenv("HERO_SMS_API_KEY", raising=False)
    model = SmsSettingsModel(database)

    model.save_api_key("smsbower", "new-smsbower-key")
    model.save_api_key("hero-sms", "new-hero-key")
    model.save_default_provider("manual")

    assert model.api_key("smsbower") == "new-smsbower-key"
    assert model.api_key("hero-sms") == "new-hero-key"
    assert model.default_provider() == "manual"
    connection = sqlite3.connect(database)
    try:
        stored = json.loads(
            connection.execute(
                "SELECT value FROM settings WHERE key = 'smsbower_mail_config_v1'"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert stored["service"] == "dr"


def test_hero_sms_resolver_reads_shared_database(tmp_path, monkeypatch):
    database = tmp_path / "hidemyemail.db"
    model = SmsSettingsModel(database)
    model.save_api_key("hero-sms", "database-hero-key")
    monkeypatch.delenv("HERO_SMS_API_KEY", raising=False)
    monkeypatch.setenv("HME_DB_FILE", str(database))

    assert resolve_api_key() == "database-hero-key"


def test_settings_presenter_never_returns_provider_keys(tmp_path):
    model = SmsSettingsModel(tmp_path / "settings.db")
    model.save_api_key("smsbower", "smsbower-secret")
    model.save_api_key("hero-sms", "hero-secret")

    class ClientStub:
        def __init__(self, provider):
            self.provider = provider

        def public_status(self, *, country, probe):
            return {
                "provider": self.provider,
                "label": self.provider,
                "configured": True,
                "country": country,
                "apiKey": "must-be-removed",
            }

    presenter = SmsSettingsPresenter(model, lambda provider: ClientStub(provider))
    payload = presenter.present(country="GB", probe=False)

    serialized = json.dumps(payload)
    assert "apiKey" not in serialized
    assert "smsbower-secret" not in serialized
    assert "hero-secret" not in serialized


def test_sms_settings_http_entry_saves_key_without_echoing_it(tmp_path, monkeypatch):
    model = SmsSettingsModel(tmp_path / "settings.db")

    class ClientStub:
        def __init__(self, provider):
            self.provider = provider

        def public_status(self, *, country, probe):
            return {
                "provider": self.provider,
                "label": self.provider,
                "configured": model.configured(self.provider),
                "country": country,
                "supportedCountries": ["GB"],
                "defaultMaxPrice": 3,
                "docsUrl": "https://example.test/docs",
                "balance": None,
                "price": None,
                "count": 0,
                "error": "",
            }

    presenter = SmsSettingsPresenter(model, lambda provider: ClientStub(provider))
    monkeypatch.setattr(web, "SMS_SETTINGS_PRESENTER", presenter)
    web.RATE_BUCKETS.clear()
    server = web.WebThreadingHTTPServer(("127.0.0.1", 0), web.WebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        response = httpx.post(
            base_url + "/api/sms/config",
            json={
                "provider": "hero-sms",
                "apiKey": "http-hero-secret",
                "defaultProvider": "hero-sms",
                "country": "GB",
            },
            timeout=5,
        )
        status = httpx.get(
            base_url + "/api/sms/providers?country=GB&probe=0", timeout=5
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status_code == 200
    assert status.status_code == 200
    assert model.api_key("hero-sms") == "http-hero-secret"
    for payload in (response.json(), status.json()):
        assert payload["defaultProvider"] == "hero-sms"
        assert payload["timeoutSeconds"] == 60
        assert "apiKey" not in json.dumps(payload)
        assert "http-hero-secret" not in json.dumps(payload)


def test_hero_sms_job_starts_without_manual_phone(monkeypatch):
    class ClientStub:
        provider_label = "HeroSMS"

        def configured(self):
            return True

    monkeypatch.setattr(web, "HERO_SMS_PHONE_CLIENT", ClientStub())
    web.JOBS.clear()
    try:
        with patch.object(threading.Thread, "start", autospec=True):
            job = web.create_job(
                owner_device_id="h" * 32,
                ba_token="BA-HERO123456",
                phone="",
                debug=False,
                max_card_attempts=5,
                sms_provider="hero-sms",
                sms_service="paypal",
                sms_country="GB",
                sms_max_price=1.5,
                country="GB",
                proxy_pool=["http://user:pass@127.0.0.1:8888"],
            )

        assert job.phone == ""
        assert job.sms_provider == "hero-sms"
        assert job.to_dict(include_logs=False)["sms_auto"] is True
    finally:
        web.JOBS.clear()


def test_activation_is_finalized_by_the_jobs_own_provider(monkeypatch):
    events = []

    class HeroClientStub:
        provider_label = "HeroSMS"

        def complete(self, activation):
            events.append(("hero-complete", activation.activation_id))

        def cancel(self, activation):
            events.append(("hero-cancel", activation.activation_id))

    class WrongClientStub:
        provider_label = "SMSBower"

        def complete(self, _activation):
            pytest.fail("HeroSMS activation reached SMSBower")

        def cancel(self, _activation):
            pytest.fail("HeroSMS activation reached SMSBower")

    monkeypatch.setattr(web, "HERO_SMS_PHONE_CLIENT", HeroClientStub())
    monkeypatch.setattr(web, "SMSBOWER_PHONE_CLIENT", WrongClientStub())
    job = web.WebJob(
        id="hero-job",
        owner_device_id="d" * 32,
        ba_token="BA-HERO123456",
        phone="",
        country="GB",
        sms_provider="hero-sms",
    )
    first = SMSBowerPhoneActivation(
        "hero-ok", "+447700900123", "GB", provider="hero-sms"
    )
    second = SMSBowerPhoneActivation(
        "hero-cancel", "+447700900124", "GB", provider="hero-sms"
    )

    job.attach_sms_activation(first)
    assert job.finalize_sms_activation(success=True) is True
    job.attach_sms_activation(second)
    assert job.finalize_sms_activation(success=False) is True

    assert events == [("hero-complete", "hero-ok"), ("hero-cancel", "hero-cancel")]
