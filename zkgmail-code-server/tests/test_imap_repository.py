import asyncio
import re
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from tests.helpers import settings
from zkgmail_code_server.adapters.imap_repository import (
    CachedCodeRepository,
    ImapCodeRepository,
)
from zkgmail_code_server.domain import CodeMessage, LookupState, MailboxNotConfiguredError
from zkgmail_code_server.presenter import LookupPresenter
from zkgmail_code_server.strategies.keyword_code_extractor import KeywordCodeExtractor


def raw_message(
    recipient,
    code,
    *,
    subject="OpenAI verification code",
    date=None,
    trusted_header="X-Original-To",
    to_address="receiver@example.test",
    body=None,
):
    message = EmailMessage()
    message["To"] = to_address
    if recipient and trusted_header:
        message[trusted_header] = recipient
    message["From"] = "noreply@example.test"
    message["Subject"] = subject
    message["Date"] = date or datetime(2026, 8, 17, 1, 59, tzinfo=timezone.utc)
    message.set_content(body if body is not None else f"Your verification code is {code}")
    return message.as_bytes()


class MailboxStub:
    def __init__(self, messages):
        self.messages = messages
        self.commands = []
        self.readonly = None

    def login(self, username, password):
        self.commands.append(("login", username, password))
        return "OK", []

    def select(self, folder, readonly=False):
        self.readonly = readonly
        self.commands.append(("select", folder))
        return "OK", []

    def noop(self):
        self.commands.append(("noop",))
        return "OK", []

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "search":
            return "OK", [b" ".join(self.messages)]
        if command == "fetch":
            return "OK", [(b"payload", self.messages[args[0]])]
        raise AssertionError(command)

    def logout(self):
        self.commands.append(("logout",))
        return "BYE", []


def test_repository_matches_exact_recipient_and_peeks_without_marking_read():
    target = "alias@zkgmail.com"
    mailbox = MailboxStub(
        {
            b"1": raw_message(target, "246810"),
            b"2": raw_message("otheralias@zkgmail.com", "999999"),
        }
    )
    repository = ImapCodeRepository(
        settings(),
        KeywordCodeExtractor(),
        connection_factory=lambda _: mailbox,
        clock=lambda: datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc),
    )

    item = asyncio.run(repository.latest_for(target))

    assert item.code == "246810"
    assert item.received_at == "2026-08-17T01:59:00+00:00"
    assert item.sender == "noreply@example.test"
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", item.cursor)
    assert item.cursor != "1"
    assert mailbox.readonly is True
    assert any(command[0:3] == ("fetch", b"1", "(BODY.PEEK[])") for command in mailbox.commands)
    assert mailbox.commands[-1] == ("logout",)


def test_repository_rejects_sender_controlled_to_and_body_recipient_claims():
    target = "victim@zkgmail.com"
    mailbox = MailboxStub(
        {
            b"1": raw_message(
                "other@zkgmail.com",
                "246810",
                to_address=target,
                body=f"Recipient {target}. Your verification code is 246810",
            )
        }
    )
    repository = ImapCodeRepository(
        settings(),
        KeywordCodeExtractor(),
        connection_factory=lambda _: mailbox,
        clock=lambda: datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc),
    )

    assert asyncio.run(repository.latest_for(target)) is None


def test_repository_accepts_an_explicitly_configured_trusted_envelope_header():
    target = "alias@zkgmail.com"
    mailbox = MailboxStub(
        {
            b"7": raw_message(
                target,
                "135790",
                trusted_header="X-Zkgmail-Envelope-To",
            )
        }
    )
    repository = ImapCodeRepository(
        settings(),
        KeywordCodeExtractor(),
        connection_factory=lambda _: mailbox,
        clock=lambda: datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc),
        trusted_recipient_headers=("X-Zkgmail-Envelope-To",),
    )

    item = asyncio.run(repository.latest_for(target))

    assert item.code == "135790"


def test_repository_returns_second_newest_code_without_consuming_it():
    target = "repeat@zkgmail.com"
    mailbox = MailboxStub(
        {
            b"1": raw_message(
                target,
                "111111",
                date=datetime(2026, 8, 17, 1, 58, tzinfo=timezone.utc),
            ),
            b"2": raw_message(
                target,
                "222222",
                date=datetime(2026, 8, 17, 1, 59, tzinfo=timezone.utc),
            ),
        }
    )
    repository = ImapCodeRepository(
        settings(),
        KeywordCodeExtractor(),
        connection_factory=lambda _: mailbox,
        clock=lambda: datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc),
    )

    first_lookup = asyncio.run(repository.latest_for(target))
    repeated_lookup = asyncio.run(repository.latest_for(target))

    assert first_lookup.code == "222222"
    assert repeated_lookup.code == "222222"
    assert first_lookup.cursor == repeated_lookup.cursor
    assert first_lookup.cursor != "2"


def test_repository_sorts_unordered_search_uids_before_selecting_latest():
    target = "unordered@zkgmail.com"
    mailbox = MailboxStub(
        {
            b"10": raw_message(target, "101010"),
            b"2": raw_message(target, "222222"),
        }
    )
    repository = ImapCodeRepository(
        settings(),
        KeywordCodeExtractor(),
        connection_factory=lambda _: mailbox,
        clock=lambda: datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc),
    )

    item = asyncio.run(repository.latest_for(target))

    assert item.code == "101010"
    fetches = [command for command in mailbox.commands if command[0] == "fetch"]
    assert fetches[0][1] == b"10"
    assert item.cursor != "10"


def test_opaque_cursor_is_bound_to_recipient_and_detects_new_mail():
    first_recipient = "first@zkgmail.com"
    second_recipient = "second@zkgmail.com"
    first_mailbox = MailboxStub({b"42": raw_message(first_recipient, "111111")})
    second_mailbox = MailboxStub({b"42": raw_message(second_recipient, "222222")})

    def fixed_clock():
        return datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)

    first_repository = ImapCodeRepository(
        settings(),
        KeywordCodeExtractor(),
        connection_factory=lambda _: first_mailbox,
        clock=fixed_clock,
        cursor_secret=b"cursor-test-secret",
    )
    second_repository = ImapCodeRepository(
        settings(),
        KeywordCodeExtractor(),
        connection_factory=lambda _: second_mailbox,
        clock=fixed_clock,
        cursor_secret=b"cursor-test-secret",
    )

    async def scenario():
        first_item = await first_repository.latest_for(first_recipient)
        second_item = await second_repository.latest_for(second_recipient)
        first_presenter = LookupPresenter(first_repository, clock=fixed_clock)
        second_presenter = LookupPresenter(second_repository, clock=fixed_clock)

        assert first_item.cursor != second_item.cursor
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first_item.cursor)
        assert "42" not in first_item.cursor

        waiting = await first_presenter.lookup(
            first_recipient,
            after_cursor=first_item.cursor,
        )
        assert waiting.state is LookupState.WAITING

        replacement = "A" if first_item.cursor[-1] != "A" else "B"
        tampered_cursor = first_item.cursor[:-1] + replacement
        tampered = await first_presenter.lookup(
            first_recipient,
            after_cursor=tampered_cursor,
        )
        assert tampered.state is LookupState.FOUND

        cross_recipient = await second_presenter.lookup(
            second_recipient,
            after_cursor=first_item.cursor,
        )
        assert cross_recipient.state is LookupState.FOUND

        first_mailbox.messages[b"43"] = raw_message(first_recipient, "333333")
        updated = await first_presenter.lookup(
            first_recipient,
            after_cursor=first_item.cursor,
        )
        assert updated.state is LookupState.FOUND
        assert updated.code == "333333"
        assert updated.cursor != first_item.cursor

    asyncio.run(scenario())


def test_repository_rejects_stale_message():
    mailbox = MailboxStub(
        {
            b"1": raw_message(
                "alias@zkgmail.com",
                "246810",
                date=datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc),
            )
        }
    )
    repository = ImapCodeRepository(
        settings(lookback_minutes=30),
        KeywordCodeExtractor(),
        connection_factory=lambda _: mailbox,
        clock=lambda: datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc),
    )
    assert asyncio.run(repository.latest_for("alias@zkgmail.com")) is None


def test_repository_requires_credentials_before_connecting():
    repository = ImapCodeRepository(
        settings(imap_password=""),
        KeywordCodeExtractor(),
        connection_factory=lambda _: pytest.fail("must not connect"),
    )
    with pytest.raises(MailboxNotConfiguredError):
        asyncio.run(repository.latest_for("alias@zkgmail.com"))


def test_cache_decorator_coalesces_rapid_empty_results():
    class Repository:
        calls = 0

        async def latest_for(self, _recipient):
            self.calls += 1
            return None

    wrapped = Repository()
    current = [10.0]
    repository = CachedCodeRepository(
        wrapped,
        ttl_seconds=2,
        monotonic=lambda: current[0],
    )

    async def scenario():
        assert await repository.latest_for("alias@zkgmail.com") is None
        assert await repository.latest_for("alias@zkgmail.com") is None
        assert wrapped.calls == 1
        current[0] = 12.1
        assert await repository.latest_for("alias@zkgmail.com") is None
        assert wrapped.calls == 2

    asyncio.run(scenario())


def test_cache_decorator_never_caches_a_found_code():
    class Repository:
        def __init__(self):
            self.results = [
                CodeMessage("111111", "2026-08-17T01:58:00+00:00"),
                CodeMessage("222222", "2026-08-17T01:59:00+00:00"),
            ]

        async def latest_for(self, _recipient):
            return self.results.pop(0)

    async def scenario():
        repository = CachedCodeRepository(Repository(), ttl_seconds=30)
        first = await repository.latest_for("repeat@zkgmail.com")
        second = await repository.latest_for("repeat@zkgmail.com")
        assert first.code == "111111"
        assert second.code == "222222"

    asyncio.run(scenario())
