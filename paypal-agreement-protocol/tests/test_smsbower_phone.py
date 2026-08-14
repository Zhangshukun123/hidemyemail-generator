import json
import sqlite3
import threading
from pathlib import Path
from types import MethodType
from unittest.mock import patch

import pytest

import web
from paypal.smsbower import (
    SMSBowerPhoneActivation,
    SMSBowerPhoneClient,
    SMSBowerPhoneError,
    resolve_api_key,
)


class ScriptedRequester:
    def __init__(self, responses):
        self.responses = {key: list(values) for key, values in responses.items()}
        self.calls = []

    def __call__(self, query):
        self.calls.append(dict(query))
        action = query["action"]
        values = self.responses[action]
        return values.pop(0) if len(values) > 1 else values[0]


def test_phone_activation_uses_paypal_service_and_country_mapping():
    requester = ScriptedRequester(
        {
            "getNumber": ["ACCESS_NUMBER:812345:+551199887766"],
            "setStatus": ["ACCESS_READY", "ACCESS_ACTIVATION"],
        }
    )
    client = SMSBowerPhoneClient(api_key="test-key", requester=requester)

    activation = client.acquire_phone("BR", max_price=0.75)
    client.mark_sent(activation)
    client.complete(activation)

    assert activation == SMSBowerPhoneActivation(
        activation_id="812345", phone="+551199887766", country="BR"
    )
    assert requester.calls[0]["service"] == "ts"
    assert requester.calls[0]["country"] == 73
    assert requester.calls[0]["maxPrice"] == "0.75"
    assert [call["status"] for call in requester.calls[1:]] == [1, 6]


def test_payment_sms_timeout_is_70_seconds_and_germany_is_supported():
    assert web.SMSBOWER_OTP_TIMEOUT_SECONDS == 70
    assert web.SMSBOWER_PHONE_CLIENT.otp_timeout_seconds == 70
    assert web.SMSBOWER_COUNTRY_IDS["DE"] == 43
    assert "DE" in web.VERIFIED_PROTOCOL_COUNTRIES


def test_wait_for_code_polls_until_status_ok():
    requester = ScriptedRequester(
        {"getStatus": ["STATUS_WAIT_CODE", "STATUS_OK:'739104'"]}
    )
    client = SMSBowerPhoneClient(
        api_key="test-key",
        requester=requester,
        poll_interval_seconds=0.01,
        otp_timeout_seconds=1,
    )
    activation = SMSBowerPhoneActivation("9", "+447700900123", "GB")

    assert client.wait_for_code(activation) == "739104"
    assert len(requester.calls) == 2


def test_provider_errors_are_normalized_without_api_key():
    requester = ScriptedRequester({"getNumber": ["NO_BALANCE"]})
    client = SMSBowerPhoneClient(api_key="secret-provider-key", requester=requester)

    with pytest.raises(SMSBowerPhoneError, match="余额不足") as error:
        client.acquire_phone("BR")

    assert "secret-provider-key" not in str(error.value)


def test_public_status_returns_live_balance_price_and_inventory():
    requester = ScriptedRequester(
        {
            "getBalance": ["ACCESS_BALANCE:9.25"],
            "getPrices": [json.dumps({"16": {"ts": {"cost": 0.086, "count": 17}}})],
        }
    )
    client = SMSBowerPhoneClient(api_key="test-key", requester=requester)

    status = client.public_status(country="GB")

    assert status["configured"] is True
    assert status["service"] == "ts"
    assert status["balance"] == 9.25
    assert status["price"] == 0.086
    assert status["count"] == 17
    assert "apiKey" not in status


def test_resolve_api_key_reuses_parent_database(tmp_path, monkeypatch):
    database = tmp_path / "hidemyemail.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
    connection.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?)",
        (
            "smsbower_mail_config_v1",
            json.dumps({"apiKey": "database-api-key-123456"}),
        ),
    )
    connection.commit()
    connection.close()
    monkeypatch.delenv("SMSBOWER_API_KEY", raising=False)
    monkeypatch.setenv("HME_DB_FILE", str(database))

    assert resolve_api_key() == "database-api-key-123456"


def test_smsbower_job_can_start_without_manual_phone(monkeypatch):
    class ClientStub:
        def configured(self):
            return True

    monkeypatch.setattr(web, "SMSBOWER_PHONE_CLIENT", ClientStub())
    web.JOBS.clear()
    with patch.object(threading.Thread, "start", autospec=True):
        job = web.create_job(
            owner_device_id="a" * 32,
            ba_token="BA-12345678",
            phone="",
            debug=False,
            max_card_attempts=5,
            sms_provider="smsbower",
            sms_service="ts",
            sms_country="GB",
            sms_max_price=1.25,
            country="GB",
            proxy_pool=["http://user:pass@127.0.0.1:8888"],
        )

    assert job.sms_provider == "smsbower"
    assert job.sms_service == "ts"
    assert job.sms_country == "GB"
    assert job.phone == ""
    assert job.sms_max_price == 1.25
    assert job.to_dict(include_logs=False)["sms_auto"] is True
    assert job.to_dict(include_logs=False)["sms_max_price"] == 1.25
    web.JOBS.clear()


def test_smsbower_job_rejects_country_mismatch(monkeypatch):
    class ClientStub:
        def configured(self):
            return True

    monkeypatch.setattr(web, "SMSBOWER_PHONE_CLIENT", ClientStub())
    with pytest.raises(ValueError, match="验证码国家必须与 PayPal 国家一致"):
        web.create_job(
            owner_device_id="b" * 32,
            ba_token="BA-12345678",
            phone="",
            debug=False,
            max_card_attempts=5,
            sms_provider="smsbower",
            sms_service="ts",
            sms_country="BR",
            country="GB",
            proxy_pool=["http://user:pass@127.0.0.1:8888"],
        )


def test_web_flow_automatically_submits_smsbower_code(monkeypatch):
    activation = SMSBowerPhoneActivation("44", "+551199887766", "BR")
    events = []

    class ClientStub:
        def mark_sent(self, item):
            events.append(("mark", item.activation_id))

        def wait_for_code(self, item, **_kwargs):
            events.append(("poll", item.activation_id))
            return "739104"

    class JobStub:
        sms_provider = "smsbower"
        sms_max_price = 1.0
        _cancel_event = threading.Event()

        def check_cancelled(self):
            return None

        def sms_activation(self):
            return activation

        def set_status(self, _status, stage):
            events.append(("stage", stage))

        def finalize_sms_activation(self, *, success):
            events.append(("finalize", success))

    flow = web.WebPayPalFlow.__new__(web.WebPayPalFlow)
    flow.job = JobStub()
    flow.country = "BR"
    flow._masked_phone = MethodType(lambda _self: "+55******7766", flow)
    flow._initiate_2fa_phone_confirmation = MethodType(
        lambda _self, _token, _url: ("auth", "challenge"), flow
    )
    flow._confirm_2fa_phone_confirmation = MethodType(
        lambda _self, _token, _url, _auth, _challenge, code: code == "739104",
        flow,
    )
    monkeypatch.setattr(web, "SMSBOWER_PHONE_CLIENT", ClientStub())

    flow._confirm_phone_with_retry("token", "https://paypal.test/signup")

    assert ("mark", "44") in events
    assert ("poll", "44") in events
    assert events[-1] == ("finalize", True)


def test_smsbower_timeout_cancels_number_and_acquires_another(monkeypatch):
    activations = [
        SMSBowerPhoneActivation("70-a", "+66811111111", "TH"),
        SMSBowerPhoneActivation("70-b", "+66822222222", "TH"),
    ]
    events = []

    class ClientStub:
        def acquire_phone(self, country, *, max_price):
            item = activations.pop(0)
            events.append(("acquire", item.activation_id, country, max_price))
            return item

        def mark_sent(self, item):
            events.append(("mark", item.activation_id))

        def wait_for_code(self, item, **kwargs):
            events.append(("wait", item.activation_id, kwargs["timeout_seconds"]))
            if item.activation_id == "70-a":
                raise SMSBowerPhoneError("SMSBower PayPal 短信验证码等待超时")
            return "739104"

    class JobStub:
        sms_provider = "smsbower"
        sms_max_price = 1.0
        _cancel_event = threading.Event()

        def __init__(self):
            self.current = None

        def check_cancelled(self):
            return None

        def sms_activation(self):
            return self.current

        def attach_sms_activation(self, activation):
            self.current = activation

        def set_status(self, _status, stage):
            events.append(("stage", stage))

        def finalize_sms_activation(self, *, success):
            events.append(("finalize", self.current.activation_id, success))
            self.current = None

    flow = web.WebPayPalFlow.__new__(web.WebPayPalFlow)
    flow.job = JobStub()
    flow.country = "TH"
    flow._update_user_phone = MethodType(lambda _self, phone: events.append(("phone", phone)), flow)
    flow._masked_phone = MethodType(lambda _self: "+66*******", flow)
    flow._initiate_2fa_phone_confirmation = MethodType(
        lambda _self, _token, _url: ("auth", "challenge"), flow
    )
    flow._confirm_2fa_phone_confirmation = MethodType(
        lambda _self, _token, _url, _auth, _challenge, code: code == "739104", flow
    )
    monkeypatch.setattr(web, "SMSBOWER_PHONE_CLIENT", ClientStub())

    flow._confirm_phone_with_retry("token", "https://paypal.test/signup")

    assert ("wait", "70-a", 70) in events
    assert ("finalize", "70-a", False) in events
    assert ("acquire", "70-b", "TH", 1.0) in events
    assert events[-1] == ("finalize", "70-b", True)


def test_paypal_ui_exposes_smsbower_provider_controls():
    root = Path(__file__).resolve().parents[1]
    page = (root / "web_static" / "index.html").read_text(encoding="utf-8")
    script = (root / "web_static" / "app.js").read_text(encoding="utf-8")

    assert 'id="smsProvider"' in page
    assert 'value="smsbower"' in page
    assert 'id="smsbowerPaymentService"' in page
    assert 'id="smsbowerCountry"' in page
    assert 'id="smsbowerMaxPrice"' in page
    assert "验证码平台设置" in page
    assert "支付设置" in page
    assert "/smsbower/status?country=" in script
    assert "sms_provider: smsProvider" in script
    assert "sms_service: smsService" in script
    assert "sms_country: automaticSms ? smsCountry" in script
    assert "sms_max_price: automaticSms ? smsMaxPrice" in script


def test_one_click_handoff_hides_manual_settings_and_loads_requested_job():
    root = Path(__file__).resolve().parents[1]
    page = (root / "web_static" / "index.html").read_text(encoding="utf-8")
    script = (root / "web_static" / "app.js").read_text(encoding="utf-8")
    styles = (root / "web_static" / "styles.css").read_text(encoding="utf-8")

    assert "new URLSearchParams(location.search).get('job')" in script
    assert "document.body.classList.add('auto-handoff-mode')" in script
    assert "const initialJob =" in script
    assert ".auto-handoff-mode .protocol-hero" in styles
    assert ".auto-handoff-mode .form-panel{display:none}" in styles
    assert "20260814-auto-handoff01" in page
