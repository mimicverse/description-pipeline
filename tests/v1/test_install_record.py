"""Install-record assembly and persistence (W2 lifecycle increment 2).

The deploy-side module must stay compatible with the package-side IF-16 validator:
``description_pipeline.interface_records.validate_install_record`` must accept every assembled
record, and the D04 per-end binding (no cross-end equality requirement) must hold.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from description_pipeline import io as pipeline_io
from description_pipeline.pipeline.contracts import NativeTargetIdentity

from .contract_wheel import write_contract_wheel

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ROOT / "deploy" / "operator"


def _load_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


platform_config = _load_from_path("deploy_platform_config_ir", OPERATOR / "platform_config.py")
install_record = _load_from_path("deploy_install_record", OPERATOR / "install_record.py")

TIMESTAMP = "2026-10-13T00:00:00Z"
COMMIT = "c" * 40
SOURCE_SHA = "a" * 64
WHEEL_NAME = "mimicverse_description-1.4.0-py3-none-any.whl"
SERVICES = ["description-postgres", "description-airflow-scheduler", "description-operator-proxy"]


def native_target(install_id: str = "ep-7") -> dict:
    return NativeTargetIdentity.create(
        install_id=install_id,
        pipeline_id="solidworks-to-urdf",
    ).to_record()


def platform_document(root: Path) -> dict:
    return {
        "schema_version": platform_config.PLATFORM_SCHEMA,
        "operator": {
            "origin": "https://10.0.0.235:9443",
            "certificate_file": str(root / "secrets" / "tls.crt"),
            "private_key_file": str(root / "secrets" / "tls.key"),
        },
        "storage": {
            "state_root": str(root / "state"),
            "handoff_root": str(root / "handoffs"),
            "artifact_root": str(root / "artifacts"),
            "workspace_root": str(root / "workspaces"),
            "release_root": str(root / "releases"),
        },
        "database": {"dsn_file": str(root / "secrets" / "postgres.dsn")},
        "windows": {
            "ssh_host": "windows-m3",
            "endpoint_port": 9765,
            "token_file": str(root / "secrets" / "endpoint.token"),
            "instance": {
                "task_name": "description-e2e-endpoint",
                "package_root": "C:/description-e2e/packages",
                "output_root": "C:/description-e2e/results",
                "state_root": "C:/description-e2e/state",
            },
        },
        "auth": {
            "feishu_secret_file": str(root / "secrets" / "feishu.json"),
            "tenant_keys": ["tenant-key"],
            "admin_open_ids": [],
        },
        "targets": {"m3.0": {"repository_slug": "owner/model-repository", "base": "main"}},
        "specifications": {"record_roots": [str(root / "specifications")]},
    }


def contract_owner(root: Path):
    """The canonical owner exactly as the deployment consumes it: the wheel's closure."""

    wheel = root / "distribution" / WHEEL_NAME
    if not wheel.is_file():
        wheel.parent.mkdir(parents=True, exist_ok=True)
        write_contract_wheel(wheel)
    return platform_config.load_contract_owner(platform_config.extract_contract_closure(wheel))


def make_snapshot(root: Path) -> Path:
    resolved = platform_config.validate_platform(platform_document(root))
    snapshot = platform_config.build_snapshot(
        resolved,
        platform_json_sha256="e" * 64,
        install_id="gen-1",
        generated_at=TIMESTAMP,
    )
    return platform_config.write_snapshot(snapshot, root / "state" / "snapshots", contract_owner(root))


def make_release(root: Path) -> Path:
    distribution = root / "distribution"
    distribution.mkdir(parents=True, exist_ok=True)
    wheel = distribution / WHEEL_NAME
    write_contract_wheel(wheel)
    dag = root / install_record.DAG_MEMBER
    dag.parent.mkdir(parents=True, exist_ok=True)
    if not dag.exists():
        dag.write_text("# dag fixture\n", encoding="utf-8")
    document = {
        "schema_version": install_record.RELEASE_SCHEMA,
        "pipeline_id": "solidworks-to-urdf",
        "version": "1.4.0",
        "source_commit": COMMIT,
        "source_tree": "d" * 40,
        "source_sha256": SOURCE_SHA,
        "artifacts": {
            WHEEL_NAME: hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "description-1.4.0-windows-cp312-x86_64.zip": "b" * 64,
        },
        "deployment_files": {
            install_record.DAG_MEMBER: hashlib.sha256(dag.read_bytes()).hexdigest(),
        },
    }
    path = distribution / "release.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def make_endpoint_identity(root: Path) -> Path:
    document = {
        "install_id": "ep-7",
        "config_digest": "d" * 64,
        "release": {"version": "1.4.0", "source_commit": COMMIT, "source_sha256": SOURCE_SHA, "role": "native"},
        "target_identity": native_target(),
    }
    path = root / "endpoint-identity.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def make_dag(root: Path) -> Path:
    path = root / install_record.DAG_MEMBER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# dag fixture\n", encoding="utf-8")
    return path


def build_record(root: Path) -> dict:
    release_document = json.loads((root / "distribution" / "release.json").read_text())
    release = install_record.release_identity(release_document)
    endpoint = install_record.endpoint_identity(json.loads((root / "endpoint-identity.json").read_text()))
    return install_record.build_install_record(
        install_id="gen-1",
        timestamp=TIMESTAMP,
        snapshot_path=root / "state" / "snapshots" / "gen-1.json",
        release=release,
        endpoint=endpoint,
        services=list(SERVICES),
        dag_file=root / install_record.DAG_MEMBER,
        deployment_root=root,
        release_dag_sha256=install_record.release_dag_digest(release_document),
    )


class InstallRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="install-record-")
        self.root = Path(self._temporary.name)
        make_snapshot(self.root)
        make_release(self.root)
        make_endpoint_identity(self.root)
        make_dag(self.root)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_assembled_record_passes_the_frozen_validator(self):
        from description_pipeline import interface_records as ir

        record = build_record(self.root)
        ir.validate_install_record(record)
        self.assertEqual(record["ends"]["linux"]["install_id"], "gen-1")
        self.assertEqual(record["ends"]["windows"]["install_id"], "ep-7")
        self.assertEqual(
            record["ends"]["windows"]["target_identity"],
            native_target(),
        )
        self.assertEqual(record["ends"]["windows"]["release"]["role"], "native")
        self.assertEqual(record["ends"]["linux"]["release"]["role"], "portable")
        snapshot_digest = hashlib.sha256((self.root / "state" / "snapshots" / "gen-1.json").read_bytes()).hexdigest()
        self.assertEqual(record["snapshot"]["digest"], snapshot_digest)
        self.assertEqual(record["ends"]["linux"]["config_digest"], snapshot_digest)
        self.assertEqual(record["paths"]["state_root"], str(self.root / "state"))
        self.assertEqual(record["previous"], None)
        self.assertEqual(record["journal"], [])

    def test_endpoint_target_identity_is_canonical_and_bound_to_runtime_install(self):
        base = json.loads((self.root / "endpoint-identity.json").read_text(encoding="utf-8"))
        variants = []
        wrong_install = copy.deepcopy(base)
        wrong_install["target_identity"]["install_id"] = "ep-other"
        variants.append(wrong_install)
        wrong_pipeline = copy.deepcopy(base)
        wrong_pipeline["target_identity"]["pipeline_id"] = "other-pipeline"
        variants.append(wrong_pipeline)
        wrong_digest = copy.deepcopy(base)
        wrong_digest["target_identity"]["target_id"] = "0" * 64
        variants.append(wrong_digest)
        for document in variants:
            with self.subTest(document=document["target_identity"]), self.assertRaises(SystemExit):
                install_record.endpoint_identity(document)

    def test_endpoint_identity_cli_copies_the_registered_target_instead_of_current_health(self):
        from .test_interface_records import health

        record = build_record(self.root)
        record_path = self.root / "state" / "install-record.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        health_path = self.root / "endpoint-health.json"
        matching = health()
        matching["install"] = {"config_digest": "d" * 64, "install_id": "ep-7"}
        matching["endpoint"] = {"port": 9765}
        health_path.write_text(json.dumps(matching), encoding="utf-8")
        out = self.root / "verified-endpoint-identity.json"
        result = install_record.main(
            [
                "endpoint-identity",
                "--health",
                str(health_path),
                "--release-file",
                str(self.root / "distribution" / "release.json"),
                "--record",
                str(record_path),
                "--out",
                str(out),
            ]
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            json.loads(out.read_text(encoding="utf-8"))["target_identity"],
            record["ends"]["windows"]["target_identity"],
        )
        forged = matching
        forged["install"]["install_id"] = "ep-other"
        health_path.write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "does not match the active record"):
            install_record.main(
                [
                    "endpoint-identity",
                    "--health",
                    str(health_path),
                    "--release-file",
                    str(self.root / "distribution" / "release.json"),
                    "--record",
                    str(record_path),
                    "--out",
                    str(out),
                ]
            )

    def test_dag_must_be_the_current_release_member(self):
        record = build_record(self.root)
        expected = (self.root / install_record.DAG_MEMBER).resolve()
        self.assertEqual(Path(record["dag"]["path"]), expected)

        release_document = json.loads(
            (self.root / "distribution" / "release.json").read_text(encoding="utf-8")
        )
        release = install_record.release_identity(release_document)
        endpoint = install_record.endpoint_identity(
            json.loads((self.root / "endpoint-identity.json").read_text(encoding="utf-8"))
        )
        historical = self.root / "historical" / install_record.DAG_MEMBER
        historical.parent.mkdir(parents=True)
        historical.write_bytes(expected.read_bytes())
        dag_sha256 = hashlib.sha256(expected.read_bytes()).hexdigest()
        with self.assertRaisesRegex(SystemExit, "outside the current deployment root"):
            install_record.build_install_record(
                install_id="gen-1",
                timestamp=TIMESTAMP,
                snapshot_path=self.root / "state" / "snapshots" / "gen-1.json",
                release=release,
                endpoint=endpoint,
                services=list(SERVICES),
                dag_file=historical,
                deployment_root=self.root,
                release_dag_sha256=dag_sha256,
            )
        with self.assertRaisesRegex(SystemExit, "does not match the release deployment member"):
            install_record.build_install_record(
                install_id="gen-1",
                timestamp=TIMESTAMP,
                snapshot_path=self.root / "state" / "snapshots" / "gen-1.json",
                release=release,
                endpoint=endpoint,
                services=list(SERVICES),
                dag_file=expected,
                deployment_root=self.root,
                release_dag_sha256="0" * 64,
            )

    def test_schema_constants_match_the_package(self):
        from description_pipeline import schemas
        from description_pipeline.stages import STAGE_IDS

        self.assertEqual(install_record.INSTALL_RECORD_SCHEMA, schemas.INSTALL_RECORD_SCHEMA)
        self.assertEqual(install_record.DAG_TASK_COUNT, len(STAGE_IDS))

    def test_record_generations_are_write_once_and_the_active_pointer_replaces(self):
        record = build_record(self.root)
        state_root = self.root / "state"
        owner = contract_owner(self.root)
        generation, active = install_record.write_install_record(record, state_root, owner)
        again, _ = install_record.write_install_record(record, state_root, owner)
        self.assertEqual(generation, again)
        self.assertEqual(stat.S_IMODE(generation.stat().st_mode), 0o600)
        self.assertEqual(json.loads(active.read_text(encoding="utf-8")), record)

        tampered = copy.deepcopy(record)
        tampered["timestamp"] = "2026-10-13T00:00:01Z"
        with self.assertRaises(SystemExit):
            install_record.write_install_record(tampered, state_root, owner)

        second = copy.deepcopy(record)
        second["install_id"] = "gen-2"
        second["snapshot"]["install_id"] = "gen-2"
        second["ends"]["linux"]["install_id"] = "gen-2"
        second_generation, active = install_record.write_install_record(second, state_root, owner)
        self.assertEqual(second_generation.name, "gen-2.json")
        self.assertTrue(generation.is_file(), "older generations must be retained")
        self.assertEqual(json.loads(active.read_text(encoding="utf-8"))["install_id"], "gen-2")

    def test_build_cli_writes_generation_and_active_record(self):
        result = subprocess.run(
            [
                sys.executable,
                str(OPERATOR / "install_record.py"),
                "build",
                "--state-root",
                str(self.root / "state"),
                "--install-id",
                "gen-1",
                "--timestamp",
                TIMESTAMP,
                "--snapshot",
                str(self.root / "state" / "snapshots" / "gen-1.json"),
                "--release-file",
                str(self.root / "distribution" / "release.json"),
                "--endpoint-identity",
                str(self.root / "endpoint-identity.json"),
                "--services",
                ",".join(SERVICES),
                "--dag",
                str(self.root / install_record.DAG_MEMBER),
                "--deployment-root",
                str(self.root),
                "--wheel",
                str(self.root / "distribution" / WHEEL_NAME),
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
        generation = Path(summary["generation"])
        active = Path(summary["active"])
        self.assertTrue(generation.is_file())
        record = json.loads(active.read_text(encoding="utf-8"))
        self.assertEqual(record, json.loads(generation.read_text(encoding="utf-8")))
        self.assertEqual(summary["verified_artifacts"], "1")
        expected_digest = hashlib.sha256(pipeline_io.canonical(record)).hexdigest()
        self.assertEqual(summary["record_digest"], expected_digest)

    def assert_rejected(self, mutate, needle: str) -> None:
        root = self.root
        record_inputs = {
            "release": json.loads((root / "distribution" / "release.json").read_text(encoding="utf-8")),
            "endpoint": json.loads((root / "endpoint-identity.json").read_text(encoding="utf-8")),
        }
        mutate(record_inputs, root)
        with self.assertRaises(SystemExit) as raised:
            release = install_record.release_identity(record_inputs["release"])
            install_record.verify_artifacts(root / "distribution", release["artifacts"])
            endpoint = install_record.endpoint_identity(record_inputs["endpoint"])
            install_record.build_install_record(
                install_id="gen-1",
                timestamp=TIMESTAMP,
                snapshot_path=root / "state" / "snapshots" / "gen-1.json",
                release=release,
                endpoint=endpoint,
                services=list(SERVICES),
                dag_file=root / install_record.DAG_MEMBER,
                deployment_root=root,
                release_dag_sha256=hashlib.sha256(
                    (root / install_record.DAG_MEMBER).read_bytes()
                ).hexdigest(),
            )
        self.assertIn(needle, str(raised.exception))

    def test_input_negatives(self):
        cases = (
            ("release schema version", lambda i, r: i["release"].__setitem__("schema_version", "x"), "schema_version"),
            ("release short commit", lambda i, r: i["release"].__setitem__("source_commit", "c" * 8), "malformed"),
            ("release empty artifacts", lambda i, r: i["release"].__setitem__("artifacts", {}), "non-empty object"),
            (
                "artifact digest mismatch",
                lambda i, r: i["release"]["artifacts"].__setitem__(WHEEL_NAME, "f" * 64),
                "does not match release.json",
            ),
            (
                "no release artifact present",
                lambda i, r: i["release"].__setitem__("artifacts", {"only-missing.zip": "b" * 64}),
                "no release artifact is present",
            ),
            ("endpoint role", lambda i, r: i["endpoint"]["release"].__setitem__("role", "portable"), "must be native"),
            (
                "endpoint triple mismatch",
                lambda i, r: i["endpoint"]["release"].__setitem__("source_commit", "d" * 40),
                "identity mismatch",
            ),
            (
                "endpoint extra key",
                lambda i, r: i["endpoint"].__setitem__("profile", "x"),
                "exactly install_id, config_digest, release, target_identity",
            ),
            ("endpoint bad digest", lambda i, r: i["endpoint"].__setitem__("config_digest", "short"), "sha256 hex"),
            (
                "snapshot without storage",
                lambda i, r: (r / "state" / "snapshots" / "gen-1.json").write_text("{}", encoding="utf-8"),
                "schema_version",
            ),
            ("bad install id", lambda i, r: None, "install_id is malformed"),
        )
        for label, mutate, needle in cases:
            with self.subTest(label):
                if label == "bad install id":
                    with tempfile.TemporaryDirectory(prefix="install-record-bad-id-") as temporary:
                        root = Path(temporary)
                        make_snapshot(root)
                        make_release(root)
                        make_endpoint_identity(root)
                        make_dag(root)
                        release = install_record.release_identity(
                            json.loads((root / "distribution" / "release.json").read_text())
                        )
                        endpoint = install_record.endpoint_identity(
                            json.loads((root / "endpoint-identity.json").read_text())
                        )
                        with self.assertRaises(SystemExit) as raised:
                            install_record.build_install_record(
                                install_id="-bad",
                                timestamp=TIMESTAMP,
                                snapshot_path=root / "state" / "snapshots" / "gen-1.json",
                                release=release,
                                endpoint=endpoint,
                                services=list(SERVICES),
                                dag_file=root / install_record.DAG_MEMBER,
                                deployment_root=root,
                                release_dag_sha256=hashlib.sha256(
                                    (root / install_record.DAG_MEMBER).read_bytes()
                                ).hexdigest(),
                            )
                        self.assertIn(needle, str(raised.exception))
                    continue
                self.assert_rejected(mutate, needle)

    def test_missing_inputs_fail_closed(self):
        record_inputs = install_record._load_json
        self.assertTrue(callable(record_inputs))
        with self.assertRaises(SystemExit):
            install_record._load_json(self.root / "missing.json", "release file")
        release = install_record.release_identity(json.loads((self.root / "distribution" / "release.json").read_text()))
        endpoint = install_record.endpoint_identity(json.loads((self.root / "endpoint-identity.json").read_text()))
        with self.assertRaises(SystemExit):
            install_record.build_install_record(
                install_id="gen-1",
                timestamp=TIMESTAMP,
                snapshot_path=self.root / "state" / "snapshots" / "missing.json",
                release=release,
                endpoint=endpoint,
                services=list(SERVICES),
                dag_file=self.root / install_record.DAG_MEMBER,
                deployment_root=self.root,
                release_dag_sha256=hashlib.sha256(
                    (self.root / install_record.DAG_MEMBER).read_bytes()
                ).hexdigest(),
            )
        with self.assertRaises(SystemExit):
            install_record.build_install_record(
                install_id="gen-1",
                timestamp=TIMESTAMP,
                snapshot_path=self.root / "state" / "snapshots" / "gen-1.json",
                release=release,
                endpoint=endpoint,
                services=[],
                dag_file=self.root / install_record.DAG_MEMBER,
                deployment_root=self.root,
                release_dag_sha256=hashlib.sha256(
                    (self.root / install_record.DAG_MEMBER).read_bytes()
                ).hexdigest(),
            )
        with self.assertRaises(SystemExit):
            install_record.build_install_record(
                install_id="gen-1",
                timestamp="not-a-timestamp",
                snapshot_path=self.root / "state" / "snapshots" / "gen-1.json",
                release=release,
                endpoint=endpoint,
                services=list(SERVICES),
                dag_file=self.root / install_record.DAG_MEMBER,
                deployment_root=self.root,
                release_dag_sha256=hashlib.sha256(
                    (self.root / install_record.DAG_MEMBER).read_bytes()
                ).hexdigest(),
            )

    def test_review_bindings_reject_without_writing(self):
        def run_cli(root: Path, *, install_id: str, snapshot: Path, release: Path) -> subprocess.CompletedProcess:
            return subprocess.run(
                [
                    sys.executable,
                    str(OPERATOR / "install_record.py"),
                    "build",
                    "--state-root",
                    str(root / "state"),
                    "--install-id",
                    install_id,
                    "--timestamp",
                    TIMESTAMP,
                    "--snapshot",
                    str(snapshot),
                    "--release-file",
                    str(release),
                    "--endpoint-identity",
                    str(root / "endpoint-identity.json"),
                    "--services",
                    ",".join(SERVICES),
                    "--dag",
                    str(root / install_record.DAG_MEMBER),
                    "--deployment-root",
                    str(root),
                    "--wheel",
                    str(root / "distribution" / WHEEL_NAME),
                ],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
            )

        def assert_no_record(root: Path, result: subprocess.CompletedProcess) -> None:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            state = root / "state"
            self.assertFalse((state / "install-record.json").exists(), "active record must not be written")
            self.assertFalse((state / "install-records").exists(), "generation files must not be written")

        with tempfile.TemporaryDirectory(prefix="install-record-binding-") as temporary:
            root = Path(temporary)
            snapshot = make_snapshot(root)
            release = make_release(root)
            make_endpoint_identity(root)
            make_dag(root)
            result = run_cli(root, install_id="gen-2", snapshot=snapshot, release=release)
            self.assertIn("generation mismatch", result.stderr)
            assert_no_record(root, result)

        with tempfile.TemporaryDirectory(prefix="install-record-binding-") as temporary:
            root = Path(temporary)
            snapshot = make_snapshot(root)
            release = make_release(root)
            make_endpoint_identity(root)
            make_dag(root)
            document = json.loads(snapshot.read_text(encoding="utf-8"))
            document["schema_version"] = "review.foreign-snapshot/v99"
            snapshot.write_text(json.dumps(document), encoding="utf-8")
            result = run_cli(root, install_id="gen-1", snapshot=snapshot, release=release)
            self.assertIn("schema_version", result.stderr)
            assert_no_record(root, result)

        with tempfile.TemporaryDirectory(prefix="install-record-binding-") as temporary:
            root = Path(temporary)
            snapshot = make_snapshot(root)
            release = make_release(root)
            make_endpoint_identity(root)
            make_dag(root)
            outsider = root / "outsider.bin"
            outsider.write_bytes(b"outside-distribution-controlled-by-review")
            document = json.loads(release.read_text(encoding="utf-8"))
            document["artifacts"] = {"../outsider.bin": hashlib.sha256(outsider.read_bytes()).hexdigest()}
            release.write_text(json.dumps(document), encoding="utf-8")
            result = run_cli(root, install_id="gen-1", snapshot=snapshot, release=release)
            self.assertIn("escapes its root", result.stderr)
            assert_no_record(root, result)

        with tempfile.TemporaryDirectory(prefix="install-record-binding-") as temporary:
            root = Path(temporary)
            snapshot = make_snapshot(root)
            release = make_release(root)
            make_endpoint_identity(root)
            make_dag(root)
            result = run_cli(root, install_id="gen-1", snapshot=snapshot, release=release)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "state" / "install-record.json").is_file())

    def _check(self, *extra: str, identity: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(OPERATOR / "install_record.py"),
                "check",
                "--release-file",
                str(self.root / "distribution" / "release.json"),
                "--endpoint-identity",
                str(identity or (self.root / "endpoint-identity.json")),
                *extra,
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )

    def test_check_binds_endpoint_triple_and_the_consumed_wheel(self):
        tree = str(self.root / "distribution")
        result = self._check("--tree", tree)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check=ok", result.stdout)

        rogue = self.root / "distribution" / "rogue-extra.whl"
        rogue.write_bytes(b"extra")
        result = self._check("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one", result.stderr)
        rogue.unlink()

        base = json.loads((self.root / "endpoint-identity.json").read_text(encoding="utf-8"))
        variant = self.root / "endpoint-identity-variant.json"
        for key, value in (("version", "9.9.9"), ("source_commit", "e" * 40), ("source_sha256", "e" * 64)):
            document = json.loads(json.dumps(base))
            document["release"][key] = value
            variant.write_text(json.dumps(document), encoding="utf-8")
            result = self._check("--tree", tree, identity=variant)
            self.assertNotEqual(result.returncode, 0, key)
            self.assertIn("identity mismatch", result.stderr)
        variant.unlink()

        rogue_same_name = self.root / "unrelated" / WHEEL_NAME
        rogue_same_name.parent.mkdir()
        rogue_same_name.write_bytes(b"UNINDEXED-ROGUE-WHEEL")
        result = self._check("--wheel", str(rogue_same_name))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match release.json", result.stderr)

        unknown = self.root / "unrelated" / "other.whl"
        unknown.write_bytes(b"other")
        result = self._check("--wheel", str(unknown))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a release artifact", result.stderr)

        correct = self.root / "distribution" / WHEEL_NAME
        result = self._check("--wheel", str(correct))
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlink support")
    def test_terminal_symlink_artifact_rejected_without_writing(self):
        with tempfile.TemporaryDirectory(prefix="install-record-terminal-link-") as temporary:
            root = Path(temporary)
            snapshot = make_snapshot(root)
            release = make_release(root)
            make_endpoint_identity(root)
            make_dag(root)
            distribution = root / "distribution"
            alias = distribution / "alias.whl"
            alias.symlink_to(distribution / WHEEL_NAME)
            document = json.loads(release.read_text(encoding="utf-8"))
            document["artifacts"] = {alias.name: hashlib.sha256((distribution / WHEEL_NAME).read_bytes()).hexdigest()}
            release.write_text(json.dumps(document), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(OPERATOR / "install_record.py"),
                    "build",
                    "--state-root",
                    str(root / "state"),
                    "--install-id",
                    "gen-1",
                    "--timestamp",
                    TIMESTAMP,
                    "--snapshot",
                    str(snapshot),
                    "--release-file",
                    str(release),
                    "--endpoint-identity",
                    str(root / "endpoint-identity.json"),
                    "--services",
                    ",".join(SERVICES),
                    "--dag",
                    str(root / install_record.DAG_MEMBER),
                    "--deployment-root",
                    str(root),
                ],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
            )
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("crosses a link", result.stderr)
            self.assertFalse((root / "state" / "install-record.json").exists())
            self.assertFalse((root / "state" / "install-records").exists())

    def test_no_production_task_or_path_fallback_exists(self):
        source = (OPERATOR / "install_record.py").read_text(encoding="utf-8")
        self.assertNotIn("description-v1-endpoint", source)

    def test_promote_binding_carries_previous_and_repoint_activates_it(self):
        from description_pipeline import interface_records as ir

        record = build_record(self.root)
        state_root = self.root / "state"
        owner = contract_owner(self.root)
        generation, _ = install_record.write_install_record(record, state_root, owner)
        release = install_record.release_identity(
            json.loads((self.root / "distribution" / "release.json").read_text(encoding="utf-8"))
        )
        endpoint = install_record.endpoint_identity(
            json.loads((self.root / "endpoint-identity.json").read_text(encoding="utf-8"))
        )
        snapshot = json.loads((state_root / "snapshots" / "gen-1.json").read_text(encoding="utf-8"))
        snapshot["install_id"] = "gen-2"
        second_snapshot = state_root / "snapshots" / "gen-2.json"
        second_snapshot.write_text(json.dumps(snapshot), encoding="utf-8")
        promoted = install_record.build_install_record(
            install_id="gen-2",
            timestamp=TIMESTAMP,
            snapshot_path=second_snapshot,
            release=release,
            endpoint=endpoint,
            services=list(SERVICES),
            dag_file=self.root / install_record.DAG_MEMBER,
            deployment_root=self.root,
            release_dag_sha256=hashlib.sha256(
                (self.root / install_record.DAG_MEMBER).read_bytes()
            ).hexdigest(),
            previous=install_record.previous_from_record(record, generation),
            journal=[
                {
                    "verb": "promote",
                    "step": "close-admission",
                    "status": "completed",
                    "at": TIMESTAMP,
                    "evidence": ["promotions/gen-2/receipt.json"],
                }
            ],
        )
        ir.validate_install_record(promoted, previous_required=True)
        self.assertEqual(promoted["previous"]["record"], str(generation))
        self.assertEqual(promoted["journal"][0]["seq"], 1)
        restored = install_record.repoint_active(state_root, generation)
        self.assertEqual(restored["install_id"], "gen-1")
        active = json.loads((state_root / "install-record.json").read_text(encoding="utf-8"))
        self.assertEqual(active["install_id"], "gen-1")
        self.assertEqual(stat.S_IMODE((state_root / "install-record.json").stat().st_mode), 0o600)
        self.assertFalse((state_root / "install-record.json.repoint.tmp").exists())

    def test_verify_running_is_the_full_reader_without_the_config_term(self):
        record = build_record(self.root)
        state_root = self.root / "state"
        owner = contract_owner(self.root)
        install_record.write_install_record(record, state_root, owner)
        self.assertEqual(install_record.verify_running_generation(state_root)["install_id"], "gen-1")

        # A consistent but non-JSON snapshot (record digest updated to match) is still refused.
        junk = b"not-json"
        (state_root / "snapshots" / "gen-1.json").write_bytes(junk)
        stored = json.loads((state_root / "install-record.json").read_text(encoding="utf-8"))
        stored["snapshot"]["digest"] = hashlib.sha256(junk).hexdigest()
        (state_root / "install-record.json").write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(SystemExit):
            install_record.verify_running_generation(state_root)

        # A snapshot belonging to another state root is refused as well.
        resolved = platform_config.validate_platform(platform_document(self.root))
        snapshot = platform_config.build_snapshot(
            resolved,
            platform_json_sha256="e" * 64,
            install_id="gen-1",
            generated_at=TIMESTAMP,
        )
        snapshot["resolved"]["storage"]["state_root"] = "/elsewhere/state"
        payload = json.dumps(snapshot).encode("utf-8")
        (state_root / "snapshots" / "gen-1.json").write_bytes(payload)
        stored = json.loads((state_root / "install-record.json").read_text(encoding="utf-8"))
        stored["snapshot"]["digest"] = hashlib.sha256(payload).hexdigest()
        (state_root / "install-record.json").write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(SystemExit):
            install_record.verify_running_generation(state_root)

    def test_if11_connection_identity_consumers_are_canonical_and_strict(self):
        from description_pipeline import interface_records as ir

        record = build_record(self.root)
        state_root = self.root / "state"
        snapshot_path = state_root / "snapshots" / "gen-1.json"
        reference = {
            "schema_version": ir.CONFIG_SNAPSHOT_SCHEMA,
            "path": str(snapshot_path),
            "sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        }
        identity = ir.validate_connection_config_identity(
            {"host": "", "port": None, "password": "", "extra": {"config_identity": reference}}
        )
        payload = ir.check_config_identity_file(identity, allowed_root=str(state_root))
        ir.verify_config_identity_digest(payload, identity)
        ir.require_active_config_identity(identity, record)

        # A surviving route copy and a divergent digest are refused by the same owners.
        with self.assertRaises(ir.InterfaceRecordError):
            ir.validate_connection_config_identity(
                {"host": "127.0.0.1", "port": None, "password": "", "extra": {"config_identity": reference}}
            )
        with self.assertRaises(ir.InterfaceRecordError):
            ir.validate_connection_config_identity(
                {
                    "host": "",
                    "port": None,
                    "password": "",
                    "extra": {"config_identity": reference, "repositories": {}},
                }
            )
        with self.assertRaises(ir.InterfaceRecordError):
            ir.require_active_config_identity({**reference, "sha256": "0" * 64}, record)
        with self.assertRaises(ir.InterfaceRecordError):
            ir.check_config_identity_file(identity, allowed_root=str(self.root / "distribution"))

    def test_verify_active_consumes_the_canonical_validator(self):
        with tempfile.TemporaryDirectory(prefix="verify-active-") as temporary:
            root = Path(temporary)
            config_path = root / "platform.json"
            config_path.write_text(json.dumps(platform_document(root)), encoding="utf-8")
            resolved = platform_config.validate_platform(json.loads(config_path.read_text(encoding="utf-8")))
            snapshot = platform_config.build_snapshot(
                resolved,
                platform_json_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
                install_id="gen-1",
                generated_at=TIMESTAMP,
            )
            owner = contract_owner(root)
            snapshot_path = platform_config.write_snapshot(snapshot, root / "state" / "snapshots", owner)
            make_release(root)
            make_endpoint_identity(root)
            dag = make_dag(root)
            release = install_record.release_identity(
                json.loads((root / "distribution" / "release.json").read_text(encoding="utf-8"))
            )
            endpoint = install_record.endpoint_identity(
                json.loads((root / "endpoint-identity.json").read_text(encoding="utf-8"))
            )
            record = install_record.build_install_record(
                install_id="gen-1",
                timestamp=TIMESTAMP,
                snapshot_path=snapshot_path,
                release=release,
                endpoint=endpoint,
                services=list(SERVICES),
                dag_file=dag,
                deployment_root=root,
                release_dag_sha256=hashlib.sha256(dag.read_bytes()).hexdigest(),
            )
            install_record.write_install_record(record, root / "state", owner)
            self.assertNotIn("source_tree", record["release"])
            old_manifest = root / "distribution" / "release.json"
            old_document = json.loads(old_manifest.read_text())
            old_document.pop("source_tree")
            old_manifest.write_text(json.dumps(old_document))
            verified = install_record.verify_active_generation(config_path, root / "state")
            self.assertEqual(verified["install_id"], "gen-1")

            record_path = root / "state" / "install-record.json"
            broken = json.loads(record_path.read_text(encoding="utf-8"))
            broken["previous"] = {"release": {}, "config": {"path": "/x", "digest": "a" * 64}, "record": "/y"}
            record_path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(SystemExit) as raised:
                install_record.verify_active_generation(config_path, root / "state")
            self.assertIn("install record invalid", str(raised.exception))

            broken["previous"] = None
            broken["timestamp"] = "2026-99-99T00:00:00Z"
            record_path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(SystemExit) as raised:
                install_record.verify_active_generation(config_path, root / "state")
            self.assertIn("install record invalid", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
