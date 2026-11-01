// Operator page logic. All Airflow and Windows endpoint credentials stay on the server; this
// module only talks to the portal's own JSON API and the digest-verified artifact routes.
import { buildJointControls, createViewer, disposeRobot, loadRobot } from "/static/viewer.js";
import { createInspection } from "/static/inspection.js";
import {
  menuAction,
  overlayRun,
  resolveDeleted,
  resolveTitle,
  shouldRefreshRunList,
} from "/static/run_history.js";
import { activityView } from "/static/activity.js";
import {
  closureRows,
  ensureSingleViewer,
  frameRows,
  infoPanelRows,
  jointRows,
  linkRows,
  previewDiagnosticState,
  previewExpansion,
  purposeLabel,
  registeredVerificationArtifact,
  stageEntersPreview,
  stageSummaryRows,
  summaryIdentity,
} from "/static/preview_logic.js";

const state = {
  csrf: null,
  user: null,
  dagRunId: null,
  previewSubject: null,
  previewKey: null,
  previewFormat: "urdf",
  mjcfView: null,
  previewRequest: null,
  previewFiles: null,
  previewData: null,
  previewCollapsed: false,
  previewCollapseFor: null,
  lastRun: null,
  folderPick: null,
  assemblyChoice: null,
  uploading: false,
  uploadAbort: null,
  submissionKey: null,
  blockedPick: false,
  targetsRetryPick: null,
  targetsRequest: 0,
  stages: [],
  selectedStage: null,
  viewer: null,
  controls: null,
  timer: null,
  pollRequest: null,
  activityTimer: null,
  retryingRunId: null,
  detailsInitializedFor: null,
  rerunningRunId: null,
  rerunNotice: null,
  rerunError: null,
  rerunMainByRun: {},
  rerunKeyByRun: {},
  runsList: [],
  listRequest: null,
  runView: "active",
  runsNextOffset: null,
  runsLoadingMore: false,
  runActionsFor: null,
};

const RUN_STATES = {
  queued: "Queued",
  running: "Running",
  waiting: "Awaiting external result",
  success: "Succeeded",
  failed: "Failed",
  unresolved: "Unresolved",
  cancelled: "Cancelled",
};
const TASK_STATES = {
  success: "Succeeded",
  failed: "Failed",
  running: "Running",
  queued: "Queued",
  scheduled: "Scheduled",
  up_for_retry: "Waiting to retry",
  up_for_reschedule: "Waiting to reschedule",
  upstream_failed: "Upstream failed",
  skipped: "Skipped",
  deferred: "Deferred",
  none: "Not started",
  completed: "Completed",
};
const TASK_LABELS = {
  freeze: "Freeze inputs",
  discover: "Discover structure",
  capture: "Capture evidence",
  generate: "Generate URDF",
  verify: "Independent verification",
  mjcf: "Convert and verify MJCF",
  publish: "Publish review PR",
};
const CHECK_STATES = { passed: "Passed", failed: "Failed", not_run: "Not run", unsupported: "Unsupported" };
// Compact state words for the stepper; the full reason stays in the tooltip.
const STEP_STATE_SHORT = {
  not_run: "Not run",
  waiting: "Waiting",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  blocked: "Blocked",
  cancelled: "Cancelled",
  unresolved: "Unresolved",
};
const STAGE_SELECTION_PRIORITY = [
  "unresolved",
  "failed",
  "cancelled",
  "running",
  "waiting",
  "blocked",
];
const PREREQ_STATE = { ok: "ready", invalid: "unavailable", absent: "missing", changed: "changed", unknown: "unknown" };
const AUTOMATIC_STATES = {
  passed: "Automatic checks passed",
  failed: "Automatic checks failed",
  unverified: "No formal verification qualification",
  waiting: "Awaiting external result",
  unresolved: "External result needs reconciliation",
  cancelled: "Run cancelled before verification completed",
  pending: "Waiting for automatic checks",
  queued: "Queued",
  running: "Checking",
};
const RETRY_REASONS = {
  temporary_disconnection: "Continue the original job without repeating CAD capture.",
  evidence_corruption: "Capture evidence is sealed; recovery rereads sealed evidence without repeating CAD.",
  engineering_terminal_failure: "An engineering stage failed terminally; fix the findings and start a linked run.",
  publication_response_unknown: "Publication response is unknown; reconcile the result first, because in-place retry is not allowed.",
  run_active: "The run has not finished.",
  run_succeeded: "The run succeeded.",
  run_missing: "The run no longer exists; refresh the run list.",
  admission_failed: "The original input was not frozen; review the findings and start a new run.",
  unknown_failed_task: "No recoverable task can be identified; contact platform maintenance.",
  no_failed_operation: "No failed operation can be recovered; review the issues and findings.",
  native_terminal_failure: "The original job terminated; review the checks and start a linked run.",
  native_evidence_unavailable: "Native job evidence cannot be confirmed; retry later.",
  sealed_evidence_reimport: "Capture evidence is sealed; recovery rereads sealed evidence without repeating CAD.",
  portable_retry: "Continue side-effect-free stages without repeating CAD capture.",
  endpoint_evidence_unavailable: "The original job state cannot be confirmed; retry later.",
  deleted: "This delivery is hidden; restore it before retrying or rerunning.",
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

function assemblyLabel(value) {
  return folderLabel(value).replace(/\.sldasm$/i, "");
}

function shortRunId(value) {
  const text = String(value || "");
  return text.split("-").pop() || text;
}

function rerunStageName(rows, stageId) {
  const row = rows.find((item) => item && item.stage === stageId);
  return row ? row.name || row.stage : String(stageId || "");
}

function normalizeRelativePath(raw) {
  const path = String(raw || "");
  if (path.includes("\\")) return null;
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
    label.textContent = `${formatBytes(uploadLimits.maxTotalBytes)} total, ${uploadLimits.maxFiles} files, ${formatBytes(uploadLimits.maxFileBytes)} per file`;
  }
}

function folderPick(files) {
  const records = [];
  let top = null;
  let bytes = 0;
  for (const file of files) {
    const path = normalizeRelativePath(file.webkitRelativePath || file.name);
    if (!path) return { error: `Unsupported filename: ${String(file.name || "").slice(0, 120)}` };
    const name = path.split("/").pop() || "";
    if (name.startsWith("~$")) {
      return { error: "Upload contains ~$ temporary lock files. Close SolidWorks or remove them before retrying." };
    }
    const first = path.split("/")[0];
    if (top === null) top = first;
    if (first !== top) return { error: "The selection spans multiple top-level folders; select one complete engineering folder." };
    records.push({ file, path });
    bytes += file.size;
  }
  if (!records.length || !top) return { error: "The selected folder contains no uploadable files." };
  if (records.length > uploadLimits.maxFiles) return { error: `File count exceeds the ${uploadLimits.maxFiles}-file limit.` };
  if (bytes > uploadLimits.maxTotalBytes) return { error: `Folder size exceeds the ${formatBytes(uploadLimits.maxTotalBytes)} limit.` };
  const oversized = records.find((item) => item.file.size > uploadLimits.maxFileBytes);
  if (oversized) return { error: `A file exceeds the ${formatBytes(uploadLimits.maxFileBytes)} limit: ${oversized.path.slice(0, 120)}` };
  const prefix = `${top}/`;
  const assemblies = records
    .map((item) => item.path.slice(prefix.length))
    .filter((relative) => relative.toLowerCase().endsWith(".sldasm"))
    .sort();
  return { records, top, count: records.length, bytes, assemblies };
}

function resetAssemblyRow() {
  state.assemblyChoice = null;
  $("assembly-row").hidden = true;
  $("assembly-select").textContent = "";
  $("assembly-select").hidden = true;
  $("assembly-note").hidden = true;
  $("assembly-note").textContent = "";
}

// One delivered assembly must be provable: auto-select a sole candidate, require an explicit
// choice when several exist, and refuse folders without any .SLDASM (platform admission requires
// at least one assembly, so such an upload would be rejected anyway).
function applyAssemblies(pick) {
  const row = $("assembly-row");
  const select = $("assembly-select");
  const note = $("assembly-note");
  const assemblies = pick.assemblies || [];
  state.assemblyChoice = null;
  select.textContent = "";
  row.hidden = false;
  if (!assemblies.length) {
    select.hidden = true;
    note.hidden = false;
    note.textContent = "The selected folder contains no .SLDASM file; select a folder that contains the main assembly.";
    return;
  }
  if (assemblies.length === 1) {
    state.assemblyChoice = assemblies[0];
    select.hidden = true;
    note.hidden = false;
    note.textContent = `Main assembly (automatic): ${assemblies[0]}`;
    return;
  }
  select.hidden = false;
  note.hidden = true;
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Select the main assembly...";
  placeholder.disabled = true;
  placeholder.selected = true;
  select.append(placeholder);
  for (const relative of assemblies) {
    const option = document.createElement("option");
    option.value = relative;
    option.textContent = relative;
    select.append(option);
  }
}

async function loadTargets(pick) {
  const requestId = state.targetsRequest + 1;
  state.targetsRequest = requestId;
  $("targets-retry").hidden = true;
  const row = $("target-row");
  const select = $("target-select");
  const summary = $("change-summary");
  const note = $("target-note");
  select.textContent = "";
  row.hidden = false;
  select.hidden = false;
  summary.hidden = false;
  const main = state.assemblyChoice ? `${pick.top}/${state.assemblyChoice}` : "";
  let payload;
  try {
    payload = await api(
      `/api/targets?folder=${encodeURIComponent(pick.top)}&main_assembly=${encodeURIComponent(main)}`
    );
  } catch (error) {
    if (requestId !== state.targetsRequest || state.folderPick !== pick) return;
    if (error.status === 401) {
      state.targetsRetryPick = pick;
      return;
    }
    // A failed fetch is not "no targets configured": keep the pick so the fetch retries once a
    // session is ready again, and say what happened instead of misreporting an empty platform.
    state.targetsRetryPick = pick;
    const failure = document.createElement("option");
    failure.value = "";
    failure.textContent = "Failed to load publication targets";
    failure.disabled = true;
    failure.selected = true;
    select.append(failure);
    note.hidden = false;
    note.textContent = "Publication targets could not be loaded. Keep this folder selected and reload targets.";
    $("targets-retry").hidden = false;
    updateStartEnabled();
    return;
  }
  if (requestId !== state.targetsRequest || state.folderPick !== pick) return;
  state.targetsRetryPick = null;
  const targets = payload.targets || [];
  const placeholder = document.createElement("option");
  placeholder.value = "";
  // Success with configured:false lands here; a fetch failure never does (handled above).
  placeholder.textContent = targets.length ? "Select a publication target" : "No publication target is configured";
  select.append(placeholder);
  for (const item of targets) {
    const option = document.createElement("option");
    option.value = item.hardware_id;
    option.textContent = `${item.hardware_id} → ${item.repository_slug}@${item.base}`;
    select.append(option);
  }
  if (payload.suggestion) {
    select.value = payload.suggestion;
    note.hidden = false;
    note.textContent = `Matched from the folder name: ${payload.suggestion}`;
  } else {
    note.hidden = true;
    note.textContent = "";
  }
  select.onchange = changeSubmission;
  if (summary) summary.oninput = changeSubmission;
  const purpose = $("purpose-select");
  if (purpose) purpose.onchange = changeSubmission;
  updateStartEnabled();
}

function changeSubmission() {
  state.submissionKey = null;
  state.blockedPick = false;
  updateStartEnabled();
}

function requestKey() {
  return typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function responseRunId(payload, expected = null) {
  const runId = payload && payload.dag_run_id;
  if (typeof runId !== "string" || !/^[A-Za-z0-9_.:-]{1,250}$/.test(runId)) {
    const error = new Error("The server did not confirm a valid run identity; check the run list or retry the same submission.");
    error.unconfirmed = true;
    throw error;
  }
  if (expected !== null && runId !== expected) {
    const error = new Error("The server returned a different request identity; retry the same submission.");
    error.unconfirmed = true;
    throw error;
  }
  return runId;
}

function displayState(rawState, automatic) {
  return rawState === "unresolved" && automatic?.state === "waiting" ? "waiting" : rawState;
}

function hasPreviewEvidence(run) {
  return run.automatic?.state === "passed" || (run.verification_reports || [])
    .some((report) => report.path === "verification-certificate.json");
}

function automaticStateKind(state) {
  return state === "passed" ? "ok" : state === "failed" ? "bad" : "pending";
}

function updateStartEnabled() {
  const target = $("target-select");
  const summary = $("change-summary");
  const purpose = $("purpose-select");
  $("start-button").textContent = state.blockedPick ? "Retry same submission" : "Start delivery";
  $("start-button").disabled = !(
    state.folderPick &&
    !state.uploading &&
    state.assemblyChoice &&
    target &&
    target.value &&
    purpose &&
    purpose.value &&
    summary &&
    summary.value.trim()
  );
}

function submitRun(pick, { onProgress } = {}) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const item of pick.records) form.append("files", item.file, item.path);
    if (state.assemblyChoice) form.append("main_assembly", state.assemblyChoice);
    const targetSelect = $("target-select");
    if (!targetSelect || !targetSelect.value) {
      reject(new Error("Publication target is required"));
      return;
    }
    form.append("target_hardware", targetSelect.value);
    const purposeSelect = $("purpose-select");
    if (!purposeSelect || !purposeSelect.value) {
      reject(new Error("Purpose is required"));
      return;
    }
    form.append("purpose", purposeSelect.value);
    const summaryInput = $("change-summary");
    if (!summaryInput || !summaryInput.value.trim()) {
      reject(new Error("Change summary is required"));
      return;
    }
    form.append("change_summary", summaryInput.value.trim());
    const request = new XMLHttpRequest();
    request.open("POST", "/api/runs");
    request.withCredentials = true;
    request.setRequestHeader("Accept", "application/json");
    if (!state.submissionKey) state.submissionKey = requestKey();
    const submissionKey = state.submissionKey;
    request.setRequestHeader("Idempotency-Key", submissionKey);
    const csrf = state.csrf;
    if (csrf) request.setRequestHeader("X-CSRF-Token", csrf);
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    });
    request.addEventListener("load", () => {
      let payload = {};
      try {
        payload = JSON.parse(request.responseText || "{}");
        if (payload === null || typeof payload !== "object" || Array.isArray(payload)) payload = {};
      } catch {
        payload = {};
      }
      if (request.status >= 200 && request.status < 300) {
        try {
          responseRunId(payload, `portal-${submissionKey}`);
          resolve(payload);
        } catch (error) {
          error.dagRunId = `portal-${submissionKey}`;
          reject(error);
        }
        return;
      }
      const error = new Error(payload.error || `Upload failed (HTTP ${request.status})`);
      error.status = request.status;
      if (payload && payload.dag_run_id) error.dagRunId = String(payload.dag_run_id);
      if (request.status >= 500) {
        error.unconfirmed = true;
        error.dagRunId ||= `portal-${submissionKey}`;
      }
      reject(error);
      handleUnauthorized(error, csrf);
    });
    request.addEventListener("error", () => {
      const error = new Error("The network was interrupted before an upload response arrived. If the submission succeeded, confirm it in the run list; the system will not retry automatically.");
      error.network = true;
      error.unconfirmed = true;
      error.dagRunId = `portal-${submissionKey}`;
      reject(error);
    });
    request.addEventListener("abort", () => {
      const error = new Error("Upload cancelled.");
      error.aborted = true;
      error.unconfirmed = true;
      error.dagRunId = `portal-${submissionKey}`;
      reject(error);
    });
    state.uploadAbort = { abort: () => request.abort() };
    request.send(form);
  });
}

function resetPick() {
  state.folderPick = null;
  state.submissionKey = null;
  state.blockedPick = false;
  $("folder-input").value = "";
  $("folder-summary").hidden = true;
  $("upload-status").hidden = true;
  resetAssemblyRow();
  const targetSelect = $("target-select");
  if (targetSelect) targetSelect.value = "";
  $("start-button").disabled = true;
}

function handleUnauthorized(error, csrf) {
  if (error.status !== 401 || state.csrf !== csrf) return;
  clearSession();
  setError($("login-error"), error.message);
}

async function api(path, { method = "GET", body, signal, idempotencyKey } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (state.csrf && method !== "GET") headers["X-CSRF-Token"] = state.csrf;
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const csrf = state.csrf;
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    signal,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let payload = {};
  let validJson = false;
  if (text) {
    try {
      payload = JSON.parse(text);
      validJson = payload !== null && typeof payload === "object" && !Array.isArray(payload);
    } catch {
      payload = {};
    }
  }
  if (!validJson) payload = {};
  if (!response.ok) {
    const error = new Error(payload.error || `Request failed (HTTP ${response.status})`);
    error.status = response.status;
    error.payload = payload;
    handleUnauthorized(error, csrf);
    throw error;
  }
  if (!validJson) {
    const error = new Error("The server returned an invalid JSON response; the request outcome is unconfirmed.");
    error.unconfirmed = method !== "GET";
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
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("en-US", { hour12: false });
}

function showSession(session) {
  state.user = session.user;
  state.csrf = session.csrf_token;
  applyUploadLimits(session.upload_limits);
  $("login-card").hidden = true;
  $("workspace").hidden = false;
  $("account").hidden = false;
  $("account-user").textContent = session.user;
  $("account-user").title = session.user;
  if (state.targetsRetryPick) void loadTargets(state.targetsRetryPick);
}

function clearPreview() {
  if (state.previewRequest) state.previewRequest.abort();
  state.previewRequest = null;
  state.previewSubject = null;
  state.previewKey = null;
  state.previewFiles = null;
  state.previewData = null;
  state.previewCollapsed = false;
  state.previewCollapseFor = null;
  state.controls = null;
  if (state.viewer) disposeRobot(state.viewer);
  state.mjcfView = null;
  state.previewFormat = "urdf";
  $("preview-format").value = "urdf";
  $("mjcf-tools").hidden = true;
  $("mjcf-status").hidden = true;
  $("focus-joints").disabled = true;
  $("viewer-card").hidden = true;
  $("viewer-placeholder").hidden = false;
  $("preview-meta").textContent = "";
  $("preview-info").textContent = "";
  $("preview-diagnostic").hidden = true;
  $("preview-diagnostic").replaceChildren();
  delete $("preview-diagnostic").dataset.renderKey;
  $("viewer-note").hidden = true;
  $("joint-controls").textContent = "";
  $("preview-panels").querySelectorAll(".panel-table").forEach((node) => {
    node.textContent = "";
  });
  $("preview-collapse").hidden = true;
  setError($("preview-note"), "");
  updatePreviewLayout();
}

function stopRunRead() {
  if (state.timer) window.clearTimeout(state.timer);
  state.timer = null;
  if (state.pollRequest) state.pollRequest.abort();
  state.pollRequest = null;
  stopActivityTimer();
  clearPreview();
}

function stopListRead() {
  if (state.listRequest) state.listRequest.abort();
  state.listRequest = null;
  state.runsLoadingMore = false;
}

function clearSession() {
  stopRunRead();
  stopListRead();
  if (state.uploadAbort) state.uploadAbort.abort();
  state.csrf = null;
  state.user = null;
  state.dagRunId = null;
  state.lastRun = null;
  state.detailsInitializedFor = null;
  state.rerunNotice = "";
  state.rerunError = "";
  state.runsList = [];
  state.runsNextOffset = null;
  state.runsLoadingMore = false;
  state.runActionsFor = null;
  $("stage-rerun").hidden = true;
  $("workspace").hidden = true;
  $("account").hidden = true;
  $("login-card").hidden = false;
  $("detail-card").hidden = true;
  $("viewer-card").hidden = true;
  $("retry-panel").hidden = true;
  setError($("retry-error"), "");
}

function runDisplayTitle(run) {
  const custom = String(run.title || "").trim();
  return custom || assemblyLabel(run.main_assembly) || "Engineering delivery";
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

// Row timestamp stays short (MM-DD HH:mm in the current year); the full stamp stays in the tooltip.
function formatRunTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const stamp = `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  return date.getFullYear() === new Date().getFullYear() ? stamp : `${date.getFullYear()}-${stamp}`;
}

function deleteBlockedReason(run) {
  if (run.state === "success" || run.state === "failed" || run.state === "cancelled") return "";
  if (run.state === "unresolved") return "External effects require reconciliation before hiding this delivery.";
  if (run.state === "queued" || run.state === "running") return "This delivery cannot be hidden before it ends.";
  return "The delivery state is unknown, so it cannot be hidden.";
}

function runsForView(runs, view) {
  return runs.filter((run) => (view === "deleted" ? run.deleted === true : run.deleted !== true));
}

function setRunView(view) {
  state.runView = view === "deleted" ? "deleted" : "active";
  const active = $("runs-view-active");
  const deleted = $("runs-view-deleted");
  active.classList.toggle("active", state.runView === "active");
  deleted.classList.toggle("active", state.runView === "deleted");
  active.setAttribute("aria-pressed", String(state.runView === "active"));
  deleted.setAttribute("aria-pressed", String(state.runView === "deleted"));
  renderRunsList();
}

function ghostButton(label) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "ghost";
  button.textContent = label;
  return button;
}

async function refreshRuns() {
  stopListRead();
  const request = new AbortController();
  state.listRequest = request;
  const ownsRead = () => state.listRequest === request && !request.signal.aborted;
  const list = $("runs");
  try {
    const payload = await api("/api/runs?include_deleted=1&limit=30", { signal: request.signal });
    if (!ownsRead()) return;
    state.runsList = Array.isArray(payload.runs) ? payload.runs : [];
    state.runsNextOffset = typeof payload.next_offset === "number" ? payload.next_offset : null;
    setError($("runs-error"), "");
    renderRunsList();
  } catch (error) {
    if (!ownsRead()) return;
    state.runsList = [];
    state.runsNextOffset = null;
    list.textContent = "";
    const item = document.createElement("li");
    item.className = "muted";
    item.textContent = `Run list unavailable: ${error.message}`;
    list.append(item);
  } finally {
    if (ownsRead()) state.listRequest = null;
  }
}

// next_offset paging: a view is never declared empty from the first page alone.
async function loadMoreRuns() {
  if (state.listRequest || state.runsLoadingMore || state.runsNextOffset === null) return;
  const baseline = state.runsList;
  const offset = state.runsNextOffset;
  const request = new AbortController();
  state.listRequest = request;
  const ownsRead = () => state.listRequest === request && !request.signal.aborted;
  state.runsLoadingMore = true;
  renderRunsList();
  try {
    const payload = await api(`/api/runs?include_deleted=1&limit=30&offset=${offset}`, { signal: request.signal });
    if (!ownsRead() || state.runsList !== baseline || state.runsNextOffset !== offset) return;
    state.runsList = baseline.concat(Array.isArray(payload.runs) ? payload.runs : []);
    state.runsNextOffset = typeof payload.next_offset === "number" ? payload.next_offset : null;
    setError($("runs-error"), "");
  } catch (error) {
    if (!ownsRead()) return;
    setError($("runs-error"), error.message || "Failed to load more runs; retry.");
  } finally {
    if (ownsRead()) {
      state.listRequest = null;
      state.runsLoadingMore = false;
      renderRunsList();
    }
  }
}

function renderRunsList() {
  const list = $("runs");
  list.textContent = "";
  const rows = runsForView(state.runsList || [], state.runView);
  if (!rows.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = state.runsNextOffset !== null
      ? "No deliveries are visible in the loaded records; load more."
      : state.runView === "deleted" ? "No hidden deliveries" : "No deliveries";
    list.append(empty);
  }
  for (const run of rows) list.append(buildRunItem(run));
  if (state.runsNextOffset !== null) {
    const footer = document.createElement("li");
    footer.className = "runs-footer";
    const more = ghostButton(state.runsLoadingMore ? "Loading..." : "Load more");
    more.disabled = state.runsLoadingMore;
    more.addEventListener("click", () => {
      void loadMoreRuns();
    });
    footer.append(more);
    list.append(footer);
  } else if (rows.length) {
    const footer = document.createElement("li");
    footer.className = "muted small runs-footer";
    footer.textContent = "All runs shown";
    list.append(footer);
  }
}

function buildRunItem(run) {
  const item = document.createElement("li");
  if (run.dag_run_id === state.dagRunId) item.className = "selected";
  const box = document.createElement("div");
  box.className = "run-item";
  const open = document.createElement("button");
  open.type = "button";
  open.className = "run-open";
  const heading = document.createElement("span");
  heading.className = "run-heading";
  heading.textContent = runDisplayTitle(run);
  if (run.parent_dag_run_id) {
    const chip = document.createElement("span");
    chip.className = "run-chip";
    chip.textContent = `Rerun from ${run.resume_from_name || "upstream stage"}`;
    chip.title = `Rerun source: ${run.parent_dag_run_id}`;
    heading.append(chip);
  }
  const meta = document.createElement("span");
  meta.className = "run-meta-line";
  const stateText = RUN_STATES[displayState(run.state, run.automatic)] || run.state || "State unconfirmed";
  meta.textContent = `${[formatRunTime(run.started_at), stateText].filter(Boolean).join(" · ")}\nInitiator: ${run.user || "not recorded"}`;
  open.append(heading, meta);
  open.title = [
    runDisplayTitle(run),
    assemblyLabel(run.main_assembly) || "Engineering delivery",
    formatTime(run.started_at),
    run.dag_run_id,
  ].join("\n");
  open.addEventListener("click", () => selectRun(run.dag_run_id));
  box.append(open);
  if (run.deleted === true) {
    const tombstone = document.createElement("span");
    tombstone.className = "run-tombstone";
    tombstone.textContent =
      `Hidden · ${formatRunTime(run.deleted_at) || "time not recorded"} · Hidden by: ${run.deleted_by || "not recorded"}`;
    box.append(tombstone);
  }
  if (run.can_manage === true) box.append(buildRunMenu(run));
  item.append(box);
  return item;
}

// One compact per-run menu keeps the sidebar short: it is the only place row actions live,
// and the selected run shows the same menu in the detail header.
function buildRunMenu(run) {
  const wrap = document.createElement("span");
  wrap.className = "run-menu";
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "ghost run-menu-trigger";
  trigger.textContent = "⋯";
  trigger.setAttribute("aria-label", `Actions: ${runDisplayTitle(run)}`);
  trigger.setAttribute("aria-expanded", "false");
  const pop = document.createElement("div");
  pop.className = "run-menu-pop";
  pop.hidden = true;
  const close = (refocus) => {
    pop.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (refocus) trigger.focus();
  };
  const showMenu = () => {
    pop.textContent = "";
    const rename = ghostButton("Rename");
    rename.addEventListener("click", showRename);
    pop.append(rename);
    if (run.deleted === true) {
      const restore = ghostButton("Restore");
      restore.addEventListener("click", () => {
        close(false);
        void mutateRun(run, "POST");
      });
      pop.append(restore);
    } else {
      const remove = ghostButton("Hide from list");
      const blocked = deleteBlockedReason(run);
      if (blocked) {
        remove.disabled = true;
        remove.title = blocked;
      } else {
        remove.addEventListener("click", showDeleteConfirm);
      }
      pop.append(remove);
    }
    pop.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    const first = pop.querySelector("button:not(:disabled)");
    if (first) first.focus();
  };
  const showRename = () => {
    pop.textContent = "";
    const form = document.createElement("form");
    form.className = "run-rename-form";
    const input = document.createElement("input");
    input.type = "text";
    input.maxLength = 120;
    input.value = String(run.title || "");
    input.placeholder = assemblyLabel(run.main_assembly) || "Engineering delivery";
    input.title = "Leave empty and save to restore the main assembly name; the title affects list display only.";
    input.setAttribute("aria-label", `Rename ${runDisplayTitle(run)}`);
    const buttons = document.createElement("div");
    buttons.className = "run-menu-buttons";
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "ghost";
    save.textContent = "Save";
    const cancel = ghostButton("Cancel");
    cancel.addEventListener("click", showMenu);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      close(false);
      void mutateRun(run, "PATCH", input.value.trim() || null);
    });
    buttons.append(save, cancel);
    form.append(input, buttons);
    pop.append(form);
    queueMicrotask(() => {
      input.focus();
      input.select();
    });
  };
  const showDeleteConfirm = () => {
    pop.textContent = "";
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent = "Remove this run from the list. It can be restored from Hidden; models, evidence, and PRs remain.";
    const buttons = document.createElement("div");
    buttons.className = "run-menu-buttons";
    const confirm = ghostButton("Hide delivery");
    const cancel = ghostButton("Cancel");
    cancel.addEventListener("click", showMenu);
    confirm.addEventListener("click", () => {
      close(false);
      void mutateRun(run, "DELETE");
    });
    buttons.append(confirm, cancel);
    pop.append(note, buttons);
    queueMicrotask(() => confirm.focus());
  };
  trigger.addEventListener("click", () => {
    if (pop.hidden) showMenu();
    else close(true);
  });
  wrap.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !pop.hidden) {
      event.preventDefault();
      close(true);
    }
  });
  wrap.append(trigger, pop);
  return wrap;
}

// One mutation path for rename/delete/restore: the response overlay lands in place, so the
// loaded pages, the selection and any open menu survive, and the deleter stays server-recorded.
async function mutateRun(run, method, title) {
  const id = encodeURIComponent(run.dag_run_id);
  try {
    if (method === "PATCH") {
      const payload = await api(`/api/runs/${id}`, { method: "PATCH", body: { title } });
      overlayRun(state.runsList, run, {
        title: payload.title === null || typeof payload.title === "string" ? payload.title : title,
      });
    } else if (method === "DELETE") {
      const payload = await api(`/api/runs/${id}`, { method: "DELETE" });
      overlayRun(state.runsList, run, {
        deleted: payload.deleted !== false,
        deleted_at: payload.deleted_at || null,
        deleted_by: typeof payload.deleted_by === "string" ? payload.deleted_by : null,
      });
    } else {
      await api(`/api/runs/${id}/restore`, { method: "POST", body: {} });
      overlayRun(state.runsList, run, { deleted: false, deleted_at: null, deleted_by: null });
    }
    setError($("runs-error"), "");
    renderRunsList();
    if (state.dagRunId === run.dag_run_id) await selectRun(run.dag_run_id);
  } catch (error) {
    setError($("runs-error"), error.message || "Operation did not complete; retry.");
  }
}

function stageDotState(state) {
  if (state === "completed") return "ok";
  if (state === "failed") return "bad";
  if (["waiting", "running", "unresolved"].includes(state)) return "run";
  // Upstream-blocked and not-run stages stay neutral: nothing failed here.
  return "idle";
}

function runDotState(state) {
  if (state === "success") return "ok";
  if (state === "failed") return "bad";
  if (["waiting", "running", "unresolved"].includes(state)) return "run";
  return "idle";
}

function showUploadView(focus = false) {
  stopRunRead();
  state.dagRunId = null;
  state.lastRun = null;
  $("upload-view").hidden = false;
  $("detail-card").hidden = true;
  if (focus) $("upload-heading").focus();
}

function showRunView() {
  $("upload-view").hidden = true;
  $("detail-card").hidden = false;
  $("run-heading").focus();
  if (state.viewer) {
    requestAnimationFrame(() => {
      state.viewer.resize?.();
      state.viewer.frame();
    });
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
  return text.length > limit ? `${text.slice(0, limit)}\n... (truncated because the content is too long)` : text;
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
  if (["waiting", "running", "unresolved"].includes(state)) return "pending";
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
  for (const state of STAGE_SELECTION_PRIORITY) {
    const index = stages.findIndex((stage) => stage.state === state);
    if (index !== -1) return index;
  }
  return 0;
}

function countChecks(rows) {
  let passed = 0;
  let executed = 0;
  for (const row of rows) {
    if (row.state === "passed") passed += 1;
    const ran = row.executed === true || (row.executed === undefined && (row.state === "passed" || row.state === "failed"));
    if (ran) executed += 1;
  }
  return `Passed ${passed}/${rows.length} · Executed ${executed}/${rows.length}`;
}

function countsText(counts, rows) {
  // Canonical report counts when present; row-derived numbers only as a safety net.
  if (counts && Number.isFinite(counts.total)) {
    const passed = Number.isFinite(counts.passed) ? counts.passed : 0;
    const executed = Number.isFinite(counts.executed) ? counts.executed : 0;
    return `Passed ${passed}/${counts.total} · Executed ${executed}/${counts.total}`;
  }
  return countChecks(rows);
}

function reportRow(row, boundary) {
  const summary = row.summary && typeof row.summary === "object" ? row.summary : {};
  return {
    id: String(row.id || ""),
    boundary,
    label: row.label || String(row.id || ""),
    state: row.state || "not_run",
    stateLabel: row.state_label || CHECK_STATES[row.state] || "Not run",
    executed: typeof row.executed === "boolean" ? row.executed : undefined,
    scope: summary.scope || "",
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
      name: stage.name || stage.id,
      state: stage.state || "not_run",
      stateLabel: stage.state_label || "Not run",
      at: stage.at,
      reuse: stage.reuse || null,
      counts,
      passed: sum("passed"),
      executed: sum("executed"),
      total: sum("total"),
      boundary: (Array.isArray(stage.boundary) ? stage.boundary : []).map((row) =>
        reportRow(row, row.boundary)),
      independent: (Array.isArray(stage.independent) ? stage.independent : []).map((row) => reportRow(row, "independent")),
      files: Array.isArray(stage.files) ? stage.files : [],
      inputInventoryCount: Number.isFinite(stage.input_inventory_count) ? stage.input_inventory_count : null,
      warnings: Array.isArray(stage.warnings) ? stage.warnings : [],
      unsupported: (Array.isArray(stage.unsupported) ? stage.unsupported : [])
        .map((item) => (item && typeof item === "object" ? item.label || item.id : item))
        .filter(Boolean),
      manualNote: typeof stage.manual_scope_note === "string" ? stage.manual_scope_note : "",
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
  name.textContent = row.label || row.id || "Check";
  const chip = document.createElement("span");
  chip.className = `chip ${stateKind(row.state)}`.trim();
  chip.textContent = row.stateLabel;
  head.append(name, chip);
  item.append(head);
  if (row.scope) {
    const scope = document.createElement("p");
    scope.className = "check-scope";
    scope.textContent = `Scope: ${row.scope}`;
    item.append(scope);
  }
  const expected = inlineValue(row.expected);
  const actual = inlineValue(row.actual);
  if (expected || actual) {
    const kv = document.createElement("p");
    kv.className = "check-kv";
    if (expected) kv.append(`Expected: ${expected}`);
    if (expected && actual) kv.append("; ");
    if (actual) kv.append(`Actual: ${actual}`);
    item.append(kv);
  }
  appendEvidence(item, "Raw evidence", row.details);
  parent.append(item);
  return item;
}

//: Recorded observations render as bounded warning rows: exact recorded values
//: (including authored zeros), the qualification/publication blocker kind, and no
//: pass bookkeeping or per-item human confirmation.
const WARNING_ROW_LIMIT = 8;

function appendWarningSourceRows(parent, warning) {
  const examples = Array.isArray(warning.objects) ? warning.objects.slice(0, WARNING_ROW_LIMIT) : [];
  const details = expandable(parent, `Located source examples (${examples.length}/${warning.source_rows_total})`);
  const excerpt = document.createElement("pre");
  excerpt.className = "warning-source";
  excerpt.textContent = JSON.stringify(examples, null, 2);
  details.append(excerpt);
  const note = document.createElement("p");
  note.className = "muted small";
  note.textContent = `${warning.records_total} recorded warnings; ${warning.records_omitted} records and ` +
    `${warning.source_rows_omitted} source rows are not expanded. Complex values are explicit excerpts. ` +
    `Complete source record: ${JSON.stringify(warning.record_location)}.`;
  details.append(note);
}


function stageWarningRow(row) {
  const item = document.createElement("div");
  item.className = "warning-row";
  const head = document.createElement("div");
  head.className = "warning-head";
  const label = document.createElement("span");
  label.className = "warning-label";
  label.textContent = row.label || row.code || "Warning";
  const chip = document.createElement("span");
  chip.className = "chip warning";
  chip.textContent = row.state_label || "Warning";
  head.append(label, chip);
  item.append(head);
  const meta = [row.object, row.code].filter(Boolean).join(" · ");
  if (meta) {
    const line = document.createElement("p");
    line.className = "warning-meta muted small";
    line.textContent = meta;
    item.append(line);
  }
  if (row.message) {
    const message = document.createElement("p");
    message.className = "warning-message";
    message.textContent = row.message;
    item.append(message);
  }
  const blockers = [];
  if (row.qualification_blocker) blockers.push(`Qualification blocker: ${row.qualification_blocker}`);
  if (row.publication_blocker) blockers.push(`Publication blocker: ${row.publication_blocker}`);
  if (blockers.length) {
    const line = document.createElement("p");
    line.className = "warning-blocker";
    line.textContent = blockers.join("; ");
    item.append(line);
  }
  appendWarningSourceRows(item, row);
  return item;
}

function renderStageWarnings(root, stage, note) {
  const rows = Array.isArray(stage.warnings) ? stage.warnings : [];
  if (!rows.length) return;
  const group = document.createElement("div");
  group.className = "check-group warning-group";
  const title = document.createElement("h4");
  title.textContent = `Warnings (${stage.counts.warnings} records in ${rows.length} categories)`;
  group.append(title);
  const hint = document.createElement("p");
  hint.className = "stage-note muted small";
  hint.textContent = note || "Recorded observations do not block capture or generation and are neither passes nor acceptance/publication evidence.";
  group.append(hint);
  for (const row of rows) {
    const category = expandable(group, `${row.code}: ${String(row.message || "").slice(0, 96)} (${row.records_total} records)`);
    category.append(stageWarningRow(row));
  }
  const certificate = (state.lastRun?.verification_reports || [])
    .find((record) => record.path === "verification-certificate.json");
  if (stage.id === "verify" && certificate?.url) {
    const link = document.createElement("a");
    link.href = certificate.url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "Open complete verification certificate";
    group.append(link);
  }
  root.append(group);
}

function buildFailureCard(failure, report) {
  const card = document.createElement("div");
  card.className = "failure-summary";
  const title = document.createElement("strong");
  title.textContent = failure.title || "Run failed";
  card.append(title);
  const stage =
    report && Array.isArray(report.stages) ? report.stages.find((entry) => entry && entry.id === failure.stage) : null;
  const failedChecks = stage
    ? [...(Array.isArray(stage.boundary) ? stage.boundary : []), ...(Array.isArray(stage.independent) ? stage.independent : [])]
        .filter((row) => row && row.state === "failed")
    : [];
  const warningCount = stage && Number.isFinite(stage.counts && stage.counts.warnings) ? stage.counts.warnings : 0;
  if (failure.meaning && !(failure.kind === "unknown" && failedChecks.length)) {
    const meaning = document.createElement("p");
    meaning.textContent = failure.meaning;
    card.append(meaning);
  }
  if ((!failure.meaning || failure.kind === "unknown") && failedChecks.length) {
    const reason = document.createElement("p");
    reason.textContent = failedChecks[0].summary?.actual || failedChecks[0].label || "A required check failed.";
    card.append(reason);
  }
  const details = expandable(card, "Failure details");
  for (const [label, value] of [
    ["Error code", failure.error_code],
    ["Phase", failure.phase],
    ["Cause", failure.cause],
    ["Evidence path", failure.evidence_path],
  ]) {
    if (value === undefined || value === null || value === "") continue;
    const line = document.createElement("p");
    line.textContent = `${label}: ${value}`;
    details.append(line);
  }
  if (failure.values && typeof failure.values === "object") {
    const line = document.createElement("p");
    line.className = "muted small";
    line.textContent = `Recorded values: ${inlineValue(failure.values)}`;
    details.append(line);
    appendEvidence(details, "Recorded failure values", failure.values);
  }
  if (failedChecks.length) {
    const heading = document.createElement("p");
    heading.textContent = `Failed automatic checks (${failedChecks.length}):`;
    details.append(heading);
    for (const row of failedChecks.slice(0, 5)) {
      const line = document.createElement("p");
      line.className = "muted small";
      const message = row.summary && row.summary.actual ? row.summary.actual : "Failed";
      line.textContent = `${row.label || row.id || "Check"}: ${message}`;
      details.append(line);
    }
    if (failedChecks.length > 5) {
      const more = document.createElement("p");
      more.className = "muted small";
      more.textContent = `${failedChecks.length - 5} more checks failed; see the stage checks below for per-item evidence.`;
      details.append(more);
    }
    if (failedChecks.some((row) => String(row.id || "").startsWith("source."))) {
      const hint = document.createElement("p");
      hint.className = "muted small";
      hint.textContent =
        "A source-evidence check failed. Use the failed checks and raw evidence to distinguish missing input, inconsistent provenance, or a discovery defect.";
      details.append(hint);
    } else {
      const detail = document.createElement("p");
      detail.className = "muted small";
      detail.textContent = "See Raw evidence in the stage checks below for per-item evidence.";
      details.append(detail);
    }
  }
  if (warningCount > 0) {
    const warnings = document.createElement("p");
    warnings.className = "muted small";
    warnings.textContent = `This stage has ${warningCount} recorded warnings. Warnings are neither passes nor acceptance/publication evidence.`;
    details.append(warnings);
  }
  if (failure.kind === "definition" || failure.kind === "runtime") {
    const kind = document.createElement("p");
    kind.className = "muted small";
    kind.textContent = failure.kind === "definition" ? "Class: engineering-definition check" : "Class: runtime issue";
    details.append(kind);
  }
  if (Array.isArray(failure.finding_counts) && failure.finding_counts.length) {
    const groups = document.createElement("p");
    groups.className = "muted small";
    const shown = failure.finding_counts.slice(0, 8);
    groups.textContent =
      "Finding groups: " +
      shown.map((group) => `${group.label || group.code} x ${group.count}`).join(", ") +
      (failure.finding_counts.length > shown.length ? "..." : "") +
      (failure.finding_total ? ` (${failure.finding_total} total; expand the issue list below for details)` : "");
    details.append(groups);
  }
  if (Array.isArray(failure.finding_counts) && failure.finding_counts.length) {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent = "Counts describe reported findings and affected instances, not CAD defects. The issue list below is collapsed by default.";
    details.append(note);
    if (failure.finding_counts.some((group) => group && group.code === "discovery.link_name_missing")) {
      const hint = document.createElement("p");
      hint.className = "muted small";
      hint.textContent =
        "discovery.link_name_missing means rigid-body ownership and naming are not proven; it does not prove that CAD has no coordinate system. " +
        "Complete the delivery definition and coordinate-system ownership in the native project. " +
        "Do not add a separate coordinate system for every supplier-internal leaf.";
      details.append(hint);
      const samples = failure.finding_samples && Array.isArray(failure.finding_samples["discovery.link_name_missing"])
        ? failure.finding_samples["discovery.link_name_missing"]
        : [];
      const objects = samples.map((row) => row && row.object).filter(Boolean).slice(0, 2);
      if (objects.length) {
        const sample = document.createElement("p");
        sample.className = "muted small";
        sample.textContent = `Example objects: ${objects.join(", ")} (see the complete issue list below)`;
        details.append(sample);
      }
    }
  }
  if (failure.stage_name) {
    const stage = document.createElement("p");
    stage.className = "muted small";
    stage.textContent = `Stage: ${failure.stage_name}`;
    details.append(stage);
  }
  const affected = failure.object
    ? String(failure.object)
    : Array.isArray(failure.unresolved_dependencies)
      ? failure.unresolved_dependencies
          .map((item) => (item && (item.name || item.path)) || "")
          .filter(Boolean)
          .join(", ")
      : "";
  if (affected) {
    const object = document.createElement("p");
    object.textContent = `Objects: ${affected}`;
    details.append(object);
  }
  const raw = {};
  for (const key of ["raw_type", "raw_error", "raw_detail"]) {
    if (failure[key] !== undefined && failure[key] !== null) raw[key] = failure[key];
  }
  if (Object.keys(raw).length) appendEvidence(details, "Raw error (diagnostic)", raw);
  return card;
}

function beginRender(node, facts) {
  // A DOM replacement key preserves interaction; it never supplies business facts.
  const key = JSON.stringify(facts);
  if (node.dataset.renderKey === key) return false;
  node.dataset.renderKey = key;
  node.textContent = "";
  return true;
}

function renderStageDetail() {
  const root = $("stage-detail");
  const stage = state.stages[state.selectedStage];
  const run = state.lastRun && state.lastRun.dag_run_id === state.dagRunId ? state.lastRun : null;
  const warningNote = run?.report?.warnings?.note || "";
  const quick = stage ? stageSummaryRows(stage.id, {
    files: state.previewFiles, stage,
    certificate: state.previewData ? state.previewData.certificate : null,
    pr: run ? run.pr : null, subjectSha256: run?.report?.measured?.subject_sha256,
  }) : { rows: [] };
  $("stage-selection-label").textContent = stage?.name || "";
  if (!beginRender(root, [state.dagRunId, stage, quick, state.previewFiles, warningNote, run?.verification_reports,
    stage ? displayState(stage.state, run?.automatic) : null])) return;
  if (!stage) {
    root.textContent = "Stage details are not available yet.";
    return;
  }
  const shownState = displayState(stage.state, state.lastRun?.automatic);

  const summary = document.createElement("div");
  summary.className = "stage-summary";
  const head = document.createElement("div");
  head.className = "stage-summary-head";
  const title = document.createElement("strong");
  title.textContent = stage.name;
  const chip = document.createElement("span");
  chip.className = `badge ${stateKind(shownState)}`.trim();
  chip.textContent = shownState !== stage.state ? RUN_STATES[shownState] : stage.stateLabel;
  head.append(title, chip);
  summary.append(head);
  const counts = document.createElement("p");
  counts.className = "stage-counts muted small";
  const pieces = [];
  if (stage.boundary.length) pieces.push(`Recorded checks: ${countsText(stage.counts.boundary, stage.boundary)}`);
  if (stage.independent.length) pieces.push(`Independent checks: ${countsText(stage.counts.independent, stage.independent)}`);
  if (Number.isFinite(stage.counts.warnings) && stage.counts.warnings > 0) {
    pieces.push(`Warnings: ${stage.counts.warnings}`);
  }
  counts.textContent = pieces.join(" • ") || "No check records";
  summary.append(counts);
  if (stage.at) {
    const at = document.createElement("p");
    at.className = "muted small";
    at.textContent = `Recorded: ${formatTime(stage.at)}`;
    summary.append(at);
  }
  if (stage.reuse && stage.reuse.reused === true) {
    const reused = document.createElement("p");
    reused.className = "muted small";
    reused.textContent = "Verified results were reused from the recorded source.";
    summary.append(reused);
    appendEvidence(summary, "Reuse source", stage.reuse);
  }
  root.append(summary);

  if (quick.rows.length) {
    const block = document.createElement("div");
    block.className = "stage-quick-summary";
    const title = document.createElement("h4");
    title.textContent = "Summary";
    block.append(title);
    for (const row of quick.rows) {
      const line = document.createElement("p");
      line.className = "muted small";
      const label = document.createElement("strong");
      label.textContent = `${row.label}: `;
      const value = document.createElement("code");
      value.textContent = row.value;
      line.append(label, value);
      block.append(line);
    }
    if (quick.necessity) {
      const note = document.createElement("p");
      note.className = "muted small";
      note.textContent = `Why: ${quick.necessity}`;
      block.append(note);
    }
    root.append(block);
  }

  const groups = [
    ["Stage checks", stage.boundary.filter((row) => row.boundary === "stage")],
    ["Input checks", stage.boundary.filter((row) => row.boundary === "input")],
    ["Output checks", stage.boundary.filter((row) => row.boundary === "output")],
    ["Independent checks", stage.independent],
  ];
  for (const [heading, rows] of groups) {
    if (!rows.length) continue;
    const group = document.createElement("div");
    group.className = "check-group";
    const titleRow = document.createElement("h4");
    titleRow.textContent = `${heading} (${countChecks(rows)})`;
    group.append(titleRow);
    for (const row of rows) checkRow(group, row);
    root.append(group);
  }

  renderStageWarnings(root, stage, warningNote);

  const fileRows = (stage.files || []).map((file) => ({
    label: file.label || file.path || "File",
    boundary: file.boundary === "output" ? "Output" : file.boundary === "input" ? "Input" : "",
    path: file.path || "",
    availability: file.availability || "",
    count: Number.isFinite(file.files) ? file.files : null,
  }));
  if (fileRows.length) {
    const details = expandable(root, `Input and output records (${fileRows.length})`);
    const list = document.createElement("div");
    list.className = "stage-files";
    const authorized = state.previewFiles || {};
    for (const row of fileRows) {
      const line = document.createElement("p");
      line.className = "file-line";
      const title = [row.boundary, row.label, row.path].filter(Boolean).join(" · ") || "—";
      if (row.path && Object.prototype.hasOwnProperty.call(authorized, row.path)) {
        const link = document.createElement("a");
        link.href = `/api/runs/${encodeURIComponent(state.dagRunId)}/artifacts/${String(row.path)
          .split("/")
          .map(encodeURIComponent)
          .join("/")}`;
        link.target = "_blank";
        link.rel = "noopener";
        link.textContent = title;
        line.append(link);
      } else {
        line.textContent = title;
      }
      if (Number.isFinite(row.count) && row.count > 0) {
        const tag = document.createElement("code");
        tag.textContent = `${row.count} files`;
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
    row.append("Not covered automatically: ");
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
  workspace.classList.toggle("no-preview", !has);
}

function previewCollapseKey(runId) {
  return `preview-collapsed:${runId}`;
}

// The default behavior: with a verified delivery the interactive preview is expanded, and the
// collapse control is the only way back; entering the generate-URDF block re-expands it.
function applyPreviewExpansion() {
  const expansion = previewExpansion({
    preview: state.previewSubject ? { subject_sha256: state.previewSubject } : null,
    collapsed: state.previewCollapsed,
  });
  const card = $("viewer-card");
  if (card && !card.hidden) card.classList.toggle("preview-collapsed", !expansion.expanded);
  if (expansion.expanded && state.viewer) {
    // Re-measuring after a collapse/expand avoids a zero-sized canvas on the next frame.
    fitPreview();
  }
  const button = $("preview-collapse");
  button.hidden = !expansion.collapse_control;
  button.textContent = expansion.expanded ? "Collapse preview" : "Expand preview";
  button.setAttribute("aria-expanded", expansion.expanded ? "true" : "false");
}

function fitPreview() {
  state.viewer?.resize();
  if (state.mjcfView) state.mjcfView.fit();
  else state.viewer?.inspection?.fit();
}

function revealJoint(name, focus = false) {
  if (!state.controls) return;
  $("joint-panel").open = true;
  state.controls.reveal(name, { focus });
}

function togglePreviewCollapse() {
  const runId = state.dagRunId;
  state.previewCollapsed = !state.previewCollapsed;
  try {
    localStorage.setItem(previewCollapseKey(runId), state.previewCollapsed ? "1" : "0");
  } catch {
    // best-effort persistence
  }
  applyPreviewExpansion();
}

async function loadPreviewArtifactJson(artifactUrl, files, name, signal) {
  if (!files || !Object.prototype.hasOwnProperty.call(files, name)) return null;
  try {
    const response = await fetch(artifactUrl(name), { credentials: "same-origin", signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new Error(`Cannot read registered preview artifact ${name}: ${error.message}`);
  }
}

function fillPanelTable(host, headers, rows) {
  host.textContent = "";
  const table = document.createElement("table");
  table.className = "panel-grid";
  const head = document.createElement("tr");
  for (const title of headers) {
    const cell = document.createElement("th");
    cell.textContent = title;
    head.append(cell);
  }
  table.append(head);
  for (const row of rows) {
    const line = document.createElement("tr");
    for (const value of row) {
      const cell = document.createElement("td");
      cell.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
      line.append(cell);
    }
    table.append(line);
  }
  host.append(table);
}

async function renderPreviewPanels(preview, records, artifactUrl, signal) {
  const data = {};
  const artifactErrors = [];
  for (const [key, files, name] of [
    ["robot", preview.files, "model/robot.json"],
    ["tool", preview.files, "tool.json"],
    ["certificate", preview.report_files, "verification-certificate.json"],
  ]) {
    try {
      data[key] = await loadPreviewArtifactJson(artifactUrl, files, name, signal);
    } catch (error) {
      if (signal?.aborted) throw error;
      data[key] = null;
      artifactErrors.push(error.message);
    }
  }
  state.previewData = data;
  const identity = summaryIdentity({ runId: state.dagRunId, subjectSha256: preview.subject_sha256 });
  const info = $("preview-info");
  info.textContent = "";
  for (const row of infoPanelRows({
    runId: identity.run_id,
    subjectSha256: identity.subject_sha256,
    purpose: preview.purpose,
    tool: data.tool,
    certificateSchema: data.certificate ? data.certificate.schema_version : null,
    qualificationStates: data.certificate ? data.certificate.qualifications : null,
  })) {
    const key = document.createElement("dt");
    key.textContent = row.key;
    const value = document.createElement("dd");
    value.textContent = row.value || "not recorded";
    info.append(key, value);
  }
  for (const message of artifactErrors) {
    const key = document.createElement("dt");
    key.textContent = "Preview artifact";
    const value = document.createElement("dd");
    value.className = "error";
    value.textContent = message;
    info.append(key, value);
  }
  const limits = jointRows(records ? records.joints : []);
  const fmt = (value, digits) => (Number.isFinite(value) ? value.toFixed(digits) : "not provided");
  const modelJoints = new Map(((data.robot && data.robot.joints) || []).map((joint) => [joint.name, joint]));
  const absentDrive = "not provided (drive capability not established)";
  const sourceLabel = (source) => source || "not recorded";
  fillPanelTable(
    $("panel-limits"),
    ["Joint", "Type", "Lower", "Upper", "Effort", "Velocity", "Axis (xyz)", "Limit source"],
    limits.map((row) => {
      const source = modelJoints.get(row.name) && modelJoints.get(row.name).limit_source;
      return [
        row.name,
        row.type,
        row.lower === null ? null : row.lower.toFixed(4),
        row.upper === null ? null : row.upper.toFixed(4),
        row.effort === null || row.effort === undefined ? absentDrive : row.effort,
        row.velocity === null || row.velocity === undefined ? absentDrive : row.velocity,
        row.axis ? row.axis.map((value) => fmt(value, 3)).join(", ") : null,
        sourceLabel(source),
      ];
    }),
  );
  $("limits-title").textContent = `Limits and axis readings (${limits.length})`;
  const links = linkRows(records ? records.links : []);
  fillPanelTable(
    $("panel-links"),
    ["Link", "Mass kg", "COM m", "Inertia (kg*m^2)"],
    links.map((row) => [
      row.name,
      row.mass_kg === null ? "not provided" : row.mass_kg.toFixed(6),
      row.com_m ? row.com_m.map((value) => fmt(value, 4)).join(", ") : null,
      row.inertia_kg_m2
        ? Object.entries(row.inertia_kg_m2).map(([key, value]) => `${key} ${fmt(value, 6)}`).join(" · ")
        : null,
    ]),
  );
  $("links-title").textContent = `Link/body mass and inertia (${links.length})`;
  const frames = frameRows(data.robot ? data.robot.frames : []);
  const closure = closureRows(data.certificate ? data.certificate.checks : []);
  fillPanelTable(
    $("panel-frames"),
    ["Frame / interface", "Parent", "xyz (m)", "rpy (rad)"],
    frames.map((row) => [
      row.name,
      row.parent,
      row.xyz ? row.xyz.map((value) => fmt(value, 4)).join(", ") : null,
      row.rpy ? row.rpy.map((value) => fmt(value, 4)).join(", ") : null,
    ]),
  );
  fillPanelTable(
    $("panel-closure"),
    ["Check", "State"],
    closure.map((row) => [row.id, row.state]),
  );
  $("frames-title").textContent = `Frames, interface poses, and physical closure (${frames.length} / ${closure.length})`;
}

function stopActivityTimer() {
  if (state.activityTimer) window.clearInterval(state.activityTimer);
  state.activityTimer = null;
}

// Local 1 s ticker: only re-renders elapsed/freshness text; the network poll stays at 3 s and
// a successful poll is never treated as liveness.
function startActivityTimer() {
  if (state.activityTimer) return;
  state.activityTimer = window.setInterval(() => {
    const run = state.lastRun;
    if (!run || run.dag_run_id !== state.dagRunId) return;
    if (run.state === "success" || run.state === "failed" || run.state === "cancelled") return;
    renderActivity(run);
  }, 1000);
}

function buildActivityCard(view) {
  const card = document.createElement("section");
  card.className = `activity-card state-${view.state}`;
  card.setAttribute("aria-label", "Live activity");
  const head = document.createElement("div");
  head.className = "activity-head";
  const dot = document.createElement("span");
  dot.className = `dot ${view.state === "busy" || view.state === "queued" ? "run" : "idle"}`;
  const title = document.createElement("strong");
  title.textContent = view.final ? "Last activity" : view.state === "waiting" ? "Execution status" : "Live activity";
  const chip = document.createElement("span");
  chip.className = `chip ${view.state === "busy" ? "running" : view.state === "queued" ? "queued" : "blocked"}`;
  chip.textContent = view.stateText;
  head.append(dot, title, chip);
  if (view.stageText) {
    const stage = document.createElement("span");
    stage.className = "muted small";
    stage.textContent = `Stage: ${view.stageText}`;
    head.append(stage);
  }
  card.append(head);
  if (view.actionText) {
    const action = document.createElement("p");
    action.className = "activity-action";
    action.textContent = view.actionText;
    card.append(action);
  }
  if (view.objectText) {
    const object = document.createElement("p");
    object.className = "activity-object muted small";
    object.textContent = view.final ? `Related object: ${view.objectText}` : `Current object: ${view.objectText}`;
    card.append(object);
  }
  const metaParts = view.final
    ? [view.freshnessText].filter(Boolean)
    : [view.elapsedText, view.countsText, view.freshnessText].filter(Boolean);
  if (metaParts.length) {
    const meta = document.createElement("p");
    meta.className = view.stale ? "activity-meta muted small stale" : "activity-meta muted small";
    meta.textContent = metaParts.join(" • ");
    card.append(meta);
  }
  if (view.note) {
    const note = document.createElement("p");
    note.className = "activity-note muted small";
    note.textContent = view.note;
    card.append(note);
  }
  if (view.recent.length) {
    const list = document.createElement("ul");
    list.className = "activity-recent";
    for (const row of view.recent) {
      const item = document.createElement("li");
      const time = document.createElement("time");
      time.textContent = row.timeText || "—";
      const text = document.createElement("span");
      text.textContent = row.object ? `${row.text} · ${row.object}` : row.text;
      item.append(time, text);
      list.append(item);
    }
    card.append(list);
  }
  return card;
}

function renderActivity(run) {
  const host = $("activity-overview");
  host.textContent = "";
  if (!run || run.dag_run_id !== state.dagRunId) return;
  const view = activityView(run.activity, {
    runState: displayState(run.state, run.automatic),
    waitingMessage: run.automatic?.message || "",
    nowMs: Date.now(),
  });
  if (view.visible) host.append(buildActivityCard(view));
}

function renderStepper() {
  const stepper = $("stages");
  const existing = Array.from(stepper.children);
  if (existing.length !== state.stages.length || existing.some((node, index) => node.dataset.stage !== state.stages[index].id)) {
    stepper.textContent = "";
    for (const stage of state.stages) {
      const step = document.createElement("button");
      step.type = "button";
      step.dataset.stage = stage.id;
      for (const className of ["dot", "step-label", "step-meta"]) {
        const node = document.createElement("span");
        node.className = className;
        step.append(node);
      }
      step.addEventListener("click", () => {
        const index = state.stages.findIndex((entry) => entry.id === step.dataset.stage);
        if (index < 0) return;
        const selected = state.stages[index];
        try { localStorage.setItem(stageSelectionKey(state.dagRunId), selected.id); } catch { /* Display preference only. */ }
        state.selectedStage = index;
        if (stageEntersPreview(selected.id) && state.previewSubject) {
          state.previewCollapsed = false;
          try { localStorage.setItem(previewCollapseKey(state.dagRunId), "0"); } catch { /* Display preference only. */ }
          applyPreviewExpansion();
        }
        if (state.lastRun) renderRun(state.lastRun);
        $("stage-panel").open = true;
      });
      stepper.append(step);
    }
  }
  state.stages.forEach((stage, index) => {
    const step = stepper.children[index];
    const shownState = displayState(stage.state, state.lastRun?.automatic);
    const selected = index === state.selectedStage;
    const reused = stage.reuse?.reused === true;
    step.className = `step ${shownState || ""}${selected ? " selected" : ""}`.trim();
    step.setAttribute("aria-pressed", selected ? "true" : "false");
    step.children[0].className = `dot ${stageDotState(shownState)}`;
    step.children[1].textContent = stage.name || stage.id;
    step.children[2].textContent = reused ? "Reused" : Number.isFinite(stage.total) && stage.total > 0
      ? `${Number.isFinite(stage.passed) ? stage.passed : 0}/${stage.total}`
      : STEP_STATE_SHORT[shownState] || stage.stateLabel;
    const warningCount = Number.isFinite(stage.counts.warnings) ? stage.counts.warnings : 0;
    const label = `${stage.name} · ${reused ? "Reused" : stage.stateLabel}${warningCount ? ` · warnings ${warningCount}` : ""}`;
    step.title = label;
    step.setAttribute("aria-label", label);
  });
}

function renderRerunPanel(run) {
  const host = $("stage-rerun");
  if (!host) return;
  const policyOpen = host.querySelector("details")?.open === true;
  const rows = Array.isArray(run.stage_reruns) ? run.stage_reruns : null;
  const stage = state.stages[state.selectedStage];
  // The select owns its in-progress input; changes to eligibility still replace the panel.
  if (!beginRender(host, [run.dag_run_id, rows, stage?.id, run.main_assembly, run.can_manage,
    run.deleted, state.rerunningRunId, state.rerunKeyByRun[run.dag_run_id], state.rerunKeyByRun[`${run.dag_run_id}:mjcf`], state.rerunNotice,
    state.rerunError])) return;
  if (!stage) {
    host.hidden = true;
    return;
  }
  if (!["freeze", "mjcf"].includes(stage.id)) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  const row = rows && rows.find((item) => item && item.stage === stage.id) || null;
  const runId = String(run.dag_run_id || "");
  const canManage = run.can_manage === true;
  const eligible = Boolean(row && row.eligible === true);
  const busy = state.rerunningRunId === runId;
  const deleted = run.deleted === true;
  const options = row && Array.isArray(row.main_assembly_options)
    ? row.main_assembly_options.filter((value) => typeof value === "string" && value)
    : [];
  if (stage.id === "mjcf") {
    const button = document.createElement("button"); button.type = "button";
    button.textContent = busy ? "Creating MJCF request..." : state.rerunKeyByRun[`${runId}:mjcf`] ? "Retry same MJCF request" : "Rerun MJCF";
    button.disabled = busy || !canManage || !eligible || deleted;
    button.onclick = () => { void rerunFromStage("mjcf"); };
    const note = document.createElement("p"); note.className = "muted small";
    note.textContent = deleted ? "This delivery is hidden; restore it before rerunning." : !canManage
      ? "Only the initiator or a platform administrator can rerun." : row?.message || "Source verification is unavailable.";
    const label = document.createElement("label"); label.textContent = "Simulation policy (JSON)";
    const policy = document.createElement("textarea"); policy.id = "mjcf-policy";
    policy.value = state.rerunMainByRun[`${runId}:mjcf`] || row?.simulation || "{}";
    policy.disabled = busy || !canManage || deleted;
    policy.oninput = () => {
      state.rerunMainByRun[`${runId}:mjcf`] = policy.value;
      delete state.rerunKeyByRun[`${runId}:mjcf`];
    };
    label.append(policy);
    const advanced = document.createElement("details"); advanced.open = policyOpen;
    const advancedTitle = document.createElement("summary"); advancedTitle.textContent = "Advanced simulation settings";
    advanced.append(advancedTitle, label); host.append(button, note, advanced);
    if (row?.source_dag_run_id) {
      const source = ghostButton(`Verified input source: ${shortRunId(row.source_dag_run_id)}`);
      source.title = String(row.source_dag_run_id);
      source.addEventListener("click", () => { void selectRun(row.source_dag_run_id); });
      host.append(source);
    }
    if (state.rerunError) { const error = document.createElement("p"); error.className = "error"; error.textContent = state.rerunError; host.append(error); }
    return;
  }
  const savedMain = typeof state.rerunMainByRun[runId] === "string" ? state.rerunMainByRun[runId] : "";
  const originalMain = typeof run.main_assembly === "string" ? run.main_assembly : "";
  const selectedMain = options.includes(savedMain)
    ? savedMain
    : (options.includes(originalMain) ? originalMain : "");

  const line = document.createElement("div");
  line.className = "rerun-row";
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = busy ? "Creating request..." : state.rerunKeyByRun[runId] ? "Retry same Freeze request" : "Create new request from Freeze";
  button.disabled = busy || !canManage || !eligible || deleted || !selectedMain;
  button.addEventListener("click", () => {
    void rerunFromStage(stage.id);
  });

  const assemblyLine = document.createElement("div");
  assemblyLine.className = "rerun-row rerun-assembly";
  const label = document.createElement("label");
  label.textContent = "New request from Freeze — main assembly in the sealed upload:";
  const select = document.createElement("select");
  select.setAttribute("aria-label", "Main assembly for the new request");
  if (!options.length) {
    const unavailable = document.createElement("option");
    unavailable.value = "";
    unavailable.textContent = "Verified main assembly list unavailable";
    unavailable.selected = true;
    select.append(unavailable);
    select.disabled = true;
  } else {
    for (const name of options) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      select.append(option);
    }
    select.value = selectedMain;
    select.disabled = busy || !canManage || deleted;
    select.addEventListener("change", () => {
      const value = String(select.value || "");
      if (value !== selectedMain) delete state.rerunKeyByRun[runId];
      if (value) state.rerunMainByRun[runId] = value;
      else delete state.rerunMainByRun[runId];
      button.disabled = busy || !canManage || !eligible || deleted || !value;
    });
  }
  assemblyLine.append(label, select);
  host.append(assemblyLine);

  line.append(button);

  const note = document.createElement("span");
  note.className = "muted small";
  if (state.rerunNotice) {
    note.textContent = state.rerunNotice;
  } else if (deleted) {
    note.textContent = "This delivery is hidden; restore it before rerunning.";
  } else if (!canManage) {
    note.textContent = "Only the initiator or a platform administrator can rerun.";
  } else if (row && row.message) {
    // Canonical backend reason shown verbatim whenever the stage is not eligible.
    note.textContent = row.message;
  } else if (eligible) {
    note.textContent = "Freeze creates a new request from the sealed originals; freeze and all later stages rerun under the current rules and configuration. The original run and evidence remain.";
  } else {
    note.textContent = "Rerun conditions for this stage cannot be confirmed; refresh later.";
  }
  line.append(note);
  host.append(line);

  if (row) {
    const meta = document.createElement("p");
    meta.className = "muted small rerun-meta";
    const pieces = [];
    const prereq = row.prerequisites && typeof row.prerequisites === "object" ? row.prerequisites : null;
    if (prereq) {
      const parts = [];
      for (const [key, label] of [["inputs", "inputs"], ["tool", "tool"], ["receipt", "receipt"]]) {
        const value = prereq[key];
        if (typeof value === "string" && value) parts.push(`${label}: ${PREREQ_STATE[value] || value}`);
      }
      if (parts.length) pieces.push(`Prerequisites: ${parts.join(" · ")}`);
    }
    const recomputes = Array.isArray(row.recomputes) ? row.recomputes : [];
    if (recomputes.length) {
      pieces.push(`Recomputes: ${recomputes.map((id) => rerunStageName(rows, id)).join(" -> ")}`);
    }
    if (prereq && prereq.earliest_required && prereq.earliest_required !== stage.id) {
      pieces.push(`Earliest restart: ${rerunStageName(rows, prereq.earliest_required)}`);
    }
    if (pieces.length) {
      meta.textContent = pieces.join("; ");
      host.append(meta);
    }
    const retains = Array.isArray(row.retains) ? row.retains : [];
    if (retains.length) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `Reused inputs/results (${retains.length})`;
      details.append(summary);
      const list = document.createElement("div");
      list.className = "rerun-retains";
      for (const item of retains) {
        if (!item || typeof item !== "object") continue;
        const entry = document.createElement("p");
        entry.textContent = [item.label || item.path || "", item.availability || ""].filter(Boolean).join(" · ");
        list.append(entry);
      }
      details.append(list);
      host.append(details);
    }
  }

  if (state.rerunError) {
    const error = document.createElement("p");
    error.className = "error";
    error.textContent = state.rerunError;
    host.append(error);
  }
}

async function rerunFromStage(stageId) {
  const run = state.lastRun;
  const runId = state.dagRunId;
  if (!["freeze", "mjcf"].includes(stageId)) return;
  if (!run || !runId || state.rerunningRunId !== null || run.can_manage !== true || !stageId) return;
  const body = { stage: stageId };
  if (stageId === "freeze") {
  let selectedMain = "";
  const row = (Array.isArray(run.stage_reruns) ? run.stage_reruns : [])
    .find((item) => item && item.stage === "freeze");
  const options = row && Array.isArray(row.main_assembly_options)
    ? row.main_assembly_options.filter((value) => typeof value === "string" && value)
    : [];
  selectedMain = typeof state.rerunMainByRun[runId] === "string"
    ? state.rerunMainByRun[runId]
    : String(run.main_assembly || "");
  if (!options.includes(selectedMain)) {
    state.rerunError = (row && row.message)
      || "Select a verified main assembly from the sealed originals before creating a new request.";
    renderRerunPanel(run);
    return;
  }
  body.main_assembly = selectedMain;
  } else {
    body.simulation = document.getElementById("mjcf-policy").value;
    try { JSON.parse(body.simulation); } catch { state.rerunError = "Simulation policy must be valid JSON."; renderRerunPanel(run); return; }
  }
  const requestSlot = stageId === "freeze" ? runId : `${runId}:mjcf`;
  state.rerunKeyByRun[requestSlot] ||= requestKey();
  const idempotencyKey = state.rerunKeyByRun[requestSlot];
  state.rerunningRunId = runId;
  state.rerunNotice = "";
  state.rerunError = "";
  renderRerunPanel(run);
  try {
    const payload = await api(`/api/runs/${encodeURIComponent(runId)}/attempts`, {
      method: "POST",
      body,
      idempotencyKey,
    });
    const nextRunId = responseRunId(payload, `portal-${stageId}-${idempotencyKey}`);
    if (state.rerunKeyByRun[requestSlot] === idempotencyKey) delete state.rerunKeyByRun[requestSlot];
    const stageToShow = String(payload.resume_from || stageId);
    await refreshRuns();
    if (state.dagRunId !== runId) return; // the operator moved on; the list is refreshed
    try {
      localStorage.setItem(stageSelectionKey(nextRunId), stageToShow);
    } catch {
      // remembering the requested stage is best-effort
    }
    await selectRun(nextRunId);
  } catch (error) {
    if (error.status === 401) return;
    if (state.dagRunId === runId) {
      const detail = error.payload && typeof error.payload === "object" ? error.payload : {};
      state.rerunError = detail.message || error.message || "Rerun was not accepted.";
      if (detail.earliest_required) {
        const target = String(detail.earliest_required);
        const index = state.stages.findIndex((item) => item.id === target);
        if (index !== -1) {
          state.selectedStage = index;
          try {
            // Persist the jump so the poll's stage chooser cannot overwrite it.
            localStorage.setItem(stageSelectionKey(runId), target);
          } catch {
            // best-effort persistence
          }
        }
      }
      await poll();
      await refreshRuns();
    }
  } finally {
    state.rerunningRunId = null;
    if (state.lastRun && state.lastRun.dag_run_id === state.dagRunId) renderRun(state.lastRun);
  }
}

function renderFindings(run, report) {
  const findings = $("findings");
  if (!beginRender(findings, [run.dag_run_id, run.findings, run.verification_reports, report?.failure, Boolean(report)])) return;
  // The registered verification evidence stays directly downloadable on runs
  // without a verified delivery (failed Windows or Linux runs); no URL hunting required.
  const retainedReports = Array.isArray(run.verification_reports) ? run.verification_reports : [];
  if (retainedReports.length) {
    const item = document.createElement("li");
    item.className = "report-download";
    const label = document.createElement("span");
    label.textContent = "Verification evidence:";
    item.append(label, " ");
    for (const entry of retainedReports) {
      const link = document.createElement("a");
      link.href = String(entry.url || "");
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = entry.label || entry.path || "Verification evidence";
      item.append(link, " ");
    }
    findings.append(item);
  }
  const failure = report && report.failure ? report.failure : null;
  // The auto finding repeating the failure card's raw error would be a second
  // red container for the same fact; show each failure once.
  const visibleFindings = (run.findings || []).filter(
    (finding) => !(failure && !finding.id && finding.message === failure.raw_error),
  );
  const codeLabels = new Map(
    ((failure && failure.finding_counts) || []).map((group) => [group.code, group.label || group.code]),
  );
  const findingNameValue = (finding) => {
    const evidence = finding && finding.evidence && typeof finding.evidence === "object" ? finding.evidence : {};
    const detail = evidence.detail && typeof evidence.detail === "object" ? evidence.detail : {};
    const value = typeof detail.name === "string" ? detail.name.trim() : "";
    return value || null;
  };
  const findingClass = (finding) => !finding.severity || finding.severity === "error"
    ? "finding-error" : finding.severity === "warning" ? "warn" : "finding-info";
  const renderFinding = (finding) => {
    const item = document.createElement("li");
    item.className = findingClass(finding);
    const message = document.createElement("div");
    // A missing robot name is not a malformed name; keep the two cases distinct.
    message.textContent =
      finding.id === "discovery.robot_name_invalid" && findingNameValue(finding) === null
        ? "No robot name was read (detail.name is empty)."
        : finding.message || "No description provided";
    const context = document.createElement("div");
    context.className = "object";
    context.textContent = [finding.severity, finding.id, finding.stage, finding.object].filter(Boolean).join(" · ") || "—";
    item.append(message, context);
    if (finding.evidence && Object.keys(finding.evidence).length) {
      const evidence = document.createElement("pre");
      evidence.className = "object";
      evidence.textContent = JSON.stringify(finding.evidence, null, 2);
      expandable(item, "View evidence").append(evidence);
    }
    return item;
  };
  if (!visibleFindings.length) {
    const item = document.createElement("li");
    item.className = "muted";
    item.textContent = report ? "No findings reported." : "Check results are not available yet.";
    findings.append(item);
  }
  // Coded findings are grouped by their check code so a large discovery set stays a
  // compact hierarchy with every finding retained and expandable inside its group.
  const findingGroups = new Map();
  for (const finding of visibleFindings) {
    const code = finding.id || "";
    if (!code) {
      findings.append(renderFinding(finding));
      continue;
    }
    if (!findingGroups.has(code)) findingGroups.set(code, []);
    findingGroups.get(code).push(finding);
  }
  const orderedGroups = [...findingGroups.entries()].sort(
    (a, b) => Number(b[1].some((item) => item.severity === "error")) -
      Number(a[1].some((item) => item.severity === "error")) ||
      b[1].length - a[1].length || a[0].localeCompare(b[0]),
  );
  orderedGroups.forEach(([code, items]) => {
    const outer = document.createElement("li");
    const section = document.createElement("details");
    const header = document.createElement("summary");
    let groupLabel = codeLabels.get(code) || items[0].message || code;
    if (code === "discovery.robot_name_invalid") {
      const missing = items.filter((finding) => findingNameValue(finding) === null).length;
      if (missing === items.length) groupLabel = "Missing robot name";
    }
    header.textContent = `${groupLabel} (${items.length})`;
    const nested = document.createElement("ul");
    nested.className = "finding-group";
    // Bounded first paint: a large group renders its rows only when the operator opens it.
    let populated = false;
    const populate = () => {
      if (populated) return;
      populated = true;
      for (const finding of items) nested.append(renderFinding(finding));
    };
    section.addEventListener("toggle", () => {
      if (section.open) populate();
    });
    section.append(header, nested);
    const kinds = items.map(findingClass);
    outer.className = kinds.includes("finding-error") ? "finding-error" : kinds.includes("warn") ? "warn" : "finding-info";
    outer.append(section);
    findings.append(outer);
  });

}

function renderRun(run) {
  $("run-loading").hidden = true;
  $("run-content").hidden = false;
  const runId = String(run.dag_run_id || "");
  const listEntry = (state.runsList || []).find((item) => item && item.dag_run_id === runId) || null;
  const deleted = resolveDeleted(run, listEntry);
  const customTitle = resolveTitle(run, listEntry);
  $("run-heading").textContent = customTitle || assemblyLabel(run.main_assembly) || "Engineering delivery";
  $("run-state-label").textContent = RUN_STATES[displayState(run.state, run.automatic)] || run.state || "State unconfirmed";
  $("run-dot").className = `dot ${runDotState(displayState(run.state, run.automatic))}`;
  $("run-initiator").textContent = `Initiator: ${run.operator || run.user || "not recorded"}`;
  $("run-purpose").textContent = purposeLabel(run.purpose);
  const deletedTag = $("run-deleted");
  const deletedAt = run.deleted_at || (listEntry && listEntry.deleted_at) || null;
  const deletedBy = run.deleted_by || (listEntry && listEntry.deleted_by) || null;
  deletedTag.textContent = deleted
    ? `Hidden · ${formatRunTime(deletedAt) || "time not recorded"} · Hidden by: ${deletedBy || "not recorded"}`
    : "";
  deletedTag.hidden = !deleted;
  const actionsHost = $("run-actions");
  const canManage = run.can_manage === true;
  const action = menuAction(state.runActionsFor, runId, canManage);
  if (action !== "keep") {
    // Rebuild or clear only when the selected run changes (or actions vanish); the 3s poll
    // must never destroy an open rename form or steal focus from it.
    actionsHost.textContent = "";
    if (action === "rebuild") actionsHost.append(buildRunMenu(listEntry || run));
    state.runActionsFor = action === "rebuild" ? runId : null;
  }
  const origin = $("run-origin");
  origin.textContent = "";
  // The detail payload carries immutable lineage independently of cached list rows.
  const parentId = run.parent_dag_run_id;
  if (parentId) {
    const parentStage = run.resume_from_name || "upstream stage";
    origin.append(`Rerun from ${parentStage} · source `);
    const link = document.createElement("button");
    link.type = "button";
    link.className = "ghost";
    link.textContent = shortRunId(parentId);
    link.title = String(parentId);
    link.addEventListener("click", () => {
      void selectRun(parentId);
    });
    origin.append(link);
    origin.hidden = false;
  } else {
    origin.hidden = true;
  }
  const meta = $("run-meta");
  meta.textContent = "";
  const report = run.report && Array.isArray(run.report.stages) ? run.report : null;
  const coverage = run.coverage || {};
  const rows = [
    ["State", RUN_STATES[displayState(run.state, run.automatic)] || run.state || "—"],
    ["Started", formatTime(run.started_at)],
    ["Ended", formatTime(run.ended_at)],
  ];
  if (typeof run.main_assembly === "string" && run.main_assembly) {
    rows.unshift(["Main assembly", run.main_assembly]);
  }
  rows.splice(1, 0, ["Purpose", purposeLabel(run.purpose)]);
  const measured = report && report.measured ? report.measured : null;
  if (measured) {
    if (measured.subject_sha256) rows.push(["Delivery subject", `${String(measured.subject_sha256).slice(0, 12)}...`]);
    const window = measured.expected_mass_window;
    if (window && Number.isFinite(window.mass_kg)) {
      const range = Array.isArray(window.expected_kg) && window.expected_kg.length === 2
        ? `[${window.expected_kg[0]}, ${window.expected_kg[1]}] kg`
        : "—";
      rows.push(["Expected mass range", `measured ${window.mass_kg} kg; range ${range}`]);
    }
    const closure = measured.mass_closure;
    if (closure && Number.isFinite(closure.urdf_mass_kg)) {
      const delta = Number.isFinite(closure.delta_kg) ? `; difference ${closure.delta_kg} kg` : "";
      rows.push([
        "Mass closure",
        `URDF ${closure.urdf_mass_kg} kg / whole CAD ${closure.whole_cad_mass_kg} kg${delta}`,
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
  const headlineText = run.automatic?.state === "waiting" && report?.overall?.state === "unresolved"
    ? run.automatic.message
    : report?.overall?.headline || "";
  headline.textContent = headlineText || "";
  headline.hidden = !headlineText;
  const failureCard = $("failure-card");
  if (beginRender(failureCard, [runId, report?.failure, report?.stages])) {
    if (report?.failure) failureCard.append(buildFailureCard(report.failure, report));
  }
  failureCard.hidden = !report?.failure;
  if (state.detailsInitializedFor !== runId) {
    state.detailsInitializedFor = runId;
    $("stage-panel").open = run.state !== "success";
  }

  const retry = run.retry || {};
  const canRetry = !deleted && run.can_manage === true && retry.eligible === true;
  const retryReason = typeof retry.reason === "string" ?
    RETRY_REASONS[retry.reason] || "Retry is currently unavailable; review the check results." : "";
  $("retry-panel").hidden = !deleted && !canRetry && !(run.state === "failed" && retryReason);
  $("retry-button").hidden = !canRetry;
  $("retry-button").disabled = state.retryingRunId !== null;
  $("retry-button").textContent = state.retryingRunId === runId ? "Retrying..." : "Continue original job";
  $("retry-note").textContent = deleted ?
    RETRY_REASONS.deleted :
    retry.eligible === true && run.can_manage !== true ?
      "The initiator or a platform administrator can retry this run." : retryReason;

  const progress = $("task-progress");
  progress.textContent = "";
  for (const task of run.tasks || []) {
    const chip = document.createElement("span");
    chip.className = `chip ${task.state || ""}`.trim();
    chip.textContent = `${TASK_LABELS[task.task_id] || task.task_id}: ${TASK_STATES[task.state] || task.state || "—"}`;
    progress.append(chip);
  }

  state.stages = stageList(run);
  state.selectedStage = chooseStageIndex(state.stages, runId);
  renderStepper();
  renderStageDetail();
  renderActivity(run);

  const automatic = $("automatic");
  automatic.textContent = "";
  const automaticState = (run.automatic && run.automatic.state) || "pending";
  const automaticKind = automaticStateKind(automaticState);
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
    chip.textContent = `Not covered automatically: ${text}`;
    chipRow.append(chip);
  }
  automatic.append(chipRow);
  const confirmations = $("confirmations");
  confirmations.textContent = "";
  const external = report && report.external_review && typeof report.external_review === "object" ? report.external_review : null;
  const scopes = external && Array.isArray(external.scopes) ? external.scopes : [];
  const engineeringState = report && report.overall ? report.overall.engineering_state : null;
  if (!report) {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent = "Report data is unavailable, so engineering review scopes cannot be shown. Refresh or contact platform maintenance.";
    confirmations.append(note);
  } else if (engineeringState !== "external_review") {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent = "Engineering review scopes are not ready; after independent verification passes, externally confirmed facts appear here.";
    confirmations.append(note);
  } else if (scopes.length) {
    const lead = document.createElement("p");
    lead.className = "muted small";
    lead.textContent = "Engineering review scopes (facts that automatic checks cannot verify and external review must confirm):";
    confirmations.append(lead);
    for (const scope of scopes) {
      const item = document.createElement("div");
      item.className = "review-fact";
      const label = document.createElement("strong");
      label.textContent = scope.label || scope.id || "";
      item.append(label);
      if (scope.scope) {
        const text = document.createElement("p");
        text.textContent = scope.scope;
        item.append(text);
      }
      if (scope.automatic_exclusion) {
        const text = document.createElement("p");
        text.className = "muted small";
        text.textContent = scope.automatic_exclusion;
        item.append(text);
      }
      const stageName = (state.stages.find((entry) => entry.id === scope.stage) || {}).name;
      if (stageName) {
        const text = document.createElement("p");
        text.className = "muted small";
        text.textContent = `Stage: ${stageName}`;
        item.append(text);
      }
      confirmations.append(item);
    }
    if (external.note) {
      const note = document.createElement("p");
      note.className = "muted small";
      note.textContent = external.note;
      confirmations.append(note);
    }
  } else {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent = "The report lists no external facts requiring manual confirmation.";
    confirmations.append(note);
  }

  renderFindings(run, report);

  const pr = $("pr");
  pr.textContent = "";
  if (run.pr && run.pr.url) {
    const link = document.createElement("a");
    link.href = run.pr.url;
    link.target = "_blank";
    link.rel = "noreferrer noopener";
    link.textContent = "Open delivery PR";
    link.className = "button";
    link.title = String(run.pr.url || "");
    pr.append(link);
    const stateText = document.createElement("span");
    stateText.className = "muted small";
    stateText.textContent = run.pr.state || "";
    pr.append(stateText);
    pr.hidden = false;
  } else if (automaticState === "passed") {
    pr.textContent = "Verification passed. Publication has not completed.";
    pr.hidden = false;
  } else {
    pr.hidden = true;
  }
  renderRerunPanel(run);
  updatePreviewLayout();
}

function renderPreviewDiagnostic(preview) {
  const node = $("preview-diagnostic");
  if (!beginRender(node, [state.dagRunId, state.previewFormat, preview])) return;
  const diagnostic = previewDiagnosticState(preview);
  const mjcfUnverified = state.previewFormat === "mjcf" && preview.mjcf?.verified === false;
  if (!diagnostic.visible && !mjcfUnverified) {
    node.hidden = true;
    node.replaceChildren();
    return;
  }
  node.replaceChildren();
  node.hidden = false;
  if (diagnostic.unverified) {
    const failure = document.createElement("p");
    failure.className = "preview-failure";
    failure.textContent = diagnostic.reason === "run_unresolved"
      ? "The run has unresolved external effects; this delivery has no current formal checkpoint qualification."
      : diagnostic.mechanical_passed
      ? "Mechanical verification passed, but this diagnostic replay has no formal checkpoint qualification and must not be published."
      : "Engineering verification failed: this delivery is diagnostic only and must not be published.";
    node.append(failure);
  } else if (mjcfUnverified) {
    const failure = document.createElement("p");
    failure.className = "preview-failure";
    failure.textContent = "This MJCF preview is diagnostic only; formal publication qualification is not established.";
    node.append(failure);
  }
  const checks = Array.isArray(preview.failing_checks) ? preview.failing_checks : [];
  const checksTotal = Number.isInteger(preview.failing_checks_total) ? preview.failing_checks_total : checks.length;
  if (checks.length) {
    const shown = checks.slice(0, 8).map((check) => `${check.id} (${check.message || "check failed"})`);
    const line = document.createElement("p");
    line.textContent =
      `Failed checks: ${shown.join(", ")}` +
      `${checksTotal > shown.length ? ` and ${checksTotal} total` : ""}`;
    node.append(line);
  }
  if (diagnostic.warnings.length) {
    const warningDetails = expandable(node, `Source warnings (${diagnostic.warnings_total})`);
    warningDetails.className = "preview-warning-sources";
    const statement = document.createElement("p");
    statement.className = "muted small";
    statement.textContent =
      "Recorded source warnings are neither passes nor publication evidence. " +
      "Default ranges are not native mechanical limits or hardware qualifications.";
    warningDetails.append(statement);
    const certificateArtifact = registeredVerificationArtifact(preview, state.dagRunId);
    if (certificateArtifact) {
      const link = document.createElement("a");
      link.href = certificateArtifact.href;
      link.target = "_blank";
      link.rel = "noopener";
      link.dataset.sha256 = certificateArtifact.sha256;
      link.textContent = "Open verification certificate";
      warningDetails.append(link);
    }
    for (const warning of diagnostic.warnings) {
      const group = document.createElement("div");
      group.className = "preview-warning";
      const heading = document.createElement("strong");
      heading.textContent = `${warning.code || "warning"}${warning.object ? ` · ${warning.object}` : ""}`;
      group.append(heading);
      if (warning.message) {
        const message = document.createElement("p");
        message.className = "warning-message";
        message.textContent = warning.message;
        group.append(message);
      }
      appendWarningSourceRows(group, warning);
      warningDetails.append(group);
    }
    if (preview.warning_categories_total > diagnostic.warnings.length) {
      const more = document.createElement("p");
      more.className = "muted small";
      more.textContent =
        `${preview.warning_categories_total - diagnostic.warnings.length} more warning categories ` +
        "are available in the report.";
      warningDetails.append(more);
    }
  }
}

async function loadPreview(dagRunId) {
  if (state.previewRequest) return;
  const request = new AbortController();
  state.previewRequest = request;
  const note = $("viewer-note");
  try {
    const preview = await api(`/api/runs/${encodeURIComponent(dagRunId)}/preview`, { signal: request.signal });
    if (request.signal.aborted || state.dagRunId !== dagRunId) return;
    const selector = $("preview-format");
    selector.querySelector('[value="mjcf"]').disabled = !preview.mjcf;
    selector.querySelector('[value="urdf"]').disabled = !preview.urdf;
    if (!preview.urdf) state.previewFormat = "mjcf";
    if (!preview.mjcf && state.previewFormat === "mjcf") state.previewFormat = "urdf";
    selector.value = state.previewFormat;
    renderPreviewDiagnostic(preview);
    const previewKey = `${preview.subject_sha256}:${preview.mjcf?.certificate?.sha256 || ""}:${state.previewFormat}`;
    if (state.previewKey === previewKey) return;
    $("preview-meta").textContent = `Delivery subject ${String(preview.subject_sha256).slice(0, 12)}... · URDF ${preview.urdf}`;
    note.hidden = true;
    setError($("preview-note"), "");
    state.viewer = ensureSingleViewer(state, () => createViewer($("viewer"))).viewer;
    disposeRobot(state.viewer);
    state.mjcfView = null;
    state.controls = null;
    $("joint-controls").replaceChildren();
    note.textContent = "Loading the selected model format...";
    note.hidden = false;
    const artifactUrl = (name) =>
      `/api/runs/${encodeURIComponent(dagRunId)}/artifacts/${String(name)
        .split("/")
        .map(encodeURIComponent)
        .join("/")}`;
    const isMJCF = state.previewFormat === "mjcf";
    $("mjcf-tools").hidden = !isMJCF;
    $("mjcf-status").hidden = !isMJCF;
    $("mjcf-evidence").hidden = !isMJCF;
    $("preview-panels").hidden = isMJCF;
    let loaded;
    if (isMJCF) {
      const { loadMJCF } = await import("/static/mjcf_viewer.js");
      loaded = await loadMJCF(state.viewer, { descriptor: preview.mjcf, artifactUrl, signal: request.signal,
        onStatus: (status) => {
          if (status.error) { state.controls?.refresh(); setError($("preview-note"), status.error); return; }
          state.controls?.refresh();
          $("mjcf-play").textContent = status.playing ? "Pause" : "Play";
          $("mjcf-status").textContent = `MuJoCo ${preview.mjcf.engine.mujoco} · ${status.time.toFixed(3)} s · ${status.contacts} contacts · ${status.bodies} bodies · ${status.geoms} geoms. No controller qualification.`;
        },
      });
      state.mjcfView = loaded;
      const collision = preview.mjcf.collision || {};
      const proxies = collision.mesh_proxies || [];
      const linkSelect = $("mjcf-link"); linkSelect.replaceChildren();
      for (const name of ["", ...new Set([...(loaded.links || []), ...proxies.map((proxy) => proxy.link)])]) {
        const option = document.createElement("option"); option.value = name; option.textContent = name || "All links"; linkSelect.append(option);
      }
      const showCollision = () => {
        const selected = proxies.filter((proxy) => !linkSelect.value || proxy.link === linkSelect.value);
        loaded.selectLink(linkSelect.value);
        const text = `Collision source: ${preview.mjcf.facts.collision_source} · ${selected.length} mesh proxies · vertex budget ${collision.vertex_budget ?? "n/a"} · error budget ${collision.max_error_m ?? "n/a"} m. ${collision.scope || ""}`;
        $("mjcf-collision-info").textContent = text;
        for (const proxy of selected) {
          const row = document.createElement("p"); row.className = "muted small";
          row.textContent = `${proxy.link} · ${proxy.geom} · ${proxy.actual_vertices} vertices · convex distance ${proxy.complete_convex_vertex_distance_m} m · ${(proxy.source_meshes || []).join(", ")}`;
          $("mjcf-collision-info").append(row);
        }
      };
      linkSelect.onchange = showCollision; showCollision();
      $("mjcf-warnings").replaceChildren();
      for (const warning of preview.mjcf.warnings || []) { const item = document.createElement("li"); item.textContent = warning; $("mjcf-warnings").append(item); }
      loaded.setMode($("mjcf-mode").value); loaded.setMarkers($("mjcf-markers").checked);
      $("preview-meta").textContent = `MJCF ${preview.mjcf.entry} · subject ${preview.mjcf.subject_sha256.slice(0, 12)}...`;
      const evidence = document.createElement("a"); evidence.href = artifactUrl(preview.mjcf.certificate.path);
      evidence.textContent = "Collision approximation and source warnings"; evidence.target = "_blank"; evidence.rel = "noopener";
      $("preview-meta").append(" · ", evidence);
      for (const name of ["mjcf/robot.xml", ...(preview.mjcf.files["mjcf/scene.xml"] ? ["mjcf/scene.xml"] : [])]) {
        const download = document.createElement("a"); download.href = artifactUrl(name);
        download.download = name.split("/").pop(); download.textContent = `Download ${download.download}`;
        $("preview-meta").append(" · ", download);
      }
    } else {
      state.mjcfView = null;
      loaded = await loadRobot(state.viewer, {
      urdfUrl: artifactUrl(preview.urdf),
      urdfPath: preview.urdf,
      files: preview.files,
      artifactUrl,
      signal: request.signal,

    });
    }
    if (request.signal.aborted || state.dagRunId !== dagRunId) return;
    const warnings = isMJCF ? [] : loaded.warnings;
    note.textContent = "";
    note.hidden = warnings.length === 0;
    if (warnings.length) {
      const details = expandable(note, `Viewer notices (${warnings.length})`);
      const evidence = document.createElement("pre");
      evidence.textContent = warnings.join("\n");
      details.append(evidence);
    }
    $("inspection-primary").hidden = isMJCF;
    $("inspection-controls").hidden = isMJCF;
    if (!isMJCF) {
      const inspection = createInspection(state.viewer, loaded, $("inspection-controls"), {
        onPoseChange: () => state.controls?.refresh(),
        onJointSelect: (name) => revealJoint(name),
      });
      $("inspection-primary").replaceChildren(inspection.primary);
    }
    state.controls = buildJointControls($("joint-controls"), loaded.joints, {
      onInput: () => state.viewer.inspection?.update(),
    });
    $("joints-title").textContent = `Joint controls (${loaded.joints.length})`;
    $("focus-joints").textContent = `Joints (${loaded.joints.length})`;
    $("focus-joints").disabled = loaded.joints.length === 0;
    state.previewKey = previewKey;
    state.previewSubject = isMJCF ? preview.mjcf.subject_sha256 : preview.subject_sha256;
    state.previewFiles = preview.files || {};
    if (state.previewCollapseFor !== dagRunId) {
      state.previewCollapseFor = dagRunId;
      try {
        state.previewCollapsed = localStorage.getItem(previewCollapseKey(dagRunId)) === "1";
      } catch {
        state.previewCollapsed = false;
      }
    }
    if (!isMJCF) await renderPreviewPanels(preview, loaded.records, artifactUrl, request.signal);
    else $("preview-info").textContent = "";
    if (request.signal.aborted || state.dagRunId !== dagRunId) return;
    if (state.lastRun) renderRun(state.lastRun);
    $("viewer-card").hidden = false;
    $("viewer-placeholder").hidden = true;
    updatePreviewLayout();
    applyPreviewExpansion();
  } catch (error) {
    if (request.signal.aborted || state.dagRunId !== dagRunId) return;
    if (state.viewer) disposeRobot(state.viewer);
    state.controls = null;
    state.previewSubject = null;
  state.previewKey = null;
    state.previewFiles = null;
    state.previewData = null;
    if (state.lastRun) renderRun(state.lastRun);
    $("joint-controls").textContent = "";
    updatePreviewLayout();
    if (error.status === 404 || error.status === 409) {
      setError($("preview-note"), `No verified delivery is available: ${error.message}`);
      return;
    }
    setError($("preview-note"), `Delivery preview unavailable: ${error.message}`);
  } finally {
    if (state.previewRequest === request) state.previewRequest = null;
  }
}

async function poll() {
  const dagRunId = state.dagRunId;
  if (!dagRunId || state.pollRequest) return;
  if (state.timer) window.clearTimeout(state.timer);
  state.timer = null;
  const request = new AbortController();
  state.pollRequest = request;
  const ownsRead = () => state.pollRequest === request && state.dagRunId === dagRunId && !request.signal.aborted;
  try {
    const run = await api(`/api/runs/${encodeURIComponent(dagRunId)}`, { signal: request.signal });
    if (!ownsRead()) return;
    responseRunId(run, dagRunId);
    setError($("run-error-detail"), "");
    const previewReady = hasPreviewEvidence(run);
    if (!previewReady) clearPreview();
    const previous = state.lastRun;
    state.lastRun = run;
    renderRun(run);
    if (previewReady) await loadPreview(dagRunId);
    if (!ownsRead()) return;
    // Keep canonical polling after terminal states so a late conflict can revoke success.
    if (["success", "failed", "cancelled", "unresolved"].includes(displayState(run.state, run.automatic))) {
      stopActivityTimer();
    } else {
      startActivityTimer();
    }
    if (shouldRefreshRunList(previous, run)) await refreshRuns();
  } catch (error) {
    if (!ownsRead()) return;
    if (error.status === 401) return;
    if (error.status === 403 || error.status === 404) {
      showUploadView();
      await refreshRuns();
      setError($("run-error"), error.message);
      return;
    }
    state.lastRun = null;
    clearPreview();
    stopActivityTimer();
    renderRunLoading(dagRunId, true);
    setError($("run-error-detail"), error.message);
  } finally {
    if (ownsRead()) {
      state.pollRequest = null;
      state.timer = window.setTimeout(() => {
        state.timer = null;
        void poll();
      }, 3000);
    }
  }
}

function renderRunLoading(dagRunId, unavailable = false) {
  $("run-content").hidden = true;
  $("run-loading").hidden = false;
  $("run-loading").textContent = unavailable
    ? `The current state of run ${dagRunId} could not be read. Retrying...`
    : `Loading run ${dagRunId}...`;
  $("run-heading").textContent = unavailable ? "Run status unavailable" : "Loading run...";
  $("run-state-label").textContent = unavailable ? "Unknown" : "Loading";
  $("run-dot").className = "dot idle";
  $("run-initiator").textContent = "";
  $("run-purpose").textContent = "";
  for (const id of ["run-origin", "run-deleted", "pr", "retry-panel"]) $(id).hidden = true;
  $("run-actions").textContent = "";
  state.runActionsFor = null;
}

async function selectRun(dagRunId) {
  stopRunRead();
  state.dagRunId = dagRunId;
  state.lastRun = null;
  state.selectedStage = null;
  state.detailsInitializedFor = null;
  state.rerunNotice = "";
  state.rerunError = "";
  renderRunLoading(dagRunId);
  setError($("run-error-detail"), "");
  setError($("retry-error"), "");
  showRunView();
  renderRunsList();
  const knownRun = (state.runsList || []).some((item) => item && item.dag_run_id === dagRunId);
  if (!knownRun) void refreshRuns();
  startActivityTimer();
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
    stopRunRead();
    startActivityTimer();
    await poll();
    await refreshRuns();
  } catch (error) {
    if (error.status === 401) return;
    if (state.dagRunId === dagRunId) {
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
    showUploadView(true);
    setError($("run-error"), "");
    void refreshRuns();
  });

  $("runs-view-active").addEventListener("click", () => setRunView("active"));
  $("runs-view-deleted").addEventListener("click", () => setRunView("deleted"));
  $("retry-button").addEventListener("click", retryRun);
  $("logout").addEventListener("click", async () => {
    const csrf = state.csrf;
    try {
      await api("/api/session", { method: "DELETE" });
    } catch {
      // the local session is cleared even if Airflow is unreachable
    }
    if (state.csrf === csrf) clearSession();
  });

  $("choose-button").addEventListener("click", () => $("folder-input").click());
  $("targets-retry").addEventListener("click", async () => {
    if (state.folderPick) await loadTargets(state.folderPick);
  });

  $("folder-input").addEventListener("change", () => {
    state.blockedPick = false;
    const picked = folderPick(Array.from($("folder-input").files || []));
    const summary = $("folder-summary");
    if (picked.error) {
      state.folderPick = null;
      summary.hidden = true;
      setError($("run-error"), picked.error);
      resetAssemblyRow();
      $("start-button").disabled = true;
      return;
    }
    state.submissionKey = null;
    state.folderPick = picked;
    summary.hidden = false;
    summary.textContent = `${picked.top} · ${picked.count} files · ${formatBytes(picked.bytes)}`;
    setError($("run-error"), "");
    applyAssemblies(picked);
    loadTargets(picked);
    updateStartEnabled();
  });

  $("assembly-select").addEventListener("change", () => {
    state.assemblyChoice = $("assembly-select").value || null;
    changeSubmission();
    if (state.folderPick) void loadTargets(state.folderPick);
  });

  $("cancel-button").addEventListener("click", () => {
    stopRunRead();
    if (state.uploadAbort) state.uploadAbort.abort();
  });

  $("run-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.uploading) return;
    const picked = state.folderPick;
    if (!picked) {
      setError($("run-error"), "Select a local engineering folder first.");
      return;
    }
    if (!state.assemblyChoice || !picked.assemblies.includes(state.assemblyChoice)) {
      setError($("run-error"), "Select the main assembly first.");
      return;
    }
    const start = $("start-button");
    const status = $("upload-status");
    state.uploading = true;
    start.disabled = true;
    $("choose-button").disabled = true;
    $("folder-input").disabled = true;
    $("assembly-select").disabled = true;
    for (const id of ["target-select", "purpose-select", "change-summary"]) $(id).disabled = true;
    $("cancel-button").hidden = false;
    status.hidden = false;
    status.textContent = "Uploading engineering folder...";
    setError($("run-error"), "");
    try {
      const payload = await submitRun(picked, {
        onProgress: (ratio) => {
          if (ratio >= 1) {
            status.textContent = "Upload complete; creating run...";
            $("cancel-button").hidden = true;
          } else {
            status.textContent = `Uploading engineering folder... ${Math.round(ratio * 100)}%`;
          }
        },
      });
      status.textContent = "Upload complete; creating run...";
      await selectRun(payload.dag_run_id);
      resetPick();
    } catch (error) {
      status.hidden = true;
      if (error.status === 401) return;
      if (error.unconfirmed || error.network || error.aborted || (error.status === 502 && error.dagRunId)) {
        state.blockedPick = true;
        const runId = error.dagRunId || `portal-${state.submissionKey}`;
        const reason = error.aborted ? "Upload stopped; its outcome is unconfirmed." : error.message;
        setError($("run-error"), `${reason} Run ID: ${runId}. Check the run list or retry this same submission; no automatic retry will occur.`);
        await refreshRuns().catch(() => {});
      } else {
        setError($("run-error"), error.message);
      }
    } finally {
      state.uploading = false;
      state.uploadAbort = null;
      $("choose-button").disabled = false;
      $("folder-input").disabled = false;
      $("assembly-select").disabled = false;
      for (const id of ["target-select", "purpose-select", "change-summary"]) $(id).disabled = false;
      $("cancel-button").hidden = true;
      if (state.folderPick) {
        updateStartEnabled();
      } else {
        start.disabled = true;
        status.hidden = true;
      }
    }
  });

  $("reset-joints").addEventListener("click", () => {
    if (state.controls) state.controls.reset();
  });
  $("preview-collapse").addEventListener("click", togglePreviewCollapse);
  $("fit-preview").addEventListener("click", fitPreview);
  $("focus-joints").addEventListener("click", () => revealJoint(state.viewer?.inspection?.selectedJointName, true));
  $("preview-format").addEventListener("change", () => {
    state.previewFormat = $("preview-format").value;
    state.previewRequest?.abort(); state.previewRequest = null;
    void loadPreview(state.dagRunId);
  });
  $("mjcf-mode").onchange = () => state.mjcfView?.setMode($("mjcf-mode").value);
  $("mjcf-markers").onchange = () => state.mjcfView?.setMarkers($("mjcf-markers").checked);
  $("mjcf-play").onclick = () => state.mjcfView?.togglePlay();
  $("mjcf-step").onclick = () => state.mjcfView?.step();
  $("mjcf-reset").onclick = () => { state.mjcfView?.reset(); state.controls?.refresh(); };
}

async function boot() {
  wire();
  try {
    const session = await api("/api/session");
    showSession(session);
    await refreshRuns();
  } catch (error) {
    if (error.status !== 401) clearSession();
  }
}

void boot();
