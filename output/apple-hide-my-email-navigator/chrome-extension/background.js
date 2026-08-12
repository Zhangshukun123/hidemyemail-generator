'use strict';

const INVENTORY_ENDPOINT = 'http://127.0.0.1:8765/api/integrations/apple-hme/inventory';
const ICLOUD_EMAIL_PATTERN = /^[^\s@]+@icloud\.com$/i;

async function uploadInventory(payload, fetchFunction = fetch) {
  const email = String(payload && payload.email || '').trim().toLowerCase();
  if (!ICLOUD_EMAIL_PATTERN.test(email)) {
    throw new Error('Apple \u9875\u9762\u6ca1\u6709\u8fd4\u56de\u6709\u6548\u7684 iCloud \u9690\u85cf\u90ae\u7bb1');
  }

  const response = await fetchFunction(INVENTORY_ENDPOINT, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      email,
      label: String(payload.label || '').slice(0, 200),
      note: String(payload.note || '').slice(0, 1000),
      createdAt: String(payload.createdAt || '').slice(0, 100),
    }),
  });
  let result;
  try {
    result = await response.json();
  } catch (_) {
    throw new Error(`\u672c\u673a\u5e93\u5b58\u670d\u52a1\u8fd4\u56de\u4e86\u65e0\u6548\u54cd\u5e94\uff08HTTP ${response.status}\uff09`);
  }
  if (!response.ok || !result.ok) {
    throw new Error(String(result.error || `\u52a0\u5165\u672a\u6ce8\u518c\u5e93\u5b58\u5931\u8d25\uff08HTTP ${response.status}\uff09`));
  }
  return result;
}

if (typeof chrome === 'object' && chrome.runtime && chrome.runtime.onMessage) {
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.type !== 'apple-hme-upload-inventory') return false;
    uploadInventory(message.payload || {})
      .then((result) => sendResponse({ok: true, result}))
      .catch((error) => sendResponse({
        ok: false,
        error: String(error && error.message || '\u52a0\u5165\u672a\u6ce8\u518c\u5e93\u5b58\u5931\u8d25'),
      }));
    return true;
  });
}

if (typeof module === 'object' && module.exports) {
  module.exports = {ICLOUD_EMAIL_PATTERN, INVENTORY_ENDPOINT, uploadInventory};
}
