"""Local Roxy email authentication for the Plus Codex OAuth workflow.

The browser owns interactive identity checks only.  It stops as soon as the
OAuth route reaches the phone challenge and exports the resulting
``auth.openai.com`` Cookie state to the protocol worker.  It intentionally
does not acquire a phone number or submit an SMS code.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse


CHATGPT_BASE_URL = "https://chatgpt.com"
AUTH_BASE_URL = "https://auth.openai.com"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
CODEX_SCOPE = "openid email profile offline_access"

BrowserEvent = Callable[[str, dict[str, Any]], None]
EmailCodeFetcher = Callable[[str, str, str, int], str | None]


def _b64url_no_pad(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def build_codex_oauth_url(email: str) -> tuple[str, str]:
    """Build a forced email-login Codex PKCE URL and return its state."""

    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url_no_pad(hashlib.sha256(verifier.encode("ascii")).digest())
    query = urlencode(
        {
            "client_id": CODEX_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": CODEX_REDIRECT_URI,
            "scope": CODEX_SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "login_hint": str(email or "").strip(),
        }
    )
    return f"{AUTH_BASE_URL}/oauth/authorize?{query}", state


def _safe_route(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return "OAuth 页面"
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"[:240]


def _oauth_route_error(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.hostname != "auth.openai.com" or parsed.path.rstrip("/") != "/error":
        return ""
    query = parse_qs(parsed.query)
    encoded = str((query.get("payload") or [""])[0] or "")
    error_code = ""
    if encoded:
        try:
            decoded = json.loads(
                base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
            )
            if isinstance(decoded, dict):
                error_code = str(decoded.get("errorCode") or decoded.get("error") or "")
        except Exception:
            pass
    if error_code == "rate_limit_exceeded":
        return "OpenAI Codex OAuth 请求频率受限，请稍后手动重试"
    return f"OpenAI Codex OAuth 进入错误页：{error_code or 'unknown_error'}"


def _first_visible(page: Any, selectors: tuple[str, ...]) -> Any | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=250):
                return locator
        except Exception:
            continue
    return None


def _click_visible(page: Any, selectors: tuple[str, ...]) -> bool:
    target = _first_visible(page, selectors)
    if target is None:
        return False
    try:
        target.click(timeout=5_000)
        return True
    except Exception:
        return False


class CodexOAuthBrowserFlow:
    """Drive email authentication in a clean visible Roxy profile."""

    CONTINUE_SELECTORS = (
        'button:has-text("Continue")',
        'button:has-text("Next")',
        'button:has-text("继续")',
        'button:has-text("下一步")',
        'button:has-text("続ける")',
        'button[type="submit"]',
    )
    CONSENT_SELECTORS = (
        'button:has-text("Continue")',
        'button:has-text("Allow")',
        'button:has-text("Authorize")',
        'button:has-text("继续")',
        'button:has-text("允许")',
        'button:has-text("授权")',
        'button[type="submit"]',
    )
    OTP_SELECTORS = (
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
        'input[name="code"]',
        'input[type="tel"]',
    )

    def __init__(
        self,
        *,
        email: str,
        password: str,
        totp_secret: str,
        code_url: str,
        proxy_url: str,
        roxy: dict[str, Any],
        emit: BrowserEvent,
        email_code_fetcher: EmailCodeFetcher,
        timeout_seconds: int = 600,
    ) -> None:
        self.email = str(email or "").strip().lower()
        self.password = str(password or "")
        self.totp_secret = str(totp_secret or "").strip()
        self.code_url = str(code_url or "").strip()
        self.proxy_url = str(proxy_url or "").strip()
        self.roxy = dict(roxy or {})
        self.emit = emit
        self.email_code_fetcher = email_code_fetcher
        self.timeout_seconds = max(60, int(timeout_seconds or 600))
        self._last_action = ""
        self._roxy_session: Any | None = None

    def _event(self, event: str, message: str, **payload: Any) -> None:
        self.emit(event, {"message": message, **payload})

    def _new_context(self, playwright: Any) -> tuple[Any, Any]:
        from .roxy_registration import RoxyRegistrationBrowser

        api_url = str(self.roxy.get("api_url") or "").strip()
        workspace_id = str(self.roxy.get("workspace_id") or "").strip()
        profile_id = str(self.roxy.get("profile_id") or "").strip()
        if not api_url or not workspace_id.isdigit() or not profile_id:
            raise RuntimeError("请先在设置中选择 Roxy 专用指纹环境")
        self._roxy_session = RoxyRegistrationBrowser(
            api_url=api_url,
            api_token=str(os.environ.get("HME_ROXY_API_TOKEN") or ""),
            workspace_id=int(workspace_id),
            profile_id=profile_id,
            proxy_url=self.proxy_url,
            background=False,
            log=lambda message: self._event(
                "browser_log",
                str(message or ""),
                stage="roxy_login",
            ),
        )
        return self._roxy_session.new_browser_context(
            playwright,
            None,
            None,
        )

    def _close(self, _context: Any, browser: Any) -> None:
        try:
            if browser is not None:
                browser.close()
        finally:
            if self._roxy_session is not None:
                self._roxy_session.close()
                self._roxy_session = None

    @staticmethod
    def _navigate(page: Any, url: str) -> None:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            current = str(getattr(page, "url", "") or "")
            if not current.startswith(("http://", "https://")):
                raise
        try:
            page.bring_to_front()
        except Exception:
            pass

    @staticmethod
    def _is_phone_challenge(page: Any) -> bool:
        url = str(getattr(page, "url", "") or "").casefold()
        if any(
            marker in url
            for marker in ("/add-phone", "/phone-verification", "/phone-otp/")
        ):
            return True
        return (
            _first_visible(
                page,
                (
                    'input[type="tel"][autocomplete="tel"]',
                    'input[name*="phone" i]',
                    'input[id*="phone" i]',
                ),
            )
            is not None
        )

    @staticmethod
    def _auth_cookies(context: Any) -> list[dict[str, Any]]:
        cookies = [dict(cookie) for cookie in context.cookies()]
        auth_cookie_found = any(
            (
                str(cookie.get("domain") or "").lstrip(".").casefold()
                in {"auth.openai.com", "openai.com"}
                or str(cookie.get("domain") or "")
                .lstrip(".")
                .casefold()
                .endswith(".auth.openai.com")
            )
            for cookie in cookies
        )
        if not auth_cookie_found:
            raise RuntimeError(
                "浏览器已进入 OAuth 流程，但尚未取得 auth.openai.com Cookie"
            )
        return cookies

    @staticmethod
    def _callback_result(url: str, expected_state: str) -> dict[str, str]:
        query = parse_qs(urlparse(str(url or "")).query)
        error = str((query.get("error_description") or query.get("error") or [""])[0])
        if error:
            raise RuntimeError(f"Codex OAuth 回调失败：{error[:240]}")
        code = str((query.get("code") or [""])[0])
        state = str((query.get("state") or [""])[0])
        if not code:
            raise RuntimeError("Codex OAuth 回调缺少 code")
        if state != expected_state:
            raise RuntimeError("Codex OAuth 回调 state 不匹配")
        return {"code": code, "state": state}

    def _fill_code(self, page: Any, code: str) -> bool:
        inputs: list[Any] = []
        for selector in self.OTP_SELECTORS:
            try:
                candidates = page.locator(selector)
                for index in range(min(candidates.count(), 8)):
                    candidate = candidates.nth(index)
                    if candidate.is_visible(timeout=200):
                        inputs.append(candidate)
                if inputs:
                    break
            except Exception:
                continue
        if not inputs:
            return False
        if len(inputs) >= len(code):
            for index, character in enumerate(code):
                inputs[index].fill(character)
        else:
            inputs[0].fill(code)
        return _click_visible(page, self.CONTINUE_SELECTORS)

    def _handle_identity_page(self, page: Any) -> bool:
        url = str(getattr(page, "url", "") or "")
        folded = url.casefold()
        route = _safe_route(url)

        email_input = _first_visible(
            page,
            (
                'input[type="email"]',
                'input[name="email"]',
                'input[autocomplete="email"]',
            ),
        )
        if email_input is not None and self._last_action != f"email:{route}":
            email_input.fill(self.email)
            if _click_visible(page, self.CONTINUE_SELECTORS):
                self._last_action = f"email:{route}"
                self._event(
                    "browser_log", "Roxy 已提交当前账号邮箱", stage="roxy_login"
                )
                return True

        password_input = _first_visible(
            page,
            (
                'input[type="password"]',
                'input[name="password"]',
                'input[autocomplete="current-password"]',
            ),
        )
        if password_input is not None and self._last_action not in {
            f"password:{route}",
            f"password-wait:{route}",
        }:
            if not self.password:
                self._last_action = f"password-wait:{route}"
                self._event(
                    "browser_waiting",
                    "OAuth 要求密码，但当前账号未保存已确认密码；请在 Roxy 窗口手动完成",
                    stage="roxy_login",
                    level="warning",
                )
                return False
            password_input.fill(self.password)
            if _click_visible(page, self.CONTINUE_SELECTORS):
                self._last_action = f"password:{route}"
                self._event(
                    "browser_log",
                    "Roxy 已提交当前账号保存的密码",
                    stage="roxy_login",
                )
                return True

        otp_input = _first_visible(page, self.OTP_SELECTORS)
        if otp_input is not None and self._last_action != f"otp:{route}":
            page_text = ""
            try:
                page_text = str(
                    page.locator("body").inner_text(timeout=1_000) or ""
                ).casefold()
            except Exception:
                pass
            looks_like_totp = any(
                marker in f"{folded} {page_text}"
                for marker in (
                    "authenticator",
                    "authentication app",
                    "two-factor",
                    "2fa",
                    "totp",
                    "/mfa",
                    "动态码",
                    "身份验证器",
                    "認証アプリ",
                )
            )
            if looks_like_totp:
                if self.totp_secret:
                    from .openai_mfa import generate_totp

                    if self._fill_code(page, generate_totp(self.totp_secret)):
                        self._last_action = f"otp:{route}"
                        self._event(
                            "browser_log",
                            "Roxy 已提交本地生成的 2FA 动态码",
                            stage="roxy_login",
                        )
                        return True
                elif self._last_action != f"totp-wait:{route}":
                    self._last_action = f"totp-wait:{route}"
                    self._event(
                        "browser_waiting",
                        "OAuth 要求 2FA 动态码，但当前账号没有保存密钥；请在 Roxy 窗口手动完成",
                        stage="roxy_login",
                        level="warning",
                    )
                return False
            if self.code_url:
                self._event(
                    "email_otp_sent",
                    "OAuth 要求邮箱二次验证，正在通过当前项目邮箱服务取码",
                    stage="email_otp",
                )
                code = str(
                    self.email_code_fetcher(self.email, self.code_url, "", 180) or ""
                ).strip()
                if code and self._fill_code(page, code):
                    self._last_action = f"otp:{route}"
                    self._event(
                        "browser_log", "Roxy 已提交邮箱验证码", stage="email_otp"
                    )
                    return True

        if any(
            marker in folded for marker in ("/choose-an-account", "/choose-account")
        ):
            if self._last_action != f"choose:{route}" and _click_visible(
                page,
                (
                    f'button:has-text("{self.email}")',
                    f'[role="button"]:has-text("{self.email}")',
                    f'text="{self.email}"',
                    *self.CONTINUE_SELECTORS,
                ),
            ):
                self._last_action = f"choose:{route}"
                return True

        if any(
            marker in folded
            for marker in ("/codex/consent", "/oauth/authorize", "/workspace")
        ):
            if self._last_action != f"consent:{route}" and _click_visible(
                page, self.CONSENT_SELECTORS
            ):
                self._last_action = f"consent:{route}"
                self._event(
                    "browser_log",
                    "Roxy 已继续 Codex OAuth 授权",
                    stage="roxy_login",
                )
                return True
        return False

    def run(self) -> dict[str, Any]:
        if not self.email:
            raise RuntimeError("Roxy OAuth 缺少账号邮箱")

        from playwright.sync_api import sync_playwright

        self._event(
            "roxy_login_started",
            "正在打开干净的 Roxy 指纹环境；不注入旧 Token 或 Cookie",
            stage="roxy_login",
        )
        with sync_playwright() as playwright:
            browser = None
            context = None
            try:
                browser, context = self._new_context(playwright)
                pages = [
                    candidate
                    for candidate in list(getattr(context, "pages", ()) or ())
                    if not candidate.is_closed()
                ]
                page = pages[0] if pages else context.new_page()
                for extra_page in pages[1:]:
                    try:
                        extra_page.close()
                    except Exception:
                        pass

                oauth_url, expected_state = build_codex_oauth_url(self.email)
                callback_urls: list[str] = []

                def capture_callback(route: Any, request: Any) -> None:
                    callback_urls.append(str(request.url or ""))
                    route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=(
                            "<!doctype html><meta charset='utf-8'>"
                            "<title>Codex OAuth complete</title>"
                            "<h2>Codex OAuth complete</h2>"
                        ),
                    )

                context.route("http://localhost:1455/**", capture_callback)
                self._event(
                    "oauth_browser_started",
                    "正在 Roxy 中输入邮箱完成 Codex OAuth 登录",
                    stage="roxy_login",
                )
                self._navigate(page, oauth_url)

                deadline = time.monotonic() + self.timeout_seconds
                next_wait_log = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        if page.is_closed():
                            raise RuntimeError("Roxy 窗口已关闭，Codex OAuth 已停止")
                    except AttributeError:
                        pass
                    current_url = str(getattr(page, "url", "") or "")
                    callback_url = callback_urls[-1] if callback_urls else ""
                    if not callback_url and current_url.startswith(CODEX_REDIRECT_URI):
                        callback_url = current_url
                    if callback_url:
                        self._callback_result(callback_url, expected_state)
                        raise RuntimeError(
                            "Roxy 已完成 Codex OAuth，但 OpenAI 未显示手机号挑战；"
                            "本次未租号，也未把账号误标为已绑定手机"
                        )
                    route_error = _oauth_route_error(current_url)
                    if route_error:
                        raise RuntimeError(route_error)
                    if self._is_phone_challenge(page):
                        self._event(
                            "email_login_succeeded",
                            "Roxy 邮箱登录已到达手机号挑战；正在同步 Cookie 并切换纯协议接码",
                            stage="roxy_login",
                            level="success",
                        )
                        return {
                            "cookies": self._auth_cookies(context),
                            "oauth_record": {},
                            "phone_challenge": True,
                        }
                    if self._handle_identity_page(page):
                        time.sleep(1)
                        continue
                    if time.monotonic() >= next_wait_log:
                        self._event(
                            "browser_waiting",
                            f"Roxy 正在等待 OAuth 页面继续：{_safe_route(current_url)}",
                            stage="roxy_login",
                        )
                        next_wait_log = time.monotonic() + 10
                    time.sleep(1)
                raise TimeoutError(
                    "Roxy Codex OAuth 等待超时；请查看终端最后一条页面状态后重试"
                )
            finally:
                self._close(context, browser)


def run_browser_oauth_session(
    payload: dict[str, Any],
    *,
    emit: BrowserEvent,
    email_code_fetcher: EmailCodeFetcher,
) -> dict[str, Any]:
    """Run the local browser phase from a Plus worker payload."""

    return CodexOAuthBrowserFlow(
        email=str(payload.get("email") or ""),
        password=str(payload.get("password") or ""),
        totp_secret=str(payload.get("totp_secret") or ""),
        code_url=str(payload.get("code_url") or ""),
        proxy_url=str(payload.get("proxy_url") or ""),
        roxy=dict(payload.get("roxy") or {}),
        emit=emit,
        email_code_fetcher=email_code_fetcher,
    ).run()


__all__ = [
    "AUTH_BASE_URL",
    "CHATGPT_BASE_URL",
    "CODEX_REDIRECT_URI",
    "CodexOAuthBrowserFlow",
    "build_codex_oauth_url",
    "run_browser_oauth_session",
]
