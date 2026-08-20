from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Any, Callable, Mapping


class BrowserDiagnosticCode(StrEnum):
    """Stable, searchable codes for registration-browser milestones."""

    WINDOW_SINGLE_STABLE = "WINDOW_SINGLE_STABLE"
    WINDOW_TILING_ENABLED = "WINDOW_TILING_ENABLED"
    AUTH_HOME_READY = "AUTH_HOME_READY"
    AUTH_HOME_LOGIN_CLICK = "AUTH_HOME_LOGIN_CLICK"
    AUTH_HOME_LOGIN_RETRY = "AUTH_HOME_LOGIN_RETRY"
    AUTH_HOME_TRANSITION = "AUTH_HOME_TRANSITION"
    AUTH_DIRECT_NAV_BLOCKED = "AUTH_DIRECT_NAV_BLOCKED"
    AUTH_EMAIL_FOCUS = "AUTH_EMAIL_FOCUS"
    AUTH_EMAIL_PASTE = "AUTH_EMAIL_PASTE"
    AUTH_EMAIL_SUBMIT = "AUTH_EMAIL_SUBMIT"
    AUTH_PASSWORD_WAIT = "AUTH_PASSWORD_WAIT"
    AUTH_PASSWORD_READY = "AUTH_PASSWORD_READY"
    AUTH_PASSWORD_TIMEOUT = "AUTH_PASSWORD_TIMEOUT"
    AUTH_PASSWORD_SCREENSHOT = "AUTH_PASSWORD_SCREENSHOT"
    AUTH_PASSWORD_RESET_CONTINUE = "AUTH_PASSWORD_RESET_CONTINUE"
    AUTH_PASSWORD_RESET_WAIT = "AUTH_PASSWORD_RESET_WAIT"
    AUTH_EXISTING_ACCOUNT_REJECTED = "AUTH_EXISTING_ACCOUNT_REJECTED"
    AUTH_PASSWORD_ROUTE_TRANSITION = "AUTH_PASSWORD_ROUTE_TRANSITION"


@dataclass(frozen=True, slots=True)
class BrowserDiagnostic:
    code: BrowserDiagnosticCode | str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        code = str(self.code).strip() or "BROWSER"
        return f"[{code}] {str(self.message or '').strip()}"


def emit_browser_diagnostic(
    sink: Callable[[str], Any],
    code: BrowserDiagnosticCode | str,
    message: str,
    **details: Any,
) -> BrowserDiagnostic:
    event = BrowserDiagnostic(code=code, message=message, details=details)
    sink(event.render())
    return event


_DIAGNOSTIC_CONTEXTS: dict[BrowserDiagnosticCode, tuple[str, str, str, str]] = {
    BrowserDiagnosticCode.WINDOW_SINGLE_STABLE: (
        "browser",
        "Camoufox 浏览器",
        "启动单个稳定窗口",
        "success",
    ),
    BrowserDiagnosticCode.WINDOW_TILING_ENABLED: (
        "browser",
        "Camoufox 浏览器",
        "平铺并发浏览器窗口",
        "active",
    ),
    BrowserDiagnosticCode.AUTH_HOME_READY: (
        "openai_auth",
        "ChatGPT 首页",
        "等待登录入口可操作",
        "active",
    ),
    BrowserDiagnosticCode.AUTH_HOME_LOGIN_CLICK: (
        "openai_auth",
        "ChatGPT 首页",
        "点击登录并监测页面跳转",
        "active",
    ),
    BrowserDiagnosticCode.AUTH_HOME_LOGIN_RETRY: (
        "openai_auth",
        "ChatGPT 首页",
        "重新确认并点击登录",
        "warning",
    ),
    BrowserDiagnosticCode.AUTH_HOME_TRANSITION: (
        "openai_auth",
        "OpenAI 邮箱认证页",
        "确认首页登录入口已跳转",
        "success",
    ),
    BrowserDiagnosticCode.AUTH_DIRECT_NAV_BLOCKED: (
        "openai_auth",
        "OpenAI 邮箱认证页",
        "保留页面点击路径",
        "success",
    ),
    BrowserDiagnosticCode.AUTH_EMAIL_FOCUS: (
        "openai_auth",
        "OpenAI 邮箱认证页",
        "聚焦邮箱输入框",
        "active",
    ),
    BrowserDiagnosticCode.AUTH_EMAIL_PASTE: (
        "openai_auth",
        "OpenAI 邮箱认证页",
        "粘贴并校验邮箱",
        "success",
    ),
    BrowserDiagnosticCode.AUTH_EMAIL_SUBMIT: (
        "openai_auth",
        "OpenAI 邮箱认证页",
        "点击登录或继续",
        "active",
    ),
    BrowserDiagnosticCode.AUTH_PASSWORD_WAIT: (
        "password",
        "OpenAI 密码页",
        "监测密码控件状态",
        "waiting",
    ),
    BrowserDiagnosticCode.AUTH_PASSWORD_READY: (
        "password",
        "OpenAI 密码页",
        "密码控件已可操作",
        "success",
    ),
    BrowserDiagnosticCode.AUTH_PASSWORD_TIMEOUT: (
        "password",
        "OpenAI 密码页",
        "密码控件等待超时",
        "error",
    ),
    BrowserDiagnosticCode.AUTH_PASSWORD_SCREENSHOT: (
        "password",
        "OpenAI 密码页",
        "保存密码页诊断截图",
        "warning",
    ),
    BrowserDiagnosticCode.AUTH_PASSWORD_RESET_CONTINUE: (
        "password",
        "OpenAI 密码重置页",
        "确认开始密码重置",
        "active",
    ),
    BrowserDiagnosticCode.AUTH_PASSWORD_RESET_WAIT: (
        "password",
        "OpenAI 密码重置页",
        "等待密码重置验证码页",
        "waiting",
    ),
    BrowserDiagnosticCode.AUTH_EXISTING_ACCOUNT_REJECTED: (
        "password",
        "OpenAI 密码登录页",
        "拒绝把已有账号当作新注册账号",
        "error",
    ),
    BrowserDiagnosticCode.AUTH_PASSWORD_ROUTE_TRANSITION: (
        "password",
        "OpenAI 密码页",
        "确认密码入口跳转完成",
        "success",
    ),
}


def browser_diagnostic_context(message: str) -> dict[str, str] | None:
    """Map a stable diagnostic prefix directly to the UI execution context."""

    match = re.match(r"^\[([A-Z0-9_]+)\]\s*(.*)$", str(message or "").strip())
    if not match:
        return None
    try:
        code = BrowserDiagnosticCode(match.group(1))
    except ValueError:
        return None
    context = _DIAGNOSTIC_CONTEXTS.get(code)
    if context is None:
        return None
    stage, location, default_action, status = context
    detail = re.sub(r"^\[[^\]]+\]\s*", "", match.group(2)).strip()
    return {
        "stage": stage,
        "location": location,
        "action": detail or default_action,
        "status": status,
        "diagnosticCode": code.value,
    }
