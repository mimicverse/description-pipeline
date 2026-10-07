// Operator page logic. All Airflow and Windows endpoint credentials stay on the server; this
// module only talks to the portal's own JSON API and the digest-verified artifact routes.
import { buildJointControls, createViewer, loadRobot } from "/static/viewer.js";

const state = {
  csrf: null,
  user: null,
  dagRunId: null,
  previewSubject: null,
  viewer: null,
  controls: null,
  timer: null,
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
};
const AUTOMATIC_STATES = {
  passed: "自动校验通过",
  failed: "自动校验失败",
  unverified: "未通过独立校验",
  pending: "等待自动检查",
  queued: "排队中",
  running: "检查中",
};

const $ = (id) => document.getElementById(id);

async function api(path, { method = "GET", body } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (state.csrf && method !== "GET") headers["X-CSRF-Token"] = state.csrf;
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
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

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function showSession(session) {
  state.user = session.user;
  state.csrf = session.csrf_token;
  $("login-card").hidden = true;
  $("workspace").hidden = false;
  $("account").hidden = false;
  $("account-user").textContent = session.user;
}

function clearSession() {
  state.csrf = null;
  state.user = null;
  state.dagRunId = null;
  if (state.timer) window.clearInterval(state.timer);
  state.timer = null;
  $("workspace").hidden = true;
  $("account").hidden = true;
  $("login-card").hidden = false;
  $("detail-card").hidden = true;
  $("viewer-card").hidden = true;
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
      button.textContent = `${run.dag_run_id} · ${run.handoff_path}`;
      button.addEventListener("click", () => selectRun(run.dag_run_id));
      item.append(button);
      list.append(item);
    }
  } catch (error) {
    list.textContent = `运行列表不可用：${error.message}`;
  }
}

function renderRun(run) {
  const meta = $("run-meta");
  meta.textContent = "";
  const rows = [
    ["运行标识", run.dag_run_id],
    ["工程文件夹", run.handoff_path || "—"],
    ["Airflow 状态", RUN_STATES[run.state] || run.state || "—"],
    ["开始时间", formatTime(run.started_at)],
    ["结束时间", formatTime(run.ended_at)],
  ];
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    meta.append(dt, dd);
  }

  const progress = $("task-progress");
  progress.textContent = "";
  for (const task of run.tasks || []) {
    const chip = document.createElement("span");
    chip.className = `chip ${task.state || ""}`.trim();
    chip.textContent = `${task.task_id}：${TASK_STATES[task.state] || task.state || "—"}`;
    progress.append(chip);
  }

  const stages = $("stages");
  stages.textContent = "";
  const stageRows = run.stages || [];
  if (!stageRows.length) {
    const item = document.createElement("li");
    item.className = "muted";
    item.textContent = run.job ? "等待阶段事件" : "CAD 作业尚未开始";
    stages.append(item);
  }
  for (const stage of stageRows) {
    const item = document.createElement("li");
    item.textContent = `${stage.stage || "阶段"} · ${stage.state || ""} · ${formatTime(stage.at)}`;
    stages.append(item);
  }

  const automatic = $("automatic");
  automatic.textContent = "";
  const automaticState = (run.automatic && run.automatic.state) || "pending";
  const automaticKind = automaticState === "passed" ? "ok" : automaticState === "failed" || automaticState === "unverified" ? "bad" : "pending";
  automatic.append(badge(AUTOMATIC_STATES[automaticState] || automaticState, automaticKind));
  if (run.automatic && run.automatic.message) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = run.automatic.message;
    automatic.append(note);
  }
  for (const check of (run.automatic && run.automatic.checks) || []) {
    const chip = document.createElement("span");
    chip.className = `chip ${check.passed === false ? "failed" : check.passed === true ? "success" : ""}`.trim();
    chip.textContent = `${check.id || "检查"}：${check.passed === false ? "未通过" : check.passed === true ? "通过" : "未报告"}`;
    automatic.append(chip);
  }

  const confirmations = $("confirmations");
  confirmations.textContent = "";
  const coverage = run.coverage || {};
  const structure = coverage.structure || {};
  const identity = document.createElement("p");
  identity.className = "muted";
  identity.textContent = `结构身份：${structure.hardware_id || "待解析"} · 版本 ${structure.revision || "—"} · 交付摘要 ${
    String(structure.subject_sha256 || "").slice(0, 12) || "—"
  }`;
  confirmations.append(identity, badge("工程确认：待确认", "pending"));
  for (const item of (coverage.engineering && coverage.engineering.items) || []) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = `${item.id}：待确认`;
    confirmations.append(chip);
  }
  if (coverage.engineering && coverage.engineering.message) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = coverage.engineering.message;
    confirmations.append(note);
  }
  for (const text of (coverage.automatic && coverage.automatic.unsupported) || []) {
    const chip = document.createElement("span");
    chip.className = "chip failed";
    chip.textContent = `未自动覆盖：${text}`;
    automatic.append(chip);
  }

  const findings = $("findings");
  findings.textContent = "";
  if (!(run.findings || []).length) {
    const item = document.createElement("li");
    item.className = "muted";
    item.textContent = "暂无问题";
    findings.append(item);
  }
  for (const finding of run.findings || []) {
    const item = document.createElement("li");
    if (finding.severity && finding.severity !== "error") item.className = "warn";
    const message = document.createElement("div");
    message.textContent = finding.message || "未提供说明";
    const context = document.createElement("div");
    context.className = "object";
    context.textContent = [finding.id, finding.stage, finding.object].filter(Boolean).join(" · ") || "—";
    item.append(message, context);
    if (finding.evidence && Object.keys(finding.evidence).length) {
      const evidence = document.createElement("div");
      evidence.className = "object";
      evidence.textContent = JSON.stringify(finding.evidence);
      item.append(evidence);
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
    link.textContent = `PR：${run.pr.url}`;
    pr.append(link);
    const stateText = document.createElement("span");
    stateText.className = "muted";
    stateText.textContent = ` · ${run.pr.state || ""} · ${String(run.pr.commit || "").slice(0, 12)}`;
    pr.append(stateText);
    pr.hidden = false;
  } else if (automaticState === "passed") {
    pr.textContent = "模型已通过独立校验；PR 尚未创建或发布服务失败，可先查看下方已验证 URDF。";
    pr.hidden = false;
  } else {
    pr.hidden = true;
  }
}

async function loadPreview(dagRunId) {
  const note = $("viewer-note");
  try {
    const preview = await api(`/api/runs/${encodeURIComponent(dagRunId)}/preview`);
    if (state.previewSubject === preview.subject_sha256) return;
    $("viewer-card").hidden = false;
    $("preview-meta").textContent = `交付摘要 ${String(preview.subject_sha256).slice(0, 12)}… · URDF ${preview.urdf}`;
    note.hidden = true;
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
      onWarning: (warnings) => {
        if (warnings.length) {
          note.textContent = warnings.join("；");
          note.hidden = false;
        }
      },
    });
    state.controls = buildJointControls($("joint-controls"), loaded.joints, {});
    state.previewSubject = preview.subject_sha256;
  } catch (error) {
    if (error.status === 404 || error.status === 409) {
      note.textContent = `尚未提供已验证交付：${error.message}`;
      note.hidden = false;
      return;
    }
    note.textContent = `交付预览不可用：${error.message}`;
    note.hidden = false;
  }
}

async function poll() {
  if (!state.dagRunId) return;
  try {
    const run = await api(`/api/runs/${encodeURIComponent(state.dagRunId)}`);
    renderRun(run);
    if (run.automatic && run.automatic.state === "passed") {
      await loadPreview(state.dagRunId);
    }
    const jobDone = run.job && (run.job.status === "passed" || run.job.status === "failed");
    const verified = run.automatic && run.automatic.state === "passed";
    if ((run.state === "success" || run.state === "failed") && jobDone && (!verified || state.previewSubject)) {
      if (state.timer) window.clearInterval(state.timer);
      state.timer = null;
    }
  } catch (error) {
    setError($("run-error"), error.message);
  }
}

async function selectRun(dagRunId) {
  state.dagRunId = dagRunId;
  state.previewSubject = null;
  $("detail-card").hidden = false;
  await refreshRuns();
  await poll();
  if (state.timer) window.clearInterval(state.timer);
  state.timer = window.setInterval(() => {
    void poll();
  }, 3000);
}

function wire() {
  $("logout").addEventListener("click", async () => {
    try {
      await api("/api/session", { method: "DELETE" });
    } catch {
      // the local session is cleared even if Airflow is unreachable
    }
    clearSession();
  });

  $("run-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    setError($("run-error"), "");
    const button = $("start-button");
    button.disabled = true;
    try {
      const payload = await api("/api/runs", {
        method: "POST",
        body: { handoff_path: $("handoff-path").value },
      });
      await selectRun(payload.dag_run_id);
    } catch (error) {
      setError($("run-error"), error.message);
    } finally {
      button.disabled = false;
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
