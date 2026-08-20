CODE_PORTAL_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>获取 iCloud 验证码</title>
  <style>
    :root { font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif; color-scheme: dark; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; background: #0b1220; color: #e5edf8; }
    main { width: min(620px, 100%); padding: 32px; border: 1px solid #26354d; border-radius: 20px; background: #111c2e; box-shadow: 0 24px 70px #0008; }
    h1 { margin: 0 0 8px; font-size: clamp(25px, 5vw, 34px); }
    .hint { margin: 0 0 24px; color: #9db0ca; line-height: 1.6; }
    form { display: grid; grid-template-columns: 1fr auto; gap: 12px; }
    input, button { min-height: 50px; border-radius: 12px; font: inherit; }
    input { width: 100%; padding: 0 15px; border: 1px solid #405372; background: #0b1424; color: #fff; outline: none; }
    input:focus { border-color: #60a5fa; box-shadow: 0 0 0 3px #2563eb3d; }
    button { padding: 0 22px; border: 0; background: #2563eb; color: #fff; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: .6; cursor: wait; }
    #status { min-height: 24px; margin: 18px 0 0; color: #9db0ca; }
    #status.error { color: #fca5a5; }
    #result { display: none; margin-top: 18px; padding: 22px; border: 1px solid #2b3b55; border-radius: 16px; background: #0c1728; text-align: center; }
    #result.visible { display: block; }
    #code { display: block; margin-bottom: 8px; font-size: 34px; font-weight: 800; letter-spacing: .12em; color: #93c5fd; }
    #time { color: #9db0ca; font-size: 14px; }
    #copy { min-height: 38px; margin-top: 16px; padding: 0 16px; border: 1px solid #3b4f70; background: #172640; font-size: 14px; }
    @media (max-width: 520px) { main { padding: 24px 18px; } form { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <main>
    <h1>获取 iCloud 验证码</h1>
    <p class="hint">输入“隐藏我的邮箱”子邮箱，获取该邮箱当前最新的验证码。</p>
    <form id="lookup">
      <input id="email" type="email" autocomplete="email" inputmode="email" placeholder="子邮箱@icloud.com" required autofocus>
      <button id="submit" type="submit">获取验证码</button>
    </form>
    <p id="status" role="status" aria-live="polite"></p>
    <section id="result">
      <code id="code"></code>
      <div id="time"></div>
      <button id="copy" type="button">复制验证码</button>
    </section>
  </main>
  <script>
    const form = document.getElementById("lookup");
    const email = document.getElementById("email");
    const submit = document.getElementById("submit");
    const status = document.getElementById("status");
    const result = document.getElementById("result");
    const code = document.getElementById("code");
    const time = document.getElementById("time");
    const copy = document.getElementById("copy");

    function message(value, isError = false) {
      status.textContent = value;
      status.className = isError ? "error" : "";
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      result.classList.remove("visible");
      message("正在同步 iCloud 邮件…");
      try {
        const response = await fetch("/api/code/latest", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: email.value.trim() }),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || "获取失败");
        code.textContent = data.code;
        const received = new Date(data.receivedAt);
        time.textContent = Number.isNaN(received.getTime()) ? (data.receivedAt || "") : `接收时间：${received.toLocaleString()}`;
        result.classList.add("visible");
        message("已获取当前最新验证码");
      } catch (error) {
        message(error.message || "获取失败", true);
      } finally {
        submit.disabled = false;
      }
    });

    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(code.textContent);
      copy.textContent = "已复制";
      setTimeout(() => { copy.textContent = "复制验证码"; }, 1200);
    });
  </script>
</body>
</html>
"""
