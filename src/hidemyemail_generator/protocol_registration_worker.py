"""Subprocess entry point for one Mail Auth protocol registration."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
from types import ModuleType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


EVENT_PREFIX = "HME_PROTOCOL_EVENT:"
RESULT_PREFIX = "HME_PROTOCOL_RESULT:"
INVALID_STATE_FULL_RETRY_LIMIT = 1


def _configure_utf8_stdio(streams: tuple[Any, ...] | None = None) -> None:
    for stream in streams or (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def _emit_event(stage: str, message: str, status: str = "active") -> None:
    safe = re.sub(
        r"(?i)((?:access_?token|session_?token|otp)\s*[=:]\s*)[A-Za-z0-9._~+/-]{6,}",
        r"\1***",
        str(message or "").strip(),
    )
    print(
        EVENT_PREFIX
        + json.dumps(
            {"stage": stage, "message": safe[:500], "status": status},
            ensure_ascii=False,
        ),
        flush=True,
    )


def _is_invalid_state_failure(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if str(result.get("status") or "").strip().casefold() == "success":
        return False
    error = result.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip().casefold()
        if code == "invalid_state":
            return True
        detail = " ".join(str(value or "") for value in error.values())
    else:
        detail = str(error or "")
    normalized = detail.casefold()
    return (
        "invalid_state" in normalized
        or "sign-in session is no longer valid" in normalized
    )


def _load_core(root: Path) -> ModuleType:
    core = root / "core"
    required = (
        core / "chatgpt_register.py",
        core / "sentinel_token.py",
        core / "codex_oauth.py",
        core / "gpt_trial_protocol" / "__init__.py",
        core / "gpt_trial_protocol" / "models.py",
        core / "gpt_trial_protocol" / "sentinel_http.py",
        core / "sentinel_vm" / "runtime_worker.js",
    )
    if not all(path.is_file() for path in required):
        raise RuntimeError(f"gptfree-register 协议内核不完整：{core}")
    core_text = str(core)
    if core_text not in sys.path:
        sys.path.insert(0, core_text)
    module_path = core / "chatgpt_register.py"
    spec = importlib.util.spec_from_file_location("_hme_gptfree_register", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 Mail Auth 内核：{module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        provider = module._SentinelWithProxy()
        provider._impl._browser_profile("runtime-check")
    except (ImportError, AttributeError) as error:
        raise RuntimeError(f"Mail Auth 动态协议模块加载失败：{error}") from error
    return module


def _local_otp_fetcher(code_url: str):
    def fetcher(
        _email: str,
        _refresh_token: str,
        _client_id: str = "",
        *,
        timeout: int = 90,
        log_fn=None,
    ) -> str:
        deadline = time.time() + max(5, int(timeout or 30))
        if log_fn:
            log_fn("[OTP] 正在从账号工作台收取验证码")
        while time.time() < deadline:
            try:
                request = Request(
                    code_url,
                    headers={"Accept": "text/plain", "User-Agent": "HME-Protocol/1.0"},
                )
                with urlopen(request, timeout=15) as response:
                    code = response.read().decode("utf-8", errors="replace").strip()
                if re.fullmatch(r"[A-Za-z0-9]{4,10}", code):
                    if log_fn:
                        log_fn("[OTP] 已收到验证码")
                    return code
            except HTTPError as error:
                if error.code not in {404, 409, 429, 503}:
                    if log_fn:
                        log_fn(f"[OTP] 取码接口 HTTP {error.code}")
            except (URLError, TimeoutError, OSError) as error:
                if log_fn:
                    log_fn(f"[OTP] 取码接口暂未就绪：{type(error).__name__}")
            time.sleep(1.5)
        return ""

    return fetcher


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        segment = str(token or "").split(".")[1]
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _find_email(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("email", "account_email", "preferred_username", "upn"):
            candidate = str(value.get(key) or "").strip()
            if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", candidate):
                return candidate
        for nested in value.values():
            found = _find_email(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_email(nested)
            if found:
                return found
    return ""


def _storage_state(session_token: str, device_id: str) -> dict[str, Any]:
    cookies: list[dict[str, Any]] = []
    if session_token:
        cookies.append(
            {
                "name": "__Secure-next-auth.session-token",
                "value": session_token,
                "domain": "chatgpt.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    if device_id:
        cookies.append(
            {
                "name": "oai-did",
                "value": device_id,
                "domain": "chatgpt.com",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return {"cookies": cookies, "origins": []}


def run(payload: dict[str, Any]) -> dict[str, Any]:
    email = str(payload.get("email") or "").strip().lower()
    code_url = str(payload.get("code_url") or "").strip()
    proxy_url = str(payload.get("proxy_url") or "").strip()
    password = str(payload.get("existing_password") or "")
    password_confirmed = bool(payload.get("existing_password_confirmed"))
    totp_secret = str(payload.get("existing_totp_secret") or "")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise RuntimeError("协议注册邮箱无效")
    if not code_url.startswith(("http://", "https://")):
        raise RuntimeError("协议注册取码地址无效")

    project_root = Path(str(payload.get("project_root") or "")).resolve()
    source_root = Path(str(payload.get("source_root") or "")).resolve()
    source_text = str(source_root)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    from hidemyemail_generator.protocol_credentials import (
        complete_protocol_credentials,
    )

    module = _load_core(project_root)
    module._fetch_otp_sync = _local_otp_fetcher(code_url)
    register_class = getattr(module, "ChatGPTRegister", None)
    if not callable(register_class):
        raise RuntimeError("Mail Auth 内核缺少 ChatGPTRegister")

    _emit_event("protocol_auth", "启动 Mail Auth：CSRF → OTP → OAuth callback → Session")

    def log(message: Any) -> None:
        text = str(message or "").strip()
        lower = text.casefold()
        stage = "protocol_auth"
        if "otp" in lower or "验证码" in text:
            stage = "email_verification"
        elif "password" in lower or "密码" in text:
            stage = "password"
        elif "session" in lower or "token" in lower:
            stage = "session"
        _emit_event(stage, text)

    bot = None
    raw: Any = None
    for full_attempt in range(INVALID_STATE_FULL_RETRY_LIMIT + 1):
        # Reconstructing the register object is intentional: register() creates
        # a fresh OpenAIAuthClient, device id, cookie jar, OAuth state, and OTP
        # request for every full attempt.
        bot = register_class(
            {
                "email": email,
                "password": password,
                "client_id": "",
                "refresh_token": code_url,
            },
            log_fn=log,
            proxy=proxy_url,
            otp_timeout=120,
            impersonate="firefox144",
            # Always finish the password after account creation through
            # POST /api/accounts/password/add.  This keeps passwordless and
            # password-page registrations on the same verified completion path.
            with_password=False,
        )
        raw = bot.register()
        if not _is_invalid_state_failure(raw):
            break
        if full_attempt >= INVALID_STATE_FULL_RETRY_LIMIT:
            break
        _emit_event(
            "protocol_auth",
            "OpenAI 在创建账号步骤返回会话失效；正在创建新会话并重新获取验证码（1/1）",
            "warning",
        )
    if not isinstance(raw, dict):
        raise RuntimeError("Mail Auth 返回格式无效")
    if str(raw.get("status") or "").strip().lower() != "success":
        raise RuntimeError(str(raw.get("error") or "Mail Auth 注册失败"))
    access_token = str(raw.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("Mail Auth 成功响应缺少 Access Token")
    session_payload = raw.get("session_json")
    session = dict(session_payload) if isinstance(session_payload, dict) else {}
    session["accessToken"] = access_token
    session_token = str(raw.get("session_token") or session.get("sessionToken") or "").strip()
    if session_token:
        session["sessionToken"] = session_token
    actual_email = _find_email(session) or _find_email(_decode_jwt_payload(access_token))
    if actual_email and actual_email.casefold() != email.casefold():
        raise RuntimeError(f"Session 账号不匹配：当前={actual_email}，目标={email}")

    device_id = str(raw.get("device_id") or "").strip()
    generated_password = str(
        password
        if password and password_confirmed
        else raw.get("password") or getattr(bot, "password", "") or password
    ).strip()
    _emit_event("password", "正在后置设置协议账号密码")
    credentials = complete_protocol_credentials(
        email=email,
        access_token=access_token,
        generated_password=generated_password,
        password_set=bool(
            raw.get("password_set") or (password and password_confirmed)
        ),
        proxy_url=proxy_url,
        session_token=session_token,
        device_id=device_id,
        existing_totp_secret=totp_secret,
        log=lambda message: _emit_event(
            "two_factor" if "2FA" in message or "TOTP" in message else "password",
            message,
        ),
    )
    refreshed_access_token = str(credentials.get("access_token") or "").strip()
    if refreshed_access_token:
        access_token = refreshed_access_token
        session["accessToken"] = refreshed_access_token
    confirmed_password = str(credentials.get("password") or "").strip()
    two_factor = credentials.get("two_factor")
    if len(confirmed_password) < 12:
        raise RuntimeError("协议注册完成但密码未确认")
    if not (
        isinstance(two_factor, dict)
        and two_factor.get("enabled")
        and two_factor.get("secret")
    ):
        raise RuntimeError("协议注册完成但 TOTP 2FA 未激活")
    _emit_event("completed", "Session、密码和 TOTP 2FA 已全部完成", "success")
    return {
        "status": "success",
        "email": email,
        "access_token": access_token,
        "session_json": json.dumps(session, ensure_ascii=False),
        "storage_state_json": json.dumps(
            _storage_state(session_token, device_id), ensure_ascii=False
        ),
        "session_acquisition_method": "gptfree_mail_auth",
        "session_token": session_token,
        "device_id": device_id,
        "password": confirmed_password,
        "password_confirmed": True,
        "totp_secret": str(two_factor.get("secret") or ""),
        "two_factor": two_factor,
        "registration_diagnostics": {
            "full_attempts": full_attempt + 1,
            "invalid_state_recovered": bool(full_attempt > 0),
        },
    }


def main() -> int:
    _configure_utf8_stdio()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise RuntimeError("协议注册任务输入无效")
        result = run(payload)
        print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        _emit_event("failed", message, "error")
        print(
            RESULT_PREFIX
            + json.dumps({"status": "failed", "error": message}, ensure_ascii=False),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
