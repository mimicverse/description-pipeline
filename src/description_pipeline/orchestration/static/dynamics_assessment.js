/* Optional assessment controls. The root app supplies its authenticated API helper. */

const OWNER = Symbol("dynamics-assessment-view");
const ACTIVE = new Set(["registered", "dispatching", "queued", "running", "verifying", "sealing", "unknown"]);
const LABELS = {
  registered: "Registered", dispatching: "Dispatch pending", queued: "Queued", running: "Running",
  verifying: "Verifying", sealing: "Saving evidence", unknown: "Awaiting reconciliation",
  completed: "Assessment completed", unsupported: "Inputs unsupported", failed: "Assessment failed",
  cancelled: "Cancelled", timeout: "Timed out",
};

export function assessmentStateLabel(state) {
  return LABELS[state] || "Status unavailable";
}

export function mountDynamicsAssessment({ container, runId, requestJson, getBundle = null,
  canManage = false, onError = () => {} }) {
  if (!container || typeof requestJson !== "function" || typeof runId !== "string") {
    throw new TypeError("Assessment view requires a container, source run and authenticated API helper");
  }
  const binding = {};
  container[OWNER] = binding;
  const document = container.ownerDocument;
  const reports = new Map();
  const controllers = new Set();
  const sourceUrl = `/api/runs/${encodeURIComponent(runId)}/assessments/dynamics`;
  let enabled = null;
  let sourceReady = false;
  let sourceSha = null;
  let inputLimit = 64 * 1024 ** 2;
  let selectedFile = null;
  let uploadedBundle = null;
  let uploading = false;
  let uploadCancelled = false;
  let selected = false;
  let assessments = [];
  let error = "";
  let busy = false;
  let pendingSubmission = null;
  let timer = null;
  let sequence = 0;
  let destroyed = false;
  const alive = () => !destroyed && container[OWNER] === binding;

  function element(tag, text, className = "") {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function button(text, action, disabled = false) {
    const node = element("button", text, "button secondary");
    node.type = "button";
    node.disabled = disabled;
    node.addEventListener("click", action);
    return node;
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    controllers.add(controller);
    try {
      return await requestJson(path, { ...options, signal: controller.signal });
    } finally {
      controllers.delete(controller);
    }
  }

  function counts(value) {
    return value && Object.keys(value).length
      ? Object.entries(value).map(([state, count]) => `${state.replaceAll("_", " ")}: ${count}`).join("; ")
      : "No recorded result";
  }

  function renderReport(parent, report, attemptId) {
    const facts = element("dl", undefined, "assessment-facts");
    function fact(label, value) {
      facts.append(element("dt", label), element("dd", value));
    }
    fact("Input qualification", report.state === "unsupported" ? "Unsupported" : (
      report.dynamic_recording?.qualification_scope?.replaceAll("_", " ") || "Not established"
    ));
    fact("Recorded execution", counts(report.canonical_axes?.execution));
    fact("Recorded feasibility", counts(report.canonical_axes?.feasibility));
    fact("Recorded verification", counts(report.canonical_axes?.verification));
    const receipt = report.independent_verification;
    fact("Independent verification", receipt ? (
      `${receipt.pass === true ? "Passed" : "Not passed"}; ${receipt.records_checked ?? "unknown"}`
      + ` / ${receipt.expected_records ?? "unknown"} records; coverage ${receipt.coverage_complete === true ? "complete" : "incomplete"}`
    ) : "Not available");
    parent.append(facts);
    const metrics = report.reported_metrics || {};
    if (Object.keys(metrics).length) {
      const details = element("details");
      details.append(element("summary", "Reported task measurements"));
      const table = element("table");
      const heading = element("tr");
      for (const text of ["Task", "Maximum joint error (normalized)", "TCP position error (m)", "TCP orientation error (rad)", "Control saturation fraction"]) {
        heading.append(element("th", text));
      }
      table.append(heading);
      for (const [name, row] of Object.entries(metrics)) {
        const line = element("tr");
        for (const value of [name, row.joint_tracking_max_normalized, row.tcp_position_error_m,
          row.tcp_orientation_error_rad, row.ctrl_saturation_fraction]) {
          line.append(element("td", value === null || value === undefined ? "Not available" : value));
        }
        table.append(line);
      }
      details.append(table);
      parent.append(details);
    }
    const limitations = element("ul");
    for (const value of report.limitations || []) limitations.append(element("li", value));
    parent.append(limitations);
    const download = element("a", "Download assessment summary");
    download.href = `/api/dynamics-assessments/attempts/${encodeURIComponent(attemptId)}/result`;
    parent.append(download);
    for (const [role, path] of Object.entries(report.source_certificate_members || {})) {
      const certificate = element("a", `Download ${role.toUpperCase()} source certificate and warnings`);
      certificate.href = `${download.href}?path=${encodeURIComponent(path)}`;
      parent.append(certificate);
    }
    if (report.verification_receipt) {
      const receiptLink = element("a", "Download independent receipt");
      receiptLink.href = `${download.href}?path=${encodeURIComponent(report.verification_receipt)}`;
      parent.append(receiptLink);
    }
  }

  function render() {
    if (!alive()) return;
    const panel = element("section", undefined, "optional-check dynamics-assessment");
    panel.append(element("h3", "Optional dynamics assessment"));
    panel.append(element("p", "Engineering publication proceeds independently. Results apply to the declared model and inputs."));
    if (error) {
      const alert = element("p", error, "error");
      alert.setAttribute("role", "alert");
      panel.append(alert);
    }
    if (enabled === null) panel.append(element("p", error ? "Assessment status unavailable" : "Loading assessment status…"));
    if (enabled === false) panel.append(element("p", "Optional dynamics assessment is disabled."));
    if (enabled === true) {
      const label = element("label");
      const checkbox = element("input");
      checkbox.type = "checkbox";
      checkbox.checked = selected;
      checkbox.disabled = busy || !canManage || pendingSubmission !== null;
      checkbox.addEventListener("change", () => { selected = checkbox.checked; render(); });
      label.append(checkbox, element("span", "Select optional dynamics assessment"));
      panel.append(label);
      panel.append(element("p", sourceReady
        ? "An execution bundle is required for dynamics. Missing inputs produce an explicit unsupported result."
        : "Verified engineering URDF/MJCF inputs are not available yet."));
      if (sourceReady && canManage) {
        const bindingLink = element("a", "Download source binding for the execution bundle");
        bindingLink.href = `${sourceUrl}/source`;
        panel.append(bindingLink);
        const fileLabel = element("label", "Execution bundle (.zip)");
        const file = element("input"); file.type = "file"; file.accept = ".zip";
        file.disabled = busy || pendingSubmission !== null;
        file.addEventListener("change", () => {
          selectedFile = file.files?.[0] || null; uploadedBundle = null; render();
        });
        fileLabel.append(file); panel.append(fileLabel);
        if (selectedFile) panel.append(element("p", selectedFile.name));
        panel.append(button("Upload execution bundle", upload,
          busy || pendingSubmission !== null || !selectedFile));
        if (uploadedBundle) panel.append(element("p", "Execution inputs admitted for this source. Ready to run."));
        if (uploading) panel.append(button("Cancel input upload", () => {
          uploadCancelled = true;
          for (const controller of controllers) controller.abort();
        }));
      }
      const unresolved = assessments.some(item => ACTIVE.has(item.attempt?.state));
      panel.append(button("Run assessment", start, !canManage || !selected || !sourceReady || busy || unresolved));
      if (pendingSubmission) panel.append(button("Reconcile submitted request", () =>
        mutate(sourceUrl, pendingSubmission.body, pendingSubmission.key), busy || !canManage || !sourceReady));
      for (const item of assessments) {
        const attempt = item.attempt;
        if (!attempt) continue;
        const article = element("article", undefined, "assessment-attempt");
        const state = element("p", `Attempt ${attempt.attempt_number}: ${assessmentStateLabel(attempt.state)}`);
        state.setAttribute("aria-live", "polite");
        article.append(state);
        if (attempt.cancel_requested && !attempt.resources_settled) {
          article.append(element("p", "Cancellation requested; resource settlement is pending."));
        }
        const attemptUrl = `/api/dynamics-assessments/attempts/${encodeURIComponent(attempt.attempt_id)}`;
        if (ACTIVE.has(attempt.state)) {
          article.append(button("Cancel assessment", () => mutate(`${attemptUrl}/cancel`), busy || !canManage));
        }
        if (attempt.state === "unknown" || (attempt.state === "registered" && attempt.detail?.dispatch_observation !== "absent")) {
          article.append(button("Reconcile original attempt", () => mutate(`${attemptUrl}/reconcile`), busy || !canManage));
        }
        if (attempt.state === "registered" && attempt.dispatch_accepted !== true && attempt.detail?.dispatch_observation === "absent") {
          article.append(button("Send original attempt", () => mutate(`${attemptUrl}/dispatch`), busy || !canManage));
        }
        if (["failed", "cancelled", "timeout"].includes(attempt.state) && attempt.resources_settled) {
          article.append(button("Retry in a new attempt", () => mutate(
            `/api/dynamics-assessments/${encodeURIComponent(item.request_id)}/retry`,
          ), busy || !canManage));
        }
        const report = reports.get(attempt.attempt_id);
        if (report) renderReport(article, report, attempt.attempt_id);
        panel.append(article);
      }
    }
    panel.append(button("Refresh assessment", refresh, busy));
    container.replaceChildren(panel);
  }

  async function refresh() {
    if (!alive()) return;
    clearTimeout(timer);
    const current = ++sequence;
    try {
      const value = await api(sourceUrl);
      if (!alive() || current !== sequence) return;
      if (typeof value.enabled !== "boolean" || !Array.isArray(value.assessments)) {
        throw new Error("Assessment status response is invalid");
      }
      enabled = value.enabled;
      sourceReady = value.source_ready !== false;
      if (sourceSha !== (value.source_sha256 ?? null)) uploadedBundle = null;
      sourceSha = value.source_sha256 ?? null;
      if (Number.isSafeInteger(value.input_limit_bytes) && value.input_limit_bytes > 0) {
        inputLimit = Math.min(value.input_limit_bytes, 64 * 1024 ** 2);
      }
      assessments = value.assessments;
      error = "";
      for (const item of assessments) {
        const attempt = item.attempt;
        if (attempt && item.result_artifact && !reports.has(attempt.attempt_id)) {
          const report = await api(`/api/dynamics-assessments/attempts/${encodeURIComponent(attempt.attempt_id)}/result`);
          if (!alive() || current !== sequence) return;
          reports.set(attempt.attempt_id, report);
        }
      }
      render();
      if (assessments.some(item => ACTIVE.has(item.attempt?.state))) timer = setTimeout(refresh, 2000);
    } catch (failure) {
      if (!alive() || current !== sequence || failure.name === "AbortError") return;
      error = failure.message || "Assessment status is unavailable";
      assessments = [];
      reports.clear();
      render();
      onError(failure);
    }
  }

  async function mutate(path, body = undefined, idempotencyKey = undefined) {
    if (!alive() || busy) return;
    busy = true;
    error = "";
    render();
    try {
      await api(path, { method: "POST", body, idempotencyKey });
      if (!alive()) return;
      if (idempotencyKey && pendingSubmission?.key === idempotencyKey) pendingSubmission = null;
      await refresh();
    } catch (failure) {
      if (!alive() || failure.name === "AbortError") return;
      error = failure.message || "Assessment operation was not confirmed";
      render();
      onError(failure);
    } finally {
      busy = false;
      render();
    }
  }

  async function start() {
    if (!alive() || busy || !selected || !sourceReady || !canManage) return;
    try {
      if (!pendingSubmission) {
        const bundle = uploadedBundle || (typeof getBundle === "function" ? await getBundle() : null);
        if (!alive()) return;
        pendingSubmission = { key: crypto.randomUUID(), body: structuredClone({ bundle: bundle ?? null }) };
      }
      await mutate(sourceUrl, pendingSubmission.body, pendingSubmission.key);
    } catch (failure) {
      error = failure.message || "Execution bundle could not be selected";
      render();
      onError(failure);
    }
  }

  async function upload() {
    if (!alive() || busy || !selectedFile || !canManage || !sourceReady || pendingSubmission) return;
    busy = true; uploading = true; uploadCancelled = false; error = ""; render();
    try {
      if (selectedFile.size <= 0 || selectedFile.size > inputLimit) {
        throw new Error(`Execution archive must be between 1 and ${inputLimit} bytes.`);
      }
      const bytes = new Uint8Array(await selectedFile.arrayBuffer());
      if (!alive() || uploadCancelled) return;
      let encoded = "";
      for (let offset = 0; offset < bytes.length; offset += 32768) {
        encoded += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
      }
      const response = await api(`${sourceUrl}/bundles`, {
        method: "POST", body: { archive_base64: btoa(encoded) },
      });
      if (!alive() || uploadCancelled) return;
      if (!response.bundle || response.source_sha256 !== sourceSha) {
        throw new Error("Admitted execution inputs do not match this source. Refresh before running.");
      }
      uploadedBundle = response.bundle;
    } catch (failure) {
      if (alive() && failure.name !== "AbortError" && !uploadCancelled) {
        error = failure.message || "Execution inputs were not admitted"; onError(failure);
      }
    } finally {
      busy = false; uploading = false; render();
    }
  }

  render();
  return {
    ready: refresh(),
    refresh,
    destroy() {
      destroyed = true;
      ++sequence;
      clearTimeout(timer);
      for (const controller of controllers) controller.abort();
      if (container[OWNER] === binding) delete container[OWNER];
    },
  };
}
