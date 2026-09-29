import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from tools.solidworks_export.com_executor import ComExecutor
from tools.solidworks_export.errors import CadError
from tools.solidworks_export.native_swapi import (
    SolidWorksBackend,
    _inertia_from_raw,
    _member,
    _parallel_axis_terms,
    normalize_document_path,
    transform_from_solidworks,
)


class AcceptanceRegressions(unittest.TestCase):
    def test_live_member_flags_method_before_access(self):
        class Dispatch:
            flagged = False

            def _FlagAsMethod(self, name):
                self.flagged = True

            @property
            def Method(self):
                if not self.flagged:
                    raise AssertionError("eager zero-argument invocation")
                return lambda argument: argument

        self.assertEqual(_member(Dispatch(), "Method", 4), 4)

    def test_callable_com_interface_is_not_invoked(self):
        class ComInterface:
            _oleobj_ = object()

            def __call__(self):
                raise AssertionError("DISPID_VALUE must not be invoked")

        interface = ComInterface()
        with patch("tools.solidworks_export.native_swapi._dynamic", lambda value: value):
            self.assertIs(_member(SimpleNamespace(ActiveDoc=interface), "ActiveDoc"), interface)
        self.assertEqual(_member(SimpleNamespace(GetPathName="saved.SLDASM"), "GetPathName"), "saved.SLDASM")

    def test_configuration_switch_still_blocks_the_source_binding(self):
        backend = SolidWorksBackend()
        backend._source_configuration = "Default"
        config = SimpleNamespace(Name="Default")
        doc = SimpleNamespace(
            GetPathName="robot.SLDASM",
            GetSaveFlag=False,
            ConfigurationManager=SimpleNamespace(ActiveConfiguration=config),
        )
        backend._doc = doc
        config.Name = "Different"
        with self.assertRaisesRegex(CadError, "changed in memory") as error:
            backend.verify_sources_unchanged()
        assert isinstance(error.exception.detail, dict)
        self.assertEqual(error.exception.detail["after"], {"configuration": "Different"})

    def test_save_flag_is_recorded_and_does_not_block_the_source_binding(self):
        # `GetSaveFlag` says SolidWorks would prompt to save; it is not evidence about
        # the operator's session, and the capture names the bytes on disk instead.
        backend = SolidWorksBackend()
        backend._source_configuration = "Default"
        config = SimpleNamespace(Name="Default")
        doc = SimpleNamespace(
            GetPathName="robot.SLDASM",
            GetSaveFlag=True,
            ConfigurationManager=SimpleNamespace(ActiveConfiguration=config),
        )
        backend._doc = doc
        backend._record_save_flag(doc, "robot.SLDASM")

        backend.verify_sources_unchanged()

        self.assertEqual(backend.save_flag_documents(), [normalize_document_path("robot.SLDASM")])

    def test_live_full9_finite_symmetric_required(self):
        for values in ([1, 2, 3], [float("nan")] * 9, [1, 0, 0, 1, 2, 0, 0, 0, 3]):
            with self.assertRaises(CadError):
                _inertia_from_raw(values, "fixture")
        tensor, shape = _inertia_from_raw([1, 0.1, 0.2, 0.1, 2, 0.3, 0.2, 0.3, 3], "fixture")
        self.assertEqual(shape, "full9")
        self.assertEqual(tensor[0], (1.0, 0.1, 0.2))

    def test_parallel_axis_uses_rotational_not_second_moment_diagonal(self):
        self.assertEqual(_parallel_axis_terms(2, (1, 2, 3)), (26, 20, 10, 4, 6, 12))

    def test_sw_transform_transpose_and_translation(self):
        # SW row-vector +90deg about Z, translation(1,2,3).
        raw = (0, 1, 0, -1, 0, 0, 0, 0, 1, 1, 2, 3, 1, 0, 0, 0)
        self.assertEqual(transform_from_solidworks(raw), (0, -1, 0, 1, 1, 0, 0, 2, 0, 0, 1, 3, 0, 0, 0, 1))

    def test_scaled_and_reflected_transforms_rejected(self):
        for raw in (
            (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 2, 0, 0, 0),
            (-1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0),
        ):
            with self.assertRaises(CadError):
                transform_from_solidworks(raw)

    def test_small_asymmetry_cannot_hide_under_absolute_floor(self):
        with self.assertRaises(CadError):
            _inertia_from_raw((1e-8, 2e-9, 0, 3e-9, 1e-8, 0, 0, 0, 1e-8), "tiny")

    def test_complete_operations_and_release_have_one_owner_thread(self):
        class Backend:
            released_by = None

            def release(self):
                self.released_by = threading.get_ident()

        backend = Backend()
        executor = ComExecutor(backend)
        trace: list = []

        def submit(i):
            def operation():
                owner = threading.get_ident()
                trace.extend(((i, "start"), (i, "finish")))
                return owner

            return executor.run(operation)

        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                owners = list(pool.map(submit, range(40)))
            self.assertEqual(len(set(owners)), 1)
            self.assertNotEqual(owners[0], threading.get_ident())
            self.assertTrue(all(trace[i][0] == trace[i + 1][0] for i in range(0, 80, 2)))
            with self.assertRaisesRegex(ValueError, "expected"):
                executor.run(lambda: (_ for _ in ()).throw(ValueError("expected")))
            self.assertEqual(executor.run(lambda: 42), 42)
        finally:
            executor.close()
        self.assertEqual(backend.released_by, owners[0])
