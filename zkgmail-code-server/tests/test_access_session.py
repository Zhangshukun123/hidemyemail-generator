from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from zkgmail_code_server.access_session import AccessSessionStore


def test_session_expires_at_exact_max_age_boundary():
    now = [10.0]
    store = AccessSessionStore(
        max_age_seconds=300,
        clock=lambda: now[0],
    )
    token = store.issue("alias@zkgmail.com", invite_id="invite-expiry")

    now[0] = 309.999
    assert store.scope(token) == "alias@zkgmail.com"

    now[0] = 310.0
    assert store.scope(token) == ""


def test_session_lifetime_cannot_outlive_the_invite_remainder():
    now = [20.0]
    store = AccessSessionStore(
        max_age_seconds=300,
        clock=lambda: now[0],
    )
    token = store.issue(
        "alias@zkgmail.com",
        invite_id="short-lived-invite",
        max_age_seconds=30,
    )

    now[0] = 49.999
    assert store.scope(token) == "alias@zkgmail.com"
    now[0] = 50.0
    assert store.scope(token) == ""


def test_session_capacity_evicts_oldest_live_session():
    now = [100.0]
    store = AccessSessionStore(
        max_age_seconds=300,
        max_sessions=32,
        clock=lambda: now[0],
    )
    tokens: list[str] = []
    for index in range(32):
        tokens.append(
            store.issue(
                f"user{index}@zkgmail.com",
                invite_id=f"invite-{index}",
            )
        )
        now[0] += 1

    newest = store.issue("newest@zkgmail.com", invite_id="invite-newest")

    assert store.scope(tokens[0]) == ""
    assert store.scope(tokens[1]) == "user1@zkgmail.com"
    assert store.scope(newest) == "newest@zkgmail.com"


def test_each_issued_session_has_a_distinct_opaque_value():
    store = AccessSessionStore(max_age_seconds=300)
    with mock.patch(
        "zkgmail_code_server.access_session.secrets.token_urlsafe",
        side_effect=["opaque-session-one", "opaque-session-two"],
    ):
        first = store.issue("alias@zkgmail.com", invite_id="shared-invite")
        second = store.issue("alias@zkgmail.com", invite_id="shared-invite")

    assert first != second
    assert store.scope(first) == "alias@zkgmail.com"
    assert store.scope(second) == "alias@zkgmail.com"


def test_per_invite_capacity_evicts_only_that_invites_oldest_session():
    now = [500.0]
    store = AccessSessionStore(
        max_age_seconds=300,
        max_sessions_per_invite=2,
        clock=lambda: now[0],
    )
    unrelated = store.issue("other@zkgmail.com", invite_id="other-invite")
    first = store.issue("alias@zkgmail.com", invite_id="shared-invite")
    now[0] += 1
    second = store.issue("alias@zkgmail.com", invite_id="shared-invite")
    now[0] += 1
    newest = store.issue("alias@zkgmail.com", invite_id="shared-invite")

    assert store.scope(first) == ""
    assert store.scope(second) == "alias@zkgmail.com"
    assert store.scope(newest) == "alias@zkgmail.com"
    assert store.scope(unrelated) == "other@zkgmail.com"


def test_live_session_survives_store_recreation(tmp_path: Path):
    now = [1_786_977_600.0]
    storage_path = tmp_path / "access_sessions.json"
    first_store = AccessSessionStore(
        max_age_seconds=300,
        clock=lambda: now[0],
        storage_path=storage_path,
    )
    token = first_store.issue("alias@zkgmail.com", invite_id="restart-safe")

    restarted_store = AccessSessionStore(
        max_age_seconds=300,
        clock=lambda: now[0],
        storage_path=storage_path,
    )

    assert restarted_store.scope(token) == "alias@zkgmail.com"
    persisted = json.loads(storage_path.read_text(encoding="utf-8"))
    assert persisted["version"] == 1
    assert token not in storage_path.read_text(encoding="utf-8")


def test_corrupt_persisted_store_is_ignored(tmp_path: Path):
    storage_path = tmp_path / "access_sessions.json"
    storage_path.write_text("not-json", encoding="utf-8")

    store = AccessSessionStore(max_age_seconds=300, storage_path=storage_path)
    token = store.issue("alias@zkgmail.com", invite_id="replacement")

    assert store.scope(token) == "alias@zkgmail.com"
    assert json.loads(storage_path.read_text(encoding="utf-8"))["version"] == 1
