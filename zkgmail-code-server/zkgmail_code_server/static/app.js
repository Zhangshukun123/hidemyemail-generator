"use strict";

const elements = {
  form: document.getElementById("lookupForm"),
  email: document.getElementById("email"),
  submit: document.getElementById("submitButton"),
  cancel: document.getElementById("cancelButton"),
  statusPanel: document.getElementById("statusPanel"),
  statusTitle: document.getElementById("statusTitle"),
  statusMessage: document.getElementById("statusMessage"),
  result: document.getElementById("result"),
  code: document.getElementById("code"),
  resultEmail: document.getElementById("resultEmail"),
  receivedAt: document.getElementById("receivedAt"),
  copyCode: document.getElementById("copyCodeButton"),
  nextCode: document.getElementById("nextCodeButton"),
  toast: document.getElementById("toast"),
};

const POLLING_POLICY = Object.freeze({
  intervalMs: 4000,
  timeoutMs: 180 * 1000,
  timeoutLabel: "3 分钟",
});
let activeRun = 0;
let activeController = null;
let toastTimer = null;

class LookupCursorModel {
  constructor() {
    this.email = "";
    this.cursor = "";
  }

  remember(email, cursor) {
    this.email = normalizeEmail(email);
    this.cursor = String(cursor || "");
  }

  afterCursorFor(email) {
    return normalizeEmail(email) === this.email ? this.cursor : "";
  }
}

const cursorModel = new LookupCursorModel();

async function bootstrapAccess() {
  const rawFragment = location.hash.slice(1);
  if (!rawFragment) return true;
  const entries = [...new URLSearchParams(rawFragment).entries()];
  if (entries.length !== 1 || entries[0][0] !== "invite" || !entries[0][1]) {
    history.replaceState(null, "", location.pathname + location.search);
    setStatus("error", "邀请链接无效", "请联系分享者获取新的邀请链接。");
    return false;
  }
  let inviteToken = entries[0][1];
  history.replaceState(null, "", location.pathname + location.search);
  setStatus("loading", "正在验证邀请链接", "正在建立安全访问会话…");
  try {
    const requestBody = JSON.stringify({ token: inviteToken });
    inviteToken = "";
    const response = await fetch("/api/access", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: requestBody,
      credentials: "same-origin",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      setStatus("error", "邀请链接无效", data.error || "请联系分享者获取新的邀请链接。");
      return false;
    }
    elements.email.value = String(data.email || "");
    elements.email.readOnly = true;
    setStatus("idle", "访问已授权", "输入接码邮箱后即可获取最新验证码。");
    return true;
  } catch (_) {
    setStatus("error", "连接失败", "暂时无法建立访问会话，请稍后重试。");
    return false;
  }
}

function normalizeEmail(value) {
  return String(value || "").trim().toLowerCase();
}

function validEmail(value) {
  return /^[a-z0-9](?:[a-z0-9._+\-]{0,62}[a-z0-9])?@zkgmail\.com$/i.test(value);
}

function setStatus(state, title, message) {
  elements.statusPanel.dataset.state = state;
  elements.statusTitle.textContent = title;
  elements.statusMessage.textContent = message;
}

function setBusy(busy, waitingForNext = false) {
  elements.submit.disabled = busy;
  const availableCursor = cursorModel.afterCursorFor(elements.email.value);
  elements.submit.querySelector("span").textContent = busy
    ? waitingForNext ? "正在等待下一条" : "正在等待"
    : availableCursor ? "等待下一条验证码" : "获取验证码";
  elements.nextCode.disabled = busy || !cursorModel.afterCursorFor(elements.resultEmail.textContent);
  elements.cancel.hidden = !busy;
}

function showToast(message) {
  window.clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.add("visible");
  toastTimer = window.setTimeout(() => elements.toast.classList.remove("visible"), 1500);
}

async function copyText(value, successMessage) {
  try {
    await navigator.clipboard.writeText(value);
  } catch (_) {
    const helper = document.createElement("textarea");
    helper.value = value;
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.appendChild(helper);
    helper.select();
    document.execCommand("copy");
    helper.remove();
  }
  showToast(successMessage);
}

function delay(milliseconds, runId) {
  return new Promise((resolve) => {
    const timer = window.setTimeout(resolve, milliseconds);
    if (runId !== activeRun) {
      window.clearTimeout(timer);
      resolve();
    }
  });
}

async function requestCode(email, afterCursor = "") {
  activeController = new AbortController();
  const payload = { email };
  if (afterCursor) payload.afterCursor = afterCursor;
  const response = await fetch("/api/code/latest", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "application/json" },
    body: JSON.stringify(payload),
    signal: activeController.signal,
  });
  const data = await response.json().catch(() => ({}));
  return { response, data };
}

function showResult(data) {
  cursorModel.remember(data.email, data.cursor);
  elements.code.textContent = data.code;
  elements.resultEmail.textContent = data.email;
  const date = new Date(data.receivedAt);
  elements.receivedAt.textContent = Number.isNaN(date.getTime())
    ? String(data.receivedAt || "")
    : date.toLocaleString();
  elements.result.hidden = false;
  setStatus("success", "验证码已收到", "点击下方验证码即可复制。重复查询不会使验证码失效。");
}

function stopLookup(message = "已停止等待验证码。") {
  activeRun += 1;
  if (activeController) activeController.abort();
  activeController = null;
  setBusy(false);
  setStatus("idle", "等待查询", message);
}

async function startLookup(email, afterCursor = "") {
  if (activeController) activeController.abort();
  activeController = null;
  const runId = ++activeRun;
  const deadline = Date.now() + POLLING_POLICY.timeoutMs;
  let lastAttemptHadNetworkError = false;
  elements.result.hidden = true;
  setBusy(true, Boolean(afterCursor));

  while (runId === activeRun && Date.now() < deadline) {
    const secondsLeft = Math.max(1, Math.ceil((deadline - Date.now()) / 1000));
    const title = afterCursor ? "正在等待下一条验证码" : "正在等待邮件";
    const message = afterCursor
      ? `已忽略上一条验证码，正在等待 ${email} 的新邮件，剩余 ${secondsLeft} 秒…`
      : `正在查询 ${email}，剩余 ${secondsLeft} 秒…`;
    setStatus("loading", title, message);
    try {
      const { response, data } = await requestCode(email, afterCursor);
      lastAttemptHadNetworkError = false;
      if (runId !== activeRun) return;
      if (response.ok && data.ok && data.code) {
        const responseCursor = String(data.cursor || "");
        const isUpdated = !afterCursor || (responseCursor && responseCursor !== afterCursor);
        if (isUpdated) {
          showResult(data);
          setBusy(false);
          activeController = null;
          return;
        }
      }
      if (response.status !== 404 && !(response.ok && data.ok && data.code)) {
        const message = data.error || data.message || "获取验证码失败";
        setStatus("error", "查询失败", message);
        setBusy(false);
        activeController = null;
        return;
      }
    } catch (error) {
      if (error.name === "AbortError" || runId !== activeRun) return;
      lastAttemptHadNetworkError = true;
      setStatus(
        "loading",
        "正在恢复连接",
        `服务器可能正在重启，将自动重试，剩余 ${secondsLeft} 秒…`,
      );
      activeController = null;
    }
    await delay(POLLING_POLICY.intervalMs, runId);
  }

  if (runId === activeRun) {
    setBusy(false);
    if (lastAttemptHadNetworkError) {
      setStatus("error", "连接超时", "服务器暂时无法连接，请稍后再次获取验证码。");
      return;
    }
    const message = afterCursor
      ? `${POLLING_POLICY.timeoutLabel}内没有收到下一条验证码，请确认已重新发送后再试。`
      : `${POLLING_POLICY.timeoutLabel}内没有收到验证码，请确认收件地址后重试。`;
    setStatus("error", "等待超时", message);
  }
}

const accessReady = bootstrapAccess();

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!(await accessReady)) return;
  const email = normalizeEmail(elements.email.value);
  elements.email.value = email;
  if (!validEmail(email)) {
    elements.result.hidden = true;
    setStatus("error", "邮箱格式不正确", "请输入完整的 zkgmail.com 邮箱地址。");
    elements.email.focus();
    return;
  }
  startLookup(email, cursorModel.afterCursorFor(email));
});

elements.email.addEventListener("input", () => {
  if (elements.cancel.hidden) setBusy(false);
});

elements.copyCode.addEventListener("click", () => {
  if (elements.code.textContent) copyText(elements.code.textContent, "验证码已复制");
});

elements.nextCode.addEventListener("click", () => {
  const email = normalizeEmail(elements.resultEmail.textContent);
  const afterCursor = cursorModel.afterCursorFor(email);
  if (validEmail(email) && afterCursor) startLookup(email, afterCursor);
});

elements.cancel.addEventListener("click", () => stopLookup());
