import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.roxy_registration import (
    RoxyOpenApiClient,
    RoxyRegistrationBrowser,
    RoxyRegistrationStore,
    normalize_roxy_api_url,
    roxy_cdp_endpoint,
    roxy_proxy_info,
)
from hidemyemail_generator.webapp import create_app


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeOpener:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return FakeResponse(self.payloads.pop(0))


class RoxyRegistrationTests(unittest.TestCase):
    def test_normalizes_local_api_and_local_airport_proxy(self):
        self.assertEqual(
            normalize_roxy_api_url("127.0.0.1:50000"),
            "http://127.0.0.1:50000",
        )
        self.assertEqual(
            roxy_proxy_info("http://user:pass@127.0.0.1:18001"),
            {
                "moduleId": 0,
                "proxyMethod": "custom",
                "proxyCategory": "HTTP",
                "protocol": "HTTP",
                "ipType": "IPV4",
                "host": "127.0.0.1",
                "port": "18001",
                "proxyUserName": "user",
                "proxyPassword": "pass",
            },
        )

    def test_open_profile_sends_background_choice_and_returns_cdp(self):
        opener = FakeOpener(
            {
                "code": 0,
                "data": {
                    "ws": "ws://127.0.0.1:52000/devtools/browser/test",
                    "pid": 123,
                },
            }
        )
        client = RoxyOpenApiClient(opener=opener)

        result = client.open_profile(136502, "profile-id", background=True)

        request = opener.requests[0][0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "http://127.0.0.1:50000/browser/open")
        self.assertEqual(payload["workspaceId"], 136502)
        self.assertEqual(payload["dirId"], "profile-id")
        self.assertTrue(payload["headless"])
        self.assertIn("--disable-save-password-bubble", payload["args"])
        self.assertEqual(roxy_cdp_endpoint(result), result["ws"])

    def test_store_saves_only_selected_dedicated_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RoxyRegistrationStore(Path(temp_dir) / "records.db")
            saved = store.configure(
                api_url="127.0.0.1:50000",
                workspace_id="136502",
                profile_id="dedicated-profile",
            )

            self.assertEqual(saved["workspaceId"], "136502")
            self.assertEqual(saved["profileId"], "dedicated-profile")
            self.assertEqual(store.runtime_config()["apiUrl"], "http://127.0.0.1:50000")

    def test_public_state_lists_profiles_and_requires_selected_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RoxyRegistrationStore(Path(temp_dir) / "records.db")
            store.configure(workspace_id="136502", profile_id="chosen")
            client = mock.Mock()
            client.health.return_value = "ok"
            client.list_workspaces.return_value = [
                {"id": 136502, "workspaceName": "Team"}
            ]
            client.list_profiles.return_value = [
                {
                    "dirId": "chosen",
                    "windowSortNum": 8,
                    "windowName": "Registration",
                    "os": "Windows",
                    "coreVersion": "150",
                }
            ]
            client.connection_info.return_value = []
            with mock.patch(
                "hidemyemail_generator.roxy_registration.RoxyOpenApiClient",
                return_value=client,
            ):
                state = store.public_state()

            self.assertTrue(state["available"])
            self.assertTrue(state["configured"])
            self.assertEqual(state["profiles"][0]["name"], "Registration")
            self.assertEqual(state["maxConcurrency"], 1)
            self.assertFalse(state["nativeHeadless"])

    def test_runtime_config_allocates_distinct_closed_profiles_selected_first(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RoxyRegistrationStore(Path(temp_dir) / "records.db")
            store.configure(workspace_id="136502", profile_id="profile-3")
            client = mock.Mock()
            client.health.return_value = "ok"
            client.list_workspaces.return_value = [
                {"id": 136502, "workspaceName": "Team"}
            ]
            client.list_profiles.return_value = [
                {
                    "dirId": f"profile-{index}",
                    "windowSortNum": index,
                    "windowName": f"Registration {index}",
                }
                for index in range(1, 5)
            ]
            client.connection_info.return_value = [{"dirId": "profile-2"}]
            with mock.patch(
                "hidemyemail_generator.roxy_registration.RoxyOpenApiClient",
                return_value=client,
            ):
                config = store.runtime_config(profile_count=3)

            self.assertEqual(
                config["profileIds"],
                ["profile-3", "profile-1", "profile-4"],
            )

    def test_browser_prepares_random_profile_and_injects_saved_cookies(self):
        events = []

        class Client:
            def connection_info(self, _profile_id):
                return []

            def clear_profile(self, workspace_id, profile_id):
                events.append(("clear", workspace_id, profile_id))

            def modify_profile(self, payload):
                events.append(("modify", payload))

            def randomize_profile(self, workspace_id, profile_id):
                events.append(("random", workspace_id, profile_id))

            def open_profile(self, workspace_id, profile_id, *, background):
                events.append(("open", workspace_id, profile_id, background))
                return {"http": "127.0.0.1:52000", "pid": 0}

            def close_profile(self, profile_id):
                events.append(("close", profile_id))

        class Context:
            def __init__(self):
                self.cookies = []
                self.routes = []

            def add_cookies(self, cookies):
                self.cookies.extend(cookies)

            def route(self, pattern, handler):
                self.routes.append((pattern, handler))

        context = Context()
        browser = mock.Mock(contexts=[context])
        chromium = mock.Mock()
        chromium.connect_over_cdp.return_value = browser
        playwright = mock.Mock(chromium=chromium)
        session = RoxyRegistrationBrowser(
            api_url="http://127.0.0.1:50000",
            api_token="",
            workspace_id=136502,
            profile_id="dedicated",
            proxy_url="http://127.0.0.1:18001",
            background=False,
            log=lambda message: events.append(("log", message)),
        )
        session.client = Client()

        returned_browser, returned_context = session.new_browser_context(
            playwright,
            None,
            {"cookies": [{"name": "session", "value": "x", "domain": ".chatgpt.com", "path": "/"}]},
        )
        session.close()

        self.assertIs(returned_browser, browser)
        self.assertIs(returned_context, context)
        self.assertEqual(context.cookies[0]["name"], "session")
        self.assertEqual(context.routes[0][0], "**/*")

        route_events = []

        class Route:
            def abort(self, **kwargs):
                route_events.append(("abort", kwargs.get("error_code")))

            def fallback(self):
                route_events.append(("fallback", None))

        context.routes[0][1](Route(), mock.Mock(resource_type="image"))
        context.routes[0][1](Route(), mock.Mock(resource_type="script"))
        self.assertEqual(
            route_events,
            [("abort", "blockedbyclient"), ("fallback", None)],
        )
        self.assertEqual(events[0][0], "log")
        modified_profile = next(item[1] for item in events if item[0] == "modify")
        self.assertFalse(modified_profile["fingerInfo"]["syncPassword"])
        self.assertTrue(modified_profile["fingerInfo"]["forbidSavePassword"])
        self.assertIn(("random", 136502, "dedicated"), events)
        self.assertIn(("open", 136502, "dedicated", False), events)
        self.assertTrue(
            any(item[0] == "log" and "已关闭图片加载" in item[1] for item in events)
        )
        self.assertTrue(
            any(item[0] == "log" and "已禁用 Google 密码保存" in item[1] for item in events)
        )
        self.assertEqual(events[-1], ("close", "dedicated"))


class RoxyRegistrationEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(base_dir=Path(self.temp_dir.name))
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_config_endpoint_saves_selected_profile(self):
        store = mock.Mock()
        store.configure.return_value = {}
        store.public_state.return_value = {
            "available": True,
            "configured": True,
            "workspaceId": "136502",
            "profileId": "dedicated",
            "workspaces": [],
            "profiles": [],
        }
        self.app["roxy_registration_store"] = store

        response = await self.client.post(
            "/api/roxy-registration/config",
            json={"workspaceId": "136502", "profileId": "dedicated"},
            headers={"X-Local-Token": self.app["local_token"]},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["configured"])
        store.configure.assert_called_once_with(
            api_url=None,
            workspace_id="136502",
            profile_id="dedicated",
        )

    async def test_registration_endpoint_forwards_roxy_engine(self):
        captured = {}

        class Manager:
            def start(self, **options):
                captured.update(options)
                return {"id": "task", "running": True}

            async def close(self):
                return None

        self.app["registration_manager"] = Manager()
        response = await self.client.post(
            "/api/registration/start",
            json={
                "label": "Roxy 注册",
                "provider": "inventory",
                "headless": True,
                "browser_engine": "roxy",
                "concurrency": 5,
                "target_count": 12,
            },
            headers={"X-Local-Token": self.app["local_token"]},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(captured["browser_engine"], "roxy")
        self.assertEqual(captured["concurrency"], 5)
        self.assertEqual(captured["target_count"], 12)
        self.assertTrue(captured["headless"])

    async def test_registration_endpoint_rejects_roxy_concurrency_above_five(self):
        response = await self.client.post(
            "/api/registration/start",
            json={
                "label": "Roxy 注册",
                "provider": "manual",
                "email": "roxy@icloud.com",
                "browser_engine": "roxy",
                "concurrency": 6,
            },
            headers={"X-Local-Token": self.app["local_token"]},
        )
        payload = await response.json()

        self.assertEqual(response.status, 400)
        self.assertIn("1–5", payload["error"])

    async def test_registration_endpoint_rejects_roxy_target_above_one_hundred(self):
        response = await self.client.post(
            "/api/registration/start",
            json={
                "label": "Roxy 目标注册",
                "provider": "inventory",
                "browser_engine": "roxy",
                "concurrency": 5,
                "target_count": 101,
            },
            headers={"X-Local-Token": self.app["local_token"]},
        )
        payload = await response.json()

        self.assertEqual(response.status, 400)
        self.assertIn("1–100", payload["error"])


if __name__ == "__main__":
    unittest.main()
