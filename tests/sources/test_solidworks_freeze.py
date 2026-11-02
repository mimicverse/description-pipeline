"""Windows Capture seals raw facts and performs no semantic projection."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from description_pipeline.io import file_digest, read_data
from description_pipeline.schemas import (
    NATIVE_CAPTURE_OBSERVATION_KEYS,
    NATIVE_CAPTURE_OBSERVATION_SCHEMA,
)
from description_pipeline.sources.solidworks.errors import CadError, EnvironmentError_
from description_pipeline.sources.solidworks.freeze import capture_prepared

from . import support


class RawCaptureFreezeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="solidworks-raw-freeze-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.assembly = support.make_cad_tree(self.root / "cad")
        self.backend = support.FixtureCadBackend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "transform": support.placement(),
                    "mass": support.mass_payload(
                        1.0,
                        (0.01, 0.02, 0.0),
                    ),
                },
                {
                    "name": "arm-1",
                    "transform": support.placement((0.0, 0.0, 0.2)),
                    "mass": support.mass_payload(0.5),
                },
            ],
            dependencies=[
                self.root / "cad/base.SLDPRT",
                self.root / "cad/arm.SLDPRT",
            ],
            coordinate_systems={
                "CS_base_link": support.placement(),
                "CS_arm_link": support.placement((0.0, 0.0, 0.2)),
            },
        )
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.assembly),
            "configuration": "Default",
            "geometry": {"enabled": True, "format": "stl_binary"},
            "coordinate_systems": ["CS_base_link", "CS_arm_link"],
            "bodies": [
                {
                    "id": "base",
                    "name": "base_link",
                    "components": ["base-1"],
                    "frame": {"coordinate_system": "CS_base_link"},
                },
                {
                    "id": "arm",
                    "name": "arm_link",
                    "components": ["arm-1"],
                    "frame": {"coordinate_system": "CS_arm_link"},
                },
            ],
            "joints": [],
        }
        self.destination = self.root / "capture"

    def capture(self) -> dict:
        return support.capture_prepared_fixture(
            self.config,
            self.destination,
            backend=self.backend,
        )

    def test_capture_seals_only_raw_native_members(self) -> None:
        manifest = self.capture()
        self.assertEqual(
            manifest["scene"],
            "raw/native_capture_facts.json.gz",
        )
        self.assertEqual(
            set(manifest["files"]),
            {
                "native-runtime.json",
                "raw/dependency_closure.json",
                "raw/native_capture_facts.json.gz",
                "raw/triangles/f56f6426f638a44dc1da3320e7d1c79c149af69c01ae6e0f147733f8a5dbeff5.triangles",
            },
        )
        for retired in (
            "capture.json",
            "geometry",
            "raw/native_capture_facts.json",
            "raw/scene_raw.json",
            "raw/mass_properties.json",
            "raw/coordinate_systems.json",
            "raw/link_group_mass_properties.json",
            "raw/material_provenance.json",
            "raw/effective_instances.json",
            "raw/mass_closure.json",
            "raw/geometry.json",
            "raw/axis_references.json",
            "raw/source_scopes.json",
            "raw/inertia_convention.json",
        ):
            self.assertFalse((self.destination / retired).exists(), retired)

        facts = read_data(
            self.destination / "raw/native_capture_facts.json.gz"
        )
        self.assertEqual(
            set(facts),
            set(NATIVE_CAPTURE_OBSERVATION_KEYS),
        )
        self.assertEqual(
            facts["schema_version"],
            NATIVE_CAPTURE_OBSERVATION_SCHEMA,
        )
        self.assertEqual(
            set(facts["source_scopes"]),
            {"base-1", "arm-1"},
        )
        self.assertEqual(
            set(facts["geometry_raw"]),
            {"base-1", "arm-1"},
        )
        self.assertEqual(
            facts["input_stability"]["originals_unchanged"],
            {
                "files_checked": len(
                    facts["input_stability"]["source_hashes"]
                )
            },
        )

    def test_cad_is_released_after_the_native_reads(self) -> None:
        self.capture()
        self.assertEqual(self.backend.order[-1], "release")
        self.assertLess(
            self.backend.order.index("collect_prepared_scene"),
            self.backend.order.index("capture_prepared_geometry"),
        )
        self.assertLess(
            self.backend.order.index("capture_prepared_geometry"),
            self.backend.order.index("release"),
        )

    def test_geometry_identity_mismatch_fails_after_release(self) -> None:
        capture_geometry = self.backend.capture_prepared_geometry

        def mismatched(source_scopes, on_activity=None):
            rows = capture_geometry(
                source_scopes,
                on_activity=on_activity,
            )
            rows.pop("base-1")
            return rows

        self.backend.capture_prepared_geometry = mismatched
        with self.assertRaises(Exception) as caught:
            self.capture()
        self.assertIn("geometry", str(caught.exception).lower())
        self.assertIn("release", self.backend.order)
        failed = self.destination.with_name(self.destination.name + ".failed-001")
        runtime = read_data(failed / "native_observations.json")["runtime"]
        self.assertEqual(set(runtime["document_state"]), {"before", "after"})
        self.assertEqual(runtime["document_state"]["after"]["active_configuration"], "Default")

    def test_failure_runtime_retains_only_completed_observation_prefixes(self) -> None:
        for failing in (
            "prepare_prepared_capture", "open_document", "document_state",
            "environment", "collect_prepared_scene", "cancelled",
        ):
            with self.subTest(failing=failing):
                self.setUp()
                error = (
                    KeyboardInterrupt("Native capture cancelled")
                    if failing == "cancelled"
                    else CadError("native_read_failed", "Native reading failed", {"step": failing})
                )
                observed = {}
                for method in ("prepare_prepared_capture", "open_document", "document_state", "environment"):
                    original = getattr(self.backend, method)

                    def read(
                        *args, _method=method, _original=original,
                        _failing=failing, _error=error, _observed=observed, **kwargs,
                    ):
                        if _method == _failing:
                            raise _error
                        value = _original(*args, **kwargs)
                        _observed[_method] = value
                        return value

                    setattr(self.backend, method, Mock(side_effect=read))
                self.backend.collect_prepared_scene = Mock(side_effect=error)
                with self.assertRaises(type(error)) as caught:
                    self.capture()
                self.assertIs(caught.exception, error)
                failed = self.destination.with_name(self.destination.name + ".failed-001")
                raw = read_data(failed / "native_observations.json")
                runtime = raw["runtime"]
                if failing == "prepare_prepared_capture":
                    self.assertEqual(runtime, {})
                    self.backend.open_document.assert_not_called()
                    self.backend.document_state.assert_not_called()
                    self.backend.environment.assert_not_called()
                    continue
                self.assertEqual(runtime["prepared_capture"]["policy"], observed["prepare_prepared_capture"])
                if failing == "open_document":
                    self.assertEqual(set(runtime), {"prepared_capture"})
                    self.assertEqual(set(runtime["prepared_capture"]), {"policy"})
                else:
                    self.assertEqual(runtime["prepared_capture"]["opened"], observed["open_document"])
                if failing in {"environment", "collect_prepared_scene", "cancelled"}:
                    self.assertEqual(runtime["document_state"], {"before": observed["document_state"]})
                else:
                    self.assertNotIn("document_state", runtime)
                if failing in {"collect_prepared_scene", "cancelled"}:
                    self.assertEqual(runtime["solidworks"], observed["environment"])
                else:
                    self.assertNotIn("solidworks", runtime)
                self.backend.open_document.assert_called_once()
                self.assertEqual(self.backend.document_state.call_count, int(failing != "open_document"))
                self.assertEqual(
                    self.backend.environment.call_count,
                    int(failing in {"environment", "collect_prepared_scene", "cancelled"}),
                )
                self.assertFalse((failed / "partial/native-runtime.json").exists())
                self.assertFalse(self.destination.exists())

    def _check_cleanup_failure(self, primary, cleanup, *, outer_failure=None, at_release=False, partial_scene=False):
        original_release = self.backend.release
        release_calls = []

        def release():
            release_calls.append(len(release_calls) + 1)
            if at_release and len(release_calls) == 1:
                raise primary
            if len(release_calls) == (2 if at_release else 1):
                raise cleanup
            if outer_failure is not None:
                raise outer_failure
            return original_release()

        self.backend.release = Mock(side_effect=release)
        if not at_release:
            if partial_scene:
                original_collect = self.backend.collect_prepared_scene

                def collect(*args, **kwargs):
                    primary.partial_native_facts = original_collect(*args, **kwargs)
                    raise primary

                self.backend.collect_prepared_scene = Mock(side_effect=collect)
            else:
                self.backend.collect_prepared_scene = Mock(side_effect=primary)
        for method in ("open_document", "document_state", "environment"):
            setattr(self.backend, method, Mock(wraps=getattr(self.backend, method)))
        settlement = {}
        propagated = cleanup if not isinstance(cleanup, Exception) else primary
        package = self.assembly.parent
        with self.assertRaises(type(propagated)) as caught:
            capture_prepared(
                support.capture_requests(self.config), self.destination,
                package=package, prepared_root=package,
                native_files={path.name: file_digest(path) for path in [self.assembly, *self.backend._dependencies]},
                backend=self.backend, worker_version="cleanup-retention-test", resource_settlement=settlement,
            )
        self.assertIs(caught.exception, propagated)
        if propagated is cleanup:
            self.assertIs(propagated.__context__, primary)
        failed = self.destination.with_name(self.destination.name + ".failed-001")
        failure = read_data(failed / "failure.json")
        raw = read_data(failed / "native_observations.json")
        self.assertEqual(failure["code"], getattr(primary, "code", type(primary).__name__))
        self.assertEqual(failure["message"], str(primary))
        self.assertEqual(failure["detail"], getattr(primary, "detail", None))
        self.assertIn(type(cleanup).__name__, failure["exceptions"][0]["notes"][0])
        self.assertEqual(raw["cleanup_exceptions"][0]["type"], type(cleanup).__name__)
        self.assertEqual(raw["cleanup_exceptions"][0]["message"], str(cleanup))
        self.assertTrue(raw["cleanup_exceptions"][0]["frames"])
        self.assertEqual(raw["cleanup_exceptions"][1]["type"], type(primary).__name__)
        self.assertEqual(set(raw["runtime"]["document_state"]), {"before", "after"} if at_release else {"before"})
        self.assertIn("solidworks", raw["runtime"])
        if partial_scene:
            self.assertEqual(raw["scene"], primary.partial_native_facts)
        if at_release or partial_scene:
            self.assertTrue(raw["scene"]["scene_raw"]["components"])
        self.assertEqual(self.backend.release.call_count, 3 if at_release else 2)
        self.backend.open_document.assert_called_once()
        self.assertEqual(self.backend.document_state.call_count, 2 if at_release else 1)
        self.backend.environment.assert_called_once()
        self.assertEqual(settlement["settled"], outer_failure is None)
        if outer_failure is not None:
            self.assertEqual(settlement["cleanup_error"]["message"], str(outer_failure))
        self.assertFalse(self.destination.exists())
        return {"failure": failure, "observations": raw, "settlement": settlement,
                "failed_directory": str(failed), "release_calls": self.backend.release.call_count,
                "open_calls": self.backend.open_document.call_count,
                "document_state_calls": self.backend.document_state.call_count,
                "environment_calls": self.backend.environment.call_count,
                "propagated_type": type(caught.exception).__name__, "propagated_identity_preserved": True}

    def test_one_cleanup_interrupt_preserves_primary_failure_before_actual_outer_settlement(self) -> None:
        for outer_fails in (False, True):
            with self.subTest(outer_fails=outer_fails):
                self.setUp()
                self._check_cleanup_failure(
                    CadError("native_read_failed", "Native reading failed", {"phase": "readings"}),
                    KeyboardInterrupt("Cleanup interrupted once"),
                    outer_failure=(
                        EnvironmentError_("cad_process_cleanup_failed", "Exit unproven") if outer_fails else None
                    ),
                    partial_scene=outer_fails,
                )

    def test_primary_cancel_and_cleanup_error_preserve_the_real_propagated_exception(self) -> None:
        for primary, cleanup in (
            (KeyboardInterrupt("Capture cancelled"), EnvironmentError_("cad_cleanup_failed", "Cleanup failed")),
            (CadError("native_read_failed", "Native reading failed"), SystemExit("Cleanup exited")),
            (CadError("native_read_failed", "Native reading failed"), KeyboardInterrupt()),
        ):
            with self.subTest(cleanup=type(cleanup).__name__):
                self.setUp()
                self._check_cleanup_failure(primary, cleanup)

    def test_release_stage_and_cleanup_cancels_retain_actual_after_state_and_scene(self) -> None:
        self._check_cleanup_failure(
            KeyboardInterrupt("Release cancelled"), KeyboardInterrupt("Cleanup cancelled"), at_release=True,
        )

    def test_axis_cancel_retains_completed_raw_prefix_without_further_getters(self) -> None:
        self.config["joints"] = [
            {"name": name, "axis_reference": {"kind": "reference_axis", "feature_name": name}}
            for name in ("joint-a", "joint-b")
        ]
        cancellation = KeyboardInterrupt("Second axis cancelled")
        original_axis = self.backend.capture_axis_reference
        completed = []

        def axis(reference):
            if completed:
                raise cancellation
            reading = original_axis(reference)
            completed.append({"joint": "joint-a", **reading})
            return reading

        self.backend.capture_axis_reference = Mock(side_effect=axis)
        for method in ("open_document", "document_state", "environment", "capture_prepared_geometry"):
            setattr(self.backend, method, Mock(wraps=getattr(self.backend, method)))
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.capture()
        self.assertIs(caught.exception, cancellation)
        self.assertEqual(cancellation.partial_axis_facts, completed)
        failed = self.destination.with_name(self.destination.name + ".failed-001")
        raw = read_data(failed / "native_observations.json")
        self.assertEqual(raw["axis_references"], completed)
        self.assertEqual(len(raw["axis_references"]), 1)
        self.assertTrue(raw["scene"]["scene_raw"]["components"])
        self.assertEqual(set(raw["runtime"]["document_state"]), {"before"})
        self.backend.capture_axis_reference.assert_has_calls([
            unittest.mock.call(joint["axis_reference"]) for joint in self.config["joints"]
        ])
        self.assertEqual(self.backend.capture_axis_reference.call_count, 2)
        self.backend.open_document.assert_called_once()
        self.backend.document_state.assert_called_once()
        self.backend.environment.assert_called_once()
        self.backend.capture_prepared_geometry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
