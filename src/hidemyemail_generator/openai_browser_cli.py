"""Command-line orchestration for one OpenAI browser task."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def run(api) -> int:
    args = api.build_parser().parse_args()
    source_dir = Path(args.source_dir).resolve()
    if not (source_dir / "app_backend.py").is_file():
        api.emit("error", error=f"目标项目缺少 app_backend.py：{source_dir}")
        return 2
    sys.path.insert(0, str(source_dir))

    password = os.environ.get("HME_OPENAI_PASSWORD", "")
    ensure_password = os.environ.get("HME_ENSURE_OPENAI_PASSWORD", "") == "1"
    saved_password_confirmed = (
        os.environ.get("HME_OPENAI_PASSWORD_CONFIRMED", "") == "1"
    )
    enable_2fa = os.environ.get("HME_ENABLE_OPENAI_2FA", "") == "1"
    cookie_refresh_only = os.environ.get("HME_COOKIE_SESSION_REFRESH", "") == "1"
    manual_otp_entry = os.environ.get("HME_MANUAL_OTP_ENTRY", "") == "1"
    gmail_account = (
        args.email.strip().lower().endswith("@gmail.com")
        and bool(ensure_password)
    )
    gmail_registration = (
        not manual_otp_entry
        and gmail_account
    )
    password_first_required = (
        os.environ.get("HME_PASSWORD_FIRST_REQUIRED", "") == "1" or gmail_account
    )
    strict_gmail_credentials = gmail_account
    foreground_required = os.environ.get("HME_BROWSER_FOREGROUND_REQUIRED", "") == "1"
    browser_headless = bool(args.headless and not foreground_required)
    saved_storage_state = api.load_saved_storage_state(
        os.environ.get("HME_BROWSER_DB_FILE", ""),
        args.email,
    )
    saved_cookie_count = len(saved_storage_state.get("cookies") or [])
    if cookie_refresh_only and not saved_cookie_count:
        api.emit("error", error="该账号尚未保存可用 Cookie，请先重新登录或注册")
        return 2
    try:
        pending_2fa = json.loads(os.environ.get("HME_OPENAI_2FA_STATE", "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        pending_2fa = {}
    account = None
    try:
        api.ensure_tkinter_importable()
        # Keep the temporary directory alive for the complete browser run.
        _camoufox_runtime = api.prepare_writable_camoufox_fontconfig()
        if _camoufox_runtime is not None:
            api.emit(
                "log", message="[运行环境] Camoufox fontconfig 已切换到可写临时目录"
            )
        import app_backend
        from account_models import MailAccount

        app_backend.HotmailOtpReader = api.ManualOtpReader
        if not manual_otp_entry:
            api.configure_registration_otp_reader(app_backend, args.email)
        api.configure_windowed_camoufox(app_backend)
        app_backend.OpenAIRegisterPayLinkWorker._force_fill_locator = (
            api.resilient_force_fill_locator
        )
        account = MailAccount(
            email=args.email.strip().lower(),
            password=password,
            client_id="manual",
            refresh_token="manual",
            raw="",
        )
        proxy_url = str(os.environ.get("HME_REGISTRATION_PROXY_URL") or "").strip()
        required_proxy = str(
            os.environ.get("HME_REGISTRATION_PROXY_REQUIRED") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        expected_proxy_country = (
            str(os.environ.get("HME_REGISTRATION_PROXY_COUNTRY") or "").strip().upper()
        )
        if required_proxy and not proxy_url:
            raise RuntimeError("注册动态代理已设为必需，但未分配代理")
        proxy = app_backend.ProxyConfig(
            local_proxy="", dynamic_proxy=proxy_url, chain_url=""
        )

        def log(message: str) -> None:
            api.emit("log", message=api.safe_log_message(message))

        worker = app_backend.OpenAIRegisterPayLinkWorker(
            account,
            "",
            browser_headless,
            proxy,
            proxy,
            log,
            browser_engine="camoufox",
        )
        diagnostics_dir = str(
            os.environ.get("HME_BROWSER_DIAGNOSTICS_DIR")
            or (
                Path(os.environ.get("HME_BROWSER_DB_FILE", ".")).resolve().parent
                / "output"
                / "browser-diagnostics"
            )
        )
        api.configure_password_readiness_diagnostics(
            worker,
            diagnostics_dir=diagnostics_dir,
        )
        if saved_cookie_count:
            api.emit(
                "log",
                message=(
                    f"[Cookie] 已载入 {saved_cookie_count} 个保存 Cookie；"
                    "优先复用登录态，只有 OpenAI 要求最近认证时才重新登录"
                ),
            )
        if cookie_refresh_only:

            def reject_cookie_fallback(*_args, **_kwargs):
                raise RuntimeError("保存的 Cookie 已失效，请重新登录后再刷新账号")

            worker._register = reject_cookie_fallback
        direct_location = {"country": "", "locale": "", "timezone": ""}
        if proxy_url:
            health = worker._prepare_fingerprint_for_proxy(
                proxy, "注册", check_stripe=False
            )
            actual_country = api.require_registration_proxy_country(
                health, expected_proxy_country
            )
            api.emit(
                "log",
                message=(
                    f"[代理] 注册出口国家已确认：{actual_country or '未知'}；"
                    "本账号注册、2FA 与 Session 获取保持同一粘性代理"
                ),
            )
        else:
            api.emit("log", message="[代理] 注册动态代理未启用，使用本地直连")
            direct_location = api.detect_direct_registration_location(app_backend, log)
        api.configure_password_first_login(
            worker,
            enabled=ensure_password,
            required=password_first_required,
        )
        api.configure_email_verification_priority(worker)
        api.configure_email_password_only_registration(
            worker,
            enabled=password_first_required,
        )
        api.configure_chatgpt_home_login_entry(worker)
        api.configure_security_challenge_monitoring(worker)
        if api.configure_manual_browser_verification(
            worker,
            enabled=manual_otp_entry,
        ):
            api.emit(
                "log",
                message=(
                    "自有邮箱已启用浏览器手动验证码模式；"
                    "输入验证码并点击继续后，程序会自动完成后续操作"
                ),
            )
        if foreground_required:
            api.emit(
                "log",
                message="当前流程已选择前台浏览器；交互前会自动激活窗口",
            )
        else:
            api.emit(
                "log",
                message="Camoufox 已启用后台加载与交互；窗口被遮挡时任务仍会继续",
            )
        if api.configure_direct_registration_browser(
            worker,
            enabled=not bool(proxy_url),
            locale=str(direct_location.get("locale") or ""),
        ):
            country = str(direct_location.get("country") or "未知")
            locale = str(direct_location.get("locale") or "GeoIP 自动")
            timezone_name = str(direct_location.get("timezone") or "自动")
            api.emit(
                "log",
                message=(
                    "[直连] 未配置本次注册代理；浏览器使用本机公网 IP，"
                    f"出口国家 {country}，语言 {locale}，时区 {timezone_name}"
                ),
            )
        slot_index, slot_count = api._browser_window_slot_from_environment()
        if slot_count > 1 and not browser_headless:
            api.emit_browser_diagnostic(
                lambda line: api.emit("log", message=line),
                api.BrowserDiagnosticCode.WINDOW_TILING_ENABLED,
                f"当前浏览器使用独立窗口槽位 {slot_index + 1}/{slot_count}",
                slot=slot_index,
                slots=slot_count,
            )
        elif not browser_headless:
            api.emit_browser_diagnostic(
                lambda line: api.emit("log", message=line),
                api.BrowserDiagnosticCode.WINDOW_SINGLE_STABLE,
                "单窗口已启用启动期窗口发现与 Windows 前台激活",
                slot=0,
                slots=1,
            )
        api.configure_worker_login_totp(worker, pending_2fa)
        api.configure_registration_profile_capture(app_backend, worker)
        api.configure_resilient_about_you_input(worker)
        api.configure_resilient_registration_navigation(worker)
        api.configure_existing_account_two_factor(
            worker,
            enabled=bool(enable_2fa and saved_password_confirmed),
            pending_two_factor=pending_2fa,
        )
        worker.existing_login_only = bool(enable_2fa and saved_password_confirmed)
        worker.initial_storage_state = saved_storage_state or None
        # Session/AT are sufficient for this service. Camoufox can occasionally
        # stall indefinitely while exporting a complete browser storage snapshot;
        # the database merge keeps any previously saved snapshot intact.
        worker.skip_storage_state_capture = True
        result = worker.run()
        password_confirmed = api._password_confirmed_for_two_factor(
            worker, saved_password_confirmed
        )
        if strict_gmail_credentials and not password_confirmed:
            raise RuntimeError(
                "Gmail 注册未确认密码；已拒绝保存免密码账号，2FA 未执行"
            )
        if ensure_password and not password_confirmed:
            api.emit(
                "log",
                message=(
                    "OpenAI 免密码注册成功且 Session 已保存；不进入设置页添加密码"
                ),
            )
        if enable_2fa and password_confirmed:
            if (
                not strict_gmail_credentials
                and not getattr(worker, "_hme_account_registered_emitted", False)
            ):
                api.emit(
                    "account_registered",
                    result=result,
                    password=str(account.password or ""),
                    password_confirmed=password_confirmed,
                )
            if getattr(worker, "_hme_two_factor_completed", False):
                two_factor = result.get("two_factor")
            else:
                two_factor = api.reusable_enabled_two_factor(pending_2fa)
            if not getattr(worker, "_hme_two_factor_completed", False) and two_factor:
                api.emit("log", message="账号已有 TOTP 2FA，已保留现有启用状态")
            elif not getattr(worker, "_hme_two_factor_completed", False):
                api.emit("two_factor_start")
                mfa_client = api.MfaHttpClient()
                try:
                    mfa_pending = dict(pending_2fa)

                    def remember_enrolled(state):
                        nonlocal mfa_pending
                        mfa_pending = dict(state)
                        api.emit("two_factor_enrolled", two_factor=state)

                    for mfa_attempt in range(3):
                        try:
                            two_factor = api.enable_totp_mfa(
                                mfa_client,
                                access_token=str(result.get("access_token") or ""),
                                email=str(account.email or ""),
                                pending=mfa_pending,
                                on_enrolled=remember_enrolled,
                            )
                            break
                        except api.MfaSetupError as error:
                            if (
                                not api._mfa_token_was_invalidated(error)
                                or mfa_attempt >= 2
                            ):
                                raise
                            api.emit(
                                "log",
                                message=(
                                    "[2FA] 当前 Token 不满足最近认证要求，"
                                    f"正在完整重新登录后重试（{mfa_attempt + 1}/2）"
                                ),
                            )
                            retained_context = api._retained_registration_context(
                                app_backend, str(account.email or "")
                            )
                            if retained_context is None:
                                raise RuntimeError(
                                    "2FA Token 需要最近认证，但当前登录浏览器不可用"
                                ) from error
                            refreshed = api._reauthenticate_for_mfa(
                                worker, retained_context
                            )
                            result.update(refreshed)
                            if not strict_gmail_credentials:
                                api.emit(
                                    "account_registered",
                                    result=result,
                                    password=str(account.password or ""),
                                    password_confirmed=password_confirmed,
                                )
                finally:
                    mfa_client.close()
            if not getattr(worker, "_hme_two_factor_completed", False):
                result["two_factor"] = two_factor
                api.emit("two_factor_enabled")
        elif enable_2fa:
            api.emit("log", message="[2FA] 密码尚未设置成功，已跳过开启 2FA")
        two_factor_state = (
            result.get("two_factor") if isinstance(result, dict) else None
        )
        if strict_gmail_credentials and not (
            isinstance(two_factor_state, dict)
            and bool(two_factor_state.get("enabled"))
        ):
            raise RuntimeError(
                "Gmail 注册未确认 TOTP 2FA 已开启；已拒绝保存该账号"
            )
        api.emit(
            "result",
            result=result,
            password=str(account.password or ""),
            password_confirmed=password_confirmed,
        )
        return 0
    except api.FreshFingerprintRequiredError as error:
        api.emit(
            "fresh_fingerprint_required",
            reason=api.safe_log_message(str(error)),
            password=str(getattr(account, "password", "") or ""),
            password_confirmed=api._password_confirmed_for_two_factor(
                locals().get("worker"), saved_password_confirmed
            ),
        )
        return 75
    except KeyboardInterrupt:
        api.emit(
            "error",
            error="浏览器任务已停止",
            password=str(getattr(account, "password", "") or ""),
            password_confirmed=False,
        )
        return 130
    except Exception as error:
        api.emit(
            "error",
            error=api.safe_log_message(str(error)),
            password=str(getattr(account, "password", "") or ""),
            password_confirmed=api._password_confirmed_for_two_factor(
                locals().get("worker"), saved_password_confirmed
            ),
        )
        return 1
