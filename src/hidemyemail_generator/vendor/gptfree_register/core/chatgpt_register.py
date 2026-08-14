"""ChatGPT Mail Auth registration protocol (curl_cffi + Sentinel).

对照 ``~/Downloads/chatgpt_register``：
- TLS 指纹：curl_cffi impersonate=chrome136
- Sentinel：实时 SDK P + Turnstile + SO 双 header
- 流程：login_hint 初始化 → 账号密码注册 → 发送/接收 OTP → about-you → OAuth callback

OTP 仍走项目原版 ``email_provider.fetch_otp``（Graph→IMAP fallback），兼容 outlook 卡密。

对外 API 保持兼容：
    bot = ChatGPTRegister(outlook_creds, log_fn=..., proxy=...)
    result = bot.register()
    # {"status": "success"|"failed", "email", "password", "access_token", ...}
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import string
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urljoin, urlparse

log = logging.getLogger("chatgpt_register")

FIRST_NAMES = [
    "James", "John", "Robert", "Michael", "David", "William", "Richard", "Joseph",
    "Thomas", "Chris", "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara",
    "Susan", "Jessica", "Sarah", "Karen", "Daniel", "Matthew", "Anthony", "Mark",
    "Donald", "Steven", "Paul", "Andrew", "Joshua", "Kenneth",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Wilson", "Anderson", "Taylor", "Thomas", "Moore",
    "Jackson", "Martin", "Lee", "Thompson", "White",
]


def _norm_email(value: str) -> str:
    return str(value or "").strip()


def random_name() -> str:
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


def random_age() -> int:
    return random.randint(24, 36)


def random_password(length: int = 16) -> str:
    if length < 12:
        length = 12
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    special = "!@#$%^&*"
    must = [
        random.choice(upper),
        random.choice(lower),
        random.choice(digits),
        random.choice(special),
    ]
    all_chars = upper + lower + digits + special
    rest = random.choices(all_chars, k=length - len(must))
    pwd_list = must + rest
    random.shuffle(pwd_list)
    return "".join(pwd_list)


def birthdate_from_age(age: int) -> str:
    return (datetime.now() - timedelta(days=int(age) * 365)).strftime("%Y-%m-%d")


def _normalize_proxy(proxy: str) -> str:
    """curl_cffi 接受 http(s)/socks5 URL；空串原样返回。"""
    p = (proxy or "").strip()
    if not p:
        return ""
    if "://" not in p:
        # host:port 或 user:pass@host:port → 默认 http
        p = "http://" + p
    return p


def _accept_language(language: str) -> str:
    primary = str(language or "en-US").strip() or "en-US"
    root = primary.split("-", 1)[0]
    if primary.casefold() == "en-us":
        return "en-US,en;q=0.9"
    return f"{primary},{root};q=0.9,en-US;q=0.8,en;q=0.7"


def auth_step_requires_password(continue_url: str, page_type: str) -> bool:
    """Follow the server-selected auth branch instead of forcing password creation."""
    marker = f"{continue_url or ''} {page_type or ''}".lower()
    return "password" in marker


def auth_step_requires_email_otp(continue_url: str, page_type: str) -> bool:
    """Return whether auth is showing the pre-verification choice page.

    The localized page renders a language-independent link to
    ``/create-account/password``.  Seeing the OTP input does not mean the OTP
    should be consumed yet: password signup must follow that link first.
    """
    marker = f"{continue_url or ''} {page_type or ''}".lower()
    return "email-verification" in marker or "email_otp" in marker


def auth_step_requires_mfa(continue_url: str, page_type: str) -> bool:
    """Return whether a successful password verification advanced to MFA."""
    marker = f"{continue_url or ''} {page_type or ''}".lower().replace("-", "_")
    return "mfa_challenge" in marker or "/mfa" in marker


def auth_mfa_details(result: Any) -> tuple[str, str, str]:
    """Extract the selected MFA factor from current auth response shapes."""
    if not isinstance(result, dict):
        return "", "", ""
    page = result.get("page") if isinstance(result.get("page"), dict) else {}
    payload = page.get("payload") if isinstance(page.get("payload"), dict) else {}
    factor_id = str(
        payload.get("factor_id")
        or result.get("factor_id")
        or ""
    ).strip()
    factors = payload.get("factors") or result.get("mfa_factors") or []
    factors = factors if isinstance(factors, list) else []
    selected: dict[str, Any] = {}
    if factor_id:
        selected = next(
            (
                item
                for item in factors
                if isinstance(item, dict)
                and str(item.get("id") or "").strip() == factor_id
            ),
            {},
        )
    if not selected:
        selected = next(
            (
                item
                for item in factors
                if isinstance(item, dict)
                and str(
                    item.get("factor_type") or item.get("type") or ""
                ).strip().casefold() == "totp"
            ),
            {},
        )
    if not factor_id:
        factor_id = str(selected.get("id") or "").strip()
    if not factor_id:
        continue_url, _ = auth_next_step(result)
        path_parts = [part for part in urlparse(continue_url).path.split("/") if part]
        if "mfa-challenge" in path_parts and path_parts[-1] != "mfa-challenge":
            factor_id = path_parts[-1]
    factor_type = str(
        selected.get("factor_type") or selected.get("type") or "totp"
    ).strip().casefold()
    metadata = (
        selected.get("metadata")
        if isinstance(selected.get("metadata"), dict)
        else {}
    )
    request_id = str(
        payload.get("mfa_request_id")
        or result.get("mfa_request_id")
        or metadata.get("mfa_request_id")
        or ""
    ).strip()
    return factor_id, factor_type or "totp", request_id


ACCOUNT_UNUSABLE_CODES = frozenset(
    {"account_deactivated", "account_deleted", "account_banned"}
)
OTP_RETRYABLE_CODES = frozenset(
    {
        "code_expired",
        "code_invalid",
        "expired_otp",
        "incorrect_otp",
        "invalid_code",
        "invalid_otp",
        "verification_code_invalid",
    }
)


def auth_error_details(result: Any) -> tuple[str, str]:
    """Return a normalized auth error code and message from a response payload."""
    if not isinstance(result, dict):
        return "", ""
    error = result.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or ""), str(error.get("message") or "")
    if error:
        return str(error), str(result.get("message") or "")
    return str(result.get("error_code") or ""), str(result.get("message") or "")


def otp_error_is_retryable(code: str, message: str) -> bool:
    """Identify an invalid or expired email OTP without retrying unrelated errors."""
    normalized_code = str(code or "").lower()
    if normalized_code in OTP_RETRYABLE_CODES:
        return True
    text = f"{normalized_code} {message or ''}".lower()
    return any(marker in text for marker in ("invalid otp", "expired otp", "incorrect code"))


def auth_next_step(result: Any) -> tuple[str, str]:
    """Read the server-selected URL/page type across current auth response shapes."""
    if not isinstance(result, dict):
        return "", ""
    page = result.get("page")
    page = page if isinstance(page, dict) else {}
    payload = page.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    continue_url = next(
        (
            str(value)
            for value in (
                result.get("continue_url"),
                result.get("external_url"),
                result.get("url"),
                page.get("continue_url"),
                page.get("external_url"),
                page.get("url"),
                payload.get("continue_url"),
                payload.get("external_url"),
                payload.get("url"),
            )
            if value
        ),
        "",
    )
    return continue_url, str(page.get("type") or result.get("page_type") or "")


def auth_step_is_direct_oauth(continue_url: str, page_type: str) -> bool:
    """Detect an OTP result that should finalize OAuth instead of creating a user."""
    target = str(continue_url or "")
    if not target or "about-you" in target.lower() or "about_you" in target.lower():
        return False
    parsed = urlparse(target)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    return bool(
        str(page_type or "").lower() == "external_url"
        or (host == "chatgpt.com" and path.startswith("/api/auth/callback/"))
        or (host == "auth.openai.com" and path.startswith("/api/auth/callback/"))
        or (host == "auth.openai.com" and path.startswith("/authorize/continue"))
    )


def session_cookie_records(session: Any) -> list[dict[str, Any]]:
    """Export the live registration cookie jar for same-session follow-up APIs."""
    records: list[dict[str, Any]] = []
    try:
        cookie_jar = session.cookies.jar
    except Exception:
        return records
    for cookie in cookie_jar:
        records.append(
            {
                "name": str(getattr(cookie, "name", "") or ""),
                "value": str(getattr(cookie, "value", "") or ""),
                "domain": str(getattr(cookie, "domain", "") or ""),
                "path": str(getattr(cookie, "path", "") or "/"),
                "secure": bool(getattr(cookie, "secure", False)),
            }
        )
    return [record for record in records if record["name"]]


# ─────────────────────────────────────────────────────────────────────
# Sentinel provider（共享 session / 代理）
# ─────────────────────────────────────────────────────────────────────
class _SentinelWithProxy:
    """包装 sentinel_token.SentinelTokenProvider，注入 proxy 与共享 session。"""

    def __init__(
        self,
        impersonate: str = "chrome136",
        proxy: str = "",
        language: str = "en-US",
        timezone_name: str = "UTC",
    ):
        from sentinel_token import SentinelTokenProvider as _Impl

        class _Provider(_Impl):
            def __init__(
                self,
                impersonate: str = "chrome136",
                cookies: dict = None,
                proxy: str = None,
                language: str = "en-US",
                timezone_name: str = "UTC",
            ):
                super().__init__(
                    impersonate=impersonate,
                    cookies=cookies,
                    language=language,
                    timezone_name=timezone_name,
                )
                self._proxy = proxy or ""
                self._language = language

            async def _get_session(self):
                if not self._session:
                    from curl_cffi import requests as _req
                    kwargs: dict[str, Any] = {
                        "impersonate": self.impersonate,
                        "timeout": 60,
                        "headers": {"accept-language": _accept_language(self._language)},
                    }
                    if self._proxy:
                        kwargs["proxies"] = {"http": self._proxy, "https": self._proxy}
                    self._session = _req.AsyncSession(**kwargs)
                return self._session

            def set_session(self, session) -> None:
                self._session = session

            def set_cookies(self, cookies: dict) -> None:
                self._cookies = cookies or {}

        self._impl = _Provider(
            impersonate=impersonate,
            proxy=proxy or None,
            language=language,
            timezone_name=timezone_name,
        )

    def __getattr__(self, name: str):
        return getattr(self._impl, name)


# ─────────────────────────────────────────────────────────────────────
# OpenAI Auth Client（新协议）
# ─────────────────────────────────────────────────────────────────────
class OpenAIAuthClient:
    BASE_URL = "https://auth.openai.com"
    CHATGPT_URL = "https://chatgpt.com"

    def __init__(
        self,
        *,
        impersonate: str = "chrome136",
        sentinel: Any = None,
        proxy: str = "",
        language: str = "en-US",
        timezone_name: str = "UTC",
        initial_session_token: str = "",
        initial_session_cookies: Any = None,
        device_id: str = "",
    ):
        self.impersonate = impersonate
        self.proxy = _normalize_proxy(proxy)
        self.language = str(language or "en-US")
        self.timezone_name = str(timezone_name or "UTC")
        self.sentinel = sentinel or _SentinelWithProxy(
            impersonate=impersonate,
            proxy=self.proxy,
            language=self.language,
            timezone_name=self.timezone_name,
        )
        self._session = None
        self.initial_session_token = str(initial_session_token or "").strip()
        self.initial_session_cookies = (
            [dict(item) for item in initial_session_cookies if isinstance(item, dict)]
            if isinstance(initial_session_cookies, list)
            else []
        )
        self.device_id: str = str(device_id or uuid.uuid4())
        self.auth_session_logging_id: str = str(uuid.uuid4())
        self.cookies: dict = {}

    async def _get_session(self):
        if not self._session:
            from curl_cffi import requests as _req
            kwargs: dict[str, Any] = {
                "impersonate": self.impersonate,
                "timeout": 60,
                "headers": {"accept-language": _accept_language(self.language)},
            }
            if self.proxy:
                kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
            self._session = _req.AsyncSession(**kwargs)
            cookie_names: set[str] = set()
            for cookie in self.initial_session_cookies:
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
                self._session.cookies.set(name, value, **options)
                cookie_names.add(name)
            if (
                self.initial_session_token
                and "__Secure-next-auth.session-token" not in cookie_names
            ):
                self._session.cookies.set(
                    "__Secure-next-auth.session-token",
                    self.initial_session_token,
                    domain="chatgpt.com",
                    path="/",
                )
        return self._session

    async def share_session_with_sentinel(self) -> None:
        s = await self._get_session()
        set_session = getattr(self.sentinel, "set_session", None)
        if callable(set_session):
            set_session(s)

    def _common_headers(self, referer: str | None = None) -> dict:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": self.BASE_URL,
            "oai-device-id": self.device_id,
            "accept-language": _accept_language(self.language),
            "oai-language": self.language,
        }
        if referer:
            headers["referer"] = referer
        return headers

    async def _add_sentinel_headers(
        self,
        headers: dict,
        flow: str,
        referer: str,
        *,
        force_refresh: bool = False,
        log_fn=None,
    ) -> dict:
        get_token = self.sentinel.get_token
        try:
            token = await get_token(flow, self.device_id, force_refresh=force_refresh)
        except TypeError:
            # 旧签名兼容
            if force_refresh:
                inv = getattr(self.sentinel, "invalidate_cache", None)
                if callable(inv):
                    inv()
            token = await get_token(flow, self.device_id)
        if not token:
            raise RuntimeError(f"sentinel get_token 失败 flow={flow}")
        # 不把内部标记字段塞进 header
        header_token = {k: v for k, v in token.items() if not str(k).startswith("_")}
        missing_t = bool(token.get("_turnstile_missing"))
        if log_fn:
            log_fn(
                f"  [sentinel] flow={flow} keys={list(header_token.keys())} "
                f"has_t={'t' in header_token} t_len={len(header_token.get('t') or '')} "
                f"missing_t={missing_t}"
            )
        if missing_t:
            raise RuntimeError(
                "sentinel turnstile(t) 生成失败：请确认已 npm install jsdom，"
                "且 sentinel_vm/sdk.js 或 ~/.codeium/windsurf/sentinel_sdk_full.js 存在"
            )
        headers["openai-sentinel-token"] = json.dumps(header_token)
        so_token = await self.sentinel.get_so_token(flow, self.device_id)
        if so_token:
            headers["openai-sentinel-so-token"] = json.dumps(so_token)
        return headers

    async def init_page_email(
        self,
        email: str,
        *,
        prefer_login: bool = False,
        prefer_password_signup: bool = False,
        post_login_add_password: bool = False,
    ) -> dict:
        """Initialize auth, optionally stopping before OpenAI selects OTP signup.

        Password-first signup deliberately omits ``login_hint`` from the initial
        OAuth request. Supplying it lets the server auto-select the passwordless
        email-OTP branch before we can post ``connection=password``.
        """
        s = await self._get_session()

        # Keep the device id used by NextAuth, the OAuth authorize request,
        # Sentinel, and the account APIs identical for the entire transaction.
        s.cookies.set("oai-did", self.device_id, domain="chatgpt.com")
        await s.get(self.CHATGPT_URL)

        csrf_resp = await s.get(f"{self.CHATGPT_URL}/api/auth/csrf")
        if csrf_resp.status_code != 200:
            raise RuntimeError(f"CSRF 请求失败: {csrf_resp.status_code}")
        csrf_token = csrf_resp.json().get("csrfToken")
        if not csrf_token:
            raise RuntimeError("CSRF token 为空")

        signin_params = {
            "prompt": "login",
            "screen_hint": "login" if prefer_login else "login_or_signup",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": self.auth_session_logging_id,
            "ext-passkey-client-capabilities": "1111",
        }
        if post_login_add_password:
            signin_params["post_login_add_password"] = "true"
        if not prefer_password_signup and not post_login_add_password:
            signin_params["login_hint"] = email
        params = urlencode(signin_params)
        signin_resp = await s.post(
            f"{self.CHATGPT_URL}/api/auth/signin/openai?{params}",
            data={
                "callbackUrl": f"{self.CHATGPT_URL}/",
                "csrfToken": csrf_token,
                "json": "true",
            },
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "origin": self.CHATGPT_URL,
                "referer": f"{self.CHATGPT_URL}/",
            },
            allow_redirects=False,
        )
        loc = ""
        try:
            loc = signin_resp.json().get("url", "")
        except Exception:
            loc = signin_resp.headers.get("location", "") or ""

        final_resp = None
        current_url = loc
        referer = f"{self.CHATGPT_URL}/"
        for _ in range(12):
            if not current_url:
                break
            final_resp = await s.get(
                current_url,
                headers={"referer": referer},
                allow_redirects=False,
            )
            next_location = final_resp.headers.get("location", "") or ""
            if not next_location:
                break
            referer = str(getattr(final_resp, "url", None) or current_url)
            current_url = urljoin(referer, next_location)

        for cookie in s.cookies.jar:
            if cookie.name == "oai-did":
                self.device_id = cookie.value
                break
        self.cookies = {c.name: c.value for c in s.cookies.jar}
        page_url = str(getattr(final_resp, "url", "") or current_url or "")
        return {
            "status": final_resp.status_code if final_resp else 0,
            "cookies": self.cookies,
            "device_id": self.device_id,
            "page_url": page_url,
            "page_path": urlparse(page_url).path,
        }

    async def validate_email_otp(self, code: str) -> dict:
        s = await self._get_session()
        url = f"{self.BASE_URL}/api/accounts/email-otp/validate"
        referer = f"{self.BASE_URL}/email-verification"
        headers = self._common_headers(referer=referer)
        headers["accept"] = "application/json"
        resp = await s.post(url, json={"code": code}, headers=headers)
        try:
            data = resp.json()
        except Exception:
            data = {"status": resp.status_code, "text": resp.text}
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        return data

    async def send_email_otp(self) -> dict:
        """Request a fresh email OTP while preserving the current auth session."""
        s = await self._get_session()
        url = f"{self.BASE_URL}/api/accounts/email-otp/send"
        referer = f"{self.BASE_URL}/email-verification"
        resp = await s.get(
            url,
            headers=self._common_headers(referer=referer),
            allow_redirects=True,
        )
        try:
            payload = resp.json()
        except Exception:
            payload = {"text": str(getattr(resp, "text", "") or "")}
        if not isinstance(payload, dict):
            payload = {"payload": payload}
        payload["_http_status"] = int(getattr(resp, "status_code", 0) or 0)
        return payload

    async def navigate_password_registration(self) -> dict:
        """Enter the username/password signup branch before receiving the OTP."""
        s = await self._get_session()
        target = f"{self.BASE_URL}/create-account/password"
        resp = await s.get(
            target,
            headers={"referer": f"{self.BASE_URL}/email-verification"},
            allow_redirects=True,
        )
        return {
            "status": int(getattr(resp, "status_code", 0) or 0),
            "url": str(getattr(resp, "url", "") or target),
            "text": str(getattr(resp, "text", "") or ""),
        }

    async def register_password_email(
        self,
        email: str,
        password: str,
        *,
        force_refresh_sentinel: bool = False,
        log_fn=None,
    ) -> dict:
        """POST /api/accounts/user/register to set the ChatGPT account password.

        flow=username_password_create，referer=/create-account/password
        """
        s = await self._get_session()
        url = f"{self.BASE_URL}/api/accounts/user/register"
        referer = f"{self.BASE_URL}/create-account/password"
        headers = await self._add_sentinel_headers(
            self._common_headers(referer=referer),
            "username_password_create",
            referer,
            force_refresh=force_refresh_sentinel,
            log_fn=log_fn,
        )
        resp = await s.post(
            url,
            json={"password": password, "username": email},
            headers=headers,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"status": resp.status_code, "text": resp.text}
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        return data

    async def verify_password_email(
        self,
        password: str,
        *,
        force_refresh_sentinel: bool = False,
        log_fn=None,
    ) -> dict:
        """Verify a previously saved password on the existing-account branch."""
        s = await self._get_session()
        referer = f"{self.BASE_URL}/log-in/password"
        headers = await self._add_sentinel_headers(
            self._common_headers(referer=referer),
            "login_password",
            referer,
            force_refresh=force_refresh_sentinel,
            log_fn=log_fn,
        )
        resp = await s.post(
            f"{self.BASE_URL}/api/accounts/password/verify",
            json={"password": password},
            headers=headers,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"status": resp.status_code, "text": resp.text}
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        return data

    async def authorize_password_email(
        self,
        email: str,
        *,
        screen_hint: str,
        referer: str,
        force_refresh_sentinel: bool = False,
        log_fn=None,
    ) -> dict:
        """Select the password connection for signup or existing-account login."""
        s = await self._get_session()
        headers = await self._add_sentinel_headers(
            self._common_headers(referer=referer),
            "authorize_continue",
            referer,
            force_refresh=force_refresh_sentinel,
            log_fn=log_fn,
        )
        resp = await s.post(
            f"{self.BASE_URL}/api/accounts/authorize/continue",
            json={
                "connection": "password",
                "username": {"kind": "email", "value": email},
                "screen_hint": screen_hint,
            },
            headers=headers,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"status": resp.status_code, "text": resp.text}
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        return data

    async def authorize_username_email(
        self,
        email: str,
        *,
        screen_hint: str,
        referer: str,
        force_refresh_sentinel: bool = False,
        log_fn=None,
    ) -> dict:
        """Submit the email form and advance before choosing a sign-up method.

        The official multilingual login-or-sign-up page sends only ``username``
        and ``screen_hint`` for this click.  Password registration is selected
        from the following page via ``/create-account/password``.
        """
        s = await self._get_session()
        headers = await self._add_sentinel_headers(
            self._common_headers(referer=referer),
            "authorize_continue",
            referer,
            force_refresh=force_refresh_sentinel,
            log_fn=log_fn,
        )
        resp = await s.post(
            f"{self.BASE_URL}/api/accounts/authorize/continue",
            json={
                "username": {"kind": "email", "value": email},
                "screen_hint": screen_hint,
            },
            headers=headers,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"status": resp.status_code, "text": resp.text}
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        return data

    async def authorize_existing_email(
        self,
        email: str,
        *,
        force_refresh_sentinel: bool = False,
        log_fn=None,
    ) -> dict:
        """Select the existing-account password-login branch before verify."""
        return await self.authorize_password_email(
            email,
            screen_hint="login",
            referer=f"{self.BASE_URL}/log-in",
            force_refresh_sentinel=force_refresh_sentinel,
            log_fn=log_fn,
        )

    async def authorize_signup_email(
        self,
        email: str,
        *,
        current_url: str = "",
        force_refresh_sentinel: bool = False,
        log_fn=None,
    ) -> dict:
        """Submit the account and advance to the password-method choice page."""
        return await self.authorize_username_email(
            email,
            screen_hint="login_or_signup",
            referer=(
                current_url
                or f"{self.BASE_URL}/api/accounts/authorize"
            ),
            force_refresh_sentinel=force_refresh_sentinel,
            log_fn=log_fn,
        )

    async def send_password_reset_otp(self) -> dict:
        """Enter password recovery and send its email verification code."""
        s = await self._get_session()
        reset_page = f"{self.BASE_URL}/reset-password"
        await s.get(
            reset_page,
            headers={"referer": f"{self.BASE_URL}/log-in/password"},
            allow_redirects=True,
        )
        resp = await s.post(
            f"{self.BASE_URL}/api/accounts/password/send-otp",
            json={},
            headers=self._common_headers(referer=reset_page),
        )
        try:
            data = resp.json()
        except Exception:
            data = {"status": resp.status_code, "text": resp.text}
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        return data

    async def reset_password(
        self,
        password: str,
        *,
        force_refresh_sentinel: bool = False,
        log_fn=None,
    ) -> dict:
        """Set a new password after the reset email OTP has been validated."""
        s = await self._get_session()
        referer = f"{self.BASE_URL}/reset-password/new-password"
        headers = await self._add_sentinel_headers(
            self._common_headers(referer=referer),
            "password_reset",
            referer,
            force_refresh=force_refresh_sentinel,
            log_fn=log_fn,
        )
        resp = await s.post(
            f"{self.BASE_URL}/api/accounts/password/reset",
            json={"password": password},
            headers=headers,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"status": resp.status_code, "text": resp.text}
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        return data

    async def issue_mfa_challenge(
        self,
        factor_id: str,
        *,
        factor_type: str = "totp",
        mfa_request_id: str = "",
    ) -> dict:
        """Issue the selected MFA challenge in the current auth session."""
        s = await self._get_session()
        referer = f"{self.BASE_URL}/mfa-challenge/{factor_id}"
        payload: dict[str, Any] = {
            "id": factor_id,
            "type": factor_type,
            "force_fresh_challenge": False,
        }
        if mfa_request_id:
            payload["mfa_request_id"] = mfa_request_id
        resp = await s.post(
            f"{self.BASE_URL}/api/accounts/mfa/issue_challenge",
            json=payload,
            headers=self._common_headers(referer=referer),
        )
        try:
            data = resp.json()
        except Exception:
            data = {"status": resp.status_code, "text": resp.text}
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        return data

    async def verify_mfa_challenge(
        self,
        factor_id: str,
        code: str,
        *,
        factor_type: str = "totp",
        mfa_request_id: str = "",
    ) -> dict:
        """Verify one existing MFA factor and continue the reauth flow."""
        s = await self._get_session()
        referer = f"{self.BASE_URL}/mfa-challenge/{factor_id}"
        payload: dict[str, Any] = {
            "id": factor_id,
            "type": factor_type,
            "code": code,
        }
        if mfa_request_id:
            payload["mfa_request_id"] = mfa_request_id
        resp = await s.post(
            f"{self.BASE_URL}/api/accounts/mfa/verify",
            json=payload,
            headers=self._common_headers(referer=referer),
        )
        try:
            data = resp.json()
        except Exception:
            data = {"status": resp.status_code, "text": resp.text}
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        return data

    async def add_password_after_reauth(
        self,
        password: str,
        *,
        force_refresh_sentinel: bool = False,
        log_fn=None,
    ) -> dict:
        """Set a password after the post_login_add_password reauth flow."""
        s = await self._get_session()
        referer = f"{self.BASE_URL}/reset-password/new-password"
        headers = await self._add_sentinel_headers(
            self._common_headers(referer=referer),
            "password_reset",
            referer,
            force_refresh=force_refresh_sentinel,
            log_fn=log_fn,
        )
        resp = await s.post(
            f"{self.BASE_URL}/api/accounts/password/add",
            json={"password": password},
            headers=headers,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"status": resp.status_code, "text": resp.text}
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        return data

    async def create_account(
        self,
        name: str,
        birthdate: str,
        *,
        force_refresh_sentinel: bool = False,
        log_fn=None,
    ) -> dict:
        s = await self._get_session()
        url = f"{self.BASE_URL}/api/accounts/create_account"
        referer = f"{self.BASE_URL}/about-you"
        headers = await self._add_sentinel_headers(
            self._common_headers(referer=referer),
            "oauth_create_account",
            referer,
            force_refresh=force_refresh_sentinel,
            log_fn=log_fn,
        )
        resp = await s.post(url, json={"name": name, "birthdate": birthdate}, headers=headers)
        try:
            data = resp.json()
        except Exception:
            data = {"status": resp.status_code, "text": resp.text}
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        return data

    async def close(self) -> None:
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        close_fn = getattr(self.sentinel, "close", None)
        if callable(close_fn):
            try:
                await close_fn()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────
# 同步 OTP：email_provider.fetch_otp
# ─────────────────────────────────────────────────────────────────────
def _fetch_otp_sync(
    email: str,
    refresh_token: str,
    client_id: str = "",
    *,
    timeout: int = 90,
    log_fn: Optional[Callable[[str], None]] = None,
) -> str:
    _log = log_fn or log.info
    try:
        from email_provider import fetch_otp
    except Exception as exc:
        _log(f"[OTP] email_provider 加载失败: {exc}")
        return ""

    try:
        code = fetch_otp(
            email=email,
            refresh_token=refresh_token,
            client_id=client_id or "",
            method="graph",
            timeout=max(30, int(timeout)),
        )
    except Exception as exc:
        _log(f"[OTP] fetch_otp 异常: {type(exc).__name__}: {exc}")
        return ""

    code = (code or "").strip()
    if code:
        _log(f"[OTP] ✓ 拿到 OTP={code}")
    else:
        _log("[OTP] ✗ fetch_otp returned empty")
    return code


# ─────────────────────────────────────────────────────────────────────
# 公共入口
# ─────────────────────────────────────────────────────────────────────
class ChatGPTRegister:
    """注册一个新的 ChatGPT 账号（outlook 邮箱收 OTP）。

    用法：
        bot = ChatGPTRegister({
            "email": "...",
            "password": "...",
            "client_id": "...",
            "refresh_token": "...",
        }, proxy="socks5://...")
        result = bot.register()
    """

    def __init__(
        self,
        outlook_creds: dict,
        *,
        log_fn=None,
        proxy: str = "",
        otp_timeout: int = 90,
        impersonate: str = "firefox144",
        with_password: bool = True,
        password_checkpoint_fn=None,
    ):
        self.outlook = outlook_creds or {}
        self.email = _norm_email(self.outlook.get("email"))
        if not self.email:
            raise ValueError("outlook_creds.email 不能为空")
        self._log_fn = log_fn or log.info
        self.proxy = _normalize_proxy(proxy or self.outlook.get("proxy") or "")
        self.otp_timeout = int(otp_timeout or 90)
        self.impersonate = impersonate or "chrome136"
        self.fingerprint_country = str(
            self.outlook.get("fingerprint_country") or "UNSET"
        ).strip().upper()
        self.language = str(self.outlook.get("language") or "en-US").strip()
        self.timezone_name = str(
            self.outlook.get("timezone") or "UTC"
        ).strip()
        # True: use the username/password signup branch before receiving OTP.
        self.with_password = bool(with_password)
        self.password_confirmed = bool(self.outlook.get("password_confirmed"))
        self.has_staged_password = len(str(self.outlook.get("password") or "")) >= 12
        self.force_password_reset = bool(
            self.outlook.get("force_password_reset")
        )
        self.password_verification_only = bool(
            self.outlook.get("password_verification_only")
        )
        self.password_add_reauth = bool(
            self.outlook.get("password_add_reauth")
        )
        self.reauth_session_token = str(
            self.outlook.get("reauth_session_token") or ""
        ).strip()
        self.reauth_session_cookies = self.outlook.get("reauth_session_cookies")
        self.reauth_device_id = str(
            self.outlook.get("reauth_device_id") or ""
        ).strip()
        self.totp_secret = str(self.outlook.get("totp_secret") or "").strip()
        self._password_checkpoint_fn = password_checkpoint_fn

        # 兼容字段
        self.password = ""
        self.access_token = ""
        self.session_token = ""
        self.device_id = ""

    def _l(self, msg: str) -> None:
        try:
            self._log_fn(msg)
        except Exception:
            log.info(msg)

    def _checkpoint_password(self, password: str, confirmed: bool) -> None:
        callback = self._password_checkpoint_fn
        if not callable(callback) or len(str(password or "")) < 12:
            return
        try:
            callback(str(password), bool(confirmed))
        except Exception as exc:
            self._l(f"  [!] 密码检查点回调失败: {type(exc).__name__}: {exc}")

    async def _read_existing_session(self, auth: OpenAIAuthClient) -> tuple[str, str, dict]:
        """Read a session created by OTP validation or an already-existing account."""
        s = await auth._get_session()
        resp = await s.get(f"{auth.CHATGPT_URL}/api/auth/session")
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        access_token = str(payload.get("accessToken") or payload.get("access_token") or "")
        session_token = str(payload.get("sessionToken") or "")
        if not session_token:
            try:
                for cookie in s.cookies.jar:
                    if cookie.name == "__Secure-next-auth.session-token":
                        session_token = str(cookie.value or "")
                        break
            except Exception:
                pass
        if access_token:
            payload["accessToken"] = access_token
        if session_token:
            payload["sessionToken"] = session_token
        return access_token, session_token, payload

    async def _reset_existing_password(
        self,
        auth: OpenAIAuthClient,
        *,
        password: str,
        client_id: str,
        refresh_token: str,
    ) -> tuple[bool, str]:
        """Reset an existing account to the staged password using email OTP."""
        self._l("  [..] 已保存密码与账号不匹配；启动忘记密码重置流程...")
        try:
            send_result = await auth.send_password_reset_otp()
        except Exception as exc:
            return False, f"password_reset_send_exception: {type(exc).__name__}: {exc}"
        send_code, send_message = auth_error_details(send_result)
        send_status = int(send_result.get("_http_status", 0) or 0)
        if send_code or send_status >= 400 or send_result.get("error"):
            return False, (
                "password_reset_send_failed: "
                f"{send_code or send_status or send_result}"
                + (f" ({send_message})" if send_message else "")
            )

        self._l("  [..] 等待忘记密码验证码...")
        attempted_codes: set[str] = set()
        validate_result: dict = {}
        reset_continue_url = ""
        for otp_attempt in range(8):
            code = await asyncio.to_thread(
                _fetch_otp_sync,
                self.email,
                refresh_token,
                client_id,
                timeout=30 if otp_attempt == 0 else 10,
                log_fn=self._log_fn,
            )
            if not code or code in attempted_codes:
                if otp_attempt < 7:
                    await asyncio.sleep(2)
                continue
            attempted_codes.add(code)
            validate_result = await auth.validate_email_otp(code)
            validate_code, validate_message = auth_error_details(validate_result)
            validate_status = int(validate_result.get("_http_status", 0) or 0)
            if (
                not validate_code
                and validate_status < 400
                and not validate_result.get("error")
            ):
                reset_continue_url, _ = auth_next_step(validate_result)
                break
            self._l(
                "  [!] 忘记密码验证码尚未匹配最新邮件："
                f"{validate_code or validate_status or validate_result}"
                + (f" ({validate_message})" if validate_message else "")
            )
        else:
            return False, "password_reset_otp_failed: 未取得可用的最新验证码"

        s = await auth._get_session()
        target = reset_continue_url or f"{auth.BASE_URL}/reset-password/new-password"
        await s.get(
            urljoin(auth.BASE_URL, target),
            headers={"referer": f"{auth.BASE_URL}/email-verification"},
            allow_redirects=True,
        )
        reset_result: dict = {}
        for reset_attempt in range(2):
            try:
                reset_result = await auth.reset_password(
                    password,
                    force_refresh_sentinel=(reset_attempt > 0),
                    log_fn=self._l,
                )
            except Exception as exc:
                if reset_attempt < 1:
                    await asyncio.sleep(1)
                    continue
                return False, (
                    "password_reset_exception: "
                    f"{type(exc).__name__}: {exc}"
                )
            reset_code, reset_message = auth_error_details(reset_result)
            reset_status = int(reset_result.get("_http_status", 0) or 0)
            if (
                not reset_code
                and reset_status < 400
                and not reset_result.get("error")
            ):
                self.password_confirmed = True
                self._checkpoint_password(password, True)
                self._l("  [+] 忘记密码重置成功；将用同一密码重新登录")
                return True, ""
            if reset_attempt < 1:
                await asyncio.sleep(1)
                continue
            return False, (
                "password_reset_failed: "
                f"{reset_code or reset_status or reset_result}"
                + (f" ({reset_message})" if reset_message else "")
            )
        return False, "password_reset_failed: unknown"

    async def _complete_password_add_reauth(
        self,
        auth: OpenAIAuthClient,
        *,
        auth_result: dict[str, Any],
        password: str,
    ) -> tuple[bool, str]:
        """Finish MFA and add a password through OpenAI's current reauth flow."""
        next_url, next_page_type = auth_next_step(auth_result)
        if auth_step_requires_mfa(next_url, next_page_type):
            factor_id, factor_type, mfa_request_id = auth_mfa_details(auth_result)
            if not factor_id:
                return False, "password_add_mfa_factor_missing"
            if factor_type != "totp":
                return False, f"password_add_mfa_factor_unsupported: {factor_type}"
            if not self.totp_secret:
                return False, "password_add_totp_secret_missing"
            issue_result = await auth.issue_mfa_challenge(
                factor_id,
                factor_type=factor_type,
                mfa_request_id=mfa_request_id,
            )
            issue_code, issue_message = auth_error_details(issue_result)
            issue_status = int(issue_result.get("_http_status", 0) or 0)
            if issue_code or issue_status >= 400 or issue_result.get("error"):
                return False, (
                    "password_add_mfa_issue_failed: "
                    f"{issue_code or issue_status or issue_result}"
                    + (f" ({issue_message})" if issue_message else "")
                )
            from hidemyemail_generator.openai_mfa import generate_totp

            verify_result: dict[str, Any] = {}
            for mfa_attempt in range(2):
                remaining = 30 - (time.time() % 30)
                if remaining < 3:
                    await asyncio.sleep(remaining + 0.25)
                verify_result = await auth.verify_mfa_challenge(
                    factor_id,
                    generate_totp(self.totp_secret),
                    factor_type=factor_type,
                    mfa_request_id=mfa_request_id,
                )
                verify_code, verify_message = auth_error_details(verify_result)
                verify_status = int(verify_result.get("_http_status", 0) or 0)
                if (
                    not verify_code
                    and verify_status < 400
                    and not verify_result.get("error")
                ):
                    break
                if mfa_attempt >= 1:
                    return False, (
                        "password_add_mfa_verify_failed: "
                        f"{verify_code or verify_status or verify_result}"
                        + (f" ({verify_message})" if verify_message else "")
                    )
                await asyncio.sleep(min(31, max(1, remaining + 0.25)))
            auth_result = verify_result
            next_url, next_page_type = auth_next_step(auth_result)

        password_path = urlparse(next_url).path.rstrip("/").lower()
        if next_url and password_path not in {
            "/reset-password/new-password",
            "/reset-password",
        }:
            return False, (
                "password_add_reauth_unexpected_step: "
                f"{next_page_type or next_url or auth_result}"
            )
        s = await auth._get_session()
        target = next_url or "/reset-password/new-password"
        page_response = await s.get(
            urljoin(auth.BASE_URL, target),
            headers={"referer": f"{auth.BASE_URL}/mfa-challenge"},
            allow_redirects=True,
        )
        page_status = int(getattr(page_response, "status_code", 0) or 0)
        if page_status >= 400:
            return False, f"password_add_page_failed: {page_status}"
        add_result: dict[str, Any] = {}
        for add_attempt in range(2):
            add_result = await auth.add_password_after_reauth(
                password,
                force_refresh_sentinel=(add_attempt > 0),
                log_fn=self._l,
            )
            add_code, add_message = auth_error_details(add_result)
            add_status = int(add_result.get("_http_status", 0) or 0)
            if not add_code and add_status < 400 and not add_result.get("error"):
                self.password_confirmed = True
                self._l("  [+] 当前登录复核流程已添加密码；正在创建新会话验证")
                return True, ""
            if add_attempt >= 1:
                return False, (
                    "password_add_reauth_failed: "
                    f"{add_code or add_status or add_result}"
                    + (f" ({add_message})" if add_message else "")
                )
            await asyncio.sleep(1)
        return False, "password_add_reauth_failed: unknown"

    def _recovered_success(
        self,
        *,
        password: str,
        password_set: bool = False,
        access_token: str,
        session_token: str,
        session_json: dict,
        name: str,
        birthdate: str,
        reason: str,
        session_cookies: list[dict[str, Any]] | None = None,
    ) -> dict:
        self.access_token = access_token
        self.session_token = session_token
        self._l(f"  ✓ 认证会话恢复成功 reason={reason}")
        return {
            "email": self.email,
            "password": password if password_set else "",
            "session_token": session_token,
            "session_json": session_json,
            "access_token": access_token,
            "device_id": self.device_id,
            "session_cookies": list(session_cookies or []),
            "impersonate": self.impersonate,
            "status": "success",
            "password_set": password_set,
            "raw": {
                "name": name,
                "birthdate": birthdate,
                "password_set": password_set,
                "recovered_existing_session": reason,
            },
        }

    def register(self) -> dict:
        """跑完整协议注册流程（同步入口，内部 asyncio）。"""
        self._l("=" * 60)
        self._l(f"  [register/outlook-auth] {self.email}")
        self._l("=" * 60)

        client_id = _norm_email(self.outlook.get("client_id"))
        refresh_token = _norm_email(self.outlook.get("refresh_token"))
        if not refresh_token:
            return self._failed("missing_refresh_token: 没有 outlook refresh_token，无法收 OTP")

        supplied_password = str(self.outlook.get("password") or "")
        password = supplied_password if len(supplied_password) >= 12 else random_password()
        name = random_name()
        age = random_age()
        birthdate = birthdate_from_age(age)
        self.password = password
        if self.with_password:
            # Persist the exact candidate before the first POST.  An ambiguous
            # response or process restart must reuse it instead of generating a
            # different password for an account the server may have created.
            self._checkpoint_password(
                password,
                self.password_confirmed and not self.password_verification_only,
            )
        self._l(
            f"  身份: {name}  年龄: {age}  密码: {password[:4]}****  "
            f"proxy={'yes' if self.proxy else 'no'}  with_password={self.with_password}"
        )

        try:
            result = asyncio.run(
                self._register_async(
                    password=password,
                    name=name,
                    birthdate=birthdate,
                    client_id=client_id,
                    refresh_token=refresh_token,
                )
            )
        except RuntimeError as exc:
            # 已有 running loop（极少见）：丢到新线程跑
            if "asyncio.run()" in str(exc) or "running event loop" in str(exc).lower():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(
                        lambda: asyncio.run(
                            self._register_async(
                                password=password,
                                name=name,
                                birthdate=birthdate,
                                client_id=client_id,
                                refresh_token=refresh_token,
                            )
                        )
                    )
                    result = fut.result()
            else:
                return self._failed(f"asyncio RuntimeError: {exc}")
        except Exception as exc:
            return self._failed(f"{type(exc).__name__}: {exc}")

        return result

    async def _register_async(
        self,
        *,
        password: str,
        name: str,
        birthdate: str,
        client_id: str,
        refresh_token: str,
    ) -> dict:
        t0 = time.time()

        def _ts() -> str:
            return f"[{time.time() - t0:.1f}s]"

        auth = OpenAIAuthClient(
            impersonate=self.impersonate,
            proxy=self.proxy,
            language=self.language,
            timezone_name=self.timezone_name,
            initial_session_token=(
                self.reauth_session_token if self.password_add_reauth else ""
            ),
            initial_session_cookies=(
                self.reauth_session_cookies if self.password_add_reauth else None
            ),
            device_id=(
                self.reauth_device_id if self.password_add_reauth else ""
            ),
        )

        async def submit_signup_password(
            *,
            ensure_password_page: bool,
            timing_label: str,
        ) -> dict:
            if ensure_password_page:
                self._l(f"  {_ts()} [..] 进入账号密码注册页...")
                password_page = await auth.navigate_password_registration()
                password_page_status = int(password_page.get("status", 0) or 0)
                if password_page_status >= 400:
                    raise RuntimeError(
                        f"password_page_failed: {password_page_status}"
                    )

            self._l(
                f"  {_ts()} [..] {timing_label}提交账号密码 "
                "(username_password_create)..."
            )
            password_result: dict = {}
            # A registration POST is deliberately single-shot. Replaying the
            # same password after a structured auth error cannot repair the
            # server state and may trigger temporary login restrictions.
            for password_attempt in range(1):
                try:
                    password_result = await auth.register_password_email(
                        self.email,
                        password,
                        force_refresh_sentinel=(password_attempt > 0),
                        log_fn=self._l,
                    )
                except Exception as exc:
                    self._l(
                        f"  [!] account_password_register 异常: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    raise RuntimeError(
                        "account_password_register_exception: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc

                if not isinstance(password_result, dict):
                    password_result = {
                        "error": {
                            "code": "invalid_response",
                            "message": str(password_result),
                        }
                    }
                error_code, error_message = auth_error_details(password_result)
                http_status = int(password_result.get("_http_status", 0) or 0)
                if (
                    not error_code
                    and http_status < 400
                    and not password_result.get("error")
                ):
                    self.password_confirmed = True
                    self._checkpoint_password(password, True)
                    return password_result
                if error_code in ACCOUNT_UNUSABLE_CODES:
                    raise RuntimeError(f"account_unusable: {error_code}")
                raise RuntimeError(
                    "account_password_register_failed: "
                    f"{error_code or http_status or password_result}"
                    + (f" ({error_message})" if error_message else "")
                )
            raise RuntimeError("account_password_register_failed: unknown")

        try:
            # 1. 初始化认证会话。
            password_signup_first = bool(
                self.with_password
                and not self.password_confirmed
                and not self.force_password_reset
                and not self.password_verification_only
            )
            self._l(
                f"  {_ts()} [..] "
                + (
                    "初始化 OpenAI 密码注册会话（等待选择 password connection）..."
                    if password_signup_first
                    else "初始化 OpenAI 页面 (login_hint=email)..."
                )
            )
            since = time.time()
            await auth.share_session_with_sentinel()
            try:
                if self.has_staged_password and (
                    self.password_confirmed
                    or self.force_password_reset
                    or self.password_verification_only
                ):
                    if self.password_add_reauth:
                        init = await auth.init_page_email(
                            self.email,
                            prefer_login=True,
                            post_login_add_password=True,
                        )
                    else:
                        init = await auth.init_page_email(
                            self.email,
                            prefer_login=True,
                        )
                else:
                    init = await auth.init_page_email(
                        self.email,
                        prefer_password_signup=password_signup_first,
                    )
            except Exception as exc:
                return self._failed(f"init_page_email: {type(exc).__name__}: {exc}")

            self.device_id = init.get("device_id") or auth.device_id
            set_cookies = getattr(auth.sentinel, "set_cookies", None)
            if callable(set_cookies):
                set_cookies(init.get("cookies") or {})
            self._l(
                f"  {_ts()} [+] 设备ID: {(self.device_id or '')[:12]}... "
                f"auth_page={init.get('page_path') or '-'}"
            )

            # A retried signup may already belong to an existing account even
            # when the redirect still reports another page. Explicitly select
            # the login branch before password verify; never submit another
            # username_password_create password for a staged account.
            init_page_path = str(init.get("page_path") or "").rstrip("/").lower()
            password_page_ready = False
            password_after_otp_required = bool(
                password_signup_first
                and init_page_path == "/email-verification"
            )
            password_signup_chooser_paths = {
                "/api/accounts/authorize",
                "/log-in-or-create-account",
            }
            if password_signup_first and init_page_path in password_signup_chooser_paths:
                s = await auth._get_session()
                authorize_page_url = str(init.get("page_url") or "").strip()
                if not authorize_page_url:
                    authorize_page_url = urljoin(auth.BASE_URL, init_page_path)
                self._l(
                    f"  {_ts()} [..] 认证会话正在等待账号方式；"
                    "输入邮箱账号并跳转下一步..."
                )
                try:
                    signup_result = await auth.authorize_signup_email(
                        self.email,
                        current_url=authorize_page_url,
                        log_fn=self._l,
                    )
                except Exception as exc:
                    return self._failed(
                        "account_password_signup_authorize_exception: "
                        f"{type(exc).__name__}: {exc}"
                    )
                signup_code, signup_message = auth_error_details(signup_result)
                signup_status = int(signup_result.get("_http_status", 0) or 0)
                if signup_code or signup_status >= 400 or signup_result.get("error"):
                    return self._failed(
                        "account_password_signup_authorize_failed: "
                        f"{signup_code or signup_status or signup_result}"
                        + (f" ({signup_message})" if signup_message else "")
                    )
                signup_url, signup_page_type = auth_next_step(signup_result)
                self._l(
                    f"  {_ts()} [state] password signup next page="
                    f"{signup_page_type or '-'} "
                    f"path={urlparse(signup_url).path or '-'}"
                )
                password_step_selected = auth_step_requires_password(
                    signup_url,
                    signup_page_type,
                )
                otp_step_selected = auth_step_requires_email_otp(
                    signup_url,
                    signup_page_type,
                )
                if not password_step_selected and not otp_step_selected:
                    return self._failed(
                        "account_password_signup_step_required: "
                        f"{signup_page_type or signup_url or signup_result}"
                    )
                if signup_url:
                    selected_page_response = await s.get(
                        urljoin(auth.BASE_URL, signup_url),
                        headers={"referer": authorize_page_url},
                        allow_redirects=True,
                    )
                    selected_page_status = int(
                        getattr(selected_page_response, "status_code", 0) or 0
                    )
                    if selected_page_status >= 400:
                        return self._failed(
                            f"password_signup_page_failed: {selected_page_status}"
                        )
                if password_step_selected:
                    password_page_ready = True
                    init_page_path = "/create-account/password"
                    self._l(
                        f"  {_ts()} [+] 邮箱下一步已进入密码注册页；"
                        "现在输入密码"
                    )
                else:
                    # The current OpenAI frontend exposes the password fallback
                    # conditionally. A bare email_otp_verification response is
                    # authoritative: honor OTP first instead of assuming that a
                    # client-side GET can change the backend auth step.
                    password_after_otp_required = True
                    init_page_path = "/email-verification"
                    since = time.time()
                    self._l(
                        f"  {_ts()} [state] 服务端要求先验证邮箱；"
                        "先完成 OTP，再按服务端下一步或已认证 Session 设置同一密码"
                    )

            should_try_existing_login = self.with_password and (
                self.password_confirmed
                or self.force_password_reset
                or self.password_verification_only
                or init_page_path in {"/log-in/password", "/login/password"}
            )
            if should_try_existing_login:
                s = await auth._get_session()
                self._l(
                    f"  {_ts()} [..] 检测到已保存密码；"
                    "先选择现有账号登录分支并复用该密码..."
                )
                if (
                    self.password_add_reauth
                    and init_page_path == "/email-verification"
                ):
                    # post_login_add_password=true with login_hint already
                    # selected the account and emitted its reauth OTP. Posting
                    # authorize/continue again duplicates the login request and
                    # is rate-limited by the current auth service.
                    authorize_result = {
                        "continue_url": "/email-verification",
                        "page": {"type": "email_otp_verification"},
                        "_http_status": 200,
                    }
                    self._l(
                        f"  {_ts()} [state] 添加密码复核已进入邮箱验证码页；"
                        "跳过重复提交账号"
                    )
                else:
                    try:
                        authorize_result = await auth.authorize_existing_email(
                            self.email,
                            log_fn=self._l,
                        )
                    except Exception as exc:
                        return self._failed(
                            "account_login_authorize_exception: "
                            f"{type(exc).__name__}: {exc}"
                        )
                authorize_code, authorize_message = auth_error_details(
                    authorize_result
                )
                authorize_status = int(
                    authorize_result.get("_http_status", 0) or 0
                )
                if (
                    authorize_code
                    or authorize_status >= 400
                    or authorize_result.get("error")
                ):
                    return self._failed(
                        "account_login_authorize_failed: "
                        f"{authorize_code or authorize_status or authorize_result}"
                        + (
                            f" ({authorize_message})"
                            if authorize_message
                            else ""
                        )
                    )
                authorize_url, authorize_page_type = auth_next_step(
                    authorize_result
                )
                self._l(
                    f"  {_ts()} [state] existing login next page="
                    f"{authorize_page_type or '-'} "
                    f"path={urlparse(authorize_url).path or '-'}"
                )
                authorize_path = urlparse(authorize_url).path.rstrip("/").lower()
                authorize_requires_email_otp = auth_step_requires_email_otp(
                    authorize_url,
                    authorize_page_type,
                )
                if (
                    self.password_verification_only
                    and not self.password_add_reauth
                    and authorize_requires_email_otp
                ):
                    # The current auth UI exposes “Continue with password” as a
                    # plain link from the email-code page.  A verifier must take
                    # that link; validating the email code would select a
                    # passwordless login and can advance directly to MFA without
                    # ever testing the saved password.
                    password_fallback_url = "/log-in/password"
                    password_page_response = await s.get(
                        urljoin(auth.BASE_URL, password_fallback_url),
                        headers={
                            "referer": urljoin(auth.BASE_URL, authorize_url)
                        },
                        allow_redirects=True,
                    )
                    password_page_status = int(
                        getattr(password_page_response, "status_code", 0) or 0
                    )
                    if password_page_status >= 400:
                        # Passwordless accounts expose no password fallback.
                        # Restart through ChatGPT's current Settings → Account
                        # reauth contract (post_login_add_password=true).
                        return self._failed(
                            "account_password_add_reauth_required"
                        )
                    authorize_url = password_fallback_url
                    authorize_page_type = "login_password"
                    authorize_path = password_fallback_url
                    self._l(
                        f"  {_ts()} [state] 已从邮箱验证码页切换为密码验证页"
                    )
                elif authorize_url:
                    await s.get(
                        urljoin(auth.BASE_URL, authorize_url),
                        headers={"referer": f"{auth.BASE_URL}/log-in"},
                        allow_redirects=True,
                    )
                if (
                    str(authorize_page_type or "").strip().casefold()
                    == "email_otp_verification"
                    or authorize_path == "/email-verification"
                ):
                    self._l(
                        f"  {_ts()} [..] 登录分支要求先验证邮箱；等待最新验证码..."
                    )
                    login_otp_result: dict = {}
                    attempted_login_otps: set[str] = set()
                    login_otp_validation_attempts = 0
                    for login_otp_fetch_attempt in range(1, 7):
                        login_otp_wait = (
                            self.otp_timeout
                            if login_otp_fetch_attempt == 1
                            else min(self.otp_timeout, 45)
                        )
                        login_otp = await asyncio.to_thread(
                            _fetch_otp_sync,
                            self.email,
                            refresh_token,
                            client_id,
                            timeout=login_otp_wait,
                            log_fn=self._log_fn,
                        )
                        if not login_otp:
                            return self._failed(
                                f"login_otp_timeout: {login_otp_wait}s 内未收到验证码"
                            )
                        if login_otp in attempted_login_otps:
                            self._l(
                                f"  {_ts()} [skip] 登录验证码邮件重复；"
                                "继续等待重发后的新验证码"
                            )
                            continue
                        attempted_login_otps.add(login_otp)
                        login_otp_validation_attempts += 1
                        login_otp_result = await auth.validate_email_otp(login_otp)
                        login_otp_code, login_otp_message = auth_error_details(
                            login_otp_result
                        )
                        login_otp_status = int(
                            login_otp_result.get("_http_status", 0) or 0
                        )
                        if (
                            not login_otp_code
                            and login_otp_status < 400
                            and not login_otp_result.get("error")
                        ):
                            break
                        self._l(
                            f"  {_ts()} [retry] 登录验证码校验未通过 "
                            f"attempt={login_otp_validation_attempts}/3 "
                            f"code={login_otp_code or '-'} "
                            f"status={login_otp_status or '-'} "
                            f"message={login_otp_message or '-'}"
                        )
                        if (
                            login_otp_validation_attempts >= 3
                            or not otp_error_is_retryable(
                                login_otp_code,
                                login_otp_message,
                            )
                        ):
                            return self._failed(
                                "account_login_otp_failed: "
                                f"{login_otp_code or login_otp_status or login_otp_result}"
                                + (
                                    f" ({login_otp_message})"
                                    if login_otp_message
                                    else ""
                                )
                            )
                        resend_result = await auth.send_email_otp()
                        resend_code, resend_message = auth_error_details(resend_result)
                        resend_status = int(
                            resend_result.get("_http_status", 0) or 0
                        )
                        if resend_code or resend_status >= 400:
                            return self._failed(
                                "account_login_otp_resend_failed: "
                                f"{resend_code or resend_status or resend_result}"
                                + (
                                    f" ({resend_message})"
                                    if resend_message
                                    else ""
                                )
                            )
                    else:
                        return self._failed(
                            "account_login_otp_failed: 未取得可校验的新验证码"
                        )
                    authorize_url, authorize_page_type = auth_next_step(
                        login_otp_result
                    )
                    self._l(
                        f"  {_ts()} [state] login OTP next page="
                        f"{authorize_page_type or '-'} "
                        f"path={urlparse(authorize_url).path or '-'}"
                    )
                    if self.password_add_reauth:
                        add_ok, add_error = await self._complete_password_add_reauth(
                            auth,
                            auth_result=login_otp_result,
                            password=password,
                        )
                        if add_ok:
                            return self._failed(
                                "account_password_add_completed_retry_login"
                            )
                        return self._failed(add_error)
                    if authorize_url:
                        await s.get(
                            urljoin(auth.BASE_URL, authorize_url),
                            headers={
                                "referer": f"{auth.BASE_URL}/email-verification"
                            },
                            allow_redirects=True,
                        )
                    if auth_step_is_direct_oauth(
                        authorize_url,
                        authorize_page_type,
                    ):
                        for session_attempt in range(3):
                            access_token, session_token, session_json = (
                                await self._read_existing_session(auth)
                            )
                            if access_token:
                                self.password_confirmed = True
                                self._checkpoint_password(password, True)
                                return self._recovered_success(
                                    password=password,
                                    password_set=True,
                                    access_token=access_token,
                                    session_token=session_token,
                                    session_json=session_json,
                                    name=name,
                                    birthdate=birthdate,
                                    reason="saved_password_email_otp",
                                    session_cookies=session_cookie_records(s),
                                )
                            if session_attempt < 2:
                                await asyncio.sleep(0.5 * (session_attempt + 1))
                        return self._failed(
                            "account_login_otp_session_missing: 邮箱验证已通过，"
                            "但 OAuth callback 未返回 Session"
                        )
                if self.force_password_reset:
                    reset_ok, reset_error = await self._reset_existing_password(
                        auth,
                        password=password,
                        client_id=client_id,
                        refresh_token=refresh_token,
                    )
                    if reset_ok:
                        return self._failed(
                            "account_password_reset_completed_retry_login"
                        )
                    return self._failed(reset_error)
                login_result: dict = {}
                for login_attempt in range(2):
                    try:
                        login_result = await auth.verify_password_email(
                            password,
                            force_refresh_sentinel=(login_attempt > 0),
                            log_fn=self._l,
                        )
                    except Exception as exc:
                        if login_attempt < 1:
                            await asyncio.sleep(1)
                            continue
                        return self._failed(
                            "account_password_login_exception: "
                            f"{type(exc).__name__}: {exc}"
                        )
                    login_code, login_message = auth_error_details(login_result)
                    login_status = int(login_result.get("_http_status", 0) or 0)
                    if not login_code and login_status < 400 and not login_result.get(
                        "error"
                    ):
                        break
                    if login_attempt < 1:
                        await asyncio.sleep(1)
                        continue
                    if login_code == "invalid_username_or_password":
                        reset_ok, reset_error = await self._reset_existing_password(
                            auth,
                            password=password,
                            client_id=client_id,
                            refresh_token=refresh_token,
                        )
                        if reset_ok:
                            return self._failed(
                                "account_password_reset_completed_retry_login"
                            )
                        return self._failed(reset_error)
                    return self._failed(
                        "account_password_login_failed: "
                        f"{login_code or login_status or login_result}"
                        + (f" ({login_message})" if login_message else "")
                    )

                self.password_confirmed = True
                self._checkpoint_password(password, True)
                login_continue_url, login_page_type = auth_next_step(login_result)
                self._l(
                    f"  {_ts()} [state] password login next page="
                    f"{login_page_type or '-'} "
                    f"path={urlparse(login_continue_url).path or '-'}"
                )
                if auth_step_requires_mfa(login_continue_url, login_page_type):
                    # Reaching MFA is positive proof that the preceding password
                    # was accepted.  A clean verifier deliberately stops here so
                    # the existing authenticated Session can finish credential
                    # setup without attempting another password or OTP login.
                    return self._failed("account_password_verified_mfa_required")
                if login_continue_url:
                    await s.get(
                        urljoin(auth.BASE_URL, login_continue_url),
                        headers={"referer": f"{auth.BASE_URL}/log-in/password"},
                        allow_redirects=True,
                    )
                for session_attempt in range(3):
                    access_token, session_token, session_json = (
                        await self._read_existing_session(auth)
                    )
                    if access_token:
                        return self._recovered_success(
                            password=password,
                            password_set=True,
                            access_token=access_token,
                            session_token=session_token,
                            session_json=session_json,
                            name=name,
                            birthdate=birthdate,
                            reason="saved_password_login",
                            session_cookies=session_cookie_records(s),
                        )
                    if session_attempt < 2:
                        await asyncio.sleep(0.5 * (session_attempt + 1))
                return self._failed(
                    "account_password_login_session_missing: 密码已验证，"
                    "但 OAuth callback 未返回 Session"
                )

            # 2. Submit the password first only when the server actually selected
            # the password page. Otherwise honor the OTP page and add the same
            # staged password after authentication.
            password_set = False
            if self.with_password and not password_after_otp_required:
                s = await auth._get_session()
                try:
                    password_result = await submit_signup_password(
                        ensure_password_page=not password_page_ready,
                        timing_label="验证码前",
                    )
                except RuntimeError as exc:
                    return self._failed(str(exc))
                password_set = True
                self._l(f"  {_ts()} [+] 账号密码已在接收验证码前设置")

                otp_send_url, password_next_type = auth_next_step(password_result)
                self._l(
                    f"  {_ts()} [state] password next page="
                    f"{password_next_type or '-'} "
                    f"path={urlparse(otp_send_url).path or '-'}"
                )
                since = time.time()
                if otp_send_url:
                    send_response = await s.get(
                        otp_send_url,
                        headers={
                            "referer": f"{auth.BASE_URL}/create-account/password"
                        },
                        allow_redirects=True,
                    )
                    send_status = int(
                        getattr(send_response, "status_code", 0) or 0
                    )
                    if send_status >= 400:
                        return self._failed(f"otp_send_failed: {send_status}")
                else:
                    send_result = await auth.send_email_otp()
                    send_code, send_message = auth_error_details(send_result)
                    send_status = int(send_result.get("_http_status", 0) or 0)
                    if send_code or send_status >= 400:
                        return self._failed(
                            f"otp_send_failed: {send_code or send_status}"
                            + (f" ({send_message})" if send_message else "")
                        )
                self._l(f"  {_ts()} [+] 密码注册验证码已发送，开始接收")

            # 3. 获取并校验 OTP；错误或过期时按服务端当前会话重新发码。
            validate_result: dict = {}
            code = ""
            for otp_attempt in range(1, 4):
                self._l(
                    f"  {_ts()} [..] 等待邮箱验证码 "
                    f"(第 {otp_attempt}/3 次, timeout={self.otp_timeout}s)..."
                )
                code = await asyncio.to_thread(
                    _fetch_otp_sync,
                    self.email,
                    refresh_token,
                    client_id,
                    timeout=self.otp_timeout,
                    log_fn=self._log_fn,
                )
                # since 仅作日志；email_provider 自己做时间窗口
                _ = since
                if not code:
                    return self._failed(
                        f"otp_timeout: {self.otp_timeout}s 内未收到验证码"
                    )

                self._l(f"  {_ts()} [..] 提交邮箱验证码...")
                validate_result = await auth.validate_email_otp(code)
                err_code, err_msg = auth_error_details(validate_result)
                if not err_code and not (
                    isinstance(validate_result, dict) and "error" in validate_result
                ):
                    break
                if err_code in ACCOUNT_UNUSABLE_CODES:
                    return self._failed(f"account_unusable: {err_code}")
                if otp_attempt >= 3 or not otp_error_is_retryable(err_code, err_msg):
                    return self._failed(
                        f"otp_validate_failed: {err_code or validate_result}"
                        + (f" ({err_msg})" if err_msg else "")
                    )

                self._l(
                    f"  {_ts()} [!] 验证码无效或过期，重新发送 "
                    f"({otp_attempt}/2)..."
                )
                resend_result = await auth.send_email_otp()
                resend_code, resend_msg = auth_error_details(resend_result)
                resend_status = int(resend_result.get("_http_status", 0) or 0)
                if resend_code or resend_status >= 400:
                    return self._failed(
                        f"otp_resend_failed: {resend_code or resend_status}"
                        + (f" ({resend_msg})" if resend_msg else "")
                    )
                since = time.time()
            self._l(f"  {_ts()} [+] 邮箱验证码已通过（账号尚未完成）")

            continue_url, page_type = auth_next_step(validate_result)
            if (
                self.with_password
                and not password_set
                and auth_step_requires_password(continue_url, page_type)
            ):
                s = await auth._get_session()
                if continue_url:
                    password_page_response = await s.get(
                        urljoin(auth.BASE_URL, continue_url),
                        headers={"referer": f"{auth.BASE_URL}/email-verification"},
                        allow_redirects=True,
                    )
                    password_page_status = int(
                        getattr(password_page_response, "status_code", 0) or 0
                    )
                    if password_page_status >= 400:
                        return self._failed(
                            f"password_page_failed: {password_page_status}"
                        )
                try:
                    password_result = await submit_signup_password(
                        ensure_password_page=False,
                        timing_label="邮箱验证后立即",
                    )
                except RuntimeError as exc:
                    return self._failed(str(exc))
                password_set = True
                self._l(f"  {_ts()} [+] 服务端密码步骤已确认账号密码")
                continue_url, page_type = auth_next_step(password_result)
                self._l(
                    f"  {_ts()} [state] password-after-OTP next page="
                    f"{page_type or '-'} "
                    f"path={urlparse(continue_url).path or '-'}"
                )
            if page_type.lower() == "external_url" and not continue_url:
                return self._failed(
                    "otp_external_url_missing: 服务端要求进入外部 OAuth，"
                    "但响应中没有可跟随 URL"
                )
            self._l(
                f"  {_ts()} [state] OTP next page={page_type or '-'} "
                f"path={urlparse(continue_url).path or '-'}"
            )

            s = await auth._get_session()
            # 4. Follow the server-selected step.  An external OpenAI OAuth
            # callback can finish an existing/partially-created account and
            # consume the auth step.  Read that Session before attempting
            # create_account; posting create_account afterwards produces 409
            # invalid_state and used to trigger a pointless second OTP cycle.
            about_you_url = continue_url or f"{auth.BASE_URL}/about-you"
            is_session_callback = auth_step_is_direct_oauth(
                about_you_url,
                page_type,
            )
            if about_you_url and (
                is_session_callback
                or "about-you" in about_you_url
                or "about_you" in about_you_url
                or not password_set
            ):
                self._l(
                    f"  {_ts()} [..] "
                    + (
                        "完成 OAuth callback 并读取 Session..."
                        if is_session_callback
                        else "导航到 about-you..."
                    )
                )
                about_response = await s.get(
                    about_you_url,
                    headers={
                        "referer": (
                            f"{auth.BASE_URL}/create-account/password"
                            if password_set
                            else f"{auth.BASE_URL}/email-verification"
                        )
                    },
                )
                final_url = str(getattr(about_response, "url", "") or "")
                self._l(
                    f"  {_ts()} [+] 已访问认证下一步 "
                    f"status={getattr(about_response, 'status_code', '-')} "
                    f"path={urlparse(final_url).path or '-'}"
                )
                if is_session_callback:
                    access_token = ""
                    session_token = ""
                    session_json: dict = {}
                    for session_attempt in range(3):
                        access_token, session_token, session_json = (
                            await self._read_existing_session(auth)
                        )
                        if access_token:
                            return self._recovered_success(
                                password=password,
                                password_set=password_set,
                                access_token=access_token,
                                session_token=session_token,
                                session_json=session_json,
                                name=name,
                                birthdate=birthdate,
                                reason="otp_oauth_callback_session",
                                session_cookies=session_cookie_records(s),
                            )
                        if session_attempt < 2:
                            await asyncio.sleep(0.5 * (session_attempt + 1))
                    return self._failed(
                        "otp_callback_session_missing: OAuth callback 已完成，"
                        "但未返回 Session；为避免重复验证码，已停止本次注册"
                    )
                final = urlparse(final_url)
                if final.hostname == urlparse(auth.CHATGPT_URL).hostname:
                    return self._failed(
                        "auth_navigation_unexpected: 未进入 about-you，"
                        f"实际跳转到 {final.path or '/'}"
                    )
            elif password_set and about_you_url and "about-you" not in about_you_url:
                # 设密后 continue 可能不是 about-you，仍访问一次标准 about-you 页
                self._l(f"  {_ts()} [..] 访问 about-you 页面...")
                about_response = await s.get(
                    f"{auth.BASE_URL}/about-you",
                    headers={"referer": f"{auth.BASE_URL}/create-account/password"},
                )
                self._l(
                    f"  {_ts()} [+] 已访问 about-you "
                    f"status={getattr(about_response, 'status_code', '-')} "
                    f"path={urlparse(str(getattr(about_response, 'url', '') or '')).path or '-'}"
                )

            # 5. 创建账号（带 sentinel）
            self._l(f"  {_ts()} [..] 创建账号 (sentinel oauth_create_account)...")
            create_result: dict = {}
            create_ok = False
            for create_attempt in range(3):
                try:
                    create_result = await auth.create_account(
                        name,
                        birthdate,
                        force_refresh_sentinel=(create_attempt > 0),
                        log_fn=self._l,
                    )
                except Exception as exc:
                    self._l(f"  [!] create_account 异常: {type(exc).__name__}: {exc}")
                    if create_attempt < 2:
                        await asyncio.sleep(2)
                        continue
                    return self._failed(f"create_account_exception: {type(exc).__name__}: {exc}")
                if isinstance(create_result, dict) and "error" in create_result:
                    err_code, err_msg = auth_error_details(create_result)
                    http_st = create_result.get("_http_status", "")
                    self._l(
                        f"  [!] create_account error code={err_code} http={http_st} "
                        f"msg={str(err_msg)[:160]}"
                    )
                    if err_code in ACCOUNT_UNUSABLE_CODES:
                        return self._failed(f"account_unusable: {err_code}")
                    if err_code == "registration_disallowed" and create_attempt < 2:
                        self._l(f"  [!] registration_disallowed, 刷新 sentinel 重试 ({create_attempt + 1}/3)")
                        await asyncio.sleep(2)
                        continue
                    if err_code in {
                        "invalid_auth_step",
                        "user_already_exists",
                        "username_already_exists",
                    }:
                        access_token, session_token, session_json = (
                            await self._read_existing_session(auth)
                        )
                        if access_token:
                            return self._recovered_success(
                                password=password,
                                password_set=password_set,
                                access_token=access_token,
                                session_token=session_token,
                                session_json=session_json,
                                name=name,
                                birthdate=birthdate,
                                reason=err_code,
                                session_cookies=session_cookie_records(s),
                            )
                    return self._failed(
                        f"create_account_failed: {err_code or create_result}"
                        + (f" ({err_msg})" if err_msg else "")
                    )
                create_ok = True
                break

            if not create_ok:
                return self._failed("create_account_failed: unknown")
            self._l(f"  {_ts()} [+] 账号创建成功")

            # 6. OAuth 回调 + session
            access_token = ""
            session_token = ""
            session_json: dict = {}
            continue_url = ""
            if isinstance(create_result, dict):
                continue_url, _create_page_type = auth_next_step(create_result)
            if continue_url:
                self._l(f"  {_ts()} [..] OAuth 回调...")
                s = await auth._get_session()
                cb_resp = await s.get(continue_url, allow_redirects=True)
                self._l(f"  {_ts()} [+] 回调状态: {cb_resp.status_code}")

                self._l(f"  {_ts()} [..] 获取 session...")
                sess_resp = await s.get(f"{auth.CHATGPT_URL}/api/auth/session")
                try:
                    sess_data = sess_resp.json()
                except Exception:
                    sess_data = {}
                if isinstance(sess_data, dict):
                    session_json = dict(sess_data)
                access_token = (sess_data.get("accessToken") or sess_data.get("access_token") or "")
                session_token = sess_data.get("sessionToken") or ""
                if not session_token:
                    try:
                        for cookie in s.cookies.jar:
                            if cookie.name == "__Secure-next-auth.session-token":
                                session_token = cookie.value
                                break
                    except Exception:
                        pass
                if access_token:
                    session_json["accessToken"] = access_token
                if session_token:
                    session_json["sessionToken"] = session_token
                if access_token:
                    self._l(f"  {_ts()} [+] accessToken: {access_token[:20]}...")
                else:
                    self._l(f"  {_ts()} [!] 未获取到 accessToken: {str(sess_resp.text)[:200]}")

            if not access_token:
                return self._failed("registered but no access_token returned")

            self.access_token = access_token
            self.session_token = session_token
            self._l(
                f"  ✓ 注册成功 access_token={access_token[:24]}... "
                f"password_set={password_set}"
            )
            return {
                "email": self.email,
                "password": password if password_set else "",
                "session_token": session_token,
                "session_json": session_json,
                "access_token": access_token,
                "device_id": self.device_id,
                "session_cookies": session_cookie_records(s),
                "impersonate": self.impersonate,
                "status": "success",
                "password_set": password_set,
                "raw": {
                    "name": name,
                    "birthdate": birthdate,
                    "password_set": password_set,
                    "create_result": {
                        k: create_result.get(k)
                        for k in ("continue_url", "page")
                        if isinstance(create_result, dict) and k in create_result
                    },
                },
            }
        finally:
            await auth.close()

    def _failed(self, error: str) -> dict:
        self._l(f"  ✗ 注册失败: {error}")
        return {
            "email": self.email,
            "password": self.password,
            "session_token": "",
            "access_token": "",
            "device_id": self.device_id or "",
            "status": "failed",
            "error": error,
        }


def register_account(outlook_creds: dict, *, log_fn=None, proxy: str = "") -> dict:
    """便捷函数：注册单个账号。"""
    bot = ChatGPTRegister(outlook_creds, log_fn=log_fn, proxy=proxy)
    return bot.register()


__all__ = [
    "ChatGPTRegister",
    "OpenAIAuthClient",
    "register_account",
    "random_name",
    "random_age",
    "auth_step_requires_password",
    "auth_step_requires_mfa",
    "random_password",
    "birthdate_from_age",
]
