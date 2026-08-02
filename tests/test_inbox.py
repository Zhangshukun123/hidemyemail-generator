import unittest

from hidemyemail_generator.inbox import extract_verification_code


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
