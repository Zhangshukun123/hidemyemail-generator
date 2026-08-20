from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.protocol_registration import PROTOCOL_CODE_PREFIX
from protocol_registration_server.model import OfferAccount, OfferPoolRepository
from protocol_registration_server.settings import ServerSettings
from protocol_registration_server.web import create_app


class FakeManager:
    @staticmethod
    def snapshot():
        return {"runtime": {"available": True}, "running": False, "accounts": []}


class FakeCodeClient:
    @staticmethod
    async def fetch(email: str, since: str):
        return 200, "123456"


class FakePresenter:
    def __init__(self, repository: OfferPoolRepository) -> None:
        self.offer_repository = repository
        self.manager = FakeManager()
        self.code_client = FakeCodeClient()
        self.closed = False
        self.last_start = {}
        self.last_refresh = {}

    def snapshot(self):
        return {
            "service": {"status": "idle", "running": False},
            "task": self.manager.snapshot(),
            "offerPool": self.offer_repository.snapshot(),
            "registrationProxy": {},
            "offerProxy": {},
        }

    async def start(self, **kwargs):
        if not 1 <= int(kwargs["count"]) <= 100:
            raise ValueError("注册次数必须是 1–100")
        if not 1 <= int(kwargs["concurrency"]) <= 10:
            raise ValueError("并发注册数必须是 1–10")
        self.last_start = kwargs
        return self.snapshot()

    async def stop(self):
        return self.snapshot()

    async def refresh_offer(self, email: str, countries: list[str]):
        self.last_refresh = {"email": email, "countries": countries}
        item = self.offer_repository.get(email)
        if item is None:
            raise ValueError("优惠记录不存在")
        return item.to_dict()

    @staticmethod
    def token_record(token: str):
        return {"email": "account@example.com", "since": "now"} if token == "code" else None

    async def close(self) -> None:
        self.closed = True


def test_api_authentication_and_account_export(tmp_path: Path) -> None:
    async def scenario() -> None:
        shared = tmp_path / "shared.db"
        service = tmp_path / "service.db"
        repository = OfferPoolRepository(service)
        repository.save(
            OfferAccount(
                email="account@example.com",
                status="offer",
                pool="offer",
                eligible=True,
                checkout_submitted=True,
                checkout_country="DE",
                checkout_currency="EUR",
                checkout_amount_minor="0",
                paypal_available=True,
                checkout_evidence_json=(
                    '[{"exitCountry":"BR","checkoutCountry":"DE",'
                    '"paypalAvailable":true,"amountMinor":"0"}]'
                ),
                checked_at="2026-08-18T00:00:00+00:00",
                registration_ip="203.0.113.40",
                registration_country="DE",
                registration_proxy_mode="kookeey",
            )
        )
        record = {
            "email": "account@example.com",
            "password": "password-value",
            "two_factor": {"enabled": True, "secret": "totp-value"},
            "session": {"accessToken": "access-value", "sessionToken": "session-value"},
            "registration_environment": {
                "exit_ip": "203.0.113.40",
                "exit_country": "DE",
                "proxy_mode": "kookeey",
            },
        }
        with connect_db(str(shared)) as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("gpt_account:account@example.com", json.dumps(record)),
            )
        settings = ServerSettings(
            shared_db=shared,
            service_db=service,
            api_token="a" * 32,
            code_service_token="b" * 32,
        )
        presenter = FakePresenter(repository)
        server = TestServer(create_app(settings, presenter=presenter))
        client = TestClient(server)
        await client.start_server()
        try:
            health = await client.get("/healthz")
            assert health.status == 200
            unauthenticated = await client.get("/api/status")
            assert unauthenticated.status == 401
            authenticated = await client.get(
                "/api/status", headers={"Authorization": f"Bearer {'a' * 32}"}
            )
            assert authenticated.status == 200
            invalid_count = await client.post(
                "/api/tasks/start",
                json={"count": 0, "checkOffer": False, "setupCredentials": False},
                headers={"Authorization": f"Bearer {'a' * 32}"},
            )
            assert invalid_count.status == 400
            valid_start = await client.post(
                "/api/tasks/start",
                json={
                    "count": 10,
                    "concurrency": 10,
                    "useRegistrationKookeey": True,
                    "registrationCountry": "DE",
                    "offerCountries": ["US", "GB", "DE"],
                    "checkOffer": True,
                    "setupCredentials": True,
                },
                headers={"Authorization": f"Bearer {'a' * 32}"},
            )
            assert valid_start.status == 200
            assert presenter.last_start["concurrency"] == 10
            assert presenter.last_start["use_registration_kookeey"] is True
            assert presenter.last_start["registration_country"] == "DE"
            assert presenter.last_start["offer_countries"] == ["US", "GB", "DE"]
            code = await client.get(f"{PROTOCOL_CODE_PREFIX}code")
            assert code.status == 200
            assert await code.text() == "123456"
            accounts = await client.get(
                "/api/accounts?pool=offer&credentials=1",
                headers={"Authorization": f"Bearer {'a' * 32}"},
            )
            payload = await accounts.json()
            assert accounts.status == 200
            assert payload["accounts"][0]["credentials"]["accessToken"] == "access-value"
            assert payload["accounts"][0]["credentials"]["totpSecret"] == "totp-value"
            assert payload["accounts"][0]["registration"]["ip"] == "203.0.113.40"
            refreshed = await client.post(
                "/api/offers/refresh",
                json={
                    "email": "account@example.com",
                    "countries": ["BR", "TH", "DE"],
                },
                headers={"Authorization": f"Bearer {'a' * 32}"},
            )
            assert refreshed.status == 200
            assert (await refreshed.json())["offer"]["pool"] == "offer"
            assert presenter.last_refresh["countries"] == ["BR", "TH", "DE"]
            missing = await client.post(
                "/api/offers/refresh",
                json={"email": "missing@example.com", "countries": ["US"]},
                headers={"Authorization": f"Bearer {'a' * 32}"},
            )
            assert missing.status == 404
            invalid_countries = await client.post(
                "/api/offers/refresh",
                json={"email": "account@example.com", "countries": "US"},
                headers={"Authorization": f"Bearer {'a' * 32}"},
            )
            assert invalid_countries.status == 400
        finally:
            await client.close()
        assert presenter.closed is True

    asyncio.run(scenario())
