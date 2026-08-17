from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _AccessSession:
    expires_at: float
    issued_at: float
    email: str
    invite_id: str


class AccessSessionStore:
    """Issue opaque sessions while retaining only their SHA-256 digests."""

    def __init__(
        self,
        *,
        max_age_seconds: int,
        max_sessions: int = 4096,
        max_sessions_per_invite: int = 8,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_age_seconds = max(300, int(max_age_seconds))
        self._max_sessions = max(32, int(max_sessions))
        self._max_sessions_per_invite = min(
            self._max_sessions,
            max(1, int(max_sessions_per_invite)),
        )
        self._monotonic = monotonic
        self._sessions: dict[str, _AccessSession] = {}

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _cleanup(self, now: float) -> None:
        self._sessions = {
            digest: session
            for digest, session in self._sessions.items()
            if session.expires_at > now
        }

    def _evict_oldest(self, digests: list[str]) -> None:
        if not digests:
            return
        oldest = min(
            digests,
            key=lambda digest: (
                self._sessions[digest].issued_at,
                self._sessions[digest].expires_at,
            ),
        )
        self._sessions.pop(oldest, None)

    def issue(
        self,
        email: str,
        *,
        invite_id: str = "",
        max_age_seconds: int | None = None,
    ) -> str:
        now = self._monotonic()
        self._cleanup(now)
        scope = str(invite_id or self._digest(email))
        scoped_digests = [
            digest
            for digest, session in self._sessions.items()
            if session.invite_id == scope
        ]
        while len(scoped_digests) >= self._max_sessions_per_invite:
            self._evict_oldest(scoped_digests)
            scoped_digests = [
                digest
                for digest, session in self._sessions.items()
                if session.invite_id == scope
            ]
        if len(self._sessions) >= self._max_sessions:
            self._evict_oldest(list(self._sessions))
        lifetime = self._max_age_seconds
        if max_age_seconds is not None:
            lifetime = min(lifetime, max(1, int(max_age_seconds)))
        token = secrets.token_urlsafe(32)
        digest = self._digest(token)
        while digest in self._sessions:
            token = secrets.token_urlsafe(32)
            digest = self._digest(token)
        self._sessions[digest] = _AccessSession(
            expires_at=now + lifetime,
            issued_at=now,
            email=email,
            invite_id=scope,
        )
        return token

    def scope(self, token: str) -> str:
        supplied = str(token or "")
        if not supplied or len(supplied) > 256:
            return ""
        now = self._monotonic()
        digest = self._digest(supplied)
        session = self._sessions.get(digest)
        if session is None or session.expires_at <= now:
            self._sessions.pop(digest, None)
            return ""
        return session.email

    def revoke(self, token: str) -> None:
        supplied = str(token or "")
        if supplied:
            self._sessions.pop(self._digest(supplied), None)
