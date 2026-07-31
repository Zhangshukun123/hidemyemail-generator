import asyncio
import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from hidemyemail_generator.browser_tasks import (
    BrowserTaskManager,
    access_token_is_expired,
    load_account_record,
)
from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.openai_browser_bridge import safe_log_message


def token_with_exp(expires_at: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


class BrowserTaskHelperTests(unittest.TestCase):
    def test_access_token_expiration(self):
        now = time.time()
        self.assertFalse(
            access_token_is_expired(token_with_exp(int(now + 3600)), now=now)
        )
        self.assertTrue(
            access_token_is_expired(token_with_exp(int(now - 1)), now=now)
        )
        self.assertTrue(access_token_is_expired("not-a-jwt", now=now))

    def test_generated_password_is_redacted_from_worker_log(self):
        message = safe_log_message("账户需要密码步骤，已生成密码: Secret123!A7")
        self.assertNotIn("Secret123", message)
        self.assertIn("已安全保存", message)


class BrowserTaskManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_result_is_saved_without_exposing_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json, os\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "print(prefix + json.dumps({'type':'log','message':'started'}), flush=True)\n"
                "result = {'access_token':'at-test','session_json':json.dumps({'user':{'email':'one@icloud.com'}}),'storage_state_json':'{}'}\n"
                "print(prefix + json.dumps({'type':'result','result':result,'password':'Generated!A7'}), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="test-token",
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            state = manager.start(
                [{"email": "one@icloud.com", "password": ""}],
                headless=True,
                concurrency=1,
            )
            self.assertTrue(state["running"])
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["succeeded"], 1)
            self.assertNotIn("_result", snapshot["accounts"][0])
            self.assertNotIn("access_token", json.dumps(snapshot))

            record = load_account_record(db_file, "one@icloud.com")
            self.assertEqual(record["access_token"], "at-test")
            self.assertEqual(record["password"], "Generated!A7")
            conn = connect_db(str(db_file))
            try:
                row = conn.execute(
                    "SELECT state FROM addresses WHERE email = ?",
                    ("one@icloud.com",),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row["state"], "used")


if __name__ == "__main__":
    unittest.main()
