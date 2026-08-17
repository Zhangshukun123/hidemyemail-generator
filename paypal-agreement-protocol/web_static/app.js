const API_BASE = '/paypal-pay/api';
const GROK_API_BASE = '/api/grok-trial';
const $ = (id) => document.getElementById(id);
const PRIVATE_BRAINTREE_SLUG = 'bt-vault-8f1d2e4c9a7b6d3e';
const privateBraintreeEnabled = location.pathname.includes(`/${PRIVATE_BRAINTREE_SLUG}`);
const SMS_PROVIDER_IDS = ['manual', 'smsbower', 'hero-sms'];
const SMS_PROVIDER_LABELS = {manual: '手动接码', smsbower: 'SMSBower', 'hero-sms': 'Hero-SMS'};
const FALLBACK_SMS_COUNTRY_CODES = ['BR', 'GB', 'US', 'JP', 'TH', 'ID', 'PH', 'TW', 'MX', 'AE', 'AU', 'CA'];
const shellParams = new URLSearchParams(location.search);
const handoffJobId = new URLSearchParams(location.search).get('job') || '';

const state = {
  jobId: '',
  job: null,
  pollTimer: null,
  jobsTimer: null,
  browserTimer: null,
  browserFrameUrl: '',
  logPinned: true,
  renderedLogCount: 0,
  smsSettings: {
    defaultProvider: 'manual', timeoutSeconds: 60, maxPhoneAttempts: 3,
    providers: [], probe: 0, initialized: false, initialSelectionResolved: false,
  },
};

const vaultState = {
  session: null,
  paypalCheckout: null,
  rendered: false,
  generated: null,
};

if (privateBraintreeEnabled) {
  $('privateBraintreeMode').hidden = false;
} else {
  $('protocolModeSwitch').style.gridTemplateColumns = '1fr';
}
$('vaultAccessToken').value = sessionStorage.getItem('pay153-braintree-access-token') || '';
$('vaultProxy').value = sessionStorage.getItem('pay153-braintree-proxy') || '';
$('vaultAccessToken').addEventListener('input', () => {
  sessionStorage.setItem('pay153-braintree-access-token', $('vaultAccessToken').value);
});
$('vaultProxy').addEventListener('input', () => {
  sessionStorage.setItem('pay153-braintree-proxy', $('vaultProxy').value.trim());
});

class PaymentShellModel {
  constructor(params) {
    this.embedded = params.get('embedded') === '1' || window.self !== window.top;
    this.requestedTheme = ['dark', 'light'].includes(params.get('theme')) ? params.get('theme') : '';
    this.handoffJobId = params.get('job') || '';
  }
}

class PaymentShellView {
  constructor() {
    this.root = document.documentElement;
    this.themeToggle = $('themeToggle');
  }

  setEmbedded(embedded) { this.root.classList.toggle('embedded', embedded); }
  setTheme(dark) {
    this.root.classList.toggle('dark', dark);
    this.themeToggle.textContent = dark ? '☀' : '◐';
  }
  setHandoff(jobId) {
    if (!/^[a-f0-9-]{6,64}$/i.test(jobId)) return;
    document.body.classList.add('auto-handoff-mode');
    const title = document.querySelector('.status-panel h2');
    if (title) title.textContent = '自动支付进度';
    document.title = '自动支付进度 · PAY.153';
  }
}

class PaymentShellPresenter {
  constructor(model, view) {
    this.model = model;
    this.view = view;
    this.colorScheme = matchMedia('(prefers-color-scheme: dark)');
  }

  parentTheme() {
    if (!this.model.embedded || window.parent === window) return '';
    try { return window.parent.document.documentElement.dataset.theme || ''; } catch (_) { return ''; }
  }

  applyTheme(mode, persist = false) {
    const dark = mode === 'dark' || (mode === 'system' && this.colorScheme.matches);
    this.view.setTheme(dark);
    if (persist && !this.model.embedded) localStorage.setItem('pay153-theme', mode);
  }

  mount() {
    this.view.setEmbedded(this.model.embedded);
    this.view.setHandoff(this.model.handoffJobId);
    const saved = localStorage.getItem('pay153-theme') || 'system';
    this.applyTheme(this.model.requestedTheme || this.parentTheme() || (this.model.embedded ? 'dark' : saved));
    this.view.themeToggle.addEventListener('click', () => {
      const next = this.view.root.classList.contains('dark') ? 'light' : 'dark';
      this.applyTheme(next, true);
    });
    this.colorScheme.addEventListener('change', () => {
      if (!this.model.embedded && (localStorage.getItem('pay153-theme') || 'system') === 'system') this.applyTheme('system');
    });
    window.addEventListener('message', event => {
      if (!this.model.embedded || event.origin !== location.origin || event.data?.type !== 'hme-theme') return;
      if (['dark', 'light'].includes(event.data.theme)) this.applyTheme(event.data.theme);
    });
    if (this.model.embedded && window.parent !== window) {
      try {
        const parentRoot = window.parent.document.documentElement;
        this.parentObserver = new MutationObserver(() => this.applyTheme(parentRoot.dataset.theme || 'dark'));
        this.parentObserver.observe(parentRoot, {attributes: true, attributeFilter: ['data-theme']});
      } catch (_) {}
    }
  }
}

const paymentShellPresenter = new PaymentShellPresenter(new PaymentShellModel(shellParams), new PaymentShellView());
paymentShellPresenter.mount();

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'same-origin',
    headers: options.body ? {'Content-Type': 'application/json'} : {},
    ...options,
  });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) { const error = new Error(data.error || `HTTP ${response.status}`); error.status = response.status; throw error; }
  return data;
}

function proxyLines() {
  return $('proxies').value.split(/\r?\n/).map(v => v.trim()).filter(v => v && !v.startsWith('#'));
}
function updateProxyCount() {
  const count = proxyLines().length;
  $('proxyCount').textContent = `${count} / 500`;
  $('proxyCount').classList.toggle('over-limit', count > 500);
}
$('proxies').addEventListener('input', updateProxyCount);

function checkoutId(value) {
  return String(value || '').match(/(?:^|[^A-Za-z0-9_-])((?:oaics|cs)_[A-Za-z0-9_-]{4,})(?![A-Za-z0-9_-])/i)?.[1] || '';
}

function updateOaicsValidation() {
  const required = $('requireOaics').checked;
  $('oaicsCheckoutField').hidden = !required;
  $('oaicsCheckout').required = required;
}
$('requireOaics').addEventListener('change', updateOaicsValidation);
updateOaicsValidation();

let paypalCountries = [];
let dynamicCountriesEnabled = false;

async function updateCountrySchemaHint(country) {
  const hint = $('countrySchemaHint');
  if (!hint || !country) return;
  const selected = $('paypalCountry').selectedOptions[0];
  const cached = selected?.dataset.schemaCached === '1';
  if (!cached) {
    hint.textContent = '\u65b0\u56fd\u5bb6 \u00b7 \u4efb\u52a1\u5f00\u59cb\u65f6\u5b9e\u65f6\u89e3\u6790\u5730\u533a\u5b57\u6bb5\u4e0e\u5730\u5740\u89c4\u5219';
    return;
  }
  try {
    const data = await api(`/country-fields?country=${encodeURIComponent(country)}`);
    const schema = data.fields || {};
    const required = (schema.address_fields || []).filter(v => v.required).map(v => v.paypal_name).join('\u3001') || '\u6309\u9875\u9762\u914d\u7f6e';
    const status = schema.implementation_status === 'live' ? '\u5df2\u9a8c\u8bc1\u534f\u8bae' : '\u5df2\u6709\u5b57\u6bb5\u7f13\u5b58';
    hint.textContent = `${status} \u00b7 ${schema.locale || country} \u00b7 ${schema.currency || '\u2014'} \u00b7 \u5730\u5740\u5fc5\u586b\uff1a${required}`;
  } catch (_) {
    hint.textContent = '\u5b57\u6bb5\u7f13\u5b58\u672a\u547d\u4e2d\uff0c\u4efb\u52a1\u5f00\u59cb\u65f6\u5b9e\u65f6\u89e3\u6790';
  }
}

function updateCountryFields({probe = true} = {}) {
  const select = $('paypalCountry');
  const country = select.value;
  const option = select.selectedOptions[0];
  if (!country || !option) return;
  const zh = option.dataset.zh || country;
  const calling = option.dataset.calling || '+';
  $('dynamicCountryNotice').hidden = option.dataset.live === '1';
  $('countryPickerValue').textContent = `${country} \u00b7 ${zh} \u00b7 ${option.dataset.en || ''}`;
  $('phoneLabel').textContent = `${zh}\u624b\u673a\u53f7`;
  $('phone').placeholder = `${calling} \u624b\u673a\u53f7`;
  $('phone').required = true;
  $('phone').disabled = false;
  $('proxyCountryHint').textContent = `\u63a8\u8350\u586b\u5199 ${country}`;
  updateCountrySchemaHint(country);
  updateSmsProvider({probe});
}

function selectedSmsBowerCountry() {
  return $('smsbowerCountry').value || $('paypalCountry').value || 'BR';
}

function populateSmsBowerCountrySelect() {
  smsSettingsPresenter.populateCountrySelect();
}

class SmsSettingsView {
  constructor() {
    this.panel = document.querySelector('.sms-provider-panel');
    this.dialog = $('smsSettingsDialog');
    this.providerSelect = $('smsProvider');
    this.defaultProviderSelect = $('smsDefaultProvider');
    this.countrySelect = $('smsbowerCountry');
    this.maxPriceInput = $('smsbowerMaxPrice');
  }

  providerId() { return SMS_PROVIDER_IDS.includes(this.providerSelect.value) ? this.providerSelect.value : 'manual'; }
  defaultProviderId() { return SMS_PROVIDER_IDS.includes(this.defaultProviderSelect.value) ? this.defaultProviderSelect.value : 'manual'; }
  country() { return this.countrySelect.value || $('paypalCountry').value || 'BR'; }
  maxPrice() { return Number(this.maxPriceInput.value); }
  setProvider(provider) { this.providerSelect.value = SMS_PROVIDER_IDS.includes(provider) ? provider : 'manual'; }

  bind(presenter) {
    $('smsSettingsOpen').addEventListener('click', () => presenter.open());
    $('smsSettingsOpenInline').addEventListener('click', () => presenter.open());
    $('smsSettingsClose').addEventListener('click', () => this.closeDialog());
    this.dialog.addEventListener('click', event => { if (event.target === this.dialog) this.closeDialog(); });
    this.dialog.addEventListener('close', () => document.documentElement.classList.remove('dialog-open'));
    this.providerSelect.addEventListener('change', () => presenter.changeProvider());
    this.countrySelect.addEventListener('change', () => presenter.changeCountry());
    $('smsbowerPaymentService').addEventListener('change', () => presenter.changePaymentService());
    this.maxPriceInput.addEventListener('change', () => presenter.changeMaxPrice());
    $('smsDefaultSave').addEventListener('click', () => presenter.saveDefaultProvider());
    document.querySelectorAll('[data-sms-action]').forEach(button => {
      button.addEventListener('click', () => {
        if (button.dataset.smsAction === 'clear') presenter.clearApiKey(button.dataset.provider);
        else presenter.saveApiKey(button.dataset.provider);
      });
    });
  }

  openDialog() {
    document.documentElement.classList.add('dialog-open');
    if (typeof this.dialog.showModal === 'function') this.dialog.showModal();
    else this.dialog.setAttribute('open', '');
  }

  closeDialog() {
    if (typeof this.dialog.close === 'function' && this.dialog.open) this.dialog.close();
    else this.dialog.removeAttribute('open');
    document.documentElement.classList.remove('dialog-open');
  }

  apiKey(provider) {
    return (provider === 'hero-sms' ? $('heroSmsApiKey') : $('smsbowerApiKey')).value.trim();
  }

  clearApiKeyInput(provider) {
    (provider === 'hero-sms' ? $('heroSmsApiKey') : $('smsbowerApiKey')).value = '';
  }

  setFeedback(message, kind = '') {
    $('smsSettingsFeedback').textContent = message;
    $('smsSettingsFeedback').className = `sms-settings-feedback${kind ? ` ${kind}` : ''}`;
  }

  setBusy(provider, busy) {
    const selector = provider
      ? `[data-sms-action][data-provider="${provider}"]`
      : '#smsDefaultSave';
    document.querySelectorAll(selector).forEach(button => { button.disabled = busy; });
  }

  renderCredential(providerId, provider) {
    const hero = providerId === 'hero-sms';
    const badge = $(hero ? 'heroSmsConfigBadge' : 'smsbowerConfigBadge');
    const meta = $(hero ? 'heroSmsConfigMeta' : 'smsbowerConfigMeta');
    const docs = $(hero ? 'heroSmsConfigDocs' : 'smsbowerConfigDocs');
    const label = provider?.label || SMS_PROVIDER_LABELS[providerId];
    if (!provider?.configured) {
      badge.textContent = '未配置';
      badge.className = 'status-badge error';
    } else if (provider.error) {
      badge.textContent = '检查异常';
      badge.className = 'status-badge error';
    } else {
      badge.textContent = '已配置';
      badge.className = 'status-badge done';
    }
    const pieces = [];
    if (provider?.error) pieces.push(provider.error);
    if (Number.isFinite(provider?.balance)) pieces.push(`余额 $${provider.balance.toFixed(2)}`);
    if (Number.isFinite(provider?.price)) pieces.push(`当前价 $${provider.price.toFixed(3)}`);
    if (Number.isFinite(provider?.count)) pieces.push(`库存 ${provider.count}`);
    meta.textContent = pieces.join(' · ') || `${label} ${provider?.configured ? '凭据已安全保存' : '尚未保存 API Key'}`;
    const docsUrl = provider?.docsUrl || (!hero ? 'https://smsbower.app/cn/api?page=client' : '');
    docs.hidden = !docsUrl;
    if (docsUrl) docs.href = docsUrl;
  }

  renderSettings(model) {
    const settings = model.smsSettings;
    this.defaultProviderSelect.value = SMS_PROVIDER_IDS.includes(settings.defaultProvider) ? settings.defaultProvider : 'manual';
    $('smsConfigSummary').textContent = `默认 · ${SMS_PROVIDER_LABELS[this.defaultProviderSelect.value]}`;
    const timeout = Number(settings.timeoutSeconds) || 60;
    $('smsDialogTimeoutPolicy').textContent = `SMSBower 与 Hero-SMS 均在 ${timeout} 秒未收到验证码时取消旧号码，并立即重新获取手机号。`;
    this.renderCredential('smsbower', settings.providers.find(item => item.id === 'smsbower'));
    this.renderCredential('hero-sms', settings.providers.find(item => item.id === 'hero-sms'));
    this.renderTaskProvider(model);
  }

  renderTaskProvider(model) {
    const providerId = this.providerId();
    const automatic = providerId !== 'manual';
    const provider = model.smsSettings.providers.find(item => item.id === providerId);
    const label = provider?.label || SMS_PROVIDER_LABELS[providerId];
    const timeout = Number(model.smsSettings.timeoutSeconds) || 60;
    const attempts = Number(model.smsSettings.maxPhoneAttempts) || 3;
    this.panel.dataset.provider = providerId;
    $('smsbowerControls').hidden = !automatic;
    $('smsTimeoutPolicy').hidden = !automatic;
    $('phone').disabled = automatic;
    $('phone').required = !automatic;
    $('phoneField').classList.toggle('provider-disabled', automatic);
    if (!automatic) {
      const option = $('paypalCountry').selectedOptions[0];
      const country = $('paypalCountry').value || '';
      $('phoneLabel').textContent = `${option?.dataset.zh || country}手机号`;
      $('phone').placeholder = `${option?.dataset.calling || '+'} 手机号`;
      $('smsProviderHint').textContent = '使用自有手机号时，PayPal 发码后在右侧手动提交验证码。';
      return;
    }
    $('phoneLabel').textContent = `手机号由 ${label} 自动分配`;
    $('phone').placeholder = '任务运行时自动取号';
    $('smsProviderHint').textContent = `任务使用 ${label} 自动取号取码；${timeout} 秒无验证码会取消旧号并立即重取，最多尝试 ${attempts} 个号码。`;
    const badge = $('smsProviderStatus');
    const meta = $('smsProviderMeta');
    if (!provider?.configured) {
      badge.textContent = `${label} 未配置`;
      badge.className = 'status-badge error';
      meta.textContent = '请打开“接码配置”保存该平台 API Key';
    } else if (provider.error) {
      badge.textContent = `${label} 检查异常`;
      badge.className = 'status-badge error';
      meta.textContent = provider.error;
    } else {
      badge.textContent = `${label} 在线`;
      badge.className = 'status-badge done';
      const pieces = ['PayPal', `${this.country()} 库存 ${Number(provider.count) || 0}`];
      if (Number.isFinite(provider.price)) pieces.push(`当前 $${provider.price.toFixed(3)}`);
      if (Number.isFinite(provider.balance)) pieces.push(`余额 $${provider.balance.toFixed(2)}`);
      meta.textContent = pieces.join(' · ');
    }
    const docs = $('smsProviderDocs');
    docs.hidden = !provider?.docsUrl;
    if (provider?.docsUrl) docs.href = provider.docsUrl;
  }
}

class SmsSettingsPresenter {
  constructor(model, view, request) {
    this.model = model;
    this.view = view;
    this.request = request;
  }

  mount() {
    this.view.bind(this);
    const savedProvider = localStorage.getItem('pay153-sms-provider');
    this.view.setProvider(SMS_PROVIDER_IDS.includes(savedProvider) ? savedProvider : 'manual');
    const savedMaxPrice = Number(localStorage.getItem('pay153-sms-max-price') || localStorage.getItem('pay153-smsbower-max-price'));
    if (Number.isFinite(savedMaxPrice) && savedMaxPrice >= 0.001 && savedMaxPrice <= 50) this.view.maxPriceInput.value = String(savedMaxPrice);
    this.view.renderSettings(this.model);
  }

  provider(providerId = this.view.providerId()) {
    return this.model.smsSettings.providers.find(item => item.id === providerId);
  }

  normalizeResponse(payload) {
    const rawProviders = Array.isArray(payload?.providers) ? payload.providers : [];
    const providers = rawProviders.filter(item => item && ['smsbower', 'hero-sms'].includes(String(item.id || item.provider || '').toLowerCase())).map(item => {
      const id = String(item.id || item.provider).toLowerCase();
      const balance = item.balance == null || item.balance === '' ? null : Number(item.balance);
      const price = item.price == null || item.price === '' ? null : Number(item.price);
      const count = item.count == null || item.count === '' ? null : Number(item.count);
      const defaultMaxPrice = Number(item.defaultMaxPrice);
      return {
        id,
        label: String(item.label || SMS_PROVIDER_LABELS[id]),
        configured: item.configured === true,
        supportedCountries: Array.isArray(item.supportedCountries) ? item.supportedCountries.map(value => String(value).toUpperCase()).filter(Boolean) : [],
        defaultMaxPrice: Number.isFinite(defaultMaxPrice) ? defaultMaxPrice : 3,
        docsUrl: /^https?:\/\//i.test(String(item.docsUrl || '')) ? String(item.docsUrl) : '',
        balance: Number.isFinite(balance) ? balance : null,
        price: Number.isFinite(price) ? price : null,
        count: Number.isFinite(count) ? count : null,
        error: String(item.error || ''),
      };
    });
    ['smsbower', 'hero-sms'].forEach(id => {
      if (!providers.some(item => item.id === id)) providers.push({id, label: SMS_PROVIDER_LABELS[id], configured: false, supportedCountries: [], defaultMaxPrice: 3, docsUrl: '', balance: null, price: null, count: null, error: ''});
    });
    const defaultProvider = SMS_PROVIDER_IDS.includes(payload?.defaultProvider) ? payload.defaultProvider : 'manual';
    return {
      defaultProvider,
      timeoutSeconds: Math.max(1, Number(payload?.timeoutSeconds) || 60),
      maxPhoneAttempts: Math.max(1, Number(payload?.maxPhoneAttempts) || 3),
      providers,
    };
  }

  async loadLegacySmsBower(country) {
    const legacy = await this.request(`/smsbower/status?country=${encodeURIComponent(country)}`);
    return {
      defaultProvider: localStorage.getItem('pay153-sms-provider') || 'manual',
      timeoutSeconds: 60,
      maxPhoneAttempts: 3,
      providers: [{
        id: 'smsbower', label: 'SMSBower', configured: legacy.configured === true,
        supportedCountries: FALLBACK_SMS_COUNTRY_CODES, defaultMaxPrice: Number(legacy.defaultMaxPrice) || 3,
        docsUrl: 'https://smsbower.app/cn/api?page=client', balance: legacy.balance,
        price: legacy.price, count: legacy.count, error: legacy.error || '',
      }],
    };
  }

  applyResponse(payload) {
    const normalized = this.normalizeResponse(payload);
    Object.assign(this.model.smsSettings, normalized, {initialized: true});
    if (!this.model.smsSettings.initialSelectionResolved) {
      const saved = localStorage.getItem('pay153-sms-provider');
      this.view.setProvider(SMS_PROVIDER_IDS.includes(saved) ? saved : normalized.defaultProvider);
      this.model.smsSettings.initialSelectionResolved = true;
    }
    this.populateCountrySelect();
    const provider = this.provider();
    if (!localStorage.getItem('pay153-sms-max-price') && Number.isFinite(provider?.defaultMaxPrice)) {
      this.view.maxPriceInput.value = String(provider.defaultMaxPrice);
    }
    const country = this.view.country();
    if (this.view.providerId() !== 'manual' && country && $('paypalCountry').querySelector(`option[value="${country}"]`) && $('paypalCountry').value !== country) {
      $('paypalCountry').value = country;
      updateCountryFields({probe: false});
    }
    this.view.renderSettings(this.model);
  }

  async refresh(country = this.view.country()) {
    const probe = ++this.model.smsSettings.probe;
    const providerId = this.view.providerId();
    if (providerId !== 'manual') {
      $('smsProviderStatus').textContent = `正在检查 ${SMS_PROVIDER_LABELS[providerId]}`;
      $('smsProviderStatus').className = 'status-badge running';
    }
    try {
      let payload;
      try {
        payload = await this.request(`/sms/providers?country=${encodeURIComponent(country || 'BR')}`);
      } catch (error) {
        if (Number(error.status) !== 404) throw error;
        payload = await this.loadLegacySmsBower(country || 'BR');
      }
      if (probe === this.model.smsSettings.probe) {
        this.applyResponse(payload);
        this.view.setFeedback('接码平台状态已同步；API Key 不会回显。', 'success');
      }
    } catch (error) {
      if (probe !== this.model.smsSettings.probe) return;
      const provider = this.provider();
      if (provider) provider.error = error.message || String(error);
      this.view.renderSettings(this.model);
      this.view.setFeedback(`接码配置读取失败：${error.message || error}`, 'error');
    }
  }

  populateCountrySelect() {
    const select = this.view.countrySelect;
    const previous = select.value;
    const saved = localStorage.getItem('pay153-sms-country') || localStorage.getItem('pay153-smsbower-country') || '';
    const supported = this.provider()?.supportedCountries || [];
    const allowed = supported.length ? new Set(supported) : new Set(FALLBACK_SMS_COUNTRY_CODES);
    const candidates = paypalCountries.filter(item => allowed.has(item.code));
    select.replaceChildren();
    candidates.forEach(item => {
      const option = document.createElement('option');
      option.value = item.code;
      option.textContent = `${item.code} · ${item.name_zh} · ${item.name_en}`;
      select.appendChild(option);
    });
    const preferred = [previous, saved, $('paypalCountry').value, 'BR', candidates[0]?.code].find(code => code && candidates.some(item => item.code === code));
    select.value = preferred || '';
  }

  syncFromPayPalCountry(country, {probe = false} = {}) {
    if (this.view.providerId() === 'manual') {
      this.view.renderTaskProvider(this.model);
      return;
    }
    if (this.view.countrySelect.querySelector(`option[value="${country}"]`)) {
      this.view.countrySelect.value = country;
      localStorage.setItem('pay153-sms-country', country);
      localStorage.setItem('pay153-smsbower-country', country);
    }
    this.view.renderTaskProvider(this.model);
    if (probe) this.refresh(this.view.country());
  }

  changeProvider() {
    const providerId = this.view.providerId();
    this.model.smsSettings.initialSelectionResolved = true;
    localStorage.setItem('pay153-sms-provider', providerId);
    this.populateCountrySelect();
    const provider = this.provider(providerId);
    if (!localStorage.getItem('pay153-sms-max-price') && Number.isFinite(provider?.defaultMaxPrice)) this.view.maxPriceInput.value = String(provider.defaultMaxPrice);
    const country = this.view.country();
    if (providerId !== 'manual' && country && $('paypalCountry').querySelector(`option[value="${country}"]`)) $('paypalCountry').value = country;
    updateCountryFields({probe: false});
    this.view.renderTaskProvider(this.model);
    if (providerId !== 'manual') this.refresh(country);
  }

  changeCountry() {
    const country = this.view.country();
    localStorage.setItem('pay153-sms-country', country);
    localStorage.setItem('pay153-smsbower-country', country);
    if ($('paypalCountry').querySelector(`option[value="${country}"]`)) $('paypalCountry').value = country;
    updateCountryFields({probe: false});
    this.refresh(country);
  }

  changePaymentService() {
    localStorage.setItem('pay153-sms-service', $('smsbowerPaymentService').value);
    localStorage.setItem('pay153-smsbower-service', $('smsbowerPaymentService').value);
  }

  changeMaxPrice() {
    localStorage.setItem('pay153-sms-max-price', this.view.maxPriceInput.value);
    localStorage.setItem('pay153-smsbower-max-price', this.view.maxPriceInput.value);
  }

  open() {
    this.view.openDialog();
    this.view.setFeedback('正在读取接码平台状态…');
    this.refresh();
  }

  async configure(provider, apiKey, successMessage) {
    this.view.setBusy(provider, true);
    try {
      const payload = await this.request('/sms/config', {method: 'POST', body: JSON.stringify({
        provider, apiKey, defaultProvider: this.model.smsSettings.defaultProvider,
      })});
      this.applyResponse(payload);
      this.view.clearApiKeyInput(provider);
      this.view.setFeedback(successMessage, 'success');
    } catch (error) {
      this.view.setFeedback(`配置操作失败：${error.message || error}`, 'error');
    } finally {
      this.view.setBusy(provider, false);
    }
  }

  saveApiKey(provider) {
    const apiKey = this.view.apiKey(provider);
    if (!apiKey) {
      this.view.setFeedback(`请输入 ${SMS_PROVIDER_LABELS[provider]} API Key；留空不会覆盖现有 Key。`, 'error');
      return;
    }
    this.configure(provider, apiKey, `${SMS_PROVIDER_LABELS[provider]} API Key 已保存；Key 未回显。`);
  }

  clearApiKey(provider) {
    this.configure(provider, '', `${SMS_PROVIDER_LABELS[provider]} API Key 已清除。`);
  }

  async saveDefaultProvider() {
    const provider = this.view.defaultProviderId();
    this.view.setBusy('', true);
    try {
      const payload = await this.request('/sms/config', {method: 'POST', body: JSON.stringify({provider, defaultProvider: provider})});
      this.applyResponse(payload);
      this.view.setProvider(provider);
      localStorage.setItem('pay153-sms-provider', provider);
      this.changeProvider();
      this.view.setFeedback(`默认接码平台已设为 ${SMS_PROVIDER_LABELS[provider]}。`, 'success');
    } catch (error) {
      this.view.setFeedback(`默认平台保存失败：${error.message || error}`, 'error');
    } finally {
      this.view.setBusy('', false);
    }
  }
}

const smsSettingsView = new SmsSettingsView();
const smsSettingsPresenter = new SmsSettingsPresenter(state, smsSettingsView, api);
smsSettingsPresenter.mount();

async function loadSmsBowerStatus() { return smsSettingsPresenter.refresh(); }
function updateSmsProvider({probe = false} = {}) {
  smsSettingsPresenter.syncFromPayPalCountry($('paypalCountry').value, {probe});
}

function populateCountrySelect() {
  const select = $('paypalCountry');
  select.replaceChildren();
  paypalCountries.forEach(item => {
    const option = document.createElement('option');
    option.value = item.code;
    option.textContent = `${item.code} \u00b7 ${item.name_zh} \u00b7 ${item.name_en}`;
    option.dataset.zh = item.name_zh;
    option.dataset.en = item.name_en;
    option.dataset.calling = item.calling_code || '+';
    option.dataset.live = item.verified ? '1' : '0';
    option.dataset.schemaCached = item.schema_cached ? '1' : '0';
    select.appendChild(option);
  });
  select.value = paypalCountries.some(v => v.code === 'BR') ? 'BR' : (paypalCountries[0]?.code || '');
}
function populatePhoneCheckCountrySelect() {
  const select = $('phoneCheckCountry');
  const previous = select.value;
  select.replaceChildren();
  paypalCountries.forEach(item => {
    const option = document.createElement('option');
    option.value = item.code;
    option.textContent = `${item.code} · ${item.name_zh} · ${item.calling_code || '—'}`;
    option.dataset.calling = item.calling_code || '+';
    select.appendChild(option);
  });
  const fallback = $('paypalCountry').value || 'BR';
  select.value = paypalCountries.some(v => v.code === previous)
    ? previous
    : (paypalCountries.some(v => v.code === fallback) ? fallback : (paypalCountries[0]?.code || ''));
  updatePhoneCheckPlaceholder();
}
function updatePhoneCheckPlaceholder() {
  const option = $('phoneCheckCountry').selectedOptions[0];
  $('phoneCheckInput').placeholder = `${option?.dataset.calling || '+'} 手机号码`;
}
function closeCountryPicker() { $('countryPickerMenu').hidden = true; $('countryPickerToggle').setAttribute('aria-expanded', 'false'); }
function openCountryPicker() { $('countryPickerMenu').hidden = false; $('countryPickerToggle').setAttribute('aria-expanded', 'true'); $('countrySearch').value = ''; renderCountryOptionList(''); requestAnimationFrame(() => $('countrySearch').focus()); }
function renderCountryOptionList(query = '') {
  const list = $('countryOptionList');
  const selectedCode = $('paypalCountry').value;
  const q = query.trim().toLowerCase();
  const exactCode = q && paypalCountries.find(item => item.code.toLowerCase() === q);
  const matches = exactCode ? [exactCode] : paypalCountries.filter(item => `${item.code} ${item.name_zh} ${item.name_en}`.toLowerCase().includes(q));
  list.replaceChildren();
  const addRows = (label, rows) => {
    if (!rows.length) return;
    const heading = document.createElement('div'); heading.className = 'country-option-heading'; heading.textContent = label; list.appendChild(heading);
    rows.forEach(item => {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'country-option'; button.setAttribute('role', 'option'); button.setAttribute('aria-selected', item.code === selectedCode ? 'true' : 'false'); button.dataset.code = item.code;
      const main = document.createElement('span');
      const code = document.createElement('b'); code.textContent = item.code;
      const chinese = document.createElement('strong'); chinese.textContent = item.name_zh;
      main.append(code, chinese);
      const english = document.createElement('small'); english.textContent = item.name_en;
      button.append(main, english);
      button.addEventListener('click', () => { $('paypalCountry').value = item.code; updateCountryFields(); closeCountryPicker(); });
      list.appendChild(button);
    });
  };
  addRows('\u5df2\u9a8c\u8bc1\u534f\u8bae\u652f\u4ed8', matches.filter(v => v.verified));
  addRows('\u6309\u9700\u5b9e\u65f6\u89e3\u6790', matches.filter(v => !v.verified));
  if (!matches.length) { const empty = document.createElement('div'); empty.className = 'country-option-empty'; empty.textContent = '\u6ca1\u6709\u5339\u914d\u7684\u56fd\u5bb6'; list.appendChild(empty); }
}
async function loadPayPalCountries() {
  const data = await api('/supported-countries');
  paypalCountries = Array.isArray(data.countries) ? data.countries : [];
  dynamicCountriesEnabled = data.dynamic_countries_enabled === true;
  populateCountrySelect();
  populatePhoneCheckCountrySelect();
  populateSmsBowerCountrySelect();
  if ($('smsProvider').value !== 'manual' && selectedSmsBowerCountry()) $('paypalCountry').value = selectedSmsBowerCountry();
  renderCountryOptionList('');
  updateCountryFields({probe: false});
  await smsSettingsPresenter.refresh(selectedSmsBowerCountry());
}
$('countryPickerToggle').addEventListener('click', () => { if ($('countryPickerMenu').hidden) openCountryPicker(); else closeCountryPicker(); });
$('countrySearch').addEventListener('input', () => renderCountryOptionList($('countrySearch').value));
$('countrySearch').addEventListener('keydown', event => { if (event.key === 'Escape') { closeCountryPicker(); $('countryPickerToggle').focus(); } });
$('paypalCountry').addEventListener('change', updateCountryFields);
$('phoneCheckCountry').addEventListener('change', updatePhoneCheckPlaceholder);
document.addEventListener('click', event => { if (!$('countryPicker').contains(event.target)) closeCountryPicker(); });
loadPayPalCountries().catch(() => { $('countrySchemaHint').textContent = '\u56fd\u5bb6\u76ee\u5f55\u52a0\u8f7d\u5931\u8d25'; });

function renderPhoneCheckResult(stateName, title, detail) {
  const panel = $('phoneCheckResult');
  panel.className = `manual-phone-check-result ${stateName}`;
  panel.querySelector('strong').textContent = title;
  panel.querySelector('span').textContent = detail;
}

async function runPassivePhoneCheck() {
  const phone = $('phoneCheckInput').value.trim();
  const country = $('phoneCheckCountry').value;
  if (!phone) {
    renderPhoneCheckResult('failed', '请输入手机号', '检测未执行；未访问 PayPal，未发送验证码。');
    return;
  }
  const button = $('phoneCheckButton');
  button.disabled = true;
  renderPhoneCheckResult('checking', '正在本地检测', '只读取本地国家与号码格式规则。');
  try {
    const data = await api('/phone/check', {
      method: 'POST',
      body: JSON.stringify({phone, country}),
    });
    const result = data.result || {};
    renderPhoneCheckResult(
      'accepted',
      `被动监测成功 · ${result.phone_masked || country}`,
      `${result.country_name || country} ${result.calling_code || ''} · 未访问 PayPal，未发送验证码。${result.limitation || ''}`,
    );
  } catch (error) {
    renderPhoneCheckResult(
      'failed',
      '号码格式未通过',
      `${error.message || error}；未访问 PayPal，未发送验证码。`,
    );
  } finally {
    button.disabled = false;
  }
}

$('phoneCheckButton').addEventListener('click', runPassivePhoneCheck);
$('phoneCheckInput').addEventListener('keydown', event => {
  if (event.key === 'Enter') {
    event.preventDefault();
    runPassivePhoneCheck();
  }
});

$('buyerMode').addEventListener('change', () => {
  const elevated = $('buyerMode').value === 'identity_elevation';
  $('buyerModeHint').value = elevated
    ? '注册后提升 Guest 身份并绑定当前 EC，再提交授权'
    : '沿用当前协议流程，兼容性优先';
});

function normalizePhone(value) {
  return value.replace(/[\s().-]+/g, '');
}
function extractBa(value) {
  const match = value.trim().match(/BA-[A-Za-z0-9]{8,80}/);
  return match ? match[0] : value.trim();
}

function statusMeta(status) {
  const map = {
    queued: ['排队', 'queued'], running: ['运行中', 'running'], awaiting_otp: ['等待验证码', 'awaiting'], awaiting_captcha: ['等待手动验证', 'awaiting'],
    cancelling: ['正在停止', 'queued'], cancelled: ['已停止', 'cancelled'], completed: ['已完成', 'done'], failed: ['失败', 'error'],
  };
  return map[status] || ['等待', 'idle'];
}

function progressFor(job) {
  if (!job) return 0;
  if (job.status === 'completed') return 100;
  if (job.status === 'queued') return 5;
  if (job.status === 'cancelled') return Math.max(5, progressFromStage(job.stage));
  if (job.status === 'failed') return Math.max(8, progressFromStage(job.stage));
  if (job.status === 'awaiting_captcha') return 16;
  if (job.status === 'awaiting_otp') return 78;
  return progressFromStage(job.stage);
}
function progressFromStage(stage = '') {
  if (/Phase 4|最终授权/.test(stage)) return 92;
  if (/Phase 3|短信验证|注册/.test(stage)) return 72;
  if (/Phase 2|创建账号/.test(stage)) return 54;
  if (/Phase 1|风控|指纹/.test(stage)) return 36;
  if (/Phase 0|协议页/.test(stage)) return 18;
  if (/生成/.test(stage)) return 10;
  if (/收到输入/.test(stage)) return 82;
  return 8;
}

function setProgress(percent, text, stage) {
  const p = Math.max(0, Math.min(100, Math.round(percent)));
  $('progressValue').textContent = `${p}%`;
  $('progressText').textContent = text || '处理中';
  $('progressStage').textContent = stage || '运行中';
  $('progressBar').style.width = `${p}%`;
  $('orbitValue').style.strokeDashoffset = `${320.44 * (1 - p / 100)}`;
}

function isMajorLog(log) {
  const level = String(log.level || '').toUpperCase();
  if (level === 'ERROR' || level === 'WARNING' || level === 'WARN' || level === 'SUCCESS') return true;
  const message = String(log.message || '');
  return /Phase\s*[0-4]|协议支付任务|SMS|OTP|verification code|account|authorization|authorize|completed|success|failed|验证码|短信|授权|注册/.test(message);
}
function formatTime(ts) {
  if (!ts) return '--:--:--';
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', {hour12: false});
}
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function renderLogs(logs = []) {
  const box = $('logBox');
  const keepTop = box.scrollTop;
  const wasPinned = state.logPinned || (box.scrollHeight - box.scrollTop - box.clientHeight < 32);
  const major = logs.filter(isMajorLog).slice(-100);
  if (!major.length) {
    box.innerHTML = '<div class="empty-log">任务已开始，正在等待主要步骤。</div>';
    return;
  }
  box.innerHTML = major.map(item => {
    const level = String(item.level || '').toLowerCase();
    const cls = level === 'error' ? 'error' : (level === 'warning' || level === 'warn' ? 'warn' : '');
    return `<div class="log-row ${cls}"><time>${formatTime(item.time)}</time><span>${escapeHtml(item.message)}</span></div>`;
  }).join('');
  if (wasPinned) box.scrollTop = box.scrollHeight;
  else box.scrollTop = keepTop;
}
$('logBox').addEventListener('scroll', () => {
  const box = $('logBox');
  state.logPinned = box.scrollHeight - box.scrollTop - box.clientHeight < 32;
});

function renderResult(job) {
  const terminal = ['completed', 'failed', 'cancelled'].includes(job.status);
  $('resultPanel').hidden = !terminal;
  if (!terminal) return;
  const result = job.result || {};
  $('resultStatus').textContent = job.status === 'completed' ? '授权完成' : (job.status === 'cancelled' ? '任务已停止' : '执行失败');
  $('paymentAction').textContent = result.payment_action || result.status || '—';
  const settlementLabels = {
    confirmed: '已确认到账',
    pending_verification: '等待到账确认',
    authorization_only: '仅协议授权',
  };
  $('settlementStatus').textContent = settlementLabels[result.settlement_status] || '待检查';
  $('billingCountry').textContent = result.billing_country || result.region || job.country || '—';
  const verificationUrl = String(result.pending_url || result.verification_url || '');
  const verifyLink = $('openVerification');
  if (/^https:\/\/(?:chatgpt\.com|chat\.openai\.com|pay\.openai\.com)\//i.test(verificationUrl)) {
    verifyLink.href = verificationUrl;
    verifyLink.textContent = '打开 OpenAI Pending 页面';
    verifyLink.hidden = false;
  } else {
    verifyLink.removeAttribute('href');
    verifyLink.hidden = true;
  }
  $('buyerId').textContent = result.user_id || result.buyer_id || '—';
  $('resultBa').textContent = result.ba_token || job.ba_token || '—';
  const payload = job.status === 'failed'
    ? {...(job.result || {}), status: job.status, error: job.error || job.result?.error, stage: job.stage}
    : (job.result || {status: job.status});
  $('resultValue').value = JSON.stringify(payload, null, 2);
  $('resultSeal').textContent = job.status === 'completed' ? 'READY' : (job.status === 'cancelled' ? 'STOP' : 'ERROR');
}

function renderJob(job) {
  state.job = job;
  state.jobId = job.id;
  sessionStorage.setItem('paypal-protocol-job', job.id);
  const [label, cls] = statusMeta(job.status);
  $('statusBadge').textContent = label;
  $('statusBadge').className = `status-badge ${cls}`;
  setProgress(progressFor(job), job.stage || label, job.status === 'awaiting_otp' ? '需要操作' : label);
  $('cancelButton').hidden = !job.cancellable;
  $('cancelButton').disabled = job.status === 'cancelling';
  $('submitButton').disabled = job.cancellable;
  const phoneValidation = job.phone_validation || {};
  const validationStatus = String(phoneValidation.status || 'not_started');
  const validationMeta = {
    not_started: ['等待号码', 'pending'],
    provider_allocated: ['已获取号码', 'pending'],
    format_valid: ['被动监测成功', 'accepted'],
    format_invalid: ['格式无效', 'failed'],
  }[validationStatus] || ['检查中', 'pending'];
  $('phoneValidationPanel').className = `phone-validation ${validationMeta[1]}`;
  $('phoneValidationStatus').textContent = validationMeta[0];
  $('phoneValidationMessage').textContent = phoneValidation.message ||
    '仅在本地检查国家区号和号码格式；号码监测不会发送验证码。';
  $('phoneValidationAttempt').textContent = Number(phoneValidation.attempt || 0) > 0
    ? `第 ${Number(phoneValidation.attempt)} 次号码检查`
    : '尚未检查';
  const phoneVerification = job.phone_verification || {};
  const verificationStatus = String(phoneVerification.status || 'not_started');
  const verificationMeta = {
    not_started: ['尚未开始', 'pending'],
    code_sent: ['PayPal 已发验证码', 'accepted'],
    sms_received: ['已收到短信', 'accepted'],
    confirmed: ['最终验证通过', 'confirmed'],
    send_rejected: ['PayPal 发码拒绝', 'failed'],
    sms_timeout: ['未收到短信', 'failed'],
    confirmation_rejected: ['验证码未通过', 'failed'],
  }[verificationStatus] || ['验证中', 'pending'];
  $('phoneVerificationPanel').className = `phone-verification ${verificationMeta[1]}`;
  $('phoneVerificationStatus').textContent = verificationMeta[0];
  $('phoneVerificationMessage').textContent = phoneVerification.message ||
    '这是协议支付的后续验证阶段，与号码被动监测相互独立。';
  $('phoneVerificationAttempt').textContent = Number(phoneVerification.attempt || 0) > 0
    ? `第 ${Number(phoneVerification.attempt)} 次短信验证`
    : '尚未进入短信验证';
  $('otpPanel').hidden = !job.awaiting_otp || job.sms_auto;
  $('captchaPanel').hidden = !job.awaiting_captcha;
  if (job.awaiting_otp) {
    $('otpPrompt').textContent = job.awaiting_prompt || '请输入 6 位验证码，也可填写新手机号重新发送。';
  }
  if (job.awaiting_captcha) {
    const challengeUrl = String(job.challenge_url || '').trim();
    const captchaLink = $('captchaLink');
    $('captchaPrompt').textContent = job.awaiting_prompt || '打开真实验证地址完成验证，再粘贴 datadome Cookie 或 adsddtoken。';
    if (/^https:\/\/([a-z0-9-]+\.)*captcha-delivery\.com\/captcha(?:\/|\?|$)/i.test(challengeUrl)) {
      captchaLink.href = challengeUrl;
      captchaLink.hidden = false;
      captchaLink.textContent = '打开真实验证地址';
    } else {
      captchaLink.removeAttribute('href');
      captchaLink.hidden = true;
    }
    const browserActive = Boolean(job.browser_active);
    $('browserPanel').hidden = !browserActive;
    $('captchaFallback').hidden = browserActive;
    if (browserActive) {
      const browserState = job.browser_state || {};
      $('browserStatus').textContent = `${browserState.message || '服务器临时 Chromium 运行中'}${browserState.http_status ? ` · HTTP ${browserState.http_status}` : ''}`;
      scheduleBrowserFrame();
    } else {
      stopBrowserFrame();
    }
  } else {
    $('captchaLink').removeAttribute('href');
    $('captchaLink').hidden = true;
    $('browserPanel').hidden = true;
    $('captchaFallback').hidden = false;
    stopBrowserFrame();
  }
  renderLogs(job.logs || []);
  renderResult(job);
}

function stopBrowserFrame() {
  clearTimeout(state.browserTimer);
  state.browserTimer = null;
  if (state.browserFrameUrl) {
    URL.revokeObjectURL(state.browserFrameUrl);
    state.browserFrameUrl = '';
  }
}

function scheduleBrowserFrame() {
  clearTimeout(state.browserTimer);
  state.browserTimer = setTimeout(refreshBrowserFrame, 180);
}

async function refreshBrowserFrame() {
  if (!state.jobId || !state.job?.browser_active) return stopBrowserFrame();
  const image = $('browserFrame');
  image.src = `${API_BASE}/jobs/${encodeURIComponent(state.jobId)}/browser/frame?t=${Date.now()}`;
  state.browserTimer = setTimeout(refreshBrowserFrame, 700);
}

async function browserAction(payload) {
  if (!state.jobId || !state.job?.browser_active) return;
  const data = await api(`/jobs/${encodeURIComponent(state.jobId)}/browser/action`, {
    method: 'POST', body: JSON.stringify(payload),
  });
  if (data.job) renderJob(data.job);
}

$('browserFrame').addEventListener('click', async event => {
  const image = $('browserFrame');
  if (!image.naturalWidth || !image.naturalHeight) return;
  const rect = image.getBoundingClientRect();
  const x = (event.clientX - rect.left) * image.naturalWidth / rect.width;
  const y = (event.clientY - rect.top) * image.naturalHeight / rect.height;
  try { await browserAction({type: 'click', x, y}); } catch (error) { showClientError(error.message); }
});

document.querySelectorAll('[data-browser-action]').forEach(button => {
  button.addEventListener('click', async () => {
    const action = button.dataset.browserAction;
    const payload = action === 'reload'
      ? {type: 'reload'}
      : {type: 'scroll', delta_y: action === 'scroll-up' ? -600 : 600};
    try { await browserAction(payload); } catch (error) { showClientError(error.message); }
  });
});

document.querySelectorAll('[data-browser-key]').forEach(button => {
  button.addEventListener('click', async () => {
    try { await browserAction({type: 'key', key: button.dataset.browserKey}); }
    catch (error) { showClientError(error.message); }
  });
});

$('browserType').addEventListener('click', async () => {
  const value = $('browserText').value;
  if (!value) return;
  try {
    await browserAction({type: 'text', value});
    $('browserText').value = '';
  } catch (error) { showClientError(error.message); }
});

$('browserFinish').addEventListener('click', async () => {
  try { await browserAction({type: 'finish'}); }
  catch (error) { showClientError(error.message); }
});

function clearMissingJob(message = '') {
  clearTimeout(state.pollTimer);
  stopBrowserFrame();
  sessionStorage.removeItem('paypal-protocol-job');
  state.jobId = '';
  state.job = null;
  state.renderedLogCount = 0;
  $('otpPanel').hidden = true;
  $('captchaPanel').hidden = true;
  $('browserPanel').hidden = true;
  $('cancelButton').hidden = true;
  $('cancelButton').disabled = false;
  $('submitButton').disabled = false;
  $('resultPanel').hidden = true;
  $('logBox').innerHTML = '<div class="empty-log">\u5f53\u524d\u4efb\u52a1\u5df2\u7ed3\u675f\u6216\u670d\u52a1\u91cd\u542f\uff0c\u8bf7\u91cd\u65b0\u63d0\u4ea4\u3002</div>';
  $('statusBadge').textContent = '\u7b49\u5f85';
  $('statusBadge').className = 'status-badge idle';
  setProgress(0, message || '\u7b49\u5f85\u521b\u5efa\u65b0\u4efb\u52a1', 'IDLE');
}

async function loadJob(id, quiet = false) {
  if (!id) return;
  try {
    const job = await api(`/jobs/${encodeURIComponent(id)}?log_offset=0`);
    renderJob(job);
    if (!['completed','failed','cancelled'].includes(job.status)) schedulePoll();
    else clearTimeout(state.pollTimer);
  } catch (error) {
    if (Number(error.status) === 404) {
      clearMissingJob('\u65e7\u4efb\u52a1\u5df2\u7ecf\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u63d0\u4ea4');
      await refreshJobs();
      return;
    }
    if (!quiet) showClientError(error.message);
  }
}
function schedulePoll() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    await loadJob(state.jobId, true);
  }, 1000);
}

function showClientError(message) {
  $('statusBadge').textContent = '失败';
  $('statusBadge').className = 'status-badge error';
  $('progressText').textContent = message;
  $('progressStage').textContent = '请求失败';
}

function protocolMode() {
  return document.querySelector('input[name="protocolMode"]:checked')?.value || 'legacy';
}

function updateProtocolMode() {
  const vault = protocolMode() === 'braintree';
  $('braintreeVaultFields').hidden = !vault;
  $('legacyTokenField').hidden = vault;
  $('legacyCountryFields').hidden = vault;
  $('legacyProxyFields').hidden = vault;
  $('legacyActions').hidden = vault;
  document.querySelectorAll('#protocolModeSwitch label').forEach(label => {
    label.classList.toggle('active', label.querySelector('input')?.checked);
  });
  if (vault) {
    setProgress(0, '等待加载英国 Braintree Vault', 'Braintree 模式');
    $('statusBadge').textContent = 'VAULT';
    $('statusBadge').className = 'status-badge idle';
  }
}

document.querySelectorAll('input[name="protocolMode"]').forEach(input => input.addEventListener('change', updateProtocolMode));

function updateVaultRegion() {
  const region = $('vaultRegion').value;
  const locale = region === 'GB' ? 'en_GB' : 'en_US';
  $('vaultParameterBox').innerHTML = [
    `账单国家=${region}`, `代理出口=待检测`, `buyerCountry=${region}`, `locale=${locale}`,
    'vault=true', 'intent=tokenize', 'fundingSource=paypal',
  ].map(value => `<span>${value}</span>`).join('');
  $('loadVaultButton').querySelector('span').textContent = `生成${region === 'GB' ? '英国' : '美国'} Braintree BA 链接`;
}

$('vaultRegion').addEventListener('change', updateVaultRegion);
updateVaultRegion();
updateProtocolMode();

async function grokApi(path, options = {}) {
  const response = await fetch(`${GROK_API_BASE}${path}`, {
    headers: {'Content-Type': 'application/json', 'X-Grok-Access-Token': $('vaultAccessToken').value.trim()},
    ...options,
  });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data.result;
}

function renderVaultResult(result) {
  const verification = result?.verification || {};
  const active = Boolean(verification.activated);
  $('resultPanel').hidden = false;
  $('resultStatus').textContent = active ? 'SuperGrok 已到账' : '订阅已提交，等待同步';
  $('paymentAction').textContent = result?.payment_action || 'BRAINTREE_VAULT';
  $('settlementStatus').textContent = active ? '已确认到账' : '等待到账确认';
  $('billingCountry').textContent = result?.billing_country || $('vaultRegion').value || '—';
  $('buyerId').textContent = $('vaultAccountId').value.trim() || '—';
  $('resultBa').textContent = vaultState.session?.campaign_id || '—';
  $('resultValue').value = JSON.stringify(result, null, 2);
  $('resultSeal').textContent = active ? 'READY' : 'PENDING';
  $('statusBadge').textContent = active ? '完成' : '同步中';
  $('statusBadge').className = `status-badge ${active ? 'done' : 'awaiting'}`;
  setProgress(active ? 100 : 92, active ? 'Braintree PayPal 订阅已激活' : 'nonce 已提交，等待 Grok 同步', active ? '已到账' : '订阅同步');
}

$('loadVaultButton').addEventListener('click', async () => {
  const accountId = $('vaultAccountId').value.trim();
  const region = $('vaultRegion').value;
  const proxy = $('vaultProxy').value.trim();
  const accessToken = $('vaultAccessToken').value.trim();
  if (!accessToken) return showClientError('请填写私有访问令牌');
  if (!proxy) return showClientError('请填写与账单国家一致的协议生成代理');
  const button = $('loadVaultButton');
  button.disabled = true;
  $('vaultStatus').textContent = '正在校验代理并生成 Braintree BA 链接…';
  setProgress(20, '正在生成 Braintree BA 链接', 'Vault 初始化');
  try {
    const generated = await grokApi('/braintree-link', {method:'POST', body:JSON.stringify({account_id:accountId, region, proxy})});
    $('resultPanel').hidden = false;
    $('resultStatus').textContent = 'Braintree BA 链接已生成';
    $('paymentAction').textContent = 'BRAINTREE_VAULT_LINK';
    $('settlementStatus').textContent = '等待 PayPal 授权并回传 Grok';
    $('billingCountry').textContent = `${generated.billing_country || region} · 代理出口 ${generated.proxy_country || '未知'}${generated.proxy_city ? ' / ' + generated.proxy_city : ''}`;
    $('buyerId').textContent = generated.account_id || accountId || '—';
    if (generated.account_id) $('vaultAccountId').value = generated.account_id;
    $('resultBa').textContent = generated.billing_token || '—';
    $('resultValue').value = generated.approval_url || '';
    $('resultSeal').textContent = 'WAIT';
    vaultState.generated = generated;
    $('vaultReturnPanel').hidden = false;
    $('statusBadge').textContent = '等待授权';
    $('statusBadge').className = 'status-badge awaiting';
    const openLink = $('openVerification');
    openLink.href = generated.approval_url || '#';
    openLink.textContent = '打开 PayPal 授权链接';
    openLink.hidden = !generated.approval_url;
    $('vaultParameterBox').innerHTML = [
      `账单国家=${generated.billing_country || region}`,
      `代理出口=${generated.proxy_country || '未知'}${generated.proxy_city ? '/' + generated.proxy_city : ''}`,
      `buyerCountry=${region}`, 'vault=true', 'intent=tokenize'
    ].map(value => `<span>${value}</span>`).join('');
    $('vaultStatus').textContent = 'BA 链接已返回。点击右侧“打开 PayPal 授权链接”，或复制结果后自行打开。';
    setProgress(50, 'BA 链接已生成，等待 PayPal 授权结果', '等待授权');
  } catch (error) {
    $('vaultStatus').textContent = `生成失败：${error.message || error}`;
    showClientError(error.message || String(error));
  } finally {
    button.disabled = false;
  }
});


function extractVaultPayerId(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  try {
    const parsed = new URL(raw);
    return parsed.searchParams.get('PayerID') || parsed.searchParams.get('payerId') || parsed.searchParams.get('payer_id') || '';
  } catch (_) {
    const match = raw.match(/(?:PayerID|payerId|payer_id)=([^&#\s]+)/i);
    return match ? decodeURIComponent(match[1]) : raw;
  }
}

$('completeVaultButton').addEventListener('click', async () => {
  const generated = vaultState.generated;
  if (!generated?.billing_token || !generated?.account_id) return showClientError('请先生成 Braintree BA 链接');
  const payerId = extractVaultPayerId($('vaultReturnValue').value);
  if (!payerId) return showClientError('请粘贴 PayPal 授权完成后的 URL 或 Payer ID');
  const button = $('completeVaultButton');
  button.disabled = true;
  $('vaultStatus').textContent = '正在将 PayPal 授权回传 Braintree 并提交 Grok…';
  setProgress(72, '正在生成 payment method nonce', 'Braintree tokenize');
  try {
    const result = await grokApi('/braintree-complete', {method:'POST', body:JSON.stringify({
      account_id: generated.account_id,
      billing_token: generated.billing_token,
      payer_id: payerId,
      region: generated.region || $('vaultRegion').value,
      plan_id: generated.plan_id || 'supergrok_monthly',
      campaign_id: generated.campaign_id || '',
      proxy: $('vaultProxy').value.trim(),
    })});
    renderVaultResult(result);
    $('vaultStatus').textContent = result?.verification?.activated ? '已回传 Grok，订阅状态已激活。' : '已回传 Grok，正在等待订阅状态同步。';
  } catch (error) {
    $('vaultStatus').textContent = `回传失败：${error.message || error}`;
    showClientError(error.message || String(error));
  } finally {
    button.disabled = false;
  }
});

$('protocolForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (protocolMode() === 'braintree') return;
  const raw = $('baToken').value.trim();
  const selectedCountryOption = $('paypalCountry').selectedOptions[0];
  if (selectedCountryOption?.dataset.live !== '1' && !dynamicCountriesEnabled) return showClientError('\u8be5\u56fd\u5bb6\u5df2\u8fdb\u5165\u5b9e\u65f6\u89e3\u6790\u76ee\u5f55\uff0c\u52a8\u6001\u56fd\u5bb6\u6267\u884c\u5f00\u5173\u5f53\u524d\u5173\u95ed');
  const smsProvider = $('smsProvider').value || 'manual';
  const automaticSms = smsProvider !== 'manual';
  const phone = automaticSms ? '' : normalizePhone($('phone').value);
  const smsService = $('smsbowerPaymentService').value || 'paypal';
  const smsCountry = selectedSmsBowerCountry();
  const smsMaxPrice = Number($('smsbowerMaxPrice').value);
  const country = automaticSms ? smsCountry : $('paypalCountry').value;
  const proxies = proxyLines();
  const requireOaics = $('requireOaics').checked;
  const checkoutReference = $('oaicsCheckout').value.trim();
  if (!extractBa(raw).startsWith('BA-')) return showClientError('请填写有效的 PayPal 链接或 BA Token');
  if (requireOaics) {
    const sourceCheckoutId = checkoutId(checkoutReference);
    if (!sourceCheckoutId) return showClientError('DE/EUR OAICS 验证需要填写 Checkout ID 或 ChatGPT Checkout URL');
    if (!sourceCheckoutId.toLowerCase().startsWith('oaics_')) {
      return showClientError(`PayPal DE/EUR OAICS 模式要求 custom Checkout 返回 oaics_；当前为 ${sourceCheckoutId.slice(0, 24)}`);
    }
  }
  if (automaticSms) {
    const smsProviderState = smsSettingsPresenter.provider(smsProvider);
    const smsProviderLabel = smsProviderState?.label || SMS_PROVIDER_LABELS[smsProvider] || smsProvider;
    if (!SMS_PROVIDER_IDS.includes(smsProvider)) return showClientError('请选择有效的验证码平台');
    if (!smsProviderState?.configured) return showClientError(`请先打开“接码配置”保存 ${smsProviderLabel} API Key`);
    if (smsProviderState.error) return showClientError(smsProviderState.error);
    if (smsService !== 'paypal') return showClientError(`${smsProviderLabel} 支付设置当前仅支持 PayPal`);
    if (smsCountry !== $('paypalCountry').value) return showClientError('验证码国家必须与 PayPal 国家一致');
    if (smsProviderState.supportedCountries.length && !smsProviderState.supportedCountries.includes(smsCountry)) return showClientError(`${smsProviderLabel} 暂不支持 ${smsCountry} 接码`);
    if (!Number.isFinite(smsMaxPrice) || smsMaxPrice < 0.001 || smsMaxPrice > 50) return showClientError('自动接码 PayPal 最高价必须在 0.001–50 美元之间');
  } else {
    if (!/^\+?\d{8,20}$/.test(phone)) return showClientError('请填写有效手机号');
    if (country === 'BR' && !/^\+?55\d{8,15}$/.test(phone)) return showClientError('巴西 PayPal 请填写 +55 手机号');
    if (country === 'GB' && !/^\+?44\d{9,12}$/.test(phone)) return showClientError('英国 PayPal 请填写 +44 手机号');
    if (country === 'US' && !/^\+?1\d{10}$/.test(phone)) return showClientError('美国 PayPal 请填写 +1 手机号');
    if (country === 'JP' && !/^\+?81\d{9,11}$/.test(phone)) return showClientError('日本 PayPal 请填写 +81 手机号');
    if (country === 'TH' && !/^\+?66[689]\d{8}$/.test(phone)) return showClientError('泰国 PayPal 请填写 +66 后 9 位手机号码');
    if (country === 'ID' && !/^\+?628\d{8,11}$/.test(phone)) return showClientError('印度尼西亚 PayPal 请填写 +62 8xx 手机号码');
    if (country === 'PH' && !/^\+?639\d{9}$/.test(phone)) return showClientError('菲律宾 PayPal 请填写 +63 9xx 手机号码');
    if (country === 'TW' && !/^\+?8869\d{8}$/.test(phone)) return showClientError('中国台湾 PayPal 请填写 +886 9xx 手机号码');
    if (country === 'MX' && !/^\+?52\d{10}$/.test(phone)) return showClientError('墨西哥 PayPal 请填写 +52 后 10 位手机号码');
    if (country === 'AE' && !/^\+?9715[024568]\d{7}$/.test(phone)) return showClientError('阿联酋 PayPal 请填写 +971 5x 手机号码');
  }
  if (!proxies.length) return showClientError(`请至少填写一条 ${country} 代理`);
  if (proxies.length > 500) return showClientError('代理池最多支持 500 条');
  $('submitButton').disabled = true;
  $('resultPanel').hidden = true;
  setProgress(3, '正在创建独立任务', '提交中');
  try {
    const data = await api('/jobs', {method:'POST', body:JSON.stringify({
      paypal_url: raw, phone, country, proxies, agreement_only: false,
      sms_provider: smsProvider,
      sms_service: smsService,
      sms_country: automaticSms ? smsCountry : undefined,
      sms_max_price: automaticSms ? smsMaxPrice : undefined,
      require_oaics: requireOaics,
      checkout_reference: requireOaics ? checkoutReference : '',
      buyer_mode: $('buyerMode').value,
    })});
    state.logPinned = true;
    renderJob(data.job);
    await refreshJobs();
    schedulePoll();
  } catch (error) {
    $('submitButton').disabled = false;
    showClientError(error.message);
  }
});

$('cancelButton').addEventListener('click', async () => {
  if (!state.jobId) return;
  $('cancelButton').disabled = true;
  try {
    const data = await api(`/jobs/${encodeURIComponent(state.jobId)}/cancel`, {method:'POST', body:'{}'});
    renderJob(data.job);
    schedulePoll();
  } catch (error) {
    if (Number(error.status) === 404) clearMissingJob('\u4efb\u52a1\u5df2\u7ecf\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u63d0\u4ea4');
    else showClientError(error.message);
  }
});

$('captchaSubmit').addEventListener('click', submitCaptcha);
async function submitCaptcha() {
  const value = $('captchaValue').value.trim();
  if (!value || !state.jobId) return;
  $('captchaSubmit').disabled = true;
  try {
    const data = await api(`/jobs/${encodeURIComponent(state.jobId)}/captcha`, {method:'POST', body:JSON.stringify({value})});
    $('captchaValue').value = '';
    renderJob(data.job);
    schedulePoll();
  } catch (error) { showClientError(error.message); }
  finally { $('captchaSubmit').disabled = false; }
}

$('otpSubmit').addEventListener('click', submitOtp);
$('otpValue').addEventListener('keydown', event => { if (event.key === 'Enter') submitOtp(); });
async function submitOtp() {
  const value = $('otpValue').value.trim();
  if (!value || !state.jobId) return;
  $('otpSubmit').disabled = true;
  try {
    const data = await api(`/jobs/${encodeURIComponent(state.jobId)}/otp`, {method:'POST', body:JSON.stringify({value})});
    $('otpValue').value = '';
    renderJob(data.job);
    schedulePoll();
  } catch (error) { showClientError(error.message); }
  finally { $('otpSubmit').disabled = false; }
}

async function refreshJobs() {
  try {
    const data = await api('/jobs');
    const jobs = data.jobs || [];
    const wrap = $('recentJobs');
    if (!jobs.length) {
      wrap.innerHTML = '<div class="empty-log">当前浏览器还没有协议支付任务。</div>';
      return;
    }
    wrap.innerHTML = jobs.slice(0, 8).map(job => {
      const [label] = statusMeta(job.status);
      return `<button class="recent-job ${job.id === state.jobId ? 'active' : ''}" type="button" data-id="${escapeHtml(job.id)}"><span><b>${escapeHtml(job.ba_token || job.id)}</b><small>${escapeHtml(job.stage || '')}</small></span><em>${label}</em></button>`;
    }).join('');
    wrap.querySelectorAll('.recent-job').forEach(button => button.addEventListener('click', () => loadJob(button.dataset.id)));
  } catch (_) {}
}
$('refreshJobs').addEventListener('click', refreshJobs);

$('copyResult').addEventListener('click', async () => {
  const text = $('resultValue').value;
  if (!text) return;
  try { await navigator.clipboard.writeText(text); }
  catch (_) { $('resultValue').select(); document.execCommand('copy'); }
  $('copyResult').textContent = '已复制';
  setTimeout(() => $('copyResult').textContent = '复制结果', 1200);
});

async function checkHealth() {
  try {
    await api('/health');
    $('serviceState').innerHTML = '<i></i> 服务在线';
  } catch (_) {
    $('serviceState').innerHTML = '<i class="offline"></i> 服务异常';
  }
}

function formatDuration(seconds) {
  const value = Math.max(0, Math.round(Number(seconds) || 0));
  if (!value) return '—';
  if (value < 60) return `${value}s`;
  const minutes = Math.floor(value / 60);
  const remain = value % 60;
  return remain ? `${minutes}m ${remain}s` : `${minutes}m`;
}

function renderSuccessStats(stats) {
  const events = Array.isArray(stats.success_events) ? stats.success_events : [];
  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  const todayCount = Number.isFinite(Number(stats.success_today))
    ? Number(stats.success_today)
    : events.filter(item => Number(item.ts || 0) >= todayStart).length;
  $('successTotal').textContent = String(stats.success_total || 0);
  $('successToday').textContent = String(todayCount);
  $('successAverage').textContent = formatDuration(stats.average_success_seconds);
  $('successLatest').textContent = stats.latest_success_at
    ? new Date(Number(stats.latest_success_at) * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})
    : '—';

  const serverBuckets = Array.isArray(stats.success_hourly_24h)
    ? stats.success_hourly_24h : [];
  let buckets;
  if (serverBuckets.length === 24) {
    buckets = serverBuckets.map(item => ({
      time: new Date(Number(item.start_ts || 0) * 1000),
      label: String(item.label || ''),
      count: Math.max(0, Number(item.count) || 0),
    }));
  } else {
    const endHour = new Date(now);
    endHour.setMinutes(0, 0, 0);
    const startMs = endHour.getTime() - 23 * 3600 * 1000;
    buckets = Array.from({length: 24}, (_, index) => ({
      time: new Date(startMs + index * 3600 * 1000), count: 0,
    }));
    for (const event of events) {
      const eventMs = Number(event.ts || 0) * 1000;
      const index = Math.floor((eventMs - startMs) / 3600000);
      if (index >= 0 && index < buckets.length) buckets[index].count += 1;
    }
  }
  const maxCount = Math.max(1, ...buckets.map(item => item.count));
  const chartWidth = 720;
  const plotHeight = 58;
  const step = chartWidth / 24;
  const barWidth = 18;
  const bars = buckets.map((item, index) => {
    const height = item.count ? Math.round(10 + item.count / maxCount * 48) : 2;
    const x = index * step + (step - barWidth) / 2;
    const y = plotHeight - height;
    const hour = item.label || item.time.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    const label = index % 4 === 0 || index === 23
      ? `<text class="chart-hour" x="${x + barWidth / 2}" y="78" text-anchor="middle">${hour.slice(0, 2)}</text>` : '';
    const count = item.count
      ? `<text class="chart-count" x="${x + barWidth / 2}" y="${Math.max(8, y - 4)}" text-anchor="middle">${item.count}</text>` : '';
    return `<g><title>${hour} · ${item.count} 次</title><rect class="chart-bar${item.count ? ' active' : ''}" x="${x}" y="${y}" width="${barWidth}" height="${height}" rx="5"></rect>${count}${label}</g>`;
  }).join('');
  $('successTimeline').innerHTML = `<svg class="success-chart-svg" viewBox="0 0 ${chartWidth} 82" preserveAspectRatio="none" role="img">${bars}</svg>`;
  const recentHour = buckets[buckets.length - 1].count;
  $('successTimelineCaption').textContent = stats.success_total
    ? `当前小时 ${recentHour} 次 · 累计 ${stats.success_total} 次`
    : '等待第一个成功信号';
}

async function refreshSuccessStats() {
  try {
    renderSuccessStats(await api('/stats'));
  } catch (_) {}
}

(async function init() {
  updateProxyCount();
  await checkHealth();
  await refreshSuccessStats();
  await refreshJobs();
  const saved = sessionStorage.getItem('paypal-protocol-job');
  const initialJob = /^[a-f0-9-]{6,64}$/i.test(handoffJobId) ? handoffJobId : saved;
  if (initialJob) await loadJob(initialJob, true);
  state.jobsTimer = setInterval(refreshJobs, 5000);
  setInterval(refreshSuccessStats, 15000);
  setInterval(checkHealth, 15000);
})();
