from __future__ import annotations

import argparse
import asyncio
import hmac
import json
from importlib.resources import files
from typing import Any

from aiohttp import web
from hidemyemail_generator.protocol_registration import PROTOCOL_CODE_PREFIX

from .model import AccountExportRepository
from .presenter import DEFAULT_OFFER_COUNTRIES, ServerRegistrationPresenter
from .settings import ServerSettings


PUBLIC_PATHS = {"/", "/healthz", "/assets/app.css", "/assets/app.js"}
CODE_PREFIX = PROTOCOL_CODE_PREFIX
SETTINGS_KEY = web.AppKey("settings", ServerSettings)
PRESENTER_KEY = web.AppKey("presenter", object)
ACCOUNTS_KEY = web.AppKey("accounts", AccountExportRepository)


def _asset(name: str) -> str:
    return files("protocol_registration_server").joinpath("static", name).read_text(
        encoding="utf-8"
    )


def _supplied_token(request: web.Request) -> str:
    authorization = str(request.headers.get("Authorization") or "")
    scheme, _, value = authorization.partition(" ")
    if scheme.casefold() == "bearer":
        return value.strip()
    return str(request.headers.get("X-Protocol-Token") or "").strip()


@web.middleware
async def token_middleware(
    request: web.Request,
    handler,
) -> web.StreamResponse:
    if request.path in PUBLIC_PATHS or request.path.startswith(CODE_PREFIX):
        return await handler(request)
    supplied = _supplied_token(request)
    expected = request.app[SETTINGS_KEY].api_token
    if supplied and hmac.compare_digest(supplied, expected):
        return await handler(request)
    return web.json_response(
        {"ok": False, "error": "服务器注册 API 认证失败"},
        status=401,
        headers={"Cache-Control": "no-store"},
    )


def _no_store(payload: dict[str, Any], *, status: int = 200) -> web.Response:
    return web.json_response(
        payload,
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def create_app(
    settings: ServerSettings,
    *,
    presenter: ServerRegistrationPresenter | None = None,
) -> web.Application:
    app = web.Application(middlewares=[token_middleware], client_max_size=1024**2)
    app[SETTINGS_KEY] = settings
    app[PRESENTER_KEY] = presenter or ServerRegistrationPresenter(settings)
    app[ACCOUNTS_KEY] = AccountExportRepository(
        settings.shared_db,
        app[PRESENTER_KEY].offer_repository,
    )

    async def index(_: web.Request) -> web.Response:
        return web.Response(
            text=_asset("index.html"),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def app_css(_: web.Request) -> web.Response:
        return web.Response(
            text=_asset("app.css"),
            content_type="text/css",
            headers={"Cache-Control": "public, max-age=300"},
        )

    async def app_js(_: web.Request) -> web.Response:
        return web.Response(
            text=_asset("app.js"),
            content_type="application/javascript",
            headers={"Cache-Control": "public, max-age=300"},
        )

    async def health(_: web.Request) -> web.Response:
        runtime = app[PRESENTER_KEY].manager.snapshot().get("runtime", {})
        return _no_store(
            {
                "ok": True,
                "service": "protocol-registration-server",
                "runtimeAvailable": bool(runtime.get("available")),
            }
        )

    async def status(_: web.Request) -> web.Response:
        return _no_store({"ok": True, **app[PRESENTER_KEY].snapshot()})

    async def start(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return _no_store({"ok": False, "error": "请求格式无效"}, status=400)
        if not isinstance(payload, dict):
            return _no_store({"ok": False, "error": "请求格式无效"}, status=400)
        check_offer = payload.get("checkOffer", False)
        setup_credentials = payload.get("setupCredentials", False)
        use_registration_kookeey = payload.get("useRegistrationKookeey", False)
        offer_countries = payload.get("offerCountries", list(DEFAULT_OFFER_COUNTRIES))
        if not all(
            isinstance(value, bool)
            for value in (
                check_offer,
                setup_credentials,
                use_registration_kookeey,
            )
        ):
            return _no_store(
                {"ok": False, "error": "布尔配置字段无效"}, status=400
            )
        if not isinstance(offer_countries, list):
            return _no_store(
                {"ok": False, "error": "优惠检查国家必须是数组"}, status=400
            )
        try:
            count = int(payload["count"] if "count" in payload else 1)
            concurrency = int(
                payload["concurrency"] if "concurrency" in payload else 1
            )
            result = await app[PRESENTER_KEY].start(
                count=count,
                provider=str(payload.get("provider") or "inventory"),
                concurrency=concurrency,
                use_registration_kookeey=use_registration_kookeey,
                registration_country=str(
                    payload.get("registrationCountry") or "JP"
                ),
                offer_countries=[str(value or "") for value in offer_countries],
                check_offer=check_offer,
                setup_credentials=setup_credentials,
            )
        except ValueError as error:
            return _no_store({"ok": False, "error": str(error)}, status=400)
        except RuntimeError as error:
            return _no_store({"ok": False, "error": str(error)}, status=409)
        return _no_store({"ok": True, "started": True, **result})

    async def stop(_: web.Request) -> web.Response:
        result = await app[PRESENTER_KEY].stop()
        return _no_store({"ok": True, **result})

    async def accounts(request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "100") or 100)
            result = await asyncio.to_thread(
                app[ACCOUNTS_KEY].export,
                pool=str(request.query.get("pool") or ""),
                limit=limit,
                include_credentials=request.query.get("credentials") == "1",
            )
        except ValueError as error:
            return _no_store({"ok": False, "error": str(error)}, status=400)
        return _no_store({"ok": True, **result})

    async def refresh_offer(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return _no_store({"ok": False, "error": "请求格式无效"}, status=400)
        if not isinstance(payload, dict):
            return _no_store({"ok": False, "error": "请求格式无效"}, status=400)
        countries = payload.get("countries", list(DEFAULT_OFFER_COUNTRIES))
        if not isinstance(countries, list):
            return _no_store(
                {"ok": False, "error": "优惠检查国家必须是数组"}, status=400
            )
        try:
            result = await app[PRESENTER_KEY].refresh_offer(
                str(payload.get("email") or ""),
                [str(value or "") for value in countries],
            )
        except (TypeError, ValueError) as error:
            status_code = 404 if "不存在" in str(error) else 400
            return _no_store({"ok": False, "error": str(error)}, status=status_code)
        return _no_store({"ok": True, "offer": result})

    async def code(request: web.Request) -> web.Response:
        token = str(request.match_info.get("token") or "")
        record = app[PRESENTER_KEY].token_record(token)
        if not record:
            return web.Response(
                text="协议取码令牌已失效",
                status=404,
                headers={"Cache-Control": "no-store"},
            )
        code_status, value = await app[PRESENTER_KEY].code_client.fetch(
            str(record.get("email") or ""),
            str(record.get("since") or ""),
        )
        return web.Response(
            text=value,
            status=code_status,
            headers={"Cache-Control": "no-store"},
        )

    async def lifecycle(_: web.Application):
        yield
        await app[PRESENTER_KEY].close()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/", index)
    app.router.add_get("/assets/app.css", app_css)
    app.router.add_get("/assets/app.js", app_js)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/tasks/start", start)
    app.router.add_post("/api/tasks/stop", stop)
    app.router.add_post("/api/offers/refresh", refresh_offer)
    app.router.add_get("/api/accounts", accounts)
    app.router.add_get(f"{CODE_PREFIX}{{token}}", code)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone protocol registration server")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    settings = ServerSettings.from_env()
    web.run_app(
        create_app(settings),
        host=args.host or settings.host,
        port=args.port or settings.port,
        access_log=None,
    )
