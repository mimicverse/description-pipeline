"""Nothing may pass silently: the shipped examples must reject every mutation.

The pipeline claims that unknown, unrun and failed checks never pass.  This sweeps a released
workspace with the mutation classes an operator or a bad merge can actually produce — a changed
limit or mass, a dropped joint, a moved body, a relaxed tolerance, a rewritten manifest or source
lock, and a rewritten mesh — and requires every one of them to fail closed (a failed report or a
``PipelineError``).
"""

import json
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from description_pipeline.build import assess, build, freeze, lock_toolchain
from description_pipeline.io import PipelineError
from description_pipeline.verification.urdf_quality.model import ModelError

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "demo-arm"
MESH_EXAMPLE = ROOT / "examples" / "mesh-arm"
IGNORED = shutil.ignore_patterns("build", ".venv", "__pycache__")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8", newline="\n")


def edit_json(path: Path, change) -> None:
    payload = json.loads(read(path))
    change(payload)
    write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def mutate_urdf_limit(root: Path) -> None:
    path = root / "urdf/robot.urdf"
    write(path, read(path).replace('"2.2000000000000002"', '"2.4000000000000002"'))


def mutate_urdf_mass(root: Path) -> None:
    path = root / "urdf/robot.urdf"
    write(path, read(path).replace('mass value="0.45000000000000001"', 'mass value="0.55000000000000001"'))


def mutate_urdf_drop_joint(root: Path) -> None:
    path = root / "urdf/robot.urdf"
    write(path, re.sub(r'\s*<joint name="elbow_joint".*?</joint>', "", read(path), flags=re.S))


#: A hostile delivery can carry a DTD.  CPython bundles an Expat with an amplification guard, so this
#: is a parse error in milliseconds; the pipeline has to turn that into a diagnostic, which is what
#: the mutation sweep asserts, and the timing test below keeps a silent expansion from creeping back.
ENTITY_BOMB = (
    '<?xml version="1.0"?>\n'
    "<!DOCTYPE robot [\n"
    '<!ENTITY a "aaaa">\n'
    '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">\n'
    '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">\n'
    '<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">\n'
    '<!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">\n'
    '<!ENTITY f "&e;&e;&e;&e;&e;&e;&e;&e;&e;&e;">\n'
    '<!ENTITY g "&f;&f;&f;&f;&f;&f;&f;&f;&f;&f;">\n'
    '<!ENTITY h "&g;&g;&g;&g;&g;&g;&g;&g;&g;&g;">\n'
    "]>\n"
    '<robot name="&h;"><link name="base_link"/></robot>\n'
)
EXTERNAL_ENTITY = (
    '<?xml version="1.0"?>\n<!DOCTYPE robot [<!ENTITY secret SYSTEM "file:///etc/passwd">]>\n<robot name="&secret;"/>\n'
)


def mutate_urdf_entity_bomb(root: Path) -> None:
    write(root / "urdf/robot.urdf", ENTITY_BOMB)


def mutate_urdf_external_entity(root: Path) -> None:
    write(root / "urdf/robot.urdf", EXTERNAL_ENTITY)


def mutate_mjcf_entity_bomb(root: Path) -> None:
    write(root / "mjcf/robot.xml", ENTITY_BOMB)


def mutate_mjcf_body(root: Path) -> None:
    path = root / "mjcf/robot.xml"
    write(
        path,
        read(path).replace(
            '<body name="forearm_link" pos="0 0 0.17999999999999999"',
            '<body name="forearm_link" pos="0 0 0.20000000000000001"',
        ),
    )


def mutate_manifest_digest(root: Path) -> None:
    edit_json(root / "manifest.json", lambda payload: payload["files"].__setitem__("urdf/robot.urdf", "0" * 64))


def mutate_profile_tolerance(root: Path) -> None:
    edit_json(root / "config/profiles/kinematics.json", lambda payload: payload.__setitem__("rotation_atol", 0.01))


def mutate_source_lock(root: Path) -> None:
    edit_json(root / "sources/source.lock.json", lambda payload: payload.__setitem__("manifest_digest", "f" * 64))


MUTATIONS = {
    "urdf joint limit": mutate_urdf_limit,
    "urdf link mass": mutate_urdf_mass,
    "urdf dropped joint": mutate_urdf_drop_joint,
    "urdf entity bomb": mutate_urdf_entity_bomb,
    "urdf external entity": mutate_urdf_external_entity,
    "mjcf body pose": mutate_mjcf_body,
    "mjcf entity bomb": mutate_mjcf_entity_bomb,
    "manifest digest": mutate_manifest_digest,
    "profile tolerance": mutate_profile_tolerance,
    "source lock digest": mutate_source_lock,
}


def rewrite_last_triangle(path: Path) -> None:
    """Corrupt the final triangle so the mesh no longer matches its declared geometry."""

    data = bytearray(path.read_bytes())
    data[-8:] = bytes(8)
    path.write_bytes(bytes(data))


def mutate_delivered_mesh(root: Path) -> None:
    rewrite_last_triangle(root / "meshes/visual/base_link_0.stl")


def mutate_fixture_mesh(root: Path) -> None:
    rewrite_last_triangle(root / "sources/fixture/geometry/parts/base.stl")


MESH_MUTATIONS = {
    "delivered mesh": mutate_delivered_mesh,
    "fixture mesh": mutate_fixture_mesh,
}


class ExampleMutationTests(unittest.TestCase):
    def prepare(self, source: Path, temporary: str) -> Path:
        """Re-lock a shipped example to this tool and assert the clean copy really passes.

        The examples ship the lock of the released tool, so a test that assesses them unchanged would
        raise ``Toolchain differs from lock`` before any check runs — every mutation would "fail" for
        the wrong reason.  Re-lock, rebuild, and require the unmutated baseline to pass.
        """

        workspace = Path(temporary) / source.name
        shutil.copytree(source, workspace, ignore=IGNORED)
        lock_toolchain(workspace)
        freeze(workspace)
        baseline = build(workspace, "kinematics")
        self.assertTrue(baseline["passed"], baseline["blockers"] + [baseline.get("diagnostic_path", "")])
        return workspace

    def test_every_mutation_fails_closed(self):
        for name, mutate in MUTATIONS.items():
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as temporary:
                workspace = self.prepare(EXAMPLE, temporary)
                mutate(workspace)
                try:
                    report = assess(workspace, "kinematics")
                except (PipelineError, ModelError):
                    continue  # A hard error is also a rejection.
                self.assertFalse(report["passed"], f"{name} passed silently: {report.get('blockers')}")

    def test_mesh_mutations_fail_closed(self):
        for name, mutate in MESH_MUTATIONS.items():
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as temporary:
                workspace = self.prepare(MESH_EXAMPLE, temporary)
                mutate(workspace)
                try:
                    report = assess(workspace, "kinematics")
                except (PipelineError, ModelError):
                    continue  # A hard error is also a rejection.
                self.assertFalse(report["passed"], f"{name} passed silently: {report.get('blockers')}")

    def test_an_entity_bomb_is_rejected_quickly(self):
        """A DTD must be a diagnostic in milliseconds, never an expansion to wait for."""

        for name in ("urdf entity bomb", "urdf external entity", "mjcf entity bomb"):
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as temporary:
                workspace = self.prepare(EXAMPLE, temporary)
                MUTATIONS[name](workspace)
                started = time.monotonic()
                try:
                    report = assess(workspace, "kinematics")
                    self.assertFalse(report["passed"], f"{name} passed silently")
                except (PipelineError, ModelError):
                    pass  # The CLI turns this into the diagnostic envelope.
                self.assertLess(time.monotonic() - started, 30.0, f"{name} took too long to reject")


if __name__ == "__main__":
    unittest.main()
