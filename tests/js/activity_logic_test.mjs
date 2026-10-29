// Focused regression for the pure live-activity view helpers (no DOM).
// Run with: node tests/js/activity_logic_test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  path.join(here, "..", "..", "src", "description_pipeline", "orchestration", "static", "activity.js"),
  "utf8",
);
const {
  activityPlacement,
  activityView,
  actionText,
  countsText,
  formatDuration,
  freshnessText,
  stageText,
} = await import(`data:text/javascript;base64,${Buffer.from(source, "utf8").toString("base64")}`);

const START = "2026-10-10T09:33:05.628Z";
const startedAt = Date.parse(START);
const nowMs = startedAt + 125000; // two minutes and five seconds into the stage

// busy: full live card from a real record.
const busy = activityView(
  {
    available: true,
    state: "busy",
    stage: "discover",
    stage_started_at: START,
    updated_at: new Date(nowMs - 5000).toISOString(),
    action: { code: "discover.read_components", params: { batch: 2 } },
    object: "3.0 总装1008.SLDASM",
    counts: { done: 120, total: 483, unit: "instances" },
    recent: [
      { at: new Date(nowMs - 180000).toISOString(), code: "discover.scan_documents", object: null },
      { at: new Date(nowMs - 9000).toISOString(), code: "discover.read_components", object: "3.0 总装1008.SLDASM" },
      { at: new Date(nowMs - 60000).toISOString(), code: "discover.read_mates", object: null },
    ],
  },
  { runState: "running", nowMs },
);
assert.equal(busy.visible, true);
assert.equal(busy.state, "busy");
assert.equal(busy.stateText, "In progress");
assert.equal(busy.stageText, "Discover structure");
assert.equal(busy.actionText, "Reading assembly components");
assert.equal(busy.objectText, "3.0 总装1008.SLDASM");
assert.equal(busy.countsText, "Processed 120/483 instances");
assert.equal(busy.elapsedText, "Elapsed 2m 5s");
assert.equal(busy.freshnessText, "Updated just now");
assert.equal(busy.stale, false);
assert.equal(busy.note, "");
assert.equal(busy.recent.length, 3);
assert.match(busy.recent[0].timeText, /^\d{2}:\d{2}:\d{2}$/);
assert.ok(busy.recent[0].at >= busy.recent[1].at && busy.recent[1].at >= busy.recent[2].at, "newest first");
assert.equal(busy.recent[0].text, "Reading assembly components");
assert.equal(busy.recent[0].object, "3.0 总装1008.SLDASM");

// Hard rules: no percentage figures, no forecasts, in any produced string.
const strings = [
  busy.stateText, busy.stageText, busy.actionText, busy.objectText, busy.countsText,
  busy.elapsedText, busy.freshnessText, busy.note,
  ...busy.recent.flatMap((row) => [row.timeText, row.text, row.object]),
];
for (const value of strings) {
  assert.ok(!value.includes("%"), `percent leaked: ${value}`);
  assert.ok(!value.toLowerCase().includes("forecast") && !value.toLowerCase().includes("remaining"), `forecast leaked: ${value}`);
}

// Stale record: recency comes only from updated_at; wording states "no new update" only.
const stale = activityView(
  {
    available: true,
    state: "busy",
    stage: "discover",
    stage_started_at: START,
    updated_at: new Date(nowMs - 200000).toISOString(),
    action: { code: "discover.read_mates" },
    recent: [],
  },
  { runState: "running", nowMs },
);
assert.equal(stale.stale, true);
assert.ok(stale.freshnessText.startsWith("No new update"), stale.freshnessText);

// No telemetry: explicit unavailable, no fabricated action, still visible while running.
const none = activityView(
  { available: false, state: "none", stage: null, stage_started_at: null, updated_at: null, action: null, object: null, counts: null, recent: [] },
  { runState: "running", nowMs },
);
assert.equal(none.visible, true);
assert.equal(none.state, "none");
assert.equal(none.actionText, "");
assert.equal(none.freshnessText, "");
assert.ok(none.note.includes("no detailed progress"), none.note);

// waiting: runner active without a telemetry record — same explicit wording.
const waiting = activityView(
  { available: false, state: "waiting", stage: "capture", stage_started_at: null, updated_at: null, action: null, recent: [] },
  { runState: "running", nowMs },
);
assert.equal(waiting.state, "waiting");
assert.equal(waiting.stateText, "Waiting for progress");
assert.ok(waiting.note.includes("no detailed progress"));

// queued: accepted but not started — no elapsed, no action.
const queued = activityView(
  { available: false, state: "queued", stage: null, stage_started_at: null, updated_at: null, action: null, recent: [] },
  { runState: "queued", nowMs },
);
assert.equal(queued.visible, true);
assert.equal(queued.stateText, "Queued");
assert.equal(queued.elapsedText, "");
assert.equal(queued.note, "Queued and waiting to start.");

// Terminal runs and finished records never render a live block.
assert.equal(
  activityView({ available: true, state: "busy", stage: "publish", recent: [] }, { runState: "success", nowMs }).visible,
  false,
);
assert.equal(
  activityView({ available: false, state: "finished", stage: null, recent: [] }, { runState: "running", nowMs }).visible,
  false,
);

// A preserved last activity at terminal stays visible as a compact final block.
const finalView = activityView(
  {
    available: true,
    state: "finished",
    stage: "capture",
    stage_started_at: null,
    updated_at: new Date(nowMs - 300000).toISOString(),
    action: { code: "discover.read_mates" },
    object: "headM3.0.SLDASM",
    counts: null,
    recent: [{ at: new Date(nowMs - 300000).toISOString(), code: "discover.read_mates", object: null }],
  },
  { runState: "failed", nowMs },
);
assert.equal(finalView.visible, true);
assert.equal(finalView.final, true);
assert.equal(finalView.stateText, "Finished");
assert.equal(finalView.actionText, "Reading mates");
assert.equal(finalView.objectText, "headM3.0.SLDASM");
assert.ok(finalView.freshnessText.startsWith("Last activity:"), finalView.freshnessText);
assert.equal(finalView.recent.length, 1);
assert.equal(activityPlacement(finalView, { stageId: "capture", stageRunning: false, stageFailed: true }), "final");
assert.equal(activityPlacement(finalView, { stageId: "capture", stageRunning: false, stageFailed: false }), "hidden");
assert.equal(activityPlacement(finalView, { stageId: "discover", stageRunning: false, stageFailed: true }), "hidden");

// The full emitted vocabulary maps to concrete labels (a slow open/rebuild call must not stay
// at a blind 进行中); unknown codes keep the neutral fallback.
const EMITTED_ACTIONS = [
  ["discover.session_start", "Starting SolidWorks"],
  ["discover.scan_documents", "Scanning engineering files"],
  ["discover.open_document", "Opening document"],
  ["discover.select_configuration", "Switching configuration"],
  ["discover.read_document_state", "Reading saved model state"],
  ["discover.read_components", "Reading assembly components"],
  ["discover.read_mates", "Reading mates"],
  ["discover.read_datums", "Reading datums and coordinate systems"],
  ["discover.read_properties", "Reading properties and delivery definitions"],
  ["discover.hash_sources", "Computing file digests"],
  ["discover.build_record", "Building discovery record"],
  ["capture.start", "Starting prepared Capture"],
  ["capture.prepared_source", "Verifying the prepared source"],
  ["capture.open_prepared", "Opening the prepared assembly read-only"],
  ["capture.readings", "Reading native scene, mass, datums and configuration"],
  ["capture.geometry_facts", "Reading native bodies, faces and triangles"],
  ["capture.release", "Releasing the owned CAD session"],
  ["capture.scene_projection", "Projecting sealed native facts offline"],
  ["capture.geometry", "Writing offline STL geometry"],
  ["capture.seal", "Sealing Capture evidence"],
  ["capture.prepare_capture", "Preparing the current Capture occurrence"],
  ["capture.walk", "Reading the native occurrence tree"],
  ["capture.instance_reads", "Reading instance configuration and mass properties"],
];
assert.equal(EMITTED_ACTIONS.length, 23);
for (const [code, label] of EMITTED_ACTIONS) {
  assert.equal(
    actionText(code, code.startsWith("capture.") ? "capture" : "discover"),
    label,
    code,
  );
}

// Unknown codes fall back to a neutral stage phrase, never an invented specific.
assert.equal(actionText("discover.something_new", "discover"), "Discover structure in progress");
assert.equal(actionText(null, null), "In progress");
assert.equal(stageText("capture"), "Capture evidence");
assert.equal(stageText("mystery"), "mystery");

// Counts: real numbers only, unit optional, never a percentage.
assert.equal(countsText({ done: 5, total: 10 }), "Processed 5/10");
assert.equal(countsText({ done: 5 }), "Processed 5");
assert.equal(countsText({ total: 10 }), "Planned 10");
assert.equal(countsText({ done: 5, total: 10, unit: "documents" }), "Processed 5/10 documents");
assert.equal(countsText(null), "");

// Duration and freshness formatting.
assert.equal(formatDuration(45000), "45s");
assert.equal(formatDuration(125000), "2m 5s");
assert.equal(formatDuration(120000), "2m");
assert.equal(formatDuration(3600000 + 120000), "1h 2m");
assert.equal(freshnessText(null, nowMs).known, false);
assert.equal(freshnessText(new Date(nowMs - 30000).toISOString(), nowMs).text, "Updated 30s ago");

// Placement: live only for the running stage's own activity; otherwise an explicit note.
assert.equal(activityPlacement(busy, { stageId: "discover", stageRunning: true }), "live");
assert.equal(activityPlacement(busy, { stageId: "capture", stageRunning: true }), "note");
assert.equal(activityPlacement(busy, { stageId: "discover", stageRunning: false }), "hidden");
assert.equal(activityPlacement(none, { stageId: "discover", stageRunning: true }), "note");
assert.equal(
  activityPlacement(
    activityView({ available: false, state: "finished", recent: [] }, { runState: "success", nowMs }),
    { stageId: "discover", stageRunning: true },
  ),
  "hidden",
);

// Objects stay plain strings (escaping is the DOM layer's textContent job, never HTML).
const literal = activityView(
  {
    available: true,
    state: "busy",
    stage: "discover",
    stage_started_at: START,
    updated_at: null,
    action: { code: "discover.read_components" },
    object: "<img src=x onerror=alert(1)>",
    counts: null,
    recent: [],
  },
  { runState: "running", nowMs },
);
assert.equal(literal.objectText, "<img src=x onerror=alert(1)>");

console.log("activity_logic_test OK");

assert.equal(activityView({ available: true, state: "busy", stage: "capture", recent: [] }, { runState: "cancelled", nowMs }).visible, false);

const unresolved = activityView({ available: true, state: "busy", stage: "capture", recent: [] }, { runState: "unresolved", nowMs });
assert.equal(unresolved.state, "waiting");
assert.equal(unresolved.elapsedText, "");
assert.match(unresolved.note, /reconciliation/);

assert.equal(unresolved.final, false);
const authorizedWait = activityView({available: false, state: "waiting"}, {runState: "waiting", nowMs});
assert.equal(authorizedWait.final, false);
assert.equal(authorizedWait.stateText, "Awaiting external result");
assert.equal(authorizedWait.actionText, "");
assert.equal(authorizedWait.countsText, "");
assert.equal(authorizedWait.elapsedText, "");
assert.equal(activityPlacement(authorizedWait, {stageId: "capture", stageRunning: true}), "live");
const recordedWait = activityView({available: true, state: "busy", stage: "capture", action: {code: "capture.geometry_facts"}, counts: {done: 1, total: 111}}, {runState: "waiting", nowMs});
assert.equal(recordedWait.state, "busy");
assert.equal(recordedWait.countsText, "Processed 1/111");
console.log("authorized wait activity preserves evidence OK");
