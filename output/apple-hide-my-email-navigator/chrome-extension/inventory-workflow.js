(function attachAppleHmeInventoryWorkflow(root, factory) {
  'use strict';

  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.document && typeof chrome === 'object' && chrome.runtime) {
    root.AppleHmeInventoryWorkflow = api.attach(root, chrome);
  }
})(typeof window === 'object' ? window : null, function createWorkflowApi() {
  'use strict';

  const WORKFLOW_KEY = 'appleHmeInventoryWorkflow';
  const TARGET_HOST = 'account.apple.com';
  const MANAGE_PATH = '/account/manage';
  const PRIVACY_PATH = '/account/manage/section/privacy';
  const PRIVACY_URL = `https://${TARGET_HOST}${PRIVACY_PATH}`;
  const MAX_BATCH_SIZE = 100;
  const EMAIL_PATTERN = /[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@icloud\.com/gi;

  // Unicode escapes keep the extension reliable on Windows machines whose
  // default text encoding is not UTF-8.
  const LABELS = Object.freeze({
    privacy: ['\u9690\u79c1', 'Privacy'],
    hideMyEmail: [
      '\u9690\u85cf\u90ae\u4ef6\u5730\u5740',
      '\u9690\u85cf\u6211\u7684\u7535\u5b50\u90ae\u4ef6\u5730\u5740',
      '\u9690\u85cf\u6211\u7684\u90ae\u4ef6\u5730\u5740',
      'Hide My Email',
    ],
    createAddress: ['\u521b\u5efa\u65b0\u5730\u5740', 'Create New Address'],
    confirmAddress: [
      '\u521b\u5efa\u5730\u5740',
      '\u521b\u5efa\u7535\u5b50\u90ae\u4ef6\u5730\u5740',
      '\u4f7f\u7528\u6b64\u5730\u5740',
      '\u7ee7\u7eed',
      'Create Address',
      'Create Email Address',
      'Use Address',
      'Continue',
    ],
    done: ['\u5b8c\u6210', 'Done'],
    back: ['\u8fd4\u56de', 'Back'],
    close: ['\u5173\u95ed', 'Close'],
  });

  const MESSAGES = Object.freeze({
    openingAccount: '\u6b63\u5728\u6253\u5f00 Apple \u8d26\u6237\u7ba1\u7406\u9875\u9762\u2026',
    waitingPassword: '\u7b49\u5f85\u4f60\u5728 Apple \u9875\u9762\u786e\u8ba4\u5bc6\u7801\u2026',
    openingPrivacy: '\u6b63\u5728\u6253\u5f00\u201c\u9690\u79c1\u201d\u2026',
    openingHideMyEmail: '\u6b63\u5728\u6253\u5f00\u201c\u9690\u85cf\u90ae\u4ef6\u5730\u5740\u201d\u2026',
    requestingAddress: '\u6b63\u5728\u8bf7\u6c42 Apple \u751f\u6210\u65b0\u9690\u85cf\u90ae\u7bb1\u2026',
    addressDetected: '\u5df2\u8bc6\u522b\u65b0\u9690\u85cf\u90ae\u7bb1\uff0c\u6b63\u5728\u786e\u8ba4\u521b\u5efa\u2026',
    confirming: '\u6b63\u5728\u786e\u8ba4\u521b\u5efa\u9690\u85cf\u90ae\u7bb1\u2026',
    finalizing: '\u9690\u85cf\u90ae\u7bb1\u5df2\u521b\u5efa\uff0c\u6b63\u5728\u5b8c\u6210\u2026',
    uploading: '\u9690\u85cf\u90ae\u7bb1\u5df2\u521b\u5efa\uff0c\u6b63\u5728\u52a0\u5165\u672a\u6ce8\u518c\u5e93\u5b58\u2026',
    uploaded: '\u5df2\u52a0\u5165\u670d\u52a1\u5668\u672a\u6ce8\u518c\u5e93\u5b58',
    nextAddress: '\u5df2\u5165\u5e93\uff0c\u6b63\u5728\u521b\u5efa\u4e0b\u4e00\u4e2a\u9690\u85cf\u90ae\u7bb1\u2026',
    uploadFailed: '\u52a0\u5165\u672a\u6ce8\u518c\u5e93\u5b58\u5931\u8d25',
    failed: '\u81ea\u52a8\u521b\u5efa\u9690\u85cf\u90ae\u7bb1\u5931\u8d25',
  });

  function normalizeText(value) {
    return String(value || '')
      .replace(/[\u200b-\u200d\ufeff]/g, '')
      .replace(/\s+/g, ' ')
      .trim()
      .toLocaleLowerCase();
  }

  function extractIcloudEmails(value) {
    return Array.from(new Set(
      (String(value || '').match(EMAIL_PATTERN) || [])
        .map((email) => email.toLowerCase())
    ));
  }

  function normalizeBatchSize(value) {
    const parsed = Number.parseInt(String(value || ''), 10);
    if (!Number.isFinite(parsed)) return 1;
    return Math.min(MAX_BATCH_SIZE, Math.max(1, parsed));
  }

  function successfulUploadPatch(workflow, result) {
    const targetCount = normalizeBatchSize(workflow && workflow.targetCount);
    const completedCount = Math.min(
      targetCount,
      Math.max(0, Number.parseInt(String(workflow && workflow.completedCount || 0), 10) || 0) + 1
    );
    const hasMore = completedCount < targetCount;
    return {
      active: hasMore,
      phase: hasMore ? 'next_address' : 'uploaded',
      message: hasMore ? MESSAGES.nextAddress : MESSAGES.uploaded,
      completedCount,
      targetCount,
      lastEmail: workflow.email,
      email: hasMore ? '' : workflow.email,
      createdAt: hasMore ? '' : workflow.createdAt,
      uploadAttempts: 0,
      error: '',
      imported: Boolean(result && result.imported),
      updated: Boolean(result && result.updated),
    };
  }

  function isVisible(windowObject, element) {
    if (!element || element.hidden) return false;
    if (element.getAttribute && element.getAttribute('aria-hidden') === 'true') return false;
    const style = windowObject.getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function visibleDialogs(windowObject) {
    return Array.from(windowObject.document.querySelectorAll('[role="dialog"]'))
      .filter((dialog) => isVisible(windowObject, dialog));
  }

  function actionText(element) {
    return normalizeText([
      element && element.getAttribute ? element.getAttribute('aria-label') : '',
      element && element.getAttribute ? element.getAttribute('title') : '',
      element ? element.innerText : '',
      element ? element.textContent : '',
    ].filter(Boolean).join(' '));
  }

  function chooseAction(windowObject, rootElement, labels, excludedLabels = []) {
    if (!rootElement || typeof rootElement.querySelectorAll !== 'function') return null;
    const excluded = excludedLabels.map(normalizeText);
    let best = null;
    let bestScore = 0;
    for (const element of rootElement.querySelectorAll('a, button, [role="button"], [role="link"]')) {
      if (!isVisible(windowObject, element) || element.disabled) continue;
      if (element.getAttribute('aria-disabled') === 'true') continue;
      const actual = actionText(element);
      if (!actual || excluded.some((label) => actual.includes(label))) continue;
      for (const label of labels) {
        const target = normalizeText(label);
        const score = actual === target ? 100 : actual.includes(target) ? 50 : 0;
        if (score > bestScore) {
          best = element;
          bestScore = score;
        }
      }
    }
    return best;
  }

  function passwordPromptVisible(windowObject) {
    return visibleDialogs(windowObject).some((dialog) => {
      const text = normalizeText(dialog.innerText || dialog.textContent);
      return (
        (text.includes('\u786e\u8ba4\u4f60\u7684\u5bc6\u7801') && text.includes('\u51fa\u4e8e\u5b89\u5168\u8003\u8651'))
        || (text.includes('confirm your password') && text.includes('security'))
      );
    });
  }

  function candidateAddressDialog(windowObject) {
    const candidates = [];
    for (const dialog of visibleDialogs(windowObject)) {
      const text = String(dialog.innerText || dialog.textContent || '');
      if (text.length > 5000) continue;
      if (/\u4e2a\u4f7f\u7528\u4e2d|active email addresses/i.test(text)) continue;
      const normalized = normalizeText(text);
      if (!LABELS.createAddress.some((label) => normalized.includes(normalizeText(label)))) continue;
      const emails = extractIcloudEmails(text);
      if (emails.length === 1) candidates.push({dialog, email: emails[0]});
    }
    return candidates.length ? candidates[candidates.length - 1] : null;
  }

  function setInputValue(windowObject, input, value) {
    if (!input || String(input.value || '').trim()) return false;
    const prototype = input instanceof windowObject.HTMLTextAreaElement
      ? windowObject.HTMLTextAreaElement.prototype
      : windowObject.HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
    if (descriptor && descriptor.set) descriptor.set.call(input, value);
    else input.value = value;
    input.dispatchEvent(new windowObject.Event('input', {bubbles: true}));
    input.dispatchEvent(new windowObject.Event('change', {bubbles: true}));
    return true;
  }

  function fillMetadata(windowObject, dialog, workflow) {
    const inputs = Array.from(dialog.querySelectorAll('input, textarea'));
    for (const [index, input] of inputs.entries()) {
      const descriptor = normalizeText([
        input.getAttribute('aria-label'),
        input.getAttribute('placeholder'),
        input.name,
        input.parentElement && input.parentElement.innerText,
        input.parentElement && input.parentElement.parentElement
          && input.parentElement.parentElement.innerText,
      ].filter(Boolean).join(' '));
      const isLabel = /\u6807\u7b7e|label/.test(descriptor)
        || (input.tagName === 'INPUT' && index === 0);
      const isNote = /\u5907\u6ce8|note/.test(descriptor)
        || input.tagName === 'TEXTAREA';
      if (isLabel) {
        setInputValue(windowObject, input, workflow.label || 'OpenAI \u81ea\u52a8\u5e93\u5b58');
      } else if (isNote) {
        setInputValue(windowObject, input, workflow.note || 'Apple \u9690\u85cf\u90ae\u4ef6\u5730\u5740\u6269\u5c55\u521b\u5efa');
      }
    }
  }

  function emailListed(windowObject, email) {
    const target = normalizeText(email);
    if (!target) return false;
    return normalizeText(windowObject.document.body && windowObject.document.body.innerText).includes(target);
  }

  function attach(windowObject, chromeObject) {
    const clickKeys = new WeakMap();
    let timer = null;
    let busy = false;
    let pauseRequested = false;

    async function readWorkflow() {
      const stored = await chromeObject.storage.local.get(WORKFLOW_KEY);
      return stored[WORKFLOW_KEY] || null;
    }

    async function updateWorkflow(current, patch) {
      const next = {
        ...(current || {}),
        ...patch,
        updatedAt: new Date().toISOString(),
      };
      await chromeObject.storage.local.set({[WORKFLOW_KEY]: next});
      return next;
    }

    function schedule(delay = 500) {
      if (pauseRequested) return;
      if (timer !== null) return;
      timer = windowObject.setTimeout(() => {
        timer = null;
        tick();
      }, delay);
    }

    function clickOnce(element, key) {
      if (pauseRequested || !element) return false;
      const keys = clickKeys.get(element) || new Set();
      if (keys.has(key)) return false;
      keys.add(key);
      clickKeys.set(element, keys);
      element.click();
      return true;
    }

    async function upload(workflow) {
      const attempts = Number(workflow.uploadAttempts || 0) + 1;
      workflow = await updateWorkflow(workflow, {
        phase: 'uploading',
        message: MESSAGES.uploading,
        uploadAttempts: attempts,
        error: '',
      });
      let response;
      try {
        response = await chromeObject.runtime.sendMessage({
          type: 'apple-hme-upload-inventory',
          payload: {
            email: workflow.email,
            label: workflow.label,
            note: workflow.note,
            createdAt: workflow.createdAt || new Date().toISOString(),
          },
        });
      } catch (error) {
        response = {ok: false, error: String(error && error.message || MESSAGES.uploadFailed)};
      }
      if (response && response.ok) {
        const patch = successfulUploadPatch(workflow, response.result);
        await updateWorkflow(workflow, patch);
        if (patch.active) schedule(500);
        return;
      }
      const error = String(response && response.error || MESSAGES.uploadFailed);
      if (attempts >= 3) {
        await updateWorkflow(workflow, {
          active: false,
          phase: 'upload_failed',
          message: '',
          error,
        });
      } else {
        workflow = await updateWorkflow(workflow, {
          phase: 'upload_retry',
          message: `\u4e0a\u4f20\u5931\u8d25\uff0c\u6b63\u5728\u91cd\u8bd5\uff08${attempts}/3\uff09\u2026`,
          error,
        });
        schedule(2500);
      }
    }

    async function tick() {
      if (busy) return;
      busy = true;
      try {
        let workflow = await readWorkflow();
        if (pauseRequested || !workflow || !workflow.active) return;

        let parsed;
        try {
          parsed = new URL(windowObject.location.href);
        } catch (_) {
          schedule();
          return;
        }
        if (parsed.hostname !== TARGET_HOST) return;
        if (!parsed.pathname.startsWith(MANAGE_PATH)) {
          workflow = await updateWorkflow(workflow, {
            phase: 'opening_account',
            message: MESSAGES.openingAccount,
          });
          windowObject.location.assign(PRIVACY_URL);
          return;
        }

        if (passwordPromptVisible(windowObject)) {
          await updateWorkflow(workflow, {
            phase: 'waiting_password',
            message: MESSAGES.waitingPassword,
          });
          schedule(700);
          return;
        }

        if (!parsed.pathname.startsWith(PRIVACY_PATH)) {
          const privacy = chooseAction(windowObject, windowObject.document, LABELS.privacy);
          if (clickOnce(privacy, 'open-privacy')) {
            await updateWorkflow(workflow, {
              phase: 'opening_privacy',
              message: MESSAGES.openingPrivacy,
            });
          } else {
            windowObject.location.assign(PRIVACY_URL);
          }
          schedule(600);
          return;
        }

        // Keep the generated-address form ahead of completion detection. The
        // same email is already visible before Apple confirms creation, so it
        // must not be uploaded while this form is still open.
        const candidate = candidateAddressDialog(windowObject);
        if (candidate) {
          if (!workflow.email) {
            workflow = await updateWorkflow(workflow, {
              email: candidate.email,
              createdAt: new Date().toISOString(),
              phase: 'address_detected',
              message: MESSAGES.addressDetected,
              error: '',
            });
          }
          fillMetadata(windowObject, candidate.dialog, workflow);
          const confirm = chooseAction(
            windowObject,
            candidate.dialog,
            LABELS.confirmAddress,
            LABELS.createAddress
          );
          if (clickOnce(confirm, `confirm-address:${candidate.email}`)) {
            await updateWorkflow(workflow, {
              phase: 'confirming',
              message: MESSAGES.confirming,
            });
          }
          schedule(600);
          return;
        }

        if (workflow.email) {
          if (workflow.phase === 'upload_retry') {
            await upload(workflow);
            return;
          }
          for (const dialog of visibleDialogs(windowObject)) {
            const done = chooseAction(windowObject, dialog, LABELS.done);
            if (clickOnce(done, 'finish-created-address')) {
              await updateWorkflow(workflow, {
                phase: 'finalizing',
                message: MESSAGES.finalizing,
              });
              schedule(500);
              return;
            }
          }
          if (emailListed(windowObject, workflow.email)) {
            await upload(workflow);
            return;
          }
        }

        if (workflow.phase === 'next_address') {
          for (const dialog of visibleDialogs(windowObject)) {
            const back = chooseAction(windowObject, dialog, LABELS.back);
            if (clickOnce(back, `return-to-list:${workflow.completedCount || 0}`)) {
              await updateWorkflow(workflow, {
                phase: 'returning_to_list',
                message: MESSAGES.nextAddress,
              });
              schedule(500);
              return;
            }
          }
          for (const dialog of visibleDialogs(windowObject)) {
            const close = chooseAction(windowObject, dialog, LABELS.close);
            if (clickOnce(close, `close-result:${workflow.completedCount || 0}`)) {
              await updateWorkflow(workflow, {
                phase: 'opening_hide_my_email',
                message: MESSAGES.openingHideMyEmail,
              });
              schedule(500);
              return;
            }
          }
        }

        const create = chooseAction(windowObject, windowObject.document, LABELS.createAddress);
        const mayStartCreation = [
          'starting',
          'opening_account',
          'opening_privacy',
          'opening_hide_my_email',
          'waiting_password',
          'next_address',
          'returning_to_list',
        ].includes(String(workflow.phase || 'starting'));
        const cycle = Math.max(0, Number.parseInt(String(workflow.completedCount || 0), 10) || 0);
        if (create && mayStartCreation && clickOnce(create, `create-address:${cycle}:${workflow.phase || 'starting'}`)) {
          await updateWorkflow(workflow, {
            phase: 'requesting_address',
            message: MESSAGES.requestingAddress,
          });
          schedule(600);
          return;
        }

        const hideMyEmail = chooseAction(windowObject, windowObject.document, LABELS.hideMyEmail);
        if (clickOnce(hideMyEmail, 'open-hide-my-email')) {
          await updateWorkflow(workflow, {
            phase: 'opening_hide_my_email',
            message: MESSAGES.openingHideMyEmail,
          });
        }
        schedule();
      } catch (error) {
        const workflow = await readWorkflow().catch(() => null);
        if (workflow && workflow.active) {
          await updateWorkflow(workflow, {
            active: false,
            phase: 'failed',
            message: '',
            error: String(error && error.message || MESSAGES.failed),
          }).catch(() => {});
        }
      } finally {
        busy = false;
      }
    }

    async function start() {
      const workflow = await readWorkflow();
      if (!workflow || !workflow.active) return false;
      pauseRequested = false;
      schedule(0);
      return true;
    }

    function stop() {
      pauseRequested = true;
      if (timer !== null) {
        windowObject.clearTimeout(timer);
        timer = null;
      }
      return true;
    }

    chromeObject.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      if (!message) return false;
      if (message.type === 'apple-hme-stop-inventory-workflow') {
        stop();
        sendResponse({ok: true, stopped: true});
        return false;
      }
      if (message.type !== 'apple-hme-start-inventory-workflow') return false;
      start()
        .then((started) => sendResponse({ok: true, started}))
        .catch((error) => sendResponse({ok: false, error: String(error && error.message || error)}));
      return true;
    });

    if (typeof windowObject.MutationObserver === 'function') {
      const observer = new windowObject.MutationObserver(() => schedule(80));
      observer.observe(windowObject.document.documentElement, {childList: true, subtree: true});
    }
    start().catch(() => {});
    return Object.freeze({start, stop, tick});
  }

  return Object.freeze({
    LABELS,
    MESSAGES,
    MAX_BATCH_SIZE,
    WORKFLOW_KEY,
    attach,
    candidateAddressDialog,
    chooseAction,
    extractIcloudEmails,
    normalizeBatchSize,
    normalizeText,
    passwordPromptVisible,
    successfulUploadPatch,
  });
});
