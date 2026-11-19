import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// No browser, package installation, server, or network connection is needed.
const source = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
const template = readFileSync(new URL("../templates/index.html", import.meta.url), "utf8");
const ui = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const reply = (body, status = 200) => new Response(JSON.stringify(body), { status });

test("every API request carries the page token, including GET", async () => {
  const calls = [];
  const request = ui.createApiClient({
    csrfToken: "test-token",
    fetchImpl: async (path, options) => {
      calls.push({ path, options });
      return reply({ ok: true });
    },
  });
  await request("/api/jobs");
  await request("/api/download", { method: "POST", body: { urls: "synthetic-link", format: "audio", quality: "192" } });
  assert.equal(calls.length, 2);
  for (const { options } of calls) {
    assert.equal(options.headers["X-CSRF-Token"], "test-token");
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.mode, "same-origin");
  }
  assert.equal(JSON.parse(calls[1].options.body).urls, "synthetic-link");
});

test("HTTP failure is not mistaken for a successful JSON response", async () => {
  const request = ui.createApiClient({
    csrfToken: "test-token", fetchImpl: async () => reply({ error: "Downloads are still active." }, 409),
  });
  await assert.rejects(request("/api/shutdown", { method: "POST" }), (error) => {
    assert.equal(error.status, 409);
    assert.match(error.message, /HTTP 409: Downloads are still active/);
    return true;
  });
});

test("non-JSON errors preserve the actual HTTP status", async () => {
  const request = ui.createApiClient({
    csrfToken: "test-token", fetchImpl: async () => new Response("not JSON", { status: 503 }),
  });
  await assert.rejects(request("/api/jobs"), (error) => error.status === 503 && /HTTP 503/.test(error.message));
});

test("missing session token fails without making a request", async () => {
  let called = false;
  const request = ui.createApiClient({ csrfToken: "", fetchImpl: async () => { called = true; return reply({}); } });
  await assert.rejects(request("/api/jobs"), /Reload this page/);
  assert.equal(called, false);
});

test("network failures expose an actionable message instead of transport internals", async () => {
  const request = ui.createApiClient({
    csrfToken: "test-token", fetchImpl: async () => { throw new TypeError("internal transport detail"); },
  });
  await assert.rejects(request("/api/open-folder", { method: "POST" }), /Cannot reach the local app/);
});

test("actions require explicit confirmation, not merely a 200 response", () => {
  assert.doesNotThrow(() => ui.requireConfirmation({ ok: true }));
  for (const data of [null, {}, { ok: false }, { ok: "true" }]) {
    assert.throws(() => ui.requireConfirmation(data), /did not confirm/);
  }
});

test("progress is finite and clamped; untrusted status cannot become a CSS class", () => {
  assert.equal(ui.clampPercent(150), 100);
  assert.equal(ui.clampPercent(-1), 0);
  assert.equal(ui.clampPercent("42.25"), 42.3);
  for (const value of [NaN, Infinity, "1%;color:red", {}, undefined]) assert.equal(ui.clampPercent(value), 0);
  const job = ui.normalizeJob({ title: "<img onerror=unexpected>", status: "done injected", percent: "100%" });
  assert.equal(job.status, "unknown");
  assert.equal(job.title, "<img onerror=unexpected>");
  assert.equal(ui.normalizeJob({ status: "skipped" }).label, "Already downloaded");
});

test("duplicate queue replies explain that no new job was created", () => {
  assert.match(ui.queuedMessage({ count: 0, queued: [], duplicates: [{ id: "fake-job" }] }), /already queued.*No new jobs/);
  assert.match(ui.queuedMessage({ count: 2, queued: ["a", "b"], duplicates: [{}] }), /Added 2 links.*1 link was already queued/);
  assert.throws(() => ui.queuedMessage({ count: "2", queued: [] }), /did not confirm/);
});

function fakeClock() {
  const timers = new Set();
  return {
    timers,
    setTimer(callback) { timers.add(callback); return callback; },
    clearTimer(callback) { timers.delete(callback); },
    take() { const callback = timers.values().next().value; timers.delete(callback); return callback; },
  };
}

test("polling never overlaps even when refresh is requested during a slow response", async () => {
  const clock = fakeClock();
  let calls = 0;
  let release;
  const poller = ui.createPoller({
    task: () => { calls += 1; return new Promise((resolve) => { release = resolve; }); },
    onError: assert.fail, ...clock,
  });
  poller.start();
  const first = poller.refresh();
  assert.equal(first, poller.refresh());
  await Promise.resolve();
  assert.equal(calls, 1);
  assert.equal(clock.timers.size, 0);
  release();
  await first;
  assert.equal(clock.timers.size, 1);
  const second = clock.take()();
  await Promise.resolve();
  assert.equal(calls, 2);
  poller.stop();
  release();
  await second;
  assert.equal(clock.timers.size, 0);
});

test("poll failures are reported and retried rather than silently ignored", async () => {
  const clock = fakeClock();
  const errors = [];
  const poller = ui.createPoller({
    task: () => { throw new Error("offline"); }, onError: (error) => errors.push(error.message), ...clock,
  });
  poller.start();
  await poller.refresh();
  assert.deepEqual(errors, ["offline"]);
  assert.equal(clock.timers.size, 1);
  poller.stop();
  assert.equal(clock.timers.size, 0);
});

// A deliberately small DOM double exercises action outcomes. It rejects
// innerHTML writes, making accidental interpolation of server text fail tests.
class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.dataset = {};
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
    this.classList = { toggle() {} };
  }
  set innerHTML(value) { throw new Error("Use textContent for DOM rendering"); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  querySelectorAll() { return this.children; }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  async emit(name, event = {}) { await this.listeners.get(name)?.(event); }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() { this.focused = true; }
}

function documentDouble() {
  const elements = new Map();
  for (const match of template.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const element = new Element(match[1]);
    element.hidden = /\bhidden\b/.test(match[0]);
    elements.set(match[2], element);
  }
  for (const format of ["audio", "video"]) {
    const button = new Element("button");
    button.dataset.format = format;
    elements.get("format-switch").append(button);
  }
  elements.get("quality").value = "192";
  return {
    getElementById: (id) => elements.get(id),
    querySelector: () => ({ content: "test-token" }),
    querySelectorAll: () => [...elements.values()].filter((element) => element.tagName === "button"),
    createElement: (tag) => new Element(tag),
    createDocumentFragment: () => new Element("fragment"),
  };
}

const nextTurn = () => new Promise((resolve) => setImmediate(resolve));

test("failed shutdown keeps the app visible; confirmed shutdown alone closes the view", async (context) => {
  let acceptShutdown = false;
  context.mock.method(globalThis, "fetch", async (path) => path === "/api/jobs" ? reply([])
    : acceptShutdown ? reply({ ok: true }) : reply({ error: "Downloads are still active." }, 409));
  const document = documentDouble();
  const app = ui.initializeApp(document, { addEventListener() {} });
  app.poller.stop();
  const find = document.getElementById;
  await find("shutdown").emit("click");
  await find("confirm-shutdown").emit("click");
  assert.equal(find("closed-view").hidden, true);
  assert.equal(find("app-view").hidden, false);
  assert.equal(find("shutdown-dialog").open, true);
  assert.match(find("shutdown-error").textContent, /HTTP 409/);
  acceptShutdown = true;
  await find("confirm-shutdown").emit("click");
  assert.equal(find("closed-view").hidden, false);
  assert.equal(find("app-view").hidden, true);
  assert.equal(find("closed-view").focused, true);
});

test("folder and clear failures are shown inline and controls recover", async (context) => {
  context.mock.method(globalThis, "fetch", async (path) => path === "/api/jobs" ? reply([])
    : reply({ error: "Action unavailable." }, 500));
  const document = documentDouble();
  const app = ui.initializeApp(document, { addEventListener() {} });
  app.poller.stop();
  for (const id of ["open-folder", "clear-finished"]) {
    await document.getElementById(id).emit("click");
    await nextTurn();
    assert.match(document.getElementById("message").textContent, /HTTP 500: Action unavailable/);
    assert.equal(document.getElementById("message").hidden, false);
    assert.equal(document.getElementById(id).disabled, false);
  }
});

test("polling failure is visible in the connection status", async (context) => {
  context.mock.method(globalThis, "fetch", async () => reply({ error: "Session expired." }, 403));
  const document = documentDouble();
  const app = ui.initializeApp(document, { addEventListener() {} });
  try {
    await app.poller.refresh();
    assert.match(document.getElementById("connection-status").textContent, /HTTP 403.*Retrying/);
  } finally {
    app.poller.stop();
  }
});

test("job titles and errors are rendered as text, with no HTML injection sink", async (context) => {
  const title = "<img src=invalid onerror=unexpected>";
  context.mock.method(globalThis, "fetch", async () => reply([{ title, status: "error", error: "<script>not code</script>" }]));
  const document = documentDouble();
  const app = ui.initializeApp(document, { addEventListener() {} });
  try {
    await app.poller.refresh();
    const [fragment] = document.getElementById("jobs").children;
    const [job] = fragment.children;
    assert.equal(job.children[0].children[0].textContent, title);
    assert.equal(job.children.at(-1).textContent, "Error: <script>not code</script>");
  } finally {
    app.poller.stop();
  }
});
