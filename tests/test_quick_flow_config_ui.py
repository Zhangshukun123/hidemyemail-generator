import json
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from hidemyemail_generator.web_ui import build_app_page


def _quick_flow_payloads() -> dict[str, dict]:
    idle_task = {
        "ok": True,
        "id": "",
        "status": "idle",
        "running": False,
        "canStartNext": True,
        "tasks": [],
        "logs": [],
        "runtime": {"available": True, "forceHeadless": False},
    }
    countries = [
        {"code": "DE", "name": "德国"},
        {"code": "GB", "name": "英国"},
        {"code": "JP", "name": "日本"},
        {"code": "US", "name": "美国"},
    ]
    registration_proxy = {
        "ok": True,
        "enabled": False,
        "configured": True,
        "mode": "direct",
        "country": "JP",
        "countries": countries,
        "modes": [
            {"code": "clash", "label": "Clash 日本固定端口轮询", "configured": True},
        ],
    }
    card_link_proxy = {
        "ok": True,
        "enabled": True,
        "configured": True,
        "mode": "dynamic",
        "country": "GB",
        "countries": countries,
        "modes": [
            {"code": "dynamic", "label": "Kookeey 动态住宅", "configured": True},
        ],
        "cardLinkModes": {
            "de_oaics_paypal": "dynamic",
            "paypal_us": "dynamic",
            "paypal_gb": "dynamic",
        },
        "cardLinkCountries": {
            "de": "DE",
            "paypalUsCreate": "US",
            "paypalUsFollowup": "US",
            # A stale saved preference must not override the PayPal GB route.
            "paypalGbCreate": "JP",
        },
    }
    return {
        "/api/gpt-emails": {"ok": True, "items": []},
        "/api/browser/status": idle_task,
        "/api/registration/status": idle_task,
        "/api/protocol-registration/status": idle_task,
        "/api/account-verification/status": idle_task,
        "/api/inbox/status": {"ok": True, "configured": True, "codeCount": 0},
        "/api/registration-proxy/status": registration_proxy,
        "/api/registration-proxy/config": registration_proxy,
        "/api/card-link-proxy/status": card_link_proxy,
        "/api/card-link-proxy/config": card_link_proxy,
        "/api/roxy-registration/status": {
            "ok": True,
            "available": True,
            "configured": True,
            "workspaceId": "workspace-1",
            "profileId": "profile-1",
            "maxConcurrency": 5,
            "workspaces": [{"id": "workspace-1", "name": "Workspace"}],
            "profiles": [{"id": "profile-1", "name": "Roxy 日本环境", "open": False}],
        },
        "/api/smsbower/status": {
            "ok": True,
            "configured": True,
            "service": "dr",
            "domain": "gmail.com",
            "maxPrice": 0.05,
        },
        "/api/payment-sms/status": {
            "ok": True,
            "configured": True,
            "provider": "smsbower",
            "label": "SMSBower",
            "timeoutSeconds": 60,
        },
        "/api/zkgmail/status": {
            "ok": True,
            "configured": True,
            "domain": "cclgmail.com",
            "domains": ["cclgmail.com", "zkgmail.com"],
            "forwardAccount": "35***4@qq.com",
        },
        "/api/paypal/status": {"ok": True, "available": True, "running": False},
    }


def test_quick_flow_restores_saved_config_and_keeps_start_visible_when_collapsed():
    html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
    payloads = _quick_flow_payloads()
    page_errors: list[str] = []
    console_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_init_script(
            "localStorage.setItem('hme_workspace_auto_refresh', '0');"
        )
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )

        def fulfill(route):
            path = urlparse(route.request.url).path
            if path == "/":
                route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=html,
                )
                return
            if path == "/api/zkgmail/config" and route.request.method == "POST":
                requested = route.request.post_data_json.get("domain")
                payloads["/api/zkgmail/status"]["domain"] = requested
                route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=json.dumps(
                        payloads["/api/zkgmail/status"], ensure_ascii=False
                    ),
                )
                return
            route.fulfill(
                status=200,
                content_type="application/json; charset=utf-8",
                body=json.dumps(payloads.get(path, {"ok": True}), ensure_ascii=False),
            )

        page.route("**/*", fulfill)
        page.goto("http://hme-quick-flow.test/#quick-flow", wait_until="domcontentloaded")
        page.wait_for_function(
            "document.title === '一键注册、提链并支付 · 账号工作台' && "
            "document.querySelectorAll('#quickExtractionProxyMode option').length === 1 && "
            "!document.getElementById('startQuickFlowButton').disabled"
        )

        details = page.locator("#quickFlowConfigDetails")
        save_button = page.locator("#saveQuickFlowConfigButton")
        start_button = page.locator("#startQuickFlowButton")
        assert details.get_attribute("open") is not None
        assert save_button.is_visible()
        assert save_button.is_enabled()
        assert save_button.inner_text() == "保存配置"
        assert start_button.is_visible()
        assert start_button.is_enabled()

        page.locator("#quickRegistrationMode").select_option("protocol")
        credential_option = page.locator("#quickProtocolSetupCredentialsLabel")
        assert credential_option.is_visible()
        credential_toggle = page.locator("#quickProtocolSetupCredentials")
        assert credential_toggle.is_checked()
        assert credential_toggle.is_disabled()
        assert page.evaluate(
            "JSON.parse(localStorage.getItem('hme_quick_flow_config_v1')).protocolSetupCredentials"
        ) is True
        assert "密码 + 2FA" in page.locator("#quickFlowSavedConfigSummary").inner_text()

        page.locator("#quickRegistrationMode").select_option("roxy")
        page.locator("#quickRegistrationConcurrency").fill("3")
        page.locator("#quickRegistrationConcurrency").dispatch_event("change")
        page.locator("#quickRegistrationTargetCount").fill("7")
        page.locator("#quickRegistrationTargetCount").dispatch_event("change")
        page.locator("#quickCardLinkMethod").select_option("paypal_gb")
        assert page.locator("#quickExtractionFirstProxyCountry").input_value() == "GB"
        page.locator("#quickExtractionCount").fill("2")
        page.locator("#quickExtractionCount").dispatch_event("change")

        page.locator("#quickRegistrationMode").select_option("headed")
        assert page.locator("#quickRegistrationTargetCount").input_value() == "3"
        page.locator("#quickRegistrationMode").select_option("roxy")
        assert page.locator("#quickRegistrationTargetCount").input_value() == "7"

        snapshot = page.evaluate(
            "JSON.parse(localStorage.getItem('hme_quick_flow_config_v1'))"
        )
        assert snapshot["registrationMode"] == "roxy"
        assert snapshot["concurrency"] == "3"
        assert snapshot["targetCount"] == "7"
        assert snapshot["cardLinkMethod"] == "paypal_gb"
        assert snapshot["extractionFirstCountry"] == "GB"
        assert snapshot["extractionCount"] == "2"
        assert snapshot["savedAt"]
        summary = page.locator("#quickFlowSavedConfigSummary").inner_text()
        assert "Roxy 随机指纹" in summary
        assert "并发 3 / 目标 7" in summary
        assert "PayPal / 英国 · GBP" in summary
        assert "每号 2 次" in summary

        page.evaluate(
            "document.getElementById('quickExtractionCount').value = '6'"
        )
        assert page.evaluate(
            "JSON.parse(localStorage.getItem('hme_quick_flow_config_v1')).extractionCount"
        ) == "2"
        save_button.click()
        page.wait_for_function(
            "document.getElementById('quickFlowSavedConfigState').textContent.startsWith('配置已保存 ·')"
        )
        snapshot = page.evaluate(
            "JSON.parse(localStorage.getItem('hme_quick_flow_config_v1'))"
        )
        assert snapshot["extractionCount"] == "6"
        assert page.evaluate(
            "localStorage.getItem('hme_quick_extraction_count')"
        ) == "6"
        assert "任务配置已保存" in page.locator("#toast").inner_text()
        assert "每号 6 次" in page.locator("#quickFlowSavedConfigSummary").inner_text()

        page.locator("#quickFlowConfigDetails > summary").click()
        page.wait_for_function("!document.getElementById('quickFlowConfigDetails').open")
        assert save_button.is_visible()
        assert save_button.is_enabled()
        assert start_button.is_visible()
        assert start_button.is_enabled()
        assert page.locator("#quickFlowRunList").is_visible()
        assert page.evaluate(
            "localStorage.getItem('hme_quick_flow_config_collapsed')"
        ) == "1"

        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "document.getElementById('quickRegistrationMode').value === 'roxy' && "
            "document.getElementById('quickRegistrationTargetCount').value === '7' && "
            "document.getElementById('quickExtractionCount').value === '6' && "
            "!document.getElementById('quickFlowConfigDetails').open"
        )
        assert page.locator("#quickRegistrationConcurrency").input_value() == "3"
        assert page.locator("#quickCardLinkMethod").input_value() == "paypal_gb"
        assert page.locator("#quickExtractionFirstProxyCountry").input_value() == "GB"
        assert page.locator("#quickExtractionCount").input_value() == "6"
        assert page.locator("#quickProtocolSetupCredentials").is_checked()
        assert page.locator("#quickProtocolSetupCredentials").is_disabled()
        assert page.locator("#catchAllDomainSelect").input_value() == "cclgmail.com"
        catch_all_options = page.locator(
            "#quickRegistrationProvider option[data-catchall-domain]"
        )
        assert catch_all_options.count() == 2
        assert catch_all_options.nth(0).text_content() == "zkgmail.com · QQ 接码"
        assert catch_all_options.nth(1).text_content() == "cclgmail.com · QQ 接码"
        page.evaluate(
            """
            const select = document.getElementById('quickRegistrationProvider');
            const option = [...select.options].find(
              (item) => item.dataset.catchallDomain === 'zkgmail.com'
            );
            option.selected = true;
            select.dispatchEvent(new Event('change', { bubbles: true }));
            """
        )
        page.wait_for_function(
            "document.getElementById('catchAllDomainSelect').value === 'zkgmail.com'"
        )
        assert "zkgmail.com" in page.locator("#zkgmailStatus").inner_text()
        assert save_button.is_visible()
        assert save_button.is_enabled()
        assert start_button.is_visible()
        assert start_button.is_enabled()

        page.set_viewport_size({"width": 390, "height": 844})
        assert save_button.is_visible()
        assert start_button.is_visible()
        assert page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        ) <= 1

        browser.close()

    assert page_errors == []
    assert console_errors == []
