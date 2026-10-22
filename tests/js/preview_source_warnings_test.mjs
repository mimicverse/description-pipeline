// Source warnings remain visible for verified previews and retain every object row.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const staticDir = new URL("../../src/description_pipeline/orchestration/static/", import.meta.url);
const logicSource = readFileSync(new URL("preview_logic.js", staticDir), "utf8");
const logic = await import(
  `data:text/javascript;base64,${Buffer.from(logicSource, "utf8").toString("base64")}`
);
const { previewDiagnosticState, registeredQualityArtifact, warningSourceRows } = logic;
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
        file: "records/record.json",
        sha256: "b".repeat(64),
        anchor: '"limits"',
        key: "limits",
        reference: "joints/left.json#limits",
        native_observation_errors: [{ mate: "native_limit", reason: "unreadable" }],
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
assert.deepEqual(rows[0].native_observation_errors, [
  { mate: "native_limit", reason: "unreadable" },
]);
assert.equal(rows[0].file, "records/record.json");
assert.equal(rows[0].sha256, "b".repeat(64));
assert.equal(rows[0].anchor, '"limits"');
assert.equal(rows[0].key, "limits");
assert.equal(rows[0].reference, "joints/left.json#limits");
assert.deepEqual(
  registeredQualityArtifact(
    { report_files: { "reports/quality.json": "a".repeat(64) } },
    "run/id",
  ),
  {
    name: "reports/quality.json",
    sha256: "a".repeat(64),
    href: "/api/runs/run%2Fid/artifacts/reports/quality.json",
  },
);
assert.equal(registeredQualityArtifact({ report_files: {} }, "run"), null);
assert.equal(
  registeredQualityArtifact({ report_files: { "reports/quality.json": "A".repeat(64) } }, "run"),
  null,
);
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
  "registeredQualityArtifact",
  "Open registered quality report",
  "link.textContent",
  "replaceChildren()",
  "textContent",
]) {
  assert.ok(renderer.includes(token), token);
}
assert.ok(app.includes("native_observation_errors"));
assert.equal(renderer.includes("innerHTML"), false);
assert.equal(renderer.includes("insertAdjacentHTML"), false);
console.log("preview_source_warnings_test OK");
