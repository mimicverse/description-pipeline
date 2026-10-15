"""The pre-acquisition capture plan: sealed production, consumption and refusals."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline import steps
from description_pipeline.io import PipelineError, digest, write_json

MAIN = "cad/robot.SLDASM"


def build_package(root: Path) -> Path:
    """One prepared package: sealed discovery, sealed source identity, sealed plan."""

    discovery = {
        "schema_version": "solidworks-to-urdf.native-discovery/v1",
        "derivation": {"main_assembly": MAIN, "configuration": None},
        "native_files": {MAIN: "a" * 64, "cad/part.SLDPRT": "b" * 64},
        "raw": {"saved_configuration": "Default"},
    }
    (root / "discovery").mkdir(parents=True)
    discovery_path = root / "discovery" / "native-discovery.json"
    write_json(discovery_path, discovery)
    write_json(root / "source.json", {"schema_version": "solidworks-to-urdf.source/v1", "robot_name": "robot"})
    files = {str(key): str(value) for key, value in discovery["native_files"].items()}
    plan = {
        "schema_version": "solidworks-to-urdf.capture-plan/v1",
        "run_id": "r-1",
        "handoff_sha256": digest(files),
        "discovery_sha256": hashlib.sha256(discovery_path.read_bytes()).hexdigest(),
        "main_assembly": MAIN,
        "configuration": "Default",
        "identity": {"hardware_id": "m3.0", "revision": "r1"},
        "selection": {"files": files},
        "objects": [{"name": "base_link"}, {"name": "neck-A"}],
        "datums": [],
        "evidence_requests": ["evidence/capture.json", "evidence/geometry/", "evidence/native-runtime.json"],
    }
    write_json(root / "capture-plan.json", plan)
    return root


class CapturePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="capture-plan-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "prepared"
        self.root.mkdir()
        build_package(self.root)

    def _rewrite_plan(self, mutate) -> None:
        plan = json.loads((self.root / "capture-plan.json").read_text(encoding="utf-8"))
        mutate(plan)
        write_json(self.root / "capture-plan.json", plan)

    def test_an_intact_plan_is_consumed_with_the_sealed_source(self) -> None:
        source = steps._consume_sealed_definition(self.root)
        self.assertEqual(source["robot_name"], "robot")

    def test_a_missing_plan_is_refused(self) -> None:
        (self.root / "capture-plan.json").unlink()
        with self.assertRaisesRegex(PipelineError, "no sealed capture plan"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_not_bound_to_the_sealed_discovery_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan.update(discovery_sha256="f" * 64))
        with self.assertRaisesRegex(PipelineError, "does not bind the sealed discovery"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_with_another_selection_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan["selection"]["files"].pop("cad/part.SLDPRT"))
        with self.assertRaisesRegex(PipelineError, "selection differs"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_naming_another_project_entry_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan.update(main_assembly="cad/other.SLDASM"))
        with self.assertRaisesRegex(PipelineError, "another project entry"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_with_another_configuration_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan.update(configuration="Other"))
        with self.assertRaisesRegex(PipelineError, "saved configuration"):
            steps._consume_sealed_definition(self.root)

    def test_a_plan_without_usable_object_selectors_is_refused(self) -> None:
        self._rewrite_plan(lambda plan: plan.update(objects=[{"name": ""}, {"instance": "x"}]))
        with self.assertRaisesRegex(PipelineError, "object selectors"):
            steps._consume_sealed_definition(self.root)

    def test_a_missing_source_identity_is_refused(self) -> None:
        (self.root / "source.json").unlink()
        with self.assertRaisesRegex(PipelineError, "no sealed source identity"):
            steps._consume_sealed_definition(self.root)


if __name__ == "__main__":
    unittest.main()
