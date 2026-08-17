from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.web_ui import page_builder
from hidemyemail_generator.web_ui.page_builder import build_app_page
from hidemyemail_generator.webapp import create_app


def jwt(payload: dict) -> str:
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    return f"eyJhbGciOiJub25lIn0.{encoded}.signature"


def exportable_record(email: str) -> dict:
    account_id = "acct-plus-web"
    user_id = "user-plus-web"
    expires_at = int(time.time()) + 3600
    access_token = jwt(
        {
            "exp": expires_at,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
                "chatgpt_user_id": user_id,
                "chatgpt_plan_type": "plus",
            },
            "https://api.openai.com/profile": {"email": email},
        }
    )
    id_token = jwt(
        {
            "exp": expires_at,
            "email": email,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
                "chatgpt_user_id": user_id,
            },
        }
    )
    return {
        "email": email,
        "account_type": "plus",
        "payment_confirmation": {
            "status": "plus",
            "payment_succeeded": True,
            "plan": "plus",
        },
        "plus_codex": {
            "status": "completed",
            "export_ready": True,
            "completed_at": "2026-08-17T04:00:00Z",
        },
        "codex_oauth": {
            "status": "completed",
            "access_token": access_token,
            "refresh_token": "refresh-token-from-plus-oauth",
            "id_token": id_token,
            "account_id": account_id,
            "last_refresh": "2026-08-17T04:00:00Z",
        },
    }


class PlusExportEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(base_dir=Path(self.temp_dir.name))
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.headers = {"X-Local-Token": self.app["local_token"]}

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temp_dir.cleanup()

    def save_account(self, email: str, record: dict) -> None:
        connection = connect_db(str(self.app["db_file"]))
        try:
            connection.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (f"gpt_account:{email}", json.dumps(record, ensure_ascii=False)),
            )
            connection.commit()
        finally:
            connection.close()

    async def test_successfully_downloads_cpa_and_sub2api(self) -> None:
        email = "export-ready@zkgmail.com"
        record = exportable_record(email)
        self.save_account(email, record)

        cpa_response = await self.client.post(
            "/api/plus-accounts/export",
            json={"format": "cpa", "email": email},
            headers=self.headers,
        )
        self.assertEqual(cpa_response.status, 200)
        self.assertEqual(cpa_response.content_type, "application/json")
        self.assertEqual(cpa_response.headers["X-Account-Count"], "1")
        self.assertIn(
            "plus-cpa-export-ready-zkgmail.com.json",
            cpa_response.headers["Content-Disposition"],
        )
        cpa = await cpa_response.json()
        self.assertEqual(cpa["type"], "codex")
        self.assertEqual(cpa["email"], email)
        self.assertEqual(cpa["access_token"], record["codex_oauth"]["access_token"])
        self.assertEqual(cpa["refresh_token"], record["codex_oauth"]["refresh_token"])

        sub2api_response = await self.client.post(
            "/api/plus-accounts/export",
            json={"format": "sub2api", "email": email},
            headers=self.headers,
        )
        self.assertEqual(sub2api_response.status, 200)
        self.assertEqual(sub2api_response.headers["X-Account-Count"], "1")
        sub2api = await sub2api_response.json()
        self.assertEqual(set(sub2api), {"exported_at", "proxies", "accounts"})
        self.assertEqual(sub2api["proxies"], [])
        credentials = sub2api["accounts"][0]["credentials"]
        self.assertEqual(credentials["email"], email)
        self.assertEqual(credentials["chatgpt_account_id"], "acct-plus-web")
        self.assertEqual(credentials["chatgpt_user_id"], "user-plus-web")
        self.assertEqual(credentials["plan_type"], "plus")

    async def test_returns_not_found_when_no_account_is_exportable(self) -> None:
        response = await self.client.post(
            "/api/plus-accounts/export",
            json={"format": "cpa"},
            headers=self.headers,
        )

        self.assertEqual(response.status, 404)
        payload = await response.json()
        self.assertIn("没有同时完成", payload["error"])

    async def test_rejects_unknown_export_format(self) -> None:
        response = await self.client.post(
            "/api/plus-accounts/export",
            json={"format": "csv"},
            headers=self.headers,
        )

        self.assertEqual(response.status, 400)
        payload = await response.json()
        self.assertIn("不支持的导出格式", payload["error"])

    async def test_rejects_invalid_local_token(self) -> None:
        response = await self.client.post(
            "/api/plus-accounts/export",
            json={"format": "cpa"},
            headers={"X-Local-Token": "invalid-local-token"},
        )

        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())["error"], "本地请求令牌无效")

    async def test_page_contains_export_controller_and_both_buttons(self) -> None:
        response = await self.client.get("/")

        self.assertEqual(response.status, 200)
        page = await response.text()
        self.assertIn('data-plus-export="cpa"', page)
        self.assertIn('data-plus-export="sub2api"', page)
        self.assertIn("导出 CPA", page)
        self.assertIn("导出 Sub2API", page)
        self.assertIn('fetch("/api/plus-accounts/export"', page)


class PlusExportFrontendAssemblyTests(unittest.TestCase):
    def test_page_builder_loads_plus_exports_script(self) -> None:
        source = Path(page_builder.__file__).read_text(encoding="utf-8")

        for script in ("static/app.js", "static/plus_exports.js"):
            self.assertIn(f'"{script}"', source)
        self.assertIn('fetch("/api/plus-accounts/export"', build_app_page())

    def test_frontend_javascript_files_stay_below_line_limit(self) -> None:
        static_root = Path(page_builder.__file__).with_name("static")

        for script_path in static_root.glob("*.js"):
            with self.subTest(filename=script_path.name):
                line_count = len(
                    script_path.read_text(encoding="utf-8").splitlines()
                )
                self.assertLessEqual(line_count, 5000)


if __name__ == "__main__":
    unittest.main()
