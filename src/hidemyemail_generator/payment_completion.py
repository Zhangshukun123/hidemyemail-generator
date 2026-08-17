"""Confirm a protocol payment by refreshing the account AT from saved cookies."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .account_verifier import (
    refresh_session_with_saved_cookies,
    validate_refreshed_session,
)
from .browser_tasks import (
    account_registration_proxy_url,
    account_saved_cookies,
    account_session_access_token,
    jwt_account_type,
    load_account_record,
)
from .inbox import connect_db


TERMINAL_CONFIRMATION_STATUSES = {"plus", "not_plus", "refresh_failed"}
SessionRefresher = Callable[..., Awaitable[dict[str, Any]]]
OPENAI_COMPLETION_HOSTS = {"pay.openai.com", "chatgpt.com", "chat.openai.com"}
OPENAI_SUCCESS_REDIRECT_STATUSES = {"success", "succeeded"}


def _openai_completion_url(value: Any) -> bool:
    candidate = str(value or "").strip()
    if (
        not candidate
        or "\\" in candidate
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        return False
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").lower()
    if (
        parsed.scheme.lower() != "https"
        or host not in OPENAI_COMPLETION_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return False
    return host == "pay.openai.com" or path.startswith("/checkout/verify")


def _protocol_redirect_status(result: dict[str, Any]) -> str:
    final_redirect_url = str(result.get("final_redirect_url") or "").strip()
    if not _openai_completion_url(final_redirect_url):
        return ""
    try:
        values = parse_qs(urlsplit(final_redirect_url).query).get("redirect_status")
    except ValueError:
        values = None
    return str(values[0] if values else "").strip().lower()


def reconcile_openai_protocol_job(job: dict[str, Any]) -> dict[str, Any]:
    """Restore the narrow legacy false-negative shape without replaying payment."""

    if not isinstance(job, dict):
        return job
    result = job.get("result")
    if not isinstance(result, dict):
        return job
    trusted_openai_return = _openai_completion_url(result.get("final_redirect_url"))
    legacy_false_negative = (
        str(job.get("status") or "") == "failed"
        and str(result.get("status") or "") == "error"
        and str(result.get("error_code") or "") == "BRAINTREE_VAULT_FAILED"
        and result.get("paypal_authorized") is True
        and _protocol_redirect_status(result) in OPENAI_SUCCESS_REDIRECT_STATUSES
        and trusted_openai_return
    )
    if not legacy_false_negative:
        return job
    corrected_result = dict(result)
    corrected_result.pop("error", None)
    corrected_result.pop("error_code", None)
    corrected_result.update(
        {
            "status": "success",
            "settlement_status": "confirmed",
            "completion_provider": "openai",
            "braintree_bridge_status": "not_applicable",
            "openai_checkout_confirmed": True,
            "reconciled_from_error_code": "BRAINTREE_VAULT_FAILED",
        }
    )
    corrected_job = dict(job)
    corrected_job.update(
        {
            "status": "completed",
            "stage": "已完成",
            "error": "",
            "result": corrected_result,
        }
    )
    return corrected_job


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _public_error(error: Exception, *secrets: str) -> str:
    detail = str(error or "刷新新 AT 失败")
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "[REDACTED]")
    detail = re.sub(r"(?i)(https?://)([^/@\s]+)@", r"\1[REDACTED]@", detail)
    return detail[:500]


class PaymentCompletionModel:
    """Persistence model for payment-triggered AT refresh and classification."""

    def __init__(self, db_file: Path) -> None:
        self.db_file = Path(db_file)

    @staticmethod
    def public_outcome(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": str(value.get("job_id") or ""),
            "email": str(value.get("email") or ""),
            "status": str(value.get("status") or "refresh_failed"),
            "protocol_succeeded": bool(value.get("protocol_succeeded")),
            "plus_confirmed": bool(value.get("plus_confirmed")),
            "payment_succeeded": bool(value.get("payment_succeeded")),
            "at_refreshed": bool(value.get("at_refreshed")),
            "at_changed": bool(value.get("at_changed")),
            "account_type": str(value.get("account_type") or "unverified"),
            "plan": str(value.get("plan") or "")[:100],
            "detail": str(value.get("detail") or "")[:1000],
            "checked_at": str(value.get("checked_at") or ""),
            "attempt": max(0, int(value.get("attempt") or 0)),
            "max_attempts": max(1, int(value.get("max_attempts") or 1)),
            "retry_after": max(0.0, float(value.get("retry_after") or 0)),
        }

    def current(self, email: str, job_id: str) -> dict[str, Any] | None:
        record = load_account_record(self.db_file, email)
        confirmations = record.get("payment_confirmations")
        value = confirmations.get(job_id) if isinstance(confirmations, dict) else None
        if not isinstance(value, dict):
            value = record.get("payment_confirmation")
        if not isinstance(value, dict):
            return None
        if str(value.get("job_id") or "") != job_id:
            return None
        return self.public_outcome(value)

    def refresh_context(self, email: str) -> dict[str, Any]:
        record = load_account_record(self.db_file, email)
        if not record:
            raise RuntimeError("协议支付账号记录不存在，无法刷新新 AT")
        cookies = account_saved_cookies(record)
        if not cookies:
            raise RuntimeError("协议支付账号没有可用于刷新新 AT 的 Cookie")
        proxy_url = account_registration_proxy_url(record)
        if not proxy_url:
            raise RuntimeError("协议支付账号未保存原注册代理，无法用 Cookie 刷新新 AT")
        return {
            "previous_token": account_session_access_token(record),
            "cookies": cookies,
            "proxy_url": proxy_url,
            "storage_state": _json_object(record.get("storage_state_json")),
        }

    def persist(
        self,
        *,
        email: str,
        job_id: str,
        status: str,
        account_type: str,
        plan: str,
        detail: str,
        attempt: int,
        max_attempts: int,
        retry_after: float = 0,
        refreshed_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        checked_at = utc_now()
        connection = connect_db(str(self.db_file))
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?",
                (f"gpt_account:{email}",),
            ).fetchone()
            try:
                record = json.loads(str(row["value"] or "")) if row else {}
            except (json.JSONDecodeError, TypeError, ValueError):
                record = {}
            if not isinstance(record, dict):
                record = {}
            confirmations = record.get("payment_confirmations")
            confirmations = (
                dict(confirmations) if isinstance(confirmations, dict) else {}
            )
            previous = confirmations.get(job_id)
            previous = previous if isinstance(previous, dict) else {}
            if refreshed_result:
                token = str(refreshed_result.get("access_token") or "").strip()
                session_json = str(refreshed_result.get("session_json") or "").strip()
                cookies_json = str(refreshed_result.get("cookies_json") or "").strip()
                storage_state_json = str(
                    refreshed_result.get("storage_state_json") or ""
                ).strip()
                if token:
                    record["access_token"] = token
                if session_json:
                    record["session_json"] = session_json
                    record["session"] = _json_object(session_json)
                if cookies_json:
                    record["cookies_json"] = cookies_json
                    try:
                        cookies = json.loads(cookies_json)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        cookies = []
                    if isinstance(cookies, list):
                        record["cookies"] = cookies
                if storage_state_json:
                    record["storage_state_json"] = storage_state_json
                record["session_acquisition_method"] = str(
                    refreshed_result.get("session_acquisition_method")
                    or "payment_cookie_refresh"
                )
                record.pop("session_invalid_at", None)
            was_refreshed = bool(previous.get("at_refreshed")) or bool(refreshed_result)
            outcome = self.public_outcome(
                {
                    "job_id": job_id,
                    "email": email,
                    "status": status,
                    "protocol_succeeded": True,
                    "plus_confirmed": status == "plus",
                    "payment_succeeded": status == "plus",
                    "at_refreshed": was_refreshed,
                    "at_changed": was_refreshed,
                    "account_type": account_type,
                    "plan": plan,
                    "detail": detail,
                    "checked_at": checked_at,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "retry_after": retry_after,
                }
            )
            if row is None:
                connection.rollback()
                return outcome
            if status == "plus":
                record.update(
                    {
                        "account_type": "plus",
                        "account_type_source": "payment_at_refresh",
                        "verified_at": checked_at,
                        "verification_detail": detail[:1000],
                    }
                )
            confirmations[job_id] = outcome
            if len(confirmations) > 20:
                confirmations = dict(list(confirmations.items())[-20:])
            record.update(
                {
                    "updated_at": checked_at,
                    "payment_confirmation": outcome,
                    "payment_confirmations": confirmations,
                }
            )
            connection.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"gpt_account:{email}", json.dumps(record, ensure_ascii=False)),
            )
            connection.execute(
                "DELETE FROM settings WHERE key = ?", (f"gpt_removed:{email}",)
            )
            connection.commit()
        finally:
            connection.close()
        return outcome


class PaymentCompletionPresenter:
    """Refresh a new AT per payment job and use its plan as final truth."""

    def __init__(
        self,
        model: PaymentCompletionModel,
        *,
        session_refresher: SessionRefresher = refresh_session_with_saved_cookies,
        max_attempts: int = 3,
        retry_delay_seconds: float = 2.0,
    ) -> None:
        self.model = model
        self.session_refresher = session_refresher
        self.max_attempts = max(1, min(int(max_attempts), 5))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self._locks: dict[str, asyncio.Lock] = {}

    async def confirm(self, job: dict[str, Any]) -> dict[str, Any]:
        job = reconcile_openai_protocol_job(job)
        job_id = str(job.get("id") or "").strip()
        email = str(job.get("source_account_email") or "").strip().lower()
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        protocol_succeeded = (
            str(job.get("status") or "") == "completed"
            and str(result.get("status") or "") == "success"
            and str(result.get("settlement_status") or "") == "confirmed"
        )
        if not protocol_succeeded:
            return self.model.public_outcome(
                {
                    "job_id": job_id,
                    "email": email,
                    "status": "refresh_failed",
                    "detail": "协议支付尚未确认到账，禁止刷新 AT 或修改 Plus 标记",
                }
            )
        if not job_id or not email:
            return self.model.public_outcome(
                {
                    "job_id": job_id,
                    "email": email,
                    "status": "refresh_failed",
                    "detail": "协议支付任务缺少账号标识，无法用 Cookie 刷新新 AT",
                }
            )
        current = await asyncio.to_thread(self.model.current, email, job_id)
        if (
            current
            and str(current.get("status") or "") in TERMINAL_CONFIRMATION_STATUSES
        ):
            return current
        if current and float(current.get("retry_after") or 0) > time.time():
            return current
        lock = self._locks.setdefault(email, asyncio.Lock())
        async with lock:
            current = await asyncio.to_thread(self.model.current, email, job_id)
            if (
                current
                and str(current.get("status") or "") in TERMINAL_CONFIRMATION_STATUSES
            ):
                return current
            if current and float(current.get("retry_after") or 0) > time.time():
                return current
            attempt = int((current or {}).get("attempt") or 0) + 1
            previous_token = ""
            proxy_url = ""
            context_loaded = False
            try:
                context = await asyncio.to_thread(self.model.refresh_context, email)
                context_loaded = True
                previous_token = str(context["previous_token"] or "")
                proxy_url = str(context["proxy_url"] or "")
                refreshed = await self.session_refresher(
                    email=email,
                    previous_token=previous_token,
                    cookies=context["cookies"],
                    proxy_url=proxy_url,
                    storage_state=context["storage_state"],
                )
                if not isinstance(refreshed, dict):
                    raise RuntimeError("Cookie 刷新新 AT 返回格式无效")
                session = _json_object(refreshed.get("session_json"))
                token = validate_refreshed_session(
                    expected_email=email,
                    previous_token=previous_token,
                    session=session,
                )
                returned_token = str(refreshed.get("access_token") or "").strip()
                if returned_token and returned_token != token:
                    raise RuntimeError("Cookie 刷新结果中的新 AT 与 Session 不一致")
                account_type, raw_plan = jwt_account_type(token)
                stored_type = (
                    account_type if account_type in {"plus", "free"} else "unverified"
                )
                plus_confirmed = account_type == "plus"
                exhausted = attempt >= self.max_attempts
                status = (
                    "plus"
                    if plus_confirmed
                    else "not_plus"
                    if exhausted
                    else "retrying"
                )
                detail = (
                    f"支付后 Cookie 已刷新新 AT；JWT chatgpt_plan_type={raw_plan}，已确认 Plus"
                    if plus_confirmed
                    else f"第 {attempt}/{self.max_attempts} 次刷新后新 AT 套餐="
                    f"{raw_plan or '无法识别'}；"
                    + ("未确认 Plus" if exhausted else "等待套餐传播后重试")
                )
                refreshed_result = {
                    **refreshed,
                    "access_token": token,
                    "session_json": json.dumps(session, ensure_ascii=False),
                    "session_acquisition_method": "payment_cookie_refresh",
                }
                return await asyncio.to_thread(
                    self.model.persist,
                    email=email,
                    job_id=job_id,
                    status=status,
                    account_type=stored_type,
                    plan=raw_plan,
                    detail=detail,
                    attempt=attempt,
                    max_attempts=self.max_attempts,
                    retry_after=(
                        time.time() + self.retry_delay_seconds
                        if status == "retrying"
                        else 0
                    ),
                    refreshed_result=refreshed_result,
                )
            except Exception as error:
                detail = _public_error(error, previous_token, proxy_url)
                retrying = context_loaded and attempt < self.max_attempts
                if retrying:
                    detail = (
                        f"第 {attempt}/{self.max_attempts} 次 Cookie 刷新新 AT 失败："
                        f"{detail}；稍后自动重试"
                    )
                return await asyncio.to_thread(
                    self.model.persist,
                    email=email,
                    job_id=job_id,
                    status="retrying" if retrying else "refresh_failed",
                    account_type="unverified",
                    plan="",
                    detail=detail,
                    attempt=attempt,
                    max_attempts=self.max_attempts,
                    retry_after=(
                        time.time() + self.retry_delay_seconds if retrying else 0
                    ),
                )


__all__ = [
    "PaymentCompletionModel",
    "PaymentCompletionPresenter",
    "reconcile_openai_protocol_job",
]
