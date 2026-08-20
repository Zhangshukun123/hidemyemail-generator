(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
  let renderedRoutingSignature = "";

  function routingRenderSignature(routing) {
    const purpose = (value) => ({
      provider: value?.provider || "",
      configured: Boolean(value?.configured),
      maxPrice: value?.maxPrice ?? null,
      countries: Array.isArray(value?.countries) ? value.countries : [],
    });
    return JSON.stringify({
      binding: purpose(routing.binding),
      paypal: purpose(routing.paypal),
      providers: Array.isArray(routing.providers) ? routing.providers.map((item) => ({
        provider: item.provider,
        label: item.label,
        configured: Boolean(item.configured),
      })) : [],
      countryOptions: Array.isArray(routing.countryOptions) ? routing.countryOptions.map((item) => ({
        code: item.code,
        label: item.label,
        binding: Boolean(item.binding),
        paypal: Boolean(item.paypal),
      })) : [],
    });
  }

  function render(state) {
    const paymentSms = state.paymentSms || {};
    const routing = paymentSms.routing || {};
    const settingsPanel = $("settingsPanel");
    const nextRoutingSignature = routingRenderSignature(routing);
    if (settingsPanel.querySelector(".sms-routing-settings") &&
        nextRoutingSignature === renderedRoutingSignature) {
      return;
    }
    renderedRoutingSignature = nextRoutingSignature;
    const binding = routing.binding || {
      provider: "smsbower", maxPrice: 0.064, countries: ["CL", "US"],
    };
    const paypal = routing.paypal || {
      provider: "smsbower", maxPrice: 0.30, countries: [],
    };
    const providers = Array.isArray(routing.providers) ? routing.providers : [];
    const countries = Array.isArray(routing.countryOptions) ? routing.countryOptions : [];
    const providerOptions = (selected) => providers.map((item) =>
      '<option value="' + escapeHtml(item.provider) + '"' +
      (item.provider === selected ? " selected" : "") + ">" +
      escapeHtml(item.label) + (item.configured ? " · 已配置" : " · 未配置") + "</option>"
    ).join("");
    const countryOptions = (purpose, selected) => countries.filter((item) => item[purpose]).map((item) =>
      '<label class="sms-country-option"><input type="checkbox" data-sms-country="' + purpose +
      '" value="' + escapeHtml(item.code) + '"' + (selected.includes(item.code) ? " checked" : "") +
      '><span><b>' + escapeHtml(item.code) + "</b>" + escapeHtml(item.label) + "</span></label>"
    ).join("");
    const configuredCount = providers.filter((item) => item.configured).length;
    const bothReady = Boolean(binding.configured && paypal.configured);
    $("settingsStatus").className = "badge " + (bothReady ? "success" : configuredCount ? "warning" : "error");
    $("settingsStatus").textContent = bothReady ? "全局接码已就绪" : configuredCount ? "部分平台已配置" : "等待 API Key";
    settingsPanel.innerHTML =
      '<div class="settings-form sms-routing-settings"><section class="form-section"><h3>平台凭据</h3>' +
      '<div class="sms-provider-key-grid">' + providers.map((item) =>
        '<label class="field-label"><span>' + escapeHtml(item.label) + ' API Key · ' +
        (item.configured ? "已保存" : "未配置") + '</span><input id="' +
        (item.provider === "smsbower" ? "smsbowerGlobalApiKey" : "heroSmsGlobalApiKey") +
        '" type="password" autocomplete="off" placeholder="' +
        (item.configured ? "留空保持现有密钥" : "请输入 API Key") + '"></label>'
      ).join("") + '</div></section><section class="form-section"><h3>全局用途策略</h3>' +
      '<div class="sms-purpose-grid"><article class="sms-purpose-card"><header><div><strong>绑定手机号</strong><small>按勾选顺序取号；当前国家无库存才尝试下一个</small></div><span class="badge">OPENAI</span></header>' +
      '<div class="sms-purpose-fields"><label class="field-label"><span>接码平台</span><select id="bindingSmsProvider">' +
      providerOptions(binding.provider) + '</select></label><label class="field-label"><span>统一最高价 $</span><input id="bindingSmsMaxPrice" type="number" min="0.001" max="50" step="0.001" value="' +
      escapeHtml(binding.maxPrice) + '"></label></div><div class="sms-country-title"><strong>取号国家（多选）</strong><small>按界面顺序回退</small></div><div class="sms-country-grid">' +
      countryOptions("binding", binding.countries || []) + '</div></article>' +
      '<article class="sms-purpose-card"><header><div><strong>PayPal</strong><small>必须勾选当前 Checkout / 代理对应国家</small></div><span class="badge">PAYPAL</span></header>' +
      '<div class="sms-purpose-fields"><label class="field-label"><span>接码平台</span><select id="paypalSmsProvider">' +
      providerOptions(paypal.provider) + '</select></label><label class="field-label"><span>统一最高价 $</span><input id="paypalSmsMaxPrice" type="number" min="0.001" max="50" step="0.001" value="' +
      escapeHtml(paypal.maxPrice) + '"></label></div><div class="sms-country-title"><strong>允许国家（多选）</strong><small>支付国家不在范围内时禁止启动</small></div><div class="sms-country-grid">' +
      countryOptions("paypal", paypal.countries || []) + '</div></article></div></section>' +
      '<div class="connection-card sms-global-note"><strong>全局配置</strong><span>保存后，账号页绑定、支付后绑定、一键流程和 PayPal 支付台都会读取同一数据库策略。</span></div>' +
      '<div class="settings-actions"><button class="button primary" data-action="save-sms-routing">保存全局接码配置</button></div></div>';
  }

  function buildPayload() {
    const selectedCountries = (purpose) => [...document.querySelectorAll(
      '[data-sms-country="' + purpose + '"]:checked',
    )].map((input) => input.value);
    return {
      binding: {
        provider: $("bindingSmsProvider").value,
        maxPrice: Number($("bindingSmsMaxPrice").value),
        countries: selectedCountries("binding"),
      },
      paypal: {
        provider: $("paypalSmsProvider").value,
        maxPrice: Number($("paypalSmsMaxPrice").value),
        countries: selectedCountries("paypal"),
      },
      apiKeys: {
        smsbower: $("smsbowerGlobalApiKey").value,
        "hero-sms": $("heroSmsGlobalApiKey").value,
      },
    };
  }

  function register(controller) {
    controller.commands.register("save-sms-routing", async () => {
      const data = await controller.api.post("/api/payment-sms/config", buildPayload());
      renderedRoutingSignature = "";
      controller.store.patch({ paymentSms: data });
      render(controller.store.state);
      return "全局接码配置已保存";
    });
  }

  window.HmeSmsSettings = Object.freeze({ buildPayload, register, render });
})();
