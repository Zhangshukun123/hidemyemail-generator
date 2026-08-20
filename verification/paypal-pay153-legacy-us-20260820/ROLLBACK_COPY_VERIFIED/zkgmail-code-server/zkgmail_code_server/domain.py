from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


ZKGMAIL_DOMAIN = "zkgmail.com"
_LOCAL_PART_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._+\-]{0,62}[a-z0-9])?$", re.IGNORECASE)


class InvalidAddressError(ValueError):
    """The requested address is not an exact zkgmail.com address."""


class MailboxNotConfiguredError(RuntimeError):
    """The receiving mailbox credentials are absent."""


class MailboxUnavailableError(RuntimeError):
    """The receiving mailbox could not be queried."""


class LookupState(StrEnum):
    FOUND = "found"
    WAITING = "waiting"
    INVALID = "invalid"
    UNCONFIGURED = "unconfigured"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class CodeMessage:
    code: str
    received_at: str
    sender: str = ""
    cursor: str = ""


@dataclass(frozen=True, slots=True)
class LookupViewModel:
    state: LookupState
    status: int
    email: str = ""
    message: str = ""
    code: str = ""
    received_at: str = ""
    cursor: str = ""
    checked_at: str = ""

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.state is LookupState.FOUND,
            "state": self.state.value,
            "message": self.message,
            "checkedAt": self.checked_at,
        }
        if self.email:
            payload["email"] = self.email
        if self.code:
            payload["code"] = self.code
        if self.received_at:
            payload["receivedAt"] = self.received_at
        if self.cursor:
            payload["cursor"] = self.cursor
        if self.state is not LookupState.FOUND:
            payload["error"] = self.message
        return payload


def normalize_zkgmail_address(value: str) -> str:
    address = str(value or "").strip().lower()
    if len(address) > 320 or address.count("@") != 1:
        raise InvalidAddressError("请输入有效的 zkgmail.com 邮箱地址")
    local_part, domain = address.rsplit("@", 1)
    if domain != ZKGMAIL_DOMAIN or not _LOCAL_PART_RE.fullmatch(local_part):
        raise InvalidAddressError("请输入有效的 zkgmail.com 邮箱地址")
    return address
