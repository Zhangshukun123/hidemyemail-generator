"""MVP application layer and HTTP routes for account-management actions."""

from __future__ import annotations

import asyncio
import hmac
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from aiohttp import web

from .account_browser import (
    AccountBrowserModel,
    AccountBrowserPresenter,
    BrowserWorkerLauncher,
)
from .browser_tasks import load_account_record
from .payment_sms import GlobalSmsRoutingConfigStore
from .plus_codex import PlusCodexView, direct_plus_phone_job_id


TokenValidator = Callable[[web.Request], bool]
PAYMENT_CONFIRMATION_TERMINAL = {
    "plus",
    "not_plus",
    "refresh_failed",
    "plus_sms_failed",
}


def account_is_plus(record: dict[str, Any] | None) -> bool:
    return isinstance(record, dict) and str(record.get("account_type") or "").strip().lower() == "plus"


def account_phone_binding_state(record: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize current and legacy phone-binding markers for every entry point."""

    account = record if isinstance(record, dict) else {}
    plus_codex = account.get("plus_codex")
    plus_codex = plus_codex if isinstance(plus_codex, dict) else {}
    plus_sms = account.get("plus_sms")
    plus_sms = plus_sms if isinstance(plus_sms, dict) else {}
    status = str(plus_codex.get("status") or "").strip().lower()
    legacy_statuses = (
        plus_codex.get("detail"),
        plus_sms.get("status"),
        plus_sms.get("detail"),
        account.get("phone_binding_status"),
        account.get("bound_phone_status"),
    )
    legacy_bound = any(
        marker in str(value or "")
        for value in legacy_statuses
        for marker in ("手机号已绑定", "手机号码已绑定")
    )
    bound = bool(
        plus_codex.get("sms_verified")
        or status == "completed"
        or plus_sms.get("phone_bound") is True
        or str(plus_sms.get("status") or "").strip().lower() == "completed"
        or account.get("phone_bound") is True
        or str(account.get("bound_phone") or "").strip()
        or legacy_bound
    )
    return {
        "bound": bound,
        "running": not bound and status == "running",
        "status": "completed" if bound else status,
        "sms_verified": bound,
        "phone_masked": str(
            plus_codex.get("phone_masked") or plus_sms.get("phone_masked") or ""
        ),
    }


def account_payment_job_is_terminal(job: dict[str, Any]) -> bool:
    status = str(job.get("status") or "").lower()
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    confirmation = (
        job.get("account_confirmation")
        if isinstance(job.get("account_confirmation"), dict)
        else {}
    )
    protocol_succeeded = status == "completed" and result.get("status") == "success"
    return status in {"completed", "failed", "cancelled"} and (
        not protocol_succeeded
        or str(confirmation.get("status") or "").lower()
        in PAYMENT_CONFIRMATION_TERMINAL
    )


class AccountPaymentGuard:
    """Per-account idempotency guard for live PayPal protocol jobs."""

    def __init__(self, ttl_seconds: float = 2 * 60 * 60) -> None:
        self.ttl_seconds = max(60.0, float(ttl_seconds))
        self._lock = asyncio.Lock()
        self._active: dict[str, dict[str, Any]] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        self._active = {
            email: state
            for email, state in self._active.items()
            if float(state.get("expires_at") or 0) > now
        }

    async def reserve(self, email: str) -> bool:
        target = str(email or "").strip().lower()
        async with self._lock:
            self._prune()
            if target in self._active:
                return False
            self._active[target] = {
                "job_id": "",
                "expires_at": time.monotonic() + self.ttl_seconds,
            }
            return True

    async def started(self, email: str, job_id: str) -> None:
        target = str(email or "").strip().lower()
        async with self._lock:
            state = self._active.setdefault(target, {})
            state.update(
                job_id=str(job_id or ""),
                expires_at=time.monotonic() + self.ttl_seconds,
            )

    async def release(self, *, email: str = "", job_id: str = "") -> None:
        target = str(email or "").strip().lower()
        selected_job = str(job_id or "").strip()
        async with self._lock:
            self._prune()
            if target:
                state = self._active.get(target)
                if state is not None and (
                    not selected_job
                    or not state.get("job_id")
                    or str(state.get("job_id")) == selected_job
                ):
                    self._active.pop(target, None)
                return
            for account_email, state in list(self._active.items()):
                if selected_job and str(state.get("job_id") or "") == selected_job:
                    self._active.pop(account_email, None)
                    return

    async def active_emails(self) -> set[str]:
        """Return a credential-free snapshot for account-list presentation."""

        async with self._lock:
            self._prune()
            return set(self._active)


class PlusCodexPort(Protocol):
    model: Any

    async def ensure(
        self,
        *,
        job: dict[str, Any],
        confirmation: dict[str, Any],
        base_url: str,
        sms_provider: str,
        retry_failed: bool = False,
    ) -> dict[str, Any]: ...


class SmsResolverPort(Protocol):
    def resolve(self) -> Any: ...


class AccountPhoneBindingModel:
    """Model: recover the latest confirmed Plus payment for one account."""

    def __init__(self, db_file: Path) -> None:
        self.db_file = Path(db_file)

    @staticmethod
    def _valid_email(value: str) -> str:
        email = str(value or "").strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("账号邮箱格式无效")
        return email

    @staticmethod
    def _confirmed_payment(record: dict[str, Any]) -> dict[str, Any]:
        latest = record.get("payment_confirmation")
        candidates = [latest] if isinstance(latest, dict) else []
        historical = record.get("payment_confirmations")
        if isinstance(historical, dict):
            candidates.extend(
                value
                for value in reversed(list(historical.values()))
                if isinstance(value, dict)
            )
        return next(
            (
                dict(item)
                for item in candidates
                if str(item.get("status") or "") == "plus"
                and item.get("payment_succeeded") is True
                and str(item.get("job_id") or "").strip()
            ),
            {},
        )

    def context(self, email: str) -> dict[str, Any]:
        target = self._valid_email(email)
        record = load_account_record(self.db_file, target)
        if not record:
            raise RuntimeError("账号不存在，请刷新后重试")
        current = record.get("plus_codex")
        binding = account_phone_binding_state(record)
        if binding["bound"]:
            public = PlusCodexView.present(current if isinstance(current, dict) else {})
            public.update(
                status="completed",
                stage="completed",
                detail="该账号手机号已绑定",
                sms_verified=True,
                phone_masked=binding["phone_masked"],
            )
            return {"email": target, "current": public}
        if binding["running"] and isinstance(current, dict):
            return {"email": target, "current": PlusCodexView.present(current)}
        if not account_is_plus(record):
            raise RuntimeError("请先确认账号已升级为 Plus")
        confirmation = self._confirmed_payment(record)
        if confirmation:
            job_id = str(confirmation.get("job_id") or "").strip()
        else:
            job_id = direct_plus_phone_job_id(target)
            confirmation = {
                "job_id": job_id,
                "status": "plus",
                "plus_confirmed": True,
                "account_type": "plus",
                "payment_succeeded": False,
                "source": "existing_plus_account",
            }
        return {
            "email": target,
            "job": {"id": job_id, "source_account_email": target},
            "confirmation": confirmation,
        }

    def status(self, email: str) -> tuple[str, dict[str, Any]]:
        target = self._valid_email(email)
        record = load_account_record(self.db_file, target)
        if not record:
            raise RuntimeError("账号不存在，请刷新后重试")
        current = record.get("plus_codex")
        state = PlusCodexView.present(current if isinstance(current, dict) else {})
        binding = account_phone_binding_state(record)
        if binding["bound"]:
            state.update(
                status="completed",
                stage="completed",
                detail="该账号手机号已绑定",
                sms_verified=True,
                phone_masked=binding["phone_masked"],
            )
        return target, state


class AccountPhoneBindingView:
    """View: project Plus phone progress without raw phone or provider secrets."""

    @staticmethod
    def present(email: str, value: dict[str, Any]) -> dict[str, Any]:
        state = PlusCodexView.present(value)
        status = state["status"]
        message = (
            "手机号已绑定，Codex OAuth 凭据已就绪"
            if status == "completed" and state["export_ready"]
            else "手机号已绑定"
            if status == "completed"
            else state["detail"]
            if status == "failed"
            else "手机号绑定任务已启动，正在先完成 Roxy 邮箱登录"
        )
        return {
            "ok": status != "failed",
            "error": message if status == "failed" else "",
            "email": email,
            "status": status,
            "stage": state["stage"],
            "detail": state["detail"],
            "provider": state["provider"],
            "smsVerified": state["sms_verified"],
            "phoneMasked": state["phone_masked"],
            "exportReady": state["export_ready"],
            "attempt": state["attempt"],
            "jobId": state["job_id"],
            "startedAt": state["started_at"],
            "updatedAt": state["updated_at"],
            "finishedAt": state["completed_at"],
            "logSequence": state["log_sequence"],
            "logs": state["logs"],
            "message": message,
        }


class AccountPhoneBindingPresenter:
    """Presenter: start or replay the existing Plus-Codex phone workflow."""

    def __init__(
        self,
        model: AccountPhoneBindingModel,
        *,
        plus_codex: PlusCodexPort,
        sms_resolver: SmsResolverPort,
        base_url: str,
    ) -> None:
        self.model = model
        self.plus_codex = plus_codex
        self.sms_resolver = sms_resolver
        self.base_url = str(base_url or "").rstrip("/")

    async def bind(self, email: str) -> dict[str, Any]:
        context = await asyncio.to_thread(self.model.context, email)
        target = str(context["email"])
        current = context.get("current")
        if isinstance(current, dict):
            return AccountPhoneBindingView.present(target, current)
        sms_policy = GlobalSmsRoutingConfigStore(self.model.db_file).purpose("binding")
        if not sms_policy["configured"]:
            raise RuntimeError(
                f"请先在设置 → 接码配置中保存 {sms_policy['providerLabel']} API Key"
            )
        result = await self.plus_codex.ensure(
            job=context["job"],
            confirmation=context["confirmation"],
            base_url=self.base_url,
            sms_provider=str(sms_policy["provider"]),
            retry_failed=True,
        )
        return AccountPhoneBindingView.present(target, result)

    async def status(self, email: str, *, log_after: int = 0) -> dict[str, Any]:
        target, state = await asyncio.to_thread(self.model.status, email)
        result = AccountPhoneBindingView.present(target, state)
        after = max(0, int(log_after or 0))
        result["logs"] = [
            item
            for item in result["logs"]
            if int(item.get("sequence") or 0) > after
        ]
        # A failed workflow is still a successful status read.  Keep the
        # terminal error in the payload so the polling UI can render it.
        result["ok"] = True
        return result


class AccountActionRouteAdapter:
    """HTTP adapter: authenticate and delegate to the account Presenters."""

    def __init__(
        self,
        app: web.Application,
        *,
        token_validator: TokenValidator,
    ) -> None:
        self.app = app
        self.token_validator = token_validator

    async def _payload(self, request: web.Request) -> dict[str, Any]:
        try:
            value = await request.json()
        except Exception as error:
            raise ValueError("请求格式无效") from error
        if not isinstance(value, dict):
            raise ValueError("请求格式无效")
        return value

    def _forbidden(self, request: web.Request) -> web.Response | None:
        if self.token_validator(request):
            return None
        return web.json_response({"ok": False, "error": "本地请求令牌无效"}, status=403)

    async def open_browser(self, request: web.Request) -> web.Response:
        forbidden = self._forbidden(request)
        if forbidden is not None:
            return forbidden
        try:
            payload = await self._payload(request)
            result = await self.app["account_browser_presenter"].open(
                str(payload.get("email") or ""),
                str(payload.get("mode") or ""),
            )
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        except RuntimeError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=409)
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def bind_phone(self, request: web.Request) -> web.Response:
        forbidden = self._forbidden(request)
        if forbidden is not None:
            return forbidden
        try:
            payload = await self._payload(request)
            result = await self.app["account_phone_binding_presenter"].bind(
                str(payload.get("email") or "")
            )
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        except RuntimeError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=409)
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def bind_phone_status(self, request: web.Request) -> web.Response:
        forbidden = self._forbidden(request)
        if forbidden is not None:
            return forbidden
        try:
            try:
                log_after = int(request.query.get("log_after", "0") or 0)
            except ValueError as error:
                raise ValueError("日志序号无效") from error
            result = await self.app["account_phone_binding_presenter"].status(
                str(request.query.get("email") or ""),
                log_after=max(0, log_after),
            )
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        except RuntimeError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=409)
        return web.json_response(result, headers={"Cache-Control": "no-store"})


def _default_token_validator(app: web.Application) -> TokenValidator:
    def validate(request: web.Request) -> bool:
        expected = str(app.get("local_token") or "")
        supplied = str(request.headers.get("X-Local-Token") or "")
        return bool(expected and supplied and hmac.compare_digest(expected, supplied))

    return validate


def setup_account_action_routes(
    app: web.Application,
    *,
    base_url: str,
    token_validator: TokenValidator | None = None,
    browser_presenter: Any | None = None,
    phone_presenter: Any | None = None,
) -> None:
    """Compose account Presenters, register routes, and bind cleanup."""

    if browser_presenter is None:
        browser_manager = app.get("browser_manager")
        python_executable = Path(
            getattr(browser_manager, "python_executable", sys.executable)
            or sys.executable
        )
        browser_presenter = AccountBrowserPresenter(
            AccountBrowserModel(app["db_file"], app["roxy_registration_store"]),
            launcher=BrowserWorkerLauncher(python_executable=python_executable),
        )
    if phone_presenter is None:
        phone_presenter = AccountPhoneBindingPresenter(
            AccountPhoneBindingModel(app["db_file"]),
            plus_codex=app["plus_codex_presenter"],
            sms_resolver=app["payment_sms_resolver"],
            base_url=base_url,
        )
    app["account_browser_presenter"] = browser_presenter
    app["account_phone_binding_presenter"] = phone_presenter
    if "account_payment_guard" not in app:
        app["account_payment_guard"] = AccountPaymentGuard()
    adapter = AccountActionRouteAdapter(
        app, token_validator=token_validator or _default_token_validator(app)
    )
    app.router.add_post("/api/account/actions/open-browser", adapter.open_browser)
    app.router.add_post("/api/account/actions/bind-phone", adapter.bind_phone)
    app.router.add_get(
        "/api/account/actions/bind-phone/status", adapter.bind_phone_status
    )
    # Keep the short alias useful to local integrations while the UI uses the
    # grouped account-actions namespace.
    app.router.add_post("/api/account/bind-phone", adapter.bind_phone)
    app.router.add_get("/api/account/bind-phone/status", adapter.bind_phone_status)

    async def account_browser_context(_: web.Application):
        try:
            yield
        finally:
            presenter = app.get("account_browser_presenter")
            close = getattr(presenter, "close", None)
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    app.cleanup_ctx.append(account_browser_context)


__all__ = [
    "AccountActionRouteAdapter",
    "AccountPaymentGuard",
    "AccountPhoneBindingModel",
    "AccountPhoneBindingPresenter",
    "AccountPhoneBindingView",
    "account_is_plus",
    "account_phone_binding_state",
    "account_payment_job_is_terminal",
    "setup_account_action_routes",
]
