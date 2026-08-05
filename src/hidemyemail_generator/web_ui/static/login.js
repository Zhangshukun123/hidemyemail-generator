(() => {
  "use strict";
  const form = document.getElementById("loginForm");
  const submit = document.getElementById("submit");
  const notice = document.getElementById("notice");
  const password = document.getElementById("password");
  const passwordToggle = document.getElementById("passwordToggle");
  const themeToggle = document.getElementById("themeToggle");

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#08111f" : "#f3f6fa";
    localStorage.setItem("hme_theme", theme);
  }

  const saved = localStorage.getItem("hme_theme");
  applyTheme(saved === "light" ? "light" : "dark");
  themeToggle.addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
  passwordToggle.addEventListener("click", () => {
    const reveal = password.type === "password";
    password.type = reveal ? "text" : "password";
    passwordToggle.textContent = reveal ? "隐藏" : "显示";
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    notice.textContent = "正在验证…";
    try {
      const response = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: password.value }),
      });
      const data = await response.json().catch(() => ({ ok: false, error: "服务响应无效" }));
      if (!response.ok || !data.ok) throw new Error(data.error || "登录失败");
      location.replace("/");
    } catch (error) {
      notice.textContent = error.message;
      submit.disabled = false;
    }
  });
})();
