import json
import sqlite3
import threading
from pathlib import Path
from types import MethodType
from unittest.mock import patch

import pytest
import httpx

import web
from paypal.smsbower import (
    DEFAULT_OTP_TIMEOUT_SECONDS,
    SMSBowerPhoneActivation,
    SMSBowerPhoneClient,
    SMSBowerPhoneError,
    SMSBOWER_US_VIRTUAL_COUNTRY_ID,
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


def test_us_paypal_prefers_the_virtual_us_number_pool():
    requester = ScriptedRequester(
        {"getNumber": ["ACCESS_NUMBER:912345:+12025550199"]}
    )
    client = SMSBowerPhoneClient(api_key="test-key", requester=requester)

    activation = client.acquire_phone("US", max_price=0.25)

    assert activation.country == "US"
    assert activation.service == "paypal"
    assert activation.is_virtual is True
    assert requester.calls[0]["service"] == "ts"
    assert requester.calls[0]["country"] == SMSBOWER_US_VIRTUAL_COUNTRY_ID
    assert requester.calls[0]["maxPrice"] == "0.25"


def test_us_paypal_falls_back_to_the_standard_us_pool():
    requester = ScriptedRequester(
        {
            "getNumber": [
                "NO_NUMBERS",
                "ACCESS_NUMBER:912346:+12025550200",
            ]
        }
    )
    client = SMSBowerPhoneClient(api_key="test-key", requester=requester)

    activation = client.acquire_phone("US", max_price=3)

    assert activation.is_virtual is False
    assert [call["country"] for call in requester.calls] == [12, 187]
    assert all(call["service"] == "ts" for call in requester.calls)


def test_us_paypal_price_status_selects_the_available_virtual_pool():
    requester = ScriptedRequester(
        {
            "getPrices": [
                json.dumps({"12": {"ts": {"cost": 0.187, "count": 243911}}}),
                json.dumps({"187": {"ts": {"cost": 2.296, "count": 148675}}}),
            ]
        }
    )
    client = SMSBowerPhoneClient(api_key="test-key", requester=requester)

    status = client.price("US")

    assert status["countryId"] == 12
    assert status["virtual"] is True
    assert status["price"] == 0.187
    assert [item["countryId"] for item in status["routes"]] == [12, 187]


def test_payment_sms_timeout_is_60_seconds_for_every_automatic_provider():
    assert DEFAULT_OTP_TIMEOUT_SECONDS == 60
    assert web.AUTO_SMS_OTP_TIMEOUT_SECONDS == 60
    assert web.AUTO_SMS_MAX_PHONE_ATTEMPTS == 10
    assert web.SMSBOWER_OTP_TIMEOUT_SECONDS == 60
    assert web.SMSBOWER_MAX_PHONE_ATTEMPTS == 10
    assert web.SMSBOWER_PHONE_CLIENT.otp_timeout_seconds == 60
    assert web.HERO_SMS_PHONE_CLIENT.otp_timeout_seconds == 60
    assert web.SMSBOWER_COUNTRY_IDS["DE"] == 43
    assert web.HERO_SMS_COUNTRY_IDS["DE"] == 43
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
    assert status["service"] == "paypal"
    assert status["serviceCode"] == "ts"
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
    assert job.sms_service == "paypal"
    assert job.sms_country == "GB"
    assert job.phone == ""
    assert job.sms_max_price == 1.25
    assert job.to_dict(include_logs=False)["sms_auto"] is True
    assert job.to_dict(include_logs=False)["sms_max_price"] == 1.25
    assert job.to_dict(include_logs=False)["phone_validation"]["status"] == "not_started"
    job.set_phone_validation(
        "format_valid",
        "号码国家区号与格式通过本地监测；未向 PayPal 发码",
        attempt=1,
        format_valid=True,
    )
    job.set_phone_verification(
        "code_sent",
        "协议支付已进入 PayPal 短信验证并发送验证码",
        attempt=1,
        paypal_send_accepted=True,
    )
    validation = job.to_dict(include_logs=False)["phone_validation"]
    verification = job.to_dict(include_logs=False)["phone_verification"]
    assert validation["status"] == "format_valid"
    assert validation["format_valid"] is True
    assert verification["status"] == "code_sent"
    assert verification["paypal_send_accepted"] is True
    assert verification["paypal_confirmed"] is False
    web.JOBS.clear()


def test_internal_auto_jobs_share_one_device_without_browser_job_limit(monkeypatch):
    class ClientStub:
        def configured(self):
            return True

    monkeypatch.setattr(web, "SMSBOWER_PHONE_CLIENT", ClientStub())
    web.JOBS.clear()
    try:
        with patch.object(threading.Thread, "start", autospec=True):
            jobs = [
                web.create_job(
                    owner_device_id="c" * 32,
                    ba_token=f"BA-AUTOJOB{index:04d}",
                    phone="",
                    debug=False,
                    max_card_attempts=5,
                    sms_provider="smsbower",
                    sms_service="ts",
                    sms_country="DE",
                    country="DE",
                    proxy_pool=["http://user:pass@127.0.0.1:8888"],
                    exclude_public_metrics=True,
                )
                for index in range(web.MAX_ACTIVE_JOBS_PER_DEVICE + 1)
            ]

        assert len(jobs) == web.MAX_ACTIVE_JOBS_PER_DEVICE + 1
        assert all(job.owner_device_id == "c" * 32 for job in jobs)
    finally:
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

        def set_phone_validation(self, status, _message, **details):
            events.append(("validation", status, details.get("attempt", 0)))

        def set_phone_verification(self, status, _message, **details):
            events.append(("verification", status, details.get("attempt", 0)))

        def finalize_sms_activation(self, *, success):
            events.append(("finalize", success))

    flow = web.WebPayPalFlow.__new__(web.WebPayPalFlow)
    flow.job = JobStub()
    flow.country = "BR"
    flow._update_user_phone = MethodType(lambda _self, phone: events.append(("phone", phone)), flow)
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
    assert events.index(("validation", "format_valid", 1)) < events.index(
        ("verification", "code_sent", 1)
    )
    assert events.index(("verification", "code_sent", 1)) < events.index(("poll", "44"))
    assert ("verification", "confirmed", 1) in events
    assert events[-1] == ("finalize", True)


def test_passive_phone_monitor_does_not_call_paypal_or_send_sms():
    events = []

    class JobStub:
        def set_phone_validation(self, status, _message, **details):
            events.append((status, details.get("format_valid")))

    flow = web.WebPayPalFlow.__new__(web.WebPayPalFlow)
    flow.job = JobStub()
    flow.country = "BR"
    flow._update_user_phone = MethodType(
        lambda _self, phone: events.append(("local_update", phone)), flow
    )
    flow._initiate_2fa_phone_confirmation = MethodType(
        lambda *_args: pytest.fail("passive monitoring must not call PayPal"), flow
    )

    flow._monitor_phone_without_sms("+551199887766", attempt=2)

    assert events == [
        ("local_update", "+551199887766"),
        ("format_valid", True),
    ]


def test_manual_passive_phone_check_normalizes_local_number_without_sms():
    result = web.passive_phone_check("081 234 5678", "TH")

    assert result["valid"] is True
    assert result["country"] == "TH"
    assert result["calling_code"] == "+66"
    assert result["validation_level"] == "country_mobile_pattern"
    assert result["paypal_contacted"] is False
    assert result["sms_sent"] is False
    assert "未发送验证码" in result["message"]


def test_manual_passive_phone_check_rejects_mismatched_country_code():
    with pytest.raises(ValueError, match="国际区号与所选国家不一致"):
        web.passive_phone_check("+447700900123", "TH")


def test_manual_phone_check_api_does_not_create_a_job_or_send_sms():
    server = web.WebThreadingHTTPServer(("127.0.0.1", 0), web.WebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = httpx.post(
            f"http://127.0.0.1:{server.server_port}/api/phone/check",
            json={"country": "PH", "phone": "+639171234567"},
            timeout=5,
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["result"]["valid"] is True
        assert payload["result"]["paypal_contacted"] is False
        assert payload["result"]["sms_sent"] is False
        assert payload["result"]["phone_masked"] != "+639171234567"
        assert web.JOBS == {}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("provider", "client_attribute"),
    (("smsbower", "SMSBOWER_PHONE_CLIENT"), ("hero-sms", "HERO_SMS_PHONE_CLIENT")),
)
def test_automatic_sms_timeout_cancels_then_reacquires(
    monkeypatch, provider, client_attribute
):
    activations = [
        SMSBowerPhoneActivation("60-a", "+66811111111", "TH", provider=provider),
        SMSBowerPhoneActivation("60-b", "+66822222222", "TH", provider=provider),
    ]
    events = []

    class ClientStub:
        provider_label = "HeroSMS" if provider == "hero-sms" else "SMSBower"

        def acquire_phone(self, country, *, max_price):
            item = activations.pop(0)
            events.append(("acquire", item.activation_id, country, max_price))
            return item

        def mark_sent(self, item):
            events.append(("mark", item.activation_id))

        def wait_for_code(self, item, **kwargs):
            events.append(("wait", item.activation_id, kwargs["timeout_seconds"]))
            if item.activation_id == "60-a":
                raise SMSBowerPhoneError(
                    f"{self.provider_label} PayPal 短信验证码等待超时"
                )
            return "739104"

    class JobStub:
        sms_max_price = 1.0
        _cancel_event = threading.Event()

        def __init__(self):
            self.sms_provider = provider
            self.current = None

        def check_cancelled(self):
            return None

        def sms_activation(self):
            return self.current

        def attach_sms_activation(self, activation):
            self.current = activation

        def set_status(self, _status, stage):
            events.append(("stage", stage))

        def set_phone_validation(self, status, _message, **details):
            events.append(("validation", status, details.get("attempt", 0)))

        def set_phone_verification(self, status, _message, **details):
            events.append(("verification", status, details.get("attempt", 0)))

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
    monkeypatch.setattr(web, client_attribute, ClientStub())

    flow._confirm_phone_with_retry("token", "https://paypal.test/signup")

    assert ("wait", "60-a", 60) in events
    assert ("validation", "format_valid", 1) in events
    assert ("verification", "sms_timeout", 1) in events
    assert ("finalize", "60-a", False) in events
    assert ("acquire", "60-b", "TH", 1.0) in events
    assert events.index(("finalize", "60-a", False)) < events.index(
        ("acquire", "60-b", "TH", 1.0)
    )
    assert ("validation", "format_valid", 2) in events
    assert ("verification", "code_sent", 2) in events
    assert ("verification", "confirmed", 2) in events
    assert events[-1] == ("finalize", "60-b", True)


def test_paypal_ui_exposes_provider_controls_and_separate_sms_settings_entry():
    root = Path(__file__).resolve().parents[1]
    page = (root / "web_static" / "index.html").read_text(encoding="utf-8")
    script = (root / "web_static" / "app.js").read_text(encoding="utf-8")

    assert 'id="smsProvider"' in page
    assert 'value="smsbower"' in page
    assert 'value="hero-sms"' in page
    assert 'id="smsSettingsOpen"' in page
    assert 'id="smsSettingsDialog"' in page
    assert 'id="smsDefaultProvider"' in page
    assert 'id="smsbowerApiKey" type="password"' in page
    assert 'id="heroSmsApiKey" type="password"' in page
    assert "60 秒获取不到验证码" in page
    assert 'id="smsbowerPaymentService"' in page
    assert '<option value="paypal">PayPal</option>' in page
    assert 'id="smsbowerCountry"' in page
    assert 'id="smsbowerMaxPrice"' in page
    assert "验证码平台设置" in page
    assert "支付设置" in page
    assert 'id="phoneValidationPanel"' in page
    assert 'id="phoneValidationStatus"' in page
    assert 'id="phoneVerificationPanel"' in page
    assert 'id="phoneCheckCountry"' in page
    assert 'id="phoneCheckInput"' in page
    assert 'id="phoneCheckButton"' in page
    assert 'id="phoneCheckResult"' in page
    assert "号码监测不会发送验证码" in page
    assert "被动监测成功" in script
    assert "PayPal 已发验证码" in script
    assert "api('/phone/check'" in script
    assert "未访问 PayPal，未发送验证码" in script
    assert "/smsbower/status?country=" in script
    assert "/sms/providers?country=" in script
    assert "this.request('/sms/config'" in script
    assert "接码平台状态已同步；API Key 不会回显。" in script
    assert "const automaticSms = smsProvider !== 'manual'" in script
    assert "class SmsSettingsView" in script
    assert "class SmsSettingsPresenter" in script
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
    assert "20260817-phone-check03" in page
