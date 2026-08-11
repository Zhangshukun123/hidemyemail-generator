"""Registration email-code readers and manual verification hooks."""

from __future__ import annotations

import os
import re
import secrets
import time
import types
from datetime import datetime, timezone

try:
    from .openai_browser_dom import _activate_visible_registration_page
    from .openai_browser_selectors import (
        LOCALIZED_EMAIL_OTP_INPUT_SELECTORS,
        OTP_POLL_INTERVAL_SECONDS,
    )
except ImportError:
    from openai_browser_dom import _activate_visible_registration_page
    from openai_browser_selectors import (
        LOCALIZED_EMAIL_OTP_INPUT_SELECTORS,
        OTP_POLL_INTERVAL_SECONDS,
    )


def iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


EMAIL_VERIFICATION_CONTEXT_INPUT_SELECTORS = (
    'input:not([type])',
    'input[type="text"]',
    'input[type="number"]',
    'input[type="tel"]',
    'input[role="textbox"]',
)
EMAIL_VERIFICATION_UI_MARKERS = {
    "日文": (
        "受信箱を確認",
        "検証コード",
        "確認コード",
        "メールを再送信",
        "続行",
    ),
    "中文": (
        "检查收件箱",
        "查看收件箱",
        "验证码",
        "重新发送邮件",
        "继续",
    ),
    "英文": (
        "check your inbox",
        "verification code",
        "enter the code",
        "resend email",
        "continue",
    ),
}


def _email_verification_ui_state(
    page,
    expected_email: str,
    visible_inputs,
) -> dict[str, object]:
    """Combine task context with URL and visible text before touching OTP UI."""

    url = str(getattr(page, "url", "") or "")
    try:
        body_text = str(page.locator("body").inner_text(timeout=800) or "")
    except Exception:
        body_text = ""
    normalized = re.sub(r"\s+", " ", body_text).strip()
    folded = normalized.casefold()
    target = str(expected_email or "").strip().casefold()
    locale_label = ""
    marker_count = 0
    for label, markers in EMAIL_VERIFICATION_UI_MARKERS.items():
        count = sum(marker.casefold() in folded for marker in markers)
        if count > marker_count:
            locale_label = label
            marker_count = count
    try:
        inputs = visible_inputs(
            page,
            list(EMAIL_VERIFICATION_CONTEXT_INPUT_SELECTORS),
        )
    except Exception:
        inputs = []
    route_match = "email-verification" in url.casefold()
    email_match = bool(target and target in folded)
    has_input = bool(inputs)
    content_match = marker_count >= 2
    signal_count = sum((route_match, email_match, content_match))
    return {
        "recognized": has_input and signal_count >= 2,
        "url": url,
        "routeMatch": route_match,
        "emailMatch": email_match,
        "contentMatch": content_match,
        "locale": locale_label or "本地化",
        "markerCount": marker_count,
        "inputs": inputs,
    }


class ManualOtpReader:
    def __init__(self, account, log, _proxy_url: str = "") -> None:
        import requests

        self.email = str(account.email or "").strip().lower()
        self.icloud_inbox = self.email.endswith("@icloud.com")
        self.log = log
        self.service_url = os.environ.get(
            "HME_BROWSER_SERVICE_URL", "http://127.0.0.1:8765"
        ).rstrip("/")
        self.token = os.environ.get("HME_BROWSER_WORKER_TOKEN", "")
        self.session = requests.Session()
        self.session.trust_env = False

    def connect(self) -> None:
        if not self.token:
            raise RuntimeError("浏览器工作器令牌未配置")
        try:
            response = self.session.get(self.service_url + "/healthz", timeout=5)
            response.raise_for_status()
        except Exception as error:
            raise RuntimeError(f"无法连接手动验证码服务：{error}") from error
        icloud_inbox = getattr(
            self, "icloud_inbox", self.email.endswith("@icloud.com")
        )
        self.log(
            "iCloud 验证码通道已连接；将自动扫描收件箱与垃圾邮件"
            if icloud_inbox
            else "手动验证码通道已连接；需要验证码时请在工作台输入"
        )

    def wait_for_code(self, _min_timestamp: float) -> str:
        deadline = time.time() + 600
        last_error = ""
        request_id = secrets.token_urlsafe(12)
        standalone_code_route_logged = False
        icloud_inbox = getattr(
            self, "icloud_inbox", self.email.endswith("@icloud.com")
        )
        while time.time() < deadline:
            try:
                if icloud_inbox:
                    response = self.session.post(
                        self.service_url + "/api/gpt-code",
                        headers={"X-Local-Token": self.token},
                        json={
                            "email": self.email,
                            "since": iso_timestamp(float(_min_timestamp or 0)),
                        },
                        timeout=10,
                    )
                else:
                    response = self.session.post(
                        self.service_url + "/api/registration/code/poll",
                        headers={"X-Local-Token": self.token},
                        json={
                            "email": self.email,
                            "requestId": request_id,
                            "minTimestamp": float(_min_timestamp or 0),
                        },
                        timeout=10,
                    )
                payload = response.json()
                registration_error = str(payload.get("error") or "")
                if (
                    not icloud_inbox
                    and response.status_code == 409
                    and "没有正在运行的注册任务" in registration_error
                ):
                    if not standalone_code_route_logged:
                        self.log(
                            "[验证码] 当前是独立账号任务，改从 SMSBower 邮件历史获取下一封新验证码"
                        )
                        standalone_code_route_logged = True
                    response = self.session.post(
                        self.service_url + "/api/gpt-code",
                        headers={"X-Local-Token": self.token},
                        json={
                            "email": self.email,
                            "since": iso_timestamp(float(_min_timestamp or 0)),
                        },
                        timeout=10,
                    )
                    payload = response.json()
                elif response.status_code == 409 and registration_error:
                    raise RuntimeError(registration_error)
                if response.status_code == 404:
                    time.sleep(OTP_POLL_INTERVAL_SECONDS)
                    continue
                if response.ok and payload.get("ok"):
                    code = re.sub(r"[^A-Za-z0-9]", "", str(payload.get("code") or ""))
                    if 4 <= len(code) <= 10:
                        self.log("已安全取得本次所需的邮箱验证码")
                        return code
                last_error = str(payload.get("error") or f"HTTP {response.status_code}")
            except RuntimeError:
                raise
            except Exception as error:
                last_error = str(error)
            time.sleep(OTP_POLL_INTERVAL_SECONDS)
        detail = f"：{last_error}" if last_error else ""
        raise TimeoutError(f"在 600 秒内未收到邮箱验证码{detail}")

    def close(self) -> None:
        self.session.close()


def configure_registration_otp_reader(app_backend, registration_email: str) -> bool:
    """Route supported registration mail through the local code APIs."""
    target_email = str(registration_email or "").strip().lower()
    if not target_email.endswith(("@gmail.com", "@icloud.com")):
        return False
    icloud_inbox = target_email.endswith("@icloud.com")

    worker_type = app_backend.OpenAIRegisterPayLinkWorker
    original_preconnect = worker_type._preconnect_otp_reader
    original_wait_for_code = worker_type._wait_for_openai_email_code
    original_submit_email_code = getattr(worker_type, "_submit_email_code", None)
    original_visible_inputs = getattr(worker_type, "_visible_inputs", None)
    reader_type = app_backend.HotmailOtpReader

    def is_supported_worker(worker) -> bool:
        account = getattr(worker, "account", None)
        email = str(getattr(account, "email", "") or "").strip().lower()
        return email == target_email

    def preconnect_otp_reader(worker) -> None:
        if not is_supported_worker(worker):
            return original_preconnect(worker)
        if getattr(worker, "otp_reader", None):
            return
        worker.log(
            "iCloud 邮箱自动扫描 INBOX 与垃圾邮件取码"
            if icloud_inbox
            else "Gmail 账号使用 SMSBower API 自动取码，不连接 Outlook Graph/IMAP"
        )
        worker.otp_reader = reader_type(worker.account, worker.log, "")
        worker.otp_reader.connect()

    def wait_for_openai_email_code(worker, min_timestamp: float) -> str:
        if not is_supported_worker(worker):
            return original_wait_for_code(worker, min_timestamp)
        if not getattr(worker, "otp_reader", None):
            preconnect_otp_reader(worker)
        worker.log(
            "正在从 iCloud 转发收件箱与垃圾邮件等待验证码"
            if icloud_inbox
            else "正在等待 SMSBower 返回 Gmail 验证码"
        )
        return worker.otp_reader.wait_for_code(min_timestamp)

    def visible_inputs_with_localized_email_code(worker, page, selectors):
        inputs = original_visible_inputs(worker, page, selectors)
        if not is_supported_worker(worker):
            return inputs
        selector_text = " ".join(str(selector or "") for selector in selectors)
        is_email_code_lookup = any(
            marker in selector_text
            for marker in (
                "one-time-code",
                'name="code"',
                'inputmode="numeric"',
                'type="tel"',
            )
        )
        if not is_email_code_lookup:
            return inputs
        ui_state = _email_verification_ui_state(
            page,
            target_email,
            original_visible_inputs.__get__(worker, type(worker)),
        )
        if inputs:
            if ui_state["recognized"] and not getattr(
                worker, "_hme_otp_fill_context_logged", False
            ):
                worker._hme_otp_fill_context_logged = True
                worker.log(
                    f"[验证码] 新验证码已到达；已再次确认目标邮箱、页面 URL、"
                    f"{ui_state['locale']}验证文案和 Code 输入框，正在自动填写并提交"
                )
            return inputs
        page_url = str(getattr(page, "url", "") or "").lower()
        retry_count = (
            20
            if getattr(worker, "_hme_waiting_to_fill_otp_input", False)
            and (
                "email-verification" in page_url
                or bool(ui_state["recognized"])
            )
            else 1
        )
        for attempt in range(retry_count):
            current_page_url = str(getattr(page, "url", "") or "").lower()
            if attempt:
                time.sleep(0.25)
                current_page_url = str(getattr(page, "url", "") or "").lower()
                if "email-verification" not in current_page_url:
                    return []
                inputs = original_visible_inputs(worker, page, selectors)
                if inputs:
                    worker.log("[验证码] Code 输入框重渲染后已恢复，继续自动填写")
                    return inputs
            for selector in LOCALIZED_EMAIL_OTP_INPUT_SELECTORS:
                localized_inputs = original_visible_inputs(worker, page, [selector])
                if not localized_inputs:
                    continue
                if not getattr(worker, "_hme_localized_otp_input_logged", False):
                    worker._hme_localized_otp_input_logged = True
                    worker.log("[验证码] 已识别本地化 Code 输入框，准备自动填写")
                return localized_inputs
            ui_state = _email_verification_ui_state(
                page,
                target_email,
                original_visible_inputs.__get__(worker, type(worker)),
            )
            if ui_state["recognized"]:
                context_inputs = list(ui_state["inputs"])
                if len(context_inputs) == 1:
                    if not getattr(worker, "_hme_otp_fill_context_logged", False):
                        worker._hme_otp_fill_context_logged = True
                        worker.log(
                            f"[验证码] 新验证码已到达；已结合目标邮箱、页面 URL、"
                            f"{ui_state['locale']}界面文案识别唯一 Code 输入框，"
                            "正在自动填写并提交"
                        )
                    return context_inputs
            if "email-verification" in current_page_url:
                text_inputs = original_visible_inputs(
                    worker,
                    page,
                    list(EMAIL_VERIFICATION_CONTEXT_INPUT_SELECTORS),
                )
                if len(text_inputs) == 1:
                    worker.log("[验证码] 已按邮箱验证页面的唯一文本框定位 Code 输入框")
                    return text_inputs
        return inputs

    def submit_email_code_with_stable_input(
        worker,
        page,
        min_timestamp,
        *,
        wait_for_session=True,
    ):
        if not is_supported_worker(worker):
            return original_submit_email_code(
                worker,
                page,
                min_timestamp,
                wait_for_session=wait_for_session,
            )
        ui_state = _email_verification_ui_state(
            page,
            target_email,
            original_visible_inputs.__get__(worker, type(worker)),
        )
        if ui_state["recognized"]:
            worker.log(
                f"[验证码] 已结合当前注册上下文、页面 URL、目标邮箱、"
                f"{ui_state['locale']}界面文案和 Code 输入框识别邮箱验证页；"
                "下一步自动扫描 INBOX 与垃圾邮件"
            )
        else:
            worker.log(
                "[验证码] 当前注册流程已进入邮箱验证码分支；"
                "正在继续核对页面 URL、目标邮箱、可见文案和 Code 输入框"
            )
        worker._hme_waiting_to_fill_otp_input = True
        try:
            return original_submit_email_code(
                worker,
                page,
                min_timestamp,
                wait_for_session=wait_for_session,
            )
        finally:
            worker._hme_waiting_to_fill_otp_input = False

    worker_type._preconnect_otp_reader = preconnect_otp_reader
    worker_type._wait_for_openai_email_code = wait_for_openai_email_code
    if callable(original_submit_email_code):
        worker_type._submit_email_code = submit_email_code_with_stable_input
    if callable(original_visible_inputs):
        worker_type._visible_inputs = visible_inputs_with_localized_email_code
    return True


def configure_manual_browser_verification(
    worker,
    *,
    enabled: bool,
    activate_page=None,
) -> bool:
    """Wait for a user to submit the email code in the visible browser."""

    activate_page = activate_page or _activate_visible_registration_page
    if not enabled:
        return False
    if getattr(worker, "_hme_manual_browser_verification_configured", False):
        return True

    def skip_automatic_code_reader(self) -> None:
        self.otp_reader = None
        self.log(
            "[验证码] 自有邮箱使用浏览器手动输入；"
            "本次注册不连接 IMAP，也不调用自动取码服务"
        )

    def wait_for_manual_browser_submit(
        self,
        page,
        _min_timestamp: float,
        *,
        wait_for_session: bool = True,
    ) -> None:
        del wait_for_session
        self.log("[验证码] 请在浏览器中手动输入邮箱验证码并点击继续")
        activate_page(self, page)
        deadline = time.time() + 600
        code_input_seen = False
        while time.time() < deadline:
            self._raise_if_page_closed(page, "手动输入邮箱验证码")
            if self._has_chatgpt_session(page):
                self.log("[验证码] 已检测到登录会话，继续完成后续注册操作")
                return
            url = str(getattr(page, "url", "") or "")
            if self._has_otp_input(page):
                code_input_seen = True
                time.sleep(0.5)
                continue
            if code_input_seen or "email-verification" not in url:
                self.log("[验证码] 已检测到你提交验证码，继续完成后续注册操作")
                return
            time.sleep(0.5)
        raise TimeoutError("在 600 秒内未检测到手动验证码提交")

    worker._preconnect_otp_reader = types.MethodType(skip_automatic_code_reader, worker)
    worker._submit_email_code = types.MethodType(wait_for_manual_browser_submit, worker)
    worker._hme_manual_browser_verification_configured = True
    return True
