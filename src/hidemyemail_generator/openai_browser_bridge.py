from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import types
from datetime import datetime, timezone
from pathlib import Path

try:
    from .openai_mfa import MfaSetupError, enable_totp_mfa, generate_totp
except ImportError:
    from openai_mfa import MfaSetupError, enable_totp_mfa, generate_totp


EVENT_PREFIX = "HME_BROWSER_EVENT:"
CAMOUFOX_WINDOW_SIZE = (1280, 800)
CAMOUFOX_PERSISTENT_STORAGE_PREFS = {
    "dom.storageManager.prompt.testing": True,
    "dom.storageManager.prompt.testing.allow": True,
}
CHATGPT_HOME_URL = "https://chatgpt.com/"
CHATGPT_SECURITY_SETTINGS_URL = "https://chatgpt.com/#settings/Security"
OTP_POLL_INTERVAL_SECONDS = 1.5

PROFILE_MENU_STRICT_SELECTORS = (
    '[data-testid="profile-button"]',
    '[data-testid="accounts-profile-button"]',
    'button[aria-label="Open profile menu" i]',
    'button[aria-label="Profile menu" i]',
    'button[aria-label="Account menu" i]',
    'button[aria-label="打开个人资料菜单" i]',
    'button[aria-label="个人资料菜单" i]',
    'button[aria-label="账户菜单" i]',
    'button[aria-label="帐户菜单" i]',
    'button[aria-label="账号菜单" i]',
    'button[aria-label="プロフィールメニューを開く" i]',
    'button[aria-label="プロフィールメニュー" i]',
    'button[aria-label="アカウントメニュー" i]',
    'button[aria-haspopup="menu"]:has([data-testid*="avatar" i])',
    'button:has([data-testid="user-avatar"])',
)
PROFILE_MENU_FALLBACK_SELECTORS = (
    'button[aria-haspopup="menu"]',
    '[role="button"][aria-haspopup="menu"]',
)
PROFILE_IDENTITY_MARKERS = (
    "profile",
    "avatar",
    "account menu",
    "user menu",
    "个人资料",
    "个人菜单",
    "账户菜单",
    "帐户菜单",
    "账号菜单",
    "プロフィール",
    "アカウントメニュー",
)
PROFILE_REJECT_MARKERS = (
    "upgrade",
    "offer",
    "promotion",
    "promo",
    "discount",
    "trial",
    "get plus",
    "get pro",
    "business",
    "优惠",
    "升级",
    "促销",
    "折扣",
    "试用",
    "订阅",
    "套餐",
    "アップグレード",
    "オファー",
    "プロモーション",
    "割引",
    "トライアル",
    "プラン",
)
SETTINGS_MENU_SELECTORS = (
    '[data-testid="settings-menu-item"]',
    '[role="menuitem"]:has-text("Settings")',
    '[role="menuitem"]:has-text("设置")',
    'button:has-text("Settings")',
    'button:has-text("设置")',
    '[role="menuitem"]:has-text("設定")',
    'button:has-text("設定")',
)
COMPLETED_ONBOARDING_MARKERS = (
    'text="You’re all set"',
    'text="You\'re all set"',
    'text="You are all set"',
    'text="準備が完了しました"',
    'text="准备就绪"',
    'text="準備就緒"',
)
COMPLETED_ONBOARDING_CONTINUE_SELECTORS = (
    'button:has-text("Continue")',
    '[role="button"]:has-text("Continue")',
    'button:has-text("続行")',
    '[role="button"]:has-text("続行")',
    'button:has-text("继续")',
    '[role="button"]:has-text("继续")',
    'button:has-text("繼續")',
    '[role="button"]:has-text("繼續")',
)
ONE_TIME_CODE_LOGIN_SELECTORS = (
    'button:has-text("Log in with a one-time code")',
    '[role="button"]:has-text("Log in with a one-time code")',
    'text="Log in with a one-time code"',
    'button:has-text("Log in with a code")',
    'text="Log in with a code"',
    'button:has-text("使用一次性验证码登录")',
    'button:has-text("使用验证码登录")',
    'text="使用一次性验证码登录"',
    'button:has-text("ワンタイムコードでログインする")',
    '[role="button"]:has-text("ワンタイムコードでログインする")',
    'text="ワンタイムコードでログインする"',
)
FORGOT_PASSWORD_SELECTORS = (
    'a:has-text("Forgot password")',
    'button:has-text("Forgot password")',
    '[role="button"]:has-text("Forgot password")',
    'text="Forgot password?"',
    'a:has-text("忘记密码")',
    'button:has-text("忘记密码")',
    'a:has-text("忘記密碼")',
    'button:has-text("忘記密碼")',
    'text="忘记了密码?"',
    'text="忘记了密码？"',
    'text="忘記密碼?"',
    'text="忘記密碼？"',
    'a:has-text("パスワードをお忘れ")',
    'button:has-text("パスワードをお忘れ")',
    'text="パスワードをお忘れですか？"',
)
SECURITY_TAB_SELECTORS = (
    '[data-testid="security-tab"]',
    '[role="tab"]:has-text("Security")',
    '[role="tab"]:has-text("安全")',
    'button:has-text("Security")',
    'button:has-text("安全设置")',
    'a:has-text("Security")',
    'a:has-text("安全设置")',
    '[role="tab"]:has-text("セキュリティとログイン")',
    '[role="tab"]:has-text("セキュリティ")',
    'button:has-text("セキュリティとログイン")',
    'button:has-text("セキュリティ")',
    'a:has-text("セキュリティとログイン")',
    'a:has-text("セキュリティ")',
)
ADD_PASSWORD_SELECTORS = (
    '[data-testid="add-password-button"]',
    "xpath=//*[normalize-space(.)='パスワード']/following::*[(self::button or @role='button') and contains(normalize-space(.), '追加する')][1]",
    "xpath=//*[normalize-space(.)='Password']/following::*[(self::button or @role='button') and normalize-space(.)='Add'][1]",
    "xpath=//*[normalize-space(.)='密码' or normalize-space(.)='密碼']/following::*[(self::button or @role='button') and (normalize-space(.)='添加' or normalize-space(.)='新增')][1]",
    'button:has-text("Add password")',
    'button:has-text("Set password")',
    'button:has-text("添加密码")',
    'button:has-text("设置密码")',
    'button:has-text("パスワードを追加")',
    'button:has-text("パスワードを設定")',
    'button:has-text("追加する")',
    '[role="button"]:has-text("追加する")',
    'text="追加する"',
)
PASSWORD_PRESENT_SELECTORS = (
    "xpath=//*[normalize-space(.)='パスワード']/following::*[(self::button or @role='button') and (contains(normalize-space(.), '変更する') or contains(normalize-space(.), '管理する') or contains(normalize-space(.), 'リセット'))][1]",
    "xpath=//*[normalize-space(.)='Password']/following::*[(self::button or @role='button') and (contains(normalize-space(.), 'Change') or contains(normalize-space(.), 'Manage') or contains(normalize-space(.), 'Reset'))][1]",
    "xpath=//*[normalize-space(.)='密码' or normalize-space(.)='密碼']/following::*[(self::button or @role='button') and (contains(normalize-space(.), '修改') or contains(normalize-space(.), '更改') or contains(normalize-space(.), '管理') or contains(normalize-space(.), '重置'))][1]",
    'button:has-text("Change password")',
    'button:has-text("Update password")',
    'button:has-text("Reset password")',
    'button:has-text("修改密码")',
    'button:has-text("更改密码")',
    'button:has-text("重置密码")',
    'button:has-text("パスワードを変更")',
    'button:has-text("パスワードを更新")',
    'button:has-text("パスワードをリセット")',
    'button:has-text("変更する")',
    '[role="button"]:has-text("変更する")',
    'button:has-text("管理する")',
    '[role="button"]:has-text("管理する")',
)
PASSWORD_SUCCESS_SELECTORS = (
    'text="Password added"',
    'text="Password set"',
    'text="Password updated"',
    'text="密码已添加"',
    'text="密码已设置"',
    'text="密码已更新"',
    'text="パスワードが追加されました"',
    'text="パスワードが設定されました"',
    'text="パスワードが更新されました"',
    'text="Password reset successfully"',
    'text="Your password has been reset"',
    'text="密码重置成功"',
    'text="密码已重置"',
    'text="密碼重設成功"',
    'text="密碼已重設"',
    'text="パスワードがリセットされました"',
)
PROFILE_NAME_CLICK_SCRIPT = r"""
name => {
  const normalize = value => String(value || "").replace(/\s+/g, " ").trim();
  const visible = element => {
    if (!(element instanceof HTMLElement)) return false;
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" &&
      style.visibility !== "hidden";
  };
  const expected = normalize(name);
  if (!expected) return false;
  const candidates = Array.from(document.querySelectorAll("body *"))
    .filter(element => visible(element) && normalize(element.textContent) === expected)
    .filter(element => !Array.from(element.children).some(
      child => normalize(child.textContent) === expected
    ))
    .sort((left, right) => {
      const a = left.getBoundingClientRect();
      const b = right.getBoundingClientRect();
      return (a.width * a.height) - (b.width * b.height);
    });
  for (const leaf of candidates) {
    let target = leaf.closest(
      'button, [role="button"], a, [aria-haspopup="menu"], [data-testid*="profile" i]'
    );
    let ancestor = leaf.parentElement;
    for (let depth = 0; !target && ancestor && depth < 5; depth += 1) {
      const style = window.getComputedStyle(ancestor);
      if (style.cursor === "pointer" || ancestor.tabIndex >= 0 ||
          ancestor.hasAttribute("aria-expanded")) {
        target = ancestor;
        break;
      }
      ancestor = ancestor.parentElement;
    }
    (target || leaf).click();
    return true;
  }
  return false;
}
"""
PASSWORD_ROW_DOM_SCRIPT = r"""
options => {
  const normalize = value => String(value || "").replace(/\s+/g, " ").trim();
  const visible = element => {
    if (!(element instanceof HTMLElement)) return false;
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" &&
      style.visibility !== "hidden";
  };
  const passwordLabels = new Set(["Password", "密码", "密碼", "パスワード"]);
  const addLabels = new Set([
    "Add", "Add password", "Set password", "添加", "新增", "追加する",
    "添加密码", "设置密码", "パスワードを追加", "パスワードを設定"
  ]);
  const presentLabels = new Set([
    "Change", "Manage", "Reset", "Update", "Change password",
    "Manage password", "Reset password", "Update password", "修改", "更改",
    "管理", "重置", "変更する", "管理する", "リセット", "更新する",
    "修改密码", "更改密码", "重置密码", "パスワードを変更",
    "パスワードを更新", "パスワードをリセット"
  ]);
  const leaves = Array.from(document.querySelectorAll("body *"))
    .filter(element => visible(element))
    .filter(element => !Array.from(element.children).some(
      child => normalize(child.textContent) === normalize(element.textContent)
    ));
  const passwordLabel = leaves.find(
    element => passwordLabels.has(normalize(element.textContent))
  );
  if (!passwordLabel) return {state: "missing", clicked: false};

  let row = passwordLabel.parentElement;
  for (let depth = 0; row && row !== document.body && depth < 8; depth += 1) {
    const rect = row.getBoundingClientRect();
    if (rect.height > 0 && rect.height <= 220) {
      const rowLeaves = Array.from(row.querySelectorAll("*"))
        .filter(element => visible(element))
        .filter(element => !Array.from(element.children).some(
          child => normalize(child.textContent) === normalize(element.textContent)
        ));
      const action = rowLeaves.find(element => {
        const text = normalize(element.textContent);
        return addLabels.has(text) || presentLabels.has(text);
      });
      if (action) {
        const text = normalize(action.textContent);
        const state = addLabels.has(text) ? "add" : "present";
        if (state === "add" && options && options.markAdd) {
          const target = action.closest('button, [role="button"], a') || action;
          document.querySelectorAll('[data-hme-password-action="add"]')
            .forEach(element => element.removeAttribute("data-hme-password-action"));
          target.setAttribute("data-hme-password-action", "add");
          return {state, marked: true};
        }
        return {state, marked: false};
      }
    }
    row = row.parentElement;
  }
  return {state: "missing", clicked: false};
}
"""
TOPMOST_ADD_POINT_SCRIPT = r"""
() => {
  const normalize = value => String(value || "").replace(/\s+/g, " ").trim();
  const visible = element => {
    if (!(element instanceof HTMLElement)) return false;
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" &&
      style.visibility !== "hidden";
  };
  const addLabels = new Set([
    "Add", "Add password", "Set password", "添加", "新增", "追加する",
    "添加密码", "设置密码", "パスワードを追加", "パスワードを設定"
  ]);
  const passwordLabels = new Set(["Password", "密码", "密碼", "パスワード"]);
  const leaves = Array.from(document.querySelectorAll("body *"))
    .filter(element => visible(element))
    .filter(element => !Array.from(element.children).some(
      child => normalize(child.textContent) === normalize(element.textContent)
    ));
  const passwordLabel = leaves
    .filter(element => passwordLabels.has(normalize(element.textContent)))
    .map(element => ({element, rect: element.getBoundingClientRect()}))
    .sort((left, right) =>
      (left.rect.width * left.rect.height) - (right.rect.width * right.rect.height)
    )[0];
  if (!passwordLabel) return null;

  const actionPoints = leaves
    .filter(element => addLabels.has(normalize(element.textContent)))
    .map(element => {
      const rect = element.getBoundingClientRect();
      return {
        x: rect.left + (rect.width / 2),
        right: rect.right,
      };
    });
  const actionPoint = actionPoints.sort(
    (left, right) => (right.right - left.right) || (right.x - left.x)
  )[0];
  const labelRect = passwordLabel.rect;
  const dialog = passwordLabel.element.closest('[role="dialog"]');
  const dialogRect = dialog ? dialog.getBoundingClientRect() : null;
  const fallbackX = dialogRect
    ? dialogRect.right - Math.min(90, dialogRect.width * 0.08)
    : window.innerWidth - 100;
  return {
    x: actionPoint ? actionPoint.x : fallbackX,
    y: labelRect.top + (labelRect.height / 2),
    labelTop: labelRect.top,
    actionRight: actionPoint ? actionPoint.right : fallbackX,
  };
}
"""
PASSWORD_INPUT_SELECTORS = (
    'input[type="password"]',
    'input[name*="password" i]',
    'input[autocomplete="new-password"]',
)
PASSWORD_SUBMIT_SELECTORS = (
    'button[type="submit"]:has-text("Add password")',
    'button[type="submit"]:has-text("Set password")',
    'button[type="submit"]:has-text("Save")',
    'button[type="submit"]:has-text("Continue")',
    'button[type="submit"]:has-text("添加密码")',
    'button[type="submit"]:has-text("设置密码")',
    'button[type="submit"]:has-text("保存")',
    'button[type="submit"]:has-text("继续")',
    'button[type="submit"]:has-text("パスワードを追加")',
    'button[type="submit"]:has-text("パスワードを設定")',
    'button[type="submit"]:has-text("保存")',
    'button[type="submit"]:has-text("続行")',
    'button[type="submit"]',
)
OTP_INPUT_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    'input[name="code"]',
    'input[aria-label*="code" i]',
    'input[placeholder*="code" i]',
    'input[aria-label*="验证码" i]',
    'input[placeholder*="验证码" i]',
)
OTP_SUBMIT_SELECTORS = (
    'button[type="submit"]:has-text("Continue")',
    'button[type="submit"]:has-text("Verify")',
    'button[type="submit"]:has-text("继续")',
    'button[type="submit"]:has-text("验证")',
    'button[type="submit"]:has-text("続行")',
    'button[type="submit"]:has-text("確認")',
    'button[type="submit"]',
)


def load_saved_storage_state(db_file: str, email: str) -> dict:
    path_text = str(db_file or "").strip()
    target_email = str(email or "").strip().lower()
    if not path_text or not target_email:
        return {}
    try:
        connection = sqlite3.connect(path_text)
        try:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?",
                (f"gpt_account:{target_email}",),
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return {}
        record = json.loads(str(row[0] or "{}"))
        if not isinstance(record, dict):
            return {}
        raw_state = record.get("storage_state_json")
        if isinstance(raw_state, dict):
            return dict(raw_state)
        if not isinstance(raw_state, str) or not raw_state.strip():
            return {}
        state = json.loads(raw_state)
        return state if isinstance(state, dict) else {}
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ):
        return {}


def configure_worker_login_totp(worker, two_factor: dict | None) -> bool:
    state = two_factor if isinstance(two_factor, dict) else {}
    secret = str(state.get("secret") or "").strip()
    if not secret:
        return False

    def current_code() -> str:
        return generate_totp(secret)

    worker.login_totp_provider = current_code
    return True


def configure_registration_profile_capture(app_backend, worker) -> bool:
    original = getattr(app_backend, "random_profile", None)
    if not callable(original):
        return False
    if getattr(original, "_hme_profile_capture", False):
        return True

    def captured_random_profile():
        result = original()
        if isinstance(result, (tuple, list)) and result:
            worker.registration_profile_name = str(result[0] or "").strip()
        return result

    captured_random_profile._hme_profile_capture = True
    app_backend.random_profile = captured_random_profile
    return True


def configure_passwordless_email_code_login(worker, *, enabled: bool) -> bool:
    """Prefer email OTP when an existing account asks for an unknown password."""

    original = getattr(worker, "_fill_password_step", None)
    if not enabled or not callable(original):
        return False
    if getattr(worker, "_hme_email_code_login_configured", False):
        return True

    def password_reset_form_ready(page) -> bool:
        if _first_visible(page, FORGOT_PASSWORD_SELECTORS, timeout=250) is not None:
            return False
        url = str(getattr(page, "url", "") or "").casefold()
        if any(marker in url for marker in ("password-reset", "reset-password", "new-password")):
            return True
        if _first_visible(
            page,
            (
                'input[autocomplete="new-password"]',
                'input[name*="new-password" i]',
                'input[name*="new_password" i]',
            ),
            timeout=300,
        ) is not None:
            return True
        try:
            body = str(page.locator("body").inner_text(timeout=500) or "").casefold()
        except Exception:
            body = ""
        return any(
            marker in body
            for marker in (
                "reset your password",
                "create a new password",
                "set a new password",
                "重置密码",
                "设置新密码",
                "重設密碼",
                "設定新密碼",
                "パスワードをリセット",
                "新しいパスワード",
            )
        )

    def fill_password_or_choose_code(self, page):
        if getattr(self, "_hme_password_reset_requested", False):
            if password_reset_form_ready(page):
                original(page)
                self._hme_password_reset_submitted = True
                self.log("[认证] 已在邮箱重置流程提交保存的唯一密码")
            else:
                self.log("[认证] 已请求重置密码，等待验证码或新密码页面")
            return None
        if _click_first_visible(page, ONE_TIME_CODE_LOGIN_SELECTORS, timeout=900):
            self._hme_email_code_login_selected = True
            self.log("[认证] 当前账号进入密码页，已改用一次性邮箱验证码登录")
            return None
        if _click_first_visible(page, FORGOT_PASSWORD_SELECTORS, timeout=700):
            self._hme_password_reset_requested = True
            self.log("[认证] 当前账号只有密码登录入口，已点击忘记密码")
            return None
        if getattr(self, "_hme_email_code_login_selected", False):
            self.log("[认证] 已选择邮箱验证码登录，等待验证码页面加载")
            return None
        return original(page)

    worker._fill_password_step = types.MethodType(fill_password_or_choose_code, worker)
    worker._hme_email_code_login_configured = True
    return True


def reusable_enabled_two_factor(two_factor: dict | None) -> dict:
    state = dict(two_factor) if isinstance(two_factor, dict) else {}
    if state.get("enabled") and str(state.get("secret") or "").strip():
        return state
    return {}


def _mfa_token_was_invalidated(error: Exception) -> bool:
    message = str(error or "").casefold()
    return "http 401" in message and any(
        marker in message
        for marker in ("token", "authentication", "invalidated", "signing in again")
    )


def _fontconfig_generator_with_home(generator, runtime_home: Path):
    """Run Camoufox's hard-coded fontconfig writer against a writable home."""

    def redirected(fontconfig_path: str) -> str:
        previous_home = os.environ.get("HOME")
        os.environ["HOME"] = str(runtime_home)
        try:
            return generator(fontconfig_path)
        finally:
            if previous_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous_home

    return redirected


def _configure_camoufox_runtime_cache(runtime_home: Path) -> Path:
    """Point Firefox's XDG font cache at the same writable runtime tree."""

    runtime_cache = runtime_home / ".cache"
    (runtime_cache / "camoufox" / "fontconfig").mkdir(
        parents=True,
        exist_ok=True,
    )
    (runtime_cache / "fontconfig").mkdir(parents=True, exist_ok=True)
    os.environ["XDG_CACHE_HOME"] = str(runtime_cache)
    return runtime_cache


def prepare_writable_camoufox_fontconfig():
    """Redirect Camoufox and Firefox fontconfig writes to writable /tmp."""

    if os.name != "posix":
        return None

    from camoufox import utils as camoufox_utils

    generator = getattr(camoufox_utils, "_generate_fontconfig", None)
    if not callable(generator):
        return None

    runtime_home = tempfile.TemporaryDirectory(
        prefix="hidemyemail-camoufox-",
        dir="/tmp",
    )
    runtime_root = Path(runtime_home.name)
    _configure_camoufox_runtime_cache(runtime_root)
    camoufox_utils._generate_fontconfig = _fontconfig_generator_with_home(
        generator,
        runtime_root,
    )
    return runtime_home


class MfaHttpClient:
    def __init__(self) -> None:
        import requests

        self.session = requests.Session()
        self.session.trust_env = False

    def post(self, url: str, **kwargs):
        kwargs["timeout"] = 60
        return self.session.post(url, **kwargs)

    def close(self) -> None:
        self.session.close()


def emit(kind: str, **payload) -> None:
    print(
        EVENT_PREFIX + json.dumps({"type": kind, **payload}, ensure_ascii=False),
        flush=True,
    )


def safe_log_message(message: str) -> str:
    text = str(message or "")
    text = re.sub(
        r"(已生成密码\s*[:：])\s*\S+",
        r"\1 [已安全保存]",
        text,
        flags=re.I,
    )
    return text[:1500]


def _locator_value_matches(locator, expected: str) -> bool:
    input_value = getattr(locator, "input_value", None)
    if not callable(input_value):
        return False
    try:
        actual = input_value(timeout=2000)
    except TypeError:
        try:
            actual = input_value()
        except Exception:
            return False
    except Exception:
        return False
    return str(actual or "") == expected


def resilient_force_fill_locator(worker, locator, value: str) -> bool:
    """Fill a React-controlled input and confirm that the value was applied."""
    expected = str(value)

    try:
        locator.click(timeout=3000, force=True)
    except TypeError:
        try:
            locator.click(timeout=3000)
        except Exception:
            pass
    except Exception:
        pass

    try:
        locator.fill(expected, timeout=7000, force=True)
    except TypeError:
        try:
            locator.fill(expected, timeout=7000)
        except Exception:
            pass
    except Exception:
        pass
    if _locator_value_matches(locator, expected):
        return True

    try:
        locator.evaluate(
            """(el, value) => {
                el.focus();
                const proto = el instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
                if (descriptor && descriptor.set) descriptor.set.call(el, value);
                else el.value = value;
                const inputEvent = typeof InputEvent === "function"
                    ? new InputEvent("input", {
                        bubbles: true,
                        composed: true,
                        inputType: "insertText",
                        data: value,
                    })
                    : new Event("input", { bubbles: true, composed: true });
                el.dispatchEvent(inputEvent);
                el.dispatchEvent(new Event("change", { bubbles: true }));
            }""",
            expected,
        )
    except Exception:
        pass
    if _locator_value_matches(locator, expected):
        worker.log("[认证] 已使用兼容输入方式填写密码")
        return True

    try:
        locator.click(timeout=3000, force=True)
        locator.press("Control+A", timeout=2000)
        locator.press("Backspace", timeout=2000)
        locator.type(expected, delay=25, timeout=10000)
    except TypeError:
        try:
            locator.click(timeout=3000)
            locator.press("Control+A")
            locator.press("Backspace")
            locator.type(expected, delay=25)
        except Exception:
            return False
    except Exception:
        return False
    if _locator_value_matches(locator, expected):
        worker.log("[认证] 已使用键盘输入方式填写密码")
        return True
    return False


def configure_windowed_camoufox(app_backend) -> bool:
    """Force Camoufox to use a centered, non-fullscreen outer window."""

    original = getattr(app_backend, "CamoufoxNewBrowser", None)
    if not callable(original):
        return False
    if getattr(original, "_hme_windowed", False):
        return True

    def windowed_camoufox(playwright, *args, **kwargs):
        kwargs.setdefault("window", CAMOUFOX_WINDOW_SIZE)
        firefox_user_prefs = dict(kwargs.get("firefox_user_prefs") or {})
        firefox_user_prefs.update(CAMOUFOX_PERSISTENT_STORAGE_PREFS)
        kwargs["firefox_user_prefs"] = firefox_user_prefs
        return original(playwright, *args, **kwargs)

    windowed_camoufox._hme_windowed = True
    app_backend.CamoufoxNewBrowser = windowed_camoufox
    return True


def _navigation_was_aborted(error: Exception) -> bool:
    message = str(error or "").casefold()
    return any(
        marker in message
        for marker in (
            "ns_binding_aborted",
            "navigation interrupted by another one",
            "navigation was interrupted by another navigation",
        )
    )


def configure_resilient_registration_navigation(worker, *, attempts: int = 3) -> bool:
    """Retry Firefox navigations that were superseded by an automatic redirect."""

    original_register = getattr(worker, "_register", None)
    if not callable(original_register):
        return False
    if getattr(worker, "_hme_resilient_registration_navigation", False):
        return True
    max_attempts = max(2, int(attempts))

    def register_with_resilient_navigation(page, context):
        original_goto = getattr(page, "goto", None)
        if not callable(original_goto):
            return original_register(page, context)

        def resilient_goto(url, *args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    return original_goto(url, *args, **kwargs)
                except Exception as error:
                    if not _navigation_was_aborted(error) or attempt >= max_attempts:
                        raise
                    safe_target = re.sub(r"[?#].*$", "", str(url or ""))[:160]
                    worker.log(
                        "[认证] 页面自动重定向打断了导航，"
                        f"正在重试 ({attempt + 1}/{max_attempts})：{safe_target}"
                    )
                    time.sleep(min(2.0, 0.5 * attempt))
            raise RuntimeError("页面导航重试次数已用尽")

        try:
            page.goto = resilient_goto
        except Exception:
            # Playwright Page currently permits an instance override. If a future
            # version does not, retain the original behavior instead of changing
            # unrelated registration semantics.
            return original_register(page, context)
        try:
            return original_register(page, context)
        finally:
            try:
                page.goto = original_goto
            except Exception:
                pass

    worker._register = register_with_resilient_navigation
    worker._hme_resilient_registration_navigation = True
    return True


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


def _click_first_visible(page, selectors: tuple[str, ...], *, timeout: int = 700) -> bool:
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


def _open_settings_from_profile(page, worker) -> bool:
    profile_name = str(
        getattr(worker, "registration_profile_name", "") or ""
    ).strip()
    if profile_name and _click_profile_name_by_dom(page, profile_name):
        _page_wait(page, 600)
        settings = _first_visible(page, SETTINGS_MENU_SELECTORS, timeout=900)
        if settings is not None and _click_locator(settings):
            worker.log(f"[密码] 已点击账号姓名 {profile_name} 并打开设置")
            return True
        worker.log("[密码] 姓名入口未打开设置菜单，继续查找账号入口")
        _dismiss_menu(page)
        _page_wait(page, 250)
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
        (PROFILE_MENU_FALLBACK_SELECTORS, False),
    )
    for selectors, strict in groups:
        for selector in selectors:
            for candidate in _visible_locators(page, selector, timeout=700):
                if not _profile_candidate_allowed(candidate, strict=strict):
                    continue
                if not _click_locator(candidate):
                    continue
                _page_wait(page, 600)
                settings = _first_visible(page, SETTINGS_MENU_SELECTORS, timeout=900)
                if settings is not None and _click_locator(settings):
                    worker.log("[密码] 已确认账号菜单并打开设置")
                    return True
                worker.log("[密码] 当前按钮未打开设置菜单，已忽略并继续查找账号入口")
                _dismiss_menu(page)
                _page_wait(page, 250)
    return False


def _completed_onboarding_visible(page, *, timeout: int = 500) -> bool:
    return (
        _first_visible(page, COMPLETED_ONBOARDING_MARKERS, timeout=timeout)
        is not None
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


def _open_security_settings(page, worker) -> bool:
    page.goto(CHATGPT_HOME_URL, wait_until="domcontentloaded", timeout=60000)
    _page_wait(page, 1200)
    _dismiss_completed_onboarding(page, worker)

    settings_clicked = _open_settings_from_profile(page, worker)
    if settings_clicked:
        _page_wait(page, 700)
    security_clicked = settings_clicked and _click_first_visible(
        page, SECURITY_TAB_SELECTORS
    )
    if security_clicked:
        _page_wait(page, 700)
        if _security_settings_ready(page):
            worker.log("[密码] 已确认进入安全设置")
            return True
        worker.log("[密码] 安全设置点击后未出现密码入口，继续尝试直达地址")

    worker.log("[密码] 设置菜单定位失败，改用安全设置直达地址")
    for target_url in (
        CHATGPT_SECURITY_SETTINGS_URL,
        "https://chatgpt.com/#settings/security",
        "https://chatgpt.com/#settings",
    ):
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        _page_wait(page, 1000)
        if _dismiss_completed_onboarding(page, worker):
            page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            _page_wait(page, 800)
        _click_first_visible(page, SECURITY_TAB_SELECTORS)
        _page_wait(page, 500)
        if _security_settings_ready(page, attempts=3):
            worker.log("[密码] 已通过直达地址确认安全设置页面")
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


def _fill_settings_otp(page, worker, min_timestamp: float) -> bool:
    inputs = []
    for selector in OTP_INPUT_SELECTORS:
        inputs = _visible_locators(page, selector, timeout=400)
        if inputs:
            break
    if not inputs:
        return False

    reader = ICloudOtpReader(worker.account, worker.log)
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
        raise RuntimeError("未能打开 ChatGPT 安全设置，请检查账号菜单结构")
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
        raise RuntimeError("安全设置中未找到添加或重置密码按钮")

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
            page, worker, verification_started_at
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
                raise RuntimeError(
                    f"Session 接口返回 HTTP {response.status}：{detail}"
                )
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
            if not getattr(worker, "skip_storage_state_capture", False):
                result["storage_state_json"] = json.dumps(
                    context.storage_state(), ensure_ascii=False
                )
            worker.log("[Session] 已在当前浏览器后台获取 Session 和 Access Token")
            return result
        except Exception as error:
            last_error = error
            if attempt < 4:
                worker.log(
                    f"[Session] 后台读取暂未成功（{attempt}/4），准备重试"
                )

    raise RuntimeError(f"无法在当前浏览器后台获取 Session：{last_error}")


def _enable_two_factor_before_browser_closes(
    worker,
    context,
    result: dict,
    pending_two_factor: dict | None,
) -> dict:
    """Finish 2FA while the worker's sync_playwright scope is still alive."""

    password = str(getattr(worker.account, "password", "") or "")
    password_confirmed = bool(
        getattr(worker, "_password_step_submitted", False)
    )
    emit(
        "account_registered",
        result=result,
        password=password,
        password_confirmed=password_confirmed,
    )
    worker._hme_account_registered_emitted = True

    two_factor = reusable_enabled_two_factor(pending_two_factor)
    if two_factor:
        worker.log("账号已有 TOTP 2FA，已保留现有启用状态")
    else:
        emit("two_factor_start")
        mfa_client = MfaHttpClient()
        try:
            mfa_pending = (
                dict(pending_two_factor)
                if isinstance(pending_two_factor, dict)
                else {}
            )

            def remember_enrolled(state):
                nonlocal mfa_pending
                mfa_pending = dict(state)
                emit("two_factor_enrolled", two_factor=state)

            for mfa_attempt in range(3):
                try:
                    two_factor = enable_totp_mfa(
                        mfa_client,
                        access_token=str(result.get("access_token") or ""),
                        email=str(worker.account.email or ""),
                        pending=mfa_pending,
                        on_enrolled=remember_enrolled,
                    )
                    break
                except MfaSetupError as error:
                    if (
                        not _mfa_token_was_invalidated(error)
                        or mfa_attempt >= 2
                    ):
                        raise
                    worker.log(
                        "[2FA] 密码变更使当前 Token 失效，"
                        f"正在浏览器关闭前刷新 Session（{mfa_attempt + 1}/2）"
                    )
                    page = _reuse_single_registration_page(context, worker)
                    try:
                        page.goto(
                            CHATGPT_HOME_URL,
                            wait_until="domcontentloaded",
                            timeout=60000,
                        )
                        _page_wait(page, 1500 + mfa_attempt * 1000)
                    except Exception:
                        _page_wait(page, 2000 + mfa_attempt * 1000)
                    refreshed = extract_session_without_navigation(worker, context)
                    result.update(refreshed)
                    emit(
                        "account_registered",
                        result=result,
                        password=password,
                        password_confirmed=password_confirmed,
                    )
        finally:
            mfa_client.close()

    result["two_factor"] = two_factor
    emit("two_factor_enabled")
    worker._hme_two_factor_completed = True
    return result


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
    """Run the settings password flow before the worker opens the Session page."""

    if not enabled:
        return False
    original_extract = getattr(worker, "_extract_session_info", None)
    if not callable(original_extract):
        raise RuntimeError("目标注册工作器缺少 Session 提取方法")

    def extract_after_password_setup(context):
        if getattr(worker, "_hme_password_reset_submitted", False):
            worker._password_step_submitted = True
            worker.log("[密码] 邮箱重置后已成功建立会话，确认唯一密码生效")
        if not getattr(worker, "_password_step_submitted", False):
            ensure_password_in_security_settings(
                app_backend,
                worker,
                password,
                context=context,
                force_reset_password=force_reset_password,
            )
        result = extract_session_without_navigation(worker, context)
        if enable_2fa:
            result = _enable_two_factor_before_browser_closes(
                worker,
                context,
                result,
                pending_two_factor,
            )
        return result

    worker._extract_session_info = extract_after_password_setup
    return True


def iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class ICloudOtpReader:
    def __init__(self, account, log, _proxy_url: str = "") -> None:
        import requests

        self.email = str(account.email or "").strip().lower()
        self.log = log
        self.service_url = os.environ.get(
            "HME_BROWSER_SERVICE_URL", "http://127.0.0.1:8765"
        ).rstrip("/")
        self.token = os.environ.get("HME_BROWSER_WORKER_TOKEN", "")
        self.session = requests.Session()
        self.session.trust_env = False

    def connect(self) -> None:
        if not self.token:
            raise RuntimeError("iCloud 浏览器工作器令牌未配置")
        try:
            response = self.session.get(self.service_url + "/healthz", timeout=5)
            response.raise_for_status()
        except Exception as error:
            raise RuntimeError(f"无法连接 iCloud 邮箱服务：{error}") from error
        self.log("iCloud 收码通道已连接")

    def wait_for_code(self, min_timestamp: float) -> str:
        deadline = time.time() + 240
        since = iso_timestamp(min_timestamp)
        last_error = ""
        while time.time() < deadline:
            try:
                response = self.session.post(
                    self.service_url + "/api/gpt-code",
                    headers={"X-Local-Token": self.token},
                    json={"email": self.email, "since": since},
                    timeout=40,
                )
                if response.status_code == 404:
                    time.sleep(OTP_POLL_INTERVAL_SECONDS)
                    continue
                payload = response.json()
                if response.ok and payload.get("ok"):
                    code = re.sub(r"[^A-Za-z0-9]", "", str(payload.get("code") or ""))
                    if 4 <= len(code) <= 10:
                        self.log("已从 iCloud 转发收件箱获取对应邮箱的新验证码")
                        return code
                last_error = str(payload.get("error") or f"HTTP {response.status_code}")
            except Exception as error:
                last_error = str(error)
            time.sleep(OTP_POLL_INTERVAL_SECONDS)
        detail = f"：{last_error}" if last_error else ""
        raise TimeoutError(f"iCloud 在 240 秒内未收到该邮箱的新验证码{detail}")

    def close(self) -> None:
        self.session.close()


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
    parser = argparse.ArgumentParser(description="iCloud OpenAI Camoufox bridge")
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--headless", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source_dir = Path(args.source_dir).resolve()
    if not (source_dir / "app_backend.py").is_file():
        emit("error", error=f"目标项目缺少 app_backend.py：{source_dir}")
        return 2
    sys.path.insert(0, str(source_dir))

    password = os.environ.get("HME_OPENAI_PASSWORD", "")
    ensure_password = os.environ.get("HME_ENSURE_OPENAI_PASSWORD", "") == "1"
    force_reset_password = (
        os.environ.get("HME_FORCE_RESET_OPENAI_PASSWORD", "") == "1"
    )
    enable_2fa = os.environ.get("HME_ENABLE_OPENAI_2FA", "") == "1"
    saved_storage_state = (
        load_saved_storage_state(
            os.environ.get("HME_BROWSER_DB_FILE", ""),
            args.email,
        )
        if ensure_password
        else {}
    )
    try:
        pending_2fa = json.loads(os.environ.get("HME_OPENAI_2FA_STATE", "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        pending_2fa = {}
    account = None
    try:
        ensure_tkinter_importable()
        # Keep the temporary directory alive for the complete browser run.
        _camoufox_runtime = prepare_writable_camoufox_fontconfig()
        if _camoufox_runtime is not None:
            emit("log", message="[运行环境] Camoufox fontconfig 已切换到可写临时目录")
        import app_backend
        from account_models import MailAccount

        app_backend.HotmailOtpReader = ICloudOtpReader
        configure_windowed_camoufox(app_backend)
        app_backend.OpenAIRegisterPayLinkWorker._force_fill_locator = (
            resilient_force_fill_locator
        )
        account = MailAccount(
            email=args.email.strip().lower(),
            password=password,
            client_id="icloud",
            refresh_token="icloud",
            raw="",
        )
        proxy = app_backend.ProxyConfig(
            local_proxy="", dynamic_proxy="", chain_url=""
        )

        def log(message: str) -> None:
            emit("log", message=safe_log_message(message))

        worker = app_backend.OpenAIRegisterPayLinkWorker(
            account,
            "",
            bool(args.headless),
            proxy,
            proxy,
            log,
            browser_engine="camoufox",
        )
        configure_passwordless_email_code_login(worker, enabled=ensure_password)
        configure_worker_login_totp(worker, pending_2fa)
        configure_registration_profile_capture(app_backend, worker)
        configure_resilient_registration_navigation(worker)
        worker.require_password_setup = ensure_password
        worker.initial_storage_state = saved_storage_state or None
        # Session/AT are sufficient for this service. Camoufox can occasionally
        # stall indefinitely while exporting a complete browser storage snapshot;
        # the database merge keeps any previously saved snapshot intact.
        worker.skip_storage_state_capture = True
        configure_post_registration_password_setup(
            app_backend,
            worker,
            str(account.password or ""),
            enabled=ensure_password,
            force_reset_password=force_reset_password,
            enable_2fa=bool(enable_2fa and ensure_password),
            pending_two_factor=pending_2fa,
        )
        result = worker.run()
        password_confirmed = bool(
            getattr(worker, "_password_step_submitted", False)
        )
        if ensure_password and not password_confirmed:
            raise RuntimeError("OpenAI 端未确认密码设置，未保存本地密码")
        if enable_2fa:
            if not getattr(worker, "_hme_account_registered_emitted", False):
                emit(
                    "account_registered",
                    result=result,
                    password=str(account.password or ""),
                    password_confirmed=password_confirmed,
                )
            if getattr(worker, "_hme_two_factor_completed", False):
                two_factor = result.get("two_factor")
            else:
                two_factor = reusable_enabled_two_factor(pending_2fa)
            if not getattr(worker, "_hme_two_factor_completed", False) and two_factor:
                emit("log", message="账号已有 TOTP 2FA，已保留现有启用状态")
            elif not getattr(worker, "_hme_two_factor_completed", False):
                emit("two_factor_start")
                mfa_client = MfaHttpClient()
                try:
                    mfa_pending = dict(pending_2fa)

                    def remember_enrolled(state):
                        nonlocal mfa_pending
                        mfa_pending = dict(state)
                        emit("two_factor_enrolled", two_factor=state)

                    for mfa_attempt in range(3):
                        try:
                            two_factor = enable_totp_mfa(
                                mfa_client,
                                access_token=str(result.get("access_token") or ""),
                                email=str(account.email or ""),
                                pending=mfa_pending,
                                on_enrolled=remember_enrolled,
                            )
                            break
                        except MfaSetupError as error:
                            if (
                                not _mfa_token_was_invalidated(error)
                                or mfa_attempt >= 2
                            ):
                                raise
                            emit(
                                "log",
                                message=(
                                    "[2FA] 密码变更使当前 Token 失效，"
                                    f"正在刷新 Session 后重试（{mfa_attempt + 1}/2）"
                                ),
                            )
                            retained_context = _retained_registration_context(
                                app_backend, str(account.email or "")
                            )
                            if retained_context is None:
                                raise RuntimeError(
                                    "2FA Token 已失效，且无法复用当前浏览器刷新 Session"
                                ) from error
                            retained_page = _reuse_single_registration_page(
                                retained_context, worker
                            )
                            try:
                                retained_page.goto(
                                    CHATGPT_HOME_URL,
                                    wait_until="domcontentloaded",
                                    timeout=60000,
                                )
                                _page_wait(retained_page, 1500 + mfa_attempt * 1000)
                            except Exception:
                                _page_wait(retained_page, 2000 + mfa_attempt * 1000)
                            refreshed = extract_session_without_navigation(
                                worker, retained_context
                            )
                            result.update(refreshed)
                            emit(
                                "account_registered",
                                result=result,
                                password=str(account.password or ""),
                                password_confirmed=password_confirmed,
                            )
                finally:
                    mfa_client.close()
            if not getattr(worker, "_hme_two_factor_completed", False):
                result["two_factor"] = two_factor
                emit("two_factor_enabled")
        emit(
            "result",
            result=result,
            password=str(account.password or ""),
            password_confirmed=password_confirmed,
        )
        return 0
    except KeyboardInterrupt:
        emit(
            "error",
            error="浏览器任务已停止",
            password=str(getattr(account, "password", "") or ""),
            password_confirmed=False,
        )
        return 130
    except Exception as error:
        emit(
            "error",
            error=safe_log_message(str(error)),
            password=str(getattr(account, "password", "") or ""),
            password_confirmed=bool(
                getattr(locals().get("worker"), "_password_step_submitted", False)
            ),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
