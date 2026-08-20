"""Finish password and TOTP setup for a Mail Auth protocol registration."""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from .openai_mfa import generate_totp, normalize_totp_secret


PASSWORD_ADD_URL = "https://chatgpt.com/api/accounts/password/add"
MFA_BASE_URL = "https://chatgpt.com/backend-api/accounts/mfa"
SESSION_URL = "https://chatgpt.com/api/auth/session"


def _language_header(language: str) -> str:
    primary = str(language or "en-US").strip() or "en-US"
    root = primary.split("-", 1)[0]
    if primary.casefold() == "en-us":
        return "en-US,en;q=0.9"
    return f"{primary},{root};q=0.9,en-US;q=0.8,en;q=0.7"


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


def _headers(
    access_token: str,
    *,
    device_id: str = "",
    target_url: str = "",
    language: str = "en-US",
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "Accept-Language": _language_header(language),
        "oai-language": str(language or "en-US"),
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "priority": "u=1, i",
    }
    if device_id:
        headers["oai-device-id"] = device_id
    if target_url.startswith("https://chatgpt.com/backend-api/"):
        path = "/" + target_url.split("/", 3)[-1].split("?", 1)[0]
        headers["x-openai-target-path"] = path
        headers["x-openai-target-route"] = path
    return headers


def _iter_session_cookies(value: Any) -> list[dict[str, str]]:
    if isinstance(value, dict):
        return [
            {"name": str(name), "value": str(cookie_value)}
            for name, cookie_value in value.items()
            if str(name) and cookie_value is not None
        ]
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    return []


def _new_session(
    *,
    proxy_url: str,
    session_token: str,
    device_id: str,
    session_cookies: Any = None,
    impersonate: str = "firefox144",
    language: str = "en-US",
) -> Any:
    from .protocol_browser import ProtocolBrowserPersona

    persona = ProtocolBrowserPersona.from_impersonate(impersonate)
    try:
        from curl_cffi.requests import Session

        session = Session(impersonate=persona.impersonate)
    except ImportError:
        import requests

        session = requests.Session()
    if not hasattr(session, "headers"):
        session.headers = {}
    session.headers.update(persona.session_headers(language))
    session.headers["oai-language"] = str(language or "en-US")
    proxy = str(proxy_url or "").strip()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    for cookie in _iter_session_cookies(session_cookies):
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if not name:
            continue
        options: dict[str, str] = {}
        domain = str(cookie.get("domain") or "").strip()
        path = str(cookie.get("path") or "").strip()
        if domain:
            options["domain"] = domain
        if path:
            options["path"] = path
        session.cookies.set(name, value, **options)
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
    *,
    device_id: str = "",
    language: str = "en-US",
) -> Any:
    return session.post(
        url,
        headers=_headers(
            access_token,
            device_id=device_id,
            target_url=url,
            language=language,
        ),
        data=json.dumps(payload),
        timeout=60,
        allow_redirects=False,
    )


def _require_success(response: Any, action: str) -> dict[str, Any]:
    status = _status(response)
    if not 200 <= status < 300:
        detail = _detail(response)
        raise ProtocolCredentialSetupError(
            f"{action}失败：HTTP {status}" + (f" · {detail}" if detail else "")
        )
    return _payload(response)


def _refresh_access_token(session: Any, *, language: str = "en-US") -> str:
    try:
        response = session.get(
            f"{SESSION_URL}?auth_check={int(time.time() * 1000)}",
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache, no-store",
                "Pragma": "no-cache",
                "Referer": "https://chatgpt.com/",
                "Accept-Language": _language_header(language),
                "oai-language": str(language or "en-US"),
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


def _totp_activation_retryable(response: Any) -> bool:
    status = _status(response)
    if status not in {400, 409, 422}:
        return False
    detail = _detail(response).casefold()
    return any(
        marker in detail
        for marker in (
            "invalid code",
            "invalid totp",
            "expired code",
            "incorrect code",
            "verification code",
        )
    )


def complete_protocol_credentials(
    *,
    email: str,
    access_token: str,
    generated_password: str,
    password_set: bool,
    proxy_url: str = "",
    session_token: str = "",
    device_id: str = "",
    session_cookies: Any = None,
    impersonate: str = "firefox144",
    language: str = "en-US",
    existing_totp_secret: str = "",
    log: Callable[[str], None] | None = None,
    on_password_confirmed: Callable[[], None] | None = None,
    password_verifier: Callable[[], dict[str, Any]] | None = None,
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
    saved_secret = normalize_totp_secret(saved_totp) if saved_totp else ""
    if saved_secret and password_set and password_verifier is None:
        logger("已保留现有密码与 TOTP 2FA")
        return {
            "password": password,
            "password_set": True,
            "access_token": token,
            "totp_secret": saved_secret,
            "two_factor": {
                "enabled": True,
                "status": "enabled",
                "type": "totp",
                "secret": saved_secret,
                "recovery_codes": [],
                "server_verified": False,
                "verification_source": "saved_record",
            },
        }

    own_session = request_session is None
    session = request_session or _new_session(
        proxy_url=proxy_url,
        session_token=session_token,
        device_id=device_id,
        session_cookies=session_cookies,
        impersonate=impersonate,
        language=language,
    )
    try:
        if not password_set:
            logger("账号尚无密码；将使用当前注册 Session 直接添加密码")
        else:
            logger("注册阶段已设置密码；直接继续配置 2FA")
        verification_performed = password_verifier is not None
        verified_access_token = ""
        reuse_registration_session = False
        if password_verifier is not None:
            verification = password_verifier()
            if not isinstance(verification, dict) or not verification.get("verified"):
                detail = (
                    str(verification.get("error") or "")
                    if isinstance(verification, dict)
                    else ""
                )
                raise ProtocolCredentialSetupError(
                    "当前认证会话添加密码失败"
                    + (f" · {detail[:300]}" if detail else "")
                )
            reuse_registration_session = bool(
                verification.get("reuse_registration_session")
            )
            verified_access_token = str(
                verification.get("access_token") or ""
            ).strip()
            if verified_access_token:
                token = verified_access_token
            logger("当前认证会话已确认密码添加成功")
        elif not password_set:
            raise ProtocolCredentialSetupError(
                "账号尚无密码且未执行添加密码流程，停止配置 2FA"
            )
        if on_password_confirmed is not None:
            on_password_confirmed()

        if saved_secret:
            logger("已保留现有 TOTP 2FA")
            return {
                "password": password,
                "password_set": True,
                "access_token": token,
                "totp_secret": saved_secret,
                "two_factor": {
                    "enabled": True,
                    "status": "enabled",
                    "type": "totp",
                    "secret": saved_secret,
                    "recovery_codes": [],
                    "server_verified": False,
                    "verification_source": "saved_record",
                },
            }

        # Password setup reuses the just-authenticated registration Session.
        # Refresh its token in-place and continue to MFA without another login
        # or another email OTP challenge.
        if (not verification_performed or reuse_registration_session) and session_token:
            refreshed = _refresh_access_token(session, language=language)
            if refreshed:
                token = refreshed
                logger("添加密码后已刷新当前注册 Session 的 Access Token")
            elif reuse_registration_session:
                logger("添加密码后继续使用当前注册 Session 的 Access Token")
        elif (
            verification_performed
            and not reuse_registration_session
            and not verified_access_token
        ):
            raise ProtocolCredentialSetupError(
                "添加密码流程未返回可用于创建 2FA 的 Access Token"
            )

        enrolled = _require_success(
            _post_json(
                session,
                f"{MFA_BASE_URL}/enroll",
                token,
                {"factor_type": "totp"},
                device_id=device_id,
                language=language,
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
        activation_response = _post_json(
            session,
            f"{MFA_BASE_URL}/user/activate_enrollment",
            token,
            {
                "factor_id": factor_id,
                "factor_type": "totp",
                "session_id": session_id,
                "code": generate_totp(secret, now=now()),
            },
            device_id=device_id,
            language=language,
        )
        if _totp_activation_retryable(activation_response):
            wait_seconds = 30 - (now() % 30) + 0.25
            logger("TOTP 验证码跨时间窗口失效；等待下一组验证码重试（1/1）")
            sleep(wait_seconds)
            activation_response = _post_json(
                session,
                f"{MFA_BASE_URL}/user/activate_enrollment",
                token,
                {
                    "factor_id": factor_id,
                    "factor_type": "totp",
                    "session_id": session_id,
                    "code": generate_totp(secret, now=now()),
                },
                device_id=device_id,
                language=language,
            )
        activated = _require_success(activation_response, "激活 2FA")
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
                "server_verified": True,
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
