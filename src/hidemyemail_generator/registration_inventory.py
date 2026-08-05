from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .inbox import connect_db


REGISTRATION_INVENTORY_CLAIM_PREFIX = "registration_inventory_claim:"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _account_has_session(value: Any) -> bool:
    try:
        account = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(account, dict):
        return False
    session = account.get("session")
    if not isinstance(session, dict):
        session = {}
    return bool(
        str(
            session.get("accessToken")
            or session.get("access_token")
            or account.get("access_token")
            or account.get("accessToken")
            or ""
        ).strip()
    )


def claim_generated_inventory_email(db_file: Path) -> str:
    """Atomically claim the oldest generated, unused address without a Session."""

    conn = connect_db(str(db_file))
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT a.email, account.value AS account_value
            FROM addresses AS a
            LEFT JOIN settings AS account
              ON account.key = 'gpt_account:' || lower(a.email)
            LEFT JOIN settings AS claim
              ON claim.key = ? || lower(a.email)
            WHERE a.state = 'unused'
              AND a.source = 'generated'
              AND claim.key IS NULL
            ORDER BY a.created_at ASC, lower(a.email) ASC
            """,
            (REGISTRATION_INVENTORY_CLAIM_PREFIX,),
        ).fetchall()
        for row in rows:
            email = str(row["email"] or "").strip().lower()
            if not email or _account_has_session(row["account_value"]):
                continue
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                (
                    f"{REGISTRATION_INVENTORY_CLAIM_PREFIX}{email}",
                    json.dumps({"claimedAt": _utc_now()}, separators=(",", ":")),
                ),
            )
            conn.commit()
            return email
        conn.commit()
        return ""
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_generated_inventory_email(db_file: Path, email: str) -> None:
    target = str(email or "").strip().lower()
    if not target:
        return
    conn = connect_db(str(db_file))
    try:
        conn.execute(
            "DELETE FROM settings WHERE key = ?",
            (f"{REGISTRATION_INVENTORY_CLAIM_PREFIX}{target}",),
        )
        conn.commit()
    finally:
        conn.close()


def available_generated_inventory_count(db_file: Path) -> int:
    """Return generated/unused addresses that can be claimed for registration."""

    conn = connect_db(str(db_file))
    try:
        rows = conn.execute(
            """
            SELECT a.email, account.value AS account_value, claim.key AS claim_key
            FROM addresses AS a
            LEFT JOIN settings AS account
              ON account.key = 'gpt_account:' || lower(a.email)
            LEFT JOIN settings AS claim
              ON claim.key = ? || lower(a.email)
            WHERE a.state = 'unused'
              AND a.source = 'generated'
            """,
            (REGISTRATION_INVENTORY_CLAIM_PREFIX,),
        ).fetchall()
        return sum(
            1
            for row in rows
            if not row["claim_key"] and not _account_has_session(row["account_value"])
        )
    finally:
        conn.close()


def clear_generated_inventory_claims(db_file: Path) -> int:
    """Release claims left behind by a stopped local web-service process."""

    conn = connect_db(str(db_file))
    try:
        cursor = conn.execute(
            "DELETE FROM settings WHERE substr(key, 1, ?) = ?",
            (
                len(REGISTRATION_INVENTORY_CLAIM_PREFIX),
                REGISTRATION_INVENTORY_CLAIM_PREFIX,
            ),
        )
        conn.commit()
        return max(0, int(cursor.rowcount or 0))
    finally:
        conn.close()


__all__ = [
    "REGISTRATION_INVENTORY_CLAIM_PREFIX",
    "available_generated_inventory_count",
    "claim_generated_inventory_email",
    "clear_generated_inventory_claims",
    "release_generated_inventory_email",
]
