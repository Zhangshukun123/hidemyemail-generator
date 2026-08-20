from __future__ import annotations

import json
import tempfile
from pathlib import Path

from hidemyemail_generator.deleted_email_repository import DeletedEmailRepository
from hidemyemail_generator.inbox import connect_db, upsert_address
from hidemyemail_generator.registration_inventory import (
    lease_generated_inventory_email,
    registration_inventory_status,
)
from hidemyemail_generator.registration_inventory_sync import (
    export_inventory_record,
    import_inventory_records,
)


TARGET = "deleted@icloud.com"


def _add_address(db_file: Path, *, state: str = "unused") -> None:
    conn = connect_db(str(db_file))
    try:
        upsert_address(
            conn,
            TARGET,
            state=state,
            source="generated",
            is_active=True,
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            (
                f"gpt_account:{TARGET}",
                json.dumps({"email": TARGET, "password": "retry-password"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_deleted(db_file: Path) -> None:
    conn = connect_db(str(db_file))
    try:
        DeletedEmailRepository(conn).mark_deleted(
            TARGET,
            removed_at="2026-08-15T04:00:00+00:00",
        )
        conn.commit()
    finally:
        conn.close()


def test_failed_lease_cannot_reactivate_deleted_address() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db_file = Path(temp_dir) / "inventory.db"
        _add_address(db_file)
        lease = lease_generated_inventory_email(db_file)
        assert lease and lease["email"] == TARGET
        _mark_deleted(db_file)

        status = registration_inventory_status(db_file)
        conn = connect_db(str(db_file))
        try:
            address = conn.execute(
                "SELECT state, is_active FROM addresses WHERE email = ?", (TARGET,)
            ).fetchone()
            active_leases = conn.execute(
                """
                SELECT COUNT(*) AS total FROM registration_inventory_leases
                WHERE lower(email) = lower(?) AND status = 'active'
                """,
                (TARGET,),
            ).fetchone()["total"]
        finally:
            conn.close()

        assert dict(address) == {"state": "trash", "is_active": 0}
        assert active_leases == 0
        assert status["available"] == 0
        assert lease_generated_inventory_email(db_file) is None


def test_stale_sync_record_cannot_overwrite_local_tombstone() -> None:
    with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
        stale_db = Path(source_dir) / "stale.db"
        deleted_db = Path(target_dir) / "deleted.db"
        _add_address(stale_db)
        _add_address(deleted_db)
        _mark_deleted(deleted_db)

        stale_record = export_inventory_record(stale_db, TARGET)
        assert stale_record is not None and stale_record["removed"] is None
        import_inventory_records(deleted_db, [stale_record])

        conn = connect_db(str(deleted_db))
        try:
            address = conn.execute(
                "SELECT state, is_active FROM addresses WHERE email = ?", (TARGET,)
            ).fetchone()
            account = conn.execute(
                "SELECT 1 FROM settings WHERE key = ?", (f"gpt_account:{TARGET}",)
            ).fetchone()
            removed = conn.execute(
                "SELECT 1 FROM settings WHERE key = ?", (f"gpt_removed:{TARGET}",)
            ).fetchone()
        finally:
            conn.close()

        assert dict(address) == {"state": "trash", "is_active": 0}
        assert account is None
        assert removed is not None


def test_tombstone_sync_permanently_excludes_remote_inventory() -> None:
    with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as remote_dir:
        source_db = Path(source_dir) / "source.db"
        remote_db = Path(remote_dir) / "remote.db"
        _add_address(source_db)
        _mark_deleted(source_db)
        _add_address(remote_db)

        record = export_inventory_record(source_db, TARGET)
        assert record is not None and record["removed"]["email"] == TARGET
        import_inventory_records(remote_db, [record])

        remote_record = export_inventory_record(remote_db, TARGET)
        assert remote_record is not None
        assert remote_record["address"]["state"] == "trash"
        assert remote_record["address"]["is_active"] == 0
        assert remote_record["account"] is None
        assert remote_record["removed"]["email"] == TARGET
        assert lease_generated_inventory_email(remote_db) is None
