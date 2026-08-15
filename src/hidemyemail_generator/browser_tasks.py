from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from .browser_diagnostics import browser_diagnostic_context
from .inbox import connect_db, mark_address
from .registration_proxy import RegistrationProxyStore
from .registration_retry_policy import (
    RegistrationRetryContext,
    RegistrationRetryPolicy,
    build_reliability_report,
)
from .roxy_registration import RoxyRegistrationStore


EVENT_PREFIX = "HME_BROWSER_EVENT:"
MAX_LOG_ITEMS = 300
MAX_GOOGLE_FINGERPRINT_RETRIES = 1
MAX_WORKER_LAUNCHES = 3
DEFAULT_FAILURE_CIRCUIT_THRESHOLD = 3
AccountSaved = Callable[[str], Awaitable[dict[str, Any] | None]]
RetrySleep = Callable[[float], Awaitable[None]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def registration_email_type(email: str) -> str:
    """Return a stable provider label suitable for registration comparisons."""

    target = str(email or "").strip().lower()
    if target.endswith("@icloud.com"):
        return "icloud_hide_my_email"
    if target.endswith("@gmail.com"):
        return "gmail_smsbower"
    if target.endswith(("@outlook.com", "@hotmail.com", "@live.com")):
        return "microsoft_mail"
    domain = target.rsplit("@", 1)[-1] if "@" in target else ""
    return f"other:{domain}" if domain else "other"


def _masked_proxy_endpoint(proxy_url: str, fallback: str = "") -> str:
    value = str(proxy_url or "").strip()
    if value:
        try:
            parsed = urlsplit(value if "://" in value else f"http://{value}")
            host = str(parsed.hostname or "").strip()
            port = parsed.port
            if host:
                host_text = f"[{host}]" if ":" in host else host
                return f"{host_text}:{port}" if port else host_text
        except ValueError:
            pass
    safe_fallback = str(fallback or "").strip()
    if "@" in safe_fallback:
        safe_fallback = safe_fallback.rsplit("@", 1)[-1]
    return safe_fallback[:255]


def build_registration_environment(
    email: str,
    *,
    registration_mode: str,
    proxy_url: str = "",
    proxy_state: dict[str, Any] | None = None,
    proxy_mode: str = "",
    proxy_country: str = "",
) -> dict[str, Any]:
    """Build a credential-free snapshot of one registration network."""

    state = proxy_state if isinstance(proxy_state, dict) else {}
    enabled = bool(str(proxy_url or "").strip())
    selected_proxy_mode = str(proxy_mode or state.get("mode") or "").strip()
    return {
        "registration_mode": str(registration_mode or "").strip(),
        "email_type": registration_email_type(email),
        "proxy_enabled": enabled,
        "proxy_mode": selected_proxy_mode if enabled else "direct",
        "proxy_country": str(
            proxy_country or state.get("country") or ""
        ).strip().upper(),
        "proxy_country_label": str(state.get("countryLabel") or "").strip(),
        "proxy_endpoint": _masked_proxy_endpoint(
            proxy_url, str(state.get("endpoint") or "")
        ),
        "proxy_node": str(state.get("currentNode") or "").strip(),
        "proxy_selector": str(state.get("selector") or "").strip(),
        "proxy_latency_ms": max(0, int(state.get("lastLatencyMs") or 0)),
        "exit_ip": str(state.get("exitIp") or "").strip(),
        "exit_country": str(state.get("exitCountry") or "").strip().upper(),
        "captured_at": utc_now(),
    }


def browser_log_context(
    message: str, *, browser_engine: str = ""
) -> dict[str, str]:
    """Turn a worker message into a concise, UI-friendly execution context."""

    text = str(message or "").strip()
    normalized = text.casefold()
    selected_browser_engine = str(browser_engine or "").strip().lower()
    browser_name = (
        "Roxy"
        if selected_browser_engine == "roxy" or "roxy" in normalized
        else "Camoufox"
    )
    structured = browser_diagnostic_context(text)
    if structured is not None:
        if browser_name == "Roxy" and structured.get("stage") == "browser":
            structured = dict(structured)
            structured["location"] = str(structured.get("location") or "").replace(
                "Camoufox", "Roxy"
            )
            structured["action"] = str(structured.get("action") or "").replace(
                "Camoufox", "Roxy"
            )
        return structured
    detail = text
    if text.startswith("[") and "]" in text:
        detail = text.split("]", 1)[1].strip()
    detail = (detail.split("；", 1)[0] or text)[:120]

    completed_counts = re.search(
        r"成功\s*(\d+).*?失败\s*(\d+).*?跳过\s*(\d+)", normalized
    )
    status = "active"
    if completed_counts and int(completed_counts.group(2)) == 0:
        status = "success"
    elif any(marker in normalized for marker in ("失败", "错误", "异常", "http 401", "超时")):
        status = "error"
    elif any(marker in normalized for marker in ("停止", "取消", "仍是", "未完成", "跳过")):
        status = "warning"
    elif any(marker in normalized for marker in ("等待", "请在", "手动", "继续监测")):
        status = "waiting"
    elif any(marker in normalized for marker in ("成功", "已保存", "已开启", "校验通过", "已确认")):
        status = "success"

    stage = "running"
    location = "注册任务"
    action = detail or "处理浏览器任务"

    google_page_markers = (
        "Google 账号登录页面",
        "Google 登录页",
        "Google 登录要求",
        "Google 页面返回",
        "从 Google 返回",
    )
    if "2fa" in normalized or "totp" in normalized:
        stage = "two_factor"
        location = "OpenAI 两步验证"
        action = "配置并保存 TOTP 2FA"
    elif any(marker in text for marker in ("注册完成", "浏览器取全部完成")) or (
        "注册成功" in text and "Session 已保存" in text
    ):
        stage = "completed"
        location = "注册完成"
        action = "保存账号结果并完成任务"
    elif any(marker in normalized for marker in ("smsbower", "购买 openai gmail", "购买 gmail")):
        stage = "provider"
        location = "SMSBower 服务"
        action = "获取 Gmail 与邮箱验证码" if "验证码" in text else "获取 Gmail 注册邮箱"
    elif any(marker in text for marker in google_page_markers):
        stage = "google_oauth"
        location = "Google 登录页"
        if "全新指纹" in text or "更换指纹" in text:
            action = "关闭当前浏览器并更换指纹"
        elif any(marker in text for marker in ("已从 Google 返回", "已重新打开", "重新输入邮箱")):
            action = "返回 OpenAI 并重新输入邮箱"
        elif "仍是" in text or "返回失败" in text:
            action = "保持浏览器并继续监测返回"
        else:
            action = "识别误入页面并返回 OpenAI"
    elif any(marker in text for marker in ("安全验证", "security-check", "challenge")):
        stage = "security"
        location = "安全验证页"
        action = "等待手动完成安全验证，完成后自动继续"
    elif "密码" in text or "password" in normalized:
        stage = "password"
        location = "OpenAI 密码页"
        if "等待" in text or "使用密码继续" in text:
            action = "等待密码输入页并持续监测"
        elif any(marker in text for marker in ("已提交", "已设置", "保存唯一密码")):
            action = "填写并提交账号密码"
        else:
            action = "检查并处理密码登录"
    elif "验证码" in text or "verification" in normalized or "otp" in normalized:
        stage = "email_verification"
        location = "邮箱验证码页"
        if any(
            marker in text
            for marker in (
                "已点击验证码页面继续按钮",
                "已直接点击当前页面的继续按钮",
                "已通过接口提交邮箱验证码",
                "验证码提交请求已执行",
                "验证码提交步骤已返回成功",
                "已检测到你提交验证码",
            )
        ):
            action = "验证码已提交，等待页面跳转"
        elif any(
            marker in text
            for marker in (
                "输入检查通过",
                "输入框已回读一致",
                "验证码已输入并回读验证",
            )
        ):
            action = "校验验证码输入并点击继续"
        elif any(
            marker in text
            for marker in (
                "获取检查通过",
                "已安全取得本次所需的邮箱验证码",
                "已获取邮箱验证码",
                "已收到邮箱验证码",
                "自动取得 Gmail 验证码",
            )
        ):
            action = "验证码已获取，准备写入输入框"
        elif "下一步=读取本轮最新邮箱验证码" in text:
            action = "等待并监测邮箱验证码"
        elif any(
            marker in text
            for marker in ("等待", "请在", "轮询", "扫描", "重新发送邮件")
        ):
            action = "等待并监测邮箱验证码"
        elif any(
            marker in text
            for marker in ("Code 输入框", "验证码输入控件", "准备自动填写")
        ):
            action = "定位验证码输入框并准备写入"
        else:
            action = "核对邮箱验证码页面状态"
    elif any(
        marker in text
        for marker in (
            "邮箱登录页",
            "邮箱注册字段",
            "邮箱输入框",
            "Google 账号入口已禁用",
            "未点击 Google 登录按钮",
        )
    ):
        stage = "openai_auth"
        location = "OpenAI 邮箱登录页"
        action = "输入邮箱并进入密码流程"
    elif any(marker in text for marker in ("基础资料", "姓名", "出生", "年龄")):
        stage = "profile"
        location = "OpenAI 基础资料页"
        action = "填写并校验姓名与出生信息"
    elif "session" in normalized or "cookie" in normalized:
        stage = "session"
        location = "OpenAI Session 接口"
        action = "获取并保存 Session / Cookie"
    elif any(marker in text for marker in ("代理", "直连", "出口国家", "公网 IP")):
        stage = "network"
        location = "网络与代理检查"
        action = "确认注册出口与浏览器语言"
    elif any(
        marker in normalized
        for marker in ("camoufox", "roxy", "浏览器", "fontconfig", "窗口")
    ):
        stage = "browser"
        location = "浏览器运行环境"
        action = f"启动并监测 {browser_name} 浏览器"
    elif any(marker in text for marker in ("库存", "领取邮箱", "邮箱已添加", "准备注册邮箱")):
        stage = "prepare"
        location = "注册准备"
        action = "准备邮箱与账号凭据"
    if status == "error":
        action = detail or "检查失败原因"
    return {
        "stage": stage,
        "location": location,
        "action": action,
        "status": status,
    }


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def jwt_account_type(token: str) -> tuple[str, str]:
    """Read ChatGPT's plan claim from a saved JWT without making a request."""

    payload = decode_jwt_payload(token)
    auth_claim = payload.get("https://api.openai.com/auth")
    candidates: list[Any] = []
    if isinstance(auth_claim, dict):
        candidates.extend(
            (
                auth_claim.get("chatgpt_plan_type"),
                auth_claim.get("plan_type"),
            )
        )
    candidates.extend(
        (
            payload.get("chatgpt_plan_type"),
            payload.get("plan_type"),
        )
    )
    for value in candidates:
        raw_plan = str(value or "").strip()
        plan = raw_plan.casefold()
        if not plan:
            continue
        if any(marker in plan for marker in ("plus", "pro", "team", "enterprise")):
            return "plus", raw_plan
        if plan in {"free", "none", "no_plan", "chatgptfreeplan"}:
            return "free", raw_plan
        return "", raw_plan
    return "", ""


def access_token_is_expired(
    token: str, *, now: float | None = None, skew_seconds: int = 60
) -> bool:
    payload = decode_jwt_payload(token)
    try:
        expires_at = float(payload.get("exp") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if not expires_at:
        return True
    return expires_at <= (time.time() if now is None else now) + skew_seconds


def load_account_record(db_file: Path, email: str) -> dict[str, Any]:
    conn = connect_db(str(db_file))
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (f"gpt_account:{email.strip().lower()}",),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    try:
        payload = json.loads(str(row["value"] or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def account_session(record: dict[str, Any]) -> dict[str, Any]:
    """Return the saved ChatGPT Session object, including legacy JSON fields."""

    for key in ("session", "session_json", "sessionJson"):
        value = record.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


def account_session_access_token(record: dict[str, Any]) -> str:
    """Read the current token from Session first, then legacy top-level fields."""

    session = account_session(record)
    token = str(
        session.get("accessToken")
        or session.get("access_token")
        or record.get("access_token")
        or record.get("accessToken")
        or ""
    ).strip()
    return token


def account_session_token_cookie(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the reusable ChatGPT auth cookie exposed by Session JSON."""

    session = account_session(record)
    session_token = str(
        session.get("sessionToken") or session.get("session_token") or ""
    ).strip()
    if not session_token:
        return []
    return [
        {
            "name": "__Secure-next-auth.session-token",
            "value": session_token,
            "domain": "chatgpt.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
    ]


def account_saved_cookies(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return reusable browser cookies from current and legacy account fields."""

    raw_state = record.get("storage_state_json")
    if isinstance(raw_state, str) and raw_state.strip():
        try:
            raw_state = json.loads(raw_state)
        except (json.JSONDecodeError, TypeError, ValueError):
            raw_state = {}
    if isinstance(raw_state, dict):
        cookies = raw_state.get("cookies")
        if isinstance(cookies, list):
            normalized = [dict(item) for item in cookies if isinstance(item, dict)]
            if normalized:
                return normalized

    for key in ("cookies", "cookies_json"):
        cookies = record.get(key)
        if isinstance(cookies, str) and cookies.strip():
            try:
                cookies = json.loads(cookies)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        if isinstance(cookies, list):
            normalized = [dict(item) for item in cookies if isinstance(item, dict)]
            if normalized:
                return normalized
    return account_session_token_cookie(record)


def account_registration_proxy_url(record: dict[str, Any]) -> str:
    """Return the exact proxy URL retained when this account was registered."""

    direct = str(record.get("registration_proxy_url") or "").strip()
    if direct:
        return direct
    metadata = record.get("registration_proxy")
    if isinstance(metadata, dict):
        return str(metadata.get("url") or "").strip()
    return ""


def session_email(session: Any) -> str:
    if not isinstance(session, dict):
        return ""
    user = session.get("user")
    if isinstance(user, dict):
        email = str(user.get("email") or "").strip().lower()
        if email:
            return email
    return str(session.get("email") or "").strip().lower()


def session_account_type(session: Any) -> tuple[str, str]:
    if not isinstance(session, dict):
        return "", ""
    account = session.get("account")
    if not isinstance(account, dict):
        return "", ""
    raw_plan = str(account.get("planType") or account.get("plan_type") or "").strip()
    plan = raw_plan.casefold()
    if any(marker in plan for marker in ("plus", "pro", "team", "enterprise")):
        return "plus", raw_plan
    if plan in {"free", "none", "no_plan"}:
        return "free", raw_plan
    return "", raw_plan


def _save_account_record(
    db_file: Path,
    email: str,
    *,
    result: dict[str, Any] | None = None,
    password: str = "",
    password_confirmed: bool | None = None,
    two_factor: dict[str, Any] | None = None,
) -> None:
    target = email.strip().lower()
    current = load_account_record(db_file, target)
    current["email"] = target
    current["updated_at"] = utc_now()
    if password:
        current["password"] = password
    if password_confirmed is True:
        current["password_confirmed"] = True
        current["password_confirmed_at"] = utc_now()
    elif password_confirmed is False:
        current["password_confirmed"] = False
        current.pop("password_confirmed_at", None)
    if isinstance(two_factor, dict) and two_factor.get("secret"):
        current["two_factor"] = dict(two_factor)
    if result:
        access_token = str(result.get("access_token") or "").strip()
        session_json = str(result.get("session_json") or "").strip()
        storage_state_json = str(result.get("storage_state_json") or "").strip()
        cookies_json = str(result.get("cookies_json") or "").strip()
        acquisition_method = str(
            result.get("session_acquisition_method") or ""
        ).strip()
        registration_proxy_url = str(
            result.get("registration_proxy_url") or ""
        ).strip()
        registration_proxy = result.get("registration_proxy")
        registration_environment = result.get("registration_environment")
        registration_diagnostics = result.get("registration_diagnostics")
        if access_token or session_json:
            current.pop("session_invalid_at", None)
        if access_token:
            current["access_token"] = access_token
        if session_json:
            current["session_json"] = session_json
            try:
                parsed_session = json.loads(session_json)
            except (json.JSONDecodeError, TypeError, ValueError):
                parsed_session = session_json
            current["session"] = parsed_session
            session_type, raw_plan = session_account_type(parsed_session)
            if session_type and current.get("account_type_source") != "manual":
                current["account_type"] = session_type
                current["account_type_source"] = "session"
                current["verified_at"] = utc_now()
                current["verification_detail"] = (
                    f"最新登录 Session account.planType={raw_plan}"
                )
        if storage_state_json:
            current["storage_state_json"] = storage_state_json
        if cookies_json:
            current["cookies_json"] = cookies_json
            try:
                parsed_cookies = json.loads(cookies_json)
            except (json.JSONDecodeError, TypeError, ValueError):
                parsed_cookies = []
            if isinstance(parsed_cookies, list):
                current["cookies"] = parsed_cookies
        if acquisition_method:
            current["session_acquisition_method"] = acquisition_method
        if registration_proxy_url:
            current["registration_proxy_url"] = registration_proxy_url
        if isinstance(registration_proxy, dict) and registration_proxy:
            current["registration_proxy"] = dict(registration_proxy)
        if isinstance(registration_environment, dict) and registration_environment:
            current["registration_environment"] = dict(registration_environment)
            current["registration_completed_at"] = str(
                registration_environment.get("captured_at") or utc_now()
            )
        if isinstance(registration_diagnostics, dict) and registration_diagnostics:
            current["registration_diagnostics"] = dict(registration_diagnostics)
        result_two_factor = result.get("two_factor")
        if isinstance(result_two_factor, dict) and result_two_factor.get("secret"):
            current["two_factor"] = dict(result_two_factor)

    saved_cookies = account_saved_cookies(current)
    if saved_cookies:
        if not current.get("cookies"):
            current["cookies"] = saved_cookies
        if not current.get("cookies_json"):
            current["cookies_json"] = json.dumps(saved_cookies, ensure_ascii=False)
        if not current.get("storage_state_json"):
            current["storage_state_json"] = json.dumps(
                {"cookies": saved_cookies, "origins": []},
                ensure_ascii=False,
            )

    conn = connect_db(str(db_file))
    try:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (
                f"gpt_account:{target}",
                json.dumps(current, ensure_ascii=False),
            ),
        )
        conn.execute("DELETE FROM settings WHERE key = ?", (f"gpt_removed:{target}",))
        conn.commit()
        if result and str(result.get("access_token") or "").strip():
            mark_address(conn, target, "used")
    finally:
        conn.close()


def set_manual_account_type(
    db_file: Path, email: str, account_type: str
) -> dict[str, Any]:
    target = email.strip().lower()
    selected = str(account_type or "").strip().lower()
    if selected not in {"plus", "free", "unverified"}:
        raise ValueError("账号类型无效")
    current = load_account_record(db_file, target)
    current["email"] = target
    current["updated_at"] = utc_now()
    if selected == "unverified":
        current.pop("account_type", None)
        current.pop("account_type_source", None)
        current["verification_detail"] = "已手动恢复为等待验证"
    else:
        current["account_type"] = selected
        current["account_type_source"] = "manual"
        current["verified_at"] = utc_now()
        current["verification_detail"] = f"手动设置为 {selected.title()}"

    conn = connect_db(str(db_file))
    try:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (f"gpt_account:{target}", json.dumps(current, ensure_ascii=False)),
        )
        conn.execute("DELETE FROM settings WHERE key = ?", (f"gpt_removed:{target}",))
        conn.commit()
    finally:
        conn.close()
    return current


class BrowserTaskManager:
    def __init__(
        self,
        *,
        target_project_dir: Path,
        service_url: str,
        worker_token: str,
        db_file: Path,
        python_executable: Path | None = None,
        bridge_file: Path | None = None,
        force_headless: bool = False,
        registration_proxy_store: RegistrationProxyStore | None = None,
        roxy_registration_store: RoxyRegistrationStore | None = None,
        on_account_saved: AccountSaved | None = None,
        registration_retry_policy: RegistrationRetryPolicy | None = None,
        retry_sleep: RetrySleep | None = None,
        failure_circuit_threshold: int = DEFAULT_FAILURE_CIRCUIT_THRESHOLD,
    ) -> None:
        self.target_project_dir = target_project_dir.resolve()
        self.python_executable = (
            python_executable
            or self.target_project_dir / ".venv" / "Scripts" / "python.exe"
        ).resolve()
        self.bridge_file = (
            bridge_file or Path(__file__).with_name("openai_browser_bridge.py")
        ).resolve()
        self.service_url = service_url.rstrip("/")
        self.worker_token = worker_token
        self.db_file = db_file.resolve()
        self.force_headless = bool(force_headless)
        self.registration_proxy_store = registration_proxy_store
        self.roxy_registration_store = roxy_registration_store
        self.on_account_saved = on_account_saved
        if registration_retry_policy is None:
            try:
                configured_retries = int(
                    os.environ.get("HME_REGISTRATION_TRANSIENT_RETRIES", "2")
                )
            except (TypeError, ValueError):
                configured_retries = 2
            registration_retry_policy = RegistrationRetryPolicy(
                max_retries=configured_retries
            )
        self.registration_retry_policy = registration_retry_policy
        self._retry_sleep = retry_sleep or asyncio.sleep
        self.failure_circuit_threshold = max(
            1, min(10, int(failure_circuit_threshold))
        )
        self._batch_task: asyncio.Task | None = None
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._log_sequence = 0
        self._state: dict[str, Any] = self._idle_state()

    @staticmethod
    def _idle_state() -> dict[str, Any]:
        return {
            "id": "",
            "status": "idle",
            "running": False,
            "total": 0,
            "completed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "recovered": 0,
            "transientRetries": 0,
            "retriedAccounts": 0,
            "failureCounts": {},
            "lastFailureCode": "",
            "failureStreak": 0,
            "circuitOpen": False,
            "circuitReason": "",
            "reliability": build_reliability_report(0, 0).as_dict(),
            "accounts": [],
            "logs": [],
            "currentStage": "idle",
            "currentLocation": "等待任务",
            "currentAction": "尚未开始",
            "currentStatus": "idle",
            "startedAt": "",
            "finishedAt": "",
        }

    def availability(self) -> dict[str, Any]:
        missing: list[str] = []
        if not self.target_project_dir.is_dir():
            missing.append(f"目标项目不存在：{self.target_project_dir}")
        if not self.python_executable.is_file():
            missing.append(f"目标项目 Python 不存在：{self.python_executable}")
        if not (self.target_project_dir / "app_backend.py").is_file():
            missing.append("目标项目缺少 app_backend.py")
        if not self.bridge_file.is_file():
            missing.append("当前项目缺少浏览器桥接脚本")
        return {
            "available": not missing,
            "targetProject": str(self.target_project_dir),
            "python": str(self.python_executable),
            "errors": missing,
            "forceHeadless": self.force_headless,
        }

    def snapshot(self) -> dict[str, Any]:
        state = {
            key: value
            for key, value in self._state.items()
            if not key.startswith("_")
        }
        state["accounts"] = [
            {
                key: value
                for key, value in item.items()
                if not key.startswith("_")
            }
            for item in self._state.get("accounts", [])
        ]
        state["logs"] = list(self._state.get("logs", []))
        state["runtime"] = self.availability()
        return state

    def reset(self) -> dict[str, Any]:
        if self._batch_task and not self._batch_task.done():
            raise RuntimeError("浏览器获取任务正在运行")
        self._log_sequence = 0
        self._state = self._idle_state()
        return self.snapshot()

    def start(
        self,
        accounts: list[dict[str, Any]],
        *,
        headless: bool,
        concurrency: int,
        skipped: int = 0,
        use_registration_proxy: bool = False,
        browser_engine: str = "camoufox",
    ) -> dict[str, Any]:
        if self._batch_task and not self._batch_task.done():
            raise RuntimeError("浏览器获取任务正在运行")
        runtime = self.availability()
        if not runtime["available"]:
            raise RuntimeError("；".join(runtime["errors"]))

        deduplicated: list[dict[str, Any]] = []
        seen: set[str] = set()
        for account in accounts:
            email = str(account.get("email") or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            manual_otp_entry = bool(account.get("manual_otp_entry", False))
            gmail_registration = (
                not manual_otp_entry
                and email.endswith("@gmail.com")
                and bool(account.get("ensure_password", False))
            )
            deduplicated.append(
                {
                    "email": email,
                    "password": str(account.get("password") or ""),
                    "password_confirmed": bool(
                        account.get("password_confirmed", False)
                    ),
                    "ensure_password": bool(account.get("ensure_password", False)),
                    "force_reset_password": bool(
                        account.get("force_reset_password", False)
                    ),
                    "enable_2fa": bool(account.get("enable_2fa", False)),
                    "cookie_refresh_only": bool(
                        account.get("cookie_refresh_only", False)
                    ),
                    "manual_otp_entry": manual_otp_entry,
                    "password_first_required": bool(
                        account.get("password_first_required", False)
                        or gmail_registration
                    ),
                    "foreground_required": bool(
                        account.get("foreground_required", False)
                        or manual_otp_entry
                    ),
                    "two_factor": account.get("two_factor")
                    if isinstance(account.get("two_factor"), dict)
                    else {},
                }
            )
        if not deduplicated:
            raise RuntimeError("没有需要获取 Session 的 iCloud 邮箱")

        selected_browser_engine = str(browser_engine or "camoufox").strip().lower()
        if selected_browser_engine not in {"camoufox", "roxy"}:
            raise ValueError("不支持的注册浏览器引擎")
        if selected_browser_engine == "roxy":
            if self.roxy_registration_store is None:
                raise RuntimeError("Roxy 注册配置不可用")
        concurrency_limit = 5 if selected_browser_engine == "roxy" else 10
        concurrency = max(1, min(concurrency_limit, int(concurrency)))
        foreground_required = any(
            item["foreground_required"] for item in deduplicated
        )
        if foreground_required:
            concurrency = 1
        headless = bool(headless or self.force_headless) and not foreground_required
        proxy_state = (
            self.registration_proxy_store.public_state()
            if self.registration_proxy_store is not None
            else {"enabled": False, "configured": False}
        )
        proxy_active = bool(
            use_registration_proxy
            and proxy_state.get("enabled")
            and proxy_state.get("configured")
        )
        clash_proxy_active = bool(
            proxy_active and proxy_state.get("mode") == "clash"
        )
        if clash_proxy_active:
            if selected_browser_engine == "roxy" and proxy_state.get(
                "fixedPortsEnabled"
            ):
                concurrency = min(
                    concurrency,
                    max(1, int(proxy_state.get("fixedPortCount") or 1)),
                )
            else:
                concurrency = 1
        roxy_config: dict[str, Any] = {}
        roxy_profile_ids: list[str] = []
        if selected_browser_engine == "roxy":
            required_profiles = min(concurrency, len(deduplicated))
            if required_profiles > 1:
                roxy_config = self.roxy_registration_store.runtime_config(
                    profile_count=required_profiles
                )
            else:
                roxy_config = self.roxy_registration_store.runtime_config()
            roxy_profile_ids = [
                str(profile_id).strip()
                for profile_id in roxy_config.get("profileIds", [])
                if str(profile_id).strip()
            ]
            if not roxy_profile_ids and roxy_config.get("profileId"):
                roxy_profile_ids = [str(roxy_config["profileId"]).strip()]
            if len(roxy_profile_ids) < required_profiles:
                raise RuntimeError(
                    f"Roxy 可用环境不足：需要 {required_profiles} 个，"
                    f"当前仅分配到 {len(roxy_profile_ids)} 个"
                )
        self._log_sequence = 0
        self._state = {
            "id": uuid.uuid4().hex,
            "status": "running",
            "running": True,
            "headless": headless,
            "foregroundRequired": foreground_required,
            "concurrency": concurrency,
            "total": len(deduplicated),
            "targetCount": len(deduplicated),
            "loopCount": (len(deduplicated) + concurrency - 1) // concurrency,
            "completed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": max(0, int(skipped)),
            "recovered": 0,
            "transientRetries": 0,
            "retriedAccounts": 0,
            "failureCounts": {},
            "lastFailureCode": "",
            "failureStreak": 0,
            "circuitOpen": False,
            "circuitReason": "",
            "reliability": build_reliability_report(0, 0).as_dict(),
            "accounts": [
                {
                    "email": item["email"],
                    "status": "queued",
                    "message": "等待浏览器任务",
                    "latestLog": "",
                    "_password": item["password"],
                    "_ensure_password": item["ensure_password"],
                    "_force_reset_password": item["force_reset_password"],
                    "_password_confirmed": item["password_confirmed"],
                    "passwordConfirmed": item["password_confirmed"],
                    "_enable_2fa": item["enable_2fa"],
                    "_cookie_refresh_only": item["cookie_refresh_only"],
                    "_manual_otp_entry": item["manual_otp_entry"],
                    "manualOtpEntry": item["manual_otp_entry"],
                    "_password_first_required": item["password_first_required"],
                    "_foreground_required": item["foreground_required"],
                    "_two_factor": item["two_factor"],
                    "_fingerprint_retry_count": 0,
                    "fingerprintRetries": 0,
                    "_transient_retry_count": 0,
                    "transientRetries": 0,
                    "workerAttempts": 0,
                    "maxWorkerAttempts": MAX_WORKER_LAUNCHES,
                    "retryState": "idle",
                    "lastRetryCode": "",
                    "lastRetryStage": "",
                    "retryDelayMs": 0,
                    "retryHistory": [],
                    "phase": "queued",
                    "twoFactorEnabled": bool(item["two_factor"].get("enabled")),
                    "roxyProfileId": (
                        roxy_profile_ids[
                            slot_index % len(roxy_profile_ids)
                        ]
                        if roxy_profile_ids
                        else ""
                    ),
                    "_roxy_profile_id": (
                        roxy_profile_ids[
                            slot_index % len(roxy_profile_ids)
                        ]
                        if roxy_profile_ids
                        else ""
                    ),
                    "_window_slot": slot_index % min(concurrency, len(deduplicated)),
                    "_window_slots": min(concurrency, len(deduplicated)),
                    "loopIndex": slot_index // min(concurrency, len(deduplicated)) + 1,
                }
                for slot_index, item in enumerate(deduplicated)
            ],
            "logs": [],
            "currentStage": "prepare",
            "currentLocation": "任务队列",
            "currentAction": "准备启动浏览器任务",
            "currentStatus": "active",
            "startedAt": utc_now(),
            "finishedAt": "",
            "useRegistrationProxy": proxy_active,
            "registrationProxy": proxy_state,
            "browserEngine": selected_browser_engine,
            "roxyProfileId": str(roxy_config.get("profileId") or ""),
            "roxyProfileIds": list(roxy_profile_ids),
            "roxyApiUrl": str(roxy_config.get("apiUrl") or ""),
            "roxyWorkspaceId": str(roxy_config.get("workspaceId") or ""),
        }
        self._append_log(
            f"浏览器取全部已启动：待处理 {len(deduplicated)}，"
            f"跳过 {skipped}，并发 {concurrency}，"
            f"{'后台隐藏' if headless and selected_browser_engine == 'roxy' else '无头' if headless else '显示浏览器'}"
        )
        self._append_log(
            "启动参数："
            f"引擎 {'Roxy' if selected_browser_engine == 'roxy' else 'Camoufox'}；"
            f"账号 {len(deduplicated)} 个；并发 {concurrency}；"
            f"窗口模式 {'后台隐藏' if headless and selected_browser_engine == 'roxy' else '无头' if headless else '前台显示'}；"
            f"注册代理 {'已启用' if proxy_active else '未启用'}；"
            f"需要前台交互 {'是' if foreground_required else '否'}",
            source="browser_manager",
            event_type="task_parameters",
        )
        if selected_browser_engine == "roxy":
            self._append_log(
                f"Roxy 注册已启用：已为 {len(roxy_profile_ids)} 个并发窗口分配"
                "互不重复的专用环境；每个环境将在启动前清空缓存并生成全新随机指纹；"
                + ("窗口将后台隐藏" if headless else "窗口将在前台显示")
            )
        if foreground_required:
            if any(item["manual_otp_entry"] for item in deduplicated):
                self._append_log(
                    "自有邮箱手动验证码已锁定为单浏览器前台模式："
                    "请在浏览器中输入验证码并点击继续"
                )
            else:
                self._append_log(
                    "该浏览器任务已显式要求前台模式："
                    "禁用无头并避免并发窗口抢焦点"
                )
        else:
            if selected_browser_engine == "roxy":
                self._append_log(
                    "Roxy CDP 后台交互已启用：窗口被其他应用遮挡时仍继续加载和执行"
                    if headless
                    else "Roxy CDP 自动化已启用：窗口将在前台显示并持续执行"
                )
            else:
                self._append_log(
                    "Camoufox 后台交互已启用：窗口被其他应用遮挡时仍继续加载和执行"
                )
        if proxy_active:
            if clash_proxy_active:
                if proxy_state.get("fixedPortsEnabled"):
                    self._append_log(
                        (
                            "Clash 日本固定端口已启用：每个 Roxy 并发窗口分配独立固定端口；"
                            if selected_browser_engine == "roxy"
                            else "Clash 日本固定端口已启用：当前进程内保持单账号；"
                            "可同时启动其他注册进程，每个进程使用独立固定端口；"
                        )
                        + f"高于 {proxy_state.get('maxLatencyMs') or 900} ms 的出口会被跳过"
                    )
                else:
                    self._append_log(
                        "Clash 日本节点轮询已启用：注册任务强制串行；"
                        f"每个账号开始前切换节点并跳过高于 {proxy_state.get('maxLatencyMs') or 900} ms 的出口"
                    )
            else:
                self._append_log(
                    "注册动态代理已启用："
                    f"{proxy_state.get('countryLabel') or proxy_state.get('country')} "
                    "出口；每个账号使用独立 SID"
                )
        self._batch_task = asyncio.create_task(
            self._run_batch(headless=headless, concurrency=concurrency)
        )
        return self.snapshot()

    def _append_log(
        self,
        message: str,
        *,
        email: str = "",
        source: str = "browser_manager",
        event_type: str = "log",
    ) -> None:
        text = str(message or "").strip()
        if not text:
            return
        self._log_sequence += 1
        sequence = self._log_sequence
        task_id = str(self._state.get("id") or "")
        context = browser_log_context(
            text,
            browser_engine=str(self._state.get("browserEngine") or ""),
        )
        entry = {
            "sequence": sequence,
            "seq": sequence,
            "taskId": task_id,
            "originTaskId": task_id,
            "originSequence": sequence,
            "originSeq": sequence,
            "source": str(source or "browser_manager")[:80],
            "eventType": str(event_type or "log")[:80],
            "at": utc_now(),
            "email": email,
            "message": text[:1000],
            **context,
        }
        logs = self._state.setdefault("logs", [])
        logs.append(entry)
        if len(logs) > MAX_LOG_ITEMS:
            del logs[:-MAX_LOG_ITEMS]
        self._state.update(
            currentStage=context["stage"],
            currentLocation=context["location"],
            currentAction=context["action"],
            currentStatus=context["status"],
        )
        if email:
            item = self._account_item(email)
            if item is not None:
                item["latestLog"] = text[:500]
                item.update(
                    stage=context["stage"],
                    location=context["location"],
                    action=context["action"],
                    logStatus=context["status"],
                )

    def _account_item(self, email: str) -> dict[str, Any] | None:
        target = email.strip().lower()
        for item in self._state.get("accounts", []):
            if item.get("email") == target:
                return item
        return None

    def _refresh_reliability(self) -> None:
        self._state["reliability"] = build_reliability_report(
            int(self._state.get("succeeded") or 0),
            int(self._state.get("failed") or 0),
        ).as_dict()

    def _record_terminal_failure(self, reason_code: str) -> None:
        code = str(reason_code or "not_retryable")[:80]
        counts = self._state.setdefault("failureCounts", {})
        counts[code] = max(0, int(counts.get(code) or 0)) + 1
        if self._state.get("lastFailureCode") == code:
            self._state["failureStreak"] = max(
                0, int(self._state.get("failureStreak") or 0)
            ) + 1
        else:
            self._state["lastFailureCode"] = code
            self._state["failureStreak"] = 1
        streak = int(self._state["failureStreak"])
        circuit_codes = {
            "browser_closed",
            "network_interrupted",
            "navigation_stalled",
            "temporary_capacity",
            "worker_exit",
        }
        if (
            code in circuit_codes
            and streak >= self.failure_circuit_threshold
            and not self._state.get("circuitOpen")
        ):
            self._state["circuitOpen"] = True
            self._state["circuitReason"] = code
            self._append_log(
                "稳定性熔断已开启："
                f"同类瞬时故障 {code} 连续达到 {streak} 个账号；"
                "剩余账号保留在队列外，等待运行环境恢复"
            )

    @staticmethod
    async def _reap_worker_process(
        process: asyncio.subprocess.Process | None,
        stdout_task: asyncio.Task | None,
        stderr_task: asyncio.Task | None,
        *,
        terminate: bool,
    ) -> None:
        if terminate and process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), timeout=3)
            except asyncio.TimeoutError:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
        readers = [
            task
            for task in (stdout_task, stderr_task)
            if task is not None
        ]
        for task in readers:
            if not task.done():
                task.cancel()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)

    async def _run_batch(self, *, headless: bool, concurrency: int) -> None:
        semaphore = asyncio.Semaphore(concurrency)
        try:
            accounts = self._state["accounts"]
            if (
                self._state.get("browserEngine") == "roxy"
                and len(accounts) > concurrency
            ):
                async def run_roxy_slot(slot: int) -> None:
                    for item in accounts:
                        if int(item.get("_window_slot") or 0) != slot:
                            continue
                        await self._run_account(
                            item,
                            semaphore=semaphore,
                            headless=headless,
                        )

                await asyncio.gather(
                    *(run_roxy_slot(slot) for slot in range(concurrency))
                )
            else:
                await asyncio.gather(
                    *(
                        self._run_account(
                            item,
                            semaphore=semaphore,
                            headless=headless,
                        )
                        for item in accounts
                    )
                )
            self._refresh_reliability()
            if self._state["status"] == "cancelling":
                self._state["status"] = "cancelled"
                self._append_log("浏览器获取任务已停止")
            else:
                self._state["status"] = "completed"
                self._append_log(
                    f"浏览器取全部完成：成功 {self._state['succeeded']}，"
                    f"失败 {self._state['failed']}，跳过 {self._state['skipped']}"
                )
        except asyncio.CancelledError:
            self._state["status"] = "cancelled"
            self._append_log("浏览器获取任务已停止")
            raise
        finally:
            self._state["running"] = False
            self._state["finishedAt"] = utc_now()
            self._processes.clear()

    async def _run_account(
        self,
        item: dict[str, Any],
        *,
        semaphore: asyncio.Semaphore,
        headless: bool,
    ) -> None:
        email = str(item["email"])
        async with semaphore:
            if self._state.get("status") == "cancelling":
                item["status"] = "cancelled"
                item["message"] = "任务已停止"
                return
            if self._state.get("circuitOpen"):
                item["status"] = "skipped"
                item["phase"] = "circuit_open"
                item["message"] = (
                    "稳定性熔断已开启；账号未启动，等待运行环境恢复"
                )
                item["terminalReasonCode"] = "circuit_open"
                self._state["skipped"] += 1
                self._state["completed"] += 1
                self._append_log(item["message"], email=email)
                return
            existing_record = await asyncio.to_thread(
                load_account_record, self.db_file, email
            )
            is_new_registration = bool(
                not item.get("_cookie_refresh_only")
                and not account_session_access_token(existing_record)
                and not account_session(existing_record)
                and not account_saved_cookies(existing_record)
            )
            item["status"] = "running"
            item["phase"] = "registering_openai"
            browser_name = (
                "Roxy"
                if self._state.get("browserEngine") == "roxy"
                else "Camoufox"
            )
            item["message"] = f"正在启动 {browser_name}"
            self._append_log("开始浏览器注册或登录", email=email)

            proxy_url = ""
            proxy_state: dict[str, Any] = {}
            if self._state.get("useRegistrationProxy"):
                if self.registration_proxy_store is None:
                    item["status"] = "failed"
                    item["message"] = "注册代理配置不可用"
                    self._state["failed"] += 1
                    self._state["completed"] += 1
                    self._record_terminal_failure("proxy_configuration")
                    self._refresh_reliability()
                    self._append_log(item["message"], email=email)
                    return
                try:
                    proxy_url, proxy_state = await asyncio.to_thread(
                        self.registration_proxy_store.next_proxy
                    )
                except Exception as error:
                    item["status"] = "failed"
                    item["message"] = f"注册代理切换失败：{error}"
                    self._state["failed"] += 1
                    self._state["completed"] += 1
                    self._record_terminal_failure("proxy_assignment")
                    self._refresh_reliability()
                    self._append_log(item["message"], email=email)
                    return
                if not proxy_url:
                    item["status"] = "failed"
                    item["message"] = "注册动态代理已启用但未配置"
                    self._state["failed"] += 1
                    self._state["completed"] += 1
                    self._record_terminal_failure("proxy_unavailable")
                    self._refresh_reliability()
                    self._append_log(item["message"], email=email)
                    return
                item["proxyCountry"] = str(proxy_state.get("country") or "")
                item["proxyEndpoint"] = str(proxy_state.get("endpoint") or "")
                if proxy_state.get("mode") == "clash":
                    item["proxyNode"] = str(proxy_state.get("currentNode") or "")
                    item["proxyLatencyMs"] = int(
                        proxy_state.get("lastLatencyMs") or 0
                    )
                    self._append_log(
                        "已固定 Clash 日本出口："
                        f"{proxy_state.get('currentNode') or '日本节点'}，"
                        f"延迟 {proxy_state.get('lastLatencyMs') or 0} ms；"
                        "本账号注册、2FA 与 Session 获取结束前不再切换",
                        email=email,
                    )
                else:
                    self._append_log(
                        "已分配新的粘性代理会话："
                        f"{proxy_state.get('countryLabel') or proxy_state.get('country')}；"
                        "注册、2FA 与 Session 获取全程保持同一出口",
                        email=email,
                    )

            env = os.environ.copy()
            env.update(
                {
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "HME_BROWSER_SERVICE_URL": self.service_url,
                    "HME_BROWSER_WORKER_TOKEN": self.worker_token,
                    "HME_BROWSER_DB_FILE": str(self.db_file),
                    "HME_BROWSER_DIAGNOSTICS_DIR": str(
                        self.db_file.parent / "output" / "browser-diagnostics"
                    ),
                    "HME_OPENAI_PASSWORD": str(item.get("_password") or ""),
                    "HME_ENSURE_OPENAI_PASSWORD": "1"
                    if item.get("_ensure_password")
                    else "0",
                    "HME_FORCE_RESET_OPENAI_PASSWORD": "1"
                    if item.get("_force_reset_password")
                    else "0",
                    "HME_OPENAI_PASSWORD_CONFIRMED": "1"
                    if item.get("_password_confirmed")
                    else "0",
                    "HME_ENABLE_OPENAI_2FA": "1"
                    if item.get("_enable_2fa")
                    else "0",
                    "HME_COOKIE_SESSION_REFRESH": "1"
                    if item.get("_cookie_refresh_only")
                    else "0",
                    "HME_MANUAL_OTP_ENTRY": "1"
                    if item.get("_manual_otp_entry")
                    else "0",
                    "HME_PASSWORD_FIRST_REQUIRED": "1"
                    if item.get("_password_first_required")
                    else "0",
                    "HME_BROWSER_FOREGROUND_REQUIRED": "1"
                    if item.get("_foreground_required")
                    else "",
                    "HME_OPENAI_2FA_STATE": json.dumps(
                        item.get("_two_factor") or {}, ensure_ascii=False
                    ),
                    "HME_REGISTRATION_PROXY_URL": proxy_url,
                    "HME_REGISTRATION_PROXY_COUNTRY": str(
                        proxy_state.get("country") or ""
                    ),
                    "HME_REGISTRATION_PROXY_REQUIRED": "1" if proxy_url else "0",
                    "HME_BROWSER_WINDOW_SLOT": str(
                        max(0, int(item.get("_window_slot") or 0))
                    ),
                    "HME_BROWSER_WINDOW_SLOTS": str(
                        max(1, int(item.get("_window_slots") or 1))
                    ),
                    "HME_BROWSER_ENGINE": str(
                        self._state.get("browserEngine") or "camoufox"
                    ),
                    "HME_ROXY_API_URL": str(
                        self._state.get("roxyApiUrl") or ""
                    ),
                    "HME_ROXY_WORKSPACE_ID": str(
                        self._state.get("roxyWorkspaceId") or ""
                    ),
                    "HME_ROXY_PROFILE_ID": str(
                        item.get("_roxy_profile_id") or ""
                    ),
                }
            )
            command = [
                str(self.python_executable),
                str(self.bridge_file),
                "--source-dir",
                str(self.target_project_dir),
                "--email",
                email,
            ]
            if headless:
                command.append("--headless")
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW

            return_code = -1
            fingerprint_attempt = 0
            worker_launches = 0
            while worker_launches < MAX_WORKER_LAUNCHES:
                if self._state.get("status") == "cancelling":
                    item["status"] = "cancelled"
                    item["message"] = "任务已停止"
                    return
                env["HME_BROWSER_FINGERPRINT_ATTEMPT"] = str(
                    fingerprint_attempt
                )
                env["HME_BROWSER_WORKER_ATTEMPT"] = str(worker_launches)
                item.pop("_fresh_fingerprint_required", None)
                item.pop("_fresh_fingerprint_reason", None)
                item.pop("_error", None)
                process: asyncio.subprocess.Process | None = None
                stdout_task: asyncio.Task | None = None
                stderr_task: asyncio.Task | None = None
                worker_launches += 1
                item["workerAttempts"] = worker_launches
                item["retryState"] = "running"
                try:
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        cwd=str(self.target_project_dir),
                        env=env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        creationflags=creationflags,
                        limit=4 * 1024 * 1024,
                    )
                    self._processes[email] = process
                    stdout_task = asyncio.create_task(
                        self._read_stdout(process.stdout, item)
                    )
                    stderr_task = asyncio.create_task(
                        self._read_stderr(process.stderr, item)
                    )
                    return_code = await process.wait()
                    await asyncio.gather(stdout_task, stderr_task)
                except asyncio.CancelledError:
                    item["status"] = "cancelled"
                    item["message"] = "任务已停止"
                    await self._reap_worker_process(
                        process,
                        stdout_task,
                        stderr_task,
                        terminate=True,
                    )
                    raise
                except Exception as error:
                    return_code = -1
                    item["_error"] = str(error)
                    await self._reap_worker_process(
                        process,
                        stdout_task,
                        stderr_task,
                        terminate=True,
                    )
                finally:
                    self._processes.pop(email, None)

                if self._state.get("status") == "cancelling":
                    item["status"] = "cancelled"
                    item["message"] = "任务已停止"
                    return

                fresh_fingerprint = item.pop(
                    "_fresh_fingerprint_required", False
                )
                if fresh_fingerprint:
                    reason = str(
                        item.pop("_fresh_fingerprint_reason", "") or ""
                    ).strip()
                    item.pop("_result", None)
                    if (
                        fingerprint_attempt >= MAX_GOOGLE_FINGERPRINT_RETRIES
                        or worker_launches >= MAX_WORKER_LAUNCHES
                    ):
                        item["_error"] = (
                            "第二个独立指纹仍被要求 Google 登录；"
                            "已放弃该 Gmail，不再继续注册"
                        )
                        item["terminalReasonCode"] = "google_login_required"
                        item["retryState"] = "exhausted"
                        self._append_log(
                            "第二次启动仍要求 Google 登录；当前浏览器已关闭，"
                            "已放弃该 Gmail，不再生成新指纹",
                            email=email,
                        )
                        break

                    fingerprint_attempt += 1
                    item["_fingerprint_retry_count"] = fingerprint_attempt
                    item["fingerprintRetries"] = fingerprint_attempt
                    item["message"] = "正在关闭当前浏览器并生成全新指纹"
                    self._append_log(
                        "检测到 Google 登录要求，本轮注册已判定失败；"
                        "当前浏览器已关闭，正在新建 Camoufox 进程、生成全新指纹并"
                        f"重新打开注册（{fingerprint_attempt}/"
                        f"{MAX_GOOGLE_FINGERPRINT_RETRIES}）"
                        + (f"：{reason}" if reason else ""),
                        email=email,
                    )
                    continue

                result = item.get("_result")
                if isinstance(result, dict) and str(
                    result.get("access_token") or ""
                ).strip():
                    break

                page_state = item.get("pageState")
                if not isinstance(page_state, dict):
                    page_state = {}
                registration_chain = item.get("registrationChain")
                if not isinstance(registration_chain, dict):
                    registration_chain = {}
                two_factor = item.get("_two_factor")
                two_factor_enrolled = bool(
                    isinstance(two_factor, dict)
                    and str(two_factor.get("status") or "").casefold()
                    in {"enrolled", "enabled"}
                )
                retry_context = RegistrationRetryContext(
                    error=str(item.get("_error") or ""),
                    stage=str(page_state.get("stage") or ""),
                    return_code=return_code,
                    retry_count=max(
                        0, int(item.get("_transient_retry_count") or 0)
                    ),
                    registration_chain=registration_chain,
                    page_state=page_state,
                    manual_otp_entry=bool(item.get("_manual_otp_entry")),
                    result_received=isinstance(result, dict),
                    two_factor_enrolled=two_factor_enrolled,
                )
                decision = self.registration_retry_policy.decide(retry_context)
                item["terminalReasonCode"] = decision.reason_code
                item["terminalRetryDecision"] = decision.explanation
                if not decision.retryable:
                    item["retryState"] = (
                        "exhausted"
                        if decision.reason_code == "retry_limit"
                        else "terminal"
                    )
                    break
                if worker_launches >= MAX_WORKER_LAUNCHES:
                    item["terminalReasonCode"] = "worker_attempt_limit"
                    item["terminalRetryDecision"] = (
                        "工作器总启动次数已达到稳定性上限"
                    )
                    item["retryState"] = "exhausted"
                    break

                transient_retry_count = max(
                    0, int(item.get("_transient_retry_count") or 0)
                ) + 1
                item["_transient_retry_count"] = transient_retry_count
                item["transientRetries"] = transient_retry_count
                item["lastRetryCode"] = decision.reason_code
                retry_stage = str(
                    registration_chain.get("currentCode")
                    or page_state.get("stage")
                    or "startup"
                )[:80]
                item["lastRetryStage"] = retry_stage
                item["retryDelayMs"] = round(
                    max(0.0, decision.delay_seconds) * 1000
                )
                item.setdefault("retryHistory", []).append(
                    {
                        "attempt": transient_retry_count,
                        "reasonCode": decision.reason_code,
                        "stage": retry_stage,
                        "delayMs": item["retryDelayMs"],
                        "proxyReused": not decision.rotate_proxy,
                    }
                )
                self._state["transientRetries"] += 1
                if transient_retry_count == 1:
                    self._state["retriedAccounts"] += 1
                item["retryState"] = "waiting"
                item["message"] = (
                    "检测到可安全恢复的瞬时故障，正在复用当前账号环境重试"
                )
                self._append_log(
                    "[稳定性] 瞬时故障分类为 "
                    f"{decision.reason_code}；验证码页前安全重试 "
                    f"{transient_retry_count}/{self.registration_retry_policy.max_retries}；"
                    "保持同一代理与浏览器槽位",
                    email=email,
                    event_type="transient_retry",
                )
                try:
                    await self._retry_sleep(decision.delay_seconds)
                except asyncio.CancelledError:
                    item["status"] = "cancelled"
                    item["message"] = "任务已停止"
                    raise
                if self._state.get("status") == "cancelling":
                    item["status"] = "cancelled"
                    item["message"] = "任务已停止"
                    return

            password = str(item.get("_password") or "")
            result = item.get("_result")
            password_confirmed = bool(item.get("_password_confirmed"))
            strict_password_credentials = bool(
                item.get("_ensure_password")
                and item.get("_password_first_required")
            )
            session_saved = bool(
                isinstance(result, dict)
                and str(result.get("access_token") or "").strip()
            )
            requested_two_factor = bool(item.get("_enable_2fa"))
            strict_two_factor_credentials = bool(
                strict_password_credentials
                or (
                    item.get("_ensure_password")
                    and item.get("_password_first_required")
                    and requested_two_factor
                )
            )
            if session_saved and strict_password_credentials:
                if not password_confirmed:
                    item["_error"] = "注册未确认密码；已拒绝保存免密码账号"
                    session_saved = False
            if (
                session_saved
                and strict_two_factor_credentials
                and not item.get("twoFactorEnabled")
            ):
                item["_error"] = "注册未确认 TOTP 2FA 已开启；已拒绝保存该账号"
                session_saved = False
            if session_saved:
                result_to_save = dict(result)
                if is_new_registration:
                    result_to_save["registration_environment"] = (
                        build_registration_environment(
                            email,
                            registration_mode=(
                                "roxy_background"
                                if self._state.get("browserEngine") == "roxy"
                                and headless
                                else "roxy_headed"
                                if self._state.get("browserEngine") == "roxy"
                                else "browser_headless"
                                if headless
                                else "browser_headed"
                            ),
                            proxy_url=proxy_url,
                            proxy_state=proxy_state,
                        )
                    )
                    registration_diagnostics = result_to_save.get(
                        "registration_diagnostics"
                    )
                    if not isinstance(registration_diagnostics, dict):
                        registration_diagnostics = {}
                    registration_diagnostics.update(
                        {
                            "fingerprint_retries": max(
                                0,
                                int(item.get("_fingerprint_retry_count") or 0),
                            ),
                            "transient_retries": max(
                                0,
                                int(item.get("_transient_retry_count") or 0),
                            ),
                            "worker_attempts": max(
                                1, int(item.get("workerAttempts") or 1)
                            ),
                            "recovered_after_retry": bool(
                                item.get("_transient_retry_count")
                            ),
                            "last_retry_code": str(
                                item.get("lastRetryCode") or ""
                            )[:80],
                        }
                    )
                    result_to_save["registration_diagnostics"] = (
                        registration_diagnostics
                    )
                if proxy_url:
                    result_to_save["registration_proxy_url"] = proxy_url
                    result_to_save["registration_proxy"] = {
                        "mode": str(proxy_state.get("mode") or ""),
                        "country": str(proxy_state.get("country") or ""),
                        "endpoint": str(proxy_state.get("endpoint") or ""),
                        "node": str(proxy_state.get("currentNode") or ""),
                        "saved_at": utc_now(),
                    }
                await asyncio.to_thread(
                    _save_account_record,
                    self.db_file,
                    email,
                    result=result_to_save,
                    password=password,
                    password_confirmed=(
                        password_confirmed
                        if item.get("_ensure_password")
                        else None
                    ),
                )
                if self.on_account_saved is not None:
                    try:
                        await self.on_account_saved(email)
                    except Exception as error:
                        self._append_log(
                            f"账号已保存，但同步到远程服务器失败：{str(error)[:300]}",
                            email=email,
                        )
                item["status"] = "success"
                item["phase"] = "completed"
                self._state["lastFailureCode"] = ""
                self._state["failureStreak"] = 0
                if item.get("_transient_retry_count"):
                    item["retryState"] = "recovered"
                    self._state["recovered"] += 1
                else:
                    item["retryState"] = "completed"
                saved_parts = ["Session / AT / Cookie 已保存"]
                if item.get("_ensure_password") and not password_confirmed:
                    saved_parts.append("OpenAI 免密码注册")
                if item.get("twoFactorEnabled"):
                    saved_parts.append("2FA 已开启")
                elif item.get("_enable_2fa") and not password_confirmed:
                    saved_parts.append("2FA 已跳过（免密码账号）")
                elif item.get("_enable_2fa"):
                    saved_parts.append("2FA 待开启")
                item["message"] = "；".join(saved_parts)
                self._state["succeeded"] += 1
                self._append_log(item["message"], email=email)
                error = str(item.get("_error") or "")
                if return_code != 0 and error:
                    self._append_log(
                        f"后续账号设置未完成：{error[:500]}", email=email
                    )
            else:
                if password and not item.get("_ensure_password"):
                    await asyncio.to_thread(
                        _save_account_record,
                        self.db_file,
                        email,
                        password=password,
                        two_factor=item.get("_two_factor"),
                    )
                error = str(item.get("_error") or "")
                if (
                    not error
                    and return_code == 0
                    and item.get("_ensure_password")
                    and not password_confirmed
                ):
                    error = "OpenAI 端未确认密码设置，未保存本地密码"
                if not error:
                    error = f"浏览器工作器退出，代码 {return_code}"
                item["status"] = "failed"
                item["phase"] = "failed"
                item["message"] = error[:500]
                self._state["failed"] += 1
                failure_code = str(
                    item.get("lastRetryCode")
                    or item.get("terminalReasonCode")
                    or "not_retryable"
                )
                self._record_terminal_failure(failure_code)
                self._append_log(f"失败：{error[:500]}", email=email)
            self._state["completed"] += 1
            self._refresh_reliability()

    async def _read_stdout(
        self, stream: asyncio.StreamReader | None, item: dict[str, Any]
    ) -> None:
        if stream is None:
            return
        email = str(item["email"])
        async for raw_line in stream:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if not line.startswith(EVENT_PREFIX):
                self._append_log(
                    line[:500],
                    email=email,
                    source="browser_worker_stdout",
                    event_type="stdout",
                )
                continue
            try:
                event = json.loads(line[len(EVENT_PREFIX) :])
            except (json.JSONDecodeError, TypeError, ValueError):
                self._append_log(
                    "浏览器工作器返回了无效事件",
                    email=email,
                    source="browser_worker_stdout",
                    event_type="invalid_event",
                )
                continue
            kind = str(event.get("type") or "")
            if kind == "log":
                self._append_log(
                    str(event.get("message") or ""),
                    email=email,
                    source="browser_worker",
                    event_type="worker_log",
                )
            elif kind == "result":
                result = event.get("result")
                if isinstance(result, dict):
                    item["_result"] = result
                password = str(event.get("password") or "")
                if password:
                    item["_password"] = password
                if event.get("password_confirmed") is not None:
                    item["_password_confirmed"] = bool(
                        event.get("password_confirmed")
                    )
                    item["passwordConfirmed"] = item["_password_confirmed"]
                two_factor = (
                    result.get("two_factor") if isinstance(result, dict) else None
                )
                if isinstance(two_factor, dict):
                    item["_two_factor"] = two_factor
                    item["twoFactorEnabled"] = bool(two_factor.get("enabled"))
            elif kind == "account_registered":
                result = event.get("result")
                if isinstance(result, dict):
                    item["_result"] = result
                password = str(event.get("password") or "")
                if password:
                    item["_password"] = password
                password_confirmed = bool(event.get("password_confirmed"))
                item["_password_confirmed"] = password_confirmed
                item["passwordConfirmed"] = password_confirmed
                two_factor = (
                    result.get("two_factor") if isinstance(result, dict) else None
                )
                two_factor_enabled = bool(
                    isinstance(two_factor, dict) and two_factor.get("enabled")
                )
                strict_password_credentials = bool(
                    item.get("_ensure_password")
                    and item.get("_password_first_required")
                )
                if isinstance(result, dict) and (
                    not strict_password_credentials
                    or (password_confirmed and two_factor_enabled)
                ):
                    await asyncio.to_thread(
                        _save_account_record,
                        self.db_file,
                        email,
                        result=result,
                        password=password,
                        password_confirmed=(
                            password_confirmed
                            if item.get("_ensure_password")
                            else None
                        ),
                    )
                item["message"] = (
                    "密码已确认，正在开启 2FA"
                    if strict_password_credentials and password_confirmed
                    else
                    "OpenAI 注册成功，正在开启 2FA"
                    if item.get("_enable_2fa") and password_confirmed
                    else "OpenAI 免密码注册成功，已跳过依赖密码的 2FA"
                    if item.get("_enable_2fa")
                    else "OpenAI 注册成功，Session 已保存"
                )
            elif kind == "two_factor_start":
                item["phase"] = "enabling_2fa"
                item["message"] = "正在创建 TOTP 2FA"
                self._append_log(
                    "OpenAI 注册成功，开始开启 2FA",
                    email=email,
                    source="browser_worker",
                    event_type="two_factor_start",
                )
            elif kind == "two_factor_enrolled":
                two_factor = event.get("two_factor")
                if isinstance(two_factor, dict):
                    item["_two_factor"] = two_factor
                    if not (
                        email.endswith("@gmail.com")
                        and item.get("_ensure_password")
                    ):
                        await asyncio.to_thread(
                            _save_account_record,
                            self.db_file,
                            email,
                            password=str(item.get("_password") or ""),
                            two_factor=two_factor,
                        )
                item["message"] = "2FA 密钥已保存，正在激活"
            elif kind == "two_factor_enabled":
                item["twoFactorEnabled"] = True
                item["message"] = "2FA 已开启，正在保存账号"
                self._append_log(
                    "TOTP 2FA 已成功开启",
                    email=email,
                    source="browser_worker",
                    event_type="two_factor_enabled",
                )
            elif kind == "registration_chain":
                chain_state = event.get("state")
                if isinstance(chain_state, dict):
                    completed_steps = chain_state.get("completedSteps")
                    step_ledger = chain_state.get("steps")
                    request_activity = chain_state.get("requestActivity")
                    if not isinstance(request_activity, dict):
                        request_activity = {}
                    chain = {
                        "status": str(chain_state.get("status") or "running")[:40],
                        "currentStep": str(
                            chain_state.get("currentStep") or "准备注册请求"
                        )[:180],
                        "currentCode": str(
                            chain_state.get("currentCode") or ""
                        )[:80],
                        "currentValue": str(
                            chain_state.get("currentValue") or ""
                        )[:240],
                        "currentCompleted": bool(
                            chain_state.get("currentCompleted", False)
                        ),
                        "nextCode": str(chain_state.get("nextCode") or "")[:80],
                        "canAdvance": bool(chain_state.get("canAdvance", False)),
                        "steps": [
                            {
                                "index": max(0, int(step.get("index") or 0)),
                                "code": str(step.get("code") or "")[:80],
                                "label": str(step.get("label") or "")[:180],
                                "status": str(step.get("status") or "pending")[:20],
                                "value": str(step.get("value") or "")[:240],
                                "completed": bool(step.get("completed", False)),
                                "updatedAt": str(step.get("updatedAt") or "")[:80],
                            }
                            for step in step_ledger
                            if isinstance(step, dict)
                        ][:24]
                        if isinstance(step_ledger, list)
                        else [],
                        "completedSteps": [
                            str(step)[:180]
                            for step in completed_steps
                            if str(step).strip()
                        ][:24]
                        if isinstance(completed_steps, list)
                        else [],
                        "nextAction": str(
                            chain_state.get("nextAction") or "继续监测完整注册链路"
                        )[:300],
                        "registrationCreated": bool(
                            chain_state.get("registrationCreated", False)
                        ),
                        "sessionReady": bool(chain_state.get("sessionReady", False)),
                        "passwordConfirmed": bool(
                            chain_state.get("passwordConfirmed", False)
                        ),
                        "twoFactorEnabled": bool(
                            chain_state.get("twoFactorEnabled", False)
                        ),
                        "fullRegistrationComplete": bool(
                            chain_state.get("fullRegistrationComplete", False)
                        ),
                        "requestActivity": {
                            "requestCount": max(
                                0, int(request_activity.get("requestCount") or 0)
                            ),
                            "responseCount": max(
                                0, int(request_activity.get("responseCount") or 0)
                            ),
                            "failedCount": max(
                                0, int(request_activity.get("failedCount") or 0)
                            ),
                            "loadCount": max(
                                0, int(request_activity.get("loadCount") or 0)
                            ),
                            "lastMethod": str(
                                request_activity.get("lastMethod") or ""
                            )[:12],
                            "lastRoute": str(
                                request_activity.get("lastRoute") or ""
                            )[:220],
                            "lastStatus": max(
                                0, int(request_activity.get("lastStatus") or 0)
                            ),
                        },
                        "startedAt": str(chain_state.get("startedAt") or "")[:80],
                        "updatedAt": utc_now(),
                    }
                    item["registrationChain"] = chain
                    item["message"] = chain["nextAction"]
                    self._state["registrationChain"] = {**chain, "email": email}
            elif kind == "page_state":
                page_state = event.get("state")
                if isinstance(page_state, dict):
                    completed_steps = page_state.get("completedSteps")
                    state = {
                        "code": str(page_state.get("code") or "unknown")[:80],
                        "currentPage": str(
                            page_state.get("currentPage") or "未识别页面"
                        )[:160],
                        "stage": str(page_state.get("stage") or "running")[:80],
                        "completedSteps": [
                            str(step)[:160]
                            for step in completed_steps
                            if str(step).strip()
                        ][:12]
                        if isinstance(completed_steps, list)
                        else [],
                        "nextAction": str(
                            page_state.get("nextAction") or "继续监测页面变化"
                        )[:300],
                        "actionMode": str(
                            page_state.get("actionMode") or "automatic"
                        )[:40],
                        "confidence": max(
                            0, min(100, int(page_state.get("confidence") or 0))
                        ),
                        "source": str(page_state.get("source") or "dom")[:40],
                        "route": str(page_state.get("route") or "")[:240],
                        "readyState": str(
                            page_state.get("readyState") or "unknown"
                        )[:40],
                        "stalled": bool(page_state.get("stalled", False)),
                        "stalledSeconds": max(
                            0, int(page_state.get("stalledSeconds") or 0)
                        ),
                        "diagnosticScreenshot": str(
                            page_state.get("diagnosticScreenshot") or ""
                        )[:500],
                        "updatedAt": utc_now(),
                    }
                    item["pageState"] = state
                    item["phase"] = state["stage"]
                    item["message"] = state["nextAction"]
                    self._state["pageState"] = {**state, "email": email}
            elif kind == "error":
                item["_error"] = str(event.get("error") or "浏览器任务失败")
                self._append_log(
                    f"工作器错误：{item['_error'][:500]}",
                    email=email,
                    source="browser_worker",
                    event_type="worker_error",
                )
                password = str(event.get("password") or "")
                if password:
                    item["_password"] = password
                if event.get("password_confirmed") is not None:
                    item["_password_confirmed"] = bool(
                        event.get("password_confirmed")
                    )
                    item["passwordConfirmed"] = item["_password_confirmed"]
            elif kind == "fresh_fingerprint_required":
                item["_fresh_fingerprint_required"] = True
                item["_fresh_fingerprint_reason"] = str(
                    event.get("reason") or "OpenAI 注册要求 Google 登录"
                )[:500]
                password = str(event.get("password") or "")
                if password:
                    item["_password"] = password
                if event.get("password_confirmed") is not None:
                    item["_password_confirmed"] = bool(
                        event.get("password_confirmed")
                    )
                    item["passwordConfirmed"] = item[
                        "_password_confirmed"
                    ]

    async def _read_stderr(
        self, stream: asyncio.StreamReader | None, item: dict[str, Any]
    ) -> None:
        if stream is None:
            return
        email = str(item["email"])
        lines: list[str] = []
        async for raw_line in stream:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line:
                lines.append(line)
                if len(lines) > 20:
                    del lines[:-20]
        if lines and not item.get("_error"):
            item["_error"] = "；".join(lines)[-1500:]
            self._append_log(
                lines[-1][:500],
                email=email,
                source="browser_worker_stderr",
                event_type="stderr",
            )

    async def stop(self) -> dict[str, Any]:
        if not self._batch_task or self._batch_task.done():
            return self.snapshot()
        self._state["status"] = "cancelling"
        self._append_log("正在停止浏览器获取任务…")
        for process in list(self._processes.values()):
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        try:
            await asyncio.wait_for(self._batch_task, timeout=8)
        except asyncio.TimeoutError:
            for process in list(self._processes.values()):
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            self._batch_task.cancel()
            try:
                await self._batch_task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass
        return self.snapshot()

    async def wait(self) -> dict[str, Any]:
        task = self._batch_task
        if task and not task.done():
            await asyncio.shield(task)
        return self.snapshot()

    async def close(self) -> None:
        await self.stop()


__all__ = [
    "BrowserTaskManager",
    "account_registration_proxy_url",
    "access_token_is_expired",
    "decode_jwt_payload",
    "jwt_account_type",
    "load_account_record",
    "build_registration_environment",
    "registration_email_type",
    "set_manual_account_type",
]
