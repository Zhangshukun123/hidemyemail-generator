"use strict";

const elements = {
  form: document.getElementById("lookupForm"),
  email: document.getElementById("email"),
  submit: document.getElementById("submitButton"),
  generate: document.getElementById("generateButton"),
  copyAddress: document.getElementById("copyAddressButton"),
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

const POLL_INTERVAL_MS = 4000;
const POLL_TIMEOUT_MS = 60000;
let activeRun = 0;
let activeController = null;
let toastTimer = null;
let currentCursor = "";

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

function setBusy(busy) {
  elements.submit.disabled = busy;
  elements.submit.querySelector("span").textContent = busy ? "正在等待" : "获取验证码";
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

function randomAlias() {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  const random = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `zk${Date.now().toString(36)}${random}@zkgmail.com`;
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
  currentCursor = String(data.cursor || "");
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
  const runId = ++activeRun;
  const deadline = Date.now() + POLL_TIMEOUT_MS;
  elements.result.hidden = true;
  setBusy(true);

  while (runId === activeRun && Date.now() < deadline) {
    const secondsLeft = Math.max(1, Math.ceil((deadline - Date.now()) / 1000));
    setStatus("loading", "正在等待邮件", `正在查询 ${email}，剩余 ${secondsLeft} 秒…`);
    try {
      const { response, data } = await requestCode(email, afterCursor);
      if (runId !== activeRun) return;
      if (response.ok && data.ok && data.code) {
        showResult(data);
        setBusy(false);
        activeController = null;
        return;
      }
      if (response.status !== 404) {
        const message = data.error || data.message || "获取验证码失败";
        setStatus("error", "查询失败", message);
        setBusy(false);
        activeController = null;
        return;
      }
    } catch (error) {
      if (error.name === "AbortError" || runId !== activeRun) return;
      setStatus("error", "网络连接失败", "暂时无法连接接码服务，请稍后重试。");
      setBusy(false);
      activeController = null;
      return;
    }
    await delay(POLL_INTERVAL_MS, runId);
  }

  if (runId === activeRun) {
    setBusy(false);
    setStatus("error", "等待超时", "一分钟内没有收到验证码，请确认收件地址后重试。");
  }
}

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const email = normalizeEmail(elements.email.value);
  elements.email.value = email;
  elements.copyAddress.disabled = !validEmail(email);
  if (!validEmail(email)) {
    elements.result.hidden = true;
    setStatus("error", "邮箱格式不正确", "请输入完整的 zkgmail.com 邮箱地址。");
    elements.email.focus();
    return;
  }
  startLookup(email);
});

elements.email.addEventListener("input", () => {
  elements.copyAddress.disabled = !validEmail(normalizeEmail(elements.email.value));
});

elements.generate.addEventListener("click", async () => {
  if (activeRun && !elements.cancel.hidden) stopLookup("已切换到新的接码地址。");
  const email = randomAlias();
  elements.email.value = email;
  elements.copyAddress.disabled = false;
  elements.result.hidden = true;
  setStatus("idle", "新地址已生成", "复制此地址完成验证，然后点击“获取验证码”。");
  await copyText(email, "新邮箱已复制");
});

elements.copyAddress.addEventListener("click", () => {
  const email = normalizeEmail(elements.email.value);
  if (validEmail(email)) copyText(email, "邮箱已复制");
});

elements.copyCode.addEventListener("click", () => {
  if (elements.code.textContent) copyText(elements.code.textContent, "验证码已复制");
});

elements.nextCode.addEventListener("click", () => {
  const email = normalizeEmail(elements.resultEmail.textContent);
  if (validEmail(email) && currentCursor) startLookup(email, currentCursor);
});

elements.cancel.addEventListener("click", () => stopLookup());
