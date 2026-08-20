from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
from typing import Any

from aiohttp import web

from .access_session import AccessSessionStore
from .adapters.imap_repository import CachedCodeRepository, ImapCodeRepository
from .domain import InvalidAddressError, normalize_zkgmail_address
from .invite import InviteTokenService
from .ports import CodeRepository
from .presenter import LookupPresenter
from .rate_limit import SlidingWindowRateLimiter
from .settings import Settings
from .strategies.keyword_code_extractor import KeywordCodeExtractor
from .view import PortalView


PRESENTER_KEY = web.AppKey("lookup_presenter", LookupPresenter)
VIEW_KEY = web.AppKey("portal_view", PortalView)
LIMITER_KEY = web.AppKey("rate_limiter", SlidingWindowRateLimiter)
ACCESS_LIMITER_KEY = web.AppKey("access_rate_limiter", SlidingWindowRateLimiter)
ACCESS_STORE_KEY = web.AppKey("access_session_store", AccessSessionStore)
INVITE_SERVICE_KEY = web.AppKey("invite_token_service", InviteTokenService)
SETTINGS_KEY = web.AppKey("settings", Settings)
ACCESS_COOKIE_NAME = "__Host-zkg_access"

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@web.middleware
async def security_headers(request: web.Request, handler):
    response = await handler(request)
    for name, value in SECURITY_HEADERS.items():
        if name == "Cache-Control" and response.headers.get(name):
            continue
        response.headers[name] = value
    return response


def _client_key(request: web.Request) -> str:
    try:
        trusted_proxy = ipaddress.ip_address(str(request.remote or "")).is_loopback
    except ValueError:
        trusted_proxy = False
    forwarded = str(request.headers.get("X-Forwarded-For") or "")
    proxy_address = (
        forwarded.rsplit(",", 1)[-1].strip() if trusted_proxy and forwarded else ""
    )
    return str(proxy_address or request.remote or "unknown")[:128]


def _access_scope(request: web.Request) -> str:
    settings = request.app[SETTINGS_KEY]
    if not settings.access_protected:
        return ""
    supplied = str(request.cookies.get(ACCESS_COOKIE_NAME) or "")
    return request.app[ACCESS_STORE_KEY].scope(supplied)


def _same_origin_json(request: web.Request) -> bool:
    if request.content_type != "application/json":
        return False
    origin = str(request.headers.get("Origin") or "").rstrip("/")
    if not origin or origin == "null":
        return False
    fetch_site = str(request.headers.get("Sec-Fetch-Site") or "")
    if fetch_site and fetch_site != "same-origin":
        return False
    try:
        trusted_proxy = ipaddress.ip_address(str(request.remote or "")).is_loopback
    except ValueError:
        trusted_proxy = False
    scheme = request.scheme
    host = request.host
    if trusted_proxy:
        scheme = str(request.headers.get("X-Forwarded-Proto") or scheme)
        host = str(request.headers.get("X-Forwarded-Host") or host)
    return hmac.compare_digest(origin, f"{scheme}://{host}")


async def index(request: web.Request) -> web.Response:
    return request.app[VIEW_KEY].page()


async def stylesheet(request: web.Request) -> web.Response:
    return request.app[VIEW_KEY].stylesheet()


async def script(request: web.Request) -> web.Response:
    return request.app[VIEW_KEY].script()


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def establish_access(request: web.Request) -> web.Response:
    if not _same_origin_json(request):
        return request.app[VIEW_KEY].error("请求来源无效", 403, state="forbidden")
    allowed, retry_after = request.app[ACCESS_LIMITER_KEY].allow(
        f"access:{_client_key(request)}"
    )
    if not allowed:
        response = request.app[VIEW_KEY].error("访问尝试过于频繁，请稍后再试", 429, state="limited")
        response.headers["Retry-After"] = str(retry_after)
        return response
    settings = request.app[SETTINGS_KEY]
    if not settings.require_invite or not settings.access_protected:
        return request.app[VIEW_KEY].error("邀请访问尚未配置", 503, state="unconfigured")
    try:
        payload: Any = await request.json(loads=json.loads)
    except (json.JSONDecodeError, web.HTTPBadRequest, TypeError, ValueError):
        return request.app[VIEW_KEY].error("请求格式无效", 400)
    if not isinstance(payload, dict):
        return request.app[VIEW_KEY].error("请求格式无效", 400)
    supplied = str(payload.get("token") or "")
    scope = request.app[INVITE_SERVICE_KEY].verify(supplied)
    if scope is None:
        return request.app[VIEW_KEY].error("邀请链接无效", 403, state="unauthorized")
    response = web.json_response(
        {"ok": True, "state": "authorized", "email": scope.email}
    )
    cookie_max_age = min(
        settings.session_max_age_seconds,
        scope.remaining_seconds,
    )
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        request.app[ACCESS_STORE_KEY].issue(
            scope.email,
            invite_id=scope.invite_id,
            max_age_seconds=cookie_max_age,
        ),
        max_age=cookie_max_age,
        httponly=True,
        secure=True,
        samesite="Strict",
        path="/",
    )
    return response


async def latest_code(request: web.Request) -> web.Response:
    if not _same_origin_json(request):
        return request.app[VIEW_KEY].error("请求来源无效", 403, state="forbidden")
    try:
        payload: Any = await request.json(loads=json.loads)
    except (json.JSONDecodeError, web.HTTPBadRequest, TypeError, ValueError):
        return request.app[VIEW_KEY].error("请求格式无效", 400)
    if not isinstance(payload, dict):
        return request.app[VIEW_KEY].error("请求格式无效", 400)
    raw_email = str(payload.get("email") or "")
    try:
        target_email = normalize_zkgmail_address(raw_email)
    except InvalidAddressError:
        target_email = ""
    settings = request.app[SETTINGS_KEY]
    limit_keys = [f"ip:{_client_key(request)}"]
    if settings.require_invite:
        access_email = _access_scope(request)
        if not access_email:
            return request.app[VIEW_KEY].error(
                "请使用有效邀请链接访问",
                401,
                state="unauthorized",
            )
        if target_email and not hmac.compare_digest(target_email, access_email):
            return request.app[VIEW_KEY].error(
                "该邀请链接未授权此邮箱",
                403,
                state="forbidden",
            )
        cookie_digest = hashlib.sha256(
            str(request.cookies.get(ACCESS_COOKIE_NAME) or "").encode("utf-8")
        ).hexdigest()
        limit_keys.append(f"session:{cookie_digest}")
    email_digest = hashlib.sha256(target_email.encode("utf-8")).hexdigest()
    limit_keys.append(f"email:{email_digest}")
    for limit_key in limit_keys:
        allowed, retry_after = request.app[LIMITER_KEY].allow(limit_key)
        if not allowed:
            response = request.app[VIEW_KEY].error(
                "请求过于频繁，请稍后再试", 429, state="limited"
            )
            response.headers["Retry-After"] = str(retry_after)
            return response
    model = await request.app[PRESENTER_KEY].lookup(
        raw_email,
        after_cursor=str(payload.get("afterCursor") or ""),
    )
    return request.app[VIEW_KEY].lookup(model)


async def favicon(_: web.Request) -> web.Response:
    return web.Response(status=204)


async def not_found(request: web.Request) -> web.Response:
    return request.app[VIEW_KEY].error("页面不存在", 404, state="not_found")


def create_app(
    *,
    repository: CodeRepository | None = None,
    settings: Settings | None = None,
) -> web.Application:
    current_settings = settings or Settings.from_env()
    if current_settings.require_invite and not current_settings.access_protected:
        raise RuntimeError("ZKGMAIL_ACCESS_TOKEN must be a 64-character hex secret")
    if repository is None:
        imap_repository = ImapCodeRepository(
            current_settings,
            KeywordCodeExtractor(),
            trusted_recipient_headers=(current_settings.trusted_recipient_header,),
            max_concurrent_queries=current_settings.imap_max_concurrent_queries,
        )
        repository = CachedCodeRepository(
            imap_repository,
            ttl_seconds=current_settings.cache_ttl_seconds,
        )
    app = web.Application(middlewares=[security_headers], client_max_size=16 * 1024)
    app[SETTINGS_KEY] = current_settings
    app[PRESENTER_KEY] = LookupPresenter(repository)
    app[VIEW_KEY] = PortalView()
    app[LIMITER_KEY] = SlidingWindowRateLimiter(
        request_limit=current_settings.rate_limit_requests,
        window_seconds=current_settings.rate_limit_window_seconds,
        max_keys=current_settings.rate_limit_max_keys,
    )
    app[ACCESS_LIMITER_KEY] = SlidingWindowRateLimiter(
        request_limit=current_settings.access_rate_limit_requests,
        window_seconds=current_settings.access_rate_limit_window_seconds,
        max_keys=current_settings.rate_limit_max_keys,
    )
    app[ACCESS_STORE_KEY] = AccessSessionStore(
        max_age_seconds=current_settings.session_max_age_seconds,
        max_sessions=current_settings.session_max_sessions,
        max_sessions_per_invite=current_settings.session_max_per_invite,
        storage_path=current_settings.session_store_path or None,
    )
    if current_settings.require_invite:
        app[INVITE_SERVICE_KEY] = InviteTokenService(current_settings.access_token)
    app.router.add_get("/", index)
    app.router.add_get("/assets/app.css", stylesheet)
    app.router.add_get("/assets/app.js", script)
    app.router.add_get("/healthz", health)
    app.router.add_post("/api/access", establish_access)
    app.router.add_post("/api/code/latest", latest_code)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_route("*", "/{tail:.*}", not_found)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the zkgmail.com code portal")
    parser.add_argument("--host", default=os.environ.get("ZKGMAIL_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ZKGMAIL_PORT", "18768")),
    )
    args = parser.parse_args()
    web.run_app(create_app(), host=args.host, port=args.port, access_log_format='%a "%r" %s %Tf')


if __name__ == "__main__":
    main()
