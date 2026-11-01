"""Real six-stage handler chain over ArtifactStore and isolated PostgreSQL."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

try:
    import pgserver
    import psycopg2
except ImportError:
    pgserver = None
    psycopg2 = None

from description_pipeline import __version__
from description_pipeline.artifacts import ArtifactStore
from description_pipeline.interface_records import config_snapshot_digest
from description_pipeline.io import digest, read_data, write_json
from tests.v1.archive_discover_adapter import ArchiveDiscoverAdapter
from description_pipeline.orchestration.native_operation_client import (
    NativeOperationClient,
    NativeOperationEndpoint,
    NativeOperationToolProvider,
)
from description_pipeline.orchestration.native_operation_runtime import (
    CAPTURE_KIND,
    DISCOVER_KIND,
    CaptureOperationExecutor,
    DiscoverOperationExecutor,
    NativeOperationRuntime,
)
from description_pipeline.orchestration.windows import (
    handler as windows_handler,
)
from description_pipeline.pipeline.admission import AdmissionSpec
from description_pipeline.pipeline.application import (
    StageApplication,
    application_from_environment,
)
from description_pipeline.pipeline.capture import CaptureHandler
from description_pipeline.pipeline.discover import DiscoverHandler
from description_pipeline.pipeline.effects import EffectState
from description_pipeline.pipeline.freeze import (
    FreezeHandler,
)
from description_pipeline.pipeline.ingestion import (
    ProductionAdmissionRequest,
    ProductionAdmissionFactory,
    ProductionMJCFSuccessorRequest,
    ProductionFreezeSuccessorRequest,
    ReceivedUploadReceipt,
    load_production_admission,
    load_production_freeze_successor,
)
from description_pipeline.pipeline.services import (
    CanonicalStageServices,
    ClaimPolicy,
)
from description_pipeline.pipeline.reconciliation import EffectReconciliationService
from description_pipeline.pipeline.stages.generate import GenerateHandler
from description_pipeline.pipeline.stages.mjcf import MJCFHandler
from description_pipeline.pipeline.stages.publish import (
    PR_EFFECT_KEY,
    PublishHandler,
    PublishPullRequestReconciliationAdapter,
)
from description_pipeline.pipeline.verify import VerifyHandler
from description_pipeline.platform.facts import FactStore
from description_pipeline.schemas import (
    CONFIG_SNAPSHOT_SCHEMA,
    INSTALL_RECORD_SCHEMA,
    PLATFORM_CONFIG_SCHEMA,
)
from description_pipeline.pipeline.contracts import (
    NativeTargetIdentity,
    StageEffectWait,
    StagePending,
)
from description_pipeline.sources.solidworks.handoff import describe_handoff
from description_pipeline.stages import CONTRACT_SHA256

from tests.sources import support
from tests.v1.test_capture_handler import _capture_component_closure
from tests.v1.native_fixture_support import capture_record
from tests.v1.verification_fixture import (
    MAIN_ASSEMBLY,
    QualifiedFixtureBackend,
    _mass_reading,
    _native_tool,
    _portable_tool,
)
from tests.v1.test_pipeline_discover import (
    NativeObservationFixture,
    CountingBackend,
)
from tests.v1.test_pipeline_publish import PublishFixture, _PullRequests, git

PG_AVAILABLE = pgserver is not None and psycopg2 is not None
PLATFORM_SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "platform"
    / "schema.sql"
)
RUN_ID = "b9b65805-7c8e-4ae3-90ef-a4d99f0ef963"
SUCCESSOR_RUN_ID = "53d6bf77-6d98-40cb-a20b-6c127e36c041"
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


class _UnknownPullRequests(_PullRequests):
    """Lose the create response on the first observation without losing the row."""

    def __init__(self) -> None:
        super().__init__()
        self.hide_next_observe = False

    def create(self, **kwargs):
        super().create(**kwargs)
        self.hide_next_observe = True
        raise RuntimeError("controlled pull-request response loss")

    def observe(self, **kwargs):
        if self.hide_next_observe:
            self.hide_next_observe = False
            return ()
        return super().observe(**kwargs)


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
        self.addCleanup(self.publish.close)
        git_target = patch(
            "description_pipeline.pipeline.ingestion.validate_git_remote",
            return_value=str(self.publish.remote),
        )
        git_target.start()
        self.addCleanup(git_target.stop)
        self.root = self.publish.root
        self.store = self.publish.store
        self.portable = _portable_tool()
        self.native = _native_tool()
        self.native_target = NativeTargetIdentity.create(
            install_id="win-" + "4" * 32,
            pipeline_id="solidworks-to-urdf",
        )
        release = {
            "schema_version": "solidworks-to-urdf.release/v1",
            "pipeline_id": "solidworks-to-urdf",
            "version": __version__,
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "source_sha256": digest(
                {"description_pipeline/runtime.py": "c" * 64}
            ),
            "package_files": {
                "description_pipeline/runtime.py": "c" * 64,
            },
            "contract": {
                "schema_version": "fixture-contract/v1",
                "digest": CONTRACT_SHA256,
            },
            "support": {
                "python": "3.12",
                "targets": ["linux", "windows"],
            },
        }
        self.portable["release"] = copy.deepcopy(release)
        self.native["release"] = copy.deepcopy(release)
        self.portable["source_sha256"] = release["source_sha256"]
        self.native["source_sha256"] = release["source_sha256"]
        mjcf_identity = patch("description_pipeline.mjcf_contract.tool_record",
                              side_effect=lambda **_kwargs: copy.deepcopy(self.portable))
        mjcf_identity.start()
        self.addCleanup(mjcf_identity.stop)

        self.handoff_root = self.root / "handoff"
        self.upload_root = self.handoff_root / "run-produced"
        self.source_root = self.upload_root / "CAD"
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

        self.rules_root = self.root / "rules"
        logical_root = self.rules_root / "root-000"
        for relative, payload in RECORDS.items():
            write_json(logical_root / relative, payload)

        self.facts = FactStore(self._connect, self.store)
        self._bind_services()

    def _bind_services(
        self,
        *,
        owner: str = "real-six-stage",
        now: datetime | None = None,
        work_suffix: str = "",
    ) -> None:
        current = datetime(2026, 10, 25, tzinfo=UTC) if now is None else now
        self.services = CanonicalStageServices(
            facts=self.facts,
            artifacts=self.store,
            work_root=self.root / f"engine-work{work_suffix}",
            claim_policy=ClaimPolicy(
                owner_sha256=digest({"owner": owner}),
                executor_sha256=self.portable["source_sha256"],
                lease=timedelta(minutes=10),
                claim_id_factory=lambda: "chain-claim-" + uuid.uuid4().hex,
            ),
            clock=lambda: current,
        )

    def _connect(self):
        return psycopg2.connect(
            self.server.get_uri(),
            options=f"-c search_path={self.schema},public",
        )

    def _production_request(
        self,
        *,
        run_id: str = RUN_ID,
        request_id: str = "real-six-stage-request",
        principal: str = "cli:tenant:ou_chain",
        change_summary: str = "Exercise production intake through six real handlers",
    ) -> ProductionAdmissionRequest:
        identity = describe_handoff(self.source_root)
        receipt = ReceivedUploadReceipt(
            folder="CAD",
            files=len(identity["files"]),
            bytes=sum(
                (self.source_root / name).stat().st_size
                for name in identity["files"]
            ),
            handoff_sha256=identity["handoff_sha256"],
            main_assembly=MAIN_ASSEMBLY,
            target_hardware="nd_fixture",
            change_summary=change_summary,
            purpose="engineering",
        )
        return ProductionAdmissionRequest(
            run_id=run_id,
            request_id=request_id,
            upload_dir=self.upload_root,
            receipt=receipt,
            main_assembly=MAIN_ASSEMBLY,
            target_hardware="nd_fixture",
            purpose="engineering",
            change_summary=receipt.change_summary,
            principal=principal,
        )

    def _platform_snapshot(
        self,
        *,
        generated_at: str = "2026-10-25T00:00:00Z",
    ) -> Path:
        token = self.root / "endpoint-token"
        secret = self.root / "feishu-secret"
        certificate = self.root / "operator-certificate.pem"
        private_key = self.root / "operator-private-key.pem"
        dsn = self.root / "database-dsn"
        for path, content in (
            (token, "t" * 64),
            (secret, "s" * 64),
            (certificate, "certificate"),
            (private_key, "key"),
            (dsn, "postgresql://fixture"),
        ):
            path.write_text(content, encoding="utf-8")
        paths = {}
        for name in ("state", "workspace-root", "release-root"):
            path = self.root / name
            path.mkdir(exist_ok=True)
            paths[name] = path
        repository = paths["workspace-root"] / "robot"
        if not repository.exists():
            shutil.copytree(self.publish.repo, repository)
        self.production_repository = repository
        snapshot = {
            "schema_version": CONFIG_SNAPSHOT_SCHEMA,
            "install_id": "real-six-stage",
            "generated_at": generated_at,
            "resolved": {
                "schema_version": PLATFORM_CONFIG_SCHEMA,
                "operator": {
                    "origin": "https://operator.example.test",
                    "certificate_file": str(certificate),
                    "private_key_file": str(private_key),
                },
                "storage": {
                    "state_root": str(paths["state"]),
                    "handoff_root": str(self.handoff_root),
                    "artifact_root": str(self.store.root),
                    "workspace_root": str(paths["workspace-root"]),
                    "release_root": str(paths["release-root"]),
                },
                "database": {"dsn_file": str(dsn)},
                "windows": {
                    "ssh_host": "windows-fixture",
                    "endpoint_port": 8765,
                    "token_file": str(token),
                    "tunnel_loopback_port": 18765,
                    "instance": {
                        "task_name": "description-native",
                        "package_root": "C:/payloads/packages",
                        "output_root": "C:/payloads/outputs",
                        "state_root": "C:/payloads/state",
                    },
                },
                "auth": {
                    "feishu_secret_file": str(secret),
                    "tenant_keys": ["tenant"],
                    "admin_open_ids": [],
                },
                "targets": {
                    "nd_fixture": {
                        "repository_slug": "example/robot",
                        "base": "main",
                    }
                },
                "specifications": {
                    "record_roots": [str(self.rules_root / "root-000")]
                },
                "retention": {"unreferenced_evidence_days": None},
                "derived": {
                    "repositories": {
                        "example/robot": str(repository),
                    },
                    "discovery": {
                        "record_roots": [str(self.rules_root / "root-000")]
                    },
                },
            },
            "sources": {"platform_json_sha256": "a" * 64},
        }
        path = self.root / "config-snapshot.json"
        write_json(path, snapshot)
        self._write_install_record(path, snapshot)
        self.platform_snapshot_path = path
        return path

    def _write_install_record(self, path: Path, snapshot: dict) -> None:
        state_root = Path(snapshot["resolved"]["storage"]["state_root"])
        snapshot_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        release_common = self.portable["release"]
        release_triple = {
            "version": release_common["version"],
            "source_commit": release_common["source_commit"],
            "source_sha256": release_common["source_sha256"],
        }
        record = {
            "schema_version": INSTALL_RECORD_SCHEMA,
            "install_id": snapshot["install_id"],
            "timestamp": "2026-10-25T00:00:00Z",
            "snapshot": {
                "path": str(path),
                "digest": snapshot_sha256,
                "install_id": snapshot["install_id"],
            },
            "release": {
                **release_triple,
                "artifacts": [
                    {
                        "name": "fixture.whl",
                        "sha256": digest({"fixture": "artifact"}),
                    }
                ],
            },
            "services": ["description-postgres"],
            "paths": {"state_root": str(state_root)},
            "dag": {
                "id": "solidworks_to_urdf",
                "path": "/srv/description/dags/solidworks_to_urdf.py",
                "digest": digest({"dag": "fixture"}),
                "tasks": 7,
            },
            "ends": {
                "linux": {
                    "install_id": snapshot["install_id"],
                    "config_digest": snapshot_sha256,
                    "release": {
                        **release_triple,
                        "role": "portable",
                    },
                },
                "windows": {
                    "install_id": self.native_target.install_id,
                    "config_digest": digest({"windows": snapshot_sha256}),
                    "release": {
                        **release_triple,
                        "role": "native",
                    },
                    "target_identity": self.native_target.to_record(),
                },
            },
        }
        write_json(state_root / "install-record.json", record)

    def _install_current_tools(
        self,
        *,
        source_commit: str,
        source_tree: str,
        seed: str,
    ) -> None:
        portable = copy.deepcopy(self.portable)
        native = copy.deepcopy(self.native)
        for role, tool in (("portable", portable), ("native", native)):
            package_files = {
                f"description_pipeline/{role}_{seed}.py": digest(
                    {"release-file": role, "seed": seed}
                )
            }
            source_sha256 = digest(package_files)
            tool["source_sha256"] = source_sha256
            tool["release"]["source_sha256"] = source_sha256
            tool["release"]["source_commit"] = source_commit
            tool["release"]["source_tree"] = source_tree
            tool["release"]["package_files"] = package_files
        self.portable = portable
        self.native = native
        self._bind_services()

    def _advance_production_repository(self, generation: str) -> None:
        target = self.production_repository / "README.md"
        target.write_text(
            f"fixture\nsuccessor generation {generation}\n",
            encoding="utf-8",
        )
        for command in (
            ("add", "README.md"),
            ("commit", "-m", f"successor generation {generation}"),
            ("push", "origin", "main"),
        ):
            subprocess.run(
                ["git", "-C", str(self.production_repository), *command],
                check=True,
                capture_output=True,
                text=True,
            )

    def _application(
        self,
        *,
        pull_requests=None,
        reconciler=None,
        reconciliation_adapter=None,
        clock: datetime | None = None,
    ) -> StageApplication:
        self.pull_requests = pull_requests or _PullRequests()
        current = datetime(2026, 10, 25, tzinfo=UTC) if clock is None else clock
        discover = DiscoverHandler(
            ArchiveDiscoverAdapter(
                artifacts=self.store,
                native_tool=self.native,
                backend_factory=lambda _root, _files: CountingBackend(
                    copy.deepcopy(capture_record()),
                    observation_fixture=NativeObservationFixture(
                        transform=_capture_component_closure,
                        reader_sha256=self.native["source_sha256"],
                    ),
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
                "mjcf": MJCFHandler(self.store),
                "publish": PublishHandler(
                    self.store,
                    pull_requests=self.pull_requests,
                ),
            },
            builders={},
            clock=lambda: current,
            reconciler=reconciler,
            reconciliation_adapters=(
                {"pr_upsert": reconciliation_adapter}
                if reconciliation_adapter is not None
                else None
            ),
        )

    def test_production_intake_completes_the_real_six_stage_chain(self) -> None:
        application = self._application()
        factory_work = self.root / "factory-work"
        factory_work.mkdir()
        request = self._production_request(
            change_summary="Align the camera bracket\r\nRetain measured joint limits\rReview the full assembly",
        )
        platform_snapshot = self._platform_snapshot()
        with (
            patch(
                "description_pipeline.runtime.tool_record",
                return_value=copy.deepcopy(self.native),
            ),
            patch(
                "description_pipeline.pipeline.stages.generate.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
            patch(
                "description_pipeline.pipeline.stages.publish.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
        ):
            admitted = load_production_admission(
                application,
                request,
                artifacts=self.store,
                platform_snapshot=platform_snapshot,
                native_tool_provider=lambda: copy.deepcopy(self.native),
                work_root=factory_work,
                clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
                portable_tool_provider=lambda: copy.deepcopy(self.portable),
            )
            spec = AdmissionSpec.from_record(admitted.admission.request)
            self.assertEqual(
                application.load_admission_outcome(RUN_ID),
                admitted,
            )
            self.assertEqual(
                admitted.dispatch_command.dag_run_id,
                request.request_id,
            )
            self.assertEqual(
                spec.originals.inventory_sha256,
                request.receipt.handoff_sha256,
            )
            self.assertEqual(
                spec.platform_config_sha256,
                config_snapshot_digest(read_data(platform_snapshot)),
            )
            outcome = application.execute_run(RUN_ID)

        self.assertEqual(outcome.outcome, "completed", repr(outcome))
        self.assertEqual(outcome.stage, "publish")
        self.assertEqual(len(self.pull_requests.rows), 1)
        self.assertEqual(
            self.pull_requests.rows[0].head_ref,
            spec.publish_intent.repository.branch_ref,
        )
        self._assert_published_change_summary(
            spec,
            "Align the camera bracket\nRetain measured joint limits\nReview the full assembly",
        )
        with (
            self._connect() as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT stage, outcome FROM stage_decision "
                "WHERE run_id = %s "
                "ORDER BY array_position("
                "ARRAY['freeze','discover','capture','generate','verify','mjcf','publish'],"
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
                ("mjcf", "completed"),
                ("publish", "completed"),
            ],
        )

    def test_mjcf_only_successor_uses_real_qualified_inputs_without_upstream_execution(self) -> None:
        from description_pipeline.runtime import tool_record

        actual_executor = tool_record(role="portable")["source_sha256"]
        application = self._application()
        work = self.root / "mjcf-successor-factory"
        work.mkdir()
        snapshot = self._platform_snapshot()
        factory = ProductionAdmissionFactory(
            artifacts=self.store, platform_snapshot=snapshot, native_tool_provider=lambda: copy.deepcopy(self.native),
            portable_tool_provider=lambda: copy.deepcopy(self.portable), work_root=work,
            clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
        )
        with (
            patch("description_pipeline.runtime.tool_record", return_value=copy.deepcopy(self.native)),
            patch("description_pipeline.pipeline.stages.generate.tool_record",
                  return_value=copy.deepcopy(self.portable)),
            patch("description_pipeline.pipeline.stages.publish.tool_record",
                  return_value=copy.deepcopy(self.portable)),
        ):
            prepared = factory.prepare(self._production_request())
            source = replace(prepared.spec,
                             simulation='{"collision":{"source":"visual_convex","max_vertices":4,"max_error_m":1e-12}}')
            application.admit(source)
            failed = application.execute_run(source.run_id)
            self.assertEqual(("mjcf", "failed"), (failed.stage, failed.outcome))
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT stage, result_digest FROM stage_decision WHERE run_id = %s ORDER BY stage",
                               (source.run_id,))
                before = cursor.fetchall()
            def forbidden_native():
                raise AssertionError("MJCF rerun contacted the native endpoint")
            factory.native_tool_provider = forbidden_native
            cold_request = ProductionMJCFSuccessorRequest(
                run_id="cold-mjcf-run", request_id="cold-mjcf-request", parent_run_id=source.run_id,
                principal=source.principal, change_summary="Admit and execute MJCF without native capabilities",
            )
            captured = []

            class CapturedAdmission(Exception):
                pass

            def capture_spec(spec):
                captured.append(spec)
                raise CapturedAdmission()

            with patch.object(application, "admit", side_effect=capture_spec), self.assertRaises(CapturedAdmission):
                factory.admit_mjcf(application, cold_request)
            cold_spec = replace(captured[0], executors=replace(captured[0].executors, mjcf=actual_executor))
            spec_path = work / "cold-admission.json"
            write_json(spec_path, cold_spec.to_record())
            self.assertIsNone(application.facts.load_admission(cold_spec.run_id))
            environment = {
                "PATH": os.environ["PATH"],
                "PYTHONPATH": str(Path(application_from_environment.__code__.co_filename).resolve().parents[2]),
                "PLATFORM_SERVICE_DSN": psycopg2.extensions.make_dsn(
                    self.server.get_uri(), options=f"-c search_path={self.schema},public",
                ),
                "PLATFORM_ARTIFACT_ROOT": str(self.store.root),
                "PIPELINE_WORK_ROOT": str(work / "cold-runtime"),
            }

            def cold_cli(*args):
                return subprocess.run([sys.executable, "-B", "-m", "description_pipeline.cli", *args],
                                      env=environment, capture_output=True, text=True, timeout=60)

            cold = cold_cli("admit", str(spec_path))
            self.assertEqual(0, cold.returncode, cold.stderr)
            admitted_cold = json.loads(cold.stdout)
            self.assertEqual(cold_spec.run_id, admitted_cold["admission"]["run_id"])
            repeated = cold_cli("admit", str(spec_path))
            self.assertEqual(0, repeated.returncode, repeated.stderr)
            self.assertEqual(admitted_cold, json.loads(repeated.stdout))
            cold_stage = cold_cli("stage", admitted_cold["entry_command"]["command_id"])
            self.assertEqual(0, cold_stage.returncode, cold_stage.stderr)
            self.assertEqual("completed", json.loads(cold_stage.stdout)["outcome"])
            invalid = replace(cold_spec, run_id="invalid-cold-run", request_id="invalid-cold-request",
                              parent_run_id="missing-source")
            write_json(spec_path, invalid.to_record())
            rejected = cold_cli("admit", str(spec_path))
            self.assertNotEqual(0, rejected.returncode)
            self.assertNotIn("native_target_missing", rejected.stderr)
            self.assertIsNone(application.facts.load_admission(invalid.run_id))
            rerun = ProductionMJCFSuccessorRequest(
                run_id="mjcf-only-run", request_id="mjcf-only-request", parent_run_id=source.run_id,
                principal=source.principal, change_summary="Use a qualified collision budget",
                simulation='{"collision":{"max_vertices":128}}',
            )
            with ExitStack() as stack:
                for stage in ("freeze", "discover", "capture", "generate", "verify"):
                    stack.enter_context(patch.object(application.handlers[stage], "execute",
                                                     side_effect=AssertionError("Upstream execution was repeated")))
                admitted = factory.admit_mjcf(application, rerun)
                self.assertEqual("mjcf", admitted.entry_command.stage)
                self.assertEqual(admitted, factory.admit_mjcf(application, rerun))
                for stage in ("freeze", "discover", "capture", "generate", "verify"):
                    reuse = application.run_stage(rerun.run_id, stage).to_record()
                    self.assertEqual(("reused", source.run_id), (reuse["outcome"], reuse["source_run_id"]))
                completed = application.execute_run(rerun.run_id)
                self.assertEqual(("publish", "completed"), (completed.stage, completed.outcome), completed)
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT stage, outcome FROM stage_decision WHERE run_id = %s ORDER BY stage",
                               (rerun.run_id,))
                self.assertEqual([("mjcf", "completed"), ("publish", "completed")], cursor.fetchall())
                cursor.execute("SELECT stage, result_digest FROM stage_decision WHERE run_id = %s ORDER BY stage",
                               (source.run_id,))
                self.assertEqual(before, cursor.fetchall())

    def _assert_published_change_summary(self, spec: AdmissionSpec, summary: str) -> None:
        branch = spec.publish_intent.repository.branch_ref

        def published_record(relative: str) -> dict:
            return json.loads(
                git(None, "--git-dir", str(self.publish.remote), "show", f"refs/heads/{branch}:{relative}").stdout
            )

        self.assertEqual(spec.publish_intent.commit.body, summary)
        self.assertEqual(published_record("input/request.json")["change_summary"], summary)
        source = published_record("input/source.json")["metadata"]
        self.assertEqual(source["change_summary_source"], "cad")
        self.assertEqual(source["change_summary"], capture_record()["identity"]["change_summary"])
        self.assertNotEqual(source["change_summary"], summary)
        self.assertTrue(self.pull_requests.rows[0].body.startswith(summary + "\nVerification warnings ("))

    def test_default_environment_factory_runs_native_loopback_chain(self) -> None:
        snapshot = self._platform_snapshot()
        state_root = self.root / "state"
        lock_directory = state_root / "locks"
        lock_directory.mkdir(exist_ok=True)
        (lock_directory / "maintenance.lock").write_bytes(b"")
        (self.root / "endpoint-token").chmod(0o600)
        token = (self.root / "endpoint-token").read_text(encoding="utf-8")

        runtime = NativeOperationRuntime(
            state_root=self.root / "native-runtime",
            native_tool_factory=lambda: copy.deepcopy(self.native),
            executors={
                DISCOVER_KIND: DiscoverOperationExecutor(
                    backend_factory=lambda _root, _files: CountingBackend(
                    copy.deepcopy(capture_record()),
                    observation_fixture=NativeObservationFixture(
                        transform=_capture_component_closure,
                        reader_sha256=self.native["source_sha256"],
                    ),
                ),
                ),
                CAPTURE_KIND: CaptureOperationExecutor(
                    backend_factory=_qualified_backend
                ),
            },
            install_id=self.native_target.install_id,
        )
        self.addCleanup(runtime.close)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            windows_handler(runtime, token),
        )
        thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
        )
        thread.start()

        def close_server() -> None:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            if thread.is_alive():
                raise AssertionError("native operation test server did not stop")

        self.addCleanup(close_server)
        host, port = server.server_address[:2]
        client = NativeOperationClient(
            NativeOperationEndpoint(
                f"http://{host}:{port}",
                token,
                timeout=10,
            ),
            target_id=self.native_target.target_id,
        )
        tool_provider = NativeOperationToolProvider(client)
        work_root = self.root / "default-environment-work"
        work_root.mkdir()
        factory_work = self.root / "default-environment-admission"
        factory_work.mkdir()
        self.pull_requests = _PullRequests()
        environment = {
            "PLATFORM_CONFIG_SNAPSHOT": str(snapshot),
            "PLATFORM_ARTIFACT_ROOT": str(self.store.root),
            "PLATFORM_SERVICE_DSN": "postgresql://fixture",
            "PIPELINE_WORK_ROOT": str(work_root),
            "PIPELINE_CLAIM_LEASE_SECONDS": "60",
            "SOLIDWORKS_ENDPOINT_URL": f"http://{host}:{port}",
            "ENDPOINT_TOKEN_FILE": str(self.root / "endpoint-token"),
        }

        with (
            patch.object(
                ArtifactStore,
                "from_environment",
                return_value=self.store,
            ),
            patch.object(
                FactStore,
                "from_environment",
                return_value=self.facts,
            ),
            patch(
                "description_pipeline.pipeline.application."
                "_portable_executor_sha256",
                return_value=self.portable["source_sha256"],
            ),
            patch(
                "description_pipeline.runtime.tool_record",
                return_value=copy.deepcopy(self.native),
            ),
            patch(
                "description_pipeline.pipeline.stages.generate.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
            patch(
                "description_pipeline.pipeline.stages.publish.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
            patch(
                "description_pipeline.pipeline.verify.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
            patch(
                "description_pipeline.repository.publish."
                "GitHubCliPullRequestClient",
                return_value=self.pull_requests,
            ),
            patch(
                "description_pipeline.pipeline.stages.publish."
                "GitHubCliPullRequestClient",
                return_value=self.pull_requests,
            ),
        ):
            application = application_from_environment(environment)
            self.assertEqual(
                application.native_target_identity,
                self.native_target,
            )
            admitted = load_production_admission(
                application,
                self._production_request(),
                artifacts=self.store,
                platform_snapshot=snapshot,
                native_tool_provider=tool_provider,
                work_root=factory_work,
                clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
                portable_tool_provider=lambda: copy.deepcopy(self.portable),
            )
            self.assertIsNotNone(admitted)

            deadline = time.monotonic() + 10
            last_attempt = None
            while True:
                if time.monotonic() >= deadline:
                    with runtime._connect() as connection:
                        operation_ids = [
                            str(row[0])
                            for row in connection.execute(
                                "SELECT operation_id FROM native_operation "
                                "ORDER BY created_at, operation_id"
                            ).fetchall()
                        ]
                    operations = [
                        client.get_operation(operation_id)
                        for operation_id in operation_ids
                    ]
                    self.fail(
                        "native loopback chain did not settle: "
                        f"{last_attempt!r}; operations={operations!r}"
                    )
                outcome = application.execute_run(RUN_ID)
                last_attempt = outcome
                if outcome.outcome == "pending":
                    wait = StageEffectWait.from_record(
                        outcome.payload["wait"]
                    )
                    while True:
                        if time.monotonic() >= deadline:
                            self.fail(
                                "native loopback chain did not settle: "
                                f"{last_attempt!r}"
                            )
                        attempt = application.resume_stage(wait)
                        last_attempt = attempt
                        if isinstance(attempt, StagePending):
                            wait = attempt.wait
                            time.sleep(0.05)
                            continue
                        break
                    continue
                break

        self.assertEqual(outcome.outcome, "completed", repr(outcome))
        self.assertEqual(outcome.stage, "publish")
        self.assertEqual(len(self.pull_requests.rows), 1)
        with runtime._connect() as connection:
            operation_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT operation_id FROM native_operation "
                    "ORDER BY created_at, operation_id"
                ).fetchall()
            ]
        operations = [client.get_operation(operation_id) for operation_id in operation_ids]
        self.assertEqual(
            {operation.kind for operation in operations},
            {DISCOVER_KIND, CAPTURE_KIND},
        )
        self.assertEqual(
            {operation.state for operation in operations},
            {"satisfied"},
        )
        self.assertEqual(
            {operation.target_id for operation in operations},
            {self.native_target.target_id},
        )

    def test_freeze_successor_reuses_originals_and_runs_all_six_stages(self) -> None:
        parent_application = self._application()
        parent_work = self.root / "parent-factory-work"
        parent_work.mkdir()
        parent_request = self._production_request()
        parent_snapshot = self._platform_snapshot()
        with (
            patch(
                "description_pipeline.runtime.tool_record",
                return_value=copy.deepcopy(self.native),
            ),
            patch(
                "description_pipeline.pipeline.stages.generate.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
            patch(
                "description_pipeline.pipeline.stages.publish.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
        ):
            parent = load_production_admission(
                parent_application,
                parent_request,
                artifacts=self.store,
                platform_snapshot=parent_snapshot,
                native_tool_provider=lambda: copy.deepcopy(self.native),
                work_root=parent_work,
                clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
                portable_tool_provider=lambda: copy.deepcopy(self.portable),
            )
            freeze = parent_application.execute_registered_command(
                parent.entry_command.command_id
            )
        self.assertEqual(freeze.outcome, "completed")
        parent_spec = AdmissionSpec.from_record(parent.admission.request)
        parent_executor = parent_spec.executors.freeze

        shutil.rmtree(self.upload_root)
        shutil.rmtree(parent_work)
        self.assertFalse(self.upload_root.exists())
        self.assertFalse(parent_work.exists())

        write_json(
            self.rules_root / "root-000" / "budget.json",
            {
                **RECORDS["budget.json"],
                "robot": {
                    "expected_mass_kg": [0.29, 0.31],
                    "expected_extent_m": [0.35, 0.5],
                },
            },
        )
        self._advance_production_repository("successor")
        current_snapshot = self._platform_snapshot(
            generated_at="2026-10-25T01:00:00Z"
        )
        self._install_current_tools(
            source_commit="d" * 40,
            source_tree="e" * 40,
            seed="successor",
        )
        self.assertNotEqual(
            self.portable["source_sha256"],
            parent_executor,
        )

        successor = ProductionFreezeSuccessorRequest(
            run_id=SUCCESSOR_RUN_ID,
            request_id="real-six-stage-successor-request",
            parent_run_id=RUN_ID,
            main_assembly=MAIN_ASSEMBLY,
            target_hardware="nd_fixture",
            purpose="engineering",
            change_summary="Reuse sealed CAD bytes under current production inputs\nReview this successor separately",
            principal="cli:tenant:ou_successor",
        )
        successor_work = self.root / "successor-factory-work"
        successor_work.mkdir()
        application = self._application()
        with (
            patch(
                "description_pipeline.runtime.tool_record",
                return_value=copy.deepcopy(self.native),
            ),
            patch(
                "description_pipeline.pipeline.stages.generate.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
            patch(
                "description_pipeline.pipeline.stages.publish.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
        ):
            child = load_production_freeze_successor(
                application,
                successor,
                artifacts=self.store,
                platform_snapshot=current_snapshot,
                native_tool_provider=lambda: copy.deepcopy(self.native),
                work_root=successor_work,
                clock=lambda: datetime(2026, 10, 25, 1, tzinfo=UTC),
                portable_tool_provider=lambda: copy.deepcopy(self.portable),
            )
            child_spec = AdmissionSpec.from_record(child.admission.request)
            self.assertEqual(child_spec.originals, parent_spec.originals)
            self.assertEqual(
                child_spec.originals.ref.producer_tool_sha256,
                parent_executor,
            )
            self.assertNotEqual(
                child_spec.platform_config_sha256,
                parent_spec.platform_config_sha256,
            )
            for artifact in (
                child_spec.ruleset.artifact.ref,
                child_spec.configuration.ref,
                child_spec.native_tool_requirement.ref,
                child_spec.publish_intent.repository.artifact.ref,
            ):
                self.assertEqual(
                    artifact.producer_tool_sha256,
                    self.portable["source_sha256"],
                )
                self.assertNotEqual(
                    artifact.producer_tool_sha256,
                    parent_executor,
                )
            outcome = application.execute_run(SUCCESSOR_RUN_ID)

        self.assertEqual(outcome.outcome, "completed", repr(outcome))
        self.assertEqual(outcome.stage, "publish")
        self.assertEqual(len(self.pull_requests.rows), 1)
        self.assertEqual(
            self.pull_requests.rows[0].head_ref,
            child_spec.publish_intent.repository.branch_ref,
        )
        self._assert_published_change_summary(child_spec, successor.change_summary)
        self.assertNotEqual(successor.change_summary, parent_request.change_summary)
        with (
            self._connect() as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT stage, outcome FROM stage_decision "
                "WHERE run_id = %s "
                "ORDER BY array_position("
                "ARRAY['freeze','discover','capture','generate','verify','mjcf','publish'],"
                " stage)",
                (SUCCESSOR_RUN_ID,),
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
                ("mjcf", "completed"),
                ("publish", "completed"),
            ],
        )

    def test_real_chain_records_a_failure_and_stops_downstream_stages(self) -> None:
        write_json(
            self.rules_root / "root-000" / "budget.json",
            {
                "robot": {
                    "expected_mass_kg": [0.9, 1.0],
                    "expected_extent_m": [0.35, 0.5],
                }
            },
        )
        application = self._application()
        factory_work = self.root / "failure-factory-work"
        factory_work.mkdir()
        with (
            patch(
                "description_pipeline.runtime.tool_record",
                return_value=copy.deepcopy(self.native),
            ),
            patch(
                "description_pipeline.pipeline.stages.generate.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
            patch(
                "description_pipeline.pipeline.stages.publish.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
        ):
            load_production_admission(
                application,
                self._production_request(),
                artifacts=self.store,
                platform_snapshot=self._platform_snapshot(),
                native_tool_provider=lambda: copy.deepcopy(self.native),
                work_root=factory_work,
                clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
                portable_tool_provider=lambda: copy.deepcopy(self.portable),
            )
            outcome = application.execute_run(RUN_ID)

        self.assertEqual(outcome.outcome, "failed")
        self.assertEqual(outcome.stage, "verify")
        with (
            self._connect() as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT stage, outcome FROM stage_decision "
                "WHERE run_id = %s "
                "ORDER BY array_position("
                "ARRAY['freeze','discover','capture','generate','verify','mjcf','publish'],"
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
                ("verify", "failed"),
            ],
        )

    def test_real_chain_reconciles_unknown_publish_effect_after_restart(self) -> None:
        pull_requests = _UnknownPullRequests()
        application = self._application(pull_requests=pull_requests)
        factory_work = self.root / "unknown-factory-work"
        factory_work.mkdir()
        with (
            patch(
                "description_pipeline.runtime.tool_record",
                return_value=copy.deepcopy(self.native),
            ),
            patch(
                "description_pipeline.pipeline.stages.generate.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
            patch(
                "description_pipeline.pipeline.stages.publish.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
        ):
            admitted = load_production_admission(
                application,
                self._production_request(),
                artifacts=self.store,
                platform_snapshot=self._platform_snapshot(),
                native_tool_provider=lambda: copy.deepcopy(self.native),
                work_root=factory_work,
                clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
                portable_tool_provider=lambda: copy.deepcopy(self.portable),
            )
            pending = application.execute_run(RUN_ID)

        self.assertEqual(pending.outcome, "pending")
        self.assertEqual(pending.stage, "publish")
        self.assertEqual(
            pending.payload["wait"]["effect_key"],
            PR_EFFECT_KEY,
        )

        self.assertEqual(pull_requests.create_calls, 1)
        self.assertEqual(len(pull_requests.rows), 1)
        original_body = pull_requests.rows[0].body
        command = self.facts.load_stage_command(RUN_ID, "publish")
        unknown = self.facts.effect_settlement_for_key(
            command.command_id,
            PR_EFFECT_KEY,
        )
        self.assertIsNotNone(unknown)
        self.assertIs(unknown.state, EffectState.UNKNOWN)
        with (
            self._connect() as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT stage, outcome FROM stage_decision "
                "WHERE run_id = %s "
                "ORDER BY array_position("
                "ARRAY['freeze','discover','capture','generate','verify','mjcf','publish'],"
                " stage)",
                (RUN_ID,),
            )
            self.assertEqual(
                cursor.fetchall(),
                [
                    ("freeze", "completed"),
                    ("discover", "completed"),
                    ("capture", "completed"),
                    ("generate", "completed"),
                    ("verify", "completed"),
                ("mjcf", "completed"),
                ],
            )

        later = datetime(2026, 10, 25, 0, 15, tzinfo=UTC)
        self._bind_services(
            owner="real-six-stage-restart",
            now=later,
            work_suffix="-restart",
        )
        reconciler = EffectReconciliationService(
            facts=self.facts,
            artifacts=self.store,
            work_root=self.root / "restart-reconciliation",
            observer_tool_sha256=digest({"tool": "publish-read-observer"}),
            clock=lambda: later,
        )
        restarted = self._application(
            pull_requests=pull_requests,
            reconciler=reconciler,
            reconciliation_adapter=PublishPullRequestReconciliationAdapter(
                pull_requests
            ),
            clock=later,
        )
        reconciled = restarted.reconcile_effect(
            command_id=command.command_id,
            effect_key=PR_EFFECT_KEY,
        )
        self.assertIs(reconciled.settlement.state, EffectState.SATISFIED)
        with (
            patch(
                "description_pipeline.runtime.tool_record",
                return_value=copy.deepcopy(self.native),
            ),
            patch(
                "description_pipeline.pipeline.stages.generate.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
            patch(
                "description_pipeline.pipeline.stages.publish.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
        ):
            outcome = restarted.execute_run(RUN_ID)

        self.assertEqual(outcome.outcome, "completed", repr(outcome))
        self.assertEqual(outcome.stage, "publish")
        self.assertEqual(pull_requests.create_calls, 1)
        self.assertEqual(len(pull_requests.rows), 1)
        self.assertEqual(pull_requests.rows[0].body, original_body)
        self._assert_published_change_summary(
            AdmissionSpec.from_record(admitted.admission.request),
            "Exercise production intake through six real handlers",
        )
        with (
            self._connect() as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT stage, outcome FROM stage_decision "
                "WHERE run_id = %s "
                "ORDER BY array_position("
                "ARRAY['freeze','discover','capture','generate','verify','mjcf','publish'],"
                " stage)",
                (RUN_ID,),
            )
            self.assertEqual(
                cursor.fetchall(),
                [
                    ("freeze", "completed"),
                    ("discover", "completed"),
                    ("capture", "completed"),
                    ("generate", "completed"),
                    ("verify", "completed"),
                ("mjcf", "completed"),
                    ("publish", "completed"),
                ],
            )

    def test_real_chain_cancels_verify_before_its_handler_runs(self) -> None:
        application = self._application()
        factory_work = self.root / "cancel-factory-work"
        factory_work.mkdir()
        load_production_admission(
            application,
            self._production_request(),
            artifacts=self.store,
            platform_snapshot=self._platform_snapshot(),
            native_tool_provider=lambda: copy.deepcopy(self.native),
            work_root=factory_work,
            clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
            portable_tool_provider=lambda: copy.deepcopy(self.portable),
        )
        create_command = self.facts.create_command
        cancel_request = None

        def create_then_cancel(command, **kwargs):
            nonlocal cancel_request
            stored = create_command(command, **kwargs)
            if command.stage == "verify":
                cancel_request = self.facts.request_cancel(
                    target_command_id=stored.command_id,
                    cancel_executor_sha256=digest(
                        {"tool": "verify-cancel"}
                    ),
                    reason_code="operator_cancelled",
                    reason="Cancel verification before its handler starts",
                    requested_by="cli:tenant:ou_operator",
                    requested_at=datetime(
                        2026,
                        10,
                        25,
                        0,
                        0,
                        1,
                        tzinfo=UTC,
                    ),
                )
            return stored

        with (
            patch(
                "description_pipeline.runtime.tool_record",
                return_value=copy.deepcopy(self.native),
            ),
            patch(
                "description_pipeline.pipeline.stages.generate.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
            patch(
                "description_pipeline.pipeline.stages.publish.tool_record",
                return_value=copy.deepcopy(self.portable),
            ),
            patch.object(
                self.facts,
                "create_command",
                side_effect=create_then_cancel,
            ),
            patch.object(
                application.handlers["verify"],
                "execute",
                side_effect=AssertionError(
                    "A requested cancellation must run before Verify"
                ),
            ) as verify_execute,
        ):
            generated = application.execute_run(
                RUN_ID,
                through="generate",
            )
            self.assertEqual(generated.outcome, "completed")
            cancelled = application.run_stage(RUN_ID, "verify")

        self.assertEqual(cancelled.outcome, "cancelled")
        self.assertIsNotNone(cancel_request)
        verify_execute.assert_not_called()
        with (
            self._connect() as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT stage, outcome FROM stage_decision "
                "WHERE run_id = %s "
                "ORDER BY array_position("
                "ARRAY['freeze','discover','capture','generate','verify','mjcf','publish'],"
                " stage)",
                (RUN_ID,),
            )
            self.assertEqual(
                cursor.fetchall(),
                [
                    ("freeze", "completed"),
                    ("discover", "completed"),
                    ("capture", "completed"),
                    ("generate", "completed"),
                    ("verify", "cancelled"),
                ],
            )

if __name__ == "__main__":
    unittest.main()
