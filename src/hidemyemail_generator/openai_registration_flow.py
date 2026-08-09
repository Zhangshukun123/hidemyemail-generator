"""Registration-specific worker hooks and navigation policy."""

from __future__ import annotations

import os
import re
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

try:
    from .browser_platform import (
        copy_registration_clipboard_text,
        registration_clipboard_lock,
    )
    from .browser_diagnostics import BrowserDiagnosticCode, emit_browser_diagnostic
    from .openai_browser_dom import (
        _activate_visible_registration_page,
        _click_first_visible,
        _first_visible,
        _page_wait,
    )
    from .openai_browser_selectors import (
        PASSWORD_CONTINUE_SELECTORS,
        PASSWORD_ENTRY_SECURITY_MARKERS,
        PASSWORD_ENTRY_STATUS_INTERVAL_SECONDS,
        PASSWORD_RESET_CONFIRM_CONTINUE_SELECTORS,
        PASSWORD_RESET_CONFIRM_MARKERS,
    )
    from . import registration_auth as _registration_auth
except ImportError:
    from browser_platform import (
        copy_registration_clipboard_text,
        registration_clipboard_lock,
    )
    from browser_diagnostics import BrowserDiagnosticCode, emit_browser_diagnostic
    from openai_browser_dom import (
        _activate_visible_registration_page,
        _click_first_visible,
        _first_visible,
        _page_wait,
    )
    from openai_browser_selectors import (
        PASSWORD_CONTINUE_SELECTORS,
        PASSWORD_ENTRY_SECURITY_MARKERS,
        PASSWORD_ENTRY_STATUS_INTERVAL_SECONDS,
        PASSWORD_RESET_CONFIRM_CONTINUE_SELECTORS,
        PASSWORD_RESET_CONFIRM_MARKERS,
    )
    import registration_auth as _registration_auth

_auth_click_email_submit = _registration_auth.click_email_submit
_auth_input_value = _registration_auth.input_value
_auth_paste_email_and_submit = _registration_auth.paste_email_and_submit
OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS = (
    _registration_auth.OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS
)
_REGISTRATION_CLIPBOARD_LOCK = registration_clipboard_lock()
_copy_registration_clipboard_text = copy_registration_clipboard_text


def _locator_state(candidate, method: str, *, default: bool) -> bool:
    callback = getattr(candidate, method, None)
    if not callable(callback):
        return default
    try:
        return bool(callback(timeout=300))
    except TypeError:
        try:
            return bool(callback())
        except Exception:
            return False
    except Exception:
        return False


def _control_metrics(
    page, selectors, *, require_editable: bool
) -> dict[str, int | bool]:
    metrics: dict[str, int | bool] = {
        "matched": 0,
        "visible": 0,
        "enabled": 0,
        "editable": 0,
        "actionable": False,
    }
    for selector in tuple(str(item) for item in selectors if str(item)):
        try:
            locator = page.locator(selector)
            count = min(int(locator.count()), 12)
        except Exception:
            continue
        metrics["matched"] = int(metrics["matched"]) + count
        for index in range(count):
            try:
                candidate = locator.nth(index)
            except Exception:
                continue
            visible = _locator_state(candidate, "is_visible", default=False)
            if not visible:
                continue
            metrics["visible"] = int(metrics["visible"]) + 1
            enabled = _locator_state(candidate, "is_enabled", default=True)
            if not enabled:
                continue
            metrics["enabled"] = int(metrics["enabled"]) + 1
            editable = _locator_state(candidate, "is_editable", default=True)
            if editable:
                metrics["editable"] = int(metrics["editable"]) + 1
            if editable or not require_editable:
                metrics["actionable"] = True
    return metrics


def _password_readiness_snapshot(page, selectors, *, require_editable: bool) -> dict:
    url = str(getattr(page, "url", "") or "")
    try:
        parsed = urlparse(url)
        route = f"{parsed.netloc}{parsed.path}".strip("/") or "当前页面"
    except (TypeError, ValueError):
        route = "当前页面"
    try:
        page_state = page.evaluate(
            """() => ({
                readyState: document.readyState,
                styleSheetCount: Array.from(document.styleSheets || []).length,
                loadedStyleLinkCount: Array.from(document.querySelectorAll('link[rel~="stylesheet"]')).filter((link) => Boolean(link.sheet)).length
            })"""
        )
    except Exception:
        page_state = {}
    if not isinstance(page_state, dict):
        page_state = {"readyState": str(page_state or "")}
    inputs = _control_metrics(page, selectors, require_editable=require_editable)
    submits = _control_metrics(
        page,
        PASSWORD_CONTINUE_SELECTORS,
        require_editable=False,
    )
    return {
        "route": route[:180],
        "readyState": str(page_state.get("readyState") or "未知")[:32],
        "styleSheetCount": int(page_state.get("styleSheetCount") or 0),
        "loadedStyleLinkCount": int(page_state.get("loadedStyleLinkCount") or 0),
        "securityChallenge": _security_challenge_visible(page),
        "inputs": inputs,
        "submits": submits,
    }


def _password_snapshot_summary(snapshot: dict) -> str:
    inputs = snapshot.get("inputs") or {}
    submits = snapshot.get("submits") or {}
    return (
        f"URL={snapshot.get('route') or '当前页面'}；"
        f"readyState={snapshot.get('readyState') or '未知'}；"
        f"CSS={snapshot.get('styleSheetCount', 0)}/"
        f"{snapshot.get('loadedStyleLinkCount', 0)}；"
        "密码匹配/可见/启用/可编辑="
        f"{inputs.get('matched', 0)}/{inputs.get('visible', 0)}/"
        f"{inputs.get('enabled', 0)}/{inputs.get('editable', 0)}；"
        "提交匹配/可见/启用="
        f"{submits.get('matched', 0)}/{submits.get('visible', 0)}/"
        f"{submits.get('enabled', 0)}；"
        f"安全验证={'是' if snapshot.get('securityChallenge') else '否'}"
    )


def _save_password_diagnostic_screenshot(page, diagnostics_dir: Path | str) -> str:
    target_dir = Path(diagnostics_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = target_dir / f"{timestamp}-pid{os.getpid()}-password-timeout.png"
    screenshot = getattr(page, "screenshot", None)
    if not callable(screenshot):
        return ""
    try:
        screenshot(path=str(target), full_page=True, timeout=10000)
    except TypeError:
        screenshot(path=str(target), full_page=True)
    except Exception:
        return ""
    return str(target)


def configure_password_readiness_diagnostics(
    worker, *, diagnostics_dir: Path | str
) -> bool:
    """Add periodic, secret-free diagnostics to password-control readiness waits."""

    original_wait = getattr(worker, "_wait_for_auth_page_ready", None)
    if not callable(original_wait):
        return False
    if getattr(worker, "_hme_password_readiness_diagnostics_configured", False):
        return True

    def wait_with_password_diagnostics(
        self,
        page,
        action: str,
        *,
        ready_selectors=(),
        require_editable: bool = False,
        timeout_seconds: float = 60.0,
    ) -> None:
        if "密码" not in str(action) and "password" not in str(action).casefold():
            return original_wait(
                page,
                action,
                ready_selectors=ready_selectors,
                require_editable=require_editable,
                timeout_seconds=timeout_seconds,
            )
        selectors = tuple(str(item) for item in ready_selectors if str(item))
        started_at = time.monotonic()
        timeout_value = max(1.0, float(timeout_seconds))
        wait_for_load_state = getattr(page, "wait_for_load_state", None)
        if callable(wait_for_load_state):
            try:
                wait_for_load_state(
                    "load", timeout=max(1000, int(timeout_value * 1000))
                )
            except Exception as error:
                snapshot = _password_readiness_snapshot(
                    page,
                    selectors,
                    require_editable=require_editable,
                )
                summary = _password_snapshot_summary(snapshot)
                emit_browser_diagnostic(
                    self.log,
                    BrowserDiagnosticCode.AUTH_PASSWORD_TIMEOUT,
                    f"{action}页面未完成加载；{summary}",
                )
                raise RuntimeError(f"{action}页面未完成加载；{summary}") from error
        if not selectors:
            return

        deadline = started_at + timeout_value
        stable_checks = 0
        last_logged_at = 0.0
        latest_snapshot: dict = {}
        while time.monotonic() < deadline:
            latest_snapshot = _password_readiness_snapshot(
                page,
                selectors,
                require_editable=require_editable,
            )
            actionable = bool(
                (latest_snapshot.get("inputs") or {}).get("actionable")
            )
            stable_checks = stable_checks + 1 if actionable else 0
            now = time.monotonic()
            if last_logged_at == 0.0 or now - last_logged_at >= 10.0:
                emit_browser_diagnostic(
                    self.log,
                    BrowserDiagnosticCode.AUTH_PASSWORD_WAIT,
                    f"{action}控件监测（已等待 {max(0, int(now - started_at))} 秒）；"
                    + _password_snapshot_summary(latest_snapshot),
                )
                last_logged_at = now
            if stable_checks >= 2:
                emit_browser_diagnostic(
                    self.log,
                    BrowserDiagnosticCode.AUTH_PASSWORD_READY,
                    f"{action}控件已连续两次可操作（耗时 "
                    f"{max(0, int(now - started_at))} 秒）；"
                    + _password_snapshot_summary(latest_snapshot),
                )
                return
            time.sleep(0.25)

        if not latest_snapshot:
            latest_snapshot = _password_readiness_snapshot(
                page,
                selectors,
                require_editable=require_editable,
            )
        summary = _password_snapshot_summary(latest_snapshot)
        screenshot_path = _save_password_diagnostic_screenshot(
            page,
            diagnostics_dir,
        )
        emit_browser_diagnostic(
            self.log,
            BrowserDiagnosticCode.AUTH_PASSWORD_TIMEOUT,
            f"{action}控件等待 {int(timeout_value)} 秒后超时；{summary}",
        )
        if screenshot_path:
            emit_browser_diagnostic(
                self.log,
                BrowserDiagnosticCode.AUTH_PASSWORD_SCREENSHOT,
                f"已保存密码页诊断截图：{screenshot_path}",
            )
        raise RuntimeError(
            f"{action}页面已加载，但目标控件在 {int(timeout_value)} 秒内仍不可操作；"
            + summary
        )

    worker._wait_for_auth_page_ready = types.MethodType(
        wait_with_password_diagnostics,
        worker,
    )
    worker._hme_password_readiness_diagnostics_configured = True
    return True


def _security_challenge_visible(page) -> bool:
    url = str(getattr(page, "url", "") or "").casefold()
    if any(
        marker in url
        for marker in (
            "challenge",
            "captcha",
            "verify",
            "security-check",
            "turnstile",
        )
    ):
        return True
    try:
        body_text = str(page.locator("body").inner_text(timeout=700) or "")
    except Exception:
        return False
    folded_text = re.sub(r"\s+", " ", body_text).strip().casefold()
    return any(
        marker.casefold() in folded_text for marker in PASSWORD_ENTRY_SECURITY_MARKERS
    )


def configure_password_first_login(
    worker,
    *,
    enabled: bool,
    required: bool = False,
    password_choice_timeout_seconds: float = 30.0,
) -> bool:
    """Choose password on the initial email-code page and submit the saved password."""

    original_has_otp = getattr(worker, "_has_otp_input", None)
    original_has_password = getattr(worker, "_has_visible_password", None)
    original_has_password_auth_error = getattr(
        worker,
        "_has_password_auth_error",
        None,
    )
    original_fill_password = getattr(worker, "_fill_password_step", None)
    original_continue_registration = getattr(
        worker, "_continue_chatgpt_registration_complete", None
    )
    if (
        not enabled
        or not callable(original_has_otp)
        or not callable(original_fill_password)
    ):
        return False
    if getattr(worker, "_hme_password_first_login_configured", False):
        return True

    def password_entry_page_state(page, *, has_otp: bool) -> tuple[str, str]:
        url = str(getattr(page, "url", "") or "")
        lowered_url = url.casefold()
        try:
            parsed = urlparse(url)
            route = f"{parsed.netloc}{parsed.path}".strip("/") or "当前页面"
        except (TypeError, ValueError):
            route = "当前页面"
        if _security_challenge_visible(page):
            return "security", "安全验证页面"
        if has_otp or "email-verification" in lowered_url:
            return "email_verification", "邮箱验证页面"
        if any(
            marker in lowered_url
            for marker in ("/log-in", "/login", "/sign-in", "/signup")
        ):
            return "login", "登录页面"
        try:
            ready_state = str(page.evaluate("() => document.readyState") or "")
        except Exception:
            ready_state = ""
        if ready_state and ready_state != "complete":
            return "loading", "页面仍在加载"
        return "other", route[:160]

    def monitor_password_entry_wait(self, page, *, has_otp: bool) -> None:
        now = time.monotonic()
        started_at = float(getattr(self, "_hme_password_entry_started_at", 0) or now)
        state, detail = password_entry_page_state(page, has_otp=has_otp)
        previous_state = str(getattr(self, "_hme_password_entry_wait_state", "") or "")
        last_logged_at = float(
            getattr(self, "_hme_password_entry_wait_logged_at", 0) or 0
        )
        if (
            state == previous_state
            and now - last_logged_at < PASSWORD_ENTRY_STATUS_INTERVAL_SECONDS
        ):
            return
        elapsed = max(0, int(now - started_at))
        if state == "security":
            message = (
                "[认证] 已检测到安全验证，请在当前浏览器完成；"
                f"程序保持登录流程并继续监测（已等待 {elapsed} 秒）"
            )
        else:
            message = (
                f"[认证] 等待密码页面：当前为{detail}；"
                f"继续监测，不会因 15 秒未跳转而退出（已等待 {elapsed} 秒）"
            )
        self.log(message)
        self._hme_password_entry_wait_state = state
        self._hme_password_entry_wait_logged_at = now

    def click_password_reset_confirmation(self, page) -> bool:
        try:
            route = urlparse(str(getattr(page, "url", "") or "")).path.rstrip("/")
        except (TypeError, ValueError):
            return False
        if route != "/reset-password":
            return False
        try:
            body_text = str(page.locator("body").inner_text(timeout=800) or "")
        except Exception:
            body_text = ""
        normalized = re.sub(r"\s+", " ", body_text).strip().casefold()
        if not any(marker.casefold() in normalized for marker in PASSWORD_RESET_CONFIRM_MARKERS):
            return False
        clicked_at = float(
            getattr(self, "_hme_password_reset_confirm_clicked_at", 0.0) or 0.0
        )
        now = time.monotonic()
        if clicked_at:
            last_logged_at = float(
                getattr(self, "_hme_password_reset_wait_logged_at", 0.0) or 0.0
            )
            if now - last_logged_at >= PASSWORD_ENTRY_STATUS_INTERVAL_SECONDS:
                emit_browser_diagnostic(
                    self.log,
                    BrowserDiagnosticCode.AUTH_PASSWORD_RESET_WAIT,
                    "已点击密码重置确认，页面尚未跳转；继续等待验证码页",
                )
                self._hme_password_reset_wait_logged_at = now
            return False
        _activate_visible_registration_page(self, page)
        if not _click_first_visible(
            page,
            PASSWORD_RESET_CONFIRM_CONTINUE_SELECTORS,
            timeout=800,
        ):
            return False
        self._hme_password_reset_confirm_clicked_at = now
        self._hme_password_reset_wait_logged_at = now
        emit_browser_diagnostic(
            self.log,
            BrowserDiagnosticCode.AUTH_PASSWORD_RESET_CONTINUE,
            "已识别密码重置确认页并单次点击继续；等待邮箱验证码页",
        )
        return True

    def choose_password_if_available(self, page) -> bool:
        if getattr(self, "_hme_password_entry_selected", False) or getattr(
            self, "_password_step_submitted", False
        ):
            return False
        before_url = str(getattr(page, "url", "") or "")
        _activate_visible_registration_page(self, page)
        if not _click_first_visible(page, PASSWORD_CONTINUE_SELECTORS, timeout=500):
            return False
        self._hme_password_entry_selected = True
        self._hme_password_entry_pending = True
        self._hme_password_entry_started_at = time.monotonic()
        self._hme_password_entry_wait_state = ""
        self._hme_password_entry_wait_logged_at = 0.0
        self.log(
            "[认证] 已选择使用密码继续；等待密码输入页面，"
            "如出现安全验证可手动完成，程序会持续监测"
        )
        if before_url:
            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                current_url = str(getattr(page, "url", "") or "")
                password_visible = bool(
                    callable(original_has_password)
                    and original_has_password(page)
                )
                if password_visible or (
                    current_url
                    and current_url != before_url
                    and "email-verification" not in current_url.casefold()
                ):
                    try:
                        route = urlparse(current_url).path or "/"
                    except (TypeError, ValueError):
                        route = "/"
                    emit_browser_diagnostic(
                        self.log,
                        BrowserDiagnosticCode.AUTH_PASSWORD_ROUTE_TRANSITION,
                        f"使用密码入口已完成页面切换：{route}；"
                        "未启动邮箱验证码读取",
                    )
                    return True
                monitor_password_entry_wait(
                    self,
                    page,
                    has_otp=bool(original_has_otp(page)),
                )
                _page_wait(page, 250)
            raise RuntimeError(
                "点击使用密码继续后 60 秒内仍未进入密码页；"
                "已停止邮箱验证码读取，避免重新登录到错误分支"
            )
        return True

    def wait_for_required_password_choice(self, page) -> bool:
        url = str(getattr(page, "url", "") or "").casefold()
        if "email-verification" not in url:
            return False
        timeout_seconds = max(0.0, float(password_choice_timeout_seconds))
        timeout_label = f"{timeout_seconds:g}"
        deadline = time.monotonic() + timeout_seconds
        otp_seen = False
        while time.monotonic() < deadline:
            if choose_password_if_available(self, page):
                return True
            current_url = str(getattr(page, "url", "") or "").casefold()
            password_visible = bool(
                callable(original_has_password) and original_has_password(page)
            )
            if password_visible or (
                "password" in current_url
                and "email-verification" not in current_url
            ):
                self.log(
                    "[认证] 等待使用密码入口期间已进入密码页面；"
                    "未启动 SMSBower 验证码读取"
                )
                return True
            has_otp = bool(original_has_otp(page))
            if has_otp and not otp_seen:
                otp_seen = True
                self.log(
                    "[认证] 邮箱验证码框已先出现，但使用密码继续入口可能仍在异步加载；"
                    f"继续等待最多 {timeout_label} 秒，期间不读取 SMSBower 验证码"
                )
            if "email-verification" not in current_url:
                return False
            _page_wait(page, 250)
        raise RuntimeError(
            f"Gmail 注册在邮箱验证页完整等待 {timeout_label} 秒仍未出现使用密码继续入口；"
            "已拒绝创建免密码账号"
        )

    def continue_registration_and_choose_password(self, page):
        password_path_started = bool(
            getattr(self, "_hme_password_entry_selected", False)
            or getattr(self, "_hme_password_entry_pending", False)
            or getattr(self, "_password_step_submitted", False)
        )
        if required and not password_path_started:
            if choose_password_if_available(self, page):
                return True
            if wait_for_required_password_choice(self, page):
                return True
            if original_has_otp(page):
                raise RuntimeError(
                    "Gmail 注册必须先选择使用密码继续；"
                    "已停止首次验证码入口，避免点击错误验证方式"
                )
        result = original_continue_registration(page)
        if choose_password_if_available(self, page):
            return result
        if (
            required
            and not getattr(self, "_password_step_submitted", False)
            and original_has_otp(page)
        ):
            raise RuntimeError(
                "Gmail 注册未经过密码输入就进入邮箱验证码；"
                "已停止并拒绝保存免密码账号"
            )
        if (
            not getattr(self, "_hme_password_entry_selected", False)
            and not getattr(self, "_password_step_submitted", False)
            and original_has_otp(page)
            and not getattr(self, "_hme_password_entry_unavailable_logged", False)
        ):
            self._hme_password_entry_unavailable_logged = True
            self.log("[认证] 当前验证码页面没有使用密码入口，继续读取邮箱验证码")
        return result

    def has_otp_or_choose_password(self, page):
        if click_password_reset_confirmation(self, page):
            return False
        has_otp = bool(original_has_otp(page))
        if getattr(self, "_hme_password_entry_pending", False):
            monitor_password_entry_wait(self, page, has_otp=has_otp)
            return False
        if not has_otp:
            return False
        if getattr(self, "_hme_password_entry_selected", False) or getattr(
            self, "_password_step_submitted", False
        ):
            return has_otp
        if not choose_password_if_available(self, page):
            if required:
                raise RuntimeError(
                    "Gmail 注册必须使用密码继续；当前页面未找到密码入口，已停止验证码回退"
                )
            if not getattr(self, "_hme_password_entry_unavailable_logged", False):
                self._hme_password_entry_unavailable_logged = True
                self.log("[认证] 当前验证码页面没有使用密码入口，继续读取邮箱验证码")
            return has_otp
        return False

    def fill_saved_password(self, page):
        original_fill_password(page)
        self._hme_password_entry_pending = False
        self._password_step_submitted = True
        self.log("[认证] 已提交创建邮箱时保存的唯一密码")

    def reject_existing_account_password_error(self, page) -> bool:
        detected = bool(original_has_password_auth_error(page))
        if (
            detected
            and required
            and not bool(getattr(self, "existing_login_only", False))
        ):
            try:
                route = urlparse(str(getattr(page, "url", "") or "")).path or "/"
            except (TypeError, ValueError):
                route = "/"
            emit_browser_diagnostic(
                self.log,
                BrowserDiagnosticCode.AUTH_EXISTING_ACCOUNT_REJECTED,
                f"新购 Gmail 在 {route} 被 OpenAI 判定为已有账号；"
                "已停止本次注册，不执行忘记密码或密码重置",
            )
            raise RuntimeError(
                "SMSBower Gmail 已存在 OpenAI 账号，不能作为新注册邮箱；"
                "已停止密码重置，请重新获取未注册邮箱"
            )
        return detected

    worker._has_otp_input = types.MethodType(has_otp_or_choose_password, worker)
    worker._fill_password_step = types.MethodType(fill_saved_password, worker)
    if callable(original_has_password_auth_error):
        worker._has_password_auth_error = types.MethodType(
            reject_existing_account_password_error,
            worker,
        )
    if callable(original_continue_registration):
        worker._continue_chatgpt_registration_complete = types.MethodType(
            continue_registration_and_choose_password, worker
        )
    worker._hme_password_first_login_configured = True
    return True


def configure_email_verification_priority(worker) -> bool:
    """Keep OpenAI's email-code control out of the password branch."""

    original_has_password = getattr(worker, "_has_visible_password", None)
    if not callable(original_has_password):
        return False
    if getattr(worker, "_hme_email_verification_priority_configured", False):
        return True

    def has_password_outside_email_verification(self, page) -> bool:
        url = str(getattr(page, "url", "") or "").casefold()
        if "email-verification" in url:
            return False
        return bool(original_has_password(page))

    worker._has_visible_password = types.MethodType(
        has_password_outside_email_verification,
        worker,
    )
    worker._hme_email_verification_priority_configured = True
    return True


def configure_security_challenge_monitoring(
    worker,
    *,
    activate_page=None,
) -> bool:
    """Pause browser automation while a human completes a security challenge."""

    activate_page = activate_page or _activate_visible_registration_page
    original_continue = getattr(worker, "_continue_chatgpt_registration_complete", None)
    if not callable(original_continue):
        return False
    if getattr(worker, "_hme_security_challenge_monitoring_configured", False):
        return True

    def challenge_is_active(self, page) -> bool:
        visible = _security_challenge_visible(page)
        was_active = bool(getattr(self, "_hme_security_challenge_active", False))
        if not visible:
            if was_active:
                self.log("[认证] 安全验证已完成，正在自动继续登录流程")
            self._hme_security_challenge_active = False
            return False

        now = time.monotonic()
        last_logged_at = float(
            getattr(self, "_hme_security_challenge_logged_at", 0) or 0
        )
        if not was_active:
            activate_page(self, page)
        if not was_active or now - last_logged_at >= 10.0:
            self.log(
                "[认证] 已识别 Cloudflare 人机验证；请在当前浏览器手动点击"
                "“我不是机器人”。程序不会代点，完成后会自动继续"
            )
            self._hme_security_challenge_logged_at = now
        self._hme_security_challenge_active = True
        return True

    def continue_after_manual_challenge(self, page, *args, **kwargs):
        if challenge_is_active(self, page):
            return True
        return original_continue(page, *args, **kwargs)

    worker._continue_chatgpt_registration_complete = types.MethodType(
        continue_after_manual_challenge, worker
    )
    for method_name in (
        "_fill_email_if_visible",
        "_has_visible_password",
        "_has_otp_input",
        "_has_about_you_form",
    ):
        original_method = getattr(worker, method_name, None)
        if not callable(original_method):
            continue

        def blocked_during_challenge(
            self,
            page,
            *args,
            _original_method=original_method,
            **kwargs,
        ):
            if challenge_is_active(self, page):
                return False
            return _original_method(page, *args, **kwargs)

        setattr(
            worker,
            method_name,
            types.MethodType(blocked_during_challenge, worker),
        )
    worker._hme_security_challenge_monitoring_configured = True
    return True


def _about_you_profile_values_match(
    worker,
    page,
    name: str,
    birthdate: str,
    birth_year: str,
    age: str,
) -> tuple[bool, list[str], str]:
    values_reader = getattr(worker, "_visible_input_values", None)
    context_reader = getattr(worker, "_about_you_second_field_context", None)
    kind_reader = getattr(worker, "_about_you_second_field_kind_from_context", None)
    value_reader = getattr(worker, "_about_you_second_field_value", None)
    semantic_validator = getattr(worker, "_about_you_values_ok", None)
    if not all(
        callable(method)
        for method in (
            values_reader,
            context_reader,
            kind_reader,
            value_reader,
            semantic_validator,
        )
    ):
        return False, [], "unknown"

    try:
        context = str(context_reader(page) or "")
        second_kind = str(kind_reader(context) or "birth_year")
        expected_second = str(
            value_reader(second_kind, birth_year, age, birthdate, context) or ""
        ).strip()
        values = [str(value or "").strip() for value in values_reader(page)]
    except Exception:
        return False, [], "unknown"

    if not values or values[0] != str(name or "").strip():
        return False, values, second_kind
    if second_kind == "birth_date":
        return bool(semantic_validator(values, second_kind)), values, second_kind
    if len(values) < 2 or values[1] != expected_second:
        return False, values, second_kind
    return bool(semantic_validator(values, second_kind)), values, second_kind


def configure_resilient_about_you_input(
    worker,
    *,
    activate_page=None,
) -> bool:
    """Keep visible about-you input focused and reject unverified field values."""

    activate_page = activate_page or _activate_visible_registration_page
    if getattr(worker, "_hme_about_you_input_configured", False):
        return True
    original_keyboard_fill = getattr(worker, "_fill_visible_input_by_keyboard", None)
    original_profile_fill = getattr(worker, "_fill_about_you_inputs", None)
    original_profile_submit = getattr(worker, "_submit_about_you", None)
    dom_profile_fill = getattr(worker, "_fill_about_you_inputs_by_dom", None)
    focus_submit = getattr(worker, "_focus_about_you_submit_or_body", None)
    if not callable(original_keyboard_fill) or not callable(original_profile_fill):
        return False

    def keyboard_fill_with_foreground(self, page, index: int, value: str):
        activated = activate_page(self, page)
        if activated and not getattr(self, "_hme_about_you_focus_logged", False):
            self.log("[基础资料] 键盘输入前已激活当前浏览器窗口")
            self._hme_about_you_focus_logged = True
        return original_keyboard_fill(page, index, value)

    def profile_fill_with_readback(
        self,
        page,
        name: str,
        birthdate: str,
        birth_year: str,
        age: str,
    ):
        activate_page(self, page)
        result = original_profile_fill(page, name, birthdate, birth_year, age)
        matched, values, second_kind = _about_you_profile_values_match(
            self, page, name, birthdate, birth_year, age
        )
        if matched:
            self.log("[基础资料] 姓名与年龄/出生信息回读校验通过")
            return result

        self.log(
            "[基础资料] 键盘输入回读不一致，改用 DOM 事件重填；"
            f"字段类型={second_kind}，当前值={values}"
        )
        if callable(dom_profile_fill):
            context_reader = getattr(self, "_about_you_second_field_context", None)
            kind_reader = getattr(
                self, "_about_you_second_field_kind_from_context", None
            )
            value_reader = getattr(self, "_about_you_second_field_value", None)
            if all(
                callable(method)
                for method in (context_reader, kind_reader, value_reader)
            ):
                context = str(context_reader(page) or "")
                second_kind = str(kind_reader(context) or "birth_year")
                second_value = str(
                    value_reader(second_kind, birth_year, age, birthdate, context) or ""
                )
                dom_profile_fill(page, name, second_value, second_kind)
                if callable(focus_submit):
                    focus_submit(page)

        matched, values, second_kind = _about_you_profile_values_match(
            self, page, name, birthdate, birth_year, age
        )
        if matched:
            self.log("[基础资料] DOM 重填后回读校验通过")
            return result
        raise RuntimeError(
            "基础资料自动填写未确认成功，已停止提交以避免发送错误资料；"
            f"字段类型={second_kind}，当前值={values}"
        )

    worker._fill_visible_input_by_keyboard = types.MethodType(
        keyboard_fill_with_foreground, worker
    )
    worker._fill_about_you_inputs = types.MethodType(profile_fill_with_readback, worker)
    if callable(original_profile_submit):

        def submit_profile_with_one_retry(self, page):
            backend_module = sys.modules.get(
                str(getattr(original_profile_submit, "__module__", "") or "")
            )
            timeout_name = "OPENAI_ABOUT_YOU_SUBMIT_RESPONSE_TIMEOUT_SECONDS"
            original_timeout = (
                getattr(backend_module, timeout_name, None)
                if backend_module is not None
                else None
            )
            try:
                if backend_module is not None and original_timeout is not None:
                    setattr(backend_module, timeout_name, min(30, original_timeout))
                return original_profile_submit(page)
            except RuntimeError as error:
                if "基础资料按钮点击后" not in str(error):
                    raise
            finally:
                if backend_module is not None and original_timeout is not None:
                    setattr(backend_module, timeout_name, original_timeout)

            self.log(
                "[基础资料] 第一次提交 30 秒后页面仍未跳转；"
                "确认仍在当前表单，重新激活后台标签并只重试一次"
            )
            activate_page(self, page)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            _page_wait(page, 1000)
            return original_profile_submit(page)

        worker._submit_about_you = types.MethodType(
            submit_profile_with_one_retry,
            worker,
        )
    worker._hme_about_you_input_configured = True
    return True


def _is_google_account_url(url: str) -> bool:
    try:
        host = (urlparse(str(url or "")).hostname or "").strip().lower()
    except Exception:
        return False
    return host == "accounts.google.com" or host.endswith(".accounts.google.com")


def _is_openai_auth_url(url: str) -> bool:
    try:
        host = (urlparse(str(url or "")).hostname or "").strip().lower()
    except Exception:
        return False
    return host == "auth.openai.com" or host.endswith(".auth.openai.com")


def _registration_input_value(candidate) -> str | None:
    return _auth_input_value(candidate)


def _click_openai_email_submit(page) -> bool:
    return _auth_click_email_submit(page, first_visible=_first_visible)


class FreshFingerprintRequiredError(RuntimeError):
    """Stop the current browser so the outer task can retry with a new fingerprint."""


def configure_email_password_only_registration(
    worker,
    *,
    enabled: bool,
    activate_page=None,
    clipboard_write=None,
    clipboard_lock=None,
    first_visible=None,
    wait=None,
) -> bool:
    """Keep Gmail addresses on OpenAI's email/password path, never Google OAuth."""

    activate_page = activate_page or _activate_visible_registration_page
    clipboard_write = clipboard_write or _copy_registration_clipboard_text
    clipboard_lock = clipboard_lock or _REGISTRATION_CLIPBOARD_LOCK
    first_visible = first_visible or _first_visible
    wait = wait or _page_wait
    original_register = getattr(worker, "_register", None)
    original_fill_email = getattr(worker, "_fill_email_if_visible", None)
    original_continue_registration = getattr(
        worker, "_continue_chatgpt_registration_complete", None
    )
    if (
        not enabled
        or not callable(original_register)
        or not callable(original_fill_email)
    ):
        return False
    if getattr(worker, "_hme_email_password_only_configured", False):
        return True

    def register_without_google_oauth(self, page, context, *args, **kwargs):
        self._hme_openai_registration_context = context
        route_method = getattr(context, "route", None)
        if callable(route_method):
            for pattern in (
                "https://accounts.google.com/**",
                "https://*.accounts.google.com/**",
            ):
                try:
                    route_method(pattern, lambda route, *_args: route.abort())
                except Exception:
                    pass
        self.log("[认证] Gmail 地址仅使用 OpenAI 邮箱+密码注册；Google 账号入口已禁用")
        return original_register(page, context, *args, **kwargs)

    def require_fresh_fingerprint_after_google_oauth(self, page) -> bool:
        if not _is_google_account_url(str(getattr(page, "url", "") or "")):
            return False
        self.log(
            "[认证] 检测到 Google 登录要求；本轮注册立即判定失败，"
            "关闭当前浏览器并请求生成全新指纹"
        )
        raise FreshFingerprintRequiredError(
            "OpenAI 注册要求 Google 登录；需要关闭当前浏览器并更换全新指纹"
        )

    def continue_registration_without_google(self, page):
        if require_fresh_fingerprint_after_google_oauth(self, page):
            return True
        if callable(original_continue_registration):
            return original_continue_registration(page)
        return False

    def fill_openai_email_without_social_login(self, page) -> bool:
        if require_fresh_fingerprint_after_google_oauth(self, page):
            return False

        visible_inputs = getattr(self, "_visible_inputs", None)
        if not callable(visible_inputs):
            return original_fill_email(page)
        inputs = visible_inputs(
            page,
            [
                'input[type="email"]',
                'input[name="email"]',
                'input[name="username"]',
                'input[autocomplete="email"]',
            ],
        )
        if not inputs:
            return False

        _auth_paste_email_and_submit(
            page,
            inputs[0],
            self.account.email,
            log=self.log,
            activate=lambda target_page: activate_page(self, target_page),
            wait=wait,
            first_visible=first_visible,
            clipboard_write=clipboard_write,
            clipboard_lock=clipboard_lock,
            submit_selectors=OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS,
            submit_allowed_labels=(
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
            ),
            allow_enter_submit=False,
            submit_diagnostic_message=(
                "已点击注册继续；注册流程未按 Enter，且不会匹配登录按钮"
            ),
        )
        if _is_google_account_url(str(getattr(page, "url", "") or "")):
            require_fresh_fingerprint_after_google_oauth(self, page)
        return True

    worker._register = types.MethodType(register_without_google_oauth, worker)
    worker._fill_email_if_visible = types.MethodType(
        fill_openai_email_without_social_login, worker
    )
    if callable(original_continue_registration):
        worker._continue_chatgpt_registration_complete = types.MethodType(
            continue_registration_without_google, worker
        )
    worker._hme_email_password_only_configured = True
    return True
