'use strict';

document.getElementById('open-page').addEventListener('click', () => {
  chrome.tabs.create({
    url: 'https://account.apple.com/account/manage/section/information',
  });
  window.close();
});
