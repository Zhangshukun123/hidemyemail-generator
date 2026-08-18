from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hidemyemail_generator.account_plan import AccountPlanResult
from hidemyemail_generator.inbox import connect_db
from protocol_registration_server.model import OfferPoolRepository
from protocol_registration_server.network import CheckoutOfferProbe
from protocol_registration_server.presenter import (
    AccountVerificationPresenter,
    OfferPresenter,
)


class FakeOfferView:
    def __init__(self, outcomes: dict[str, CheckoutOfferProbe | Exception]) -> None:
        self.outcomes = outcomes
        self.checkout_calls: list[str] = []

    def check_checkout(self, access_token: str, country: str) -> CheckoutOfferProbe:
        assert access_token == "access-token"
        self.checkout_calls.append(country)
        outcome = self.outcomes[country]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def checkout_probe(
    country: str,
    *,
    checkout_country: str = "",
    amount: str = "0",
    paypal: bool = True,
) -> CheckoutOfferProbe:
    return CheckoutOfferProbe(
        exit_country=country,
        checkout_country=checkout_country or country,
        currency="EUR" if (checkout_country or country) == "DE" else "USD",
        amount_minor=amount,
        amount_source="total_summary.due",
        paypal_available=paypal,
        checkout_url=f"https://chatgpt.com/checkout/{country}",
        payment_methods=("card", "paypal") if paypal else ("card",),
    )


def save_account(db_file: Path, email: str) -> None:
    record = {
        "email": email,
        "session": {"accessToken": "access-token", "sessionToken": "session-token"},
        "registration_environment": {
            "exit_ip": "203.0.113.30",
            "exit_country": "JP",
            "proxy_mode": "clash",
        },
    }
    with connect_db(str(db_file)) as connection:
        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            (f"gpt_account:{email}", json.dumps(record)),
        )


def build_presenter(
    tmp_path: Path,
    outcomes: dict[str, CheckoutOfferProbe | Exception],
) -> tuple[OfferPresenter, OfferPoolRepository, FakeOfferView]:
    shared = tmp_path / "shared.db"
    repository = OfferPoolRepository(tmp_path / "offers.db")
    email = "account@example.com"
    save_account(shared, email)
    view = FakeOfferView(outcomes)
    return (
        OfferPresenter(shared_db=shared, repository=repository, view=view),
        repository,
        view,
    )


def test_eligible_account_enters_offer_pool_after_checkout(tmp_path: Path) -> None:
    presenter, repository, view = build_presenter(
        tmp_path,
        {
            "US": checkout_probe("US", amount="2000"),
            "GB": checkout_probe("GB", amount="0"),
            "DE": checkout_probe("DE", amount="0"),
        },
    )

    result = presenter.process("account@example.com")

    assert result["pool"] == "offer"
    assert result["checkout_submitted"] is True
    assert result["checkout_country"] == "GB"
    assert result["checkout_amount_minor"] == "0"
    assert result["paypal_available"] is True
    assert view.checkout_calls == ["US", "GB"]
    snapshot = repository.snapshot()
    assert snapshot["offerCount"] == 1
    assert snapshot["items"][0]["registrationIp"] == "203.0.113.30"
    assert snapshot["items"][0]["registrationProxyMode"] == "clash"


def test_ineligible_account_enters_no_offer_pool(tmp_path: Path) -> None:
    presenter, repository, view = build_presenter(
        tmp_path,
        {
            "US": checkout_probe("US", amount="2000"),
            "GB": checkout_probe("GB", amount="0", paypal=False),
            "DE": checkout_probe("DE", amount="2000"),
        },
    )

    result = presenter.process("account@example.com")

    assert result["pool"] == "no_offer"
    assert view.checkout_calls == ["US", "GB", "DE"]
    assert repository.snapshot()["noOfferCount"] == 1


def test_refresh_rechecks_checkout_and_moves_pool(tmp_path: Path) -> None:
    presenter, repository, view = build_presenter(
        tmp_path,
        {
            "US": checkout_probe("US", amount="2000"),
            "GB": checkout_probe("GB", amount="2000"),
            "DE": checkout_probe("DE", amount="2000"),
        },
    )
    first = presenter.process("account@example.com")
    view.outcomes["US"] = checkout_probe("US", amount="0")

    refreshed = presenter.process("account@example.com")

    assert first["pool"] == "no_offer"
    assert refreshed["pool"] == "offer"
    assert refreshed["checkout_url"] == "https://chatgpt.com/checkout/US"
    assert view.checkout_calls == ["US", "GB", "DE", "US"]
    assert repository.get("ACCOUNT@example.com") is not None


def test_uncertain_checkout_result_enters_pending_pool(tmp_path: Path) -> None:
    presenter, repository, _ = build_presenter(
        tmp_path,
        {
            "US": RuntimeError("temporary"),
            "GB": checkout_probe("GB", amount="2000"),
            "DE": checkout_probe("DE", amount="2000"),
        },
    )

    result = presenter.process("account@example.com")

    assert result["status"] == "error"
    assert result["pool"] == ""
    assert repository.snapshot()["pendingCount"] == 1


def test_shared_database_lock_becomes_retryable_offer_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    presenter, repository, _ = build_presenter(
        tmp_path,
        {
            "US": checkout_probe("US"),
            "GB": checkout_probe("GB"),
            "DE": checkout_probe("DE"),
        },
    )

    def locked(_email: str):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(presenter.account_repository, "load", locked)

    result = presenter.process("account@example.com")

    assert result["status"] == "error"
    assert result["pool"] == ""
    assert "database is locked" in result["detail"]
    assert repository.snapshot()["pendingCount"] == 1


def test_new_account_is_verified_immediately_with_registration_route(
    tmp_path: Path,
) -> None:
    class FakePlanPresenter:
        def check(self, access_token: str, *, proxy_url: str, **kwargs):
            assert access_token == "access-token"
            assert proxy_url == "http://registration-proxy"
            return AccountPlanResult(status="free", detail="verified")

    shared = tmp_path / "shared.db"
    save_account(shared, "account@example.com")
    with connect_db(str(shared)) as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key='gpt_account:account@example.com'"
        ).fetchone()
        record = json.loads(row["value"])
        record["registration_proxy_url"] = "http://registration-proxy"
        connection.execute(
            "UPDATE settings SET value=? WHERE key='gpt_account:account@example.com'",
            (json.dumps(record),),
        )

    result = AccountVerificationPresenter(
        shared,
        plan_presenter=FakePlanPresenter(),
    ).process("account@example.com")

    assert result["verified"] is True
    assert result["planStatus"] == "free"
