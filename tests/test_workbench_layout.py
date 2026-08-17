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
            'class="workspace-route-tabs"',
            'class="control-task-panel"',
            'class="workbench-terminal-panel"',
            'class="workbench-footer"',
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
            "--workbench-titlebar-height: 44px",
            "--workbench-activitybar-width: 58px",
            "--workbench-statusbar-height: 28px",
            "--workbench-tabbar-height: 42px",
            "--workbench-terminal-height: clamp(250px, 31vh, 310px)",
            "--canvas: #0d0d0d",
            "--surface: #181818",
            "--blue: #19c37d",
            "grid-template-rows: minmax(0, 1fr) var(--workbench-terminal-height)",
        )
        for token in expected_tokens:
            self.assertIn(token, self.page)

        self.assertIn('<meta name="theme-color" content="#0d0d0d">', self.page)
        self.assertIn('resolved === "dark" ? "#0d0d0d" : "#f7f7f5"', self.page)

    def test_terminal_is_the_only_runtime_log_surface(self):
        self.assertIn('id="workbenchTerminalPanel"', self.page)
        self.assertIn('id="terminalPreviewList"', self.page)
        self.assertIn('id="terminalPreviewTitle">终端</button>', self.page)
        self.assertIn('data-action="toggle-terminal-preview"', self.page)
        self.assertIn('id="sidebarRuntimeLabel"', self.page)
        self.assertNotIn('id="statusBarRuntimeLabel"', self.page)
        self.assertNotIn('$("statusBarRuntime").dataset.status', self.page)
        for removed_marker in (
            'id="runtimeLogButton"',
            'id="runtimeLogDrawer"',
            'id="runtimeLogBackdrop"',
            'data-action="open-runtime-log"',
            'data-action="close-runtime-log"',
            'data-action="copy-runtime-logs"',
            'class="runtime-log-drawer"',
            "class RuntimeLogView",
            "class RuntimeLogResizeView",
            "class RuntimeLogResizePresenter",
            "--runtime-log-drawer-width",
        ):
            self.assertNotIn(removed_marker, self.page)

    def test_terminal_log_has_an_accessible_collapsible_scroll_surface(self):
        expected_markup = (
            'id="workbenchTerminalPanel"',
            'aria-labelledby="terminalPreviewTitle"',
            'id="terminalSessionSelect"',
            'aria-label="选择日志会话"',
            'id="terminalPreviewList"',
            'role="log"',
            'aria-live="off"',
            'data-action="toggle-terminal-preview"',
            'aria-label="折叠终端日志"',
        )
        for marker in expected_markup:
            self.assertIn(marker, self.page)

        removed_resize_markup = (
            'id="runtimeLogResizeHandle"',
            'role="separator"',
            'aria-label="调整运行日志宽度"',
            'aria-orientation="vertical"',
            'aria-controls="runtimeLogDrawer"',
            'aria-valuemin="420"',
            'aria-valuemax="1200"',
            'aria-valuenow="680"',
        )
        for marker in removed_resize_markup:
            self.assertNotIn(marker, self.page)

        expected_behavior = (
            "class TerminalSessionModel",
            "class TerminalLogView",
            "class TerminalLogPresenter",
            "window.HmeTerminalLog.create",
            '$("terminalSessionSelect").addEventListener("change"',
            "this.terminalLogPresenter.present(state)",
            "setTerminalPreviewCollapsed(",
            'this.commands.register("toggle-terminal-preview"',
            'localStorage.setItem("hme_terminal_preview_collapsed"',
        )
        for marker in expected_behavior:
            self.assertIn(marker, self.page)

        expected_styles = (
            ".workbench-terminal-panel",
            ".terminal-session-picker select",
            ".terminal-preview-list",
            "overflow: auto",
            ".workbench-terminal-panel.is-collapsed .terminal-preview-list",
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
        self.assertIn("资源管理器", sidebar_markup)
        self.assertIn('class="product-lockup sidebar-brand explorer-title"', sidebar_markup)
        self.assertIn('id="sidebarTaskGroupTitle"', sidebar_markup)
        self.assertIn('id="sidebarResourceTitle"', sidebar_markup)
        self.assertLess(
            sidebar_markup.index('class="product-lockup sidebar-brand explorer-title"'),
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

    def test_control_center_uses_real_task_state_and_redacted_log_preview(self):
        expected_markup = (
            'id="controlTaskTableBody"',
            'id="controlTaskSearch"',
            'data-control-task-filter="running"',
            'id="terminalPreviewList"',
            'data-action="toggle-terminal-preview"',
            'id="footerRunningCount"',
            'id="footerApiLabel"',
        )
        for marker in expected_markup:
            self.assertIn(marker, self.page)

        expected_behavior = (
            "function workspaceTaskRows(state)",
            "normalizeWorkspaceTaskStatus",
            "this.renderControlCenter(state)",
            "this.terminalLogPresenter.present(state)",
            "this.redact(item.message)",
            'this.commands.register("toggle-terminal-preview"',
            'this.commands.register("focus-control-task-search"',
            "scheduleWorkspaceRefresh()",
        )
        for marker in expected_behavior:
            self.assertIn(marker, self.page)
        self.assertIn('<div class="terminal-preview-row" data-level="', self.page)


if __name__ == "__main__":
    unittest.main()
