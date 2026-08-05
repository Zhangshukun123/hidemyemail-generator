from __future__ import annotations

import asyncio
import secrets
import string
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .browser_tasks import BrowserTaskManager


AcquireInventoryEmail = Callable[[str], Awaitable[str]]
ConfirmEmail = Callable[[str], Awaitable[None]]
SavePassword = Callable[[str, str], Awaitable[None]]
ReleaseInventoryEmail = Callable[[str], Awaitable[None]]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_openai_password(length: int = 20) -> str:
    length = max(16, int(length))
    characters = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*_-+="),
    ]
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*_-+="
    characters.extend(
        secrets.choice(alphabet) for _ in range(length - len(characters))
    )
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


class RegistrationTaskManager:
    def __init__(
        self,
        *,
        browser_manager: BrowserTaskManager,
        acquire_email: AcquireInventoryEmail,
        confirm_email: ConfirmEmail,
        save_password: SavePassword | None = None,
        release_email: ReleaseInventoryEmail | None = None,
    ) -> None:
        self.browser_manager = browser_manager
        self.acquire_email = acquire_email
        self.confirm_email = confirm_email
        self.save_password = save_password
        self.release_email = release_email
        self._task: asyncio.Task | None = None
        self._state: dict[str, Any] = self._idle_state()

    @staticmethod
    def _idle_state() -> dict[str, Any]:
        return {
            "id": "",
            "status": "idle",
            "running": False,
            "phase": "idle",
            "email": "",
            "message": "尚未启动一键注册",
            "logs": [],
            "startedAt": "",
            "finishedAt": "",
        }

    def snapshot(self) -> dict[str, Any]:
        return {**self._state, "logs": list(self._state.get("logs", []))}

    def start(
        self, *, label: str, headless: bool, concurrency: int = 1
    ) -> dict[str, Any]:
        if self._task and not self._task.done():
            raise RuntimeError("一键注册任务正在运行")
        if self.browser_manager.snapshot().get("running"):
            raise RuntimeError("浏览器任务正在运行，请等待完成后再注册")
        runtime = self.browser_manager.availability()
        if not runtime.get("available"):
            raise RuntimeError("；".join(runtime.get("errors") or ["浏览器环境不可用"]))
        concurrency = max(1, min(10, int(concurrency)))
        reset_browser_state = getattr(self.browser_manager, "reset", None)
        if callable(reset_browser_state):
            reset_browser_state()
        self._state = {
            "id": uuid.uuid4().hex,
            "status": "running",
            "running": True,
            "phase": "claiming_inventory",
            "email": "",
            "emails": [],
            "requested": concurrency,
            "claimed": 0,
            "message": f"正在从已生成邮箱库存领取 {concurrency} 个账号",
            "logs": [],
            "startedAt": utc_now(),
            "finishedAt": "",
        }
        self._append_log(f"开始并发注册：计划领取 {concurrency} 个库存邮箱")
        self._task = asyncio.create_task(
            self._run(
                label=label,
                headless=bool(headless),
                concurrency=concurrency,
            )
        )
        return self.snapshot()

    def _append_log(self, message: str) -> None:
        self._state.setdefault("logs", []).append(
            {"at": utc_now(), "message": str(message or "")[:1000]}
        )
        del self._state["logs"][:-100]

    async def _run(
        self, *, label: str, headless: bool, concurrency: int
    ) -> None:
        claimed_emails: list[str] = []
        try:
            for _index in range(concurrency):
                email = (await self.acquire_email(label)).strip().lower()
                if not email:
                    break
                claimed_emails.append(email)
                self._state.update(
                    email=claimed_emails[0],
                    emails=list(claimed_emails),
                    claimed=len(claimed_emails),
                    message=(
                        f"正在领取库存邮箱：{len(claimed_emails)}/{concurrency}"
                    ),
                )
                self._append_log(
                    f"已从生成库存领取邮箱（{len(claimed_emails)}/{concurrency}）：{email}"
                )
            if len(claimed_emails) != concurrency:
                raise RuntimeError(
                    f"邮箱库存不足：需要 {concurrency} 个，当前可领取 "
                    f"{len(claimed_emails)} 个；未启动浏览器"
                )
            self._state.update(
                phase="confirming_email",
                message=f"已领取 {concurrency} 个库存邮箱，正在确认 iCloud 列表状态",
            )
            accounts: list[dict[str, Any]] = []
            for index, email in enumerate(claimed_emails, start=1):
                await self.confirm_email(email)
                password = generate_openai_password()
                if self.save_password is not None:
                    await self.save_password(email, password)
                accounts.append(
                    {
                        "email": email,
                        "password": password,
                        "ensure_password": True,
                        "force_reset_password": False,
                        "enable_2fa": True,
                    }
                )
                self._append_log(
                    f"邮箱已确认并保存唯一密码（{index}/{concurrency}）：{email}"
                )
            self._state.update(
                phase="registering_openai",
                message=f"正在同时启动 {concurrency} 个 Camoufox 注册浏览器",
            )
            self._append_log(
                f"{concurrency} 个 iCloud 邮箱已确认，按并发 {concurrency} 启动 Camoufox"
            )
            self.browser_manager.start(
                accounts,
                headless=headless,
                concurrency=concurrency,
                use_registration_proxy=True,
            )
            browser_wait = asyncio.create_task(self.browser_manager.wait())
            while not browser_wait.done():
                await asyncio.sleep(0.4)
            browser_result = await browser_wait
            succeeded = max(0, int(browser_result.get("succeeded") or 0))
            if succeeded == concurrency:
                result_accounts = browser_result.get("accounts") or []
                password_confirmed = sum(
                    1 for item in result_accounts if item.get("passwordConfirmed")
                )
                two_factor_enabled = sum(
                    1 for item in result_accounts if item.get("twoFactorEnabled")
                )
                detail = (
                    f"并发注册完成：成功 {succeeded}/{concurrency}；"
                    f"密码已设置 {password_confirmed}/{concurrency}；"
                    f"2FA 已开启 {two_factor_enabled}/{concurrency}；Session 已保存"
                )
                self._state.update(
                    status="completed",
                    phase="completed",
                    message=detail,
                )
                self._append_log(detail)
            elif browser_result.get("status") in {"cancelled", "cancelling"}:
                self._state.update(
                    status="cancelled",
                    phase="cancelled",
                    message="一键注册已停止",
                )
                self._append_log("一键注册已停止")
            else:
                result_accounts = browser_result.get("accounts") or []
                failed_items = [
                    item for item in result_accounts if item.get("status") != "success"
                ]
                detail = str((failed_items[0] if failed_items else {}).get("message") or "")
                raise RuntimeError(
                    f"并发注册未全部成功：成功 {succeeded}/{concurrency}"
                    + (f"；{detail}" if detail else "")
                )
        except asyncio.CancelledError:
            self._state.update(
                status="cancelled", phase="cancelled", message="一键注册已停止"
            )
            self._append_log("一键注册已停止")
            raise
        except Exception as error:
            self._state.update(
                status="failed",
                phase="failed",
                message=str(error or "一键注册失败")[:500],
            )
            self._append_log(f"失败：{self._state['message']}")
        finally:
            if self.release_email is not None:
                for email in claimed_emails:
                    try:
                        await self.release_email(email)
                    except Exception as error:
                        self._append_log(
                            f"释放库存领取锁失败（{email}）：{str(error)[:300]}"
                        )
            self._state["running"] = False
            self._state["finishedAt"] = utc_now()

    async def stop(self) -> dict[str, Any]:
        if not self._task or self._task.done():
            return self.snapshot()
        self._state.update(
            status="cancelling", phase="cancelling", message="正在停止一键注册"
        )
        if self.browser_manager.snapshot().get("running"):
            await self.browser_manager.stop()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        return self.snapshot()

    async def close(self) -> None:
        await self.stop()


__all__ = ["RegistrationTaskManager", "generate_openai_password"]
