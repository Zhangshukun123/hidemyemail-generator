from __future__ import annotations

import asyncio
import inspect
import secrets
import string
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .browser_tasks import BrowserTaskManager, browser_log_context


AcquireInventoryEmail = Callable[[str], Awaitable[str]]
ConfirmEmail = Callable[[str], Awaitable[None]]
SavePassword = Callable[[str, str], Awaitable[None]]
ReleaseInventoryEmail = Callable[[str], Awaitable[None]]
CompleteInventoryEmail = Callable[[str, bool, str], Awaitable[None]]
AcquireProviderEmail = Callable[[str], Awaitable[str]]
PollProviderCode = Callable[[str], Awaitable[str]]
PollProviderNextCode = Callable[[str], Awaitable[str]]
CompleteProviderEmail = Callable[[str, bool, str], Awaitable[None]]
CancelProviderEmail = Callable[[str, str], Awaitable[None]]
LoadAccount = Callable[[str], Awaitable[dict[str, Any]]]
RecordProcessFailure = Callable[[dict[str, Any]], Awaitable[None] | None]


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
        poll_provider_next_code: PollProviderNextCode | None = None,
        complete_provider_email: CompleteProviderEmail | None = None,
        cancel_provider_email: CancelProviderEmail | None = None,
        load_account: LoadAccount | None = None,
        provider_code_timeout_seconds: float = 30.0,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.browser_manager = browser_manager
        self.acquire_email = acquire_email
        self.confirm_email = confirm_email
        self.save_password = save_password
        self.release_email = release_email
        self.complete_email = complete_email
        self.acquire_provider_email = acquire_provider_email
        self.poll_provider_code = poll_provider_code
        self.poll_provider_next_code = poll_provider_next_code
        self.complete_provider_email = complete_provider_email
        self.cancel_provider_email = cancel_provider_email
        self.load_account = load_account
        self.provider_code_timeout_seconds = max(
            1.0, float(provider_code_timeout_seconds)
        )
        self._monotonic = monotonic or time.monotonic
        self._task: asyncio.Task | None = None
        self._state: dict[str, Any] = self._idle_state()
        self._manual_codes: dict[str, str] = {}
        self._awaiting_code_emails: set[str] = set()
        self._provider_code_request_ids: dict[str, list[str]] = {}
        self._provider_code_cache: dict[tuple[str, str], str] = {}
        self._provider_code_started_at: dict[tuple[str, str], float] = {}
        self._provider_cancelled_emails: set[str] = set()
        self._last_relayed_browser_log_key: tuple[str, str, str] | None = None

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
            "currentStage": "idle",
            "currentLocation": "等待任务",
            "currentAction": "尚未开始",
            "currentStatus": "idle",
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
        target_count: int | None = None,
        email: str = "",
        provider: str = "",
        browser_engine: str = "camoufox",
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
        selected_browser_engine = str(browser_engine or "camoufox").strip().lower()
        if selected_browser_engine not in {"camoufox", "roxy"}:
            raise ValueError("不支持的注册浏览器引擎")
        concurrency = (
            1
            if manual_email or provider == "smsbower"
            else max(
                1,
                min(5 if selected_browser_engine == "roxy" else 10, int(concurrency)),
            )
        )
        requested_target_count = (
            concurrency if target_count is None else int(target_count)
        )
        target_count = (
            1
            if manual_email or provider == "smsbower"
            else max(1, min(100, requested_target_count))
            if selected_browser_engine == "roxy"
            else concurrency
        )
        reset_browser_state = getattr(self.browser_manager, "reset", None)
        if callable(reset_browser_state):
            reset_browser_state()
        self._manual_codes.clear()
        self._awaiting_code_emails.clear()
        self._provider_code_request_ids.clear()
        self._provider_code_cache.clear()
        self._provider_code_started_at.clear()
        self._provider_cancelled_emails.clear()
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
            "browserEngine": selected_browser_engine,
            "email": manual_email,
            "emails": [manual_email] if manual_email else [],
            "awaitingCode": False,
            "awaitingCodeEmails": [],
            "concurrency": concurrency,
            "targetCount": target_count,
            "requested": target_count,
            "effectiveConcurrency": 0,
            "claimed": 0,
            "message": (
                "正在通过 SMSBower API 获取 Gmail"
                if provider == "smsbower"
                else f"正在准备使用 {manual_email} 注册"
                if manual_email
                else f"正在从已生成邮箱库存领取 {target_count} 个账号"
            ),
            "logs": [],
            "currentStage": "prepare",
            "currentLocation": "注册准备",
            "currentAction": "准备注册邮箱",
            "currentStatus": "active",
            "startedAt": utc_now(),
            "finishedAt": "",
        }
        if provider == "smsbower":
            self._append_log("正在向 SMSBower 购买 OpenAI Gmail 激活")
        elif manual_email:
            self._append_log(f"已添加注册邮箱：{manual_email}")
        else:
            self._append_log(
                f"开始 Roxy 目标注册：并发 {concurrency} 个窗口，"
                f"目标 {target_count} 个账号"
                if selected_browser_engine == "roxy"
                else f"开始并发注册：计划领取 {target_count} 个库存邮箱"
            )
        self._task = asyncio.create_task(
            self._run(
                label=label,
                headless=bool(headless),
                concurrency=concurrency,
                target_count=target_count,
                manual_email=manual_email,
                provider=provider,
                browser_engine=selected_browser_engine,
            )
        )
        return self.snapshot()

    def _append_log(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        context = browser_log_context(text)
        self._state.setdefault("logs", []).append(
            {"at": utc_now(), "message": text[:1000], **context}
        )
        del self._state["logs"][:-100]
        self._state.update(
            currentStage=context["stage"],
            currentLocation=context["location"],
            currentAction=context["action"],
            currentStatus=context["status"],
        )

    def _relay_browser_logs(
        self,
        snapshot: dict[str, Any],
        *,
        task_id: str,
        cursor: int,
    ) -> tuple[str, int]:
        current_task_id = str(snapshot.get("id") or "")
        if current_task_id and current_task_id != task_id:
            task_id = current_task_id
            cursor = 0
            self._last_relayed_browser_log_key = None
        logs = snapshot.get("logs")
        if not isinstance(logs, list):
            return task_id, cursor
        if cursor > len(logs):
            cursor = 0
        elif cursor == len(logs) and logs and self._last_relayed_browser_log_key:
            previous_index = next(
                (
                    index
                    for index in range(len(logs) - 1, -1, -1)
                    if isinstance(logs[index], dict)
                    and (
                        str(logs[index].get("at") or ""),
                        str(logs[index].get("email") or ""),
                        str(logs[index].get("message") or ""),
                    )
                    == self._last_relayed_browser_log_key
                ),
                -1,
            )
            cursor = previous_index + 1 if previous_index >= 0 else 0
        for entry in logs[cursor:]:
            if not isinstance(entry, dict):
                continue
            message = str(entry.get("message") or "").strip()
            if not message:
                continue
            context = browser_log_context(message)
            self._append_log(message)
            self._last_relayed_browser_log_key = (
                str(entry.get("at") or ""),
                str(entry.get("email") or ""),
                message,
            )
            self._state.update(
                phase=context["stage"],
                message=message[:500],
            )
        return task_id, len(logs)

    async def _wait_for_browser_with_logs(self) -> dict[str, Any]:
        browser_wait = asyncio.create_task(self.browser_manager.wait())
        task_id = ""
        cursor = 0
        while not browser_wait.done():
            task_id, cursor = self._relay_browser_logs(
                self.browser_manager.snapshot(),
                task_id=task_id,
                cursor=cursor,
            )
            await asyncio.sleep(0.4)
        result = await browser_wait
        self._relay_browser_logs(result, task_id=task_id, cursor=cursor)
        return result

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

    @staticmethod
    def _normalize_provider_request_id(request_id: str) -> str:
        value = str(request_id or "").strip()
        if not value:
            return ""
        if len(value) > 128 or any(
            not (character.isalnum() or character in "-_") for character in value
        ):
            raise ValueError("验证码请求标识无效")
        return value

    async def poll_verification_code_async(
        self,
        email: str,
        *,
        request_id: str = "",
    ) -> str:
        target = self._validate_poll_target(email)
        provider_request_id = self._normalize_provider_request_id(request_id)
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
        timeout_seconds = self.provider_code_timeout_seconds
        timeout_label = f"{timeout_seconds:g}"
        timeout_message = (
            f"SMSBower Gmail 验证码等待超过 {timeout_label} 秒，"
            "已取消邮箱激活并判定注册失败"
        )
        if target in self._provider_cancelled_emails:
            raise RuntimeError(timeout_message)
        timeout_key = (target, provider_request_id or "__default__")
        started_at = self._provider_code_started_at.setdefault(
            timeout_key,
            self._monotonic(),
        )

        async def cancel_timed_out_activation() -> None:
            if target in self._provider_cancelled_emails:
                return
            cancel_provider = self.cancel_provider_email
            if cancel_provider is None and self.complete_provider_email is not None:

                async def cancel_provider(email: str, message: str) -> None:
                    await self.complete_provider_email(email, False, message)

            if cancel_provider is None:
                raise RuntimeError(
                    timeout_message + "；SMSBower 取消接口未配置"
                )
            try:
                await cancel_provider(target, timeout_message)
            except Exception as error:
                failure_message = (
                    f"SMSBower Gmail 验证码等待超过 {timeout_label} 秒，"
                    f"取消邮箱激活失败：{str(error)[:240]}"
                )
                self._state.update(
                    status="failed",
                    phase="failed",
                    message=failure_message,
                )
                self._append_log(f"失败：{failure_message}")
                raise RuntimeError(failure_message) from error
            self._provider_cancelled_emails.add(target)
            self._provider_code_started_at = {
                key: value
                for key, value in self._provider_code_started_at.items()
                if key[0] != target
            }
            self._state.update(
                status="failed",
                phase="failed",
                message=timeout_message,
            )
            self._append_log(f"失败：{timeout_message}")

        if self._monotonic() - started_at >= timeout_seconds:
            await cancel_timed_out_activation()
            raise RuntimeError(timeout_message)
        provider_poller = self.poll_provider_code
        if provider_request_id:
            cache_key = (target, provider_request_id)
            cached_code = self._provider_code_cache.get(cache_key, "")
            if cached_code:
                return cached_code
            request_ids = self._provider_code_request_ids.setdefault(target, [])
            if provider_request_id not in request_ids:
                request_ids.append(provider_request_id)
                del request_ids[:-8]
            request_index = request_ids.index(provider_request_id)
            if request_index > 0 and self.poll_provider_next_code is not None:
                provider_poller = self.poll_provider_next_code
        code = await provider_poller(target)
        if code:
            self._provider_code_started_at.pop(timeout_key, None)
            if provider_request_id:
                self._provider_code_cache[(target, provider_request_id)] = code
                active_request_ids = set(
                    self._provider_code_request_ids.get(target, [])
                )
                self._provider_code_cache = {
                    key: value
                    for key, value in self._provider_code_cache.items()
                    if key[0] != target or key[1] in active_request_ids
                }
            self._awaiting_code_emails.discard(target)
            self._sync_code_state()
            self._state.update(
                phase="registering_openai",
                message=f"SMSBower 已返回验证码，继续注册 {target}",
            )
            self._append_log(f"已从 SMSBower 自动取得 Gmail 验证码：{target}")
            return code
        if self._monotonic() - started_at >= timeout_seconds:
            await cancel_timed_out_activation()
            raise RuntimeError(timeout_message)
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

    def _finalize_cancelled_state(self) -> None:
        already_cancelled = (
            self._state.get("status") == "cancelled"
            and not self._state.get("running")
        )
        self._state.update(
            status="cancelled",
            running=False,
            phase="cancelled",
            message="一键注册已停止",
        )
        if not already_cancelled:
            self._append_log("一键注册已停止")
        self._manual_codes.clear()
        self._awaiting_code_emails.clear()
        self._provider_code_request_ids.clear()
        self._provider_code_cache.clear()
        self._provider_code_started_at.clear()
        self._provider_cancelled_emails.clear()
        self._last_relayed_browser_log_key = None
        self._sync_code_state()
        if not self._state.get("finishedAt"):
            self._state["finishedAt"] = utc_now()

    async def _run(
        self,
        *,
        label: str,
        headless: bool,
        concurrency: int,
        target_count: int,
        manual_email: str = "",
        provider: str = "inventory",
        browser_engine: str = "camoufox",
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
                for _index in range(target_count):
                    email = (await self.acquire_email(label)).strip().lower()
                    if not email:
                        break
                    claimed_emails.append(email)
                    self._state.update(
                        email=claimed_emails[0],
                        emails=list(claimed_emails),
                        claimed=len(claimed_emails),
                        message=(
                            f"正在领取库存邮箱：{len(claimed_emails)}/{target_count}"
                        ),
                    )
                    self._append_log(
                        f"已从生成库存领取邮箱（{len(claimed_emails)}/{target_count}）：{email}"
                    )
            effective_concurrency = len(claimed_emails)
            if effective_concurrency == 0:
                raise RuntimeError(
                    f"邮箱库存不足：目标 {target_count} 个，当前可领取 0 个；未启动浏览器"
                )
            browser_concurrency = min(concurrency, effective_concurrency)
            self._state["effectiveConcurrency"] = browser_concurrency
            if effective_concurrency < target_count:
                self._state["message"] = (
                    f"邮箱库存不足：目标 {target_count} 个，实际领取 "
                    f"{effective_concurrency} 个；自动按最小可用数量继续注册"
                )
                self._append_log(
                    f"库存不足，已将本次目标账号数从 {target_count} 自动降为 "
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
                            + (
                                "验证码将自动扫描收件箱与垃圾邮件"
                                if claimed_emails[0].endswith("@icloud.com")
                                else "验证码将在浏览器中手动输入"
                            )
                            if provider == "manual"
                            else f"已领取 {effective_concurrency} 个库存邮箱，正在准备注册"
                        )
                    )
                    + (
                        f"（原目标 {target_count} 个，已按库存自动调整）"
                        if effective_concurrency < target_count
                        else ""
                    )
                ),
            )
            accounts: list[dict[str, Any]] = []
            for index, email in enumerate(claimed_emails, start=1):
                if provider == "inventory":
                    await self.confirm_email(email)
                automatic_inbox_code = (
                    provider == "manual" and email.endswith("@icloud.com")
                )
                manual_browser_code = provider == "manual" and not automatic_inbox_code
                saved_account: dict[str, Any] = {}
                if self.load_account is not None:
                    loaded_account = await self.load_account(email)
                    if isinstance(loaded_account, dict):
                        saved_account = loaded_account
                password = str(saved_account.get("password") or "")
                reused_password = bool(password)
                if not reused_password:
                    password = generate_openai_password()
                    if self.save_password is not None:
                        await self.save_password(email, password)
                saved_two_factor = (
                    dict(saved_account.get("two_factor"))
                    if isinstance(saved_account.get("two_factor"), dict)
                    else {}
                )
                accounts.append(
                    {
                        "email": email,
                        "password": password,
                        "password_confirmed": bool(
                            saved_account.get("password_confirmed", False)
                        ),
                        "ensure_password": True,
                        "force_reset_password": False,
                        "enable_2fa": True,
                        "manual_otp_entry": manual_browser_code,
                        "password_first_required": True,
                        "foreground_required": manual_browser_code,
                        "two_factor": saved_two_factor,
                    }
                )
                if str(saved_two_factor.get("secret") or "").strip():
                    self._append_log(
                        f"[2FA] 已载入该账号本地保存的 TOTP 密钥（{index}/"
                        f"{effective_concurrency}）：{email}；"
                        "登录遇到动态码页面时将自动生成并提交当前验证码"
                    )
                if reused_password:
                    self._append_log(
                        "检测到该邮箱已有首次注册保存的密码"
                        f"（{index}/{effective_concurrency}）：{email}；"
                        "本次继续使用原密码，不生成或覆盖新密码"
                    )
                else:
                    self._append_log(
                        f"邮箱已添加并保存唯一密码（{index}/{effective_concurrency}）：{email}"
                    )
            self._state.update(
                phase="registering_openai",
                message=(
                    f"正在启动 {effective_concurrency} 个 "
                    f"{'Roxy' if browser_engine == 'roxy' else 'Camoufox'} 注册浏览器"
                    + (
                        (
                            "；验证码将自动扫描收件箱与垃圾邮件"
                            if claimed_emails[0].endswith("@icloud.com")
                            else "；验证码请直接在浏览器中手动输入并点击继续"
                        )
                        if provider == "manual"
                        else ""
                    )
                    + (
                        f"（库存不足，目标已从 {target_count} 降为 {effective_concurrency}）"
                        if effective_concurrency < target_count
                        else ""
                    )
                ),
            )
            self._append_log(
                f"{effective_concurrency} 个邮箱已准备，按并发 "
                f"{browser_concurrency} 启动 "
                f"{'Roxy' if browser_engine == 'roxy' else 'Camoufox'}"
                + (
                    (
                        "；iCloud 自有邮箱自动扫描 INBOX 与垃圾邮件取码"
                        if claimed_emails[0].endswith("@icloud.com")
                        else "；自有邮箱不连接 IMAP，验证码由你在浏览器中手动输入"
                    )
                    if provider == "manual"
                    else ""
                )
            )
            browser_start_options = {
                "headless": False if provider == "manual" else headless,
                "concurrency": browser_concurrency,
                "use_registration_proxy": True,
            }
            if browser_engine != "camoufox":
                browser_start_options["browser_engine"] = browser_engine
            self.browser_manager.start(accounts, **browser_start_options)
            browser_result = await self._wait_for_browser_with_logs()
            succeeded = max(0, int(browser_result.get("succeeded") or 0))
            result_accounts = browser_result.get("accounts") or []
            registered_emails = {
                str(item.get("email") or "").strip().lower()
                for item in result_accounts
                if item.get("status") == "success" and item.get("email")
            }
            if provider == "inventory":
                successful_emails.update(registered_emails)
            if (
                succeeded == effective_concurrency
                and len(registered_emails) != effective_concurrency
            ):
                registered_emails = set(claimed_emails)
            if succeeded == effective_concurrency:
                result_by_email = {
                    str(item.get("email") or "").strip().lower(): item
                    for item in result_accounts
                    if isinstance(item, dict) and item.get("email")
                }
                account_by_email = {
                    str(item.get("email") or "").strip().lower(): item
                    for item in accounts
                }
                retry_accounts: list[dict[str, Any]] = []
                passwordless_accounts: list[str] = []
                already_enabled: set[str] = set()
                for email in claimed_emails:
                    if email not in registered_emails:
                        continue
                    result_item = result_by_email.get(email, {})
                    if result_item.get("twoFactorEnabled"):
                        already_enabled.add(email)
                        continue
                    record: dict[str, Any] = {}
                    if self.load_account is not None:
                        loaded = await self.load_account(email)
                        if isinstance(loaded, dict):
                            record = loaded
                    saved_two_factor = (
                        record.get("two_factor")
                        if isinstance(record.get("two_factor"), dict)
                        else {}
                    )
                    if saved_two_factor.get("enabled"):
                        already_enabled.add(email)
                        result_by_email[email] = {
                            **result_item,
                            "email": email,
                            "twoFactorEnabled": True,
                        }
                        continue
                    source_account = account_by_email.get(email, {})
                    password = str(
                        record.get("password") or source_account.get("password") or ""
                    )
                    password_confirmed = bool(
                        record.get("password_confirmed")
                        if "password_confirmed" in record
                        else result_item.get("passwordConfirmed")
                    )
                    if not password or not password_confirmed:
                        passwordless_accounts.append(email)
                        continue
                    retry_accounts.append(
                        {
                            "email": email,
                            "password": password,
                            "password_confirmed": True,
                            "enable_2fa": True,
                            "foreground_required": False,
                            "two_factor": saved_two_factor,
                        }
                    )

                if passwordless_accounts:
                    strict_password_failures = [
                        email
                        for email in passwordless_accounts
                        if account_by_email.get(email, {}).get(
                            "password_first_required"
                        )
                    ]
                    if strict_password_failures:
                        raise RuntimeError(
                            "注册必须确认密码并开启 2FA；"
                            "已拒绝免密码账号："
                            + "、".join(strict_password_failures)
                        )
                    self._append_log(
                        "OpenAI 当前使用免密码注册，已跳过密码设置和依赖密码的 2FA；"
                        "账号注册、Session、AT 与 Cookie 仍判定成功："
                        + "、".join(passwordless_accounts)
                    )

                if retry_accounts:
                    self._state.update(
                        phase="repairing_2fa",
                        message=(
                            f"检测到 {len(retry_accounts)} 个账号尚未完成 2FA；"
                            f"正在使用 Cookie 启动一次"
                            f"{'显示浏览器后台' if not headless else '无头'}补做"
                        ),
                    )
                    self._append_log(self._state["message"])
                    retry_start_options = {
                        "headless": headless,
                        "concurrency": len(retry_accounts),
                        "use_registration_proxy": True,
                    }
                    if browser_engine != "camoufox":
                        retry_start_options["browser_engine"] = browser_engine
                    self.browser_manager.start(
                        retry_accounts, **retry_start_options
                    )
                    retry_result = await self._wait_for_browser_with_logs()
                    retry_items = {
                        str(item.get("email") or "").strip().lower(): item
                        for item in retry_result.get("accounts") or []
                        if isinstance(item, dict) and item.get("email")
                    }
                    retry_failures = []
                    for retry_account in retry_accounts:
                        email = str(retry_account["email"])
                        item = retry_items.get(email, {})
                        if item.get("status") == "success" and item.get(
                            "twoFactorEnabled"
                        ):
                            already_enabled.add(email)
                            result_by_email[email] = item
                            continue
                        retry_failures.append(
                            f"{email}（{str(item.get('message') or '2FA 未开启')[:160]}）"
                        )
                    if retry_failures:
                        raise RuntimeError(
                            "注册后的 2FA 补做仍未完成：" + "；".join(retry_failures)
                        )
                    self._append_log(
                        f"注册后 2FA 补做完成：{len(retry_accounts)}/{len(retry_accounts)}"
                    )

                missing_two_factor = [
                    email
                    for email in claimed_emails
                    if email not in already_enabled
                    and email not in passwordless_accounts
                ]
                if missing_two_factor:
                    raise RuntimeError(
                        "以下账号仍未完成 2FA：" + "、".join(missing_two_factor)
                    )

                successful_emails = set(claimed_emails)
                result_accounts = [
                    result_by_email.get(email, {}) for email in claimed_emails
                ]
                password_confirmed = sum(
                    1 for item in result_accounts if item.get("passwordConfirmed")
                )
                two_factor_enabled = sum(
                    1 for item in result_accounts if item.get("twoFactorEnabled")
                )
                detail = (
                    f"并发注册完成：成功 {succeeded}/{effective_concurrency}；"
                    f"密码已确认 {password_confirmed}/{effective_concurrency}；"
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
            self._finalize_cancelled_state()
            raise
        except Exception as error:
            self._state.update(
                status="failed",
                phase="failed",
                message=str(error or "一键注册失败")[:500],
            )
            self._append_log(f"失败：{self._state['message']}")
        finally:
            try:
                finalization_emails = (
                    claimed_emails if provider in {"inventory", "smsbower"} else []
                )
                for email in finalization_emails:
                    try:
                        if (
                            provider == "smsbower"
                            and self.complete_provider_email is not None
                        ):
                            if email in self._provider_cancelled_emails:
                                self._append_log(
                                    f"SMSBower Gmail 验证码超时，激活已取消：{email}"
                                )
                                continue
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
                                    else str(
                                        self._state.get("message") or "OpenAI 注册失败"
                                    )
                                ),
                            )
                            self._append_log(
                                f"已向远端库存回执（{email}）："
                                f"{'注册成功，标记已使用' if success else '注册失败，已释放，可再次注册'}"
                            )
                        elif provider == "inventory" and self.release_email is not None:
                            await self.release_email(email)
                    except Exception as error:
                        self._append_log(
                            f"提交{' SMSBower 激活' if provider == 'smsbower' else '库存'}回执失败"
                            f"（{email}）：{str(error)[:300]}"
                        )
            finally:
                if self._state.get("status") == "cancelling":
                    self._finalize_cancelled_state()
                else:
                    self._state["running"] = False
                    self._manual_codes.clear()
                    self._awaiting_code_emails.clear()
                    self._provider_code_request_ids.clear()
                    self._provider_code_cache.clear()
                    self._provider_code_started_at.clear()
                    self._provider_cancelled_emails.clear()
                    self._sync_code_state()
                    self._state["finishedAt"] = utc_now()

    async def stop(self) -> dict[str, Any]:
        if not self._task:
            return self.snapshot()
        if self._task.done():
            if self._state.get("running") or self._state.get("status") == "cancelling":
                self._finalize_cancelled_state()
            return self.snapshot()
        self._state.update(
            status="cancelling", phase="cancelling", message="正在停止一键注册"
        )
        if self.browser_manager.snapshot().get("running"):
            await self.browser_manager.stop()
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        if self._state.get("running") or self._state.get("status") == "cancelling":
            self._finalize_cancelled_state()
        return self.snapshot()

    async def close(self) -> None:
        await self.stop()


class ConcurrentRegistrationTaskManager:
    """Coordinate independent registration processes behind one web API."""

    def __init__(
        self,
        *,
        process_factory: Callable[[], RegistrationTaskManager],
        shared_browser_manager: BrowserTaskManager | None = None,
        acquire_email: AcquireInventoryEmail | None = None,
        complete_email: CompleteInventoryEmail | None = None,
        record_failure: RecordProcessFailure | None = None,
        max_processes: int = 10,
        history_limit: int = 20,
    ) -> None:
        self.process_factory = process_factory
        self.shared_browser_manager = shared_browser_manager
        self.acquire_email = acquire_email
        self.complete_email = complete_email
        self.record_failure = record_failure
        self.max_processes = max(2, min(20, int(max_processes)))
        self.history_limit = max(self.max_processes, int(history_limit))
        self._processes: dict[str, RegistrationTaskManager] = {}
        self._monitor_tasks: dict[str, asyncio.Task] = {}
        self._recorded_failures: set[str] = set()
        self._latest_process_id = ""

    def _process_snapshots(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (process_id, manager.snapshot())
            for process_id, manager in self._processes.items()
        ]

    def _trim_history(self) -> None:
        completed = [
            process_id
            for process_id, snapshot in self._process_snapshots()
            if not snapshot.get("running")
        ]
        while len(self._processes) > self.history_limit and completed:
            self._processes.pop(completed.pop(0), None)

    def snapshot(self) -> dict[str, Any]:
        snapshots = self._process_snapshots()
        if not snapshots:
            return {
                **RegistrationTaskManager._idle_state(),
                "runningCount": 0,
                "processCount": 0,
                "maxProcesses": self.max_processes,
                "canStartNext": True,
                "recordedFailureCount": len(self._recorded_failures),
                "tasks": [],
            }

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
        display_state = dict(display[1])
        active_count = len(active)
        awaiting_emails: list[str] = []
        combined_logs: list[dict[str, Any]] = []
        public_tasks: list[dict[str, Any]] = []
        for process_index, (process_id, state) in enumerate(snapshots, start=1):
            emails = [
                str(email or "").strip().lower()
                for email in state.get("emails", [])
                if str(email or "").strip()
            ]
            for email in state.get("awaitingCodeEmails", []):
                target = str(email or "").strip().lower()
                if target and target not in awaiting_emails:
                    awaiting_emails.append(target)
            process_label = f"进程 {process_index}"
            if emails:
                process_label += f" · {emails[0]}"
            public_tasks.append(
                {
                    **state,
                    "processId": process_id,
                    "processIndex": process_index,
                    "processLabel": process_label,
                    "failureRecorded": process_id in self._recorded_failures,
                }
            )
            for log in state.get("logs", []):
                entry = dict(log)
                entry["processId"] = process_id
                entry["processIndex"] = process_index
                entry["message"] = f"[{process_label}] {entry.get('message', '')}"
                combined_logs.append(entry)
        combined_logs.sort(key=lambda item: str(item.get("at") or ""))
        summary_snapshots = active or [display]
        summary_states = [state for _, state in summary_snapshots]
        summary_emails: list[str] = []
        for state in summary_states:
            for email in state.get("emails", []):
                target = str(email or "").strip().lower()
                if target and target not in summary_emails:
                    summary_emails.append(target)

        display_state.update(
            id=display[0],
            running=bool(active),
            status="running" if active else display_state.get("status", "idle"),
            message=(
                f"{active_count} 个注册进程正在运行；最新进程："
                f"{display_state.get('message', '')}"
                if active_count
                else display_state.get("message", "")
            ),
            emails=summary_emails,
            awaitingCode=bool(awaiting_emails),
            awaitingCodeEmails=awaiting_emails,
            requested=sum(int(state.get("requested") or 0) for state in summary_states),
            effectiveConcurrency=sum(
                int(state.get("effectiveConcurrency") or 0) for state in summary_states
            ),
            claimed=sum(int(state.get("claimed") or 0) for state in summary_states),
            logs=combined_logs[-100:],
            runningCount=active_count,
            processCount=len(snapshots),
            maxProcesses=self.max_processes,
            canStartNext=active_count < self.max_processes,
            recordedFailureCount=len(self._recorded_failures),
            tasks=public_tasks,
        )
        return display_state

    def _active_manager_for_email(self, email: str) -> RegistrationTaskManager:
        target = str(email or "").strip().lower()
        for _, manager in reversed(list(self._processes.items())):
            state = manager.snapshot()
            emails = {
                str(item or "").strip().lower()
                for item in state.get("emails", [])
            }
            if state.get("running") and target in emails:
                return manager
        raise RuntimeError("该邮箱当前没有正在运行的注册任务")

    def start(
        self,
        *,
        label: str,
        headless: bool,
        concurrency: int = 1,
        target_count: int | None = None,
        email: str = "",
        provider: str = "",
        browser_engine: str = "camoufox",
    ) -> dict[str, Any]:
        active = [
            state
            for _, state in self._process_snapshots()
            if state.get("running")
        ]
        if len(active) >= self.max_processes:
            raise RuntimeError(f"注册进程已达到上限 {self.max_processes}")
        selected_browser_engine = str(browser_engine or "camoufox").strip().lower()
        if selected_browser_engine == "roxy" and any(
            str(state.get("browserEngine") or "").strip().lower() == "roxy"
            for state in active
        ):
            raise RuntimeError("Roxy 专用指纹环境正在注册，请等待当前任务完成")
        target = str(email or "").strip().lower()
        if target and any(
            target
            in {
                str(item or "").strip().lower()
                for item in state.get("emails", [])
            }
            for state in active
        ):
            raise RuntimeError("该邮箱已有正在运行的注册进程")
        if (
            self.shared_browser_manager is not None
            and self.shared_browser_manager.snapshot().get("running")
        ):
            raise RuntimeError("浏览器任务正在运行，请等待完成后再注册")

        manager = self.process_factory()
        process_start_options = dict(
            label=label,
            headless=headless,
            concurrency=concurrency,
            email=target,
            provider=provider,
        )
        if selected_browser_engine != "camoufox":
            process_start_options["browser_engine"] = selected_browser_engine
            if target_count is not None:
                process_start_options["target_count"] = target_count
        task = manager.start(**process_start_options)
        process_id = str(task.get("id") or uuid.uuid4().hex)
        self._processes[process_id] = manager
        self._latest_process_id = process_id
        self._monitor_tasks[process_id] = asyncio.create_task(
            self._monitor_process(process_id, manager)
        )
        self._trim_history()
        return self.snapshot()

    async def _monitor_process(
        self,
        process_id: str,
        manager: RegistrationTaskManager,
    ) -> None:
        while manager.snapshot().get("running"):
            await asyncio.sleep(0.25)
        state = manager.snapshot()
        if (
            state.get("status") != "failed"
            or process_id in self._recorded_failures
            or self.record_failure is None
        ):
            return
        failure = {
            **state,
            "processId": process_id,
            "recordedAt": utc_now(),
        }
        result = self.record_failure(failure)
        if inspect.isawaitable(result):
            await result
        self._recorded_failures.add(process_id)

    def poll_verification_code(self, email: str) -> str:
        return self._active_manager_for_email(email).poll_verification_code(email)

    async def poll_verification_code_async(
        self,
        email: str,
        *,
        request_id: str = "",
    ) -> str:
        manager = self._active_manager_for_email(email)
        return await manager.poll_verification_code_async(
            email,
            request_id=request_id,
        )

    def submit_verification_code(self, email: str, code: str) -> dict[str, Any]:
        self._active_manager_for_email(email).submit_verification_code(email, code)
        return self.snapshot()

    async def stop(self) -> dict[str, Any]:
        active_managers = [
            manager
            for manager in self._processes.values()
            if manager.snapshot().get("running")
        ]
        if active_managers:
            await asyncio.gather(*(manager.stop() for manager in active_managers))
        return self.snapshot()

    async def close(self) -> None:
        await self.stop()
        monitors = [task for task in self._monitor_tasks.values() if not task.done()]
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)


__all__ = [
    "CancelProviderEmail",
    "CompleteInventoryEmail",
    "ConcurrentRegistrationTaskManager",
    "LoadAccount",
    "RecordProcessFailure",
    "RegistrationTaskManager",
    "generate_openai_password",
]
