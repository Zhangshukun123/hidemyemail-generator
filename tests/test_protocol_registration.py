import json
import tempfile
import unittest
from pathlib import Path

from hidemyemail_generator.browser_tasks import load_account_record
from hidemyemail_generator.inbox import connect_db
from hidemyemail_generator.protocol_credentials import (
    MFA_BASE_URL,
    PASSWORD_ADD_URL,
    complete_protocol_credentials,
)
from hidemyemail_generator.protocol_registration import ProtocolRegistrationManager


class FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.posts = []
        self.closed = False

    def post(self, url, *, headers, data, timeout):
        payload = json.loads(data)
        self.posts.append((url, payload, headers, timeout))
        if url == PASSWORD_ADD_URL:
            return FakeResponse(200, {"ok": True})
        if url == f"{MFA_BASE_URL}/enroll":
            return FakeResponse(
                200,
                {
                    "secret": "JBSWY3DPEHPK3PXP",
                    "session_id": "mfa-session",
                    "factor": {"id": "factor-1"},
                },
            )
        if url == f"{MFA_BASE_URL}/user/activate_enrollment":
            return FakeResponse(200, {"recovery_codes": ["RECOVERY-1"]})
        return FakeResponse(404, {})

    def close(self):
        self.closed = True


class ProtocolCredentialTests(unittest.TestCase):
    def test_passwordless_result_adds_password_and_activates_totp(self):
        session = FakeSession()

        result = complete_protocol_credentials(
            email="protocol@icloud.com",
            access_token="access-token",
            generated_password="GeneratedPassword!1",
            password_set=False,
            request_session=session,
            now=lambda: 1_700_000_010.0,
        )

        self.assertEqual(result["password"], "GeneratedPassword!1")
        self.assertTrue(result["password_set"])
        self.assertEqual(result["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertTrue(result["two_factor"]["enabled"])
        self.assertEqual(
            [url for url, *_ in session.posts],
            [
                PASSWORD_ADD_URL,
                f"{MFA_BASE_URL}/enroll",
                f"{MFA_BASE_URL}/user/activate_enrollment",
            ],
        )
        activation = session.posts[-1][1]
        self.assertRegex(activation["code"], r"^\d{6}$")


class ProtocolRegistrationManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.db_file = self.base_dir / "hidemyemail.db"
        connection = connect_db(str(self.db_file))
        connection.close()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_success_requires_and_persists_password_two_factor_and_session(self):
        captured = []

        async def runner(payload, on_event):
            captured.append(payload)
            on_event(
                {
                    "stage": "email_verification",
                    "message": "已收到邮箱验证码",
                    "status": "success",
                }
            )
            return {
                "status": "success",
                "email": payload["email"],
                "access_token": "header.payload.signature",
                "session_json": json.dumps(
                    {
                        "accessToken": "header.payload.signature",
                        "sessionToken": "session-token",
                    }
                ),
                "storage_state_json": json.dumps({"cookies": [], "origins": []}),
                "session_acquisition_method": "gptfree_mail_auth",
                "password": "GeneratedPassword!1",
                "two_factor": {
                    "enabled": True,
                    "status": "enabled",
                    "type": "totp",
                    "secret": "JBSWY3DPEHPK3PXP",
                },
            }

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=runner,
        )
        initial = manager.start(
            emails=["protocol@icloud.com"],
            base_url="http://127.0.0.1:8080",
            concurrency=1,
        )
        self.assertTrue(initial["running"])
        final = await manager.wait()

        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["succeeded"], 1)
        self.assertEqual(final["failed"], 0)
        self.assertIn("/api/protocol-registration/code/", captured[0]["code_url"])
        record = load_account_record(self.db_file, "protocol@icloud.com")
        self.assertEqual(record["password"], "GeneratedPassword!1")
        self.assertTrue(record["password_confirmed"])
        self.assertTrue(record["two_factor"]["enabled"])
        self.assertEqual(record["session_acquisition_method"], "gptfree_mail_auth")
        self.assertEqual(record["session"]["sessionToken"], "session-token")

    async def test_incomplete_worker_result_is_failed_and_not_persisted(self):
        async def runner(payload, on_event):
            return {
                "status": "success",
                "email": payload["email"],
                "access_token": "access-token",
                "password": "GeneratedPassword!1",
                "two_factor": {"enabled": False},
            }

        manager = ProtocolRegistrationManager(
            base_dir=self.base_dir,
            db_file=self.db_file,
            worker_runner=runner,
        )
        manager.start(
            emails=["incomplete@icloud.com"],
            base_url="http://127.0.0.1:8080",
        )
        final = await manager.wait()

        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["failed"], 1)
        self.assertEqual(load_account_record(self.db_file, "incomplete@icloud.com"), {})
        self.assertIn("TOTP 2FA", final["accounts"][0]["message"])


if __name__ == "__main__":
    unittest.main()
