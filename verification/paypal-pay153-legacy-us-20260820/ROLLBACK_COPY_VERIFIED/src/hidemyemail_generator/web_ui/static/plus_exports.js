(() => {
  "use strict";

  function filename(response, format) {
    const disposition = response.headers.get("Content-Disposition") || "";
    const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    if (encoded) return decodeURIComponent(encoded[1]);
    const plain = disposition.match(/filename="?([^";]+)"?/i);
    return plain ? plain[1] : `openai-plus-${format}.json`;
  }

  async function exportAccount(button) {
    const format = String(button.dataset.plusExport || "").toLowerCase();
    const email = String(button.dataset.email || "").toLowerCase();
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "正在导出…";
    try {
      const response = await fetch("/api/plus-accounts/export", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Local-Token": window.__HME_LOCAL_TOKEN__,
        },
        body: JSON.stringify({ email, format }),
        cache: "no-store",
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || `导出失败（HTTP ${response.status}）`);
      }
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename(response, format);
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      button.textContent = `${format === "cpa" ? "CPA" : "Sub2API"} 已导出`;
      setTimeout(() => { button.textContent = original; }, 1600);
    } catch (error) {
      button.textContent = original;
      window.alert(error.message || "导出失败");
    } finally {
      button.disabled = false;
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-plus-export]");
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    exportAccount(button);
  });
})();
