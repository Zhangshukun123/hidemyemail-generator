"""Fast, side-effect-free recognition of the current OpenAI registration page."""

from __future__ import annotations

import re
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


# Registration pages are small, and the status is consumed by a local UI.  A
# one-second heartbeat keeps the display responsive without changing or
# re-submitting any browser controls.  Five seconds is only an early warning;
# the existing bounded step timeouts remain the authority for stopping a task.
STATE_HEARTBEAT_SECONDS = 1.0
STATE_STALL_SCREENSHOT_SECONDS = 5.0

_SECURITY_MARKERS = (
    "security verification",
    "verify you are human",
    "checking your browser",
    "just a moment",
    "captcha",
    "cloudflare",
    "安全验证",
    "人机验证",
    "验证您是真人",
    "セキュリティ",
    "人間であることを確認",
    "私はロボットではありません",
)
_COMPLETED_MARKERS = (
    "you're all set",
    "you’re all set",
    "you are all set",
    "準備が完了しました",
    "准备就绪",
    "準備就緒",
)
_PROFILE_MARKERS = (
    "tell us about you",
    "date of birth",
    "full name",
    "about you",
    "お名前",
    "生年月日",
    "氏名",
    "出生日期",
    "你的姓名",
    "您的姓名",
)
_OTP_MARKERS = (
    "check your inbox",
    "verification code",
    "6-digit code",
    "one-time code",
    "受信箱を確認",
    "確認コード",
    "验证码",
    "驗證碼",
)
_PASSWORD_MARKERS = (
    "continue with password",
    "create a password",
    "enter your password",
    "パスワードで続行",
    "パスワードを作成",
    "使用密码继续",
    "创建密码",
    "使用密碼繼續",
)
_EMAIL_MARKERS = (
    "email address",
    "continue with email",
    "create your account",
    "メールアドレス",
    "メールで続行",
    "电子邮件地址",
    "邮箱地址",
    "電郵地址",
)
_ERROR_MARKERS = (
    "something went wrong",
    "try again later",
    "too many requests",
    "access denied",
    "发生错误",
    "出了点问题",
    "稍后重试",
    "エラーが発生",
    "しばらくしてから",
)

_PASSWORD_SELECTORS = ('input[type="password"]', 'input[autocomplete="new-password"]')
_OTP_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    'input[name*="code" i]',
)
_PROFILE_SELECTORS = (
    'input[name="name"]',
    'input[autocomplete="name"]',
    'input[name*="birth" i]',
)
_EMAIL_SELECTORS = ('input[type="email"]', 'input[name="email"]')


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_route(page) -> str:
    value = str(getattr(page, "url", "") or "")
    try:
        parsed = urlparse(value)
    except (TypeError, ValueError):
        return "当前页面"
    host = str(parsed.hostname or "")
    path = str(parsed.path or "/")
    return f"{host}{path}"[:240] if host else path[:240]


def _safe_body_text(page) -> str:
    try:
        body = page.locator("body")
        text = body.inner_text(timeout=700)
    except Exception:
        return ""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:12000]


def _safe_ready_state(page) -> str:
    try:
        value = page.evaluate("() => document.readyState")
    except Exception:
        return "unknown"
    return str(value or "unknown")[:32]


def _selector_visible(page, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            matches = page.locator(selector)
            count = min(5, int(matches.count()))
        except Exception:
            continue
        for index in range(count):
            try:
                candidate = matches.nth(index)
                if bool(candidate.is_visible(timeout=300)):
                    return True
            except TypeError:
                try:
                    if bool(candidate.is_visible()):
                        return True
                except Exception:
                    continue
            except Exception:
                continue
    return False


def _contains(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _completed_steps(worker, code: str) -> list[str]:
    steps = ["浏览器与网络已准备"]
    if code not in {"home", "loading", "unknown"}:
        steps.append("已进入 OpenAI 认证")
    if code in {
        "password",
        "email_verification",
        "profile",
        "completed",
        "session",
    }:
        steps.append("邮箱已提交")
    if bool(getattr(worker, "_password_step_submitted", False)):
        steps.append("密码已提交")
    if code in {"profile", "completed", "session"}:
        steps.append("邮箱验证码已通过")
    if code in {"completed", "session"}:
        steps.append("基础资料已完成")
    if code == "session":
        steps.append("Session 已确认")
    return list(dict.fromkeys(steps))


def recognize_registration_page(
    worker,
    page,
    *,
    session_ready: bool = False,
    ocr_text: str = "",
) -> dict[str, Any]:
    """Describe the page without clicking, typing, or navigating."""

    route = _safe_route(page)
    lowered_route = route.casefold()
    body_text = _safe_body_text(page)
    folded = body_text.casefold()
    ocr_folded = re.sub(r"\s+", " ", str(ocr_text or "")).strip().casefold()
    combined = f"{folded} {ocr_folded}".strip()
    source = "dom" if body_text else "ocr" if ocr_text else "url"
    ready_state = _safe_ready_state(page)

    password_visible = _selector_visible(page, _PASSWORD_SELECTORS)
    otp_visible = _selector_visible(page, _OTP_SELECTORS)
    profile_visible = _selector_visible(page, _PROFILE_SELECTORS)
    email_visible = _selector_visible(page, _EMAIL_SELECTORS)

    if session_ready:
        code, label, stage = "session", "Session 已建立", "session"
        next_action, mode, confidence = "保存 Session、Cookie 和账号结果", "automatic", 100
    elif _contains(combined, _SECURITY_MARKERS) or any(
        marker in lowered_route
        for marker in ("challenge", "captcha", "security-check", "turnstile")
    ):
        code, label, stage = "security", "安全验证页", "security"
        next_action, mode, confidence = (
            "请手动完成安全验证；完成后程序会自动继续",
            "manual",
            99,
        )
    elif "accounts.google." in lowered_route or "google.com/signin" in lowered_route:
        code, label, stage = "google", "Google 登录页", "google_oauth"
        next_action, mode, confidence = (
            "返回 OpenAI 注册；无法返回时关闭当前窗口并更换指纹",
            "recovering",
            99,
        )
    elif _contains(combined, _ERROR_MARKERS):
        code, label, stage = "error", "OpenAI 错误页", "openai_auth"
        next_action, mode, confidence = "记录页面错误并停止本账号，避免空等", "error", 92
    elif _contains(combined, _COMPLETED_MARKERS):
        code, label, stage = "completed", "注册完成确认页", "profile"
        next_action, mode, confidence = "点击继续并确认 Session", "automatic", 98
    elif profile_visible or _contains(combined, _PROFILE_MARKERS) or "about-you" in lowered_route:
        code, label, stage = "profile", "姓名与出生信息页", "profile"
        next_action, mode, confidence = "填写并校验姓名、生日，然后提交一次", "automatic", 96
    elif otp_visible or _contains(combined, _OTP_MARKERS) or "email-verification" in lowered_route:
        code, label, stage = "email_verification", "邮箱验证码页", "email_verification"
        password_done = bool(getattr(worker, "_password_step_submitted", False))
        manual_otp = bool(getattr(worker, "_hme_manual_otp_entry", False))
        if manual_otp:
            next_action, mode = "等待你在浏览器输入邮箱验证码", "manual"
        elif not password_done and _contains(combined, _PASSWORD_MARKERS):
            next_action, mode = "选择“使用密码继续”，先完成密码设置", "automatic"
        else:
            next_action, mode = "读取本轮最新邮箱验证码并提交", "automatic"
        confidence = 98 if otp_visible else 90
    elif password_visible or "password" in lowered_route or _contains(combined, _PASSWORD_MARKERS):
        code, label, stage = "password", "OpenAI 密码页", "password"
        next_action, mode, confidence = "填写已保存的唯一密码并提交一次", "automatic", 97
    elif email_visible or _contains(combined, _EMAIL_MARKERS) or "auth.openai.com" in lowered_route:
        code, label, stage = "email", "OpenAI 邮箱认证页", "openai_auth"
        next_action, mode, confidence = "填写并校验邮箱，然后提交一次", "automatic", 92
    elif "chatgpt.com" in lowered_route:
        code, label, stage = "home", "ChatGPT 首页", "openai_auth"
        next_action, mode, confidence = "打开注册入口并进入邮箱认证", "automatic", 82
    elif ready_state != "complete":
        code, label, stage = "loading", "页面加载中", "browser"
        next_action, mode, confidence = "等待页面资源完成加载", "waiting", 70
    else:
        code, label, stage = "unknown", "未识别页面", "running"
        next_action, mode, confidence = "保存诊断截图并继续监测页面变化", "waiting", 35

    return {
        "code": code,
        "currentPage": label,
        "stage": stage,
        "completedSteps": _completed_steps(worker, code),
        "nextAction": next_action,
        "actionMode": mode,
        "confidence": confidence,
        "source": source,
        "route": route,
        "readyState": ready_state,
        "updatedAt": _utc_now(),
    }


def _save_state_screenshot(page, diagnostics_dir: Path | str, code: str) -> str:
    screenshot = getattr(page, "screenshot", None)
    if not callable(screenshot):
        return ""
    target_dir = Path(diagnostics_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = target_dir / f"{timestamp}-state-{code}.png"
    try:
        screenshot(path=str(target), full_page=True, timeout=2000)
    except TypeError:
        try:
            screenshot(path=str(target), full_page=True)
        except Exception:
            return ""
    except Exception:
        return ""
    return str(target)


def configure_registration_state_recognition(
    worker,
    *,
    emit_state: Callable[[dict[str, Any]], Any],
    diagnostics_dir: Path | str,
    monotonic: Callable[[], float] = time.monotonic,
    heartbeat_seconds: float = STATE_HEARTBEAT_SECONDS,
    screenshot_seconds: float = STATE_STALL_SCREENSHOT_SECONDS,
) -> bool:
    """Observe existing worker checkpoints and publish structured page state."""

    if getattr(worker, "_hme_state_recognition_configured", False):
        return True
    method_names = (
        "_continue_chatgpt_registration_complete",
        "_fill_email_if_visible",
        "_has_visible_password",
        "_has_otp_input",
        "_has_about_you_form",
        "_fill_password_step",
        "_submit_email_code",
        "_fill_about_you",
        "_wait_for_auth_page_ready",
        "_wait_after_otp_submit",
        "_has_chatgpt_session",
    )
    available = [name for name in method_names if callable(getattr(worker, name, None))]
    if not available:
        return False

    worker._hme_state_key = ""
    worker._hme_state_first_seen_at = 0.0
    worker._hme_state_last_emitted_at = 0.0
    worker._hme_state_screenshot_key = ""

    def observe(self, page, *, session_ready: bool = False, force: bool = False):
        state = recognize_registration_page(
            self,
            page,
            session_ready=session_ready,
        )
        key = f"{state['code']}|{state['route']}|{state['nextAction']}"
        now = monotonic()
        changed = key != str(getattr(self, "_hme_state_key", "") or "")
        if changed:
            self._hme_state_key = key
            self._hme_state_first_seen_at = now
            self._hme_state_screenshot_key = ""
        first_seen_at = getattr(self, "_hme_state_first_seen_at", now)
        try:
            first_seen_at = float(first_seen_at)
        except (TypeError, ValueError):
            first_seen_at = now
        stalled_seconds = max(0, int(now - first_seen_at))
        state["stalledSeconds"] = stalled_seconds
        state["stalled"] = stalled_seconds >= max(1.0, float(screenshot_seconds))
        if state["stalled"] and self._hme_state_screenshot_key != key:
            screenshot_path = _save_state_screenshot(
                page,
                diagnostics_dir,
                str(state.get("code") or "unknown"),
            )
            if screenshot_path:
                state["diagnosticScreenshot"] = screenshot_path
            self._hme_state_screenshot_key = key
        last_emitted = float(getattr(self, "_hme_state_last_emitted_at", 0.0) or 0.0)
        should_emit = changed or force or now - last_emitted >= max(
            1.0, float(heartbeat_seconds)
        )
        if not should_emit:
            return state
        self._hme_state_last_emitted_at = now
        emit_state(state)
        done = "、".join(state["completedSteps"]) or "尚无"
        stall = f"；已停留 {stalled_seconds} 秒" if stalled_seconds else ""
        self.log(
            f"[状态识别] 当前={state['currentPage']}；已完成={done}；"
            f"下一步={state['nextAction']}；来源={state['source']}；"
            f"置信度={state['confidence']}%{stall}"
        )
        return state

    worker._recognize_registration_state = types.MethodType(observe, worker)

    for method_name in available:
        original = getattr(worker, method_name)

        def observed_method(
            self,
            page,
            *args,
            _method_name=method_name,
            _original=original,
            **kwargs,
        ):
            self._recognize_registration_state(page)
            result = _original(page, *args, **kwargs)
            self._recognize_registration_state(
                page,
                session_ready=bool(
                    _method_name == "_has_chatgpt_session" and result
                ),
            )
            return result

        setattr(worker, method_name, types.MethodType(observed_method, worker))

    worker._hme_state_recognition_configured = True
    return True


__all__ = [
    "configure_registration_state_recognition",
    "recognize_registration_page",
]
