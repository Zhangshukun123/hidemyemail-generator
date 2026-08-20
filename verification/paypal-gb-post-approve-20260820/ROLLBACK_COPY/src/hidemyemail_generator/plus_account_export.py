"""Export paid Plus accounts with completed Codex OAuth credentials.

The module follows MVP at the application boundary and Strategy for the two
wire formats.  The model is deliberately strict: an account is exportable only
when payment, Plus-code completion, credential completeness, and token expiry
all agree.  Export strategies never synthesize or substitute credentials.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from .inbox import connect_db


JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)
JSON_CONTENT_TYPE = "application/json; charset=utf-8"
SUPPORTED_EXPORT_FORMATS = ("cpa", "sub2api")
_SETTING_PREFIX = "gpt_account:"
_SAFE_FILENAME_CHARACTER = re.compile(r"[^a-z0-9._-]+")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")
_PLACEHOLDER_VALUES = {
    "missing",
    "none",
    "null",
    "placeholder",
    "undefined",
    "__missing_refresh_token__",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    return _aware_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    return None


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return {}
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        parsed = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _auth_claims(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("https://api.openai.com/auth")
    return dict(value) if isinstance(value, dict) else {}


def _profile_claims(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("https://api.openai.com/profile")
    return dict(value) if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _texts(*values: Any) -> list[str]:
    return [text for value in values if (text := _text(value))]


def _credential(value: Any) -> str:
    text = _text(value)
    folded = text.casefold()
    if (
        not text
        or len(text) < 2
        or folded in _PLACEHOLDER_VALUES
        or "placeholder" in folded
        or (text.startswith("<") and text.endswith(">"))
    ):
        return ""
    return text


def _is_paid_plan(value: str) -> bool:
    folded = value.casefold()
    return any(marker in folded for marker in ("plus", "pro", "team", "enterprise"))


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = _text(value)
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return _aware_utc(parsed)
    return _timestamp(number)


def _token_expiry(
    oauth: Mapping[str, Any],
    plus_codex: Mapping[str, Any],
    access_payload: Mapping[str, Any],
) -> datetime | None:
    # The signed access-token claim is the strongest local expiry signal.
    expiry = _timestamp(access_payload.get("exp"))
    if expiry is not None:
        return expiry

    for candidate in (
        oauth.get("expires_at_unix"),
        oauth.get("expires_at"),
        plus_codex.get("expires_at_unix"),
        plus_codex.get("expires_at"),
    ):
        expiry = _timestamp(candidate)
        if expiry is not None:
            return expiry

    try:
        expires_in = int(oauth.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in <= 0:
        return None
    refreshed_at = _timestamp(
        oauth.get("last_refresh")
        or oauth.get("refreshed_at")
        or oauth.get("created_at")
    )
    return refreshed_at + timedelta(seconds=expires_in) if refreshed_at else None


def _email_key(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", email.casefold()).strip("_")


def _normalize_email(value: Any) -> str:
    email = _text(value).casefold()
    if (
        not email
        or len(email) > 320
        or _EMAIL_PATTERN.fullmatch(email) is None
        or email.split("@", 1)[0].startswith(".")
        or email.split("@", 1)[0].endswith(".")
        or ".." in email
    ):
        raise ValueError("邮箱地址无效")
    return email


@dataclass(frozen=True, slots=True)
class ExportablePlusAccount:
    """Normalized, verified input shared by every export strategy."""

    email: str
    access_token: str
    refresh_token: str
    id_token: str
    account_id: str
    user_id: str
    plan: str
    expires_at: datetime
    last_refresh: datetime | None = None

    @property
    def expires_at_unix(self) -> int:
        return int(self.expires_at.timestamp())

    def expires_in(self, exported_at: datetime) -> int:
        return max(0, int((self.expires_at - exported_at).total_seconds()))


class PlusAccountExportModel:
    """Read and validate exportable records from ``settings``."""

    def __init__(
        self,
        db_file: str | Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.db_file = Path(db_file)
        self.clock = clock

    def eligible_accounts(
        self,
        email: str | None = None,
        *,
        now: datetime | None = None,
    ) -> list[ExportablePlusAccount]:
        """Return sorted eligible accounts, optionally restricted to one email."""

        target = _normalize_email(email) if email is not None else None
        connection = connect_db(str(self.db_file))
        try:
            if target is None:
                rows = connection.execute(
                    """
                    SELECT key, value
                    FROM settings
                    WHERE key LIKE 'gpt_account:%'
                    ORDER BY lower(key), key
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT key, value FROM settings WHERE key = ?",
                    (f"{_SETTING_PREFIX}{target}",),
                ).fetchall()
        finally:
            connection.close()

        current = _aware_utc(now or self.clock())
        accounts: list[ExportablePlusAccount] = []
        for row in rows:
            setting_key = _text(row["key"])
            if not setting_key.startswith(_SETTING_PREFIX):
                continue
            setting_email = setting_key.removeprefix(_SETTING_PREFIX).casefold()
            try:
                record = json.loads(str(row["value"] or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            account = self._normalize_record(setting_email, record, current)
            if account is not None:
                accounts.append(account)
        return accounts

    # Compatibility names keep route and CLI adapters free to choose vocabulary.
    list_exportable = eligible_accounts
    load = eligible_accounts

    @staticmethod
    def _normalize_record(
        setting_email: str,
        record: Any,
        now: datetime,
    ) -> ExportablePlusAccount | None:
        record = _json_object(record)
        if record is None:
            return None
        if _text(record.get("account_type")).casefold() != "plus":
            return None

        payment = _json_object(record.get("payment_confirmation"))
        if (
            payment is None
            or _text(payment.get("status")).casefold() != "plus"
            or payment.get("payment_succeeded") is not True
        ):
            return None

        plus_codex = _json_object(record.get("plus_codex"))
        if (
            plus_codex is None
            or _text(plus_codex.get("status")).casefold() != "completed"
            or plus_codex.get("export_ready") is not True
        ):
            return None

        oauth = _json_object(record.get("codex_oauth"))
        if oauth is None:
            oauth = _json_object(plus_codex.get("codex_oauth"))
        if oauth is None:
            return None
        oauth_status = _text(oauth.get("status")).casefold()
        if oauth_status and oauth_status not in {"completed", "ready", "success"}:
            return None

        tokens = _json_object(oauth.get("tokens")) or {}
        access_token = _credential(
            _first_text(oauth.get("access_token"), tokens.get("access_token"))
        )
        refresh_token = _credential(
            _first_text(oauth.get("refresh_token"), tokens.get("refresh_token"))
        )
        id_token = _credential(
            _first_text(oauth.get("id_token"), tokens.get("id_token"))
        )
        account_id = _credential(
            _first_text(oauth.get("account_id"), tokens.get("account_id"))
        )
        if not all((access_token, refresh_token, id_token, account_id)):
            return None
        if any(
            value.get("id_token_synthetic") is True
            for value in (record, plus_codex, oauth, tokens)
        ):
            return None

        access_payload = _decode_jwt_payload(access_token)
        id_payload = _decode_jwt_payload(id_token)
        access_auth = _auth_claims(access_payload)
        id_auth = _auth_claims(id_payload)
        access_profile = _profile_claims(access_payload)
        id_profile = _profile_claims(id_payload)

        claimed_account_ids = _texts(
            access_auth.get("chatgpt_account_id"),
            access_payload.get("chatgpt_account_id"),
            id_auth.get("chatgpt_account_id"),
            id_payload.get("chatgpt_account_id"),
        )
        if any(value != account_id for value in claimed_account_ids):
            return None

        try:
            email = _normalize_email(setting_email)
        except ValueError:
            return None
        email_values = _texts(
            oauth.get("email"),
            record.get("email"),
            access_profile.get("email"),
            access_payload.get("email"),
            id_profile.get("email"),
            id_payload.get("email"),
        )
        try:
            normalized_emails = [_normalize_email(value) for value in email_values]
        except ValueError:
            return None
        if any(value != email for value in normalized_emails):
            return None

        user_ids = _texts(
            oauth.get("chatgpt_user_id"),
            oauth.get("user_id"),
            access_auth.get("chatgpt_user_id"),
            access_auth.get("user_id"),
            id_auth.get("chatgpt_user_id"),
            id_auth.get("user_id"),
        )
        if len(set(user_ids)) > 1:
            return None
        user_id = user_ids[0] if user_ids else ""

        plan_values = _texts(
            oauth.get("plan_type"),
            access_auth.get("chatgpt_plan_type"),
            access_auth.get("plan_type"),
            id_auth.get("chatgpt_plan_type"),
            id_auth.get("plan_type"),
            payment.get("plan"),
        )
        if any(not _is_paid_plan(value) for value in plan_values):
            return None

        expiry = _token_expiry(oauth, plus_codex, access_payload)
        id_expiry = _timestamp(id_payload.get("exp"))
        if (
            expiry is None
            or expiry <= now
            or (id_expiry is not None and id_expiry <= now)
        ):
            return None

        last_refresh = _timestamp(
            oauth.get("last_refresh")
            or oauth.get("refreshed_at")
            or plus_codex.get("completed_at")
        )
        return ExportablePlusAccount(
            email=email,
            access_token=access_token,
            refresh_token=refresh_token,
            id_token=id_token,
            account_id=account_id,
            user_id=user_id,
            plan="plus",
            expires_at=expiry,
            last_refresh=last_refresh,
        )


class PlusAccountExportStrategy(Protocol):
    """Strategy interface consumed by the presenter."""

    format_name: str

    def build(
        self,
        accounts: list[ExportablePlusAccount],
        *,
        exported_at: datetime,
        batch: bool = False,
    ) -> JSONValue: ...


class CpaExportStrategy:
    """Build the commonly accepted flat Codex/CPA auth JSON."""

    format_name = "cpa"

    @staticmethod
    def _account(
        account: ExportablePlusAccount, exported_at: datetime
    ) -> dict[str, JSONValue]:
        value: dict[str, JSONValue] = {
            "type": "codex",
            "account_id": account.account_id,
            "chatgpt_account_id": account.account_id,
            "email": account.email,
            "name": account.email,
            "plan_type": account.plan,
            "chatgpt_plan_type": account.plan,
            "id_token": account.id_token,
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            "last_refresh": _iso_z(account.last_refresh or exported_at),
            "expired": _iso_z(account.expires_at),
        }
        value["user_id"] = account.user_id
        value["chatgpt_user_id"] = account.user_id
        return value

    def build(
        self,
        accounts: list[ExportablePlusAccount],
        *,
        exported_at: datetime,
        batch: bool = False,
    ) -> JSONValue:
        values = [self._account(account, exported_at) for account in accounts]
        return values[0] if len(values) == 1 and not batch else values

    export = build


class Sub2ApiExportStrategy:
    """Build the Sub2API account bundle used by its import endpoint."""

    format_name = "sub2api"

    @staticmethod
    def _account(
        account: ExportablePlusAccount, exported_at: datetime
    ) -> dict[str, JSONValue]:
        credentials: dict[str, JSONValue] = {
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            "id_token": account.id_token,
            "account_id": account.account_id,
            "chatgpt_account_id": account.account_id,
            "email": account.email,
            "expires_at": _iso_z(account.expires_at),
            "expires_in": account.expires_in(exported_at),
            "plan_type": account.plan,
        }
        credentials["user_id"] = account.user_id
        credentials["chatgpt_user_id"] = account.user_id
        return {
            "name": account.email,
            "platform": "openai",
            "type": "oauth",
            "expires_at": account.expires_at_unix,
            "auto_pause_on_expired": True,
            "concurrency": 10,
            "priority": 1,
            "credentials": credentials,
            "extra": {
                "email": account.email,
                "email_key": _email_key(account.email),
                "name": account.email,
                "auth_provider": "openai",
                "source": "codex_oauth",
                "last_refresh": _iso_z(account.last_refresh or exported_at),
            },
        }

    def build(
        self,
        accounts: list[ExportablePlusAccount],
        *,
        exported_at: datetime,
        batch: bool = False,
    ) -> JSONValue:
        del batch  # Sub2API always wraps accounts in its batch document.
        return {
            "exported_at": _iso_z(exported_at),
            "proxies": [],
            "accounts": [self._account(account, exported_at) for account in accounts],
        }

    export = build


@dataclass(frozen=True, slots=True)
class ExportArtifact:
    """Transport-neutral result that HTTP, CLI, and desktop views can present."""

    format: str
    filename: str
    content: bytes
    count: int
    payload: JSONValue
    content_type: str = JSON_CONTENT_TYPE

    @property
    def body(self) -> bytes:
        return self.content

    @property
    def json_bytes(self) -> bytes:
        return self.content

    @property
    def media_type(self) -> str:
        return "application/json"

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "filename": self.filename,
            "content": self.content,
            "body": self.content,
            "json_bytes": self.content,
            "count": self.count,
            "payload": self.payload,
            "content_type": self.content_type,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[Any]:
        # Convenient compatibility with adapters expecting (filename, payload, count).
        yield self.filename
        yield self.payload
        yield self.count


class PlusAccountExportPresenter:
    """Select a strategy and return a complete downloadable JSON artifact."""

    def __init__(
        self,
        model: PlusAccountExportModel | str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        strategies: Mapping[str, PlusAccountExportStrategy] | None = None,
    ) -> None:
        resolved_clock = (
            clock
            or (model.clock if isinstance(model, PlusAccountExportModel) else None)
            or _utc_now
        )
        self.model = (
            model
            if isinstance(model, PlusAccountExportModel)
            else PlusAccountExportModel(model, clock=resolved_clock)
        )
        self.clock = resolved_clock
        configured = strategies or {
            "cpa": CpaExportStrategy(),
            "sub2api": Sub2ApiExportStrategy(),
        }
        self.strategies = {
            _text(name).casefold(): strategy for name, strategy in configured.items()
        }

    def export(self, format: str, email: str | None = None) -> ExportArtifact:
        format_name = _text(format).casefold()
        strategy = self.strategies.get(format_name)
        if strategy is None:
            supported = ", ".join(sorted(self.strategies))
            raise ValueError(f"不支持的导出格式：{format!r}；可选格式：{supported}")

        exported_at = _aware_utc(self.clock())
        accounts = self.model.eligible_accounts(email, now=exported_at)
        payload = strategy.build(
            accounts,
            exported_at=exported_at,
            batch=email is None,
        )
        content = (
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        )
        return ExportArtifact(
            format=format_name,
            filename=self._filename(format_name, email),
            content=content,
            count=len(accounts),
            payload=payload,
        )

    def export_cpa(self, email: str | None = None) -> ExportArtifact:
        return self.export("cpa", email)

    def export_sub2api(self, email: str | None = None) -> ExportArtifact:
        return self.export("sub2api", email)

    @staticmethod
    def _filename(format_name: str, email: str | None) -> str:
        if email is not None:
            token = _SAFE_FILENAME_CHARACTER.sub("-", email.casefold()).strip("-.")
            return f"plus-{format_name}-{token}.json"
        return f"plus-{format_name}-accounts.json"


# Short names are useful to web/CLI adapters while retaining domain-specific names.
AccountExportModel = PlusAccountExportModel
AccountExportPresenter = PlusAccountExportPresenter


__all__ = [
    "AccountExportModel",
    "AccountExportPresenter",
    "CpaExportStrategy",
    "ExportArtifact",
    "ExportablePlusAccount",
    "JSON_CONTENT_TYPE",
    "PlusAccountExportModel",
    "PlusAccountExportPresenter",
    "PlusAccountExportStrategy",
    "SUPPORTED_EXPORT_FORMATS",
    "Sub2ApiExportStrategy",
]
