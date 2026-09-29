"""End-to-end through the root pipeline: build.freeze -> worker HTTP -> snapshot.

This is the integration the contract cares about: the public ``build.freeze``
hands an existing empty directory to the source adapter, the adapter captures a
real snapshot over the worker's HTTP API, and the root verifies the result.
The tests require the complete installed pipeline package.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline import build
from description_pipeline.model import Robot
from description_pipeline.io import read_data, write_json
from description_pipeline.sources.snapshot import verify_snapshot

from description_pipeline.sources.solidworks.freeze import freeze  # noqa: E402
from description_pipeline.sources.solidworks.errors import BridgeError
from description_pipeline.sources.solidworks.verify import verify_normalization  # noqa: E402
from description_pipeline.sources.solidworks.worker import Worker, serve  # noqa: E402

from . import support  # noqa: E402


class RootFreezeLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.build, self.robot_cls, self.verify_snapshot = build, Robot, verify_snapshot
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-root-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.backend = support.FixtureCadBackend(
            self.assembly,
            [
                {"name": "base-1", "transform": support.placement(), "mass": support.mass_payload(1.0)},
                {
                    "name": "arm-1",
                    "transform": support.placement((0.0, 0.0, 0.2)),
                    "mass": support.mass_payload(0.5, (0.0, 0.0, 0.05)),
                },
            ],
            dependencies=[self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"],
        )
        self.worker = Worker(
            jobs_root=self.tmp / "jobs",
            backend_factory=lambda: self.backend,
            freeze_fn=lambda config, destination: freeze(config, destination, backend=self.backend),
            watchdog_seconds=60.0,
        )
        self.server, self.server_thread = serve(self.worker, port=0)
        self.worker_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.root = self.tmp / "model"
        (self.root / "config").mkdir(parents=True)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        self.worker.close()
        support.cleanup(self.tmp)

    def _write_definition(self, documented_masses=None) -> None:
        import yaml

        mass_evidence = None
        if documented_masses:
            # 归档证据文件属于模型输入：写进模型仓库并按内容摘要绑定，
            # 每条声明质量的 evidence 是文件内的定位锚点。
            evidence_dir = self.root / "docs" / "provenance"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            evidence_file = evidence_dir / "mass-spec.json"
            evidence_file.write_text(
                json.dumps({"components": {name: {"source": "fixture spec"} for name in documented_masses}}),
                encoding="utf-8",
            )
            from description_pipeline.io import file_digest

            mass_evidence = {
                "reference": "fixture mass spec",
                "file": "docs/provenance/mass-spec.json",
                "sha256": file_digest(evidence_file),
            }
            documented_masses = {name: dict(entry, evidence=name) for name, entry in documented_masses.items()}
        definition = {
            "schema_version": "description.definition/v1",
            "hardware_id": "fixture-robot",
            "source": {
                "provider": "solidworks",
                "worker_url": self.worker_url,
                "assembly": str(self.assembly),
                "configuration": "Default",
                # the worker in this test runs the fixture backend, so the capture
                # is declared as fixture evidence rather than native CAD
                "evidence_class": "fixture",
                "allowed_roots": [str(self.tmp / "cad")],
                "geometry": {"enabled": True},
                "material_source": "documented_table" if documented_masses else "cad",
                "documented_masses": documented_masses or {},
                **({"mass_evidence": mass_evidence} if mass_evidence else {}),
                "bodies": [
                    {"id": "base", "name": "base_link", "components": ["base-1"]},
                    {
                        "id": "arm",
                        "name": "arm_link",
                        "components": ["arm-1"],
                        "frame": {"xyz": [0.0, 0.0, 0.2], "rpy": [0.0, 0.0, 0.0]},
                    },
                ],
                "joints": [
                    {
                        "id": "hinge",
                        "name": "hinge_joint",
                        "type": "revolute",
                        "parent": "base_link",
                        "child": "arm_link",
                        "xyz": [0.0, 0.0, 0.2],
                        "rpy": [0.0, 0.0, 0.0],
                        "axis": [0.0, 0.0, 1.0],
                        "limits": {"lower": -1.0, "upper": 1.0, "effort": 2.0, "velocity": 3.0},
                    }
                ],
            },
            "overrides": [],
        }
        (self.root / "config" / "robot.yaml").write_text(yaml.safe_dump(definition, sort_keys=False), encoding="utf-8")

    def test_public_freeze_drives_the_worker_and_verifies_the_snapshot(self) -> None:
        self._write_definition()

        locked = self.build.freeze(self.root)

        self.assertEqual(locked["provider"], "solidworks")
        snapshot = self.root / locked["snapshot"]
        manifest = self.verify_snapshot(snapshot)
        self.assertEqual(manifest["evidence_class"], "fixture")
        self.assertEqual(locked["manifest_digest"][:8], self.build.digest(manifest)[:8])
        scene = json.loads((snapshot / "scene.json").read_text(encoding="utf-8"))
        self.robot_cls.from_dict(scene)  # the shared contract accepts the scene
        self.assertEqual(scene["provenance"]["expected_entities"], ["arm-1", "base-1"])

    def test_declared_masses_reach_the_normalized_robot_but_not_the_snapshot(self) -> None:
        # documented_table 的合同是"覆盖全部被纳入的组件"：两个组件都要给质量，
        # 缺一个会让另一个组件悄悄落回 CAD 默认密度占位值。
        self._write_definition(
            documented_masses={
                "base-1": {"mass_kg": 2.5, "reason": "fixture spec", "evidence": "fixture://spec"},
                # 声明值等于 CAD 读数：覆盖完整，但不变动这条 link 的质量（scale = 1）
                "arm-1": {"mass_kg": 0.5, "reason": "fixture spec", "evidence": "fixture://spec"},
            }
        )

        locked = self.build.freeze(self.root)
        snapshot = self.root / locked["snapshot"]
        raw_scene = json.loads((snapshot / "scene.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(raw_scene["links"][0]["inertial"]["mass"], 1.0, places=9)
        self.assertEqual(raw_scene["links"][0]["provenance"]["mass"], "raw/mass_properties.json")

        definition = self.build.definition(self.root)
        normalized = self.build.normalize(raw_scene, definition, self.root, snapshot)
        robot = self.robot_cls.from_dict(normalized)
        masses = {link["name"]: link["inertial"]["mass"] for link in robot.to_dict()["links"]}
        self.assertAlmostEqual(masses["base_link"], 2.5, places=9)
        self.assertAlmostEqual(masses["arm_link"], 0.5, places=9)
        link = next(entry for entry in normalized["links"] if entry["name"] == "base_link")
        record = link["provenance"]["declared_masses"][0]
        self.assertEqual(record["raw_mass_kg"], 1.0)
        self.assertEqual(record["used_mass_kg"], 2.5)
        # the raw input dict was not mutated by the hook
        self.assertEqual(raw_scene["links"][0]["provenance"]["mass"], "raw/mass_properties.json")

        # the same call shape the public assess hook uses
        checks = verify_normalization(raw_scene, definition, snapshot, normalized)
        statuses = {check["id"]: check["status"] for check in checks}
        self.assertTrue(statuses, checks)
        self.assertTrue(all(entry["status"] == "passed" for entry in checks), checks)

    def test_native_failure_diagnostics_do_not_leak_into_author_sources(self) -> None:
        self._write_definition()
        self.build.freeze(self.root)
        previous = (self.root / "sources/source.lock.json").read_bytes()
        definition = self.build.definition(self.root)
        definition["source"].pop("worker_url")
        write_json(self.root / "config/robot.yaml", definition)

        def local_capture(source, destination):
            return freeze(source, destination, backend=self.backend)

        with (
            patch("description_pipeline.sources.solidworks.freeze", side_effect=local_capture),
            patch.object(
                self.backend, "collect_scene", side_effect=BridgeError("fixture_interrupted", "reader stopped")
            ),
            self.assertRaises(BridgeError) as raised,
        ):
            self.build.freeze(self.root)
        diagnostic = Path(vars(raised.exception)["diagnostic_path"])
        self.assertTrue(list(diagnostic.glob("snapshot.failed-*/failure.json")))
        self.assertTrue(list(diagnostic.glob("snapshot.failed-*/partial/source/*.SLDASM")))
        self.assertFalse(list((self.root / "sources").glob(".freeze-*")))
        self.assertEqual(previous, (self.root / "sources/source.lock.json").read_bytes())

    def test_non_empty_destination_is_refused_by_the_public_path(self) -> None:
        self._write_definition()
        sources = self.root / "sources" / "snapshots"
        sources.mkdir(parents=True)
        blocked = sources / "not-empty"
        blocked.mkdir()
        (blocked / "leftover.txt").write_text("x", encoding="utf-8")

        # the adapter itself is what enforces the contract on its destination
        from description_pipeline.sources.solidworks.errors import BridgeError

        with self.assertRaises(BridgeError) as raised:
            freeze(
                {
                    "provider": "solidworks",
                    "assembly": str(self.assembly),
                    "configuration": "Default",
                    "allowed_roots": [str(self.tmp / "cad")],
                    "geometry": {"enabled": False},
                    "bodies": [{"id": "base", "name": "base_link", "components": ["base-1"]}],
                    "joints": [],
                },
                blocked,
                backend=self.backend,
            )
        self.assertEqual(raised.exception.code, "destination_not_empty")

    def test_author_override_has_a_separate_raw_oracle_and_final_derivation(self) -> None:
        self._write_definition()
        locked = self.build.freeze(self.root)
        snapshot = self.root / locked["snapshot"]
        scene = read_data(snapshot / "scene.json")
        definition = self.build.definition(self.root)
        definition["overrides"] = [
            {
                "kind": "links",
                "id": scene["links"][0]["id"],
                "field": "inertial.mass",
                "value": 2.0,
                "reason": "Synthetic measured mass correction for the override contract",
                "evidence": "docs/mass.txt",
            }
        ]
        write_json(self.root / "config/robot.yaml", definition)
        write_json(self.root / "config/profiles/kinematics.json", self.build.PROFILE)
        (self.root / "docs").mkdir()
        (self.root / "docs/mass.txt").write_text("Synthetic test evidence, never live hardware.\n")
        self.build.lock_toolchain(self.root)
        report = self.build.build(self.root, "kinematics")
        checks = {entry["id"]: entry for entry in report["checks"]}
        self.assertEqual(checks["source.derivation"]["status"], "passed")
        self.assertEqual(checks["source.normalization.base_link"]["status"], "passed")
        decision = checks["source.author_decisions"]["details"]["applied_overrides"][0]
        self.assertEqual((decision["previous"], decision["value"]), (1.0, 2.0))
        output = Path(report.get("diagnostic_path", self.root))
        model = read_data(output / "model/robot.json")
        model["links"][0]["inertial"]["mass"] = 3.0
        write_json(output / "model/robot.json", model)
        tampered = self.build.assess(output, "kinematics", verify_manifest=False)
        self.assertIn("source.derivation", tampered["blockers"])
