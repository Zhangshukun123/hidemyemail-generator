"""Query the current ChatGPT account plan through ``accounts/check``.

The module intentionally keeps transport, response parsing, and presentation
separate.  Saved cookies are not plan evidence: the gateway authenticates with
the access token and the presenter only falls back to JWT claims after a valid
2xx ``accounts/check`` response selected an account entry.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote


ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
ACCOUNTS_CHECK_ORIGIN = "https://chatgpt.com"
DEFAULT_IMPERSONATE = "chrome136"
DEFAULT_TIMEOUT_SECONDS = 20.0

_CHROME_VERSION = "136.0.0.0"
_CHROME_MAJOR = _CHROME_VERSION.split(".", 1)[0]
_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{_CHROME_VERSION} Safari/537.36"
)


def normalize_access_token(value: str) -> str:
    """Return a bare access token accepted by the Authorization header."""

    token = str(value or "").strip().strip('"').strip("'")
    if token.casefold().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.casefold().startswith("bearer "):
        token = token[7:].strip()
    return token


def decode_jwt_payload_unverified(token: str) -> dict[str, Any]:
    """Decode JWT metadata locally without treating it as verified evidence."""

    parts = normalize_access_token(token).split(".")
    if len(parts) < 2:
        return {}
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _claim_text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if str(value or "").strip():
            return str(value).strip()
    return ""


@dataclass(frozen=True)
class AccountPlanModel:
    """Input model for a single access-token plan lookup."""

    access_token: str
    proxy_url: str = ""
    device_id: str = ""
    language: str = "en-US"
    timezone_offset_min: str | int = "-"

    @property
    def token(self) -> str:
        return normalize_access_token(self.access_token)

    @property
    def claims(self) -> dict[str, Any]:
        return decode_jwt_payload_unverified(self.token)

    @property
    def claim_account_id(self) -> str:
        payload = self.claims
        auth = payload.get("https://api.openai.com/auth")
        auth = auth if isinstance(auth, Mapping) else {}
        return _claim_text(
            auth,
            "chatgpt_account_id",
            "account_id",
            "workspace_id",
        ) or _claim_text(payload, "account_id", "workspace_id")

    @property
    def claim_plan_type(self) -> str:
        payload = self.claims
        auth = payload.get("https://api.openai.com/auth")
        auth = auth if isinstance(auth, Mapping) else {}
        return _claim_text(auth, "chatgpt_plan_type", "plan_type") or _claim_text(
            payload, "chatgpt_plan_type", "plan_type"
        )


@dataclass(frozen=True)
class AccountPlanGatewayResponse:
    """Transport-level response returned by :class:`AccountPlanGateway`."""

    http_status: int | None
    payload: dict[str, Any] | None = None
    detail: str = ""

    @property
    def successful(self) -> bool:
        return (
            self.http_status is not None
            and 200 <= self.http_status < 300
            and isinstance(self.payload, dict)
        )


@dataclass(frozen=True)
class AccountPlanResult:
    """Stable view model consumed by account-management callers."""

    status: str
    plan_type: str = ""
    source: str = "accounts_check"
    detail: str = ""
    http_status: int | None = None
    account_id: str = ""
    selected_account_key: str = ""
    claim_plan_type: str = ""
    has_active_subscription: bool = False
    plus_trial_eligible: bool = False
    needs_refresh: bool = False

    @property
    def ok(self) -> bool:
        return self.status in {"free", "plus"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"ok": self.ok}


def accounts_check_url(timezone_offset_min: str | int = "-") -> str:
    """Build the versioned endpoint including its required query string."""

    offset = str(timezone_offset_min if timezone_offset_min is not None else "-")
    return (
        f"{ACCOUNTS_CHECK_ORIGIN}{ACCOUNTS_CHECK_PATH}"
        f"?timezone_offset_min={quote(offset, safe='')}"
    )


def _accept_language(language: str) -> str:
    primary = str(language or "en-US").strip() or "en-US"
    root = primary.split("-", 1)[0]
    if primary.casefold() == "en-us":
        return "en-US,en;q=0.9"
    return f"{primary},{root};q=0.9,en-US;q=0.8,en;q=0.7"


def browser_headers(model: AccountPlanModel, *, device_id: str) -> dict[str, str]:
    """Build headers consistent with the gateway's Chrome TLS identity."""

    language = str(model.language or "en-US").strip() or "en-US"
    return {
        "accept": "*/*",
        "accept-language": _accept_language(language),
        "authorization": f"Bearer {model.token}",
        "oai-device-id": device_id,
        "oai-language": language,
        "priority": "u=1, i",
        "referer": f"{ACCOUNTS_CHECK_ORIGIN}/",
        "sec-ch-ua": (
            f'"Google Chrome";v="{_CHROME_MAJOR}", '
            f'"Chromium";v="{_CHROME_MAJOR}", "Not.A/Brand";v="24"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": _CHROME_USER_AGENT,
        "x-openai-target-path": ACCOUNTS_CHECK_PATH,
        "x-openai-target-route": ACCOUNTS_CHECK_PATH,
    }


def _default_session_factory(**kwargs: Any) -> Any:
    from curl_cffi.requests import Session

    return Session(**kwargs)


def _response_status(response: Any) -> int | None:
    try:
        value = getattr(response, "status_code", None)
        if value is None:
            value = getattr(response, "status", None)
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _response_text(response: Any) -> str:
    try:
        value = getattr(response, "text", "")
        value = value() if callable(value) else value
        return str(value or "")
    except Exception:
        return ""


def _response_payload(response: Any) -> dict[str, Any] | None:
    try:
        value = response.json()
    except Exception:
        text = _response_text(response).strip()
        if not text.startswith("{"):
            return None
        try:
            value = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    return dict(value) if isinstance(value, Mapping) else None


class AccountPlanGateway:
    """curl_cffi gateway for one isolated ``accounts/check`` request."""

    def __init__(
        self,
        *,
        session_factory: Callable[..., Any] | None = None,
        impersonate: str = DEFAULT_IMPERSONATE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory or _default_session_factory
        self._impersonate = str(impersonate or DEFAULT_IMPERSONATE)
        self._timeout = max(1.0, float(timeout))

    def fetch(self, model: AccountPlanModel) -> AccountPlanGatewayResponse:
        if not model.token:
            return AccountPlanGatewayResponse(None, detail="access token is empty")

        session: Any | None = None
        response: Any | None = None
        try:
            session = self._session_factory(impersonate=self._impersonate)
            proxy = str(model.proxy_url or "").strip()
            if proxy:
                proxies = getattr(session, "proxies", None)
                if proxies is None:
                    session.proxies = {"http": proxy, "https": proxy}
                else:
                    proxies.update({"http": proxy, "https": proxy})

            device_id = str(model.device_id or "").strip() or str(uuid.uuid4())
            cookies = getattr(session, "cookies", None)
            if cookies is not None and hasattr(cookies, "set"):
                cookies.set("oai-did", device_id, domain="chatgpt.com", path="/")

            response = session.get(
                accounts_check_url(model.timezone_offset_min),
                headers=browser_headers(model, device_id=device_id),
                allow_redirects=False,
                timeout=self._timeout,
            )
            status = _response_status(response)
            if status is None:
                return AccountPlanGatewayResponse(None, detail="response has no HTTP status")
            if not 200 <= status < 300:
                preview = _response_text(response).strip()[:300]
                return AccountPlanGatewayResponse(
                    status,
                    detail=f"HTTP {status}" + (f": {preview}" if preview else ""),
                )

            payload = _response_payload(response)
            if payload is None:
                return AccountPlanGatewayResponse(
                    status,
                    detail="accounts/check response is not a JSON object",
                )
            return AccountPlanGatewayResponse(status, payload=payload)
        except Exception as exc:
            return AccountPlanGatewayResponse(
                _response_status(response) if response is not None else None,
                detail=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass


def _select_account(
    payload: Mapping[str, Any], claim_account_id: str
) -> tuple[str, Mapping[str, Any]]:
    accounts = payload.get("accounts")
    if not isinstance(accounts, Mapping):
        raise ValueError("accounts/check response is missing accounts")

    if claim_account_id and isinstance(accounts.get(claim_account_id), Mapping):
        return claim_account_id, accounts[claim_account_id]
    if isinstance(accounts.get("default"), Mapping):
        return "default", accounts["default"]
    for key, value in accounts.items():
        if isinstance(value, Mapping):
            return str(key), value
    raise ValueError("accounts/check response has no account entry")


def _normalized_plan(value: Any) -> str:
    return str(value or "").strip().casefold().replace("_", "").replace("-", "")


def _classify_plan(plan_type: str) -> str:
    normalized = _normalized_plan(plan_type)
    if normalized in {"plus", "chatgptplusplan"}:
        return "plus"
    if normalized in {"free", "chatgptfreeplan"}:
        return "free"
    return "unknown"


class AccountPlanPresenter:
    """Turn gateway and model data into a conservative account-plan decision."""

    def __init__(self, gateway: AccountPlanGateway | None = None) -> None:
        self._gateway = gateway or AccountPlanGateway()

    def check(
        self,
        access_token: str,
        *,
        proxy_url: str = "",
        device_id: str = "",
        language: str = "en-US",
        timezone_offset_min: str | int = "-",
    ) -> AccountPlanResult:
        return self.present(
            AccountPlanModel(
                access_token=access_token,
                proxy_url=proxy_url,
                device_id=device_id,
                language=language,
                timezone_offset_min=timezone_offset_min,
            )
        )

    def present(self, model: AccountPlanModel) -> AccountPlanResult:
        gateway_result = self._gateway.fetch(model)
        common = {
            "http_status": gateway_result.http_status,
            "claim_plan_type": model.claim_plan_type,
        }
        if gateway_result.http_status == 401:
            return AccountPlanResult(
                status="needs_refresh",
                detail=gateway_result.detail or "access token needs refresh",
                needs_refresh=True,
                **common,
            )
        if not gateway_result.successful:
            return AccountPlanResult(
                status="unknown",
                detail=gateway_result.detail or "accounts/check did not succeed",
                **common,
            )

        try:
            selected_key, item = _select_account(
                gateway_result.payload or {}, model.claim_account_id
            )
        except ValueError as exc:
            return AccountPlanResult(status="unknown", detail=str(exc), **common)

        account = item.get("account")
        account = account if isinstance(account, Mapping) else {}
        entitlement = item.get("entitlement")
        entitlement = entitlement if isinstance(entitlement, Mapping) else {}
        raw_plan = _claim_text(account, "plan_type")
        subscription_plan = _claim_text(entitlement, "subscription_plan")
        has_active_subscription = bool(
            entitlement.get("has_active_subscription", False)
        )

        # API account data wins.  Subscription data is also online evidence, but
        # a Plus subscription is only current when the entitlement says active.
        plan_type = raw_plan
        source = "accounts_check"
        status = _classify_plan(raw_plan)
        if not raw_plan and subscription_plan:
            plan_type = subscription_plan
            status = _classify_plan(subscription_plan)
            if status == "plus" and not has_active_subscription:
                status = "unknown"
        if not plan_type and model.claim_plan_type:
            # A claim may fill a field only after a valid 2xx response and strict
            # account selection.  It never rescues 401/403/network failures.
            plan_type = model.claim_plan_type
            status = _classify_plan(plan_type)
            source = "access_token_claim_after_accounts_check"

        campaigns = item.get("eligible_promo_campaigns")
        campaigns = campaigns if isinstance(campaigns, Mapping) else {}
        plus_trial_eligible = bool(campaigns.get("plus")) and status == "free"
        account_id = _claim_text(account, "account_id")
        if not account_id and selected_key != "default":
            account_id = selected_key
        if not account_id:
            account_id = model.claim_account_id

        detail = (
            f"accounts/check selected {selected_key}; plan={plan_type}"
            if status != "unknown"
            else f"accounts/check selected {selected_key}; plan is unknown"
        )
        return AccountPlanResult(
            status=status,
            plan_type=plan_type,
            source=source,
            detail=detail,
            account_id=account_id,
            selected_account_key=selected_key,
            has_active_subscription=has_active_subscription,
            plus_trial_eligible=plus_trial_eligible,
            **common,
        )


__all__ = [
    "ACCOUNTS_CHECK_PATH",
    "ACCOUNTS_CHECK_ORIGIN",
    "AccountPlanGateway",
    "AccountPlanGatewayResponse",
    "AccountPlanModel",
    "AccountPlanPresenter",
    "AccountPlanResult",
    "accounts_check_url",
    "browser_headers",
    "decode_jwt_payload_unverified",
    "normalize_access_token",
]
