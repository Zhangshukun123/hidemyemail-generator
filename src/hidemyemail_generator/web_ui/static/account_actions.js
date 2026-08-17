(() => {
  "use strict";

  const PAYMENT_METHODS = {
    de_oaics_paypal: { country: "DE", amount: "0", singleProxy: true },
    paypal_us: { country: "US", amount: "", singleProxy: true },
    paypal_gb: { country: "GB", amount: "0", singleProxy: true },
  };

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

  class AccountActionModel {
    constructor(token = window.__HME_LOCAL_TOKEN__) {
      this.token = token;
    }

    async request(path, payload) {
      const headers = {};
      if (this.token) headers["X-Local-Token"] = this.token;
      if (payload !== undefined) headers["Content-Type"] = "application/json";
      const response = await fetch(path, {
        method: payload === undefined ? "GET" : "POST",
        headers,
        body: payload === undefined ? undefined : JSON.stringify(payload),
        cache: "no-store",
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        const error = new Error(data.error || `请求失败（HTTP ${response.status}）`);
        error.status = response.status;
        error.logs = Array.isArray(data.logs) ? data.logs : [];
        throw error;
      }
      return data;
    }

    async accounts() {
      const data = await this.request("/api/gpt-emails");
      return Array.isArray(data.items) ? data.items : [];
    }

    quickFlowConfig() {
      let saved = {};
      try {
        saved = JSON.parse(localStorage.getItem("hme_quick_flow_config_v1") || "{}");
      } catch (_error) {
        saved = {};
      }
      const supported = Object.keys(PAYMENT_METHODS);
      const storedMethod = String(
        saved.cardLinkMethod || localStorage.getItem("hme_quick_card_link_method") || "",
      );
      const method = supported.includes(storedMethod) ? storedMethod : "de_oaics_paypal";
      const mode = String(
        saved.extractionProxyMode || localStorage.getItem("hme_quick_extraction_proxy_mode") || "",
      );
      const attempts = Math.max(1, Math.min(100, Number(
        saved.extractionCount || localStorage.getItem("hme_quick_extraction_count") || 1,
      ) || 1));
      const policy = PAYMENT_METHODS[method];
      const firstCountry = policy.country;
      const secondCountry = policy.singleProxy ? firstCountry : String(
        saved.extractionSecondCountry ||
        localStorage.getItem("hme_quick_extraction_second_country") || firstCountry,
      ).toUpperCase();
      const targetAmount = policy.amount || String(
        saved.targetAmount || localStorage.getItem("hme_quick_card_link_target_amount") || "",
      ).trim();
      return { method, mode, attempts, firstCountry, secondCountry, targetAmount, policy };
    }

    cardLinkPayload(account, forceRetry = false) {
      const config = this.quickFlowConfig();
      return {
        email: account.email,
        method: config.method,
        country: config.policy.country,
        proxy_mode: config.mode,
        create_proxy_country: config.firstCountry,
        promotion_proxy_country: config.secondCountry,
        secondary_proxy_country: config.secondCountry,
        reuse_registration_proxy: false,
        independent_proxy_pair: !config.policy.singleProxy,
        use_secondary_proxy: false,
        promotion_proxy_choice: "first",
        target_amount: config.targetAmount,
        force_retry: Boolean(forceRetry),
        attempt_limit: config.attempts,
      };
    }

    async extractAndPay(account) {
      if (account.accountType === "plus") {
        throw new Error("该账号已是 Plus 套餐，无需再次提链支付");
      }
      const config = this.quickFlowConfig();
      const reusable = account.cardLink && account.cardLinkStatus === "generated" &&
        account.cardLinkMethod === config.method;
      let extraction = { url: account.cardLink || "", reused: true };
      if (!reusable) {
        extraction = await this.request(
          "/api/account/card-link",
          this.cardLinkPayload(
            account,
            account.cardLinkStatus === "cs_live" && account.cardLinkMethod === config.method,
          ),
        );
        if (extraction.cardLinkStatus === "cs_live" || !extraction.url) {
          throw new Error("提链结果仍为 cs_live，尚未生成可支付的 PayPal 链接");
        }
      }
      const payment = await this.request("/api/account/paypal-payment", {
        email: account.email,
      });
      return { extraction, payment };
    }

    bindPhone(email) {
      return this.request("/api/account/actions/bind-phone", { email });
    }

    bindPhoneStatus(email, logAfter = 0) {
      const query = new URLSearchParams({
        email: String(email || ""),
        log_after: String(Math.max(0, Number(logAfter) || 0)),
      });
      return this.request("/api/account/actions/bind-phone/status?" + query);
    }

    openBrowser(email, mode) {
      return this.request("/api/account/actions/open-browser", { email, mode });
    }

    async monitorPayment(jobId, onUpdate) {
      const target = String(jobId || "");
      if (!target) throw new Error("协议支付任务 ID 缺失");
      const deadline = Date.now() + 2 * 60 * 60 * 1000;
      let lastError = null;
      while (Date.now() < deadline) {
        try {
          const data = await this.request(
            "/api/account/paypal-payment/" + encodeURIComponent(target) +
            "?log_offset=0&log_after=0",
          );
          const job = data.job || {};
          if (onUpdate) onUpdate(job);
          if (window.PaymentOutcomeModel.classify(job).terminal) return job;
          lastError = null;
        } catch (error) {
          if (error.status === 404) throw error;
          lastError = error;
        }
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
      throw lastError || new Error("协议支付仍在运行，可在 PP 支付工作台继续查看");
    }
  }

  class AccountActionView {
    constructor() {
      this.toastTimer = 0;
    }

    button(label, operation, item, { disabled = false, style = "", title = "" } = {}) {
      return '<button type="button" class="button small ' + escapeHtml(style) +
        '" data-account-operation="' + escapeHtml(operation) + '" data-email="' +
        escapeHtml(item.email) + '" data-idle-label="' + escapeHtml(label) + '"' +
        (title ? ' title="' + escapeHtml(title) + '"' : "") +
        (disabled ? " disabled" : "") + ">" + escapeHtml(label) + "</button>";
    }

    actions(item) {
      const hasCookies = Boolean(item.hasCookies);
      const hasSession = item.sessionStatus === "ready";
      const isPlus = item.accountType === "plus";
      const paymentRunning = Boolean(item.accountPaymentRunning);
      const phoneStatus = String(
        item.plusPhoneBindingStatus || item.plusCodexStatus || "",
      ).toLowerCase();
      const phoneBound = Boolean(
        item.plusPhoneBound || item.plusSmsVerified || phoneStatus === "completed",
      );
      const phoneRunning = phoneStatus === "running";
      const phoneFailed = phoneStatus === "failed";
      const plusReady = isPlus;
      const phoneLabel = phoneBound ? "手机号已绑定"
        : phoneRunning ? "手机号绑定中" : phoneFailed ? "重新绑定手机号" : "绑定手机号";
      const phoneTitle = phoneBound ? "该账号已完成 Plus 手机验证"
        : phoneRunning ? "Roxy 登录与协议接码正在后台执行"
          : phoneFailed && plusReady ? "上次绑定失败，可直接使用已保存的 Cookie 重新尝试"
            : plusReady ? "直接使用已保存的 Cookie 登录，并通过已配置接码平台自动绑定手机号"
              : "需先确认账号已升级为 Plus";
      return [
        this.button(isPlus ? "无需提链支付" : paymentRunning ? "提链支付中" : "提链支付", "extract-payment", item, {
          disabled: isPlus || paymentRunning || !hasCookies || !hasSession,
          style: "primary",
          title: isPlus ? "该账号已是 Plus 套餐" : paymentRunning ? "该账号已有提链支付任务正在执行" : hasCookies && hasSession
            ? "按一键流程配置提取 PayPal 链接并启动协议支付"
            : "需要可用 Session、AT 和 Cookie",
        }),
        this.button(phoneLabel, "bind-phone", item, {
          disabled: phoneBound || phoneRunning || !plusReady,
          title: phoneTitle,
        }),
        this.button("打开谷歌无痕浏览器", "open-chrome", item, {
          disabled: !hasCookies,
          title: hasCookies ? "打开 Google Chrome 无痕窗口并注入账号 Cookie" : "尚未保存 Cookie",
        }),
        this.button("打开 Roxy 浏览器", "open-roxy", item, {
          disabled: !hasCookies,
          title: hasCookies ? "打开已配置 Roxy 环境并注入账号 Cookie" : "尚未保存 Cookie",
        }),
      ].join("");
    }

    renderPhoneLogs(container) {
      const detail = container.closest(".account-detail");
      if (!detail) return;
      const panel = [...detail.children].find(
        (child) => child.classList?.contains("phone-binding-log-panel"),
      );
      if (panel) panel.remove();
    }

    decorate(accountMap, replace = false, phoneSnapshots = new Map()) {
      document.querySelectorAll(".account-detail-row .credential-actions").forEach((container) => {
        const existing = [...container.querySelectorAll("[data-account-operation]")];
        const anchor = container.querySelector("[data-email]");
        const email = String(anchor?.dataset.email || "").trim().toLowerCase();
        const item = accountMap.get(email);
        if (!item) return;
        if (existing.length && !replace) {
          this.renderPhoneLogs(container, item, phoneSnapshots.get(email));
          return;
        }
        existing.forEach((button) => button.remove());
        const template = document.createElement("template");
        template.innerHTML = this.actions(item);
        const deleteButton = container.querySelector('[data-action="delete-email"]');
        container.insertBefore(template.content, deleteButton || null);
        this.renderPhoneLogs(container, item, phoneSnapshots.get(email));
      });
    }

    busy(button, label) {
      button.disabled = true;
      button.textContent = label;
    }

    restore(button) {
      button.disabled = false;
      button.textContent = button.dataset.idleLabel || button.textContent;
    }

    notify(message, type = "") {
      const toast = document.getElementById("toast");
      if (!toast) return;
      toast.textContent = message;
      toast.className = "toast " + type;
      requestAnimationFrame(() => toast.classList.add("show"));
      clearTimeout(this.toastTimer);
      this.toastTimer = setTimeout(() => toast.classList.remove("show"), 3600);
    }

    openPaymentWorkspace(data) {
      const payment = data.payment || {};
      const jobId = String(payment.job?.id || "");
      const nav = document.querySelector('[data-route="pp-payment"]');
      if (nav) nav.click();
      const frame = document.getElementById("paypalPaymentFrame");
      if (!frame) return;
      const url = new URL(payment.url || "/paypal-pay/", location.origin);
      url.searchParams.set("embedded", "1");
      url.searchParams.set("theme", document.documentElement.dataset.theme || "dark");
      if (jobId) url.searchParams.set("job", jobId);
      frame.src = url.origin === location.origin ? url.pathname + url.search + url.hash : url.href;
      frame.dataset.loaded = "1";
    }
  }

  class AccountActionPresenter {
    constructor(model = new AccountActionModel(), view = new AccountActionView()) {
      this.model = model;
      this.view = view;
      this.accounts = new Map();
      this.paymentInFlight = new Set();
      this.phoneSnapshots = new Map();
      this.phoneMonitors = new Map();
      this.renderQueued = false;
      this.observer = new MutationObserver(() => this.queueRender());
    }

    async refresh() {
      const accounts = await this.model.accounts();
      this.accounts = new Map(accounts.map((item) => {
        const email = String(item.email || "").trim().toLowerCase();
        return [email, {
          ...item,
          accountPaymentRunning: Boolean(
            item.accountPaymentRunning || this.paymentInFlight.has(email),
          ),
        }];
      }));
      this.view.decorate(this.accounts, true, this.phoneSnapshots);
      this.accounts.forEach((item, email) => {
        const status = String(
          item.plusPhoneBindingStatus || item.plusCodexStatus || "",
        ).toLowerCase();
        if (status === "running" || (
          ["completed", "failed"].includes(status) && !this.phoneSnapshots.has(email)
        )) {
          void this.monitorPhoneBinding(email);
        }
      });
    }

    queueRender() {
      if (this.renderQueued) return;
      this.renderQueued = true;
      requestAnimationFrame(() => {
        this.renderQueued = false;
        this.view.decorate(this.accounts, false, this.phoneSnapshots);
      });
    }

    account(email) {
      const item = this.accounts.get(String(email || "").trim().toLowerCase());
      if (!item) throw new Error("账号不存在，请刷新后重试");
      return item;
    }

    mergePhoneSnapshot(email, snapshot) {
      const target = String(email || "").trim().toLowerCase();
      const previous = this.phoneSnapshots.get(target) || { logs: [], logSequence: 0 };
      const keyed = new Map();
      [...(previous.logs || []), ...(snapshot.logs || [])].forEach((log) => {
        const sequence = Number(log?.sequence || 0);
        if (sequence > 0) keyed.set(sequence, log);
      });
      const merged = {
        ...previous,
        ...snapshot,
        logs: [...keyed.values()].sort(
          (left, right) => Number(left.sequence || 0) - Number(right.sequence || 0),
        ).slice(-200),
        logSequence: Math.max(
          Number(previous.logSequence || 0),
          Number(snapshot.logSequence || 0),
        ),
      };
      this.phoneSnapshots.set(target, merged);
      window.dispatchEvent(new CustomEvent("hme:phone-binding-snapshot", {
        detail: { email: target, snapshot: merged },
      }));
      const item = this.accounts.get(target);
      if (item) {
        this.accounts.set(target, {
          ...item,
          plusPhoneBindingStatus: merged.status,
          plusCodexStatus: merged.status,
          plusPhoneBound: merged.status === "completed" || Boolean(merged.smsVerified),
          plusSmsVerified: Boolean(merged.smsVerified),
        });
      }
      this.view.decorate(this.accounts, true, this.phoneSnapshots);
      return merged;
    }

    monitorPhoneBinding(email) {
      const target = String(email || "").trim().toLowerCase();
      if (this.phoneMonitors.has(target)) return this.phoneMonitors.get(target);
      const monitor = (async () => {
        const deadline = Date.now() + 2 * 60 * 60 * 1000;
        while (Date.now() < deadline) {
          try {
            const previous = this.phoneSnapshots.get(target) || {};
            const snapshot = await this.model.bindPhoneStatus(
              target,
              Number(previous.logSequence || 0),
            );
            const merged = this.mergePhoneSnapshot(target, snapshot);
            if (["completed", "failed"].includes(String(merged.status || ""))) {
              await this.refresh().catch(() => {});
              return merged;
            }
          } catch (_error) {
            // Status reads are transient; keep the live monitor attached.
          }
          await new Promise((resolve) => setTimeout(resolve, 800));
        }
        return this.phoneSnapshots.get(target) || null;
      })().finally(() => this.phoneMonitors.delete(target));
      this.phoneMonitors.set(target, monitor);
      return monitor;
    }

    async execute(button) {
      const operation = String(button.dataset.accountOperation || "");
      const email = String(button.dataset.email || "").trim().toLowerCase();
      const item = this.account(email);
      const phoneStatus = String(
        item.plusPhoneBindingStatus || item.plusCodexStatus || "",
      ).toLowerCase();
      const phoneBound = Boolean(
        item.plusPhoneBound || item.plusSmsVerified || phoneStatus === "completed",
      );
      if (operation === "extract-payment" && item.accountType === "plus") {
        this.view.notify("该账号已是 Plus 套餐，无需再次提链支付", "warning");
        return;
      }
      if (operation === "bind-phone" && phoneBound) {
        this.view.notify("该账号手机号已绑定，无需重复操作", "warning");
        return;
      }
      if (operation === "bind-phone" && phoneStatus === "running") {
        this.view.notify("该账号手机号绑定任务正在执行", "warning");
        return;
      }
      if (operation === "extract-payment" && this.paymentInFlight.has(email)) {
        this.view.notify("该账号已有提链支付任务正在执行", "error");
        return;
      }
      const busyLabels = {
        "extract-payment": "正在提链支付…",
        "bind-phone": "正在启动绑定…",
        "open-chrome": "正在打开谷歌无痕…",
        "open-roxy": "正在打开 Roxy…",
      };
      this.view.busy(button, busyLabels[operation] || "正在执行…");
      let keepPaymentBusy = false;
      if (operation === "extract-payment") {
        this.paymentInFlight.add(email);
        this.accounts.set(email, { ...item, accountPaymentRunning: true });
        this.view.decorate(this.accounts, true, this.phoneSnapshots);
      }
      try {
        if (operation === "extract-payment") {
          const result = await this.model.extractAndPay(item);
          this.view.openPaymentWorkspace(result);
          const payment = result.payment || {};
          this.view.notify(
            "提链支付已启动：Cookie " + Number(payment.cookieCount || 0) + " 条 · " +
            String(payment.smsProviderLabel || payment.smsProvider || "接码平台"),
          );
          keepPaymentBusy = true;
          void this.monitorPayment(payment, email);
        } else if (operation === "bind-phone") {
          const result = await this.model.bindPhone(email);
          this.mergePhoneSnapshot(email, result);
          this.view.notify(result.message || "手机号绑定任务已启动");
          void this.monitorPhoneBinding(email);
        } else if (operation === "open-chrome") {
          const result = await this.model.openBrowser(email, "chrome");
          this.view.notify(result.message || "Google Chrome 无痕窗口已打开");
        } else if (operation === "open-roxy") {
          const result = await this.model.openBrowser(email, "roxy");
          this.view.notify(result.message || "Roxy 浏览器已打开");
        } else {
          throw new Error("账号操作不存在");
        }
        await this.refresh().catch(() => {});
      } catch (error) {
        if (operation === "extract-payment") {
          this.paymentInFlight.delete(email);
          await this.refresh().catch(() => {});
        }
        this.view.notify(error.message || "账号操作失败", "error");
      } finally {
        if (!keepPaymentBusy) this.view.restore(button);
      }
    }

    async monitorPayment(payment, email) {
      const jobId = String(payment.job?.id || "");
      if (!jobId) {
        this.paymentInFlight.delete(email);
        this.view.notify("协议支付任务 ID 缺失", "error");
        await this.refresh().catch(() => {});
        return;
      }
      try {
        const job = await this.model.monitorPayment(jobId);
        const outcome = window.PaymentOutcomeModel.classify(job);
        if (!outcome.paymentSucceeded) {
          throw new Error(outcome.paymentError || "协议支付未完成");
        }
        const delivery = job.plus_codex || outcome.confirmation.plus_codex || {};
        if (outcome.deliveryError) {
          this.view.notify(
            "协议支付与 Plus 确认已完成；手机号/Codex 后处理失败：" +
            outcome.deliveryError,
            "warning",
          );
        } else if (outcome.confirmationError) {
          this.view.notify(
            "协议支付成功；AT/Plus 后置校验失败：" + outcome.confirmationError,
            "warning",
          );
        } else {
          this.view.notify(delivery.status === "completed"
            ? "提链支付与手机号绑定已完成"
            : delivery.status === "running"
              ? "提链支付已完成，手机号绑定正在自动执行"
              : outcome.plusConfirmed
                ? "提链支付已完成并确认 Plus"
                : "协议支付已完成");
        }
      } catch (error) {
        this.view.notify(
          "支付任务 " + email + "：" + (error.message || "状态读取失败"),
          "error",
        );
      } finally {
        this.paymentInFlight.delete(email);
        await this.refresh().catch(() => {});
      }
    }

    start() {
      document.addEventListener("click", (event) => {
        const button = event.target.closest("[data-account-operation]");
        if (!button) return;
        event.preventDefault();
        event.stopPropagation();
        if (!button.disabled) void this.execute(button);
      });
      this.observer.observe(document.body, { childList: true, subtree: true });
      void this.refresh().catch(() => {});
    }
  }

  window.AccountActionModel = AccountActionModel;
  window.AccountActionView = AccountActionView;
  window.AccountActionPresenter = AccountActionPresenter;
  new AccountActionPresenter().start();
})();
