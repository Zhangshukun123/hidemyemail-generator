from __future__ import annotations

import asyncio
import secrets
import string
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .browser_tasks import BrowserTaskManager


GenerateEmail = Callable[[str], Awaitable[str]]
ConfirmEmail = Callable[[str], Awaitable[None]]
SavePassword = Callable[[str, str], Awaitable[None]]


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
        generate_email: GenerateEmail,
        confirm_email: ConfirmEmail,
        save_password: SavePassword | None = None,
    ) -> None:
        self.browser_manager = browser_manager
        self.generate_email = generate_email
        self.confirm_email = confirm_email
        self.save_password = save_password
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

    def start(self, *, label: str, headless: bool) -> dict[str, Any]:
        if self._task and not self._task.done():
            raise RuntimeError("一键注册任务正在运行")
        if self.browser_manager.snapshot().get("running"):
            raise RuntimeError("浏览器任务正在运行，请等待完成后再注册")
        runtime = self.browser_manager.availability()
        if not runtime.get("available"):
            raise RuntimeError("；".join(runtime.get("errors") or ["浏览器环境不可用"]))
        reset_browser_state = getattr(self.browser_manager, "reset", None)
        if callable(reset_browser_state):
            reset_browser_state()
        self._state = {
            "id": uuid.uuid4().hex,
            "status": "running",
            "running": True,
            "phase": "generating_email",
            "email": "",
            "message": "正在生成 iCloud 隐藏邮箱",
            "logs": [],
            "startedAt": utc_now(),
            "finishedAt": "",
        }
        self._append_log("开始一键注册")
        self._task = asyncio.create_task(
            self._run(label=label, headless=bool(headless))
        )
        return self.snapshot()

    def _append_log(self, message: str) -> None:
        self._state.setdefault("logs", []).append(
            {"at": utc_now(), "message": str(message or "")[:1000]}
        )
        del self._state["logs"][:-100]

    async def _run(self, *, label: str, headless: bool) -> None:
        try:
            email = (await self.generate_email(label)).strip().lower()
            if not email:
                raise RuntimeError("iCloud 未返回新邮箱地址")
            self._state.update(
                email=email,
                phase="confirming_email",
                message="邮箱已生成，正在等待 iCloud 列表同步",
            )
            self._append_log(f"已生成邮箱：{email}")
            await self.confirm_email(email)
            password = generate_openai_password()
            if self.save_password is not None:
                await self.save_password(email, password)
            self._append_log("已生成并保存唯一密码")
            self._state.update(
                phase="registering_openai",
                message="邮箱和密码已保存，正在选择使用密码继续注册 OpenAI",
            )
            self._append_log("iCloud 邮箱已确认，启动 Camoufox")
            self.browser_manager.start(
                [
                    {
                        "email": email,
                        "password": password,
                        "ensure_password": True,
                        "force_reset_password": False,
                        "enable_2fa": True,
                    }
                ],
                headless=headless,
                concurrency=1,
            )
            browser_wait = asyncio.create_task(self.browser_manager.wait())
            while not browser_wait.done():
                await asyncio.sleep(0.4)
            browser_result = await browser_wait
            if browser_result.get("succeeded") == 1:
                accounts = browser_result.get("accounts") or []
                account = accounts[0] if accounts else {}
                password_confirmed = bool(
                    account.get("passwordConfirmed")
                )
                two_factor_enabled = bool(account.get("twoFactorEnabled"))
                detail = [
                    "注册成功，Session 已保存",
                    (
                        "唯一密码已设置"
                        if password_confirmed
                        else "唯一密码已保存，自动设置待完成"
                    ),
                    (
                        "2FA 已开启"
                        if two_factor_enabled
                        else "2FA 未开启"
                        if password_confirmed
                        else "已跳过 2FA（密码未成功）"
                    ),
                    "未执行账号验证",
                ]
                self._state.update(
                    status="completed",
                    phase="completed",
                    message="；".join(detail),
                )
                self._append_log("；".join(detail))
            elif browser_result.get("status") in {"cancelled", "cancelling"}:
                self._state.update(
                    status="cancelled",
                    phase="cancelled",
                    message="一键注册已停止",
                )
                self._append_log("一键注册已停止")
            else:
                accounts = browser_result.get("accounts") or []
                detail = str((accounts[0] if accounts else {}).get("message") or "")
                raise RuntimeError(detail or "OpenAI 浏览器注册失败")
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
