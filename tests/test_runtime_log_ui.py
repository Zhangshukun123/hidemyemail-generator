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
        "启动参数 password=SuperSecret! Bearer eyJabc.def.ghi "
        "Cookie: session=raw-cookie proxy=http://proxy-user:proxy-pass@proxy.test:8080 "
        'JSON {"password":"SecretJson","totp":"ABCDEF","session":"Sess123",'
        '"token":"Tok123","twoFactorSecret":"TwoFA123"}'
    )
    logs[1]["message"] = (
        'JSON {"otp":"654321","verificationCode":"123456","sessionId":"SessID",'
        '"authToken":"AuthTok","proxyPassword":"ProxyPw",'
        '"openaiKey":"sk-proj-abc123"}'
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


def test_control_center_renders_real_tasks_footer_and_redacted_terminal_preview():
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
            "document.querySelectorAll('.terminal-preview-row').length === 9"
        )

        assert page.title() == "控制台 · 账号工作台"
        assert page.locator("#viewTitle").inner_text() == "控制台"
        active_tab = page.locator(".workspace-route-tab.active")
        assert active_tab.inner_text().startswith("控制台")
        assert active_tab.get_attribute("aria-current") == "page"
        assert page.locator("#controlTaskTableBody tr").count() == 2
        assert "current-run@icloud.com" in page.locator(
            "#controlTaskTableBody"
        ).inner_text()
        assert page.locator(".runtime-log-entry").count() == 0

        terminal_text = page.locator("#terminalPreviewList").inner_text()
        assert "详细消息 19" in terminal_text
        for secret in (
            "SuperSecret", "eyJabc.def.ghi", "raw-cookie", "proxy-user", "proxy-pass",
            "SecretJson", "ABCDEF", "Sess123", "Tok123", "TwoFA123",
        ):
            assert secret not in terminal_text
        assert page.locator("#footerRuntimeLabel").inner_text() == "连接正常"
        assert page.locator("#footerRunningCount").inner_text() == "1"
        assert page.locator("#footerFailedCount").inner_text() == "1"

        page.locator('[data-control-task-filter="failed"]').click()
        assert page.locator("#controlTaskTableBody tr").count() == 1
        assert "old-run@icloud.com" in page.locator("#controlTaskTableBody").inner_text()
        page.locator("#controlTaskSearch").fill("current-run")
        assert "暂无匹配任务" in page.locator("#controlTaskTableBody").inner_text()
        page.locator('[data-control-task-filter="all"]').click()
        assert page.locator("#controlTaskTableBody tr").count() == 1

        toggle = page.locator('[data-action="toggle-terminal-preview"]')
        toggle.click()
        assert page.locator("#workbenchTerminalPanel").evaluate(
            "element => element.classList.contains('is-collapsed')"
        )
        collapsed_box = page.locator("#workbenchTerminalPanel").bounding_box()
        assert collapsed_box is not None and collapsed_box["height"] <= 40
        toggle.click()
        expanded_box = page.locator("#workbenchTerminalPanel").bounding_box()
        assert expanded_box is not None and expanded_box["height"] >= 240

        assert page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        ) <= 1
        assert set(request_methods) == {"GET"}
        assert page_errors == []
        assert console_errors == []
        browser.close()


def test_runtime_log_drawer_opens_filters_and_renders_more_than_16_detailed_logs():
    html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
    payloads = _runtime_state_payloads()
    page_errors: list[str] = []
    console_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            permissions=["clipboard-read", "clipboard-write"],
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
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                return
            payload = payloads.get(path, {"ok": True})
            route.fulfill(
                status=200,
                content_type="application/json; charset=utf-8",
                body=json.dumps(payload, ensure_ascii=False),
            )

        page.route("**/*", fulfill)
        page.goto("http://hme-runtime.test/#accounts", wait_until="domcontentloaded")
        page.wait_for_function(
            "document.getElementById('runtimeLogTriggerCount').textContent === '20'"
        )

        assert page.locator("#taskLog").count() == 0
        assert page.locator("text=浏览器执行轨迹").count() == 0
        assert page.locator(".runtime-log-entry").count() == 0
        assert page.locator(".editor-tabbar").count() == 0
        assert page.locator(".editor-tab").count() == 0
        workspace_box_before = page.locator(".workspace").bounding_box()
        page.locator("#runtimeLogButton").click()
        assert page.locator("#runtimeLogDrawer").is_visible()
        assert page.locator("#runtimeLogButton").get_attribute("aria-expanded") == "true"
        page.wait_for_timeout(250)
        drawer_box = page.locator("#runtimeLogDrawer").bounding_box()
        workspace_box_after = page.locator(".workspace").bounding_box()
        assert drawer_box is not None
        assert workspace_box_before == workspace_box_after
        assert drawer_box["x"] > 720
        content_right = page.evaluate("document.body.getBoundingClientRect().right")
        assert abs(drawer_box["x"] + drawer_box["width"] - content_right) < 1
        assert drawer_box["height"] >= 800
        assert drawer_box["height"] > drawer_box["width"]
        assert page.locator("#runtimeLogBackdrop").is_visible()
        drawer_borders = page.locator("#runtimeLogDrawer").evaluate(
            "element => ({ left: getComputedStyle(element).borderLeftWidth, "
            "top: getComputedStyle(element).borderTopWidth })"
        )
        assert drawer_borders == {"left": "1px", "top": "0px"}
        assert page.locator(".runtime-log-entry").count() == 20
        drawer_text = page.locator("#runtimeLogDrawer").inner_text()
        assert "不应混入当前运行日志的历史记录" not in drawer_text
        assert "昨天任务的全局历史失败，不属于当前任务" not in drawer_text
        assert page.locator("#runtimeLogState").inner_text() == "1 个任务运行中"
        assert "SuperSecret" not in drawer_text
        assert "eyJabc.def.ghi" not in drawer_text
        assert "raw-cookie" not in drawer_text
        assert "proxy-user" not in drawer_text
        assert "proxy-pass" not in drawer_text
        for secret in ("SecretJson", "ABCDEF", "Sess123", "Tok123", "TwoFA123"):
            assert secret not in drawer_text
        for secret in ("654321", "123456", "SessID", "AuthTok", "ProxyPw", "sk-proj-abc123"):
            assert secret not in drawer_text
        assert "[REDACTED]" in drawer_text
        assert page.locator(".app-shell").get_attribute("inert") == ""
        assert page.locator("#runtimeLogButton").get_attribute("aria-label") == (
            "打开运行日志，20 条"
        )
        page.evaluate(
            """
            window.__copiedRuntimeLogs = '';
            document.execCommand = command => {
              if (command !== 'copy') return false;
              window.__copiedRuntimeLogs = document.activeElement?.value || '';
              return true;
            };
            """
        )
        page.get_by_role("button", name="复制当前日志").click()
        page.wait_for_function("document.getElementById('toast').classList.contains('show')")
        copied_logs = page.evaluate("window.__copiedRuntimeLogs")
        for secret in (
            "SecretJson", "ABCDEF", "Sess123", "Tok123", "TwoFA123", "654321",
            "123456", "SessID", "AuthTok", "ProxyPw", "sk-proj-abc123",
        ):
            assert secret not in copied_logs
        assert "[REDACTED]" in copied_logs
        assert "2026-08-15" in page.locator(".runtime-log-entry time").first.inner_text()
        assert page.locator(".runtime-log-entry").nth(19).get_by_text(
            "详细消息 19：页面状态、执行动作和接口结果均已记录"
        ).is_visible()
        assert page.locator(".runtime-log-entry").nth(19).get_by_text(
            "识别并提交验证码"
        ).is_visible()

        page.locator("#runtimeLogSearch").fill("详细消息 17")
        assert page.locator(".runtime-log-entry").count() == 1
        assert "详细消息 17" in page.locator(".runtime-log-message").inner_text()
        page.locator("#runtimeLogSearch").fill("")
        page.locator("#runtimeLogLevel").select_option("error")
        assert page.locator(".runtime-log-entry").count() == 2

        page.keyboard.press("Escape")
        assert page.locator("#runtimeLogDrawer").is_hidden()
        assert page.locator("#runtimeLogButton").get_attribute("aria-expanded") == "false"
        assert page.locator(".app-shell").get_attribute("inert") is None

        page.set_viewport_size({"width": 390, "height": 760})
        mobile_opener = page.locator('[data-action="open-runtime-log"]:visible').last
        mobile_opener.click()
        assert not mobile_opener.is_disabled()
        page.wait_for_timeout(250)
        mobile_box = page.locator("#runtimeLogDrawer").bounding_box()
        assert mobile_box is not None
        assert mobile_box["x"] >= 0
        assert mobile_box["x"] + mobile_box["width"] <= 390.5
        assert page.locator("#runtimeLogDrawer").evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        )
        page.keyboard.press("Escape")
        assert mobile_opener.evaluate("element => document.activeElement === element")

        for viewport in ({"width": 760, "height": 390}, {"width": 640, "height": 360}):
            page.set_viewport_size(viewport)
            landscape_opener = page.locator('[data-action="open-runtime-log"]:visible').last
            landscape_opener.click()
            page.wait_for_timeout(250)
            drawer_box = page.locator("#runtimeLogDrawer").bounding_box()
            list_box = page.locator("#runtimeLogList").bounding_box()
            footer_box = page.locator(".runtime-log-footer").bounding_box()
            assert drawer_box is not None
            assert list_box is not None
            assert footer_box is not None
            assert list_box["height"] >= 120
            assert list_box["y"] >= drawer_box["y"]
            assert list_box["y"] + list_box["height"] <= drawer_box["y"] + drawer_box["height"]
            assert footer_box["y"] + footer_box["height"] <= (
                drawer_box["y"] + drawer_box["height"] + 1
            )
            assert page.locator("#runtimeLogDrawer").evaluate(
                "element => element.scrollHeight <= element.clientHeight"
            )
            page.keyboard.press("Escape")
            assert landscape_opener.evaluate("element => document.activeElement === element")

        payloads["/api/registration/status"] = {
            "ok": True,
            "id": "registration-old-failure",
            "status": "failed",
            "running": False,
            "tasks": [],
            "logs": [],
            "failureRecords": [
                {
                    "recordedAt": "2026-08-14T03:00:00.000000+00:00",
                    "email": "newest-failure@icloud.com",
                    "failureReason": "最新的空闲诊断记录",
                    "failedStage": "failed",
                    "reasonCode": "NEWEST_FAILURE",
                },
                {
                    "recordedAt": "2026-08-14T01:00:00.000000+00:00",
                    "email": "old-run@icloud.com",
                    "failureReason": "昨天任务的全局历史失败，不属于当前任务",
                    "failedStage": "failed",
                    "reasonCode": "OLD_FAILURE",
                }
            ],
        }
        payloads["/api/browser/status"] = {
            "ok": True,
            "id": "browser-middle-completed",
            "status": "completed",
            "running": False,
            "startedAt": "2026-08-14T02:00:00.000000+00:00",
            "finishedAt": "2026-08-14T02:00:01.000000+00:00",
            "logs": [
                {
                    "at": "2026-08-14T02:00:01.000000+00:00",
                    "message": "时间居中的普通浏览器完成记录",
                    "status": "success",
                    "sequence": 1,
                }
            ],
        }
        page.locator('[data-action="open-runtime-log"]:visible').last.click()
        page.wait_for_function(
            "document.getElementById('runtimeLogList').innerText.includes('最新的空闲诊断记录')"
        )
        assert "时间居中的普通浏览器完成记录" not in page.locator("#runtimeLogList").inner_text()
        assert page.locator("#runtimeLogState").inner_text().startswith("当前无运行任务")
        page.keyboard.press("Escape")
        assert page_errors == []
        assert console_errors == []
        browser.close()


def test_opening_runtime_log_refreshes_an_idle_page_to_the_current_server_task():
    html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
    payloads = _runtime_state_payloads()
    idle_registration = {
        "ok": True,
        "id": "",
        "status": "idle",
        "running": False,
        "tasks": [],
        "logs": [],
        "failureRecords": [],
    }
    registration_reads = 0
    page_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        def fulfill(route):
            nonlocal registration_reads
            path = urlparse(route.request.url).path
            if path == "/":
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                return
            if path == "/api/registration/status":
                registration_reads += 1
                payload = idle_registration if registration_reads == 1 else payloads[path]
            else:
                payload = payloads.get(path, {"ok": True})
            route.fulfill(
                status=200,
                content_type="application/json; charset=utf-8",
                body=json.dumps(payload, ensure_ascii=False),
            )

        page.route("**/*", fulfill)
        page.goto("http://hme-runtime-refresh.test/#accounts", wait_until="domcontentloaded")
        page.wait_for_function(
            "document.getElementById('registrationRuntimeState').textContent.includes('当前无运行任务')"
        )
        assert page.locator("#runtimeLogTriggerCount").inner_text() == "0"

        page.locator("#runtimeLogButton").click()
        page.wait_for_function(
            "document.getElementById('runtimeLogTriggerCount').textContent === '20'"
        )
        assert page.locator("#runtimeLogDrawer").is_visible()
        assert page.locator(".runtime-log-entry").count() == 20
        assert registration_reads >= 2
        assert page_errors == []
        browser.close()


def test_mobile_overview_keeps_the_global_runtime_log_drawer_entry_visible():
    html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
    payloads = _runtime_state_payloads()
    page_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 760})
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
        page.goto("http://hme-runtime-mobile.test/#overview", wait_until="domcontentloaded")
        page.wait_for_function(
            "document.getElementById('runtimeLogTriggerCount').textContent === '20'"
        )

        opener = page.locator("#runtimeLogButton")
        assert opener.is_visible()
        assert page.locator(".editor-tabbar").count() == 0
        assert page.locator(".workbench-statusbar").count() == 0

        opener.click()
        page.wait_for_timeout(250)
        drawer = page.locator("#runtimeLogDrawer")
        drawer_box = drawer.bounding_box()
        assert drawer.is_visible()
        assert drawer_box is not None
        assert drawer_box["x"] >= 0
        assert drawer_box["x"] + drawer_box["width"] <= 390.5
        assert drawer_box["height"] >= 700
        assert opener.get_attribute("aria-expanded") == "true"

        page.keyboard.press("Escape")
        assert drawer.is_hidden()
        assert opener.get_attribute("aria-expanded") == "false"
        assert opener.evaluate("element => document.activeElement === element")
        assert page_errors == []
        browser.close()


def test_runtime_log_drawer_resizes_with_pointer_keyboard_and_persists():
    html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
    payloads = _runtime_state_payloads()
    page_errors: list[str] = []
    console_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
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
            route.fulfill(
                status=200,
                content_type="application/json; charset=utf-8",
                body=json.dumps(payloads.get(path, {"ok": True}), ensure_ascii=False),
            )

        def drag_handle(delta_x: float):
            handle_box = handle.bounding_box()
            assert handle_box is not None
            x = handle_box["x"] + handle_box["width"] / 2
            y = handle_box["y"] + min(120, handle_box["height"] / 2)
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + delta_x, y, steps=8)
            page.mouse.up()

        page.route("**/*", fulfill)
        page.goto("http://hme-runtime-resize.test/#accounts", wait_until="domcontentloaded")
        page.wait_for_function(
            "document.getElementById('runtimeLogTriggerCount').textContent === '20'"
        )
        workspace_before = page.locator(".workspace").bounding_box()
        assert workspace_before is not None
        page.locator("#runtimeLogButton").click()
        drawer = page.locator("#runtimeLogDrawer")
        handle = page.locator("#runtimeLogResizeHandle")
        assert drawer.is_visible()
        assert handle.is_visible()
        assert handle.get_attribute("role") == "separator"
        assert handle.get_attribute("aria-orientation") == "vertical"
        assert handle.get_attribute("aria-controls") == "runtimeLogDrawer"
        assert handle.get_attribute("tabindex") == "0"
        page.wait_for_timeout(250)
        close_button = page.locator("#runtimeLogCloseButton")
        close_button.focus()
        page.keyboard.press("Shift+Tab")
        assert handle.evaluate("element => document.activeElement === element")
        page.keyboard.press("Shift+Tab")
        copy_button = page.get_by_role("button", name="复制当前日志")
        assert copy_button.evaluate("element => document.activeElement === element")
        page.keyboard.press("Tab")
        assert handle.evaluate("element => document.activeElement === element")

        initial = drawer.bounding_box()
        assert initial is not None
        assert abs(initial["width"] - 680) <= 1
        content_right = page.evaluate("document.body.getBoundingClientRect().right")
        drag_handle(-140)
        wider = drawer.bounding_box()
        assert wider is not None
        assert abs(wider["width"] - (initial["width"] + 140)) <= 2
        assert abs(wider["x"] + wider["width"] - content_right) <= 1
        assert int(handle.get_attribute("aria-valuenow")) == round(wider["width"])
        workspace_after = page.locator(".workspace").bounding_box()
        assert workspace_after is not None
        for dimension in ("x", "y", "width"):
            assert abs(workspace_after[dimension] - workspace_before[dimension]) <= 1

        drag_handle(90)
        narrower = drawer.bounding_box()
        assert narrower is not None
        assert abs(narrower["width"] - (wider["width"] - 90)) <= 2
        pointer_saved_width = str(round(narrower["width"]))
        assert page.evaluate("localStorage.getItem('hme_runtime_log_width')") == (
            pointer_saved_width
        )

        handle_box = handle.bounding_box()
        assert handle_box is not None
        drag_x = handle_box["x"] + handle_box["width"] / 2
        drag_y = handle_box["y"] + 120
        page.mouse.move(drag_x, drag_y)
        page.mouse.down()
        page.mouse.move(drag_x - 60, drag_y, steps=4)
        assert drawer.bounding_box()["width"] > narrower["width"]
        assert page.locator("body").evaluate(
            "element => element.classList.contains('runtime-log-resizing')"
        )
        page.keyboard.press("Escape")
        page.mouse.up()
        assert drawer.is_hidden()
        assert not page.locator("body").evaluate(
            "element => element.classList.contains('runtime-log-resizing')"
        )
        assert page.locator("#runtimeLogButton").evaluate(
            "element => document.activeElement === element"
        )
        assert page.evaluate("localStorage.getItem('hme_runtime_log_width')") == (
            pointer_saved_width
        )
        page.locator("#runtimeLogButton").click()
        page.wait_for_timeout(250)
        assert abs(drawer.bounding_box()["width"] - narrower["width"]) <= 1

        minimum = int(handle.get_attribute("aria-valuemin"))
        maximum = int(handle.get_attribute("aria-valuemax"))
        drag_handle(3000)
        at_minimum = drawer.bounding_box()
        assert at_minimum is not None
        assert abs(at_minimum["width"] - minimum) <= 1
        assert drawer.evaluate("element => element.scrollWidth <= element.clientWidth")
        drag_handle(-3000)
        at_maximum = drawer.bounding_box()
        assert at_maximum is not None
        assert abs(at_maximum["width"] - maximum) <= 1
        assert abs(at_maximum["x"] + at_maximum["width"] - content_right) <= 1

        handle.focus()
        page.keyboard.press("Home")
        assert abs(drawer.bounding_box()["width"] - minimum) <= 1
        page.keyboard.press("ArrowLeft")
        assert abs(drawer.bounding_box()["width"] - (minimum + 16)) <= 1
        page.keyboard.press("ArrowRight")
        assert abs(drawer.bounding_box()["width"] - minimum) <= 1
        page.keyboard.press("End")
        assert abs(drawer.bounding_box()["width"] - maximum) <= 1
        assert handle.evaluate("element => document.activeElement === element")

        handle.dblclick()
        handle.focus()
        page.keyboard.press("Shift+ArrowLeft")
        saved_width = 744
        assert abs(drawer.bounding_box()["width"] - saved_width) <= 1
        assert page.evaluate("localStorage.getItem('hme_runtime_log_width')") == str(saved_width)
        page.keyboard.press("Escape")
        page.locator("#runtimeLogButton").click()
        assert abs(drawer.bounding_box()["width"] - saved_width) <= 1

        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "document.getElementById('runtimeLogTriggerCount').textContent === '20'"
        )
        page.locator("#runtimeLogButton").click()
        assert abs(drawer.bounding_box()["width"] - saved_width) <= 1

        page.set_viewport_size({"width": 700, "height": 800})
        page.wait_for_function(
            "() => { const handle = document.getElementById('runtimeLogResizeHandle'); "
            "return Math.abs(document.getElementById('runtimeLogDrawer').getBoundingClientRect().width - "
            "Number(handle.getAttribute('aria-valuemax'))) <= 1; }"
        )
        constrained = drawer.bounding_box()
        activity_bar_width = page.evaluate(
            "parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--workbench-activitybar-width'))"
        )
        assert constrained is not None
        assert constrained["x"] >= activity_bar_width - 1
        assert page.evaluate("localStorage.getItem('hme_runtime_log_width')") == str(saved_width)

        page.set_viewport_size({"width": 390, "height": 760})
        page.wait_for_function(
            "document.getElementById('runtimeLogResizeHandle').tabIndex === -1"
        )
        mobile = drawer.bounding_box()
        mobile_layout_width = page.evaluate("window.innerWidth")
        assert mobile is not None
        assert mobile["width"] >= mobile_layout_width - 1
        assert mobile["width"] <= 390.5
        assert mobile["x"] >= 0
        assert handle.is_hidden()
        assert handle.get_attribute("tabindex") == "-1"
        assert page.locator("#runtimeLogCloseButton").evaluate(
            "element => document.activeElement === element"
        )
        assert page.evaluate("localStorage.getItem('hme_runtime_log_width')") == str(saved_width)

        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "document.getElementById('runtimeLogTriggerCount').textContent === '20'"
        )
        page.locator("#runtimeLogButton").click()
        assert handle.is_hidden()
        assert handle.get_attribute("aria-disabled") == "true"
        mobile_before_keyboard = drawer.bounding_box()["width"]
        handle.evaluate("element => element.focus()")
        assert not handle.evaluate("element => document.activeElement === element")
        page.keyboard.press("ArrowLeft")
        assert abs(drawer.bounding_box()["width"] - mobile_before_keyboard) <= 1
        assert page.evaluate("localStorage.getItem('hme_runtime_log_width')") == str(saved_width)

        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_function(
            "width => Math.abs(document.getElementById('runtimeLogDrawer').getBoundingClientRect().width - width) <= 1",
            arg=saved_width,
        )
        restored = drawer.bounding_box()
        assert restored is not None
        assert abs(restored["width"] - saved_width) <= 1
        assert handle.get_attribute("aria-disabled") == "false"
        assert page.evaluate("localStorage.getItem('hme_runtime_log_width')") == str(saved_width)
        page.keyboard.press("Escape")
        assert page_errors == []
        assert console_errors == []
        browser.close()
