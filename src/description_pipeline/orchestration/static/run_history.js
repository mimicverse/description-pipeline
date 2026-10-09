// Pure run-history list helpers, shared by the operator page and the Node regression test.
// No DOM access here: app.js wires these into the page.

// The detail payload is authoritative once it carries the field; the cached row is a fallback,
// so a run opened via a parent link never depends on its row being on a loaded page.
export function resolveTitle(detail, cached) {
  const source = detail && detail.title === undefined ? (cached && cached.title) || "" : (detail && detail.title) || "";
  return String(source).trim();
}

export function resolveDeleted(detail, cached) {
  if (detail && typeof detail.deleted === "boolean") return detail.deleted;
  return Boolean(cached && cached.deleted === true);
}

// One decision for the detail-header menu host: rebuild only when the selected run changes
// (or permissions vanish); the 3s poll of the same run must never destroy an open form.
export function menuAction(currentRunId, runId, canManage) {
  if (!canManage) return "clear";
  return currentRunId === runId ? "keep" : "rebuild";
}

// Merge one authoritative server overlay into the cached row and the object the menu was
// opened with, so paging, selection and an open menu all survive a mutation.
export function overlayRun(runs, run, patch) {
  if (Array.isArray(runs) && run) {
    const entry = runs.find((item) => item && item.dag_run_id === run.dag_run_id) || null;
    if (entry) Object.assign(entry, patch);
  }
  if (run) Object.assign(run, patch);
}
