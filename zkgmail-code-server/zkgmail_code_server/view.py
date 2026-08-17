from __future__ import annotations

import json
from importlib.resources import files

from aiohttp import web

from .domain import LookupViewModel


class PortalView:
    """MVP view responsible only for rendering HTTP representations."""

    def __init__(self) -> None:
        static_root = files("zkgmail_code_server").joinpath("static")
        self._index = static_root.joinpath("index.html").read_text(encoding="utf-8")
        self._css = static_root.joinpath("app.css").read_text(encoding="utf-8")
        self._js = static_root.joinpath("app.js").read_text(encoding="utf-8")

    def page(self) -> web.Response:
        return web.Response(text=self._index, content_type="text/html")

    def stylesheet(self) -> web.Response:
        return web.Response(text=self._css, content_type="text/css", headers=self._asset_headers())

    def script(self) -> web.Response:
        return web.Response(
            text=self._js,
            content_type="application/javascript",
            headers=self._asset_headers(),
        )

    def lookup(self, model: LookupViewModel) -> web.Response:
        return web.json_response(model.as_payload(), status=model.status)

    @staticmethod
    def error(message: str, status: int, *, state: str = "invalid") -> web.Response:
        return web.Response(
            text=json.dumps(
                {"ok": False, "state": state, "error": message, "message": message},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            status=status,
            content_type="application/json",
        )

    @staticmethod
    def _asset_headers() -> dict[str, str]:
        return {"Cache-Control": "public, max-age=3600"}
