"""Post-login session, password, security-settings, and MFA workflows."""

from __future__ import annotations

import json
import os
import time

try:
    from .openai_bridge_runtime import (
        MfaHttpClient,
        _mfa_token_was_invalidated,
        emit,
        resilient_force_fill_locator,
        reusable_enabled_two_factor,
        safe_log_message,
    )
    from .openai_browser_dom import (
        _click_add_password,
        _click_first_visible,
        _dismiss_completed_onboarding,
        _first_visible,
        _open_settings_from_profile,
        _page_wait,
        _password_is_present,
        _security_settings_ready,
        _visible_locators,
    )
    from .openai_browser_selectors import (
        ACCOUNT_TAB_SELECTORS,
        CHATGPT_ACCOUNT_SETTINGS_URL,
        CHATGPT_HOME_URL,
        CHATGPT_SECURITY_SETTINGS_URL,
        FORGOT_PASSWORD_SELECTORS,
        OTP_INPUT_SELECTORS,
        OTP_SUBMIT_SELECTORS,
        PASSWORD_INPUT_SELECTORS,
        PASSWORD_PRESENT_SELECTORS,
        PASSWORD_SUBMIT_SELECTORS,
        PASSWORD_SUCCESS_SELECTORS,
        SECURITY_TAB_SELECTORS,
    )
    from .openai_mfa import MfaSetupError, enable_totp_mfa
except ImportError:
    from openai_bridge_runtime import (
        MfaHttpClient,
        _mfa_token_was_invalidated,
        emit,
        resilient_force_fill_locator,
        reusable_enabled_two_factor,
        safe_log_message,
    )
    from openai_browser_dom import (
        _click_add_password,
        _click_first_visible,
        _dismiss_completed_onboarding,
        _first_visible,
        _open_settings_from_profile,
        _page_wait,
        _password_is_present,
        _security_settings_ready,
        _visible_locators,
    )
    from openai_browser_selectors import (
        ACCOUNT_TAB_SELECTORS,
        CHATGPT_ACCOUNT_SETTINGS_URL,
        CHATGPT_HOME_URL,
        CHATGPT_SECURITY_SETTINGS_URL,
        FORGOT_PASSWORD_SELECTORS,
        OTP_INPUT_SELECTORS,
        OTP_SUBMIT_SELECTORS,
        PASSWORD_INPUT_SELECTORS,
        PASSWORD_PRESENT_SELECTORS,
        PASSWORD_SUBMIT_SELECTORS,
        PASSWORD_SUCCESS_SELECTORS,
        SECURITY_TAB_SELECTORS,
    )
    from openai_mfa import MfaSetupError, enable_totp_mfa

ICloudOtpReader = None
PASSWORD_ADD_URL = "https://chatgpt.com/api/accounts/password/add"
PASSWORD_ADD_RETRY_DELAYS_SECONDS = (2.0, 5.0)


def _password_add_preconnect_error(error: Exception) -> bool:
    """Return whether the account request failed before HTTP could be sent."""

    message = str(error or "").casefold()
    return any(
        marker in message
        for marker in (
            "before secure tls connection was established",
            "tls handshake",
            "ssl handshake",
            "getaddrinfo",
            "enotfound",
            "eai_again",
            "connection refused",
        )
    )


def _api_response_status(response) -> int:
    try:
        return int(getattr(response, "status", None) or response.status_code)
    except (AttributeError, TypeError, ValueError):
        return 0


def _api_response_detail(response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            value = error.get("message") or error.get("errorMessage")
            if value:
                return str(value)[:300]
        for key in ("detail", "message"):
            if payload.get(key):
                return str(payload[key])[:300]
    try:
        value = response.text
        if callable(value):
            value = value()
        return str(value or "")[:300]
    except Exception:
        return ""


def add_password_via_account_api(
    worker,
    context,
    access_token: str,
    password: str,
    *,
    timeout_seconds: int = 60,
    retry_delays: tuple[float, ...] = PASSWORD_ADD_RETRY_DELAYS_SECONDS,
    sleep=time.sleep,
) -> bool:
    """Add a password after registration with the authenticated account API."""

    target_password = str(password or "")
    if len(target_password) < 12:
        raise RuntimeError("待设置的 OpenAI 密码长度不足 12 位")
    token = str(access_token or "").strip()
    if not token:
        raise RuntimeError("注册成功但尚未取得 Access Token，不能后置密码")
    request = getattr(context, "request", None)
    if request is None or not callable(getattr(request, "post", None)):
        raise RuntimeError("当前浏览器上下文不支持账号密码接口")

    delays = tuple(max(0.0, float(value)) for value in retry_delays)
    total_attempts = len(delays) + 1
    response = None
    for attempt in range(total_attempts):
        try:
            response = request.post(
                PASSWORD_ADD_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Origin": "https://chatgpt.com",
                    "Referer": "https://chatgpt.com/",
                },
                data=json.dumps({"password": target_password}),
                timeout=max(15, int(timeout_seconds)) * 1000,
            )
            break
        except Exception as error:
            retryable = _password_add_preconnect_error(error)
            if not retryable or attempt >= len(delays):
                detail = (
                    f"；TLS 建连重试 {len(delays)} 次后仍失败"
                    if retryable and delays
                    else ""
                )
                worker.log(
                    "[密码] POST /api/accounts/password/add 网络连接失败"
                    + detail
                )
                raise RuntimeError(
                    "POST /api/accounts/password/add 网络连接失败" + detail
                ) from error
            delay = delays[attempt]
            worker.log(
                "[密码] POST /api/accounts/password/add 在 TLS 建连前断开；"
                f"{delay:g} 秒后重试（{attempt + 2}/{total_attempts}）"
            )
            sleep(delay)

    if response is None:
        raise RuntimeError("POST /api/accounts/password/add 未返回响应")
    status = _api_response_status(response)
    if not 200 <= status < 300:
        detail = _api_response_detail(response)
        worker.log(
            f"[密码] POST /api/accounts/password/add 失败：HTTP {status}"
            + (f" · {detail}" if detail else "")
        )
        raise RuntimeError(
            f"POST /api/accounts/password/add 返回 HTTP {status}"
            + (f" · {detail}" if detail else "")
        )
    worker._password_step_submitted = True
    worker.log("[密码] POST /api/accounts/password/add 已确认后置密码成功")
    return True


def _retained_registration_context(app_backend, email: str):
    sessions = getattr(app_backend, "KEPT_REGISTER_BROWSER_SESSIONS", None)
    if not isinstance(sessions, dict):
        return None
    retained = sessions.get(str(email or "").strip().lower())
    if not isinstance(retained, (tuple, list)) or not retained:
        return None
    return retained[0]


def _reuse_single_registration_page(context, worker):
    """Reuse the logged-in registration page and remove leftover tabs."""

    try:
        pages = list(getattr(context, "pages", []) or [])
    except Exception:
        pages = []

    open_pages = []
    for candidate in pages:
        is_closed = getattr(candidate, "is_closed", None)
        try:
            if callable(is_closed) and is_closed():
                continue
        except Exception:
            continue
        open_pages.append(candidate)

    if not open_pages:
        worker.log("[浏览器] 当前上下文没有可复用页面，创建一个页面")
        return context.new_page()

    chatgpt_pages = []
    for candidate in open_pages:
        try:
            url = str(getattr(candidate, "url", "") or "")
        except Exception:
            url = ""
        if url.startswith(CHATGPT_HOME_URL) and "/api/auth/session" not in url:
            chatgpt_pages.append(candidate)

    page = chatgpt_pages[-1] if chatgpt_pages else open_pages[-1]
    closed_count = 0
    for candidate in open_pages:
        if candidate is page:
            continue
        try:
            candidate.close()
            closed_count += 1
        except Exception:
            pass

    try:
        page.bring_to_front()
    except Exception:
        pass
    if closed_count:
        worker.log(f"[浏览器] 已复用注册页面并关闭 {closed_count} 个多余页面")
    else:
        worker.log("[浏览器] 已复用注册页面，不创建新页面")
    return page


def _reauthenticate_for_mfa(
    worker,
    context,
    *,
    extract_session=None,
) -> dict:
    """Perform a real existing-account login before retrying MFA enrollment."""

    extract_session = extract_session or extract_session_without_navigation
    register = getattr(worker, "_register", None)
    if not callable(register):
        raise RuntimeError("当前浏览器工作器不支持为 2FA 重新登录")
    clear_cookies = getattr(context, "clear_cookies", None)
    if callable(clear_cookies):
        clear_cookies()
    page = _reuse_single_registration_page(context, worker)
    worker.log("[2FA] OpenAI 要求最近认证，正在完整重新登录当前账号")
    register(page, context, existing_login_only=True)
    refreshed = extract_session(worker, context)
    if not str(refreshed.get("access_token") or "").strip():
        raise RuntimeError("2FA 重新登录完成后没有获得新的 Access Token")
    worker.log("[2FA] 账号已重新登录，正在使用新 Token 重试激活")
    return refreshed


def _open_security_settings(page, worker) -> bool:
    """Open the current Account password page, with legacy Security fallback."""

    page.goto(CHATGPT_HOME_URL, wait_until="domcontentloaded", timeout=60000)
    _page_wait(page, 1200)
    _dismiss_completed_onboarding(page, worker)

    settings_clicked = _open_settings_from_profile(page, worker)
    if settings_clicked:
        _page_wait(page, 700)
        for tab_name, selectors in (
            ("账户设置", ACCOUNT_TAB_SELECTORS),
            ("旧版安全设置", SECURITY_TAB_SELECTORS),
        ):
            if not _click_first_visible(page, selectors):
                continue
            _page_wait(page, 700)
            if _security_settings_ready(page):
                worker.log(f"[密码] 已确认进入{tab_name}")
                return True
            worker.log(f"[密码] {tab_name}未出现密码入口，继续尝试其他入口")

    worker.log("[密码] 菜单中未找到密码入口，改用账户设置直达地址")
    for target_url in (
        CHATGPT_ACCOUNT_SETTINGS_URL,
        "https://chatgpt.com/#settings/account",
        CHATGPT_SECURITY_SETTINGS_URL,
        "https://chatgpt.com/#settings/security",
        "https://chatgpt.com/#settings",
    ):
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        _page_wait(page, 1000)
        if _dismiss_completed_onboarding(page, worker):
            page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            _page_wait(page, 800)
        _click_first_visible(page, ACCOUNT_TAB_SELECTORS)
        _page_wait(page, 500)
        if _security_settings_ready(page, attempts=3):
            worker.log("[密码] 已通过直达地址确认账户密码页面")
            return True
        _click_first_visible(page, SECURITY_TAB_SELECTORS)
        _page_wait(page, 400)
        if _security_settings_ready(page, attempts=2):
            worker.log("[密码] 已通过旧版安全设置找到密码入口")
            return True
    return False


def _hold_visible_browser_after_password_failure(page, worker) -> None:
    if getattr(worker, "headless", True) is not False:
        return
    try:
        seconds = max(
            0,
            min(60, int(os.environ.get("HME_BROWSER_FAILURE_HOLD_SECONDS", "20"))),
        )
    except ValueError:
        seconds = 20
    if not seconds:
        return
    worker.log(f"[密码] 自动设置失败，保留浏览器窗口 {seconds} 秒供检查")
    _page_wait(page, seconds * 1000)


def _fill_password_form(page, worker, password: str) -> bool:
    # The security action can send a passwordless account to OpenAI's ordinary
    # password-login page.  Filling our newly generated password there does not
    # set it; it merely produces "Incorrect email address or password".  That
    # page must enter the reset flow first.
    if _first_visible(page, FORGOT_PASSWORD_SELECTORS, timeout=350) is not None:
        return False
    inputs = []
    for selector in PASSWORD_INPUT_SELECTORS:
        inputs = _visible_locators(page, selector, timeout=500)
        if inputs:
            break
    if not inputs:
        return False
    for input_box in inputs:
        if not resilient_force_fill_locator(worker, input_box, password):
            raise RuntimeError("安全设置中的密码输入框填写未生效")
    if not _click_first_visible(page, PASSWORD_SUBMIT_SELECTORS):
        raise RuntimeError("密码已填写，但安全设置中未找到提交按钮")
    worker.log("[密码] 已在安全设置提交添加密码")
    return True


def _fill_settings_otp(
    page,
    worker,
    min_timestamp: float,
    *,
    reader_factory=None,
) -> bool:
    inputs = []
    for selector in OTP_INPUT_SELECTORS:
        inputs = _visible_locators(page, selector, timeout=400)
        if inputs:
            break
    if not inputs:
        return False

    reader_factory = reader_factory or ICloudOtpReader
    if not callable(reader_factory):
        raise RuntimeError("邮箱验证码读取器未配置")
    reader = reader_factory(worker.account, worker.log)
    try:
        reader.connect()
        code = reader.wait_for_code(min_timestamp)
    finally:
        reader.close()
    if len(inputs) >= 4:
        for index, character in enumerate(code[: len(inputs)]):
            inputs[index].fill(character)
    else:
        inputs[0].fill(code)
    if not _click_first_visible(page, OTP_SUBMIT_SELECTORS):
        raise RuntimeError("密码设置验证码已填写，但未找到验证按钮")
    worker.log("[密码] 已提交安全设置验证码")
    return True


def ensure_password_in_security_settings(
    app_backend,
    worker,
    password: str,
    *,
    context=None,
    timeout_seconds: int = 150,
    force_reset_password: bool = False,
    reader_factory=None,
) -> bool:
    """Open ChatGPT settings and add a password in the Security section."""

    target_password = str(password or "")
    if len(target_password) < 12:
        raise RuntimeError("待设置的 OpenAI 密码长度不足 12 位")
    context = context or _retained_registration_context(
        app_backend, str(getattr(worker.account, "email", "") or "")
    )
    if context is None:
        raise RuntimeError("注册完成后未找到保留的 ChatGPT 浏览器会话")

    page = _reuse_single_registration_page(context, worker)
    if not _open_security_settings(page, worker):
        _hold_visible_browser_after_password_failure(page, worker)
        raise RuntimeError("未能打开 ChatGPT 账户密码设置，请检查账号菜单结构")
    if not force_reset_password and _password_is_present(page, timeout=900):
        worker._password_step_submitted = True
        worker.log("[密码] 安全设置已显示密码管理入口，确认账号已有密码")
        return True
    password_action_clicked = (
        _click_first_visible(page, PASSWORD_PRESENT_SELECTORS, timeout=1200)
        if force_reset_password and _password_is_present(page, timeout=500)
        else _click_add_password(page, timeout=1200)
    )
    if not password_action_clicked:
        _hold_visible_browser_after_password_failure(page, worker)
        raise RuntimeError("账户设置中未找到添加或重置密码按钮")

    worker.log(
        "[密码] 已点击密码行并准备重新设置密码"
        if force_reset_password
        else "[密码] 已点击添加密码"
    )
    verification_started_at = time.time() - 2
    otp_submitted = False
    reset_requested = False
    deadline = time.time() + max(15, int(timeout_seconds))
    while time.time() < deadline:
        if not force_reset_password and _password_is_present(page, timeout=300):
            worker._password_step_submitted = True
            worker.log("[密码] 安全设置已确认密码添加成功")
            return True
        if _first_visible(page, PASSWORD_SUCCESS_SELECTORS, timeout=300) is not None:
            worker._password_step_submitted = True
            worker.log("[密码] 页面已明确确认密码设置成功")
            return True

        if not reset_requested and _click_first_visible(
            page, FORGOT_PASSWORD_SELECTORS, timeout=450
        ):
            reset_requested = True
            verification_started_at = time.time() - 2
            worker.log("[密码] 当前是密码登录页，已点击忘记密码并进入邮箱重置流程")
            _page_wait(page, 700)
            continue
        if not otp_submitted and _fill_settings_otp(
            page,
            worker,
            verification_started_at,
            reader_factory=reader_factory,
        ):
            otp_submitted = True
            _page_wait(page, 800)
            continue
        if _fill_password_form(page, worker, target_password):
            worker._password_step_submitted = True
            worker.log("[密码] 新密码已提交，立即获取 Session")
            return True
        _page_wait(page, 500)

    raise TimeoutError("安全设置未在规定时间内确认密码添加成功")


def extract_session_without_navigation(worker, context) -> dict:
    """Read the current ChatGPT Session through the context request API."""

    last_error: Exception | None = None
    for attempt, delay in enumerate((0, 1, 2, 4), start=1):
        if delay:
            time.sleep(delay)
        try:
            response = context.request.get(
                f"{CHATGPT_HOME_URL}api/auth/session?auth_check={int(time.time() * 1000)}",
                headers={
                    "Accept": "application/json",
                    "Cache-Control": "no-cache, no-store",
                    "Pragma": "no-cache",
                    "Referer": CHATGPT_HOME_URL,
                },
                timeout=30000,
            )
            if not response.ok:
                detail = ""
                try:
                    detail = str(response.text() or "")[:300]
                except Exception:
                    pass
                raise RuntimeError(f"Session 接口返回 HTTP {response.status}：{detail}")
            session = response.json()
            if not isinstance(session, dict):
                raise RuntimeError("Session 接口返回格式无效")
            access_token = str(session.get("accessToken") or "").strip()
            if not access_token:
                raise RuntimeError("Session 接口未返回 Access Token")

            session_email_reader = getattr(worker, "_chatgpt_session_email", None)
            actual_email = (
                str(session_email_reader(session) or "").strip()
                if callable(session_email_reader)
                else ""
            )
            expected_email = str(getattr(worker.account, "email", "") or "").strip()
            if (
                actual_email
                and expected_email
                and actual_email.casefold() != expected_email.casefold()
            ):
                raise RuntimeError(
                    f"Session 账号不匹配：当前={actual_email}，目标={expected_email}"
                )

            result = {
                "url": "",
                "access_token": access_token,
                "session_json": json.dumps(session, ensure_ascii=False, indent=2),
            }
            cookies: list[dict] = []
            cookie_reader = getattr(context, "cookies", None)
            if callable(cookie_reader):
                try:
                    raw_cookies = cookie_reader()
                    if isinstance(raw_cookies, list):
                        cookies = [
                            dict(cookie)
                            for cookie in raw_cookies
                            if isinstance(cookie, dict)
                        ]
                except Exception as error:
                    worker.log(f"[Cookie] 浏览器 Cookie 读取失败：{error}")
            if getattr(worker, "skip_storage_state_capture", False):
                if cookies:
                    result["storage_state_json"] = json.dumps(
                        {"cookies": cookies, "origins": []}, ensure_ascii=False
                    )
            else:
                storage_state = context.storage_state()
                result["storage_state_json"] = json.dumps(
                    storage_state, ensure_ascii=False
                )
                if not cookies and isinstance(storage_state, dict):
                    stored_cookies = storage_state.get("cookies")
                    if isinstance(stored_cookies, list):
                        cookies = [
                            dict(cookie)
                            for cookie in stored_cookies
                            if isinstance(cookie, dict)
                        ]
            if cookies:
                result["cookies_json"] = json.dumps(cookies, ensure_ascii=False)
                worker.log(f"[Cookie] 已保存 {len(cookies)} 个浏览器 Cookie")
            worker.log("[Session] 已在当前浏览器后台获取 Session 和 Access Token")
            return result
        except Exception as error:
            last_error = error
            if attempt < 4:
                worker.log(f"[Session] 后台读取暂未成功（{attempt}/4），准备重试")

    raise RuntimeError(f"无法在当前浏览器后台获取 Session：{last_error}")


def _enable_two_factor_before_browser_closes(
    worker,
    context,
    result: dict,
    pending_two_factor: dict | None,
    *,
    password_confirmed: bool | None = None,
    emit_event=None,
    mfa_client_factory=None,
    enable_mfa=None,
    reauthenticate=None,
) -> dict:
    """Finish 2FA while the worker's sync_playwright scope is still alive."""

    emit_event = emit_event or emit
    mfa_client_factory = mfa_client_factory or MfaHttpClient
    enable_mfa = enable_mfa or enable_totp_mfa
    reauthenticate = reauthenticate or _reauthenticate_for_mfa
    password = str(getattr(worker.account, "password", "") or "")
    if password_confirmed is None:
        password_confirmed = bool(getattr(worker, "_password_step_submitted", False))
    else:
        password_confirmed = bool(password_confirmed)
    emit_event(
        "account_registered",
        result=result,
        password=password,
        password_confirmed=password_confirmed,
    )
    worker._hme_account_registered_emitted = True
    if not password_confirmed:
        worker.log("[2FA] 密码尚未设置成功，已跳过开启 2FA")
        return result

    pending_state = (
        dict(pending_two_factor)
        if isinstance(pending_two_factor, dict)
        else {}
    )
    two_factor = reusable_enabled_two_factor(pending_state)
    if two_factor:
        worker.log("账号已有 TOTP 2FA，已保留现有启用状态")
    else:
        emit_event("two_factor_start")
        mfa_client = mfa_client_factory()
        try:
            mfa_pending = pending_state

            # A stored enrollment belongs to the token/session that created it.
            # After the normal login flow consumes that TOTP and issues a new
            # authenticated token, OpenAI may silently invalidate the old pending
            # enrollment.  Reusing its factor/session IDs then leaves the account
            # logged in but never confirms MFA.  Start one fresh enrollment for
            # the new token; the callback persists its new secret before activation.
            if (
                mfa_pending
                and not mfa_pending.get("enabled")
                and getattr(worker, "_hme_login_totp_submitted", False)
            ):
                worker.log(
                    "[2FA] 已用保存的动态码完成登录；旧待激活登记已失效，"
                    "正在使用当前会话重新创建并激活 TOTP"
                )
                mfa_pending = {}

            def remember_enrolled(state):
                nonlocal mfa_pending
                mfa_pending = dict(state)
                emit_event("two_factor_enrolled", two_factor=state)

            for mfa_attempt in range(3):
                try:
                    two_factor = enable_mfa(
                        mfa_client,
                        access_token=str(result.get("access_token") or ""),
                        email=str(worker.account.email or ""),
                        pending=mfa_pending,
                        on_enrolled=remember_enrolled,
                    )
                    break
                except MfaSetupError as error:
                    if not _mfa_token_was_invalidated(error) or mfa_attempt >= 2:
                        raise
                    worker.log(
                        "[2FA] 当前 Token 不满足最近认证要求，"
                        f"正在浏览器关闭前重新登录（{mfa_attempt + 1}/2）"
                    )
                    refreshed = reauthenticate(worker, context)
                    result.update(refreshed)
                    emit_event(
                        "account_registered",
                        result=result,
                        password=password,
                        password_confirmed=password_confirmed,
                    )
        finally:
            mfa_client.close()

    result["two_factor"] = two_factor
    emit_event("two_factor_enabled")
    worker._hme_two_factor_completed = True
    return result


def configure_existing_account_two_factor(
    worker,
    *,
    enabled: bool,
    pending_two_factor: dict | None = None,
    extract_session=None,
    enable_two_factor=None,
) -> bool:
    """Complete existing-account 2FA while Playwright is still running."""

    extract_session = extract_session or extract_session_without_navigation
    enable_two_factor = enable_two_factor or _enable_two_factor_before_browser_closes
    if not enabled:
        return False
    if getattr(worker, "_hme_existing_two_factor_configured", False):
        return True
    original_extract = getattr(worker, "_extract_session_info", None)
    if not callable(original_extract):
        raise RuntimeError("目标注册工作器缺少 Session 提取方法")

    def extract_and_enable_two_factor(context):
        result = extract_session(worker, context)
        return enable_two_factor(
            worker,
            context,
            result,
            pending_two_factor,
            password_confirmed=True,
        )

    worker._extract_session_info = extract_and_enable_two_factor
    worker._hme_existing_two_factor_configured = True
    return True


def configure_post_registration_password_setup(
    app_backend,
    worker,
    password: str,
    *,
    enabled: bool,
    force_reset_password: bool = False,
    enable_2fa: bool = False,
    pending_two_factor: dict | None = None,
    ensure_password=None,
    add_password=None,
    extract_session=None,
    emit_event=None,
    enable_two_factor=None,
) -> bool:
    """Save the authenticated Session before attempting optional account setup."""

    if add_password is None:
        if ensure_password is not None:
            def add_password(target_worker, context, _access_token, target_password):
                return ensure_password(
                    app_backend,
                    target_worker,
                    target_password,
                    context=context,
                    force_reset_password=force_reset_password,
                )
        else:
            add_password = add_password_via_account_api
    extract_session = extract_session or extract_session_without_navigation
    emit_event = emit_event or emit
    enable_two_factor = enable_two_factor or _enable_two_factor_before_browser_closes
    if not enabled:
        return False
    original_extract = getattr(worker, "_extract_session_info", None)
    if not callable(original_extract):
        raise RuntimeError("目标注册工作器缺少 Session 提取方法")

    # Registration success is sufficient to retain the account.  Password
    # setup is a follow-up operation and must not block Session extraction in
    # the target worker's registration loop.
    worker.require_password_setup = False

    def extract_after_password_setup(context):
        result = extract_session(worker, context)
        password_confirmed = bool(getattr(worker, "_password_step_submitted", False))
        emit_event(
            "account_registered",
            result=result,
            password=str(getattr(worker.account, "password", "") or ""),
            password_confirmed=password_confirmed,
        )
        worker._hme_account_registered_emitted = True

        if getattr(worker, "_hme_password_reset_submitted", False):
            worker._password_step_submitted = True
            worker.log("[密码] 邮箱重置后已成功建立会话，确认唯一密码生效")
        if not getattr(worker, "_password_step_submitted", False):
            try:
                add_password(
                    worker,
                    context,
                    str(result.get("access_token") or ""),
                    password,
                )
            except Exception as error:
                worker._hme_password_setup_error = safe_log_message(str(error))
                worker.log(
                    "[密码] 自动设置未成功；注册 Session 已保存，可稍后单独设置密码："
                    + worker._hme_password_setup_error
                )
        if getattr(worker, "_password_step_submitted", False):
            # Password changes may rotate the authenticated token.  Refresh the
            # saved Session only after OpenAI confirms the password operation.
            result = extract_session(worker, context)
            emit_event(
                "account_registered",
                result=result,
                password=str(getattr(worker.account, "password", "") or ""),
                password_confirmed=True,
            )
        if enable_2fa and getattr(worker, "_password_step_submitted", False):
            result = enable_two_factor(
                worker,
                context,
                result,
                pending_two_factor,
            )
        elif enable_2fa:
            worker.log("[2FA] 密码尚未设置成功，已跳过开启 2FA")
        return result

    worker._extract_session_info = extract_after_password_setup
    return True
