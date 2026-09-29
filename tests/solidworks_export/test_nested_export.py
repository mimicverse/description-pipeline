"""Regressions found while exercising a real nested SolidWorks assembly."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from tools.solidworks_export.config import config_from_dict
from tools.solidworks_export.errors import CadError
from tools.solidworks_export.exporter import run_export
from tools.solidworks_export.native_swapi import SolidWorksBackend

from .helpers import make_config_dict
from .test_frame_consistency import RotatedAssemblyBackend, _first_vertex, _write_marked_triangle


class NestedExportTests(unittest.TestCase):
    def test_component_names_are_data_not_filesystem_paths(self):
        for name in ("module-1/ArmA-1", "../escape", r"C:\escape", "module/同名-1"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                backend = RotatedAssemblyBackend()
                component = backend.components.pop("ArmA-1")
                component.name = name
                backend.components[name] = component
                backend.mass_properties[name] = backend.mass_properties.pop("ArmA-1")
                paths: list = []

                def export(component, dest_path, progress=None, paths=paths):
                    path = Path(dest_path)
                    # Assert BEFORE opening, so the test cannot escape scratch.
                    self.assertEqual(path.parent.name, "parts-scratch")
                    self.assertNotIn("..", path.parts)
                    self.assertNotIn("\\", path.name)
                    self.assertNotIn(":", path.name)
                    self.assertNotIn(dest_path, paths)
                    paths.append(dest_path)
                    _write_marked_triangle(dest_path, (0.0, 0.0, 0.0))
                    return {"triangles": 1, "used_api": "fixture"}

                backend.export_component_mesh = export  # type: ignore[method-assign]
                data = make_config_dict()
                data["links"][1]["components"] = [name]
                run_export(backend, config_from_dict(data), os.path.join(tmp, "out"), "fixture.SLDASM")
                self.assertEqual(len(paths), 2)

    def test_no_merge_still_transforms_mesh_to_link_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = make_config_dict()
            data["mesh"]["merge"] = "none"
            data["links"][1]["components"] = ["ArmA-1"]
            run_export(RotatedAssemblyBackend(), config_from_dict(data), os.path.join(tmp, "out"), "fixture.SLDASM")
            actual = _first_vertex(os.path.join(tmp, "out", "meshes", "arm_link.STL"))
            for got, want in zip(actual, (0.1, 0.8, -0.2), strict=True):
                self.assertAlmostEqual(got, want, places=6)


class NestedSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        def doc(name, kind):
            path = Path(self.tmp.name) / name
            path.write_bytes(name.encode())
            return NS(
                GetType=kind,
                GetSaveFlag=False,
                IsOpenedReadOnly=True,
                GetPathName=str(path),
                ConfigurationManager=NS(ActiveConfiguration=NS(Name="Default")),
            )

        self.root = doc("robot.SLDASM", 2)
        self.sub = doc("module.SLDASM", 2)
        self.part = doc("part.SLDPRT", 1)

        def comp(name, document, children):
            return NS(
                Name2=name,
                IsSuppressed=False,
                GetChildren=children,
                GetModelDoc2=document,
                GetPathName=document.GetPathName,
                ReferencedConfiguration="Default",
                IsFixed=False,
                GetTotalTransform=lambda presentation: NS(ArrayData=(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)),
            )

        self.leaf = comp("module-1/part-1", self.part, [])
        self.branch = comp("module-1", self.sub, [self.leaf])
        self.root.ConfigurationManager.ActiveConfiguration.GetRootComponent3 = lambda resolve: NS(
            GetChildren=[self.branch]
        )
        self.backend = SolidWorksBackend()
        self.backend._document_by_path = lambda path: self.root  # type: ignore[method-assign]
        self.backend._mass_properties_document = lambda doc, require_material=True: {  # type: ignore[method-assign]
            "mass": 1,
            "com": (0, 0, 0),
            "inertia": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
            "reference": {"used_api": "fixture"},
        }

    def collect(self):
        return self.backend.collect_scene(self.root.GetPathName, [])

    def test_intermediate_assembly_is_in_native_source_closure(self):
        self.collect()
        self.assertEqual(
            set(self.backend.verify_sources_unchanged()),
            {self.root.GetPathName, self.sub.GetPathName, self.part.GetPathName},
        )

    def test_save_flag_of_an_intermediate_assembly_is_recorded_not_refused(self):
        # SolidWorks' save flag is evidence, not a gate: it is set by many operations
        # (and for documents created by an older release), while the capture reads the
        # bytes on disk in a session of its own.
        self.sub.GetSaveFlag = True

        self.collect()

        self.assertEqual(len(self.backend.save_flag_documents()), 1)
        self.assertTrue(self.backend.save_flag_documents()[0].endswith("module.sldasm"))

    def test_intermediate_configuration_mismatch_blocks_collection(self):
        self.branch.ReferencedConfiguration = "Other"
        with self.assertRaises(CadError) as error:
            self.collect()
        self.assertEqual(error.exception.code, "cad_configuration_mismatch")

    def test_intermediate_save_flag_does_not_break_the_final_binding(self):
        self.collect()
        recorded = self.backend.save_flag_documents()

        self.sub.GetSaveFlag = True
        self.backend.verify_sources_unchanged()

        # the flag is read when the document is walked, and a later flip is not a change
        self.assertEqual(self.backend.save_flag_documents(), recorded)

    def test_synchronized_configuration_change_still_blocks_binding(self):
        self.collect()
        self.sub.ConfigurationManager.ActiveConfiguration.Name = "Other"
        self.branch.ReferencedConfiguration = "Other"
        with self.assertRaises(CadError):
            self.backend.verify_sources_unchanged()

    def test_intermediate_disk_change_blocks_final_binding(self):
        self.collect()
        Path(self.sub.GetPathName).write_bytes(b"changed assembly")
        with self.assertRaises(CadError):
            self.backend.verify_sources_unchanged()
