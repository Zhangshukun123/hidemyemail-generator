from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


LOGGER = logging.getLogger(__name__)
_STORE_VERSION = 1
_MAX_STORE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _AccessSession:
    expires_at: float
    issued_at: float
    email: str
    invite_id: str


class AccessSessionStore:
    """Issue opaque sessions while retaining only their SHA-256 digests.

    When ``storage_path`` is configured, live digests are written atomically so
    HttpOnly access cookies remain valid after a container or host restart.
    """

    def __init__(
        self,
        *,
        max_age_seconds: int,
        max_sessions: int = 4096,
        max_sessions_per_invite: int = 8,
        clock: Callable[[], float] = time.time,
        storage_path: str | Path | None = None,
    ) -> None:
        self._max_age_seconds = max(300, int(max_age_seconds))
        self._max_sessions = max(32, int(max_sessions))
        self._max_sessions_per_invite = min(
            self._max_sessions,
            max(1, int(max_sessions_per_invite)),
        )
        self._clock = clock
        self._storage_path = Path(storage_path) if storage_path else None
        self._sessions: dict[str, _AccessSession] = {}
        self._load()

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _cleanup(self, now: float) -> bool:
        live_sessions = {
            digest: session
            for digest, session in self._sessions.items()
            if session.expires_at > now
        }
        changed = len(live_sessions) != len(self._sessions)
        self._sessions = live_sessions
        return changed

    def _load(self) -> None:
        path = self._storage_path
        if path is None or not path.exists():
            return
        try:
            if path.stat().st_size > _MAX_STORE_BYTES:
                raise ValueError("session store is too large")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != _STORE_VERSION:
                raise ValueError("unsupported session store format")
            raw_sessions = payload.get("sessions")
            if not isinstance(raw_sessions, dict):
                raise ValueError("session store does not contain a session map")
            loaded: dict[str, _AccessSession] = {}
            for digest, raw_session in raw_sessions.items():
                if not isinstance(digest, str) or len(digest) != 64:
                    continue
                if not all(character in "0123456789abcdef" for character in digest):
                    continue
                if not isinstance(raw_session, dict):
                    continue
                email = raw_session.get("email")
                invite_id = raw_session.get("invite_id")
                try:
                    expires_at = float(raw_session.get("expires_at"))
                    issued_at = float(raw_session.get("issued_at"))
                except (TypeError, ValueError):
                    continue
                if (
                    not isinstance(email, str)
                    or not email
                    or len(email) > 320
                    or not isinstance(invite_id, str)
                    or len(invite_id) > 256
                    or expires_at <= issued_at
                ):
                    continue
                loaded[digest] = _AccessSession(
                    expires_at=expires_at,
                    issued_at=issued_at,
                    email=email,
                    invite_id=invite_id,
                )
            newest = sorted(
                loaded.items(),
                key=lambda item: (item[1].issued_at, item[1].expires_at),
                reverse=True,
            )[: self._max_sessions]
            self._sessions = dict(newest)
            if self._cleanup(self._clock()):
                self._persist()
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning("Ignoring unreadable access session store %s: %s", path, exc)
            self._sessions = {}

    def _persist(self) -> None:
        path = self._storage_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _STORE_VERSION,
            "sessions": {
                digest: {
                    "expires_at": session.expires_at,
                    "issued_at": session.issued_at,
                    "email": session.email,
                    "invite_id": session.invite_id,
                }
                for digest, session in self._sessions.items()
            },
        }
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                json.dump(payload, temporary, ensure_ascii=True, separators=(",", ":"))
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass

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
        now = self._clock()
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
        self._persist()
        return token

    def scope(self, token: str) -> str:
        supplied = str(token or "")
        if not supplied or len(supplied) > 256:
            return ""
        now = self._clock()
        digest = self._digest(supplied)
        session = self._sessions.get(digest)
        if session is None or session.expires_at <= now:
            removed = self._sessions.pop(digest, None)
            if removed is not None:
                self._persist()
            return ""
        return session.email

    def revoke(self, token: str) -> None:
        supplied = str(token or "")
        if supplied:
            removed = self._sessions.pop(self._digest(supplied), None)
            if removed is not None:
                self._persist()
