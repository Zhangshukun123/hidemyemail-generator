from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import hmac
import imaplib
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Any

from ..domain import CodeMessage, MailboxNotConfiguredError, MailboxUnavailableError
from ..ports import CodeExtractor, CodeRepository
from ..settings import Settings


TRUSTED_ENVELOPE_RECIPIENT_HEADERS = (
    "X-Original-To",
    "Delivered-To",
    "Envelope-To",
    "Original-Recipient",
)
EMAIL_ADDRESS_PATTERN = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
TRUSTED_RECIPIENT_VALUE_RE = re.compile(
    rf"^\s*(?:rfc822\s*;\s*)?(?:<(?P<bracket>{EMAIL_ADDRESS_PATTERN})>|"
    rf"(?P<bare>{EMAIL_ADDRESS_PATTERN}))\s*$",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
CURSOR_KEY_CONTEXT = b"zkgmail-code-server/cursor-key/v1"
CURSOR_VALUE_CONTEXT = b"zkgmail-code-server/cursor/v1"
MAX_IMAP_UID = (1 << 32) - 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_connection(settings: Settings):
    return imaplib.IMAP4_SSL(
        settings.imap_host,
        settings.imap_port,
        timeout=settings.imap_timeout_seconds,
    )


def _content_text(part: EmailMessage) -> str:
    try:
        content = part.get_content()
    except (LookupError, UnicodeError, ValueError):
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return str(payload or "")
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    if isinstance(content, bytes):
        return content.decode(part.get_content_charset() or "utf-8", errors="replace")
    return str(content or "")


def message_body(message: EmailMessage) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.is_multipart() or str(part.get_content_disposition() or "") == "attachment":
            continue
        content = _content_text(part)
        if not content:
            continue
        if part.get_content_type() == "text/html":
            html_parts.append(html.unescape(TAG_RE.sub(" ", content)))
        elif part.get_content_maintype() == "text":
            plain_parts.append(content)
    return "\n".join(plain_parts or html_parts)


def trusted_recipient_addresses(
    message: EmailMessage,
    header_names: tuple[str, ...] = TRUSTED_ENVELOPE_RECIPIENT_HEADERS,
) -> set[str]:
    """Read recipients only from envelope metadata supplied by the forwarder.

    The configured forwarding service must overwrite or strip these headers at
    its trust boundary. Ordinary message headers and body text are deliberately
    excluded because a sender controls them.
    """

    addresses: set[str] = set()
    for name in header_names:
        for raw_value in message.get_all(name, []):
            value = str(raw_value or "")
            match = TRUSTED_RECIPIENT_VALUE_RE.fullmatch(value)
            if match:
                addresses.add(str(match.group("bracket") or match.group("bare")).lower())
    return addresses


def numerically_sorted_uids(raw_uids: bytes) -> list[bytes]:
    """Return valid IMAP UIDs in numeric rather than byte-string order."""

    parsed: list[tuple[int, bytes]] = []
    for raw_uid in raw_uids.split():
        if not raw_uid.isdigit():
            continue
        numeric_uid = int(raw_uid)
        if 1 <= numeric_uid <= MAX_IMAP_UID:
            parsed.append((numeric_uid, raw_uid))
    parsed.sort(key=lambda item: item[0])
    return [raw_uid for _, raw_uid in parsed]


def message_received_at(message: EmailMessage) -> datetime | None:
    raw_value = message.get("Date")
    if not raw_value:
        return None
    try:
        parsed = parsedate_to_datetime(str(raw_value))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ImapCodeRepository:
    """IMAP adapter that performs an exact-recipient, non-consuming lookup."""

    def __init__(
        self,
        settings: Settings,
        extractor: CodeExtractor,
        *,
        connection_factory: Callable[[Settings], Any] = _default_connection,
        clock: Callable[[], datetime] = _utc_now,
        trusted_recipient_headers: tuple[str, ...] = TRUSTED_ENVELOPE_RECIPIENT_HEADERS,
        cursor_secret: str | bytes | None = None,
        max_concurrent_queries: int = 2,
    ) -> None:
        self._settings = settings
        self._extractor = extractor
        self._connection_factory = connection_factory
        self._clock = clock
        self._trusted_recipient_headers = tuple(
            dict.fromkeys(
                str(name).strip()
                for name in trusted_recipient_headers
                if str(name).strip()
            )
        )
        secret = settings.access_token if cursor_secret is None else cursor_secret
        secret_bytes = secret if isinstance(secret, bytes) else str(secret).encode("utf-8")
        self._cursor_key = hmac.new(
            secret_bytes,
            CURSOR_KEY_CONTEXT,
            hashlib.sha256,
        ).digest()
        self._query_slots = threading.BoundedSemaphore(max(1, int(max_concurrent_queries)))

    def _opaque_cursor(self, recipient: str, raw_uid: bytes) -> str:
        message = b"\x00".join(
            (
                CURSOR_VALUE_CONTEXT,
                recipient.strip().lower().encode("utf-8"),
                raw_uid,
            )
        )
        digest = hmac.new(self._cursor_key, message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    async def latest_for(self, recipient: str) -> CodeMessage | None:
        return await asyncio.to_thread(self._latest_for_sync_limited, recipient)

    def _latest_for_sync_limited(self, recipient: str) -> CodeMessage | None:
        with self._query_slots:
            return self._latest_for_sync(recipient)

    def _latest_for_sync(self, recipient: str) -> CodeMessage | None:
        if not self._settings.configured:
            raise MailboxNotConfiguredError("mailbox credentials are missing")

        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        cutoff = now - timedelta(minutes=self._settings.lookback_minutes)
        mailbox = None
        try:
            mailbox = self._connection_factory(self._settings)
            mailbox.login(self._settings.imap_username, self._settings.imap_password)
            status, _ = mailbox.select(self._settings.imap_folder, readonly=True)
            if status != "OK":
                raise imaplib.IMAP4.error("mailbox folder is unavailable")
            noop = getattr(mailbox, "noop", None)
            if callable(noop):
                noop()
            status, data = mailbox.uid("search", None, "ALL")
            if status != "OK":
                raise imaplib.IMAP4.error("mailbox search failed")
            raw_uids = data[0] if data else b""
            uids = numerically_sorted_uids(raw_uids) if raw_uids else []
            for raw_uid in reversed(uids[-self._settings.fetch_limit :]):
                fetch_status, parts = mailbox.uid("fetch", raw_uid, "(BODY.PEEK[])")
                if fetch_status != "OK" or not parts:
                    continue
                raw_message = b"".join(
                    part[1] for part in parts if isinstance(part, tuple) and isinstance(part[1], bytes)
                )
                if not raw_message:
                    continue
                message = BytesParser(policy=policy.default).parsebytes(raw_message)
                body = message_body(message)
                recipients = trusted_recipient_addresses(
                    message,
                    self._trusted_recipient_headers,
                )
                if recipient not in recipients:
                    continue
                received_at = message_received_at(message)
                if received_at is not None and received_at < cutoff:
                    continue
                code = self._extractor.extract(str(message.get("Subject", "")), body)
                if not code:
                    continue
                timestamp = (received_at or now).isoformat()
                return CodeMessage(
                    code=code,
                    received_at=timestamp,
                    sender=str(message.get("From", ""))[:320],
                    cursor=self._opaque_cursor(recipient, raw_uid),
                )
            return None
        except (imaplib.IMAP4.error, OSError, TimeoutError, EOFError) as error:
            raise MailboxUnavailableError("IMAP lookup failed") from error
        finally:
            if mailbox is not None:
                try:
                    mailbox.logout()
                except Exception:
                    pass


class CachedCodeRepository:
    """Repository decorator that coalesces rapid browser polling."""

    def __init__(
        self,
        wrapped: CodeRepository,
        *,
        ttl_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._wrapped = wrapped
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._monotonic = monotonic
        self._cache: dict[str, tuple[float, CodeMessage | None]] = {}
        self._lock = asyncio.Lock()

    async def latest_for(self, recipient: str) -> CodeMessage | None:
        if self._ttl_seconds <= 0:
            return await self._wrapped.latest_for(recipient)
        cached = self._cache.get(recipient)
        now = self._monotonic()
        if cached and now - cached[0] < self._ttl_seconds:
            return cached[1]
        async with self._lock:
            cached = self._cache.get(recipient)
            now = self._monotonic()
            if cached and now - cached[0] < self._ttl_seconds:
                return cached[1]
            result = await self._wrapped.latest_for(recipient)
            # Cache only an empty poll. A delivered code is deliberately not
            # cached so a second message for the same alias becomes visible on
            # the very next lookup instead of briefly returning the first code.
            if result is None:
                self._cache[recipient] = (self._monotonic(), None)
            else:
                self._cache.pop(recipient, None)
            if len(self._cache) > 1024:
                cutoff = self._monotonic() - self._ttl_seconds
                self._cache = {key: value for key, value in self._cache.items() if value[0] >= cutoff}
            return result
