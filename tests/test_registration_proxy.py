import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.registration_proxy import (
    RegistrationProxyStore,
    parse_proxy_credential,
)
from hidemyemail_generator.webapp import create_app


class RegistrationProxyStoreTests(unittest.TestCase):
    def test_replaces_supplied_country_and_sid_with_a_fresh_selected_session(self):
        credential = (
            "proxy.example:3010:customer-region-BR-sid-AbCd1234-t-5:local-secret"
        )
        parsed = parse_proxy_credential(credential)

        self.assertEqual(parsed["endpoint"], "proxy.example:3010")
        self.assertEqual(parsed["username"], "customer")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = RegistrationProxyStore(Path(temp_dir) / "hme.db")
            public = store.configure(
                enabled=True,
                country="NL",
                proxy_line=credential,
            )
            first_url, _ = store.next_proxy()
            second_url, _ = store.next_proxy()

        first = urlsplit(first_url)
        second = urlsplit(second_url)
        first_username = unquote(first.username or "")
        second_username = unquote(second.username or "")

        self.assertTrue(public["enabled"])
        self.assertEqual(public["country"], "NL")
        self.assertEqual(public["endpoint"], "proxy.example:3010")
        self.assertIn("-region-NL-sid-", first_username)
        self.assertIn("-region-NL-sid-", second_username)
        self.assertNotIn("-region-BR-", first_username)
        self.assertNotEqual(first_username, second_username)
        self.assertEqual(unquote(first.password or ""), "local-secret")

    def test_public_state_never_returns_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RegistrationProxyStore(Path(temp_dir) / "hme.db")
            public = store.configure(
                enabled=True,
                country="NL",
                proxy_line="proxy.example:3010:private-user:private-password",
            )

            serialized = json.dumps(public)
            self.assertNotIn("private-user", serialized)
            self.assertNotIn("private-password", serialized)
            self.assertNotIn("username", public)
            self.assertNotIn("password", public)

            disabled = store.configure(enabled=False, country="JP")
            self.assertFalse(disabled["enabled"])
            self.assertEqual(disabled["country"], "JP")


class RegistrationProxyRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(base_dir=Path(self.temp_dir.name))
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_proxy_config_requires_token_and_returns_only_public_state(self):
        payload = {
            "enabled": True,
            "country": "NL",
            "proxyLine": "proxy.example:3010:private-user:private-password",
        }
        denied = await self.client.post(
            "/api/registration-proxy/config",
            json=payload,
        )
        self.assertEqual(denied.status, 403)

        response = await self.client.post(
            "/api/registration-proxy/config",
            json=payload,
            headers={"X-Local-Token": self.app["local_token"]},
        )
        body = await response.text()

        self.assertEqual(response.status, 200)
        self.assertNotIn("private-user", body)
        self.assertNotIn("private-password", body)
        state = json.loads(body)
        self.assertTrue(state["enabled"])
        self.assertTrue(state["configured"])
        self.assertEqual(state["country"], "NL")

        status = await self.client.get("/api/registration-proxy/status")
        status_body = await status.text()
        self.assertEqual(status.status, 200)
        self.assertNotIn("private-user", status_body)
        self.assertNotIn("private-password", status_body)


if __name__ == "__main__":
    unittest.main()
