// Exercise the production loader with actual Three geometry and test-only DOM/network seams.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
const staticDir = new URL("../../src/description_pipeline/orchestration/static/", import.meta.url);
const THREE = await import(new URL("vendor/three.module.min.js", staticDir));
const logic = await import(new URL("preview_logic.js", staticDir));
const source = readFileSync(new URL("viewer.js", staticDir), "utf8")
  .replace('"/static/vendor/three.module.min.js"', JSON.stringify(new URL("vendor/three.module.min.js", staticDir).href))
  .replace('"/static/preview_logic.js"', JSON.stringify(new URL("preview_logic.js", staticDir).href));
const { loadRobot, buildJointControls, parseStl } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
class Element {
  constructor(tagName, attributes = {}, children = []) { Object.assign(this, { tagName, attributes, children, textContent: "", listeners: {} }); }
  getAttribute(name) { return Object.hasOwn(this.attributes, name) ? this.attributes[name] : null; }
  hasAttribute(name) { return Object.hasOwn(this.attributes, name); }
  querySelectorAll(selector) { return this.children.filter((node) => node.tagName === selector.replace(":scope > ", "")); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  append(...items) { this.children.push(...items); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  set value(value) { this._value = this.type === "range" ? String(Math.max(Number(this.min), Math.min(Number(this.max), Number(value)))) : value; }
  get value() { return this._value; }
}
const e = (tag, attrs = {}, ...children) => new Element(tag, attrs, children);
const find = (node, tag) => node.children.find((item) => item.tagName === tag);
function model() {
  return e("robot", { name: "neutral_robot" },
    e("link", { name: "base" }, e("visual", {}, e("geometry", {}, e("box", { size: "1 2 3" })))),
    e("link", { name: "moving" }),
    e("joint", { name: "axis_joint", type: "revolute" }, e("parent", { link: "base" }), e("child", { link: "moving" }),
      e("limit", { effort: "0", velocity: "0" })),
  );
}
let current;
const saved = { DOMParser: globalThis.DOMParser, fetch: globalThis.fetch, document: globalThis.document };
globalThis.DOMParser = class { parseFromString() { return { documentElement: current, querySelector: () => null }; } };
globalThis.document = { createElement: (tag) => e(tag) };
const mesh = new ArrayBuffer(134);
new DataView(mesh).setUint32(80, 1, true);
new DataView(mesh).setFloat32(96, 1, true);
new DataView(mesh).setFloat32(112, 1, true);
new DataView(mesh).setFloat32(128, 1, true);
const requests = [];
globalThis.fetch = async (url) => {
  requests.push(url);
  return { ok: true, text: async () => "fixture", arrayBuffer: async () => mesh };
};
function viewer() {
  return { robotRoot: new THREE.Group(), orbit: { target: new THREE.Vector3() }, camera: new THREE.PerspectiveCamera(), updateCamera() {}, frame() {} };
}
const files = { "urdf/robot.urdf": "registered", "meshes/exact.stl": "registered", "other/exact.stl": "registered" };
async function load(root = model()) {
  current = root;
  const view = viewer();
  return { view, result: await loadRobot(view, { urdfUrl: "/robot", urdfPath: "urdf/robot.urdf", files, artifactUrl: (name) => `/artifact/${name}` }) };
}
const joint = (root) => find(root, "joint");
const visual = (root) => find(find(root, "link"), "visual");
const geometry = (root) => find(visual(root), "geometry");
let cases = 0;
async function rejected(change, pattern) {
  const root = model(); change(root);
  await assert.rejects(() => load(root), pattern);
  cases += 1;
}
try {
  const { result } = await load();
  assert.deepEqual(result.records.joints[0], { name: "axis_joint", type: "revolute", lower: 0, upper: 0, effort: 0, velocity: 0, axis: [1, 0, 0] });
  assert.equal(result.records.links[0].mass_kg, null);
  assert.match(result.warnings[0], /neutral preview appearance/);
  for (const number of ["0x10", "0b10", "Infinity", "NaN", "1junk", "1e999"]) {
    assert.throws(() => logic.parseUrdfNumber(number, "number"), /finite decimal/);
  }
  assert.equal(logic.parseUrdfNumber("-1.2e-3", "number"), -0.0012);
  for (const attribute of ["name", "type"]) await rejected((root) => delete joint(root).attributes[attribute], /missing/);
  for (const type of ["REVOLUTE", "unknown", "planar", "floating"]) await rejected((root) => joint(root).attributes.type = type, /unsupported/);
  await rejected((root) => root.children.push(joint(root)), /duplicated/);
  await rejected((root) => root.children.push(e("link", { name: "base" })), /duplicated/);
  await rejected((root) => root.children.push(e("link", { name: "disconnected" })), /one root/);
  for (const tag of ["parent", "child", "limit"]) await rejected((root) => joint(root).children = joint(root).children.filter((node) => node.tagName !== tag), /missing|exactly one/);
  await rejected((root) => find(joint(root), "child").attributes.link = "unknown", /bindings/);
  await rejected((root) => find(joint(root), "child").attributes.link = "base", /bindings/);
  await rejected((root) => root.children.push(e("joint", { name: "duplicate_parent", type: "fixed" }, e("parent", { link: "base" }), e("child", { link: "moving" }))), /bindings/);
  await rejected((root) => root.children.push(e("link", { name: "root" }), e("joint", { name: "cycle", type: "fixed" }, e("parent", { link: "moving" }), e("child", { link: "base" }))), /cycle/);
  {
    const root = model(); joint(root).attributes.type = "fixed";
    joint(root).children = joint(root).children.filter((node) => node.tagName !== "limit");
    const loaded = await load(root);
    assert.equal(loaded.result.joints.length, 0); assert.equal(loaded.result.records.joints[0].axis, null);
  }
  for (const xyz of [undefined, "", "0 0 0", "0 0 NaN"]) await rejected((root) => joint(root).children.push(e("axis", xyz === undefined ? {} : { xyz })), /missing|finite|non-zero/);
  for (const field of ["effort", "velocity"]) await rejected((root) => delete find(joint(root), "limit").attributes[field], /missing/);
  await rejected((root) => Object.assign(find(joint(root), "limit").attributes, { lower: "1", upper: "-1" }), /exceeds/);
  for (const field of ["effort", "velocity"]) await rejected((root) => find(joint(root), "limit").attributes[field] = "-1", /non-negative/);
  await rejected((root) => { joint(root).attributes.type = "fixed"; }, /fixed limits/);
  await rejected((root) => { joint(root).attributes.type = "fixed"; joint(root).children.push(e("axis", { xyz: "1 0 0" })); }, /fixed axis/);
  await rejected((root) => { joint(root).attributes.type = "continuous"; find(joint(root), "limit").attributes.lower = "0"; }, /continuous position/);
  await rejected((root) => joint(root).children.push(e("origin", { xyz: "" })), /three finite/);
  await rejected((root) => joint(root).children.push(e("mimic", { joint: "other" })), /unsupported/);
  for (const size of [undefined, "", "1 2 x", "1 0 1", "1 -1 1"]) await rejected((root) => geometry(root).children = [e("box", size === undefined ? {} : { size })], /missing|finite|positive/);
  for (const shape of [e("sphere"), e("cylinder", { radius: "1" })]) await rejected((root) => geometry(root).children = [shape], /missing/);
  await rejected((root) => visual(root).children = [], /exactly one geometry/);
  await rejected((root) => geometry(root).children.push(e("sphere", { radius: "1" })), /exactly one/);
  for (const rgba of ["1 1 1", "1 1 1 2", "1 1 NaN 1", "1 1 1 bad"]) await rejected((root) => visual(root).children.push(e("material", { name: "color" }, e("color", { rgba }))), /four finite|finite decimal/);
  await rejected((root) => visual(root).children.push(e("material", { name: "unknown" })), /unknown material/);
  await rejected((root) => root.children.push(e("material", { name: "same" }), e("material", { name: "same" })), /duplicated/);
  await rejected((root) => visual(root).children.push(e("material", { name: "texture" }, e("color", { rgba: "1 1 1 1" }), e("texture", { filename: "a.png" }))), /unsupported/);
  for (const alpha of ["0", "1"]) {
    const root = model(); root.children.push(e("material", { name: "named" }, e("color", { rgba: `0.1 0.2 0.3 ${alpha}` })));
    visual(root).children.push(e("material", { name: "named" }));
    const { view } = await load(root); let rendered;
    view.robotRoot.traverse((node) => { if (node.material) rendered = node.material; });
    assert.equal(rendered.opacity, Number(alpha));
  }
  await rejected((root) => find(root, "link").children.push(e("inertial")), /exactly one mass/);
  await rejected((root) => find(root, "link").children.push(e("inertial", {}, e("mass", { value: "0" }), e("inertia", { ixx: "0" }))), /missing/);
  {
    const root = model(); find(root, "link").children.push(e("inertial", {}, e("mass", { value: "0" }), e("inertia", Object.fromEntries(["ixx", "ixy", "ixz", "iyy", "iyz", "izz"].map((key) => [key, "0"])) )));
    assert.equal((await load(root)).result.records.links[0].mass_kg, 0);
  }
  for (const filename of ["exact.stl", "missing/exact.stl", "../../meshes/exact.stl", "package://robot/meshes/exact.stl", "/meshes/exact.stl", "https://host/a.stl"]) {
    await rejected((root) => geometry(root).children = [e("mesh", { filename })], /inventory|escapes|unsupported/);
  }
  {
    const root = model(); geometry(root).children = [e("mesh", { filename: "../meshes/exact.stl" })];
    await load(root); assert.equal(requests.at(-1), "/artifact/meshes/exact.stl");
    geometry(root).children[0].attributes.scale = "-1 2 3";
    const mirrored = await load(root); let mirroredMesh;
    mirrored.view.robotRoot.traverse((node) => { if (node.isMesh) mirroredMesh = node; });
    assert.deepEqual(mirroredMesh.scale.toArray(), [-1, 2, 3]);
    assert.ok(mirroredMesh.matrixWorld.determinant() < 0);
    const bounds = new THREE.Box3().setFromObject(mirroredMesh);
    assert.deepEqual(bounds.min.toArray(), [-1, 0, 0]); assert.deepEqual(bounds.max.toArray(), [0, 2, 3]);
    geometry(root).children[0].attributes.scale = "0 -2 3";
    await load(root);
    geometry(root).children[0].attributes.scale = "";
    await assert.rejects(() => load(root), /three finite/);
  }
  {
    const root = model(); joint(root).attributes.type = "continuous"; joint(root).children = joint(root).children.filter((node) => node.tagName !== "limit");
    joint(root).children.push(e("axis", { xyz: "0 0 2" }));
    const { result } = await load(root);
    assert.equal(result.records.joints[0].lower, null); assert.equal(result.records.joints[0].upper, null);
    assert.deepEqual(result.records.joints[0].axis, [0, 0, 2]);
    const container = e("container"); buildJointControls(container, result.joints);
    assert.equal(container.children[0].children[2].min, "-180");
    assert.equal(result.joints[0].value, 0);
  }
  {
    const root = model(); Object.assign(find(joint(root), "limit").attributes, { lower: "1", upper: "2" });
    const { result } = await load(root); const container = e("container"); let events = 0;
    const controls = buildJointControls(container, result.joints, { onInput: () => events++ });
    assert.equal(result.joints[0].value, 0); assert.equal(events, 0);
    const [label, readout, range] = container.children[0].children;
    assert.equal(label.textContent, "axis_joint"); assert.match(readout.textContent, /reference pose outside/);
    range.value = "90"; range.listeners.input(); assert.equal(events, 1); assert.equal(result.joints[0].value, Math.PI / 2);
    controls.reset(); assert.equal(result.joints[0].value, 0); assert.equal(events, 2);
  }
  assert.ok(parseStl(mesh).getAttribute("position"));
  console.log(`viewer parser: production load/geometry/records/controls passed; ${cases} invalid declarations rejected`);
} finally { Object.assign(globalThis, saved); }
