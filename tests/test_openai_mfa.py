import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hidemyemail_generator.openai_mfa import (
    MfaSetupError,
    enable_totp_mfa,
    generate_totp,
    provisioning_uri,
)
from hidemyemail_generator.openai_browser_bridge import (
    configure_worker_login_totp,
    load_saved_storage_state,
    reusable_enabled_two_factor,
)


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    def json(self):
        return self.payload

    def text(self):
        return json.dumps(self.payload)


class FakeRequest:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class FakeContext:
    def __init__(self, responses):
        self.request = FakeRequest(responses)


class OpenAiMfaTests(unittest.TestCase):
    def test_saved_browser_state_is_loaded_for_settings_password_flow(self):
        state = {"cookies": [{"name": "session", "value": "hidden"}], "origins": []}
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)",
                    (
                        "gpt_account:user@icloud.com",
                        json.dumps({"storage_state_json": json.dumps(state)}),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            loaded = load_saved_storage_state(str(db_file), "USER@icloud.com")

        self.assertEqual(loaded, state)

    def test_invalid_saved_browser_state_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "hme.db"
            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)",
                    (
                        "gpt_account:user@icloud.com",
                        json.dumps({"storage_state_json": "not-json"}),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(
                load_saved_storage_state(str(db_file), "user@icloud.com"),
                {},
            )

    def test_saved_totp_secret_is_provided_to_login_worker(self):
        worker = type("Worker", (), {})()

        with patch(
            "hidemyemail_generator.openai_browser_bridge.generate_totp",
            return_value="123456",
        ) as generator:
            configured = configure_worker_login_totp(
                worker,
                {"enabled": True, "secret": "JBSWY3DPEHPK3PXP"},
            )
            code = worker.login_totp_provider()

        self.assertTrue(configured)
        self.assertEqual(code, "123456")
        generator.assert_called_once_with("JBSWY3DPEHPK3PXP")

    def test_enabled_two_factor_is_reused_without_reactivation(self):
        state = {
            "enabled": True,
            "status": "enabled",
            "secret": "JBSWY3DPEHPK3PXP",
        }

        self.assertEqual(reusable_enabled_two_factor(state), state)
        self.assertEqual(
            reusable_enabled_two_factor({**state, "enabled": False}), {}
        )

    def test_rfc_totp_vector_uses_six_digits(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        self.assertEqual(generate_totp(secret, now=59), "287082")

    def test_enrolls_persists_then_activates_totp(self):
        secret = "JBSWY3DPEHPK3PXP"
        context = FakeContext(
            [
                FakeResponse(
                    200,
                    {
                        "secret": secret,
                        "session_id": "session-1",
                        "factor": {"id": "factor-1", "factor_type": "totp"},
                    },
                ),
                FakeResponse(200, {"recovery_codes": ["recover-one"]}),
            ]
        )
        enrolled = []
        state = enable_totp_mfa(
            context,
            access_token="at-test",
            email="new@icloud.com",
            on_enrolled=enrolled.append,
        )

        self.assertEqual(enrolled[0]["status"], "enrolled")
        self.assertFalse(enrolled[0]["enabled"])
        self.assertTrue(state["enabled"])
        self.assertEqual(state["status"], "enabled")
        self.assertEqual(state["secret"], secret)
        self.assertEqual(state["recovery_codes"], ["recover-one"])
        self.assertEqual(len(context.request.calls), 2)
        activate_body = json.loads(context.request.calls[1][1]["data"])
        self.assertRegex(activate_body["code"], r"^\d{6}$")
        self.assertNotIn(activate_body["code"], json.dumps(state))

    def test_activation_failure_keeps_enrolled_callback(self):
        secret = "JBSWY3DPEHPK3PXP"
        context = FakeContext(
            [
                FakeResponse(
                    200,
                    {
                        "secret": secret,
                        "session_id": "session-1",
                        "factor": {"id": "factor-1"},
                    },
                ),
                FakeResponse(500, {"detail": "temporary failure"}),
            ]
        )
        enrolled = []
        with self.assertRaises(MfaSetupError):
            enable_totp_mfa(
                context,
                access_token="at-test",
                email="new@icloud.com",
                on_enrolled=enrolled.append,
            )
        self.assertEqual(enrolled[0]["secret"], secret)

    def test_provisioning_uri_contains_account_and_issuer(self):
        uri = provisioning_uri("JBSWY3DPEHPK3PXP", "New@iCloud.com")
        self.assertIn("OpenAI%3Anew%40icloud.com", uri)
        self.assertIn("issuer=OpenAI", uri)


if __name__ == "__main__":
    unittest.main()
