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
const AUTOMATIC_STATES = {
  passed: "自动校验通过",
  failed: "自动校验失败",
  unverified: "未通过独立校验",
  pending: "等待自动检查",
  queued: "排队中",
  running: "检查中",
};

const $ = (id) => document.getElementById(id);

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
  $("preview-meta").textContent = "";
  $("viewer-note").hidden = true;
  $("joint-controls").textContent = "";
}

function clearSession() {
  clearPreview();
  state.csrf = null;
  state.user = null;
  state.dagRunId = null;
  state.lastRun = null;
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
      const folder = String(run.handoff_path || "工程交付").replace(/[\\/]+$/, "").split(/[\\/]/).pop();
      button.textContent = `${folder} · ${RUN_STATES[run.state] || run.state || "待执行"}`;
      button.title = `${run.handoff_path || ""}\n${run.dag_run_id}`;
      button.addEventListener("click", () => selectRun(run.dag_run_id));
      item.append(button);
      list.append(item);
    }
  } catch (error) {
    list.textContent = `运行列表不可用：${error.message}`;
  }
}

function renderRun(run) {
  const runId = String(run.dag_run_id || "");
  const authorized = state.previewFiles || {};
  const artifactUrl = (name) =>
    `/api/runs/${encodeURIComponent(runId)}/artifacts/${String(name)
      .split("/")
      .map(encodeURIComponent)
      .join("/")}`;
  const authorizedDigest = (path, hash) =>
    Boolean(hash) && Object.prototype.hasOwnProperty.call(authorized, path) && authorized[path] === hash;
  const reference = (path, hash) => {
    const line = document.createElement("p");
    if (authorizedDigest(path, hash)) {
      const link = document.createElement("a");
      link.href = artifactUrl(path);
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = path;
      line.append(link);
    } else {
      line.append(document.createTextNode(path));
      line.title = "引用与明细；仅已验证且摘要绑定的 URDF／网格资产提供下载";
    }
    if (hash) {
      const digest = document.createElement("code");
      digest.textContent = hash;
      line.append(" ", digest);
    }
    return line;
  };
  const meta = $("run-meta");
  meta.textContent = "";
  const rows = [
    ["运行标识", run.dag_run_id],
    ["工程文件夹", run.handoff_path || "—"],
    ["状态", RUN_STATES[run.state] || run.state || "—"],
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
    chip.textContent = `${TRANSPORT[task.task_id] || task.task_id}：${TASK_STATES[task.state] || task.state || "—"}`;
    progress.append(chip);
  }

  const stages = $("stages");
  stages.textContent = "";
  for (const [index, stage] of ((run.stage_view && run.stage_view.stages) || []).entries()) {
    const item = document.createElement("section");
    item.className = `stage-card ${stage.state}`;
    const heading = document.createElement("h4");
    heading.textContent = `${index + 1}. ${stage.name_zh || stage.name} · ${STAGE_STATES[stage.state] || stage.state} · ${stage.checks_passed}/${stage.checks_total} 项`;
    item.append(heading);
    if (stage.error) {
      const error = document.createElement("p");
      error.className = "error";
      error.textContent = stage.error;
      item.append(error);
      if (stage.diagnostic) {
        const evidence = document.createElement("pre");
        evidence.textContent = JSON.stringify(stage.diagnostic, null, 2);
        expandable(item, "失败诊断").append(evidence);
      }
    }
    const grid = document.createElement("div");
    grid.className = "stage-boundaries";
    for (const [key, title] of [["inputs", "输入"], ["input_qc", "输入质检"], ["outputs", "输出"], ["output_qc", "输出质检"]]) {
      const column = document.createElement("div");
      const label = document.createElement("strong");
      label.textContent = title;
      column.append(label);
      for (const row of stage[key]) {
        const line = document.createElement("p");
        line.textContent = row.label;
        column.append(line);
        if (row.state) {
          line.append(" ", badge(CHECK_STATES[row.state] || row.state, row.state === "passed" ? "ok" : row.state === "failed" ? "bad" : "pending"));
          const detail = expandable(column, `${row.id} · 结果与证据`);
          const checks = row.details && (row.details.checks || (row.details.diagnostic && row.details.diagnostic.checks));
          if (checks) {
            for (const check of checks) {
              const child = expandable(detail, `${check.id} · ${CHECK_STATES[check.state] || "未报告"}`);
              const evidence = document.createElement("pre");
              evidence.textContent = JSON.stringify(check.details || {}, null, 2);
              child.append(evidence);
            }
          } else {
            const evidence = document.createElement("pre");
            evidence.textContent = JSON.stringify(row.details || {}, null, 2);
            detail.append(evidence);
          }
        } else {
          const files = Object.entries(row.files || {});
          const info = document.createElement("p");
          info.className = "muted";
          info.textContent = `${row.path} · ${row.class} · ${files.length ? `${files.length} 个文件` : "尚无文件记录"}`;
          column.append(info);
          if (files.length) {
            const detail = expandable(column, "文件与 SHA-256");
            const list = document.createElement("div");
            list.className = "stage-files";
            for (const [path, hash] of files) list.append(reference(path, hash));
            detail.append(list);
          }
        }
      }
      grid.append(column);
    }
    item.append(grid);
    const evidencePaths = (stage.evidence || []).filter((path) => typeof path === "string" && path);
    if (evidencePaths.length) {
      const evidenceRow = document.createElement("div");
      evidenceRow.className = "stage-evidence muted";
      evidenceRow.append("契约证据引用：");
      evidencePaths.forEach((path, position) => {
        if (position) evidenceRow.append("、");
        evidenceRow.append(reference(path, authorized[path]));
      });
      item.append(evidenceRow);
    }
    const unsupported = (stage.unsupported || []).filter((item) => item && item.label);
    if (unsupported.length) {
      const row = document.createElement("div");
      row.className = "stage-unsupported muted";
      row.append("未支持项（工程确认后才能关闭，不代表通过）：");
      for (const item of unsupported) {
        const chip = document.createElement("span");
        chip.className = "chip pending";
        chip.textContent = `${item.label} · 不支持/待工程确认`;
        row.append(" ", chip);
      }
      item.append(row);
    }
    if (stage.confirmations.length) {
      const pending = document.createElement("p");
      pending.className = "muted";
      pending.textContent = `工程确认（在本版本 PR／受控记录中完成）：${stage.confirmations.map((row) => row.label).join("、")} · 待确认`;
      item.append(pending);
    }
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
  const checks = (run.automatic && run.automatic.checks) || [];
  const allChecks = checks.length ? expandable(automatic, `查看全部 ${checks.length} 项检查`) : automatic;
  for (const check of checks) {
    const chip = document.createElement("span");
    chip.className = `chip ${check.passed === false ? "failed" : check.passed === true ? "success" : ""}`.trim();
    chip.textContent = `${check.id || "检查"}：${CHECK_STATES[check.state] || "未报告"}`;
    allChecks.append(chip);
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
  const items = (coverage.engineering && coverage.engineering.items) || [];
  const allConfirmations = items.length ? expandable(confirmations, `查看 ${items.length} 项工程确认`) : confirmations;
  for (const item of items) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = `${item.id}：待确认`;
    allConfirmations.append(chip);
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
    state.viewer.frame();
  } catch (error) {
    if (request.signal.aborted || state.dagRunId !== dagRunId) return;
    if (state.viewer) disposeRobot(state.viewer);
    state.controls = null;
    state.previewSubject = null;
    state.previewFiles = null;
    if (state.lastRun) renderRun(state.lastRun);
    $("joint-controls").textContent = "";
    $("viewer-card").hidden = false;
    if (error.status === 404 || error.status === 409) {
      note.textContent = `尚未提供已验证交付：${error.message}`;
      note.hidden = false;
      return;
    }
    note.textContent = `交付预览不可用：${error.message}`;
    note.hidden = false;
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
    setError($("run-error"), "");
    const verified = run.automatic && run.automatic.state === "passed";
    if (!verified) clearPreview();
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
  } catch (error) {
    if (state.dagRunId !== dagRunId) return;
    if (error.status === 401) {
      clearSession();
      setError($("login-error"), error.message);
      return;
    }
    if (error.status === 403) clearPreview();
    setError($("run-error"), error.message);
  }
}

async function selectRun(dagRunId) {
  if (state.timer) window.clearInterval(state.timer);
  state.timer = null;
  clearPreview();
  state.dagRunId = dagRunId;
  state.lastRun = null;
  $("detail-card").hidden = false;
  await refreshRuns();
  if (state.dagRunId !== dagRunId) return;
  state.timer = window.setInterval(() => {
    void poll();
  }, 3000);
  await poll();
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
