from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlparse

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from playwright.sync_api import sync_playwright

from hidemyemail_generator.account_actions import (
    AccountPaymentGuard,
    AccountPhoneBindingModel,
    AccountPhoneBindingPresenter,
    account_phone_binding_state,
    account_payment_job_is_terminal,
    setup_account_action_routes,
)
from hidemyemail_generator.account_browser import (
    ACCOUNT_BROWSER_RESULT_PREFIX,
    AccountBrowserLaunch,
    AccountBrowserModel,
    AccountBrowserPresenter,
    BrowserWorkerLauncher,
    chatgpt_cookie_storage_state,
)
from hidemyemail_generator.account_browser_worker import (
    ChromeIncognitoStrategy,
    RoxyBrowserStrategy,
    _proxy_options,
)
from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.payment_sms import GlobalSmsRoutingConfigStore
from hidemyemail_generator.web_ui import page_builder
from hidemyemail_generator.web_ui.page_builder import build_app_page
from tests.test_account_management_compact_ui import _workspace_payloads


AUTH_COOKIE = "__Secure-next-auth.session-token"


def save_record(database: Path, email: str, record: dict) -> None:
    connection = connect_db(str(database))
    try:
        connection.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            (f"gpt_account:{email}", json.dumps({"email": email, **record})),
        )
        connection.commit()
    finally:
        connection.close()


def configure_binding_sms(database: Path, provider: str = "smsbower") -> None:
    GlobalSmsRoutingConfigStore(database).configure(
        {
            "binding": {
                "provider": provider,
                "maxPrice": 0.064,
                "countries": ["CL", "US"],
            },
            "apiKeys": {provider: f"{provider}-test-api-key"},
        }
    )


def account_record(cookie_value: str = "private-session-cookie") -> dict:
    return {
        "password": "Confirmed!Password123",
        "password_confirmed": True,
        "registration_proxy_url": "http://user:pass@proxy.test:8080",
        "storage_state_json": json.dumps(
            {
                "cookies": [
                    {
                        "name": AUTH_COOKIE,
                        "value": cookie_value,
                        "domain": ".chatgpt.com",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    },
                    {
                        "name": f"{AUTH_COOKIE}.1",
                        "value": "private-cookie-chunk",
                        "domain": "chatgpt.com",
                        "path": "/",
                        "expires": -1,
                    },
                    {
                        "name": "oai-did",
                        "value": "device-id",
                        "domain": "chatgpt.com",
                        "path": "/",
                        "expires": -1,
                    },
                    {
                        "name": "cf_clearance",
                        "value": "edge-cookie",
                        "domain": ".chatgpt.com",
                        "path": "/",
                        "expires": -1,
                    },
                    {
                        "name": AUTH_COOKIE,
                        "value": "expired-cookie",
                        "domain": ".chatgpt.com",
                        "path": "/",
                        "expires": time.time() - 60,
                    },
                    {
                        "name": AUTH_COOKIE,
                        "value": "foreign-cookie",
                        "domain": ".example.com",
                        "path": "/",
                        "expires": -1,
                    },
                ],
                "origins": [{"origin": "https://chatgpt.com", "localStorage": []}],
            }
        ),
    }


class CookieStorageStateTests(unittest.TestCase):
    def test_keeps_login_identity_and_drops_edge_expired_and_foreign_cookies(self):
        state = chatgpt_cookie_storage_state(account_record())

        names = [item["name"] for item in state["cookies"]]
        values = [item["value"] for item in state["cookies"]]
        self.assertEqual(
            names,
            [AUTH_COOKIE, f"{AUTH_COOKIE}.1", "oai-did"],
        )
        self.assertNotIn("edge-cookie", values)
        self.assertNotIn("expired-cookie", values)
        self.assertNotIn("foreign-cookie", values)
        self.assertEqual(state["origins"], [])

    def test_requires_a_chatgpt_auth_cookie(self):
        with self.assertRaisesRegex(RuntimeError, "ChatGPT Cookie"):
            chatgpt_cookie_storage_state(
                {
                    "cookies": [
                        {
                            "name": "oai-did",
                            "value": "device-only",
                            "domain": "chatgpt.com",
                        }
                    ]
                }
            )


class FakeRoxyStore:
    def runtime_config(self, count):
        assert count == 1
        return {
            "apiUrl": "http://127.0.0.1:50000",
            "workspaceId": "7",
            "profileId": "profile-7",
        }


class RecordingBrowserLauncher:
    def __init__(self):
        self.launches = []
        self.closed = False

    async def open(self, launch):
        self.launches.append(launch)
        return {
            "email": launch.email,
            "mode": launch.mode,
            "cookieCount": launch.cookie_count,
            "processId": 4321,
        }

    async def close(self):
        self.closed = True


class AccountBrowserPresenterTests(unittest.TestCase):
    def test_model_and_presenter_pass_cookie_state_to_chrome_and_roxy(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "accounts.db"
            email = "browser-actions@icloud.com"
            save_record(database, email, account_record())
            launcher = RecordingBrowserLauncher()
            presenter = AccountBrowserPresenter(
                AccountBrowserModel(database, FakeRoxyStore()), launcher=launcher
            )

            chrome = asyncio.run(presenter.open(email, "chrome-incognito"))
            roxy = asyncio.run(presenter.open(email, "roxy"))

        self.assertEqual(chrome["browser"], "Google Chrome 无痕")
        self.assertEqual(roxy["browser"], "Roxy 浏览器")
        self.assertEqual(chrome["cookieCount"], 3)
        self.assertEqual(launcher.launches[0].mode, "chrome")
        self.assertIsNone(launcher.launches[0].roxy)
        self.assertEqual(launcher.launches[1].roxy["profile_id"], "profile-7")
        self.assertEqual(
            launcher.launches[0].storage_state["cookies"][0]["value"],
            "private-session-cookie",
        )
        self.assertNotIn("private-session-cookie", json.dumps(chrome))

    def test_worker_launcher_transmits_cookie_over_stdin_not_command_line(self):
        async def exercise(temporary: str):
            worker = Path(temporary) / "fake_account_browser_worker.py"
            worker.write_text(
                "import json,sys,time\n"
                "payload=json.loads(sys.stdin.read())\n"
                "assert 'private-worker-cookie' not in ' '.join(sys.argv)\n"
                f"print({ACCOUNT_BROWSER_RESULT_PREFIX!r} + "
                "json.dumps({'ok': True}), flush=True)\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            launcher = BrowserWorkerLauncher(
                python_executable=Path(sys.executable),
                worker_script=worker,
                startup_timeout=5,
            )
            launch = AccountBrowserLaunch(
                email="stdin@icloud.com",
                mode="chrome",
                storage_state={
                    "cookies": [
                        {
                            "name": AUTH_COOKIE,
                            "value": "private-worker-cookie",
                            "domain": "chatgpt.com",
                        }
                    ],
                    "origins": [],
                },
                proxy_url="",
            )
            try:
                result = await launcher.open(launch)
                self.assertEqual(result["cookieCount"], 1)
                self.assertNotIn("private-worker-cookie", json.dumps(result))
            finally:
                await launcher.close()

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(exercise(temporary))

    def test_one_roxy_profile_cannot_open_for_two_accounts(self):
        async def exercise(temporary: str):
            worker = Path(temporary) / "fake_roxy_worker.py"
            worker.write_text(
                "import json,sys,time\n"
                "json.loads(sys.stdin.read())\n"
                f"print({ACCOUNT_BROWSER_RESULT_PREFIX!r} + "
                "json.dumps({'ok': True}), flush=True)\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            launcher = BrowserWorkerLauncher(
                python_executable=Path(sys.executable),
                worker_script=worker,
                startup_timeout=5,
            )
            state = {
                "cookies": [
                    {"name": AUTH_COOKIE, "value": "secret", "domain": "chatgpt.com"}
                ],
                "origins": [],
            }
            roxy = {
                "api_url": "http://127.0.0.1:50000",
                "workspace_id": "7",
                "profile_id": "shared-profile",
            }
            try:
                await launcher.open(
                    AccountBrowserLaunch(
                        email="first@icloud.com",
                        mode="roxy",
                        storage_state=state,
                        proxy_url="",
                        roxy=roxy,
                    )
                )
                with self.assertRaisesRegex(RuntimeError, "Roxy 指纹环境已经打开"):
                    await launcher.open(
                        AccountBrowserLaunch(
                            email="second@icloud.com",
                            mode="roxy",
                            storage_state=state,
                            proxy_url="",
                            roxy=roxy,
                        )
                    )
            finally:
                await launcher.close()

        with tempfile.TemporaryDirectory() as temporary:
            asyncio.run(exercise(temporary))


class FakePage:
    def __init__(self, events):
        self.events = events
        self.url = "about:blank"

    def goto(self, url, **options):
        self.events.append(("goto", url, options))
        self.url = url

    def bring_to_front(self):
        self.events.append(("front",))

    def is_closed(self):
        return False

    def close(self):
        self.events.append(("page-close",))


class FakeContext:
    def __init__(self, events, with_page=False):
        self.events = events
        self.pages = [FakePage(events)] if with_page else []

    def new_page(self):
        page = FakePage(self.events)
        self.pages.append(page)
        self.events.append(("new-page",))
        return page

    def clear_cookies(self):
        self.events.append(("clear-cookies",))

    def add_cookies(self, cookies):
        self.events.append(("add-cookies", [dict(item) for item in cookies]))


class FakeBrowser:
    def __init__(self, events, context):
        self.events = events
        self.contexts = [context]

    def new_context(self, **options):
        self.events.append(("new-context", options))
        return self.contexts[0]

    def is_connected(self):
        return True


class FakeChromium:
    def __init__(self, events, browser):
        self.events = events
        self.browser = browser

    def launch(self, **options):
        self.events.append(("launch", options))
        return self.browser

    def connect_over_cdp(self, endpoint, **options):
        self.events.append(("connect", endpoint, options))
        return self.browser


class BrowserStrategyTests(unittest.TestCase):
    def test_nonempty_invalid_proxy_never_falls_back_to_direct(self):
        with self.assertRaisesRegex(RuntimeError, "代理格式无效"):
            _proxy_options("not-a-valid-proxy")

    def test_chrome_is_visible_incognito_and_receives_storage_state_before_navigation(
        self,
    ):
        events = []
        context = FakeContext(events)
        browser = FakeBrowser(events, context)
        playwright = SimpleNamespace(chromium=FakeChromium(events, browser))
        storage = {"cookies": [{"name": AUTH_COOKIE, "value": "secret"}], "origins": []}

        with mock.patch("hidemyemail_generator.account_browser_worker._emit") as emit:
            ChromeIncognitoStrategy().open(
                playwright,
                {
                    "storage_state": storage,
                    "landing_url": "https://chatgpt.com/",
                    "proxy_url": "",
                },
            )

        launch = events[0][1]
        context_options = next(item[1] for item in events if item[0] == "new-context")
        self.assertEqual(launch["channel"], "chrome")
        self.assertFalse(launch["headless"])
        self.assertIn("--incognito", launch["args"])
        self.assertIs(context_options["storage_state"], storage)
        self.assertLess(
            next(
                index for index, item in enumerate(events) if item[0] == "new-context"
            ),
            next(index for index, item in enumerate(events) if item[0] == "goto"),
        )
        emit.assert_called_once_with({"ok": True, "mode": "chrome", "cookie_count": 1})

    def test_roxy_clears_then_adds_cookies_before_navigation_without_image_route(self):
        events = []
        context = FakeContext(events, with_page=True)
        browser = FakeBrowser(events, context)
        playwright = SimpleNamespace(chromium=FakeChromium(events, browser))

        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                events.append(("client",))

            def connection_info(self, profile_id):
                events.append(("connection-info", profile_id))
                return []

            def clear_profile(self, workspace_id, profile_id):
                events.append(("clear-profile", workspace_id, profile_id))

            def modify_profile(self, body):
                events.append(("modify-profile", body))

            def randomize_profile(self, workspace_id, profile_id):
                events.append(("randomize", workspace_id, profile_id))

            def open_profile(self, workspace_id, profile_id, *, background):
                events.append(("open-profile", workspace_id, profile_id, background))
                return {"http": "127.0.0.1:9222"}

            def close_profile(self, profile_id):
                events.append(("close-profile", profile_id))

        cookie = {"name": AUTH_COOKIE, "value": "private-roxy-cookie"}
        with (
            mock.patch(
                "hidemyemail_generator.roxy_registration.RoxyOpenApiClient",
                FakeClient,
            ),
            mock.patch("hidemyemail_generator.account_browser_worker._emit") as emit,
        ):
            RoxyBrowserStrategy().open(
                playwright,
                {
                    "storage_state": {"cookies": [cookie], "origins": []},
                    "landing_url": "https://chatgpt.com/",
                    "proxy_url": "",
                    "roxy": {
                        "api_url": "http://127.0.0.1:50000",
                        "workspace_id": "7",
                        "profile_id": "profile-7",
                    },
                },
            )

        clear_index = next(
            i for i, item in enumerate(events) if item[0] == "clear-cookies"
        )
        add_index = next(i for i, item in enumerate(events) if item[0] == "add-cookies")
        goto_index = next(i for i, item in enumerate(events) if item[0] == "goto")
        self.assertLess(clear_index, add_index)
        self.assertLess(add_index, goto_index)
        self.assertFalse(any(item[0] == "route" for item in events))
        self.assertIn(
            ("open-profile", 7, "profile-7", False),
            events,
        )
        profile = next(item[1] for item in events if item[0] == "modify-profile")
        self.assertTrue(profile["fingerInfo"]["clearHistory"])
        emit.assert_called_once_with({"ok": True, "mode": "roxy", "cookie_count": 1})

    def test_roxy_rejects_remote_cdp_and_closes_profile_after_open_failure(self):
        events = []
        playwright = SimpleNamespace(
            chromium=FakeChromium(events, FakeBrowser(events, FakeContext(events)))
        )

        class RemoteCdpClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def connection_info(self, _profile_id):
                return []

            def clear_profile(self, *_args):
                pass

            def modify_profile(self, _body):
                pass

            def randomize_profile(self, *_args):
                pass

            def open_profile(self, *_args, **_kwargs):
                events.append(("opened",))
                return {"ws": "ws://198.51.100.20:9222/devtools/browser/private"}

            def close_profile(self, profile_id):
                events.append(("closed", profile_id))

        with mock.patch(
            "hidemyemail_generator.roxy_registration.RoxyOpenApiClient",
            RemoteCdpClient,
        ):
            with self.assertRaisesRegex(RuntimeError, "Roxy CDP必须使用本机回环地址"):
                RoxyBrowserStrategy().open(
                    playwright,
                    {
                        "storage_state": {"cookies": [], "origins": []},
                        "landing_url": "https://chatgpt.com/",
                        "proxy_url": "",
                        "roxy": {
                            "api_url": "http://127.0.0.1:50000",
                            "workspace_id": "7",
                            "profile_id": "profile-7",
                        },
                    },
                )

        self.assertEqual(events, [("opened",), ("closed", "profile-7")])

    def test_roxy_rejects_remote_openapi_before_any_cookie_is_sent(self):
        playwright = SimpleNamespace(
            chromium=FakeChromium([], FakeBrowser([], FakeContext([])))
        )
        with self.assertRaisesRegex(RuntimeError, "Roxy OpenAPI必须使用本机回环地址"):
            RoxyBrowserStrategy().open(
                playwright,
                {
                    "storage_state": {
                        "cookies": [{"name": AUTH_COOKIE, "value": "private"}],
                        "origins": [],
                    },
                    "landing_url": "https://chatgpt.com/",
                    "proxy_url": "",
                    "roxy": {
                        "api_url": "http://198.51.100.10:50000",
                        "workspace_id": "7",
                        "profile_id": "profile-7",
                    },
                },
            )


class FakePlusCodex:
    def __init__(self):
        self.calls = []

    async def ensure(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "job_id": kwargs["job"]["id"],
            "email": kwargs["job"]["source_account_email"],
            "status": "running",
            "stage": "oauth_start",
            "detail": "正在启动 Codex OAuth",
            "provider": kwargs["sms_provider"],
        }


class AccountPhoneBindingTests(unittest.TestCase):
    def test_normalizes_current_and_legacy_bound_phone_markers(self):
        self.assertTrue(
            account_phone_binding_state(
                {"plus_sms": {"phone_bound": True, "phone_masked": "+***1234"}}
            )["bound"]
        )
        self.assertTrue(
            account_phone_binding_state(
                {"phone_binding_status": "手机号码已绑定"}
            )["bound"]
        )
        self.assertFalse(
            account_phone_binding_state(
                {"plus_codex": {"status": "failed", "sms_verified": False}}
            )["bound"]
        )

    def test_reuses_confirmed_plus_payment_and_uses_global_binding_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "accounts.db"
            email = "phone-binding@icloud.com"
            job_id = "payment-job-123456"
            save_record(
                database,
                email,
                {
                    **account_record(),
                    "account_type": "plus",
                    "payment_confirmation": {
                        "job_id": job_id,
                        "status": "plus",
                        "payment_succeeded": True,
                    },
                },
            )
            configure_binding_sms(database, "hero-sms")
            plus_codex = FakePlusCodex()
            presenter = AccountPhoneBindingPresenter(
                AccountPhoneBindingModel(database),
                plus_codex=plus_codex,
                sms_resolver=SimpleNamespace(
                    resolve=lambda: SimpleNamespace(provider="hero-sms")
                ),
                base_url="http://127.0.0.1:8765",
            )

            result = asyncio.run(presenter.bind(email))

        self.assertEqual(result["status"], "running")
        self.assertEqual(plus_codex.calls[0]["job"]["id"], job_id)
        self.assertEqual(plus_codex.calls[0]["sms_provider"], "hero-sms")
        self.assertNotIn("password", json.dumps(result).lower())

    def test_existing_plus_without_payment_history_can_start_phone_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "accounts.db"
            email = "existing-plus@icloud.com"
            save_record(
                database,
                email,
                {
                    "account_type": "plus",
                    "registration_proxy_url": "http://user:pass@proxy.test:8080",
                },
            )
            configure_binding_sms(database)
            plus_codex = FakePlusCodex()
            presenter = AccountPhoneBindingPresenter(
                AccountPhoneBindingModel(database),
                plus_codex=plus_codex,
                sms_resolver=SimpleNamespace(
                    resolve=lambda: SimpleNamespace(provider="smsbower")
                ),
                base_url="http://127.0.0.1:8765",
            )

            result = asyncio.run(presenter.bind(email))

        self.assertEqual(result["status"], "running")
        call = plus_codex.calls[0]
        self.assertTrue(call["job"]["id"].startswith("plus-account-"))
        self.assertEqual(call["confirmation"]["source"], "existing_plus_account")
        self.assertFalse(call["confirmation"]["payment_succeeded"])

    def test_failed_phone_binding_is_retryable_until_it_is_really_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "accounts.db"
            email = "retry-plus@icloud.com"
            job_id = "payment-job-retry"
            save_record(
                database,
                email,
                {
                    **account_record(),
                    "account_type": "plus",
                    "payment_confirmation": {
                        "job_id": job_id,
                        "status": "plus",
                        "payment_succeeded": True,
                    },
                    "plus_codex": {
                        "job_id": job_id,
                        "status": "failed",
                        "sms_verified": False,
                    },
                },
            )
            configure_binding_sms(database)
            plus_codex = FakePlusCodex()
            presenter = AccountPhoneBindingPresenter(
                AccountPhoneBindingModel(database),
                plus_codex=plus_codex,
                sms_resolver=SimpleNamespace(
                    resolve=lambda: SimpleNamespace(provider="smsbower")
                ),
                base_url="http://127.0.0.1:8765",
            )

            result = asyncio.run(presenter.bind(email))

        self.assertEqual(result["status"], "running")
        self.assertEqual(plus_codex.calls[0]["job"]["id"], job_id)
        self.assertTrue(plus_codex.calls[0]["retry_failed"])

    def test_phone_binding_status_returns_only_new_persisted_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "accounts.db"
            email = "phone-status@icloud.com"
            save_record(
                database,
                email,
                {
                    **account_record(),
                    "account_type": "plus",
                    "plus_codex": {
                        "job_id": "plus-status-job",
                        "email": email,
                        "status": "failed",
                        "stage": "failed",
                        "detail": "SMSBower 美国线路无库存",
                        "log_sequence": 2,
                        "logs": [
                            {
                                "sequence": 1,
                                "stage": "sms_route",
                                "level": "warning",
                                "message": "智利线路无库存，回退美国",
                            },
                            {
                                "sequence": 2,
                                "stage": "failed",
                                "level": "error",
                                "message": "美国线路无库存",
                            },
                        ],
                    },
                },
            )
            presenter = AccountPhoneBindingPresenter(
                AccountPhoneBindingModel(database),
                plus_codex=FakePlusCodex(),
                sms_resolver=SimpleNamespace(resolve=lambda: None),
                base_url="http://127.0.0.1:8765",
            )

            result = asyncio.run(presenter.status(email, log_after=1))

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["logSequence"], 2)
        self.assertEqual([item["sequence"] for item in result["logs"]], [2])

    def test_legacy_bound_phone_does_not_start_another_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "accounts.db"
            email = "already-bound@icloud.com"
            save_record(
                database,
                email,
                {
                    **account_record(),
                    "account_type": "plus",
                    "bound_phone": "+15551234567",
                },
            )
            plus_codex = FakePlusCodex()
            presenter = AccountPhoneBindingPresenter(
                AccountPhoneBindingModel(database),
                plus_codex=plus_codex,
                sms_resolver=SimpleNamespace(resolve=lambda: None),
                base_url="http://127.0.0.1:8765",
            )

            result = asyncio.run(presenter.bind(email))

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["smsVerified"])
        self.assertEqual(plus_codex.calls, [])

    def test_rejects_free_account_before_renting_a_phone(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "accounts.db"
            email = "free-account@icloud.com"
            save_record(database, email, {**account_record(), "account_type": "free"})
            presenter = AccountPhoneBindingPresenter(
                AccountPhoneBindingModel(database),
                plus_codex=FakePlusCodex(),
                sms_resolver=SimpleNamespace(
                    resolve=lambda: SimpleNamespace(provider="smsbower")
                ),
                base_url="http://127.0.0.1:8765",
            )

            with self.assertRaisesRegex(RuntimeError, "升级为 Plus"):
                asyncio.run(presenter.bind(email))


class AccountPaymentGuardTests(unittest.TestCase):
    def test_guard_reserves_once_until_matching_job_is_released(self):
        async def exercise():
            guard = AccountPaymentGuard()
            first, second = await asyncio.gather(
                guard.reserve("paid@icloud.com"),
                guard.reserve("paid@icloud.com"),
            )
            self.assertEqual(sorted((first, second)), [False, True])
            await guard.started("paid@icloud.com", "payment-job-123456")
            self.assertEqual(await guard.active_emails(), {"paid@icloud.com"})
            await guard.release(job_id="another-job-123456")
            self.assertFalse(await guard.reserve("paid@icloud.com"))
            await guard.release(job_id="payment-job-123456")
            self.assertEqual(await guard.active_emails(), set())
            self.assertTrue(await guard.reserve("paid@icloud.com"))

        asyncio.run(exercise())

    def test_payment_terminal_waits_for_account_confirmation(self):
        base = {
            "status": "completed",
            "result": {"status": "success"},
        }
        self.assertFalse(
            account_payment_job_is_terminal(
                {**base, "account_confirmation": {"status": "retrying"}}
            )
        )
        self.assertFalse(
            account_payment_job_is_terminal(
                {**base, "account_confirmation": {"status": "plus_sms"}}
            )
        )
        self.assertTrue(
            account_payment_job_is_terminal(
                {**base, "account_confirmation": {"status": "plus"}}
            )
        )
        self.assertTrue(
            account_payment_job_is_terminal(
                {"status": "failed", "result": {"status": "error"}}
            )
        )


class RouteBrowserPresenter:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def open(self, email, mode):
        self.calls.append((email, mode))
        return {
            "ok": True,
            "email": email,
            "mode": mode,
            "browser": "Google Chrome 无痕",
            "cookieCount": 2,
            "message": "已打开并注入 2 条账号 Cookie",
        }

    async def close(self):
        self.closed = True


class RoutePhonePresenter:
    def __init__(self):
        self.calls = []

    async def bind(self, email):
        self.calls.append(email)
        return {"ok": True, "email": email, "status": "running"}

    async def status(self, email, *, log_after=0):
        self.calls.append((email, log_after))
        return {
            "ok": True,
            "email": email,
            "status": "running",
            "logSequence": 3,
            "logs": [{"sequence": 3, "message": "智利线路无库存，回退美国"}],
        }


class AccountActionRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.browser = RouteBrowserPresenter()
        self.phone = RoutePhonePresenter()
        self.app = web.Application()
        self.app["local_token"] = "local-test-token"
        setup_account_action_routes(
            self.app,
            base_url="http://127.0.0.1:8765",
            browser_presenter=self.browser,
            phone_presenter=self.phone,
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.headers = {"X-Local-Token": "local-test-token"}

    async def asyncTearDown(self):
        await self.client.close()

    async def test_browser_route_passes_only_email_and_mode_to_presenter(self):
        response = await self.client.post(
            "/api/account/actions/open-browser",
            json={"email": "route@icloud.com", "mode": "chrome"},
            headers=self.headers,
        )

        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(self.browser.calls, [("route@icloud.com", "chrome")])
        self.assertEqual(payload["cookieCount"], 2)
        self.assertNotIn("private", json.dumps(payload))

    async def test_bind_phone_alias_and_grouped_route_share_presenter(self):
        for path in (
            "/api/account/actions/bind-phone",
            "/api/account/bind-phone",
        ):
            response = await self.client.post(
                path,
                json={"email": "route@icloud.com"},
                headers=self.headers,
            )
            self.assertEqual(response.status, 200)
        self.assertEqual(self.phone.calls, ["route@icloud.com", "route@icloud.com"])

    async def test_bind_phone_status_aliases_forward_incremental_log_sequence(self):
        for path in (
            "/api/account/actions/bind-phone/status",
            "/api/account/bind-phone/status",
        ):
            response = await self.client.get(
                path + "?email=route%40icloud.com&log_after=2",
                headers=self.headers,
            )
            payload = await response.json()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["logSequence"], 3)
            self.assertIn("回退美国", payload["logs"][0]["message"])
        self.assertEqual(
            self.phone.calls,
            [("route@icloud.com", 2), ("route@icloud.com", 2)],
        )

    async def test_routes_require_local_token(self):
        response = await self.client.post(
            "/api/account/actions/open-browser",
            json={"email": "route@icloud.com", "mode": "roxy"},
        )

        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())["error"], "本地请求令牌无效")


class AccountActionFrontendTests(unittest.TestCase):
    def test_account_management_assembles_four_mvp_actions(self):
        page = build_app_page()

        for label in (
            "提链支付",
            "绑定手机号",
            "打开谷歌无痕浏览器",
            "打开 Roxy 浏览器",
        ):
            self.assertIn(label, page)
        for class_name in (
            "AccountActionModel",
            "AccountActionView",
            "AccountActionPresenter",
        ):
            self.assertIn(f"class {class_name}", page)
        self.assertIn('data-account-operation="', page)
        self.assertIn('"/api/account/actions/open-browser"', page)
        self.assertIn('"/api/account/actions/bind-phone"', page)
        self.assertIn('"/api/account/actions/bind-phone/status?"', page)
        self.assertIn('"hme:phone-binding-snapshot"', page)
        self.assertIn("phoneBindingCandidate", page)
        self.assertNotIn("手机号绑定日志</strong>", page)
        self.assertIn("setTimeout(resolve, 800)", page)
        self.assertIn('"/api/account/card-link"', page)
        self.assertIn('"/api/account/paypal-payment"', page)

    def test_account_action_controller_is_separate_and_below_line_limit(self):
        static_root = Path(page_builder.__file__).with_name("static")
        source = Path(page_builder.__file__).read_text(encoding="utf-8")

        self.assertIn('"static/account_actions.js"', source)
        self.assertIn('"static/payment_outcome.js"', source)
        self.assertIn('"static/sms_settings.js"', source)
        for filename in (
            "app.js",
            "account_actions.js",
            "payment_outcome.js",
            "quick_flow_account_result.js",
            "sms_settings.js",
        ):
            line_count = len(
                (static_root / filename).read_text(encoding="utf-8").splitlines()
            )
            self.assertLessEqual(line_count, 5000, filename)

    def test_paypal_us_and_gb_account_actions_use_fixed_two_proxy_update_route(self):
        source = (
            Path(page_builder.__file__).with_name("static") / "account_actions.js"
        ).read_text(encoding="utf-8")

        for country in ("US", "GB"):
            self.assertIn(
                f'country: "{country}", amount: "0", singleProxy: false, '
                f'secondCountry: "{country}", promotionProxyChoice: "second"',
                source,
            )
        self.assertIn(
            'promotion_proxy_choice: config.policy.promotionProxyChoice || "first"',
            source,
        )
        self.assertIn("use_secondary_proxy: false", source)

    def test_phone_binding_logs_are_rendered_in_terminal_session(self):
        html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()

            def fulfill(route):
                if urlparse(route.request.url).path == "/":
                    route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=html,
                    )
                else:
                    route.fulfill(
                        status=200,
                        content_type="application/json; charset=utf-8",
                        body='{"ok":true,"items":[]}',
                    )

            page.route("**/*", fulfill)
            page.goto("http://hme-account.test/", wait_until="domcontentloaded")
            page.evaluate(
                """() => window.dispatchEvent(new CustomEvent(
                  "hme:phone-binding-snapshot",
                  {detail: {
                    email: "terminal-phone@icloud.com",
                    snapshot: {
                      jobId: "phone-job-terminal",
                      status: "running",
                      startedAt: "2026-08-17T13:34:41Z",
                      logs: [{
                        sequence: 1,
                        at: "2026-08-17T13:34:42Z",
                        stage: "cookie_login",
                        level: "info",
                        message: "正在使用已保存的 Cookie 登录",
                      }],
                    },
                  }},
                ))"""
            )
            page.wait_for_function(
                "() => document.getElementById('terminalPreviewList').textContent.includes('Cookie 登录')"
            )

            self.assertIn(
                "手机号绑定 · terminal-phone@icloud.com",
                page.locator("#terminalSessionSelect").input_value()
                + " "
                + " ".join(page.locator("#terminalSessionSelect option").all_text_contents()),
            )
            self.assertIn("[手机号绑定]", page.locator("#terminalPreviewList").inner_text())
            self.assertEqual(page.locator(".phone-binding-log-panel").count(), 0)
            browser.close()

    def test_expanded_account_shows_actions_and_browser_request_never_uploads_cookie(
        self,
    ):
        html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
        payloads = _workspace_payloads()
        account = payloads["/api/gpt-emails"]["items"][0]
        account.update(
            accountType="free",
            hasCookies=True,
            plusCodexStatus="",
            plusSmsVerified=False,
        )
        browser_requests = []
        phone_requests = []
        payment_requests = []
        payment_status_reads = []

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
                if path == "/api/account/actions/open-browser":
                    browser_payload = route.request.post_data_json
                    browser_requests.append(browser_payload)
                    route.fulfill(
                        status=200,
                        content_type="application/json; charset=utf-8",
                        body=json.dumps(
                            {
                                "ok": True,
                                "message": "Roxy 浏览器已打开"
                                if browser_payload.get("mode") == "roxy"
                                else "Google Chrome 无痕窗口已打开",
                                "cookieCount": 3,
                            },
                            ensure_ascii=False,
                        ),
                    )
                    return
                if path == "/api/account/actions/bind-phone":
                    phone_requests.append(route.request.post_data_json)
                    route.fulfill(
                        status=200,
                        content_type="application/json; charset=utf-8",
                        body=json.dumps(
                            {"ok": True, "message": "手机号绑定任务已启动"},
                            ensure_ascii=False,
                        ),
                    )
                    return
                if path == "/api/account/card-link":
                    payment_requests.append((path, route.request.post_data_json))
                    route.fulfill(
                        status=200,
                        content_type="application/json; charset=utf-8",
                        body=json.dumps(
                            {
                                "ok": True,
                                "cardLinkStatus": "generated",
                                "url": "https://www.paypal.com/agreements/approve?ba_token=test",
                            }
                        ),
                    )
                    return
                if path == "/api/account/paypal-payment":
                    payment_requests.append((path, route.request.post_data_json))
                    route.fulfill(
                        status=201,
                        content_type="application/json; charset=utf-8",
                        body=json.dumps(
                            {
                                "ok": True,
                                "cookieCount": 3,
                                "smsProviderLabel": "HeroSMS",
                                "url": "/paypal-pay/",
                                "job": {"id": "payment-job-123456", "status": "queued"},
                            }
                        ),
                    )
                    return
                if path == "/api/account/paypal-payment/payment-job-123456":
                    payment_status_reads.append(path)
                    confirmation_ready = len(payment_status_reads) > 1
                    route.fulfill(
                        status=200,
                        content_type="application/json; charset=utf-8",
                        body=json.dumps(
                            {
                                "ok": True,
                                "job": {
                                    "id": "payment-job-123456",
                                    "status": "completed",
                                    "result": {"status": "success"},
                                    "account_confirmation": {
                                        "status": "plus"
                                        if confirmation_ready
                                        else "retrying",
                                        "plus_confirmed": confirmation_ready,
                                        "account_type": "plus"
                                        if confirmation_ready
                                        else "unverified",
                                    },
                                    "plus_codex": {
                                        "status": "completed"
                                        if confirmation_ready
                                        else "running"
                                    },
                                },
                            }
                        ),
                    )
                    return
                route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=json.dumps(
                        payloads.get(path, {"ok": True}), ensure_ascii=False
                    ),
                )

            page.route("**/*", fulfill)
            page.goto(
                "http://hme-account.test/#accounts", wait_until="domcontentloaded"
            )
            page.locator('button[data-action="select-account"]').first.click()
            page.wait_for_selector('[data-account-operation="open-roxy"]')

            actions = page.locator("[data-account-operation]")
            self.assertEqual(actions.count(), 4)
            self.assertEqual(
                actions.all_text_contents(),
                [
                    "提链支付",
                    "绑定手机号",
                    "打开谷歌无痕浏览器",
                    "打开 Roxy 浏览器",
                ],
            )
            page.locator('[data-account-operation="open-chrome"]').click()
            page.wait_for_function(
                "() => document.getElementById('toast').classList.contains('show')"
            )
            self.assertEqual(
                browser_requests,
                [{"email": account["email"], "mode": "chrome"}],
            )
            self.assertNotIn("cookie", json.dumps(browser_requests).lower())
            page.locator('[data-account-operation="open-roxy"]').click()
            page.wait_for_function("() => document.getElementById('toast').textContent.includes('Roxy')")
            self.assertEqual(
                browser_requests[-1],
                {"email": account["email"], "mode": "roxy"},
            )
            page.locator('[data-account-operation="extract-payment"]').click()
            page.wait_for_function(
                "() => document.getElementById('paypalPaymentFrame').dataset.loaded === '1'"
            )
            page.wait_for_function(
                "() => document.getElementById('toast').textContent.includes('手机号绑定已完成')"
            )
            self.assertEqual(
                [path for path, _payload in payment_requests],
                ["/api/account/card-link", "/api/account/paypal-payment"],
            )
            self.assertEqual(payment_requests[0][1]["email"], account["email"])
            self.assertEqual(payment_requests[0][1]["method"], "de_oaics_paypal")
            self.assertEqual(payment_requests[1][1], {"email": account["email"]})
            self.assertNotIn("cookie", json.dumps(payment_requests).lower())
            self.assertEqual(len(payment_status_reads), 2)
            self.assertEqual(phone_requests, [])
            warning = page.evaluate(
                """async (email) => {
                  const messages = [];
                  const model = {
                    accounts: async () => [],
                    monitorPayment: async () => ({
                      id: "payment-job-refresh-failed",
                      status: "completed",
                      result: {status: "success", settlement_status: "confirmed"},
                      account_confirmation: {
                        status: "refresh_failed",
                        protocol_succeeded: true,
                        payment_succeeded: true,
                        plus_confirmed: false,
                        account_type: "unverified",
                        detail: "Cookie 登录未能获取新 AT",
                      },
                    }),
                  };
                  const view = {
                    notify: (message, type) => messages.push({message, type}),
                    decorate: () => {},
                  };
                  const presenter = new window.AccountActionPresenter(model, view);
                  presenter.paymentInFlight.add(email);
                  await presenter.monitorPayment({job: {id: "payment-job-refresh-failed"}}, email);
                  return messages.at(-1);
                }""",
                account["email"],
            )
            self.assertEqual(warning["type"], "warning")
            self.assertIn("协议支付成功；AT/Plus 后置校验失败", warning["message"])
            self.assertNotIn("协议支付失败", warning["message"])
            missing_job = page.evaluate(
                """async (email) => {
                  const messages = [];
                  const model = {accounts: async () => []};
                  const view = {
                    notify: (message, type) => messages.push({message, type}),
                    decorate: () => {},
                  };
                  const presenter = new window.AccountActionPresenter(model, view);
                  presenter.paymentInFlight.add(email);
                  await presenter.monitorPayment({job: {}}, email);
                  return {
                    busy: presenter.paymentInFlight.has(email),
                    notification: messages.at(-1),
                  };
                }""",
                account["email"],
            )
            self.assertFalse(missing_job["busy"])
            self.assertEqual(missing_job["notification"]["type"], "error")
            self.assertIn("ID 缺失", missing_job["notification"]["message"])
            overflow = page.evaluate(
                "document.getElementById('accountsView').scrollWidth - "
                "document.getElementById('accountsView').clientWidth"
            )
            self.assertLessEqual(overflow, 1)
            browser.close()

    def test_plus_payment_and_phone_buttons_follow_real_binding_state(self):
        html = build_app_page().replace("__LOCAL_TOKEN__", json.dumps("ui-test-token"))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            page.route(
                "**/*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=html,
                )
                if urlparse(route.request.url).path == "/"
                else route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body='{"ok":true,"items":[]}',
                ),
            )
            page.goto("http://hme-account.test/", wait_until="domcontentloaded")

            states = page.evaluate(
                """() => {
                  const read = (item) => {
                    const template = document.createElement("template");
                    template.innerHTML = new window.AccountActionView().actions(item);
                    return [...template.content.querySelectorAll("button")].slice(0, 2).map(
                      (button) => ({
                        label: button.textContent,
                        disabled: button.disabled,
                        title: button.title,
                      }),
                    );
                  };
                  const base = {
                    email: "plus@example.com",
                    accountType: "plus",
                    hasPassword: true,
                    hasCookies: true,
                    sessionStatus: "ready",
                  };
                  return {
                    failed: read({...base, plusPhoneBindingStatus: "failed"}),
                    bound: read({...base, plusPhoneBound: true}),
                    running: read({...base, plusPhoneBindingStatus: "running"}),
                    noPassword: read({...base, hasPassword: false}),
                  };
                }"""
            )

            self.assertEqual(states["failed"][0]["label"], "无需提链支付")
            self.assertTrue(states["failed"][0]["disabled"])
            self.assertEqual(states["failed"][1]["label"], "重新绑定手机号")
            self.assertFalse(states["failed"][1]["disabled"])
            self.assertEqual(states["bound"][1]["label"], "手机号已绑定")
            self.assertTrue(states["bound"][1]["disabled"])
            self.assertEqual(states["running"][1]["label"], "手机号绑定中")
            self.assertTrue(states["running"][1]["disabled"])
            self.assertFalse(states["noPassword"][1]["disabled"])
            self.assertIn("Cookie 登录", states["noPassword"][1]["title"])
            browser.close()


if __name__ == "__main__":
    unittest.main()
