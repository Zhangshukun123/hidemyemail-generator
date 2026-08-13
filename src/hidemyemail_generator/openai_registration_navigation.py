"""Auth-page readiness, direct navigation, and homepage entry policy."""

from __future__ import annotations

import re
import time
import types

try:
    from . import registration_auth as _registration_auth
    from .browser_diagnostics import BrowserDiagnosticCode, emit_browser_diagnostic
    from .openai_bridge_runtime import safe_log_message
    from .openai_browser_dom import (
        _activate_visible_registration_page,
        _first_visible,
        _page_wait,
    )
    from .openai_browser_selectors import AUTH_RESOURCE_RELOAD_ATTEMPTS
except ImportError:
    import registration_auth as _registration_auth
    from browser_diagnostics import BrowserDiagnosticCode, emit_browser_diagnostic
    from openai_bridge_runtime import safe_log_message
    from openai_browser_dom import (
        _activate_visible_registration_page,
        _first_visible,
        _page_wait,
    )
    from openai_browser_selectors import AUTH_RESOURCE_RELOAD_ATTEMPTS

CHATGPT_HOME_LOGIN_SELECTORS = _registration_auth.CHATGPT_HOME_LOGIN_SELECTORS
CHATGPT_HOME_SIGNUP_SELECTORS = _registration_auth.CHATGPT_HOME_SIGNUP_SELECTORS
OPENAI_EMAIL_LOGIN_INPUT_SELECTORS = (
    _registration_auth.OPENAI_EMAIL_LOGIN_INPUT_SELECTORS
)
OPENAI_EMAIL_SUBMIT_SELECTORS = _registration_auth.OPENAI_EMAIL_SUBMIT_SELECTORS
OPENAI_EMAIL_LOGIN_SUBMIT_SELECTORS = (
    _registration_auth.OPENAI_EMAIL_LOGIN_SUBMIT_SELECTORS
)
OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS = (
    _registration_auth.OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS
)
_auth_click_chatgpt_home_login = _registration_auth.click_chatgpt_home_login
_auth_click_chatgpt_home_signup = _registration_auth.click_chatgpt_home_signup
_auth_is_chatgpt_homepage = _registration_auth.is_chatgpt_homepage
_auth_is_chatgpt_auth_entry_url = _registration_auth.is_chatgpt_auth_entry_url
HOME_EMAIL_MODAL_TRANSITION_TIMEOUT_SECONDS = 120.0
HOME_EMAIL_MODAL_PROGRESS_INTERVAL_SECONDS = 10.0
HOME_EMAIL_MODAL_PROGRESS_SELECTORS = (
    'input[type="password"]',
    'input[autocomplete="current-password"]',
    'input[autocomplete="new-password"]',
    'input[autocomplete="one-time-code"]',
    'input[name="code"]',
    'input[inputmode="numeric"]',
)
CHATGPT_HOME_URL = "https://chatgpt.com/"


def _wait_for_home_email_modal_transition(
    page,
    log,
    *,
    first_visible,
    wait,
    timeout_seconds: float = HOME_EMAIL_MODAL_TRANSITION_TIMEOUT_SECONDS,
    progress_interval_seconds: float = HOME_EMAIL_MODAL_PROGRESS_INTERVAL_SECONDS,
    initial_url: str | None = None,
) -> bool:
    timeout_seconds = max(1.0, float(timeout_seconds))
    progress_interval_seconds = max(1.0, float(progress_interval_seconds))
    started = time.monotonic()
    deadline = started + timeout_seconds
    next_notice_at = started + progress_interval_seconds
    initial_url = str(
        initial_url
        if initial_url is not None
        else getattr(page, "url", "") or ""
    )
    initial_is_homepage = _is_chatgpt_homepage(initial_url)
    initial_is_auth_entry = _is_chatgpt_auth_entry_url(initial_url)
    location_label = (
        "首页邮箱弹窗" if initial_is_homepage else "ChatGPT 登录或新注册页"
    )
    while time.monotonic() < deadline:
        current_url = str(getattr(page, "url", "") or "")
        still_on_entry = (
            _is_chatgpt_homepage(current_url)
            if initial_is_homepage
            else _is_chatgpt_auth_entry_url(current_url)
            if initial_is_auth_entry
            else current_url == initial_url
        )
        if (
            not still_on_entry
            or first_visible(
                page,
                OPENAI_EMAIL_LOGIN_INPUT_SELECTORS,
                timeout=300,
            )
            is None
            or first_visible(
                page,
                HOME_EMAIL_MODAL_PROGRESS_SELECTORS,
                timeout=300,
            )
            is not None
        ):
            log(f"[认证] {location_label}已提交，正在继续认证流程")
            _registration_auth.mark_page_registration_milestone(
                page,
                "email_responded",
                f"{location_label}已离开邮箱输入状态",
            )
            return True
        now = time.monotonic()
        if now >= next_notice_at:
            waited = int(now - started)
            log(
                f"[认证] 邮箱已提交一次，{location_label}仍在等待网络响应；"
                f"已等待 {waited} 秒，继续监测且不刷新、不重复点击"
            )
            next_notice_at = now + progress_interval_seconds
        wait(page, 250)
    raise RuntimeError(
        f"邮箱已输入并点击继续，但{location_label}在 {timeout_seconds:g} 秒内"
        "没有变化；已停止等待，期间未刷新或重复点击"
    )


def _auth_page_resource_state(page) -> dict:
    try:
        state = page.evaluate(
            """() => {
                const host = String(location.hostname || '').toLowerCase();
                const isAuthPage = host === 'auth.openai.com'
                    || host.endsWith('.auth.openai.com');
                const styleSheets = Array.from(document.styleSheets || []);
                const loadedLinks = Array.from(
                    document.querySelectorAll('link[rel~="stylesheet"]')
                ).filter((link) => Boolean(link.sheet));
                return {
                    isAuthPage,
                    styleSheetCount: styleSheets.length,
                    loadedStyleLinkCount: loadedLinks.length,
                };
            }"""
        )
    except Exception:
        return {"isAuthPage": False, "styleSheetCount": 1, "loadedStyleLinkCount": 0}
    return state if isinstance(state, dict) else {}


def ensure_auth_page_resources(
    page,
    log,
    *,
    reload_attempts: int = AUTH_RESOURCE_RELOAD_ATTEMPTS,
) -> bool:
    """Reload an unstyled OpenAI password page before submitting its form."""

    attempts = max(0, int(reload_attempts))
    for attempt in range(attempts + 1):
        for _ in range(8):
            state = _auth_page_resource_state(page)
            if (
                not state.get("isAuthPage")
                or int(state.get("styleSheetCount") or 0) > 0
            ):
                return True
            _page_wait(page, 500)
        if attempt >= attempts:
            break
        log(
            "[认证] OpenAI 密码页样式资源尚未加载，"
            f"保持本机 IP 直连并重新加载 ({attempt + 1}/{attempts})"
        )
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
        except TypeError:
            page.reload()
        except Exception as error:
            log(f"[认证] 直连页面重新加载未完成：{safe_log_message(error)}")
        _page_wait(page, 1500)
    raise RuntimeError(
        "OpenAI 认证页 CSS/JavaScript 未完整加载；当前保持本机 IP 直连，"
        "请检查本机网络或 Cloudflare 验证后重试"
    )


def configure_direct_registration_browser(
    worker,
    *,
    enabled: bool,
    locale: str = "",
) -> bool:
    """Keep proxy-free registration local while stabilizing its auth page."""

    if not enabled:
        return False
    if getattr(worker, "_hme_direct_registration_configured", False):
        return True
    original_new_context = getattr(worker, "_new_browser_context", None)
    original_fill_password = getattr(worker, "_fill_password_step", None)
    original_log = getattr(worker, "log", None)
    if (
        not callable(original_new_context)
        or not callable(original_fill_password)
        or not callable(original_log)
    ):
        return False

    def direct_log(message):
        text = str(message or "")
        text = text.replace(
            "浏览器 HTTP 缓存保持禁用",
            "当前隔离任务启用内存资源缓存",
        )
        text = text.replace(
            "HTTP 缓存禁用",
            "隔离任务内存缓存启用",
        )
        if locale:
            text = text.replace(
                "GeoIP/时区/语言/WebRTC 自动对齐",
                f"GeoIP/时区/WebRTC 自动对齐 · 语言 {locale}",
            )
        return original_log(text)

    def new_direct_context(
        self,
        playwright,
        proxy,
        storage_state=None,
        *args,
        **kwargs,
    ):
        if locale and not str(kwargs.get("locale_override") or "").strip():
            kwargs["locale_override"] = str(locale)
        return original_new_context(
            playwright,
            proxy,
            storage_state,
            *args,
            **kwargs,
        )

    def fill_password_after_resource_check(self, page):
        ensure_auth_page_resources(page, self.log)
        return original_fill_password(page)

    worker._new_browser_context = types.MethodType(new_direct_context, worker)
    worker._fill_password_step = types.MethodType(
        fill_password_after_resource_check, worker
    )
    worker.log = direct_log
    worker._hme_direct_registration_configured = True
    return True


def detect_direct_registration_location(app_backend, log) -> dict[str, str]:
    """Detect the real local exit without introducing a proxy."""

    detector = getattr(app_backend, "detect_proxy_health", None)
    locale_for_country = getattr(app_backend, "country_browser_locale", None)
    if not callable(detector) or not callable(locale_for_country):
        return {"country": "", "locale": "", "timezone": ""}
    try:
        health = detector("", timeout=12, check_stripe=False)
    except Exception as error:
        log(
            "[直连] 本机公网出口地区检测失败，"
            f"浏览器将继续使用 Camoufox GeoIP 自动语言：{safe_log_message(error)}"
        )
        return {"country": "", "locale": "", "timezone": ""}
    country = str(getattr(health, "country", "") or "").strip().upper()
    timezone_name = str(getattr(health, "timezone", "") or "").strip()
    locale = str(locale_for_country(country) or "").strip() if country else ""
    return {
        "country": country,
        "locale": locale,
        "timezone": timezone_name,
    }


def _navigation_was_aborted(error: Exception) -> bool:
    message = str(error or "").casefold()
    return any(
        marker in message
        for marker in (
            "ns_binding_aborted",
            "navigation interrupted by another one",
            "navigation was interrupted by another navigation",
        )
    )


def configure_resilient_registration_navigation(worker, *, attempts: int = 3) -> bool:
    """Retry Firefox navigations that were superseded by an automatic redirect."""

    original_register = getattr(worker, "_register", None)
    if not callable(original_register):
        return False
    if getattr(worker, "_hme_resilient_registration_navigation", False):
        return True
    max_attempts = max(2, int(attempts))

    def register_with_resilient_navigation(page, context, *args, **kwargs):
        original_goto = getattr(page, "goto", None)
        if not callable(original_goto):
            return original_register(page, context, *args, **kwargs)

        def resilient_goto(url, *args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    return original_goto(url, *args, **kwargs)
                except Exception as error:
                    if not _navigation_was_aborted(error) or attempt >= max_attempts:
                        raise
                    safe_target = re.sub(r"[?#].*$", "", str(url or ""))[:160]
                    worker.log(
                        "[认证] 页面自动重定向打断了导航，"
                        f"正在重试 ({attempt + 1}/{max_attempts})：{safe_target}"
                    )
                    time.sleep(min(2.0, 0.5 * attempt))
            raise RuntimeError("页面导航重试次数已用尽")

        try:
            page.goto = resilient_goto
        except Exception:
            # Playwright Page currently permits an instance override. If a future
            # version does not, retain the original behavior instead of changing
            # unrelated registration semantics.
            return original_register(page, context, *args, **kwargs)
        try:
            return original_register(page, context, *args, **kwargs)
        finally:
            try:
                page.goto = original_goto
            except Exception:
                pass

    worker._register = register_with_resilient_navigation
    worker._hme_resilient_registration_navigation = True
    return True


def _is_chatgpt_homepage(url: str) -> bool:
    return _auth_is_chatgpt_homepage(url)


def _is_chatgpt_auth_entry_url(url: str) -> bool:
    return _auth_is_chatgpt_auth_entry_url(url)


def _click_chatgpt_home_login(
    page,
    worker,
    *,
    activate_page=None,
    timeout_seconds: float = 20.0,
    transition_timeout_seconds: float = 15.0,
) -> bool:
    activate_page = activate_page or _activate_visible_registration_page
    return _auth_click_chatgpt_home_login(
        page,
        worker.log,
        first_visible=_first_visible,
        wait=_page_wait,
        activate=lambda target_page: activate_page(worker, target_page),
        timeout_seconds=timeout_seconds,
        transition_timeout_seconds=transition_timeout_seconds,
    )


def _click_chatgpt_home_signup(
    page,
    worker,
    *,
    activate_page=None,
    timeout_seconds: float = 20.0,
    transition_timeout_seconds: float = 15.0,
) -> bool:
    activate_page = activate_page or _activate_visible_registration_page
    return _auth_click_chatgpt_home_signup(
        page,
        worker.log,
        first_visible=_first_visible,
        wait=_page_wait,
        activate=lambda target_page: activate_page(worker, target_page),
        timeout_seconds=timeout_seconds,
        transition_timeout_seconds=transition_timeout_seconds,
    )


def configure_chatgpt_home_login_entry(
    worker,
    *,
    enabled: bool = True,
    activate_page=None,
) -> bool:
    """Enter auth through visible clicks without jumping to a generated auth URL."""

    activate_page = activate_page or _activate_visible_registration_page
    original_register = getattr(worker, "_register", None)
    original_create_registration_url = getattr(
        worker, "_create_openai_signin_url", None
    )
    original_create_login_url = getattr(worker, "_create_login_url", None)
    original_goto_auth_page = getattr(worker, "_goto_auth_page", None)
    original_restart_stalled_session = getattr(
        worker, "_restart_login_after_stalled_session", None
    )
    if not enabled or not all(
        callable(item)
        for item in (
            original_register,
            original_create_registration_url,
            original_create_login_url,
            original_goto_auth_page,
        )
    ):
        return False
    if getattr(worker, "_hme_home_login_entry_configured", False):
        return True

    def register_from_home_login(self, page, context, *args, **kwargs):
        original_goto = getattr(page, "goto", None)
        if not callable(original_goto):
            raise RuntimeError("当前浏览器页面不支持打开 ChatGPT 登录主页")
        clicked_home_login = False
        auth_navigation_pending = False
        auth_navigation_handoff_complete = False
        use_signup_entry = not bool(
            kwargs.get(
                "existing_login_only",
                getattr(self, "existing_login_only", False),
            )
        )

        def goto_and_choose_login(url, *goto_args, **goto_kwargs):
            nonlocal clicked_home_login
            nonlocal auth_navigation_pending, auth_navigation_handoff_complete
            activate_page(self, page)
            result = original_goto(url, *goto_args, **goto_kwargs)
            activate_page(self, page)
            force_home_reentry = bool(
                getattr(self, "_hme_force_home_auth_reentry", False)
            )
            if _is_chatgpt_homepage(url) and (
                not clicked_home_login or force_home_reentry
            ):
                if force_home_reentry:
                    clicked_home_login = False
                    auth_navigation_pending = False
                    auth_navigation_handoff_complete = False
                    self._hme_force_home_auth_reentry = False
                current_url = str(getattr(page, "url", "") or "")
                direct_auth_entry = _is_chatgpt_auth_entry_url(current_url)
                email_input = _first_visible(
                    page,
                    OPENAI_EMAIL_LOGIN_INPUT_SELECTORS,
                    timeout=700,
                )
                recovered_problem_page = False
                if email_input is None and _registration_auth.is_auth_problem_page(page):
                    email_input = _registration_auth.wait_for_auth_email_entry(
                        page,
                        self.log,
                        first_visible=_first_visible,
                        wait=_page_wait,
                        activate=lambda target_page: activate_page(self, target_page),
                    )
                    if email_input is None:
                        raise RuntimeError(
                            "已从登录错误页点击一次返回，但持续监听后仍未识别第二个邮箱界面"
                        )
                    recovered_problem_page = True
                    current_url = str(getattr(page, "url", "") or "")
                    direct_auth_entry = _is_chatgpt_auth_entry_url(current_url)
                preopened_home_modal = bool(
                    _is_chatgpt_homepage(current_url)
                    and email_input is not None
                )
                if (
                    direct_auth_entry
                    or preopened_home_modal
                    or recovered_problem_page
                ) and email_input is not None:
                    clicked_home_login = True
                    _registration_auth.skip_page_registration_step(
                        page,
                        "registration_clicked",
                        "页面已直接进入邮箱输入状态，无需点击注册入口",
                    )
                    _registration_auth.begin_page_registration_step(
                        page,
                        "registration_entry_ready",
                        "正在确认直接出现的邮箱输入界面",
                    )
                    _registration_auth.mark_page_registration_milestone(
                        page,
                        "registration_entry_ready",
                        "页面已直接显示邮箱输入框",
                    )
                    if recovered_problem_page:
                        self.log(
                            "[认证] 登录错误页已点击一次返回；"
                            "已重新识别登录或新注册第二界面的邮箱输入框"
                        )
                    elif direct_auth_entry:
                        self.log(
                            "[认证] ChatGPT 首页直接跳入登录或新注册页；"
                            "已识别邮箱输入框，跳过首页注册按钮"
                        )
                    else:
                        self.log(
                            "[认证] ChatGPT 首页已预先打开登录或新注册抽屉；"
                            "已识别邮箱输入框，跳过首页注册按钮"
                        )
                    activate_page(self, page)
                    email_entry_url = str(getattr(page, "url", "") or "")
                    if not self._fill_email_if_visible(page):
                        raise RuntimeError(
                            "ChatGPT 登录或新注册界面已出现邮箱框，"
                            "但未能输入并提交邮箱"
                        )
                    _wait_for_home_email_modal_transition(
                        page,
                        self.log,
                        first_visible=_first_visible,
                        wait=_page_wait,
                        initial_url=email_entry_url,
                    )
                else:
                    if direct_auth_entry:
                        raise RuntimeError(
                            "ChatGPT 已直接进入登录或新注册页，"
                            "但页面加载完成后未找到邮箱输入框"
                        )
                    if use_signup_entry:
                        _click_chatgpt_home_signup(
                            page,
                            self,
                            activate_page=activate_page,
                        )
                    else:
                        _click_chatgpt_home_login(
                            page,
                            self,
                            activate_page=activate_page,
                        )
                    clicked_home_login = True
                    if not callable(getattr(page, "locator", None)):
                        # Lightweight adapters expose only URL transitions and
                        # cannot inspect the newly opened second screen.
                        return result
                    email_input = _registration_auth.wait_for_auth_email_entry(
                        page,
                        self.log,
                        first_visible=_first_visible,
                        wait=_page_wait,
                        activate=lambda target_page: activate_page(self, target_page),
                    )
                    if email_input is None:
                        raise RuntimeError(
                            "点击注册入口后持续监听 30 秒，仍未识别登录或新注册第二界面"
                        )
                    current_url = str(getattr(page, "url", "") or "")
                    if _is_chatgpt_homepage(current_url):
                        self.log(
                            "[认证] 检测到 ChatGPT 首页邮箱登录弹窗；"
                            "正在当前弹窗输入邮箱，不刷新页面"
                        )
                    else:
                        self.log(
                            "[认证] 已持续监听并识别登录或新注册第二界面；"
                            "正在输入邮箱并点击继续"
                        )
                    activate_page(self, page)
                    email_entry_url = current_url
                    if not self._fill_email_if_visible(page):
                        raise RuntimeError(
                            "登录或新注册第二界面已出现，但未能输入邮箱并点击继续"
                        )
                    _wait_for_home_email_modal_transition(
                        page,
                        self.log,
                        first_visible=_first_visible,
                        wait=_page_wait,
                        initial_url=email_entry_url,
                    )
            return result

        def registration_url_from_clicked_page(request_context):
            nonlocal auth_navigation_pending
            if clicked_home_login and not auth_navigation_handoff_complete:
                current_url = str(getattr(page, "url", "") or "")
                if _is_chatgpt_homepage(current_url):
                    self.log(
                        "[认证] 首页邮箱抽屉提交后仍停留在 ChatGPT 首页；"
                        "改用本次新注册认证 URL，避免把首页误判为已完成登录"
                    )
                    return original_create_registration_url(request_context)
                auth_navigation_pending = True
                return current_url
            return original_create_registration_url(request_context)

        def login_url_from_clicked_page(request_context):
            nonlocal auth_navigation_pending
            if clicked_home_login and not auth_navigation_handoff_complete:
                current_url = str(getattr(page, "url", "") or "")
                if _is_chatgpt_homepage(current_url):
                    self.log(
                        "[认证] 首页邮箱抽屉提交后仍停留在 ChatGPT 首页；"
                        "改用本次登录认证 URL，避免把首页误判为已完成登录"
                    )
                    return original_create_login_url(request_context)
                auth_navigation_pending = True
                return current_url
            return original_create_login_url(request_context)

        def keep_clicked_auth_page(target_page, url):
            nonlocal auth_navigation_pending, auth_navigation_handoff_complete
            if auth_navigation_pending and target_page is page:
                auth_navigation_pending = False
                auth_navigation_handoff_complete = True
                emit_browser_diagnostic(
                    self.log,
                    BrowserDiagnosticCode.AUTH_DIRECT_NAV_BLOCKED,
                    "已通过首页登录按钮进入邮箱页；保留当前页面，不直接跳转认证 URL",
                )
                return None
            return original_goto_auth_page(target_page, url)

        def restart_stalled_session_from_visible_page(
            target_page,
            target_context,
        ):
            """Re-enter auth without closing the visible OTP page/context."""

            is_closed = getattr(target_page, "is_closed", None)
            if callable(is_closed) and is_closed():
                raise RuntimeError(
                    "邮箱验证码页已关闭，保留浏览器上下文后仍没有可操作页面"
                )
            self.log(
                "[验证码] 验证页面会话已失效；保留当前浏览器页和固定代理，"
                "不关闭可见验证码窗口，正在从 ChatGPT 首页重新进入当前邮箱"
            )
            self._hme_force_home_auth_reentry = True
            target_context.clear_cookies()
            self._last_chatgpt_session_email = ""
            self._last_chatgpt_session_mismatch = False
            self._session_mismatch_log_key = ""
            target_page.goto(
                CHATGPT_HOME_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            signin_url = self._create_login_url(target_context)
            self._goto_auth_page(target_page, signin_url)
            self.log(
                "[验证码] 已根据当前页面重新建立同一邮箱登录流程；"
                "正在等待并识别新的邮箱验证码界面"
            )

        try:
            page.goto = goto_and_choose_login
            self._create_openai_signin_url = registration_url_from_clicked_page
            self._create_login_url = login_url_from_clicked_page
            self._goto_auth_page = keep_clicked_auth_page
            if callable(original_restart_stalled_session):
                self._restart_login_after_stalled_session = (
                    restart_stalled_session_from_visible_page
                )
        except Exception as error:
            raise RuntimeError("无法保护 ChatGPT 主页登录入口") from error
        try:
            return original_register(page, context, *args, **kwargs)
        finally:
            try:
                page.goto = original_goto
            except Exception:
                pass
            self._create_openai_signin_url = original_create_registration_url
            self._create_login_url = original_create_login_url
            self._goto_auth_page = original_goto_auth_page
            if callable(original_restart_stalled_session):
                self._restart_login_after_stalled_session = (
                    original_restart_stalled_session
                )

    worker._register = types.MethodType(register_from_home_login, worker)
    worker._hme_home_login_entry_configured = True
    return True
