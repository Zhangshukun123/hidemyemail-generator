from __future__ import annotations

from typing import Protocol

from .domain import CodeMessage


class CodeExtractor(Protocol):
    """Strategy port for recognizing a verification code in a message."""

    def extract(self, subject: str, body: str) -> str: ...


class CodeRepository(Protocol):
    """Repository port for finding the latest code for one exact recipient."""

    async def latest_for(self, recipient: str) -> CodeMessage | None: ...
