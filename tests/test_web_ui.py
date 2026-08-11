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
            "ppPaymentView",
            "verificationView",
            "settingsView",
        ):
            self.assertIn(f'id="{view}"', page)
        for route in (
            "overview",
            "accounts",
            "card-links",
            "pp-payment",
            "verification",
            "settings",
        ):
            self.assertIn(f'data-route="{route}"', page)
        self.assertIn("gpt-link · PH / PHP hosted · 双代理严格 0", page)
        self.assertIn('id="verificationAccountSelect"', page)
        self.assertIn('id="verificationConcurrency"', page)
        self.assertIn('data-action="previous-verification"', page)
        self.assertIn('data-action="next-verification"', page)
        self.assertIn('data-action="verify-selected"', page)
        self.assertIn("Cookie 刷新选中账号", page)
        self.assertIn('id="verificationLog"', page)
        self.assertIn("renderVerificationLogs", page)
        self.assertIn("接口响应和删除原因", page)
        self.assertIn("PP 支付", page)
        self.assertIn('data-src="/paypal-pay/"', page)
        self.assertIn("/api/paypal/status", page)
        self.assertIn("renderPayPal", page)
        self.assertIn('id="registrationProxyMode"', page)
        self.assertIn("Clash 日本轮询", page)
        self.assertIn("/api/registration-proxy/rotate", page)

    def test_app_page_uses_frontend_design_patterns(self):
        page = build_app_page()

        self.assertIn("class ApiGateway", page)
        self.assertIn("class ObservableStore", page)
        self.assertIn("class HashRouter", page)
        self.assertIn("class CommandBus", page)
        self.assertIn("class WorkspaceRenderer", page)
        self.assertIn("class WorkspaceController", page)
        self.assertIn("window.__HME_LOCAL_TOKEN__ = __LOCAL_TOKEN__", page)
        self.assertIn("按需同步（仅接码时连接）", page)

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
            "taskCurrentLocation",
            "taskCurrentStage",
            "taskCurrentAction",
            "taskCurrentAccount",
            "taskAssistance",
            "taskAssistanceBadge",
            "taskAssistanceTitle",
            "taskAssistanceText",
            "taskLogCount",
            "taskLog",
        ):
            self.assertIn(f'id="{element_id}"', page)
        self.assertIn("formatElapsed", page)
        self.assertIn("abbreviateEmail", page)
        self.assertIn("task-log-row", page)
        self.assertIn("task-live-context", page)
        self.assertIn("registration-command-deck", page)
        self.assertIn("task-console-grid", page)
        self.assertIn("task-flow-strip", page)
        self.assertIn("taskStageGroup", page)
        self.assertIn("task-timeline", page)
        self.assertIn("taskStageLabel", page)
        self.assertIn("inferLogContext", page)
        self.assertIn("浏览器执行轨迹", page)
        self.assertIn("data-task-tone", page)

    def test_running_registration_keeps_start_next_process_available(self):
        page = build_app_page()

        self.assertIn("registration.canStartNext !== false", page)
        self.assertIn("registration.runningCount || 0", page)
        self.assertIn("启动下一个注册进程（运行中", page)
        self.assertIn("registration.failureRecords || []", page)
        self.assertIn("失败邮箱已记录，可重新点击注册", page)
        self.assertNotIn(
            '$("registerEmailButton").disabled = Boolean(state.registrationTask.running)',
            page,
        )

    def test_hourly_inventory_generation_is_removed_from_local_workspace(self):
        page = build_app_page()

        self.assertNotIn('id="scheduledGenerationPanel"', page)
        self.assertNotIn('data-action="toggle-scheduled-generation"', page)
        self.assertNotIn("/api/scheduled-generation/status", page)
        self.assertNotIn("/api/scheduled-generation/config", page)
        self.assertNotIn("/api/registration-inventory/status", page)

    def test_registration_accepts_manual_email_and_verification_code(self):
        page = build_app_page()

        self.assertIn('id="registrationEmail"', page)
        self.assertIn('id="registerEmailButton"', page)
        self.assertIn("添加邮箱并注册", page)
        self.assertIn("iCloud 自动扫描收件箱与垃圾邮件", page)
        self.assertIn("其他邮箱在浏览器中手动输入", page)
        self.assertIn('id="registrationCodePanel"', page)
        self.assertIn('id="registrationCode"', page)
        self.assertIn("submit-registration-code", page)
        self.assertIn("/api/registration/code", page)
        self.assertIn("awaiting_verification_code", page)
        self.assertNotIn("registerFromInventoryButton", page)
        self.assertNotIn("registrationInventory", page)
        self.assertIn('id="registrationNetworkMode"', page)
        self.assertIn("本机 IP 直连 · 语言随出口", page)
        self.assertIn("关闭时使用本机公网 IP 直连", page)

    def test_registration_can_buy_smsbower_gmail_and_poll_code_automatically(self):
        page = build_app_page()

        self.assertIn('id="smsbowerStatus"', page)
        self.assertIn('id="smsbowerMaxPrice"', page)
        self.assertIn('id="registrationEmailProvider"', page)
        self.assertIn('<option value="icloud">iCloud 库存邮箱</option>', page)
        self.assertIn('<option value="gmail">Gmail · SMSBower</option>', page)
        self.assertIn('id="registerProviderButton"', page)
        self.assertIn("开始 Gmail 注册", page)
        self.assertIn("开始 iCloud 注册", page)
        self.assertIn("无头浏览器（关闭＝前台窗口）", page)
        self.assertNotIn("options.headless = true", page)
        self.assertIn("本机取码保留 ", page)
        self.assertIn("smsBower.retentionHours || 24", page)
        self.assertIn('data-action="set-smsbower-key"', page)
        self.assertIn('data-action="register-provider"', page)
        self.assertIn("/api/smsbower/status", page)
        self.assertIn("/api/smsbower/config", page)
        self.assertIn('provider: source === "gmail" ? "smsbower" : "inventory"', page)
        self.assertIn('registration.provider === "smsbower"', page)

    def test_verification_results_keep_every_account_visible(self):
        page = build_app_page()

        self.assertIn("const taskAccountsByEmail = new Map", page)
        self.assertIn("return [...accountRows, ...taskOnlyRows]", page)
        self.assertIn("请选择一个账号（共 ", page)

    def test_selected_account_refresh_uses_saved_cookie(self):
        page = build_app_page()

        self.assertIn("Cookie 刷新选中账号", page)
        self.assertIn("refresh_with_cookie: true", page)
        self.assertIn("使用保存的 Cookie 刷新 Session 与账号状态", page)

    def test_account_without_two_factor_exposes_add_action(self):
        page = build_app_page()

        self.assertIn('this.credentialButton("添加 2FA", "enable-2fa"', page)
        self.assertIn('this.commands.register("enable-2fa"', page)
        self.assertIn('this.api.post("/api/account/enable-2fa"', page)

    def test_gmail_delete_warning_describes_local_cleanup(self):
        page = build_app_page()

        self.assertIn('email.toLowerCase().endsWith("@gmail.com")', page)
        self.assertIn("不会删除 Gmail 服务商侧的邮箱", page)
        self.assertIn('return data.message || "邮箱已删除"', page)

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

    async def test_paypal_status_reports_missing_vendored_service(self):
        response = await self.client.get("/api/paypal/status")
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["available"])
        self.assertFalse(payload["running"])
        self.assertEqual(payload["url"], "/paypal-pay/")

    async def test_registration_failure_record_is_persisted_in_status(self):
        manager = self.app["registration_manager"]
        await manager.record_failure(
            {
                "processId": "failed-process-1",
                "status": "failed",
                "provider": "manual",
                "email": "retry@icloud.com",
                "emails": ["retry@icloud.com"],
                "message": "验证码失败",
                "currentStage": "email_verification",
                "currentLocation": "OpenAI 验证码页",
                "currentAction": "等待邮箱验证码",
                "startedAt": "2026-08-11T00:00:00+00:00",
                "finishedAt": "2026-08-11T00:01:00+00:00",
                "recordedAt": "2026-08-11T00:01:01+00:00",
                "logs": [
                    {
                        "at": "2026-08-11T00:01:00+00:00",
                        "message": "失败：验证码失败",
                        "stage": "email_verification",
                        "location": "OpenAI 验证码页",
                        "action": "等待邮箱验证码",
                        "status": "error",
                        "password": "must-not-be-saved",
                    }
                ],
                "password": "must-not-be-saved",
                "token": "must-not-be-saved",
            }
        )

        response = await self.client.get("/api/registration/status")
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["failureRecords"][0]["processId"], "failed-process-1")
        self.assertEqual(payload["failureRecords"][0]["email"], "retry@icloud.com")
        self.assertEqual(payload["failureRecords"][0]["message"], "验证码失败")
        self.assertNotIn("password", payload["failureRecords"][0])
        self.assertNotIn("token", payload["failureRecords"][0])
        self.assertNotIn("password", payload["failureRecords"][0]["logs"][0])


if __name__ == "__main__":
    unittest.main()
