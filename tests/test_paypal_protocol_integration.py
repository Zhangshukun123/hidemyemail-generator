import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.webapp import create_app


class PayPalProtocolProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        project_dir = Path(self.temp_dir.name) / "paypal-agreement-protocol"
        project_dir.mkdir()
        (project_dir / "web.py").write_text("# proxy fixture\n", encoding="utf-8")

        upstream = web.Application()

        async def upstream_handler(request: web.Request) -> web.Response:
            if request.path == "/api/health":
                return web.json_response({"ok": True})
            response = web.Response(
                text="<html><body>PayPal fixture</body></html>",
                content_type="text/html",
                headers={
                    "Content-Security-Policy": (
                        "default-src 'self'; frame-ancestors 'none'"
                    ),
                    "X-Frame-Options": "DENY",
                },
            )
            response.set_cookie("paypal_web_device_id", "fixture", path="/")
            return response

        upstream.router.add_route("*", "/{tail:.*}", upstream_handler)
        self.upstream_server = TestServer(upstream)
        await self.upstream_server.start_server()

        app = create_app(
            base_dir=Path(self.temp_dir.name),
            paypal_project_dir=str(project_dir),
            paypal_port=self.upstream_server.port,
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream_server.close()
        self.temp_dir.cleanup()

    async def test_status_reports_running_upstream(self):
        response = await self.client.get("/api/paypal/status")
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["available"])
        self.assertTrue(payload["running"])
        self.assertEqual(payload["url"], "/paypal-pay/")

    async def test_proxy_maps_prefix_and_allows_same_origin_frame(self):
        response = await self.client.get("/paypal-pay/")
        page = await response.text()

        self.assertEqual(response.status, 200)
        self.assertIn("PayPal fixture", page)
        self.assertIn(
            "frame-ancestors 'self'", response.headers["Content-Security-Policy"]
        )
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("Path=/paypal-pay/", response.headers["Set-Cookie"])


if __name__ == "__main__":
    unittest.main()
