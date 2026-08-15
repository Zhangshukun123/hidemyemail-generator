from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .inbox import connect_db
from .registration_inventory_sync import (
    export_inventory_record,
    import_inventory_records,
)


REGISTRATION_INVENTORY_CLAIM_PREFIX = "registration_inventory_claim:"
DEFAULT_LEASE_SECONDS = 10 * 60
ACTIVE_LEASE_STATUS = "active"
TERMINAL_LEASE_STATUSES = {"succeeded", "failed", "expired"}


def _as_utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return _as_utc(value).isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _account_has_completed_registration(value: Any) -> bool:
    try:
        account = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(account, dict):
        return False
    return bool(
        str(account.get("access_token") or account.get("accessToken") or "").strip()
        or account.get("session")
        or str(account.get("session_json") or "").strip()
        or account.get("cookies")
        or str(account.get("cookies_json") or "").strip()
        or str(account.get("storage_state_json") or "").strip()
    )


def _init_lease_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS registration_inventory_leases (
            lease_id TEXT PRIMARY KEY,
            email TEXT NOT NULL COLLATE NOCASE,
            client_id TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            leased_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            registration_inventory_one_active_lease_per_email
        ON registration_inventory_leases(email COLLATE NOCASE)
        WHERE status = 'active'
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS registration_inventory_lease_expiry
        ON registration_inventory_leases(status, expires_at)
        """
    )


def _expire_leases(conn, now: datetime) -> int:
    now_text = _timestamp(now)
    cursor = conn.execute(
        """
        UPDATE registration_inventory_leases
        SET status = 'expired', completed_at = ?,
            message = CASE WHEN message = '' THEN '客户端 10 分钟内未回执' ELSE message END
        WHERE status = 'active' AND expires_at <= ?
        """,
        (now_text, now_text),
    )
    _reconcile_terminal_addresses(conn, now_text)
    return max(0, int(cursor.rowcount or 0))


def _reconcile_terminal_addresses(conn, now_text: str) -> None:
    """Return failed/expired attempts to inventory; keep successes consumed."""

    conn.execute(
        """
        UPDATE addresses AS address
        SET state = 'unused', updated_at = ?
        WHERE address.state = 'unused'
          AND address.source = 'generated'
          AND NOT EXISTS (
              SELECT 1 FROM settings AS removed
              WHERE removed.key = 'gpt_removed:' || lower(address.email)
          )
          AND EXISTS (
              SELECT 1 FROM registration_inventory_leases AS lease
              WHERE lower(lease.email) = lower(address.email)
                AND lease.status IN ('failed', 'expired')
          )
          AND NOT EXISTS (
              SELECT 1 FROM registration_inventory_leases AS lease
              WHERE lower(lease.email) = lower(address.email)
                AND lease.status = 'succeeded'
          )
          AND NOT EXISTS (
              SELECT 1 FROM registration_inventory_leases AS lease
              WHERE lower(lease.email) = lower(address.email)
                AND lease.status = 'active'
          )
        """,
        (now_text,),
    )
    conn.execute(
        """
        UPDATE addresses AS address
        SET state = 'used', updated_at = ?
        WHERE address.source = 'generated'
          AND address.state = 'unused'
          AND NOT EXISTS (
              SELECT 1 FROM settings AS removed
              WHERE removed.key = 'gpt_removed:' || lower(address.email)
          )
          AND EXISTS (
              SELECT 1 FROM registration_inventory_leases AS lease
              WHERE lower(lease.email) = lower(address.email)
                AND lease.status = 'succeeded'
          )
        """,
        (now_text,),
    )


def expire_inventory_leases(
    db_file: Path, *, now: datetime | None = None
) -> int:
    conn = connect_db(str(db_file))
    try:
        conn.execute("BEGIN IMMEDIATE")
        _init_lease_table(conn)
        expired = _expire_leases(conn, _as_utc(now))
        conn.commit()
        return expired
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _available_rows(conn) -> list[Any]:
    return conn.execute(
        """
        SELECT a.email, account.value AS account_value
        FROM addresses AS a
        LEFT JOIN settings AS account
          ON account.key = 'gpt_account:' || lower(a.email)
        WHERE a.state = 'unused'
          AND a.source = 'generated'
          AND NOT EXISTS (
              SELECT 1 FROM settings AS removed
              WHERE removed.key = 'gpt_removed:' || lower(a.email)
          )
          AND NOT EXISTS (
              SELECT 1 FROM registration_inventory_leases AS lease
              WHERE lower(lease.email) = lower(a.email)
                AND lease.status IN ('active', 'succeeded')
          )
        ORDER BY a.created_at ASC, lower(a.email) ASC
        """
    ).fetchall()


def lease_generated_inventory_email(
    db_file: Path,
    *,
    client_id: str = "",
    label: str = "",
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Atomically lease the oldest available generated address."""

    current = _as_utc(now)
    ttl = max(1, int(lease_seconds))
    conn = connect_db(str(db_file))
    try:
        conn.execute("BEGIN IMMEDIATE")
        _init_lease_table(conn)
        _expire_leases(conn, current)
        email = ""
        for row in _available_rows(conn):
            candidate = str(row["email"] or "").strip().lower()
            if candidate and not _account_has_completed_registration(
                row["account_value"]
            ):
                email = candidate
                break
        if not email:
            conn.commit()
            return None

        lease_id = uuid.uuid4().hex
        leased_at = _timestamp(current)
        expires_at = _timestamp(current + timedelta(seconds=ttl))
        conn.execute(
            """
            INSERT INTO registration_inventory_leases(
                lease_id, email, client_id, label, status, leased_at, expires_at
            ) VALUES (?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                lease_id,
                email,
                str(client_id or "")[:200],
                str(label or "")[:200],
                leased_at,
                expires_at,
            ),
        )
        conn.commit()
        return {
            "leaseId": lease_id,
            "email": email,
            "status": ACTIVE_LEASE_STATUS,
            "leasedAt": leased_at,
            "expiresAt": expires_at,
            "leaseSeconds": ttl,
            "record": export_inventory_record(db_file, email),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_generated_inventory_lease(
    db_file: Path,
    *,
    lease_id: str,
    success: bool,
    email: str = "",
    message: str = "",
    record: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Finish an active lease and mark successful registrations as used."""

    target_lease = str(lease_id or "").strip()
    target_email = str(email or "").strip().lower()
    if not target_lease:
        raise ValueError("leaseId 不能为空")

    current = _as_utc(now)
    conn = connect_db(str(db_file))
    try:
        conn.execute("BEGIN IMMEDIATE")
        _init_lease_table(conn)
        _expire_leases(conn, current)
        row = conn.execute(
            """
            SELECT lease_id, email, status, leased_at, expires_at, completed_at, message
            FROM registration_inventory_leases WHERE lease_id = ?
            """,
            (target_lease,),
        ).fetchone()
        if not row:
            conn.commit()
            return {"ok": False, "status": "not_found", "error": "租约不存在"}

        leased_email = str(row["email"] or "").strip().lower()
        if target_email and target_email != leased_email:
            conn.commit()
            return {"ok": False, "status": "conflict", "error": "租约邮箱不匹配"}
        if record is not None:
            record_email = str(record.get("email") or "").strip().lower()
            if record_email and record_email != leased_email:
                conn.commit()
                return {
                    "ok": False,
                    "status": "conflict",
                    "error": "同步账号与租约邮箱不匹配",
                }
        if row["status"] != ACTIVE_LEASE_STATUS:
            conn.commit()
            return {
                "ok": False,
                "status": str(row["status"]),
                "error": "租约已经结束",
                "email": leased_email,
            }

        outcome = "succeeded" if bool(success) else "failed"
        completed_at = _timestamp(current)
        conn.execute(
            """
            UPDATE registration_inventory_leases
            SET status = ?, completed_at = ?, message = ?
            WHERE lease_id = ? AND status = 'active'
            """,
            (outcome, completed_at, str(message or "")[:1000], target_lease),
        )
        conn.execute(
            """
            UPDATE addresses SET state = ?, updated_at = ?
            WHERE lower(email) = lower(?)
              AND state = 'unused'
              AND NOT EXISTS (
                  SELECT 1 FROM settings AS removed
                  WHERE removed.key = 'gpt_removed:' || lower(addresses.email)
              )
            """,
            (
                "used" if outcome == "succeeded" else "unused",
                completed_at,
                leased_email,
            ),
        )
        conn.commit()
        if record is not None:
            import_inventory_records(db_file, [record])
        return {
            "ok": True,
            "leaseId": target_lease,
            "email": leased_email,
            "status": outcome,
            "completedAt": completed_at,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def registration_inventory_status(
    db_file: Path, *, now: datetime | None = None
) -> dict[str, Any]:
    conn = connect_db(str(db_file))
    try:
        conn.execute("BEGIN IMMEDIATE")
        _init_lease_table(conn)
        expired_now = _expire_leases(conn, _as_utc(now))
        available = sum(
            1
            for row in _available_rows(conn)
            if not _account_has_completed_registration(row["account_value"])
        )
        counts = {
            str(row["status"]): int(row["total"])
            for row in conn.execute(
                """
                SELECT status, COUNT(*) AS total
                FROM registration_inventory_leases GROUP BY status
                """
            ).fetchall()
        }
        conn.commit()
        return {
            "available": available,
            "activeLeases": counts.get("active", 0),
            "succeededLeases": counts.get("succeeded", 0),
            "failedLeases": counts.get("failed", 0),
            "expiredLeases": counts.get("expired", 0),
            "expiredNow": expired_now,
            "leaseSeconds": DEFAULT_LEASE_SECONDS,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Compatibility helpers retained for callers from the previous local-only inventory.
def claim_generated_inventory_email(db_file: Path) -> str:
    lease = lease_generated_inventory_email(db_file, client_id="legacy-local")
    return str((lease or {}).get("email") or "")


def release_generated_inventory_email(db_file: Path, email: str) -> None:
    target = str(email or "").strip().lower()
    if not target:
        return
    conn = connect_db(str(db_file))
    try:
        _init_lease_table(conn)
        row = conn.execute(
            """
            SELECT lease_id FROM registration_inventory_leases
            WHERE lower(email) = lower(?) AND status = 'active'
            ORDER BY leased_at DESC LIMIT 1
            """,
            (target,),
        ).fetchone()
    finally:
        conn.close()
    if row:
        complete_generated_inventory_lease(
            db_file,
            lease_id=str(row["lease_id"]),
            email=target,
            success=False,
            message="兼容调用释放租约",
        )


def available_generated_inventory_count(db_file: Path) -> int:
    return int(registration_inventory_status(db_file)["available"])


def clear_generated_inventory_claims(db_file: Path) -> int:
    conn = connect_db(str(db_file))
    try:
        conn.execute("BEGIN IMMEDIATE")
        _init_lease_table(conn)
        now_text = _timestamp()
        cursor = conn.execute(
            """
            UPDATE registration_inventory_leases
            SET status = 'failed', completed_at = ?, message = '兼容启动清理'
            WHERE status = 'active'
            """,
            (now_text,),
        )
        _reconcile_terminal_addresses(conn, now_text)
        conn.execute(
            "DELETE FROM settings WHERE substr(key, 1, ?) = ?",
            (
                len(REGISTRATION_INVENTORY_CLAIM_PREFIX),
                REGISTRATION_INVENTORY_CLAIM_PREFIX,
            ),
        )
        conn.commit()
        return max(0, int(cursor.rowcount or 0))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


__all__ = [
    "ACTIVE_LEASE_STATUS",
    "DEFAULT_LEASE_SECONDS",
    "REGISTRATION_INVENTORY_CLAIM_PREFIX",
    "TERMINAL_LEASE_STATUSES",
    "available_generated_inventory_count",
    "claim_generated_inventory_email",
    "clear_generated_inventory_claims",
    "complete_generated_inventory_lease",
    "expire_inventory_leases",
    "lease_generated_inventory_email",
    "registration_inventory_status",
    "release_generated_inventory_email",
]
