import argparse
import asyncio
import hmac
import json
import os
import re
import secrets
import time
import webbrowser
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from .account_verifier import AccountVerificationManager, removed_account_emails
from .browser_tasks import (
    BrowserTaskManager,
    access_token_is_expired,
    load_account_record,
)
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


SESSION_COOKIE_NAME = "hme_session"
SESSION_MAX_AGE = 12 * 60 * 60
PUBLIC_PATHS = {"/login", "/api/login", "/healthz"}

PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline' https://cdn.jsdelivr.net; "
        "script-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


LOGIN_HTML = r"""<!doctype html>
<html lang="zh-CN" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <meta name="theme-color" content="#07131f">
  <title>登录 · iCloud 隐藏邮箱</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.min.css" integrity="sha384-L1dWfspMTHU/ApYnFiMz2QID/PlP1xCW9visvBdbEkOLkSSWsP6ZJWhPw6apiXxU" crossorigin="anonymous">
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
      --pico-font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
      --pico-primary: #6ea8fe;
      --pico-primary-background: #367bf5;
      --pico-primary-hover-background: #4a89f7;
      --pico-border-radius: 14px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; display: grid; place-items: center; overflow: hidden; color: #edf5ff;
      background: radial-gradient(circle at 12% 5%, rgba(54,123,245,.24), transparent 32rem),
                  radial-gradient(circle at 88% 92%, rgba(45,212,191,.12), transparent 30rem), #07131f;
    }
    body::before { content: ""; position: fixed; inset: 0; pointer-events: none; opacity: .18;
      background-image: linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px),
                        linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px);
      background-size: 32px 32px; mask-image: linear-gradient(to bottom, black, transparent 75%); }
    .login-shell { width: min(440px, calc(100% - 32px)); position: relative; z-index: 1; }
    .login {
      margin: 0; padding: clamp(26px, 6vw, 38px); border-radius: 26px;
      background: rgba(10,25,40,.88); border: 1px solid rgba(135,167,207,.2);
      box-shadow: 0 32px 90px rgba(0,0,0,.42); backdrop-filter: blur(22px);
    }
    .brand { display: flex; align-items: center; gap: 13px; margin-bottom: 28px; }
    .icon { width: 48px; height: 48px; display: grid; place-items: center; border-radius: 15px;
      color: #fff; background: linear-gradient(145deg, #65a4ff, #2768df); box-shadow: 0 12px 28px rgba(54,123,245,.3); }
    .icon svg { width: 23px; height: 23px; }
    .brand-copy { color: #8da4bf; font-size: 12px; letter-spacing: .14em; text-transform: uppercase; }
    h1 { margin: 3px 0 0; font-size: 28px; letter-spacing: -.035em; color: #f4f8ff; }
    .intro { margin: 0 0 26px; color: #8fa6c1; line-height: 1.65; font-size: 14px; }
    label { color: #adbed2; font-size: 13px; font-weight: 650; }
    input { margin-top: 8px; border-color: #29435e; background: rgba(5,17,29,.76); color: #eef6ff; }
    input:focus { border-color: #5d99f5; box-shadow: 0 0 0 3px rgba(75,139,244,.15); }
    button { width: 100%; margin: 10px 0 0; border: 0; padding: 13px 15px;
      color: white; background: linear-gradient(135deg, #438bf8, #2869df); font-weight: 750;
      box-shadow: 0 10px 24px rgba(40,105,223,.22); }
    button:disabled { opacity: .55; cursor: wait; }
    #notice { min-height: 21px; margin-top: 12px; color: #ff9b9f; font-size: 13px; }
    .safe { display: flex; align-items: center; justify-content: center; gap: 7px; margin-top: 19px;
      color: #6f88a5; font-size: 12px; }
    .safe svg { width: 14px; height: 14px; }
  </style>
</head>
<body>
  <main class="login-shell">
    <form class="login" id="loginForm">
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
      const response = await fetch(path, { ...options, headers, cache: "no-store" });
      const data = await response.json().catch(() => ({ ok: false, error: "服务返回了无效响应" }));
      if (response.status === 401) {
        location.replace("/login");
        throw new Error("登录已过期");
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
  <meta name="color-scheme" content="dark">
  <meta name="theme-color" content="#07131f">
  <title>隐藏邮箱控制台</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.min.css" integrity="sha384-L1dWfspMTHU/ApYnFiMz2QID/PlP1xCW9visvBdbEkOLkSSWsP6ZJWhPw6apiXxU" crossorigin="anonymous">
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
      --pico-font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
      --pico-font-size: 100%;
      --pico-primary: #75aaff;
      --pico-primary-background: #367bf5;
      --pico-primary-hover-background: #4b89f6;
      --pico-primary-focus: rgba(65, 132, 246, .22);
      --pico-border-radius: 13px;
      --canvas: #07131f;
      --surface: rgba(11, 27, 43, .88);
      --surface-strong: #0d2033;
      --surface-soft: rgba(17, 38, 59, .62);
      --border: rgba(124, 157, 195, .2);
      --text: #edf5ff;
      --muted: #829ab6;
      --success: #54d7ad;
      --warning: #f0b86a;
      --danger: #ff8f98;
    }
    * { box-sizing: border-box; }
    html { background: var(--canvas); }
    body {
      margin: 0; min-height: 100vh; color: var(--text);
      background: radial-gradient(circle at 5% -5%, rgba(54,123,245,.22), transparent 32rem),
                  radial-gradient(circle at 100% 20%, rgba(45,212,191,.09), transparent 29rem), var(--canvas);
    }
    body::before { content: ""; position: fixed; inset: 0; pointer-events: none; opacity: .15;
      background-image: linear-gradient(rgba(255,255,255,.032) 1px, transparent 1px),
                        linear-gradient(90deg, rgba(255,255,255,.032) 1px, transparent 1px);
      background-size: 36px 36px; mask-image: linear-gradient(to bottom, black, transparent 72%); }
    main.app-shell { width: min(1240px, calc(100% - 40px)); margin: 0 auto; padding: 34px 0 64px; position: relative; z-index: 1; }
    .app-header { display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 26px; }
    .brand { display: flex; align-items: center; gap: 15px; }
    .brand-mark { width: 48px; height: 48px; display: grid; place-items: center; flex: 0 0 auto; border-radius: 15px;
      color: #fff; background: linear-gradient(145deg, #65a4ff, #2768df); box-shadow: 0 12px 30px rgba(54,123,245,.27); }
    .brand-mark svg { width: 23px; height: 23px; }
    .eyebrow { color: #6e91bd; font-size: 11px; font-weight: 750; letter-spacing: .15em; text-transform: uppercase; }
    h1 { margin: 3px 0 0; font-size: clamp(25px, 3vw, 34px); letter-spacing: -.04em; color: #f3f8ff; }
    .subtitle { margin-top: 5px; color: var(--muted); font-size: 13px; }
    .header-actions { display: flex; align-items: center; gap: 10px; }
    .runtime-pill { display: flex; align-items: center; gap: 8px; height: 39px; padding: 0 13px; border: 1px solid var(--border);
      border-radius: 999px; color: #9cb1c9; background: rgba(9,23,37,.68); font-size: 12px; white-space: nowrap; }
    .runtime-dot { width: 7px; height: 7px; border-radius: 50%; background: #71839a; box-shadow: 0 0 0 4px rgba(113,131,154,.11); }
    .runtime-dot.ok { background: var(--success); box-shadow: 0 0 0 4px rgba(84,215,173,.11); }
    .runtime-dot.bad { background: var(--danger); box-shadow: 0 0 0 4px rgba(255,143,152,.11); }
    button {
      width: auto; min-height: 40px; margin: 0; border: 1px solid transparent; padding: 9px 14px; font-size: 13px;
      font-weight: 720; cursor: pointer; color: #c9d9eb; background: #17314b; box-shadow: none;
      transition: transform .16s ease, border-color .16s ease, background .16s ease, opacity .16s ease;
    }
    button:hover:not(:disabled) { transform: translateY(-1px); }
    button:disabled { opacity: .48; cursor: wait; }
    .icon-button { width: 40px; padding: 0; display: grid; place-items: center; border-color: var(--border); background: rgba(13,31,49,.74); }
    .icon-button svg { width: 17px; height: 17px; }
    .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
    .stat-card { margin: 0; padding: 17px 18px; display: flex; align-items: center; gap: 14px; border: 1px solid var(--border);
      border-radius: 17px; background: rgba(10,25,40,.7); box-shadow: 0 12px 35px rgba(0,0,0,.13); backdrop-filter: blur(14px); }
    .stat-icon { width: 38px; height: 38px; display: grid; place-items: center; border-radius: 12px; color: #89b8ff; background: rgba(54,123,245,.12); }
    .stat-icon.success { color: var(--success); background: rgba(84,215,173,.1); }
    .stat-icon.warning { color: var(--warning); background: rgba(240,184,106,.1); }
    .stat-icon.plus { color: #c8a8ff; background: rgba(148,99,235,.12); }
    .stat-icon svg { width: 18px; height: 18px; }
    .stat-value { font-size: 23px; line-height: 1; font-weight: 780; color: #f0f6ff; font-variant-numeric: tabular-nums; }
    .stat-label { margin-top: 5px; color: var(--muted); font-size: 11px; }
    .card {
      margin: 0; padding: 0; background: var(--surface); border: 1px solid var(--border);
      border-radius: 20px; box-shadow: 0 22px 65px rgba(0,0,0,.22); overflow: hidden; backdrop-filter: blur(18px);
    }
    .card + .card { margin-top: 16px; }
    .section-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; padding: 21px 22px; border-bottom: 1px solid var(--border); }
    .section-title { display: flex; gap: 12px; }
    .section-glyph { width: 36px; height: 36px; display: grid; place-items: center; flex: 0 0 auto; border-radius: 11px;
      color: #86b6ff; background: rgba(54,123,245,.11); }
    .section-glyph svg { width: 18px; height: 18px; }
    .section-head h2 { margin: 0; font-size: 17px; color: #edf5ff; }
    .section-copy { color: var(--muted); font-size: 12px; line-height: 1.55; margin-top: 5px; max-width: 760px; }
    .automation-body { padding: 18px 22px 21px; }
    .controls { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }
    .control-field { margin: 0; color: #9eb1c7; font-size: 11px; font-weight: 650; }
    .control-field input[type="number"] { width: 76px; height: 40px; margin: 5px 0 0; border-color: #29435e; background: rgba(5,17,29,.72); color: #eef6ff; padding: 8px 10px; }
    .switch-field { min-height: 40px; display: flex; align-items: center; gap: 8px; margin: 0 4px 0 0; color: #9eb1c7; font-size: 12px; }
    input[type="checkbox"] { accent-color: #438bf8; }
    .primary { border-color: rgba(113,169,255,.22); color: white; background: linear-gradient(135deg, #438bf8, #2869df); box-shadow: 0 9px 22px rgba(40,105,223,.2); }
    .danger { border-color: rgba(255,143,152,.14); color: #ffb2b8; background: rgba(214,63,75,.12); }
    .task { margin-top: 16px; padding: 14px 15px; border: 1px solid var(--border); border-radius: 14px; background: rgba(6,18,30,.46); }
    .task-topline { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .task-summary { color: #b8cbe2; font-size: 12px; }
    .task progress { width: 150px; height: 5px; margin: 0; accent-color: #438bf8; }
    .task-accounts { display: grid; gap: 6px; margin-top: 11px; max-height: 205px; overflow: auto; }
    .task-row { display: grid; grid-template-columns: minmax(190px, .8fr) 90px 1.6fr; gap: 10px; padding: 9px 11px; border-radius: 9px; background: rgba(17,38,59,.62); color: #8299b4; font-size: 11px; }
    .task-row .task-email { color: #d8e8fb; overflow-wrap: anywhere; }
    .task-log { margin-top: 9px; max-height: 120px; overflow: auto; white-space: pre-wrap; color: #657f9e; font: 11px/1.55 Consolas, monospace; }
    .list-head { display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 20px 22px 14px; }
    .list-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    #verifySummary { max-width: 440px; color: var(--muted); font-size: 11px; text-align: right; }
    .list-head h2 { margin: 0; font-size: 17px; color: #edf5ff; }
    #summary { color: var(--muted); font-size: 12px; margin-top: 5px; }
    .toolbar { display: grid; grid-template-columns: minmax(220px, 1fr) 135px 150px auto; gap: 9px; padding: 0 22px 17px; }
    .search-wrap { position: relative; }
    .search-wrap svg { position: absolute; top: 50%; left: 12px; width: 16px; height: 16px; color: #66809d; transform: translateY(-50%); pointer-events: none; }
    .toolbar input, .toolbar select { height: 40px; margin: 0; border-color: #29435e; background: rgba(5,17,29,.65); color: #dce9f8; font-size: 12px; }
    .toolbar input { padding-left: 37px; }
    .list { padding: 0 14px 14px; display: grid; gap: 9px; }
    .email-row {
      display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 16px;
      padding: 15px 16px; background: rgba(7,19,31,.58); border: 1px solid rgba(117,151,190,.17); border-radius: 14px;
      transition: border-color .16s ease, background .16s ease, transform .16s ease;
    }
    .email-row:hover { border-color: rgba(111,168,255,.34); background: rgba(11,28,45,.78); transform: translateY(-1px); }
    .identity { display: flex; align-items: center; gap: 13px; min-width: 0; }
    .avatar { width: 38px; height: 38px; display: grid; place-items: center; flex: 0 0 auto; border-radius: 12px; color: #8bb9ff;
      background: linear-gradient(145deg, rgba(54,123,245,.17), rgba(54,123,245,.07)); font-weight: 780; }
    .identity-copy { min-width: 0; }
    .address { font-size: 14px; font-weight: 650; color: #e8f2ff; overflow-wrap: anywhere; }
    .meta-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
    .status-badge { display: inline-flex; align-items: center; gap: 6px; padding: 3px 8px; border-radius: 999px; font-size: 10px; font-weight: 700; }
    .status-badge::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
    .status-badge.ready { color: var(--success); background: rgba(84,215,173,.09); }
    .status-badge.expired { color: var(--warning); background: rgba(240,184,106,.09); }
    .status-badge.pending { color: #8fa6c1; background: rgba(143,166,193,.09); }
    .plan-badge { display: inline-flex; align-items: center; padding: 3px 8px; border-radius: 999px; font-size: 10px; font-weight: 760; }
    .plan-badge.plus { color: #d2b7ff; background: rgba(148,99,235,.13); }
    .plan-badge.free { color: #8ec4ff; background: rgba(54,123,245,.11); }
    .plan-badge.unverified { color: #8fa6c1; background: rgba(143,166,193,.09); }
    .meta { color: #6f89a8; font-size: 10px; }
    .actions { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
    .action { min-height: 34px; padding: 7px 10px; border-color: rgba(103,160,235,.12); color: #8ec4ff; background: rgba(25,125,213,.1); white-space: nowrap; font-size: 11px; }
    .action:first-child { color: #dfefff; background: rgba(54,123,245,.2); }
    .empty { padding: 66px 20px; text-align: center; color: #728aa8; }
    .empty-icon { width: 44px; height: 44px; display: grid; place-items: center; margin: 0 auto 12px; border-radius: 14px; color: #718ba9; background: rgba(113,139,169,.08); }
    .empty-icon svg { width: 21px; height: 21px; }
    .empty strong { display: block; margin-bottom: 4px; color: #9ab0c8; font-size: 13px; }
    .error { color: var(--danger) !important; }
    .toast { position: fixed; right: 20px; bottom: 20px; z-index: 10; max-width: min(360px, calc(100% - 40px)); padding: 11px 14px;
      border: 1px solid var(--border); border-radius: 12px; color: #dceafa; background: rgba(13,32,51,.96); box-shadow: 0 18px 45px rgba(0,0,0,.32);
      font-size: 12px; opacity: 0; transform: translateY(10px); pointer-events: none; transition: .2s ease; }
    .toast.show { opacity: 1; transform: translateY(0); }
    .toast.error { border-color: rgba(255,143,152,.22); }
    @media (max-width: 760px) {
      main.app-shell { width: min(100% - 24px, 1240px); padding-top: 22px; }
      .app-header { align-items: flex-start; }
      .runtime-pill { display: none; }
      .stats-grid { grid-template-columns: repeat(2, 1fr); gap: 8px; }
      .stat-card { padding: 13px 15px; }
      .section-head { padding: 18px; }
      .automation-body { padding: 16px 18px 18px; }
      .list-head { align-items: flex-start; }
      .list-actions { max-width: 52%; }
      #verifySummary { text-align: right; }
      .toolbar { grid-template-columns: 1fr 130px 130px auto; padding-inline: 18px; }
      .email-row { grid-template-columns: 1fr; }
      .actions { justify-content: stretch; }
      .action { flex: 1; }
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
      .search-wrap { grid-column: 1 / span 2; }
      .toolbar select { grid-row: 2; }
      .toolbar .icon-button { grid-column: 3; grid-row: 1 / span 2; height: 100%; }
      .list-head { padding-inline: 18px; }
    }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; } }
  </style>
</head>
<body>
  <main class="app-shell">
    <header class="app-header">
      <div class="brand">
        <div class="brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="14" rx="3"></rect><path d="m4 7 8 6 8-6"></path></svg></div>
        <div>
          <div class="eyebrow">Private Relay Console</div>
          <h1>隐藏邮箱控制台</h1>
          <div class="subtitle">统一管理 iCloud 邮箱与 OpenAI Session</div>
        </div>
      </div>
      <div class="header-actions">
        <div class="runtime-pill"><span id="runtimeDot" class="runtime-dot"></span><span id="runtimeLabel">正在连接运行环境</span></div>
        <button id="logout" class="icon-button" aria-label="退出登录" title="退出登录"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M10 17l5-5-5-5M15 12H3"></path><path d="M14 3h5a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-5"></path></svg></button>
      </div>
    </header>

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
            <div class="section-copy">自动复用注册流程获取凭据；有效 Token 会跳过，验证码仅读取本次请求后的新邮件。</div>
          </div>
        </div>
      </div>
      <div class="automation-body">
        <div class="controls">
          <label class="switch-field"><input id="headless" type="checkbox" role="switch"> 无头浏览器</label>
          <label class="control-field">认证并发<input id="concurrency" type="number" min="1" max="10" value="1" aria-label="认证并发数"></label>
          <button id="fetchAll" class="primary">浏览器取全部</button>
          <button id="stopTask" class="danger" disabled>停止当前任务</button>
        </div>
        <div id="task" class="task" aria-live="polite">
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
          <div id="verifySummary">仅验证已保存 AT 的账号</div>
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
        <button id="refresh" class="icon-button" aria-label="刷新邮箱列表" title="刷新"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.34 5.66"></path><path d="M20 4v7h-7"></path></svg></button>
      </div>
      <div id="list" class="list"><div class="empty"><div class="empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="14" rx="3"></rect><path d="m4 7 8 6 8-6"></path></svg></div><strong>正在加载邮箱</strong>请稍候…</div></div>
    </section>
  </main>
  <div id="toast" class="toast" role="status" aria-live="polite"></div>

  <script>
    const localToken = __LOCAL_TOKEN__;
    const $ = (id) => document.getElementById(id);
    let currentItems = [];
    let taskPoll = null;
    let verificationPoll = null;
    let toastTimer = null;

    async function api(path, options = {}) {
      const headers = { ...(options.headers || {}) };
      if (options.method && options.method !== "GET") headers["X-Local-Token"] = localToken;
      if (options.body) headers["Content-Type"] = "application/json";
      const response = await fetch(path, { ...options, headers, cache: "no-store" });
      const data = await response.json().catch(() => ({ ok: false, error: "服务响应无效" }));
      if (response.status === 401) {
        location.replace("/login");
        throw new Error("登录已过期");
      }
      if (!response.ok || data.ok === false) throw new Error(data.error || `请求失败 (${response.status})`);
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
          await action();
          button.textContent = successLabel;
          showToast(successLabel);
          setTimeout(() => { button.textContent = original; }, 1200);
        } catch (error) {
          button.textContent = original;
          showToast(error.message, "error");
        } finally {
          button.disabled = false;
        }
      });
      return button;
    }

    async function copyCredential(email, kind) {
      const data = await api("/api/gpt-credential", {
        method: "POST", body: JSON.stringify({ email, kind })
      });
      await navigator.clipboard.writeText(data.value);
    }

    async function copyOpenAiCode(email) {
      const data = await api("/api/gpt-code", {
        method: "POST", body: JSON.stringify({ email })
      });
      await navigator.clipboard.writeText(data.code);
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
      const payload = { ...browserOptions() };
      if (emails) payload.emails = emails;
      const data = await api(path, { method: "POST", body: JSON.stringify(payload) });
      if (!data.started) {
        showToast(data.message || "无需重复获取");
      }
      await loadTask();
      return data;
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
      $("fetchAll").disabled = Boolean(data.running) || !runtime.available;
      $("stopTask").disabled = !data.running;
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
      }
    }

    function renderVerification(data) {
      const runtime = data.runtime || {};
      const statusNames = {
        idle: "尚未验证", running: "验证中", cancelling: "正在停止",
        completed: "验证完成", cancelled: "已停止",
      };
      if (!runtime.available) {
        $("verifySummary").className = "error";
        $("verifySummary").textContent = (runtime.errors || ["账号验证运行环境不可用"]).join("；");
      } else if (data.status === "idle") {
        $("verifySummary").className = "";
        $("verifySummary").textContent = "仅验证已保存 AT 的账号";
      } else {
        $("verifySummary").className = data.failed ? "error" : "";
        $("verifySummary").textContent = `${statusNames[data.status] || data.status} · ${data.completed || 0}/${data.total || 0} · Plus ${data.plus || 0} · Free ${data.free || 0} · 已删除 ${data.deleted || 0} · 失败 ${data.failed || 0}`;
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
      }
    }

    async function startVerification() {
      const verifiable = currentItems.filter((item) => item.hasSession).length;
      if (!verifiable) throw new Error("暂无已保存 Access Token 的账号");
      if (!confirm(`将在线验证 ${verifiable} 个账号；明确无效的账号会删除本地密码、AT 和 Session。是否继续？`)) return;
      const data = await api("/api/account-verification/start", {
        method: "POST", body: JSON.stringify({ concurrency: 3 })
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

    function render(items) {
      const root = $("list");
      root.replaceChildren();
      if (!items.length) {
        const filtered = currentItems.length > 0;
        root.append(renderEmpty(filtered ? "没有匹配的邮箱" : "暂无 GPT 邮箱", filtered ? "尝试更换搜索词或筛选条件" : "同步邮箱后会显示在这里"));
        return;
      }
      for (const item of items) {
        const row = document.createElement("div");
        row.className = "email-row";
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
        const planBadge = document.createElement("span");
        planBadge.className = `plan-badge ${item.accountType}`;
        planBadge.textContent = planLabel(item.accountType);
        metaLine.append(planBadge, badge);
        identityCopy.append(address, metaLine);
        identity.append(avatar, identityCopy);
        const actions = document.createElement("div");
        actions.className = "actions";
        actions.append(actionButton("浏览器获取", () => startBrowser([item.email]), "已启动"));
        actions.append(actionButton("复制邮箱", () => navigator.clipboard.writeText(item.email)));
        if (item.hasPassword) actions.append(actionButton("复制密码", () => copyCredential(item.email, "password")));
        if (item.hasSession) {
          actions.append(
            actionButton("复制 AT", () => copyCredential(item.email, "access_token")),
            actionButton("复制 Session", () => copyCredential(item.email, "session"))
          );
        }
        actions.append(actionButton("获取 OpenAI 码", () => copyOpenAiCode(item.email)));
        row.append(identity, actions);
        root.append(row);
      }
    }

    function applyFilters() {
      const query = $("search").value.trim().toLowerCase();
      const plan = $("planFilter").value;
      const status = $("statusFilter").value;
      const items = currentItems.filter((item) =>
        (!query || item.email.toLowerCase().includes(query)) &&
        (plan === "all" || item.accountType === plan) &&
        (status === "all" || item.sessionStatus === status)
      );
      render(items);
      const ready = currentItems.filter((item) => item.sessionStatus === "ready").length;
      $("summary").textContent = items.length === currentItems.length
        ? `${currentItems.length} 个 iCloud 邮箱 · ${ready} 个 Session 有效`
        : `显示 ${items.length} / ${currentItems.length} 个邮箱`;
    }

    async function load() {
      $("refresh").disabled = true;
      $("summary").className = "";
      $("summary").textContent = "正在加载…";
      try {
        const data = await api("/api/gpt-emails");
        currentItems = data.items;
        const plus = data.items.filter((item) => item.accountType === "plus").length;
        const free = data.items.filter((item) => item.accountType === "free").length;
        $("totalCount").textContent = data.items.length;
        $("plusCount").textContent = plus;
        $("freeCount").textContent = free;
        $("unverifiedCount").textContent = data.items.length - plus - free;
        applyFilters();
      } catch (error) {
        currentItems = [];
        $("totalCount").textContent = "—";
        $("plusCount").textContent = "—";
        $("freeCount").textContent = "—";
        $("unverifiedCount").textContent = "—";
        render([]);
        $("summary").className = "error";
        $("summary").textContent = error.message;
      } finally {
        $("refresh").disabled = false;
      }
    }

    async function logout() {
      try { await api("/api/logout", { method: "POST" }); }
      finally { location.replace("/login"); }
    }

    $("refresh").addEventListener("click", load);
    $("search").addEventListener("input", applyFilters);
    $("planFilter").addEventListener("change", applyFilters);
    $("statusFilter").addEventListener("change", applyFilters);
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
      if (!confirm("停止当前浏览器获取任务？")) return;
      try {
        const data = await api("/api/browser/stop", { method: "POST", body: "{}" });
        renderTask(data.task);
      } catch (error) { showToast(error.message, "error"); }
    });
    $("logout").addEventListener("click", logout);
    Promise.all([load(), loadTask(), loadVerification()]);
  </script>
</body>
</html>
"""


def _error_reason(result: dict) -> str:
    error = result.get("error", {}) if result else {}
    if isinstance(error, dict):
        return str(error.get("errorMessage") or error.get("reason") or "iCloud 请求失败")
    return str(result.get("reason") or error or "iCloud 请求失败")


def _resolve_data_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def _local_token_valid(request: web.Request, app: web.Application) -> bool:
    return hmac.compare_digest(
        request.headers.get("X-Local-Token", ""), app["local_token"]
    )


def _session_valid(request: web.Request) -> bool:
    if not request.app["web_password"]:
        return True
    supplied = request.cookies.get(SESSION_COOKIE_NAME, "")
    return bool(supplied) and hmac.compare_digest(
        supplied, request.app["session_token"]
    )


@web.middleware
async def auth_middleware(
    request: web.Request, handler
) -> web.StreamResponse:
    if request.path in PUBLIC_PATHS or _session_valid(request):
        return await handler(request)
    if request.path.startswith("/api/"):
        return web.json_response(
            {"ok": False, "error": "请先登录"},
            status=401,
            headers={"Cache-Control": "no-store"},
        )
    raise web.HTTPFound("/login")


def _inbox_error_message(error: Exception) -> str:
    message = str(error).lower()
    if "authentication" in message or "login" in message or "auth" in message:
        return "IMAP 登录失败，请确认已开启 IMAP，并使用授权码或应用专用密码"
    if isinstance(error, (TimeoutError, OSError)):
        return "无法连接 IMAP 服务器，请检查主机、端口和网络"
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
        return str(payload.get("password") or "").strip()
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


def _latest_gpt_code(
    db_file: Path, email: str, identities: list[dict], since: str = ""
) -> dict | None:
    target = email.strip().lower()
    since_at = _parse_timestamp(since)
    conn = connect_db(str(db_file))
    try:
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
                return {"code": code, "receivedAt": received_at}
        return None
    finally:
        conn.close()


def _browser_email_items(db_file: Path, identities: list[dict]) -> list[dict]:
    activity = {
        item["email"]: item for item in _gpt_email_items(db_file, identities)
    }
    removed = removed_account_emails(db_file)
    items: list[dict] = []
    for identity in identities:
        email = str(identity.get("hme") or "").strip().lower()
        if not email or email in removed:
            continue
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
        access_token = str(
            account.get("access_token") or account.get("accessToken") or ""
        ).strip()
        token_expired = bool(access_token) and access_token_is_expired(access_token)
        account_type = str(account.get("account_type") or "").lower()
        if account_type not in {"plus", "free"}:
            account_type = "unverified"
        timestamp = identity.get("createTimestamp")
        created_at = ""
        if isinstance(timestamp, (int, float)):
            created_at = datetime.fromtimestamp(
                timestamp / 1000, timezone.utc
            ).astimezone().isoformat()
        current.update(
            {
                "createdAt": created_at,
                "hasPassword": bool(str(account.get("password") or "")),
                "hasSession": bool(access_token),
                "tokenExpired": token_expired,
                "sessionStatus": (
                    "expired" if token_expired else "ready" if access_token else "pending"
                ),
                "accountType": account_type,
                "verifiedAt": str(account.get("verified_at") or ""),
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


def create_app(
    *,
    base_dir: Path,
    cookie_file: str = "cookies.txt",
    output_file: str = "emails.txt",
    db_file: str = DEFAULT_DB_FILE,
    inbox_config_file: str = DEFAULT_INBOX_CONFIG_FILE,
    region: str = "china",
    web_password: str = "",
    inbox_sync_interval: int = 30,
    target_project_dir: str = "",
    target_python: str = "",
    browser_service_url: str = "http://127.0.0.1:8765",
    force_browser_headless: bool = False,
) -> web.Application:
    app = web.Application(
        client_max_size=16 * 1024, middlewares=[auth_middleware]
    )
    app["local_token"] = secrets.token_urlsafe(32)
    app["web_password"] = web_password
    app["session_token"] = secrets.token_urlsafe(48)
    app["login_attempts"] = []
    app["cookie_file"] = _resolve_data_path(base_dir, cookie_file)
    app["output_file"] = _resolve_data_path(base_dir, output_file)
    app["db_file"] = _resolve_data_path(base_dir, db_file)
    app["inbox_config_file"] = _resolve_data_path(base_dir, inbox_config_file)
    app["region"] = region
    app["inbox_sync_interval"] = max(15, inbox_sync_interval)
    app["inbox_background_last_sync"] = ""
    app["inbox_background_error"] = ""
    app["generate_lock"] = asyncio.Lock()
    app["inbox_sync_lock"] = asyncio.Lock()
    browser_source = (
        Path(target_project_dir).resolve()
        if target_project_dir
        else (
            base_dir.resolve().parent
            / "openai-register-paylink-ui-dist-20260706-README-deploy"
        )
    )
    app["browser_manager"] = BrowserTaskManager(
        target_project_dir=browser_source,
        service_url=browser_service_url,
        worker_token=app["local_token"],
        db_file=app["db_file"],
        python_executable=Path(target_python) if target_python else None,
        force_headless=force_browser_headless,
    )
    app["verification_manager"] = AccountVerificationManager(
        target_project_dir=browser_source,
        db_file=app["db_file"],
        python_executable=app["browser_manager"].python_executable,
    )

    async def active_icloud_identities() -> list[dict]:
        cookie_path: Path = app["cookie_file"]
        if not cookie_path.exists() or cookie_path.stat().st_size == 0:
            raise RuntimeError("iCloud Cookie 尚未准备好")
        async with RichHideMyEmail(
            cookie_file=str(cookie_path), region=app["region"]
        ) as hme:
            result = await hme.list_email()
        if not result or not result.get("success"):
            raise RuntimeError(_error_reason(result))
        return [
            row
            for row in result.get("result", {}).get("hmeEmails", [])
            if row.get("isActive") and row.get("hme") and row.get("anonymousId")
        ]

    async def background_inbox_sync() -> None:
        while True:
            try:
                config_path: Path = app["inbox_config_file"]
                if config_path.exists():
                    config = load_config(str(config_path))
                    async with app["inbox_sync_lock"]:
                        await asyncio.to_thread(
                            sync_inbox, config, str(app["db_file"]), 100
                        )
                    app["inbox_background_last_sync"] = (
                        datetime.now().astimezone().isoformat()
                    )
                    app["inbox_background_error"] = ""
            except asyncio.CancelledError:
                raise
            except Exception as error:
                app["inbox_background_error"] = _inbox_error_message(error)
            await asyncio.sleep(app["inbox_sync_interval"])

    async def background_inbox_context(_: web.Application):
        task = asyncio.create_task(background_inbox_sync())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app.cleanup_ctx.append(background_inbox_context)

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

    async def login_page(request: web.Request) -> web.Response:
        if not app["web_password"] or _session_valid(request):
            raise web.HTTPFound("/")
        return web.Response(
            text=LOGIN_HTML,
            content_type="text/html",
            headers=PAGE_HEADERS,
        )

    async def login_api(request: web.Request) -> web.Response:
        if not app["web_password"]:
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
        supplied = str(payload.get("password") or "")
        if not hmac.compare_digest(supplied, app["web_password"]):
            attempts.append(now)
            return web.json_response(
                {"ok": False, "error": "密码错误"},
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
        body = GPT_INDEX_HTML.replace(
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
        try:
            identities = await active_icloud_identities()
        except RuntimeError as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=502
            )
        items = await asyncio.to_thread(
            _browser_email_items, app["db_file"], identities
        )
        return web.json_response(
            {
                "ok": True,
                "items": items,
                "updatedAt": datetime.now().astimezone().isoformat(),
            },
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
        if not email.endswith("@icloud.com") or len(email) > 320:
            return web.json_response(
                {"ok": False, "error": "邮箱地址无效"}, status=400
            )
        if kind not in {"password", "access_token", "session"}:
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
            }[kind]
            return web.json_response(
                {"ok": False, "error": f"当前邮箱暂无 {name} 数据"}, status=404
            )
        return web.json_response(
            {"ok": True, "value": value}, headers={"Cache-Control": "no-store"}
        )

    async def gpt_code(request: web.Request) -> web.Response:
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
        since = str(payload.get("since") or "").strip()
        if not email.endswith("@icloud.com") or len(email) > 320:
            return web.json_response(
                {"ok": False, "error": "邮箱地址无效"}, status=400
            )
        config_path: Path = app["inbox_config_file"]
        if not config_path.exists():
            return web.json_response(
                {"ok": False, "error": "iCloud 收件箱尚未配置"}, status=503
            )
        try:
            config = load_config(str(config_path))
            async with app["inbox_sync_lock"]:
                await asyncio.to_thread(
                    sync_inbox, config, str(app["db_file"]), 100
                )
        except Exception as error:
            return web.json_response(
                {"ok": False, "error": _inbox_error_message(error)}, status=502
            )
        try:
            identities = await active_icloud_identities()
        except RuntimeError as error:
            return web.json_response(
                {"ok": False, "error": str(error)}, status=502
            )
        item = await asyncio.to_thread(
            _latest_gpt_code, app["db_file"], email, identities, since
        )
        if not item:
            return web.json_response(
                {"ok": False, "error": "暂未获取到该邮箱的 OpenAI 验证码"},
                status=404,
            )
        return web.json_response(
            {"ok": True, **item}, headers={"Cache-Control": "no-store"}
        )

    async def browser_status(_: web.Request) -> web.Response:
        return web.json_response(
            {"ok": True, **app["browser_manager"].snapshot()},
            headers={"Cache-Control": "no-store"},
        )

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
            or not 1 <= concurrency <= 5
        ):
            return web.json_response(
                {"ok": False, "error": "验证并发必须是 1–5 的整数"}, status=400
            )
        try:
            task = app["verification_manager"].start(concurrency=concurrency)
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
        headless = bool(payload.get("headless", False))
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
            if access_token and not access_token_is_expired(access_token):
                skipped += 1
                continue
            accounts.append(
                {"email": email, "password": str(record.get("password") or "")}
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
        if not generated:
            return web.json_response(
                {"ok": False, "error": "未能生成地址，可能触发了 Apple 频率限制"}, status=502
            )
        return web.json_response({"ok": True, "emails": list(generated)})

    async def inbox_status(_: web.Request) -> web.Response:
        config_path: Path = app["inbox_config_file"]
        if not config_path.exists():
            return web.json_response(
                {"ok": True, "configured": False, "codeCount": 0},
                headers={"Cache-Control": "no-store"},
            )
        try:
            config = load_config(str(config_path))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return web.json_response(
                {"ok": True, "configured": False, "codeCount": 0, "error": "收件箱配置文件无效"},
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
                "backgroundInterval": app["inbox_sync_interval"],
                "lastBackgroundSync": app["inbox_background_last_sync"],
                "backgroundError": app["inbox_background_error"],
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
            config = load_config(str(config_path))
            async with app["inbox_sync_lock"]:
                inserted = await asyncio.to_thread(
                    sync_inbox, config, str(app["db_file"]), limit
                )
        except Exception as error:
            return web.json_response(
                {"ok": False, "error": _inbox_error_message(error)}, status=502
            )
        items = await asyncio.to_thread(_code_items, app["db_file"], 30)
        return web.json_response({"ok": True, "inserted": len(inserted), "items": items})

    app.router.add_get("/login", login_page)
    app.router.add_post("/api/login", login_api)
    app.router.add_post("/api/logout", logout_api)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", index)
    app.router.add_get("/api/gpt-emails", gpt_emails)
    app.router.add_post("/api/gpt-credential", gpt_credential)
    app.router.add_post("/api/gpt-code", gpt_code)
    app.router.add_get("/api/browser/status", browser_status)
    app.router.add_post("/api/browser/fetch-all", browser_fetch_all)
    app.router.add_post("/api/browser/fetch-selected", browser_fetch_selected)
    app.router.add_post("/api/browser/stop", browser_stop)
    app.router.add_get("/api/account-verification/status", verification_status)
    app.router.add_post("/api/account-verification/start", verification_start)
    app.router.add_post("/api/account-verification/stop", verification_stop)
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
        web_password=os.environ.get("HIDEMYEMAIL_WEB_PASSWORD", ""),
        inbox_sync_interval=int(
            os.environ.get("HIDEMYEMAIL_INBOX_SYNC_INTERVAL", "30")
        ),
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


def main() -> None:
    args = build_parser().parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("For safety, this service may only listen on the local machine.")
    try:
        asyncio.run(run_server(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
