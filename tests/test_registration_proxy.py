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
from hidemyemail_generator.webapp import (
    _chatgpt_proxy_status_reachable,
    _registration_proxy_test_error,
    create_app,
)
from aiohttp import ClientHttpProxyError


class RegistrationProxyStoreTests(unittest.TestCase):
    def test_chatgpt_403_still_proves_proxy_destination_is_reachable(self):
        self.assertTrue(_chatgpt_proxy_status_reachable(200))
        self.assertTrue(_chatgpt_proxy_status_reachable(403))
        self.assertFalse(_chatgpt_proxy_status_reachable(0))
        self.assertFalse(_chatgpt_proxy_status_reachable(502))

    def test_proxy_http_tunnel_error_has_actionable_safe_message(self):
        error = ClientHttpProxyError(
            request_info=None,
            history=(),
            status=407,
            message="Proxy Authentication Required",
        )

        detail = _registration_proxy_test_error(error)

        self.assertIn("代理网关拒绝 HTTPS 连接", detail)
        self.assertNotIn("Proxy Authentication Required", detail)

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

    def test_kookeey_builds_country_session_and_duration_in_password(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RegistrationProxyStore(Path(temp_dir) / "hme.db")
            public = store.configure(
                enabled=True,
                mode="kookeey",
                country="BR",
                proxy_endpoint="gate.kookeey.info:1000",
                proxy_username="1234567-AbCdEf1234",
                proxy_password="secret-global-OldSid01-5m",
            )
            first_url, _ = store.next_proxy()
            second_url, _ = store.next_proxy()

        first = urlsplit(first_url)
        second = urlsplit(second_url)
        first_password = unquote(first.password or "")
        second_password = unquote(second.password or "")

        self.assertEqual(public["mode"], "kookeey")
        self.assertEqual(public["country"], "BR")
        self.assertEqual(public["dynamicEndpoint"], "gate.kookeey.info:1000")
        self.assertTrue(public["usernameConfigured"])
        self.assertTrue(public["passwordConfigured"])
        self.assertEqual(unquote(first.username or ""), "1234567-AbCdEf1234")
        self.assertRegex(first_password, r"^secret-BR-[A-Za-z0-9]{8}-5m$")
        self.assertRegex(second_password, r"^secret-BR-[A-Za-z0-9]{8}-5m$")
        self.assertNotEqual(first_password, second_password)

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

    def test_card_link_country_choices_persist_without_changing_registration_country(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RegistrationProxyStore(Path(temp_dir) / "hme.db")
            store.configure(
                enabled=False,
                country="NL",
                proxy_line="proxy.example:3010:private-user:private-password",
            )
            saved = store.configure(
                card_link_countries={"phCreate": "US", "phPromotion": "TR", "de": "DE"}
            )
            proxy_url, _ = store.proxy_for_country("DE")
            persisted = store.public_state()

        parsed = urlsplit(proxy_url)
        self.assertEqual(saved["cardLinkCountries"]["de"], "DE")
        self.assertIn("-region-DE-sid-", unquote(parsed.username or ""))
        self.assertEqual(persisted["country"], "NL")
        self.assertEqual(persisted["cardLinkCountries"]["phPromotion"], "TR")

    def test_card_link_proxy_mode_is_saved_and_does_not_change_registration_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RegistrationProxyStore(Path(temp_dir) / "hme.db")
            store.configure(
                enabled=True,
                mode="kookeey",
                country="BR",
                proxy_endpoint="gate.kookeey.info:1000",
                proxy_username="1234567-AbCdEf1234",
                proxy_password="private-secret",
            )
            saved = store.configure(
                card_link_modes={"de_oaics_paypal": "dynamic"}
            )
            proxy_url, _ = store.proxy_for_country("DE", mode="dynamic")
            persisted = store.public_state()

        self.assertEqual(saved["cardLinkModes"]["de_oaics_paypal"], "dynamic")
        self.assertIn("-region-DE-sid-", unquote(urlsplit(proxy_url).username or ""))
        self.assertEqual(persisted["mode"], "kookeey")
        self.assertTrue(
            next(item for item in persisted["modes"] if item["code"] == "dynamic")[
                "configured"
            ]
        )

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

    async def test_kookeey_proxy_config_accepts_separate_workbench_fields(self):
        response = await self.client.post(
            "/api/registration-proxy/config",
            json={
                "enabled": True,
                "mode": "kookeey",
                "country": "BR",
                "proxyEndpoint": "gate.kookeey.info:1000",
                "proxyUsername": "1234567-AbCdEf1234",
                "proxyPassword": "private-base-password",
            },
            headers={"X-Local-Token": self.app["local_token"]},
        )
        body = await response.text()

        self.assertEqual(response.status, 200)
        self.assertNotIn("1234567-AbCdEf1234", body)
        self.assertNotIn("private-base-password", body)
        state = json.loads(body)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["mode"], "kookeey")
        self.assertEqual(state["country"], "BR")
        self.assertEqual(state["endpoint"], "gate.kookeey.info:1000")
        self.assertTrue(state["usernameConfigured"])
        self.assertTrue(state["passwordConfigured"])

    async def test_card_link_country_choices_are_saved_through_proxy_config(self):
        response = await self.client.post(
            "/api/registration-proxy/config",
            json={"cardLinkCountries": {"de": "GB", "phPromotion": "TR"}},
            headers={"X-Local-Token": self.app["local_token"]},
        )
        status = await self.client.get("/api/registration-proxy/status")

        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["cardLinkCountries"]["de"], "GB")
        self.assertEqual(
            (await status.json())["cardLinkCountries"]["phPromotion"], "TR"
        )

    async def test_card_link_proxy_mode_is_saved_through_proxy_config(self):
        response = await self.client.post(
            "/api/registration-proxy/config",
            json={"cardLinkModes": {"de_oaics_paypal": "clash"}},
            headers={"X-Local-Token": self.app["local_token"]},
        )
        status = await self.client.get("/api/registration-proxy/status")

        self.assertEqual(response.status, 200)
        self.assertEqual(
            (await response.json())["cardLinkModes"]["de_oaics_paypal"],
            "clash",
        )
        self.assertEqual(
            (await status.json())["cardLinkModes"]["de_oaics_paypal"],
            "clash",
        )

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

    async def test_proxy_test_uses_configured_country_and_returns_safe_result(self):
        await self.client.post(
            "/api/registration-proxy/config",
            json={
                "enabled": True,
                "country": "JP",
                "proxyLine": "proxy.example:3010:private-user:private-password",
            },
            headers={"X-Local-Token": self.app["local_token"]},
        )
        captured = {}

        async def tester(proxy_url, expected_country):
            captured["proxy_url"] = proxy_url
            captured["expected_country"] = expected_country
            return {
                "ok": True,
                "exitIp": "203.0.113.8",
                "country": "JP",
                "expectedCountry": "JP",
                "countryMatches": True,
                "chatgptStatus": 200,
                "latencyMs": 123,
                "message": "出口 203.0.113.8 · JP · 延迟 123 ms · ChatGPT HTTP 200",
                "testedAt": "2026-08-12T00:00:00+00:00",
            }

        self.app["registration_proxy_tester"] = tester
        denied = await self.client.post("/api/registration-proxy/test", json={})
        response = await self.client.post(
            "/api/registration-proxy/test",
            json={},
            headers={"X-Local-Token": self.app["local_token"]},
        )
        body = await response.text()

        self.assertEqual(denied.status, 403)
        self.assertEqual(response.status, 200)
        self.assertEqual(captured["expected_country"], "JP")
        self.assertIn("private-user", captured["proxy_url"])
        self.assertNotIn("private-user", body)
        self.assertNotIn("private-password", body)
        result = json.loads(body)["testResult"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["country"], "JP")
        self.assertEqual(result["chatgptStatus"], 200)


if __name__ == "__main__":
    unittest.main()
