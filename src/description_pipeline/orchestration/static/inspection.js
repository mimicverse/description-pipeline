// Read-only inspection of the loaded URDF scene. Display edits never change delivery bytes.
import * as THREE from "/static/vendor/three.module.min.js";

export function inertiaEllipsoid(mass, tensor) {
  if (!(mass > 0) || !tensor) throw new Error("positive mass and an inertia tensor are required");
  const { ixx, ixy, ixz, iyy, iyz, izz } = tensor;
  const a = [[ixx, ixy, ixz], [ixy, iyy, iyz], [ixz, iyz, izz]];
  if (!a.flat().every(Number.isFinite)) throw new Error("inertia tensor must be finite");
  const scale = Math.max(...a.flat().map(Math.abs));
  if (!(scale > 0)) throw new Error("zero inertia has no ellipsoid");
  // Jacobi rotations diagonalize the full symmetric tensor, including cross terms.
  const v = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
  for (let iteration = 0; iteration < 32; iteration++) {
    const [p, q] = [[0, 1], [0, 2], [1, 2]].sort((x, y) => Math.abs(a[y[0]][y[1]]) - Math.abs(a[x[0]][x[1]]))[0];
    if (Math.abs(a[p][q]) <= scale * 1e-14) break;
    const angle = 0.5 * Math.atan2(2 * a[p][q], a[q][q] - a[p][p]);
    const c = Math.cos(angle), s = Math.sin(angle);
    const rotation = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
    rotation[p][p] = c; rotation[q][q] = c;
    rotation[p][q] = s; rotation[q][p] = -s;
    const multiply = (x, y) => x.map((row) => y[0].map((_, j) => row.reduce((sum, value, k) => sum + value * y[k][j], 0)));
    const transpose = rotation[0].map((_, j) => rotation.map((row) => row[j]));
    const next = multiply(multiply(transpose, a), rotation);
    const vectors = multiply(v, rotation);
    for (let i = 0; i < 3; i++) { a[i] = next[i]; v[i] = vectors[i]; }
  }
  const moments = a.map((row, i) => row[i]);
  if (moments.some((value) => value < -scale * 1e-12)) throw new Error("inertia has negative principal moments");
  // Ixx = m * (b² + c²) / 5 for an equivalent uniform solid ellipsoid.
  const squares = moments.map((value) => 5 * (moments.reduce((x, y) => x + y, 0) - 2 * value) / (2 * mass));
  if (squares.some((value) => value < -scale / mass * 1e-10)) throw new Error("inertia violates the principal-moment triangle inequalities");
  const radii = squares.map((value) => Math.sqrt(Math.max(0, value)));
  if (!radii.every(Number.isFinite)) throw new Error("inertia ellipsoid is not finite");
  return { radii, axes: v[0].map((_, j) => v.map((row) => row[j])), moments };
}

export function visualBounds(loaded) {
  const bounds = new THREE.Box3();
  for (const { visuals } of loaded.links.values()) {
    visuals.updateWorldMatrix(true, false);
    for (const mesh of visuals.children) bounds.union(new THREE.Box3().setFromObject(mesh));
  }
  return bounds;
}

export function createInspection(viewer, loaded, container, { onPoseChange } = {}) {
  container.textContent = "";
  const { links, allJoints, records } = loaded;
  const controls = new AbortController();
  const overlays = [];
  const axes = [], centers = [], inertias = [];
  const bounds = visualBounds(loaded);
  const span = bounds.isEmpty() ? 1 : Math.max(...bounds.getSize(new THREE.Vector3()).toArray(), 0.001);
  const size = span * 0.08;
  let selected = null, wireframe = false, ground = true;
  let endpoints = [];
  const measurements = new THREE.Group();
  viewer.scene.add(measurements);

  const element = (tag, text, parent = container) => {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    parent.append(node);
    return node;
  };
  const listen = (node, event, fn) => node.addEventListener(event, fn, { signal: controls.signal });
  const button = (text, fn, parent) => {
    const node = element("button", text, parent); node.type = "button"; node.className = "ghost";
    listen(node, "click", fn); return node;
  };
  const field = (text, type, parent) => {
    const label = element("label", null, parent);
    const input = element("input", null, label); input.type = type;
    element("span", text, label); return input;
  };
  const toggle = (text, initial, fn, parent) => {
    const input = field(text, "checkbox", parent); input.checked = initial;
    listen(input, "change", () => fn(input.checked)); return input;
  };
  const select = (text, options, parent) => {
    const label = element("label", text, parent), input = element("select", null, label);
    input.setAttribute("aria-label", text.trim());
    for (const [value, title] of options) {
      const option = element("option", title, input); option.value = value;
    }
    return input;
  };
  const details = (title) => {
    const node = element("details"); node.className = "preview-panel";
    element("summary", title, node); return element("div", null, node);
  };
  const toolbar = element("div"); toolbar.className = "inspection-toolbar";
  function fit() {
    const box = visualBounds(loaded);
    if (!box.isEmpty()) {
      viewer.orbit.target.copy(box.getCenter(new THREE.Vector3()));
      viewer.orbit.radius = Math.max(...box.getSize(new THREE.Vector3()).toArray(), 0.001) * 2.4;
    }
    viewer.updateCamera();
  }
  button("Fit", fit, toolbar);
  const view = select("View ", [["iso", "Isometric"], ["front", "Front (−Y)"], ["back", "Back (+Y)"], ["left", "Left (−X)"], ["right", "Right (+X)"], ["top", "Top (+Z)"], ["bottom", "Bottom (−Z)"]], toolbar);
  listen(view, "change", () => {
    const angles = { iso: [Math.PI * 0.75, Math.PI * 0.38], front: [-Math.PI / 2, Math.PI / 2], back: [Math.PI / 2, Math.PI / 2], left: [Math.PI, Math.PI / 2], right: [0, Math.PI / 2], top: [0, 1e-6], bottom: [0, Math.PI - 1e-6] };
    [viewer.orbit.theta, viewer.orbit.phi] = angles[view.value]; viewer.updateCamera();
  });
  const jointSelect = select("Inspect joint ", [["", "Select joint"], ...loaded.joints.map((joint) => [joint.name, joint.name])], toolbar);
  const dragMode = toggle("Drag joint in view", false, () => { pointMode.checked = false; dragState = null; }, toolbar);
  const help = element("p", "Drag to orbit; Shift/right drag to pan; scroll to zoom. To move a joint, select it and enable Drag joint in view. Rotations follow the joint-axis plane; translations follow the projected axis. Use numeric controls when the axis is edge-on.");
  help.className = "muted small";
  const jointBadge = element("div", null, viewer.renderer.domElement.parentElement);
  jointBadge.className = "joint-badge"; jointBadge.hidden = true;
  const arc = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0xf4b400, depthTest: false }));
  viewer.scene.add(arc); overlays.push(arc); arc.visible = false;
  listen(jointSelect, "change", update);
  const display = details("Display and engineering overlays"); display.className = "inspection-toolbar";
  toggle("Grid", true, (value) => { viewer.grid.visible = value; }, display);
  const worldAxes = new THREE.AxesHelper(size * 2); viewer.scene.add(worldAxes); overlays.push(worldAxes);
  toggle("World axes", true, (value) => { worldAxes.visible = value; }, display);
  toggle("Wireframe", false, (value) => {
    wireframe = value;
    for (const { visuals } of links.values()) for (const mesh of visuals.children) mesh.material.wireframe = value;
  }, display);
  toggle("Dark background", false, (value) => { viewer.scene.background.set(value ? 0x18202b : 0xeef2f7); }, display);
  toggle("Ground grid", true, (value) => { ground = value; update(); }, display);
  const axisSize = field("Axis size (m)", "number", display);
  axisSize.min = "0.000001"; axisSize.step = "any"; axisSize.value = String(size);
  for (const joint of allJoints) {
    const frame = new THREE.AxesHelper(1); frame.scale.setScalar(size); joint.pivot.add(frame);
    frame.visible = false; axes.push(frame); overlays.push(frame);
    if (joint.axis) {
      const arrow = new THREE.ArrowHelper(joint.axis, new THREE.Vector3(), size, 0xf4b400, size * 0.25, size * 0.12);
      joint.pivot.add(arrow); arrow.visible = false; axes.push(arrow); overlays.push(arrow);
    }
  }
  toggle("Joint frames / motion axes", false, (value) => { for (const axis of axes) axis.visible = value; }, display);
  const issues = [];
  for (const record of records.links) {
    if (!record.com_m) { issues.push(`${record.name}: inertial data not provided`); continue; }
    const link = links.get(record.name);
    const center = new THREE.Mesh(new THREE.SphereGeometry(1, 12, 8), new THREE.MeshBasicMaterial({ color: 0xe83e8c, depthTest: false }));
    center.position.set(...record.com_m); center.scale.setScalar(size * 0.08); center.visible = false;
    center.renderOrder = 2; link.group.add(center); centers.push(center); overlays.push(center);
    try {
      const { radii, axes: basis } = inertiaEllipsoid(record.mass_kg, record.inertia_kg_m2);
      const rotation = new THREE.Matrix4().makeBasis(...basis.map((axis) => new THREE.Vector3(...axis)));
      const ellipsoid = new THREE.Mesh(new THREE.SphereGeometry(1, 24, 16), new THREE.MeshBasicMaterial({ color: 0x9b59b6, wireframe: true, transparent: true, opacity: 0.5 }));
      ellipsoid.position.set(...record.com_m);
      ellipsoid.quaternion.fromArray(record.inertial_quaternion).multiply(new THREE.Quaternion().setFromRotationMatrix(rotation));
      ellipsoid.scale.set(...radii); ellipsoid.visible = false;
      link.group.add(ellipsoid); inertias.push(ellipsoid); overlays.push(ellipsoid);
    } catch (error) { issues.push(`${record.name}: ${error.message}`); }
  }
  toggle("Centers of mass", false, (value) => { for (const center of centers) center.visible = value; }, display);
  toggle("Inertia ellipsoids", false, (value) => { for (const inertia of inertias) inertia.visible = value; }, display);
  listen(axisSize, "change", () => {
    const value = Number(axisSize.value);
    if (!(value >= 0.000001) || !Number.isFinite(value)) { axisSize.value = String(size); return; }
    for (const axis of axes) {
      if (axis.type === "AxesHelper") axis.scale.setScalar(value);
      else axis.setLength(value, value * 0.25, value * 0.12);
    }
    worldAxes.scale.setScalar(value / size);
    for (const center of centers) center.scale.setScalar(value * 0.08);
  });
  const note = element("p", "Display only: RGB = X/Y/Z; yellow = positive joint axis; pink = URDF COM. Inertia ellipsoids are derived from the full URDF tensor in its inertial frame, not body geometry. Colors, opacity, marker sizes and grid placement are viewing aids; they do not change the delivery or establish hardware qualification.", display);
  note.className = "muted small";
  if (issues.length) { const warning = element("p", `Inertia overlay unavailable — ${issues.join("; ")}`, display); warning.className = "viewer-note"; }

  const parts = details("Links and appearance");
  const parentByChild = new Map(allJoints.map((joint) => [joint.child, joint.parent]));
  const ordered = [];
  function visit(name, depth) {
    ordered.push([name, `${"· ".repeat(depth)}${name}`]);
    for (const joint of allJoints.filter((item) => item.parent === name)) visit(joint.child, depth + 1);
  }
  for (const name of links.keys()) if (!parentByChild.has(name)) visit(name, 0);
  const linkSelect = select("Link tree ", [["", "Select a link"], ...ordered], parts);
  const partControls = element("div", null, parts); partControls.className = "inspection-toolbar";
  const visible = toggle("Visible", true, (value) => { if (selected) links.get(selected).visuals.visible = value; }, partControls);
  const opacity = field("Opacity multiplier", "range", partControls); opacity.min = "0"; opacity.max = "1"; opacity.step = "0.01"; opacity.value = "1";
  const color = field("Preview color", "color", partControls);
  const selectionInfo = element("p", "Click a visible mesh or select a link.", parts); selectionInfo.className = "muted small";
  const highlight = new THREE.Box3Helper(new THREE.Box3(), 0xf4b400); viewer.scene.add(highlight); overlays.push(highlight); highlight.visible = false;
  function choose(name) {
    selected = name || null; linkSelect.value = name;
    const entry = links.get(name);
    for (const input of [visible, opacity, color]) input.disabled = !entry;
    if (!entry) { highlight.visible = false; return; }
    visible.checked = entry.visuals.visible;
    opacity.value = String(entry.visuals.userData.opacity ?? 1);
    color.value = `#${(entry.visuals.children[0]?.material.color.getHex() ?? 0x8fa8bf).toString(16).padStart(6, "0")}`;
    const incoming = allJoints.find((joint) => joint.child === name);
    jointSelect.value = incoming?.type !== "fixed" ? incoming?.name || "" : "";
    selectionInfo.textContent = `${name} · parent: ${parentByChild.get(name) || "root"}${incoming ? ` · joint: ${incoming.name} (${incoming.type})` : ""}`;
    const record = records.links.find((item) => item.name === name);
    const children = allJoints.filter((joint) => joint.parent === name).map((joint) => joint.name);
    selectionInfo.textContent += ` · child joints: ${children.join(", ") || "none"} · visuals: ${entry.visuals.children.length} · mass: ${record.mass_kg ?? "not provided"} kg`;
    if (record.inertia_kg_m2) selectionInfo.textContent += ` · inertia (kg*m²): ${Object.entries(record.inertia_kg_m2).map(([key, value]) => `${key}=${value}`).join(", ")}`;
    update();
  }
  listen(linkSelect, "change", () => choose(linkSelect.value));
  function applyOpacity(entry, value) {
    entry.visuals.userData.opacity = value;
    for (const mesh of entry.visuals.children) {
      mesh.material.opacity = mesh.userData.sourceStyle.opacity * value;
      mesh.material.transparent = mesh.material.opacity < 1; mesh.material.depthWrite = !mesh.material.transparent;
      mesh.material.needsUpdate = true;
    }
  }
  listen(opacity, "input", () => { if (selected) applyOpacity(links.get(selected), Number(opacity.value)); });
  listen(color, "input", () => { if (selected) for (const mesh of links.get(selected).visuals.children) mesh.material.color.set(color.value); });
  function resetAppearance(entry) {
    entry.visuals.visible = true; entry.visuals.userData.opacity = 1;
    for (const mesh of entry.visuals.children) {
      mesh.material.color.copy(mesh.userData.sourceStyle.color); mesh.material.opacity = mesh.userData.sourceStyle.opacity;
      mesh.material.transparent = mesh.material.opacity < 1; mesh.material.depthWrite = !mesh.material.transparent;
      mesh.material.wireframe = wireframe; mesh.material.needsUpdate = true;
    }
  }
  button("Reset selected appearance", () => { if (selected) { resetAppearance(links.get(selected)); choose(selected); } }, partControls);
  button("Reset all appearances", () => { for (const entry of links.values()) resetAppearance(entry); choose(selected || ""); }, partControls);
  button("Show all", () => { for (const entry of links.values()) { entry.visuals.visible = true; applyOpacity(entry, 1); } choose(selected || ""); }, partControls);
  button("Half opacity", () => { for (const entry of links.values()) { entry.visuals.visible = true; applyOpacity(entry, 0.5); } choose(selected || ""); }, partControls);
  button("Hide all", () => { for (const entry of links.values()) entry.visuals.visible = false; choose(selected || ""); }, partControls);
  choose("");

  const measure = details("Measure");
  const measureToolbar = element("div", null, measure); measureToolbar.className = "inspection-toolbar";
  const pointMode = toggle("Pick two surface points", false, () => { dragMode.checked = false; clearMeasurement(); }, measureToolbar);
  const jointOptions = [["", "Select joint origin"], ...allJoints.map((joint) => [joint.name, joint.name])];
  const from = select("From ", jointOptions, measureToolbar), to = select("To ", jointOptions, measureToolbar);
  const measurement = element("output", "No measurement.", measure); measurement.setAttribute("aria-live", "polite");
  const measureNote = element("p", "Distances use the current articulated pose in meters. Surface picks measure the delivered tessellated mesh, not exact CAD geometry; joint measurements use URDF joint-frame origins. Endpoints follow their bound link or joint when the pose changes.", measure); measureNote.className = "muted small";
  const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]), new THREE.LineBasicMaterial({ color: 0xe83e8c, depthTest: false }));
  const markers = [0, 1].map(() => new THREE.Mesh(new THREE.SphereGeometry(size * 0.08, 12, 8), new THREE.MeshBasicMaterial({ color: 0xe83e8c, depthTest: false })));
  measurements.add(line, ...markers); measurements.visible = false;
  function clearMeasurement() {
    endpoints = []; from.value = ""; to.value = "";
    measurements.visible = false; measurement.textContent = pointMode.checked ? "Click the first surface point." : "No measurement.";
  }
  button("Clear measurement", clearMeasurement, measureToolbar);
  function measureJoints() {
    pointMode.checked = false;
    endpoints = [from, to].map((input) => allJoints.find((joint) => joint.name === input.value)).filter(Boolean)
      .map((joint) => ({ object: joint.pivot, local: new THREE.Vector3(), label: joint.name }));
    update();
  }
  listen(from, "change", measureJoints); listen(to, "change", measureJoints);
  const raycaster = new THREE.Raycaster();
  function pointerRay(event) {
    const rect = viewer.renderer.domElement.getBoundingClientRect();
    viewer.robotRoot.updateMatrixWorld(true);
    raycaster.setFromCamera(new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, 1 - (event.clientY - rect.top) / rect.height * 2), viewer.camera);
    return raycaster.ray;
  }
  let dragState = null;
  function beginDrag(event) {
    if (!dragMode.checked) return false;
    const joint = loaded.joints.find((item) => item.name === jointSelect.value);
    if (!joint) return false;
    joint.pivot.updateWorldMatrix(true, false);
    const origin = joint.pivot.getWorldPosition(new THREE.Vector3());
    const axis = joint.axis.clone().transformDirection(joint.pivot.matrixWorld);
    const normal = joint.type === "prismatic" ? viewer.camera.getWorldDirection(new THREE.Vector3()) : axis;
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(normal, origin);
    const point = pointerRay(event).intersectPlane(plane, new THREE.Vector3());
    const projectedAxis = axis.clone().addScaledVector(normal, -axis.dot(normal));
    if (!point || (joint.type === "prismatic" ? projectedAxis.lengthSq() < 1e-8 : point.distanceTo(origin) < size * 0.01)) return false;
    dragState = { joint, origin, axis, plane, projectedAxis, previous: point.clone().sub(origin).normalize(), point, start: joint.value };
    return true;
  }
  function drag(event) {
    if (!dragState) return false;
    const { joint, origin, axis, plane, projectedAxis, previous, point, start } = dragState;
    const next = pointerRay(event).intersectPlane(plane, new THREE.Vector3());
    if (!next) return true;
    let value;
    if (joint.type === "prismatic") value = start + next.clone().sub(point).dot(projectedAxis) / projectedAxis.lengthSq();
    else {
      const direction = next.clone().sub(origin).normalize();
      value = joint.value + Math.atan2(axis.dot(previous.clone().cross(direction)), previous.dot(direction));
      dragState.previous.copy(direction);
    }
    joint.set(joint.type === "continuous" ? value : Math.max(joint.lower, Math.min(joint.upper, value)));
    onPoseChange?.(); update(); return true;
  }
  function pick(event) {
    pointerRay(event);
    const meshes = [...links.values()].filter((entry) => entry.visuals.visible).flatMap((entry) => entry.visuals.children.filter((mesh) => mesh.material.opacity > 0));
    const hit = raycaster.intersectObjects(meshes, false)[0];
    if (!hit) return;
    const name = hit.object.userData.linkName; choose(name);
    const entry = links.get(name), local = entry.group.worldToLocal(hit.point.clone());
    selectionInfo.textContent += ` · picked link xyz (m): ${local.toArray().map((value) => value.toFixed(6)).join(", ")}`;
    if (pointMode.checked) {
      if (endpoints.length === 2) endpoints = [];
      endpoints.push({ object: entry.group, local, label: name }); update();
    }
  }
  function update() {
    viewer.robotRoot.updateMatrixWorld(true);
    const box = visualBounds(loaded);
    viewer.grid.position.z = ground && !box.isEmpty() ? box.min.z : 0;
    viewer.grid.scale.setScalar(span / 2);
    if (selected) {
      highlight.box.copy(new THREE.Box3().setFromObject(links.get(selected).visuals));
      highlight.visible = links.get(selected).visuals.visible && !highlight.box.isEmpty();
    }
    measurements.visible = endpoints.length > 0;
    const points = endpoints.map((endpoint) => endpoint.object.localToWorld(endpoint.local.clone()));
    markers.forEach((marker, i) => { marker.visible = !!points[i]; if (points[i]) marker.position.copy(points[i]); });
    line.visible = points.length === 2;
    if (points.length === 2) {
      const positions = line.geometry.attributes.position;
      points.forEach((point, i) => positions.setXYZ(i, point.x, point.y, point.z)); positions.needsUpdate = true;
      line.geometry.computeBoundingSphere();
      const delta = points[1].clone().sub(points[0]);
      measurement.textContent = `${endpoints[0].label} → ${endpoints[1].label}: ${delta.length().toFixed(6)} m · Δ world xyz: ${delta.toArray().map((value) => value.toFixed(6)).join(", ")} m`;
    } else if (points.length === 1) measurement.textContent = `${endpoints[0].label} selected. Choose the second endpoint.`;
    else measurement.textContent = pointMode.checked ? "Click the first surface point." : "No measurement.";
    const joint = loaded.joints.find((item) => item.name === jointSelect.value);
    arc.visible = !!joint && joint.type !== "prismatic";
    if (arc.visible) {
      const axis = joint.axis;
      const u = new THREE.Vector3(Math.abs(axis.x) < 0.9 ? 1 : 0, Math.abs(axis.x) < 0.9 ? 0 : 1, 0).cross(axis).normalize();
      const v = axis.clone().cross(u);
      const points = Array.from({ length: 33 }, (_, i) => {
        const angle = joint.value * i / 32;
        return joint.pivot.localToWorld(u.clone().multiplyScalar(size * Math.cos(angle)).addScaledVector(v, size * Math.sin(angle)));
      });
      arc.geometry.setFromPoints(points); arc.geometry.computeBoundingSphere();
    }
    updateScreen();
  }
  function updateScreen() {
    const joint = loaded.joints.find((item) => item.name === jointSelect.value);
    jointBadge.hidden = !joint;
    if (!joint) return;
    const point = joint.pivot.getWorldPosition(new THREE.Vector3()).project(viewer.camera);
    jointBadge.hidden = point.z < -1 || point.z > 1 || Math.abs(point.x) > 1 || Math.abs(point.y) > 1;
    jointBadge.style.left = `${(point.x + 1) * 50}%`; jointBadge.style.top = `${(1 - point.y) * 50}%`;
    jointBadge.textContent = `${joint.name}: ${joint.value.toFixed(4)} ${joint.type === "prismatic" ? "m" : "rad"}`;
  }
  // One callback for pose-dependent overlays; no second animation loop or kinematic model.
  const inspection = {
    pick, update, updateScreen, fit, beginDrag, drag, endDrag() { dragState = null; },
    dispose() {
      controls.abort(); container.textContent = "";
      jointBadge.remove();
      for (const object of [...overlays, measurements]) {
        object.removeFromParent();
        object.traverse((node) => { node.geometry?.dispose(); node.material?.dispose(); });
      }
      viewer.grid.visible = true; viewer.grid.position.z = 0; viewer.grid.scale.setScalar(1);
      viewer.scene.background.set(0xeef2f7);
    },
  };
  viewer.inspection = inspection;
  update();
  return inspection;
}
