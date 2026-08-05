(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const localToken = window.__HME_LOCAL_TOKEN__;
  const pageDetails = {
    overview: ["DASHBOARD", "概览", "集中查看账号、任务与服务状态"],
    accounts: ["ACCOUNT WORKSPACE", "邮箱账号", "管理 iCloud 邮箱、OpenAI Session 与账号凭据"],
    "card-links": ["CHECKOUT WORKSPACE", "直卡提链接", "使用 PH / PHP hosted 双代理流程提取严格 0 链接"],
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

  function formatCountdown(value) {
    const seconds = Math.max(0, Number(value || 0));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remaining = Math.floor(seconds % 60);
    return [hours, minutes, remaining]
      .map((part) => String(part).padStart(2, "0"))
      .join(":");
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
        metricCard("全部邮箱", total, "iCloud 隐藏邮箱", "", "✉"),
        metricCard("已注册", registered, "OpenAI 账号", "green", "✓"),
        metricCard("Session 可用", sessions, "可直接验证与提链", "", "S"),
        metricCard("已开启 2FA", twoFactor, "双重验证", "amber", "2"),
      ].join("");
      const registrationProxy = state.registrationProxy || {};
      $("registrationProxyEnabled").checked = Boolean(registrationProxy.enabled);
      $("registrationProxyEnabled").disabled = !registrationProxy.configured;
      $("registrationProxyEnabled").title = registrationProxy.configured
        ? "注册全程使用所选国家的粘性动态代理"
        : "请先设置代理连接";
      if (registrationProxy.country) {
        $("registrationProxyCountry").value = registrationProxy.country;
      }
      const task = state.browserTask;
      const registration = state.registrationTask;
      const hasRegistration = Boolean(registration.id && registration.status !== "idle");
      const primaryTask = hasRegistration ? registration : task;
      const status = primaryTask.status || "idle";
      const statusMeta = taskStatusMeta(status);
      const taskTotal = Number(task.total || (hasRegistration ? registration.requested || 1 : 0));
      const taskCompleted = Number(task.total
        ? task.completed || 0
        : (hasRegistration && !registration.running ? 1 : 0));
      const taskSucceeded = Number(task.succeeded || (registration.status === "completed" ? 1 : 0));
      let progress = taskTotal ? Math.round(taskCompleted / taskTotal * 100) : 0;
      if (hasRegistration && registration.running && !task.total) {
        progress = {
          generating_email: 12,
          claiming_inventory: 12,
          confirming_email: 28,
          registering_openai: 40,
          cancelling: 40,
        }[registration.phase] || progress;
      }
      if (registration.status === "completed") progress = 100;

      $("taskPanel").dataset.taskTone = statusMeta[1];
      $("taskStatusIcon").textContent = statusMeta[2];
      $("taskStateBadge").textContent = statusMeta[0];
      $("registrationSummary").textContent = hasRegistration
        ? "一键注册 · " + this.registrationLabel(registration)
        : (task.status && task.status !== "idle"
          ? "浏览器任务 · " + statusMeta[0]
          : "一键注册 · 等待开始");
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

      const seenLogs = new Set();
      const logs = [
        ...(registration.logs || []).map((item) => ({ ...item, email: item.email || registration.email || "" })),
        ...(task.logs || []),
      ].filter((item) => {
        const key = [item.at, item.email, item.message].join("|");
        if (seenLogs.has(key)) return false;
        seenLogs.add(key);
        return true;
      }).sort((left, right) => new Date(left.at || 0) - new Date(right.at || 0)).slice(-8);
      $("taskLog").innerHTML = logs.length ? logs.map((item) => {
        const message = String(item.message || "");
        const kind = /失败|错误|异常/.test(message) ? "error" : /停止|取消/.test(message) ? "warning" : "success";
        const glyph = kind === "error" ? "!" : kind === "warning" ? "×" : "✓";
        return '<div class="task-log-row"><span class="task-log-icon ' + kind + '">' + glyph +
          '</span><time datetime="' + escapeHtml(item.at || "") + '">' + formatClock(item.at) +
          '</time><span class="task-log-email" title="' + escapeHtml(item.email || "") + '">' +
          escapeHtml(abbreviateEmail(item.email)) + '</span><span class="task-log-message">' +
          escapeHtml(message) + "</span></div>";
      }).join("") : '<div class="task-log-empty">暂无任务日志</div>';
      $("taskLog").scrollTop = $("taskLog").scrollHeight;

      const items = this.filteredAccounts(state);
      $("accountSummary").textContent = "显示 " + items.length + " / " + total + " 个账号";
      $("accountTableBody").innerHTML = items.length ? items.map((item) =>
        this.accountRows(item, state.selectedAccountEmail === item.email)
      ).join("") : '<tr><td colspan="6"><div class="empty-state compact">没有匹配的账号</div></td></tr>';
    }

    renderScheduledGeneration(state) {
      const schedule = state.scheduledGeneration || {};
      const enabled = Boolean(schedule.enabled);
      const running = Boolean(schedule.running);
      const hasError = Boolean(schedule.lastError);
      const panel = $("scheduledGenerationPanel");
      panel.dataset.taskTone = running || enabled ? (hasError ? "failed" : "running") : "cancelled";
      $("scheduledGenerationIcon").textContent = running ? "↻" : enabled ? "◷" : "×";
      $("scheduledGenerationBadge").textContent = running ? "生成中" : enabled ? "计时中" : "已暂停";
      $("scheduledGenerationMessage").textContent = running
        ? "正在生成 5 个邮箱并存入库存；不会启动 OpenAI 注册。"
        : enabled
          ? "下次执行：" + formatDate(schedule.nextRunAt) + "；每轮只生成邮箱，不注册。"
          : "定时生成已暂停；重新启用后会从完整 1 小时重新计时。";
      $("scheduledGenerationBatch").textContent = Number(schedule.batchSize || 5) + " 个";
      $("scheduledGenerationInterval").textContent = "1 小时";
      const inventoryAvailable = Math.max(0, Number(schedule.inventoryAvailable || 0));
      $("registrationInventoryAvailable").textContent = inventoryAvailable + " 个";
      $("registerFromInventoryButton").textContent = "从库存注册账号（" + inventoryAvailable + "）";
      $("registerFromInventoryButton").title = "当前可领取 " + inventoryAvailable + " 个生成邮箱";
      $("scheduledGenerationCountdown").textContent = enabled
        ? formatCountdown(schedule.secondsUntilNext)
        : "已暂停";
      const toggle = $("toggleScheduledGenerationButton");
      toggle.textContent = enabled ? "暂停定时生成" : "启用并开始 1 小时计时";
      toggle.className = "button " + (enabled ? "danger" : "primary") + " small";

      const logs = Array.isArray(schedule.logs) ? schedule.logs.slice(-8) : [];
      $("scheduledGenerationLog").innerHTML = logs.length ? logs.map((item) => {
        const level = item.level === "error" ? "error" : item.level === "warning" ? "warning" : "success";
        const glyph = level === "error" ? "!" : level === "warning" ? "×" : "✓";
        return '<div class="task-log-row"><span class="task-log-icon ' + level + '">' + glyph +
          '</span><time datetime="' + escapeHtml(item.at || "") + '">' + formatClock(item.at) +
          '</time><span class="task-log-email">定时检查</span><span class="task-log-message">' +
          escapeHtml(item.message || "") + "</span></div>";
      }).join("") : '<div class="task-log-empty">暂无定时生成检查日志</div>';
      $("scheduledGenerationLog").scrollTop = $("scheduledGenerationLog").scrollHeight;
    }

    registrationLabel(task) {
      const labels = {
        idle: "空闲", generating_email: "正在创建邮箱", claiming_inventory: "正在领取库存", confirming_email: "正在确认邮箱",
        registering_openai: "正在注册 OpenAI", completed: "注册成功",
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
      return main + '<tr class="account-detail-row"><td colspan="6"><div class="account-detail"><div class="credential-summary">' +
        '<span><b>账号</b><code>' + escapeHtml(item.email) + '</code></span><span><b>密码</b><code>' +
        (item.hasPassword ? "••••••••••••" : "尚未保存") + '</code></span><span><b>2FA</b><code>' +
        (item.hasTwoFactor ? "已开启" : "未开启") + '</code></span></div><div class="credential-actions">' +
        this.credentialButton("复制密码", "copy-credential", item, "password", !item.hasPassword) +
        this.credentialButton("复制 2FA 密钥", "copy-credential", item, "totp_secret", !item.hasTwoFactor) +
        this.credentialButton("复制 2FA 码", "copy-credential", item, "totp_code", !item.hasTwoFactor) +
        this.credentialButton("复制 AT", "copy-credential", item, "access_token", !item.hasSession) +
        this.credentialButton("复制 Session", "copy-credential", item, "session", !item.hasSession) +
        this.credentialButton("获取验证码", "get-code", item) +
        this.credentialButton("验证账号", "verify-account", item) +
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
      const errors = rows.filter((item) => ["failed", "expired", "error"].includes(item.status) || item.sessionStatus === "expired").length;
      $("verificationMetrics").innerHTML = [
        metricCard("本次验证", task.total || rows.length, "账号数量", "", "✓"),
        metricCard("Plus 账号", task.plus || plus, "已识别 Plus", "purple", "P"),
        metricCard("Free 账号", task.free || free, "已识别 Free", "green", "F"),
        metricCard("异常账号", task.failed || task.expired || errors, "需要处理", "amber", "!"),
      ].join("");
      $("verificationTaskTitle").textContent = task.id ? "验证任务 #" + task.id.slice(0, 10) : "验证任务";
      $("verificationSummary").textContent = task.status && task.status !== "idle"
        ? (task.completed || 0) + " / " + (task.total || 0) + " 已完成"
        : "尚未开始批量验证";
      $("verificationProgress").value = task.total ? Math.round((task.completed || 0) / task.total * 100) : 0;
      $("verificationProgress").hidden = !task.running;
      const selectedEmail = state.accounts.some((item) => item.email === state.selectedVerificationEmail)
        ? state.selectedVerificationEmail : "";
      $("verificationAccountSelect").innerHTML = '<option value="">请选择一个账号（共 ' +
        state.accounts.length + ' 个）</option>' +
        state.accounts.map((item) => '<option value="' + escapeHtml(item.email) + '">' +
          escapeHtml(item.email + " · " + planName(item.accountType) + " · Session " + sessionName(item.sessionStatus)) +
          "</option>").join("");
      $("verificationAccountSelect").value = selectedEmail;
      $("verificationAccountSelect").disabled = Boolean(task.running) || !state.accounts.length;
      $("verifySelectedButton").disabled = Boolean(task.running) || !selectedEmail || !task.runtime?.available;
      $("verifyAllButton").disabled = Boolean(task.running) || !state.verificationTask.runtime?.available;
      $("stopVerificationButton").disabled = !task.running;
      const filter = state.verificationFilter;
      const filtered = rows.filter((item) =>
        filter === "all" ||
        (filter === "plus" && item.accountType === "plus") ||
        (filter === "free" && item.accountType === "free") ||
        (filter === "error" && (["failed", "expired", "error"].includes(item.status) || item.sessionStatus === "expired"))
      );
      $("verificationTableBody").innerHTML = filtered.length ? filtered.map((item) => {
        const selected = state.selectedVerificationEmail === item.email;
        const status = item.status || "pending";
        const isError = ["failed", "expired", "error"].includes(status) || item.sessionStatus === "expired";
        const statusLabel = isError ? "异常" : status === "queued" ? "等待中" :
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
      const isError = ["failed", "expired", "error"].includes(status) || item.sessionStatus === "expired";
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
        '</div>' : "") + '<div class="verification-detail-actions"><button class="button primary" data-action="verify-account" data-email="' +
        escapeHtml(item.email) + '"' + (state.verificationTask.running ? " disabled" : "") + '>' +
        (sessionReady ? "验证此账号" : "获取 Session 并验证") + "</button></div>";
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
      $("settingsStatus").className = "badge " + (inbox.configured ? "success" : "warning");
      $("settingsStatus").textContent = inbox.configured ? "IMAP 已配置" : "等待配置";
      $("settingsPanel").innerHTML =
        '<form id="imapForm" class="settings-form"><section class="form-section"><h3>邮箱连接</h3><div class="form-grid two">' +
        '<label class="field-label">IMAP 主机<input id="imapHost" value="' + escapeHtml(inbox.host || "") +
        '" placeholder="imap.mail.me.com"></label><label class="field-label">端口<input id="imapPort" type="number" value="' +
        escapeHtml(inbox.port || 993) + '"></label><label class="field-label">邮箱账号<input id="imapUsername" value="' +
        escapeHtml(inbox.username || "") + '" placeholder="name@icloud.com"></label><label class="field-label">应用专用密码<input id="imapPassword" type="password" placeholder="' +
        (inbox.configured ? "留空则保持现有密码" : "请输入应用专用密码") + '"></label></div></section>' +
        '<section class="form-section"><h3>连接状态</h3><div class="connection-card"><strong>' +
        (inbox.configured ? "✓ IMAP 配置可用" : "尚未配置 IMAP") + '</strong><span>验证码数量：' +
        (inbox.codeCount || 0) + '</span><span>后台同步：' + escapeHtml(inbox.lastBackgroundSync || "尚未同步") +
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
        '</span></div></section><section class="form-section"><h3>注册动态代理</h3><div class="connection-card"><strong>' +
        (proxy.enabled ? "✓ 已启用 " + escapeHtml(proxy.countryLabel || proxy.country || "") + " 出口" : proxy.configured ? "已配置但暂停" : "尚未配置") +
        '</strong><span>连接：' + escapeHtml(proxy.endpoint || "未保存") + '</span><span>每个账号自动生成独立 SID；单账号注册、2FA 和 Session 获取全程保持同一出口。</span></div></section><div class="settings-actions"><button class="button" data-action="set-registration-proxy-credential">更新代理连接</button><button class="button primary" data-action="save-browser-settings">保存设置</button></div></div>';
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
        scheduledGeneration: { enabled: true, running: false, batchSize: 5, intervalSeconds: 3600, logs: [] },
        registrationProxy: { enabled: false, configured: false, country: "NL", countries: [] },
        verificationTask: { status: "idle", runtime: {} },
        inbox: { configured: false, codeCount: 0 },
        selectedAccountEmail: "",
        selectedCardEmail: "",
        selectedVerificationEmail: "",
        verificationFilter: "all",
        settingsSection: "imap",
      });
      this.renderer = new WorkspaceRenderer(this.store);
      this.router = new HashRouter({
        overview: () => this.renderer.renderOverview(this.store.state),
        accounts: () => this.renderer.renderAccounts(this.store.state),
        "card-links": () => this.renderer.renderCardLinks(this.store.state),
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
      this.renderer.renderScheduledGeneration(state);
      this.renderer.renderCardLinks(state);
      this.renderer.renderVerification(state);
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
      const patch = { accounts: data.items || [] };
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
          this.loadAccounts(), this.loadBrowserTask(), this.loadScheduledGeneration(),
        ]);
      } catch (error) {
        this.toast(error.message, "error");
        this.schedule("registration", () => this.loadRegistrationTask(), 2500);
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

    async loadScheduledGeneration() {
      try {
        const previousTotal = Number(this.store.state.scheduledGeneration.totalGenerated || 0);
        const data = await this.api.get("/api/scheduled-generation/status");
        this.store.patch({ scheduledGeneration: data });
        if (Number(data.totalGenerated || 0) > previousTotal) await this.loadAccounts();
        this.schedule("scheduled-generation", () => this.loadScheduledGeneration(), 10000);
      } catch (error) {
        this.toast(error.message, "error");
        this.schedule("scheduled-generation", () => this.loadScheduledGeneration(), 15000);
      }
    }

    async loadRegistrationProxy() {
      const data = await this.api.get("/api/registration-proxy/status");
      this.store.patch({ registrationProxy: data });
    }

    browserOptions() {
      const concurrency = Number($("concurrency").value);
      if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 10) {
        throw new Error("并发数必须是 1–10 的整数");
      }
      return { headless: $("headless").checked, concurrency };
    }

    selectedAccount(email) {
      return this.store.state.accounts.find((item) => item.email === email);
    }

    async startAccountVerification(email) {
      const item = this.selectedAccount(email);
      if (!item) throw new Error("请先选择一个账号");
      this.store.patch({ selectedVerificationEmail: item.email });
      const data = await this.api.post("/api/account/verify-or-register", {
        email: item.email, headless: $("headless").checked, reset_password: false,
      });
      await Promise.all([this.loadBrowserTask(), this.loadVerificationTask()]);
      this.schedule("verification", () => this.loadVerificationTask(), 800);
      return data.mode === "verify" ? "正在验证 Plus 与 Session 状态" : "正在获取 Session 并验证套餐";
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
        await Promise.all([this.loadAccounts(), this.loadBrowserTask(), this.loadRegistrationTask(), this.loadVerificationTask(), this.loadInbox(), this.loadScheduledGeneration(), this.loadRegistrationProxy()]);
        return "数据已刷新";
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
        const data = await this.api.post("/api/registration/start", {
          label: "OpenAI 一键注册", ...options,
        });
        this.store.patch({ registrationTask: data.task });
        this.schedule("registration", () => this.loadRegistrationTask(), 800);
        return "已按并发 " + options.concurrency + " 从生成邮箱库存启动注册";
      });
      this.commands.register("toggle-scheduled-generation", async () => {
        const enabled = Boolean(this.store.state.scheduledGeneration.enabled);
        if (enabled && !confirm("暂停每小时生成 5 个邮箱？已生成的邮箱会保留，且不会启动注册。")) {
          throw Object.assign(new Error(), { name: "AbortError" });
        }
        const data = await this.api.post("/api/scheduled-generation/config", { enabled: !enabled });
        this.store.patch({ scheduledGeneration: data });
        this.schedule("scheduled-generation", () => this.loadScheduledGeneration(), 1000);
        return enabled ? "定时生成已暂停" : "已开始计时，1 小时后生成第一批 5 个邮箱";
      });
      this.commands.register("set-registration-proxy-credential", async () => {
        const proxyLine = prompt("输入动态代理连接（host:port:username:password）。凭据只保存在本地，不会显示在日志中：", "");
        if (proxyLine === null) throw Object.assign(new Error(), { name: "AbortError" });
        if (!proxyLine.trim()) throw new Error("代理连接不能为空");
        const country = $("registrationProxyCountry").value || "NL";
        const data = await this.api.post("/api/registration-proxy/config", {
          proxyLine, country, enabled: true,
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
          await this.api.post("/api/registration/stop");
          await this.loadRegistrationTask();
        } else {
          await this.api.post("/api/browser/stop");
          await this.loadBrowserTask();
        }
        return "停止请求已发送";
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
        if (!confirm("永久删除邮箱 " + email + "？该操作无法撤销。")) throw Object.assign(new Error(), { name: "AbortError" });
        await this.api.post("/api/gpt-email/delete", { email });
        await this.loadAccounts();
        return "邮箱已删除";
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
        const concurrency = this.browserOptions().concurrency;
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
      ["cardSearch", "cardStatusFilter"].forEach((id) => {
        $(id).addEventListener(id === "cardSearch" ? "input" : "change", () => this.renderer.renderCardLinks(this.store.state));
      });
      $("verificationAccountSelect").addEventListener("change", (event) => {
        this.store.patch({ selectedVerificationEmail: event.target.value });
      });
      $("registrationProxyEnabled").addEventListener("change", async (event) => {
        try {
          const data = await this.api.post("/api/registration-proxy/config", {
            enabled: event.target.checked,
          });
          this.store.patch({ registrationProxy: data });
          this.toast(data.enabled ? "注册动态代理已启用" : "注册动态代理已关闭");
        } catch (error) {
          event.target.checked = !event.target.checked;
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
    }

    async start() {
      const savedTheme = localStorage.getItem("hme_theme") || "dark";
      this.applyTheme(savedTheme, false);
      this.store.subscribe((state) => this.render(state));
      this.bindEvents();
      this.router.start();
      const results = await Promise.allSettled([
        this.loadAccounts(), this.loadBrowserTask(), this.loadRegistrationTask(),
        this.loadVerificationTask(), this.loadInbox(), this.loadScheduledGeneration(), this.loadRegistrationProxy(),
      ]);
      const failure = results.find((result) => result.status === "rejected");
      if (failure) this.toast(failure.reason.message, "error");
    }
  }

  new WorkspaceController().start();
})();
