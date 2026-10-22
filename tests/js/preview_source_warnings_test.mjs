// Source warnings remain visible for verified previews and retain every object row.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const staticDir = new URL("../../src/description_pipeline/orchestration/static/", import.meta.url);
const logicSource = readFileSync(new URL("preview_logic.js", staticDir), "utf8");
const logic = await import(
  `data:text/javascript;base64,${Buffer.from(logicSource, "utf8").toString("base64")}`
);
const { previewDiagnosticState, warningSourceRows } = logic;
const app = readFileSync(new URL("app.js", staticDir), "utf8");

const warning = {
  code: "limits.default_policy",
  message: "Engineering position range remains unqualified.",
  detail: {
    objects: [
      {
        object_type: "joint",
        object: "joint:left",
        field: "limits",
        original: { lower: 0, upper: 0 },
        actual: { lower: -Math.PI, upper: Math.PI },
        source: "engineering_rotation_default_window/v1",
        rule_id: "engineering_rotation_default_window/v1",
        rule_version: "v1",
        native_cad_ranges: false,
        hardware_qualified: false,
        engineering_position_range_qualified: false,
      },
      {
        object_type: "joint",
        object: "joint:right",
        field: "limits",
        source: "engineering_rotation_default_window/v1",
        rule_id: "engineering_rotation_default_window/v1",
        rule_version: "v1",
        native_cad_ranges: false,
        hardware_qualified: false,
        engineering_position_range_qualified: false,
      },
    ],
  },
};

assert.equal(previewDiagnosticState({ verified: true, warnings: [] }).visible, false);
const state = previewDiagnosticState({ verified: true, warnings: [warning], warnings_total: 1 });
assert.equal(state.visible, true);
assert.equal(state.unverified, false);
const rows = warningSourceRows(warning);
assert.deepEqual(rows.map((row) => [row.object, row.fields]), [
  ["joint:left", ["limits"]],
  ["joint:right", ["limits"]],
]);
for (const row of rows) {
  assert.equal(row.rule_id, "engineering_rotation_default_window/v1");
  assert.equal(row.rule_version, "v1");
}
assert.deepEqual(previewDiagnosticState({ verified: false, warnings: [] }), {
  visible: true,
  unverified: true,
  warnings: [],
  warnings_total: 0,
});

const renderer = app.slice(
  app.indexOf("function renderPreviewDiagnostic"),
  app.indexOf("async function loadPreview("),
);
for (const token of [
  "previewDiagnosticState(preview)",
  "diagnostic.warnings.length",
  "Source warnings",
  "appendWarningSourceRows",
  "replaceChildren()",
  "textContent",
]) {
  assert.ok(renderer.includes(token), token);
}
assert.equal(renderer.includes("innerHTML"), false);
assert.equal(renderer.includes("insertAdjacentHTML"), false);
console.log("preview_source_warnings_test OK");
