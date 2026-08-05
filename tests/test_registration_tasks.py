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

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
            save_password=save_password,
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

    async def test_inventory_claim_is_released_after_registration_failure(self):
        browser = FakeBrowserManager()
        released = []

        async def acquire(_label):
            return "retry@icloud.com"

        async def confirm(_email):
            raise RuntimeError("confirmation failed")

        async def release(email):
            released.append(email)

        manager = RegistrationTaskManager(
            browser_manager=browser,
            acquire_email=acquire,
            confirm_email=confirm,
            release_email=release,
        )
        manager.start(label="OpenAI 一键注册", headless=True)
        await asyncio.wait_for(manager._task, timeout=5)

        self.assertEqual(manager.snapshot()["status"], "failed")
        self.assertEqual(released, ["retry@icloud.com"])

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

    async def test_inventory_shortage_releases_partial_batch_without_browser(self):
        browser = FakeBrowserManager()
        inventory = ["first@icloud.com", "second@icloud.com"]
        released = []

        async def acquire(_label):
            return inventory.pop(0) if inventory else ""

        async def confirm(_email):
            raise AssertionError("partial inventory must not be confirmed")

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
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("需要 3 个，当前可领取 2 个", snapshot["message"])
        self.assertEqual(browser.started_accounts, [])
        self.assertEqual(released, ["first@icloud.com", "second@icloud.com"])


if __name__ == "__main__":
    unittest.main()
