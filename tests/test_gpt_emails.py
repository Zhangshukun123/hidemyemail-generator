import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from hidemyemail_generator.browser_tasks import _save_account_record
from hidemyemail_generator.inbox import connect_db, insert_message
from hidemyemail_generator.webapp import (
    _account_has_confirmed_password,
    _browser_email_items,
    GPT_INDEX_HTML,
    _gpt_account_export,
    _gpt_email_items,
    _gpt_credential,
    _effective_gpt_code_since,
    _latest_gpt_code,
    _claim_single_unmapped_gpt_code,
    _inbox_error_message,
    _match_relay_identity,
    _save_account_card_link,
    _wait_for_shared_inbox_code_sync,
    _workbench_import_payload,
    _valid_supported_account_email,
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


class InboxCodeSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_code_polls_reuse_one_shielded_inbox_sync(self):
        state = {"inbox_code_sync_task": None}
        release = asyncio.Event()
        calls = 0

        async def slow_sync():
            nonlocal calls
            calls += 1
            await release.wait()

        self.assertFalse(
            await _wait_for_shared_inbox_code_sync(
                state,
                slow_sync,
                wait_seconds=0.01,
            )
        )
        first_task = state["inbox_code_sync_task"]
        self.assertFalse(first_task.done())
        self.assertFalse(
            await _wait_for_shared_inbox_code_sync(
                state,
                slow_sync,
                wait_seconds=0.01,
            )
        )
        self.assertIs(state["inbox_code_sync_task"], first_task)
        self.assertEqual(calls, 1)

        release.set()
        self.assertTrue(
            await _wait_for_shared_inbox_code_sync(
                state,
                slow_sync,
                wait_seconds=1,
            )
        )
        self.assertIsNone(state["inbox_code_sync_task"])
        self.assertEqual(calls, 1)


class GptEmailTests(unittest.TestCase):
    def test_claims_one_fresh_unmapped_code_for_waiting_registration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "messages.db"
            conn = connect_db(str(db_file))
            try:
                insert_message(
                    conn,
                    {
                        "account_key": "test",
                        "folder": "INBOX",
                        "uid": "fresh-unmapped",
                        "sender": "noreply_at_tm_openai_com_unknown@icloud.com",
                        "recipients": "",
                        "hme_address": "",
                        "subject": "ChatGPT temporary verification code",
                        "code": "753837",
                        "body_preview": "Enter this verification code to continue: 753837",
                        "received_at": "2026-08-12T08:15:13+00:00",
                        "created_at": "2026-08-12T08:15:13+00:00",
                    },
                )
            finally:
                conn.close()

            item = _claim_single_unmapped_gpt_code(
                db_file,
                "precept.wildest_3g@icloud.com",
                "2026-08-12T08:14:18+00:00",
            )

            self.assertEqual(item["code"], "753837")
            conn = connect_db(str(db_file))
            try:
                mapped = conn.execute(
                    "SELECT hme_address FROM messages WHERE uid = 'fresh-unmapped'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(
                mapped["hme_address"], "precept.wildest_3g@icloud.com"
            )
            self.assertIsNone(
                _claim_single_unmapped_gpt_code(
                    db_file,
                    "precept.wildest_3g@icloud.com",
                    "2026-08-12T08:14:18+00:00",
                )
            )

    def test_does_not_guess_between_multiple_unmapped_codes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "messages.db"
            conn = connect_db(str(db_file))
            try:
                for index, code in enumerate(("123456", "654321"), start=1):
                    insert_message(
                        conn,
                        {
                            "account_key": "test",
                            "folder": "INBOX",
                            "uid": f"ambiguous-{index}",
                            "sender": "noreply_at_tm_openai_com_unknown@icloud.com",
                            "recipients": "",
                            "hme_address": "",
                            "subject": "ChatGPT temporary verification code",
                            "code": code,
                            "body_preview": f"Verification code: {code}",
                            "received_at": f"2026-08-12T08:15:1{index}+00:00",
                            "created_at": f"2026-08-12T08:15:1{index}+00:00",
                        },
                    )
            finally:
                conn.close()

            self.assertIsNone(
                _claim_single_unmapped_gpt_code(
                    db_file,
                    "precept.wildest_3g@icloud.com",
                    "2026-08-12T08:14:18+00:00",
                )
            )

    def test_manual_non_icloud_account_is_visible_without_relay_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "messages.db"
            _save_account_record(
                db_file,
                "352121354@qq.com",
                password="Manual!Password123",
                password_confirmed=True,
            )

            items = _browser_email_items(db_file, [])

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["email"], "352121354@qq.com")
            self.assertTrue(items[0]["hasPassword"])

    def test_card_link_is_saved_and_exposed_on_account_item(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "messages.db"
            conn = connect_db(str(db_file))
            try:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    (
                        "gpt_account:wombat-uneasy04@icloud.com",
                        json.dumps(
                            {
                                "session": {"accessToken": "at-test"},
                                "access_token": "at-test",
                            }
                        ),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            saved = _save_account_card_link(
                db_file,
                "wombat-uneasy04@icloud.com",
                url="https://chatgpt.com/checkout/openai_llc/cs_test_card_link",
                country="US",
                currency="USD",
            )
            items = _browser_email_items(db_file, IDENTITIES[:1])

            self.assertEqual(saved["country"], "US")
            self.assertEqual(
                items[0]["cardLink"],
                "https://chatgpt.com/checkout/openai_llc/cs_test_card_link",
            )
            self.assertEqual(items[0]["cardLinkCurrency"], "USD")

    def test_ph_hosted_card_link_metadata_is_exposed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "messages.db"
            _save_account_record(
                db_file,
                "wombat-uneasy04@icloud.com",
                result={
                    "session": {"accessToken": "at-test"},
                    "access_token": "at-test",
                },
            )

            _save_account_card_link(
                db_file,
                "wombat-uneasy04@icloud.com",
                url="https://chatgpt.com/checkout/openai_ie/oaics_test_hosted",
                country="PH",
                currency="PHP",
                method="ph_hosted",
                payment_link_type="chatgpt_checkout_short",
                checkout_ui_mode="hosted",
                amount="0",
                amount_currency="PHP",
                amount_verification="checkout_update",
                promotion_applied=True,
                promotion_strategy="gpt_link_hosted_create_and_update",
            )
            item = _browser_email_items(db_file, IDENTITIES[:1])[0]

            self.assertEqual(item["cardLinkMethod"], "ph_hosted")
            self.assertEqual(item["cardLinkCountry"], "PH")
            self.assertEqual(item["cardLinkAmount"], "0")
            self.assertEqual(item["cardLinkCheckoutUiMode"], "hosted")
            self.assertTrue(item["cardLinkPromotionApplied"])

    def test_imap_connection_error_explains_registration_code_dependency(self):
        message = _inbox_error_message(TimeoutError())

        self.assertIn("OpenAI 注册验证码无法收取", message)
        self.assertIn("与 2FA 无关", message)

    def test_active_browser_task_excludes_codes_from_earlier_runs(self):
        snapshot = {
            "running": True,
            "startedAt": "2026-08-04T11:31:10+00:00",
            "accounts": [{"email": "Wombat-Uneasy04@icloud.com"}],
        }
        self.assertEqual(
            _effective_gpt_code_since(
                "2026-08-04T11:26:10+00:00",
                "wombat-uneasy04@icloud.com",
                snapshot,
                {
                    "running": True,
                    "startedAt": "2026-08-04T11:31:20+00:00",
                    "accounts": [{"email": "wombat-uneasy04@icloud.com"}],
                },
            ),
            "2026-08-04T11:31:20+00:00",
        )
        self.assertEqual(
            _effective_gpt_code_since(
                "2026-08-04T11:32:00+00:00",
                "wombat-uneasy04@icloud.com",
                snapshot,
            ),
            "2026-08-04T11:32:00+00:00",
        )
        self.assertEqual(
            _effective_gpt_code_since(
                "2026-08-04T11:26:10+00:00",
                "ally-gospels.7v@icloud.com",
                snapshot,
            ),
            "2026-08-04T11:26:10+00:00",
        )

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

    def test_builds_workbench_import_with_confirmed_credentials(self):
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
                "password": "Unique!Password123",
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "session": {
                    "user": {"email": "one@icloud.com"},
                    "accessToken": "at-test",
                },
            },
        )

    def test_workbench_import_accepts_supported_account_email_providers(self):
        self.assertTrue(_valid_supported_account_email("one@icloud.com"))
        self.assertTrue(_valid_supported_account_email("one@gmail.com"))
        self.assertFalse(_valid_supported_account_email("not-an-email"))
        self.assertFalse(_valid_supported_account_email("one@outlook.com"))

    def test_workbench_import_omits_unconfirmed_credentials(self):
        payload = _workbench_import_payload(
            {
                "password": "Pending!Password123",
                "password_confirmed": False,
                "two_factor": {
                    "secret": "JBSWY3DPEHPK3PXP",
                    "enabled": False,
                },
                "session": {
                    "accessToken": "at-pending",
                    "user": {"email": "pending@icloud.com"},
                },
            },
            "pending@icloud.com",
        )

        self.assertEqual(
            payload,
            {
                "email": "pending@icloud.com",
                "session": {
                    "accessToken": "at-pending",
                    "user": {"email": "pending@icloud.com"},
                },
            },
        )

    def test_workbench_import_still_requires_session(self):
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

    def test_plus_workbench_import_targets_plus_group(self):
        payload = _workbench_import_payload(
            {
                "account_type": "plus",
                "session": {
                    "accessToken": "at-plus",
                    "user": {"email": "plus@icloud.com"},
                },
            },
            "plus@icloud.com",
        )

        self.assertEqual(payload["account_type"], "plus")
        self.assertEqual(payload["group"], "Plus")
        self.assertEqual(payload["session"]["accessToken"], "at-plus")

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

    def test_consumed_code_is_not_returned_twice(self):
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
                    "subject": "Your temporary ChatGPT login code",
                    "body_preview": "Use this verification code to continue",
                    "created_at": "2026-08-04T12:00:00+00:00",
                }
                insert_message(
                    conn,
                    {
                        **base,
                        "uid": "first",
                        "code": "123456",
                        "received_at": "2026-08-04T12:00:00+00:00",
                    },
                )
            finally:
                conn.close()

            first = _latest_gpt_code(
                db_file,
                "wombat-uneasy04@icloud.com",
                IDENTITIES,
                consume=True,
            )
            self.assertEqual(first["code"], "123456")
            self.assertIsNone(
                _latest_gpt_code(
                    db_file,
                    "wombat-uneasy04@icloud.com",
                    IDENTITIES,
                    consume=True,
                )
            )

            conn = connect_db(str(db_file))
            try:
                insert_message(
                    conn,
                    {
                        **base,
                        "uid": "second",
                        "code": "654321",
                        "received_at": "2026-08-04T12:01:00+00:00",
                    },
                )
            finally:
                conn.close()

            second = _latest_gpt_code(
                db_file,
                "wombat-uneasy04@icloud.com",
                IDENTITIES,
                consume=True,
            )
            self.assertEqual(second["code"], "654321")

    def test_served_page_contains_only_gpt_list(self):
        self.assertIn("GPT 邮箱列表", GPT_INDEX_HTML)
        self.assertIn("复制邮箱", GPT_INDEX_HTML)
        self.assertIn("复制 AT", GPT_INDEX_HTML)
        self.assertIn("复制 Session", GPT_INDEX_HTML)
        self.assertIn("获取 OpenAI 码", GPT_INDEX_HTML)
        self.assertIn(
            "activeTaskStartedAt",
            GPT_INDEX_HTML,
        )
        self.assertIn("browserStartedAt", GPT_INDEX_HTML)
        self.assertIn("activeBrowserEmails.has(targetEmail)", GPT_INDEX_HTML)
        self.assertIn("verificationStartedAt", GPT_INDEX_HTML)
        self.assertIn("activeVerificationEmails.has(targetEmail)", GPT_INDEX_HTML)
        self.assertIn(
            "new Date(Date.now() - 5 * 60_000).toISOString()", GPT_INDEX_HTML
        )
        self.assertIn("JSON.stringify({ email, since })", GPT_INDEX_HTML)
        self.assertIn("等待本轮验证码…", GPT_INDEX_HTML)
        self.assertIn("查找未使用验证码…", GPT_INDEX_HTML)
        self.assertIn("最近 5 分钟内尚未使用的新验证码", GPT_INDEX_HTML)
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
        self.assertIn("直卡支付", GPT_INDEX_HTML)
        self.assertIn("直卡支付链接", GPT_INDEX_HTML)
        self.assertIn("生成并复制", GPT_INDEX_HTML)
        self.assertIn("打开支付页", GPT_INDEX_HTML)
        self.assertIn("/api/account/card-link", GPT_INDEX_HTML)
        self.assertIn('class="side-nav"', GPT_INDEX_HTML)
        self.assertIn('data-view="accounts"', GPT_INDEX_HTML)
        self.assertIn('data-view="card-links"', GPT_INDEX_HTML)
        self.assertIn('id="accountsView"', GPT_INDEX_HTML)
        self.assertIn('id="cardLinksView"', GPT_INDEX_HTML)
        self.assertIn('id="cardLinkList"', GPT_INDEX_HTML)
        self.assertIn("function renderCardLinks(items)", GPT_INDEX_HTML)
        self.assertIn("直卡提链接", GPT_INDEX_HTML)
        self.assertIn(
            "gpt-link · PH / PHP hosted · 双代理严格 0", GPT_INDEX_HTML
        )
        self.assertIn('id="cardLinkCreateProxy"', GPT_INDEX_HTML)
        self.assertIn('id="cardLinkPromotionProxy"', GPT_INDEX_HTML)
        self.assertIn('method: hosted ? "ph_hosted" : "standard"', GPT_INDEX_HTML)
        self.assertIn("cardLinkExtractionModes", GPT_INDEX_HTML)
        self.assertIn("重新提取严格 0", GPT_INDEX_HTML)
        self.assertNotIn("ph_paypal", GPT_INDEX_HTML)
        self.assertIn("账号、Session 与支付链接分区展示", GPT_INDEX_HTML)
        self.assertNotIn("payment-panel", GPT_INDEX_HTML)
        self.assertNotIn("quick-pay", GPT_INDEX_HTML)
        self.assertIn("/api/account/import-workbench", GPT_INDEX_HTML)
        self.assertIn('data.group === "Plus"', GPT_INDEX_HTML)
        self.assertIn("已导入工作台 Plus 分组", GPT_INDEX_HTML)
        self.assertIn("if (!item.hasImportableSession)", GPT_INDEX_HTML)
        self.assertIn("请先获取 Session 后再导入工作台", GPT_INDEX_HTML)
        self.assertNotIn("请先完成密码设置并开启 2FA", GPT_INDEX_HTML)
        self.assertIn("重置密码", GPT_INDEX_HTML)
        self.assertIn("reset_password", GPT_INDEX_HTML)
        self.assertIn("验证账号", GPT_INDEX_HTML)
        self.assertIn('actionButton("验证账号"', GPT_INDEX_HTML)
        self.assertIn("仅在返回 401/token_invalid 时", GPT_INDEX_HTML)
        self.assertIn("复验成功后覆盖", GPT_INDEX_HTML)
        self.assertIn("refresh_with_cookie: !resetPassword", GPT_INDEX_HTML)
        self.assertIn('data.mode === "refresh_cookie"', GPT_INDEX_HTML)
        self.assertIn('data.mode === "verify"', GPT_INDEX_HTML)
        self.assertIn('data.mode === "refresh_session"', GPT_INDEX_HTML)
        self.assertNotIn("请先获取 Session 后再验证账号", GPT_INDEX_HTML)
        self.assertNotIn("将启动浏览器完成账号和密码设置", GPT_INDEX_HTML)
        self.assertIn("if (!item.hasPassword)", GPT_INDEX_HTML)
        self.assertIn('actionButton("设置密码"', GPT_INDEX_HTML)
        self.assertIn(
            "缺失 Session 时使用无头浏览器，支持多进程", GPT_INDEX_HTML
        )
        self.assertIn("最多并行 ${concurrency} 个进程", GPT_INDEX_HTML)
        self.assertIn("JSON.stringify({ concurrency, emails })", GPT_INDEX_HTML)
        self.assertIn('id="concurrency" type="number" min="1" max="10" value="3"', GPT_INDEX_HTML)
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
            "密码成功后自动开启 2FA，不再进入设置添加密码",
            GPT_INDEX_HTML,
        )
        self.assertNotIn("将创建新的 iCloud 隐藏邮箱", GPT_INDEX_HTML)
        self.assertIn("复制 2FA 密钥", GPT_INDEX_HTML)
        self.assertIn("复制 2FA 码", GPT_INDEX_HTML)
        self.assertNotIn('actionButton("开启 2FA"', GPT_INDEX_HTML)
        self.assertNotIn("enableTwoFactor", GPT_INDEX_HTML)
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

    def test_operation_account_selection_is_highlighted_and_exclusive(self):
        self.assertIn(".email-row.operation-selected", GPT_INDEX_HTML)
        self.assertIn("正在操作", GPT_INDEX_HTML)
        self.assertIn('let selectedOperationEmail = ""', GPT_INDEX_HTML)
        self.assertIn("function syncOperationSelection()", GPT_INDEX_HTML)
        self.assertIn(
            'row.classList.toggle("operation-selected", selected)',
            GPT_INDEX_HTML,
        )
        self.assertIn(
            'selectedOperationEmail = selectedOperationEmail === item.email ? "" : item.email',
            GPT_INDEX_HTML,
        )
        self.assertIn(
            'moreButton.querySelector(".more-action-label").textContent = selected ? "收起操作" : "更多操作"',
            GPT_INDEX_HTML,
        )

    def test_long_account_list_uses_scroll_friendly_rendering(self):
        self.assertIn("scrollbar-gutter: stable", GPT_INDEX_HTML)
        self.assertIn("content-visibility: auto", GPT_INDEX_HTML)
        self.assertIn("contain-intrinsic-size: auto 72px", GPT_INDEX_HTML)
        self.assertNotIn("backdrop-filter", GPT_INDEX_HTML)
        self.assertNotIn("body::before", GPT_INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
