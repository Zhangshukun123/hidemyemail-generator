import json
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from hidemyemail_generator.web_ui import build_app_page
from tests.test_runtime_log_ui import _runtime_state_payloads


def test_payment_outcome_keeps_protocol_success_when_cookie_at_refresh_fails():
    html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
    payloads = _runtime_state_payloads()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        def fulfill(route):
            path = urlparse(route.request.url).path
            if path == "/":
                route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=html,
                )
                return
            route.fulfill(
                status=200,
                content_type="application/json; charset=utf-8",
                body=json.dumps(payloads.get(path, {"ok": True}), ensure_ascii=False),
            )

        page.route("**/*", fulfill)
        page.goto("http://hme-payment-outcome.test/#quick-flow", wait_until="domcontentloaded")
        model = page.evaluate(
            """() => {
              const job = {
                id: "payment-job-refresh-failed",
                status: "completed",
                result: {status: "success", settlement_status: "confirmed"},
                account_confirmation: {
                  status: "refresh_failed",
                  protocol_succeeded: true,
                  payment_succeeded: true,
                  plus_confirmed: false,
                  account_type: "unverified",
                  detail: "协议支付账号未保存原注册代理，无法用 Cookie 刷新新 AT",
                },
              };
              const outcome = window.PaymentOutcomeModel.classify(job);
              const snapshot = new window.PayPalPaymentJobModel({
                paymentJobId: job.id,
              }).apply(job);
              const item = {
                ok: true,
                email: "refresh-failed@icloud.com",
                url: "https://www.paypal.com/agreements/approve?ba_token=test",
                paymentStarted: true,
                ...snapshot.fields,
              };
              const presenter = new window.QuickFlowAccountResultPresenter(
                {}, {state: {}}, {confirmRemoval: () => false},
              );
              return {
                paymentSucceeded: snapshot.fields.paymentSucceeded,
                plusConfirmed: snapshot.fields.paymentPlusConfirmed,
                paymentError: snapshot.fields.paymentError,
                confirmationError: snapshot.fields.paymentConfirmationError,
                terminal: snapshot.terminal,
                rendered: presenter.render(item, {}, {runId: "flow-1", status: "completed"},
                  () => "", () => ""),
              };
            }"""
        )

        assert model["paymentSucceeded"] is True
        assert model["plusConfirmed"] is False
        assert model["paymentError"] == ""
        assert model["terminal"] is True
        assert "无法用 Cookie 刷新新 AT" in model["confirmationError"]
        assert "支付成功 · AT/Plus 后置校验失败" in model["rendered"]
        assert "协议支付失败" not in model["rendered"]
        assert "支付后状态" in model["rendered"]
        browser.close()


def test_quick_flow_waits_for_paypal_success_and_streams_logs_without_navigation():
    html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
    payloads = _runtime_state_payloads()
    email = "payment-monitor@icloud.com"
    state = {"started": False, "job_polls": 0, "payment_payloads": []}
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
                "cardLinkCountries": {
                    "deOaicsCreate": "DE",
                    "deOaicsPromotion": "DE",
                },
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

        def fulfill(route):
            path = urlparse(route.request.url).path
            method = route.request.method
            if path == "/":
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                return
            if path == "/api/gpt-emails":
                items = [] if not state["started"] else [
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
                        "id": "registration-monitor",
                        "processId": "registration-monitor",
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
                            "id": "registration-monitor",
                            "processId": "registration-monitor",
                            "status": "completed",
                            "running": False,
                            "message": "注册完成，Session 已保存",
                            "emails": [email],
                            "total": 1,
                            "completed": 1,
                            "logs": [
                                {
                                    "at": "2026-08-17T12:00:00+00:00",
                                    "message": "注册完成，Session 已保存",
                                    "status": "success",
                                    "sequence": 1,
                                    "taskId": "registration-monitor",
                                }
                            ],
                        }
                    ],
                    "logs": [],
                }
            elif path == "/api/account/card-link":
                body = {
                    "ok": True,
                    "url": "https://www.paypal.com/agreements/approve?ba_token=BA-MONITOR123",
                    "country": "DE",
                    "link_proxy_country": "DE",
                    "cardLinkStatus": "generated",
                    "attemptCount": 1,
                    "logs": ["PayPal 链接已生成"],
                }
            elif path == "/api/account/paypal-payment" and method == "POST":
                state["payment_payloads"].append(route.request.post_data_json)
                body = {
                    "ok": True,
                    "country": "DE",
                    "proxyMode": "dynamic",
                    "proxySource": "card_link",
                    "smsProvider": "smsbower",
                    "smsProviderLabel": "SMSBower",
                    "postPaymentPhoneBinding": False,
                    "job": {"id": "payment-job-12345678", "status": "queued", "stage": "排队中"},
                }
            elif path == "/api/account/paypal-payment/payment-job-12345678":
                state["job_polls"] += 1
                completed = state["job_polls"] >= 2
                plus_confirmed = state["job_polls"] >= 3
                sequence = 2 if completed else 1
                body = {
                    "ok": True,
                    "job": {
                        "id": "payment-job-12345678",
                        "status": "completed" if completed else "running",
                        "stage": "已完成" if completed else "正在授权 PayPal 协议",
                        "result": (
                            {"status": "success", "settlement_status": "confirmed"}
                            if completed else None
                        ),
                        "account_confirmation": (
                            {
                                "status": "plus" if plus_confirmed else "retrying",
                                "protocol_succeeded": True,
                                "plus_confirmed": plus_confirmed,
                                "payment_succeeded": True,
                                "at_refreshed": plus_confirmed,
                                "account_type": "plus" if plus_confirmed else "unverified",
                                "plan": "plus" if plus_confirmed else "",
                                "attempt": 1 if plus_confirmed else 0,
                                "max_attempts": 3,
                                "retry_after": 0 if plus_confirmed else time.time() + 10,
                                "detail": (
                                    "支付后 Cookie 已刷新新 AT，已确认 Plus"
                                    if plus_confirmed
                                    else "协议支付成功；等待 10 秒后进行第 1/3 次 Cookie 登录获取新 AT"
                                ),
                                "checked_at": "2026-08-17T12:00:00+00:00",
                            }
                            if completed else None
                        ),
                        "logs": [
                            {
                                "time": time.time(),
                                "level": "SUCCESS" if completed else "INFO",
                                "message": "协议支付成功" if completed else "协议支付正在执行",
                                "sequence": sequence,
                            }
                        ],
                        "log_count": sequence,
                        "log_sequence": sequence,
                        "finished_at": time.time() if completed else None,
                    },
                }
            else:
                body = payloads.get(path, {"ok": True})
            route.fulfill(
                status=201 if path == "/api/account/paypal-payment" else 200,
                content_type="application/json; charset=utf-8",
                body=json.dumps(body, ensure_ascii=False),
            )

        page.route("**/*", fulfill)
        page.goto("http://hme-control-center.test/#quick-flow", wait_until="domcontentloaded")
        page.wait_for_function("!document.getElementById('startQuickFlowButton').disabled")
        assert page.locator("#quickPostPaymentPhoneBinding").is_checked() is False
        page.locator("#startQuickFlowButton").click()
        page.wait_for_function(
            "document.getElementById('quickFlowStatusBadge').textContent === '已完成'",
            timeout=15_000,
        )
        page.wait_for_function(
            "document.getElementById('terminalPreviewList').innerText.includes('协议支付成功')"
        )

        assert state["job_polls"] == 3
        assert state["payment_payloads"] == [
            {"email": email, "post_payment_phone_binding": False}
        ]
        assert page.locator("#quickFlowPaymentCount").inner_text() == "1"
        assert "新 AT 已确认 Plus" in page.locator("#quickFlowResults").inner_text()
        assert "支付后 Cookie 已刷新新 AT" in page.locator("#terminalPreviewList").inner_text()
        assert "等待 10 秒后进行第 1/3 次" in page.locator(
            "#terminalPreviewList"
        ).inner_text()
        assert page.locator("#terminalPreviewList .terminal-preview-row p").all_inner_texts().count(
            "注册完成，Session 已保存"
        ) == 1
        assert "查看 PP 支付" not in page.locator("#quickFlowResults").inner_text()
        assert page.url.endswith("#quick-flow")
        assert page_errors == []
        browser.close()


def test_failed_paypal_payment_reextracts_link_before_retrying_payment():
    html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
    payloads = _runtime_state_payloads()
    email = "payment-retry@icloud.com"
    old_url = "https://www.paypal.com/agreements/approve?ba_token=BA-OLD123"
    new_url = "https://www.paypal.com/agreements/approve?ba_token=BA-NEW456"
    state = {
        "started": False,
        "card_link_requests": [],
        "payment_requests": 0,
        "request_sequence": [],
    }
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

        def fulfill(route):
            path = urlparse(route.request.url).path
            method = route.request.method
            if path == "/":
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                return
            if path == "/api/gpt-emails":
                items = [] if not state["started"] else [
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
                        "id": "registration-retry",
                        "processId": "registration-retry",
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
                            "id": "registration-retry",
                            "processId": "registration-retry",
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
                payload = route.request.post_data_json
                state["card_link_requests"].append(payload)
                attempt = len(state["card_link_requests"])
                state["request_sequence"].append(f"card-link:{attempt}")
                body = {
                    "ok": True,
                    "url": old_url if attempt == 1 else new_url,
                    "country": "DE",
                    "link_proxy_country": "DE",
                    "cardLinkStatus": "generated",
                    "attemptCount": attempt,
                    "attemptLimit": 2,
                    "logs": ["PayPal 链接已生成"],
                }
            elif path == "/api/account/paypal-payment" and method == "POST":
                state["payment_requests"] += 1
                attempt = state["payment_requests"]
                state["request_sequence"].append(f"payment:{attempt}")
                body = {
                    "ok": True,
                    "country": "DE",
                    "proxyMode": "dynamic",
                    "proxySource": "card_link",
                    "smsProvider": "smsbower",
                    "smsProviderLabel": "SMSBower",
                    "job": {
                        "id": "payment-job-failed" if attempt == 1 else "payment-job-success",
                        "status": "queued",
                        "stage": "排队中",
                    },
                }
            elif path == "/api/account/paypal-payment/payment-job-failed":
                body = {
                    "ok": True,
                    "job": {
                        "id": "payment-job-failed",
                        "status": "failed",
                        "stage": "协议授权失败",
                        "error": "短信验证码等待超时",
                        "result": {"status": "failed"},
                        "logs": [],
                        "log_count": 0,
                        "log_sequence": 0,
                        "finished_at": time.time(),
                    },
                }
            elif path == "/api/account/paypal-payment/payment-job-success":
                body = {
                    "ok": True,
                    "job": {
                        "id": "payment-job-success",
                        "status": "completed",
                        "stage": "已完成",
                        "result": {"status": "success", "settlement_status": "confirmed"},
                        "account_confirmation": {
                            "status": "plus",
                            "protocol_succeeded": True,
                            "plus_confirmed": True,
                            "payment_succeeded": True,
                            "at_refreshed": True,
                            "account_type": "plus",
                            "plan": "plus",
                            "detail": "支付后 Cookie 已刷新新 AT，已确认 Plus",
                            "checked_at": "2026-08-17T12:00:00+00:00",
                        },
                        "logs": [],
                        "log_count": 0,
                        "log_sequence": 0,
                        "finished_at": time.time(),
                    },
                }
            else:
                body = payloads.get(path, {"ok": True})
            route.fulfill(
                status=201 if path == "/api/account/paypal-payment" else 200,
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

        retry_button = page.locator('[data-action="retry-quick-payment"]')
        retry_button.wait_for(state="visible", timeout=1_000)
        assert retry_button.inner_text() == "重新支付"
        assert retry_button.is_enabled()
        assert "点击后将强制重新提链，再启动协议支付" in page.locator(
            ".paypal-payment-action"
        ).inner_text()
        assert "重新启动协议支付" not in page.locator("#quickFlowResults").inner_text()
        assert state["request_sequence"] == ["card-link:1", "payment:1"]

        retry_button.click()
        page.wait_for_function(
            "document.getElementById('quickFlowStatusBadge').textContent === '已完成'",
            timeout=15_000,
        )

        assert state["request_sequence"] == [
            "card-link:1",
            "payment:1",
            "card-link:2",
            "payment:2",
        ]
        assert state["card_link_requests"][0]["force_retry"] is False
        assert state["card_link_requests"][1]["force_retry"] is True
        assert new_url in page.locator("#quickFlowResults").inner_text()
        assert "新 AT 已确认 Plus" in page.locator("#quickFlowResults").inner_text()
        assert page.locator('[data-action="retry-quick-payment"]').count() == 0
        assert page.locator("#quickFlowPaymentCount").inner_text() == "1"
        assert page_errors == []
        browser.close()
