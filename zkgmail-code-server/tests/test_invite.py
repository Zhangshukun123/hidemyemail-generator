from __future__ import annotations

import base64
import json
import re

from zkgmail_code_server.invite import InviteTokenService


SECRET = "22" * 32
OTHER_SECRET = "33" * 32


def _decode_payload(encoded: str) -> dict:
    padding = "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded + padding))


def _encode_payload(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


def test_invite_signature_binds_exact_email_and_secret():
    service = InviteTokenService(SECRET, clock=lambda: 1_000)
    token = service.issue(" Alias@ZKGMAIL.COM ", ttl_seconds=600)

    scope = service.verify(token)

    assert scope is not None
    assert scope.email == "alias@zkgmail.com"
    assert scope.expires_at == 1_600
    assert scope.remaining_seconds == 600
    assert re.fullmatch(r"[0-9a-f]{64}", scope.invite_id)
    encoded, _ = token.rsplit(".", 1)
    assert re.fullmatch(r"[0-9a-f]{64}", _decode_payload(encoded)["n"])
    assert InviteTokenService(OTHER_SECRET, clock=lambda: 1_000).verify(token) is None


def test_expired_invite_is_rejected_at_expiry_boundary():
    now = [2_000.0]
    service = InviteTokenService(SECRET, clock=lambda: now[0])
    token = service.issue("alias@zkgmail.com", ttl_seconds=300)

    now[0] = 2_299
    assert service.verify(token) is not None

    now[0] = 2_300
    assert service.verify(token) is None


def test_tampered_invite_payload_is_rejected_without_resigning():
    service = InviteTokenService(SECRET, clock=lambda: 3_000)
    token = service.issue("a@zkgmail.com", ttl_seconds=600)
    encoded, signature = token.rsplit(".", 1)
    payload = _decode_payload(encoded)
    payload["e"] = "b@zkgmail.com"
    tampered = f"{_encode_payload(payload)}.{signature}"

    assert service.verify(tampered) is None


def test_tampered_invite_signature_is_rejected():
    service = InviteTokenService(SECRET, clock=lambda: 4_000)
    token = service.issue("alias@zkgmail.com", ttl_seconds=600)
    encoded, signature = token.rsplit(".", 1)
    replacement = "0" if signature[-1] != "0" else "1"

    assert service.verify(f"{encoded}.{signature[:-1]}{replacement}") is None


def test_malformed_base64_payload_is_rejected_without_an_exception():
    service = InviteTokenService(SECRET, clock=lambda: 5_000)
    malformed = f"{'_' * 41}.{'0' * 64}"

    assert service.verify(malformed) is None
