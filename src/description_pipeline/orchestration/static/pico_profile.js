// Edit one source-bound draft; the native worker owns profile validation and IK.
function element(tag, properties = {}, children = []) {
  const node = Object.assign(document.createElement(tag), properties);
  node.append(...children);
  return node;
}
function field(label, input, description = "") {
  return element("label", { className: "profile-field" }, [
    element("span", { textContent: label }), input,
    ...(description ? [element("small", { textContent: description })] : []),
  ]);
}
function number(value = "", properties = {}) {
  return element("input", { type: "number", step: "any", value: String(value), ...properties });
}
function focusField(input) {
  for (let details = input.closest("details"); details; details = details.parentElement?.closest("details")) details.open = true;
  input.focus();
}
function numeric(input, path) {
  if (!input.value.trim() || !Number.isFinite(input.valueAsNumber)) {
    focusField(input); throw new Error(`${path}: enter a finite number`);
  }
  return input.valueAsNumber;
}
function select(values, label) {
  const input = element("select");
  input.append(element("option", { value: "", textContent: `Select ${label}` }));
  for (const value of values) input.append(element("option", { value, textContent: value }));
  return input;
}
function checkRotation(matrix) {
  if (matrix.length !== 9 || !matrix.every(Number.isFinite)) throw new Error("Input rotation: enter nine finite numbers");
  for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) {
    const dot = matrix.slice(i * 3, i * 3 + 3).reduce((sum, value, k) => sum + value * matrix[j * 3 + k], 0);
    if (Math.abs(dot - (i === j ? 1 : 0)) > 1e-8) throw new Error("Input rotation: rows must be orthonormal");
  }
  const determinant = matrix[0] * (matrix[4] * matrix[8] - matrix[5] * matrix[7])
    - matrix[1] * (matrix[3] * matrix[8] - matrix[5] * matrix[6])
    + matrix[2] * (matrix[3] * matrix[7] - matrix[4] * matrix[6]);
  if (Math.abs(determinant - 1) > 1e-8) throw new Error("Input rotation: reflections are not permitted");
}
export function sourceChain(model, first, tip) {
  if (!first || !tip) throw new Error("Choose the first controlled joint and the TCP frame");
  const parents = new Map(model.joints.map((joint) => [joint.child, joint]));
  const path = [], visited = new Set();
  let current = tip;
  while (parents.has(current)) {
    if (visited.has(current)) throw new Error("Source joint topology contains a cycle");
    visited.add(current);
    const joint = parents.get(current);
    path.unshift(joint);
    if (joint.name === first) {
      return path.filter((entry) => entry.type !== "fixed").map((entry) => entry.name);
    }
    current = joint.parent;
  }
  throw new Error(`${tip}: frame is not downstream of ${first}`);
}

export function createProfileEditor(container, model, subject, reportError) {
  const defaults = {
    schema_version: 1, id: `source_${subject.slice(0, 20)}`, label: model.robot_name,
    robot_name: model.robot_name, description_env: "UNUSED_PIPELINE_PROFILE", asset_directory: "unused",
    urdf: model.urdf, mjcf: model.mjcf, arms: {}, home_joints: {}, head_joints: [],
    waist_joint: null, leg_joints: [], backend_adapter: "urdf-preview",
    notes: "Explicit software execution conditions; no mechanical or hardware qualification.",
    pose_mode_default: "strict", allow_motion_without_collision_geometry: false,
  };
  let original = null, dirty = true;
  const controlled = model.joints.filter((joint) => joint.type === "revolute").map((joint) => joint.name);
  const movable = model.joints.filter((joint) => joint.type !== "fixed");
  const label = element("input", { type: "text", value: model.robot_name });
  container.append(field("Profile label", label), element("p", {
    textContent: "Choices come from this registered URDF. Home, TCP offsets, alignment and rates are software settings. Source limits and collision geometry do not establish hardware qualification.",
  }));
  const homeInputs = new Map();
  const chains = {};
  function importedChain(joints, tip) {
    if (!Array.isArray(joints) || !joints.length || new Set(joints).size !== joints.length) throw new Error("Controlled joints must be a nonempty unique list");
    const starts = joints.filter((first) => {
      try { const path = sourceChain(model, first, tip); return joints.every((name) => path.includes(name)); }
      catch { return false; }
    });
    if (starts.length !== 1 || joints.some((name) => !controlled.includes(name))) throw new Error("Controlled joints must lie on the TCP chain and be bounded revolute joints");
    return starts[0];
  }
  function chainFields(key, title, offset) {
    const first = select(controlled, "first joint"), tip = select(model.links, "TCP frame");
    first.id = `${key}-first`; tip.id = `${key}-tip`;
    const summary = element("p", { className: "chain-summary", textContent: "No chain selected" });
    const selection = element("div", { className: "chain-selection" });
    let boxes = [];
    const section = element("fieldset", { className: offset ? "arm-config" : "head-config" }, [element("legend", { textContent: title }),
      field("Chain root joint", first), field(offset ? "Arm TCP frame" : "Head tip frame", tip), summary,
      element("small", { textContent: "Checked joints are controlled. Other joints remain at their configured home." }), selection]);
    const offsets = offset ? [0, 1, 2].map(() => number()) : [];
    if (offset) {
      const zero = element("button", { type: "button", textContent: "Use link origin as TCP" });
      zero.onclick = () => { offsets.forEach((input) => { input.value = "0"; }); dirty = true; };
      section.append(element("div", { className: "number-row" }, offsets.map((input, index) => {
        input.id = `${key}-offset-${index}`;
        return field(`TCP ${"XYZ"[index]} offset [m]`, input);
      })), zero);
    }
    const update = (selected) => {
      boxes = []; selection.replaceChildren();
      try {
        const path = sourceChain(model, first.value, tip.value);
        summary.textContent = path.join(" → ");
        for (const name of path) {
          const supported = controlled.includes(name);
          const box = element("input", { type: "checkbox", checked: supported && (!selected || selected.includes(name)), disabled: !supported });
          if (!supported) box.dataset.sourceFixed = "true";
          boxes.push({ name, box });
          selection.append(field(`${name}${supported ? "" : " (fixed at home)"}`, box));
        }
      } catch (error) { summary.textContent = error.message; }
    };
    first.addEventListener("change", () => update()); tip.addEventListener("change", () => update());
    container.append(section);
    return { first, tip, offsets, section, update, selected: () => boxes.filter(({ box }) => box.checked && !box.disabled).map(({ name }) => name) };
  }
  chains.left = chainFields("left", "Left arm", true);
  chains.right = chainFields("right", "Right arm", true);
  const headEnabled = element("input", { type: "checkbox", id: "head-enabled" });
  container.append(field("Enable headset orientation following", headEnabled));
  chains.head = chainFields("head", "Head chain", false);
  chains.head.section.hidden = true;
  headEnabled.onchange = () => { chains.head.section.hidden = !headEnabled.checked; };

  const homes = element("details", { open: true }, [element("summary", { textContent: "Home and fixed joint positions" }),
    element("p", { textContent: "Uncontrolled joints stay at these values. Enter each position, or explicitly choose the source zero pose as the software home." })]);
  const zeroHome = element("button", { type: "button", textContent: "Use source zero pose as software home" });
  zeroHome.onclick = () => { homeInputs.forEach((input) => { input.value = "0"; }); dirty = true; };
  homes.append(zeroHome);
  const homeGrid = element("div", { className: "home-grid" });
  for (const joint of movable) {
    const input = number(); homeInputs.set(joint.name, input);
    const unit = joint.type === "prismatic" ? "m" : "rad";
    homeGrid.append(field(`${joint.name} [${unit}]`, input,
      `Source range: ${joint.lower ?? "unknown"} to ${joint.upper ?? "unknown"} ${unit}`));
  }
  homes.append(homeGrid); container.append(homes);
  const task = select(["strict", "position"], "arm task"); task.value = "strict";
  const alignment = select(["identity", "custom"], "input alignment");
  const rotation = Array.from({ length: 9 }, () => number());
  const rotationGrid = element("div", { className: "rotation-grid", hidden: true }, rotation.map((input, index) =>
    field(`R${Math.floor(index / 3) + 1}${index % 3 + 1}`, input)));
  alignment.onchange = () => { rotationGrid.hidden = alignment.value !== "custom"; };
  const inspection = element("input", { type: "checkbox" });
  container.append(field("Arm task", task, "Strict checks position and orientation; position only does not establish orientation tracking."),
    field("Input-to-base rotation", alignment, "Identity explicitly means the input axes already align with robot axes. Custom must be a proper 3×3 rotation."), rotationGrid,
    field("Allow software motion when collision geometry is unavailable", inspection,
      "Inspection only. Collision safety and hardware control remain unqualified."));
  const budgets = { position_scale: number(0.1, { min: "0", max: "20" }),
    joint_speed_radps: number(1, { min: "0", max: "20" }), head_speed_radps: number(4, { min: "0", max: "20" }) };
  container.append(element("fieldset", { className: "budget-config" }, [element("legend", { textContent: "Software execution budgets" }),
    ...Object.entries(budgets).map(([name, input]) => {
      input.id = name;
      return field({ position_scale: "Hand translation scale", joint_speed_radps: "Arm joint rate [rad/s]", head_speed_radps: "Head joint rate [rad/s]" }[name], input);
    })]));
  container.addEventListener("input", () => { dirty = true; });
  container.addEventListener("change", () => { dirty = true; });

  function load(value) {
    const profile = value.profile;
    if (profile.robot_name !== model.robot_name || profile.urdf !== model.urdf || !model.mjcf_files.includes(profile.mjcf)) {
      throw new Error("Imported profile robot or asset paths differ from this source");
    }
    if (profile.schema_version !== 1) throw new Error("Unsupported profile schema");
    for (const side of ["left", "right"]) {
      const arm = profile.arms?.[side];
      if (!arm?.joints?.length || !model.links.includes(arm.frame)) throw new Error(`${side}: invalid arm binding`);
      importedChain(arm.joints, arm.frame);
      if (arm.home?.length !== arm.joints.length || !arm.home.every(Number.isFinite)
          || arm.tip_offset?.length !== 3 || !arm.tip_offset.every(Number.isFinite)) throw new Error(`${side}: invalid home or TCP offset`);
    }
    if (!Array.isArray(profile.head_joints)) throw new Error("Head joints must be an explicit array");
    if (profile.head_joints.length) importedChain(profile.head_joints, profile.head_tip_frame || "head_link3");
    if (!Array.isArray(profile.input_to_base_rotation) || profile.input_to_base_rotation.length !== 3
        || profile.input_to_base_rotation.some((row) => !Array.isArray(row) || row.length !== 3)) throw new Error("Input rotation must be a 3×3 matrix");
    checkRotation(profile.input_to_base_rotation.flat());
    if (!profile.home_joints || Object.values(profile.home_joints).some((value) => !Number.isFinite(value))) throw new Error("Home positions must be finite numbers");
    const unknown = Object.keys(profile.home_joints).find((name) => !movable.some((joint) => joint.name === name));
    if (unknown) throw new Error(`home.${unknown}: joint is absent from this source`);
    const importedHome = { ...profile.home_joints };
    for (const arm of Object.values(profile.arms)) arm.joints.forEach((name, index) => {
      if (Object.hasOwn(importedHome, name) && importedHome[name] !== arm.home[index]) throw new Error(`home.${name}: conflicting arm home`);
      importedHome[name] = arm.home[index];
    });
    for (const joint of movable) {
      const value = importedHome[joint.name] ?? 0;
      if ((joint.lower !== null && value < Number(joint.lower)) || (joint.upper !== null && value > Number(joint.upper))) throw new Error(`home.${joint.name}: position is outside the source range`);
    }
    if (profile.arms.left.joints.some((name) => profile.arms.right.joints.includes(name))
        || profile.head_joints.some((name) => profile.arms.left.joints.includes(name) || profile.arms.right.joints.includes(name))) throw new Error("Head and arms must have independent controlled joints");
    for (const name of Object.keys(budgets)) {
      const budget = Object.hasOwn(value.settings || {}, name) ? value.settings[name] : (name === "head_speed_radps" ? 4 : undefined);
      if (!Number.isFinite(budget) || budget <= 0 || budget > 20) throw new Error(`${name}: must be positive and at most 20`);
    }
    original = structuredClone({ profile, settings: value.settings });
    label.value = profile.label;
    for (const side of ["left", "right"]) {
      const arm = profile.arms[side], inputs = chains[side];
      inputs.first.value = importedChain(arm.joints, arm.frame); inputs.tip.value = arm.frame;
      inputs.offsets.forEach((input, index) => { input.value = String(arm.tip_offset[index]); }); inputs.update(arm.joints);
    }
    headEnabled.checked = profile.head_joints.length > 0;
    chains.head.section.hidden = !headEnabled.checked;
    chains.head.first.value = headEnabled.checked ? importedChain(profile.head_joints, profile.head_tip_frame || "head_link3") : "";
    chains.head.tip.value = profile.head_tip_frame || "head_link3";
    chains.head.update(profile.head_joints);
    const home = { ...profile.home_joints };
    for (const arm of Object.values(profile.arms)) arm.joints.forEach((name, index) => { home[name] = arm.home[index]; });
    // Saved v1 profiles initialize unspecified scalar joints at software zero.
    homeInputs.forEach((input, name) => { input.value = String(home[name] ?? 0); });
    task.value = profile.pose_mode_default || "strict";
    const matrix = profile.input_to_base_rotation.flat();
    rotation.forEach((input, index) => { input.value = String(matrix[index]); });
    alignment.value = matrix.every((value, index) => value === (index % 4 === 0 ? 1 : 0)) ? "identity" : "custom";
    rotationGrid.hidden = alignment.value !== "custom";
    inspection.checked = profile.allow_motion_without_collision_geometry || false;
    for (const [name, input] of Object.entries(budgets)) input.value = String(value.settings[name] ?? (name === "head_speed_radps" ? 4 : ""));
    dirty = false;
  }
  function read() {
    if (original && !dirty) return structuredClone(original);
    const profile = structuredClone(original?.profile || defaults), home = {};
    if (!label.value.trim()) { label.focus(); throw new Error("Profile label is required"); }
    for (const joint of movable) {
      const input = homeInputs.get(joint.name), value = numeric(input, `home.${joint.name}`);
      if ((joint.lower !== null && value < Number(joint.lower)) || (joint.upper !== null && value > Number(joint.upper))) {
        focusField(input); throw new Error(`home.${joint.name}: position is outside the source range`);
      }
      home[joint.name] = value;
    }
    const arms = {};
    for (const side of ["left", "right"]) {
      const inputs = chains[side];
      let joints;
      try {
        sourceChain(model, inputs.first.value, inputs.tip.value); joints = inputs.selected();
        if (!joints.length) throw new Error("Choose at least one controlled joint");
      }
      catch (error) { inputs.first.focus(); throw new Error(`${side}: ${error.message}`); }
      arms[side] = { joints, frame: inputs.tip.value,
        tip_offset: inputs.offsets.map((input, index) => numeric(input, `${side}.TCP.${"XYZ"[index]}`)), home: joints.map((name) => home[name]) };
    }
    if (arms.left.joints.some((name) => arms.right.joints.includes(name))) throw new Error("Left and right arms must not share controlled joints");
    let headJoints = [];
    if (headEnabled.checked) {
      sourceChain(model, chains.head.first.value, chains.head.tip.value); headJoints = chains.head.selected();
      if (!headJoints.length) throw new Error("Head: choose at least one controlled joint");
    }
    if (headJoints.some((name) => arms.left.joints.includes(name) || arms.right.joints.includes(name))) throw new Error("Head and arms must not share controlled joints");
    if (!alignment.value) { alignment.focus(); throw new Error("Choose an explicit input-to-base alignment"); }
    const matrix = alignment.value === "identity" ? [1, 0, 0, 0, 1, 0, 0, 0, 1] : rotation.map((input, index) => numeric(input, `rotation.${index + 1}`));
    checkRotation(matrix);
    const settings = structuredClone(original?.settings || {});
    for (const [name, input] of Object.entries(budgets)) {
      const value = numeric(input, name);
      if (value <= 0 || value > 20) { input.focus(); throw new Error(`${name}: must be positive and at most 20`); }
      settings[name] = value;
    }
    Object.assign(profile, { label: label.value.trim(), arms, home_joints: home, head_joints: headJoints,
      pose_mode_default: task.value, allow_motion_without_collision_geometry: inspection.checked,
      input_to_base_rotation: [matrix.slice(0, 3), matrix.slice(3, 6), matrix.slice(6, 9)] });
    if (headEnabled.checked) profile.head_tip_frame = chains.head.tip.value;
    return { profile, settings };
  }

  const json = element("textarea", { spellcheck: false });
  const exportButton = element("button", { type: "button", textContent: "Export current draft to JSON" });
  const importButton = element("button", { type: "button", textContent: "Apply JSON to form" });
  exportButton.onclick = () => { try { json.value = JSON.stringify({ subject_sha256: subject, ...read() }, null, 2); } catch (error) { reportError(error); } };
  importButton.onclick = () => {
    try {
      const value = JSON.parse(json.value);
      if (value.subject_sha256 !== subject) throw new Error("Imported profile belongs to another source revision; configure a new source-bound profile");
      load(value); reportError(new Error(""));
    } catch (error) { reportError(error); }
  };
  container.append(element("details", {}, [element("summary", { textContent: "Advanced profile import and export" }),
    element("p", { textContent: "JSON binds this exact source revision, profile and settings. Apply it to this form before starting. Native validation remains required." }),
    field("Profile and settings JSON", json), exportButton, importButton]));
  return { read, load };
}
