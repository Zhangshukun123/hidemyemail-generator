'use strict';

const ACCOUNT_MANAGE_URL = 'https://account.apple.com/account/manage/section/information';
const WORKFLOW_KEY = 'appleHmeInventoryWorkflow';
const SETTINGS_KEY = 'appleHmeInventorySettings';
const MAX_BATCH_SIZE = 100;
const DEFAULT_LABEL = 'OpenAI \u81ea\u52a8\u5e93\u5b58';
const DEFAULT_NOTE = 'Apple \u9690\u85cf\u90ae\u4ef6\u5730\u5740\u6269\u5c55\u521b\u5efa';

const openPageButton = document.getElementById('open-page');
const stopTaskButton = document.getElementById('stop-task');
const batchCountInput = document.getElementById('batch-count');
const description = document.getElementById('description');
const hint = document.getElementById('hint');
const status = document.getElementById('status');

function normalizeBatchSize(value) {
  const parsed = Number.parseInt(String(value || ''), 10);
  if (!Number.isFinite(parsed)) return 1;
  return Math.min(MAX_BATCH_SIZE, Math.max(1, parsed));
}

function isAppleAccountPage(url) {
  try {
    const parsed = new URL(url);
    return parsed.hostname === 'account.apple.com'
      && parsed.pathname.startsWith('/account/manage');
  } catch (_) {
    return false;
  }
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  return tab || null;
}

async function storedState() {
  const stored = await chrome.storage.local.get([WORKFLOW_KEY, SETTINGS_KEY]);
  return {
    workflow: stored[WORKFLOW_KEY] || null,
    settings: stored[SETTINGS_KEY] || null,
  };
}

function progressText(workflow) {
  const completed = Math.max(0, Number.parseInt(String(workflow.completedCount || 0), 10) || 0);
  const target = normalizeBatchSize(workflow.targetCount);
  return `${completed}/${target}`;
}

function renderWorkflow(workflow) {
  status.className = 'status';
  status.textContent = '';
  openPageButton.disabled = false;
  stopTaskButton.hidden = true;
  batchCountInput.disabled = false;
  if (!workflow) return;

  const progress = progressText(workflow);
  if (workflow.active) {
    batchCountInput.value = normalizeBatchSize(workflow.targetCount);
    batchCountInput.disabled = true;
    stopTaskButton.hidden = false;
    openPageButton.disabled = true;
    openPageButton.textContent = '\u6279\u91cf\u4efb\u52a1\u6267\u884c\u4e2d\u2026';
    if (workflow.phase === 'waiting_password') {
      status.classList.add('warning');
      status.textContent = `\u8fdb\u5ea6 ${progress}\uff1aApple \u8981\u6c42\u786e\u8ba4\u5bc6\u7801\uff0c\u8bf7\u5728\u8d26\u6237\u9875\u9762\u624b\u52a8\u8f93\u5165\u3002`;
    } else {
      status.textContent = `\u8fdb\u5ea6 ${progress}\uff1a${workflow.message || '\u6b63\u5728\u81ea\u52a8\u5904\u7406\u2026'}`;
    }
    return;
  }

  if (workflow.phase === 'uploaded') {
    status.classList.add('success');
    status.textContent = `\u6279\u91cf\u4efb\u52a1\u5df2\u5b8c\u6210\uff1a${progress}\uff0c\u5168\u90e8\u5df2\u52a0\u5165\u672a\u6ce8\u518c\u5e93\u5b58\u3002`;
    openPageButton.textContent = '\u5f00\u59cb\u65b0\u7684\u6279\u91cf\u4efb\u52a1';
    return;
  }
  if (workflow.phase === 'stopped') {
    status.classList.add('warning');
    status.textContent = `\u4efb\u52a1\u5df2\u505c\u6b62\uff0c\u5df2\u5b8c\u6210 ${progress}\u3002`;
    openPageButton.textContent = '\u5f00\u59cb\u65b0\u7684\u6279\u91cf\u4efb\u52a1';
    return;
  }
  if (workflow.phase === 'upload_failed' && workflow.email) {
    status.classList.add('error');
    status.textContent = `\u8fdb\u5ea6 ${progress}\uff1a${workflow.error || '\u5f53\u524d\u90ae\u7bb1\u4e0a\u4f20\u5931\u8d25'}`;
    openPageButton.textContent = '\u91cd\u8bd5\u5f53\u524d\u90ae\u7bb1\u4e0a\u4f20';
    batchCountInput.disabled = true;
    return;
  }
  if (workflow.error) {
    status.classList.add('error');
    status.textContent = `\u8fdb\u5ea6 ${progress}\uff1a${workflow.error}`;
    openPageButton.textContent = '\u91cd\u65b0\u5f00\u59cb\u6279\u91cf\u4efb\u52a1';
  }
}

async function refreshPopupState() {
  const [tab, state] = await Promise.all([activeTab(), storedState()]);
  if (state.settings && !state.workflow?.active) {
    batchCountInput.value = normalizeBatchSize(state.settings.targetCount);
  }
  if (isAppleAccountPage(tab && tab.url)) {
    description.textContent = '\u5df2\u68c0\u6d4b\u5230\u5f53\u524d Apple \u8d26\u6237\u9875\u9762\u3002\u8bbe\u7f6e\u6570\u91cf\u540e\uff0c\u6269\u5c55\u4f1a\u9010\u4e2a\u521b\u5efa\u5e76\u52a0\u5165\u670d\u52a1\u5668\u672a\u6ce8\u518c\u5e93\u5b58\u3002';
    hint.textContent = '\u5982 Apple \u8981\u6c42\u786e\u8ba4\u5bc6\u7801\uff0c\u8bf7\u5728\u8d26\u6237\u9875\u9762\u624b\u52a8\u8f93\u5165\uff1b\u6269\u5c55\u4e0d\u4f1a\u8bfb\u53d6\u6216\u4fdd\u5b58\u5bc6\u7801\u3002';
  }
  renderWorkflow(state.workflow);
}

async function notifyCurrentAppleTab(message) {
  const tab = await activeTab();
  if (!tab || !tab.id || !isAppleAccountPage(tab.url)) return {tab, delivered: false};
  try {
    await chrome.tabs.sendMessage(tab.id, message);
    return {tab, delivered: true};
  } catch (_) {
    return {tab, delivered: false};
  }
}

async function startWorkflow() {
  const {workflow: previous} = await storedState();
  if (previous && previous.phase === 'upload_failed' && previous.email) {
    const resumed = {
      ...previous,
      active: true,
      phase: 'upload_retry',
      message: '\u6b63\u5728\u91cd\u8bd5\u5f53\u524d\u90ae\u7bb1\u4e0a\u4f20\u2026',
      error: '',
      updatedAt: new Date().toISOString(),
    };
    await chrome.storage.local.set({[WORKFLOW_KEY]: resumed});
  } else {
    const targetCount = normalizeBatchSize(batchCountInput.value);
    const now = new Date().toISOString();
    const workflow = {
      taskId: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      active: true,
      phase: 'starting',
      targetCount,
      completedCount: 0,
      email: '',
      lastEmail: '',
      label: DEFAULT_LABEL,
      note: DEFAULT_NOTE,
      message: '\u6b63\u5728\u6253\u5f00\u201c\u9690\u85cf\u90ae\u4ef6\u5730\u5740\u201d\u2026',
      error: '',
      uploadAttempts: 0,
      startedAt: now,
      updatedAt: now,
    };
    await chrome.storage.local.set({
      [WORKFLOW_KEY]: workflow,
      [SETTINGS_KEY]: {targetCount},
    });
  }

  const delivery = await notifyCurrentAppleTab({type: 'apple-hme-start-inventory-workflow'});
  if (delivery.tab && delivery.tab.id && isAppleAccountPage(delivery.tab.url)) {
    if (!delivery.delivered) await chrome.tabs.reload(delivery.tab.id);
  } else {
    await chrome.tabs.create({url: ACCOUNT_MANAGE_URL});
  }
  window.close();
}

async function stopWorkflow() {
  const {workflow} = await storedState();
  if (!workflow || !workflow.active) return;
  const stopped = {
    ...workflow,
    active: false,
    phase: 'stopped',
    message: '',
    error: '',
    stoppedAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
  await chrome.storage.local.set({[WORKFLOW_KEY]: stopped});
  await notifyCurrentAppleTab({type: 'apple-hme-stop-inventory-workflow'});
  renderWorkflow(stopped);
}

batchCountInput.addEventListener('change', () => {
  batchCountInput.value = normalizeBatchSize(batchCountInput.value);
});

openPageButton.addEventListener('click', async () => {
  openPageButton.disabled = true;
  try {
    await startWorkflow();
  } catch (_) {
    openPageButton.disabled = false;
    status.className = 'status error';
    status.textContent = '\u542f\u52a8\u5931\u8d25\uff0c\u8bf7\u91cd\u65b0\u52a0\u8f7d\u6269\u5c55\u540e\u91cd\u8bd5\u3002';
  }
});

stopTaskButton.addEventListener('click', async () => {
  stopTaskButton.disabled = true;
  try {
    await stopWorkflow();
  } finally {
    stopTaskButton.disabled = false;
  }
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === 'local' && changes[WORKFLOW_KEY]) {
    renderWorkflow(changes[WORKFLOW_KEY].newValue || null);
  }
});

refreshPopupState().catch(() => {
  status.className = 'status error';
  status.textContent = '\u672a\u80fd\u8bfb\u53d6\u5f53\u524d\u9875\u9762\u72b6\u6001\uff0c\u8bf7\u91cd\u65b0\u52a0\u8f7d\u6269\u5c55\u3002';
});
