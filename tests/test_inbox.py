import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hidemyemail_generator.inbox import (
    InboxConfig,
    extract_verification_code,
    sync_inbox,
)


class FakeMailbox:
    def __init__(self):
        self.fetched_uids = []

    def login(self, _username, _password):
        return "OK", []

    def select(self, _folder):
        return "OK", []

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"1 2 3 4"]
        if command == "fetch":
            self.fetched_uids.append(args[0])
            return "OK", [(b"metadata", b"raw message")]
        raise AssertionError(f"Unexpected IMAP command: {command}")

    def logout(self):
        return "BYE", []


class InboxSyncTests(unittest.TestCase):
    def test_sync_fetches_newest_unseen_messages_first(self):
        mailbox = FakeMailbox()
        config = InboxConfig(
            host="imap.example.com",
            port=993,
            username="user@example.com",
            password="password",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / "inbox.db"
            with (
                patch(
                    "hidemyemail_generator.inbox.imaplib.IMAP4_SSL",
                    return_value=mailbox,
                ),
                patch(
                    "hidemyemail_generator.inbox.message_to_record",
                    return_value={},
                ),
                patch(
                    "hidemyemail_generator.inbox.insert_message",
                    return_value=True,
                ),
            ):
                sync_inbox(config, str(db_file), limit=3)

        self.assertEqual(mailbox.fetched_uids, [b"4", b"3", b"2"])


class VerificationCodeExtractionTests(unittest.TestCase):
    def test_extracts_chinese_verification_code(self):
        self.assertEqual(
            extract_verification_code(
                "你的 ChatGPT 临时验证码",
                "你的验证码是 937455。请勿告诉他人。",
            ),
            "937455",
        )

    def test_extracts_english_verification_code(self):
        self.assertEqual(
            extract_verification_code(
                "Verify your email",
                "Your verification code is AB12CD.",
            ),
            "AB12CD",
        )

    def test_extracts_japanese_verification_code(self):
        self.assertEqual(
            extract_verification_code(
                "ChatGPT 用の一時ログインコード",
                "この一時検証コードを入力して続行してください: 818214",
            ),
            "818214",
        )

    def test_extracts_portuguese_verification_code(self):
        self.assertEqual(
            extract_verification_code(
                "Seu código de entrada temporário do ChatGPT",
                "Informe este código de verificação temporário para continuar: 624813",
            ),
            "624813",
        )

    def test_extracts_codes_from_additional_languages(self):
        examples = [
            ("Il tuo codice temporaneo ChatGPT", "Codice di verifica: 310241"),
            ("Временный код ChatGPT", "Код подтверждения: 310242"),
            ("Tymczasowy kod ChatGPT", "Kod weryfikacyjny: 310243"),
            ("Geçici ChatGPT kodunuz", "Doğrulama kodu: 310244"),
            ("Kode sementara ChatGPT", "Kode verifikasi: 310245"),
            ("Mã ChatGPT tạm thời", "Mã xác minh: 310246"),
            ("رمز ChatGPT المؤقت", "رمز التحقق: 310247"),
            ("รหัส ChatGPT ชั่วคราว", "รหัสยืนยัน: 310248"),
        ]
        for subject, body in examples:
            with self.subTest(subject=subject):
                self.assertEqual(
                    extract_verification_code(subject, body), body[-6:]
                )

    def test_chatgpt_six_digit_fallback_supports_unlisted_languages(self):
        self.assertEqual(
            extract_verification_code(
                "ChatGPT уақытша кіру коды",
                "Жалғастыру үшін осы кодты енгізіңіз: 310249",
            ),
            "310249",
        )

    def test_six_digit_fallback_requires_trusted_product_name(self):
        self.assertEqual(
            extract_verification_code(
                "Security alert",
                "A sign-in event has reference number 310250.",
            ),
            "",
        )

    def test_ignores_year_in_security_notification(self):
        self.assertEqual(
            extract_verification_code(
                "New sign-in to your OpenAI account",
                "A sign-in happened on June 27, 2026.",
            ),
            "",
        )

    def test_ignores_uppercase_words_near_dates(self):
        self.assertEqual(
            extract_verification_code(
                "NGC API paths retiring September 30",
                "NVIDIA account notices can mention 2026 without containing a code.",
            ),
            "",
        )

    def test_ignores_plain_account_numbers(self):
        self.assertEqual(
            extract_verification_code(
                "Security alert for user",
                "Account 1067452334 signed in.",
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()
