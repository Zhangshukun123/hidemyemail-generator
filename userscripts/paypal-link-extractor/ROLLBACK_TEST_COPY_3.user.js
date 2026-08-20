// ==UserScript==
// @name         PayPal 提链结果提取器
// @namespace    local.hidemyemail
// @version      1.0.0
// @description  自动提取 pp.uplw.uno 单条/批量任务中的 PayPal 结果链接并复制。
// @author       Codex
// @match        https://pp.uplw.uno/*
// @grant        GM_setClipboard
// @grant        GM_registerMenuCommand
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  const RESULT_FIELDS = Object.freeze([
    "paypal_approve_url",
    "final_redirect_url",
    "paypal_return_url",
    "approval_url",
    "return_url",
  ]);
  const STORAGE_KEY = "paypal-link-extractor:mvp:v1";
  const MAX_HISTORY = 50;

  class LinkModel {
    constructor(baseUrl = "https://pp.uplw.uno/", initialState = {}) {
      this.baseUrl = new URL(baseUrl);
      this.history = Array.isArray(initialState.history)
        ? initialState.history.filter((item) => item && item.url).slice(0, MAX_HISTORY)
        : [];
      this.autoCopy = initialState.autoCopy !== false;
    }

    normalize(candidate) {
      let value = String(candidate || "")
        .trim()
        .replaceAll("&amp;", "&")
        .replaceAll("\\u0026", "&")
        .replace(/^[\s'"`(<\[]+|[\s'"`)>\],.;。；，！？、]+$/g, "");
      if (!value) return "";
      try {
        const url = new URL(value, this.baseUrl);
        if (!/^https?:$/.test(url.protocol)) return "";
        if (
          url.origin === this.baseUrl.origin &&
          (url.pathname === "/" || url.pathname.startsWith("/api/") || url.pathname.startsWith("/static/"))
        ) {
          return "";
        }
        return url.href;
      } catch (_) {
        return "";
      }
    }

    extractUrlsFromText(text) {
      const value = String(text || "").replaceAll("&amp;", "&").replaceAll("\\u0026", "&");
      const matches = value.match(/https?:\/\/[^\s<>"'`]+/gi) || [];
      return matches.map((item) => this.normalize(item)).filter(Boolean);
    }

    extractStructured(payload, source = "structured") {
      const found = [];
      const visit = (value, path, seen) => {
        if (value == null) return;
        if (typeof value === "string") {
          for (const url of this.extractUrlsFromText(value)) {
            found.push({ url, source, field: path || "text" });
          }
          return;
        }
        if (typeof value !== "object" || seen.has(value)) return;
        seen.add(value);
        for (const field of RESULT_FIELDS) {
          if (!(field in value)) continue;
          const url = this.normalize(value[field]);
          if (url) found.push({ url, source, field: path ? `${path}.${field}` : field });
        }
        for (const [key, child] of Object.entries(value)) {
          if (RESULT_FIELDS.includes(key)) continue;
          visit(child, path ? `${path}.${key}` : key, seen);
        }
      };
      visit(payload, "", new WeakSet());
      return this.uniqueByUrl(found);
    }

    uniqueByUrl(items) {
      const seen = new Set();
      return items.filter((item) => {
        if (!item.url || seen.has(item.url)) return false;
        seen.add(item.url);
        return true;
      });
    }

    classify(url) {
      const parsed = new URL(url);
      const paypalHost = /(^|\.)paypal\.com$/i.test(parsed.hostname);
      if (paypalHost && (parsed.searchParams.has("ba_token") || /\/agreements\/approve/i.test(parsed.pathname))) {
        return "PayPal BA";
      }
      if (paypalHost) return "PayPal";
      return "回跳链接";
    }

    capture(candidates) {
      const added = [];
      const knownUrls = new Set(this.history.map((item) => item.url));
      for (const candidate of candidates) {
        const url = this.normalize(candidate.url);
        if (!url) continue;
        if (knownUrls.has(url)) continue;
        const item = {
          url,
          source: candidate.source || "页面",
          field: candidate.field || "href",
          kind: this.classify(url),
          capturedAt: new Date().toISOString(),
        };
        added.push(item);
        knownUrls.add(url);
      }
      this.history = [...added, ...this.history].slice(0, MAX_HISTORY);
      return added;
    }

    latest() {
      return this.history[0] || null;
    }

    clear() {
      this.history = [];
    }

    toJSON() {
      return { autoCopy: this.autoCopy, history: this.history };
    }
  }

  class LinkView {
    constructor(root = document) {
      this.root = root;
      this.host = null;
      this.shadow = null;
      this.observer = null;
      this.scanTimer = 0;
    }

    mount(handlers) {
      this.host = this.root.createElement("div");
      this.host.id = "paypal-link-extractor-host";
      this.shadow = this.host.attachShadow({ mode: "open" });
      this.shadow.innerHTML = `
        <style>
          :host{all:initial}*{box-sizing:border-box}.panel{position:fixed;right:18px;bottom:18px;z-index:2147483647;width:360px;padding:14px;border:1px solid #d8e5ff;border-radius:16px;background:#fff;box-shadow:0 18px 55px rgba(27,62,126,.24);font:13px/1.45 "Noto Sans SC",system-ui,sans-serif;color:#16233d}.head{display:flex;align-items:center;justify-content:space-between;gap:8px}.head strong{font-size:15px}.count{padding:2px 8px;border-radius:999px;background:#edf4ff;color:#1e5fc7}.latest{display:block;margin:10px 0;padding:10px;border-radius:10px;background:#f5f8fe;color:#1255b4;text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.empty{color:#6d7890}.actions{display:flex;flex-wrap:wrap;gap:7px}.actions button{border:1px solid #c9d8f4;border-radius:8px;padding:6px 9px;background:#fff;color:#28446e;cursor:pointer}.actions button.primary{border-color:#1769e0;background:#1769e0;color:#fff}.history{max-height:150px;margin:10px 0 0;padding:0;overflow:auto;list-style:none}.history li{display:grid;grid-template-columns:72px 1fr;gap:7px;padding:6px 0;border-top:1px solid #eef2f8}.history span{color:#6b7590}.history a{color:#254f91;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.status{min-height:18px;margin-top:8px;color:#48813d}.off{color:#9a5d1c}
        </style>
        <section class="panel" aria-label="PayPal 提链结果提取器">
          <div class="head"><strong>PayPal 提链结果</strong><span class="count">0 条</span></div>
          <a class="latest empty" href="#">等待页面生成结果链接…</a>
          <div class="actions">
            <button class="primary" data-action="copy">复制最新</button>
            <button data-action="scan">立即提取</button>
            <button data-action="copy-all">复制全部</button>
            <button data-action="auto">自动复制：开</button>
            <button data-action="clear">清空</button>
          </div>
          <ul class="history"></ul>
          <div class="status" role="status"></div>
        </section>`;
      this.root.body.appendChild(this.host);
      this.shadow.addEventListener("click", (event) => {
        const action = event.target.closest("[data-action]")?.dataset.action;
        if (action && handlers[action]) handlers[action]();
      });
    }

    observe(onChange) {
      this.observer = new MutationObserver(() => {
        clearTimeout(this.scanTimer);
        this.scanTimer = setTimeout(onChange, 80);
      });
      this.observer.observe(this.root.body, {
        subtree: true,
        childList: true,
        characterData: true,
        attributes: true,
        attributeFilter: ["href", "hidden", "data-paypal-approve-url", "data-final-redirect-url", "data-paypal-return-url"],
      });
    }

    collectCandidates(model) {
      const candidates = [];
      const direct = this.root.querySelector("#resultReturn");
      if (direct) {
        candidates.push({ url: direct.getAttribute("href"), source: "单条结果卡片", field: "#resultReturn.href" });
        candidates.push({ url: direct.textContent, source: "单条结果卡片", field: "#resultReturn.text" });
      }
      const fieldMap = {
        "data-paypal-approve-url": "paypal_approve_url",
        "data-final-redirect-url": "final_redirect_url",
        "data-paypal-return-url": "paypal_return_url",
      };
      for (const [attribute, field] of Object.entries(fieldMap)) {
        for (const node of this.root.querySelectorAll(`[${attribute}]`)) {
          candidates.push({ url: node.getAttribute(attribute), source: "结果字段", field });
        }
      }
      const resultRoots = this.root.querySelectorAll("#singleResult, #batchJobs, #batchLog, .result-link, .job-row");
      for (const resultRoot of resultRoots) {
        for (const anchor of resultRoot.querySelectorAll("a[href]")) {
          candidates.push({ url: anchor.getAttribute("href"), source: "结果区域", field: "href" });
        }
        for (const url of model.extractUrlsFromText(resultRoot.textContent)) {
          candidates.push({ url, source: "结果区域文本", field: "text" });
        }
      }
      return candidates;
    }

    render(model) {
      const latest = model.latest();
      const latestNode = this.shadow.querySelector(".latest");
      this.shadow.querySelector(".count").textContent = `${model.history.length} 条`;
      latestNode.textContent = latest ? latest.url : "等待页面生成结果链接…";
      latestNode.href = latest ? latest.url : "#";
      latestNode.target = latest ? "_blank" : "";
      latestNode.classList.toggle("empty", !latest);
      this.shadow.querySelector('[data-action="auto"]').textContent = `自动复制：${model.autoCopy ? "开" : "关"}`;
      const history = this.shadow.querySelector(".history");
      history.replaceChildren();
      for (const item of model.history.slice(0, 8)) {
        const row = this.root.createElement("li");
        const kind = this.root.createElement("span");
        const link = this.root.createElement("a");
        kind.textContent = item.kind;
        link.href = item.url;
        link.target = "_blank";
        link.rel = "noopener";
        link.textContent = item.url;
        link.title = `${item.source} · ${item.field}`;
        row.append(kind, link);
        history.appendChild(row);
      }
    }

    status(message, isOff = false) {
      const node = this.shadow.querySelector(".status");
      node.textContent = message;
      node.classList.toggle("off", isOff);
    }
  }

  class LinkPresenter {
    constructor(model, view, storage = window.localStorage) {
      this.model = model;
      this.view = view;
      this.storage = storage;
    }

    start() {
      this.view.mount({
        copy: () => this.copyLatest(),
        scan: () => this.scan("手动提取"),
        "copy-all": () => this.copyAll(),
        auto: () => this.toggleAutoCopy(),
        clear: () => this.clear(),
      });
      this.view.observe(() => this.scan("页面更新"));
      document.addEventListener("paypal-link-extractor:capture", (event) => {
        const items = this.model.extractStructured(event.detail, "自定义事件");
        this.accept(items);
      });
      this.scan("初始页面");
      this.registerMenu();
    }

    scan(source) {
      const candidates = this.view.collectCandidates(this.model).map((item) => ({ ...item, source: item.source || source }));
      this.accept(candidates);
    }

    accept(candidates) {
      const added = this.model.capture(candidates);
      this.save();
      this.view.render(this.model);
      if (added.length && this.model.autoCopy) {
        this.writeClipboard(added[0].url);
        this.view.status(`已自动提取并复制：${added[0].kind}`);
      }
      return added;
    }

    copyLatest() {
      const latest = this.model.latest();
      if (!latest) return this.view.status("还没有可复制的结果链接", true);
      this.writeClipboard(latest.url);
      this.view.status("最新链接已复制");
    }

    copyAll() {
      if (!this.model.history.length) return this.view.status("还没有可复制的结果链接", true);
      this.writeClipboard(this.model.history.map((item) => item.url).join("\n"));
      this.view.status(`已复制 ${this.model.history.length} 条链接`);
    }

    toggleAutoCopy() {
      this.model.autoCopy = !this.model.autoCopy;
      this.save();
      this.view.render(this.model);
      this.view.status(`自动复制已${this.model.autoCopy ? "开启" : "关闭"}`);
    }

    clear() {
      this.model.clear();
      this.save();
      this.view.render(this.model);
      this.view.status("历史已清空");
    }

    writeClipboard(value) {
      if (typeof GM_setClipboard === "function") GM_setClipboard(value, "text");
      else navigator.clipboard?.writeText(value);
    }

    save() {
      try {
        this.storage.setItem(STORAGE_KEY, JSON.stringify(this.model.toJSON()));
      } catch (_) {
        // Storage is optional; extraction continues in memory.
      }
    }

    registerMenu() {
      if (typeof GM_registerMenuCommand !== "function") return;
      GM_registerMenuCommand("提取当前结果", () => this.scan("菜单"));
      GM_registerMenuCommand("复制最新结果", () => this.copyLatest());
      GM_registerMenuCommand("清空提取历史", () => this.clear());
    }
  }

  function loadState(storage = window.localStorage) {
    try {
      return JSON.parse(storage.getItem(STORAGE_KEY) || "{}");
    } catch (_) {
      return {};
    }
  }

  const testApi = { RESULT_FIELDS, LinkModel, LinkView, LinkPresenter, loadState };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = testApi;
    return;
  }

  const model = new LinkModel(window.location.href, loadState());
  const view = new LinkView(document);
  new LinkPresenter(model, view).start();
})();

