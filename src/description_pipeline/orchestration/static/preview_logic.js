// Pure helpers for the default-expanded URDF preview workspace (1.4.2).  No DOM and no
// three.js: tests/js/preview142_battery_test.mjs drives them directly and viewer.js/app.js
// consume the same functions, so the battery measures production behaviour.

//: Machine keys the information panel must show verbatim (display names never substitute).
export const MACHINE_FIELD_KEYS = ["run_id", "subject_sha256", "tool_identity"];

// ----------------------------------------------------------------------------- interaction

// The exact orbit model viewer.js renders with: rotate/pan/zoom in one pure state
// transition.  `basis` carries the camera-plane axes for pan (defaults are neutral).
export function applyInteraction(state, action, delta, basis) {
  const next = {
    theta: Number(state.theta) || 0,
    phi: Number(state.phi) || 0,
    radius: Number(state.radius) || 1,
    target: {
      x: Number(state.target?.x) || 0,
      y: Number(state.target?.y) || 0,
      z: Number(state.target?.z) || 0,
    },
  };
  if (action === "orbit") {
    const dx = Number(delta?.[0]) || 0;
    const dy = Number(delta?.[1]) || 0;
    next.theta = next.theta - dx * 0.008;
    next.phi = Math.min(Math.PI - 0.02, Math.max(0.02, next.phi - dy * 0.008));
  } else if (action === "zoom") {
    const dy = Number(delta) || 0;
    next.radius = Math.min(500, Math.max(0.05, next.radius * Math.exp(dy * 0.001)));
  } else if (action === "pan") {
    const dx = Number(delta?.[0]) || 0;
    const dy = Number(delta?.[1]) || 0;
    const scale = next.radius * 0.0016;
    const right = basis?.right || [1, 0, 0];
    const up = basis?.up || [0, 1, 0];
    next.target.x += -dx * scale * right[0] + dy * scale * up[0];
    next.target.y += -dx * scale * right[1] + dy * scale * up[1];
    next.target.z += -dx * scale * right[2] + dy * scale * up[2];
  }
  return next;
}

//: The visible outcome each interaction must produce (drives the P1 battery case).
export function interactionEffect(before, after, action) {
  if (action === "zoom") {
    return { distance_decreased: Number(after.radius) < Number(before.radius) };
  }
  if (action === "pan") {
    return {
      target_changed:
        after.target.x !== before.target.x ||
        after.target.y !== before.target.y ||
        after.target.z !== before.target.z,
    };
  }
  return {
    camera_changed: after.theta !== before.theta || after.phi !== before.phi,
  };
}

// ----------------------------------------------------------------------------- expansion

//: The stage whose block entry expands the interactive preview by default.
export const PREVIEW_STAGE_ID = "generate";

export function stageEntersPreview(stageId) {
  return stageId === PREVIEW_STAGE_ID;
}

// The 1.4.2 headline: with a verified delivery the preview starts expanded; a collapse
// control can retreat from that default (never below it for stage entry).
export function previewExpansion({ preview, collapsed }) {
  const available = Boolean(preview && typeof preview.subject_sha256 === "string" && preview.subject_sha256);
  return {
    expanded: available && collapsed !== true,
    collapse_control: available,
  };
}

// Single-viewer guard: the first factory call builds the instance, every later call
// reuses it; a second independent mount is refused instead of stacking canvases.
export function ensureSingleViewer(state, factory) {
  if (state.viewer) return { viewer: state.viewer, created: false };
  const viewer = factory();
  if (!viewer || typeof viewer !== "object") throw new Error("viewer factory returned no instance");
  state.viewer = viewer;
  return { viewer, created: true };
}

// ----------------------------------------------------------------------------- identity

// The summary identity must equal the underlying record identity (run id + subject).
export function summaryIdentity({ runId, subjectSha256 }) {
  return {
    run_id: typeof runId === "string" ? runId : "",
    subject_sha256: typeof subjectSha256 === "string" ? subjectSha256 : "",
  };
}

export function infoPanelRows({ runId, subjectSha256, tool, qualitySchema, qualificationStates }) {
  const toolIdentity = tool
    ? [tool.version, tool.source_sha256].filter((value) => typeof value === "string" && value).join(" / ")
    : "";
  const states = Array.isArray(qualificationStates)
    ? qualificationStates.map((item) => `${item?.fact_id || "?"}=${item?.state || "?"}`).join(", ")
    : "";
  return [
    { key: "run_id", value: typeof runId === "string" ? runId : "" },
    { key: "subject_sha256", value: typeof subjectSha256 === "string" ? subjectSha256 : "" },
    { key: "tool_identity", value: toolIdentity },
    { key: "quality_schema", value: typeof qualitySchema === "string" ? qualitySchema : "" },
    { key: "qualification_states", value: states },
  ];
}

// ----------------------------------------------------------------------------- panels

export function jointRows(records) {
  return (Array.isArray(records) ? records : [])
    .filter((record) => record && typeof record.name === "string")
    .map((record) => ({
      name: record.name,
      type: record.type || "fixed",
      lower: Number.isFinite(record.lower) ? record.lower : null,
      upper: Number.isFinite(record.upper) ? record.upper : null,
      effort: Number.isFinite(record.effort) ? record.effort : null,
      velocity: Number.isFinite(record.velocity) ? record.velocity : null,
      axis: Array.isArray(record.axis) ? record.axis.map((value) => Number(value) || 0) : null,
    }));
}

export function linkRows(records) {
  return (Array.isArray(records) ? records : [])
    .filter((record) => record && typeof record.name === "string")
    .map((record) => ({
      name: record.name,
      mass_kg: Number.isFinite(record.mass_kg) ? record.mass_kg : null,
      com_m: Array.isArray(record.com_m) ? record.com_m.map((value) => Number(value) || 0) : null,
      inertia_kg_m2: record.inertia_kg_m2 && typeof record.inertia_kg_m2 === "object" ? record.inertia_kg_m2 : null,
    }));
}

export function frameRows(frames) {
  return (Array.isArray(frames) ? frames : [])
    .filter((frame) => frame && typeof frame.name === "string")
    .map((frame) => ({
      name: frame.name,
      parent: typeof frame.parent === "string" ? frame.parent : null,
      xyz: Array.isArray(frame.xyz) ? frame.xyz.map((value) => Number(value) || 0) : null,
      rpy: Array.isArray(frame.rpy) ? frame.rpy.map((value) => Number(value) || 0) : null,
    }));
}

//: The physics-family checks the closure summary condenses (independent verification only).
const CLOSURE_PREFIXES = ["physics.", "geometry.", "inertia."];

export function closureRows(checks) {
  return (Array.isArray(checks) ? checks : [])
    .filter((check) => check && typeof check.id === "string" && CLOSURE_PREFIXES.some((prefix) => check.id.startsWith(prefix)))
    .map((check) => ({
      id: check.id,
      state: check.state || (check.passed === true ? "passed" : "unknown"),
      detail: check.details && typeof check.details === "object" ? check.details : null,
    }));
}

// ----------------------------------------------------------------------------- stage summaries

//: One-line necessity for each lightweight stage summary (design-side review record).
export const STAGE_SUMMARY_NECESSITY = {
  freeze: "冻结摘要让复核者先见输入清单规模，不必展开原始清单。",
  discover: "发现摘要给出原生解析规模，用于对照捕获证据的覆盖。",
  capture: "采集摘要给出证据与网格件数，确认证据面完整。",
  generate: "生成摘要绑定交互预览的交付主体（subject）。",
  verify: "验证摘要给出通过计数，是发布资格的直接读数。",
  publish: "发布摘要给出 PR 回执，闭合交付链。",
};

function segmentCounts(files) {
  const counts = { input: 0, evidence: 0, meshes: 0, total: 0 };
  if (!files || typeof files !== "object") return counts;
  for (const name of Object.keys(files)) {
    counts.total += 1;
    if (name.startsWith("input/")) counts.input += 1;
    else if (name.startsWith("evidence/")) counts.evidence += 1;
    else if (name.startsWith("meshes/")) counts.meshes += 1;
  }
  return counts;
}

export function stageSummaryRows(stageId, { files, stage, quality, pr, subjectSha256 }) {
  const rows = [];
  const counts = segmentCounts(files);
  if (stageId === "freeze") {
    rows.push({ label: "输入清单", value: `${counts.input} 件（已验证交付内 input/）` });
  } else if (stageId === "capture") {
    rows.push({ label: "证据文件", value: `${counts.evidence} 件（evidence/）` });
    rows.push({ label: "网格文件", value: `${counts.meshes} 件（meshes/）` });
  } else if (stageId === "generate") {
    rows.push({ label: "交付主体", value: typeof subjectSha256 === "string" ? subjectSha256 : "未记录" });
  } else if (stageId === "verify") {
    const passed = Number.isFinite(stage?.passed) ? stage.passed : null;
    const total = Number.isFinite(stage?.total) ? stage.total : null;
    rows.push({ label: "通过计数", value: passed !== null && total !== null ? `${passed}/${total}` : "未记录" });
    if (quality && typeof quality.schema_version === "string") {
      rows.push({ label: "质量报告", value: quality.schema_version });
    }
  } else if (stageId === "publish") {
    rows.push({ label: "PR 状态", value: pr && typeof pr.state === "string" ? pr.state : "未创建" });
    if (pr && typeof pr.url === "string") rows.push({ label: "PR", value: pr.url });
  } else if (stageId === "discover") {
    rows.push({ label: "交付清单", value: `${counts.total} 件` });
  }
  return { rows, necessity: STAGE_SUMMARY_NECESSITY[stageId] || "" };
}
