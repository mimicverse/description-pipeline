// Actual-URDF viewer: three.js is bundled under /static/vendor and every mesh request goes back
// through the portal's digest-verified artifact route. No external resources are fetched.
import * as THREE from "/static/vendor/three.module.min.js";

export function createViewer(container) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xeef2f7);

  const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 500);
  camera.up.set(0, 0, 1);

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio || 1);
  container.appendChild(renderer.domElement);

  scene.add(new THREE.HemisphereLight(0xffffff, 0x5a6b7d, 2.2));
  const key = new THREE.DirectionalLight(0xffffff, 2.0);
  key.position.set(2.5, -3, 4);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.7);
  fill.position.set(-3, 2, 2);
  scene.add(fill);

  const grid = new THREE.GridHelper(4, 40, 0xb9c4d0, 0xd5dde6);
  grid.rotation.x = Math.PI / 2;
  scene.add(grid);

  const robotRoot = new THREE.Group();
  robotRoot.name = "robot";
  scene.add(robotRoot);

  const orbit = {
    target: new THREE.Vector3(0, 0, 0),
    radius: 2.5,
    theta: Math.PI * 0.75,
    phi: Math.PI * 0.38,
    dragging: null,
    last: { x: 0, y: 0 },
  };

  function updateCamera() {
    const sinPhi = Math.sin(orbit.phi);
    const position = new THREE.Vector3(
      orbit.target.x + orbit.radius * sinPhi * Math.cos(orbit.theta),
      orbit.target.y + orbit.radius * sinPhi * Math.sin(orbit.theta),
      orbit.target.z + orbit.radius * Math.cos(orbit.phi),
    );
    camera.position.copy(position);
    camera.lookAt(orbit.target);
  }

  function resize() {
    const width = Math.max(container.clientWidth, 1);
    const height = Math.max(container.clientHeight, 1);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }

  function onPointerDown(event) {
    orbit.dragging = event.button === 0 && !event.shiftKey ? "rotate" : "pan";
    orbit.last = { x: event.clientX, y: event.clientY };
    renderer.domElement.setPointerCapture(event.pointerId);
  }

  function onPointerMove(event) {
    if (!orbit.dragging) return;
    const dx = event.clientX - orbit.last.x;
    const dy = event.clientY - orbit.last.y;
    orbit.last = { x: event.clientX, y: event.clientY };
    if (orbit.dragging === "rotate") {
      orbit.theta -= dx * 0.008;
      orbit.phi = Math.min(Math.PI - 0.02, Math.max(0.02, orbit.phi - dy * 0.008));
    } else {
      const right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0);
      const up = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1);
      const scale = orbit.radius * 0.0016;
      orbit.target.addScaledVector(right, -dx * scale).addScaledVector(up, dy * scale);
    }
    updateCamera();
  }

  function onPointerUp(event) {
    orbit.dragging = null;
    if (renderer.domElement.hasPointerCapture(event.pointerId)) {
      renderer.domElement.releasePointerCapture(event.pointerId);
    }
  }

  function onWheel(event) {
    event.preventDefault();
    orbit.radius = Math.min(500, Math.max(0.05, orbit.radius * Math.exp(event.deltaY * 0.001)));
    updateCamera();
  }

  renderer.domElement.addEventListener("pointerdown", onPointerDown);
  renderer.domElement.addEventListener("pointermove", onPointerMove);
  renderer.domElement.addEventListener("pointerup", onPointerUp);
  renderer.domElement.addEventListener("wheel", onWheel, { passive: false });
  window.addEventListener("resize", resize);

  resize();
  updateCamera();
  (function renderLoop() {
    renderer.render(scene, camera);
    requestAnimationFrame(renderLoop);
  })();

  return {
    scene,
    camera,
    renderer,
    robotRoot,
    orbit,
    resize,
    updateCamera,
    frame() {
      resize();
      updateCamera();
    },
  };
}

export function disposeRobot(viewer) {
  const root = viewer.robotRoot;
  root.traverse((node) => {
    if (node.geometry) node.geometry.dispose();
    if (node.material) {
      for (const material of Array.isArray(node.material) ? node.material : [node.material]) {
        material.dispose();
      }
    }
  });
  root.clear();
}

function parseVector(text, fallback) {
  if (text === undefined || text === null || text === "") return fallback.slice();
  const parts = String(text).trim().split(/\s+/).map(Number);
  if (parts.length !== 3 || parts.some((value) => !Number.isFinite(value))) {
    throw new Error(`无法解析 URDF 向量：${text}`);
  }
  return parts;
}

function parseNumber(text, fallback) {
  const value = Number(text);
  return Number.isFinite(value) ? value : fallback;
}

function originOf(node) {
  const origin = node ? node.querySelector("origin") : null;
  const xyz = parseVector(origin && origin.getAttribute("xyz"), [0, 0, 0]);
  const rpy = parseVector(origin && origin.getAttribute("rpy"), [0, 0, 0]);
  // URDF rpy is a fixed-axis roll/pitch/yaw rotation: R = Rz(yaw) * Ry(pitch) * Rx(roll).
  const quaternion = new THREE.Quaternion().setFromEuler(new THREE.Euler(rpy[0], rpy[1], rpy[2], "ZYX"));
  return { position: new THREE.Vector3(xyz[0], xyz[1], xyz[2]), quaternion };
}

function colorOf(materialNode) {
  const color = materialNode ? materialNode.querySelector("color") : null;
  const values = color ? String(color.getAttribute("rgba") || "").trim().split(/\s+/).map(Number) : [];
  if (values.length < 3 || values.slice(0, 3).some((value) => !Number.isFinite(value))) return 0x8fa8bf;
  return new THREE.Color(values[0], values[1], values[2]).getHex();
}

export function parseStl(buffer) {
  const view = new DataView(buffer);
  if (buffer.byteLength >= 84) {
    const triangles = view.getUint32(80, true);
    if (84 + triangles * 50 === buffer.byteLength) {
      return binaryStl(view, triangles);
    }
  }
  const text = new TextDecoder().decode(buffer);
  if (/^\s*solid/i.test(text) && /facet/i.test(text)) {
    return asciiStl(text);
  }
  throw new Error("无法识别的 STL 文件");
}

function binaryStl(view, triangles) {
  const positions = new Float32Array(triangles * 9);
  let offset = 84;
  for (let index = 0; index < triangles; index += 1) {
    offset += 12; // facet normal
    for (let vertex = 0; vertex < 3; vertex += 1) {
      positions[index * 9 + vertex * 3] = view.getFloat32(offset, true);
      positions[index * 9 + vertex * 3 + 1] = view.getFloat32(offset + 4, true);
      positions[index * 9 + vertex * 3 + 2] = view.getFloat32(offset + 8, true);
      offset += 12;
    }
    offset += 2; // attribute byte count
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.computeVertexNormals();
  return geometry;
}

function asciiStl(text) {
  const vertices = [];
  const pattern = /vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)/g;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    vertices.push(Number(match[1]), Number(match[2]), Number(match[3]));
  }
  if (vertices.length < 9) throw new Error("ASCII STL 不含三角面");
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(vertices), 3));
  geometry.computeVertexNormals();
  return geometry;
}

function primitiveGeometry(geometryNode) {
  const box = geometryNode.querySelector("box");
  if (box) {
    const size = parseVector(box.getAttribute("size"), [1, 1, 1]);
    return new THREE.BoxGeometry(size[0], size[1], size[2]);
  }
  const cylinder = geometryNode.querySelector("cylinder");
  if (cylinder) {
    const radius = parseNumber(cylinder.getAttribute("radius"), 0.05);
    const length = parseNumber(cylinder.getAttribute("length"), 0.1);
    const geometry = new THREE.CylinderGeometry(radius, radius, length, 32);
    geometry.rotateX(Math.PI / 2); // URDF cylinders run along the local +Z axis
    return geometry;
  }
  const sphere = geometryNode.querySelector("sphere");
  if (sphere) {
    return new THREE.SphereGeometry(parseNumber(sphere.getAttribute("radius"), 0.05), 24, 16);
  }
  return null;
}

function resolveMesh(files, filename) {
  if (!filename) return null;
  const clean = String(filename)
    .replace(/^package:\/\/[^/]+\//, "")
    .replace(/^\.\//, "");
  const keys = Object.keys(files);
  if (Object.prototype.hasOwnProperty.call(files, clean)) return clean;
  const base = clean.split("/").pop();
  const matches = keys.filter((key) => key === base || key.endsWith(`/${base}`));
  return matches.length ? matches[0] : null;
}

async function loadMesh(viewer, url, cache) {
  if (cache.has(url)) return cache.get(url);
  const pending = fetch(url, { credentials: "same-origin" })
    .then((response) => {
      if (!response.ok) throw new Error(`网格加载失败（HTTP ${response.status}）`);
      return response.arrayBuffer();
    })
    .then((buffer) => parseStl(buffer));
  cache.set(url, pending);
  return pending;
}

export async function loadRobot(viewer, { urdfUrl, files, artifactUrl, controls, onWarning }) {
  const response = await fetch(urdfUrl, { credentials: "same-origin" });
  if (!response.ok) throw new Error(`URDF 加载失败（HTTP ${response.status}）`);
  const xml = await response.text();
  const document = new DOMParser().parseFromString(xml, "application/xml");
  if (document.querySelector("parsererror")) throw new Error("URDF XML 解析失败");
  const robot = document.querySelector("robot");
  if (!robot) throw new Error("URDF 缺少 robot 元素");

  disposeRobot(viewer);
  const meshCache = new Map();
  const warnings = [];
  const links = new Map();
  const joints = [];

  for (const node of robot.querySelectorAll(":scope > link")) {
    const name = node.getAttribute("name");
    if (!name || links.has(name)) throw new Error(`URDF link 名称缺失或重复：${name}`);
    const group = new THREE.Group();
    group.name = name;
    links.set(name, { node, group });
  }

  for (const node of robot.querySelectorAll(":scope > joint")) {
    const name = node.getAttribute("name") || "(未命名关节)";
    const type = (node.getAttribute("type") || "fixed").toLowerCase();
    const parent = node.querySelector("parent")?.getAttribute("link");
    const child = node.querySelector("child")?.getAttribute("link");
    if (!links.has(parent) || !links.has(child)) {
      warnings.push(`关节 ${name} 引用了不存在的 link`);
      continue;
    }
    const { position, quaternion } = originOf(node);
    const axis = parseVector(node.querySelector("axis")?.getAttribute("xyz"), [1, 0, 0]);
    const axisVector = new THREE.Vector3(axis[0], axis[1], axis[2]);
    if (axisVector.length() === 0) axisVector.set(1, 0, 0);
    axisVector.normalize();

    const pivot = new THREE.Group();
    pivot.position.copy(position);
    pivot.quaternion.copy(quaternion);
    links.get(parent).group.add(pivot);
    const motion = new THREE.Group();
    pivot.add(motion);
    motion.add(links.get(child).group);
    links.get(parent).group.remove(links.get(child).group);

    const limit = node.querySelector("limit");
    if ((type === "revolute" || type === "prismatic") && !limit) {
      warnings.push(`关节 ${name} 缺少 limit，未生成控制`);
    }
    const lower = limit ? parseNumber(limit.getAttribute("lower"), 0) : 0;
    const upper = limit ? parseNumber(limit.getAttribute("upper"), 0) : 0;
    const joint = {
      name,
      type,
      motion,
      axis: axisVector,
      lower: type === "continuous" ? -Math.PI : lower,
      upper: type === "continuous" ? Math.PI : upper,
      value: 0,
      set(value) {
        this.value = value;
        if (type === "revolute" || type === "continuous") {
          motion.quaternion.setFromAxisAngle(axisVector, value);
        } else if (type === "prismatic") {
          motion.position.copy(axisVector).multiplyScalar(value);
        }
      },
      reset() {
        this.set(0);
      },
    };
    if (type === "continuous" || limit) joints.push(joint);
  }

  const rootName = [...links.keys()].find(
    (name) => ![...robot.querySelectorAll(":scope > joint > child")].some((child) => child.getAttribute("link") === name),
  );
  if (!rootName) throw new Error("URDF 没有根 link");
  viewer.robotRoot.add(links.get(rootName).group);

  for (const [name, entry] of links) {
    for (const visual of entry.node.querySelectorAll(":scope > visual")) {
      const geometryNode = visual.querySelector("geometry");
      if (!geometryNode) continue;
      const { position, quaternion } = originOf(visual);
      const material = new THREE.MeshStandardMaterial({
        color: colorOf(visual.querySelector("material")),
        metalness: 0.12,
        roughness: 0.68,
      });
      const meshNode = geometryNode.querySelector("mesh");
      let geometry = null;
      if (meshNode) {
        const filename = meshNode.getAttribute("filename");
        const resolved = resolveMesh(files, filename);
        if (!resolved) {
          warnings.push(`link ${name} 的网格不在已验证交付清单中：${filename}`);
          continue;
        }
        try {
          geometry = (await loadMesh(viewer, artifactUrl(resolved), meshCache)).clone();
        } catch (error) {
          warnings.push(`link ${name} 的网格加载失败：${error.message}`);
          continue;
        }
        const scale = parseVector(meshNode.getAttribute("scale"), [1, 1, 1]);
        geometry.scale(scale[0], scale[1], scale[2]);
      } else {
        geometry = primitiveGeometry(geometryNode);
        if (!geometry) continue;
      }
      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.copy(position);
      mesh.quaternion.copy(quaternion);
      entry.group.add(mesh);
    }
  }

  viewer.robotRoot.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(viewer.robotRoot);
  if (box.isEmpty()) {
    warnings.push("URDF 没有可显示的几何");
  } else {
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const span = Math.max(size.x, size.y, size.z, 0.05);
    viewer.orbit.target.copy(center);
    viewer.orbit.radius = span * 2.4;
    viewer.orbit.phi = Math.PI * 0.38;
    viewer.updateCamera();
    viewer.camera.near = Math.max(span / 500, 0.001);
    viewer.camera.far = span * 200;
    viewer.camera.updateProjectionMatrix();
  }
  viewer.frame();
  if (onWarning) onWarning(warnings);
  return { links, joints, warnings };
}

export function buildJointControls(container, joints, { onInput } = {}) {
  container.textContent = "";
  const rows = [];
  for (const joint of joints) {
    const row = document.createElement("div");
    row.className = "joint-row";
    const label = document.createElement("label");
    label.textContent = joint.name;
    const readout = document.createElement("output");
    const range = document.createElement("input");
    range.type = "range";
    const angular = joint.type === "revolute" || joint.type === "continuous";
    if (angular) {
      range.min = String((joint.lower * 180) / Math.PI);
      range.max = String((joint.upper * 180) / Math.PI);
      range.step = "0.5";
    } else {
      range.min = String(joint.lower);
      range.max = String(joint.upper);
      range.step = String(Math.max((joint.upper - joint.lower) / 500, 1e-4));
    }
    range.value = "0";
    const update = () => {
      const raw = Number(range.value);
      const value = angular ? (raw * Math.PI) / 180 : raw;
      joint.set(value);
      readout.textContent = angular ? `${raw.toFixed(1)}°` : `${raw.toFixed(4)} m`;
      if (onInput) onInput(joint);
    };
    range.addEventListener("input", update);
    update();
    row.append(label, readout, range);
    container.append(row);
    rows.push({ joint, range, update });
  }
  return {
    reset() {
      for (const row of rows) {
        row.range.value = "0";
        row.update();
      }
    },
  };
}
