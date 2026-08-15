import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from hidemyemail_generator.browser_tasks import BrowserTaskManager


class BrowserRuntimeLogTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self, root: Path) -> BrowserTaskManager:
        return BrowserTaskManager(
            target_project_dir=root,
            service_url="http://127.0.0.1:8765",
            worker_token="test-token",
            db_file=root / "hme.db",
            python_executable=root / "python.exe",
            bridge_file=root / "bridge.py",
        )

    def test_browser_log_entries_publish_stable_structured_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self._manager(Path(temp_dir))
            manager._state.update(
                id="browser-task-structured-log",
                browserEngine="camoufox",
                accounts=[
                    {
                        "email": "person@gmail.com",
                        "status": "running",
                        "latestLog": "",
                    }
                ],
            )

            manager._append_log(
                "[认证] 检测到 Google 登录要求；关闭当前浏览器并生成全新指纹",
                email="person@gmail.com",
            )
            manager._append_log(
                "工作器已进入密码页",
                email="person@gmail.com",
                source="browser_worker",
                event_type="page_transition",
            )

            first_log, second_log = manager.snapshot()["logs"]
            self.assertEqual(first_log["sequence"], 1)
            self.assertEqual(first_log["seq"], 1)
            self.assertEqual(first_log["taskId"], "browser-task-structured-log")
            self.assertEqual(first_log["originTaskId"], first_log["taskId"])
            self.assertEqual(first_log["originSequence"], first_log["sequence"])
            self.assertEqual(first_log["originSeq"], first_log["sequence"])
            self.assertEqual(first_log["source"], "browser_manager")
            self.assertEqual(first_log["eventType"], "log")
            self.assertEqual(first_log["email"], "person@gmail.com")
            self.assertEqual(first_log["stage"], "google_oauth")
            self.assertEqual(second_log["sequence"], 2)
            self.assertEqual(second_log["source"], "browser_worker")
            self.assertEqual(second_log["eventType"], "page_transition")

    async def test_browser_start_logs_non_sensitive_runtime_parameters(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            (root / "python.exe").write_text("", encoding="utf-8")
            (root / "bridge.py").write_text("", encoding="utf-8")
            manager = self._manager(root)

            with patch.object(
                manager,
                "_run_batch",
                new=AsyncMock(return_value=None),
            ):
                started = manager.start(
                    [
                        {
                            "email": "state@icloud.com",
                            "password": "NeverWriteThisPassword!7",
                        }
                    ],
                    headless=True,
                    concurrency=1,
                )
                await manager._batch_task

            parameter_logs = [
                entry
                for entry in started["logs"]
                if entry.get("eventType") == "task_parameters"
            ]
            self.assertEqual(len(parameter_logs), 1)
            self.assertEqual(parameter_logs[0]["taskId"], started["id"])
            self.assertEqual(parameter_logs[0]["source"], "browser_manager")
            self.assertIn("引擎 Camoufox", parameter_logs[0]["message"])
            self.assertIn("并发 1", parameter_logs[0]["message"])
            self.assertIn("窗口模式 无头", parameter_logs[0]["message"])
            self.assertNotIn("NeverWriteThisPassword!7", repr(started["logs"]))


if __name__ == "__main__":
    unittest.main()
