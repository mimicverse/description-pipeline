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
  queued: "排队中",
  waiting: "等待进度",
  busy: "进行中",
  none: "无详细进度",
  finished: "已结束",
};

export const FRESH_SOON_MS = 20000;
export const FRESH_STALE_MS = 90000;

const STAGE_TEXT = {
  freeze: "冻结输入",
  discover: "解析结构",
  capture: "采集证据",
  generate: "生成 URDF",
  verify: "独立验证",
  publish: "提交评审 PR",
};

// Starter vocabulary from the emit side; unknown codes fall back to a neutral stage phrase and
// are never turned into invented specifics.
const ACTION_TEXT = {
  "discover.session_start": "正在启动 SolidWorks",
  "discover.scan_documents": "扫描工程文件",
  "discover.open_document": "打开文档",
  "discover.select_configuration": "切换配置",
  "discover.rebuild": "重建模型",
  "discover.read_components": "读取装配组件",
  "discover.read_mates": "读取配合关系",
  "discover.read_datums": "读取基准与坐标系",
  "discover.read_properties": "读取属性与交付定义",
  "discover.hash_sources": "计算文件摘要",
  "discover.build_record": "生成发现记录",
};

const UNIT_TEXT = {
  instances: "个实例",
  documents: "个文档",
  files: "个文件",
  components: "个组件",
  occurrences: "个实例",
  mates: "组配合",
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
  return label ? `${label}进行中` : "进行中";
}

export function countsText(counts) {
  if (!counts || typeof counts !== "object") return "";
  const done = Number.isFinite(counts.done) ? counts.done : null;
  const total = Number.isFinite(counts.total) ? counts.total : null;
  const unit = typeof counts.unit === "string" && UNIT_TEXT[counts.unit] ? ` ${UNIT_TEXT[counts.unit]}` : "";
  if (done === null && total === null) return "";
  if (done !== null && total !== null) return `已处理 ${done}/${total}${unit}`;
  if (done !== null) return `已处理 ${done}${unit}`;
  return `计划 ${total}${unit}`;
}

export function formatDuration(ms) {
  const safe = Number.isFinite(ms) && ms > 0 ? ms : 0;
  const seconds = Math.floor(safe / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const restSeconds = seconds % 60;
  if (minutes < 60) return restSeconds ? `${minutes} 分 ${restSeconds} 秒` : `${minutes} 分`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes ? `${hours} 小时 ${restMinutes} 分` : `${hours} 小时`;
}

export function freshnessText(updatedAt, nowMs) {
  const at = typeof updatedAt === "string" ? Date.parse(updatedAt) : NaN;
  if (!Number.isFinite(at)) return { text: "", stale: false, known: false, ageMs: null };
  const age = Math.max(0, Number.isFinite(nowMs) ? nowMs - at : 0);
  if (age <= FRESH_SOON_MS) return { text: "刚刚更新", stale: false, known: true, ageMs: age };
  if (age < FRESH_STALE_MS) {
    return { text: `${Math.floor(age / 1000)} 秒前更新`, stale: false, known: true, ageMs: age };
  }
  return {
    text: `暂无新更新（上次更新 ${formatDuration(age)}前）`,
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

export function activityView(activity, { runState = "", nowMs = Date.now() } = {}) {
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
  const terminalRun = runState === "success" || runState === "failed";
  if (terminalRun || rawState === "finished") {
    // Terminal runs keep at most a compact 最后活动 record for diagnosis: the live visuals
    // (dot animation, elapsed ticking, staleness nagging) are gone. Legacy terminal runs
    // without any preserved record stay hidden.
    const recent = recentRows(record || {});
    const stamp = record && typeof record.updated_at === "string" ? absoluteStamp(record.updated_at) : "";
    if (!recent.length && !stamp) {
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
      freshnessText: stamp ? `最后活动：${stamp}` : "",
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
      elapsedText: Number.isFinite(startedAt) ? `已进行 ${formatDuration(Math.max(0, nowMs - startedAt))}` : "",
      note: "",
    };
  }
  if (rawState === "queued") {
    return {
      ...base,
      state: "queued",
      stateText: ACTIVITY_STATE_TEXT.queued,
      note: "已进入队列，等待开始。",
    };
  }
  // waiting | none | anything without a usable record: state the gap explicitly and never
  // infer a CAD action from the stage name.
  return {
    ...base,
    state: rawState === "waiting" ? "waiting" : "none",
    stateText: rawState === "waiting" ? ACTIVITY_STATE_TEXT.waiting : ACTIVITY_STATE_TEXT.none,
    note: runState === "running" ? "运行进行中，但暂无详细进度可用（未上报实时活动）。" : "暂无详细进度可用。",
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
  if (view.state === "busy" && view.stage && stageId && view.stage === stageId) return "live";
  return "note";
}
