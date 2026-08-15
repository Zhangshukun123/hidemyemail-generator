from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


class DeletedEmailRepository:
    """Persist deletion tombstones so stale inventory data cannot resurrect mailboxes."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @staticmethod
    def normalize_email(email: str) -> str:
        return str(email or "").strip().lower()

    def read(self, email: str) -> dict[str, Any] | None:
        target = self.normalize_email(email)
        if not target:
            return None
        row = self._connection.execute(
            "SELECT value FROM settings WHERE key = ?", (f"gpt_removed:{target}",)
        ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(str(row["value"] or ""))
        except (json.JSONDecodeError, TypeError, ValueError):
            value = None
        return dict(value) if isinstance(value, dict) else {"email": target}

    def contains(self, email: str) -> bool:
        return self.read(email) is not None

    def mark_deleted(
        self,
        email: str,
        *,
        reason: str = "iCloud alias deleted",
        removed_at: str = "",
    ) -> dict[str, Any]:
        target = self.normalize_email(email)
        if not target:
            raise ValueError("邮箱地址不能为空")
        timestamp = str(removed_at or "").strip() or datetime.now(
            timezone.utc
        ).isoformat()
        tombstone = {
            "email": target,
            "removed_at": timestamp,
            "reason": str(reason or "iCloud alias deleted")[:1000],
        }
        self._connection.execute(
            "DELETE FROM settings WHERE key = ?", (f"gpt_account:{target}",)
        )
        self._connection.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (f"gpt_removed:{target}", json.dumps(tombstone, ensure_ascii=False)),
        )
        self._connection.execute(
            """
            UPDATE addresses
            SET state = 'trash', is_active = 0,
                note = 'iCloud alias deleted', updated_at = ?
            WHERE lower(email) = ?
            """,
            (timestamp, target),
        )
        lease_table = self._connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'registration_inventory_leases'
            """
        ).fetchone()
        if lease_table:
            self._connection.execute(
                """
                UPDATE registration_inventory_leases
                SET status = 'failed', completed_at = ?,
                    message = '邮箱已删除，租约已作废'
                WHERE lower(email) = ? AND status = 'active'
                """,
                (timestamp, target),
            )
        return tombstone


__all__ = ["DeletedEmailRepository"]
