// Operator page logic. All Airflow and Windows endpoint credentials stay on the server; this
// module only talks to the portal's own JSON API and the digest-verified artifact routes.
import { buildJointControls, createViewer, disposeRobot, loadRobot } from "/static/viewer.js";
import { menuAction, overlayRun, resolveDeleted, resolveTitle } from "/static/run_history.js";
import { activityPlacement, activityView } from "/static/activity.js";
import {
  closureRows,
  ensureSingleViewer,
  frameRows,
  infoPanelRows,
  jointRows,
  linkRows,
  previewExpansion,
  purposeLabel,
  stageEntersPreview,
  stageSummaryRows,
  summaryIdentity,
} from "/static/preview_logic.js";

const state = {
  csrf: null,
  user: null,
  dagRunId: null,
  previewSubject: null,
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
  blockedPick: false,
  targetsRetryPick: null,
  stages: [],
  selectedStage: null,
  viewer: null,
  controls: null,
  timer: null,
  activityTimer: null,
  retryingRunId: null,
  tabsInitializedFor: null,
  rerunningRunId: null,
  rerunNotice: null,
  rerunError: null,
  runsList: [],
  runView: "active",
  runsNextOffset: null,
  runsLoadingMore: false,
  runActionsFor: null,
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
const TASK_LABELS = {
  freeze: "冻结输入",
  discover: "解析结构",
  capture: "采集证据",
  generate: "生成 URDF",
  verify: "独立验证",
  publish: "提交评审 PR",
};
const CHECK_STATES = { passed: "通过", failed: "失败", not_run: "未执行", unsupported: "不支持" };
const STAGE_STATES = { not_run: "未执行", running: "执行中", completed: "完成", failed: "失败", blocked: "上游阻断" };
// Compact state words for the stepper; the full reason stays in the tooltip.
const STEP_STATE_SHORT = { completed: "已完成", running: "进行中", failed: "失败", blocked: "已阻断", not_run: "未执行" };
const PREREQ_STATE_ZH = { ok: "就绪", invalid: "不可用", absent: "缺失", changed: "已变更", unknown: "未知" };
const AUTOMATIC_STATES = {
  passed: "自动校验通过",
  failed: "自动校验失败",
  unverified: "未通过独立校验",
  pending: "等待自动检查",
  queued: "排队中",
  running: "检查中",
};
const RETRY_REASONS = {
  temporary_disconnection: "继续原作业，不重新采集 CAD。",
  evidence_corruption: "捕获证据已封存：恢复只重读封存证据，不重采 CAD。",
  engineering_terminal_failure: "工程阶段已终态失败：请按检查结果修复后新建关联运行。",
  publication_response_unknown: "发布响应未知：请先完成结果对账，不可原地重试。",
  run_active: "运行尚未结束。",
  run_succeeded: "运行已成功。",
  run_missing: "该运行已不存在，请刷新运行列表。",
  admission_failed: "原输入未完成冻结，请查看问题并新建运行。",
  unknown_failed_task: "无法确认可恢复的任务，请联系平台维护人员。",
  no_failed_operation: "没有失败的操作可恢复，请查看问题与发现。",
  native_terminal_failure: "原作业已终止；可查看检查结果后新建关联运行。",
  native_evidence_unavailable: "无法确认原生作业证据，稍后再试。",
  sealed_evidence_reimport: "捕获证据已封存：恢复只重读封存证据，不重采 CAD。",
  portable_retry: "继续无副作用阶段，不重新采集 CAD。",
  endpoint_evidence_unavailable: "无法确认原作业状态，稍后再试。",
  deleted: "此运行已删除：请先恢复后再重试或重跑。",
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

function shortRunId(value) {
  const text = String(value || "");
  return text.split("-").pop() || text;
}

function rerunStageName(rows, stageId) {
  const row = rows.find((item) => item && item.stage === stageId);
  return row ? row.name_zh || row.stage : String(stageId || "");
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
    note.textContent = "所选文件夹中没有 .SLDASM，无法提交：请选择包含主装配的工程文件夹。";
    return;
  }
  if (assemblies.length === 1) {
    state.assemblyChoice = assemblies[0];
    select.hidden = true;
    note.hidden = false;
    note.textContent = `主装配（自动）：${assemblies[0]}`;
    return;
  }
  select.hidden = false;
  note.hidden = true;
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "请选择主装配…";
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
    // A failed fetch is not "no targets configured": keep the pick so the fetch retries once a
    // session is ready again, and say what happened instead of misreporting an empty platform.
    state.targetsRetryPick = pick;
    const failure = document.createElement("option");
    failure.value = "";
    failure.textContent = "发布目标加载失败";
    failure.disabled = true;
    failure.selected = true;
    select.append(failure);
    note.hidden = false;
    if (error.status === 401) {
      note.textContent = "会话已失效，发布目标加载失败：请重新登录后再试。";
      clearSession();
      setError($("login-error"), "会话已失效，请重新登录后再试。");
    } else {
      note.textContent = "发布目标加载失败：网络或服务异常，请重新选择文件夹后重试。";
    }
    updateStartEnabled();
    return;
  }
  state.targetsRetryPick = null;
  const targets = payload.targets || [];
  const placeholder = document.createElement("option");
  placeholder.value = "";
  // Success with configured:false lands here; a fetch failure never does (handled above).
  placeholder.textContent = targets.length ? "请选择发布目标" : "平台未配置发布目标";
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
    note.textContent = `已按文件夹名匹配：${payload.suggestion}`;
  } else {
    note.hidden = true;
    note.textContent = "";
  }
  select.onchange = updateStartEnabled;
  if (summary) summary.oninput = updateStartEnabled;
  const purpose = $("purpose-select");
  if (purpose) purpose.onchange = updateStartEnabled;
  updateStartEnabled();
}

function updateStartEnabled() {
  const target = $("target-select");
  const summary = $("change-summary");
  const purpose = $("purpose-select");
  $("start-button").disabled = !(
    state.folderPick &&
    !state.blockedPick &&
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
      reject(new Error("发布目标为必填：请选择发布目标后重试"));
      return;
    }
    form.append("target_hardware", targetSelect.value);
    const purposeSelect = $("purpose-select");
    if (!purposeSelect || !purposeSelect.value) {
      reject(new Error("用途为必填：请选择用途后重试"));
      return;
    }
    form.append("purpose", purposeSelect.value);
    const summaryInput = $("change-summary");
    if (!summaryInput || !summaryInput.value.trim()) {
      reject(new Error("变更说明为必填：请填写后重试"));
      return;
    }
    form.append("change_summary", summaryInput.value.trim());
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
  resetAssemblyRow();
  const targetSelect = $("target-select");
  if (targetSelect) targetSelect.value = "";
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
    error.payload = payload;
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
  if (state.targetsRetryPick) void loadTargets(state.targetsRetryPick);
}

function clearPreview() {
  if (state.previewRequest) state.previewRequest.abort();
  state.previewRequest = null;
  state.previewSubject = null;
  state.previewFiles = null;
  state.previewData = null;
  state.previewCollapsed = false;
  state.previewCollapseFor = null;
  state.controls = null;
  if (state.viewer) disposeRobot(state.viewer);
  $("viewer-card").hidden = true;
  $("viewer-placeholder").hidden = false;
  $("preview-meta").textContent = "";
  $("preview-info").textContent = "";
  $("preview-diagnostic").hidden = true;
  $("preview-diagnostic").textContent = "";
  $("viewer-note").hidden = true;
  $("joint-controls").textContent = "";
  $("preview-panels").querySelectorAll(".panel-table").forEach((node) => {
    node.textContent = "";
  });
  $("preview-collapse").hidden = true;
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
  state.rerunNotice = "";
  state.rerunError = "";
  state.runsList = [];
  state.runsNextOffset = null;
  state.runsLoadingMore = false;
  state.runActionsFor = null;
  $("stage-rerun").hidden = true;
  if (state.timer) window.clearInterval(state.timer);
  state.timer = null;
  stopActivityTimer();
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
  return custom || folderLabel(run.handoff_path) || "工程交付";
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
  if (run.state === "success" || run.state === "failed") return "";
  if (run.state === "queued" || run.state === "running") return "运行结束前不能删除。";
  return "运行状态未知，暂不能删除。";
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
  const list = $("runs");
  try {
    const payload = await api("/api/runs?include_deleted=1&limit=30");
    state.runsList = Array.isArray(payload.runs) ? payload.runs : [];
    state.runsNextOffset = typeof payload.next_offset === "number" ? payload.next_offset : null;
    setError($("runs-error"), "");
    renderRunsList();
  } catch (error) {
    state.runsNextOffset = null;
    list.textContent = "";
    const item = document.createElement("li");
    item.className = "muted";
    item.textContent = `运行列表不可用：${error.message}`;
    list.append(item);
  }
}

// next_offset paging: a view is never declared empty from the first page alone.
async function loadMoreRuns() {
  if (state.runsLoadingMore || state.runsNextOffset === null) return;
  state.runsLoadingMore = true;
  renderRunsList();
  try {
    const payload = await api(`/api/runs?include_deleted=1&limit=30&offset=${state.runsNextOffset}`);
    state.runsList = state.runsList.concat(Array.isArray(payload.runs) ? payload.runs : []);
    state.runsNextOffset = typeof payload.next_offset === "number" ? payload.next_offset : null;
    setError($("runs-error"), "");
  } catch (error) {
    setError($("runs-error"), error.message || "加载更多失败，请重试。");
  } finally {
    state.runsLoadingMore = false;
    renderRunsList();
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
      ? "当前已加载记录中没有可显示的运行，可继续加载更多。"
      : state.runView === "deleted" ? "暂无已删除运行" : "暂无运行";
    list.append(empty);
  }
  for (const run of rows) list.append(buildRunItem(run));
  if (state.runsNextOffset !== null) {
    const footer = document.createElement("li");
    footer.className = "runs-footer";
    const more = ghostButton(state.runsLoadingMore ? "正在加载…" : "加载更多");
    more.disabled = state.runsLoadingMore;
    more.addEventListener("click", () => {
      void loadMoreRuns();
    });
    footer.append(more);
    list.append(footer);
  } else if (rows.length) {
    const footer = document.createElement("li");
    footer.className = "muted small runs-footer";
    footer.textContent = "已显示全部";
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
  if (run.resume_from_name_zh) {
    const chip = document.createElement("span");
    chip.className = "run-chip";
    chip.textContent = `从${run.resume_from_name_zh}重跑`;
    chip.title = `重跑来源：${run.parent_dag_run_id || "未记录"}`;
    heading.append(chip);
  }
  const meta = document.createElement("span");
  meta.className = "run-meta-line";
  const stateText = RUN_STATES[run.state] || run.state || "状态待确认";
  meta.textContent = `${[formatRunTime(run.started_at), stateText].filter(Boolean).join(" · ")}\n发起人：${run.user || "未记录"}`;
  open.append(heading, meta);
  open.title = [
    runDisplayTitle(run),
    folderLabel(run.handoff_path) || "工程文件夹",
    formatTime(run.started_at),
    run.dag_run_id,
  ].join("\n");
  open.addEventListener("click", () => selectRun(run.dag_run_id));
  box.append(open);
  if (run.deleted === true) {
    const tombstone = document.createElement("span");
    tombstone.className = "run-tombstone";
    tombstone.textContent =
      `已删除 · ${formatRunTime(run.deleted_at) || "时间未记录"} · 删除人：${run.deleted_by || "未记录"}`;
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
  trigger.setAttribute("aria-label", `操作：${runDisplayTitle(run)}`);
  trigger.setAttribute("aria-haspopup", "true");
  trigger.setAttribute("aria-expanded", "false");
  const pop = document.createElement("div");
  pop.className = "run-menu-pop";
  pop.setAttribute("role", "menu");
  pop.hidden = true;
  const close = (refocus) => {
    pop.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (refocus) trigger.focus();
  };
  const showMenu = () => {
    pop.textContent = "";
    const rename = ghostButton("重命名");
    rename.setAttribute("role", "menuitem");
    rename.addEventListener("click", showRename);
    pop.append(rename);
    if (run.deleted === true) {
      const restore = ghostButton("恢复");
      restore.setAttribute("role", "menuitem");
      restore.addEventListener("click", () => {
        close(false);
        void mutateRun(run, "POST");
      });
      pop.append(restore);
    } else {
      const remove = ghostButton("删除");
      remove.setAttribute("role", "menuitem");
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
    input.placeholder = folderLabel(run.handoff_path) || "工程交付";
    input.title = "留空并保存可恢复为工程文件夹名称；名称仅影响列表显示。";
    input.setAttribute("aria-label", `重命名「${runDisplayTitle(run)}」`);
    const buttons = document.createElement("div");
    buttons.className = "run-menu-buttons";
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "ghost";
    save.textContent = "保存";
    const cancel = ghostButton("取消");
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
    note.textContent = "从运行列表移除，可在「已删除」中恢复；模型、证据和 PR 保留。";
    const buttons = document.createElement("div");
    buttons.className = "run-menu-buttons";
    const confirm = ghostButton("移入已删除");
    const cancel = ghostButton("取消");
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
    setError($("runs-error"), error.message || "操作未完成，请重试。");
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
      reuse: stage.reuse || null,
      counts,
      passed: sum("passed"),
      executed: sum("executed"),
      total: sum("total"),
      boundary: (Array.isArray(stage.boundary) ? stage.boundary : []).map((row) =>
        reportRow(row, row.boundary === "output" ? "output" : "input")),
      independent: (Array.isArray(stage.independent) ? stage.independent : []).map((row) => reportRow(row, "independent")),
      files: Array.isArray(stage.files) ? stage.files : [],
      warnings: Array.isArray(stage.warnings) ? stage.warnings : [],
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

//: Recorded observations render as bounded warning rows: exact recorded values
//: (including authored zeros), the qualification/publication blocker kind, and no
//: pass bookkeeping or per-item human confirmation.
const WARNING_ROW_LIMIT = 8;
const WARNING_VALUE_LIMIT = 8;
const WARNING_VALUE_KEYS = [
  "limits",
  "components",
  "links",
  "joint",
  "mate",
  "mate_type",
  "scope",
  "mass_kg",
  "masses",
  "qualification_blocker",
  "publication_blocker",
];

function warningValueText(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    const shown = value.slice(0, WARNING_VALUE_LIMIT).map((item) => warningValueText(item));
    return `${shown.join("、")}${value.length > shown.length ? ` 等 ${value.length} 项` : ""}`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value).slice(0, WARNING_VALUE_LIMIT);
    const shown = entries.map(([key, item]) => `${key}=${warningValueText(item)}`);
    return `${shown.join("；")}${Object.keys(value).length > entries.length ? " …" : ""}`;
  }
  return String(value);
}

function stageWarningRow(row) {
  const item = document.createElement("div");
  item.className = "warning-row";
  const head = document.createElement("div");
  head.className = "warning-head";
  const label = document.createElement("span");
  label.className = "warning-label";
  label.textContent = row.label_zh || row.code || "警告";
  const chip = document.createElement("span");
  chip.className = "chip warning";
  chip.textContent = row.state_zh || "警告";
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
  if (row.qualification_blocker) blockers.push(`限定资格阻断：${row.qualification_blocker}`);
  if (row.publication_blocker) blockers.push(`发布阻断：${row.publication_blocker}`);
  if (blockers.length) {
    const line = document.createElement("p");
    line.className = "warning-blocker";
    line.textContent = blockers.join("；");
    item.append(line);
  }
  const keys = WARNING_VALUE_KEYS.filter((key) => row[key] !== undefined && row[key] !== null);
  const residual = row.residual && typeof row.residual === "object" ? Object.entries(row.residual) : [];
  if (keys.length || residual.length) {
    const details = expandable(item, "记录值");
    const list = document.createElement("div");
    list.className = "warning-values";
    for (const key of keys) {
      const line = document.createElement("p");
      line.textContent = `${key}：${warningValueText(row[key])}`;
      list.append(line);
    }
    for (const [key, value] of residual) {
      const line = document.createElement("p");
      line.textContent = `residual.${key}：${warningValueText(value)}`;
      list.append(line);
    }
    details.append(list);
  }
  return item;
}

function renderStageWarnings(root, stage, note) {
  const rows = Array.isArray(stage.warnings) ? stage.warnings : [];
  if (!rows.length) return;
  const group = document.createElement("div");
  group.className = "check-group warning-group";
  const title = document.createElement("h4");
  title.textContent = `警告（${rows.length}）`;
  group.append(title);
  const hint = document.createElement("p");
  hint.className = "stage-note muted small";
  hint.textContent = note || "记录项，不阻断采集与生成；不计为通过，也不作为验收或发布依据。";
  group.append(hint);
  for (const row of rows.slice(0, WARNING_ROW_LIMIT)) group.append(stageWarningRow(row));
  if (rows.length > WARNING_ROW_LIMIT) {
    const more = document.createElement("p");
    more.className = "muted small";
    more.textContent = `另有 ${rows.length - WARNING_ROW_LIMIT} 项警告，详见报告记录。`;
    group.append(more);
  }
  root.append(group);
}

function buildFailureCard(failure, report) {
  const card = document.createElement("div");
  card.className = "fail-card";
  const title = document.createElement("strong");
  title.textContent = failure.title_zh || "运行失败";
  card.append(title);
  const stage =
    report && Array.isArray(report.stages) ? report.stages.find((entry) => entry && entry.id === failure.stage) : null;
  const failedChecks = stage
    ? [...(Array.isArray(stage.boundary) ? stage.boundary : []), ...(Array.isArray(stage.independent) ? stage.independent : [])]
        .filter((row) => row && row.state === "failed")
    : [];
  const warningCount = stage && Number.isFinite(stage.counts && stage.counts.warnings) ? stage.counts.warnings : 0;
  if (failure.meaning_zh && !(failure.kind === "unknown" && failedChecks.length)) {
    const meaning = document.createElement("p");
    meaning.textContent = failure.meaning_zh;
    card.append(meaning);
  }
  if (failedChecks.length) {
    const heading = document.createElement("p");
    heading.textContent = `未通过的自动检查（${failedChecks.length} 项）：`;
    card.append(heading);
    for (const row of failedChecks.slice(0, 5)) {
      const line = document.createElement("p");
      line.className = "muted small";
      const message = row.summary && row.summary.actual ? row.summary.actual : "未通过";
      line.textContent = `${row.label_zh || row.id || "检查"}：${message}`;
      card.append(line);
    }
    if (failedChecks.length > 5) {
      const more = document.createElement("p");
      more.className = "muted small";
      more.textContent = `另有 ${failedChecks.length - 5} 项未通过检查，逐项证据见下方阶段检查。`;
      card.append(more);
    }
    if (failedChecks.some((row) => String(row.id || "").startsWith("source."))) {
      const hint = document.createElement("p");
      hint.className = "muted small";
      hint.textContent =
        "来源证据检查未通过；请依据失败检查及原始证据区分输入缺失、来源不一致或解析问题。";
      card.append(hint);
    } else {
      const detail = document.createElement("p");
      detail.className = "muted small";
      detail.textContent = "逐项证据见下方阶段检查的“原始证据”。";
      card.append(detail);
    }
  }
  if (warningCount > 0) {
    const warnings = document.createElement("p");
    warnings.className = "muted small";
    warnings.textContent = `该阶段另有 ${warningCount} 项记录警告：警告不计为通过，也不作为验收或发布依据。`;
    card.append(warnings);
  }
  if (failure.kind === "definition" || failure.kind === "runtime") {
    const kind = document.createElement("p");
    kind.className = "muted small";
    kind.textContent = failure.kind === "definition" ? "分类：工程定义检查" : "分类：运行时问题";
    card.append(kind);
  }
  if (Array.isArray(failure.finding_counts) && failure.finding_counts.length) {
    const groups = document.createElement("p");
    groups.className = "muted small";
    const shown = failure.finding_counts.slice(0, 8);
    groups.textContent =
      "分类汇总：" +
      shown.map((group) => `${group.label_zh || group.code}×${group.count}`).join("、") +
      (failure.finding_counts.length > shown.length ? "…" : "") +
      (failure.finding_total ? `（共 ${failure.finding_total} 项，逐项见下方问题清单，可展开查看）` : "");
    card.append(groups);
  }
  if (Array.isArray(failure.finding_counts) && failure.finding_counts.length) {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent = "以上计数为平台报告发现与受影响实例，不代表 CAD 缺陷；逐项证据见下方问题清单（默认折叠）。";
    card.append(note);
    if (failure.finding_counts.some((group) => group && group.code === "discovery.link_name_missing")) {
      const hint = document.createElement("p");
      hint.className = "muted small";
      hint.textContent =
        "未识别刚体坐标系（discovery.link_name_missing）：记录表示刚体归属与命名尚未证明，并不代表 CAD 中不存在坐标系；" +
        "处置方向为补齐交付定义与坐标系归属（由结构交付侧在原生工程中完成），" +
        "不需要为每个供应商内部叶件单独添加坐标系。";
      card.append(hint);
      const samples = failure.finding_samples && Array.isArray(failure.finding_samples["discovery.link_name_missing"])
        ? failure.finding_samples["discovery.link_name_missing"]
        : [];
      const objects = samples.map((row) => row && row.object).filter(Boolean).slice(0, 2);
      if (objects.length) {
        const sample = document.createElement("p");
        sample.className = "muted small";
        sample.textContent = `示例对象：${objects.join("、")}（完整清单见下方问题清单）`;
        card.append(sample);
      }
    }
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
  if (Number.isFinite(stage.counts.warnings) && stage.counts.warnings > 0) {
    pieces.push(`警告：${stage.counts.warnings} 项`);
  }
  counts.textContent = pieces.join("　•　") || "无检查记录";
  summary.append(counts);
  if (stage.at) {
    const at = document.createElement("p");
    at.className = "muted small";
    at.textContent = `记录时间：${formatTime(stage.at)}`;
    summary.append(at);
  }
  if (stage.reuse && stage.reuse.reused === true) {
    const reused = document.createElement("p");
    reused.className = "muted small";
    reused.textContent = "复用已验证结果；上方时间为原检查时间。";
    summary.append(reused);
    appendEvidence(summary, "复用来源", stage.reuse);
  }
  root.append(summary);

  // Lightweight per-stage preview: a data-bound summary with its necessity, so a reviewer
  // reads the stage's outcome without expanding the raw evidence.
  const run = state.lastRun && state.lastRun.dag_run_id === state.dagRunId ? state.lastRun : null;
  const quick = stageSummaryRows(stage.id, {
    files: state.previewFiles,
    stage,
    quality: state.previewData ? state.previewData.quality : null,
    pr: run ? run.pr : null,
    subjectSha256: state.previewSubject,
  });
  if (quick.rows.length) {
    const block = document.createElement("div");
    block.className = "stage-quick-summary";
    const title = document.createElement("h4");
    title.textContent = "轻量摘要";
    block.append(title);
    for (const row of quick.rows) {
      const line = document.createElement("p");
      line.className = "muted small";
      const label = document.createElement("strong");
      label.textContent = `${row.label}：`;
      const value = document.createElement("code");
      value.textContent = row.value;
      line.append(label, value);
      block.append(line);
    }
    if (quick.necessity) {
      const note = document.createElement("p");
      note.className = "muted small";
      note.textContent = `必要性：${quick.necessity}`;
      block.append(note);
    }
    root.append(block);
  }

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

  const reportWarnings = state.lastRun && state.lastRun.report ? state.lastRun.report.warnings : null;
  renderStageWarnings(
    root,
    stage,
    reportWarnings && typeof reportWarnings.note_zh === "string" ? reportWarnings.note_zh : "",
  );

  const fileRows = (stage.files || []).map((file) => ({
    label: file.label_zh || file.path || "文件",
    boundary: file.boundary === "output" ? "输出" : file.boundary === "input" ? "输入" : "",
    path: file.path || "",
    availability: file.availability || "",
    count: Number.isFinite(file.files) ? file.files : null,
  }));
  if (fileRows.length) {
    const details = expandable(root, `输入与输出记录（${fileRows.length} 项）`);
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
    row.append("尚未自动覆盖：");
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
    state.viewer.resize?.();
    state.viewer.frame?.();
  }
  const button = $("preview-collapse");
  button.hidden = !expansion.collapse_control;
  button.textContent = expansion.expanded ? "收起预览" : "展开预览";
  button.setAttribute("aria-expanded", expansion.expanded ? "true" : "false");
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
    if (!response.ok) return null;
    return await response.json();
  } catch (error) {
    if (signal?.aborted) throw error;
    return null;
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
  const data = {
    robot: await loadPreviewArtifactJson(artifactUrl, preview.files, "model/robot.json", signal),
    tool: await loadPreviewArtifactJson(artifactUrl, preview.files, "tool.json", signal),
    quality: await loadPreviewArtifactJson(artifactUrl, preview.files, "reports/quality.json", signal),
  };
  state.previewData = data;
  const identity = summaryIdentity({ runId: state.dagRunId, subjectSha256: preview.subject_sha256 });
  const info = $("preview-info");
  info.textContent = "";
  for (const row of infoPanelRows({
    runId: identity.run_id,
    subjectSha256: identity.subject_sha256,
    purpose: preview.purpose,
    tool: data.tool,
    qualitySchema: data.quality ? data.quality.schema_version : null,
    qualificationStates: data.quality ? data.quality.qualifications : null,
  })) {
    const key = document.createElement("dt");
    key.textContent = row.key;
    const value = document.createElement("dd");
    value.textContent = row.value || "未记录";
    info.append(key, value);
  }
  const limits = jointRows(records ? records.joints : []);
  const fmt = (value, digits) => (Number.isFinite(value) ? value.toFixed(digits) : "未提供");
  const modelJoints = new Map(((data.robot && data.robot.joints) || []).map((joint) => [joint.name, joint]));
  const windowLabel = "检查/预览分析窗口，非机械限位";
  const absentDrive = "未提供（未建立驱动能力）";
  const sourceLabel = (source) =>
    source === "policy_preview_window" || source === "declared_preview_window"
      ? `${source}（${windowLabel}）`
      : source || "未记录";
  fillPanelTable(
    $("panel-limits"),
    ["关节", "类型", "下限", "上限", "力矩", "速度", "轴 (xyz)", "限位来源"],
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
  $("limits-title").textContent = `限位 / 轴读数（${limits.length}）`;
  const links = linkRows(records ? records.links : []);
  fillPanelTable(
    $("panel-links"),
    ["链接", "质量 kg", "质心 m", "惯量 (kg·m²)"],
    links.map((row) => [
      row.name,
      row.mass_kg === null ? "未提供" : row.mass_kg.toFixed(6),
      row.com_m ? row.com_m.map((value) => fmt(value, 4)).join(", ") : null,
      row.inertia_kg_m2
        ? `ixx ${fmt(row.inertia_kg_m2.ixx, 6)} · iyy ${fmt(row.inertia_kg_m2.iyy, 6)} · izz ${fmt(row.inertia_kg_m2.izz, 6)}`
        : null,
    ]),
  );
  $("links-title").textContent = `链接/刚体质量惯量（${links.length}）`;
  const frames = frameRows(data.robot ? data.robot.frames : []);
  const closure = closureRows(data.quality ? data.quality.checks : []);
  fillPanelTable(
    $("panel-frames"),
    ["坐标系 / 接口", "父级", "xyz (m)", "rpy (rad)"],
    frames.map((row) => [
      row.name,
      row.parent,
      row.xyz ? row.xyz.map((value) => fmt(value, 4)).join(", ") : null,
      row.rpy ? row.rpy.map((value) => fmt(value, 4)).join(", ") : null,
    ]),
  );
  fillPanelTable(
    $("panel-closure"),
    ["检查", "状态"],
    closure.map((row) => [row.id, row.state]),
  );
  $("frames-title").textContent = `坐标系 / 接口位姿与物理闭合（${frames.length} / ${closure.length}）`;
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
    if (run.state === "success" || run.state === "failed") return;
    renderActivity(run);
  }, 1000);
}

function buildActivityCard(view) {
  const card = document.createElement("section");
  card.className = `activity-card state-${view.state}`;
  card.setAttribute("aria-label", "实时活动");
  const head = document.createElement("div");
  head.className = "activity-head";
  const dot = document.createElement("span");
  dot.className = `dot ${view.state === "busy" || view.state === "queued" ? "run" : "idle"}`;
  const title = document.createElement("strong");
  title.textContent = view.final ? "最后活动" : "实时活动";
  const chip = document.createElement("span");
  chip.className = `chip ${view.state === "busy" ? "running" : view.state === "queued" ? "queued" : "blocked"}`;
  chip.textContent = view.stateText;
  head.append(dot, title, chip);
  if (view.stageText) {
    const stage = document.createElement("span");
    stage.className = "muted small";
    stage.textContent = `阶段：${view.stageText}`;
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
    object.textContent = view.final ? `相关对象：${view.objectText}` : `当前对象：${view.objectText}`;
    card.append(object);
  }
  const metaParts = view.final
    ? [view.freshnessText].filter(Boolean)
    : [view.elapsedText, view.countsText, view.freshnessText].filter(Boolean);
  if (metaParts.length) {
    const meta = document.createElement("p");
    meta.className = view.stale ? "activity-meta muted small stale" : "activity-meta muted small";
    meta.textContent = metaParts.join("　•　");
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
  const overview = $("activity-overview");
  const stageHost = $("activity-stage");
  if (!overview || !stageHost) return;
  overview.textContent = "";
  stageHost.textContent = "";
  if (!run || run.dag_run_id !== state.dagRunId) return;
  const view = activityView(run.activity, { runState: run.state, nowMs: Date.now() });
  if (view.visible) overview.append(buildActivityCard(view));
  const selected = Array.isArray(state.stages) && Number.isInteger(state.selectedStage)
    ? state.stages[state.selectedStage] || null
    : null;
  const mode = activityPlacement(view, {
    stageId: selected ? selected.id : "",
    stageRunning: Boolean(selected && selected.state === "running"),
    stageFailed: Boolean(selected && selected.state === "failed"),
  });
  if (mode === "live" || mode === "final") {
    stageHost.append(buildActivityCard(view));
  } else if (mode === "note") {
    const note = document.createElement("p");
    note.className = "activity-note muted small";
    note.textContent = view.state === "busy" && view.stageText && selected && view.stage !== selected.id
      ? `当前活动阶段为「${view.stageText}」，此阶段暂无详细进度。`
      : "此阶段暂无详细进度可用。";
    stageHost.append(note);
  }
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
    const warningCount = Number.isFinite(stage.counts.warnings) ? stage.counts.warnings : 0;
    const tooltip = `${stage.nameZh} · ${stage.stateZh}${warningCount > 0 ? ` · 警告 ${warningCount}` : ""}${
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
      if (stageEntersPreview(stage.id) && state.previewSubject) {
        // Entering the generate-URDF block expands the interactive preview again; the
        // collapse control stays available for every other stage.
        state.previewCollapsed = false;
        try {
          localStorage.setItem(previewCollapseKey(runId), "0");
        } catch {
          // best-effort persistence
        }
        applyPreviewExpansion();
      }
      if (state.lastRun) renderRun(state.lastRun);
      selectInspectTab("stage");
    });
    stepper.append(step);
  });
}

function renderRerunPanel(run) {
  const host = $("stage-rerun");
  if (!host) return;
  host.textContent = "";
  const rows = Array.isArray(run.stage_reruns) ? run.stage_reruns : null;
  const stage = state.stages[state.selectedStage];
  if (!stage) {
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

  const line = document.createElement("div");
  line.className = "rerun-row";
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = busy ? "正在创建尝试…" : "从此步骤重新运行";
  button.disabled = busy || !canManage || !eligible || deleted;
  button.addEventListener("click", () => {
    void rerunFromStage(stage.id);
  });
  line.append(button);

  const note = document.createElement("span");
  note.className = "muted small";
  if (state.rerunNotice) {
    note.textContent = state.rerunNotice;
  } else if (deleted) {
    note.textContent = "此运行已删除：请先恢复后再重跑。";
  } else if (!canManage) {
    note.textContent = "仅发起人或平台管理员可重新运行。";
  } else if (row && row.reason_zh) {
    // Canonical backend reason shown verbatim whenever the stage is not eligible.
    note.textContent = row.reason_zh;
  } else if (eligible) {
    note.textContent = "将重算该步骤及其后续阶段；原始运行与证据保留，重跑为新关联运行。";
  } else {
    note.textContent = "暂时无法确认此步骤的重跑条件，请稍后刷新。";
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
      for (const [key, label] of [["inputs", "输入"], ["tool", "工具"], ["receipt", "回执"]]) {
        const value = prereq[key];
        if (typeof value === "string" && value) parts.push(`${label}〈${PREREQ_STATE_ZH[value] || value}〉`);
      }
      if (parts.length) pieces.push(`前置条件：${parts.join(" · ")}`);
    }
    const recomputes = Array.isArray(row.recomputes) ? row.recomputes : [];
    if (recomputes.length) {
      pieces.push(`将重算：${recomputes.map((id) => rerunStageName(rows, id)).join(" → ")}`);
    }
    if (prereq && prereq.earliest_required && prereq.earliest_required !== stage.id) {
      pieces.push(`最早可重新运行：${rerunStageName(rows, prereq.earliest_required)}`);
    }
    if (pieces.length) {
      meta.textContent = pieces.join("；");
      host.append(meta);
    }
    const retains = Array.isArray(row.retains) ? row.retains : [];
    if (retains.length) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `复用既有输入/结果（${retains.length} 项）`;
      details.append(summary);
      const list = document.createElement("div");
      list.className = "rerun-retains";
      for (const item of retains) {
        if (!item || typeof item !== "object") continue;
        const entry = document.createElement("p");
        entry.textContent = [item.label_zh || item.path || "", item.availability || ""].filter(Boolean).join(" · ");
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
  if (!run || !runId || state.rerunningRunId !== null || run.can_manage !== true || !stageId) return;
  state.rerunningRunId = runId;
  state.rerunNotice = "";
  state.rerunError = "";
  renderRerunPanel(run);
  try {
    const payload = await api(`/api/runs/${encodeURIComponent(runId)}/attempts`, {
      method: "POST",
      body: { stage: stageId },
    });
    const nextRunId = String(payload.dag_run_id || "");
    const stageToShow = String(payload.resume_from || stageId);
    await refreshRuns();
    if (state.dagRunId !== runId) return; // the operator moved on; the list is refreshed
    if (!nextRunId) {
      const name = payload.resume_from_name_zh || stageId;
      state.rerunNotice = `已创建新关联运行，自「${name}」重新执行；原始运行与证据保留。`;
      if (!state.timer) {
        state.timer = window.setInterval(() => {
          void poll();
        }, 3000);
      }
      await poll();
      return;
    }
    try {
      localStorage.setItem(stageSelectionKey(nextRunId), stageToShow);
    } catch {
      // remembering the requested stage is best-effort
    }
    await selectRun(nextRunId);
  } catch (error) {
    if (error.status === 401) {
      clearSession();
      setError($("login-error"), error.message);
      return;
    }
    if (state.dagRunId === runId) {
      const detail = error.payload && typeof error.payload === "object" ? error.payload : {};
      state.rerunError = detail.reason_zh || error.message || "重新运行未受理。";
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

function renderRun(run) {
  const runId = String(run.dag_run_id || "");
  const listEntry = (state.runsList || []).find((item) => item && item.dag_run_id === runId) || null;
  const deleted = resolveDeleted(run, listEntry);
  const customTitle = resolveTitle(run, listEntry);
  $("run-heading").textContent = customTitle || folderLabel(run.handoff_path) || "工程文件夹";
  $("run-state-label").textContent = RUN_STATES[run.state] || run.state || "状态待确认";
  $("run-dot").className = `dot ${runDotState(run.state)}`;
  $("run-initiator").textContent = `发起人：${run.operator || run.user || "未记录"}`;
  const deletedTag = $("run-deleted");
  const deletedAt = run.deleted_at || (listEntry && listEntry.deleted_at) || null;
  const deletedBy = run.deleted_by || (listEntry && listEntry.deleted_by) || null;
  deletedTag.textContent = deleted
    ? `已删除 · ${formatRunTime(deletedAt) || "时间未记录"} · 删除人：${deletedBy || "未记录"}`
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
  // Parent lineage prefers the detail payload; the cached row is only a fallback, so a parent
  // link never depends on that row being on a loaded page.
  const parentId = run.parent_dag_run_id || (listEntry && listEntry.parent_dag_run_id) || null;
  if (parentId) {
    const parentStage = run.resume_from_name_zh || (listEntry && listEntry.resume_from_name_zh) || "上游阶段";
    origin.append(`自「${parentStage}」重新运行 · 来源 `);
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
    ["工程文件夹", folderLabel(run.handoff_path) || "—"],
    ["状态", RUN_STATES[run.state] || run.state || "—"],
    ["开始时间", formatTime(run.started_at)],
    ["结束时间", formatTime(run.ended_at)],
  ];
  if (typeof run.main_assembly === "string" && run.main_assembly) {
    rows.splice(1, 0, ["主装配", run.main_assembly]);
  }
  rows.splice(1, 0, ["用途", purposeLabel(run.purpose)]);
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
    failureCard.append(buildFailureCard(report.failure, report));
    failureCard.hidden = false;
  } else {
    failureCard.hidden = true;
  }
  if (state.tabsInitializedFor !== runId) {
    state.tabsInitializedFor = runId;
    selectInspectTab(run.state === "failed" ? "stage" : "overview");
  }

  const retry = run.retry || {};
  const canRetry = !deleted && run.can_manage === true && retry.eligible === true;
  const retryReason = typeof retry.reason === "string" ?
    RETRY_REASONS[retry.reason] || "暂不可重试，请查看检查结果。" : "";
  $("retry-panel").hidden = !deleted && !canRetry && !(run.state === "failed" && retryReason);
  $("retry-button").hidden = !canRetry;
  $("retry-button").disabled = state.retryingRunId !== null;
  $("retry-button").textContent = state.retryingRunId === runId ? "正在重试…" : "继续原作业";
  $("retry-note").textContent = deleted ?
    RETRY_REASONS.deleted :
    retry.eligible === true && run.can_manage !== true ?
      "该运行可由发起人或平台管理员重试。" : retryReason;

  const progress = $("task-progress");
  progress.textContent = "";
  for (const task of run.tasks || []) {
    const chip = document.createElement("span");
    chip.className = `chip ${task.state || ""}`.trim();
    chip.textContent = `${TASK_LABELS[task.task_id] || task.task_id}：${TASK_STATES[task.state] || task.state || "—"}`;
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
        text.textContent = scope.scope;
        item.append(text);
      }
      if (scope.automatic_exclusion) {
        const text = document.createElement("p");
        text.className = "muted small";
        text.textContent = scope.automatic_exclusion;
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
  // The automatically retained structural-team report stays directly downloadable on runs
  // without a verified delivery (failed Windows or Linux runs); no URL hunting required.
  const retainedReports = Array.isArray(run.structural_report) ? run.structural_report : [];
  if (retainedReports.length) {
    const item = document.createElement("li");
    item.className = "report-download";
    const label = document.createElement("span");
    label.textContent = "结构团队报告（自动生成）：";
    item.append(label, " ");
    for (const entry of retainedReports) {
      const link = document.createElement("a");
      link.href = String(entry.url || "");
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = entry.label_zh || entry.path || "结构团队报告";
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
    ((failure && failure.finding_counts) || []).map((group) => [group.code, group.label_zh || group.code]),
  );
  const findingNameValue = (finding) => {
    const evidence = finding && finding.evidence && typeof finding.evidence === "object" ? finding.evidence : {};
    const detail = evidence.detail && typeof evidence.detail === "object" ? evidence.detail : {};
    const value = typeof detail.name === "string" ? detail.name.trim() : "";
    return value || null;
  };
  const renderFinding = (finding) => {
    const item = document.createElement("li");
    if (finding.severity && finding.severity !== "error") item.className = "warn";
    const message = document.createElement("div");
    // A missing robot name is not a malformed name; keep the two cases distinct.
    message.textContent =
      finding.id === "discovery.robot_name_invalid" && findingNameValue(finding) === null
        ? "未读取到机器人名称（detail.name 为空）。"
        : finding.message || "未提供说明";
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
    return item;
  };
  if (!visibleFindings.length) {
    const item = document.createElement("li");
    item.className = "muted";
    item.textContent = "暂无问题";
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
    (a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]),
  );
  orderedGroups.forEach(([code, items], index) => {
    const outer = document.createElement("li");
    const section = document.createElement("details");
    const header = document.createElement("summary");
    let groupLabel = codeLabels.get(code) || code;
    if (code === "discovery.robot_name_invalid") {
      const missing = items.filter((finding) => findingNameValue(finding) === null).length;
      if (missing === items.length) groupLabel = "缺少机器人名称";
    }
    header.textContent = `${groupLabel}（${items.length} 项）`;
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
    if (index === 0 && items.length <= 12) {
      section.open = true;
      populate();
    }
    section.append(header, nested);
    outer.append(section);
    findings.append(outer);
  });

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
  renderRerunPanel(run);
  updatePreviewLayout();
}

function renderPreviewDiagnostic(preview) {
  const node = $("preview-diagnostic");
  if (preview.verified !== false) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  const lines = ["未通过工程校验：该交付仅供诊断，禁止发布。"];
  const checks = Array.isArray(preview.failing_checks) ? preview.failing_checks : [];
  const checksTotal = Number.isInteger(preview.failing_checks_total) ? preview.failing_checks_total : checks.length;
  if (checks.length) {
    const shown = checks.slice(0, 8).map((check) => `${check.id}（${check.message || "检查未通过"}）`);
    lines.push(`未通过检查：${shown.join("、")}${checksTotal > shown.length ? ` 等 ${checksTotal} 项` : ""}`);
  }
  const warnings = Array.isArray(preview.warnings) ? preview.warnings : [];
  const warningsTotal = Number.isInteger(preview.warnings_total) ? preview.warnings_total : warnings.length;
  if (warnings.length) {
    const shown = warnings.slice(0, 6).map((item) => `${item.code || "警告"}${item.object ? `（${item.object}）` : ""}`);
    lines.push(`警告：${shown.join("、")}${warningsTotal > shown.length ? ` 等 ${warningsTotal} 条` : ""}`);
  }
  node.textContent = lines.join("\n");
  node.hidden = false;
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
    renderPreviewDiagnostic(preview);
    note.hidden = true;
    setError($("preview-note"), "");
    state.viewer = ensureSingleViewer(state, () => createViewer($("viewer"))).viewer;
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
    $("joints-title").textContent = `关节滑杆（${loaded.joints.length}）`;
    state.previewSubject = preview.subject_sha256;
    state.previewFiles = preview.files || {};
    if (state.previewCollapseFor !== dagRunId) {
      state.previewCollapseFor = dagRunId;
      try {
        state.previewCollapsed = localStorage.getItem(previewCollapseKey(dagRunId)) === "1";
      } catch {
        state.previewCollapsed = false;
      }
    }
    await renderPreviewPanels(preview, loaded.records, artifactUrl, request.signal);
    if (request.signal.aborted || state.dagRunId !== dagRunId) return;
    if (state.lastRun) renderRun(state.lastRun);
    $("viewer-card").hidden = false;
    $("viewer-placeholder").hidden = true;
    applyPreviewExpansion();
    state.viewer.frame();
    updatePreviewLayout();
  } catch (error) {
    if (request.signal.aborted || state.dagRunId !== dagRunId) return;
    if (state.viewer) disposeRobot(state.viewer);
    state.controls = null;
    state.previewSubject = null;
    state.previewFiles = null;
    state.previewData = null;
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
    const diagnosable = !verified && run.state === "failed";
    if (!verified && !diagnosable) clearPreview();
    const previous = state.lastRun;
    state.lastRun = run;
    renderRun(run);
    if (verified || diagnosable) {
      await loadPreview(dagRunId);
    }
    if (state.dagRunId !== dagRunId) return;
    const jobDone = run.job && (run.job.status === "passed" || run.job.status === "failed");
    if ((run.state === "success" || run.state === "failed") && jobDone && (!verified || state.previewSubject)) {
      if (state.timer) window.clearInterval(state.timer);
      state.timer = null;
      stopActivityTimer();
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
      stopActivityTimer();
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
  stopActivityTimer();
  clearPreview();
  state.dagRunId = dagRunId;
  state.lastRun = null;
  state.selectedStage = null;
  state.tabsInitializedFor = null;
  state.rerunNotice = "";
  state.rerunError = "";
  $("retry-panel").hidden = true;
  setError($("run-error-detail"), "");
  setError($("retry-error"), "");
  showRunView();
  renderRunsList();
  // A run opened from a stale list (deep link, another tab, freshly queued) still lands in the
  // sidebar: refresh once when the selected id is not part of the cached page.
  const knownRun = (state.runsList || []).some((item) => item && item.dag_run_id === dagRunId);
  if (!knownRun) void refreshRuns();
  if (state.dagRunId !== dagRunId) return;
  state.timer = window.setInterval(() => {
    void poll();
  }, 3000);
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
    if (!state.timer) state.timer = window.setInterval(() => { void poll(); }, 3000);
    startActivityTimer();
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

  $("runs-view-active").addEventListener("click", () => setRunView("active"));
  $("runs-view-deleted").addEventListener("click", () => setRunView("deleted"));
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
      resetAssemblyRow();
      $("start-button").disabled = true;
      return;
    }
    state.folderPick = picked;
    summary.hidden = false;
    summary.textContent = `${picked.top} · ${picked.count} 个文件 · ${formatBytes(picked.bytes)}`;
    setError($("run-error"), "");
    applyAssemblies(picked);
    loadTargets(picked);
    updateStartEnabled();
  });

  $("assembly-select").addEventListener("change", () => {
    state.assemblyChoice = $("assembly-select").value || null;
    updateStartEnabled();
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
    if (!state.assemblyChoice || !picked.assemblies.includes(state.assemblyChoice)) {
      setError($("run-error"), "请先选择主装配。");
      return;
    }
    const start = $("start-button");
    const status = $("upload-status");
    state.uploading = true;
    start.disabled = true;
    $("choose-button").disabled = true;
    $("folder-input").disabled = true;
    $("assembly-select").disabled = true;
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
      $("assembly-select").disabled = false;
      $("cancel-button").hidden = true;
      if (state.folderPick && !state.blockedPick) {
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
