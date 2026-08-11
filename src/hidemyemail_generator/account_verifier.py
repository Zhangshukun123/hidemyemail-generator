from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import urllib.request
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import aiohttp

from .browser_tasks import (
    BrowserTaskManager,
    _save_account_record,
    account_registration_proxy_url,
    account_saved_cookies,
    account_session,
    account_session_access_token,
    access_token_is_expired,
    decode_jwt_payload,
    jwt_account_type,
    load_account_record,
    session_account_type,
    session_email,
)
from .inbox import connect_db


EVENT_PREFIX = "HME_VERIFY_EVENT:"
PROTOCOL_EVENT_PREFIX = "HME_PROTOCOL_EVENT:"
MAX_LOG_ITEMS = 300
MAX_HISTORY_LOG_ITEMS = 1000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _claim_strings(container: Any, *keys: str) -> set[str]:
    if not isinstance(container, dict):
        return set()
    values: set[str] = set()
    for key in keys:
        value = container.get(key)
        if isinstance(value, (list, tuple, set)):
            values.update(str(item).strip().lower() for item in value if str(item).strip())
        elif str(value or "").strip():
            values.add(str(value).strip().lower())
    return values


def _token_identity(token: str) -> tuple[set[str], set[str]]:
    payload = decode_jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth")
    profile = payload.get("https://api.openai.com/profile")
    emails = _claim_strings(payload, "email")
    emails.update(_claim_strings(profile, "email"))
    emails.update(_claim_strings(auth, "email"))
    identities = _claim_strings(
        payload,
        "account_id",
        "accountId",
        "workspace_id",
        "workspaceId",
        "organization_id",
    )
    identities.update(
        _claim_strings(
            auth,
            "chatgpt_account_id",
            "account_id",
            "accountId",
            "workspace_id",
            "workspaceId",
            "organization_id",
        )
    )
    return emails, identities


def _session_identities(session: Any) -> set[str]:
    if not isinstance(session, dict):
        return set()
    identities = _claim_strings(
        session, "account_id", "accountId", "workspace_id", "workspaceId"
    )
    identities.update(
        _claim_strings(
            session.get("account"),
            "id",
            "account_id",
            "accountId",
            "workspace_id",
            "workspaceId",
        )
    )
    return identities


def validate_refreshed_session(
    *, expected_email: str, previous_token: str, session: dict[str, Any]
) -> str:
    """Validate an untrusted refreshed Session before it may replace saved data."""

    target = str(expected_email or "").strip().lower()
    token = str(session.get("accessToken") or session.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("刷新 Session 未返回 Access Token")
    if previous_token and token == previous_token:
        raise RuntimeError("刷新 Session 返回的仍是旧 Access Token")
    if access_token_is_expired(token):
        raise RuntimeError("刷新 Session 返回的 Access Token 已过期")

    owner = session_email(session)
    if not owner or owner != target:
        raise RuntimeError(f"刷新 Session 账号不匹配：{owner or '缺少邮箱'}")
    token_emails, token_identities = _token_identity(token)
    if not token_emails or target not in token_emails:
        raise RuntimeError("新 Access Token 的邮箱声明与账号不匹配")
    if not token_identities:
        raise RuntimeError("新 Access Token 缺少 account_id/workspace 声明")

    _old_emails, previous_identities = _token_identity(previous_token)
    if previous_identities and token_identities.isdisjoint(previous_identities):
        raise RuntimeError("新 Access Token 的 account_id/workspace 与原账号不匹配")
    session_identities = _session_identities(session)
    if session_identities and token_identities.isdisjoint(session_identities):
        raise RuntimeError("新 Access Token 的 account_id/workspace 与 Session 不匹配")
    return token


def _merge_response_cookies(
    cookies: list[dict[str, Any]], set_cookie_headers: list[str]
) -> list[dict[str, Any]]:
    merged = {
        (
            str(item.get("name") or ""),
            str(item.get("domain") or "chatgpt.com"),
            str(item.get("path") or "/"),
        ): dict(item)
        for item in cookies
        if str(item.get("name") or "").strip()
    }
    for header in set_cookie_headers:
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except Exception:
            continue
        for name, morsel in parsed.items():
            domain = str(morsel["domain"] or "chatgpt.com")
            path = str(morsel["path"] or "/")
            expires = -1.0
            if str(morsel["max-age"] or "").strip():
                try:
                    expires = datetime.now(timezone.utc).timestamp() + float(
                        morsel["max-age"]
                    )
                except (TypeError, ValueError):
                    expires = -1.0
            elif str(morsel["expires"] or "").strip():
                try:
                    expires = parsedate_to_datetime(morsel["expires"]).timestamp()
                except (TypeError, ValueError, OverflowError):
                    expires = -1.0
            same_site = str(morsel["samesite"] or "Lax").title()
            if same_site not in {"Lax", "Strict", "None"}:
                same_site = "Lax"
            merged[(name, domain, path)] = {
                "name": name,
                "value": str(morsel.value),
                "domain": domain,
                "path": path,
                "expires": expires,
                "httpOnly": bool(morsel["httponly"]),
                "secure": bool(morsel["secure"]),
                "sameSite": same_site,
            }
    return list(merged.values())


async def refresh_session_with_saved_cookies(
    *,
    email: str,
    previous_token: str,
    cookies: list[dict[str, Any]],
    proxy_url: str,
    storage_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch a candidate Session twice through the account's registration proxy."""

    if not cookies:
        raise RuntimeError("该账号没有可用于刷新 Session 的 Cookie")
    if not str(proxy_url or "").strip():
        raise RuntimeError("该账号未保存原注册代理，无法按原出口刷新 Session")

    headers = {
        "Accept": "application/json",
        "Cache-Control": "no-cache, no-store",
        "Pragma": "no-cache",
        "Referer": "https://chatgpt.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136 Safari/537.36",
    }
    sessions: list[dict[str, Any]] = []
    errors: list[str] = []
    set_cookie_headers: list[str] = []
    current_cookies = [dict(cookie) for cookie in cookies]
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as client:
        for url in (
            "https://chatgpt.com/api/auth/session",
            "https://chatgpt.com/api/auth/session?refresh=true",
        ):
            try:
                async with client.get(
                    url,
                    proxy=str(proxy_url).strip(),
                    allow_redirects=False,
                    headers={
                        "Cookie": "; ".join(
                            f"{str(cookie.get('name') or '').strip()}={str(cookie.get('value') or '')}"
                            for cookie in current_cookies
                            if str(cookie.get("name") or "").strip()
                        )
                    },
                ) as response:
                    response_cookie_headers = response.headers.getall("Set-Cookie", [])
                    set_cookie_headers.extend(response_cookie_headers)
                    current_cookies = _merge_response_cookies(
                        current_cookies, response_cookie_headers
                    )
                    if response.status != 200:
                        errors.append(
                            f"{url.rsplit('/', 1)[-1]} 返回 HTTP {response.status}"
                        )
                        continue
                    payload = await response.json(content_type=None)
                    if isinstance(payload, dict):
                        sessions.append(payload)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                errors.append(str(error)[:200])
        candidate = next(
            (
                value
                for value in reversed(sessions)
                if str(value.get("accessToken") or value.get("access_token") or "").strip()
            ),
            None,
        )
        if candidate is None:
            raise RuntimeError("Cookie 刷新未返回有效 Session" + (f"：{'；'.join(errors)}" if errors else ""))
        token = validate_refreshed_session(
            expected_email=email,
            previous_token=previous_token,
            session=candidate,
        )
        refreshed_cookies = _merge_response_cookies(current_cookies, set_cookie_headers)

    if not refreshed_cookies:
        raise RuntimeError("Cookie 刷新成功但未返回可保存的 Cookie")
    origins = []
    if isinstance(storage_state, dict) and isinstance(storage_state.get("origins"), list):
        origins = list(storage_state["origins"])
    return {
        "access_token": token,
        "session_json": json.dumps(candidate, ensure_ascii=False),
        "cookies_json": json.dumps(refreshed_cookies, ensure_ascii=False),
        "storage_state_json": json.dumps(
            {"cookies": refreshed_cookies, "origins": origins}, ensure_ascii=False
        ),
        "session_acquisition_method": "cookie_session_refresh",
    }


def load_verifiable_accounts(db_file: Path) -> list[dict[str, Any]]:
    conn = connect_db(str(db_file))
    try:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'gpt_account:%'"
        ).fetchall()
    finally:
        conn.close()
    accounts: list[dict[str, Any]] = []
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
        if email and session and token:
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
                    "two_factor": (
                        record.get("two_factor")
                        if isinstance(record.get("two_factor"), dict)
                        else {}
                    ),
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
        protocol_project_dir: Path | None = None,
        node_executable: Path | None = None,
        protocol_bridge_file: Path | None = None,
        code_service_url: str = "",
        code_service_token: str = "",
        browser_manager: BrowserTaskManager | None = None,
        delete_invalid_email: Callable[[str, str], Awaitable[str]] | None = None,
        cookie_session_refresher: Callable[..., Awaitable[dict[str, Any]]]
        | None = None,
    ) -> None:
        self.target_project_dir = target_project_dir.resolve()
        self.db_file = db_file.resolve()
        self.python_executable = python_executable.resolve()
        self.bridge_file = (
            bridge_file or Path(__file__).with_name("openai_account_check_bridge.py")
        ).resolve()
        self.protocol_project_dir = (
            protocol_project_dir
            or self.target_project_dir.parent / "chatgpt-session-forge"
        ).resolve()
        detected_node = str(node_executable or shutil.which("node") or "").strip()
        self.node_executable = Path(detected_node).resolve() if detected_node else None
        self.protocol_bridge_file = (
            protocol_bridge_file
            or Path(__file__).with_name("openai_protocol_login_bridge.js")
        ).resolve()
        self.code_service_url = str(code_service_url or "").strip().rstrip("/")
        self.code_service_token = str(code_service_token or "")
        self.browser_manager = browser_manager
        self.delete_invalid_email = delete_invalid_email
        self.cookie_session_refresher = (
            cookie_session_refresher or refresh_session_with_saved_cookies
        )
        self._batch_task: asyncio.Task | None = None
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._browser_refresh_lock = asyncio.Lock()
        self._state: dict[str, Any] = self._idle_state()
        self._history_logs: list[dict[str, Any]] = []

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

    def protocol_availability(self) -> dict[str, Any]:
        missing = list(self.availability()["errors"])
        if not self.protocol_project_dir.is_dir():
            missing.append(f"协议登录项目不存在：{self.protocol_project_dir}")
        if not (
            self.protocol_project_dir / "services" / "chatgpt-service.js"
        ).is_file():
            missing.append("协议登录项目缺少 services/chatgpt-service.js")
        if not self.node_executable or not self.node_executable.is_file():
            missing.append("未找到 Node.js，无法执行协议登录")
        if not self.protocol_bridge_file.is_file():
            missing.append("当前项目缺少协议登录桥接脚本")
        if not self.code_service_url or not self.code_service_token:
            missing.append("本地验证码服务尚未配置")
        return {"available": not missing, "errors": missing}

    def snapshot(self) -> dict[str, Any]:
        return {
            **{key: value for key, value in self._state.items() if key != "accounts"},
            "accounts": [
                {key: value for key, value in item.items() if not key.startswith("_")}
                for item in self._state.get("accounts", [])
            ],
            "historyLogs": list(self._history_logs),
            "runtime": self.availability(),
        }

    def start(
        self,
        *,
        concurrency: int = 3,
        emails: list[str] | set[str] | None = None,
        force_online: bool = False,
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
        concurrency = max(1, min(10, int(concurrency)))
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
                    "_two_factor": item.get("two_factor") or {},
                    "_force_online": bool(force_online),
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

    def start_protocol_relogin(
        self, *, email: str, headless: bool = False
    ) -> dict[str, Any]:
        if self._batch_task and not self._batch_task.done():
            raise RuntimeError("账号验证任务正在运行")
        runtime = self.protocol_availability()
        if not runtime["available"]:
            raise RuntimeError("；".join(runtime["errors"]))
        target = str(email or "").strip().lower()
        if not target or "@" not in target:
            raise RuntimeError("邮箱地址无效")
        record = load_account_record(self.db_file, target)
        account_type = str(record.get("account_type") or "").strip().lower()
        account_type_source = str(
            record.get("account_type_source") or ""
        ).strip().lower()
        saved_two_factor = (
            record.get("two_factor")
            if isinstance(record.get("two_factor"), dict)
            else {}
        )
        self._state = {
            "id": uuid.uuid4().hex,
            "status": "running",
            "running": True,
            "concurrency": 1,
            "total": 1,
            "completed": 0,
            "plus": 0,
            "free": 0,
            "expired": 0,
            "deleted": 0,
            "failed": 0,
            "accounts": [
                {
                    "email": target,
                    "status": "queued",
                    "message": "等待协议登录",
                    "_protocol_relogin": True,
                    "_headless": bool(headless),
                    "_password": str(record.get("password") or ""),
                    "_two_factor": saved_two_factor,
                    "_access_token": "",
                    "_account_type": account_type,
                    "_account_type_source": account_type_source,
                    "_session_email": "",
                    "_session_plan": "",
                }
            ],
            "logs": [],
            "startedAt": utc_now(),
            "finishedAt": "",
        }
        self._append_log(f"协议重新登录已启动：{target}")
        self._batch_task = asyncio.create_task(self._run_batch(1))
        return self.snapshot()

    def start_with_browser(
        self,
        *,
        emails: list[str] | set[str],
        concurrency: int = 3,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Refresh Sessions in one headless browser batch, then verify."""
        if self._batch_task and not self._batch_task.done():
            raise RuntimeError("账号验证任务正在运行")
        runtime = self.availability()
        if not runtime["available"]:
            raise RuntimeError("；".join(runtime["errors"]))
        if self.browser_manager is None:
            raise RuntimeError("无头浏览器运行环境不可用")

        targets = sorted(
            {
                str(value or "").strip().lower()
                for value in emails
                if str(value or "").strip()
            }
        )
        if not targets:
            raise RuntimeError("没有需要验证的账号")
        concurrency = max(1, min(10, int(concurrency)))
        accounts: list[dict[str, Any]] = []
        refresh_count = 0
        for email in targets:
            record = load_account_record(self.db_file, email)
            session = account_session(record)
            token = account_session_access_token(record)
            refresh_session = (
                bool(force_refresh)
                or not session
                or not token
                or access_token_is_expired(token)
                or bool(record.get("session_invalid_at"))
            )
            refresh_count += int(refresh_session)
            account_type = str(record.get("account_type") or "").strip().lower()
            account_type_source = str(
                record.get("account_type_source") or ""
            ).strip().lower()
            session_type, raw_session_plan = session_account_type(session)
            if session_type and account_type_source != "manual":
                account_type = session_type
                account_type_source = "session"
            saved_two_factor = (
                record.get("two_factor")
                if isinstance(record.get("two_factor"), dict)
                else {}
            )
            accounts.append(
                {
                    "email": email,
                    "status": "queued",
                    "message": (
                        "等待使用已保存 Cookie 重新获取 Session"
                        if force_refresh
                        else "等待无头浏览器重新获取 Session"
                        if refresh_session
                        else "等待验证"
                    ),
                    "_refresh_session": refresh_session,
                    "_cookie_refresh_only": bool(force_refresh),
                    "_access_token": token,
                    "_account_type": account_type,
                    "_account_type_source": account_type_source,
                    "_session_email": session_email(session),
                    "_session_plan": raw_session_plan,
                    "_two_factor": saved_two_factor,
                }
            )

        if refresh_count:
            browser_runtime = self.browser_manager.availability()
            if not browser_runtime.get("available"):
                raise RuntimeError("；".join(browser_runtime.get("errors", [])))
        self._state = {
            "id": uuid.uuid4().hex,
            "status": "running",
            "running": True,
            "headless": True,
            "refreshSource": "cookie" if force_refresh else "session_if_needed",
            "concurrency": concurrency,
            "total": len(accounts),
            "completed": 0,
            "plus": 0,
            "free": 0,
            "expired": 0,
            "deleted": 0,
            "failed": 0,
            "accounts": accounts,
            "logs": [],
            "startedAt": utc_now(),
            "finishedAt": "",
        }
        self._append_log(
            f"{'Cookie 刷新' if force_refresh else '无头浏览器验证'}已启动："
            f"{len(accounts)} 个账号，"
            f"刷新 Session {refresh_count} 个，并发 {concurrency}"
        )
        self._batch_task = asyncio.create_task(
            self._refresh_sessions_and_verify(concurrency)
        )
        return self.snapshot()

    @staticmethod
    def _clear_private_item_fields(item: dict[str, Any]) -> None:
        for key in tuple(item):
            if key.startswith("_"):
                item.pop(key, None)

    def _fail_session_refresh(self, item: dict[str, Any], message: str) -> None:
        item.update(status="failed", message=str(message or "无头浏览器未返回 Session")[:500])
        self._state["failed"] += 1
        self._state["completed"] += 1
        self._append_log(f"Session 获取失败，账号已保留：{item['message']}", email=item["email"])
        self._clear_private_item_fields(item)

    async def _refresh_sessions_and_verify(self, concurrency: int) -> None:
        refresh_items = [
            item for item in self._state["accounts"] if item.get("_refresh_session")
        ]
        try:
            if refresh_items:
                for item in refresh_items:
                    item.update(
                        status="running",
                        message=(
                            "正在加载保存 Cookie 并重新获取 Session"
                            if item.get("_cookie_refresh_only")
                            else "正在启动无头浏览器获取 Session"
                        ),
                    )
                self._append_log(
                    f"正在启动 {len(refresh_items)} 个账号的"
                    f"{'Cookie 刷新' if any(item.get('_cookie_refresh_only') for item in refresh_items) else '无头浏览器'}"
                    "进程，"
                    f"并发 {concurrency}"
                )
                self.browser_manager.start(
                    [
                        {
                            "email": item["email"],
                            "password": "",
                            "ensure_password": False,
                            "force_reset_password": False,
                            "enable_2fa": False,
                            "cookie_refresh_only": bool(
                                item.get("_cookie_refresh_only")
                            ),
                            "two_factor": item.get("_two_factor") or {},
                        }
                        for item in refresh_items
                    ],
                    headless=True,
                    concurrency=concurrency,
                )
                browser_task = await self.browser_manager.wait()
                browser_accounts = {
                    str(account.get("email") or "").strip().lower(): account
                    for account in browser_task.get("accounts", [])
                    if isinstance(account, dict)
                }
                for item in refresh_items:
                    email = str(item["email"])
                    browser_account = browser_accounts.get(email, {})
                    if browser_account.get("status") != "success":
                        self._fail_session_refresh(
                            item,
                            str(
                                browser_account.get("message")
                                or browser_account.get("latestLog")
                                or "无头浏览器未返回 Session"
                            ),
                        )
                        continue
                    record = await asyncio.to_thread(
                        load_account_record, self.db_file, email
                    )
                    session = account_session(record)
                    token = account_session_access_token(record)
                    if not session or not token:
                        self._fail_session_refresh(item, "无头浏览器未保存有效 Session")
                        continue
                    owner = session_email(session)
                    if owner and owner != email:
                        self._fail_session_refresh(
                            item, f"浏览器返回的 Session 账号不匹配：{owner}"
                        )
                        continue
                    session_type, session_plan = session_account_type(session)
                    if item.get("_account_type_source") != "manual" and session_type:
                        item["_account_type"] = session_type
                        item["_account_type_source"] = "session"
                    item.update(
                        status="queued",
                        message="Session 已获取，等待验证",
                        _access_token=token,
                        _session_email=owner,
                        _session_plan=session_plan,
                        _refresh_session=False,
                    )
                    self._append_log(
                        "已使用保存 Cookie 获取最新 Session 与账号状态"
                        if item.get("_cookie_refresh_only")
                        else "无头浏览器已获取 Session",
                        email=email,
                    )
        except asyncio.CancelledError:
            if self.browser_manager.snapshot().get("running"):
                await self.browser_manager.stop()
            self._state["status"] = "cancelled"
            self._state["running"] = False
            self._state["finishedAt"] = utc_now()
            self._append_log("账号验证任务已停止")
            raise
        except Exception as error:
            for item in refresh_items:
                if item.get("status") != "failed":
                    self._fail_session_refresh(item, str(error))

        await self._run_batch(concurrency)

    async def _browser_relogin(
        self,
        item: dict[str, Any],
        email: str,
        protocol_error: str,
        *,
        force_headless: bool = False,
    ) -> tuple[bool, str]:
        if self.browser_manager is None:
            return False, protocol_error
        previous_token = str(item.get("_access_token") or "").strip()
        item.update(
            status="running",
            message=(
                "正在使用无头浏览器自动提取新的 Session"
                if force_headless
                else "协议登录不可用，正在改用浏览器重新获取 Session"
            ),
        )
        self._append_log(item["message"], email=email)
        try:
            self.browser_manager.start(
                [
                    {
                        "email": email,
                        "password": "",
                        "ensure_password": False,
                        "force_reset_password": False,
                        "enable_2fa": False,
                        "two_factor": item.get("_two_factor") or {},
                    }
                ],
                headless=bool(force_headless or item.get("_headless", False)),
                concurrency=1,
            )
            browser_task = await self.browser_manager.wait()
        except asyncio.CancelledError:
            await self.browser_manager.stop()
            raise
        except RuntimeError as error:
            return False, f"{protocol_error}；浏览器重新获取失败：{error}"

        browser_accounts = browser_task.get("accounts", [])
        browser_account = next(
            (
                account
                for account in browser_accounts
                if str(account.get("email") or "").strip().lower() == email
            ),
            {},
        )
        if browser_account.get("status") != "success":
            detail = str(
                browser_account.get("message")
                or browser_account.get("latestLog")
                or "浏览器任务未成功提取 Session"
            ).strip()
            return False, f"{protocol_error}；浏览器重新获取失败：{detail}"
        record = await asyncio.to_thread(load_account_record, self.db_file, email)
        session = account_session(record)
        token = account_session_access_token(record)
        if not session or not token:
            detail = str(
                browser_account.get("message")
                or browser_account.get("latestLog")
                or "浏览器任务未返回 Session"
            ).strip()
            return False, f"{protocol_error}；浏览器重新获取失败：{detail}"
        if previous_token and token == previous_token:
            return False, f"{protocol_error}；浏览器重新获取后 Access Token 未更新"

        owner = session_email(session)
        if owner and owner != email:
            return False, f"浏览器返回的 Session 账号不匹配：{owner}"
        session_type, session_plan = session_account_type(session)
        if item.get("_account_type_source") != "manual" and session_type:
            item["_account_type"] = session_type
            item["_account_type_source"] = "session"
        item["_access_token"] = token
        item["_session_email"] = owner
        item["_session_plan"] = session_plan
        item["message"] = "浏览器已重新获取 Session，正在验证账号"
        self._append_log(item["message"], email=email)
        return True, ""

    async def _refresh_invalid_token_with_cookie(
        self, item: dict[str, Any], email: str
    ) -> tuple[bool, str]:
        """Prepare, but do not persist, a Cookie-refreshed Session candidate."""

        record = await asyncio.to_thread(load_account_record, self.db_file, email)
        cookies = account_saved_cookies(record)
        if not cookies:
            return False, "该账号没有保存 Cookie"
        proxy_url = account_registration_proxy_url(record)
        proxy_label = "原注册出口"
        if not proxy_url:
            proxy_store = getattr(self.browser_manager, "registration_proxy_store", None)
            try:
                proxy_state = (
                    proxy_store.public_state() if proxy_store is not None else {}
                )
            except Exception:
                proxy_state = {}
            fallback_url = str(proxy_state.get("endpoint") or "").strip()
            if (
                proxy_state.get("enabled")
                and proxy_state.get("configured")
                and proxy_state.get("mode") == "clash"
                and fallback_url.startswith(("http://", "https://", "socks5://"))
            ):
                proxy_url = fallback_url
                proxy_label = "当前 Clash 注册代理入口（旧记录未保存当次出口）"
            else:
                return False, "该账号未保存原注册代理"
        raw_storage_state = record.get("storage_state_json")
        if isinstance(raw_storage_state, str) and raw_storage_state.strip():
            try:
                raw_storage_state = json.loads(raw_storage_state)
            except (json.JSONDecodeError, TypeError, ValueError):
                raw_storage_state = {}
        storage_state = raw_storage_state if isinstance(raw_storage_state, dict) else {}
        previous_token = str(item.get("_access_token") or "").strip()
        try:
            candidate_result = await self.cookie_session_refresher(
                email=email,
                previous_token=previous_token,
                cookies=cookies,
                proxy_url=proxy_url,
                storage_state=storage_state,
            )
            if not isinstance(candidate_result, dict):
                raise RuntimeError("Cookie 刷新返回格式无效")
            raw_session = candidate_result.get("session_json")
            if isinstance(raw_session, str):
                session = json.loads(raw_session)
            else:
                session = raw_session
            if not isinstance(session, dict):
                raise RuntimeError("Cookie 刷新未返回有效 Session")
            token = validate_refreshed_session(
                expected_email=email,
                previous_token=previous_token,
                session=session,
            )
            raw_cookies = candidate_result.get("cookies_json")
            parsed_cookies = json.loads(raw_cookies) if isinstance(raw_cookies, str) else raw_cookies
            if not isinstance(parsed_cookies, list) or not parsed_cookies:
                raise RuntimeError("Cookie 刷新未返回可保存的新 Cookie")
        except (RuntimeError, json.JSONDecodeError, TypeError, ValueError) as error:
            return False, str(error)[:500]

        session_type, session_plan = session_account_type(session)
        if item.get("_account_type_source") != "manual" and session_type:
            item["_account_type"] = session_type
            item["_account_type_source"] = "session"
        candidate_result = {
            **candidate_result,
            "access_token": token,
            "session_json": json.dumps(session, ensure_ascii=False),
            "cookies_json": json.dumps(parsed_cookies, ensure_ascii=False),
        }
        item["_pending_session_result"] = candidate_result
        item["_access_token"] = token
        item["_session_email"] = session_email(session)
        item["_session_plan"] = session_plan
        item["message"] = f"已通过{proxy_label}取得候选 Session，正在复验新 Access Token"
        self._append_log(item["message"], email=email)
        return True, ""

    def _append_log(
        self, message: str, *, email: str = "", level: str = "info"
    ) -> None:
        entry = {
            "at": utc_now(),
            "taskId": str(self._state.get("id") or ""),
            "email": email,
            "level": level if level in {"info", "success", "warning", "error"} else "info",
            "message": str(message)[:1000],
        }
        logs = self._state.setdefault("logs", [])
        logs.append(entry)
        if len(logs) > MAX_LOG_ITEMS:
            del logs[:-MAX_LOG_ITEMS]
        self._history_logs.append(dict(entry))
        if len(self._history_logs) > MAX_HISTORY_LOG_ITEMS:
            del self._history_logs[:-MAX_HISTORY_LOG_ITEMS]

    def record_invalid_email_deleted(self, email: str, reason: str) -> dict[str, Any]:
        """Expose a failed preflight check in the same UI used for verification logs."""
        if self._batch_task and not self._batch_task.done():
            raise RuntimeError("账号验证任务正在运行")
        target = str(email or "").strip().lower()
        detail = str(reason or "iCloud 邮箱无效或已停用")[:1000]
        now = utc_now()
        self._state = {
            "id": uuid.uuid4().hex,
            "status": "completed",
            "running": False,
            "concurrency": 1,
            "total": 1,
            "completed": 1,
            "plus": 0,
            "free": 0,
            "expired": 1,
            "deleted": 1,
            "failed": 0,
            "accounts": [
                {
                    "email": target,
                    "status": "deleted",
                    "sessionStatus": "expired",
                    "message": detail,
                }
            ],
            "logs": [],
            "startedAt": now,
            "finishedAt": now,
        }
        self._append_log(f"验证前检查失败：{detail}", email=target, level="error")
        self._append_log("无效邮箱及本地账号凭据已自动删除", email=target, level="warning")
        return self.snapshot()

    async def _delete_confirmed_invalid_email(self, email: str, reason: str) -> str:
        if self.delete_invalid_email is not None:
            return str(await self.delete_invalid_email(email, reason) or "邮箱已删除")
        await asyncio.to_thread(remove_invalid_account, self.db_file, email, reason)
        return "本地邮箱记录及账号凭据已删除"

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
                    f"Token 失效 {self._state['expired']}，"
                    f"已删除 {self._state['deleted']}，失败 {self._state['failed']}",
                    level=("warning" if self._state["expired"] else "success"),
                )
        except asyncio.CancelledError:
            self._state["status"] = "cancelled"
            self._append_log("账号验证任务已停止")
            raise
        finally:
            self._state["running"] = False
            self._state["finishedAt"] = utc_now()
            self._processes.clear()

    @staticmethod
    def _protocol_environment_proxy(env: dict[str, str]) -> None:
        proxies = urllib.request.getproxies()
        http_proxy = str(proxies.get("http") or "").strip()
        https_proxy = str(proxies.get("https") or http_proxy).strip()
        if http_proxy:
            env.setdefault("HTTP_PROXY", http_proxy)
        if https_proxy:
            env.setdefault("HTTPS_PROXY", https_proxy)
        current_no_proxy = str(env.get("NO_PROXY") or "").strip()
        local_hosts = "127.0.0.1,localhost"
        env["NO_PROXY"] = (
            f"{current_no_proxy},{local_hosts}" if current_no_proxy else local_hosts
        )

    @staticmethod
    def _protocol_event(stdout: bytes) -> dict[str, Any]:
        event: dict[str, Any] = {}
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            if not line.startswith(PROTOCOL_EVENT_PREFIX):
                continue
            try:
                candidate = json.loads(line[len(PROTOCOL_EVENT_PREFIX) :])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                event = candidate
        return event

    async def _protocol_relogin(
        self, item: dict[str, Any], email: str
    ) -> tuple[bool, str]:
        item.update(
            status="running",
            message="正在使用协议重新登录（不会启动浏览器）",
        )
        self._append_log(item["message"], email=email)
        password = str(item.get("_password") or "")
        env = os.environ.copy()
        env.update(
            {
                "HME_PROTOCOL_EMAIL": email,
                "HME_PROTOCOL_PASSWORD": password,
                "HME_PROTOCOL_PROJECT_DIR": str(self.protocol_project_dir),
                "HME_CODE_SERVICE_URL": self.code_service_url,
                "HME_CODE_SERVICE_TOKEN": self.code_service_token,
                "NODE_USE_ENV_PROXY": "1",
            }
        )
        self._protocol_environment_proxy(env)
        command = [str(self.node_executable)]
        if self.node_executable and self.node_executable.name.casefold() in {
            "node",
            "node.exe",
        }:
            command.append("--use-env-proxy")
        command.append(str(self.protocol_bridge_file))
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.protocol_project_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
                limit=2 * 1024 * 1024,
            )
            self._processes[email] = process
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=240
                )
            except asyncio.TimeoutError:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                return False, "协议登录超时，请稍后重试"
            return_code = process.returncode
        except asyncio.CancelledError:
            item.update(status="cancelled", message="任务已停止")
            raise
        except Exception as error:
            stdout, stderr, return_code = b"", str(error).encode(), -1
        finally:
            self._processes.pop(email, None)

        event = self._protocol_event(stdout)
        if return_code != 0 or event.get("status") != "success":
            error = str(event.get("detail") or "").strip()
            if not error:
                error = stderr.decode("utf-8", errors="replace").strip()
            for secret in (password, self.code_service_token):
                if secret:
                    error = error.replace(secret, "[REDACTED]")
            return False, (error or "协议登录失败")[:500]

        session = event.get("session")
        if not isinstance(session, dict):
            return False, "协议登录没有返回有效 Session"
        token = str(session.get("accessToken") or session.get("access_token") or "").strip()
        if not token:
            return False, "协议登录返回的 Session 缺少 Access Token"
        owner = session_email(session)
        if owner and owner != email:
            return False, f"协议登录返回的 Session 账号不匹配：{owner}"

        await asyncio.to_thread(
            _save_account_record,
            self.db_file,
            email,
            result={
                "access_token": token,
                "session_json": json.dumps(session, ensure_ascii=False),
                "session_acquisition_method": "protocol_login",
            },
        )
        session_type, session_plan = session_account_type(session)
        if item.get("_account_type_source") != "manual" and session_type:
            item["_account_type"] = session_type
            item["_account_type_source"] = "session"
        item["_access_token"] = token
        item["_session_email"] = owner
        item["_session_plan"] = session_plan
        item["message"] = "协议登录成功，正在验证账号"
        self._append_log(item["message"], email=email)
        return True, ""

    async def _check_access_token(
        self,
        item: dict[str, Any],
        email: str,
        token: str,
        *,
        force_online: bool = False,
    ) -> tuple[int, dict[str, Any], bytes]:
        event: dict[str, Any] = {}
        stderr = b""
        jwt_type, jwt_plan = jwt_account_type(token)
        if not force_online and jwt_type == "plus" and not access_token_is_expired(token):
            event = {
                "status": "plus",
                "detail": f"JWT chatgpt_plan_type={jwt_plan}（本地快速验证）",
            }
            item["message"] = "JWT 已确认 Plus"
            self._append_log(item["message"], email=email)
            return 0, event, stderr

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

        for line in stdout.decode("utf-8", errors="replace").splitlines():
            if not line.startswith(EVENT_PREFIX):
                continue
            try:
                candidate = json.loads(line[len(EVENT_PREFIX) :])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                event = candidate
        return return_code, event, stderr

    async def _run_account(
        self, item: dict[str, Any], semaphore: asyncio.Semaphore
    ) -> None:
        email = str(item["email"])
        async with semaphore:
            if item.get("status") in {"failed", "cancelled"}:
                return
            if self._state.get("status") == "cancelling":
                item.update(status="cancelled", message="任务已停止")
                return
            if item.get("_protocol_relogin"):
                succeeded, error = await self._protocol_relogin(item, email)
                if not succeeded:
                    succeeded, error = await self._browser_relogin(
                        item, email, error
                    )
                if not succeeded:
                    item.update(status="failed", message=error)
                    self._state["failed"] += 1
                    self._state["completed"] += 1
                    self._append_log(
                        f"协议登录失败，原 Session 已保留：{error}", email=email
                    )
                    for key in tuple(item):
                        if key.startswith("_"):
                            item.pop(key, None)
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
            automatic_refresh_attempted = False
            automatic_refresh_succeeded = False
            automatic_refresh_error = ""
            cookie_refresh_attempted = False
            result = "error"
            detail = ""
            stderr = b""
            return_code = 0
            token = ""
            for verification_attempt in range(2):
                item.update(
                    status="running",
                    message=(
                        "正在重新校验自动提取的 Session"
                        if verification_attempt
                        else "正在根据 Session 检查账号"
                    ),
                )
                self._append_log(item["message"], email=email)
                token = str(item.get("_access_token") or "")
                return_code, event, stderr = await self._check_access_token(
                    item,
                    email,
                    token,
                    force_online=bool(
                        item.get("_force_online")
                        or (
                            verification_attempt
                            and item.get("_pending_session_result")
                        )
                    ),
                )
                result = str(event.get("status") or "error")
                detail = str(event.get("detail") or "").strip()
                safe_detail = detail.replace(token, "[REDACTED]") if token else detail
                response_log = (
                    f"验证响应：status={result}，exitCode={return_code}"
                    + (f"；{safe_detail}" if safe_detail else "；未返回详细说明")
                )
                self._append_log(
                    response_log,
                    email=email,
                    level=(
                        "success"
                        if return_code == 0 and result in {"plus", "free"}
                        else "warning"
                        if return_code == 0 and result == "invalid"
                        else "error"
                    ),
                )
                session_owner = str(
                    item.get("_session_email") or ""
                ).strip().lower()
                session_detail = ""
                if session_owner:
                    session_detail = f"Session user.email={session_owner}"
                session_plan = str(item.get("_session_plan") or "").strip()
                if session_plan:
                    session_detail = (
                        f"{session_detail}，" if session_detail else ""
                    ) + f"account.planType={session_plan}"
                if session_detail:
                    detail = (
                        f"{session_detail}；{detail}" if detail else session_detail
                    )
                if not (
                    return_code == 0
                    and result == "invalid"
                    and not automatic_refresh_attempted
                ):
                    break

                automatic_refresh_attempted = True
                current_record = await asyncio.to_thread(
                    load_account_record, self.db_file, email
                )
                if account_saved_cookies(current_record):
                    cookie_refresh_attempted = True
                    item.update(
                        status="running",
                        message=(
                            "旧 Token 返回 401/token_invalid，正在通过注册代理与保存 Cookie "
                            "刷新 Session"
                        ),
                    )
                    self._append_log(item["message"], email=email)
                    refreshed, automatic_refresh_error = (
                        await self._refresh_invalid_token_with_cookie(item, email)
                    )
                else:
                    item.update(
                        status="running",
                        message="Token 已失效，正在使用无头浏览器自动提取 Session",
                    )
                    self._append_log(item["message"], email=email)
                    async with self._browser_refresh_lock:
                        refreshed, automatic_refresh_error = await self._browser_relogin(
                            item,
                            email,
                            "Token 已失效",
                            force_headless=True,
                        )
                if not refreshed:
                    break
                automatic_refresh_succeeded = True
                self._append_log(
                    (
                        "Cookie Session 候选已通过身份与过期时间校验，准备在线复验一次"
                        if cookie_refresh_attempted
                        else "无头浏览器自动提取完成，准备重新校验 Access Token"
                    ),
                    email=email,
                )
            if return_code == 0 and result in {"plus", "free"}:
                pending_session_result = item.get("_pending_session_result")
                if isinstance(pending_session_result, dict):
                    await asyncio.to_thread(
                        _save_account_record,
                        self.db_file,
                        email,
                        result=pending_session_result,
                    )
                    self._append_log(
                        "新 Access Token 复验成功，已原子覆盖 Session、AT 与 Cookie",
                        email=email,
                        level="success",
                    )
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
                self._append_log(item["message"], email=email, level="success")
            elif return_code == 0 and result == "invalid":
                if cookie_refresh_attempted and not automatic_refresh_succeeded:
                    invalid_message = (
                        "旧 Token 已失效，Cookie Session 刷新失败；原 Session、AT 与 Cookie 均未覆盖"
                    )
                    refresh_detail = str(automatic_refresh_error or "").strip()
                    if refresh_detail:
                        invalid_message += f"：{refresh_detail}"
                    await asyncio.to_thread(
                        mark_account_session_invalid,
                        self.db_file,
                        email,
                        invalid_message,
                    )
                    item.update(
                        status="expired",
                        sessionStatus="expired",
                        message=invalid_message,
                    )
                    self._state["expired"] += 1
                    self._state["failed"] += 1
                    self._append_log(item["message"], email=email, level="error")
                elif automatic_refresh_succeeded:
                    invalid_message = (
                        "已自动取得候选 Session，但新 Token 复验仍失效，"
                        "原 Session 已保留"
                    )
                elif automatic_refresh_attempted:
                    refresh_detail = str(automatic_refresh_error or "").strip()
                    invalid_message = "Token 已失效，无头浏览器自动提取失败"
                    if refresh_detail:
                        invalid_message += f"：{refresh_detail}"
                    invalid_message += "；原 Session 已保留"
                else:
                    invalid_message = "Token 已失效，原 Session 已保留"
                if not (cookie_refresh_attempted and not automatic_refresh_succeeded):
                    deletion_reason = f"{invalid_message}；验证详情：{detail or '两个账号接口均返回 401'}"
                    self._state["expired"] += 1
                    try:
                        deletion_message = await self._delete_confirmed_invalid_email(
                            email, deletion_reason
                        )
                        item.update(
                            status="deleted",
                            sessionStatus="expired",
                            message=f"Token 已确认失效；{deletion_message}",
                        )
                        self._state["deleted"] += 1
                        self._append_log(item["message"], email=email, level="warning")
                    except Exception as error:
                        delete_error = str(error or "删除邮箱失败")[:500]
                        await asyncio.to_thread(
                            mark_account_session_invalid,
                            self.db_file,
                            email,
                            f"{deletion_reason}；自动删除失败：{delete_error}",
                        )
                        item.update(
                            status="expired",
                            sessionStatus="expired",
                            message=f"{invalid_message}；自动删除失败：{delete_error}",
                        )
                        self._state["failed"] += 1
                        self._append_log(item["message"], email=email, level="error")
            else:
                error = detail or stderr.decode("utf-8", errors="replace").strip()
                if token:
                    error = error.replace(token, "[REDACTED]")
                item.update(status="failed", message=(error or "账号验证失败")[:500])
                self._state["failed"] += 1
                self._append_log(
                    f"验证失败，账号已保留：{item['message']}",
                    email=email,
                    level="error",
                )
            self._state["completed"] += 1
            self._clear_private_item_fields(item)

    async def stop(self) -> dict[str, Any]:
        if not self._batch_task or self._batch_task.done():
            return self.snapshot()
        self._state["status"] = "cancelling"
        if self.browser_manager and self.browser_manager.snapshot().get("running"):
            await self.browser_manager.stop()
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
