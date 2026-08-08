import asyncio
import unittest

from hidemyemail_generator.registration_tasks import RegistrationTaskManager
from hidemyemail_generator.registration_tasks import generate_openai_password


class FakeBrowserManager:
    def __init__(self, *, password_confirmed=True, two_factor_enabled=True):
        self.started_accounts = []
        self.start_options = {}
        self.reset_count = 0
        self.password_confirmed = password_confirmed
        self.two_factor_enabled = two_factor_enabled
        self.state = {
            "status": "idle",
            "running": False,
            "succeeded": 0,
            "accounts": [],
        }

    def availability(self):
        return {"available": True, "errors": []}

    def snapshot(self):
        return dict(self.state)

    def reset(self):
        self.reset_count += 1
        self.state.update(
            status="idle",
            running=False,
            succeeded=0,
            accounts=[],
        )
        return self.snapshot()

    def start(
        self, accounts, *, headless, concurrency, use_registration_proxy=False
    ):
        self.started_accounts = accounts
        self.start_options = {
            "headless": headless,
            "concurrency": concurrency,
            "use_registration_proxy": use_registration_proxy,
        }
        self.state.update(
            status="running",
            running=True,
            headless=headless,
            useRegistrationProxy=use_registration_proxy,
        )
        return self.snapshot()

    async def wait(self):
        await asyncio.sleep(0)
        self.state.update(
            status="completed",
            running=False,
            succeeded=len(self.started_accounts),
            accounts=[
                {
                    "email": account["email"],
                    "status": "success",
                    "passwordConfirmed": self.password_confirmed,
                    "twoFactorEnabled": self.two_factor_enabled,
                }
                for account in self.started_accounts
            ],
        )
        return self.snapshot()

    async def stop(self):
        self.state.update(status="cancelled", running=False)
        return self.snapshot()


class RegistrationTaskManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_password_contains_required_character_groups(self):
        password = generate_openai_password()
        self.assertGreaterEqual(len(password), 16)
        self.assertRegex(password, r"[A-Z]")
        self.assertRegex(password, r"[a-z]")
        self.assertRegex(password, r"\d")
        self.assertRegex(password, r"[!@#$%^&*_+=-]")

    async def test_claims_inventory_email_then_runs_browser_registration(self):
        browser = FakeBrowserManager()
        events = []

        async def acquire(label):
            events.append(("acquire", label))
            return "new-alias@icloud.com"

        async def confirm(email):
            events.append(("confirm", email))

        async def save_password(email, password):
            events.append(("save_password", email, password))

        async def complete(email, success, message):
            events.append(("complete", email, success, message))

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
            save_password=save_password,
            complete_email=complete,
        )
        state = manager.start(label="OpenAI 一键注册", headless=True, concurrency=1)
        self.assertTrue(state["running"])
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["phase"], "completed")
        self.assertEqual(snapshot["email"], "new-alias@icloud.com")
        self.assertEqual(events[0], ("acquire", "OpenAI 一键注册"))
        self.assertEqual(events[1], ("confirm", "new-alias@icloud.com"))
        self.assertEqual(events[2][0:2], ("save_password", "new-alias@icloud.com"))
        self.assertEqual(events[3][0:3], ("complete", "new-alias@icloud.com", True))
        self.assertEqual(browser.started_accounts[0]["email"], "new-alias@icloud.com")
        self.assertTrue(browser.started_accounts[0]["enable_2fa"])
        self.assertTrue(browser.started_accounts[0]["ensure_password"])
        self.assertFalse(browser.started_accounts[0]["force_reset_password"])
        self.assertEqual(browser.started_accounts[0]["password"], events[2][2])
        self.assertIn("成功 1/1", snapshot["message"])
        self.assertIn("密码已设置 1/1", snapshot["message"])
        self.assertIn("2FA 已开启 1/1", snapshot["message"])
        self.assertTrue(browser.state["headless"])
        self.assertTrue(browser.state["useRegistrationProxy"])
        self.assertEqual(browser.start_options["concurrency"], 1)
        self.assertEqual(browser.reset_count, 1)

    async def test_manual_email_skips_inventory_and_starts_single_registration(self):
        browser = FakeBrowserManager()
        events = []

        async def acquire(_label):
            events.append("acquire")
            return "unused@icloud.com"

        async def confirm(_email):
            events.append("confirm")

        async def save_password(email, _password):
            events.append(("save", email))

        async def complete(*_args):
            events.append("complete")

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
            save_password=save_password,
            complete_email=complete,
        )
        state = manager.start(
            label="手动邮箱注册",
            email="Manual.User@QQ.COM",
            headless=False,
            concurrency=8,
        )
        self.assertEqual(state["email"], "manual.user@qq.com")
        self.assertEqual(state["requested"], 1)
        await asyncio.wait_for(manager._task, timeout=5)

        self.assertEqual(events, [("save", "manual.user@qq.com")])
        self.assertEqual(browser.start_options["concurrency"], 1)
        self.assertFalse(browser.start_options["headless"])
        self.assertEqual(browser.started_accounts[0]["email"], "manual.user@qq.com")
        self.assertTrue(browser.started_accounts[0]["manual_otp_entry"])
        self.assertTrue(browser.started_accounts[0]["foreground_required"])

    async def test_manual_verification_code_wait_and_submit_lifecycle(self):
        manager = RegistrationTaskManager(
            browser_manager=FakeBrowserManager(),
            acquire_email=lambda _label: None,
            confirm_email=lambda _email: None,
        )
        manager._state.update(
            running=True,
            status="running",
            email="manual@qq.com",
            emails=["manual@qq.com"],
        )

        self.assertEqual(manager.poll_verification_code("manual@qq.com"), "")
        self.assertTrue(manager.snapshot()["awaitingCode"])
        manager.submit_verification_code("manual@qq.com", " 12 34 56 ")
        self.assertEqual(manager.poll_verification_code("manual@qq.com"), "123456")
        self.assertFalse(manager.snapshot()["awaitingCode"])

    async def test_smsbower_provider_acquires_gmail_and_reports_completion(self):
        browser = FakeBrowserManager()
        events = []

        async def acquire_provider(label):
            events.append(("acquire_provider", label))
            return "api.gmail@gmail.com"

        async def poll_provider(email):
            events.append(("poll_provider", email))
            return "654321"

        async def complete_provider(email, success, message):
            events.append(("complete_provider", email, success, message))

        async def acquire_inventory(_label):
            raise AssertionError("SMSBower flow must not use iCloud inventory")

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire_inventory,
            confirm_email=lambda _email: None,
            acquire_provider_email=acquire_provider,
            poll_provider_code=poll_provider,
            complete_provider_email=complete_provider,
        )
        state = manager.start(
            label="SMSBower Gmail 注册",
            provider="smsbower",
            headless=True,
            concurrency=8,
        )
        self.assertEqual(state["provider"], "smsbower")
        self.assertEqual(state["requested"], 1)
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["email"], "api.gmail@gmail.com")
        self.assertEqual(events[0], ("acquire_provider", "SMSBower Gmail 注册"))
        self.assertEqual(events[-1][0:3], ("complete_provider", "api.gmail@gmail.com", True))
        self.assertEqual(browser.started_accounts[0]["email"], "api.gmail@gmail.com")
        self.assertTrue(
            browser.started_accounts[0]["password_first_required"]
        )
        self.assertTrue(browser.started_accounts[0]["foreground_required"])
        self.assertFalse(browser.start_options["headless"])

    async def test_smsbower_provider_poll_returns_code_without_manual_input(self):
        async def poll_provider(email):
            self.assertEqual(email, "auto.code@gmail.com")
            return "246810"

        manager = RegistrationTaskManager(
            browser_manager=FakeBrowserManager(),
            acquire_email=lambda _label: None,
            confirm_email=lambda _email: None,
            poll_provider_code=poll_provider,
        )
        manager._state.update(
            running=True,
            status="running",
            provider="smsbower",
            email="auto.code@gmail.com",
            emails=["auto.code@gmail.com"],
        )

        code = await manager.poll_verification_code_async("auto.code@gmail.com")

        self.assertEqual(code, "246810")
        self.assertFalse(manager.snapshot()["awaitingCode"])
        self.assertIn("SMSBower 已返回验证码", manager.snapshot()["message"])

    async def test_skips_two_factor_when_password_was_not_confirmed(self):
        browser = FakeBrowserManager(
            password_confirmed=False,
            two_factor_enabled=False,
        )

        async def acquire(_label):
            return "pending-password@icloud.com"

        async def confirm(_email):
            return None

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
        )
        manager.start(label="OpenAI 一键注册", headless=True)
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "completed")
        self.assertIn("密码已设置 0/1", snapshot["message"])
        self.assertIn("2FA 已开启 0/1", snapshot["message"])

    async def test_empty_inventory_fails_without_starting_browser(self):
        browser = FakeBrowserManager()

        async def acquire(_label):
            return ""

        async def confirm(_email):
            raise AssertionError("empty inventory must not confirm an email")

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
        )
        manager.start(label="OpenAI 一键注册", headless=True)
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("邮箱库存不足", snapshot["message"])
        self.assertEqual(browser.started_accounts, [])

    async def test_inventory_failure_is_reported_to_remote_inventory(self):
        browser = FakeBrowserManager()
        completed = []

        async def acquire(_label):
            return "retry@icloud.com"

        async def confirm(_email):
            raise RuntimeError("confirmation failed")

        async def complete(email, success, message):
            completed.append((email, success, message))

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
            complete_email=complete,
        )
        manager.start(label="OpenAI 一键注册", headless=True)
        await asyncio.wait_for(manager._task, timeout=5)

        self.assertEqual(manager.snapshot()["status"], "failed")
        self.assertEqual(completed[0][0:2], ("retry@icloud.com", False))
        self.assertIn("confirmation failed", completed[0][2])

    async def test_concurrency_three_claims_three_accounts_and_starts_three_browsers(self):
        browser = FakeBrowserManager()
        inventory = [
            "first@icloud.com",
            "second@icloud.com",
            "third@icloud.com",
        ]
        released = []

        async def acquire(_label):
            return inventory.pop(0) if inventory else ""

        async def confirm(_email):
            return None

        async def release(email):
            released.append(email)

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
            release_email=release,
        )
        state = manager.start(
            label="OpenAI 一键注册",
            headless=False,
            concurrency=3,
        )
        self.assertEqual(state["requested"], 3)
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["claimed"], 3)
        self.assertEqual(len(snapshot["emails"]), 3)
        self.assertEqual(len(browser.started_accounts), 3)
        self.assertEqual(browser.start_options["concurrency"], 3)
        self.assertEqual(len({item["password"] for item in browser.started_accounts}), 3)
        self.assertEqual(len(released), 3)
        self.assertIn("成功 3/3", snapshot["message"])

    async def test_inventory_shortage_uses_available_minimum(self):
        browser = FakeBrowserManager()
        inventory = ["only@icloud.com"]
        released = []

        async def acquire(_label):
            return inventory.pop(0) if inventory else ""

        async def confirm(_email):
            return None

        async def release(email):
            released.append(email)

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
            release_email=release,
        )
        manager.start(
            label="OpenAI 一键注册",
            headless=True,
            concurrency=3,
        )
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["requested"], 3)
        self.assertEqual(snapshot["effectiveConcurrency"], 1)
        self.assertEqual(snapshot["claimed"], 1)
        self.assertEqual(len(browser.started_accounts), 1)
        self.assertEqual(browser.start_options["concurrency"], 1)
        self.assertIn("成功 1/1", snapshot["message"])
        self.assertTrue(
            any("从 3 自动降为 1" in item["message"] for item in snapshot["logs"])
        )
        self.assertEqual(released, ["only@icloud.com"])


if __name__ == "__main__":
    unittest.main()
