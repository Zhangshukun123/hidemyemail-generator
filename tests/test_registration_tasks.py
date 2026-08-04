import asyncio
import unittest

from hidemyemail_generator.registration_tasks import RegistrationTaskManager
from hidemyemail_generator.registration_tasks import generate_openai_password


class FakeBrowserManager:
    def __init__(self):
        self.started_accounts = []
        self.reset_count = 0
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

    def start(self, accounts, *, headless, concurrency):
        self.started_accounts = accounts
        self.state.update(status="running", running=True, headless=headless)
        return self.snapshot()

    async def wait(self):
        await asyncio.sleep(0)
        self.state.update(status="completed", running=False, succeeded=1)
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

    async def test_generates_email_then_runs_browser_registration(self):
        browser = FakeBrowserManager()
        events = []

        async def generate(label):
            events.append(("generate", label))
            return "new-alias@icloud.com"

        async def confirm(email):
            events.append(("confirm", email))

        manager = RegistrationTaskManager(
            browser_manager=browser,
            generate_email=generate,
            confirm_email=confirm,
        )
        state = manager.start(label="OpenAI 一键注册", headless=True)
        self.assertTrue(state["running"])
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["phase"], "completed")
        self.assertEqual(snapshot["email"], "new-alias@icloud.com")
        self.assertEqual(events[0], ("generate", "OpenAI 一键注册"))
        self.assertEqual(events[1], ("confirm", "new-alias@icloud.com"))
        self.assertEqual(browser.started_accounts[0]["email"], "new-alias@icloud.com")
        self.assertFalse(browser.started_accounts[0]["enable_2fa"])
        self.assertFalse(browser.started_accounts[0]["ensure_password"])
        self.assertEqual(browser.started_accounts[0]["password"], "")
        self.assertIn("未设置密码和 2FA", snapshot["message"])
        self.assertIn("未执行账号验证", snapshot["message"])
        self.assertTrue(browser.state["headless"])
        self.assertEqual(browser.reset_count, 1)


if __name__ == "__main__":
    unittest.main()
