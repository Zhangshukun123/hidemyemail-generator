// ==UserScript==
// @name         PayPal 提链结果复制器（基线）
// @namespace    local.hidemyemail
// @version      0.1.0
// @match        https://pp.uplw.uno/*
// @grant        GM_setClipboard
// ==/UserScript==

(function () {
  "use strict";

  function copyVisibleResult(root = document) {
    const link = root.querySelector("#resultReturn");
    const value = String(link?.href || "").trim();
    if (value && !value.endsWith("#")) {
      if (typeof GM_setClipboard === "function") GM_setClipboard(value);
      return value;
    }
    return "";
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { copyVisibleResult };
    return;
  }

  window.addEventListener("load", () => copyVisibleResult(document), { once: true });
})();
