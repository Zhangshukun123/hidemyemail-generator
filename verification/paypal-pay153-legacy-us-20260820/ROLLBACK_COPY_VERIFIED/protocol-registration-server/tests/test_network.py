from __future__ import annotations

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer
from protocol_registration_server.network import (
    CheckoutOfferProbe,
    CodeServiceClient,
    KookeeyOfferView,
    KookeeyRegistrationProxyStrategy,
    ServerAlternatingProxyStrategy,
)


class FakeStore:
    def __init__(self) -> None:
        self.calls = 0

    def next_proxy(self, *, force: bool = False):
        self.calls += 1
        return f"http://clash-{self.calls}", {
            "configured": True,
            "enabled": True,
            "country": "JP",
            "selector": "自动选择",
            "normalEndpoint": "http://127.0.0.1:7899",
        }

    @staticmethod
    def public_state():
        return {
            "configured": True,
            "enabled": True,
            "selector": "自动选择",
            "normalEndpoint": "http://127.0.0.1:7899",
        }

    @staticmethod
    def load():
        return {"lastExitIp": "203.0.113.10", "lastExitCountry": "JP"}


def test_server_proxy_strategy_alternates_clash_and_direct() -> None:
    strategy = ServerAlternatingProxyStrategy(
        FakeStore(), exit_detector=lambda _: ("198.51.100.2", "US")
    )

    routes = [strategy.next_proxy() for _ in range(4)]

    assert [item[0] for item in routes] == [
        "http://clash-1",
        "",
        "http://clash-2",
        "",
    ]
    assert [item[1]["currentRoute"] for item in routes] == [
        "clash",
        "direct",
        "clash",
        "direct",
    ]
    assert strategy.public_state()["nextRoute"] == "clash"
    assert routes[0][1]["exitIp"] == "203.0.113.10"
    assert routes[1][1]["exitIp"] == "198.51.100.2"


def test_forced_proxy_uses_clash_without_changing_alternation() -> None:
    strategy = ServerAlternatingProxyStrategy(
        FakeStore(), exit_detector=lambda _: ("198.51.100.2", "US")
    )

    forced, state = strategy.next_proxy(force=True)
    first, first_state = strategy.next_proxy()

    assert forced == "http://clash-1"
    assert state["currentRoute"] == "clash"
    assert first == "http://clash-2"
    assert first_state["currentRoute"] == "clash"


class FakeKookeeyStore:
    @staticmethod
    def proxy_for_country(country: str, *, mode: str):
        assert country == "DE"
        assert mode == "kookeey"
        return "http://kookeey", {"modes": []}

    @staticmethod
    def public_state():
        return {
            "dynamicEndpoint": "proxy.example:1000",
            "modes": [{"code": "kookeey", "configured": True}],
        }


def test_kookeey_registration_strategy_uses_selected_country_and_records_ip() -> None:
    strategy = KookeeyRegistrationProxyStrategy(
        FakeKookeeyStore(),
        "DE",
        exit_detector=lambda _: ("203.0.113.20", "DE"),
    )

    proxy_url, state = strategy.next_proxy()

    assert proxy_url == "http://kookeey"
    assert state["country"] == "DE"
    assert state["exitIp"] == "203.0.113.20"
    assert strategy.public_state()["exitIpVerified"] is True


def test_checkout_without_paypal_switches_to_de_exit_and_billing(monkeypatch) -> None:
    view = KookeeyOfferView(FakeKookeeyStore())
    calls: list[tuple[str, str]] = []

    def probe(_token: str, *, exit_country: str, checkout_country: str):
        calls.append((exit_country, checkout_country))
        paypal = checkout_country == "DE"
        return CheckoutOfferProbe(
            exit_country=exit_country,
            checkout_country=checkout_country,
            currency="EUR" if checkout_country == "DE" else "BRL",
            amount_minor="0",
            amount_source="total_summary.due",
            paypal_available=paypal,
            checkout_url="https://chatgpt.com/checkout/test",
            payment_methods=("paypal",) if paypal else ("card",),
        )

    monkeypatch.setattr(view, "_check_checkout_once", probe)

    result = view.check_checkout("access-token", "BR")

    assert calls == [("BR", "BR"), ("DE", "DE")]
    assert result.requested_country == "BR"
    assert result.exit_country == "DE"
    assert result.checkout_country == "DE"
    assert result.eligible is True
    assert result.to_dict()["deFallback"] is True


def test_failed_th_checkout_falls_back_to_de(monkeypatch) -> None:
    view = KookeeyOfferView(FakeKookeeyStore())

    def probe(_token: str, *, exit_country: str, checkout_country: str):
        if checkout_country == "TH":
            raise RuntimeError("TH policy unavailable")
        return CheckoutOfferProbe(
            exit_country=exit_country,
            checkout_country=checkout_country,
            currency="EUR",
            amount_minor="0",
            amount_source="invoice.amount_due",
            paypal_available=True,
            checkout_url="https://chatgpt.com/checkout/de",
            payment_methods=("paypal",),
        )

    monkeypatch.setattr(view, "_check_checkout_once", probe)

    result = view.check_checkout("access-token", "TH")

    assert result.requested_country == "TH"
    assert result.exit_country == "DE"
    assert result.checkout_country == "DE"
    assert "回退 DE" in result.fallback_reason


def test_checkout_retries_with_fresh_proxy_before_pending(monkeypatch) -> None:
    view = KookeeyOfferView(FakeKookeeyStore())
    attempts = 0

    def probe(_token: str, *, exit_country: str, checkout_country: str):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("temporary proxy failure")
        return CheckoutOfferProbe(
            exit_country=exit_country,
            checkout_country=checkout_country,
            currency="EUR",
            amount_minor="100",
            amount_source="invoice.amount_due",
            paypal_available=True,
            checkout_url="https://chatgpt.com/checkout/de",
            payment_methods=("paypal",),
        )

    monkeypatch.setattr(view, "_check_checkout_once", probe)

    result = view.check_checkout("access-token", "DE")

    assert attempts == 3
    assert result.amount_minor == "100"


def test_code_service_uses_workbench_import_header() -> None:
    async def scenario() -> None:
        async def code(request: web.Request) -> web.Response:
            assert request.headers.get("X-HME-Import-Token") == "code-token"
            assert request.headers.get("Authorization") is None
            return web.json_response({"ok": True, "code": "123456"})

        app = web.Application()
        app.router.add_post("/api/integrations/workbench/openai-code", code)
        server = TestServer(app)
        await server.start_server()
        try:
            status, value = await CodeServiceClient(
                str(server.make_url("/")), "code-token"
            ).fetch("account@example.com", "now")
        finally:
            await server.close()
        assert status == 200
        assert value == "123456"

    asyncio.run(scenario())
