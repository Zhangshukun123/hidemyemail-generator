"""Post-payment Plus phone binding and Codex OAuth orchestration.

The module is intentionally split into Model, View, and Presenter roles.  A
subprocess runner isolates the bundled protocol implementation and the
presenter makes repeated payment-status polls idempotent per account.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .browser_tasks import (
    account_registration_proxy_url,
    load_account_record,
)
from .inbox import connect_db
from .payment_sms import GlobalSmsRoutingConfigStore
from .plus_sms import (
    PLUS_CODEX_SMS_MAX_PRICE_USD,
    PLUS_CODEX_SMS_PROVIDER,
    mask_phone,
)
from .protocol_registration import PROTOCOL_CODE_PREFIX
from .roxy_registration import RoxyRegistrationStore


PLUS_CODEX_EVENT_PREFIX = "HME_PLUS_CODEX_EVENT:"
PLUS_CODEX_RESULT_PREFIX = "HME_PLUS_CODEX_RESULT:"
PLUS_CODEX_TERMINAL_STATUSES = {"completed", "failed"}
PLUS_CODEX_MAX_ATTEMPTS = 1
PLUS_CODEX_MAX_LOGS = 200
DIRECT_PLUS_PHONE_JOB_PREFIX = "plus-account-"

Runner = Callable[
    [dict[str, Any], Callable[[dict[str, Any]], None]],
    Awaitable[dict[str, Any]],
]
AccountSaved = Callable[[str], Awaitable[Any]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def direct_plus_phone_job_id(email: str) -> str:
    """Return a stable internal job id for Plus accounts without payment history."""

    normalized = str(email or "").strip().lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"{DIRECT_PLUS_PHONE_JOB_PREFIX}{digest}"


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            payload = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(payload) if isinstance(payload, dict) else {}
    return {}


def _public_error(value: Any, *secrets_to_remove: str) -> str:
    text = str(value or "Plus 协议接码失败")
    if "rate_limit_exceeded" in text.lower():
        return "OpenAI Codex OAuth 请求频率受限，请稍后手动重试"
    for secret in secrets_to_remove:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(https?://)([^/@\s]+)@", r"\1[REDACTED]@", text)
    text = re.sub(
        r"(?i)((?:access|refresh|id)[_-]?token|api[_-]?key|otp)\s*[=:]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    return text[:800]


def _normalized_logs(value: dict[str, Any] | None) -> list[dict[str, Any]]:
    state = value if isinstance(value, dict) else {}
    raw_logs = state.get("logs")
    if not isinstance(raw_logs, list):
        return []
    logs: list[dict[str, Any]] = []
    fallback_sequence = 0
    for raw in raw_logs[-PLUS_CODEX_MAX_LOGS:]:
        if not isinstance(raw, dict):
            continue
        try:
            sequence = int(raw.get("sequence") or 0)
        except (TypeError, ValueError):
            sequence = 0
        fallback_sequence = max(fallback_sequence + 1, sequence)
        level = str(raw.get("level") or "info").strip().lower()
        if level not in {"info", "success", "warning", "error"}:
            level = "info"
        logs.append(
            {
                "sequence": fallback_sequence,
                "at": str(raw.get("at") or ""),
                "stage": str(raw.get("stage") or "protocol")[:80],
                "level": level,
                "message": _public_error(raw.get("message") or "")[:800],
            }
        )
    return logs


def _append_log(
    state: dict[str, Any],
    *,
    stage: str,
    message: Any,
    level: str = "info",
    at: str = "",
) -> None:
    logs = _normalized_logs(state)
    safe_stage = str(stage or "protocol")[:80]
    safe_message = _public_error(message)
    safe_level = str(level or "info").strip().lower()
    if safe_level not in {"info", "success", "warning", "error"}:
        safe_level = "info"
    if logs and (
        logs[-1]["stage"] == safe_stage
        and logs[-1]["message"] == safe_message
        and logs[-1]["level"] == safe_level
    ):
        state["logs"] = logs
        state["log_sequence"] = logs[-1]["sequence"]
        return
    sequence = max(
        int(state.get("log_sequence") or 0),
        logs[-1]["sequence"] if logs else 0,
    ) + 1
    logs.append(
        {
            "sequence": sequence,
            "at": str(at or utc_now()),
            "stage": safe_stage,
            "level": safe_level,
            "message": safe_message,
        }
    )
    state["logs"] = logs[-PLUS_CODEX_MAX_LOGS:]
    state["log_sequence"] = sequence


class PlusCodexView:
    """View: expose progress without OAuth tokens, raw phones, or activation IDs."""

    @staticmethod
    def present(value: dict[str, Any] | None) -> dict[str, Any]:
        state = value if isinstance(value, dict) else {}
        logs = _normalized_logs(state)
        return {
            "job_id": str(state.get("job_id") or ""),
            "email": str(state.get("email") or ""),
            "status": str(state.get("status") or "pending"),
            "stage": str(state.get("stage") or "pending"),
            "detail": str(state.get("detail") or "等待 Plus 协议接码")[:800],
            "provider": str(state.get("provider") or ""),
            "service": "openai",
            "max_price": float(
                state.get("max_price") or PLUS_CODEX_SMS_MAX_PRICE_USD
            ),
            "sms_verified": bool(state.get("sms_verified")),
            "phone_masked": str(state.get("phone_masked") or ""),
            "export_ready": bool(state.get("export_ready")),
            "attempt": max(0, int(state.get("attempt") or 0)),
            "started_at": str(state.get("started_at") or ""),
            "updated_at": str(state.get("updated_at") or ""),
            "completed_at": str(state.get("completed_at") or ""),
            "log_sequence": max(
                int(state.get("log_sequence") or 0),
                logs[-1]["sequence"] if logs else 0,
            ),
            "logs": logs,
        }

    @classmethod
    def merge_confirmation(
        cls, confirmation: dict[str, Any], delivery: dict[str, Any]
    ) -> dict[str, Any]:
        """Project delivery progress into the existing payment monitor contract."""

        result = dict(confirmation)
        public = cls.present(delivery)
        result["plus_codex"] = public
        result["export_ready"] = public["export_ready"]
        payment_detail = str(confirmation.get("detail") or "新 AT 已确认 Plus")
        status = public["status"]
        if status == "completed":
            result["status"] = "plus"
            result["detail"] = f"{payment_detail}；Plus 协议接码及 Codex OAuth 已完成"
        elif status == "failed":
            result["status"] = "plus_sms_failed"
            result["detail"] = f"{payment_detail}；{public['detail']}"
        else:
            result["status"] = "plus_sms"
            result["detail"] = public["detail"]
        return result


class PlusCodexModel:
    """Model: atomically persist one post-payment delivery per Plus account."""

    def __init__(self, db_file: Path) -> None:
        self.db_file = Path(db_file)

    def _record(self, connection: Any, email: str) -> tuple[Any, dict[str, Any]]:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (f"gpt_account:{email}",)
        ).fetchone()
        record = _json_object(
            row["value"] if row and hasattr(row, "keys") else row[0] if row else ""
        )
        return row, record

    @staticmethod
    def _save(connection: Any, email: str, record: dict[str, Any]) -> None:
        connection.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (f"gpt_account:{email}", json.dumps(record, ensure_ascii=False)),
        )

    def current(self, email: str, job_id: str = "") -> dict[str, Any] | None:
        record = load_account_record(self.db_file, email)
        latest = record.get("plus_codex")
        if isinstance(latest, dict) and (
            not job_id
            or str(latest.get("job_id") or "") == job_id
            or str(latest.get("status") or "") == "completed"
        ):
            return PlusCodexView.present(latest)
        jobs = record.get("plus_codex_jobs")
        state = jobs.get(job_id) if isinstance(jobs, dict) else None
        return PlusCodexView.present(state) if isinstance(state, dict) else None

    def context(self, email: str, job_id: str) -> dict[str, Any]:
        record = load_account_record(self.db_file, email)
        if not record:
            raise RuntimeError("Plus 协议接码账号记录不存在")
        if str(record.get("account_type") or "").lower() != "plus":
            raise RuntimeError("账号尚未确认 Plus，禁止启动 Plus 接码")
        confirmation = record.get("payment_confirmation")
        confirmations = record.get("payment_confirmations")
        if isinstance(confirmations, dict) and isinstance(
            confirmations.get(job_id), dict
        ):
            confirmation = confirmations[job_id]
        direct_plus_binding = job_id == direct_plus_phone_job_id(email)
        if not direct_plus_binding and not (
            isinstance(confirmation, dict)
            and str(confirmation.get("job_id") or "") == job_id
            and str(confirmation.get("status") or "") == "plus"
            and confirmation.get("payment_succeeded") is True
        ):
            raise RuntimeError("该支付任务尚未确认 Plus，禁止启动 Plus 接码")
        diagnostics = record.get("registration_diagnostics")
        password = (
            str(record.get("password") or "")
            if record.get("password_confirmed") is not False
            else ""
        )
        two_factor = record.get("two_factor")
        two_factor = two_factor if isinstance(two_factor, dict) else {}
        return {
            "email": email,
            "password": password,
            # The Roxy phase always starts clean and logs in with this email.
            # Old Session Tokens and Cookies must never enter the worker payload.
            "initial_session_token": "",
            "initial_session_cookies": [],
            "cookie_login_only": True,
            "totp_secret": str(two_factor.get("secret") or ""),
            "proxy_url": account_registration_proxy_url(record),
            "impersonate": (
                str(diagnostics.get("impersonate") or "")
                if isinstance(diagnostics, dict)
                else ""
            ),
        }

    def claim(
        self,
        *,
        email: str,
        job_id: str,
        provider: str,
        max_price: float = PLUS_CODEX_SMS_MAX_PRICE_USD,
    ) -> dict[str, Any]:
        connection = connect_db(str(self.db_file))
        try:
            connection.execute("BEGIN IMMEDIATE")
            row, record = self._record(connection, email)
            if row is None:
                raise RuntimeError("Plus 协议接码账号记录不存在")
            latest = record.get("plus_codex")
            if (
                isinstance(latest, dict)
                and str(latest.get("status") or "") == "completed"
            ):
                connection.rollback()
                return PlusCodexView.present(latest)
            jobs = record.get("plus_codex_jobs")
            jobs = dict(jobs) if isinstance(jobs, dict) else {}
            previous = jobs.get(job_id)
            previous = previous if isinstance(previous, dict) else {}
            claimed_attempts = [
                int(state.get("attempt") or 0)
                for state in jobs.values()
                if isinstance(state, dict)
            ]
            if isinstance(latest, dict):
                claimed_attempts.append(int(latest.get("attempt") or 0))
            timestamp = utc_now()
            state = {
                "job_id": job_id,
                "email": email,
                "status": "running",
                "stage": "roxy_login",
                "detail": "Plus 已确认，正在打开 Roxy 并使用账号邮箱登录",
                "provider": provider,
                "service": "openai",
                "max_price": float(max_price),
                "sms_verified": False,
                "phone_masked": "",
                "export_ready": False,
                "attempt": max(claimed_attempts, default=0) + 1,
                "started_at": str(previous.get("started_at") or timestamp),
                "updated_at": timestamp,
                "completed_at": "",
                "logs": _normalized_logs(previous),
                "log_sequence": int(previous.get("log_sequence") or 0),
            }
            _append_log(
                state,
                stage="roxy_login",
                message="手机号绑定任务已启动，正在准备 Roxy 邮箱登录",
                at=timestamp,
            )
            jobs[job_id] = state
            record.update(
                plus_codex=state,
                plus_codex_jobs=dict(list(jobs.items())[-20:]),
                updated_at=timestamp,
            )
            self._save(connection, email, record)
            connection.commit()
            return PlusCodexView.present(state)
        finally:
            connection.close()

    def progress(
        self,
        *,
        email: str,
        job_id: str,
        stage: str,
        detail: str,
        level: str = "info",
    ) -> dict[str, Any]:
        connection = connect_db(str(self.db_file))
        try:
            connection.execute("BEGIN IMMEDIATE")
            row, record = self._record(connection, email)
            if row is None:
                connection.rollback()
                return PlusCodexView.present(None)
            jobs = record.get("plus_codex_jobs")
            jobs = dict(jobs) if isinstance(jobs, dict) else {}
            state = jobs.get(job_id)
            state = dict(state) if isinstance(state, dict) else {}
            if str(state.get("status") or "") in PLUS_CODEX_TERMINAL_STATUSES:
                connection.rollback()
                return PlusCodexView.present(state)
            timestamp = utc_now()
            safe_detail = _public_error(detail)
            state.update(
                status="running",
                stage=str(stage or "protocol")[:80],
                detail=safe_detail,
                updated_at=timestamp,
            )
            _append_log(
                state,
                stage=state["stage"],
                message=safe_detail,
                level=level,
                at=timestamp,
            )
            jobs[job_id] = state
            record.update(
                plus_codex=state, plus_codex_jobs=jobs, updated_at=state["updated_at"]
            )
            self._save(connection, email, record)
            connection.commit()
            return PlusCodexView.present(state)
        finally:
            connection.close()

    def complete(
        self, *, email: str, job_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        oauth = {
            "access_token": str(result.get("access_token") or "").strip(),
            "refresh_token": str(result.get("refresh_token") or "").strip(),
            "id_token": str(result.get("id_token") or "").strip(),
            "account_id": str(result.get("account_id") or "").strip(),
        }
        if not all(oauth.values()):
            raise RuntimeError("Codex OAuth 未返回完整的 AT/RT/ID Token/账号 ID")
        if result.get("phone_bound") is not True:
            raise RuntimeError("Codex OAuth 未完成本账号的 Plus 协议接码")
        phone = str(result.get("phone") or "")
        activation_id = str(result.get("activation_id") or "")
        provider = str(result.get("sms_provider") or "")
        sms_max_price = float(
            result.get("sms_max_price") or PLUS_CODEX_SMS_MAX_PRICE_USD
        )
        expires_in = max(0, int(result.get("expires_in") or 0))
        timestamp = utc_now()
        expires_at = (
            (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
            if expires_in
            else ""
        )
        receipt = {
            "status": "completed",
            "provider": provider,
            "service": "openai",
            "service_code": "dr",
            "country": str(result.get("sms_country") or ""),
            "max_price": sms_max_price,
            "phone_masked": mask_phone(phone),
            "activation_fingerprint": hashlib.sha256(
                activation_id.encode("utf-8")
            ).hexdigest()[:16]
            if activation_id
            else "",
            "phone_attempts": max(1, int(result.get("phone_attempts") or 1)),
            "phone_bound": True,
            "source_payment_job_id": job_id,
            "completed_at": timestamp,
        }
        connection = connect_db(str(self.db_file))
        try:
            connection.execute("BEGIN IMMEDIATE")
            row, record = self._record(connection, email)
            if row is None:
                raise RuntimeError("Plus 协议接码账号记录不存在")
            jobs = record.get("plus_codex_jobs")
            jobs = dict(jobs) if isinstance(jobs, dict) else {}
            state = jobs.get(job_id)
            state = dict(state) if isinstance(state, dict) else {}
            state.update(
                job_id=job_id,
                email=email,
                status="completed",
                stage="completed",
                detail="Plus 协议接码及 Codex OAuth 已完成，可导出 CPA/Sub2API",
                provider=provider,
                service="openai",
                max_price=sms_max_price,
                sms_verified=True,
                phone_masked=receipt["phone_masked"],
                export_ready=True,
                updated_at=timestamp,
                completed_at=timestamp,
            )
            _append_log(
                state,
                stage="completed",
                message=state["detail"],
                level="success",
                at=timestamp,
            )
            jobs[job_id] = state
            receipts = record.get("plus_sms_receipts")
            receipts = dict(receipts) if isinstance(receipts, dict) else {}
            receipts[job_id] = receipt
            record.update(
                {
                    "plus_codex": state,
                    "plus_codex_jobs": dict(list(jobs.items())[-20:]),
                    "plus_sms": receipt,
                    "plus_sms_receipts": dict(list(receipts.items())[-20:]),
                    "codex_oauth": {
                        **oauth,
                        "status": "completed",
                        "email": email,
                        "expires_in": expires_in,
                        "expires_at": expires_at,
                        "last_refresh": timestamp,
                        "source": "plus_protocol_add_phone",
                    },
                    "updated_at": timestamp,
                }
            )
            self._save(connection, email, record)
            connection.commit()
            return PlusCodexView.present(state)
        finally:
            connection.close()

    def fail(
        self, *, email: str, job_id: str, error: Any, provider: str = ""
    ) -> dict[str, Any]:
        connection = connect_db(str(self.db_file))
        try:
            connection.execute("BEGIN IMMEDIATE")
            row, record = self._record(connection, email)
            if row is None:
                connection.rollback()
                return PlusCodexView.present(None)
            jobs = record.get("plus_codex_jobs")
            jobs = dict(jobs) if isinstance(jobs, dict) else {}
            state = jobs.get(job_id)
            state = dict(state) if isinstance(state, dict) else {}
            timestamp = utc_now()
            state.update(
                job_id=job_id,
                email=email,
                status="failed",
                stage="failed",
                detail="Plus 协议接码失败：" + _public_error(error),
                provider=provider or str(state.get("provider") or ""),
                max_price=float(
                    state.get("max_price") or PLUS_CODEX_SMS_MAX_PRICE_USD
                ),
                sms_verified=False,
                export_ready=False,
                updated_at=timestamp,
                completed_at=timestamp,
            )
            _append_log(
                state,
                stage="failed",
                message=state["detail"],
                level="error",
                at=timestamp,
            )
            jobs[job_id] = state
            record.update(plus_codex=state, plus_codex_jobs=jobs, updated_at=timestamp)
            self._save(connection, email, record)
            connection.commit()
            return PlusCodexView.present(state)
        finally:
            connection.close()


class ProtocolPlusCodexRunner:
    """Run Roxy email login plus protocol SMS in an isolated subprocess."""

    def __init__(
        self,
        *,
        db_file: Path,
        roxy_registration_store: RoxyRegistrationStore,
        source_root: Path | None = None,
        gptfree_root: Path | None = None,
        python_executable: Path | None = None,
    ) -> None:
        package_root = Path(__file__).resolve().parent
        self.db_file = Path(db_file).resolve()
        self.roxy_registration_store = roxy_registration_store
        self.source_root = Path(source_root or package_root.parent).resolve()
        self.gptfree_root = Path(
            gptfree_root or package_root / "vendor" / "gptfree_register"
        ).resolve()
        self.python_executable = Path(python_executable or sys.executable).resolve()
        self.worker_script = package_root / "plus_codex_worker.py"
        self._processes: set[asyncio.subprocess.Process] = set()

    def _roxy_payload(self) -> dict[str, str]:
        config = self.roxy_registration_store.runtime_config(1)
        profile_ids = config.get("profileIds") or [config.get("profileId") or ""]
        return {
            "api_url": str(config.get("apiUrl") or ""),
            "workspace_id": str(config.get("workspaceId") or ""),
            "profile_id": str(profile_ids[0]),
        }

    async def __call__(
        self,
        payload: dict[str, Any],
        on_event: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        request = {
            **payload,
            "db_file": str(self.db_file),
            "source_root": str(self.source_root),
            "gptfree_root": str(self.gptfree_root),
            "roxy": self._roxy_payload(),
        }
        environment = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
        }
        process = await asyncio.create_subprocess_exec(
            str(self.python_executable),
            str(self.worker_script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        self._processes.add(process)
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise RuntimeError("Plus 协议接码子进程管道创建失败")
        process.stdin.write(json.dumps(request, ensure_ascii=False).encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        with suppress(Exception):
            await process.stdin.wait_closed()
        stderr_task = asyncio.create_task(process.stderr.read())
        result: dict[str, Any] = {}
        try:
            async for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line.startswith(PLUS_CODEX_EVENT_PREFIX):
                    event = _json_object(line[len(PLUS_CODEX_EVENT_PREFIX) :])
                    if event:
                        on_event(event)
                elif line.startswith(PLUS_CODEX_RESULT_PREFIX):
                    result = _json_object(line[len(PLUS_CODEX_RESULT_PREFIX) :])
                elif line:
                    on_event({"stage": "protocol", "message": line})
            return_code = await process.wait()
            stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
            if return_code or result.get("ok") is not True:
                raise RuntimeError(
                    _public_error(
                        result.get("error") or stderr or f"子进程退出 {return_code}"
                    )
                )
            return result
        finally:
            self._processes.discard(process)
            if not stderr_task.done():
                stderr_task.cancel()

    async def close(self) -> None:
        for process in list(self._processes):
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
        if self._processes:
            await asyncio.gather(
                *(process.wait() for process in list(self._processes)),
                return_exceptions=True,
            )
        self._processes.clear()


class PlusCodexPresenter:
    """Presenter: start one background delivery and project its public state."""

    EVENT_STAGES = {
        "browser_started": ("roxy_login", "正在打开 Roxy 专用指纹环境"),
        "roxy_login_started": ("roxy_login", "Roxy 专用环境已打开，正在登录"),
        "email_login_succeeded": (
            "roxy_login",
            "Roxy 邮箱登录成功，已到达手机号挑战",
        ),
        "cookies_synced": (
            "cookie_sync",
            "Roxy 登录 Cookie 已同步到当前账号",
        ),
        "cookie_login_succeeded": (
            "cookie_login",
            "Roxy 登录 Cookie 校验成功",
        ),
        "oauth_browser_started": (
            "roxy_login",
            "正在 Roxy 中完成 Codex OAuth 身份认证",
        ),
        "browser_oauth_ready": (
            "roxy_login",
            "Roxy OAuth 登录完成，正在切换纯协议手机号接码",
        ),
        "cookie_login_started": ("cookie_login", "正在使用已保存的 Cookie 登录"),
        "cookie_login_fallback": (
            "email_otp",
            "ChatGPT Cookie 仍可用于浏览器登录；Codex OAuth 要求邮箱二次验证",
        ),
        "oauth_started": ("oauth_start", "正在启动 Codex OAuth"),
        "email_otp_sent": ("email_otp", "正在协议获取邮箱验证码"),
        "email_otp_validated": ("email_otp", "邮箱验证码已确认"),
        "phone_acquired": ("phone_acquired", "已取得 Plus 接码号码"),
        "sms_route": ("sms_route", "正在按全局接码国家顺序取号"),
        "sms_sent": ("waiting_sms", "OpenAI 已发送短信，正在等待验证码"),
        "otp_received": ("waiting_sms", "已收到短信验证码，正在协议提交"),
        "phone_bound": ("phone_bound", "Plus 接码验证完成"),
        "rt_ready": ("token_exchange", "正在完成 Codex OAuth Token 交换"),
    }

    def __init__(
        self,
        model: PlusCodexModel,
        *,
        runner: Runner,
        on_account_saved: AccountSaved | None = None,
    ) -> None:
        self.model = model
        self.runner = runner
        self.on_account_saved = on_account_saved
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._code_tokens: dict[str, dict[str, Any]] = {}

    def token_record(self, token: str) -> dict[str, str] | None:
        key = str(token or "")
        state = self._code_tokens.get(key)
        if not isinstance(state, dict):
            return None
        if float(state.get("expires_at") or 0) <= __import__("time").time():
            self._code_tokens.pop(key, None)
            return None
        return {
            "email": str(state.get("email") or ""),
            "since": str(state.get("since") or ""),
        }

    def valid_code_token(self, token: str) -> bool:
        return self.token_record(token) is not None

    def _issue_code_token(self, email: str) -> tuple[str, str]:
        token = secrets.token_urlsafe(24)
        since = utc_now()
        self._code_tokens[token] = {
            "email": email,
            "since": since,
            "expires_at": __import__("time").time() + 15 * 60,
        }
        return token, since

    async def ensure(
        self,
        *,
        job: dict[str, Any],
        confirmation: dict[str, Any],
        base_url: str,
        sms_provider: str,
        retry_failed: bool = False,
    ) -> dict[str, Any]:
        job_id = str(job.get("id") or "").strip()
        email = str(job.get("source_account_email") or "").strip().lower()
        if str(confirmation.get("status") or "") != "plus" or not job_id or not email:
            raise RuntimeError("只有已确认 Plus 的账号可以启动 Plus 接码")
        sms_policy = GlobalSmsRoutingConfigStore(self.model.db_file).purpose("binding")
        sms_provider = str(sms_policy["provider"] or PLUS_CODEX_SMS_PROVIDER)
        sms_max_price = float(
            sms_policy["maxPrice"] or PLUS_CODEX_SMS_MAX_PRICE_USD
        )
        current = await asyncio.to_thread(self.model.current, email, job_id)
        if current and (
            current["status"] == "completed"
            or (current["status"] == "failed" and not retry_failed)
        ):
            return current
        lock = self._locks.setdefault(email, asyncio.Lock())
        async with lock:
            current = await asyncio.to_thread(self.model.current, email, job_id)
            if current and (
                current["status"] == "completed"
                or (current["status"] == "failed" and not retry_failed)
            ):
                return current
            task = self._tasks.get(email)
            if task is None or task.done():
                try:
                    context = await asyncio.to_thread(self.model.context, email, job_id)
                    state = await asyncio.to_thread(
                        self.model.claim,
                        email=email,
                        job_id=job_id,
                        provider=sms_provider,
                        max_price=sms_max_price,
                    )
                except Exception as error:
                    return await asyncio.to_thread(
                        self.model.fail,
                        email=email,
                        job_id=job_id,
                        error=error,
                        provider=sms_provider,
                    )
                if state["status"] == "completed":
                    return state
                token, _ = self._issue_code_token(email)
                payload = {
                    **context,
                    "job_id": job_id,
                    "code_url": (
                        str(base_url or "").rstrip("/")
                        + PROTOCOL_CODE_PREFIX
                        + quote(token)
                    ),
                    "sms_provider": sms_provider,
                    "sms_max_price": sms_max_price,
                    "sms_countries": list(sms_policy["countries"]),
                    "sms_max_attempts": PLUS_CODEX_MAX_ATTEMPTS,
                }
                task = asyncio.create_task(
                    self._execute(
                        email=email,
                        job_id=job_id,
                        provider=sms_provider,
                        code_token=token,
                        payload=payload,
                    )
                )
                self._tasks[email] = task
                await asyncio.sleep(0)
            return await asyncio.to_thread(
                self.model.current, email, job_id
            ) or PlusCodexView.present(None)

    async def _execute(
        self,
        *,
        email: str,
        job_id: str,
        provider: str,
        code_token: str,
        payload: dict[str, Any],
    ) -> None:
        def on_event(event: dict[str, Any]) -> None:
            event_name = str(event.get("event") or "")
            stage, default_detail = self.EVENT_STAGES.get(
                event_name,
                (str(event.get("stage") or "protocol"), "Plus 协议接码正在执行"),
            )
            detail = str(event.get("message") or default_detail)
            self.model.progress(
                email=email,
                job_id=job_id,
                stage=stage,
                detail=detail,
                level=str(event.get("level") or "info"),
            )

        try:
            result = await self.runner(payload, on_event)
            await asyncio.to_thread(
                self.model.complete, email=email, job_id=job_id, result=result
            )
            if self.on_account_saved is not None:
                await self.on_account_saved(email)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await asyncio.to_thread(
                self.model.fail,
                email=email,
                job_id=job_id,
                error=error,
                provider=provider,
            )
        finally:
            if code_token:
                self._code_tokens.pop(code_token, None)

    async def close(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        close = getattr(self.runner, "close", None)
        if callable(close):
            result = close()
            if isinstance(result, Awaitable):
                await result
        self._tasks.clear()
        self._code_tokens.clear()


__all__ = [
    "PLUS_CODEX_EVENT_PREFIX",
    "PLUS_CODEX_MAX_ATTEMPTS",
    "PLUS_CODEX_RESULT_PREFIX",
    "PLUS_CODEX_TERMINAL_STATUSES",
    "DIRECT_PLUS_PHONE_JOB_PREFIX",
    "PlusCodexModel",
    "PlusCodexPresenter",
    "PlusCodexView",
    "ProtocolPlusCodexRunner",
    "direct_plus_phone_job_id",
]
