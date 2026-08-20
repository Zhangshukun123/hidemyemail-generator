"""Subprocess entry point for one Mail Auth protocol registration."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import re
import secrets
import sys
import time
from types import ModuleType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    from hidemyemail_generator.protocol_browser import (
        CHROME_IMPERSONATE_PROFILES,
        FIREFOX_IMPERSONATE_PROFILES,
        PROTOCOL_IMPERSONATE_PROFILES,
        normalize_protocol_impersonate,
    )
except ModuleNotFoundError:  # Direct script launch before src is on sys.path.
    from protocol_browser import (  # type: ignore[no-redef]
        CHROME_IMPERSONATE_PROFILES,
        FIREFOX_IMPERSONATE_PROFILES,
        PROTOCOL_IMPERSONATE_PROFILES,
        normalize_protocol_impersonate,
    )


EVENT_PREFIX = "HME_PROTOCOL_EVENT:"
RESULT_PREFIX = "HME_PROTOCOL_RESULT:"
INVALID_STATE_FULL_RETRY_LIMIT = 1
INVALID_AUTH_STEP_RECOVERY_LIMIT = 1
PASSWORD_RESET_LOGIN_RETRY_LIMIT = 1
TRANSIENT_INIT_FULL_RETRY_LIMIT = 2

PROXY_FINGERPRINT_PROFILES = {
    "NL": ("nl-NL", "Europe/Amsterdam"),
    "US": ("en-US", "America/New_York"),
    "JP": ("ja-JP", "Asia/Tokyo"),
    "DE": ("de-DE", "Europe/Berlin"),
    "GB": ("en-GB", "Europe/London"),
    "FR": ("fr-FR", "Europe/Paris"),
    "CA": ("en-CA", "America/Toronto"),
    "AU": ("en-AU", "Australia/Sydney"),
    "SG": ("en-SG", "Asia/Singapore"),
    "HK": ("zh-HK", "Asia/Hong_Kong"),
    "TW": ("zh-TW", "Asia/Taipei"),
    "KR": ("ko-KR", "Asia/Seoul"),
    "BR": ("pt-BR", "America/Sao_Paulo"),
    "IN": ("en-IN", "Asia/Kolkata"),
    "TR": ("tr-TR", "Europe/Istanbul"),
    "ES": ("es-ES", "Europe/Madrid"),
    "ID": ("id-ID", "Asia/Jakarta"),
    "IT": ("it-IT", "Europe/Rome"),
    "MX": ("es-MX", "America/Mexico_City"),
    "NZ": ("en-NZ", "Pacific/Auckland"),
    "PT": ("pt-PT", "Europe/Lisbon"),
    "TH": ("th-TH", "Asia/Bangkok"),
    "VN": ("vi-VN", "Asia/Ho_Chi_Minh"),
    "AT": ("de-AT", "Europe/Vienna"),
    "BE": ("nl-BE", "Europe/Brussels"),
    "CH": ("de-CH", "Europe/Zurich"),
    "CN": ("zh-CN", "Asia/Shanghai"),
    "IE": ("en-IE", "Europe/Dublin"),
    "MY": ("ms-MY", "Asia/Kuala_Lumpur"),
    "PH": ("en-PH", "Asia/Manila"),
    "PL": ("pl-PL", "Europe/Warsaw"),
    "SE": ("sv-SE", "Europe/Stockholm"),
    "AE": ("ar-AE", "Asia/Dubai"),
    "AR": ("es-AR", "America/Argentina/Buenos_Aires"),
    "CL": ("es-CL", "America/Santiago"),
    "CO": ("es-CO", "America/Bogota"),
    "ZA": ("en-ZA", "Africa/Johannesburg"),
}


def _choose_protocol_impersonate() -> str:
    """Choose one supported device profile for a new registration session."""
    return secrets.choice(PROTOCOL_IMPERSONATE_PROFILES)


def _tls_fallback_impersonate(current: str) -> str:
    """Switch a failed TLS session to a profile from the other browser family."""
    normalized = str(current or "").strip().casefold()
    if normalized.startswith("chrome"):
        return FIREFOX_IMPERSONATE_PROFILES[0]
    return CHROME_IMPERSONATE_PROFILES[0]


def _proxy_fingerprint_profile(country: Any) -> dict[str, str]:
    normalized = str(country or "").strip().upper()
    language, timezone_name = PROXY_FINGERPRINT_PROFILES.get(
        normalized, ("en-US", "UTC")
    )
    return {
        "country": normalized or "UNSET",
        "language": language,
        "timezone": timezone_name,
    }
TRANSIENT_INIT_TRANSPORT_MARKERS = (
    "sslerror",
    "ssl_connect",
    "ssl_error_syscall",
    "curl: (28)",
    "curl: (35)",
    "curl: (52)",
    "curl: (56)",
    "connection closed abruptly",
    "connection reset",
    "unexpected eof",
    "tls handshake",
    "timed out",
)
TRANSIENT_INIT_EDGE_MARKERS = (
    "csrf 请求失败: 403",
    "csrf request failed: 403",
)
_PROVIDER_PROXY_SID_RE = re.compile(
    r"(-sid-)([A-Za-z0-9]{4,32})(-t-)",
    re.IGNORECASE,
)
_KOOKEEY_PROXY_ROUTE_RE = re.compile(
    r"^(?P<base>.+)-(?P<region>[A-Za-z]{2}|global)-"
    r"(?P<sid>[A-Za-z0-9]{8})-(?P<duration>\d+[mh])$",
    re.IGNORECASE,
)


class TransientInitRecoveryStrategy:
    """Classify cold-session failures and rotate refreshable proxy sessions."""

    @staticmethod
    def classify(result: Any) -> str:
        if not isinstance(result, dict):
            return ""
        if str(result.get("status") or "").strip().casefold() == "success":
            return ""
        error = result.get("error")
        detail = (
            " ".join(str(value or "") for value in error.values())
            if isinstance(error, dict)
            else str(error or "")
        )
        normalized = detail.casefold()
        if "init_page_email" not in normalized:
            return ""
        if any(marker in normalized for marker in TRANSIENT_INIT_EDGE_MARKERS):
            return "edge_403"
        if any(marker in normalized for marker in TRANSIENT_INIT_TRANSPORT_MARKERS):
            return "transport"
        return ""

    @staticmethod
    def refresh_proxy_session(proxy_url: str) -> str:
        """Return a provider URL with a fresh sticky-session id when supported."""

        value = str(proxy_url or "").strip()
        if not value:
            return value
        fresh_sid = "".join(
            secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
            for _ in range(8)
        )
        refreshed = _PROVIDER_PROXY_SID_RE.sub(
            lambda match: f"{match.group(1)}{fresh_sid}{match.group(3)}",
            value,
            count=1,
        )
        if refreshed != value:
            return refreshed
        try:
            parsed = urlsplit(value)
            username = unquote(parsed.username or "")
            password = unquote(parsed.password or "")
            route = _KOOKEEY_PROXY_ROUTE_RE.fullmatch(password)
            hostname = parsed.hostname or ""
        except ValueError:
            return value
        if route is None or not username or not hostname:
            return value
        refreshed_password = (
            f"{route.group('base')}-{route.group('region')}-"
            f"{fresh_sid}-{route.group('duration')}"
        )
        host = f"[{hostname}]" if ":" in hostname else hostname
        port = f":{parsed.port}" if parsed.port else ""
        netloc = (
            f"{quote(username, safe='')}:{quote(refreshed_password, safe='')}@"
            f"{host}{port}"
        )
        return urlunsplit(
            (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
        )


TRANSIENT_INIT_RECOVERY = TransientInitRecoveryStrategy()


def _configure_utf8_stdio(streams: tuple[Any, ...] | None = None) -> None:
    for stream in streams or (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def _emit_event(
    stage: str,
    message: str,
    status: str = "active",
    **details: Any,
) -> None:
    safe = re.sub(
        r"(?i)((?:access_?token|session_?token|otp)\s*[=:]\s*)[A-Za-z0-9._~+/-]{6,}",
        r"\1***",
        str(message or "").strip(),
    )
    event = {"stage": stage, "message": safe[:500], "status": status}
    event.update(details)
    print(EVENT_PREFIX + json.dumps(event, ensure_ascii=False), flush=True)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


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


def _is_invalid_auth_step_failure(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if str(result.get("status") or "").strip().casefold() == "success":
        return False
    error = result.get("error")
    if isinstance(error, dict):
        detail = " ".join(str(value or "") for value in error.values())
    else:
        detail = str(error or "")
    normalized = detail.casefold()
    return (
        "invalid_auth_step" in normalized
        or "invalid authorization step" in normalized
    )


def _is_password_reset_completed(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if str(result.get("status") or "").strip().casefold() == "success":
        return False
    error = result.get("error")
    if isinstance(error, dict):
        detail = " ".join(str(value or "") for value in error.values())
    else:
        detail = str(error or "")
    normalized = detail.casefold()
    return any(
        marker in normalized
        for marker in (
            "account_password_reset_completed_use_current_session",
            "account_password_reset_completed_retry_login",
        )
    )


def _is_password_add_completed(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    error = result.get("error")
    detail = (
        " ".join(str(value or "") for value in error.values())
        if isinstance(error, dict)
        else str(error or "")
    )
    normalized = detail.casefold()
    return any(
        marker in normalized
        for marker in (
            "account_password_add_completed_use_current_session",
            "account_password_add_completed_retry_login",
        )
    )


def _is_transient_init_failure(result: Any) -> bool:
    """Retry only transport failures that happen before password/OTP work starts."""
    return bool(TRANSIENT_INIT_RECOVERY.classify(result))


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
    setup_credentials = payload.get("setup_credentials", True) is not False
    fingerprint = _proxy_fingerprint_profile(payload.get("proxy_country"))
    fingerprint_country = fingerprint["country"]
    fingerprint_language = fingerprint["language"]
    fingerprint_timezone = fingerprint["timezone"]
    password = (
        str(payload.get("existing_password") or "") if setup_credentials else ""
    )
    password_confirmed = bool(
        setup_credentials and payload.get("existing_password_confirmed")
    )
    totp_secret = (
        str(payload.get("existing_totp_secret") or "")
        if setup_credentials
        else ""
    )
    existing_access_token = str(payload.get("existing_access_token") or "").strip()
    existing_session_token = str(payload.get("existing_session_token") or "").strip()
    existing_session = _json_object(payload.get("existing_session_json"))
    existing_session_cookies = payload.get("existing_session_cookies")
    existing_device_id = str(payload.get("existing_device_id") or "").strip()
    raw_existing_impersonate = str(
        payload.get("existing_impersonate") or ""
    ).strip()
    existing_impersonate = normalize_protocol_impersonate(
        raw_existing_impersonate
    )
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

    _emit_event(
        "protocol_auth",
        (
            "启动 Mail Auth：CSRF → 账号密码注册 → OTP → OAuth callback → Session"
            if setup_credentials
            else "启动 Mail Auth：CSRF → 邮箱 OTP → OAuth callback → Session/Cookie"
        ),
    )
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

    def password_checkpoint(candidate: str, confirmed: bool) -> None:
        nonlocal password, password_confirmed
        if not setup_credentials:
            return
        value = str(candidate or "").strip()
        if len(value) < 12:
            return
        password = value
        password_confirmed = bool(confirmed) or password_confirmed
        _emit_event(
            "password_checkpoint",
            (
                "账号密码已由 OpenAI 确认并保存"
                if password_confirmed
                else "注册密码候选值已保存；后续重试将复用同一密码"
            ),
            "success" if password_confirmed else "active",
            password_checkpoint={
                "password": password,
                "password_confirmed": password_confirmed,
                "result": {},
            },
        )

    bot = None
    raw: Any = None
    invalid_state_retries = 0
    invalid_auth_step_recovery_attempts = 0
    invalid_auth_step_recovered = False
    invalid_auth_step_replay_stopped = False
    password_reset_login_retries = 0
    password_add_attempts = 0
    password_login_verification_attempts = 0
    transient_init_retries = 0
    full_attempts = 0
    impersonate = existing_impersonate or _choose_protocol_impersonate()
    if raw_existing_impersonate and not existing_impersonate:
        _emit_event(
            "network",
            f"已保存的协议指纹不受支持：{raw_existing_impersonate}；"
            f"已重新选择 {impersonate}",
            "warning",
        )
    impersonate_source = "复用已保存" if existing_impersonate else "随机选择"
    _emit_event(
        "network",
        f"协议设备指纹已{impersonate_source}并锁定本次会话：{impersonate}；"
        "代理地区："
        f"{fingerprint_country} / {fingerprint_language} / {fingerprint_timezone}",
    )
    resumed_passwordless_session = bool(
        len(password) >= 12 and existing_access_token and not password_confirmed
    )
    recent_auth_login_required = bool(
        password_confirmed
        and len(password) >= 12
        and existing_access_token
        and not totp_secret
    )
    password_reset_required = False
    recovery_session_raw: dict[str, Any] | None = None
    auth_recovery_mode = False
    if resumed_passwordless_session:
        recovery_session_raw = {
            "status": "success",
            "access_token": existing_access_token,
            "session_token": existing_session_token,
            "session_json": existing_session,
            "session_cookies": existing_session_cookies,
            "device_id": existing_device_id,
            "impersonate": existing_impersonate or impersonate,
            "password": "",
            "password_set": False,
        }
    resumed_from_checkpoint = bool(
        resumed_passwordless_session
        or (
            password_confirmed
            and len(password) >= 12
            and existing_access_token
            and bool(totp_secret)
        )
    )
    if resumed_from_checkpoint:
        raw = {
            "status": "success",
            "access_token": existing_access_token,
            "session_token": existing_session_token,
            "session_json": existing_session,
            "session_cookies": existing_session_cookies,
            "device_id": existing_device_id,
            "impersonate": impersonate,
            "password": password,
            "password_set": password_confirmed,
        }
        _emit_event(
            "two_factor" if password_confirmed else "password",
            (
                "检测到已保存的账号密码和 Session；跳过重复注册，仅补跑 TOTP 2FA"
                if password_confirmed
                else "检测到已保存的账号与 Session；跳过重复注册，直接补设同一候选密码"
            ),
            "active",
        )
    elif recent_auth_login_required:
        _emit_event(
            "two_factor",
            "密码与 Session 已保存但 TOTP 待补跑；先用同一密码重新认证以满足近期认证要求",
            "active",
        )
    while not resumed_from_checkpoint:
        # Reconstructing the register object is intentional: register() creates
        # a fresh OpenAIAuthClient, device id, cookie jar, OAuth state, and OTP
        # request for every full attempt.
        full_attempts += 1
        if auth_recovery_mode or recent_auth_login_required:
            password_login_verification_attempts += 1
        bot = register_class(
            {
                "email": email,
                # Reuse even an unconfirmed staged candidate. The previous POST
                # may have succeeded server-side before its response was lost.
                "password": password,
                "password_confirmed": (
                    password_confirmed or auth_recovery_mode
                ),
                "password_verification_only": auth_recovery_mode,
                "force_password_reset": password_reset_required,
                "fingerprint_country": fingerprint_country,
                "language": fingerprint_language,
                "timezone": fingerprint_timezone,
                "client_id": "",
                "refresh_token": code_url,
            },
            log_fn=log,
            proxy=proxy_url,
            otp_timeout=120,
            impersonate=impersonate,
            # Passwordless mode stops after the OTP-authenticated Session.
            # Credential mode also asks the service to add and verify a password.
            with_password=setup_credentials,
            password_checkpoint_fn=password_checkpoint,
        )
        raw = bot.register()
        if (
            auth_recovery_mode
            and isinstance(raw, dict)
            and str(raw.get("status") or "").strip().casefold() == "success"
        ):
            invalid_auth_step_recovered = True
        if _is_password_reset_completed(raw):
            if password_reset_login_retries >= PASSWORD_RESET_LOGIN_RETRY_LIMIT:
                break
            password_reset_login_retries += 1
            password_confirmed = True
            password_reset_required = False
            if recovery_session_raw is not None:
                raw = dict(recovery_session_raw)
                raw["status"] = "success"
                raw["password"] = password
                raw["password_set"] = True
                _emit_event(
                    "password",
                    "账号密码已重置为保存值；复用已保存 Session 继续配置 2FA",
                    "success",
                )
                break
            _emit_event(
                "password",
                "账号密码已重置为保存值；正在创建新会话并重新登录（1/1）",
                "warning",
            )
            continue
        if _is_invalid_state_failure(raw):
            if invalid_state_retries >= INVALID_STATE_FULL_RETRY_LIMIT:
                break
            invalid_state_retries += 1
            _emit_event(
                "protocol_auth",
                "OpenAI 在创建账号步骤返回会话失效；正在创建新会话并重新获取验证码（1/1）",
                "warning",
            )
            continue
        if _is_invalid_auth_step_failure(raw):
            if (
                invalid_auth_step_recovery_attempts
                < INVALID_AUTH_STEP_RECOVERY_LIMIT
                and len(password) >= 12
            ):
                invalid_auth_step_recovery_attempts += 1
                auth_recovery_mode = True
                _emit_event(
                    "password",
                    "OpenAI 当前认证步骤已变化；正在停止创建账号并切换到"
                    "现有账号登录/密码重置恢复（1/1）",
                    "warning",
                )
                continue
            invalid_auth_step_replay_stopped = True
            _emit_event(
                "password",
                "OpenAI 当前认证步骤与密码注册不匹配，且一次登录恢复未成功；"
                "已停止自动处理，避免重复提交密码或创建账号",
                "error",
            )
            break
        transient_init_kind = TRANSIENT_INIT_RECOVERY.classify(raw)
        if transient_init_kind:
            if transient_init_retries >= TRANSIENT_INIT_FULL_RETRY_LIMIT:
                break
            transient_init_retries += 1
            refreshed_proxy_url = TRANSIENT_INIT_RECOVERY.refresh_proxy_session(
                proxy_url
            )
            proxy_session_rotated = refreshed_proxy_url != proxy_url
            proxy_url = refreshed_proxy_url
            if transient_init_retries == 1:
                impersonate = _tls_fallback_impersonate(impersonate)
                retry_profile_message = f"切换 TLS 指纹为 {impersonate}"
            else:
                retry_profile_message = f"继续使用 TLS 指纹 {impersonate}"
            failure_message = (
                "CSRF 被边缘防护拒绝"
                if transient_init_kind == "edge_403"
                else "TLS/代理连接中断"
            )
            proxy_message = (
                "并刷新住宅代理会话"
                if proxy_session_rotated
                else "并保持当前代理入口"
            )
            _emit_event(
                "network",
                f"init_page_email {failure_message}；正在创建全新会话，"
                f"{retry_profile_message}{proxy_message}"
                f"（{transient_init_retries}/{TRANSIENT_INIT_FULL_RETRY_LIMIT}）",
                "warning",
            )
            time.sleep(float(transient_init_retries))
            continue
        break
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
    password_was_set = bool(raw.get("password_set"))
    checkpoint_diagnostics = {
        "full_attempts": (
            full_attempts
        ),
        "invalid_state_recovered": bool(invalid_state_retries),
        "invalid_auth_step_recovered": invalid_auth_step_recovered,
        "invalid_auth_step_recovery_attempts": invalid_auth_step_recovery_attempts,
        "invalid_auth_step_replay_stopped": invalid_auth_step_replay_stopped,
        "password_reset_recovered": bool(password_reset_login_retries),
        "password_add_reauth_recovered": False,
        "passwordless_signup_recovered": resumed_passwordless_session,
        "password_add_from_session": not password_was_set,
        "password_add_attempts": 0,
        "password_login_verified": False,
        "password_login_verification_attempts": (
            password_login_verification_attempts
        ),
        "recent_auth_login": recent_auth_login_required,
        "setup_credentials": setup_credentials,
        "transient_init_recovered": bool(transient_init_retries),
        "resumed_from_password_checkpoint": resumed_from_checkpoint,
        "impersonate": impersonate,
        "fingerprint_country": fingerprint_country,
        "fingerprint_language": fingerprint_language,
        "fingerprint_timezone": fingerprint_timezone,
    }
    checkpoint_result = {
        "status": "partial",
        "email": email,
        "access_token": access_token,
        "session_json": json.dumps(session, ensure_ascii=False),
        "storage_state_json": json.dumps(
            _storage_state(session_token, device_id), ensure_ascii=False
        ),
        "cookies_json": json.dumps(
            raw.get("session_cookies") or [], ensure_ascii=False
        ),
        "session_acquisition_method": "gptfree_mail_auth",
        "registration_diagnostics": checkpoint_diagnostics,
    }
    if not setup_credentials:
        _emit_event(
            "completed",
            "Session/Cookie 已保存；已按设置跳过密码和 TOTP 2FA",
            "success",
        )
        return {
            "status": "success",
            "email": email,
            "access_token": access_token,
            "session_json": json.dumps(session, ensure_ascii=False),
            "storage_state_json": json.dumps(
                _storage_state(session_token, device_id), ensure_ascii=False
            ),
            "cookies_json": json.dumps(
                raw.get("session_cookies") or [], ensure_ascii=False
            ),
            "session_acquisition_method": "gptfree_mail_auth",
            "session_token": session_token,
            "device_id": device_id,
            "registration_diagnostics": checkpoint_diagnostics,
        }

    def confirm_password_checkpoint() -> None:
        nonlocal password_confirmed
        password_confirmed = True
        _emit_event(
            "password_checkpoint",
            "账号密码和 Session 已确认并保存；后续 2FA 失败将只补跑 2FA",
            "success",
            password_checkpoint={
                "password": generated_password,
                "password_confirmed": True,
                "result": checkpoint_result,
            },
        )
        _emit_event("password", "账号密码已确认，正在配置 2FA")

    def add_password_with_current_session() -> dict[str, Any]:
        """Add the staged password once, reusing the registration Session."""
        nonlocal password_confirmed
        nonlocal password_add_attempts
        nonlocal session_token

        def verified_password_checkpoint(
            candidate_password: str,
            confirmed: bool,
        ) -> None:
            # The outer credential coordinator owns the durable checkpoint.
            # Ignore the nested callback so it is written exactly once after
            # password/add or password/reset reports success.
            _ = (candidate_password, confirmed)

        password_add_attempts += 1
        checkpoint_diagnostics["password_add_attempts"] = password_add_attempts
        _emit_event(
            "password",
            "正在使用当前注册 Session 添加密码（1/1）",
        )
        password_adder = register_class(
            {
                "email": email,
                "password": generated_password,
                "password_confirmed": False,
                "password_verification_only": True,
                "password_add_reauth": True,
                "reauth_access_token": access_token,
                "reauth_session_token": session_token,
                "reauth_session_cookies": raw.get("session_cookies") or [],
                "reauth_device_id": device_id,
                "totp_secret": totp_secret,
                "force_password_reset": False,
                "fingerprint_country": fingerprint_country,
                "language": fingerprint_language,
                "timezone": fingerprint_timezone,
                "client_id": "",
                "refresh_token": code_url,
            },
            log_fn=log,
            proxy=proxy_url,
            otp_timeout=120,
            impersonate=impersonate,
            with_password=True,
            password_checkpoint_fn=verified_password_checkpoint,
        )
        added_raw = password_adder.register()
        if _is_password_add_completed(added_raw) or _is_password_reset_completed(
            added_raw
        ):
            password_confirmed = True
            checkpoint_diagnostics["password_add_reauth_recovered"] = True
            _emit_event(
                "password",
                "当前认证会话已添加密码；直接复用注册 Session 配置 2FA",
                "success",
            )
            return {
                "verified": True,
                "password_added": True,
                "reuse_registration_session": True,
            }
        if isinstance(added_raw, dict) and str(
            added_raw.get("status") or ""
        ).strip().casefold() == "success":
            password_confirmed = True
            access_token_from_add = str(added_raw.get("access_token") or "").strip()
            session_token_from_add = str(
                added_raw.get("session_token") or ""
            ).strip()
            session_payload_from_add = added_raw.get("session_json")
            session_cookies_from_add = added_raw.get("session_cookies")
            if isinstance(session_payload_from_add, dict):
                session.update(session_payload_from_add)
            if session_token_from_add:
                session_token = session_token_from_add
                session["sessionToken"] = session_token_from_add
            if isinstance(session_cookies_from_add, list):
                raw["session_cookies"] = session_cookies_from_add
            _emit_event(
                "password",
                "当前认证会话已添加密码并返回可用 Session；正在配置 2FA",
                "success",
            )
            return {
                "verified": True,
                "password_added": True,
                "access_token": access_token_from_add,
                "session_token": session_token_from_add,
                "reuse_registration_session": not access_token_from_add,
            }
        return {
            "verified": False,
            "error": str(
                added_raw.get("error")
                if isinstance(added_raw, dict)
                else added_raw
            ),
        }

    credentials = complete_protocol_credentials(
        email=email,
        access_token=access_token,
        generated_password=generated_password,
        password_set=password_was_set,
        proxy_url=proxy_url,
        session_token=session_token,
        device_id=device_id,
        session_cookies=raw.get("session_cookies"),
        impersonate=str(raw.get("impersonate") or impersonate),
        language=fingerprint_language,
        existing_totp_secret=totp_secret,
        on_password_confirmed=(
            confirm_password_checkpoint
        ),
        password_verifier=(
            add_password_with_current_session if not password_was_set else None
        ),
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
            "full_attempts": (
                full_attempts
            ),
            "invalid_state_recovered": bool(invalid_state_retries),
            "invalid_auth_step_recovered": invalid_auth_step_recovered,
            "invalid_auth_step_recovery_attempts": (
                invalid_auth_step_recovery_attempts
            ),
            "invalid_auth_step_replay_stopped": invalid_auth_step_replay_stopped,
            "password_reset_recovered": bool(password_reset_login_retries),
            "password_add_reauth_recovered": bool(
                checkpoint_diagnostics.get("password_add_reauth_recovered")
            ),
            "passwordless_signup_recovered": resumed_passwordless_session,
            "password_add_from_session": not password_was_set,
            "password_add_attempts": password_add_attempts,
            "password_login_verified": bool(
                checkpoint_diagnostics.get("password_login_verified")
            ),
            "password_login_verification_attempts": password_login_verification_attempts,
            "recent_auth_login": recent_auth_login_required,
            "transient_init_recovered": bool(transient_init_retries),
            "resumed_from_password_checkpoint": resumed_from_checkpoint,
            "impersonate": impersonate,
            "fingerprint_country": fingerprint_country,
            "fingerprint_language": fingerprint_language,
            "fingerprint_timezone": fingerprint_timezone,
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
