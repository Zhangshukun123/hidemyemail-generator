"""Concurrent task manager for Mail Auth protocol registrations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
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
AccountFinished = Callable[[str, bool, str], Awaitable[None]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


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
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.db_file = Path(db_file).resolve()
        self.proxy_store = proxy_store
        self.worker_runner = worker_runner
        self.on_account_saved = on_account_saved
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
        if not 1 <= concurrency <= 5:
            raise ValueError("协议注册并发必须是 1–5")
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
            self._run(unique, origin, concurrency, on_account_finished),
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
        on_account_finished: AccountFinished | None,
    ) -> None:
        semaphore = asyncio.Semaphore(concurrency)

        async def guarded(email: str) -> None:
            async with semaphore:
                if self._cancel_requested:
                    return
                await self._run_one(email, base_url, on_account_finished)

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
                self._state["message"] = (
                    f"协议注册完成：{self._state['succeeded']} 个账号均已添加密码和 2FA"
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
        record = await asyncio.to_thread(load_account_record, self.db_file, email)
        existing_two_factor = record.get("two_factor")
        existing_totp = (
            str(existing_two_factor.get("secret") or "")
            if isinstance(existing_two_factor, dict)
            else ""
        )
        proxy_url = ""
        proxy_state: dict[str, Any] = {}
        if self.proxy_store is not None:
            proxy_url, proxy_state = await asyncio.to_thread(
                self.proxy_store.next_proxy
            )
            if proxy_url:
                self._append_log(
                    f"已分配注册代理：{proxy_state.get('countryLabel') or proxy_state.get('country') or '代理出口'}",
                    email=email,
                    stage="network",
                )
        payload = {
            "email": email,
            "code_url": f"{base_url}{PROTOCOL_CODE_PREFIX}{quote(token)}",
            "proxy_url": proxy_url,
            "existing_password": str(record.get("password") or ""),
            "existing_password_confirmed": bool(
                record.get("password") and record.get("password_confirmed")
            ),
            "existing_totp_secret": existing_totp,
            "project_root": str(self.gptfree_root),
            "source_root": str(Path(__file__).resolve().parents[1]),
        }

        def on_event(event: dict[str, Any]) -> None:
            stage = str(event.get("stage") or "protocol_auth")
            status = str(event.get("status") or "active")
            self._state["phase"] = stage
            self._state["message"] = _sanitize_message(event.get("message"))
            self._append_log(
                event.get("message"), email=email, stage=stage, status=status
            )

        try:
            runner = self.worker_runner or self._run_worker
            result = await runner(payload, on_event)
            password = str(result.get("password") or "").strip()
            two_factor = result.get("two_factor")
            access_token = str(result.get("access_token") or "").strip()
            if len(password) < 12:
                raise RuntimeError("协议注册未返回已确认密码")
            if not access_token:
                raise RuntimeError("协议注册未返回 Access Token")
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
            await asyncio.to_thread(
                _save_account_record,
                self.db_file,
                email,
                result=result,
                password=password,
                password_confirmed=True,
                two_factor=two_factor,
            )
            if self.on_account_saved is not None:
                try:
                    saved_callback_result = await self.on_account_saved(email)
                    if isinstance(saved_callback_result, dict):
                        checkout_type = str(
                            saved_callback_result.get("checkout_id_type") or ""
                        ).strip().lower()
                        account_state["checkoutIdType"] = checkout_type
                        account_state["checkoutProbeStatus"] = str(
                            saved_callback_result.get("status") or ""
                        )
                        checkout_labels = {
                            "oaics": "OAICS",
                            "cs_live": "CS LIVE",
                            "cs": "CS",
                            "other": "OTHER",
                            "error": "检测失败",
                        }
                        self._append_log(
                            "Checkout 自动验证："
                            + checkout_labels.get(checkout_type, "待检测")
                            + (
                                f"（第 {int(saved_callback_result.get('attempt_count') or 1)}"
                                f"/{int(saved_callback_result.get('max_attempts') or 1)} 次）"
                                if int(saved_callback_result.get("attempt_count") or 1) > 1
                                else ""
                            ),
                            email=email,
                            stage="checkout_probe",
                            status=(
                                "success" if checkout_type == "oaics" else "warning"
                            ),
                        )
                except Exception as error:
                    self._append_log(
                        f"账号已保存，但同步到远程服务器失败：{error}",
                        email=email,
                        stage="sync",
                        status="warning",
                    )
            account_state["status"] = "success"
            account_state["stage"] = "completed"
            account_state["message"] = "协议注册完成（密码+2FA）"
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
            account_state["stage"] = "failed"
            account_state["message"] = _sanitize_message(error)
            completion_message = account_state["message"]
            self._state["failed"] += 1
            self._append_log(
                f"失败：{error}", email=email, stage="failed", status="error"
            )
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


__all__ = [
    "EVENT_PREFIX",
    "PROTOCOL_CODE_PREFIX",
    "ProtocolRegistrationManager",
    "RESULT_PREFIX",
]
