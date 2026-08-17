from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from .domain import (
    InvalidAddressError,
    LookupState,
    LookupViewModel,
    MailboxNotConfiguredError,
    MailboxUnavailableError,
    normalize_zkgmail_address,
)
from .ports import CodeRepository


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LookupPresenter:
    """MVP presenter: validate input, query the model port, build a view model."""

    def __init__(
        self,
        repository: CodeRepository,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repository = repository
        self._clock = clock

    def _checked_at(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    async def lookup(self, raw_email: str, *, after_cursor: str = "") -> LookupViewModel:
        checked_at = self._checked_at()
        cursor = str(after_cursor or "").strip()
        if len(cursor) > 128:
            return LookupViewModel(
                state=LookupState.INVALID,
                status=400,
                message="验证码游标无效",
                checked_at=checked_at,
            )
        try:
            email = normalize_zkgmail_address(raw_email)
        except InvalidAddressError as error:
            return LookupViewModel(
                state=LookupState.INVALID,
                status=400,
                message=str(error),
                checked_at=checked_at,
            )

        try:
            item = await self._repository.latest_for(email)
        except MailboxNotConfiguredError:
            return LookupViewModel(
                state=LookupState.UNCONFIGURED,
                status=503,
                email=email,
                message="接码服务尚未配置",
                checked_at=checked_at,
            )
        except MailboxUnavailableError:
            return LookupViewModel(
                state=LookupState.UNAVAILABLE,
                status=502,
                email=email,
                message="邮箱服务暂时不可用，请稍后重试",
                checked_at=checked_at,
            )

        if item is None or (cursor and item.cursor == cursor):
            return LookupViewModel(
                state=LookupState.WAITING,
                status=404,
                email=email,
                message=(
                    "暂未收到新的验证码，请稍后再试"
                    if cursor
                    else "暂未收到该邮箱的验证码，请稍后再试"
                ),
                checked_at=checked_at,
            )
        return LookupViewModel(
            state=LookupState.FOUND,
            status=200,
            email=email,
            code=item.code,
            received_at=item.received_at,
            cursor=item.cursor,
            message="已获取最新验证码",
            checked_at=checked_at,
        )
