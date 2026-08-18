from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hidemyemail_generator.registration_proxy import (
    CARD_LINK_PROXY_SETTING_KEY,
    PROXY_MODE_KOOKEEY,
    REGISTRATION_PROXY_SETTING_KEY,
)
from hidemyemail_generator.zkgmail import ZKGMAIL_SETTING_KEY

from .network import OFFER_PROXY_SETTING_KEY


@dataclass(frozen=True, slots=True)
class ConfigurationTransferResult:
    offer_proxy_copied: bool
    zkgmail_copied: bool

    def to_dict(self) -> dict[str, bool]:
        return {
            "offerProxyCopied": self.offer_proxy_copied,
            "zkgmailCopied": self.zkgmail_copied,
        }


class SQLiteSettingsRepository:
    """Repository used to transfer only explicitly allowed setting records."""

    def __init__(self, db_file: Path, *, read_only: bool = False) -> None:
        self.db_file = Path(db_file).resolve()
        self.read_only = bool(read_only)

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            uri = f"file:{self.db_file.as_posix()}?mode=ro"
            return sqlite3.connect(uri, uri=True, timeout=30)
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_file, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        return connection

    def read_json(self, key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(str(row[0] or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def write_json(self, key: str, value: dict[str, Any]) -> None:
        if self.read_only:
            raise RuntimeError("只读设置仓库不能写入")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, encoded),
            )


class ConfigurationTransferPresenter:
    """MVP Presenter that merges deploy-time configuration without source writes."""

    def __init__(
        self,
        *,
        source: SQLiteSettingsRepository,
        shared_target: SQLiteSettingsRepository,
        service_target: SQLiteSettingsRepository,
    ) -> None:
        self.source = source
        self.shared_target = shared_target
        self.service_target = service_target

    def _offer_proxy(self) -> dict[str, Any] | None:
        for key in (REGISTRATION_PROXY_SETTING_KEY, CARD_LINK_PROXY_SETTING_KEY):
            value = self.source.read_json(key)
            if not value:
                continue
            if all(
                str(value.get(field) or "").strip()
                for field in ("endpoint", "username", "password")
            ):
                return {
                    **value,
                    "enabled": True,
                    "mode": PROXY_MODE_KOOKEEY,
                    "country": "US",
                    "rotationCursor": 0,
                    "lastProxyUrl": "",
                    "lastExitIp": "",
                    "lastExitCountry": "",
                }
        return None

    def transfer(self) -> ConfigurationTransferResult:
        proxy = self._offer_proxy()
        if proxy is not None:
            self.service_target.write_json(OFFER_PROXY_SETTING_KEY, proxy)
        zkgmail = self.source.read_json(ZKGMAIL_SETTING_KEY)
        if zkgmail is not None:
            self.shared_target.write_json(ZKGMAIL_SETTING_KEY, zkgmail)
        return ConfigurationTransferResult(
            offer_proxy_copied=proxy is not None,
            zkgmail_copied=zkgmail is not None,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy standalone server configuration from a source SQLite database"
    )
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--shared-db", required=True, type=Path)
    parser.add_argument("--service-db", required=True, type=Path)
    args = parser.parse_args()
    presenter = ConfigurationTransferPresenter(
        source=SQLiteSettingsRepository(args.source_db, read_only=True),
        shared_target=SQLiteSettingsRepository(args.shared_db),
        service_target=SQLiteSettingsRepository(args.service_db),
    )
    print(json.dumps(presenter.transfer().to_dict(), ensure_ascii=False))


if __name__ == "__main__":
    main()
