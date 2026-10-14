"""The manifest policy slot is the check-policy family; the resource policy stays request-bound."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from description_pipeline.delivery import write_delivery_manifest  # noqa: E402
from description_pipeline.interface_records import (  # noqa: E402
    CHECK_POLICY_SCHEMA,
    RESOURCE_POLICY_V1,
    check_policy_digest,
    resource_policy_v1_digest,
)
from description_pipeline.io import write_json  # noqa: E402
from description_pipeline.stages import CONTRACT, CONTRACT_SHA256  # noqa: E402

CONTRACT_BLOCK = {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256}


class ManifestPolicyTests(unittest.TestCase):
    def _tree(self, root: Path) -> None:
        for name in ("input", "evidence", "model", "urdf", "meshes"):
            (root / name).mkdir(parents=True)
        write_json(root / "input/handoff.json", {"passed": True})
        write_json(root / "evidence/discovery.json", {"schema_version": "fixture"})
        write_json(root / "model/robot.json", {"schema_version": "fixture"})
        (root / "urdf/robot.urdf").write_text("<robot/>\n", encoding="utf-8")
        (root / "meshes/part.stl").write_bytes(b"solid part\nendsolid part\n")
        (root / "tool.json").write_text("{}\n", encoding="utf-8")

    def test_positive_item_is_the_canonical_check_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._tree(root)
            manifest = write_delivery_manifest(
                root,
                contract=CONTRACT_BLOCK,
                policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
            )
            self.assertEqual(manifest["policy"]["schema_version"], CHECK_POLICY_SCHEMA)
            self.assertEqual(manifest["policy"]["digest"], check_policy_digest())

    def test_resource_policy_in_the_manifest_slot_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._tree(root)
            with self.assertRaises(Exception) as caught:  # noqa: B017 - the machine error carries its code
                write_delivery_manifest(
                    root,
                    contract=CONTRACT_BLOCK,
                    policy={
                        "schema_version": RESOURCE_POLICY_V1["schema_version"],
                        "digest": resource_policy_v1_digest(),
                    },
                )
            detail = str(getattr(caught.exception, "code", "") or caught.exception)
            self.assertIn("delivery_manifest_invalid", detail)
