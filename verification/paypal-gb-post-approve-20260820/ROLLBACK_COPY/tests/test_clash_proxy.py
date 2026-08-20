import copy
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

from hidemyemail_generator.clash_proxy import (
    ClashConnection,
    ClashController,
    ClashControllerError,
    build_japanese_fixed_ports,
    is_japanese_node,
    load_fixed_port_proxies,
    render_mihomo_fixed_listeners,
)


class FakeClashApi:
    def __init__(self, delays: dict[str, int | None]) -> None:
        self.delays = delays
        self.selected: list[tuple[str, str]] = []
        self.proxies = {
            "GLOBAL": {
                "type": "Selector",
                "now": "日本一号",
                "all": ["日本一号", "日本二号", "美国一号"],
            },
            "主节点选择": {
                "type": "Selector",
                "now": "自动选择",
                "all": ["日本一号", "日本二号", "日本三号", "美国一号"],
            },
            "自动选择": {
                "type": "URLTest",
                "now": "日本三号",
                "all": ["日本一号", "日本二号", "日本三号", "美国一号"],
            },
            "日本一号": {"type": "Vless"},
            "日本二号": {"type": "Vless"},
            "日本三号": {"type": "Hysteria2"},
            "美国一号": {"type": "Vless"},
        }

    def __call__(self, method, path, payload):
        if method == "GET" and path == "/configs":
            return 200, {"mode": "rule"}
        if method == "GET" and path == "/proxies":
            return 200, {"proxies": copy.deepcopy(self.proxies)}
        if method == "GET" and path.startswith("/proxies/") and "/delay?" in path:
            node = unquote(path.split("/proxies/", 1)[1].split("/delay?", 1)[0])
            delay = self.delays.get(node)
            if delay is None:
                return 504, {"message": "timeout"}
            return 200, {"delay": delay}
        if method == "PUT" and path.startswith("/proxies/"):
            selector = unquote(path.split("/proxies/", 1)[1])
            node = payload["name"]
            self.proxies[selector]["now"] = node
            self.selected.append((selector, node))
            return 204, None
        return 404, {"message": "not found"}


class ClashControllerTests(unittest.TestCase):
    def connection(self) -> ClashConnection:
        return ClashConnection(
            controller_url="http://127.0.0.1:9097",
            proxy_url="http://127.0.0.1:7897",
            available_hint=True,
        )

    def test_identifies_japanese_node_names(self):
        self.assertTrue(is_japanese_node("🇯🇵日本-东京-01"))
        self.assertTrue(is_japanese_node("JP Tokyo Premium"))
        self.assertFalse(is_japanese_node("US Los Angeles"))

    def test_builds_one_stable_local_port_per_japanese_node(self):
        entries = build_japanese_fixed_ports(
            ["日本一号", "日本二号", "日本一号"],
            base_port=19000,
        )

        self.assertEqual(
            [(item.index, item.node, item.port) for item in entries],
            [(1, "日本一号", 19001), (2, "日本二号", 19002)],
        )
        rendered = render_mihomo_fixed_listeners(entries)
        self.assertIn('name: "hme-jp-01"', rendered)
        self.assertIn("port: 19001", rendered)
        self.assertIn('proxy: "日本一号"', rendered)
        self.assertNotIn("7897", rendered)

    def test_loads_verified_fixed_port_map(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "fixed.json"
            path.write_text(
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

            mapping = load_fixed_port_proxies(path)

        self.assertEqual(
            mapping,
            {
                "日本一号": "http://127.0.0.1:19001",
                "日本二号": "http://127.0.0.1:19002",
            },
        )

    def test_skips_slow_and_failed_nodes_then_switches_selector(self):
        api = FakeClashApi(
            {"日本一号": 1200, "日本二号": None, "日本三号": 188}
        )
        controller = ClashController(
            self.connection(), requester=api, exit_detector=lambda _proxy: ("192.0.2.10", "JP")
        )

        result = controller.rotate_japanese_proxy(max_latency_ms=900)

        self.assertEqual(result.selector, "主节点选择")
        self.assertEqual(result.node, "日本三号")
        self.assertEqual(result.latency_ms, 188)
        self.assertEqual(result.skipped, 2)
        self.assertEqual(result.next_cursor, 0)
        self.assertEqual(api.selected, [("主节点选择", "日本三号")])

    def test_rotation_cursor_and_previous_node_select_a_new_node(self):
        api = FakeClashApi({"日本一号": 100, "日本二号": 110, "日本三号": 120})
        controller = ClashController(
            self.connection(), requester=api, exit_detector=lambda _proxy: ("192.0.2.11", "JP")
        )

        result = controller.rotate_japanese_proxy(
            max_latency_ms=900,
            cursor=0,
            previous_node="日本一号",
        )

        self.assertEqual(result.node, "日本二号")
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.next_cursor, 2)

    def test_raises_when_every_japanese_node_exceeds_threshold(self):
        api = FakeClashApi({"日本一号": 901, "日本二号": 1300, "日本三号": None})
        controller = ClashController(
            self.connection(), requester=api, exit_detector=lambda _proxy: ("192.0.2.12", "JP")
        )

        with self.assertRaisesRegex(ClashControllerError, "不超过 900 ms"):
            controller.rotate_japanese_proxy(max_latency_ms=900)

    def test_skips_node_when_public_exit_ip_did_not_change(self):
        api = FakeClashApi({"日本一号": 100, "日本二号": 110, "日本三号": 120})
        exits = iter((("192.0.2.10", "JP"), ("192.0.2.20", "JP")))
        controller = ClashController(
            self.connection(), requester=api, exit_detector=lambda _proxy: next(exits)
        )

        result = controller.rotate_japanese_proxy(
            max_latency_ms=900,
            previous_exit_ip="192.0.2.10",
        )

        self.assertEqual(result.node, "日本二号")
        self.assertEqual(result.exit_ip, "192.0.2.20")
        self.assertEqual(result.exit_country, "JP")
        self.assertEqual(result.skipped, 1)

    def test_fixed_port_rotation_does_not_change_normal_clash_selector(self):
        api = FakeClashApi({"日本一号": 100, "日本二号": 110, "日本三号": 120})
        checked_urls = []
        controller = ClashController(
            self.connection(),
            requester=api,
            exit_detector=lambda proxy: (
                checked_urls.append(proxy) or "192.0.2.21",
                "JP",
            ),
        )

        result = controller.rotate_japanese_proxy(
            max_latency_ms=900,
            fixed_ports={
                "日本一号": "http://127.0.0.1:19001",
                "日本二号": "http://127.0.0.1:19002",
                "日本三号": "http://127.0.0.1:19003",
            },
        )

        self.assertEqual(result.node, "日本一号")
        self.assertEqual(result.proxy_url, "http://127.0.0.1:19001")
        self.assertEqual(checked_urls, ["http://127.0.0.1:19001"])
        self.assertEqual(api.selected, [])
        self.assertEqual(api.proxies["主节点选择"]["now"], "自动选择")


if __name__ == "__main__":
    unittest.main()
