"""COM 接触层的纯逻辑：质量/质心/惯量装配与材料门禁（无需 Windows）。"""

import contextlib
import math
import sys
import types
import unittest
from unittest import mock

from tools.solidworks_export import native_swapi
from tools.solidworks_export.errors import CadError


class _FakeWin32:
    """只实现本测试用到的 VARIANT 构造。"""

    def __init__(self) -> None:
        self.variants: list[tuple] = []

    def VARIANT(self, vartype, values):
        self.variants.append((vartype, values))
        return {"variant": values}


class _Member:
    """按名字回答属性的最小 COM 对象桩。"""

    def __init__(self, **members) -> None:
        self._members = members

    def __getattr__(self, name):
        if name not in self._members:
            raise AttributeError(name)
        return self._members[name]


class _MassProperty(_Member):
    def __init__(self, **kwargs) -> None:
        self.recalculated = 0
        super().__init__(**kwargs)

    def Recalculate(self) -> None:
        self.recalculated += 1


def fake_pythoncom() -> types.ModuleType:
    module = types.ModuleType("pythoncom")
    module.VT_ARRAY = 0x2000  # type: ignore[attr-defined]
    module.VT_DISPATCH = 9  # type: ignore[attr-defined]
    return module


def part_document(*, overrides=None, mass=0.25, com=(-0.01, 0.02, 0.03), values=None, bodies=None):
    """构造一个"叶子零件"文档 + MassProperty 桩。"""

    if bodies is None:
        bodies = (object(),)
    if values is None:
        values = (1e-4, 2e-6, -3e-6, 2e-6, 2e-4, 4e-6, -3e-6, 4e-6, 3e-4)
    flags = {
        "OverrideMass": False,
        "OverrideCenterOfMass": False,
        "OverrideMomentsOfInertia": False,
    }
    flags.update(overrides or {})
    mp = _MassProperty(
        Mass=mass,
        CenterOfMass=com,
        Volume=1e-4,
        Density=2500.0,
        GetMomentOfInertia=lambda index: values,
        GetOverrideOptions=lambda: _Member(**flags),
    )
    doc = _Member(
        GetType=lambda: 1,
        GetBodies2=lambda kind, visible: bodies,
        GetTitle=lambda: "part.SLDPRT",
        GetPathName=lambda: "D:/models/part.SLDPRT",
        Extension=_Member(CreateMassProperty2=mp),
        ConfigurationManager=_Member(ActiveConfiguration=_Member(Name="Default")),
    )
    return doc, mp


MATERIALS = {"schema_version": "swbridge.material-assignment/v1", "configuration": "Default"}


class MassPropertyTests(unittest.TestCase):
    def setUp(self):
        self.win32 = _FakeWin32()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(native_swapi, "_win32", return_value=self.win32))
        stack.enter_context(mock.patch.dict(sys.modules, {"pythoncom": fake_pythoncom()}))
        stack.enter_context(
            mock.patch.object(native_swapi, "_material_assignments_document", return_value=dict(MATERIALS))
        )
        self.backend = native_swapi.SolidWorksBackend()

    def test_reads_mass_com_and_full_inertia_with_evidence(self):
        doc, mp = part_document()
        result = self.backend._mass_properties_document(doc)
        self.assertAlmostEqual(result["mass"], 0.25)
        self.assertEqual(result["com"], (-0.01, 0.02, 0.03))
        self.assertEqual(result["inertia"][0], (1e-4, 2e-6, -3e-6))
        self.assertEqual(result["inertia"][2], (-3e-6, 4e-6, 3e-4))
        reference = result["reference"]
        self.assertTrue(reference["use_system_units"])
        self.assertEqual(reference["product_convention"], "solidworks_positive")
        self.assertEqual(reference["body_count"], 1)
        self.assertEqual(reference["configuration"], "Default")
        self.assertEqual(mp.recalculated, 1)
        self.assertEqual(self.win32.variants[0][1], (doc.GetBodies2(0, False)[0],))

    def test_optional_material_failure_is_recorded_not_hidden(self):
        with mock.patch.object(
            native_swapi,
            "_material_assignments_document",
            side_effect=CadError("cad_material_provenance_missing", "no material"),
        ):
            doc, _mp = part_document()
            result = self.backend._mass_properties_document(doc, require_material=False)
        assignment = result["reference"]["material_assignment"]
        self.assertEqual(assignment["unverified_reason"], "cad_material_provenance_missing")
        self.assertEqual(assignment["configuration"], "Default")

    def test_required_material_failure_propagates(self):
        with mock.patch.object(
            native_swapi,
            "_material_assignments_document",
            side_effect=CadError("cad_material_provenance_missing", "no material"),
        ):
            doc, _mp = part_document()
            with self.assertRaises(CadError):
                self.backend._mass_properties_document(doc)

    def test_non_part_and_empty_documents_are_rejected(self):
        assembly_doc = _Member(GetType=lambda: 2)
        with self.assertRaises(CadError) as not_part:
            self.backend._mass_properties_document(assembly_doc)
        self.assertEqual(not_part.exception.code, "cad_not_part")
        empty_doc, _mp = part_document(bodies=())
        with self.assertRaises(CadError) as empty:
            self.backend._mass_properties_document(empty_doc)
        self.assertEqual(empty.exception.code, "cad_empty_model")

    def test_manual_overrides_are_refused(self):
        doc, _mp = part_document(overrides={"OverrideMass": True})
        with self.assertRaises(CadError) as caught:
            self.backend._mass_properties_document(doc)
        self.assertEqual(caught.exception.code, "cad_mass_override")

    def test_invalid_and_nonfinite_mass_are_rejected(self):
        for mass, com in ((0.0, (0.0, 0.0, 0.0)), (0.25, (0.0, float("nan"), 0.0))):
            with self.subTest(mass=mass, com=com):
                doc, _mp = part_document(mass=mass, com=com)
                with self.assertRaises(CadError) as caught:
                    self.backend._mass_properties_document(doc)
                self.assertEqual(caught.exception.code, "cad_mass_property_invalid")

    def test_wrong_inertia_length_is_reported(self):
        for values in ((1.0, 2.0, 3.0), (1.0, 2.0, 3.0, 4.0)):
            with self.subTest(length=len(values)):
                doc, _mp = part_document(values=values)
                with self.assertRaises(CadError) as caught:
                    self.backend._mass_properties_document(doc)
                self.assertEqual(caught.exception.code, "cad_mass_property_inertia_unsupported")


class InertiaParsingTests(unittest.TestCase):
    def test_full_nine_is_symmetric_checked(self):
        values = (1e-4, 2e-6, -3e-6, 2e-6, 2e-4, 4e-6, -3e-6, 4e-6, 3e-4)
        tensor, label = native_swapi._inertia_from_raw(values, "part")
        self.assertEqual(label, "full9")
        self.assertEqual(tensor[1][0], tensor[0][1])
        with self.assertRaises(CadError) as caught:
            native_swapi._inertia_from_raw((1e-4, 2e-6, 0, 9e-6, 2e-4, 0, 0, 0, 3e-4), "part")
        self.assertEqual(caught.exception.code, "cad_mass_property_inertia_asymmetric")

    def test_legacy_twelve_values_are_validated_with_parallel_axis(self):
        mass, com = 0.25, (0.01, 0.02, 0.03)
        shift = native_swapi._parallel_axis_terms(mass, com)
        com_first = tuple(a + b for a, b in zip(shift, (1e-5,) * 6, strict=True))
        com_second = (1e-5,) * 6
        tensor, label = native_swapi._inertia_from_raw(com_first + com_second, "part", mass=mass, com=com)
        self.assertTrue(label.startswith("six6:validated:"), label)
        self.assertAlmostEqual(tensor[0][0], 1e-5)
        with self.assertRaises(CadError) as caught:
            native_swapi._inertia_from_raw((1.0,) * 12, "part", mass=mass, com=com)
        self.assertEqual(caught.exception.code, "cad_mass_property_inertia_ambiguous")

    def test_nonfinite_and_short_inputs(self):
        with self.assertRaises(CadError) as caught:
            native_swapi._inertia_from_raw((math.inf, 0, 0, 0, 0, 0, 0, 0, 0), "part")
        self.assertEqual(caught.exception.code, "cad_mass_property_inertia_nonfinite")
        with self.assertRaises(CadError) as principal:
            native_swapi._inertia_from_raw((1.0, 2.0, 3.0), "part")
        self.assertEqual(principal.exception.code, "cad_mass_property_principal_only")


class PathHelperTests(unittest.TestCase):
    def test_document_path_normalisation(self):
        self.assertEqual(native_swapi.normalize_document_path("D:/models/A.SLDASM"), "d:\\models\\a.sldasm")
        self.assertTrue(native_swapi.document_paths_match("D:/models/a.SLDASM", "d:\\models\\a.sldasm"))
        self.assertTrue(native_swapi.document_paths_match("D:/models/a.SLDASM", "a.sldasm"))
        self.assertFalse(native_swapi.document_paths_match("", "a.sldasm"))

    def test_member_and_dynamic_helpers(self):
        holder = _Member(child=lambda name: 7 if name == "value" else None)
        self.assertEqual(native_swapi._member(holder, "child", "value"), 7)
        not_callable = _Member(child=5)
        with self.assertRaises(CadError) as refused:
            native_swapi._member(not_callable, "child", "value")
        self.assertEqual(refused.exception.code, "cad_member_not_callable")
        with self.assertRaises(CadError) as caught:
            native_swapi._member(holder, "missing")
        self.assertEqual(caught.exception.code, "cad_member_missing")
        self.assertEqual(native_swapi._as_list(None), [])
        self.assertEqual(native_swapi._as_list(5), [5])
        self.assertEqual(native_swapi._as_list((1, 2)), [1, 2])


if __name__ == "__main__":
    unittest.main()
