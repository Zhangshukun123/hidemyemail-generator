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
CompleteInventoryEmail = Callable[[str, bool, str], Awaitable[None]]
AcquireProviderEmail = Callable[[str], Awaitable[str]]
PollProviderCode = Callable[[str], Awaitable[str]]
CompleteProviderEmail = Callable[[str, bool, str], Awaitable[None]]


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
        complete_email: CompleteInventoryEmail | None = None,
        acquire_provider_email: AcquireProviderEmail | None = None,
        poll_provider_code: PollProviderCode | None = None,
        complete_provider_email: CompleteProviderEmail | None = None,
    ) -> None:
        self.browser_manager = browser_manager
        self.acquire_email = acquire_email
        self.confirm_email = confirm_email
        self.save_password = save_password
        self.release_email = release_email
        self.complete_email = complete_email
        self.acquire_provider_email = acquire_provider_email
        self.poll_provider_code = poll_provider_code
        self.complete_provider_email = complete_provider_email
        self._task: asyncio.Task | None = None
        self._state: dict[str, Any] = self._idle_state()
        self._manual_codes: dict[str, str] = {}
        self._awaiting_code_emails: set[str] = set()

    @staticmethod
    def _idle_state() -> dict[str, Any]:
        return {
            "id": "",
            "status": "idle",
            "running": False,
            "phase": "idle",
            "provider": "manual",
            "email": "",
            "emails": [],
            "awaitingCode": False,
            "awaitingCodeEmails": [],
            "message": "尚未启动一键注册",
            "logs": [],
            "startedAt": "",
            "finishedAt": "",
        }

    def snapshot(self) -> dict[str, Any]:
        return {**self._state, "logs": list(self._state.get("logs", []))}

    def start(
        self,
        *,
        label: str,
        headless: bool,
        concurrency: int = 1,
        email: str = "",
        provider: str = "",
    ) -> dict[str, Any]:
        if self._task and not self._task.done():
            raise RuntimeError("一键注册任务正在运行")
        if self.browser_manager.snapshot().get("running"):
            raise RuntimeError("浏览器任务正在运行，请等待完成后再注册")
        runtime = self.browser_manager.availability()
        if not runtime.get("available"):
            raise RuntimeError("；".join(runtime.get("errors") or ["浏览器环境不可用"]))
        manual_email = str(email or "").strip().lower()
        provider = str(provider or ("manual" if manual_email else "inventory")).strip().lower()
        if provider not in {"manual", "inventory", "smsbower"}:
            raise ValueError("不支持的注册邮箱来源")
        if provider == "manual" and not manual_email:
            raise ValueError("手动注册必须提供邮箱地址")
        if provider == "smsbower" and self.acquire_provider_email is None:
            raise RuntimeError("SMSBower Gmail 获取服务未配置")
        concurrency = (
            1
            if manual_email or provider == "smsbower"
            else max(1, min(10, int(concurrency)))
        )
        reset_browser_state = getattr(self.browser_manager, "reset", None)
        if callable(reset_browser_state):
            reset_browser_state()
        self._manual_codes.clear()
        self._awaiting_code_emails.clear()
        self._state = {
            "id": uuid.uuid4().hex,
            "status": "running",
            "running": True,
            "phase": (
                "purchasing_gmail"
                if provider == "smsbower"
                else "preparing_email"
                if manual_email
                else "claiming_inventory"
            ),
            "provider": provider,
            "email": manual_email,
            "emails": [manual_email] if manual_email else [],
            "awaitingCode": False,
            "awaitingCodeEmails": [],
            "requested": concurrency,
            "effectiveConcurrency": 0,
            "claimed": 0,
            "message": (
                "正在通过 SMSBower API 获取 Gmail"
                if provider == "smsbower"
                else f"正在准备使用 {manual_email} 注册"
                if manual_email
                else f"正在从已生成邮箱库存领取 {concurrency} 个账号"
            ),
            "logs": [],
            "startedAt": utc_now(),
            "finishedAt": "",
        }
        if provider == "smsbower":
            self._append_log("正在向 SMSBower 购买 OpenAI Gmail 激活")
        elif manual_email:
            self._append_log(f"已添加注册邮箱：{manual_email}")
        else:
            self._append_log(f"开始并发注册：计划领取 {concurrency} 个库存邮箱")
        self._task = asyncio.create_task(
            self._run(
                label=label,
                headless=bool(headless),
                concurrency=concurrency,
                manual_email=manual_email,
                provider=provider,
            )
        )
        return self.snapshot()

    def _append_log(self, message: str) -> None:
        self._state.setdefault("logs", []).append(
            {"at": utc_now(), "message": str(message or "")[:1000]}
        )
        del self._state["logs"][:-100]

    def _validate_poll_target(self, email: str) -> str:
        target = str(email or "").strip().lower()
        active_emails = {
            str(item or "").strip().lower()
            for item in self._state.get("emails", [])
        }
        if not self._state.get("running") or target not in active_emails:
            raise RuntimeError("该邮箱当前没有正在运行的注册任务")
        return target

    def poll_verification_code(self, email: str) -> str:
        target = self._validate_poll_target(email)
        code = self._manual_codes.pop(target, "")
        if code:
            self._awaiting_code_emails.discard(target)
            self._sync_code_state()
            self._state.update(
                phase="registering_openai",
                message=f"验证码已提交给浏览器，继续注册 {target}",
            )
            self._append_log(f"已将手动验证码提交给注册浏览器：{target}")
            return code
        if target not in self._awaiting_code_emails:
            self._awaiting_code_emails.add(target)
            self._sync_code_state()
            self._state.update(
                phase="awaiting_verification_code",
                message=f"请在页面输入 {target} 收到的验证码",
            )
            self._append_log(f"注册浏览器正在等待手动输入验证码：{target}")
        return ""

    async def poll_verification_code_async(self, email: str) -> str:
        target = self._validate_poll_target(email)
        manual_code = self._manual_codes.pop(target, "")
        if manual_code:
            self._awaiting_code_emails.discard(target)
            self._sync_code_state()
            self._state.update(
                phase="registering_openai",
                message=f"验证码已提交给浏览器，继续注册 {target}",
            )
            self._append_log(f"已将手动验证码提交给注册浏览器：{target}")
            return manual_code
        if self._state.get("provider") != "smsbower":
            return self.poll_verification_code(target)
        if self.poll_provider_code is None:
            raise RuntimeError("SMSBower Gmail 验证码服务未配置")
        code = await self.poll_provider_code(target)
        if code:
            self._awaiting_code_emails.discard(target)
            self._sync_code_state()
            self._state.update(
                phase="registering_openai",
                message=f"SMSBower 已返回验证码，继续注册 {target}",
            )
            self._append_log(f"已从 SMSBower 自动取得 Gmail 验证码：{target}")
            return code
        if target not in self._awaiting_code_emails:
            self._awaiting_code_emails.add(target)
            self._sync_code_state()
            self._state.update(
                phase="awaiting_verification_code",
                message=f"正在等待 SMSBower 接收 {target} 的 Gmail 验证码",
            )
            self._append_log(f"正在通过 SMSBower API 轮询 Gmail 验证码：{target}")
        return ""

    def submit_verification_code(self, email: str, code: str) -> dict[str, Any]:
        target = str(email or "").strip().lower()
        normalized = "".join(character for character in str(code or "") if character.isalnum())
        active_emails = {
            str(item or "").strip().lower()
            for item in self._state.get("emails", [])
        }
        if not self._state.get("running") or target not in active_emails:
            raise RuntimeError("该邮箱当前没有正在运行的注册任务")
        if not 4 <= len(normalized) <= 10:
            raise ValueError("验证码必须为 4–10 位字母或数字")
        self._manual_codes[target] = normalized
        self._awaiting_code_emails.add(target)
        self._sync_code_state()
        self._state.update(
            phase="awaiting_verification_code",
            message=f"已收到验证码，正在交给注册浏览器：{target}",
        )
        self._append_log(f"已手动提交验证码：{target}")
        return self.snapshot()

    def _sync_code_state(self) -> None:
        waiting = sorted(self._awaiting_code_emails)
        self._state["awaitingCode"] = bool(waiting)
        self._state["awaitingCodeEmails"] = waiting

    async def _run(
        self,
        *,
        label: str,
        headless: bool,
        concurrency: int,
        manual_email: str = "",
        provider: str = "inventory",
    ) -> None:
        claimed_emails: list[str] = []
        successful_emails: set[str] = set()
        try:
            if provider == "smsbower":
                email = str(await self.acquire_provider_email(label)).strip().lower()
                if not email.endswith("@gmail.com"):
                    raise RuntimeError("SMSBower 未返回有效的 Gmail 地址")
                claimed_emails.append(email)
                self._state.update(
                    email=email,
                    emails=[email],
                    claimed=1,
                    message=f"SMSBower Gmail 已获取，正在准备注册：{email}",
                )
                self._append_log(f"已通过 SMSBower API 获取 Gmail：{email}")
            elif manual_email:
                claimed_emails.append(manual_email)
                self._state.update(
                    email=manual_email,
                    emails=[manual_email],
                    claimed=1,
                    message=f"正在准备注册邮箱：{manual_email}",
                )
            else:
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
            effective_concurrency = len(claimed_emails)
            if effective_concurrency == 0:
                raise RuntimeError(
                    f"邮箱库存不足：需要 {concurrency} 个，当前可领取 0 个；未启动浏览器"
                )
            self._state["effectiveConcurrency"] = effective_concurrency
            if effective_concurrency < concurrency:
                self._state["message"] = (
                    f"邮箱库存不足：期望 {concurrency} 个，实际领取 "
                    f"{effective_concurrency} 个；自动按最小可用数量继续注册"
                )
                self._append_log(
                    f"库存不足，已将本次并发从 {concurrency} 自动降为 "
                    f"{effective_concurrency}"
                )
            self._state.update(
                phase="confirming_email",
                message=(
                    (
                        f"SMSBower Gmail 已获取，正在准备 OpenAI 注册：{claimed_emails[0]}"
                        if provider == "smsbower"
                        else (
                            f"自有邮箱已就绪，正在准备 OpenAI 注册：{claimed_emails[0]}；"
                            "验证码将在浏览器中手动输入"
                            if provider == "manual"
                            else f"已领取 {effective_concurrency} 个库存邮箱，正在准备注册"
                        )
                    )
                    + (
                        f"（原计划 {concurrency} 个，已自动降并发）"
                        if effective_concurrency < concurrency
                        else ""
                    )
                ),
            )
            accounts: list[dict[str, Any]] = []
            for index, email in enumerate(claimed_emails, start=1):
                if provider == "inventory":
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
                        "manual_otp_entry": provider == "manual",
                        "password_first_required": provider == "smsbower",
                        "foreground_required": provider in {"manual", "smsbower"},
                    }
                )
                self._append_log(
                    f"邮箱已添加并保存唯一密码（{index}/{effective_concurrency}）：{email}"
                )
            self._state.update(
                phase="registering_openai",
                message=(
                    f"正在启动 {effective_concurrency} 个 Camoufox 注册浏览器"
                    + (
                        "；验证码请直接在浏览器中手动输入并点击继续"
                        if provider == "manual"
                        else ""
                    )
                    + (
                        f"（库存不足，已从 {concurrency} 降为 {effective_concurrency}）"
                        if effective_concurrency < concurrency
                        else ""
                    )
                ),
            )
            self._append_log(
                f"{effective_concurrency} 个邮箱已准备，按并发 "
                f"{effective_concurrency} 启动 Camoufox"
                + (
                    "；自有邮箱不连接 IMAP，验证码由你在浏览器中手动输入"
                    if provider == "manual"
                    else ""
                )
            )
            self.browser_manager.start(
                accounts,
                headless=False if provider in {"manual", "smsbower"} else headless,
                concurrency=effective_concurrency,
                use_registration_proxy=True,
            )
            browser_wait = asyncio.create_task(self.browser_manager.wait())
            while not browser_wait.done():
                await asyncio.sleep(0.4)
            browser_result = await browser_wait
            succeeded = max(0, int(browser_result.get("succeeded") or 0))
            result_accounts = browser_result.get("accounts") or []
            successful_emails = {
                str(item.get("email") or "").strip().lower()
                for item in result_accounts
                if item.get("status") == "success" and item.get("email")
            }
            if (
                succeeded == effective_concurrency
                and len(successful_emails) != effective_concurrency
            ):
                successful_emails = set(claimed_emails)
            if succeeded == effective_concurrency:
                password_confirmed = sum(
                    1 for item in result_accounts if item.get("passwordConfirmed")
                )
                two_factor_enabled = sum(
                    1 for item in result_accounts if item.get("twoFactorEnabled")
                )
                detail = (
                    f"并发注册完成：成功 {succeeded}/{effective_concurrency}；"
                    f"密码已设置 {password_confirmed}/{effective_concurrency}；"
                    f"2FA 已开启 {two_factor_enabled}/{effective_concurrency}；"
                    f"Session / Cookie 已保存"
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
                failed_items = [
                    item for item in result_accounts if item.get("status") != "success"
                ]
                detail = str((failed_items[0] if failed_items else {}).get("message") or "")
                raise RuntimeError(
                    f"并发注册未全部成功：成功 {succeeded}/{effective_concurrency}"
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
            finalization_emails = (
                claimed_emails if provider in {"inventory", "smsbower"} else []
            )
            for email in finalization_emails:
                try:
                    if provider == "smsbower" and self.complete_provider_email is not None:
                        success = email in successful_emails
                        await self.complete_provider_email(
                            email,
                            success,
                            str(self._state.get("message") or "OpenAI 注册失败"),
                        )
                        self._append_log(
                            f"已向 SMSBower 回执 Gmail 激活状态（{email}）："
                            f"{'注册成功' if success else '注册未完成'}"
                        )
                    elif provider == "inventory" and self.complete_email is not None:
                        success = email in successful_emails
                        await self.complete_email(
                            email,
                            success,
                            (
                                "OpenAI 注册成功"
                                if success
                                else str(self._state.get("message") or "OpenAI 注册失败")
                            ),
                        )
                        self._append_log(
                            f"已向远端库存回执（{email}）："
                            f"{'注册成功，标记已使用' if success else '注册失败，已永久隔离'}"
                        )
                    elif provider == "inventory" and self.release_email is not None:
                        await self.release_email(email)
                except Exception as error:
                    self._append_log(
                        f"提交{' SMSBower 激活' if provider == 'smsbower' else '库存'}回执失败"
                        f"（{email}）：{str(error)[:300]}"
                    )
            self._state["running"] = False
            self._manual_codes.clear()
            self._awaiting_code_emails.clear()
            self._sync_code_state()
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


__all__ = [
    "CompleteInventoryEmail",
    "RegistrationTaskManager",
    "generate_openai_password",
]
