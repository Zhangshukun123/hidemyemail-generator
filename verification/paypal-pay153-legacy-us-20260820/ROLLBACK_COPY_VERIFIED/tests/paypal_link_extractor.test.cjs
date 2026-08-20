const assert = require("node:assert/strict");
const path = require("node:path");

const target = path.resolve(process.argv[2] || "");
const expectation = process.argv[3] || "modified";
delete require.cache[target];
const subject = require(target);

if (expectation === "baseline") {
  assert.equal(typeof subject.copyVisibleResult, "function");
  const visible = subject.copyVisibleResult({
    querySelector() {
      return { href: "https://www.paypal.com/agreements/approve?ba_token=BA-BASELINE" };
    },
  });
  assert.match(visible, /BA-BASELINE/);
  assert.equal(subject.LinkModel, undefined);
  console.log("BASELINE PASS: #resultReturn.href supported; structured result fields unavailable");
  process.exit(0);
}

if (expectation === "v1-baseline") {
  const baselineModel = new subject.LinkModel("https://pp.uplw.uno/");
  assert.equal(baselineModel.normalize("-"), "https://pp.uplw.uno/-");
  assert.equal(subject.ApiCaptureBridge, undefined);
  console.log("BASELINE PASS: placeholder '-' reproduces https://pp.uplw.uno/-; Job API bridge unavailable");
  process.exit(0);
}

assert.deepEqual(subject.RESULT_FIELDS.slice(0, 3), [
  "paypal_approve_url",
  "final_redirect_url",
  "paypal_return_url",
]);

const model = new subject.LinkModel("https://pp.uplw.uno/");
assert.equal(model.normalize("-"), "");
assert.equal(model.normalize("https://pp.uplw.uno/-"), "");
const structured = model.extractStructured({
  result: {
    paypal_approve_url: "https://www.paypal.com/agreements/approve?ba_token=BA-PRIMARY&country.x=US",
    final_redirect_url: "https://merchant.example/complete",
    paypal_return_url: "https://merchant.example/return",
  },
  logs: [{ message: "authorize 1/5 http://us.cliproxy.io:3010" }],
});
assert.equal(structured[0].field, "result.paypal_approve_url");
assert.match(structured[0].url, /BA-PRIMARY/);
assert.equal(structured.length, 3);

const escaped = model.extractUrlsFromText(
  "PayPal 提链成功 https://www.paypal.com/agreements/approve?ba_token=BA-ESCAPED&amp;country.x=US。",
);
assert.equal(escaped.length, 1);
assert.match(escaped[0], /country\.x=US/);
assert.equal(new URL(escaped[0]).searchParams.get("country.x"), "US");

const domView = new subject.LinkView({
  querySelector(selector) {
    return selector === "#resultReturn"
      ? {
          getAttribute: () => "https://www.paypal.com/agreements/approve?ba_token=BA-DOM",
          textContent: "https://www.paypal.com/agreements/approve?ba_token=BA-DOM",
        }
      : null;
  },
  querySelectorAll() {
    return [];
  },
});
const domCandidates = domView.collectCandidates(model);
assert.equal(domCandidates[0].field, "#resultReturn.href");
assert.match(domCandidates[0].url, /BA-DOM/);

const added = model.capture([...structured, ...structured]);
assert.equal(added.length, 3);
assert.equal(model.history.length, 3);
assert.equal(model.latest().kind, "PayPal BA");
assert.equal(model.normalize("https://pp.uplw.uno/api/jobs/123"), "");

assert.deepEqual(model.extractJobIds({ job_id: "12345678-abcd", job: { id: "job-87654321" } }), [
  "12345678-abcd",
  "job-87654321",
]);
const repository = new subject.JobRepository(() => {}, "https://pp.uplw.uno/");
assert.equal(repository.jobUrl("12345678-abcd"), "https://pp.uplw.uno/api/jobs/12345678-abcd");

global.CustomEvent = class CustomEvent {
  constructor(type, options) {
    this.type = type;
    this.detail = options.detail;
  }
};
let capturedEvent = null;
const fakeResponse = {
  clone: () => ({ json: async () => ({ job_id: "12345678-abcd" }) }),
};
const fakePage = {
  location: { href: "https://pp.uplw.uno/", origin: "https://pp.uplw.uno" },
  fetch: async () => fakeResponse,
};
const fakeDocument = { dispatchEvent: (event) => { capturedEvent = event; } };
subject.ApiCaptureBridge.install(fakePage, fakeDocument);

(async () => {
  await fakePage.fetch("/api/jobs", { method: "POST" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(capturedEvent.type, subject.API_CAPTURE_EVENT);
  assert.equal(capturedEvent.detail.payload.job_id, "12345678-abcd");
  assert.equal(capturedEvent.detail.method, "POST");
  console.log("MODIFIED PASS: placeholders rejected; Job API capture/polling, field priority, DOM/text capture, and dedupe verified");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
