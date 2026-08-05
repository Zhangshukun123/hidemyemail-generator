import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.inbox import connect_db, upsert_address
from hidemyemail_generator.registration_inventory import (
    available_generated_inventory_count,
    claim_generated_inventory_email,
    clear_generated_inventory_claims,
    release_generated_inventory_email,
)
from hidemyemail_generator.webapp import create_app


class RegistrationInventoryTests(unittest.TestCase):
    def test_claims_only_oldest_generated_unused_address(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            conn = connect_db(str(db_file))
            try:
                upsert_address(
                    conn,
                    "manual@icloud.com",
                    state="unused",
                    source="inbox",
                )
                upsert_address(
                    conn,
                    "newer@icloud.com",
                    state="unused",
                    source="generated",
                )
                upsert_address(
                    conn,
                    "oldest@icloud.com",
                    state="unused",
                    source="generated",
                )
                upsert_address(
                    conn,
                    "used@icloud.com",
                    state="used",
                    source="generated",
                )
                conn.execute(
                    "UPDATE addresses SET created_at = ? WHERE email = ?",
                    ("2026-08-05T02:00:00+00:00", "newer@icloud.com"),
                )
                conn.execute(
                    "UPDATE addresses SET created_at = ? WHERE email = ?",
                    ("2026-08-05T01:00:00+00:00", "oldest@icloud.com"),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(
                claim_generated_inventory_email(db_file),
                "oldest@icloud.com",
            )
            self.assertEqual(
                claim_generated_inventory_email(db_file),
                "newer@icloud.com",
            )

    def test_release_makes_failed_claim_available_again(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            conn = connect_db(str(db_file))
            try:
                upsert_address(
                    conn,
                    "retry@icloud.com",
                    state="unused",
                    source="generated",
                )
            finally:
                conn.close()

            self.assertEqual(
                claim_generated_inventory_email(db_file),
                "retry@icloud.com",
            )
            self.assertEqual(available_generated_inventory_count(db_file), 0)
            self.assertEqual(claim_generated_inventory_email(db_file), "")
            release_generated_inventory_email(db_file, "retry@icloud.com")
            self.assertEqual(available_generated_inventory_count(db_file), 1)
            self.assertEqual(
                claim_generated_inventory_email(db_file),
                "retry@icloud.com",
            )

    def test_concurrent_claims_receive_different_inventory_addresses(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            conn = connect_db(str(db_file))
            try:
                for email in ("first@icloud.com", "second@icloud.com"):
                    upsert_address(
                        conn,
                        email,
                        state="unused",
                        source="generated",
                    )
            finally:
                conn.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                claimed = list(
                    pool.map(
                        lambda _index: claim_generated_inventory_email(db_file),
                        range(2),
                    )
                )

            self.assertEqual(set(claimed), {"first@icloud.com", "second@icloud.com"})
            self.assertEqual(claim_generated_inventory_email(db_file), "")

    def test_skips_inventory_address_that_already_has_a_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            conn = connect_db(str(db_file))
            try:
                upsert_address(
                    conn,
                    "registered@icloud.com",
                    state="unused",
                    source="generated",
                )
                upsert_address(
                    conn,
                    "available@icloud.com",
                    state="unused",
                    source="generated",
                )
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    (
                        "gpt_account:registered@icloud.com",
                        json.dumps({"access_token": "existing-token"}),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(
                claim_generated_inventory_email(db_file),
                "available@icloud.com",
            )

    def test_startup_cleanup_removes_abandoned_claims(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            conn = connect_db(str(db_file))
            try:
                upsert_address(
                    conn,
                    "abandoned@icloud.com",
                    state="unused",
                    source="generated",
                )
            finally:
                conn.close()
            claim_generated_inventory_email(db_file)

            self.assertEqual(clear_generated_inventory_claims(db_file), 1)
            self.assertEqual(
                claim_generated_inventory_email(db_file),
                "abandoned@icloud.com",
            )


class RegistrationInventoryWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_webapp_registration_manager_acquires_from_generated_inventory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            conn = connect_db(str(app["db_file"]))
            try:
                upsert_address(
                    conn,
                    "inventory@icloud.com",
                    state="unused",
                    source="generated",
                )
            finally:
                conn.close()

            email = await app["registration_manager"].acquire_email("ignored")
            await app["registration_manager"].release_email(email)

            self.assertEqual(email, "inventory@icloud.com")

    async def test_registration_endpoint_forwards_concurrency_three(self):
        class RegistrationManagerStub:
            def __init__(self):
                self.starts = []

            def snapshot(self):
                return {"status": "idle", "running": False}

            def start(self, **options):
                self.starts.append(options)
                return {
                    "status": "running",
                    "running": True,
                    "requested": options["concurrency"],
                }

            async def stop(self):
                return self.snapshot()

            async def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            manager = RegistrationManagerStub()
            app["registration_manager"] = manager
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/registration/start",
                    json={
                        "label": "OpenAI 一键注册",
                        "headless": False,
                        "concurrency": 3,
                    },
                    headers={"X-Local-Token": app["local_token"]},
                )
                payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["task"]["requested"], 3)
        self.assertEqual(manager.starts[0]["concurrency"], 3)

    async def test_scheduled_status_reports_available_inventory_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            conn = connect_db(str(app["db_file"]))
            try:
                for index in range(4):
                    upsert_address(
                        conn,
                        f"inventory-{index}@icloud.com",
                        state="unused",
                        source="generated",
                    )
            finally:
                conn.close()
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.get("/api/scheduled-generation/status")
                payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["inventoryAvailable"], 4)


if __name__ == "__main__":
    unittest.main()
