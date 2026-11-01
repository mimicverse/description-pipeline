// Execute the production adapter and worker command handlers with a delayed transport.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import vm from "node:vm";
const staticDir = new URL("../../src/description_pipeline/orchestration/static/", import.meta.url);
const three = new URL("vendor/three.module.min.js", staticDir).href;
const viewerSource = readFileSync(new URL("viewer.js", staticDir), "utf8")
  .replace('"/static/vendor/three.module.min.js"', JSON.stringify(three))
  .replace('"/static/preview_logic.js"', JSON.stringify(new URL("preview_logic.js", staticDir).href));
const viewerUrl = `data:text/javascript;base64,${Buffer.from(viewerSource).toString("base64")}`;
const source = readFileSync(new URL("mjcf_viewer.js", staticDir), "utf8")
  .replace('"/static/vendor/three.module.min.js"', JSON.stringify(three))
  .replace('"/static/viewer.js"', JSON.stringify(viewerUrl));
const { loadMJCF, compiledGeometry, frameCompiledGeometry } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const THREE = await import(three);
const meshRecord = {type: "mjGEOM_MESH", positions: [0, 0, 0, 0.4, 0, 0, 0, 0.3, 0], indices: [0, 1, 2]};
const mesh = new THREE.Mesh(compiledGeometry(meshRecord), new THREE.MeshBasicMaterial());
mesh.position.set(4, -2, 0.4);
const floor = new THREE.Mesh(new THREE.PlaneGeometry(1000, 1000), new THREE.MeshBasicMaterial());
const fitView = {robotRoot: new THREE.Group(), camera: new THREE.PerspectiveCamera(45, 0.25, 0.01, 500),
  orbit: {target: new THREE.Vector3()}, resize() {}, updateCamera() {
    this.camera.position.copy(this.orbit.target).add(new THREE.Vector3(0, 0, this.orbit.radius));
    this.camera.lookAt(this.orbit.target); this.camera.updateMatrixWorld();
  }};
fitView.robotRoot.add(mesh, floor);
frameCompiledGeometry(fitView, [{object: mesh, record: meshRecord}, {object: floor, record: {type: "mjGEOM_PLANE"}}]);
assert.ok(fitView.orbit.radius < 10, "The environment floor must not determine the robot framing radius");
for (let index = 0; index < 3; index++) {
  const projected = new THREE.Vector3().fromBufferAttribute(mesh.geometry.attributes.position, index)
    .applyMatrix4(mesh.matrixWorld).project(fitView.camera);
  assert.ok(Math.abs(projected.x) < 1 && Math.abs(projected.y) < 1 && projected.z > -1 && projected.z < 1,
    "Every native JSON mesh vertex must remain visible in a narrow viewport");
}
mesh.geometry.dispose(); mesh.material.dispose(); floor.geometry.dispose(); floor.material.dispose();
const original = { Worker: globalThis.Worker, fetch: globalThis.fetch, DOMParser: globalThis.DOMParser };
const bytes = new TextEncoder().encode("<mujoco/>");
const sha = createHash("sha256").update(bytes).digest("hex");
let transport;
class DelayedWorker {
  constructor() {
    transport = this; this.queue = [];
    const self = { postMessage: (data) => this.onmessage({ data }) };
    this.context = vm.createContext({ self, performance, setInterval() {}, console });
    const workerSource = readFileSync(new URL("mjcf_worker.js", staticDir), "utf8")
      .replace('import loadMujoco from "/static/vendor/mujoco.js";', "");
    vm.runInContext(`${workerSource}\n
      data = {ncon: 0, time: 0, qpos: new Float64Array([0.5]), qvel: new Float64Array([0])};
      for (const name of ["geom_xpos", "geom_xmat", "xpos", "xmat", "xipos", "xanchor", "xaxis", "site_xpos", "site_xmat"])
        data[name] = new Float64Array(0);
      model = {qpos0: new Float64Array([0.125])};
      joints = [{name: "test_joint", q: 0}];
      mj = {mj_forward() {}, mj_resetData(model, data) {data.qpos.set(model.qpos0);}};
    `, this.context);
  }
  postMessage(message) {
    if (message.type !== "initialize") { this.queue.push(message); return; }
    this.onmessage({ data: {type: "model", geoms: [], bodies: 0, sites: 0, memory_bytes: 8,
      joints: [{name: "test_joint", q: 0, index: 0, type: "revolute", lower: -1, upper: 1, resetValue: 0.125}],
      state: vm.runInContext("state()", this.context)} });
  }
  async acknowledge() { await this.context.self.onmessage({data: this.queue.shift()}); }
  snapshot() { vm.runInContext("publish()", this.context); }
  terminate() { this.terminated = true; }
}
try {
  globalThis.Worker = DelayedWorker;
  globalThis.DOMParser = class { parseFromString() { return {querySelector: () => null, querySelectorAll: () => []}; } };
  globalThis.fetch = async () => ({ok: true, arrayBuffer: async () => bytes.buffer.slice(0)});
  const view = {robotRoot: new THREE.Group(), grid: {}, camera: new THREE.PerspectiveCamera(),
    orbit: {target: new THREE.Vector3()}, resize() {}, updateCamera() {}};
  const loaded = await loadMJCF(view, {descriptor: {entry: "mjcf/robot.xml", files: {"mjcf/robot.xml": sha}},
    artifactUrl: (path) => path});
  const joint = loaded.joints[0];
  joint.set(0.25); transport.snapshot();
  assert.equal(joint.value, 0.5); assert.equal(joint.requestedValue, 0.25);
  joint.set(0.75); await transport.acknowledge();
  assert.equal(joint.value, 0.25); assert.equal(joint.requestedValue, 0.75, "An older acknowledgement cannot clear a newer request");
  transport.snapshot(); assert.equal(joint.requestedValue, 0.75);
  await transport.acknowledge(); assert.equal(joint.value, 0.75); assert.equal(joint.requestedValue, undefined);
  joint.reset(); transport.snapshot(); assert.equal(joint.requestedValue, 0.125);
  await transport.acknowledge(); assert.equal(joint.value, 0.125); assert.equal(joint.requestedValue, undefined);
  joint.set(0.6); await transport.acknowledge(); loaded.reset();
  assert.equal(joint.value, 0.6); assert.equal(joint.requestedValue, 0.125);
  await transport.acknowledge(); assert.equal(joint.value, 0.125); assert.equal(joint.requestedValue, undefined);
  joint.set(NaN); joint.set(0.4); await transport.acknowledge();
  assert.equal(joint.value, 0.125); assert.equal(joint.requestedValue, 0.4, "A failed older command cannot erase the newer intent");
  await transport.acknowledge(); assert.equal(joint.value, 0.4); assert.equal(joint.requestedValue, undefined);
  joint.set(NaN); await transport.acknowledge(); assert.equal(joint.requestedValue, undefined);
  joint.set(0.8); loaded.dispose(); assert.equal(joint.requestedValue, undefined); assert.equal(transport.terminated, true);
  console.log("MJCF adapter/worker delayed request, ordered acknowledgement and native reset checks passed");
} finally { Object.assign(globalThis, original); }
