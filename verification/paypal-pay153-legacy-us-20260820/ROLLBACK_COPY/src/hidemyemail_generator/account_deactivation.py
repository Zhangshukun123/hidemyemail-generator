from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Mapping

from .inbox import connect_db


NOTICE_SETTING_PREFIX = "openai_deactivation_notice:"
ASSOCIATED_EMAIL_PATTERN = re.compile(
    r"\bopenai\s+account\s+associated\s+with\s+"
    r"([a-z0-9.!#$%&'*+/=?^_`{|}~-]+@icloud\.com)\b",
    re.IGNORECASE,
)
REQUIRED_BODY_MARKERS = (
    "your account has been deactivated",
    "your account can no longer be used",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _openai_sender(sender: str) -> bool:
    addresses = [address for _, address in getaddresses([str(sender or "")])]
    for address in addresses:
        _, separator, domain = address.strip().lower().rpartition("@")
        if separator and (domain == "openai.com" or domain.endswith(".openai.com")):
            return True
    return False


def extract_deactivated_account_email(
    sender: str,
    subject: str,
    body_preview: str,
) -> str:
    """Return the exact iCloud account named in a genuine-shaped notice."""

    del subject  # The subject varies; the sender and complete body markers do not.
    if not _openai_sender(sender):
        return ""
    body = " ".join(str(body_preview or "").split())
    folded = body.casefold()
    if any(marker not in folded for marker in REQUIRED_BODY_MARKERS):
        return ""
    match = ASSOCIATED_EMAIL_PATTERN.search(body)
    if not match:
        return ""
    return match.group(1).strip().lower()


def deactivation_notice_key(notice: Mapping[str, Any]) -> str:
    identity = "\0".join(
        str(notice.get(field) or "") for field in ("account_key", "folder", "uid")
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{NOTICE_SETTING_PREFIX}{digest}"


def pending_deactivation_notices(db_file: Path) -> list[dict[str, Any]]:
    """Find strict OpenAI deactivation notices not handled in an earlier run."""

    conn = connect_db(str(db_file))
    try:
        processed = {
            str(row["key"])
            for row in conn.execute(
                "SELECT key FROM settings WHERE key LIKE ?",
                (f"{NOTICE_SETTING_PREFIX}%",),
            ).fetchall()
        }
        rows = conn.execute(
            """
            SELECT id, account_key, folder, uid, sender, subject,
                   body_preview, received_at
            FROM messages
            WHERE instr(lower(COALESCE(body_preview, '')),
                        'account has been deactivated') > 0
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    notices: list[dict[str, Any]] = []
    for row in rows:
        notice = dict(row)
        notice_key = deactivation_notice_key(notice)
        if notice_key in processed:
            continue
        email = extract_deactivated_account_email(
            str(notice.get("sender") or ""),
            str(notice.get("subject") or ""),
            str(notice.get("body_preview") or ""),
        )
        if not email:
            continue
        notice.update(email=email, notice_key=notice_key)
        notices.append(notice)
    return notices


def mark_deactivation_notice_processed(
    db_file: Path,
    notice: Mapping[str, Any],
    *,
    email: str,
    status: str,
    detail: str,
    account_record: Mapping[str, Any] | None = None,
) -> None:
    """Persist notice idempotency and, after deletion, the account audit."""

    target = str(email or "").strip().lower()
    processed_at = _utc_now()
    marker = {
        "email": target,
        "status": str(status or "processed"),
        "detail": str(detail or "")[:1000],
        "account_key": str(notice.get("account_key") or ""),
        "folder": str(notice.get("folder") or ""),
        "uid": str(notice.get("uid") or ""),
        "processed_at": processed_at,
    }
    conn = connect_db(str(db_file))
    try:
        if status == "deleted":
            audit: dict[str, Any] = {
                "email": target,
                "removed_at": processed_at,
                "reason": str(detail or "OpenAI account deactivation notice")[:1000],
                "source": "openai_deactivation_email",
            }
            if account_record:
                audit["account_record"] = dict(account_record)
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"gpt_removed:{target}", json.dumps(audit, ensure_ascii=False)),
            )
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (
                deactivation_notice_key(notice),
                json.dumps(marker, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()
