// 1.4.2 preview battery (P1-P5): the neutral fixture expectations from
// reviews/d-acceptance-20261014/preview142-fixtures are inlined here (values only, no repo
// coupling) and drive the same pure helpers viewer.js/app.js consume.
// Run with: node tests/js/preview142_battery_test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  path.join(here, "..", "..", "src", "description_pipeline", "orchestration", "static", "preview_logic.js"),
  "utf8",
);
const logic = await import(`data:text/javascript;base64,${Buffer.from(source, "utf8").toString("base64")}`);
const {
  MACHINE_FIELD_KEYS,
  applyInteraction,
  closureRows,
  ensureSingleViewer,
  frameRows,
  infoPanelRows,
  interactionEffect,
  jointRows,
  linkRows,
  previewExpansion,
  purposeLabel,
  stageEntersPreview,
  stageSummaryRows,
  summaryIdentity,
} = logic;

// P1 interaction.responds (fixture case-01-interaction-responds.json):
// orbit/zoom/pan each visibly change the camera, the distance or the target.
{
  const before = { theta: 2.0, phi: 1.2, radius: 2.5, target: { x: 0, y: 0, z: 0 } };
  const orbit = applyInteraction(before, "orbit", [30, 10]);
  assert.deepEqual(interactionEffect(before, orbit, "orbit"), { camera_changed: true });
  const zoom = applyInteraction(before, "zoom", -120);
  assert.equal(interactionEffect(before, zoom, "zoom").distance_decreased, true);
  const pan = applyInteraction(before, "pan", [20, 0]);
  assert.equal(interactionEffect(before, pan, "pan").target_changed, true);
  // Negative class: a no-op interaction must be detected as unchanged, never counted.
  assert.equal(interactionEffect(before, { ...before }, "orbit").camera_changed, false);
  assert.equal(interactionEffect(before, { ...before }, "pan").target_changed, false);
}

// P2 default.expanded (fixture case-02-default-expanded.json):
// a verified delivery starts expanded with a collapse control that can retreat.
{
  const preview = { subject_sha256: "a".repeat(64) };
  const initial = previewExpansion({ preview, collapsed: false });
  assert.equal(initial.expanded, true);
  assert.equal(initial.collapse_control, true);
  assert.equal(previewExpansion({ preview, collapsed: true }).expanded, false);
  assert.equal(previewExpansion({ preview: null, collapsed: false }).expanded, false);
  // Entering the generate-URDF block re-expands the interactive preview.
  assert.equal(stageEntersPreview("generate"), true);
  assert.equal(stageEntersPreview("verify"), false);
  assert.equal(stageEntersPreview("publish"), false);
}

// P3 info_panel.fields (fixture case-03-info-panel-fields.json):
// the machine keys appear verbatim; display names never substitute for them.
{
  const tool = { version: "1.4.0", source_sha256: "b".repeat(64) };
  const rows = infoPanelRows({
    runId: "portal-20261015T000000-aa11bb22",
    subjectSha256: "a".repeat(64),
    tool,
    qualitySchema: "solidworks-to-urdf.quality/v1",
    qualificationStates: [
      { fact_id: "drive_sizing", status: "unqualified" },
      { fact_id: "machine_qualification", status: "out_of_scope" },
    ],
  });
  const keys = rows.map((row) => row.key);
  for (const required of MACHINE_FIELD_KEYS) assert.ok(keys.includes(required), required);
  const byKey = new Map(rows.map((row) => [row.key, row.value]));
  assert.equal(byKey.get("run_id"), "portal-20261015T000000-aa11bb22");
  assert.equal(byKey.get("subject_sha256"), "a".repeat(64));
  assert.ok(byKey.get("tool_identity").includes(tool.source_sha256), "tool_identity must carry the machine digest");
  assert.equal(byKey.get("qualification_states"), "drive_sizing=未建立, machine_qualification=不适用");
  assert.equal(byKey.get("purpose"), "未记录");
  assert.equal(purposeLabel("engineering"), "engineering");
  assert.equal(purposeLabel("inspection_preview"), "inspection_preview");
  assert.equal(purposeLabel("unknown"), "未记录");
  assert.equal(infoPanelRows({ runId: "", subjectSha256: "", tool: null }).length, MACHINE_FIELD_KEYS.length + 2);
}

// Static display seams (no browser): missing drive stays absent and labelled, the
// preview window is labelled as an analysis window, and the tool identity is the
// delivery-root tool.json.
{
  const staticDir = path.join(here, "..", "..", "src", "description_pipeline", "orchestration", "static");
  const viewer = readFileSync(path.join(staticDir, "viewer.js"), "utf8");
  const app = readFileSync(path.join(staticDir, "app.js"), "utf8");
  assert.ok(viewer.includes('parseNumber(limit.getAttribute("effort"), null)'));
  assert.ok(!viewer.includes('parseNumber(limit.getAttribute("effort"), 0)'));
  assert.ok(app.includes("未提供（未建立驱动能力）"));
  assert.ok(app.includes("检查/预览分析窗口，非机械限位"));
  assert.ok(app.includes('loadPreviewArtifactJson(artifactUrl, preview.files, "tool.json", signal)'));
  assert.ok(app.includes('["用途", purposeLabel(run.purpose)]'));
  assert.ok(app.includes("purpose: preview.purpose"));
  assert.ok(!app.includes('"reports/tool.json"'));
}

// P4 summary.bound (fixture case-04-summary-binding.json):
// every summary identity equals the underlying run record identity.
{
  const record = { run_id: "portal-20261015T000000-aa11bb22", subject_sha256: "a".repeat(64) };
  const summary = summaryIdentity({ runId: record.run_id, subjectSha256: record.subject_sha256 });
  assert.deepEqual(summary, record);
  const quick = stageSummaryRows("generate", {
    files: {},
    stage: null,
    quality: null,
    pr: null,
    subjectSha256: record.subject_sha256,
  });
  assert.equal(quick.rows.find((row) => row.label === "交付主体").value, record.subject_sha256);
  assert.ok(quick.necessity);
}

// P5 single_viewer (fixture case-05-single-viewer.json):
// exactly one viewer instance; repeated mounts reuse instead of stacking canvases.
{
  const state = { viewer: null };
  let built = 0;
  const factory = () => {
    built += 1;
    return { name: "viewer" };
  };
  const first = ensureSingleViewer(state, factory);
  const second = ensureSingleViewer(state, factory);
  assert.equal(first.created, true);
  assert.equal(second.created, false);
  assert.equal(built, 1);
  assert.equal(first.viewer, second.viewer);
}

// Supporting rows: the five panels render machine-shaped data without fabrication.
{
  const joints = jointRows([
    { name: "waist", type: "revolute", lower: -1.5, upper: 1.5, effort: 6, velocity: 2, axis: [0, 0, 1] },
  ]);
  assert.equal(joints[0].axis[2], 1);
  assert.equal(joints[0].effort, 6);
  const links = linkRows([
    { name: "base_link", mass_kg: 1.25, com_m: [0, 0, 0.1], inertia_kg_m2: { ixx: 1, iyy: 1, izz: 1 } },
  ]);
  assert.equal(links[0].mass_kg, 1.25);
  const closure = closureRows([
    { id: "physics.expected_mass", state: "passed" },
    { id: "bundle.subject", state: "passed" },
  ]);
  assert.deepEqual(closure.map((row) => row.id), ["physics.expected_mass"]);
  const frames = frameRows([{ name: "CS_base_link", parent: "base_link", xyz: [0, 0, 0.1], rpy: [0, 0, 0] }]);
  assert.equal(frames[0].parent, "base_link");
  assert.equal(frames[0].xyz[2], 0.1);
}

console.log("preview142 battery: P1-P5 OK");
