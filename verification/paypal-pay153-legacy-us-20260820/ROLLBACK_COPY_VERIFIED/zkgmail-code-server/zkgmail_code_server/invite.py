from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from .domain import normalize_zkgmail_address


TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,1024}\.[0-9a-f]{64}$")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True, slots=True)
class InviteScope:
    email: str
    expires_at: int
    remaining_seconds: int
    invite_id: str


class InviteTokenService:
    """Issue and verify exact-email invitations signed with a 256-bit secret."""

    def __init__(
        self,
        secret_hex: str,
        *,
        clock: Callable[[], float] = time.time,
        maximum_ttl_seconds: int = 30 * 24 * 60 * 60,
    ) -> None:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(secret_hex or "")):
            raise ValueError("ZKGMAIL_ACCESS_TOKEN must be a 64-character hex secret")
        self._secret = bytes.fromhex(secret_hex)
        self._clock = clock
        self._maximum_ttl_seconds = max(300, int(maximum_ttl_seconds))

    def issue(self, email: str, *, ttl_seconds: int) -> str:
        target = normalize_zkgmail_address(email)
        ttl = min(self._maximum_ttl_seconds, max(300, int(ttl_seconds)))
        payload = {
            "v": 1,
            "e": target,
            "x": int(self._clock()) + ttl,
            "n": secrets.token_hex(32),
        }
        encoded = _encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{encoded}.{signature}"

    def verify(self, token: str) -> InviteScope | None:
        supplied = str(token or "")
        if not TOKEN_RE.fullmatch(supplied):
            return None
        encoded, signature = supplied.rsplit(".", 1)
        expected = hmac.new(
            self._secret,
            encoded.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            payload = json.loads(_decode(encoded))
            email = normalize_zkgmail_address(payload["e"])
            expires_at = int(payload["x"])
            version = int(payload["v"])
            nonce = str(payload["n"])
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            binascii.Error,
            json.JSONDecodeError,
        ):
            return None
        now = int(self._clock())
        if (
            version != 1
            or not re.fullmatch(r"[0-9a-f]{64}", nonce)
            or expires_at <= now
            or expires_at > now + self._maximum_ttl_seconds
        ):
            return None
        return InviteScope(
            email=email,
            expires_at=expires_at,
            remaining_seconds=expires_at - now,
            invite_id=hashlib.sha256(supplied.encode("utf-8")).hexdigest(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an exact-email ZKGMail invite link")
    parser.add_argument("email")
    parser.add_argument("--hours", type=int, default=168)
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "ZKGMAIL_PUBLIC_URL",
            "https://zkgmail.8-208-13-52.sslip.io/",
        ),
    )
    args = parser.parse_args()
    service = InviteTokenService(os.environ.get("ZKGMAIL_ACCESS_TOKEN", ""))
    token = service.issue(args.email, ttl_seconds=max(1, args.hours) * 60 * 60)
    print(f"{args.base_url.rstrip('/')}/#invite={token}")


if __name__ == "__main__":
    main()
