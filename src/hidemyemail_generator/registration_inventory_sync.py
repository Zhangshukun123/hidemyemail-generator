from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .deleted_email_repository import DeletedEmailRepository
from .inbox import ADDRESS_STATES, connect_db, utc_now


SYNC_SCHEMA_VERSION = 1
ADDRESS_SYNC_FIELDS = (
    "email",
    "label",
    "state",
    "source",
    "note",
    "is_active",
    "batch_id",
    "created_at",
    "updated_at",
)


def _normalize_email(value: Any) -> str:
    email = str(value or "").strip().lower()
    if not email or len(email) > 320 or "@" not in email:
        raise ValueError("邮箱地址无效")
    return email


def _parse_account(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _account_completed(account: dict[str, Any] | None) -> bool:
    if not isinstance(account, dict):
        return False
    return bool(
        str(account.get("access_token") or account.get("accessToken") or "").strip()
        or account.get("session")
        or str(account.get("session_json") or account.get("sessionJson") or "").strip()
        or account.get("cookies")
        or str(account.get("cookies_json") or "").strip()
        or str(account.get("storage_state_json") or "").strip()
    )


def _serialize_address(row: Any) -> dict[str, Any]:
    return {field: row[field] for field in ADDRESS_SYNC_FIELDS}


def export_inventory_records(
    db_file: Path, *, emails: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Export every address property and the complete associated account record."""

    requested = None
    if emails is not None:
        requested = {
            _normalize_email(email)
            for email in emails
            if str(email or "").strip()
        }
        if not requested:
            return []

    conn = connect_db(str(db_file))
    try:
        address_rows = conn.execute(
            f"SELECT {', '.join(ADDRESS_SYNC_FIELDS)} FROM addresses"
        ).fetchall()
        account_rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'gpt_account:%'"
        ).fetchall()
        removed_rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'gpt_removed:%'"
        ).fetchall()
    finally:
        conn.close()

    addresses = {
        str(row["email"] or "").strip().lower(): _serialize_address(row)
        for row in address_rows
        if str(row["email"] or "").strip()
    }
    accounts: dict[str, dict[str, Any]] = {}
    for row in account_rows:
        email = str(row["key"] or "").removeprefix("gpt_account:").strip().lower()
        account = _parse_account(row["value"])
        if email and account is not None:
            accounts[email] = account

    removed: dict[str, dict[str, Any]] = {}
    for row in removed_rows:
        email = str(row["key"] or "").removeprefix("gpt_removed:").strip().lower()
        tombstone = _parse_account(row["value"])
        if email:
            removed[email] = {
                "email": email,
                "removed_at": str((tombstone or {}).get("removed_at") or ""),
                "reason": str((tombstone or {}).get("reason") or "邮箱已删除")[:1000],
            }

    all_emails = set(addresses) | set(accounts) | set(removed)
    if requested is not None:
        all_emails &= requested
    return [
        {
            "email": email,
            "address": addresses.get(email),
            "account": accounts.get(email),
            "removed": removed.get(email),
        }
        for email in sorted(all_emails)
    ]


def export_inventory_record(db_file: Path, email: str) -> dict[str, Any] | None:
    records = export_inventory_records(db_file, emails=[email])
    return records[0] if records else None


def _merged_account(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
    email: str,
) -> dict[str, Any]:
    existing = dict(existing or {})
    incoming = dict(incoming)
    existing_updated = _parse_timestamp(existing.get("updated_at"))
    incoming_updated = _parse_timestamp(incoming.get("updated_at"))
    if existing_updated and incoming_updated and existing_updated > incoming_updated:
        merged = {**incoming, **existing}
    else:
        merged = {**existing, **incoming}
    merged["email"] = email
    return merged


def import_inventory_records(
    db_file: Path, records: Iterable[dict[str, Any]]
) -> dict[str, int]:
    """Idempotently merge complete mailbox/account records into one database."""

    normalized: list[
        tuple[
            str,
            dict[str, Any] | None,
            dict[str, Any] | None,
            dict[str, Any] | None,
        ]
    ] = []
    seen: set[str] = set()
    for raw_record in records:
        if not isinstance(raw_record, dict):
            raise ValueError("同步记录必须是对象")
        address = raw_record.get("address")
        if address is not None and not isinstance(address, dict):
            raise ValueError("address 必须是对象或 null")
        account = raw_record.get("account")
        if account is not None and not isinstance(account, dict):
            raise ValueError("account 必须是对象或 null")
        removed = raw_record.get("removed")
        if removed is not None and not isinstance(removed, dict):
            raise ValueError("removed 必须是对象或 null")
        email = _normalize_email(
            raw_record.get("email")
            or (address or {}).get("email")
            or (account or {}).get("email")
        )
        if email in seen:
            raise ValueError(f"同步批次包含重复邮箱：{email}")
        seen.add(email)
        normalized.append(
            (
                email,
                dict(address) if address else None,
                dict(account) if account else None,
                dict(removed) if removed else None,
            )
        )

    conn = connect_db(str(db_file))
    address_count = 0
    account_count = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        deleted_repository = DeletedEmailRepository(conn)
        for email, address, account, removed in normalized:
            if removed is not None:
                deleted_repository.mark_deleted(
                    email,
                    reason=str(removed.get("reason") or "邮箱已删除"),
                    removed_at=str(removed.get("removed_at") or ""),
                )
                continue
            if deleted_repository.contains(email):
                # A stale workstation/server snapshot must never override an
                # explicit deletion tombstone.  Restores remove the tombstone
                # through the existing account-save flow before syncing.
                continue
            existing_address = conn.execute(
                f"SELECT {', '.join(ADDRESS_SYNC_FIELDS)} FROM addresses WHERE lower(email) = lower(?)",
                (email,),
            ).fetchone()
            existing_account_row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (f"gpt_account:{email}",),
            ).fetchone()
            existing_account = (
                _parse_account(existing_account_row["value"])
                if existing_account_row
                else None
            )
            merged_account = (
                _merged_account(existing_account, account, email)
                if account is not None
                else existing_account
            )

            if (
                address is not None
                or existing_address is None
                or _account_completed(merged_account)
            ):
                values = dict(_serialize_address(existing_address)) if existing_address else {}
                if address:
                    values.update(
                        {
                            field: address[field]
                            for field in ADDRESS_SYNC_FIELDS
                            if field in address
                        }
                    )
                now = utc_now()
                values["email"] = email
                values["label"] = values.get("label")
                state = str(values.get("state") or "unused")
                if state not in ADDRESS_STATES:
                    raise ValueError(f"邮箱状态无效：{state}")
                if (
                    (existing_address and existing_address["state"] == "used")
                    or _account_completed(merged_account)
                ):
                    state = "used"
                values["state"] = state
                values["source"] = str(values.get("source") or "remote")
                values["note"] = values.get("note")
                is_active = values.get("is_active")
                values["is_active"] = (
                    None if is_active is None else int(bool(is_active))
                )
                values["batch_id"] = values.get("batch_id")
                values["created_at"] = str(values.get("created_at") or now)
                values["updated_at"] = str(values.get("updated_at") or now)
                conn.execute(
                    """
                    INSERT INTO addresses(
                        email, label, state, source, note, is_active, batch_id,
                        created_at, updated_at
                    ) VALUES (
                        :email, :label, :state, :source, :note, :is_active,
                        :batch_id, :created_at, :updated_at
                    )
                    ON CONFLICT(email) DO UPDATE SET
                        label = excluded.label,
                        state = excluded.state,
                        source = excluded.source,
                        note = excluded.note,
                        is_active = excluded.is_active,
                        batch_id = excluded.batch_id,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at
                    """,
                    values,
                )
                address_count += 1

            if account is not None and merged_account is not None:
                conn.execute(
                    """
                    INSERT INTO settings(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (
                        f"gpt_account:{email}",
                        json.dumps(merged_account, ensure_ascii=False),
                    ),
                )
                conn.execute(
                    "DELETE FROM settings WHERE key = ?", (f"gpt_removed:{email}",)
                )
                account_count += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "records": len(normalized),
        "addresses": address_count,
        "accounts": account_count,
    }


__all__ = [
    "ADDRESS_SYNC_FIELDS",
    "SYNC_SCHEMA_VERSION",
    "export_inventory_record",
    "export_inventory_records",
    "import_inventory_records",
]
