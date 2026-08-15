import unittest
from pathlib import Path

from hidemyemail_generator.web_ui import build_app_page


class WorkbenchLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = build_app_page()

    def test_page_uses_vscode_style_workbench_regions(self):
        regions = (
            'class="workbench-titlebar"',
            'class="activity-bar"',
            'class="sidebar"',
            'class="workspace"',
            'class="runtime-log-drawer"',
        )
        for region in regions:
            self.assertIn(region, self.page)

        self.assertLess(
            self.page.index('class="workbench-titlebar"'),
            self.page.index('class="app-shell"'),
        )
        self.assertLess(
            self.page.index('class="activity-bar"'),
            self.page.index('class="sidebar"'),
        )
        self.assertLess(
            self.page.index('class="workspace"'),
            self.page.index('class="workspace-header"'),
        )
        self.assertNotIn('class="editor-tabbar"', self.page)
        self.assertNotIn('class="nav-item editor-tab"', self.page)
        self.assertNotIn('class="workbench-statusbar"', self.page)

    def test_activity_bar_and_sidebar_keep_every_route_available(self):
        routes = (
            "overview",
            "accounts",
            "quick-flow",
            "network",
            "card-links",
            "pp-payment",
            "verification",
            "settings",
        )
        activity = self.page[
            self.page.index('class="activity-bar"') :
            self.page.index('class="sidebar"')
        ]
        sidebar = self.page[
            self.page.index('class="sidebar"') :
            self.page.index('class="workspace"')
        ]

        for route in routes:
            marker = f'data-route="{route}"'
            self.assertIn(marker, activity)
            self.assertIn(marker, sidebar)
        self.assertEqual(activity.count('class="nav-item activity-item"'), 8)
        self.assertEqual(sidebar.count('class="nav-item"'), 8)

    def test_codex_palette_and_dense_dimensions_are_bundled(self):
        expected_tokens = (
            "--workbench-titlebar-height: 35px",
            "--workbench-activitybar-width: 48px",
            "--workbench-statusbar-height: 0px",
            "--canvas: #0d0d0d",
            "--surface: #181818",
            "--blue: #19c37d",
            "grid-template-rows: var(--workbench-titlebar-height)",
        )
        for token in expected_tokens:
            self.assertIn(token, self.page)

        self.assertIn('<meta name="theme-color" content="#0d0d0d">', self.page)
        self.assertIn('resolved === "dark" ? "#0d0d0d" : "#f7f7f5"', self.page)

    def test_runtime_log_is_presented_as_a_right_side_drawer(self):
        self.assertIn("class RuntimeLogView", self.page)
        self.assertIn("class RuntimeLogPresenter", self.page)
        self.assertIn('data-action="open-runtime-log"', self.page)
        self.assertIn('id="sidebarRuntimeLabel"', self.page)
        self.assertNotIn('id="statusBarRuntimeLabel"', self.page)
        self.assertNotIn('$("statusBarRuntime").dataset.status', self.page)
        self.assertIn("--runtime-log-drawer-width: 680px", self.page)
        self.assertIn("width: min(var(--runtime-log-drawer-width, 680px)", self.page)
        self.assertIn("top: var(--workbench-titlebar-height)", self.page)
        self.assertIn("right: 0", self.page)
        self.assertIn("bottom: 0", self.page)
        self.assertIn("left: auto", self.page)
        self.assertIn("border-left: 1px solid var(--border-strong)", self.page)
        self.assertIn("animation: runtime-log-drawer-in 200ms", self.page)

    def test_runtime_log_drawer_has_an_accessible_mvp_resize_control(self):
        expected_markup = (
            'id="runtimeLogResizeHandle"',
            'role="separator"',
            'aria-label="调整运行日志宽度"',
            'aria-orientation="vertical"',
            'aria-controls="runtimeLogDrawer"',
            'aria-valuemin="420"',
            'aria-valuemax="1200"',
            'aria-valuenow="680"',
        )
        for marker in expected_markup:
            self.assertIn(marker, self.page)

        expected_behavior = (
            "class RuntimeLogResizeView",
            "class RuntimeLogResizePresenter",
            'this.storageKey = "hme_runtime_log_width"',
            'resizeHandle.addEventListener("pointerdown"',
            'resizeHandle.addEventListener("pointermove"',
            'resizeHandle.addEventListener("keydown"',
            'resizeHandle.addEventListener("dblclick"',
            "this.runtimeLogResizePresenter.restore()",
        )
        for marker in expected_behavior:
            self.assertIn(marker, self.page)

        expected_styles = (
            ".runtime-log-resize-handle",
            "cursor: ew-resize",
            "touch-action: none",
            "container: runtime-log / inline-size",
            "@container runtime-log (max-width: 459px)",
            "@media (forced-colors: active)",
            "background: Highlight",
        )
        for marker in expected_styles:
            self.assertIn(marker, self.page)

    def test_workbench_stylesheet_respects_file_size_rule(self):
        stylesheet = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "hidemyemail_generator"
            / "web_ui"
            / "static"
            / "workbench.css"
        )
        self.assertTrue(stylesheet.is_file())
        self.assertLessEqual(
            len(stylesheet.read_text(encoding="utf-8").splitlines()), 5000
        )

    def test_navigation_sidebar_collapses_to_a_right_edge_rail(self):
        expected_markup = (
            'data-sidebar-collapsed="false"',
            'id="workbenchSidebar"',
            'id="sidebarCollapseButton"',
            'id="sidebarExpandRail"',
            'data-action="toggle-sidebar"',
            'aria-controls="workbenchSidebar"',
        )
        for marker in expected_markup:
            self.assertIn(marker, self.page)

        sidebar_markup = self.page[
            self.page.index('id="workbenchSidebar"') :
            self.page.index('id="sidebarExpandRail"')
        ]
        self.assertNotIn('class="explorer-title"', sidebar_markup)
        self.assertNotIn("资源管理器", sidebar_markup)
        self.assertLess(
            sidebar_markup.index('class="product-lockup sidebar-brand"'),
            sidebar_markup.index('id="sidebarCollapseButton"'),
        )
        self.assertIn('href="#i-panel-expand"', self.page)

        expected_behavior = (
            "class NavigationSidebarView",
            "class NavigationSidebarPresenter",
            'this.root.dataset.sidebarCollapsed = String(collapsed)',
            "this.sidebar.hidden = collapsed",
            'this.sidebar.toggleAttribute("inert", collapsed)',
            "this.expandRail.hidden = expanded",
            'this.storage.getItem("hme_sidebar_collapsed")',
            'this.storage.setItem("hme_sidebar_collapsed", String(this.collapsed))',
            'this.commands.register("toggle-sidebar"',
        )
        for marker in expected_behavior:
            self.assertIn(marker, self.page)

        expected_styles = (
            'html[data-sidebar-collapsed="true"]',
            "--sidebar-width: 0px",
            ".sidebar-expand-rail::before",
            ".sidebar-expand-rail > svg",
            "left: calc(var(--workbench-activitybar-width) - 1px)",
            "grid-column: 3",
        )
        for marker in expected_styles:
            self.assertIn(marker, self.page)


if __name__ == "__main__":
    unittest.main()
