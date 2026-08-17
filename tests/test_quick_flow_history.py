from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlparse

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from playwright.sync_api import sync_playwright

from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.quick_flow_history import (
    QUICK_FLOW_HISTORY_PREFIX,
    QuickFlowHistoryModel,
    QuickFlowHistoryRepository,
    setup_quick_flow_history_routes,
)
from hidemyemail_generator.web_ui import page_builder
from hidemyemail_generator.web_ui.page_builder import build_app_page
from tests.test_account_management_compact_ui import _workspace_payloads


def flow(run_id: str, *, status: str = "completed", started_at: str) -> dict:
    return {
        "runId": run_id,
        "status": status,
        "phase": "register" if status == "running" else "complete",
        "startedAt": started_at,
        "manager": "protocol",
        "taskId": f"task-{run_id}",
        "emails": [f"{run_id}@example.com"],
        "logs": [{"at": started_at, "message": "流程已启动"}],
    }


class QuickFlowHistoryModelTests(unittest.TestCase):
    def test_terminal_runs_survive_new_model_and_selected_delete_is_permanent(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "history.db"
            first_model = QuickFlowHistoryModel(
                QuickFlowHistoryRepository(database),
                server_instance_id="server-one",
            )
            first_model.save(
                flow(
                    "run-first",
                    started_at="2026-08-17T08:00:00+00:00",
                )
            )
            first_model.save(
                flow(
                    "run-second",
                    status="failed",
                    started_at="2026-08-17T09:00:00+00:00",
                )
            )

            restarted = QuickFlowHistoryModel(
                QuickFlowHistoryRepository(database),
                server_instance_id="server-two",
            )
            self.assertEqual(
                [item["runId"] for item in restarted.list()],
                ["run-first", "run-second"],
            )
            self.assertTrue(restarted.delete("run-first"))
            self.assertEqual(
                [item["runId"] for item in restarted.list()],
                ["run-second"],
            )

            connection = connect_db(str(database))
            try:
                keys = [
                    row["key"]
                    for row in connection.execute(
                        "SELECT key FROM settings WHERE key LIKE ?",
                        (f"{QUICK_FLOW_HISTORY_PREFIX}%",),
                    )
                ]
            finally:
                connection.close()
            self.assertEqual(keys, [f"{QUICK_FLOW_HISTORY_PREFIX}run-second"])

    def test_restart_marks_only_running_record_interrupted_and_keeps_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "history.db"
            original = QuickFlowHistoryModel(
                QuickFlowHistoryRepository(database),
                server_instance_id="server-before-restart",
            )
            original.save(
                flow(
                    "run-live",
                    status="running",
                    started_at="2026-08-17T10:00:00+00:00",
                )
            )
            self.assertEqual(original.list()[0]["status"], "running")

            restarted = QuickFlowHistoryModel(
                QuickFlowHistoryRepository(database),
                server_instance_id="server-after-restart",
            )
            recovered = restarted.list()[0]

            self.assertEqual(recovered["status"], "failed")
            self.assertTrue(recovered["interrupted"])
            self.assertIn("永久保留", recovered["message"])
            self.assertIn("服务器重启", recovered["logs"][-1]["message"])
            self.assertTrue(recovered["finishedAt"])
            self.assertEqual(len(restarted.list()[0]["logs"]), 2)

    def test_corrupt_or_invalid_rows_do_not_break_history_loading(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "history.db"
            connection = connect_db(str(database))
            try:
                connection.executemany(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    [
                        (f"{QUICK_FLOW_HISTORY_PREFIX}broken", "not-json"),
                        (
                            f"{QUICK_FLOW_HISTORY_PREFIX}invalid",
                            json.dumps({"flow": {"runId": "bad id", "status": "failed"}}),
                        ),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            model = QuickFlowHistoryModel(QuickFlowHistoryRepository(database))
            self.assertEqual(model.list(), [])


class QuickFlowHistoryRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "history.db"
        self.app = web.Application()
        self.app["db_file"] = self.database
        setup_quick_flow_history_routes(
            self.app,
            token_validator=lambda request: request.headers.get("X-Local-Token")
            == "history-token",
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temporary.cleanup()

    async def test_routes_persist_reload_and_delete_history(self):
        unauthorized = await self.client.post(
            "/api/quick-flow/history",
            json={
                "flow": flow(
                    "run-route",
                    started_at="2026-08-17T11:00:00+00:00",
                )
            },
        )
        self.assertEqual(unauthorized.status, 403)

        headers = {"X-Local-Token": "history-token"}
        saved = await self.client.post(
            "/api/quick-flow/history",
            json={
                "flow": flow(
                    "run-route",
                    started_at="2026-08-17T11:00:00+00:00",
                )
            },
            headers=headers,
        )
        self.assertEqual(saved.status, 200, await saved.text())

        history = await self.client.get("/api/quick-flow/history")
        payload = await history.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["runId"], "run-route")

        deleted = await self.client.post(
            "/api/quick-flow/history/delete",
            json={"runId": "run-route"},
            headers=headers,
        )
        self.assertTrue((await deleted.json())["deleted"])
        self.assertEqual((await (await self.client.get("/api/quick-flow/history")).json())["items"], [])


class QuickFlowHistoryFrontendTests(unittest.TestCase):
    def test_workspace_uses_separate_mvp_history_presenter_below_line_limit(self):
        page = build_app_page()
        static_root = Path(page_builder.__file__).with_name("static")
        builder_source = Path(page_builder.__file__).read_text(encoding="utf-8")

        self.assertIn('"static/quick_flow_history.js"', builder_source)
        for component in (
            "class QuickFlowHistoryModel",
            "class QuickFlowHistoryView",
            "class QuickFlowHistoryPresenter",
        ):
            self.assertIn(component, page)
        self.assertIn('this.api.get("/api/quick-flow/history")', page)
        self.assertIn('this.api.post("/api/quick-flow/history", { flow: snapshot })', page)
        self.assertIn("this.loadQuickFlowHistory()", page)
        self.assertIn("this.quickFlowHistoryPresenter.persist(next)", page)
        self.assertLessEqual(
            len((static_root / "app.js").read_text(encoding="utf-8").splitlines()),
            5000,
        )
        self.assertLessEqual(
            len(
                (static_root / "quick_flow_history.js")
                .read_text(encoding="utf-8")
                .splitlines()
            ),
            5000,
        )

    def test_saved_run_is_rendered_after_reload_and_close_deletes_server_record(self):
        html = build_app_page().replace(
            "__LOCAL_TOKEN__", json.dumps("history-ui-token")
        )
        payloads = _workspace_payloads()
        payloads["/api/quick-flow/history"] = {
            "ok": True,
            "count": 1,
            "items": [
                {
                    **flow(
                        "run-restored",
                        status="failed",
                        started_at="2026-08-17T12:00:00+00:00",
                    ),
                    "message": "服务器重启前流程未完成；记录已永久保留",
                    "progress": 55,
                    "failed": 1,
                }
            ],
        }
        deleted: list[dict] = []

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})

            def fulfill(route):
                path = urlparse(route.request.url).path
                if path == "/":
                    route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=html,
                    )
                    return
                if path == "/api/quick-flow/history/delete":
                    deleted.append(route.request.post_data_json)
                    route.fulfill(
                        status=200,
                        content_type="application/json; charset=utf-8",
                        body=json.dumps(
                            {"ok": True, "runId": "run-restored", "deleted": True}
                        ),
                    )
                    return
                route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=json.dumps(payloads.get(path, {"ok": True}), ensure_ascii=False),
                )

            page.route("**/*", fulfill)
            page.goto("http://quick-flow.test/#quick-flow", wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelectorAll('[data-action=\"select-quick-flow\"]').length === 1"
            )
            self.assertIn("记录已永久保留", page.locator("#quickFlowRunList").inner_text())
            self.assertIn("0 运行 / 1 条", page.locator("#quickFlowRunCount").inner_text())

            page.locator('[data-action="dismiss-quick-flow-run"]').click()
            page.wait_for_function(
                "document.querySelectorAll('[data-action=\"select-quick-flow\"]').length === 0"
            )
            browser.close()

        self.assertEqual(deleted, [{"runId": "run-restored"}])


if __name__ == "__main__":
    unittest.main()
