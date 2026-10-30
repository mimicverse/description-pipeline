// Source warning summaries remain visible with their complete-certificate download.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const staticDir = new URL("../../src/description_pipeline/orchestration/static/", import.meta.url);
const logicSource = readFileSync(new URL("preview_logic.js", staticDir), "utf8");
const logic = await import(
  `data:text/javascript;base64,${Buffer.from(logicSource, "utf8").toString("base64")}`
);
const { previewDiagnosticState, registeredVerificationArtifact } = logic;
const app = readFileSync(new URL("app.js", staticDir), "utf8");

const warning = {
  code: "limits.default_policy",
  message: "Engineering position range remains unqualified.",
  warning_summary: true,
  records_total: 1,
  records_omitted: 0,
  source_rows_total: 2,
  source_rows_omitted: 0,
  record_location: { stage: "verify", record: "reports/verification-certificate.json" },
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
};

assert.equal(previewDiagnosticState({ verified: true, warnings: [] }).visible, false);
const state = previewDiagnosticState({ verified: true, warnings: [warning], warnings_total: 1 });
assert.equal(state.visible, true);
assert.equal(state.unverified, false);
assert.equal(state.warnings[0], warning);
assert.deepEqual(
  registeredVerificationArtifact(
    { report_files: { "verification-certificate.json": "a".repeat(64) } },
    "run/id",
  ),
  {
    name: "verification-certificate.json",
    sha256: "a".repeat(64),
    href: "/api/runs/run%2Fid/artifacts/verification-certificate.json",
  },
);
assert.equal(registeredVerificationArtifact({ report_files: {} }, "run"), null);
assert.equal(
  registeredVerificationArtifact({ report_files: { "verification-certificate.json": "A".repeat(64) } }, "run"),
  null,
);
assert.deepEqual(previewDiagnosticState({ verified: false, warnings: [] }), {
  visible: true,
  unverified: true,
  warnings: [],
  warnings_total: 0,
  reason: "",
  mechanical_passed: false,
});
assert.deepEqual(
  previewDiagnosticState({
    verified: false,
    reason: "run_unresolved",
    mechanical_passed: true,
    warnings: [],
  }),
  {
    visible: true,
    unverified: true,
    warnings: [],
    warnings_total: 0,
    reason: "run_unresolved",
    mechanical_passed: true,
  },
);

const renderer = app.slice(
  app.indexOf("function renderPreviewDiagnostic"),
  app.indexOf("async function loadPreview("),
);
for (const token of [
  "previewDiagnosticState(preview)",
  "diagnostic.warnings.length",
  "Source warnings",
  "appendWarningSourceRows",
  "registeredVerificationArtifact",
  "Open verification certificate",
  "link.textContent",
  "replaceChildren()",
  "textContent",
]) {
  assert.ok(renderer.includes(token), token);
}
assert.equal(renderer.includes("innerHTML"), false);
assert.equal(renderer.includes("insertAdjacentHTML"), false);
console.log("preview_source_warnings_test OK");
