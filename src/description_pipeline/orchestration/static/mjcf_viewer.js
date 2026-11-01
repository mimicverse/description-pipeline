// Draw engine-produced geometry and poses. Physics stays in the dedicated MuJoCo worker.
import * as THREE from "/static/vendor/three.module.min.js";
import { disposeRobot, frameBounds } from "/static/viewer.js";
const hex = (buffer) => [...new Uint8Array(buffer)].map((n) => n.toString(16).padStart(2, "0")).join("");
function geometry(record) {
  const s = record.size;
  if (record.type === "mjGEOM_MESH") {
    const result = new THREE.BufferGeometry();
    result.setAttribute("position", new THREE.BufferAttribute(record.positions, 3));
    result.setIndex(Array.from(record.indices)); result.computeVertexNormals(); return result;
  }
  if (record.type === "mjGEOM_BOX") return new THREE.BoxGeometry(...s.map((n) => 2 * n));
  if (record.type === "mjGEOM_SPHERE") return new THREE.SphereGeometry(s[0], 24, 16);
  if (record.type === "mjGEOM_CAPSULE") return new THREE.CapsuleGeometry(s[0], 2 * s[1], 8, 16).rotateX(Math.PI / 2);
  if (record.type === "mjGEOM_CYLINDER") return new THREE.CylinderGeometry(s[0], s[0], 2 * s[1], 24).rotateX(Math.PI / 2);
  if (record.type === "mjGEOM_PLANE") return new THREE.PlaneGeometry(2 * s[0], 2 * s[1]);
  throw new Error(`Unsupported compiled geometry: ${record.type}`);
}
export async function loadMJCF(viewer, { descriptor, artifactUrl, signal, onStatus }) {
  if (!descriptor.files[descriptor.entry]) throw new Error("MJCF entry is absent from the verified inventory");
  const files = [];
  for (const [name, sha] of Object.entries(descriptor.files)) {
    signal?.throwIfAborted();
    if (!/^(mjcf|meshes|urdf|model)\/[A-Za-z0-9_./-]+$/.test(name) || name.split("/").includes("..")) throw new Error("MJCF resource path is invalid");
    const response = await fetch(artifactUrl(name), { signal });
    if (!response.ok) throw new Error(`MJCF resource unavailable: ${name}`);
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (hex(await crypto.subtle.digest("SHA-256", bytes)) !== sha) throw new Error(`MJCF resource digest mismatch: ${name}`);
    files.push([name, bytes]);
  }
  signal?.throwIfAborted();
  const robot = files.find(([name]) => name === "mjcf/robot.xml");
  if (!robot) throw new Error("Verified robot XML is unavailable");
  const xml = new DOMParser().parseFromString(new TextDecoder().decode(robot[1]), "application/xml");
  if (xml.querySelector("parsererror")) throw new Error("MJCF XML is malformed");
  const jointLimits = {};
  for (const joint of xml.querySelectorAll("worldbody joint")) {
    const name = joint.getAttribute("name"), limited = joint.getAttribute("limited");
    if (!name || name in jointLimits || !["true", "false"].includes(limited)) throw new Error("MJCF joint limit flags are not explicit");
    jointLimits[name] = limited === "true";
  }
  const worker = new Worker("/static/mjcf_worker.js", { type: "module" });
  let disposed = false, mode = "visual", markers = false, selectedLink = "", snapshot, definition;
  let requestId = 0;
  const pendingJoints = new Map();
  const objects = [], helpers = [];
  const matrix = new THREE.Matrix4();
  const cleanup = () => { if (!disposed) { disposed = true; pendingJoints.clear(); worker.terminate(); } };
  const send = (message, requested = []) => {
    if (disposed) return;
    const id = ++requestId;
    for (const [name, value] of requested) pendingJoints.set(name, { id, value });
    worker.postMessage({ ...message, requestId: id });
  };
  const abort = () => cleanup(); signal?.addEventListener("abort", abort, { once: true });
  try {
    const initial = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("MJCF preview compilation timed out")), 60000);
      const done = (callback, value) => { clearTimeout(timeout); signal?.removeEventListener("abort", cancelled); callback(value); };
      const cancelled = () => done(reject, new DOMException("Preview cancelled", "AbortError"));
      signal?.addEventListener("abort", cancelled, { once: true });
      worker.onerror = (event) => done(reject, new Error(event.message || "MuJoCo worker failed"));
      worker.onmessage = ({ data }) => data.type === "model" ? done(resolve, data) : data.type === "error" ? done(reject, new Error(data.message)) : null;
      worker.postMessage({ type: "initialize", engine: descriptor.engine, entry: descriptor.entry, jointLimits, files },
        files.map(([, bytes]) => bytes.buffer));
    });
    signal?.throwIfAborted();
    signal?.removeEventListener("abort", abort);
    definition = { bodies: initial.bodies }; snapshot = initial.state;
    disposeRobot(viewer); viewer.modelDispose = cleanup; viewer.grid.visible = false;
    for (const record of initial.geoms) {
      const environment = record.type === "mjGEOM_PLANE";
      const color = record.collision && !environment ? new THREE.Color(1, 0.42, 0.08) : new THREE.Color(...record.rgba.slice(0, 3));
      const object = new THREE.Mesh(geometry(record), new THREE.MeshStandardMaterial({ color, side: THREE.DoubleSide, roughness: 0.75 }));
      viewer.robotRoot.add(object); objects.push({ object, record, environment });
    }
    for (let body = 1; body <= initial.bodies; body++) {
      const axes = new THREE.AxesHelper(0.04);
      const com = new THREE.Mesh(new THREE.SphereGeometry(0.004, 8, 6), new THREE.MeshBasicMaterial({ color: 0xb51bcd }));
      viewer.robotRoot.add(axes, com); helpers.push({ body, axes, com });
    }
    for (const joint of initial.joints) {
      const axis = new THREE.ArrowHelper(new THREE.Vector3(0, 0, 1), new THREE.Vector3(), 0.06, 0x1d6aa8);
      viewer.robotRoot.add(axis); helpers.push({ joint: joint.index, axis });
    }
    for (let site = 0; site < initial.sites; site++) {
      const object = new THREE.AxesHelper(0.025); viewer.robotRoot.add(object); helpers.push({ site, object });
    }
    const contactGeometry = new THREE.BufferGeometry();
    const contactLines = new THREE.LineSegments(contactGeometry, new THREE.LineBasicMaterial({ color: 0xd61d24 }));
    viewer.robotRoot.add(contactLines);
    function pose(object, positions, rotations, index) {
      const p = positions.subarray(3 * index, 3 * index + 3), r = rotations.subarray(9 * index, 9 * index + 9);
      matrix.set(r[0], r[1], r[2], p[0], r[3], r[4], r[5], p[1], r[6], r[7], r[8], p[2], 0, 0, 0, 1);
      object.matrixAutoUpdate = false; object.matrix.copy(matrix);
    }
    function sync() {
      if (disposed) return;
      objects.forEach(({ object, record, environment }, index) => {
        pose(object, snapshot.geom_xpos, snapshot.geom_xmat, index);
        object.visible = environment || mode === "overlay" || (record.collision ? mode === "collision" : mode === "visual");
        object.material.opacity = mode === "overlay" && !environment ? (record.collision ? 0.55 : 0.3) : record.rgba[3];
        if (selectedLink && record.link !== selectedLink && !environment) object.material.opacity *= 0.12;
        object.material.transparent = object.material.opacity < 1;
      });
      for (const h of helpers) {
        for (const object of [h.axes, h.com, h.axis, h.object].filter(Boolean)) object.visible = markers;
        if (h.body !== undefined) { pose(h.axes, snapshot.xpos, snapshot.xmat, h.body); h.com.position.fromArray(snapshot.xipos, 3 * h.body); }
        if (h.joint !== undefined) { h.axis.position.fromArray(snapshot.xanchor, 3 * h.joint); h.axis.setDirection(new THREE.Vector3().fromArray(snapshot.xaxis, 3 * h.joint)); }
        if (h.site !== undefined) pose(h.object, snapshot.site_xpos, snapshot.site_xmat, h.site);
      }
      const lines = [];
      if (markers) for (const { position: p, normal: n } of snapshot.contacts) lines.push(...p, ...p.map((v, k) => v + 0.03 * n[k]));
      contactGeometry.setAttribute("position", new THREE.Float32BufferAttribute(lines, 3));
      contactLines.visible = markers;
      onStatus?.({ time: snapshot.time, contacts: snapshot.contacts.length, playing: snapshot.playing, bodies: definition.bodies, geoms: objects.length });
    }
    worker.onmessage = ({ data }) => {
      if (disposed) return;
      if (data.type === "state") {
        snapshot = data.state;
        for (const [name, request] of pendingJoints) if (request.id <= data.appliedRequest) pendingJoints.delete(name);
        sync();
      }
      if (data.type === "error") {
        for (const [name, request] of pendingJoints) if (data.requestId === undefined || request.id <= data.requestId) pendingJoints.delete(name);
        onStatus?.({ error: data.message });
      }
    };
    worker.onerror = (event) => { pendingJoints.clear(); onStatus?.({ error: event.message || "MuJoCo worker failed" }); };
    sync(); viewer.robotRoot.updateMatrixWorld(true); viewer.resize();
    const fit = () => {
      const bounds = new THREE.Box3();
      for (const { object, environment } of objects) if (!environment) bounds.union(new THREE.Box3().setFromObject(object));
      frameBounds(viewer, bounds);
    };
    fit();
    return {
      fit, engineMemoryBytes: initial.memory_bytes,
      links: [...new Set(initial.geoms.filter((geom) => geom.collision && geom.type !== "mjGEOM_PLANE").map((geom) => geom.link))],
      joints: initial.joints.map((joint) => ({ ...joint,
        get value() { return snapshot.qpos[joint.q]; },
        get requestedValue() { return pendingJoints.get(joint.name)?.value; },
        set(value) { send({ type: "joint", name: joint.name, value }, [[joint.name, value]]); },
        reset() { send({ type: "joint-reset", name: joint.name }, [[joint.name, joint.resetValue]]); },
      })),
      selectLink(value) { selectedLink = value; sync(); }, setMode(value) { mode = value; sync(); },
      setMarkers(value) { markers = value; sync(); }, togglePlay() { send({ type: "play" }); },
      step() { send({ type: "step" }); },
      reset() { send({ type: "reset" }, initial.joints.map((joint) => [joint.name, joint.resetValue])); },
      dispose: cleanup,
    };
  } catch (error) {
    signal?.removeEventListener("abort", abort); cleanup();
    if (viewer.modelDispose === cleanup) disposeRobot(viewer);
    throw error;
  }
}
