"""Portable regressions for the raw coordinate-system reference observations."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from description_pipeline.sources.solidworks.native import SolidWorksBackend

from tests.sources.test_solidworks_native_reader import (
    SW_IDENTITY,
    _App,
    _Component,
    _Doc,
    _Feature,
    _Session,
    _com_stubs,
    _raiser,
    _write,
)


class _RefEntity:
    """IEntity-like reference whose GetComponent may be a value or a callable."""

    def __init__(self, component=None):
        self._component = component

    def GetComponent(self):
        return self._component() if callable(self._component) else self._component


class _CoordinateSystemDefinition:
    """ICoordinateSystemFeatureData-like definition for the reference reads."""

    def __init__(
        self,
        *,
        origin=None,
        x=(),
        y=(),
        z=(),
        origin_type=0,
        x_types=(),
        y_types=(),
        z_types=(),
        x_flipped=False,
        y_flipped=False,
        z_flipped=False,
    ):
        self.OriginEntity = origin
        self.XAxisEntities = list(x)
        self.YAxisEntities = list(y)
        self.ZAxisEntities = list(z)
        self.XFlipped = x_flipped
        self.YFlipped = y_flipped
        self.ZFlipped = z_flipped
        self._origin_type = origin_type
        self._types = {
            "XAxisEntities": list(x_types),
            "YAxisEntities": list(y_types),
            "ZAxisEntities": list(z_types),
        }

    def GetOriginEntityType(self):
        return self._origin_type

    def GetXAxisEntitiesTypes(self):
        return list(self._types["XAxisEntities"])

    def GetYAxisEntitiesTypes(self):
        return list(self._types["YAxisEntities"])

    def GetZAxisEntitiesTypes(self):
        return list(self._types["ZAxisEntities"])


def _read(root: Path, app) -> dict:
    backend = SolidWorksBackend(session_factory=lambda: _Session(app))
    with _com_stubs():
        return backend.discover_native(root, {})


class CoordinateSystemReferenceTests(unittest.TestCase):
    def test_cross_component_reference_facts_stay_per_entity(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _write(root, "base.SLDPRT")
            arm = _write(root, "arm.SLDPRT")
            anchor = _Component("base-1", base)
            other = _Component("arm-2", arm)
            definition = _CoordinateSystemDefinition(
                origin=_RefEntity(anchor),
                x=[_RefEntity(anchor), _RefEntity(other)],
                y=[_RefEntity(None)],
                origin_type=3,
                x_types=[2, 5],
            )
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(
                assembly,
                coordinate_systems={"CS_mix": SW_IDENTITY},
                first_feature=_Feature("CS_mix", "CoordSys", definition=definition),
            )

            record = _read(root, _App({assembly: doc}))

            datum = record["datums"][0]
            self.assertEqual((datum["name"], datum["owner"]), ("CS_mix", ""))
            refs = datum["references"]
            self.assertEqual(refs["context"], {"document": "robot.SLDASM", "configuration": "Default", "scope": ""})
            self.assertEqual(refs["origin"]["type_value"], 3)
            self.assertEqual(refs["origin"]["entity"]["owner_component"], "base-1")
            self.assertEqual(refs["origin"]["entity"]["owner_document"], "base.SLDPRT")
            self.assertEqual(
                [item.get("owner_component") for item in refs["x_axis"]["entities"]],
                ["base-1", "arm-2"],
            )
            self.assertEqual(refs["x_axis"]["types"], [2, 5])
            self.assertIs(refs["x_axis"]["flipped"], False)
            self.assertIsNone(refs["y_axis"]["entities"][0]["owner_component"])
            self.assertIsNone(refs["y_axis"]["entities"][0]["owner_document"])

    def test_unreadable_definition_is_explicit_and_transform_unchanged(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            doc = _Doc(
                assembly,
                coordinate_systems={"CS_tip": SW_IDENTITY},
                first_feature=_Feature(
                    "CS_tip", "CoordSys", definition=_raiser(RuntimeError("definition unavailable"))
                ),
            )

            record = _read(root, _App({assembly: doc}))

            datum = record["datums"][0]
            matrix = datum["array"]
            self.assertEqual([matrix[index] for index in (0, 5, 10, 15)], [1.0, 1.0, 1.0, 1.0])
            self.assertEqual(
                [value for index, value in enumerate(matrix) if index not in (0, 5, 10, 15)],
                [0.0] * 12,
            )
            self.assertIn("definition unavailable", datum["references"]["definition_error"])

    def test_unreadable_owner_is_explicit_per_entity(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            assembly = _write(root, "robot.SLDASM")
            definition = _CoordinateSystemDefinition(origin=_RefEntity(_raiser(RuntimeError("owner unavailable"))))
            doc = _Doc(
                assembly,
                coordinate_systems={"CS_tip": SW_IDENTITY},
                first_feature=_Feature("CS_tip", "CoordSys", definition=definition),
            )

            record = _read(root, _App({assembly: doc}))

            origin = record["datums"][0]["references"]["origin"]["entity"]
            self.assertTrue(origin["unreadable"])
            self.assertIn("owner unavailable", origin["owner_error"])


if __name__ == "__main__":
    unittest.main()
