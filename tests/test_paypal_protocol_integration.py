import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.webapp import create_app


class PayPalProtocolProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.upstream_calls = []
        project_dir = Path(self.temp_dir.name) / "paypal-agreement-protocol"
        project_dir.mkdir()
        (project_dir / "web.py").write_text("# proxy fixture\n", encoding="utf-8")

        upstream = web.Application()

        async def upstream_handler(request: web.Request) -> web.Response:
            if request.path == "/api/health":
                return web.json_response({"ok": True})
            if request.path == "/api/jobs/job-legacy-1234":
                return web.json_response(
                    {
                        "id": "job-legacy-1234",
                        "status": "failed",
                        "stage": "最终授权失败",
                        "error": "BRAINTREE_VAULT_FAILED",
                        "source_account_email": "payment-at@icloud.com",
                        "result": {
                            "status": "error",
                            "error_code": "BRAINTREE_VAULT_FAILED",
                            "error": "bridge refused",
                            "paypal_authorized": True,
                            "redirect_status": "succeeded",
                            "settlement_status": "vault_failed",
                            "final_redirect_url": (
                                "https://pay.openai.com/complete?"
                                "redirect_status=succeeded"
                            ),
                            "verification_url": (
                                "https://chatgpt.com/checkout/verify?plan_type=plus"
                            ),
                        },
                    }
                )
            if request.path == "/api/jobs/job-12345678":
                self.upstream_calls.append(
                    {
                        "cookie": request.cookies.get("paypal_web_device_id"),
                        "log_offset": request.query.get("log_offset"),
                        "log_after": request.query.get("log_after"),
                    }
                )
                return web.json_response(
                    {
                        "id": "job-12345678",
                        "status": "completed",
                        "stage": "已完成",
                        "source_account_email": "payment-at@icloud.com",
                        "result": {
                            "status": "success",
                            "settlement_status": "confirmed",
                        },
                        "logs": [
                            {
                                "time": 1_787_000_000,
                                "level": "SUCCESS",
                                "message": "Protocol payment completed",
                                "sequence": 8,
                            }
                        ],
                        "log_count": 8,
                        "log_sequence": 8,
                    }
                )
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
        self.payment_completion_presenter = mock.Mock()
        self.payment_completion_presenter.confirm = mock.AsyncMock(
            return_value={
                "job_id": "job-12345678",
                "email": "payment-at@icloud.com",
                "status": "plus",
                "protocol_succeeded": True,
                "plus_confirmed": True,
                "payment_succeeded": True,
                "at_refreshed": True,
                "at_changed": True,
                "account_type": "plus",
                "plan": "plus",
                "detail": "支付后 Cookie 已刷新新 AT，已确认 Plus",
            }
        )
        app["payment_completion_presenter"] = self.payment_completion_presenter
        self.plus_codex_presenter = mock.Mock()
        self.plus_codex_presenter.ensure = mock.AsyncMock(
            return_value={
                "job_id": "job-12345678",
                "email": "payment-at@icloud.com",
                "status": "completed",
                "stage": "completed",
                "detail": "Plus 协议接码及 Codex OAuth 已完成",
                "provider": "smsbower",
                "sms_verified": True,
                "export_ready": True,
            }
        )
        self.plus_codex_presenter.close = mock.AsyncMock()
        self.plus_codex_presenter.valid_code_token.return_value = False
        app["plus_codex_presenter"] = self.plus_codex_presenter
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

    async def test_job_status_forwards_device_cookie_and_incremental_log_cursor(self):
        response = await self.client.get(
            "/api/account/paypal-payment/job-12345678?log_offset=0&log_after=7",
            cookies={"hme_paypal_auto_device_id": "a" * 32},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200, payload)
        self.assertEqual(payload["job"]["status"], "completed")
        self.assertEqual(payload["job"]["result"]["status"], "success")
        self.assertEqual(payload["job"]["account_confirmation"]["status"], "plus")
        self.assertTrue(payload["job"]["account_confirmation"]["payment_succeeded"])
        self.assertEqual(payload["job"]["logs"][0]["sequence"], 8)
        self.payment_completion_presenter.confirm.assert_awaited_once()
        self.plus_codex_presenter.ensure.assert_awaited_once()
        self.assertEqual(
            self.upstream_calls[-1],
            {"cookie": "a" * 32, "log_offset": "0", "log_after": "7"},
        )

    async def test_job_status_requires_the_automatic_payment_session(self):
        response = await self.client.get("/api/account/paypal-payment/job-12345678")
        payload = await response.json()

        self.assertEqual(response.status, 404)
        self.assertIn("会话已失效", payload["error"])

    async def test_legacy_openai_bridge_false_negative_is_returned_as_success(self):
        response = await self.client.get(
            "/api/account/paypal-payment/job-legacy-1234",
            cookies={"hme_paypal_auto_device_id": "a" * 32},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200, payload)
        job = payload["job"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["status"], "success")
        self.assertEqual(job["result"]["settlement_status"], "confirmed")
        self.assertEqual(
            job["result"]["braintree_bridge_status"], "not_applicable"
        )
        self.assertEqual(job["account_confirmation"]["status"], "plus")
        normalized = self.payment_completion_presenter.confirm.await_args.args[0]
        self.assertEqual(normalized["status"], "completed")


if __name__ == "__main__":
    unittest.main()
