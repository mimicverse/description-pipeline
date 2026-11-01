// The pinned MuJoCo binding runs in this worker, isolated from the page's DOM and CSP.
import loadMujoco from "/static/vendor/mujoco.js";
let mj, model, data, joints, playing = false, last = performance.now(), initialized = false;
let appliedRequest = 0;
const filesRoot = "/delivery";
function state() {
  mj.mj_forward(model, data);
  const contacts = [];
  for (let index = 0; index < data.ncon; index++) {
    const contact = data.contact.get(index);
    contacts.push({ position: Array.from(contact.pos), normal: Array.from(contact.frame).slice(0, 3) });
    contact.delete();
  }
  const snapshot = { time: data.time, contacts, playing };
  for (const name of ["geom_xpos", "geom_xmat", "xpos", "xmat", "xipos", "xanchor", "xaxis", "site_xpos", "site_xmat", "qpos"]) {
    snapshot[name] = data[name].slice();
  }
  return snapshot;
}
function geometry(index) {
  const type = model.geom_type[index], kinds = mj.mjtGeom;
  const record = { type: Object.keys(kinds).find((name) => kinds[name]?.value === type),
    size: Array.from(model.geom_size.subarray(3 * index, 3 * index + 3)),
    collision: model.geom_contype[index] !== 0 || model.geom_conaffinity[index] !== 0,
    rgba: Array.from(model.geom_rgba.subarray(4 * index, 4 * index + 4)),
    link: mj.mj_id2name(model, mj.mjtObj.mjOBJ_BODY.value, model.geom_bodyid[index]),
  };
  if (type !== kinds.mjGEOM_MESH.value) return record;
  const mesh = model.geom_dataid[index], begin = model.mesh_vertadr[mesh];
  record.positions = model.mesh_vert.slice(3 * begin, 3 * (begin + model.mesh_vertnum[mesh]));
  if (record.collision) {
    const graph = model.mesh_graphadr[mesh];
    if (graph < 0) throw new Error("Collision mesh has no compiled convex hull");
    const vertices = model.mesh_graph[graph], faces = model.mesh_graph[graph + 1];
    const start = graph + 2 + 3 * vertices + 3 * faces;
    record.indices = model.mesh_graph.slice(start, start + 3 * faces);
  } else {
    const begin = model.mesh_faceadr[mesh];
    record.indices = model.mesh_face.slice(3 * begin, 3 * (begin + model.mesh_facenum[mesh]));
  }
  return record;
}
function publish() { self.postMessage({ type: "state", state: state(), appliedRequest }); }
function step(count) {
  for (let index = 0; index < count; index++) mj.mj_step(model, data);
  if (![...data.qpos, ...data.qvel].every(Number.isFinite)) throw new Error("Simulation produced a non-finite state");
}
self.onmessage = async ({ data: message }) => {
  try {
    if (message.type === "initialize") {
      if (initialized) throw new Error("Worker model initialization was repeated");
      initialized = true;
      mj = await loadMujoco();
      if (mj.mj_versionString() !== message.engine.mujoco) throw new Error("Preview engine differs from the verification engine");
      for (const [name, bytes] of message.files) {
        const path = `${filesRoot}/${name}`;
        mj.FS.mkdirTree(path.slice(0, path.lastIndexOf("/")));
        mj.FS.writeFile(path, bytes);
      }
      model = mj.MjModel.from_xml_path(`${filesRoot}/${message.entry}`);
      data = new mj.MjData(model);
      const geoms = Array.from({ length: model.ngeom }, (_, index) => geometry(index));
      joints = [];
      for (let index = 0; index < model.njnt; index++) {
        const type = model.jnt_type[index];
        if (![mj.mjtJoint.mjJNT_HINGE.value, mj.mjtJoint.mjJNT_SLIDE.value].includes(type)) continue;
        const name = mj.mj_id2name(model, mj.mjtObj.mjOBJ_JOINT.value, index);
        const limited = message.jointLimits[name];
        if (typeof limited !== "boolean") throw new Error("Source joint limit flag is unavailable");
        const angular = type === mj.mjtJoint.mjJNT_HINGE.value;
        joints.push({ index, q: model.jnt_qposadr[index],
          name,
          type: angular ? (limited ? "revolute" : "continuous") : "prismatic",
          lower: model.jnt_range[2 * index], upper: model.jnt_range[2 * index + 1],
          resetValue: model.qpos0[model.jnt_qposadr[index]],
        });
      }
      self.postMessage({ type: "model", geoms, joints, bodies: model.nbody - 1, sites: model.nsite, memory_bytes: data.qpos.buffer.byteLength, state: state() },
        geoms.flatMap((geom) => geom.positions ? [geom.positions.buffer, geom.indices.buffer] : []));
    } else {
      if (!data) throw new Error("Preview model is not initialized");
      if (message.type === "play") { playing = !playing; last = performance.now(); }
      else if (message.type === "step") { playing = false; step(1); }
      else if (message.type === "reset") { playing = false; mj.mj_resetData(model, data); }
      else if (message.type === "joint") {
        const joint = joints.find((joint) => joint.name === message.name);
        if (!joint || !Number.isFinite(message.value)) throw new Error("Invalid preview joint command");
        playing = false; data.qpos[joint.q] = message.value; data.qvel.fill(0);
      } else if (message.type === "joint-reset") {
        const joint = joints.find((joint) => joint.name === message.name);
        if (!joint) throw new Error("Unknown preview joint");
        playing = false; data.qpos[joint.q] = model.qpos0[joint.q]; data.qvel.fill(0);
      } else throw new Error("Unknown preview command");
      appliedRequest = message.requestId;
      publish();
    }
  } catch (error) { playing = false; self.postMessage({ type: "error", requestId: message.requestId, message: String(error.message || error) }); }
};
setInterval(() => {
  if (!playing || !data) return;
  try {
    const now = performance.now();
    const count = Math.min(32, Math.max(1, Math.floor(Math.min(0.03, (now - last) / 1000) / model.opt.timestep)));
    last = now; step(count); publish();
  } catch (error) { playing = false; self.postMessage({ type: "error", message: String(error.message || error) }); }
}, 16);
