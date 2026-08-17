import json
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from hidemyemail_generator.web_ui import build_app_page
from tests.test_runtime_log_ui import _runtime_state_payloads


def test_no_free_quota_account_is_labeled_and_removed_from_quick_flow():
    html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
    payloads = _runtime_state_payloads()
    email = "no-quota@zkgmail.com"
    failure = (
        "生成直卡支付链接失败：连续 3/3 次提链失败，最后错误："
        "所有组合均未找到 PayPal BA approve 链；GB+GB: "
        "活动更新响应未证明优惠已生效；"
        "初始金额=1667(total_summary.due), 更新后金额=1667(total_summary.due), "
        "payment_method_types=card,paypal"
    )
    state = {"started": False, "deleted": False, "delete_requests": []}
    payloads.update(
        {
            "/api/registration/status": {
                "ok": True,
                "status": "idle",
                "running": False,
                "canStartNext": True,
                "tasks": [],
                "logs": [],
            },
            "/api/registration-proxy/status": {
                "ok": True,
                "enabled": False,
                "configured": True,
                "mode": "dynamic",
                "country": "NL",
                "countries": [{"code": "NL", "label": "荷兰"}],
                "modes": [{"code": "dynamic", "label": "动态住宅", "configured": True}],
            },
            "/api/card-link-proxy/status": {
                "ok": True,
                "enabled": True,
                "configured": True,
                "mode": "dynamic",
                "country": "DE",
                "countries": [{"code": "DE", "label": "德国"}],
                "modes": [{"code": "dynamic", "label": "动态住宅", "configured": True}],
                "cardLinkModes": {"de_oaics_paypal": "dynamic"},
                "cardLinkCountries": {"deOaicsCreate": "DE", "deOaicsPromotion": "DE"},
            },
            "/api/payment-sms/status": {
                "ok": True,
                "configured": True,
                "provider": "smsbower",
                "label": "SMSBower",
                "timeoutSeconds": 60,
            },
            "/api/paypal/status": {
                "ok": True,
                "available": True,
                "running": True,
                "url": "/paypal-pay/",
            },
        }
    )
    page_errors = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1500, "height": 980})
        context.add_init_script(
            "localStorage.setItem('hme_workspace_auto_refresh', '0');"
            "localStorage.setItem('hme_quick_registration_mode', 'headless');"
        )
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())

        def fulfill(route):
            path = urlparse(route.request.url).path
            method = route.request.method
            status = 200
            if path == "/":
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                return
            if path == "/api/gpt-emails":
                items = [] if not state["started"] or state["deleted"] else [
                    {
                        "email": email,
                        "sessionStatus": "ready",
                        "accountType": "free",
                        "cardLink": "",
                        "cardLinkStatus": "",
                    }
                ]
                body = {"ok": True, "items": items}
            elif path == "/api/card-link-proxy/config":
                body = payloads["/api/card-link-proxy/status"]
            elif path == "/api/registration/start":
                state["started"] = True
                body = {
                    "ok": True,
                    "task": {
                        "id": "registration-no-quota",
                        "processId": "registration-no-quota",
                        "status": "running",
                        "running": True,
                        "message": "注册任务已启动",
                        "emails": [email],
                    },
                }
            elif path == "/api/registration/status" and state["started"]:
                body = {
                    "ok": True,
                    "status": "completed",
                    "running": False,
                    "canStartNext": True,
                    "tasks": [
                        {
                            "id": "registration-no-quota",
                            "processId": "registration-no-quota",
                            "status": "completed",
                            "running": False,
                            "message": "注册完成，Session 已保存",
                            "emails": [email],
                            "total": 1,
                            "completed": 1,
                            "logs": [],
                        }
                    ],
                    "logs": [],
                }
            elif path == "/api/account/card-link" and method == "POST":
                status = 502
                body = {
                    "ok": False,
                    "error": failure,
                    "retryable": True,
                    "attemptCount": 3,
                    "attemptLimit": 3,
                    "attemptsExhausted": True,
                    "logs": [failure],
                }
            elif path == "/api/gpt-email/delete" and method == "POST":
                state["delete_requests"].append(route.request.post_data_json)
                state["deleted"] = True
                body = {"ok": True, "deleted": True, "message": "本地账号凭据已删除"}
            else:
                body = payloads.get(path, {"ok": True})
            route.fulfill(
                status=status,
                content_type="application/json; charset=utf-8",
                body=json.dumps(body, ensure_ascii=False),
            )

        page.route("**/*", fulfill)
        page.goto("http://hme-control-center.test/#quick-flow", wait_until="domcontentloaded")
        page.wait_for_function("!document.getElementById('startQuickFlowButton').disabled")
        page.locator("#startQuickFlowButton").click()
        page.wait_for_function(
            "document.getElementById('quickFlowStatusBadge').textContent === '失败'",
            timeout=15_000,
        )

        card = page.locator(".quick-flow-result.no-free-quota")
        card.wait_for(state="visible", timeout=2_000)
        assert card.locator(".quick-flow-quota-badge").inner_text() == "无免费额度"
        assert "账号没有免费额度" in card.locator("code").inner_text()
        assert card.locator('[data-action="retry-quick-card-link"]').count() == 0
        remove_button = card.locator('[data-action="remove-no-free-quota-account"]')
        assert remove_button.inner_text() == "移除账号"
        assert page.evaluate(
            "window.QuickFlowQuotaEligibilityModel.classify({error: '代理连接超时'}).noFreeQuota"
        ) is False

        remove_button.click()
        page.wait_for_function(
            "document.querySelectorAll('.quick-flow-result.no-free-quota').length === 0"
        )

        assert state["delete_requests"] == [{"email": email, "local_only": True}]
        assert "无免费额度账号已从工作台移除" in page.locator("#quickFlowMessage").inner_text()
        assert page_errors == []
        browser.close()
