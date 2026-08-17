import json
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from hidemyemail_generator.web_ui import build_app_page


def _runtime_state_payloads() -> dict[str, dict]:
    logs = [
        {
            "at": f"2026-08-15T07:30:{index:02d}.123000+00:00",
            "email": "current-run@icloud.com",
            "message": f"详细消息 {index}：页面状态、执行动作和接口结果均已记录",
            "stage": "email_verification" if index > 9 else "openai_auth",
            "location": "邮箱验证码页" if index > 9 else "OpenAI 邮箱登录页",
            "action": "识别并提交验证码" if index > 9 else "填写注册邮箱并继续",
            "status": "error" if index in {5, 17} else "active",
            "source": "browser_worker",
            "eventType": "worker_log",
            "sequence": index + 1,
            "taskId": "browser-task-current",
        }
        for index in range(20)
    ]
    logs[0]["message"] = (
        "详细消息 0：启动参数 password=SuperSecret! Bearer eyJabc.def.ghi "
        "Cookie: session=raw-cookie proxy=http://proxy-user:proxy-pass@proxy.test:8080 "
        "; cf_clearance=SecondCookieSecret "
        'JSON {"password":"SecretJson","totp":"ABCDEF","session":"Sess123",'
        '"token":"Tok123","twoFactorSecret":"TwoFA123"}'
    )
    logs[1]["message"] = (
        'JSON {"otp":"654321","verificationCode":"123456","sessionId":"SessID",'
        '"authToken":"AuthTok","proxyPassword":"ProxyPw",'
        '"openaiKey":"sk-proj-abc123"} 验证码为 778899 verification code is 445566'
    )
    registration = {
        "ok": True,
        "id": "registration-current",
        "status": "running",
        "running": True,
        "phase": "registering_openai",
        "runningCount": 1,
        "requested": 1,
        "claimed": 1,
        "effectiveConcurrency": 1,
        "message": "正在处理当前注册任务",
        "startedAt": "2026-08-15T07:30:00.000000+00:00",
        "failureRecords": [
            {
                "recordedAt": "2026-08-14T01:00:00.000000+00:00",
                "email": "old-run@icloud.com",
                "failureReason": "昨天任务的全局历史失败，不属于当前任务",
                "failedStage": "failed",
                "reasonCode": "OLD_FAILURE",
            }
        ],
        "tasks": [
            {
                "id": "registration-current",
                "processId": "registration-current",
                "processLabel": "进程 1 · current-run@icloud.com",
                "status": "running",
                "running": True,
                "email": "current-run@icloud.com",
                "emails": ["current-run@icloud.com"],
                "startedAt": "2026-08-15T07:30:00.000000+00:00",
                "logs": logs,
                "historyLogs": [
                    {
                        "at": "2026-08-14T01:00:00.000000+00:00",
                        "message": "不应混入当前运行日志的历史记录",
                        "status": "error",
                    }
                ],
            }
        ],
        "logs": logs,
    }
    browser = {
        "ok": True,
        "id": "browser-main-idle",
        "status": "idle",
        "running": False,
        "total": 0,
        "completed": 0,
        "succeeded": 0,
        "failed": 0,
        "logs": [],
        "runtime": {"available": True, "forceHeadless": False},
    }
    return {
        "/api/gpt-emails": {"ok": True, "items": []},
        "/api/browser/status": browser,
        "/api/registration/status": registration,
        "/api/protocol-registration/status": {
            "ok": True,
            "status": "idle",
            "running": False,
            "logs": [],
            "runtime": {"available": True},
        },
        "/api/account-verification/status": {
            "ok": True,
            "status": "idle",
            "running": False,
            "logs": [],
            "runtime": {"available": True},
        },
        "/api/inbox/status": {"ok": True, "configured": False, "codeCount": 0},
        "/api/registration-proxy/status": {
            "ok": True,
            "enabled": False,
            "configured": False,
            "country": "NL",
            "countries": [],
            "modes": [],
        },
        "/api/card-link-proxy/status": {
            "ok": True,
            "enabled": False,
            "configured": False,
            "country": "DE",
            "countries": [],
            "modes": [],
        },
        "/api/roxy-registration/status": {
            "ok": True,
            "available": False,
            "configured": False,
            "workspaces": [],
            "profiles": [],
        },
        "/api/smsbower/status": {
            "ok": True,
            "configured": False,
            "service": "dr",
            "domain": "gmail.com",
            "maxPrice": 0.05,
        },
        "/api/paypal/status": {"ok": True, "available": False, "running": False},
    }



def test_terminal_is_the_only_log_surface_and_renders_all_redacted_task_logs():
    html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
    payloads = _runtime_state_payloads()
    page_errors: list[str] = []
    console_errors: list[str] = []
    request_methods: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1586, "height": 992})
        context.add_init_script(
            "localStorage.setItem('hme_workspace_auto_refresh', '0');"
            "localStorage.removeItem('hme_terminal_preview_collapsed');"
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
            request_methods.append(route.request.method)
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
        page.goto("http://hme-control-center.test/#overview", wait_until="domcontentloaded")
        page.wait_for_function(
            "document.getElementById('controlTaskCountRunning').textContent === '1' && "
            "document.querySelectorAll('#terminalPreviewList .terminal-preview-row').length === 20"
        )

        assert page.title() == "控制台 · 账号工作台"
        assert page.locator("#viewTitle").inner_text() == "控制台"
        active_tab = page.locator(".workspace-route-tab.active")
        assert active_tab.inner_text().startswith("控制台")
        assert active_tab.get_attribute("aria-current") == "page"

        for removed_selector in (
            "#runtimeLogButton",
            "#runtimeLogTriggerCount",
            "#runtimeLogBackdrop",
            "#runtimeLogDrawer",
            "#runtimeLogResizeHandle",
            "#runtimeLogList",
            "[data-action='open-runtime-log']",
            "[data-action='close-runtime-log']",
            "[data-action='copy-runtime-logs']",
            "#quickFlowLog",
            "#quickFlowLogCount",
            ".quick-flow-console-section",
        ):
            assert page.locator(removed_selector).count() == 0
        assert page.get_by_text("运行日志", exact=True).count() == 0
        assert page.get_by_text("直卡提链日志", exact=True).count() == 0
        assert page.locator("#quickFlowResults").count() == 1

        terminal_panel = page.locator("#workbenchTerminalPanel")
        terminal = page.locator("#terminalPreviewList")
        rows = terminal.locator(".terminal-preview-row")
        assert terminal_panel.is_visible()
        assert page.locator("#terminalPreviewTitle").inner_text() == "终端"
        assert page.locator("#terminalSessionSelect option").count() == 2
        assert page.locator("#terminalSessionSelect").input_value() == (
            "registration:registration-current"
        )
        assert rows.count() == 20

        first_row = rows.first
        first_row.scroll_into_view_if_needed()
        assert first_row.is_visible()
        assert "详细消息 0" in first_row.inner_text()
        assert "[REDACTED]" in first_row.inner_text()

        last_row = rows.last
        last_row.scroll_into_view_if_needed()
        assert last_row.is_visible()
        assert "详细消息 19：页面状态、执行动作和接口结果均已记录" in (
            last_row.inner_text()
        )

        terminal_metrics = terminal.evaluate(
            "element => ({scrollHeight: element.scrollHeight, clientHeight: element.clientHeight})"
        )
        assert terminal_metrics["scrollHeight"] > terminal_metrics["clientHeight"]
        assert terminal.evaluate(
            "element => { element.scrollTop = element.scrollHeight; "
            "return Math.abs(element.scrollHeight - element.clientHeight - element.scrollTop) <= 2; }"
        )

        terminal_text = terminal.inner_text()
        assert "不应混入当前运行日志的历史记录" not in terminal_text
        assert "昨天任务的全局历史失败，不属于当前任务" not in terminal_text
        for secret in (
            "SuperSecret",
            "eyJabc.def.ghi",
            "raw-cookie",
            "proxy-user",
            "proxy-pass",
            "SecretJson",
            "ABCDEF",
            "Sess123",
            "Tok123",
            "TwoFA123",
            "654321",
            "123456",
            "SessID",
            "AuthTok",
            "ProxyPw",
            "sk-proj-abc123",
            "SecondCookieSecret",
            "778899",
            "445566",
        ):
            assert secret not in terminal_text
        assert "[REDACTED]" in terminal_text

        assert page.locator("#controlTaskTableBody tr").count() == 2
        assert "current-run@icloud.com" in page.locator(
            "#controlTaskTableBody"
        ).inner_text()
        assert page.locator("#footerRuntimeLabel").inner_text() == "连接正常"
        assert page.locator("#footerRunningCount").inner_text() == "1"
        assert page.locator("#footerFailedCount").inner_text() == "1"

        toggle = page.locator('[data-action="toggle-terminal-preview"]')
        assert toggle.count() == 1
        toggle.click()
        assert terminal_panel.evaluate(
            "element => element.classList.contains('is-collapsed')"
        )
        collapsed_box = terminal_panel.bounding_box()
        assert collapsed_box is not None and collapsed_box["height"] <= 40
        toggle.click()
        expanded_box = terminal_panel.bounding_box()
        assert expanded_box is not None and expanded_box["height"] >= 240
        assert rows.count() == 20

        assert page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        ) <= 1
        assert set(request_methods) == {"GET"}
        assert page_errors == []
        assert console_errors == []
        browser.close()


def test_terminal_keeps_concurrent_protocol_logs_in_selectable_sessions():
    html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
    payloads = _runtime_state_payloads()
    payloads["/api/registration/status"] = {
        "ok": True,
        "status": "idle",
        "running": False,
        "logs": [],
        "runtime": {"available": True},
    }
    first_logs = [
        {
            "at": "2026-08-17T06:30:01.000000+00:00",
            "email": "first@zkgmail.com",
            "message": "A_ONLY_LOG_1：第一个流程开始",
            "status": "active",
            "sequence": 1,
            "taskId": "protocol-first",
        },
        {
            "at": "2026-08-17T06:30:02.000000+00:00",
            "email": "first@zkgmail.com",
            "message": "A_ONLY_LOG_2：第一个流程等待验证码",
            "status": "waiting",
            "sequence": 2,
            "taskId": "protocol-first",
        },
    ]
    second_logs = [
        {
            "at": "2026-08-17T06:31:01.000000+00:00",
            "email": "second@zkgmail.com",
            "message": "B_ONLY_LOG_1：第二个流程开始",
            "status": "active",
            "sequence": 1,
            "taskId": "protocol-second",
        },
        {
            "at": "2026-08-17T06:31:02.000000+00:00",
            "email": "second@zkgmail.com",
            "message": "B_ONLY_LOG_2：第二个流程保存 Session",
            "status": "success",
            "sequence": 2,
            "taskId": "protocol-second",
        },
    ]
    protocol_state = {
        "ok": True,
        "id": "protocol-second",
        "status": "running",
        "running": True,
        "runningCount": 2,
        "processCount": 2,
        "startedAt": "2026-08-17T06:30:00.000000+00:00",
        "logs": [
            {**entry, "message": "[顶层合并] " + entry["message"]}
            for entry in [*first_logs, *second_logs]
        ],
        "tasks": [
            {
                "id": "protocol-first",
                "processId": "protocol-first",
                "processLabel": "协议流程 01 · first@zkgmail.com",
                "status": "running",
                "running": True,
                "startedAt": "2026-08-17T06:30:00.000000+00:00",
                "currentEmail": "first@zkgmail.com",
                "logs": first_logs,
            },
            {
                "id": "protocol-second",
                "processId": "protocol-second",
                "processLabel": "协议流程 02 · second@zkgmail.com",
                "status": "running",
                "running": True,
                "startedAt": "2026-08-17T06:31:00.000000+00:00",
                "currentEmail": "second@zkgmail.com",
                "logs": second_logs,
            },
        ],
        "runtime": {"available": True},
    }
    payloads["/api/protocol-registration/status"] = protocol_state
    page_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1586, "height": 992})
        context.add_init_script(
            "localStorage.setItem('hme_workspace_auto_refresh', '0');"
            "localStorage.setItem('hme_workspace_refresh_interval', '5000');"
        )
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))

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
        page.goto("http://hme-control-center.test/#overview", wait_until="domcontentloaded")
        selector = page.locator("#terminalSessionSelect")
        terminal = page.locator("#terminalPreviewList")
        page.wait_for_function(
            "document.querySelectorAll('#terminalSessionSelect option').length === 2 && "
            "document.querySelectorAll('#terminalPreviewList .terminal-preview-row').length === 2"
        )

        assert selector.get_attribute("aria-label") == "选择日志会话"
        assert selector.input_value() == "protocol:protocol-second"
        assert "协议流程 01 · first@zkgmail.com" in selector.inner_text()
        assert "协议流程 02 · second@zkgmail.com" in selector.inner_text()
        assert "B_ONLY_LOG_1" in terminal.inner_text()
        assert "A_ONLY_LOG_1" not in terminal.inner_text()
        assert "[顶层合并]" not in terminal.inner_text()

        selector.select_option("protocol:protocol-first")
        assert terminal.get_attribute("data-session-id") == "protocol:protocol-first"
        assert "A_ONLY_LOG_1" in terminal.inner_text()
        assert "B_ONLY_LOG_1" not in terminal.inner_text()

        first_logs.append(
            {
                "at": "2026-08-17T06:32:00.000000+00:00",
                "email": "first@zkgmail.com",
                "message": "A_ONLY_LOG_3：第一个流程已完成",
                "status": "success",
                "sequence": 3,
                "taskId": "protocol-first",
            }
        )
        second_logs.append(
            {
                "at": "2026-08-17T06:32:01.000000+00:00",
                "email": "second@zkgmail.com",
                "message": "B_ONLY_LOG_3：第二个流程继续运行",
                "status": "active",
                "sequence": 3,
                "taskId": "protocol-second",
            }
        )
        protocol_state["runningCount"] = 1
        protocol_state["tasks"][0].update(
            status="completed",
            running=False,
            finishedAt="2026-08-17T06:32:00.000000+00:00",
        )
        page.locator("#workspaceAutoRefresh").check()
        page.wait_for_function(
            "document.getElementById('terminalPreviewList').textContent.includes('A_ONLY_LOG_3')",
            timeout=9_000,
        )

        assert selector.input_value() == "protocol:protocol-first"
        assert "A_ONLY_LOG_3" in terminal.inner_text()
        assert "B_ONLY_LOG_3" not in terminal.inner_text()
        assert "已完成" in selector.locator("option:checked").inner_text()
        assert selector.locator("option").count() == 2

        selector.select_option("protocol:protocol-second")
        assert "B_ONLY_LOG_3" in terminal.inner_text()
        assert "A_ONLY_LOG_3" not in terminal.inner_text()
        assert page_errors == []
        browser.close()
