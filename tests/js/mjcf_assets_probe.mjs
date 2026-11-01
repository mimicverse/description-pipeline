// Load sealed assets through the production worker and the bundled official WASM engine.
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { resolve, join } from "node:path";
import { createHash } from "node:crypto";
import vm from "node:vm";
import loadMujoco from "../../src/description_pipeline/orchestration/static/vendor/mujoco.js";

const [root, bindingPath, output] = process.argv.slice(2).map((path) => resolve(path));
const {binding, jointLimits} = JSON.parse(readFileSync(bindingPath, "utf8"));
const files = Object.entries(binding.files).map(([name, record]) => {
  const bytes = readFileSync(join(root, name));
  if (bytes.length !== record.bytes || createHash("sha256").update(bytes).digest("hex") !== record.sha256)
    throw new Error(`Sealed input changed: ${name}`);
  return [name, bytes];
});
const messages = [];
const context = vm.createContext({
  self: { postMessage: (message) => messages.push(message) },
  performance, setInterval() {}, console,
  loadMujoco: () => loadMujoco({ wasmBinary: readFileSync(new URL(
    "../../src/description_pipeline/orchestration/static/vendor/mujoco.wasm", import.meta.url)) }),
});
const worker = readFileSync(new URL(
  "../../src/description_pipeline/orchestration/static/mjcf_worker.js", import.meta.url), "utf8")
  .replace('import loadMujoco from "/static/vendor/mujoco.js";', "");
vm.runInContext(worker, context);
context.request = { type: "initialize", engine: binding.engine, files,
  entry: "mjcf/robot.xml", jointLimits };
await context.self.onmessage({data: context.request});
const error = messages.find((message) => message.type === "error");
if (error) throw new Error(error.message);
const result = messages.find((message) => message.type === "model");
if (!result) throw new Error("Worker produced no compiled model");
mkdirSync(output, {recursive: true});
for (const [index, geom] of result.geoms.entries()) {
  if (!geom.positions) continue;
  for (const field of ["positions", "indices"]) {
    const values = geom[field];
    const name = `${index}-${field}.bin`;
    const bytes = Buffer.from(values.buffer, values.byteOffset, values.byteLength);
    writeFileSync(join(output, name), bytes);
    geom[field] = {file: name, bytes: bytes.length, sha256: createHash("sha256").update(bytes).digest("hex")};
  }
}
for (const [name, values] of Object.entries(result.state))
  if (ArrayBuffer.isView(values)) result.state[name] = Array.from(values);
result.engine = vm.runInContext("mj.mj_versionString()", context);
result.geomNames = vm.runInContext(`Array.from({length:model.ngeom},
  (_, i) => mj.mj_id2name(model, mj.mjtObj.mjOBJ_GEOM.value, i))`, context);
result.meshFaces = vm.runInContext("Array.from(model.mesh_facenum)", context);
result.inputFiles = Object.fromEntries(Object.entries(binding.files).map(([name, record]) => [name, record.sha256]));
context.command = {type: "step", requestId: 1};
await context.self.onmessage({data: context.command});
const stepError = messages.find((message) => message.type === "error");
if (stepError) throw new Error(stepError.message);
result.steppedTime = messages.at(-1).state.time;
writeFileSync(join(output, "result.json"), JSON.stringify(result));
console.log(JSON.stringify({engine: result.engine, geoms: result.geoms.length,
  faces: result.meshFaces.reduce((sum, count) => sum + count, 0), steppedTime: result.steppedTime}));
