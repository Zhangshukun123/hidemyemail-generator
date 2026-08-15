"""Composable HTML page builder for the web workspace.

The builder keeps templates, design tokens, page styles, and controllers in
separate resources.  It is intentionally small: the backend owns transport and
authentication, while the frontend package owns presentation and interaction.
"""

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files


@dataclass(frozen=True)
class PageSpec:
    """Describe one page assembled from reusable frontend resources."""

    template: str
    styles: tuple[str, ...]
    script: str


class PageBuilder:
    """Builder-pattern implementation for self-contained HTML responses."""

    def __init__(self) -> None:
        self._root = files(__package__)

    def _read(self, relative_path: str) -> str:
        return self._root.joinpath(relative_path).read_text(encoding="utf-8")

    def build(self, spec: PageSpec) -> str:
        page = self._read(spec.template)
        css = "\n".join(self._read(path) for path in spec.styles)
        script = self._read(spec.script)
        return page.replace("{{PAGE_STYLES}}", css).replace(
            "{{PAGE_SCRIPT}}", script
        )


@lru_cache(maxsize=1)
def build_app_page() -> str:
    return PageBuilder().build(
        PageSpec(
            template="templates/app.html",
            styles=("static/base.css", "static/app.css", "static/workbench.css"),
            script="static/app.js",
        )
    )


@lru_cache(maxsize=1)
def build_login_page() -> str:
    return PageBuilder().build(
        PageSpec(
            template="templates/login.html",
            styles=("static/base.css", "static/login.css"),
            script="static/login.js",
        )
    )
