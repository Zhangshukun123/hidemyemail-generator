import json
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.web_ui import build_app_page, build_login_page
from hidemyemail_generator.webapp import create_app
from hidemyemail_generator.browser_tasks import _save_account_record, load_account_record


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
        self.assertIn('id="cardLinkMethod"', page)
        self.assertIn('value="de_oaics_paypal">PayPal / 德国 · EUR', page)
        self.assertIn("generate_opll_de_oaics_paypal_link", (
            Path(__file__).resolve().parents[1]
            / "src"
            / "hidemyemail_generator"
            / "openai_card_link_bridge.py"
        ).read_text(encoding="utf-8"))
        self.assertIn("cardLinkExtractionModes", page)
        self.assertIn('method, country: config.country', page)
        self.assertIn('config.singleProxy ? ""', page)
        self.assertIn('.field-label[hidden] { display: none; }', page)
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
        self.assertIn("协议注册", page)
        self.assertIn('id="protocolRegistrationPanel"', page)
        self.assertNotIn('data-route="protocol-registration"', page)
        self.assertIn("Mail Auth 协议注册", page)
        self.assertNotIn("协议注册选中", page)
        self.assertNotIn("协议注册全部", page)
        self.assertNotIn("协议注册账号", page)
        self.assertIn("/api/protocol-registration/start", page)
        self.assertIn("/api/protocol-registration/status", page)
        self.assertIn("renderProtocolRegistration", page)
        self.assertIn("添加密码", page)
        self.assertIn("激活 2FA", page)
        self.assertIn('item.checkoutIdType === "oaics"', page)
        self.assertIn('"OAICS"', page)
        self.assertIn('"重新检测 Checkout"', page)
        self.assertIn('this.commands.register("retry-checkout-probe"', page)
        self.assertIn('/api/account/checkout-probe', page)
        self.assertIn("注册出口", page)
        self.assertIn("item.checkoutExitIp", page)
        self.assertIn('id="registrationProxyMode"', page)
        self.assertIn("Clash 日本轮询", page)
        self.assertIn("/api/registration-proxy/rotate", page)

    def test_account_settings_selects_browser_or_protocol_registration(self):
        page = build_app_page()

        self.assertIn("账号设置", page)
        self.assertIn('role="radiogroup" aria-label="注册方式"', page)
        self.assertIn('name="registrationMode" value="headless"', page)
        self.assertIn('name="registrationMode" value="headed"', page)
        self.assertIn('name="registrationMode" value="roxy"', page)
        self.assertIn('name="registrationMode" value="protocol"', page)
        self.assertIn("无头浏览器", page)
        self.assertIn("有头浏览器", page)
        self.assertIn("Mail Auth · 无浏览器", page)
        self.assertIn('$("protocolRegistrationPanel").hidden = !protocolMode', page)
        self.assertIn('$("taskPanel").hidden = protocolMode', page)
        self.assertIn('browser_engine: mode === "roxy" ? "roxy" : "camoufox"', page)
        self.assertIn('id="roxyWindowMode"', page)
        self.assertIn('id="roxyProfile"', page)
        self.assertIn("/api/roxy-registration/status", page)
        self.assertIn('localStorage.setItem("hme_registration_mode", mode)', page)

    def test_protocol_registration_uses_inventory_entry_without_account_picker(self):
        page = build_app_page()

        self.assertNotIn('id="protocolRuntimeStatus"', page)
        self.assertNotIn("Mail Auth 环境可用", page)
        self.assertNotIn('this.commands.register("refresh-protocol-runtime"', page)
        self.assertNotIn('id="protocolAccountList"', page)
        self.assertNotIn('id="protocolSearch"', page)
        self.assertNotIn('id="protocolStatusFilter"', page)
        self.assertNotIn('id="protocolSelectAll"', page)
        self.assertNotIn('id="startProtocolSelectedButton"', page)
        self.assertNotIn('id="startProtocolAllButton"', page)
        self.assertNotIn('this.commands.register("start-protocol-selected"', page)
        self.assertNotIn('this.commands.register("start-protocol-all"', page)
        self.assertIn('class="panel protocol-task-panel"', page)
        self.assertIn('this.assertProtocolRuntime()', page)
        self.assertIn('provider: "inventory"', page)
        self.assertIn('const options = this.browserOptions();', page)

    def test_account_workbench_configures_country_registration_proxy(self):
        page = build_app_page()

        self.assertIn('data-route="network"', page)
        self.assertIn('id="networkView" data-view="network"', page)
        self.assertIn('id="registrationProxyPanel"', page)
        self.assertIn("代理与线路", page)
        self.assertIn("代理与注册方式互相独立", page)
        self.assertIn("无头浏览器、有头浏览器、Roxy 和协议注册都会使用该出口", page)
        self.assertIn('value="kookeey">Kookeey 动态住宅', page)
        self.assertIn("国家、8 位 Session 和 5m", page)
        self.assertIn('id="registrationProxyUsername"', page)
        self.assertIn('id="registrationProxyPassword"', page)
        self.assertIn('id="registrationProxyEndpoint"', page)
        self.assertIn('id="registrationProxyCountry"', page)
        self.assertIn('id="registrationProxyCountrySearch"', page)
        self.assertIn('list="registrationProxyCountryOptions"', page)
        self.assertIn("注册出口", page)
        self.assertIn('data-action="save-registration-proxy"', page)
        self.assertIn('data-action="test-registration-proxy"', page)
        self.assertIn('/api/registration-proxy/test', page)
        self.assertIn('payload.proxyEndpoint = endpoint', page)
        self.assertIn('payload.proxyUsername = username', page)
        self.assertIn('payload.proxyPassword = password', page)
        self.assertIn('country: $("registrationProxyCountry").value || "NL"', page)
        self.assertIn("proxy.dynamicEndpoint", page)
        self.assertIn("matchProxyCountry", page)
        self.assertIn('addEventListener("input", (event) =>', page)
        self.assertIn("void commitProxyCountry(event)", page)
        self.assertIn("未找到唯一国家", page)
        self.assertIn("注册出口已切换为", page)

    def test_card_link_uses_saved_proxy_country_selectors(self):
        page = build_app_page()

        self.assertIn('id="cardLinkCreateProxyCountry"', page)
        self.assertIn('id="cardLinkPromotionProxyCountry"', page)
        self.assertIn("提链代理国家", page)
        self.assertIn("建单代理国家", page)
        self.assertIn("优惠代理国家", page)
        self.assertIn("cardLinkCountries", page)
        self.assertIn("create_proxy_country", page)
        self.assertIn("promotion_proxy_country", page)
        self.assertNotIn('id="cardLinkCreateProxy" type="password"', page)
        self.assertNotIn('id="cardLinkPromotionProxy" type="password"', page)

    def test_card_link_supports_saved_proxy_mode_and_one_click_extraction(self):
        page = build_app_page()

        self.assertIn('id="cardLinkProxyMode"', page)
        self.assertIn("提链代理模式", page)
        self.assertIn("cardLinkModes", page)
        self.assertIn("proxy_mode", page)
        self.assertIn('id="generateAllCardLinksButton"', page)
        self.assertIn('data-action="generate-all-card-links"', page)
        self.assertIn('this.commands.register("generate-all-card-links"', page)
        self.assertIn("cardLinkMarkedForMethod", page)
        self.assertIn('item?.cardLinkMethod === method', page)
        self.assertIn("cs_live 已标注", page)
        self.assertIn("当前模式不再提链", page)

    def test_proxy_module_is_not_disabled_by_registration_mode(self):
        page = build_app_page()

        self.assertIn(
            '$("registrationSourceBlock").classList.remove("mode-disabled")', page
        )
        self.assertIn(
            '$("registrationManualBlock").classList.toggle("mode-disabled", protocolMode)',
            page,
        )
        self.assertNotIn(
            '["registrationSourceBlock", "registrationNetworkBlock", "registrationManualBlock"]',
            page,
        )
        self.assertIn('$("registrationProxyEnabled").disabled = !proxy.configured', page)
        self.assertNotIn('$("registrationProxyEnabled").disabled = protocolMode', page)
        self.assertNotIn('$("registrationProxyMode").disabled = protocolMode', page)
        self.assertNotIn('$("registrationProxySetupButton").disabled = protocolMode', page)
        self.assertNotIn('$("rotateRegistrationProxyButton").disabled = protocolMode', page)
        self.assertIn('.registration-proxy-credentials[hidden] { display: none; }', page)

    def test_icloud_inventory_can_start_protocol_registration(self):
        page = build_app_page()
        command_start = page.index('this.commands.register("register-provider"')
        command_end = page.index(
            'this.commands.register("stop-protocol-registration"', command_start
        )
        command = page[command_start:command_end]

        self.assertIn('$("registrationEmailProvider").disabled = false', page)
        self.assertIn('"开始 iCloud 协议注册"', page)
        self.assertIn('registrationProvider !== "icloud"', page)
        self.assertIn('this.store.state.registrationMode === "protocol"', page)
        self.assertIn('this.api.post("/api/protocol-registration/start"', command)
        self.assertIn('provider: "inventory"', command)
        self.assertIn("已从库存领取 iCloud 邮箱并启动协议注册", command)
        self.assertLess(
            command.index("if (protocolMode)"),
            command.index("const options = this.browserOptions();"),
        )
        self.assertIn(
            "已切换为协议注册，点击上方按钮即可自动领取 iCloud 邮箱", page
        )

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

    def test_account_workspace_uses_task_first_two_level_layout(self):
        page = build_app_page()

        self.assertIn('data-layout="task-first"', page)
        self.assertIn("registration-launch-shell", page)
        self.assertIn("registration-launch-head", page)
        self.assertIn("account-inline-metrics", page)
        self.assertIn("发起注册任务、跟踪执行状态并维护账号资产", page)
        self.assertIn("批量获取全部 Session", page)
        self.assertIn("grid-template-columns: minmax(390px, 5fr) minmax(620px, 7fr)", page)
        self.assertIn("#accountsView > .table-panel { min-width: 0; grid-column: 1 / -1; }", page)

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

    def test_roxy_missing_profile_keeps_registration_button_clickable(self):
        page = build_app_page()

        self.assertNotIn(
            '(roxyMode && !state.roxyRegistration?.configured)',
            page,
        )
        self.assertIn('profile.classList.toggle("needs-selection"', page)
        self.assertIn('profile.scrollIntoView({ behavior: "smooth"', page)
        self.assertIn(
            '请先在上方“专用指纹环境”中选择一个 Roxy 环境，再点击注册',
            page,
        )

    def test_roxy_registration_exposes_five_window_concurrency(self):
        page = build_app_page()

        self.assertIn('id="roxyConcurrency"', page)
        self.assertIn('max="5"', page)
        self.assertIn('id="roxyTargetCount"', page)
        self.assertIn('max="100"', page)
        self.assertIn('localStorage.getItem("hme_roxy_concurrency") || 5', page)
        self.assertIn('localStorage.getItem("hme_roxy_target_count") || 5', page)
        self.assertIn('concurrency: mode === "roxy" ? roxyConcurrency', page)
        self.assertIn('target_count: mode === "roxy" ? roxyTargetCount', page)
        self.assertIn("同一环境不会并行执行两个账号", page)

    def test_registration_task_exposes_structured_page_recognition(self):
        page = build_app_page()

        self.assertIn('id="taskCompletedSteps"', page)
        self.assertIn('id="taskNextAction"', page)
        self.assertIn('id="taskRecognitionMeta"', page)
        self.assertIn('id="taskStepLedger"', page)
        self.assertIn("task.pageState", page)
        self.assertIn("task.registrationChain", page)
        self.assertIn("pageRecognition?.completedSteps", page)
        self.assertIn("registrationChain.currentCompleted", page)
        self.assertIn("registrationChain.nextCode", page)
        self.assertIn("task-ledger-step", page)
        self.assertIn("当前界面停留 ", page)
        self.assertIn(
            'this.schedule("browser", () => this.loadBrowserTask(), 500)', page
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
        self.assertIn('name="registrationMode" value="headless"', page)
        self.assertIn('name="registrationMode" value="headed"', page)
        self.assertIn('name="registrationMode" value="roxy"', page)
        self.assertIn('name="registrationMode" value="protocol"', page)
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
        self.assertIn('id="username"', page)
        self.assertIn("username: username.value", page)

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

    async def test_username_and_password_login_unlocks_the_workspace(self):
        await self.client.close()
        self.app = create_app(
            base_dir=Path(self.temp_dir.name),
            web_username="inventory-user",
            web_password="strong-test-password",
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

        locked = await self.client.get("/", allow_redirects=False)
        wrong = await self.client.post(
            "/api/login",
            json={"username": "inventory-user", "password": "wrong-password"},
        )
        logged_in = await self.client.post(
            "/api/login",
            json={
                "username": "inventory-user",
                "password": "strong-test-password",
            },
        )
        workspace = await self.client.get("/")

        self.assertEqual(locked.status, 302)
        self.assertEqual(locked.headers["Location"], "/login")
        self.assertEqual(wrong.status, 401)
        self.assertEqual(logged_in.status, 200)
        self.assertEqual(workspace.status, 200)

    async def test_paypal_status_reports_missing_vendored_service(self):
        response = await self.client.get("/api/paypal/status")
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["available"])
        self.assertFalse(payload["running"])
        self.assertEqual(payload["url"], "/paypal-pay/")

    async def test_protocol_registration_status_exposes_mail_auth_runtime(self):
        response = await self.client.get("/api/protocol-registration/status")
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "idle")
        self.assertIn("available", payload["runtime"])
        self.assertIn("projectRoot", payload["runtime"])

    async def test_protocol_registration_runtime_can_be_rechecked(self):
        manager = self.app["protocol_registration_manager"]
        manager._runtime_cache = {"available": True, "stale": True}
        response = await self.client.post(
            "/api/protocol-registration/runtime/refresh",
            headers={"X-Local-Token": self.app["local_token"]},
            json={},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertNotIn("stale", payload["runtime"])
        self.assertIn("available", payload["runtime"])

    async def test_protocol_registration_start_rejects_empty_account_pool(self):
        response = await self.client.post(
            "/api/protocol-registration/start",
            headers={"X-Local-Token": self.app["local_token"]},
            json={"all": True, "concurrency": 1},
        )
        payload = await response.json()

        self.assertEqual(response.status, 409)
        self.assertFalse(payload["ok"])
        self.assertIn("没有可协议注册", payload["error"])

    async def test_protocol_registration_can_claim_one_icloud_inventory_email(self):
        email = "inventory-protocol@icloud.com"

        class InventoryClientStub:
            def __init__(self):
                self.completed = []

            async def acquire_email(self, label):
                self.label = label
                return email

            def leased_record(self, leased_email):
                self.asserted_email = leased_email
                return {
                    "email": leased_email,
                    "address": {
                        "email": leased_email,
                        "state": "unused",
                        "source": "generated",
                    },
                    "account": {"email": leased_email},
                }

            async def complete_email(
                self, completed_email, success, message, *, record=None
            ):
                self.completed.append(
                    (completed_email, success, message, record)
                )

        class ProtocolManagerStub:
            def __init__(self):
                self.options = None

            def start(self, **options):
                self.options = options
                return {"status": "running", "running": True}

            async def close(self):
                return None

        inventory = InventoryClientStub()
        manager = ProtocolManagerStub()
        self.app["inventory_client"] = inventory
        self.app["inventory_initial_sync_complete"] = True
        self.app["protocol_registration_manager"] = manager

        response = await self.client.post(
            "/api/protocol-registration/start",
            headers={"X-Local-Token": self.app["local_token"]},
            json={"provider": "inventory", "concurrency": 1},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["provider"], "inventory")
        self.assertEqual(payload["email"], email)
        self.assertEqual(manager.options["emails"], [email])
        self.assertTrue(callable(manager.options["on_account_finished"]))
        await manager.options["on_account_finished"](email, True, "registered")
        self.assertEqual(inventory.completed[0][:3], (email, True, "registered"))
        self.assertIsNotNone(inventory.completed[0][3])

    async def test_protocol_registration_skips_account_with_saved_session(self):
        _save_account_record(
            self.app["db_file"],
            "registered@icloud.com",
            result={
                "access_token": "header.eyJleHAiOjQxMDI0NDQ4MDB9.signature",
                "session_json": json.dumps(
                    {
                        "accessToken": "header.eyJleHAiOjQxMDI0NDQ4MDB9.signature",
                        "sessionToken": "saved-session-token",
                    }
                ),
            },
            password="GeneratedPassword!1",
            password_confirmed=False,
        )

        response = await self.client.post(
            "/api/protocol-registration/start",
            headers={"X-Local-Token": self.app["local_token"]},
            json={"all": True, "concurrency": 1},
        )
        payload = await response.json()

        self.assertEqual(response.status, 409)
        self.assertFalse(payload["ok"])
        self.assertIn("均已注册", payload["error"])

    async def test_protocol_registration_api_persists_complete_credentials(self):
        email = "api-protocol@icloud.com"
        _save_account_record(self.app["db_file"], email)

        async def runner(payload, on_event):
            on_event({"stage": "two_factor", "message": "TOTP 2FA 已激活"})
            return {
                "status": "success",
                "email": payload["email"],
                "access_token": "header.payload.signature",
                "session_json": json.dumps(
                    {"accessToken": "header.payload.signature", "sessionToken": "session"}
                ),
                "storage_state_json": json.dumps({"cookies": [], "origins": []}),
                "session_acquisition_method": "gptfree_mail_auth",
                "password": "GeneratedPassword!1",
                "two_factor": {
                    "enabled": True,
                    "status": "enabled",
                    "type": "totp",
                    "secret": "JBSWY3DPEHPK3PXP",
                },
            }

        manager = self.app["protocol_registration_manager"]
        manager.worker_runner = runner
        manager._runtime_cache = None
        response = await self.client.post(
            "/api/protocol-registration/start",
            headers={"X-Local-Token": self.app["local_token"]},
            json={"emails": [email], "concurrency": 1},
        )
        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["started"])

        final = await manager.wait()
        self.assertEqual(final["status"], "completed")
        record = load_account_record(self.app["db_file"], email)
        self.assertEqual(record["password"], "GeneratedPassword!1")
        self.assertTrue(record["two_factor"]["enabled"])
        self.assertEqual(record["session_acquisition_method"], "gptfree_mail_auth")

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
