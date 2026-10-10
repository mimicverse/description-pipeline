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
assert.equal(busy.stateText, "进行中");
assert.equal(busy.stageText, "解析结构");
assert.equal(busy.actionText, "读取装配组件");
assert.equal(busy.objectText, "3.0 总装1008.SLDASM");
assert.equal(busy.countsText, "已处理 120/483 个实例");
assert.equal(busy.elapsedText, "已进行 2 分 5 秒");
assert.equal(busy.freshnessText, "刚刚更新");
assert.equal(busy.stale, false);
assert.equal(busy.note, "");
assert.equal(busy.recent.length, 3);
assert.match(busy.recent[0].timeText, /^\d{2}:\d{2}:\d{2}$/);
assert.ok(busy.recent[0].at >= busy.recent[1].at && busy.recent[1].at >= busy.recent[2].at, "newest first");
assert.equal(busy.recent[0].text, "读取装配组件");
assert.equal(busy.recent[0].object, "3.0 总装1008.SLDASM");

// Hard rules: no percentage figures, no forecasts, in any produced string.
const strings = [
  busy.stateText, busy.stageText, busy.actionText, busy.objectText, busy.countsText,
  busy.elapsedText, busy.freshnessText, busy.note,
  ...busy.recent.flatMap((row) => [row.timeText, row.text, row.object]),
];
for (const value of strings) {
  assert.ok(!value.includes("%"), `percent leaked: ${value}`);
  assert.ok(!value.includes("预计") && !value.includes("剩余"), `forecast leaked: ${value}`);
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
assert.ok(stale.freshnessText.startsWith("暂无新更新"), stale.freshnessText);

// No telemetry: explicit unavailable, no fabricated action, still visible while running.
const none = activityView(
  { available: false, state: "none", stage: null, stage_started_at: null, updated_at: null, action: null, object: null, counts: null, recent: [] },
  { runState: "running", nowMs },
);
assert.equal(none.visible, true);
assert.equal(none.state, "none");
assert.equal(none.actionText, "");
assert.equal(none.freshnessText, "");
assert.ok(none.note.includes("暂无详细进度"), none.note);

// waiting: runner active without a telemetry record — same explicit wording.
const waiting = activityView(
  { available: false, state: "waiting", stage: "capture", stage_started_at: null, updated_at: null, action: null, recent: [] },
  { runState: "running", nowMs },
);
assert.equal(waiting.state, "waiting");
assert.equal(waiting.stateText, "等待进度");
assert.ok(waiting.note.includes("暂无详细进度"));

// queued: accepted but not started — no elapsed, no action.
const queued = activityView(
  { available: false, state: "queued", stage: null, stage_started_at: null, updated_at: null, action: null, recent: [] },
  { runState: "queued", nowMs },
);
assert.equal(queued.visible, true);
assert.equal(queued.stateText, "排队中");
assert.equal(queued.elapsedText, "");
assert.equal(queued.note, "已进入队列，等待开始。");

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
    action: null,
    object: null,
    counts: null,
    recent: [{ at: new Date(nowMs - 300000).toISOString(), code: "discover.read_mates", object: null }],
  },
  { runState: "failed", nowMs },
);
assert.equal(finalView.visible, true);
assert.equal(finalView.final, true);
assert.equal(finalView.stateText, "已结束");
assert.ok(finalView.freshnessText.startsWith("最后活动："), finalView.freshnessText);
assert.equal(finalView.recent.length, 1);
assert.equal(activityPlacement(finalView, { stageId: "capture", stageRunning: false, stageFailed: true }), "final");
assert.equal(activityPlacement(finalView, { stageId: "capture", stageRunning: false, stageFailed: false }), "hidden");
assert.equal(activityPlacement(finalView, { stageId: "discover", stageRunning: false, stageFailed: true }), "hidden");

// The full emitted vocabulary maps to concrete labels (a slow open/rebuild call must not stay
// at a blind 进行中); unknown codes keep the neutral fallback.
const EMITTED_ACTIONS = [
  ["discover.session_start", "正在启动 SolidWorks"],
  ["discover.scan_documents", "扫描工程文件"],
  ["discover.open_document", "打开文档"],
  ["discover.select_configuration", "切换配置"],
  ["discover.rebuild", "重建模型"],
  ["discover.read_components", "读取装配组件"],
  ["discover.read_mates", "读取配合关系"],
  ["discover.read_datums", "读取基准与坐标系"],
  ["discover.read_properties", "读取属性与交付定义"],
  ["discover.hash_sources", "计算文件摘要"],
  ["discover.build_record", "生成发现记录"],
];
assert.equal(EMITTED_ACTIONS.length, 11);
for (const [code, label] of EMITTED_ACTIONS) {
  assert.equal(actionText(code, "discover"), label, code);
}

// Unknown codes fall back to a neutral stage phrase, never an invented specific.
assert.equal(actionText("discover.something_new", "discover"), "解析结构进行中");
assert.equal(actionText(null, null), "进行中");
assert.equal(stageText("capture"), "采集证据");
assert.equal(stageText("mystery"), "mystery");

// Counts: real numbers only, unit optional, never a percentage.
assert.equal(countsText({ done: 5, total: 10 }), "已处理 5/10");
assert.equal(countsText({ done: 5 }), "已处理 5");
assert.equal(countsText({ total: 10 }), "计划 10");
assert.equal(countsText({ done: 5, total: 10, unit: "documents" }), "已处理 5/10 个文档");
assert.equal(countsText(null), "");

// Duration and freshness formatting.
assert.equal(formatDuration(45000), "45 秒");
assert.equal(formatDuration(125000), "2 分 5 秒");
assert.equal(formatDuration(120000), "2 分");
assert.equal(formatDuration(3600000 + 120000), "1 小时 2 分");
assert.equal(freshnessText(null, nowMs).known, false);
assert.equal(freshnessText(new Date(nowMs - 30000).toISOString(), nowMs).text, "30 秒前更新");

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
