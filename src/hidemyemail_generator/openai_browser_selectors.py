"""Selectors and browser-side scripts for OpenAI account automation."""

EVENT_PREFIX = "HME_BROWSER_EVENT:"
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
PASSWORD_RESET_CONFIRM_MARKERS = (
    "reset your password",
    "password reset",
    "重置密码",
    "重設密碼",
    "パスワードのリセット",
    "パスワードをリセット",
)
PASSWORD_RESET_CONFIRM_CONTINUE_SELECTORS = (
    'button[type="submit"]:has-text("Continue")',
    'button:has-text("Continue")',
    '[role="button"]:has-text("Continue")',
    'button[type="submit"]:has-text("続行")',
    'button:has-text("続行")',
    '[role="button"]:has-text("続行")',
    'button[type="submit"]:has-text("继续")',
    'button:has-text("继续")',
    '[role="button"]:has-text("继续")',
    'button[type="submit"]:has-text("繼續")',
    'button:has-text("繼續")',
    '[role="button"]:has-text("繼續")',
)
PASSWORD_ENTRY_STATUS_INTERVAL_SECONDS = 10.0
PASSWORD_ENTRY_SECURITY_MARKERS = (
    "security verification",
    "verify you are human",
    "verify that you are human",
    "checking your browser",
    "just a moment",
    "complete the security check",
    "captcha",
    "安全验证",
    "安全驗證",
    "验证您是真人",
    "驗證您是真人",
    "人机验证",
    "人機驗證",
    "セキュリティ確認",
    "人間であることを確認",
    "私はロボットではありません",
    "セキュリティチャレンジ",
    "cloudflare security challenge",
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
LOCALIZED_EMAIL_OTP_INPUT_SELECTORS = (
    'input[id="code"]',
    'input[id*="code" i]',
    'input[name*="code" i]',
    'input[id*="otp" i]',
    'input[name*="otp" i]',
    'input[data-testid="code-input"]',
    'input[data-testid*="verification-code" i]',
    'input[aria-labelledby*="code" i]',
    'input[aria-label*="コード" i]',
    'input[placeholder*="コード" i]',
    'input[aria-label*="認証" i]',
    'input[placeholder*="認証" i]',
    'input[aria-label*="確認" i]',
    'input[placeholder*="確認" i]',
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
