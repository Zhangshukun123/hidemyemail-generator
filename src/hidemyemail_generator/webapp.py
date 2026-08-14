import argparse
import asyncio
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import sys
import time
import webbrowser
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp
from aiohttp import web

from .account_deactivation import (
    mark_deactivation_notice_processed,
    pending_deactivation_notices,
)
from .account_verifier import AccountVerificationManager, removed_account_emails
from .browser_tasks import (
    BrowserTaskManager,
    _save_account_record,
    account_registration_proxy_url,
    account_saved_cookies,
    account_session,
    account_session_access_token,
    access_token_is_expired,
    build_registration_environment,
    load_account_record,
    set_manual_account_type,
)
from .code_portal import CODE_PORTAL_HTML
from .inbox import (
    DEFAULT_DB_FILE,
    DEFAULT_FOLDER,
    DEFAULT_INBOX_CONFIG_FILE,
    InboxConfig,
    connect_db,
    extract_verification_code,
    list_messages,
    load_config,
    mask_account,
    save_config,
    sync_inbox,
)
from .main import _generate, fetch_account_info
from .main import RichHideMyEmail
from .openai_mfa import generate_totp
from .paypal_protocol_service import PayPalProtocolService
from .protocol_registration import (
    ConcurrentProtocolRegistrationManager,
    PROTOCOL_CODE_PREFIX,
    ProtocolRegistrationManager,
)
from .registration_inventory import (
    DEFAULT_LEASE_SECONDS,
    available_generated_inventory_count,
    complete_generated_inventory_lease,
    expire_inventory_leases,
    lease_generated_inventory_email,
    registration_inventory_status as stored_registration_inventory_status,
)
from .registration_inventory_auth import (
    InventoryCredentialStore,
    access_token_digest,
)
from .registration_inventory_client import (
    INVENTORY_INTEGRATION_PATHS,
    INVENTORY_LEASE_PATH,
    INVENTORY_LOGIN_PATH,
    INVENTORY_RESULT_PATH,
    INVENTORY_STATUS_PATH,
    INVENTORY_SYNC_PATH,
    RemoteRegistrationInventoryClient,
)
from .registration_inventory_sync import (
    SYNC_SCHEMA_VERSION,
    export_inventory_record,
    export_inventory_records,
    import_inventory_records,
)
from .registration_tasks import (
    ConcurrentRegistrationTaskManager,
    RegistrationTaskManager,
    generate_openai_password,
)
from .registration_proxy import CARD_LINK_PROXY_SETTING_KEY, RegistrationProxyStore
from .roxy_registration import RoxyRegistrationStore
from .scheduled_generation import (
    DEFAULT_BATCH_SIZE as DEFAULT_INVENTORY_BATCH_SIZE,
    DEFAULT_INTERVAL_SECONDS as DEFAULT_INVENTORY_INTERVAL_SECONDS,
    ScheduledGenerationManager,
)
from .smsbower import (
    DEFAULT_SMSBOWER_MAX_PRICE,
    DEFAULT_SMSBOWER_SERVICE,
    SMSBOWER_API_BASE_URL,
    SMSBowerConfigStore,
    SMSBowerMailClient,
)
from .web_ui import build_app_page, build_login_page


SESSION_COOKIE_NAME = "hme_session"
SESSION_MAX_AGE = 12 * 60 * 60
WORKBENCH_OPENAI_CODE_PATH = "/api/integrations/workbench/openai-code"
WORKBENCH_INVENTORY_IMPORT_PATH = "/api/integrations/apple-hme/inventory"
PUBLIC_PATHS = {
    "/login",
    "/api/login",
    "/access",
    "/code",
    "/api/code/latest",
    "/healthz",
    WORKBENCH_INVENTORY_IMPORT_PATH,
    INVENTORY_LOGIN_PATH,
}
GPT_CODE_CURSOR_PREFIX = "gpt_code_cursor:"
CARD_LINK_EVENT_PREFIX = "HME_CARD_LINK_EVENT:"
ON_DEMAND_INBOX_SUCCESS_COOLDOWN_SECONDS = 15
ON_DEMAND_INBOX_FAILURE_BACKOFF_SECONDS = 60
ON_DEMAND_INBOX_AUTH_BACKOFF_SECONDS = 15 * 60
ON_DEMAND_INBOX_MAX_BACKOFF_SECONDS = 60 * 60
OPENAI_CODE_INBOX_SYNC_LIMIT = 3
OPENAI_CODE_INBOX_WAIT_SECONDS = 7.0
DEACTIVATION_SCAN_INTERVAL_SECONDS = 5 * 60
INVENTORY_SYNC_INTERVAL_SECONDS = 5 * 60
REGISTRATION_PROCESS_FAILURE_PREFIX = "registration_process_failure:"
CARD_LINK_REGIONS = {
    "US": {"label": "美国", "currency": "USD", "locale": "en-US"},
    "JP": {"label": "日本", "currency": "JPY", "locale": "ja-JP"},
    "DE": {"label": "德国", "currency": "EUR", "locale": "de-DE"},
    "GB": {"label": "英国", "currency": "GBP", "locale": "en-GB"},
    "CA": {"label": "加拿大", "currency": "CAD", "locale": "en-CA"},
    "AU": {"label": "澳大利亚", "currency": "AUD", "locale": "en-AU"},
}
CARD_LINK_METHODS = {"standard", "ph_hosted", "de_oaics_paypal"}


class InboxSyncDeferredError(RuntimeError):
    """Reuse a recent IMAP failure without opening another connection."""

    def __init__(self, public_message: str):
        super().__init__(public_message)
        self.public_message = public_message


async def _wait_for_shared_inbox_code_sync(
    state: dict,
    sync_factory,
    *,
    wait_seconds: float = OPENAI_CODE_INBOX_WAIT_SECONDS,
) -> bool:
    """Reuse one shielded IMAP task across short-lived HTTP code polls."""

    task = state.get("inbox_code_sync_task")
    if task is None:
        task = asyncio.create_task(sync_factory())
        state["inbox_code_sync_task"] = task
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=max(0.01, float(wait_seconds)),
        )
    except asyncio.TimeoutError:
        # The browser worker uses a short HTTP timeout. Keep the IMAP task alive
        # so the next poll can read the message instead of opening another scan.
        return False
    finally:
        if task.done():
            state["inbox_code_sync_task"] = None
    return True

PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline' https://cdn.jsdelivr.net; "
        "script-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; frame-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

OPENAI_RUNTIME_SIBLING_NAMES = (
    "openai-register-paylink",
    "openai-register-paylink-ui-dist-20260706-README-deploy",
)
PAYPAL_PROTOCOL_PREFIX = "/paypal-pay"


def _default_paypal_runtime_dir() -> Path:
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app_data:
        return Path(local_app_data) / "HideMyEmailGenerator" / "paypal-protocol"
    return Path.home() / ".local" / "share" / "HideMyEmailGenerator" / "paypal-protocol"


def _default_openai_runtime_dir(base_dir: Path) -> Path:
    """Find a usable sibling checkout while keeping the new name canonical."""

    parent = base_dir.resolve().parent
    candidates = [parent / name for name in OPENAI_RUNTIME_SIBLING_NAMES]
    for candidate in candidates:
        if (candidate / "app_backend.py").is_file():
            return candidate
    return candidates[0]


def _configure_utf8_stdio() -> None:
    """Keep Rich and browser-worker logs Unicode-safe on Windows."""

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass


LOGIN_HTML = r"""<!doctype html>
<html lang="zh-CN" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="theme-color" content="#0f0f10">
  <title>登录 · iCloud 隐藏邮箱</title>
  <script>
    (() => {
      try {
        const saved = localStorage.getItem("hme_theme");
        const theme = saved === "light" || saved === "dark"
          ? saved
          : matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
        document.documentElement.dataset.theme = theme;
        document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#0f0f10" : "#f7f7f5";
      } catch (_) {}
    })();
  </script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.min.css" integrity="sha384-L1dWfspMTHU/ApYnFiMz2QID/PlP1xCW9visvBdbEkOLkSSWsP6ZJWhPw6apiXxU" crossorigin="anonymous">
  <style>
    :root, html[data-theme="dark"] {
      color-scheme: dark;
      font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
      --pico-font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
      --pico-primary: #f0f0f0;
      --pico-primary-background: #ececec;
      --pico-primary-hover-background: #ffffff;
      --pico-border-radius: 14px;
      --canvas: #0f0f10;
      --surface: rgba(25, 25, 26, .96);
      --surface-border: rgba(255, 255, 255, .1);
      --text: #f3f3f4;
      --muted: #9b9b9f;
      --label: #c4c4c7;
      --input: #171718;
      --input-border: #39393b;
      --input-focus: #77777b;
      --primary: #ececec;
      --primary-hover: #ffffff;
      --primary-text: #151515;
      --subtle: rgba(255, 255, 255, .035);
      --grid: rgba(255, 255, 255, .026);
      --brand: linear-gradient(145deg, #39393c, #19191a);
      --brand-border: rgba(255, 255, 255, .13);
      --brand-copy: #a5a5a8;
      --danger: #e67584;
      --shadow: 0 32px 90px rgba(0, 0, 0, .44), inset 0 1px 0 rgba(255, 255, 255, .025);
    }
    html[data-theme="light"] {
      color-scheme: light;
      --pico-primary: #202021;
      --pico-primary-background: #202021;
      --pico-primary-hover-background: #0d0d0e;
      --canvas: #f7f7f5;
      --surface: rgba(255, 255, 255, .97);
      --surface-border: rgba(0, 0, 0, .1);
      --text: #222223;
      --muted: #6b6b6e;
      --label: #4d4d50;
      --input: #ffffff;
      --input-border: #d4d4d1;
      --input-focus: #77777a;
      --primary: #202021;
      --primary-hover: #0d0d0e;
      --primary-text: #ffffff;
      --subtle: rgba(0, 0, 0, .025);
      --grid: rgba(0, 0, 0, .025);
      --brand: linear-gradient(145deg, #363638, #171718);
      --brand-border: rgba(0, 0, 0, .08);
      --brand-copy: #6d6d70;
      --danger: #b84354;
      --shadow: 0 28px 70px rgba(0, 0, 0, .11), inset 0 1px 0 rgba(255, 255, 255, .7);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; display: grid; place-items: center; overflow: hidden; color: var(--text);
      background: radial-gradient(circle at 12% 0%, var(--subtle), transparent 34rem), var(--canvas);
      transition: color .2s ease, background-color .2s ease;
    }
    body::before { content: ""; position: fixed; inset: 0; pointer-events: none; opacity: .18;
      background-image: linear-gradient(var(--grid) 1px, transparent 1px),
                        linear-gradient(90deg, var(--grid) 1px, transparent 1px);
      background-size: 32px 32px; mask-image: linear-gradient(to bottom, black, transparent 75%); }
    .login-shell { width: min(440px, calc(100% - 32px)); position: relative; z-index: 1; }
    .login {
      position: relative; margin: 0; padding: clamp(26px, 6vw, 38px); border-radius: 26px;
      background: var(--surface); border: 1px solid var(--surface-border);
      box-shadow: var(--shadow); backdrop-filter: blur(22px);
    }
    .brand { display: flex; align-items: center; gap: 13px; margin-bottom: 28px; }
    .icon { width: 48px; height: 48px; display: grid; place-items: center; border-radius: 15px;
      color: #fff; background: var(--brand); border: 1px solid var(--brand-border);
      box-shadow: 0 10px 24px rgba(0,0,0,.18), inset 0 1px 0 rgba(255,255,255,.1); }
    .icon svg { width: 23px; height: 23px; }
    .brand-copy { color: var(--brand-copy); font-size: 12px; letter-spacing: .14em; text-transform: uppercase; }
    h1 { margin: 3px 0 0; font-size: 28px; letter-spacing: -.035em; color: var(--text); }
    .intro { margin: 0 0 26px; color: var(--muted); line-height: 1.65; font-size: 14px; }
    label { color: var(--label); font-size: 13px; font-weight: 650; }
    input { margin-top: 8px; border-color: var(--input-border); background: var(--input); color: var(--text); }
    input:focus { border-color: var(--input-focus); box-shadow: 0 0 0 3px color-mix(in srgb, var(--input-focus) 18%, transparent); }
    button { width: 100%; margin: 10px 0 0; border: 0; padding: 13px 15px;
      color: var(--primary-text); background: var(--primary); font-weight: 750;
      box-shadow: 0 8px 20px rgba(0,0,0,.14); }
    button[type="button"] { margin-block: 0; }
    #submit { color: var(--primary-text); background: var(--primary); }
    #submit:hover:not(:disabled) { background: var(--primary-hover); }
    button:hover:not(:disabled) { background: var(--primary-hover); }
    button:disabled { opacity: .55; cursor: wait; }
    #notice { min-height: 21px; margin-top: 12px; color: var(--danger); font-size: 13px; }
    .safe { display: flex; align-items: center; justify-content: center; gap: 7px; margin-top: 19px;
      color: var(--muted); font-size: 12px; }
    .safe svg { width: 14px; height: 14px; }
    .theme-toggle { position: absolute; top: 20px; right: 20px; width: 38px; height: 38px; min-height: 38px; margin: 0;
      display: grid; place-items: center; padding: 0; color: var(--muted); background: transparent; border: 1px solid var(--surface-border); box-shadow: none; }
    .theme-toggle:hover:not(:disabled) { color: var(--text); background: var(--subtle); }
    .theme-toggle svg { width: 17px; height: 17px; }
    .theme-icon-sun, .theme-icon-moon { display: none; }
    html[data-theme="dark"] .theme-icon-sun { display: block; }
    html[data-theme="light"] .theme-icon-moon { display: block; }
  </style>
</head>
<body>
  <main class="login-shell">
    <form class="login" id="loginForm">
      <button id="themeToggle" class="theme-toggle" type="button" aria-label="切换主题" title="切换主题">
        <svg class="theme-icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.65 17.65l1.42 1.42M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.65 6.35l1.42-1.42"></path></svg>
        <svg class="theme-icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M20.5 15.5A8.5 8.5 0 0 1 8.5 3.5 8.5 8.5 0 1 0 20.5 15.5Z"></path></svg>
      </button>
      <div class="brand">
        <div class="icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="14" rx="3"></rect><path d="m4 7 8 6 8-6"></path></svg></div>
        <div><div class="brand-copy">Private Relay Console</div><h1>隐藏邮箱控制台</h1></div>
      </div>
      <p class="intro">登录后管理 iCloud 隐藏邮箱，并安全获取 OpenAI 会话凭据。</p>
      <label for="password">管理密码
        <input id="password" type="password" autocomplete="current-password" placeholder="请输入管理密码" autofocus required>
      </label>
      <button id="submit" type="submit">安全登录</button>
      <div id="notice" role="alert" aria-live="polite"></div>
      <div class="safe"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="5" y="10" width="14" height="10" rx="2"></rect><path d="M8 10V7a4 4 0 0 1 8 0v3"></path></svg>通过 SSH 安全隧道访问</div>
    </form>
  </main>
  <script>
    const form = document.getElementById("loginForm");
    const submit = document.getElementById("submit");
    const notice = document.getElementById("notice");
    const themeToggle = document.getElementById("themeToggle");

    function applyTheme(theme, persist = false) {
      document.documentElement.dataset.theme = theme;
      document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#0f0f10" : "#f7f7f5";
      themeToggle.setAttribute("aria-label", theme === "dark" ? "切换至白天模式" : "切换至夜间模式");
      themeToggle.title = themeToggle.getAttribute("aria-label");
      if (persist) {
        try { localStorage.setItem("hme_theme", theme); } catch (_) {}
      }
    }

    applyTheme(document.documentElement.dataset.theme || "dark");
    themeToggle.addEventListener("click", () => {
      applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      notice.textContent = "正在验证…";
      try {
        const response = await fetch("/api/login", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: document.getElementById("password").value }),
          cache: "no-store"
        });
        const data = await response.json().catch(() => ({ error: "服务响应无效" }));
        if (!response.ok || !data.ok) throw new Error(data.error || "登录失败");
        location.replace("/");
      } catch (error) {
        notice.textContent = error.message;
        document.getElementById("password").select();
      } finally {
        submit.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>iCloud 隐藏邮箱</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
      background: #07111f;
      color: #eaf2ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at 12% 0%, rgba(20, 132, 255, .22), transparent 32rem),
        radial-gradient(circle at 92% 18%, rgba(65, 214, 180, .12), transparent 26rem),
        #07111f;
    }
    main { width: min(1080px, calc(100% - 32px)); margin: 0 auto; padding: 42px 0 60px; }
    header { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 24px; }
    h1 { margin: 0; font-size: clamp(28px, 4vw, 44px); letter-spacing: -.04em; }
    .subtitle { color: #8fa7c6; margin-top: 8px; }
    .status {
      display: inline-flex; align-items: center; gap: 9px; padding: 9px 13px;
      border: 1px solid #243955; border-radius: 999px; background: rgba(9, 24, 42, .86);
      color: #a9bad0; white-space: nowrap;
    }
    .header-actions { display: flex; align-items: center; gap: 10px; }
    .logout { padding: 9px 13px; color: #a9bad0; background: rgba(9, 24, 42, .86); border: 1px solid #243955; }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: #77869a; box-shadow: 0 0 0 4px rgba(119,134,154,.12); }
    .dot.ok { background: #38d89f; box-shadow: 0 0 0 4px rgba(56,216,159,.13); }
    .dot.bad { background: #ff6b73; box-shadow: 0 0 0 4px rgba(255,107,115,.13); }
    .grid { display: grid; grid-template-columns: 360px 1fr; gap: 18px; align-items: start; }
    .card {
      background: rgba(10, 25, 44, .88); border: 1px solid rgba(80, 116, 158, .27);
      border-radius: 20px; box-shadow: 0 22px 70px rgba(0, 0, 0, .28); overflow: hidden;
    }
    .card-body { padding: 22px; }
    h2 { margin: 0 0 18px; font-size: 19px; }
    label { display: block; color: #9bb0ca; font-size: 13px; margin-bottom: 7px; }
    input, select {
      width: 100%; border: 1px solid #29415f; background: #071523; color: #eef6ff;
      border-radius: 12px; padding: 12px 13px; font: inherit; outline: none;
    }
    input:focus, select:focus { border-color: #2799ff; box-shadow: 0 0 0 3px rgba(39,153,255,.13); }
    .field { margin-bottom: 15px; }
    .row { display: grid; grid-template-columns: 1fr 110px; gap: 12px; }
    button {
      border: 0; border-radius: 12px; padding: 12px 15px; font: inherit; font-weight: 700;
      cursor: pointer; transition: transform .14s ease, opacity .14s ease, background .14s ease;
    }
    button:hover:not(:disabled) { transform: translateY(-1px); }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .primary { width: 100%; color: white; background: linear-gradient(135deg, #1688ff, #2e6df6); }
    .secondary { color: #c6d8ef; background: #162a43; }
    .button-row { display: flex; gap: 10px; flex-wrap: wrap; }
    .button-row button { flex: 1; min-width: 150px; }
    .notice { min-height: 22px; margin-top: 14px; color: #91a8c4; font-size: 13px; line-height: 1.5; }
    .notice.good { color: #56dda9; }
    .notice.error { color: #ff8990; }
    .results { margin-top: 18px; display: grid; gap: 9px; }
    .result-item, .email-row {
      display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 14px;
      padding: 13px 15px; background: rgba(7, 19, 33, .72); border: 1px solid #203653; border-radius: 13px;
    }
    .address { color: #e6f2ff; overflow-wrap: anywhere; }
    .meta { color: #7f96b3; font-size: 12px; margin-top: 5px; }
    .copy { padding: 8px 11px; color: #83c6ff; background: rgba(25, 125, 213, .14); }
    .list-head { display: flex; justify-content: space-between; align-items: center; padding: 20px 22px 14px; }
    .list-head h2 { margin: 0; }
    .list { padding: 0 14px 14px; display: grid; gap: 8px; max-height: 680px; overflow: auto; }
    .empty { padding: 42px 18px; text-align: center; color: #728aa8; }
    .pill { display: inline-block; margin-left: 8px; padding: 2px 7px; border-radius: 999px; font-size: 11px; background: #153b34; color: #6de2b5; }
    .inbox-card { display: none !important; }
    .inbox-grid { display: grid; grid-template-columns: 360px 1fr; gap: 18px; padding: 0 22px 22px; }
    .config-grid { display: grid; grid-template-columns: 1fr 96px; gap: 12px; }
    .code-list { display: grid; gap: 9px; max-height: 460px; overflow: auto; }
    .code-row { display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: center; padding: 14px 15px; background: rgba(7, 19, 33, .72); border: 1px solid #203653; border-radius: 13px; }
    .code { color: #69ddae; font-size: 24px; font-weight: 800; letter-spacing: .08em; }
    .inbox-state { color: #8fa7c6; font-size: 13px; }
    .hint { color: #718aa8; font-size: 12px; line-height: 1.55; }
    footer { margin-top: 18px; color: #617a99; font-size: 12px; text-align: center; }
    @media (max-width: 790px) {
      header { align-items: flex-start; flex-direction: column; }
      .grid { grid-template-columns: 1fr; }
      .inbox-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>iCloud 隐藏邮箱</h1>
        <div class="subtitle">本地生成与管理 · 中国区</div>
      </div>
      <div class="header-actions">
        <div class="status"><span id="statusDot" class="dot"></span><span id="statusText">正在检查 Cookie…</span></div>
        <button id="logout" class="logout">退出</button>
      </div>
    </header>

    <div class="grid">
      <section class="card">
        <div class="card-body">
          <h2>生成新地址</h2>
          <div class="field">
            <label for="label">标签</label>
            <input id="label" maxlength="100" autocomplete="off" placeholder="例如：注册001">
          </div>
          <div class="field row">
            <div>
              <label for="count">数量（1–10）</label>
              <input id="count" type="number" min="1" max="10" value="1">
            </div>
            <div></div>
          </div>
          <button id="generate" class="primary" disabled>生成并保留</button>
          <div id="notice" class="notice">Cookie 仅保存在本机，不会返回到页面。</div>
          <div id="results" class="results"></div>
        </div>
      </section>

      <section class="card">
        <div class="list-head">
          <h2>使用中的地址</h2>
          <button id="refresh" class="secondary">刷新</button>
        </div>
        <div id="list" class="list"><div class="empty">正在加载…</div></div>
      </section>
    </div>

    <!-- 收件箱与验证码由服务器后台自动同步，不在管理界面展示。 -->
    <footer>服务仅监听 127.0.0.1；请勿分享 cookies.txt、邮箱授权码或本地数据库。</footer>
  </main>

  <script>
    const localToken = __LOCAL_TOKEN__;
    const $ = (id) => document.getElementById(id);
    const state = { ready: false, busy: false };

    function defaultLabel() {
      const d = new Date();
      const p = (n) => String(n).padStart(2, "0");
      return `网页-${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
    }

    async function api(path, options = {}) {
      const headers = { ...(options.headers || {}) };
      if (options.method && options.method !== "GET") headers["X-Local-Token"] = localToken;
      if (options.body) headers["Content-Type"] = "application/json";
      let response;
      try {
        response = await fetch(path, { ...options, headers, cache: "no-store" });
      } catch (_) {
        throw new Error("无法连接本地服务，请刷新页面后重试");
      }
      const data = await response.json().catch(() => ({ ok: false, error: "服务返回了无效响应" }));
      if (response.status === 401) {
        location.replace("/login");
        throw new Error("登录已过期");
      }
      if (response.status === 403 && data.error === "本地请求令牌无效") {
        setTimeout(() => location.reload(), 100);
        throw new Error("本地服务已重启，正在刷新页面");
      }
      if (!response.ok || data.ok === false) throw new Error(data.error || `请求失败 (${response.status})`);
      return data;
    }

    function setStatus(ok, text) {
      $("statusDot").className = `dot ${ok ? "ok" : "bad"}`;
      $("statusText").textContent = text;
      state.ready = ok;
      $("generate").disabled = !ok || state.busy;
    }

    function setNotice(text, type = "") {
      $("notice").className = `notice ${type}`;
      $("notice").textContent = text;
    }

    function makeCopyButton(email) {
      const button = document.createElement("button");
      button.className = "copy";
      button.textContent = "复制";
      button.addEventListener("click", async () => {
        await navigator.clipboard.writeText(email);
        button.textContent = "已复制";
        setTimeout(() => { button.textContent = "复制"; }, 1200);
      });
      return button;
    }

    function setInboxNotice(text, type = "") {
      $("inboxNotice").className = `notice ${type}`;
      $("inboxNotice").textContent = text;
    }

    function renderCodes(items) {
      const root = $("codeList");
      root.replaceChildren();
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "暂未提取到验证码，请先让网站发送验证码后再同步。";
        root.append(empty);
        return;
      }
      for (const item of items) {
        const row = document.createElement("div");
        row.className = "code-row";
        const info = document.createElement("div");
        const code = document.createElement("div");
        code.className = "code";
        code.textContent = item.code;
        const address = document.createElement("div");
        address.className = "address";
        address.textContent = item.hmeAddress || "未匹配隐藏地址";
        const meta = document.createElement("div");
        meta.className = "meta";
        const received = item.receivedAt ? new Date(item.receivedAt).toLocaleString() : "时间未知";
        meta.textContent = `${item.subject || "无主题"} · ${received}`;
        info.append(code, address, meta);
        row.append(info, makeCopyButton(item.code));
        root.append(row);
      }
    }

    function renderGenerated(emails) {
      const root = $("results");
      root.replaceChildren();
      for (const email of emails) {
        const row = document.createElement("div");
        row.className = "result-item";
        const address = document.createElement("div");
        address.className = "address";
        address.textContent = email;
        row.append(address, makeCopyButton(email));
        root.append(row);
      }
    }

    function renderList(items) {
      const root = $("list");
      root.replaceChildren();
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "暂无使用中的地址";
        root.append(empty);
        return;
      }
      for (const item of items) {
        const row = document.createElement("div");
        row.className = "email-row";
        const info = document.createElement("div");
        const address = document.createElement("div");
        address.className = "address";
        address.textContent = item.email;
        const pill = document.createElement("span");
        pill.className = "pill";
        pill.textContent = "使用中";
        address.append(pill);
        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent = `${item.label || "无标签"}${item.createdAt ? ` · ${new Date(item.createdAt).toLocaleString()}` : ""}`;
        info.append(address, meta);
        row.append(info, makeCopyButton(item.email));
        root.append(row);
      }
    }

    async function loadStatus() {
      try {
        const data = await api("/api/status");
        setStatus(data.authenticated, data.authenticated ? "Cookie 有效" : "Cookie 无效");
        if (!data.authenticated) setNotice(data.error || "请重新获取 Cookie", "error");
      } catch (error) {
        setStatus(false, "服务异常");
        setNotice(error.message, "error");
      }
    }

    async function loadEmails() {
      $("refresh").disabled = true;
      try {
        const data = await api("/api/emails");
        renderList(data.items);
      } catch (error) {
        renderList([]);
        setNotice(error.message, "error");
      } finally {
        $("refresh").disabled = false;
      }
    }

    async function generate() {
      const label = $("label").value.trim();
      const count = Number($("count").value);
      if (!label) return setNotice("请输入标签。", "error");
      if (!Number.isInteger(count) || count < 1 || count > 10) return setNotice("数量必须是 1–10 的整数。", "error");
      state.busy = true;
      $("generate").disabled = true;
      setNotice(`正在生成 ${count} 个地址，请不要重复点击…`);
      try {
        const data = await api("/api/generate", { method: "POST", body: JSON.stringify({ label, count }) });
        renderGenerated(data.emails);
        setNotice(`成功生成 ${data.emails.length} 个地址。`, "good");
        await loadEmails();
      } catch (error) {
        setNotice(error.message, "error");
      } finally {
        state.busy = false;
        $("generate").disabled = !state.ready;
      }
    }

    async function loadInboxStatus() {
      try {
        const data = await api("/api/inbox/status");
        state.inboxConfigured = data.configured;
        $("syncInbox").disabled = !data.configured || state.inboxBusy;
        $("inboxState").textContent = data.configured
          ? `${data.account} · ${data.host}:${data.port} · 已保存 ${data.codeCount} 个验证码`
          : "尚未配置接收邮箱";
        if (data.configured) {
          $("imapHost").value = data.host;
          $("imapPort").value = data.port;
          $("imapUsername").value = data.username;
          $("imapFolder").value = data.folder;
          const known = [...$("provider").options].some((option) => option.value === data.host);
          $("provider").value = known ? data.host : "custom";
        }
      } catch (error) {
        $("inboxState").textContent = "收件箱状态读取失败";
        setInboxNotice(error.message, "error");
      }
    }

    async function saveInboxConfig() {
      const payload = {
        host: $("imapHost").value.trim(),
        port: Number($("imapPort").value),
        username: $("imapUsername").value.trim(),
        password: $("imapPassword").value,
        folder: $("imapFolder").value.trim() || "INBOX",
        useSsl: true,
      };
      if (!payload.host || !payload.username) return setInboxNotice("请填写 IMAP 主机和邮箱账号。", "error");
      if (!Number.isInteger(payload.port) || payload.port < 1 || payload.port > 65535) return setInboxNotice("IMAP 端口无效。", "error");
      $("saveInbox").disabled = true;
      setInboxNotice("正在保存并测试 IMAP 登录…");
      try {
        const data = await api("/api/inbox/config", { method: "POST", body: JSON.stringify(payload) });
        $("imapPassword").value = "";
        setInboxNotice(data.message, "good");
        await loadInboxStatus();
      } catch (error) {
        setInboxNotice(error.message, "error");
      } finally {
        $("saveInbox").disabled = false;
      }
    }

    async function loadCodes() {
      try {
        const data = await api("/api/inbox/codes?limit=30");
        renderCodes(data.items);
      } catch (error) {
        setInboxNotice(error.message, "error");
      }
    }

    async function syncInbox() {
      state.inboxBusy = true;
      $("syncInbox").disabled = true;
      setInboxNotice("正在连接邮箱并同步最近邮件…");
      try {
        const data = await api("/api/inbox/sync", { method: "POST", body: JSON.stringify({ limit: 100 }) });
        renderCodes(data.items);
        setInboxNotice(`同步完成：新增 ${data.inserted} 封邮件，当前显示 ${data.items.length} 条验证码。`, "good");
        await loadInboxStatus();
      } catch (error) {
        setInboxNotice(error.message, "error");
      } finally {
        state.inboxBusy = false;
        $("syncInbox").disabled = !state.inboxConfigured;
      }
    }

    async function logout() {
      try {
        await api("/api/logout", { method: "POST" });
      } finally {
        location.replace("/login");
      }
    }

    $("label").value = defaultLabel();
    $("generate").addEventListener("click", generate);
    $("refresh").addEventListener("click", loadEmails);
    $("logout").addEventListener("click", logout);
    Promise.all([loadStatus(), loadEmails()]);
  </script>
</body>
</html>
"""


GPT_INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="theme-color" content="#0f0f10">
  <title>隐藏邮箱控制台</title>
  <script>
    (() => {
      try {
        const saved = localStorage.getItem("hme_theme");
        const theme = saved === "light" || saved === "dark"
          ? saved
          : matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
        document.documentElement.dataset.theme = theme;
        document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#0f0f10" : "#f7f7f5";
      } catch (_) {}
    })();
  </script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.min.css" integrity="sha384-L1dWfspMTHU/ApYnFiMz2QID/PlP1xCW9visvBdbEkOLkSSWsP6ZJWhPw6apiXxU" crossorigin="anonymous">
  <style>
    :root, html[data-theme="dark"] {
      color-scheme: dark;
      font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
      --pico-font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
      --pico-font-size: 100%;
      --pico-primary: #f0f0f0;
      --pico-primary-background: #ececec;
      --pico-primary-hover-background: #ffffff;
      --pico-primary-focus: rgba(255, 255, 255, .16);
      --pico-border-radius: 13px;
      --canvas: #0f0f10;
      --surface: #19191a;
      --surface-strong: #1f1f20;
      --surface-soft: #242426;
      --border: rgba(255, 255, 255, .1);
      --text: #f3f3f4;
      --muted: #9b9b9f;
      --label: #c2c2c5;
      --success: #55b992;
      --warning: #d2a14e;
      --danger: #e67584;
      --accent: #eeeeef;
      --accent-contrast: #151515;
      --page-glow: rgba(255, 255, 255, .04);
      --grid: rgba(255, 255, 255, .024);
      --subtle: rgba(255, 255, 255, .045);
      --subtle-hover: rgba(255, 255, 255, .072);
      --input: #171718;
      --input-border: #38383a;
      --button: #2b2b2d;
      --button-text: #ececee;
      --primary: #ececec;
      --primary-hover: #ffffff;
      --primary-text: #151515;
      --stat-bg: #1a1a1b;
      --row-bg: #151516;
      --row-hover: #202022;
      --success-soft: rgba(85, 185, 146, .1);
      --warning-soft: rgba(210, 161, 78, .1);
      --danger-soft: rgba(230, 117, 132, .1);
      --accent-soft: rgba(255, 255, 255, .07);
      --brand: linear-gradient(145deg, #39393c, #19191a);
      --brand-border: rgba(255, 255, 255, .13);
      --brand-copy: #a5a5a8;
      --card-shadow: 0 12px 34px rgba(0, 0, 0, .22), inset 0 1px 0 rgba(255, 255, 255, .025);
      --toast: rgba(31, 31, 33, .98);
    }
    html[data-theme="light"] {
      color-scheme: light;
      --pico-primary: #202021;
      --pico-primary-background: #202021;
      --pico-primary-hover-background: #0d0d0e;
      --pico-primary-focus: rgba(32, 32, 33, .16);
      --canvas: #f7f7f5;
      --surface: #ffffff;
      --surface-strong: #ffffff;
      --surface-soft: #f2f2f0;
      --border: rgba(0, 0, 0, .1);
      --text: #222223;
      --muted: #6b6b6e;
      --label: #4e4e51;
      --success: #147a5b;
      --warning: #946415;
      --danger: #b84354;
      --accent: #202021;
      --accent-contrast: #ffffff;
      --page-glow: rgba(0, 0, 0, .025);
      --grid: rgba(0, 0, 0, .025);
      --subtle: rgba(0, 0, 0, .035);
      --subtle-hover: rgba(0, 0, 0, .06);
      --input: #ffffff;
      --input-border: #d5d5d2;
      --button: #e9e9e7;
      --button-text: #303033;
      --primary: #202021;
      --primary-hover: #0d0d0e;
      --primary-text: #ffffff;
      --stat-bg: #ffffff;
      --row-bg: #fafaf8;
      --row-hover: #f1f1ef;
      --success-soft: rgba(20, 122, 91, .09);
      --warning-soft: rgba(148, 100, 21, .09);
      --danger-soft: rgba(184, 67, 84, .09);
      --accent-soft: rgba(0, 0, 0, .055);
      --brand: linear-gradient(145deg, #363638, #171718);
      --brand-border: rgba(0, 0, 0, .08);
      --brand-copy: #6d6d70;
      --card-shadow: 0 10px 28px rgba(0, 0, 0, .075), inset 0 1px 0 rgba(255, 255, 255, .7);
      --toast: rgba(255, 255, 255, .98);
    }
    * { box-sizing: border-box; }
    html { background: var(--canvas); scrollbar-gutter: stable; }
    body {
      margin: 0; min-height: 100vh; color: var(--text); overscroll-behavior-y: none;
      background: radial-gradient(circle at 7% 0%, var(--page-glow), transparent 29rem), var(--canvas);
      transition: color .2s ease, background-color .2s ease;
    }
    .app-shell {
      width: min(1480px, calc(100% - 40px)); margin: 0 auto; position: relative; z-index: 1;
      display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 20px; align-items: start;
    }
    .side-nav {
      position: sticky; top: 20px; min-height: calc(100vh - 40px); padding: 17px 14px;
      display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: 20px;
      background: var(--surface); box-shadow: var(--card-shadow);
    }
    .side-brand { display: flex; align-items: center; gap: 11px; padding: 4px 5px 20px; border-bottom: 1px solid var(--border); }
    .side-brand .brand-mark { width: 40px; height: 40px; border-radius: 13px; }
    .side-brand .brand-mark svg { width: 19px; height: 19px; }
    .side-brand-title { color: var(--text); font-size: 13px; font-weight: 780; }
    .side-brand-copy { margin-top: 2px; color: var(--muted); font-size: 9px; letter-spacing: .11em; text-transform: uppercase; }
    .side-nav-label { margin: 19px 9px 8px; color: var(--muted); font-size: 9px; font-weight: 750; letter-spacing: .13em; text-transform: uppercase; }
    .side-nav-list { display: grid; gap: 7px; }
    .nav-item {
      width: 100%; min-height: 44px; padding: 9px 11px; justify-content: flex-start; gap: 10px;
      border-color: transparent; border-radius: 12px; color: var(--muted); background: transparent;
    }
    .nav-item:hover:not(:disabled) { color: var(--text); background: var(--subtle-hover); transform: none; }
    .nav-item.active { color: var(--accent-contrast); background: var(--accent); }
    .nav-item svg { width: 17px; height: 17px; flex: 0 0 auto; }
    .nav-item-label { text-align: left; }
    .nav-badge { min-width: 22px; margin-left: auto; padding: 2px 6px; border-radius: 999px; color: inherit; background: color-mix(in srgb, currentColor 11%, transparent); font-size: 9px; text-align: center; }
    .nav-item.active .nav-badge { background: color-mix(in srgb, var(--accent-contrast) 12%, transparent); }
    .side-nav-note { margin-top: auto; padding: 15px 9px 3px; color: var(--muted); font-size: 10px; line-height: 1.55; }
    .side-nav-note strong { display: block; margin-bottom: 4px; color: var(--label); font-size: 10px; }
    main.app-main { min-width: 0; padding: 34px 0 64px; }
    .view-panel[hidden] { display: none; }
    .app-header { display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 26px; }
    .brand { display: flex; align-items: center; gap: 15px; }
    .brand-mark { width: 48px; height: 48px; display: grid; place-items: center; flex: 0 0 auto; border-radius: 15px;
      color: #fff; background: var(--brand); border: 1px solid var(--brand-border);
      box-shadow: 0 10px 24px rgba(0,0,0,.18), inset 0 1px 0 rgba(255,255,255,.1); }
    .brand-mark svg { width: 23px; height: 23px; }
    .eyebrow { color: var(--brand-copy); font-size: 11px; font-weight: 750; letter-spacing: .15em; text-transform: uppercase; }
    h1 { margin: 3px 0 0; font-size: clamp(25px, 3vw, 34px); letter-spacing: -.04em; color: var(--text); }
    .subtitle { margin-top: 5px; color: var(--muted); font-size: 13px; }
    .header-actions { display: flex; align-items: center; gap: 10px; }
    .runtime-pill { display: flex; align-items: center; gap: 8px; height: 40px; padding: 0 13px; border: 1px solid var(--border);
      border-radius: 999px; color: var(--muted); background: var(--subtle); font-size: 12px; white-space: nowrap; }
    .runtime-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); box-shadow: 0 0 0 4px var(--subtle); }
    .runtime-dot.ok { background: var(--success); box-shadow: 0 0 0 4px var(--success-soft); }
    .runtime-dot.bad { background: var(--danger); box-shadow: 0 0 0 4px var(--danger-soft); }
    button {
      width: auto; min-height: 40px; margin: 0; border: 1px solid transparent; padding: 9px 14px; display: inline-flex;
      align-items: center; justify-content: center; line-height: 1.2; font-size: 13px;
      font-weight: 720; cursor: pointer; color: var(--button-text); background: var(--button); box-shadow: none;
      transition: border-color .16s ease, background-color .16s ease, opacity .16s ease;
    }
    button[type="button"], button[type="submit"], button[type="reset"] { margin-block: 0; }
    button:disabled { opacity: .48; cursor: wait; }
    .icon-button { width: 40px; padding: 0; display: grid; place-items: center; border-color: var(--border); background: var(--subtle); }
    .icon-button:hover:not(:disabled) { background: var(--subtle-hover); }
    .icon-button svg { width: 17px; height: 17px; }
    .theme-icon-sun, .theme-icon-moon { display: none; }
    html[data-theme="dark"] .theme-icon-sun { display: block; }
    html[data-theme="light"] .theme-icon-moon { display: block; }
    .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
    .stat-card { margin: 0; padding: 17px 18px; display: flex; align-items: center; gap: 14px; border: 1px solid var(--border);
      border-radius: 17px; background: var(--stat-bg); box-shadow: 0 7px 20px rgba(0,0,0,.07), inset 0 1px 0 var(--subtle); }
    .stat-icon { width: 38px; height: 38px; display: grid; place-items: center; border-radius: 12px; color: var(--text); background: var(--accent-soft); }
    .stat-icon.success { color: var(--success); background: var(--success-soft); }
    .stat-icon.warning { color: var(--warning); background: var(--warning-soft); }
    .stat-icon.plus { color: var(--warning); background: var(--warning-soft); }
    .stat-icon svg { width: 18px; height: 18px; }
    .stat-value { font-size: 23px; line-height: 1; font-weight: 780; color: var(--text); font-variant-numeric: tabular-nums; }
    .stat-label { margin-top: 5px; color: var(--muted); font-size: 11px; }
    .card {
      margin: 0; padding: 0; background: var(--surface); border: 1px solid var(--border);
      border-radius: 20px; box-shadow: var(--card-shadow); overflow: hidden;
    }
    .card + .card { margin-top: 16px; }
    .section-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; padding: 21px 22px; border-bottom: 1px solid var(--border); }
    .section-title { display: flex; gap: 12px; }
    .section-glyph { width: 36px; height: 36px; display: grid; place-items: center; flex: 0 0 auto; border-radius: 11px;
      color: var(--text); background: var(--accent-soft); }
    .section-glyph svg { width: 18px; height: 18px; }
    .section-head h2 { margin: 0; font-size: 17px; color: var(--text); }
    .section-copy { color: var(--muted); font-size: 12px; line-height: 1.55; margin-top: 5px; max-width: 760px; }
    .automation-body { padding: 18px 22px 21px; }
    .controls { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }
    .control-field { margin: 0; color: var(--label); font-size: 11px; font-weight: 650; }
    .control-field input[type="number"] { width: 76px; height: 40px; margin: 5px 0 0; border-color: var(--input-border); background: var(--input); color: var(--text); padding: 8px 10px; }
    .switch-field { min-height: 40px; display: flex; align-items: center; gap: 8px; margin: 0 4px 0 0; color: var(--label); font-size: 12px; }
    input[type="checkbox"] { accent-color: var(--accent); }
    .primary { border-color: transparent; color: var(--primary-text); background: var(--primary); box-shadow: 0 5px 14px rgba(0,0,0,.12); }
    .primary:hover:not(:disabled) { background: var(--primary-hover); }
    .danger { border-color: var(--danger-soft); color: var(--danger); background: var(--danger-soft); }
    .task { margin-top: 16px; padding: 14px 15px; border: 1px solid var(--border); border-radius: 14px; background: var(--row-bg); }
    .registration-summary { margin-bottom: 10px; padding: 9px 11px; border-radius: 10px; color: var(--label); background: var(--subtle); font-size: 12px; }
    .registration-summary.success { color: var(--success); background: var(--success-soft); }
    .registration-summary.error { color: var(--danger); background: var(--danger-soft); }
    .task-topline { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .task-summary { color: var(--label); font-size: 12px; }
    .task progress { width: 150px; height: 5px; margin: 0; accent-color: var(--accent); }
    .task-accounts { display: grid; gap: 6px; margin-top: 11px; max-height: 205px; overflow: auto; }
    .task-row { display: grid; grid-template-columns: minmax(190px, .8fr) 90px 1.6fr; gap: 10px; padding: 9px 11px; border-radius: 9px; background: var(--surface-soft); color: var(--muted); font-size: 11px; }
    .task-row .task-email { color: var(--text); overflow-wrap: anywhere; }
    .task-log { margin-top: 9px; max-height: 120px; overflow: auto; white-space: pre-wrap; color: var(--muted); font: 11px/1.55 Consolas, monospace; }
    .list-head { display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 20px 22px 14px; }
    .list-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    #verifySummary { max-width: 440px; color: var(--muted); font-size: 11px; text-align: right; }
    .list-head h2 { margin: 0; font-size: 17px; color: var(--text); }
    #summary, #cardLinkSummary { color: var(--muted); font-size: 12px; margin-top: 5px; }
    .toolbar { display: grid; grid-template-columns: minmax(220px, 1fr) 135px 150px 140px auto; gap: 9px; padding: 0 22px 17px; }
    .search-wrap { position: relative; }
    .search-wrap svg { position: absolute; top: 50%; left: 12px; width: 16px; height: 16px; color: var(--muted); transform: translateY(-50%); pointer-events: none; }
    .toolbar input, .toolbar select { height: 40px; margin: 0; border-color: var(--input-border); background: var(--input); color: var(--text); font-size: 12px; }
    .toolbar input { padding-left: 37px; }
    .list { padding: 0 14px 14px; display: grid; gap: 9px; }
    .email-row {
      position: relative;
      display: grid; grid-template-columns: minmax(280px, .9fr) minmax(220px, 1.1fr) auto; align-items: center; gap: 14px;
      padding: 15px 16px; background: var(--row-bg); border: 1px solid var(--border); border-radius: 14px;
      content-visibility: auto; contain-intrinsic-size: auto 72px;
      transition: border-color .12s ease, box-shadow .12s ease;
    }
    .email-row:hover { border-color: color-mix(in srgb, var(--text) 20%, transparent); }
    .email-row.operation-selected {
      border-color: var(--accent);
      background: color-mix(in srgb, var(--accent) 9%, var(--row-bg));
      box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 24%, transparent), 0 14px 32px rgba(0, 0, 0, .16);
    }
    .email-row.operation-selected::before {
      content: ""; position: absolute; top: 11px; bottom: 11px; left: 5px; width: 4px;
      border-radius: 999px; background: var(--accent);
    }
    .email-row.operation-selected:hover { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, var(--row-bg)); }
    .identity { grid-column: 1; display: flex; align-items: center; gap: 13px; min-width: 0; }
    .avatar { width: 38px; height: 38px; display: grid; place-items: center; flex: 0 0 auto; border-radius: 12px; color: var(--text);
      background: var(--accent-soft); font-weight: 780; }
    .identity-copy { min-width: 0; }
    .address { font-size: 14px; font-weight: 650; color: var(--text); overflow-wrap: anywhere; }
    .meta-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
    .created-date { display: inline-flex; align-items: center; min-height: 22px; padding: 2px 8px; border-radius: 999px;
      color: var(--muted); background: var(--subtle); font-size: 10px; font-weight: 650; white-space: nowrap; }
    .operation-badge { display: inline-flex; align-items: center; gap: 5px; min-height: 22px; padding: 2px 9px; border-radius: 999px;
      color: var(--accent-contrast); background: var(--accent); font-size: 10px; font-weight: 800; white-space: nowrap; }
    .operation-badge[hidden] { display: none; }
    .operation-badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 16%, transparent); }
    .date-group { display: flex; align-items: center; gap: 9px; padding: 10px 4px 2px; color: var(--label); font-size: 12px; font-weight: 750; }
    .date-group::after { content: ""; height: 1px; flex: 1; background: var(--border); }
    .date-group-count { color: var(--muted); font-size: 10px; font-weight: 650; }
    .status-badge { display: inline-flex; align-items: center; gap: 6px; padding: 3px 8px; border-radius: 999px; font-size: 10px; font-weight: 700; }
    .status-badge::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
    .status-badge.ready { color: var(--success); background: var(--success-soft); }
    .status-badge.expired { color: var(--warning); background: var(--warning-soft); }
    .status-badge.pending { color: var(--muted); background: var(--subtle); }
    .payment-link-badge { display: inline-flex; align-items: center; min-height: 22px; padding: 2px 8px; border-radius: 999px;
      color: var(--warning); background: var(--warning-soft); font-size: 10px; font-weight: 720; white-space: nowrap; }
    .plan-badge { display: inline-flex; align-items: center; padding: 3px 8px; border-radius: 999px; font-size: 10px; font-weight: 760; }
    .plan-badge.plus { color: var(--warning); background: var(--warning-soft); }
    .plan-badge.free { color: var(--success); background: var(--success-soft); }
    .plan-badge.unverified { color: var(--muted); background: var(--subtle); }
    .account-type-select {
      width: auto; min-height: 22px; height: 22px; margin: 0; padding: 1px 24px 1px 8px;
      border: 0; border-radius: 999px; font-size: 10px; font-weight: 760; cursor: pointer;
    }
    .account-type-select.plus { color: var(--warning); background: var(--warning-soft); }
    .account-type-select.free { color: var(--success); background: var(--success-soft); }
    .account-type-select.unverified { color: var(--muted); background: var(--subtle); }
    .meta { color: var(--muted); font-size: 10px; }
    .quick-actions { grid-column: 3; display: flex; align-items: center; gap: 7px; justify-content: flex-end; }
    .secondary-actions {
      display: none; grid-column: 1 / -1; gap: 7px; flex-wrap: wrap; padding-top: 12px;
      border-top: 1px solid var(--border);
    }
    .email-row.expanded .secondary-actions { display: flex; }
    .action { height: 34px; min-height: 34px; padding: 7px 10px; border-color: var(--border); color: var(--button-text); background: var(--subtle); white-space: nowrap; font-size: 11px; }
    .action:hover:not(:disabled) { background: var(--subtle-hover); }
    .action.quick-copy { color: var(--primary-text); background: var(--primary); border-color: transparent; }
    .more-action { display: inline-flex; align-items: center; gap: 6px; }
    .more-action svg { width: 13px; height: 13px; transition: transform .16s ease; }
    .more-action[aria-expanded="true"] svg { transform: rotate(180deg); }
    .action.danger-action { color: var(--danger); border-color: var(--danger-soft); background: var(--danger-soft); }
    .mfa-badge { display: inline-flex; align-items: center; min-height: 22px; padding: 2px 8px; border-radius: 999px; font-size: 10px; font-weight: 700; color: var(--success); border: 1px solid var(--success-soft); background: var(--success-soft); }
    .mfa-badge.pending { color: var(--warning); border-color: var(--warning-soft); background: var(--warning-soft); }
    .account-state { grid-column: 2; min-width: 0; }
    .account-code { min-height: 42px; display: flex; align-items: center; gap: 10px; padding: 6px 8px 6px 12px;
      border: 1px solid var(--success-soft); border-radius: 11px; background: var(--success-soft); }
    .account-code-label { color: var(--muted); font-size: 10px; white-space: nowrap; }
    .account-code-value { color: var(--success); font: 750 15px/1 "SFMono-Regular", Consolas, monospace; letter-spacing: .12em; user-select: all; }
    .account-code-status { flex: 1; color: var(--success); font-size: 10px; white-space: nowrap; }
    .account-code button { width: 30px; height: 30px; min-height: 30px; flex: 0 0 30px; padding: 0; border-color: var(--border); background: var(--subtle); }
    .account-code button svg { width: 14px; height: 14px; }
    .card-link-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px; }
    .card-link-toolbar { display: grid; grid-template-columns: minmax(220px, 1fr) 170px auto; gap: 9px; padding: 0 22px 17px; }
    .card-link-toolbar input, .card-link-toolbar select {
      height: 40px; margin: 0; border-color: var(--input-border); background: var(--input); color: var(--text); font-size: 12px;
    }
    .card-link-toolbar input { padding-left: 37px; }
    .ph-flow-panel {
      display: grid; grid-template-columns: minmax(250px, .8fr) minmax(430px, 1.2fr); gap: 18px;
      align-items: center; margin: 0 22px 16px; padding: 14px 15px; border: 1px solid var(--warning-soft);
      border-radius: 14px; background: var(--warning-soft);
    }
    .ph-flow-copy strong { display: block; color: var(--warning); font-size: 12px; }
    .ph-flow-copy span { display: block; margin-top: 4px; color: var(--muted); font-size: 10px; line-height: 1.5; }
    .ph-proxy-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
    .ph-proxy-field { margin: 0; color: var(--label); font-size: 10px; font-weight: 680; }
    .ph-proxy-field input {
      height: 36px; margin: 5px 0 0; padding: 7px 10px; border-color: var(--input-border);
      background: var(--input); color: var(--text); font: 11px/1.2 "SFMono-Regular", Consolas, monospace;
    }
    .card-link-list { padding: 0 14px 14px; display: grid; gap: 9px; }
    .card-link-row {
      display: grid; grid-template-columns: minmax(250px, 1fr) minmax(190px, .65fr) auto;
      align-items: center; gap: 18px; padding: 17px 18px; border: 1px solid var(--border);
      border-radius: 15px; background: var(--row-bg); content-visibility: auto; contain-intrinsic-size: auto 78px;
      transition: border-color .12s ease;
    }
    .card-link-row:hover { border-color: color-mix(in srgb, var(--warning) 32%, transparent); }
    .card-link-identity { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .card-link-avatar { width: 40px; height: 40px; display: grid; place-items: center; flex: 0 0 auto; border-radius: 12px; color: var(--warning); background: var(--warning-soft); }
    .card-link-avatar svg { width: 19px; height: 19px; }
    .card-link-address { color: var(--text); font-size: 13px; font-weight: 730; overflow-wrap: anywhere; }
    .card-link-meta { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; margin-top: 6px; }
    .card-link-state { min-width: 0; }
    .card-link-state strong { display: block; color: var(--text); font-size: 12px; }
    .card-link-state span { display: block; margin-top: 4px; color: var(--muted); font-size: 10px; line-height: 1.5; overflow-wrap: anywhere; }
    .card-link-controls { display: flex; align-items: center; justify-content: flex-end; gap: 7px; flex-wrap: wrap; }
    .card-link-mode { width: auto; min-width: 240px; height: 34px; min-height: 34px; margin: 0; padding: 5px 28px 5px 9px; border-color: var(--input-border); background: var(--input); color: var(--text); font-size: 11px; }
    .card-link-controls .action.generate-link { color: var(--warning); border-color: var(--warning-soft); background: var(--warning-soft); }
    .empty { padding: 66px 20px; text-align: center; color: var(--muted); }
    .empty-icon { width: 44px; height: 44px; display: grid; place-items: center; margin: 0 auto 12px; border-radius: 14px; color: var(--muted); background: var(--subtle); }
    .empty-icon svg { width: 21px; height: 21px; }
    .empty strong { display: block; margin-bottom: 4px; color: var(--label); font-size: 13px; }
    .error { color: var(--danger) !important; }
    .toast { position: fixed; right: 20px; bottom: 20px; z-index: 10; max-width: min(360px, calc(100% - 40px)); padding: 11px 14px;
      border: 1px solid var(--border); border-radius: 12px; color: var(--text); background: var(--toast); box-shadow: 0 18px 48px rgba(0,0,0,.24);
      font-size: 12px; opacity: 0; transform: translateY(10px); pointer-events: none; transition: .2s ease; }
    .toast.show { opacity: 1; transform: translateY(0); }
    .toast.error { border-color: var(--danger-soft); }
    @media (max-width: 1020px) {
      .app-shell { grid-template-columns: 190px minmax(0, 1fr); gap: 14px; }
      .card-link-row { grid-template-columns: minmax(220px, 1fr) minmax(180px, .7fr); }
      .card-link-controls { grid-column: 1 / -1; justify-content: flex-start; padding-top: 12px; border-top: 1px solid var(--border); }
    }
    @media (max-width: 760px) {
      .app-shell { width: min(100% - 24px, 1360px); display: block; padding-top: 12px; }
      .side-nav { position: relative; top: auto; min-height: 0; padding: 10px; margin-bottom: 12px; }
      .side-brand, .side-nav-label, .side-nav-note { display: none; }
      .side-nav-list { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
      .nav-item { justify-content: center; }
      .nav-badge { margin-left: 0; }
      main.app-main { padding: 10px 0 48px; }
      .app-header { align-items: flex-start; }
      .runtime-pill { display: none; }
      .stats-grid { grid-template-columns: repeat(2, 1fr); gap: 8px; }
      .card-link-stats { grid-template-columns: repeat(3, 1fr); gap: 8px; }
      .stat-card { padding: 13px 15px; }
      .section-head { padding: 18px; }
      .automation-body { padding: 16px 18px 18px; }
      .list-head { align-items: flex-start; }
      .list-actions { max-width: 52%; }
      #verifySummary { text-align: right; }
      .toolbar { grid-template-columns: minmax(180px, 1fr) 125px 125px 125px auto; padding-inline: 18px; }
      .email-row { grid-template-columns: minmax(0, 1fr); }
      .identity, .account-state, .quick-actions, .secondary-actions { grid-column: 1; }
      .quick-actions { justify-content: flex-start; }
      .card-link-toolbar { padding-inline: 18px; }
      .ph-flow-panel { grid-template-columns: 1fr; margin-inline: 18px; }
      .card-link-row { grid-template-columns: minmax(0, 1fr); gap: 13px; }
      .card-link-state, .card-link-controls { grid-column: 1; }
      .card-link-controls { justify-content: flex-start; }
      .task-row { grid-template-columns: 1fr; }
    }
    @media (max-width: 520px) {
      .brand-mark { width: 42px; height: 42px; }
      .header-actions { align-self: flex-start; }
      .subtitle { display: none; }
      .stats-grid { grid-template-columns: repeat(2, 1fr); }
      .stat-card { display: block; text-align: center; padding: 12px 6px; }
      .stat-icon { width: 30px; height: 30px; margin: 0 auto 8px; }
      .stat-value { font-size: 19px; }
      .controls { align-items: stretch; }
      .controls .primary, .controls .danger { flex: 1 1 42%; }
      .task-topline { display: block; }
      .task progress { width: 100%; margin-top: 10px; }
      .list-head { display: block; }
      .list-actions { max-width: none; margin-top: 12px; justify-content: stretch; }
      .list-actions button { flex: 1; }
      #verifySummary { flex: 1 0 100%; text-align: left; }
      .toolbar { grid-template-columns: 1fr 1fr 44px; }
      .search-wrap { grid-column: 1 / span 2; grid-row: 1; }
      #planFilter { grid-column: 1; grid-row: 2; }
      #statusFilter { grid-column: 2; grid-row: 2; }
      #dateFilter { grid-column: 1 / span 2; grid-row: 3; }
      .toolbar .icon-button { grid-column: 3; grid-row: 1 / span 3; height: 100%; }
      .list-head { padding-inline: 18px; }
      .quick-actions { width: 100%; }
      .quick-actions .action { flex: 1; }
      .card-link-stats { grid-template-columns: 1fr; }
      .card-link-toolbar { grid-template-columns: 1fr 44px; }
      .card-link-toolbar .search-wrap { grid-column: 1; grid-row: 1; }
      #cardLinkStatusFilter { grid-column: 1; grid-row: 2; }
      .card-link-toolbar .icon-button { grid-column: 2; grid-row: 1 / span 2; height: 100%; }
      .ph-proxy-fields { grid-template-columns: 1fr; }
      .card-link-mode { width: 100%; min-width: 0; }
      .card-link-controls .action { flex: 1 1 calc(50% - 7px); }
      .secondary-actions .action { flex: 1 1 calc(50% - 7px); }
    }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; } }
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="side-nav" aria-label="主导航">
      <div class="side-brand">
        <div class="brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="14" rx="3"></rect><path d="m4 7 8 6 8-6"></path></svg></div>
        <div>
          <div class="side-brand-title">隐藏邮箱控制台</div>
          <div class="side-brand-copy">Private Relay</div>
        </div>
      </div>
      <div class="side-nav-label">工作区</div>
      <nav class="side-nav-list">
        <button class="nav-item active" type="button" data-view="accounts" aria-controls="accountsView" aria-current="page">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="3"></rect><path d="m4 7 8 6 8-6"></path></svg>
          <span class="nav-item-label">账号管理</span><span id="accountNavCount" class="nav-badge">—</span>
        </button>
        <button class="nav-item" type="button" data-view="card-links" aria-controls="cardLinksView">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="3"></rect><path d="M3 10h18M7 15h3"></path></svg>
          <span class="nav-item-label">直卡提链接</span><span id="cardLinkNavCount" class="nav-badge">—</span>
        </button>
      </nav>
      <div class="side-nav-note"><strong>直卡链接独立管理</strong>账号、Session 与支付链接分区展示，避免操作混在同一张卡片。</div>
    </aside>

    <main class="app-main">
    <header class="app-header">
      <div>
        <div id="viewEyebrow" class="eyebrow">Account Workspace</div>
        <h1 id="viewTitle">账号管理</h1>
        <div id="viewSubtitle" class="subtitle">管理 iCloud 邮箱、OpenAI Session 与账号凭据</div>
      </div>
      <div class="header-actions">
        <div class="runtime-pill"><span id="runtimeDot" class="runtime-dot"></span><span id="runtimeLabel">正在连接运行环境</span></div>
        <button id="themeToggle" class="icon-button" type="button" aria-label="切换主题" title="切换主题">
          <svg class="theme-icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.65 17.65l1.42 1.42M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.65 6.35l1.42-1.42"></path></svg>
          <svg class="theme-icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M20.5 15.5A8.5 8.5 0 0 1 8.5 3.5 8.5 8.5 0 1 0 20.5 15.5Z"></path></svg>
        </button>
        <button id="logout" class="icon-button" aria-label="退出登录" title="退出登录"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M10 17l5-5-5-5M15 12H3"></path><path d="M14 3h5a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-5"></path></svg></button>
      </div>
    </header>

    <div id="accountsView" class="view-panel">
    <section class="stats-grid" aria-label="邮箱概览">
      <article class="stat-card">
        <div class="stat-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="3"></rect><path d="m4 7 8 6 8-6"></path></svg></div>
        <div><div id="totalCount" class="stat-value">—</div><div class="stat-label">邮箱总数</div></div>
      </article>
      <article class="stat-card">
        <div class="stat-icon plus"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z"></path></svg></div>
        <div><div id="plusCount" class="stat-value">—</div><div class="stat-label">Plus 账号</div></div>
      </article>
      <article class="stat-card">
        <div class="stat-icon success"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M20 6 9 17l-5-5"></path></svg></div>
        <div><div id="freeCount" class="stat-value">—</div><div class="stat-label">Free 账号</div></div>
      </article>
      <article class="stat-card">
        <div class="stat-icon warning"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M12 8v4l2.5 2.5"></path><circle cx="12" cy="12" r="9"></circle></svg></div>
        <div><div id="unverifiedCount" class="stat-value">—</div><div class="stat-label">等待验证</div></div>
      </article>
    </section>

    <section class="card">
      <div class="section-head">
        <div class="section-title">
          <div class="section-glyph"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="4" width="18" height="14" rx="2"></rect><path d="M8 21h8M12 18v3M7 9h.01M10 9h7M7 13h.01M10 13h5"></path></svg></div>
          <div>
          <h2>浏览器获取 Session</h2>
            <div class="section-copy">创建并保存唯一密码；验证码页有密码入口时优先使用，否则自动填写邮箱验证码。密码成功后自动开启 2FA，不再进入设置添加密码。</div>
          </div>
        </div>
      </div>
      <div class="automation-body">
        <div class="controls">
          <label class="switch-field"><input id="headless" type="checkbox" role="switch"> 无头浏览器</label>
          <label class="control-field">认证并发<input id="concurrency" type="number" min="1" max="10" value="3" aria-label="认证并发数"></label>
          <button id="registerOne" class="primary">一键注册新账号</button>
          <button id="fetchAll" class="primary">浏览器取全部</button>
          <button id="stopTask" class="danger" disabled>停止当前任务</button>
        </div>
        <div id="task" class="task" aria-live="polite">
          <div id="registrationSummary" class="registration-summary">一键注册：空闲</div>
          <div class="task-topline">
            <div id="taskSummary" class="task-summary">正在读取浏览器运行环境…</div>
            <progress id="taskProgress" value="0" max="100" hidden></progress>
          </div>
          <div id="taskAccounts" class="task-accounts"></div>
          <div id="taskLog" class="task-log"></div>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="list-head">
        <div>
          <h2>GPT 邮箱列表</h2>
          <div id="summary">正在加载…</div>
        </div>
        <div class="list-actions">
          <div id="verifySummary">验证账号；缺失 Session 时使用无头浏览器，支持多进程</div>
          <button id="verifyAll" class="primary">一键验证账号</button>
          <button id="stopVerify" class="danger" disabled>停止验证</button>
        </div>
      </div>
      <div class="toolbar">
        <div class="search-wrap">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-4-4"></path></svg>
          <input id="search" type="search" placeholder="搜索邮箱地址" autocomplete="off" aria-label="搜索邮箱地址">
        </div>
        <select id="planFilter" aria-label="按账号类型筛选">
          <option value="all">全部分类</option>
          <option value="plus">Plus</option>
          <option value="free">Free</option>
          <option value="unverified">等待验证</option>
        </select>
        <select id="statusFilter" aria-label="按 Session 状态筛选">
          <option value="all">全部状态</option>
          <option value="ready">Session 有效</option>
          <option value="expired">Token 已过期</option>
          <option value="pending">尚未获取</option>
        </select>
        <select id="dateFilter" aria-label="按添加日期筛选">
          <option value="all">全部日期</option>
          <option value="today">今天添加</option>
          <option value="yesterday">昨天添加</option>
          <option value="recent">近 7 天添加</option>
          <option value="earlier">更早添加</option>
          <option value="unknown">日期未知</option>
        </select>
        <button id="refresh" class="icon-button" aria-label="刷新邮箱列表" title="刷新"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.34 5.66"></path><path d="M20 4v7h-7"></path></svg></button>
      </div>
      <div id="list" class="list"><div class="empty"><div class="empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="14" rx="3"></rect><path d="m4 7 8 6 8-6"></path></svg></div><strong>正在加载邮箱</strong>请稍候…</div></div>
    </section>
    </div>

    <div id="cardLinksView" class="view-panel" hidden>
      <section class="card-link-stats" aria-label="直卡链接概览">
        <article class="stat-card">
          <div class="stat-icon plus"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M4 7h16M5 5h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z"></path><path d="M7 15h4"></path></svg></div>
          <div><div id="payableCount" class="stat-value">—</div><div class="stat-label">可提链接账号</div></div>
        </article>
        <article class="stat-card">
          <div class="stat-icon success"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.1 1.1"></path><path d="M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.1-1.1"></path></svg></div>
          <div><div id="generatedLinkCount" class="stat-value">—</div><div class="stat-label">已生成链接</div></div>
        </article>
        <article class="stat-card">
          <div class="stat-icon warning"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 8v4l2.5 2.5"></path></svg></div>
          <div><div id="cardLinkPendingCount" class="stat-value">—</div><div class="stat-label">等待 Session</div></div>
        </article>
      </section>

      <section class="card">
        <div class="list-head">
          <div>
            <h2>直卡提链接</h2>
            <div id="cardLinkSummary">正在加载…</div>
          </div>
          <div class="section-copy">默认使用 PH/PHP hosted 双代理严格零元提取；不会启动 Camoufox，也不会自动付款。</div>
        </div>
        <div class="ph-flow-panel">
          <div class="ph-flow-copy">
            <strong>gpt-link · PH / PHP hosted · 双代理严格 0</strong>
            <span>阶段 1 用建单代理创建带优惠的 Checkout；阶段 2 用优惠代理更新同一 Checkout，并强制校验 oaics_ 与零金额。</span>
          </div>
          <div class="ph-proxy-fields">
            <label class="ph-proxy-field">建单代理
              <input id="cardLinkCreateProxy" type="password" placeholder="代理 1（留空则直连）" autocomplete="off" spellcheck="false">
            </label>
            <label class="ph-proxy-field">优惠代理
              <input id="cardLinkPromotionProxy" type="password" placeholder="代理 2（留空则复用代理 1）" autocomplete="off" spellcheck="false">
            </label>
          </div>
        </div>
        <div class="card-link-toolbar">
          <div class="search-wrap">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-4-4"></path></svg>
            <input id="cardLinkSearch" type="search" placeholder="搜索可提链接账号" autocomplete="off" aria-label="搜索直卡链接账号">
          </div>
          <select id="cardLinkStatusFilter" aria-label="按直卡链接状态筛选">
            <option value="all">全部账号</option>
            <option value="generated">已生成链接</option>
            <option value="available">可生成链接</option>
            <option value="unavailable">等待 Session</option>
          </select>
          <button id="cardLinkRefresh" class="icon-button" aria-label="刷新直卡链接列表" title="刷新"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.34 5.66"></path><path d="M20 4v7h-7"></path></svg></button>
        </div>
        <div id="cardLinkList" class="card-link-list"><div class="empty"><div class="empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="14" rx="3"></rect><path d="M3 10h18M7 15h3"></path></svg></div><strong>正在加载账号</strong>请稍候…</div></div>
      </section>
    </div>
    </main>
  </div>
  <div id="toast" class="toast" role="status" aria-live="polite"></div>

  <script>
    const localToken = __LOCAL_TOKEN__;
    const $ = (id) => document.getElementById(id);
    const themeToggle = $("themeToggle");
    let currentItems = [];
    let visibleItems = [];
    let retrievedCode = null;
    let taskPoll = null;
    let registrationPoll = null;
    let verificationPoll = null;
    let browserRunning = false;
    let browserStartedAt = "";
    let activeBrowserEmails = new Set();
    let verificationRunning = false;
    let verificationStartedAt = "";
    let activeVerificationEmails = new Set();
    let registrationRunning = false;
    let browserRuntimeAvailable = false;
    let toastTimer = null;
    let selectedOperationEmail = "";
    const cardLinkExtractionModes = [
      ["ph_hosted", "gpt-link · PH / PHP hosted · 双代理严格 0"],
      ["de_oaics_paypal", "PayPal / 德国 · EUR · OAICS 严格 0"],
      ["standard:US", "标准直卡 · 美国 · USD"],
      ["standard:JP", "标准直卡 · 日本 · JPY"],
      ["standard:DE", "标准直卡 · 德国 · EUR"],
      ["standard:GB", "标准直卡 · 英国 · GBP"],
      ["standard:CA", "标准直卡 · 加拿大 · CAD"],
      ["standard:AU", "标准直卡 · 澳大利亚 · AUD"],
    ];
    const viewDetails = {
      accounts: {
        eyebrow: "Account Workspace",
        title: "账号管理",
        subtitle: "管理 iCloud 邮箱、OpenAI Session 与账号凭据",
      },
      "card-links": {
        eyebrow: "Checkout Workspace",
        title: "直卡提链接",
        subtitle: "集中生成、复制和打开 ChatGPT Plus 直卡支付链接",
      },
    };

    function setView(view, updateHash = true) {
      const target = viewDetails[view] ? view : "accounts";
      $("accountsView").hidden = target !== "accounts";
      $("cardLinksView").hidden = target !== "card-links";
      for (const button of document.querySelectorAll(".nav-item[data-view]")) {
        const active = button.dataset.view === target;
        button.classList.toggle("active", active);
        if (active) button.setAttribute("aria-current", "page");
        else button.removeAttribute("aria-current");
      }
      $("viewEyebrow").textContent = viewDetails[target].eyebrow;
      $("viewTitle").textContent = viewDetails[target].title;
      $("viewSubtitle").textContent = viewDetails[target].subtitle;
      document.title = `${viewDetails[target].title} · 隐藏邮箱控制台`;
      if (updateHash && location.hash !== `#${target}`) {
        history.replaceState(null, "", `#${target}`);
      }
    }

    function applyTheme(theme, persist = false) {
      document.documentElement.dataset.theme = theme;
      document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#0f0f10" : "#f7f7f5";
      themeToggle.setAttribute("aria-label", theme === "dark" ? "切换至白天模式" : "切换至夜间模式");
      themeToggle.title = themeToggle.getAttribute("aria-label");
      if (persist) {
        try { localStorage.setItem("hme_theme", theme); } catch (_) {}
      }
    }

    applyTheme(document.documentElement.dataset.theme || "dark");

    async function api(path, options = {}) {
      const headers = { ...(options.headers || {}) };
      if (options.method && options.method !== "GET") headers["X-Local-Token"] = localToken;
      if (options.body) headers["Content-Type"] = "application/json";
      let response;
      try {
        response = await fetch(path, { ...options, headers, cache: "no-store" });
      } catch (_) {
        throw new Error("无法连接本地服务，请刷新页面后重试");
      }
      const data = await response.json().catch(() => ({ ok: false, error: "服务响应无效" }));
      if (response.status === 401) {
        location.replace("/login");
        throw new Error("登录已过期");
      }
      if (response.status === 403 && data.error === "本地请求令牌无效") {
        setTimeout(() => location.reload(), 100);
        throw new Error("本地服务已重启，正在刷新页面");
      }
      if (!response.ok || data.ok === false) {
        const error = new Error(data.error || `请求失败 (${response.status})`);
        error.status = response.status;
        throw error;
      }
      return data;
    }

    function showToast(message, type = "") {
      const toast = $("toast");
      clearTimeout(toastTimer);
      toast.className = `toast ${type}`.trim();
      toast.textContent = message;
      requestAnimationFrame(() => toast.classList.add("show"));
      toastTimer = setTimeout(() => toast.classList.remove("show"), 2600);
    }

    function actionButton(label, action, successLabel = "已复制") {
      const button = document.createElement("button");
      button.className = "action";
      button.textContent = label;
      button.addEventListener("click", async () => {
        const original = button.textContent;
        button.disabled = true;
        button.textContent = "处理中…";
        try {
          const result = await action(button);
          const resolvedLabel = result && result.successLabel ? result.successLabel : successLabel;
          button.textContent = resolvedLabel;
          showToast(resolvedLabel);
          setTimeout(() => { button.textContent = original; }, 1200);
        } catch (error) {
          button.textContent = original;
          if (error.name !== "AbortError") showToast(error.message, "error");
        } finally {
          button.disabled = false;
        }
      });
      return button;
    }

    async function copyText(value) {
      const text = String(value ?? "");
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (_clipboardError) {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        textarea.style.pointerEvents = "none";
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        try { return document.execCommand("copy"); }
        catch (_fallbackError) { return false; }
        finally { textarea.remove(); }
      }
    }

    async function copyCredential(email, kind) {
      const data = await api("/api/gpt-credential", {
        method: "POST", body: JSON.stringify({ email, kind })
      });
      if (!await copyText(data.value)) throw new Error("浏览器拒绝复制，请检查剪贴板权限");
    }

    async function copyOpenAiCode(email, button) {
      // An active browser task must only receive a code sent for this run.
      // Outside a task, allow a recently delivered code. The server records
      // each served message so the same code cannot be returned twice.
      const targetEmail = String(email || "").toLowerCase();
      const activeTaskStartedAt = [
        browserRunning && activeBrowserEmails.has(targetEmail) ? browserStartedAt : "",
        verificationRunning && activeVerificationEmails.has(targetEmail)
          ? verificationStartedAt
          : "",
      ].filter(Boolean).sort().pop() || "";
      const since = activeTaskStartedAt
        || new Date(Date.now() - 5 * 60_000).toISOString();
      const deadline = Date.now() + 60_000;
      if (retrievedCode?.email === email) {
        retrievedCode = null;
        button.closest(".email-row")?.querySelector(".account-state")?.replaceChildren();
      }
      button.textContent = activeTaskStartedAt ? "等待本轮验证码…" : "查找未使用验证码…";
      while (Date.now() < deadline) {
        try {
          const data = await api("/api/gpt-code", {
            method: "POST", body: JSON.stringify({ email, since })
          });
          const copied = await copyText(data.code);
          retrievedCode = { email, code: data.code, copied };
          render(visibleItems);
          return { successLabel: copied ? "验证码已获取并复制" : "验证码已获取，请手动复制" };
        } catch (error) {
          if (error.status !== 404) throw error;
        }
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
      throw new Error(activeTaskStartedAt
        ? "未找到本轮任务发送的新验证码，请让 OpenAI 重新发送后再试"
        : "未找到最近 5 分钟内尚未使用的新验证码，请让 OpenAI 重新发送后再试");
    }

    async function copyAccount(email) {
      const response = await fetch("/api/gpt-accounts/export", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Local-Token": localToken },
        body: JSON.stringify({ email }),
        cache: "no-store"
      });
      if (response.status === 401) {
        location.replace("/login");
        throw new Error("登录已过期");
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({ error: "复制账号失败" }));
        throw new Error(data.error || `复制失败 (${response.status})`);
      }
      const content = await response.text();
      if (!await copyText(content)) {
        throw new Error("浏览器拒绝复制，请检查剪贴板权限");
      }
      return { successLabel: "已复制账号" };
    }

    async function importAccountToWorkbench(email) {
      const data = await api("/api/account/import-workbench", {
        method: "POST",
        body: JSON.stringify({ email }),
      });
      if (data.group === "Plus") {
        return {
          successLabel: data.updated ? "工作台 Plus 分组已更新" : "已导入工作台 Plus 分组",
        };
      }
      return {
        successLabel: data.updated ? "工作台账号已更新" : "已导入工作台",
      };
    }

    function cardLinkDateLabel(value) {
      if (!value) return "";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return "";
      return date.toLocaleString("zh-CN", {
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
        hour12: false,
      });
    }

    async function generateCardLink(item, extractionMode) {
      const hosted = extractionMode === "ph_hosted";
      const deOaicsPayPal = extractionMode === "de_oaics_paypal";
      const country = hosted ? "PH" : deOaicsPayPal ? "DE" : extractionMode.split(":", 2)[1] || "US";
      const data = await api("/api/account/card-link", {
        method: "POST",
        body: JSON.stringify({
          email: item.email,
          method: hosted ? "ph_hosted" : deOaicsPayPal ? "de_oaics_paypal" : "standard",
          country,
          create_proxy: hosted || deOaicsPayPal ? $("cardLinkCreateProxy").value.trim() : "",
          promotion_proxy: hosted ? $("cardLinkPromotionProxy").value.trim() : "",
        }),
      });
      if (!await copyText(data.url)) {
        throw new Error("链接已生成，但浏览器拒绝复制，请使用打开支付页");
      }
      await load();
      return {
        successLabel: hosted
          ? "PH/PHP hosted 严格 0 链接已提取并复制"
          : "直卡链接已生成并复制",
      };
    }

    async function copyCardLink(item) {
      if (!item.cardLink) throw new Error("请先生成直卡支付链接");
      if (!await copyText(item.cardLink)) {
        throw new Error("浏览器拒绝复制，请检查剪贴板权限");
      }
      return { successLabel: "直卡链接已复制" };
    }

    function openCardLink(item) {
      if (!item.cardLink) throw new Error("请先生成直卡支付链接");
      window.open(item.cardLink, "_blank", "noopener,noreferrer");
      return { successLabel: "支付页已打开" };
    }

    async function deleteEmail(email) {
      const isGmail = email.toLowerCase().endsWith("@gmail.com");
      const warning = isGmail
        ? `确定从本机账号列表删除 ${email} 吗？\n\n该操作会清除本地保存的 OpenAI 密码、Session、AT、2FA 和 SMSBower 激活记录，但不会删除 Gmail 服务商侧的邮箱，无法撤销。`
        : `确定永久删除邮箱 ${email} 吗？\n\n该操作会停用并删除 iCloud 隐藏邮箱，同时清除本地保存的 OpenAI 密码、Session、AT 和 2FA，无法撤销。`;
      if (!confirm(warning)) {
        const error = new Error("已取消删除");
        error.name = "AbortError";
        throw error;
      }
      const data = await api("/api/gpt-email/delete", {
        method: "POST", body: JSON.stringify({ email })
      });
      await load();
      if (!data.deleted) throw new Error(data.message || "邮箱已停用，但永久删除失败");
      return data;
    }

    function browserOptions() {
      const concurrency = Number($('concurrency').value);
      if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 10) {
        throw new Error("认证并发必须是 1–10 的整数");
      }
      return { headless: $('headless').checked, concurrency };
    }

    async function startBrowser(emails = null) {
      const path = emails ? "/api/browser/fetch-selected" : "/api/browser/fetch-all";
      const payload = browserOptions();
      if (emails) payload.emails = emails;
      const data = await api(path, { method: "POST", body: JSON.stringify(payload) });
      if (!data.started) {
        showToast(data.message || "无需重复获取");
      }
      await loadTask();
      return data;
    }

    async function verifyOrRegisterAccount(item, resetPassword = false) {
      if (resetPassword && !confirm(`将通过邮箱验证码为 ${item.email} 重置密码，是否继续？`)) {
        const error = new Error("已取消重置密码");
        error.name = "AbortError";
        throw error;
      }
      if (!resetPassword && !item.hasCookies) {
        throw new Error("该账号尚未保存 Cookie，请先重新登录或注册获取 Cookie");
      }
      const verifyPrompt = `将使用 ${item.email} 已保存的 Cookie 重新获取 Session、套餐和账号状态；不会设置密码或 2FA。是否继续？`;
      if (!resetPassword && !confirm(verifyPrompt)) {
        const error = new Error("已取消验证账号");
        error.name = "AbortError";
        throw error;
      }
      const data = await api("/api/account/verify-or-register", {
        method: "POST",
        body: JSON.stringify({
          email: item.email,
          headless: $("headless").checked,
          reset_password: resetPassword,
          refresh_with_cookie: !resetPassword,
        }),
      });
      if (data.mode === "refresh_cookie") {
        await loadVerification();
        return { successLabel: "正在使用 Cookie 刷新账号状态" };
      }
      if (data.mode === "refresh_session") {
        await loadVerification();
        return { successLabel: "正在重新获取 Session" };
      }
      await loadTask();
      return { successLabel: data.mode === "set_password" ? "密码设置已启动" : "注册已启动" };
    }

    async function setAccountType(email, accountType) {
      const data = await api("/api/account/type", {
        method: "POST",
        body: JSON.stringify({ email, account_type: accountType }),
      });
      showToast(
        data.accountType === "unverified"
          ? "已恢复为等待验证"
          : `已手动设置为 ${planLabel(data.accountType)}`
      );
      await load();
      return data;
    }

    function syncBrowserButtons() {
      const busy = browserRunning || registrationRunning;
      $("registerOne").disabled = busy || !browserRuntimeAvailable;
      $("fetchAll").disabled = busy || !browserRuntimeAvailable;
      $("stopTask").disabled = !busy;
    }

    function renderTask(data) {
      const runtime = data.runtime || {};
      const accounts = data.accounts || [];
      const statusNames = {
        idle: "空闲", running: "运行中", cancelling: "正在停止",
        completed: "已完成", cancelled: "已停止",
      };
      $("runtimeDot").className = `runtime-dot ${runtime.available ? "ok" : "bad"}`;
      $("runtimeLabel").textContent = runtime.available ? "运行环境已连接" : "运行环境不可用";
      browserRuntimeAvailable = Boolean(runtime.available);
      browserRunning = Boolean(data.running);
      browserStartedAt = browserRunning ? String(data.startedAt || "") : "";
      activeBrowserEmails = new Set(
        browserRunning ? accounts.map((item) => String(item.email || "").toLowerCase()) : []
      );
      if (!runtime.available) {
        $("taskSummary").className = "task-summary error";
        $("taskSummary").textContent = (runtime.errors || ["Camoufox 运行环境不可用"]).join("；");
      } else if (data.status === "idle") {
        $("taskSummary").className = "task-summary";
        $("taskSummary").textContent = `运行环境已连接：${runtime.targetProject}`;
      } else {
        $("taskSummary").className = "task-summary";
        $("taskSummary").textContent = `${statusNames[data.status] || data.status} · ${data.completed || 0}/${data.total || 0} · 成功 ${data.succeeded || 0} · 失败 ${data.failed || 0} · 跳过 ${data.skipped || 0}`;
      }
      if (runtime.forceHeadless) {
        $("headless").checked = true;
        $("headless").disabled = true;
        $("headless").title = "服务器环境固定使用无头浏览器";
      }
      syncBrowserButtons();
      const progress = $("taskProgress");
      progress.hidden = !data.running;
      progress.value = data.total ? Math.round(((data.completed || 0) / data.total) * 100) : 0;

      const accountRoot = $("taskAccounts");
      accountRoot.replaceChildren();
      for (const item of accounts) {
        const row = document.createElement("div");
        row.className = "task-row";
        const email = document.createElement("div");
        email.className = "task-email";
        email.textContent = item.email;
        const status = document.createElement("div");
        status.textContent = item.status;
        const message = document.createElement("div");
        message.textContent = item.message || item.latestLog || "";
        row.append(email, status, message);
        accountRoot.append(row);
      }
      const logs = (data.logs || []).slice(-30).map((item) => `${item.email ? `[${item.email}] ` : ""}${item.message}`);
      $("taskLog").textContent = logs.join("\n");
      if (data.running) {
        clearTimeout(taskPoll);
        taskPoll = setTimeout(loadTask, 1500);
      }
    }

    async function loadTask() {
      try {
        const data = await api("/api/browser/status");
        renderTask(data);
        if (!data.running && data.status !== "idle") await load();
      } catch (error) {
        $("taskSummary").className = "task-summary error";
        $("taskSummary").textContent = error.message;
        $("runtimeDot").className = "runtime-dot bad";
        $("runtimeLabel").textContent = "连接失败";
        clearTimeout(taskPoll);
        taskPoll = setTimeout(loadTask, 1500);
      }
    }

    function renderRegistration(data) {
      registrationRunning = Boolean(data.running);
      const phaseNames = {
        idle: "空闲",
        generating_email: "正在生成 iCloud 邮箱",
        confirming_email: "正在同步新邮箱",
        registering_openai: "正在注册 OpenAI",
        completed: "注册成功",
        failed: "注册失败",
        cancelling: "正在停止",
        cancelled: "已停止",
      };
      const summary = $("registrationSummary");
      summary.className = `registration-summary ${data.status === "completed" ? "success" : data.status === "failed" ? "error" : ""}`.trim();
      const phase = phaseNames[data.phase] || data.phase || "空闲";
      summary.textContent = `一键注册：${phase}${data.email ? ` · ${data.email}` : ""}${data.message && data.phase !== "idle" ? ` · ${data.message}` : ""}`;
      syncBrowserButtons();
      if (data.running) {
        clearTimeout(registrationPoll);
        registrationPoll = setTimeout(loadRegistration, 1200);
      }
    }

    async function loadRegistration() {
      try {
        const data = await api("/api/registration/status");
        const wasRunning = registrationRunning;
        renderRegistration(data);
        if (wasRunning && !data.running) {
          await Promise.all([load(), loadTask()]);
          showToast(data.status === "completed" ? "新账号已加入邮箱列表" : data.message, data.status === "completed" ? "" : "error");
        }
      } catch (error) {
        $("registrationSummary").className = "registration-summary error";
        $("registrationSummary").textContent = `一键注册：${error.message}`;
        clearTimeout(registrationPoll);
        registrationPoll = setTimeout(loadRegistration, 1500);
      }
    }

    async function startRegistration() {
      const options = browserOptions();
      const data = await api("/api/registration/start", {
        method: "POST",
        body: JSON.stringify({
          label: "OpenAI 一键注册",
          provider: "inventory",
          ...options,
        }),
      });
      renderRegistration(data.task);
      showToast(`已按并发 ${options.concurrency} 启动库存注册`);
    }

    function renderVerification(data) {
      const runtime = data.runtime || {};
      const accounts = data.accounts || [];
      const statusNames = {
        idle: "尚未验证", running: "验证中", cancelling: "正在停止",
        completed: "验证完成", cancelled: "已停止",
      };
      verificationRunning = Boolean(data.running);
      verificationStartedAt = verificationRunning ? String(data.startedAt || "") : "";
      activeVerificationEmails = new Set(
        verificationRunning
          ? accounts.map((item) => String(item.email || "").toLowerCase())
          : []
      );
      if (!runtime.available) {
        $("verifySummary").className = "error";
        $("verifySummary").textContent = (runtime.errors || ["账号验证运行环境不可用"]).join("；");
      } else if (data.status === "idle") {
        $("verifySummary").className = "";
        $("verifySummary").textContent = "验证账号；缺失 Session 时使用无头浏览器，支持多进程";
      } else {
        $("verifySummary").className = data.failed ? "error" : "";
        $("verifySummary").textContent = `${statusNames[data.status] || data.status} · ${data.completed || 0}/${data.total || 0} · Plus ${data.plus || 0} · Free ${data.free || 0} · Token 失效 ${data.expired || 0} · 失败 ${data.failed || 0}`;
      }
      $("verifyAll").disabled = Boolean(data.running) || !runtime.available;
      $("stopVerify").disabled = !data.running;
      if (data.running) {
        clearTimeout(verificationPoll);
        verificationPoll = setTimeout(loadVerification, 1500);
      }
    }

    async function loadVerification() {
      try {
        const data = await api("/api/account-verification/status");
        const wasRunning = $("stopVerify").disabled === false;
        renderVerification(data);
        if (wasRunning && !data.running) await load();
      } catch (error) {
        $("verifySummary").className = "error";
        $("verifySummary").textContent = error.message;
        clearTimeout(verificationPoll);
        verificationPoll = setTimeout(loadVerification, 1500);
      }
    }

    async function startVerification() {
      const emails = currentItems.map((item) => item.email);
      if (!emails.length) throw new Error("暂无可验证账号");
      const concurrency = browserOptions().concurrency;
      const refreshCount = currentItems.filter((item) => item.sessionStatus !== "ready").length;
      if (!confirm(
        `将验证 ${emails.length} 个账号；其中 ${refreshCount} 个账号会通过无头浏览器重新获取 Session，最多并行 ${concurrency} 个进程。不会设置密码或 2FA，是否继续？`
      )) return;
      const data = await api("/api/account-verification/start", {
        method: "POST", body: JSON.stringify({ concurrency, emails })
      });
      renderVerification(data.task);
      showToast("账号验证已启动");
    }

    function sessionLabel(status) {
      return status === "ready" ? "Session 有效" : status === "expired" ? "Token 已过期" : "尚未获取";
    }

    function planLabel(accountType) {
      return accountType === "plus" ? "Plus" : accountType === "free" ? "Free" : "等待验证";
    }

    const dateCategoryLabels = {
      today: "今天添加",
      yesterday: "昨天添加",
      recent: "近 7 天添加",
      earlier: "更早添加",
      unknown: "添加日期未知",
    };

    function itemCreatedDate(item) {
      if (!item.createdAt) return null;
      const value = new Date(item.createdAt);
      return Number.isNaN(value.getTime()) ? null : value;
    }

    function dateCategory(item) {
      const created = itemCreatedDate(item);
      if (!created) return "unknown";
      const now = new Date();
      const today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
      const createdDay = Date.UTC(created.getFullYear(), created.getMonth(), created.getDate());
      const daysAgo = Math.floor((today - createdDay) / 86400000);
      if (daysAgo <= 0) return "today";
      if (daysAgo === 1) return "yesterday";
      if (daysAgo < 7) return "recent";
      return "earlier";
    }

    function createdDateLabel(item) {
      const created = itemCreatedDate(item);
      if (!created) return "添加日期未知";
      return `添加于 ${created.toLocaleString("zh-CN", {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hour12: false,
      })}`;
    }

    function renderEmpty(title, detail) {
      const empty = document.createElement("div");
      empty.className = "empty";
      const icon = document.createElement("div");
      icon.className = "empty-icon";
      icon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="3"></rect><path d="m4 7 8 6 8-6"></path></svg>';
      const heading = document.createElement("strong");
      heading.textContent = title;
      const copy = document.createTextNode(detail);
      empty.append(icon, heading, copy);
      return empty;
    }

    function syncOperationSelection() {
      for (const row of $("list").querySelectorAll(".email-row")) {
        const selected = row.dataset.accountEmail === selectedOperationEmail;
        row.classList.toggle("operation-selected", selected);
        row.classList.toggle("expanded", selected);
        if (selected) row.setAttribute("aria-current", "true");
        else row.removeAttribute("aria-current");
        const moreButton = row.querySelector(".more-action");
        moreButton.setAttribute("aria-expanded", String(selected));
        moreButton.querySelector(".more-action-label").textContent = selected ? "收起操作" : "更多操作";
        row.querySelector(".operation-badge").hidden = !selected;
      }
    }

    function render(items) {
      const root = $("list");
      root.replaceChildren();
      if (!items.length) {
        const filtered = currentItems.length > 0;
        root.append(renderEmpty(filtered ? "没有匹配的邮箱" : "暂无 GPT 邮箱", filtered ? "尝试更换搜索词或筛选条件" : "同步邮箱后会显示在这里"));
        return;
      }
      let activeDateCategory = "";
      for (const item of items) {
        const itemDateCategory = dateCategory(item);
        if (itemDateCategory !== activeDateCategory) {
          activeDateCategory = itemDateCategory;
          const count = items.filter((candidate) => dateCategory(candidate) === itemDateCategory).length;
          const group = document.createElement("div");
          group.className = "date-group";
          const label = document.createElement("span");
          label.textContent = dateCategoryLabels[itemDateCategory];
          const total = document.createElement("span");
          total.className = "date-group-count";
          total.textContent = `${count} 个`;
          group.append(label, total);
          root.append(group);
        }
        const row = document.createElement("div");
        row.className = "email-row";
        row.dataset.accountEmail = item.email;
        const identity = document.createElement("div");
        identity.className = "identity";
        const avatar = document.createElement("div");
        avatar.className = "avatar";
        avatar.textContent = "@";
        const identityCopy = document.createElement("div");
        identityCopy.className = "identity-copy";
        const address = document.createElement("div");
        address.className = "address";
        address.textContent = item.email;
        const metaLine = document.createElement("div");
        metaLine.className = "meta-line";
        const badge = document.createElement("span");
        badge.className = `status-badge ${item.sessionStatus}`;
        badge.textContent = sessionLabel(item.sessionStatus);
        const planSelect = document.createElement("select");
        planSelect.className = `account-type-select ${item.accountType}`;
        planSelect.setAttribute("aria-label", `更改 ${item.email} 的账号类型`);
        planSelect.title = item.accountTypeSource === "manual"
          ? "当前为手动设置；选择等待验证可恢复在线验证"
          : "手动更改账号类型";
        for (const [value, label] of [["unverified", "等待验证"], ["free", "Free"], ["plus", "Plus"]]) {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = label;
          planSelect.append(option);
        }
        planSelect.value = item.accountType;
        planSelect.addEventListener("change", async () => {
          const previous = item.accountType;
          planSelect.disabled = true;
          try {
            await setAccountType(item.email, planSelect.value);
          } catch (error) {
            planSelect.value = previous;
            showToast(error.message, "error");
          } finally {
            planSelect.disabled = false;
          }
        });
        const createdDate = document.createElement("span");
        createdDate.className = "created-date";
        createdDate.textContent = createdDateLabel(item);
        const operationBadge = document.createElement("span");
        operationBadge.className = "operation-badge";
        operationBadge.textContent = "正在操作";
        operationBadge.hidden = true;
        metaLine.append(operationBadge, planSelect, badge, createdDate);
        if (item.hasTwoFactor) {
          const mfaBadge = document.createElement("span");
          mfaBadge.className = "mfa-badge";
          mfaBadge.textContent = "2FA 已开启";
          metaLine.append(mfaBadge);
        }
        identityCopy.append(address, metaLine);
        identity.append(avatar, identityCopy);
        const accountState = document.createElement("div");
        accountState.className = "account-state";
        if (retrievedCode?.email === item.email) {
          const codeResult = document.createElement("div");
          codeResult.className = "account-code";
          codeResult.setAttribute("role", "status");
          codeResult.setAttribute("aria-live", "polite");
          const codeLabel = document.createElement("span");
          codeLabel.className = "account-code-label";
          codeLabel.textContent = "最新验证码";
          const codeValue = document.createElement("strong");
          codeValue.className = "account-code-value";
          codeValue.textContent = retrievedCode.code;
          const codeStatus = document.createElement("span");
          codeStatus.className = "account-code-status";
          codeStatus.textContent = retrievedCode.copied ? "已复制" : "可手动复制";
          const copyCodeButton = document.createElement("button");
          copyCodeButton.type = "button";
          copyCodeButton.setAttribute("aria-label", `复制 ${item.email} 的验证码`);
          copyCodeButton.title = "复制验证码";
          copyCodeButton.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="8" y="8" width="11" height="11" rx="2"></rect><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"></path></svg>';
          copyCodeButton.addEventListener("click", async () => {
            if (await copyText(retrievedCode.code)) {
              retrievedCode.copied = true;
              codeStatus.textContent = "已复制";
              showToast("验证码已复制");
            } else {
              showToast("复制失败，请手动选择验证码", "error");
            }
          });
          const dismissCodeButton = document.createElement("button");
          dismissCodeButton.type = "button";
          dismissCodeButton.setAttribute("aria-label", `关闭 ${item.email} 的验证码状态`);
          dismissCodeButton.title = "关闭";
          dismissCodeButton.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"></path></svg>';
          dismissCodeButton.addEventListener("click", () => {
            retrievedCode = null;
            accountState.replaceChildren();
          });
          codeResult.append(codeLabel, codeValue, codeStatus, copyCodeButton, dismissCodeButton);
          accountState.append(codeResult);
        }
        const quickActions = document.createElement("div");
        quickActions.className = "quick-actions";
        const copyEmailButton = actionButton("复制邮箱", async () => {
          if (!await copyText(item.email)) throw new Error("浏览器拒绝复制，请检查剪贴板权限");
        });
        copyEmailButton.classList.add("quick-copy");
        const importWorkbenchButton = actionButton(
          "一键导入工作台",
          () => importAccountToWorkbench(item.email),
          "已导入工作台"
        );
        importWorkbenchButton.classList.add("quick-import");
        if (!item.hasImportableSession) {
          importWorkbenchButton.disabled = true;
          importWorkbenchButton.title = "请先获取 Session 后再导入工作台";
        }
        const moreButton = document.createElement("button");
        moreButton.type = "button";
        moreButton.className = "action more-action";
        moreButton.setAttribute("aria-expanded", "false");
        moreButton.innerHTML = '<span class="more-action-label">更多操作</span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m6 9 6 6 6-6"></path></svg>';
        quickActions.append(copyEmailButton, moreButton);
        const secondaryActions = document.createElement("div");
        secondaryActions.className = "secondary-actions";
        secondaryActions.append(importWorkbenchButton);
        const verifyAccountButton = actionButton("Cookie 刷新状态", () => verifyOrRegisterAccount(item), "刷新已启动");
        verifyAccountButton.disabled = !item.hasCookies;
        verifyAccountButton.title = item.hasCookies
          ? "使用保存的 Cookie 重新获取 Session、套餐和账号状态"
          : "该账号尚未保存 Cookie";
        secondaryActions.append(verifyAccountButton);
        if (!item.hasPassword) {
          secondaryActions.append(actionButton("设置密码", () => verifyOrRegisterAccount(item, true), "密码设置已启动"));
        }
        if (item.hasPassword) {
          secondaryActions.append(
            actionButton("复制密码", () => copyCredential(item.email, "password")),
            actionButton("重置密码", () => verifyOrRegisterAccount(item, true), "密码重置已启动")
          );
        }
        if (item.hasTwoFactor) {
          secondaryActions.append(
            actionButton("复制 2FA 密钥", () => copyCredential(item.email, "totp_secret")),
            actionButton("复制 2FA 码", () => copyCredential(item.email, "totp_code"))
          );
        }
        if (item.hasSession) {
          secondaryActions.append(
            actionButton("复制 AT", () => copyCredential(item.email, "access_token")),
            actionButton("复制 Session", () => copyCredential(item.email, "session"))
          );
        }
        secondaryActions.append(actionButton("获取 OpenAI 码", (button) => copyOpenAiCode(item.email, button)));
        const deleteButton = actionButton("删除邮箱", () => deleteEmail(item.email), "已删除");
        deleteButton.classList.add("danger-action");
        const copyAccountButton = actionButton("复制账号", () => copyAccount(item.email), "已复制账号");
        if (!item.hasPassword) {
          copyAccountButton.disabled = true;
          copyAccountButton.title = "该账号尚未保存密码";
        }
        secondaryActions.append(deleteButton, copyAccountButton);
        moreButton.addEventListener("click", () => {
          selectedOperationEmail = selectedOperationEmail === item.email ? "" : item.email;
          syncOperationSelection();
        });
        row.append(identity, accountState, quickActions, secondaryActions);
        root.append(row);
      }
      syncOperationSelection();
    }

    function renderCardLinks(items) {
      const root = $("cardLinkList");
      root.replaceChildren();
      if (!items.length) {
        const filtered = currentItems.length > 0;
        root.append(renderEmpty(
          filtered ? "没有匹配的账号" : "暂无可提链接账号",
          filtered ? "尝试更换搜索词或链接状态" : "账号保存有效 Session 后会显示在这里"
        ));
        return;
      }
      const planLabels = { plus: "Plus", free: "Free", unverified: "等待验证" };
      for (const item of items) {
        const row = document.createElement("article");
        row.className = "card-link-row";
        row.dataset.cardLinkEmail = item.email;

        const identity = document.createElement("div");
        identity.className = "card-link-identity";
        const avatar = document.createElement("div");
        avatar.className = "card-link-avatar";
        avatar.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="3"></rect><path d="M3 10h18M7 15h3"></path></svg>';
        const identityCopy = document.createElement("div");
        identityCopy.className = "identity-copy";
        const address = document.createElement("div");
        address.className = "card-link-address";
        address.textContent = item.email;
        const meta = document.createElement("div");
        meta.className = "card-link-meta";
        const planBadge = document.createElement("span");
        planBadge.className = `plan-badge ${item.accountType}`;
        planBadge.textContent = planLabels[item.accountType] || "等待验证";
        const sessionBadge = document.createElement("span");
        sessionBadge.className = `status-badge ${item.sessionStatus}`;
        sessionBadge.textContent = sessionLabel(item.sessionStatus);
        meta.append(planBadge, sessionBadge);
        if (item.cardLink) {
          const linkBadge = document.createElement("span");
          linkBadge.className = "payment-link-badge";
          linkBadge.textContent = item.cardLinkMethod === "de_oaics_paypal"
            ? "PayPal DE/EUR"
            : item.cardLinkMethod === "ph_hosted" ? "hosted 严格 0" : "已生成";
          meta.append(linkBadge);
        }
        identityCopy.append(address, meta);
        identity.append(avatar, identityCopy);

        const state = document.createElement("div");
        state.className = "card-link-state";
        const stateTitle = document.createElement("strong");
        const stateDetail = document.createElement("span");
        const generatedLabel = cardLinkDateLabel(item.cardLinkGeneratedAt);
        if (item.cardLink) {
          if (item.cardLinkMethod === "de_oaics_paypal") {
            const zeroVerified = item.cardLinkAmount === "0";
            stateTitle.textContent = "PayPal DE/EUR OAICS 严格 0 已提取";
            stateDetail.textContent = `PayPal BA · ${zeroVerified ? "零金额已校验" : "严格零元流程"}${generatedLabel ? ` · ${generatedLabel} 生成` : ""} · 建议尽快使用`;
          } else if (item.cardLinkMethod === "ph_hosted") {
            const zeroVerified = item.cardLinkAmount === "0";
            stateTitle.textContent = "PH/PHP hosted 严格 0 已提取";
            stateDetail.textContent = `oaics_ · ${zeroVerified ? "零金额已校验" : "严格零元流程"}${generatedLabel ? ` · ${generatedLabel} 生成` : ""} · 建议尽快使用`;
          } else {
            stateTitle.textContent = "标准直卡链接已生成";
            stateDetail.textContent = `${item.cardLinkCountry} · ${item.cardLinkCurrency}${generatedLabel ? ` · ${generatedLabel} 生成` : ""} · 建议尽快使用`;
          }
        } else if (item.sessionStatus === "ready") {
          stateTitle.textContent = "可以提取 hosted 短链";
          stateDetail.textContent = "默认使用 PH/PHP 双代理严格 0，也可切换标准直卡";
        } else {
          stateTitle.textContent = "等待有效 Session";
          stateDetail.textContent = "请先在账号管理页面获取 Session，再生成支付链接";
        }
        state.append(stateTitle, stateDetail);

        const controls = document.createElement("div");
        controls.className = "card-link-controls";
        const modeSelect = document.createElement("select");
        modeSelect.className = "card-link-mode";
        modeSelect.setAttribute("aria-label", `选择 ${item.email} 的提取方式`);
        for (const [value, label] of cardLinkExtractionModes) {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = label;
          modeSelect.append(option);
        }
        const storedStandardMode = `standard:${item.cardLinkCountry || "US"}`;
        modeSelect.value = item.cardLink && ["ph_hosted", "de_oaics_paypal"].includes(item.cardLinkMethod)
          ? item.cardLinkMethod
          : item.cardLink && cardLinkExtractionModes.some(([value]) => value === storedStandardMode)
          ? storedStandardMode
          : "ph_hosted";
        const generateButton = actionButton(
          modeSelect.value === "ph_hosted"
            ? item.cardLink ? "重新提取严格 0" : "提取严格 0"
            : item.cardLink ? "重新生成并复制" : "生成并复制",
          () => generateCardLink(item, modeSelect.value),
          "链接已生成并复制"
        );
        generateButton.classList.add("generate-link");
        generateButton.disabled = item.sessionStatus !== "ready";
        if (generateButton.disabled) generateButton.title = "请先获取有效 Session";
        modeSelect.addEventListener("change", () => {
          generateButton.textContent = modeSelect.value === "ph_hosted"
            ? item.cardLink ? "重新提取严格 0" : "提取严格 0"
            : item.cardLink ? "重新生成并复制" : "生成并复制";
        });
        controls.append(modeSelect, generateButton);
        if (item.cardLink) {
          controls.append(
            actionButton("复制链接", () => copyCardLink(item), "直卡链接已复制"),
            actionButton("打开支付页", () => openCardLink(item), "支付页已打开")
          );
        }
        row.append(identity, state, controls);
        root.append(row);
      }
    }

    function applyCardLinkFilters() {
      const query = $("cardLinkSearch").value.trim().toLowerCase();
      const status = $("cardLinkStatusFilter").value;
      const items = currentItems.filter((item) =>
        (!query || item.email.toLowerCase().includes(query)) &&
        (status === "all" ||
          (status === "generated" && Boolean(item.cardLink)) ||
          (status === "available" && item.sessionStatus === "ready") ||
          (status === "unavailable" && item.sessionStatus !== "ready"))
      );
      renderCardLinks(items);
      const generated = currentItems.filter((item) => item.cardLink).length;
      $("cardLinkSummary").textContent = items.length === currentItems.length
        ? `${currentItems.length} 个账号 · ${generated} 个链接已生成`
        : `显示 ${items.length} / ${currentItems.length} 个账号`;
    }

    function applyFilters() {
      const query = $("search").value.trim().toLowerCase();
      const plan = $("planFilter").value;
      const status = $("statusFilter").value;
      const date = $("dateFilter").value;
      const dateOrder = { today: 0, yesterday: 1, recent: 2, earlier: 3, unknown: 4 };
      const items = currentItems.filter((item) =>
        (!query || item.email.toLowerCase().includes(query)) &&
        (plan === "all" || item.accountType === plan) &&
        (status === "all" || item.sessionStatus === status) &&
        (date === "all" || dateCategory(item) === date)
      ).sort((left, right) => {
        const categoryDifference = dateOrder[dateCategory(left)] - dateOrder[dateCategory(right)];
        if (categoryDifference) return categoryDifference;
        return (itemCreatedDate(right)?.getTime() || 0) - (itemCreatedDate(left)?.getTime() || 0);
      });
      visibleItems = items;
      render(items);
      const ready = currentItems.filter((item) => item.sessionStatus === "ready").length;
      $("summary").textContent = items.length === currentItems.length
        ? `${currentItems.length} 个 iCloud 邮箱 · ${ready} 个 Session 有效`
        : `显示 ${items.length} / ${currentItems.length} 个邮箱`;
    }

    async function load() {
      $("refresh").disabled = true;
      $("cardLinkRefresh").disabled = true;
      $("summary").className = "";
      $("summary").textContent = "正在加载…";
      $("cardLinkSummary").className = "";
      $("cardLinkSummary").textContent = "正在加载…";
      try {
        const data = await api("/api/gpt-emails");
        currentItems = data.items;
        const plus = data.items.filter((item) => item.accountType === "plus").length;
        const free = data.items.filter((item) => item.accountType === "free").length;
        const payable = data.items.filter((item) => item.sessionStatus === "ready").length;
        const generated = data.items.filter((item) => item.cardLink).length;
        $("totalCount").textContent = data.items.length;
        $("plusCount").textContent = plus;
        $("freeCount").textContent = free;
        $("unverifiedCount").textContent = data.items.length - plus - free;
        $("payableCount").textContent = payable;
        $("generatedLinkCount").textContent = generated;
        $("cardLinkPendingCount").textContent = data.items.length - payable;
        $("accountNavCount").textContent = data.items.length;
        $("cardLinkNavCount").textContent = generated;
        applyFilters();
        applyCardLinkFilters();
      } catch (error) {
        currentItems = [];
        visibleItems = [];
        $("totalCount").textContent = "—";
        $("plusCount").textContent = "—";
        $("freeCount").textContent = "—";
        $("unverifiedCount").textContent = "—";
        $("payableCount").textContent = "—";
        $("generatedLinkCount").textContent = "—";
        $("cardLinkPendingCount").textContent = "—";
        $("accountNavCount").textContent = "—";
        $("cardLinkNavCount").textContent = "—";
        render([]);
        renderCardLinks([]);
        $("summary").className = "error";
        $("summary").textContent = error.message;
        $("cardLinkSummary").className = "error";
        $("cardLinkSummary").textContent = error.message;
      } finally {
        $("refresh").disabled = false;
        $("cardLinkRefresh").disabled = false;
      }
    }

    async function logout() {
      try { await api("/api/logout", { method: "POST" }); }
      finally { location.replace("/login"); }
    }

    $("refresh").addEventListener("click", load);
    $("cardLinkRefresh").addEventListener("click", load);
    $("search").addEventListener("input", applyFilters);
    $("planFilter").addEventListener("change", applyFilters);
    $("statusFilter").addEventListener("change", applyFilters);
    $("dateFilter").addEventListener("change", applyFilters);
    $("cardLinkSearch").addEventListener("input", applyCardLinkFilters);
    $("cardLinkStatusFilter").addEventListener("change", applyCardLinkFilters);
    for (const button of document.querySelectorAll(".nav-item[data-view]")) {
      button.addEventListener("click", () => setView(button.dataset.view));
    }
    window.addEventListener("hashchange", () => setView(location.hash.slice(1), false));
    $("registerOne").addEventListener("click", async () => {
      try { await startRegistration(); } catch (error) { showToast(error.message, "error"); }
    });
    $("verifyAll").addEventListener("click", async () => {
      try { await startVerification(); } catch (error) { showToast(error.message, "error"); }
    });
    $("stopVerify").addEventListener("click", async () => {
      try {
        const data = await api("/api/account-verification/stop", { method: "POST", body: "{}" });
        renderVerification(data.task);
      } catch (error) { showToast(error.message, "error"); }
    });
    $("fetchAll").addEventListener("click", async () => {
      const pending = currentItems.filter((item) => item.sessionStatus !== "ready").length;
      if (!pending) { showToast("全部邮箱的 Token 都仍有效"); return; }
      if (!confirm(`将为 ${pending} 个待处理 iCloud 邮箱启动 Camoufox，是否继续？`)) return;
      try { await startBrowser(); } catch (error) { showToast(error.message, "error"); }
    });
    $("stopTask").addEventListener("click", async () => {
      if (!confirm("停止当前注册或浏览器任务？")) return;
      try {
        if (registrationRunning) {
          const data = await api("/api/registration/stop", { method: "POST", body: "{}" });
          renderRegistration(data.task);
        } else {
          const data = await api("/api/browser/stop", { method: "POST", body: "{}" });
          renderTask(data.task);
        }
      } catch (error) { showToast(error.message, "error"); }
    });
    themeToggle.addEventListener("click", () => {
      applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
    });
    $("logout").addEventListener("click", logout);
    setView(location.hash.slice(1), false);
    Promise.all([load(), loadTask(), loadRegistration(), loadVerification()]);
  </script>
</body>
</html>
"""

# The structured frontend is assembled from package resources by a Page Builder.
# Keep the legacy constants above temporarily for compatibility with older tests
# and downstream imports while runtime traffic uses the new modular interface.
DESIGNED_LOGIN_HTML = build_login_page()
DESIGNED_INDEX_HTML = build_app_page()


def _error_reason(result: dict) -> str:
    error = result.get("error", {}) if result else {}
    if isinstance(error, dict):
        return str(error.get("errorMessage") or error.get("reason") or "iCloud 请求失败")
    return str(result.get("reason") or error or "iCloud 请求失败")


def _generation_failure_message(result: dict) -> str:
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        message = str(
            error.get("message")
            or error.get("errorMessage")
            or error.get("reason")
            or "iCloud 未确认地址创建"
        ).strip()
        code = error.get("code") or error.get("errorCode")
        retry_after = error.get("retry_after") or error.get("retryAfter")
    else:
        message = str(error or "iCloud 未确认地址创建").strip()
        code = None
        retry_after = None
    if message.casefold() == "generation failed":
        message = "iCloud 未确认地址创建"
    details = []
    if code is not None:
        details.append(f"错误码 {code}")
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        details.append(f"建议 {retry_after:g} 秒后重试")
    suffix = f"（{'；'.join(details)}）" if details else ""
    return f"iCloud 创建地址失败：{message[:300]}{suffix}"


def _resolve_data_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def _local_token_valid(request: web.Request, app: web.Application) -> bool:
    return hmac.compare_digest(
        request.headers.get("X-Local-Token", ""), app["local_token"]
    )


def _workbench_import_token_valid(
    request: web.Request, app: web.Application
) -> bool:
    configured = str(app.get("workbench_import_token") or "")
    supplied = str(request.headers.get("X-HME-Import-Token") or "")
    return bool(configured) and hmac.compare_digest(supplied, configured)


def _web_login_required(app: web.Application) -> bool:
    return bool(app.get("inventory_auth_enabled") or app.get("web_password"))


def _session_cookie_valid(request: web.Request) -> bool:
    supplied = request.cookies.get(SESSION_COOKIE_NAME, "")
    return bool(supplied) and hmac.compare_digest(
        supplied, request.app["session_token"]
    )


def _session_valid(request: web.Request) -> bool:
    return not _web_login_required(request.app) or _session_cookie_valid(request)


def _inventory_access_valid(
    request: web.Request, app: web.Application
) -> bool:
    if not app.get("inventory_auth_enabled"):
        return _workbench_import_token_valid(request, app)
    authorization = str(request.headers.get("Authorization") or "")
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return False
    now = time.monotonic()
    tokens: dict[str, float] = app["inventory_access_tokens"]
    for digest, expires_at in list(tokens.items()):
        if expires_at <= now:
            tokens.pop(digest, None)
    expires_at = tokens.get(access_token_digest(supplied))
    return bool(expires_at and expires_at > now)


@web.middleware
async def auth_middleware(
    request: web.Request, handler
) -> web.StreamResponse:
    if request.path in INVENTORY_INTEGRATION_PATHS:
        if _inventory_access_valid(request, request.app) or _session_cookie_valid(
            request
        ):
            return await handler(request)
        return web.json_response(
            {"ok": False, "error": "请先登录"},
            status=401,
            headers={"Cache-Control": "no-store"},
        )
    if (
        request.path in PUBLIC_PATHS
        or (
            request.path.startswith(PROTOCOL_CODE_PREFIX)
            and request.app.get("protocol_registration_manager") is not None
            and request.app["protocol_registration_manager"].valid_code_token(
                request.path.removeprefix(PROTOCOL_CODE_PREFIX)
            )
        )
        or (
            request.path == WORKBENCH_OPENAI_CODE_PATH
            and _workbench_import_token_valid(request, request.app)
        )
        or _local_token_valid(request, request.app)
        or _session_valid(request)
    ):
        return await handler(request)
    if request.path.startswith("/api/"):
        return web.json_response(
            {"ok": False, "error": "请先登录"},
            status=401,
            headers={"Cache-Control": "no-store"},
        )
    raise web.HTTPFound("/login")


def _inbox_error_message(error: Exception) -> str:
    if isinstance(error, InboxSyncDeferredError):
        return error.public_message
    message = str(error).lower()
    if "authentication" in message or "login" in message or "auth" in message:
        return "IMAP 登录失败，请确认已开启 IMAP，并使用授权码或应用专用密码"
    if isinstance(error, (TimeoutError, OSError)):
        return (
            "无法连接 IMAP 服务器，OpenAI 注册验证码无法收取（与 2FA 无关）；"
            "请检查主机、端口和网络"
        )
    return "同步邮箱失败，请检查 IMAP 设置"


def _code_items(db_file: Path, limit: int) -> list[dict]:
    conn = connect_db(str(db_file))
    try:
        rows = list_messages(conn, only_codes=True, limit=limit)
        return [
            {
                "receivedAt": str(row["received_at"] or ""),
                "hmeAddress": str(row["hme_address"] or ""),
                "sender": str(row["sender"] or ""),
                "subject": str(row["subject"] or ""),
                "code": str(row["code"] or ""),
            }
            for row in rows
        ]
    finally:
        conn.close()


def _relay_payload(sender: str) -> str:
    for address in sender.lower().split(","):
        local_part = address.strip().split("@", 1)[0]
        marker = "_openai_com_"
        if marker in local_part:
            suffix = local_part.split(marker, 1)[1]
            return re.sub(r"[^a-z0-9]", "", suffix)
    return ""


def _is_subsequence(value: str, container: str) -> bool:
    characters = iter(container)
    return bool(value) and all(character in characters for character in value)


def _match_relay_identity(sender: str, identities: list[dict]) -> dict | None:
    payload = _relay_payload(sender)
    if not payload:
        return None
    exact = [
        item
        for item in identities
        if str(item.get("anonymousId") or "").lower() in payload
    ]
    if len(exact) == 1:
        return exact[0]
    fuzzy = [
        item
        for item in identities
        if _is_subsequence(
            str(item.get("anonymousId") or "").lower(), payload
        )
    ]
    return fuzzy[0] if len(fuzzy) == 1 else None


def _gpt_email_items(db_file: Path, identities: list[dict]) -> list[dict]:
    identity_by_email = {
        str(item.get("hme") or "").lower(): item
        for item in identities
        if item.get("hme")
    }
    conn = connect_db(str(db_file))
    try:
        rows = conn.execute(
            """
            SELECT hme_address, sender, subject, received_at, created_at
            FROM messages
            WHERE lower(
                COALESCE(sender, '') || ' ' ||
                COALESCE(subject, '') || ' ' ||
                COALESCE(body_preview, '')
            ) LIKE '%chatgpt%'
               OR lower(
                COALESCE(sender, '') || ' ' ||
                COALESCE(subject, '') || ' ' ||
                COALESCE(body_preview, '')
            ) LIKE '%openai%'
            ORDER BY COALESCE(received_at, created_at) DESC
            """
        ).fetchall()
    finally:
        conn.close()

    results: dict[str, dict] = {}
    for row in rows:
        stored_email = str(row["hme_address"] or "").lower()
        identity = identity_by_email.get(stored_email)
        if identity is None:
            identity = _match_relay_identity(str(row["sender"] or ""), identities)
        if identity is None:
            continue
        email = str(identity.get("hme") or "").lower()
        if not email:
            continue
        activity = str(row["received_at"] or row["created_at"] or "")
        current = results.get(email)
        if current is None:
            results[email] = {
                "email": email,
                "latestSubject": str(row["subject"] or ""),
                "lastActivity": activity,
                "registeredAt": activity,
                "messageCount": 1,
            }
            continue
        current["messageCount"] += 1
        if activity and (
            not current["registeredAt"] or activity < current["registeredAt"]
        ):
            current["registeredAt"] = activity
    return sorted(
        results.values(), key=lambda item: item["lastActivity"], reverse=True
    )


def _gpt_credential(db_file: Path, email: str, kind: str) -> str:
    conn = connect_db(str(db_file))
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (f"gpt_account:{email.lower()}",),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return ""
    try:
        payload = json.loads(str(row["value"] or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    if kind == "password":
        if payload.get("password_confirmed") is False:
            return ""
        return str(payload.get("password") or "").strip()
    two_factor = payload.get("two_factor")
    if not isinstance(two_factor, dict):
        two_factor = {}
    if kind == "totp_secret":
        return str(two_factor.get("secret") or "").strip()
    if kind == "totp_code":
        secret = str(two_factor.get("secret") or "").strip()
        if not secret or not two_factor.get("enabled"):
            return ""
        try:
            return generate_totp(secret)
        except RuntimeError:
            return ""
    if kind == "access_token":
        return str(
            payload.get("access_token") or payload.get("accessToken") or ""
        ).strip()
    session = (
        payload.get("session")
        or payload.get("session_json")
        or payload.get("sessionJson")
        or ""
    )
    if isinstance(session, (dict, list)):
        return json.dumps(session, ensure_ascii=False, indent=2)
    return str(session).strip()


def _gpt_account_export(db_file: Path, email: str = "") -> list[str]:
    target = email.strip().lower()
    if target and (
        not target.endswith("@icloud.com") or "\n" in target or "\r" in target
    ):
        return []
    conn = connect_db(str(db_file))
    try:
        if target:
            rows = conn.execute(
                "SELECT key, value FROM settings WHERE key = ?",
                (f"gpt_account:{target}",),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT key, value
                FROM settings
                WHERE key LIKE 'gpt_account:%'
                ORDER BY key
                """
            ).fetchall()
    finally:
        conn.close()

    lines: list[str] = []
    for row in rows:
        account_email = str(row["key"] or "").split(":", 1)[-1].strip().lower()
        if (
            not account_email.endswith("@icloud.com")
            or "\n" in account_email
            or "\r" in account_email
        ):
            continue
        try:
            account = json.loads(str(row["value"] or ""))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(account, dict):
            continue
        if account.get("password_confirmed") is False:
            continue
        password = str(account.get("password") or "").strip()
        if not password:
            continue
        two_factor = account.get("two_factor")
        if not isinstance(two_factor, dict):
            two_factor = {}
        mfa_secret = str(two_factor.get("secret") or "").strip()
        password = password.replace("\r", "").replace("\n", "")
        mfa_secret = mfa_secret.replace("\r", "").replace("\n", "")
        lines.append(f"{account_email}----{password}----{mfa_secret}")
    return lines


def _valid_supported_account_email(email: str) -> bool:
    target = str(email or "").strip().lower()
    if len(target) > 320 or not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}"
        r"@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+",
        target,
    ):
        return False
    return target.rsplit("@", 1)[1] in {"icloud.com", "gmail.com"}


def _workbench_import_payload(record: dict, email: str) -> dict:
    target = str(email or "").strip().lower()
    if not _valid_supported_account_email(target):
        raise RuntimeError("邮箱地址无效")
    session = account_session(record)
    access_token = account_session_access_token(record)
    if not session or not access_token:
        raise RuntimeError("该账号尚未保存有效 Session，不能导入工作台")
    if not str(session.get("accessToken") or session.get("access_token") or "").strip():
        session = {**session, "accessToken": access_token}
    payload = {
        "email": target,
        "session": session,
    }
    password = str(record.get("password") or "")
    if password.strip() and record.get("password_confirmed") is not False:
        payload["password"] = password

    two_factor = record.get("two_factor")
    if isinstance(two_factor, dict) and two_factor.get("enabled"):
        totp_secret = str(two_factor.get("secret") or "").strip()
        if totp_secret:
            payload["totp_secret"] = totp_secret

    if str(record.get("account_type") or "").strip().lower() == "plus":
        payload.update({"account_type": "plus", "group": "Plus"})
    return payload


def _account_has_confirmed_password(record: dict) -> bool:
    return bool(str(record.get("password") or "")) and (
        record.get("password_confirmed") is not False
    )


def _account_card_link(record: dict) -> dict:
    value = record.get("card_link")
    if not isinstance(value, dict):
        return {}
    method = str(value.get("method") or "standard").strip().lower()
    if method not in CARD_LINK_METHODS:
        method = "standard"
    url = str(value.get("url") or "").strip()
    status = str(value.get("status") or "").strip().lower()
    if status == "cs_live" and method == "de_oaics_paypal":
        return {
            "url": "",
            "status": "cs_live",
            "method": method,
            "country": str(value.get("country") or "DE").strip().upper(),
            "currency": str(value.get("currency") or "EUR").strip().upper(),
            "generated_at": "",
            "checked_at": str(value.get("checked_at") or "").strip(),
        }
    if not _valid_card_link(url, method):
        return {}
    return {
        "url": url,
        "status": "generated",
        "method": method,
        "country": str(value.get("country") or "US").strip().upper(),
        "currency": str(value.get("currency") or "USD").strip().upper(),
        "generated_at": str(value.get("generated_at") or "").strip(),
        "checked_at": str(value.get("checked_at") or "").strip(),
        "payment_link_type": str(value.get("payment_link_type") or "").strip(),
        "checkout_ui_mode": str(value.get("checkout_ui_mode") or "").strip(),
        "amount": str(value.get("amount") or "").strip(),
        "amount_currency": str(value.get("amount_currency") or "").strip().upper(),
        "amount_verification": str(
            value.get("amount_verification") or ""
        ).strip(),
        "promotion_applied": bool(value.get("promotion_applied")),
        "promotion_strategy": str(value.get("promotion_strategy") or "").strip(),
    }


def _valid_card_link(value: str, method: str = "") -> bool:
    text = str(value or "").strip()
    normalized_method = str(method or "").strip().lower()
    chatgpt_match = re.fullmatch(
        r"https://chatgpt\.com/checkout/[A-Za-z0-9_-]+/(?:cs_|oaics_)[A-Za-z0-9_-]+",
        text,
    )
    if normalized_method and normalized_method != "de_oaics_paypal":
        return bool(chatgpt_match)
    try:
        parsed = urlsplit(text)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    paypal_approve = bool(
        parsed.scheme.lower() == "https"
        and (host == "paypal.com" or host.endswith(".paypal.com"))
        and parsed.path.rstrip("/").lower() == "/agreements/approve"
        and str(parse_qs(parsed.query).get("ba_token", [""])[0]).strip()
    )
    stripe_redirect = bool(
        parsed.scheme.lower() == "https"
        and host == "pm-redirects.stripe.com"
        and parsed.path not in {"", "/"}
    )
    provider_link = paypal_approve or stripe_redirect
    if normalized_method == "de_oaics_paypal":
        return provider_link
    return bool(chatgpt_match) or provider_link


def _normalize_card_link_proxy_url(value: str) -> str:
    text = str(value or "").strip().strip("\"'")
    if not text:
        return ""
    if len(text) > 1000 or any(character.isspace() for character in text):
        raise RuntimeError("提链代理格式无效")
    prefixed_four_part = re.match(
        r"^(?P<scheme>https?|socks4a?|socks5h?)://"
        r"(?P<host>\[[^\]]+\]|[^:/\s]+):(?P<port>\d+):"
        r"(?P<user>[^:\s]+):(?P<password>.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if prefixed_four_part:
        item = prefixed_four_part.groupdict()
        text = (
            f"{item['scheme'].lower()}://{item['user']}:{item['password']}"
            f"@{item['host']}:{item['port']}"
        )
    if "://" not in text:
        parts = text.split(":")
        if len(parts) >= 4 and parts[1].isdigit():
            host, port, username = parts[0], parts[1], parts[2]
            password = ":".join(parts[3:])
            text = f"http://{username}:{password}@{host}:{port}"
        else:
            text = f"http://{text}"
    try:
        parsed = urlsplit(text)
        _ = parsed.port
    except ValueError as error:
        raise RuntimeError("提链代理格式无效") from error
    scheme = {"socks": "socks5h"}.get(parsed.scheme.lower(), parsed.scheme.lower())
    if scheme not in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}:
        raise RuntimeError("提链代理协议不受支持")
    if not parsed.hostname or parsed.query or parsed.fragment:
        raise RuntimeError("提链代理格式无效")
    if parsed.path not in {"", "/"}:
        raise RuntimeError("提链代理不能包含路径")
    if scheme != parsed.scheme.lower():
        text = f"{scheme}://{text.split('://', 1)[1]}"
    return text


def _save_account_card_link(
    db_file: Path,
    email: str,
    *,
    url: str,
    country: str,
    currency: str,
    method: str = "standard",
    payment_link_type: str = "",
    checkout_ui_mode: str = "",
    amount: str = "",
    amount_currency: str = "",
    amount_verification: str = "",
    promotion_applied: bool = False,
    promotion_strategy: str = "",
    status: str = "generated",
) -> dict:
    target = str(email or "").strip().lower()
    record = load_account_record(db_file, target)
    if not record:
        raise RuntimeError("未找到账号记录")
    normalized_method = str(method or "standard").strip().lower()
    if normalized_method not in CARD_LINK_METHODS:
        raise RuntimeError("不支持该直卡提取方式")
    normalized_status = str(status or "generated").strip().lower()
    if normalized_status not in {"generated", "cs_live"}:
        raise RuntimeError("提链结果状态无效")
    if normalized_status == "cs_live" and normalized_method != "de_oaics_paypal":
        raise RuntimeError("cs_live 标记仅适用于 PayPal DE/EUR OAICS 模式")
    if normalized_status == "generated" and not _valid_card_link(
        url, normalized_method
    ):
        raise RuntimeError("支付链接格式无效")
    now = datetime.now(timezone.utc).isoformat()
    card_link = {
        "url": str(url).strip() if normalized_status == "generated" else "",
        "status": normalized_status,
        "method": normalized_method,
        "country": str(country or "US").strip().upper(),
        "currency": str(currency or "USD").strip().upper(),
        "generated_at": now if normalized_status == "generated" else "",
        "checked_at": now,
        "payment_link_type": str(payment_link_type or "").strip(),
        "checkout_ui_mode": str(checkout_ui_mode or "").strip(),
        "amount": str(amount or "").strip(),
        "amount_currency": str(amount_currency or "").strip().upper(),
        "amount_verification": str(amount_verification or "").strip(),
        "promotion_applied": bool(promotion_applied),
        "promotion_strategy": str(promotion_strategy or "").strip(),
    }
    record["card_link"] = card_link
    record["updated_at"] = now
    conn = connect_db(str(db_file))
    try:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (f"gpt_account:{target}", json.dumps(record, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()
    return card_link


def _save_registration_checkout_probe(
    db_file: Path,
    email: str,
    *,
    registration_environment: dict[str, Any],
    checkout_proxy: dict[str, Any],
    classification: str = "",
    error: str = "",
    attempt_count: int = 1,
    max_attempts: int = 1,
    attempt_errors: list[str] | None = None,
) -> dict[str, Any]:
    """Persist a comparison-safe Checkout classification without its ID."""

    target = str(email or "").strip().lower()
    record = load_account_record(db_file, target)
    if not record:
        raise RuntimeError("未找到账户记录")
    normalized = str(classification or "").strip().lower()
    if normalized not in {"oaics", "cs_live", "cs", "other"}:
        normalized = ""
    checked_at = datetime.now(timezone.utc).isoformat()
    registration_snapshot = dict(registration_environment or {})
    checkout_snapshot = dict(checkout_proxy or {})
    differences = {
        "proxy_mode_changed": str(registration_snapshot.get("proxy_mode") or "")
        != str(checkout_snapshot.get("proxy_mode") or ""),
        "proxy_country_changed": str(
            registration_snapshot.get("proxy_country") or ""
        ).upper()
        != str(checkout_snapshot.get("proxy_country") or "").upper(),
        "proxy_endpoint_changed": str(
            registration_snapshot.get("proxy_endpoint") or ""
        )
        != str(checkout_snapshot.get("proxy_endpoint") or ""),
        "exit_ip_changed": bool(
            registration_snapshot.get("exit_ip")
            and checkout_snapshot.get("exit_ip")
            and registration_snapshot.get("exit_ip")
            != checkout_snapshot.get("exit_ip")
        ),
    }
    probe = {
        "status": "verified" if normalized else "error",
        "checkout_id_type": normalized or "error",
        "is_oaics": normalized == "oaics",
        "method": "oaics_probe",
        "country": "DE",
        "currency": "EUR",
        "attempt_count": max(1, int(attempt_count or 1)),
        "max_attempts": max(1, int(max_attempts or 1)),
        "attempt_errors": [
            str(item or "").strip()[:300]
            for item in (attempt_errors or [])
            if str(item or "").strip()
        ],
        "checked_at": checked_at,
        "registration_marker": str(
            registration_snapshot.get("captured_at")
            or record.get("registration_completed_at")
            or ""
        ),
        "registration_environment": registration_snapshot,
        "checkout_proxy": checkout_snapshot,
        "differences": differences,
        "error": str(error or "").strip()[:500] if not normalized else "",
    }
    record["registration_environment"] = registration_snapshot
    record["registration_checkout_probe"] = probe
    record["updated_at"] = checked_at
    conn = connect_db(str(db_file))
    try:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (f"gpt_account:{target}", json.dumps(record, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()
    return probe


async def _run_card_link_bridge(
    *,
    target_project_dir: Path,
    python_executable: Path,
    bridge_file: Path,
    access_token: str,
    method: str,
    country: str,
    currency: str,
    locale: str,
    account_email: str = "",
    create_proxy_url: str = "",
    promotion_proxy_url: str = "",
) -> dict:
    token = str(access_token or "").strip()
    create_proxy = str(create_proxy_url or "").strip()
    promotion_proxy = str(promotion_proxy_url or "").strip()
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "HME_OPENAI_ACCESS_TOKEN": token,
            "HME_CARD_LINK_CREATE_PROXY_URL": create_proxy,
            "HME_CARD_LINK_PROMO_PROXY_URL": promotion_proxy,
        }
    )
    command = [
        str(python_executable),
        str(bridge_file),
        "--source-dir",
        str(target_project_dir),
        "--method",
        method,
        "--country",
        country,
        "--currency",
        currency,
        "--locale",
        locale,
        "--account-email",
        str(account_email or "").strip(),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(target_project_dir),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
        limit=1024 * 1024,
    )
    try:
        timeout = (
            240
            if method == "de_oaics_paypal"
            else 150
            if method == "ph_hosted"
            else 75
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError as error:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise RuntimeError("生成直卡支付链接超时，请稍后重试") from error

    event: dict = {}
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if not line.startswith(CARD_LINK_EVENT_PREFIX):
            continue
        try:
            candidate = json.loads(line[len(CARD_LINK_EVENT_PREFIX) :])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            event = candidate
    event_status = str(event.get("status") or "")
    if process.returncode != 0 or event_status not in {"success", "classified"}:
        detail = str(event.get("detail") or "").strip()
        if not detail:
            detail = stderr.decode("utf-8", errors="replace").strip()
        if token:
            detail = detail.replace(token, "[REDACTED]")
        for proxy in (create_proxy, promotion_proxy):
            if proxy:
                detail = detail.replace(proxy, "[REDACTED_PROXY]")
        raise RuntimeError((detail or "直卡支付链接生成失败")[:1000])
    if event_status == "classified":
        if method == "oaics_probe" and str(
            event.get("classification") or ""
        ) in {"oaics", "cs_live", "cs", "other"}:
            return event
        if (
            method == "de_oaics_paypal"
            and event.get("classification") == "cs_live"
        ):
            return event
        raise RuntimeError("生成器返回了不支持的 Checkout 分类")
    if not _valid_card_link(str(event.get("url") or ""), method):
        raise RuntimeError("生成器没有返回有效的支付链接")
    return event


def _remove_deleted_email_records(db_file: Path, email: str) -> None:
    target = email.strip().lower()
    now = datetime.now(timezone.utc).isoformat()
    conn = connect_db(str(db_file))
    try:
        conn.execute("DELETE FROM settings WHERE key = ?", (f"gpt_account:{target}",))
        conn.execute("DELETE FROM settings WHERE key = ?", (f"gpt_removed:{target}",))
        conn.execute(
            """
            UPDATE addresses
            SET state = 'trash', note = 'iCloud alias deleted', updated_at = ?
            WHERE lower(email) = ?
            """,
            (now, target),
        )
        conn.commit()
    finally:
        conn.close()


def _parse_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _effective_gpt_code_since(
    since: str, email: str, *task_snapshots: dict | None
) -> str:
    """Do not let an active login or verification reuse an earlier code."""
    target = email.strip().lower()
    requested_at = _parse_timestamp(since)
    effective_since = since
    for candidate in task_snapshots:
        snapshot = candidate if isinstance(candidate, dict) else {}
        if not snapshot.get("running"):
            continue
        active_emails = {
            str(item.get("email") or "").strip().lower()
            for item in snapshot.get("accounts", [])
            if isinstance(item, dict)
        }
        if target not in active_emails:
            continue
        task_started = str(snapshot.get("startedAt") or "").strip()
        task_started_at = _parse_timestamp(task_started)
        if task_started_at and (
            requested_at is None or task_started_at > requested_at
        ):
            requested_at = task_started_at
            effective_since = task_started
    return effective_since


def _latest_gpt_code(
    db_file: Path,
    email: str,
    identities: list[dict],
    since: str = "",
    *,
    consume: bool = False,
) -> dict | None:
    target = email.strip().lower()
    since_at = _parse_timestamp(since)
    conn = connect_db(str(db_file))
    try:
        consumed_message_id = 0
        cursor_key = f"{GPT_CODE_CURSOR_PREFIX}{target}"
        if consume:
            conn.execute("BEGIN IMMEDIATE")
            cursor_row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (cursor_key,)
            ).fetchone()
            try:
                consumed_message_id = int(cursor_row["value"] if cursor_row else 0)
            except (TypeError, ValueError):
                consumed_message_id = 0
        rows = conn.execute(
            """
            SELECT id, received_at, hme_address, sender, subject, code, body_preview
            FROM messages
            WHERE (
                lower(COALESCE(sender, '') || ' ' || COALESCE(subject, '') || ' ' || COALESCE(body_preview, '')) LIKE '%chatgpt%'
                OR lower(COALESCE(sender, '') || ' ' || COALESCE(subject, '') || ' ' || COALESCE(body_preview, '')) LIKE '%openai%'
              )
            ORDER BY COALESCE(received_at, created_at) DESC
            LIMIT 200
            """
        ).fetchall()
        for row in rows:
            message_id = int(row["id"])
            if consume and message_id <= consumed_message_id:
                continue
            received_at = str(row["received_at"] or "")
            received = _parse_timestamp(received_at)
            if since_at and (not received or received < since_at):
                continue
            code = str(row["code"] or "").strip()
            if not code:
                code = extract_verification_code(
                    str(row["subject"] or ""), str(row["body_preview"] or "")
                )
                if code:
                    conn.execute(
                        "UPDATE messages SET code = ? WHERE id = ?", (code, row["id"])
                    )
                    conn.commit()
            if not code:
                continue
            matched_email = str(row["hme_address"] or "").strip().lower()
            if matched_email != target:
                identity = _match_relay_identity(
                    str(row["sender"] or ""), identities
                )
                matched_email = str((identity or {}).get("hme") or "").lower()
            if matched_email == target:
                if consume:
                    conn.execute(
                        """
                        INSERT INTO settings(key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (cursor_key, str(message_id)),
                    )
                    conn.commit()
                return {"code": code, "receivedAt": received_at}
        return None
    finally:
        conn.close()


def _claim_single_unmapped_gpt_code(
    db_file: Path,
    email: str,
    since: str,
) -> dict | None:
    """Claim one unambiguous fresh relay message for the only waiting account."""

    target = email.strip().lower()
    since_at = _parse_timestamp(since)
    if not target or since_at is None:
        return None
    conn = connect_db(str(db_file))
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor_key = f"{GPT_CODE_CURSOR_PREFIX}{target}"
        cursor_row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (cursor_key,)
        ).fetchone()
        try:
            consumed_message_id = int(cursor_row["value"] if cursor_row else 0)
        except (TypeError, ValueError):
            consumed_message_id = 0
        rows = conn.execute(
            """
            SELECT id, received_at, code, subject, body_preview
            FROM messages
            WHERE id > ?
              AND COALESCE(hme_address, '') = ''
              AND code IS NOT NULL AND code != ''
              AND (
                lower(COALESCE(sender, '') || ' ' || COALESCE(subject, '') || ' ' || COALESCE(body_preview, '')) LIKE '%chatgpt%'
                OR lower(COALESCE(sender, '') || ' ' || COALESCE(subject, '') || ' ' || COALESCE(body_preview, '')) LIKE '%openai%'
              )
            ORDER BY COALESCE(received_at, created_at) DESC
            LIMIT 20
            """,
            (consumed_message_id,),
        ).fetchall()
        candidates = []
        for row in rows:
            received_at = str(row["received_at"] or "")
            received = _parse_timestamp(received_at)
            if received is None or received < since_at:
                continue
            code = re.sub(r"[^A-Za-z0-9]", "", str(row["code"] or ""))
            if 4 <= len(code) <= 10:
                candidates.append((row, code, received_at))
        if len(candidates) != 1:
            conn.commit()
            return None
        row, code, received_at = candidates[0]
        message_id = int(row["id"])
        conn.execute(
            "UPDATE messages SET hme_address = ? WHERE id = ? AND COALESCE(hme_address, '') = ''",
            (target, message_id),
        )
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (cursor_key, str(message_id)),
        )
        conn.commit()
        return {"code": code, "receivedAt": received_at}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _latest_code_for_email(
    db_file: Path, email: str, identities: list[dict]
) -> dict | None:
    """Return only the newest code that can be attributed to one exact alias."""

    target = email.strip().lower()
    conn = connect_db(str(db_file))
    try:
        rows = conn.execute(
            """
            SELECT id, received_at, hme_address, sender, subject, code, body_preview
            FROM messages
            ORDER BY COALESCE(received_at, created_at) DESC
            LIMIT 500
            """
        ).fetchall()
        for row in rows:
            matched_email = str(row["hme_address"] or "").strip().lower()
            if matched_email != target:
                identity = _match_relay_identity(
                    str(row["sender"] or ""), identities
                )
                matched_email = str((identity or {}).get("hme") or "").lower()
            if matched_email != target:
                continue
            code = str(row["code"] or "").strip()
            if not code:
                code = extract_verification_code(
                    str(row["subject"] or ""), str(row["body_preview"] or "")
                )
                if code:
                    conn.execute(
                        "UPDATE messages SET code = ? WHERE id = ?",
                        (code, row["id"]),
                    )
                    conn.commit()
            if not code:
                continue
            return {
                "code": code,
                "receivedAt": str(row["received_at"] or ""),
            }
        return None
    finally:
        conn.close()


def _browser_email_items(db_file: Path, identities: list[dict]) -> list[dict]:
    activity = {
        item["email"]: item for item in _gpt_email_items(db_file, identities)
    }
    identities_by_email = {
        str(identity.get("hme") or "").strip().lower(): identity
        for identity in identities
        if str(identity.get("hme") or "").strip()
    }
    conn = connect_db(str(db_file))
    try:
        rows = conn.execute(
            "SELECT key FROM settings WHERE key LIKE 'gpt_account:%'"
        ).fetchall()
    finally:
        conn.close()
    stored_emails = {
        str(row["key"] or "").removeprefix("gpt_account:").strip().lower()
        for row in rows
    }
    items: list[dict] = []
    for email in sorted(set(identities_by_email) | stored_emails):
        if not email:
            continue
        identity = identities_by_email.get(email, {})
        current = dict(
            activity.get(email)
            or {
                "email": email,
                "latestSubject": "",
                "lastActivity": "",
                "registeredAt": "",
                "messageCount": 0,
            }
        )
        account = load_account_record(db_file, email)
        session = account_session(account)
        access_token = account_session_access_token(account)
        card_link = _account_card_link(account)
        registration_environment = account.get("registration_environment")
        if not isinstance(registration_environment, dict):
            registration_environment = {}
        checkout_probe = account.get("registration_checkout_probe")
        if not isinstance(checkout_probe, dict):
            checkout_probe = {}
        checkout_proxy = checkout_probe.get("checkout_proxy")
        if not isinstance(checkout_proxy, dict):
            checkout_proxy = {}
        token_expired = bool(access_token) and (
            access_token_is_expired(access_token)
            or bool(account.get("session_invalid_at"))
        )
        account_type = str(account.get("account_type") or "").lower()
        if account_type not in {"plus", "free"}:
            account_type = "unverified"
        timestamp = identity.get("createTimestamp")
        created_at = str(account.get("created_at") or account.get("updated_at") or "")
        if isinstance(timestamp, (int, float)):
            created_at = datetime.fromtimestamp(
                timestamp / 1000, timezone.utc
            ).astimezone().isoformat()
        current.update(
            {
                "createdAt": created_at,
                "hasPassword": _account_has_confirmed_password(account),
                "passwordConfirmed": account.get("password_confirmed") is not False,
                "hasTwoFactor": bool(
                    isinstance(account.get("two_factor"), dict)
                    and account["two_factor"].get("enabled")
                ),
                "twoFactorStatus": str(
                    account.get("two_factor", {}).get("status") or ""
                )
                if isinstance(account.get("two_factor"), dict)
                else "",
                "hasSession": bool(session and access_token),
                "registrationComplete": bool(session and access_token),
                "protocolReady": bool(
                    _account_has_confirmed_password(account)
                    and isinstance(account.get("two_factor"), dict)
                    and account["two_factor"].get("enabled")
                    and session
                    and access_token
                    and not token_expired
                ),
                "sessionAcquisitionMethod": str(
                    account.get("session_acquisition_method") or ""
                ),
                "registrationMode": str(
                    registration_environment.get("registration_mode") or ""
                ),
                "registrationEmailType": str(
                    registration_environment.get("email_type") or ""
                ),
                "registrationProxyMode": str(
                    registration_environment.get("proxy_mode") or ""
                ),
                "registrationProxyCountry": str(
                    registration_environment.get("proxy_country") or ""
                ),
                "registrationProxyEndpoint": str(
                    registration_environment.get("proxy_endpoint") or ""
                ),
                "registrationProxyNode": str(
                    registration_environment.get("proxy_node") or ""
                ),
                "registrationExitIp": str(
                    registration_environment.get("exit_ip") or ""
                ),
                "registrationExitCountry": str(
                    registration_environment.get("exit_country") or ""
                ),
                "checkoutProbeStatus": str(checkout_probe.get("status") or ""),
                "checkoutIdType": str(
                    checkout_probe.get("checkout_id_type") or ""
                ),
                "checkoutIsOaics": bool(checkout_probe.get("is_oaics")),
                "checkoutProbeCheckedAt": str(
                    checkout_probe.get("checked_at") or ""
                ),
                "checkoutProbeError": str(checkout_probe.get("error") or ""),
                "checkoutProbeAttemptCount": max(
                    0, int(checkout_probe.get("attempt_count") or 0)
                ),
                "checkoutProbeMaxAttempts": max(
                    0, int(checkout_probe.get("max_attempts") or 0)
                ),
                "checkoutProxyMode": str(checkout_proxy.get("proxy_mode") or ""),
                "checkoutProxyCountry": str(
                    checkout_proxy.get("proxy_country") or ""
                ),
                "checkoutProxyEndpoint": str(
                    checkout_proxy.get("proxy_endpoint") or ""
                ),
                "checkoutExitIp": str(checkout_proxy.get("exit_ip") or ""),
                "checkoutExitCountry": str(
                    checkout_proxy.get("exit_country") or ""
                ),
                "hasCookies": bool(account_saved_cookies(account)),
                "hasImportableSession": bool(
                    session and access_token and not token_expired
                ),
                "tokenExpired": token_expired,
                "sessionStatus": (
                    "expired"
                    if token_expired
                    else "ready"
                    if session and access_token
                    else "pending"
                ),
                "accountType": account_type,
                "accountTypeSource": str(account.get("account_type_source") or ""),
                "verifiedAt": str(account.get("verified_at") or ""),
                "cardLink": str(card_link.get("url") or ""),
                "cardLinkStatus": str(
                    card_link.get("status")
                    or ("generated" if card_link.get("url") else "")
                ),
                "cardLinkMethod": str(card_link.get("method") or ""),
                "cardLinkCountry": str(card_link.get("country") or "US"),
                "cardLinkCurrency": str(card_link.get("currency") or "USD"),
                "cardLinkGeneratedAt": str(card_link.get("generated_at") or ""),
                "cardLinkCheckedAt": str(card_link.get("checked_at") or ""),
                "cardLinkPaymentType": str(
                    card_link.get("payment_link_type") or ""
                ),
                "cardLinkCheckoutUiMode": str(
                    card_link.get("checkout_ui_mode") or ""
                ),
                "cardLinkAmount": str(card_link.get("amount") or ""),
                "cardLinkAmountCurrency": str(
                    card_link.get("amount_currency") or ""
                ),
                "cardLinkAmountVerification": str(
                    card_link.get("amount_verification") or ""
                ),
                "cardLinkPromotionApplied": bool(
                    card_link.get("promotion_applied")
                ),
                "cardLinkPromotionStrategy": str(
                    card_link.get("promotion_strategy") or ""
                ),
            }
        )
        items.append(current)
    return sorted(
        items,
        key=lambda item: (
            item["sessionStatus"] == "ready",
            item.get("lastActivity") or item.get("createdAt") or "",
        ),
        reverse=True,
    )


def _chatgpt_proxy_status_reachable(status: int) -> bool:
    return int(status or 0) in {200, 403}


async def test_registration_proxy_connection(
    proxy_url: str,
    expected_country: str,
) -> dict[str, Any]:
    """Test the configured registration proxy without exposing its URL."""

    started = time.perf_counter()
    timeout = aiohttp.ClientTimeout(total=20, connect=10, sock_read=12)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        async with session.get(
            "https://www.cloudflare.com/cdn-cgi/trace",
            proxy=proxy_url or None,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as response:
            trace_text = await response.text()
            if response.status != 200:
                raise RuntimeError(f"出口检测 HTTP {response.status}")
        trace = {}
        for line in trace_text.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                trace[key.strip()] = value.strip()
        exit_ip = str(trace.get("ip") or "").strip()
        actual_country = str(trace.get("loc") or "").strip().upper()
        if not exit_ip or not actual_country:
            raise RuntimeError("出口检测未返回 IP 或国家")
        try:
            async with session.get(
                "https://chatgpt.com/api/auth/csrf",
                proxy=proxy_url or None,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as response:
                chatgpt_status = int(response.status)
                await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            chatgpt_status = 0
    latency_ms = max(1, int((time.perf_counter() - started) * 1000))
    expected = str(expected_country or "").strip().upper()
    country_matches = not expected or actual_country == expected
    # The real browser completes Cloudflare's JavaScript challenge.  This
    # lightweight aiohttp probe cannot do that, so ChatGPT commonly answers
    # 403 even when the proxy tunnel and destination are both reachable.  Keep
    # this aligned with the payment project's established proxy-health check.
    chatgpt_ok = _chatgpt_proxy_status_reachable(chatgpt_status)
    parts = [f"出口 {exit_ip}", actual_country, f"延迟 {latency_ms} ms"]
    if chatgpt_status == 200:
        parts.append("ChatGPT 可达 (HTTP 200)")
    elif chatgpt_status == 403:
        parts.append("ChatGPT 可达 (HTTP 403，等待浏览器验证)")
    else:
        parts.append(
            f"ChatGPT HTTP {chatgpt_status}" if chatgpt_status else "ChatGPT 连接失败"
        )
    if not country_matches:
        parts.append(f"要求 {expected}，实际 {actual_country}")
    return {
        "ok": bool(country_matches and chatgpt_ok),
        "exitIp": exit_ip,
        "country": actual_country,
        "expectedCountry": expected,
        "countryMatches": country_matches,
        "chatgptStatus": chatgpt_status,
        "latencyMs": latency_ms,
        "message": " · ".join(parts),
        "testedAt": datetime.now(timezone.utc).isoformat(),
    }


def _registration_proxy_test_error(error: Exception) -> str:
    """Return a useful proxy-test error without exposing proxy credentials."""

    if isinstance(error, asyncio.TimeoutError):
        return "代理连接超时"
    if isinstance(error, aiohttp.ClientHttpProxyError):
        return "代理网关拒绝 HTTPS 连接，请检查主机、端口或账号余额"
    if isinstance(error, aiohttp.ClientProxyConnectionError):
        return "无法连接代理网关，请检查主机和端口"
    if isinstance(error, aiohttp.ClientError):
        return f"代理连接异常 ({type(error).__name__})"
    if isinstance(error, RuntimeError):
        return str(error)
    return type(error).__name__


def create_app(
    *,
    base_dir: Path,
    cookie_file: str = "cookies.txt",
    output_file: str = "emails.txt",
    db_file: str = DEFAULT_DB_FILE,
    inbox_config_file: str = DEFAULT_INBOX_CONFIG_FILE,
    region: str = "china",
    web_username: str = "",
    web_password: str = "",
    target_project_dir: str = "",
    target_python: str = "",
    browser_service_url: str = "http://127.0.0.1:8765",
    force_browser_headless: bool = False,
    workbench_url: str = "",
    workbench_import_token: str = "",
    inventory_server_enabled: bool = False,
    inventory_service_url: str = "",
    inventory_service_token: str = "",
    inventory_service_username: str = "",
    inventory_service_password: str = "",
    inventory_lease_seconds: int = DEFAULT_LEASE_SECONDS,
    inventory_batch_size: int = DEFAULT_INVENTORY_BATCH_SIZE,
    inventory_interval_seconds: int = DEFAULT_INVENTORY_INTERVAL_SECONDS,
    inventory_sync_interval_seconds: float = INVENTORY_SYNC_INTERVAL_SECONDS,
    smsbower_api_key: str = "",
    smsbower_service: str = DEFAULT_SMSBOWER_SERVICE,
    smsbower_max_price: float = DEFAULT_SMSBOWER_MAX_PRICE,
    smsbower_api_base_url: str = SMSBOWER_API_BASE_URL,
    paypal_project_dir: str = "",
    paypal_python: str = "",
    paypal_port: int = 18097,
    deactivation_scan_interval_seconds: float = DEACTIVATION_SCAN_INTERVAL_SECONDS,
    auto_oaics_probe: bool = True,
) -> web.Application:
    app = web.Application(
        client_max_size=4 * 1024 * 1024, middlewares=[auth_middleware]
    )
    app["local_token"] = secrets.token_urlsafe(32)
    app["session_token"] = secrets.token_urlsafe(48)
    app["login_attempts"] = []
    app["inventory_login_attempts"] = []
    app["inventory_access_tokens"] = {}
    app["cookie_file"] = _resolve_data_path(base_dir, cookie_file)
    app["output_file"] = _resolve_data_path(base_dir, output_file)
    app["db_file"] = _resolve_data_path(base_dir, db_file)
    credential_store = InventoryCredentialStore(app["db_file"])
    if str(web_username or "").strip():
        if not web_password:
            raise ValueError("HIDEMYEMAIL_WEB_PASSWORD is required with WEB_USERNAME")
        credential_store.configure(web_username, web_password)
    app["inventory_credential_store"] = credential_store
    app["inventory_auth_enabled"] = credential_store.configured
    app["web_password"] = "" if app["inventory_auth_enabled"] else web_password
    app["inbox_config_file"] = _resolve_data_path(base_dir, inbox_config_file)
    app["region"] = region
    app["inbox_background_last_sync"] = ""
    app["inbox_background_error"] = ""
    app["inbox_on_demand_next_attempt"] = 0.0
    app["inbox_on_demand_failures"] = 0
    app["inbox_on_demand_retry_after"] = 0
    app["inbox_code_sync_task"] = None
    app["deactivation_scan_state"] = {
        "intervalSeconds": max(0.05, float(deactivation_scan_interval_seconds)),
        "status": "waiting",
        "lastScan": "",
        "nextScan": "",
        "error": "",
        "deletedCount": 0,
        "lastResult": {},
    }
    app["generate_lock"] = asyncio.Lock()
    app["delete_lock"] = asyncio.Lock()
    app["inbox_sync_lock"] = asyncio.Lock()
    app["identity_lock"] = asyncio.Lock()
    app["card_link_lock"] = asyncio.Lock()
    app["auto_oaics_probe"] = bool(auto_oaics_probe)
    app["auto_oaics_probe_sleep"] = asyncio.sleep
    app["workbench_url"] = str(workbench_url or "").strip().rstrip("/")
    app["workbench_import_token"] = str(workbench_import_token or "").strip()
    app["inventory_server_enabled"] = bool(inventory_server_enabled)
    app["inventory_lease_seconds"] = max(1, int(inventory_lease_seconds))
    app["inventory_client"] = RemoteRegistrationInventoryClient(
        service_url=inventory_service_url,
        token=inventory_service_token,
        username=inventory_service_username,
        password=inventory_service_password,
    )
    app["local_registration_leases"] = {}
    app["inventory_fallback_active"] = False
    app["inventory_sync_lock"] = asyncio.Lock()
    app["inventory_sync_interval_seconds"] = max(
        1.0, float(inventory_sync_interval_seconds)
    )
    app["inventory_initial_sync_complete"] = False
    app["inventory_sync_state"] = {
        "status": "waiting" if app["inventory_client"].configured else "disabled",
        "lastAttemptAt": "",
        "lastSuccessAt": "",
        "lastError": "",
        "records": 0,
        "addresses": 0,
        "accounts": 0,
        "batches": 0,
    }

    async def sync_inventory_to_remote(
        emails: list[str] | None = None,
    ) -> dict[str, Any]:
        client: RemoteRegistrationInventoryClient = app["inventory_client"]
        if not client.configured:
            return {"configured": False, "records": 0, "addresses": 0, "accounts": 0}
        async with app["inventory_sync_lock"]:
            state: dict[str, Any] = app["inventory_sync_state"]
            state.update(
                status="syncing",
                lastAttemptAt=datetime.now(timezone.utc).isoformat(),
                lastError="",
            )
            try:
                records = await asyncio.to_thread(
                    export_inventory_records,
                    app["db_file"],
                    emails=emails,
                )
                result = await client.sync_records(records)
            except Exception as error:
                state.update(status="error", lastError=str(error)[:1000])
                raise
            state.update(
                status="ready",
                lastSuccessAt=datetime.now(timezone.utc).isoformat(),
                lastError="",
                **result,
            )
            if emails is None:
                app["inventory_initial_sync_complete"] = True
            return {"configured": True, **result}

    async def sync_saved_account_to_remote(email: str) -> None:
        try:
            await sync_inventory_to_remote([str(email or "").strip().lower()])
        except RuntimeError:
            if not app["inventory_fallback_active"]:
                raise

    app["sync_inventory_to_remote"] = sync_inventory_to_remote
    app["registration_proxy_store"] = RegistrationProxyStore(app["db_file"])
    app["card_link_proxy_store"] = RegistrationProxyStore(
        app["db_file"], setting_key=CARD_LINK_PROXY_SETTING_KEY
    )
    app["roxy_registration_store"] = RoxyRegistrationStore(app["db_file"])
    app["registration_proxy_tester"] = test_registration_proxy_connection
    def create_protocol_registration_process() -> ProtocolRegistrationManager:
        return ProtocolRegistrationManager(
            base_dir=base_dir,
            db_file=app["db_file"],
            proxy_store=app["registration_proxy_store"],
        )

    app["protocol_registration_manager"] = ConcurrentProtocolRegistrationManager(
        process_factory=create_protocol_registration_process,
        on_account_saved=sync_saved_account_to_remote,
        max_processes=5,
    )
    app["smsbower_config_store"] = SMSBowerConfigStore(
        app["db_file"],
        api_key=smsbower_api_key,
        service=smsbower_service,
        max_price=smsbower_max_price,
    )
    app["smsbower_client"] = SMSBowerMailClient(
        app["smsbower_config_store"], base_url=smsbower_api_base_url
    )
    paypal_source = (
        Path(paypal_project_dir).resolve()
        if paypal_project_dir
        else (base_dir / "paypal-agreement-protocol").resolve()
    )
    app["paypal_service"] = PayPalProtocolService(
        project_dir=paypal_source,
        runtime_dir=_default_paypal_runtime_dir(),
        config_db_file=app["db_file"],
        port=max(1, min(int(paypal_port), 65535)),
        python_executable=Path(paypal_python) if paypal_python else None,
    )
    browser_source = (
        Path(target_project_dir).resolve()
        if target_project_dir
        else _default_openai_runtime_dir(base_dir)
    )
    app["browser_manager"] = BrowserTaskManager(
        target_project_dir=browser_source,
        service_url=browser_service_url,
        worker_token=app["local_token"],
        db_file=app["db_file"],
        python_executable=Path(target_python) if target_python else None,
        force_headless=force_browser_headless,
        registration_proxy_store=app["registration_proxy_store"],
        roxy_registration_store=app["roxy_registration_store"],
        on_account_saved=sync_saved_account_to_remote,
    )
    app["card_link_bridge_file"] = Path(__file__).with_name(
        "openai_card_link_bridge.py"
    ).resolve()

    async def inspect_proxy_environment(
        proxy_url: str, environment: dict[str, Any]
    ) -> dict[str, Any]:
        snapshot = dict(environment)
        try:
            tested = await app["registration_proxy_tester"](
                proxy_url, str(snapshot.get("proxy_country") or "")
            )
        except Exception as error:
            snapshot["exit_ip_status"] = "error"
            snapshot["exit_ip_error"] = _registration_proxy_test_error(error)[:300]
            return snapshot
        snapshot.update(
            exit_ip=str(tested.get("exitIp") or "").strip(),
            exit_country=str(tested.get("country") or "").strip().upper(),
            exit_ip_status="verified" if tested.get("exitIp") else "unknown",
            proxy_test_latency_ms=max(0, int(tested.get("latencyMs") or 0)),
        )
        return snapshot

    async def validate_saved_registration_checkout(
        email: str,
        *,
        force: bool = False,
    ) -> dict[str, Any] | None:
        target = str(email or "").strip().lower()
        if not app["auto_oaics_probe"]:
            await sync_saved_account_to_remote(target)
            return None
        record = await asyncio.to_thread(
            load_account_record, app["db_file"], target
        )
        registration_environment = record.get("registration_environment")
        if not isinstance(registration_environment, dict):
            await sync_saved_account_to_remote(target)
            return None
        marker = str(
            registration_environment.get("captured_at")
            or record.get("registration_completed_at")
            or ""
        )
        previous_probe = record.get("registration_checkout_probe")
        if (
            isinstance(previous_probe, dict)
            and marker
            and str(previous_probe.get("registration_marker") or "") == marker
            and str(previous_probe.get("status") or "") == "verified"
            and not force
        ):
            await sync_saved_account_to_remote(target)
            return dict(previous_probe)

        card_link_proxy_state = app["card_link_proxy_store"].public_state()
        card_link_countries = card_link_proxy_state.get("cardLinkCountries")
        if not isinstance(card_link_countries, dict):
            card_link_countries = {}
        card_link_modes = card_link_proxy_state.get("cardLinkModes")
        if not isinstance(card_link_modes, dict):
            card_link_modes = {}
        checkout_proxy_country = str(
            card_link_countries.get("de")
            or card_link_proxy_state.get("country")
            or "DE"
        ).strip().upper()
        checkout_proxy_mode = str(
            card_link_modes.get("de_oaics_paypal")
            or card_link_proxy_state.get("mode")
            or "dynamic"
        ).strip().lower()
        checkout_country_item = next(
            (
                item for item in card_link_proxy_state.get("countries", [])
                if isinstance(item, dict)
                and str(item.get("code") or "").strip().upper()
                == checkout_proxy_country
            ),
            {},
        )
        checkout_proxy_country_label = str(
            checkout_country_item.get("label") or checkout_proxy_country
        )

        checkout_environment: dict[str, Any] = {
            "registration_mode": "checkout_probe",
            "email_type": str(registration_environment.get("email_type") or ""),
            "proxy_enabled": True,
            "proxy_mode": checkout_proxy_mode,
            "proxy_country": checkout_proxy_country,
            "proxy_country_label": checkout_proxy_country_label,
            "proxy_role": "first_card_link_proxy",
            "proxy_endpoint": "",
            "proxy_node": "",
            "proxy_selector": "",
            "proxy_latency_ms": 0,
            "exit_ip": "",
            "exit_country": "",
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        access_token = account_session_access_token(record)
        registration_proxy_url = account_registration_proxy_url(record)
        max_probe_attempts = 3
        probe_retry_delays = (2, 5)
        attempt_count = 0
        attempt_errors: list[str] = []
        try:
            if not access_token:
                raise RuntimeError("账户没有可用 Access Token")
            if access_token_is_expired(access_token):
                raise RuntimeError("Access Token 已过期")
            browser_manager: BrowserTaskManager = app["browser_manager"]
            if not browser_manager.target_project_dir.is_dir():
                raise RuntimeError("OpenAI 支付运行目录不存在")
            if not browser_manager.python_executable.is_file():
                raise RuntimeError("OpenAI 支付运行环境不可用")
            registration_environment = await inspect_proxy_environment(
                registration_proxy_url, registration_environment
            )
            result: dict[str, Any] = {}
            for attempt in range(1, max_probe_attempts + 1):
                attempt_count = attempt
                checkout_proxy_url, checkout_proxy_state = await asyncio.to_thread(
                    app["card_link_proxy_store"].proxy_for_country,
                    checkout_proxy_country,
                    mode=checkout_proxy_mode,
                )
                if not checkout_proxy_url:
                    raise RuntimeError("配置的第一提链代理尚未配置")
                checkout_state = {
                    **checkout_proxy_state,
                    "mode": checkout_proxy_mode,
                    "country": checkout_proxy_country,
                    "countryLabel": checkout_proxy_country_label,
                }
                checkout_environment = build_registration_environment(
                    target,
                    registration_mode="checkout_probe",
                    proxy_url=checkout_proxy_url,
                    proxy_state=checkout_state,
                    proxy_mode=checkout_proxy_mode,
                    proxy_country=checkout_proxy_country,
                )
                checkout_environment["proxy_role"] = "first_card_link_proxy"
                checkout_environment = await inspect_proxy_environment(
                    checkout_proxy_url, checkout_environment
                )
                try:
                    async with app["card_link_lock"]:
                        result = await _run_card_link_bridge(
                            target_project_dir=browser_manager.target_project_dir,
                            python_executable=browser_manager.python_executable,
                            bridge_file=app["card_link_bridge_file"],
                            access_token=access_token,
                            method="oaics_probe",
                            country="DE",
                            currency="EUR",
                            locale="de-DE",
                            account_email=target,
                            create_proxy_url=checkout_proxy_url,
                        )
                    break
                except Exception as error:
                    attempt_errors.append(
                        _registration_proxy_test_error(error)[:300]
                    )
                    if attempt >= max_probe_attempts:
                        raise
                    await app["auto_oaics_probe_sleep"](
                        probe_retry_delays[attempt - 1]
                    )
            probe = await asyncio.to_thread(
                _save_registration_checkout_probe,
                app["db_file"],
                target,
                registration_environment=registration_environment,
                checkout_proxy=checkout_environment,
                classification=str(
                    result.get("checkout_id_type")
                    or result.get("classification")
                    or ""
                ),
                attempt_count=attempt_count,
                max_attempts=max_probe_attempts,
                attempt_errors=attempt_errors,
            )
        except Exception as error:
            probe = await asyncio.to_thread(
                _save_registration_checkout_probe,
                app["db_file"],
                target,
                registration_environment=registration_environment,
                checkout_proxy=checkout_environment,
                error=_registration_proxy_test_error(error),
                attempt_count=max(1, attempt_count),
                max_attempts=max_probe_attempts,
                attempt_errors=attempt_errors,
            )
        await sync_saved_account_to_remote(target)
        return probe

    app["browser_manager"].on_account_saved = validate_saved_registration_checkout
    app["protocol_registration_manager"].on_account_saved = (
        validate_saved_registration_checkout
    )
    app["validate_saved_registration_checkout"] = (
        validate_saved_registration_checkout
    )
    gpt_code_identity_cache: list[dict] = []
    gpt_code_identity_cache_at = 0.0

    async def icloud_identities() -> list[dict]:
        cookie_path: Path = app["cookie_file"]
        if not cookie_path.exists() or cookie_path.stat().st_size == 0:
            raise RuntimeError("iCloud Cookie 尚未准备好")
        async with app["identity_lock"]:
            async with RichHideMyEmail(
                cookie_file=str(cookie_path), region=app["region"]
            ) as hme:
                result = await hme.list_email()
        if not result or not result.get("success"):
            raise RuntimeError(_error_reason(result))
        return [
            row
            for row in result.get("result", {}).get("hmeEmails", [])
            if row.get("hme") and row.get("anonymousId")
        ]

    async def active_icloud_identities() -> list[dict]:
        return [row for row in await icloud_identities() if row.get("isActive")]

    async def delete_invalid_icloud_email(email: str, _reason: str) -> str:
        """Delete a confirmed-invalid alias and its local credentials."""
        target = str(email or "").strip().lower()
        async with app["delete_lock"]:
            identities = await icloud_identities()
            identity = next(
                (
                    item
                    for item in identities
                    if str(item.get("hme") or "").strip().lower() == target
                ),
                None,
            )
            if identity is None:
                await asyncio.to_thread(
                    _remove_deleted_email_records, app["db_file"], target
                )
                return "Apple 邮箱列表中已不存在，已清理本地账号凭据"

            anonymous_id = str(identity.get("anonymousId") or "").strip()
            cookie_path: Path = app["cookie_file"]
            async with app["identity_lock"]:
                async with RichHideMyEmail(
                    cookie_file=str(cookie_path), region=app["region"]
                ) as hme:
                    if identity.get("isActive"):
                        deactivated = await hme.deactivate_email(anonymous_id)
                        if not deactivated or not deactivated.get("success"):
                            raise RuntimeError(
                                f"停用邮箱失败：{_error_reason(deactivated)}"
                            )
                    deleted = await hme.delete_email(anonymous_id)

            await asyncio.to_thread(
                _remove_deleted_email_records, app["db_file"], target
            )
            if not deleted or not deleted.get("success"):
                return (
                    "邮箱已停用并清理本地凭据，但 Apple 永久删除未完成："
                    f"{_error_reason(deleted)}"
                )
            return "iCloud 邮箱及本地账号凭据已删除"

    app["deactivation_delete_account"] = delete_invalid_icloud_email

    app["verification_manager"] = AccountVerificationManager(
        target_project_dir=browser_source,
        db_file=app["db_file"],
        python_executable=app["browser_manager"].python_executable,
        code_service_url=browser_service_url,
        code_service_token=app["local_token"],
        browser_manager=app["browser_manager"],
        delete_invalid_email=delete_invalid_icloud_email,
    )

    async def acquire_registration_inventory_email(label: str) -> str:
        # The remote inventory database cannot see account records saved on
        # this workstation.  Consume only completed accounts; a password-only
        # record from a failed attempt stays retryable and reuses that password.
        #
        # The full local-to-remote sync is owned by
        # ``remote_inventory_sync_context``.  Waiting for that multi-batch sync
        # here used to block the start endpoint (and therefore the quick-flow
        # UI) for about a minute.  Lease immediately instead; the duplicate
        # check below still reconciles any completed local account safely.
        remote_error: RuntimeError | None = None
        try:
            for _attempt in range(100):
                email = await app["inventory_client"].acquire_email(label)
                if not email:
                    break
                leased_record = app["inventory_client"].leased_record(email)
                if leased_record is not None:
                    await asyncio.to_thread(
                        import_inventory_records,
                        app["db_file"],
                        [leased_record],
                    )
                record = await asyncio.to_thread(
                    load_account_record, app["db_file"], email
                )
                completed_locally = bool(
                    account_session_access_token(record)
                    or account_saved_cookies(record)
                    or account_session(record)
                )
                if not completed_locally:
                    return email
                await app["inventory_client"].complete_email(
                    email,
                    True,
                    "本地已有已完成的 OpenAI 账号，已从远端库存标记使用",
                )
            else:
                raise RuntimeError("远端邮箱库存连续返回已有账号，已停止领取")
        except RuntimeError as error:
            remote_error = error

        local_lease = await asyncio.to_thread(
            lease_generated_inventory_email,
            app["db_file"],
            client_id="local-registration-fallback",
            label=label,
            lease_seconds=app["inventory_lease_seconds"],
        )
        if local_lease:
            email = str(local_lease["email"]).strip().lower()
            app["local_registration_leases"][email] = local_lease
            app["inventory_fallback_active"] = True
            return email
        if remote_error is not None:
            raise RuntimeError(f"{remote_error}；本地生成库存为空") from remote_error
        return ""

    async def complete_registration_inventory_email(
        email: str, success: bool, message: str
    ) -> None:
        target = str(email or "").strip().lower()
        record = (
            await asyncio.to_thread(
                export_inventory_record, app["db_file"], email
            )
            if success
            else None
        )
        local_lease = app["local_registration_leases"].get(target)
        if local_lease is not None:
            result = await asyncio.to_thread(
                complete_generated_inventory_lease,
                app["db_file"],
                lease_id=local_lease["leaseId"],
                email=target,
                success=success,
                message=message,
                record=record,
            )
            if not result.get("ok"):
                raise RuntimeError(str(result.get("error") or "本地库存回执失败"))
            app["local_registration_leases"].pop(target, None)
            return
        await app["inventory_client"].complete_email(
            email, success, message, record=record
        )

    async def confirm_registration_email(email: str) -> None:
        if not str(email or "").strip().lower().endswith("@icloud.com"):
            raise RuntimeError("远端库存服务返回了无效的 iCloud 邮箱")

    async def save_registration_password(email: str, password: str) -> None:
        await asyncio.to_thread(
            _save_account_record,
            app["db_file"],
            email,
            password=password,
            password_confirmed=False,
        )

    async def load_registration_account(email: str) -> dict:
        return await asyncio.to_thread(load_account_record, app["db_file"], email)

    async def record_registration_failure(task: dict) -> None:
        process_id = str(task.get("processId") or task.get("id") or "").strip()
        if not process_id:
            return
        record = {
            "processId": process_id,
            "status": "failed",
            "provider": str(task.get("provider") or ""),
            "email": str(task.get("email") or "").strip().lower(),
            "emails": [
                str(item or "").strip().lower()
                for item in task.get("emails", [])
                if str(item or "").strip()
            ],
            "message": str(task.get("message") or "注册失败")[:1000],
            "currentStage": str(task.get("currentStage") or "failed"),
            "currentLocation": str(task.get("currentLocation") or "注册流程"),
            "currentAction": str(task.get("currentAction") or "注册失败")[:500],
            "startedAt": str(task.get("startedAt") or ""),
            "finishedAt": str(task.get("finishedAt") or ""),
            "recordedAt": str(task.get("recordedAt") or ""),
            "logs": [
                {
                    key: str(log.get(key) or "")[:1000]
                    for key in (
                        "at",
                        "message",
                        "stage",
                        "location",
                        "action",
                        "status",
                    )
                }
                for log in task.get("logs", [])[-30:]
                if isinstance(log, dict)
            ],
        }

        def save_failure() -> None:
            conn = connect_db(str(app["db_file"]))
            try:
                conn.execute(
                    """
                    INSERT INTO settings(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (
                        f"{REGISTRATION_PROCESS_FAILURE_PREFIX}{process_id}",
                        json.dumps(record, ensure_ascii=False),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

        await asyncio.to_thread(save_failure)

    def load_registration_failures(limit: int = 20) -> list[dict]:
        conn = connect_db(str(app["db_file"]))
        try:
            rows = conn.execute(
                """
                SELECT value
                FROM settings
                WHERE key LIKE ?
                ORDER BY rowid DESC
                LIMIT ?
                """,
                (f"{REGISTRATION_PROCESS_FAILURE_PREFIX}%", max(1, int(limit))),
            ).fetchall()
        finally:
            conn.close()
        records: list[dict] = []
        for row in rows:
            try:
                item = json.loads(str(row["value"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(item, dict):
                records.append(item)
        return records

    def create_registration_process() -> RegistrationTaskManager:
        primary_browser = app["browser_manager"]
        process_browser = BrowserTaskManager(
            target_project_dir=primary_browser.target_project_dir,
            service_url=primary_browser.service_url,
            worker_token=primary_browser.worker_token,
            db_file=primary_browser.db_file,
            python_executable=primary_browser.python_executable,
            bridge_file=primary_browser.bridge_file,
            force_headless=primary_browser.force_headless,
            registration_proxy_store=primary_browser.registration_proxy_store,
            roxy_registration_store=primary_browser.roxy_registration_store,
            on_account_saved=validate_saved_registration_checkout,
        )
        return RegistrationTaskManager(
            browser_manager=process_browser,
            acquire_email=acquire_registration_inventory_email,
            confirm_email=confirm_registration_email,
            save_password=save_registration_password,
            complete_email=complete_registration_inventory_email,
            acquire_provider_email=app["smsbower_client"].acquire_email,
            poll_provider_code=app["smsbower_client"].poll_code,
            poll_provider_next_code=app["smsbower_client"].poll_next_code,
            complete_provider_email=app["smsbower_client"].complete_email,
            cancel_provider_email=app["smsbower_client"].cancel_email,
            load_account=load_registration_account,
        )

    app["registration_manager"] = ConcurrentRegistrationTaskManager(
        process_factory=create_registration_process,
        shared_browser_manager=app["browser_manager"],
        acquire_email=acquire_registration_inventory_email,
        complete_email=complete_registration_inventory_email,
        record_failure=record_registration_failure,
        max_processes=10,
    )

    async def generate_inventory_batch(label: str, count: int) -> dict:
        """Generate aliases for inventory without starting any registration flow."""

        cookie_path: Path = app["cookie_file"]
        if not cookie_path.exists() or cookie_path.stat().st_size == 0:
            raise RuntimeError("Cookie 尚未准备好")
        async with app["generate_lock"]:
            account = await fetch_account_info(str(cookie_path), app["region"])
            if account.get("error"):
                raise RuntimeError("Cookie 已失效，请重新获取")
            return await _generate(
                label=label,
                count=count,
                cookie_file=str(cookie_path),
                output_file=str(app["output_file"]),
                region=app["region"],
                db_file=str(app["db_file"]),
            )

    app["scheduled_generation_manager"] = (
        ScheduledGenerationManager(
            db_file=app["db_file"],
            generate_batch=generate_inventory_batch,
            batch_size=inventory_batch_size,
            interval_seconds=inventory_interval_seconds,
        )
        if app["inventory_server_enabled"]
        else None
    )

    def reset_inbox_sync_backoff() -> None:
        app["inbox_on_demand_next_attempt"] = 0.0
        app["inbox_on_demand_failures"] = 0
        app["inbox_on_demand_retry_after"] = 0
        app["inbox_background_error"] = ""

    async def sync_inbox_on_demand(limit: int, *, force: bool = False) -> list[dict]:
        """Connect only for an active code request, with shared rate limiting."""

        config_path: Path = app["inbox_config_file"]
        if not config_path.exists():
            raise FileNotFoundError("请先配置接收邮箱")

        async with app["inbox_sync_lock"]:
            now = time.monotonic()
            if not force and now < app["inbox_on_demand_next_attempt"]:
                if app["inbox_background_error"]:
                    raise InboxSyncDeferredError(app["inbox_background_error"])
                return []

            config = load_config(str(config_path))
            try:
                inserted = await asyncio.to_thread(
                    sync_inbox, config, str(app["db_file"]), limit
                )
            except Exception as error:
                public_message = _inbox_error_message(error)
                failures = app["inbox_on_demand_failures"] + 1
                base_delay = (
                    ON_DEMAND_INBOX_AUTH_BACKOFF_SECONDS
                    if "IMAP 登录失败" in public_message
                    else ON_DEMAND_INBOX_FAILURE_BACKOFF_SECONDS
                )
                delay = min(
                    ON_DEMAND_INBOX_MAX_BACKOFF_SECONDS,
                    base_delay * (2 ** min(failures - 1, 6)),
                )
                app["inbox_on_demand_failures"] = failures
                app["inbox_on_demand_retry_after"] = delay
                app["inbox_on_demand_next_attempt"] = time.monotonic() + delay
                app["inbox_background_error"] = public_message
                raise

            reset_inbox_sync_backoff()
            app["inbox_on_demand_next_attempt"] = (
                time.monotonic() + ON_DEMAND_INBOX_SUCCESS_COOLDOWN_SECONDS
            )
            app["inbox_background_last_sync"] = (
                datetime.now().astimezone().isoformat()
            )
            return inserted

    def account_task_running() -> bool:
        for manager_name in (
            "registration_manager",
            "browser_manager",
            "verification_manager",
        ):
            try:
                if app[manager_name].snapshot().get("running"):
                    return True
            except (AttributeError, RuntimeError, TypeError):
                continue
        return False

    async def scan_deactivation_notices_once(
        *, force_sync: bool = True
    ) -> dict[str, object]:
        state: dict = app["deactivation_scan_state"]
        scanned_at = datetime.now().astimezone().isoformat()
        state.update(status="running", lastScan=scanned_at, error="")

        if account_task_running():
            result: dict[str, object] = {
                "status": "deferred",
                "message": "账号任务正在运行，停用通知删除已延期到下一轮",
                "notices": 0,
                "deleted": 0,
                "missing": 0,
                "failed": 0,
            }
            state.update(status="deferred", lastResult=result)
            return result

        try:
            await sync_inbox_on_demand(100, force=force_sync)
        except FileNotFoundError:
            result = {
                "status": "unconfigured",
                "message": "收件箱尚未配置",
                "notices": 0,
                "deleted": 0,
                "missing": 0,
                "failed": 0,
            }
            state.update(status="unconfigured", lastResult=result)
            return result
        except Exception as error:
            message = _inbox_error_message(error)
            result = {
                "status": "error",
                "message": message,
                "notices": 0,
                "deleted": 0,
                "missing": 0,
                "failed": 1,
            }
            state.update(status="error", error=message, lastResult=result)
            return result

        notices = await asyncio.to_thread(
            pending_deactivation_notices, app["db_file"]
        )
        deleted = 0
        missing = 0
        failed = 0
        errors: list[str] = []
        deferred = False
        for notice in notices:
            if account_task_running():
                deferred = True
                break
            email = str(notice["email"])
            account_record = await asyncio.to_thread(
                load_account_record, app["db_file"], email
            )
            if not account_record:
                await asyncio.to_thread(
                    mark_deactivation_notice_processed,
                    app["db_file"],
                    notice,
                    email=email,
                    status="account_missing",
                    detail="停用通知对应的本地账号已不存在",
                )
                missing += 1
                continue

            reason = "OpenAI 停用邮件确认该账号已无法使用"
            try:
                deletion_message = await app["deactivation_delete_account"](
                    email, reason
                )
                detail = f"{reason}；{deletion_message}"
                await asyncio.to_thread(
                    mark_deactivation_notice_processed,
                    app["db_file"],
                    notice,
                    email=email,
                    status="deleted",
                    detail=detail,
                    account_record=account_record,
                )
            except Exception as error:
                failed += 1
                errors.append(f"{email}: {error}")
                continue
            deleted += 1

        status = "deferred" if deferred else "error" if failed else "ok"
        message = (
            "账号任务开始运行，剩余停用通知已延期到下一轮"
            if deferred
            else "；".join(errors)[:1000]
            if errors
            else f"已检查 {len(notices)} 封停用通知，删除 {deleted} 个账号"
        )
        result = {
            "status": status,
            "message": message,
            "notices": len(notices),
            "deleted": deleted,
            "missing": missing,
            "failed": failed,
        }
        state["deletedCount"] += deleted
        state.update(
            status=status,
            error="；".join(errors)[:1000],
            lastResult=result,
        )
        return result

    app["deactivation_scan_once"] = scan_deactivation_notices_once

    async def paypal_service_context(_: web.Application):
        await app["paypal_service"].ensure_running()
        try:
            yield
        finally:
            await app["paypal_service"].close()

    app.cleanup_ctx.append(paypal_service_context)

    async def browser_manager_context(_: web.Application):
        try:
            yield
        finally:
            await app["browser_manager"].close()

    app.cleanup_ctx.append(browser_manager_context)

    async def verification_manager_context(_: web.Application):
        try:
            yield
        finally:
            await app["verification_manager"].close()

    app.cleanup_ctx.append(verification_manager_context)

    async def registration_manager_context(_: web.Application):
        try:
            yield
        finally:
            await app["registration_manager"].close()

    app.cleanup_ctx.append(registration_manager_context)

    async def protocol_registration_manager_context(_: web.Application):
        try:
            yield
        finally:
            await app["protocol_registration_manager"].close()

    app.cleanup_ctx.append(protocol_registration_manager_context)

    async def deactivation_scan_context(_: web.Application):
        state: dict = app["deactivation_scan_state"]

        async def scan_loop() -> None:
            first_scan = True
            while True:
                scan_task = asyncio.create_task(
                    scan_deactivation_notices_once(force_sync=not first_scan)
                )
                try:
                    await asyncio.shield(scan_task)
                except asyncio.CancelledError:
                    # asyncio.to_thread keeps running after its awaiting task is
                    # cancelled.  Wait for the in-flight scan so its SQLite
                    # connection is closed before application cleanup returns.
                    await scan_task
                    raise
                except Exception as error:
                    message = str(error)[:1000]
                    state.update(status="error", error=message)
                first_scan = False
                interval = float(state["intervalSeconds"])
                state["nextScan"] = datetime.fromtimestamp(
                    time.time() + interval
                ).astimezone().isoformat()
                await asyncio.sleep(interval)

        task = asyncio.create_task(scan_loop())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app.cleanup_ctx.append(deactivation_scan_context)

    if app["inventory_client"].configured:
        async def remote_inventory_sync_context(_: web.Application):
            async def sync_loop() -> None:
                while True:
                    try:
                        await sync_inventory_to_remote()
                    except Exception:
                        # The status endpoint exposes the last error.  Keep the
                        # local service usable and retry without losing data.
                        pass
                    await asyncio.sleep(app["inventory_sync_interval_seconds"])

            task = asyncio.create_task(sync_loop())
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        app.cleanup_ctx.append(remote_inventory_sync_context)

    if app["inventory_server_enabled"]:
        async def scheduled_generation_context(_: web.Application):
            await app["scheduled_generation_manager"].start()
            try:
                yield
            finally:
                await app["scheduled_generation_manager"].close()

        async def inventory_lease_cleanup_context(_: web.Application):
            async def cleanup_loop() -> None:
                while True:
                    await asyncio.to_thread(expire_inventory_leases, app["db_file"])
                    await asyncio.sleep(30)

            task = asyncio.create_task(cleanup_loop())
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        app.cleanup_ctx.append(scheduled_generation_context)
        app.cleanup_ctx.append(inventory_lease_cleanup_context)

    async def login_page(request: web.Request) -> web.Response:
        if not _web_login_required(app) or _session_valid(request):
            raise web.HTTPFound("/")
        return web.Response(
            text=DESIGNED_LOGIN_HTML,
            content_type="text/html",
            headers=PAGE_HEADERS,
        )

    async def access_page(request: web.Request) -> web.Response:
        supplied = str(request.query.get("token") or "")
        configured = str(app.get("workbench_import_token") or "")
        if not configured or not hmac.compare_digest(supplied, configured):
            raise web.HTTPNotFound()
        response = web.HTTPFound("/code")
        response.set_cookie(
            SESSION_COOKIE_NAME,
            app["session_token"],
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=request.headers.get("X-Forwarded-Proto") == "https",
            samesite="Strict",
            path="/",
        )
        response.headers.update(PAGE_HEADERS)
        return response

    async def code_page(_: web.Request) -> web.Response:
        return web.Response(
            text=CODE_PORTAL_HTML,
            content_type="text/html",
            headers=PAGE_HEADERS,
        )

    async def login_api(request: web.Request) -> web.Response:
        if not _web_login_required(app):
            return web.json_response({"ok": True})
        now = time.monotonic()
        attempts = [
            timestamp
            for timestamp in app["login_attempts"]
            if now - timestamp < 60
        ]
        app["login_attempts"] = attempts
        if len(attempts) >= 5:
            return web.json_response(
                {"ok": False, "error": "尝试次数过多，请一分钟后再试"},
                status=429,
                headers={"Cache-Control": "no-store"},
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        supplied_username = str(payload.get("username") or "").strip()
        supplied_password = str(payload.get("password") or "")
        if app["inventory_auth_enabled"]:
            valid = await asyncio.to_thread(
                app["inventory_credential_store"].verify,
                supplied_username,
                supplied_password,
            )
        else:
            valid = hmac.compare_digest(supplied_password, app["web_password"])
        if not valid:
            attempts.append(now)
            return web.json_response(
                {"ok": False, "error": "账号或密码错误"},
                status=401,
                headers={"Cache-Control": "no-store"},
            )
        app["login_attempts"] = []
        response = web.json_response(
            {"ok": True}, headers={"Cache-Control": "no-store"}
        )
        response.set_cookie(
            SESSION_COOKIE_NAME,
            app["session_token"],
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=request.headers.get("X-Forwarded-Proto") == "https",
            samesite="Strict",
            path="/",
        )
        return response

    async def inventory_login_api(request: web.Request) -> web.Response:
        if not app["inventory_server_enabled"]:
            raise web.HTTPNotFound()
        if not app["inventory_auth_enabled"]:
            return web.json_response(
                {"ok": False, "error": "服务器登录账号尚未配置"},
                status=503,
                headers={"Cache-Control": "no-store"},
            )
        now = time.monotonic()
        attempts = [
            timestamp
            for timestamp in app["inventory_login_attempts"]
            if now - timestamp < 60
        ]
        app["inventory_login_attempts"][:] = attempts
        if len(attempts) >= 5:
            return web.json_response(
                {"ok": False, "error": "尝试次数过多，请一分钟后再试"},
                status=429,
                headers={"Cache-Control": "no-store"},
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        valid = isinstance(payload, dict) and await asyncio.to_thread(
            app["inventory_credential_store"].verify,
            str(payload.get("username") or "").strip(),
            str(payload.get("password") or ""),
        )
        if not valid:
            attempts.append(now)
            return web.json_response(
                {"ok": False, "error": "账号或密码错误"},
                status=401,
                headers={"Cache-Control": "no-store"},
            )
        app["inventory_login_attempts"].clear()
        access_token = secrets.token_urlsafe(48)
        app["inventory_access_tokens"][access_token_digest(access_token)] = (
            now + SESSION_MAX_AGE
        )
        return web.json_response(
            {
                "ok": True,
                "accessToken": access_token,
                "expiresIn": SESSION_MAX_AGE,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def logout_api(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        response = web.json_response(
            {"ok": True}, headers={"Cache-Control": "no-store"}
        )
        response.del_cookie(SESSION_COOKIE_NAME, path="/")
        return response

    async def healthz(_: web.Request) -> web.Response:
        return web.Response(
            text="ok", headers={"Cache-Control": "no-store"}
        )

    async def index(_: web.Request) -> web.Response:
        body = DESIGNED_INDEX_HTML.replace(
            "__LOCAL_TOKEN__", json.dumps(app["local_token"])
        )
        return web.Response(
            text=body,
            content_type="text/html",
            headers=PAGE_HEADERS,
        )

    async def status(_: web.Request) -> web.Response:
        cookie_path: Path = app["cookie_file"]
        if not cookie_path.exists() or cookie_path.stat().st_size == 0:
            return web.json_response(
                {"ok": True, "authenticated": False, "error": "cookies.txt 不存在或为空"},
                headers={"Cache-Control": "no-store"},
            )
        account = await fetch_account_info(str(cookie_path), app["region"])
        error = account.get("error")
        return web.json_response(
            {
                "ok": True,
                "authenticated": not bool(error),
                "error": str(error) if error else "",
                "region": app["region"],
            },
            headers={"Cache-Control": "no-store"},
        )

    async def emails(_: web.Request) -> web.Response:
        cookie_path: Path = app["cookie_file"]
        if not cookie_path.exists() or cookie_path.stat().st_size == 0:
            return web.json_response({"ok": False, "error": "Cookie 尚未准备好"}, status=401)
        async with RichHideMyEmail(
            cookie_file=str(cookie_path), region=app["region"]
        ) as hme:
            result = await hme.list_email()
        if not result or not result.get("success"):
            return web.json_response(
                {"ok": False, "error": _error_reason(result)}, status=502
            )
        items = []
        for row in result.get("result", {}).get("hmeEmails", []):
            if not row.get("isActive"):
                continue
            timestamp = row.get("createTimestamp")
            created_at = ""
            if isinstance(timestamp, (int, float)):
                created_at = datetime.fromtimestamp(timestamp / 1000).astimezone().isoformat()
            items.append(
                {
                    "email": str(row.get("hme") or ""),
                    "label": str(row.get("label") or ""),
                    "createdAt": created_at,
                }
            )
        items.sort(key=lambda item: item["createdAt"], reverse=True)
        return web.json_response(
            {"ok": True, "items": items}, headers={"Cache-Control": "no-store"}
        )

    async def gpt_emails(_: web.Request) -> web.Response:
        identity_warning = ""
        try:
            identities = await active_icloud_identities()
        except RuntimeError as error:
            identities = []
            identity_warning = str(error)
        items = await asyncio.to_thread(
            _browser_email_items, app["db_file"], identities
        )
        return web.json_response(
            {
                "ok": True,
                "items": items,
                "identityWarning": identity_warning,
                "updatedAt": datetime.now().astimezone().isoformat(),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def account_type_update(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        email = str(payload.get("email") or "").strip().lower()
        account_type = str(payload.get("account_type") or "").strip().lower()
        if not email.endswith("@icloud.com") or len(email) > 320:
            return web.json_response(
                {"ok": False, "error": "邮箱地址无效"}, status=400
            )
        if account_type not in {"plus", "free", "unverified"}:
            return web.json_response(
                {"ok": False, "error": "账号类型无效"}, status=400
            )
        await asyncio.to_thread(
            set_manual_account_type, app["db_file"], email, account_type
        )
        return web.json_response(
            {"ok": True, "email": email, "accountType": account_type},
            headers={"Cache-Control": "no-store"},
        )

    async def gpt_credential(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        email = str(payload.get("email") or "").strip().lower()
        kind = str(payload.get("kind") or "").strip()
        if len(email) > 320 or not re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}"
            r"@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+",
            email,
        ):
            return web.json_response(
                {"ok": False, "error": "邮箱地址无效"}, status=400
            )
        if kind not in {
            "password",
            "access_token",
            "session",
            "totp_secret",
            "totp_code",
        }:
            return web.json_response(
                {"ok": False, "error": "凭据类型无效"}, status=400
            )
        value = await asyncio.to_thread(
            _gpt_credential, app["db_file"], email, kind
        )
        if not value:
            name = {
                "password": "密码",
                "access_token": "AT",
                "session": "Session",
                "totp_secret": "2FA 密钥",
                "totp_code": "2FA 动态码",
            }[kind]
            return web.json_response(
                {"ok": False, "error": f"当前邮箱暂无 {name} 数据"}, status=404
            )
        return web.json_response(
            {"ok": True, "value": value}, headers={"Cache-Control": "no-store"}
        )

    async def export_gpt_accounts(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, TypeError):
            payload = {}
        email = str(payload.get("email") or "").strip().lower()
        if not email.endswith("@icloud.com"):
            return web.json_response(
                {"ok": False, "error": "邮箱地址无效"}, status=400
            )
        lines = await asyncio.to_thread(_gpt_account_export, app["db_file"], email)
        if not lines:
            return web.json_response(
                {"ok": False, "error": "该账号尚未保存密码，无法下载"}, status=404
            )
        safe_name = re.sub(r"[^a-z0-9._-]+", "-", email.split("@", 1)[0])
        filename = (
            f"openai-account-{safe_name}-"
            f"{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}.txt"
        )
        return web.Response(
            text="\n".join(lines) + "\n",
            content_type="text/plain",
            charset="utf-8",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Account-Count": str(len(lines)),
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def delete_gpt_email(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        email = str(payload.get("email") or "").strip().lower()
        if not _valid_supported_account_email(email):
            return web.json_response(
                {"ok": False, "error": "邮箱地址无效"}, status=400
            )
        if any(
            manager.snapshot().get("running")
            for manager in (
                app["registration_manager"],
                app["browser_manager"],
                app["verification_manager"],
            )
        ):
            return web.json_response(
                {"ok": False, "error": "当前有账号任务运行，请停止或等待完成后再删除"},
                status=409,
            )

        async with app["delete_lock"]:
            if email.endswith("@gmail.com"):
                await app["smsbower_client"].forget_email(email)
                await asyncio.to_thread(
                    _remove_deleted_email_records, app["db_file"], email
                )
                return web.json_response(
                    {
                        "ok": True,
                        "deleted": True,
                        "deactivated": False,
                        "message": "Gmail 本地账号凭据及 SMSBower 激活记录已删除",
                    }
                )

            try:
                identities = await active_icloud_identities()
            except RuntimeError as error:
                return web.json_response(
                    {"ok": False, "error": str(error)}, status=502
                )
            identity = next(
                (
                    item
                    for item in identities
                    if str(item.get("hme") or "").strip().lower() == email
                ),
                None,
            )
            if identity is None:
                return web.json_response(
                    {"ok": False, "error": "邮箱不存在或已经停用"}, status=404
                )
            anonymous_id = str(identity.get("anonymousId") or "").strip()
            if not anonymous_id:
                return web.json_response(
                    {"ok": False, "error": "邮箱缺少 Apple 匿名标识，无法删除"},
                    status=409,
                )
            cookie_path: Path = app["cookie_file"]
            async with RichHideMyEmail(
                cookie_file=str(cookie_path), region=app["region"]
            ) as hme:
                deactivated = await hme.deactivate_email(anonymous_id)
                if not deactivated or not deactivated.get("success"):
                    return web.json_response(
                        {
                            "ok": False,
                            "error": f"停用邮箱失败：{_error_reason(deactivated)}",
                        },
                        status=502,
                    )
                deleted = await hme.delete_email(anonymous_id)

            await asyncio.to_thread(
                _remove_deleted_email_records, app["db_file"], email
            )
            if not deleted or not deleted.get("success"):
                return web.json_response(
                    {
                        "ok": True,
                        "deleted": False,
                        "deactivated": True,
                        "message": (
                            "邮箱已停用，但 Apple 未完成永久删除："
                            f"{_error_reason(deleted)}"
                        ),
                    }
                )
            return web.json_response(
                {
                    "ok": True,
                    "deleted": True,
                    "deactivated": True,
                    "message": "邮箱及本地账号凭据已删除",
                }
            )

    async def resolve_gpt_code(
        email: str, since: str
    ) -> tuple[dict | None, str, int]:
        nonlocal gpt_code_identity_cache, gpt_code_identity_cache_at
        if email.endswith("@gmail.com") and len(email) <= 320:
            try:
                code = await app["smsbower_client"].poll_next_code(email)
            except RuntimeError as error:
                message = str(error)
                if "激活已" in message:
                    status = 410
                elif "未保存 SMSBower mailId" in message:
                    status = 409
                else:
                    status = 502
                return None, message, status
            if not code:
                return None, "SMSBower Gmail 验证码尚未到达", 404
            return {
                "code": code,
                "receivedAt": datetime.now(timezone.utc).isoformat(),
            }, "", 200
        if not email.endswith("@icloud.com") or len(email) > 320:
            return None, "邮箱地址无效", 400
        config_path: Path = app["inbox_config_file"]
        if not config_path.exists():
            return None, "iCloud 收件箱尚未配置", 503

        since = _effective_gpt_code_since(
            since,
            email,
            app["browser_manager"].snapshot(),
            app["verification_manager"].snapshot(),
        )

        # Read the database first.  IMAP is opened only when an active code
        # request cannot be satisfied from messages already stored locally.
        item = await asyncio.to_thread(
            _latest_gpt_code,
            app["db_file"],
            email,
            gpt_code_identity_cache,
            since,
            consume=True,
        )
        if item:
            return item, "", 200

        try:
            await _wait_for_shared_inbox_code_sync(
                app,
                lambda: sync_inbox_on_demand(OPENAI_CODE_INBOX_SYNC_LIMIT),
            )
        except Exception as error:
            return None, _inbox_error_message(error), 502

        # Most forwarded messages expose the Hide My Email recipient directly.
        # Check those immediately after IMAP sync so an expired iCloud cookie
        # cannot hide a code that has already reached INBOX or Junk.
        item = await asyncio.to_thread(
            _latest_gpt_code,
            app["db_file"],
            email,
            gpt_code_identity_cache,
            since,
            consume=True,
        )
        if item:
            return item, "", 200

        # Some iCloud relays omit both the original recipient and a currently
        # resolvable relay identity.  When exactly one registration is waiting
        # and exactly one new OpenAI code arrived after it started, attribution
        # is unambiguous; bind that message to the waiting address atomically.
        registration_snapshot = app["registration_manager"].snapshot()
        waiting_emails = {
            str(value or "").strip().lower()
            for value in registration_snapshot.get("emails", [])
            if str(value or "").strip()
        }
        if not waiting_emails:
            waiting_email = str(registration_snapshot.get("email") or "").strip().lower()
            if waiting_email:
                waiting_emails.add(waiting_email)
        protocol_snapshot = app["protocol_registration_manager"].snapshot()
        protocol_waiting_email = str(
            protocol_snapshot.get("currentEmail") or ""
        ).strip().lower()
        if (
            protocol_snapshot.get("running")
            and protocol_snapshot.get("phase") == "email_verification"
            and protocol_waiting_email
        ):
            waiting_emails.add(protocol_waiting_email)
        if (
            (
                (
                    registration_snapshot.get("running")
                    and registration_snapshot.get("phase") == "email_verification"
                )
                or (
                    protocol_snapshot.get("running")
                    and protocol_snapshot.get("phase") == "email_verification"
                )
            )
            and waiting_emails == {email}
        ):
            item = await asyncio.to_thread(
                _claim_single_unmapped_gpt_code,
                app["db_file"],
                email,
                since,
            )
            if item:
                return item, "", 200

        if (
            not gpt_code_identity_cache
            or time.monotonic() - gpt_code_identity_cache_at > 120
        ):
            try:
                gpt_code_identity_cache = await active_icloud_identities()
                gpt_code_identity_cache_at = time.monotonic()
            except RuntimeError:
                # The iCloud web cookie is only needed for the legacy relay-
                # sender mapping fallback.  IMAP recipient parsing is the
                # primary code path, so an expired global session must not end
                # the 60-second code poll with an unrelated 502 toast.  Cache
                # the empty fallback briefly and let the caller continue
                # polling for a directly attributable message.
                gpt_code_identity_cache = []
                gpt_code_identity_cache_at = time.monotonic()
        item = await asyncio.to_thread(
            _latest_gpt_code,
            app["db_file"],
            email,
            gpt_code_identity_cache,
            since,
            consume=True,
        )
        if not item:
            return None, "暂未获取到该邮箱的 OpenAI 验证码", 404
        return item, "", 200

    async def openai_code_payload(
        request: web.Request,
    ) -> tuple[dict | None, web.Response | None]:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return None, web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        if not isinstance(payload, dict):
            return None, web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        return payload, None

    async def gpt_code(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        payload, error_response = await openai_code_payload(request)
        if error_response is not None:
            return error_response
        email = str(payload.get("email") or "").strip().lower()
        since = str(payload.get("since") or "").strip()
        item, error, status = await resolve_gpt_code(email, since)
        if not item:
            return web.json_response(
                {"ok": False, "error": error}, status=status
            )
        return web.json_response(
            {"ok": True, **item}, headers={"Cache-Control": "no-store"}
        )

    async def workbench_openai_code(request: web.Request) -> web.Response:
        if not _workbench_import_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "工作台认证失败"}, status=401
            )
        payload, error_response = await openai_code_payload(request)
        if error_response is not None:
            return error_response
        email = str(payload.get("email") or "").strip().lower()
        since = str(payload.get("since") or "").strip()
        item, error, status = await resolve_gpt_code(email, since)
        if not item:
            return web.json_response(
                {"ok": False, "error": error}, status=status
            )
        return web.json_response(
            {"ok": True, **item}, headers={"Cache-Control": "no-store"}
        )

    async def latest_code(request: web.Request) -> web.Response:
        payload, error_response = await openai_code_payload(request)
        if error_response is not None:
            return error_response
        email = str(payload.get("email") or "").strip().lower()
        if not email.endswith("@icloud.com") or len(email) > 320:
            return web.json_response(
                {"ok": False, "error": "请输入有效的 iCloud 子邮箱"}, status=400
            )
        config_path: Path = app["inbox_config_file"]
        if not config_path.exists():
            return web.json_response(
                {"ok": False, "error": "iCloud 收件箱尚未配置"}, status=503
            )
        try:
            # A verification code is a newly arrived message.  Scanning 100
            # UIDs also backfills old incomplete rows and can hold the public
            # lookup open for minutes on iCloud; the newest few messages are
            # sufficient and match the authenticated OpenAI-code endpoint.
            await sync_inbox_on_demand(OPENAI_CODE_INBOX_SYNC_LIMIT)
        except Exception as error:
            return web.json_response(
                {"ok": False, "error": _inbox_error_message(error)}, status=502
            )
        # Prefer an exact recipient parsed from the IMAP message.  This path
        # remains available when the iCloud web cookie used to list aliases has
        # expired; possession of the exact alias is already the portal's lookup
        # credential.
        item = await asyncio.to_thread(
            _latest_code_for_email, app["db_file"], email, []
        )
        if item:
            return web.json_response(
                {"ok": True, "email": email, **item},
                headers={"Cache-Control": "no-store"},
            )

        # Older relay rows may only be attributable through the iCloud identity
        # list.  Use that fallback only when no direct message match exists.
        try:
            identities = await active_icloud_identities()
        except RuntimeError as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=502
            )
        if not any(
            str(identity.get("hme") or "").strip().lower() == email
            for identity in identities
        ):
            return web.json_response(
                {"ok": False, "error": "未找到这个 iCloud 子邮箱"}, status=404
            )
        item = await asyncio.to_thread(
            _latest_code_for_email, app["db_file"], email, identities
        )
        if not item:
            return web.json_response(
                {"ok": False, "error": "该子邮箱暂未收到验证码"}, status=404
            )
        return web.json_response(
            {"ok": True, "email": email, **item},
            headers={"Cache-Control": "no-store"},
        )

    async def import_workbench_account(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            body = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        email = str(body.get("email") or "").strip().lower()
        if not _valid_supported_account_email(email):
            return web.json_response(
                {"ok": False, "error": "邮箱地址无效"}, status=400
            )
        if not app["workbench_url"] or not app["workbench_import_token"]:
            return web.json_response(
                {"ok": False, "error": "OpenAI 账户工作台导入尚未配置"},
                status=503,
            )
        record = await asyncio.to_thread(
            load_account_record, app["db_file"], email
        )
        try:
            payload = _workbench_import_payload(record, email)
        except RuntimeError as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=409
            )

        target = f'{app["workbench_url"]}/api/integrations/hidemyemail/import'
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.post(
                    target,
                    json=payload,
                    headers={
                        "X-HME-Import-Token": app["workbench_import_token"],
                    },
                ) as response:
                    result = await response.json(content_type=None)
                    if response.status >= 400 or not result.get("success"):
                        message = str(result.get("error") or "工作台拒绝导入")
                        raise RuntimeError(message)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as error:
            return web.json_response(
                {"ok": False, "error": f"导入工作台失败：{error}"}, status=502
            )
        imported_account = (
            result.get("account") if isinstance(result.get("account"), dict) else {}
        )
        imported_type = str(imported_account.get("accountType") or "").lower()
        imported_group = str(imported_account.get("group") or "")
        return web.json_response(
            {
                "ok": True,
                "imported": int(result.get("imported") or 0),
                "updated": int(result.get("updated") or 0),
                "accountType": imported_type,
                "group": imported_group,
                "hasPassword": "password" in payload,
                "hasTwoFactor": "totp_secret" in payload,
                "message": (
                    "Plus 账号已导入 OpenAI 账户工作台 Plus 分组"
                    if imported_type == "plus" and imported_group == "Plus"
                    else "账号凭据已安全导入 OpenAI 账户工作台"
                ),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def create_card_link(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            body = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        email = str(body.get("email") or "").strip().lower()
        method = str(body.get("method") or "standard").strip().lower()
        force_retry = body.get("force_retry") is True
        try:
            attempt_limit = int(body.get("attempt_limit") or 1)
        except (TypeError, ValueError):
            return web.json_response(
                {"ok": False, "error": "提链次数必须是 1–100 的整数"},
                status=400,
            )
        if isinstance(body.get("attempt_limit"), bool) or not 1 <= attempt_limit <= 100:
            return web.json_response(
                {"ok": False, "error": "提链次数必须是 1–100 的整数"},
                status=400,
            )
        reuse_registration_proxy = body.get("reuse_registration_proxy") is True
        independent_proxy_pair = body.get("independent_proxy_pair") is True
        use_secondary_proxy = body.get("use_secondary_proxy") is True
        promotion_proxy_choice = str(
            body.get("promotion_proxy_choice") or "first"
        ).strip().lower()
        if promotion_proxy_choice not in {"first", "second"}:
            return web.json_response(
                {"ok": False, "error": "优惠更新 IP 只能选择第一 IP 或第二 IP"},
                status=400,
            )
        if not email.endswith("@icloud.com") or len(email) > 320:
            return web.json_response(
                {"ok": False, "error": "邮箱地址无效"}, status=400
            )
        if method not in CARD_LINK_METHODS:
            return web.json_response(
                {"ok": False, "error": "不支持该直卡提取方式"}, status=400
            )
        if method == "ph_hosted":
            country = "PH"
            region = {"currency": "PHP", "locale": "en-US"}
        elif method == "de_oaics_paypal":
            country = "DE"
            region = {"currency": "EUR", "locale": "de-DE"}
        else:
            country = str(body.get("country") or "US").strip().upper()
            region = CARD_LINK_REGIONS.get(country)
            if region is None:
                return web.json_response(
                    {"ok": False, "error": "不支持该直卡支付地区"},
                    status=400,
                )
        record = await asyncio.to_thread(
            load_account_record, app["db_file"], email
        )
        existing_card_link = (
            record.get("card_link") if isinstance(record, dict) else {}
        )
        if not isinstance(existing_card_link, dict):
            existing_card_link = {}
        if (
            method == "de_oaics_paypal"
            and existing_card_link.get("method") == method
            and existing_card_link.get("status") == "cs_live"
            and not force_retry
            and attempt_limit == 1
        ):
            return web.json_response(
                {
                    "ok": True,
                    "email": email,
                    "skipped": True,
                    "cardLinkStatus": "cs_live",
                    **existing_card_link,
                },
                headers={"Cache-Control": "no-store"},
            )
        proxy_store = (
            app["registration_proxy_store"]
            if reuse_registration_proxy
            else app["card_link_proxy_store"]
        )
        create_proxy_country = str(
            body.get("create_proxy_country") or ""
        ).strip().upper()
        promotion_proxy_country = str(
            body.get("promotion_proxy_country") or ""
        ).strip().upper()
        registration_environment = (
            record.get("registration_environment") if isinstance(record, dict) else {}
        )
        if not isinstance(registration_environment, dict):
            registration_environment = {}
        secondary_proxy_country = str(
            body.get("secondary_proxy_country")
            or create_proxy_country
            or registration_environment.get("proxy_country")
            or ""
        ).strip().upper()
        proxy_mode = str(body.get("proxy_mode") or "").strip().lower()

        async def configured_proxy_for_country(
            selected_country: str, legacy_value: object
        ) -> str:
            if selected_country:
                try:
                    proxy_url, _ = await asyncio.to_thread(
                        proxy_store.proxy_for_country,
                        selected_country,
                        mode=proxy_mode,
                    )
                except ValueError as error:
                    raise RuntimeError(str(error)) from error
                if not proxy_url:
                    raise RuntimeError("请先在“代理与线路”中保存代理配置")
                return proxy_url
            return _normalize_card_link_proxy_url(str(legacy_value or ""))

        retry_create_proxy = ""
        if method == "standard":
            create_proxy = ""
            promotion_proxy = ""
        else:
            try:
                if not reuse_registration_proxy and independent_proxy_pair:
                    first_proxy = await configured_proxy_for_country(
                        create_proxy_country,
                        body.get("create_proxy"),
                    )
                    needs_second_proxy = (
                        use_secondary_proxy
                        or promotion_proxy_choice == "second"
                        or attempt_limit > 1
                    )
                    second_proxy = ""
                    if needs_second_proxy:
                        if proxy_mode == "clash":
                            second_proxy, _ = await asyncio.to_thread(
                                proxy_store.next_proxy, force=True
                            )
                        else:
                            second_proxy = await configured_proxy_for_country(
                                secondary_proxy_country,
                                body.get("secondary_proxy"),
                            )
                        if not second_proxy:
                            raise RuntimeError("第二提链代理出口尚未配置")
                    create_proxy = second_proxy if use_secondary_proxy else first_proxy
                    retry_create_proxy = second_proxy or create_proxy
                    promotion_proxy = (
                        second_proxy
                        if promotion_proxy_choice == "second"
                        else first_proxy
                    )
                elif not reuse_registration_proxy:
                    create_proxy = await configured_proxy_for_country(
                        create_proxy_country,
                        body.get("create_proxy"),
                    )
                    promotion_proxy = (
                        ""
                        if method == "de_oaics_paypal"
                        else await configured_proxy_for_country(
                            promotion_proxy_country,
                            body.get("promotion_proxy"),
                        )
                    )
                    retry_create_proxy = create_proxy
                else:
                    first_proxy = account_registration_proxy_url(record)
                    if not first_proxy and proxy_mode:
                        first_proxy = await configured_proxy_for_country(
                            create_proxy_country or secondary_proxy_country,
                            body.get("create_proxy"),
                        )
                    needs_second_proxy = (
                        use_secondary_proxy
                        or promotion_proxy_choice == "second"
                        or attempt_limit > 1
                    )
                    second_proxy = ""
                    if needs_second_proxy:
                        if proxy_mode == "clash":
                            second_proxy, _ = await asyncio.to_thread(
                                proxy_store.next_proxy, force=True
                            )
                        else:
                            second_proxy = await configured_proxy_for_country(
                                secondary_proxy_country,
                                body.get("secondary_proxy"),
                            )
                        if not second_proxy:
                            raise RuntimeError("第二 IP 尚未配置")
                    create_proxy = second_proxy if use_secondary_proxy else first_proxy
                    retry_create_proxy = second_proxy or create_proxy
                    promotion_proxy = (
                        second_proxy
                        if promotion_proxy_choice == "second"
                        else first_proxy
                    )
            except RuntimeError as error:
                return web.json_response(
                    {"ok": False, "error": str(error)}, status=400
                )
        session = account_session(record)
        access_token = account_session_access_token(record)
        if not session or not access_token:
            return web.json_response(
                {"ok": False, "error": "该账号尚未保存 Session / AT"},
                status=409,
            )
        if access_token_is_expired(access_token):
            return web.json_response(
                {"ok": False, "error": "Access Token 已过期，请先重新获取 Session"},
                status=409,
            )
        browser_manager: BrowserTaskManager = app["browser_manager"]
        if not browser_manager.target_project_dir.is_dir():
            return web.json_response(
                {"ok": False, "error": "OpenAI 支付运行目录不存在"}, status=503
            )
        if not browser_manager.python_executable.is_file():
            return web.json_response(
                {"ok": False, "error": "OpenAI 支付运行环境不可用"}, status=503
            )
        try:
            async with app["card_link_lock"]:
                result: dict[str, Any] = {}
                attempt_count = 0
                for attempt_count in range(1, attempt_limit + 1):
                    result = await _run_card_link_bridge(
                        target_project_dir=browser_manager.target_project_dir,
                        python_executable=browser_manager.python_executable,
                        bridge_file=app["card_link_bridge_file"],
                        access_token=access_token,
                        method=method,
                        country=country,
                        currency=str(region["currency"]),
                        locale=str(region["locale"]),
                        account_email=email,
                        create_proxy_url=(
                            retry_create_proxy
                            if attempt_count > 1 and retry_create_proxy
                            else create_proxy
                        ),
                        promotion_proxy_url=promotion_proxy,
                    )
                    if not (
                        method == "de_oaics_paypal"
                        and result.get("classification") == "cs_live"
                    ):
                        break
                saved = await asyncio.to_thread(
                    _save_account_card_link,
                    app["db_file"],
                    email,
                    url=str(result.get("url") or ""),
                    country=str(result.get("country") or country),
                    currency=str(result.get("currency") or region["currency"]),
                    method=str(result.get("method") or method),
                    payment_link_type=str(
                        result.get("payment_link_type") or ""
                    ),
                    checkout_ui_mode=str(
                        result.get("checkout_ui_mode") or ""
                    ),
                    amount=str(result.get("amount") or ""),
                    amount_currency=str(
                        result.get("amount_currency") or ""
                    ),
                    amount_verification=str(
                        result.get("amount_verification") or ""
                    ),
                    promotion_applied=bool(
                        result.get("promotion_applied")
                    ),
                    promotion_strategy=str(
                        result.get("promotion_strategy") or ""
                    ),
                    status=(
                        "cs_live"
                        if result.get("classification") == "cs_live"
                        else "generated"
                    ),
                )
        except RuntimeError as error:
            return web.json_response(
                {"ok": False, "error": f"生成直卡支付链接失败：{error}"},
                status=502,
            )
        return web.json_response(
            {
                "ok": True,
                "email": email,
                "cardLinkStatus": saved["status"],
                "attemptCount": attempt_count,
                "attemptLimit": attempt_limit,
                "attemptsExhausted": (
                    saved["status"] == "cs_live" and attempt_count >= attempt_limit
                ),
                **saved,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def retry_registration_checkout_probe(
        request: web.Request,
    ) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            body = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        email = str(body.get("email") or "").strip().lower()
        if not _valid_supported_account_email(email):
            return web.json_response(
                {"ok": False, "error": "邮箱地址无效"}, status=400
            )
        probe = await app["validate_saved_registration_checkout"](email, force=True)
        if not isinstance(probe, dict):
            return web.json_response(
                {
                    "ok": False,
                    "error": "账号缺少注册环境记录，无法重新检测 Checkout",
                },
                status=409,
            )
        return web.json_response(
            {"ok": True, **probe}, headers={"Cache-Control": "no-store"}
        )

    async def browser_status(_: web.Request) -> web.Response:
        return web.json_response(
            {"ok": True, **app["browser_manager"].snapshot()},
            headers={"Cache-Control": "no-store"},
        )

    async def registration_status(_: web.Request) -> web.Response:
        task = app["registration_manager"].snapshot()
        task["failureRecords"] = await asyncio.to_thread(
            load_registration_failures
        )
        return web.json_response(
            {"ok": True, **task},
            headers={"Cache-Control": "no-store"},
        )

    async def import_workbench_inventory(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        email = str(body.get("email") or "").strip().lower()
        if not _valid_supported_account_email(email) or not email.endswith(
            "@icloud.com"
        ):
            return web.json_response(
                {"ok": False, "error": "请输入有效的 iCloud 邮箱"}, status=400
            )
        if not app["workbench_url"] or not app["workbench_import_token"]:
            return web.json_response(
                {"ok": False, "error": "OpenAI 账户工作台导入尚未配置"},
                status=503,
            )
        payload = {
            "email": email,
            "inventoryOnly": True,
            "label": str(body.get("label") or "")[:200],
            "note": str(body.get("note") or "")[:1000],
            "createdAt": str(body.get("createdAt") or "")[:100],
            "source": "apple-hide-my-email-extension",
        }
        target = f'{app["workbench_url"]}/api/integrations/hidemyemail/import'
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.post(
                    target,
                    json=payload,
                    headers={
                        "X-HME-Import-Token": app["workbench_import_token"],
                    },
                ) as response:
                    result = await response.json(content_type=None)
                    if response.status >= 400 or not result.get("success"):
                        message = str(result.get("error") or "工作台拒绝库存导入")
                        raise RuntimeError(message)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as error:
            return web.json_response(
                {"ok": False, "error": f"加入工作台未注册库存失败：{error}"},
                status=502,
            )
        return web.json_response(
            {
                "ok": True,
                "email": email,
                "inventory": "unregistered",
                "imported": bool(result.get("imported")),
                "updated": bool(result.get("updated")),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def registration_start(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        label = str(payload.get("label") or "OpenAI 一键注册").strip()
        if not label or len(label) > 100:
            return web.json_response(
                {"ok": False, "error": "邮箱标签长度必须是 1–100 个字符"},
                status=400,
            )
        provider = str(
            payload.get("provider")
            or ("manual" if str(payload.get("email") or "").strip() else "inventory")
        ).strip().lower()
        if provider not in {"manual", "inventory", "smsbower"}:
            return web.json_response(
                {"ok": False, "error": "不支持的注册邮箱来源"}, status=400
            )
        browser_engine = str(
            payload.get("browser_engine") or "camoufox"
        ).strip().lower()
        if browser_engine not in {"camoufox", "roxy"}:
            return web.json_response(
                {"ok": False, "error": "不支持的注册浏览器引擎"}, status=400
            )
        try:
            concurrency = int(payload.get("concurrency", 1))
        except (TypeError, ValueError):
            return web.json_response(
                {"ok": False, "error": "注册并发数必须是整数"}, status=400
            )
        concurrency_limit = 5 if browser_engine == "roxy" else 10
        if concurrency < 1 or concurrency > concurrency_limit:
            return web.json_response(
                {
                    "ok": False,
                    "error": f"{('Roxy ' if browser_engine == 'roxy' else '')}注册并发数必须是 1–{concurrency_limit}",
                },
                status=400,
            )
        try:
            target_count = int(payload.get("target_count", concurrency))
        except (TypeError, ValueError):
            return web.json_response(
                {"ok": False, "error": "目标账号数必须是整数"}, status=400
            )
        if target_count < 1 or target_count > 100:
            return web.json_response(
                {"ok": False, "error": "目标账号数必须是 1–100"}, status=400
            )
        if provider in {"manual", "smsbower"}:
            concurrency = 1
            target_count = 1
        elif browser_engine != "roxy":
            target_count = concurrency
        email = str(payload.get("email") or "").strip().lower()
        if provider == "manual" and (
            not email
            or len(email) > 254
            or email.count("@") != 1
            or any(character.isspace() for character in email)
            or "." not in email.rsplit("@", 1)[1]
        ):
            return web.json_response(
                {"ok": False, "error": "请输入有效的注册邮箱地址"},
                status=400,
            )
        try:
            registration_options = dict(
                label=label,
                headless=bool(payload.get("headless", False)),
                concurrency=concurrency,
                email=email,
                provider=provider,
            )
            if browser_engine != "camoufox":
                registration_options["browser_engine"] = browser_engine
                registration_options["target_count"] = target_count
            task = app["registration_manager"].start(**registration_options)
        except (RuntimeError, ValueError) as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=409
            )
        return web.json_response({"ok": True, "started": True, "task": task})

    async def registration_code_submit(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
            task = app["registration_manager"].submit_verification_code(
                str(payload.get("email") or ""),
                str(payload.get("code") or ""),
            )
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        except RuntimeError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=409)
        return web.json_response({"ok": True, "task": task})

    async def registration_code_poll(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
            manager = app["registration_manager"]
            email = str(payload.get("email") or "")
            request_id = str(payload.get("requestId") or "")
            async_poller = getattr(manager, "poll_verification_code_async", None)
            if callable(async_poller):
                code = await async_poller(email, request_id=request_id)
            else:
                code = manager.poll_verification_code(email)
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        except RuntimeError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=409)
        if not code:
            provider = app["registration_manager"].snapshot().get("provider")
            return web.json_response(
                {
                    "ok": False,
                    "error": (
                        "SMSBower Gmail 验证码尚未到达"
                        if provider == "smsbower"
                        else "等待手动输入验证码"
                    ),
                },
                status=404,
            )
        return web.json_response({"ok": True, "code": code})

    async def registration_stop(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        process_id = str(
            payload.get("process_id") or payload.get("processId") or ""
        ).strip()
        try:
            task = await app["registration_manager"].stop(process_id=process_id)
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=404)
        return web.json_response({"ok": True, "task": task})

    async def protocol_registration_status(_: web.Request) -> web.Response:
        return web.json_response(
            {"ok": True, **app["protocol_registration_manager"].snapshot()},
            headers={"Cache-Control": "no-store"},
        )

    async def protocol_registration_runtime_refresh(
        request: web.Request,
    ) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        runtime = await asyncio.to_thread(
            app["protocol_registration_manager"].refresh_runtime
        )
        return web.json_response(
            {"ok": True, "runtime": runtime},
            headers={"Cache-Control": "no-store"},
        )

    async def protocol_registration_start(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        try:
            concurrency = int(payload.get("concurrency") or 1)
        except (TypeError, ValueError):
            concurrency = 0
        if not 1 <= concurrency <= 5:
            return web.json_response(
                {"ok": False, "error": "协议注册并发必须是 1–5"}, status=400
            )

        provider = str(payload.get("provider") or "").strip().lower()
        if provider not in {"", "inventory"}:
            return web.json_response(
                {"ok": False, "error": "协议注册暂不支持该邮箱来源"}, status=400
            )
        inventory_email = ""
        if provider == "inventory":
            try:
                inventory_email = await acquire_registration_inventory_email(
                    "iCloud 邮箱协议注册"
                )
            except RuntimeError as error:
                return web.json_response(
                    {"ok": False, "error": str(error)}, status=409
                )
            if not inventory_email:
                return web.json_response(
                    {
                        "ok": False,
                        "code": "inventory_empty",
                        "error": "iCloud 未注册库存为空",
                    },
                    status=409,
                )

        async def release_inventory_email(message: str) -> None:
            if not inventory_email:
                return
            await complete_registration_inventory_email(
                inventory_email, False, message
            )

        try:
            identities = await active_icloud_identities()
        except RuntimeError:
            identities = []
        items = await asyncio.to_thread(
            _browser_email_items, app["db_file"], identities
        )
        accounts_by_email = {
            str(item.get("email") or "").strip().lower(): item
            for item in items
            if str(item.get("email") or "").strip()
        }
        # A remote inventory lease may contain only the address payload.  Such
        # an address is a valid registration input even though it is not yet an
        # active Apple identity or a stored GPT account, so it will not appear
        # in ``_browser_email_items`` until registration saves credentials.
        if inventory_email and inventory_email not in accounts_by_email:
            account = await asyncio.to_thread(
                load_account_record, app["db_file"], inventory_email
            )
            session = account_session(account)
            access_token = account_session_access_token(account)
            accounts_by_email[inventory_email] = {
                "email": inventory_email,
                "registrationComplete": bool(session and access_token),
            }
        run_all = bool(payload.get("all"))
        force_recheck = bool(payload.get("force"))
        if inventory_email:
            requested = [inventory_email]
        elif run_all:
            requested = list(accounts_by_email)
        else:
            raw_emails = payload.get("emails")
            if not isinstance(raw_emails, list):
                raw_emails = []
            requested = []
            for value in raw_emails:
                email = str(value or "").strip().lower()
                if email and email not in requested:
                    requested.append(email)
        invalid = [
            email
            for email in requested
            if email not in accounts_by_email
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
        ]
        if invalid:
            await release_inventory_email("库存邮箱未能导入本地账号列表")
            return web.json_response(
                {
                    "ok": False,
                    "error": f"协议注册账号不存在：{invalid[0]}",
                },
                status=404,
            )
        pending = [
            email
            for email in requested
            if force_recheck
            or not bool(accounts_by_email[email].get("protocolReady"))
        ]
        skipped = len(requested) - len(pending)
        if not pending:
            await release_inventory_email("库存邮箱已完成协议凭据配置")
            message = (
                "全部账号的密码、Session 与 TOTP 2FA 均已完成"
                if requested
                else "当前没有可协议注册的邮箱账号"
            )
            return web.json_response(
                {"ok": False, "error": message}, status=409
            )
        try:
            start_options: dict[str, Any] = {
                "emails": pending,
                "base_url": f"{request.scheme}://{request.host}",
                "concurrency": concurrency,
            }
            if inventory_email:
                start_options["on_account_finished"] = (
                    complete_registration_inventory_email
                )
            task = app["protocol_registration_manager"].start(**start_options)
        except ValueError as error:
            await release_inventory_email(str(error))
            return web.json_response(
                {"ok": False, "error": str(error)}, status=400
            )
        except RuntimeError as error:
            await release_inventory_email(str(error))
            return web.json_response(
                {"ok": False, "error": str(error)}, status=409
            )
        task["skipped"] = skipped
        return web.json_response(
            {
                "ok": True,
                "started": True,
                "skipped": skipped,
                "provider": provider or "selected",
                "email": inventory_email,
                "task": task,
            }
        )

    async def protocol_registration_stop(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        process_id = str(
            payload.get("process_id") or payload.get("processId") or ""
        ).strip()
        try:
            task = await app["protocol_registration_manager"].stop(
                process_id=process_id
            )
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=404)
        return web.json_response({"ok": True, "task": task})

    async def protocol_registration_code(request: web.Request) -> web.Response:
        token = str(request.match_info.get("token") or "")
        record = app["protocol_registration_manager"].token_record(token)
        if not record:
            return web.Response(
                text="协议取码令牌已失效",
                status=404,
                headers={"Cache-Control": "no-store"},
            )
        item, error, status = await resolve_gpt_code(
            str(record.get("email") or ""),
            str(record.get("since") or ""),
        )
        if not item:
            return web.Response(
                text=error or "验证码尚未到达",
                status=status,
                headers={"Cache-Control": "no-store"},
            )
        return web.Response(
            text=str(item.get("code") or ""),
            content_type="text/plain",
            headers={"Cache-Control": "no-store"},
        )

    async def smsbower_status(_: web.Request) -> web.Response:
        return web.json_response(
            {"ok": True, **app["smsbower_client"].public_state()},
            headers={"Cache-Control": "no-store"},
        )

    async def smsbower_config(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
            state = app["smsbower_config_store"].configure(
                api_key=payload.get("apiKey") if "apiKey" in payload else None,
                service=payload.get("service") if "service" in payload else None,
                max_price=(
                    payload.get("maxPrice") if "maxPrice" in payload else None
                ),
            )
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        return web.json_response({"ok": True, **state})

    async def verification_status(_: web.Request) -> web.Response:
        return web.json_response(
            {"ok": True, **app["verification_manager"].snapshot()},
            headers={"Cache-Control": "no-store"},
        )

    async def verification_start(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        concurrency = payload.get("concurrency", 3)
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or not 1 <= concurrency <= 10
        ):
            return web.json_response(
                {"ok": False, "error": "验证并发必须是 1–10 的整数"}, status=400
            )
        raw_emails = payload.get("emails")
        emails: list[str] | None = None
        if raw_emails is not None:
            if app["registration_manager"].snapshot().get("running"):
                return web.json_response(
                    {"ok": False, "error": "一键注册正在运行，请等待完成"},
                    status=409,
                )
            if app["browser_manager"].snapshot().get("running"):
                return web.json_response(
                    {"ok": False, "error": "浏览器任务正在运行，请等待完成"},
                    status=409,
                )
            if not isinstance(raw_emails, list) or not 1 <= len(raw_emails) <= 500:
                return web.json_response(
                    {"ok": False, "error": "验证账号列表无效"}, status=400
                )
            emails = []
            for value in raw_emails:
                email = str(value or "").strip().lower()
                if not email.endswith("@icloud.com") or len(email) > 320:
                    return web.json_response(
                        {"ok": False, "error": "验证账号列表包含无效邮箱"},
                        status=400,
                    )
                emails.append(email)
        try:
            task = (
                app["verification_manager"].start_with_browser(
                    concurrency=concurrency, emails=emails
                )
                if emails is not None
                else app["verification_manager"].start(concurrency=concurrency)
            )
        except RuntimeError as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=409
            )
        return web.json_response({"ok": True, "started": True, "task": task})

    async def verification_stop(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        task = await app["verification_manager"].stop()
        return web.json_response({"ok": True, "task": task})

    async def verify_or_register_account(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        email = str(payload.get("email") or "").strip().lower()
        if not _valid_supported_account_email(email):
            return web.json_response(
                {"ok": False, "error": "邮箱地址无效"}, status=400
            )
        if app["registration_manager"].snapshot().get("running"):
            return web.json_response(
                {"ok": False, "error": "一键注册正在运行，请等待完成"}, status=409
            )
        if app["browser_manager"].snapshot().get("running"):
            return web.json_response(
                {"ok": False, "error": "浏览器任务正在运行，请等待完成"}, status=409
            )
        if app["verification_manager"].snapshot().get("running"):
            return web.json_response(
                {"ok": False, "error": "账号验证任务正在运行，请等待完成"}, status=409
            )

        record = await asyncio.to_thread(
            load_account_record, app["db_file"], email
        )
        reset_password = bool(payload.get("reset_password", False))
        refresh_with_cookie = bool(payload.get("refresh_with_cookie", False))
        session = account_session(record)
        access_token = account_session_access_token(record)
        saved_two_factor = (
            record.get("two_factor")
            if isinstance(record.get("two_factor"), dict)
            else {}
        )
        if refresh_with_cookie and not reset_password:
            if not account_saved_cookies(record):
                return web.json_response(
                    {
                        "ok": False,
                        "error": "该账号尚未保存 Cookie，请先重新登录或注册获取 Cookie",
                    },
                    status=409,
                )
            try:
                task = app["verification_manager"].start_with_browser(
                    emails=[email], concurrency=1, force_refresh=True
                )
            except RuntimeError as error:
                return web.json_response(
                    {"ok": False, "error": str(error)}, status=409
                )
            return web.json_response(
                {
                    "ok": True,
                    "started": True,
                    "mode": "refresh_cookie",
                    "message": "正在使用保存的 Cookie 重新获取 Session 与账号状态",
                    "task": task,
                }
            )
        if (
            not reset_password
            and session
            and access_token
            and not access_token_is_expired(access_token)
            and not record.get("session_invalid_at")
        ):
            try:
                task = app["verification_manager"].start(
                    concurrency=1, emails=[email]
                )
            except RuntimeError as error:
                return web.json_response(
                    {"ok": False, "error": str(error)}, status=409
                )
            return web.json_response(
                {"ok": True, "started": True, "mode": "verify", "task": task}
            )
        config_path: Path = app["inbox_config_file"]
        if not config_path.exists():
            return web.json_response(
                {"ok": False, "error": "请先配置接收邮箱，再注册账号"}, status=409
            )
        try:
            identities = await active_icloud_identities()
        except RuntimeError as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=502
            )
        identity_emails = {
            str(item.get("hme") or "").strip().lower() for item in identities
        }
        if email not in identity_emails:
            try:
                deletion_message = await delete_invalid_icloud_email(
                    email, "Apple 活动邮箱列表中未找到该地址"
                )
            except RuntimeError as error:
                return web.json_response(
                    {
                        "ok": False,
                        "error": f"该 iCloud 邮箱无效或已停用；自动删除失败：{error}",
                    },
                    status=502,
                )
            detail = f"Apple 活动邮箱列表中未找到该地址；{deletion_message}"
            task = app["verification_manager"].record_invalid_email_deleted(
                email, detail
            )
            return web.json_response(
                {
                    "ok": True,
                    "started": False,
                    "deleted": True,
                    "mode": "deleted_invalid",
                    "message": detail,
                    "task": task,
                }
            )
        if not reset_password:
            try:
                task = app["verification_manager"].start_with_browser(
                    emails=[email], concurrency=1
                )
            except RuntimeError as error:
                return web.json_response(
                    {"ok": False, "error": str(error)}, status=409
                )
            return web.json_response(
                {
                    "ok": True,
                    "started": True,
                    "mode": "refresh_session",
                    "task": task,
                }
            )
        registration_only = not access_token and not reset_password
        account_password = (
            "" if registration_only else str(record.get("password") or "")
        )
        if not registration_only and not account_password:
            account_password = generate_openai_password()
            await asyncio.to_thread(
                _save_account_record,
                app["db_file"],
                email,
                password=account_password,
                password_confirmed=False,
            )
        try:
            task = app["browser_manager"].start(
                [
                    {
                        "email": email,
                        "password": account_password,
                        "ensure_password": not registration_only,
                        "force_reset_password": bool(
                            reset_password
                            or not str(record.get("password") or "")
                            or record.get("password_confirmed") is False
                        ),
                        "enable_2fa": False,
                        "two_factor": saved_two_factor,
                    }
                ],
                headless=bool(payload.get("headless", False)),
                concurrency=1,
            )
        except RuntimeError as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=409
            )
        return web.json_response(
            {
                "ok": True,
                "started": True,
                "mode": (
                    "set_password" if reset_password or access_token else "register"
                ),
                "task": task,
            }
        )

    async def enable_account_two_factor(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        email = str(payload.get("email") or "").strip().lower()
        if not _valid_supported_account_email(email):
            return web.json_response(
                {"ok": False, "error": "邮箱地址无效"}, status=400
            )
        if app["registration_manager"].snapshot().get("running"):
            return web.json_response(
                {"ok": False, "error": "一键注册正在运行，请等待完成"}, status=409
            )
        if app["browser_manager"].snapshot().get("running"):
            return web.json_response(
                {"ok": False, "error": "浏览器任务正在运行，请等待完成"}, status=409
            )
        if app["verification_manager"].snapshot().get("running"):
            return web.json_response(
                {"ok": False, "error": "账号验证任务正在运行，请等待完成"},
                status=409,
            )

        record = await asyncio.to_thread(
            load_account_record, app["db_file"], email
        )
        two_factor = (
            record.get("two_factor")
            if isinstance(record.get("two_factor"), dict)
            else {}
        )
        if two_factor.get("enabled"):
            return web.json_response(
                {"ok": False, "error": "该账号已经开启 2FA"}, status=409
            )
        password = str(record.get("password") or "")
        if not password or record.get("password_confirmed") is False:
            return web.json_response(
                {
                    "ok": False,
                    "error": "请先设置并确认账号密码，再添加 2FA",
                },
                status=409,
            )
        try:
            task = app["browser_manager"].start(
                [
                    {
                        "email": email,
                        "password": password,
                        "password_confirmed": True,
                        "enable_2fa": True,
                        "two_factor": two_factor,
                    }
                ],
                headless=True,
                concurrency=1,
            )
        except RuntimeError as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=409
            )
        return web.json_response(
            {
                "ok": True,
                "started": True,
                "mode": "enable_2fa",
                "message": "正在使用无头浏览器登录账号并添加 2FA",
                "task": task,
            }
        )

    async def start_browser_task(
        request: web.Request, *, selected_only: bool
    ) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        if app["registration_manager"].snapshot().get("running"):
            return web.json_response(
                {"ok": False, "error": "一键注册正在运行，请等待完成"}, status=409
            )
        headless = bool(payload.get("headless", False))
        if bool(payload.get("enable_2fa", False)):
            return web.json_response(
                {"ok": False, "error": "已停用新账号的 2FA 设置"},
                status=400,
            )
        concurrency = payload.get("concurrency", 1)
        if isinstance(concurrency, bool) or not isinstance(concurrency, int):
            return web.json_response(
                {"ok": False, "error": "并发数必须是 1–10 的整数"}, status=400
            )
        if not 1 <= concurrency <= 10:
            return web.json_response(
                {"ok": False, "error": "并发数必须是 1–10 的整数"}, status=400
            )
        try:
            identities = await active_icloud_identities()
        except RuntimeError as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=502
            )
        identity_emails = {
            str(item.get("hme") or "").strip().lower() for item in identities
        }
        identity_emails -= await asyncio.to_thread(
            removed_account_emails, app["db_file"]
        )
        requested: set[str] | None = None
        if selected_only:
            values = payload.get("emails")
            if not isinstance(values, list):
                return web.json_response(
                    {"ok": False, "error": "请选择需要获取的邮箱"}, status=400
                )
            requested = {
                str(value or "").strip().lower()
                for value in values
                if str(value or "").strip()
            }
            if not requested or not requested.issubset(identity_emails):
                return web.json_response(
                    {"ok": False, "error": "选择中包含无效或已停用的 iCloud 邮箱"},
                    status=400,
                )

        accounts: list[dict] = []
        skipped = 0
        for email in sorted(identity_emails):
            if requested is not None and email not in requested:
                continue
            record = await asyncio.to_thread(
                load_account_record, app["db_file"], email
            )
            access_token = str(
                record.get("access_token") or record.get("accessToken") or ""
            ).strip()
            if (
                access_token
                and not access_token_is_expired(access_token)
            ):
                skipped += 1
                continue
            accounts.append(
                {
                    "email": email,
                    "password": str(record.get("password") or ""),
                    "enable_2fa": False,
                    "two_factor": record.get("two_factor")
                    if isinstance(record.get("two_factor"), dict)
                    else {},
                }
            )
        if not accounts:
            message = (
                "所选邮箱的 Token 都仍有效，无需重复打开浏览器"
                if selected_only
                else "全部邮箱的 Token 都仍有效，无需重复打开浏览器"
            )
            return web.json_response(
                {
                    "ok": True,
                    "started": False,
                    "message": message,
                    "skipped": skipped,
                    "task": app["browser_manager"].snapshot(),
                }
            )
        try:
            task = app["browser_manager"].start(
                accounts,
                headless=headless,
                concurrency=concurrency,
                skipped=skipped,
            )
        except RuntimeError as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=409
            )
        return web.json_response({"ok": True, "started": True, "task": task})

    async def browser_fetch_all(request: web.Request) -> web.Response:
        return await start_browser_task(request, selected_only=False)

    async def browser_fetch_selected(request: web.Request) -> web.Response:
        return await start_browser_task(request, selected_only=True)

    async def browser_stop(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        task = await app["browser_manager"].stop()
        return web.json_response({"ok": True, "task": task})

    async def generate(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response({"ok": False, "error": "本地请求令牌无效"}, status=403)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response({"ok": False, "error": "请求格式无效"}, status=400)

        label = str(payload.get("label") or "").strip()
        count = payload.get("count")
        if not label or len(label) > 100:
            return web.json_response({"ok": False, "error": "标签长度必须是 1–100 个字符"}, status=400)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
            return web.json_response({"ok": False, "error": "数量必须是 1–10 的整数"}, status=400)

        cookie_path: Path = app["cookie_file"]
        if not cookie_path.exists() or cookie_path.stat().st_size == 0:
            return web.json_response({"ok": False, "error": "Cookie 尚未准备好"}, status=401)

        async with app["generate_lock"]:
            account = await fetch_account_info(str(cookie_path), app["region"])
            if account.get("error"):
                return web.json_response(
                    {"ok": False, "error": "Cookie 已失效，请重新获取"}, status=401
                )
            generated = await _generate(
                label=label,
                count=count,
                cookie_file=str(cookie_path),
                output_file=str(app["output_file"]),
                region=app["region"],
                db_file=str(app["db_file"]),
            )
        emails = generated.get("emails", [])
        if not generated.get("ok") or not emails:
            return web.json_response(
                {"ok": False, "error": _generation_failure_message(generated)}, status=502
            )
        return web.json_response({"ok": True, "emails": list(emails)})

    async def scheduled_generation_status(_: web.Request) -> web.Response:
        manager = app.get("scheduled_generation_manager")
        if manager is None:
            return web.json_response(
                {"ok": False, "error": "定时生成已迁移到远端库存服务"},
                status=404,
            )
        await manager.initialize()
        inventory = await asyncio.to_thread(
            stored_registration_inventory_status, app["db_file"]
        )
        return web.json_response(
            {
                "ok": True,
                **manager.snapshot(),
                "inventoryAvailable": inventory["available"],
            },
            headers={"Cache-Control": "no-store"},
        )

    async def scheduled_generation_config(request: web.Request) -> web.Response:
        manager = app.get("scheduled_generation_manager")
        if manager is None:
            return web.json_response(
                {"ok": False, "error": "定时生成已迁移到远端库存服务"},
                status=404,
            )
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            return web.json_response(
                {"ok": False, "error": "enabled 必须是布尔值"}, status=400
            )
        state = await manager.configure(enabled=enabled)
        inventory_available = await asyncio.to_thread(
            available_generated_inventory_count, app["db_file"]
        )
        return web.json_response(
            {"ok": True, **state, "inventoryAvailable": inventory_available}
        )

    async def registration_inventory_client_status(_: web.Request) -> web.Response:
        client = app["inventory_client"]
        if not client.configured:
            return web.json_response(
                {
                    "ok": True,
                    "configured": False,
                    "available": 0,
                    "error": "远端邮箱库存服务未配置",
                },
                headers={"Cache-Control": "no-store"},
            )
        try:
            state = await client.status()
        except RuntimeError as error:
            local_state = await asyncio.to_thread(
                stored_registration_inventory_status, app["db_file"]
            )
            local_available = max(0, int(local_state.get("available") or 0))
            return web.json_response(
                {
                    "ok": True,
                    "configured": True,
                    "available": local_available,
                    "fallback": local_available > 0,
                    "error": (
                        "远端库存暂不可用，已切换本地生成库存"
                        if local_available > 0
                        else str(error)
                    ),
                },
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response(
            {
                "configured": True,
                **state,
                "sync": dict(app["inventory_sync_state"]),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def integration_inventory_status(_: web.Request) -> web.Response:
        if not app["inventory_server_enabled"]:
            raise web.HTTPNotFound()
        inventory = await asyncio.to_thread(
            stored_registration_inventory_status, app["db_file"]
        )
        manager = app.get("scheduled_generation_manager")
        if manager is not None:
            await manager.initialize()
        return web.json_response(
            {
                "ok": True,
                **inventory,
                "scheduledGeneration": manager.snapshot() if manager else None,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def integration_inventory_lease(request: web.Request) -> web.Response:
        if not app["inventory_server_enabled"]:
            raise web.HTTPNotFound()
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        client_id = str(payload.get("clientId") or "").strip()
        label = str(payload.get("label") or "").strip()
        if len(client_id) > 200 or len(label) > 200:
            return web.json_response(
                {"ok": False, "error": "clientId 或 label 过长"}, status=400
            )
        lease = await asyncio.to_thread(
            lease_generated_inventory_email,
            app["db_file"],
            client_id=client_id,
            label=label,
            lease_seconds=app["inventory_lease_seconds"],
        )
        if not lease:
            return web.json_response(
                {
                    "ok": False,
                    "code": "inventory_empty",
                    "error": "远端邮箱库存不足",
                },
                status=409,
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response(
            {"ok": True, "lease": lease}, headers={"Cache-Control": "no-store"}
        )

    async def integration_inventory_sync(request: web.Request) -> web.Response:
        if not app["inventory_server_enabled"]:
            raise web.HTTPNotFound()
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        if payload.get("schemaVersion") != SYNC_SCHEMA_VERSION:
            return web.json_response(
                {"ok": False, "error": "不支持的同步协议版本"}, status=400
            )
        records = payload.get("records")
        if not isinstance(records, list) or len(records) > 50:
            return web.json_response(
                {"ok": False, "error": "records 必须是最多 50 项的数组"},
                status=400,
            )
        try:
            result = await asyncio.to_thread(
                import_inventory_records, app["db_file"], records
            )
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        return web.json_response(
            {"ok": True, **result}, headers={"Cache-Control": "no-store"}
        )

    async def integration_inventory_result(request: web.Request) -> web.Response:
        if not app["inventory_server_enabled"]:
            raise web.HTTPNotFound()
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        if not isinstance(payload.get("success"), bool):
            return web.json_response(
                {"ok": False, "error": "success 必须是布尔值"}, status=400
            )
        record = payload.get("record")
        if record is not None and not isinstance(record, dict):
            return web.json_response(
                {"ok": False, "error": "record 必须是对象或 null"}, status=400
            )
        try:
            result = await asyncio.to_thread(
                complete_generated_inventory_lease,
                app["db_file"],
                lease_id=str(payload.get("leaseId") or ""),
                email=str(payload.get("email") or ""),
                success=payload["success"],
                message=str(payload.get("message") or ""),
                record=record,
            )
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        if not result.get("ok"):
            status = 404 if result.get("status") == "not_found" else 409
            return web.json_response(result, status=status)
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def roxy_registration_status(_: web.Request) -> web.Response:
        state = await asyncio.to_thread(
            app["roxy_registration_store"].public_state
        )
        return web.json_response(
            {"ok": True, **state}, headers={"Cache-Control": "no-store"}
        )

    async def roxy_registration_config(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        fields = {
            "apiUrl": payload.get("apiUrl") if "apiUrl" in payload else None,
            "workspaceId": (
                payload.get("workspaceId") if "workspaceId" in payload else None
            ),
            "profileId": (
                payload.get("profileId") if "profileId" in payload else None
            ),
        }
        if any(
            value is not None
            and (not isinstance(value, (str, int)) or isinstance(value, bool))
            for value in fields.values()
        ):
            return web.json_response(
                {"ok": False, "error": "Roxy 注册配置字段无效"}, status=400
            )
        try:
            await asyncio.to_thread(
                app["roxy_registration_store"].configure,
                api_url=fields["apiUrl"],
                workspace_id=fields["workspaceId"],
                profile_id=fields["profileId"],
            )
            state = await asyncio.to_thread(
                app["roxy_registration_store"].public_state
            )
        except ValueError as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=400
            )
        return web.json_response({"ok": True, **state})

    async def _proxy_status(_: web.Request, store_key: str) -> web.Response:
        state = await asyncio.to_thread(
            app[store_key].public_state
        )
        return web.json_response(
            {"ok": True, **state}, headers={"Cache-Control": "no-store"}
        )

    async def _proxy_config(request: web.Request, store_key: str) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        enabled = payload.get("enabled") if "enabled" in payload else None
        mode = payload.get("mode") if "mode" in payload else None
        country = payload.get("country") if "country" in payload else None
        proxy_line = payload.get("proxyLine") if "proxyLine" in payload else None
        proxy_endpoint = (
            payload.get("proxyEndpoint") if "proxyEndpoint" in payload else None
        )
        proxy_username = (
            payload.get("proxyUsername") if "proxyUsername" in payload else None
        )
        proxy_password = (
            payload.get("proxyPassword") if "proxyPassword" in payload else None
        )
        clash_controller = (
            payload.get("clashController") if "clashController" in payload else None
        )
        clash_secret = payload.get("clashSecret") if "clashSecret" in payload else None
        clash_selector = (
            payload.get("clashSelector") if "clashSelector" in payload else None
        )
        clash_proxy_url = (
            payload.get("clashProxyUrl") if "clashProxyUrl" in payload else None
        )
        max_latency_ms = (
            payload.get("maxLatencyMs") if "maxLatencyMs" in payload else None
        )
        card_link_countries = (
            payload.get("cardLinkCountries")
            if "cardLinkCountries" in payload
            else None
        )
        card_link_modes = (
            payload.get("cardLinkModes") if "cardLinkModes" in payload else None
        )
        if enabled is not None and not isinstance(enabled, bool):
            return web.json_response(
                {"ok": False, "error": "enabled 必须是布尔值"}, status=400
            )
        if country is not None and not isinstance(country, str):
            return web.json_response(
                {"ok": False, "error": "country 必须是国家代码"}, status=400
            )
        if mode is not None and not isinstance(mode, str):
            return web.json_response(
                {"ok": False, "error": "mode 必须是代理模式"}, status=400
            )
        if proxy_line is not None and (
            not isinstance(proxy_line, str) or len(proxy_line) > 1000
        ):
            return web.json_response(
                {"ok": False, "error": "代理连接信息无效"}, status=400
            )
        proxy_fields = {
            "proxyEndpoint": proxy_endpoint,
            "proxyUsername": proxy_username,
            "proxyPassword": proxy_password,
        }
        if any(
            value is not None
            and (not isinstance(value, str) or len(value) > 1000)
            for value in proxy_fields.values()
        ):
            return web.json_response(
                {"ok": False, "error": "代理配置字段无效"}, status=400
            )
        clash_strings = {
            "clashController": clash_controller,
            "clashSecret": clash_secret,
            "clashSelector": clash_selector,
            "clashProxyUrl": clash_proxy_url,
        }
        if any(
            value is not None
            and (not isinstance(value, str) or len(value) > 1000)
            for value in clash_strings.values()
        ):
            return web.json_response(
                {"ok": False, "error": "Clash 配置字段无效"}, status=400
            )
        if max_latency_ms is not None and (
            isinstance(max_latency_ms, bool) or not isinstance(max_latency_ms, int)
        ):
            return web.json_response(
                {"ok": False, "error": "maxLatencyMs 必须是整数"}, status=400
            )
        if card_link_countries is not None and not isinstance(
            card_link_countries, dict
        ):
            return web.json_response(
                {"ok": False, "error": "cardLinkCountries 必须是国家配置"},
                status=400,
            )
        if card_link_modes is not None and not isinstance(card_link_modes, dict):
            return web.json_response(
                {"ok": False, "error": "cardLinkModes 必须是代理模式配置"},
                status=400,
            )
        try:
            state = await asyncio.to_thread(
                app[store_key].configure,
                enabled=enabled,
                mode=mode,
                country=country,
                proxy_line=proxy_line,
                proxy_endpoint=proxy_endpoint,
                proxy_username=proxy_username,
                proxy_password=proxy_password,
                card_link_countries=card_link_countries,
                card_link_modes=card_link_modes,
                clash_controller=clash_controller,
                clash_secret=clash_secret,
                clash_selector=clash_selector,
                clash_proxy_url=clash_proxy_url,
                max_latency_ms=max_latency_ms,
            )
        except ValueError as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=400
            )
        return web.json_response({"ok": True, **state})

    async def _proxy_rotate(
        request: web.Request, store_key: str, scope_label: str
    ) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            proxy_url, state = await asyncio.to_thread(
                app[store_key].next_proxy,
                force=True,
            )
        except (RuntimeError, ValueError) as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=502
            )
        if not proxy_url:
            return web.json_response(
                {"ok": False, "error": f"{scope_label}代理尚未配置"}, status=400
            )
        return web.json_response(
            {"ok": True, **state}, headers={"Cache-Control": "no-store"}
        )

    async def _proxy_test(
        request: web.Request, store_key: str, scope_label: str
    ) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        proxy_url, state = await asyncio.to_thread(
            app[store_key].proxy_for_test
        )
        if not proxy_url:
            return web.json_response(
                {"ok": False, "error": f"{scope_label}代理尚未配置"}, status=400
            )
        try:
            result = await app["registration_proxy_tester"](
                proxy_url, str(state.get("country") or "")
            )
        except Exception as error:
            return web.json_response(
                {
                    "ok": False,
                    "error": f"代理测试失败：{_registration_proxy_test_error(error)}",
                },
                status=502,
            )
        return web.json_response(
            {"ok": True, **state, "testResult": result},
            headers={"Cache-Control": "no-store"},
        )

    async def registration_proxy_status(request: web.Request) -> web.Response:
        return await _proxy_status(request, "registration_proxy_store")

    async def registration_proxy_config(request: web.Request) -> web.Response:
        return await _proxy_config(request, "registration_proxy_store")

    async def registration_proxy_rotate(request: web.Request) -> web.Response:
        return await _proxy_rotate(request, "registration_proxy_store", "注册")

    async def registration_proxy_test(request: web.Request) -> web.Response:
        return await _proxy_test(request, "registration_proxy_store", "注册")

    async def card_link_proxy_status(request: web.Request) -> web.Response:
        return await _proxy_status(request, "card_link_proxy_store")

    async def card_link_proxy_config(request: web.Request) -> web.Response:
        return await _proxy_config(request, "card_link_proxy_store")

    async def card_link_proxy_rotate(request: web.Request) -> web.Response:
        return await _proxy_rotate(request, "card_link_proxy_store", "提链")

    async def card_link_proxy_test(request: web.Request) -> web.Response:
        return await _proxy_test(request, "card_link_proxy_store", "提链")

    async def inbox_status(_: web.Request) -> web.Response:
        config_path: Path = app["inbox_config_file"]
        cleanup_status = dict(app["deactivation_scan_state"])
        if not config_path.exists():
            return web.json_response(
                {
                    "ok": True,
                    "configured": False,
                    "codeCount": 0,
                    "syncMode": "scheduled-and-on-demand",
                    "backgroundInterval": cleanup_status["intervalSeconds"],
                    "deactivationCleanup": cleanup_status,
                },
                headers={"Cache-Control": "no-store"},
            )
        try:
            config = load_config(str(config_path))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return web.json_response(
                {
                    "ok": True,
                    "configured": False,
                    "codeCount": 0,
                    "error": "收件箱配置文件无效",
                    "syncMode": "scheduled-and-on-demand",
                    "backgroundInterval": cleanup_status["intervalSeconds"],
                    "deactivationCleanup": cleanup_status,
                },
                headers={"Cache-Control": "no-store"},
            )
        conn = connect_db(str(app["db_file"]))
        try:
            code_count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE code IS NOT NULL AND code != ''"
            ).fetchone()[0]
        finally:
            conn.close()
        return web.json_response(
            {
                "ok": True,
                "configured": True,
                "account": mask_account(config.username),
                "username": config.username,
                "host": config.host,
                "port": config.port,
                "folder": config.folder,
                "useSsl": config.use_ssl,
                "codeCount": code_count,
                "syncMode": "scheduled-and-on-demand",
                "backgroundInterval": cleanup_status["intervalSeconds"],
                "lastBackgroundSync": app["inbox_background_last_sync"],
                "backgroundError": app["inbox_background_error"],
                "retryAfterSeconds": app["inbox_on_demand_retry_after"],
                "deactivationCleanup": cleanup_status,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def inbox_config(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response({"ok": False, "error": "本地请求令牌无效"}, status=403)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response({"ok": False, "error": "请求格式无效"}, status=400)

        host = str(payload.get("host") or "").strip()
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        folder = str(payload.get("folder") or DEFAULT_FOLDER).strip()
        port = payload.get("port")
        if not host or len(host) > 255 or any(ch.isspace() for ch in host):
            return web.json_response({"ok": False, "error": "IMAP 主机无效"}, status=400)
        if not username or len(username) > 320:
            return web.json_response({"ok": False, "error": "邮箱账号无效"}, status=400)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            return web.json_response({"ok": False, "error": "IMAP 端口无效"}, status=400)
        if not folder or len(folder) > 255:
            return web.json_response({"ok": False, "error": "IMAP 文件夹无效"}, status=400)

        config_path: Path = app["inbox_config_file"]
        if not password and config_path.exists():
            try:
                current = load_config(str(config_path))
                if current.username == username and current.host == host:
                    password = current.password
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        if not password:
            return web.json_response({"ok": False, "error": "请填写邮箱授权码或应用专用密码"}, status=400)
        if len(password) > 1024:
            return web.json_response({"ok": False, "error": "邮箱授权码长度无效"}, status=400)

        config = InboxConfig(
            host=host,
            port=port,
            username=username,
            password=password,
            folder=folder,
            use_ssl=bool(payload.get("useSsl", True)),
        )
        try:
            async with app["inbox_sync_lock"]:
                await asyncio.to_thread(sync_inbox, config, str(app["db_file"]), 1)
        except Exception as error:
            return web.json_response(
                {"ok": False, "error": _inbox_error_message(error)}, status=502
            )
        save_config(config, str(config_path))
        reset_inbox_sync_backoff()
        app["inbox_background_last_sync"] = datetime.now().astimezone().isoformat()
        return web.json_response({"ok": True, "message": "IMAP 登录成功，配置已保存在本机"})

    async def inbox_codes(request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "30"))
        except ValueError:
            return web.json_response({"ok": False, "error": "数量参数无效"}, status=400)
        if not 1 <= limit <= 100:
            return web.json_response({"ok": False, "error": "数量必须是 1–100"}, status=400)
        items = await asyncio.to_thread(_code_items, app["db_file"], limit)
        return web.json_response(
            {"ok": True, "items": items}, headers={"Cache-Control": "no-store"}
        )

    async def inbox_sync(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response({"ok": False, "error": "本地请求令牌无效"}, status=403)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response({"ok": False, "error": "请求格式无效"}, status=400)
        limit = payload.get("limit", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            return web.json_response({"ok": False, "error": "同步数量必须是 1–200"}, status=400)
        config_path: Path = app["inbox_config_file"]
        if not config_path.exists():
            return web.json_response({"ok": False, "error": "请先配置接收邮箱"}, status=400)
        try:
            inserted = await sync_inbox_on_demand(limit, force=True)
        except Exception as error:
            return web.json_response(
                {"ok": False, "error": _inbox_error_message(error)}, status=502
            )
        items = await asyncio.to_thread(_code_items, app["db_file"], 30)
        return web.json_response({"ok": True, "inserted": len(inserted), "items": items})

    async def paypal_status(_: web.Request) -> web.Response:
        state = await app["paypal_service"].snapshot(ensure=True)
        return web.json_response(
            {"ok": True, **state}, headers={"Cache-Control": "no-store"}
        )

    async def start_account_paypal_payment(request: web.Request) -> web.Response:
        if not _local_token_valid(request, app):
            return web.json_response(
                {"ok": False, "error": "本地请求令牌无效"}, status=403
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest):
            return web.json_response(
                {"ok": False, "error": "请求格式无效"}, status=400
            )
        email = str(payload.get("email") or "").strip().lower()
        if not _valid_supported_account_email(email):
            return web.json_response(
                {"ok": False, "error": "邮箱地址无效"}, status=400
            )
        record = await asyncio.to_thread(load_account_record, app["db_file"], email)
        if not record:
            return web.json_response(
                {"ok": False, "error": "未找到账号记录"}, status=404
            )
        card_link = _account_card_link(record)
        paypal_url = str(card_link.get("url") or "").strip()
        if not paypal_url:
            return web.json_response(
                {"ok": False, "error": "当前账号还没有提取成功的支付链接"},
                status=409,
            )
        link_country = str(card_link.get("country") or "").strip().upper()
        if not link_country:
            return web.json_response(
                {
                    "ok": False,
                    "error": "当前提链记录缺少国家，需重新提链",
                },
                status=409,
            )
        cookies = account_saved_cookies(record)
        if not cookies:
            return web.json_response(
                {"ok": False, "error": "当前账号没有可传入 PP 协议的 Cookie"},
                status=409,
            )
        sms_state = app["smsbower_config_store"].public_state()
        if not sms_state.get("configured"):
            return web.json_response(
                {"ok": False, "error": "请先在系统设置配置 SMSBower API Key"},
                status=409,
            )
        proxy_store: RegistrationProxyStore = app["registration_proxy_store"]
        proxy_state = proxy_store.public_state()
        proxy_mode = str(proxy_state.get("mode") or "dynamic")
        try:
            proxy_url, _ = proxy_store.proxy_for_country(
                link_country, mode=proxy_mode
            )
        except ValueError as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=409
            )
        if not proxy_url:
            return web.json_response(
                {"ok": False, "error": "请先配置通用代理与线路"}, status=409
            )
        device_id = secrets.token_hex(16)
        protocol_payload = {
            "paypal_url": paypal_url,
            "country": link_country,
            "sms_provider": "smsbower",
            "sms_max_price": sms_state.get("maxPrice"),
            "proxy_pool": [proxy_url],
            "source_account_email": email,
            "account_cookies": cookies,
        }
        status, upstream = await app["paypal_service"].create_job(
            protocol_payload, device_id=device_id
        )
        if status != 201:
            return web.json_response(
                {
                    "ok": False,
                    "error": str(upstream.get("error") or "PP 协议支付任务启动失败"),
                },
                status=status if 400 <= status < 600 else 502,
            )
        job = upstream.get("job") if isinstance(upstream.get("job"), dict) else {}
        response = web.json_response(
            {
                "ok": True,
                "email": email,
                "country": link_country,
                "cookieCount": len(cookies),
                "proxyMode": proxy_mode,
                "smsProvider": "smsbower",
                "job": job,
                "url": "/paypal-pay/",
            },
            status=201,
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            "paypal_web_device_id",
            device_id,
            path="/paypal-pay/",
            max_age=30 * 24 * 60 * 60,
            httponly=True,
            samesite="Strict",
        )
        return response

    async def paypal_proxy(request: web.Request) -> web.Response:
        service: PayPalProtocolService = app["paypal_service"]
        if not await service.ensure_running():
            state = await service.snapshot()
            message = str(state.get("error") or "PayPal 协议服务暂不可用")
            return web.Response(
                text=(
                    "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
                    "<title>PP 支付服务不可用</title>"
                    "<style>body{margin:0;padding:48px;background:#08111f;color:#e7eefb;"
                    "font:15px system-ui}main{max-width:720px;margin:auto;padding:28px;"
                    "border:1px solid #22324a;border-radius:14px;background:#0d192b}"
                    "h1{font-size:22px}p{color:#91a2bb;line-height:1.7}</style>"
                    f"<main><h1>PP 支付服务不可用</h1><p>{html.escape(message)}</p>"
                    "<p>请刷新工作台后重试。</p></main></html>"
                ),
                status=503,
                content_type="text/html",
                headers={"Cache-Control": "no-store"},
            )

        tail = str(request.match_info.get("tail") or "")
        upstream_url = f"{service.upstream_url}/" + tail
        if request.query_string:
            upstream_url += "?" + request.query_string
        forwarded_headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower()
            not in {
                "connection",
                "content-length",
                "host",
                "transfer-encoding",
            }
        }
        forwarded_headers["Host"] = request.host
        if request.remote:
            forwarded_headers["X-Forwarded-For"] = request.remote
        body = await request.read()
        timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_read=None)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    request.method,
                    upstream_url,
                    headers=forwarded_headers,
                    data=body or None,
                    allow_redirects=False,
                ) as upstream:
                    response_body = await upstream.read()
                    response_headers = {}
                    for name in (
                        "Content-Type",
                        "Cache-Control",
                        "Content-Disposition",
                        "Last-Modified",
                        "ETag",
                        "Referrer-Policy",
                        "Permissions-Policy",
                        "X-Content-Type-Options",
                    ):
                        value = upstream.headers.get(name)
                        if value:
                            response_headers[name] = value
                    content_type = str(upstream.headers.get("Content-Type") or "")
                    if "text/html" in content_type:
                        content_security_policy = str(
                            upstream.headers.get("Content-Security-Policy") or ""
                        ).replace("frame-ancestors 'none'", "frame-ancestors 'self'")
                        if content_security_policy:
                            response_headers["Content-Security-Policy"] = (
                                content_security_policy
                            )
                        response_headers["X-Frame-Options"] = "SAMEORIGIN"
                    set_cookie = upstream.headers.get("Set-Cookie")
                    if set_cookie:
                        response_headers["Set-Cookie"] = set_cookie.replace(
                            "Path=/", f"Path={PAYPAL_PROTOCOL_PREFIX}/"
                        )
                    return web.Response(
                        body=response_body,
                        status=upstream.status,
                        headers=response_headers,
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            service.ready = False
            service.error = f"PayPal 协议服务连接失败：{error}"
            return web.json_response(
                {"ok": False, "error": service.error}, status=502
            )

    app.router.add_get("/login", login_page)
    app.router.add_post("/api/login", login_api)
    app.router.add_post(INVENTORY_LOGIN_PATH, inventory_login_api)
    app.router.add_get("/access", access_page)
    app.router.add_get("/code", code_page)
    app.router.add_post("/api/code/latest", latest_code)
    app.router.add_post("/api/logout", logout_api)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", index)
    app.router.add_get("/api/paypal/status", paypal_status)
    app.router.add_post("/api/account/paypal-payment", start_account_paypal_payment)
    app.router.add_route("*", PAYPAL_PROTOCOL_PREFIX, paypal_proxy)
    app.router.add_route("*", f"{PAYPAL_PROTOCOL_PREFIX}/{{tail:.*}}", paypal_proxy)
    app.router.add_get("/api/gpt-emails", gpt_emails)
    app.router.add_post("/api/account/type", account_type_update)
    app.router.add_post("/api/gpt-credential", gpt_credential)
    app.router.add_post("/api/gpt-accounts/export", export_gpt_accounts)
    app.router.add_post(
        "/api/account/import-workbench", import_workbench_account
    )
    app.router.add_post("/api/account/card-link", create_card_link)
    app.router.add_post(
        "/api/account/checkout-probe", retry_registration_checkout_probe
    )
    app.router.add_post("/api/gpt-email/delete", delete_gpt_email)
    app.router.add_post("/api/gpt-code", gpt_code)
    app.router.add_post(WORKBENCH_OPENAI_CODE_PATH, workbench_openai_code)
    app.router.add_post(
        WORKBENCH_INVENTORY_IMPORT_PATH, import_workbench_inventory
    )
    app.router.add_get("/api/browser/status", browser_status)
    app.router.add_post("/api/browser/fetch-all", browser_fetch_all)
    app.router.add_post("/api/browser/fetch-selected", browser_fetch_selected)
    app.router.add_post("/api/browser/stop", browser_stop)
    app.router.add_get("/api/registration/status", registration_status)
    app.router.add_post("/api/registration/start", registration_start)
    app.router.add_post("/api/registration/code", registration_code_submit)
    app.router.add_post("/api/registration/code/poll", registration_code_poll)
    app.router.add_post("/api/registration/stop", registration_stop)
    app.router.add_get(
        "/api/protocol-registration/status", protocol_registration_status
    )
    app.router.add_post(
        "/api/protocol-registration/runtime/refresh",
        protocol_registration_runtime_refresh,
    )
    app.router.add_post(
        "/api/protocol-registration/start", protocol_registration_start
    )
    app.router.add_post(
        "/api/protocol-registration/stop", protocol_registration_stop
    )
    app.router.add_get(
        f"{PROTOCOL_CODE_PREFIX}{{token}}", protocol_registration_code
    )
    app.router.add_get("/api/smsbower/status", smsbower_status)
    app.router.add_post("/api/smsbower/config", smsbower_config)
    app.router.add_get(
        "/api/registration-inventory/status", registration_inventory_client_status
    )
    app.router.add_get(INVENTORY_STATUS_PATH, integration_inventory_status)
    app.router.add_post(INVENTORY_LEASE_PATH, integration_inventory_lease)
    app.router.add_post(INVENTORY_SYNC_PATH, integration_inventory_sync)
    app.router.add_post(INVENTORY_RESULT_PATH, integration_inventory_result)
    app.router.add_get(
        "/api/scheduled-generation/status", scheduled_generation_status
    )
    app.router.add_post(
        "/api/scheduled-generation/config", scheduled_generation_config
    )
    app.router.add_get("/api/registration-proxy/status", registration_proxy_status)
    app.router.add_post("/api/registration-proxy/config", registration_proxy_config)
    app.router.add_get("/api/roxy-registration/status", roxy_registration_status)
    app.router.add_post("/api/roxy-registration/config", roxy_registration_config)
    app.router.add_post("/api/registration-proxy/rotate", registration_proxy_rotate)
    app.router.add_post("/api/registration-proxy/test", registration_proxy_test)
    app.router.add_get("/api/card-link-proxy/status", card_link_proxy_status)
    app.router.add_post("/api/card-link-proxy/config", card_link_proxy_config)
    app.router.add_post("/api/card-link-proxy/rotate", card_link_proxy_rotate)
    app.router.add_post("/api/card-link-proxy/test", card_link_proxy_test)
    app.router.add_get("/api/account-verification/status", verification_status)
    app.router.add_post("/api/account-verification/start", verification_start)
    app.router.add_post("/api/account-verification/stop", verification_stop)
    app.router.add_post(
        "/api/account/verify-or-register", verify_or_register_account
    )
    app.router.add_post("/api/account/enable-2fa", enable_account_two_factor)
    app.router.add_get("/api/inbox/status", inbox_status)
    app.router.add_post("/api/inbox/config", inbox_config)
    app.router.add_get("/api/inbox/codes", inbox_codes)
    app.router.add_post("/api/inbox/sync", inbox_sync)
    return app


async def run_server(args: argparse.Namespace) -> None:
    app = create_app(
        base_dir=Path(args.data_dir).resolve(),
        cookie_file=args.cookie_file,
        output_file=args.output_file,
        db_file=args.db_file,
        inbox_config_file=args.inbox_config_file,
        region=args.region,
        web_username=os.environ.get("HIDEMYEMAIL_WEB_USERNAME", ""),
        web_password=os.environ.get("HIDEMYEMAIL_WEB_PASSWORD", ""),
        target_project_dir=os.environ.get("OPENAI_REGISTER_PROJECT_DIR", ""),
        target_python=os.environ.get("OPENAI_REGISTER_PYTHON", ""),
        browser_service_url=os.environ.get(
            "HIDEMYEMAIL_BROWSER_SERVICE_URL",
            f"http://{'127.0.0.1' if args.host in {'0.0.0.0', '::'} else args.host}:{args.port}",
        ),
        force_browser_headless=os.environ.get(
            "HIDEMYEMAIL_FORCE_BROWSER_HEADLESS", ""
        ).strip().lower()
        in {"1", "true", "yes", "on"},
        workbench_url=os.environ.get("ACCOUNT_WORKBENCH_URL", ""),
        workbench_import_token=_configured_workbench_import_token(),
        inventory_server_enabled=os.environ.get(
            "HIDEMYEMAIL_INVENTORY_SERVER", ""
        ).strip().lower()
        in {"1", "true", "yes", "on"},
        inventory_service_url=os.environ.get("HIDEMYEMAIL_INVENTORY_URL", ""),
        inventory_service_token=_configured_inventory_service_token(),
        inventory_service_username=os.environ.get(
            "HIDEMYEMAIL_INVENTORY_USERNAME", ""
        ),
        inventory_service_password=os.environ.get(
            "HIDEMYEMAIL_INVENTORY_PASSWORD", ""
        ),
        inventory_lease_seconds=int(
            os.environ.get(
                "HIDEMYEMAIL_INVENTORY_LEASE_SECONDS",
                str(DEFAULT_LEASE_SECONDS),
            )
        ),
        inventory_batch_size=int(
            os.environ.get(
                "HIDEMYEMAIL_INVENTORY_BATCH_SIZE",
                str(DEFAULT_INVENTORY_BATCH_SIZE),
            )
        ),
        inventory_interval_seconds=int(
            os.environ.get(
                "HIDEMYEMAIL_INVENTORY_INTERVAL_SECONDS",
                str(DEFAULT_INVENTORY_INTERVAL_SECONDS),
            )
        ),
        inventory_sync_interval_seconds=float(
            os.environ.get(
                "HIDEMYEMAIL_INVENTORY_SYNC_INTERVAL_SECONDS",
                str(INVENTORY_SYNC_INTERVAL_SECONDS),
            )
        ),
        smsbower_api_key=os.environ.get("SMSBOWER_API_KEY", ""),
        smsbower_service=os.environ.get(
            "SMSBOWER_MAIL_SERVICE", DEFAULT_SMSBOWER_SERVICE
        ),
        smsbower_max_price=float(
            os.environ.get("SMSBOWER_MAX_PRICE", str(DEFAULT_SMSBOWER_MAX_PRICE))
        ),
        smsbower_api_base_url=os.environ.get(
            "SMSBOWER_API_BASE_URL", SMSBOWER_API_BASE_URL
        ),
        paypal_project_dir=os.environ.get("PAYPAL_PROTOCOL_PROJECT_DIR", ""),
        paypal_python=os.environ.get("PAYPAL_PROTOCOL_PYTHON", ""),
        paypal_port=int(os.environ.get("PAYPAL_PROTOCOL_PORT", "18097")),
        auto_oaics_probe=os.environ.get(
            "HIDEMYEMAIL_AUTO_OAICS_PROBE", "1"
        ).strip().lower()
        in {"1", "true", "yes", "on"},
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=args.host, port=args.port)
    await site.start()
    url = f"http://{args.host}:{args.port}/"
    print(f"iCloud 隐藏邮箱本地服务已启动：{url}", flush=True)
    print("按 Ctrl+C 停止服务。", flush=True)
    if args.open_browser:
        webbrowser.open(url)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="iCloud Hide My Email local web service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--region", choices=["global", "china"], default="china")
    parser.add_argument("--data-dir", default=".")
    parser.add_argument("--cookie-file", default="cookies.txt")
    parser.add_argument("--output-file", default="emails.txt")
    parser.add_argument("--db-file", default=DEFAULT_DB_FILE)
    parser.add_argument("--inbox-config-file", default=DEFAULT_INBOX_CONFIG_FILE)
    parser.add_argument("--open-browser", action="store_true")
    return parser


def _load_local_env_file(path: Path) -> None:
    """Load local integration settings without overriding the process environment."""
    if not path.is_file():
        return
    allowed = {
        "ACCOUNT_WORKBENCH_URL",
        "ACCOUNT_WORKBENCH_IMPORT_TOKEN",
        "HIDEMYEMAIL_WEB_USERNAME",
        "HIDEMYEMAIL_WEB_PASSWORD",
        "HIDEMYEMAIL_INVENTORY_SERVER",
        "HIDEMYEMAIL_INVENTORY_URL",
        "HIDEMYEMAIL_INVENTORY_TOKEN",
        "HIDEMYEMAIL_INVENTORY_USERNAME",
        "HIDEMYEMAIL_INVENTORY_PASSWORD",
        "HIDEMYEMAIL_INVENTORY_LEASE_SECONDS",
        "HIDEMYEMAIL_INVENTORY_BATCH_SIZE",
        "HIDEMYEMAIL_INVENTORY_INTERVAL_SECONDS",
        "HIDEMYEMAIL_INVENTORY_SYNC_INTERVAL_SECONDS",
        "SMSBOWER_API_KEY",
        "SMSBOWER_MAIL_SERVICE",
        "SMSBOWER_MAX_PRICE",
        "SMSBOWER_API_BASE_URL",
        "PAYPAL_PROTOCOL_PROJECT_DIR",
        "PAYPAL_PROTOCOL_PYTHON",
        "PAYPAL_PROTOCOL_PORT",
    }
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in allowed or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value


def _configured_workbench_import_token() -> str:
    """Return the dedicated local workbench token.

    ``HME_IMPORT_TOKEN`` belongs to the workbench/remote-code-server side and
    is intentionally ignored here.  Reading it from the Windows user
    environment used to let a remote-service credential silently shadow the
    local ``.env`` value and caused intermittent import authentication errors.
    """
    return str(os.environ.get("ACCOUNT_WORKBENCH_IMPORT_TOKEN") or "").strip()


def _configured_inventory_service_token() -> str:
    """Return only the credential dedicated to the remote inventory client."""
    return str(os.environ.get("HIDEMYEMAIL_INVENTORY_TOKEN") or "").strip()


def main() -> None:
    _configure_utf8_stdio()
    _load_local_env_file(Path.cwd() / ".env")
    args = build_parser().parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("For safety, this service may only listen on the local machine.")
    try:
        asyncio.run(run_server(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
