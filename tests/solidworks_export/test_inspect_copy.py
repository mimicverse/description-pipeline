"""``inspect_copy`` must key *instances*, never documents.

``IComponent2::GetPathName`` is the document path and repeats for every placement
of the same part, so these doubles make it intentionally useless as a key: the
instance identity has to come from the ``Name2`` assembly path (stitched with the
parent context when a build reports the leaf alone).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from tools.solidworks_export.native_swapi import SolidWorksBackend


class InspectCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root_dir = Path(self.tmp.name) / "source"
        self.root_dir.mkdir(parents=True)

        def doc(name: str, kind: int) -> NS:
            path = self.root_dir / name
            path.write_bytes(name.encode())
            return NS(
                GetType=kind,
                GetSaveFlag=False,
                IsOpenedReadOnly=True,
                GetPathName=str(path),
                ConfigurationManager=NS(ActiveConfiguration=NS(Name="Default")),
            )

        self.assembly_doc = doc("robot.SLDASM", 2)
        self.sub_doc = doc("module.SLDASM", 2)
        self.part_doc = doc("part.SLDPRT", 1)
        self.bracket_doc = doc("bracket.SLDPRT", 1)

        self.backend = SolidWorksBackend()
        self.backend.open_document = lambda path: {"opened": Path(path).name, "path": str(path)}  # type: ignore[method-assign]
        self.backend._document_by_path = lambda path: self.assembly_doc  # type: ignore[method-assign]

    def comp(
        self,
        name: str,
        document: NS | None,
        children: list[NS] | None = None,
        *,
        referenced: str = "Default",
        suppressed: bool = False,
    ) -> NS:
        return NS(
            Name2=name,
            IsSuppressed=suppressed,
            GetChildren=list(children or []),
            GetModelDoc2=document,
            # the document path, identical for every placement of the same part;
            # a placement SolidWorks cannot resolve has none at all
            GetPathName=document.GetPathName if document is not None else "",
            ReferencedConfiguration=referenced,
        )

    def install_root(self, children: list[NS]) -> None:
        self.assembly_doc.ConfigurationManager.ActiveConfiguration.GetRootComponent3 = lambda resolve: NS(
            GetChildren=children
        )

    def rows(self, payload: dict) -> dict[str, dict]:
        return {entry["instance"]: entry for entry in payload["instances"]}

    def test_one_part_placed_twice_stays_two_instances(self) -> None:
        self.install_root([self.comp("part-1", self.part_doc), self.comp("part-2", self.part_doc)])

        report = self.backend.inspect_copy(self.assembly_doc.GetPathName)
        rows = self.rows(report)

        self.assertEqual(sorted(rows), ["part-1", "part-2"])
        self.assertEqual(rows["part-1"]["document"], str(self.part_doc.GetPathName))
        self.assertEqual(rows["part-2"]["document"], str(self.part_doc.GetPathName))
        self.assertEqual(report["components"], 2)
        self.assertEqual(report["unresolved"], [])
        self.assertEqual(report["configuration"], "Default")

    def test_repeated_subassembly_keeps_distinct_instance_paths(self) -> None:
        module1 = self.comp("module-1", self.sub_doc, [self.comp("part-1", self.part_doc)])
        module2 = self.comp("module-2", self.sub_doc, [self.comp("part-1", self.part_doc)])
        self.install_root([module1, module2])

        report = self.backend.inspect_copy(self.assembly_doc.GetPathName)
        rows = self.rows(report)

        # the child reports the leaf alone on both sides and is stitched with its
        # parent placement, so the two children cannot collide
        self.assertEqual(sorted(rows), ["module-1", "module-1/part-1", "module-2", "module-2/part-1"])
        self.assertEqual(rows["module-1/part-1"]["document"], str(self.part_doc.GetPathName))
        self.assertEqual(rows["module-2/part-1"]["document"], str(self.part_doc.GetPathName))
        self.assertEqual(rows["module-1/part-1"]["depth"], 1)
        self.assertEqual(rows["module-2"]["depth"], 0)

    def test_hierarchical_name_is_not_prefixed_twice(self) -> None:
        module = self.comp("module-1", self.sub_doc, [self.comp("module-1/part-1", self.part_doc)])
        self.install_root([module])

        rows = self.rows(self.backend.inspect_copy(self.assembly_doc.GetPathName))

        self.assertEqual(sorted(rows), ["module-1", "module-1/part-1"])

    def test_referenced_configuration_and_suppression_are_reported(self) -> None:
        suppressed = self.comp("bracket-1", self.bracket_doc, [self.comp("part-1", self.part_doc)], suppressed=True)
        self.install_root([self.comp("part-1", self.part_doc, referenced="Machining"), suppressed])

        report = self.backend.inspect_copy(self.assembly_doc.GetPathName)
        rows = self.rows(report)

        self.assertEqual(rows["part-1"]["configuration"], "Machining")
        self.assertTrue(rows["bracket-1"]["suppressed"])
        self.assertEqual(report["suppressed"], ["bracket-1"])
        # a suppressed placement loads no model and contributes no children
        self.assertNotIn("bracket-1/part-1", rows)
        self.assertEqual(report["unresolved"], [])

    def test_an_instance_without_a_model_is_unresolved(self) -> None:
        self.install_root([self.comp("part-1", None)])

        report = self.backend.inspect_copy(self.assembly_doc.GetPathName)

        self.assertEqual(report["unresolved"], ["part-1"])
        self.assertEqual(report["instances"][0]["document"], None)


if __name__ == "__main__":
    unittest.main()
