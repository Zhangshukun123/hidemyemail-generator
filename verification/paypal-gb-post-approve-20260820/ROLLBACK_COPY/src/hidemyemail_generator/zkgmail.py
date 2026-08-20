from __future__ import annotations

import asyncio
import imaplib
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Callable, Protocol

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
CCLGMAIL_DOMAIN = "cclgmail.com"
SHUKUNLABS_DOMAIN = "shukunlabs.xyz"
LLHDSCZY_DOMAIN = "llhdsczy.com"
DEFAULT_QQ_FORWARD_DOMAINS = (
    CCLGMAIL_DOMAIN,
    ZKGMAIL_DOMAIN,
    SHUKUNLABS_DOMAIN,
    LLHDSCZY_DOMAIN,
)
ZKGMAIL_FORWARD_ACCOUNT = "352121354@qq.com"
ZKGMAIL_IMAP_HOST = "imap.qq.com"
ZKGMAIL_IMAP_PORT = 993
ZKGMAIL_FOLDER = "INBOX"
ZKGMAIL_SYNC_INTERVAL_SECONDS = 1.0
ZKGMAIL_MESSAGE_LIMIT = 30
ZKGMAIL_IDLE_POLL_SECONDS = 1.0
ZKGMAIL_IDLE_RESTART_SECONDS = 60.0
ZKGMAIL_IDLE_RECONNECT_SECONDS = 2.0
LLHDSCZY_CODE_SERVICE_URL = (
    "https://zkgmail.8-208-13-52.sslip.io/api/internal/code/latest"
)
LLHDSCZY_CODE_SERVICE_TIMEOUT_SECONDS = 10.0
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


def _normalize_domain(value: Any) -> str:
    domain = str(value or "").strip().lower().lstrip("@")
    if "@" in domain:
        domain = domain.rsplit("@", 1)[-1]
    if len(domain) > 253 or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
        domain,
    ):
        raise ValueError("请输入有效的邮箱域名，例如 cclgmail.com")
    return domain


class ZkgmailSyncStrategy(Protocol):
    """Strategy interface for synchronizing verification messages."""

    def __call__(
        self,
        config: InboxConfig,
        db_file: Path,
        aliases: set[str],
        *,
        limit: int = ZKGMAIL_MESSAGE_LIMIT,
    ) -> int: ...


class ZkgmailIdleStrategy(Protocol):
    """Strategy interface for listening to QQ IMAP IDLE notifications."""

    def __call__(
        self,
        config: InboxConfig,
        stop_event: threading.Event,
        on_activity: Callable[[], None],
    ) -> None: ...


class LocalPartNamingStrategy(Protocol):
    """Strategy interface for domain-specific mailbox local parts."""

    def generate(self, domain: str) -> str: ...


class RemoteCodeFetcher(Protocol):
    def __call__(
        self,
        url: str,
        token: str,
        email: str,
        after_cursor: str,
    ) -> tuple[str, str]: ...


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


def _fetch_remote_code(
    url: str,
    token: str,
    email: str,
    after_cursor: str,
) -> tuple[str, str]:
    payload: dict[str, str] = {"email": email}
    if after_cursor:
        payload["afterCursor"] = after_cursor
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=LLHDSCZY_CODE_SERVICE_TIMEOUT_SECONDS,
        ) as response:
            body = response.read(16 * 1024)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return "", after_cursor
        if error.code in (401, 403):
            raise RuntimeError("llhdsczy 本地归档取码凭据无效") from error
        raise RuntimeError("llhdsczy 本地归档取码服务暂时不可用") from error
    except (OSError, TimeoutError, urllib.error.URLError) as error:
        raise RuntimeError("无法连接 llhdsczy 本地归档取码服务") from error
    try:
        result = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("llhdsczy 本地归档取码响应无效") from error
    if not isinstance(result, dict) or result.get("state") != "found":
        return "", after_cursor
    code = re.sub(r"[^A-Za-z0-9]", "", str(result.get("code") or ""))
    cursor = str(result.get("cursor") or "").strip()
    if not 4 <= len(code) <= 10 or not 1 <= len(cursor) <= 128:
        raise RuntimeError("llhdsczy 本地归档取码响应无效")
    return code, cursor


class DomainLocalPartNamingStrategy:
    """Generate a random human-name local part for every catch-all domain."""

    def generate(self, domain: str) -> str:
        del domain
        return _generate_human_local_part()


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


def _watch_mailbox_idle(
    config: InboxConfig,
    stop_event: threading.Event,
    on_activity: Callable[[], None],
) -> None:
    """Keep one read-only QQ IMAP connection waiting for mailbox changes."""

    from imap_tools import MailBox

    while not stop_event.is_set():
        try:
            with MailBox(
                config.host,
                port=config.port,
                timeout=DEFAULT_IMAP_TIMEOUT,
            ).login(
                config.username,
                config.password,
                initial_folder=None,
            ) as mailbox:
                mailbox.folder.set(config.folder, readonly=True)
                while not stop_event.is_set():
                    cycle_deadline = time.monotonic() + ZKGMAIL_IDLE_RESTART_SECONDS
                    mailbox.idle.start()
                    try:
                        while (
                            not stop_event.is_set()
                            and time.monotonic() < cycle_deadline
                        ):
                            responses = mailbox.idle.poll(
                                timeout=ZKGMAIL_IDLE_POLL_SECONDS
                            )
                            if responses:
                                on_activity()
                                break
                    finally:
                        mailbox.idle.stop()
        except Exception:
            if stop_event.wait(ZKGMAIL_IDLE_RECONNECT_SECONDS):
                return


def _known_zkgmail_aliases(db_file: Path) -> set[str]:
    conn = connect_db(str(db_file))
    try:
        rows = conn.execute(
            """
            SELECT email FROM addresses
            WHERE source = ? OR lower(email) LIKE ? OR lower(email) LIKE ?
            """,
            ("zkgmail", f"%@{ZKGMAIL_DOMAIN}", f"%@{CCLGMAIL_DOMAIN}"),
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
                if insert_message(conn, record, address_source="zkgmail"):
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
            "codeServiceToken": "",
            "folder": ZKGMAIL_FOLDER,
            "useSsl": True,
            "domains": list(DEFAULT_QQ_FORWARD_DOMAINS),
            "activeDomain": CCLGMAIL_DOMAIN,
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
        state["codeServiceToken"] = str(state.get("codeServiceToken") or "").strip()
        state["folder"] = ZKGMAIL_FOLDER
        state["useSsl"] = True
        raw_domains = state.get("domains")
        domains: list[str] = []
        if isinstance(raw_domains, list):
            for candidate in raw_domains:
                try:
                    domain = _normalize_domain(candidate)
                except ValueError:
                    continue
                if domain not in domains:
                    domains.append(domain)
        for domain in DEFAULT_QQ_FORWARD_DOMAINS:
            if domain not in domains:
                domains.append(domain)
        try:
            active_domain = _normalize_domain(state.get("activeDomain"))
        except ValueError:
            active_domain = CCLGMAIL_DOMAIN
        if active_domain not in domains:
            domains.append(active_domain)
        state["domains"] = domains[:20]
        state["activeDomain"] = active_domain
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

    def configure(
        self,
        *,
        authorization_code: str | None = None,
        domain: str | None = None,
    ) -> dict[str, Any]:
        state = self.load()
        if authorization_code is not None:
            code = str(authorization_code or "").strip()
            if not code:
                raise ValueError("请填写 QQ 邮箱 IMAP/SMTP 授权码")
            if len(code) > 1024:
                raise ValueError("QQ 邮箱授权码长度无效")
            state["authorizationCode"] = code
        if domain is not None:
            active_domain = _normalize_domain(domain)
            domains = list(state["domains"])
            if active_domain not in domains:
                if len(domains) >= 20:
                    raise ValueError("最多可配置 20 个 QQ 转发邮箱域名")
                domains.append(active_domain)
            state["domains"] = domains
            state["activeDomain"] = active_domain
        if not str(state.get("authorizationCode") or "").strip() and domain is None:
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
            "localArchiveConfigured": bool(
                re.fullmatch(r"[0-9a-fA-F]{64}", state["codeServiceToken"])
            ),
            "domain": state["activeDomain"],
            "domains": list(state["domains"]),
            "forwardAccount": mask_account(state["username"]),
            "host": state["host"],
            "port": state["port"],
            "folder": state["folder"],
            "useSsl": state["useSsl"],
            "updatedAt": state["updatedAt"],
        }

    def configure_code_service(self, token: str) -> dict[str, Any]:
        value = str(token or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("llhdsczy 本地归档取码令牌无效")
        state = self.load()
        state["codeServiceToken"] = value.lower()
        state["updatedAt"] = _utc_now().isoformat()
        self._save(state)
        return self.public_state()

    def code_service_token(self) -> str:
        value = str(self.load().get("codeServiceToken") or "").strip()
        return value if re.fullmatch(r"[0-9a-fA-F]{64}", value) else ""

    def supports_email(self, email: str) -> bool:
        target = str(email or "").strip().lower()
        local, separator, domain = target.rpartition("@")
        return bool(
            separator
            and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", local)
            and domain in self.load()["domains"]
        )


class ZkgmailMailClient:
    """Generate catch-all addresses and read their codes from the QQ mailbox."""

    def __init__(
        self,
        config_store: ZkgmailConfigStore,
        *,
        sync_interval_seconds: float = ZKGMAIL_SYNC_INTERVAL_SECONDS,
        sync_strategy: ZkgmailSyncStrategy | None = None,
        idle_enabled: bool = False,
        idle_strategy: ZkgmailIdleStrategy | None = None,
        local_part_strategy: LocalPartNamingStrategy | None = None,
        remote_code_fetcher: RemoteCodeFetcher | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.config_store = config_store
        self.db_file = config_store.db_file
        self.sync_interval_seconds = max(0.0, float(sync_interval_seconds))
        self._clock = clock or _utc_now
        self._monotonic = monotonic or time.monotonic
        self._sync_strategy = sync_strategy
        self._idle_enabled = bool(idle_enabled)
        self._idle_strategy = idle_strategy or _watch_mailbox_idle
        self._local_part_strategy = local_part_strategy or DomainLocalPartNamingStrategy()
        self._remote_code_fetcher = remote_code_fetcher or _fetch_remote_code
        self._acquired_at: dict[str, datetime] = {}
        self._consumed_message_ids: dict[str, set[int]] = {}
        self._remote_cursors: dict[str, str] = {}
        self._next_sync_at = 0.0
        self._sync_task: asyncio.Task[None] | None = None
        self._sync_error: RuntimeError | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._idle_stop_event: threading.Event | None = None
        self._idle_sync_pending = False
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

    @property
    def sync_task(self) -> asyncio.Task[None] | None:
        """Expose the single-flight task for lifecycle coordination and tests."""

        return self._sync_task

    async def configure(
        self,
        authorization_code: str | None = None,
        domain: str | None = None,
    ) -> dict[str, Any]:
        await self._stop_idle_listener()
        async with self._lock:
            await self._wait_for_sync_completion()
            if authorization_code is None and domain is not None:
                state = self.config_store.configure(domain=domain)
                return {
                    **state,
                    "active": len(self._acquired_at),
                    "automaticCode": True,
                }
            config = self.config_store.inbox_config(authorization_code)
            try:
                await asyncio.to_thread(_test_imap_connection, config)
            except imaplib.IMAP4.error as error:
                raise RuntimeError(
                    "QQ 邮箱 IMAP 登录失败，请检查授权码并确认 IMAP 服务已开启"
                ) from error
            except (OSError, TimeoutError) as error:
                raise RuntimeError("无法连接 QQ 邮箱 IMAP 服务") from error
            state = self.config_store.configure(
                authorization_code=authorization_code,
                domain=domain,
            )
            self._sync_error = None
            self._next_sync_at = 0.0
            return {
                **state,
                "active": len(self._acquired_at),
                "automaticCode": True,
            }

    def supports_email(self, email: str) -> bool:
        return self.config_store.supports_email(email)

    def _normalize_email(self, email: str) -> str:
        target = str(email or "").strip().lower()
        if not self.supports_email(target):
            raise RuntimeError("QQ 转发注册邮箱地址无效或域名未配置")
        return target

    async def acquire_email(self, label: str = "") -> str:
        self.config_store.inbox_config()
        domain = str(self.config_store.load()["activeDomain"])
        async with self._lock:
            conn = connect_db(str(self.db_file))
            try:
                for _attempt in range(100):
                    local_part = self._local_part_strategy.generate(domain)
                    email = f"{local_part}@{domain}"
                    exists = conn.execute(
                        "SELECT 1 FROM addresses WHERE lower(email) = ?",
                        (email,),
                    ).fetchone()
                    if exists:
                        continue
                    upsert_address(
                        conn,
                        email,
                        label=str(label or f"{domain} 注册")[:200],
                        state="unused",
                        source="zkgmail",
                        note=(
                            "Catch-all 保存至服务器本地归档"
                            if domain == LLHDSCZY_DOMAIN
                            else f"Catch-all 转发至 {ZKGMAIL_FORWARD_ACCOUNT}"
                        ),
                        is_active=True,
                    )
                    break
                else:
                    raise RuntimeError(f"无法生成未使用的 {domain} 邮箱")
            finally:
                conn.close()
        self._acquired_at[email] = self._now()
        self._consumed_message_ids[email] = set()
        return email

    async def _run_sync(
        self,
        config: InboxConfig,
        aliases: set[str],
    ) -> None:
        strategy = self._sync_strategy or _sync_relevant_messages
        try:
            await asyncio.to_thread(
                strategy,
                config,
                self.db_file,
                aliases,
                limit=ZKGMAIL_MESSAGE_LIMIT,
            )
        except imaplib.IMAP4.error as error:
            raise RuntimeError(
                "QQ 邮箱 IMAP 登录失败，请检查授权码并确认 IMAP 服务已开启"
            ) from error
        except (OSError, TimeoutError) as error:
            raise RuntimeError("无法连接 QQ 邮箱 IMAP 服务") from error

    def _finish_sync(self, task: asyncio.Task[None]) -> None:
        if self._sync_task is not task:
            return
        try:
            task.result()
        except asyncio.CancelledError:
            self._sync_error = None
        except RuntimeError as error:
            self._sync_error = error
        except Exception as error:  # pragma: no cover - defensive adapter boundary
            self._sync_error = RuntimeError(
                f"QQ 邮箱同步失败：{type(error).__name__}"
            )
        else:
            self._sync_error = None
        self._sync_task = None
        self._next_sync_at = self._monotonic() + self.sync_interval_seconds
        if self._idle_sync_pending:
            self._idle_sync_pending = False
            self._next_sync_at = 0.0
            self._start_sync_if_due()

    def _handle_idle_activity(self) -> None:
        if self._sync_task is not None:
            self._idle_sync_pending = True
            return
        self._next_sync_at = 0.0
        self._start_sync_if_due()

    def _finish_idle_listener(self, task: asyncio.Task[None]) -> None:
        if self._idle_task is not task:
            return
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass
        self._idle_task = None
        self._idle_stop_event = None

    def _start_idle_listener_if_needed(self) -> None:
        if not self._idle_enabled or self._idle_task is not None:
            return
        config = self.config_store.inbox_config()
        loop = asyncio.get_running_loop()
        stop_event = threading.Event()

        def notify() -> None:
            try:
                loop.call_soon_threadsafe(self._handle_idle_activity)
            except RuntimeError:
                return

        task = asyncio.create_task(
            asyncio.to_thread(
                self._idle_strategy,
                config,
                stop_event,
                notify,
            ),
            name="zkgmail-imap-idle",
        )
        self._idle_stop_event = stop_event
        self._idle_task = task
        task.add_done_callback(self._finish_idle_listener)

    def _start_sync_if_due(self) -> bool:
        # Single-Flight pattern: concurrent HTTP polls share one IMAP scan.
        if self._sync_task is not None:
            return False
        now = self._monotonic()
        if now < self._next_sync_at:
            return False
        config = self.config_store.inbox_config()
        aliases = _known_zkgmail_aliases(self.db_file) | set(self._acquired_at)
        self._sync_error = None
        task = asyncio.create_task(
            self._run_sync(config, aliases),
            name="zkgmail-imap-sync",
        )
        self._sync_task = task
        task.add_done_callback(self._finish_sync)
        return True

    def _next_stored_code(self, email: str, since: str = "") -> str:
        acquired_at = self._acquired_at.get(email)
        requested_at = _parse_utc(since)
        if acquired_at is None and requested_at is None:
            raise RuntimeError("未找到该 QQ 转发注册邮箱的本机取码记录")
        if acquired_at is None and email not in _known_zkgmail_aliases(self.db_file):
            raise RuntimeError("未找到该 QQ 转发注册邮箱的本机取码记录")
        # Mail-server clocks can differ slightly from the workstation clock.
        anchors = [item for item in (acquired_at, requested_at) if item is not None]
        earliest = max(anchors) - timedelta(minutes=5)
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

    async def poll_code(self, email: str, *, since: str = "") -> str:
        target = self._normalize_email(email)
        if target.endswith(f"@{LLHDSCZY_DOMAIN}"):
            token = self.config_store.code_service_token()
            if token:
                cursor = self._remote_cursors.get(target, "")
                code, next_cursor = await asyncio.to_thread(
                    self._remote_code_fetcher,
                    LLHDSCZY_CODE_SERVICE_URL,
                    token,
                    target,
                    cursor,
                )
                if code:
                    self._remote_cursors[target] = next_cursor
                return code
        async with self._lock:
            self._start_idle_listener_if_needed()
            # Cache-Aside pattern: a local hit must never wait behind remote IMAP.
            code = self._next_stored_code(target, since)
            if code:
                return code
            previous_error = self._sync_error
            if previous_error is not None:
                # Surface every failed scan once before starting its retry.
                self._sync_error = None
                raise previous_error
            self._start_sync_if_due()
            return ""

    async def poll_next_code(self, email: str, *, since: str = "") -> str:
        return await self.poll_code(email, since=since)

    async def complete_email(self, email: str, success: bool, message: str) -> None:
        target = self._normalize_email(email)
        normalized_message = str(message or "").casefold()
        terminal_failure = any(
            marker in normalized_message
            for marker in (
                "不可自动重试",
                "account_deactivated",
                "account_deleted",
                "account_banned",
                "sso_required",
            )
        )
        state = "used" if success else "trash" if terminal_failure else "unused"
        async with self._lock:
            conn = connect_db(str(self.db_file))
            try:
                conn.execute(
                    """
                    UPDATE addresses
                    SET state = ?, note = ?, updated_at = ?
                    WHERE lower(email) = ? AND source = 'zkgmail'
                    """,
                    (
                        state,
                        str(message or "")[:500],
                        _utc_now().isoformat(),
                        target,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    async def cancel_email(self, email: str, _message: str) -> None:
        self._normalize_email(email)

    async def close(self) -> None:
        """Wait for the detached IMAP adapter before application shutdown."""

        await self._stop_idle_listener()
        await self._wait_for_sync_completion()

    async def _stop_idle_listener(self) -> None:
        task = self._idle_task
        stop_event = self._idle_stop_event
        if stop_event is not None:
            stop_event.set()
        if task is None:
            return
        await asyncio.gather(task, return_exceptions=True)
        if self._idle_task is task:
            self._finish_idle_listener(task)

    async def _wait_for_sync_completion(self) -> None:
        task = self._sync_task
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # ``to_thread`` keeps running after cancellation.  Finish the
            # adapter so its IMAP and SQLite handles close before shutdown.
            await asyncio.gather(task, return_exceptions=True)
            raise
        except Exception:
            # The next poll exposes the normalized synchronization error.
            pass
        finally:
            # A task waiter can resume before its done callback.  Finalize it
            # here so configure/close cannot be overwritten by a stale callback.
            if task.done() and self._sync_task is task:
                self._finish_sync(task)

    async def forget_email(self, email: str) -> bool:
        target = self._normalize_email(email)
        removed = self._acquired_at.pop(target, None) is not None
        self._consumed_message_ids.pop(target, None)
        self._remote_cursors.pop(target, None)
        return removed


__all__ = [
    "CCLGMAIL_DOMAIN",
    "DEFAULT_QQ_FORWARD_DOMAINS",
    "LLHDSCZY_CODE_SERVICE_URL",
    "LLHDSCZY_DOMAIN",
    "SHUKUNLABS_DOMAIN",
    "ZKGMAIL_DOMAIN",
    "ZKGMAIL_FORWARD_ACCOUNT",
    "ZKGMAIL_IMAP_HOST",
    "ZKGMAIL_IMAP_PORT",
    "ZkgmailConfigStore",
    "ZkgmailMailClient",
]
