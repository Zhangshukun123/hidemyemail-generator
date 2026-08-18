import json
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.web_ui import build_app_page, build_login_page
from hidemyemail_generator.webapp import create_app
from hidemyemail_generator.browser_tasks import (
    _save_account_record,
    load_account_record,
)


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
        self.assertIn(
            "generate_opll_de_oaics_paypal_link",
            (
                Path(__file__).resolve().parents[1]
                / "src"
                / "hidemyemail_generator"
                / "openai_card_link_bridge.py"
            ).read_text(encoding="utf-8"),
        )
        self.assertIn("cardLinkExtractionModes", page)
        self.assertIn("method, country: config.country", page)
        self.assertIn('config.singleProxy ? ""', page)
        self.assertIn(".field-label[hidden] { display: none; }", page)
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
        self.assertIn('data-settings-section="sms"', page)
        self.assertIn("接码配置", page)
        self.assertIn('id="bindingSmsProvider"', page)
        self.assertIn('id="bindingSmsMaxPrice"', page)
        self.assertIn('id="paypalSmsProvider"', page)
        self.assertIn('id="paypalSmsMaxPrice"', page)
        self.assertIn('data-sms-country="', page)
        self.assertIn("取号国家（多选）", page)
        self.assertIn("允许国家（多选）", page)
        self.assertIn("/api/payment-sms/config", page)
        self.assertIn("window.HmeSmsSettings", page)
        self.assertIn("function routingRenderSignature(routing)", page)
        self.assertIn(
            'settingsPanel.querySelector(".sms-routing-settings")', page
        )
        self.assertIn(
            "nextRoutingSignature === renderedRoutingSignature", page
        )
        self.assertIn(
            'const data = await controller.api.post("/api/payment-sms/config", buildPayload());\n'
            '      renderedRoutingSignature = "";',
            page,
        )
        self.assertIn('data-src="/paypal-pay/"', page)
        self.assertIn("/api/paypal/status", page)
        self.assertIn("renderPayPal", page)
        self.assertIn("提取链接成功", page)
        self.assertIn('data-action="one-click-paypal-payment"', page)
        self.assertIn('this.commands.register("one-click-paypal-payment"', page)
        self.assertIn("/api/account/paypal-payment", page)
        self.assertIn('this.api.post("/api/account/paypal-payment", { email })', page)
        self.assertIn("根据提链真实出口国家自动生成身份资料", page)
        self.assertNotIn('data-payment-country="1"', page)
        self.assertIn("支付地址</span><strong>自动匹配", page)
        self.assertIn("当前账号 Cookie、提链代理与接码平台", page)
        self.assertIn("startQuickFlowPaypalPayment", page)
        self.assertIn("await this.startQuickFlowPaypalPayment(", page)
        self.assertIn('phase: "payment"', page)
        self.assertIn('id="quickFlowPaymentCount"', page)
        self.assertIn('data-quick-stage="payment"', page)
        self.assertIn("自动选择代理、获取接码手机号并启动协议支付", page)
        self.assertIn("请先打开 PP 支付中的“接码配置”", page)
        self.assertIn("单账号提链次数", page)
        self.assertIn("返回 cs_live 时自动继续同模式提链", page)
        self.assertIn("attempt_limit:", page)
        self.assertIn("[methodConfig.createProxyPreference]", page)
        self.assertIn(
            "cardLinkModes: { [method]: configSnapshot.extractionProxyMode }",
            page,
        )
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
        self.assertIn("密码（可选）", page)
        self.assertIn("2FA（可选）", page)
        self.assertNotIn("badge(checkoutLabel, checkoutKind)", page)
        self.assertNotIn('"重新检测 Checkout"', page)
        self.assertNotIn('this.commands.register("retry-checkout-probe"', page)
        self.assertNotIn("/api/account/checkout-probe", page)
        self.assertNotIn("checkoutIdType", page)
        self.assertIn("注册出口", page)
        self.assertIn("item.registrationExitIp", page)
        self.assertIn("联动小铺", page)
        self.assertIn('id="accountLiandongFilter"', page)
        self.assertIn('data-action="upload-liandong-shop"', page)
        self.assertIn("/api/account/liandong-shop-upload", page)
        self.assertIn("item.liandongShopUploaded", page)
        self.assertIn('id="liandongShopMerchantToken"', page)
        self.assertIn("/api/liandong-shop/config", page)
        self.assertIn(
            'item.accountType === "plus" && (!item.hasPassword || item.hasTwoFactor)',
            page,
        )
        self.assertIn("邮箱-----------密码----------2FA码", page)
        self.assertIn("邮箱--------接码地址", page)
        self.assertIn("window.HmeLiandongShop", page)
        self.assertNotIn("item.checkoutExitIp", page)
        self.assertIn('id="registrationProxyMode"', page)
        self.assertIn("Clash 日本轮询", page)
        self.assertIn("/api/registration-proxy/rotate", page)

    def test_account_management_selects_browser_or_protocol_registration(self):
        page = build_app_page()

        self.assertIn("账号管理", page)
        self.assertIn("ACCOUNT MANAGEMENT", page)
        self.assertNotIn("账号设置", page)
        self.assertIn('role="radiogroup" aria-label="注册方式"', page)
        self.assertIn('name="registrationMode" value="headless"', page)
        self.assertIn('name="registrationMode" value="headed"', page)
        self.assertIn('name="registrationMode" value="roxy"', page)
        self.assertIn('name="registrationMode" value="protocol"', page)
        self.assertIn("无头浏览器", page)
        self.assertIn("有头浏览器", page)
        self.assertIn("Mail Auth · 默认仅 Session", page)
        self.assertIn('<option value="protocol">Mail Auth 协议</option>', page)
        self.assertIn("密码与 TOTP 2FA 可选", page)
        otp_step = page.index('data-protocol-stage="email_verification"')
        session_step = page.index('data-protocol-stage="session"')
        password_step = page.index('data-protocol-stage="password"')
        self.assertLess(otp_step, session_step)
        self.assertLess(session_step, password_step)
        self.assertIn(
            '? ["password", "email_verification", "session", "two_factor", "completed"]',
            page,
        )
        self.assertIn('id="protocolSetupCredentials" type="checkbox"', page)
        self.assertIn("同时设置密码与 2FA", page)
        self.assertIn("credentialToggle.disabled = protocolBusy", page)
        self.assertIn("Session/Cookie 仍待获取", page)
        self.assertIn("Session 已保存", page)
        self.assertIn('$("protocolRegistrationPanel").hidden = !protocolMode', page)
        self.assertNotIn('$("taskPanel")', page)
        inventory_panel = page.index(
            '<article class="panel table-panel">', page.index('id="accountsView"')
        )
        protocol_panel = page.index('id="protocolRegistrationPanel"')
        self.assertLess(inventory_panel, protocol_panel)
        self.assertNotIn(
            'insertBefore($("protocolRegistrationPanel")',
            page,
        )
        self.assertIn('browser_engine: mode === "roxy" ? "roxy" : "camoufox"', page)
        self.assertIn('id="roxyWindowMode"', page)
        self.assertIn('id="roxyProfile"', page)
        self.assertIn("/api/roxy-registration/status", page)
        self.assertIn('localStorage.setItem("hme_registration_mode", mode)', page)

    def test_account_plan_filter_uses_saved_account_type(self):
        page = build_app_page()

        self.assertNotIn('<option value="oai">OAI 账号</option>', page)
        self.assertIn('(plan === "all" || item.accountType === plan)', page)
        self.assertNotIn("item.checkoutIsOaics", page)
        self.assertNotIn("item.checkoutIdType", page)

    def test_protocol_registration_uses_provider_entry_without_account_picker(self):
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
        self.assertIn("this.assertProtocolRuntime()", page)
        self.assertIn("provider: protocolProvider", page)
        self.assertIn("const options = this.browserOptions();", page)

    def test_account_workbench_configures_country_registration_proxy(self):
        page = build_app_page()

        self.assertIn('data-route="network"', page)
        self.assertIn('id="networkView" data-view="network"', page)
        self.assertIn('id="registrationProxyPanel"', page)
        self.assertIn('id="cardLinkProxyPanel"', page)
        self.assertIn("代理与线路", page)
        self.assertIn("代理与注册方式互相独立", page)
        self.assertIn("注册代理与提链代理分开配置", page)
        self.assertIn("提链代理独立配置", page)
        self.assertIn('data-action="save-card-link-proxy"', page)
        self.assertIn('data-action="test-card-link-proxy"', page)
        self.assertIn("/api/card-link-proxy/status", page)
        self.assertIn("/api/card-link-proxy/config", page)
        self.assertIn("cardLinkProxy: data", page)
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
        self.assertIn("/api/registration-proxy/test", page)
        self.assertIn("payload.proxyEndpoint = endpoint", page)
        self.assertIn("payload.proxyUsername = username", page)
        self.assertIn("payload.proxyPassword = password", page)
        self.assertIn('country: $("registrationProxyCountry").value || "NL"', page)
        self.assertIn("proxy.dynamicEndpoint", page)
        self.assertIn("matchProxyCountry", page)
        self.assertIn('addEventListener("input", (event) =>', page)
        self.assertIn("void commitProxyCountry(event)", page)
        self.assertIn("未找到唯一国家", page)
        self.assertIn("注册出口已切换为", page)

    def test_card_link_uses_saved_proxy_country_selectors(self):
        page = build_app_page()
        webapp_source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "hidemyemail_generator"
            / "webapp.py"
        ).read_text(encoding="utf-8")
        card_link_handler = webapp_source[
            webapp_source.index("async def create_card_link") : webapp_source.index(
                "async def browser_status"
            )
        ]

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
        self.assertIn('app["registration_proxy_store"]', card_link_handler)
        self.assertIn('app["card_link_proxy_store"]', card_link_handler)

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
        self.assertIn("item?.cardLinkMethod === method", page)
        self.assertIn("cs_live 可重新提链", page)
        self.assertIn("重新提链当前账号", page)
        self.assertIn("force_retry: cardLinkMarkedForMethod(item, method)", page)

    def test_one_click_registration_and_card_link_is_a_separate_pipeline_module(self):
        page = build_app_page()

        self.assertIn('data-route="quick-flow"', page)
        self.assertIn('id="quickFlowView" data-view="quick-flow"', page)
        self.assertIn("一键注册、提链并支付", page)
        self.assertIn("paymentProxyBackupCount", page)
        self.assertIn("个实测出口（", page)
        self.assertIn('id="quickRegistrationMode"', page)
        self.assertIn('id="quickProtocolSetupCredentials" type="checkbox"', page)
        self.assertIn("同时设置密码与 TOTP 2FA", page)
        self.assertIn('id="quickRegistrationProvider"', page)
        self.assertIn('<option value="inventory">iCloud 库存邮箱</option>', page)
        self.assertIn('data-catchall-domain="zkgmail.com">zkgmail.com · QQ 接码</option>', page)
        self.assertIn('data-catchall-domain="cclgmail.com">cclgmail.com · QQ 接码</option>', page)
        self.assertIn('id="catchAllDomainSelect"', page)
        self.assertIn('id="addCatchAllDomain"', page)
        self.assertIn("class CatchAllMailboxPresenter", page)
        self.assertIn('id="quickRegistrationProxyMode"', page)
        self.assertIn('id="quickRegistrationProxyCountry"', page)
        self.assertIn('<option value="direct">本机 IP 直连</option>', page)
        self.assertIn('id="quickRegistrationTargetCount"', page)
        self.assertIn('id="quickCardLinkMethod"', page)
        self.assertIn('id="quickPostPaymentPhoneBinding" type="checkbox"', page)
        self.assertIn("不勾选则确认 Plus 后直接结束任务", page)
        self.assertIn('data-action="start-quick-flow"', page)
        self.assertIn('id="quickFlowRunList"', page)
        self.assertIn('id="quickFlowRunCount"', page)
        self.assertIn('data-action="stop-quick-flow-run"', page)
        self.assertIn('this.commands.register("start-quick-flow"', page)
        self.assertIn('this.commands.register("stop-quick-flow-run"', page)
        self.assertIn('this.commands.register("dismiss-quick-flow-run"', page)
        self.assertIn("quickFlows: []", page)
        self.assertIn("activeQuickFlowId", page)
        self.assertIn('this.schedule("quick-flow:" + runId', page)
        self.assertIn(
            "await this.extractQuickFlowAccounts(runId, succeededEmails)", page
        )
        self.assertIn('this.api.post("/api/registration/start"', page)
        self.assertIn('this.api.post("/api/protocol-registration/start"', page)
        self.assertIn('this.api.post("/api/account/card-link"', page)
        self.assertIn('this.commands.register("retry-quick-card-link"', page)
        self.assertIn('data-action="retry-quick-card-link"', page)
        self.assertIn("this.quickCardLinkPayload(target, true, flow)", page)
        self.assertIn("force_retry: Boolean(forceRetry)", page)
        self.assertIn('id="quickRegistrationProxySummary"', page)
        self.assertIn('id="quickExtractionProxySummary"', page)
        self.assertIn("注册代理：", page)
        self.assertIn("提链代理：", page)
        self.assertIn('id="quickPromotionProxyChoice"', page)
        self.assertIn('id="quickExtractionProxyMode"', page)
        self.assertIn('id="quickExtractionFirstProxyCountry"', page)
        self.assertIn('id="quickExtractionSecondProxyCountry"', page)
        self.assertIn("第一代理出口", page)
        self.assertIn("第二代理出口", page)
        self.assertIn("reuse_registration_proxy: false", page)
        self.assertIn("independent_proxy_pair: !singleProxy", page)
        self.assertIn("use_secondary_proxy: !singleProxy && Boolean(forceRetry)", page)
        self.assertIn("promotion_proxy_choice: singleProxy", page)
        self.assertIn(
            'localStorage.setItem("hme_quick_registration_proxy_mode", mode)', page
        )
        self.assertIn('id="quickFlowSavedConfigState"', page)
        self.assertIn('id="quickFlowSavedConfigSummary"', page)
        self.assertIn("开始一键注册", page)
        self.assertIn("process_id: flow.taskId", page)
        self.assertIn("enabled: Boolean(candidate?.configured)", page)
        self.assertIn("provider: registrationProvider", page)
        self.assertIn('registrationProvider === "zkgmail"', page)
        self.assertIn('localStorage.setItem("hme_quick_registration_provider"', page)
        self.assertIn('class="quick-flow-steps"', page)
        self.assertIn('data-quick-stage="payment"', page)
        self.assertIn("接码平台自动取号", page)
        self.assertIn(".quick-flow-layout", page)

    def test_quick_flow_config_uses_mvp_persistence_and_keeps_start_outside_details(
        self,
    ):
        page = build_app_page()

        details_start = page.index('id="quickFlowConfigDetails"')
        details_end = page.index("</details>", details_start)
        start_button = page.index('id="startQuickFlowButton"')
        run_board = page.index('class="quick-flow-run-board"')
        shell_end = page.index("</section>", run_board)

        self.assertLess(details_end, start_button)
        self.assertLess(start_button, run_board)
        self.assertLess(run_board, shell_end)
        self.assertEqual(page.count('id="startQuickFlowButton"'), 1)
        self.assertIn('class="panel quick-flow-config-shell"', page)
        self.assertIn('aria-label="一键注册启动操作"', page)
        self.assertIn(".quick-flow-config-shell", page)
        self.assertIn(".quick-flow-saved-config", page)

        for presenter_component in (
            "class QuickFlowConfigModel",
            "class QuickFlowConfigView",
            "class QuickFlowConfigPresenter",
            "this.quickFlowConfigPresenter.restore()",
            "this.quickFlowConfigPresenter.bind()",
            "this.quickFlowConfigPresenter.present()",
        ):
            self.assertIn(presenter_component, page)

        for storage_key in (
            "hme_quick_flow_config_v1",
            "hme_quick_flow_config_collapsed",
            "hme_quick_registration_provider",
            "hme_quick_registration_mode",
            "hme_quick_registration_concurrency",
            "hme_quick_registration_target",
            "hme_quick_registration_proxy_mode",
            "hme_quick_registration_proxy_country",
            "hme_quick_card_link_method",
            "hme_quick_extraction_count",
            "hme_quick_extraction_proxy_mode",
            "hme_quick_extraction_first_country",
            "hme_quick_extraction_second_country",
            "hme_quick_promotion_proxy_choice",
            "hme_quick_paypal_us_target_amount",
            "hme_quick_post_payment_phone_binding",
        ):
            self.assertIn(storage_key, page)

        self.assertIn(
            "const configSnapshot = this.quickFlowConfigPresenter.persist()", page
        )
        self.assertIn("configSnapshot,", page)
        self.assertIn("hme_quick_protocol_setup_credentials", page)
        self.assertIn(
            "setup_credentials: configSnapshot.protocolSetupCredentials === true",
            page,
        )
        self.assertIn(
            "postPaymentPhoneBinding: booleanValue(candidate.postPaymentPhoneBinding, false)",
            page,
        )
        self.assertIn(
            "post_payment_phone_binding: postPaymentPhoneBinding", page
        )
        self.assertIn("saveCollapsed(collapsed)", page)
        self.assertIn('this.details.addEventListener("toggle"', page)
        self.assertIn("配置已保存，可直接开始", page)

    def test_quick_flow_monitors_protocol_payment_to_terminal_state(self):
        page = build_app_page()
        app_js = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "hidemyemail_generator"
            / "web_ui"
            / "static"
            / "app.js"
        ).read_text(encoding="utf-8")
        self.assertLessEqual(len(app_js.splitlines()), 5000)

        for component in (
            "class PaymentOutcomeModel",
            "class PayPalPaymentJobModel",
            "class PayPalPaymentMonitorPresenter",
            "this.paypalPaymentMonitorPresenter = new PayPalPaymentMonitorPresenter(this.api)",
            "await this.paypalPaymentMonitorPresenter.monitor(",
            '"?log_offset=0&log_after=" + this.logSequence',
            "await Promise.all(monitorTargets.map",
            "const confirmation = job.account_confirmation",
            "confirmation.plus_confirmed === true",
            "paymentSucceeded: protocolSucceeded",
            "paymentPlusConfirmed: plusConfirmed",
            "paymentConfirmationError: confirmationError",
            "paymentAtRefreshStatus: confirmationStatus",
            'source: "paypal_protocol"',
            'source: "payment_at_refresh"',
            "result.paymentLogs",
            "window.PaymentOutcomeModel.classify(job)",
            "支付成功，但 AT/Plus 后置校验失败",
            "新 AT 已确认 Plus",
            "协议支付失败",
        ):
            self.assertIn(component, page)
        self.assertNotIn("paymentSucceeded: succeeded", page)
        self.assertIn("协议成功", page)
        self.assertNotIn(">查看 PP 支付</button>", page)
        self.assertNotIn(
            '$("quickFlowPaymentCount").textContent = Number(flow.paymentStarted || 0)',
            page,
        )

    def test_one_click_pipeline_skips_only_an_existing_generated_paypal_link(self):
        page = build_app_page()

        self.assertIn("PayPal / 严格 0 · DE / EUR", page)
        self.assertIn("提链代理与注册代理独立", page)
        self.assertIn("首次提链使用第一代理出口", page)
        self.assertIn("重新提链使用第二代理出口", page)
        self.assertIn("更新优惠使用 IP", page)
        self.assertIn("第一提链 IP", page)
        self.assertIn("第二提链 IP", page)
        self.assertIn(
            'const supportedMethods = ["de_oaics_paypal", "paypal_us", "paypal_gb"]',
            page,
        )
        self.assertIn("hasGeneratedCardLinkForMethod(account, method)", page)
        self.assertIn("账号已有同模式 PayPal 链接，已跳过重复创建", page)
        self.assertIn("cs_live 已自动重试", page)
        self.assertIn("提链未完成 · 可重试", page)
        self.assertIn("results.length > 0 && failed === 0", page)
        self.assertIn('id="quickFlowSkippedCount"', page)
        self.assertIn("一键注册、提链并协议支付已启动：使用 ", page)
        self.assertIn("methodConfig.label", page)
        self.assertIn('id="quickExtractionCount"', page)
        self.assertIn("单账号提链次数必须是 1–100 的整数", page)
        self.assertIn("attempt_limit:", page)
        self.assertIn('localStorage.setItem("hme_quick_extraction_count"', page)
        self.assertIn("返回 cs_live 时自动继续同模式提链", page)
        self.assertIn("quickFlowFailureExplanation", page)
        for component in (
            "class QuickFlowQuotaEligibilityModel",
            "class QuickFlowAccountResultView",
            "class QuickFlowAccountResultPresenter",
            "活动更新响应未证明优惠已生效",
            "无免费额度",
            'data-action="remove-no-free-quota-account"',
            'local_only: true',
        ):
            self.assertIn(component, page)
        self.assertIn("本次请求被服务端拦截，不代表账号无法提链", page)
        self.assertIn('class="quick-flow-monitor-details"', page)
        self.assertNotIn("直卡提链日志", page)
        self.assertNotIn('id="quickFlowLog"', page)
        self.assertNotIn('id="quickFlowLogCount"', page)
        self.assertNotIn('class="quick-flow-console-section"', page)
        self.assertIn('id="quickFlowResults"', page)
        self.assertIn("error.logs = Array.isArray(data.logs) ? data.logs : []", page)
        self.assertIn('"[直卡提链] " + message', page)
        self.assertIn("async requestQuickFlowCardLink(runId, payload)", page)
        self.assertIn('"?log_after=" + logSequence', page)
        self.assertIn("progress_id: progressId", page)
        self.assertIn("await new Promise((resolve) => setTimeout(resolve, 500))", page)
        self.assertIn("remainingLiveCounts", page)
        self.assertIn('if (account?.accountType === "plus")', page)
        self.assertIn("账号已是 Plus 套餐，已跳过提链支付", page)
        self.assertIn("账号均已是 Plus，无需提链支付", page)
        self.assertIn("该账号已确认 Plus，无需重新提链或支付", page)
        self.assertNotIn(".quick-flow-log .task-log-row.failed span", page)
        self.assertNotIn(
            ".quick-flow-counters,\n.quick-flow-monitor-details {",
            page,
        )
        quick_flow = page[
            page.index('id="quickFlowView"') : page.index('id="networkView"')
        ]
        self.assertNotIn('value="ph_hosted"', quick_flow)
        self.assertIn('value="de_oaics_paypal"', quick_flow)
        self.assertIn('value="paypal_us"', quick_flow)
        self.assertIn('value="paypal_gb"', quick_flow)
        self.assertIn('id="quickCardLinkTargetAmount"', quick_flow)
        self.assertIn("target_amount: config.targetAmount", page)

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
        self.assertIn(
            '$("registrationProxyEnabled").disabled = !proxy.configured', page
        )
        self.assertNotIn('$("registrationProxyEnabled").disabled = protocolMode', page)
        self.assertNotIn('$("registrationProxyMode").disabled = protocolMode', page)
        self.assertNotIn(
            '$("registrationProxySetupButton").disabled = protocolMode', page
        )
        self.assertNotIn(
            '$("rotateRegistrationProxyButton").disabled = protocolMode', page
        )
        self.assertIn(
            ".registration-proxy-credentials[hidden] { display: none; }", page
        )

    def test_icloud_and_zkgmail_can_start_protocol_registration(self):
        page = build_app_page()
        command_start = page.index('this.commands.register("register-provider"')
        command_end = page.index(
            'this.commands.register("stop-protocol-registration"', command_start
        )
        command = page[command_start:command_end]

        self.assertIn("gmailProviderOption.disabled = protocolMode", page)
        self.assertIn("registrationProviderSelect.disabled = false", page)
        self.assertIn('"开始 iCloud 协议注册"', page)
        self.assertIn('"开始 " + (zkgmail.domain || "cclgmail.com") + " 协议注册"', page)
        self.assertIn('["icloud", "zkgmail"].includes(registrationProvider)', page)
        self.assertIn('registrationProvider === "zkgmail" && !zkgmail.configured', page)
        self.assertIn(
            '$("zkgmailControls").hidden = registrationProvider !== "zkgmail"', page
        )
        self.assertIn('this.store.state.registrationMode === "protocol"', page)
        self.assertIn('this.api.post("/api/protocol-registration/start"', command)
        self.assertIn(
            'const protocolProvider = source === "zkgmail" ? "zkgmail" : "inventory"',
            command,
        )
        self.assertIn("provider: protocolProvider", command)
        self.assertIn("已从库存领取 iCloud 邮箱并启动协议注册", command)
        self.assertIn('"已生成 " + (zkgmail.domain || "cclgmail.com")', command)
        self.assertLess(
            command.index("if (protocolMode)"),
            command.index("const options = this.browserOptions();"),
        )
        self.assertIn("已切换为协议注册，可选择 iCloud 或 QQ 转发自有域名邮箱", page)
        self.assertIn("setup_credentials: setupCredentials", page)

    def test_app_page_uses_frontend_design_patterns(self):
        page = build_app_page()

        self.assertIn("class ApiGateway", page)
        self.assertIn('if (this.token) headers["X-Local-Token"] = this.token', page)
        self.assertIn(
            'response.status === 403 && data.error === "本地请求令牌无效"',
            page,
        )
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

    def test_inline_registration_task_panel_is_removed(self):
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
        ):
            self.assertNotIn(f'id="{element_id}"', page)
        self.assertIn("registration-command-deck", page)
        self.assertIn('id="registrationRuntimeControls"', page)
        self.assertIn('id="registrationRuntimeState"', page)
        self.assertIn('id="registrationRuntimeMessage"', page)
        self.assertIn('id="stopTaskButton"', page)
        self.assertIn("taskStageLabel", page)
        self.assertIn("inferLogContext", page)
        self.assertNotIn('id="taskLogCount"', page)
        self.assertNotIn('id="taskLog"', page)
        self.assertNotIn("浏览器执行轨迹", page)
        self.assertNotIn("task-console-activity", page)
        self.assertNotIn("task-timeline", page)

    def test_terminal_is_the_only_runtime_log_surface(self):
        page = build_app_page()

        for removed_element_id in (
            "runtimeLogButton",
            "runtimeLogTriggerCount",
            "runtimeLogLiveDot",
            "runtimeLogBackdrop",
            "runtimeLogDrawer",
            "runtimeLogCloseButton",
            "runtimeLogState",
            "runtimeLogTask",
            "runtimeLogStartedAt",
            "runtimeLogTotal",
            "runtimeLogSearch",
            "runtimeLogLevel",
            "runtimeLogAutoscroll",
            "runtimeLogList",
            "runtimeLogVisibleCount",
            "runtimeLogUpdatedAt",
            "runtimeLogAnnouncement",
        ):
            self.assertNotIn(f'id="{removed_element_id}"', page)
        self.assertNotIn('aria-controls="runtimeLogDrawer"', page)
        self.assertNotIn('role="dialog" aria-modal="true"', page)
        self.assertNotIn('data-action="open-runtime-log"', page)
        self.assertNotIn('data-action="close-runtime-log"', page)
        self.assertNotIn('data-action="copy-runtime-logs"', page)
        self.assertNotIn("class RuntimeLogView", page)
        self.assertNotIn("class RuntimeLogResizeView", page)
        self.assertNotIn("class RuntimeLogResizePresenter", page)
        self.assertIn("class TerminalLogView", page)
        self.assertIn("class TerminalLogPresenter", page)
        self.assertIn("this.terminalLogPresenter.present(state)", page)
        self.assertIn('id="workbenchTerminalPanel"', page)
        self.assertIn('id="terminalPreviewTitle">任务日志</button>', page)
        self.assertIn(
            'id="terminalPreviewList" class="terminal-preview-list" role="log"', page
        )
        self.assertIn('data-action="toggle-terminal-preview"', page)
        self.assertIn("this.managerCandidates(", page)
        self.assertIn('"registration", "注册进程"', page)
        self.assertIn('this.candidate("browser", "浏览器任务"', page)
        self.assertIn('"protocol", "Mail Auth 协议注册"', page)
        self.assertIn('this.candidate("verification", "账号验证"', page)
        self.assertIn('this.candidate("pipeline", "注册提链流水线"', page)
        self.assertIn('this.candidate("phone-binding", "手机号绑定"', page)
        self.assertIn('"hme:phone-binding-snapshot"', page)
        self.assertIn("item.originTaskId || item.taskId", page)
        self.assertIn("item.originSeq || item.originSequence", page)
        self.assertIn("function redactTerminalLogText", page)
        self.assertIn("this.redact(item.message)", page)
        self.assertIn("REDACTED_API_KEY", page)
        self.assertIn("running ? currentLogs : historyLogs.length", page)
        self.assertIn("logs.slice(-1200)", page)
        self.assertIn("formatLogTimestamp", page)
        self.assertIn("item.location", page)
        self.assertIn("item.action", page)
        self.assertIn("diagnosticCode: this.redact", page)
        self.assertIn('escape(item.message || "（无消息内容）")', page)
        self.assertIn("cursor: logs.at(-1)?.key", page)
        self.assertNotIn("打开运行日志检查失败上下文后重新注册", page)

    def test_account_workspace_uses_compact_registration_launchpad(self):
        page = build_app_page()

        self.assertIn('data-layout="task-first"', page)
        self.assertIn("registration-launch-shell", page)
        self.assertIn("registration-launch-head", page)
        self.assertIn("account-inline-metrics", page)
        self.assertIn("集中管理账号资产，并以紧凑流程发起注册任务", page)
        self.assertIn('data-density="compact"', page)
        self.assertIn("批量获取全部 Session", page)
        self.assertIn("grid-template-areas:", page)
        self.assertIn('"source network"', page)
        self.assertIn("roxy-control-meta", page)
        self.assertIn("#accountsView:not([hidden])", page)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", page)
        self.assertIn(
            "#accountsView > .table-panel { min-width: 0; grid-column: 1; margin: 0; }",
            page,
        )

        runtime_start = page.index('id="registrationRuntimeControls"')
        runtime_end = page.index('id="registrationCodePanel"')
        self.assertIn('id="fetchAllButton"', page[runtime_start:runtime_end])
        for element_id in (
            "registrationEmailProvider",
            "registerProviderButton",
            "registrationProxyEnabled",
            "registrationProxyCountrySearch",
            "registrationProxySetupButton",
            "roxyWorkspace",
            "roxyProfile",
            "roxyConcurrency",
            "roxyTargetCount",
            "roxyWindowMode",
            "registrationEmail",
            "registerEmailButton",
            "fetchAllButton",
            "registrationRuntimeControls",
            "stopTaskButton",
            "registrationCodePanel",
            "registrationCode",
        ):
            self.assertEqual(page.count(f'id="{element_id}"'), 1, element_id)

    def test_running_registration_keeps_start_next_process_available(self):
        page = build_app_page()

        self.assertIn("registration.canStartNext !== false", page)
        self.assertIn("registration.runningCount || 0", page)
        self.assertIn("启动下一个注册进程（运行中", page)
        self.assertIn("registration.failureRecords || []", page)
        self.assertIn("record.failureReason || record.message", page)
        self.assertIn("record.suggestedAction ||", page)
        self.assertIn("record.reasonCode ||", page)
        self.assertNotIn(
            '$("registerEmailButton").disabled = Boolean(state.registrationTask.running)',
            page,
        )

    def test_roxy_missing_profile_keeps_registration_button_clickable(self):
        page = build_app_page()

        self.assertNotIn(
            "(roxyMode && !state.roxyRegistration?.configured)",
            page,
        )
        self.assertIn('profile.classList.toggle("needs-selection"', page)
        self.assertIn('profile.scrollIntoView({ behavior: "smooth"', page)
        self.assertIn(
            "请先在上方“专用指纹环境”中选择一个 Roxy 环境，再点击注册",
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

    def test_removed_inline_panel_keeps_page_context_in_terminal_logs(self):
        page = build_app_page()

        self.assertNotIn('id="taskCompletedSteps"', page)
        self.assertNotIn('id="taskNextAction"', page)
        self.assertNotIn('id="taskRecognitionMeta"', page)
        self.assertNotIn('id="taskStepLedger"', page)
        self.assertIn("this.inferContext({", page)
        self.assertIn("location: this.redact(item.location)", page)
        self.assertIn("action: this.redact(item.action)", page)
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
        self.assertIn("可选 · iCloud 支持自动取码", page)
        self.assertIn("邮箱地址（可选）", page)
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
        self.assertIn('data-catchall-domain="zkgmail.com">zkgmail.com · QQ 接码</option>', page)
        self.assertIn('data-catchall-domain="cclgmail.com">cclgmail.com · QQ 接码</option>', page)
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
        self.assertIn('source === "zkgmail" ? "zkgmail" : "inventory"', page)
        self.assertIn('["smsbower", "zkgmail"].includes(registration.provider)', page)
        self.assertIn('id="zkgmailStatus"', page)
        self.assertIn('data-action="set-zkgmail-auth"', page)
        self.assertIn("/api/zkgmail/status", page)
        self.assertIn("/api/zkgmail/config", page)

    def test_verification_results_keep_every_account_visible(self):
        page = build_app_page()

        self.assertIn("const taskAccountsByEmail = new Map", page)
        self.assertIn("return [...accountRows, ...taskOnlyRows]", page)
        self.assertIn("请选择一个账号（共 ", page)

    def test_selected_account_refresh_uses_saved_cookie(self):
        page = build_app_page()

        self.assertIn("Cookie 刷新选中账号", page)
        self.assertIn("refresh_with_cookie: true", page)
        self.assertIn("正在使用保存的 Cookie 刷新 Session / AT 并查询实时套餐", page)
        self.assertIn("if (data.task) this.store.patch({ verificationTask: data.task })", page)
        self.assertIn("if (data.task) this.store.patch({ browserTask: data.task })", page)
        self.assertIn("requestSequence !== this.accountsRequestSequence", page)

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
                }

            async def complete_email(
                self, completed_email, success, message, *, record=None
            ):
                self.completed.append((completed_email, success, message, record))

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
        self.assertFalse(manager.options["setup_credentials"])
        self.assertTrue(callable(manager.options["on_account_finished"]))
        await manager.options["on_account_finished"](email, True, "registered")
        self.assertEqual(inventory.completed[0][:3], (email, True, "registered"))
        self.assertIsNotNone(inventory.completed[0][3])

    async def test_protocol_registration_can_generate_one_zkgmail_email(self):
        email = "protocol-zkgmail@zkgmail.com"

        class ZkgmailClientStub:
            def __init__(self):
                self.completed = []
                self.cancelled = []

            async def acquire_email(self, label):
                self.label = label
                return email

            async def complete_email(self, completed_email, success, message):
                self.completed.append((completed_email, success, message))

            async def cancel_email(self, cancelled_email, message):
                self.cancelled.append((cancelled_email, message))

        class ProtocolManagerStub:
            def __init__(self):
                self.options = None

            def start(self, **options):
                self.options = options
                return {"status": "running", "running": True}

            async def close(self):
                return None

        zkgmail = ZkgmailClientStub()
        manager = ProtocolManagerStub()
        self.app["zkgmail_client"] = zkgmail
        self.app["protocol_registration_manager"] = manager

        response = await self.client.post(
            "/api/protocol-registration/start",
            headers={"X-Local-Token": self.app["local_token"]},
            json={"provider": "zkgmail", "concurrency": 1},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["provider"], "zkgmail")
        self.assertEqual(payload["email"], email)
        self.assertTrue(zkgmail.label.startswith("QQ 转发邮箱协议注册"))
        self.assertEqual(manager.options["emails"], [email])
        self.assertFalse(manager.options["setup_credentials"])
        self.assertTrue(callable(manager.options["on_account_finished"]))
        await manager.options["on_account_finished"](email, True, "registered")
        self.assertEqual(zkgmail.completed, [(email, True, "registered")])
        self.assertEqual(zkgmail.cancelled, [])

    async def test_protocol_registration_cancels_zkgmail_if_task_cannot_start(self):
        email = "cancelled-protocol@zkgmail.com"

        class ZkgmailClientStub:
            def __init__(self):
                self.cancelled = []

            async def acquire_email(self, _label):
                return email

            async def cancel_email(self, cancelled_email, message):
                self.cancelled.append((cancelled_email, message))

        class ProtocolManagerStub:
            def start(self, **_options):
                raise RuntimeError("协议任务忙碌")

            async def close(self):
                return None

        zkgmail = ZkgmailClientStub()
        self.app["zkgmail_client"] = zkgmail
        self.app["protocol_registration_manager"] = ProtocolManagerStub()

        response = await self.client.post(
            "/api/protocol-registration/start",
            headers={"X-Local-Token": self.app["local_token"]},
            json={"provider": "zkgmail", "concurrency": 1},
        )
        payload = await response.json()

        self.assertEqual(response.status, 409)
        self.assertEqual(payload["error"], "协议任务忙碌")
        self.assertEqual(zkgmail.cancelled, [(email, "协议任务忙碌")])

    async def test_protocol_registration_code_polls_zkgmail_forward_mailbox(self):
        token = "zkgmail-code-token"
        email = "code-protocol@zkgmail.com"

        class ZkgmailClientStub:
            def __init__(self):
                self.polled = []

            async def poll_next_code(self, polled_email, *, since=""):
                self.polled.append((polled_email, since))
                return "246810"

        class ProtocolManagerStub:
            def valid_code_token(self, candidate):
                return candidate == token

            def token_record(self, candidate):
                return (
                    {"email": email, "since": "2026-08-16T00:00:00+00:00"}
                    if candidate == token
                    else None
                )

            async def close(self):
                return None

        zkgmail = ZkgmailClientStub()
        self.app["zkgmail_client"] = zkgmail
        self.app["protocol_registration_manager"] = ProtocolManagerStub()

        response = await self.client.get(f"/api/protocol-registration/code/{token}")

        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), "246810")
        self.assertEqual(
            zkgmail.polled,
            [(email, "2026-08-16T00:00:00+00:00")],
        )

    async def test_protocol_registration_skips_protocol_ready_account(self):
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
                "two_factor": {
                    "enabled": True,
                    "status": "enabled",
                    "secret": "JBSWY3DPEHPK3PXP",
                },
            },
            password="GeneratedPassword!1",
            password_confirmed=True,
        )

        response = await self.client.post(
            "/api/protocol-registration/start",
            headers={"X-Local-Token": self.app["local_token"]},
            json={"all": True, "concurrency": 1},
        )
        payload = await response.json()

        self.assertEqual(response.status, 409)
        self.assertFalse(payload["ok"])
        self.assertIn("均已完成", payload["error"])

    async def test_protocol_registration_force_rechecks_protocol_ready_account(self):
        email = "password-recheck@icloud.com"
        _save_account_record(
            self.app["db_file"],
            email,
            result={
                "access_token": "header.eyJleHAiOjQxMDI0NDQ4MDB9.signature",
                "session_json": json.dumps(
                    {
                        "accessToken": "header.eyJleHAiOjQxMDI0NDQ4MDB9.signature",
                        "sessionToken": "saved-session-token",
                    }
                ),
                "two_factor": {
                    "enabled": True,
                    "status": "enabled",
                    "secret": "JBSWY3DPEHPK3PXP",
                },
            },
            password="GeneratedPassword!1",
            password_confirmed=True,
        )

        class ProtocolManagerStub:
            def __init__(self):
                self.options = None

            def start(self, **options):
                self.options = options
                return {"status": "running", "running": True}

            async def close(self):
                return None

        manager = ProtocolManagerStub()
        self.app["protocol_registration_manager"] = manager
        response = await self.client.post(
            "/api/protocol-registration/start",
            headers={"X-Local-Token": self.app["local_token"]},
            json={"emails": [email], "concurrency": 1, "force": True},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["started"])
        self.assertEqual(manager.options["emails"], [email])

    async def test_protocol_registration_resumes_unconfirmed_saved_session(self):
        email = "passwordless-session@icloud.com"
        _save_account_record(
            self.app["db_file"],
            email,
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

        class ProtocolManagerStub:
            def __init__(self):
                self.options = None

            def start(self, **options):
                self.options = options
                return {"status": "running", "running": True}

            async def close(self):
                return None

        manager = ProtocolManagerStub()
        self.app["protocol_registration_manager"] = manager
        response = await self.client.post(
            "/api/protocol-registration/start",
            headers={"X-Local-Token": self.app["local_token"]},
            json={"emails": [email], "concurrency": 1, "setup_credentials": True},
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["started"])
        self.assertEqual(manager.options["emails"], [email])

    async def test_protocol_registration_api_defaults_to_session_only(self):
        email = "api-session-only@icloud.com"
        _save_account_record(self.app["db_file"], email)
        captured = []

        async def runner(payload, on_event):
            captured.append(payload)
            on_event({"stage": "session", "message": "Session/Cookie 已获取"})
            return {
                "status": "success",
                "email": payload["email"],
                "access_token": "header.payload.signature",
                "session_json": json.dumps(
                    {
                        "accessToken": "header.payload.signature",
                        "sessionToken": "session",
                    }
                ),
                "storage_state_json": json.dumps({"cookies": [], "origins": []}),
                "session_acquisition_method": "gptfree_mail_auth",
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
        self.assertEqual(final["succeeded"], 1)
        self.assertFalse(captured[0]["setup_credentials"])
        record = load_account_record(self.app["db_file"], email)
        self.assertEqual(record["session"]["sessionToken"], "session")
        self.assertNotIn("password", record)
        self.assertNotIn("two_factor", record)

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
                    {
                        "accessToken": "header.payload.signature",
                        "sessionToken": "session",
                    }
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
            json={"emails": [email], "concurrency": 1, "setup_credentials": True},
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

    async def test_protocol_registration_failure_is_saved_by_monitor(self):
        email = "api-protocol-failed@icloud.com"
        _save_account_record(self.app["db_file"], email)

        async def runner(_payload, _on_event):
            raise RuntimeError("Mail Auth protocol rejected token=private-value")

        manager = self.app["protocol_registration_manager"]
        manager.worker_runner = runner
        manager._runtime_cache = None
        response = await self.client.post(
            "/api/protocol-registration/start",
            headers={"X-Local-Token": self.app["local_token"]},
            json={"emails": [email], "concurrency": 1},
        )
        self.assertEqual(response.status, 200)

        final = await manager.wait()
        self.assertEqual(final["failed"], 1)
        response = await self.client.get(
            "/api/registration/failures?reason=protocol_auth_failed"
        )
        failures = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(failures["total"], 1)
        self.assertEqual(failures["records"][0]["mode"], "protocol")
        self.assertEqual(failures["records"][0]["email"], email)
        self.assertNotIn("private-value", json.dumps(failures, ensure_ascii=False))

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
        self.assertEqual(
            payload["failureRecords"][0]["reasonCode"], "email_verification"
        )
        self.assertEqual(
            payload["failureRecords"][0]["failedStage"], "email_verification"
        )
        self.assertTrue(payload["failureRecords"][0]["suggestedAction"])
        self.assertEqual(payload["failureSummary"]["total"], 1)
        self.assertTrue(Path(payload["failureLogFile"]).is_file())
        self.assertNotIn("password", payload["failureRecords"][0])
        self.assertNotIn("token", payload["failureRecords"][0])
        self.assertNotIn("logs", payload["failureRecords"][0])
        self.assertEqual(payload["failureRecords"][0]["logCount"], 1)

        response = await self.client.get(
            "/api/registration/failures?limit=1&reason=email_verification"
        )
        failures = await response.json()
        self.assertEqual(response.status, 200)
        self.assertTrue(failures["ok"])
        self.assertEqual(failures["total"], 1)
        self.assertEqual(failures["records"][0]["processId"], "failed-process-1")
        self.assertNotIn("password", failures["records"][0]["logs"][0])
        self.assertEqual(failures["summary"]["byReason"], {"email_verification": 1})

    async def test_registration_status_survives_monitor_read_failure(self):
        class BrokenMonitor:
            def snapshot(self, **_options):
                raise OSError("monitor database unavailable")

        self.app["registration_monitor"] = BrokenMonitor()

        response = await self.client.get("/api/registration/status")
        status = await response.json()
        self.assertEqual(response.status, 200)
        self.assertTrue(status["ok"])
        self.assertEqual(status["failureRecords"], [])
        self.assertEqual(status["failureSummary"]["total"], 0)
        self.assertIn("monitor database unavailable", status["failureMonitorError"])

        response = await self.client.get("/api/registration/failures")
        failures = await response.json()
        self.assertEqual(response.status, 503)
        self.assertFalse(failures["ok"])
        self.assertIn("monitor database unavailable", failures["error"])


if __name__ == "__main__":
    unittest.main()
