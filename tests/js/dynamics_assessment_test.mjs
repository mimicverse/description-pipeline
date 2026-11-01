// Exercise the production UI module's requests, disclosure and source-switch races.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../../src/description_pipeline/orchestration/static/dynamics_assessment.js", import.meta.url), "utf8");
const { mountDynamicsAssessment, assessmentStateLabel } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`
);

class Node {
  constructor(tag, document) { this.tagName = tag; this.ownerDocument = document; this.children = []; this.handlers = {}; this.attributes = {}; this.value = ""; }
  set textContent(value) { this.value = value; this.children = []; }
  get textContent() { return this.value + this.children.map(node => node.textContent).join(" "); }
  set innerHTML(_value) { throw new Error("Assessment data must not be interpreted as markup"); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.value = ""; this.children = nodes; }
  addEventListener(name, handler) { (this.handlers[name] ||= []).push(handler); }
  setAttribute(name, value) { this.attributes[name] = value; }
  async emit(name) { if (!this.disabled) for (const handler of this.handlers[name] || []) await handler({ currentTarget: this }); }
}
const document = { createElement: tag => new Node(tag, document) };
const container = () => new Node("div", document);
const descendants = node => [node, ...node.children.flatMap(descendants)];
const find = (node, predicate) => descendants(node).find(predicate);
const button = (node, text) => find(node, value => value.tagName === "button" && value.textContent === text);
const checkbox = node => find(node, value => value.tagName === "input" && value.type === "checkbox");
const pending = state => ({ request_id: "request", attempt: { attempt_id: "attempt", attempt_number: 1,
  state, resources_settled: false, cancel_requested: false } });

{
  const host = container(); const calls = [];
  const view = mountDynamicsAssessment({ container: host, runId: "source", canManage: true,
    requestJson: async (...args) => { calls.push(args); return { enabled: false, assessments: [] }; } });
  await view.ready;
  assert.equal(checkbox(host), undefined);
  assert.match(host.textContent, /assessment is disabled/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].method, undefined);
  view.destroy();
}
{
  const host = container(); const calls = [];
  const view = mountDynamicsAssessment({ container: host, runId: "source", canManage: true,
    requestJson: async (...args) => { calls.push(args); return { enabled: true, assessments: [] }; } });
  await view.ready;
  assert.equal(checkbox(host).checked, false);
  assert.equal(button(host, "Run assessment").disabled, true);
  assert.equal(calls.length, 1, "Mounting must not submit a job");
  view.destroy();
}
{
  const host = container(); const calls = []; let submitted = false;
  const item = pending("unsupported"); item.attempt.resources_settled = true; item.result_artifact = { artifact_id: "result" };
  const view = mountDynamicsAssessment({ container: host, runId: "source", canManage: true,
    requestJson: async (path, options) => {
      calls.push({ path, ...options });
      if (options.method === "POST") { submitted = true; return item; }
      if (path.endsWith("/result")) return { state: "unsupported", limitations: ["Missing motor parameters"] };
      return { enabled: true, assessments: submitted ? [item] : [] };
    } });
  await view.ready;
  checkbox(host).checked = true; await checkbox(host).emit("change");
  await button(host, "Run assessment").emit("click");
  const post = calls.find(value => value.method === "POST");
  assert.deepEqual(post.body, { bundle: null });
  assert.equal(typeof post.idempotencyKey, "string");
  assert.match(host.textContent, /Inputs unsupported/);
  assert.match(host.textContent, /Missing motor parameters/);
  assert(calls.every(value => !value.path.includes("publish")));
  view.destroy();
}
{
  const host = container(); const item = pending("unknown");
  const view = mountDynamicsAssessment({ container: host, runId: "source", canManage: true,
    requestJson: async () => ({ enabled: true, assessments: [item] }) });
  await view.ready;
  assert.match(host.textContent, /Awaiting reconciliation/);
  assert(button(host, "Reconcile original attempt"));
  assert.equal(button(host, "Retry in a new attempt"), undefined);
  assert.equal(button(host, "Run assessment").disabled, true);
  view.destroy();
}
{
  const host = container(); const item = pending("running"); item.attempt.cancel_requested = true;
  const view = mountDynamicsAssessment({ container: host, runId: "source", canManage: true,
    requestJson: async () => ({ enabled: true, assessments: [item] }) });
  await view.ready;
  assert.match(host.textContent, /Cancellation requested; resource settlement is pending/);
  assert(!host.textContent.includes("Attempt 1: Cancelled"));
  view.destroy();
}
{
  const host = container(); let oldResponse;
  const first = mountDynamicsAssessment({ container: host, runId: "old", requestJson: () => new Promise(resolve => { oldResponse = resolve; }) });
  const second = mountDynamicsAssessment({ container: host, runId: "new", requestJson: async () => ({ enabled: false, assessments: [] }) });
  await second.ready;
  oldResponse({ enabled: true, assessments: [pending("running")] });
  await first.ready;
  assert.match(host.textContent, /assessment is disabled/);
  assert(!host.textContent.includes("Attempt 1"));
  first.destroy(); second.destroy();
}
{
  const host = container(); const item = pending("completed"); item.attempt.resources_settled = true; item.result_artifact = {};
  let revoked = false;
  const view = mountDynamicsAssessment({ container: host, runId: "source", requestJson: async path => {
    if (revoked) throw Object.assign(new Error("Source access revoked"), { status: 403 });
    if (path.endsWith("/result")) return { state: "completed", canonical_axes: { execution: { not_achieved: 8 },
      feasibility: { not_established: 8 }, verification: { unchecked: 8 } },
      independent_verification: { pass: true, records_checked: 8, expected_records: 8, coverage_complete: true },
      limitations: ["Contact force remains unqualified", "<script>untrusted</script>"],
      source_certificate_members: { urdf: "engineering/urdf-certificate/certificate.json" } };
    return { enabled: true, assessments: [item] };
  } });
  await view.ready;
  assert.match(host.textContent, /unchecked: 8/);
  assert.match(host.textContent, /not achieved: 8/);
  assert.match(host.textContent, /Passed; 8 \/ 8 records/);
  assert.match(host.textContent, /Contact force remains unqualified/);
  revoked = true; await view.refresh();
  assert(!host.textContent.includes("unchecked: 8"));
  assert.match(host.textContent, /Source access revoked/);
  view.destroy();
}
{
  const host = container(); const keys = [], bodies = [];
  const bundle = { artifact_id: "original-bundle" }; let selections = 0;
  const view = mountDynamicsAssessment({ container: host, runId: "source", canManage: true,
    getBundle: async () => { selections += 1; return bundle; },
    requestJson: async (_path, options) => {
      if (options.method === "POST") {
        keys.push(options.idempotencyKey); bodies.push(structuredClone(options.body));
        throw new Error("Reply lost");
      }
      return { enabled: true, assessments: [] };
    } });
  await view.ready; checkbox(host).checked = true; await checkbox(host).emit("change");
  await button(host, "Run assessment").emit("click");
  bundle.artifact_id = "changed-after-lost-reply";
  await button(host, "Run assessment").emit("click");
  assert.equal(keys.length, 2); assert.equal(keys[0], keys[1]);
  assert.equal(selections, 1); assert.deepEqual(bodies[0], bodies[1]);
  assert.equal(bodies[1].bundle.artifact_id, "original-bundle");
  view.destroy();
}
assert.equal(assessmentStateLabel("unexpected"), "Status unavailable");
{
  const host = container(), controls = []; let selections = 0;
  const item = pending("unknown"); item.attempt.detail = {};
  const view = mountDynamicsAssessment({ container: host, runId: "source", canManage: true,
    getBundle: async () => { selections += 1; throw new Error("Original dispatch must not select new inputs"); },
    requestJson: async (path, options) => {
      if (options.method === "POST") {
        controls.push({ path, body: options.body });
        if (path.endsWith("/reconcile")) {
          item.attempt.state = "registered";
          item.attempt.detail = { dispatch_observation: "absent" };
          item.attempt.dispatch_accepted = false;
        } else if (path.endsWith("/dispatch")) {
          item.attempt.state = "queued"; item.attempt.dispatch_accepted = true;
        } else throw new Error("Unexpected new submission");
      }
      return { enabled: true, assessments: [item] };
    } });
  await view.ready;
  await button(host, "Reconcile original attempt").emit("click");
  assert.equal(button(host, "Run assessment").disabled, true);
  await button(host, "Send original attempt").emit("click");
  assert.deepEqual(controls.map(value => value.path), [
    "/api/dynamics-assessments/attempts/attempt/reconcile",
    "/api/dynamics-assessments/attempts/attempt/dispatch",
  ]);
  assert(controls.every(value => value.body === undefined));
  assert.equal(selections, 0);
  assert.equal(button(host, "Send original attempt"), undefined);
  view.destroy();
}
{
  const host = container(), calls = [];
  const bundle = { artifact_id: "admitted-upload" };
  const snapshot = { enabled: true, source_ready: true, source_sha256: "fixed-source", assessments: [], input_limit_bytes: 1024 };
  const view = mountDynamicsAssessment({ container: host, runId: "source", canManage: true,
    requestJson: async (path, options) => {
      if (options.method === "POST") {
        calls.push({ path, body: structuredClone(options.body) });
        return path.endsWith("/bundles") ? { bundle, source_sha256: "fixed-source" } : {};
      }
      return snapshot;
    } });
  await view.ready;
  const file = find(host, node => node.type === "file");
  file.files = [{ name: "execution.zip", size: 3, arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer }];
  await file.emit("change");
  await button(host, "Upload execution bundle").emit("click");
  assert.equal(calls.length, 1); assert(calls[0].path.endsWith("/assessments/dynamics/bundles"));
  assert.equal(calls[0].body.archive_base64, "AQID");
  assert.match(host.textContent, /Execution inputs admitted for this source/);
  checkbox(host).checked = true; await checkbox(host).emit("change");
  await button(host, "Run assessment").emit("click");
  assert.equal(calls.length, 2); assert.deepEqual(calls[1].body, { bundle });
  view.destroy();
}
{
  const host = container(); let resolveFile, posts = 0;
  const view = mountDynamicsAssessment({ container: host, runId: "source", canManage: true,
    requestJson: async (_path, options) => {
      if (options.method === "POST") posts += 1;
      return { enabled: true, source_ready: true, source_sha256: "source", assessments: [] };
    } });
  await view.ready;
  const file = find(host, node => node.type === "file");
  file.files = [{ name: "cancelled.zip", size: 3, arrayBuffer: () => new Promise(resolve => { resolveFile = resolve; }) }];
  await file.emit("change");
  const uploading = button(host, "Upload execution bundle").emit("click");
  await button(host, "Cancel input upload").emit("click");
  resolveFile(new Uint8Array([1, 2, 3]).buffer); await uploading;
  assert.equal(posts, 0); assert(!host.textContent.includes("Ready to run"));
  view.destroy();
}
console.log("Dynamics assessment UI: default-off, explicit submission, UNKNOWN, cancel settlement, source races, raw axes, revocation and idempotency passed");
