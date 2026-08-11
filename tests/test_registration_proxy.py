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
from hidemyemail_generator.clash_proxy import ClashRotationResult
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

    def test_clash_mode_persists_rotation_and_never_returns_secret(self):
        calls = []

        class FakeClient:
            def rotate_japanese_proxy(self, **kwargs):
                calls.append(kwargs)
                index = len(calls)
                return ClashRotationResult(
                    proxy_url="http://127.0.0.1:7897",
                    selector="主节点选择",
                    node=f"日本节点-{index}",
                    latency_ms=100 + index,
                    next_cursor=index,
                    skipped=index - 1,
                    candidate_count=4,
                    exit_ip=f"192.0.2.{index}",
                    exit_country="JP",
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            store = RegistrationProxyStore(
                Path(temp_dir) / "hme.db",
                clash_client_factory=lambda _connection: FakeClient(),
            )
            configured = store.configure(
                enabled=True,
                mode="clash",
                clash_controller="http://127.0.0.1:9097",
                clash_secret="controller-private-secret",
                clash_proxy_url="http://127.0.0.1:7897",
                max_latency_ms=900,
            )
            first_url, first = store.next_proxy()
            second_url, second = store.next_proxy()

        self.assertEqual(first_url, "http://127.0.0.1:7897")
        self.assertEqual(second_url, "http://127.0.0.1:7897")
        self.assertEqual(first["currentNode"], "日本节点-1")
        self.assertEqual(second["currentNode"], "日本节点-2")
        self.assertEqual(calls[0]["cursor"], 0)
        self.assertEqual(calls[1]["cursor"], 1)
        self.assertEqual(calls[1]["previous_node"], "日本节点-1")
        self.assertEqual(calls[1]["previous_exit_ip"], "192.0.2.1")
        self.assertEqual(configured["mode"], "clash")
        self.assertEqual(configured["country"], "JP")
        self.assertNotIn("controller-private-secret", json.dumps(second))
        self.assertNotIn("secret", second)
        self.assertNotIn("192.0.2.2", json.dumps(second))
        self.assertTrue(second["exitIpVerified"])

    def test_clash_registration_uses_fixed_port_map_without_touching_normal_port(self):
        calls = []

        class FakeClient:
            def rotate_japanese_proxy(self, **kwargs):
                calls.append(kwargs)
                return ClashRotationResult(
                    proxy_url="http://127.0.0.1:19002",
                    selector="主节点选择",
                    node="日本二号",
                    latency_ms=120,
                    next_cursor=2,
                    skipped=1,
                    candidate_count=2,
                    exit_ip="192.0.2.22",
                    exit_country="JP",
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output"
            output.mkdir()
            (output / "clash-jp-fixed-ports.json").write_text(
                json.dumps(
                    {
                        "ports": [
                            {"node": "日本一号", "port": 19001},
                            {"node": "日本二号", "port": 19002},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            store = RegistrationProxyStore(
                root / "hme.db",
                clash_client_factory=lambda _connection: FakeClient(),
            )
            store.configure(
                enabled=True,
                mode="clash",
                clash_controller="http://127.0.0.1:9097",
                clash_proxy_url="http://127.0.0.1:7897",
            )

            proxy_url, state = store.next_proxy()

        self.assertEqual(proxy_url, "http://127.0.0.1:19002")
        self.assertEqual(
            calls[0]["fixed_ports"],
            {
                "日本一号": "http://127.0.0.1:19001",
                "日本二号": "http://127.0.0.1:19002",
            },
        )
        self.assertTrue(state["fixedPortsEnabled"])
        self.assertEqual(state["fixedPortBase"], 19000)
        self.assertEqual(state["fixedPortCount"], 2)
        self.assertEqual(state["normalEndpoint"], "http://127.0.0.1:7897")
        self.assertEqual(state["endpoint"], "http://127.0.0.1:19002")


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

    async def test_clash_config_does_not_return_controller_secret(self):
        response = await self.client.post(
            "/api/registration-proxy/config",
            json={
                "enabled": True,
                "mode": "clash",
                "country": "JP",
                "clashController": "http://127.0.0.1:9097",
                "clashProxyUrl": "http://127.0.0.1:7897",
                "clashSecret": "controller-private-secret",
                "maxLatencyMs": 900,
            },
            headers={"X-Local-Token": self.app["local_token"]},
        )
        body = await response.text()

        self.assertEqual(response.status, 200)
        self.assertNotIn("controller-private-secret", body)
        self.assertNotIn("clashSecret", body)
        self.assertEqual(json.loads(body)["mode"], "clash")


if __name__ == "__main__":
    unittest.main()
