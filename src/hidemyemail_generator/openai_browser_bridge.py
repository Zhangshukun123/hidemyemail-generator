from __future__ import annotations

import argparse
import sys
import time  # noqa: F401 - compatibility patch point for older tests/integrations
import types

try:
    from . import openai_account_security as _account_security
    from . import openai_bridge_runtime as _runtime
    from . import openai_browser_dom as _dom
    from . import openai_browser_selectors as _selectors
    from . import openai_registration_flow as _registration_flow
    from . import openai_registration_navigation as _registration_navigation
    from . import openai_registration_otp as _registration_otp
    from . import openai_registration_state as _registration_state
    from . import registration_activity as _registration_activity
    from .browser_diagnostics import (  # noqa: F401 - CLI service exports
        BrowserDiagnosticCode,
        emit_browser_diagnostic,
    )
    from .browser_platform import (
        browser_window_slot_from_environment as _platform_browser_window_slot,
        camoufox_window_layout as _platform_camoufox_window_layout,
        configure_windowed_camoufox as _platform_configure_windowed_camoufox,
        copy_registration_clipboard_text as _platform_copy_clipboard_text,
        focus_camoufox_window_once as _platform_focus_camoufox_window,
        move_camoufox_window as _platform_move_camoufox_window,
        primary_screen_size as _platform_primary_screen_size,
        registration_clipboard_lock as _platform_clipboard_lock,
        remember_camoufox_window as _platform_remember_camoufox_window,
        windows_descendant_process_ids as _platform_descendant_process_ids,
    )
    from .openai_mfa import (  # noqa: F401 - CLI and compatibility exports
        MfaSetupError,
        enable_totp_mfa,
        generate_totp,
    )
    from .roxy_registration import RoxyRegistrationBrowser  # noqa: F401
except ImportError:
    import openai_account_security as _account_security
    import openai_bridge_runtime as _runtime
    import openai_browser_dom as _dom
    import openai_browser_selectors as _selectors
    import openai_registration_flow as _registration_flow
    import openai_registration_navigation as _registration_navigation
    import openai_registration_otp as _registration_otp
    import openai_registration_state as _registration_state
    import registration_activity as _registration_activity
    from browser_diagnostics import (  # noqa: F401 - CLI service exports
        BrowserDiagnosticCode,
        emit_browser_diagnostic,
    )
    from browser_platform import (
        browser_window_slot_from_environment as _platform_browser_window_slot,
        camoufox_window_layout as _platform_camoufox_window_layout,
        configure_windowed_camoufox as _platform_configure_windowed_camoufox,
        copy_registration_clipboard_text as _platform_copy_clipboard_text,
        focus_camoufox_window_once as _platform_focus_camoufox_window,
        move_camoufox_window as _platform_move_camoufox_window,
        primary_screen_size as _platform_primary_screen_size,
        registration_clipboard_lock as _platform_clipboard_lock,
        remember_camoufox_window as _platform_remember_camoufox_window,
        windows_descendant_process_ids as _platform_descendant_process_ids,
    )
    from openai_mfa import (  # noqa: F401 - CLI and compatibility exports
        MfaSetupError,
        enable_totp_mfa,
        generate_totp,
    )
    from roxy_registration import RoxyRegistrationBrowser  # noqa: F401


# Public selector/configuration compatibility exports.
EVENT_PREFIX = _selectors.EVENT_PREFIX
AUTH_RESOURCE_RELOAD_ATTEMPTS = _selectors.AUTH_RESOURCE_RELOAD_ATTEMPTS
CHATGPT_HOME_URL = _selectors.CHATGPT_HOME_URL
CHATGPT_ACCOUNT_SETTINGS_URL = _selectors.CHATGPT_ACCOUNT_SETTINGS_URL
CHATGPT_SECURITY_SETTINGS_URL = _selectors.CHATGPT_SECURITY_SETTINGS_URL
OTP_POLL_INTERVAL_SECONDS = _selectors.OTP_POLL_INTERVAL_SECONDS
PROFILE_MENU_STRICT_SELECTORS = _selectors.PROFILE_MENU_STRICT_SELECTORS
PROFILE_MENU_FALLBACK_SELECTORS = _selectors.PROFILE_MENU_FALLBACK_SELECTORS
PROFILE_IDENTITY_MARKERS = _selectors.PROFILE_IDENTITY_MARKERS
PROFILE_REJECT_MARKERS = _selectors.PROFILE_REJECT_MARKERS
SETTINGS_MENU_SELECTORS = _selectors.SETTINGS_MENU_SELECTORS
COMPLETED_ONBOARDING_MARKERS = _selectors.COMPLETED_ONBOARDING_MARKERS
COMPLETED_ONBOARDING_CONTINUE_SELECTORS = (
    _selectors.COMPLETED_ONBOARDING_CONTINUE_SELECTORS
)
ONE_TIME_CODE_LOGIN_SELECTORS = _selectors.ONE_TIME_CODE_LOGIN_SELECTORS
PASSWORD_CONTINUE_SELECTORS = _selectors.PASSWORD_CONTINUE_SELECTORS
PASSWORD_RESET_CONFIRM_MARKERS = _selectors.PASSWORD_RESET_CONFIRM_MARKERS
PASSWORD_RESET_CONFIRM_CONTINUE_SELECTORS = (
    _selectors.PASSWORD_RESET_CONFIRM_CONTINUE_SELECTORS
)
PASSWORD_ENTRY_STATUS_INTERVAL_SECONDS = (
    _selectors.PASSWORD_ENTRY_STATUS_INTERVAL_SECONDS
)
PASSWORD_ENTRY_SECURITY_MARKERS = _selectors.PASSWORD_ENTRY_SECURITY_MARKERS
FORGOT_PASSWORD_SELECTORS = _selectors.FORGOT_PASSWORD_SELECTORS
ACCOUNT_TAB_SELECTORS = _selectors.ACCOUNT_TAB_SELECTORS
SECURITY_TAB_SELECTORS = _selectors.SECURITY_TAB_SELECTORS
ADD_PASSWORD_SELECTORS = _selectors.ADD_PASSWORD_SELECTORS
PASSWORD_PRESENT_SELECTORS = _selectors.PASSWORD_PRESENT_SELECTORS
PASSWORD_SUCCESS_SELECTORS = _selectors.PASSWORD_SUCCESS_SELECTORS
PROFILE_NAME_CLICK_SCRIPT = _selectors.PROFILE_NAME_CLICK_SCRIPT
PASSWORD_ROW_DOM_SCRIPT = _selectors.PASSWORD_ROW_DOM_SCRIPT
TOPMOST_ADD_POINT_SCRIPT = _selectors.TOPMOST_ADD_POINT_SCRIPT
PASSWORD_INPUT_SELECTORS = _selectors.PASSWORD_INPUT_SELECTORS
PASSWORD_SUBMIT_SELECTORS = _selectors.PASSWORD_SUBMIT_SELECTORS
OTP_INPUT_SELECTORS = _selectors.OTP_INPUT_SELECTORS
LOCALIZED_EMAIL_OTP_INPUT_SELECTORS = _selectors.LOCALIZED_EMAIL_OTP_INPUT_SELECTORS
OTP_SUBMIT_SELECTORS = _selectors.OTP_SUBMIT_SELECTORS

CHATGPT_HOME_LOGIN_SELECTORS = _registration_navigation.CHATGPT_HOME_LOGIN_SELECTORS
CHATGPT_HOME_SIGNUP_SELECTORS = _registration_navigation.CHATGPT_HOME_SIGNUP_SELECTORS
OPENAI_EMAIL_LOGIN_INPUT_SELECTORS = (
    _registration_navigation.OPENAI_EMAIL_LOGIN_INPUT_SELECTORS
)
OPENAI_EMAIL_SUBMIT_SELECTORS = _registration_navigation.OPENAI_EMAIL_SUBMIT_SELECTORS
OPENAI_EMAIL_LOGIN_SUBMIT_SELECTORS = (
    _registration_navigation.OPENAI_EMAIL_LOGIN_SUBMIT_SELECTORS
)
OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS = (
    _registration_navigation.OPENAI_EMAIL_REGISTRATION_SUBMIT_SELECTORS
)

_REGISTRATION_CLIPBOARD_LOCK = _platform_clipboard_lock()
_copy_registration_clipboard_text = _platform_copy_clipboard_text


# Runtime and low-level helper compatibility exports.
load_saved_storage_state = _runtime.load_saved_storage_state
configure_registration_profile_capture = _runtime.configure_registration_profile_capture
reusable_enabled_two_factor = _runtime.reusable_enabled_two_factor
_mfa_token_was_invalidated = _runtime._mfa_token_was_invalidated
_fontconfig_generator_with_home = _runtime._fontconfig_generator_with_home
_configure_camoufox_runtime_cache = _runtime._configure_camoufox_runtime_cache
prepare_writable_camoufox_fontconfig = _runtime.prepare_writable_camoufox_fontconfig
MfaHttpClient = _runtime.MfaHttpClient
emit = _runtime.emit
safe_log_message = _runtime.safe_log_message
_locator_value_matches = _runtime._locator_value_matches
resilient_force_fill_locator = _runtime.resilient_force_fill_locator


def configure_worker_login_totp(worker, two_factor: dict | None) -> bool:
    return _runtime.configure_worker_login_totp(
        worker,
        two_factor,
        generate_code=lambda secret: generate_totp(secret),
    )


# DOM compatibility exports. Wrappers inject patchable bridge dependencies.
_visible_locators = _dom._visible_locators
_first_visible = _dom._first_visible
_click_locator = _dom._click_locator
_click_first_visible = _dom._click_first_visible
_locator_identity = _dom._locator_identity
_profile_candidate_allowed = _dom._profile_candidate_allowed
_dismiss_menu = _dom._dismiss_menu
_click_profile_name_by_dom = _dom._click_profile_name_by_dom
_password_row_dom_state = _dom._password_row_dom_state
_password_is_present = _dom._password_is_present
_click_password_add_by_geometry = _dom._click_password_add_by_geometry
_click_add_password = _dom._click_add_password
_completed_onboarding_visible = _dom._completed_onboarding_visible
_dismiss_completed_onboarding = _dom._dismiss_completed_onboarding
_security_settings_ready = _dom._security_settings_ready
_page_wait = _dom._page_wait


def _activate_visible_registration_page(worker, page) -> bool:
    return _dom._activate_visible_registration_page(
        worker,
        page,
        focus_window=_focus_camoufox_window_once,
    )


def _open_settings_from_profile(page, worker) -> bool:
    return _dom._open_settings_from_profile(
        page,
        worker,
        visible_locators=_visible_locators,
        first_visible=_first_visible,
        click_locator=_click_locator,
        click_profile_name_by_dom=_click_profile_name_by_dom,
    )


# Registration worker hooks.
_security_challenge_visible = _registration_flow._security_challenge_visible
configure_password_first_login = _registration_flow.configure_password_first_login
configure_password_readiness_diagnostics = (
    _registration_flow.configure_password_readiness_diagnostics
)
recognize_registration_page = _registration_state.recognize_registration_page
configure_registration_state_recognition = (
    _registration_state.configure_registration_state_recognition
)
configure_request_driven_registration = (
    _registration_activity.configure_request_driven_registration
)
registration_chain_snapshot = _registration_activity.registration_chain_snapshot
begin_registration_step = _registration_activity.begin_registration_step
skip_registration_step = _registration_activity.skip_registration_step
mark_registration_chain = _registration_activity.mark_registration_chain
finalize_registration_chain = _registration_activity.finalize_registration_chain
fail_registration_chain = _registration_activity.fail_registration_chain
configure_email_verification_priority = (
    _registration_flow.configure_email_verification_priority
)
_about_you_profile_values_match = _registration_flow._about_you_profile_values_match
_is_google_account_url = _registration_flow._is_google_account_url
_is_openai_auth_url = _registration_flow._is_openai_auth_url
_registration_input_value = _registration_flow._registration_input_value
_click_openai_email_submit = _registration_flow._click_openai_email_submit
FreshFingerprintRequiredError = (
    _registration_flow.FreshFingerprintRequiredError
)
_auth_page_resource_state = _registration_navigation._auth_page_resource_state
ensure_auth_page_resources = _registration_navigation.ensure_auth_page_resources
configure_direct_registration_browser = (
    _registration_navigation.configure_direct_registration_browser
)
detect_direct_registration_location = (
    _registration_navigation.detect_direct_registration_location
)
_navigation_was_aborted = _registration_navigation._navigation_was_aborted
configure_resilient_registration_navigation = (
    _registration_navigation.configure_resilient_registration_navigation
)
_is_chatgpt_homepage = _registration_navigation._is_chatgpt_homepage
_is_chatgpt_auth_entry_url = (
    _registration_navigation._is_chatgpt_auth_entry_url
)
_click_chatgpt_home_login = _registration_navigation._click_chatgpt_home_login
_click_chatgpt_home_signup = _registration_navigation._click_chatgpt_home_signup
_wait_for_home_email_modal_transition = (
    _registration_navigation._wait_for_home_email_modal_transition
)


def configure_chatgpt_home_login_entry(worker, *, enabled: bool = True) -> bool:
    return _registration_navigation.configure_chatgpt_home_login_entry(
        worker,
        enabled=enabled,
        activate_page=_activate_visible_registration_page,
    )


def configure_security_challenge_monitoring(worker) -> bool:
    return _registration_flow.configure_security_challenge_monitoring(
        worker,
        activate_page=_activate_visible_registration_page,
    )


def configure_resilient_about_you_input(worker) -> bool:
    return _registration_flow.configure_resilient_about_you_input(
        worker,
        activate_page=_activate_visible_registration_page,
    )


def configure_email_password_only_registration(worker, *, enabled: bool) -> bool:
    return _registration_flow.configure_email_password_only_registration(
        worker,
        enabled=enabled,
        activate_page=_activate_visible_registration_page,
        clipboard_write=lambda value: _copy_registration_clipboard_text(value),
        clipboard_lock=_REGISTRATION_CLIPBOARD_LOCK,
        first_visible=_first_visible,
        wait=_page_wait,
    )


# Window/platform compatibility wrappers.
def configure_windowed_camoufox(app_backend) -> bool:
    return _platform_configure_windowed_camoufox(app_backend)


def _browser_window_slot_from_environment() -> tuple[int, int]:
    return _platform_browser_window_slot()


def _primary_screen_size() -> tuple[int, int]:
    return _platform_primary_screen_size()


def _camoufox_window_layout(
    slot_index: int,
    slot_count: int,
    *,
    screen_size: tuple[int, int] | None = None,
    randomizer=None,
) -> dict[str, int]:
    return _platform_camoufox_window_layout(
        slot_index,
        slot_count,
        screen_size=screen_size,
        randomizer=randomizer,
    )


def _windows_descendant_process_ids(root_pid: int) -> set[int]:
    return _platform_descendant_process_ids(root_pid)


def _move_camoufox_window(browser, layout: dict[str, int]) -> bool:
    return _platform_move_camoufox_window(browser, layout)


def _remember_camoufox_window(hwnd: int) -> None:
    _platform_remember_camoufox_window(hwnd)


def _focus_camoufox_window_once() -> bool:
    return _platform_focus_camoufox_window()


# Account security and MFA compatibility wrappers.
_retained_registration_context = _account_security._retained_registration_context
_reuse_single_registration_page = _account_security._reuse_single_registration_page
_open_security_settings = _account_security._open_security_settings
_hold_visible_browser_after_password_failure = (
    _account_security._hold_visible_browser_after_password_failure
)
_fill_password_form = _account_security._fill_password_form
extract_session_without_navigation = (
    _account_security.extract_session_without_navigation
)

ManualOtpReader = _registration_otp.ManualOtpReader
ICloudOtpReader = ManualOtpReader
iso_timestamp = _registration_otp.iso_timestamp
configure_registration_otp_reader = _registration_otp.configure_registration_otp_reader


def _fill_settings_otp(page, worker, min_timestamp: float) -> bool:
    return _account_security._fill_settings_otp(
        page,
        worker,
        min_timestamp,
        reader_factory=ICloudOtpReader,
    )


def ensure_password_in_security_settings(
    app_backend,
    worker,
    password: str,
    *,
    context=None,
    force_reset_password: bool = False,
    timeout_seconds: int = 150,
) -> bool:
    return _account_security.ensure_password_in_security_settings(
        app_backend,
        worker,
        password,
        context=context,
        force_reset_password=force_reset_password,
        timeout_seconds=timeout_seconds,
        reader_factory=ICloudOtpReader,
    )


def add_password_via_account_api(
    worker,
    context,
    access_token: str,
    password: str,
) -> bool:
    return _account_security.add_password_via_account_api(
        worker,
        context,
        access_token,
        password,
    )


def _reauthenticate_for_mfa(worker, context) -> dict:
    return _account_security._reauthenticate_for_mfa(
        worker,
        context,
        extract_session=extract_session_without_navigation,
    )


def _enable_two_factor_before_browser_closes(
    worker,
    context,
    result: dict,
    pending_two_factor: dict | None,
    *,
    password_confirmed: bool | None = None,
) -> dict:
    return _account_security._enable_two_factor_before_browser_closes(
        worker,
        context,
        result,
        pending_two_factor,
        password_confirmed=password_confirmed,
        emit_event=emit,
        mfa_client_factory=MfaHttpClient,
        enable_mfa=enable_totp_mfa,
        reauthenticate=_reauthenticate_for_mfa,
    )


def configure_existing_account_two_factor(
    worker,
    *,
    enabled: bool,
    pending_two_factor: dict | None = None,
) -> bool:
    return _account_security.configure_existing_account_two_factor(
        worker,
        enabled=enabled,
        pending_two_factor=pending_two_factor,
        extract_session=extract_session_without_navigation,
        enable_two_factor=_enable_two_factor_before_browser_closes,
    )


def configure_post_registration_password_setup(
    app_backend,
    worker,
    password: str,
    *,
    enabled: bool,
    force_reset_password: bool = False,
    enable_2fa: bool = False,
    pending_two_factor: dict | None = None,
) -> bool:
    return _account_security.configure_post_registration_password_setup(
        app_backend,
        worker,
        password,
        enabled=enabled,
        force_reset_password=force_reset_password,
        enable_2fa=enable_2fa,
        pending_two_factor=pending_two_factor,
        add_password=add_password_via_account_api,
        extract_session=extract_session_without_navigation,
        emit_event=emit,
        enable_two_factor=_enable_two_factor_before_browser_closes,
    )


def configure_manual_browser_verification(worker, *, enabled: bool) -> bool:
    return _registration_otp.configure_manual_browser_verification(
        worker,
        enabled=enabled,
        activate_page=_activate_visible_registration_page,
    )


def ensure_tkinter_importable() -> None:
    try:
        import tkinter  # noqa: F401

        return
    except ImportError:
        pass

    class DummyWidget:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    class DummyModule(types.ModuleType):
        def __getattr__(self, _name: str):
            return DummyWidget

    tkinter = DummyModule("tkinter")
    ttk = DummyModule("tkinter.ttk")
    font = DummyModule("tkinter.font")
    tkinter.ttk = ttk
    tkinter.font = font
    tkinter.filedialog = DummyModule("tkinter.filedialog")
    tkinter.messagebox = DummyModule("tkinter.messagebox")
    tkinter.simpledialog = DummyModule("tkinter.simpledialog")
    sys.modules["tkinter"] = tkinter
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.font"] = font
    sys.modules["tkinter.filedialog"] = tkinter.filedialog
    sys.modules["tkinter.messagebox"] = tkinter.messagebox
    sys.modules["tkinter.simpledialog"] = tkinter.simpledialog


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="iCloud OpenAI browser bridge")
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--headless", action="store_true")
    return parser


def require_registration_proxy_country(health, expected_country: str) -> str:
    expected = str(expected_country or "").strip().upper()
    actual = str(getattr(health, "country", "") or "").strip().upper()
    if expected and actual != expected:
        raise RuntimeError(
            f"注册代理出口国家不符：要求 {expected}，实际 {actual or '未知'}；已拒绝直连或跨区注册"
        )
    chatgpt_status = int(getattr(health, "chatgpt_status", 0) or 0)
    if chatgpt_status not in {200, 403}:
        status_label = str(chatgpt_status) if chatgpt_status else "未知"
        raise RuntimeError(
            "注册代理访问 ChatGPT 不可用："
            f"HTTP {status_label}；代理探测未确认出口可达"
        )
    return actual


def _password_confirmed_for_two_factor(worker, saved_confirmed: bool) -> bool:
    return bool(saved_confirmed or getattr(worker, "_password_step_submitted", False))


def main() -> int:
    try:
        from . import openai_browser_cli
    except ImportError:
        import openai_browser_cli

    return openai_browser_cli.run(sys.modules[__name__])


if __name__ == "__main__":
    raise SystemExit(main())
