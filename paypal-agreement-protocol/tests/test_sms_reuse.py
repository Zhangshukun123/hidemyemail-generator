import threading
from types import MethodType

import web
from paypal.hero_sms import HeroSmsPhoneClient
from paypal.sms_reuse import (
    HERO_SMS_ACTIVATION_TTL_SECONDS,
    ReusableSmsActivationModel,
    SmsActivationReusePresenter,
)
from paypal.smsbower import SMSBowerPhoneActivation


class FakeClock:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


class ViewStub:
    def __init__(self):
        self.lease = None
        self.events = []

    def attach_sms_activation_lease(self, lease):
        self.lease = lease
        self.events.append(("attach", lease.activation.activation_id, lease.reused))

    def take_sms_activation_lease(self):
        lease, self.lease = self.lease, None
        return lease

    def set_status(self, status, stage=None):
        self.events.append(("stage", status, stage))

    def add_log(self, level, message, **_kwargs):
        self.events.append(("log", level, message))


class ClientStub:
    provider_label = "HeroSMS"

    def __init__(self, activations):
        self.activations = list(activations)
        self.events = []

    def acquire_phone(self, country, *, max_price):
        activation = self.activations.pop(0)
        self.events.append(("acquire", activation.activation_id, country, max_price))
        return activation

    def request_another(self, activation):
        self.events.append(("another", activation.activation_id))

    def mark_sent(self, activation):
        self.events.append(("mark", activation.activation_id))

    def wait_for_code(self, activation, **_kwargs):
        self.events.append(("wait", activation.activation_id))
        return "739104"

    def complete(self, activation):
        self.events.append(("complete", activation.activation_id))

    def cancel(self, activation):
        self.events.append(("cancel", activation.activation_id))


def hero_activation(activation_id, phone):
    return SMSBowerPhoneActivation(
        activation_id,
        phone,
        "GB",
        provider="hero-sms",
    )


def acquire(presenter, view, client, *, owner="owner-a"):
    return presenter.acquire(
        view,
        client,
        owner_id=owner,
        provider="hero-sms",
        service="paypal",
        country="GB",
        max_price=1.5,
    )


def test_second_account_reuses_unexpired_hero_sms_number():
    clock = FakeClock()
    model = ReusableSmsActivationModel(clock=clock)
    presenter = SmsActivationReusePresenter(model)
    original = hero_activation("hero-old", "+447700900123")
    client = ClientStub([original])
    first = ViewStub()
    second = ViewStub()

    assert acquire(presenter, first, client) == original
    assert presenter.finalize(first, client, success=True) is True
    clock.advance(HERO_SMS_ACTIVATION_TTL_SECONDS - 1)

    reused = acquire(presenter, second, client)

    assert reused == original
    assert second.lease.activation.phone == "+447700900123"
    assert second.lease.reused is True
    assert [event for event in client.events if event[0] == "acquire"] == [
        ("acquire", "hero-old", "GB", 1.5)
    ]
    assert ("another", "hero-old") in client.events
    assert not any(event[0] in {"complete", "cancel"} for event in client.events)
    assert presenter.finalize(second, client, success=True) is True


def test_reusable_number_is_isolated_from_a_different_owner():
    model = ReusableSmsActivationModel()
    presenter = SmsActivationReusePresenter(model)
    original = hero_activation("hero-owner-a", "+447700900123")
    other = hero_activation("hero-owner-b", "+447700900124")
    client = ClientStub([original, other])
    owner_a_first = ViewStub()
    owner_b = ViewStub()
    owner_a_second = ViewStub()

    acquire(presenter, owner_a_first, client, owner="owner-a")
    presenter.finalize(owner_a_first, client, success=True)

    assert acquire(presenter, owner_b, client, owner="owner-b") == other
    assert acquire(presenter, owner_a_second, client, owner="owner-a") == original

    assert owner_b.lease.reused is False
    assert owner_a_second.lease.reused is True
    assert ("another", "hero-owner-a") in client.events
    assert ("another", "hero-owner-b") not in client.events
    presenter.finalize(owner_b, client, success=True)
    presenter.finalize(owner_a_second, client, success=True)


def test_expired_hero_sms_number_is_completed_then_replaced():
    clock = FakeClock()
    model = ReusableSmsActivationModel(clock=clock)
    presenter = SmsActivationReusePresenter(model)
    original = hero_activation("hero-old", "+447700900123")
    replacement = hero_activation("hero-new", "+447700900124")
    client = ClientStub([original, replacement])
    first = ViewStub()
    second = ViewStub()

    acquire(presenter, first, client)
    presenter.finalize(first, client, success=True)
    clock.advance(HERO_SMS_ACTIVATION_TTL_SECONDS)

    assert acquire(presenter, second, client) == replacement
    assert ("complete", "hero-old") in client.events
    assert ("another", "hero-old") not in client.events
    assert [event[1] for event in client.events if event[0] == "acquire"] == [
        "hero-old",
        "hero-new",
    ]
    presenter.finalize(second, client, success=False)


def test_concurrent_second_account_waits_then_claims_the_same_number():
    model = ReusableSmsActivationModel(wait_interval_seconds=0.01)
    presenter = SmsActivationReusePresenter(model)
    original = hero_activation("hero-shared", "+447700900123")
    client = ClientStub([original])
    first = ViewStub()
    second = ViewStub()
    second_started = threading.Event()
    result = []

    acquire(presenter, first, client)

    def acquire_second():
        second_started.set()
        result.append(acquire(presenter, second, client))

    thread = threading.Thread(target=acquire_second)
    thread.start()
    assert second_started.wait(1)
    thread.join(timeout=0.05)
    assert thread.is_alive()
    assert len([event for event in client.events if event[0] == "acquire"]) == 1

    presenter.finalize(first, client, success=True)
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert result == [original]
    assert second.lease.reused is True
    assert ("another", "hero-shared") in client.events
    presenter.finalize(second, client, success=True)


def test_busy_lease_expiry_allows_new_number_without_late_finalize_corruption():
    clock = FakeClock()
    model = ReusableSmsActivationModel(clock=clock)
    presenter = SmsActivationReusePresenter(model)
    original = hero_activation("hero-stalled", "+447700900123")
    replacement = hero_activation("hero-current", "+447700900124")
    client = ClientStub([original, replacement])
    stalled = ViewStub()
    current = ViewStub()
    next_account = ViewStub()

    acquire(presenter, stalled, client)
    clock.advance(HERO_SMS_ACTIVATION_TTL_SECONDS)

    assert acquire(presenter, current, client) == replacement
    assert current.lease.activation == replacement
    assert presenter.finalize(stalled, client, success=True) is True
    assert current.lease.activation == replacement
    assert ("complete", "hero-stalled") in client.events
    assert presenter.finalize(current, client, success=True) is True
    assert acquire(presenter, next_account, client) == replacement
    assert next_account.lease.reused is True
    assert presenter.finalize(next_account, client, success=True) is True


def test_cached_request_crossing_expiry_rejoins_current_generation_without_extra_buy():
    class BlockingClient(ClientStub):
        def __init__(self, activations):
            super().__init__(activations)
            self.old_request_started = threading.Event()
            self.release_old_request = threading.Event()

        def request_another(self, activation):
            super().request_another(activation)
            if activation.activation_id == "hero-old":
                self.old_request_started.set()
                assert self.release_old_request.wait(1)

    clock = FakeClock()
    model = ReusableSmsActivationModel(clock=clock, wait_interval_seconds=0.01)
    presenter = SmsActivationReusePresenter(model)
    original = hero_activation("hero-old", "+447700900123")
    replacement = hero_activation("hero-current", "+447700900124")
    client = BlockingClient([original, replacement])
    first = ViewStub()
    crossing = ViewStub()
    current = ViewStub()
    result = []

    acquire(presenter, first, client)
    presenter.finalize(first, client, success=True)

    thread = threading.Thread(
        target=lambda: result.append(acquire(presenter, crossing, client))
    )
    thread.start()
    assert client.old_request_started.wait(1)
    clock.advance(HERO_SMS_ACTIVATION_TTL_SECONDS)

    assert acquire(presenter, current, client) == replacement
    client.release_old_request.set()
    thread.join(timeout=0.05)

    assert thread.is_alive()
    assert [event[1] for event in client.events if event[0] == "acquire"] == [
        "hero-old",
        "hero-current",
    ]

    presenter.finalize(current, client, success=True)
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert result == [replacement]
    assert crossing.lease.reused is True
    assert presenter.finalize(crossing, client, success=True) is True


def test_paypal_send_failure_discards_reused_number_before_getting_new(
    monkeypatch,
):
    clock = FakeClock()
    model = ReusableSmsActivationModel(clock=clock)
    presenter = SmsActivationReusePresenter(model)
    original = hero_activation("hero-old", "+447700900123")
    replacement = hero_activation("hero-new", "+447700900124")
    client = ClientStub([original, replacement])
    monkeypatch.setattr(web, "SMS_ACTIVATION_MODEL", model)
    monkeypatch.setattr(web, "SMS_ACTIVATION_PRESENTER", presenter)
    monkeypatch.setattr(web, "HERO_SMS_PHONE_CLIENT", client)

    first = web.WebJob(
        id="first-account",
        owner_device_id="a" * 32,
        ba_token="BA-FIRSTACCOUNT",
        phone="",
        country="GB",
        sms_provider="hero-sms",
        sms_country="GB",
        sms_max_price=1.5,
    )
    web.acquire_job_sms_activation(first, client, country="GB")
    assert first.finalize_sms_activation(success=True) is True

    second = web.WebJob(
        id="second-account",
        owner_device_id="a" * 32,
        ba_token="BA-SECONDACCOUNT",
        phone="",
        country="GB",
        sms_provider="hero-sms",
        sms_country="GB",
        sms_max_price=1.5,
    )
    assert web.acquire_job_sms_activation(second, client, country="GB") == original
    assert second.to_dict(include_logs=False)["sms_activation_reused"] is True

    flow = web.WebPayPalFlow.__new__(web.WebPayPalFlow)
    flow.job = second
    flow.country = "GB"
    send_attempts = []
    flow._monitor_phone_without_sms = MethodType(
        lambda _self, _phone, attempt=0: second.set_phone_validation(
            "format_valid", "ok", attempt=attempt, format_valid=True
        ),
        flow,
    )

    def initiate(_self, _token, _url):
        send_attempts.append(second.sms_activation().activation_id)
        if len(send_attempts) == 1:
            raise RuntimeError("send failed")
        return "auth", "challenge"

    flow._initiate_2fa_phone_confirmation = MethodType(initiate, flow)
    flow._confirm_2fa_phone_confirmation = MethodType(
        lambda _self, _token, _url, _auth, _challenge, code: code == "739104",
        flow,
    )
    flow._masked_phone = MethodType(lambda _self: "+44******", flow)

    flow._confirm_phone_with_retry("token", "https://paypal.test/signup")

    old_complete = client.events.index(("complete", "hero-old"))
    new_acquire = client.events.index(("acquire", "hero-new", "GB", 1.5))
    assert old_complete < new_acquire
    assert send_attempts == ["hero-old", "hero-new"]
    assert ("wait", "hero-old") not in client.events
    assert ("wait", "hero-new") in client.events
    assert second.phone_verification["status"] == "confirmed"
    assert second.phone == replacement.phone


def test_transient_cleanup_failure_is_retained_and_retried_before_new_number():
    class FlakyCleanupClient(ClientStub):
        def __init__(self, activations):
            super().__init__(activations)
            self.cancel_attempts = 0

        def cancel(self, activation):
            self.cancel_attempts += 1
            self.events.append(("cancel", activation.activation_id))
            if self.cancel_attempts == 1:
                raise RuntimeError("temporary cleanup failure")

    model = ReusableSmsActivationModel()
    presenter = SmsActivationReusePresenter(model)
    original = hero_activation("hero-orphan", "+447700900123")
    replacement = hero_activation("hero-new", "+447700900124")
    client = FlakyCleanupClient([original, replacement])
    failed = ViewStub()
    next_account = ViewStub()

    acquire(presenter, failed, client)
    assert presenter.finalize(failed, client, success=False) is True
    assert model.pending_cleanup_count() == 1

    assert acquire(presenter, next_account, client) == replacement

    assert model.pending_cleanup_count() == 0
    assert client.cancel_attempts == 2
    assert [event[1] for event in client.events if event[0] == "acquire"] == [
        "hero-orphan",
        "hero-new",
    ]
    presenter.finalize(next_account, client, success=False)


def test_hero_sms_request_another_uses_status_three():
    calls = []

    def requester(params):
        calls.append(dict(params))
        return "ACCESS_RETRY_GET"

    client = HeroSmsPhoneClient(api_key="test-key", requester=requester)
    activation = hero_activation("hero-repeat", "+447700900123")

    client.request_another(activation)

    assert calls == [
        {
            "api_key": "test-key",
            "action": "setStatus",
            "id": "hero-repeat",
            "status": 3,
        }
    ]
