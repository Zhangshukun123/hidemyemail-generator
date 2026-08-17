from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from hidemyemail_generator.account_plan import (
    ACCOUNTS_CHECK_PATH,
    AccountPlanGateway,
    AccountPlanPresenter,
)


def _jwt(*, account_id: str = "acct-jwt", plan: str = "free") -> str:
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": plan,
        }
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"header.{encoded}.signature"


class FakeCookies:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def set(self, name: str, value: str, **kwargs: str) -> None:
        self.calls.append((name, value, kwargs))


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, Any] | None = None,
        *,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(
        self,
        response: FakeResponse | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.proxies: dict[str, str] = {}
        self.cookies = FakeCookies()
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response

    def close(self) -> None:
        self.closed = True


def _presenter(session: FakeSession) -> AccountPlanPresenter:
    return AccountPlanPresenter(
        AccountPlanGateway(session_factory=lambda **_kwargs: session)
    )


def _entry(
    plan: str | None,
    *,
    account_id: str,
    active: bool = False,
    subscription_plan: str = "",
    plus_trial: bool = False,
) -> dict[str, Any]:
    account: dict[str, Any] = {"account_id": account_id}
    if plan is not None:
        account["plan_type"] = plan
    return {
        "account": account,
        "entitlement": {
            "has_active_subscription": active,
            "subscription_plan": subscription_plan,
        },
        "eligible_promo_campaigns": (
            {"plus": {"id": "trial-campaign"}} if plus_trial else {}
        ),
    }


def test_api_plus_overrides_stale_free_jwt_claim() -> None:
    response = FakeResponse(
        200,
        {
            "accounts": {
                "acct-jwt": _entry(
                    "plus", account_id="acct-jwt", active=True
                )
            }
        },
    )

    result = _presenter(FakeSession(response)).check(_jwt(plan="free"))

    assert result.ok is True
    assert result.status == "plus"
    assert result.plan_type == "plus"
    assert result.source == "accounts_check"
    assert result.has_active_subscription is True


def test_401_requires_refresh_without_using_free_claim() -> None:
    result = _presenter(FakeSession(FakeResponse(401, text="unauthorized"))).check(
        _jwt(plan="free")
    )

    assert result.ok is False
    assert result.status == "needs_refresh"
    assert result.needs_refresh is True
    assert result.http_status == 401
    assert result.plan_type == ""
    assert result.claim_plan_type == "free"


@pytest.mark.parametrize("status_code", [403, 429, 500])
def test_non_401_failure_is_unknown_without_using_claim(status_code: int) -> None:
    result = _presenter(FakeSession(FakeResponse(status_code))).check(
        _jwt(plan="plus")
    )

    assert result.ok is False
    assert result.status == "unknown"
    assert result.needs_refresh is False
    assert result.plan_type == ""
    assert result.claim_plan_type == "plus"


def test_network_failure_is_unknown_without_using_claim() -> None:
    result = _presenter(
        FakeSession(error=TimeoutError("upstream timed out"))
    ).check(_jwt(plan="plus"))

    assert result.status == "unknown"
    assert result.http_status is None
    assert result.plan_type == ""
    assert "TimeoutError" in result.detail


def test_account_selection_uses_claim_then_default_then_first_entry() -> None:
    claimed_payload = {
        "accounts": {
            "first": _entry("free", account_id="first"),
            "default": _entry("free", account_id="acct-default"),
            "acct-jwt": _entry("plus", account_id="acct-jwt", active=True),
        }
    }
    claimed = _presenter(FakeSession(FakeResponse(200, claimed_payload))).check(
        _jwt(account_id="acct-jwt")
    )
    assert claimed.status == "plus"
    assert claimed.selected_account_key == "acct-jwt"

    default_payload = {
        "accounts": {
            "first": _entry("plus", account_id="first", active=True),
            "default": _entry("free", account_id="acct-default"),
        }
    }
    default = _presenter(FakeSession(FakeResponse(200, default_payload))).check(
        _jwt(account_id="missing")
    )
    assert default.status == "free"
    assert default.selected_account_key == "default"

    first_payload = {
        "accounts": {
            "first": _entry("plus", account_id="first", active=True),
            "second": _entry("free", account_id="second"),
        }
    }
    first = _presenter(FakeSession(FakeResponse(200, first_payload))).check(
        _jwt(account_id="missing")
    )
    assert first.status == "plus"
    assert first.selected_account_key == "first"


def test_plus_trial_offer_does_not_turn_free_account_into_plus() -> None:
    response = FakeResponse(
        200,
        {
            "accounts": {
                "acct-jwt": _entry(
                    "free", account_id="acct-jwt", plus_trial=True
                )
            }
        },
    )

    result = _presenter(FakeSession(response)).check(_jwt(plan="plus"))

    assert result.status == "free"
    assert result.source == "accounts_check"
    assert result.plus_trial_eligible is True


def test_claim_only_fills_missing_plan_after_successful_account_response() -> None:
    response = FakeResponse(
        204,
        {"accounts": {"acct-jwt": _entry(None, account_id="acct-jwt")}},
    )

    result = _presenter(FakeSession(response)).check(_jwt(plan="free"))

    assert result.status == "free"
    assert result.plan_type == "free"
    assert result.source == "access_token_claim_after_accounts_check"


def test_gateway_sends_full_url_browser_headers_proxy_and_device_cookie() -> None:
    session = FakeSession(
        FakeResponse(
            200,
            {"accounts": {"acct-jwt": _entry("free", account_id="acct-jwt")}},
        )
    )
    factory_calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> FakeSession:
        factory_calls.append(kwargs)
        return session

    presenter = AccountPlanPresenter(
        AccountPlanGateway(
            session_factory=factory,
            impersonate="chrome136",
            timeout=12.5,
        )
    )
    result = presenter.check(
        f"Authorization: Bearer {_jwt()}",
        proxy_url="http://127.0.0.1:7890",
        device_id="device-123",
        language="zh-CN",
        timezone_offset_min=-480,
    )

    assert result.status == "free"
    assert factory_calls == [{"impersonate": "chrome136"}]
    assert session.closed is True
    assert session.proxies == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
    assert session.cookies.calls == [
        ("oai-did", "device-123", {"domain": "chatgpt.com", "path": "/"})
    ]

    url, kwargs = session.calls[0]
    assert url == (
        "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
        "?timezone_offset_min=-480"
    )
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == 12.5
    headers = kwargs["headers"]
    assert headers["authorization"] == f"Bearer {_jwt()}"
    assert headers["oai-device-id"] == "device-123"
    assert headers["oai-language"] == "zh-CN"
    assert headers["sec-fetch-site"] == "same-origin"
    assert headers["sec-fetch-mode"] == "cors"
    assert headers["sec-fetch-dest"] == "empty"
    assert headers["x-openai-target-path"] == ACCOUNTS_CHECK_PATH
    assert headers["x-openai-target-route"] == ACCOUNTS_CHECK_PATH
    assert "Chrome/136" in headers["user-agent"]
    assert '"Windows"' == headers["sec-ch-ua-platform"]
