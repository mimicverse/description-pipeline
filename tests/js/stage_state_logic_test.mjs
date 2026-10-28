// Focused regression for the pure stage-state display helpers (no DOM).
// Run with: node tests/js/stage_state_logic_test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  path.join(here, "..", "..", "src", "description_pipeline", "orchestration", "static", "app.js"),
  "utf8",
);

const functionSource = (name) => {
  const start = source.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `missing function ${name}`);
  const bodyStart = source.indexOf("{", start);
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`unterminated function ${name}`);
};
const stageDotState = new Function(`return (${functionSource("stageDotState")});`)();
const runDotState = new Function(`return (${functionSource("runDotState")});`)();
const stateKind = new Function(`return (${functionSource("stateKind")});`)();
const prioritySource = source.match(/const STAGE_SELECTION_PRIORITY = \[[\s\S]*?\n\];/)[0];
assert.doesNotMatch(prioritySource, /"(?:queued|ready)"/);
const chooseStageIndex = new Function(
  "localStorage",
  `${prioritySource}\n${functionSource("stageSelectionKey")}\n${functionSource("chooseStageIndex")}\nreturn chooseStageIndex;`,
)({ getItem: () => null });

const stages = (...states) =>
  states.map((state, index) => ({ id: `stage-${index}`, state }));

assert.equal(stageDotState("completed"), "ok");
assert.equal(runDotState("failed"), "bad");
assert.equal(runDotState("unresolved"), "bad");
assert.equal(runDotState("cancelled"), "idle");
assert.equal(runDotState("running"), "run");
assert.equal(stageDotState("failed"), "bad");
assert.equal(stageDotState("unresolved"), "bad");
assert.equal(stageDotState("cancelled"), "idle");
assert.equal(stageDotState("blocked"), "idle");
for (const state of ["waiting", "running"]) {
  assert.equal(stageDotState(state), "run");
}

assert.equal(stateKind("completed"), "ok");
assert.equal(stateKind("failed"), "bad");
assert.equal(stateKind("unresolved"), "bad");
assert.equal(stateKind("cancelled"), "");
assert.equal(stateKind("blocked"), "");
for (const state of ["waiting", "running"]) {
  assert.equal(stateKind(state), "run");
}

assert.equal(
  chooseStageIndex(stages("completed", "cancelled", "blocked"), "run-1"),
  1,
);
assert.equal(
  chooseStageIndex(stages("completed", "running", "unresolved", "cancelled"), "run-2"),
  2,
);
assert.equal(
  chooseStageIndex(stages("completed", "unresolved", "failed"), "run-3"),
  1,
);
assert.equal(
  chooseStageIndex(stages("completed", "blocked", "waiting"), "run-4"),
  2,
);
assert.equal(chooseStageIndex(stages("completed", "not_run"), "run-5"), 0);
assert.equal(stageDotState("queued"), "idle");
assert.equal(stateKind("ready"), "");

const remembered = new Function(
  "localStorage",
  `${prioritySource}\n${functionSource("stageSelectionKey")}\n${functionSource("chooseStageIndex")}\nreturn chooseStageIndex;`,
)({ getItem: (key) => (key === "portal.stage.run-6" ? "stage-0" : null) });
assert.equal(
  remembered(stages("completed", "cancelled"), "run-6"),
  0,
);

console.log("stage_state_logic_test OK");
