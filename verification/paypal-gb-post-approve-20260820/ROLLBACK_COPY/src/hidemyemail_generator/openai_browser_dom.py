"""Reusable DOM interaction helpers for the OpenAI browser flows."""

from __future__ import annotations

import json
import os
import re
import time

try:
    from .browser_platform import focus_camoufox_window_once
    from .openai_bridge_runtime import safe_log_message
    from .openai_browser_selectors import (
        ADD_PASSWORD_SELECTORS,
        COMPLETED_ONBOARDING_CONTINUE_SELECTORS,
        COMPLETED_ONBOARDING_MARKERS,
        PASSWORD_PRESENT_SELECTORS,
        PASSWORD_ROW_DOM_SCRIPT,
        PROFILE_IDENTITY_MARKERS,
        PROFILE_MENU_FALLBACK_SELECTORS,
        PROFILE_MENU_STRICT_SELECTORS,
        PROFILE_NAME_CLICK_SCRIPT,
        PROFILE_REJECT_MARKERS,
        SETTINGS_MENU_SELECTORS,
        TOPMOST_ADD_POINT_SCRIPT,
    )
except ImportError:
    from browser_platform import focus_camoufox_window_once
    from openai_bridge_runtime import safe_log_message
    from openai_browser_selectors import (
        ADD_PASSWORD_SELECTORS,
        COMPLETED_ONBOARDING_CONTINUE_SELECTORS,
        COMPLETED_ONBOARDING_MARKERS,
        PASSWORD_PRESENT_SELECTORS,
        PASSWORD_ROW_DOM_SCRIPT,
        PROFILE_IDENTITY_MARKERS,
        PROFILE_MENU_FALLBACK_SELECTORS,
        PROFILE_MENU_STRICT_SELECTORS,
        PROFILE_NAME_CLICK_SCRIPT,
        PROFILE_REJECT_MARKERS,
        SETTINGS_MENU_SELECTORS,
        TOPMOST_ADD_POINT_SCRIPT,
    )


def _activate_visible_registration_page(
    worker,
    page,
    *,
    focus_window=focus_camoufox_window_once,
) -> bool:
    """Focus a manual browser once; automatic DOM work stays in the background."""

    if bool(getattr(worker, "headless", False)):
        return False
    foreground_required = str(
        os.environ.get("HME_BROWSER_FOREGROUND_REQUIRED") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not foreground_required:
        return False
    if bool(getattr(worker, "_hme_foreground_activation_attempted", False)):
        return False
    worker._hme_foreground_activation_attempted = True
    try:
        page.bring_to_front()
        try:
            page.evaluate("() => window.focus()")
        except Exception:
            pass
        focus_window()
        return True
    except Exception as error:
        if not getattr(worker, "_hme_about_you_focus_warning_logged", False):
            worker.log(
                "[基础资料] 浏览器窗口激活失败，将继续使用 DOM 输入并严格核验："
                f"{safe_log_message(error)}"
            )
            worker._hme_about_you_focus_warning_logged = True
        return False


def _visible_locators(page, selector: str, *, timeout: int = 500) -> list:
    try:
        locator = page.locator(selector)
        count = locator.count()
    except Exception:
        return []
    visible = []
    for index in range(count):
        candidate = locator.nth(index)
        try:
            is_visible = candidate.is_visible(timeout=timeout)
        except TypeError:
            try:
                is_visible = candidate.is_visible()
            except Exception:
                is_visible = False
        except Exception:
            is_visible = False
        if is_visible:
            visible.append(candidate)
    return visible


def _first_visible(page, selectors: tuple[str, ...], *, timeout: int = 500):
    for selector in selectors:
        candidates = _visible_locators(page, selector, timeout=timeout)
        if candidates:
            return candidates[0]
    return None


def _click_locator(candidate) -> bool:
    try:
        candidate.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        candidate.click(timeout=5000, force=True)
    except TypeError:
        try:
            candidate.click(timeout=5000)
        except Exception:
            return False
    except Exception:
        return False
    return True


def _click_first_visible(
    page, selectors: tuple[str, ...], *, timeout: int = 700
) -> bool:
    candidate = _first_visible(page, selectors, timeout=timeout)
    return candidate is not None and _click_locator(candidate)


def _locator_identity(candidate) -> str:
    values = []
    for attribute in ("data-testid", "aria-label", "title"):
        try:
            value = candidate.get_attribute(attribute, timeout=500)
        except TypeError:
            try:
                value = candidate.get_attribute(attribute)
            except Exception:
                value = ""
        except Exception:
            value = ""
        if value:
            values.append(str(value))
    for reader_name in ("inner_text", "text_content"):
        reader = getattr(candidate, reader_name, None)
        if not callable(reader):
            continue
        try:
            value = reader(timeout=500)
        except TypeError:
            try:
                value = reader()
            except Exception:
                value = ""
        except Exception:
            value = ""
        if value:
            values.append(str(value))
    return " ".join(values).casefold()


def _profile_candidate_allowed(candidate, *, strict: bool) -> bool:
    identity = _locator_identity(candidate)
    if any(marker in identity for marker in PROFILE_REJECT_MARKERS):
        return False
    if strict:
        return True
    return any(marker in identity for marker in PROFILE_IDENTITY_MARKERS) or bool(
        re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", identity)
    )


def _dismiss_menu(page) -> None:
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def _click_profile_name_by_dom(page, profile_name: str) -> bool:
    try:
        return bool(page.evaluate(PROFILE_NAME_CLICK_SCRIPT, profile_name))
    except Exception:
        return False


def _password_row_dom_state(page, *, mark_add: bool = False) -> str:
    try:
        result = page.evaluate(
            PASSWORD_ROW_DOM_SCRIPT,
            {"markAdd": bool(mark_add)},
        )
    except Exception:
        return ""
    if isinstance(result, dict):
        state = str(result.get("state") or "")
        if mark_add and state == "add" and not result.get("marked"):
            return ""
        return state
    return str(result or "")


def _password_is_present(page, *, timeout: int = 700) -> bool:
    return (
        _first_visible(page, PASSWORD_PRESENT_SELECTORS, timeout=timeout) is not None
        or _password_row_dom_state(page) == "present"
    )


def _click_password_add_by_geometry(page, *, timeout: int = 900) -> bool:
    """Click the exact text pixels of the topmost localized Add action."""

    del timeout
    try:
        point = page.evaluate(TOPMOST_ADD_POINT_SCRIPT)
        if not isinstance(point, dict):
            return False
        x = float(point["x"])
        y = float(point["y"])
        if x <= 0 or y <= 0:
            return False
        page.mouse.click(x, y)
        return True
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    except Exception:
        return False


def _click_add_password(page, *, timeout: int = 1200) -> bool:
    # Prefer the row-scoped DOM action because the security-key row can expose
    # an identical localized "Add" label immediately below the password row.
    if _click_password_add_by_geometry(page, timeout=min(timeout, 900)):
        return True
    if _password_row_dom_state(page, mark_add=True) == "add" and _click_first_visible(
        page, ('[data-hme-password-action="add"]',), timeout=timeout
    ):
        return True
    return _click_first_visible(page, ADD_PASSWORD_SELECTORS, timeout=timeout)


def _open_settings_from_profile(
    page,
    worker,
    *,
    visible_locators=_visible_locators,
    first_visible=_first_visible,
    click_locator=_click_locator,
    click_profile_name_by_dom=_click_profile_name_by_dom,
) -> bool:
    profile_name = str(getattr(worker, "registration_profile_name", "") or "").strip()
    name_selectors = ()
    if profile_name:
        quoted_name = json.dumps(profile_name, ensure_ascii=False)
        name_selectors = (
            f"button:has-text({quoted_name})",
            f'[role="button"]:has-text({quoted_name})',
        )
    groups = (
        (name_selectors, True),
        (PROFILE_MENU_STRICT_SELECTORS, True),
    )
    for selectors, strict in groups:
        for selector in selectors:
            for candidate in visible_locators(page, selector, timeout=700):
                if not _profile_candidate_allowed(candidate, strict=strict):
                    continue
                if not click_locator(candidate):
                    continue
                _page_wait(page, 600)
                settings = first_visible(page, SETTINGS_MENU_SELECTORS, timeout=900)
                if settings is not None and click_locator(settings):
                    worker.log("[密码] 已确认账号菜单并打开设置")
                    return True
                worker.log("[密码] 当前按钮未打开设置菜单，已忽略并继续查找账号入口")
                _dismiss_menu(page)
                _page_wait(page, 250)
    # Keep the DOM fallback bounded to interactive elements.  Scanning every
    # node in ChatGPT's large app DOM can leave Playwright waiting forever.
    if profile_name and click_profile_name_by_dom(page, profile_name):
        _page_wait(page, 600)
        settings = first_visible(page, SETTINGS_MENU_SELECTORS, timeout=900)
        if settings is not None and click_locator(settings):
            worker.log(f"[密码] 已点击账号姓名 {profile_name} 并打开设置")
            return True
        worker.log("[密码] 姓名入口未打开设置菜单，继续查找账号入口")
        _dismiss_menu(page)
        _page_wait(page, 250)
    for selector in PROFILE_MENU_FALLBACK_SELECTORS:
        for candidate in visible_locators(page, selector, timeout=700):
            if not _profile_candidate_allowed(candidate, strict=False):
                continue
            if not click_locator(candidate):
                continue
            _page_wait(page, 600)
            settings = first_visible(page, SETTINGS_MENU_SELECTORS, timeout=900)
            if settings is not None and click_locator(settings):
                worker.log("[密码] 已确认账号菜单并打开设置")
                return True
            worker.log("[密码] 当前按钮未打开设置菜单，已忽略并继续查找账号入口")
            _dismiss_menu(page)
            _page_wait(page, 250)
    return False


def _completed_onboarding_visible(page, *, timeout: int = 500) -> bool:
    return (
        _first_visible(page, COMPLETED_ONBOARDING_MARKERS, timeout=timeout) is not None
    )


def _dismiss_completed_onboarding(page, worker, *, attempts: int = 3) -> bool:
    """Dismiss ChatGPT's localized post-registration welcome overlay."""

    for _ in range(max(1, attempts)):
        if not _completed_onboarding_visible(page, timeout=700):
            return False
        if _click_first_visible(
            page,
            COMPLETED_ONBOARDING_CONTINUE_SELECTORS,
            timeout=900,
        ):
            worker.log("[密码] 已关闭 ChatGPT 首次使用欢迎页")
            for _ in range(10):
                _page_wait(page, 250)
                if not _completed_onboarding_visible(page, timeout=250):
                    return True
            return True
        _page_wait(page, 300)
    return False


def _security_settings_ready(page, *, attempts: int = 6) -> bool:
    for _ in range(max(1, attempts)):
        if _completed_onboarding_visible(page, timeout=300):
            return False
        if (
            _first_visible(page, ADD_PASSWORD_SELECTORS, timeout=700) is not None
            or _password_is_present(page, timeout=700)
            or _password_row_dom_state(page) == "add"
        ):
            return True
        _page_wait(page, 400)
    return False


def _page_wait(page, milliseconds: int) -> None:
    try:
        page.wait_for_timeout(milliseconds)
    except Exception:
        time.sleep(max(0, milliseconds) / 1000)
