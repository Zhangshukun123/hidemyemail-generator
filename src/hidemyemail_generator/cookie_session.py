from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from http.cookiejar import Cookie
from typing import Any, Protocol

from curl_cffi.requests import Session


CHATGPT_SESSION_URL = "https://chatgpt.com/api/auth/session"
CHATGPT_SESSION_REFRESH_URL = f"{CHATGPT_SESSION_URL}?refresh=true"
DEFAULT_IMPERSONATE = "firefox144"
DEFAULT_LANGUAGE = "en-US"


@dataclass(frozen=True)
class CookieSessionModel:
    """Saved browser state required to request the ChatGPT Session endpoint."""

    cookies: tuple[dict[str, Any], ...]
    previous_token: str = ""
    proxy_url: str = ""
    impersonate: str = DEFAULT_IMPERSONATE
    language: str = DEFAULT_LANGUAGE
    origins: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CookieSessionSnapshot:
    """Session response and the updated HTTP cookie jar."""

    session: dict[str, Any]
    cookies: tuple[dict[str, Any], ...]


class CookieSessionGateway(Protocol):
    """Gateway boundary used by the presenter to access ChatGPT."""

    def fetch(self, model: CookieSessionModel) -> CookieSessionSnapshot: ...


def _is_chatgpt_domain(domain: str) -> bool:
    host = str(domain or "chatgpt.com").strip().lower().lstrip(".")
    return host == "chatgpt.com" or host.endswith(".chatgpt.com")


def _cookie_expiry(value: Any) -> int | None:
    try:
        expiry = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if expiry <= 0:
        return None
    return int(expiry)


def _http_cookie(raw: dict[str, Any], *, now: float) -> Cookie | None:
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    domain = str(raw.get("domain") or "chatgpt.com").strip().lower()
    if not _is_chatgpt_domain(domain):
        return None
    path = str(raw.get("path") or "/").strip()
    if not path.startswith("/"):
        path = "/"
    expires = _cookie_expiry(raw.get("expires"))
    if expires is not None and expires <= now:
        return None
    rest: dict[str, Any] = {}
    if bool(raw.get("httpOnly")):
        rest["HttpOnly"] = None
    same_site = str(raw.get("sameSite") or "").strip().title()
    if same_site in {"Lax", "Strict", "None"}:
        rest["SameSite"] = same_site
    return Cookie(
        version=0,
        name=name,
        value=str(raw.get("value") or ""),
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=bool(raw.get("secure", True)),
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest=rest,
        rfc2109=False,
    )


def _rest_value(rest: dict[str, Any], name: str) -> Any:
    wanted = name.casefold()
    return next(
        (value for key, value in rest.items() if str(key).casefold() == wanted),
        None,
    )


def _export_chatgpt_cookies(cookie_jar: Any) -> tuple[dict[str, Any], ...]:
    now = time.time()
    exported: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cookie in cookie_jar:
        if not _is_chatgpt_domain(str(cookie.domain or "")):
            continue
        if cookie.expires is not None and cookie.expires <= now:
            continue
        rest = dict(getattr(cookie, "_rest", {}) or {})
        same_site = str(_rest_value(rest, "SameSite") or "Lax").title()
        if same_site not in {"Lax", "Strict", "None"}:
            same_site = "Lax"
        value = {
            "name": str(cookie.name or ""),
            "value": str(cookie.value or ""),
            "domain": str(cookie.domain or "chatgpt.com"),
            "path": str(cookie.path or "/"),
            "expires": float(cookie.expires) if cookie.expires is not None else -1.0,
            "httpOnly": any(str(key).casefold() == "httponly" for key in rest),
            "secure": bool(cookie.secure),
            "sameSite": same_site,
        }
        identity = (
            value["name"],
            value["domain"].lower().lstrip("."),
            value["path"],
        )
        exported[identity] = value
    values = sorted(
        exported.values(),
        key=lambda item: (
            str(item["domain"]),
            str(item["path"]),
            str(item["name"]),
        ),
    )
    return tuple(values)


def _accept_language(language: str) -> str:
    normalized = str(language or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE
    if "," in normalized:
        return normalized
    base = normalized.split("-", 1)[0]
    return f"{normalized},{base};q=0.9" if base != normalized else normalized


class CurlCffiCookieSessionGateway:
    """Fetch a Session with a dedicated curl_cffi browser session and cookie jar."""

    def __init__(self, *, session_factory: Any = Session, timeout_seconds: float = 30):
        self.session_factory = session_factory
        self.timeout_seconds = timeout_seconds

    def _request_session(self, client: Any, url: str) -> dict[str, Any]:
        response = client.get(
            url,
            allow_redirects=False,
            timeout=self.timeout_seconds,
        )
        if int(response.status_code) != 200:
            raise RuntimeError(
                f"Cookie Session 请求返回 HTTP {int(response.status_code)}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Cookie Session 响应格式无效")
        token = str(
            payload.get("accessToken") or payload.get("access_token") or ""
        ).strip()
        if not token:
            raise RuntimeError("Cookie Session 响应未返回 Access Token")
        return payload

    def fetch(self, model: CookieSessionModel) -> CookieSessionSnapshot:
        language = str(model.language or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE
        headers = {
            "Accept": "application/json",
            "Accept-Language": _accept_language(language),
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
            "Referer": "https://chatgpt.com/",
            "oai-language": language.split(",", 1)[0],
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        options: dict[str, Any] = {
            "impersonate": str(model.impersonate or DEFAULT_IMPERSONATE),
            "headers": headers,
            "timeout": self.timeout_seconds,
        }
        proxy_url = str(model.proxy_url or "").strip()
        if proxy_url:
            options["proxies"] = {"http": proxy_url, "https": proxy_url}
        client = self.session_factory(**options)
        try:
            now = time.time()
            loaded = 0
            for raw_cookie in model.cookies:
                cookie = _http_cookie(raw_cookie, now=now)
                if cookie is None:
                    continue
                client.cookies.jar.set_cookie(cookie)
                loaded += 1
            if not loaded:
                raise RuntimeError("该账号没有未过期的 ChatGPT Cookie")
            payload = self._request_session(client, CHATGPT_SESSION_URL)
            token = str(
                payload.get("accessToken") or payload.get("access_token") or ""
            ).strip()
            if model.previous_token and token == model.previous_token:
                payload = self._request_session(client, CHATGPT_SESSION_REFRESH_URL)
            cookies = _export_chatgpt_cookies(client.cookies.jar)
            if not cookies:
                raise RuntimeError("Cookie Session 成功但没有可保存的 Cookie")
            return CookieSessionSnapshot(session=payload, cookies=cookies)
        finally:
            client.close()


class CookieSessionPresenter:
    """MVP presenter that turns the gateway snapshot into persisted fields."""

    def __init__(self, gateway: CookieSessionGateway | None = None):
        self.gateway = gateway or CurlCffiCookieSessionGateway()

    async def refresh(self, model: CookieSessionModel) -> dict[str, Any]:
        snapshot = await asyncio.to_thread(self.gateway.fetch, model)
        token = str(
            snapshot.session.get("accessToken")
            or snapshot.session.get("access_token")
            or ""
        ).strip()
        cookies = [dict(cookie) for cookie in snapshot.cookies]
        origins = [dict(origin) for origin in model.origins]
        return {
            "access_token": token,
            "session_json": json.dumps(snapshot.session, ensure_ascii=False),
            "cookies_json": json.dumps(cookies, ensure_ascii=False),
            "storage_state_json": json.dumps(
                {"cookies": cookies, "origins": origins}, ensure_ascii=False
            ),
            "session_acquisition_method": "cookie_session_refresh",
        }


async def request_cookie_session(
    *,
    cookies: list[dict[str, Any]],
    previous_token: str = "",
    proxy_url: str = "",
    storage_state: dict[str, Any] | None = None,
    impersonate: str = DEFAULT_IMPERSONATE,
    language: str = DEFAULT_LANGUAGE,
    gateway: CookieSessionGateway | None = None,
) -> dict[str, Any]:
    """Request the standard Session endpoint with saved browser state."""

    if not cookies:
        raise RuntimeError("该账号没有可用于刷新 Session 的 Cookie")
    origins: tuple[dict[str, Any], ...] = ()
    if isinstance(storage_state, dict) and isinstance(storage_state.get("origins"), list):
        origins = tuple(
            dict(origin)
            for origin in storage_state["origins"]
            if isinstance(origin, dict)
        )
    model = CookieSessionModel(
        cookies=tuple(dict(cookie) for cookie in cookies if isinstance(cookie, dict)),
        previous_token=str(previous_token or "").strip(),
        proxy_url=str(proxy_url or "").strip(),
        impersonate=str(impersonate or DEFAULT_IMPERSONATE).strip(),
        language=str(language or DEFAULT_LANGUAGE).strip(),
        origins=origins,
    )
    return await CookieSessionPresenter(gateway).refresh(model)


__all__ = [
    "CHATGPT_SESSION_URL",
    "CHATGPT_SESSION_REFRESH_URL",
    "CookieSessionGateway",
    "CookieSessionModel",
    "CookieSessionPresenter",
    "CookieSessionSnapshot",
    "CurlCffiCookieSessionGateway",
    "request_cookie_session",
]
