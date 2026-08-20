(() => {
  "use strict";

  class CatchAllMailboxModel {
    constructor(token) {
      this.token = token || "";
      this.state = { configured: false, domain: "cclgmail.com", domains: [] };
    }

    async request(path, options = {}) {
      const headers = { ...(options.headers || {}) };
      if (this.token) headers["X-Local-Token"] = this.token;
      if (options.body) headers["Content-Type"] = "application/json";
      const response = await fetch(path, { ...options, headers, cache: "no-store" });
      const data = await response.json().catch(() => ({ ok: false, error: "服务响应无效" }));
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || "邮箱域名配置失败");
      }
      this.state = data;
      return data;
    }

    load() {
      return this.request("/api/zkgmail/status");
    }

    select(domain) {
      return this.request("/api/zkgmail/config", {
        method: "POST",
        body: JSON.stringify({ domain }),
      });
    }
  }

  class CatchAllMailboxView {
    constructor() {
      this.select = document.getElementById("catchAllDomainSelect");
      this.addButton = document.getElementById("addCatchAllDomain");
      this.status = document.getElementById("zkgmailStatus");
      this.providerSelects = [
        document.getElementById("registrationEmailProvider"),
        document.getElementById("quickRegistrationProvider"),
      ].filter(Boolean);
    }

    get available() {
      return Boolean(this.select && this.addButton && this.status);
    }

    setBusy(busy) {
      this.select.disabled = busy;
      this.addButton.disabled = busy;
      this.providerSelects.forEach((select) => { select.disabled = busy; });
    }

    render(state) {
      const domains = Array.isArray(state.domains) ? [...state.domains] : [];
      domains.sort((left, right) =>
        left === "zkgmail.com" ? -1 : right === "zkgmail.com" ? 1 : left.localeCompare(right));
      this.select.replaceChildren(...domains.map((domain) => {
        const option = document.createElement("option");
        option.value = domain;
        option.textContent = domain;
        return option;
      }));
      this.select.value = state.domain || domains[0] || "";
      this.providerSelects.forEach((select) => {
        const selectedOption = select.selectedOptions[0];
        const selectedCatchAll = selectedOption?.dataset.catchallDomain || "";
        const previousValue = select.value;
        select.querySelectorAll("option[data-catchall-domain]").forEach((option) => option.remove());
        const insertionPoint = select.querySelector('option[value="gmail"]');
        domains.forEach((domain) => {
          const option = document.createElement("option");
          option.value = "zkgmail";
          option.dataset.catchallDomain = domain;
          option.textContent = domain + " · QQ 接码";
          select.insertBefore(option, insertionPoint);
        });
        if (selectedCatchAll) {
          const activeOption = [...select.options].find(
            (option) => option.dataset.catchallDomain === state.domain,
          );
          if (activeOption) activeOption.selected = true;
        } else {
          select.value = previousValue;
        }
      });
      this.status.className = "badge " + (state.configured ? "success" : "warning");
      this.status.textContent = state.configured
        ? "QQ 自动取码 · " + state.domain + " · " + (state.forwardAccount || "")
        : state.domain + " 已添加 · 待配置 QQ 授权码";
    }

    showError(message) {
      this.status.className = "badge error";
      this.status.textContent = message;
    }

    promptDomain() {
      return window.prompt(
        "输入要增加的邮箱域名（例如 cclgmail.com）。该域名的邮件需已转发到 352121354@qq.com：",
        "",
      );
    }

    bindProviderDomain(handler) {
      this.providerSelects.forEach((select) => {
        select.addEventListener("change", () => {
          const domain = select.selectedOptions[0]?.dataset.catchallDomain || "";
          if (domain) handler(domain);
        });
      });
    }
  }

  class CatchAllMailboxPresenter {
    constructor(model, view) {
      this.model = model;
      this.view = view;
    }

    async update(domain) {
      const normalized = String(domain || "").trim().toLowerCase();
      if (!normalized) return;
      this.view.setBusy(true);
      try {
        this.view.render(await this.model.select(normalized));
      } catch (error) {
        this.view.showError(error.message || String(error));
        await this.reload(false);
      } finally {
        this.view.setBusy(false);
      }
    }

    async reload(showError = true) {
      try {
        this.view.render(await this.model.load());
      } catch (error) {
        if (showError) this.view.showError(error.message || String(error));
      }
    }

    mount() {
      if (!this.view.available) return;
      this.view.select.addEventListener("change", (event) => this.update(event.target.value));
      this.view.bindProviderDomain((domain) => this.update(domain));
      this.view.addButton.addEventListener("click", () => {
        const domain = this.view.promptDomain();
        if (domain !== null) this.update(domain);
      });
      this.reload();
    }
  }

  new CatchAllMailboxPresenter(
    new CatchAllMailboxModel(window.__HME_LOCAL_TOKEN__),
    new CatchAllMailboxView(),
  ).mount();
})();
