"""Saved-display gaps retain native identities without fabricated triangles."""

from __future__ import annotations

import copy
import hashlib
import json
import struct
import tempfile
import unittest
import shutil
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from description_pipeline.backends.urdf import generate_urdf
from description_pipeline.geometry.native_scope import NATIVE_GATE_WARNING_CODES, NATIVE_SHEET_DISPLAY_GAP
from description_pipeline.geometry.native_tessellation import (
    build_native_tessellation,
    materialize_native_stl,
    native_display_gap_value_warnings,
    native_zero_triangle_evidence_is_valid,
    parse_native_tessellation,
    read_native_triangle_stream,
    summarize_native_topology,
    write_native_triangle_stream,
)
from description_pipeline.io import PipelineError, digest, read_data, write_json
from description_pipeline.sources.solidworks.appearance import (
    AppearanceError,
    appearance_groups,
    resolve_deliverable_appearance,
)
from description_pipeline.sources.snapshot import write_manifest
from description_pipeline.sources.solidworks.capture_geometry import (
    materialize_native_geometry,
    seal_native_geometry,
)
from description_pipeline.verification import native_appearance, solidworks_urdf
from description_pipeline.verification.actual_urdf import _warning_comments
from description_pipeline.verification.native_mesh_observations import verify_native_mesh_observations
from description_pipeline.orchestration.portal import _quality_warnings, PortalError
from tests.v1.test_native_scope_warning_projection import (
    quality_report,
    recorded_warnings,
)
from tests.v1 import test_quality as quality_tests

from . import support, test_solidworks_native_reader as reader
from tests.v1 import verification_fixture as hermetic, test_pipeline_verify as verify_fixtures
from tests.v1 import test_generate_handler as generate_fixtures
from description_pipeline.pipeline.stages.publish import _publication_warning_text
from description_pipeline.pipeline.verify import validate_certificate_structure
from description_pipeline.pipeline.verify_contract import certificate_check_details
from description_pipeline.verification import consumer
from . import test_native_triangles_contract as triangles


def gap_evidence(return_kind="null", count=0, area=0.0):
    return {
        "get_tess_triangles_return": return_kind,
        "get_tess_triangle_count": count,
        "get_area_m2": area,
    }


class NativeSheetDisplayGapTests(unittest.TestCase):
    def _capture(self, *, result=None, body_type=1, count=0, area=0.0, error=None):
        with tempfile.TemporaryDirectory() as directory, reader._com_stubs():
            root = Path(directory)
            part = reader._write(root, "part.SLDPRT")
            document = reader._Doc(part)
            positive = reader._mesh_face(
                lambda _: [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
                "positive",
            )
            empty = reader._mesh_face(
                reader._raiser(error) if error else lambda _: result,
                "empty",
            )
            empty.GetTessTriangleCount = count if callable(count) else lambda: count
            empty.GetArea = area if callable(area) else lambda: area
            body = reader._mesh_body([positive, empty], "sheet")
            component = reader._Component("part-1", part, doc=document)
            for face in (positive, empty):
                face._fixture_component = component
            component.GetBodies3 = reader._component_bodies3(
                lambda kind: [body] if kind == body_type else []
            )
            assembly = reader._write(root, "assembly.SLDASM")
            main = reader._Doc(assembly, doc_type=2, children=[component])
            backend = reader._CaptureBackend(
                session_factory=lambda: reader._Session(reader._App({assembly: main}))
            )
            backend.capture_scene_for_test(str(assembly), [])
            return backend.capture_geometry_for_test()["part-1"]

    def test_native_success_keeps_null_and_empty_distinct_without_area_threshold(self):
        for result in (None, []):
            for area in (0, 1e-300, 1e-13, 1.0, 1e100):
                with self.subTest(result=result, area=area):
                    facts = self._capture(result=result, area=area)
                    self.assertEqual(facts["state"], "observed")
                    self.assertEqual(facts["errors"], [])
                    self.assertEqual(facts["body_inventory"][0]["face_count"], 2)
                    self.assertEqual(len(facts["face_inventory"]), 2)
                    gap = facts["face_inventory"][1]
                    self.assertEqual((gap["triangle_start"], gap["triangle_count"]), (1, 0))
                    self.assertEqual(gap["zero_triangle_evidence"], gap_evidence(
                        "null" if result is None else "empty_array", area=area,
                    ))
                    observations = facts["appearance_raw"]["bodies"][0]["native_identity"]["face_body_observations"]
                    self.assertEqual([row["face_index"] for row in observations], [0, 1])

    def test_native_errors_and_unknown_readings_remain_failures(self):
        for kwargs in (
            {"body_type": 0}, {"count": 1}, {"count": None}, {"count": True},
            {"count": 0.0}, {"area": None}, {"area": -1.0}, {"area": True},
            {"area": float("nan")}, {"area": float("inf")},
            {"error": RuntimeError("triangle call failed")},
            {"count": reader._raiser(RuntimeError("count call failed"))},
            {"area": reader._raiser(RuntimeError("area call failed"))},
        ):
            with self.subTest(kwargs=kwargs):
                facts = self._capture(**kwargs)
                self.assertEqual(facts["state"], "partial")
                self.assertTrue(facts["errors"])
                self.assertEqual(len(facts["face_inventory"]), 1)

    def _inventory(self):
        fixture = triangles.NativeTrianglesContractTest()
        fixture.setUp()
        bodies = [
            {"body_type": 1, "body_index": 0, "body_name": "sheet", "face_count": 7},
        ]
        spans = []
        # Leading, consecutive and trailing gaps must never steal a positive owner.
        for index, count in enumerate((0, 1, 0, 0, 1, 1, 1)):
            row = {
                "body_type": 1, "body_index": 0, "face_index": index,
                "triangle_start": sum(item["triangle_count"] for item in spans),
                "triangle_count": count,
            }
            if count == 0:
                row["zero_triangle_evidence"] = gap_evidence(area=index * 1e-13)
            spans.append(row)
        spans.append({**spans[0], "face_index": 7, "triangle_start": 4})
        bodies[0]["face_count"] = 8
        return fixture, bodies, spans

    def test_raw_seal_linux_materialization_and_independent_topology_keep_zero_faces(self):
        fixture, bodies, faces = self._inventory()
        facts = support.fixture_native_geometry(fixture.component, fixture.source_scope)
        facts.update(body_inventory=bodies, face_inventory=faces, bodies={"solid": 0, "sheet": 1},
                     faces=8, triangles=4, triangle_values=fixture.values)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sealed = seal_native_geometry(
                {fixture.component: facts}, {fixture.component: fixture.source_scope}, root,
            )
            self.assertEqual(sealed[fixture.component]["face_inventory"], faces)
            warnings = []
            rows = materialize_native_geometry(
                sealed, {fixture.component: fixture.source_scope}, evidence_root=root,
                destination_root=root / "derived", warnings=warnings,
            )
            row = rows[fixture.component]
            self.assertEqual(len(row["tessellation"]["face_spans"]), 8)
            payload = (root / "derived" / row["path"]).read_bytes()
            owners = [struct.unpack_from("<H", payload, 84 + index * 50 + 48)[0] for index in range(4)]
            self.assertEqual(owners, [2, 5, 6, 7])
            raw = root / sealed[fixture.component]["stream_path"]
            self.assertEqual(read_native_triangle_stream(raw), fixture.triangles)
            projected = []
            spans = parse_native_tessellation(
                fixture.component, row["tessellation"], {"solid": 0, "sheet": 1}, 4,
                source_scope=fixture.source_scope, warnings=projected,
            )
            replay = root / "independent.stl"
            materialize_native_stl(raw, replay, spans)
            self.assertEqual(replay.read_bytes(), payload)
            topology = summarize_native_topology(spans, fixture.triangles)["bodies"][0]
            self.assertEqual((topology["faces"], topology["triangles"]), (8, 4))
            gaps = [item for item in projected if item["code"] == NATIVE_SHEET_DISPLAY_GAP]
            self.assertEqual(len(gaps), 4)
            self.assertTrue(all("complete CAD-surface coverage is not qualified" in item["message"] for item in gaps))

    def test_tampered_proof_and_missing_zero_inventory_are_rejected(self):
        fixture, bodies, faces = self._inventory()
        invalid = [None, {}, gap_evidence(count=True), gap_evidence(count=1),
                   gap_evidence(count=None), gap_evidence(area=-1), gap_evidence(area=float("nan")),
                   gap_evidence(area=float("inf")), gap_evidence("exception")]
        for evidence in invalid:
            with self.subTest(evidence=evidence):
                changed = copy.deepcopy(faces)
                changed[0]["zero_triangle_evidence"] = evidence
                tessellation = build_native_tessellation(
                    fixture.component, bodies, changed, source_scope=fixture.source_scope,
                )
                with self.assertRaises(PipelineError):
                    parse_native_tessellation(fixture.component, tessellation, {"solid": 0, "sheet": 1}, 4,
                                              source_scope=fixture.source_scope, warnings=[])
        for mutation in ("drop", "duplicate", "reference", "start", "positive_proof"):
            tessellation = build_native_tessellation(
                fixture.component, bodies, faces, source_scope=fixture.source_scope,
            )
            spans = tessellation["face_spans"]
            if mutation == "drop":
                spans.pop(0)
                tessellation["faces"] -= 1
            elif mutation == "duplicate":
                spans[2] = dict(spans[0])
            elif mutation == "reference":
                spans[0]["face_reference_sha256"] = "0" * 64
            elif mutation == "positive_proof":
                spans[1]["zero_triangle_evidence"] = gap_evidence()
            else:
                spans[0]["triangle_start"] = 1
            with self.subTest(mutation=mutation), self.assertRaises(PipelineError):
                parse_native_tessellation(fixture.component, tessellation, {"solid": 0, "sheet": 1}, 4,
                                          source_scope=fixture.source_scope, warnings=[])
        self.assertFalse(native_zero_triangle_evidence_is_valid(0, gap_evidence()))

    def test_zero_faces_keep_appearance_validation_but_never_create_empty_meshes(self):
        fixture_path = Path(__file__).parents[1] / "fixtures/native-appearance-part-face-override.json"
        case = json.loads(fixture_path.read_text())
        spans = case["face_spans"]
        # Keep the same native solid fixture for full source/owner resolution.
        resolved = resolve_deliverable_appearance(
            case["raw"], component="arm-1", expected_document_path=r"C:\capture\arm.SLDPRT",
            expected_configuration="Painted", face_spans=spans,
        )
        faces = tuple(replace(face, body_type=1) for face in resolved.faces)
        zero = replace(faces[0], triangle_count=0)
        positive = replace(faces[1], triangle_start=0)
        groups = appearance_groups((zero, positive))
        baseline = appearance_groups((positive,))
        self.assertEqual(groups, baseline)
        self.assertEqual(groups[0]["face_references"], [positive.face_reference_sha256])
        with self.assertRaises(PipelineError):
            appearance_groups((replace(zero, rgba=(float("nan"), 0, 0, 1)), positive))
        gap_spans = [
            {**spans[0], "triangle_count": 0, "zero_triangle_evidence": gap_evidence()},
            {**spans[1], "triangle_start": 0},
        ]
        # A solid cannot borrow valid sheet evidence to bypass appearance checks.
        with self.assertRaises(AppearanceError):
            resolve_deliverable_appearance(case["raw"], component="arm-1",
                expected_document_path=r"C:\capture\arm.SLDPRT", expected_configuration="Painted",
                face_spans=gap_spans)
        triangle = [0., 0., 0., 1., 0., 0., 0., 1., 0.]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.triangles"
            write_native_triangle_stream(raw, triangle)
            stl = root / "source.stl"
            materialize_native_stl(raw, stl, [{"triangle_start": 0, "triangle_count": 1, "face_owner_id": 2}])
            source_bytes = stl.read_bytes()
            independent_spans = [
                {**gap_spans[0], "body_type": 1}, {**gap_spans[1], "body_type": 1},
            ]
            row = {"component": "arm-1", "tessellation": {"face_spans": independent_spans},
                   "source_bytes": source_bytes, "sha": hashlib.sha256(source_bytes).hexdigest()}
            observed = {"faces": {
                (1, face.body_index, face.face_index): {"rgba": face.rgba, "source": face.source}
                for face in (zero, positive)
            }}
            expected = native_appearance._expected_groups(root, row, observed)
            self.assertEqual(expected[0]["face_references"], [positive.face_reference_sha256])
            observed["faces"].pop((1, 0, 0))
            with self.assertRaises(PipelineError):
                native_appearance._expected_groups(root, row, observed)

    def test_generate_verify_and_consumer_preserve_an_independent_empty_sheet_body(self):
        original_geometry = support.fixture_native_geometry
        original_appearance = hermetic._fixture_appearance_raw

        def with_gap(component, scope, **kwargs):
            facts = original_geometry(component, scope, **kwargs)
            if component == "base-1":
                facts["body_inventory"].append({
                    "body_type": 1, "body_index": 0,
                    "body_name": "sheet-without-display", "face_count": 1,
                })
                facts["face_inventory"].append({
                    "body_type": 1, "body_index": 0, "face_index": 0,
                    "triangle_start": facts["triangles"], "triangle_count": 0,
                    "zero_triangle_evidence": gap_evidence(area=1e-25),
                })
                facts["bodies"]["sheet"] += 1
                facts["faces"] += 1
            return facts

        def appearance(component, scope, faces):
            raw = original_appearance(component, scope, faces)
            for body in raw["bodies"]:
                if body["body_type"] == 1:
                    identity = body["faces"][0]["appearance"]["native_identity"]
                    identity["persistent_reference"] = [201, 202, 203]
                    identity["feature"]["persistent_reference"] = [204, 205, 206]
            return raw

        fixture = verify_fixtures.PipelineVerifyHandlerTests(methodName="runTest")
        self.addCleanup(fixture.doCleanups)
        with patch.object(support, "fixture_native_geometry", side_effect=with_gap), patch.object(
            hermetic, "_fixture_appearance_raw", side_effect=appearance,
        ):
            fixture.setUp()
        result = fixture._run_verify()
        self.assertEqual(result[1].outcome, "completed")
        certificate = result[2]
        native = next(row for row in certificate["checks"] if row["id"] == "geometry.native_tessellation")
        self.assertTrue(native["passed"])
        appearance_gate = next(row for row in certificate["checks"] if row["id"] == "appearance.native")
        self.assertTrue(appearance_gate["passed"])
        qualification = next(row for row in certificate["qualifications"]
                             if row["fact_id"] == "geometry.native_tessellation")
        self.assertEqual(qualification["status"], "unqualified")
        gaps = [row for row in certificate["diagnostics"] if row["code"] == NATIVE_SHEET_DISPLAY_GAP]
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0]["detail"]["body_type"], gaps[0]["detail"]["face_index"]), (1, 0))
        self.assertEqual(gaps[0]["detail"]["zero_triangle_evidence"], gap_evidence(area=1e-25))
        model = json.loads((fixture.generate_root / "model/robot.json").read_text())
        native_warnings = certificate_check_details(certificate, native)["warnings"]
        self.assertEqual(len(native_display_gap_value_warnings(native_warnings)[0]["objects"]), 1)
        self.assertIn("WARNING " + NATIVE_SHEET_DISPLAY_GAP,
                      (fixture.generate_root / "urdf/robot.urdf").read_text())
        self.assertIn(NATIVE_SHEET_DISPLAY_GAP, _publication_warning_text(certificate))
        # The gap remains prominent even when ordinary provenance exceeds the PR limit.
        crowded = copy.deepcopy(certificate)
        ordinary = next(row for row in crowded["diagnostics"] if row["code"] not in NATIVE_GATE_WARNING_CODES)
        crowded["diagnostics"] += [
            {**copy.deepcopy(ordinary), "object": f"fixture-object-{index}"}
            for index in range(200)
        ]
        crowded["verification_key"] = digest(
            {key: value for key, value in crowded.items() if key != "verification_key"}
        )
        self.assertIn("get_area_m2", _publication_warning_text(crowded))
        self.assertEqual(len([row for row in _quality_warnings(certificate)
                             if row["code"] == NATIVE_SHEET_DISPLAY_GAP]), 1)
        loaded = consumer.load(fixture.generate_root)
        self.assertEqual(loaded["reader"], "mujoco")
        self.assertTrue(all(row["triangle_count"] > 0 for link in model["links"]
                            for shape in link["visuals"] for row in shape.get("triangle_ranges", [])))

        closure = generate_fixtures.GenerateVerifyClosureTests(methodName="runTest")
        closure.fixture = fixture
        closure.view_index = 0
        report, _generated = closure._evaluate_details()
        self.assertTrue(report["passed"])
        view = fixture.root / "independent-view-1"
        facts = read_data(view / "evidence/raw/native_capture_facts.json.gz")
        independently_resolved = native_appearance._resolved_faces(
            facts["geometry_raw"]["base-1"]["appearance_raw"] | {
                "component": "base-1",
                "source_document_path": facts["source_scopes"]["base-1"]["source_document_path"],
                "referenced_configuration": "Default",
            }, configuration="Default", where="empty sheet body",
        )
        self.assertIn((1, 0, 0), independently_resolved["faces"])
        self.assertFalse(any(
            gaps[0]["detail"]["face_reference_sha256"] in shape.get("face_references", [])
            for link in model["links"] for shape in link["visuals"]
        ))
        for mutation in ("drop", "proof"):
            def mutate(payload, mutation=mutation):
                warning = next(row for row in payload["provenance"]["value_warnings"]
                               if row["code"] == NATIVE_SHEET_DISPLAY_GAP)
                if mutation == "drop":
                    payload["provenance"]["value_warnings"].remove(warning)
                else:
                    warning["objects"][0]["zero_triangle_evidence"]["get_area_m2"] = 1.0
            reference, _model, _receipt = closure._reseal_with_emitter("gap-" + mutation, mutate)
            bad = fixture._bundle_report(reference)
            self.assertFalse(bad["passed"])
            gate = next(row for row in bad["checks"] if row["id"] == "provenance.value_warnings")
            self.assertFalse(gate["passed"])
        for mutation in ("count", "area", "solid"):
            target = fixture.root / ("raw-gap-" + mutation)
            shutil.copytree(view, target)
            raw_path = target / "evidence/raw/native_capture_facts.json.gz"
            facts = read_data(raw_path)
            row = facts["geometry_raw"]["base-1"]["face_inventory"][-1]
            if mutation == "solid":
                row["body_type"] = 0
            else:
                row["zero_triangle_evidence"].pop("get_tess_triangle_count" if mutation == "count" else "get_area_m2")
            write_json(raw_path, facts)
            previous = json.loads((target / "evidence/manifest.json").read_text())
            write_manifest(target / "evidence", kind=previous["kind"], identity=previous["identity"],
                           evidence_class=previous["evidence_class"])
            with self.subTest(mutation=mutation), self.assertRaises(PipelineError):
                verify_native_mesh_observations(target, model)

    def test_saved_zero_face_appearance_errors_remain_blocking(self):
        path = Path(__file__).parents[1] / "fixtures/native-appearance-part-face-override.json"
        case = json.loads(path.read_text())

        def sheet_keys(value):
            if isinstance(value, dict):
                return {
                    key: 1 if key == "body_type" and item == 0 else
                    [[1, *row[1:]] for row in item] if key == "occurrence_bodies" else
                    sheet_keys(item) for key, item in value.items()
                }
            return [sheet_keys(item) for item in value] if isinstance(value, list) else value

        case = sheet_keys(case)
        spans = case["face_spans"]
        spans[0].update(triangle_count=0, zero_triangle_evidence=gap_evidence())
        spans[1]["triangle_start"] = 0
        resolution = resolve_deliverable_appearance(
            case["raw"], component="arm-1", expected_document_path=r"C:\capture\arm.SLDPRT",
            expected_configuration="Painted", face_spans=spans,
        )
        self.assertEqual(len(resolution.faces), 2)
        self.assertEqual(len(appearance_groups(resolution.faces)), 1)
        independent = native_appearance._resolved_faces(
            case["raw"], configuration="Painted", where="sheet fixture",
        )
        self.assertEqual(len(independent["faces"]), 2)
        case["raw"]["bodies"][0]["faces"][0]["appearance"]["read_errors"]["material_property_values"] = {
            "code": "fixture_read_failed", "message": "material read failed", "detail": {},
        }
        with self.assertRaises(AppearanceError):
            resolve_deliverable_appearance(
                case["raw"], component="arm-1", expected_document_path=r"C:\capture\arm.SLDPRT",
                expected_configuration="Painted", face_spans=spans,
            )
        with self.assertRaises(PipelineError):
            native_appearance._resolved_faces(case["raw"], configuration="Painted", where="sheet fixture")

    def test_warning_surfaces_and_existing_qualification_keep_the_gap_explicit(self):
        fixture, bodies, faces = self._inventory()
        tessellation = build_native_tessellation(fixture.component, bodies, faces, source_scope=fixture.source_scope)
        warnings = []
        parse_native_tessellation(fixture.component, tessellation, {"solid": 0, "sheet": 1}, 4,
                                  source_scope=fixture.source_scope, warnings=warnings)
        report = quality_report(warnings)
        view = recorded_warnings(report)
        summaries = [item for item in view if item["code"] == NATIVE_SHEET_DISPLAY_GAP]
        self.assertEqual(sum(item["records_total"] for item in summaries), 4)
        self.assertEqual(sum(item["source_rows_shown"] for item in summaries), 4)
        self.assertEqual({item["object"] for group in summaries for item in group["objects"]},
                         {item["object"] for item in report["diagnostics"] if item["code"] == NATIVE_SHEET_DISPLAY_GAP})
        self.assertEqual(sum(item["code"] == NATIVE_SHEET_DISPLAY_GAP
                             for item in _quality_warnings(report)), 4)
        from tests.v1.test_pipeline_publish import PublishFixture

        transport = PublishFixture(diagnostics=report["diagnostics"])
        self.addCleanup(transport.close)
        certificate = {**transport.certificate, "diagnostics": report["diagnostics"], "checks": report["checks"],
                       "required_checks": [row["id"] for row in report["checks"]]}
        certificate["verification_key"] = digest(
            {key: value for key, value in certificate.items() if key != "verification_key"}
        )
        validate_certificate_structure(certificate)
        for mutation in ("drop", "field", "object"):
            forged = copy.deepcopy(certificate)
            index = next(index for index, item in enumerate(forged["diagnostics"])
                         if item["code"] == NATIVE_SHEET_DISPLAY_GAP)
            if mutation == "drop":
                forged["diagnostics"].pop(index)
                with self.assertRaises(PortalError):
                    _quality_warnings(forged)
                with self.assertRaises(PipelineError):
                    recorded_warnings(forged)
            elif mutation == "field":
                forged["diagnostics"][index]["detail"]["field"] = "unrelated"
            else:
                forged["diagnostics"][index]["object"] = "unrelated"
            # The registered certificate consumer verifies its proof before rendering.
            with self.subTest(mutation=mutation), self.assertRaises(PipelineError):
                validate_certificate_structure(forged)
        value_warnings = native_display_gap_value_warnings(warnings)
        model = {"name": "fixture", "provenance": {"value_warnings": value_warnings},
                 "links": [], "joints": [], "frames": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = generate_urdf(SimpleNamespace(to_dict=lambda: model, name="fixture"), root, root / "urdf")
            xml = path.read_text()
            self.assertEqual(xml.count("WARNING " + NATIVE_SHEET_DISPLAY_GAP), 4)
            self.assertTrue(all(comment in xml for comment in _warning_comments(model)))
        units = {
            "code": "units.si_conversion",
            "message": "Delivered values use SI units after conversion from native CAD units.",
            "objects": [{"object_type": "robot", "object": "fixture", "field": "units",
                         "actual": "SI", "source": "native_derived"}],
        }
        model["provenance"]["value_warnings"].append(units)
        solidworks_urdf._value_warning_gate(Path("/tmp"), {"robot_name": "fixture"}, model,
                                          native_geometry_warnings=warnings)
        for mutation in ("drop", "proof", "object"):
            forged = copy.deepcopy(model)
            if mutation == "drop":
                forged["provenance"]["value_warnings"].pop(0)
            elif mutation == "proof":
                forged["provenance"]["value_warnings"][0]["objects"][0]["zero_triangle_evidence"]["get_area_m2"] = 2.0
            else:
                forged["provenance"]["value_warnings"][0]["objects"][0]["object"] = "unrelated"
            with self.subTest(mutation=mutation), self.assertRaises(PipelineError):
                solidworks_urdf._value_warning_gate(Path("/tmp"), {"robot_name": "fixture"}, forged,
                                                  native_geometry_warnings=warnings)
        qualification = quality_tests.QualificationItemTests()
        root = qualification._root()
        self.addCleanup(qualification.doCleanups)
        (root / "evidence/raw").mkdir(parents=True)
        write_json(root / "evidence/raw/native_capture_facts.json.gz", {"faces": faces})
        definition = qualification._definition()
        checks = qualification._checks(definition)
        gate = next(item for item in checks if item["id"] == "geometry.native_tessellation")
        gate["details"] = {"warnings": warnings}
        item = next(item for item in solidworks_urdf.qualifications(root, definition, checks)
                    if item["fact_id"] == "geometry.native_tessellation")
        self.assertEqual(item["status"], "unqualified")
        self.assertEqual([entry["record"] for entry in item["evidence"]],
                         ["evidence/raw/native_capture_facts.json.gz", "model/robot.json"])
