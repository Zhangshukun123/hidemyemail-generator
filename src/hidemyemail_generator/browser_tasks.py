from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .inbox import connect_db, mark_address


EVENT_PREFIX = "HME_BROWSER_EVENT:"
MAX_LOG_ITEMS = 300


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    if password and password_confirmed is not False:
        current["password"] = password
    if password_confirmed is True:
        current["password_confirmed"] = True
        current["password_confirmed_at"] = utc_now()
    if isinstance(two_factor, dict) and two_factor.get("secret"):
        current["two_factor"] = dict(two_factor)
    if result:
        access_token = str(result.get("access_token") or "").strip()
        session_json = str(result.get("session_json") or "").strip()
        storage_state_json = str(result.get("storage_state_json") or "").strip()
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
        result_two_factor = result.get("two_factor")
        if isinstance(result_two_factor, dict) and result_two_factor.get("secret"):
            current["two_factor"] = dict(result_two_factor)

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
        self._batch_task: asyncio.Task | None = None
        self._processes: dict[str, asyncio.subprocess.Process] = {}
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
            "accounts": [],
            "logs": [],
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

    def start(
        self,
        accounts: list[dict[str, Any]],
        *,
        headless: bool,
        concurrency: int,
        skipped: int = 0,
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
            deduplicated.append(
                {
                    "email": email,
                    "password": str(account.get("password") or ""),
                    "ensure_password": bool(account.get("ensure_password", False)),
                    "enable_2fa": bool(account.get("enable_2fa", False)),
                    "two_factor": account.get("two_factor")
                    if isinstance(account.get("two_factor"), dict)
                    else {},
                }
            )
        if not deduplicated:
            raise RuntimeError("没有需要获取 Session 的 iCloud 邮箱")

        concurrency = max(1, min(10, int(concurrency)))
        headless = bool(headless or self.force_headless)
        self._state = {
            "id": uuid.uuid4().hex,
            "status": "running",
            "running": True,
            "headless": headless,
            "concurrency": concurrency,
            "total": len(deduplicated),
            "completed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": max(0, int(skipped)),
            "accounts": [
                {
                    "email": item["email"],
                    "status": "queued",
                    "message": "等待浏览器任务",
                    "latestLog": "",
                    "_password": item["password"],
                    "_ensure_password": item["ensure_password"],
                    "_password_confirmed": False,
                    "_enable_2fa": item["enable_2fa"],
                    "_two_factor": item["two_factor"],
                    "phase": "queued",
                    "twoFactorEnabled": bool(item["two_factor"].get("enabled")),
                }
                for item in deduplicated
            ],
            "logs": [],
            "startedAt": utc_now(),
            "finishedAt": "",
        }
        self._append_log(
            f"浏览器取全部已启动：待处理 {len(deduplicated)}，"
            f"跳过 {skipped}，并发 {concurrency}，"
            f"{'无头' if headless else '显示浏览器'}"
        )
        self._batch_task = asyncio.create_task(
            self._run_batch(headless=headless, concurrency=concurrency)
        )
        return self.snapshot()

    def _append_log(self, message: str, *, email: str = "") -> None:
        text = str(message or "").strip()
        if not text:
            return
        entry = {"at": utc_now(), "email": email, "message": text[:1000]}
        logs = self._state.setdefault("logs", [])
        logs.append(entry)
        if len(logs) > MAX_LOG_ITEMS:
            del logs[:-MAX_LOG_ITEMS]
        if email:
            item = self._account_item(email)
            if item is not None:
                item["latestLog"] = text[:500]

    def _account_item(self, email: str) -> dict[str, Any] | None:
        target = email.strip().lower()
        for item in self._state.get("accounts", []):
            if item.get("email") == target:
                return item
        return None

    async def _run_batch(self, *, headless: bool, concurrency: int) -> None:
        semaphore = asyncio.Semaphore(concurrency)
        try:
            await asyncio.gather(
                *(
                    self._run_account(item, semaphore=semaphore, headless=headless)
                    for item in self._state["accounts"]
                )
            )
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
            item["status"] = "running"
            item["phase"] = "registering_openai"
            item["message"] = "正在启动 Camoufox"
            self._append_log("开始浏览器注册或登录", email=email)

            env = os.environ.copy()
            env.update(
                {
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "HME_BROWSER_SERVICE_URL": self.service_url,
                    "HME_BROWSER_WORKER_TOKEN": self.worker_token,
                    "HME_BROWSER_DB_FILE": str(self.db_file),
                    "HME_OPENAI_PASSWORD": str(item.get("_password") or ""),
                    "HME_ENSURE_OPENAI_PASSWORD": "1"
                    if item.get("_ensure_password")
                    else "0",
                    "HME_ENABLE_OPENAI_2FA": "1"
                    if item.get("_enable_2fa")
                    else "0",
                    "HME_OPENAI_2FA_STATE": json.dumps(
                        item.get("_two_factor") or {}, ensure_ascii=False
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
                raise
            except Exception as error:
                return_code = -1
                item["_error"] = str(error)
            finally:
                self._processes.pop(email, None)

            password = str(item.get("_password") or "")
            result = item.get("_result")
            password_confirmed = bool(item.get("_password_confirmed"))
            if (
                return_code == 0
                and isinstance(result, dict)
                and (
                    not item.get("_ensure_password")
                    or password_confirmed
                )
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
                item["status"] = "success"
                item["phase"] = "completed"
                item["message"] = (
                    "Session / AT / 2FA 已保存"
                    if item.get("_enable_2fa")
                    else "Session / AT 已保存"
                )
                self._state["succeeded"] += 1
                self._append_log("Session / AT 已保存到本地数据库", email=email)
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
                self._append_log(f"失败：{error[:500]}", email=email)
            self._state["completed"] += 1

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
                self._append_log(line[:500], email=email)
                continue
            try:
                event = json.loads(line[len(EVENT_PREFIX) :])
            except (json.JSONDecodeError, TypeError, ValueError):
                self._append_log("浏览器工作器返回了无效事件", email=email)
                continue
            kind = str(event.get("type") or "")
            if kind == "log":
                self._append_log(str(event.get("message") or ""), email=email)
            elif kind == "result":
                result = event.get("result")
                if isinstance(result, dict):
                    item["_result"] = result
                password = str(event.get("password") or "")
                if password:
                    item["_password"] = password
                item["_password_confirmed"] = bool(
                    event.get("password_confirmed")
                )
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
                if isinstance(result, dict):
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
                item["message"] = "OpenAI 注册成功，正在开启 2FA"
            elif kind == "two_factor_start":
                item["phase"] = "enabling_2fa"
                item["message"] = "正在创建 TOTP 2FA"
                self._append_log("OpenAI 注册成功，开始开启 2FA", email=email)
            elif kind == "two_factor_enrolled":
                two_factor = event.get("two_factor")
                if isinstance(two_factor, dict):
                    item["_two_factor"] = two_factor
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
                self._append_log("TOTP 2FA 已成功开启", email=email)
            elif kind == "error":
                item["_error"] = str(event.get("error") or "浏览器任务失败")
                password = str(event.get("password") or "")
                if password:
                    item["_password"] = password
                if event.get("password_confirmed") is not None:
                    item["_password_confirmed"] = bool(
                        event.get("password_confirmed")
                    )

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
            self._append_log(lines[-1][:500], email=email)

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
    "access_token_is_expired",
    "decode_jwt_payload",
    "load_account_record",
    "set_manual_account_type",
]
