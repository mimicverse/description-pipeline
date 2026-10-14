"""Capture-side product-convention normalization at the single evidence throat.

Companion layers: ``test_solidworks_inertia_scope`` covers the consumer/verifier
refusal of unqualified records, and ``test_solidworks_product_convention``
keeps the analytic cuboid benchmark.  This file covers the new capture-side
layer:

* the dual-read family decision (standard / positive / mixed / neither /
  low-signal / insufficient samples -> fail closed, never a default family);
* the golden pair of the 2026-10-15 direct-cause report - SW 34.0.0 returns
  the standard products, SW 34.4.1 returns the positive products for the same
  assembly.  The link-group fixtures are copied verbatim from that pair (their
  published sha256 values are pinned below); after normalization both runs
  must carry the same canonical tensors and pass the production closure
  identity with ``CLOSURE_TENSOR_RTOL``;
* the single-write-throat coverage: every tensor recorded by a real freeze
  capture is inside the flipper registry, the transform is an involution, and
  a positive capture normalizes back to the exact original bytes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks import inertia_convention as convention  # noqa: E402
from description_pipeline.sources.solidworks.errors import BridgeError  # noqa: E402
from description_pipeline.sources.solidworks.freeze import freeze  # noqa: E402
from description_pipeline.verification.solidworks_physics import (  # noqa: E402
    CLOSURE_TENSOR_RTOL,
)

from . import support  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "inertia-convention"
STANDARD_RUN = FIXTURES / "standard-34.0.0"
POSITIVE_RUN = FIXTURES / "positive-34.4.1"

#: Published sha256 of the two link-group records (direct-cause evidence file).
PUBLISHED_LINK_SHA256 = {
    STANDARD_RUN: "0caab9133057ee38cc618475262cba167854bc53818c5754853bbc7791791d04",
    POSITIVE_RUN: "47fa22ce4f07cb57fc0342ac84cc823db9e8847fa51206a286a4e1356d705479",
}

_TENSOR_KEYS = {"inertia", "inertia_tensor_about_com", "inertia_values_about_origin_api", "inertia_kg_m2"}


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _link_read(
    *,
    mass: float,
    com: tuple[float, float, float],
    tensor: list[list[float]],
    family: str,
) -> dict:
    """One synthetic dual-read entry: the about-origin recording of ``family``."""

    norm = math.fsum(value * value for value in com)
    sign = -1.0 if family == convention.STANDARD else 1.0
    origin = [
        [
            tensor[row][column]
            + mass * ((norm - com[row] * com[column]) if row == column else sign * com[row] * com[column])
            for column in range(3)
        ]
        for row in range(3)
    ]
    return {
        "mass_kg": mass,
        "com_m": list(com),
        "inertia_tensor_about_com": [list(row) for row in tensor],
        "inertia_values_about_origin_api": [origin[row][column] for row in range(3) for column in range(3)],
        "overrides": {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False},
    }


def _link_record(links: dict[str, list[dict]]) -> dict:
    return {
        "assembly": "fixture.SLDASM",
        "configuration": "Default",
        "links": {
            name: {"selection": [name], "reads": reads} for name, reads in links.items()
        },
    }


_TENSOR = [[2.0e-3, -2.0e-4, -1.0e-4], [-2.0e-4, 3.0e-3, -5.0e-5], [-1.0e-4, -5.0e-5, 4.0e-3]]
_COM = (0.05, -0.03, 0.02)


class FamilyDecisionTests(unittest.TestCase):
    """The dual-read identity decides the family; anything less refuses."""

    def _record(self, family: str) -> dict:
        read = _link_read(mass=1.0, com=_COM, tensor=_TENSOR, family=family)
        return _link_record({"link_a": [read, copy.deepcopy(read)]})

    def test_standard_capture_is_detected_and_takes_no_transform(self) -> None:
        documents = {"link_group_mass_properties.json": self._record(convention.STANDARD)}
        report = convention.normalize_capture_inertia(documents)
        self.assertEqual(report["detected"], convention.STANDARD)
        self.assertFalse(report["capture_transform"]["applied"])
        self.assertGreaterEqual(report["samples"]["decisive"], convention.MIN_DECISIVE_SAMPLES)
        self.assertEqual(report["samples"]["decisive_links"], 1)

    def test_positive_capture_is_detected_flipped_once_and_reproven(self) -> None:
        documents = {"link_group_mass_properties.json": self._record(convention.POSITIVE)}
        original = copy.deepcopy(documents["link_group_mass_properties.json"])
        report = convention.normalize_capture_inertia(documents)
        self.assertEqual(report["detected"], convention.POSITIVE)
        self.assertTrue(report["capture_transform"]["applied"])
        self.assertEqual(report["capture_transform"]["operation"], "negate_products_of_inertia")
        flipped = documents["link_group_mass_properties.json"]["links"]["link_a"]["reads"][0]
        source = original["links"]["link_a"]["reads"][0]
        for row, column in ((0, 1), (0, 2), (1, 2)):
            target = flipped["inertia_tensor_about_com"]
            origin = source["inertia_tensor_about_com"]
            self.assertAlmostEqual(target[row][column], -origin[row][column])
            self.assertAlmostEqual(target[column][row], -origin[column][row])

    def test_mixed_families_are_refused(self) -> None:
        documents = {
            "link_group_mass_properties.json": _link_record(
                {
                    "link_a": [_link_read(mass=1.0, com=_COM, tensor=_TENSOR, family=convention.STANDARD)],
                    "link_b": [_link_read(mass=1.0, com=_COM, tensor=_TENSOR, family=convention.POSITIVE)],
                }
            )
        }
        with self.assertRaises(BridgeError) as raised:
            convention.normalize_capture_inertia(documents)
        self.assertEqual(raised.exception.code, "inertia_convention_disagreement")

    def test_a_sample_fitting_neither_family_is_refused(self) -> None:
        read = _link_read(mass=1.0, com=_COM, tensor=_TENSOR, family=convention.STANDARD)
        read["inertia_values_about_origin_api"][1] += 1e-3  # beyond any rounding
        documents = {"link_group_mass_properties.json": _link_record({"link_a": [read]})}
        with self.assertRaises(BridgeError) as raised:
            convention.normalize_capture_inertia(documents)
        self.assertEqual(raised.exception.code, "inertia_convention_no_fit")

    def test_low_signal_capture_is_refused_instead_of_defaulting(self) -> None:
        # Product signal zero: both families double-fit; the design refuses.
        read = _link_read(mass=1.0, com=(0.0, 0.0, 0.25), tensor=_TENSOR, family=convention.STANDARD)
        documents = {"link_group_mass_properties.json": _link_record({"link_a": [read, copy.deepcopy(read)]})}
        with self.assertRaises(BridgeError) as raised:
            convention.normalize_capture_inertia(documents)
        self.assertEqual(raised.exception.code, "inertia_convention_insufficient_samples")
        self.assertEqual(raised.exception.detail["decisive_samples"], 0)

    def test_one_decisive_sample_is_not_enough(self) -> None:
        read = _link_read(mass=1.0, com=_COM, tensor=_TENSOR, family=convention.STANDARD)
        documents = {"link_group_mass_properties.json": _link_record({"link_a": [read]})}
        with self.assertRaises(BridgeError) as raised:
            convention.normalize_capture_inertia(documents)
        self.assertEqual(raised.exception.code, "inertia_convention_insufficient_samples")
        self.assertEqual(raised.exception.detail["decisive_samples"], 1)

    def test_missing_origin_recording_is_refused(self) -> None:
        read = _link_read(mass=1.0, com=_COM, tensor=_TENSOR, family=convention.STANDARD)
        read["inertia_values_about_origin_api"] = {"error": "GetMomentOfInertia(1) failed"}
        documents = {
            "link_group_mass_properties.json": _link_record({"link_a": [read, copy.deepcopy(read)]})
        }
        with self.assertRaises(BridgeError) as raised:
            convention.normalize_capture_inertia(documents)
        self.assertEqual(raised.exception.code, "inertia_convention_insufficient_samples")
        self.assertEqual(raised.exception.detail["total_samples"], 0)


class GoldenPairTests(unittest.TestCase):
    """SW 34.0.0 (standard) vs SW 34.4.1 (positive): normalize, converge, close."""

    def _documents(self, root: Path) -> dict:
        return {
            "link_group_mass_properties.json": _read_json(root / "link_group_mass_properties.json"),
            "mass_closure.json": _read_json(root / "mass_closure.json"),
        }

    def test_fixtures_are_the_published_bytes(self) -> None:
        for root, digest in PUBLISHED_LINK_SHA256.items():
            payload = (root / "link_group_mass_properties.json").read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)

    def test_each_run_proves_its_own_family(self) -> None:
        standard = convention.detect_family(self._documents(STANDARD_RUN)["link_group_mass_properties.json"])
        positive = convention.detect_family(self._documents(POSITIVE_RUN)["link_group_mass_properties.json"])
        self.assertEqual(
            (standard["family"], standard["decisive"], standard["decisive_links"]),
            (convention.STANDARD, 52, 26),
        )
        self.assertEqual(
            (positive["family"], positive["decisive"], positive["decisive_links"]),
            (convention.POSITIVE, 52, 26),
        )
        for sample in standard["samples"]:
            self.assertLess(sample["residual_standard"], 1e-15)
            self.assertGreater(sample["residual_positive"], 1e-7)
        for sample in positive["samples"]:
            self.assertLess(sample["residual_positive"], 1e-15)
            self.assertGreater(sample["residual_standard"], 1e-7)

    def test_normalization_converges_the_pair(self) -> None:
        standard = self._documents(STANDARD_RUN)
        positive = self._documents(POSITIVE_RUN)
        report = convention.normalize_capture_inertia(positive)
        self.assertEqual(report["detected"], convention.POSITIVE)
        self.assertTrue(report["capture_transform"]["applied"])
        worst = 0.0
        for name, entry in standard["link_group_mass_properties.json"]["links"].items():
            counterpart = positive["link_group_mass_properties.json"]["links"][name]
            for left, right in zip(entry["reads"], counterpart["reads"], strict=True):
                for row in range(3):
                    for column in range(3):
                        value = left["inertia_tensor_about_com"][row][column]
                        delta = abs(value - right["inertia_tensor_about_com"][row][column])
                        self.assertLessEqual(delta, 3e-20 + 1e-15 * abs(value))
                        worst = max(worst, delta)
                for index in range(9):
                    value = left["inertia_values_about_origin_api"][index]
                    delta = abs(value - right["inertia_values_about_origin_api"][index])
                    self.assertLessEqual(delta, 3e-20 + 1e-15 * abs(value))
                    worst = max(worst, delta)
        self.assertLess(worst, 1e-15)
        top_left = standard["mass_closure.json"]["top_level"]["inertia"]
        top_right = positive["mass_closure.json"]["top_level"]["inertia"]
        for row in range(3):
            for column in range(3):
                value = top_left[row][column]
                self.assertLessEqual(abs(value - top_right[row][column]), 3e-20 + 1e-15 * abs(value))
        recheck = convention.detect_family(positive["link_group_mass_properties.json"])
        self.assertEqual(recheck["family"], convention.STANDARD)

    @staticmethod
    def _direct_link_sum(record: dict) -> tuple[float, list[float], list[list[float]]]:
        """The closure ledger's direct link sum (same formula as the verifier)."""

        entries = []
        for entry in record["links"].values():
            read = entry["reads"][0]
            entries.append((float(read["mass_kg"]), list(read["com_m"]), read["inertia_tensor_about_com"]))
        total = math.fsum(mass for mass, _com, _tensor in entries)
        center = [math.fsum(mass * com[index] for mass, com, _tensor in entries) / total for index in range(3)]
        inertia = [[0.0] * 3 for _ in range(3)]
        for mass, com, tensor in entries:
            offset = [com[index] - center[index] for index in range(3)]
            norm = math.fsum(value * value for value in offset)
            for row in range(3):
                for column in range(3):
                    inertia[row][column] += tensor[row][column] + mass * (
                        (norm if row == column else 0.0) - offset[row] * offset[column]
                    )
        return total, center, inertia

    def _closure_delta(self, documents: dict) -> tuple[float, float]:
        """(measured tensor gap, allowed gap) under the production closure constants.

        The golden fixtures carry the link groups and the whole-assembly reading
        only; the unassigned-occurrence ledger of a real bundle is out of scope
        here, exactly as in the 2026-10-15 recompute.  The tensor identity is
        the metric the closure gate reported (``full tensor differs``), with
        the production tolerance ``CLOSURE_TENSOR_RTOL`` and scale.
        """

        record = documents["link_group_mass_properties.json"]
        top = documents["mass_closure.json"]["top_level"]
        _total, _center, summed = self._direct_link_sum(record)
        scale = max(max(abs(value) for row in summed for value in row), 1e-20)
        delta = max(abs(top["inertia"][row][column] - summed[row][column]) for row in range(3) for column in range(3))
        return delta, CLOSURE_TENSOR_RTOL * scale + 1e-15

    def test_closure_identity_passes_for_both_normalized_runs(self) -> None:
        standard = self._documents(STANDARD_RUN)
        positive = self._documents(POSITIVE_RUN)
        convention.normalize_capture_inertia(positive)
        for name, documents in (("standard", standard), ("positive", positive)):
            with self.subTest(run=name):
                delta, allowed = self._closure_delta(documents)
                self.assertLessEqual(delta, allowed)
        # The un-normalized positive capture is exactly the failure the closure
        # gate reported on 2026-10-15 (12.75x over threshold); keep it refused.
        raw_positive = self._documents(POSITIVE_RUN)
        delta, allowed = self._closure_delta(raw_positive)
        self.assertGreater(delta, allowed)
        self.assertAlmostEqual(delta / allowed, 12.75, delta=1.0)

    def test_a_single_flipped_link_is_refused(self) -> None:
        documents = self._documents(STANDARD_RUN)
        read = documents["link_group_mass_properties.json"]["links"]["waist"]["reads"][0]
        for row, column in ((0, 1), (0, 2), (1, 2)):
            read["inertia_tensor_about_com"][row][column] = -read["inertia_tensor_about_com"][row][column]
            read["inertia_tensor_about_com"][column][row] = -read["inertia_tensor_about_com"][column][row]
        for index in (1, 2, 3, 5, 6, 7):
            read["inertia_values_about_origin_api"][index] = -read["inertia_values_about_origin_api"][index]
        with self.assertRaises(BridgeError) as raised:
            convention.normalize_capture_inertia(documents)
        self.assertIn(raised.exception.code, {"inertia_convention_no_fit", "inertia_convention_disagreement"})

    def test_the_transform_is_an_involution(self) -> None:
        documents = self._documents(POSITIVE_RUN)
        original = copy.deepcopy(documents)
        report = convention.normalize_capture_inertia(documents)
        self.assertTrue(report["capture_transform"]["reversible"])
        for name, flipper in convention._FLIPPERS.items():  # noqa: SLF001 - the registry is the transform
            if name in documents:
                flipper(documents[name])
        self.assertEqual(documents, original)


def _tensor_paths(node, path: str = "") -> set[str]:  # noqa: ANN001 - module helper
    """Every leaf path whose key names an inertia tensor recording."""

    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if key in _TENSOR_KEYS:
                found.add(child)
            found |= _tensor_paths(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found |= _tensor_paths(value, f"{path}[{index}]")
    return found


def _differing_paths(left, right, path: str = "") -> set[str]:  # noqa: ANN001 - module helper
    found: set[str] = set()
    if isinstance(left, dict) and isinstance(right, dict) and set(left) == set(right):
        for key in left:
            child = f"{path}.{key}" if path else key
            found |= _differing_paths(left[key], right[key], child)
        return found
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            found |= _differing_paths(a, b, f"{path}[{index}]")
        return found
    if left != right:
        found.add(path)
    return found


class CaptureThroatTests(unittest.TestCase):
    """The freeze throat: sealing, refusal, registry coverage and involution."""

    _DOCUMENTS = (
        "mass_properties.json",
        "scene_raw.json",
        "effective_instances.json",
        "mass_closure.json",
        "material_provenance.json",
        "link_group_mass_properties.json",
    )

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-inertia-convention-"))

    def tearDown(self) -> None:
        support.cleanup(self.tmp)

    def _freeze(self, *, off_axis: bool) -> Path:
        assembly = support.make_cad_tree(self.tmp / "cad")
        com = (0.01, 0.02, 0.0) if off_axis else (0.0, 0.0, 0.0)
        backend = support.FixtureCadBackend(
            assembly,
            [{"name": "base-1", "transform": support.placement(), "mass": support.mass_payload(1.0, com)}],
            dependencies=[self.tmp / "cad" / "base.SLDPRT"],
        )
        config = {
            "provider": "solidworks",
            "assembly": str(assembly),
            "configuration": "Default",
            "allowed_roots": [str(self.tmp / "cad")],
            "geometry": {"enabled": False},
            "bodies": [
                {
                    "id": "base",
                    "name": "base_link",
                    "components": ["base-1"],
                    "frame": {"coordinate_system": "base_datum"},
                }
            ],
            "joints": [],
        }
        snapshot = self.tmp / "snapshot"
        freeze(config, snapshot, backend=backend)
        return snapshot

    def _documents_from(self, snapshot: Path) -> dict[str, dict]:
        raw = snapshot / "raw"
        return {name: _read_json(raw / name) for name in self._DOCUMENTS if (raw / name).is_file()}

    def test_freeze_seals_the_runtime_proof_even_for_a_standard_capture(self) -> None:
        snapshot = self._freeze(off_axis=True)
        report = _read_json(snapshot / "raw" / "inertia_convention.json")
        self.assertEqual(report["detected"], convention.STANDARD)
        self.assertFalse(report["capture_transform"]["applied"])
        self.assertEqual(report["samples"]["decisive"], report["samples"]["usable"])
        self.assertGreaterEqual(report["samples"]["decisive"], convention.MIN_DECISIVE_SAMPLES)
        self.assertEqual(len(report["links"]), 2)  # both reads of the one link group

    def test_freeze_refuses_a_capture_without_product_signal(self) -> None:
        with self.assertRaises(BridgeError) as raised:
            self._freeze(off_axis=False)
        self.assertEqual(raised.exception.code, "inertia_convention_insufficient_samples")
        self.assertFalse((self.tmp / "snapshot").exists())

    def test_a_positive_capture_normalizes_to_the_exact_original_bytes(self) -> None:
        snapshot = self._freeze(off_axis=True)
        documents = self._documents_from(snapshot)
        original = copy.deepcopy(documents)
        # Simulate the 34.4.1 machine on this exact capture: the same tensors
        # with every product negated arrive as the sealed raw evidence.
        for name, flipper in convention._FLIPPERS.items():  # noqa: SLF001 - the registry is the transform
            if name in documents:
                flipper(documents[name])
        self.assertNotEqual(documents, original)
        self.assertEqual(
            convention.detect_family(documents["link_group_mass_properties.json"])["family"], convention.POSITIVE
        )
        report = convention.normalize_capture_inertia(documents)
        self.assertTrue(report["capture_transform"]["applied"])
        self.assertEqual(documents, original)
        # Registry coverage: applying the transform changed only tensor
        # recordings; no tensor-bearing path is outside it.
        flipped = copy.deepcopy(original)
        for name, flipper in convention._FLIPPERS.items():  # noqa: SLF001 - the registry is the transform
            if name in flipped:
                flipper(flipped[name])
        changed: set[str] = set()
        tensors: set[str] = set()
        for name in documents:
            changed |= _differing_paths(original[name], flipped[name])
            tensors |= _tensor_paths(original[name])
        self.assertTrue(changed)
        foreign = {
            path
            for path in changed
            if not any(
                path == tensor or path.startswith((tensor + ".", tensor + "["))
                for tensor in tensors
            )
        }
        self.assertEqual(foreign, set())


if __name__ == "__main__":
    unittest.main()
