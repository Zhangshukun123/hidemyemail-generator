from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct
import time
from typing import Any, Callable
from urllib.parse import quote


MFA_BASE_URL = "https://chatgpt.com/backend-api/accounts/mfa"


class MfaSetupError(RuntimeError):
    pass


def normalize_totp_secret(value: str) -> str:
    secret = re.sub(r"[^A-Z2-7]", "", str(value or "").upper())
    if len(secret) < 16:
        raise MfaSetupError("OpenAI 返回的 2FA 密钥无效")
    return secret


def generate_totp(secret: str, *, now: float | None = None) -> str:
    normalized = normalize_totp_secret(secret)
    padding = "=" * (-len(normalized) % 8)
    try:
        key = base64.b32decode(normalized + padding, casefold=True)
    except (ValueError, TypeError) as error:
        raise MfaSetupError("2FA 密钥不是有效的 Base32") from error
    counter = int((time.time() if now is None else now) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def provisioning_uri(secret: str, email: str) -> str:
    normalized = normalize_totp_secret(secret)
    label = quote(f"OpenAI:{email.strip().lower()}", safe="")
    return (
        f"otpauth://totp/{label}?secret={normalized}"
        "&issuer=OpenAI&algorithm=SHA1&digits=6&period=30"
    )


def _response_payload(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _response_detail(response: Any) -> str:
    payload = _response_payload(response)
    if payload:
        error = payload.get("error")
        if isinstance(error, dict):
            detail = error.get("message") or error.get("errorMessage")
            if detail:
                return str(detail)[:300]
        for key in ("detail", "message"):
            if payload.get(key):
                return str(payload[key])[:300]
    try:
        text = response.text
        if callable(text):
            text = text()
        return str(text or "")[:300]
    except Exception:
        return ""


def _status(response: Any) -> int:
    try:
        return int(getattr(response, "status", None) or response.status_code)
    except (TypeError, ValueError, AttributeError):
        return 0


def _post_json(request: Any, url: str, token: str, payload: dict[str, Any]) -> Any:
    return request.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/141.0.0.0 Safari/537.36"
            ),
        },
        data=json.dumps(payload),
        timeout=60_000,
    )


def _recovery_codes(payload: dict[str, Any]) -> list[str]:
    candidates = [
        payload.get("recovery_codes"),
        payload.get("recoveryCodes"),
        (payload.get("factor") or {}).get("recovery_codes")
        if isinstance(payload.get("factor"), dict)
        else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [str(value).strip() for value in candidate if str(value).strip()]
    return []


def enable_totp_mfa(
    context: Any,
    *,
    access_token: str,
    email: str,
    pending: dict[str, Any] | None = None,
    on_enrolled: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    token = str(access_token or "").strip()
    if not token:
        raise MfaSetupError("注册成功但没有获得 AT，无法开启 2FA")
    request = getattr(context, "request", None)
    if request is None and callable(getattr(context, "post", None)):
        request = context
    if request is None:
        raise MfaSetupError("当前请求客户端不支持 2FA")

    pending = pending if isinstance(pending, dict) else {}
    secret = str(pending.get("secret") or "")
    factor_id = str(pending.get("factor_id") or "")
    session_id = str(pending.get("session_id") or "")

    if not (secret and factor_id and session_id):
        response = _post_json(
            request,
            f"{MFA_BASE_URL}/enroll",
            token,
            {"factor_type": "totp"},
        )
        if not 200 <= _status(response) < 300:
            detail = _response_detail(response)
            raise MfaSetupError(
                f"创建 2FA 验证器失败：HTTP {_status(response)}"
                + (f" · {detail}" if detail else "")
            )
        enrolled = _response_payload(response)
        secret = normalize_totp_secret(str(enrolled.get("secret") or ""))
        session_id = str(enrolled.get("session_id") or "").strip()
        factor = enrolled.get("factor")
        factor_id = (
            str(factor.get("id") or "").strip() if isinstance(factor, dict) else ""
        )
        if not factor_id or not session_id:
            raise MfaSetupError("OpenAI 的 2FA 注册响应缺少必要字段")

    state: dict[str, Any] = {
        "enabled": False,
        "status": "enrolled",
        "type": "totp",
        "secret": normalize_totp_secret(secret),
        "provisioning_uri": provisioning_uri(secret, email),
        "factor_id": factor_id,
        "session_id": session_id,
    }
    if on_enrolled:
        on_enrolled(dict(state))

    remaining = 30 - (time.time() % 30)
    if remaining < 4:
        time.sleep(remaining + 0.25)
    code = generate_totp(state["secret"])
    response = _post_json(
        request,
        f"{MFA_BASE_URL}/user/activate_enrollment",
        token,
        {
            "factor_id": factor_id,
            "factor_type": "totp",
            "session_id": session_id,
            "code": code,
        },
    )
    status = _status(response)
    detail = _response_detail(response)
    already_enabled = status in {400, 409} and any(
        marker in detail.casefold() for marker in ("already", "active", "enabled")
    )
    if not (200 <= status < 300 or already_enabled):
        raise MfaSetupError(
            f"激活 2FA 失败：HTTP {status}" + (f" · {detail}" if detail else "")
        )
    activated = _response_payload(response)
    state.update(
        enabled=True,
        status="enabled",
        recovery_codes=_recovery_codes(activated),
    )
    return state


__all__ = [
    "MfaSetupError",
    "enable_totp_mfa",
    "generate_totp",
    "normalize_totp_secret",
    "provisioning_uri",
]
