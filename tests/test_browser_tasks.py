import asyncio
import base64
import json
import os
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
from hidemyemail_generator.openai_browser_bridge import (
    _configure_camoufox_runtime_cache,
    _fontconfig_generator_with_home,
    resilient_force_fill_locator,
    safe_log_message,
)


def token_with_exp(expires_at: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


class BrowserTaskHelperTests(unittest.TestCase):
    def test_camoufox_runtime_cache_is_writable_and_uses_xdg(self):
        previous_cache = os.environ.get("XDG_CACHE_HOME")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                runtime_home = Path(temp_dir)
                runtime_cache = _configure_camoufox_runtime_cache(runtime_home)

                self.assertEqual(runtime_cache, runtime_home / ".cache")
                self.assertEqual(os.environ["XDG_CACHE_HOME"], str(runtime_cache))
                self.assertTrue((runtime_cache / "fontconfig").is_dir())
                self.assertTrue(
                    (runtime_cache / "camoufox" / "fontconfig").is_dir()
                )
        finally:
            if previous_cache is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = previous_cache

    def test_fontconfig_generator_uses_writable_home_and_restores_environment(self):
        observed = []

        def generator(fontconfig_path):
            observed.append((fontconfig_path, os.environ.get("HOME")))
            return "runtime-fonts.conf"

        runtime_home = Path("/tmp/hidemyemail-camoufox-test")
        redirected = _fontconfig_generator_with_home(generator, runtime_home)
        previous_home = os.environ.get("HOME")
        os.environ["HOME"] = "original-home"
        try:
            self.assertEqual(redirected("bundled-fonts"), "runtime-fonts.conf")
            self.assertEqual(
                observed,
                [("bundled-fonts", str(runtime_home))],
            )
            self.assertEqual(os.environ.get("HOME"), "original-home")
        finally:
            if previous_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous_home

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

    def test_password_fill_falls_back_to_native_input_event(self):
        class Worker:
            def __init__(self):
                self.logs = []

            def log(self, message):
                self.logs.append(message)

        class Locator:
            def __init__(self):
                self.value = ""

            def click(self, **_kwargs):
                return None

            def fill(self, *_args, **_kwargs):
                raise RuntimeError("controlled input rejected fill")

            def evaluate(self, _script, value):
                self.value = value

            def input_value(self, **_kwargs):
                return self.value

        worker = Worker()
        locator = Locator()

        self.assertTrue(resilient_force_fill_locator(worker, locator, "Strong!Pass123"))
        self.assertEqual(locator.value, "Strong!Pass123")
        self.assertEqual(worker.logs, ["[认证] 已使用兼容输入方式填写密码"])

    def test_password_fill_falls_back_to_keyboard_input(self):
        class Worker:
            def __init__(self):
                self.logs = []

            def log(self, message):
                self.logs.append(message)

        class Locator:
            def __init__(self):
                self.value = ""

            def click(self, **_kwargs):
                return None

            def fill(self, *_args, **_kwargs):
                return None

            def evaluate(self, *_args):
                return None

            def press(self, key, **_kwargs):
                if key == "Backspace":
                    self.value = ""

            def type(self, value, **_kwargs):
                self.value += value

            def input_value(self, **_kwargs):
                return self.value

        worker = Worker()
        locator = Locator()

        self.assertTrue(resilient_force_fill_locator(worker, locator, "Strong!Pass123"))
        self.assertEqual(locator.value, "Strong!Pass123")
        self.assertEqual(worker.logs, ["[认证] 已使用键盘输入方式填写密码"])


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

    async def test_partial_two_factor_state_is_saved_before_activation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                "import json, sys\n"
                "prefix = 'HME_BROWSER_EVENT:'\n"
                "result = {'access_token':'at-partial','session_json':'{}'}\n"
                "print(prefix + json.dumps({'type':'account_registered','result':result,'password':'Strong!Pass123'}), flush=True)\n"
                "two_factor = {'enabled':False,'status':'enrolled','secret':'JBSWY3DPEHPK3PXP','factor_id':'factor-1','session_id':'session-1'}\n"
                "print(prefix + json.dumps({'type':'two_factor_enrolled','two_factor':two_factor}), flush=True)\n"
                "print(prefix + json.dumps({'type':'error','error':'activation failed','password':'Strong!Pass123'}), flush=True)\n"
                "sys.exit(1)\n",
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
            manager.start(
                [
                    {
                        "email": "partial@icloud.com",
                        "password": "Strong!Pass123",
                        "enable_2fa": True,
                    }
                ],
                headless=True,
                concurrency=1,
            )
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["failed"], 1)
            self.assertNotIn("JBSWY3D", json.dumps(snapshot))
            record = load_account_record(db_file, "partial@icloud.com")
            self.assertEqual(record["access_token"], "at-partial")
            self.assertEqual(record["two_factor"]["status"], "enrolled")
            self.assertEqual(record["two_factor"]["secret"], "JBSWY3DPEHPK3PXP")


if __name__ == "__main__":
    unittest.main()
