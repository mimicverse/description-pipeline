"""Real six-stage handler chain over ArtifactStore and isolated PostgreSQL."""

from __future__ import annotations

import copy
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline.artifacts import ArtifactStore
from description_pipeline.interface_records import check_policy_digest
from description_pipeline.io import canonical, digest, inventory, write_json
from description_pipeline.orchestration.discover_protocol import (
    ArchiveDiscoverAdapter,
)
from description_pipeline.pipeline.admission import (
    AdmissionSpec,
    StageExecutors,
    ADMISSION_SPEC_SCHEMA,
    NATIVE_TOOL_REQUIREMENT_KIND,
    VerifiedArtifact,
    VerifiedPublishIntent,
    VerifiedRepositorySnapshot,
    VerifiedRuleset,
)
from description_pipeline.pipeline.application import StageApplication
from description_pipeline.pipeline.capture import CaptureHandler
from description_pipeline.pipeline.discover import DiscoverHandler
from description_pipeline.pipeline.freeze import (
    CONFIGURATION_KIND,
    FreezeHandler,
    ORIGINALS_KIND,
    RULESET_KIND,
    RulesetCatalog,
    RulesetRoot,
)
from description_pipeline.pipeline.services import (
    CanonicalStageServices,
    ClaimPolicy,
)
from description_pipeline.pipeline.stages.generate import GenerateHandler
from description_pipeline.pipeline.stages.publish import PublishHandler
from description_pipeline.pipeline.verify import VerifyHandler
from description_pipeline.platform.facts import FactStore
from description_pipeline.repository.publish import CommitSpec
from description_pipeline.stages import CONTRACT_SHA256

from tests.sources import support
from tests.v1.test_capture_handler import _capture_component_closure
from tests.v1.test_hermetic_six_stage import (
    MAIN_ASSEMBLY,
    QualifiedFixtureBackend,
    _mass_reading,
    _native_tool,
    _portable_tool,
)
from tests.v1.test_pipeline_discover import (
    ClosureReader,
    CountingBackend,
)
from tests.v1.test_pipeline_publish import PublishFixture, _PullRequests
from tests.v1.test_split_roundtrip import capture_record

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "platform"
    / "schema.sql"
)
RUN_ID = "b9b65805-7c8e-4ae3-90ef-a4d99f0ef963"
RECORDS = {
    "budget.json": {
        "robot": {
            "expected_mass_kg": [0.28, 0.32],
            "expected_extent_m": [0.35, 0.5],
        }
    },
    "joints/arm.json": {
        "limits": {"lower": -1.5, "upper": 1.5},
        "drive": {"effort": 6.0, "velocity": 2.0},
    },
}


def _verified(
    artifacts: ArtifactStore,
    ref,
) -> VerifiedArtifact:
    return VerifiedArtifact.from_store_record(
        ref,
        artifacts.verify(ref),
    )


def _qualified_backend(package: Path) -> QualifiedFixtureBackend:
    components = [
        {
            "name": "base-1",
            "path": str(package / "cad/base.SLDPRT"),
            "transform": support.placement(),
            "mass": _mass_reading(
                0.192,
                (0.03, 0.02, 0.01),
                package / "cad/base.SLDPRT",
            ),
        },
        {
            "name": "arm-1",
            "path": str(package / "cad/arm.SLDPRT"),
            "transform": support.placement((0.0, 0.0, 0.1)),
            "mass": _mass_reading(
                0.105654866776462,
                (0.0, 0.0, 0.05),
                package / "cad/arm.SLDPRT",
            ),
        },
    ]
    return QualifiedFixtureBackend(
        package / MAIN_ASSEMBLY,
        components,
        dependencies=[
            package / "cad/base.SLDPRT",
            package / "cad/arm.SLDPRT",
        ],
        coordinate_systems={
            "CS_base_link": support.placement(),
            "CS_arm_link": support.placement((0.0, 0.0, 0.1)),
        },
    )


@unittest.skipUnless(
    PG_AVAILABLE,
    "pgserver/psycopg2 are not installed",
)
class RealSixStageChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_root = tempfile.TemporaryDirectory(
            prefix="real-six-stage-postgres-"
        )
        cls.server = pgserver.get_server(
            Path(cls.server_root.name) / "pgdata",
            cleanup_mode="delete",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.cleanup()
        cls.server_root.cleanup()

    def setUp(self) -> None:
        self.schema = "chain_" + uuid.uuid4().hex
        schema_sql = PLATFORM_SCHEMA.read_text(encoding="utf-8").replace(
            ':"platform_schema"',
            f'"{self.schema}"',
        )
        with (
            psycopg2.connect(self.server.get_uri()) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')
            cursor.execute(schema_sql)
            cursor.execute(
                f'UPDATE "{self.schema}".admission '
                "SET state = %s WHERE id = 1",
                ("open",),
            )

        self.publish = PublishFixture()
        self.addCleanup(self.publish.temporary.cleanup)
        self.root = self.publish.root
        self.store = self.publish.store
        self.portable = _portable_tool()
        self.native = _native_tool()
        release = {
            "schema_version": "solidworks-to-urdf.release/v1",
            "pipeline_id": "solidworks-to-urdf",
            "version": "1.5.0",
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "source_sha256": digest(
                {"description_pipeline/runtime.py": "c" * 64}
            ),
            "package_files": {
                "description_pipeline/runtime.py": "c" * 64,
            },
        }
        self.portable["release"] = copy.deepcopy(release)
        self.native["release"] = copy.deepcopy(release)

        self.source_root = self.root / "source"
        for relative, payload in {
            MAIN_ASSEMBLY: b"placeholder cad/robot.SLDASM\n",
            "cad/base.SLDPRT": b"placeholder cad/base.SLDPRT\n",
            "cad/arm.SLDPRT": b"placeholder cad/arm.SLDPRT\n",
            "spare/base.SLDPRT": b"selected same-basename part\n",
            "unused/base.SLDPRT": b"excluded same-basename part\n",
        }.items():
            target = self.source_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        self.originals_ref = self.store.seal_tree(
            self.source_root,
            kind=ORIGINALS_KIND,
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest({"producer": ORIGINALS_KIND}),
        )

        self.rules_root = self.root / "rules"
        logical_root = self.rules_root / "root-000"
        for relative, payload in RECORDS.items():
            write_json(logical_root / relative, payload)
        root_record = RulesetRoot(
            path="root-000",
            members=inventory(logical_root),
        )
        catalog = RulesetCatalog(
            version="records-"
            + digest([root_record.to_record()]),
            roots=(root_record,),
            records={
                f"root-000/{relative}": checksum
                for relative, checksum in root_record.members.items()
            },
        )
        write_json(
            self.rules_root / "catalog.json",
            catalog.to_record(),
        )
        self.ruleset_ref = self.store.seal_tree(
            self.rules_root,
            kind=RULESET_KIND,
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest({"producer": RULESET_KIND}),
        )

        self.configuration_root = self.root / "configuration"
        write_json(
            self.configuration_root / "config.json",
            {"snapshot": "real-six-stage"},
        )
        self.configuration_ref = self.store.seal_tree(
            self.configuration_root,
            kind=CONFIGURATION_KIND,
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest(
                {"producer": CONFIGURATION_KIND}
            ),
        )
        self.native_root = self.root / "native-requirement"
        self.native_root.mkdir()
        (self.native_root / "tool.json").write_bytes(
            canonical(self.native)
        )
        self.native_ref = self.store.seal_tree(
            self.native_root,
            kind=NATIVE_TOOL_REQUIREMENT_KIND,
            producer_run_id=None,
            producer_stage=None,
            producer_tool_sha256=digest(
                {"producer": NATIVE_TOOL_REQUIREMENT_KIND}
            ),
        )

        self.facts = FactStore(self._connect, self.store)
        self.services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.store,
            work_root=self.root / "engine-work",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"owner": "real-six-stage"}),
                executor_sha256=self.portable["source_sha256"],
                lease=timedelta(minutes=10),
                claim_id_factory=lambda: "chain-claim-" + uuid.uuid4().hex,
            ),
            clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
        )

    def _connect(self):
        return psycopg2.connect(
            self.server.get_uri(),
            options=f"-c search_path={self.schema},public",
        )

    def _spec(self) -> AdmissionSpec:
        snapshot = _verified(self.store, self.publish.snapshot_ref)
        metadata = canonical(self.publish.snapshot.to_record())
        repository = VerifiedRepositorySnapshot.from_metadata_bytes(
            snapshot,
            metadata,
        )
        commit_record = dict(
            self.publish.command.request["commit"]
        )
        ruleset = VerifiedRuleset.from_catalog_bytes(
            _verified(self.store, self.ruleset_ref),
            (self.rules_root / "catalog.json").read_bytes(),
        )
        executors = StageExecutors(
            freeze=self.portable["source_sha256"],
            discover=self.portable["source_sha256"],
            capture=self.portable["source_sha256"],
            generate=self.portable["source_sha256"],
            verify=self.portable["source_sha256"],
            publish=self.portable["source_sha256"],
        )
        return AdmissionSpec(
            schema_version=ADMISSION_SPEC_SCHEMA,
            run_id=RUN_ID,
            request_id="real-six-stage-request",
            parent_run_id=None,
            from_stage=None,
            principal="fixture:real-six-stage",
            target_hardware="nd_fixture",
            purpose="engineering",
            change_summary="Exercise the real six-stage application chain",
            main_assembly=MAIN_ASSEMBLY,
            originals=_verified(self.store, self.originals_ref),
            ruleset=ruleset,
            configuration=_verified(
                self.store,
                self.configuration_ref,
            ),
            executors=executors,
            dispatch_executor_sha256=self.portable["source_sha256"],
            native_tool_source_sha256=self.native["source_sha256"],
            native_tool_record_sha256=digest(self.native),
            native_tool_requirement=_verified(
                self.store,
                self.native_ref,
            ),
            contract_sha256=CONTRACT_SHA256,
            policy_sha256=check_policy_digest(),
            platform_config_sha256=self.configuration_ref.set_sha256,
            publish_intent=VerifiedPublishIntent(
                repository=repository,
                commit=CommitSpec(**commit_record),
            ),
            created_at=datetime(2026, 10, 25, tzinfo=UTC),
        )

    def _application(self) -> StageApplication:
        self.pull_requests = _PullRequests()
        discover = DiscoverHandler(
            ArchiveDiscoverAdapter(
                artifacts=self.store,
                native_tool=self.native,
                selection_reader_factory=lambda: ClosureReader(
                    transform=_capture_component_closure,
                    reader_sha256=self.native["source_sha256"],
                ),
                backend_factory=lambda _root, _files: CountingBackend(
                    copy.deepcopy(capture_record())
                ),
            )
        )
        capture = CaptureHandler(
            artifact_store=self.store,
            backend_factory=_qualified_backend,
            native_tool_factory=lambda: copy.deepcopy(self.native),
        )
        return StageApplication(
            services=self.services,
            facts=self.facts,
            artifacts=self.store,
            handlers={
                "freeze": FreezeHandler(),
                "discover": discover,
                "capture": capture,
                "generate": GenerateHandler(self.store),
                "verify": VerifyHandler(
                    identity_provider=lambda: copy.deepcopy(
                        self.portable
                    )
                ),
                "publish": PublishHandler(
                    self.store,
                    pull_requests=self.pull_requests,
                ),
            },
            builders={},
            clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
        )

    def test_real_stage_handlers_complete_one_admitted_run(self) -> None:
        application = self._application()
        admitted = application.admit(self._spec())
        self.assertEqual(
            admitted.dispatch_command.expected_executor_sha256,
            admitted.admission.request["dispatch_executor_sha256"],
        )
        with (
            patch(
                "description_pipeline.runtime.tool_record",
                return_value=self.native,
            ),
            patch(
                "description_pipeline.pipeline.stages.generate.tool_record",
                return_value=self.portable,
            ),
            patch(
                "description_pipeline.pipeline.stages.publish.tool_record",
                return_value=self.portable,
            ),
        ):
            outcome = application.execute_run(RUN_ID)

        self.assertEqual(outcome.outcome, "completed")
        self.assertEqual(outcome.stage, "publish")
        self.assertEqual(len(self.pull_requests.rows), 1)
        self.assertEqual(
            self.pull_requests.rows[0].head_ref,
            self.publish.snapshot.branch_ref,
        )
        with (
            self._connect() as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT stage, outcome FROM stage_decision "
                "WHERE run_id = %s "
                "ORDER BY array_position("
                "ARRAY['freeze','discover','capture','generate','verify','publish'],"
                " stage)",
                (RUN_ID,),
            )
            stages = cursor.fetchall()
        self.assertEqual(
            stages,
            [
                ("freeze", "completed"),
                ("discover", "completed"),
                ("capture", "completed"),
                ("generate", "completed"),
                ("verify", "completed"),
                ("publish", "completed"),
            ],
        )

if __name__ == "__main__":
    unittest.main()
