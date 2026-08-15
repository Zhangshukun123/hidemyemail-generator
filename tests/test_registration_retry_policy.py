import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from hidemyemail_generator.browser_tasks import (
    BrowserTaskManager,
    load_account_record,
)
from hidemyemail_generator.registration_retry_policy import (
    RegistrationRetryContext,
    RegistrationRetryPolicy,
    build_reliability_report,
)


async def _no_retry_delay(_seconds: float) -> None:
    await asyncio.sleep(0)


class RegistrationRetryPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = RegistrationRetryPolicy(
            max_retries=2,
            retry_delays=(0, 0),
        )

    def test_browser_crash_before_verification_is_retryable(self):
        decision = self.policy.decide(
            RegistrationRetryContext(
                error=(
                    "Page.goto: Target page, context or browser has been closed"
                ),
                stage="openai_auth",
                registration_chain={"currentCode": "email_submitted"},
            )
        )

        self.assertTrue(decision.retryable)
        self.assertEqual(decision.reason_code, "browser_closed")
        self.assertFalse(decision.rotate_proxy)

    def test_verification_page_is_a_hard_replay_boundary(self):
        decision = self.policy.decide(
            RegistrationRetryContext(
                error="browser disconnected",
                stage="email_verification",
                registration_chain={"currentCode": "verification_page"},
            )
        )

        self.assertFalse(decision.retryable)
        self.assertEqual(decision.reason_code, "unsafe_page_stage")

    def test_chain_ledger_blocks_replay_even_when_page_state_is_stale(self):
        decision = self.policy.decide(
            RegistrationRetryContext(
                error="ERR_CONNECTION_RESET",
                stage="openai_auth",
                registration_chain={
                    "currentCode": "email_responded",
                    "steps": [
                        {
                            "code": "verification_page",
                            "status": "completed",
                        }
                    ],
                },
            )
        )

        self.assertFalse(decision.retryable)
        self.assertEqual(decision.reason_code, "unsafe_replay_boundary")

    def test_durable_result_and_business_errors_are_terminal(self):
        durable = self.policy.decide(
            RegistrationRetryContext(
                error="ERR_CONNECTION_RESET",
                stage="openai_auth",
                result_received=True,
            )
        )
        rejected = self.policy.decide(
            RegistrationRetryContext(
                error="HTTP 403 invalid credential",
                stage="openai_auth",
            )
        )

        self.assertEqual(durable.reason_code, "durable_result_received")
        self.assertFalse(durable.retryable)
        self.assertEqual(rejected.reason_code, "terminal_failure")
        self.assertFalse(rejected.retryable)

    def test_retry_limit_is_bounded(self):
        decision = self.policy.decide(
            RegistrationRetryContext(
                error="ERR_CONNECTION_RESET",
                stage="openai_auth",
                retry_count=2,
            )
        )

        self.assertFalse(decision.retryable)
        self.assertEqual(decision.reason_code, "retry_limit")

    def test_exact_home_entry_stall_is_safely_retryable_before_verification(self):
        decision = self.policy.decide(
            RegistrationRetryContext(
                error=(
                    "ChatGPT 首页免费注册已有网络请求，"
                    "但页面未在限定时间内完成变化；"
                    "为避免重复提交已停止补点"
                ),
                stage="openai_auth",
                registration_chain={"currentCode": "registration_entry_ready"},
            )
        )

        self.assertTrue(decision.retryable)
        self.assertEqual(decision.reason_code, "navigation_stalled")
        self.assertFalse(decision.rotate_proxy)

    def test_home_entry_stall_does_not_cross_verification_boundary(self):
        error = (
            "ChatGPT 首页免费注册已有注册入口网络请求，"
            "但页面未在限定时间内完成变化"
        )
        unsafe_stage = self.policy.decide(
            RegistrationRetryContext(
                error=error,
                stage="email_verification",
                registration_chain={"currentCode": "verification_page"},
            )
        )
        unsafe_ledger = self.policy.decide(
            RegistrationRetryContext(
                error=error,
                stage="openai_auth",
                registration_chain={
                    "currentCode": "registration_entry_ready",
                    "steps": [
                        {"code": "verification_page", "status": "completed"}
                    ],
                },
            )
        )

        self.assertFalse(unsafe_stage.retryable)
        self.assertEqual(unsafe_stage.reason_code, "unsafe_page_stage")
        self.assertFalse(unsafe_ledger.retryable)
        self.assertEqual(unsafe_ledger.reason_code, "unsafe_replay_boundary")

    def test_bounded_home_click_exhaustion_is_navigation_stalled(self):
        decision = self.policy.decide(
            RegistrationRetryContext(
                error=(
                    "ChatGPT 首页免费注册最多点击 5 次后，"
                    "页面未在限定时间内完成变化且未检测到注册入口响应"
                ),
                stage="openai_auth",
                registration_chain={"currentCode": "registration_entry_ready"},
            )
        )

        self.assertTrue(decision.retryable)
        self.assertEqual(decision.reason_code, "navigation_stalled")

    def test_unrelated_background_stall_phrase_is_not_retryable(self):
        decision = self.policy.decide(
            RegistrationRetryContext(
                error="后台刷新未在限定时间内完成变化",
                stage="openai_auth",
                registration_chain={"currentCode": "registration_entry_ready"},
            )
        )

        self.assertFalse(decision.retryable)
        self.assertEqual(decision.reason_code, "not_retryable")

    def test_reliability_gate_requires_at_least_98_percent_of_100(self):
        passing = build_reliability_report(98, 2)
        failing = build_reliability_report(97, 3)
        collecting = build_reliability_report(49, 1)

        self.assertEqual(passing.gate, "pass")
        self.assertEqual(passing.success_rate_percent, 98.0)
        self.assertEqual(failing.gate, "fail")
        self.assertEqual(collecting.gate, "collecting")


class BrowserTaskRetryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _manager(
        root: Path,
        bridge_source: str,
        *,
        max_retries: int = 2,
        failure_circuit_threshold: int = 3,
    ) -> BrowserTaskManager:
        target = root / "target"
        target.mkdir()
        (target / "app_backend.py").write_text(
            "# synthetic registration runtime\n",
            encoding="utf-8",
        )
        bridge = root / "fake_bridge.py"
        bridge.write_text(bridge_source, encoding="utf-8")
        return BrowserTaskManager(
            target_project_dir=target,
            service_url="http://127.0.0.1:8765",
            worker_token="synthetic-worker-token",
            db_file=root / "hme.db",
            python_executable=Path(sys.executable),
            bridge_file=bridge,
            registration_retry_policy=RegistrationRetryPolicy(
                max_retries=max_retries,
                retry_delays=(0, 0),
            ),
            retry_sleep=_no_retry_delay,
            failure_circuit_threshold=failure_circuit_threshold,
        )

    async def test_transient_browser_failure_recovers_once_and_saves(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self._manager(
                root,
                """
import json
import os
import sys
from pathlib import Path

prefix = "HME_BROWSER_EVENT:"
attempt = int(os.environ.get("HME_BROWSER_WORKER_ATTEMPT", "0"))
counter = Path(__file__).with_name("attempts.txt")
counter.write_text(counter.read_text() + "x" if counter.exists() else "x")
if attempt == 0:
    state = {"stage": "openai_auth", "code": "email", "nextAction": "submit"}
    print(prefix + json.dumps({"type": "page_state", "state": state}), flush=True)
    error = "Page.goto: Target page, context or browser has been closed"
    print(prefix + json.dumps({"type": "error", "error": error}), flush=True)
    sys.exit(1)
result = {"access_token": "at-synthetic", "session_json": "{}"}
print(prefix + json.dumps({"type": "result", "result": result}), flush=True)
""",
            )
            manager.start(
                [{"email": "recover@icloud.com", "password": ""}],
                headless=True,
                concurrency=1,
            )
            snapshot = await asyncio.wait_for(manager.wait(), timeout=15)

            account = snapshot["accounts"][0]
            self.assertEqual(snapshot["succeeded"], 1)
            self.assertEqual(snapshot["failed"], 0)
            self.assertEqual(snapshot["recovered"], 1)
            self.assertEqual(snapshot["transientRetries"], 1)
            self.assertEqual(account["transientRetries"], 1)
            self.assertEqual(account["workerAttempts"], 2)
            self.assertEqual(account["retryState"], "recovered")
            self.assertEqual(
                account["retryHistory"][0]["reasonCode"], "browser_closed"
            )
            self.assertEqual(
                (root / "attempts.txt").read_text(encoding="utf-8"), "xx"
            )
            saved = load_account_record(root / "hme.db", "recover@icloud.com")
            diagnostics = saved["registration_diagnostics"]
            self.assertEqual(diagnostics["transient_retries"], 1)
            self.assertEqual(diagnostics["worker_attempts"], 2)
            self.assertTrue(diagnostics["recovered_after_retry"])

    async def test_latest_home_entry_stall_retries_in_clean_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self._manager(
                root,
                """
import json
import os
import sys
from pathlib import Path

prefix = "HME_BROWSER_EVENT:"
attempt = int(os.environ.get("HME_BROWSER_WORKER_ATTEMPT", "0"))
counter = Path(__file__).with_name("attempts.txt")
counter.write_text(counter.read_text() + "x" if counter.exists() else "x")
if attempt == 0:
    page = {"stage": "openai_auth", "code": "home"}
    chain = {
        "currentCode": "registration_entry_ready",
        "registrationCreated": False,
        "sessionReady": False,
        "passwordConfirmed": False,
        "twoFactorEnabled": False,
    }
    error = (
        "ChatGPT 首页免费注册已有网络请求，"
        "但页面未在限定时间内完成变化；"
        "为避免重复提交已停止补点"
    )
    print(prefix + json.dumps({"type": "page_state", "state": page}), flush=True)
    print(prefix + json.dumps({"type": "registration_chain", "state": chain}), flush=True)
    print(prefix + json.dumps({"type": "error", "error": error}, ensure_ascii=False), flush=True)
    sys.exit(1)
result = {"access_token": "at-recovered", "session_json": "{}"}
print(prefix + json.dumps({"type": "result", "result": result}), flush=True)
""",
            )
            manager.start(
                [{"email": "home-stall@icloud.com", "password": ""}],
                headless=True,
                concurrency=1,
            )
            snapshot = await asyncio.wait_for(manager.wait(), timeout=15)

            account = snapshot["accounts"][0]
            self.assertEqual(snapshot["succeeded"], 1)
            self.assertEqual(snapshot["transientRetries"], 1)
            self.assertEqual(account["workerAttempts"], 2)
            self.assertEqual(account["retryState"], "recovered")
            self.assertEqual(
                account["retryHistory"][0]["reasonCode"],
                "navigation_stalled",
            )
            self.assertEqual(
                (root / "attempts.txt").read_text(encoding="utf-8"),
                "xx",
            )

    async def test_invalid_otp_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self._manager(
                root,
                """
import json
import sys
from pathlib import Path

prefix = "HME_BROWSER_EVENT:"
counter = Path(__file__).with_name("attempts.txt")
counter.write_text(counter.read_text() + "x" if counter.exists() else "x")
state = {"stage": "email_verification", "code": "email_verification"}
print(prefix + json.dumps({"type": "page_state", "state": state}), flush=True)
print(prefix + json.dumps({"type": "error", "error": "验证码无效"}), flush=True)
sys.exit(1)
""",
            )
            manager.start(
                [{"email": "otp@icloud.com", "password": ""}],
                headless=True,
                concurrency=1,
            )
            snapshot = await asyncio.wait_for(manager.wait(), timeout=15)

            account = snapshot["accounts"][0]
            self.assertEqual(snapshot["failed"], 1)
            self.assertEqual(account["workerAttempts"], 1)
            self.assertEqual(account["transientRetries"], 0)
            self.assertEqual(account["terminalReasonCode"], "unsafe_page_stage")
            self.assertEqual(
                (root / "attempts.txt").read_text(encoding="utf-8"), "x"
            )

    async def test_circuit_breaker_skips_remaining_identical_failures(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self._manager(
                root,
                """
import json
import sys

prefix = "HME_BROWSER_EVENT:"
error = "ERR_CONNECTION_RESET"
print(prefix + json.dumps({"type": "error", "error": error}), flush=True)
sys.exit(1)
""",
                max_retries=1,
                failure_circuit_threshold=1,
            )
            manager.start(
                [
                    {"email": f"circuit-{index}@icloud.com", "password": ""}
                    for index in range(4)
                ],
                headless=True,
                concurrency=1,
            )
            snapshot = await asyncio.wait_for(manager.wait(), timeout=45)

            self.assertTrue(snapshot["circuitOpen"])
            self.assertEqual(snapshot["circuitReason"], "network_interrupted")
            self.assertEqual(snapshot["failed"], 1)
            self.assertEqual(snapshot["skipped"], 3)
            self.assertEqual(snapshot["completed"], 4)

    async def test_stop_reaps_a_running_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self._manager(
                root,
                """
import time
time.sleep(60)
""",
            )
            manager.start(
                [{"email": "cancel@icloud.com", "password": ""}],
                headless=True,
                concurrency=1,
            )
            for _ in range(100):
                if manager._processes:
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(manager._processes)

            snapshot = await asyncio.wait_for(manager.stop(), timeout=12)

            self.assertEqual(snapshot["status"], "cancelled")
            self.assertFalse(manager._processes)

    async def test_batch_cancellation_reaps_a_running_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = self._manager(
                root,
                """
import time
time.sleep(60)
""",
            )
            manager.start(
                [{"email": "cancel-task@icloud.com", "password": ""}],
                headless=True,
                concurrency=1,
            )
            for _ in range(100):
                if manager._processes:
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(manager._processes)

            task = manager._batch_task
            self.assertIsNotNone(task)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)

            self.assertFalse(manager._processes)
            self.assertEqual(manager.snapshot()["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
