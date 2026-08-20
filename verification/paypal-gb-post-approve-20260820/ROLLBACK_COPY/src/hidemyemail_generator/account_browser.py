"""Account-scoped browser launchers with server-side Cookie injection.

The module follows an MVP boundary: ``AccountBrowserModel`` owns persisted
account state, ``AccountBrowserView`` exposes a credential-free response, and
``AccountBrowserPresenter`` coordinates a disposable worker process.  Browser
engines live behind a Strategy boundary in ``account_browser_worker.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .browser_tasks import (
    account_registration_proxy_url,
    account_saved_cookies,
    account_session,
    load_account_record,
)
from .roxy_registration import RoxyRegistrationStore


ACCOUNT_BROWSER_RESULT_PREFIX = "HME_ACCOUNT_BROWSER_RESULT:"
ACCOUNT_BROWSER_MODES = {"chrome", "roxy"}
CHATGPT_URL = "https://chatgpt.com/"
CHATGPT_AUTH_COOKIE_NAMES = frozenset(
    {
        "__Secure-next-auth.session-token",
        "__Secure-authjs.session-token",
        "next-auth.session-token",
        "authjs.session-token",
    }
)
CHATGPT_IDENTITY_COOKIE_NAMES = frozenset(
    {*CHATGPT_AUTH_COOKIE_NAMES, "oai-did", "oai-sc"}
)


def normalize_account_browser_mode(value: str) -> str:
    mode = str(value or "").strip().lower().replace("_", "-")
    if mode in {"chrome", "chrome-incognito", "google", "google-incognito"}:
        return "chrome"
    if mode == "roxy":
        return mode
    raise ValueError("浏览器类型必须是 Google Chrome 无痕或 Roxy")


def is_chatgpt_auth_cookie_name(value: str) -> bool:
    name = str(value or "").strip()
    return bool(
        name in CHATGPT_AUTH_COOKIE_NAMES
        or any(name.startswith(f"{base}.") for base in CHATGPT_AUTH_COOKIE_NAMES)
    )


def is_chatgpt_identity_cookie_name(value: str) -> bool:
    name = str(value or "").strip()
    return name in CHATGPT_IDENTITY_COOKIE_NAMES or is_chatgpt_auth_cookie_name(name)


def _cookie_from_session(record: dict[str, Any]) -> list[dict[str, Any]]:
    session = account_session(record)
    token = str(
        session.get("sessionToken") or session.get("session_token") or ""
    ).strip()
    if not token:
        return []
    return [
        {
            "name": "__Secure-next-auth.session-token",
            "value": token,
            "domain": "chatgpt.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
    ]


def chatgpt_cookie_storage_state(record: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal, portable Playwright state for a ChatGPT login.

    Only authentication/device cookies are moved to the new browser.  Expired,
    unrelated, and edge-network cookies (for example ``cf_clearance``) stay in
    the original profile because they are commonly bound to the old exit IP.
    """

    now = time.time()
    cookies: list[dict[str, Any]] = []
    saved_names: set[str] = set()
    candidates = [*account_saved_cookies(record), *_cookie_from_session(record)]
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        value = str(raw.get("value") or "").strip()
        domain = str(raw.get("domain") or "chatgpt.com").strip().lower()
        domain_key = domain.lstrip(".")
        if (
            not is_chatgpt_identity_cookie_name(name)
            or not value
            or name in saved_names
            or domain_key not in {"chatgpt.com", "openai.com"}
            or (domain_key == "openai.com" and name != "oai-did")
        ):
            continue
        try:
            expires = float(raw.get("expires", -1) or -1)
        except (TypeError, ValueError):
            expires = -1
        if expires > 0 and expires <= now:
            continue
        same_site = str(raw.get("sameSite") or "Lax").strip().title()
        if same_site not in {"Strict", "Lax", "None"}:
            same_site = "Lax"
        cookie = {
            "name": name,
            "value": value,
            "domain": domain or "chatgpt.com",
            "path": str(raw.get("path") or "/"),
            "expires": expires,
            "httpOnly": bool(raw.get("httpOnly", is_chatgpt_auth_cookie_name(name))),
            "secure": bool(raw.get("secure", True)),
            "sameSite": same_site,
        }
        cookies.append(cookie)
        saved_names.add(name)

    if not any(is_chatgpt_auth_cookie_name(item["name"]) for item in cookies):
        raise RuntimeError(
            "当前账号没有可用于登录的 ChatGPT Cookie，请先重新获取 Session"
        )
    return {"cookies": cookies, "origins": []}


@dataclass(frozen=True, repr=False)
class AccountBrowserLaunch:
    email: str
    mode: str
    storage_state: dict[str, Any]
    proxy_url: str
    landing_url: str = CHATGPT_URL
    roxy: dict[str, Any] | None = None

    @property
    def cookie_count(self) -> int:
        return len(self.storage_state.get("cookies") or [])

    def worker_payload(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "mode": self.mode,
            "storage_state": self.storage_state,
            "proxy_url": self.proxy_url,
            "landing_url": self.landing_url,
            "roxy": dict(self.roxy or {}),
        }


class AccountBrowserModel:
    """Model: resolve one account and its reusable browser state."""

    def __init__(self, db_file: Path, roxy_store: RoxyRegistrationStore) -> None:
        self.db_file = Path(db_file)
        self.roxy_store = roxy_store

    def prepare(self, email: str, mode: str) -> AccountBrowserLaunch:
        target = str(email or "").strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", target):
            raise ValueError("账号邮箱格式无效")
        selected_mode = normalize_account_browser_mode(mode)
        record = load_account_record(self.db_file, target)
        if not record:
            raise RuntimeError("账号不存在，请刷新后重试")
        storage_state = chatgpt_cookie_storage_state(record)
        roxy_config: dict[str, Any] | None = None
        if selected_mode == "roxy":
            runtime = self.roxy_store.runtime_config(1)
            roxy_config = {
                "api_url": str(runtime.get("apiUrl") or ""),
                "workspace_id": str(runtime.get("workspaceId") or ""),
                "profile_id": str(runtime.get("profileId") or ""),
            }
        return AccountBrowserLaunch(
            email=target,
            mode=selected_mode,
            storage_state=storage_state,
            proxy_url=account_registration_proxy_url(record),
            roxy=roxy_config,
        )


class AccountBrowserLauncher(Protocol):
    async def open(self, launch: AccountBrowserLaunch) -> dict[str, Any]: ...

    async def close(self) -> None: ...


def _public_error(value: Any) -> str:
    text = str(value or "浏览器启动失败")
    text = re.sub(r"(?i)(https?://)([^/@\s]+)@", r"\1[REDACTED]@", text)
    text = re.sub(
        r"(?i)((?:session|access)[_-]?token|cookie|password)\s*[=:]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    return text[:800]


class BrowserWorkerLauncher:
    """Adapter: launch browser Strategies in credential-isolated subprocesses."""

    def __init__(
        self,
        *,
        python_executable: Path | None = None,
        worker_script: Path | None = None,
        startup_timeout: float = 60.0,
    ) -> None:
        package_root = Path(__file__).resolve().parent
        self.python_executable = Path(python_executable or sys.executable).resolve()
        self.worker_script = Path(
            worker_script or package_root / "account_browser_worker.py"
        ).resolve()
        self.source_root = package_root.parent
        self.startup_timeout = max(1.0, float(startup_timeout))
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._watchers: set[asyncio.Task[None]] = set()
        self._launch_lock = asyncio.Lock()

    @staticmethod
    def _resource_key(launch: AccountBrowserLaunch) -> str:
        if launch.mode == "roxy":
            roxy = launch.roxy or {}
            return (
                "roxy:"
                + str(roxy.get("workspace_id") or "")
                + ":"
                + str(roxy.get("profile_id") or "")
            )
        return f"chrome:{launch.email}"

    async def open(self, launch: AccountBrowserLaunch) -> dict[str, Any]:
        async with self._launch_lock:
            return await self._open_locked(launch)

    async def _open_locked(self, launch: AccountBrowserLaunch) -> dict[str, Any]:
        key = self._resource_key(launch)
        active = self._processes.get(key)
        if active is not None and active.returncode is None:
            raise RuntimeError(
                "所选 Roxy 指纹环境已经打开"
                if launch.mode == "roxy"
                else "该账号的浏览器窗口已经打开"
            )
        if not self.python_executable.is_file():
            raise RuntimeError("浏览器 Python 运行环境不存在")
        if not self.worker_script.is_file():
            raise RuntimeError("账号浏览器启动器不存在")
        environment = dict(os.environ)
        current_pythonpath = str(environment.get("PYTHONPATH") or "").strip()
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(self.source_root), current_pythonpath) if part
        )
        environment.update(
            PYTHONIOENCODING="utf-8",
            PYTHONUTF8="1",
            PYTHONUNBUFFERED="1",
        )
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        process = await asyncio.create_subprocess_exec(
            str(self.python_executable),
            "-X",
            "utf8",
            str(self.worker_script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.worker_script.parents[2]),
            env=environment,
            creationflags=creationflags,
        )
        self._processes[key] = process
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.terminate()
            raise RuntimeError("浏览器启动器标准输入输出不可用")
        process.stdin.write(
            json.dumps(launch.worker_payload(), ensure_ascii=False).encode("utf-8")
        )
        await process.stdin.drain()
        process.stdin.close()
        try:
            line = await asyncio.wait_for(
                process.stdout.readline(), timeout=self.startup_timeout
            )
        except asyncio.TimeoutError as error:
            process.terminate()
            await process.wait()
            self._processes.pop(key, None)
            raise RuntimeError("浏览器启动等待超时") from error

        text = line.decode("utf-8", errors="replace").strip()
        if not text.startswith(ACCOUNT_BROWSER_RESULT_PREFIX):
            stderr = (
                (await process.stderr.read()).decode("utf-8", errors="replace").strip()
            )
            await process.wait()
            self._processes.pop(key, None)
            raise RuntimeError(
                _public_error(stderr or text or "浏览器启动器未返回结果")
            )
        try:
            result = json.loads(text[len(ACCOUNT_BROWSER_RESULT_PREFIX) :])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            process.terminate()
            await process.wait()
            self._processes.pop(key, None)
            raise RuntimeError("浏览器启动器返回了无效结果") from error
        if not isinstance(result, dict) or result.get("ok") is not True:
            if process.returncode is None:
                process.terminate()
            await process.wait()
            self._processes.pop(key, None)
            raise RuntimeError(
                _public_error(result.get("error") if isinstance(result, dict) else "")
            )

        watcher = asyncio.create_task(self._watch(key, process))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return {
            "mode": launch.mode,
            "email": launch.email,
            "cookieCount": launch.cookie_count,
            "processId": int(process.pid or 0),
            "landingUrl": CHATGPT_URL,
        }

    async def _watch(self, key: str, process: asyncio.subprocess.Process) -> None:
        async def drain(stream: asyncio.StreamReader | None) -> None:
            if stream is None:
                return
            while await stream.readline():
                pass

        try:
            await asyncio.gather(
                drain(process.stdout), drain(process.stderr), process.wait()
            )
        finally:
            if self._processes.get(key) is process:
                self._processes.pop(key, None)

    async def close(self) -> None:
        processes = list(self._processes.values())
        for process in processes:
            if process.returncode is None:
                process.terminate()
        if processes:
            await asyncio.gather(
                *(process.wait() for process in processes), return_exceptions=True
            )
        if self._watchers:
            await asyncio.gather(*list(self._watchers), return_exceptions=True)
        self._processes.clear()
        self._watchers.clear()


class AccountBrowserView:
    """View: expose launch evidence without returning Cookie values."""

    LABELS = {"chrome": "Google Chrome 无痕", "roxy": "Roxy 浏览器"}

    @classmethod
    def present(cls, result: dict[str, Any]) -> dict[str, Any]:
        mode = normalize_account_browser_mode(str(result.get("mode") or ""))
        cookie_count = max(0, int(result.get("cookieCount") or 0))
        return {
            "ok": True,
            "email": str(result.get("email") or ""),
            "mode": mode,
            "browser": cls.LABELS[mode],
            "cookieCount": cookie_count,
            "processId": max(0, int(result.get("processId") or 0)),
            "landingUrl": CHATGPT_URL,
            "message": f"{cls.LABELS[mode]}已打开，并注入 {cookie_count} 条账号 Cookie",
        }


class AccountBrowserPresenter:
    """Presenter: validate the account, choose a Strategy, and present result."""

    def __init__(
        self,
        model: AccountBrowserModel,
        *,
        launcher: AccountBrowserLauncher,
    ) -> None:
        self.model = model
        self.launcher = launcher

    async def open(self, email: str, mode: str) -> dict[str, Any]:
        launch = await asyncio.to_thread(self.model.prepare, email, mode)
        result = await self.launcher.open(launch)
        return AccountBrowserView.present(result)

    async def close(self) -> None:
        await self.launcher.close()


__all__ = [
    "ACCOUNT_BROWSER_MODES",
    "ACCOUNT_BROWSER_RESULT_PREFIX",
    "AccountBrowserLaunch",
    "AccountBrowserModel",
    "AccountBrowserPresenter",
    "AccountBrowserView",
    "BrowserWorkerLauncher",
    "chatgpt_cookie_storage_state",
    "is_chatgpt_auth_cookie_name",
    "is_chatgpt_identity_cookie_name",
    "normalize_account_browser_mode",
]
