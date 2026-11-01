import * as THREE from "/static/vendor/three.module.min.js";
import { createViewer, disposeRobot } from "/static/viewer.js";
import { compiledGeometry, frameCompiledGeometry } from "/static/mjcf_viewer.js";
import { createProfileEditor } from "/static/pico_profile.js";
import { downloadLinkSetup } from "/static/pico_link.js";
const $ = (id) => document.getElementById(id);
const run = new URL(location.href).searchParams.get("run");
const launchSaved = new URL(location.href).searchParams.get("launch") === "1";
history.replaceState(null, "", `/static/pico.html?run=${encodeURIComponent(run || "")}`);
let csrf, sessionId, socket, heartbeat, profileEditor, objects = [], targetObjects = [], mode = "visual";
const pendingKey = `pico-request:${run}`;
let pending = JSON.parse(sessionStorage.getItem(pendingKey) || "null");
const viewer = createViewer($("scene"));
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "", ...options.headers } });
  const result = await response.json();
  if (!response.ok) { const error = new Error(result.error || `HTTP ${response.status}`); error.status = response.status; throw error; }
  return result;
}
function failure(error) { $("error").textContent = error.message; }
function showWarnings(inventory, scope) {
  const certificate = inventory.certificate;
  const lines = [
    `${inventory.count} warnings · ${inventory.shown} displayed · ${inventory.omitted} not expanded.`,
    `Complete inventory: ${certificate.path} (${certificate.bytes} bytes).`,
    `Certificate SHA-256: ${certificate.sha256}`,
    `Source artifact: ${certificate.artifact.artifact_id} · Scope: ${scope}`,
    ...inventory.items.map((item) => `${item.excerpt ? "[Excerpt]" : "[Full record]"} ${item.text}`),
  ];
  $("warnings").textContent = lines.join("\n\n");
}
function pendingControls() {
  for (const input of $("profile-editor").querySelectorAll("input, select, textarea, button")) input.disabled = Boolean(pending) || input.dataset.sourceFixed === "true";
  $("record").disabled = Boolean(pending);
  if (!pending) $("launch").textContent = "Start Session";
}
function draw(state) {
  const matrix = new THREE.Matrix4();
  objects.forEach(({ object, record }, index) => {
    const p = state.geom_xpos.slice(index * 3, index * 3 + 3), r = state.geom_xmat.slice(index * 9, index * 9 + 9);
    matrix.set(r[0], r[1], r[2], p[0], r[3], r[4], r[5], p[1], r[6], r[7], r[8], p[2], 0, 0, 0, 1);
    object.matrixAutoUpdate = false; object.matrix.copy(matrix);
    object.visible = record.type === "mjGEOM_PLANE" || mode === "overlay" || (record.collision ? mode === "collision" : mode === "visual");
    object.material.opacity = mode === "overlay" ? (record.collision ? 0.55 : 0.3) : record.rgba[3];
    object.material.transparent = object.material.opacity < 1;
  });
  const { report, session_state, calibrated, calibration_state, collision_available, time, qpos } = state;
  $("live-status").textContent = `${report?.pose_mode || "unknown"} IK · ${session_state} · ${calibrated ? "Calibrated" : calibration_state}\n${report?.input_status || "No input"} · Emergency stop ${report?.estop_latched ? "LATCHED" : "clear"}\nCollision ${collision_available ? "geometry available" : "UNKNOWN — inspection only"}`;
  $("arms").replaceChildren();
  const errorMetric = (value, scale, digits, unit) => Number.isFinite(value) ? `${(value * scale).toFixed(digits)} ${unit}` : "unknown";
  const xyz = (pose) => pose?.position_m?.map((v) => v.toFixed(4)).join(", ") || "Unavailable";
  for (const [side, arm] of Object.entries(report?.arms || {})) {
    const card = document.createElement("div"); card.className = "arm";
    const title = document.createElement("h3"); title.textContent = `${side.toUpperCase()} · ${arm.state}`; card.append(title);
    for (const text of [
      `Clutch ${arm.clutched ? "held" : "released"} · collision ${arm.collision_state}`,
      `Requested [m]: ${xyz(arm.requested_pose)}`,
      `Admitted [m]: ${xyz(arm.solver?.target)}`,
      `Applied [m]: ${xyz(arm.applied_pose)}`,
      `Tracking error: ${errorMetric(arm.applied_position_error_m, 1000, 2, "mm")} · ${errorMetric(arm.applied_orientation_error_rad, 1, 4, "rad")}`,
      `Solver: ${arm.solver?.status || "Unavailable"} · rank ${arm.solver?.jacobian_rank ?? "unknown"}`,
    ]) { const row = document.createElement("p"); row.textContent = text; card.append(row); }
    $("arms").append(card);
  }
  $("head").textContent = report?.head ? `Head: ${JSON.stringify(report.head)}` : "Head: awaiting a report or disabled by profile";
  for (const { object, side, field } of targetObjects) {
    const arm = report?.arms?.[side];
    const pose = field === "admitted" ? arm?.solver?.target : arm?.[field];
    object.visible = Boolean(pose);
    if (pose) { object.position.fromArray(pose.position_m); const q = pose.quaternion_wxyz; object.quaternion.set(q[1], q[2], q[3], q[0]); }
  }
  $("state").textContent = JSON.stringify({ session_state, calibrated, calibration_state, collision_available, time, qpos, report }, null, 2);
}
function model(message) {
  disposeRobot(viewer); objects = []; targetObjects = []; viewer.grid.visible = false;
  for (const record of message.geoms) {
    const color = record.collision ? new THREE.Color(1, 0.42, 0.08) : new THREE.Color(...record.rgba.slice(0, 3));
    const object = new THREE.Mesh(compiledGeometry(record), new THREE.MeshStandardMaterial({ color, side: THREE.DoubleSide, roughness: 0.75 }));
    viewer.robotRoot.add(object); objects.push({ object, record });
  }
  for (const side of ["left", "right"]) for (const [field, color] of [["requested_pose", 0xd82c3a], ["admitted", 0xe59b21], ["applied_pose", 0x20a070]]) {
    const object = new THREE.Group();
    object.add(new THREE.Mesh(new THREE.SphereGeometry(0.004, 8, 8), new THREE.MeshBasicMaterial({ color })), new THREE.AxesHelper(0.025));
    viewer.robotRoot.add(object); targetObjects.push({ object, side, field });
  }
  draw(message.state); frameCompiledGeometry(viewer, objects);
}
async function connect() {
  socket?.close(); clearInterval(heartbeat);
  const channel = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/simulations/${sessionId}/stream`);
  socket = channel;
  channel.onopen = () => {
    if (socket !== channel) return;
    channel.send(JSON.stringify({ type: "authenticate", csrf_token: csrf }));
    heartbeat = setInterval(() => { if (channel.readyState === 1) channel.send(JSON.stringify({ type: "heartbeat" })); }, 500);
  };
  channel.onmessage = ({ data }) => {
    if (socket !== channel) return;
    try {
      const message = JSON.parse(data);
      if (message.type === "model") { model(message); $("status").textContent = "Native worker connected. Release controllers and hold a stable neutral pose to calibrate."; }
      else if (message.type === "state") draw(message.state);
    } catch (error) { failure(error); channel.close(); }
  };
  channel.onclose = async () => {
    if (socket !== channel) return;
    clearInterval(heartbeat);
    $("status").textContent = "State stream disconnected. Input is revoked; refresh to inspect or reconnect.";
    try {
      const row = await inspect();
      if (["stopped", "failed", "unresolved"].includes(row.state)) {
        for (const name of ["estop", "stop", "pair"]) $(name).disabled = true;
        $("status").textContent = `Session ${row.state}.`;
        if (row.error) failure(new Error(row.error));
        $("setup").hidden = false; $("launch").disabled = false;
      }
    } catch (error) { failure(error); }
  };
}
async function inspect() {
  const row = await api(`/api/simulations/${sessionId}`);
  $("link-setup").disabled = !["starting", "ready"].includes(row.state);
  $("source").textContent = JSON.stringify({ session_id: row.session_id, request_digest: row.request_digest, source: row.source, runtime: row.runtime }, null, 2);
  showWarnings(row.source.warning_inventory, row.source.verification_scope);
  $("recording").hidden = !row.recording_artifact;
  $("verify-recording").hidden = !row.recording_artifact;
  if (row.recording_artifact) { $("recording").hidden = false; $("recording").href = `/api/simulations/${sessionId}/recording`; }
  const verification = row.verification;
  if (verification?.state === "unknown") $("verification-status").textContent = "Recording check is unresolved. Its capacity and partial inputs are retained; retry to reconcile after the process exits.";
  else if (["starting", "running"].includes(verification?.state)) $("verification-status").textContent = "Recording check is running.";
  else if (["failed", "cancelled"].includes(verification?.state)) $("verification-status").textContent = `Recording check ${verification.state}: ${verification.error || "No result available"}`;
  else if (verification?.state === "completed") showVerification(verification.value);
  return row;
}
function showVerification(result) {
  $("verification-status").textContent = `${result.recording_complete ? "Complete" : "Partial"} recording: ${result.ticks} ticks checked against the registered source. Control-input replay is not covered.`;
}
async function command(type) {
  const result = await api(`/api/simulations/${sessionId}/commands`, { method: "POST", body: JSON.stringify({ type, command_id: crypto.randomUUID() }) });
  if (type === "pair") $("pairing").textContent = `One-time code (60 s): ${result.pairing_code}. Session: ${sessionId}. Enter this code into the local PICO link.`;
  if (type === "stop") { socket?.close(); const row = await inspect(); $("status").textContent = `Session ${row.state}.`;
    if (row.error) failure(new Error(row.error));
    for (const name of ["estop", "stop", "pair"]) $(name).disabled = true;
    $("pairing").textContent = "";
  }
  if (type === "estop") $("status").textContent = "Emergency stop requested; inspect the live latched state.";
}
$("verify-recording").onclick = async () => {
  $("verify-recording").disabled = true;
  $("verification-status").textContent = "Checking the registered recording and source…";
  try {
    const result = await api(`/api/simulations/${sessionId}/recording/verify`, { method: "POST" });
    showVerification(result);
  } catch (error) { $("verification-status").textContent = "Recording check failed."; failure(error); }
  finally { $("verify-recording").disabled = false; try { await inspect(); } catch (error) { failure(error); } }
};
$("mode").onchange = () => { mode = $("mode").value; };
$("link-setup").onclick = async () => {
  try {
    downloadLinkSetup(location.origin, await inspect());
    $("link-help").textContent = "Open this setup file with the installed pico-link launcher, then enter a fresh code from Pair PICO Link. The setup file contains no pairing code.";
  } catch (error) { failure(error); }
};
for (const type of ["estop", "stop", "pair"]) $(type).onclick = () => command(type).catch(failure);
$("setup").onsubmit = async (event) => {
  event.preventDefault(); $("error").textContent = ""; $("launch").disabled = true;
  try {
    const entered = pending?.body || { ...profileEditor.read(), record: $("record").checked };
    if (!pending) { pending = { key: crypto.randomUUID(), body: entered }; sessionStorage.setItem(pendingKey, JSON.stringify(pending)); }
    pendingControls();
    const row = await api(`/api/runs/${encodeURIComponent(run)}/simulations`, { method: "POST", headers: { "Idempotency-Key": pending.key }, body: JSON.stringify(pending.body) });
    sessionStorage.removeItem(pendingKey); pending = null; pendingControls();
    sessionId = row.session_id; sessionStorage.setItem(`pico:${run}`, sessionId);
    const status = await inspect();
    if (!["starting", "ready"].includes(status.state)) throw new Error(status.error || `Session ${status.state}`);
    await connect();
    for (const type of ["estop", "stop", "pair"]) $(type).disabled = false;
    $("setup").hidden = true;
  } catch (error) {
    if ([400, 403, 404].includes(error.status)) { sessionStorage.removeItem(pendingKey); pending = null; }
    pendingControls();
    failure(error); $("launch").disabled = false;
    $("launch").textContent = pending ? "Retry Original Request" : "Start Session";
    if (pending) $("status").textContent = "Launch outcome is unknown. Retry the original request to reconcile its result.";
  }
};
async function initialize() {
  if (!run) throw new Error("Select a source run from its MJCF result first");
  csrf = (await api("/api/session")).csrf_token;
  if (!(await api("/api/simulation-capability")).available) throw new Error("The native Teleop worker is not configured on this portal");
  const saved = await api(`/api/runs/${encodeURIComponent(run)}/simulation-profile`);
  $("source").textContent = JSON.stringify({ subject_sha256: saved.subject_sha256, robot_name: saved.model.robot_name }, null, 2);
  showWarnings(saved.warning_inventory, saved.verification_scope);
  profileEditor = createProfileEditor($("profile-editor"), saved.model, saved.subject_sha256, failure);
  if (saved.saved) { profileEditor.load(saved.saved); $("record").checked = saved.saved.record; }
  $("status").textContent = `Source ${saved.model.robot_name}. Review the profile and software execution budgets.`;
  const previous = sessionStorage.getItem(`pico:${run}`);
  if (pending) {
    profileEditor.load(pending.body);
    $("record").checked = pending.body.record;
    $("launch").textContent = "Retry Original Request";
    $("status").textContent = "Launch outcome is unknown. Retry the original request to reconcile its result.";
  }
  pendingControls();
  if (previous) {
    sessionId = previous;
    try {
      const row = await inspect();
      if (["starting", "ready"].includes(row.state)) {
        $("setup").hidden = true; await connect();
        for (const type of ["estop", "stop", "pair"]) $(type).disabled = false;
      }
    } catch (error) {
      if (![403, 404].includes(error.status)) throw error;
      sessionStorage.removeItem(`pico:${run}`); sessionId = null;
    }
  }
  $("launch").disabled = false;
  if (launchSaved && saved.saved && !$("setup").hidden && !pending) $("setup").requestSubmit();
}
window.addEventListener("pagehide", () => { clearInterval(heartbeat); socket?.close(); });
initialize().catch(failure);
