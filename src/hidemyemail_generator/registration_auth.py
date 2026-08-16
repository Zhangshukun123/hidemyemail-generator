from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from urllib.parse import urlparse

try:
    from .browser_diagnostics import BrowserDiagnosticCode, emit_browser_diagnostic
    from .registration_locale import registration_action_labels
    from .registration_activity import (
        CLICK_RESPONSE_SECONDS,
        MAX_NO_RESPONSE_CLICK_ATTEMPTS,
        begin_page_registration_step,
        ensure_registration_activity_monitor,
        mark_page_registration_milestone,
        registration_activity_snapshot,
        skip_page_registration_step,  # noqa: F401 - navigation compatibility export
        wait_for_registration_activity,
    )
except ImportError:
    from browser_diagnostics import BrowserDiagnosticCode, emit_browser_diagnostic
    from registration_locale import registration_action_labels
    from registration_activity import (
        CLICK_RESPONSE_SECONDS,
        MAX_NO_RESPONSE_CLICK_ATTEMPTS,
        begin_page_registration_step,
        ensure_registration_activity_monitor,
        mark_page_registration_milestone,
        registration_activity_snapshot,
        skip_page_registration_step,  # noqa: F401 - navigation compatibility export
        wait_for_registration_activity,
    )


CHATGPT_HOME_LOGIN_SELECTORS = (
    'a[data-testid="login-button"]',
    'button[data-testid="login-button"]',
    'a[href*="/auth/login" i]',
    'a[href*="/log-in" i]',
    'button:text-is("Log in")',
    'a:text-is("Log in")',
    '[role="button"]:text-is("Log in")',
    'button:text-is("登录")',
    'a:text-is("登录")',
    '[role="button"]:text-is("登录")',
    'button:text-is("登入")',
    'a:text-is("登入")',
    '[role="button"]:text-is("登入")',
    'button:text-is("ログイン")',
    'a:text-is("ログイン")',
    '[role="button"]:text-is("ログイン")',
)
CHATGPT_HOME_SIGNUP_SELECTORS = (
    'a[data-testid="signup-button"]',
    'button[data-testid="signup-button"]',
    'a[href*="/auth/signup" i]',
    'a[href*="/create-account" i]',
    'button:text-is("Sign up")',
    'a:text-is("Sign up")',
    '[role="button"]:text-is("Sign up")',
    'button:text-is("免费注册")',
    'a:text-is("免费注册")',
    '[role="button"]:text-is("免费注册")',
    'button:text-is("免費註冊")',
    'a:text-is("免費註冊")',
    '[role="button"]:text-is("免費註冊")',
    'button:text-is("新規登録")',
    'a:text-is("新規登録")',
    '[role="button"]:text-is("新規登録")',
    'button:text-is("無料でサインアップ")',
    'a:text-is("無料でサインアップ")',
    '[role="button"]:text-is("無料でサインアップ")',
    'button:text-is("サインアップ")',
    'a:text-is("サインアップ")',
    '[role="button"]:text-is("サインアップ")',
)
CHATGPT_HOME_INTERACTIVE_SELECTOR = (
    'a, button, [role="button"], [role="link"]'
)
CHATGPT_HOME_LOGIN_MARKERS = (
    "log in",
    "login",
    "sign in",
    "signin",
    "登录",
    "登入",
    "ログイン",
)
CHATGPT_HOME_SIGNUP_MARKERS = (
    "sign up",
    "signup",
    "sign-up",
    "create account",
    "create an account",
    "create-account",
    "get started",
    "register",
    "免费注册",
    "立即注册",
    "註冊",
    "创建账号",
    "建立帳戶",
    "开始使用",
    "新規登録",
    "サインアップ",
    "無料で登録",
    "無料登録",
    "無料で始める",
    "始める",
    "회원가입",
    "가입",
)
OPENAI_EMAIL_LOGIN_INPUT_SELECTORS = (
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
    'input[autocomplete="email"]',
    'input[data-testid*="email" i]',
    'input[placeholder="Email address" i]',
    'input[placeholder*="email" i]',
    'input[aria-label*="email" i]',
    'input[placeholder*="メールアドレス"]',
    'input[aria-label*="メールアドレス"]',
    'input[placeholder*="邮箱"]',
    'input[aria-label*="邮箱"]',
    '[role="dialog"]:has-text("Sign up or log in") '
    'input:not([type="hidden"]):not([type="password"])',
    '[role="dialog"]:has-text("Log in or sign up") '
    'input:not([type="hidden"]):not([type="password"])',
    '[role="dialog"]:has-text("登录或注册") '
    'input:not([type="hidden"]):not([type="password"])',
    '[role="dialog"]:has-text("注册或登录") '
    'input:not([type="hidden"]):not([type="password"])',
    '[role="dialog"]:has-text("登入或註冊") '
    'input:not([type="hidden"]):not([type="password"])',
    '[role="dialog"]:has-text("ログインまたは新規登録") '
    'input:not([type="hidden"]):not([type="password"])',
)
_EMAIL_INPUT_SCOPE_SELECTOR = (
    'input:is([type="email"], [name="email"], [name="username"], '
    '[autocomplete="email"], [data-testid*="email" i], '
    '[placeholder*="email" i], [aria-label*="email" i], '
    '[placeholder*="メールアドレス"], [aria-label*="メールアドレス"], '
    '[placeholder*="邮箱"], [aria-label*="邮箱"])'
)
_EMAIL_CONTROL_SCOPES = (
    ':is(form, [role="dialog"]):has('
    f'{_EMAIL_INPUT_SCOPE_SELECTOR})',
    ':is(aside, [data-testid*="auth" i], [data-testid*="modal" i], '
    '[data-testid*="drawer" i], [class*="auth" i], [class*="modal" i], '
    '[class*="drawer" i]):has('
    f'{_EMAIL_INPUT_SCOPE_SELECTOR})',
)
_EMAIL_REGISTRATION_ACTION_LABELS = registration_action_labels("email_submit")
OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS = (
    *tuple(
        f'{scope} {control}:text-is("{label}")'
        for scope in _EMAIL_CONTROL_SCOPES
        for label in _EMAIL_REGISTRATION_ACTION_LABELS
        for control in ("button", '[role="button"]')
    ),
)
OPENAI_EMAIL_LOGIN_SUBMIT_SELECTORS = (
    *OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS,
    *tuple(
        f'{scope} {control}:text-is("{label}")'
        for scope in _EMAIL_CONTROL_SCOPES
        for label in ("Log in", "登录", "登入", "ログイン")
        for control in ("button", '[role="button"]')
    ),
    *tuple(f'{scope} button[type="submit"]' for scope in _EMAIL_CONTROL_SCOPES),
)
# Compatibility export for existing-account login callers. Registration callers
# must explicitly use OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS.
OPENAI_EMAIL_SUBMIT_SELECTORS = OPENAI_EMAIL_LOGIN_SUBMIT_SELECTORS

AUTH_PROBLEM_PAGE_MARKERS = (
    "問題が発生しました",
    "サインイン中に問題が発生しました",
    "もう一度お試しください",
    "something went wrong",
    "problem signing in",
    "problem while signing in",
    "登录时出现问题",
    "登入時發生問題",
)
AUTH_PROBLEM_BACK_LABELS = (
    "戻る",
    "Back",
    "返回",
    "返回上一页",
    "返回上一頁",
)
AUTH_PROBLEM_BACK_SELECTORS = tuple(
    f'{control}:text-is("{label}")'
    for label in AUTH_PROBLEM_BACK_LABELS
    for control in ("button", '[role="button"]', "a", '[role="link"]')
)
AUTH_EMAIL_ENTRY_MONITOR_TIMEOUT_SECONDS = 30.0
AUTH_EMAIL_ENTRY_MONITOR_LOG_INTERVAL_SECONDS = 5.0


def is_chatgpt_homepage(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    host = (parsed.hostname or "").strip().lower()
    return host == "chatgpt.com" and (parsed.path or "/").rstrip("/") == ""


def is_chatgpt_auth_entry_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    host = (parsed.hostname or "").strip().lower()
    path = (parsed.path or "/").rstrip("/").casefold()
    return host == "chatgpt.com" and path in {
        "/auth/login",
        "/auth/log-in",
        "/auth/signup",
        "/auth/sign-up",
        "/auth/register",
        "/auth/create-account",
    }


def is_openai_auth_url(url: str) -> bool:
    try:
        host = (urlparse(str(url or "")).hostname or "").strip().lower()
    except Exception:
        return False
    return host == "auth.openai.com" or host.endswith(".auth.openai.com")


def locator_is_enabled(candidate) -> bool:
    is_enabled = getattr(candidate, "is_enabled", None)
    if not callable(is_enabled):
        return True
    try:
        return bool(is_enabled(timeout=700))
    except TypeError:
        try:
            return bool(is_enabled())
        except Exception:
            return False
    except Exception:
        return False


def _candidate_identity(candidate) -> str:
    values = []
    for reader_name in ("inner_text", "text_content"):
        reader = getattr(candidate, reader_name, None)
        if not callable(reader):
            continue
        try:
            value = reader(timeout=500)
        except TypeError:
            try:
                value = reader()
            except Exception:
                value = ""
        except Exception:
            value = ""
        if value:
            values.append(str(value))
    getter = getattr(candidate, "get_attribute", None)
    if callable(getter):
        for name in (
            "aria-label",
            "title",
            "value",
            "data-testid",
            "href",
        ):
            try:
                value = getter(name, timeout=500)
            except TypeError:
                try:
                    value = getter(name)
                except Exception:
                    value = ""
            except Exception:
                value = ""
            if value:
                values.append(str(value))
    return " ".join(" ".join(values).split()).casefold()


def _semantic_home_entry(page, *, entry_kind: str):
    try:
        controls = page.locator(CHATGPT_HOME_INTERACTIVE_SELECTOR)
        count = min(int(controls.count()), 80)
    except Exception:
        return None
    markers = (
        CHATGPT_HOME_SIGNUP_MARKERS
        if entry_kind == "signup"
        else CHATGPT_HOME_LOGIN_MARKERS
    )
    for index in range(count):
        try:
            candidate = controls.nth(index)
            is_visible = getattr(candidate, "is_visible", None)
            if callable(is_visible):
                try:
                    if not is_visible(timeout=500):
                        continue
                except TypeError:
                    if not is_visible():
                        continue
            identity = _candidate_identity(candidate)
            if identity and any(marker in identity for marker in markers):
                return candidate
        except Exception:
            continue
    return None


def _wait_for_candidate(
    page,
    selectors: tuple[str, ...],
    *,
    timeout_seconds: float,
    first_visible: Callable,
    wait: Callable,
    semantic_candidate: Callable | None = None,
):
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        candidate = first_visible(page, selectors, timeout=700)
        if candidate is None and semantic_candidate is not None:
            candidate = semantic_candidate(page)
        if candidate is not None and locator_is_enabled(candidate):
            return candidate
        wait(page, 250)
    return None


def _transitioned(page, before_url: str, *, first_visible: Callable) -> bool:
    current_url = str(getattr(page, "url", "") or "")
    if is_openai_auth_url(current_url) or is_chatgpt_auth_entry_url(current_url):
        return True
    # A query/hash update on the homepage or a jump to an unrelated host is
    # not proof that the registration drawer opened.
    _ = before_url
    return (
        first_visible(page, OPENAI_EMAIL_LOGIN_INPUT_SELECTORS, timeout=300)
        is not None
    )


def _entry_evidence_from_snapshot(snapshot: object) -> dict[str, object]:
    state = snapshot if isinstance(snapshot, dict) else {}
    return {
        "event": str(state.get("lastEntryEvent") or ""),
        "method": str(state.get("lastEntryMethod") or ""),
        "route": str(state.get("lastEntryRoute") or ""),
        "resourceType": str(state.get("lastEntryResourceType") or ""),
        "status": max(0, int(state.get("lastEntryStatus") or 0)),
    }


def _entry_evidence_text(evidence: object) -> str:
    state = evidence if isinstance(evidence, dict) else {}
    return (
        f"event={str(state.get('event') or 'unknown')[:24]} "
        f"route={str(state.get('route') or 'unknown')[:220]} "
        f"status={max(0, int(state.get('status') or 0))} "
        f"type={str(state.get('resourceType') or 'unknown')[:32]}"
    )


def _wait_for_transition(
    page,
    before_url: str,
    *,
    timeout_seconds: float,
    first_visible: Callable,
    wait: Callable,
) -> bool:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while time.monotonic() < deadline:
        if _transitioned(page, before_url, first_visible=first_visible):
            return True
        wait(page, 250)
    return _transitioned(page, before_url, first_visible=first_visible)


def _click_candidate(candidate) -> None:
    try:
        candidate.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        candidate.click(timeout=15000, no_wait_after=True)
    except TypeError:
        try:
            candidate.click(timeout=15000)
        except Exception as error:
            try:
                candidate.evaluate("element => element.click()")
            except Exception:
                raise RuntimeError(
                    "ChatGPT 首页入口按钮点击失败"
                ) from error
    except Exception as error:
        detail = str(error or "").lower()
        pointer_failure = any(
            marker in detail
            for marker in (
                "intercept",
                "obscur",
                "receives pointer",
                "not visible",
                "not stable",
                "outside of the viewport",
                "detached",
            )
        )
        if not pointer_failure:
            # A non-pointer timeout may mean the first click already initiated
            # navigation. The caller checks the page transition before retrying.
            raise RuntimeError(
                "ChatGPT 首页入口按钮点击后等待页面响应超时"
            ) from error
        try:
            candidate.click(
                timeout=5000,
                no_wait_after=True,
                force=True,
            )
        except TypeError:
            try:
                candidate.click(timeout=5000, force=True)
            except Exception:
                try:
                    candidate.evaluate("element => element.click()")
                except Exception:
                    raise RuntimeError(
                        "ChatGPT 首页入口按钮强制点击失败"
                    ) from error
        except Exception:
            try:
                candidate.evaluate("element => element.click()")
            except Exception:
                raise RuntimeError(
                    "ChatGPT 首页入口按钮强制点击失败"
                ) from error


def _page_body_text(page) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=700) or "")
    except TypeError:
        try:
            return str(page.locator("body").inner_text() or "")
        except Exception:
            return ""
    except Exception:
        return ""


def is_auth_problem_page(page) -> bool:
    """Recognize the localized sign-in error page shown before email entry."""

    body_text = " ".join(_page_body_text(page).split())
    folded = body_text.casefold()
    return any(
        marker.casefold() in folded for marker in AUTH_PROBLEM_PAGE_MARKERS
    )


def _semantic_auth_problem_back(page):
    try:
        controls = page.locator('button, a, [role="button"], [role="link"]')
        count = min(int(controls.count()), 40)
    except Exception:
        return None
    labels = tuple(label.casefold() for label in AUTH_PROBLEM_BACK_LABELS)
    for index in range(count):
        try:
            candidate = controls.nth(index)
            is_visible = getattr(candidate, "is_visible", None)
            if callable(is_visible):
                try:
                    if not is_visible(timeout=500):
                        continue
                except TypeError:
                    if not is_visible():
                        continue
            identity = _candidate_identity(candidate)
            if identity and any(label in identity for label in labels):
                return candidate
        except Exception:
            continue
    return None


def _auth_problem_back_candidate(page, *, first_visible: Callable):
    candidate = first_visible(
        page,
        AUTH_PROBLEM_BACK_SELECTORS,
        timeout=700,
    )
    if candidate is not None and locator_is_enabled(candidate):
        return candidate
    candidate = _semantic_auth_problem_back(page)
    if candidate is not None and locator_is_enabled(candidate):
        return candidate
    return None


def wait_for_auth_email_entry(
    page,
    log: Callable[[str], object],
    *,
    first_visible: Callable,
    wait: Callable,
    activate: Callable | None = None,
    timeout_seconds: float = AUTH_EMAIL_ENTRY_MONITOR_TIMEOUT_SECONDS,
):
    """Monitor the auth surface, recover one error page, and return its email input."""

    started = time.monotonic()
    deadline = started + max(0.25, float(timeout_seconds))
    next_notice_at = started + AUTH_EMAIL_ENTRY_MONITOR_LOG_INTERVAL_SECONDS
    problem_back_clicked = False
    problem_back_clicked_at = 0.0
    while time.monotonic() < deadline:
        email_input = first_visible(
            page,
            OPENAI_EMAIL_LOGIN_INPUT_SELECTORS,
            timeout=500,
        )
        if email_input is not None:
            if problem_back_clicked:
                log(
                    "[界面监听] 点击一次「戻る」后已识别登录或新注册第二界面；"
                    "邮箱输入框可操作"
                )
            else:
                log("[界面监听] 已识别登录或新注册第二界面；邮箱输入框可操作")
            mark_page_registration_milestone(
                page,
                "registration_entry_ready",
                "已持续监听并识别邮箱输入界面",
            )
            return email_input

        now = time.monotonic()
        if is_auth_problem_page(page):
            if problem_back_clicked:
                if now - problem_back_clicked_at >= CLICK_RESPONSE_SECONDS:
                    raise RuntimeError(
                        "日文登录错误页的「戻る」已点击一次，"
                        "但页面仍未返回登录或新注册第二界面"
                    )
            else:
                candidate = _auth_problem_back_candidate(
                    page,
                    first_visible=first_visible,
                )
                if candidate is None:
                    raise RuntimeError(
                        "已识别日文登录错误页，但未找到可操作的「戻る」按钮"
                    )
                if activate is not None:
                    activate(page)
                try:
                    _click_candidate(candidate)
                except RuntimeError as error:
                    raise RuntimeError("日文登录错误页的「戻る」按钮点击失败") from error
                problem_back_clicked = True
                problem_back_clicked_at = time.monotonic()
                log(
                    "[界面监听] 已识别日文登录错误页并点击一次「戻る」；"
                    "正在持续等待登录或新注册第二界面"
                )
                begin_page_registration_step(
                    page,
                    "registration_entry_ready",
                    "错误页已返回，正在监听第二个邮箱界面",
                )
        elif now >= next_notice_at:
            waited = int(now - started)
            log(
                f"[界面监听] 正在等待登录或新注册第二界面，已监听 {waited} 秒"
            )
            next_notice_at = now + AUTH_EMAIL_ENTRY_MONITOR_LOG_INTERVAL_SECONDS
        wait(page, 250)
    return None


def click_chatgpt_home_login(
    page,
    log: Callable[[str], object],
    *,
    first_visible: Callable,
    wait: Callable,
    activate: Callable | None = None,
    timeout_seconds: float = 20.0,
    transition_timeout_seconds: float = 15.0,
    entry_selectors: tuple[str, ...] = CHATGPT_HOME_LOGIN_SELECTORS,
    entry_label: str = "登录",
    entry_kind: str = "login",
) -> bool:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("load", timeout=30000)
    except Exception as error:
        ready_state = ""
        evaluate = getattr(page, "evaluate", None)
        if callable(evaluate):
            try:
                ready_state = str(evaluate("document.readyState") or "").lower()
            except Exception:
                ready_state = ""
        if ready_state != "complete":
            raise RuntimeError(
                "ChatGPT 首页尚未完成加载；未点击登录或免费注册"
            ) from error

    emit_browser_diagnostic(
        log,
        BrowserDiagnosticCode.AUTH_HOME_READY,
        f"ChatGPT 首页已完成加载，正在定位{entry_label}按钮",
    )
    mark_page_registration_milestone(page, "site_loaded", "document.readyState=complete")
    wait(page, int(CLICK_RESPONSE_SECONDS * 1000))
    semantic_match_logged = False

    def semantic_candidate(target_page):
        nonlocal semantic_match_logged
        candidate = _semantic_home_entry(
            target_page,
            entry_kind=entry_kind,
        )
        if candidate is not None and not semantic_match_logged:
            log(
                f"[认证] 固定选择器未命中；已通过可见控件文字或链接识别"
                f"{entry_label}按钮"
            )
            semantic_match_logged = True
        return candidate

    candidate = _wait_for_candidate(
        page,
        entry_selectors,
        timeout_seconds=timeout_seconds,
        first_visible=first_visible,
        wait=wait,
        semantic_candidate=semantic_candidate,
    )
    if candidate is None:
        log(
            f"[认证] ChatGPT 首页加载完成后暂未出现{entry_label}按钮；"
            "正在后台重新加载一次并等待控件恢复"
        )
        if activate is not None:
            activate(page)
        reload_page = getattr(page, "reload", None)
        if callable(reload_page):
            try:
                reload_page(wait_until="domcontentloaded", timeout=60000)
            except TypeError:
                reload_page()
            try:
                page.wait_for_load_state("load", timeout=30000)
            except Exception:
                pass
            candidate = _wait_for_candidate(
                page,
                entry_selectors,
                timeout_seconds=timeout_seconds,
                first_visible=first_visible,
                wait=wait,
                semantic_candidate=semantic_candidate,
            )
        if candidate is None:
            raise RuntimeError(
                f"ChatGPT 首页重新加载后仍未找到可用的{entry_label}按钮"
            )

    before_url = str(getattr(page, "url", "") or "")
    ensure_registration_activity_monitor(page)
    response_window = min(
        CLICK_RESPONSE_SECONDS,
        max(0.05, float(transition_timeout_seconds)),
    )
    begin_page_registration_step(
        page,
        "registration_clicked",
        f"等待点击{entry_label}，最多 {MAX_NO_RESPONSE_CLICK_ATTEMPTS} 次",
    )
    current_candidate = candidate
    for attempt in range(1, MAX_NO_RESPONSE_CLICK_ATTEMPTS + 1):
        before_activity = registration_activity_snapshot(page)
        click_error: RuntimeError | None = None
        try:
            if activate is not None:
                activate(page)
            _click_candidate(current_candidate)
        except RuntimeError as error:
            click_error = error
        mark_page_registration_milestone(
            page,
            "registration_clicked",
            f"第 {attempt} 次点击{entry_label}",
        )
        begin_page_registration_step(
            page,
            "registration_entry_ready",
            f"第 {attempt} 次点击后静默等待 {CLICK_RESPONSE_SECONDS:g} 秒",
        )
        emit_browser_diagnostic(
            log,
            (
                BrowserDiagnosticCode.AUTH_HOME_LOGIN_CLICK
                if attempt == 1
                else BrowserDiagnosticCode.AUTH_HOME_LOGIN_RETRY
            ),
            f"ChatGPT 首页第 {attempt} 次点击{entry_label}；"
            f"正在保留 {CLICK_RESPONSE_SECONDS:g} 秒响应时间",
            attempt=attempt,
        )
        activity = wait_for_registration_activity(
            page,
            before_activity,
            timeout_seconds=response_window,
            wait=wait,
            transition=lambda: _transitioned(
                page,
                before_url,
                first_visible=first_visible,
            ),
            signal="registration_entry",
        )
        if activity.get("changed"):
            if activity.get("reason") != "transition":
                evidence = activity.get("evidence")
                log(
                    f"[请求监测] 第 {attempt} 次点击{entry_label}后检测到"
                    f"{activity.get('reason') or '注册入口网络'}活动；"
                    f"停止补点并等待页面完成；{_entry_evidence_text(evidence)}"
                )
                transitioned = _wait_for_transition(
                    page,
                    before_url,
                    timeout_seconds=transition_timeout_seconds,
                    first_visible=first_visible,
                    wait=wait,
                )
                latest_evidence = _entry_evidence_from_snapshot(
                    registration_activity_snapshot(page)
                )
                if latest_evidence.get("event"):
                    evidence = latest_evidence
            else:
                transitioned = True
                evidence = activity.get("evidence")
            if transitioned:
                mark_page_registration_milestone(
                    page,
                    "registration_entry_ready",
                    f"第 {attempt} 次点击后页面已有响应",
                )
                emit_browser_diagnostic(
                    log,
                    BrowserDiagnosticCode.AUTH_HOME_TRANSITION,
                    f"第 {attempt} 次点击{entry_label}后页面已响应",
                    attempt=attempt,
                )
                return True
            status = max(
                0,
                int((evidence if isinstance(evidence, dict) else {}).get("status") or 0),
            )
            if status >= 400:
                raise RuntimeError(
                    f"ChatGPT 首页{entry_label}入口响应 HTTP {status}；"
                    f"{_entry_evidence_text(evidence)}；已停止补点"
                )
            raise RuntimeError(
                f"ChatGPT 首页{entry_label}已有注册入口网络请求，"
                "但页面未在限定时间内完成变化；"
                f"{_entry_evidence_text(evidence)}；为避免重复提交已停止补点"
            )
        if activity.get("ignoredActivity"):
            log(
                f"[请求监测] 第 {attempt} 次点击{entry_label}后仅检测到"
                f"{activity.get('ignoredReason') or '后台'}活动，未将其当作注册入口响应；"
                f"{_entry_evidence_text(activity.get('ignoredEvidence'))}"
            )
        if click_error is not None and attempt >= MAX_NO_RESPONSE_CLICK_ATTEMPTS:
            raise RuntimeError(
                f"ChatGPT 首页{entry_label}按钮点击失败"
            ) from click_error
        if attempt >= MAX_NO_RESPONSE_CLICK_ATTEMPTS:
            break
        current_candidate = _wait_for_candidate(
            page,
            entry_selectors,
            timeout_seconds=response_window,
            first_visible=first_visible,
            wait=wait,
            semantic_candidate=semantic_candidate,
        )
        if current_candidate is None or not is_chatgpt_homepage(
            str(getattr(page, "url", "") or "")
        ):
            if _wait_for_transition(
                page,
                before_url,
                timeout_seconds=transition_timeout_seconds,
                first_visible=first_visible,
                wait=wait,
            ):
                mark_page_registration_milestone(
                    page,
                    "registration_entry_ready",
                    f"第 {attempt} 次点击后延迟进入注册入口",
                )
                return True
            raise RuntimeError(
                f"ChatGPT 首页{entry_label}按钮已不可再次操作，"
                "但页面未在限定时间内完成变化"
            )
        log(
            f"[请求监测] 第 {attempt} 次点击{entry_label}后 "
            f"{CLICK_RESPONSE_SECONDS:g} 秒无请求、无响应且页面未变化；"
            f"准备第 {attempt + 1}/{MAX_NO_RESPONSE_CLICK_ATTEMPTS} 次点击"
        )
    raise RuntimeError(
        f"ChatGPT 首页最多点击 {MAX_NO_RESPONSE_CLICK_ATTEMPTS} 次"
        f"{entry_label}后，"
        "页面未在限定时间内完成变化且未检测到注册入口响应"
    )


def click_chatgpt_home_signup(
    page,
    log: Callable[[str], object],
    *,
    first_visible: Callable,
    wait: Callable,
    activate: Callable | None = None,
    timeout_seconds: float = 20.0,
    transition_timeout_seconds: float = 15.0,
) -> bool:
    return click_chatgpt_home_login(
        page,
        log,
        first_visible=first_visible,
        wait=wait,
        activate=activate,
        timeout_seconds=timeout_seconds,
        transition_timeout_seconds=transition_timeout_seconds,
        entry_selectors=CHATGPT_HOME_SIGNUP_SELECTORS,
        entry_label="免费注册",
        entry_kind="signup",
    )


def input_value(candidate) -> str | None:
    getter = getattr(candidate, "input_value", None)
    if not callable(getter):
        return None
    try:
        return str(getter(timeout=2000) or "")
    except TypeError:
        return str(getter() or "")


def _submit_candidate_label(candidate) -> str:
    values = []
    for reader_name in ("inner_text", "text_content"):
        reader = getattr(candidate, reader_name, None)
        if not callable(reader):
            continue
        try:
            value = reader(timeout=500)
        except TypeError:
            try:
                value = reader()
            except Exception:
                value = ""
        except Exception:
            value = ""
        if value:
            values.append(str(value))
    getter = getattr(candidate, "get_attribute", None)
    if callable(getter):
        for name in ("aria-label", "title", "value"):
            try:
                value = getter(name, timeout=500)
            except TypeError:
                try:
                    value = getter(name)
                except Exception:
                    value = ""
            except Exception:
                value = ""
            if value:
                values.append(str(value))
    return " ".join(values).casefold()


def _visible_submit_candidates(page) -> list:
    try:
        candidates = page.locator('button, [role="button"]')
        count = min(int(candidates.count()), 40)
    except Exception:
        return []
    visible = []
    for index in range(count):
        try:
            candidate = candidates.nth(index)
            is_visible = getattr(candidate, "is_visible", None)
            if callable(is_visible):
                try:
                    if not is_visible(timeout=500):
                        continue
                except TypeError:
                    if not is_visible():
                        continue
            visible.append(candidate)
        except Exception:
            continue
    return visible


def visible_email_submit_labels(page) -> list[str]:
    labels = []
    for candidate in _visible_submit_candidates(page):
        label = " ".join(_submit_candidate_label(candidate).split())
        if label and label not in labels:
            labels.append(label)
    return labels


def _candidate_bounding_box(candidate) -> dict | None:
    bounding_box = getattr(candidate, "bounding_box", None)
    if not callable(bounding_box):
        return None
    try:
        box = bounding_box(timeout=1000)
    except TypeError:
        try:
            box = bounding_box()
        except Exception:
            return None
    except Exception:
        return None
    return box if isinstance(box, dict) else None


def click_email_submit(
    page,
    *,
    first_visible: Callable,
    submit_selectors: tuple[str, ...] = OPENAI_EMAIL_SUBMIT_SELECTORS,
    allowed_labels: tuple[str, ...] | None = None,
    anchor_input=None,
    selection_log: Callable[[str], object] | None = None,
) -> bool:
    def click_candidate(current, *, target_detail: str) -> bool:
        label = _submit_candidate_label(current)
        if any(
            marker in label
            for marker in (
                "google",
                "apple",
                "microsoft",
                "github",
                "sso",
                "phone",
                "手机号",
                "手机号码",
                "電話番号",
            )
        ):
            return False
        if selection_log is not None:
            selection_log(
                "[AUTH_EMAIL_SUBMIT_TARGET] 注册邮箱提交目标："
                f"文字={label or '空'}；{target_detail}"
            )
        try:
            current.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            current.click(timeout=5000, no_wait_after=True)
        except TypeError:
            try:
                current.click(timeout=5000)
            except Exception:
                try:
                    current.evaluate("element => element.click()")
                except Exception:
                    return False
        except Exception:
            try:
                current.click(
                    timeout=5000,
                    no_wait_after=True,
                    force=True,
                )
            except Exception:
                try:
                    current.evaluate("element => element.click()")
                except Exception:
                    return False
        return True

    for selector in submit_selectors:
        current = first_visible(page, (selector,), timeout=700)
        if current is None:
            continue
        if click_candidate(current, target_detail=f"范围选择器={selector}"):
            return True

    if allowed_labels:
        allowed = tuple(label.casefold() for label in allowed_labels)
        rejected = (
            "log in",
            "登录",
            "登入",
            "ログイン",
            "phone",
            "手机号",
            "手机号码",
            "電話番号",
        )
        anchor_box = _candidate_bounding_box(anchor_input)
        ranked_candidates = []
        for current in _visible_submit_candidates(page):
            label = _submit_candidate_label(current)
            if any(marker in label for marker in rejected):
                continue
            if not any(marker in label for marker in allowed):
                continue
            current_box = _candidate_bounding_box(current)
            if anchor_input is not None:
                if anchor_box is None or current_box is None:
                    continue
                anchor_left = float(anchor_box.get("x") or 0.0)
                anchor_width = float(anchor_box.get("width") or 0.0)
                anchor_right = anchor_left + anchor_width
                anchor_bottom = float(anchor_box.get("y") or 0.0) + float(
                    anchor_box.get("height") or 0.0
                )
                current_left = float(current_box.get("x") or 0.0)
                current_width = float(current_box.get("width") or 0.0)
                current_right = current_left + current_width
                vertical_gap = float(current_box.get("y") or 0.0) - anchor_bottom
                overlap = max(
                    0.0,
                    min(anchor_right, current_right)
                    - max(anchor_left, current_left),
                )
                min_width = max(1.0, min(anchor_width, current_width))
                if vertical_gap < -5.0 or vertical_gap > 260.0:
                    continue
                if overlap / min_width < 0.5:
                    continue
                center_delta = abs(
                    (current_left + current_width / 2.0)
                    - (anchor_left + anchor_width / 2.0)
                )
                score = vertical_gap + center_delta * 0.25
                detail = (
                    f"邮箱框下方间距={vertical_gap:.0f}px；"
                    f"横向重叠={overlap / min_width:.0%}"
                )
            else:
                score = float(len(ranked_candidates))
                detail = "无邮箱框坐标，仅用于兼容调用"
            ranked_candidates.append((score, current, detail))
        for _score, current, detail in sorted(
            ranked_candidates,
            key=lambda item: item[0],
        ):
            if click_candidate(current, target_detail=detail):
                return True
    return False


def paste_email_and_submit(
    page,
    email_input,
    email: str,
    *,
    log: Callable[[str], object],
    activate: Callable,
    wait: Callable,
    first_visible: Callable,
    clipboard_write: Callable[[str], None],
    clipboard_lock: AbstractContextManager,
    submit_selectors: tuple[str, ...] = OPENAI_EMAIL_SUBMIT_SELECTORS,
    submit_allowed_labels: tuple[str, ...] | None = None,
    allow_enter_submit: bool = True,
    submit_diagnostic_message: str = "已点击登录/继续；未直接跳转认证 URL",
) -> None:
    begin_page_registration_step(
        page,
        "email_entered",
        "正在输入邮箱并回读输入框",
    )
    activate(page)
    emit_browser_diagnostic(
        log,
        BrowserDiagnosticCode.AUTH_EMAIL_FOCUS,
        "点击 OpenAI 邮箱输入框，准备从系统剪贴板粘贴注册邮箱",
    )
    pointer_focus_failed = False
    try:
        email_input.click(timeout=5000)
    except TypeError:
        try:
            email_input.click()
        except Exception:
            pointer_focus_failed = True
    except Exception:
        pointer_focus_failed = True
    if pointer_focus_failed:
        try:
            email_input.click(
                timeout=5000,
                no_wait_after=True,
                force=True,
            )
        except Exception:
            evaluate = getattr(email_input, "evaluate", None)
            if callable(evaluate):
                evaluate("element => element.focus()")

    dom_filled = False
    try:
        try:
            email_input.press("Control+A", timeout=2000)
        except TypeError:
            email_input.press("Control+A")
    except Exception:
        fill = getattr(email_input, "fill", None)
        if not callable(fill):
            raise
        try:
            fill(email, timeout=5000, force=True)
        except TypeError:
            fill(email)
        dom_filled = True

    if not dom_filled:
        with clipboard_lock:
            clipboard_write(email)
            try:
                try:
                    email_input.press("Control+V", timeout=5000)
                except TypeError:
                    email_input.press("Control+V")
                wait(page, 250)
            except Exception:
                fill = getattr(email_input, "fill", None)
                if not callable(fill):
                    raise
                try:
                    fill(email, timeout=5000, force=True)
                except TypeError:
                    fill(email)
                dom_filled = True
            finally:
                try:
                    clipboard_write("")
                except Exception:
                    pass
    pasted_value = input_value(email_input)
    if pasted_value is None:
        raise RuntimeError("无法回读邮箱输入框内容，已停止提交")
    if pasted_value.strip() != email.strip():
        fill = getattr(email_input, "fill", None)
        if not callable(fill):
            raise RuntimeError("Ctrl+V 后邮箱输入框内容校验失败")
        try:
            fill(email, timeout=5000)
        except TypeError:
            fill(email)
        wait(page, 250)
        pasted_value = input_value(email_input)
        if pasted_value is None:
            raise RuntimeError("后台 DOM 填写后无法回读邮箱输入框内容")
        if pasted_value.strip() != email.strip():
            raise RuntimeError("后台 DOM 填写后邮箱输入框内容校验失败")
        log("[认证] 无头/后台剪贴板未生效，已改用输入框 DOM 填写并校验")
    verified_email = pasted_value.strip()
    emit_browser_diagnostic(
        log,
        BrowserDiagnosticCode.AUTH_EMAIL_PASTE,
        f"邮箱输入检查通过：{verified_email}；浏览器输入框回读与目标邮箱一致",
    )
    mark_page_registration_milestone(
        page,
        "email_entered",
        "邮箱输入框回读与目标邮箱一致",
    )
    wait(page, int(CLICK_RESPONSE_SECONDS * 1000))
    activate(page)
    submitted_with_enter = False
    before_url = str(getattr(page, "url", "") or "")
    monitor = ensure_registration_activity_monitor(page)
    begin_page_registration_step(
        page,
        "email_submitted",
        f"等待提交邮箱，提交后静默等待 {CLICK_RESPONSE_SECONDS:g} 秒",
    )
    if allow_enter_submit:
        before_activity = registration_activity_snapshot(page)
        try:
            try:
                email_input.press("Enter", timeout=5000)
            except TypeError:
                email_input.press("Enter")
            if monitor.available:
                activity = wait_for_registration_activity(
                    page,
                    before_activity,
                    timeout_seconds=CLICK_RESPONSE_SECONDS,
                    wait=wait,
                    transition=lambda: (
                        str(getattr(page, "url", "") or "") != before_url
                        or first_visible(
                            page,
                            OPENAI_EMAIL_LOGIN_INPUT_SELECTORS,
                            timeout=300,
                        )
                        is None
                    ),
                )
                submitted_with_enter = bool(activity.get("changed"))
            else:
                for _ in range(3):
                    wait(page, 500)
                    current_url = str(getattr(page, "url", "") or "")
                    if current_url and current_url != before_url:
                        submitted_with_enter = True
                        break
                    if first_visible(
                        page,
                        OPENAI_EMAIL_LOGIN_INPUT_SELECTORS,
                        timeout=300,
                    ) is None:
                        submitted_with_enter = True
                        break
        except Exception:
            submitted_with_enter = False
    submitted_with_click = False
    if not submitted_with_enter:
        max_attempts = MAX_NO_RESPONSE_CLICK_ATTEMPTS if monitor.available else 1
        for attempt in range(1, max_attempts + 1):
            current_value = input_value(email_input)
            if current_value is not None and current_value.strip() != email.strip():
                fill = getattr(email_input, "fill", None)
                if not callable(fill):
                    raise RuntimeError("邮箱提交前回读不一致，无法重新填写")
                try:
                    fill(email, timeout=5000)
                except TypeError:
                    fill(email)
                if (input_value(email_input) or "").strip() != email.strip():
                    raise RuntimeError("邮箱提交前重填一次后回读仍不一致")
            before_activity = registration_activity_snapshot(page)
            clicked = click_email_submit(
                page,
                first_visible=first_visible,
                submit_selectors=submit_selectors,
                allowed_labels=submit_allowed_labels,
                anchor_input=email_input,
                selection_log=log,
            )
            if not clicked:
                break
            mark_page_registration_milestone(
                page,
                "email_submitted",
                f"第 {attempt} 次点击邮箱提交按钮",
            )
            begin_page_registration_step(
                page,
                "email_responded",
                f"第 {attempt} 次点击后静默等待 {CLICK_RESPONSE_SECONDS:g} 秒",
            )
            if not monitor.available:
                submitted_with_click = True
                break
            activity = wait_for_registration_activity(
                page,
                before_activity,
                timeout_seconds=CLICK_RESPONSE_SECONDS,
                wait=wait,
                transition=lambda: (
                    str(getattr(page, "url", "") or "") != before_url
                    or first_visible(
                        page,
                        OPENAI_EMAIL_LOGIN_INPUT_SELECTORS,
                        timeout=300,
                    )
                    is None
                ),
            )
            if activity.get("changed"):
                mark_page_registration_milestone(
                    page,
                    "email_responded",
                    f"第 {attempt} 次点击后检测到请求或页面变化",
                )
                submitted_with_click = True
                break
            log(
                f"[请求监测] 邮箱提交第 {attempt} 次点击后 "
                f"{CLICK_RESPONSE_SECONDS:g} 秒无请求、无响应且页面未变化"
            )
        if not submitted_with_click and monitor.available and clicked:
            raise RuntimeError(
                f"邮箱提交最多点击 {MAX_NO_RESPONSE_CLICK_ATTEMPTS} 次后"
                "仍无请求或页面响应"
            )
    if not submitted_with_enter and not submitted_with_click:
        labels = visible_email_submit_labels(page)
        log(
            "[AUTH_EMAIL_SUBMIT_CANDIDATES] 当前可见按钮文字："
            + (" | ".join(labels) if labels else "未读取到可见按钮")
        )
        if allow_enter_submit:
            raise RuntimeError(
                "邮箱已粘贴，但按 Enter 后表单未提交，且未找到非社交登录的继续按钮"
            )
        raise RuntimeError(
            "注册邮箱已粘贴，但未找到继续/创建账号按钮；未点击登录按钮"
        )
    if submitted_with_enter:
        mark_page_registration_milestone(page, "email_submitted", "按 Enter 提交邮箱")
        begin_page_registration_step(
            page,
            "email_responded",
            f"按 Enter 后静默等待 {CLICK_RESPONSE_SECONDS:g} 秒",
        )
        mark_page_registration_milestone(page, "email_responded", "邮箱提交已有响应")
    emit_browser_diagnostic(
        log,
        BrowserDiagnosticCode.AUTH_EMAIL_SUBMIT,
        submit_diagnostic_message,
    )
    wait(page, 400)
