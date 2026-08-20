import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.browser_tasks import _save_account_record, load_account_record
from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.web_ui import build_app_page
from hidemyemail_generator.webapp import create_app


class InvalidSessionHideMyEmail:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def list_email(self):
        return {"success": False, "error": 2, "reason": "Invalid global session"}


class ICloudDeleteFallbackRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_icloud_session_offers_local_cleanup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "cookies.txt").write_text("expired-cookie", encoding="utf-8")
            app = create_app(base_dir=root)
            email = "expired.alias@icloud.com"
            _save_account_record(app["db_file"], email, result={"email": email})
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                with mock.patch(
                    "hidemyemail_generator.webapp.RichHideMyEmail",
                    InvalidSessionHideMyEmail,
                ):
                    response = await client.post(
                        "/api/gpt-email/delete",
                        json={"email": email},
                        headers={"X-Local-Token": app["local_token"]},
                    )
                    payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(response.status, 409)
            self.assertEqual(payload["code"], "icloud_session_expired")
            self.assertTrue(payload["canDeleteLocal"])
            self.assertNotIn("Invalid global session", payload["error"])
            self.assertTrue(load_account_record(app["db_file"], email))

    async def test_confirmed_local_cleanup_removes_workbench_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = create_app(base_dir=root)
            email = "removed.at.apple@icloud.com"
            _save_account_record(app["db_file"], email, result={"email": email})
            conn = connect_db(str(app["db_file"]))
            try:
                conn.execute(
                    """
                    INSERT INTO addresses(
                        email, label, state, source, note, created_at,
                        updated_at, is_active, batch_id
                    ) VALUES (?, 'test', 'unused', 'test', '', '', '', 1, '')
                    """,
                    (email,),
                )
                conn.commit()
            finally:
                conn.close()

            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/gpt-email/delete",
                    json={"email": email, "local_only": True},
                    headers={"X-Local-Token": app["local_token"]},
                )
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(response.status, 200)
            self.assertTrue(payload["deleted"])
            self.assertTrue(payload["localOnly"])
            self.assertFalse(payload["providerDeleted"])
            self.assertEqual(load_account_record(app["db_file"], email), {})
            conn = connect_db(str(app["db_file"]))
            try:
                row = conn.execute(
                    "SELECT state, is_active FROM addresses WHERE email = ?", (email,)
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(tuple(row), ("trash", 0))


class ICloudDeleteFallbackUiTests(unittest.TestCase):
    def test_ui_prompts_for_local_cleanup_when_icloud_session_expired(self):
        page = build_app_page()

        self.assertIn('error.code === "icloud_session_expired"', page)
        self.assertIn("仅从工作台移除本地记录", page)
        self.assertIn("local_only: true", page)


if __name__ == "__main__":
    unittest.main()
