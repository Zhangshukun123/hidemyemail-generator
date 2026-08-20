"""Isolated worker for interactive account browsers.

The parent sends the Cookie storage state through JSON stdin.  Command-line
arguments and emitted results never contain Cookie values.
"""

from __future__ import annotations

import json
import os
import ipaddress
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


RESULT_PREFIX = "HME_ACCOUNT_BROWSER_RESULT:"
DEFAULT_LANDING_URL = "https://chatgpt.com/"


def _configure_utf8() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _public_error(value: Any) -> str:
    text = str(value or "浏览器启动失败")
    for marker in ("storage_state", "cookies"):
        if marker in text.lower():
            return "浏览器未能载入账号 Cookie，请刷新账号状态后重试"
    return text[:800]


def _emit(payload: dict[str, Any]) -> None:
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def _proxy_options(value: str) -> dict[str, str] | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlsplit(text if "://" in text else f"http://{text}")
    if not parsed.hostname or not parsed.port:
        raise RuntimeError("账号注册代理格式无效，已停止浏览器启动")
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "socks5"}:
        raise RuntimeError("账号注册代理协议不受支持，已停止浏览器启动")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    result = {"server": f"{scheme}://{host}:{parsed.port}"}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result


def _require_loopback_endpoint(value: str, schemes: set[str], label: str) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    host = str(parsed.hostname or "").strip().lower()
    loopback = host == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if parsed.scheme.lower() not in schemes or not loopback or not parsed.port:
        raise RuntimeError(f"{label}必须使用本机回环地址")
    return text


def _visible_pages(context: Any) -> list[Any]:
    pages = []
    for page in list(getattr(context, "pages", ()) or ()):
        try:
            if not page.is_closed():
                pages.append(page)
        except Exception:
            pages.append(page)
    return pages


def _wait_for_window(browser: Any, context: Any) -> None:
    while browser.is_connected():
        if not _visible_pages(context):
            return
        time.sleep(0.5)


def _navigate(page: Any, target: str) -> None:
    try:
        page.goto(target, wait_until="domcontentloaded", timeout=60_000)
    except Exception as error:
        # A timeout after the document begins loading should leave the manual
        # window usable.  Navigation failures with no page URL remain fatal.
        if not str(getattr(page, "url", "") or "").startswith(("http://", "https://")):
            raise error
    try:
        page.bring_to_front()
    except Exception:
        pass


class AccountBrowserStrategy(ABC):
    """Strategy contract for one interactive browser engine."""

    @abstractmethod
    def open(self, playwright: Any, payload: dict[str, Any]) -> tuple[Any, Any]:
        """Open, inject Cookie state, emit readiness, and return handles."""


class ChromeIncognitoStrategy(AccountBrowserStrategy):
    """Open the installed official Google Chrome in an isolated context."""

    def open(self, playwright: Any, payload: dict[str, Any]) -> tuple[Any, Any]:
        launch_options: dict[str, Any] = {
            "channel": "chrome",
            "headless": False,
            "args": [
                "--incognito",
                "--disable-blink-features=AutomationControlled",
                "--lang=zh-CN",
                "--disable-application-cache",
                "--disk-cache-size=0",
                "--media-cache-size=0",
            ],
        }
        proxy = _proxy_options(str(payload.get("proxy_url") or ""))
        if proxy:
            launch_options["proxy"] = proxy
        try:
            browser = playwright.chromium.launch(**launch_options)
        except Exception as error:
            raise RuntimeError(
                "Google Chrome 无痕浏览器启动失败，请确认已安装官方 Google Chrome："
                f"{error}"
            ) from error
        storage_state = payload.get("storage_state")
        context_options: dict[str, Any] = {"locale": "zh-CN"}
        if isinstance(storage_state, dict):
            context_options["storage_state"] = storage_state
        context = browser.new_context(**context_options)
        page = context.new_page()
        _navigate(page, str(payload.get("landing_url") or DEFAULT_LANDING_URL))
        cookie_count = len((storage_state or {}).get("cookies") or [])
        _emit({"ok": True, "mode": "chrome", "cookie_count": cookie_count})
        return browser, context


class RoxyBrowserStrategy(AccountBrowserStrategy):
    """Open the configured Roxy profile, connect through CDP, then inject Cookies."""

    def open(self, playwright: Any, payload: dict[str, Any]) -> tuple[Any, Any]:
        package_root = Path(__file__).resolve().parent
        source_root = package_root.parent
        if str(source_root) not in sys.path:
            sys.path.insert(0, str(source_root))
        from hidemyemail_generator.roxy_registration import (
            RoxyOpenApiClient,
            roxy_cdp_endpoint,
            roxy_proxy_info,
        )

        config = payload.get("roxy")
        config = dict(config) if isinstance(config, dict) else {}
        workspace_id = str(config.get("workspace_id") or "").strip()
        profile_id = str(config.get("profile_id") or "").strip()
        if not workspace_id.isdigit() or not profile_id:
            raise RuntimeError("请先在账号管理中选择 Roxy 专用指纹环境")
        api_url = _require_loopback_endpoint(
            str(config.get("api_url") or ""), {"http", "https"}, "Roxy OpenAPI"
        )
        client = RoxyOpenApiClient(
            api_url,
            str(os.environ.get("HME_ROXY_API_TOKEN") or ""),
        )
        if client.connection_info(profile_id):
            raise RuntimeError("所选 Roxy 环境正在打开，请先关闭原窗口")
        profile_opened = False
        try:
            client.clear_profile(int(workspace_id), profile_id)
            client.modify_profile(
                {
                    "workspaceId": int(workspace_id),
                    "dirId": profile_id,
                    "proxyInfo": roxy_proxy_info(str(payload.get("proxy_url") or "")),
                    "fingerInfo": {
                        "randomFingerprint": True,
                        "clearCacheFile": True,
                        "clearCookie": True,
                        "clearHistory": True,
                        "syncTab": False,
                        "syncCookie": False,
                        "syncPassword": False,
                        "forbidSavePassword": True,
                        "isLanguageBaseIp": True,
                        "isDisplayLanguageBaseIp": True,
                        "isTimeZone": True,
                        "isPositionBaseIp": True,
                    },
                },
            )
            client.randomize_profile(int(workspace_id), profile_id)
            connection = client.open_profile(
                int(workspace_id), profile_id, background=False
            )
            profile_opened = True
            endpoint = _require_loopback_endpoint(
                roxy_cdp_endpoint(connection),
                {"http", "https", "ws", "wss"},
                "Roxy CDP",
            )
            browser = playwright.chromium.connect_over_cdp(endpoint, timeout=30_000)
            contexts = list(browser.contexts)
            context = contexts[0] if contexts else browser.new_context()
            storage_state = payload.get("storage_state")
            cookies = (
                list(storage_state.get("cookies") or [])
                if isinstance(storage_state, dict)
                else []
            )
            context.clear_cookies()
            if cookies:
                context.add_cookies(cookies)
            pages = _visible_pages(context)
            page = pages[0] if pages else context.new_page()
            for extra_page in pages[1:]:
                try:
                    extra_page.close()
                except Exception:
                    pass
            _navigate(page, str(payload.get("landing_url") or DEFAULT_LANDING_URL))
            _emit({"ok": True, "mode": "roxy", "cookie_count": len(cookies)})
            return browser, context
        except Exception:
            if profile_opened:
                try:
                    client.close_profile(profile_id)
                except Exception:
                    pass
            raise


class AccountBrowserWorkerPresenter:
    """Presenter: select a browser Strategy and own its visible lifecycle."""

    STRATEGIES = {
        "chrome": ChromeIncognitoStrategy,
        "roxy": RoxyBrowserStrategy,
    }

    def run(self, payload: dict[str, Any]) -> None:
        mode = str(payload.get("mode") or "").strip().lower()
        strategy_type = self.STRATEGIES.get(mode)
        if strategy_type is None:
            raise ValueError("浏览器类型无效")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = None
            context = None
            try:
                browser, context = strategy_type().open(playwright, payload)
                _wait_for_window(browser, context)
            finally:
                # Chrome is owned by this worker and closes with its last page.
                # A Roxy browser is remote; disconnecting Playwright does not
                # call the Roxy close-profile API, so the user's window remains
                # under direct control until they close it.
                if mode == "chrome":
                    try:
                        if context is not None:
                            context.close()
                    except Exception:
                        pass
                    try:
                        if browser is not None:
                            browser.close()
                    except Exception:
                        pass


def main() -> int:
    _configure_utf8()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise RuntimeError("浏览器启动输入格式无效")
        AccountBrowserWorkerPresenter().run(payload)
        return 0
    except Exception as error:
        _emit({"ok": False, "error": _public_error(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
