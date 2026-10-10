"""Focused all-occurrence imported-provenance tests for native discovery (no CAD)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.verification.native_discovery import DISCOVERY_SCHEMA, INPUT_SCHEMA, verify_discovery

ROOT = r"C:\description-v1-review\probe-46986340\input-copy"
TEMP = r"C:\Users\33985\AppData\Local\Temp\swx42380\IC~~"


def canonical(files):
    return hashlib.sha256(
        (json.dumps(files, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    ).hexdigest()


def accepted_provenance(document, sha):
    return {
        "state": "resolved",
        "source_root": ROOT,
        "native_document_path": rf"{TEMP}\{document}.SLDPRT",
        "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
        "imported_path": ROOT + "\\" + document,
        "kind": "native_imported_document",
        "document": document,
        "sha256": sha,
        "is_virtual": False,
    }


def foreign_provenance():
    return {
        "state": "unread",
        "source_root": ROOT,
        "native_document_path": rf"{TEMP}\HD-1910-C001-20260902.stp.SLDASM",
        "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
        "imported_path": r"D:\M2.2(9.17) DengJingjie - 副本\change11\HD-1910-C001-20260902.stp",
        "kind": None,
        "document": "HD-1910-C001-20260902.stp",
        "sha256": None,
        "is_virtual": None,
        "error": "ValueError: native imported source does not resolve to a file inside the delivered folder",
    }


def foreign_component(name, *, containing=None):
    return {
        "name2": name,
        "document": rf"{TEMP}\HD-1910-C001-20260902.stp.SLDASM",
        "suppressed": False,
        "lightweight": False,
        "suppression_state": 2,
        "source_provenance": foreign_provenance(),
        **({"solver_observation": {"context": {"document": containing}}} if containing is not None else {}),
    }


class ImportedSourceTests(unittest.TestCase):
    def _package(self, directory, contents, components, *, raw_files=None):
        package = Path(directory)
        (package / "discovery").mkdir()
        native = {}
        for name, data in contents.items():
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            native[name] = hashlib.sha256(data).hexdigest()
        payload = {
            "schema_version": DISCOVERY_SCHEMA,
            "contract": "native-discovery/v1",
            "run_id": "imported-source-test",
            "namespace": "dp",
            "raw": {
                "components": components,
                "files": dict(native if raw_files is None else raw_files),
                "identity": {},
            },
            "identity": {},
            "native_files": native,
        }
        record = json.dumps(payload).encode()
        (package / "discovery/native-discovery.json").write_bytes(record)
        (package / "robot.yaml").write_text(
            json.dumps(
                {
                    "schema_version": INPUT_SCHEMA,
                    "hardware_id": None,
                    "source": {"robot_name": "robot"},
                    "provenance": {
                        "generator": "native-discovery",
                        "generator_version": "test",
                        "contract": "native-discovery/v1",
                        "run_id": "imported-source-test",
                        "discovery_sha256": hashlib.sha256(record).hexdigest(),
                        "native_inventory_sha256": canonical(native),
                    },
                }
            )
        )
        return package

    @staticmethod
    def _check(report):
        return next(item for item in report["checks"] if item["id"] == "discovery.imported_sources")

    @staticmethod
    def _unbound(report):
        return next(item for item in report["errors"] if item["code"] == "discovery.imported_source_unbound")

    def test_resealed_evidence_suppression_states_are_refused_by_verification(self) -> None:
        """Independently resealed raw evidence cannot hide an invalid suppression state."""

        content = b"frozen step bytes"
        sha = hashlib.sha256(content).hexdigest()
        baseline = {
            "name2": "NP-F550.step-1",
            "document": "NP-F550.step",
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
            "source_provenance": accepted_provenance("NP-F550.step", sha),
        }
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(directory, {"NP-F550.step": content}, [json.loads(json.dumps(baseline))])
            report = verify_discovery(package)
            sealed_hash = hashlib.sha256((package / "discovery/native-discovery.json").read_bytes()).hexdigest()
        # The binding check stays first so the passed report keeps its sealed provenance hash.
        self.assertEqual(report["checks"][0]["id"], "discovery.binding")
        self.assertEqual(report["discovery_sha256"], sealed_hash)
        self.assertTrue(
            next(item for item in report["checks"] if item["id"] == "discovery.suppression_state")["passed"]
        )
        cases = {
            "missing_state": lambda item: item.pop("suppression_state"),
            "state5": lambda item: item.update(suppression_state=5),
            "unknown": lambda item: item.update(suppression_state=7),
            "boolean_state": lambda item: item.update(suppression_state=True),
            "flag_missing": lambda item: item.pop("lightweight"),
            "flag_nonbool": lambda item: item.update(lightweight=0),
            "flag_mismatch": lambda item: item.update(suppressed=True),
            "lightweight": lambda item: item.update(suppression_state=1, lightweight=True),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                component = json.loads(json.dumps(baseline))
                mutate(component)
                with tempfile.TemporaryDirectory() as directory:
                    package = self._package(directory, {"NP-F550.step": content}, [component])
                    report = verify_discovery(package)
                failed = next(item for item in report["checks"] if item["id"] == "discovery.suppression_state")
                self.assertFalse(failed["passed"], label)
                self.assertIn(
                    failed["details"]["code"],
                    {"discovery.suppression_state", "discovery.suppression_lightweight"},
                )
                self.assertTrue(
                    any(str(item["code"]).startswith("discovery.suppression") for item in report["errors"])
                )

    def test_resealed_malformed_raw_fails_precisely_without_a_crash(self) -> None:
        """A None raw record fails the suppression check instead of crashing route selection."""

        content = b"frozen step bytes"
        sha = hashlib.sha256(content).hexdigest()
        component = {
            "name2": "NP-F550.step-1",
            "document": "NP-F550.step",
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
            "source_provenance": accepted_provenance("NP-F550.step", sha),
        }
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(directory, {"NP-F550.step": content}, [component])
            record_path = package / "discovery/native-discovery.json"
            payload = json.loads(record_path.read_bytes())
            payload["raw"] = None
            record = json.dumps(payload).encode()
            record_path.write_bytes(record)
            robot = json.loads((package / "robot.yaml").read_bytes())
            robot["provenance"]["discovery_sha256"] = hashlib.sha256(record).hexdigest()
            (package / "robot.yaml").write_text(json.dumps(robot))
            report = verify_discovery(package)
        self.assertFalse(report["passed"])
        failed = next(item for item in report["checks"] if item["id"] == "discovery.suppression_state")
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["details"]["code"], "discovery.suppression_state")

    def test_resolved_frozen_source_class_is_preserved(self):
        content = b"frozen step bytes"
        sha = hashlib.sha256(content).hexdigest()
        component = {
            "name2": "NP-F550.step-1",
            "document": "NP-F550.step",
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
            "source_provenance": accepted_provenance("NP-F550.step", sha),
        }
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(directory, {"NP-F550.step": content}, [component])
            report = verify_discovery(package)
        self.assertTrue(self._check(report)["passed"], self._check(report)["details"])
        self.assertNotIn("discovery.imported_source_unbound", {item["code"] for item in report["errors"]})

    def test_frozen_inventory_is_the_sole_authority_when_raw_files_omit_the_document(self):
        content = b"frozen step bytes"
        sha = hashlib.sha256(content).hexdigest()
        component = {
            "name2": "NP-F550.step-1",
            "document": "NP-F550.step",
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
            "source_provenance": accepted_provenance("NP-F550.step", sha),
        }
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(directory, {"NP-F550.step": content}, [component], raw_files={})
            report = verify_discovery(package)
        self.assertTrue(self._check(report)["passed"], self._check(report)["details"])

    def test_foreign_imported_ancestor_under_selected_container_fails(self):
        contents = {"module.SLDASM": b"module bytes", "tool_part.SLDPRT": b"tool bytes"}
        ancestor = foreign_component("module-1", containing="module.SLDASM")
        child = {
            "name2": "module-1/tool_part",
            "document": "tool_part.SLDPRT",
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
        }
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(directory, contents, [ancestor, child])
            report = verify_discovery(package)
        error = self._unbound(report)
        detail = error["detail"]
        self.assertEqual(detail["unbound"], 1, detail)
        self.assertEqual(detail["occurrences"], ["module-1"])
        sample = detail["samples"][0]
        self.assertEqual(sample["occurrence"], "module-1")
        self.assertEqual(sample["document"], rf"{TEMP}\HD-1910-C001-20260902.stp.SLDASM")
        self.assertEqual(sample["containing_document"], "module.SLDASM")
        self.assertIn("IC~~", sample["native_path"])
        self.assertIn("M2.2", sample["imported_path"])
        self.assertIn("unread", sample["reason"])

    def test_all_unbound_occurrences_are_retained(self):
        contents = {"module.SLDASM": b"module bytes", "part.SLDPRT": b"part bytes"}
        first = foreign_component("module-1", containing="module.SLDASM")
        second = foreign_component("module-1/part", containing="module.SLDASM")
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(directory, contents, [first, second])
            report = verify_discovery(package)
        detail = self._unbound(report)["detail"]
        self.assertEqual(detail["unbound"], 2)
        self.assertEqual(detail["occurrences"], ["module-1", "module-1/part"])
        self.assertEqual(len(detail["samples"]), 2)

    def test_resolved_class_still_requires_exact_provenance(self):
        content = b"frozen step bytes"
        sha = hashlib.sha256(content).hexdigest()
        cases = (
            ({"document": "other.step"}, "differs"),
            ({"document": "./NP-F550.step"}, "canonical"),
            ({"is_virtual": True}, "virtual"),
            ({"sha256": "f" * 64}, "bound"),
            ({"imported_path": r"C:\elsewhere\NP-F550.step"}, "frozen root"),
            ({"imported_path": r"D:\elsewhere\NP-F550.step"}, "different drives"),
        )
        for patch, token in cases:
            provenance = accepted_provenance("NP-F550.step", sha)
            provenance.update(patch)
            component_document = "./NP-F550.step" if patch.get("document") == "./NP-F550.step" else "NP-F550.step"
            component = {
                "name2": "NP-F550.step-1",
                "document": component_document,
                "suppressed": False,
                "lightweight": False,
                "suppression_state": 2,
                "source_provenance": provenance,
            }
            with tempfile.TemporaryDirectory() as directory:
                package = self._package(directory, {"NP-F550.step": content}, [component])
                report = verify_discovery(package)
            with self.subTest(patch=patch):
                detail = self._unbound(report)["detail"]
                self.assertEqual(detail["unbound"], 1)
                self.assertIn(token, detail["samples"][0]["reason"])

    def test_raw_recorded_hash_must_agree_with_the_frozen_inventory(self):
        content = b"frozen step bytes"
        sha = hashlib.sha256(content).hexdigest()
        component = {
            "name2": "NP-F550.step-1",
            "document": "NP-F550.step",
            "suppressed": False,
            "lightweight": False,
            "suppression_state": 2,
            "source_provenance": accepted_provenance("NP-F550.step", sha),
        }
        with tempfile.TemporaryDirectory() as directory:
            package = self._package(
                directory,
                {"NP-F550.step": content},
                [component],
                raw_files={"NP-F550.step": "e" * 64},
            )
            report = verify_discovery(package)
        detail = self._unbound(report)["detail"]
        self.assertEqual(detail["unbound"], 1)
        self.assertIn("bound", detail["samples"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
