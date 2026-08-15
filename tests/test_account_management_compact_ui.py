import json
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from hidemyemail_generator.web_ui import build_app_page


def _workspace_payloads() -> dict[str, dict]:
    idle_task = {
        "ok": True,
        "id": "",
        "status": "idle",
        "running": False,
        "runningCount": 0,
        "requested": 0,
        "completed": 0,
        "succeeded": 0,
        "failed": 0,
        "tasks": [],
        "logs": [],
        "failureRecords": [],
        "canStartNext": True,
        "runtime": {"available": True, "forceHeadless": False},
    }
    proxy = {
        "ok": True,
        "enabled": False,
        "configured": False,
        "country": "NL",
        "countries": [],
        "modes": [],
    }
    return {
        "/api/gpt-emails": {"ok": True, "items": []},
        "/api/browser/status": idle_task,
        "/api/registration/status": idle_task,
        "/api/protocol-registration/status": idle_task,
        "/api/account-verification/status": idle_task,
        "/api/inbox/status": {
            "ok": True,
            "configured": False,
            "codeCount": 0,
        },
        "/api/registration-proxy/status": proxy,
        "/api/card-link-proxy/status": {**proxy, "country": "DE"},
        "/api/roxy-registration/status": {
            "ok": True,
            "available": True,
            "configured": True,
            "workspaceId": "workspace-1",
            "profileId": "profile-1",
            "maxConcurrency": 5,
            "workspaces": [{"id": "workspace-1", "name": "Roxy Workspace"}],
            "profiles": [
                {
                    "id": f"profile-{index}",
                    "name": f"日本专用环境 {index}",
                    "sortNumber": index,
                    "open": False,
                }
                for index in range(1, 7)
            ],
        },
        "/api/smsbower/status": {
            "ok": True,
            "configured": False,
            "service": "dr",
            "domain": "gmail.com",
            "maxPrice": 0.05,
        },
        "/api/paypal/status": {
            "ok": True,
            "available": False,
            "running": False,
        },
    }


def _geometry(page) -> dict:
    return page.evaluate(
        """() => {
          const deck = document.querySelector('.registration-command-deck').getBoundingClientRect();
          const table = document.querySelector('#accountsView > .table-panel').getBoundingClientRect();
          const accounts = document.getElementById('accountsView');
          return {
            deckHeight: deck.height,
            deckBottom: deck.bottom,
            tableY: table.y,
            documentOverflow:
              document.documentElement.scrollWidth - document.documentElement.clientWidth,
            accountOverflow: accounts.scrollWidth - accounts.clientWidth,
          };
        }"""
    )


def test_account_management_registration_launchpad_is_compact_and_responsive():
    html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
    payloads = _workspace_payloads()
    page_errors: list[str] = []
    console_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_init_script(
            "localStorage.setItem('hme_registration_mode', 'roxy');"
            "localStorage.setItem('hme_roxy_concurrency', '5');"
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
            route.fulfill(
                status=200,
                content_type="application/json; charset=utf-8",
                body=json.dumps(payloads.get(path, {"ok": True}), ensure_ascii=False),
            )

        page.route("**/*", fulfill)
        page.goto("http://hme-account.test/#accounts", wait_until="domcontentloaded")
        page.wait_for_function(
            "document.title === '账号管理 · 账号工作台' && "
            "!document.getElementById('roxyRegistrationControls').hidden"
        )
        page.wait_for_timeout(200)

        assert page.locator("#viewTitle").inner_text() == "账号管理"
        assert page.locator('[data-route="accounts"]').first.get_attribute(
            "aria-label"
        ) == "账号管理"
        assert "账号设置" not in page.locator("body").inner_text()
        assert page.locator('.registration-command-deck[data-density="compact"]').count() == 1
        assert page.locator("#roxyRegistrationControls").is_visible()
        for element_id in (
            "roxyWorkspace",
            "roxyProfile",
            "roxyConcurrency",
            "roxyTargetCount",
            "roxyWindowMode",
            "registerProviderButton",
            "registrationProxyCountrySearch",
        ):
            assert page.locator(f"#{element_id}").is_visible(), element_id

        desktop_roxy = _geometry(page)
        assert desktop_roxy["deckHeight"] <= 430
        assert desktop_roxy["tableY"] <= 590
        assert desktop_roxy["documentOverflow"] <= 1
        assert desktop_roxy["accountOverflow"] <= 1

        page.locator(
            'label:has(input[name="registrationMode"][value="headed"])'
        ).click()
        page.wait_for_function("document.getElementById('roxyRegistrationControls').hidden")
        desktop_headed = _geometry(page)
        assert desktop_headed["deckHeight"] <= 360
        assert desktop_headed["tableY"] < desktop_roxy["tableY"]

        page.locator(
            'label:has(input[name="registrationMode"][value="protocol"])'
        ).click()
        page.wait_for_function("!document.getElementById('protocolRegistrationPanel').hidden")
        assert page.locator("#registrationManualBlock").evaluate(
            "element => element.classList.contains('mode-disabled')"
        )

        page.locator(
            'label:has(input[name="registrationMode"][value="roxy"])'
        ).click()
        page.wait_for_function("!document.getElementById('roxyRegistrationControls').hidden")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(200)

        mobile_roxy = _geometry(page)
        assert mobile_roxy["deckHeight"] <= 720
        assert mobile_roxy["tableY"] < 844
        assert mobile_roxy["documentOverflow"] <= 1
        assert mobile_roxy["accountOverflow"] <= 1

        deck_height_before = mobile_roxy["deckHeight"]
        page.locator("#registrationMoreOptions > summary").click()
        assert page.locator(".registration-more-panel").is_visible()
        panel = page.locator(".registration-more-panel").bounding_box()
        assert panel is not None
        assert panel["x"] >= 0
        assert panel["x"] + panel["width"] <= 390.5
        assert panel["y"] >= 0
        assert panel["y"] + panel["height"] <= 844.5
        assert page.locator("#registrationEmail").is_visible()
        assert page.locator("#fetchAllButton").is_visible()
        assert abs(_geometry(page)["deckHeight"] - deck_height_before) < 1

        assert page_errors == []
        assert console_errors == []
        browser.close()
