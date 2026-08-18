from __future__ import annotations

from pathlib import Path

from hidemyemail_generator.registration_proxy import REGISTRATION_PROXY_SETTING_KEY
from hidemyemail_generator.zkgmail import ZKGMAIL_SETTING_KEY
from protocol_registration_server.configuration import (
    ConfigurationTransferPresenter,
    SQLiteSettingsRepository,
)
from protocol_registration_server.network import OFFER_PROXY_SETTING_KEY
from protocol_registration_server.model import RegistrationRunRepository


def test_configuration_transfer_is_allowlisted_and_source_is_read_only(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.db"
    shared_path = tmp_path / "shared.db"
    service_path = tmp_path / "service.db"
    source = SQLiteSettingsRepository(source_path)
    source.write_json(
        REGISTRATION_PROXY_SETTING_KEY,
        {
            "enabled": True,
            "mode": "kookeey",
            "country": "JP",
            "endpoint": "proxy.example:1000",
            "username": "user-policy",
            "password": "secret",
            "rotationCursor": 88,
        },
    )
    source.write_json(ZKGMAIL_SETTING_KEY, {"username": "mail@example.com"})
    source.write_json("unrelated", {"value": "must-not-copy"})

    result = ConfigurationTransferPresenter(
        source=SQLiteSettingsRepository(source_path, read_only=True),
        shared_target=SQLiteSettingsRepository(shared_path),
        service_target=SQLiteSettingsRepository(service_path),
    ).transfer()

    assert result.offer_proxy_copied is True
    assert result.zkgmail_copied is True
    proxy = SQLiteSettingsRepository(service_path).read_json(OFFER_PROXY_SETTING_KEY)
    assert proxy is not None
    assert proxy["mode"] == "kookeey"
    assert proxy["country"] == "US"
    assert proxy["rotationCursor"] == 0
    assert SQLiteSettingsRepository(shared_path).read_json(ZKGMAIL_SETTING_KEY)
    assert SQLiteSettingsRepository(shared_path).read_json("unrelated") is None
    assert SQLiteSettingsRepository(source_path).read_json("unrelated") == {
        "value": "must-not-copy"
    }


def test_registration_run_state_is_persisted(tmp_path: Path) -> None:
    repository = RegistrationRunRepository(tmp_path / "service.db")
    repository.save(
        {
            "id": "run-1",
            "status": "running",
            "running": True,
            "requested": 100,
            "acquired": 10,
            "concurrency": 10,
            "useRegistrationKookeey": True,
            "registrationCountry": "US",
            "startedAt": "2026-08-18T00:00:00+00:00",
        }
    )

    restored = RegistrationRunRepository(tmp_path / "service.db").latest()

    assert restored is not None
    assert restored["id"] == "run-1"
    assert restored["concurrency"] == 10
    assert restored["useRegistrationKookeey"] is True
