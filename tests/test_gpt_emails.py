import json
import tempfile
import unittest
from pathlib import Path

from hidemyemail_generator.inbox import connect_db, insert_message
from hidemyemail_generator.webapp import (
    _account_has_confirmed_password,
    GPT_INDEX_HTML,
    _gpt_account_export,
    _gpt_email_items,
    _gpt_credential,
    _latest_gpt_code,
    _match_relay_identity,
    _workbench_import_payload,
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
    def test_two_factor_password_gate_requires_confirmed_password(self):
        self.assertFalse(_account_has_confirmed_password({}))
        self.assertFalse(
            _account_has_confirmed_password(
                {"password": "Generated!Password123", "password_confirmed": False}
            )
        )
        self.assertTrue(
            _account_has_confirmed_password(
                {"password": "Confirmed!Password123", "password_confirmed": True}
            )
        )

    def test_exports_accounts_as_email_password_and_mfa(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "messages.db"
            conn = connect_db(str(db_file))
            try:
                records = {
                    "gpt_account:plus@icloud.com": {
                        "password": "Plus!Password7",
                        "two_factor": {"secret": "JBSWY3DPEHPK3PXP", "enabled": True},
                    },
                    "gpt_account:free@icloud.com": {"password": "Free!Password8"},
                    "gpt_account:no-password@icloud.com": {"access_token": "at-only"},
                    "gpt_account:unconfirmed@icloud.com": {
                        "password": "LocalOnly!Password9",
                        "password_confirmed": False,
                    },
                }
                conn.executemany(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    [
                        (key, json.dumps(value, ensure_ascii=False))
                        for key, value in records.items()
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(
                _gpt_account_export(db_file),
                [
                    "free@icloud.com----Free!Password8----",
                    "plus@icloud.com----Plus!Password7----JBSWY3DPEHPK3PXP",
                ],
            )
            self.assertEqual(
                _gpt_account_export(db_file, "plus@icloud.com"),
                ["plus@icloud.com----Plus!Password7----JBSWY3DPEHPK3PXP"],
            )
            self.assertEqual(
                _gpt_credential(
                    db_file, "unconfirmed@icloud.com", "password"
                ),
                "",
            )

    def test_builds_workbench_import_from_session_only(self):
        payload = _workbench_import_payload(
            {
                "password": "Unique!Password123",
                "password_confirmed": True,
                "two_factor": {
                    "secret": "JBSWY3DPEHPK3PXP",
                    "enabled": True,
                },
                "access_token": "at-test",
                "session": {"user": {"email": "one@icloud.com"}},
            },
            "one@icloud.com",
        )
        self.assertEqual(
            payload,
            {
                "email": "one@icloud.com",
                "session": {
                    "user": {"email": "one@icloud.com"},
                    "accessToken": "at-test",
                },
            },
        )

        with self.assertRaisesRegex(RuntimeError, "尚未保存有效 Session"):
            _workbench_import_payload(
                {
                    "password": "Pending!Password123",
                    "password_confirmed": False,
                    "two_factor": {
                        "secret": "JBSWY3DPEHPK3PXP",
                        "enabled": True,
                    },
                },
                "pending@icloud.com",
            )

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

    def test_latest_code_prefers_newer_portuguese_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "messages.db"
            conn = connect_db(str(db_file))
            try:
                base = {
                    "account_key": "test",
                    "folder": "INBOX",
                    "sender": "noreply@openai.com",
                    "recipients": "wombat-uneasy04@icloud.com",
                    "hme_address": "wombat-uneasy04@icloud.com",
                    "created_at": "2026-08-02T04:10:00+00:00",
                }
                insert_message(
                    conn,
                    {
                        **base,
                        "uid": "older",
                        "subject": "ChatGPT 用の一時ログインコード",
                        "code": "818214",
                        "body_preview": "この一時検証コードを入力してください: 818214",
                        "received_at": "2026-08-02T04:08:00+00:00",
                    },
                )
                insert_message(
                    conn,
                    {
                        **base,
                        "uid": "newer",
                        "subject": "Seu código de entrada temporário do ChatGPT",
                        "code": "",
                        "body_preview": "Informe este código de verificação temporário para continuar: 624813",
                        "received_at": "2026-08-02T04:09:00+00:00",
                    },
                )
            finally:
                conn.close()

            item = _latest_gpt_code(
                db_file, "wombat-uneasy04@icloud.com", IDENTITIES
            )
            self.assertEqual(item["code"], "624813")
            self.assertEqual(item["receivedAt"], "2026-08-02T04:09:00+00:00")

    def test_served_page_contains_only_gpt_list(self):
        self.assertIn("GPT 邮箱列表", GPT_INDEX_HTML)
        self.assertIn("复制邮箱", GPT_INDEX_HTML)
        self.assertIn("复制 AT", GPT_INDEX_HTML)
        self.assertIn("复制 Session", GPT_INDEX_HTML)
        self.assertIn("获取 OpenAI 码", GPT_INDEX_HTML)
        self.assertIn(
            "const since = new Date(Date.now() - 5 * 60_000).toISOString()",
            GPT_INDEX_HTML,
        )
        self.assertIn("JSON.stringify({ email, since })", GPT_INDEX_HTML)
        self.assertIn("查找最近验证码…", GPT_INDEX_HTML)
        self.assertIn("未找到最近 5 分钟发送的验证码", GPT_INDEX_HTML)
        self.assertIn("error.status = response.status", GPT_INDEX_HTML)
        self.assertIn("复制账号", GPT_INDEX_HTML)
        self.assertIn("/api/gpt-accounts/export", GPT_INDEX_HTML)
        self.assertIn('copyAccount(item.email)', GPT_INDEX_HTML)
        self.assertIn("const content = await response.text()", GPT_INDEX_HTML)
        self.assertIn("copyText(content)", GPT_INDEX_HTML)
        self.assertNotIn("下载账号", GPT_INDEX_HTML)
        self.assertNotIn("downloadAccount", GPT_INDEX_HTML)
        self.assertNotIn("URL.createObjectURL", GPT_INDEX_HTML)
        self.assertNotIn('id="downloadAccounts"', GPT_INDEX_HTML)
        self.assertNotIn("list-footer", GPT_INDEX_HTML)
        self.assertIn("最新验证码", GPT_INDEX_HTML)
        self.assertIn("account-code", GPT_INDEX_HTML)
        self.assertNotIn('id="retrievedCode"', GPT_INDEX_HTML)
        self.assertIn("浏览器取全部", GPT_INDEX_HTML)
        self.assertIn("浏览器获取", GPT_INDEX_HTML)
        self.assertIn("复制密码", GPT_INDEX_HTML)
        self.assertIn("一键导入工作台", GPT_INDEX_HTML)
        self.assertIn("/api/account/import-workbench", GPT_INDEX_HTML)
        self.assertIn("if (!item.hasImportableSession)", GPT_INDEX_HTML)
        self.assertIn("请先获取 Session 后再导入工作台", GPT_INDEX_HTML)
        self.assertNotIn("请先完成密码设置并开启 2FA", GPT_INDEX_HTML)
        self.assertIn("重置密码", GPT_INDEX_HTML)
        self.assertIn("reset_password", GPT_INDEX_HTML)
        self.assertIn("验证账号", GPT_INDEX_HTML)
        self.assertIn('actionButton("验证账号"', GPT_INDEX_HTML)
        self.assertIn("将使用协议登录", GPT_INDEX_HTML)
        self.assertIn("重新获取 Session 并验证账号；不会启动浏览器", GPT_INDEX_HTML)
        self.assertIn("使用协议登录重新获取 Session 并验证账号，不会启动浏览器", GPT_INDEX_HTML)
        self.assertNotIn("请先获取 Session 后再验证账号", GPT_INDEX_HTML)
        self.assertNotIn("将启动浏览器完成账号和密码设置", GPT_INDEX_HTML)
        self.assertIn("if (!item.hasPassword)", GPT_INDEX_HTML)
        self.assertIn('actionButton("设置密码"', GPT_INDEX_HTML)
        self.assertIn("根据 Session 验证账号，不设置密码和 2FA", GPT_INDEX_HTML)
        self.assertIn("将只根据 Session 在线检查", GPT_INDEX_HTML)
        self.assertIn("verifyOrRegisterAccount(item)", GPT_INDEX_HTML)
        self.assertIn("/api/account/verify-or-register", GPT_INDEX_HTML)
        self.assertIn("/api/account/type", GPT_INDEX_HTML)
        self.assertIn("account-type-select", GPT_INDEX_HTML)
        self.assertIn("手动更改账号类型", GPT_INDEX_HTML)
        self.assertIn("无法连接本地服务，请刷新页面后重试", GPT_INDEX_HTML)
        self.assertIn("本地服务已重启，正在刷新页面", GPT_INDEX_HTML)
        self.assertIn("一键验证账号", GPT_INDEX_HTML)
        self.assertIn("一键注册新账号", GPT_INDEX_HTML)
        self.assertIn(
            "注册阶段只保存 Session/AT，不设置密码、2FA，也不执行账号验证",
            GPT_INDEX_HTML,
        )
        self.assertNotIn("将创建新的 iCloud 隐藏邮箱", GPT_INDEX_HTML)
        self.assertIn("复制 2FA 密钥", GPT_INDEX_HTML)
        self.assertIn("复制 2FA 码", GPT_INDEX_HTML)
        self.assertIn("开启 2FA", GPT_INDEX_HTML)
        self.assertIn("删除邮箱", GPT_INDEX_HTML)
        self.assertIn("Plus 账号", GPT_INDEX_HTML)
        self.assertIn("Free 账号", GPT_INDEX_HTML)
        self.assertIn('id="dateFilter"', GPT_INDEX_HTML)
        self.assertIn("按添加日期筛选", GPT_INDEX_HTML)
        self.assertIn("今天添加", GPT_INDEX_HTML)
        self.assertIn("添加于", GPT_INDEX_HTML)
        self.assertIn("date-group", GPT_INDEX_HTML)
        self.assertNotIn("选择当前结果", GPT_INDEX_HTML)
        self.assertNotIn("获取选中", GPT_INDEX_HTML)
        self.assertNotIn("封 OpenAI 邮件", GPT_INDEX_HTML)
        self.assertNotIn("自动识别", GPT_INDEX_HTML)
        self.assertNotIn("已识别", GPT_INDEX_HTML)
        self.assertNotIn("识别到", GPT_INDEX_HTML)
        self.assertNotIn("生成新地址", GPT_INDEX_HTML)
        self.assertNotIn("使用中的地址", GPT_INDEX_HTML)
        self.assertNotIn("收件箱与验证码", GPT_INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
