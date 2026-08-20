from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote, urlsplit


DEFAULT_CLASH_CONTROLLER = "http://127.0.0.1:9097"
DEFAULT_CLASH_PROXY = "http://127.0.0.1:7897"
DEFAULT_DELAY_TEST_URL = "https://www.gstatic.com/generate_204"
DEFAULT_MAX_LATENCY_MS = 900
DEFAULT_JP_FIXED_PORT_BASE = 19000
JP_FIXED_PORT_MAP_NAME = "clash-jp-fixed-ports.json"
CLASH_GROUP_TYPES = {
    "Selector",
    "URLTest",
    "Fallback",
    "LoadBalance",
    "Relay",
    "Compatible",
}
JAPAN_NODE_RE = re.compile(
    r"(?:🇯🇵|日本|东京|東京|大阪|\b(?:jp|japan|tokyo|osaka)\b)",
    re.IGNORECASE,
)


class ClashControllerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClashConnection:
    controller_url: str = ""
    secret: str = ""
    proxy_url: str = ""
    pipe_path: str = ""
    prefer_pipe: bool = False
    available_hint: bool = False

    @property
    def configured(self) -> bool:
        return bool(
            self.available_hint
            and self.proxy_url
            and (self.controller_url or self.pipe_path)
        )


@dataclass(frozen=True)
class ClashRotationResult:
    proxy_url: str
    selector: str
    node: str
    latency_ms: int
    next_cursor: int
    skipped: int
    candidate_count: int
    exit_ip: str
    exit_country: str


@dataclass(frozen=True)
class ClashFixedPort:
    index: int
    node: str
    port: int
    proxy_url: str


def is_japanese_node(name: str) -> bool:
    return bool(JAPAN_NODE_RE.search(str(name or "")))


def build_japanese_fixed_ports(
    nodes: Sequence[str],
    *,
    base_port: int = DEFAULT_JP_FIXED_PORT_BASE,
    host: str = "127.0.0.1",
) -> list[ClashFixedPort]:
    """Assign one stable local mixed-proxy port to every Japanese node."""

    first_port = int(base_port) + 1
    unique_nodes = list(dict.fromkeys(str(node or "").strip() for node in nodes))
    unique_nodes = [node for node in unique_nodes if node]
    if not unique_nodes:
        raise ValueError("没有可绑定固定端口的日本节点")
    if first_port < 1024 or first_port + len(unique_nodes) - 1 > 65535:
        raise ValueError("日本固定代理端口范围无效")
    normalized_host = str(host or "").strip()
    if normalized_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("日本固定代理只允许监听本机地址")
    url_host = "[::1]" if normalized_host == "::1" else normalized_host
    return [
        ClashFixedPort(
            index=index,
            node=node,
            port=int(base_port) + index,
            proxy_url=f"http://{url_host}:{int(base_port) + index}",
        )
        for index, node in enumerate(unique_nodes, start=1)
    ]


def render_mihomo_fixed_listeners(entries: Sequence[ClashFixedPort]) -> str:
    """Render Mihomo mixed listeners whose traffic bypasses the normal selector."""

    lines = ["listeners:"]
    for entry in entries:
        lines.extend(
            (
                f'  - name: "hme-jp-{entry.index:02d}"',
                "    type: mixed",
                "    listen: 127.0.0.1",
                f"    port: {entry.port}",
                f"    proxy: {json.dumps(entry.node, ensure_ascii=False)}",
                "    udp: true",
            )
        )
    return "\n".join(lines) + "\n"


def load_fixed_port_proxies(path: Path) -> dict[str, str]:
    """Load the verified node-to-port map used by registration tasks."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    items = payload.get("ports") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return {}
    result: dict[str, str] = {}
    used_ports: set[int] = set()
    for item in items:
        if not isinstance(item, dict):
            return {}
        node = str(item.get("node") or "").strip()
        try:
            port = int(item.get("port"))
        except (TypeError, ValueError):
            return {}
        if not node or not is_japanese_node(node) or not 1024 <= port <= 65535:
            return {}
        if port in used_ports:
            return {}
        used_ports.add(port)
        result[node] = f"http://127.0.0.1:{port}"
    return result


def _yaml_scalar(text: str, key: str) -> str:
    match = re.search(rf"^\s*{re.escape(key)}\s*:\s*(.*?)\s*$", text, re.MULTILINE)
    if not match:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _normalize_controller_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text if "://" in text else f"http://{text}")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Clash Controller 地址无效")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}:{port}"


def _normalize_local_proxy_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text if "://" in text else f"http://{text}")
    if parsed.scheme not in {"http", "socks5"} or not parsed.hostname or not parsed.port:
        raise ValueError("Clash 本地代理地址无效")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}:{parsed.port}"


def _clash_verge_dir() -> Path | None:
    appdata = str(os.environ.get("APPDATA") or "").strip()
    if not appdata:
        return None
    return Path(appdata) / "io.github.clash-verge-rev.clash-verge-rev"


def discover_clash_connection(
    *,
    controller_url: str = "",
    secret: str = "",
    proxy_url: str = "",
    pipe_path: str = "",
) -> ClashConnection:
    explicit_controller = str(controller_url or os.environ.get("HME_CLASH_CONTROLLER_URL") or "").strip()
    explicit_secret = str(secret or os.environ.get("HME_CLASH_SECRET") or "").strip()
    explicit_proxy = str(proxy_url or os.environ.get("HME_CLASH_PROXY_URL") or "").strip()
    explicit_pipe = str(pipe_path or os.environ.get("HME_CLASH_PIPE_PATH") or "").strip()

    config_text = ""
    verge_text = ""
    base_dir = _clash_verge_dir()
    if base_dir is not None:
        try:
            config_text = (base_dir / "config.yaml").read_text(encoding="utf-8")
        except OSError:
            pass
        try:
            verge_text = (base_dir / "verge.yaml").read_text(encoding="utf-8")
        except OSError:
            pass

    discovered_controller = _yaml_scalar(config_text, "external-controller")
    discovered_secret = _yaml_scalar(config_text, "secret")
    discovered_pipe = _yaml_scalar(config_text, "external-controller-pipe")
    mixed_port = _yaml_scalar(config_text, "mixed-port")
    discovered_proxy = f"http://127.0.0.1:{mixed_port}" if mixed_port.isdigit() else ""
    external_enabled = _yaml_scalar(verge_text, "enable_external_controller").casefold()
    prefer_pipe = bool(
        os.name == "nt"
        and (explicit_pipe or discovered_pipe)
        and not explicit_controller
        and external_enabled in {"", "false", "no", "off", "0"}
    )

    final_controller = explicit_controller or discovered_controller or DEFAULT_CLASH_CONTROLLER
    final_proxy = explicit_proxy or discovered_proxy or DEFAULT_CLASH_PROXY
    return ClashConnection(
        controller_url=_normalize_controller_url(final_controller),
        secret=explicit_secret or discovered_secret,
        proxy_url=_normalize_local_proxy_url(final_proxy),
        pipe_path=explicit_pipe or discovered_pipe,
        prefer_pipe=prefer_pipe,
        available_hint=bool(
            (explicit_controller or explicit_pipe or discovered_controller or discovered_pipe)
            and (explicit_proxy or discovered_proxy)
        ),
    )


class _PipeReader:
    def __init__(self, stream) -> None:
        self.stream = stream
        self.buffer = bytearray()

    def _fill(self, length: int) -> None:
        while len(self.buffer) < length:
            chunk = self.stream.read(max(4096, length - len(self.buffer)))
            if not chunk:
                raise ClashControllerError("Clash 命名管道响应意外结束")
            self.buffer.extend(chunk)

    def line(self) -> bytes:
        while b"\r\n" not in self.buffer:
            chunk = self.stream.read(4096)
            if not chunk:
                raise ClashControllerError("Clash 命名管道响应头不完整")
            self.buffer.extend(chunk)
        value, _, remainder = self.buffer.partition(b"\r\n")
        self.buffer[:] = remainder
        return bytes(value)

    def exact(self, length: int) -> bytes:
        self._fill(length)
        value = bytes(self.buffer[:length])
        del self.buffer[:length]
        return value


Requester = Callable[[str, str, dict[str, Any] | None], tuple[int, Any]]
ExitDetector = Callable[[str], tuple[str, str]]


def detect_proxy_exit(proxy_url: str, *, timeout_seconds: float = 12) -> tuple[str, str]:
    endpoints = (
        "https://api.country.is/",
        "https://ipinfo.io/json",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    errors: list[str] = []
    for endpoint in endpoints:
        request = urllib.request.Request(
            endpoint,
            headers={"Accept": "application/json", "User-Agent": "hidemyemail-generator/2"},
        )
        try:
            with opener.open(request, timeout=max(1, float(timeout_seconds))) as response:
                data = json.loads(response.read())
        except (OSError, ValueError, json.JSONDecodeError) as error:
            errors.append(str(error))
            continue
        ip = str(data.get("ip") or "").strip()
        country = str(data.get("country") or data.get("country_code") or "").strip().upper()
        if ip and country:
            return ip, country
        errors.append(f"{endpoint} 未返回 IP/国家")
    raise ClashControllerError(
        "无法通过 Clash 检测公网出口：" + "；".join(errors[-2:])
    )


class ClashController:
    def __init__(
        self,
        connection: ClashConnection,
        *,
        timeout_seconds: float = 5,
        requester: Requester | None = None,
        exit_detector: ExitDetector | None = None,
    ) -> None:
        self.connection = connection
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self.requester = requester
        self.exit_detector = exit_detector or detect_proxy_exit

    def _pipe_request(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> tuple[int, Any]:
        body = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else b""
        )
        headers = [
            f"{method} {path} HTTP/1.1",
            "Host: localhost",
            "Connection: close",
        ]
        if self.connection.secret:
            headers.append(f"Authorization: Bearer {self.connection.secret}")
        if body:
            headers.extend(
                ["Content-Type: application/json", f"Content-Length: {len(body)}"]
            )
        request = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + body
        try:
            stream = open(self.connection.pipe_path, "r+b", buffering=0)
        except OSError as error:
            raise ClashControllerError(f"Clash 命名管道连接失败：{error}") from error
        with stream:
            stream.write(request)
            reader = _PipeReader(stream)
            status_line = reader.line().decode("ascii", errors="replace")
            match = re.match(r"HTTP/\d(?:\.\d)?\s+(\d+)", status_line)
            if not match:
                raise ClashControllerError("Clash 命名管道返回了无效 HTTP 状态")
            status = int(match.group(1))
            response_headers: dict[str, str] = {}
            while True:
                line = reader.line()
                if not line:
                    break
                key, separator, value = line.partition(b":")
                if separator:
                    response_headers[key.decode("ascii").casefold()] = value.decode(
                        "ascii", errors="replace"
                    ).strip()
            response_body = bytearray()
            if response_headers.get("transfer-encoding", "").casefold() == "chunked":
                while True:
                    size = int(reader.line().split(b";", 1)[0], 16)
                    if size == 0:
                        while reader.line():
                            pass
                        break
                    response_body.extend(reader.exact(size))
                    if reader.exact(2) != b"\r\n":
                        raise ClashControllerError("Clash 命名管道分块响应无效")
            else:
                length = int(response_headers.get("content-length") or 0)
                if length:
                    response_body.extend(reader.exact(length))
        data: Any = None
        if response_body:
            try:
                data = json.loads(response_body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = response_body.decode("utf-8", errors="replace")
        return status, data

    def _http_request(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> tuple[int, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.connection.secret:
            headers["Authorization"] = f"Bearer {self.connection.secret}"
        request = urllib.request.Request(
            f"{self.connection.controller_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                raw = response.read()
        except urllib.error.HTTPError as error:
            status = int(error.code)
            raw = error.read()
        except OSError as error:
            raise ClashControllerError(f"Clash Controller 连接失败：{error}") from error
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return status, raw.decode("utf-8", errors="replace")

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        if self.requester is not None:
            status, data = self.requester(method, path, payload)
        else:
            transports = []
            if self.connection.prefer_pipe and self.connection.pipe_path:
                transports.append(self._pipe_request)
            if self.connection.controller_url:
                transports.append(self._http_request)
            if self.connection.pipe_path and self._pipe_request not in transports:
                transports.append(self._pipe_request)
            errors: list[str] = []
            for transport in transports:
                try:
                    status, data = transport(method, path, payload)
                    break
                except ClashControllerError as error:
                    errors.append(str(error))
            else:
                detail = "；".join(errors) or "未配置 Controller 或命名管道"
                raise ClashControllerError(f"无法连接 Clash：{detail}")
        if not 200 <= int(status) < 300:
            detail = data.get("message") if isinstance(data, dict) else str(data or "")
            raise ClashControllerError(
                f"Clash API {method} {path.split('?', 1)[0]} 返回 HTTP {status}"
                + (f"：{detail}" if detail else "")
            )
        return data

    def proxies(self) -> dict[str, dict[str, Any]]:
        data = self._request("GET", "/proxies")
        proxies = data.get("proxies") if isinstance(data, dict) else None
        if not isinstance(proxies, dict):
            raise ClashControllerError("Clash 未返回代理节点列表")
        return proxies

    def mode(self) -> str:
        data = self._request("GET", "/configs")
        return str(data.get("mode") or "rule").strip().casefold() if isinstance(data, dict) else "rule"

    def reload_config(self, path: Path, *, force: bool = True) -> None:
        query = "?force=true" if force else ""
        self._request("PUT", f"/configs{query}", {"path": str(Path(path).resolve())})

    def delay(
        self,
        node: str,
        *,
        timeout_ms: int,
        test_url: str = DEFAULT_DELAY_TEST_URL,
    ) -> int:
        path = (
            f"/proxies/{quote(node, safe='')}/delay"
            f"?timeout={int(timeout_ms)}&url={quote(test_url, safe='')}"
        )
        data = self._request("GET", path)
        try:
            delay = int(data.get("delay"))
        except (AttributeError, TypeError, ValueError) as error:
            raise ClashControllerError(f"节点 {node} 未返回有效延迟") from error
        if delay <= 0:
            raise ClashControllerError(f"节点 {node} 延迟检测失败")
        return delay

    def select(self, selector: str, node: str) -> None:
        self._request(
            "PUT", f"/proxies/{quote(selector, safe='')}", {"name": node}
        )

    @staticmethod
    def japanese_candidates(
        proxies: dict[str, dict[str, Any]], selector: str
    ) -> list[str]:
        info = proxies.get(selector) or {}
        choices = info.get("all") if isinstance(info, dict) else None
        if not isinstance(choices, list):
            return []
        return [
            str(name)
            for name in choices
            if is_japanese_node(str(name))
            and isinstance(proxies.get(str(name)), dict)
            and proxies[str(name)].get("type") not in CLASH_GROUP_TYPES
        ]

    @classmethod
    def choose_selector(
        cls,
        proxies: dict[str, dict[str, Any]],
        *,
        mode: str,
        requested: str = "",
    ) -> str:
        requested = str(requested or "").strip()
        if requested:
            if not cls.japanese_candidates(proxies, requested):
                raise ClashControllerError(
                    f"Clash Selector {requested} 中没有可轮询的日本节点"
                )
            return requested
        selector_names = [
            name
            for name, info in proxies.items()
            if isinstance(info, dict)
            and info.get("type") == "Selector"
            and cls.japanese_candidates(proxies, name)
        ]
        if not selector_names:
            raise ClashControllerError("Clash 中没有包含日本节点的 Selector")
        if mode == "global" and "GLOBAL" in selector_names:
            return "GLOBAL"

        def score(name: str) -> tuple[int, int]:
            info = proxies[name]
            current = str(info.get("now") or "")
            current_info = proxies.get(current) or {}
            points = len(cls.japanese_candidates(proxies, name))
            if name == "GLOBAL":
                points -= 1000
            if current_info.get("type") in CLASH_GROUP_TYPES:
                points += 100
            if re.search(r"(?:节点|选择|代理|proxy|select)", name, re.IGNORECASE):
                points += 50
            return points, -selector_names.index(name)

        return max(selector_names, key=score)

    def rotate_japanese_proxy(
        self,
        *,
        selector: str = "",
        max_latency_ms: int = DEFAULT_MAX_LATENCY_MS,
        cursor: int = 0,
        previous_node: str = "",
        previous_exit_ip: str = "",
        test_url: str = DEFAULT_DELAY_TEST_URL,
        fixed_ports: Mapping[str, str] | None = None,
    ) -> ClashRotationResult:
        maximum = max(50, min(10000, int(max_latency_ms)))
        proxies = self.proxies()
        selected_group = self.choose_selector(
            proxies, mode=self.mode(), requested=selector
        )
        candidates = self.japanese_candidates(proxies, selected_group)
        if not candidates:
            raise ClashControllerError("Clash 中没有可用的日本节点")
        start = int(cursor) % len(candidates)
        skipped = 0
        failures: list[str] = []
        for offset in range(len(candidates)):
            index = (start + offset) % len(candidates)
            node = candidates[index]
            if len(candidates) > 1 and node == previous_node:
                skipped += 1
                continue
            try:
                latency = self.delay(
                    node,
                    timeout_ms=maximum,
                    test_url=test_url,
                )
            except ClashControllerError:
                skipped += 1
                failures.append(f"{node}=timeout")
                continue
            if latency > maximum:
                skipped += 1
                failures.append(f"{node}={latency}ms")
                continue
            proxy_url = self.connection.proxy_url
            if fixed_ports is not None:
                proxy_url = str(fixed_ports.get(node) or "").strip()
                if not proxy_url:
                    skipped += 1
                    failures.append(f"{node}=fixed-port-missing")
                    continue
            else:
                self.select(selected_group, node)
                refreshed = self.proxies()
                actual = str((refreshed.get(selected_group) or {}).get("now") or "")
                if actual != node:
                    raise ClashControllerError(
                        f"Clash Selector 切换未生效：期望 {node}，实际 {actual or '未知'}"
                    )
            try:
                exit_ip, exit_country = self.exit_detector(proxy_url)
            except ClashControllerError:
                skipped += 1
                failures.append(f"{node}=exit-check-failed")
                continue
            exit_country = str(exit_country or "").strip().upper()
            if exit_country != "JP":
                skipped += 1
                failures.append(f"{node}=country-{exit_country or 'unknown'}")
                continue
            if previous_exit_ip and exit_ip == previous_exit_ip:
                skipped += 1
                failures.append(f"{node}=same-exit-ip")
                continue
            return ClashRotationResult(
                proxy_url=proxy_url,
                selector=selected_group,
                node=node,
                latency_ms=latency,
                next_cursor=(index + 1) % len(candidates),
                skipped=skipped,
                candidate_count=len(candidates),
                exit_ip=exit_ip,
                exit_country=exit_country,
            )
        detail = "，".join(failures[-5:])
        raise ClashControllerError(
            f"没有延迟不超过 {maximum} ms 的日本节点"
            + (f"（最近检测：{detail}）" if detail else "")
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="轮询 Clash 日本节点并固定当前出口")
    parser.add_argument("--selector", default="", help="Clash Selector 名称，留空自动检测")
    parser.add_argument("--max-latency", type=int, default=DEFAULT_MAX_LATENCY_MS)
    parser.add_argument("--cursor", type=int)
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path("output") / "clash-jp-rotation.json",
        help="保存轮询游标和上次公网 IP",
    )
    args = parser.parse_args(argv)
    saved: dict[str, Any] = {}
    try:
        saved = json.loads(args.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    connection = discover_clash_connection()
    result = ClashController(connection).rotate_japanese_proxy(
        selector=args.selector,
        max_latency_ms=args.max_latency,
        cursor=args.cursor if args.cursor is not None else int(saved.get("cursor") or 0),
        previous_node=str(saved.get("node") or ""),
        previous_exit_ip=str(saved.get("exitIp") or ""),
    )
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    args.state_file.write_text(
        json.dumps(
            {
                "cursor": result.next_cursor,
                "node": result.node,
                "exitIp": result.exit_ip,
                "exitCountry": result.exit_country,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "selector": result.selector,
                "node": result.node,
                "latencyMs": result.latency_ms,
                "nextCursor": result.next_cursor,
                "skipped": result.skipped,
                "proxyUrl": result.proxy_url,
                "exitCountry": result.exit_country,
                "exitIpFingerprint": hashlib.sha256(
                    result.exit_ip.encode("utf-8")
                ).hexdigest()[:12],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CLASH_GROUP_TYPES",
    "DEFAULT_CLASH_CONTROLLER",
    "DEFAULT_CLASH_PROXY",
    "DEFAULT_DELAY_TEST_URL",
    "DEFAULT_JP_FIXED_PORT_BASE",
    "DEFAULT_MAX_LATENCY_MS",
    "JP_FIXED_PORT_MAP_NAME",
    "ClashConnection",
    "ClashController",
    "ClashControllerError",
    "ClashFixedPort",
    "ClashRotationResult",
    "build_japanese_fixed_ports",
    "discover_clash_connection",
    "detect_proxy_exit",
    "is_japanese_node",
    "load_fixed_port_proxies",
    "render_mihomo_fixed_listeners",
]
