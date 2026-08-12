from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.webapp import (
    WORKBENCH_INVENTORY_IMPORT_PATH,
    create_app,
)


class WorkbenchInventoryImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_extension_endpoint_forwards_unregistered_inventory(self):
        captured: list[dict] = []
        token = "inventory-test-token-at-least-32-characters"

        async def receive(request: web.Request) -> web.Response:
            self.assertEqual(request.headers.get("X-HME-Import-Token"), token)
            captured.append(await request.json())
            return web.json_response(
                {
                    "success": True,
                    "imported": True,
                    "updated": False,
                    "account": {
                        "email": captured[-1]["email"],
                        "inventoryOnly": True,
                        "status": "未注册",
                    },
                }
            )

        workbench = web.Application()
        workbench.router.add_post(
            "/api/integrations/hidemyemail/import", receive
        )
        workbench_server = TestServer(workbench)
        await workbench_server.start_server()
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                app = create_app(
                    base_dir=Path(temp_dir),
                    web_password="configured-password",
                    workbench_url=str(workbench_server.make_url("/")).rstrip("/"),
                    workbench_import_token=token,
                    deactivation_scan_interval_seconds=3600,
                )
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    response = await client.post(
                        WORKBENCH_INVENTORY_IMPORT_PATH,
                        json={
                            "email": "Fresh.Inventory@icloud.com",
                            "label": "购物",
                            "note": "Apple 扩展创建",
                            "createdAt": "2026-08-12T12:00:00Z",
                        },
                    )
                    payload = await response.json()
                finally:
                    await client.close()
        finally:
            await workbench_server.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["inventory"], "unregistered")
        self.assertTrue(payload["imported"])
        self.assertEqual(
            captured,
            [
                {
                    "email": "fresh.inventory@icloud.com",
                    "inventoryOnly": True,
                    "label": "购物",
                    "note": "Apple 扩展创建",
                    "createdAt": "2026-08-12T12:00:00Z",
                    "source": "apple-hide-my-email-extension",
                }
            ],
        )

    async def test_extension_endpoint_rejects_non_icloud_email(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(
                base_dir=Path(temp_dir),
                workbench_url="http://127.0.0.1:18766",
                workbench_import_token="test-token",
                deactivation_scan_interval_seconds=3600,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    WORKBENCH_INVENTORY_IMPORT_PATH,
                    json={"email": "not-inventory@example.com"},
                )
            finally:
                await client.close()

        self.assertEqual(response.status, 400)


if __name__ == "__main__":
    unittest.main()
