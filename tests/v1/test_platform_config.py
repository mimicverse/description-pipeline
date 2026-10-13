"""platform.json validation and config-snapshot rendering (W2 lifecycle increment 1).

The deploy-side module must stay byte-compatible with the package-side validators and the
canonical serialization: the parity tests here import ``description_pipeline.interface_records``
and ``description_pipeline.io`` on purpose.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ROOT / "deploy" / "operator"
PLATFORM_CONFIG = OPERATOR / "platform_config.py"
RENDER_OPERATOR = OPERATOR / "render_operator.py"


def _load_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


platform_config = _load_from_path("deploy_platform_config", PLATFORM_CONFIG)


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


VALID_ROOT = Path("/srv/description-e2e")


def run_cli(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PLATFORM_CONFIG), *arguments],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )


class ValidationTests(unittest.TestCase):
    def assert_rejected(self, mutate, needle: str) -> None:
        document = platform_document(VALID_ROOT)
        platform_config.validate_platform(document)
        candidate = copy.deepcopy(document)
        mutate(candidate)
        with self.assertRaises(SystemExit) as raised:
            platform_config.validate_platform(candidate)
        self.assertIn(needle, str(raised.exception))

    def test_positive_resolves_with_defaults_and_derived_values(self):
        resolved = platform_config.validate_platform(platform_document(VALID_ROOT))
        self.assertEqual(resolved["schema_version"], platform_config.PLATFORM_SCHEMA)
        self.assertEqual(
            resolved["windows"]["instance"],
            {
                "task_name": "description-e2e-endpoint",
                "package_root": "C:/description-e2e/packages",
                "output_root": "C:/description-e2e/results",
                "state_root": "C:/description-e2e/state",
            },
        )
        self.assertEqual(resolved["windows"]["tunnel_loopback_port"], platform_config.DEFAULT_TUNNEL_LOOPBACK_PORT)
        self.assertEqual(resolved["execution"], {"linux_concurrency": 1})
        self.assertEqual(resolved["retention"], {"unreferenced_evidence_days": None})
        self.assertEqual(resolved["derived"]["store_root"], "/srv/description-e2e/state/runs")
        self.assertEqual(
            resolved["derived"]["repositories"],
            {"owner/model-repository": "/srv/description-e2e/workspaces/model-repository"},
        )
        self.assertEqual(resolved["derived"]["discovery"]["record_roots"], resolved["specifications"]["record_roots"])

    def test_schema_constants_and_defaults_match_the_package(self):
        from description_pipeline import schemas

        render_operator = _load_from_path("deploy_render_operator_constants", RENDER_OPERATOR)
        self.assertEqual(platform_config.PLATFORM_SCHEMA, schemas.PLATFORM_CONFIG_SCHEMA)
        self.assertEqual(platform_config.SNAPSHOT_SCHEMA, schemas.CONFIG_SNAPSHOT_SCHEMA)
        self.assertEqual(
            platform_config.DEFAULT_TUNNEL_LOOPBACK_PORT,
            render_operator.PORT_DEFAULTS["TUNNEL_LOOPBACK_PORT"],
        )

    def test_no_production_task_or_path_fallback_exists(self):
        source = PLATFORM_CONFIG.read_text(encoding="utf-8")
        self.assertNotIn("description-v1-endpoint", source)

    def test_single_variable_negatives(self):
        cases = (
            ("unknown top-level key", lambda d: d.__setitem__("extra", 1), "unknown keys"),
            ("secret key in operator", lambda d: d["operator"].__setitem__("token", "x"), "unknown keys"),
            (
                "wrong schema version",
                lambda d: d.__setitem__("schema_version", "solidworks-to-urdf.platform/v2"),
                "schema_version",
            ),
            (
                "relative certificate path",
                lambda d: d["operator"].__setitem__("certificate_file", "secrets/tls.crt"),
                "absolute path",
            ),
            (
                "traversal in state root",
                lambda d: d["storage"].__setitem__("state_root", "/srv/description-e2e/state/../x"),
                "absolute path",
            ),
            ("missing targets", lambda d: d.pop("targets"), "missing keys"),
            ("empty targets", lambda d: d.__setitem__("targets", {}), "non-empty object"),
            ("target unknown key", lambda d: d["targets"]["m3.0"].__setitem__("bogus", 1), "unknown keys"),
            ("target without base", lambda d: d["targets"]["m3.0"].pop("base"), "missing keys"),
            ("base starting with dash", lambda d: d["targets"]["m3.0"].__setitem__("base", "-main"), "malformed"),
            ("base with traversal", lambda d: d["targets"]["m3.0"].__setitem__("base", "feature/../x"), "malformed"),
            (
                "slug without owner",
                lambda d: d["targets"]["m3.0"].__setitem__("repository_slug", "model-repository"),
                "malformed",
            ),
            (
                "distinct repositories share one checkout",
                lambda d: d["targets"].__setitem__(
                    "m4.0", {"repository_slug": "another-owner/model-repository", "base": "main"}
                ),
                "share one checkout",
            ),
            (
                "overlapping storage roots",
                lambda d: d["storage"].__setitem__("artifact_root", d["storage"]["state_root"]),
                "must not overlap",
            ),
            (
                "release root inside state root",
                lambda d: d["storage"].__setitem__("release_root", "/srv/description-e2e/state/releases"),
                "must not overlap",
            ),
            ("empty record roots", lambda d: d["specifications"].__setitem__("record_roots", []), "must not be empty"),
            (
                "record root equals handoff root",
                lambda d: d["specifications"].__setitem__("record_roots", ["/srv/description-e2e/handoffs"]),
                "must not overlap",
            ),
            ("malformed ssh host", lambda d: d["windows"].__setitem__("ssh_host", "bad host"), "malformed"),
            ("string endpoint port", lambda d: d["windows"].__setitem__("endpoint_port", "9765"), "TCP port"),
            ("out-of-range endpoint port", lambda d: d["windows"].__setitem__("endpoint_port", 70000), "TCP port"),
            (
                "origin collides with tunnel port",
                lambda d: d["operator"].__setitem__("origin", "https://10.0.0.235:18765"),
                "must not collide",
            ),
            (
                "origin port zero",
                lambda d: d["operator"].__setitem__("origin", "https://10.0.0.235:0"),
                "TCP port",
            ),
            (
                "origin port too large",
                lambda d: d["operator"].__setitem__("origin", "https://10.0.0.235:99999"),
                "TCP port",
            ),
            ("string tunnel port", lambda d: d["windows"].__setitem__("tunnel_loopback_port", "x"), "TCP port"),
            ("empty tenant keys", lambda d: d["auth"].__setitem__("tenant_keys", []), "must not be empty"),
            ("duplicate tenant keys", lambda d: d["auth"].__setitem__("tenant_keys", ["a", "a"]), "unique"),
            (
                "relative feishu secret",
                lambda d: d["auth"].__setitem__("feishu_secret_file", "secrets/feishu.json"),
                "absolute path",
            ),
            ("app secret value in auth", lambda d: d["auth"].__setitem__("app_secret", "x"), "unknown keys"),
            ("missing windows instance", lambda d: d["windows"].pop("instance"), "missing keys"),
            ("unknown instance key", lambda d: d["windows"]["instance"].__setitem__("profile", "x"), "unknown keys"),
            (
                "task name with space",
                lambda d: d["windows"]["instance"].__setitem__("task_name", "bad name"),
                "malformed",
            ),
            (
                "task name starting with dash",
                lambda d: d["windows"]["instance"].__setitem__("task_name", "-lead"),
                "malformed",
            ),
            (
                "relative package root",
                lambda d: d["windows"]["instance"].__setitem__("package_root", "packages"),
                "drive-absolute",
            ),
            (
                "windows traversal",
                lambda d: d["windows"]["instance"].__setitem__("state_root", "C:/e2e/../state"),
                "traversal",
            ),
            (
                "windows empty segment",
                lambda d: d["windows"]["instance"].__setitem__("output_root", "C:/e2e//results"),
                "traversal",
            ),
            ("zero concurrency", lambda d: d.__setitem__("execution", {"linux_concurrency": 0}), "positive integer"),
            (
                "unknown execution key",
                lambda d: d.__setitem__("execution", {"linux_concurrency": 1, "x": 1}),
                "unknown keys",
            ),
            (
                "zero retention days",
                lambda d: d.__setitem__("retention", {"unreferenced_evidence_days": 0}),
                "null or a positive integer",
            ),
            ("unknown retention key", lambda d: d.__setitem__("retention", {"x": None}), "unknown keys"),
        )
        for label, mutate, needle in cases:
            with self.subTest(label):
                self.assert_rejected(mutate, needle)

    def test_snapshot_document_validator(self):
        from description_pipeline import interface_records as ir

        with tempfile.TemporaryDirectory(prefix="snapshot-validator-") as temporary:
            root = Path(temporary)
            resolved = platform_config.validate_platform(platform_document(root))
            snapshot = platform_config.build_snapshot(
                resolved,
                platform_json_sha256="a" * 64,
                install_id="gen-1",
                generated_at="2026-10-13T00:00:00Z",
            )
        platform_config.validate_snapshot_document(snapshot)
        ir.validate_config_snapshot(snapshot)
        cases = (
            (
                "foreign schema",
                lambda s: s.__setitem__("schema_version", "review.foreign-snapshot/v99"),
                "schema_version",
            ),
            ("unknown top key", lambda s: s.__setitem__("extra", 1), "unknown keys"),
            ("missing sources", lambda s: s.pop("sources"), "missing keys"),
            ("impossible instant", lambda s: s.__setitem__("generated_at", "2026-99-99T99:99:99Z"), "generated_at"),
            (
                "secret key in resolved",
                lambda s: s["resolved"]["auth"].__setitem__("app_secret", "x"),
                "secret keys",
            ),
            (
                "origin port collision",
                lambda s: s["resolved"]["windows"].__setitem__("tunnel_loopback_port", 9443),
                "must differ",
            ),
            (
                "store root outside state",
                lambda s: s["resolved"]["derived"].__setitem__("store_root", "/elsewhere/runs"),
                "store_root",
            ),
            (
                "execution zero",
                lambda s: s["resolved"]["execution"].__setitem__("linux_concurrency", 0),
                "positive integer",
            ),
        )
        for label, mutate, needle in cases:
            with self.subTest(label):
                candidate = copy.deepcopy(snapshot)
                mutate(candidate)
                with self.assertRaises(SystemExit) as raised:
                    platform_config.validate_snapshot_document(candidate)
                self.assertIn(needle, str(raised.exception))


class RenderTests(unittest.TestCase):
    def test_render_cli_writes_generation_snapshot_and_matches_package_validators(self):
        from description_pipeline import interface_records as ir
        from description_pipeline.io import canonical as io_canonical

        with tempfile.TemporaryDirectory(prefix="platform-config-") as temporary:
            root = Path(temporary)
            config_path = root / "platform.json"
            config_path.write_text(json.dumps(platform_document(root)), encoding="utf-8")

            result = run_cli(
                "render",
                "--config",
                str(config_path),
                "--install-id",
                "gen-1",
                "--generated-at",
                "2026-10-13T00:00:00Z",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
            snapshot_path = Path(summary["snapshot"])
            self.assertEqual(snapshot_path.name, "gen-1.json")
            self.assertEqual(snapshot_path.parent, root / "state" / "snapshots")
            self.assertEqual(stat.S_IMODE(snapshot_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(snapshot_path.parent.stat().st_mode), 0o700)

            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            ir.validate_config_snapshot(snapshot)
            self.assertEqual(platform_config.snapshot_digest(snapshot), ir.config_snapshot_digest(snapshot))
            self.assertEqual(summary["config_digest"], ir.config_snapshot_digest(snapshot))
            self.assertEqual(platform_config.canonical(snapshot), io_canonical(snapshot))
            self.assertEqual(snapshot["resolved"]["windows"]["instance"]["task_name"], "description-e2e-endpoint")

            validate_result = run_cli("validate", "--config", str(config_path))
            self.assertEqual(validate_result.returncode, 0)
            self.assertIn("valid", validate_result.stdout)

    def test_write_snapshot_is_idempotent_per_generation_and_refuses_rewrites(self):
        resolved = platform_config.validate_platform(platform_document(VALID_ROOT))
        snapshot = platform_config.build_snapshot(
            resolved,
            platform_json_sha256="a" * 64,
            install_id="gen-1",
            generated_at="2026-10-13T00:00:00Z",
        )
        with tempfile.TemporaryDirectory(prefix="platform-config-write-") as temporary:
            snapshots = Path(temporary) / "state" / "snapshots"
            first = platform_config.write_snapshot(snapshot, snapshots)
            second = platform_config.write_snapshot(snapshot, snapshots)
            self.assertEqual(first, second)
            tampered = copy.deepcopy(snapshot)
            tampered["generated_at"] = "2026-10-13T00:00:01Z"
            with self.assertRaises(SystemExit):
                platform_config.write_snapshot(tampered, snapshots)

    @unittest.skipUnless(hasattr(os, "fork"), "requires a POSIX fork")
    def test_concurrent_different_payloads_for_one_generation_never_overwrite(self):
        resolved = platform_config.validate_platform(platform_document(VALID_ROOT))
        first = platform_config.build_snapshot(
            resolved,
            platform_json_sha256="a" * 64,
            install_id="gen-race",
            generated_at="2026-10-13T00:00:00Z",
        )
        second = platform_config.build_snapshot(
            resolved,
            platform_json_sha256="b" * 64,
            install_id="gen-race",
            generated_at="2026-10-13T00:00:00Z",
        )
        with tempfile.TemporaryDirectory(prefix="platform-config-race-") as temporary:
            snapshots = Path(temporary) / "snapshots"
            go_ahead, wait_for_write = os.pipe()
            pid = os.fork()
            if pid == 0:  # child: the second writer, released together with the first
                child_code = 2
                try:
                    os.close(wait_for_write)
                    os.read(go_ahead, 1)
                    platform_config.write_snapshot(second, snapshots)
                    child_code = 0
                except SystemExit:
                    child_code = 1
                except Exception:
                    child_code = 2
                finally:
                    os._exit(child_code)
            os.close(go_ahead)
            parent_ok = True
            try:
                os.write(wait_for_write, b"x")
                platform_config.write_snapshot(first, snapshots)
            except SystemExit:
                parent_ok = False
            finally:
                os.close(wait_for_write)
            _, status = os.waitpid(pid, 0)
            child_code = os.waitstatus_to_exitcode(status)
            self.assertIn(child_code, (0, 1), f"child writer crashed with {child_code}")
            self.assertNotEqual(parent_ok, child_code == 0, "exactly one writer must win")
            stored = json.loads((snapshots / "gen-race.json").read_text(encoding="utf-8"))
            self.assertEqual(stored, first if parent_ok else second)

    def test_cli_usage_and_error_exit_codes(self):
        result = run_cli("validate")
        self.assertEqual(result.returncode, 2)
        result = run_cli("frobnicate", "--config", "/nonexistent/platform.json")
        self.assertEqual(result.returncode, 2)
        result = run_cli("validate", "--config", "/nonexistent/platform.json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("config file not found", result.stderr)
        with tempfile.TemporaryDirectory(prefix="platform-config-bad-") as temporary:
            bad = Path(temporary) / "platform.json"
            document = platform_document(Path(temporary))
            document["targets"] = {}
            bad.write_text(json.dumps(document), encoding="utf-8")
            result = run_cli("validate", "--config", str(bad))
            self.assertEqual(result.returncode, 1)
            self.assertIn("platform_config:", result.stderr)
            result = run_cli("render", "--config", str(bad), "--install-id", "gen-1")
            self.assertEqual(result.returncode, 1)
            self.assertIn("platform_config:", result.stderr)


if __name__ == "__main__":
    unittest.main()
