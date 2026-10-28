"""General saved-state carrier boundary tests independent of Source inference."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from description_pipeline.verification.saved_native_references import rebuild_saved_native_references as rebuild
from description_pipeline.verification.native_discovery import _Failure as Failure


def transform(x=0):
    return [1, 0, 0, 0, 1, 0, 0, 0, 1, x, 0, 0, 1, 0, 0, 0]


def context(document="machine.SLDASM", scope=""):
    return {"document": document, "configuration": "Default", "scope": scope}


def component(name, document=None, parent="", fixed=False):
    return {
        "name2": name,
        "parent_occurrence": parent,
        "document": document or name + ".SLDPRT",
        "configuration": "Default",
        "transform_raw": transform(),
        "suppressed": False,
        "fixed": fixed,
        "fixed_error": None,
        "solver_observation": {
            "context": context(),
            "solving": {
                "method": "IComponent2.Solving",
                "family": "swComponentSolvingOption_e",
                "value": 0,
                "error": None,
            },
        },
    }


def entity(name, index, x, reference_type=4):
    return {
        "reference_name": name,
        "reference_document": name + ".SLDPRT",
        "reference_configuration": "Default",
        "assembly_frame": False,
        "entity_index": index,
        "feature": "face",
        "face_index": None,
        "mate_entity_reference": {
            "source": "mate-entity-params",
            "space": "mate-assembly",
            "reference_type": reference_type,
            "params": [x, 0, 0, 0, 0, 1, 0.01, 0],
            "error": None,
        },
    }


def mate(name, left, right, x, kind="concentric"):
    return {
        "name": name,
        "type": kind,
        "scope": "",
        "configuration": "Default",
        "suppressed": False,
        "error_code": 0,
        "entity_count": 2,
        "entities": [entity(left, 0, x), entity(right, 1, x)],
        "lock_rotation": False,
        "lock_rotation_api": "IConcentricMateFeatureData.LockRotation",
        "native_limit": None,
        "limits": None,
        "is_advanced_mate": None,
        "is_advanced_mate_error": None,
        "is_advanced_mate_api": None,
    }


def fixture():
    raw = {
        "identity": {"main_assembly": "machine.SLDASM"},
        "saved_configuration": "Default",
        "namespace": "dp",
        "files": {name + ".SLDPRT": "0" * 64 for name in ("base", "boom", "tool")},
        "exporter": {
            "state": "absent",
            "document": "machine.SLDASM",
            "configuration": "Default",
        },
        "components": [
            component("base", fixed=True),
            component("boom"),
            component("tool"),
        ],
        "properties": {"document": {}, "components": {}, "mates": {}},
        "mates": [
            mate("hinge_one", "base", "boom", 1),
            mate("hinge_two", "boom", "tool", 2),
        ],
        "datums": [],
        "reference_axes": [],
    }
    for name, x in (("base_link", 0), ("boom", 1), ("tool", 2)):
        raw["datums"].append(
            {
                "name": "c_" + name,
                "owner": "",
                "configuration": "Default",
                "array_raw": transform(x),
                "references": {"context": context()},
            }
        )
        raw["reference_axes"].append(
            {
                "component": "",
                "selector": {
                    "kind": "reference_axis",
                    "component": "",
                    "feature_name": "a_" + name,
                },
                "kind": "reference_axis",
                "coordinate_frame": "assembly",
                "direction_semantics": "undirected_axis_line",
                "axis_point_m": [x, 0, 0],
                "axis_direction": [0, 0, 1],
                "raw_endpoints_m": [x, 0, 0, x, 0, 0.01],
                "used_api": "IFeature.GetSpecificFeature2/IRefAxis.GetRefAxisParams",
            }
        )
    return raw


class SavedNativeTests(unittest.TestCase):
    def run_fixture(self, raw=None, **kwargs):
        return rebuild(fixture() if raw is None else raw, purpose="inspection_preview", **kwargs)

    def test_rebuilds_material_tree_and_preserves_raw(self):
        raw = fixture()
        original = copy.deepcopy(raw)
        result = self.run_fixture(raw)
        self.assertEqual(raw, original)
        self.assertEqual(result["root"], "base_link")
        self.assertEqual(len(result["bodies"]), 3)
        self.assertEqual(len(result["joints"]), 2)
        self.assertTrue(all(j["limit_source"] == "preview_policy" for j in result["joints"]))
        self.assertTrue(all(j["preview_policy"]["author_limits"] is None for j in result["joints"]))
        self.assertTrue(all(j["axis_provenance"]["native_positive_motion"] is False for j in result["joints"]))
        self.assertEqual({j["source_name"] for j in result["joints"]}, {"a_boom", "a_tool"})

    def test_accepts_documented_line_cylinder_circle_vectors_without_circle_inference(
        self,
    ):
        for reference_type in (2, 4, 7):
            raw = fixture()
            for endpoint in raw["mates"][0]["entities"]:
                endpoint["mate_entity_reference"]["reference_type"] = reference_type
            with self.subTest(reference_type=reference_type):
                result = self.run_fixture(raw)
                self.assertEqual(len(result["joints"]), 2)
                self.assertNotIn("circle", json.dumps(result))

    def test_rejects_incomplete_or_conflicting_native_boundaries(self):
        mutations = [
            lambda r: r["exporter"].update(state="unread"),
            lambda r: r["exporter"].update(document="other.SLDASM"),
            lambda r: r["exporter"].update(raw_xml="<declaration/>"),
            lambda r: r["datums"][1].update(configuration="Other"),
            lambda r: r["reference_axes"][1].update(raw_endpoints_m=[1, 0, 0, 1, 0, 0]),
            lambda r: r["mates"][0]["entities"][1]["mate_entity_reference"].update(
                params=[1.01, 0, 0, 0, 0, 1, 0.01, 0]
            ),
            lambda r: r["mates"][0]["entities"][1]["mate_entity_reference"].update(reference_type=True),
            lambda r: r["mates"][0]["entities"][1].update(reference_configuration="Other"),
            lambda r: r["mates"][0].update(entity_count=True),
            lambda r: r["mates"][0].update(lock_rotation=None),
            lambda r: r["mates"][0].update(error_code=True),
            lambda r: r["mates"][0].update(is_advanced_mate_error="unread"),
            lambda r: r["components"][1].update(document="boom.SLDASM"),
            lambda r: r["properties"]["document"].update({"dp.joint.unknown.axis_sign": 1}),
            lambda r: r["properties"]["components"].update({"boom": {"dp.joint.axis_sign": 1}}),
            lambda r: r["properties"]["mates"].update({"hinge_one": {"dp.joint.type": "prismatic"}}),
            lambda r: r["properties"]["mates"].update({"hinge_one": {"dp.joint.axis_sign": 0}}),
            lambda r: r["properties"]["mates"].update({"hinge_one": {"dp.joint.drive_record": "missing.json#drive"}}),
        ]
        for index, mutate in enumerate(mutations):
            raw = fixture()
            mutate(raw)
            with self.subTest(index=index), self.assertRaises(Failure):
                self.run_fixture(raw)
        with self.assertRaises(Failure):
            rebuild(fixture(), purpose="engineering")

    def test_requires_child_solver_context_for_subassembly(self):
        raw = fixture()
        raw["components"][1]["document"] = "boom.SLDASM"
        raw["files"]["boom.SLDASM"] = "1" * 64
        raw["mates"][0]["entities"][1]["reference_document"] = "boom.SLDASM"
        raw["mates"][1]["entities"][0]["reference_document"] = "boom.SLDASM"
        child = component("boom/plate", "plate.SLDPRT", parent="boom")
        child["solver_observation"]["context"] = context("boom.SLDASM", "boom")
        raw["components"].append(child)
        result = self.run_fixture(raw)
        self.assertIn(["boom/plate"], [body["components"] for body in result["bodies"]])
        raw["components"][1]["solver_observation"]["solving"]["value"] = False
        with self.assertRaisesRegex(Failure, "rigid solving"):
            self.run_fixture(raw)
        raw["components"][1]["solver_observation"]["solving"]["value"] = 0
        raw["components"][-1]["solver_observation"]["context"]["scope"] = "wrong"
        with self.assertRaises(Failure):
            self.run_fixture(raw)

    def test_pose_mates_cannot_bridge_nonadjacent_material_bodies(self):
        raw = fixture()
        raw["mates"].append(mate("cross", "base", "tool", 0, "coincident"))
        with self.assertRaisesRegex(Failure, "nonadjacent"):
            self.run_fixture(raw)
        endpoint = raw["mates"][-1]["entities"][0]
        endpoint.update(
            reference_name="machine",
            reference_document="machine.SLDASM",
            assembly_frame=True,
        )
        self.assertEqual(len(self.run_fixture(raw)["joints"]), 2)

    def test_rejects_ambiguous_global_binding_without_auto_number_guess(self):
        raw = fixture()
        for index in (1, 2):
            raw["datums"][index].update(name=f"c_auto_link_00{index}", array_raw=transform(1))
            axis = raw["reference_axes"][index]
            axis["selector"]["feature_name"] = f"a_auto_link_00{index}"
            axis.update(axis_point_m=[1, 0, 0], raw_endpoints_m=[1, 0, 0, 1, 0, 0.01])
        for endpoint in raw["mates"][1]["entities"]:
            endpoint["mate_entity_reference"]["params"][0] = 1
        with self.assertRaisesRegex(Failure, "complete geometric binding"):
            self.run_fixture(raw)

    def test_concentric_graph_rejects_cycles_and_disconnection(self):
        for edges in (
            (("base", "boom"), ("boom", "tool"), ("tool", "base")),
            (("boom", "tool"), ("tool", "sensor"), ("sensor", "boom")),
        ):
            raw = fixture()
            raw["components"].append(component("sensor"))
            raw["files"]["sensor.SLDPRT"] = "2" * 64
            datum, axis = copy.deepcopy(raw["datums"][-1]), copy.deepcopy(raw["reference_axes"][-1])
            datum["name"] = "c_sensor"
            axis["selector"]["feature_name"] = "a_sensor"
            raw["datums"].append(datum)
            raw["reference_axes"].append(axis)
            raw["mates"] = [mate(f"edge_{index}", left, right, 1) for index, (left, right) in enumerate(edges)]
            with self.subTest(edges=edges), self.assertRaisesRegex(Failure, "cycle|disconnected"):
                self.run_fixture(raw)

    def test_native_axis_sign_is_used_without_hardware_claim(self):
        raw = fixture()
        raw["properties"]["mates"]["hinge_one"] = {
            "dp.joint.axis_sign": "-1",
            "dp.joint.type": "revolute",
        }
        joint = next(item for item in self.run_fixture(raw)["joints"] if item["source_name"] == "a_boom")
        self.assertEqual(joint["axis"], [0.0, 0.0, -1.0])
        self.assertFalse(joint["axis_provenance"]["native_positive_motion"])
        self.assertFalse(joint["axis_provenance"]["hardware_qualified"])

    def test_reads_controlled_true_zero_and_drive_from_exact_archived_bytes(self):
        raw = fixture()
        value = {
            "bounds": {"lower": 0, "upper": 0, "effort": 2, "velocity": 3, "note": "author coordinate declaration"},
            "drive": {"effort": 2, "velocity": 3, "metadata": {"source": "author"}},
        }
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder)
            (package / "records").mkdir()
            content = json.dumps(value).encode()
            (package / "records/author.json").write_bytes(content)
            records = [
                {
                    "file": "author.json",
                    "key": key,
                    "package_file": "records/author.json",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "anchor": "joint:boom",
                }
                for key in ("bounds", "drive")
            ]
            raw["properties"]["mates"]["hinge_one"] = {
                "dp.joint.limits_record": "author.json#bounds",
                "dp.joint.drive_record": "author.json#drive",
            }
            result = self.run_fixture(raw, payload={"records": records}, package=package)
            joint = next(item for item in result["joints"] if item["source_name"] == "a_boom")
            self.assertEqual(
                joint["limits"],
                {"lower": 0.0, "upper": 0.0, "effort": 2.0, "velocity": 3.0},
            )
            self.assertEqual(joint["limit_source"], "declared_author")
            self.assertNotIn("preview_policy", joint)
            native_properties = raw["properties"]["mates"]["hinge_one"]
            reference = native_properties.pop("dp.joint.drive_record")
            limits_only = self.run_fixture(raw, payload={"records": records}, package=package)
            limits_joint = next(item for item in limits_only["joints"] if item["source_name"] == "a_boom")
            self.assertEqual(limits_joint["limits"], joint["limits"])
            native_properties["dp.joint.drive_record"] = reference
            value["drive"]["effort"] = 4
            content = json.dumps(value).encode()
            (package / "records/author.json").write_bytes(content)
            for entry in records:
                entry["sha256"] = hashlib.sha256(content).hexdigest()
            with self.assertRaisesRegex(Failure, "records disagree"):
                self.run_fixture(raw, payload={"records": records}, package=package)
            (package / "records/author.json").write_bytes(b"{}")
            with self.assertRaisesRegex(Failure, "bytes changed"):
                self.run_fixture(raw, payload={"records": records}, package=package)

    def test_residual_auto_binding_is_unique_and_independent_of_inventory_order(self):
        raw = fixture()
        raw["datums"][2].update(name="c_auto_link_007", array_raw=transform(1))
        raw["reference_axes"][2]["selector"]["feature_name"] = "a_auto_link_007"
        raw["reference_axes"][2].update(axis_point_m=[1, 0, 0], raw_endpoints_m=[1, 0, 0, 1, 0, 0.01])
        for endpoint in raw["mates"][1]["entities"]:
            endpoint["mate_entity_reference"]["params"][0] = 1
        first = self.run_fixture(raw)
        for key in ("components", "datums", "reference_axes", "mates"):
            raw[key].reverse()
        second = self.run_fixture(raw)
        self.assertEqual(first, second)
        body = next(item for item in first["bodies"] if item["source_name"] == "auto_link_007")
        self.assertEqual(body["components"], ["tool"])

    def test_native_zero_range_is_preserved_in_refusal_without_coordinate_binding(self):
        raw = fixture()
        observed = mate("native_zero", "base", "boom", 1, "limitangle")
        observed.update(is_advanced_mate=True, is_advanced_mate_api="IAngleMateFeatureData.IsAdvancedMate")
        observed["limits"] = {"lower": 0, "upper": 0, "unit": "rad"}
        observed["native_limit"] = {
            "type_index": 6,
            "definition_interface": "IAngleMateFeatureData",
            "definition_interface_iid": "{EF4303D7-B4FD-46E7-AD34-D942C420E096}",
            "nominal_dimension": {"value": 0, "api": "IAngleMateFeatureData.Angle", "unit": "rad"},
            "minimum_variation": {"value": 0, "api": "IMate2.MinimumVariation", "unit": "rad"},
            "maximum_variation": {"value": 0, "api": "IMate2.MaximumVariation", "unit": "rad"},
            "minimum_limit": {"value": 0, "api": "IAngleMateFeatureData.MinimumAngle", "unit": "rad"},
            "maximum_limit": {"value": 0, "api": "IAngleMateFeatureData.MaximumAngle", "unit": "rad"},
        }
        raw["mates"].append(observed)
        raw["properties"]["mates"]["native_zero"] = {"dp.joint.axis_sign": 1}
        with self.assertRaisesRegex(Failure, "signed coordinate binding") as caught:
            self.run_fixture(raw)
        self.assertEqual(caught.exception.detail["native_range"], [0.0, 0.0])
        self.assertEqual(raw["mates"][-1]["limits"], {"lower": 0, "upper": 0, "unit": "rad"})

    def test_additional_native_interface_frames_bind_the_exact_saved_owner(self):
        raw = fixture()
        raw["datums"].append(
            {"name": "TCP_probe", "owner": "tool", "configuration": "Default", "array_raw": transform(0.1)}
        )
        frame = self.run_fixture(raw)["frames"][0]
        self.assertEqual(frame["parent"], "tool_link")
        self.assertEqual(frame["source_name"], "TCP_probe")
        raw["datums"][-1]["configuration"] = "Other"
        with self.assertRaisesRegex(Failure, "frame configuration"):
            self.run_fixture(raw)

    def test_arbitrary_datum_rotation_and_origin_along_axis_are_valid(self):
        raw = fixture()
        raw["datums"][1]["array_raw"] = [0, 0, -1, 0, 1, 0, 1, 0, 0, 1, 0, 2, 1, 0, 0, 0]
        result = self.run_fixture(raw)
        joint = next(item for item in result["joints"] if item["source_name"] == "a_boom")
        self.assertEqual(joint["axis"], [-1.0, 0.0, 0.0])
        raw["datums"][0]["array_raw"][:9] = [0, 0, -1, 0, 1, 0, 1, 0, 0]
        joint = next(item for item in self.run_fixture(raw)["joints"] if item["source_name"] == "a_boom")
        self.assertEqual(joint["axis_provenance"]["native_direction"], [-1.0, 0.0, 0.0])
        self.assertEqual(joint["axis_provenance"]["parent_axis"], [-1.0, 0.0, 0.0])
        self.assertEqual(joint["axis_provenance"]["original_assembly_direction"], [0.0, 0.0, 1.0])

    def test_parts_do_not_claim_rigidity_from_their_solving_value(self):
        raw = fixture()
        for part in raw["components"]:
            part["solver_observation"]["solving"]["value"] = -1
        self.assertEqual(len(self.run_fixture(raw)["bodies"]), 3)

    def test_geometric_binding_uses_the_strict_shared_tolerances(self):
        for mutation in ("offset", "angle"):
            raw = fixture()
            for endpoint in raw["mates"][0]["entities"]:
                params = endpoint["mate_entity_reference"]["params"]
                if mutation == "offset":
                    params[0] += 2e-7
                else:
                    params[3] = 2e-4
            with self.subTest(mutation=mutation), self.assertRaisesRegex(Failure, "geometric witness"):
                self.run_fixture(raw)

    def test_prefix_similarity_cannot_resolve_a_geometric_ambiguity(self):
        raw = fixture()
        raw["datums"][2].update(name="c_auto_link_007", array_raw=transform(1))
        raw["reference_axes"][2]["selector"]["feature_name"] = "a_auto_link_007"
        raw["reference_axes"][2].update(axis_point_m=[1, 0, 0], raw_endpoints_m=[1, 0, 0, 1, 0, 0.01])
        raw["components"][1].update(name2="boom_extension", document="boom_extension.SLDPRT")
        raw["files"]["boom_extension.SLDPRT"] = "3" * 64
        for native_mate in raw["mates"]:
            for endpoint in native_mate["entities"]:
                endpoint["mate_entity_reference"]["params"][0] = 1
                if endpoint["reference_name"] == "boom":
                    endpoint.update(reference_name="boom_extension", reference_document="boom_extension.SLDPRT")
        with self.assertRaisesRegex(Failure, "complete geometric binding"):
            self.run_fixture(raw)
        raw["components"][1]["document"] = "boom_20401231.SLDPRT"
        raw["files"]["boom_20401231.SLDPRT"] = "4" * 64
        for native_mate in raw["mates"]:
            for endpoint in native_mate["entities"]:
                if endpoint["reference_name"] == "boom_extension":
                    endpoint["reference_document"] = "boom_20401231.SLDPRT"
        self.assertEqual(len(self.run_fixture(raw)["bodies"]), 3)

    def test_nested_solver_context_binds_each_immediate_parent(self):
        raw = fixture()
        raw["components"][1]["document"] = "boom.SLDASM"
        raw["files"]["boom.SLDASM"] = "1" * 64
        raw["mates"][0]["entities"][1]["reference_document"] = "boom.SLDASM"
        raw["mates"][1]["entities"][0]["reference_document"] = "boom.SLDASM"
        inner = component("boom/inner", "inner.SLDASM", "boom")
        inner["solver_observation"]["context"] = context("boom.SLDASM", "boom")
        leaf = component("boom/inner/plate", "plate.SLDPRT", "boom/inner")
        leaf["solver_observation"]["context"] = context("inner.SLDASM", "boom/inner")
        raw["components"].extend((inner, leaf))
        self.assertEqual(len(self.run_fixture(raw)["bodies"]), 3)
        leaf["solver_observation"]["context"] = context("boom.SLDASM", "boom")
        with self.assertRaisesRegex(Failure, "immediate parent context"):
            self.run_fixture(raw)



if __name__ == "__main__":
    unittest.main()
