// Exercise the real page functions with controllable transport and a small DOM facade.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/description_pipeline/orchestration/static/app.js", import.meta.url), "utf8")
  .replace(/^import[\s\S]*?from "[^"]+";\n/gm, "")
  .replace("void boot();", "");
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
const response = (status, payload, raw = false) => ({
  ok: status >= 200 && status < 300,
  status,
  text: async () => raw ? payload : JSON.stringify(payload),
});
function element(id = "") {
  const node = {
    id, hidden: false, disabled: false, value: "", textContent: "", children: [], handlers: {}, style: {}, dataset: {},
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener(name, handler) { this.handlers[name] = handler; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    setAttribute() {}, focus() {}, select() {},
  };
  let text = "";
  Object.defineProperty(node, "textContent", {
    get: () => text,
    set: (value) => { text = value; node.children = []; },
  });
  return node;
}
function page() {
  const elements = new Map();
  const $ = (id) => {
    if (!elements.has(id)) elements.set(id, element(id));
    return elements.get(id);
  };
  const timeouts = new Map();
  const requests = [];
  let nextTimer = 0, nextKey = 0;
  class XHR {
    constructor() { this.handlers = {}; this.upload = element(); this.headers = {}; this.finished = false; }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(name, value) { this.headers[name] = value; }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    send(form) { this.form = form; requests.push(this); }
    finish(status, payload, raw = false) {
      this.finished = true; this.status = status; this.responseText = raw ? payload : JSON.stringify(payload); this.handlers.load();
    }
    abort() { if (!this.finished) { this.finished = true; this.handlers.abort?.(); } }
    disconnect() { this.finished = true; this.handlers.error(); }
  }
  const context = vm.createContext({
    console, AbortController, XMLHttpRequest: XHR,
    FormData: class { entries = []; append(...entry) { this.entries.push(entry); } },
    crypto: { randomUUID: () => `submission-${++nextKey}` },
    document: { getElementById: $, createElement: () => element(), querySelectorAll: () => [], querySelector: () => element() },
    window: {
      setTimeout: (callback) => { const id = ++nextTimer; timeouts.set(id, callback); return id; },
      clearTimeout: (id) => timeouts.delete(id), setInterval: () => ++nextTimer, clearInterval() {},
    },
    localStorage: { getItem: () => null, setItem() {} },
    requestAnimationFrame: (callback) => callback(),
    shouldRefreshRunList: () => false,
    fetch: async () => response(200, { runs: [] }),
  });
  vm.runInContext(`${source}\nglobalThis.app = {state, wire, api, poll, selectRun, showUploadView, clearSession, stopRunRead, refreshRuns, loadMoreRuns, submitRun, retryRun, rerunFromStage, renderRerunPanel, folderPick, changeSubmission, loadTargets};`, context);
  context.clearPreview = () => {};
  context.renderRun = (run) => { context.displayed = run; };
  context.loadPreview = async () => {};
  context.refreshRuns = async () => {};
  const app = context.app;
  app.state.user = "operator";
  app.state.csrf = "session-csrf";
  $("login-card").hidden = true;
  return { context, app, $, timeouts, requests };
}
function uploadReady(p) {
  p.app.state.folderPick = { top: "robot", assemblies: ["main.SLDASM"], records: [] };
  p.app.state.assemblyChoice = "main.SLDASM";
  p.$("target-select").value = "target-a";
  p.$("purpose-select").value = "engineering";
  p.$("change-summary").value = "A delivery request";
  p.app.wire();
}
const submit = (p) => p.$("run-form").handlers.submit({ preventDefault() {} });

// Exactly one read owns the selected run; the next poll starts only after completion.
{
  const p = page(); const pending = deferred(); let calls = 0;
  p.app.state.dagRunId = "run-a";
  p.context.fetch = async () => { calls++; return pending.promise; };
  const first = p.app.poll(); await p.app.poll();
  assert.equal(calls, 1);
  assert.equal(p.timeouts.size, 0);
  pending.resolve(response(200, { dag_run_id: "run-a", state: "success", automatic: { state: "passed" } }));
  await first;
  assert.equal(p.context.displayed.state, "success");
  assert.equal(p.app.state.pollRequest, null);
  assert.equal(p.timeouts.size, 1, "Terminal runs must continue canonical polling");
}
// Abort and ownership checks also reject the old response after A -> B -> A.
{
  const p = page(); const pending = [deferred(), deferred(), deferred()]; const signals = []; let calls = 0;
  p.context.fetch = async (_url, options) => { signals.push(options.signal); return pending[calls++].promise; };
  p.app.state.dagRunId = "run-a";
  const oldA = p.app.poll();
  const b = p.app.selectRun("run-b");
  const newA = p.app.selectRun("run-a");
  assert.equal(signals[0].aborted, true); assert.equal(signals[1].aborted, true);
  pending[2].resolve(response(200, { dag_run_id: "run-a", state: "failed" })); await newA;
  pending[0].resolve(response(200, { dag_run_id: "run-a", state: "success" })); await oldA;
  pending[1].resolve(response(200, { dag_run_id: "run-b", state: "success" })); await b;
  assert.equal(p.app.state.lastRun.state, "failed");
  assert.equal(p.context.displayed.state, "failed");
  assert.equal(p.timeouts.size, 1);
}
// Selection immediately hides all old report content, including when the new read fails.
{
  const p = page(); const pending = deferred();
  p.$("run-heading").textContent = "Old successful run";
  p.$("run-state-label").textContent = "Succeeded";
  p.context.fetch = async () => pending.promise;
  const selected = p.app.selectRun("run-b");
  assert.equal(p.$("run-heading").textContent, "Loading run...");
  assert.equal(p.$("run-state-label").textContent, "Loading");
  assert.equal(p.$("run-content").hidden, true);
  pending.resolve(response(502, { error: "Read unavailable" })); await selected;
  assert.equal(p.$("run-content").hidden, true);
  assert.equal(p.$("run-error-detail").textContent, "Read unavailable");
}
// New delivery view invalidates the old selection and any pending response.
{
  const p = page(); const pending = deferred(); let signal;
  p.app.state.dagRunId = "run-a";
  p.context.fetch = async (_url, options) => { signal = options.signal; return pending.promise; };
  const poll = p.app.poll(); p.app.wire();
  p.$("new-run-button").handlers.click();
  assert.equal(signal.aborted, true); assert.equal(p.app.state.dagRunId, null);
  pending.resolve(response(200, { dag_run_id: "run-a", state: "success" })); await poll;
  assert.equal(p.context.displayed, undefined); assert.equal(p.timeouts.size, 0);
}
// A history-only 401 uses the same session invalidation as a detail request.
{
  const p = page(); p.context.fetch = async () => response(401, { error: "Sign in again" });
  await p.app.refreshRuns();
  assert.equal(p.app.state.user, null); assert.equal(p.$("login-card").hidden, false);
  assert.equal(p.$("login-error").textContent, "Sign in again");
}
// A late 401 from an older session must not sign out a newly adopted session.
{
  const p = page(); const pending = deferred(); p.context.fetch = async () => pending.promise;
  const old = p.app.api("/api/runs"); p.app.state.csrf = "new-session";
  pending.resolve(response(401, { error: "Old session expired" })); await assert.rejects(old);
  assert.equal(p.app.state.csrf, "new-session");
}
// Invalid success, network loss and cancellation preserve the exact upload identity.
for (const failure of ["html", "missing-id", "wrong-id", "null", "network", "cancel"]) {
  const p = page(); uploadReady(p); p.context.selectRun = async () => {};
  const first = submit(p); const request = p.requests[0]; const key = request.headers["Idempotency-Key"];
  await submit(p); assert.equal(p.requests.length, 1, "Repeated click must not create another request");
  if (failure === "html") request.finish(200, "<html>Sign in</html>", true);
  if (failure === "missing-id") request.finish(201, {});
  if (failure === "wrong-id") request.finish(201, { dag_run_id: "portal-some-other-request" });
  if (failure === "null") request.finish(201, null);
  if (failure === "network") request.disconnect();
  if (failure === "cancel") p.$("cancel-button").handlers.click();
  await first;
  assert.equal(p.app.state.submissionKey, key);
  assert.ok(p.app.state.folderPick);
  assert.equal(p.$("start-button").textContent, "Retry same submission");
  assert.equal(p.requests.length, 1, "No failure may trigger an automatic upload retry");
  const retry = submit(p); assert.equal(p.requests[1].headers["Idempotency-Key"], key);
  p.requests[1].finish(201, { dag_run_id: `portal-${key}` }); await retry;
  assert.equal(p.app.state.submissionKey, null);
}
// Session loss does not discard a potentially admitted upload identity.
{
  const p = page(); uploadReady(p);
  const pending = submit(p); const key = p.requests[0].headers["Idempotency-Key"];
  p.requests[0].finish(401, { error: "Expired", dag_run_id: `portal-${key}` }); await pending;
  assert.equal(p.app.state.submissionKey, key); assert.ok(p.app.state.folderPick);
  assert.equal(p.$("login-card").hidden, false);
}
// Actual filenames are preserved; a backslash cannot be turned into a directory separator.
{
  const p = page();
  const rejected = p.app.folderPick([{ name: "frame\\housing.SLDPRT", webkitRelativePath: "robot/frame\\housing.SLDPRT", size: 1 }]);
  assert.match(rejected.error, /Unsupported filename/);
  const accepted = p.app.folderPick([{ name: "main.SLDASM", webkitRelativePath: "robot/main.SLDASM", size: 1 }]);
  assert.equal(accepted.records[0].path, "robot/main.SLDASM");
}
// A new Freeze user action has a new key, but unconfirmed retries keep the same key.
{
  const p = page(); const calls = []; let outcome = 502;
  const run = { dag_run_id: "parent-run", main_assembly: "main.SLDASM", can_manage: true,
    stage_reruns: [{ stage: "freeze", eligible: true, main_assembly_options: ["main.SLDASM", "other.SLDASM"] }] };
  const selectParent = () => { p.app.state.dagRunId = run.dag_run_id; p.app.state.lastRun = run; };
  selectParent(); p.context.poll = async () => {}; p.context.selectRun = async () => {};
  p.context.fetch = async (_url, options) => {
    calls.push(options);
    return outcome === 502 ? response(502, { error: "Unconfirmed" }) : response(202, { dag_run_id: `portal-freeze-${options.headers["Idempotency-Key"]}`, resume_from: "freeze" });
  };
  await p.app.rerunFromStage("freeze");
  const firstKey = calls[0].headers["Idempotency-Key"];
  await p.app.rerunFromStage("freeze");
  assert.equal(calls[1].headers["Idempotency-Key"], firstKey);
  outcome = 202; await p.app.rerunFromStage("freeze");
  assert.equal(calls[2].headers["Idempotency-Key"], firstKey);
  selectParent(); await p.app.rerunFromStage("freeze");
  assert.notEqual(calls[3].headers["Idempotency-Key"], firstKey);
  outcome = 502; selectParent(); await p.app.rerunFromStage("freeze");
  const pendingKey = p.app.state.rerunKeyByRun[run.dag_run_id];
  p.app.state.stages = [{ id: "freeze" }]; p.app.state.selectedStage = 0;
  p.app.renderRerunPanel(run);
  const mainSelect = p.$("stage-rerun").children[0].children[1];
  mainSelect.value = "other.SLDASM"; mainSelect.handlers.change();
  assert.equal(p.app.state.rerunKeyByRun[run.dag_run_id], undefined);
  await p.app.rerunFromStage("freeze");
  assert.notEqual(calls.at(-1).headers["Idempotency-Key"], pendingKey);
  assert.equal(JSON.parse(calls.at(-1).body).main_assembly, "other.SLDASM");
}
// Sign out invalidates an in-flight read even if its transport later delivers a response.
{
  const p = page(); const pending = deferred(); let readSignal;
  p.app.state.dagRunId = "run-a";
  p.context.fetch = async (url, options) => {
    if (url === "/api/session") return response(200, { authenticated: false });
    readSignal = options.signal;
    return pending.promise;
  };
  p.app.wire();
  const reading = p.app.poll(); await p.$("logout").handlers.click();
  assert.equal(readSignal.aborted, true); assert.equal(p.app.state.dagRunId, null);
  pending.resolve(response(200, { dag_run_id: "run-a", state: "success" })); await reading;
  assert.equal(p.context.displayed, undefined); assert.equal(p.timeouts.size, 0);
}
// The owner remains held while preview work is pending, so slow previews cannot create overlap.
{
  const p = page(); const preview = deferred(); let calls = 0;
  p.app.state.dagRunId = "run-a";
  p.context.fetch = async () => { calls++; return response(200, { dag_run_id: "run-a", state: "success", automatic: { state: "passed" } }); };
  p.context.loadPreview = () => preview.promise;
  const reading = p.app.poll();
  await new Promise((resolve) => setImmediate(resolve));
  await p.app.poll(); assert.equal(calls, 1); assert.equal(p.timeouts.size, 0);
  preview.resolve(); await reading; assert.equal(p.timeouts.size, 1);
}
// Editing the actual form starts a deliberate new intent; unchanged retry keeps the old key.
{
  const p = page(); uploadReady(p);
  const first = submit(p); const key = p.requests[0].headers["Idempotency-Key"];
  assert.equal(p.$("change-summary").disabled, true);
  p.requests[0].disconnect(); await first;
  p.context.fetch = async () => response(200, { targets: [{ hardware_id: "target-a", repository_slug: "example/repository", base: "main" }] });
  await p.app.loadTargets(p.app.state.folderPick);
  p.$("change-summary").value = "A deliberate changed request";
  p.$("change-summary").oninput();
  assert.equal(p.app.state.submissionKey, null); assert.equal(p.app.state.blockedPick, false);
  const next = submit(p); const nextKey = p.requests[1].headers["Idempotency-Key"];
  assert.notEqual(nextKey, key);
  p.requests[1].abort(); await next;
}
// A failed authoritative read revokes the visible success without inventing a business state.
{
  const p = page(); let failRead = false; let previewClears = 0;
  p.app.state.dagRunId = "run-a";
  p.context.clearPreview = () => {
    previewClears++;
    p.app.state.previewSubject = null;
    p.$("viewer-card").hidden = true;
  };
  p.context.loadPreview = async () => {
    p.app.state.previewSubject = "verified-subject";
    p.$("viewer-card").hidden = false;
  };
  p.context.fetch = async () => failRead
    ? response(502, { error: "Authoritative read unavailable" })
    : response(200, { dag_run_id: "run-a", state: "success", automatic: { state: "passed" } });
  await p.app.poll();
  assert.equal(p.app.state.lastRun.state, "success");
  assert.equal(p.app.state.previewSubject, "verified-subject");
  failRead = true; await p.app.poll();
  assert.equal(p.app.state.lastRun, null);
  assert.equal(p.app.state.previewSubject, null);
  assert.equal(previewClears, 1);
  assert.equal(p.$("viewer-card").hidden, true);
  assert.equal(p.$("run-content").hidden, true);
  assert.equal(p.$("run-state-label").textContent, "Unknown");
  assert.equal(p.$("run-loading").hidden, false);
  assert.equal(p.$("run-actions").textContent, "");
  assert.equal(p.$("retry-panel").hidden, true);
  assert.equal(p.$("run-error-detail").textContent, "Authoritative read unavailable");
  assert.equal(p.timeouts.size, 1);
}
// Every caller leaves 401 handling to the request's captured session, including action requests.
for (const action of ["retryRun", "rerunFromStage"]) {
  const p = page(); const pending = deferred();
  const run = { dag_run_id: "parent-run", main_assembly: "main.SLDASM", can_manage: true,
    retry: { eligible: true },
    stage_reruns: [{ stage: "freeze", eligible: true, main_assembly_options: ["main.SLDASM"] }] };
  p.app.state.dagRunId = run.dag_run_id; p.app.state.lastRun = run;
  p.context.fetch = async () => pending.promise;
  const request = action === "retryRun" ? p.app.retryRun() : p.app.rerunFromStage("freeze");
  p.app.state.csrf = "replacement-session"; p.app.state.user = "replacement-operator";
  pending.resolve(response(401, { error: "Expired earlier session" })); await request;
  assert.equal(p.app.state.csrf, "replacement-session");
  assert.equal(p.app.state.user, "replacement-operator");
  assert.equal(p.$("login-card").hidden, true);
  assert.equal(p.$("login-error").textContent, "");
}
// Matching action failures invalidate once; their catch handlers never clear a second time.
for (const action of ["retryRun", "rerunFromStage"]) {
  const p = page(); let invalidations = 0;
  const clear = p.app.clearSession;
  p.context.clearSession = () => { invalidations++; clear(); };
  const run = { dag_run_id: "parent-run", main_assembly: "main.SLDASM", can_manage: true,
    retry: { eligible: true },
    stage_reruns: [{ stage: "freeze", eligible: true, main_assembly_options: ["main.SLDASM"] }] };
  p.app.state.dagRunId = run.dag_run_id; p.app.state.lastRun = run;
  p.context.fetch = async () => response(401, { error: "Current session expired" });
  if (action === "retryRun") await p.app.retryRun(); else await p.app.rerunFromStage("freeze");
  assert.equal(invalidations, 1);
  assert.equal(p.app.state.user, null);
  assert.equal(p.$("login-error").textContent, "Current session expired");
}
// An old upload's 401 cannot erase or show an expiry message over a new session either.
{
  const p = page(); uploadReady(p);
  const request = submit(p);
  p.app.state.csrf = "replacement-session"; p.app.state.user = "replacement-operator";
  p.requests[0].finish(401, { error: "Expired earlier upload session" }); await request;
  assert.equal(p.app.state.csrf, "replacement-session");
  assert.equal(p.$("login-card").hidden, true);
  assert.equal(p.$("login-error").textContent, "");
}
// A different but syntactically valid child cannot acknowledge this Freeze operation.
{
  const p = page(); let correct = false; const calls = [];
  const run = { dag_run_id: "parent-run", main_assembly: "main.SLDASM", can_manage: true,
    stage_reruns: [{ stage: "freeze", eligible: true, main_assembly_options: ["main.SLDASM"] }] };
  p.app.state.dagRunId = run.dag_run_id; p.app.state.lastRun = run;
  p.context.poll = async () => {}; p.context.selectRun = async () => {};
  p.context.fetch = async (_url, options) => {
    calls.push(options);
    return response(202, { dag_run_id: correct ? `portal-freeze-${options.headers["Idempotency-Key"]}` : "portal-freeze-other-request" });
  };
  await p.app.rerunFromStage("freeze");
  const key = calls[0].headers["Idempotency-Key"];
  assert.equal(p.app.state.rerunKeyByRun[run.dag_run_id], key);
  assert.match(p.app.state.rerunError, /different request identity/);
  correct = true; await p.app.rerunFromStage("freeze");
  assert.equal(calls[1].headers["Idempotency-Key"], key);
  assert.equal(p.app.state.rerunKeyByRun[run.dag_run_id], undefined);
}
// A fresh list read replaces the prior owner; late data or errors cannot revoke newer state.
for (const oldStatus of [200, 502]) {
  const p = page(); const pending = [deferred(), deferred()]; const signals = []; let calls = 0;
  p.context.renderRunsList = () => {};
  p.context.fetch = async (_url, options) => {
    signals.push(options.signal);
    return pending[calls++].promise;
  };
  const old = p.app.refreshRuns();
  const fresh = p.app.refreshRuns();
  assert.equal(signals[0].aborted, true);
  pending[1].resolve(response(200, { runs: [{ dag_run_id: "run-a", state: "unresolved" }], next_offset: 30 }));
  await fresh;
  pending[0].resolve(response(oldStatus, oldStatus === 200
    ? { runs: [{ dag_run_id: "run-a", state: "success" }], next_offset: 90 }
    : { error: "Stale read failure" }));
  await old;
  assert.equal(p.app.state.runsList[0].state, "unresolved");
  assert.equal(p.app.state.runsNextOffset, 30);
  assert.equal(p.app.state.listRequest, null);
}
// Refresh invalidates pagination based on the old list, including its late finally block.
{
  const p = page(); const pending = [deferred(), deferred()]; const signals = []; let calls = 0;
  p.context.renderRunsList = () => {};
  p.app.state.runsList = [{ dag_run_id: "old-first-page" }];
  p.app.state.runsNextOffset = 30;
  p.context.fetch = async (url, options) => {
    signals.push(options.signal);
    if (calls === 0) assert.match(url, /offset=30$/);
    return pending[calls++].promise;
  };
  const more = p.app.loadMoreRuns();
  const fresh = p.app.refreshRuns();
  await p.app.loadMoreRuns();
  assert.equal(calls, 2, "Pagination must wait while a new baseline is loading");
  assert.equal(signals[0].aborted, true);
  const freshOwner = p.app.state.listRequest;
  pending[0].resolve(response(200, { runs: [{ dag_run_id: "old-second-page" }], next_offset: 60 }));
  await more;
  assert.equal(p.app.state.listRequest, freshOwner, "Old pagination cleanup cannot clear the refresh owner");
  pending[1].resolve(response(200, { runs: [{ dag_run_id: "new-first-page" }], next_offset: null }));
  await fresh;
  assert.equal(p.app.state.runsList.length, 1);
  assert.equal(p.app.state.runsList[0].dag_run_id, "new-first-page");
  assert.equal(p.app.state.runsNextOffset, null);
  assert.equal(p.app.state.runsLoadingMore, false);
}
// A completed refresh followed by a late old page also cannot append to the new baseline.
{
  const p = page(); const moreResult = deferred();
  p.context.renderRunsList = () => {};
  p.app.state.runsList = [{ dag_run_id: "old-first-page" }];
  p.app.state.runsNextOffset = 30;
  p.context.fetch = async (url) => /offset=/.test(url) ? moreResult.promise
    : response(200, { runs: [{ dag_run_id: "new-first-page" }], next_offset: 10 });
  const more = p.app.loadMoreRuns(); await p.app.refreshRuns();
  moreResult.resolve(response(200, { runs: [{ dag_run_id: "old-second-page" }], next_offset: 60 }));
  await more;
  assert.equal(p.app.state.runsList.length, 1);
  assert.equal(p.app.state.runsList[0].dag_run_id, "new-first-page");
  assert.equal(p.app.state.runsNextOffset, 10);
}
// Successful pagination still extends exactly its current baseline and releases the owner.
{
  const p = page(); p.context.renderRunsList = () => {};
  p.app.state.runsList = [{ dag_run_id: "first-page" }];
  p.app.state.runsNextOffset = 30;
  p.context.fetch = async () => response(200, { runs: [{ dag_run_id: "second-page" }], next_offset: null });
  await p.app.loadMoreRuns();
  assert.equal(p.app.state.runsList.length, 2);
  assert.equal(p.app.state.runsList[1].dag_run_id, "second-page");
  assert.equal(p.app.state.runsLoadingMore, false);
  assert.equal(p.app.state.listRequest, null);
}
// Sign out revokes both list and pagination readers before clearing account-visible rows.
for (const read of ["refreshRuns", "loadMoreRuns"]) {
  const p = page(); const pending = deferred(); let signal;
  p.context.renderRunsList = () => {};
  p.app.state.runsList = [{ dag_run_id: "private-first-page" }];
  p.app.state.runsNextOffset = 30;
  p.context.fetch = async (_url, options) => { signal = options.signal; return pending.promise; };
  const reading = p.app[read](); p.app.clearSession();
  assert.equal(signal.aborted, true);
  pending.resolve(response(200, { runs: [{ dag_run_id: "late-private-page" }], next_offset: 60 }));
  await reading;
  assert.equal(p.app.state.runsList.length, 0);
  assert.equal(p.app.state.runsNextOffset, null);
  assert.equal(p.app.state.runsLoadingMore, false);
  assert.equal(p.app.state.listRequest, null);
}
console.log("submission_flow_test OK");

// Canonical UNKNOWN keeps polling and ticks only while its authorized wait is proven.
for (const automaticState of ["waiting", "unresolved"]) {
  const p = page(); let previews = 0;
  p.app.state.dagRunId = "run-wait";
  p.context.fetch = async () => response(200, {dag_run_id: "run-wait", state: "unresolved", automatic: {state: automaticState}, verification_reports: []});
  p.context.loadPreview = async () => { previews++; };
  await p.app.poll();
  assert.equal(previews, 0);
  assert.equal(p.timeouts.size, 1);
  assert.equal(p.app.state.activityTimer !== null, automaticState === "waiting");
}
{
  const p = page(); let previews = 0;
  p.app.state.dagRunId = "run-failed";
  p.context.fetch = async () => response(200, {dag_run_id: "run-failed", state: "failed", automatic: {state: "failed", message: "Capture failed"}, verification_reports: []});
  p.context.loadPreview = async () => { previews++; };
  await p.app.poll();
  assert.equal(previews, 0);
  assert.equal(p.app.state.activityTimer, null);
  assert.equal(p.timeouts.size, 1);
}
console.log("unfinished preview and activity polling OK");
