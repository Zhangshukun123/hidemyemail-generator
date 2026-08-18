(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;" })[char]);
  let token = localStorage.getItem("protocol_server_token") || "";
  let timer = 0;
  let activePool = "all";
  const refreshingOffers = new Set();
  const selectedOfferCountries = () => Array.from($("offerCountries").selectedOptions).map((item) => item.value);

  function toast(message, error = false) {
    $("toast").textContent = message;
    $("toast").className = "toast show" + (error ? " error" : "");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => $("toast").classList.remove("show"), 3500);
  }
  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Authorization":"Bearer " + token, "Content-Type":"application/json", ...(options.headers || {}) },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) throw new Error(data.error || "HTTP " + response.status);
    return data;
  }
  function render(data) {
    const service = data.service || {};
    const task = data.task || {};
    const pool = data.offerPool || {};
    const runtime = task.runtime || {};
    $("runtime").className = "badge " + (runtime.available ? "ok" : "bad");
    $("runtime").textContent = runtime.available ? "协议运行时就绪" : "运行时缺失";
    $("taskStatus").textContent = service.status || "idle";
    $("taskMessage").textContent = service.message || "等待开始";
    $("verificationInfo").textContent = "验证 " + Number(service.verificationVerified || 0) + " / " + Number(service.verificationCompleted || 0);
    $("offerCount").textContent = Number(pool.offerCount || 0);
    $("noOfferCount").textContent = Number(pool.noOfferCount || 0);
    $("pendingCount").textContent = Number(pool.pendingCount || 0);
    const total = Number(task.total || 0), completed = Number(task.completed || 0);
    $("progress").value = total ? Math.round(completed / total * 100) : 0;
    $("progressText").textContent = completed + " / " + total;
    const proxy = data.registrationProxy || {};
    $("route").textContent = proxy.strategy === "server_kookeey" ? "Kookeey · " + (proxy.countryLabel || proxy.country || "") : (proxy.currentRoute || proxy.nextRoute || "clash") + " · 下一出口 " + (proxy.nextRoute || "clash");
    const countries = Array.isArray(data.registrationCountries) ? data.registrationCountries : [];
    if (!$('registrationCountry').options.length && countries.length) {
      $('registrationCountry').innerHTML = countries.map((item) => '<option value="' + escapeHtml(item.code) + '">' + escapeHtml(item.label) + ' · ' + escapeHtml(item.code) + '</option>').join("");
      $('registrationCountry').value = service.registrationCountry || "JP";
    }
    if (!$('offerCountries').options.length && countries.length) {
      const selected = new Set(Array.isArray(service.offerCountries) && service.offerCountries.length ? service.offerCountries : ["US", "GB", "DE"]);
      $('offerCountries').innerHTML = countries.map((item) => '<option value="' + escapeHtml(item.code) + '"' + (selected.has(item.code) ? ' selected' : '') + '>' + escapeHtml(item.label) + ' · ' + escapeHtml(item.code) + '</option>').join("");
    }
    $("start").disabled = Boolean(service.running);
    $("stop").disabled = !service.running;
    const accounts = Array.isArray(task.accounts) ? task.accounts : [];
    $("accounts").className = "list" + (accounts.length ? "" : " empty");
    $("accounts").innerHTML = accounts.length ? accounts.map((item) => '<div class="row"><div><strong>' + escapeHtml(item.email) + '</strong><small>' + escapeHtml(item.message || item.stage || "") + '</small></div><span class="badge">' + escapeHtml(item.status || "queued") + '</span></div>').join("") : "暂无运行账号";
    const items = (pool.items || []).filter((item) => activePool === "all" || (activePool === "pending" ? !item.pool : item.pool === activePool));
    $("pool").className = "list" + (items.length ? "" : " empty");
    $("pool").innerHTML = items.length ? items.map((item) => { const route = [item.registrationProxyMode, item.registrationCountry, item.registrationIp].filter(Boolean).join(" · "); const evidence = (item.checkoutEvidence || []).map((value) => { const requested = value.requestedCountry || value.exitCountry; const path = [requested, value.exitCountry && value.exitCountry !== requested ? '出口 ' + value.exitCountry : '', value.checkoutCountry ? '账单 ' + value.checkoutCountry : ''].filter(Boolean).join(' → '); return path + '：PayPal ' + (value.paypalAvailable ? '有' : '无') + ' · ' + (value.amountMinor ?? '?') + ' ' + (value.currency || '') + (value.deFallback ? ' · DE回退' : ''); }).join('；'); const refreshing = refreshingOffers.has(item.email); return '<div class="row"><div><strong>' + escapeHtml(item.email) + '</strong><small>' + escapeHtml(item.detail || "") + '</small>' + (evidence ? '<small>Checkout：' + escapeHtml(evidence) + '</small>' : '') + '<small>检查时间：' + escapeHtml(item.checkedAt || "-") + '</small>' + (route ? '<small>注册出口：' + escapeHtml(route) + '</small>' : '') + '</div><div class="offer-actions"><div><span class="badge ' + (item.pool === "offer" ? "ok" : "") + '">' + escapeHtml(item.pool || "pending") + '</span><button class="icon-button refresh-offer' + (refreshing ? ' spinning' : '') + '" data-email="' + escapeHtml(item.email) + '" title="重新判断优惠" aria-label="重新判断 ' + escapeHtml(item.email) + ' 的优惠"' + (refreshing ? ' disabled' : '') + '>↻</button></div>' + (item.checkoutUrl ? '<small><a target="_blank" rel="noopener noreferrer" href="' + escapeHtml(item.checkoutUrl) + '">打开 Checkout</a></small>' : "") + '</div></div>'; }).join("") : "暂无优惠记录";
    const history = Array.isArray(data.runHistory) ? data.runHistory : [];
    $("runHistory").className = "list" + (history.length ? "" : " empty");
    $("runHistory").innerHTML = history.length ? history.map((item) => '<div class="row"><div><strong>' + escapeHtml(item.startedAt || item.id || "任务") + '</strong><small>请求 ' + Number(item.requested || 0) + ' · 领取 ' + Number(item.acquired || 0) + ' · 并发 ' + Number(item.concurrency || 1) + (item.useRegistrationKookeey ? ' · Kookeey ' + escapeHtml(item.registrationCountry || '') : ' · Clash/直连') + ' · 优惠国家 ' + escapeHtml((item.offerCountries || ["US","GB","DE"]).join('/')) + '</small></div><span class="badge">' + escapeHtml(item.status || "idle") + '</span></div>').join("") : "暂无任务记录";
  }
  async function refresh() {
    try {
      const data = await api("/api/status");
      $("authPanel").hidden = true; $("workspace").hidden = false; render(data);
      clearTimeout(timer); timer = setTimeout(refresh, data.service?.running ? 1000 : 4000);
    } catch (error) {
      clearTimeout(timer); $("authPanel").hidden = false; $("workspace").hidden = true;
      if (token) toast(error.message, true);
    }
  }
  async function refreshOffer(email) {
    if (!email || refreshingOffers.has(email)) return;
    refreshingOffers.add(email);
    try {
      render(await api("/api/status"));
      const countries = selectedOfferCountries();
      if (!countries.length) throw new Error("请至少选择一个优惠检查国家");
      const data = await api("/api/offers/refresh", { method:"POST", body:JSON.stringify({ email, countries }) });
      toast("优惠已重新判断：" + (data.offer?.pool || "pending"));
    } catch (error) {
      toast(error.message, true);
    } finally {
      refreshingOffers.delete(email);
      refresh();
    }
  }
  $("token").value = token;
  $("connect").addEventListener("click", () => { token = $("token").value.trim(); localStorage.setItem("protocol_server_token", token); refresh(); });
  $("start").addEventListener("click", async () => { try { const offerCountries = selectedOfferCountries(); if (!offerCountries.length) throw new Error("请至少选择一个优惠检查国家"); await api("/api/tasks/start", { method:"POST", body:JSON.stringify({ provider:$("provider").value, count:Number($("count").value), concurrency:Number($("concurrency").value), useRegistrationKookeey:$("useRegistrationKookeey").checked, registrationCountry:$("registrationCountry").value || "JP", offerCountries, setupCredentials:$("setupCredentials").checked, checkOffer:$("checkOffer").checked }) }); toast("服务器注册已启动"); refresh(); } catch (error) { toast(error.message, true); } });
  $("stop").addEventListener("click", async () => { try { await api("/api/tasks/stop", { method:"POST", body:"{}" }); toast("停止请求已完成"); refresh(); } catch (error) { toast(error.message, true); } });
  $("refresh").addEventListener("click", refresh);
  $("pool").addEventListener("click", (event) => { const button = event.target.closest(".refresh-offer"); if (button) refreshOffer(button.dataset.email || ""); });
  $("useRegistrationKookeey").addEventListener("change", () => { $("registrationCountryWrap").hidden = !$("useRegistrationKookeey").checked; });
  document.querySelectorAll("[data-pool]").forEach((button) => button.addEventListener("click", () => { activePool = button.dataset.pool; document.querySelectorAll("[data-pool]").forEach((item) => item.classList.toggle("active", item === button)); refresh(); }));
  if (token) refresh();
})();
