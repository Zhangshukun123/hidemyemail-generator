import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from hidemyemail_generator.account_verifier import (
    AccountVerificationManager,
    removed_account_emails,
)
from hidemyemail_generator.browser_tasks import load_account_record
from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.openai_account_check_bridge import confirmed_invalid
from hidemyemail_generator.webapp import _browser_email_items


def save_record(db_file: Path, email: str, token: str) -> None:
    conn = connect_db(str(db_file))
    try:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            (
                f"gpt_account:{email}",
                json.dumps(
                    {
                        "email": email,
                        "password": "Secret!A7",
                        "access_token": token,
                        "session": {"accessToken": token},
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()


class AccountVerificationManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_classifies_valid_accounts_and_removes_invalid_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            bridge = root / "fake_check_bridge.py"
            bridge.write_text(
                "import json, os\n"
                "token = os.environ['HME_OPENAI_ACCESS_TOKEN']\n"
                "status = {'at-plus':'plus','at-free':'free','at-bad':'invalid'}[token]\n"
                "print('HME_VERIFY_EVENT:' + json.dumps({'status':status,'detail':'test result'}), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            save_record(db_file, "plus@icloud.com", "at-plus")
            save_record(db_file, "free@icloud.com", "at-free")
            save_record(db_file, "bad@icloud.com", "at-bad")

            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            state = manager.start(concurrency=3)
            self.assertTrue(state["running"])
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["plus"], 1)
            self.assertEqual(snapshot["free"], 1)
            self.assertEqual(snapshot["deleted"], 1)
            self.assertNotIn("access_token", json.dumps(snapshot))
            self.assertEqual(
                load_account_record(db_file, "plus@icloud.com")["account_type"],
                "plus",
            )
            self.assertEqual(
                load_account_record(db_file, "free@icloud.com")["account_type"],
                "free",
            )
            self.assertEqual(load_account_record(db_file, "bad@icloud.com"), {})
            self.assertIn("bad@icloud.com", removed_account_emails(db_file))

            identities = [
                {"hme": "plus@icloud.com", "anonymousId": "plus", "isActive": True},
                {"hme": "free@icloud.com", "anonymousId": "free", "isActive": True},
                {"hme": "bad@icloud.com", "anonymousId": "bad", "isActive": True},
            ]
            items = _browser_email_items(db_file, identities)
            self.assertEqual({item["email"] for item in items}, {"plus@icloud.com", "free@icloud.com"})


class InvalidConfirmationTests(unittest.TestCase):
    def test_requires_two_independent_authorization_failures(self):
        self.assertFalse(confirmed_invalid("/backend-api/me: HTTP 403"))
        self.assertTrue(
            confirmed_invalid(
                "/backend-api/accounts/check: HTTP 401; /backend-api/me: HTTP 403"
            )
        )


if __name__ == "__main__":
    unittest.main()
