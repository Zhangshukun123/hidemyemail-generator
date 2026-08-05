from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.scheduled_generation import ScheduledGenerationManager
from hidemyemail_generator.webapp import create_app


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


class ScheduledGenerationManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_file = Path(self.temp_dir.name) / "hidemyemail.db"
        self.clock = MutableClock()
        self.calls: list[tuple[str, int]] = []

        async def generate_batch(label: str, count: int) -> dict:
            self.calls.append((label, count))
            return {
                "ok": True,
                "emails": [f"inventory-{len(self.calls)}-{index}@icloud.com" for index in range(count)],
                "error": None,
            }

        self.generate_batch = generate_batch
        self.manager = ScheduledGenerationManager(
            db_file=self.db_file,
            generate_batch=self.generate_batch,
            clock=self.clock,
        )

    async def asyncTearDown(self):
        await self.manager.close()
        self.temp_dir.cleanup()

    async def test_first_batch_waits_a_full_hour_and_only_generates_inventory(self):
        state = await self.manager.initialize()

        self.assertTrue(state["enabled"])
        self.assertEqual(state["batchSize"], 5)
        self.assertEqual(state["intervalSeconds"], 3600)
        self.assertEqual(state["secondsUntilNext"], 3600)
        self.assertEqual(self.calls, [])

        self.clock.advance(seconds=3599)
        self.assertFalse(await self.manager.tick())
        self.assertEqual(self.calls, [])

        self.clock.advance(seconds=1)
        self.assertTrue(await self.manager.tick())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], 5)
        self.assertIn("Hourly inventory", self.calls[0][0])
        state = self.manager.snapshot()
        self.assertEqual(state["totalRuns"], 1)
        self.assertEqual(state["totalGenerated"], 5)
        self.assertEqual(state["secondsUntilNext"], 3600)
        self.assertIn("未启动注册", state["logs"][-1]["message"])

    async def test_persisted_timer_survives_restart_without_resetting(self):
        first = await self.manager.initialize()
        original_next_run = first["nextRunAt"]
        self.clock.advance(minutes=30)

        restored = ScheduledGenerationManager(
            db_file=self.db_file,
            generate_batch=self.generate_batch,
            clock=self.clock,
        )
        try:
            state = await restored.initialize()
        finally:
            await restored.close()

        self.assertEqual(state["nextRunAt"], original_next_run)
        self.assertEqual(state["secondsUntilNext"], 1800)
        self.assertEqual(self.calls, [])

    async def test_reenable_starts_a_new_full_hour_timer(self):
        await self.manager.initialize()
        self.clock.advance(minutes=20)
        paused = await self.manager.configure(enabled=False)
        self.assertFalse(paused["enabled"])
        self.assertIsNone(paused["secondsUntilNext"])

        self.clock.advance(minutes=10)
        resumed = await self.manager.configure(enabled=True)
        self.assertTrue(resumed["enabled"])
        self.assertEqual(resumed["secondsUntilNext"], 3600)

        self.clock.advance(seconds=3599)
        self.assertFalse(await self.manager.tick())
        self.clock.advance(seconds=1)
        self.assertTrue(await self.manager.tick())
        self.assertEqual(len(self.calls), 1)

    async def test_failure_is_logged_and_next_attempt_waits_an_hour(self):
        async def fail_batch(_label: str, _count: int) -> dict:
            raise RuntimeError("iCloud temporary failure")

        manager = ScheduledGenerationManager(
            db_file=self.db_file,
            generate_batch=fail_batch,
            clock=self.clock,
        )
        await manager.initialize()
        self.clock.advance(hours=1)
        try:
            self.assertTrue(await manager.tick())
            state = manager.snapshot()
        finally:
            await manager.close()

        self.assertEqual(state["lastOutcome"], "failed")
        self.assertEqual(state["secondsUntilNext"], 3600)
        self.assertIn("iCloud temporary failure", state["logs"][-1]["message"])


class ScheduledGenerationEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(base_dir=Path(self.temp_dir.name))
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_status_starts_enabled_but_does_not_run_immediately(self):
        response = await self.client.get("/api/scheduled-generation/status")
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["enabled"])
        self.assertFalse(payload["running"])
        self.assertEqual(payload["batchSize"], 5)
        self.assertGreaterEqual(payload["secondsUntilNext"], 3599)
        self.assertEqual(payload["totalRuns"], 0)
        self.assertFalse(self.app["registration_manager"].snapshot()["running"])

    async def test_pause_and_resume_requires_token_and_restarts_timer(self):
        denied = await self.client.post(
            "/api/scheduled-generation/config", json={"enabled": False}
        )
        self.assertEqual(denied.status, 403)

        paused = await self.client.post(
            "/api/scheduled-generation/config",
            json={"enabled": False},
            headers={"X-Local-Token": self.app["local_token"]},
        )
        self.assertFalse((await paused.json())["enabled"])

        resumed = await self.client.post(
            "/api/scheduled-generation/config",
            json={"enabled": True},
            headers={"X-Local-Token": self.app["local_token"]},
        )
        payload = await resumed.json()
        self.assertTrue(payload["enabled"])
        self.assertGreaterEqual(payload["secondsUntilNext"], 3599)
        self.assertEqual(payload["totalRuns"], 0)


if __name__ == "__main__":
    unittest.main()
