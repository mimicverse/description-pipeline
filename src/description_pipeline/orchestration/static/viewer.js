// Actual-URDF viewer: three.js is bundled under /static/vendor and every mesh request goes back
// through the portal's digest-verified artifact route. No external resources are fetched.
import * as THREE from "/static/vendor/three.module.min.js";
import {
  applyInteraction,
  parseUrdfAxis,
  parseUrdfNumber,
  parseUrdfRgba,
  parseUrdfVector,
  resolveUrdfMesh,
} from "/static/preview_logic.js";

export function frameBounds(viewer, bounds) {
  if (bounds.isEmpty()) return;
  const sphere = bounds.getBoundingSphere(new THREE.Sphere());
  const radius = Math.max(sphere.radius, 1e-6);
  const vertical = THREE.MathUtils.degToRad(viewer.camera.fov);
  const horizontal = 2 * Math.atan(Math.tan(vertical / 2) * viewer.camera.aspect);
  viewer.orbit.target.copy(sphere.center);
  viewer.orbit.radius = 1.15 * radius / Math.sin(Math.min(vertical, horizontal) / 2);
  viewer.orbit.minRadius = radius * 0.02;
  viewer.orbit.maxRadius = viewer.orbit.radius * 20;
  viewer.camera.near = radius / 1000;
  viewer.camera.far = viewer.orbit.maxRadius + 2 * radius;
  viewer.camera.updateProjectionMatrix();
  viewer.updateCamera();
}

export function createViewer(container) {
  let viewer = null;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xeef2f7);

  const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 500);
  camera.up.set(0, 0, 1);

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1;
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
    camera.updateMatrixWorld();
    viewer?.inspection?.updateScreen();
  }

  function resize() {
    const width = Math.max(container.clientWidth, 1);
    const height = Math.max(container.clientHeight, 1);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    viewer?.inspection?.updateScreen();
  }

  function onPointerDown(event) {
    if (![0, 1, 2].includes(event.button)) return;
    if (event.button === 0 && viewer.inspection?.beginDrag(event)) {
      renderer.domElement.setPointerCapture(event.pointerId);
      return;
    }
    orbit.dragging = event.button === 0 && !event.shiftKey ? "rotate" : "pan";
    orbit.last = { x: event.clientX, y: event.clientY };
    orbit.start = { ...orbit.last };
    renderer.domElement.setPointerCapture(event.pointerId);
  }

  function onPointerMove(event) {
    if (viewer.inspection?.drag(event)) return;
    if (!orbit.dragging) return;
    const dx = event.clientX - orbit.last.x;
    const dy = event.clientY - orbit.last.y;
    orbit.last = { x: event.clientX, y: event.clientY };
    let next = null;
    if (orbit.dragging === "rotate") {
      next = applyInteraction(orbit, "orbit", [dx, dy]);
    } else {
      const right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0);
      const up = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1);
      next = applyInteraction(orbit, "pan", [dx, dy], {
        right: [right.x, right.y, right.z],
        up: [up.x, up.y, up.z],
      });
    }
    orbit.theta = next.theta;
    orbit.phi = next.phi;
    orbit.radius = next.radius;
    orbit.target.set(next.target.x, next.target.y, next.target.z);
    updateCamera();
  }

  function onPointerUp(event) {
    viewer.inspection?.endDrag();
    const click = orbit.dragging && event.button === 0 && !event.shiftKey
      && Math.hypot(event.clientX - orbit.start.x, event.clientY - orbit.start.y) < 4;
    orbit.dragging = null;
    if (renderer.domElement.hasPointerCapture(event.pointerId)) {
      renderer.domElement.releasePointerCapture(event.pointerId);
    }
    if (click) viewer.inspection?.pick(event);
  }

  function onWheel(event) {
    event.preventDefault();
    const next = applyInteraction(orbit, "zoom", event.deltaY);
    orbit.theta = next.theta;
    orbit.phi = next.phi;
    orbit.radius = next.radius;
    orbit.target.set(next.target.x, next.target.y, next.target.z);
    updateCamera();
  }

  renderer.domElement.addEventListener("pointerdown", onPointerDown);
  renderer.domElement.addEventListener("pointermove", onPointerMove);
  renderer.domElement.addEventListener("pointerup", onPointerUp);
  renderer.domElement.addEventListener("pointercancel", () => { orbit.dragging = null; viewer.inspection?.endDrag(); });
  renderer.domElement.addEventListener("contextmenu", (event) => event.preventDefault());
  renderer.domElement.addEventListener("wheel", onWheel, { passive: false });
  window.addEventListener("resize", resize);

  resize();
  updateCamera();
  (function renderLoop() {
    renderer.render(scene, camera);
    requestAnimationFrame(renderLoop);
  })();

  viewer = {
    modelDispose: null,
    scene,
    camera,
    renderer,
    robotRoot,
    orbit,
    grid,
    resize,
    updateCamera,
    frame() {
      resize();
      updateCamera();
    },
  };
  return viewer;
}

export function disposeRobot(viewer) {
  viewer.modelDispose?.();
  viewer.modelDispose = null;
  viewer.inspection?.dispose();
  viewer.inspection = null;
  const root = viewer.robotRoot;
  disposeGeometry(root);
  root.clear();
}

function disposeGeometry(root) {
  root.traverse((node) => {
    if (node.geometry) node.geometry.dispose();
    if (node.material) {
      for (const material of Array.isArray(node.material) ? node.material : [node.material]) {
        material.dispose();
      }
    }
  });
}

function child(node, tag, required = false) {
  const matches = node ? [...node.children].filter((item) => item.tagName === tag) : [];
  if (matches.length > 1 || (required && matches.length !== 1)) {
    throw new Error(`${node?.tagName || "URDF"} must contain ${required ? "exactly" : "at most"} one ${tag}`);
  }
  return matches[0] || null;
}

function requiredName(node, attribute = "name") {
  const value = node?.getAttribute(attribute);
  if (!value || value.trim() !== value || /\s/.test(value)) throw new Error(`${node?.tagName || "URDF"} ${attribute} is missing or invalid`);
  return value;
}

function originOf(node, label) {
  const origin = child(node, "origin");
  const xyz = parseUrdfVector(origin?.getAttribute("xyz"), [0, 0, 0], `${label} origin xyz`);
  const rpy = parseUrdfVector(origin?.getAttribute("rpy"), [0, 0, 0], `${label} origin rpy`);
  // URDF rpy is a fixed-axis roll/pitch/yaw rotation: R = Rz(yaw) * Ry(pitch) * Rx(roll).
  const quaternion = new THREE.Quaternion().setFromEuler(new THREE.Euler(rpy[0], rpy[1], rpy[2], "ZYX"));
  return { position: new THREE.Vector3(xyz[0], xyz[1], xyz[2]), quaternion };
}

function materialStyle(materials, materialNode, label) {
  if (!materialNode) {
    return { color: 0x8fa8bf, opacity: 1 };
  }
  if (child(materialNode, "texture")) throw new Error(`${label} texture rendering is unsupported`);
  let color = child(materialNode, "color");
  if (!color) {
    const name = materialNode.getAttribute("name");
    if (!name) throw new Error(`${label} has neither a color nor a material name`);
    const definition = materials.get(name);
    if (!definition) throw new Error(`${label} references an unknown material: ${name}`);
    if (child(definition, "texture")) throw new Error(`${label} texture rendering is unsupported`);
    color = child(definition, "color");
  }
  if (!color) throw new Error(`${label} has no renderable color`);
  const values = parseUrdfRgba(color.getAttribute("rgba"), `${label} rgba`);
  return {
    color: new THREE.Color(values[0], values[1], values[2]).getHex(),
    opacity: values[3],
  };
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
  throw new Error("Unrecognized STL file");
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
  if (vertices.length < 9) throw new Error("ASCII STL contains no triangles");
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(vertices), 3));
  geometry.computeVertexNormals();
  return geometry;
}

function requiredPositiveNumber(node, attribute, label) {
  const value = parseUrdfNumber(node.getAttribute(attribute), label);
  if (!(value > 0)) throw new Error(`${label} must be positive`);
  return value;
}

function primitiveGeometry(geometryNode, label) {
  const recognized = ["box", "cylinder", "sphere"].map((name) => geometryNode.querySelector(`:scope > ${name}`));
  if (recognized.filter(Boolean).length !== 1) {
    throw new Error(`${label} must contain exactly one box, cylinder, sphere or mesh`);
  }
  const [box, cylinder, sphere] = recognized;
  if (box) {
    const size = parseUrdfVector(box.getAttribute("size"), undefined, `${label} size`);
    if (size.some((value) => !(value > 0))) throw new Error(`${label} size must contain three positive numbers`);
    return new THREE.BoxGeometry(size[0], size[1], size[2]);
  }
  if (cylinder) {
    const radius = requiredPositiveNumber(cylinder, "radius", `${label} radius`);
    const length = requiredPositiveNumber(cylinder, "length", `${label} length`);
    const geometry = new THREE.CylinderGeometry(radius, radius, length, 32);
    geometry.rotateX(Math.PI / 2); // URDF cylinders run along the local +Z axis
    return geometry;
  }
  if (sphere) {
    return new THREE.SphereGeometry(requiredPositiveNumber(sphere, "radius", `${label} radius`), 24, 16);
  }
  return null;
}

async function loadMesh(url, cache, signal) {
  if (cache.has(url)) return cache.get(url);
  const pending = fetch(url, { credentials: "same-origin", signal })
    .then((response) => {
      if (!response.ok) throw new Error(`Mesh load failed (HTTP ${response.status})`);
      return response.arrayBuffer();
    })
    .then((buffer) => parseStl(buffer));
  cache.set(url, pending);
  return pending;
}

export async function loadRobot(viewer, { urdfUrl, urdfPath, files, artifactUrl, signal }) {
  const response = await fetch(urdfUrl, { credentials: "same-origin", signal });
  if (!response.ok) throw new Error(`URDF load failed (HTTP ${response.status})`);
  const xml = await response.text();
  signal?.throwIfAborted();
  if (/<!doctype/i.test(xml)) throw new Error("URDF must not contain a document type declaration");
  const document = new DOMParser().parseFromString(xml, "application/xml");
  if (document.querySelector("parsererror")) throw new Error("URDF XML parsing failed");
  const robot = document.documentElement;
  if (robot?.tagName !== "robot") throw new Error("URDF has no robot root element");
  requiredName(robot);

  disposeRobot(viewer);
  viewer.grid.visible = true;
  const meshCache = new Map();
  const warnings = [];
  const links = new Map();
  const joints = [];
  const linkRecords = [];
  try {
    const materials = new Map();
    for (const node of robot.querySelectorAll(":scope > material")) {
      const name = requiredName(node);
      if (materials.has(name)) throw new Error(`URDF material name is duplicated: ${name}`);
      materials.set(name, node);
    }
    for (const node of robot.querySelectorAll(":scope > link")) {
      const name = requiredName(node);
      if (links.has(name)) throw new Error(`URDF link name is duplicated: ${name}`);
      const group = new THREE.Group();
      group.name = name;
      const visuals = new THREE.Group();
      group.add(visuals);
      links.set(name, { node, group, visuals });
      const inertial = child(node, "inertial");
      const mass = inertial ? child(inertial, "mass", true) : null;
      const tensor = inertial ? child(inertial, "inertia", true) : null;
      const origin = inertial ? originOf(inertial, `Inertial for link ${name}`) : null;
      linkRecords.push({
        name,
        mass_kg: mass ? parseUrdfNumber(mass.getAttribute("value"), `Link ${name} mass`) : null,
        com_m: origin ? [origin.position.x, origin.position.y, origin.position.z] : null,
        inertial_quaternion: origin ? origin.quaternion.toArray() : null,
        inertia_kg_m2: tensor ? Object.fromEntries(
          ["ixx", "ixy", "ixz", "iyy", "iyz", "izz"].map((key) => [
            key, parseUrdfNumber(tensor.getAttribute(key), `Link ${name} inertia ${key}`),
          ]),
        ) : null,
      });
    }

    const jointNames = new Set();
    const parents = new Map();
    for (const node of robot.querySelectorAll(":scope > joint")) {
      const name = requiredName(node);
      if (jointNames.has(name)) throw new Error(`URDF joint name is duplicated: ${name}`);
      jointNames.add(name);
      const type = requiredName(node, "type");
      if (!["fixed", "revolute", "continuous", "prismatic"].includes(type)) {
        throw new Error(`Joint ${name} type is unsupported: ${type}`);
      }
      if (child(node, "mimic")) throw new Error(`Joint ${name} mimic motion is unsupported`);
      const parent = requiredName(child(node, "parent", true), "link");
      const childName = requiredName(child(node, "child", true), "link");
      if (!links.has(parent) || !links.has(childName) || parent === childName || parents.has(childName)) {
        throw new Error(`Joint ${name} has invalid or ambiguous link bindings`);
      }
      parents.set(childName, parent);
      const { position, quaternion } = originOf(node, `Joint ${name}`);
      const axisNode = child(node, "axis");
      if (type === "fixed" && axisNode) throw new Error(`Joint ${name} fixed axis is unsupported`);
      const sourceAxis = axisNode
        ? parseUrdfVector(axisNode.getAttribute("xyz"), undefined, `Joint ${name} axis`)
        : type === "fixed" ? null : [1, 0, 0];
      const direction = sourceAxis ? parseUrdfAxis(sourceAxis.join(" "), `Joint ${name} axis`) : null;
      const axisVector = direction ? new THREE.Vector3(...direction) : null;
      const limit = child(node, "limit");
      if (type === "fixed" && limit) throw new Error(`Joint ${name} fixed limits are unsupported`);
      if (type === "continuous" && limit && (limit.hasAttribute("lower") || limit.hasAttribute("upper"))) {
        throw new Error(`Joint ${name} continuous position limits are unsupported`);
      }
      if ((type === "revolute" || type === "prismatic") && !limit) throw new Error(`Joint ${name} limit is missing`);
      const lower = limit ? (limit.hasAttribute("lower") ? parseUrdfNumber(limit.getAttribute("lower"), `Joint ${name} lower limit`) : 0) : null;
      const upper = limit ? (limit.hasAttribute("upper") ? parseUrdfNumber(limit.getAttribute("upper"), `Joint ${name} upper limit`) : 0) : null;
      if (limit && lower > upper) throw new Error(`Joint ${name} lower limit exceeds upper limit`);
      const effort = limit ? parseUrdfNumber(limit.getAttribute("effort"), `Joint ${name} effort limit`) : null;
      const velocity = limit ? parseUrdfNumber(limit.getAttribute("velocity"), `Joint ${name} velocity limit`) : null;
      if (effort < 0 || velocity < 0) throw new Error(`Joint ${name} effort and velocity must be non-negative`);

      const pivot = new THREE.Group();
      pivot.position.copy(position);
      pivot.quaternion.copy(quaternion);
      links.get(parent).group.add(pivot);
      const motion = new THREE.Group();
      pivot.add(motion);
      motion.add(links.get(childName).group);
      joints.push({
        name, type, parent, child: childName, pivot, motion, axis: axisVector, sourceAxis,
        lower: type === "continuous" ? null : lower,
        upper: type === "continuous" ? null : upper,
        effort, velocity, value: 0,
        set(value) {
          this.value = value;
          if (type === "revolute" || type === "continuous") motion.quaternion.setFromAxisAngle(axisVector, value);
          else if (type === "prismatic") motion.position.copy(axisVector).multiplyScalar(value);
        },
        reset() { this.set(0); },
      });
    }
    const roots = [...links.keys()].filter((name) => !parents.has(name));
    if (roots.length !== 1) throw new Error("URDF must have exactly one root link");
    for (const name of links.keys()) {
      const seen = new Set();
      let current = name;
      while (parents.has(current)) {
        if (seen.has(current)) throw new Error("URDF link graph contains a cycle");
        seen.add(current);
        current = parents.get(current);
      }
      if (current !== roots[0]) throw new Error("URDF link graph is disconnected");
    }

    // Attach only after every visual is readable; failed loads never display a partial robot.
    for (const [name, entry] of links) {
      for (const visual of entry.node.querySelectorAll(":scope > visual")) {
        const geometryNode = child(visual, "geometry", true);
        if (geometryNode.children.length !== 1 || !["mesh", "box", "cylinder", "sphere"].includes(geometryNode.children[0].tagName)) {
          throw new Error(`Visual for link ${name} must contain exactly one supported geometry`);
        }
        const label = `Visual for link ${name}`;
        const { position, quaternion } = originOf(visual, label);
        const materialNode = child(visual, "material");
        const style = materialStyle(materials, materialNode, `${label} material`);
        if (!materialNode) warnings.push(`${label} has no material; a neutral preview appearance is used`);
        const meshNode = child(geometryNode, "mesh");
        let geometry;
        let meshScale;
        if (meshNode) {
          const resolved = resolveUrdfMesh(files, urdfPath, meshNode.getAttribute("filename"));
          meshScale = parseUrdfVector(meshNode.getAttribute("scale"), [1, 1, 1], `${label} mesh scale`);
          const sourceGeometry = await loadMesh(artifactUrl(resolved), meshCache, signal);
          signal?.throwIfAborted();
          geometry = sourceGeometry.clone();
        } else geometry = primitiveGeometry(geometryNode, `${label} geometry`);
        const material = new THREE.MeshStandardMaterial({
          color: style.color, opacity: style.opacity,
          transparent: style.opacity < 1, depthWrite: style.opacity >= 1,
          metalness: 0.12, roughness: 0.68,
        });
        const mesh = new THREE.Mesh(geometry, material);
        // Keep reflections on the object transform so Three also reverses face culling.
        if (meshScale) mesh.scale.set(...meshScale);
        mesh.position.copy(position);
        mesh.quaternion.copy(quaternion);
        mesh.userData.linkName = name;
        mesh.userData.sourceStyle = { color: material.color.clone(), opacity: material.opacity };
        entry.visuals.add(mesh);
      }
    }

    viewer.robotRoot.add(links.get(roots[0]).group);
    viewer.robotRoot.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(viewer.robotRoot);
    if (box.isEmpty()) warnings.push("URDF contains no displayable geometry");
    else {
      viewer.orbit.theta = Math.PI * 0.75;
      viewer.orbit.phi = Math.PI * 0.38;
      frameBounds(viewer, box);
    }
    viewer.frame();
    const jointRecords = joints.map((joint) => ({
      name: joint.name, type: joint.type, lower: joint.lower, upper: joint.upper,
      effort: joint.effort, velocity: joint.velocity, axis: joint.sourceAxis,
    }));
    return { links, allJoints: joints, joints: joints.filter((joint) => joint.type !== "fixed"), warnings, records: { links: linkRecords, joints: jointRecords } };
  } catch (error) {
    // Failed or cancelled loads also own resources that have not reached robotRoot yet.
    for (const { visuals, group } of links.values()) { disposeGeometry(visuals); group.removeFromParent(); }
    throw error;
  } finally {
    for (const result of await Promise.allSettled(meshCache.values())) {
      if (result.status === "fulfilled") result.value.dispose();
    }
  }
}

export function buildJointControls(container, joints, { onInput } = {}) {
  container.textContent = "";
  const rows = [];
  for (const joint of joints) {
    const row = document.createElement("div");
    row.className = "joint-row";
    const label = document.createElement("label");
    label.textContent = joint.name;
    label.title = joint.name;
    const readout = document.createElement("output");
    const range = document.createElement("input");
    range.type = "range";
    range.step = "any";
    const angular = joint.type === "revolute" || joint.type === "continuous";
    const numeric = document.createElement("input");
    numeric.type = "number"; numeric.step = "any";
    numeric.setAttribute("aria-label", `${joint.name} value`);
    range.setAttribute("aria-label", `${joint.name} slider`);
    const unit = document.createElement("select");
    for (const value of angular ? ["deg", "rad"] : ["m"]) {
      const option = document.createElement("option"); option.value = value; option.textContent = value; unit.append(option);
    }
    unit.value = angular ? "deg" : "m";
    unit.setAttribute("aria-label", `${joint.name} unit`);
    const factor = () => angular && unit.value === "deg" ? 180 / Math.PI : 1;
    const display = (syncInput = true) => {
      const value = joint.value * factor();
      const inputValue = (joint.requestedValue ?? joint.value) * factor();
      range.min = String((joint.type === "continuous" ? -Math.PI : joint.lower) * factor());
      range.max = String((joint.type === "continuous" ? Math.PI : joint.upper) * factor());
      if (syncInput || document.activeElement !== range) range.value = String(inputValue);
      if (syncInput) numeric.value = String(inputValue);
      readout.textContent = `${value.toFixed(4)} ${unit.value}${joint.requestedValue === undefined ? "" : " (updating)"}`;
      if (value < Number(range.min) || value > Number(range.max)) {
        readout.textContent += joint.type === "continuous"
          ? " (outside slider window; joint is continuous)"
          : " (reference pose outside position limits)";
      }
    };
    const update = () => {
      const raw = Number(range.value);
      const value = raw / factor();
      joint.set(value);
      display(false);
      numeric.value = String(raw);
      if (onInput) onInput(joint);
    };
    range.addEventListener("input", update);
    const updateNumeric = (commit) => {
      const raw = numeric.value.trim() === "" ? NaN : Number(numeric.value);
      if (!Number.isFinite(raw)) { if (commit) display(); return; }
      const value = raw / factor();
      const requested = joint.type === "continuous" ? value : Math.max(joint.lower, Math.min(joint.upper, value));
      joint.set(requested);
      display(false);
      if (commit) numeric.value = String(requested * factor());
      if (onInput) onInput(joint);
    };
    numeric.addEventListener("input", () => updateNumeric(false));
    numeric.addEventListener("change", () => updateNumeric(true));
    unit.addEventListener("change", () => display());
    display();
    row.append(label, readout, range, numeric, unit);
    container.append(row);
    rows.push({ joint, display, row, numeric, range });
  }
  return {
    refresh() {
      for (const row of rows) {
        const editing = document.activeElement === row.numeric || document.activeElement === row.range;
        row.display(!editing);
      }
    },
    reveal(name, { focus = false } = {}) {
      const selected = rows.find((entry) => entry.joint.name === name) || (focus ? rows[0] : null);
      for (const entry of rows) entry.row.classList.toggle("selected", entry === selected);
      if (focus && selected) {
        selected.numeric.focus();
        selected.numeric.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    },
    reset() {
      for (const row of rows) {
        row.joint.reset();
        row.display();
        if (onInput) onInput(row.joint);
      }
    },
  };
}
