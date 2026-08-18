"""Concurrent task manager for Mail Auth protocol registrations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from .browser_tasks import (
    _save_account_record,
    account_saved_cookies,
    account_session,
    account_session_access_token,
    build_registration_environment,
    load_account_record,
)


EVENT_PREFIX = "HME_PROTOCOL_EVENT:"
RESULT_PREFIX = "HME_PROTOCOL_RESULT:"
PROTOCOL_CODE_PREFIX = "/api/protocol-registration/code/"
WorkerRunner = Callable[
    [dict[str, Any], Callable[[dict[str, Any]], None]],
    Awaitable[dict[str, Any]],
]
AccountSaved = Callable[[str], Awaitable[dict[str, Any] | None]]
AccountPlanVerifier = Callable[[str], Awaitable[dict[str, Any] | None]]
AccountFinished = Callable[[str, bool, str], Awaitable[None]]
RecordFailure = Callable[[dict[str, Any]], Awaitable[None] | None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_email(value: str) -> bool:
    return bool(
        len(value) <= 320
        and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value)
    )


def _sanitize_message(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(
        r"(?i)((?:access_?token|session_?token|otp)\s*[=:]\s*)[A-Za-z0-9._~+/-]{6,}",
        r"\1***",
        text,
    )
    return text[:500]


class ProtocolRegistrationManager:
    """Run protocol registrations while exposing a polling-friendly snapshot."""

    def __init__(
        self,
        *,
        base_dir: Path,
        db_file: Path,
        proxy_store: Any | None = None,
        gptfree_root: Path | None = None,
        python_executable: Path | None = None,
        worker_runner: WorkerRunner | None = None,
        on_account_saved: AccountSaved | None = None,
        verify_account_plan: AccountPlanVerifier | None = None,
        record_failure: RecordFailure | None = None,
        max_concurrency: int = 5,
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.db_file = Path(db_file).resolve()
        self.proxy_store = proxy_store
        self.worker_runner = worker_runner
        self.on_account_saved = on_account_saved
        self.verify_account_plan = verify_account_plan
        self.record_failure = record_failure
        if isinstance(max_concurrency, bool) or not 1 <= int(max_concurrency) <= 10:
            raise ValueError("协议注册最大并发必须是 1–10")
        self.max_concurrency = int(max_concurrency)
        self.worker_script = Path(__file__).with_name(
            "protocol_registration_worker.py"
        ).resolve()
        self.gptfree_root = self._resolve_root(gptfree_root)
        self.python_executable = self._resolve_python(python_executable)
        self._task: asyncio.Task[None] | None = None
        self._active_processes: set[asyncio.subprocess.Process] = set()
        self._code_tokens: dict[str, dict[str, str]] = {}
        self._cancel_requested = False
        self._runtime_cache: dict[str, Any] | None = None
        self._state = self._idle_state()

    def _resolve_root(self, explicit: Path | None) -> Path:
        bundled_root = Path(__file__).with_name("vendor") / "gptfree_register"
        candidates = [
            explicit,
            Path(os.environ["GPTFREE_REGISTER_ROOT"])
            if os.environ.get("GPTFREE_REGISTER_ROOT")
            else None,
            bundled_root,
            self.base_dir.parent / "gptfree-register",
            Path(r"D:\AI\gptfree-register"),
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            root = Path(candidate).expanduser().resolve()
            if (root / "core" / "chatgpt_register.py").is_file():
                return root
        return Path(explicit or bundled_root).resolve()

    def _resolve_python(self, explicit: Path | None) -> Path:
        configured = os.environ.get("GPTFREE_REGISTER_PYTHON")
        current_python = Path(sys.executable).resolve()
        console_python = (
            current_python.with_name("python.exe")
            if current_python.name.casefold() == "pythonw.exe"
            else current_python
        )
        candidates = [
            explicit,
            Path(configured) if configured else None,
            self.gptfree_root / ".venv" / "Scripts" / "python.exe",
            self.gptfree_root / ".venv" / "bin" / "python",
            console_python,
            current_python,
        ]
        for candidate in candidates:
            if candidate is not None and Path(candidate).is_file():
                return Path(candidate).resolve()
        return Path(explicit or console_python).resolve()

    def _runtime_state(self) -> dict[str, Any]:
        if self._runtime_cache is not None:
            return deepcopy(self._runtime_cache)
        missing: list[str] = []
        core = self.gptfree_root / "core"
        required_core = [
            (core / "chatgpt_register.py", "gptfree-register/core/chatgpt_register.py"),
            (core / "sentinel_token.py", "gptfree-register/core/sentinel_token.py"),
            (core / "codex_oauth.py", "gptfree-register/core/codex_oauth.py"),
            (
                core / "sentinel_vm" / "runtime_worker.js",
                "gptfree-register/core/sentinel_vm/runtime_worker.js",
            ),
            (core / "gen_token_jsdom.js", "gptfree-register/core/gen_token_jsdom.js"),
        ]
        protocol_module = core / "gpt_trial_protocol"
        for name in (
            "__init__.py",
            "chatgpt.py",
            "codex_oauth.py",
            "email_code.py",
            "errors.py",
            "flows.py",
            "http_client.py",
            "local_email_code.py",
            "models.py",
            "risk_tokens.py",
            "sentinel_http.py",
            "service.py",
            "sms.py",
        ):
            required_core.append(
                (
                    protocol_module / name,
                    f"gptfree-register/core/gpt_trial_protocol/{name}",
                )
            )
        for path, label in required_core:
            if not path.is_file():
                missing.append(label)
        if not self.python_executable.is_file():
            missing.append("gptfree Python")
        if not self.worker_script.is_file():
            missing.append("protocol_registration_worker.py")
        if self.worker_runner is None and not shutil.which("node"):
            missing.append("Node.js")
        if (
            self.worker_runner is None
            and shutil.which("node")
            and not (core / "node_modules" / "jsdom" / "package.json").is_file()
        ):
            missing.append("jsdom")
        if self.worker_runner is None and self.python_executable.is_file():
            python_checks = (
                ("curl_cffi", "import curl_cffi"),
                (
                    "gptfree Python 内核",
                    "import sys;"
                    f"sys.path.insert(0, {str(core)!r});"
                    "from chatgpt_register import ChatGPTRegister;"
                    "from sentinel_token import SentinelTokenProvider;"
                    "SentinelTokenProvider()._browser_profile('runtime-check')",
                ),
            )
            for label, command in python_checks:
                try:
                    result = __import__("subprocess").run(
                        [str(self.python_executable), "-c", command],
                        capture_output=True,
                        timeout=20,
                        check=False,
                    )
                    if result.returncode:
                        missing.append(label)
                except (OSError, TimeoutError):
                    missing.append(label)
        self._runtime_cache = {
            "available": not missing or self.worker_runner is not None,
            "projectRoot": str(self.gptfree_root),
            "python": str(self.python_executable),
            "error": "、".join(missing),
        }
        return deepcopy(self._runtime_cache)

    def _idle_state(self) -> dict[str, Any]:
        return {
            "id": "",
            "running": False,
            "status": "idle",
            "phase": "idle",
            "message": "等待协议注册任务",
            "total": 0,
            "completed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "concurrency": 1,
            "currentEmail": "",
            "startedAt": "",
            "finishedAt": "",
            "accounts": [],
            "logs": [],
        }

    def snapshot(self) -> dict[str, Any]:
        state = deepcopy(self._state)
        state["runtime"] = self._runtime_state()
        return state

    def refresh_runtime(self) -> dict[str, Any]:
        """Discard the cached dependency probe and return the current state."""
        self._runtime_cache = None
        return self._runtime_state()

    def token_record(self, token: str) -> dict[str, str] | None:
        return deepcopy(self._code_tokens.get(str(token or "")))

    def valid_code_token(self, token: str) -> bool:
        return bool(self._code_tokens.get(str(token or "")))

    def _append_log(
        self,
        message: Any,
        *,
        email: str = "",
        stage: str = "running",
        status: str = "active",
    ) -> None:
        entry = {
            "at": _utc_now(),
            "email": email,
            "stage": stage,
            "status": status,
            "message": _sanitize_message(message),
        }
        self._state["logs"].append(entry)
        self._state["logs"] = self._state["logs"][-250:]
        if email:
            for account in self._state["accounts"]:
                if account["email"] == email:
                    account["stage"] = stage
                    account["message"] = entry["message"]
                    break

    def start(
        self,
        *,
        emails: list[str],
        base_url: str,
        concurrency: int = 1,
        setup_credentials: bool = True,
        on_account_finished: AccountFinished | None = None,
    ) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            raise RuntimeError("协议注册任务正在运行")
        runtime = self._runtime_state()
        if not runtime["available"]:
            raise RuntimeError(f"协议运行环境未就绪：{runtime['error']}")
        unique: list[str] = []
        seen: set[str] = set()
        for value in emails:
            email = str(value or "").strip().lower()
            if not _valid_email(email):
                raise ValueError(f"协议注册邮箱无效：{email or value}")
            if email not in seen:
                seen.add(email)
                unique.append(email)
        if not unique:
            raise ValueError("请选择至少一个待协议注册账号")
        concurrency = int(concurrency)
        if not 1 <= concurrency <= self.max_concurrency:
            raise ValueError(f"协议注册并发必须是 1–{self.max_concurrency}")
        origin = str(base_url or "").strip().rstrip("/")
        if not origin.startswith(("http://", "https://")):
            raise ValueError("协议注册本地服务地址无效")

        task_id = secrets.token_hex(8)
        self._cancel_requested = False
        self._code_tokens.clear()
        self._state = {
            "id": task_id,
            "running": True,
            "status": "running",
            "phase": "prepare",
            "message": f"准备协议注册 {len(unique)} 个账号",
            "total": len(unique),
            "completed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "concurrency": concurrency,
            "setupCredentials": bool(setup_credentials),
            "currentEmail": "",
            "startedAt": _utc_now(),
            "finishedAt": "",
            "accounts": [
                {
                    "email": email,
                    "status": "queued",
                    "stage": "prepare",
                    "message": "等待执行",
                }
                for email in unique
            ],
            "logs": [],
        }
        self._append_log(self._state["message"], stage="prepare")
        self._task = asyncio.create_task(
            self._run(
                unique,
                origin,
                concurrency,
                bool(setup_credentials),
                on_account_finished,
            ),
            name=f"protocol-registration-{task_id}",
        )
        return self.snapshot()

    async def stop(self) -> dict[str, Any]:
        self._cancel_requested = True
        if self._task is None or self._task.done():
            return self.snapshot()
        self._state["status"] = "cancelling"
        self._state["phase"] = "cancelling"
        self._state["message"] = "正在停止协议注册任务"
        self._append_log(self._state["message"], stage="cancelling", status="warning")
        for process in list(self._active_processes):
            if process.returncode is None:
                process.terminate()
        await self._task
        return self.snapshot()

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            await self.stop()

    async def wait(self) -> dict[str, Any]:
        if self._task is not None:
            await self._task
        return self.snapshot()

    async def _run(
        self,
        emails: list[str],
        base_url: str,
        concurrency: int,
        setup_credentials: bool,
        on_account_finished: AccountFinished | None,
    ) -> None:
        semaphore = asyncio.Semaphore(concurrency)

        async def guarded(email: str) -> None:
            async with semaphore:
                if self._cancel_requested:
                    return
                await self._run_one(
                    email,
                    base_url,
                    setup_credentials,
                    on_account_finished,
                )

        try:
            await asyncio.gather(*(guarded(email) for email in emails))
        finally:
            self._code_tokens.clear()
            self._state["running"] = False
            self._state["finishedAt"] = _utc_now()
            if self._cancel_requested:
                self._state["status"] = "cancelled"
                self._state["phase"] = "cancelled"
                self._state["message"] = "协议注册任务已停止"
            elif self._state["failed"]:
                self._state["status"] = (
                    "failed" if not self._state["succeeded"] else "completed"
                )
                self._state["phase"] = "completed"
                self._state["message"] = (
                    f"协议注册完成：成功 {self._state['succeeded']}，"
                    f"失败 {self._state['failed']}"
                )
            else:
                self._state["status"] = "completed"
                self._state["phase"] = "completed"
                completion = (
                    "Session/Cookie、密码和 2FA"
                    if setup_credentials
                    else "Session/Cookie"
                )
                self._state["message"] = (
                    f"协议注册完成：{self._state['succeeded']} 个账号已保存{completion}"
                )
            self._append_log(
                self._state["message"],
                stage=self._state["phase"],
                status="success" if self._state["status"] == "completed" else "warning",
            )

    async def _run_one(
        self,
        email: str,
        base_url: str,
        setup_credentials: bool,
        on_account_finished: AccountFinished | None,
    ) -> None:
        account_state = next(
            item for item in self._state["accounts"] if item["email"] == email
        )
        account_state["status"] = "running"
        completion_success = False
        completion_message = ""
        self._state["currentEmail"] = email
        self._state["phase"] = "protocol_auth"
        self._state["message"] = f"正在协议注册 {email}"
        self._append_log(
            "启动 Mail Auth 协议注册",
            email=email,
            stage="protocol_auth",
        )
        token = secrets.token_urlsafe(24)
        since = _utc_now()
        self._code_tokens[token] = {"email": email, "since": since}
        setup_error: BaseException | None = None
        payload: dict[str, Any] = {}
        proxy_url = ""
        proxy_state: dict[str, Any] = {}
        setup_stage = "account_load"
        try:
            record = await asyncio.to_thread(load_account_record, self.db_file, email)
            existing_two_factor = record.get("two_factor")
            existing_totp = (
                str(existing_two_factor.get("secret") or "")
                if isinstance(existing_two_factor, dict)
                else ""
            )
            existing_session = account_session(record)
            existing_cookies = account_saved_cookies(record)
            existing_device_id = next(
                (
                    str(cookie.get("value") or "")
                    for cookie in existing_cookies
                    if str(cookie.get("name") or "") == "oai-did"
                ),
                "",
            )
            existing_diagnostics = record.get("registration_diagnostics")
            existing_impersonate = (
                str(existing_diagnostics.get("impersonate") or "")
                if isinstance(existing_diagnostics, dict)
                else ""
            )
            if self.proxy_store is not None:
                setup_stage = "proxy"
                proxy_url, proxy_state = await asyncio.to_thread(
                    self.proxy_store.next_proxy
                )
                if proxy_url:
                    self._append_log(
                        f"已分配注册代理：{proxy_state.get('countryLabel') or proxy_state.get('country') or '代理出口'}",
                        email=email,
                        stage="network",
                    )
            setup_stage = "protocol_auth"
            payload = {
                "email": email,
                "code_url": f"{base_url}{PROTOCOL_CODE_PREFIX}{quote(token)}",
                "proxy_url": proxy_url,
                "proxy_country": str(
                    proxy_state.get("lastExitCountry")
                    or proxy_state.get("country")
                    or ""
                ).strip().upper(),
                "existing_password": str(record.get("password") or ""),
                "existing_password_confirmed": bool(
                    record.get("password") and record.get("password_confirmed")
                ),
                "existing_totp_secret": existing_totp,
                "existing_access_token": account_session_access_token(record),
                "existing_session_token": str(
                    existing_session.get("sessionToken")
                    or existing_session.get("session_token")
                    or ""
                ),
                "existing_session_json": existing_session,
                "existing_session_cookies": existing_cookies,
                "existing_device_id": existing_device_id,
                "existing_impersonate": existing_impersonate,
                "setup_credentials": setup_credentials,
                "project_root": str(self.gptfree_root),
                "source_root": str(Path(__file__).resolve().parents[1]),
            }
        except asyncio.CancelledError as error:
            setup_error = error
        except Exception as error:
            setup_error = error
        password_candidate_saved = False
        session_checkpoint_saved = False

        def on_event(event: dict[str, Any]) -> None:
            nonlocal password_candidate_saved, session_checkpoint_saved
            stage = str(event.get("stage") or "protocol_auth")
            status = str(event.get("status") or "active")
            self._state["phase"] = stage
            self._state["message"] = _sanitize_message(event.get("message"))
            self._append_log(
                event.get("message"), email=email, stage=stage, status=status
            )
            checkpoint = event.get("password_checkpoint")
            if not isinstance(checkpoint, dict):
                return
            checkpoint_password = str(checkpoint.get("password") or "").strip()
            checkpoint_result = checkpoint.get("result")
            checkpoint_confirmed = bool(checkpoint.get("password_confirmed"))
            if len(checkpoint_password) < 12 or not isinstance(
                checkpoint_result, dict
            ):
                return
            try:
                _save_account_record(
                    self.db_file,
                    email,
                    result=checkpoint_result,
                    password=checkpoint_password,
                    password_confirmed=checkpoint_confirmed,
                )
                password_candidate_saved = True
                session_checkpoint_saved = session_checkpoint_saved or bool(
                    checkpoint_confirmed
                    and str(checkpoint_result.get("access_token") or "").strip()
                )
            except Exception as error:
                self._append_log(
                    f"账号密码检查点保存失败：{error}",
                    email=email,
                    stage="password_checkpoint",
                    status="warning",
                )

        try:
            if setup_error is not None:
                raise setup_error
            runner = self.worker_runner or self._run_worker
            result = await runner(payload, on_event)
            password = str(result.get("password") or "").strip()
            two_factor = result.get("two_factor")
            access_token = str(result.get("access_token") or "").strip()
            if not access_token:
                raise RuntimeError("协议注册未返回 Access Token")
            if setup_credentials:
                if len(password) < 12:
                    raise RuntimeError("协议注册未返回已确认密码")
                if not (
                    isinstance(two_factor, dict)
                    and two_factor.get("enabled")
                    and two_factor.get("secret")
                ):
                    raise RuntimeError("协议注册未确认 TOTP 2FA")
            result = dict(result)
            result["registration_environment"] = build_registration_environment(
                email,
                registration_mode="protocol",
                proxy_url=proxy_url,
                proxy_state=proxy_state,
            )
            if proxy_url:
                result["registration_proxy_url"] = proxy_url
                result["registration_proxy"] = {
                    "mode": str(proxy_state.get("mode") or ""),
                    "country": str(proxy_state.get("country") or ""),
                    "endpoint": str(proxy_state.get("endpoint") or ""),
                    "node": str(proxy_state.get("currentNode") or ""),
                    "saved_at": _utc_now(),
                }
            await asyncio.to_thread(
                _save_account_record,
                self.db_file,
                email,
                result=result,
                password=password if setup_credentials else "",
                password_confirmed=True if setup_credentials else None,
                two_factor=two_factor if setup_credentials else None,
            )
            if self.verify_account_plan is not None:
                try:
                    plan_result = await self.verify_account_plan(email)
                    if isinstance(plan_result, dict):
                        plan_status = str(plan_result.get("status") or "").lower()
                        if plan_status in {"plus", "free"}:
                            account_state["accountType"] = plan_status
                            account_state["planVerificationSource"] = str(
                                plan_result.get("source") or "access_token_online"
                            )
                            self._append_log(
                                f"AT 套餐查询完成：{plan_status.title()}",
                                email=email,
                                stage="plan_verification",
                                status="success",
                            )
                        else:
                            self._append_log(
                                "AT 套餐查询未确认："
                                + _sanitize_message(
                                    plan_result.get("detail") or plan_status
                                ),
                                email=email,
                                stage="plan_verification",
                                status="warning",
                            )
                except Exception as error:
                    self._append_log(
                        f"账号已保存，但 AT 套餐查询失败：{error}",
                        email=email,
                        stage="plan_verification",
                        status="warning",
                    )
            if self.on_account_saved is not None:
                try:
                    await self.on_account_saved(email)
                except Exception as error:
                    self._append_log(
                        f"账号已保存，但同步到远程服务器失败：{error}",
                        email=email,
                        stage="sync",
                        status="warning",
                    )
            account_state["status"] = "success"
            account_state["stage"] = "completed"
            account_state["message"] = (
                "协议注册完成（Session/Cookie + 密码 + 2FA）"
                if setup_credentials
                else "协议注册完成（仅 Session/Cookie）"
            )
            completion_success = True
            completion_message = account_state["message"]
            self._state["succeeded"] += 1
            self._append_log(
                account_state["message"],
                email=email,
                stage="completed",
                status="success",
            )
        except asyncio.CancelledError:
            account_state["status"] = "cancelled"
            account_state["message"] = "任务已停止"
            completion_message = account_state["message"]
            raise
        except Exception as error:
            account_state["status"] = "failed"
            failed_stage = (
                "two_factor"
                if session_checkpoint_saved
                else "password" if password_candidate_saved else setup_stage
            )
            account_state["stage"] = failed_stage
            account_state["message"] = (
                "账号注册和密码已保存；TOTP 2FA 待补跑："
                + _sanitize_message(error)
                if session_checkpoint_saved
                else (
                    "已保留同一注册密码；下次将复用，不会重新生成："
                    + _sanitize_message(error)
                    if password_candidate_saved
                    else _sanitize_message(error)
                )
            )
            completion_message = account_state["message"]
            self._state["failed"] += 1
            self._append_log(
                (
                    f"账号注册和密码已保存；TOTP 2FA 待补跑：{error}"
                    if session_checkpoint_saved
                    else (
                        f"已保留同一注册密码；下次将复用，不会重新生成：{error}"
                        if password_candidate_saved
                        else f"失败：{error}"
                    )
                ),
                email=email,
                stage=failed_stage,
                status=(
                    "warning"
                    if session_checkpoint_saved or password_candidate_saved
                    else "error"
                ),
            )
            if self.record_failure is not None:
                failed_stage = str(account_state.get("stage") or "failed")
                failure_logs = [
                    dict(log)
                    for log in self._state.get("logs", [])
                    if isinstance(log, dict)
                    and str(log.get("email") or "") in {"", email}
                ][-80:]
                failure = {
                    "processId": f"{self._state.get('id') or 'protocol'}:{email}",
                    "status": "failed",
                    "mode": "protocol",
                    "provider": "mail_auth",
                    "browserEngine": "protocol",
                    "email": email,
                    "emails": [email],
                    "message": account_state["message"],
                    "currentStage": failed_stage,
                    "currentLocation": "Mail Auth 协议注册",
                    "currentAction": "检查协议注册失败步骤并重试",
                    "startedAt": str(self._state.get("startedAt") or ""),
                    "recordedAt": _utc_now(),
                    "logs": failure_logs,
                    "failureContext": {
                        "message": account_state["message"],
                        "currentStage": failed_stage,
                        "currentLocation": "Mail Auth 协议注册",
                        "currentAction": "检查协议注册失败步骤并重试",
                        "failedStage": failed_stage,
                        "logs": failure_logs,
                        "failedAccounts": [dict(account_state)],
                    },
                }
                for attempt in range(3):
                    try:
                        stored = self.record_failure(failure)
                        if inspect.isawaitable(stored):
                            await stored
                        account_state.pop("monitorError", None)
                        break
                    except Exception as monitor_error:
                        account_state["monitorError"] = _sanitize_message(
                            monitor_error
                        )
                        if attempt < 2:
                            await asyncio.sleep(0.05 * (2**attempt))
        finally:
            self._code_tokens.pop(token, None)
            self._state["completed"] += 1
            if on_account_finished is not None:
                try:
                    await on_account_finished(
                        email,
                        completion_success,
                        completion_message or "协议注册未完成",
                    )
                except Exception as error:
                    self._append_log(
                        f"协议注册结果写回库存失败：{error}",
                        email=email,
                        stage="inventory",
                        status="warning",
                    )

    async def _run_worker(
        self,
        payload: dict[str, Any],
        on_event: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        worker_env = os.environ.copy()
        worker_env.update(
            {
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        process = await asyncio.create_subprocess_exec(
            str(self.python_executable),
            str(self.worker_script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=worker_env,
        )
        self._active_processes.add(process)
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise RuntimeError("协议注册子进程管道创建失败")
        process.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        with __import__("contextlib").suppress(Exception):
            await process.stdin.wait_closed()
        stderr_task = asyncio.create_task(process.stderr.read())
        result: dict[str, Any] = {}
        try:
            async for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line.startswith(EVENT_PREFIX):
                    try:
                        event = json.loads(line[len(EVENT_PREFIX) :])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
                    if isinstance(event, dict):
                        on_event(event)
                elif line.startswith(RESULT_PREFIX):
                    try:
                        parsed = json.loads(line[len(RESULT_PREFIX) :])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        parsed = {}
                    if isinstance(parsed, dict):
                        result = parsed
                elif line:
                    on_event({"stage": "protocol_auth", "message": line})
            return_code = await process.wait()
            stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
            if return_code or str(result.get("status") or "") != "success":
                error = str(result.get("error") or stderr or f"子进程退出 {return_code}")
                raise RuntimeError(_sanitize_message(error))
            return result
        finally:
            self._active_processes.discard(process)
            if not stderr_task.done():
                stderr_task.cancel()


class ConcurrentProtocolRegistrationManager:
    """Expose independent Mail Auth registration processes through one API."""

    def __init__(
        self,
        *,
        process_factory: Callable[[], ProtocolRegistrationManager],
        on_account_saved: AccountSaved | None = None,
        verify_account_plan: AccountPlanVerifier | None = None,
        max_processes: int = 5,
        history_limit: int = 20,
    ) -> None:
        self.process_factory = process_factory
        self.on_account_saved = on_account_saved
        self.verify_account_plan = verify_account_plan
        # Keep the single-manager test/integration hooks available while each
        # started flow receives its own isolated manager instance.
        self.worker_runner: WorkerRunner | None = None
        self._runtime_cache: dict[str, Any] | None = None
        self.max_processes = max(2, min(10, int(max_processes)))
        self.history_limit = max(self.max_processes, int(history_limit))
        self._processes: dict[str, ProtocolRegistrationManager] = {}
        self._latest_process_id = ""
        self._runtime_probe: ProtocolRegistrationManager | None = None

    def _new_process(self) -> ProtocolRegistrationManager:
        manager = self.process_factory()
        manager.on_account_saved = self.on_account_saved
        manager.verify_account_plan = self.verify_account_plan
        manager.worker_runner = self.worker_runner
        manager._runtime_cache = deepcopy(self._runtime_cache)
        return manager

    def _probe(self) -> ProtocolRegistrationManager:
        if self._runtime_probe is None:
            self._runtime_probe = self._new_process()
        return self._runtime_probe

    def _process_snapshots(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (process_id, manager.snapshot())
            for process_id, manager in self._processes.items()
        ]

    def _trim_history(self) -> None:
        completed = [
            process_id
            for process_id, state in self._process_snapshots()
            if not state.get("running")
        ]
        while len(self._processes) > self.history_limit and completed:
            self._processes.pop(completed.pop(0), None)

    def snapshot(self) -> dict[str, Any]:
        snapshots = self._process_snapshots()
        if not snapshots:
            state = self._probe().snapshot()
            state.update(
                runningCount=0,
                processCount=0,
                maxProcesses=self.max_processes,
                canStartNext=True,
                tasks=[],
            )
            return state

        active = [item for item in snapshots if item[1].get("running")]
        latest = next(
            (
                item
                for item in reversed(snapshots)
                if item[0] == self._latest_process_id
            ),
            snapshots[-1],
        )
        display = active[-1] if active else latest
        display_state = deepcopy(display[1])
        public_tasks: list[dict[str, Any]] = []
        combined_logs: list[dict[str, Any]] = []
        for process_index, (process_id, state) in enumerate(snapshots, start=1):
            accounts = state.get("accounts") if isinstance(state.get("accounts"), list) else []
            first_email = str(
                (accounts[0].get("email") if accounts and isinstance(accounts[0], dict) else "")
                or state.get("currentEmail")
                or ""
            ).strip().lower()
            label = f"协议流程 {process_index}"
            if first_email:
                label += f" · {first_email}"
            public_tasks.append(
                {
                    **deepcopy(state),
                    "processId": process_id,
                    "processIndex": process_index,
                    "processLabel": label,
                }
            )
            for log in state.get("logs", []):
                if not isinstance(log, dict):
                    continue
                entry = dict(log)
                entry["processId"] = process_id
                entry["processIndex"] = process_index
                entry["message"] = f"[{label}] {entry.get('message', '')}"
                combined_logs.append(entry)
        combined_logs.sort(key=lambda item: str(item.get("at") or ""))
        active_count = len(active)
        display_state.update(
            id=display[0],
            running=bool(active),
            status="running" if active else display_state.get("status", "idle"),
            message=(
                f"{active_count} 个协议注册流程正在运行；最新流程："
                f"{display_state.get('message', '')}"
                if active_count
                else display_state.get("message", "")
            ),
            logs=combined_logs[-250:],
            runningCount=active_count,
            processCount=len(snapshots),
            maxProcesses=self.max_processes,
            canStartNext=active_count < self.max_processes,
            tasks=public_tasks,
        )
        return display_state

    def refresh_runtime(self) -> dict[str, Any]:
        self._runtime_cache = None
        probe = self._probe()
        probe._runtime_cache = None
        return probe.refresh_runtime()

    def token_record(self, token: str) -> dict[str, str] | None:
        for manager in reversed(list(self._processes.values())):
            record = manager.token_record(token)
            if record:
                return record
        return self._probe().token_record(token)

    def valid_code_token(self, token: str) -> bool:
        return self.token_record(token) is not None

    def start(self, **kwargs: Any) -> dict[str, Any]:
        active_count = sum(
            bool(state.get("running")) for _, state in self._process_snapshots()
        )
        if active_count >= self.max_processes:
            raise RuntimeError(f"协议注册流程已达到上限 {self.max_processes}")
        manager = self._new_process()
        task = manager.start(**kwargs)
        process_id = str(task.get("id") or secrets.token_hex(8))
        self._processes[process_id] = manager
        self._latest_process_id = process_id
        self._trim_history()
        return self.snapshot()

    async def stop(self, process_id: str = "") -> dict[str, Any]:
        target_process_id = str(process_id or "").strip()
        if target_process_id:
            manager = self._processes.get(target_process_id)
            if manager is None:
                raise ValueError("协议注册流程不存在或已归档")
            managers = [manager] if manager.snapshot().get("running") else []
        else:
            managers = [
                manager
                for manager in self._processes.values()
                if manager.snapshot().get("running")
            ]
        if managers:
            await asyncio.gather(*(manager.stop() for manager in managers))
        return self.snapshot()

    async def wait(self) -> dict[str, Any]:
        managers = list(self._processes.values())
        if managers:
            await asyncio.gather(*(manager.wait() for manager in managers))
        return self.snapshot()

    async def close(self) -> None:
        managers = [
            manager
            for manager in self._processes.values()
            if manager.snapshot().get("running")
        ]
        if managers:
            await asyncio.gather(*(manager.stop() for manager in managers))


__all__ = [
    "ConcurrentProtocolRegistrationManager",
    "EVENT_PREFIX",
    "PROTOCOL_CODE_PREFIX",
    "ProtocolRegistrationManager",
    "RESULT_PREFIX",
]
