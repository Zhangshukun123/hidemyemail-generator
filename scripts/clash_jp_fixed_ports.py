from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Sequence

from hidemyemail_generator.clash_proxy import (
    DEFAULT_JP_FIXED_PORT_BASE,
    JP_FIXED_PORT_MAP_NAME,
    ClashController,
    ClashControllerError,
    ClashFixedPort,
    build_japanese_fixed_ports,
    discover_clash_connection,
    render_mihomo_fixed_listeners,
)


BEGIN_MARKER = "# HME_JP_FIXED_PORTS_BEGIN"
END_MARKER = "# HME_JP_FIXED_PORTS_END"


def _clash_verge_dir() -> Path:
    appdata = str(os.environ.get("APPDATA") or "").strip()
    if not appdata:
        raise RuntimeError("APPDATA 未配置，无法定位 Clash Verge 配置目录")
    path = Path(appdata) / "io.github.clash-verge-rev.clash-verge-rev"
    if not path.is_dir():
        raise RuntimeError(f"未找到 Clash Verge 配置目录：{path}")
    return path


def _current_merge_file(base_dir: Path) -> Path:
    profiles_file = base_dir / "profiles.yaml"
    text = profiles_file.read_text(encoding="utf-8")
    current_match = re.search(r"(?m)^current:\s*([^\s#]+)", text)
    if not current_match:
        raise RuntimeError("profiles.yaml 未记录当前 Clash 订阅")
    current = current_match.group(1)
    blocks = re.split(r"(?m)^- uid:\s*", text)[1:]
    for block in blocks:
        lines = block.splitlines()
        if not lines or lines[0].strip() != current:
            continue
        merge_match = re.search(r"(?m)^\s{4}merge:\s*([^\s#]+)", block)
        if not merge_match:
            raise RuntimeError(f"当前订阅 {current} 没有关联 merge 配置")
        merge_uid = merge_match.group(1)
        file_match = re.search(
            rf"(?ms)^- uid:\s*{re.escape(merge_uid)}\s*$.*?^\s{{2}}file:\s*([^\s#]+)",
            text,
        )
        filename = file_match.group(1) if file_match else f"{merge_uid}.yaml"
        return base_dir / "profiles" / filename
    raise RuntimeError(f"profiles.yaml 未找到当前订阅：{current}")


def _top_level_section(text: str, key: str) -> tuple[int, int] | None:
    lines = text.splitlines(keepends=True)
    start = -1
    position = 0
    offsets: list[int] = []
    for line in lines:
        offsets.append(position)
        position += len(line)
    for index, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}\s*:\s*(?:#.*)?$", line.rstrip("\r\n")):
            start = index
            break
    if start < 0:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index].rstrip("\r\n")
        if re.match(r"^[A-Za-z0-9_.-]+\s*:", line):
            end = index
            break
    return offsets[start], offsets[end] if end < len(offsets) else len(text)


def replace_fixed_listener_block(text: str, rendered: str) -> str:
    managed = f"{BEGIN_MARKER}\n{rendered}{END_MARKER}\n"
    marker_pattern = re.compile(
        rf"(?ms)^\s*{re.escape(BEGIN_MARKER)}\s*$.*?^\s*{re.escape(END_MARKER)}\s*$\r?\n?"
    )
    if marker_pattern.search(text):
        return marker_pattern.sub(managed, text, count=1)
    section = _top_level_section(text, "listeners")
    if section is not None:
        start, end = section
        existing = text[start:end]
        names = re.findall(r"(?m)^\s*-\s+name:\s*[\"']?([^\"'#\s]+)", existing)
        if not names or any(not name.startswith("hme-jp-") for name in names):
            raise RuntimeError("现有 listeners 包含非 HME 入口，已停止以避免覆盖")
        text = text[:start] + text[end:]
    return text.rstrip() + "\n\n" + managed


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".hme-tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _mapping_payload(
    entries: Sequence[ClashFixedPort],
    *,
    selector: str,
    normal_proxy_url: str,
) -> dict:
    return {
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "selector": selector,
        "listen": "127.0.0.1",
        "normalProxyUrl": normal_proxy_url,
        "basePort": entries[0].port - 1,
        "ports": [
            {
                "index": entry.index,
                "label": f"日本 {entry.index:02d}",
                "node": entry.node,
                "port": entry.port,
                "proxyUrl": entry.proxy_url,
            }
            for entry in entries
        ],
    }


def apply_fixed_ports(
    *,
    base_port: int,
    selector: str,
    map_file: Path,
) -> dict:
    connection = discover_clash_connection()
    controller = ClashController(connection)
    proxies = controller.proxies()
    selected_group = controller.choose_selector(
        proxies,
        mode=controller.mode(),
        requested=selector,
    )
    nodes = controller.japanese_candidates(proxies, selected_group)
    entries = build_japanese_fixed_ports(nodes, base_port=base_port)
    rendered = render_mihomo_fixed_listeners(entries)

    verge_dir = _clash_verge_dir()
    generated_config = verge_dir / "clash-verge.yaml"
    merge_file = _current_merge_file(verge_dir)
    original_generated = generated_config.read_text(encoding="utf-8")
    original_merge = merge_file.read_text(encoding="utf-8")
    original_map = map_file.read_bytes() if map_file.exists() else None
    try:
        _write_atomic(
            merge_file,
            replace_fixed_listener_block(original_merge, rendered),
        )
        _write_atomic(
            generated_config,
            replace_fixed_listener_block(original_generated, rendered),
        )
        payload = _mapping_payload(
            entries,
            selector=selected_group,
            normal_proxy_url=connection.proxy_url,
        )
        _write_atomic(
            map_file,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        controller.reload_config(generated_config)
    except Exception:
        _write_atomic(merge_file, original_merge)
        _write_atomic(generated_config, original_generated)
        if original_map is None:
            map_file.unlink(missing_ok=True)
        else:
            map_file.write_bytes(original_map)
        try:
            controller.reload_config(generated_config)
        except Exception:
            pass
        raise
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="为每个 Clash 日本节点创建独立固定 mixed 端口"
    )
    parser.add_argument("--base-port", type=int, default=DEFAULT_JP_FIXED_PORT_BASE)
    parser.add_argument("--selector", default="")
    parser.add_argument(
        "--map-file",
        type=Path,
        default=Path("output") / JP_FIXED_PORT_MAP_NAME,
    )
    parser.add_argument("--reload-only", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.reload_only is not None:
            ClashController(discover_clash_connection()).reload_config(
                args.reload_only
            )
            print(json.dumps({"ok": True, "reloaded": str(args.reload_only)}))
            return 0
        payload = apply_fixed_ports(
            base_port=args.base_port,
            selector=args.selector,
            map_file=args.map_file,
        )
    except (ClashControllerError, OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **payload}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
