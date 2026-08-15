"""Central locale model and presenter for browser registration.

The browser remains free to follow the proxy/IP language.  This module only
translates the rendered page into stable semantic actions, so changing locale
does not change the registration state machine or its one-click safeguards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class RegistrationLocale:
    """Model: immutable text catalog for one registration locale."""

    code: str
    label: str
    direction: str = "ltr"
    aliases: tuple[str, ...] = ()
    email: tuple[str, ...] = ()
    password: tuple[str, ...] = ()
    otp: tuple[str, ...] = ()
    profile: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    security: tuple[str, ...] = ()
    error: tuple[str, ...] = ()
    continue_labels: tuple[str, ...] = ()
    email_submit_labels: tuple[str, ...] = ()
    password_continue_labels: tuple[str, ...] = ()
    resend_labels: tuple[str, ...] = ()
    finish_labels: tuple[str, ...] = ()

    def markers(self, stage: str) -> tuple[str, ...]:
        value = getattr(self, stage, ())
        return tuple(str(marker) for marker in value)

    def action_labels(self, action: str) -> tuple[str, ...]:
        field = {
            "continue": "continue_labels",
            "email_submit": "email_submit_labels",
            "password_continue": "password_continue_labels",
            "resend": "resend_labels",
            "finish": "finish_labels",
        }.get(action, "")
        return tuple(getattr(self, field, ())) if field else ()


_PROFILES = (
    RegistrationLocale(
        code="en-US",
        label="英文",
        aliases=("en", "en-GB", "en-CA", "en-AU", "en-NZ", "en-SG"),
        email=("email address", "continue with email", "create your account"),
        password=(
            "continue with password",
            "create a password",
            "enter your password",
        ),
        otp=(
            "check your inbox",
            "verification code",
            "6-digit code",
            "one-time code",
            "enter the code",
        ),
        profile=("tell us about you", "date of birth", "full name", "about you"),
        completed=("you're all set", "you’re all set", "you are all set"),
        security=(
            "security verification",
            "verify you are human",
            "checking your browser",
            "just a moment",
            "complete the security check",
        ),
        error=(
            "something went wrong",
            "try again later",
            "too many requests",
            "access denied",
            "problem signing in",
        ),
        continue_labels=("Continue",),
        email_submit_labels=("Continue", "Create account", "Create an account"),
        password_continue_labels=("Continue with password",),
        resend_labels=("Resend email", "Resend code"),
        finish_labels=("Finish creating account", "Finish"),
    ),
    RegistrationLocale(
        code="zh-CN",
        label="中文",
        aliases=("zh", "zh-Hans", "zh-SG"),
        email=("电子邮件地址", "邮箱地址", "使用电子邮件继续", "创建账号"),
        password=("使用密码继续", "创建密码", "输入密码"),
        otp=("检查收件箱", "查看收件箱", "验证码", "6 位代码", "一次性验证码"),
        profile=("介绍一下自己", "出生日期", "你的姓名", "您的姓名", "全名"),
        completed=("准备就绪", "一切准备就绪"),
        security=("安全验证", "人机验证", "验证您是真人", "正在检查浏览器"),
        error=("发生错误", "出了点问题", "稍后重试", "请求过多", "拒绝访问"),
        continue_labels=("继续",),
        email_submit_labels=("继续", "继续注册", "创建账号"),
        password_continue_labels=("使用密码继续",),
        resend_labels=("重新发送邮件", "重新发送验证码"),
        finish_labels=("完成帐户创建", "完成账户创建", "完成"),
    ),
    RegistrationLocale(
        code="zh-TW",
        label="繁体中文",
        aliases=("zh-Hant", "zh-HK", "zh-MO"),
        email=("電子郵件地址", "電郵地址", "使用電子郵件繼續", "建立帳戶"),
        password=("使用密碼繼續", "建立密碼", "輸入密碼"),
        otp=("檢查收件匣", "查看收件匣", "驗證碼", "6 位數代碼", "一次性驗證碼"),
        profile=("介紹一下自己", "出生日期", "你的姓名", "您的姓名", "完整姓名"),
        completed=("準備就緒", "一切準備就緒"),
        security=("安全驗證", "人機驗證", "驗證您是真人", "正在檢查瀏覽器"),
        error=("發生錯誤", "出了點問題", "稍後再試", "要求過多", "拒絕存取"),
        continue_labels=("繼續",),
        email_submit_labels=("繼續", "繼續註冊", "建立帳戶"),
        password_continue_labels=("使用密碼繼續",),
        resend_labels=("重新傳送電子郵件", "重新傳送驗證碼"),
        finish_labels=("完成帳戶建立", "完成"),
    ),
    RegistrationLocale(
        code="ja-JP",
        label="日文",
        aliases=("ja",),
        email=("メールアドレス", "メールで続行", "アカウントを作成"),
        password=("パスワードで続行", "パスワードを作成", "パスワードを入力"),
        otp=("受信箱を確認", "検証コード", "確認コード", "6桁のコード", "ワンタイムコード"),
        profile=("あなたについて教えてください", "お名前", "生年月日", "氏名"),
        completed=("準備が完了しました", "すべての準備ができました"),
        security=("セキュリティ確認", "人間であることを確認", "ブラウザを確認しています"),
        error=("問題が発生しました", "エラーが発生", "しばらくしてから", "アクセスが拒否"),
        continue_labels=("続行", "続ける"),
        email_submit_labels=("続行", "続ける", "アカウントを作成"),
        password_continue_labels=("パスワードで続行",),
        resend_labels=("メールを再送信する", "コードを再送信"),
        finish_labels=("アカウントの作成を完了する", "作成を完了", "完了"),
    ),
    RegistrationLocale(
        code="de-DE",
        label="德文",
        aliases=("de", "de-AT", "de-CH"),
        email=("e-mail-adresse", "mit e-mail fortfahren", "konto erstellen"),
        password=("mit passwort fortfahren", "passwort erstellen", "passwort eingeben"),
        otp=("posteingang überprüfen", "bestätigungscode", "6-stelliger code", "einmalcode"),
        profile=("erzähl uns etwas über dich", "geburtsdatum", "vollständiger name"),
        completed=("alles bereit", "du bist startklar"),
        security=("sicherheitsüberprüfung", "bestätigen sie, dass sie ein mensch sind", "browser wird überprüft"),
        error=("etwas ist schiefgelaufen", "versuche es später erneut", "zu viele anfragen", "zugriff verweigert"),
        continue_labels=("Weiter", "Fortfahren"),
        email_submit_labels=("Weiter", "Fortfahren", "Konto erstellen"),
        password_continue_labels=("Mit Passwort fortfahren",),
        resend_labels=("E-Mail erneut senden", "Code erneut senden"),
        finish_labels=("Kontoerstellung abschließen", "Fertig"),
    ),
    RegistrationLocale(
        code="fr-FR",
        label="法文",
        aliases=("fr", "fr-CA", "fr-BE", "fr-CH"),
        email=("adresse e-mail", "continuer avec l’adresse e-mail", "créer votre compte"),
        password=("continuer avec un mot de passe", "créer un mot de passe", "saisissez votre mot de passe"),
        otp=("vérifiez votre boîte de réception", "code de vérification", "code à 6 chiffres", "code à usage unique"),
        profile=("parlez-nous de vous", "date de naissance", "nom complet"),
        completed=("tout est prêt", "vous êtes prêt"),
        security=("vérification de sécurité", "vérifiez que vous êtes humain", "vérification de votre navigateur"),
        error=("une erreur s'est produite", "réessayez plus tard", "trop de demandes", "accès refusé"),
        continue_labels=("Continuer",),
        email_submit_labels=("Continuer", "Créer un compte"),
        password_continue_labels=("Continuer avec un mot de passe",),
        resend_labels=("Renvoyer l'e-mail", "Renvoyer le code"),
        finish_labels=("Terminer la création du compte", "Terminer"),
    ),
    RegistrationLocale(
        code="es-ES",
        label="西班牙文",
        aliases=("es", "es-MX", "es-AR", "es-CL", "es-CO"),
        email=("dirección de correo electrónico", "continuar con correo electrónico", "crea tu cuenta"),
        password=("continuar con contraseña", "crear una contraseña", "introduce tu contraseña"),
        otp=("revisa tu bandeja de entrada", "código de verificación", "código de 6 dígitos", "código de un solo uso"),
        profile=("cuéntanos sobre ti", "fecha de nacimiento", "nombre completo"),
        completed=("todo listo", "ya está todo listo"),
        security=("verificación de seguridad", "comprueba que eres humano", "comprobando tu navegador"),
        error=("algo salió mal", "inténtalo de nuevo más tarde", "demasiadas solicitudes", "acceso denegado"),
        continue_labels=("Continuar",),
        email_submit_labels=("Continuar", "Crear una cuenta"),
        password_continue_labels=("Continuar con contraseña",),
        resend_labels=("Reenviar correo", "Reenviar código"),
        finish_labels=("Finalizar la creación de la cuenta", "Finalizar"),
    ),
    RegistrationLocale(
        code="pt-BR",
        label="葡萄牙文（巴西）",
        aliases=("pt", "pt-PT"),
        email=("endereço de e-mail", "continuar com e-mail", "criar sua conta"),
        password=("continuar com uma senha", "continuar com a senha", "criar uma senha", "digite sua senha"),
        otp=("confira sua caixa de entrada", "código de verificação", "código de 6 dígitos", "código de uso único"),
        profile=("conte-nos sobre você", "data de nascimento", "nome completo"),
        completed=("tudo pronto", "está tudo pronto"),
        security=("verificação de segurança", "verifique se você é humano", "verificando seu navegador"),
        error=("algo deu errado", "tente novamente mais tarde", "muitas solicitações", "acesso negado"),
        continue_labels=("Continuar",),
        email_submit_labels=("Continuar", "Criar conta"),
        password_continue_labels=("Continuar com uma senha", "Continuar com a senha"),
        resend_labels=("Reenviar e-mail", "Reenviar código"),
        finish_labels=("Concluir a criação da conta", "Concluir"),
    ),
    RegistrationLocale(
        code="th-TH",
        label="泰文（泰国）",
        aliases=("th",),
        email=("ที่อยู่อีเมล", "ดำเนินการต่อด้วยอีเมล", "สร้างบัญชีของคุณ"),
        password=("ดำเนินการต่อด้วยรหัสผ่าน", "สร้างรหัสผ่าน", "ป้อนรหัสผ่าน"),
        otp=("ตรวจสอบกล่องข้อความของคุณ", "รหัสยืนยัน", "รหัส 6 หลัก", "รหัสแบบใช้ครั้งเดียว"),
        profile=("บอกเราเกี่ยวกับคุณ", "วันเกิด", "ชื่อเต็ม"),
        completed=("พร้อมแล้ว", "ทุกอย่างพร้อมแล้ว"),
        security=("การตรวจสอบความปลอดภัย", "ยืนยันว่าคุณเป็นมนุษย์", "กำลังตรวจสอบเบราว์เซอร์"),
        error=("เกิดข้อผิดพลาด", "ลองอีกครั้งในภายหลัง", "คำขอมากเกินไป", "การเข้าถึงถูกปฏิเสธ"),
        continue_labels=("ดำเนินการต่อ",),
        email_submit_labels=("ดำเนินการต่อ", "สร้างบัญชี"),
        password_continue_labels=("ดำเนินการต่อด้วยรหัสผ่าน",),
        resend_labels=("ส่งอีเมลซ้ำ", "ส่งรหัสอีกครั้ง"),
        finish_labels=("สร้างบัญชีให้เสร็จสิ้น", "เสร็จสิ้น"),
    ),
    RegistrationLocale(
        code="ko-KR",
        label="韩文",
        aliases=("ko",),
        email=("이메일 주소", "이메일로 계속", "계정 만들기"),
        password=("비밀번호로 계속", "비밀번호 만들기", "비밀번호 입력"),
        otp=("받은편지함을 확인", "인증 코드", "6자리 코드", "일회용 코드"),
        profile=("본인에 대해 알려주세요", "생년월일", "이름"),
        completed=("모든 준비가 완료되었습니다", "준비가 완료"),
        security=("보안 확인", "사람인지 확인", "브라우저 확인 중"),
        error=("문제가 발생했습니다", "나중에 다시 시도", "요청이 너무 많습니다", "액세스가 거부되었습니다"),
        continue_labels=("계속",),
        email_submit_labels=("계속", "계정 만들기"),
        password_continue_labels=("비밀번호로 계속",),
        resend_labels=("이메일 다시 보내기", "코드 다시 보내기"),
        finish_labels=("계정 만들기 완료", "완료"),
    ),
)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))


class RegistrationLocaleRegistry:
    """Strategy registry that resolves locale and builds safe selectors."""

    _STAGES = ("email", "password", "otp", "profile", "completed", "security", "error")

    def __init__(self, profiles: Iterable[RegistrationLocale]) -> None:
        self._profiles = tuple(profiles)
        self._by_code = {profile.code.casefold(): profile for profile in self._profiles}
        self._aliases: dict[str, RegistrationLocale] = {}
        for profile in self._profiles:
            for alias in (profile.code, *profile.aliases):
                self._aliases[self._clean_code(alias)] = profile

    @staticmethod
    def _clean_code(value: str) -> str:
        return str(value or "").strip().replace("_", "-").casefold()

    @property
    def profiles(self) -> tuple[RegistrationLocale, ...]:
        return self._profiles

    def resolve(self, value: str | None) -> RegistrationLocale | None:
        cleaned = self._clean_code(value or "")
        if not cleaned or cleaned in {"auto", "und"}:
            return None
        exact = self._aliases.get(cleaned)
        if exact is not None:
            return exact
        primary = cleaned.split("-", 1)[0]
        return self._aliases.get(primary)

    def normalize(self, value: str | None, *, default: str = "auto") -> str:
        profile = self.resolve(value)
        return profile.code if profile is not None else default

    def detect(
        self,
        text: str,
        *,
        declared_locale: str = "",
    ) -> RegistrationLocale | None:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
        declared = self.resolve(declared_locale)
        if not normalized:
            return declared

        scored: list[tuple[int, int, RegistrationLocale]] = []
        for index, profile in enumerate(self._profiles):
            semantic_markers = (
                marker
                for stage in self._STAGES
                for marker in profile.markers(stage)
            )
            action_markers = (
                *profile.password_continue_labels,
                *profile.resend_labels,
                *profile.finish_labels,
            )
            score = sum(
                1
                for marker in (*semantic_markers, *action_markers)
                if marker.casefold() in normalized
            )
            if score:
                declared_bonus = 1 if profile is declared else 0
                scored.append((score, declared_bonus, profile))
        if not scored:
            return declared
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return scored[0][2]

    def markers(self, stage: str) -> tuple[str, ...]:
        return _unique(
            marker.casefold()
            for profile in self._profiles
            for marker in profile.markers(stage)
        )

    def action_labels(self, action: str) -> tuple[str, ...]:
        return _unique(
            label
            for profile in self._profiles
            for label in profile.action_labels(action)
        )

    def action_selectors(
        self,
        action: str,
        *,
        controls: tuple[str, ...] = ("button", '[role="button"]'),
        text_is: bool = False,
    ) -> tuple[str, ...]:
        operator = "text-is" if text_is else "has-text"
        return _unique(
            f'{control}:{operator}("{label}")'
            for label in self.action_labels(action)
            for control in controls
        )

    def text_selectors(self, stage: str) -> tuple[str, ...]:
        return _unique(
            f'text="{marker}"'
            for profile in self._profiles
            for marker in profile.markers(stage)
        )

    def verification_ui_markers(self) -> dict[str, tuple[str, ...]]:
        return {
            profile.label: _unique(
                (
                    *profile.otp,
                    *profile.resend_labels,
                    *profile.continue_labels,
                    *profile.password_continue_labels,
                )
            )
            for profile in self._profiles
        }


class RegistrationLocalePresenter:
    """Presenter: serializes a resolved locale for state/status views."""

    @staticmethod
    def present(
        profile: RegistrationLocale | None,
        *,
        declared_locale: str = "",
        source: str = "unknown",
    ) -> dict[str, str]:
        if profile is None:
            return {
                "locale": "und",
                "localeLabel": "未确认",
                "textDirection": "ltr",
                "declaredLocale": str(declared_locale or ""),
                "localeSource": "unknown",
            }
        return {
            "locale": profile.code,
            "localeLabel": profile.label,
            "textDirection": profile.direction,
            "declaredLocale": str(declared_locale or ""),
            "localeSource": source,
        }


REGISTRATION_LOCALES = RegistrationLocaleRegistry(_PROFILES)
REGISTRATION_LOCALE_PRESENTER = RegistrationLocalePresenter()


def supported_registration_locales() -> tuple[RegistrationLocale, ...]:
    return REGISTRATION_LOCALES.profiles


def normalize_registration_locale(value: str | None, *, default: str = "auto") -> str:
    return REGISTRATION_LOCALES.normalize(value, default=default)


def detect_registration_locale(
    text: str,
    *,
    declared_locale: str = "",
) -> RegistrationLocale | None:
    return REGISTRATION_LOCALES.detect(text, declared_locale=declared_locale)


def registration_markers(stage: str) -> tuple[str, ...]:
    return REGISTRATION_LOCALES.markers(stage)


def registration_action_labels(action: str) -> tuple[str, ...]:
    return REGISTRATION_LOCALES.action_labels(action)


def registration_action_selectors(
    action: str,
    *,
    controls: tuple[str, ...] = ("button", '[role="button"]'),
    text_is: bool = False,
) -> tuple[str, ...]:
    return REGISTRATION_LOCALES.action_selectors(
        action,
        controls=controls,
        text_is=text_is,
    )


def registration_text_selectors(stage: str) -> tuple[str, ...]:
    return REGISTRATION_LOCALES.text_selectors(stage)


def registration_verification_ui_markers() -> dict[str, tuple[str, ...]]:
    return REGISTRATION_LOCALES.verification_ui_markers()
