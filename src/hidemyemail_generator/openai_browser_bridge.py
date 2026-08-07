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
CAMOUFOX_AUTH_RESOURCE_CACHE_PREFS = {
    # Every registration uses a disposable browser/context.  Keeping the
    # in-memory HTTP cache enabled inside that isolated run lets OpenAI's auth
    # CSS/JavaScript survive redirects and transient direct-connection errors
    # without sharing data between accounts.
    "browser.cache.memory.enable": True,
    "network.http.use-cache": True,
}
AUTH_RESOURCE_RELOAD_ATTEMPTS = 2
CHATGPT_HOME_URL = "https://chatgpt.com/"
CHATGPT_ACCOUNT_SETTINGS_URL = "https://chatgpt.com/#settings/Account"
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
    'button[aria-label*="プロフィールメニューを開く" i]',
    'button[aria-label*="プロファイルメニューを開く" i]',
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
PASSWORD_CONTINUE_SELECTORS = (
    'button:has-text("Continue with password")',
    '[role="button"]:has-text("Continue with password")',
    'a:has-text("Continue with password")',
    'button:has-text("使用密码继续")',
    '[role="button"]:has-text("使用密码继续")',
    'a:has-text("使用密码继续")',
    'button:has-text("使用密碼繼續")',
    '[role="button"]:has-text("使用密碼繼續")',
    'a:has-text("使用密碼繼續")',
    'button:has-text("パスワードで続行")',
    '[role="button"]:has-text("パスワードで続行")',
    'a:has-text("パスワードで続行")',
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
ACCOUNT_TAB_SELECTORS = (
    '[data-testid="account-tab"]',
    '[role="tab"]:has-text("Account")',
    'button:has-text("Account")',
    'a:has-text("Account")',
    '[role="tab"]:has-text("账户")',
    '[role="tab"]:has-text("帐户")',
    '[role="tab"]:has-text("帳戶")',
    'button:has-text("账户")',
    'button:has-text("帐户")',
    'button:has-text("帳戶")',
    'a:has-text("账户")',
    'a:has-text("帐户")',
    'a:has-text("帳戶")',
    '[role="tab"]:has-text("アカウント")',
    'button:has-text("アカウント")',
    'a:has-text("アカウント")',
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
  const candidates = Array.from(document.querySelectorAll(
    'button, [role="button"], a, [aria-haspopup="menu"], [data-testid*="profile" i]'
  ))
    .filter(element => visible(element))
    .filter(element => {
      const text = normalize(element.textContent);
      const label = normalize(element.getAttribute("aria-label"));
      return text.includes(expected) || label.includes(expected);
    })
    .sort((left, right) => {
      const a = left.getBoundingClientRect();
      const b = right.getBoundingClientRect();
      return (a.width * a.height) - (b.width * b.height);
    });
  for (const target of candidates) {
    target.click();
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
        if isinstance(raw_state, str) and raw_state.strip():
            raw_state = json.loads(raw_state)
        if isinstance(raw_state, dict):
            state = dict(raw_state)
            if isinstance(state.get("cookies"), list) and state["cookies"]:
                state.setdefault("origins", [])
                return state

        for key in ("cookies", "cookies_json"):
            cookies = record.get(key)
            if isinstance(cookies, str) and cookies.strip():
                cookies = json.loads(cookies)
            if isinstance(cookies, list) and cookies:
                return {
                    "cookies": [dict(item) for item in cookies if isinstance(item, dict)],
                    "origins": [],
                }
        return {}
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


def configure_password_first_login(worker, *, enabled: bool) -> bool:
    """Choose password on the initial email-code page and submit the saved password."""

    original_has_otp = getattr(worker, "_has_otp_input", None)
    original_fill_password = getattr(worker, "_fill_password_step", None)
    original_continue_registration = getattr(
        worker, "_continue_chatgpt_registration_complete", None
    )
    if (
        not enabled
        or not callable(original_has_otp)
        or not callable(original_fill_password)
    ):
        return False
    if getattr(worker, "_hme_password_first_login_configured", False):
        return True

    def choose_password_if_available(self, page) -> bool:
        if getattr(self, "_hme_password_entry_selected", False):
            return False
        if not _click_first_visible(page, PASSWORD_CONTINUE_SELECTORS, timeout=500):
            return False
        self._hme_password_entry_selected = True
        self._hme_password_entry_pending = True
        self._hme_password_entry_started_at = time.monotonic()
        self.log("[认证] 已选择使用密码继续，等待密码输入页面")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _first_visible(page, PASSWORD_INPUT_SELECTORS, timeout=250) is not None:
                return True
            _page_wait(page, 200)
        raise RuntimeError("已点击使用密码继续，但 15 秒内没有进入密码页面")

    def continue_registration_and_choose_password(self, page):
        result = original_continue_registration(page)
        if choose_password_if_available(self, page):
            return result
        if (
            not getattr(self, "_hme_password_entry_selected", False)
            and original_has_otp(page)
            and not getattr(self, "_hme_password_entry_unavailable_logged", False)
        ):
            self._hme_password_entry_unavailable_logged = True
            self.log("[认证] 当前验证码页面没有使用密码入口，继续读取邮箱验证码")
        return result

    def has_otp_or_choose_password(self, page):
        has_otp = bool(original_has_otp(page))
        if not has_otp:
            return False
        if getattr(self, "_hme_password_entry_pending", False):
            started_at = float(getattr(self, "_hme_password_entry_started_at", 0) or 0)
            if time.monotonic() - started_at <= 15:
                return False
            raise RuntimeError("已点击使用密码继续，但 15 秒内没有进入密码页面")
        if getattr(self, "_hme_password_entry_selected", False):
            return has_otp
        if not choose_password_if_available(self, page):
            if not getattr(self, "_hme_password_entry_unavailable_logged", False):
                self._hme_password_entry_unavailable_logged = True
                self.log("[认证] 当前验证码页面没有使用密码入口，继续读取邮箱验证码")
            return has_otp
        return False

    def fill_saved_password(self, page):
        original_fill_password(page)
        self._hme_password_entry_pending = False
        self._password_step_submitted = True
        self.log("[认证] 已提交创建邮箱时保存的唯一密码")

    worker._has_otp_input = types.MethodType(has_otp_or_choose_password, worker)
    worker._fill_password_step = types.MethodType(fill_saved_password, worker)
    if callable(original_continue_registration):
        worker._continue_chatgpt_registration_complete = types.MethodType(
            continue_registration_and_choose_password, worker
        )
    worker._hme_password_first_login_configured = True
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


def _activate_visible_registration_page(worker, page) -> bool:
    """Activate the visible registration tab before mouse/keyboard input."""

    if bool(getattr(worker, "headless", False)):
        return False
    try:
        page.bring_to_front()
        try:
            page.evaluate("() => window.focus()")
        except Exception:
            pass
        return True
    except Exception as error:
        if not getattr(worker, "_hme_about_you_focus_warning_logged", False):
            worker.log(
                "[基础资料] 浏览器窗口激活失败，将继续使用 DOM 输入并严格核验："
                f"{safe_log_message(error)}"
            )
            worker._hme_about_you_focus_warning_logged = True
        return False


def _about_you_profile_values_match(
    worker,
    page,
    name: str,
    birthdate: str,
    birth_year: str,
    age: str,
) -> tuple[bool, list[str], str]:
    values_reader = getattr(worker, "_visible_input_values", None)
    context_reader = getattr(worker, "_about_you_second_field_context", None)
    kind_reader = getattr(worker, "_about_you_second_field_kind_from_context", None)
    value_reader = getattr(worker, "_about_you_second_field_value", None)
    semantic_validator = getattr(worker, "_about_you_values_ok", None)
    if not all(
        callable(method)
        for method in (
            values_reader,
            context_reader,
            kind_reader,
            value_reader,
            semantic_validator,
        )
    ):
        return False, [], "unknown"

    try:
        context = str(context_reader(page) or "")
        second_kind = str(kind_reader(context) or "birth_year")
        expected_second = str(
            value_reader(second_kind, birth_year, age, birthdate, context) or ""
        ).strip()
        values = [str(value or "").strip() for value in values_reader(page)]
    except Exception:
        return False, [], "unknown"

    if not values or values[0] != str(name or "").strip():
        return False, values, second_kind
    if second_kind == "birth_date":
        return bool(semantic_validator(values, second_kind)), values, second_kind
    if len(values) < 2 or values[1] != expected_second:
        return False, values, second_kind
    return bool(semantic_validator(values, second_kind)), values, second_kind


def configure_resilient_about_you_input(worker) -> bool:
    """Keep visible about-you input focused and reject unverified field values."""

    if getattr(worker, "_hme_about_you_input_configured", False):
        return True
    original_keyboard_fill = getattr(worker, "_fill_visible_input_by_keyboard", None)
    original_profile_fill = getattr(worker, "_fill_about_you_inputs", None)
    dom_profile_fill = getattr(worker, "_fill_about_you_inputs_by_dom", None)
    focus_submit = getattr(worker, "_focus_about_you_submit_or_body", None)
    if not callable(original_keyboard_fill) or not callable(original_profile_fill):
        return False

    def keyboard_fill_with_foreground(self, page, index: int, value: str):
        activated = _activate_visible_registration_page(self, page)
        if activated and not getattr(self, "_hme_about_you_focus_logged", False):
            self.log("[基础资料] 键盘输入前已激活当前浏览器窗口")
            self._hme_about_you_focus_logged = True
        return original_keyboard_fill(page, index, value)

    def profile_fill_with_readback(
        self,
        page,
        name: str,
        birthdate: str,
        birth_year: str,
        age: str,
    ):
        _activate_visible_registration_page(self, page)
        result = original_profile_fill(page, name, birthdate, birth_year, age)
        matched, values, second_kind = _about_you_profile_values_match(
            self, page, name, birthdate, birth_year, age
        )
        if matched:
            self.log("[基础资料] 姓名与年龄/出生信息回读校验通过")
            return result

        self.log(
            "[基础资料] 键盘输入回读不一致，改用 DOM 事件重填；"
            f"字段类型={second_kind}，当前值={values}"
        )
        if callable(dom_profile_fill):
            context_reader = getattr(self, "_about_you_second_field_context", None)
            kind_reader = getattr(self, "_about_you_second_field_kind_from_context", None)
            value_reader = getattr(self, "_about_you_second_field_value", None)
            if all(callable(method) for method in (context_reader, kind_reader, value_reader)):
                context = str(context_reader(page) or "")
                second_kind = str(kind_reader(context) or "birth_year")
                second_value = str(
                    value_reader(second_kind, birth_year, age, birthdate, context) or ""
                )
                dom_profile_fill(page, name, second_value, second_kind)
                if callable(focus_submit):
                    focus_submit(page)

        matched, values, second_kind = _about_you_profile_values_match(
            self, page, name, birthdate, birth_year, age
        )
        if matched:
            self.log("[基础资料] DOM 重填后回读校验通过")
            return result
        raise RuntimeError(
            "基础资料自动填写未确认成功，已停止提交以避免发送错误资料；"
            f"字段类型={second_kind}，当前值={values}"
        )

    worker._fill_visible_input_by_keyboard = types.MethodType(
        keyboard_fill_with_foreground, worker
    )
    worker._fill_about_you_inputs = types.MethodType(
        profile_fill_with_readback, worker
    )
    worker._hme_about_you_input_configured = True
    return True


def configure_windowed_camoufox(app_backend) -> bool:
    """Force Camoufox to use a centered, non-fullscreen outer window."""

    original = getattr(app_backend, "CamoufoxNewBrowser", None)
    if not callable(original):
        return False
    if getattr(original, "_hme_windowed", False):
        return True

    def windowed_camoufox(playwright, *args, **kwargs):
        slot_index, slot_count = _browser_window_slot_from_environment()
        layout = _camoufox_window_layout(slot_index, slot_count)
        kwargs.setdefault("window", (layout["width"], layout["height"]))
        firefox_user_prefs = dict(kwargs.get("firefox_user_prefs") or {})
        firefox_user_prefs.update(CAMOUFOX_PERSISTENT_STORAGE_PREFS)
        if not str(os.environ.get("HME_REGISTRATION_PROXY_URL") or "").strip():
            kwargs["enable_cache"] = True
            firefox_user_prefs.update(CAMOUFOX_AUTH_RESOURCE_CACHE_PREFS)
        kwargs["firefox_user_prefs"] = firefox_user_prefs
        browser = original(playwright, *args, **kwargs)
        if not kwargs.get("headless") and slot_count > 1:
            # Camoufox creates its visible top-level window only after the
            # caller opens a context/page.  Move it in the background so this
            # wrapper can return and let page creation proceed.
            import threading

            threading.Thread(
                target=_move_camoufox_window,
                args=(browser, layout),
                name=f"camoufox-window-slot-{slot_index + 1}",
                daemon=True,
            ).start()
        return browser

    windowed_camoufox._hme_windowed = True
    app_backend.CamoufoxNewBrowser = windowed_camoufox
    return True


def _browser_window_slot_from_environment() -> tuple[int, int]:
    try:
        slot_count = max(
            1,
            min(10, int(os.environ.get("HME_BROWSER_WINDOW_SLOTS") or "1")),
        )
    except (TypeError, ValueError):
        slot_count = 1
    try:
        slot_index = max(
            0,
            min(
                slot_count - 1,
                int(os.environ.get("HME_BROWSER_WINDOW_SLOT") or "0"),
            ),
        )
    except (TypeError, ValueError):
        slot_index = 0
    return slot_index, slot_count


def _primary_screen_size() -> tuple[int, int]:
    if os.name == "nt":
        try:
            import ctypes

            width = int(ctypes.windll.user32.GetSystemMetrics(0))
            height = int(ctypes.windll.user32.GetSystemMetrics(1))
            if width >= 1024 and height >= 720:
                return width, height
        except Exception:
            pass
    return 1920, 1080


def _camoufox_window_layout(
    slot_index: int,
    slot_count: int,
    *,
    screen_size: tuple[int, int] | None = None,
) -> dict[str, int]:
    screen_width, screen_height = screen_size or _primary_screen_size()
    count = max(1, min(10, int(slot_count)))
    index = max(0, min(count - 1, int(slot_index)))
    if count == 1:
        width, height = CAMOUFOX_WINDOW_SIZE
        return {
            "slot": 0,
            "slots": 1,
            "x": max(0, (screen_width - width) // 2),
            "y": max(0, (screen_height - height) // 2),
            "width": width,
            "height": height,
        }
    columns = min(3, count)
    rows = (count + columns - 1) // columns
    tile_width = max(1, screen_width // columns)
    usable_height = max(720, screen_height - 60)
    tile_height = max(1, usable_height // rows)
    margin = 10
    width = max(560, tile_width - margin * 2)
    height = max(560, min(900, tile_height - margin * 2))
    column = index % columns
    row = index // columns
    return {
        "slot": index,
        "slots": count,
        "x": column * tile_width + margin,
        "y": row * tile_height + margin,
        "width": width,
        "height": height,
    }


def _windows_descendant_process_ids(root_pid: int) -> set[int]:
    if os.name != "nt" or root_pid <= 0:
        return {root_pid} if root_pid > 0 else set()
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessEntry32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot in {0, -1}:
            return {root_pid}
        parents: dict[int, int] = {}
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                parents[int(entry.th32ProcessID)] = int(
                    entry.th32ParentProcessID
                )
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        kernel32.CloseHandle(snapshot)
        descendants = {int(root_pid)}
        changed = True
        while changed:
            changed = False
            for process_id, parent_id in parents.items():
                if parent_id in descendants and process_id not in descendants:
                    descendants.add(process_id)
                    changed = True
        return descendants
    except Exception:
        return {root_pid}


def _move_camoufox_window(browser, layout: dict[str, int]) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        process = browser._impl_obj._connection._transport._proc
        root_pid = int(process.pid)
        user32 = ctypes.windll.user32
        moved = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            process_ids = _windows_descendant_process_ids(root_pid)
            handles: list[int] = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def collect_window(hwnd, _lparam):
                process_id = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
                if (
                    int(process_id.value) in process_ids
                    and user32.IsWindowVisible(hwnd)
                ):
                    handles.append(int(hwnd))
                return True

            user32.EnumWindows(collect_window, 0)
            for hwnd in handles:
                moved = bool(
                    user32.MoveWindow(
                        hwnd,
                        int(layout["x"]),
                        int(layout["y"]),
                        int(layout["width"]),
                        int(layout["height"]),
                        True,
                    )
                ) or moved
            # Firefox may replace or reposition its initial top-level window
            # while the first context/page is being created.  Keep applying
            # the slot briefly so the final browser window stays tiled.
            time.sleep(0.1)
        return moved
    except Exception:
        return False


def _auth_page_resource_state(page) -> dict:
    try:
        state = page.evaluate(
            """() => {
                const host = String(location.hostname || '').toLowerCase();
                const isAuthPage = host === 'auth.openai.com'
                    || host.endsWith('.auth.openai.com');
                const styleSheets = Array.from(document.styleSheets || []);
                const loadedLinks = Array.from(
                    document.querySelectorAll('link[rel~="stylesheet"]')
                ).filter((link) => Boolean(link.sheet));
                return {
                    isAuthPage,
                    styleSheetCount: styleSheets.length,
                    loadedStyleLinkCount: loadedLinks.length,
                };
            }"""
        )
    except Exception:
        return {"isAuthPage": False, "styleSheetCount": 1, "loadedStyleLinkCount": 0}
    return state if isinstance(state, dict) else {}


def ensure_auth_page_resources(
    page,
    log,
    *,
    reload_attempts: int = AUTH_RESOURCE_RELOAD_ATTEMPTS,
) -> bool:
    """Reload an unstyled OpenAI password page before submitting its form."""

    attempts = max(0, int(reload_attempts))
    for attempt in range(attempts + 1):
        for _ in range(8):
            state = _auth_page_resource_state(page)
            if not state.get("isAuthPage") or int(state.get("styleSheetCount") or 0) > 0:
                return True
            _page_wait(page, 500)
        if attempt >= attempts:
            break
        log(
            "[认证] OpenAI 密码页样式资源尚未加载，"
            f"保持本机 IP 直连并重新加载 ({attempt + 1}/{attempts})"
        )
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
        except TypeError:
            page.reload()
        except Exception as error:
            log(f"[认证] 直连页面重新加载未完成：{safe_log_message(error)}")
        _page_wait(page, 1500)
    raise RuntimeError(
        "OpenAI 认证页 CSS/JavaScript 未完整加载；当前保持本机 IP 直连，"
        "请检查本机网络或 Cloudflare 验证后重试"
    )


def configure_direct_registration_browser(
    worker,
    *,
    enabled: bool,
    locale: str = "",
) -> bool:
    """Keep proxy-free registration local while stabilizing its auth page."""

    if not enabled:
        return False
    if getattr(worker, "_hme_direct_registration_configured", False):
        return True
    original_new_context = getattr(worker, "_new_browser_context", None)
    original_fill_password = getattr(worker, "_fill_password_step", None)
    original_log = getattr(worker, "log", None)
    if (
        not callable(original_new_context)
        or not callable(original_fill_password)
        or not callable(original_log)
    ):
        return False

    def direct_log(message):
        text = str(message or "")
        text = text.replace(
            "浏览器 HTTP 缓存保持禁用",
            "当前隔离任务启用内存资源缓存",
        )
        text = text.replace(
            "HTTP 缓存禁用",
            "隔离任务内存缓存启用",
        )
        if locale:
            text = text.replace(
                "GeoIP/时区/语言/WebRTC 自动对齐",
                f"GeoIP/时区/WebRTC 自动对齐 · 语言 {locale}",
            )
        return original_log(text)

    def new_direct_context(
        self,
        playwright,
        proxy,
        storage_state=None,
        *args,
        **kwargs,
    ):
        if locale and not str(kwargs.get("locale_override") or "").strip():
            kwargs["locale_override"] = str(locale)
        return original_new_context(
            playwright,
            proxy,
            storage_state,
            *args,
            **kwargs,
        )

    def fill_password_after_resource_check(self, page):
        ensure_auth_page_resources(page, self.log)
        return original_fill_password(page)

    worker._new_browser_context = types.MethodType(new_direct_context, worker)
    worker._fill_password_step = types.MethodType(
        fill_password_after_resource_check, worker
    )
    worker.log = direct_log
    worker._hme_direct_registration_configured = True
    return True


def detect_direct_registration_location(app_backend, log) -> dict[str, str]:
    """Detect the real local exit without introducing a proxy."""

    detector = getattr(app_backend, "detect_proxy_health", None)
    locale_for_country = getattr(app_backend, "country_browser_locale", None)
    if not callable(detector) or not callable(locale_for_country):
        return {"country": "", "locale": "", "timezone": ""}
    try:
        health = detector("", timeout=12, check_stripe=False)
    except Exception as error:
        log(
            "[直连] 本机公网出口地区检测失败，"
            f"浏览器将继续使用 Camoufox GeoIP 自动语言：{safe_log_message(error)}"
        )
        return {"country": "", "locale": "", "timezone": ""}
    country = str(getattr(health, "country", "") or "").strip().upper()
    timezone_name = str(getattr(health, "timezone", "") or "").strip()
    locale = str(locale_for_country(country) or "").strip() if country else ""
    return {
        "country": country,
        "locale": locale,
        "timezone": timezone_name,
    }


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
    # Keep the DOM fallback bounded to interactive elements.  Scanning every
    # node in ChatGPT's large app DOM can leave Playwright waiting forever.
    if profile_name and _click_profile_name_by_dom(page, profile_name):
        _page_wait(page, 600)
        settings = _first_visible(page, SETTINGS_MENU_SELECTORS, timeout=900)
        if settings is not None and _click_locator(settings):
            worker.log(f"[密码] 已点击账号姓名 {profile_name} 并打开设置")
            return True
        worker.log("[密码] 姓名入口未打开设置菜单，继续查找账号入口")
        _dismiss_menu(page)
        _page_wait(page, 250)
    for selector in PROFILE_MENU_FALLBACK_SELECTORS:
        for candidate in _visible_locators(page, selector, timeout=700):
            if not _profile_candidate_allowed(candidate, strict=False):
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
    if not password_confirmed:
        worker.log("[2FA] 密码尚未设置成功，已跳过开启 2FA")
        return result

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
    """Save the authenticated Session before attempting optional account setup."""

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
        result = extract_session_without_navigation(worker, context)
        password_confirmed = bool(
            getattr(worker, "_password_step_submitted", False)
        )
        emit(
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
                ensure_password_in_security_settings(
                    app_backend,
                    worker,
                    password,
                    context=context,
                    force_reset_password=force_reset_password,
                )
            except Exception as error:
                worker._hme_password_setup_error = safe_log_message(str(error))
                worker.log(
                    "[密码] 自动设置未成功；注册 Session 已保存，可稍后单独设置密码"
                )
        if getattr(worker, "_password_step_submitted", False):
            # Password changes may rotate the authenticated token.  Refresh the
            # saved Session only after OpenAI confirms the password operation.
            result = extract_session_without_navigation(worker, context)
            emit(
                "account_registered",
                result=result,
                password=str(getattr(worker.account, "password", "") or ""),
                password_confirmed=True,
            )
        if enable_2fa and getattr(worker, "_password_step_submitted", False):
            result = _enable_two_factor_before_browser_closes(
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


def iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class ManualOtpReader:
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
            raise RuntimeError("浏览器工作器令牌未配置")
        try:
            response = self.session.get(self.service_url + "/healthz", timeout=5)
            response.raise_for_status()
        except Exception as error:
            raise RuntimeError(f"无法连接手动验证码服务：{error}") from error
        self.log("手动验证码通道已连接；需要验证码时请在工作台输入")

    def wait_for_code(self, _min_timestamp: float) -> str:
        deadline = time.time() + 600
        last_error = ""
        while time.time() < deadline:
            try:
                response = self.session.post(
                    self.service_url + "/api/registration/code/poll",
                    headers={"X-Local-Token": self.token},
                    json={"email": self.email},
                    timeout=10,
                )
                if response.status_code == 404:
                    time.sleep(OTP_POLL_INTERVAL_SECONDS)
                    continue
                payload = response.json()
                if response.ok and payload.get("ok"):
                    code = re.sub(r"[^A-Za-z0-9]", "", str(payload.get("code") or ""))
                    if 4 <= len(code) <= 10:
                        self.log("已收到工作台手动输入的验证码")
                        return code
                last_error = str(payload.get("error") or f"HTTP {response.status_code}")
            except Exception as error:
                last_error = str(error)
            time.sleep(OTP_POLL_INTERVAL_SECONDS)
        detail = f"：{last_error}" if last_error else ""
        raise TimeoutError(f"在 600 秒内未收到手动输入的验证码{detail}")

    def close(self) -> None:
        self.session.close()


# Keep the public name used by older integrations while routing it to manual entry.
ICloudOtpReader = ManualOtpReader


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


def require_registration_proxy_country(health, expected_country: str) -> str:
    expected = str(expected_country or "").strip().upper()
    actual = str(getattr(health, "country", "") or "").strip().upper()
    if expected and actual != expected:
        raise RuntimeError(
            f"注册代理出口国家不符：要求 {expected}，实际 {actual or '未知'}；已拒绝直连或跨区注册"
        )
    return actual


def main() -> int:
    args = build_parser().parse_args()
    source_dir = Path(args.source_dir).resolve()
    if not (source_dir / "app_backend.py").is_file():
        emit("error", error=f"目标项目缺少 app_backend.py：{source_dir}")
        return 2
    sys.path.insert(0, str(source_dir))

    password = os.environ.get("HME_OPENAI_PASSWORD", "")
    ensure_password = os.environ.get("HME_ENSURE_OPENAI_PASSWORD", "") == "1"
    enable_2fa = os.environ.get("HME_ENABLE_OPENAI_2FA", "") == "1"
    cookie_refresh_only = os.environ.get("HME_COOKIE_SESSION_REFRESH", "") == "1"
    saved_storage_state = load_saved_storage_state(
        os.environ.get("HME_BROWSER_DB_FILE", ""),
        args.email,
    )
    saved_cookie_count = len(saved_storage_state.get("cookies") or [])
    if cookie_refresh_only and not saved_cookie_count:
        emit("error", error="该账号尚未保存可用 Cookie，请先重新登录或注册")
        return 2
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

        app_backend.HotmailOtpReader = ManualOtpReader
        configure_windowed_camoufox(app_backend)
        app_backend.OpenAIRegisterPayLinkWorker._force_fill_locator = (
            resilient_force_fill_locator
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
        expected_proxy_country = str(
            os.environ.get("HME_REGISTRATION_PROXY_COUNTRY") or ""
        ).strip().upper()
        if required_proxy and not proxy_url:
            raise RuntimeError("注册动态代理已设为必需，但未分配代理")
        proxy = app_backend.ProxyConfig(
            local_proxy="", dynamic_proxy=proxy_url, chain_url=""
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
        if saved_cookie_count:
            emit(
                "log",
                message=(
                    f"[Cookie] 已载入 {saved_cookie_count} 个保存 Cookie；"
                    "正在重新获取 Session 与账号状态"
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
            actual_country = require_registration_proxy_country(
                health, expected_proxy_country
            )
            emit(
                "log",
                message=(
                    f"[代理] 注册出口国家已确认：{actual_country or '未知'}；"
                    "本账号注册、2FA 与 Session 获取保持同一粘性代理"
                ),
            )
        else:
            emit("log", message="[代理] 注册动态代理未启用，使用本地直连")
            direct_location = detect_direct_registration_location(
                app_backend, log
            )
        configure_password_first_login(worker, enabled=ensure_password)
        if configure_direct_registration_browser(
            worker,
            enabled=not bool(proxy_url),
            locale=str(direct_location.get("locale") or ""),
        ):
            country = str(direct_location.get("country") or "未知")
            locale = str(direct_location.get("locale") or "GeoIP 自动")
            timezone_name = str(direct_location.get("timezone") or "自动")
            emit(
                "log",
                message=(
                    "[直连] 未配置本次注册代理；浏览器使用本机公网 IP，"
                    f"出口国家 {country}，语言 {locale}，时区 {timezone_name}"
                ),
            )
        slot_index, slot_count = _browser_window_slot_from_environment()
        if slot_count > 1 and not args.headless:
            emit(
                "log",
                message=(
                    f"[并发] 当前浏览器使用独立窗口槽位 "
                    f"{slot_index + 1}/{slot_count}"
                ),
            )
        configure_worker_login_totp(worker, pending_2fa)
        configure_registration_profile_capture(app_backend, worker)
        configure_resilient_about_you_input(worker)
        configure_resilient_registration_navigation(worker)
        worker.initial_storage_state = saved_storage_state or None
        # Session/AT are sufficient for this service. Camoufox can occasionally
        # stall indefinitely while exporting a complete browser storage snapshot;
        # the database merge keeps any previously saved snapshot intact.
        worker.skip_storage_state_capture = True
        result = worker.run()
        password_confirmed = bool(
            getattr(worker, "_password_step_submitted", False)
        )
        if ensure_password and not password_confirmed:
            emit(
                "log",
                message="OpenAI 注册成功且 Session 已保存；密码尚待单独设置",
            )
        if enable_2fa and password_confirmed:
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
        elif enable_2fa:
            emit("log", message="[2FA] 密码尚未设置成功，已跳过开启 2FA")
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
