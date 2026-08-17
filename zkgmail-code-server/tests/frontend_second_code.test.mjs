import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";


class ElementStub {
  constructor(id) {
    this.id = id;
    this.value = "";
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.readOnly = false;
    this.dataset = {};
    this.style = {};
    this.listeners = new Map();
    this.classList = {
      add() {},
      remove() {},
    };
    this.span = { textContent: "" };
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  querySelector(selector) {
    return selector === "span" ? this.span : null;
  }

  focus() {}
  select() {}
  remove() {}
}


function response(status, data) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return data;
    },
  };
}


async function eventually(predicate, message) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.fail(message);
}


function loadPortal(replies) {
  const ids = [
    "lookupForm", "email", "submitButton", "cancelButton", "statusPanel",
    "statusTitle", "statusMessage", "result", "code", "resultEmail",
    "receivedAt", "copyCodeButton", "nextCodeButton", "toast",
  ];
  const elements = new Map(ids.map((id) => [id, new ElementStub(id)]));
  elements.get("result").hidden = true;
  elements.get("cancelButton").hidden = true;
  const requests = [];
  let nowMilliseconds = 0;

  class PortalDate extends Date {
    static now() {
      return nowMilliseconds;
    }
  }

  const context = vm.createContext({
    AbortController,
    Date: PortalDate,
    URLSearchParams,
    console,
    document: {
      body: { appendChild() {} },
      createElement: (tag) => new ElementStub(tag),
      execCommand() {},
      getElementById: (id) => elements.get(id),
    },
    fetch: async (url, options) => {
      requests.push({ url, body: JSON.parse(options.body) });
      if (typeof replies === "function") return replies();
      assert.ok(replies.length, "unexpected fetch call");
      return replies.shift();
    },
    history: { replaceState() {} },
    location: { hash: "", pathname: "/", search: "" },
    navigator: { clipboard: { async writeText() {} } },
    window: {
      clearTimeout() {},
      setTimeout(callback, milliseconds = 0) {
        nowMilliseconds += Number(milliseconds) || 0;
        callback();
        return 1;
      },
    },
  });
  const source = readFileSync(
    new URL("../zkgmail_code_server/static/app.js", import.meta.url),
    "utf8",
  );
  vm.runInContext(source, context, { filename: "app.js" });
  return { elements, requests };
}


async function submit(elements) {
  await elements.get("lookupForm").listeners.get("submit")({ preventDefault() {} });
}


test("same mailbox submits its opaque cursor and polls until the next code", async () => {
  const firstCursor = "opaque:uid/42==?do-not-parse";
  const secondCursor = "opaque:uid/43==?do-not-parse";
  const replies = [
    response(200, {
      ok: true,
      email: "repeat@zkgmail.com",
      code: "111111",
      cursor: firstCursor,
      receivedAt: "2026-08-17T01:58:00+00:00",
    }),
    response(404, { ok: false, state: "waiting" }),
    response(200, {
      ok: true,
      email: "repeat@zkgmail.com",
      code: "111111",
      cursor: firstCursor,
      receivedAt: "2026-08-17T01:58:00+00:00",
    }),
    response(200, {
      ok: true,
      email: "repeat@zkgmail.com",
      code: "222222",
      cursor: secondCursor,
      receivedAt: "2026-08-17T01:59:00+00:00",
    }),
  ];
  const { elements, requests } = loadPortal(replies);
  elements.get("email").value = "repeat@zkgmail.com";

  await submit(elements);
  await eventually(
    () => elements.get("code").textContent === "111111",
    "first verification code was not rendered",
  );
  assert.deepEqual(requests[0].body, { email: "repeat@zkgmail.com" });

  await submit(elements);
  await eventually(
    () => elements.get("code").textContent === "222222",
    "second verification code was not rendered after polling",
  );

  assert.equal(requests.length, 4);
  for (const request of requests.slice(1)) {
    assert.equal(request.body.afterCursor, firstCursor);
  }
  assert.equal(elements.get("submitButton").span.textContent, "等待下一条验证码");
});


test("cursor remains scoped to the mailbox that produced it", async () => {
  const replies = [
    response(200, {
      ok: true,
      email: "first@zkgmail.com",
      code: "111111",
      cursor: "first-mailbox-cursor",
      receivedAt: "2026-08-17T01:58:00+00:00",
    }),
    response(200, {
      ok: true,
      email: "second@zkgmail.com",
      code: "222222",
      cursor: "second-mailbox-cursor",
      receivedAt: "2026-08-17T01:59:00+00:00",
    }),
  ];
  const { elements, requests } = loadPortal(replies);
  elements.get("email").value = "first@zkgmail.com";
  await submit(elements);
  await eventually(
    () => elements.get("code").textContent === "111111",
    "first mailbox result was not rendered",
  );

  elements.get("email").value = "second@zkgmail.com";
  await submit(elements);
  await eventually(
    () => elements.get("code").textContent === "222222",
    "second mailbox result was not rendered",
  );

  assert.deepEqual(requests[1].body, { email: "second@zkgmail.com" });
});


test("lookup keeps polling for three minutes before timing out", async () => {
  const { elements, requests } = loadPortal(
    () => response(404, { ok: false, state: "waiting" }),
  );
  elements.get("email").value = "slow@zkgmail.com";

  await submit(elements);
  await eventually(
    () => elements.get("statusTitle").textContent === "等待超时",
    "lookup did not reach the three-minute timeout",
  );

  assert.equal(requests.length, 45);
  assert.equal(
    elements.get("statusMessage").textContent,
    "3 分钟内没有收到验证码，请确认收件地址后重试。",
  );
});
