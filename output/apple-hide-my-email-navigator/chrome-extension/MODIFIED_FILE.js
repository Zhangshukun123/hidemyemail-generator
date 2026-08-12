(function attachAppleHideMyEmailNavigator(root, factory) {
  'use strict';

  const navigatorApi = factory();

  if (typeof module === 'object' && module.exports) {
    module.exports = navigatorApi;
  }

  if (root && root.document) {
    root.AppleHideMyEmailNavigator = navigatorApi;
    navigatorApi.start(root);
  }
})(typeof window === 'object' ? window : null, function createAppleHideMyEmailNavigator() {
  'use strict';

  const AUTO_NAVIGATION_ENABLED = true;
  const TARGET_HOST = 'account.apple.com';
  const INFORMATION_PATH = '/account/manage/section/information';
  const PRIVACY_PATH = '/account/manage/section/privacy';
  const SCAN_INTERVAL_MS = 700;

  const LABELS = Object.freeze({
    privacy: Object.freeze(['隐私', 'Privacy']),
    hideMyEmail: Object.freeze([
      '隐藏邮件地址',
      '隐藏我的电子邮件地址',
      '隐藏我的邮件地址',
      'Hide My Email',
    ]),
  });

  function normalizeText(value) {
    return String(value || '')
      .replace(/[\u200b-\u200d\ufeff]/g, '')
      .replace(/\s+/g, ' ')
      .trim()
      .toLocaleLowerCase();
  }

  function scoreText(value, expected) {
    const actual = normalizeText(value);
    const target = normalizeText(expected);
    if (!actual || !target) return 0;
    if (actual === target) return 100;
    if (actual.startsWith(`${target} `) || actual.endsWith(` ${target}`)) return 80;
    if (actual.includes(target)) return 50;
    return 0;
  }

  function elementLabels(element) {
    return [
      element && element.getAttribute ? element.getAttribute('aria-label') : '',
      element && element.getAttribute ? element.getAttribute('title') : '',
      element && element.getAttribute ? element.getAttribute('data-testid') : '',
      element ? element.innerText : '',
      element ? element.textContent : '',
    ];
  }

  function isUsable(element) {
    if (!element || typeof element.click !== 'function') return false;
    if (element.disabled) return false;
    if (element.getAttribute && element.getAttribute('aria-disabled') === 'true') return false;
    if (element.getAttribute && element.getAttribute('aria-hidden') === 'true') return false;
    if (element.hidden) return false;

    if (typeof element.getClientRects === 'function') {
      const rects = element.getClientRects();
      if (rects && rects.length === 0) return false;
    }
    return true;
  }

  function chooseAction(elements, labels) {
    let best = null;
    let bestScore = 0;

    for (const element of Array.from(elements || [])) {
      if (!isUsable(element)) continue;
      let score = 0;
      for (const value of elementLabels(element)) {
        for (const label of labels || []) {
          score = Math.max(score, scoreText(value, label));
        }
      }
      if (score > bestScore) {
        best = element;
        bestScore = score;
      }
    }
    return best;
  }

  function stageFromUrl(url, forcedStage) {
    if (forcedStage === 'hide-my-email') return forcedStage;
    let parsed;
    try {
      parsed = new URL(url);
    } catch (_) {
      return null;
    }
    if (parsed.hostname !== TARGET_HOST) return null;
    if (parsed.pathname.startsWith(PRIVACY_PATH)) return 'hide-my-email';
    if (parsed.pathname.startsWith(INFORMATION_PATH)) return 'privacy';
    return null;
  }

  function queryActionElements(documentObject) {
    if (!documentObject || typeof documentObject.querySelectorAll !== 'function') return [];
    return documentObject.querySelectorAll([
      'a',
      'button',
      '[role="button"]',
      '[role="link"]',
      '[tabindex]:not([tabindex="-1"])',
    ].join(','));
  }

  function createNavigator(windowObject) {
    const state = {
      clickCount: 0,
      clickedElements: new WeakSet(),
      forcedStage: null,
      stopped: false,
      timer: null,
    };

    function report(stage, result) {
      const detail = {stage, result, url: windowObject.location.href};
      try {
        windowObject.dispatchEvent(new windowObject.CustomEvent('apple-hme-navigator-status', {detail}));
      } catch (_) {
        // A status event is optional; navigation remains functional without it.
      }
      if (windowObject.console && typeof windowObject.console.info === 'function') {
        windowObject.console.info('[Apple HME Navigator]', detail);
      }
    }

    function schedule(delay = SCAN_INTERVAL_MS) {
      if (state.stopped) return;
      if (state.timer !== null) return;
      state.timer = windowObject.setTimeout(tick, delay);
    }

    function clickAction(stage, labels) {
      const elements = queryActionElements(windowObject.document);
      const action = chooseAction(elements, labels);
      if (!action || state.clickedElements.has(action)) return false;

      state.clickedElements.add(action);
      state.clickCount += 1;
      action.click();
      report(stage, 'clicked');
      return true;
    }

    function tick() {
      state.timer = null;
      if (state.stopped || !AUTO_NAVIGATION_ENABLED) return false;

      const stage = stageFromUrl(windowObject.location.href, state.forcedStage);
      if (stage === 'privacy') {
        if (clickAction(stage, LABELS.privacy)) {
          state.forcedStage = 'hide-my-email';
          schedule(250);
          return true;
        }
      } else if (stage === 'hide-my-email') {
        if (clickAction(stage, LABELS.hideMyEmail)) {
          state.forcedStage = 'done';
          return true;
        }
      }
      schedule();
      return false;
    }

    function stop() {
      state.stopped = true;
      if (state.timer !== null) {
        windowObject.clearTimeout(state.timer);
        state.timer = null;
      }
      if (state.observer) state.observer.disconnect();
    }

    function start() {
      if (!AUTO_NAVIGATION_ENABLED || state.stopped) return false;
      if (typeof windowObject.MutationObserver === 'function') {
        state.observer = new windowObject.MutationObserver(() => schedule(50));
        state.observer.observe(windowObject.document.documentElement, {
          childList: true,
          subtree: true,
        });
      }
      schedule(0);
      return true;
    }

    return Object.freeze({start, stop, tick, state});
  }

  function start(windowObject) {
    if (!windowObject || !windowObject.document) return null;
    const instance = createNavigator(windowObject);
    instance.start();
    return instance;
  }

  return Object.freeze({
    AUTO_NAVIGATION_ENABLED,
    INFORMATION_PATH,
    LABELS,
    PRIVACY_PATH,
    TARGET_HOST,
    chooseAction,
    createNavigator,
    normalizeText,
    scoreText,
    stageFromUrl,
    start,
  });
});
