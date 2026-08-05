import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.web_ui import build_app_page, build_login_page
from hidemyemail_generator.webapp import create_app


class StructuredWebUiTests(unittest.TestCase):
    def test_app_page_contains_all_designed_views(self):
        page = build_app_page()

        for view in (
            "overviewView",
            "accountsView",
            "cardLinksView",
            "verificationView",
            "settingsView",
        ):
            self.assertIn(f'id="{view}"', page)
        for route in (
            "overview",
            "accounts",
            "card-links",
            "verification",
            "settings",
        ):
            self.assertIn(f'data-route="{route}"', page)
        self.assertIn("gpt-link · PH / PHP hosted · 双代理严格 0", page)
        self.assertIn('id="verificationAccountSelect"', page)
        self.assertIn('data-action="verify-selected"', page)
        self.assertIn("验证选中账号", page)

    def test_app_page_uses_frontend_design_patterns(self):
        page = build_app_page()

        self.assertIn("class ApiGateway", page)
        self.assertIn("class ObservableStore", page)
        self.assertIn("class HashRouter", page)
        self.assertIn("class CommandBus", page)
        self.assertIn("class WorkspaceRenderer", page)
        self.assertIn("class WorkspaceController", page)
        self.assertIn("window.__HME_LOCAL_TOKEN__ = __LOCAL_TOKEN__", page)

    def test_design_system_is_shared_and_scroll_friendly(self):
        page = build_app_page()

        self.assertIn("--sidebar-width", page)
        self.assertIn("--surface-raised", page)
        self.assertIn("scrollbar-gutter: stable", page)
        self.assertNotIn("backdrop-filter", page)
        self.assertNotIn("cdn.jsdelivr.net", page)

    def test_registration_task_panel_exposes_live_status_details(self):
        page = build_app_page()

        for element_id in (
            "taskPanel",
            "taskStatusIcon",
            "taskStateBadge",
            "browserTaskSummary",
            "browserTaskSuccess",
            "taskElapsed",
            "browserTaskProgressValue",
            "taskLog",
        ):
            self.assertIn(f'id="{element_id}"', page)
        self.assertIn("formatElapsed", page)
        self.assertIn("abbreviateEmail", page)
        self.assertIn("task-log-row", page)
        self.assertIn("data-task-tone", page)

    def test_hourly_inventory_panel_waits_before_generating_without_registration(self):
        page = build_app_page()

        for element_id in (
            "scheduledGenerationPanel",
            "scheduledGenerationBadge",
            "scheduledGenerationCountdown",
            "scheduledGenerationLog",
            "toggleScheduledGenerationButton",
        ):
            self.assertIn(f'id="{element_id}"', page)
        self.assertIn("等待完整 1 小时", page)
        self.assertIn("只入库，不注册", page)
        self.assertIn('data-action="toggle-scheduled-generation"', page)
        self.assertIn("/api/scheduled-generation/status", page)
        self.assertIn("/api/scheduled-generation/config", page)

    def test_one_click_registration_uses_generated_inventory(self):
        page = build_app_page()

        self.assertIn('id="registerFromInventoryButton"', page)
        self.assertIn("从库存注册账号（--）", page)
        self.assertIn('id="registrationInventoryAvailable"', page)
        self.assertIn("可注册库存", page)
        self.assertIn("claiming_inventory", page)
        self.assertIn("正在领取库存", page)
        self.assertIn("已按并发", page)
        self.assertIn("inventoryAvailable", page)

    def test_verification_results_keep_every_account_visible(self):
        page = build_app_page()

        self.assertIn("const taskAccountsByEmail = new Map", page)
        self.assertIn("return [...accountRows, ...taskOnlyRows]", page)
        self.assertIn("请选择一个账号（共 ", page)

    def test_login_page_matches_the_workspace_identity(self):
        page = build_login_page()

        self.assertIn("统一管理邮箱与账号", page)
        self.assertIn("登录工作台", page)
        self.assertIn("本地数据存储", page)
        self.assertIn("SSH 安全隧道", page)
        self.assertIn('id="loginForm"', page)

class StructuredWebUiRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(base_dir=Path(self.temp_dir.name))
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_index_serves_the_structured_workspace(self):
        response = await self.client.get("/")
        page = await response.text()

        self.assertEqual(response.status, 200)
        self.assertIn("class ObservableStore", page)
        self.assertIn("window.__HME_LOCAL_TOKEN__", page)
        self.assertNotIn("__LOCAL_TOKEN__", page)

    async def test_login_route_serves_the_designed_login_page(self):
        await self.client.close()
        self.app = create_app(
            base_dir=Path(self.temp_dir.name), web_password="preview-password"
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

        response = await self.client.get("/login")
        page = await response.text()

        self.assertEqual(response.status, 200)
        self.assertIn("统一管理邮箱与账号", page)
        self.assertIn("登录工作台", page)


if __name__ == "__main__":
    unittest.main()
