// Pure live-activity view helpers (no DOM). app.js wires them into the operator page and the
// Node regression test exercises them directly. Portal view contract (activity v2): the
// "activity" object is always present on run payloads; "available" marks a real telemetry
// record and "state" is one of queued | waiting | busy | finished | none. The server sends
// codes only (action.code = "<stage>.<action>"); wording lives here.
//
// Hard rules: only updated_at indicates recency (a successful poll is never liveness); no
// percentage figures and no completion forecasts anywhere; unavailable telemetry is stated
// explicitly and never replaced by a guess from the stage name.

export const ACTIVITY_STATE_TEXT = {
  queued: "Queued",
  waiting: "Waiting for progress",
  busy: "In progress",
  none: "No detailed progress",
  finished: "Finished",
};

export const FRESH_SOON_MS = 20000;
export const FRESH_STALE_MS = 90000;

const STAGE_TEXT = {
  freeze: "Freeze inputs",
  discover: "Discover structure",
  capture: "Capture evidence",
  generate: "Generate URDF",
  verify: "Independent verification",
  publish: "Publish review PR",
};

// Starter vocabulary from the emit side; unknown codes fall back to a neutral stage phrase and
// are never turned into invented specifics.
const ACTION_TEXT = {
  "discover.session_start": "Starting SolidWorks",
  "discover.scan_documents": "Scanning engineering files",
  "discover.open_document": "Opening document",
  "discover.select_configuration": "Switching configuration",
  "discover.read_document_state": "Reading saved model state",
  "discover.read_components": "Reading assembly components",
  "discover.read_mates": "Reading mates",
  "discover.read_datums": "Reading datums and coordinate systems",
  "discover.read_properties": "Reading properties and delivery definitions",
  "discover.hash_sources": "Computing file digests",
  "discover.build_record": "Building discovery record",
  "capture.start": "Starting prepared Capture",
  "capture.prepared_source": "Verifying the prepared source",
  "capture.open_prepared": "Opening the prepared assembly read-only",
  "capture.readings": "Reading native scene, mass, datums and configuration",
  "capture.geometry_facts": "Reading native bodies, faces and triangles",
  "capture.release": "Releasing the owned CAD session",
  "capture.scene_projection": "Projecting sealed native facts offline",
  "capture.geometry": "Writing offline STL geometry",
  "capture.seal": "Sealing Capture evidence",
  "capture.prepare_capture": "Preparing the current Capture occurrence",
  "capture.walk": "Reading the native occurrence tree",
  "capture.instance_reads": "Reading instance configuration and mass properties",
};

const UNIT_TEXT = {
  instances: "instances",
  documents: "documents",
  files: "files",
  components: "components",
  occurrences: "occurrences",
  mates: "mates",
};

export function stageText(stage) {
  if (typeof stage !== "string" || !stage) return "";
  return STAGE_TEXT[stage] || stage;
}

export function actionText(code, stage) {
  if (typeof code === "string" && Object.prototype.hasOwnProperty.call(ACTION_TEXT, code)) {
    return ACTION_TEXT[code];
  }
  const label = stageText(stage);
  return label ? `${label} in progress` : "In progress";
}

export function countsText(counts) {
  if (!counts || typeof counts !== "object") return "";
  const done = Number.isFinite(counts.done) ? counts.done : null;
  const total = Number.isFinite(counts.total) ? counts.total : null;
  const unit = typeof counts.unit === "string" && UNIT_TEXT[counts.unit] ? ` ${UNIT_TEXT[counts.unit]}` : "";
  if (done === null && total === null) return "";
  if (done !== null && total !== null) return `Processed ${done}/${total}${unit}`;
  if (done !== null) return `Processed ${done}${unit}`;
  return `Planned ${total}${unit}`;
}

export function formatDuration(ms) {
  const safe = Number.isFinite(ms) && ms > 0 ? ms : 0;
  const seconds = Math.floor(safe / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const restSeconds = seconds % 60;
  if (minutes < 60) return restSeconds ? `${minutes}m ${restSeconds}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes ? `${hours}h ${restMinutes}m` : `${hours}h`;
}

export function freshnessText(updatedAt, nowMs) {
  const at = typeof updatedAt === "string" ? Date.parse(updatedAt) : NaN;
  if (!Number.isFinite(at)) return { text: "", stale: false, known: false, ageMs: null };
  const age = Math.max(0, Number.isFinite(nowMs) ? nowMs - at : 0);
  if (age <= FRESH_SOON_MS) return { text: "Updated just now", stale: false, known: true, ageMs: age };
  if (age < FRESH_STALE_MS) {
    return { text: `Updated ${Math.floor(age / 1000)}s ago`, stale: false, known: true, ageMs: age };
  }
  return {
    text: `No new update (last updated ${formatDuration(age)} ago)`,
    stale: true,
    known: true,
    ageMs: age,
  };
}

function clockText(ms) {
  const date = new Date(ms);
  const pad = (value) => String(value).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function absoluteStamp(value) {
  const at = typeof value === "string" ? Date.parse(value) : NaN;
  if (!Number.isFinite(at)) return "";
  const date = new Date(at);
  const pad = (item) => String(item).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

// Newest first, at most five rows; entries without a usable timestamp sort last.
export function recentRows(activity) {
  const list = activity && Array.isArray(activity.recent) ? activity.recent : [];
  const stage = activity && typeof activity.stage === "string" ? activity.stage : "";
  return list
    .filter((item) => item && typeof item === "object")
    .map((item) => {
      const at = typeof item.at === "string" ? Date.parse(item.at) : NaN;
      return {
        at: Number.isFinite(at) ? at : 0,
        timeText: Number.isFinite(at) ? clockText(at) : "",
        text: actionText(item.code, stage),
        object: typeof item.object === "string" ? item.object : "",
      };
    })
    .sort((left, right) => right.at - left.at)
    .slice(0, 5);
}

export function activityView(activity, { runState = "", waitingMessage = "", nowMs = Date.now() } = {}) {
  const record = activity && typeof activity === "object" ? activity : null;
  const rawState = record && typeof record.state === "string" ? record.state : "none";
  const available = Boolean(record && record.available === true);
  const stage = record && typeof record.stage === "string" ? record.stage : "";
  const empty = {
    visible: false,
    final: false,
    state: "none",
    stateText: ACTIVITY_STATE_TEXT.none,
    stage: "",
    stageText: "",
    actionText: "",
    objectText: "",
    countsText: "",
    elapsedText: "",
    freshnessText: "",
    stale: false,
    note: "",
    recent: [],
  };
  if (runState === "unresolved") {
    return {
      ...empty, visible: true, state: "waiting",
      stateText: "Awaiting reconciliation",
      note: "External effects require reconciliation; prior activity does not prove current execution.",
      recent: recentRows(record || {}),
    };
  }
  const terminalRun =
    runState === "success" || runState === "failed" || runState === "cancelled";
  if (terminalRun || rawState === "finished") {
    // Terminal runs keep at most a compact last-activity record for diagnosis.
    // (dot animation, elapsed ticking, staleness nagging) are gone. Legacy terminal runs
    // without any preserved record stay hidden.
    const recent = recentRows(record || {});
    const stamp = record && typeof record.updated_at === "string" ? absoluteStamp(record.updated_at) : "";
    const action = record && record.action && typeof record.action === "object" ? record.action : null;
    const objectText = record && typeof record.object === "string" ? record.object : "";
    const hasEvidence = recent.length > 0 || Boolean(stamp) || Boolean(action && action.code) || Boolean(objectText);
    if (!hasEvidence) {
      return { ...empty, state: "finished", stateText: ACTIVITY_STATE_TEXT.finished };
    }
    return {
      ...empty,
      visible: true,
      final: true,
      state: "finished",
      stateText: ACTIVITY_STATE_TEXT.finished,
      stage,
      stageText: stageText(stage),
      actionText: action && action.code ? actionText(action.code, stage) : "",
      objectText,
      freshnessText: stamp ? `Last activity: ${stamp}` : "",
      recent,
    };
  }
  const freshness = record ? freshnessText(record.updated_at, nowMs) : { text: "", stale: false };
  const base = {
    ...empty,
    visible: true,
    state: "none",
    stateText: ACTIVITY_STATE_TEXT.none,
    stage,
    stageText: stageText(stage),
    freshnessText: freshness.text,
    stale: Boolean(freshness.stale),
    recent: recentRows(record || {}),
  };
  if (rawState === "busy" && available) {
    const action = record.action && typeof record.action === "object" ? record.action : null;
    const startedAt = typeof record.stage_started_at === "string" ? Date.parse(record.stage_started_at) : NaN;
    return {
      ...base,
      state: "busy",
      stateText: ACTIVITY_STATE_TEXT.busy,
      actionText: actionText(action && action.code, stage),
      objectText: typeof record.object === "string" ? record.object : "",
      countsText: countsText(record.counts),
      elapsedText: Number.isFinite(startedAt) ? `Elapsed ${formatDuration(Math.max(0, nowMs - startedAt))}` : "",
      note: "",
    };
  }
  if (runState === "waiting") {
    return {
      ...base,
      state: "waiting",
      stateText: "Awaiting external result",
      note: waitingMessage || "An authorized execution is awaiting its recorded external result; no detailed progress is available.",
    };
  }
  if (rawState === "queued") {
    return {
      ...base,
      state: "queued",
      stateText: ACTIVITY_STATE_TEXT.queued,
    note: "Queued and waiting to start.",
    };
  }
  // waiting | none | anything without a usable record: state the gap explicitly and never
  // infer a CAD action from the stage name.
  return {
    ...base,
    state: rawState === "waiting" ? "waiting" : "none",
    stateText: rawState === "waiting" ? ACTIVITY_STATE_TEXT.waiting : ACTIVITY_STATE_TEXT.none,
    note: runState === "running" ? "The run is active but no detailed progress is available (no live activity was reported)." : "No detailed progress is available.",
  };
}

// Where the card belongs on the stage pane: only the running stage shows live activity for
// itself ("live"), anything else degrades to an explicit note ("note") or hides ("hidden").
export function activityPlacement(view, { stageId = "", stageRunning = false, stageFailed = false } = {}) {
  if (!view || view.visible !== true) return "hidden";
  if (view.final === true) {
    return stageFailed && (!view.stage || view.stage === stageId) ? "final" : "hidden";
  }
  if (!stageRunning) return "hidden";
  if (view.state === "waiting" && (!view.stage || view.stage === stageId)) return "live";
  if (view.state === "busy" && view.stage && stageId && view.stage === stageId) return "live";
  return "note";
}
