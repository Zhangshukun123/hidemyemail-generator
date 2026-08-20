from __future__ import annotations

import asyncio
import json
from pathlib import Path

import protocol_registration_server.presenter as presenter_module
from hidemyemail_generator.protocol_registration import ProtocolRegistrationManager
from protocol_registration_server.presenter import ServerRegistrationPresenter
from protocol_registration_server.settings import ServerSettings


class FakeRegistrationManager:
    instances: list["FakeRegistrationManager"] = []
    wait_for_stop = False

    def __init__(self, **kwargs) -> None:
        self.max_concurrency = kwargs.get("max_concurrency")
        self.state = {
            "runtime": {"available": True, "error": ""},
            "running": False,
            "status": "idle",
            "accounts": [],
            "succeeded": 0,
            "total": 0,
            "completed": 0,
        }
        self.started_emails: list[str] = []
        self.started_concurrency = 0
        self.on_account_finished = None
        self.stop_event = asyncio.Event()
        self.instances.append(self)

    def snapshot(self):
        return dict(self.state)

    def start(self, *, emails: list[str], **kwargs):
        self.started_emails = list(emails)
        self.started_concurrency = int(kwargs.get("concurrency") or 0)
        self.on_account_finished = kwargs.get("on_account_finished")
        self.state.update(running=True, status="running", total=len(emails))
        return self.snapshot()

    async def wait(self):
        if self.wait_for_stop:
            await self.stop_event.wait()
            return {**self.snapshot(), "status": "cancelled", "accounts": []}
        accounts = [{"email": email, "status": "success"} for email in self.started_emails]
        if self.on_account_finished is not None:
            for email in self.started_emails:
                await self.on_account_finished(email, True, "注册完成")
        self.state.update(
            running=False,
            status="completed",
            accounts=accounts,
            succeeded=len(accounts),
            completed=len(accounts),
        )
        return self.snapshot()

    async def stop(self):
        self.state.update(running=False, status="cancelled")
        self.stop_event.set()
        return self.snapshot()

    @staticmethod
    def token_record(token: str):
        return None


def make_presenter(tmp_path: Path, monkeypatch) -> ServerRegistrationPresenter:
    FakeRegistrationManager.instances.clear()
    monkeypatch.setattr(
        presenter_module, "ProtocolRegistrationManager", FakeRegistrationManager
    )
    settings = ServerSettings(
        shared_db=tmp_path / "shared.db",
        service_db=tmp_path / "service.db",
        api_token="a" * 32,
        code_service_token="b" * 32,
    )
    return ServerRegistrationPresenter(settings)


def test_batch_count_is_passed_to_registration_manager(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        FakeRegistrationManager.wait_for_stop = False
        presenter = make_presenter(tmp_path, monkeypatch)

        async def acquire(provider: str, count: int) -> list[str]:
            assert provider == "inventory"
            return [f"account-{index}@example.com" for index in range(count)]

        monkeypatch.setattr(presenter, "_acquire", acquire)
        started = await presenter.start(
            count=3,
            provider="inventory",
            concurrency=10,
            use_registration_kookeey=False,
            registration_country="JP",
            offer_countries=["US", "GB", "DE"],
            check_offer=False,
            setup_credentials=False,
        )
        assert started["service"]["requested"] == 3
        assert presenter._supervisor is not None
        await presenter._supervisor
        assert presenter.manager.started_emails == [
            "account-0@example.com",
            "account-1@example.com",
            "account-2@example.com",
        ]
        assert presenter.manager.started_concurrency == 10
        assert presenter.manager.max_concurrency == 10
        assert started["service"]["concurrency"] == 10
        assert started["service"]["offerCountries"] == ["US", "GB", "DE"]
        assert presenter.snapshot()["runHistory"][0]["requested"] == 3
        assert presenter.snapshot()["service"]["status"] == "completed"
        assert presenter.snapshot()["service"]["verificationCompleted"] == 3

    asyncio.run(scenario())


def test_running_batch_can_be_stopped(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        FakeRegistrationManager.wait_for_stop = True
        presenter = make_presenter(tmp_path, monkeypatch)

        async def acquire(provider: str, count: int) -> list[str]:
            return ["account@example.com"]

        monkeypatch.setattr(presenter, "_acquire", acquire)
        await presenter.start(
            count=1,
            provider="inventory",
            concurrency=1,
            use_registration_kookeey=False,
            registration_country="JP",
            offer_countries=["US", "GB", "DE"],
            check_offer=False,
            setup_credentials=False,
        )
        stopped = await presenter.stop()
        assert stopped["service"]["running"] is False
        assert stopped["service"]["status"] == "cancelled"

    asyncio.run(scenario())


def test_kookeey_registration_uses_selected_country(tmp_path: Path, monkeypatch) -> None:
    class FakeKookeeyStrategy:
        def __init__(self, store, country: str) -> None:
            self.country = country

        @staticmethod
        def public_state():
            return {
                "configured": True,
                "strategy": "server_kookeey",
                "country": "DE",
            }

        @staticmethod
        def next_proxy(*, force: bool = False):
            return "http://kookeey", {"country": "DE"}

    async def scenario() -> None:
        FakeRegistrationManager.wait_for_stop = False
        presenter = make_presenter(tmp_path, monkeypatch)
        monkeypatch.setattr(
            presenter_module,
            "KookeeyRegistrationProxyStrategy",
            FakeKookeeyStrategy,
        )

        async def acquire(provider: str, count: int) -> list[str]:
            return ["account@example.com"]

        monkeypatch.setattr(presenter, "_acquire", acquire)
        started = await presenter.start(
            count=1,
            provider="inventory",
            concurrency=1,
            use_registration_kookeey=True,
            registration_country="DE",
            offer_countries=["US", "GB", "DE"],
            check_offer=False,
            setup_credentials=False,
        )
        assert started["service"]["useRegistrationKookeey"] is True
        assert started["service"]["registrationCountry"] == "DE"
        assert started["registrationProxy"]["strategy"] == "server_kookeey"
        assert presenter._supervisor is not None
        await presenter._supervisor

    asyncio.run(scenario())


def test_standalone_manager_allows_ten_concurrent_registrations(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async def runner(payload, on_event):
            return {
                "status": "success",
                "email": payload["email"],
                "access_token": "header.payload.signature",
                "session_json": json.dumps(
                    {
                        "accessToken": "header.payload.signature",
                        "sessionToken": "session-token",
                    }
                ),
                "storage_state_json": json.dumps({"cookies": [], "origins": []}),
            }

        manager = ProtocolRegistrationManager(
            base_dir=tmp_path,
            db_file=tmp_path / "shared.db",
            worker_runner=runner,
            max_concurrency=10,
        )
        manager.start(
            emails=[f"account-{index}@example.com" for index in range(10)],
            base_url="http://127.0.0.1:18769",
            concurrency=10,
            setup_credentials=False,
        )
        final = await manager.wait()

        assert final["concurrency"] == 10
        assert final["succeeded"] == 10

    asyncio.run(scenario())
