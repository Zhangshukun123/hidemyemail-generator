import asyncio
import unittest

from hidemyemail_generator.registration_tasks import RegistrationTaskManager
from hidemyemail_generator.registration_tasks import generate_openai_password


class FakeBrowserManager:
    def __init__(self, *, password_confirmed=True, two_factor_enabled=True):
        self.started_accounts = []
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

    def start(self, accounts, *, headless, concurrency):
        self.started_accounts = accounts
        self.state.update(status="running", running=True, headless=headless)
        return self.snapshot()

    async def wait(self):
        await asyncio.sleep(0)
        self.state.update(
            status="completed",
            running=False,
            succeeded=1,
            accounts=[
                {
                    "passwordConfirmed": self.password_confirmed,
                    "twoFactorEnabled": self.two_factor_enabled,
                }
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

    async def test_generates_email_then_runs_browser_registration(self):
        browser = FakeBrowserManager()
        events = []

        async def generate(label):
            events.append(("generate", label))
            return "new-alias@icloud.com"

        async def confirm(email):
            events.append(("confirm", email))

        async def save_password(email, password):
            events.append(("save_password", email, password))

        manager = RegistrationTaskManager(
            browser_manager=browser,
            generate_email=generate,
            confirm_email=confirm,
            save_password=save_password,
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
        self.assertEqual(events[2][0:2], ("save_password", "new-alias@icloud.com"))
        self.assertEqual(browser.started_accounts[0]["email"], "new-alias@icloud.com")
        self.assertTrue(browser.started_accounts[0]["enable_2fa"])
        self.assertTrue(browser.started_accounts[0]["ensure_password"])
        self.assertFalse(browser.started_accounts[0]["force_reset_password"])
        self.assertEqual(browser.started_accounts[0]["password"], events[2][2])
        self.assertIn("唯一密码已设置", snapshot["message"])
        self.assertIn("2FA 已开启", snapshot["message"])
        self.assertIn("未执行账号验证", snapshot["message"])
        self.assertTrue(browser.state["headless"])
        self.assertEqual(browser.reset_count, 1)

    async def test_skips_two_factor_when_password_was_not_confirmed(self):
        browser = FakeBrowserManager(
            password_confirmed=False,
            two_factor_enabled=False,
        )

        async def generate(_label):
            return "pending-password@icloud.com"

        async def confirm(_email):
            return None

        manager = RegistrationTaskManager(
            browser_manager=browser,
            generate_email=generate,
            confirm_email=confirm,
        )
        manager.start(label="OpenAI 一键注册", headless=True)
        await asyncio.wait_for(manager._task, timeout=5)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["status"], "completed")
        self.assertIn("已跳过 2FA（密码未成功）", snapshot["message"])


if __name__ == "__main__":
    unittest.main()
