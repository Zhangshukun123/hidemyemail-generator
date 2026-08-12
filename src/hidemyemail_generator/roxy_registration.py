from __future__ import annotations

import ctypes
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Any

try:
    from .inbox import connect_db
except ImportError:  # pragma: no cover - direct bridge execution
    from inbox import connect_db


DEFAULT_ROXY_API_URL = "http://127.0.0.1:50000"
ROXY_REGISTRATION_SETTING_KEY = "roxy_registration"


class RoxyRegistrationError(RuntimeError):
    pass


def normalize_roxy_api_url(value: str) -> str:
    text = str(value or "").strip() or DEFAULT_ROXY_API_URL
    if "://" not in text:
        text = f"http://{text}"
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Roxy OpenAPI 地址无效")
    port = parsed.port
    authority = parsed.hostname
    if ":" in authority and not authority.startswith("["):
        authority = f"[{authority}]"
    if port:
        authority = f"{authority}:{port}"
    return urllib.parse.urlunsplit((parsed.scheme, authority, "", "", ""))


class RoxyOpenApiClient:
    def __init__(
        self,
        base_url: str = DEFAULT_ROXY_API_URL,
        api_token: str = "",
        *,
        timeout: float = 15.0,
        opener: Any | None = None,
    ) -> None:
        self.base_url = normalize_roxy_api_url(base_url)
        self.api_token = str(api_token or "").strip()
        self.timeout = max(1.0, float(timeout))
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        query = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}/{str(path or '').lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        payload = None
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["token"] = self.api_token
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url,
            data=payload,
            headers=headers,
            method=str(method or "GET").upper(),
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            try:
                message = str(json.loads(raw).get("msg") or "").strip()
            except (TypeError, ValueError, json.JSONDecodeError):
                message = ""
            raise RoxyRegistrationError(
                message or f"Roxy OpenAPI 请求失败：HTTP {error.code}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RoxyRegistrationError(
                "无法连接 Roxy OpenAPI；请启动 RoxyBrowser，并在“API & AI MCP”中开启 API"
            ) from error
        try:
            result = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RoxyRegistrationError("Roxy OpenAPI 返回了无效 JSON") from error
        if not isinstance(result, dict):
            raise RoxyRegistrationError("Roxy OpenAPI 返回格式无效")
        try:
            success = int(result.get("code") or 0) == 0
        except (TypeError, ValueError):
            success = False
        if not success:
            message = str(result.get("msg") or "Roxy OpenAPI 请求失败").strip()
            if self.api_token:
                message = message.replace(self.api_token, "***")
            raise RoxyRegistrationError(message)
        return result.get("data")

    def health(self) -> Any:
        return self._request("GET", "/health")

    def list_workspaces(self) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/browser/workspace",
            params={"page_index": 1, "page_size": 100},
        )
        rows = data.get("rows", []) if isinstance(data, dict) else []
        return [dict(row) for row in rows if isinstance(row, dict)]

    def list_profiles(self, workspace_id: int) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/browser/list_v3",
            params={
                "workspaceId": int(workspace_id),
                "page_index": 1,
                "page_size": 100,
            },
        )
        rows = data.get("rows", []) if isinstance(data, dict) else []
        return [dict(row) for row in rows if isinstance(row, dict)]

    def connection_info(self, dir_id: str = "") -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/browser/connection_info",
            params={"dirIds": str(dir_id or "").strip()} if dir_id else None,
        )
        if isinstance(data, dict):
            return [dict(data)] if data else []
        return [dict(item) for item in data or [] if isinstance(item, dict)]

    def modify_profile(self, body: dict[str, Any]) -> Any:
        return self._request("POST", "/browser/mdf", body=body)

    def clear_profile(self, workspace_id: int, dir_id: str) -> Any:
        return self._request(
            "POST",
            "/browser/clear_local_cache",
            body={
                "workspaceId": int(workspace_id),
                "dirIds": [str(dir_id)],
                "type": "cloud",
            },
        )

    def randomize_profile(self, workspace_id: int, dir_id: str) -> Any:
        return self._request(
            "POST",
            "/browser/random_env",
            body={"workspaceId": int(workspace_id), "dirId": str(dir_id)},
        )

    def open_profile(
        self,
        workspace_id: int,
        dir_id: str,
        *,
        background: bool = False,
    ) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/browser/open",
            body={
                "workspaceId": int(workspace_id),
                "dirId": str(dir_id),
                "forceOpen": False,
                "headless": bool(background),
                "args": ["--disable-save-password-bubble"],
            },
        )
        if not isinstance(data, dict):
            raise RoxyRegistrationError("Roxy 启动响应缺少 CDP 连接信息")
        return dict(data)

    def close_profile(self, dir_id: str) -> Any:
        return self._request(
            "POST", "/browser/close", body={"dirId": str(dir_id)}
        )


def roxy_cdp_endpoint(connection: dict[str, Any]) -> str:
    websocket = str(connection.get("ws") or "").strip()
    if websocket:
        return websocket
    http = str(connection.get("http") or "").strip()
    if not http:
        return ""
    if "://" not in http:
        http = f"http://{http}"
    return http


def roxy_proxy_info(proxy_url: str) -> dict[str, Any]:
    value = str(proxy_url or "").strip()
    if not value:
        return {
            "moduleId": 0,
            "proxyMethod": "custom",
            "proxyCategory": "noproxy",
            "ipType": "IPV4",
        }
    parsed = urllib.parse.urlsplit(value)
    scheme = parsed.scheme.lower()
    protocol = {"http": "HTTP", "https": "HTTPS", "socks5": "SOCKS5"}.get(
        scheme
    )
    if not protocol or not parsed.hostname or not parsed.port:
        raise ValueError("Roxy 注册代理仅支持 HTTP、HTTPS 或 SOCKS5 完整连接")
    return {
        "moduleId": 0,
        "proxyMethod": "custom",
        "proxyCategory": protocol,
        "protocol": protocol,
        "ipType": "IPV4",
        "host": parsed.hostname,
        "port": str(parsed.port),
        "proxyUserName": urllib.parse.unquote(parsed.username or ""),
        "proxyPassword": urllib.parse.unquote(parsed.password or ""),
    }


class RoxyRegistrationStore:
    def __init__(self, db_file: Path | str) -> None:
        self.db_file = Path(db_file).resolve()

    @staticmethod
    def defaults() -> dict[str, Any]:
        return {
            "apiUrl": DEFAULT_ROXY_API_URL,
            "workspaceId": "",
            "profileId": "",
            "updatedAt": "",
        }

    def load(self) -> dict[str, Any]:
        state = self.defaults()
        conn = connect_db(str(self.db_file))
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (ROXY_REGISTRATION_SETTING_KEY,),
            ).fetchone()
        finally:
            conn.close()
        if row:
            try:
                saved = json.loads(str(row["value"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                saved = {}
            if isinstance(saved, dict):
                for key in state:
                    if key in saved:
                        state[key] = saved[key]
        state["apiUrl"] = normalize_roxy_api_url(str(state.get("apiUrl") or ""))
        state["workspaceId"] = str(state.get("workspaceId") or "").strip()
        state["profileId"] = str(state.get("profileId") or "").strip()
        return state

    def configure(
        self,
        *,
        api_url: str | None = None,
        workspace_id: str | int | None = None,
        profile_id: str | None = None,
    ) -> dict[str, Any]:
        state = self.load()
        if api_url is not None:
            state["apiUrl"] = normalize_roxy_api_url(api_url)
        if workspace_id is not None:
            text = str(workspace_id or "").strip()
            if text and (not text.isdigit() or int(text) <= 0):
                raise ValueError("Roxy 工作区 ID 无效")
            state["workspaceId"] = text
            if profile_id is None:
                state["profileId"] = ""
        if profile_id is not None:
            text = str(profile_id or "").strip()
            if len(text) > 128:
                raise ValueError("Roxy 指纹环境 ID 无效")
            state["profileId"] = text
        state["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn = connect_db(str(self.db_file))
        try:
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (
                    ROXY_REGISTRATION_SETTING_KEY,
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return state

    def runtime_config(self, profile_count: int = 1) -> dict[str, Any]:
        state = self.load()
        if not state["workspaceId"] or not state["profileId"]:
            raise RuntimeError("请先选择一个 Roxy 专用指纹环境")
        requested = max(1, min(5, int(profile_count)))
        if requested == 1:
            return {**state, "profileIds": [state["profileId"]]}

        public = self.public_state()
        if not public.get("available"):
            raise RuntimeError(public.get("error") or "Roxy OpenAPI 未连接")
        profiles = [
            item
            for item in public.get("profiles", [])
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]
        selected_id = state["profileId"]
        if not any(item["id"] == selected_id for item in profiles):
            raise RuntimeError("已保存的 Roxy 指纹环境不存在，请重新选择")
        ordered = [
            *[item for item in profiles if item["id"] == selected_id],
            *[item for item in profiles if item["id"] != selected_id],
        ]
        available_ids = [item["id"] for item in ordered if not item.get("open")]
        if selected_id not in available_ids:
            raise RuntimeError("所选 Roxy 首个环境正在打开，请先关闭该窗口")
        if len(available_ids) < requested:
            raise RuntimeError(
                f"Roxy 可用环境不足：需要 {requested} 个，当前仅有 "
                f"{len(available_ids)} 个未打开环境"
            )
        return {
            **state,
            "profileIds": available_ids[:requested],
            "profileCount": requested,
        }

    def public_state(self, *, refresh: bool = True) -> dict[str, Any]:
        state = self.load()
        result = {
            **state,
            "available": False,
            "configured": bool(state["workspaceId"] and state["profileId"]),
            "workspaces": [],
            "profiles": [],
            "maxConcurrency": 0,
            "error": "",
            "nativeHeadless": False,
        }
        if not refresh:
            return result
        token = str(os.environ.get("HME_ROXY_API_TOKEN") or "")
        try:
            client = RoxyOpenApiClient(state["apiUrl"], token, timeout=4)
            client.health()
            workspaces = client.list_workspaces()
            result["workspaces"] = [
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("workspaceName") or "未命名工作区"),
                }
                for item in workspaces
                if str(item.get("id") or "").strip()
            ]
            workspace_id = state["workspaceId"]
            if not workspace_id and len(result["workspaces"]) == 1:
                workspace_id = result["workspaces"][0]["id"]
            profiles = client.list_profiles(int(workspace_id)) if workspace_id else []
            open_profile_ids = {
                str(item.get("dirId") or "").strip()
                for item in client.connection_info()
                if isinstance(item, dict) and str(item.get("dirId") or "").strip()
            }
            result["workspaceId"] = workspace_id
            result["profiles"] = [
                {
                    "id": str(item.get("dirId") or ""),
                    "name": str(
                        item.get("windowName")
                        or item.get("windowRemark")
                        or f"窗口 {item.get('windowSortNum') or '?'}"
                    ),
                    "sortNumber": str(item.get("windowSortNum") or ""),
                    "os": str(item.get("os") or ""),
                    "coreVersion": str(item.get("coreVersion") or ""),
                    "open": str(item.get("dirId") or "").strip()
                    in open_profile_ids,
                }
                for item in profiles
                if str(item.get("dirId") or "").strip()
            ]
            selected_exists = any(
                item["id"] == state["profileId"] for item in result["profiles"]
            )
            result["available"] = True
            result["configured"] = bool(workspace_id and selected_exists)
            result["maxConcurrency"] = min(
                5,
                sum(not item.get("open") for item in result["profiles"]),
            )
            if state["profileId"] and not selected_exists:
                result["error"] = "已保存的 Roxy 指纹环境不存在，请重新选择"
        except (RoxyRegistrationError, ValueError) as error:
            result["error"] = str(error)
        return result


def hide_roxy_browser_window(process_id: int, *, timeout: float = 5.0) -> bool:
    if os.name != "nt" or int(process_id or 0) <= 0:
        return False
    user32 = ctypes.windll.user32
    target_pid = int(process_id)
    hidden = False
    deadline = time.monotonic() + max(0.1, float(timeout))
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    while time.monotonic() < deadline and not hidden:
        def callback(hwnd, _lparam):
            nonlocal hidden
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) == target_pid and user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 0)
                hidden = True
            return True

        user32.EnumWindows(enum_proc(callback), 0)
        if not hidden:
            time.sleep(0.1)
    return hidden


class RoxyRegistrationBrowser:
    def __init__(
        self,
        *,
        api_url: str,
        api_token: str,
        workspace_id: int,
        profile_id: str,
        proxy_url: str,
        background: bool,
        log,
    ) -> None:
        self.client = RoxyOpenApiClient(api_url, api_token)
        self.workspace_id = int(workspace_id)
        self.profile_id = str(profile_id)
        self.proxy_url = str(proxy_url or "")
        self.background = bool(background)
        self.log = log
        self.connection: dict[str, Any] = {}
        self.prepared = False

    def prepare(self) -> dict[str, Any]:
        if self.prepared:
            return self.connection
        if self.client.connection_info(self.profile_id):
            raise RoxyRegistrationError(
                "所选 Roxy 专用环境正在打开；请先关闭该窗口后再开始注册"
            )
        self.log("[Roxy] 正在清理专用环境并生成全新随机指纹")
        self.client.clear_profile(self.workspace_id, self.profile_id)
        self.client.modify_profile(
            {
                "workspaceId": self.workspace_id,
                "dirId": self.profile_id,
                "proxyInfo": roxy_proxy_info(self.proxy_url),
                "fingerInfo": {
                    "randomFingerprint": True,
                    "clearCacheFile": True,
                    "clearCookie": True,
                    "clearHistory": True,
                    "syncTab": False,
                    "syncCookie": False,
                    "isLanguageBaseIp": True,
                    "isDisplayLanguageBaseIp": True,
                    "isTimeZone": True,
                    "isPositionBaseIp": True,
                },
            }
        )
        self.client.randomize_profile(self.workspace_id, self.profile_id)
        self.connection = self.client.open_profile(
            self.workspace_id,
            self.profile_id,
            background=self.background,
        )
        if not roxy_cdp_endpoint(self.connection):
            raise RoxyRegistrationError("Roxy 未返回可用的 CDP 地址")
        if self.background:
            hidden = hide_roxy_browser_window(int(self.connection.get("pid") or 0))
            self.log(
                "[Roxy] 后台隐藏窗口已启用"
                if hidden
                else "[Roxy] 未找到可隐藏窗口，自动化继续在前台运行"
            )
        else:
            self.log("[Roxy] 有头窗口已启动")
        self.prepared = True
        return self.connection

    def new_browser_context(
        self,
        playwright,
        _proxy,
        storage_state: dict[str, Any] | None = None,
        **_options,
    ):
        connection = self.prepare()
        browser = playwright.chromium.connect_over_cdp(roxy_cdp_endpoint(connection))
        contexts = list(browser.contexts)
        if not contexts:
            raise RoxyRegistrationError("Roxy CDP 连接中没有浏览器上下文")
        context = contexts[0]
        cookies = (storage_state or {}).get("cookies") or []
        if cookies:
            context.add_cookies(cookies)
        return browser, context

    def close(self) -> None:
        if not self.prepared:
            return
        try:
            self.client.close_profile(self.profile_id)
        except RoxyRegistrationError:
            pass
        finally:
            self.prepared = False


__all__ = [
    "DEFAULT_ROXY_API_URL",
    "RoxyOpenApiClient",
    "RoxyRegistrationBrowser",
    "RoxyRegistrationError",
    "RoxyRegistrationStore",
    "hide_roxy_browser_window",
    "normalize_roxy_api_url",
    "roxy_cdp_endpoint",
    "roxy_proxy_info",
]
