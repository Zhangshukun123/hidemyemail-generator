import asyncio
from datetime import datetime, timezone

import pytest

from zkgmail_code_server.domain import (
    CodeMessage,
    LookupState,
    MailboxNotConfiguredError,
    MailboxUnavailableError,
)
from zkgmail_code_server.presenter import LookupPresenter


class RepositoryStub:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.requested = []

    async def latest_for(self, recipient):
        self.requested.append(recipient)
        if self.error:
            raise self.error
        return self.result


FIXED_NOW = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)


def test_presenter_returns_found_view_model():
    repository = RepositoryStub(
        CodeMessage("246810", "2026-08-17T01:59:30+00:00", cursor="42")
    )
    presenter = LookupPresenter(repository, clock=lambda: FIXED_NOW)

    model = asyncio.run(presenter.lookup(" Demo@ZKGMAIL.COM "))

    assert model.state is LookupState.FOUND
    assert model.status == 200
    assert model.email == "demo@zkgmail.com"
    assert model.code == "246810"
    assert model.as_payload()["receivedAt"] == "2026-08-17T01:59:30+00:00"
    assert model.as_payload()["cursor"] == "42"
    assert repository.requested == ["demo@zkgmail.com"]


@pytest.mark.parametrize(
    ("result", "error", "state", "status"),
    [
        (None, None, LookupState.WAITING, 404),
        (None, MailboxNotConfiguredError(), LookupState.UNCONFIGURED, 503),
        (None, MailboxUnavailableError(), LookupState.UNAVAILABLE, 502),
    ],
)
def test_presenter_maps_repository_outcomes(result, error, state, status):
    model = asyncio.run(
        LookupPresenter(RepositoryStub(result, error), clock=lambda: FIXED_NOW).lookup(
            "demo@zkgmail.com"
        )
    )
    assert model.state is state
    assert model.status == status
    assert model.as_payload()["ok"] is False


def test_presenter_rejects_other_domain_without_querying_repository():
    repository = RepositoryStub()
    model = asyncio.run(
        LookupPresenter(repository, clock=lambda: FIXED_NOW).lookup("demo@icloud.com")
    )
    assert model.state is LookupState.INVALID
    assert model.status == 400
    assert repository.requested == []


def test_presenter_waits_when_latest_message_matches_after_cursor():
    repository = RepositoryStub(
        CodeMessage("111111", "2026-08-17T01:58:00+00:00", cursor="41")
    )

    model = asyncio.run(
        LookupPresenter(repository, clock=lambda: FIXED_NOW).lookup(
            "repeat@zkgmail.com",
            after_cursor="41",
        )
    )

    assert model.state is LookupState.WAITING
    assert model.status == 404
    assert model.message == "暂未收到新的验证码，请稍后再试"
