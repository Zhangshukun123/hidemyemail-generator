import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.browser_tasks import _save_account_record
from hidemyemail_generator.inbox import connect_db, upsert_address
from hidemyemail_generator.registration_inventory import (
    available_generated_inventory_count,
    claim_generated_inventory_email,
    clear_generated_inventory_claims,
    complete_generated_inventory_lease,
    lease_generated_inventory_email,
    registration_inventory_status,
    release_generated_inventory_email,
)
from hidemyemail_generator.registration_inventory_auth import (
    INVENTORY_AUTH_SETTING_KEY,
    InventoryCredentialStore,
)
from hidemyemail_generator.registration_inventory_client import (
    INVENTORY_LOGIN_PATH,
    INVENTORY_STATUS_PATH,
    INVENTORY_SYNC_PATH,
    RemoteRegistrationInventoryClient,
)
from hidemyemail_generator.registration_inventory_sync import (
    export_inventory_records,
    import_inventory_records,
)
from hidemyemail_generator.webapp import create_app


class RegistrationInventoryTests(unittest.TestCase):
    def test_inventory_login_password_is_stored_as_salted_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            store = InventoryCredentialStore(db_file)

            store.configure("inventory-user", "strong-test-password")

            self.assertTrue(store.configured)
            self.assertTrue(store.verify("inventory-user", "strong-test-password"))
            self.assertFalse(store.verify("inventory-user", "wrong-password"))
            self.assertFalse(store.verify("wrong-user", "strong-test-password"))
            conn = connect_db(str(db_file))
            try:
                stored = conn.execute(
                    "SELECT value FROM settings WHERE key = ?",
                    (INVENTORY_AUTH_SETTING_KEY,),
                ).fetchone()["value"]
            finally:
                conn.close()
            self.assertNotIn("strong-test-password", stored)
            self.assertIn('"scheme":"scrypt-v1"', stored)

    def test_complete_record_round_trip_preserves_all_address_and_account_fields(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
            source_db = Path(source_dir) / "source.db"
            target_db = Path(target_dir) / "target.db"
            conn = connect_db(str(source_db))
            try:
                upsert_address(
                    conn,
                    "complete@icloud.com",
                    label="完整标签",
                    state="unused",
                    source="generated",
                    note="完整备注",
                    is_active=False,
                    batch_id="batch-all-fields",
                    created_at="2026-08-01T01:02:03+00:00",
                )
                conn.execute(
                    "UPDATE addresses SET updated_at = ? WHERE email = ?",
                    ("2026-08-02T04:05:06+00:00", "complete@icloud.com"),
                )
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    (
                        "gpt_account:complete@icloud.com",
                        json.dumps(
                            {
                                "email": "complete@icloud.com",
                                "password": "saved-password",
                                "password_confirmed": False,
                                "custom_property": {"nested": [1, 2, 3]},
                                "updated_at": "2026-08-02T04:05:06+00:00",
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            records = export_inventory_records(source_db)
            result = import_inventory_records(target_db, records)

            self.assertEqual(result, {"records": 1, "addresses": 1, "accounts": 1})
            self.assertEqual(export_inventory_records(target_db), records)

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

    def test_release_returns_failed_claim_for_retry(self):
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
            conn = connect_db(str(db_file))
            try:
                state = conn.execute(
                    "SELECT state FROM addresses WHERE email = ?",
                    ("retry@icloud.com",),
                ).fetchone()["state"]
            finally:
                conn.close()
            self.assertEqual(state, "unused")

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

    def test_password_only_registration_history_remains_retryable(self):
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
                        json.dumps(
                            {
                                "password": "previous-attempt-password",
                                "password_confirmed": False,
                            }
                        ),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(
                claim_generated_inventory_email(db_file),
                "registered@icloud.com",
            )

    def test_startup_cleanup_releases_abandoned_claims_for_retry(self):
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

    def test_failure_retries_same_address_and_success_marks_used(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            conn = connect_db(str(db_file))
            try:
                for email in ("failed@icloud.com", "succeeded@icloud.com"):
                    upsert_address(
                        conn, email, state="unused", source="generated"
                    )
            finally:
                conn.close()

            failed = lease_generated_inventory_email(db_file)
            complete_generated_inventory_lease(
                db_file,
                lease_id=failed["leaseId"],
                success=False,
                message="registration failed",
            )
            self.assertEqual(registration_inventory_status(db_file)["available"], 2)
            conn = connect_db(str(db_file))
            try:
                failed_state = conn.execute(
                    "SELECT state FROM addresses WHERE email = ?",
                    ("failed@icloud.com",),
                ).fetchone()["state"]
            finally:
                conn.close()
            self.assertEqual(failed_state, "unused")

            succeeded = lease_generated_inventory_email(db_file)
            self.assertEqual(succeeded["email"], "failed@icloud.com")
            complete_generated_inventory_lease(
                db_file,
                lease_id=succeeded["leaseId"],
                success=True,
                message="registration succeeded",
            )
            conn = connect_db(str(db_file))
            try:
                state = conn.execute(
                    "SELECT state FROM addresses WHERE email = ?",
                    ("failed@icloud.com",),
                ).fetchone()["state"]
            finally:
                conn.close()
            self.assertEqual(state, "used")
            self.assertEqual(registration_inventory_status(db_file)["available"], 1)

    def test_unanswered_lease_expires_after_ten_minutes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            conn = connect_db(str(db_file))
            try:
                upsert_address(
                    conn, "timeout@icloud.com", state="unused", source="generated"
                )
            finally:
                conn.close()
            started = datetime(2026, 8, 6, 1, 0, tzinfo=timezone.utc)
            lease = lease_generated_inventory_email(
                db_file, now=started, lease_seconds=600
            )

            status = registration_inventory_status(
                db_file, now=started + timedelta(minutes=10)
            )

            self.assertEqual(status["expiredLeases"], 1)
            self.assertEqual(status["available"], 1)
            expired_result = complete_generated_inventory_lease(
                db_file,
                lease_id=lease["leaseId"],
                success=True,
                now=started + timedelta(minutes=10),
            )
            self.assertFalse(expired_result["ok"])
            self.assertEqual(expired_result["status"], "expired")
            conn = connect_db(str(db_file))
            try:
                state = conn.execute(
                    "SELECT state FROM addresses WHERE email = ?",
                    ("timeout@icloud.com",),
                ).fetchone()["state"]
            finally:
                conn.close()
            self.assertEqual(state, "unused")


class RegistrationInventoryWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_loopback_inventory_client_is_automatically_upgraded_to_https(self):
        client = RemoteRegistrationInventoryClient(
            service_url="http://inventory.example.com",
            token="test-token",
        )

        self.assertEqual(client.service_url, "https://inventory.example.com")
        self.assertEqual(client.configuration_error, "")

    async def test_inventory_client_adds_https_when_scheme_is_omitted(self):
        client = RemoteRegistrationInventoryClient(
            service_url="inventory.example.com",
            token="test-token",
        )

        self.assertEqual(client.service_url, "https://inventory.example.com")
        self.assertEqual(client.configuration_error, "")

    async def test_inventory_client_retries_connector_error_before_request_is_sent(self):
        client = RemoteRegistrationInventoryClient(
            service_url="https://inventory.example.com",
            token="test-token",
            connect_retry_delays=(0, 0),
        )
        calls = 0

        async def fake_send_once(method, path, *, payload=None, headers=None):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise aiohttp.ClientConnectorError(None, OSError("temporary failure"))
            return 200, {"ok": True, "available": 1}

        client._send_once = fake_send_once

        status = await client.status()

        self.assertTrue(status["ok"])
        self.assertEqual(calls, 3)

    async def test_account_login_allows_sync_lease_and_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(
                base_dir=Path(temp_dir),
                inventory_server_enabled=True,
                web_username="inventory-user",
                web_password="strong-test-password",
                workbench_import_token="legacy-token-at-least-32-characters",
            )
            server = TestServer(app)
            http = TestClient(server)
            await http.start_server()
            remote = RemoteRegistrationInventoryClient(
                service_url=str(server.make_url("/")).rstrip("/"),
                username="inventory-user",
                password="strong-test-password",
            )
            try:
                unauthenticated = await http.get(INVENTORY_STATUS_PATH)
                legacy = await http.get(
                    INVENTORY_STATUS_PATH,
                    headers={
                        "X-HME-Import-Token": "legacy-token-at-least-32-characters"
                    },
                )
                bad_login = await http.post(
                    INVENTORY_LOGIN_PATH,
                    json={"username": "inventory-user", "password": "wrong-password"},
                )
                sync_result = await remote.sync_records(
                    [
                        {
                            "email": "account-login@icloud.com",
                            "address": {
                                "email": "account-login@icloud.com",
                                "state": "unused",
                                "source": "generated",
                            },
                            "account": {
                                "email": "account-login@icloud.com",
                                "custom": {"preserved": True},
                            },
                        }
                    ]
                )
                leased_email = await remote.acquire_email("account login")
                leased_record = remote.leased_record(leased_email)
                await remote.complete_email(
                    leased_email,
                    True,
                    "registered",
                    record={
                        **leased_record,
                        "account": {
                            **leased_record["account"],
                            "password_confirmed": True,
                        },
                    },
                )
                app["inventory_access_tokens"].clear()
                status_after_token_expiry = await remote.status()
            finally:
                await http.close()

            self.assertEqual(unauthenticated.status, 401)
            self.assertEqual(legacy.status, 401)
            self.assertEqual(bad_login.status, 401)
            self.assertEqual(sync_result["records"], 1)
            self.assertEqual(leased_email, "account-login@icloud.com")
            self.assertTrue(leased_record["account"]["custom"]["preserved"])
            self.assertTrue(status_after_token_expiry["ok"])
            exported = export_inventory_records(app["db_file"])
            self.assertEqual(exported[0]["address"]["state"], "used")
            self.assertTrue(exported[0]["account"]["password_confirmed"])

    async def test_static_token_exchanges_session_when_account_login_is_enabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token = "static-token-at-least-32-characters-long"
            app = create_app(
                base_dir=Path(temp_dir),
                inventory_server_enabled=True,
                web_username="inventory-user",
                web_password="strong-test-password",
                workbench_import_token=token,
            )
            server = TestServer(app)
            http = TestClient(server)
            await http.start_server()
            remote = RemoteRegistrationInventoryClient(
                service_url=str(server.make_url("/")).rstrip("/"),
                token=token,
            )
            try:
                initial = await remote.status()
                first_session = remote._session_cookie
                app["session_token"] = "rotated-after-service-restart"
                after_restart = await remote.status()
            finally:
                await http.close()

            self.assertTrue(initial["ok"])
            self.assertTrue(first_session)
            self.assertTrue(after_restart["ok"])
            self.assertNotEqual(remote._session_cookie, first_session)

    async def test_sync_api_and_lease_round_trip_complete_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token = "test-token-at-least-32-characters-long"
            app = create_app(
                base_dir=Path(temp_dir),
                inventory_server_enabled=True,
                workbench_import_token=token,
                web_password="admin-password",
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            headers = {"X-HME-Import-Token": token}
            address = {
                "email": "sync-api@icloud.com",
                "label": "远程完整属性",
                "state": "unused",
                "source": "generated",
                "note": "同步备注",
                "is_active": 0,
                "batch_id": "server-batch",
                "created_at": "2026-08-01T01:00:00+00:00",
                "updated_at": "2026-08-02T02:00:00+00:00",
            }
            try:
                synced = await client.post(
                    INVENTORY_SYNC_PATH,
                    json={
                        "schemaVersion": 1,
                        "records": [
                            {
                                "email": address["email"],
                                "address": address,
                                "account": {
                                    "email": address["email"],
                                    "password": "reserved-password",
                                    "password_confirmed": False,
                                    "custom": {"keep": True},
                                },
                            }
                        ],
                    },
                    headers=headers,
                )
                sync_payload = await synced.json()
                leased = await client.post(
                    "/api/integrations/registration-inventory/lease",
                    json={"clientId": "test-client", "label": "round trip"},
                    headers=headers,
                )
                lease_payload = await leased.json()
                completed_account = {
                    **lease_payload["lease"]["record"]["account"],
                    "password_confirmed": True,
                    "access_token": "complete-token",
                    "session": {"accessToken": "complete-token"},
                    "cookies": [{"name": "session", "value": "cookie-value"}],
                    "two_factor": {"enabled": True, "secret": "TOTP-SECRET"},
                }
                completed = await client.post(
                    "/api/integrations/registration-inventory/result",
                    json={
                        "leaseId": lease_payload["lease"]["leaseId"],
                        "email": address["email"],
                        "success": True,
                        "message": "registered",
                        "record": {
                            "email": address["email"],
                            "address": address,
                            "account": completed_account,
                        },
                    },
                    headers=headers,
                )
            finally:
                await client.close()

            self.assertEqual(synced.status, 200)
            self.assertEqual(sync_payload["accounts"], 1)
            self.assertEqual(leased.status, 200)
            self.assertEqual(lease_payload["lease"]["record"]["address"], address)
            self.assertEqual(completed.status, 200)
            exported = export_inventory_records(app["db_file"])
            self.assertEqual(exported[0]["address"]["state"], "used")
            self.assertEqual(exported[0]["account"]["access_token"], "complete-token")
            self.assertEqual(
                exported[0]["account"]["two_factor"]["secret"], "TOTP-SECRET"
            )

    async def test_webapp_registration_manager_acquires_from_remote_inventory(self):
        with tempfile.TemporaryDirectory() as remote_dir, tempfile.TemporaryDirectory() as local_dir:
            token = "test-token-at-least-32-characters-long"
            remote_app = create_app(
                base_dir=Path(remote_dir),
                inventory_server_enabled=True,
                workbench_import_token=token,
                web_password="admin-password",
            )
            conn = connect_db(str(remote_app["db_file"]))
            try:
                upsert_address(
                    conn,
                    "inventory@icloud.com",
                    state="unused",
                    source="generated",
                )
            finally:
                conn.close()
            remote_server = TestServer(remote_app)
            await remote_server.start_server()
            try:
                local_app = create_app(
                    base_dir=Path(local_dir),
                    inventory_service_url=str(remote_server.make_url("/")).rstrip("/"),
                    inventory_service_token=token,
                )
                email = await local_app["registration_manager"].acquire_email(
                    "remote test"
                )
                local_record = export_inventory_records(
                    local_app["db_file"], emails=[email]
                )[0]
                _save_account_record(
                    local_app["db_file"],
                    email,
                    result={
                        "access_token": "registered-access-token",
                        "session_json": json.dumps(
                            {
                                "accessToken": "registered-access-token",
                                "account": {"planType": "free"},
                            }
                        ),
                        "cookies_json": json.dumps(
                            [{"name": "session", "value": "registered-cookie"}]
                        ),
                    },
                    password="registered-password",
                    password_confirmed=True,
                    two_factor={"enabled": True, "secret": "REGISTERED-TOTP"},
                )
                await local_app["registration_manager"].complete_email(
                    email, True, "registration succeeded"
                )
            finally:
                await remote_server.close()

            self.assertEqual(email, "inventory@icloud.com")
            self.assertEqual(local_record["address"]["source"], "generated")
            conn = connect_db(str(remote_app["db_file"]))
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT state FROM addresses WHERE email = ?", (email,)
                    ).fetchone()["state"],
                    "used",
                )
                saved_account = json.loads(
                    conn.execute(
                        "SELECT value FROM settings WHERE key = ?",
                        (f"gpt_account:{email}",),
                    ).fetchone()["value"]
                )
            finally:
                conn.close()
            self.assertEqual(
                saved_account["access_token"], "registered-access-token"
            )
            self.assertEqual(
                saved_account["two_factor"]["secret"], "REGISTERED-TOTP"
            )

    async def test_webapp_registration_falls_back_to_local_generated_inventory(self):
        with tempfile.TemporaryDirectory() as local_dir:
            app = create_app(
                base_dir=Path(local_dir),
                inventory_service_url="https://inventory.example.com",
                inventory_service_token="expired-token-at-least-32-characters",
            )
            conn = connect_db(str(app["db_file"]))
            try:
                upsert_address(
                    conn,
                    "local-fallback@icloud.com",
                    state="unused",
                    source="generated",
                )
            finally:
                conn.close()

            async def rejected_sync(_records):
                raise RuntimeError("请先登录")

            app["inventory_client"].sync_records = rejected_sync
            email = await app["registration_manager"].acquire_email("fallback test")
            await app["registration_manager"].complete_email(
                email, False, "retry later"
            )

            self.assertEqual(email, "local-fallback@icloud.com")
            self.assertTrue(app["inventory_fallback_active"])
            conn = connect_db(str(app["db_file"]))
            try:
                state = conn.execute(
                    "SELECT state FROM addresses WHERE email = ?", (email,)
                ).fetchone()["state"]
            finally:
                conn.close()
            self.assertEqual(state, "unused")

    async def test_completed_local_account_is_removed_from_remote_inventory(self):
        with tempfile.TemporaryDirectory() as remote_dir, tempfile.TemporaryDirectory() as local_dir:
            token = "test-token-at-least-32-characters-long"
            remote_app = create_app(
                base_dir=Path(remote_dir),
                inventory_server_enabled=True,
                workbench_import_token=token,
                web_password="admin-password",
            )
            conn = connect_db(str(remote_app["db_file"]))
            try:
                for email in ("existing@icloud.com", "fresh@icloud.com"):
                    upsert_address(
                        conn, email, state="unused", source="generated"
                    )
            finally:
                conn.close()
            remote_server = TestServer(remote_app)
            await remote_server.start_server()
            try:
                local_app = create_app(
                    base_dir=Path(local_dir),
                    inventory_service_url=str(remote_server.make_url("/")).rstrip("/"),
                    inventory_service_token=token,
                )
                conn = connect_db(str(local_app["db_file"]))
                try:
                    conn.execute(
                        "INSERT INTO settings(key, value) VALUES (?, ?)",
                        (
                            "gpt_account:existing@icloud.com",
                            json.dumps(
                                {
                                    "access_token": "completed-session-token",
                                }
                            ),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                email = await local_app["registration_manager"].acquire_email(
                    "remote dedupe test"
                )
            finally:
                await remote_server.close()

            self.assertEqual(email, "fresh@icloud.com")
            conn = connect_db(str(remote_app["db_file"]))
            try:
                state = conn.execute(
                    "SELECT state FROM addresses WHERE email = ?",
                    ("existing@icloud.com",),
                ).fetchone()["state"]
            finally:
                conn.close()
            self.assertEqual(state, "used")

    async def test_password_only_local_history_retries_remote_inventory_address(self):
        with tempfile.TemporaryDirectory() as remote_dir, tempfile.TemporaryDirectory() as local_dir:
            token = "test-token-at-least-32-characters-long"
            remote_app = create_app(
                base_dir=Path(remote_dir),
                inventory_server_enabled=True,
                workbench_import_token=token,
                web_password="admin-password",
            )
            conn = connect_db(str(remote_app["db_file"]))
            try:
                upsert_address(
                    conn,
                    "retry-local@icloud.com",
                    state="unused",
                    source="generated",
                )
            finally:
                conn.close()
            remote_server = TestServer(remote_app)
            await remote_server.start_server()
            try:
                local_app = create_app(
                    base_dir=Path(local_dir),
                    inventory_service_url=str(remote_server.make_url("/")).rstrip("/"),
                    inventory_service_token=token,
                )
                conn = connect_db(str(local_app["db_file"]))
                try:
                    conn.execute(
                        "INSERT INTO settings(key, value) VALUES (?, ?)",
                        (
                            "gpt_account:retry-local@icloud.com",
                            json.dumps(
                                {
                                    "password": "same-password-on-retry",
                                    "password_confirmed": False,
                                }
                            ),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                email = await local_app["registration_manager"].acquire_email(
                    "retry password-only account"
                )
            finally:
                await remote_server.close()

        self.assertEqual(email, "retry-local@icloud.com")

    async def test_registration_endpoint_forwards_manual_email_with_single_worker(self):
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
                        "label": "手动邮箱注册",
                        "email": "352121354@qq.com",
                        "headless": False,
                        "concurrency": 3,
                    },
                    headers={"X-Local-Token": app["local_token"]},
                )
                payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["task"]["requested"], 1)
        self.assertEqual(manager.starts[0]["concurrency"], 1)
        self.assertEqual(manager.starts[0]["email"], "352121354@qq.com")

    async def test_registration_endpoint_defaults_missing_provider_to_inventory(self):
        class RegistrationManagerStub:
            def __init__(self):
                self.starts = []

            def snapshot(self):
                return {"status": "idle", "running": False}

            def start(self, **options):
                self.starts.append(options)
                return {"status": "running", "running": True}

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
                page = await client.get("/")
                html = await page.text()
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
        self.assertTrue(payload["started"])
        self.assertIn('provider: "inventory"', html)
        self.assertEqual(manager.starts[0]["provider"], "inventory")
        self.assertEqual(manager.starts[0]["concurrency"], 3)

    async def test_manual_registration_code_submit_and_worker_poll_endpoints(self):
        class RegistrationManagerStub:
            def __init__(self):
                self.code = ""

            def snapshot(self):
                return {"status": "running", "running": True}

            def submit_verification_code(self, email, code):
                self.code = code
                return {
                    "status": "running",
                    "running": True,
                    "email": email,
                    "awaitingCode": True,
                }

            def poll_verification_code(self, _email):
                code, self.code = self.code, ""
                return code

            async def stop(self):
                return self.snapshot()

            async def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            manager = RegistrationManagerStub()
            app["registration_manager"] = manager
            headers = {"X-Local-Token": app["local_token"]}
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                waiting = await client.post(
                    "/api/registration/code/poll",
                    json={"email": "352121354@qq.com"},
                    headers=headers,
                )
                submitted = await client.post(
                    "/api/registration/code",
                    json={"email": "352121354@qq.com", "code": "123456"},
                    headers=headers,
                )
                received = await client.post(
                    "/api/registration/code/poll",
                    json={"email": "352121354@qq.com"},
                    headers=headers,
                )
                payload = await received.json()
            finally:
                await client.close()

        self.assertEqual(waiting.status, 404)
        self.assertEqual(submitted.status, 200)
        self.assertEqual(received.status, 200)
        self.assertEqual(payload["code"], "123456")

    async def test_registration_code_poll_forwards_browser_request_id(self):
        class RegistrationManagerStub:
            def __init__(self):
                self.polls = []

            def snapshot(self):
                return {
                    "status": "running",
                    "running": True,
                    "provider": "smsbower",
                }

            async def poll_verification_code_async(
                self, email, *, request_id=""
            ):
                self.polls.append((email, request_id))
                return "654321"

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
                    "/api/registration/code/poll",
                    json={
                        "email": "fresh.account@gmail.com",
                        "requestId": "browser-request-1",
                    },
                    headers={"X-Local-Token": app["local_token"]},
                )
                payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["code"], "654321")
        self.assertEqual(
            manager.polls,
            [("fresh.account@gmail.com", "browser-request-1")],
        )

    async def test_integration_status_reports_available_inventory_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token = "test-token-at-least-32-characters-long"
            app = create_app(
                base_dir=Path(temp_dir),
                inventory_server_enabled=True,
                workbench_import_token=token,
                web_password="admin-password",
            )
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
                response = await client.get(
                    INVENTORY_STATUS_PATH,
                    headers={"X-HME-Import-Token": token},
                )
                payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["available"], 4)

    async def test_integration_lease_and_failure_result_protocol(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token = "test-token-at-least-32-characters-long"
            app = create_app(
                base_dir=Path(temp_dir),
                inventory_server_enabled=True,
                workbench_import_token=token,
                web_password="admin-password",
            )
            conn = connect_db(str(app["db_file"]))
            try:
                upsert_address(
                    conn,
                    "protocol@icloud.com",
                    state="unused",
                    source="generated",
                )
            finally:
                conn.close()
            client = TestClient(TestServer(app))
            await client.start_server()
            headers = {"X-HME-Import-Token": token}
            try:
                unauthorized = await client.post(
                    "/api/integrations/registration-inventory/lease", json={}
                )
                leased = await client.post(
                    "/api/integrations/registration-inventory/lease",
                    json={"clientId": "test-client", "label": "test"},
                    headers=headers,
                )
                lease_payload = await leased.json()
                locked_status = await client.get(INVENTORY_STATUS_PATH, headers=headers)
                locked_payload = await locked_status.json()
                completed = await client.post(
                    "/api/integrations/registration-inventory/result",
                    json={
                        "leaseId": lease_payload["lease"]["leaseId"],
                        "email": lease_payload["lease"]["email"],
                        "success": False,
                        "message": "registration failed",
                    },
                    headers=headers,
                )
                completed_payload = await completed.json()
                released_status = await client.get(
                    INVENTORY_STATUS_PATH, headers=headers
                )
                released_payload = await released_status.json()
            finally:
                await client.close()

        self.assertEqual(unauthorized.status, 401)
        self.assertEqual(leased.status, 200)
        self.assertEqual(locked_payload["available"], 0)
        self.assertEqual(locked_payload["activeLeases"], 1)
        self.assertEqual(completed_payload["status"], "failed")
        self.assertEqual(released_payload["available"], 1)
        self.assertEqual(released_payload["activeLeases"], 0)


if __name__ == "__main__":
    unittest.main()
