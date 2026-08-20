"""Vendor the standalone card-link runtime from openai-register-paylink.

This helper copies only the transitive function/class/constant closure required by
the PayPal US/USD card-link mode.  The generated package has no runtime dependency
on the source checkout.
"""

from __future__ import annotations

import argparse
import ast
import shutil
from pathlib import Path


ROOT_SYMBOLS = {
    "detect_proxy_health",
    "generate_opll_paypal_long_link",
    "generate_opll_paypal_us_tr_long_link",
    "opll_is_paypal_success_url",
}

HELPER_MODULES = {
    "browser_identity.py": "_card_link_browser_identity.py",
    "country_profiles.py": "_card_link_country_profiles.py",
    "payment_modes.py": "_card_link_payment_modes.py",
}


def assigned_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return {
        item.id
        for target in targets
        for item in ast.walk(target)
        if isinstance(item, ast.Name)
    }


def loaded_names(node: ast.AST) -> set[str]:
    return {
        item.id
        for item in ast.walk(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
    }


def source_segment_with_decorators(source: str, node: ast.AST) -> str:
    """Return one top-level node without dropping its decorators."""

    start_line = int(getattr(node, "lineno", 1) or 1)
    decorators = getattr(node, "decorator_list", ())
    if decorators:
        start_line = min(
            start_line,
            *(int(getattr(item, "lineno", start_line) or start_line) for item in decorators),
        )
    end_line = int(getattr(node, "end_lineno", start_line) or start_line)
    return "".join(source.splitlines(keepends=True)[start_line - 1 : end_line])


def build_runtime(source_file: Path, revision: str) -> str:
    source = source_file.read_text(encoding="utf-8")
    tree = ast.parse(source)
    definitions: dict[str, ast.AST] = {}
    assignments: dict[str, ast.Assign | ast.AnnAssign] = {}
    imports: dict[str, ast.Import | ast.ImportFrom] = {}

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for name in assigned_names(node):
                assignments[name] = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".")[0]] = node

    missing = ROOT_SYMBOLS - definitions.keys()
    if missing:
        raise RuntimeError(f"source runtime is missing required symbols: {sorted(missing)}")

    selected: set[ast.AST] = set()
    visited: set[str] = set()
    pending = list(ROOT_SYMBOLS)
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        node = definitions.get(name) or assignments.get(name) or imports.get(name)
        if node is None:
            continue
        selected.add(node)
        pending.extend(loaded_names(node))

    rendered: list[str] = []
    for node in sorted(selected, key=lambda item: item.lineno):
        snippet = source_segment_with_decorators(source, node).rstrip()
        if not snippet or snippet == "from __future__ import annotations":
            continue
        if snippet == "import requests":
            snippet = "from curl_cffi import requests"
        elif snippet.startswith("from browser_identity import"):
            snippet = snippet.replace(
                "from browser_identity import", "from ._card_link_browser_identity import", 1
            )
        elif snippet.startswith("from country_profiles import"):
            snippet = snippet.replace(
                "from country_profiles import", "from ._card_link_country_profiles import", 1
            )
        elif snippet.startswith("from payment_modes import"):
            snippet = snippet.replace(
                "from payment_modes import", "from ._card_link_payment_modes import", 1
            )
        rendered.append(snippet)

    header = f'''"""Embedded direct-card payment-link runtime.

Generated from openai-register-paylink/app_backend.py at revision {revision}.
Only the transitive code required by the PayPal US/USD card-link mode is included.
This module is self-contained inside hidemyemail-generator at runtime.
"""

from __future__ import annotations

# The source backend keeps broad shared import groups and a few branch-local
# compatibility variables.  They are intentionally retained in this snapshot.
# ruff: noqa: F401,F841

try:
    from curl_cffi.requests import Session as CurlCffiSession
    from curl_cffi.const import CurlOpt as CurlCffiOpt
except ImportError:
    CurlCffiSession = None
    CurlCffiOpt = None
'''
    return header + "\n\n".join(rendered) + "\n"


def vendor(source_dir: Path, package_dir: Path, revision: str) -> None:
    source_dir = source_dir.resolve()
    package_dir = package_dir.resolve()
    package_dir.mkdir(parents=True, exist_ok=True)
    runtime = build_runtime(source_dir / "app_backend.py", revision)
    (package_dir / "card_link_runtime.py").write_text(runtime, encoding="utf-8")

    for source_name, target_name in HELPER_MODULES.items():
        shutil.copyfile(source_dir / source_name, package_dir / target_name)

    browser_identity = package_dir / "_card_link_browser_identity.py"
    browser_source = browser_identity.read_text(encoding="utf-8").replace(
        "from country_profiles import (",
        "from ._card_link_country_profiles import (",
        1,
    )
    browser_source = browser_source.replace(
        "exit_info: ProxyHealthResult",
        "exit_info: ProxyExitInfo",
    )
    browser_identity.write_text(browser_source, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("package_dir", type=Path)
    parser.add_argument("--revision", default="working-tree")
    args = parser.parse_args()
    vendor(args.source_dir, args.package_dir, args.revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
