"""No CAD process required; real COM behavior is calibrated separately on Windows."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.solidworks_export import native_swapi as native
from tools.solidworks_export.errors import CadError


class MaterialAssignmentTests(unittest.TestCase):
    def read(self, part=("Steel", "materials.sldmat"), bodies=(("", ""),), config="Default"):
        doc = SimpleNamespace(
            ConfigurationManager=SimpleNamespace(ActiveConfiguration=SimpleNamespace(Name=config)),
            GetConfigurationNames=lambda: [config],
            material=part,
        )
        solids = [SimpleNamespace(Name=f"body-{i}", material=m) for i, m in enumerate(bodies)]

        def read(obj, method, query):
            self.assertEqual(query, "" if config == "Default" else config)
            self.assertEqual(method, "GetMaterialPropertyName2" if obj is doc else "GetMaterialPropertyName")
            return dict(zip(("name", "database"), obj.material, strict=True))

        with patch.object(native, "_read_material", side_effect=read):
            return native._material_assignments_document(doc, solids)

    def test_part_material_covers_bodies(self):
        evidence = self.read(bodies=(("", ""), ("", "")))
        self.assertEqual(evidence["body_count"], 2)
        self.assertEqual([r["effective_source"] for r in evidence["bodies"]], ["part", "part"])

    def test_body_material_takes_precedence(self):
        evidence = self.read(bodies=(("Aluminum", "custom.sldmat"),))
        self.assertEqual(evidence["bodies"][0]["effective_source"], "body")

    def test_body_only_materials_are_valid(self):
        self.read(part=("", ""), bodies=(("Steel", "a"), ("Plastic", "b")))

    def test_unassigned_import_is_rejected(self):
        with self.assertRaises(CadError) as error:
            self.read(part=("", ""))
        self.assertEqual(error.exception.code, "cad_material_provenance_missing")

    def test_one_unassigned_body_is_not_hidden_by_others(self):
        with self.assertRaises(CadError) as error:
            self.read(part=("", ""), bodies=(("Steel", "a"), ("", "")))
        detail = error.exception.detail
        assert isinstance(detail, dict)
        self.assertEqual(detail["missing_body_indices"], [1])

    def test_missing_database_rejected(self):
        with self.assertRaises(CadError):
            self.read(part=("Steel", ""))

    def test_nondefault_configuration_is_queried_explicitly(self):
        self.assertEqual(self.read(config="Alternate")["query_configuration"], "Alternate")

    def test_api_errors_never_fall_back_to_density(self):
        with (
            patch.object(native, "_member", side_effect=RuntimeError("COM failure")),
            self.assertRaises(CadError) as error,
        ):
            native._material_assignments_document(object(), [object()])
        self.assertEqual(error.exception.code, "cad_material_read_failed")

    def test_empty_solids_rejected(self):
        with self.assertRaises(CadError):
            self.read(bodies=())
