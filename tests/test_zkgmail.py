import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.webapp import create_app
from hidemyemail_generator.zkgmail import (
    ZkgmailConfigStore,
    ZkgmailMailClient,
    _generate_human_local_part,
    _sync_relevant_messages,
)


class ZkgmailConfigTests(unittest.TestCase):
    def test_authorization_code_is_persisted_but_never_returned(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ZkgmailConfigStore(Path(temp_dir) / "mail.db")

            state = store.configure(authorization_code="local-qq-auth-code")

            self.assertTrue(state["configured"])
            self.assertEqual(state["domain"], "zkgmail.com")
            self.assertEqual(store.load()["authorizationCode"], "local-qq-auth-code")
            self.assertNotIn("authorizationCode", state)


class ZkgmailClientTests(unittest.IsolatedAsyncioTestCase):
    def test_human_local_part_uses_name_and_short_number(self):
        with (
            mock.patch(
                "hidemyemail_generator.zkgmail.secrets.choice",
                side_effect=["emily", "johnson"],
            ),
            mock.patch(
                "hidemyemail_generator.zkgmail.secrets.randbelow",
                side_effect=[1, 0, 17],
            ),
        ):
            local_part = _generate_human_local_part()

        self.assertEqual(local_part, "emily.johnson27")

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
            finally:
                conn.close()

        self.assertEqual(inserted, 1)
        self.assertEqual(
            [(row["hme_address"], row["code"]) for row in rows],
            [(target, "123456")],
        )

    async def test_generated_address_reads_one_exact_forwarded_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "mail.db"
            store = ZkgmailConfigStore(db_file)
            store.configure(authorization_code="local-qq-auth-code")
            client = ZkgmailMailClient(store, sync_interval_seconds=0)
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

            with mock.patch(
                "hidemyemail_generator.zkgmail._sync_relevant_messages", return_value=0
            ):
                first = await client.poll_code(email)
                repeated = await client.poll_code(email)

            self.assertRegex(
                email,
                r"^[a-z]+(?:\.[a-z]+|[a-z]+)\d{2,4}@zkgmail\.com$",
            )
            self.assertEqual(first, "246810")
            self.assertEqual(repeated, "")

    async def test_generated_address_retries_an_existing_human_name(self):
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
                        "emily.johnson27@zkgmail.com",
                        datetime.now(timezone.utc).isoformat(),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            with mock.patch(
                "hidemyemail_generator.zkgmail._generate_human_local_part",
                side_effect=["emily.johnson27", "danielwilson824"],
            ):
                email = await client.acquire_email()

            self.assertEqual(email, "danielwilson824@zkgmail.com")

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

            async def configure(self, authorization_code=None):
                self.codes.append(authorization_code)
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
        self.assertEqual(stub.codes, ["route-secret-code"])
        self.assertTrue(status_payload["configured"])
        self.assertNotIn("authorizationCode", configured_payload)
        self.assertNotIn("authorizationCode", status_payload)


if __name__ == "__main__":
    unittest.main()
