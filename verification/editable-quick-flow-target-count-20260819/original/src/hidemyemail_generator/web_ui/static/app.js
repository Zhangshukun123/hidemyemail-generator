(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const localToken = window.__HME_LOCAL_TOKEN__;
  const pageDetails = {
    overview: ["CONTROL CENTER", "控制台", "集中查看任务、账号与运行状态"],
    accounts: ["ACCOUNT MANAGEMENT", "账号管理", "集中管理账号资产，并以紧凑流程发起注册任务"],
    "quick-flow": ["ONE-CLICK PIPELINE", "一键注册、提链并支付", "生成所选 PayPal 链接后自动选择代理与手机号并启动协议支付"],
    network: ["NETWORK ROUTING", "代理与线路", "独立管理所有注册方式共用的代理出口"],
    "card-links": ["CHECKOUT WORKSPACE", "提连中心", "提取 gpt-link 严格 0 链接或 PayPal DE/EUR OAICS 授权链接"],
    "pp-payment": ["PAYPAL WORKSPACE", "PP 支付", "PayPal BA 协议授权与支付任务"],
    verification: ["VERIFICATION WORKSPACE", "验证记录", "批量验证账号、套餐与 Session 状态"],
    settings: ["SYSTEM SETTINGS", "系统设置", "管理邮箱、浏览器、集成与安全配置"],
  };
  const cardLinkExtractionModes = {
    paypal_us: {
      country: "US",
      summary: "使用第一代理完成 US/USD Checkout、优惠、金额校验与 PayPal Confirm / Approve",
      label: "PayPal / 美国 · USD · 全程第一代理",
      checks: ["✓ Session 可用", "✓ 地区 US", "✓ 币种 USD", "✓ 返回 PayPal 授权链接"],
      button: "提取 PayPal 链接",
      success: "PayPal US/USD 授权链接已提取并复制",
      singleProxy: true,
      fixedProxyCountry: true,
      createProxyPreference: "paypalUsCreate",
      createProxyCountry: "US",
      promotionProxyPreference: "paypalUsFollowup",
      promotionProxyCountry: "US",
      createProxyLabel: "第一代理国家",
      promotionProxyLabel: "第一代理国家",
      targetAmount: true,
    },
    paypal_gb: {
      country: "GB",
      summary: "使用英国第一代理完成 GB/GBP Checkout、优惠、金额校验与 PayPal Confirm / Approve",
      label: "PayPal / 英国 · GBP · 全程第一代理",
      checks: ["✓ Session 可用", "✓ 地区 GB", "✓ 币种 GBP", "✓ 优惠后金额 0", "✓ 返回 PayPal 授权链接"],
      button: "提取 PayPal 链接",
      success: "PayPal GB/GBP 授权链接已提取并复制",
      singleProxy: true,
      fixedProxyCountry: true,
      createProxyPreference: "paypalGbCreate",
      createProxyCountry: "GB",
      createProxyLabel: "英国第一代理国家",
      fixedTargetAmount: "0",
    },
    ph_hosted: {
      country: "PH",
      summary: "选择账号后生成严格 0 hosted 链接",
      label: "gpt-link · PH / PHP hosted · 双代理严格 0",
      checks: ["✓ Session 可用", "✓ 地区 PH", "✓ 币种 PHP", "✓ 金额必须为 0"],
      button: "提取严格 0",
      success: "严格 0 hosted 链接已提取并复制",
      singleProxy: false,
      createProxyPreference: "phCreate",
      createProxyCountry: "US",
      promotionProxyPreference: "phPromotion",
      promotionProxyCountry: "TR",
    },
    de_oaics_paypal: {
      country: "DE",
      summary: "创建 DE/EUR oaics_ Checkout 并提取 PayPal BA 授权链接",
      label: "PayPal / 严格 0 · DE / EUR",
      checks: ["✓ Session 可用", "✓ 未生成同模式链接", "✓ DE / EUR", "✓ 优惠后金额 0"],
      button: "提取 PayPal 链接",
      success: "PayPal DE/EUR 链接已提取并复制",
      singleProxy: true,
      createProxyPreference: "de",
      createProxyCountry: "DE",
      fixedTargetAmount: "0",
    },
  };

  function cardLinkRuntimeConfig(method) {
    return cardLinkExtractionModes[method] || cardLinkExtractionModes.ph_hosted;
  }

  class CardLinkCountryPolicy {
    constructor(modes) {
      this.modes = modes;
    }

    resolve(method, candidate = "") {
      const config = this.modes[method];
      if (config?.fixedProxyCountry) return config.country;
      return String(candidate || "").trim().toUpperCase();
    }

    normalizeSnapshot(snapshot = {}) {
      const method = String(snapshot.cardLinkMethod || "");
      const config = this.modes[method];
      if (!config?.fixedProxyCountry) return snapshot;
      const country = this.resolve(method);
      return {
        ...snapshot,
        extractionFirstCountry: country,
        extractionSecondCountry: country,
      };
    }
  }

  const cardLinkCountryPolicy = new CardLinkCountryPolicy(cardLinkExtractionModes);
  class PayPalWorkspaceModel {
    constructor(origin) { this.origin = origin; }
    frameUrl(baseUrl = "/paypal-pay/", jobId = "", theme = "dark") {
      const url = new URL(baseUrl || "/paypal-pay/", this.origin);
      url.searchParams.set("embedded", "1"); url.searchParams.set("theme", theme || "dark");
      if (jobId) url.searchParams.set("job", jobId); else url.searchParams.delete("job");
      return url.origin === this.origin ? url.pathname + url.search + url.hash : url.href;
    }
  }

  class PayPalWorkspaceView {
    constructor(frame) {
      this.frame = frame;
      this.empty = $("paypalPaymentEmpty");
      this.status = $("paypalServiceStatus");
      this.navState = $("paypalNavState");
    }
    theme() { return document.documentElement.dataset.theme || "dark"; }
    lifecycle() {
      return {
        loaded: this.frame.dataset.loaded === "1",
        ready: this.frame.dataset.ready === "1",
        loadFailed: this.frame.dataset.loadError === "1",
      };
    }
    resetLifecycle() {
      delete this.frame.dataset.loaded; delete this.frame.dataset.ready; delete this.frame.dataset.loadError;
    }
    setEmpty(state, title, detail, hidden = false) {
      this.empty.hidden = hidden; this.empty.dataset.state = state;
      this.empty.querySelector("strong").textContent = title;
      this.empty.querySelector("p").textContent = detail;
    }
    showLoading() {
      this.frame.hidden = false; this.status.className = "badge warning"; this.status.textContent = "正在载入"; this.navState.textContent = "…";
      this.setEmpty("loading", "正在载入协议支付工作台", "服务已连接，正在等待支付页面完成初始化。");
    }
    showReady() {
      this.status.className = "badge success"; this.status.textContent = "服务已连接"; this.navState.textContent = "在线"; this.empty.hidden = true;
    }
    showLoadError() {
      this.frame.hidden = false; this.status.className = "badge error"; this.status.textContent = "载入失败"; this.navState.textContent = "异常";
      this.setEmpty("error", "协议支付页面载入失败", "支付页面未在 10 秒内响应，请检查服务后重试。");
    }
    showUnavailable(error = "") {
      const failed = Boolean(error); this.frame.hidden = true;
      this.status.className = "badge " + (failed ? "error" : "warning"); this.status.textContent = failed ? "启动失败" : "正在启动"; this.navState.textContent = failed ? "异常" : "—";
      this.setEmpty(failed ? "error" : "loading", failed ? "协议支付服务连接失败" : "正在启动协议支付服务", error || "服务就绪后会自动载入协议支付工作台。");
    }
    fallbackUrl() { return this.frame.dataset.src || "/paypal-pay/"; }
    load(url) { this.frame.src = url; this.frame.dataset.loaded = "1"; }
  }

  class PayPalWorkspacePresenter {
    constructor(model, view) { this.model = model; this.view = view; this.readyTimer = 0; this.serviceRunning = false; }
    mount() {
      window.addEventListener("message", (event) => {
        if (!this.serviceRunning || event.origin !== location.origin || event.source !== this.view.frame.contentWindow || event.data?.type !== "paypal-workspace-ready") return;
        clearTimeout(this.readyTimer); this.view.frame.dataset.ready = "1"; delete this.view.frame.dataset.loadError; this.view.showReady();
      });
    }
    open(baseUrl = "", jobId = "") {
      this.serviceRunning = true; this.view.resetLifecycle(); this.view.showLoading();
      clearTimeout(this.readyTimer);
      this.readyTimer = setTimeout(() => {
        if (this.view.frame.dataset.ready === "1") return;
        this.view.frame.dataset.loadError = "1"; this.view.showLoadError();
      }, 10000);
      this.view.load(this.model.frameUrl(baseUrl || this.view.fallbackUrl(), jobId, this.view.theme()));
    }
    render(service = {}) {
      if (!service.running) {
        this.serviceRunning = false; clearTimeout(this.readyTimer); this.view.resetLifecycle(); this.view.showUnavailable(service.error || "");
        return;
      }
      this.serviceRunning = true;
      const lifecycle = this.view.lifecycle();
      if (!lifecycle.loaded) { this.open(service.url); return; }
      if (lifecycle.loadFailed) this.view.showLoadError();
      else if (lifecycle.ready) this.view.showReady();
      else this.view.showLoading();
    }
  }
  const paypalWorkspacePresenter = new PayPalWorkspacePresenter(
    new PayPalWorkspaceModel(location.origin), new PayPalWorkspaceView($("paypalPaymentFrame")),
  );
  paypalWorkspacePresenter.mount();
  function cardLinkPaymentPayload(method) {
    const config = cardLinkRuntimeConfig(method);
    return {
      target_amount: config.fixedTargetAmount || $("cardLinkTargetAmount").value.trim(),
    };
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function quickFlowFailureExplanation(item = {}) {
    return window.QuickFlowQuotaEligibilityModel.explainFailure(item);
  }

  function redactTerminalLogText(value) {
    return String(value ?? "")
      .replace(/([a-z][a-z0-9+.-]*:\/\/)([^@\s/:]+):([^@\s]+)@/gi,
        "$1[REDACTED]:[REDACTED]@")
      .replace(/\b(Bearer|Basic)\s+[A-Za-z0-9._~+\/-]+={0,2}/gi, "$1 [REDACTED]")
      .replace(
        /(["']?)(authorization|password|passwd|pwd|api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|auth[_-]?token|session[_-]?(?:token|id)|session|token|cookie|set-cookie|secret|totp|otp|verification[_-]?code|email[_-]?code|one[_-]?time[_-]?(?:code|password)|two(?:[_-]?factor)?[_-]?secret|private[_-]?key|proxy[_-]?password|openai[_-]?key)\1(\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}\]]+)/gi,
        "$1$2$1$3[REDACTED]"
      )
      .replace(/\b(cookie|set-cookie)(\s*:\s*)[^\r\n]+/gi, "$1$2[REDACTED]")
      .replace(/(验证码|校验码|动态码|一次性密码)(\s*(?:为|是)\s*)\d{4,8}/g,
        "$1$2[REDACTED]")
      .replace(/\b(verification\s+code|one[- ]time\s+(?:code|password))(\s+is\s+)\d{4,8}\b/gi,
        "$1$2[REDACTED]")
      .replace(/\bsk-(?:proj-)?[A-Za-z0-9_-]{6,}\b/gi, "[REDACTED_API_KEY]")
      .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g,
        "[REDACTED_JWT]");
  }

  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      hour12: false,
    });
  }

  function formatClock(value) {
    const date = new Date(value || "");
    if (Number.isNaN(date.getTime())) return "--:--:--";
    return date.toLocaleTimeString("zh-CN", {
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    });
  }

  function formatLogTimestamp(value) {
    const date = new Date(value || "");
    if (Number.isNaN(date.getTime())) return "---- -- -- --:--:--.---";
    const pad = (part, width = 2) => String(part).padStart(width, "0");
    return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" +
      pad(date.getDate()) + " " + pad(date.getHours()) + ":" +
      pad(date.getMinutes()) + ":" + pad(date.getSeconds()) + "." +
      pad(date.getMilliseconds(), 3);
  }

  function formatElapsed(startValue, endValue) {
    const start = new Date(startValue || "");
    const end = endValue ? new Date(endValue) : new Date();
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return "00:00";
    const seconds = Math.max(0, Math.floor((end.getTime() - start.getTime()) / 1000));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remaining = seconds % 60;
    const parts = [minutes, remaining].map((part) => String(part).padStart(2, "0"));
    if (hours) parts.unshift(String(hours).padStart(2, "0"));
    return parts.join(":");
  }

  function abbreviateEmail(value) {
    const email = String(value || "");
    const at = email.indexOf("@");
    if (at < 1) return email || "—";
    const local = email.slice(0, at);
    return local.slice(0, 3) + (local.length > 3 ? "…" : "") + email.slice(at);
  }

  function taskStatusMeta(status) {
    const states = {
      idle: ["空闲", "idle", "—"],
      running: ["进行中", "running", "•"],
      completed: ["已完成", "completed", "✓"],
      failed: ["失败", "failed", "!"],
      cancelling: ["停止中", "cancelling", "•"],
      cancelled: ["已停止", "cancelled", "×"],
    };
    return states[status] || states.idle;
  }

  function normalizeWorkspaceTaskStatus(task = {}) {
    const status = String(task.status || task.phase || "").trim().toLowerCase();
    if (task.running || task.starting || ["running", "active", "cancelling"].includes(status)) {
      return "running";
    }
    if (["queued", "pending", "waiting", "scheduled"].includes(status)) return "queued";
    if (["completed", "complete", "success", "succeeded", "done"].includes(status)) {
      return "completed";
    }
    if (["failed", "failure", "error"].includes(status)) return "failed";
    if (["cancelled", "canceled", "stopped", "aborted"].includes(status)) return "stopped";
    return "idle";
  }

  function workspaceTaskRows(state) {
    const rows = [];
    const numberValue = (value, fallback = 0) => {
      const parsed = Number(value);
      return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
    };
    const firstNumber = (task, keys, fallback = 0) => {
      for (const key of keys) {
        if (task[key] !== undefined && task[key] !== null && task[key] !== "") {
          return numberValue(task[key], fallback);
        }
      }
      return fallback;
    };
    const addTask = ({ kind, name, group, flow, route, task = {}, status, ...overrides }) => {
      if (!task || typeof task !== "object") return;
      const normalizedStatus = status || normalizeWorkspaceTaskStatus(task);
      const id = String(overrides.id || task.processId || task.runId || task.id || task.taskId || "");
      const hasActivity = normalizedStatus !== "idle" || task.startedAt || task.finishedAt;
      if (!hasActivity) return;
      const emails = Array.isArray(task.emails) ? task.emails.filter(Boolean) : [];
      const email = String(overrides.email || task.email || emails[0] || task.currentEmail || "");
      const failed = firstNumber(task, ["failed", "failureCount"], normalizedStatus === "failed" ? 1 : 0);
      const succeeded = firstNumber(task, ["succeeded", "successCount", "generated", "registered"],
        normalizedStatus === "completed" ? 1 : 0);
      const completed = firstNumber(task, ["completed", "completedCount", "processed"], succeeded + failed);
      const totalFallback = emails.length || Math.max(completed, succeeded + failed, 1);
      const total = firstNumber(task, ["total", "requested", "targetCount", "target_count"], totalFallback);
      const threads = Math.max(1, firstNumber(task,
        ["effectiveConcurrency", "concurrency", "runningCount", "workers"], 1));
      const successRate = completed > 0
        ? Math.round((succeeded / completed) * 1000) / 10
        : normalizedStatus === "completed" ? 100 : 0;
      rows.push({
        kind,
        name: String(overrides.name || name),
        group: String(overrides.group || group),
        flow: String(overrides.flow || flow),
        route,
        id,
        email,
        status: normalizedStatus,
        threads,
        completed,
        total,
        succeeded,
        failed,
        successRate,
        startedAt: String(overrides.startedAt || task.startedAt || ""),
        finishedAt: String(overrides.finishedAt || task.finishedAt || ""),
        message: String(overrides.message || task.currentAction || task.message || ""),
      });
    };
    const addTaskGroup = (parent, metadata) => {
      const tasks = Array.isArray(parent?.tasks) ? parent.tasks : [];
      if (tasks.length) {
        tasks.forEach((task, index) => addTask({
          ...metadata,
          task,
          name: task.processLabel || task.name || metadata.name + " · " + (index + 1),
        }));
      } else {
        addTask({ ...metadata, task: parent || {} });
      }
    };

    addTaskGroup(state.registrationTask, {
      kind: "registration", name: "账号批量注册", group: "账号注册",
      flow: "注册新账号", route: "accounts",
    });
    addTaskGroup(state.protocolRegistrationTask, {
      kind: "protocol", name: "iCloud 协议注册", group: "账号注册",
      flow: "协议注册", route: "accounts",
    });
    addTask({
      kind: "browser", name: "浏览器账号任务", group: "浏览器",
      flow: "浏览器自动化", route: "accounts", task: state.browserTask || {},
    });
    addTask({
      kind: "verification", name: "账号验证", group: "验证记录",
      flow: "账号验证", route: "verification", task: state.verificationTask || {},
    });
    const quickFlows = Array.isArray(state.quickFlows) && state.quickFlows.length
      ? state.quickFlows : state.quickFlow?.runId ? [state.quickFlow] : [];
    quickFlows.forEach((task, index) => addTask({
      kind: "pipeline", name: task.name || "注册提链流水线 · " + (index + 1),
      group: "快速流程", flow: "注册、提链并支付", route: "quick-flow", task,
    }));
    (state.registrationTask?.failureRecords || []).forEach((record, index) => addTask({
      kind: "registration-failure",
      name: "注册失败记录 · " + (index + 1),
      group: "账号注册",
      flow: taskStageLabel(record.failedStage || "failed"),
      route: "accounts",
      task: record,
      status: "failed",
      id: record.processId || record.taskId || "failure-" + index,
      email: record.email || (record.emails || [])[0] || "",
      startedAt: record.startedAt || record.recordedAt || "",
      finishedAt: record.finishedAt || record.recordedAt || "",
      message: record.failureReason || record.message || "注册失败",
    }));
    const priority = { running: 0, queued: 1, failed: 2, completed: 3, stopped: 4, idle: 5 };
    return rows.sort((left, right) => {
      const statusDelta = priority[left.status] - priority[right.status];
      if (statusDelta) return statusDelta;
      const leftAt = new Date(left.finishedAt || left.startedAt || 0).getTime() || 0;
      const rightAt = new Date(right.finishedAt || right.startedAt || 0).getTime() || 0;
      return rightAt - leftAt;
    });
  }

  function taskStageLabel(stage) {
    const labels = {
      idle: "准备", running: "执行", prepare: "准备", provider: "邮箱服务", protocol_auth: "Mail Auth",
      network: "网络", browser: "浏览器", openai_auth: "OpenAI 登录",
      google_oauth: "页面纠正", security: "安全验证", password: "密码",
      email_verification: "邮箱验证", profile: "基础资料", session: "Session",
      two_factor: "2FA", completed: "完成",
    };
    return labels[stage] || "执行";
  }

  function taskStageGroup(stage) {
    if (["idle", "running", "prepare", "provider", "network", "browser"].includes(stage)) return "prepare";
    if (["openai_auth", "google_oauth", "password"].includes(stage)) return "auth";
    if (["security", "email_verification"].includes(stage)) return "verify";
    if (stage === "profile") return "profile";
    if (["session", "two_factor", "completed"].includes(stage)) return "session";
    return "prepare";
  }

  function inferLogContext(item) {
    const message = String(item.message || "");
    const lower = message.toLowerCase();
    let stage = item.stage || "running";
    let location = item.location || "注册任务";
    let action = item.action || message || "处理浏览器任务";
    let status = item.status || (/失败|错误|异常|超时/.test(message)
      ? "error" : /停止|取消|仍是|未完成|跳过/.test(message)
        ? "warning" : /等待|请在|手动|继续监测/.test(message)
          ? "waiting" : /成功|已保存|已开启|校验通过/.test(message) ? "success" : "active");
    if (!item.stage && /google 账号登录页面|google 登录页|google 登录要求|google 页面返回|从 google 返回/i.test(message)) {
      stage = "google_oauth"; location = "Google 登录页";
      action = /全新指纹|更换指纹/.test(message)
        ? "关闭当前浏览器并更换指纹" : "返回 OpenAI 并重新输入邮箱";
    } else if (!item.stage && /安全验证|security-check|challenge/.test(lower)) {
      stage = "security"; location = "安全验证页"; action = "等待手动完成安全验证，完成后自动继续";
    } else if (!item.stage && /密码|password/.test(lower)) {
      stage = "password"; location = "OpenAI 密码页"; action = "检查并处理密码登录";
    } else if (!item.stage && /验证码|verification|otp/.test(lower)) {
      stage = "email_verification"; location = "邮箱验证码页"; action = "识别并处理邮箱验证码";
    } else if (!item.stage && /邮箱登录页|邮箱注册字段|邮箱输入框|google 账号入口已禁用|未点击 google 登录按钮/i.test(message)) {
      stage = "openai_auth"; location = "OpenAI 邮箱登录页"; action = "输入邮箱并进入密码流程";
    } else if (!item.stage && /session|cookie/.test(lower)) {
      stage = "session"; location = "OpenAI Session 接口"; action = "获取并保存 Session / Cookie";
    }
    return { ...item, stage, location, action, status };
  }

  function initials(email) {
    return String(email || "?").split("@")[0].split(/[._-]/).slice(0, 2)
      .map((part) => part.slice(0, 1).toUpperCase()).join("") || "?";
  }

  function planName(type) {
    return type === "plus" ? "Plus" : type === "free" ? "Free" : "待验证";
  }

  function sessionName(status) {
    return status === "ready" ? "可用" : status === "expired" ? "已过期" : "未获取";
  }

  function badge(label, kind = "") {
    return '<span class="badge ' + kind + '">' + escapeHtml(label) + "</span>";
  }

  function metricCard(label, value, note, tone = "", icon = "•") {
    return '<article class="metric-card"><span class="metric-icon ' + tone + '">' +
      escapeHtml(icon) + '</span><div><span>' + escapeHtml(label) + '</span><strong>' +
      escapeHtml(value) + '</strong><small>' + escapeHtml(note) + "</small></div></article>";
  }

  function countryOptionLabel(item) {
    return (item?.label || item?.code || "") + " (" + (item?.code || "") + ")";
  }

  function matchProxyCountry(value, countries) {
    const query = String(value || "").trim().toLocaleLowerCase("zh-CN");
    if (!query) return null;
    const exact = countries.find((item) =>
      item.code.toLocaleLowerCase("zh-CN") === query ||
      item.label.toLocaleLowerCase("zh-CN") === query ||
      countryOptionLabel(item).toLocaleLowerCase("zh-CN") === query
    );
    if (exact) return exact;
    const matches = countries.filter((item) =>
      item.code.toLocaleLowerCase("zh-CN").includes(query) ||
      item.label.toLocaleLowerCase("zh-CN").includes(query) ||
      countryOptionLabel(item).toLocaleLowerCase("zh-CN").includes(query)
    );
    return matches.length === 1 ? matches[0] : null;
  }

  function cardLinkMarkedForMethod(item, method) {
    return item?.cardLinkStatus === "cs_live" && item?.cardLinkMethod === method;
  }

  function hasGeneratedCardLinkForMethod(item, method) {
    return Boolean(
      item?.cardLink &&
      item.cardLinkStatus === "generated" &&
      item.cardLinkMethod === method
    );
  }

  function cardLinkEligible(item, method) {
    return Boolean(
      item?.email?.toLowerCase().endsWith("@icloud.com") &&
      item.sessionStatus === "ready" &&
      !item.cardLink
    );
  }

  class ApiGateway {
    constructor(token) { this.token = token; }

    async request(path, options = {}) {
      const headers = { ...(options.headers || {}) };
      if (this.token) headers["X-Local-Token"] = this.token;
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
        setTimeout(() => location.reload(), 120);
        throw new Error(data.error || "服务已更新，正在刷新页面");
      }
      if (!response.ok || data.ok === false) {
        const error = new Error(data.error || "请求失败 (" + response.status + ")");
        error.status = response.status;
        error.code = data.code || "";
        error.canDeleteLocal = Boolean(data.canDeleteLocal);
        error.logs = Array.isArray(data.logs) ? data.logs : [];
        error.attemptCount = Number(data.attemptCount || 0);
        error.attemptLimit = Number(data.attemptLimit || 0);
        error.attemptsExhausted = Boolean(data.attemptsExhausted);
        error.retryable = data.retryable !== false;
        throw error;
      }
      return data;
    }

    get(path) { return this.request(path); }
    post(path, payload = {}) {
      return this.request(path, { method: "POST", body: JSON.stringify(payload) });
    }
  }

  class ObservableStore {
    constructor(initialState) {
      this.state = Object.freeze({ ...initialState });
      this.listeners = new Set();
    }
    subscribe(listener) {
      this.listeners.add(listener);
      return () => this.listeners.delete(listener);
    }
    patch(changes) {
      this.state = Object.freeze({ ...this.state, ...changes });
      for (const listener of this.listeners) listener(this.state);
    }
  }

  class HashRouter {
    constructor(routes, fallback = "overview") {
      this.routes = routes;
      this.fallback = fallback;
      this.current = "";
    }
    resolve() {
      const candidate = location.hash.replace(/^#/, "");
      return this.routes[candidate] ? candidate : this.fallback;
    }
    navigate(route) {
      const target = this.routes[route] ? route : this.fallback;
      if (location.hash !== "#" + target) history.replaceState(null, "", "#" + target);
      this.activate(target);
    }
    activate(route) {
      const target = this.routes[route] ? route : this.fallback;
      this.current = target;
      document.querySelectorAll("[data-view]").forEach((view) => {
        view.hidden = view.dataset.view !== target;
      });
      document.querySelectorAll(".nav-item[data-route], .workspace-route-tab[data-route]").forEach((button) => {
        const active = button.dataset.route === target;
        button.classList.toggle("active", active);
        if (button.classList.contains("workspace-route-tab")) {
          if (active) button.setAttribute("aria-current", "page");
          else button.removeAttribute("aria-current");
        }
      });
      const details = pageDetails[target];
      $("viewEyebrow").textContent = details[0];
      $("viewTitle").textContent = details[1];
      $("viewSubtitle").textContent = details[2];
      $("commandCenterLabel").textContent = "账号工作台 — " + details[1];
      document.title = details[1] + " · 账号工作台";
      this.routes[target]();
    }
    start() {
      window.addEventListener("hashchange", () => this.activate(this.resolve()));
      this.activate(this.resolve());
    }
  }

  class CommandBus {
    constructor(notify) {
      this.handlers = new Map();
      this.notify = notify;
    }
    register(name, handler) { this.handlers.set(name, handler); }
    async execute(name, context) {
      const handler = this.handlers.get(name);
      if (!handler) return;
      const button = context.element?.closest("button");
      const previousDisabled = button?.disabled;
      if (button) button.disabled = true;
      try {
        const message = await handler(context);
        if (message) this.notify(message);
      } catch (error) {
        if (error.name !== "AbortError") this.notify(error.message, "error");
      } finally {
        if (button) button.disabled = Boolean(previousDisabled);
      }
    }
  }

  class NavigationSidebarView {
    constructor() {
      this.root = document.documentElement;
      this.sidebar = $("workbenchSidebar");
      this.collapseButton = $("sidebarCollapseButton");
      this.expandRail = $("sidebarExpandRail");
    }

    render(collapsed) {
      const expanded = !collapsed;
      this.root.dataset.sidebarCollapsed = String(collapsed);
      this.sidebar.hidden = collapsed;
      this.sidebar.setAttribute("aria-hidden", String(collapsed));
      this.sidebar.toggleAttribute("inert", collapsed);
      this.collapseButton.setAttribute("aria-expanded", String(expanded));
      this.expandRail.setAttribute("aria-expanded", String(expanded));
      this.expandRail.hidden = expanded;
    }

    focusToggle(collapsed) {
      requestAnimationFrame(() => {
        (collapsed ? this.expandRail : this.collapseButton).focus();
      });
    }
  }

  class NavigationSidebarPresenter {
    constructor(view, storage = localStorage) {
      this.view = view;
      this.storage = storage;
      this.collapsed = false;
    }

    restore() {
      try {
        this.collapsed = this.storage.getItem("hme_sidebar_collapsed") === "true";
      } catch (_error) {
        this.collapsed = false;
      }
      this.view.render(this.collapsed);
    }

    toggle() {
      this.collapsed = !this.collapsed;
      try {
        this.storage.setItem("hme_sidebar_collapsed", String(this.collapsed));
      } catch (_error) {
        // The visual state remains usable when persistent storage is unavailable.
      }
      this.view.render(this.collapsed);
      this.view.focusToggle(this.collapsed);
    }
  }

  class RegistrationConfigModel {
    constructor(storage = localStorage) {
      this.storage = storage;
      this.storageKey = "hme_registration_config_collapsed";
    }

    loadCollapsed() {
      try {
        return this.storage.getItem(this.storageKey) === "1";
      } catch (_error) {
        return false;
      }
    }

    saveCollapsed(collapsed) {
      const normalized = Boolean(collapsed);
      try {
        this.storage.setItem(this.storageKey, normalized ? "1" : "0");
      } catch (_error) {
        // The panel remains interactive when persistent storage is unavailable.
      }
      return normalized;
    }
  }

  class RegistrationConfigView {
    constructor() {
      this.deck = document.querySelector("#accountsView .registration-command-deck");
      this.panel = $("registrationConfigPanel");
      this.toggleButton = $("registrationConfigToggle");
      this.toggleLabel = this.toggleButton.querySelector("span");
      this.toggleIcon = this.toggleButton.querySelector("b");
    }

    render(collapsed) {
      const expanded = !collapsed;
      this.panel.hidden = collapsed;
      this.deck.dataset.configCollapsed = String(collapsed);
      this.toggleButton.setAttribute("aria-expanded", String(expanded));
      this.toggleButton.setAttribute("aria-label", collapsed ? "展开注册配置" : "收起注册配置");
      this.toggleButton.title = collapsed ? "展开注册配置" : "收起注册配置";
      this.toggleLabel.textContent = collapsed ? "展开配置" : "收起配置";
      this.toggleIcon.textContent = collapsed ? "⌄" : "⌃";
    }

    focusToggle() {
      requestAnimationFrame(() => this.toggleButton.focus());
    }
  }

  class RegistrationConfigPresenter {
    constructor(model, view) {
      this.model = model;
      this.view = view;
      this.collapsed = false;
    }

    restore() {
      this.collapsed = this.model.loadCollapsed();
      this.view.render(this.collapsed);
    }

    toggle() {
      this.collapsed = this.model.saveCollapsed(!this.collapsed);
      this.view.render(this.collapsed);
      this.view.focusToggle();
    }

    present() {
      this.view.render(this.collapsed);
    }
  }

  class QuickFlowConfigModel {
    constructor(storage = localStorage) {
      this.storage = storage;
      this.snapshotKey = "hme_quick_flow_config_v1";
      this.collapsedKey = "hme_quick_flow_config_collapsed";
      this.legacyKeys = {
        registrationProvider: "hme_quick_registration_provider",
        registrationMode: "hme_quick_registration_mode",
        protocolSetupCredentials: "hme_quick_protocol_setup_credentials",
        concurrency: "hme_quick_registration_concurrency",
        roxyTargetCount: "hme_quick_registration_target",
        registrationProxyMode: "hme_quick_registration_proxy_mode",
        registrationProxyCountry: "hme_quick_registration_proxy_country",
        cardLinkMethod: "hme_quick_card_link_method",
        extractionCount: "hme_quick_extraction_count",
        extractionProxyMode: "hme_quick_extraction_proxy_mode",
        extractionFirstCountry: "hme_quick_extraction_first_country",
        extractionSecondCountry: "hme_quick_extraction_second_country",
        promotionProxyChoice: "hme_quick_promotion_proxy_choice",
        targetAmount: "hme_quick_paypal_us_target_amount", postPaymentPhoneBinding: "hme_quick_post_payment_phone_binding",
      };
    }
    getItem(key) {
      try {
        return this.storage.getItem(key);
      } catch (_error) {
        return null;
      }
    }

    normalize(candidate = {}) {
      const enumValue = (value, allowed, fallback) => allowed.includes(String(value))
        ? String(value) : fallback;
      const integerValue = (value, minimum, maximum, fallback) => {
        const parsed = Number(value);
        return String(Number.isInteger(parsed) && parsed >= minimum && parsed <= maximum
          ? parsed : fallback);
      };
      const textValue = (value, fallback = "") => String(value ?? fallback).trim();
      const booleanValue = (value, fallback = false) => value === null || value === undefined || value === "" ? Boolean(fallback)
        : typeof value === "boolean" ? value : ["1", "true", "yes", "on"].includes(String(value).trim().toLowerCase());
      const roxyTargetCount = integerValue(
        candidate.roxyTargetCount ?? candidate.targetCount,
        1,
        100,
        1,
      );
      const snapshot = {
        registrationProvider: enumValue(candidate.registrationProvider, ["inventory", "zkgmail"], "inventory"),
        registrationMode: enumValue(candidate.registrationMode, ["headless", "headed", "roxy", "protocol"], "headless"),
        protocolSetupCredentials: true,
        concurrency: integerValue(candidate.concurrency, 1, 10, 1),
        targetCount: integerValue(candidate.targetCount, 1, 100, Number(roxyTargetCount)),
        roxyTargetCount,
        registrationProxyMode: textValue(candidate.registrationProxyMode, "direct") || "direct",
        registrationProxyCountry: textValue(candidate.registrationProxyCountry, "NL") || "NL",
        cardLinkMethod: enumValue(candidate.cardLinkMethod, ["de_oaics_paypal", "paypal_us", "paypal_gb"], "de_oaics_paypal"),
        extractionCount: integerValue(candidate.extractionCount, 1, 100, 1),
        extractionProxyMode: textValue(candidate.extractionProxyMode, "dynamic") || "dynamic",
        extractionFirstCountry: textValue(candidate.extractionFirstCountry, "DE") || "DE",
        extractionSecondCountry: textValue(candidate.extractionSecondCountry, "DE") || "DE",
        promotionProxyChoice: enumValue(candidate.promotionProxyChoice, ["first", "second"], "first"),
        targetAmount: textValue(candidate.targetAmount),
        postPaymentPhoneBinding: booleanValue(candidate.postPaymentPhoneBinding, false),
        savedAt: textValue(candidate.savedAt),
        collapsed: Boolean(candidate.collapsed),
      };
      return cardLinkCountryPolicy.normalizeSnapshot(snapshot);
    }
    load() {
      const legacy = Object.fromEntries(Object.entries(this.legacyKeys).map(([field, key]) =>
        [field, this.getItem(key)]
      ));
      let snapshot = {};
      try {
        snapshot = JSON.parse(this.getItem(this.snapshotKey) || "{}") || {};
      } catch (_error) {
        snapshot = {};
      }
      return this.normalize({
        ...legacy,
        ...snapshot,
        collapsed: this.getItem(this.collapsedKey) === "1",
      });
    }

    write(snapshot) {
      try {
        this.storage.setItem(this.snapshotKey, JSON.stringify(snapshot));
        this.storage.setItem(this.collapsedKey, snapshot.collapsed ? "1" : "0");
        Object.entries(this.legacyKeys).forEach(([field, key]) => {
          this.storage.setItem(key, String(snapshot[field] ?? ""));
        });
      } catch (_error) {
        // The form remains usable when persistent storage is unavailable.
      }
      return snapshot;
    }
    restore() {
      return this.write(this.load());
    }

    save(candidate) {
      return this.write(this.normalize({
        ...candidate,
        savedAt: new Date().toISOString(),
      }));
    }
    saveCollapsed(collapsed) {
      const snapshot = this.load();
      snapshot.collapsed = Boolean(collapsed);
      return this.write(snapshot);
    }
  }

  class QuickFlowConfigView {
    constructor() {
      this.details = $("quickFlowConfigDetails");
      this.savedState = $("quickFlowSavedConfigState");
      this.savedSummary = $("quickFlowSavedConfigSummary"); this.postPaymentPhoneBinding = $("quickPostPaymentPhoneBinding"); this.protocolSetupCredentials = $("quickProtocolSetupCredentials");
      this.fields = {
        registrationProvider: $("quickRegistrationProvider"),
        registrationMode: $("quickRegistrationMode"),
        concurrency: $("quickRegistrationConcurrency"),
        targetCount: $("quickRegistrationTargetCount"),
        registrationProxyMode: $("quickRegistrationProxyMode"),
        registrationProxyCountry: $("quickRegistrationProxyCountry"),
        cardLinkMethod: $("quickCardLinkMethod"),
        extractionCount: $("quickExtractionCount"),
        extractionProxyMode: $("quickExtractionProxyMode"),
        extractionFirstCountry: $("quickExtractionFirstProxyCountry"),
        extractionSecondCountry: $("quickExtractionSecondProxyCountry"),
        promotionProxyChoice: $("quickPromotionProxyChoice"),
        targetAmount: $("quickCardLinkTargetAmount"),
      };
    }
    read() {
      return {
        ...Object.fromEntries(Object.entries(this.fields).map(([field, element]) => [field, element.value])),
        protocolSetupCredentials: true,
        postPaymentPhoneBinding: Boolean(this.postPaymentPhoneBinding.checked),
        collapsed: !this.details.open,
      };
    }
    apply(snapshot) {
      Object.entries(this.fields).forEach(([field, element]) => {
        const value = String(snapshot[field] ?? "");
        if (element.tagName !== "SELECT" || [...element.options].some((option) => option.value === value)) {
          element.value = value;
        }
      });
      this.protocolSetupCredentials.checked = true;
      this.protocolSetupCredentials.disabled = true;
      this.postPaymentPhoneBinding.checked = snapshot.postPaymentPhoneBinding === true;
      this.details.open = !snapshot.collapsed;
    }

    selectedLabel(field, fallback) {
      const element = this.fields[field];
      return element?.selectedOptions?.[0]?.textContent?.trim() || fallback;
    }

    render(snapshot, stateLabel) {
      const current = this.read();
      const summary = [
        this.selectedLabel("registrationProvider", current.registrationProvider),
        this.selectedLabel("registrationMode", current.registrationMode),
        current.registrationMode === "protocol" ? "密码 + 2FA" : "",
        "并发 " + current.concurrency + " / 目标 " + current.targetCount,
        this.selectedLabel("cardLinkMethod", current.cardLinkMethod),
        "每号 " + current.extractionCount + " 次",
        current.postPaymentPhoneBinding ? "确认 Plus 后接码" : "确认 Plus 即结束",
        this.selectedLabel("registrationProxyMode", current.registrationProxyMode),
        this.selectedLabel("extractionProxyMode", current.extractionProxyMode),
      ].filter(Boolean).join(" · ");
      this.savedSummary.textContent = summary;
      this.savedSummary.title = summary;
      this.savedState.textContent = snapshot.savedAt
        ? stateLabel + " · " + formatClock(snapshot.savedAt)
        : stateLabel;
    }

    bind(onChange, onToggle) {
      this.details.addEventListener("change", (event) => { if (Object.values(this.fields).includes(event.target) || event.target === this.protocolSetupCredentials || event.target === this.postPaymentPhoneBinding) onChange(); });
      this.details.addEventListener("toggle", () => onToggle(!this.details.open));
    }
  }

  class QuickFlowConfigPresenter {
    constructor(model, view) {
      this.model = model;
      this.view = view;
    }

    restore() {
      const snapshot = this.model.restore();
      this.view.apply(snapshot);
      this.view.render(snapshot, snapshot.savedAt ? "已恢复上次配置" : "配置将自动保存");
      return snapshot;
    }

    bind() {
      this.view.bind(
        () => this.persist(),
        (collapsed) => {
          const snapshot = this.model.saveCollapsed(collapsed);
          this.view.render(snapshot, collapsed ? "配置已保存，可直接开始" : "配置已自动保存");
        },
      );
    }

    persist(stateLabel = "配置已自动保存") {
      const current = this.view.read();
      const previous = this.model.load();
      const snapshot = this.model.save({
        ...current,
        roxyTargetCount: current.registrationMode === "roxy"
          ? current.targetCount : previous.roxyTargetCount,
      });
      this.view.render(snapshot, stateLabel);
      return snapshot;
    }

    save() { return this.persist("配置已保存"); }

    present() {
      const snapshot = this.model.load();
      this.view.render(snapshot, snapshot.savedAt ? "配置已自动保存" : "配置将自动保存");
    }
  }

  class PayPalPaymentJobModel {
    constructor(result) {
      this.jobId = String(result.paymentJobId || "");
      this.logOffset = Number(result.paymentLogCount || 0);
      this.logSequence = Number(result.paymentLogSequence || 0);
      this.logs = [...(result.paymentLogs || [])];
      this.previousStage = String(result.paymentStage || "");
      this.previousConfirmationStatus = String(result.paymentAtRefreshStatus || "");
    }

    endpoint() {
      return "/api/account/paypal-payment/" + encodeURIComponent(this.jobId) +
        "?log_offset=0&log_after=" + this.logSequence;
    }

    apply(job = {}) {
      const offset = this.logOffset;
      const incoming = Array.isArray(job.logs) ? job.logs : [];
      const mapped = incoming.map((item, index) => ({
        at: new Date(Number(item.time || Date.now() / 1000) * 1000).toISOString(),
        level: String(item.level || "INFO").toLowerCase(),
        message: String(item.message || ""),
        source: "paypal_protocol",
        eventType: "payment_log",
        originTaskId: this.jobId,
        originSequence: Number(item.sequence || offset + index + 1),
      }));
      this.logs = [...this.logs, ...mapped].slice(-300);
      this.logOffset = Math.max(offset + incoming.length, Number(job.log_count || 0));
      this.logSequence = Math.max(
        this.logSequence,
        Number(job.log_sequence || 0),
        ...incoming.map((item) => Number(item.sequence || 0)),
      );
      const outcome = window.PaymentOutcomeModel.classify(job);
      const {
        status, result, confirmation, confirmationStatus, protocolSucceeded,
        paymentSucceeded, terminal, confirmationPending, plusConfirmed,
        paymentError, confirmationError, deliveryError,
      } = outcome;
      const postCheckError = confirmationError || deliveryError;
      const stage = protocolSucceeded
        ? String(confirmation.detail || (confirmationPending
          ? "协议支付成功，正在用 Cookie 登录获取新 AT"
          : plusConfirmed ? "协议支付成功，新 AT 已确认 Plus" : "协议支付成功"))
        : String(job.stage || status);
      if (confirmationStatus && confirmationStatus !== this.previousConfirmationStatus) {
        this.logs = [...this.logs, {
          at: String(confirmation.checked_at || new Date().toISOString()),
          level: postCheckError ? "warning"
            : plusConfirmed ? "success"
              : ["retrying", "plus_sms"].includes(confirmationStatus) ? "info" : "warning",
          message: String(confirmation.detail || "支付后新 AT 校验状态已更新"),
          source: "payment_at_refresh", eventType: "account_confirmation",
          originTaskId: this.jobId, originSequence: this.logSequence + this.logs.length + 1,
        }].slice(-300);
      }
      this.previousConfirmationStatus = confirmationStatus;
      const stageChanged = stage !== this.previousStage;
      this.previousStage = stage;
      return {
        terminal, stageChanged,
        fields: {
          paymentStatus: status,
          paymentStage: stage,
          paymentResult: result,
          paymentLogs: this.logs,
          paymentLogCount: this.logOffset,
          paymentLogSequence: this.logSequence,
          paymentProtocolSucceeded: protocolSucceeded,
          paymentAtRefreshStatus: confirmationStatus,
          paymentAtRefreshed: Boolean(confirmation.at_refreshed),
          paymentAccountType: String(confirmation.account_type || ""),
          paymentAtPlan: String(confirmation.plan || ""),
          paymentSucceeded,
          paymentConfirmed: plusConfirmed,
          paymentPlusConfirmed: plusConfirmed,
          paymentPending: confirmationPending,
          paymentFinishedAt: job.finished_at || null,
          paymentError,
          paymentConfirmationError: confirmationError,
          paymentDeliveryError: deliveryError,
          paymentPostCheckError: postCheckError,
        },
      };
    }
  }

  window.PayPalPaymentJobModel = PayPalPaymentJobModel;

  class PayPalPaymentMonitorPresenter {
    constructor(api, intervalMs = 1000) {
      this.api = api;
      this.intervalMs = intervalMs;
    }

    async monitor(result, onSnapshot, shouldContinue) {
      const model = new PayPalPaymentJobModel(result);
      if (!model.jobId) throw new Error("协议支付任务 ID 缺失");
      while (shouldContinue()) {
        try {
          const payload = await this.api.get(model.endpoint());
          const snapshot = model.apply(payload.job || {});
          onSnapshot(snapshot);
          if (snapshot.terminal) return snapshot;
        } catch (error) {
          if (error.status === 404) {
            const snapshot = model.apply({
              status: "failed", stage: "协议支付任务已失效", error: error.message,
            });
            onSnapshot(snapshot);
            return snapshot;
          }
          onSnapshot({
            terminal: false, retryError: error.message,
            fields: { paymentStage: "状态读取暂时失败，正在自动重试" },
          });
        }
        await new Promise((resolve) => setTimeout(resolve, this.intervalMs));
      }
      return null;
    }
  }

  class WorkspaceRenderer {
    constructor(store, presenter) { this.store = store; this.quickFlowAccountResultPresenter = presenter; this.controlTaskFilter = "all"; }

    paypalPaymentAction(item, state, flow = null) {
      if (!item?.email || !item?.url) return "";
      const account = (state.accounts || []).find((candidate) =>
        String(candidate.email || "").toLowerCase() === String(item.email || "").toLowerCase()
      );
      const country = String(
        item.country || account?.cardLinkProxyCountry || account?.cardLinkCountry || ""
      ).toUpperCase();
      if (!country) return "";
      const countries = state.registrationProxy?.countries || [];
      const countryItem = countries.find((candidate) => candidate.code === country) || {
        code: country, label: country,
      };
      const postCheckError = String(
        item.paymentPostCheckError || item.paymentConfirmationError ||
        item.paymentDeliveryError || "",
      );
      if (item.paymentError && flow?.runId) {
        return '<div class="quick-flow-result-actions paypal-payment-action">' +
          '<div class="paypal-payment-auto-country"><span>支付失败</span><strong>协议支付失败 · ' +
          escapeHtml(countryOptionLabel(countryItem)) + '</strong></div>' +
          '<button class="button primary small" data-action="retry-quick-payment" data-email="' +
          escapeHtml(item.email) + '" data-run-id="' + escapeHtml(flow.runId) + '"' +
          (flow.status === "running" ? " disabled" : "") + '>重新支付</button>' +
          '<small>' + escapeHtml(item.paymentError) +
          ' · 点击后将强制重新提链，再启动协议支付</small></div>';
      }
      if (item.paymentStarted) {
        const status = String(item.paymentStatus || "queued");
        const paymentLabel = item.paymentDeliveryError
          ? "支付成功 · Plus 已确认 · 手机号/Codex 后处理失败"
          : item.paymentConfirmationError
            ? "支付成功 · AT/Plus 后置校验失败"
            : item.paymentPlusConfirmed
              ? "支付成功 · 新 AT 已确认 Plus"
              : item.paymentPending ? "支付成功 · 正在用 Cookie 获取新 AT"
                : item.paymentSucceeded ? "支付成功"
            : status === "failed" ? "协议支付失败"
            : status === "cancelled" ? "协议支付已停止"
              : ["awaiting_otp", "awaiting_captcha"].includes(status)
                ? "协议支付等待验证" : status === "queued" ? "协议支付排队中" : "协议支付进行中";
        return '<div class="quick-flow-result-actions paypal-payment-action">' +
          '<div class="paypal-payment-auto-country"><span>自动监听</span><strong>' +
          escapeHtml(paymentLabel) + ' · ' +
          escapeHtml(countryOptionLabel(countryItem)) + '</strong></div>' +
          '<small>' + escapeHtml(item.paymentError || postCheckError || item.paymentStage || "任务状态正在自动更新") +
          (item.paymentJobId ? ' · 任务 ' + escapeHtml(String(item.paymentJobId).slice(0, 12)) : '') +
          '</small></div>';
      }
      const buttonLabel = item.paymentError ? "重新启动协议支付" : "一键支付";
      return '<div class="quick-flow-result-actions paypal-payment-action">' +
        '<div class="paypal-payment-auto-country"><span>支付地址</span><strong>自动匹配 · ' +
        escapeHtml(countryOptionLabel(countryItem)) + '</strong></div>' +
        '<button class="button primary small" data-action="one-click-paypal-payment" ' +
        'data-idle-label="' + escapeHtml(buttonLabel) + '" data-email="' +
        escapeHtml(item.email) + '">' + escapeHtml(buttonLabel) + '</button>' +
        '<small>' + (item.paymentError
          ? '自动启动失败：' + escapeHtml(item.paymentError)
          : '根据提链真实出口国家自动生成身份资料，并使用当前账号 Cookie、提链代理与接码平台') +
        '</small></div>';
    }

    renderShell(state) {
      $("accountNavCount").textContent = state.accounts.length || "—";
      $("networkNavState").textContent = state.registrationProxy?.enabled ? "已启用" : "直连";
      const links = state.accounts.filter((item) => item.cardLink).length;
      $("cardLinkNavCount").textContent = links || "—";
      $("paypalNavState").textContent = state.paypal?.running ? "在线" : "—";
      const runtime = state.browserTask.runtime || {};
      const available = Boolean(runtime.available);
      $("runtimeDot").className = available ? "ok" : "bad";
      $("sidebarRuntimeDot").className = available ? "" : "bad";
      $("runtimeLabel").textContent = available ? "运行环境已连接" : "运行环境不可用";
      $("sidebarRuntimeLabel").textContent = available ? "运行环境已连接" : "运行环境不可用";
      const rows = workspaceTaskRows(state);
      const counts = {
        running: rows.filter((item) => item.status === "running").length,
        queued: rows.filter((item) => item.status === "queued").length,
        completed: rows.filter((item) => item.status === "completed").length,
        failed: rows.filter((item) => item.status === "failed").length,
      };
      $("footerRuntimeDot").className = available ? "ok" : "bad";
      $("footerRuntimeLabel").textContent = available ? "连接正常" : "运行环境不可用";
      $("footerRunningCount").textContent = counts.running;
      $("footerQueuedCount").textContent = counts.queued;
      $("footerSuccessCount").textContent = counts.completed;
      $("footerFailedCount").textContent = counts.failed;
      $("footerApiDot").className = available ? "ok" : "bad";
      $("footerApiLabel").textContent = available ? "正常" : "异常";
    }

    renderOverview(state) {
      const total = state.accounts.length;
      const plus = state.accounts.filter((item) => item.accountType === "plus").length;
      const free = state.accounts.filter((item) => item.accountType === "free").length;
      const registered = state.accounts.filter((item) => item.hasPassword || item.hasSession).length;
      const pending = Math.max(total - plus - free, 0);
      $("overviewMetrics").innerHTML = [
        metricCard("全部账号", total, "当前邮箱总量", "", "◎"),
        metricCard("已注册", registered, "已保存密码或 Session", "green", "✓"),
        metricCard("Plus 账号", plus, "已识别 Plus 套餐", "purple", "P"),
        metricCard("待处理", pending, "尚未完成账号验证", "amber", "◷"),
      ].join("");

      const registeredPercent = total ? Math.round(registered / total * 100) : 0;
      $("accountDistribution").innerHTML =
        '<div style="position:relative"><div class="donut" style="--p:' + registeredPercent +
        '"></div><div class="donut-label"><strong>' + total + '</strong><span>总数</span></div></div>' +
        '<div class="legend"><div class="legend-row"><i style="background:var(--green)"></i><span>已注册</span><strong>' +
        registered + '</strong></div><div class="legend-row"><i style="background:var(--amber)"></i><span>待验证</span><strong>' +
        pending + '</strong></div><div class="legend-row"><i style="background:var(--red)"></i><span>Session 过期</span><strong>' +
        state.accounts.filter((item) => item.sessionStatus === "expired").length + "</strong></div></div>";

      const pipeline = [
        ["创建邮箱", total], ["账号注册", registered],
        ["保存 Session", state.accounts.filter((item) => item.hasSession).length],
        ["开启 2FA", state.accounts.filter((item) => item.hasTwoFactor).length],
      ];
      $("registrationPipeline").innerHTML = pipeline.map((item, index) => {
        const percent = total ? Math.round(item[1] / total * 100) : 0;
        return '<div class="pipeline-step"><div class="pipeline-icon">' + (index + 1) +
          '</div><strong>' + item[0] + '</strong><span>' + item[1] + " · " + percent +
          '%</span><progress value="' + percent + '" max="100"></progress></div>';
      }).join("");

      const recent = [...state.accounts].sort((left, right) => {
        const leftDate = left.lastActivity || left.createdAt || "";
        const rightDate = right.lastActivity || right.createdAt || "";
        return rightDate.localeCompare(leftDate);
      }).slice(0, 7);
      $("recentAccounts").innerHTML = recent.length ? recent.map((item) =>
        '<div class="compact-row"><strong>' + escapeHtml(item.email) + '</strong><span>' +
        badge(planName(item.accountType), item.accountType === "plus" ? "plus" : "") +
        '</span><span>' + badge(sessionName(item.sessionStatus), item.sessionStatus === "ready" ? "success" : item.sessionStatus === "expired" ? "error" : "warning") +
        '</span><span>' + formatDate(item.lastActivity || item.createdAt) + "</span></div>"
      ).join("") : '<div class="empty-state compact">暂无账号</div>';

      const runtime = state.browserTask.runtime || {};
      const services = [
        ["Web 服务", true, "在线"],
        ["IMAP 收件", Boolean(state.inbox.configured), state.inbox.configured ? "已配置" : "未配置"],
        ["Camoufox", Boolean(runtime.available), runtime.available ? "可用" : "不可用"],
        ["提链运行环境", Boolean(runtime.available), runtime.available ? "可用" : "不可用"],
      ];
      const allReady = services.every((item) => item[1]);
      $("serviceStatusBadge").className = "badge " + (allReady ? "success" : "warning");
      $("serviceStatusBadge").textContent = allReady ? "全部正常" : "需要检查";
      $("serviceStatusList").innerHTML = services.map((item) =>
        '<div class="service-row"><span><i>' + (item[1] ? "✓" : "!") + '</i>' +
        item[0] + "</span><strong>" + item[2] + "</strong></div>"
      ).join("");
      this.renderControlCenter(state);
    }

    renderControlCenter(state) {
      const rows = workspaceTaskRows(state);
      const statuses = ["running", "queued", "completed", "failed", "stopped"];
      const counts = Object.fromEntries(statuses.map((status) => [
        status, rows.filter((item) => item.status === status).length,
      ]));
      $("controlTaskCountAll").textContent = rows.length;
      $("controlTaskCountRunning").textContent = counts.running;
      $("controlTaskCountQueued").textContent = counts.queued;
      $("controlTaskCountCompleted").textContent = counts.completed;
      $("controlTaskCountFailed").textContent = counts.failed;
      $("controlTaskCountStopped").textContent = counts.stopped;
      const query = $("controlTaskSearch")?.value.trim().toLocaleLowerCase("zh-CN") || "";
      const filtered = rows.filter((item) => {
        const statusMatches = this.controlTaskFilter === "all" || item.status === this.controlTaskFilter;
        const queryMatches = !query || [
          item.name, item.group, item.flow, item.email, item.id, item.message,
        ].join(" ").toLocaleLowerCase("zh-CN").includes(query);
        return statusMatches && queryMatches;
      });
      const statusLabels = {
        running: "运行中", queued: "排队中", completed: "已完成",
        failed: "失败", stopped: "已停止", idle: "空闲",
      };
      $("controlTaskSummary").textContent = "显示 " + filtered.length + " / " + rows.length +
        " 个真实任务 · 更新于 " + formatClock(new Date().toISOString());
      $("controlTaskTableBody").innerHTML = filtered.length ? filtered.map((item) => {
        const detail = item.email || item.id || item.message || "后端任务状态";
        const progress = item.completed + " / " + item.total;
        const duration = formatElapsed(item.startedAt, item.finishedAt);
        return '<tr><td><div class="control-task-name"><strong>' + escapeHtml(item.name) +
          '</strong><small title="' + escapeHtml(detail) + '">' + escapeHtml(detail) +
          '</small></div></td><td>' + escapeHtml(item.group) + '</td><td>' +
          escapeHtml(item.flow) + '</td><td>' + item.threads + '</td><td>' +
          escapeHtml(progress) + '</td><td>' + escapeHtml(item.successRate.toFixed(1)) +
          '%</td><td><span class="control-task-status ' + escapeHtml(item.status) +
          ' status-' + escapeHtml(item.status) + '">' + escapeHtml(statusLabels[item.status] || "空闲") +
          '</span></td><td class="control-task-duration">' + escapeHtml(duration) +
          '</td><td><button class="control-task-action" type="button" data-route="' +
          escapeHtml(item.route) + '" aria-label="打开' + escapeHtml(item.name) +
          '">•••</button></td></tr>';
      }).join("") : '<tr><td colspan="9"><div class="control-task-empty"><strong>暂无匹配任务</strong>' +
        '<span>启动注册、浏览器、协议、验证或提链流程后，这里会显示真实状态。</span></div></td></tr>';
    }

    filteredAccounts(state) {
      const query = $("accountSearch")?.value.trim().toLowerCase() || "";
      const plan = $("accountPlanFilter")?.value || "all";
      const session = $("accountSessionFilter")?.value || "all", liandong = $("accountLiandongFilter")?.value || "all";
      return state.accounts.filter((item) =>
        (!query || item.email.toLowerCase().includes(query)) &&
        (plan === "all" || item.accountType === plan) &&
        (session === "all" || item.sessionStatus === session) &&
        (liandong === "all" || (liandong === "uploaded") === Boolean(item.liandongShopUploaded))
      );
    }

    renderAccounts(state) {
      const total = state.accounts.length;
      const registered = state.accounts.filter((item) => item.hasPassword || item.hasSession).length;
      const sessions = state.accounts.filter((item) => item.sessionStatus === "ready").length;
      const twoFactor = state.accounts.filter((item) => item.hasTwoFactor).length;
      $("accountMetrics").innerHTML = [
        metricCard("全部邮箱", total, "已添加邮箱账号", "", "✉"),
        metricCard("已注册", registered, "OpenAI 账号", "green", "✓"),
        metricCard("Session 可用", sessions, "可直接验证与提链", "", "S"),
        metricCard("已开启 2FA", twoFactor, "双重验证", "amber", "2"),
      ].join("");
      const registrationMode = ["headless", "headed", "roxy", "protocol"].includes(state.registrationMode)
        ? state.registrationMode : "headed";
      const protocolMode = registrationMode === "protocol";
      const roxyMode = registrationMode === "roxy";
      const forceHeadless = Boolean(state.browserTask.runtime?.forceHeadless);
      document.querySelectorAll('input[name="registrationMode"]').forEach((input) => {
        input.checked = input.value === registrationMode;
        input.disabled = input.value === "headed" && forceHeadless;
        input.closest("label")?.classList.toggle("selected", input.checked);
      });
      $("headless").checked = registrationMode === "headless" ||
        (roxyMode && $("roxyWindowMode").value === "background");
      this.renderRoxyRegistration(state, roxyMode);
      $("protocolRegistrationPanel").hidden = !protocolMode;
      $("registrationSourceBlock").classList.remove("mode-disabled");
      $("registrationManualBlock").classList.toggle("mode-disabled", protocolMode);
      const smsBower = state.smsBower || {};
      const smsBowerStatus = $("smsbowerStatus");
      smsBowerStatus.className = "badge " + (smsBower.configured ? "success" : "warning");
      smsBowerStatus.textContent = smsBower.configured
        ? "SMSBower Gmail · 本机取码保留 " + (smsBower.retentionHours || 24) + " 小时"
        : "SMSBower 未配置";
      if (smsBower.maxPrice) $("smsbowerMaxPrice").value = smsBower.maxPrice;
      const zkgmail = state.zkgmail || {};
      const zkgmailStatus = $("zkgmailStatus");
      zkgmailStatus.className = "badge " + (zkgmail.configured ? "success" : "warning");
      zkgmailStatus.textContent = zkgmail.configured
        ? "QQ 自动取码 · " + (zkgmail.domain || "cclgmail.com") + " · " + (zkgmail.forwardAccount || "352***4@qq.com")
        : (zkgmail.domain || "cclgmail.com") + " 已添加 · 待配置 QQ 授权码";
      const registration = state.registrationTask || {};
      const canStartNextRegistration = registration.canStartNext !== false;
      const activeRegistrationProcesses = Number(registration.runningCount || 0);
      const protocolRegistration = state.protocolRegistrationTask || {};
      const protocolBusy = Boolean(protocolRegistration.running || protocolRegistration.starting);
      const registrationProviderSelect = $("registrationEmailProvider");
      const gmailProviderOption = registrationProviderSelect.querySelector('option[value="gmail"]');
      if (gmailProviderOption) gmailProviderOption.disabled = protocolMode;
      if (protocolMode && registrationProviderSelect.value === "gmail") {
        registrationProviderSelect.value = "icloud";
      }
      const registrationProvider = registrationProviderSelect.value || "icloud";
      registrationProviderSelect.disabled = false;
      $("smsbowerControls").hidden = protocolMode || registrationProvider !== "gmail";
      $("zkgmailControls").hidden = registrationProvider !== "zkgmail";
      $("registerProviderButton").textContent = protocolMode
        ? registrationProvider === "zkgmail"
          ? (protocolRegistration.starting ? "正在生成 " + (zkgmail.domain || "cclgmail.com") + " 邮箱…"
            : protocolRegistration.running ? (zkgmail.domain || "cclgmail.com") + " 协议注册运行中"
            : "开始 " + (zkgmail.domain || "cclgmail.com") + " 协议注册")
          : (protocolRegistration.starting ? "正在领取 iCloud 库存邮箱…"
            : protocolRegistration.running ? "iCloud 协议注册运行中"
            : "开始 iCloud 协议注册")
        : registrationProvider === "gmail"
        ? (activeRegistrationProcesses ? "启动下一个 Gmail 注册进程" : "开始 Gmail 注册")
        : registrationProvider === "zkgmail"
        ? (activeRegistrationProcesses ? "启动下一个 " + (zkgmail.domain || "cclgmail.com") + " 注册进程" : "开始 " + (zkgmail.domain || "cclgmail.com") + " 注册")
        : (activeRegistrationProcesses ? "启动下一个 iCloud 注册进程" : "开始 iCloud 注册");
      if (roxyMode && registrationProvider === "icloud") {
        const roxyWindows = Number($("roxyConcurrency").value || 1);
        const roxyTargetCount = Number($("roxyTargetCount").value || roxyWindows);
        $("registerProviderButton").textContent = activeRegistrationProcesses
          ? "Roxy 并发注册运行中"
          : "Roxy 注册 · " + roxyWindows + "×" + roxyTargetCount;
      }
      $("registerProviderButton").disabled = protocolMode
        ? !["icloud", "zkgmail"].includes(registrationProvider) || protocolBusy ||
          (registrationProvider === "zkgmail" && !zkgmail.configured)
        : !canStartNextRegistration ||
          (roxyMode && activeRegistrationProcesses > 0) ||
          (registrationProvider === "gmail" && !smsBower.configured) ||
          (registrationProvider === "zkgmail" && !zkgmail.configured);
      $("registrationEmail").disabled = protocolMode;
      $("registerEmailButton").disabled = protocolMode || !canStartNextRegistration;
      $("fetchAllButton").disabled = protocolMode;
      $("registerEmailButton").textContent = activeRegistrationProcesses
        ? `启动下一个注册进程（运行中 ${activeRegistrationProcesses}）`
        : "添加邮箱并注册";
      const task = state.browserTask || {};
      const hasRegistration = Boolean(registration.id && registration.status !== "idle");
      const primaryTask = hasRegistration ? registration : task;
      const status = primaryTask.status || "idle";
      const statusMeta = taskStatusMeta(status);
      const runtimeRunning = Boolean(task.running || registration.running);
      const processCount = Number(registration.runningCount || 0);
      $("registrationRuntimeState").textContent = runtimeRunning
        ? (processCount > 1 ? processCount + " 个注册进程运行中" : statusMeta[0])
        : (hasRegistration ? "最近任务：" + statusMeta[0] : "当前无运行任务");
      $("registrationRuntimeMessage").textContent = runtimeRunning
        ? "页面、动作与诊断详情正在实时写入下方任务日志"
        : "运行详情请查看下方任务日志";
      $("stopTaskButton").disabled = !runtimeRunning;
      const codePanel = $("registrationCodePanel");
      const awaitingEmail = (registration.awaitingCodeEmails || [])[0] || registration.email || "";
      codePanel.hidden = !registration.awaitingCode ||
        ["smsbower", "zkgmail"].includes(registration.provider);
      $("registrationCodeEmail").textContent = registration.awaitingCode
        ? "验证码将用于 " + awaitingEmail
        : "等待注册页面请求验证码";

      const items = this.filteredAccounts(state);
      $("accountSummary").textContent = "显示 " + items.length + " / " + total + " 个账号";
      $("accountTableBody").innerHTML = items.length ? items.map((item) =>
        this.accountRows(item, state.selectedAccountEmail === item.email)
      ).join("") : '<tr><td colspan="7"><div class="empty-state compact">没有匹配的账号</div></td></tr>';
    }

    renderNetwork(state) {
      const proxy = state.registrationProxy || {};
      const clashMode = proxy.mode === "clash";
      const kookeeyMode = proxy.mode === "kookeey";
      $("registrationProxyEnabled").checked = Boolean(proxy.enabled);
      $("registrationProxyEnabled").disabled = !proxy.configured;
      $("registrationProxyEnabled").title = proxy.configured
        ? (clashMode
          ? "所有注册方式在每个账号开始前轮询新的日本节点，任务期间固定出口"
          : "所有注册方式使用所选国家的粘性动态代理；关闭时使用本机公网 IP 直连")
        : "请先在代理与线路模块设置代理连接";
      const networkMode = $("registrationNetworkMode");
      networkMode.className = "badge " + (proxy.enabled ? "success" : "blue");
      networkMode.textContent = proxy.enabled && clashMode
        ? "Clash 日本轮询" + (proxy.currentNode
          ? " · " + proxy.currentNode + " · " + (proxy.lastLatencyMs || 0) + "ms"
          : " · ≤" + (proxy.maxLatencyMs || 900) + "ms")
        : (proxy.enabled
          ? (kookeeyMode ? "Kookeey" : "动态代理") + " · " + (proxy.countryLabel || proxy.country || "")
          : "本机 IP 直连 · 语言随出口");
      const countries = proxy.countries || [];
      $("registrationProxyCountryOptions").innerHTML = countries.map((item) =>
        '<option value="' + escapeHtml(countryOptionLabel(item)) + '"></option>'
      ).join("");
      $("registrationProxyMode").value = proxy.mode || "dynamic";
      $("registrationProxyMode").disabled = false;
      $("registrationProxySetupButton").textContent = "代理设置";
      $("registrationProxySetupButton").disabled = false;
      if (proxy.country) $("registrationProxyCountry").value = proxy.country;
      const countrySearch = $("registrationProxyCountrySearch");
      countrySearch.disabled = clashMode;
      if (document.activeElement !== countrySearch || clashMode) {
        countrySearch.value = countryOptionLabel(
          countries.find((item) => item.code === (proxy.country || "NL")) ||
          { code: proxy.country || "NL", label: proxy.countryLabel || proxy.country || "荷兰" }
        );
      }
      $("registrationProxyStatus").className = "badge " + (proxy.enabled ? "success" : "blue");
      $("registrationProxyStatus").textContent = proxy.enabled
        ? "已启用 · " + (proxy.countryLabel || proxy.country || "")
        : "本机 IP 直连";
      const extractionProxy = state.cardLinkProxy || {};
      $("networkUsageState").textContent =
        "注册：" + (proxy.enabled ? (proxy.countryLabel || proxy.country || "已启用") : "本机直连") +
        "；提链：" + (extractionProxy.configured
          ? (extractionProxy.countryLabel || extractionProxy.country || "已配置")
          : "尚未配置");
      $("registrationProxyModeHint").textContent = clashMode
        ? "Clash 模式固定为日本出口；每个账号开始前选择新的可用节点"
        : kookeeyMode
          ? "Kookeey 模式会自动把国家、8 位 Session 和 5m 写入连接密码"
          : "通用模式会自动把 region、8 位 SID 和 5 分钟粘性时长写入用户名";
      $("registrationProxyCredentialFields").hidden = clashMode;
      $("rotateRegistrationProxyButton").hidden = !clashMode;
      $("rotateRegistrationProxyButton").disabled = false;
      const endpointInput = $("registrationProxyEndpoint");
      if (!endpointInput.dataset.dirty && document.activeElement !== endpointInput) {
        endpointInput.value = proxy.dynamicEndpoint || (clashMode ? "" : (proxy.endpoint || ""));
      }
      $("registrationProxyUsername").placeholder = proxy.usernameConfigured
        ? "已保存，留空保持原用户名"
        : (kookeeyMode ? "用户ID-安全策略用户名" : "代理用户名");
      $("registrationProxyPassword").placeholder = proxy.passwordConfigured
        ? "已保存，留空保持原密码"
        : "代理基础密码";
      ["registrationProxyUsername", "registrationProxyPassword", "registrationProxyEndpoint", "saveRegistrationProxyButton"].forEach((id) => {
        $(id).disabled = clashMode;
      });
      $("registrationProxyRoutePreview").innerHTML = proxy.enabled
        ? '<small>当前注册出口</small><strong>' + escapeHtml(proxy.countryLabel || proxy.country || "") +
          '</strong><span>无头浏览器、有头浏览器、Roxy 和协议注册都会使用该出口</span>'
        : '<small>当前注册出口</small><strong>本机 IP 直连</strong><span>保存并启用代理后，所有注册方式都会使用所选出口</span>';
      const testResult = proxy.testResult || {};
      $("registrationProxyTestResult").className = "proxy-test-result" +
        (testResult.ok === true ? " success" : testResult.ok === false ? " error" : "");
      $("registrationProxyTestResult").innerHTML = testResult.testedAt
        ? '<small>代理测试</small><strong>' + escapeHtml(testResult.ok ? "测试通过" : "测试失败") +
          '</strong><span>' + escapeHtml(testResult.message || "") + '</span>'
        : '<small>代理测试</small><strong>尚未测试</strong><span>点击“测试代理”检查出口 IP、国家与 ChatGPT 连通性</span>';
      $("testRegistrationProxyButton").disabled = !proxy.configured;
      this.renderCardLinkRouting(state);
    }

    renderCardLinkRouting(state) {
      const proxy = state.cardLinkProxy || {};
      const clashMode = proxy.mode === "clash";
      const kookeeyMode = proxy.mode === "kookeey";
      const modes = proxy.modes || [];
      const countries = proxy.countries || [];
      const modeSelect = $("cardLinkRoutingMode");
      modeSelect.innerHTML = modes.length ? modes.map((item) =>
        '<option value="' + escapeHtml(item.code) + '">' + escapeHtml(item.label) + "</option>"
      ).join("") : '<option value="kookeey">Kookeey 动态住宅</option><option value="dynamic">通用 region/SID</option><option value="clash">Clash 日本轮询</option>';
      modeSelect.value = proxy.mode || "kookeey";
      const countrySelect = $("cardLinkRoutingCountry");
      countrySelect.innerHTML = countries.map((item) =>
        '<option value="' + escapeHtml(item.code) + '">' + escapeHtml(countryOptionLabel(item)) + "</option>"
      ).join("");
      countrySelect.value = clashMode ? "JP" : (proxy.country || "DE");
      countrySelect.disabled = clashMode;
      $("cardLinkRoutingStatus").className = "badge " + (proxy.configured ? "success" : "blue");
      $("cardLinkRoutingStatus").textContent = proxy.configured
        ? "已独立配置 · " + (proxy.countryLabel || proxy.country || "")
        : "尚未配置";
      $("cardLinkRoutingModeHint").textContent = clashMode
        ? "提链 Clash 固定为日本出口；该节点状态不会覆盖注册代理"
        : kookeeyMode
          ? "提链 Kookeey 会按每次提链所选国家生成独立 Session"
          : "提链通用代理会按所选国家生成独立 region/SID";
      $("cardLinkRoutingCredentialFields").hidden = clashMode;
      $("rotateCardLinkRoutingButton").hidden = !clashMode;
      const endpoint = $("cardLinkRoutingEndpoint");
      if (!endpoint.dataset.dirty && document.activeElement !== endpoint) {
        endpoint.value = proxy.dynamicEndpoint || (clashMode ? "" : (proxy.endpoint || ""));
      }
      $("cardLinkRoutingUsername").placeholder = proxy.usernameConfigured
        ? "已保存，留空保持提链用户名" : "提链代理用户名";
      $("cardLinkRoutingPassword").placeholder = proxy.passwordConfigured
        ? "已保存，留空保持提链密码" : "提链代理基础密码";
      ["cardLinkRoutingUsername", "cardLinkRoutingPassword", "cardLinkRoutingEndpoint", "saveCardLinkRoutingButton"].forEach((id) => {
        $(id).disabled = clashMode;
      });
      $("cardLinkRoutingPreview").innerHTML = proxy.configured
        ? '<small>当前提链出口</small><strong>' + escapeHtml(proxy.countryLabel || proxy.country || "已配置") +
          '</strong><span>OAICS / 支付链接只读取提链代理专用配置</span>'
        : '<small>当前提链出口</small><strong>尚未配置</strong><span>提链不会回退读取注册代理凭据</span>';
      const testResult = proxy.testResult || {};
      $("cardLinkRoutingTestResult").className = "proxy-test-result" +
        (testResult.ok === true ? " success" : testResult.ok === false ? " error" : "");
      $("cardLinkRoutingTestResult").innerHTML = testResult.testedAt
        ? '<small>提链代理测试</small><strong>' + escapeHtml(testResult.ok ? "测试通过" : "测试失败") +
          '</strong><span>' + escapeHtml(testResult.message || "") + "</span>"
        : '<small>提链代理测试</small><strong>尚未测试</strong><span>点击测试检查提链出口 IP、国家与 ChatGPT 连通性</span>';
      $("testCardLinkRoutingButton").disabled = !proxy.configured;
    }

    renderRoxyRegistration(state, selected) {
      const roxy = state.roxyRegistration || {};
      const controls = $("roxyRegistrationControls");
      controls.hidden = !selected;
      const workspace = $("roxyWorkspace");
      const profile = $("roxyProfile");
      const savedWorkspace = String(roxy.workspaceId || "");
      workspace.innerHTML = '<option value="">选择工作区</option>' +
        (roxy.workspaces || []).map((item) =>
          '<option value="' + escapeHtml(item.id) + '">' + escapeHtml(item.name) + '</option>'
        ).join("");
      workspace.value = savedWorkspace;
      profile.innerHTML = '<option value="">选择专用环境（会被清理）</option>' +
        (roxy.profiles || []).map((item) =>
          '<option value="' + escapeHtml(item.id) + '"' + (item.open ? " disabled" : "") + '>' +
          escapeHtml((item.sortNumber ? "#" + item.sortNumber + " · " : "") + item.name +
            (item.open ? "（已打开）" : "")) +
          '</option>'
        ).join("");
      profile.value = String(roxy.profileId || "");
      profile.disabled = !savedWorkspace || !roxy.available;
      profile.classList.toggle("needs-selection", selected && roxy.available && !roxy.configured);
      workspace.disabled = !roxy.available;
      const concurrencyInput = $("roxyConcurrency");
      const targetCountInput = $("roxyTargetCount");
      const maxConcurrency = Math.max(1, Math.min(5, Number(roxy.maxConcurrency || 1)));
      concurrencyInput.max = String(maxConcurrency);
      if (Number(concurrencyInput.value) > maxConcurrency) {
        concurrencyInput.value = String(maxConcurrency);
      }
      concurrencyInput.disabled = !roxy.available || maxConcurrency < 1;
      targetCountInput.disabled = !roxy.available;
      const selectedIndex = (roxy.profiles || []).findIndex((item) => item.id === profile.value);
      const orderedProfiles = selectedIndex < 0 ? [] : [
        roxy.profiles[selectedIndex],
        ...(roxy.profiles || []).filter((_, index) => index !== selectedIndex),
      ].filter((item) => !item.open).slice(0, Number(concurrencyInput.value || 1));
      const concurrency = Number(concurrencyInput.value || 1);
      const targetCount = Math.max(1, Number(targetCountInput.value || concurrency));
      const rounds = Math.ceil(targetCount / concurrency);
      const finalRoundCount = targetCount % concurrency || concurrency;
      const allocationText = orderedProfiles.length
        ? "使用 " + orderedProfiles.map((item) =>
          (item.sortNumber ? "#" + item.sortNumber + " " : "") + item.name
        ).join("、") + " · " + targetCount + " 个账号 / " + rounds +
          " 轮" + (rounds > 1 ? " / 末轮 " + finalRoundCount + " 个" : "")
        : "选择首个环境后，最多 5 个独立环境将按目标数自动轮换。";
      $("roxyProfileAllocation").textContent = allocationText;
      $("roxyProfileAllocation").title = allocationText + "；同一环境不会并行执行两个账号。";
      const status = $("roxyRegistrationStatus");
      status.className = "badge " + (roxy.configured ? "success" : roxy.available ? "warning" : "error");
      status.textContent = roxy.configured
        ? "Roxy 已就绪 · 可并发 " + maxConcurrency
        : roxy.error || (roxy.available ? "请选择专用指纹环境" : "Roxy OpenAPI 未连接");
    }

    registrationLabel(task) {
      const labels = {
        idle: "空闲", generating_email: "正在创建邮箱", purchasing_gmail: "正在获取 Gmail", preparing_email: "正在准备邮箱", claiming_inventory: "正在领取库存", confirming_email: "正在确认邮箱",
        registering_openai: "正在注册 OpenAI", awaiting_verification_code: "等待输入验证码", completed: "注册成功",
        failed: "注册失败", cancelling: "正在停止", cancelled: "已停止",
      };
      return labels[task.phase] || labels[task.status] || "空闲";
    }

    accountRows(item, selected) {
      const registered = item.hasPassword || item.hasSession;
      const planKind = item.accountType === "plus" ? "plus" : item.accountType === "free" ? "" : "warning";
      const sessionKind = item.sessionStatus === "ready" ? "success" : item.sessionStatus === "expired" ? "error" : "warning";
      const liandongUploaded = Boolean(item.liandongShopUploaded);
      const liandongEligible = item.accountType === "plus" && (!item.hasPassword || item.hasTwoFactor);
      const liandongLabel = liandongUploaded && item.liandongShopGoodsLabel ? "已上传 · " + item.liandongShopGoodsLabel : liandongUploaded ? "已上传" : "未上传";
      const main = '<tr data-selectable data-action="select-account" data-email="' + escapeHtml(item.email) +
        '" class="' + (selected ? "selected" : "") + '"><td><div class="identity-cell"><span class="avatar">' +
        initials(item.email) + '</span><span class="identity-copy"><strong>' + escapeHtml(item.email) +
        '</strong><small>' + (item.hasTwoFactor ? "2FA 已开启" : "2FA 未开启") +
        '</small></span></div></td><td>' + badge(registered ? "已注册" : "未注册", registered ? "success" : "warning") +
        '</td><td>' + badge(planName(item.accountType), planKind) + '</td><td>' +
        badge(sessionName(item.sessionStatus), sessionKind) + '</td><td>' +
        badge(liandongLabel, liandongUploaded ? "success" : "warning") + '</td><td>' +
        formatDate(item.lastActivity || item.createdAt) + '</td><td><div class="row-actions"><button class="row-action" data-action="copy-email" data-email="' +
        escapeHtml(item.email) + '">复制邮箱</button><button class="row-action" data-action="upload-liandong-shop" data-email="' + escapeHtml(item.email) + '" title="' + (liandongUploaded ? "该账号已经上传过" : liandongEligible ? "有密码上传密码和 2FA；无密码上传接码地址" : "需要 Plus；有密码时还必须启用 2FA") + '"' + (liandongUploaded || !liandongEligible ? " disabled" : "") + '>' + (liandongUploaded ? "已上传" : "上传到小铺") + '</button><button class="row-action" data-action="select-account" data-email="' +
        escapeHtml(item.email) + '">' + (selected ? "收起" : "更多") + "</button></div></td></tr>";
      if (!selected) return main;
      const twoFactorPrimaryAction = item.hasTwoFactor
        ? this.credentialButton("复制 2FA 密钥", "copy-credential", item, "totp_secret")
        : this.credentialButton("添加 2FA", "enable-2fa", item, "", !item.hasPassword, "primary");
      return main + '<tr class="account-detail-row"><td colspan="7"><div class="account-detail"><div class="credential-summary">' +
        '<span><b>账号</b><code>' + escapeHtml(item.email) + '</code></span><span><b>密码</b><code>' +
        (item.hasPassword ? "••••••••••••" : "尚未保存") + '</code></span><span><b>2FA</b><code>' +
        (item.hasTwoFactor ? "已开启" : "未开启") + '</code></span><span><b>注册方式</b><code>' +
        escapeHtml(item.registrationMode || "未记录") + '</code></span><span><b>注册出口</b><code>' +
        escapeHtml([item.registrationProxyMode, item.registrationProxyCountry, item.registrationProxyEndpoint, item.registrationExitIp].filter(Boolean).join(" · ") || "直连/未记录") +
        '</code></span><span><b>联动小铺</b><code>' + (liandongUploaded ? liandongLabel + " · " + formatDate(item.liandongShopUploadedAt) : "未上传") +
        '</code></span><span><b>Plus 接码</b><code>' + (item.plusExportReady ? "已完成 · 可导出" : item.plusCodexStatus || "尚未完成") +
        '</code></span></div><div class="credential-actions">' +
        this.credentialButton("复制密码", "copy-credential", item, "password", !item.hasPassword) +
        twoFactorPrimaryAction +
        this.credentialButton("复制 2FA 码", "copy-credential", item, "totp_code", !item.hasTwoFactor) +
        this.credentialButton("复制 AT", "copy-credential", item, "access_token", !item.hasSession) +
        this.credentialButton("复制 Session", "copy-credential", item, "session", !item.hasSession) +
        this.credentialButton("获取验证码", "get-code", item) +
        this.credentialButton(item.hasCookies ? "Cookie 刷新状态" : "尚未保存 Cookie", "verify-account", item, "", !item.hasCookies) +
        (item.cardLinkStatus === "cs_live" && item.cardLinkMethod === "de_oaics_paypal"
          ? this.credentialButton("重新提链", "retry-quick-card-link", item, "", item.sessionStatus !== "ready")
          : "") +
        this.credentialButton("一键导入工作台", "import-workbench", item, "", !item.hasImportableSession) +
        this.credentialButton("复制账号", "copy-account", item, "", !item.hasPassword) +
        '<button class="button small" data-plus-export="cpa" data-email="' + escapeHtml(item.email) + '"' + (item.plusExportReady ? "" : " disabled") + '>导出 CPA</button>' +
        '<button class="button small" data-plus-export="sub2api" data-email="' + escapeHtml(item.email) + '"' + (item.plusExportReady ? "" : " disabled") + '>导出 Sub2API</button>' +
        this.credentialButton("删除邮箱", "delete-email", item, "", false, "danger") +
        "</div></div></td></tr>";
    }

    credentialButton(label, action, item, kind = "", disabled = false, extra = "") {
      return '<button class="button small ' + extra + '" data-action="' + action + '" data-email="' +
        escapeHtml(item.email) + '" data-kind="' + escapeHtml(kind) + '"' +
        (disabled ? " disabled" : "") + ">" + label + "</button>";
    }

    renderProtocolRegistration(state) {
      const task = state.protocolRegistrationTask || {};
      const protocolBusy = Boolean(task.running || task.starting);
      const credentialToggle = $("protocolSetupCredentials");
      credentialToggle.checked = true;
      credentialToggle.disabled = true;
      const pending = state.accounts.filter((item) => !item.protocolReady);
      const registered = state.accounts.filter((item) => item.protocolReady);
      const passwordReady = state.accounts.filter((item) => item.hasPassword);
      const twoFactorReady = state.accounts.filter((item) => item.hasTwoFactor);
      $("protocolMetrics").innerHTML = [
        metricCard(
          "待完整注册",
          pending.length,
          "密码、Session 或 TOTP 仍待完成",
          "amber", "◷"
        ),
        metricCard(
          "完整凭据就绪",
          registered.length,
          "密码、Session 与 TOTP 均已完成",
          "green", "✓"
        ),
        metricCard("密码已确认", passwordReady.length, "注册成功必需", "green", "K"),
        metricCard("2FA 已开启", twoFactorReady.length, "注册成功必需", "purple", "2"),
      ].join("");

      $("stopProtocolButton").disabled = !task.running;

      const meta = task.starting ? ["启动中", "running", "•"] : taskStatusMeta(task.status || "idle");
      $("protocolTaskBadge").className = "badge " + (task.status === "failed" ? "error" : task.status === "completed" ? "success" : protocolBusy ? "blue" : "");
      $("protocolTaskBadge").textContent = meta[0];
      $("protocolTaskMessage").textContent = task.message || "等待开始";
      $("protocolCurrentEmail").textContent = task.currentEmail || (task.starting ? "正在领取库存邮箱" : "尚未开始");
      $("protocolCurrentStage").textContent = taskStageLabel(task.phase || "idle");
      const total = Number(task.total || 0);
      const completed = Number(task.completed || 0);
      const progress = total ? Math.round(completed / total * 100) : 0;
      $("protocolTaskProgress").value = progress;
      $("protocolTaskProgressValue").textContent = completed + " / " + total;
      $("protocolTaskSuccess").textContent = Number(task.succeeded || 0);
      $("protocolTaskFailed").textContent = Number(task.failed || 0);
      $("protocolTaskElapsed").textContent = formatElapsed(task.startedAt, task.finishedAt);

      const stageOrder = ["password", "email_verification", "session", "two_factor", "completed"];
      let activeStage = task.phase || "";
      if (activeStage === "protocol_auth") {
        activeStage = "password";
      }
      const activeIndex = stageOrder.indexOf(activeStage);
      document.querySelectorAll("[data-protocol-stage]").forEach((element) => {
        const index = stageOrder.indexOf(element.dataset.protocolStage);
        element.classList.toggle("active", Boolean(task.running && index === activeIndex));
        element.classList.toggle("done", Boolean((activeIndex > index) || (!task.running && task.status === "completed")));
      });

      const logs = task.logs || [];
      $("protocolLogCount").textContent = logs.length + " 条";
      $("protocolTaskLog").innerHTML = logs.length ? logs.map((item) =>
        '<div class="protocol-log-row ' + escapeHtml(item.status || "") + '"><time>' +
        formatClock(item.at) + '</time><b title="' + escapeHtml(item.email || "") + '">' +
        escapeHtml(abbreviateEmail(item.email)) + '</b><span>' + escapeHtml(item.message || "") + '</span></div>'
      ).join("") : '<div class="task-log-empty">暂无协议任务日志</div>';
      $("protocolTaskLog").scrollTop = $("protocolTaskLog").scrollHeight;
    }

    renderQuickFlow(state) {
      const flows = Array.isArray(state.quickFlows) ? state.quickFlows : [];
      const activeFlowId = String(state.activeQuickFlowId || "");
      const flow = flows.find((item) => item.runId === activeFlowId) || flows.at(-1) || state.quickFlow || {};
      const running = flow.status === "running";
      const registrationProviderSelect = $("quickRegistrationProvider");
      if (registrationProviderSelect.dataset.ready !== "1") {
        const savedProvider = localStorage.getItem("hme_quick_registration_provider");
        registrationProviderSelect.value = savedProvider === "zkgmail" ? "zkgmail" : "inventory";
        registrationProviderSelect.dataset.ready = "1";
      }
      const registrationProvider = registrationProviderSelect.value === "zkgmail"
        ? "zkgmail" : "inventory";
      const zkgmailReady = Boolean(state.zkgmail?.configured);
      const registrationProviderReady = registrationProvider !== "zkgmail" || zkgmailReady;
      const registrationProviderLabel = registrationProvider === "zkgmail"
        ? (state.zkgmail?.domain || "cclgmail.com") + " · QQ 接码" : "iCloud 库存";
      $("quickFlowConfigBadge").textContent = registrationProviderLabel;
      $("quickFlowConfigBadge").className = registrationProviderReady
        ? "badge blue" : "badge warning";
      $("quickFlowSourceTag").textContent = registrationProviderLabel;
      $("quickRegistrationSourceDescription").textContent = registrationProvider === "zkgmail"
        ? registrationProviderReady
          ? "生成 " + (state.zkgmail?.domain || "cclgmail.com") + " catch-all 邮箱并从 QQ 转发邮箱自动取码"
          : "尚未配置：请先在账号管理设置 QQ 邮箱授权码"
        : "领取远端 iCloud 未注册库存并创建账号";
      const methodSelect = $("quickCardLinkMethod");
      const supportedMethods = ["de_oaics_paypal", "paypal_us", "paypal_gb"];
      const savedMethod = localStorage.getItem("hme_quick_card_link_method");
      const method = methodSelect.dataset.ready === "1" && supportedMethods.includes(methodSelect.value)
        ? methodSelect.value
        : supportedMethods.includes(savedMethod) ? savedMethod : "de_oaics_paypal";
      methodSelect.value = method;
      methodSelect.dataset.ready = "1";
      const config = cardLinkExtractionModes[method];
      $("quickFlowExtractStepLabel").textContent = method === "paypal_us"
        ? "生成 US/USD PayPal 链接"
        : method === "paypal_gb"
          ? "生成 GB/GBP PayPal 链接" : "生成严格 0 链接";
      $("quickFlowGeneratedLabel").textContent = method === "de_oaics_paypal"
        ? "已补 OAICS" : "已生成链接";
      const registrationProxy = state.registrationProxy || {};
      const extractionProxy = state.cardLinkProxy || {};
      const registrationProxyModeSelect = $("quickRegistrationProxyMode");
      const registrationProxyCountrySelect = $("quickRegistrationProxyCountry");
      const registrationProxyModes = registrationProxy.modes || [];
      const savedRegistrationProxyMode = localStorage.getItem("hme_quick_registration_proxy_mode");
      const selectedRegistrationProxyMode = registrationProxyModeSelect.dataset.ready === "1"
        ? registrationProxyModeSelect.value
        : (registrationProxy.enabled
          ? (registrationProxy.mode || "direct")
          : (registrationProxyModes.some((item) => item.code === savedRegistrationProxyMode)
            ? savedRegistrationProxyMode : "direct"));
      registrationProxyModeSelect.innerHTML = '<option value="direct">本机 IP 直连</option>' +
        registrationProxyModes.map((item) =>
          '<option value="' + escapeHtml(item.code) + '">' + escapeHtml(item.label) +
          (item.configured === false ? "（未配置）" : "") + "</option>"
        ).join("");
      registrationProxyModeSelect.value = registrationProxyModes.some((item) => item.code === selectedRegistrationProxyMode)
        ? selectedRegistrationProxyMode : "direct";
      registrationProxyModeSelect.dataset.ready = "1";
      const selectedRegistrationProxy = registrationProxyModes.find(
        (item) => item.code === registrationProxyModeSelect.value
      );
      const registrationProxyReady = registrationProxyModeSelect.value === "direct" ||
        Boolean(selectedRegistrationProxy?.configured);
      const registrationCountries = (registrationProxy.countries || []).filter((item) =>
        registrationProxyModeSelect.value !== "clash" || item.code === "JP"
      );
      const selectedRegistrationCountry = registrationProxyCountrySelect.value ||
        localStorage.getItem("hme_quick_registration_proxy_country") ||
        registrationProxy.country || "NL";
      registrationProxyCountrySelect.innerHTML = registrationCountries.map((item) =>
        '<option value="' + escapeHtml(item.code) + '">' + escapeHtml(countryOptionLabel(item)) + "</option>"
      ).join("");
      registrationProxyCountrySelect.value = registrationCountries.some(
        (item) => item.code === selectedRegistrationCountry
      ) ? selectedRegistrationCountry : (registrationCountries[0]?.code || "");
      const extractionProxyModeSelect = $("quickExtractionProxyMode");
      const extractionFirstCountrySelect = $("quickExtractionFirstProxyCountry");
      const extractionSecondCountrySelect = $("quickExtractionSecondProxyCountry");
      const extractionProxyModes = extractionProxy.modes || [];
      const savedExtractionMode = localStorage.getItem("hme_quick_extraction_proxy_mode");
      const selectedExtractionMode = extractionProxyModeSelect.dataset.method === method
        ? extractionProxyModeSelect.value
        : (extractionProxyModes.some((item) => item.code === savedExtractionMode)
          ? savedExtractionMode
          : (extractionProxy.cardLinkModes?.[method] || extractionProxy.mode || "dynamic"));
      extractionProxyModeSelect.innerHTML = extractionProxyModes.map((item) =>
        '<option value="' + escapeHtml(item.code) + '">' + escapeHtml(item.label) +
        (item.configured === false ? "（未配置）" : "") + "</option>"
      ).join("");
      extractionProxyModeSelect.value = extractionProxyModes.some(
        (item) => item.code === selectedExtractionMode
      ) ? selectedExtractionMode : (extractionProxyModes[0]?.code || "");
      extractionProxyModeSelect.dataset.method = method;
      const selectedExtractionProxy = extractionProxyModes.find(
        (item) => item.code === extractionProxyModeSelect.value
      );
      const extractionProxyReady = Boolean(selectedExtractionProxy?.configured);
      const extractionCountries = (extractionProxy.countries || []).filter((item) =>
        extractionProxyModeSelect.value !== "clash" || item.code === "JP"
      );
      const savedExtractionCountries = extractionProxy.cardLinkCountries || {};
      const fillExtractionCountry = (select, preferenceKey, storageKey, fallback) => {
        const current = select.dataset.preferenceKey === preferenceKey &&
          extractionCountries.some((item) => item.code === select.value)
          ? select.value : "";
        const selected = cardLinkCountryPolicy.resolve(
          method,
          current || savedExtractionCountries[preferenceKey] ||
            localStorage.getItem(storageKey) || fallback,
        );
        select.innerHTML = extractionCountries.map((item) =>
          '<option value="' + escapeHtml(item.code) + '">' + escapeHtml(countryOptionLabel(item)) + "</option>"
        ).join("");
        select.value = extractionCountries.some((item) => item.code === selected)
          ? selected : (extractionCountries[0]?.code || "");
        select.dataset.preferenceKey = preferenceKey;
      };
      fillExtractionCountry(
        extractionFirstCountrySelect,
        config.createProxyPreference,
        "hme_quick_extraction_first_country",
        config.createProxyCountry,
      );
      fillExtractionCountry(
        extractionSecondCountrySelect,
        config.promotionProxyPreference || config.createProxyPreference,
        "hme_quick_extraction_second_country",
        config.promotionProxyCountry || config.createProxyCountry,
      );
      const extractionSecondProxyLabel = extractionSecondCountrySelect.closest("label");
      if (extractionSecondProxyLabel) extractionSecondProxyLabel.hidden = config.singleProxy;
      extractionSecondCountrySelect.disabled = config.singleProxy;
      const promotionChoice = $("quickPromotionProxyChoice");
      if (promotionChoice.dataset.ready !== "1") {
        promotionChoice.value = localStorage.getItem("hme_quick_promotion_proxy_choice") === "second"
          ? "second" : "first";
        promotionChoice.dataset.ready = "1";
      }
      const paymentSms = state.paymentSms || {};
      const paymentSmsReady = Boolean(paymentSms.configured);
      const paymentSmsLabel = paymentSms.label || "自动接码平台";
      const targetAmountLabel = $("quickCardLinkTargetAmountLabel");
      const targetAmountInput = $("quickCardLinkTargetAmount");
      targetAmountLabel.hidden = !config.targetAmount;
      if (config.targetAmount && targetAmountInput.dataset.method !== method) {
        targetAmountInput.value = localStorage.getItem("hme_quick_paypal_us_target_amount") || "";
        targetAmountInput.dataset.method = method;
      }
      $("quickPromotionProxyChoiceLabel").hidden = method !== "de_oaics_paypal";
      const postPaymentPhoneBinding = Boolean($("quickPostPaymentPhoneBinding")?.checked);
      $("quickCardLinkChecks").innerHTML = [...config.checks, "✓ 自动协议支付", `✓ ${paymentSmsLabel} 自动取号`,
        postPaymentPhoneBinding ? "✓ Plus 确认后继续接码" : "✓ Plus 确认后直接结束",
      ].map((item) => "<span>" + escapeHtml(item) + "</span>").join("");
      $("quickCardLinkHint").textContent = !paymentSmsReady
        ? "自动协议支付需要接码 API Key，请打开 PP 支付中的“接码配置”。"
        : extractionProxyReady
        ? (config.singleProxy
          ? "提链代理与注册代理独立。第一代理负责 " + config.country +
            " Checkout、优惠 Update、金额校验、Confirm 与 Approve；不会获取或调用第二代理。"
          : "提链代理与注册代理独立。首次提链使用第一代理出口；链接失败后重新提链使用第二代理出口。优惠更新当前使用" +
            (promotionChoice.value === "second" ? "第二 IP" : "第一 IP") + "。") +
          `提链成功后会按链接国家自动选择支付代理，并由 ${paymentSmsLabel} 自动取号、取码和启动协议支付。`
        : "所选提链代理尚未配置，请先到“提链代理独立配置”保存凭据。";
      $("quickRegistrationProxySummary").textContent = "注册代理：" +
        (registrationProxyModeSelect.value === "direct"
          ? "本机 IP 直连"
          : registrationProxyReady
            ? (registrationProxyModeSelect.selectedOptions[0]?.textContent || "已配置") + " · " +
              (registrationProxyCountrySelect.selectedOptions[0]?.textContent || "")
            : (registrationProxyModeSelect.selectedOptions[0]?.textContent || "所选模式") +
              "；请先到注册代理独立配置保存凭据");
      $("quickExtractionProxySummary").textContent = "提链代理：" +
        (extractionProxyModeSelect.selectedOptions[0]?.textContent || "未选择") +
        "；第一出口：" + (extractionFirstCountrySelect.selectedOptions[0]?.textContent || "—") +
        (config.singleProxy ? "；全程复用第一代理" :
          "；第二出口：" + (extractionSecondCountrySelect.selectedOptions[0]?.textContent || "—")) +
        (method === "de_oaics_paypal"
          ? "；优惠更新：" + (promotionChoice.value === "second" ? "第二 IP" : "第一 IP")
          : "；账单国家：跟随 IP 地址");

      const registrationMode = $("quickRegistrationMode").value || "headless";
      const protocolMode = registrationMode === "protocol";
      $("quickProtocolSetupCredentials").checked = true;
      $("quickProtocolSetupCredentials").disabled = true;
      const roxyMode = registrationMode === "roxy";
      $("quickProtocolSetupCredentialsLabel").hidden = !protocolMode;
      $("quickRegistrationConcurrency").max = protocolMode || roxyMode ? "5" : "10";
      const quickTargetCount = $("quickRegistrationTargetCount");
      quickTargetCount.disabled = !roxyMode;
      if (!roxyMode) {
        quickTargetCount.value = protocolMode
          ? "1" : $("quickRegistrationConcurrency").value;
      } else {
        const savedTargetCount = Number(localStorage.getItem("hme_quick_registration_target") || 1);
        quickTargetCount.value = String(
          Number.isInteger(savedTargetCount) && savedTargetCount >= 1 && savedTargetCount <= 100
            ? savedTargetCount : 1
        );
      }
      const targetCount = Number(quickTargetCount.value || 1);
      const extractionCount = $("quickExtractionCount");
      extractionCount.max = "100";
      if (Number(extractionCount.value) > 100) {
        extractionCount.value = "100";
      }
      $("quickRegistrationHint").textContent = !registrationProviderReady
        ? (state.zkgmail?.domain || "cclgmail.com") + " 需要 QQ 邮箱授权码，请先在账号管理中完成配置。"
        : protocolMode
        ? "Mail Auth 协议将获取 1 个" + (registrationProvider === "zkgmail"
          ? " " + (state.zkgmail?.domain || "cclgmail.com") + " 邮箱并从 QQ 邮箱自动取码"
          : " iCloud 库存邮箱") +
          "，并强制设置密码与 TOTP 2FA，无需启动浏览器。"
        : roxyMode
          ? "Roxy 使用账号管理中已保存的专用环境，最多 5 个并发窗口并按目标数分轮；" +
            (registrationProvider === "zkgmail" ? "验证码从 QQ 转发邮箱读取。" : "验证码从 iCloud 收件箱读取。")
          : registrationMode === "headed"
            ? "有头浏览器会显示注册窗口；验证码自动从" +
              (registrationProvider === "zkgmail" ? " QQ 转发邮箱" : " iCloud 收件箱") + "读取。"
            : "无头浏览器将在后台完成注册；验证码自动从" +
              (registrationProvider === "zkgmail" ? " QQ 转发邮箱" : " iCloud 收件箱") + "读取。";

      methodSelect.disabled = false;
      if (!roxyMode) quickTargetCount.disabled = true;
      registrationProxyCountrySelect.disabled = registrationProxyModeSelect.value === "direct" ||
        registrationProxyModeSelect.value === "clash";
      extractionFirstCountrySelect.disabled = config.fixedProxyCountry ||
        extractionProxyModeSelect.value === "clash";
      extractionSecondCountrySelect.disabled = extractionProxyModeSelect.value === "clash";
      promotionChoice.disabled = method !== "de_oaics_paypal";
      const taskState = protocolMode ? state.protocolRegistrationTask : state.registrationTask;
      const canStartNext = taskState?.canStartNext !== false;
      const roxyBusy = roxyMode && (state.registrationTask?.tasks || []).some((item) =>
        item.running && item.browserEngine === "roxy"
      );
      $("startQuickFlowButton").disabled = !canStartNext || roxyBusy || !registrationProviderReady || !registrationProxyReady || !extractionProxyReady || !paymentSmsReady ||
        (registrationProxyModeSelect.value !== "direct" && !registrationCountries.length) ||
        !extractionCountries.length;

      const runStatus = {
        idle: ["空闲", ""],
        running: ["运行中", "blue"],
        completed: ["已完成", "success"],
        failed: ["失败", "error"],
        cancelled: ["已停止", "warning"],
      };
      const activeRunId = flow.runId || "";
      const activeRunCount = flows.filter((item) => item.status === "running").length;
      $("quickFlowRunCount").textContent = flows.length
        ? activeRunCount + " 运行 / " + flows.length + " 条"
        : "暂无流程";
      $("quickFlowRunList").innerHTML = flows.length ? flows.map((item, index) => {
        const meta = runStatus[item.status || "idle"] || runStatus.idle;
        const selected = item.runId === activeRunId;
        const current = item.currentEmail || item.message || "等待任务";
        const action = window.HmeQuickFlowHistory.runActions(item, escapeHtml);
        return '<article class="quick-flow-run ' + (selected ? "selected" : "") + '">' +
          '<button class="quick-flow-run-select" data-action="select-quick-flow" data-run-id="' +
          escapeHtml(item.runId) + '"><span>流程 ' + String(index + 1).padStart(2, "0") +
          '</span><strong>' + escapeHtml(item.manager === "protocol" ? "Mail Auth 协议注册" : "浏览器注册") +
          '</strong><small>' + escapeHtml(current) + '</small></button>' +
          '<div class="quick-flow-run-meta"><b class="badge ' + meta[1] + '">' + meta[0] +
          '</b><small>' + Math.round(Number(item.progress || 0)) + '% · ' +
          escapeHtml(String(item.taskId || "等待分配").slice(0, 12)) + '</small>' + action + '</div></article>';
      }).join("") : '<div class="task-log-empty">点击“开始一键注册”可创建第一个独立注册流程；运行中的流程可继续新建。</div>';

      const stageOrder = ["prepare", "register", "session", "extract", "payment", "complete"];
      const activeStage = stageOrder.includes(flow.phase) ? flow.phase : "prepare";
      const activeIndex = stageOrder.indexOf(activeStage);
      document.querySelectorAll("[data-quick-stage]").forEach((element) => {
        const index = stageOrder.indexOf(element.dataset.quickStage);
        element.classList.toggle("active", running && index === activeIndex);
        element.classList.toggle("done", flow.status === "completed" || index < activeIndex);
        element.classList.toggle("failed", flow.status === "failed" && index === activeIndex);
      });
      const progress = Math.max(0, Math.min(100, Number(flow.progress || 0)));
      $("quickFlowProgress").value = progress;
      $("quickFlowProgressValue").textContent = progress + "%";
      $("quickFlowRegisteredCount").textContent = Number(flow.registered || 0);
      $("quickFlowGeneratedCount").textContent = Number(flow.generated || 0);
      $("quickFlowPaymentCount").textContent = Number(flow.paymentSucceeded || 0);
      $("quickFlowSkippedCount").textContent = Number(flow.skipped || 0);
      $("quickFlowFailedCount").textContent = Number(flow.failed || 0);
      $("quickFlowCurrentAccount").textContent = flow.currentEmail || "等待任务";
      $("quickFlowCurrentAction").textContent = flow.currentAction || "点击上方按钮开始";
      const storedResults = flow.results || [];
      const interruptedEmail = String(flow.currentEmail || "").trim().toLowerCase();
      const needsInterruptedRetry = flow.status === "failed" && flow.phase === "extract" &&
        interruptedEmail && !storedResults.some((item) =>
          String(item.email || "").trim().toLowerCase() === interruptedEmail
        );
      const results = needsInterruptedRetry
        ? [...storedResults, {
            ok: false,
            retryable: true,
            interrupted: true,
            email: interruptedEmail,
            error: flow.message || flow.currentAction || "提链未完成，可重新提链",
          }]
        : storedResults;
      const failedResult = results.find((item) => !item.ok || item.paymentError);
      const failureExplanation = failedResult ? quickFlowFailureExplanation(failedResult) : "";
      $("quickFlowMessage").textContent = flow.status === "failed" && failureExplanation
        ? "失败原因：" + failureExplanation
        : flow.message || "尚未启动流水线";
      if (flow.status === "failed" && flow.phase === "extract" && failureExplanation) {
        $("quickFlowExtractStepLabel").textContent = "失败原因：" + failureExplanation;
        $("quickFlowExtractStepLabel").title = String(failedResult?.error || failureExplanation);
      } else {
        $("quickFlowExtractStepLabel").title = "";
      }
      const visibleAccountCount = Math.max(1, Math.min(
        100,
        Number(flow.targetCount || flow.registered || (flow.emails || []).length || 1),
      ));
      const knownEmails = [...new Set([
        ...(flow.emails || []),
        ...results.map((item) => item.email),
      ].map((email) => String(email || "").trim()).filter(Boolean))];
      const resultByEmail = new Map(results.map((item) => [
        String(item.email || "").trim().toLowerCase(), item,
      ]));
      const currentEmailKey = String(flow.currentEmail || "").trim().toLowerCase();
      $("quickFlowAccountCount").textContent = visibleAccountCount + " 个";
      $("quickFlowAccountQueue").innerHTML = Array.from(
        { length: visibleAccountCount },
        (_, index) => {
          const email = knownEmails[index] || "";
          const emailKey = email.toLowerCase();
          const result = resultByEmail.get(emailKey);
          const active = Boolean(email && currentEmailKey === emailKey && running);
          const paymentActive = Boolean(result?.paymentPending || (
            result?.paymentStarted && !result.paymentSucceeded && !result.paymentError
          ));
          const state = result
            ? (result.ok
              ? (result.paymentError ? "failed" : result.skipped ? "skipped" : paymentActive ? "running" : "done")
              : "failed")
            : active ? "running" : email ? "queued" : "idle";
          const label = result
            ? (result.ok
              ? (result.paymentError
                ? "链接已生成 · 协议支付失败"
                : result.paymentDeliveryError
                  ? "支付成功 · Plus 已确认 · 后处理失败"
                  : result.paymentConfirmationError
                    ? "支付成功 · AT/Plus 后置校验失败"
                    : result.skipped ? "已跳过" : result.paymentPlusConfirmed
                      ? "支付成功 · 新 AT Plus"
                      : result.paymentPending ? "支付成功 · AT/Plus 确认中"
                        : result.paymentSucceeded ? "支付成功"
                          : result.paymentStarted ? (result.paymentStage || "协议支付正在自动执行") : "PayPal 链接已完成")
              : "失败原因：" + quickFlowFailureExplanation(result))
            : active ? "正在提取 PayPal 链接" : email ? "等待提链" : "启动后自动分配账号";
          const code = result?.retryable ? "RETRY" : { done: "DONE", skipped: "SKIP", failed: "FAIL", running: "LIVE", queued: "QUEUE", idle: "WAIT" }[state];
          const accountAction = result?.retryable && email && flow.runId
            ? '<button class="button small quick-flow-account-retry" data-action="retry-quick-card-link" data-email="' +
              escapeHtml(email) + '" data-run-id="' + escapeHtml(flow.runId) + '"' +
              (running ? " disabled" : "") + '>重新提链</button>'
            : "<b>" + code + "</b>";
          return '<div class="quick-flow-account-card ' + state + '"><i>' +
            String(index + 1).padStart(2, "0") + '</i><div><strong>' +
            escapeHtml(email || "等待分配账号") + '</strong><span title="' +
            escapeHtml(result?.error || result?.paymentError || result?.paymentPostCheckError || label) + '">' + escapeHtml(label) +
            '</span></div>' + accountAction + "</div>";
        },
      ).join("");
      const statusMeta = {
        idle: ["空闲", "", "等待启动"],
        running: ["运行中", "blue", "自动执行中"],
        completed: ["已完成", "success", "流水线完成"],
        failed: ["失败", "error", "需要处理"],
        cancelled: ["已停止", "warning", "流水线已停止"],
      }[flow.status || "idle"];
      $("quickFlowStatusBadge").className = "badge " + statusMeta[1];
      $("quickFlowStatusBadge").textContent = statusMeta[0];
      $("quickFlowHeroStatus").textContent = statusMeta[2];
      $("quickFlowHeroMeta").textContent = flow.status === "completed"
        ? "注册 " + Number(flow.registered || 0) +
          (flow.method === "de_oaics_paypal" ? " · 补 OAICS " : " · 生成链接 ") + Number(flow.generated || 0) +
          "/" + Number(flow.registered || 0) + " · 协议支付成功 " + Number(flow.paymentSucceeded || 0) +
          " · 单账号最多 " +
          Number(flow.extractionCount || 1) + " 次 · 已跳过 " + Number(flow.skipped || 0)
        : flow.currentAction || "注册 → Session → 提链 → 协议支付";
      $("quickFlowNavState").textContent = running ? progress + "%" : flow.status === "completed" ? "DONE" : flow.status === "failed" ? "!" : "NEW";

      $("quickFlowResults").innerHTML = results.length ? results.map((item) =>
        this.quickFlowAccountResultPresenter.render(item, state, flow,
          this.paypalPaymentAction.bind(this), quickFlowFailureExplanation)
      ).join("") : '<div class="empty-state compact">完成后在这里显示账号与支付链接</div>';
    }

    filteredCardAccounts(state) {
      const query = $("cardSearch")?.value.trim().toLowerCase() || "";
      const status = $("cardStatusFilter")?.value || "all";
      const method = $("cardLinkMethod")?.value || "ph_hosted";
      return state.accounts.filter((item) =>
        (!query || item.email.toLowerCase().includes(query)) &&
        (status === "all" ||
          (status === "generated" && item.cardLink) ||
          (status === "cs_live" && item.cardLinkStatus === "cs_live") ||
          (status === "available" && cardLinkEligible(item, method)) ||
          (status === "unavailable" && item.sessionStatus !== "ready"))
      );
    }

    renderCardLinks(state) {
      this.renderCardLinkMethod();
      const method = $("cardLinkMethod").value;
      const payable = state.accounts.filter((item) => cardLinkEligible(item, method)).length;
      const generated = state.accounts.filter((item) => item.cardLink).length;
      const classified = state.accounts.filter((item) =>
        cardLinkMarkedForMethod(item, method)
      ).length;
      $("cardMetrics").innerHTML = [
        metricCard("全部", state.accounts.length, "账号总数", "", "◎"),
        metricCard("可提取", payable, "当前模式待处理", "green", "✓"),
        metricCard("已生成", generated, "支付链接", "purple", "↗"),
        metricCard("cs_live", classified, "可对原账号重新提链", "amber", "!"),
      ].join("");
      const items = this.filteredCardAccounts(state);
      $("cardLinkSummary").textContent = "显示 " + items.length + " 个账号，待提链 " + payable +
        " 个，cs_live 可重试 " + classified + " 个";
      $("cardAccountList").innerHTML = items.length ? items.map((item) => {
        const selected = state.selectedCardEmail === item.email;
        return '<button class="select-row ' + (selected ? "selected" : "") +
          '" data-action="select-card-account" data-email="' + escapeHtml(item.email) +
          '"><span class="select-indicator"></span><span class="identity-cell"><span class="avatar">' +
          initials(item.email) + '</span><span class="identity-copy"><strong>' + escapeHtml(item.email) +
          '</strong><small>' + (item.cardLink ? "已生成链接" : item.cardLinkStatus === "cs_live"
            ? "DE OAICS · cs_live 可重新提链" : "未生成") + '</small></span></span><span>' +
          badge(planName(item.accountType), item.accountType === "plus" ? "plus" : "") + '</span><span>' +
          badge(sessionName(item.sessionStatus), item.sessionStatus === "ready" ? "success" : "warning") +
          "</span></button>";
      }).join("") : '<div class="empty-state">没有匹配的账号</div>';
      this.renderCardSelection(state);
    }

    renderCardLinkMethod() {
      const method = $("cardLinkMethod").value;
      const config = cardLinkRuntimeConfig(method);
      const proxy = this.store.state.cardLinkProxy || {};
      const savedCountries = proxy.cardLinkCountries || {};
      const savedModes = proxy.cardLinkModes || {};
      const proxyModeSelect = $("cardLinkProxyMode");
      const selectedProxyMode = proxyModeSelect.dataset.method === method
        ? proxyModeSelect.value
        : (savedModes[method] || proxy.mode || "dynamic");
      const proxyModes = proxy.modes || [];
      proxyModeSelect.innerHTML = proxyModes.map((item) =>
        '<option value="' + escapeHtml(item.code) + '"' +
        (item.configured === false ? " disabled" : "") + '>' +
        escapeHtml(item.label) + (item.configured === false ? "（未配置）" : "") +
        "</option>"
      ).join("");
      proxyModeSelect.value = proxyModes.some((item) => item.code === selectedProxyMode)
        ? selectedProxyMode : (proxyModes.find((item) => item.configured)?.code || "");
      proxyModeSelect.dataset.method = method;
      const modeConfigured = Boolean(
        proxyModes.find((item) => item.code === proxyModeSelect.value)?.configured
      );
      proxyModeSelect.disabled = !proxyModes.some((item) => item.configured);
      const countries = (proxy.countries || []).filter((item) =>
        proxyModeSelect.value !== "clash" || item.code === "JP"
      );
      const renderCountrySelect = (select, preferenceKey, fallbackCountry) => {
        const current = select.dataset.preferenceKey === preferenceKey &&
          countries.some((item) => item.code === select.value)
          ? select.value : "";
        const selected = cardLinkCountryPolicy.resolve(
          method,
          current || savedCountries[preferenceKey] || fallbackCountry,
        );
        select.innerHTML = countries.map((item) =>
          '<option value="' + escapeHtml(item.code) + '">' +
          escapeHtml(countryOptionLabel(item)) + "</option>"
        ).join("");
        select.value = countries.some((item) => item.code === selected)
          ? selected : (countries[0]?.code || "");
        select.dataset.preferenceKey = preferenceKey;
        select.disabled = config.fixedProxyCountry || !modeConfigured || !countries.length;
      };
      $("cardLinkModeSummary").textContent = config.summary;
      $("cardLinkModeLabel").textContent = config.label;
      $("cardLinkPaymentOptions").hidden = !(
        config.targetAmount
      );
      $("cardLinkTargetAmountLabel").hidden = !config.targetAmount;
      $("cardLinkTargetAmount").disabled = Boolean(config.fixedTargetAmount);
      $("cardLinkTargetAmount").value = config.fixedTargetAmount ||
        localStorage.getItem("hme_card_link_target_amount") || "";
      $("cardLinkChecks").innerHTML = config.checks.map((item) =>
        "<span>" + escapeHtml(item) + "</span>"
      ).join("");
      $("generateCardLinkButton").textContent = config.button;
      $("cardLinkPromotionProxyLabel").hidden = config.singleProxy;
      renderCountrySelect(
        $("cardLinkCreateProxyCountry"),
        config.createProxyPreference,
        config.createProxyCountry,
      );
      renderCountrySelect(
        $("cardLinkPromotionProxyCountry"),
        config.promotionProxyPreference || "phPromotion",
        config.promotionProxyCountry || "TR",
      );
      $("cardLinkPromotionProxyCountry").disabled = config.singleProxy || !modeConfigured;
      $("cardLinkCreateProxyLabel").firstChild.textContent = config.createProxyLabel || (
        config.singleProxy ? "提链代理国家" : "建单代理国家"
      );
      $("cardLinkPromotionProxyLabel").firstChild.textContent =
        config.promotionProxyLabel || "优惠代理国家";
      $("cardLinkProxyHint").textContent = modeConfigured
        ? "使用“提链代理独立配置”中已保存的" +
          (proxyModeSelect.selectedOptions[0]?.textContent || "代理") +
          "；代理模式与国家都会自动保存"
        : "当前提链代理模式尚未配置，请先到“提链代理独立配置”保存对应配置";
    }

    renderCardSelection(state) {
      const item = state.accounts.find((candidate) => candidate.email === state.selectedCardEmail);
      const generate = $("generateCardLinkButton");
      const copy = $("copyCardLinkButton");
      const open = $("openCardLinkButton");
      const method = $("cardLinkMethod").value;
      const config = cardLinkRuntimeConfig(method);
      const selectedMode = $("cardLinkProxyMode").value;
      const modeConfigured = Boolean(
        state.cardLinkProxy?.modes?.find((candidate) => candidate.code === selectedMode)?.configured
      );
      const proxyReady = Boolean(
        modeConfigured &&
        $("cardLinkCreateProxyCountry").value &&
        (config.singleProxy || $("cardLinkPromotionProxyCountry").value)
      );
      const markedForCurrentMode = cardLinkMarkedForMethod(item, method);
      generate.textContent = markedForCurrentMode ? "重新提链当前账号" : config.button;
      generate.disabled = !item || item.sessionStatus !== "ready" || !proxyReady;
      generate.title = markedForCurrentMode
        ? "该账号上次返回 cs_live，点击后将强制重新提链"
        : proxyReady ? "" : "请先在代理与线路中保存所选代理模式";
      const batch = $("generateAllCardLinksButton");
      const batchCandidates = state.accounts.filter((candidate) =>
        cardLinkEligible(candidate, method)
      );
      if (!batch.dataset.running) {
        batch.textContent = "一键提链（" + batchCandidates.length + "）";
        batch.disabled = !proxyReady || !batchCandidates.length;
        batch.title = proxyReady ? "" : "请先在代理与线路中保存所选代理模式";
      }
      copy.disabled = !item?.cardLink;
      open.disabled = !item?.cardLink;
      if (!item) {
        $("cardOperationState").className = "empty-state compact";
        $("cardOperationState").textContent = "请选择一个账号";
        return;
      }
      $("cardOperationState").className = "operation-result";
      const generatedMode = item.cardLinkMethod === "de_oaics_paypal"
        ? "提取链接成功 · PayPal / 德国 · EUR OAICS 严格 0"
        : item.cardLinkMethod === "paypal_us"
            ? "提取链接成功 · PayPal / US / USD"
        : item.cardLinkMethod === "paypal_gb"
            ? "提取链接成功 · PayPal / GB / GBP"
        : item.cardLinkMethod === "ph_hosted"
          ? "提取链接成功 · PH / PHP hosted 严格 0"
          : "提取链接成功";
      const operationMessage = markedForCurrentMode
        ? "当前模式上次返回 cs_live，可点击“重新提链当前账号”再次处理"
        : item.cardLink ? generatedMode : "等待提取支付链接";
      $("cardOperationState").innerHTML = '<strong>' + escapeHtml(item.email) + '</strong><span>' +
        operationMessage +
        '</span><code>' + escapeHtml(item.cardLink || "尚无链接") + '</code><span>Session：' +
        sessionName(item.sessionStatus) + "</span>" +
        (item.cardLink ? this.paypalPaymentAction({
          email: item.email, url: item.cardLink,
          country: item.cardLinkProxyCountry || item.cardLinkCountry,
        }, state) : "");
    }

    renderPayPal(state) {
      paypalWorkspacePresenter.render(state.paypal || {});
    }

    verificationRows(state) {
      const taskAccounts = state.verificationTask.accounts || [];
      const taskAccountsByEmail = new Map(
        taskAccounts.map((item) => [String(item.email || "").toLowerCase(), item])
      );
      const accountRows = state.accounts.map((item) => {
        const fallback = {
          ...item,
          status: item.sessionStatus === "expired" ? "expired" :
            item.accountType === "unverified" ? "pending" : "completed",
          message: item.sessionStatus === "expired" ? "Access Token 已过期" :
            item.accountType === "unverified" ? "等待验证" : "验证记录",
        };
        return { ...fallback, ...(taskAccountsByEmail.get(item.email.toLowerCase()) || {}) };
      });
      const visibleEmails = new Set(
        state.accounts.map((item) => item.email.toLowerCase())
      );
      const taskOnlyRows = taskAccounts.filter(
        (item) => !visibleEmails.has(String(item.email || "").toLowerCase())
      );
      return [...accountRows, ...taskOnlyRows];
    }

    renderVerification(state) {
      const task = state.verificationTask;
      const rows = this.verificationRows(state);
      const plus = state.accounts.filter((item) => item.accountType === "plus").length;
      const free = state.accounts.filter((item) => item.accountType === "free").length;
      const errors = rows.filter((item) => ["failed", "expired", "error", "deleted"].includes(item.status) || item.sessionStatus === "expired").length;
      $("verificationMetrics").innerHTML = [
        metricCard("本次验证", task.total || rows.length, "账号数量", "", "✓"),
        metricCard("Plus 账号", task.plus || plus, "已识别 Plus", "purple", "P"),
        metricCard("Free 账号", task.free || free, "已识别 Free", "green", "F"),
        metricCard("异常账号", task.failed || task.expired || errors, "已删除 " + (task.deleted || 0), "amber", "!"),
      ].join("");
      $("verificationTaskTitle").textContent = task.id ? "验证任务 #" + task.id.slice(0, 10) : "验证任务";
      $("verificationSummary").textContent = task.status && task.status !== "idle"
        ? (task.completed || 0) + " / " + (task.total || 0) + " 已完成 · 并发 " + (task.concurrency || 1) + " · 删除 " + (task.deleted || 0)
        : "尚未开始批量验证";
      $("verificationProgress").value = task.total ? Math.round((task.completed || 0) / task.total * 100) : 0;
      $("verificationProgress").hidden = !task.running;
      const selectedEmail = state.accounts.some((item) => item.email === state.selectedVerificationEmail)
        ? state.selectedVerificationEmail : "";
      const selectedAccount = state.accounts.find((item) => item.email === selectedEmail);
      $("verificationAccountSelect").innerHTML = '<option value="">请选择一个账号（共 ' +
        state.accounts.length + ' 个）</option>' +
        state.accounts.map((item) => '<option value="' + escapeHtml(item.email) + '">' +
          escapeHtml(item.email + " · " + planName(item.accountType) + " · Session " + sessionName(item.sessionStatus)) +
          "</option>").join("");
      $("verificationAccountSelect").value = selectedEmail;
      $("verificationAccountSelect").disabled = !state.accounts.length;
      $("verificationPreviousButton").disabled = !state.accounts.length;
      $("verificationNextButton").disabled = !state.accounts.length;
      $("verificationConcurrency").disabled = Boolean(task.running);
      $("verifySelectedButton").disabled = Boolean(task.running) || !selectedAccount?.hasCookies || !task.runtime?.available;
      $("verifyAllButton").disabled = Boolean(task.running) || !state.verificationTask.runtime?.available;
      $("stopVerificationButton").disabled = !task.running;
      const filter = state.verificationFilter;
      const filtered = rows.filter((item) =>
        filter === "all" ||
        (filter === "plus" && item.accountType === "plus") ||
        (filter === "free" && item.accountType === "free") ||
        (filter === "error" && (["failed", "expired", "error", "deleted"].includes(item.status) || item.sessionStatus === "expired"))
      );
      $("verificationTableBody").innerHTML = filtered.length ? filtered.map((item) => {
        const selected = state.selectedVerificationEmail === item.email;
        const status = item.status || "pending";
        const isDeleted = status === "deleted";
        const isError = ["failed", "expired", "error", "deleted"].includes(status) || item.sessionStatus === "expired";
        const statusLabel = isDeleted ? "已删除" : isError ? "异常" : status === "queued" ? "等待中" :
          status === "running" ? "验证中" : status === "pending" ? "待验证" : "有效";
        const statusKind = isError ? "error" : ["queued", "running", "pending"].includes(status) ? "warning" : "success";
        return '<tr data-selectable data-action="select-verification" data-email="' + escapeHtml(item.email) +
          '" class="' + (selected ? "selected" : "") + '"><td><div class="identity-cell"><span class="select-indicator"></span><span class="avatar">' +
          initials(item.email) + '</span><strong>' + escapeHtml(item.email) + '</strong></div></td><td>' +
          badge(statusLabel, statusKind) +
          '</td><td>' + badge(planName(item.accountType), item.accountType === "plus" ? "plus" : "") +
          '</td><td>' + badge(sessionName(item.sessionStatus), item.sessionStatus === "ready" ? "success" : "error") +
          '</td><td>' + formatDate(item.verifiedAt || task.finishedAt || task.startedAt) + "</td></tr>";
      }).join("") : '<tr><td colspan="5"><div class="empty-state compact">暂无验证记录</div></td></tr>';
      this.renderVerificationDetail(state, rows);
      this.renderVerificationLogs(state);
    }

    renderVerificationDetail(state, rows) {
      const item = rows.find((candidate) => candidate.email === state.selectedVerificationEmail);
      if (!item) {
        $("verificationDetailEmail").textContent = "选择记录查看详情";
        $("verificationDetail").className = "empty-state";
        $("verificationDetail").textContent = "请选择一条验证记录";
        return;
      }
      $("verificationDetailEmail").textContent = item.email;
      const status = item.status || "pending";
      const isDeleted = status === "deleted";
      const isError = ["failed", "expired", "error", "deleted"].includes(status) || item.sessionStatus === "expired";
      const isPending = ["pending", "queued", "running"].includes(status);
      const sessionReady = item.sessionStatus === "ready";
      const planKnown = ["plus", "free"].includes(item.accountType);
      const step = (label, stateName) => '<div class="verification-step ' + stateName + '"><i>' +
        (stateName === "error" ? "×" : stateName === "pending" ? "·" : "✓") + '</i><span>' + label + "</span></div>";
      $("verificationDetail").className = "";
      $("verificationDetail").innerHTML =
        '<div class="verification-status-grid"><div><small>账号套餐</small>' +
        badge(planName(item.accountType), item.accountType === "plus" ? "plus" : planKnown ? "success" : "warning") +
        '</div><div><small>Session 状态</small>' +
        badge(sessionName(item.sessionStatus), sessionReady ? "success" : item.sessionStatus === "expired" ? "error" : "warning") +
        '</div></div><div class="verification-steps">' +
        step("读取 Session", sessionReady ? "success" : item.sessionStatus === "expired" ? "error" : "pending") +
        step("校验 Access Token", isError ? "error" : isPending || !sessionReady ? "pending" : "success") +
        step("读取 Plus / Free 套餐", isError ? "pending" : isPending || !planKnown ? "pending" : "success") +
        step("保存验证结果", isError ? "pending" : isPending || !planKnown ? "pending" : "success") + '</div>' +
        (isError ? '<div class="detail-error">' + escapeHtml(item.message || "Access Token 已过期，请重新获取 Session") +
        '</div>' : "") + (isDeleted ? "" : '<div class="verification-detail-actions"><button class="button primary" data-action="verify-account" data-email="' +
        escapeHtml(item.email) + '"' + (state.verificationTask.running || !item.hasCookies ? " disabled" : "") + '>' +
        (item.hasCookies ? "使用 Cookie 刷新状态" : "尚未保存 Cookie") + "</button></div>");
    }

    renderVerificationLogs(state) {
      const selected = String(state.selectedVerificationEmail || "").toLowerCase();
      const logs = (state.verificationTask.historyLogs || state.verificationTask.logs || []).slice(-300).reverse();
      $("verificationLogCount").textContent = logs.length + " 条";
      $("verificationLog").innerHTML = logs.length ? logs.map((entry) => {
        const message = String(entry.message || "");
        const level = entry.level || (/失败|无效|失效|错误/.test(message) ? "error" : /等待|重试|删除/.test(message) ? "warning" : "info");
        const icon = level === "error" ? "!" : level === "warning" ? "·" : "✓";
        const email = String(entry.email || "");
        return '<div class="task-log-row ' + (selected && email.toLowerCase() === selected ? "selected" : "") + '">' +
          '<span class="task-log-icon ' + escapeHtml(level) + '">' + icon + '</span><time>' +
          escapeHtml(formatClock(entry.at)) + '</time><span class="task-log-email" title="' + escapeHtml(email) + '">' +
          escapeHtml(email || "任务") + '</span><span class="task-log-message">' + escapeHtml(message) + "</span></div>";
      }).join("") : '<div class="task-log-empty">尚无验证日志</div>';
    }

    renderSettings(state) {
      const section = state.settingsSection;
      const copy = {
        imap: ["邮箱与 IMAP", "配置验证码收件服务"],
        sms: ["接码配置", "统一管理绑定手机号与 PayPal 的平台、价格和国家"],
        browser: ["浏览器运行", "设置 Camoufox 与任务并发"],
        workbench: ["工作台集成", "管理账号导入工作台的连接"],
        security: ["安全与访问", "查看本地访问与敏感信息策略"],
        appearance: ["外观", "切换工作台显示主题"],
      }[section];
      $("settingsTitle").textContent = copy[0];
      $("settingsSubtitle").textContent = copy[1];
      document.querySelectorAll("[data-settings-section]").forEach((button) => {
        button.classList.toggle("active", button.dataset.settingsSection === section);
      });
      if (section === "imap") this.renderImapSettings(state);
      else if (section === "sms") window.HmeSmsSettings.render(state);
      else if (section === "browser") this.renderBrowserSettings(state);
      else if (section === "workbench") this.renderWorkbenchSettings();
      else if (section === "security") this.renderSecuritySettings();
      else this.renderAppearanceSettings();
    }
    renderImapSettings(state) {
      const inbox = state.inbox;
      const lastSync = inbox.lastBackgroundSync
        ? " · 上次：" + formatDate(inbox.lastBackgroundSync)
        : "";
      const syncStatus = "按需同步（仅接码时连接）" + lastSync;
      const retrySeconds = Number(inbox.retryAfterSeconds || 0);
      const errorDetail = inbox.backgroundError
        ? '<span>收件异常：' + escapeHtml(inbox.backgroundError) +
          '；最长 ' + retrySeconds + ' 秒后自动重试，也可点击“立即同步”恢复。</span>'
        : "";
      $("settingsStatus").className = "badge " +
        (inbox.backgroundError ? "error" : inbox.configured ? "success" : "warning");
      $("settingsStatus").textContent = inbox.backgroundError
        ? "IMAP 自动重试中" : inbox.configured ? "IMAP 已保存" : "等待配置";
      $("settingsPanel").innerHTML =
        '<form id="imapForm" class="settings-form"><section class="form-section"><h3>邮箱连接</h3><div class="form-grid two">' +
        '<label class="field-label">IMAP 主机<input id="imapHost" value="' + escapeHtml(inbox.host || "") +
        '" placeholder="imap.mail.me.com"></label><label class="field-label">端口<input id="imapPort" type="number" value="' +
        escapeHtml(inbox.port || 993) + '"></label><label class="field-label">邮箱账号<input id="imapUsername" value="' +
        escapeHtml(inbox.username || "") + '" placeholder="name@icloud.com"></label><label class="field-label">应用专用密码<input id="imapPassword" type="password" placeholder="' +
        (inbox.configured ? "留空则保持现有密码" : "请输入应用专用密码") + '"></label></div></section>' +
        '<section class="form-section"><h3>连接状态</h3><div class="connection-card"><strong>' +
        (inbox.configured ? "✓ IMAP 配置已保存" : "尚未配置 IMAP") + '</strong><span>验证码数量：' +
        (inbox.codeCount || 0) + '</span><span>收件模式：' + escapeHtml(syncStatus) +
        '</span>' + errorDetail + '</div></section><div class="settings-actions"><button class="button" type="button" data-action="sync-inbox">立即同步</button><button class="button primary" type="button" data-action="save-imap">保存并测试</button></div></form>';
    }
    renderBrowserSettings(state) {
      const runtime = state.browserTask.runtime || {};
      const mode = ["headless", "headed", "roxy", "protocol"].includes(state.registrationMode)
        ? state.registrationMode : "headed";
      const roxy = state.roxyRegistration || {};
      $("settingsStatus").className = "badge " + (runtime.available ? "success" : "error");
      $("settingsStatus").textContent = runtime.available ? "运行环境可用" : "运行环境不可用";
      $("settingsPanel").innerHTML =
        '<div class="settings-form"><section class="form-section"><h3>账号注册方式</h3><div class="settings-registration-modes" role="radiogroup" aria-label="账号注册方式">' +
        '<label><input type="radio" name="settingsRegistrationMode" value="headless" ' + (mode === "headless" ? "checked" : "") + '><span><b>无头浏览器</b><small>后台运行 Camoufox</small></span></label>' +
        '<label><input type="radio" name="settingsRegistrationMode" value="headed" ' + (mode === "headed" ? "checked" : "") + (runtime.forceHeadless ? " disabled" : "") + '><span><b>有头浏览器</b><small>显示前台浏览器窗口</small></span></label>' +
        '<label><input type="radio" name="settingsRegistrationMode" value="roxy" ' + (mode === "roxy" ? "checked" : "") + '><span><b>Roxy 注册</b><small>专用环境 · 随机指纹</small></span></label>' +
        '<label><input type="radio" name="settingsRegistrationMode" value="protocol" ' + (mode === "protocol" ? "checked" : "") + '><span><b>协议注册</b><small>Mail Auth · 密码 + 2FA</small></span></label></div>' +
        '<label class="field-label" style="margin-top:14px">验证并发<input id="settingsConcurrency" type="number" min="1" max="10" value="' +
        escapeHtml($("concurrency").value) + '"></label></section><section class="form-section"><h3>运行环境</h3><div class="connection-card"><strong>' +
        (runtime.available ? "✓ Camoufox 运行环境已连接" : "× Camoufox 运行环境不可用") +
        '</strong><span>' + escapeHtml(runtime.targetProject || (runtime.errors || []).join("；") || "未返回运行目录") +
        '</span></div><div class="connection-card" style="margin-top:10px"><strong>' +
        escapeHtml(roxy.available ? "✓ Roxy OpenAPI 已连接" : "× Roxy OpenAPI 未连接") +
        '</strong><span>' + escapeHtml(roxy.configured
          ? "已选择首个专用环境；最多可并发 " + (roxy.maxConcurrency || 1) +
            " 个独立窗口；后台模式使用隐藏窗口实现"
          : roxy.error || "请在账号页选择一个会被注册流程清理的专用环境") +
        '</span></div></section><div class="settings-actions"><button class="button" data-action="focus-registration-proxy">打开独立代理模块</button>' +
        '<button class="button primary" data-action="save-browser-settings">保存设置</button></div></div>';
    }

    renderWorkbenchSettings() {
      $("settingsStatus").className = "badge blue";
      $("settingsStatus").textContent = "服务端配置";
      $("settingsPanel").innerHTML =
        '<div class="settings-form"><section class="form-section"><h3>账号工作台导入</h3><div class="connection-card"><strong>通过服务端环境变量连接</strong><span>导入操作只传递当前账号，不在浏览器保存共享令牌。</span></div></section><section class="form-section"><h3>可导入内容</h3><div class="toggle-row"><span>邮箱与密码</span><span class="badge success">启用</span></div><div class="toggle-row"><span>Session 与 Access Token</span><span class="badge success">启用</span></div><div class="toggle-row"><span>2FA 密钥</span><span class="badge success">启用</span></div></section></div>';
    }

    renderSecuritySettings() {
      $("settingsStatus").className = "badge success";
      $("settingsStatus").textContent = "本地保护";
      $("settingsPanel").innerHTML =
        '<div class="settings-form"><section class="form-section"><h3>访问策略</h3><div class="toggle-row"><span>本地请求令牌<small style="display:block;color:var(--muted)">所有写操作均验证本地令牌</small></span><span class="badge success">已启用</span></div><div class="toggle-row"><span>登录 Cookie<small style="display:block;color:var(--muted)">HttpOnly · SameSite=Strict</small></span><span class="badge success">已启用</span></div></section><section class="form-section"><h3>敏感信息</h3><div class="connection-card"><strong>密码、Session、2FA 与代理默认隐藏</strong><span>注册代理凭据仅保存在本地数据库，接口不回传密码，日志只显示出口国家和脱敏连接。</span></div></section></div>';
    }

    renderAppearanceSettings() {
      $("settingsStatus").className = "badge blue";
      $("settingsStatus").textContent = document.documentElement.dataset.theme === "light" ? "浅色主题" : "深色主题";
      $("settingsPanel").innerHTML =
        '<div class="settings-form"><section class="form-section"><h3>界面主题</h3><div class="button-row" style="margin:0;max-width:460px"><button class="button" data-action="set-theme" data-theme="dark">深色主题</button><button class="button" data-action="set-theme" data-theme="light">浅色主题</button><button class="button" data-action="set-theme" data-theme="system">跟随系统</button></div></section><section class="form-section"><h3>设计系统</h3><div class="connection-card"><strong>统一设计令牌</strong><span>颜色、间距、圆角和状态组件由共享 CSS 变量控制。</span></div></section></div>';
    }
  }

  class WorkspaceController {
    constructor() {
      this.api = new ApiGateway(localToken);
      this.store = new ObservableStore({
        accounts: [],
        browserTask: { status: "idle", runtime: {} },
        registrationTask: { status: "idle", phase: "idle" },
        protocolRegistrationTask: { status: "idle", phase: "idle", runtime: {} },
        registrationProxy: { enabled: false, configured: false, country: "NL", countries: [] },
        cardLinkProxy: { enabled: false, configured: false, country: "DE", countries: [], modes: [] },
        roxyRegistration: { available: false, configured: false, workspaces: [], profiles: [] },
        smsBower: { configured: false, service: "dr", domain: "gmail.com", maxPrice: 0.05 },
        paymentSms: { configured: false, provider: "", label: "", timeoutSeconds: 60, routing: {} },
        zkgmail: { configured: false, domain: "cclgmail.com", domains: ["cclgmail.com", "zkgmail.com", "shukunlabs.xyz"], forwardAccount: "352***4@qq.com" },
        verificationTask: { status: "idle", runtime: {} },
        paypal: { available: false, running: false, error: "", url: "/paypal-pay/" },
        inbox: { configured: false, codeCount: 0 },
        selectedAccountEmail: "",
        registrationMode: "headed",
        quickFlow: {
          status: "idle", phase: "prepare", progress: 0, taskId: "", manager: "browser",
          registered: 0, generated: 0, paymentStarted: 0, paymentSucceeded: 0,
          failed: 0, emails: [], results: [], logs: [],
          message: "尚未启动流水线", currentEmail: "", currentAction: "",
        },
        quickFlows: [],
        activeQuickFlowId: "",
        selectedCardEmail: "",
        selectedVerificationEmail: "",
        verificationFilter: "all",
        settingsSection: "imap",
      });
      this.quickFlowHistoryPresenter = window.HmeQuickFlowHistory.create({ api: this.api, store: this.store });
      this.quickFlowResumePresenter = window.HmeQuickFlowHistory.createResume({
        api: this.api, flowById: (runId) => this.quickFlowById(runId),
        patchFlow: (...args) => this.patchQuickFlow(...args), startPayment: (...args) => this.startQuickFlowPaypalPayment(...args),
        monitorPayment: (...args) => this.monitorQuickFlowPaypalPayment(...args), retryCardLink: (...args) => this.retryQuickCardLink(...args),
        reloadAccounts: () => this.loadAccounts(),
      });
      this.quickFlowHistoryLoaded = false;
      this.accountsRequestSequence = 0;
      this.quickFlowAccountResultPresenter = new window.QuickFlowAccountResultPresenter(this.api, this.store, { refreshAccounts: () => this.loadAccounts() });
      this.renderer = new WorkspaceRenderer(this.store, this.quickFlowAccountResultPresenter);
      this.sidebarPresenter = new NavigationSidebarPresenter(new NavigationSidebarView());
      this.registrationConfigPresenter = new RegistrationConfigPresenter(
        new RegistrationConfigModel(),
        new RegistrationConfigView(),
      );
      this.quickFlowConfigPresenter = new QuickFlowConfigPresenter(new QuickFlowConfigModel(), new QuickFlowConfigView());
      this.paypalPaymentMonitorPresenter = new PayPalPaymentMonitorPresenter(this.api);
      this.terminalLogPresenter = window.HmeTerminalLog.create({
        lookup: $, escapeHtml, formatLogTimestamp, redactTerminalLogText,
        inferLogContext, taskStatusMeta,
      });
      this.router = new HashRouter({
        overview: () => this.renderer.renderOverview(this.store.state),
        accounts: () => this.renderer.renderAccounts(this.store.state),
        "quick-flow": () => this.renderer.renderQuickFlow(this.store.state),
        network: () => this.renderer.renderNetwork(this.store.state),
        "card-links": () => this.renderer.renderCardLinks(this.store.state),
        "pp-payment": () => this.renderer.renderPayPal(this.store.state),
        verification: () => this.renderer.renderVerification(this.store.state),
        settings: () => this.renderer.renderSettings(this.store.state),
      });
      this.commands = new CommandBus((message, type) => this.toast(message, type));
      this.pollTimers = {};
      this.autoRefreshTimer = 0;
      this.refreshInFlight = null;
      this.clockTimer = 0;
      this.bindCommands();
    }

    render(state) {
      this.renderer.renderShell(state);
      this.renderer.renderOverview(state);
      this.renderer.renderNetwork(state);
      this.renderer.renderAccounts(state);
      this.registrationConfigPresenter.present();
      this.renderer.renderProtocolRegistration(state);
      this.renderer.renderQuickFlow(state);
      this.quickFlowConfigPresenter.present();
      this.renderer.renderCardLinks(state);
      this.renderer.renderVerification(state);
      this.terminalLogPresenter.present(state);
      if (this.router.current === "pp-payment") this.renderer.renderPayPal(state);
      if (this.router.current === "settings") this.renderer.renderSettings(state);
    }

    applyTheme(theme, persist = true) {
      const resolved = theme === "system"
        ? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")
        : theme;
      document.documentElement.dataset.theme = resolved;
      document.querySelector('meta[name="theme-color"]').content = resolved === "dark" ? "#0d0d0d" : "#f7f7f5";
      if (persist) localStorage.setItem("hme_theme", theme);
      if (this.router.current === "settings") this.renderer.renderSettings(this.store.state);
    }

    toast(message, type = "") {
      const toast = $("toast");
      toast.textContent = message;
      toast.className = "toast " + type;
      requestAnimationFrame(() => toast.classList.add("show"));
      clearTimeout(this.toastTimer);
      this.toastTimer = setTimeout(() => toast.classList.remove("show"), 2800);
    }

    schedule(name, callback, delay = 1500) {
      clearTimeout(this.pollTimers[name]);
      this.pollTimers[name] = setTimeout(callback, delay);
    }

    updateWorkspaceClock() {
      $("workspaceLocalTime").textContent = new Date().toLocaleString("zh-CN", {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
      }).replaceAll("/", "-");
    }

    async refreshWorkspaceData() {
      if (this.refreshInFlight) return this.refreshInFlight;
      const operation = Promise.allSettled([
        this.loadQuickFlowHistory(), this.loadAccounts(), this.loadBrowserTask(), this.loadRegistrationTask(),
        this.loadProtocolRegistrationTask(), this.loadVerificationTask(), this.loadInbox(),
        this.loadRegistrationProxy(), this.loadCardLinkProxy(), this.loadRoxyRegistration(),
        this.loadSmsBower(), this.loadPaymentSms(), this.loadZkgmail(), this.loadPayPal(),
      ]);
      this.refreshInFlight = operation;
      try {
        return await operation;
      } finally {
        if (this.refreshInFlight === operation) this.refreshInFlight = null;
      }
    }

    scheduleWorkspaceRefresh() {
      clearTimeout(this.autoRefreshTimer);
      if (!$("workspaceAutoRefresh").checked) return;
      const delay = [5000, 10000, 30000].includes(Number($("workspaceRefreshInterval").value))
        ? Number($("workspaceRefreshInterval").value) : 5000;
      this.autoRefreshTimer = setTimeout(async () => {
        await this.refreshWorkspaceData();
        this.scheduleWorkspaceRefresh();
      }, delay);
    }

    setTerminalPreviewCollapsed(collapsed, persist = true) {
      const panel = $("workbenchTerminalPanel");
      const button = panel.querySelector('[data-action="toggle-terminal-preview"]');
      document.documentElement.dataset.terminalCollapsed = collapsed ? "true" : "false";
      document.body.classList.toggle("terminal-preview-collapsed", collapsed);
      panel.classList.toggle("is-collapsed", collapsed);
      button.textContent = collapsed ? "⌃" : "⌄";
      button.setAttribute("aria-expanded", String(!collapsed));
      button.setAttribute("aria-label", collapsed ? "展开任务日志" : "折叠任务日志");
      button.title = collapsed ? "展开任务日志" : "折叠任务日志";
      if (persist) localStorage.setItem("hme_terminal_preview_collapsed", collapsed ? "1" : "0");
    }

    async loadQuickFlowHistory() {
      if (this.quickFlowHistoryLoaded) return;
      const flows = await this.quickFlowHistoryPresenter.restore();
      this.quickFlowHistoryLoaded = true;
      flows.filter((flow) => flow.status === "running").forEach((flow) => {
        if (flow.phase === "register" && flow.taskId) {
          this.schedule("quick-flow:" + flow.runId, () => this.pollQuickFlow(flow.runId), 600);
          return;
        }
        this.patchQuickFlow(flow.runId, {
          status: "failed", interrupted: true,
          currentAction: "页面重新载入后前端流水线已中断",
          message: "流程记录已恢复；未完成的前端编排需要重新启动",
        }, "页面重新载入后恢复历史记录；未完成流程已标记为中断");
      });
    }

    async loadAccounts() {
      const requestSequence = ++this.accountsRequestSequence;
      const data = await this.api.get("/api/gpt-emails");
      if (requestSequence !== this.accountsRequestSequence) return;
      const accounts = data.items || [];
      const patch = { accounts };
      if (!this.store.state.selectedCardEmail && data.items?.length) {
        patch.selectedCardEmail = data.items.find((item) => item.sessionStatus === "ready")?.email || data.items[0].email;
      }
      this.store.patch(patch);
    }

    async loadBrowserTask() {
      try {
        const data = await this.api.get("/api/browser/status");
        const wasRunning = Boolean(this.store.state.browserTask.running);
        const patch = { browserTask: data };
        if (data.runtime?.forceHeadless && this.store.state.registrationMode === "headed") {
          patch.registrationMode = "headless";
          localStorage.setItem("hme_registration_mode", "headless");
        }
        this.store.patch(patch);
        if (data.running) this.schedule("browser", () => this.loadBrowserTask(), 500);
        else if (wasRunning) await this.loadAccounts();
      } catch (error) {
        this.toast(error.message, "error");
        this.schedule("browser", () => this.loadBrowserTask(), 2500);
      }
    }

    async loadRegistrationTask() {
      try {
        const data = await this.api.get("/api/registration/status");
        const wasRunning = Boolean(this.store.state.registrationTask.running);
        this.store.patch({ registrationTask: data });
        if (data.running) this.schedule("registration", () => this.loadRegistrationTask(), 1200);
        else if (wasRunning) await Promise.all([
          this.loadAccounts(), this.loadBrowserTask(),
        ]);
      } catch (error) {
        this.toast(error.message, "error");
        this.schedule("registration", () => this.loadRegistrationTask(), 2500);
      }
    }

    async loadProtocolRegistrationTask() {
      try {
        const data = await this.api.get("/api/protocol-registration/status");
        const wasRunning = Boolean(this.store.state.protocolRegistrationTask.running);
        const patch = { protocolRegistrationTask: data };
        if (data.running) {
          patch.registrationMode = "protocol";
          localStorage.setItem("hme_registration_mode", "protocol");
        }
        this.store.patch(patch);
        if (data.running) {
          this.schedule("protocol-registration", () => this.loadProtocolRegistrationTask(), 1200);
        } else if (wasRunning) {
          await this.loadAccounts();
        }
      } catch (error) {
        this.toast(error.message, "error");
        this.schedule("protocol-registration", () => this.loadProtocolRegistrationTask(), 2500);
      }
    }

    async loadVerificationTask() {
      try {
        const data = await this.api.get("/api/account-verification/status");
        const wasRunning = Boolean(this.store.state.verificationTask.running);
        this.store.patch({ verificationTask: data });
        if (data.running) this.schedule("verification", () => this.loadVerificationTask());
        else if (wasRunning) await this.loadAccounts();
      } catch (error) {
        this.toast(error.message, "error");
        this.schedule("verification", () => this.loadVerificationTask(), 2500);
      }
    }

    async loadInbox() {
      const data = await this.api.get("/api/inbox/status");
      this.store.patch({ inbox: data });
    }

    async loadPayPal() {
      try { this.store.patch({ paypal: await this.api.get("/api/paypal/status") }); }
      catch (error) { this.store.patch({ paypal: {running: false, error: error.message || String(error)} }); throw error; }
    }

    async loadRegistrationProxy() {
      const data = await this.api.get("/api/registration-proxy/status");
      this.store.patch({ registrationProxy: data });
    }

    async loadCardLinkProxy() {
      const data = await this.api.get("/api/card-link-proxy/status");
      this.store.patch({ cardLinkProxy: data });
    }

    async loadRoxyRegistration() {
      const data = await this.api.get("/api/roxy-registration/status");
      this.store.patch({ roxyRegistration: data });
    }

    async loadSmsBower() {
      const data = await this.api.get("/api/smsbower/status");
      this.store.patch({ smsBower: data });
    }

    async loadPaymentSms() {
      const data = await this.api.get("/api/payment-sms/status");
      this.store.patch({ paymentSms: data });
    }

    async loadZkgmail() {
      const data = await this.api.get("/api/zkgmail/status");
      this.store.patch({ zkgmail: data });
    }

    browserOptions() {
      const concurrency = Number($("concurrency").value);
      if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 10) {
        throw new Error("并发数必须是 1–10 的整数");
      }
      const mode = this.store.state.registrationMode || "headed";
      if (mode === "protocol") {
        throw new Error("协议注册请使用 iCloud 库存注册按钮");
      }
      if (mode === "roxy" && !this.store.state.roxyRegistration?.configured) {
        const profile = $("roxyProfile");
        profile.focus();
        profile.scrollIntoView({ behavior: "smooth", block: "center" });
        throw new Error("请先在上方“专用指纹环境”中选择一个 Roxy 环境，再点击注册");
      }
      const roxyConcurrency = Number($("roxyConcurrency").value);
      if (mode === "roxy" && (
        !Number.isInteger(roxyConcurrency) || roxyConcurrency < 1 || roxyConcurrency > 5
      )) {
        throw new Error("Roxy 并发窗口必须是 1–5 的整数");
      }
      const availableRoxyProfiles = Number(
        this.store.state.roxyRegistration?.maxConcurrency || 0
      );
      if (mode === "roxy" && roxyConcurrency > availableRoxyProfiles) {
        throw new Error(
          "Roxy 未打开环境不足：需要 " + roxyConcurrency + " 个，当前可用 " +
          availableRoxyProfiles + " 个"
        );
      }
      const roxyTargetCount = Number($("roxyTargetCount").value);
      if (mode === "roxy" && (
        !Number.isInteger(roxyTargetCount) || roxyTargetCount < 1 || roxyTargetCount > 100
      )) {
        throw new Error("Roxy 目标账号数必须是 1–100 的整数");
      }
      return {
        headless: mode === "headless" ||
          (mode === "roxy" && $("roxyWindowMode").value === "background"),
        concurrency: mode === "roxy" ? roxyConcurrency : concurrency,
        target_count: mode === "roxy" ? roxyTargetCount : concurrency,
        browser_engine: mode === "roxy" ? "roxy" : "camoufox",
      };
    }

    verificationConcurrency() {
      const concurrency = Number($("verificationConcurrency").value);
      if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 10) {
        throw new Error("验证并发必须是 1–10 的整数");
      }
      return concurrency;
    }

    assertProtocolRuntime() {
      const runtime = this.store.state.protocolRegistrationTask?.runtime || {};
      if (!runtime.available) {
        throw new Error("协议运行环境未就绪：" + (runtime.error || "请重启账号工作台后重试"));
      }
      return runtime;
    }

    moveVerificationSelection(offset) {
      const accounts = this.store.state.accounts;
      if (!accounts.length) throw new Error("暂无可验证账号");
      const current = accounts.findIndex(
        (item) => item.email === this.store.state.selectedVerificationEmail
      );
      const base = current < 0 ? (offset > 0 ? -1 : 0) : current;
      const next = (base + offset + accounts.length) % accounts.length;
      this.store.patch({ selectedVerificationEmail: accounts[next].email });
      return "已选择 " + accounts[next].email;
    }

    selectedAccount(email) {
      return this.store.state.accounts.find((item) => item.email === email);
    }

    async startAccountVerification(email) {
      const item = this.selectedAccount(email);
      if (!item) throw new Error("请先选择一个账号");
      if (!item.hasCookies) throw new Error("该账号尚未保存 Cookie，请先重新登录或注册获取 Cookie");
      this.store.patch({ selectedVerificationEmail: item.email });
      const data = await this.api.post("/api/account/verify-or-register", {
        email: item.email, headless: $("headless").checked, reset_password: false,
        refresh_with_cookie: true,
      });
      if (data.task) this.store.patch({ verificationTask: data.task });
      await Promise.all([this.loadAccounts(), this.loadBrowserTask(), this.loadVerificationTask()]);
      if (data.mode === "deleted_invalid") {
        return data.message || "无效邮箱已自动删除；请选择下一个账号继续验证";
      }
      this.schedule("verification", () => this.loadVerificationTask(), 800);
      return data.mode === "refresh_cookie"
        ? "正在使用保存的 Cookie 刷新 Session / AT 并查询实时套餐"
        : "正在获取 Session 并验证套餐";
    }

    async copyText(value) {
      const text = String(value ?? "");
      try {
        await navigator.clipboard.writeText(text);
      } catch (_) {
        const area = document.createElement("textarea");
        area.value = text;
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.append(area);
        area.select();
        if (!document.execCommand("copy")) throw new Error("浏览器拒绝复制，请检查剪贴板权限");
        area.remove();
      }
    }

    quickFlowList(state = this.store.state) {
      return Array.isArray(state.quickFlows) ? state.quickFlows : [];
    }

    activeQuickFlow(state = this.store.state) {
      const flows = this.quickFlowList(state);
      const activeId = String(state.activeQuickFlowId || "");
      return flows.find((item) => item.runId === activeId) || flows.at(-1) || state.quickFlow || {};
    }

    quickFlowById(runId) {
      const target = String(runId || "");
      return this.quickFlowList().find((item) => item.runId === target) || null;
    }

    createQuickFlowId() {
      if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
      return "quick-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
    }

    selectQuickFlow(runId) {
      const flow = this.quickFlowById(runId);
      if (!flow) throw new Error("该流水线已不在当前列表中");
      this.store.patch({ activeQuickFlowId: flow.runId, quickFlow: flow });
      return flow;
    }

    patchQuickFlow(runId, changes, logMessage = "") {
      const target = String(runId || "");
      const flows = this.quickFlowList();
      const index = flows.findIndex((item) => item.runId === target);
      if (index < 0) return null;
      const current = flows[index];
      const logs = [...(current.logs || [])];
      if (logMessage) {
        logs.push({ at: new Date().toISOString(), message: logMessage });
      }
      const terminal = ["completed", "failed", "cancelled"].includes(changes.status);
      const lifecycle = changes.status === "running" && current.status !== "running"
        ? { finishedAt: "", interrupted: false }
        : terminal && !changes.finishedAt && !current.finishedAt
          ? { finishedAt: new Date().toISOString() } : {};
      const next = { ...current, ...changes, ...lifecycle, logs: logs.slice(-120) };
      const nextFlows = [...flows];
      nextFlows[index] = next;
      const activeId = this.store.state.activeQuickFlowId || target;
      const active = nextFlows.find((item) => item.runId === activeId) || next;
      this.store.patch({
        quickFlows: nextFlows,
        activeQuickFlowId: active.runId,
        quickFlow: active,
      });
      void this.quickFlowHistoryPresenter.persist(next).catch((error) =>
        this.toast("流程记录保存失败：" + error.message, "error")
      );
      return next;
    }

    quickCardLinkPayload(email, forceRetry = false, flow = null) {
      const snapshot = flow || this.activeQuickFlow();
      const method = ["de_oaics_paypal", "paypal_us", "paypal_gb"].includes(snapshot.method)
        ? snapshot.method
        : ["de_oaics_paypal", "paypal_us", "paypal_gb"].includes($("quickCardLinkMethod").value)
          ? $("quickCardLinkMethod").value : "de_oaics_paypal";
      const config = cardLinkExtractionModes[method];
      const singleProxy = Boolean(config.singleProxy);
      const firstProxyCountry = cardLinkCountryPolicy.resolve(
        method,
        snapshot.extractionFirstProxyCountry || $("quickExtractionFirstProxyCountry").value,
      );
      const secondProxyCountry = singleProxy ? firstProxyCountry : cardLinkCountryPolicy.resolve(
        method,
        snapshot.extractionSecondProxyCountry || $("quickExtractionSecondProxyCountry").value,
      );
      return {
        email,
        method,
        country: config.country,
        proxy_mode: snapshot.extractionProxyMode || $("quickExtractionProxyMode").value,
        create_proxy_country: firstProxyCountry,
        promotion_proxy_country: secondProxyCountry,
        secondary_proxy_country: secondProxyCountry,
        reuse_registration_proxy: false,
        independent_proxy_pair: !singleProxy,
        use_secondary_proxy: !singleProxy && Boolean(forceRetry),
        promotion_proxy_choice: singleProxy
          ? "first"
          : snapshot.promotionProxyChoice || $("quickPromotionProxyChoice").value || "first",
        target_amount: config.targetAmount
          ? String(snapshot.targetAmount ?? $("quickCardLinkTargetAmount").value).trim()
          : config.fixedTargetAmount || "0",
        force_retry: Boolean(forceRetry),
        attempt_limit: Math.max(1, Math.min(
          100, Number(snapshot.extractionCount || $("quickExtractionCount").value || 1),
        )),
      };
    }

    async requestQuickFlowCardLink(runId, payload) {
      const progressId = "cardlink-" + this.createQuickFlowId().replace(/[^A-Za-z0-9_-]/g, "");
      const liveMessageCounts = new Map();
      let logSequence = 0;
      let stopping = false;

      const appendMessage = (value) => {
        const message = String(value || "").trim();
        if (!message) return;
        liveMessageCounts.set(message, Number(liveMessageCounts.get(message) || 0) + 1);
        this.patchQuickFlow(runId, {}, "[直卡提链] " + message);
      };
      const pollOnce = async () => {
        const data = await this.api.get(
          "/api/account/card-link/progress/" + encodeURIComponent(progressId) +
          "?log_after=" + logSequence,
        );
        for (const item of data.logs || []) {
          const sequence = Number(item.sequence || 0);
          if (sequence > logSequence) {
            appendMessage(item.message);
            logSequence = sequence;
          }
        }
        logSequence = Math.max(logSequence, Number(data.logSequence || 0));
      };
      const monitor = (async () => {
        while (!stopping) {
          try { await pollOnce(); } catch (_) { /* progress may not exist yet */ }
          await new Promise((resolve) => setTimeout(resolve, 500));
        }
      })();

      let data = null;
      let requestError = null;
      try {
        data = await this.api.post("/api/account/card-link", {
          ...payload,
          progress_id: progressId,
        });
      } catch (error) {
        requestError = error;
      } finally {
        stopping = true;
        await monitor;
        try { await pollOnce(); } catch (_) { /* final response remains fallback */ }
      }

      const finalLogs = requestError?.logs || data?.logs || [];
      const remainingLiveCounts = new Map(liveMessageCounts);
      for (const value of finalLogs) {
        const message = String(value || "").trim();
        const remaining = Number(remainingLiveCounts.get(message) || 0);
        if (remaining > 0) {
          remainingLiveCounts.set(message, remaining - 1);
        } else {
          appendMessage(message);
        }
      }
      if (requestError) throw requestError;
      return data;
    }

    async startQuickFlowPaypalPayment(runId, result, results, progress = 96) {
      const email = String(result?.email || "").trim().toLowerCase();
      if (!email || !result?.url) throw new Error("协议支付缺少账号或 PayPal 链接");
      const postPaymentPhoneBinding = this.quickFlowById(runId)?.postPaymentPhoneBinding === true;
      this.patchQuickFlow(runId, {
        phase: "payment",
        progress: Math.max(0, Math.min(99, Number(progress || 96))),
        currentEmail: email,
        currentAction: "正在自动选择代理、获取接码手机号并启动协议支付",
        results: [...results],
      }, "PayPal 链接已生成，自动启动协议支付：" + email);
      try {
        const data = await this.api.post("/api/account/paypal-payment", { email, post_payment_phone_binding: postPaymentPhoneBinding });
        result.paymentStarted = true;
        result.paymentError = "";
        result.paymentJobId = String(data.job?.id || "");
        if (!result.paymentJobId) throw new Error("协议服务未返回任务 ID");
        result.paymentStatus = String(data.job?.status || "queued");
        result.paymentStage = String(data.job?.stage || "协议支付任务已排队");
        result.paymentSucceeded = false;
        result.paymentConfirmed = false;
        result.paymentProtocolSucceeded = false;
        result.paymentPlusConfirmed = false;
        result.paymentPending = false;
        result.paymentAtRefreshStatus = "";
        result.paymentAtRefreshed = false;
        result.paymentAccountType = "";
        result.paymentConfirmationError = "";
        result.paymentDeliveryError = "";
        result.paymentPostCheckError = "";
        result.paymentLogs = [];
        result.paymentLogCount = 0;
        result.paymentCountry = String(data.country || result.country || "").toUpperCase();
        result.paymentProxyMode = String(data.proxyMode || "");
        result.paymentProxySource = String(data.proxySource || "");
        result.paymentProxyCandidateCount = Number(data.proxyCandidateCount || 0);
        result.paymentProxyBackupCount = Number(data.proxyBackupCount || 0);
        result.paymentSmsProvider = String(data.smsProviderLabel || data.smsProvider || "接码平台");
        result.postPaymentPhoneBinding = data.postPaymentPhoneBinding === true;
        this.patchQuickFlow(runId, {
          results: [...results],
          paymentStarted: results.filter((item) => item.paymentStarted).length,
          currentAction: "协议支付任务已自动启动",
        }, "协议支付已启动：" + email + " · 自动代理 · " +
          result.paymentProxyCandidateCount + " 个实测出口（" +
          result.paymentProxyBackupCount + " 备用） · " +
          String(data.smsProviderLabel || data.smsProvider || "接码平台") + " 自动取号" +
          (result.postPaymentPhoneBinding ? " · Plus 确认后继续接码" : " · Plus 确认后直接结束") +
          (result.paymentJobId ? " · 任务 " + result.paymentJobId.slice(0, 12) : ""));
        return true;
      } catch (error) {
        result.paymentStarted = false;
        result.paymentError = error.message || "协议支付启动失败";
        this.patchQuickFlow(runId, {
          results: [...results],
          currentAction: "PayPal 链接已生成，但协议支付启动失败",
        }, "协议支付启动失败：" + email + " · " + result.paymentError);
        return false;
      }
    }

    async monitorQuickFlowPaypalPayment(runId, result, results) {
      const email = String(result.email || "");
      await this.paypalPaymentMonitorPresenter.monitor(
        result,
        (snapshot) => {
          Object.assign(result, snapshot.fields || {});
          const currentAction = snapshot.retryError
            ? "协议支付状态读取失败，正在自动重试"
            : result.paymentError ? "协议支付失败"
              : result.paymentDeliveryError
                ? "支付成功并确认 Plus，但手机号/Codex 后处理失败"
              : result.paymentConfirmationError
                  ? "支付成功，但 AT/Plus 后置校验失败"
                  : result.paymentAtRefreshStatus === "plus_sms"
                    ? (result.paymentStage || "Plus 已确认，正在进行手机号/Codex 后处理")
                  : result.paymentPlusConfirmed
                    ? "支付成功，新 AT 已确认 Plus"
                    : result.paymentPending
                      ? "支付成功，正在用 Cookie 登录获取新 AT 并确认 Plus"
                      : result.paymentSucceeded ? "协议支付成功" : result.paymentStage;
          const logMessage = snapshot.retryError
            ? "协议支付状态暂时读取失败，稍后自动重试：" + snapshot.retryError
            : snapshot.stageChanged || snapshot.terminal
              ? "[协议支付] " + email + " · " +
                (result.paymentDeliveryError || currentAction) : "";
          this.patchQuickFlow(runId, {
            phase: "payment", progress: 99, results: [...results],
            paymentSucceeded: results.filter((item) => item.paymentSucceeded).length,
            paymentPending: results.filter((item) => item.paymentPending).length,
            currentEmail: email, currentAction,
          }, logMessage);
        },
        () => this.quickFlowById(runId)?.status === "running",
      );
      return Boolean(result.paymentSucceeded);
    }

    async retryQuickCardLink(email, runId = "") {
      const target = String(email || "").trim().toLowerCase();
      if (!target) throw new Error("缺少需要重新提链的账号");
      const flow = this.quickFlowById(runId) || this.activeQuickFlow();
      if (!flow?.runId) throw new Error("未找到该账号对应的流水线");
      if (flow.status === "running") throw new Error("该流水线正在运行，请完成后再重试");
      const previousResults = [...(flow.results || [])];
      const existingIndex = previousResults.findIndex((item) =>
        String(item.email || "").trim().toLowerCase() === target
      );
      const retrying = {
        ...(existingIndex >= 0 ? previousResults[existingIndex] : {}),
        ok: false,
        skipped: false,
        retryable: true,
        retrying: true,
        email: target,
        error: "正在强制重新提链",
      };
      if (existingIndex >= 0) previousResults[existingIndex] = retrying;
      else previousResults.push(retrying);
      this.patchQuickFlow(flow.runId, {
        status: "running", phase: "extract", progress: 92,
        currentEmail: target, currentAction: "正在重新提取 " +
          cardLinkRuntimeConfig(flow.method || "de_oaics_paypal").label,
        message: "正在对失败账号重新提链", results: previousResults,
      }, "重新提链已启动：" + target);
      try {
        await this.loadAccounts();
        const account = this.selectedAccount(target);
        if (!account) throw new Error("账号不存在，请刷新后重试");
        if (account.accountType === "plus") {
          const skippedResult = {
            ok: true,
            skipped: true,
            retryable: false,
            retrying: false,
            email: target,
            reason: "already_plus",
          };
          const nextResults = previousResults.map((item) =>
            String(item.email || "").trim().toLowerCase() === target ? skippedResult : item
          );
          this.patchQuickFlow(flow.runId, {
            status: "completed", phase: "complete", progress: 100, results: nextResults,
            skipped: nextResults.filter((item) => item.skipped).length,
            failed: nextResults.filter((item) => !item.ok).length,
            currentEmail: "", currentAction: "账号已是 Plus，已跳过提链支付",
            message: "该账号已确认 Plus，无需重新提链或支付",
          }, "账号已是 Plus 套餐，重新提链已跳过：" + target);
          return "该账号已是 Plus 套餐，无需重新提链支付：" + target;
        }
        if (account.sessionStatus !== "ready") throw new Error("该账号 Session / AT 尚未就绪");
        const data = await this.requestQuickFlowCardLink(
          flow.runId, this.quickCardLinkPayload(target, true, flow),
        );
        const classified = data.cardLinkStatus === "cs_live";
        const attemptCount = Number(data.attemptCount || 1);
        const attemptLimit = Number(data.attemptLimit || flow.extractionCount || 1);
        const nextResult = classified
          ? {
              ok: false, skipped: false, retryable: true, retrying: false,
              email: target, error: "连续 " + attemptCount + "/" + attemptLimit +
                " 次返回 cs_live，尚未获得 OAICS 支付链接",
            }
          : {
              ok: true, skipped: false, retryable: false, retrying: false,
              email: target, url: data.url,
              country: data.link_proxy_country || account.cardLinkProxyCountry || data.country || account.cardLinkCountry,
            };
        const nextResults = previousResults.map((item) =>
          String(item.email || "").trim().toLowerCase() === target ? nextResult : item
        );
        let paymentReady = false;
        if (!classified) {
          paymentReady = await this.startQuickFlowPaypalPayment(
            flow.runId, nextResult, nextResults, 98,
          );
          if (paymentReady) {
            paymentReady = await this.monitorQuickFlowPaypalPayment(
              flow.runId, nextResult, nextResults,
            );
          }
        }
        const generated = nextResults.filter((item) => item.ok && !item.skipped).length;
        const paymentStarted = nextResults.filter((item) => item.paymentStarted).length;
        const paymentSucceeded = nextResults.filter((item) => item.paymentSucceeded).length;
        const skipped = nextResults.filter((item) => item.skipped).length;
        const failed = nextResults.filter((item) => !item.ok || item.paymentError).length;
        const paymentPostCheckFailed = Boolean(nextResult.paymentPostCheckError);
        this.patchQuickFlow(flow.runId, {
          status: failed ? "failed" : "completed",
          phase: classified ? "extract" : paymentReady ? "complete" : "payment",
          progress: 100, results: nextResults, generated, paymentStarted, paymentSucceeded, skipped, failed,
          currentEmail: "",
          currentAction: classified
            ? "提链次数已用完，仍未获得 OAICS"
            : paymentReady
              ? paymentPostCheckFailed
                ? "重新提链并完成协议支付；AT/Plus 后置校验失败"
                : "重新提链并完成协议支付"
              : "重新提链成功，但协议支付失败",
          message: classified
            ? "已连续尝试 " + attemptCount + " 次，均返回 cs_live"
            : paymentReady
              ? "第 " + attemptCount + " 次提链成功，协议支付已完成" +
                (paymentPostCheckFailed ? "；AT/Plus 后置校验失败" : "")
              : "第 " + attemptCount + " 次提链成功，但协议支付失败",
        }, classified
          ? "cs_live 重试次数已用完：" + target
          : paymentReady
            ? "重新提链并完成协议支付：" + target +
              (paymentPostCheckFailed ? " · AT/Plus 后置校验失败" : "")
            : "重新提链成功，协议支付失败：" + target);
        await this.loadAccounts();
        return classified
          ? "已用完 " + attemptCount + " 次提链，结果仍为 cs_live"
          : paymentReady
            ? "第 " + attemptCount + " 次提链成功并完成协议支付：" + target +
              (paymentPostCheckFailed ? "（AT/Plus 后置校验失败）" : "")
            : "第 " + attemptCount + " 次提链成功，但协议支付失败：" + target;
      } catch (error) {
        const nextResult = {
          ok: false, skipped: false, retryable: error.retryable !== false, retrying: false,
          email: target, error: error.message || "重新提链失败",
        };
        const nextResults = previousResults.map((item) =>
          String(item.email || "").trim().toLowerCase() === target ? nextResult : item
        );
        this.patchQuickFlow(flow.runId, {
          status: "failed", phase: "extract", progress: 100, results: nextResults,
          failed: nextResults.filter((item) => !item.ok).length,
          currentEmail: "", currentAction: "重新提链失败",
          message: "重新提链失败，可再次点击重试",
        }, "重新提链失败：" + target + " · " + error.message);
        throw error;
      }
    }

    async extractQuickFlowAccounts(runId, emails) {
      await this.loadAccounts();
      const uniqueEmails = [...new Set((emails || []).map((email) =>
        String(email || "").trim().toLowerCase()
      ).filter(Boolean))];
      const accountMap = new Map(this.store.state.accounts.map((item) => [
        String(item.email || "").trim().toLowerCase(), item,
      ]));
      const results = [];
      let generated = 0;
      let paymentStarted = 0;
      let skipped = 0;
      let failed = 0;
      let attempted = 0;
      const startedFlow = this.quickFlowById(runId);
      if (!startedFlow) return;
      const method = ["de_oaics_paypal", "paypal_us", "paypal_gb"].includes(startedFlow.method)
        ? startedFlow.method : "de_oaics_paypal";
      const methodConfig = cardLinkExtractionModes[method];
      const extractionCount = Math.max(
        1, Math.min(100, Number(startedFlow.extractionCount || 1)),
      );
      for (let index = 0; index < uniqueEmails.length; index += 1) {
        const activeFlow = this.quickFlowById(runId);
        if (!activeFlow || activeFlow.status !== "running") return;
        const email = uniqueEmails[index];
        const account = accountMap.get(email);
        this.patchQuickFlow(runId, {
          phase: "extract",
          progress: 65 + Math.round(index / Math.max(1, uniqueEmails.length) * 30),
          currentEmail: email,
          currentAction: "正在提取 " + methodConfig.label + "（" +
            (index + 1) + "/" + uniqueEmails.length + "）",
          results: [...results], generated, paymentStarted, skipped, failed,
        }, "检查已有 PayPal 链接：" + email);
        if (account?.accountType === "plus") {
          skipped += 1;
          results.push({
            ok: true,
            skipped: true,
            email,
            reason: "already_plus",
          });
          this.patchQuickFlow(
            runId,
            { results: [...results], generated, paymentStarted, skipped, failed },
            "账号已是 Plus 套餐，已跳过提链支付：" + email,
          );
          continue;
        }
        if (hasGeneratedCardLinkForMethod(account, method)) {
          skipped += 1;
          results.push({
            ok: true,
            skipped: true,
            email,
            url: account.cardLink,
          });
          this.patchQuickFlow(
            runId,
            { results: [...results], generated, paymentStarted, skipped, failed },
            "账号已有同模式 PayPal 链接，已跳过重复创建：" + email,
          );
          continue;
        }
        if (!account || account.sessionStatus !== "ready") {
          failed += 1;
          results.push({ ok: false, retryable: true, email, error: "注册完成，但 Session / AT 尚未就绪" });
          this.patchQuickFlow(
            runId,
            { results: [...results], generated, paymentStarted, skipped, failed },
            "Session 未就绪，已记录：" + email,
          );
          continue;
        }
        try {
          const data = await this.requestQuickFlowCardLink(
            runId, this.quickCardLinkPayload(email, false, activeFlow),
          );
          const accountAttempts = Number(data.attemptCount || 1);
          attempted += accountAttempts;
          const classified = data.cardLinkStatus === "cs_live";
          let paymentReady = false;
          if (classified) {
            failed += 1;
            results.push({
              ok: false, retryable: true, email,
              error: "连续 " + accountAttempts + "/" + extractionCount +
                " 次返回 cs_live，" + methodConfig.label + " 提链次数已用完",
            });
          } else {
            generated += 1;
            const result = {
              ok: true, email, url: data.url,
              country: data.link_proxy_country || account.cardLinkProxyCountry || data.country || account.cardLinkCountry,
            };
            results.push(result);
            paymentReady = await this.startQuickFlowPaypalPayment(
              runId,
              result,
              results,
              68 + Math.round((index + 1) / Math.max(1, uniqueEmails.length) * 28),
            );
            if (paymentReady) paymentStarted += 1;
            else failed += 1;
          }
          this.patchQuickFlow(
            runId,
            { results: [...results], generated, paymentStarted, skipped, failed },
            classified
              ? "cs_live 已自动重试 " + accountAttempts + " 次，次数已用完：" + email
              : paymentReady
                ? "第 " + accountAttempts + " 次生成支付链接并启动协议支付：" + email
                : "第 " + accountAttempts + " 次生成支付链接，但协议支付启动失败：" + email,
          );
        } catch (error) {
          const failedAttempts = Math.max(1, Number(error.attemptCount || 1));
          attempted += failedAttempts;
          failed += 1;
          results.push({
            ok: false,
            retryable: error.retryable !== false,
            email,
            error: error.message || "提链失败",
          });
          this.patchQuickFlow(
            runId,
            { results: [...results], generated, skipped, failed },
            "提链失败（已自动尝试 " + failedAttempts + " 次）：" +
              email + " · " + error.message,
          );
        }
      }
      const monitorTargets = results.filter((item) => item.paymentStarted && item.paymentJobId);
      if (monitorTargets.length) {
        this.patchQuickFlow(runId, {
          phase: "payment", progress: 97, results: [...results],
          currentEmail: monitorTargets[0].email,
          currentAction: "正在自动监听 " + monitorTargets.length + " 个协议支付任务",
        }, "协议支付任务均已启动，开始自动监听最终结果");
        await Promise.all(monitorTargets.map((item) =>
          this.monitorQuickFlowPaypalPayment(runId, item, results)
        ));
      }
      const completedFlow = this.quickFlowById(runId);
      if (!completedFlow || completedFlow.status !== "running") return;
      await this.loadAccounts();
      const paymentSucceeded = results.filter((item) => item.paymentSucceeded).length;
      const paymentPlusConfirmed = results.filter((item) => item.paymentPlusConfirmed).length;
      const paymentPending = results.filter((item) => item.paymentPending).length;
      const paymentPostCheckFailed = results.filter((item) => item.paymentPostCheckError).length;
      failed = results.filter((item) =>
        !item.ok || (item.ok && !item.skipped && !item.paymentSucceeded)
      ).length;
      const completed = results.length > 0 && failed === 0;
      const allAlreadyPlus = results.length > 0 && results.every(
        (item) => item.reason === "already_plus",
      );
      const paymentFailed = results.some((item) => Boolean(item.paymentError));
      this.patchQuickFlow(runId, {
        status: completed ? "completed" : "failed",
        phase: completed ? "complete" : paymentFailed ? "payment" : "extract",
        progress: 100,
        results,
        generated,
        paymentStarted,
        paymentSucceeded,
        paymentPlusConfirmed,
        paymentPending,
        paymentPostCheckFailed,
        skipped,
        failed,
        currentEmail: "",
        currentAction: allAlreadyPlus
          ? "账号均已是 Plus，已跳过提链支付"
          : completed
          ? paymentPostCheckFailed
            ? "注册、提链与协议支付已完成；AT/Plus 后置校验有异常"
            : "注册、提链与协议支付已全部完成"
          : "注册或提链成功，但协议支付未全部成功",
        message: "流水线完成：注册 " + uniqueEmails.length + "，提链调用 " + attempted +
          " 次（单账号上限 " + extractionCount + "），" +
          (method === "de_oaics_paypal" ? "补 OAICS " : "生成链接 ") + generated +
          "，协议支付成功 " + paymentSucceeded + "，新 AT 确认 Plus " + paymentPlusConfirmed +
          "（AT 确认中 " + paymentPending + "，后置校验异常 " + paymentPostCheckFailed +
          "），跳过 " + skipped + "，失败 " + failed,
      }, allAlreadyPlus
        ? "账号均已是 Plus，无需提链支付"
        : completed
        ? "一键注册、提链并协议支付完成" +
          (paymentPostCheckFailed ? "；AT/Plus 后置校验有异常" : "")
        : "流水线结束，但提链或协议支付未全部完成");
    }

    async pollQuickFlow(runId) {
      const flow = this.quickFlowById(runId);
      if (!flow || flow.status !== "running" || flow.phase !== "register") return;
      try {
        const protocol = flow.manager === "protocol";
        const data = await this.api.get(protocol
          ? "/api/protocol-registration/status" : "/api/registration/status");
        this.store.patch(protocol
          ? { protocolRegistrationTask: data }
          : { registrationTask: data });
        const task = (data.tasks || []).find((item) =>
          item.processId === flow.taskId || item.id === flow.taskId
        ) || (data.id === flow.taskId ? data : null);
        if (!task) throw new Error("未找到本次注册任务状态");
        const taskEmails = protocol
          ? (task.accounts || []).map((item) => item.email)
          : (task.emails || []);
        const total = Number(task.total || task.effectiveConcurrency || task.claimed || task.requested || 1);
        const completedCount = Number(task.completed || 0);
        const registrationProgress = protocol
          ? Math.round(completedCount / Math.max(1, total) * 45)
          : task.running ? 20 : 55;
        const latestMessage = String(task.message || "正在注册账号");
        const changes = {
          emails: taskEmails.length ? taskEmails : flow.emails,
          registered: protocol ? Number(task.succeeded || 0) : (task.status === "completed" ? taskEmails.length : 0),
          progress: Math.max(Number(flow.progress || 0), 10 + registrationProgress),
          currentEmail: task.currentEmail || taskEmails[0] ||
            (flow.registrationProvider === "zkgmail"
              ? "正在生成 " + (this.store.state.zkgmail?.domain || "cclgmail.com") + " 邮箱" : "正在领取 iCloud 库存邮箱"),
          currentAction: latestMessage,
          message: latestMessage,
          lastTaskMessage: latestMessage,
        };
        this.patchQuickFlow(runId, changes, latestMessage !== flow.lastTaskMessage ? latestMessage : "");
        if (task.running) {
          this.schedule("quick-flow:" + runId, () => this.pollQuickFlow(runId), 1000);
          return;
        }
        if (task.status !== "completed") {
          this.patchQuickFlow(runId, {
            status: task.status === "cancelled" ? "cancelled" : "failed",
            phase: "register",
            currentAction: latestMessage,
            message: latestMessage,
          }, "注册阶段未完成，流水线已结束");
          return;
        }
        const succeededEmails = protocol
          ? (task.accounts || []).filter((item) => item.status === "success").map((item) => item.email)
          : taskEmails;
        if (!succeededEmails.length) {
          this.patchQuickFlow(
            runId,
            { status: "failed", phase: "session", message: "注册任务没有返回成功账号" },
            "没有可继续提链的注册账号",
          );
          return;
        }
        this.patchQuickFlow(runId, {
          phase: "session",
          progress: 62,
          registered: succeededEmails.length,
          emails: succeededEmails,
          currentEmail: succeededEmails[0],
          currentAction: "注册完成，正在确认 Session / AT",
          message: "已注册 " + succeededEmails.length + " 个账号，准备自动提链",
        }, "注册完成，Session 已保存，开始提链");
        await this.extractQuickFlowAccounts(runId, succeededEmails);
      } catch (error) {
        const current = this.quickFlowById(runId);
        if (!current || current.status !== "running") return;
        this.patchQuickFlow(runId, {
          message: "读取注册进度失败：" + error.message,
          currentAction: "正在重试读取注册状态",
        }, "状态读取失败，2.5 秒后重试：" + error.message);
        this.schedule("quick-flow:" + runId, () => this.pollQuickFlow(runId), 2500);
      }
    }

    bindCommands() {
      this.commands.register("toggle-sidebar", async () => {
        this.sidebarPresenter.toggle();
      });
      this.commands.register("toggle-registration-config", async () => {
        this.registrationConfigPresenter.toggle();
      });
      this.commands.register("save-quick-flow-config", async () => {
        this.quickFlowConfigPresenter.save();
        return "任务配置已保存，可直接开始一键注册";
      });
      this.commands.register("toggle-terminal-preview", async () => {
        const collapsed = !$("workbenchTerminalPanel").classList.contains("is-collapsed");
        this.setTerminalPreviewCollapsed(collapsed);
      });
      this.commands.register("focus-control-task-search", async () => {
        $("controlTaskSearch").focus();
      });
      this.commands.register("refresh", async () => {
        const results = await this.refreshWorkspaceData();
        const failure = results.find((result) => result.status === "rejected");
        if (failure) throw failure.reason;
        this.scheduleWorkspaceRefresh();
        return "数据已刷新";
      });
      this.commands.register("reload-paypal", async () => {
        await this.loadPayPal();
        if (!this.store.state.paypal.running) {
          throw new Error(this.store.state.paypal.error || "PP 支付服务尚未就绪");
        }
        paypalWorkspacePresenter.open(this.store.state.paypal.url);
        return "PP 支付工作台已刷新";
      });
      this.commands.register("open-paypal-workspace", async ({ element }) => {
        await this.loadPayPal();
        if (!this.store.state.paypal.running) {
          throw new Error(this.store.state.paypal.error || "PP 支付服务尚未就绪");
        }
        this.router.navigate("pp-payment");
        const jobId = String(element?.dataset.jobId || "");
        paypalWorkspacePresenter.open(this.store.state.paypal.url, jobId);
        return "已打开 PP 协议支付工作台";
      });
      this.commands.register("one-click-paypal-payment", async ({ element }) => {
        const email = String(element.dataset.email || "").trim().toLowerCase();
        if (!email) throw new Error("支付账号缺失");
        const idleLabel = element.dataset.idleLabel || element.textContent || "一键支付";
        element.disabled = true;
        element.textContent = "正在启动 PP 协议";
        try {
          const data = await this.api.post("/api/account/paypal-payment", { email });
          await this.loadPayPal();
          this.router.navigate("pp-payment");
          const jobId = data.job?.id || "";
          paypalWorkspacePresenter.open(data.url, jobId);
          return "协议支付已启动：自动匹配 " + data.country + " · Cookie " + data.cookieCount +
            " 条 · " + (data.proxySource === "card_link" ? "提链代理" : "注册代理") + " · " +
            Number(data.proxyCandidateCount || 0) + " 个实测出口（" +
            Number(data.proxyBackupCount || 0) + " 备用） · " +
            String(data.smsProviderLabel || data.smsProvider || "接码平台");
        } finally {
          element.disabled = false;
          element.textContent = idleLabel;
        }
      });
      this.commands.register("refresh-roxy", async () => {
        await this.loadRoxyRegistration();
        const roxy = this.store.state.roxyRegistration || {};
        if (!roxy.available) throw new Error(roxy.error || "Roxy OpenAPI 未连接");
        return roxy.configured ? "Roxy 专用环境已就绪" : "Roxy 已连接，请选择专用指纹环境";
      });
      this.commands.register("toggle-theme", async () => {
        this.applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
      });
      this.commands.register("set-theme", async ({ element }) => {
        this.applyTheme(element.dataset.theme);
        return "主题已更新";
      });
      this.commands.register("logout", async () => {
        try { await this.api.post("/api/logout"); }
        finally { location.replace("/login"); }
      });
      this.commands.register("start-quick-flow", async () => {
        const mode = $("quickRegistrationMode").value || "headless";
        const registrationProvider = $("quickRegistrationProvider").value === "zkgmail"
          ? "zkgmail" : "inventory";
        const registrationProviderLabel = registrationProvider === "zkgmail"
          ? (this.store.state.zkgmail?.domain || "cclgmail.com") : "iCloud 库存";
        if (registrationProvider === "zkgmail" && !this.store.state.zkgmail?.configured) {
          throw new Error("请先在账号管理中设置 QQ 邮箱授权码");
        }
        if (!this.store.state.paymentSms?.configured) {
          throw new Error("请先打开 PP 支付中的“接码配置”，自动协议支付需要自动取号与取码");
        }
        const protocol = mode === "protocol";
        const roxy = mode === "roxy";
        let concurrency = Number($("quickRegistrationConcurrency").value);
        let targetCount = Number($("quickRegistrationTargetCount").value);
        const extractionCount = Number($("quickExtractionCount").value);
        const concurrencyLimit = protocol || roxy ? 5 : 10;
        if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > concurrencyLimit) {
          throw new Error("并发窗口必须是 1–" + concurrencyLimit + " 的整数");
        }
        if (protocol) {
          concurrency = 1;
          targetCount = 1;
          this.assertProtocolRuntime();
        } else if (!roxy) {
          targetCount = concurrency;
        }
        if (!Number.isInteger(targetCount) || targetCount < 1 || targetCount > 100) {
          throw new Error("目标账号数必须是 1–100 的整数");
        }
        if (!Number.isInteger(extractionCount) || extractionCount < 1 || extractionCount > 100) {
          throw new Error("单账号提链次数必须是 1–100 的整数");
        }
        const method = ["de_oaics_paypal", "paypal_us", "paypal_gb"].includes($("quickCardLinkMethod").value)
          ? $("quickCardLinkMethod").value : "de_oaics_paypal";
        const methodConfig = cardLinkExtractionModes[method];
        const targetAmount = methodConfig.targetAmount
          ? $("quickCardLinkTargetAmount").value.trim()
          : methodConfig.fixedTargetAmount || "0";
        if (targetAmount && !/^\d+$/.test(targetAmount)) {
          throw new Error("目标金额必须是非负整数，留空表示不校验");
        }
        const configSnapshot = this.quickFlowConfigPresenter.persist();
        const firstProxyCountry = cardLinkCountryPolicy.resolve(
          method, configSnapshot.extractionFirstCountry,
        );
        const secondProxyCountry = methodConfig.singleProxy ? firstProxyCountry :
          cardLinkCountryPolicy.resolve(method, configSnapshot.extractionSecondCountry);
        const cardLinkCountries = {
          [methodConfig.createProxyPreference]: firstProxyCountry,
        };
        if (methodConfig.promotionProxyPreference) {
          cardLinkCountries[methodConfig.promotionProxyPreference] =
            secondProxyCountry;
        }
        const savedExtractionProxy = await this.api.post("/api/card-link-proxy/config", {
          cardLinkModes: { [method]: configSnapshot.extractionProxyMode },
          cardLinkCountries,
        });
        this.store.patch({ cardLinkProxy: savedExtractionProxy });
        if (roxy) {
          const roxyState = this.store.state.roxyRegistration || {};
          if (!roxyState.configured) throw new Error("请先在账号管理中选择 Roxy 专用指纹环境");
          if (concurrency > Number(roxyState.maxConcurrency || 0)) {
            throw new Error("Roxy 可用环境不足，当前最多并发 " + Number(roxyState.maxConcurrency || 0));
          }
        }
        const startedAt = new Date().toISOString();
        const runId = this.createQuickFlowId();
        const flow = {
          runId,
          status: "running", phase: "prepare", progress: 5, taskId: "",
          manager: protocol ? "protocol" : "browser", method, extractionCount, targetCount,
          registrationProvider, targetAmount,
          postPaymentPhoneBinding: configSnapshot.postPaymentPhoneBinding === true, protocolSetupCredentials: protocol,
          registrationMode: mode,
          registrationProxyMode: configSnapshot.registrationProxyMode,
          registrationProxyCountry: configSnapshot.registrationProxyCountry,
          extractionProxyMode: configSnapshot.extractionProxyMode,
          extractionFirstProxyCountry: firstProxyCountry,
          extractionSecondProxyCountry: secondProxyCountry,
          promotionProxyChoice: configSnapshot.promotionProxyChoice || "first",
          configSnapshot,
          registered: 0, generated: 0, paymentStarted: 0, paymentSucceeded: 0,
          paymentPending: 0, skipped: 0, failed: 0, emails: [], results: [],
          message: "正在检查邮箱来源、注册方式、提链代理与协议支付配置",
          currentEmail: "正在获取" + registrationProviderLabel + "邮箱", currentAction: "准备一键流水线",
          startedAt, lastTaskMessage: "",
          logs: [{ at: startedAt, message: "一键注册、提链并协议支付已启动：使用 " +
            registrationProviderLabel + " 邮箱生成 " + methodConfig.label +
            "，随后自动选代理并由 SMSBower 取号" }],
        };
        const flows = [...this.quickFlowList(), flow];
        this.store.patch({ quickFlows: flows, activeQuickFlowId: runId, quickFlow: flow });
        try {
          await this.quickFlowHistoryPresenter.persist(flow);
          const data = protocol
            ? await this.api.post("/api/protocol-registration/start", {
                provider: registrationProvider, concurrency: 1, setup_credentials: true,
              })
            : await this.api.post("/api/registration/start", {
                label: registrationProviderLabel + "一键注册、提链并协议支付",
                provider: registrationProvider,
                headless: mode === "headless" ||
                  (roxy && $("roxyWindowMode").value === "background"),
                concurrency,
                target_count: targetCount,
                browser_engine: roxy ? "roxy" : "camoufox",
              });
          const task = data.task || {};
          const taskId = task.id || task.processId || "";
          this.store.patch(protocol
            ? { protocolRegistrationTask: task }
            : { registrationTask: task });
          this.patchQuickFlow(runId, {
            phase: "register",
            progress: 10,
            taskId,
            emails: data.email ? [data.email] : (task.emails || []),
            currentEmail: data.email || task.email || "正在获取" + registrationProviderLabel + "邮箱",
            currentAction: task.message || "注册任务已启动",
            message: task.message || "注册任务已启动",
          }, "已获取 " + registrationProviderLabel + " 邮箱并启动独立" +
            (protocol ? "协议" : "浏览器") + "注册流程");
          this.schedule("quick-flow:" + runId, () => this.pollQuickFlow(runId), 600);
          return "已新建第 " + flows.length + " 个注册、提链并协议支付流程";
        } catch (error) {
          this.patchQuickFlow(runId, {
            status: "failed", phase: "prepare", progress: 0,
            message: error.message, currentAction: "流水线启动失败",
          }, "启动失败：" + error.message);
          throw error;
        }
      });
      this.commands.register("select-quick-flow", async ({ element }) => {
        const flow = this.selectQuickFlow(element.dataset.runId);
        return "已切换到该流程" + (flow.taskId ? "：" + flow.taskId.slice(0, 8) : "");
      });
      this.commands.register("stop-quick-flow-run", async ({ element }) => {
        const flow = this.quickFlowById(element.dataset.runId);
        if (!flow) throw new Error("该流水线已不在当前列表中");
        if (flow.status !== "running") throw new Error("该流水线当前未运行");
        clearTimeout(this.pollTimers["quick-flow:" + flow.runId]);
        if (flow.phase === "register" && flow.taskId) {
          const endpoint = flow.manager === "protocol"
            ? "/api/protocol-registration/stop" : "/api/registration/stop";
          const data = await this.api.post(endpoint, { process_id: flow.taskId });
          this.store.patch(flow.manager === "protocol"
            ? { protocolRegistrationTask: data.task || {} }
            : { registrationTask: data.task || {} });
        }
        const activePaymentJobs = (flow.results || []).filter((item) =>
          item.paymentJobId && !["completed", "failed", "cancelled"].includes(item.paymentStatus)
        );
        if (activePaymentJobs.length) {
          await Promise.allSettled(activePaymentJobs.map((item) =>
            this.api.post(
              "/api/account/paypal-payment/" + encodeURIComponent(item.paymentJobId) + "/cancel"
            )
          ));
        }
        this.patchQuickFlow(flow.runId, {
          status: "cancelled",
          message: "该注册、提链并协议支付流程已停止",
          currentAction: "流程已由用户停止",
        }, "已发送本流程停止请求");
        return "已停止对应的注册流程";
      });
      this.commands.register("dismiss-quick-flow-run", async ({ element }) => {
        const runId = String(element.dataset.runId || "");
        await this.quickFlowHistoryPresenter.remove(runId);
        const flows = this.quickFlowList().filter((item) => item.runId !== runId);
        const active = flows.find((item) => item.runId === this.store.state.activeQuickFlowId) || flows.at(-1) || {
          status: "idle", phase: "prepare", progress: 0, taskId: "", manager: "browser",
          registered: 0, generated: 0, paymentStarted: 0, paymentSucceeded: 0,
          paymentPending: 0, skipped: 0, failed: 0, emails: [], results: [], logs: [],
          message: "尚未启动流水线", currentEmail: "", currentAction: "",
        };
        this.store.patch({
          quickFlows: flows,
          activeQuickFlowId: active.runId || "",
          quickFlow: active,
        });
        return "已关闭该流程记录";
      });
      this.commands.register("resume-interrupted-quick-flow", async ({ element }) =>
        this.quickFlowResumePresenter.resume(element.dataset.runId)
      );
      this.commands.register("retry-quick-card-link", async ({ element }) =>
        this.retryQuickCardLink(element.dataset.email, element.dataset.runId)
      );
      this.commands.register("remove-no-free-quota-account", async ({ element }) => this.quickFlowAccountResultPresenter.remove(element.dataset.email, element.dataset.runId));
      this.commands.register("retry-quick-payment", async ({ element }) =>
        this.retryQuickCardLink(element.dataset.email, element.dataset.runId)
      );
      this.commands.register("register", async () => {
        const options = this.browserOptions();
        const email = $("registrationEmail").value.trim().toLowerCase();
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
          throw new Error("请输入有效的注册邮箱地址");
        }
        const data = await this.api.post("/api/registration/start", {
          label: "手动邮箱注册", provider: "manual", email, ...options, concurrency: 1,
        });
        this.store.patch({ registrationTask: data.task });
        this.schedule("registration", () => this.loadRegistrationTask(), 800);
        return "已添加邮箱并启动注册：" + email;
      });
      this.commands.register("register-provider", async () => {
        const source = $("registrationEmailProvider").value || "icloud";
        const smsBower = this.store.state.smsBower || {};
        const zkgmail = this.store.state.zkgmail || {};
        const protocolMode = this.store.state.registrationMode === "protocol";
        if (protocolMode) {
          if (!["icloud", "zkgmail"].includes(source)) {
            throw new Error("协议注册当前仅支持 iCloud 或 QQ 转发自有域名邮箱");
          }
          if (source === "zkgmail" && !zkgmail.configured) {
            throw new Error("请先设置 QQ 邮箱授权码");
          }
          this.assertProtocolRuntime();
          const previousTask = this.store.state.protocolRegistrationTask || {};
          const setupCredentials = true;
          $("protocolSetupCredentials").checked = true;
          const protocolProvider = source === "zkgmail" ? "zkgmail" : "inventory";
          const protocolSourceLabel = source === "zkgmail" ? (zkgmail.domain || "cclgmail.com") : "iCloud 库存";
          const startedAt = new Date().toISOString();
          this.store.patch({
            protocolRegistrationTask: {
              ...previousTask,
              setupCredentials,
              starting: true,
              running: false,
              status: "starting",
              phase: "prepare",
              message: "正在获取 " + protocolSourceLabel + " 邮箱并准备协议注册，请稍候",
              currentEmail: "",
              startedAt,
              finishedAt: "",
              logs: [...(previousTask.logs || []), {
                at: startedAt,
                message: "正在获取 " + protocolSourceLabel + " 邮箱并准备协议注册",
                stage: "prepare",
                status: "active",
              }].slice(-30),
            },
          });
          try {
            const data = await this.api.post("/api/protocol-registration/start", {
              provider: protocolProvider,
              concurrency: 1,
              setup_credentials: true,
            });
            this.store.patch({
              protocolRegistrationTask: { ...data.task, starting: false },
            });
            this.schedule("protocol-registration", () => this.loadProtocolRegistrationTask(), 500);
            return source === "zkgmail"
              ? "已生成 " + (zkgmail.domain || "cclgmail.com") + " 邮箱并启动协议注册，验证码将从 QQ 邮箱自动读取"
              : "已从库存领取 iCloud 邮箱并启动协议注册";
          } catch (error) {
            const finishedAt = new Date().toISOString();
            this.store.patch({
              protocolRegistrationTask: {
                ...previousTask,
                starting: false,
                running: false,
                status: "failed",
                phase: "prepare",
                message: "协议注册启动失败：" + error.message,
                currentEmail: "",
                startedAt,
                finishedAt,
                logs: [...(previousTask.logs || []), {
                  at: finishedAt,
                  message: "协议注册启动失败：" + error.message,
                  stage: "prepare",
                  status: "error",
                }].slice(-30),
              },
            });
            throw error;
          }
        }
        const options = this.browserOptions();
        if (source === "gmail") {
          if (!smsBower.configured) throw new Error("请先设置 SMSBower API Key");
          const maxPrice = Number($("smsbowerMaxPrice").value);
          if (!Number.isFinite(maxPrice) || maxPrice < 0.001 || maxPrice > 10) {
            throw new Error("Gmail 最高价必须在 0.001–10 美元之间");
          }
          const config = await this.api.post("/api/smsbower/config", { maxPrice });
          this.store.patch({ smsBower: { ...smsBower, ...config } });
        }
        if (source === "zkgmail" && !zkgmail.configured) {
          throw new Error("请先设置 QQ 邮箱授权码");
        }
        const provider = source === "gmail"
          ? "smsbower" : source === "zkgmail" ? "zkgmail" : "inventory";
        const data = await this.api.post("/api/registration/start", {
          label: source === "gmail" ? "SMSBower Gmail 注册"
            : source === "zkgmail" ? (zkgmail.domain || "cclgmail.com") + " 邮箱注册" : "iCloud 邮箱注册",
          provider,
          ...options,
          concurrency: source === "gmail" ? 1 : options.concurrency,
        });
        this.store.patch({ registrationTask: data.task });
        this.schedule("registration", () => this.loadRegistrationTask(), 500);
        return source === "gmail"
          ? "已启动 SMSBower Gmail 获取与自动注册（" + (options.headless ? "无头" : "前台窗口") + "）"
          : source === "zkgmail"
          ? "已启动 " + (zkgmail.domain || "cclgmail.com") + " 地址生成与 QQ 邮箱自动取码"
          : "已启动 iCloud 库存邮箱注册";
      });
      this.commands.register("stop-protocol-registration", async () => {
        if (!confirm("停止当前协议注册任务？")) {
          throw Object.assign(new Error(), { name: "AbortError" });
        }
        const data = await this.api.post("/api/protocol-registration/stop", {});
        this.store.patch({ protocolRegistrationTask: data.task });
        return data.task.message || "协议注册任务已停止";
      });
      this.commands.register("set-smsbower-key", async () => {
        const apiKey = prompt("输入 SMSBower API Key。Key 只保存在本地数据库，接口和日志不会回传：", "");
        if (apiKey === null) throw Object.assign(new Error(), { name: "AbortError" });
        if (!apiKey.trim()) throw new Error("SMSBower API Key 不能为空");
        const data = await this.api.post("/api/smsbower/config", {
          apiKey: apiKey.trim(), maxPrice: Number($("smsbowerMaxPrice").value), service: "dr",
        });
        this.store.patch({ smsBower: data });
        return "SMSBower API 已保存，可获取 Gmail 并自动注册";
      });
      this.commands.register("set-zkgmail-auth", async () => {
        const authorizationCode = prompt(
          "输入 352121354@qq.com 的 IMAP/SMTP 授权码。授权码只保存在本地数据库，接口和日志不会回传：",
          "",
        );
        if (authorizationCode === null) throw Object.assign(new Error(), { name: "AbortError" });
        if (!authorizationCode.trim()) throw new Error("QQ 邮箱授权码不能为空");
        const data = await this.api.post("/api/zkgmail/config", {
          authorizationCode: authorizationCode.trim(),
        });
        this.store.patch({ zkgmail: data });
        return "QQ 邮箱 IMAP 已连接，" + (data.domain || "cclgmail.com") + " 自动取码可用";
      });
      this.commands.register("submit-registration-code", async () => {
        const task = this.store.state.registrationTask;
        const email = (task.awaitingCodeEmails || [])[0] || task.email || "";
        const code = $("registrationCode").value.replace(/\s+/g, "");
        if (!email) throw new Error("当前没有等待验证码的注册邮箱");
        if (!/^[A-Za-z0-9]{4,10}$/.test(code)) throw new Error("请输入 4–10 位验证码");
        const data = await this.api.post("/api/registration/code", { email, code });
        $("registrationCode").value = "";
        this.store.patch({ registrationTask: data.task });
        this.schedule("registration", () => this.loadRegistrationTask(), 400);
        return "验证码已提交，注册继续运行";
      });
      this.commands.register("focus-registration-proxy", async () => {
        this.router.navigate("network");
        setTimeout(() => {
          $("registrationProxyPanel").scrollIntoView({ behavior: "smooth", block: "center" });
          $("registrationProxyMode").focus({ preventScroll: true });
        }, 0);
        return "已打开独立的代理与线路模块";
      });
      this.commands.register("save-registration-proxy", async () => {
        const current = this.store.state.registrationProxy || {};
        const mode = $("registrationProxyMode").value || "kookeey";
        if (mode === "clash") {
          throw new Error("Clash 模式请使用“检测并切换日本 IP”");
        }
        const endpoint = $("registrationProxyEndpoint").value.trim();
        const username = $("registrationProxyUsername").value.trim();
        const password = $("registrationProxyPassword").value;
        if (!current.configured && (!endpoint || !username || !password)) {
          throw new Error("首次配置请填写用户名、密码和主机:端口");
        }
        const payload = {
          mode,
          country: $("registrationProxyCountry").value || "NL",
          enabled: current.configured ? $("registrationProxyEnabled").checked : true,
        };
        if (endpoint) payload.proxyEndpoint = endpoint;
        if (username) payload.proxyUsername = username;
        if (password) payload.proxyPassword = password;
        const data = await this.api.post("/api/registration-proxy/config", payload);
        $("registrationProxyUsername").value = "";
        $("registrationProxyPassword").value = "";
        $("registrationProxyEndpoint").dataset.dirty = "";
        this.store.patch({ registrationProxy: data });
        return (data.mode === "kookeey" ? "Kookeey 动态住宅" : "动态代理") +
          "已保存并" + (data.enabled ? "启用" : "保持关闭") + "：" +
          (data.countryLabel || data.country);
      });
      this.commands.register("rotate-registration-proxy", async () => {
        await this.api.post("/api/registration-proxy/config", {
          mode: "clash", country: "JP", enabled: true, maxLatencyMs: 900,
        });
        const data = await this.api.post("/api/registration-proxy/rotate", {});
        this.store.patch({ registrationProxy: data });
        return "Clash 日本出口已固定：" + (data.currentNode || "日本节点") +
          "，延迟 " + (data.lastLatencyMs || 0) + " ms";
      });
      this.commands.register("test-registration-proxy", async () => {
        const data = await this.api.post("/api/registration-proxy/test", {});
        this.store.patch({ registrationProxy: data });
        if (!data.testResult?.ok) {
          throw new Error(data.testResult?.message || "代理测试未通过");
        }
        return "代理测试通过：" + data.testResult.message;
      });
      this.commands.register("save-card-link-proxy", async () => {
        const current = this.store.state.cardLinkProxy || {};
        const mode = $("cardLinkRoutingMode").value || "kookeey";
        if (mode === "clash") {
          throw new Error("提链 Clash 模式请使用“检测并切换日本 IP”");
        }
        const endpoint = $("cardLinkRoutingEndpoint").value.trim();
        const username = $("cardLinkRoutingUsername").value.trim();
        const password = $("cardLinkRoutingPassword").value;
        if (!current.configured && (!endpoint || !username || !password)) {
          throw new Error("首次配置提链代理请填写用户名、密码和主机:端口");
        }
        const payload = {
          mode,
          country: $("cardLinkRoutingCountry").value || "DE",
          enabled: true,
        };
        if (endpoint) payload.proxyEndpoint = endpoint;
        if (username) payload.proxyUsername = username;
        if (password) payload.proxyPassword = password;
        const data = await this.api.post("/api/card-link-proxy/config", payload);
        $("cardLinkRoutingUsername").value = "";
        $("cardLinkRoutingPassword").value = "";
        $("cardLinkRoutingEndpoint").dataset.dirty = "";
        this.store.patch({ cardLinkProxy: data });
        return "提链代理已独立保存：" + (data.countryLabel || data.country);
      });
      this.commands.register("rotate-card-link-proxy", async () => {
        await this.api.post("/api/card-link-proxy/config", {
          mode: "clash", country: "JP", enabled: true, maxLatencyMs: 900,
        });
        const data = await this.api.post("/api/card-link-proxy/rotate", {});
        this.store.patch({ cardLinkProxy: data });
        return "提链 Clash 日本出口已固定：" + (data.currentNode || "日本节点") +
          "，延迟 " + (data.lastLatencyMs || 0) + " ms";
      });
      this.commands.register("test-card-link-proxy", async () => {
        const data = await this.api.post("/api/card-link-proxy/test", {});
        this.store.patch({ cardLinkProxy: data });
        if (!data.testResult?.ok) {
          throw new Error(data.testResult?.message || "提链代理测试未通过");
        }
        return "提链代理测试通过：" + data.testResult.message;
      });
      this.commands.register("fetch-all", async () => {
        const options = this.browserOptions();
        const data = await this.api.post("/api/browser/fetch-all", options);
        if (data.task) this.store.patch({ browserTask: data.task });
        this.schedule("browser", () => this.loadBrowserTask(), 500);
        return data.message || (data.started ? "Session 获取任务已启动" : "无需重复获取");
      });
      this.commands.register("stop-task", async () => {
        if (!confirm("停止当前注册或浏览器任务？")) throw Object.assign(new Error(), { name: "AbortError" });
        if (this.store.state.registrationTask.running) {
          const data = await this.api.post("/api/registration/stop");
          this.store.patch({ registrationTask: data.task });
          await this.loadRegistrationTask();
          return data.task.running ? "正在停止一键注册" : (data.task.message || "一键注册已停止");
        } else {
          const data = await this.api.post("/api/browser/stop");
          this.store.patch({ browserTask: data.task });
          await this.loadBrowserTask();
          return data.task.running ? "正在停止浏览器任务" : (data.task.message || "浏览器任务已停止");
        }
      });
      this.commands.register("select-account", async ({ element }) => {
        const email = element.dataset.email;
        this.store.patch({ selectedAccountEmail: this.store.state.selectedAccountEmail === email ? "" : email });
      });
      this.commands.register("copy-email", async ({ element }) => {
        await this.copyText(element.dataset.email);
        return "邮箱已复制";
      });
      this.commands.register("copy-credential", async ({ element }) => {
        const data = await this.api.post("/api/gpt-credential", {
          email: element.dataset.email, kind: element.dataset.kind,
        });
        await this.copyText(data.value);
        return "凭据已复制";
      });
      this.commands.register("enable-2fa", async ({ element }) => {
        const item = this.selectedAccount(element.dataset.email);
        if (!item) throw new Error("账号不存在，请刷新后重试");
        if (item.hasTwoFactor) throw new Error("该账号已经开启 2FA");
        if (!item.hasPassword) throw new Error("请先设置并确认账号密码，再添加 2FA");
        const data = await this.api.post("/api/account/enable-2fa", {
          email: item.email, headless: true,
        });
        this.store.patch({ browserTask: data.task });
        this.schedule("browser", () => this.loadBrowserTask(), 800);
        return "正在使用无头浏览器为 " + item.email + " 添加 2FA";
      });
      this.commands.register("get-code", async ({ element }) => {
        const email = element.dataset.email;
        const since = new Date(Date.now() - 5 * 60_000).toISOString();
        const deadline = Date.now() + 60_000;
        while (Date.now() < deadline) {
          try {
            const data = await this.api.post("/api/gpt-code", { email, since });
            await this.copyText(data.code);
            return "验证码 " + data.code + " 已复制";
          } catch (error) {
            if (error.status !== 404) throw error;
          }
          await new Promise((resolve) => setTimeout(resolve, 1200));
        }
        throw new Error("最近 5 分钟内没有找到新的验证码");
      });
      this.commands.register("import-workbench", async ({ element }) => {
        const data = await this.api.post("/api/account/import-workbench", { email: element.dataset.email });
        const included = [data.hasPassword && "密码", data.hasTwoFactor && "2FA"].filter(Boolean);
        const suffix = included.length ? "（含" + included.join("、") + "）" : "";
        return (data.updated ? "工作台账号已更新" : "已导入工作台 " + (data.group || "")) + suffix;
      });
      this.commands.register("verify-account", async ({ element }) => {
        return this.startAccountVerification(element.dataset.email);
      });
      this.commands.register("copy-account", async ({ element }) => {
        const response = await fetch("/api/gpt-accounts/export", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Local-Token": localToken },
          body: JSON.stringify({ email: element.dataset.email }),
          cache: "no-store",
        });
        if (!response.ok) throw new Error("复制账号失败");
        await this.copyText(await response.text());
        return "账号已复制";
      });
      this.commands.register("delete-email", async ({ element }) => {
        const email = element.dataset.email;
        const isGmail = email.toLowerCase().endsWith("@gmail.com");
        const warning = isGmail
          ? "从本机账号列表删除 " + email + "？这会清除本地账号凭据和 SMSBower 激活记录，但不会删除 Gmail 服务商侧的邮箱。"
          : "永久删除邮箱 " + email + "？该操作无法撤销。";
        if (!confirm(warning)) throw Object.assign(new Error(), { name: "AbortError" });
        let data;
        try {
          data = await this.api.post("/api/gpt-email/delete", { email });
        } catch (error) {
          const canDeleteLocally =
            error.code === "icloud_session_expired" && error.canDeleteLocal;
          if (isGmail || !canDeleteLocally) {
            throw error;
          }
          const localWarning =
            "iCloud 登录已过期，Apple 端邮箱当前无法操作。是否仅从工作台移除本地记录？" +
            "\n\n若 Apple 端仍存在该地址，它将继续转发邮件。";
          if (!confirm(localWarning)) throw Object.assign(new Error(), { name: "AbortError" });
          data = await this.api.post("/api/gpt-email/delete", { email, local_only: true });
        }
        await this.loadAccounts();
        return data.message || "邮箱已删除";
      });
      this.commands.register("select-card-account", async ({ element }) => {
        this.store.patch({ selectedCardEmail: element.dataset.email });
      });
      this.commands.register("generate-card-link", async () => {
        const item = this.selectedAccount(this.store.state.selectedCardEmail);
        if (!item) throw new Error("请先选择账号");
        const method = $("cardLinkMethod").value;
        const config = cardLinkRuntimeConfig(method);
        const data = await this.api.post("/api/account/card-link", {
          email: item.email, method, country: config.country,
          proxy_mode: $("cardLinkProxyMode").value,
          create_proxy_country: $("cardLinkCreateProxyCountry").value,
          promotion_proxy_country: config.singleProxy ? "" : $("cardLinkPromotionProxyCountry").value,
          force_retry: cardLinkMarkedForMethod(item, method),
          ...cardLinkPaymentPayload(method),
        });
        if (data.cardLinkStatus !== "cs_live") await this.copyText(data.url);
        await this.loadAccounts();
        setTimeout(() => this.renderer.renderCardSelection(this.store.state), 0);
        return data.cardLinkStatus === "cs_live"
          ? "本次仍返回 cs_live，可再次点击重新提链"
          : config.success;
      });
      this.commands.register("generate-all-card-links", async ({ element }) => {
        const method = $("cardLinkMethod").value;
        const config = cardLinkRuntimeConfig(method);
        const candidates = this.store.state.accounts.filter((item) =>
          cardLinkEligible(item, method)
        );
        if (!candidates.length) throw new Error("当前模式没有待提链账号");
        const batchState = $("cardLinkBatchState");
        const lockedControls = [
          "cardLinkMethod", "cardLinkProxyMode", "cardLinkCreateProxyCountry",
          "cardLinkPromotionProxyCountry", "cardLinkTargetAmount",
          "generateCardLinkButton",
        ].map($);
        element.dataset.running = "1";
        lockedControls.forEach((control) => { control.disabled = true; });
        let generated = 0;
        let classified = 0;
        let failed = 0;
        batchState.className = "operation-result";
        try {
          for (let index = 0; index < candidates.length; index += 1) {
            const item = candidates[index];
            element.textContent = "正在提链 " + (index + 1) + " / " + candidates.length;
            batchState.innerHTML = '<strong>一键提链进行中</strong><span>' +
              escapeHtml(item.email) + " · " + (index + 1) + " / " + candidates.length +
              "</span><span>已生成 " + generated + " · cs_live " + classified +
              " · 失败 " + failed + "</span>";
            try {
              const data = await this.api.post("/api/account/card-link", {
                email: item.email,
                method,
                country: config.country,
                proxy_mode: $("cardLinkProxyMode").value,
                create_proxy_country: $("cardLinkCreateProxyCountry").value,
                promotion_proxy_country: config.singleProxy
                  ? "" : $("cardLinkPromotionProxyCountry").value,
                force_retry: cardLinkMarkedForMethod(item, method),
                ...cardLinkPaymentPayload(method),
              });
              if (data.cardLinkStatus === "cs_live") classified += 1;
              else generated += 1;
            } catch (_error) {
              failed += 1;
            }
          }
        } finally {
          delete element.dataset.running;
          lockedControls.forEach((control) => { control.disabled = false; });
          await this.loadAccounts();
          setTimeout(() => this.renderer.renderCardSelection(this.store.state), 0);
        }
        batchState.innerHTML = '<strong>一键提链完成</strong><span>已生成 ' + generated +
          " · cs_live 已标注 " + classified + " · 失败 " + failed +
          "</span><span>cs_live 账号只会在“" + escapeHtml(config.label) +
          "”模式下自动跳过</span>";
        return "一键提链完成：生成 " + generated + "，cs_live " + classified +
          "，失败 " + failed;
      });
      this.commands.register("copy-card-link", async () => {
        const item = this.selectedAccount(this.store.state.selectedCardEmail);
        if (!item?.cardLink) throw new Error("当前账号尚未生成链接");
        await this.copyText(item.cardLink);
        return "支付链接已复制";
      });
      this.commands.register("open-card-link", async () => {
        const item = this.selectedAccount(this.store.state.selectedCardEmail);
        if (!item?.cardLink) throw new Error("当前账号尚未生成链接");
        window.open(item.cardLink, "_blank", "noopener,noreferrer");
        return "支付页已打开";
      });
      this.commands.register("verify-all", async () => {
        const emails = this.store.state.accounts.map((item) => item.email);
        if (!emails.length) throw new Error("暂无可验证账号");
        const concurrency = this.verificationConcurrency();
        if (!confirm("验证 " + emails.length + " 个账号，并发 " + concurrency + "，是否继续？")) {
          throw Object.assign(new Error(), { name: "AbortError" });
        }
        const data = await this.api.post("/api/account-verification/start", { concurrency, emails });
        this.store.patch({ verificationTask: data.task });
        this.schedule("verification", () => this.loadVerificationTask(), 800);
        return "账号验证已启动";
      });
      this.commands.register("verify-selected", async () => {
        return this.startAccountVerification(this.store.state.selectedVerificationEmail);
      });
      this.commands.register("previous-verification", async () => {
        return this.moveVerificationSelection(-1);
      });
      this.commands.register("next-verification", async () => {
        return this.moveVerificationSelection(1);
      });
      this.commands.register("stop-verification", async () => {
        await this.api.post("/api/account-verification/stop");
        await this.loadVerificationTask();
        return "验证停止请求已发送";
      });
      this.commands.register("select-verification", async ({ element }) => {
        this.store.patch({ selectedVerificationEmail: element.dataset.email });
      });
      this.commands.register("save-imap", async () => {
        const payload = {
          host: $("imapHost").value.trim(), port: Number($("imapPort").value),
          username: $("imapUsername").value.trim(), password: $("imapPassword").value,
          folder: "INBOX", useSsl: true,
        };
        await this.api.post("/api/inbox/config", payload);
        await this.loadInbox();
        return "IMAP 配置已保存并验证";
      });
      this.commands.register("sync-inbox", async () => {
        const data = await this.api.post("/api/inbox/sync", { limit: 100 });
        await this.loadInbox();
        return "邮箱同步完成，新增 " + (data.inserted || 0) + " 封邮件";
      });
      this.commands.register("save-browser-settings", async () => {
        const mode = document.querySelector('input[name="settingsRegistrationMode"]:checked')?.value || "headed";
        localStorage.setItem("hme_registration_mode", mode);
        $("concurrency").value = $("settingsConcurrency").value;
        this.store.patch({ registrationMode: mode });
        return "账号注册方式已更新";
      });
      window.HmeSmsSettings.register(this);
      window.HmeLiandongShop.register(this);
    }
    bindEvents() {
      document.addEventListener("click", (event) => {
        const route = event.target.closest("[data-route]");
        if (route) {
          event.preventDefault();
          this.router.navigate(route.dataset.route);
          return;
        }
        const settings = event.target.closest("[data-settings-section]");
        if (settings) {
          this.store.patch({ settingsSection: settings.dataset.settingsSection });
          this.renderer.renderSettings(this.store.state);
          return;
        }
        const controlTaskFilter = event.target.closest("[data-control-task-filter]");
        if (controlTaskFilter) {
          document.querySelectorAll("[data-control-task-filter]").forEach((button) => {
            const active = button === controlTaskFilter;
            button.classList.toggle("active", active);
            button.setAttribute("aria-selected", String(active));
          });
          this.renderer.controlTaskFilter = controlTaskFilter.dataset.controlTaskFilter || "all";
          this.renderer.renderControlCenter(this.store.state);
          return;
        }
        const verifyFilter = event.target.closest("[data-verify-filter]");
        if (verifyFilter) {
          document.querySelectorAll("[data-verify-filter]").forEach((button) => button.classList.remove("active"));
          verifyFilter.classList.add("active");
          this.store.patch({ verificationFilter: verifyFilter.dataset.verifyFilter });
          return;
        }
        const command = event.target.closest("[data-action]");
        if (command) this.commands.execute(command.dataset.action, { element: command, event });
      });
      ["accountSearch", "accountPlanFilter", "accountSessionFilter", "accountLiandongFilter"].forEach((id) => {
        $(id).addEventListener(id === "accountSearch" ? "input" : "change", () => this.renderer.renderAccounts(this.store.state));
      });
      $("controlTaskSearch").addEventListener("input", () =>
        this.renderer.renderControlCenter(this.store.state));
      $("terminalSessionSelect").addEventListener("change", (event) =>
        this.terminalLogPresenter.select(event.target.value));
      $("workspaceAutoRefresh").addEventListener("change", (event) => {
        localStorage.setItem("hme_workspace_auto_refresh", event.target.checked ? "1" : "0");
        this.scheduleWorkspaceRefresh();
      });
      $("workspaceRefreshInterval").addEventListener("change", (event) => {
        const value = ["5000", "10000", "30000"].includes(event.target.value)
          ? event.target.value : "5000";
        event.target.value = value;
        localStorage.setItem("hme_workspace_refresh_interval", value);
        this.scheduleWorkspaceRefresh();
      });
      ["cardSearch", "cardStatusFilter"].forEach((id) => {
        $(id).addEventListener(id === "cardSearch" ? "input" : "change", () => this.renderer.renderCardLinks(this.store.state));
      });
      $("cardLinkMethod").addEventListener("change", () => {
        this.renderer.renderCardLinks(this.store.state);
      });
      $("cardLinkTargetAmount").addEventListener("change", (event) => {
        const value = event.target.value.trim();
        if (value && !/^\d+$/.test(value)) {
          event.target.value = "";
          this.toast("目标金额必须是非负整数，留空表示不校验", "error");
          return;
        }
        localStorage.setItem("hme_card_link_target_amount", value);
        this.renderer.renderCardSelection(this.store.state);
      });
      $("cardLinkProxyMode").addEventListener("change", async (event) => {
        const method = $("cardLinkMethod").value;
        try {
          const data = await this.api.post("/api/card-link-proxy/config", {
            cardLinkModes: { [method]: event.target.value },
          });
          this.store.patch({ cardLinkProxy: data });
          this.toast("提链代理模式已保存为 " + event.target.selectedOptions[0].textContent);
        } catch (error) {
          await this.loadCardLinkProxy();
          this.toast(error.message, "error");
        }
      });
      ["cardLinkCreateProxyCountry", "cardLinkPromotionProxyCountry"].forEach((id) => {
        $(id).addEventListener("change", async (event) => {
          const preferenceKey = event.target.dataset.preferenceKey;
          if (!preferenceKey || !event.target.value) return;
          try {
            const data = await this.api.post("/api/card-link-proxy/config", {
              cardLinkCountries: { [preferenceKey]: event.target.value },
            });
            this.store.patch({ cardLinkProxy: data });
            this.toast("提链代理国家已保存为 " + event.target.selectedOptions[0].textContent);
          } catch (error) {
            await this.loadCardLinkProxy();
            this.toast(error.message, "error");
          }
        });
      });
      $("quickRegistrationMode").addEventListener("change", (event) => {
        localStorage.setItem("hme_quick_registration_mode", event.target.value);
        this.renderer.renderQuickFlow(this.store.state);
      });
      $("quickProtocolSetupCredentials").addEventListener("change", () => this.renderer.renderQuickFlow(this.store.state));
      $("quickRegistrationProvider").addEventListener("change", (event) => {
        localStorage.setItem("hme_quick_registration_provider", event.target.value);
        this.renderer.renderQuickFlow(this.store.state);
        this.toast(event.target.value === "zkgmail"
          ? "一键流水线邮箱来源已切换为 " + (this.store.state.zkgmail?.domain || "cclgmail.com")
          : "一键流水线邮箱来源已切换为 iCloud 库存");
      });
      $("quickRegistrationProxyMode").addEventListener("change", async (event) => {
        const mode = event.target.value;
        localStorage.setItem("hme_quick_registration_proxy_mode", mode);
        try {
          if (mode === "direct") {
            const data = await this.api.post("/api/registration-proxy/config", { enabled: false });
            this.store.patch({ registrationProxy: data });
            this.toast("一键注册已选择本机 IP 直连");
            return;
          }
          const candidate = this.store.state.registrationProxy?.modes?.find(
            (item) => item.code === mode
          );
          const country = mode === "clash" ? "JP" : ($("quickRegistrationProxyCountry").value || "NL");
          const data = await this.api.post("/api/registration-proxy/config", {
            mode,
            country,
            enabled: Boolean(candidate?.configured),
          });
          this.store.patch({ registrationProxy: data });
          this.toast(candidate?.configured
            ? "一键注册代理已切换为 " + event.target.selectedOptions[0].textContent
            : "已选择该注册代理模式；请先在注册代理独立配置中保存凭据");
        } catch (error) {
          await this.loadRegistrationProxy();
          this.toast(error.message, "error");
        }
      });
      $("quickRegistrationProxyCountry").addEventListener("change", async (event) => {
        const mode = $("quickRegistrationProxyMode").value;
        if (mode === "direct") return;
        const candidate = this.store.state.registrationProxy?.modes?.find(
          (item) => item.code === mode
        );
        try {
          const data = await this.api.post("/api/registration-proxy/config", {
            mode,
            country: event.target.value,
            enabled: Boolean(candidate?.configured),
          });
          this.store.patch({ registrationProxy: data });
          this.toast("一键注册代理国家已切换为 " + event.target.selectedOptions[0].textContent);
        } catch (error) {
          await this.loadRegistrationProxy();
          this.toast(error.message, "error");
        }
      });
      $("quickRegistrationConcurrency").addEventListener("change", (event) => {
        const limit = ["protocol", "roxy"].includes($("quickRegistrationMode").value) ? 5 : 10;
        const value = Math.max(1, Math.min(limit, Number(event.target.value) || 1));
        event.target.value = String(value);
        localStorage.setItem("hme_quick_registration_concurrency", String(value));
        this.renderer.renderQuickFlow(this.store.state);
      });
      $("quickRegistrationTargetCount").addEventListener("change", (event) => {
        const value = Math.max(1, Math.min(100, Number(event.target.value) || 1));
        event.target.value = String(value);
        localStorage.setItem("hme_quick_registration_target", String(value));
        this.renderer.renderQuickFlow(this.store.state);
      });
      $("quickExtractionCount").addEventListener("change", (event) => {
        const value = Math.max(1, Math.min(100, Number(event.target.value) || 1));
        event.target.value = String(value);
        localStorage.setItem("hme_quick_extraction_count", String(value));
        this.renderer.renderQuickFlow(this.store.state);
      });
      $("quickCardLinkMethod").addEventListener("change", (event) => {
        localStorage.setItem("hme_quick_card_link_method", event.target.value);
        this.renderer.renderQuickFlow(this.store.state);
      });
      $("quickCardLinkTargetAmount").addEventListener("change", (event) => {
        const value = event.target.value.trim();
        if (value && !/^\d+$/.test(value)) {
          event.target.value = "";
          this.toast("目标金额必须是非负整数，留空表示不校验", "error");
          return;
        }
        localStorage.setItem("hme_quick_paypal_us_target_amount", value);
        this.renderer.renderQuickFlow(this.store.state);
      });
      $("quickExtractionProxyMode").addEventListener("change", async (event) => {
        localStorage.setItem("hme_quick_extraction_proxy_mode", event.target.value);
        try {
          const method = $("quickCardLinkMethod").value;
          const data = await this.api.post("/api/card-link-proxy/config", {
            cardLinkModes: { [method]: event.target.value },
          });
          this.store.patch({ cardLinkProxy: data });
          const candidate = data.modes?.find((item) => item.code === event.target.value);
          this.toast(candidate?.configured
            ? "提链代理已选择并用于 Checkout 探测"
            : "已选择该提链代理；请先在提链代理独立配置中保存凭据");
        } catch (error) {
          await this.loadCardLinkProxy();
          this.toast(error.message, "error");
        }
      });
      $("quickExtractionFirstProxyCountry").addEventListener("change", async (event) => {
        localStorage.setItem("hme_quick_extraction_first_country", event.target.value);
        try {
          const config = cardLinkRuntimeConfig($("quickCardLinkMethod").value);
          const data = await this.api.post("/api/card-link-proxy/config", {
            cardLinkCountries: { [config.createProxyPreference]: event.target.value },
          });
          this.store.patch({ cardLinkProxy: data });
          this.toast("第一代理出口已保存，并用于 Checkout 探测");
        } catch (error) {
          await this.loadCardLinkProxy();
          this.toast(error.message, "error");
        }
      });
      $("quickExtractionSecondProxyCountry").addEventListener("change", async (event) => {
        localStorage.setItem("hme_quick_extraction_second_country", event.target.value);
        const config = cardLinkRuntimeConfig($("quickCardLinkMethod").value);
        if (!config.promotionProxyPreference) {
          this.renderer.renderQuickFlow(this.store.state);
          this.toast("第二代理出口已选择 " + event.target.selectedOptions[0].textContent);
          return;
        }
        try {
          const data = await this.api.post("/api/card-link-proxy/config", {
            cardLinkCountries: { [config.promotionProxyPreference]: event.target.value },
          });
          this.store.patch({ cardLinkProxy: data });
          this.toast("第二代理出口已保存为 " + event.target.selectedOptions[0].textContent);
        } catch (error) {
          await this.loadCardLinkProxy();
          this.toast(error.message, "error");
        }
      });
      $("quickPromotionProxyChoice").addEventListener("change", (event) => {
        localStorage.setItem("hme_quick_promotion_proxy_choice", event.target.value);
        this.renderer.renderQuickFlow(this.store.state);
        this.toast("优惠更新已选择使用" + (event.target.value === "second" ? "第二 IP" : "第一 IP"));
      });
      $("verificationAccountSelect").addEventListener("change", (event) => {
        this.store.patch({ selectedVerificationEmail: event.target.value });
      });
      document.querySelectorAll('input[name="registrationMode"]').forEach((input) => {
        input.addEventListener("change", (event) => {
          if (!event.target.checked) return;
          const mode = event.target.value;
          localStorage.setItem("hme_registration_mode", mode);
          this.store.patch({ registrationMode: mode });
          if (mode === "roxy" && !this.store.state.roxyRegistration?.configured) {
            setTimeout(() => $("roxyProfile").focus(), 0);
          }
          this.toast(mode === "protocol"
            ? "已切换为协议注册，可选择 iCloud 或 QQ 转发自有域名邮箱"
            : mode === "roxy" ? "已切换为 Roxy 随机指纹注册"
            : mode === "headless" ? "已切换为无头浏览器注册" : "已切换为有头浏览器注册");
        });
      });
      $("roxyWorkspace").addEventListener("change", async (event) => {
        try {
          const data = await this.api.post("/api/roxy-registration/config", {
            workspaceId: event.target.value,
            profileId: "",
          });
          this.store.patch({ roxyRegistration: data });
          this.toast("Roxy 工作区已更新，请选择专用指纹环境");
        } catch (error) {
          await this.loadRoxyRegistration();
          this.toast(error.message, "error");
        }
      });
      $("roxyProfile").addEventListener("change", async (event) => {
        try {
          const data = await this.api.post("/api/roxy-registration/config", {
            workspaceId: $("roxyWorkspace").value,
            profileId: event.target.value,
          });
          this.store.patch({ roxyRegistration: data });
          this.toast(data.configured ? "Roxy 专用指纹环境已保存" : "请选择有效的 Roxy 环境");
        } catch (error) {
          await this.loadRoxyRegistration();
          this.toast(error.message, "error");
        }
      });
      $("roxyWindowMode").addEventListener("change", (event) => {
        localStorage.setItem("hme_roxy_window_mode", event.target.value);
        this.renderer.renderAccounts(this.store.state);
        this.toast(event.target.value === "background"
          ? "Roxy 将使用后台隐藏窗口运行"
          : "Roxy 将显示有头窗口");
      });
      $("roxyConcurrency").addEventListener("change", (event) => {
        const value = Math.max(1, Math.min(5, Number(event.target.value) || 1));
        event.target.value = String(value);
        localStorage.setItem("hme_roxy_concurrency", String(value));
        this.renderer.renderAccounts(this.store.state);
        this.toast("Roxy 并发窗口已设置为 " + value);
      });
      $("roxyTargetCount").addEventListener("change", (event) => {
        const value = Math.max(1, Math.min(100, Number(event.target.value) || 1));
        event.target.value = String(value);
        localStorage.setItem("hme_roxy_target_count", String(value));
        this.renderer.renderAccounts(this.store.state);
        this.toast("Roxy 目标账号数已设置为 " + value);
      });
      $("registrationEmailProvider").addEventListener("change", () => {
        this.renderer.renderAccounts(this.store.state);
      });
      $("cardLinkRoutingEndpoint").addEventListener("input", (event) => {
        event.target.dataset.dirty = "1";
      });
      $("cardLinkRoutingMode").addEventListener("change", (event) => {
        const mode = event.target.value;
        const clashMode = mode === "clash";
        $("cardLinkRoutingCredentialFields").hidden = clashMode;
        $("rotateCardLinkRoutingButton").hidden = !clashMode;
        $("cardLinkRoutingCountry").disabled = clashMode;
        if (clashMode) $("cardLinkRoutingCountry").value = "JP";
        ["cardLinkRoutingUsername", "cardLinkRoutingPassword", "cardLinkRoutingEndpoint", "saveCardLinkRoutingButton"].forEach((id) => {
          $(id).disabled = clashMode;
        });
        this.toast("提链代理模式已选择；保存后只用于提链，不影响注册代理");
      });
      $("cardLinkRoutingCountry").addEventListener("change", async (event) => {
        try {
          const data = await this.api.post("/api/card-link-proxy/config", {
            country: event.target.value,
          });
          this.store.patch({ cardLinkProxy: data });
          this.toast("提链代理测试出口已切换为 " + event.target.selectedOptions[0].textContent);
        } catch (error) {
          await this.loadCardLinkProxy();
          this.toast(error.message, "error");
        }
      });
      $("registrationProxyEndpoint").addEventListener("input", (event) => {
        event.target.dataset.dirty = "1";
      });
      $("registrationProxyEnabled").addEventListener("change", async (event) => {
        try {
          const data = await this.api.post("/api/registration-proxy/config", {
            enabled: event.target.checked,
          });
          this.store.patch({ registrationProxy: data });
          this.toast(data.enabled ? "所有注册方式已启用代理" : "代理已关闭，所有注册方式将使用本机 IP 直连");
        } catch (error) {
          event.target.checked = !event.target.checked;
          this.toast(error.message, "error");
        }
      });
      $("registrationProxyMode").addEventListener("change", (event) => {
        const mode = event.target.value;
        const clashMode = mode === "clash";
        $("registrationProxyCredentialFields").hidden = clashMode;
        $("rotateRegistrationProxyButton").hidden = !clashMode;
        $("registrationProxyCountry").value = clashMode
          ? "JP"
          : (this.store.state.registrationProxy?.country || "NL");
        $("registrationProxyCountrySearch").disabled = clashMode;
        if (clashMode) $("registrationProxyCountrySearch").value = "日本 (JP)";
        ["registrationProxyUsername", "registrationProxyPassword", "registrationProxyEndpoint", "saveRegistrationProxyButton"].forEach((id) => {
          $(id).disabled = clashMode;
        });
        $("registrationProxyModeHint").textContent = clashMode
          ? "Clash 模式固定为日本出口；点击下方按钮检测并切换节点"
          : mode === "kookeey"
            ? "Kookeey 模式会自动把国家、8 位 Session 和 5m 写入连接密码"
            : "通用模式会自动把 region、8 位 SID 和 5 分钟粘性时长写入用户名";
        this.toast("代理模式已选择，请点击保存后用于注册");
      });
      $("registrationProxyCountrySearch").addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          event.target.blur();
        }
      });
      const commitProxyCountry = async (event, { reportInvalid = false } = {}) => {
        const countries = this.store.state.registrationProxy?.countries || [];
        const match = matchProxyCountry(event.target.value, countries);
        if (!match) {
          if (reportInvalid) {
            this.renderer.renderNetwork(this.store.state);
            this.toast("未找到唯一国家，请输入中文国家名或两位代码", "error");
          }
          return false;
        }
        if (event.target.dataset.countryCode === match.code) return true;
        event.target.dataset.countryCode = match.code;
        $("registrationProxyCountry").value = match.code;
        try {
          const data = await this.api.post("/api/registration-proxy/config", {
            country: match.code,
          });
          this.store.patch({ registrationProxy: data });
          this.toast("注册出口已切换为 " + countryOptionLabel(match));
          return true;
        } catch (error) {
          delete event.target.dataset.countryCode;
          this.renderer.renderNetwork(this.store.state);
          this.toast(error.message, "error");
          return false;
        }
      };
      $("registrationProxyCountrySearch").addEventListener("input", (event) => {
        const value = event.target.value.trim();
        if (!value || (/^[a-z]+$/i.test(value) && value.length < 2)) return;
        void commitProxyCountry(event);
      });
      $("registrationProxyCountrySearch").addEventListener("change", async (event) => {
        await commitProxyCountry(event, { reportInvalid: true });
      });
      $("smsbowerMaxPrice").addEventListener("change", async (event) => {
        try {
          const data = await this.api.post("/api/smsbower/config", {
            maxPrice: Number(event.target.value),
          });
          this.store.patch({ smsBower: data });
          this.toast("SMSBower Gmail 最高价已更新为 $" + data.maxPrice);
        } catch (error) {
          await this.loadSmsBower();
          this.toast(error.message, "error");
        }
      });
    }

    async start() {
      const savedTheme = localStorage.getItem("hme_theme") || "dark";
      const savedAutoRefresh = localStorage.getItem("hme_workspace_auto_refresh");
      $("workspaceAutoRefresh").checked = savedAutoRefresh !== "0";
      const savedRefreshInterval = localStorage.getItem("hme_workspace_refresh_interval") || "5000";
      $("workspaceRefreshInterval").value = ["5000", "10000", "30000"].includes(savedRefreshInterval)
        ? savedRefreshInterval : "5000";
      this.setTerminalPreviewCollapsed(
        localStorage.getItem("hme_terminal_preview_collapsed") === "1",
        false
      );
      this.updateWorkspaceClock();
      clearInterval(this.clockTimer);
      this.clockTimer = setInterval(() => this.updateWorkspaceClock(), 1000);
      const savedRegistrationMode = localStorage.getItem("hme_registration_mode");
      const registrationMode = ["headless", "headed", "roxy", "protocol"].includes(savedRegistrationMode)
        ? savedRegistrationMode : "headed";
      $("protocolSetupCredentials").checked = true;
      $("protocolSetupCredentials").disabled = true;
      const savedRoxyWindowMode = localStorage.getItem("hme_roxy_window_mode");
      $("roxyWindowMode").value = savedRoxyWindowMode === "headed" ? "headed" : "background";
      const savedRoxyConcurrency = Number(localStorage.getItem("hme_roxy_concurrency") || 5);
      $("roxyConcurrency").value = String(
        Number.isInteger(savedRoxyConcurrency) && savedRoxyConcurrency >= 1 && savedRoxyConcurrency <= 5
          ? savedRoxyConcurrency : 5
      );
      const savedRoxyTargetCount = Number(localStorage.getItem("hme_roxy_target_count") || 5);
      $("roxyTargetCount").value = String(
        Number.isInteger(savedRoxyTargetCount) && savedRoxyTargetCount >= 1 && savedRoxyTargetCount <= 100
          ? savedRoxyTargetCount : 5
      );
      const savedQuickMode = localStorage.getItem("hme_quick_registration_mode");
      $("cardLinkTargetAmount").value =
        localStorage.getItem("hme_card_link_target_amount") || "";
      $("quickRegistrationMode").value = ["headless", "headed", "roxy", "protocol"].includes(savedQuickMode)
        ? savedQuickMode : "headless";
      const savedQuickConcurrency = Number(localStorage.getItem("hme_quick_registration_concurrency") || 1);
      $("quickRegistrationConcurrency").value = String(
        Number.isInteger(savedQuickConcurrency) && savedQuickConcurrency >= 1 && savedQuickConcurrency <= 10
          ? savedQuickConcurrency : 1
      );
      const savedQuickTarget = Number(localStorage.getItem("hme_quick_registration_target") || 1);
      $("quickRegistrationTargetCount").value = String(
        Number.isInteger(savedQuickTarget) && savedQuickTarget >= 1 && savedQuickTarget <= 100
          ? savedQuickTarget : 1
      );
      const savedQuickExtractionCount = Number(localStorage.getItem("hme_quick_extraction_count") || 1);
      $("quickExtractionCount").value = String(
        Number.isInteger(savedQuickExtractionCount) && savedQuickExtractionCount >= 1 &&
          savedQuickExtractionCount <= 100
          ? savedQuickExtractionCount : 1
      );
      const savedQuickCardLinkMethod = localStorage.getItem("hme_quick_card_link_method");
      $("quickCardLinkMethod").value = ["de_oaics_paypal", "paypal_us", "paypal_gb"].includes(savedQuickCardLinkMethod)
        ? savedQuickCardLinkMethod : "de_oaics_paypal";
      this.quickFlowConfigPresenter.restore();
      this.applyTheme(savedTheme, false);
      this.sidebarPresenter.restore();
      this.registrationConfigPresenter.restore();
      this.store.patch({ registrationMode });
      this.store.subscribe((state) => this.render(state));
      this.bindEvents();
      this.quickFlowConfigPresenter.bind();
      this.router.start();
      const results = await this.refreshWorkspaceData();
      const failure = results.find((result) => result.status === "rejected");
      if (failure) this.toast(failure.reason.message, "error");
      this.scheduleWorkspaceRefresh();
    }
  }

  new WorkspaceController().start();
})();
