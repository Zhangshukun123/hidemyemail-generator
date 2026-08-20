from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InboxRetryDecision:
    kind: str
    delay_seconds: int


class InboxRetryPolicy:
    """Strategy for retrying IMAP without hiding recovered mail for minutes."""

    def __init__(
        self,
        *,
        transient_base_seconds: int = 5,
        transient_max_seconds: int = 30,
        authentication_base_seconds: int = 60,
        authentication_max_seconds: int = 15 * 60,
    ) -> None:
        self._transient_base_seconds = max(1, int(transient_base_seconds))
        self._transient_max_seconds = max(
            self._transient_base_seconds, int(transient_max_seconds)
        )
        self._authentication_base_seconds = max(
            1, int(authentication_base_seconds)
        )
        self._authentication_max_seconds = max(
            self._authentication_base_seconds, int(authentication_max_seconds)
        )

    def decide(self, public_message: str, failures: int) -> InboxRetryDecision:
        attempts = max(1, int(failures))
        authentication = "IMAP 登录失败" in str(public_message or "")
        base = (
            self._authentication_base_seconds
            if authentication
            else self._transient_base_seconds
        )
        maximum = (
            self._authentication_max_seconds
            if authentication
            else self._transient_max_seconds
        )
        delay = min(maximum, base * (2 ** min(attempts - 1, 6)))
        return InboxRetryDecision(
            kind="authentication" if authentication else "transient",
            delay_seconds=delay,
        )


__all__ = ["InboxRetryDecision", "InboxRetryPolicy"]
