"use strict";

const path = require("path");

const EVENT_PREFIX = "HME_PROTOCOL_EVENT:";

function emit(payload) {
  process.stdout.write(`${EVENT_PREFIX}${JSON.stringify(payload)}\n`);
}

function safeDetail(value) {
  return String(value || "")
    .replace(/\b\d{6}\b/g, "[验证码]")
    .replace(/(access[_-]?token|session[_-]?token|authorization)\s*[:=]\s*\S+/gi, "$1=[REDACTED]")
    .slice(0, 1000);
}

async function main() {
  const email = String(process.env.HME_PROTOCOL_EMAIL || "").trim().toLowerCase();
  const password = String(process.env.HME_PROTOCOL_PASSWORD || "");
  const projectDir = String(process.env.HME_PROTOCOL_PROJECT_DIR || "").trim();
  const codeServiceUrl = String(process.env.HME_CODE_SERVICE_URL || "").trim().replace(/\/$/, "");
  const codeServiceToken = String(process.env.HME_CODE_SERVICE_TOKEN || "");
  if (!email || !projectDir || !codeServiceUrl || !codeServiceToken) {
    throw new Error("协议登录参数不完整");
  }

  const serviceFile = path.join(projectDir, "services", "chatgpt-service.js");
  const protocolLogin = require(serviceFile);
  if (!protocolLogin || typeof protocolLogin.login !== "function") {
    throw new Error("协议登录服务不可用");
  }

  const since = new Date().toISOString();
  const fetchCode = async () => {
    const response = await fetch(`${codeServiceUrl}/api/gpt-code`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Local-Token": codeServiceToken,
      },
      body: JSON.stringify({ email, since }),
    });
    let payload = {};
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
    if (response.status === 404) return [];
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `验证码服务请求失败: HTTP ${response.status}`);
    }
    const code = String(payload.code || "").trim();
    if (!/^\d{6}$/.test(code)) return [];
    return [
      {
        from: "noreply@openai.com",
        subject: "OpenAI verification code",
        bodyText: code,
        date: payload.receivedAt || new Date().toISOString(),
      },
    ];
  };

  const session = await protocolLogin.login(
    { email, password },
    fetchCode,
    (status, detail) => {
      emit({ status: "progress", phase: String(status || ""), detail: safeDetail(detail) });
    },
  );
  if (!session || typeof session !== "object" || !session.accessToken) {
    throw new Error("协议登录没有返回有效 Session");
  }
  emit({ status: "success", session });
}

main().catch((error) => {
  emit({ status: "error", detail: safeDetail(error && error.message ? error.message : error) });
  process.exitCode = 1;
});
