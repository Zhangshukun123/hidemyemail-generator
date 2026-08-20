import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from hidemyemail_generator.browser_tasks import BrowserTaskManager


class RegistrationRequestActivityTransportTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_registration_entry_evidence_is_exposed_to_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# synthetic runtime\n",
                encoding="utf-8",
            )
            bridge = root / "fake_bridge.py"
            bridge.write_text(
                """
import json

prefix = "HME_BROWSER_EVENT:"
activity = {
    "requestCount": 7,
    "responseCount": 6,
    "failedCount": 1,
    "loadCount": 2,
    "entryRequestCount": 1,
    "entryResponseCount": 1,
    "entryFailedCount": 0,
    "lastEvent": "response",
    "lastMethod": "GET",
    "lastRoute": "chatgpt.com/api/auth/session",
    "lastResourceType": "fetch",
    "lastStatus": 200,
    "lastActivityAt": "2026-08-15T08:14:48+00:00",
    "lastEntryEvent": "response",
    "lastEntryMethod": "GET",
    "lastEntryRoute": "auth.openai.com/create-account",
    "lastEntryResourceType": "document",
    "lastEntryStatus": 302,
    "lastEntryAt": "2026-08-15T08:14:47+00:00",
}
chain = {
    "status": "running",
    "currentCode": "registration_entry_ready",
    "currentStep": "注册入口已有响应",
    "currentValue": "等待邮箱界面",
    "currentCompleted": False,
    "nextCode": "registration_entry_ready",
    "canAdvance": False,
    "steps": [],
    "completedSteps": [],
    "nextAction": "识别并输入邮箱",
    "requestActivity": activity,
}
event = {"type": "registration_chain", "state": chain}
print(prefix + json.dumps(event, ensure_ascii=False), flush=True)
result = {"type": "result", "result": {"access_token": "at-test"}}
print(prefix + json.dumps(result), flush=True)
""",
                encoding="utf-8",
            )
            manager = BrowserTaskManager(
                target_project_dir=target,
                service_url="http://127.0.0.1:8765",
                worker_token="synthetic-token",
                db_file=root / "hme.db",
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )

            manager.start(
                [{"email": "telemetry@icloud.com"}],
                headless=True,
                concurrency=1,
            )
            await asyncio.wait_for(manager._batch_task, timeout=10)
            activity = manager.snapshot()["registrationChain"][
                "requestActivity"
            ]

            self.assertEqual(activity["entryRequestCount"], 1)
            self.assertEqual(activity["entryResponseCount"], 1)
            self.assertEqual(activity["lastEntryEvent"], "response")
            self.assertEqual(
                activity["lastEntryRoute"],
                "auth.openai.com/create-account",
            )
            self.assertEqual(activity["lastEntryResourceType"], "document")
            self.assertEqual(activity["lastEntryStatus"], 302)
            self.assertEqual(activity["lastResourceType"], "fetch")


if __name__ == "__main__":
    unittest.main()
