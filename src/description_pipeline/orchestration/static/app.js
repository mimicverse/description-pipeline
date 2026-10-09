// Operator page logic. All Airflow and Windows endpoint credentials stay on the server; this
// module only talks to the portal's own JSON API and the digest-verified artifact routes.
import { buildJointControls, createViewer, disposeRobot, loadRobot } from "/static/viewer.js";

const state = {
  csrf: null,
  user: null,
  dagRunId: null,
  previewSubject: null,
  previewRequest: null,
  previewFiles: null,
  lastRun: null,
  folderPick: null,
  uploading: false,
  uploadAbort: null,
  blockedPick: false,
  stages: [],
  selectedStage: null,
  viewer: null,
  controls: null,
  timer: null,
  retryingRunId: null,
  tabsInitializedFor: null,
};

const RUN_STATES = {
  queued: "排队中",
  running: "运行中",
  success: "成功",
  failed: "失败",
};
const TASK_STATES = {
  success: "成功",
  failed: "失败",
  running: "运行中",
  queued: "排队",
  scheduled: "已调度",
  up_for_retry: "等待重试",
  up_for_reschedule: "等待重排",
  upstream_failed: "上游失败",
  skipped: "跳过",
  deferred: "等待",
  none: "待执行",
  completed: "完成",
};
const TRANSPORT = {
  resolve_handoff: "接收目录",
  start_job: "提交作业",
  wait_for_job: "查询作业",
  confirm_job: "确认交付",
};
const CHECK_STATES = { passed: "通过", failed: "失败", not_run: "未执行", unsupported: "不支持" };
const STAGE_STATES = { not_run: "未执行", running: "执行中", completed: "完成", failed: "失败", blocked: "上游阻断" };
// Compact state words for the stepper; the full reason stays in the tooltip.
const STEP_STATE_SHORT = { completed: "已完成", running: "进行中", failed: "失败", blocked: "已阻断", not_run: "未执行" };
const AUTOMATIC_STATES = {
  passed: "自动校验通过",
  failed: "自动校验失败",
  unverified: "未通过独立校验",
  pending: "等待自动检查",
  queued: "排队中",
  running: "检查中",
};
const RETRY_REASONS = {
  transport_recovery: "继续原作业，不重新采集 CAD。",
  run_active: "运行尚未结束。",
  run_succeeded: "运行已成功。",
  run_missing: "该运行已不存在，请刷新运行列表。",
  resolution_not_success: "原输入未完成冻结，请查看问题并新建运行。",
  unknown_failed_task: "无法确认可恢复的任务，请联系平台维护人员。",
  resolution_or_capture_failed: "请修正工程目录或采集问题，再新建运行。",
  publication_failed: "发布失败，请修正问题后新建运行。",
  native_terminal_failure: "原作业已失败，修正问题后新建运行。",
  no_failed_transport_task: "没有可恢复的任务，请查看问题与发现。",
  endpoint_evidence_unavailable: "无法确认原作业状态，稍后再试。",
};

const $ = (id) => document.getElementById(id);

const UPLOAD_LIMITS_DEFAULT = {
  maxFiles: 4096,
  maxTotalBytes: 2 * 1024 * 1024 * 1024,
  maxFileBytes: 512 * 1024 * 1024,
  maxPathLength: 1024,
};
const uploadLimits = { ...UPLOAD_LIMITS_DEFAULT };

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

function folderLabel(value) {
  const name = String(value || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop();
  return name || "";
}

function normalizeRelativePath(raw) {
  const path = String(raw || "").replace(/\\/g, "/");
  const parts = [];
  for (const part of path.split("/")) {
    if (!part || part === ".") continue;
    if (part === ".." || /[\u0000-\u001f]/.test(part) || /[<>"|?*:]/.test(part)) return null;
    if (part.length > 255 || part.endsWith(".") || part.endsWith(" ")) return null;
    parts.push(part);
  }
  const normalized = parts.join("/");
  if (!normalized || normalized.length > uploadLimits.maxPathLength) return null;
  return normalized;
}

function applyUploadLimits(advertised) {
  const limits = advertised && typeof advertised === "object" ? advertised : {};
  const files = Number(limits.max_files);
  const bytes = Number(limits.max_bytes);
  const fileBytes = Number(limits.max_file_bytes);
  if (Number.isFinite(files) && files > 0) uploadLimits.maxFiles = Math.floor(files);
  if (Number.isFinite(bytes) && bytes > 0) uploadLimits.maxTotalBytes = Math.floor(bytes);
  if (Number.isFinite(fileBytes) && fileBytes > 0) uploadLimits.maxFileBytes = Math.floor(fileBytes);
  const label = $("upload-limits");
  if (label) {
    label.textContent = `总大小 ${formatBytes(uploadLimits.maxTotalBytes)}、文件数 ${uploadLimits.maxFiles}、单个文件 ${formatBytes(uploadLimits.maxFileBytes)}`;
  }
}

function folderPick(files) {
  const records = [];
  let top = null;
  let bytes = 0;
  for (const file of files) {
    const path = normalizeRelativePath(file.webkitRelativePath || file.name);
    if (!path) return { error: `存在不受支持的文件名：${String(file.name || "").slice(0, 120)}` };
    const name = path.split("/").pop() || "";
    if (name.startsWith("~$")) {
      return { error: "包含 ~$ 临时锁文件将拒绝上传：请关闭 SolidWorks 或删除这些文件后重试。" };
    }
    const first = path.split("/")[0];
    if (top === null) top = first;
    if (first !== top) return { error: "所选内容来自多个顶层文件夹，请一次选择一个完整的工程文件夹。" };
    records.push({ file, path });
    bytes += file.size;
  }
  if (!records.length || !top) return { error: "所选文件夹中没有可上传的文件。" };
  if (records.length > uploadLimits.maxFiles) return { error: `文件数量超出上限（${uploadLimits.maxFiles}）。` };
  if (bytes > uploadLimits.maxTotalBytes) return { error: `文件夹总大小超出上限（${formatBytes(uploadLimits.maxTotalBytes)}）。` };
  const oversized = records.find((item) => item.file.size > uploadLimits.maxFileBytes);
  if (oversized) return { error: `单个文件超出上限（${formatBytes(uploadLimits.maxFileBytes)}）：${oversized.path.slice(0, 120)}` };
  return { records, top, count: records.length, bytes };
}

function submitRun(pick, { onProgress } = {}) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const item of pick.records) form.append("files", item.file, item.path);
    const request = new XMLHttpRequest();
    request.open("POST", "/api/runs");
    request.withCredentials = true;
    request.setRequestHeader("Accept", "application/json");
    if (state.csrf) request.setRequestHeader("X-CSRF-Token", state.csrf);
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    });
    request.addEventListener("load", () => {
      let payload = {};
      try {
        payload = JSON.parse(request.responseText || "{}");
      } catch {
        payload = {};
      }
      if (request.status >= 200 && request.status < 300) {
        resolve(payload);
        return;
      }
      const error = new Error(payload.error || `上传失败（HTTP ${request.status}）`);
      error.status = request.status;
      if (payload && payload.dag_run_id) error.dagRunId = String(payload.dag_run_id);
      reject(error);
    });
    request.addEventListener("error", () => {
      const error = new Error("网络中断，上传响应未收到；如已提交成功，请在运行列表中确认，系统不会自动重试。");
      error.network = true;
      reject(error);
    });
    request.addEventListener("abort", () => {
      const error = new Error("已取消上传。");
      error.aborted = true;
      reject(error);
    });
    state.uploadAbort = { abort: () => request.abort() };
    request.send(form);
  });
}

function resetPick() {
  state.folderPick = null;
  state.blockedPick = false;
  $("folder-input").value = "";
  $("folder-summary").hidden = true;
  $("upload-status").hidden = true;
  $("start-button").disabled = true;
}

async function api(path, { method = "GET", body, signal } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (state.csrf && method !== "GET") headers["X-CSRF-Token"] = state.csrf;
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    signal,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let payload = {};
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = {};
    }
  }
  if (!response.ok) {
    const error = new Error(payload.error || `请求失败（HTTP ${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function setError(element, message) {
  element.textContent = message || "";
  element.hidden = !message;
}

function badge(text, kind) {
  const span = document.createElement("span");
  span.className = `badge ${kind || ""}`.trim();
  span.textContent = text;
  return span;
}

function expandable(parent, title) {
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = title;
  details.append(summary);
  parent.append(details);
  return details;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function showSession(session) {
  state.user = session.user;
  state.csrf = session.csrf_token;
  applyUploadLimits(session.upload_limits);
  $("login-card").hidden = true;
  $("workspace").hidden = false;
  $("account").hidden = false;
  $("account-user").textContent = session.user;
}

function clearPreview() {
  if (state.previewRequest) state.previewRequest.abort();
  state.previewRequest = null;
  state.previewSubject = null;
  state.previewFiles = null;
  state.controls = null;
  if (state.viewer) disposeRobot(state.viewer);
  $("viewer-card").hidden = true;
  $("viewer-placeholder").hidden = false;
  $("preview-meta").textContent = "";
  $("viewer-note").hidden = true;
  $("joint-controls").textContent = "";
  setError($("preview-note"), "");
  updatePreviewLayout();
}

function clearSession() {
  clearPreview();
  state.csrf = null;
  state.user = null;
  state.dagRunId = null;
  state.lastRun = null;
  state.tabsInitializedFor = null;
  if (state.timer) window.clearInterval(state.timer);
  state.timer = null;
  $("workspace").hidden = true;
  $("account").hidden = true;
  $("login-card").hidden = false;
  $("detail-card").hidden = true;
  $("viewer-card").hidden = true;
  $("retry-panel").hidden = true;
  setError($("retry-error"), "");
}

async function refreshRuns() {
  const list = $("runs");
  try {
    const payload = await api("/api/runs");
    list.textContent = "";
    if (!payload.runs.length) {
      const item = document.createElement("li");
      item.className = "muted";
      item.textContent = "暂无运行";
      list.append(item);
      return;
    }
    for (const run of payload.runs) {
      const item = document.createElement("li");
      if (run.dag_run_id === state.dagRunId) item.className = "selected";
      const button = document.createElement("button");
      button.type = "button";
      const folder = folderLabel(run.handoff_path) || "工程交付";
      const heading = document.createElement("span");
      heading.textContent = `${folder} · ${RUN_STATES[run.state] || run.state || "待执行"}`;
      const submitter = document.createElement("span");
      submitter.className = "run-submitter";
      submitter.textContent = `发起人：${run.user || "未记录"}`;
      button.append(heading, submitter);
      button.title = `${folder}\n${run.dag_run_id}`;
      button.addEventListener("click", () => selectRun(run.dag_run_id));
      item.append(button);
      list.append(item);
    }
  } catch (error) {
    list.textContent = `运行列表不可用：${error.message}`;
  }
}

function stageDotState(state) {
  if (state === "completed") return "ok";
  if (state === "failed") return "bad";
  if (state === "running") return "run";
  // Upstream-blocked and not-run stages stay neutral: nothing failed here.
  return "idle";
}

function runDotState(state) {
  if (state === "success") return "ok";
  if (state === "failed") return "bad";
  if (state === "running") return "run";
  return "idle";
}

function showUploadView() {
  $("upload-view").hidden = false;
  $("detail-card").hidden = true;
}

function showRunView() {
  $("upload-view").hidden = true;
  $("detail-card").hidden = false;
  if (state.viewer) {
    requestAnimationFrame(() => {
      state.viewer.resize?.();
      state.viewer.frame();
    });
  }
}

function selectInspectTab(name) {
  for (const tab of ["overview", "stage", "checks", "engineering"]) {
    const pane = $(`tab-${tab}`);
    if (pane) pane.hidden = tab !== name;
  }
  for (const button of document.querySelectorAll("#inspect-tabs .tab")) {
    button.classList.toggle("active", button.dataset.tab === name);
  }
}

function boundedJson(value, limit = 1400) {
  let text;
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }
  if (typeof text !== "string") text = String(text);
  return text.length > limit ? `${text.slice(0, limit)}\n…（内容过长，已截断）` : text;
}

function appendEvidence(parent, title, value) {
  if (value === undefined || value === null) return null;
  if (typeof value === "object" && !Array.isArray(value) && !Object.keys(value).length) return null;
  const details = expandable(parent, title);
  const pre = document.createElement("pre");
  pre.className = "raw";
  pre.textContent = boundedJson(value);
  details.append(pre);
  return details;
}

function inlineValue(value) {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  let text;
  try {
    text = JSON.stringify(value);
  } catch {
    return String(value);
  }
  return text.length > 240 ? `${text.slice(0, 240)}…` : text;
}

function stateKind(state) {
  if (state === "completed" || state === "passed" || state === "success") return "ok";
  if (state === "failed") return "bad";
  if (state === "running") return "run";
  return "";
}

function stageSelectionKey(runId) {
  return `portal.stage.${runId}`;
}

function chooseStageIndex(stages, runId) {
  if (!stages.length) return 0;
  let remembered = null;
  try {
    remembered = localStorage.getItem(stageSelectionKey(runId));
  } catch {
    remembered = null;
  }
  if (remembered) {
    const index = stages.findIndex((stage) => stage.id === remembered);
    if (index !== -1) return index;
  }
  const failed = stages.findIndex((stage) => stage.state === "failed");
  if (failed !== -1) return failed;
  const running = stages.findIndex((stage) => stage.state === "running");
  return running !== -1 ? running : 0;
}

function countChecks(rows) {
  let passed = 0;
  let executed = 0;
  for (const row of rows) {
    if (row.state === "passed") passed += 1;
    const ran = row.executed === true || (row.executed === undefined && (row.state === "passed" || row.state === "failed"));
    if (ran) executed += 1;
  }
  return `通过 ${passed}/${rows.length} · 已执行 ${executed}/${rows.length}`;
}

function countsText(counts, rows) {
  // Canonical report counts when present; row-derived numbers only as a safety net.
  if (counts && Number.isFinite(counts.total)) {
    const passed = Number.isFinite(counts.passed) ? counts.passed : 0;
    const executed = Number.isFinite(counts.executed) ? counts.executed : 0;
    return `通过 ${passed}/${counts.total} · 已执行 ${executed}/${counts.total}`;
  }
  return countChecks(rows);
}

function reportRow(row, boundary) {
  const summary = row.summary && typeof row.summary === "object" ? row.summary : {};
  return {
    id: String(row.id || ""),
    boundary,
    label: row.label_zh || row.label || String(row.id || ""),
    state: row.state || "not_run",
    stateZh: row.state_zh || CHECK_STATES[row.state] || "未执行",
    executed: typeof row.executed === "boolean" ? row.executed : undefined,
    scope: summary.scope_zh || "",
    expected: summary.expected,
    actual: summary.actual,
    details: row.raw_details,
  };
}

function stageList(run) {
  const report = run.report && Array.isArray(run.report.stages) ? run.report : null;
  if (!report) return [];
  return report.stages.map((stage) => {
    const counts = stage.counts && typeof stage.counts === "object" ? stage.counts : {};
    const boundaryCounts = counts.boundary && typeof counts.boundary === "object" ? counts.boundary : {};
    const independentCounts = counts.independent && typeof counts.independent === "object" ? counts.independent : {};
    const sum = (key) =>
      (Number.isFinite(boundaryCounts[key]) ? boundaryCounts[key] : 0) +
      (Number.isFinite(independentCounts[key]) ? independentCounts[key] : 0);
    return {
      id: stage.id,
      nameZh: stage.name_zh || stage.id,
      state: stage.state || "not_run",
      stateZh: stage.state_zh || "未执行",
      at: stage.at,
      counts,
      passed: sum("passed"),
      executed: sum("executed"),
      total: sum("total"),
      boundary: (Array.isArray(stage.boundary) ? stage.boundary : []).map((row) =>
        reportRow(row, row.boundary === "output" ? "output" : "input")),
      independent: (Array.isArray(stage.independent) ? stage.independent : []).map((row) => reportRow(row, "independent")),
      files: Array.isArray(stage.files) ? stage.files : [],
      unsupported: (Array.isArray(stage.unsupported) ? stage.unsupported : [])
        .map((item) => (item && typeof item === "object" ? item.label || item.id : item))
        .filter(Boolean),
      manualNote: typeof stage.manual_scope_note_zh === "string" ? stage.manual_scope_note_zh : "",
    };
  });
}

function checkRow(parent, row) {
  const item = document.createElement("div");
  item.className = `check-row state-${row.state || "none"}`;
  const head = document.createElement("div");
  head.className = "check-head";
  const name = document.createElement("span");
  name.className = "check-name";
  name.textContent = row.label || row.id || "检查";
  const chip = document.createElement("span");
  chip.className = `chip ${stateKind(row.state)}`.trim();
  chip.textContent = row.stateZh;
  head.append(name, chip);
  item.append(head);
  if (row.scope) {
    const scope = document.createElement("p");
    scope.className = "check-scope";
    scope.textContent = `检查内容：${row.scope}`;
    item.append(scope);
  }
  const expected = inlineValue(row.expected);
  const actual = inlineValue(row.actual);
  if (expected || actual) {
    const kv = document.createElement("p");
    kv.className = "check-kv";
    if (expected) kv.append(`预期：${expected}`);
    if (expected && actual) kv.append("；");
    if (actual) kv.append(`实际：${actual}`);
    item.append(kv);
  }
  appendEvidence(item, "原始证据", row.details);
  parent.append(item);
  return item;
}

function buildFailureCard(failure) {
  const card = document.createElement("div");
  card.className = "fail-card";
  const title = document.createElement("strong");
  title.textContent = failure.title_zh || "运行失败";
  card.append(title);
  if (failure.meaning_zh) {
    const meaning = document.createElement("p");
    meaning.textContent = failure.meaning_zh;
    card.append(meaning);
  }
  if (failure.stage_name_zh) {
    const stage = document.createElement("p");
    stage.className = "muted small";
    stage.textContent = `涉及阶段：${failure.stage_name_zh}`;
    card.append(stage);
  }
  const affected = failure.object
    ? String(failure.object)
    : Array.isArray(failure.unresolved_dependencies)
      ? failure.unresolved_dependencies
          .map((item) => (item && (item.name || item.path)) || "")
          .filter(Boolean)
          .join("、")
      : "";
  if (affected) {
    const object = document.createElement("p");
    object.textContent = `涉及对象：${affected}`;
    card.append(object);
  }
  const raw = {};
  for (const key of ["raw_type", "raw_error", "raw_detail"]) {
    if (failure[key] !== undefined && failure[key] !== null) raw[key] = failure[key];
  }
  if (Object.keys(raw).length) appendEvidence(card, "原始错误（供排查）", raw);
  return card;
}

function renderStageDetail() {
  const root = $("stage-detail");
  root.textContent = "";
  if (!state.stages.length) {
    root.textContent = "报告数据暂不可用：无法显示阶段检查，请刷新重试或联系平台维护人员。";
    return;
  }
  const stage = state.stages[state.selectedStage];
  if (!stage) return;

  const summary = document.createElement("div");
  summary.className = "stage-summary";
  const head = document.createElement("div");
  head.className = "stage-summary-head";
  const title = document.createElement("strong");
  title.textContent = stage.nameZh;
  const chip = document.createElement("span");
  chip.className = `chip ${stateKind(stage.state)}`.trim();
  chip.textContent = stage.stateZh;
  head.append(title, chip);
  summary.append(head);
  const counts = document.createElement("p");
  counts.className = "stage-counts muted small";
  const pieces = [];
  if (stage.boundary.length) pieces.push(`边界检查：${countsText(stage.counts.boundary, stage.boundary)}`);
  if (stage.independent.length) pieces.push(`独立检查：${countsText(stage.counts.independent, stage.independent)}`);
  counts.textContent = pieces.join("　•　") || "无检查记录";
  summary.append(counts);
  if (stage.at) {
    const at = document.createElement("p");
    at.className = "muted small";
    at.textContent = `记录时间：${formatTime(stage.at)}`;
    summary.append(at);
  }
  root.append(summary);

  const groups = [
    ["输入检查", stage.boundary.filter((row) => row.boundary === "input")],
    ["输出检查", stage.boundary.filter((row) => row.boundary === "output")],
    ["独立检查", stage.independent],
  ];
  for (const [heading, rows] of groups) {
    if (!rows.length) continue;
    const group = document.createElement("div");
    group.className = "check-group";
    const titleRow = document.createElement("h4");
    titleRow.textContent = `${heading}（${countChecks(rows)}）`;
    group.append(titleRow);
    for (const row of rows) checkRow(group, row);
    root.append(group);
  }

  const fileRows = (stage.files || []).map((file) => ({
    label: file.name_zh || file.label || file.path || "文件",
    path: file.path || "",
    availability: file.availability || "",
    count: Number.isFinite(file.files) ? file.files : null,
  }));
  if (fileRows.length) {
    const details = expandable(root, `产出与记录（${fileRows.length} 项）`);
    const list = document.createElement("div");
    list.className = "stage-files";
    const authorized = state.previewFiles || {};
    for (const row of fileRows) {
      const line = document.createElement("p");
      line.className = "file-line";
      if (row.path && Object.prototype.hasOwnProperty.call(authorized, row.path)) {
        const link = document.createElement("a");
        link.href = `/api/runs/${encodeURIComponent(state.dagRunId)}/artifacts/${String(row.path)
          .split("/")
          .map(encodeURIComponent)
          .join("/")}`;
        link.target = "_blank";
        link.rel = "noopener";
        link.textContent = [row.label, row.path].filter(Boolean).join(" · ");
        line.append(link);
      } else {
        line.textContent = [row.label, row.path].filter(Boolean).join(" · ") || "—";
      }
      if (Number.isFinite(row.count) && row.count > 0) {
        const tag = document.createElement("code");
        tag.textContent = `${row.count} 个文件`;
        line.append(" ", tag);
      } else if (row.availability) {
        const tag = document.createElement("code");
        tag.textContent = row.availability;
        line.append(" ", tag);
      }
      list.append(line);
    }
    details.append(list);
  }

  if (stage.manualNote) {
    const note = document.createElement("p");
    note.className = "stage-note muted small";
    note.textContent = stage.manualNote;
    root.append(note);
  }
  if (stage.unsupported.length) {
    const row = document.createElement("p");
    row.className = "stage-note muted small";
    row.append("不支持项（待工程确认，不代表通过）：");
    for (const text of stage.unsupported) {
      const tag = document.createElement("span");
      tag.className = "chip";
      tag.textContent = text;
      row.append(" ", tag);
    }
    root.append(row);
  }
}

function updatePreviewLayout() {
  const workspace = document.querySelector(".run-workspace");
  if (!workspace) return;
  const has = Boolean(state.previewSubject);
  workspace.classList.toggle("has-preview", has);
  workspace.classList.toggle("no-preview", !has);
}

function renderStepper() {
  const stepper = $("stages");
  stepper.textContent = "";
  state.stages.forEach((stage, index) => {
    const step = document.createElement("button");
    step.type = "button";
    step.className = `step ${stage.state || ""}${index === state.selectedStage ? " selected" : ""}`.trim();
    step.setAttribute("aria-pressed", index === state.selectedStage ? "true" : "false");
    const dot = document.createElement("span");
    dot.className = `dot ${stageDotState(stage.state)}`;
    const label = document.createElement("span");
    label.className = "step-label";
    label.textContent = stage.nameZh || stage.id;
    const meta = document.createElement("span");
    meta.className = "step-meta";
    if (!Number.isFinite(stage.total) || stage.total <= 0) {
      // No recorded checks: a short neutral state word instead of a failed percentage.
      meta.textContent = STEP_STATE_SHORT[stage.state] || stage.stateZh;
    } else {
      meta.textContent = `${Number.isFinite(stage.passed) ? stage.passed : 0}/${stage.total} 通过`;
    }
    const tooltip = `${stage.nameZh} · ${stage.stateZh}${
      Number.isFinite(stage.total) && stage.total > 0
        ? ` · 通过 ${Number.isFinite(stage.passed) ? stage.passed : 0}/${stage.total} · 已执行 ${
            Number.isFinite(stage.executed) ? stage.executed : 0
          }/${stage.total}`
        : ""
    }`;
    step.title = tooltip;
    step.setAttribute("aria-label", tooltip);
    step.append(dot, label, meta);
    step.addEventListener("click", () => {
      const runId = state.dagRunId;
      try {
        localStorage.setItem(stageSelectionKey(runId), stage.id);
      } catch {
        // Remembering the chosen stage is best-effort only.
      }
      state.selectedStage = index;
      if (state.lastRun) renderRun(state.lastRun);
      selectInspectTab("stage");
    });
    stepper.append(step);
  });
}

function renderRun(run) {
  const runId = String(run.dag_run_id || "");
  $("run-heading").textContent = folderLabel(run.handoff_path) || "工程文件夹";
  $("run-state-label").textContent = RUN_STATES[run.state] || run.state || "";
  $("run-dot").className = `dot ${runDotState(run.state)}`;
  $("run-initiator").textContent = `发起人：${run.operator || run.user || "未记录"}`;
  const meta = $("run-meta");
  meta.textContent = "";
  const report = run.report && Array.isArray(run.report.stages) ? run.report : null;
  const coverage = run.coverage || {};
  const rows = [
    ["工程文件夹", folderLabel(run.handoff_path) || "—"],
    ["状态", RUN_STATES[run.state] || run.state || "—"],
    ["开始时间", formatTime(run.started_at)],
    ["结束时间", formatTime(run.ended_at)],
  ];
  const measured = report && report.measured ? report.measured : null;
  if (measured) {
    if (measured.subject_sha256) rows.push(["交付摘要", `${String(measured.subject_sha256).slice(0, 12)}…`]);
    const window = measured.expected_mass_window;
    if (window && Number.isFinite(window.mass_kg)) {
      const range = Array.isArray(window.expected_kg) && window.expected_kg.length === 2
        ? `[${window.expected_kg[0]}，${window.expected_kg[1]}] kg`
        : "—";
      rows.push(["质量期望窗口", `实测 ${window.mass_kg} kg，窗口 ${range}`]);
    }
    const closure = measured.mass_closure;
    if (closure && Number.isFinite(closure.urdf_mass_kg)) {
      const delta = Number.isFinite(closure.delta_kg) ? `，差值 ${closure.delta_kg} kg` : "";
      rows.push([
        "质量闭合",
        `URDF ${closure.urdf_mass_kg} kg / 整机 CAD ${closure.whole_cad_mass_kg} kg${delta}`,
      ]);
    }
  }
  const ident = $("run-ident");
  if (ident) ident.textContent = runId;
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    meta.append(dt, dd);
  }

  const headline = $("run-headline");
  const headlineText = report && report.overall ? report.overall.headline_zh : "";
  headline.textContent = headlineText || "";
  headline.hidden = !headlineText;
  const failureCard = $("failure-card");
  failureCard.textContent = "";
  if (report && report.failure) {
    failureCard.append(buildFailureCard(report.failure));
    failureCard.hidden = false;
  } else {
    failureCard.hidden = true;
  }
  if (state.tabsInitializedFor !== runId) {
    state.tabsInitializedFor = runId;
    selectInspectTab(run.state === "failed" ? "stage" : "overview");
  }

  const retry = run.retry || {};
  const canRetry = run.can_manage === true && retry.eligible === true;
  const retryReason = typeof retry.reason === "string" ?
    RETRY_REASONS[retry.reason] || "暂不可重试，请查看检查结果。" : "";
  $("retry-panel").hidden = !canRetry && !(run.state === "failed" && retryReason);
  $("retry-button").hidden = !canRetry;
  $("retry-button").disabled = state.retryingRunId !== null;
  $("retry-button").textContent = state.retryingRunId === runId ? "正在重试…" : "重试";
  $("retry-note").textContent = retry.eligible === true && run.can_manage !== true ?
    "该运行可由发起人或平台管理员重试。" : retryReason;

  const progress = $("task-progress");
  progress.textContent = "";
  for (const task of run.tasks || []) {
    const chip = document.createElement("span");
    chip.className = `chip ${task.state || ""}`.trim();
    chip.textContent = `${TRANSPORT[task.task_id] || task.task_id}：${TASK_STATES[task.state] || task.state || "—"}`;
    progress.append(chip);
  }

  state.stages = stageList(run);
  state.selectedStage = chooseStageIndex(state.stages, runId);
  renderStepper();
  renderStageDetail();

  const automatic = $("automatic");
  automatic.textContent = "";
  const automaticState = (run.automatic && run.automatic.state) || "pending";
  const automaticKind = automaticState === "passed" ? "ok" : automaticState === "failed" || automaticState === "unverified" ? "bad" : "pending";
  const chipRow = document.createElement("div");
  chipRow.className = "chip-row";
  chipRow.append(badge(AUTOMATIC_STATES[automaticState] || automaticState, automaticKind));
  if (run.automatic && run.automatic.message) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = run.automatic.message;
    chipRow.append(note);
  }
  for (const text of (coverage.automatic && coverage.automatic.unsupported) || []) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = `未自动覆盖：${text}`;
    chipRow.append(chip);
  }
  automatic.append(chipRow);
  if (report) {
    for (const stage of state.stages) {
      const rows = [...stage.boundary, ...stage.independent];
      if (!rows.length) continue;
      const group = document.createElement("div");
      group.className = "check-group";
      const titleRow = document.createElement("h4");
      titleRow.textContent = `${stage.nameZh}（${countChecks(rows)}）`;
      group.append(titleRow);
      for (const row of rows) checkRow(group, row);
      automatic.append(group);
    }
  } else {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent = "报告数据暂不可用：无法显示逐项检查，请刷新重试或联系平台维护人员。";
    chipRow.append(note);
  }

  const confirmations = $("confirmations");
  confirmations.textContent = "";
  const external = report && report.external_review && typeof report.external_review === "object" ? report.external_review : null;
  const scopes = external && Array.isArray(external.scopes) ? external.scopes : [];
  const engineeringState = report && report.overall ? report.overall.engineering_state : null;
  if (!report) {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent = "报告数据暂不可用：工程评审范围无法显示，请刷新重试或联系平台维护人员。";
    confirmations.append(note);
  } else if (engineeringState !== "external_review") {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent = "工程评审范围未就绪：独立验证通过后，这里显示需要人工确认的外部事实。";
    confirmations.append(note);
  } else if (scopes.length) {
    const lead = document.createElement("p");
    lead.className = "muted small";
    lead.textContent = "工程评审范围（自动检查无法核验、需由外部评审确认的事实）：";
    confirmations.append(lead);
    for (const scope of scopes) {
      const item = document.createElement("div");
      item.className = "review-fact";
      const label = document.createElement("strong");
      label.textContent = scope.label || scope.id || "";
      item.append(label);
      if (scope.scope) {
        const text = document.createElement("p");
        text.textContent = `确认内容：${scope.scope}`;
        item.append(text);
      }
      if (scope.automatic_exclusion) {
        const text = document.createElement("p");
        text.className = "muted small";
        text.textContent = `不重复检查：${scope.automatic_exclusion}`;
        item.append(text);
      }
      const stageName = (state.stages.find((entry) => entry.id === scope.stage) || {}).nameZh;
      if (stageName) {
        const text = document.createElement("p");
        text.className = "muted small";
        text.textContent = `涉及阶段：${stageName}`;
        item.append(text);
      }
      confirmations.append(item);
    }
    if (external.note_zh) {
      const note = document.createElement("p");
      note.className = "muted small";
      note.textContent = external.note_zh;
      confirmations.append(note);
    }
  } else {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent = "报告未列出需要人工确认的外部事实。";
    confirmations.append(note);
  }

  const findings = $("findings");
  findings.textContent = "";
  const failure = report && report.failure ? report.failure : null;
  // The auto finding repeating the failure card's raw error would be a second
  // red container for the same fact; show each failure once.
  const visibleFindings = (run.findings || []).filter(
    (finding) => !(failure && !finding.id && finding.message === failure.raw_error),
  );
  if (!visibleFindings.length) {
    const item = document.createElement("li");
    item.className = "muted";
    item.textContent = "暂无问题";
    findings.append(item);
  }
  for (const finding of visibleFindings) {
    const item = document.createElement("li");
    if (finding.severity && finding.severity !== "error") item.className = "warn";
    const message = document.createElement("div");
    message.textContent = finding.message || "未提供说明";
    const context = document.createElement("div");
    context.className = "object";
    context.textContent = [finding.id, finding.stage, finding.object].filter(Boolean).join(" · ") || "—";
    item.append(message, context);
    if (finding.evidence && Object.keys(finding.evidence).length) {
      const evidence = document.createElement("pre");
      evidence.className = "object";
      evidence.textContent = JSON.stringify(finding.evidence, null, 2);
      expandable(item, "查看证据").append(evidence);
    }
    findings.append(item);
  }

  const pr = $("pr");
  pr.textContent = "";
  if (run.pr && run.pr.url) {
    const link = document.createElement("a");
    link.href = run.pr.url;
    link.target = "_blank";
    link.rel = "noreferrer noopener";
    link.textContent = "查看 PR";
    link.title = String(run.pr.url || "");
    pr.append(link);
    const stateText = document.createElement("span");
    stateText.className = "muted small";
    stateText.textContent = run.pr.state || "";
    pr.append(stateText);
    pr.hidden = false;
  } else if (automaticState === "passed") {
    pr.textContent = "模型已通过独立校验；PR 尚未创建或发布服务失败，可先查看左侧已验证 URDF。";
    pr.hidden = false;
  } else {
    pr.hidden = true;
  }
  updatePreviewLayout();
}

async function loadPreview(dagRunId) {
  if (state.previewRequest) return;
  const request = new AbortController();
  state.previewRequest = request;
  const note = $("viewer-note");
  try {
    const preview = await api(`/api/runs/${encodeURIComponent(dagRunId)}/preview`, { signal: request.signal });
    if (request.signal.aborted || state.dagRunId !== dagRunId) return;
    if (state.previewSubject === preview.subject_sha256) return;
    $("preview-meta").textContent = `交付摘要 ${String(preview.subject_sha256).slice(0, 12)}… · URDF ${preview.urdf}`;
    note.hidden = true;
    setError($("preview-note"), "");
    if (!state.viewer) state.viewer = createViewer($("viewer"));
    const artifactUrl = (name) =>
      `/api/runs/${encodeURIComponent(dagRunId)}/artifacts/${String(name)
        .split("/")
        .map(encodeURIComponent)
        .join("/")}`;
    const loaded = await loadRobot(state.viewer, {
      urdfUrl: artifactUrl(preview.urdf),
      files: preview.files,
      artifactUrl,
      signal: request.signal,
      onWarning: (warnings) => {
        if (warnings.length) {
          note.textContent = warnings.join("；");
          note.hidden = false;
        }
      },
    });
    if (request.signal.aborted || state.dagRunId !== dagRunId) return;
    state.controls = buildJointControls($("joint-controls"), loaded.joints, {});
    state.previewSubject = preview.subject_sha256;
    state.previewFiles = preview.files || {};
    if (state.lastRun) renderRun(state.lastRun);
    $("viewer-card").hidden = false;
    $("viewer-placeholder").hidden = true;
    state.viewer.frame();
    updatePreviewLayout();
  } catch (error) {
    if (request.signal.aborted || state.dagRunId !== dagRunId) return;
    if (state.viewer) disposeRobot(state.viewer);
    state.controls = null;
    state.previewSubject = null;
    state.previewFiles = null;
    if (state.lastRun) renderRun(state.lastRun);
    $("joint-controls").textContent = "";
    updatePreviewLayout();
    if (error.status === 404 || error.status === 409) {
      setError($("preview-note"), `尚未提供已验证交付：${error.message}`);
      return;
    }
    setError($("preview-note"), `交付预览不可用：${error.message}`);
  } finally {
    if (state.previewRequest === request) state.previewRequest = null;
  }
}

async function poll() {
  const dagRunId = state.dagRunId;
  if (!dagRunId) return;
  try {
    const run = await api(`/api/runs/${encodeURIComponent(dagRunId)}`);
    if (state.dagRunId !== dagRunId) return;
    setError($("run-error-detail"), "");
    const verified = run.automatic && run.automatic.state === "passed";
    if (!verified) clearPreview();
    const previous = state.lastRun;
    state.lastRun = run;
    renderRun(run);
    if (verified) {
      await loadPreview(dagRunId);
    }
    if (state.dagRunId !== dagRunId) return;
    const jobDone = run.job && (run.job.status === "passed" || run.job.status === "failed");
    if ((run.state === "success" || run.state === "failed") && jobDone && (!verified || state.previewSubject)) {
      if (state.timer) window.clearInterval(state.timer);
      state.timer = null;
    }
    const terminalNow = run.state === "success" || run.state === "failed";
    if (terminalNow && !(previous && (previous.state === "success" || previous.state === "failed"))) {
      await refreshRuns();
    }
  } catch (error) {
    if (state.dagRunId !== dagRunId) return;
    if (error.status === 401) {
      clearSession();
      setError($("login-error"), error.message);
      return;
    }
    if (error.status === 403 || error.status === 404) {
      clearPreview();
      state.lastRun = null;
      state.dagRunId = null;
      state.selectedStage = null;
      state.tabsInitializedFor = null;
      if (state.timer) window.clearInterval(state.timer);
      state.timer = null;
      $("retry-panel").hidden = true;
      showUploadView();
      await refreshRuns();
      setError($("run-error"), error.message);
      return;
    }
    setError($("run-error-detail"), error.message);
  }
}

async function selectRun(dagRunId) {
  if (state.timer) window.clearInterval(state.timer);
  state.timer = null;
  clearPreview();
  state.dagRunId = dagRunId;
  state.lastRun = null;
  state.selectedStage = null;
  state.tabsInitializedFor = null;
  $("retry-panel").hidden = true;
  setError($("run-error-detail"), "");
  setError($("retry-error"), "");
  showRunView();
  await refreshRuns();
  if (state.dagRunId !== dagRunId) return;
  state.timer = window.setInterval(() => {
    void poll();
  }, 3000);
  await poll();
}

async function retryRun() {
  const dagRunId = state.dagRunId;
  const run = state.lastRun;
  if (
    !dagRunId || state.retryingRunId !== null || !run ||
    run.dag_run_id !== dagRunId || run.can_manage !== true ||
    !run.retry || run.retry.eligible !== true
  ) return;
  state.retryingRunId = dagRunId;
  setError($("retry-error"), "");
  renderRun(run);
  try {
    await api(`/api/runs/${encodeURIComponent(dagRunId)}/retry`, { method: "POST", body: {} });
    if (state.dagRunId !== dagRunId) return;
    if (!state.timer) state.timer = window.setInterval(() => { void poll(); }, 3000);
    await poll();
    await refreshRuns();
  } catch (error) {
    if (error.status === 401) {
      clearSession();
      setError($("login-error"), error.message);
    } else if (state.dagRunId === dagRunId) {
      setError($("retry-error"), error.message);
      if (error.status === 403 || error.status === 404 || error.status === 409) {
        await poll();
        await refreshRuns();
      }
    }
  } finally {
    state.retryingRunId = null;
    if (state.lastRun && state.lastRun.dag_run_id === state.dagRunId) renderRun(state.lastRun);
  }
}

function wire() {
  $("new-run-button").addEventListener("click", () => {
    showUploadView();
    setError($("run-error"), "");
    void refreshRuns();
  });
  for (const button of document.querySelectorAll("#inspect-tabs .tab")) {
    button.addEventListener("click", () => selectInspectTab(button.dataset.tab));
  }
  selectInspectTab("overview");

  $("retry-button").addEventListener("click", retryRun);
  $("logout").addEventListener("click", async () => {
    try {
      await api("/api/session", { method: "DELETE" });
    } catch {
      // the local session is cleared even if Airflow is unreachable
    }
    clearSession();
  });

  $("choose-button").addEventListener("click", () => $("folder-input").click());

  $("folder-input").addEventListener("change", () => {
    state.blockedPick = false;
    const picked = folderPick(Array.from($("folder-input").files || []));
    const summary = $("folder-summary");
    if (picked.error) {
      state.folderPick = null;
      summary.hidden = true;
      setError($("run-error"), picked.error);
      $("start-button").disabled = true;
      return;
    }
    state.folderPick = picked;
    summary.hidden = false;
    summary.textContent = `${picked.top} · ${picked.count} 个文件 · ${formatBytes(picked.bytes)}`;
    setError($("run-error"), "");
    $("start-button").disabled = false;
  });

  $("cancel-button").addEventListener("click", () => {
    if (state.uploadAbort) state.uploadAbort.abort();
  });

  $("run-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.uploading) return;
    const picked = state.folderPick;
    if (!picked) {
      setError($("run-error"), "请先选择本机的工程文件夹。");
      return;
    }
    if (state.blockedPick) {
      setError($("run-error"), "上一提交状态尚未确认；请在上方运行列表中确认，或重新选择文件夹后再提交。");
      return;
    }
    const start = $("start-button");
    const status = $("upload-status");
    state.uploading = true;
    start.disabled = true;
    $("choose-button").disabled = true;
    $("folder-input").disabled = true;
    $("cancel-button").hidden = false;
    status.hidden = false;
    status.textContent = "正在上传工程文件夹…";
    setError($("run-error"), "");
    try {
      const payload = await submitRun(picked, {
        onProgress: (ratio) => {
          if (ratio >= 1) {
            status.textContent = "上传完成，正在创建运行…";
            $("cancel-button").hidden = true;
          } else {
            status.textContent = `正在上传工程文件夹… ${Math.round(ratio * 100)}%`;
          }
        },
      });
      status.textContent = "上传完成，正在创建运行…";
      await selectRun(payload.dag_run_id);
      resetPick();
    } catch (error) {
      if (error.aborted) {
        status.textContent = "已取消上传；正在刷新运行列表，如无新运行则表示未提交。";
        await refreshRuns().catch(() => {});
      } else if (error.network) {
        status.textContent = error.message;
        await refreshRuns().catch(() => {});
      } else if (error.status === 401) {
        clearSession();
        resetPick();
        setError($("login-error"), "会话已失效，请重新登录后再试。");
        return;
      } else if (error.status === 502 && error.dagRunId) {
        state.blockedPick = true;
        setError(
          $("run-error"),
          `启动状态尚未确认，请检查运行列表；不要重复提交。运行 ID: ${error.dagRunId}`,
        );
        await refreshRuns().catch(() => {});
      } else {
        setError($("run-error"), error.message);
      }
    } finally {
      state.uploading = false;
      state.uploadAbort = null;
      $("choose-button").disabled = false;
      $("folder-input").disabled = false;
      $("cancel-button").hidden = true;
      if (state.folderPick && !state.blockedPick) {
        start.disabled = false;
      } else {
        start.disabled = true;
        status.hidden = true;
      }
    }
  });

  $("reset-joints").addEventListener("click", () => {
    if (state.controls) state.controls.reset();
  });
}

async function boot() {
  wire();
  try {
    const session = await api("/api/session");
    showSession(session);
    await refreshRuns();
  } catch (error) {
    clearSession();
    if (error.status === 401) setError($("login-error"), error.message);
  }
}

void boot();
