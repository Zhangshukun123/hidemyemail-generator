(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const localToken = window.__HME_LOCAL_TOKEN__;
  const pageDetails = {
    overview: ["DASHBOARD", "概览", "集中查看账号、任务与服务状态"],
    accounts: ["ACCOUNT WORKSPACE", "邮箱账号", "管理 iCloud 邮箱、OpenAI Session 与账号凭据"],
    "protocol-registration": ["MAIL AUTH WORKSPACE", "协议注册", "通过 Mail Auth 完成 Session、密码与 TOTP 2FA"],
    "card-links": ["CHECKOUT WORKSPACE", "直卡提链接", "使用 PH / PHP hosted 双代理流程提取严格 0 链接"],
    "pp-payment": ["PAYPAL WORKSPACE", "PP 支付", "PayPal BA 协议授权与支付任务"],
    verification: ["VERIFICATION WORKSPACE", "验证记录", "批量验证账号、套餐与 Session 状态"],
    settings: ["SYSTEM SETTINGS", "系统设置", "管理邮箱、浏览器、集成与安全配置"],
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
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

  class ApiGateway {
    constructor(token) { this.token = token; }

    async request(path, options = {}) {
      const headers = { ...(options.headers || {}) };
      if (options.method && options.method !== "GET") headers["X-Local-Token"] = this.token;
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
      if (response.status === 403) {
        setTimeout(() => location.reload(), 120);
        throw new Error(data.error || "服务已更新，正在刷新页面");
      }
      if (!response.ok || data.ok === false) {
        const error = new Error(data.error || "请求失败 (" + response.status + ")");
        error.status = response.status;
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
      document.querySelectorAll(".nav-item[data-route]").forEach((button) => {
        button.classList.toggle("active", button.dataset.route === target);
      });
      const details = pageDetails[target];
      $("viewEyebrow").textContent = details[0];
      $("viewTitle").textContent = details[1];
      $("viewSubtitle").textContent = details[2];
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

  class WorkspaceRenderer {
    constructor(store) { this.store = store; }

    renderShell(state) {
      $("accountNavCount").textContent = state.accounts.length || "—";
      const links = state.accounts.filter((item) => item.cardLink).length;
      $("cardLinkNavCount").textContent = links || "—";
      $("paypalNavState").textContent = state.paypal?.running ? "在线" : "—";
      const protocol = state.protocolRegistrationTask || {};
      $("protocolNavState").textContent = protocol.running
        ? "运行中"
        : state.accounts.filter((item) => item.protocolReady).length || "—";
      const runtime = state.browserTask.runtime || {};
      const available = Boolean(runtime.available);
      $("runtimeDot").className = available ? "ok" : "bad";
      $("sidebarRuntimeDot").className = available ? "" : "bad";
      $("runtimeLabel").textContent = available ? "运行环境已连接" : "运行环境不可用";
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
    }

    filteredAccounts(state) {
      const query = $("accountSearch")?.value.trim().toLowerCase() || "";
      const plan = $("accountPlanFilter")?.value || "all";
      const session = $("accountSessionFilter")?.value || "all";
      return state.accounts.filter((item) =>
        (!query || item.email.toLowerCase().includes(query)) &&
        (plan === "all" || item.accountType === plan) &&
        (session === "all" || item.sessionStatus === session)
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
      const registrationProxy = state.registrationProxy || {};
      const clashProxyMode = registrationProxy.mode === "clash";
      $("registrationProxyEnabled").checked = Boolean(registrationProxy.enabled);
      $("registrationProxyEnabled").disabled = !registrationProxy.configured;
      $("registrationProxyEnabled").title = registrationProxy.configured
        ? (clashProxyMode
          ? "每个账号开始前轮询新的日本节点，任务期间固定出口"
          : "开启时使用所选国家的粘性动态代理；关闭时使用本机公网 IP 直连")
        : "请先设置代理连接";
      const networkMode = $("registrationNetworkMode");
      networkMode.className = "badge " + (registrationProxy.enabled ? "success" : "blue");
      networkMode.textContent = registrationProxy.enabled && clashProxyMode
        ? "Clash 日本轮询" + (registrationProxy.currentNode
          ? " · " + registrationProxy.currentNode + " · " + (registrationProxy.lastLatencyMs || 0) + "ms"
          : " · ≤" + (registrationProxy.maxLatencyMs || 900) + "ms")
        : (registrationProxy.enabled
          ? "动态代理 · " + (registrationProxy.countryLabel || registrationProxy.country || "")
          : "本机 IP 直连 · 语言随出口");
      $("registrationProxyMode").value = registrationProxy.mode || "dynamic";
      $("registrationProxyCountry").disabled = clashProxyMode;
      $("registrationProxySetupButton").textContent = clashProxyMode ? "检测并切换日本 IP" : "代理连接";
      if (registrationProxy.country) {
        $("registrationProxyCountry").value = registrationProxy.country;
      }
      const smsBower = state.smsBower || {};
      const smsBowerStatus = $("smsbowerStatus");
      smsBowerStatus.className = "badge " + (smsBower.configured ? "success" : "warning");
      smsBowerStatus.textContent = smsBower.configured
        ? "SMSBower Gmail · 本机取码保留 " + (smsBower.retentionHours || 24) + " 小时"
        : "SMSBower 未配置";
      if (smsBower.maxPrice) $("smsbowerMaxPrice").value = smsBower.maxPrice;
      const registration = state.registrationTask || {};
      const canStartNextRegistration = registration.canStartNext !== false;
      const activeRegistrationProcesses = Number(registration.runningCount || 0);
      const registrationProvider = $("registrationEmailProvider").value || "icloud";
      $("smsbowerControls").hidden = registrationProvider !== "gmail";
      $("registerProviderButton").textContent = registrationProvider === "gmail"
        ? (activeRegistrationProcesses ? "启动下一个 Gmail 注册进程" : "开始 Gmail 注册")
        : (activeRegistrationProcesses ? "启动下一个 iCloud 注册进程" : "开始 iCloud 注册");
      $("registerProviderButton").disabled = !canStartNextRegistration ||
        (registrationProvider === "gmail" && !smsBower.configured);
      $("registerEmailButton").disabled = !canStartNextRegistration;
      $("registerEmailButton").textContent = activeRegistrationProcesses
        ? `启动下一个注册进程（运行中 ${activeRegistrationProcesses}）`
        : "添加邮箱并注册";
      const task = state.browserTask;
      const hasRegistration = Boolean(registration.id && registration.status !== "idle");
      const primaryTask = hasRegistration ? registration : task;
      const status = primaryTask.status || "idle";
      const statusMeta = taskStatusMeta(status);
      const taskTotal = Number(task.total || (hasRegistration
        ? registration.effectiveConcurrency || registration.claimed || registration.requested || 1
        : 0));
      const taskCompleted = Number(task.total
        ? task.completed || 0
        : (hasRegistration && !registration.running ? 1 : 0));
      const taskSucceeded = Number(task.succeeded || (registration.status === "completed" ? 1 : 0));
      let progress = taskTotal ? Math.round(taskCompleted / taskTotal * 100) : 0;
      if (hasRegistration && registration.running && !task.total) {
        progress = {
          generating_email: 12,
          purchasing_gmail: 12,
          preparing_email: 18,
          claiming_inventory: 12,
          confirming_email: 28,
          registering_openai: 40,
          awaiting_verification_code: 65,
          cancelling: 40,
        }[registration.phase] || progress;
      }
      if (registration.status === "completed") progress = 100;

      $("taskPanel").dataset.taskTone = statusMeta[1];
      $("taskStatusIcon").textContent = statusMeta[2];
      $("taskStateBadge").textContent = statusMeta[0];
      $("registrationSummary").textContent = hasRegistration
        ? "邮箱注册 · " + this.registrationLabel(registration)
        : (task.status && task.status !== "idle"
          ? "浏览器任务 · " + statusMeta[0]
          : "邮箱注册 · 等待开始");
      const latestTaskLog = (task.logs || []).at(-1);
      $("taskMessage").textContent = hasRegistration
        ? (registration.message || "正在处理注册任务")
        : (latestTaskLog?.message || "尚未启动注册或 Session 获取任务");
      $("browserTaskSummary").textContent = taskCompleted + " / " + taskTotal;
      $("browserTaskSuccess").textContent = taskSucceeded;
      $("taskElapsed").textContent = formatElapsed(
        hasRegistration ? registration.startedAt : task.startedAt,
        primaryTask.running ? "" : primaryTask.finishedAt
      );
      $("stopTaskButton").disabled = !(task.running || registration.running);
      $("browserTaskProgress").value = progress;
      $("browserTaskProgress").setAttribute("aria-valuenow", String(progress));
      $("browserTaskProgressValue").textContent = progress + "%";
      const codePanel = $("registrationCodePanel");
      const awaitingEmail = (registration.awaitingCodeEmails || [])[0] || registration.email || "";
      codePanel.hidden = !registration.awaitingCode || registration.provider === "smsbower";
      $("registrationCodeEmail").textContent = registration.awaitingCode
        ? "验证码将用于 " + awaitingEmail
        : "等待注册页面请求验证码";

      const seenLogs = new Set();
      const recordedFailureLogs = (registration.failureRecords || []).map((record) => ({
        at: record.recordedAt || record.finishedAt || record.startedAt || "",
        email: record.email || (record.emails || [])[0] || "",
        message: "失败邮箱已记录，可重新点击注册：" + (record.message || "注册失败"),
        stage: record.currentStage || "failed",
        location: record.currentLocation || "注册失败记录",
        action: "失败邮箱已记录，可重新注册",
        status: "error",
      }));
      const logs = [
        ...(registration.logs || []).map((item) => ({ ...item, email: item.email || registration.email || "" })),
        ...recordedFailureLogs,
        ...(task.logs || []),
      ].filter((item) => {
        const key = [item.at, item.email, item.message].join("|");
        if (seenLogs.has(key)) return false;
        seenLogs.add(key);
        return true;
      }).sort((left, right) => new Date(left.at || 0) - new Date(right.at || 0))
        .slice(-16).map(inferLogContext);
      const latestContext = logs.at(-1) || inferLogContext({
        stage: primaryTask.currentStage || "idle",
        location: primaryTask.currentLocation || "等待任务",
        action: primaryTask.currentAction || "尚未开始",
        status: primaryTask.currentStatus || "idle",
        email: registration.email || "",
      });
      const stageGroup = taskStageGroup(latestContext.stage);
      $("taskPanel").dataset.currentStage = latestContext.stage;
      $("taskPanel").dataset.stageGroup = stageGroup;
      document.querySelectorAll("#taskPanel .task-flow-step").forEach((step) => {
        const active = step.dataset.flow === stageGroup;
        step.classList.toggle("active", active);
        if (active) step.setAttribute("aria-current", "step");
        else step.removeAttribute("aria-current");
      });
      $("taskCurrentLocation").textContent = latestContext.location;
      $("taskCurrentStage").textContent = taskStageLabel(latestContext.stage);
      $("taskCurrentAction").textContent = latestContext.action;
      $("taskCurrentAccount").textContent = latestContext.email
        ? abbreviateEmail(latestContext.email) : "未选择账号";
      let assistance = {
        mode: "automatic", badge: "自动化执行", title: "页面状态持续监测",
        text: "遇到页面跳转时会自动识别并继续",
      };
      if (latestContext.stage === "security") {
        assistance = {
          mode: "manual", badge: "需要人工操作", title: "请完成当前安全验证",
          text: "浏览器会保持登录状态，验证完成后程序自动继续",
        };
      } else if (latestContext.stage === "google_oauth") {
        assistance = {
          mode: "recovering", badge: "正在自动恢复", title: "检测到 Google 登录页",
          text: "本轮已判定失败；正在关闭当前浏览器、生成全新指纹并重新打开注册",
        };
      } else if (latestContext.stage === "email_verification" && registration.provider === "manual") {
        assistance = {
          mode: "manual", badge: "等待人工输入", title: "请在浏览器输入邮箱验证码",
          text: "提交后程序会继续完成资料、Session 与 2FA",
        };
      } else if (latestContext.status === "error") {
        assistance = {
          mode: "error", badge: "需要检查", title: "当前步骤出现异常",
          text: "请查看右侧最后一条执行记录中的失败原因",
        };
      } else if (latestContext.stage === "completed") {
        assistance = {
          mode: "completed", badge: "任务已完成", title: "账号结果已保存",
          text: "Session、Cookie 与 2FA 状态已写入账号记录",
        };
      }
      $("taskAssistance").dataset.mode = assistance.mode;
      $("taskAssistanceBadge").textContent = assistance.badge;
      $("taskAssistanceTitle").textContent = assistance.title;
      $("taskAssistanceText").textContent = assistance.text;
      $("taskLogCount").textContent = logs.length + " 条";
      $("taskLog").innerHTML = logs.length ? logs.map((item) => {
        const glyph = item.status === "error" ? "!" : item.status === "warning" ? "×" :
          item.status === "waiting" ? "…" : item.status === "success" ? "✓" : "•";
        return '<div class="task-log-row ' + escapeHtml(item.status) + '"><span class="task-log-rail"><i class="task-log-icon ' +
          escapeHtml(item.status) + '">' + glyph + '</i></span><time datetime="' + escapeHtml(item.at || "") + '">' +
          formatClock(item.at) + '</time><div class="task-log-context"><div><span class="task-log-stage">' +
          escapeHtml(taskStageLabel(item.stage)) + '</span><strong>' + escapeHtml(item.location) + '</strong></div><span class="task-log-email" title="' +
          escapeHtml(item.email || "") + '">' + escapeHtml(abbreviateEmail(item.email)) + '</span></div><div class="task-log-copy"><strong>' +
          escapeHtml(item.action) + '</strong><span>' + escapeHtml(item.message || "") + "</span></div></div>";
      }).join("") : '<div class="task-log-empty">暂无任务日志</div>';
      $("taskLog").scrollTop = $("taskLog").scrollHeight;

      const items = this.filteredAccounts(state);
      $("accountSummary").textContent = "显示 " + items.length + " / " + total + " 个账号";
      $("accountTableBody").innerHTML = items.length ? items.map((item) =>
        this.accountRows(item, state.selectedAccountEmail === item.email)
      ).join("") : '<tr><td colspan="6"><div class="empty-state compact">没有匹配的账号</div></td></tr>';
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
      const main = '<tr data-selectable data-action="select-account" data-email="' + escapeHtml(item.email) +
        '" class="' + (selected ? "selected" : "") + '"><td><div class="identity-cell"><span class="avatar">' +
        initials(item.email) + '</span><span class="identity-copy"><strong>' + escapeHtml(item.email) +
        '</strong><small>' + (item.hasTwoFactor ? "2FA 已开启" : "2FA 未开启") +
        '</small></span></div></td><td>' + badge(registered ? "已注册" : "未注册", registered ? "success" : "warning") +
        '</td><td>' + badge(planName(item.accountType), planKind) + '</td><td>' +
        badge(sessionName(item.sessionStatus), sessionKind) + '</td><td>' +
        formatDate(item.lastActivity || item.createdAt) + '</td><td><div class="row-actions"><button class="row-action" data-action="copy-email" data-email="' +
        escapeHtml(item.email) + '">复制邮箱</button><button class="row-action" data-action="select-account" data-email="' +
        escapeHtml(item.email) + '">' + (selected ? "收起" : "更多") + "</button></div></td></tr>";
      if (!selected) return main;
      const twoFactorPrimaryAction = item.hasTwoFactor
        ? this.credentialButton("复制 2FA 密钥", "copy-credential", item, "totp_secret")
        : this.credentialButton("添加 2FA", "enable-2fa", item, "", !item.hasPassword, "primary");
      return main + '<tr class="account-detail-row"><td colspan="6"><div class="account-detail"><div class="credential-summary">' +
        '<span><b>账号</b><code>' + escapeHtml(item.email) + '</code></span><span><b>密码</b><code>' +
        (item.hasPassword ? "••••••••••••" : "尚未保存") + '</code></span><span><b>2FA</b><code>' +
        (item.hasTwoFactor ? "已开启" : "未开启") + '</code></span></div><div class="credential-actions">' +
        this.credentialButton("复制密码", "copy-credential", item, "password", !item.hasPassword) +
        twoFactorPrimaryAction +
        this.credentialButton("复制 2FA 码", "copy-credential", item, "totp_code", !item.hasTwoFactor) +
        this.credentialButton("复制 AT", "copy-credential", item, "access_token", !item.hasSession) +
        this.credentialButton("复制 Session", "copy-credential", item, "session", !item.hasSession) +
        this.credentialButton("获取验证码", "get-code", item) +
        this.credentialButton(item.hasCookies ? "Cookie 刷新状态" : "尚未保存 Cookie", "verify-account", item, "", !item.hasCookies) +
        this.credentialButton("一键导入工作台", "import-workbench", item, "", !item.hasImportableSession) +
        this.credentialButton("复制账号", "copy-account", item, "", !item.hasPassword) +
        this.credentialButton("删除邮箱", "delete-email", item, "", false, "danger") +
        "</div></div></td></tr>";
    }

    credentialButton(label, action, item, kind = "", disabled = false, extra = "") {
      return '<button class="button small ' + extra + '" data-action="' + action + '" data-email="' +
        escapeHtml(item.email) + '" data-kind="' + escapeHtml(kind) + '"' +
        (disabled ? " disabled" : "") + ">" + label + "</button>";
    }

    filteredProtocolAccounts(state) {
      const query = $("protocolSearch")?.value.trim().toLowerCase() || "";
      const status = $("protocolStatusFilter")?.value || "all";
      return state.accounts.filter((item) =>
        (!query || item.email.toLowerCase().includes(query)) &&
        (status === "all" ||
          (status === "ready" && item.protocolReady) ||
          (status === "pending" && !item.protocolReady))
      );
    }

    renderProtocolRegistration(state) {
      const task = state.protocolRegistrationTask || {};
      const runtime = task.runtime || {};
      const selected = new Set(state.selectedProtocolEmails || []);
      const pending = state.accounts.filter((item) => !item.protocolReady);
      const ready = state.accounts.filter((item) => item.protocolReady);
      const passwordReady = state.accounts.filter((item) => item.hasPassword);
      const twoFactorReady = state.accounts.filter((item) => item.hasTwoFactor);
      $("protocolMetrics").innerHTML = [
        metricCard("待协议注册", pending.length, "缺少 Session、密码或 2FA", "amber", "◷"),
        metricCard("密码已确认", passwordReady.length, "至少 12 位并已保存", "green", "K"),
        metricCard("TOTP 2FA", twoFactorReady.length, "验证器已激活", "purple", "2"),
        metricCard("协议就绪", ready.length, "Session + 密码 + 2FA", "green", "✓"),
      ].join("");

      const runtimeBadge = $("protocolRuntimeStatus");
      runtimeBadge.className = "badge " + (runtime.available ? "success" : "error");
      runtimeBadge.textContent = runtime.available ? "Mail Auth 环境可用" : "运行环境未就绪";
      runtimeBadge.title = runtime.error || runtime.projectRoot || "";
      $("startProtocolSelectedButton").disabled = Boolean(task.running) || !runtime.available || !selected.size;
      $("startProtocolAllButton").disabled = Boolean(task.running) || !runtime.available || !pending.length;
      $("stopProtocolButton").disabled = !task.running;
      $("protocolConcurrency").disabled = Boolean(task.running);

      const items = this.filteredProtocolAccounts(state);
      $("protocolAccountSummary").textContent = "显示 " + items.length + " / " + state.accounts.length +
        " 个账号，已选 " + selected.size + " 个";
      $("protocolAccountList").innerHTML = items.length ? items.map((item) => {
        const checked = selected.has(item.email);
        const passwordState = item.hasPassword ? "已确认" : "待添加";
        const factorState = item.hasTwoFactor ? "已开启" : "待激活";
        const sessionState = item.sessionStatus === "ready" ? "可用" : "待获取";
        return '<label class="protocol-account-row ' + (item.protocolReady ? "ready" : "pending") + '">' +
          '<input type="checkbox" data-protocol-email="' + escapeHtml(item.email) + '" ' +
          (checked ? "checked " : "") + (item.protocolReady || task.running ? "disabled" : "") + '>' +
          '<span class="protocol-account-identity"><strong>' + escapeHtml(item.email) + '</strong><small>' +
          (item.protocolReady ? "协议注册完成（密码+2FA）" : "等待 Mail Auth 协议注册") + '</small></span>' +
          '<span class="protocol-account-field"><small>密码</small><b class="' + (item.hasPassword ? "ok" : "") + '">' + passwordState + '</b></span>' +
          '<span class="protocol-account-field"><small>2FA</small><b class="' + (item.hasTwoFactor ? "ok" : "") + '">' + factorState + '</b></span>' +
          '<span class="protocol-account-field"><small>Session</small><b class="' + (item.sessionStatus === "ready" ? "ok" : "") + '">' + sessionState + '</b></span></label>';
      }).join("") : '<div class="empty-state">没有匹配的账号</div>';

      const selectAll = $("protocolSelectAll");
      const visiblePending = items.filter((item) => !item.protocolReady);
      const selectedVisible = visiblePending.filter((item) => selected.has(item.email)).length;
      selectAll.disabled = Boolean(task.running) || !visiblePending.length;
      selectAll.checked = Boolean(visiblePending.length && selectedVisible === visiblePending.length);
      selectAll.indeterminate = Boolean(selectedVisible && selectedVisible < visiblePending.length);

      const meta = taskStatusMeta(task.status || "idle");
      $("protocolTaskBadge").className = "badge " + (task.status === "failed" ? "error" : task.status === "completed" ? "success" : task.running ? "blue" : "");
      $("protocolTaskBadge").textContent = meta[0];
      $("protocolTaskMessage").textContent = task.message || "等待开始";
      $("protocolCurrentEmail").textContent = task.currentEmail || "尚未开始";
      $("protocolCurrentStage").textContent = taskStageLabel(task.phase || "idle");
      const total = Number(task.total || 0);
      const completed = Number(task.completed || 0);
      const progress = total ? Math.round(completed / total * 100) : 0;
      $("protocolTaskProgress").value = progress;
      $("protocolTaskProgressValue").textContent = completed + " / " + total;
      $("protocolTaskSuccess").textContent = Number(task.succeeded || 0);
      $("protocolTaskFailed").textContent = Number(task.failed || 0);
      $("protocolTaskElapsed").textContent = formatElapsed(task.startedAt, task.finishedAt);

      const stageOrder = ["protocol_auth", "email_verification", "password", "two_factor", "completed"];
      let activeStage = task.phase || "";
      if (activeStage === "session") activeStage = "completed";
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

    filteredCardAccounts(state) {
      const query = $("cardSearch")?.value.trim().toLowerCase() || "";
      const status = $("cardStatusFilter")?.value || "all";
      return state.accounts.filter((item) =>
        (!query || item.email.toLowerCase().includes(query)) &&
        (status === "all" ||
          (status === "generated" && item.cardLink) ||
          (status === "available" && item.sessionStatus === "ready") ||
          (status === "unavailable" && item.sessionStatus !== "ready"))
      );
    }

    renderCardLinks(state) {
      const payable = state.accounts.filter((item) => item.sessionStatus === "ready").length;
      const generated = state.accounts.filter((item) => item.cardLink).length;
      $("cardMetrics").innerHTML = [
        metricCard("全部", state.accounts.length, "账号总数", "", "◎"),
        metricCard("可提取", payable, "Session 可用", "green", "✓"),
        metricCard("已生成", generated, "支付链接", "purple", "↗"),
      ].join("");
      const items = this.filteredCardAccounts(state);
      $("cardLinkSummary").textContent = "显示 " + items.length + " 个账号，已生成 " + generated + " 个链接";
      $("cardAccountList").innerHTML = items.length ? items.map((item) => {
        const selected = state.selectedCardEmail === item.email;
        return '<button class="select-row ' + (selected ? "selected" : "") +
          '" data-action="select-card-account" data-email="' + escapeHtml(item.email) +
          '"><span class="select-indicator"></span><span class="identity-cell"><span class="avatar">' +
          initials(item.email) + '</span><span class="identity-copy"><strong>' + escapeHtml(item.email) +
          '</strong><small>' + (item.cardLink ? "已生成链接" : "未生成") + '</small></span></span><span>' +
          badge(planName(item.accountType), item.accountType === "plus" ? "plus" : "") + '</span><span>' +
          badge(sessionName(item.sessionStatus), item.sessionStatus === "ready" ? "success" : "warning") +
          "</span></button>";
      }).join("") : '<div class="empty-state">没有匹配的账号</div>';
      this.renderCardSelection(state);
    }

    renderCardSelection(state) {
      const item = state.accounts.find((candidate) => candidate.email === state.selectedCardEmail);
      const generate = $("generateCardLinkButton");
      const copy = $("copyCardLinkButton");
      const open = $("openCardLinkButton");
      generate.disabled = !item || item.sessionStatus !== "ready";
      copy.disabled = !item?.cardLink;
      open.disabled = !item?.cardLink;
      if (!item) {
        $("cardOperationState").className = "empty-state compact";
        $("cardOperationState").textContent = "请选择一个账号";
        return;
      }
      $("cardOperationState").className = "operation-result";
      $("cardOperationState").innerHTML = '<strong>' + escapeHtml(item.email) + '</strong><span>' +
        (item.cardLink ? "已生成 PH / PHP hosted 严格 0 链接" : "等待提取支付链接") +
        '</span><code>' + escapeHtml(item.cardLink || "尚无链接") + '</code><span>Session：' +
        sessionName(item.sessionStatus) + "</span>";
    }

    renderPayPal(state) {
      const service = state.paypal || {};
      const status = $("paypalServiceStatus");
      const navState = $("paypalNavState");
      const frame = $("paypalPaymentFrame");
      const empty = $("paypalPaymentEmpty");
      if (service.running) {
        status.className = "badge success";
        status.textContent = "服务已连接";
        navState.textContent = "在线";
        empty.hidden = true;
        frame.hidden = false;
        if (!frame.dataset.loaded) {
          frame.src = service.url || frame.dataset.src;
          frame.dataset.loaded = "1";
        }
      } else {
        status.className = "badge warning";
        status.textContent = service.error ? "启动失败" : "正在启动";
        navState.textContent = "—";
        frame.hidden = true;
        empty.hidden = false;
        const detail = empty.querySelector("p");
        if (detail) detail.textContent = service.error || "服务就绪后会自动载入协议支付工作台。";
      }
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
      $("settingsStatus").className = "badge " + (inbox.configured ? "success" : "warning");
      $("settingsStatus").textContent = inbox.configured ? "IMAP 已保存" : "等待配置";
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
        '</span></div></section><div class="settings-actions"><button class="button" type="button" data-action="sync-inbox">立即同步</button><button class="button primary" type="button" data-action="save-imap">保存并测试</button></div></form>';
    }

    renderBrowserSettings(state) {
      const runtime = state.browserTask.runtime || {};
      const proxy = state.registrationProxy || {};
      $("settingsStatus").className = "badge " + (runtime.available ? "success" : "error");
      $("settingsStatus").textContent = runtime.available ? "运行环境可用" : "运行环境不可用";
      $("settingsPanel").innerHTML =
        '<div class="settings-form"><section class="form-section"><h3>任务默认值</h3><div class="toggle-row"><span>无头浏览器<small style="display:block;color:var(--muted)">服务器环境推荐开启</small></span><input id="settingsHeadless" type="checkbox" ' +
        ($("headless").checked ? "checked" : "") + '></div><label class="field-label" style="margin-top:14px">验证并发<input id="settingsConcurrency" type="number" min="1" max="10" value="' +
        escapeHtml($("concurrency").value) + '"></label></section><section class="form-section"><h3>运行环境</h3><div class="connection-card"><strong>' +
        (runtime.available ? "✓ Camoufox 运行环境已连接" : "× Camoufox 运行环境不可用") +
        '</strong><span>' + escapeHtml(runtime.targetProject || (runtime.errors || []).join("；") || "未返回运行目录") +
        '</span></div></section><section class="form-section"><h3>注册代理</h3><div class="connection-card"><strong>' +
        (proxy.enabled
          ? "✓ 已启用 " + (proxy.mode === "clash" ? "Clash 日本节点轮询" : escapeHtml(proxy.countryLabel || proxy.country || "") + " 出口")
          : "本机 IP 直连") +
        '</strong><span>代理连接：' + escapeHtml(proxy.endpoint || "未保存") + '</span><span>' +
        (proxy.mode === "clash"
          ? "每个账号开始前选择新日本节点，延迟高于 " + escapeHtml(proxy.maxLatencyMs || 900) + " ms 自动跳过；选定后全程固定出口并强制串行。"
          : "开启后每个账号自动生成独立 SID；关闭时注册全程走本机公网 IP。") +
        '</span></div></section><div class="settings-actions"><button class="button" data-action="set-registration-proxy-credential">' +
        (proxy.mode === "clash" ? "检测并切换日本 IP" : "更新代理连接") +
        '</button><button class="button primary" data-action="save-browser-settings">保存设置</button></div></div>';
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
        smsBower: { configured: false, service: "dr", domain: "gmail.com", maxPrice: 0.05 },
        verificationTask: { status: "idle", runtime: {} },
        paypal: { available: false, running: false, error: "", url: "/paypal-pay/" },
        inbox: { configured: false, codeCount: 0 },
        selectedAccountEmail: "",
        selectedProtocolEmails: [],
        selectedCardEmail: "",
        selectedVerificationEmail: "",
        verificationFilter: "all",
        settingsSection: "imap",
      });
      this.renderer = new WorkspaceRenderer(this.store);
      this.router = new HashRouter({
        overview: () => this.renderer.renderOverview(this.store.state),
        accounts: () => this.renderer.renderAccounts(this.store.state),
        "protocol-registration": () => this.renderer.renderProtocolRegistration(this.store.state),
        "card-links": () => this.renderer.renderCardLinks(this.store.state),
        "pp-payment": () => this.renderer.renderPayPal(this.store.state),
        verification: () => this.renderer.renderVerification(this.store.state),
        settings: () => this.renderer.renderSettings(this.store.state),
      });
      this.commands = new CommandBus((message, type) => this.toast(message, type));
      this.pollTimers = {};
      this.bindCommands();
    }

    render(state) {
      this.renderer.renderShell(state);
      this.renderer.renderOverview(state);
      this.renderer.renderAccounts(state);
      this.renderer.renderProtocolRegistration(state);
      this.renderer.renderCardLinks(state);
      this.renderer.renderVerification(state);
      if (this.router.current === "pp-payment") this.renderer.renderPayPal(state);
      if (this.router.current === "settings") this.renderer.renderSettings(state);
    }

    applyTheme(theme, persist = true) {
      const resolved = theme === "system"
        ? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")
        : theme;
      document.documentElement.dataset.theme = resolved;
      document.querySelector('meta[name="theme-color"]').content = resolved === "dark" ? "#08111f" : "#f3f6fa";
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

    async loadAccounts() {
      const data = await this.api.get("/api/gpt-emails");
      const accounts = data.items || [];
      const pendingEmails = new Set(accounts.filter((item) => !item.protocolReady).map((item) => item.email));
      const patch = {
        accounts,
        selectedProtocolEmails: (this.store.state.selectedProtocolEmails || []).filter((email) => pendingEmails.has(email)),
      };
      if (!this.store.state.selectedCardEmail && data.items?.length) {
        patch.selectedCardEmail = data.items.find((item) => item.sessionStatus === "ready")?.email || data.items[0].email;
      }
      this.store.patch(patch);
    }

    async loadBrowserTask() {
      try {
        const data = await this.api.get("/api/browser/status");
        const wasRunning = Boolean(this.store.state.browserTask.running);
        this.store.patch({ browserTask: data });
        if (data.runtime?.forceHeadless) {
          $("headless").checked = true;
          $("headless").disabled = true;
        }
        if (data.running) this.schedule("browser", () => this.loadBrowserTask());
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
        this.store.patch({ protocolRegistrationTask: data });
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
      const data = await this.api.get("/api/paypal/status");
      this.store.patch({ paypal: data });
    }

    async loadRegistrationProxy() {
      const data = await this.api.get("/api/registration-proxy/status");
      this.store.patch({ registrationProxy: data });
    }

    async loadSmsBower() {
      const data = await this.api.get("/api/smsbower/status");
      this.store.patch({ smsBower: data });
    }

    browserOptions() {
      const concurrency = Number($("concurrency").value);
      if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 10) {
        throw new Error("并发数必须是 1–10 的整数");
      }
      return { headless: $("headless").checked, concurrency };
    }

    verificationConcurrency() {
      const concurrency = Number($("verificationConcurrency").value);
      if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 10) {
        throw new Error("验证并发必须是 1–10 的整数");
      }
      return concurrency;
    }

    protocolConcurrency() {
      const concurrency = Number($("protocolConcurrency").value);
      if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 5) {
        throw new Error("协议注册并发必须是 1–5 的整数");
      }
      return concurrency;
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
      await Promise.all([this.loadAccounts(), this.loadBrowserTask(), this.loadVerificationTask()]);
      if (data.mode === "deleted_invalid") {
        return data.message || "无效邮箱已自动删除；请选择下一个账号继续验证";
      }
      this.schedule("verification", () => this.loadVerificationTask(), 800);
      return data.mode === "refresh_cookie"
        ? "正在使用保存的 Cookie 刷新 Session 与账号状态"
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

    bindCommands() {
      this.commands.register("refresh", async () => {
        await Promise.all([this.loadAccounts(), this.loadBrowserTask(), this.loadRegistrationTask(), this.loadProtocolRegistrationTask(), this.loadVerificationTask(), this.loadInbox(), this.loadRegistrationProxy(), this.loadSmsBower(), this.loadPayPal()]);
        return "数据已刷新";
      });
      this.commands.register("reload-paypal", async () => {
        await this.loadPayPal();
        if (!this.store.state.paypal.running) {
          throw new Error(this.store.state.paypal.error || "PP 支付服务尚未就绪");
        }
        const frame = $("paypalPaymentFrame");
        frame.src = this.store.state.paypal.url || frame.dataset.src;
        frame.dataset.loaded = "1";
        return "PP 支付工作台已刷新";
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
        const options = this.browserOptions();
        const source = $("registrationEmailProvider").value || "icloud";
        const smsBower = this.store.state.smsBower || {};
        if (source === "gmail") {
          if (!smsBower.configured) throw new Error("请先设置 SMSBower API Key");
          const maxPrice = Number($("smsbowerMaxPrice").value);
          if (!Number.isFinite(maxPrice) || maxPrice < 0.001 || maxPrice > 10) {
            throw new Error("Gmail 最高价必须在 0.001–10 美元之间");
          }
          const config = await this.api.post("/api/smsbower/config", { maxPrice });
          this.store.patch({ smsBower: { ...smsBower, ...config } });
        }
        const data = await this.api.post("/api/registration/start", {
          label: source === "gmail" ? "SMSBower Gmail 注册" : "iCloud 邮箱注册",
          provider: source === "gmail" ? "smsbower" : "inventory",
          ...options,
          concurrency: 1,
        });
        this.store.patch({ registrationTask: data.task });
        this.schedule("registration", () => this.loadRegistrationTask(), 500);
        return source === "gmail"
          ? "已启动 SMSBower Gmail 获取与自动注册（" + (options.headless ? "无头" : "前台窗口") + "）"
          : "已启动 iCloud 库存邮箱注册";
      });
      this.commands.register("start-protocol-selected", async () => {
        const emails = [...(this.store.state.selectedProtocolEmails || [])];
        if (!emails.length) throw new Error("请先选择待协议注册账号");
        if (!confirm("对选中的 " + emails.length + " 个账号执行 Mail Auth，并强制完成密码与 TOTP 2FA？")) {
          throw Object.assign(new Error(), { name: "AbortError" });
        }
        const data = await this.api.post("/api/protocol-registration/start", {
          emails, concurrency: this.protocolConcurrency(), all: false,
        });
        this.store.patch({ protocolRegistrationTask: data.task });
        this.schedule("protocol-registration", () => this.loadProtocolRegistrationTask(), 500);
        return "协议注册选中任务已启动";
      });
      this.commands.register("start-protocol-all", async () => {
        const pending = this.store.state.accounts.filter((item) => !item.protocolReady).length;
        if (!pending) throw new Error("全部账号均已完成协议注册（密码+2FA）");
        if (!confirm("协议注册全部待处理账号，共 " + pending + " 个，是否继续？")) {
          throw Object.assign(new Error(), { name: "AbortError" });
        }
        const data = await this.api.post("/api/protocol-registration/start", {
          all: true, concurrency: this.protocolConcurrency(), emails: [],
        });
        this.store.patch({ protocolRegistrationTask: data.task, selectedProtocolEmails: [] });
        this.schedule("protocol-registration", () => this.loadProtocolRegistrationTask(), 500);
        return "协议注册全部任务已启动" + (data.skipped ? "，已跳过 " + data.skipped + " 个完整账号" : "");
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
      this.commands.register("set-registration-proxy-credential", async () => {
        const current = this.store.state.registrationProxy || {};
        if ((current.mode || "dynamic") === "clash") {
          await this.api.post("/api/registration-proxy/config", {
            mode: "clash", country: "JP", enabled: true, maxLatencyMs: 900,
          });
          const data = await this.api.post("/api/registration-proxy/rotate", {});
          this.store.patch({ registrationProxy: data });
          return "Clash 日本出口已固定：" + (data.currentNode || "日本节点") +
            "，延迟 " + (data.lastLatencyMs || 0) + " ms";
        }
        const proxyLine = prompt("输入动态代理连接（host:port:username:password）。凭据只保存在本地，不会显示在日志中：", "");
        if (proxyLine === null) throw Object.assign(new Error(), { name: "AbortError" });
        if (!proxyLine.trim()) throw new Error("代理连接不能为空");
        const country = $("registrationProxyCountry").value || "NL";
        const data = await this.api.post("/api/registration-proxy/config", {
          mode: "dynamic", proxyLine, country, enabled: true,
        });
        this.store.patch({ registrationProxy: data });
        return "动态代理已保存并启用：" + (data.countryLabel || data.country);
      });
      this.commands.register("fetch-all", async () => {
        const options = this.browserOptions();
        const data = await this.api.post("/api/browser/fetch-all", options);
        await this.loadBrowserTask();
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
        const data = await this.api.post("/api/gpt-email/delete", { email });
        await this.loadAccounts();
        return data.message || "邮箱已删除";
      });
      this.commands.register("select-card-account", async ({ element }) => {
        this.store.patch({ selectedCardEmail: element.dataset.email });
      });
      this.commands.register("generate-card-link", async () => {
        const item = this.selectedAccount(this.store.state.selectedCardEmail);
        if (!item) throw new Error("请先选择账号");
        const data = await this.api.post("/api/account/card-link", {
          email: item.email, method: "ph_hosted", country: "PH",
          create_proxy: $("cardLinkCreateProxy").value.trim(),
          promotion_proxy: $("cardLinkPromotionProxy").value.trim(),
        });
        await this.copyText(data.url);
        await this.loadAccounts();
        return "严格 0 hosted 链接已提取并复制";
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
        $("headless").checked = $("settingsHeadless").checked;
        $("concurrency").value = $("settingsConcurrency").value;
        return "浏览器默认设置已更新";
      });
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
      ["accountSearch", "accountPlanFilter", "accountSessionFilter"].forEach((id) => {
        $(id).addEventListener(id === "accountSearch" ? "input" : "change", () => this.renderer.renderAccounts(this.store.state));
      });
      ["protocolSearch", "protocolStatusFilter"].forEach((id) => {
        $(id).addEventListener(id === "protocolSearch" ? "input" : "change", () => this.renderer.renderProtocolRegistration(this.store.state));
      });
      $("protocolAccountList").addEventListener("change", (event) => {
        const checkbox = event.target.closest("[data-protocol-email]");
        if (!checkbox) return;
        const selected = new Set(this.store.state.selectedProtocolEmails || []);
        if (checkbox.checked) selected.add(checkbox.dataset.protocolEmail);
        else selected.delete(checkbox.dataset.protocolEmail);
        this.store.patch({ selectedProtocolEmails: [...selected] });
      });
      $("protocolSelectAll").addEventListener("change", (event) => {
        const selected = new Set(this.store.state.selectedProtocolEmails || []);
        this.renderer.filteredProtocolAccounts(this.store.state)
          .filter((item) => !item.protocolReady)
          .forEach((item) => event.target.checked ? selected.add(item.email) : selected.delete(item.email));
        this.store.patch({ selectedProtocolEmails: [...selected] });
      });
      ["cardSearch", "cardStatusFilter"].forEach((id) => {
        $(id).addEventListener(id === "cardSearch" ? "input" : "change", () => this.renderer.renderCardLinks(this.store.state));
      });
      $("verificationAccountSelect").addEventListener("change", (event) => {
        this.store.patch({ selectedVerificationEmail: event.target.value });
      });
      $("registrationEmailProvider").addEventListener("change", () => {
        this.renderer.renderAccounts(this.store.state);
      });
      $("registrationProxyEnabled").addEventListener("change", async (event) => {
        try {
          const data = await this.api.post("/api/registration-proxy/config", {
            enabled: event.target.checked,
          });
          this.store.patch({ registrationProxy: data });
          this.toast(data.enabled ? "注册动态代理已启用" : "动态代理已关闭，注册将使用本机 IP 直连");
        } catch (error) {
          event.target.checked = !event.target.checked;
          this.toast(error.message, "error");
        }
      });
      $("registrationProxyMode").addEventListener("change", async (event) => {
        try {
          const mode = event.target.value;
          const data = await this.api.post("/api/registration-proxy/config", {
            mode,
            country: mode === "clash" ? "JP" : ($("registrationProxyCountry").value || "NL"),
          });
          this.store.patch({ registrationProxy: data });
          this.toast(mode === "clash"
            ? "已切换为 Clash 日本节点轮询，点击检测按钮验证出口"
            : "已切换为动态 SID 代理");
        } catch (error) {
          await this.loadRegistrationProxy();
          this.toast(error.message, "error");
        }
      });
      $("registrationProxyCountry").addEventListener("change", async (event) => {
        try {
          const data = await this.api.post("/api/registration-proxy/config", {
            country: event.target.value,
          });
          this.store.patch({ registrationProxy: data });
          this.toast("注册代理出口已切换为 " + (data.countryLabel || data.country));
        } catch (error) {
          await this.loadRegistrationProxy();
          this.toast(error.message, "error");
        }
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
      this.applyTheme(savedTheme, false);
      this.store.subscribe((state) => this.render(state));
      this.bindEvents();
      this.router.start();
      const results = await Promise.allSettled([
        this.loadAccounts(), this.loadBrowserTask(), this.loadRegistrationTask(), this.loadProtocolRegistrationTask(),
        this.loadVerificationTask(), this.loadInbox(), this.loadRegistrationProxy(), this.loadSmsBower(), this.loadPayPal(),
      ]);
      const failure = results.find((result) => result.status === "rejected");
      if (failure) this.toast(failure.reason.message, "error");
    }
  }

  new WorkspaceController().start();
})();
