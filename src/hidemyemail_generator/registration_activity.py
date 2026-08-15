"""Lightweight request-driven progress tracking for browser registration."""

from __future__ import annotations

import re
import time
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse


CLICK_RESPONSE_SECONDS = 1.0
MAX_NO_RESPONSE_CLICK_ATTEMPTS = 5
OTP_STABLE_READ_COUNT = 2
OTP_STABLE_READ_DELAY_SECONDS = 0.12

EMAIL_VERIFICATION_INPUT_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    'input[type="tel"]',
    'input[name="code"]',
    'input[aria-label*="コード" i]',
    'input[placeholder*="コード" i]',
)

_CHAIN_STEPS = (
    ("site_requested", "已请求 ChatGPT 网站", "等待网站加载完成"),
    ("site_loaded", "ChatGPT 网站已加载完成", "点击注册入口"),
    ("registration_clicked", "注册入口已点击", "等待注册入口响应"),
    ("registration_entry_ready", "注册入口已有响应", "识别并输入邮箱"),
    ("email_entered", "邮箱已输入并回读验证", "提交邮箱"),
    ("email_submitted", "邮箱提交动作已执行", "等待邮箱提交请求响应"),
    ("email_responded", "邮箱提交已有响应", "等待验证码界面"),
    ("verification_page", "已进入邮箱验证码界面", "请求本轮最新验证码"),
    ("verification_requested", "已开始请求邮箱验证码", "等待验证码到达"),
    ("verification_code_received", "已获取邮箱验证码", "输入并回读验证码"),
    ("verification_code_entered", "验证码已输入并回读验证", "提交验证码"),
    ("verification_submitted", "验证码提交请求已执行", "等待验证码校验响应"),
    ("registration_created", "验证码已通过，账号注册成功", "填写并验证基础资料"),
    ("profile_verified", "姓名和生日资料已回读验证", "提交基础资料"),
    ("profile_submitted", "基础资料提交已有请求响应", "等待 Session 建立"),
    ("session_ready", "Session 已获取", "确认账号密码"),
    ("password_confirmed", "密码已添加并确认", "开启 TOTP 2FA"),
    ("two_factor_enabled", "TOTP 2FA 已开启", "完成并保存账号"),
    ("complete", "完整注册链路已完成", "保存账号结果"),
)
_CHAIN_LABELS = {code: label for code, label, _next in _CHAIN_STEPS}
_CHAIN_NEXT = {code: next_action for code, _label, next_action in _CHAIN_STEPS}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _value(target: Any, name: str, default: Any = "") -> Any:
    value = getattr(target, name, default)
    if callable(value):
        try:
            value = value()
        except Exception:
            return default
    return default if value is None else value


def _safe_request_route(value: str) -> tuple[str, str]:
    try:
        parsed = urlparse(str(value or ""))
    except (TypeError, ValueError):
        return "", "/"
    host = str(parsed.hostname or "").lower()
    return host, str(parsed.path or "/")[:180]


@dataclass(frozen=True, slots=True)
class RegistrationNetworkEvent:
    """Model for one sanitized browser-network observation."""

    event: str
    method: str
    route: str
    resource_type: str
    status: int
    registration_entry: bool
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "method": self.method,
            "route": self.route,
            "resourceType": self.resource_type,
            "status": self.status,
            "registrationEntry": self.registration_entry,
            "at": self.at,
        }


class RegistrationEntryRequestStrategy:
    """Strategy that separates an auth-entry request from background traffic."""

    _CHATGPT_ENTRY_PATHS = {
        "/auth/login",
        "/auth/log-in",
        "/auth/signup",
        "/auth/sign-up",
        "/auth/register",
        "/auth/create-account",
    }
    _AUTH_ACTION_MARKERS = (
        "authorize",
        "signup",
        "sign-up",
        "register",
        "create-account",
    )

    @classmethod
    def route_matches(cls, host: str, path: str, resource_type: str) -> bool:
        folded_host = str(host or "").casefold()
        folded_path = str(path or "/").rstrip("/").casefold() or "/"
        kind = str(resource_type or "").casefold()
        auth_host = folded_host == "auth.openai.com" or folded_host.endswith(
            ".auth.openai.com"
        )
        if kind == "document":
            return auth_host or (
                folded_host == "chatgpt.com"
                and folded_path in cls._CHATGPT_ENTRY_PATHS
            )
        if kind not in {"xhr", "fetch"}:
            return False
        if not auth_host:
            return False
        return any(marker in folded_path for marker in cls._AUTH_ACTION_MARKERS)

    @classmethod
    def matches(cls, request: Any) -> bool:
        resource_type = str(_value(request, "resource_type", "") or "").lower()
        host, path = _safe_request_route(str(_value(request, "url", "") or ""))
        return cls.route_matches(host, path, resource_type)


class RegistrationActivityPresenter:
    """Presenter that converts page/network observations into UI-safe evidence."""

    @staticmethod
    def network_event(
        event: str,
        request: Any,
        *,
        status: int = 0,
    ) -> RegistrationNetworkEvent:
        method = str(_value(request, "method", "GET") or "GET").upper()[:12]
        resource_type = str(
            _value(request, "resource_type", "") or ""
        ).lower()[:32]
        host, path = _safe_request_route(str(_value(request, "url", "") or ""))
        return RegistrationNetworkEvent(
            event=str(event or "")[:24],
            method=method,
            route=f"{host}{path}"[:220],
            resource_type=resource_type,
            status=max(0, int(status or 0)),
            registration_entry=RegistrationEntryRequestStrategy.route_matches(
                host,
                path,
                resource_type,
            ),
            at=_utc_now(),
        )

    @staticmethod
    def evidence(snapshot: dict[str, Any], *, signal: str) -> dict[str, Any]:
        prefix = "lastEntry" if signal == "registration_entry" else "last"
        at_key = "lastEntryAt" if signal == "registration_entry" else "lastActivityAt"
        return {
            "event": str(snapshot.get(f"{prefix}Event") or ""),
            "method": str(snapshot.get(f"{prefix}Method") or ""),
            "route": str(snapshot.get(f"{prefix}Route") or ""),
            "resourceType": str(
                snapshot.get(f"{prefix}ResourceType") or ""
            ),
            "status": max(0, int(snapshot.get(f"{prefix}Status") or 0)),
            "at": str(snapshot.get(at_key) or ""),
        }


def _relevant_request(request: Any) -> bool:
    method = str(_value(request, "method", "GET") or "GET").upper()
    resource_type = str(_value(request, "resource_type", "") or "").lower()
    host, path = _safe_request_route(str(_value(request, "url", "") or ""))
    if not (
        host == "chatgpt.com"
        or host == "auth.openai.com"
        or host.endswith(".openai.com")
    ):
        return False
    if resource_type == "document":
        return True
    if method not in {"GET", "HEAD", "OPTIONS"}:
        return True
    if resource_type not in {"xhr", "fetch"}:
        return False
    folded = path.casefold()
    return any(
        marker in folded
        for marker in (
            "/api/",
            "/auth",
            "authorize",
            "login",
            "signup",
            "register",
            "verification",
            "otp",
            "session",
        )
    )


def _lightweight_dom_state(page: Any) -> tuple[Any, ...]:
    url = str(getattr(page, "url", "") or "")
    host, path = _safe_request_route(url)
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return (host, path, "unknown", False, False, False, False, 0, 0, 0)
    try:
        state = evaluate(
            """() => {
                const visible = (element) => {
                    if (!element) return false;
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };
                const anyVisible = (selector) =>
                    Array.from(document.querySelectorAll(selector)).some(visible);
                return {
                    readyState: document.readyState,
                    email: anyVisible('input[type="email"], input[name="email"], input[name="username"]'),
                    password: anyVisible('input[type="password"]'),
                    otp: anyVisible('input[autocomplete="one-time-code"], input[inputmode="numeric"], input[name*="code" i]'),
                    profile: anyVisible('input[name="name"], input[autocomplete="name"], input[name*="birth" i]'),
                    inputCount: Array.from(document.querySelectorAll('input, textarea, select')).filter(visible).length,
                    buttonCount: Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible).length,
                    busyCount: document.querySelectorAll('[aria-busy="true"], [data-loading="true"]').length,
                };
            }"""
        )
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}
    return (
        host,
        path,
        str(state.get("readyState") or "unknown"),
        bool(state.get("email")),
        bool(state.get("password")),
        bool(state.get("otp")),
        bool(state.get("profile")),
        max(0, int(state.get("inputCount") or 0)),
        max(0, int(state.get("buttonCount") or 0)),
        max(0, int(state.get("busyCount") or 0)),
    )


class RegistrationActivityMonitor:
    """Count safe registration network/page events without reading bodies."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self.available = False
        self.request_count = 0
        self.response_count = 0
        self.failed_count = 0
        self.load_count = 0
        self.entry_request_count = 0
        self.entry_response_count = 0
        self.entry_failed_count = 0
        self.last_event = ""
        self.last_method = ""
        self.last_route = ""
        self.last_resource_type = ""
        self.last_status = 0
        self.last_activity_at = ""
        self.last_entry_event = ""
        self.last_entry_method = ""
        self.last_entry_route = ""
        self.last_entry_resource_type = ""
        self.last_entry_status = 0
        self.last_entry_at = ""
        self.milestone_callback: Callable[[str, str], Any] | None = None
        self.begin_callback: Callable[[str, str], Any] | None = None
        self.skip_callback: Callable[[str, str], Any] | None = None
        on = getattr(page, "on", None)
        if not callable(on):
            return
        try:
            on("request", self._on_request)
            on("response", self._on_response)
            on("requestfailed", self._on_request_failed)
            on("load", self._on_load)
            self.available = True
        except Exception:
            self.available = False

    def _record_network_event(
        self,
        event: str,
        request: Any,
        *,
        status: int = 0,
    ) -> RegistrationNetworkEvent:
        observation = RegistrationActivityPresenter.network_event(
            event,
            request,
            status=status,
        )
        self.last_event = observation.event
        self.last_method = observation.method
        self.last_route = observation.route
        self.last_resource_type = observation.resource_type
        # A request must not inherit an earlier response status.  The dedicated
        # entry channel keeps its own atomic route/status evidence as well.
        self.last_status = observation.status
        self.last_activity_at = observation.at
        if observation.registration_entry:
            self.last_entry_event = observation.event
            self.last_entry_method = observation.method
            self.last_entry_route = observation.route
            self.last_entry_resource_type = observation.resource_type
            self.last_entry_status = observation.status
            self.last_entry_at = observation.at
        return observation

    def _on_request(self, request: Any) -> None:
        if not _relevant_request(request):
            return
        self.request_count += 1
        observation = self._record_network_event("request", request)
        if observation.registration_entry:
            self.entry_request_count += 1
        self.mark("site_requested", "检测到网站请求")

    def _on_response(self, response: Any) -> None:
        request = getattr(response, "request", None)
        if request is None or not _relevant_request(request):
            return
        self.response_count += 1
        try:
            status = max(0, int(_value(response, "status", 0) or 0))
        except (TypeError, ValueError):
            status = 0
        observation = self._record_network_event(
            "response",
            request,
            status=status,
        )
        if observation.registration_entry:
            self.entry_response_count += 1

    def _on_request_failed(self, request: Any) -> None:
        if not _relevant_request(request):
            return
        self.failed_count += 1
        observation = self._record_network_event("request_failed", request)
        if observation.registration_entry:
            self.entry_failed_count += 1

    def _on_load(self, *_args: Any) -> None:
        self.load_count += 1
        self.last_event = "load"
        self.last_method = ""
        self.last_route = ""
        self.last_resource_type = ""
        self.last_status = 0
        self.last_activity_at = _utc_now()
        self.mark("site_loaded", "页面 load 事件完成")

    def mark(self, step: str, detail: str = "") -> None:
        if callable(self.milestone_callback):
            self.milestone_callback(str(step or ""), str(detail or ""))

    def begin(self, step: str, value: str = "") -> None:
        if callable(self.begin_callback):
            self.begin_callback(str(step or ""), str(value or ""))

    def skip(self, step: str, value: str = "") -> None:
        if callable(self.skip_callback):
            self.skip_callback(str(step or ""), str(value or ""))

    def snapshot(self) -> dict[str, Any]:
        return {
            "requestCount": self.request_count,
            "responseCount": self.response_count,
            "failedCount": self.failed_count,
            "loadCount": self.load_count,
            "entryRequestCount": self.entry_request_count,
            "entryResponseCount": self.entry_response_count,
            "entryFailedCount": self.entry_failed_count,
            "lastEvent": self.last_event,
            "lastMethod": self.last_method,
            "lastRoute": self.last_route,
            "lastResourceType": self.last_resource_type,
            "lastStatus": self.last_status,
            "lastActivityAt": self.last_activity_at,
            "lastEntryEvent": self.last_entry_event,
            "lastEntryMethod": self.last_entry_method,
            "lastEntryRoute": self.last_entry_route,
            "lastEntryResourceType": self.last_entry_resource_type,
            "lastEntryStatus": self.last_entry_status,
            "lastEntryAt": self.last_entry_at,
            "dom": _lightweight_dom_state(self.page),
        }

    def public_state(self) -> dict[str, Any]:
        return {key: value for key, value in self.snapshot().items() if key != "dom"}


def ensure_registration_activity_monitor(
    page: Any,
    *,
    milestone_callback: Callable[[str, str], Any] | None = None,
    begin_callback: Callable[[str, str], Any] | None = None,
    skip_callback: Callable[[str, str], Any] | None = None,
) -> RegistrationActivityMonitor:
    monitor = getattr(page, "_hme_registration_activity_monitor", None)
    if not isinstance(monitor, RegistrationActivityMonitor):
        monitor = RegistrationActivityMonitor(page)
        try:
            setattr(page, "_hme_registration_activity_monitor", monitor)
        except Exception:
            pass
    if milestone_callback is not None:
        monitor.milestone_callback = milestone_callback
    if begin_callback is not None:
        monitor.begin_callback = begin_callback
    if skip_callback is not None:
        monitor.skip_callback = skip_callback
    return monitor


def registration_activity_snapshot(page: Any) -> dict[str, Any]:
    return ensure_registration_activity_monitor(page).snapshot()


def registration_activity_changed(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    signal: str = "any",
) -> tuple[bool, str]:
    if signal == "registration_entry":
        for key, label in (
            ("entryResponseCount", "registration_entry_response"),
            ("entryFailedCount", "registration_entry_request_failed"),
            ("entryRequestCount", "registration_entry_request"),
        ):
            if int(after.get(key) or 0) > int(before.get(key) or 0):
                return True, label
        if _registration_entry_dom_changed(
            before.get("dom"),
            after.get("dom"),
        ):
            return True, "page"
        return False, ""
    for key, label in (
        ("requestCount", "request"),
        ("responseCount", "response"),
        ("failedCount", "request_failed"),
        ("loadCount", "load"),
    ):
        if int(after.get(key) or 0) > int(before.get(key) or 0):
            return True, label
    if after.get("dom") != before.get("dom"):
        return True, "page"
    return False, ""


def _registration_entry_dom_changed(before: Any, after: Any) -> bool:
    if not isinstance(before, tuple) or not isinstance(after, tuple):
        return False
    if len(before) < 4 or len(after) < 4:
        return False
    if not bool(before[3]) and bool(after[3]):
        return True
    before_host = str(before[0] or "").casefold()
    before_path = str(before[1] or "/").rstrip("/").casefold() or "/"
    after_host = str(after[0] or "").casefold()
    after_path = str(after[1] or "/").rstrip("/").casefold() or "/"
    auth_host = after_host == "auth.openai.com" or after_host.endswith(
        ".auth.openai.com"
    )
    chatgpt_entry = (
        after_host == "chatgpt.com"
        and after_path in RegistrationEntryRequestStrategy._CHATGPT_ENTRY_PATHS
    )
    return (auth_host or chatgpt_entry) and (
        after_host != before_host or after_path != before_path
    )


def wait_for_registration_activity(
    page: Any,
    before: dict[str, Any],
    *,
    timeout_seconds: float = CLICK_RESPONSE_SECONDS,
    wait: Callable[[Any, int], Any] | None = None,
    transition: Callable[[], bool] | None = None,
    signal: str = "any",
) -> dict[str, Any]:
    timeout_value = max(0.05, float(timeout_seconds))
    if callable(wait):
        wait(page, max(1, int(timeout_value * 1000)))
    else:
        time.sleep(timeout_value)
    if callable(transition):
        try:
            if transition():
                after = registration_activity_snapshot(page)
                return {
                    "changed": True,
                    "reason": "transition",
                    "activity": after,
                    "evidence": RegistrationActivityPresenter.evidence(
                        after,
                        signal=signal,
                    ),
                }
        except Exception:
            pass
    after = registration_activity_snapshot(page)
    changed, reason = registration_activity_changed(
        before,
        after,
        signal=signal,
    )
    ignored_activity = False
    ignored_reason = ""
    if not changed and signal != "any":
        ignored_activity, ignored_reason = registration_activity_changed(
            before,
            after,
        )
    return {
        "changed": changed,
        "reason": reason,
        "activity": after,
        "evidence": RegistrationActivityPresenter.evidence(
            after,
            signal=signal,
        ),
        "ignoredActivity": ignored_activity,
        "ignoredReason": ignored_reason,
        "ignoredEvidence": RegistrationActivityPresenter.evidence(
            after,
            signal="any",
        ),
    }


def mark_page_registration_milestone(page: Any, step: str, detail: str = "") -> None:
    monitor = getattr(page, "_hme_registration_activity_monitor", None)
    if isinstance(monitor, RegistrationActivityMonitor):
        monitor.mark(step, detail)


def begin_page_registration_step(page: Any, step: str, value: str = "") -> None:
    monitor = getattr(page, "_hme_registration_activity_monitor", None)
    if isinstance(monitor, RegistrationActivityMonitor):
        monitor.begin(step, value)


def skip_page_registration_step(page: Any, step: str, value: str = "") -> None:
    monitor = getattr(page, "_hme_registration_activity_monitor", None)
    if isinstance(monitor, RegistrationActivityMonitor):
        monitor.skip(step, value)


def quiet_registration_delay(
    page: Any,
    *,
    seconds: float = CLICK_RESPONSE_SECONDS,
) -> None:
    timeout_value = max(0.05, float(seconds))
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    if callable(wait_for_timeout):
        try:
            wait_for_timeout(max(1, int(timeout_value * 1000)))
            return
        except Exception:
            pass
    time.sleep(timeout_value)


def _unique_visible_inputs(inputs: list[Any]) -> list[Any]:
    """Keep one locator per DOM element when selectors overlap."""

    unique: list[Any] = []
    seen: set[str] = set()
    for candidate in inputs:
        key = ""
        evaluator = getattr(candidate, "evaluate", None)
        if callable(evaluator):
            try:
                key = str(
                    evaluator(
                        """element => {
                            if (!element.dataset.hmeOtpLocatorId) {
                                element.dataset.hmeOtpLocatorId =
                                    'hme-otp-' + Math.random().toString(36).slice(2);
                            }
                            return element.dataset.hmeOtpLocatorId;
                        }"""
                    )
                    or ""
                )
            except Exception:
                key = ""
        if not key:
            key = f"object:{id(candidate)}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _read_email_verification_code(inputs: list[Any], expected: str) -> str:
    values: list[str] = []
    for candidate in inputs:
        reader = getattr(candidate, "input_value", None)
        if not callable(reader):
            continue
        try:
            value = str(reader(timeout=1000) or "")
        except TypeError:
            try:
                value = str(reader() or "")
            except Exception:
                value = ""
        except Exception:
            value = ""
        normalized = re.sub(r"[^A-Za-z0-9]", "", value)
        if normalized:
            values.append(normalized)
    if expected in values:
        return expected
    if len(values) >= len(expected) and all(len(value) == 1 for value in values):
        return "".join(values[: len(expected)])
    return values[0] if values else ""


def _fill_email_verification_code(inputs: list[Any], expected: str) -> None:
    if len(inputs) >= len(expected):
        for index, character in enumerate(expected):
            inputs[index].fill(character)
        return
    if not inputs:
        return
    # Locator.fill() dispatches the required input/change events atomically and
    # does not depend on the OS window retaining keyboard focus while six
    # delayed key presses are in flight.
    inputs[0].fill(expected)


def _email_verification_code_is_stable(
    worker: Any,
    page: Any,
    expected: str,
) -> tuple[bool, str]:
    """Require two fresh DOM reads before the submit gate can open."""

    actual = ""
    for read_index in range(OTP_STABLE_READ_COUNT):
        inputs = _unique_visible_inputs(
            list(worker._visible_inputs(page, list(EMAIL_VERIFICATION_INPUT_SELECTORS)))
        )
        actual = _read_email_verification_code(inputs, expected)
        if actual != expected:
            return False, actual
        if read_index + 1 < OTP_STABLE_READ_COUNT:
            quiet_registration_delay(
                page,
                seconds=OTP_STABLE_READ_DELAY_SECONDS,
            )
    return True, actual


def ensure_email_verification_code_entered(worker: Any, page: Any, code: str) -> None:
    """Fail closed unless the current visible OTP fields contain this code."""

    worker._hme_otp_input_attestation = None
    expected = re.sub(r"\D", "", str(code or ""))
    if not re.fullmatch(r"\d{6}", expected):
        raise RuntimeError("验证码不是 6 位数字，已阻止提交")
    last_actual = ""
    for attempt in range(3):
        inputs = _unique_visible_inputs(
            list(worker._visible_inputs(page, list(EMAIL_VERIFICATION_INPUT_SELECTORS)))
        )
        last_actual = _read_email_verification_code(inputs, expected)
        if last_actual != expected and inputs:
            _fill_email_verification_code(inputs, expected)
        if inputs:
            stable, last_actual = _email_verification_code_is_stable(
                worker,
                page,
                expected,
            )
        else:
            stable = False
        if stable:
            worker._hme_otp_input_attestation = {
                "verified": True,
                "code": expected,
                "pageUrl": str(getattr(page, "url", "") or ""),
                "verifiedAt": time.monotonic(),
                "stableReads": OTP_STABLE_READ_COUNT,
            }
            worker.log(
                "[验证码] 输入检查通过：已原子写入并连续两次回读 6 位验证码，"
                "无需保持窗口键盘焦点；现在立即点击继续"
            )
            return
        if not inputs:
            if attempt < 2:
                quiet_registration_delay(
                    page,
                    seconds=OTP_STABLE_READ_DELAY_SECONDS,
                )
                continue
            break
        worker.log(
            "[验证码] 输入框在提交前发生重渲染或值变化；"
            "已重新定位并再次原子填写"
        )
        quiet_registration_delay(
            page,
            seconds=OTP_STABLE_READ_DELAY_SECONDS,
        )
    raise RuntimeError("验证码未写入当前可见输入框，已阻止提交")


def _chain_state(worker: Any) -> dict[str, Any]:
    state = getattr(worker, "_hme_registration_chain", None)
    if not isinstance(state, dict):
        state = {
            "startedAt": _utc_now(),
            "updatedAt": _utc_now(),
            "steps": {},
            "status": "running",
            "currentStep": "准备注册请求",
            "nextAction": "请求并加载 ChatGPT 网站",
            "existingLoginOnly": False,
        }
        worker._hme_registration_chain = state
    return state


def _required_chain_codes(worker: Any, state: dict[str, Any]) -> list[str]:
    required_codes = [code for code, _label, _next in _CHAIN_STEPS]
    if bool(state.get("existingLoginOnly")):
        required_codes = [
            code
            for code in required_codes
            if code
            not in {
                "registration_clicked",
                "registration_created",
                "profile_verified",
                "profile_submitted",
            }
        ]
    if not bool(getattr(worker, "_hme_chain_require_password", False)):
        required_codes.remove("password_confirmed")
    if not bool(getattr(worker, "_hme_chain_enable_two_factor", False)):
        required_codes.remove("two_factor_enabled")
    return required_codes


def registration_chain_snapshot(worker: Any, page: Any | None = None) -> dict[str, Any]:
    state = _chain_state(worker)
    recorded = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    completed_codes = [
        code
        for code, _label, _next in _CHAIN_STEPS
        if bool((recorded.get(code) or {}).get("completed"))
    ]
    completed_steps = [_CHAIN_LABELS[code] for code in completed_codes]
    required_codes = _required_chain_codes(worker, state)
    next_code = next(
        (
            code
            for code in required_codes
            if not bool((recorded.get(code) or {}).get("completed"))
            and str((recorded.get(code) or {}).get("status") or "") != "skipped"
        ),
        "complete",
    )
    current_code = str(state.get("currentCode") or "")
    current_record = recorded.get(current_code) if current_code else None
    if not isinstance(current_record, dict):
        current_record = {}
    step_ledger = []
    for index, (code, label, _next_action) in enumerate(_CHAIN_STEPS, start=1):
        record = recorded.get(code)
        if not isinstance(record, dict):
            record = {}
        status = str(record.get("status") or "pending")
        step_ledger.append(
            {
                "index": index,
                "code": code,
                "label": label,
                "status": status,
                "value": str(record.get("value") or "")[:240],
                "completed": bool(record.get("completed", False)),
                "updatedAt": str(record.get("updatedAt") or ""),
            }
        )
    monitor_state: dict[str, Any] = {}
    if page is not None:
        monitor_state = ensure_registration_activity_monitor(page).public_state()
    return {
        "status": str(state.get("status") or "running"),
        "currentCode": current_code,
        "currentStep": str(state.get("currentStep") or "准备注册请求"),
        "currentValue": str(current_record.get("value") or "")[:240],
        "currentCompleted": bool(current_record.get("completed", False)),
        "nextCode": next_code,
        "canAdvance": bool(current_record.get("completed", False))
        if current_code
        else next_code == "site_requested",
        "steps": step_ledger,
        "completedCodes": completed_codes,
        "completedSteps": completed_steps,
        "nextAction": (
            "保存账号结果"
            if next_code == "complete" and "complete" in recorded
            else _CHAIN_NEXT.get(next_code, "继续监测注册流程")
        ),
        "registrationCreated": "registration_created" in recorded,
        "sessionReady": "session_ready" in recorded,
        "passwordConfirmed": "password_confirmed" in recorded,
        "twoFactorEnabled": "two_factor_enabled" in recorded,
        "fullRegistrationComplete": "complete" in recorded,
        "requestActivity": monitor_state,
        "startedAt": str(state.get("startedAt") or ""),
        "updatedAt": str(state.get("updatedAt") or _utc_now()),
    }


def begin_registration_step(
    worker: Any,
    step: str,
    *,
    page: Any | None = None,
    value: str = "",
) -> dict[str, Any]:
    code = str(step or "").strip()
    if code not in _CHAIN_LABELS:
        raise RuntimeError(f"未知注册步骤：{code or 'empty'}")
    state = _chain_state(worker)
    recorded = state.setdefault("steps", {})
    current = recorded.get(code)
    if isinstance(current, dict) and current.get("completed"):
        return registration_chain_snapshot(worker, page)
    snapshot = registration_chain_snapshot(worker, page)
    next_code = str(snapshot.get("nextCode") or "")
    if code != next_code:
        raise RuntimeError(
            f"注册步骤顺序错误：当前只允许 {next_code or 'none'}，收到 {code}"
        )
    now = _utc_now()
    recorded[code] = {
        "status": "running",
        "value": str(value or "正在执行")[:240],
        "completed": False,
        "updatedAt": now,
    }
    state["currentCode"] = code
    state["currentStep"] = _CHAIN_LABELS[code]
    state["updatedAt"] = now
    result = registration_chain_snapshot(worker, page)
    emitter = getattr(worker, "_hme_registration_chain_emitter", None)
    if callable(emitter):
        emitter(result)
    return result


def skip_registration_step(
    worker: Any,
    step: str,
    *,
    page: Any | None = None,
    value: str = "页面未要求此步骤",
) -> dict[str, Any]:
    code = str(step or "").strip()
    if code not in _CHAIN_LABELS:
        raise RuntimeError(f"未知注册步骤：{code or 'empty'}")
    state = _chain_state(worker)
    recorded = state.setdefault("steps", {})
    if bool((recorded.get(code) or {}).get("completed")):
        return registration_chain_snapshot(worker, page)
    snapshot = registration_chain_snapshot(worker, page)
    required_codes = _required_chain_codes(worker, state)
    if code not in required_codes:
        now = _utc_now()
        recorded[code] = {
            "status": "skipped",
            "value": str(value or "当前流程不要求此步骤")[:240],
            "completed": False,
            "updatedAt": now,
        }
        state["updatedAt"] = now
        return registration_chain_snapshot(worker, page)
    if code != str(snapshot.get("nextCode") or ""):
        raise RuntimeError(
            f"注册步骤顺序错误：当前不能跳过 {code}"
        )
    now = _utc_now()
    recorded[code] = {
        "status": "skipped",
        "value": str(value or "页面未要求此步骤")[:240],
        "completed": False,
        "updatedAt": now,
    }
    state["currentCode"] = code
    state["currentStep"] = f"{_CHAIN_LABELS[code]}（已跳过）"
    state["updatedAt"] = now
    result = registration_chain_snapshot(worker, page)
    emitter = getattr(worker, "_hme_registration_chain_emitter", None)
    if callable(emitter):
        emitter(result)
    return result


def mark_registration_chain(
    worker: Any,
    step: str,
    *,
    page: Any | None = None,
    detail: str = "",
) -> dict[str, Any]:
    code = str(step or "").strip()
    state = _chain_state(worker)
    if code in _CHAIN_LABELS:
        steps = state.setdefault("steps", {})
        existing = steps.get(code)
        if isinstance(existing, dict) and existing.get("completed"):
            if detail:
                existing["value"] = str(detail)[:240]
                existing["updatedAt"] = _utc_now()
            return registration_chain_snapshot(worker, page)
        snapshot = registration_chain_snapshot(worker, page)
        next_code = str(snapshot.get("nextCode") or "")
        if code not in _required_chain_codes(worker, state):
            steps[code] = {
                "status": "completed",
                "value": str(detail or "已完成（当前流程非必需）")[:240],
                "completed": True,
                "updatedAt": _utc_now(),
            }
            state["updatedAt"] = _utc_now()
            result = registration_chain_snapshot(worker, page)
            emitter = getattr(worker, "_hme_registration_chain_emitter", None)
            if callable(emitter):
                emitter(result)
            return result
        if code != next_code:
            raise RuntimeError(
                f"注册步骤顺序错误：当前只允许完成 {next_code or 'none'}，收到 {code}"
            )
        steps[code] = {
            "status": "completed",
            "value": str(detail or "已完成")[:240],
            "completed": True,
            "updatedAt": _utc_now(),
        }
        state["currentCode"] = code
        state["currentStep"] = _CHAIN_LABELS[code]
        if code == "complete":
            state["status"] = "success"
    elif code == "failed":
        state["status"] = "failed"
        state["currentStep"] = "完整注册链路失败"
        state["error"] = str(detail or "")[:300]
        current_code = str(state.get("currentCode") or "")
        current = state.setdefault("steps", {}).get(current_code)
        if isinstance(current, dict) and not current.get("completed"):
            current["status"] = "failed"
            current["value"] = str(detail or "执行失败")[:240]
            current["updatedAt"] = _utc_now()
    elif code:
        state["currentStep"] = str(detail or code)[:160]
    state["updatedAt"] = _utc_now()
    snapshot = registration_chain_snapshot(worker, page)
    emitter = getattr(worker, "_hme_registration_chain_emitter", None)
    if callable(emitter):
        emitter(snapshot)
    recognizer = getattr(worker, "_recognize_registration_state", None)
    if page is not None and callable(recognizer):
        try:
            recognizer(page, force=True)
        except Exception:
            pass
    return snapshot


def configure_request_driven_registration(
    worker: Any,
    *,
    emit_state: Callable[[dict[str, Any]], Any],
    require_password: bool,
    enable_two_factor: bool,
) -> bool:
    """Attach request monitoring and guarded step wrappers to one worker."""

    if getattr(worker, "_hme_request_driven_registration_configured", False):
        return True
    original_register = getattr(worker, "_register", None)
    if not callable(original_register):
        return False
    worker._hme_registration_chain_emitter = emit_state
    worker._hme_chain_require_password = bool(require_password)
    worker._hme_chain_enable_two_factor = bool(enable_two_factor)
    _chain_state(worker)

    def register_with_activity(self, page, context, *args, **kwargs):
        state = _chain_state(self)
        state["existingLoginOnly"] = bool(
            kwargs.get("existing_login_only", getattr(self, "existing_login_only", False))
        )
        monitor = ensure_registration_activity_monitor(
            page,
            milestone_callback=lambda step, detail="": mark_registration_chain(
                self,
                step,
                page=page,
                detail=detail,
            ),
            begin_callback=lambda step, value="": begin_registration_step(
                self,
                step,
                page=page,
                value=value,
            ),
            skip_callback=lambda step, value="": skip_registration_step(
                self,
                step,
                page=page,
                value=value or "页面未要求此步骤",
            ),
        )
        begin_registration_step(
            self,
            "site_requested",
            page=page,
            value="等待 ChatGPT document/XHR/fetch 请求",
        )
        result = original_register(page, context, *args, **kwargs)
        if monitor.load_count and "site_loaded" not in _chain_state(self).get("steps", {}):
            mark_registration_chain(self, "site_loaded", page=page)
        return result

    worker._register = types.MethodType(register_with_activity, worker)

    original_has_session = getattr(worker, "_has_chatgpt_session", None)
    if callable(original_has_session):
        def has_session_with_chain(self, page, *args, **kwargs):
            result = original_has_session(page, *args, **kwargs)
            if result:
                snapshot = registration_chain_snapshot(self, page)
                session_index = _required_chain_codes(
                    self,
                    _chain_state(self),
                ).index("session_ready")
                while str(snapshot.get("nextCode") or "") != "session_ready":
                    next_code = str(snapshot.get("nextCode") or "")
                    required_codes = _required_chain_codes(self, _chain_state(self))
                    if next_code not in required_codes:
                        break
                    if required_codes.index(next_code) >= session_index:
                        break
                    skip_registration_step(
                        self,
                        next_code,
                        page=page,
                        value="Session 已建立，当前页面未要求此步骤",
                    )
                    snapshot = registration_chain_snapshot(self, page)
                if str(snapshot.get("nextCode") or "") == "session_ready":
                    begin_registration_step(
                        self,
                        "session_ready",
                        page=page,
                        value="正在确认 Access Token 和 Session",
                    )
                mark_registration_chain(self, "session_ready", page=page)
            return result

        worker._has_chatgpt_session = types.MethodType(has_session_with_chain, worker)

    original_password = getattr(worker, "_fill_password_step", None)
    if callable(original_password):
        def password_with_chain(self, page, *args, **kwargs):
            monitor = ensure_registration_activity_monitor(page)
            before = monitor.snapshot()
            result = original_password(page, *args, **kwargs)
            quiet_registration_delay(page)
            after = monitor.snapshot()
            changed, _reason = registration_activity_changed(before, after)
            attempt = 1
            while monitor.available and not changed and attempt < MAX_NO_RESPONSE_CLICK_ATTEMPTS:
                attempt += 1
                click_continue = getattr(self, "_click_continue", None)
                if not callable(click_continue) or not click_continue(page):
                    break
                self.log(
                    f"[请求监测] 密码提交第 {attempt}/{MAX_NO_RESPONSE_CLICK_ATTEMPTS} 次点击；"
                    f"静默等待 {CLICK_RESPONSE_SECONDS:g} 秒后再检查"
                )
                quiet_registration_delay(page)
                latest = monitor.snapshot()
                changed, _reason = registration_activity_changed(after, latest)
                after = latest
            if monitor.available and not changed:
                raise RuntimeError(
                    f"密码提交最多点击 {MAX_NO_RESPONSE_CLICK_ATTEMPTS} 次后"
                    "仍无请求或页面响应"
                )
            mark_registration_chain(
                self,
                "password_submitted",
                page=page,
                detail=f"密码已填写并提交；总点击 {attempt} 次，等待最终确认",
            )
            return result

        worker._fill_password_step = types.MethodType(password_with_chain, worker)

    original_profile_fill = getattr(worker, "_fill_about_you_inputs", None)
    if callable(original_profile_fill):
        def profile_fill_with_chain(self, page, *args, **kwargs):
            begin_registration_step(
                self,
                "profile_verified",
                page=page,
                value="正在填写并回读姓名和生日/年龄",
            )
            result = original_profile_fill(page, *args, **kwargs)
            mark_registration_chain(
                self,
                "profile_verified",
                page=page,
                detail="姓名与生日/年龄逐字段回读一致",
            )
            quiet_registration_delay(page)
            return result

        worker._fill_about_you_inputs = types.MethodType(profile_fill_with_chain, worker)

    original_profile_click = getattr(worker, "_click_finish_creating_account", None)
    if callable(original_profile_click):
        def profile_click_with_activity(self, page, *args, **kwargs):
            monitor = ensure_registration_activity_monitor(page)
            begin_registration_step(
                self,
                "profile_submitted",
                page=page,
                value="等待点击基础资料提交按钮",
            )
            attempts = MAX_NO_RESPONSE_CLICK_ATTEMPTS if monitor.available else 1
            for attempt in range(1, attempts + 1):
                before = monitor.snapshot()
                clicked = bool(original_profile_click(page, *args, **kwargs))
                if not clicked:
                    return False
                result = wait_for_registration_activity(page, before)
                if result.get("changed") or not monitor.available:
                    mark_registration_chain(
                        self,
                        "profile_submitted",
                        page=page,
                        detail=f"第 {attempt} 次点击后检测到请求或页面变化",
                    )
                    return True
                self.log(
                    f"[请求监测] 基础资料第 {attempt} 次点击后 "
                    f"{CLICK_RESPONSE_SECONDS:g} 秒无请求、"
                    "无响应且页面未变化"
                )
            raise RuntimeError("基础资料提交最多点击 5 次后仍无请求或页面响应")

        worker._click_finish_creating_account = types.MethodType(
            profile_click_with_activity,
            worker,
        )

    original_wait_code = getattr(worker, "_wait_for_openai_email_code", None)
    if callable(original_wait_code):
        def wait_code_with_chain(self, *args, **kwargs):
            page = getattr(self, "_hme_registration_active_page", None)
            begin_registration_step(
                self,
                "verification_requested",
                page=page,
                value="正在请求本轮最新邮箱验证码",
            )
            mark_registration_chain(self, "verification_requested", page=page)
            code = original_wait_code(*args, **kwargs)
            self._hme_last_email_code = str(code or "")
            begin_registration_step(
                self,
                "verification_code_received",
                page=page,
                value="验证码已返回，正在确认格式",
            )
            mark_registration_chain(
                self,
                "verification_code_received",
                page=page,
                detail="已获取 6 位验证码（内容不记录）",
            )
            if page is not None:
                quiet_registration_delay(page)
            return code

        worker._wait_for_openai_email_code = types.MethodType(wait_code_with_chain, worker)

    original_validate_code = getattr(worker, "_validate_email_code_api", None)
    if callable(original_validate_code):
        def validate_code_with_chain(self, page, code, *args, **kwargs):
            begin_registration_step(
                self,
                "verification_code_entered",
                page=page,
                value="正在输入验证码并回读 6 位输入框",
            )
            ensure_email_verification_code_entered(self, page, code)
            mark_registration_chain(
                self,
                "verification_code_entered",
                page=page,
                detail="验证码已原子写入并连续两次回读一致",
            )
            before = registration_activity_snapshot(page)
            begin_registration_step(
                self,
                "verification_submitted",
                page=page,
                value="正在提交验证码校验请求",
            )
            result = original_validate_code(page, code, *args, **kwargs)
            after = registration_activity_snapshot(page)
            changed, _reason = registration_activity_changed(before, after)
            mark_registration_chain(
                self,
                "verification_submitted",
                page=page,
                detail=("验证码请求与响应已变化" if changed else "验证码接口已返回"),
            )
            snapshot = registration_chain_snapshot(self, page)
            if str(snapshot.get("nextCode") or "") == "registration_created":
                begin_registration_step(
                    self,
                    "registration_created",
                    page=page,
                    value="正在确认验证码校验结果",
                )
                mark_registration_chain(
                    self,
                    "registration_created",
                    page=page,
                    detail=(
                        "验证码接口成功且检测到请求变化"
                        if changed
                        else "验证码接口已返回成功"
                    ),
                )
            return result

        worker._validate_email_code_api = types.MethodType(
            validate_code_with_chain,
            worker,
        )

    original_submit_code = getattr(worker, "_submit_email_code", None)
    if callable(original_submit_code):
        def submit_code_with_chain(self, page, *args, **kwargs):
            self._hme_registration_active_page = page
            begin_registration_step(
                self,
                "verification_page",
                page=page,
                value="正在确认邮箱验证码输入界面",
            )
            mark_registration_chain(
                self,
                "verification_page",
                page=page,
                detail="已识别验证码输入控件",
            )
            try:
                result = original_submit_code(page, *args, **kwargs)
            finally:
                self._hme_registration_active_page = None
            snapshot = registration_chain_snapshot(self, page)
            if str(snapshot.get("nextCode") or "") == "verification_code_entered":
                begin_registration_step(
                    self,
                    "verification_code_entered",
                    page=page,
                    value="正在确认浏览器验证码输入结果",
                )
                mark_registration_chain(
                    self,
                    "verification_code_entered",
                    page=page,
                    detail="浏览器验证码输入步骤已返回成功（内容不记录）",
                )
                snapshot = registration_chain_snapshot(self, page)
            if str(snapshot.get("nextCode") or "") == "verification_submitted":
                begin_registration_step(
                    self,
                    "verification_submitted",
                    page=page,
                    value="正在确认验证码提交结果",
                )
                mark_registration_chain(
                    self,
                    "verification_submitted",
                    page=page,
                    detail="验证码提交步骤已返回成功",
                )
                snapshot = registration_chain_snapshot(self, page)
            if str(snapshot.get("nextCode") or "") == "registration_created":
                begin_registration_step(
                    self,
                    "registration_created",
                    page=page,
                    value="正在确认验证码校验结果",
                )
                mark_registration_chain(self, "registration_created", page=page)
            return result

        worker._submit_email_code = types.MethodType(submit_code_with_chain, worker)

    worker._hme_request_driven_registration_configured = True
    return True


def finalize_registration_chain(
    worker: Any,
    *,
    password_confirmed: bool,
    two_factor_enabled: bool,
) -> dict[str, Any]:
    if password_confirmed:
        snapshot = registration_chain_snapshot(worker)
        password_is_next = str(snapshot.get("nextCode") or "") == "password_confirmed"
        password_is_optional = "password_confirmed" not in _required_chain_codes(
            worker,
            _chain_state(worker),
        )
        if password_is_next:
            begin_registration_step(
                worker,
                "password_confirmed",
                value="正在确认密码设置结果",
            )
        if password_is_next or password_is_optional:
            mark_registration_chain(worker, "password_confirmed")
    if two_factor_enabled:
        snapshot = registration_chain_snapshot(worker)
        two_factor_is_next = str(snapshot.get("nextCode") or "") == "two_factor_enabled"
        two_factor_is_optional = "two_factor_enabled" not in _required_chain_codes(
            worker,
            _chain_state(worker),
        )
        if two_factor_is_next:
            begin_registration_step(
                worker,
                "two_factor_enabled",
                value="正在确认 TOTP 2FA 启用状态",
            )
        if two_factor_is_next or two_factor_is_optional:
            mark_registration_chain(worker, "two_factor_enabled")
    required_password = bool(getattr(worker, "_hme_chain_require_password", False))
    required_two_factor = bool(getattr(worker, "_hme_chain_enable_two_factor", False))
    if (not required_password or password_confirmed) and (
        not required_two_factor or two_factor_enabled
    ):
        snapshot = registration_chain_snapshot(worker)
        if str(snapshot.get("nextCode") or "") == "complete":
            begin_registration_step(
                worker,
                "complete",
                value="正在保存完整注册结果",
            )
            return mark_registration_chain(worker, "complete")
    return registration_chain_snapshot(worker)


def fail_registration_chain(worker: Any, error: Any) -> dict[str, Any]:
    return mark_registration_chain(worker, "failed", detail=str(error or ""))


__all__ = [
    "CLICK_RESPONSE_SECONDS",
    "MAX_NO_RESPONSE_CLICK_ATTEMPTS",
    "begin_page_registration_step",
    "begin_registration_step",
    "configure_request_driven_registration",
    "ensure_registration_activity_monitor",
    "fail_registration_chain",
    "finalize_registration_chain",
    "mark_page_registration_milestone",
    "mark_registration_chain",
    "quiet_registration_delay",
    "registration_activity_changed",
    "registration_activity_snapshot",
    "registration_chain_snapshot",
    "skip_page_registration_step",
    "skip_registration_step",
    "wait_for_registration_activity",
]
