import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hidemyemail_generator.account_deactivation import (
    NOTICE_SETTING_PREFIX,
    extract_deactivated_account_email,
    mark_deactivation_notice_processed,
    pending_deactivation_notices,
)
from hidemyemail_generator.browser_tasks import _save_account_record, load_account_record
from hidemyemail_generator.inbox import (
    InboxConfig,
    connect_db,
    insert_message,
    save_config,
)
from hidemyemail_generator.webapp import (
    DEACTIVATION_SCAN_INTERVAL_SECONDS,
    create_app,
)


TARGET_EMAIL = "napkins-halo6u@icloud.com"


def deactivation_body(email: str = TARGET_EMAIL) -> str:
    return (
        "Hello, We're writing with an important update about your OpenAI account "
        f"associated with {email} (User ID: user-example). "
        "Your account has been deactivated because recent activity violated our "
        "Terms and Usage Policies. This means your account can no longer be used."
    )


def insert_notice(
    db_file: Path,
    *,
    email: str = TARGET_EMAIL,
    folder: str = "INBOX",
    uid: str = "deactivated-1",
    sender: str = "OpenAI <noreply@tm.openai.com>",
    body: str | None = None,
) -> dict:
    record = {
        "account_key": f"icloud@example.com@imap.example.com/{folder}",
        "folder": folder,
        "uid": uid,
        "sender": sender,
        "recipients": "relay@example.com",
        "hme_address": email,
        "subject": "Important update about your OpenAI account",
        "code": "",
        "body_preview": body if body is not None else deactivation_body(email),
        "received_at": "2026-08-11T08:00:00+00:00",
        "created_at": "2026-08-11T08:00:01+00:00",
    }
    conn = connect_db(str(db_file))
    try:
        insert_message(conn, record)
    finally:
        conn.close()
    return record


class DeactivationNoticeParsingTests(unittest.TestCase):
    def test_extracts_icloud_email_from_screenshot_notice(self):
        self.assertEqual(
            extract_deactivated_account_email(
                "OpenAI <noreply@openai.com>",
                "Important update",
                deactivation_body(),
            ),
            TARGET_EMAIL,
        )

    def test_rejects_non_openai_sender(self):
        self.assertEqual(
            extract_deactivated_account_email(
                "OpenAI Support <noreply@example.com>",
                "Important update",
                deactivation_body(),
            ),
            "",
        )

    def test_requires_every_deactivation_marker(self):
        bodies = (
            deactivation_body().replace("OpenAI account associated with", "account for"),
            deactivation_body().replace("Your account has been deactivated", "Your account changed"),
            deactivation_body().replace("your account can no longer be used", "please review it"),
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    extract_deactivated_account_email(
                        "noreply@openai.com", "Important update", body
                    ),
                    "",
                )

    def test_rejects_non_icloud_target(self):
        self.assertEqual(
            extract_deactivated_account_email(
                "noreply@openai.com",
                "Important update",
                deactivation_body("person@example.com"),
            ),
            "",
        )

    def test_processed_uid_is_not_returned_again(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hidemyemail.db"
            insert_notice(db_file, folder="Junk", uid="junk-42")
            notices = pending_deactivation_notices(db_file)
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0]["folder"], "Junk")
            mark_deactivation_notice_processed(
                db_file,
                notices[0],
                email=TARGET_EMAIL,
                status="account_missing",
                detail="already absent",
            )
            self.assertEqual(pending_deactivation_notices(db_file), [])


class DeactivationScannerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def configured_app(root: Path, *, interval: float = 300):
        save_config(
            InboxConfig(
                host="imap.example.com",
                port=993,
                username="inbox@example.com",
                password="app-password",
            ),
            str(root / "inbox_config.json"),
        )
        return create_app(
            base_dir=root,
            deactivation_scan_interval_seconds=interval,
        )

    async def test_scan_deletes_only_matching_account_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = self.configured_app(root)
            _save_account_record(
                app["db_file"],
                TARGET_EMAIL,
                password="Target!Password123",
                password_confirmed=True,
            )
            _save_account_record(
                app["db_file"],
                "survivor@icloud.com",
                password="Survivor!Password123",
                password_confirmed=True,
            )

            def sync_notice(_config, db_file, _limit):
                return [
                    insert_notice(
                        Path(db_file), folder="Junk", uid="junk-deactivated-7"
                    )
                ]

            async def delete_target(email: str, _reason: str) -> str:
                conn = connect_db(str(app["db_file"]))
                try:
                    conn.execute(
                        "DELETE FROM settings WHERE key = ?",
                        (f"gpt_account:{email}",),
                    )
                    conn.execute(
                        "UPDATE addresses SET state = 'trash' WHERE lower(email) = ?",
                        (email,),
                    )
                    conn.commit()
                finally:
                    conn.close()
                return "deleted"

            app["deactivation_delete_account"] = delete_target
            with mock.patch(
                "hidemyemail_generator.webapp.sync_inbox", side_effect=sync_notice
            ):
                first = await app["deactivation_scan_once"](force_sync=True)
                second = await app["deactivation_scan_once"](force_sync=True)

            self.assertEqual(first["deleted"], 1)
            self.assertEqual(second["deleted"], 0)
            self.assertEqual(load_account_record(app["db_file"], TARGET_EMAIL), {})
            self.assertTrue(
                load_account_record(app["db_file"], "survivor@icloud.com")
            )
            conn = connect_db(str(app["db_file"]))
            try:
                removed = conn.execute(
                    "SELECT value FROM settings WHERE key = ?",
                    (f"gpt_removed:{TARGET_EMAIL}",),
                ).fetchone()
                marker_count = conn.execute(
                    "SELECT COUNT(*) FROM settings WHERE key LIKE ?",
                    (f"{NOTICE_SETTING_PREFIX}%",),
                ).fetchone()[0]
                state = conn.execute(
                    "SELECT state FROM addresses WHERE email = ?", (TARGET_EMAIL,)
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(json.loads(removed["value"])["source"], "openai_deactivation_email")
            self.assertEqual(marker_count, 1)
            self.assertEqual(state, "trash")

    async def test_running_account_task_defers_sync_and_deletion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = self.configured_app(Path(temp_dir))
            app["registration_manager"].snapshot = lambda: {"running": True}
            with mock.patch("hidemyemail_generator.webapp.sync_inbox") as sync:
                result = await app["deactivation_scan_once"](force_sync=True)
            self.assertEqual(result["status"], "deferred")
            sync.assert_not_called()

    async def test_imap_failure_is_recorded_without_stopping_scanner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = self.configured_app(Path(temp_dir))
            with mock.patch(
                "hidemyemail_generator.webapp.sync_inbox",
                side_effect=RuntimeError("AUTHENTICATIONFAILED"),
            ):
                result = await app["deactivation_scan_once"](force_sync=True)
            self.assertEqual(result["status"], "error")
            self.assertIn("IMAP 登录失败", app["deactivation_scan_state"]["error"])

    async def test_default_interval_is_five_minutes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = self.configured_app(Path(temp_dir))
            self.assertEqual(DEACTIVATION_SCAN_INTERVAL_SECONDS, 300)
            self.assertEqual(
                app["deactivation_scan_state"]["intervalSeconds"], 300
            )


if __name__ == "__main__":
    unittest.main()
