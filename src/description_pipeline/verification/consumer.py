"""Load delivered URDF bytes in a fresh, bounded consumer process; no rendering."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

TIMEOUT_SECONDS = 30


class ConsumerError(RuntimeError):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


def _inputs(root):
    paths = [root / "urdf/robot.urdf", *(root / "meshes").rglob("*")]
    result = {}
    for path in sorted(paths):
        if path.is_symlink() or path.is_junction():
            raise ConsumerError("Consumer inputs cannot be filesystem links")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def load(root: Path) -> dict:
    """Use the current locked interpreter without inheriting producer Python state."""
    root = Path(root).resolve()
    inputs = _inputs(root)
    document = ET.parse(root / "urdf/robot.urdf").getroot()
    bodies = sorted(link.attrib["name"] for link in document.findall("link"))
    joints = sorted(joint.attrib["name"] for joint in document.findall("joint") if joint.attrib["type"] != "fixed")
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), str(root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        # subprocess.run kills and waits for the child before raising.
        raise ConsumerError("Consumer loading timed out", timeout_seconds=TIMEOUT_SECONDS) from error
    if result.returncode:
        raise ConsumerError("Consumer loading failed", returncode=result.returncode, stderr=result.stderr)
    try:
        report = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise ConsumerError("Consumer returned invalid JSON", stdout=result.stdout, stderr=result.stderr) from error
    if not isinstance(report, dict) or report.get("inputs") != inputs or _inputs(root) != inputs:
        raise ConsumerError("Consumer input hashes differ from delivered bytes")
    if (
        report.get("reader") != "mujoco"
        or report.get("version") != importlib.metadata.version("mujoco")
        or type(report.get("bodies")) is not int
        or report["bodies"] != len(bodies) + 1
        or type(report.get("joints")) is not int
        or report["joints"] != len(joints)
        or report.get("body_names") != bodies
        or report.get("joint_names") != joints
    ):
        raise ConsumerError("Consumer loaded another link or joint set")
    return report


def readiness() -> dict:
    """Exercise the same loader on a neutral model before starting native work."""
    with tempfile.TemporaryDirectory(prefix="description-consumer-probe-") as directory:
        root = Path(directory)
        (root / "urdf").mkdir()
        (root / "meshes").mkdir()
        (root / "urdf/robot.urdf").write_text(
            '<robot name="consumer_probe"><link name="base_link"><inertial>'
            '<mass value="1"/><inertia ixx="1" ixy="0" ixz="0" iyy="1" iyz="0" izz="1"/>'
            "</inertial></link></robot>",
            encoding="utf-8",
        )
        return load(root)


def _read(root):
    import mujoco

    inputs = _inputs(root)
    document = ET.parse(root / "urdf/robot.urdf").getroot()
    with tempfile.TemporaryDirectory(prefix="description-consumer-") as directory:
        temporary = Path(directory)
        shutil.copytree(root / "meshes", temporary / "meshes")
        (temporary / "urdf").mkdir()
        compiler = ET.SubElement(ET.SubElement(document, "mujoco"), "compiler")
        compiler.attrib.update(discardvisual="false", fusestatic="false", strippath="false")
        path = temporary / "urdf/robot.urdf"
        ET.ElementTree(document).write(path, encoding="utf-8", xml_declaration=True)
        model = mujoco.MjModel.from_xml_path(str(path))
        return {
            "reader": "mujoco",
            "version": mujoco.__version__,
            "inputs": inputs,
            "bodies": model.nbody,
            "joints": model.njnt,
            "body_names": sorted(model.body(i).name for i in range(1, model.nbody)),
            "joint_names": sorted(model.joint(i).name for i in range(model.njnt)),
            "scope": "URDF loading only; no simulation qualification",
        }


if __name__ == "__main__":
    print(json.dumps(_read(Path(sys.argv[1]).resolve()), sort_keys=True))
