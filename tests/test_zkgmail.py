import asyncio
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.inbox import connect_db, insert_message
from hidemyemail_generator.openai_registration_otp import _is_qq_forwarded_email
from hidemyemail_generator.webapp import create_app
from hidemyemail_generator.zkgmail import (
    DomainLocalPartNamingStrategy,
    ZkgmailConfigStore,
    ZkgmailMailClient,
    _generate_human_local_part,
    _known_zkgmail_aliases,
    _sync_relevant_messages,
)


class ZkgmailConfigTests(unittest.TestCase):
    def test_authorization_code_is_persisted_but_never_returned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ZkgmailConfigStore(Path(temp_dir) / "mail.db")

            state = store.configure(authorization_code="local-qq-auth-code")

            self.assertTrue(state["configured"])
            self.assertEqual(state["domain"], "cclgmail.com")
            self.assertEqual(
                state["domains"],
                ["cclgmail.com", "zkgmail.com", "shukunlabs.xyz", "llhdsczy.com"],
            )
            self.assertEqual(store.load()["authorizationCode"], "local-qq-auth-code")
            self.assertNotIn("authorizationCode", state)
            self.assertNotIn("codeServiceToken", state)

    def test_llhdsczy_local_archive_token_is_persisted_but_never_returned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ZkgmailConfigStore(Path(temp_dir) / "mail.db")

            state = store.configure_code_service("ab" * 32)

            self.assertTrue(state["localArchiveConfigured"])
            self.assertTrue(store.supports_email("signup123@llhdsczy.com"))
            self.assertEqual(store.load()["codeServiceToken"], "ab" * 32)
            self.assertNotIn("codeServiceToken", state)

    def test_domain_can_be_added_and_selected_without_changing_qq_mailbox(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ZkgmailConfigStore(Path(temp_dir) / "mail.db")

            state = store.configure(domain="mail.example.net")

            self.assertFalse(state["configured"])
            self.assertEqual(state["domain"], "mail.example.net")
            self.assertIn("cclgmail.com", state["domains"])
            self.assertEqual(state["forwardAccount"], "35***4@qq.com")

    def test_shukunlabs_domain_is_supported_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ZkgmailConfigStore(Path(temp_dir) / "mail.db")

            self.assertTrue(store.supports_email("gpt-account@shukunlabs.xyz"))

    def test_browser_otp_reader_recognizes_a_saved_custom_forward_domain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "mail.db"
            store = ZkgmailConfigStore(db_file)
            store.configure(domain="codes.example.net")

            with mock.patch.dict(os.environ, {"HME_BROWSER_DB_FILE": str(db_file)}):
                self.assertTrue(_is_qq_forwarded_email("new.user@codes.example.net"))
                self.assertFalse(_is_qq_forwarded_email("new.user@other.example.net"))


class ZkgmailClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_llhdsczy_uses_server_archive_and_consumes_cursor(self):
        calls = []

        def fetcher(url, token, email, cursor):
            calls.append((url, token, email, cursor))
            return (("246810", "cursor-1") if not cursor else ("", cursor))

        with tempfile.TemporaryDirectory() as temp_dir:
            store = ZkgmailConfigStore(Path(temp_dir) / "mail.db")
            store.configure(
                authorization_code="local-qq-auth-code",
                domain="llhdsczy.com",
            )
            store.configure_code_service("ab" * 32)
            client = ZkgmailMailClient(store, remote_code_fetcher=fetcher)
            with mock.patch(
                "hidemyemail_generator.zkgmail._generate_human_local_part",
                return_value="emilyjohnson27",
            ):
                email = await client.acquire_email()

            self.assertEqual(await client.poll_code(email), "246810")
            self.assertEqual(await client.poll_code(email), "")

        self.assertEqual(calls[0][2:], (email, ""))
        self.assertEqual(calls[1][2:], (email, "cursor-1"))
        self.assertTrue(calls[0][0].startswith("https://"))
        self.assertEqual(calls[0][1], "ab" * 32)
    def test_human_local_part_uses_alphanumeric_name_and_short_number(self):
        with (
            mock.patch(
                "hidemyemail_generator.zkgmail.secrets.choice",
                side_effect=["emily", "johnson"],
            ),
            mock.patch(
                "hidemyemail_generator.zkgmail.secrets.randbelow",
                side_effect=[0, 17],
            ),
        ):
            local_part = _generate_human_local_part()

        self.assertEqual(local_part, "emilyjohnson27")
        self.assertRegex(local_part, r"^[a-z]+\d{2,4}$")

    def test_every_domain_uses_the_same_random_human_name_rule(self):
        strategy = DomainLocalPartNamingStrategy()
        generated_names = [
            "emilyjohnson27",
            "danielwilson824",
            "sophiataylor4827",
            "jamesanderson93",
        ]

        with mock.patch(
            "hidemyemail_generator.zkgmail._generate_human_local_part",
            side_effect=generated_names,
        ) as generate:
            local_parts = [
                strategy.generate(domain)
                for domain in (
                    "cclgmail.com",
                    "zkgmail.com",
                    "shukunlabs.xyz",
                    "codes.example.net",
                )
            ]

        self.assertEqual(local_parts, generated_names)
        self.assertEqual(generate.call_count, 4)

    async def test_zkgmail_domain_uses_random_human_name_rule(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "mail.db"
            store = ZkgmailConfigStore(db_file)
            store.configure(
                authorization_code="local-qq-auth-code",
                domain="zkgmail.com",
            )
            client = ZkgmailMailClient(store)

            with mock.patch(
                "hidemyemail_generator.zkgmail._generate_human_local_part",
                return_value="sophiaanderson4827",
            ):
                email = await client.acquire_email("OpenAI 注册")

        self.assertEqual(email, "sophiaanderson4827@zkgmail.com")

    async def test_completion_updates_generated_address_inventory_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "mail.db"
            store = ZkgmailConfigStore(db_file)
            store.configure(authorization_code="local-qq-auth-code")
            client = ZkgmailMailClient(store)

            succeeded = await client.acquire_email("OpenAI 注册")
            await client.complete_email(succeeded, True, "协议注册完成")
            terminal = await client.acquire_email("OpenAI 注册")
            await client.complete_email(
                terminal,
                False,
                "账号不可自动重试：account_deactivated",
            )

            conn = connect_db(str(db_file))
            try:
                states = {
                    row["email"]: row["state"]
                    for row in conn.execute(
                        "SELECT email, state FROM addresses WHERE email IN (?, ?)",
                        (succeeded, terminal),
                    ).fetchall()
                }
            finally:
                conn.close()

        self.assertEqual(states[succeeded], "used")
        self.assertEqual(states[terminal], "trash")

    def test_sync_stores_only_openai_code_for_known_zkgmail_alias(self):
        class MailboxStub:
            def __init__(self, messages):
                self.messages = messages

            def login(self, _username, _password):
                return "OK", []

            def list(self):
                return "OK", []

            def select(self, _folder):
                return "OK", []

            def uid(self, command, uid, _query):
                if command == "search":
                    return "OK", [b"1 2"]
                return "OK", [(b"BODY", self.messages[uid])]

            def logout(self):
                return "BYE", []

        def raw_message(to, subject, body, sender="noreply@openai.com"):
            message = EmailMessage()
            message["To"] = to
            message["From"] = sender
            message["Subject"] = subject
            message["Date"] = datetime.now(timezone.utc)
            message.set_content(body)
            return message.as_bytes()

        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "mail.db"
            store = ZkgmailConfigStore(db_file)
            store.configure(authorization_code="local-qq-auth-code")
            config = store.inbox_config()
            target = "known.alias@zkgmail.com"
            mailbox = MailboxStub(
                {
                    b"1": raw_message(
                        target,
                        "OpenAI verification code",
                        "Your ChatGPT verification code is 123456",
                    ),
                    b"2": raw_message(
                        "352121354@qq.com",
                        "Ordinary private mail",
                        "This message must not be stored",
                        sender="friend@example.com",
                    ),
                }
            )

            with mock.patch(
                "hidemyemail_generator.zkgmail._connect_mailbox",
                return_value=mailbox,
            ):
                inserted = _sync_relevant_messages(
                    config, db_file, {target}, limit=10
                )
            conn = connect_db(str(db_file))
            try:
                rows = conn.execute(
                    "SELECT hme_address, code FROM messages"
                ).fetchall()
                address = conn.execute(
                    "SELECT source FROM addresses WHERE lower(email) = ?",
                    (target,),
                ).fetchone()
            finally:
                conn.close()
            known_aliases = _known_zkgmail_aliases(db_file)

        self.assertEqual(inserted, 1)
        self.assertEqual(
            [(row["hme_address"], row["code"]) for row in rows],
            [(target, "123456")],
        )
        self.assertEqual(address["source"], "zkgmail")
        self.assertIn(target, known_aliases)

    async def test_poll_returns_while_single_flight_sync_is_still_running(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "mail.db"
            store = ZkgmailConfigStore(db_file)
            store.configure(authorization_code="local-qq-auth-code")
            client = ZkgmailMailClient(store, sync_interval_seconds=0)
            email = await client.acquire_email("OpenAI 注册")
            sync_started = threading.Event()
            allow_insert = threading.Event()
            code_inserted = threading.Event()
            allow_finish = threading.Event()
            sync_calls = 0

            def slow_sync(_config, target_db, _aliases, *, limit):
                nonlocal sync_calls
                sync_calls += 1
                sync_started.set()
                allow_insert.wait()
                now = datetime.now(timezone.utc).isoformat()
                conn = connect_db(str(target_db))
                try:
                    insert_message(
                        conn,
                        {
                            "account_key": "qq@imap.qq.com/INBOX",
                            "folder": "INBOX",
                            "uid": "slow-sync-1",
                            "sender": "noreply@openai.com",
                            "recipients": email,
                            "hme_address": email,
                            "subject": "OpenAI verification code",
                            "code": "135790",
                            "body_preview": "Your verification code is 135790",
                            "received_at": now,
                            "created_at": now,
                        },
                    )
                finally:
                    conn.close()
                code_inserted.set()
                allow_finish.wait()
                return 1

            with mock.patch(
                "hidemyemail_generator.zkgmail._sync_relevant_messages",
                side_effect=slow_sync,
            ):
                first_poll = asyncio.create_task(client.poll_code(email))
                sync_task = None
                close_task = None
                try:
                    self.assertTrue(
                        await asyncio.to_thread(sync_started.wait, 1),
                        "后台 IMAP 同步没有启动",
                    )
                    self.assertEqual(
                        await asyncio.wait_for(asyncio.shield(first_poll), 0.1),
                        "",
                    )
                    sync_task = client.sync_task
                    self.assertIsNotNone(sync_task)

                    concurrent = await asyncio.gather(
                        *(client.poll_code(email) for _ in range(5))
                    )
                    self.assertEqual(concurrent, [""] * 5)
                    self.assertEqual(sync_calls, 1)

                    allow_insert.set()
                    self.assertTrue(
                        await asyncio.to_thread(code_inserted.wait, 1),
                        "后台同步没有写入验证码",
                    )
                    self.assertEqual(await client.poll_code(email), "135790")
                    self.assertFalse(sync_task.done())
                    close_task = asyncio.create_task(client.close())
                    await asyncio.sleep(0)
                    self.assertFalse(close_task.done())
                finally:
                    allow_insert.set()
                    allow_finish.set()
                    await asyncio.gather(first_poll, return_exceptions=True)
                    if close_task is not None:
                        await close_task
                    elif sync_task is not None:
                        await asyncio.gather(sync_task, return_exceptions=True)
                self.assertIsNone(client.sync_task)

    async def test_idle_notification_bypasses_cooldown_and_syncs_new_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "mail.db"
            store = ZkgmailConfigStore(db_file)
            store.configure(authorization_code="local-qq-auth-code")
            idle_started = threading.Event()
            emit_idle = threading.Event()
            sync_calls = 0

            def sync_strategy(_config, target_db, _aliases, *, limit):
                nonlocal sync_calls
                del limit
                sync_calls += 1
                if sync_calls != 2:
                    return 0
                now = datetime.now(timezone.utc).isoformat()
                conn = connect_db(str(target_db))
                try:
                    insert_message(
                        conn,
                        {
                            "account_key": "qq-idle",
                            "folder": "INBOX",
                            "uid": "idle-1",
                            "sender": "noreply@openai.com",
                            "recipients": email,
                            "hme_address": email,
                            "subject": "OpenAI verification code",
                            "code": "864209",
                            "body_preview": "Your verification code is 864209",
                            "received_at": now,
                            "created_at": now,
                        },
                    )
                finally:
                    conn.close()
                return 1

            def idle_strategy(_config, stop_event, on_activity):
                idle_started.set()
                if emit_idle.wait(2) and not stop_event.is_set():
                    on_activity()
                stop_event.wait(2)

            client = ZkgmailMailClient(
                store,
                sync_interval_seconds=60,
                sync_strategy=sync_strategy,
                idle_enabled=True,
                idle_strategy=idle_strategy,
            )
            email = await client.acquire_email("OpenAI 注册")
            try:
                self.assertEqual(await client.poll_code(email), "")
                first_sync = client.sync_task
                self.assertIsNotNone(first_sync)
                await first_sync
                self.assertTrue(
                    await asyncio.to_thread(idle_started.wait, 1),
                    "QQ IDLE 监听没有启动",
                )
                self.assertEqual(sync_calls, 1)

                emit_idle.set()
                for _ in range(100):
                    if sync_calls >= 2:
                        break
                    await asyncio.sleep(0.01)
                second_sync = client.sync_task
                if second_sync is not None:
                    await second_sync

                self.assertEqual(sync_calls, 2)
                self.assertEqual(await client.poll_code(email), "864209")
            finally:
                await client.close()

    async def test_poll_surfaces_sync_failure_once_before_retrying(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "mail.db"
            store = ZkgmailConfigStore(db_file)
            store.configure(authorization_code="local-qq-auth-code")
            strategy = mock.Mock(side_effect=OSError("imap unavailable"))
            client = ZkgmailMailClient(
                store,
                sync_interval_seconds=0,
                sync_strategy=strategy,
            )
            email = await client.acquire_email("OpenAI 注册")

            self.assertEqual(await client.poll_code(email), "")
            first_sync = client.sync_task
            self.assertIsNotNone(first_sync)
            await asyncio.gather(first_sync, return_exceptions=True)

            with self.assertRaisesRegex(RuntimeError, "无法连接 QQ 邮箱 IMAP 服务"):
                await client.poll_code(email)

            self.assertEqual(await client.poll_code(email), "")
            retry_sync = client.sync_task
            self.assertIsNotNone(retry_sync)
            await asyncio.gather(retry_sync, return_exceptions=True)
            self.assertEqual(strategy.call_count, 2)

    async def test_configure_clears_a_completed_sync_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "mail.db"
            store = ZkgmailConfigStore(db_file)
            store.configure(authorization_code="old-qq-auth-code")
            strategy = mock.Mock(side_effect=OSError("old connection failed"))
            client = ZkgmailMailClient(
                store,
                sync_interval_seconds=0,
                sync_strategy=strategy,
            )
            email = await client.acquire_email("OpenAI 注册")

            self.assertEqual(await client.poll_code(email), "")
            failed_sync = client.sync_task
            self.assertIsNotNone(failed_sync)
            await asyncio.gather(failed_sync, return_exceptions=True)

            with mock.patch(
                "hidemyemail_generator.zkgmail._test_imap_connection"
            ):
                await client.configure("new-qq-auth-code")

            self.assertEqual(await client.poll_code(email), "")
            retry_sync = client.sync_task
            self.assertIsNotNone(retry_sync)
            await asyncio.gather(retry_sync, return_exceptions=True)

    def test_sync_refreshes_qq_snapshot_without_opening_message(self):
        target = "fresh.alias@zkgmail.com"
        message = EmailMessage()
        message["To"] = target
        message["From"] = "noreply@openai.com"
        message["Subject"] = "OpenAI verification code"
        message["Date"] = datetime.now(timezone.utc)
        message.set_content("Your ChatGPT verification code is 654321")

        class DelayedMailboxStub:
            def __init__(self):
                self.refreshed = False
                self.store_calls = []

            def login(self, _username, _password):
                return "OK", []

            def list(self):
                return "OK", []

            def select(self, _folder):
                return "OK", []

            def noop(self):
                self.refreshed = True
                return "OK", []

            def uid(self, command, uid, _query):
                if command == "search":
                    return "OK", [b"501" if self.refreshed else b""]
                if command == "store":
                    self.store_calls.append((uid, _query))
                    return "OK", []
                return "OK", [(b"BODY", message.as_bytes())]

            def logout(self):
                return "BYE", []

        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "mail.db"
            store = ZkgmailConfigStore(db_file)
            store.configure(authorization_code="local-qq-auth-code")
            mailbox = DelayedMailboxStub()

            with mock.patch(
                "hidemyemail_generator.zkgmail._connect_mailbox",
                return_value=mailbox,
            ):
                inserted = _sync_relevant_messages(
                    store.inbox_config(), db_file, {target}, limit=10
                )

            conn = connect_db(str(db_file))
            try:
                row = conn.execute(
                    "SELECT hme_address, code FROM messages WHERE uid = '501'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(inserted, 1)
        self.assertTrue(mailbox.refreshed)
        self.assertEqual(mailbox.store_calls, [])
        self.assertEqual((row["hme_address"], row["code"]), (target, "654321"))

    async def test_generated_address_reads_one_exact_forwarded_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "mail.db"
            store = ZkgmailConfigStore(db_file)
            store.configure(authorization_code="local-qq-auth-code")
            sync_strategy = mock.Mock(return_value=0)
            client = ZkgmailMailClient(
                store,
                sync_interval_seconds=0,
                sync_strategy=sync_strategy,
            )
            email = await client.acquire_email("OpenAI 注册")
            conn = connect_db(str(db_file))
            try:
                conn.execute(
                    """
                    INSERT INTO messages(
                        account_key, folder, uid, sender, recipients, hme_address,
                        subject, code, body_preview, received_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "352121354@qq.com@imap.qq.com/INBOX",
                        "INBOX",
                        "101",
                        "noreply@openai.com",
                        email,
                        email,
                        "OpenAI verification code",
                        "246810",
                        "Your ChatGPT verification code is 246810",
                        datetime.now(timezone.utc).isoformat(),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            first = await client.poll_code(email)
            sync_strategy.assert_not_called()
            repeated = await client.poll_code(email)
            sync_task = client.sync_task
            self.assertIsNotNone(sync_task)
            await asyncio.gather(sync_task, return_exceptions=True)

            self.assertRegex(
                email,
                r"^[a-z]+\d{2,4}@cclgmail\.com$",
            )
            self.assertEqual(first, "246810")
            self.assertEqual(repeated, "")
            sync_strategy.assert_called_once()

    async def test_saved_address_reads_fresh_code_after_service_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "mail.db"
            store = ZkgmailConfigStore(db_file)
            store.configure(authorization_code="local-qq-auth-code")
            email = "savedaccount90@zkgmail.com"
            requested_at = datetime.now(timezone.utc)
            old_at = requested_at.replace(microsecond=0) - timedelta(hours=2)
            fresh_at = requested_at.replace(microsecond=0)
            conn = connect_db(str(db_file))
            try:
                conn.execute(
                    """
                    INSERT INTO addresses(
                        email, label, state, source, note, is_active,
                        created_at, updated_at
                    ) VALUES (?, '', 'used', 'zkgmail', '', 1, ?, ?)
                    """,
                    (email, old_at.isoformat(), old_at.isoformat()),
                )
                for uid, code, received_at in (
                    ("old", "111111", old_at),
                    ("fresh", "597127", fresh_at),
                ):
                    insert_message(
                        conn,
                        {
                            "account_key": "qq@imap.qq.com/INBOX",
                            "folder": "INBOX",
                            "uid": uid,
                            "sender": "noreply@openai.com",
                            "recipients": email,
                            "hme_address": email,
                            "subject": "OpenAI verification code",
                            "code": code,
                            "body_preview": f"Your verification code is {code}",
                            "received_at": received_at.isoformat(),
                            "created_at": received_at.isoformat(),
                        },
                    )
                conn.commit()
            finally:
                conn.close()

            restarted = ZkgmailMailClient(
                store,
                sync_interval_seconds=0,
                sync_strategy=mock.Mock(return_value=0),
            )
            code = await restarted.poll_next_code(
                email,
                since=requested_at.isoformat(),
            )

            self.assertEqual(code, "597127")

    async def test_saved_address_without_signed_poll_time_stays_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "mail.db"
            store = ZkgmailConfigStore(db_file)
            store.configure(authorization_code="local-qq-auth-code")
            email = "savedaccount90@zkgmail.com"
            now = datetime.now(timezone.utc).isoformat()
            conn = connect_db(str(db_file))
            try:
                conn.execute(
                    """
                    INSERT INTO addresses(
                        email, label, state, source, note, is_active,
                        created_at, updated_at
                    ) VALUES (?, '', 'used', 'zkgmail', '', 1, ?, ?)
                    """,
                    (email, now, now),
                )
                conn.commit()
            finally:
                conn.close()

            restarted = ZkgmailMailClient(store)

            with self.assertRaisesRegex(RuntimeError, "本机取码记录"):
                await restarted.poll_next_code(email)

    async def test_generated_address_retries_an_existing_random_human_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "mail.db"
            store = ZkgmailConfigStore(db_file)
            store.configure(authorization_code="local-qq-auth-code")
            client = ZkgmailMailClient(store)
            conn = connect_db(str(db_file))
            try:
                conn.execute(
                    """
                    INSERT INTO addresses(
                        email, label, state, source, note, is_active,
                        created_at, updated_at
                    ) VALUES (?, '', 'unused', 'zkgmail', '', 1, ?, ?)
                    """,
                    (
                        "emilyjohnson27@cclgmail.com",
                        datetime.now(timezone.utc).isoformat(),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            with mock.patch(
                "hidemyemail_generator.zkgmail._generate_human_local_part",
                side_effect=["emilyjohnson27", "danielwilson824"],
            ):
                email = await client.acquire_email()

            self.assertEqual(email, "danielwilson824@cclgmail.com")

    async def test_configure_verifies_imap_before_saving(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ZkgmailConfigStore(Path(temp_dir) / "mail.db")
            client = ZkgmailMailClient(store)

            with mock.patch(
                "hidemyemail_generator.zkgmail._test_imap_connection"
            ) as test_connection:
                state = await client.configure("verified-qq-code")

            self.assertTrue(state["configured"])
            self.assertEqual(test_connection.call_args.args[0].host, "imap.qq.com")
            self.assertEqual(
                test_connection.call_args.args[0].username, "352121354@qq.com"
            )
            self.assertNotIn("authorizationCode", state)


class ZkgmailWebRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_domain_route_adds_and_selects_custom_qq_forward_domain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/zkgmail/config",
                    json={"domain": "signup.mail.example"},
                    headers={"X-Local-Token": app["local_token"]},
                )
                payload = await response.json()
                status = await client.get("/api/zkgmail/status")
                status_payload = await status.json()
            finally:
                await client.close()

        self.assertEqual(response.status, 200)
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["domain"], "signup.mail.example")
        self.assertIn("cclgmail.com", payload["domains"])
        self.assertEqual(status_payload["domain"], "signup.mail.example")

    async def test_config_and_status_routes_do_not_return_authorization_code(self):
        class ClientStub:
            def __init__(self):
                self.codes = []

            def public_state(self):
                return {
                    "configured": True,
                    "domain": "zkgmail.com",
                    "forwardAccount": "35***4@qq.com",
                }

            async def configure(self, authorization_code=None, domain=None):
                self.codes.append((authorization_code, domain))
                return self.public_state()

        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app(base_dir=Path(temp_dir))
            stub = ClientStub()
            app["zkgmail_client"] = stub
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                headers = {"X-Local-Token": app["local_token"]}
                configured = await client.post(
                    "/api/zkgmail/config",
                    json={"authorizationCode": "route-secret-code"},
                    headers=headers,
                )
                configured_payload = await configured.json()
                status = await client.get("/api/zkgmail/status")
                status_payload = await status.json()
            finally:
                await client.close()

        self.assertEqual(configured.status, 200)
        self.assertEqual(stub.codes, [("route-secret-code", None)])
        self.assertTrue(status_payload["configured"])
        self.assertNotIn("authorizationCode", configured_payload)
        self.assertNotIn("authorizationCode", status_payload)


if __name__ == "__main__":
    unittest.main()
