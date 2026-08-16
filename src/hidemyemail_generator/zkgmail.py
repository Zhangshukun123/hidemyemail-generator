from __future__ import annotations

import asyncio
import imaplib
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Callable

from .inbox import (
    DEFAULT_IMAP_TIMEOUT,
    InboxConfig,
    _sync_folders,
    connect_db,
    extract_verification_code,
    get_message_body,
    header_addresses,
    insert_message,
    mask_account,
    normalize_space,
    parse_received_at,
    upsert_address,
)


ZKGMAIL_SETTING_KEY = "zkgmail_qq_inbox_config_v1"
ZKGMAIL_DOMAIN = "zkgmail.com"
ZKGMAIL_FORWARD_ACCOUNT = "352121354@qq.com"
ZKGMAIL_IMAP_HOST = "imap.qq.com"
ZKGMAIL_IMAP_PORT = 993
ZKGMAIL_FOLDER = "INBOX"
ZKGMAIL_SYNC_INTERVAL_SECONDS = 1.0
ZKGMAIL_MESSAGE_LIMIT = 30
ZKGMAIL_FIRST_NAMES = (
    "james",
    "john",
    "robert",
    "michael",
    "david",
    "william",
    "richard",
    "joseph",
    "thomas",
    "daniel",
    "matthew",
    "anthony",
    "mark",
    "steven",
    "andrew",
    "joshua",
    "mary",
    "patricia",
    "jennifer",
    "linda",
    "elizabeth",
    "barbara",
    "susan",
    "jessica",
    "sarah",
    "karen",
    "emily",
    "emma",
    "olivia",
    "sophia",
)
ZKGMAIL_LAST_NAMES = (
    "smith",
    "johnson",
    "williams",
    "brown",
    "jones",
    "garcia",
    "miller",
    "davis",
    "rodriguez",
    "martinez",
    "wilson",
    "anderson",
    "taylor",
    "thomas",
    "moore",
    "jackson",
    "martin",
    "lee",
    "thompson",
    "white",
)
ZKGMAIL_RECIPIENT_HEADERS = (
    "To",
    "Delivered-To",
    "X-Original-To",
    "Envelope-To",
    "Apparently-To",
    "Original-Recipient",
    "Resent-To",
    "Cc",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _generate_human_local_part() -> str:
    """Return an alphanumeric ASCII name with a short numeric suffix."""

    first_name = secrets.choice(ZKGMAIL_FIRST_NAMES)
    last_name = secrets.choice(ZKGMAIL_LAST_NAMES)
    digit_count = 2 + secrets.randbelow(3)
    minimum = 10 ** (digit_count - 1)
    suffix = minimum + secrets.randbelow(9 * minimum)
    return f"{first_name}{last_name}{suffix}"


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _connect_mailbox(config: InboxConfig):
    return (
        imaplib.IMAP4_SSL(config.host, config.port, timeout=DEFAULT_IMAP_TIMEOUT)
        if config.use_ssl
        else imaplib.IMAP4(config.host, config.port, timeout=DEFAULT_IMAP_TIMEOUT)
    )


def _refresh_selected_mailbox(mailbox) -> None:
    """Ask IMAP to publish pending mailbox changes before searching.

    QQ can briefly keep a newly delivered message out of a selected mailbox's
    search snapshot.  NOOP is the standard, non-mutating way to request any
    pending EXISTS/RECENT updates; unlike opening the message, it does not mark
    mail as read.
    """

    noop = getattr(mailbox, "noop", None)
    if not callable(noop):
        return
    try:
        noop()
    except (imaplib.IMAP4.error, OSError):
        # SEARCH remains useful even when a provider does not support NOOP
        # correctly, so a refresh failure must not hide otherwise visible mail.
        return


def _test_imap_connection(config: InboxConfig) -> None:
    mailbox = _connect_mailbox(config)
    try:
        mailbox.login(config.username, config.password)
        status, _ = mailbox.select(config.folder)
        if status != "OK":
            raise RuntimeError("QQ 邮箱 INBOX 不可用")
    finally:
        try:
            mailbox.logout()
        except Exception:
            pass


def _known_zkgmail_aliases(db_file: Path) -> set[str]:
    conn = connect_db(str(db_file))
    try:
        rows = conn.execute(
            "SELECT email FROM addresses WHERE source = ? AND lower(email) LIKE ?",
            ("zkgmail", f"%@{ZKGMAIL_DOMAIN}"),
        ).fetchall()
    finally:
        conn.close()
    return {str(row["email"] or "").strip().lower() for row in rows}


def _sync_relevant_messages(
    config: InboxConfig,
    db_file: Path,
    aliases: set[str],
    *,
    limit: int = ZKGMAIL_MESSAGE_LIMIT,
) -> int:
    """Store only code messages addressed to known zkgmail aliases."""

    targets = {str(alias or "").strip().lower() for alias in aliases if alias}
    if not targets:
        return 0
    conn = connect_db(str(db_file))
    mailbox = _connect_mailbox(config)
    inserted = 0
    try:
        mailbox.login(config.username, config.password)
        for folder in _sync_folders(mailbox, config.folder):
            folder_config = InboxConfig(
                host=config.host,
                port=config.port,
                username=config.username,
                password=config.password,
                folder=folder,
                use_ssl=config.use_ssl,
            )
            status, _ = mailbox.select(folder)
            if status != "OK":
                if folder == config.folder:
                    raise RuntimeError("QQ 邮箱 INBOX 不可用")
                continue
            _refresh_selected_mailbox(mailbox)
            status, data = mailbox.uid("search", None, "ALL")
            if status != "OK":
                if folder == config.folder:
                    raise RuntimeError("QQ 邮箱 IMAP 搜索失败")
                continue
            raw_uids = data[0] if data else None
            uids = raw_uids.split()[-limit:] if raw_uids else []
            for raw_uid in reversed(uids):
                uid = raw_uid.decode("ascii", errors="ignore")
                existing = conn.execute(
                    "SELECT 1 FROM messages WHERE account_key = ? AND folder = ? AND uid = ?",
                    (folder_config.account_key, folder, uid),
                ).fetchone()
                if existing:
                    continue
                fetch_status, parts = mailbox.uid("fetch", raw_uid, "(BODY.PEEK[])")
                if fetch_status != "OK" or not parts:
                    continue
                raw_message = b"".join(
                    part[1] for part in parts if isinstance(part, tuple)
                )
                if not raw_message:
                    continue
                message = BytesParser(policy=policy.default).parsebytes(raw_message)
                body = get_message_body(message)
                subject = str(message.get("Subject", ""))
                sender = ", ".join(header_addresses(message, ["From"]))
                product_text = f"{sender}\n{subject}\n{body}"
                if not re.search(r"\b(?:chatgpt|openai)\b", product_text, re.I):
                    continue
                code = extract_verification_code(subject, body)
                if not code:
                    continue
                recipient_values = [
                    str(message.get(name, "")) for name in ZKGMAIL_RECIPIENT_HEADERS
                ]
                haystack = "\n".join([*recipient_values, body]).lower()
                target = next((alias for alias in targets if alias in haystack), "")
                if not target:
                    continue
                recipients = ", ".join(
                    header_addresses(message, ZKGMAIL_RECIPIENT_HEADERS)
                )
                record = {
                    "account_key": folder_config.account_key,
                    "folder": folder,
                    "uid": uid,
                    "sender": sender,
                    "recipients": recipients,
                    "hme_address": target,
                    "subject": subject,
                    "code": code,
                    "body_preview": normalize_space(body)[:500],
                    "received_at": parse_received_at(message),
                    "created_at": _utc_now().isoformat(),
                }
                if insert_message(conn, record):
                    inserted += 1
        return inserted
    finally:
        try:
            mailbox.logout()
        except Exception:
            pass
        conn.close()


class ZkgmailConfigStore:
    """Persist the QQ IMAP authorization code without returning it to the UI."""

    def __init__(
        self,
        db_file: Path,
        *,
        username: str = ZKGMAIL_FORWARD_ACCOUNT,
    ) -> None:
        self.db_file = Path(db_file)
        self.initial_username = str(username or ZKGMAIL_FORWARD_ACCOUNT).strip().lower()

    def _defaults(self) -> dict[str, Any]:
        return {
            "host": ZKGMAIL_IMAP_HOST,
            "port": ZKGMAIL_IMAP_PORT,
            "username": self.initial_username,
            "authorizationCode": "",
            "folder": ZKGMAIL_FOLDER,
            "useSsl": True,
            "updatedAt": "",
        }

    def load(self) -> dict[str, Any]:
        conn = connect_db(str(self.db_file))
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (ZKGMAIL_SETTING_KEY,)
            ).fetchone()
        finally:
            conn.close()
        state = self._defaults()
        if row:
            try:
                stored = json.loads(str(row["value"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                stored = {}
            if isinstance(stored, dict):
                for key in state:
                    if key in stored:
                        state[key] = stored[key]
        state["host"] = ZKGMAIL_IMAP_HOST
        state["port"] = ZKGMAIL_IMAP_PORT
        state["username"] = ZKGMAIL_FORWARD_ACCOUNT
        state["authorizationCode"] = str(state.get("authorizationCode") or "").strip()
        state["folder"] = ZKGMAIL_FOLDER
        state["useSsl"] = True
        return state

    def _save(self, state: dict[str, Any]) -> None:
        conn = connect_db(str(self.db_file))
        try:
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (
                    ZKGMAIL_SETTING_KEY,
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def inbox_config(self, authorization_code: str | None = None) -> InboxConfig:
        state = self.load()
        if authorization_code is not None:
            state["authorizationCode"] = str(authorization_code or "").strip()
        code = str(state.get("authorizationCode") or "").strip()
        if not code:
            raise ValueError("请填写 QQ 邮箱 IMAP/SMTP 授权码")
        if len(code) > 1024:
            raise ValueError("QQ 邮箱授权码长度无效")
        return InboxConfig(
            host=ZKGMAIL_IMAP_HOST,
            port=ZKGMAIL_IMAP_PORT,
            username=ZKGMAIL_FORWARD_ACCOUNT,
            password=code,
            folder=ZKGMAIL_FOLDER,
            use_ssl=True,
        )

    def configure(self, *, authorization_code: str | None = None) -> dict[str, Any]:
        state = self.load()
        if authorization_code is not None:
            code = str(authorization_code or "").strip()
            if not code:
                raise ValueError("请填写 QQ 邮箱 IMAP/SMTP 授权码")
            if len(code) > 1024:
                raise ValueError("QQ 邮箱授权码长度无效")
            state["authorizationCode"] = code
        if not str(state.get("authorizationCode") or "").strip():
            raise ValueError("请填写 QQ 邮箱 IMAP/SMTP 授权码")
        state.update(
            host=ZKGMAIL_IMAP_HOST,
            port=ZKGMAIL_IMAP_PORT,
            username=ZKGMAIL_FORWARD_ACCOUNT,
            folder=ZKGMAIL_FOLDER,
            useSsl=True,
            updatedAt=_utc_now().isoformat(),
        )
        self._save(state)
        return self.public_state()

    def public_state(self) -> dict[str, Any]:
        state = self.load()
        return {
            "configured": bool(state["authorizationCode"]),
            "domain": ZKGMAIL_DOMAIN,
            "forwardAccount": mask_account(state["username"]),
            "host": state["host"],
            "port": state["port"],
            "folder": state["folder"],
            "useSsl": state["useSsl"],
            "updatedAt": state["updatedAt"],
        }


class ZkgmailMailClient:
    """Generate catch-all addresses and read their codes from the QQ mailbox."""

    def __init__(
        self,
        config_store: ZkgmailConfigStore,
        *,
        sync_interval_seconds: float = ZKGMAIL_SYNC_INTERVAL_SECONDS,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.config_store = config_store
        self.db_file = config_store.db_file
        self.sync_interval_seconds = max(0.0, float(sync_interval_seconds))
        self._clock = clock or _utc_now
        self._monotonic = monotonic or time.monotonic
        self._acquired_at: dict[str, datetime] = {}
        self._consumed_message_ids: dict[str, set[int]] = {}
        self._next_sync_at = 0.0
        self._lock = asyncio.Lock()

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    def public_state(self) -> dict[str, Any]:
        return {
            **self.config_store.public_state(),
            "active": len(self._acquired_at),
            "automaticCode": True,
        }

    async def configure(self, authorization_code: str | None = None) -> dict[str, Any]:
        config = self.config_store.inbox_config(authorization_code)
        try:
            await asyncio.to_thread(_test_imap_connection, config)
        except imaplib.IMAP4.error as error:
            raise RuntimeError(
                "QQ 邮箱 IMAP 登录失败，请检查授权码并确认 IMAP 服务已开启"
            ) from error
        except (OSError, TimeoutError) as error:
            raise RuntimeError("无法连接 QQ 邮箱 IMAP 服务") from error
        state = self.config_store.configure(authorization_code=authorization_code)
        self._next_sync_at = 0.0
        return {**state, "active": len(self._acquired_at), "automaticCode": True}

    @staticmethod
    def _normalize_email(email: str) -> str:
        target = str(email or "").strip().lower()
        if not re.fullmatch(
            rf"[a-z0-9][a-z0-9._-]{{0,62}}@{re.escape(ZKGMAIL_DOMAIN)}",
            target,
        ):
            raise RuntimeError("zkgmail.com 注册邮箱地址无效")
        return target

    async def acquire_email(self, label: str = "") -> str:
        self.config_store.inbox_config()
        async with self._lock:
            conn = connect_db(str(self.db_file))
            try:
                for _attempt in range(100):
                    local_part = _generate_human_local_part()
                    email = f"{local_part}@{ZKGMAIL_DOMAIN}"
                    exists = conn.execute(
                        "SELECT 1 FROM addresses WHERE lower(email) = ?",
                        (email,),
                    ).fetchone()
                    if exists:
                        continue
                    upsert_address(
                        conn,
                        email,
                        label=str(label or "zkgmail.com 注册")[:200],
                        state="unused",
                        source="zkgmail",
                        note=f"Catch-all 转发至 {ZKGMAIL_FORWARD_ACCOUNT}",
                        is_active=True,
                    )
                    break
                else:
                    raise RuntimeError("无法生成未使用的 zkgmail.com 人名邮箱")
            finally:
                conn.close()
        self._acquired_at[email] = self._now()
        self._consumed_message_ids[email] = set()
        return email

    async def _sync_if_due(self) -> None:
        now = self._monotonic()
        if now < self._next_sync_at:
            return
        config = self.config_store.inbox_config()
        try:
            await asyncio.to_thread(
                _sync_relevant_messages,
                config,
                self.db_file,
                _known_zkgmail_aliases(self.db_file),
                limit=ZKGMAIL_MESSAGE_LIMIT,
            )
        except imaplib.IMAP4.error as error:
            raise RuntimeError(
                "QQ 邮箱 IMAP 登录失败，请检查授权码并确认 IMAP 服务已开启"
            ) from error
        except (OSError, TimeoutError) as error:
            raise RuntimeError("无法连接 QQ 邮箱 IMAP 服务") from error
        self._next_sync_at = self._monotonic() + self.sync_interval_seconds

    def _next_stored_code(self, email: str) -> str:
        acquired_at = self._acquired_at.get(email)
        if acquired_at is None:
            raise RuntimeError("未找到该 zkgmail.com 注册邮箱的本机取码记录")
        # Mail-server clocks can differ slightly from the workstation clock.
        earliest = acquired_at - timedelta(minutes=5)
        consumed = self._consumed_message_ids.setdefault(email, set())
        conn = connect_db(str(self.db_file))
        try:
            rows = conn.execute(
                """
                SELECT id, received_at, code
                FROM messages
                WHERE lower(COALESCE(hme_address, '')) = ?
                  AND code IS NOT NULL AND code != ''
                  AND (
                    lower(COALESCE(sender, '') || ' ' || COALESCE(subject, '') || ' ' || COALESCE(body_preview, '')) LIKE '%chatgpt%'
                    OR lower(COALESCE(sender, '') || ' ' || COALESCE(subject, '') || ' ' || COALESCE(body_preview, '')) LIKE '%openai%'
                  )
                ORDER BY COALESCE(received_at, created_at) ASC, id ASC
                LIMIT 50
                """,
                (email,),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            message_id = int(row["id"])
            if message_id in consumed:
                continue
            received_at = _parse_utc(row["received_at"])
            if received_at is None or received_at < earliest:
                continue
            code = re.sub(r"[^A-Za-z0-9]", "", str(row["code"] or ""))
            if not 4 <= len(code) <= 10:
                continue
            consumed.add(message_id)
            return code
        return ""

    async def poll_code(self, email: str) -> str:
        target = self._normalize_email(email)
        async with self._lock:
            await self._sync_if_due()
            return self._next_stored_code(target)

    async def poll_next_code(self, email: str) -> str:
        return await self.poll_code(email)

    async def complete_email(self, email: str, _success: bool, _message: str) -> None:
        self._normalize_email(email)

    async def cancel_email(self, email: str, _message: str) -> None:
        self._normalize_email(email)

    async def forget_email(self, email: str) -> bool:
        target = self._normalize_email(email)
        removed = self._acquired_at.pop(target, None) is not None
        self._consumed_message_ids.pop(target, None)
        return removed


__all__ = [
    "ZKGMAIL_DOMAIN",
    "ZKGMAIL_FORWARD_ACCOUNT",
    "ZKGMAIL_IMAP_HOST",
    "ZKGMAIL_IMAP_PORT",
    "ZkgmailConfigStore",
    "ZkgmailMailClient",
]
