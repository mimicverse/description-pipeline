"""One native observation closes before its closure is sealed or pruned."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from description_pipeline.io import digest, file_digest
from description_pipeline.orchestration.discover_protocol import run_windows_discover, seal_source_archive
from description_pipeline.sources.solidworks.discover_stage import NativeDiscoverRefusal, NativeDiscoverRequest
from description_pipeline.schemas import NATIVE_DISCOVER_REQUEST_SCHEMA
from description_pipeline.sources.solidworks.native import SolidWorksBackend
from description_pipeline.sources.solidworks.errors import CadError, EnvironmentError_
from description_pipeline.sources.solidworks.saved_state import warning_from_selection
from tests.sources.test_solidworks_native_reader import _App, _Component, _Doc, _Session, _com_stubs
from tests.v1.test_pipeline_discover import NATIVE_TOOL, NATIVE_TOOL_RECORD_SHA256, TOOL_SHA256


class UnifiedDiscoverObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="unified-discover-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("robot.SLDASM", "arm.SLDPRT", "excluded.SLDPRT"):
            (self.source / name).write_bytes(name.encode())
        self.files = {path.name: file_digest(path) for path in self.source.iterdir()}
        self.request = NativeDiscoverRequest(
            schema_version=NATIVE_DISCOVER_REQUEST_SCHEMA,
            operation_id="unified-native-operation",
            command_id="discover-command",
            run_id="discover-run",
            request_digest=digest({"request": "unified"}),
            source_files=self.files,
            source_inventory_sha256=digest(self.files),
            main_assembly="robot.SLDASM",
            purpose="engineering",
            expected_native_tool_sha256=TOOL_SHA256,
            expected_native_tool_record_sha256=NATIVE_TOOL_RECORD_SHA256,
            native_tool_requirement_artifact_id="native-tool-requirement-fixture",
        )
        self.archive = seal_source_archive(self.source, self.files, self.root / "source.zip")
        self.events = []
        self.sessions = []
        self.backends = []

    def run_observation(self, *, missing=False, fail_restore=False, fail_close=False, default_backend=False):
        events, sessions, backends = self.events, self.sessions, self.backends

        def factory(stage, files):
            self.assertEqual(dict(files), self.files)
            part = _Doc(stage / "arm.SLDPRT")
            component = _Component("arm-1", stage / "arm.SLDPRT", doc=part)
            assembly = _Doc(stage / "robot.SLDASM", doc_type=2, children=[component])

            class App(_App):
                def GetDocumentDependencies2(self, path, traverse, search, read_only):
                    if not search:
                        self_outer.assertNotIn("open", events)
                        events.append("stored")
                        return ()
                    events.append("missing")
                    return ("construction", "C:/missing/construction.SLDPRT", True)

                def OpenDoc6(self, *args):
                    events.append("open")
                    return super().OpenDoc6(*args)

                def SetSearchFolders(self, folder_type, folders):
                    if not folders:
                        events.append("restore")
                        if fail_restore:
                            raise RuntimeError("search restoration failed")
                    return super().SetSearchFolders(folder_type, folders)

            self_outer = self
            app = App({stage / "robot.SLDASM": assembly}, open_errors={stage / "robot.SLDASM": 2 if missing else 0})

            class Session(_Session):
                def close(self):
                    events.append("close")
                    self_outer.assertTrue((stage / "excluded.SLDPRT").exists())
                    if fail_close:
                        raise RuntimeError("owned session close failed")
                    return super().close()

            def session_factory():
                session = Session(app)
                sessions.append(session)
                return session

            backend = SolidWorksBackend(session_factory=session_factory)
            backends.append(backend)
            return backend

        with _com_stubs():
            if default_backend:
                ledger = self.root / "owned-cad.json"
                with patch(
                    "description_pipeline.sources.solidworks.native.SolidWorksBackend",
                    side_effect=lambda **kwargs: factory(self.root / "workspace/staged", self.files),
                ) as constructor:
                    observation = run_windows_discover(
                        self.archive, self.request, self.root / "workspace",
                        native_tool=NATIVE_TOOL, cad_ownership_file=ledger,
                    )
                constructor.assert_called_once_with(cad_ownership_file=ledger)
                return observation
            return run_windows_discover(
                self.archive, self.request, self.root / "workspace",
                native_tool=NATIVE_TOOL, backend_factory=factory,
            )

    def test_default_composition_constructs_only_one_owned_backend(self):
        observation = self.run_observation(default_backend=True)
        self.assertEqual(len(self.backends), 1)
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(self.events.count("open"), 1)
        self.assertTrue(observation.resources_settled)

    def test_nested_session_cannot_emit_a_falsely_closed_observation(self):
        backend = SolidWorksBackend(session_factory=lambda: self.fail("nested Discover must not connect"))
        with _com_stubs(), backend.session(), self.assertRaises(CadError) as raised:
            backend.discover_native(self.source, {"main_assembly": "robot.SLDASM"})
        self.assertEqual(raised.exception.code, "cad_discovery_nested_session")

    def test_one_exact_open_and_one_released_observation_before_pruning(self):
        observation = self.run_observation(missing=True)
        self.assertEqual(len(self.backends), 1)
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(self.events.count("open"), 1)
        self.assertEqual(self.events.count("stored"), 3)
        self.assertLess(self.events.index("stored"), self.events.index("open"))
        self.assertLess(self.events.index("restore"), self.events.index("close"))
        self.assertTrue(self.sessions[0].closed)
        self.assertTrue(observation.record["session"]["closed"])
        self.assertTrue(observation.record["search"]["restored"])
        self.assertFalse((self.root / "workspace/staged/excluded.SLDPRT").exists())
        self.assertEqual(observation.record["open_status"]["errors"], 2)
        self.assertEqual(observation.record["open_status"], observation.selection["closure"]["open_status"])
        warning = warning_from_selection(observation.selection)
        self.assertEqual(warning["detail"]["open_observations"]["discover"]["status"]["errors"], 2)
        self.assertEqual(set(observation.selection["files"]), {"robot.SLDASM", "arm.SLDPRT"})

    def test_search_restore_failure_closes_then_refuses_without_pruning(self):
        with self.assertRaises(NativeDiscoverRefusal):
            self.run_observation(fail_restore=True)
        self.assertTrue(self.sessions[0].closed)
        self.assertLess(self.events.index("restore"), self.events.index("close"))
        self.assertTrue((self.root / "workspace/staged/excluded.SLDPRT").exists())
        self.assertFalse((self.root / "workspace/native-observation/observation.json").exists())

    def test_failed_close_cannot_claim_settlement_or_prune(self):
        with self.assertRaises(EnvironmentError_):
            self.run_observation(fail_close=True)
        self.assertFalse(self.sessions[0].closed)
        self.assertTrue((self.root / "workspace/staged/excluded.SLDPRT").exists())
        self.assertFalse((self.root / "workspace/native-observation/observation.json").exists())


if __name__ == "__main__":
    unittest.main()
