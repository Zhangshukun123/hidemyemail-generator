import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestServer

from hidemyemail_generator.liandong_shop import (
    LIANDONG_SHOP_TOKEN_ENV,
    UNBOUND_GOODS,
    LiandongShopClient,
    LiandongShopConfigStore,
    LiandongShopError,
    card_upload_for_account,
)


class LiandongShopTests(unittest.IsolatedAsyncioTestCase):
    def test_card_format_and_legacy_phone_binding_routing(self):
        goods, content = card_upload_for_account(
            "Seller@zkgmail.com",
            {
                "account_type": "plus",
                "password": "pass\nword",
                "password_confirmed": True,
                "two_factor": {"enabled": True, "secret": "ABC\rDEF"},
                "plus_sms": {"phone_bound": True},
            },
        )

        self.assertEqual(goods.goods_id, 685418)
        self.assertEqual(content, "seller@zkgmail.com----password----ABCDEF")

    def test_requires_enabled_two_factor(self):
        with self.assertRaisesRegex(LiandongShopError, "2FA"):
            card_upload_for_account(
                "seller@zkgmail.com",
                {
                    "account_type": "plus",
                    "password": "password",
                    "password_confirmed": True,
                    "two_factor": {"enabled": False, "secret": "ABC"},
                },
            )

    def test_config_status_does_not_expose_token_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LiandongShopConfigStore(Path(temp_dir) / "accounts.db")
            store.save("database-secret")
            with mock.patch.dict(
                os.environ, {LIANDONG_SHOP_TOKEN_ENV: "environment-secret"}
            ):
                token, source = store.token()
                status = store.public_status()

        self.assertEqual(token, "environment-secret")
        self.assertEqual(source, "environment")
        self.assertTrue(status["configured"])
        self.assertNotIn("secret", json.dumps(status))

    async def test_client_sends_fixed_inventory_payload_and_token_header(self):
        received = {}

        async def add_card(request):
            received["token"] = request.headers.get("Merchant-Token")
            received["body"] = await request.json()
            return web.json_response({"code": 1, "msg": "添加成功"})

        app = web.Application()
        app.router.add_post("/add", add_card)
        server = TestServer(app)
        await server.start_server()
        try:
            client = LiandongShopClient(str(server.make_url("/add")))
            result = await client.upload_card(
                token="merchant-secret",
                goods=UNBOUND_GOODS,
                content="email----password----totp",
            )
        finally:
            await server.close()

        self.assertEqual(result["code"], 1)
        self.assertEqual(received["token"], "merchant-secret")
        self.assertEqual(
            received["body"],
            {
                "goods_id": 698207,
                "content": "email----password----totp",
                "first": 0,
                "remove_repeat": 1,
            },
        )

    async def test_client_treats_code_zero_as_failure(self):
        async def reject_card(_request):
            return web.json_response({"code": 0, "msg": "Token 无效"})

        app = web.Application()
        app.router.add_post("/add", reject_card)
        server = TestServer(app)
        await server.start_server()
        try:
            client = LiandongShopClient(str(server.make_url("/add")))
            with self.assertRaisesRegex(LiandongShopError, "Token 无效"):
                await client.upload_card(
                    token="invalid",
                    goods=UNBOUND_GOODS,
                    content="email----password----totp",
                )
        finally:
            await server.close()


if __name__ == "__main__":
    unittest.main()
