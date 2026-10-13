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
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


def make_snapshot(root: Path) -> Path:
    resolved = platform_config.validate_platform(platform_document(root))
    snapshot = platform_config.build_snapshot(
        resolved,
        platform_json_sha256="e" * 64,
        install_id="gen-1",
        generated_at=TIMESTAMP,
    )
    return platform_config.write_snapshot(snapshot, root / "state" / "snapshots")


def make_release(root: Path) -> Path:
    distribution = root / "distribution"
    distribution.mkdir(parents=True, exist_ok=True)
    wheel = distribution / WHEEL_NAME
    wheel.write_bytes(b"wheel-bytes")
    document = {
        "schema_version": install_record.RELEASE_SCHEMA,
        "pipeline_id": "solidworks-to-urdf",
        "version": "1.4.0",
        "source_commit": COMMIT,
        "source_sha256": SOURCE_SHA,
        "artifacts": {
            WHEEL_NAME: hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "description-1.4.0-windows-cp312-x86_64.zip": "b" * 64,
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
    }
    path = root / "endpoint-identity.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def make_dag(root: Path) -> Path:
    path = root / "solidworks_to_urdf.py"
    path.write_text("# dag fixture\n", encoding="utf-8")
    return path


def build_record(root: Path) -> dict:
    release = install_record.release_identity(json.loads((root / "distribution" / "release.json").read_text()))
    endpoint = install_record.endpoint_identity(json.loads((root / "endpoint-identity.json").read_text()))
    return install_record.build_install_record(
        install_id="gen-1",
        timestamp=TIMESTAMP,
        snapshot_path=root / "state" / "snapshots" / "gen-1.json",
        release=release,
        endpoint=endpoint,
        services=list(SERVICES),
        dag_file=root / "solidworks_to_urdf.py",
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
        self.assertEqual(record["ends"]["windows"]["release"]["role"], "native")
        self.assertEqual(record["ends"]["linux"]["release"]["role"], "portable")
        snapshot_digest = hashlib.sha256((self.root / "state" / "snapshots" / "gen-1.json").read_bytes()).hexdigest()
        self.assertEqual(record["snapshot"]["digest"], snapshot_digest)
        self.assertEqual(record["ends"]["linux"]["config_digest"], snapshot_digest)
        self.assertEqual(record["paths"]["state_root"], str(self.root / "state"))
        self.assertEqual(record["previous"], None)
        self.assertEqual(record["journal"], [])

    def test_schema_constants_match_the_package(self):
        from description_pipeline import schemas
        from description_pipeline.stages import STAGE_IDS

        self.assertEqual(install_record.INSTALL_RECORD_SCHEMA, schemas.INSTALL_RECORD_SCHEMA)
        self.assertEqual(install_record.DAG_TASK_COUNT, len(STAGE_IDS))

    def test_record_generations_are_write_once_and_the_active_pointer_replaces(self):
        record = build_record(self.root)
        state_root = self.root / "state"
        generation, active = install_record.write_install_record(record, state_root)
        again, _ = install_record.write_install_record(record, state_root)
        self.assertEqual(generation, again)
        self.assertEqual(stat.S_IMODE(generation.stat().st_mode), 0o600)
        self.assertEqual(json.loads(active.read_text(encoding="utf-8")), record)

        tampered = copy.deepcopy(record)
        tampered["timestamp"] = "2026-10-13T00:00:01Z"
        with self.assertRaises(SystemExit):
            install_record.write_install_record(tampered, state_root)

        second = copy.deepcopy(record)
        second["install_id"] = "gen-2"
        second["snapshot"]["install_id"] = "gen-2"
        second["ends"]["linux"]["install_id"] = "gen-2"
        second_generation, active = install_record.write_install_record(second, state_root)
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
                str(self.root / "solidworks_to_urdf.py"),
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
        expected_digest = hashlib.sha256(install_record.canonical(record)).hexdigest()
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
                dag_file=root / "solidworks_to_urdf.py",
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
            ("endpoint extra key", lambda i, r: i["endpoint"].__setitem__("profile", "x"), "exactly install_id"),
            ("endpoint bad digest", lambda i, r: i["endpoint"].__setitem__("config_digest", "short"), "sha256 hex"),
            (
                "snapshot without storage",
                lambda i, r: (r / "state" / "snapshots" / "gen-1.json").write_text("{}", encoding="utf-8"),
                "missing keys",
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
                                dag_file=root / "solidworks_to_urdf.py",
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
                dag_file=self.root / "solidworks_to_urdf.py",
            )
        with self.assertRaises(SystemExit):
            install_record.build_install_record(
                install_id="gen-1",
                timestamp=TIMESTAMP,
                snapshot_path=self.root / "state" / "snapshots" / "gen-1.json",
                release=release,
                endpoint=endpoint,
                services=[],
                dag_file=self.root / "solidworks_to_urdf.py",
            )
        with self.assertRaises(SystemExit):
            install_record.build_install_record(
                install_id="gen-1",
                timestamp="not-a-timestamp",
                snapshot_path=self.root / "state" / "snapshots" / "gen-1.json",
                release=release,
                endpoint=endpoint,
                services=list(SERVICES),
                dag_file=self.root / "solidworks_to_urdf.py",
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
                    str(root / "solidworks_to_urdf.py"),
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
            self.assertIn("escapes the distribution root", result.stderr)
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

    def test_no_production_task_or_path_fallback_exists(self):
        source = (OPERATOR / "install_record.py").read_text(encoding="utf-8")
        self.assertNotIn("description-v1-endpoint", source)


if __name__ == "__main__":
    unittest.main()
