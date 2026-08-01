import tempfile
import unittest
from pathlib import Path

from hidemyemail_generator.inbox import connect_db, insert_message
from hidemyemail_generator.webapp import (
    GPT_INDEX_HTML,
    _gpt_email_items,
    _gpt_credential,
    _latest_gpt_code,
    _match_relay_identity,
)


IDENTITIES = [
    {
        "anonymousId": "z8qm7qjgk47733",
        "hme": "wombat-uneasy04@icloud.com",
        "isActive": True,
    },
    {
        "anonymousId": "gy89xd5fr45694",
        "hme": "ally-gospels.7v@icloud.com",
        "isActive": True,
    },
    {
        "anonymousId": "8bw8vhsktj5694",
        "hme": "topped.divisor-4z@icloud.com",
        "isActive": True,
    },
]


class GptEmailTests(unittest.TestCase):
    def test_matches_exact_and_obfuscated_icloud_relay_ids(self):
        exact = _match_relay_identity(
            "noreply_at_tm_openai_com_8bw8vhsktj5694_3250c0d4@icloud.com",
            IDENTITIES,
        )
        obfuscated = _match_relay_identity(
            "noreply_at_tm_openai_com_z8qm6e597qjg44_91k47733@icloud.com",
            IDENTITIES,
        )
        self.assertEqual(exact["hme"], "topped.divisor-4z@icloud.com")
        self.assertEqual(obfuscated["hme"], "wombat-uneasy04@icloud.com")

    def test_aggregates_only_gpt_messages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "messages.db"
            conn = connect_db(str(db_file))
            try:
                base = {
                    "account_key": "test",
                    "folder": "INBOX",
                    "recipients": "",
                    "code": "",
                    "body_preview": "",
                    "created_at": "2026-07-31T00:00:00+00:00",
                }
                insert_message(
                    conn,
                    {
                        **base,
                        "uid": "1",
                        "sender": "noreply_at_tm_openai_com_z8qm6e597qjg44_91k47733@icloud.com",
                        "hme_address": "",
                        "subject": "你的临时 ChatGPT 登录代码",
                        "code": "123456",
                        "received_at": "2026-07-31T01:00:00+00:00",
                    },
                )
                insert_message(
                    conn,
                    {
                        **base,
                        "uid": "2",
                        "sender": "noreply_at_tm_openai_com_z8qm6e597qjg44_91k47733@icloud.com",
                        "hme_address": "",
                        "subject": "New sign-in to your OpenAI account",
                        "received_at": "2026-07-31T02:00:00+00:00",
                    },
                )
                insert_message(
                    conn,
                    {
                        **base,
                        "uid": "3",
                        "sender": "news@example.com",
                        "hme_address": "ally-gospels.7v@icloud.com",
                        "subject": "Ordinary newsletter",
                        "received_at": "2026-07-31T03:00:00+00:00",
                    },
                )
            finally:
                conn.close()

            items = _gpt_email_items(db_file, IDENTITIES)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["email"], "wombat-uneasy04@icloud.com")
            self.assertEqual(items[0]["messageCount"], 2)
            self.assertEqual(
                items[0]["latestSubject"], "New sign-in to your OpenAI account"
            )

            code = _latest_gpt_code(
                db_file, "wombat-uneasy04@icloud.com", IDENTITIES
            )
            self.assertEqual(code["code"], "123456")
            self.assertIsNone(
                _latest_gpt_code(
                    db_file,
                    "wombat-uneasy04@icloud.com",
                    IDENTITIES,
                    "2026-07-31T01:00:01+00:00",
                )
            )

            conn = connect_db(str(db_file))
            try:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    (
                        "gpt_account:wombat-uneasy04@icloud.com",
                        '{"access_token":"at-test","session":{"user":"one"}}',
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(
                _gpt_credential(
                    db_file, "wombat-uneasy04@icloud.com", "access_token"
                ),
                "at-test",
            )
            self.assertIn(
                '"user": "one"',
                _gpt_credential(
                    db_file, "wombat-uneasy04@icloud.com", "session"
                ),
            )
            self.assertEqual(
                _gpt_credential(
                    db_file, "wombat-uneasy04@icloud.com", "password"
                ),
                "",
            )

    def test_latest_code_reparses_an_existing_localized_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "messages.db"
            conn = connect_db(str(db_file))
            try:
                insert_message(
                    conn,
                    {
                        "account_key": "test",
                        "folder": "INBOX",
                        "uid": "localized-1",
                        "sender": "noreply_at_tm_openai_com_z8qm6e597qjg44_91k47733@icloud.com",
                        "recipients": "",
                        "hme_address": "",
                        "subject": "ChatGPT 用の一時ログインコード",
                        "code": "",
                        "body_preview": "この一時検証コードを入力して続行してください: 818214",
                        "received_at": "2026-07-31T09:06:55+00:00",
                        "created_at": "2026-07-31T09:06:55+00:00",
                    },
                )
            finally:
                conn.close()

            item = _latest_gpt_code(
                db_file,
                "wombat-uneasy04@icloud.com",
                IDENTITIES,
                "2026-07-31T09:06:30+00:00",
            )
            self.assertEqual(item["code"], "818214")

            conn = connect_db(str(db_file))
            try:
                stored = conn.execute(
                    "SELECT code FROM messages WHERE uid = 'localized-1'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(stored["code"], "818214")

    def test_served_page_contains_only_gpt_list(self):
        self.assertIn("GPT 邮箱列表", GPT_INDEX_HTML)
        self.assertIn("复制邮箱", GPT_INDEX_HTML)
        self.assertIn("复制 AT", GPT_INDEX_HTML)
        self.assertIn("复制 Session", GPT_INDEX_HTML)
        self.assertIn("获取 OpenAI 码", GPT_INDEX_HTML)
        self.assertIn("浏览器取全部", GPT_INDEX_HTML)
        self.assertIn("浏览器获取", GPT_INDEX_HTML)
        self.assertIn("复制密码", GPT_INDEX_HTML)
        self.assertIn("一键验证账号", GPT_INDEX_HTML)
        self.assertIn("一键注册新账号", GPT_INDEX_HTML)
        self.assertIn("复制 2FA 密钥", GPT_INDEX_HTML)
        self.assertIn("复制 2FA 码", GPT_INDEX_HTML)
        self.assertIn("开启 2FA", GPT_INDEX_HTML)
        self.assertIn("删除邮箱", GPT_INDEX_HTML)
        self.assertIn("Plus 账号", GPT_INDEX_HTML)
        self.assertIn("Free 账号", GPT_INDEX_HTML)
        self.assertNotIn("封 OpenAI 邮件", GPT_INDEX_HTML)
        self.assertNotIn("自动识别", GPT_INDEX_HTML)
        self.assertNotIn("已识别", GPT_INDEX_HTML)
        self.assertNotIn("识别到", GPT_INDEX_HTML)
        self.assertNotIn("生成新地址", GPT_INDEX_HTML)
        self.assertNotIn("使用中的地址", GPT_INDEX_HTML)
        self.assertNotIn("收件箱与验证码", GPT_INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
