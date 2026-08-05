import asyncio
import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from hidemyemail_generator.account_verifier import (
    AccountVerificationManager,
    load_verifiable_accounts,
    remove_invalid_account,
    removed_account_emails,
    save_account_classification,
)
from hidemyemail_generator.browser_tasks import (
    _save_account_record,
    load_account_record,
    set_manual_account_type,
)
from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.openai_account_check_bridge import confirmed_invalid
from hidemyemail_generator.webapp import _browser_email_items


def save_record(
    db_file: Path, email: str, token: str, *, account_type: str = ""
) -> None:
    conn = connect_db(str(db_file))
    try:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            (
                f"gpt_account:{email}",
                json.dumps(
                    {
                        "email": email,
                        "password": "Secret!A7",
                        "access_token": token,
                        "session": {"accessToken": token},
                        "account_type": account_type,
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def token_with_plan(plan: str, *, expires_in: int = 3600) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "exp": int(time.time()) + expires_in,
                "https://api.openai.com/auth": {
                    "chatgpt_plan_type": plan,
                },
            }
        ).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


class AccountVerificationManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_jwt_plus_is_classified_without_running_online_bridge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "must_not_run.py"
            bridge.write_text(
                "raise AssertionError('online bridge must not run')\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            save_record(db_file, "fast-plus@icloud.com", token_with_plan("plus"))
            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )

            manager.start(concurrency=1)
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["plus"], 1)
            self.assertEqual(snapshot["failed"], 0)
            record = load_account_record(db_file, "fast-plus@icloud.com")
            self.assertEqual(record["account_type"], "plus")
            self.assertIn("本地快速验证", record["verification_detail"])

    async def test_jwt_free_still_uses_online_check_to_detect_an_upgrade(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "online_check.py"
            bridge.write_text(
                "import json\n"
                "print('HME_VERIFY_EVENT:' + json.dumps({'status':'plus','detail':'online upgrade'}), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            save_record(db_file, "upgraded@icloud.com", token_with_plan("free"))
            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )

            manager.start(concurrency=1)
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["plus"], 1)
            self.assertEqual(snapshot["failed"], 0)
            self.assertIn(
                "online upgrade",
                load_account_record(db_file, "upgraded@icloud.com")[
                    "verification_detail"
                ],
            )

    def test_top_level_token_without_session_is_not_verifiable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            conn = connect_db(str(db_file))
            try:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    (
                        "gpt_account:legacy@icloud.com",
                        json.dumps({"access_token": "legacy-token-only"}),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(load_verifiable_accounts(db_file), [])
            items = _browser_email_items(
                db_file,
                [
                    {
                        "hme": "legacy@icloud.com",
                        "anonymousId": "legacy",
                        "isActive": True,
                    }
                ],
            )
            self.assertFalse(items[0]["hasSession"])
            self.assertFalse(items[0]["hasImportableSession"])
            self.assertEqual(items[0]["sessionStatus"], "expired")

    async def test_headless_browser_refreshes_multiple_sessions_in_one_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "fake_check_bridge.py"
            bridge.write_text(
                "import json\n"
                "print('HME_VERIFY_EVENT:' + json.dumps({'status':'free','detail':'verified'}), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            _save_account_record(
                db_file,
                "one@icloud.com",
                two_factor={
                    "secret": "JBSWY3DPEHPK3PXP",
                    "enabled": True,
                },
            )

            class BrowserManagerStub:
                def __init__(self):
                    self.started = []
                    self.running = False

                def availability(self):
                    return {"available": True, "errors": []}

                def snapshot(self):
                    return {"running": self.running}

                def start(self, accounts, **options):
                    self.started.append((accounts, options))
                    self.running = True
                    for account in accounts:
                        _save_account_record(
                            db_file,
                            account["email"],
                            result={
                                "access_token": f"at-{account['email']}",
                                "session_json": json.dumps(
                                    {
                                        "accessToken": f"at-{account['email']}",
                                        "user": {"email": account["email"]},
                                    }
                                ),
                            },
                        )
                    return {"running": True}

                async def wait(self):
                    self.running = False
                    accounts = self.started[0][0]
                    return {
                        "status": "completed",
                        "accounts": [
                            {"email": item["email"], "status": "success"}
                            for item in accounts
                        ],
                    }

                async def stop(self):
                    self.running = False
                    return {"status": "cancelled"}

            browser_manager = BrowserManagerStub()
            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
                browser_manager=browser_manager,
            )

            state = manager.start_with_browser(
                emails=["two@icloud.com", "one@icloud.com"], concurrency=2
            )
            self.assertTrue(state["headless"])
            self.assertEqual(state["concurrency"], 2)
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["free"], 2)
            self.assertEqual(snapshot["failed"], 0)
            self.assertEqual(len(browser_manager.started), 1)
            accounts, options = browser_manager.started[0]
            self.assertEqual(len(accounts), 2)
            self.assertTrue(options["headless"])
            self.assertEqual(options["concurrency"], 2)
            browser_accounts = {item["email"]: item for item in accounts}
            self.assertEqual(
                browser_accounts["one@icloud.com"]["two_factor"]["secret"],
                "JBSWY3DPEHPK3PXP",
            )
            self.assertEqual(
                browser_accounts["two@icloud.com"]["two_factor"], {}
            )

    def test_loads_token_and_classification_from_saved_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            conn = connect_db(str(db_file))
            try:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    (
                        "gpt_account:session-only@icloud.com",
                        json.dumps(
                            {
                                "session_json": json.dumps(
                                    {
                                        "accessToken": "at-from-session",
                                        "user": {
                                            "email": "session-only@icloud.com"
                                        },
                                        "account": {"planType": "plus"},
                                    }
                                ),
                                "password_confirmed": False,
                            }
                        ),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            accounts = load_verifiable_accounts(db_file)

            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0]["access_token"], "at-from-session")
            self.assertEqual(accounts[0]["session_email"], "session-only@icloud.com")
            self.assertEqual(accounts[0]["account_type"], "plus")
            self.assertEqual(accounts[0]["account_type_source"], "session")

    def test_automatic_classification_preserves_manual_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            set_manual_account_type(db_file, "manual@icloud.com", "free")

            save_account_classification(
                db_file, "manual@icloud.com", "plus", "online says plus"
            )

            record = load_account_record(db_file, "manual@icloud.com")
            self.assertEqual(record["account_type"], "free")
            self.assertEqual(record["account_type_source"], "manual")
            self.assertIn("已保留手动设置", record["verification_detail"])

    def test_legacy_removed_marker_does_not_hide_icloud_alias(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            save_record(db_file, "restored@icloud.com", "at-old")
            remove_invalid_account(db_file, "restored@icloud.com", "legacy removal")

            items = _browser_email_items(
                db_file,
                [
                    {
                        "hme": "restored@icloud.com",
                        "anonymousId": "restored",
                        "isActive": True,
                    }
                ],
            )
            self.assertEqual([item["email"] for item in items], ["restored@icloud.com"])
            self.assertFalse(items[0]["hasPassword"])

    async def test_classifies_valid_accounts_and_preserves_invalid_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            bridge = root / "fake_check_bridge.py"
            bridge.write_text(
                "import json, os\n"
                "token = os.environ['HME_OPENAI_ACCESS_TOKEN']\n"
                "status = {'at-plus':'plus','at-free':'free','at-bad':'invalid'}[token]\n"
                "print('HME_VERIFY_EVENT:' + json.dumps({'status':status,'detail':'test result'}), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            save_record(db_file, "plus@icloud.com", "at-plus")
            save_record(db_file, "free@icloud.com", "at-free")
            save_record(db_file, "bad@icloud.com", "at-bad")

            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            state = manager.start(concurrency=3)
            self.assertTrue(state["running"])
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["plus"], 1)
            self.assertEqual(snapshot["free"], 1)
            self.assertEqual(snapshot["expired"], 1)
            self.assertEqual(snapshot["deleted"], 0)
            self.assertNotIn("access_token", json.dumps(snapshot))
            self.assertEqual(
                load_account_record(db_file, "plus@icloud.com")["account_type"],
                "plus",
            )
            self.assertEqual(
                load_account_record(db_file, "free@icloud.com")["account_type"],
                "free",
            )
            preserved = load_account_record(db_file, "bad@icloud.com")
            self.assertEqual(preserved["password"], "Secret!A7")
            self.assertEqual(preserved["access_token"], "at-bad")
            self.assertEqual(preserved["session"]["accessToken"], "at-bad")
            self.assertIn("session_invalid_at", preserved)
            self.assertNotIn("bad@icloud.com", removed_account_emails(db_file))

            identities = [
                {"hme": "plus@icloud.com", "anonymousId": "plus", "isActive": True},
                {"hme": "free@icloud.com", "anonymousId": "free", "isActive": True},
                {"hme": "bad@icloud.com", "anonymousId": "bad", "isActive": True},
            ]
            items = _browser_email_items(db_file, identities)
            self.assertEqual(
                {item["email"] for item in items},
                {"plus@icloud.com", "free@icloud.com", "bad@icloud.com"},
            )
            invalid_item = next(
                item for item in items if item["email"] == "bad@icloud.com"
            )
            self.assertEqual(invalid_item["sessionStatus"], "expired")
            self.assertFalse(invalid_item["hasImportableSession"])

    async def test_invalid_token_automatically_refreshes_in_headless_browser(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "refresh_then_verify.py"
            bridge.write_text(
                "import json, os\n"
                "token = os.environ['HME_OPENAI_ACCESS_TOKEN']\n"
                "status = 'invalid' if token == 'at-expired' else 'free'\n"
                "print('HME_VERIFY_EVENT:' + json.dumps({'status':status,'detail':'online check'}), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            save_record(db_file, "refresh@icloud.com", "at-expired")

            class BrowserManagerStub:
                def __init__(self):
                    self.started = []

                def start(self, accounts, **options):
                    self.started.append((accounts, options))
                    _save_account_record(
                        db_file,
                        "refresh@icloud.com",
                        result={
                            "access_token": "at-fresh",
                            "session_json": json.dumps(
                                {
                                    "accessToken": "at-fresh",
                                    "user": {"email": "refresh@icloud.com"},
                                }
                            ),
                        },
                    )
                    return {"running": True}

                async def wait(self):
                    return {
                        "status": "completed",
                        "accounts": [
                            {
                                "email": "refresh@icloud.com",
                                "status": "success",
                            }
                        ],
                    }

                async def stop(self):
                    return {"status": "cancelled"}

                def snapshot(self):
                    return {"running": False}

            browser_manager = BrowserManagerStub()
            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
                browser_manager=browser_manager,
            )
            manager.start(concurrency=1)
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["free"], 1)
            self.assertEqual(snapshot["expired"], 0)
            self.assertEqual(snapshot["failed"], 0)
            self.assertEqual(len(browser_manager.started), 1)
            self.assertTrue(browser_manager.started[0][1]["headless"])
            self.assertEqual(
                load_account_record(db_file, "refresh@icloud.com")["access_token"],
                "at-fresh",
            )
            messages = "\n".join(item["message"] for item in snapshot["logs"])
            self.assertIn("无头浏览器自动提取完成", messages)
            self.assertIn("重新校验自动提取的 Session", messages)

    async def test_invalid_token_is_automatically_refreshed_only_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text(
                "# test runtime\n", encoding="utf-8"
            )
            bridge = root / "always_invalid.py"
            bridge.write_text(
                "import json\n"
                "print('HME_VERIFY_EVENT:' + json.dumps({'status':'invalid','detail':'rejected'}), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            save_record(db_file, "retry-once@icloud.com", "at-expired")

            class BrowserManagerStub:
                def __init__(self):
                    self.started = []

                def start(self, accounts, **options):
                    self.started.append((accounts, options))
                    _save_account_record(
                        db_file,
                        "retry-once@icloud.com",
                        result={
                            "access_token": "at-still-invalid",
                            "session_json": json.dumps(
                                {
                                    "accessToken": "at-still-invalid",
                                    "user": {"email": "retry-once@icloud.com"},
                                }
                            ),
                        },
                    )
                    return {"running": True}

                async def wait(self):
                    return {
                        "status": "completed",
                        "accounts": [
                            {
                                "email": "retry-once@icloud.com",
                                "status": "success",
                            }
                        ],
                    }

                async def stop(self):
                    return {"status": "cancelled"}

                def snapshot(self):
                    return {"running": False}

            browser_manager = BrowserManagerStub()
            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
                browser_manager=browser_manager,
            )
            manager.start(concurrency=1)
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["expired"], 1)
            self.assertEqual(len(browser_manager.started), 1)
            self.assertIn("新 Token 仍失效", snapshot["accounts"][0]["message"])

    async def test_preserves_known_plus_account_when_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            bridge = root / "fake_check_bridge.py"
            bridge.write_text(
                "import json\n"
                "print('HME_VERIFY_EVENT:' + json.dumps({'status':'invalid','detail':'both endpoints returned 401'}), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            save_record(
                db_file,
                "paid@icloud.com",
                "at-expired-plus",
                account_type="plus",
            )

            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            manager.start(concurrency=1)
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["plus"], 1)
            self.assertEqual(snapshot["deleted"], 0)
            self.assertEqual(
                load_account_record(db_file, "paid@icloud.com")["password"],
                "Secret!A7",
            )
            self.assertEqual(
                load_account_record(db_file, "paid@icloud.com")["access_token"],
                "at-expired-plus",
            )
            self.assertNotIn("paid@icloud.com", removed_account_emails(db_file))

    async def test_protocol_relogin_replaces_session_without_browser(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            check_bridge = root / "fake_check_bridge.py"
            check_bridge.write_text(
                "import json, os\n"
                "assert os.environ['HME_OPENAI_ACCESS_TOKEN'] == 'at-new'\n"
                "print('HME_VERIFY_EVENT:' + json.dumps({'status':'free','detail':'new session verified'}), flush=True)\n",
                encoding="utf-8",
            )
            protocol_project = root / "protocol"
            (protocol_project / "services").mkdir(parents=True)
            (protocol_project / "services" / "chatgpt-service.js").write_text(
                "// availability marker\n", encoding="utf-8"
            )
            protocol_bridge = root / "fake_protocol_bridge.py"
            protocol_bridge.write_text(
                "import json, os\n"
                "assert os.environ['HME_PROTOCOL_EMAIL'] == 'login@icloud.com'\n"
                "assert os.environ['HME_PROTOCOL_PASSWORD'] == 'Secret!A7'\n"
                "session = {'accessToken':'at-new','user':{'email':'login@icloud.com'},'account':{'planType':'free'}}\n"
                "print('HME_PROTOCOL_EVENT:' + json.dumps({'status':'success','session':session}), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            save_record(db_file, "login@icloud.com", "at-old")

            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=check_bridge,
                protocol_project_dir=protocol_project,
                node_executable=Path(sys.executable),
                protocol_bridge_file=protocol_bridge,
                code_service_url="http://127.0.0.1:8765",
                code_service_token="local-test-token",
            )
            state = manager.start_protocol_relogin(email="LOGIN@ICLOUD.COM")
            self.assertEqual(state["accounts"][0]["message"], "等待协议登录")
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["free"], 1)
            self.assertNotIn("at-new", json.dumps(snapshot))
            self.assertNotIn("Secret!A7", json.dumps(snapshot))
            record = load_account_record(db_file, "login@icloud.com")
            self.assertEqual(record["access_token"], "at-new")
            self.assertEqual(record["session"]["accessToken"], "at-new")
            self.assertEqual(record["session_acquisition_method"], "protocol_login")

    async def test_failed_protocol_relogin_preserves_existing_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            check_bridge = root / "unused_check.py"
            check_bridge.write_text("raise AssertionError('must not run')\n", encoding="utf-8")
            protocol_project = root / "protocol"
            (protocol_project / "services").mkdir(parents=True)
            (protocol_project / "services" / "chatgpt-service.js").write_text(
                "// availability marker\n", encoding="utf-8"
            )
            protocol_bridge = root / "failed_protocol.py"
            protocol_bridge.write_text(
                "import json, sys\n"
                "print('HME_PROTOCOL_EVENT:' + json.dumps({'status':'error','detail':'login rejected'}), flush=True)\n"
                "sys.exit(1)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            save_record(db_file, "keep@icloud.com", "at-keep")

            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=check_bridge,
                protocol_project_dir=protocol_project,
                node_executable=Path(sys.executable),
                protocol_bridge_file=protocol_bridge,
                code_service_url="http://127.0.0.1:8765",
                code_service_token="local-test-token",
            )
            manager.start_protocol_relogin(email="keep@icloud.com")
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["failed"], 1)
            self.assertIn("原 Session 已保留", snapshot["logs"][-2]["message"])
            record = load_account_record(db_file, "keep@icloud.com")
            self.assertEqual(record["access_token"], "at-keep")
            self.assertEqual(record["session"]["accessToken"], "at-keep")

    async def test_protocol_failure_falls_back_to_browser_and_verifies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            check_bridge = root / "check_browser_session.py"
            check_bridge.write_text(
                "import json, os\n"
                "assert os.environ['HME_OPENAI_ACCESS_TOKEN'] == 'at-browser'\n"
                "print('HME_VERIFY_EVENT:' + json.dumps({'status':'free','detail':'browser session verified'}), flush=True)\n",
                encoding="utf-8",
            )
            protocol_project = root / "protocol"
            (protocol_project / "services").mkdir(parents=True)
            (protocol_project / "services" / "chatgpt-service.js").write_text(
                "// availability marker\n", encoding="utf-8"
            )
            protocol_bridge = root / "failed_protocol.py"
            protocol_bridge.write_text(
                "import json, sys\n"
                "print('HME_PROTOCOL_EVENT:' + json.dumps({'status':'error','detail':'auth state failed'}), flush=True)\n"
                "sys.exit(1)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"

            class BrowserManagerStub:
                def __init__(self):
                    self.started = []

                def start(self, accounts, **options):
                    self.started.append((accounts, options))
                    save_record(db_file, "fallback@icloud.com", "at-browser")
                    return {"running": True}

                async def wait(self):
                    return {
                        "status": "completed",
                        "succeeded": 1,
                        "accounts": [
                            {
                                "email": "fallback@icloud.com",
                                "status": "success",
                                "message": "Session / AT 已保存",
                            }
                        ],
                    }

                async def stop(self):
                    return {"status": "cancelled"}

            browser_manager = BrowserManagerStub()
            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=check_bridge,
                protocol_project_dir=protocol_project,
                node_executable=Path(sys.executable),
                protocol_bridge_file=protocol_bridge,
                code_service_url="http://127.0.0.1:8765",
                code_service_token="local-test-token",
                browser_manager=browser_manager,
            )
            manager.start_protocol_relogin(email="fallback@icloud.com", headless=True)
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["free"], 1)
            self.assertEqual(len(browser_manager.started), 1)
            self.assertTrue(browser_manager.started[0][1]["headless"])
            self.assertIn(
                "浏览器已重新获取 Session",
                "\n".join(item["message"] for item in snapshot["logs"]),
            )
            record = load_account_record(db_file, "fallback@icloud.com")
            self.assertEqual(record["access_token"], "at-browser")

    async def test_preserves_fresh_session_plus_when_plan_endpoint_reports_free(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            bridge = root / "fake_check_bridge.py"
            bridge.write_text(
                "import json\n"
                "print('HME_VERIFY_EVENT:' + json.dumps({'status':'free','detail':'has_active_subscription=false'}), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            save_record(
                db_file,
                "paid@icloud.com",
                "at-plus",
                account_type="plus",
            )

            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            manager.start(concurrency=1)
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["plus"], 1)
            self.assertEqual(snapshot["free"], 0)
            record = load_account_record(db_file, "paid@icloud.com")
            self.assertEqual(record["account_type"], "plus")
            self.assertIn("已保留 Plus", record["verification_detail"])

    async def test_can_verify_one_selected_account(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            bridge = root / "fake_check_bridge.py"
            bridge.write_text(
                "import json, os\n"
                "token = os.environ['HME_OPENAI_ACCESS_TOKEN']\n"
                "status = {'at-plus':'plus','at-free':'free'}[token]\n"
                "print('HME_VERIFY_EVENT:' + json.dumps({'status':status,'detail':'selected result'}), flush=True)\n",
                encoding="utf-8",
            )
            db_file = root / "hme.db"
            save_record(db_file, "plus@icloud.com", "at-plus")
            save_record(db_file, "free@icloud.com", "at-free")

            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            state = manager.start(concurrency=1, emails=["FREE@ICLOUD.COM"])
            self.assertEqual(state["total"], 1)
            self.assertEqual(state["accounts"][0]["email"], "free@icloud.com")
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["plus"], 0)
            self.assertEqual(snapshot["free"], 1)
            self.assertEqual(
                load_account_record(db_file, "plus@icloud.com")["account_type"],
                "",
            )
            self.assertEqual(
                load_account_record(db_file, "free@icloud.com")["account_type"],
                "free",
            )

    async def test_selected_account_requires_saved_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            bridge = root / "fake_check_bridge.py"
            bridge.write_text("# unused\n", encoding="utf-8")
            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=root / "hme.db",
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )

            with self.assertRaisesRegex(
                RuntimeError, "所选账号没有可验证的 Session"
            ):
                manager.start(concurrency=1, emails=["new@icloud.com"])

    async def test_rejects_session_owned_by_a_different_account(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            (target / "app_backend.py").write_text("# test runtime\n", encoding="utf-8")
            bridge = root / "unused_bridge.py"
            bridge.write_text("raise AssertionError('must not run')\n", encoding="utf-8")
            db_file = root / "hme.db"
            conn = connect_db(str(db_file))
            try:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    (
                        "gpt_account:expected@icloud.com",
                        json.dumps(
                            {
                                "session": {
                                    "accessToken": "at-wrong-owner",
                                    "user": {"email": "other@icloud.com"},
                                }
                            }
                        ),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            manager = AccountVerificationManager(
                target_project_dir=target,
                db_file=db_file,
                python_executable=Path(sys.executable),
                bridge_file=bridge,
            )
            manager.start(concurrency=1)
            await asyncio.wait_for(manager._batch_task, timeout=10)

            snapshot = manager.snapshot()
            self.assertEqual(snapshot["failed"], 1)
            self.assertEqual(snapshot["accounts"][0]["status"], "failed")
            self.assertIn("Session 账号不匹配", snapshot["accounts"][0]["message"])
            self.assertNotIn("at-wrong-owner", json.dumps(snapshot))


class InvalidConfirmationTests(unittest.TestCase):
    def test_requires_two_independent_unauthenticated_responses(self):
        self.assertFalse(confirmed_invalid("/backend-api/me: HTTP 403"))
        self.assertFalse(
            confirmed_invalid(
                "/backend-api/accounts/check: HTTP 401; /backend-api/me: HTTP 403"
            )
        )
        self.assertFalse(
            confirmed_invalid(
                "/backend-api/accounts/check: HTTP 403; /backend-api/me: HTTP 403"
            )
        )
        self.assertTrue(
            confirmed_invalid(
                "/backend-api/accounts/check/v4-2023-04-27: HTTP 401; "
                "/backend-api/me: HTTP 401"
            )
        )


if __name__ == "__main__":
    unittest.main()
