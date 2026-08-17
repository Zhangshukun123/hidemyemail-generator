from __future__ import annotations

import re


CODE_KEYWORDS = re.compile(
    r"验证码|校验码|动态码|安全码|认证码|确认码|临时码|一次性|验证|"
    r"検証コード|確認コード|認証コード|一時コード|ログインコード|ワンタイム|"
    r"인증\s*코드|확인\s*코드|로그인\s*코드|일회용|인증번호|"
    r"verification|verify|code|otp|passcode|security code|confirmation|"
    r"bestätigungscode|sicherheitscode|anmeldecode|"
    r"code de vérification|code de confirmation|code temporaire|"
    r"código de verificación|código de confirmación|código temporal|"
    r"código de verificação|código de confirmação|código temporário|"
    r"codice di verifica|codice di conferma|codice temporaneo|codice di accesso|"
    r"verificatiecode|bevestigingscode|tijdelijke code|inlogcode|"
    r"код подтверждения|проверочный код|временный код|код входа|"
    r"doğrulama kodu|onay kodu|geçici kod|giriş kodu|"
    r"kode verifikasi|kode konfirmasi|kode sementara|kode masuk|"
    r"mã xác minh|mã xác nhận|mã tạm thời|mã đăng nhập|"
    r"รหัสยืนยัน|รหัสตรวจสอบ|รหัสชั่วคราว|รหัสเข้าสู่ระบบ|"
    r"رمز التحقق|رمز التأكيد|رمز مؤقت|رمز تسجيل الدخول",
    re.IGNORECASE,
)
TRUSTED_PRODUCT_RE = re.compile(r"\b(?:chatgpt|openai)\b", re.IGNORECASE)
DIGIT_CODE_RE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
ALNUM_CODE_RE = re.compile(
    r"\b(?=[A-Z0-9]{6,10}\b)(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{6,10}\b"
)


class KeywordCodeExtractor:
    """Score keyword-adjacent OTP candidates and return the strongest match."""

    def extract(self, subject: str, body: str) -> str:
        text = re.sub(r"\s+", " ", f"{subject}\n{body}").strip()
        if not text:
            return ""

        trusted_product_message = bool(TRUSTED_PRODUCT_RE.search(text))
        candidates: list[tuple[int, int, str]] = []
        for regex, base_score in ((DIGIT_CODE_RE, 50), (ALNUM_CODE_RE, 20)):
            for match in regex.finditer(text):
                code = match.group(0)
                start, end = match.span()
                window = text[max(0, start - 80) : min(len(text), end + 80)]
                has_keyword = bool(CODE_KEYWORDS.search(window))
                trusted_fallback = (
                    trusted_product_message and regex is DIGIT_CODE_RE and len(code) == 6
                )
                if not has_keyword and not trusted_fallback:
                    continue
                if re.fullmatch(r"(?:19|20)\d{2}", code):
                    continue
                score = base_score + (30 if has_keyword else 0)
                score += 20 if len(code) == 6 else 5 if len(code) in (4, 5, 7, 8) else 0
                candidates.append((score, -start, code))

        if not candidates:
            return ""
        candidates.sort(reverse=True)
        return candidates[0][2]
