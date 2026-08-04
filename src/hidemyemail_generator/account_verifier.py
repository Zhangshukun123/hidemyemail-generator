from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .browser_tasks import (
    account_session,
    account_session_access_token,
    load_account_record,
    session_account_type,
    session_email,
)
from .inbox import connect_db


EVENT_PREFIX = "HME_VERIFY_EVENT:"
MAX_LOG_ITEMS = 300


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_verifiable_accounts(db_file: Path) -> list[dict[str, str]]:
    conn = connect_db(str(db_file))
    try:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'gpt_account:%'"
        ).fetchall()
    finally:
        conn.close()
    accounts: list[dict[str, str]] = []
    for row in rows:
        email = str(row["key"] or "").removeprefix("gpt_account:").strip().lower()
        try:
            record = json.loads(str(row["value"] or ""))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        session = account_session(record)
        token = account_session_access_token(record)
        if email and token:
            account_type = str(record.get("account_type") or "").strip().lower()
            account_type_source = str(
                record.get("account_type_source") or ""
            ).strip().lower()
            session_type, raw_session_plan = session_account_type(session)
            if session_type and account_type_source != "manual":
                account_type = session_type
                account_type_source = "session"
            accounts.append(
                {
                    "email": email,
                    "access_token": token,
                    "account_type": account_type,
                    "account_type_source": account_type_source,
                    "session_email": session_email(session),
                    "session_plan": raw_session_plan,
                }
            )
    return sorted(accounts, key=lambda item: item["email"])


def removed_account_emails(db_file: Path) -> set[str]:
    conn = connect_db(str(db_file))
    try:
        rows = conn.execute(
            "SELECT key FROM settings WHERE key LIKE 'gpt_removed:%'"
        ).fetchall()
    finally:
        conn.close()
    return {
        str(row["key"] or "").removeprefix("gpt_removed:").strip().lower()
        for row in rows
    }


def save_account_classification(
    db_file: Path, email: str, account_type: str, detail: str
) -> None:
    target = email.strip().lower()
    record = load_account_record(db_file, target)
    if not record:
        return
    if record.get("account_type_source") == "manual":
        record.update(
            {
                "verified_at": utc_now(),
                "verification_detail": (
                    f"{str(detail or f'自动验证结果为 {account_type.title()}').strip()}；"
                    "已保留手动设置的账号类型"
                )[:1000],
            }
        )
    else:
        record.update(
            {
                "account_type": account_type,
                "account_type_source": "verification",
                "verified_at": utc_now(),
                "verification_detail": str(detail or "")[:1000],
            }
        )
    conn = connect_db(str(db_file))
    try:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (f"gpt_account:{target}", json.dumps(record, ensure_ascii=False)),
        )
        conn.execute("DELETE FROM settings WHERE key = ?", (f"gpt_removed:{target}",))
        conn.commit()
    finally:
        conn.close()


def mark_account_session_invalid(db_file: Path, email: str, detail: str) -> None:
    target = email.strip().lower()
    record = load_account_record(db_file, target)
    if not record:
        return
    for key in (
        "access_token",
        "accessToken",
        "session",
        "session_json",
        "sessionJson",
        "storage_state_json",
        "storageStateJson",
    ):
        record.pop(key, None)
    record.update(
        {
            "session_invalid_at": utc_now(),
            "verification_detail": str(detail or "Access Token 已失效")[:1000],
        }
    )
    conn = connect_db(str(db_file))
    try:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (f"gpt_account:{target}", json.dumps(record, ensure_ascii=False)),
        )
        conn.execute("DELETE FROM settings WHERE key = ?", (f"gpt_removed:{target}",))
        conn.commit()
    finally:
        conn.close()


def remove_invalid_account(db_file: Path, email: str, detail: str) -> None:
    target = email.strip().lower()
    audit = {
        "email": target,
        "removed_at": utc_now(),
        "reason": str(detail or "Access Token 已失效")[:1000],
    }
    conn = connect_db(str(db_file))
    try:
        existing = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (f"gpt_account:{target}",)
        ).fetchone()
        if existing:
            try:
                account_record = json.loads(str(existing["value"] or ""))
            except (json.JSONDecodeError, TypeError, ValueError):
                account_record = None
            if isinstance(account_record, dict):
                audit["account_record"] = account_record
        conn.execute("DELETE FROM settings WHERE key = ?", (f"gpt_account:{target}",))
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (f"gpt_removed:{target}", json.dumps(audit, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


class AccountVerificationManager:
    def __init__(
        self,
        *,
        target_project_dir: Path,
        db_file: Path,
        python_executable: Path,
        bridge_file: Path | None = None,
    ) -> None:
        self.target_project_dir = target_project_dir.resolve()
        self.db_file = db_file.resolve()
        self.python_executable = python_executable.resolve()
        self.bridge_file = (
            bridge_file or Path(__file__).with_name("openai_account_check_bridge.py")
        ).resolve()
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
            "plus": 0,
            "free": 0,
            "expired": 0,
            "deleted": 0,
            "failed": 0,
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
            missing.append("当前项目缺少账号验证桥接脚本")
        return {"available": not missing, "errors": missing}

    def snapshot(self) -> dict[str, Any]:
        return {
            **{key: value for key, value in self._state.items() if key != "accounts"},
            "accounts": [
                {key: value for key, value in item.items() if not key.startswith("_")}
                for item in self._state.get("accounts", [])
            ],
            "runtime": self.availability(),
        }

    def start(
        self,
        *,
        concurrency: int = 3,
        emails: list[str] | set[str] | None = None,
    ) -> dict[str, Any]:
        if self._batch_task and not self._batch_task.done():
            raise RuntimeError("账号验证任务正在运行")
        runtime = self.availability()
        if not runtime["available"]:
            raise RuntimeError("；".join(runtime["errors"]))
        accounts = load_verifiable_accounts(self.db_file)
        requested: set[str] | None = None
        if emails is not None:
            requested = {
                str(value or "").strip().lower()
                for value in emails
                if str(value or "").strip()
            }
            accounts = [
                account for account in accounts if account["email"] in requested
            ]
        if not accounts:
            message = (
                "所选账号没有可验证的 Session"
                if requested is not None
                else "暂无已保存 Session 的账号"
            )
            raise RuntimeError(message)
        concurrency = max(1, min(5, int(concurrency)))
        self._state = {
            "id": uuid.uuid4().hex,
            "status": "running",
            "running": True,
            "concurrency": concurrency,
            "total": len(accounts),
            "completed": 0,
            "plus": 0,
            "free": 0,
            "expired": 0,
            "deleted": 0,
            "failed": 0,
            "accounts": [
                {
                    "email": item["email"],
                    "status": "queued",
                    "message": "等待验证",
                    "_access_token": item["access_token"],
                    "_account_type": item.get("account_type", ""),
                    "_account_type_source": item.get("account_type_source", ""),
                    "_session_email": item.get("session_email", ""),
                    "_session_plan": item.get("session_plan", ""),
                }
                for item in accounts
            ],
            "logs": [],
            "startedAt": utc_now(),
            "finishedAt": "",
        }
        self._append_log(f"一键验证已启动：{len(accounts)} 个账号，并发 {concurrency}")
        self._batch_task = asyncio.create_task(self._run_batch(concurrency))
        return self.snapshot()

    def _append_log(self, message: str, *, email: str = "") -> None:
        entry = {"at": utc_now(), "email": email, "message": str(message)[:1000]}
        logs = self._state.setdefault("logs", [])
        logs.append(entry)
        if len(logs) > MAX_LOG_ITEMS:
            del logs[:-MAX_LOG_ITEMS]

    async def _run_batch(self, concurrency: int) -> None:
        semaphore = asyncio.Semaphore(concurrency)
        try:
            await asyncio.gather(
                *(self._run_account(item, semaphore) for item in self._state["accounts"])
            )
            if self._state["status"] == "cancelling":
                self._state["status"] = "cancelled"
                self._append_log("账号验证任务已停止")
            else:
                self._state["status"] = "completed"
                self._append_log(
                    f"验证完成：Plus {self._state['plus']}，Free {self._state['free']}，"
                    f"Token 失效 {self._state['expired']}，失败 {self._state['failed']}"
                )
        except asyncio.CancelledError:
            self._state["status"] = "cancelled"
            self._append_log("账号验证任务已停止")
            raise
        finally:
            self._state["running"] = False
            self._state["finishedAt"] = utc_now()
            self._processes.clear()

    async def _run_account(
        self, item: dict[str, Any], semaphore: asyncio.Semaphore
    ) -> None:
        email = str(item["email"])
        async with semaphore:
            if self._state.get("status") == "cancelling":
                item.update(status="cancelled", message="任务已停止")
                return
            session_owner = str(item.get("_session_email") or "").strip().lower()
            if session_owner and session_owner != email:
                item.update(
                    status="failed",
                    message=f"Session 账号不匹配：{session_owner}",
                )
                self._state["failed"] += 1
                self._state["completed"] += 1
                self._append_log(item["message"], email=email)
                for key in tuple(item):
                    if key.startswith("_"):
                        item.pop(key, None)
                return
            item.update(status="running", message="正在根据 Session 检查账号")
            self._append_log("正在检查 Session", email=email)
            token = str(item.get("_access_token") or "")
            env = os.environ.copy()
            env.update(
                {
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "HME_OPENAI_ACCESS_TOKEN": token,
                }
            )
            command = [
                str(self.python_executable),
                str(self.bridge_file),
                "--source-dir",
                str(self.target_project_dir),
            ]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(self.target_project_dir),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=creationflags,
                    limit=1024 * 1024,
                )
                self._processes[email] = process
                stdout, stderr = await process.communicate()
                return_code = process.returncode
            except asyncio.CancelledError:
                item.update(status="cancelled", message="任务已停止")
                raise
            except Exception as error:
                stdout, stderr, return_code = b"", str(error).encode(), -1
            finally:
                self._processes.pop(email, None)

            event: dict[str, Any] = {}
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                if line.startswith(EVENT_PREFIX):
                    try:
                        candidate = json.loads(line[len(EVENT_PREFIX) :])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(candidate, dict):
                        event = candidate
            result = str(event.get("status") or "error")
            detail = str(event.get("detail") or "").strip()
            session_detail = ""
            if session_owner:
                session_detail = f"Session user.email={session_owner}"
            session_plan = str(item.get("_session_plan") or "").strip()
            if session_plan:
                session_detail = (
                    f"{session_detail}，" if session_detail else ""
                ) + f"account.planType={session_plan}"
            if session_detail:
                detail = f"{session_detail}；{detail}" if detail else session_detail
            if return_code == 0 and result in {"plus", "free"}:
                effective_result = result
                if (
                    item.get("_account_type_source") == "manual"
                    and item.get("_account_type") in {"plus", "free"}
                ):
                    effective_result = str(item["_account_type"])
                    detail = (
                        f"自动验证结果为 {result.title()}；"
                        "已保留手动设置的账号类型"
                    )
                elif result == "free" and item.get("_account_type") == "plus":
                    effective_result = "plus"
                    detail = (
                        "套餐接口返回 Free，但已保存的最新登录 Session 明确为 Plus；"
                        f"已保留 Plus 分类。{detail}"
                    )
                await asyncio.to_thread(
                    save_account_classification,
                    self.db_file,
                    email,
                    effective_result,
                    detail,
                )
                if item.get("_account_type_source") == "manual":
                    item.update(
                        status=effective_result,
                        message=f"已保留手动设置的 {effective_result.title()}",
                    )
                elif effective_result != result:
                    item.update(status="plus", message="已保留 Plus（套餐接口返回 Free）")
                else:
                    item.update(status=result, message=f"已归类为 {result.title()}")
                self._state[effective_result] += 1
                self._append_log(item["message"], email=email)
            elif return_code == 0 and result == "invalid":
                await asyncio.to_thread(
                    mark_account_session_invalid, self.db_file, email, detail
                )
                if item.get("_account_type") == "plus":
                    item.update(
                        status="plus",
                        message="Plus 账号 Token 已失效，账号已保留，请重新获取 Session",
                    )
                    self._state["plus"] += 1
                else:
                    item.update(
                        status="expired",
                        message="Token 已失效，账号凭据已保留，请重新获取 Session",
                    )
                    self._state["expired"] += 1
                self._append_log(item["message"], email=email)
            else:
                error = detail or stderr.decode("utf-8", errors="replace").strip()
                if token:
                    error = error.replace(token, "[REDACTED]")
                item.update(status="failed", message=(error or "账号验证失败")[:500])
                self._state["failed"] += 1
                self._append_log(f"验证失败，账号已保留：{item['message']}", email=email)
            self._state["completed"] += 1
            item.pop("_access_token", None)
            item.pop("_account_type", None)
            item.pop("_account_type_source", None)
            item.pop("_session_email", None)
            item.pop("_session_plan", None)

    async def stop(self) -> dict[str, Any]:
        if not self._batch_task or self._batch_task.done():
            return self.snapshot()
        self._state["status"] = "cancelling"
        processes = list(self._processes.values())
        for process in processes:
            if process.returncode is None:
                process.terminate()
        if processes:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(process.wait() for process in processes)),
                    timeout=5,
                )
            except asyncio.TimeoutError:
                for process in processes:
                    if process.returncode is None:
                        process.kill()
        self._batch_task.cancel()
        try:
            await self._batch_task
        except asyncio.CancelledError:
            pass
        return self.snapshot()

    async def close(self) -> None:
        await self.stop()


__all__ = [
    "AccountVerificationManager",
    "load_verifiable_accounts",
    "mark_account_session_invalid",
    "remove_invalid_account",
    "removed_account_emails",
    "save_account_classification",
]
