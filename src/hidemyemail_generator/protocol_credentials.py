"""Finish password and TOTP setup for a Mail Auth protocol registration."""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from .openai_mfa import generate_totp, normalize_totp_secret


PASSWORD_ADD_URL = "https://chatgpt.com/api/accounts/password/add"
MFA_BASE_URL = "https://chatgpt.com/backend-api/accounts/mfa"
SESSION_URL = "https://chatgpt.com/api/auth/session"


class ProtocolCredentialSetupError(RuntimeError):
    """The account exists, but its password or TOTP setup did not finish."""


def _status(response: Any) -> int:
    try:
        return int(getattr(response, "status", None) or response.status_code)
    except (AttributeError, TypeError, ValueError):
        return 0


def _payload(response: Any) -> dict[str, Any]:
    try:
        value = response.json()
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _detail(response: Any) -> str:
    payload = _payload(response)
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("errorMessage")
        if message:
            return str(message)[:300]
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


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/141.0.0.0 Safari/537.36"
        ),
    }


def _new_session(*, proxy_url: str, session_token: str, device_id: str) -> Any:
    try:
        from curl_cffi.requests import Session

        session = Session(impersonate="chrome136")
    except ImportError:
        import requests

        session = requests.Session()
    proxy = str(proxy_url or "").strip()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    if session_token:
        session.cookies.set(
            "__Secure-next-auth.session-token",
            session_token,
            domain="chatgpt.com",
            path="/",
        )
    if device_id:
        session.cookies.set("oai-did", device_id, domain="chatgpt.com", path="/")
    return session


def _post_json(
    session: Any,
    url: str,
    access_token: str,
    payload: dict[str, Any],
) -> Any:
    return session.post(
        url,
        headers=_headers(access_token),
        data=json.dumps(payload),
        timeout=60,
    )


def _require_success(response: Any, action: str) -> dict[str, Any]:
    status = _status(response)
    if not 200 <= status < 300:
        detail = _detail(response)
        raise ProtocolCredentialSetupError(
            f"{action}失败：HTTP {status}" + (f" · {detail}" if detail else "")
        )
    return _payload(response)


def _refresh_access_token(session: Any) -> str:
    try:
        response = session.get(
            f"{SESSION_URL}?auth_check={int(time.time() * 1000)}",
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache, no-store",
                "Pragma": "no-cache",
                "Referer": "https://chatgpt.com/",
            },
            timeout=60,
        )
    except Exception:
        return ""
    if not 200 <= _status(response) < 300:
        return ""
    payload = _payload(response)
    return str(payload.get("accessToken") or payload.get("access_token") or "").strip()


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


def complete_protocol_credentials(
    *,
    email: str,
    access_token: str,
    generated_password: str,
    password_set: bool,
    proxy_url: str = "",
    session_token: str = "",
    device_id: str = "",
    existing_totp_secret: str = "",
    log: Callable[[str], None] | None = None,
    request_session: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Require a confirmed password and activated TOTP before success."""

    token = str(access_token or "").strip()
    if not token:
        raise ProtocolCredentialSetupError("协议注册没有返回 Access Token")
    password = str(generated_password or "")
    if len(password) < 12:
        raise ProtocolCredentialSetupError("协议注册密码长度不足 12 位")
    logger = log or (lambda _message: None)

    saved_totp = str(existing_totp_secret or "").strip()
    if saved_totp:
        secret = normalize_totp_secret(saved_totp)
        logger("已保留现有密码与 TOTP 2FA")
        return {
            "password": password,
            "password_set": True,
            "access_token": token,
            "totp_secret": secret,
            "two_factor": {
                "enabled": True,
                "status": "enabled",
                "type": "totp",
                "secret": secret,
                "recovery_codes": [],
            },
        }

    own_session = request_session is None
    session = request_session or _new_session(
        proxy_url=proxy_url,
        session_token=session_token,
        device_id=device_id,
    )
    try:
        if not password_set:
            _require_success(
                _post_json(session, PASSWORD_ADD_URL, token, {"password": password}),
                "添加密码",
            )
            logger("密码已通过 accounts/password/add 添加")
            if session_token:
                refreshed = _refresh_access_token(session)
                if refreshed:
                    token = refreshed
                    logger("添加密码后已刷新 Access Token")
        else:
            logger("Mail Auth 已确认密码")

        enrolled = _require_success(
            _post_json(
                session,
                f"{MFA_BASE_URL}/enroll",
                token,
                {"factor_type": "totp"},
            ),
            "创建 2FA 验证器",
        )
        secret = normalize_totp_secret(str(enrolled.get("secret") or ""))
        session_id = str(enrolled.get("session_id") or "").strip()
        factor = enrolled.get("factor")
        factor_id = (
            str(factor.get("id") or "").strip()
            if isinstance(factor, dict)
            else str(enrolled.get("factor_id") or "").strip()
        )
        if not factor_id or not session_id:
            raise ProtocolCredentialSetupError("OpenAI 2FA 响应缺少必要字段")
        logger("TOTP 验证器已创建，正在激活")

        remaining = 30 - (now() % 30)
        if remaining < 4:
            sleep(remaining + 0.25)
        activated = _require_success(
            _post_json(
                session,
                f"{MFA_BASE_URL}/user/activate_enrollment",
                token,
                {
                    "factor_id": factor_id,
                    "factor_type": "totp",
                    "session_id": session_id,
                    "code": generate_totp(secret, now=now()),
                },
            ),
            "激活 2FA",
        )
        logger("TOTP 2FA 已激活")
        return {
            "password": password,
            "password_set": True,
            "access_token": token,
            "totp_secret": secret,
            "two_factor": {
                "enabled": True,
                "status": "enabled",
                "type": "totp",
                "secret": secret,
                "factor_id": factor_id,
                "session_id": session_id,
                "recovery_codes": _recovery_codes(activated),
            },
        }
    finally:
        if own_session:
            close = getattr(session, "close", None)
            if callable(close):
                close()


__all__ = [
    "MFA_BASE_URL",
    "PASSWORD_ADD_URL",
    "ProtocolCredentialSetupError",
    "SESSION_URL",
    "complete_protocol_credentials",
]
