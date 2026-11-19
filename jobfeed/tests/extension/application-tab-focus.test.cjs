const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const context = vm.createContext({ URL });
vm.runInContext(fs.readFileSync(path.join(__dirname, "../../extensions/jobright-source/application-tab-focus.js"), "utf8"), context);
const { create, dashboardBase, pageIdentity } = context.ApplicationTabFocus;
const tick = () => new Promise(resolve => setImmediate(resolve));
const event = () => ({ listeners: [], addListener(fn) { this.listeners.push(fn); } });

function setup(storage = {}, fetchOverride) {
  const calls = [];
  const timers = new Map();
  let timer = 0;
  const tab = { id: 42, windowId: 7, url: "https://example.com/apply?job=4#form", active: false };
  const sockets = [];
  class Socket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.listeners = {}; this.readyState = 1; sockets.push(this); }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    emit(name, value = {}) { this.listeners[name]?.(value); }
    send(value) { calls.push(["send", JSON.parse(value)]); }
    close() { this.readyState = 3; this.emit("close", { code: 1000 }); }
  }
  const chrome = {
    runtime: { id: "extension-id", getURL: name => `chrome-extension://extension-id/${name}`, onMessage: event() },
    storage: { session: { get: async key => ({ [key]: storage[key] }), set: async value => Object.assign(storage, value) } },
    tabs: { query: async query => { calls.push(["query", query]); return [tab]; },
      get: async id => { calls.push(["get", id]); if (tab.closed) throw new Error("No tab"); return { ...tab }; },
      update: async (id, change) => { calls.push(["update", id, change]); Object.assign(tab, change); return { ...tab }; },
      onRemoved: event(), onReplaced: event() },
    windows: { update: async (id, change) => { calls.push(["window", id, change]); return { id, ...change }; },
      get: async id => ({ id, focused: true }) },
  };
  const controller = create({ chrome, WebSocket: Socket, AbortController, crypto: { randomUUID: () => "browser-session-1" },
    fetch: fetchOverride || (async (url, options) => { calls.push(["fetch", url, JSON.parse(options.body)]); return { ok: true,
      json: async () => ({ binding_id: "binding-1", session_token: "private-token" }) }; }),
    setTimeout: fn => { timers.set(++timer, fn); return timer; }, clearTimeout: id => timers.delete(id),
    setInterval: fn => { timers.set(++timer, fn); return timer; }, clearInterval: id => timers.delete(id) });
  async function pair() {
    const promise = controller.connectTab("https://jobfeed.localhost:1355", "pairing-code");
    await tick();
    const socket = sockets.at(-1);
    socket.emit("open");
    socket.emit("message", { data: JSON.stringify({ type: "ready" }) });
    await promise;
    return socket;
  }
  return { controller, calls, sockets, chrome, tab, storage, timers, pair };
}

test("only exact local dashboard origins and HTTP application pages are accepted", () => {
  assert.equal(dashboardBase("https://jobfeed.localhost:1355"), "https://jobfeed.localhost:1355");
  assert.equal(dashboardBase("http://127.0.0.1:7654/"), "http://127.0.0.1:7654");
  for (const value of ["https://jobfeed.localhost:1356", "https://evil.test", "http://localhost.evil.test", "https://user:secret@jobfeed.localhost:1355", "https://jobfeed.localhost:1355/path"]) assert.throws(() => dashboardBase(value));
  assert.equal(pageIdentity("https://example.com/form?a=1#step"), "https://example.com/form?a=1");
  assert.throws(() => pageIdentity("javascript:alert(1)"));
});

test("pairing uses the explicit active native tab and keeps token inside the extension", async () => {
  const s = setup();
  await s.pair();
  const posted = s.calls.find(call => call[0] === "fetch");
  assert.deepEqual(posted.slice(1), ["https://jobfeed.localhost:1355/api/application-tabs/connect", {
    code: "pairing-code", browser_session_id: "browser-session-1", native_tab_id: 42, url: "https://example.com/apply?job=4" }]);
  assert.equal(s.sockets[0].url, "wss://jobfeed.localhost:1355/api/application-tabs/bridge");
  assert.equal(s.calls.some(call => call[0] === "update"), false);
  assert.equal(s.storage.applicationTabFocus.bindings[0].session_token, "private-token");
});

test("focus activates the bound existing tab in its current window without navigation", async () => {
  const s = setup();
  const socket = await s.pair();
  s.tab.windowId = 99;
  socket.emit("message", { data: JSON.stringify({ type: "focus", request_id: "r1", binding_id: "binding-1", expires_at: Date.now() + 5000 }) });
  await tick();
  assert.deepEqual(JSON.parse(JSON.stringify(s.calls.filter(call => ["update", "window"].includes(call[0])))), [["update", 42, { active: true }], ["window", 99, { focused: true }]]);
  assert.equal(s.calls.at(-1)[1].status, "focused");
});

test("a different binding, changed page, and closed tab cannot focus a replacement", async () => {
  const s = setup();
  const socket = await s.pair();
  const send = binding_id => socket.emit("message", { data: JSON.stringify({ type: "focus", request_id: "r", binding_id, expires_at: Date.now() + 5000 }) });
  send("another-binding");
  await tick();
  assert.equal(s.calls.some(call => call[0] === "update"), false);
  s.tab.url = "https://example.com/apply?job=5";
  send("binding-1");
  await tick();
  assert.equal(s.calls.at(-1)[1].status, "page_changed");
  s.tab.closed = true;
  send("binding-1");
  await tick();
  assert.equal(s.calls.some(call => call[0] === "update"), false);
  assert.equal(s.storage.applicationTabFocus.bindings.length, 0);
});

test("only the extension popup can initiate pairing", async () => {
  const s = setup();
  const handler = s.chrome.runtime.onMessage.listeners[0];
  for (const sender of [{ id: "extension-id", url: "https://example.com" }, { id: "other", url: "chrome-extension://extension-id/popup.html" }]) {
    assert.equal(handler({ type: "application_tab_connect" }, sender, () => {}), false);
  }
  await tick();
  assert.equal(s.calls.some(call => call[0] === "fetch"), false);
});

test("expired or undated commands never change browser focus", async () => {
  const s = setup();
  const socket = await s.pair();
  for (const expires_at of [undefined, Date.now() - 1, "tomorrow"]) {
    socket.emit("message", { data: JSON.stringify({ type: "focus", request_id: "stale", binding_id: "binding-1", expires_at }) });
    await tick();
    assert.equal(s.calls.at(-1)[1].status, "focus_failed");
  }
  assert.equal(s.calls.some(call => ["update", "window"].includes(call[0])), false);
});

test("session restores exact bindings without POST and auth rejection stops reconnects", async () => {
  const first = setup();
  await first.pair();
  const s = setup(first.storage);
  await s.controller.restore();
  assert.equal(s.sockets.length, 1);
  assert.equal(s.calls.some(call => call[0] === "fetch"), false);
  assert.equal(s.storage.applicationTabFocus.browser_session_id, "browser-session-1");
  s.sockets[0].emit("close", { code: 1008 });
  await tick();
  assert.equal(s.storage.applicationTabFocus.bindings.length, 0);
  assert.equal(s.timers.size, 0);
});

test("removed and replaced native tabs invalidate bindings", async () => {
  for (const replaced of [false, true]) {
    const s = setup();
    await s.pair();
    if (replaced) s.chrome.tabs.onReplaced.listeners[0](84, 42);
    else s.chrome.tabs.onRemoved.listeners[0](42);
    await tick();
    assert.equal(s.storage.applicationTabFocus.bindings.length, 0);
    assert.equal(s.timers.size, 0);
  }
});

test("pairing timeout aborts a stalled request and exposes no request secrets", async () => {
  const s = setup({}, (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new Error("private-token")));
  }));
  const result = s.controller.connectTab("https://jobfeed.localhost:1355", "private-code");
  const rejected = assert.rejects(result, { message: "Cannot reach the local dashboard. Check its address and try again." });
  await tick();
  [...s.timers.values()][0]();
  await rejected;
  assert.equal(s.timers.size, 0);
  assert.equal(s.sockets.length, 0);
});

test("profile probes require the exact native ID and full URL in this profile", async () => {
  const s = setup();
  const command = { request_id: "probe-1", expires_at: Date.now() + 5000, action: "probe",
    candidates: [{ native_tab_id: 42, url: s.tab.url }, { native_tab_id: 43, url: s.tab.url },
      { native_tab_id: 42, url: s.tab.url + "-wrong" }] };
  const result = await s.controller.bridgeCommand(command);
  assert.equal(result.browser_session_id, "browser-session-1");
  assert.deepEqual(JSON.parse(JSON.stringify(result.matches)), [command.candidates[0]]);
  assert.equal(s.calls.some(call => ["fetch", "update", "window", "query"].includes(call[0])), false);
  await assert.rejects(s.controller.bridgeCommand({ ...command, request_id: "expired", expires_at: Date.now() - 1 }), /expired/);
  await assert.rejects(s.controller.bridgeCommand({ ...command, request_id: "disconnected" }, () => false), /disconnected/);
});

test("focus reports a changed page during activation without navigating it", async () => {
  const s = setup();
  const socket = await s.pair();
  s.chrome.windows.update = async () => { s.tab.url = "https://example.com/different-job"; return { focused: true }; };
  socket.emit("message", { data: JSON.stringify({ type: "focus", request_id: "race", binding_id: "binding-1", expires_at: Date.now() + 5000 }) });
  await tick();
  assert.equal(s.calls.at(-1)[1].status, "page_changed");
  assert.deepEqual(JSON.parse(JSON.stringify(s.calls.filter(call => call[0] === "update"))), [["update", 42, { active: true }]]);
  assert.equal(s.tab.url, "https://example.com/different-job");
});

test("automatic pairing targets the verified tab and duplicate delivery pairs only once", async () => {
  const s = setup();
  const command = { request_id: "auto-1", expires_at: Date.now() + 5000, action: "connect",
    browser_session_id: "browser-session-1", native_tab_id: 42, url: s.tab.url,
    base_url: "https://jobfeed.localhost:1355", code: "one-use" };
  const first = s.controller.bridgeCommand(command);
  const duplicate = s.controller.bridgeCommand(command);
  await tick();
  assert.equal(s.calls.filter(call => call[0] === "fetch").length, 1);
  assert.equal(s.calls.some(call => call[0] === "query"), false);
  let finished = false;
  first.then(() => { finished = true; });
  await tick();
  assert.equal(finished, false);
  s.sockets[0].emit("open");
  s.sockets[0].emit("message", { data: JSON.stringify({ type: "ready" }) });
  assert.equal((await first).binding_id, "binding-1");
  assert.equal((await duplicate).binding_id, "binding-1");
});

test("automatic pairing rejects other profile sessions and changed application URLs", async () => {
  const s = setup();
  const command = { request_id: "auto-2", expires_at: Date.now() + 5000, action: "connect",
    browser_session_id: "browser-session-1", native_tab_id: 42, url: s.tab.url,
    base_url: "https://jobfeed.localhost:1355", code: "one-use" };
  await assert.rejects(s.controller.bridgeCommand({ ...command, browser_session_id: "other" }), /profile session changed/);
  await assert.rejects(s.controller.bridgeCommand({ ...command, request_id: "auto-3", url: s.tab.url + "-changed" }), /changed URL/);
  assert.equal(s.calls.some(call => call[0] === "fetch"), false);
});
