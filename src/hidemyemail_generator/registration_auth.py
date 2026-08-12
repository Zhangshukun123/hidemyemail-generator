from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from urllib.parse import urlparse

try:
    from .browser_diagnostics import BrowserDiagnosticCode, emit_browser_diagnostic
except ImportError:
    from browser_diagnostics import BrowserDiagnosticCode, emit_browser_diagnostic


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
_EMAIL_REGISTRATION_ACTION_LABELS = (
    "Continue",
    "继续",
    "继续注册",
    "続行",
    "続ける",
    "Create account",
    "Create an account",
    "创建账号",
    "建立帳戶",
    "アカウントを作成",
)
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
_HOME_EMAIL_ENTRY_ALREADY_OPEN = object()


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
    email_entry_already_open: Callable | None = None,
):
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        if (
            email_entry_already_open is not None
            and email_entry_already_open(page)
        ):
            return _HOME_EMAIL_ENTRY_ALREADY_OPEN
        candidate = first_visible(page, selectors, timeout=700)
        if candidate is None and semantic_candidate is not None:
            candidate = semantic_candidate(page)
        if candidate is not None and locator_is_enabled(candidate):
            return candidate
        wait(page, 250)
    return None


def _transitioned(page, before_url: str, *, first_visible: Callable) -> bool:
    current_url = str(getattr(page, "url", "") or "")
    if current_url and current_url != before_url:
        return True
    if is_openai_auth_url(current_url):
        return True
    return (
        first_visible(page, OPENAI_EMAIL_LOGIN_INPUT_SELECTORS, timeout=300)
        is not None
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

    def email_entry_already_open(target_page) -> bool:
        return (
            first_visible(
                target_page,
                OPENAI_EMAIL_LOGIN_INPUT_SELECTORS,
                timeout=300,
            )
            is not None
        )

    def use_open_email_entry(candidate) -> bool:
        if candidate is not _HOME_EMAIL_ENTRY_ALREADY_OPEN:
            return False
        log(
            "[认证] 已识别 ChatGPT 首页右侧登录或注册抽屉；"
            f"邮箱框已经可用，跳过被抽屉覆盖的{entry_label}按钮"
        )
        return True

    candidate = _wait_for_candidate(
        page,
        entry_selectors,
        timeout_seconds=timeout_seconds,
        first_visible=first_visible,
        wait=wait,
        semantic_candidate=semantic_candidate,
        email_entry_already_open=email_entry_already_open,
    )
    if use_open_email_entry(candidate):
        return True
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
                email_entry_already_open=email_entry_already_open,
            )
        if use_open_email_entry(candidate):
            return True
        if candidate is None:
            raise RuntimeError(
                f"ChatGPT 首页重新加载后仍未找到可用的{entry_label}按钮"
            )

    before_url = str(getattr(page, "url", "") or "")
    try:
        if activate is not None:
            activate(page)
        _click_candidate(candidate)
    except RuntimeError as error:
        if _wait_for_transition(
            page,
            before_url,
            timeout_seconds=3.0,
            first_visible=first_visible,
            wait=wait,
        ):
            emit_browser_diagnostic(
                log,
                BrowserDiagnosticCode.AUTH_HOME_TRANSITION,
                f"{entry_label}点击等待超时，但页面已经发生变化，继续当前流程",
                attempt=1,
            )
            return True
        raise RuntimeError(
            f"ChatGPT 首页{entry_label}按钮点击失败"
        ) from error
    emit_browser_diagnostic(
        log,
        BrowserDiagnosticCode.AUTH_HOME_LOGIN_CLICK,
        f"ChatGPT 首页已第一次点击{entry_label}；正在等待页面变化",
        attempt=1,
    )
    if _wait_for_transition(
        page,
        before_url,
        timeout_seconds=transition_timeout_seconds,
        first_visible=first_visible,
        wait=wait,
    ):
        emit_browser_diagnostic(
            log,
            BrowserDiagnosticCode.AUTH_HOME_TRANSITION,
            f"第一次点击{entry_label}后页面已跳转",
            attempt=1,
        )
        return True

    retry_candidate = _wait_for_candidate(
        page,
        entry_selectors,
        timeout_seconds=5.0,
        first_visible=first_visible,
        wait=wait,
        semantic_candidate=semantic_candidate,
        email_entry_already_open=email_entry_already_open,
    )
    if use_open_email_entry(retry_candidate):
        return True
    if retry_candidate is None or not is_chatgpt_homepage(
        str(getattr(page, "url", "") or "")
    ):
        raise RuntimeError(
            f"第一次点击{entry_label}后页面未变化，且按钮已不可再次操作"
        )
    emit_browser_diagnostic(
        log,
        BrowserDiagnosticCode.AUTH_HOME_LOGIN_RETRY,
        f"第一次点击{entry_label}后页面仍停留在 ChatGPT 首页；"
        f"已确认{entry_label}按钮可用，正在第二次点击",
        attempt=2,
    )
    try:
        if activate is not None:
            activate(page)
        _click_candidate(retry_candidate)
    except RuntimeError as error:
        if _wait_for_transition(
            page,
            before_url,
            timeout_seconds=3.0,
            first_visible=first_visible,
            wait=wait,
        ):
            emit_browser_diagnostic(
                log,
                BrowserDiagnosticCode.AUTH_HOME_TRANSITION,
                f"第二次点击{entry_label}等待超时，但页面已经发生变化",
                attempt=2,
            )
            return True
        raise RuntimeError(
            f"ChatGPT 首页{entry_label}按钮第二次点击失败"
        ) from error
    if _wait_for_transition(
        page,
        before_url,
        timeout_seconds=transition_timeout_seconds,
        first_visible=first_visible,
        wait=wait,
    ):
        emit_browser_diagnostic(
            log,
            BrowserDiagnosticCode.AUTH_HOME_TRANSITION,
            f"第二次点击{entry_label}后页面已跳转",
            attempt=2,
        )
        return True
    raise RuntimeError(
        f"两次点击{entry_label}后页面仍未跳转；已停止继续点击"
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
    if pasted_value is not None and pasted_value.strip() != email.strip():
        fill = getattr(email_input, "fill", None)
        if not callable(fill):
            raise RuntimeError("Ctrl+V 后邮箱输入框内容校验失败")
        try:
            fill(email, timeout=5000)
        except TypeError:
            fill(email)
        wait(page, 250)
        pasted_value = input_value(email_input)
        if pasted_value is not None and pasted_value.strip() != email.strip():
            raise RuntimeError("后台 DOM 填写后邮箱输入框内容校验失败")
        log("[认证] 无头/后台剪贴板未生效，已改用输入框 DOM 填写并校验")
    emit_browser_diagnostic(
        log,
        BrowserDiagnosticCode.AUTH_EMAIL_PASTE,
        "邮箱已通过系统剪贴板粘贴并完成回读校验",
    )
    activate(page)
    submitted_with_enter = False
    before_url = str(getattr(page, "url", "") or "")
    if allow_enter_submit:
        try:
            try:
                email_input.press("Enter", timeout=5000)
            except TypeError:
                email_input.press("Enter")
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
    if not submitted_with_enter and not click_email_submit(
        page,
        first_visible=first_visible,
        submit_selectors=submit_selectors,
        allowed_labels=submit_allowed_labels,
        anchor_input=email_input,
        selection_log=log,
    ):
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
    emit_browser_diagnostic(
        log,
        BrowserDiagnosticCode.AUTH_EMAIL_SUBMIT,
        submit_diagnostic_message,
    )
    wait(page, 400)
