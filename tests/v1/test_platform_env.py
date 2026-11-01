"""platform.json snapshot -> internal operator environment derivation (W2 install bridge)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from description_pipeline import io as pipeline_io

from .contract_wheel import write_contract_wheel

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ROOT / "deploy" / "operator"


def _load_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


platform_config = _load_from_path("deploy_platform_config_env", OPERATOR / "platform_config.py")
render_operator = _load_from_path("deploy_render_operator_env", OPERATOR / "render_operator.py")
platform_env = _load_from_path("deploy_platform_env", OPERATOR / "platform_env.py")
install_record = _load_from_path("deploy_install_record_env", OPERATOR / "install_record.py")


def tree_digest(root: Path) -> str:
    """Digest every file under ``root`` so zero-write claims are detectable."""
    if not root.exists():
        return "missing"
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rows.append([path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()])
    return json.dumps(rows)


def write_release(root: Path, *wheels: Path) -> dict:
    document = {
        "schema_version": install_record.RELEASE_SCHEMA,
        "pipeline_id": "solidworks-to-urdf",
        "version": "1.4.0",
        "source_commit": "c" * 40,
        "source_tree": "d" * 40,
        "source_sha256": "a" * 64,
        "artifacts": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in wheels},
    }
    (root / "release.json").write_text(json.dumps(document), encoding="utf-8")
    return install_record.release_identity(document)


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
            "tenant_keys": ["tenant-a", "tenant-b"],
            "admin_open_ids": ["ou_admin"],
        },
        "targets": {"m3.0": {"repository_slug": "owner/model-repository", "base": "main"}},
        "specifications": {"record_roots": [str(root / "specifications")]},
    }


class PlatformEnvTests(unittest.TestCase):
    def test_omitted_optional_features_have_explicit_disabled_environment_values(self):
        result = self.emit(self.snapshot, install_id="gen-1", wheel=self.wheel)
        self.assertEqual(result["env"]["SIMULATION_CONFIG_JSON"], "")
        self.assertEqual(json.loads(result["env"]["ASSESSMENT_CONFIG_JSON"]), {"enabled": False})

    def test_assessment_environment_uses_the_validated_snapshot(self):
        for value in ({"enabled": False},
                      {"enabled": True, "python": "/opt/teleop/bin/python", "capacity": 2,
                       "limits": {"wall_seconds": 60, "memory_bytes": 2147483648,
                                  "cpu_seconds": 120, "output_bytes": 16777216}}):
            with self.subTest(value=value):
                snapshot = json.loads(json.dumps(self.snapshot))
                snapshot["resolved"]["operator"]["assessment"] = value
                path = self.root / "assessment-snapshot.json"
                path.write_bytes(pipeline_io.canonical(snapshot))
                result = platform_env.derive(
                    path.read_bytes(), install_id="gen-1", snapshot_path=path,
                    tree_root=self.tree, release=self.release, wheel=self.wheel,
                )
                actual = json.loads(result["env"]["ASSESSMENT_CONFIG_JSON"])
                expected = dict(value)
                if value["enabled"]:
                    expected["root"] = str(self.root / "state" / "assessments")
                self.assertEqual(actual, expected)

    def test_teleop_snapshot_derives_the_same_portal_configuration(self):
        snapshot = json.loads(json.dumps(self.snapshot))
        snapshot["resolved"]["operator"]["teleop"] = {"python": "/opt/teleop/bin/python", "capacity": 2}
        path = self.root / "teleop-snapshot.json"
        path.write_bytes(pipeline_io.canonical(snapshot))
        result = platform_env.derive(
            path.read_bytes(),
            install_id="gen-1",
            snapshot_path=path,
            tree_root=self.tree,
            release=self.release,
            wheel=self.wheel,
        )
        simulation = json.loads(result["env"]["SIMULATION_CONFIG_JSON"])
        self.assertEqual(
            {"python": "/opt/teleop/bin/python", "capacity": 2, "root": str(self.root / "state" / "simulations")},
            simulation,
        )
        state = self.root / "portal"
        state.mkdir()
        rendered = render_operator.ensure_portal_config(
            state,
            {
                "AIRFLOW_BASE_URL": "http://127.0.0.1:8080",
                "TUNNEL_LOOPBACK_PORT": "18765",
                "PORTAL_HOST": "127.0.0.1",
                "PORTAL_PORT": "8081",
                "ENDPOINT_TOKEN_FILE": "/private/token",
                "SOLIDWORKS_HANDOFF_ROOT": "/private/intake",
                "MODEL_REPOSITORIES_JSON": "{}",
                "MODEL_TARGETS_JSON": "{}",
                "DISCOVERY_SETTINGS_JSON": "{}",
                "SIMULATION_CONFIG_JSON": result["env"]["SIMULATION_CONFIG_JSON"],
            },
        )
        self.assertEqual(simulation, json.loads(rendered.read_bytes())["portal"]["simulation"])

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="platform-env-")
        self.root = Path(self._temporary.name)
        resolved = platform_config.validate_platform(platform_document(self.root))
        snapshot = platform_config.build_snapshot(
            resolved,
            platform_json_sha256="e" * 64,
            install_id="gen-1",
            generated_at="2026-10-13T00:00:00Z",
        )
        self.tree = self.root / "deployment"
        self.tree.mkdir()
        self.wheel = self.tree / "mimicverse_description-1.4.0-py3-none-any.whl"
        write_contract_wheel(self.wheel)
        self.release = write_release(self.root, self.wheel)
        owner = platform_config.load_contract_owner(platform_config.extract_contract_closure(self.wheel))
        self.snapshot_path = platform_config.write_snapshot(snapshot, self.root / "state" / "snapshots", owner)
        self.snapshot = snapshot

    def emit(self, snapshot, *, install_id, tree_root=None, wheel=None):
        result = platform_env.derive(
            pipeline_io.canonical(snapshot),
            install_id=install_id,
            snapshot_path=self.snapshot_path,
            tree_root=tree_root or self.tree,
            release=self.release,
            wheel=wheel,
        )
        platform_env.emit(result)
        return result

    def _emit(self, snapshot: Path, *, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(OPERATOR / "platform_env.py"),
                "emit",
                "--snapshot",
                str(snapshot),
                "--install-id",
                "gen-1",
                "--tree",
                str(self.tree),
                "--release-file",
                str(self.root / "release.json"),
            ],
            capture_output=True,
            text=True,
            cwd="/tmp",
            env=env if env is not None else {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_derives_the_internal_environment_and_derived_files(self):
        result = self.emit(self.snapshot, install_id="gen-1", tree_root=self.tree)
        env = result["env"]
        self.assertEqual(env["OPERATOR_HOST"], "10.0.0.235")
        self.assertEqual(env["OPERATOR_HTTPS_PORT"], "9443")
        self.assertEqual(env["OPERATOR_STATE"], str(self.root / "state"))
        self.assertEqual(env["AIRFLOW_VENV"], str(self.root / "releases" / "airflow-venv-gen-1"))
        self.assertEqual(env["AIRFLOW_HOME"], str(self.root / "state" / "airflow-home"))
        self.assertEqual(env["POSTGRES_ROOT"], str(self.root / "state" / "airflow-pg"))
        self.assertEqual(env["PIPELINE_WHEEL"], str(self.wheel))
        self.assertEqual(env["PLATFORM_CONFIG_SNAPSHOT"], str(self.snapshot_path))
        self.assertEqual(env["PLATFORM_ARTIFACT_ROOT"], str(self.root / "artifacts"))
        self.assertEqual(env["PIPELINE_WORK_ROOT"], str(self.root / "workspaces"))
        self.assertNotIn("PIPELINE_STORE_ROOT", env)
        self.assertEqual(env["FEISHU_TENANT_KEYS"], "tenant-a,tenant-b")
        self.assertEqual(env["FEISHU_ADMIN_OPEN_IDS"], "ou_admin")
        self.assertEqual(env["SOLIDWORKS_ENDPOINT_PORT"], "9765")
        self.assertIn("airflow_migrate@", env["AIRFLOW_DB_URL"])
        self.assertIn("airflow-pg/socket", env["AIRFLOW_DB_URL"])
        self.assertIn("airflow_runtime@", env["AIRFLOW_RUNTIME_DB_URL"])
        self.assertIn("platform_service@", env["PLATFORM_SERVICE_DSN"])
        self.assertIn("operator_ro@", env["PLATFORM_OPERATOR_DSN"])
        self.assertIn("airflow-pg/socket", env["PLATFORM_OPERATOR_DSN"])

        derived_dir = Path(env["OPERATOR_STATE"]) / "derived" / "gen-1"
        self.assertEqual(result["derived_dir"], derived_dir)
        self.assertFalse((Path(env["OPERATOR_STATE"]) / "derived" / "operator.env").exists())
        repositories = json.loads((derived_dir / "repositories.json").read_text(encoding="utf-8"))
        self.assertEqual(repositories, self.snapshot["resolved"]["derived"]["repositories"])
        targets = json.loads((derived_dir / "targets.json").read_text(encoding="utf-8"))
        self.assertEqual(targets, self.snapshot["resolved"]["targets"])
        discovery = json.loads((derived_dir / "discovery.json").read_text(encoding="utf-8"))
        self.assertEqual(discovery, self.snapshot["resolved"]["derived"]["discovery"])
        env_file = Path(result["env_path"])
        self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(env_file.parent.stat().st_mode), 0o700)
        # The file carries the one canonical encoding; reading it back with the canonical
        # decoder must reproduce every value byte-for-byte (shell-safety quoting included).
        parsed = {
            key: render_operator.decode_env_value(raw.strip())
            for key, raw in (line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines() if line)
        }
        self.assertEqual(parsed, env)

    def test_derivation_and_verification_do_not_write_installation_files(self):
        state = self.root / "state"
        before = tree_digest(state)
        result = platform_env.derive(
            json.dumps(self.snapshot).encode("utf-8"),
            install_id="gen-1",
            snapshot_path=self.snapshot_path,
            tree_root=self.tree,
            release=self.release,
        )
        self.assertEqual(tree_digest(state), before)
        with self.assertRaisesRegex(SystemExit, "missing or a link"):
            platform_env.verify(result)
        self.assertEqual(tree_digest(state), before)
        platform_env.emit(result)
        before = tree_digest(state)
        platform_env.verify(result)
        self.assertEqual(tree_digest(state), before)
        for path, payload in result["files"].items():
            with self.subTest(path.name):
                path.unlink()
                target = self.root / ("linked-" + path.name)
                target.write_bytes(payload)
                path.symlink_to(target)
                with self.assertRaisesRegex(SystemExit, "missing or a link"):
                    platform_env.verify(result)
                self.assertTrue(path.is_symlink())
                path.unlink()
                path.write_bytes(payload)

    def test_shell_verifier_rejects_derived_files_even_with_matching_resolved_values(self):
        result = self.emit(self.snapshot, install_id="gen-1")
        helpers = self.tree / "deploy" / "operator"
        helpers.mkdir(parents=True)
        names = ("platform_env.py", "platform_config.py", "install_record.py", "render_operator.py", "systemd_units.py")
        for name in names:
            shutil.copyfile(OPERATOR / name, helpers / name)
        release_file = self.root / "release.json"
        release = json.loads(release_file.read_text())
        release["deployment_files"] = {
            f"deploy/operator/{name}": hashlib.sha256((helpers / name).read_bytes()).hexdigest() for name in names
        }
        release_file.write_text(json.dumps(release))

        def verify():
            return subprocess.run(
                [
                    "bash",
                    "-c",
                    'set -euo pipefail; source "$1"; shift; verify_snapshot_derived_env "$@"',
                    "boundary",
                    str(OPERATOR / "environment.sh"),
                    str(self.snapshot_path),
                    "gen-1",
                    str(result["env_path"]),
                    str(self.tree),
                    str(release_file),
                ],
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "CONTROL_PYTHON": sys.executable,
                    "HERE": str(helpers),
                },
                capture_output=True,
                text=True,
                check=False,
            )

        completed = verify()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        cases = (
            ("MODEL_REPOSITORIES_FILE", "MODEL_REPOSITORIES_JSON", {"owner/model-repository": "/other/workspace"}),
            (
                "MODEL_TARGETS_FILE",
                "MODEL_TARGETS_JSON",
                {"m3.0": {"repository_slug": "owner/model-repository", "base": "other"}},
            ),
            (
                "DISCOVERY_SETTINGS_FILE",
                "DISCOVERY_SETTINGS_JSON",
                {"record_roots": ["/other/specifications"]},
            ),
        )
        resolved_file = self.root / "state" / "resolved.env"
        for file_key, value_key, replacement in cases:
            with self.subTest(file_key):
                path = Path(result["env"][file_key])
                original = path.read_bytes()
                path.write_bytes(pipeline_io.canonical(replacement))
                resolved_file.write_text(
                    "\n".join(render_operator.encode_env_lines([(value_key, json.dumps(replacement))])) + "\n"
                )
                before = tree_digest(self.root / "state")
                completed = verify()
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                self.assertIn("differs from its snapshot", completed.stderr)
                self.assertEqual(tree_digest(self.root / "state"), before)
                path.write_bytes(original)
        path = result["env_path"]
        path.write_bytes(path.read_bytes() + b"EXTRA=value\n")
        before = tree_digest(self.root / "state")
        completed = verify()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("differs from its snapshot", completed.stderr)
        self.assertEqual(tree_digest(self.root / "state"), before)

    def test_service_environment_prevents_imports_from_mutating_release(self) -> None:
        result = self.emit(self.snapshot, install_id="gen-1")
        env = render_operator.read_env(result["env_path"])
        units_root = self.root / "systemd-config"
        rendered = subprocess.run(
            ["/bin/bash", str(ROOT / "deploy/airflow/services.sh"), "render"],
            env={
                **env,
                "PATH": "/usr/bin:/bin",
                "HOME": str(self.root),
                "XDG_CONFIG_HOME": str(units_root),
                "AIRFLOW_API_PORT": "8080",
                "OPERATOR_DERIVED_ENV": str(result["env_path"]),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        for unit in render_operator.systemd_units.units_for_role("airflow"):
            service = (units_root / "systemd/user" / f"{unit}.service").read_text(encoding="utf-8")
            self.assertIn(f"EnvironmentFile={result['env_path']}\n", service)
        module = self.tree / "release_import_probe.py"
        module.write_text("VALUE = 42\n", encoding="utf-8")
        before = tree_digest(self.tree)
        script = (
            "import importlib.util, sys; "
            "spec = importlib.util.spec_from_file_location('release_import_probe', sys.argv[1]); "
            "module = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(module); assert module.VALUE == 42"
        )
        imported = subprocess.run(
            [sys.executable, "-c", script, str(module)],
            env=env,
            cwd="/tmp",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertEqual(tree_digest(self.tree), before)
        without_policy = {key: value for key, value in env.items() if key != "PYTHONDONTWRITEBYTECODE"}
        imported = subprocess.run(
            [sys.executable, "-c", script, str(module)],
            env=without_policy,
            cwd="/tmp",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertNotEqual(tree_digest(self.tree), before)

    def test_the_env_encoding_round_trips_every_boundary_value(self) -> None:
        """One contract: encode -> decode -> bash source reproduce the exact bytes."""

        import subprocess

        samples = {
            "empty": "",
            "plain": "m3.0",
            "ampersand_dsn": "postgresql://role@/db?host=/srv/socket&port=5433",
            "spaces": "two words and more",
            "single_quote": "it's quoted",
            "double_quote": 'with "double" quotes',
            "dollar_and_backtick": "$HOME `date` $(id)",
            "semicolons_and_stars": "a;b*c|d>e",
            "backslash": "back\\slash",
            "comma_list": "tenant-a,tenant-b",
            "space_only": " ",
        }
        lines = render_operator.encode_env_lines((f"VALUE_{key.upper()}", value) for key, value in samples.items())
        env_file = self.root / "boundary.env"
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        decoded = {key: render_operator.decode_env_value(raw) for key, raw in (line.split("=", 1) for line in lines)}
        self.assertEqual(decoded, {f"VALUE_{key.upper()}": value for key, value in samples.items()})
        # The declaration also clears an inherited value instead of silently retaining it.
        # The format stays single-quoted: an unquoted \0 is quote-removed to a literal "0".
        reads = " ; ".join(f"printf '%s\\0' \"${{VALUE_{key.upper()}}}\"" for key in samples)
        result = subprocess.run(
            ["env", "-i", "VALUE_EMPTY=ambient", "bash", "-c",
             'set -a ; source "$1" ; set +a ; ' + reads, "boundary", str(env_file)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        sourced = [item.decode("utf-8") for item in result.stdout.split(b"\x00")[:-1]]
        self.assertEqual(sourced, list(samples.values()))
        # A line break cannot be represented in this one-line format: the encoder refuses it.
        with self.assertRaises(ValueError):
            render_operator.encode_env_value("a\nb")

    def test_wheel_selection_is_bound_to_the_release(self):
        second = self.tree / "another.whl"
        write_contract_wheel(second)
        with self.assertRaises(SystemExit) as raised:
            self.emit(self.snapshot, install_id="gen-1")
        self.assertIn("exactly one", str(raised.exception))
        self.release = write_release(self.root, self.wheel, second)
        result = self.emit(self.snapshot, install_id="gen-1", wheel=second)
        self.assertEqual(result["wheel"], second)
        rogue = self.tree / "rogue.whl"
        rogue.write_bytes(b"rogue")
        with self.assertRaises(SystemExit) as raised:
            self.emit(self.snapshot, install_id="gen-1", wheel=rogue)
        self.assertIn("not a release artifact", str(raised.exception))
        rogue_same_name = self.root / "unrelated" / self.wheel.name
        rogue_same_name.parent.mkdir()
        rogue_same_name.write_bytes(b"UNINDEXED-ROGUE-WHEEL")
        with self.assertRaises(SystemExit) as raised:
            self.emit(self.snapshot, install_id="gen-1", wheel=rogue_same_name)
        self.assertIn("does not match release.json", str(raised.exception))
        with self.assertRaises(SystemExit):
            self.emit(self.snapshot, install_id="gen-1", wheel=self.tree / "missing.whl")

    def test_render_operator_consumes_the_derived_env(self):
        result = self.emit(self.snapshot, install_id="gen-1", tree_root=self.tree)
        state = self.root / "state"
        secrets = state / "secrets"
        secrets.mkdir(parents=True, exist_ok=True)
        (secrets / "tls.crt").write_bytes(b"cert")
        (secrets / "tls.key").write_bytes(b"key")
        (secrets / "feishu.json").write_bytes(b"{}")
        (secrets / "endpoint.token").write_bytes(b"token")
        (secrets / "postgres.dsn").write_bytes(b"dsn")
        os.chmod(state, 0o700)
        for name in ("tls.crt", "tls.key", "feishu.json", "endpoint.token", "postgres.dsn"):
            os.chmod(secrets / name, 0o600)
        completed = subprocess.run(
            [
                sys.executable,
                str(OPERATOR / "render_operator.py"),
                "--env-file",
                str(result["env_path"]),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        # A first dry-run reports missing installed files as DRIFT; it must parse the derived env
        # and never fail on configuration shape (exit 1 = drift, exit 0 = none).
        self.assertIn(completed.returncode, (0, 1), completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_emit_cli(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(OPERATOR / "platform_env.py"),
                "emit",
                "--snapshot",
                str(self.snapshot_path),
                "--install-id",
                "gen-1",
                "--tree",
                str(self.tree),
                "--release-file",
                str(self.root / "release.json"),
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = dict(line.split("=", 1) for line in completed.stdout.strip().splitlines())
        self.assertTrue(Path(summary["env"]).is_file())
        self.assertEqual(Path(summary["wheel"]), self.wheel)

    def test_generations_are_independent_and_immutable(self):
        first = self.emit(self.snapshot, install_id="gen-1", tree_root=self.tree)
        first_env_bytes = Path(first["env_path"]).read_bytes()
        first_targets_path = Path(first["env"]["MODEL_TARGETS_FILE"])
        first_targets_bytes = first_targets_path.read_bytes()

        second_snapshot = json.loads(json.dumps(self.snapshot))
        second_snapshot["install_id"] = "gen-2"
        second_snapshot["resolved"]["targets"]["m3.0"]["base"] = "feature/review-next"
        second = self.emit(second_snapshot, install_id="gen-2", tree_root=self.tree)
        self.assertNotEqual(first["env_path"], second["env_path"])
        self.assertEqual(Path(first["env_path"]).read_bytes(), first_env_bytes)
        self.assertEqual(first_targets_path.read_bytes(), first_targets_bytes)
        self.assertEqual(json.loads(first_targets_path.read_text())["m3.0"]["base"], "main")
        self.assertEqual(
            json.loads(Path(second["env"]["MODEL_TARGETS_FILE"]).read_text())["m3.0"]["base"],
            "feature/review-next",
        )

        conflicting = json.loads(json.dumps(self.snapshot))
        conflicting["resolved"]["targets"]["m3.0"]["base"] = "other"
        with self.assertRaises(SystemExit):
            self.emit(conflicting, install_id="gen-1", tree_root=self.tree)
        self.assertEqual(first_targets_path.read_bytes(), first_targets_bytes)
        self.assertEqual(Path(first["env_path"]).read_bytes(), first_env_bytes)

    def test_malformed_inputs_fail_closed_without_writing(self):
        state = self.root / "state"
        cases = []
        broken = json.loads(json.dumps(self.snapshot))
        broken["resolved"]["storage"]["state_root"] = "relative/state"
        cases.append(("relative state root", broken, "gen-1"))
        broken = json.loads(json.dumps(self.snapshot))
        broken["resolved"]["windows"]["endpoint_port"] = "9765"
        cases.append(("string endpoint port", broken, "gen-1"))
        cases.append(("generation mismatch", self.snapshot, "gen-2"))
        foreign = json.loads(json.dumps(self.snapshot))
        foreign["schema_version"] = "review.foreign-snapshot/v99"
        cases.append(("foreign schema", foreign, "gen-1"))
        cases.append(("bad install id", self.snapshot, "bad/id"))
        # The canonical IF-16 snapshot rules must refuse at the derive/emit boundary before
        # any write, not only inside the validator function.
        unknown_key = json.loads(json.dumps(self.snapshot))
        unknown_key["extra"] = 1
        cases.append(("unknown top key", unknown_key, "gen-1"))
        missing_sources = json.loads(json.dumps(self.snapshot))
        del missing_sources["sources"]
        cases.append(("missing sources", missing_sources, "gen-1"))
        impossible = json.loads(json.dumps(self.snapshot))
        impossible["generated_at"] = "2026-99-99T99:99:99Z"
        cases.append(("impossible instant", impossible, "gen-1"))
        secret = json.loads(json.dumps(self.snapshot))
        secret["resolved"]["auth"]["app_secret"] = "x"
        cases.append(("secret in auth", secret, "gen-1"))
        collision = json.loads(json.dumps(self.snapshot))
        collision["resolved"]["windows"]["tunnel_loopback_port"] = 9443
        cases.append(("origin port collision", collision, "gen-1"))
        for label, snapshot, install_id in cases:
            with self.subTest(label):
                before = tree_digest(state)
                with self.assertRaises(SystemExit):
                    self.emit(snapshot, install_id=install_id, tree_root=self.tree)
                self.assertEqual(tree_digest(state), before, "a refused snapshot must not write state")
                self.assertFalse((state / "derived").exists(), "no derived files may be written")
        before = tree_digest(state)
        with self.assertRaises(SystemExit):
            platform_env.derive(
                b"{not json",
                install_id="gen-1",
                snapshot_path=self.snapshot_path,
                tree_root=self.tree,
                release=self.release,
            )
        self.assertEqual(tree_digest(state), before, "an unparseable snapshot must not write state")
        self.assertFalse((state / "derived").exists())

    def test_a_valid_derivation_shows_detectable_writes(self):
        state = self.root / "state"
        before = tree_digest(state)
        result = self.emit(self.snapshot, install_id="gen-1", tree_root=self.tree)
        self.assertNotEqual(tree_digest(state), before, "the zero-write detector must detect successful writes")
        written = {path.relative_to(state).as_posix() for path in state.rglob("*") if path.is_file()}
        self.assertTrue(
            {
                "derived/gen-1/operator.env",
                "derived/gen-1/repositories.json",
                "derived/gen-1/targets.json",
                "derived/gen-1/discovery.json",
            }
            <= written
        )
        targets = json.loads(Path(result["env"]["MODEL_TARGETS_FILE"]).read_text(encoding="utf-8"))
        self.assertEqual(targets["m3.0"]["base"], "main")

    def test_wheel_without_the_contract_closure_is_refused_before_any_write(self):
        hollow = self.root / "hollow.whl"
        with zipfile.ZipFile(hollow, "w") as archive:
            archive.writestr("mimicverse_description-1.4.0.dist-info/METADATA", "Name: mimicverse-description\n")
        self.release = write_release(self.root, hollow)
        state = self.root / "state"
        before = tree_digest(state)
        with self.assertRaises(SystemExit) as raised:
            self.emit(self.snapshot, install_id="gen-1", wheel=hollow)
        self.assertIn("contract closure", str(raised.exception))
        self.assertEqual(tree_digest(state), before, "a wheel without the closure must not write state")
        self.assertFalse((state / "derived").exists())

    def test_contract_closure_extracts_and_ignores_pyyaml_under_isolation(self):
        script = f"""
import pathlib
import sys

sys.path.insert(0, {str(OPERATOR)!r})
import platform_env

directory = platform_env.extract_contract_closure(pathlib.Path(sys.argv[1]))
sys.path.insert(0, str(directory))
from description_pipeline import interface_records

print(interface_records.CONFIG_SNAPSHOT_SCHEMA)
print("yaml" in sys.modules)
from description_pipeline import io

print(io.parse_data(b'{{"a": 1}}', pathlib.Path("x.json"))["a"])
try:
    io.parse_data(b"a: 1", pathlib.Path("x.yaml"))
except io.PipelineError as error:
    print(f"yaml-required: {{error}}")
"""
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-c", script, str(self.wheel)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        schema, yaml_loaded, parsed, yaml_required = completed.stdout.strip().splitlines()
        self.assertEqual(schema, platform_config.SNAPSHOT_SCHEMA)
        self.assertEqual(yaml_loaded, "False", "the closure must load without PyYAML")
        self.assertEqual(parsed, "1")
        self.assertIn(
            "the YAML parser (PyYAML) is required to read this input",
            yaml_required,
            "the YAML read path still requires PyYAML, reported as a PipelineError",
        )

    def test_emit_ignores_ambient_validator_forks_before_any_write(self):
        def ambient_home(name: str, validator: str) -> dict:
            home = self.root / name
            package = home / "description_pipeline"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "interface_records.py").write_text(validator, encoding="utf-8")
            (home / "sitecustomize.py").write_text("import description_pipeline.interface_records\n", encoding="utf-8")
            return dict(os.environ, PYTHONPATH=str(home))

        weak = ambient_home(
            "ambient-weak",
            "class InterfaceRecordError(ValueError):\n    pass\n\n\ndef validate_config_snapshot(snapshot):\n"
            "    return None\n",
        )
        strict = ambient_home(
            "ambient-strict",
            "class InterfaceRecordError(ValueError):\n    pass\n\n\ndef validate_config_snapshot(snapshot):\n"
            "    raise InterfaceRecordError('the ambient fork must not decide')\n",
        )
        probe = subprocess.run(
            [sys.executable, "-c", "import description_pipeline.interface_records as module; print(module.__file__)"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            env=weak,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn("ambient-weak", probe.stdout, "the ambient hook must be able to shadow a plain interpreter")

        state = self.root / "state"
        relaxed_snapshot = json.loads(json.dumps(self.snapshot))
        relaxed_snapshot["review_unknown"] = 1
        unknown = self.root / "ambient-unknown.json"
        unknown.write_text(json.dumps(relaxed_snapshot), encoding="utf-8")
        before = tree_digest(state)
        relaxed = self._emit(unknown, env=weak)
        self.assertNotEqual(relaxed.returncode, 0, relaxed.stdout + relaxed.stderr)
        self.assertEqual(tree_digest(state), before, "an ambient fork must not let an invalid snapshot write state")
        self.assertFalse((state / "derived").exists())

        before = tree_digest(state)
        tightened = self._emit(self.snapshot_path, env=strict)
        self.assertEqual(tightened.returncode, 0, tightened.stdout + tightened.stderr)
        written = {path.relative_to(state).as_posix() for path in state.rglob("*") if path.is_file()}
        self.assertTrue(
            {
                "derived/gen-1/operator.env",
                "derived/gen-1/repositories.json",
                "derived/gen-1/targets.json",
                "derived/gen-1/discovery.json",
            }
            <= written
        )
        self.assertNotEqual(tree_digest(state), before)

    def test_emit_runs_outside_the_checkout_with_a_scrubbed_environment(self):
        state = self.root / "state"
        completed = self._emit(self.snapshot_path)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        written = {path.relative_to(state).as_posix() for path in state.rglob("*") if path.is_file()}
        self.assertTrue(
            {
                "derived/gen-1/operator.env",
                "derived/gen-1/repositories.json",
                "derived/gen-1/targets.json",
                "derived/gen-1/discovery.json",
            }
            <= written
        )

    def test_emit_refuses_duplicate_keys_with_the_canonical_parser_before_any_write(self):
        raw = json.dumps(self.snapshot).encode("utf-8")
        duplicate = self.root / "duplicate.json"
        duplicate.write_bytes(b'{"install_id":"bad/id",' + raw[1:])
        with self.assertRaises(pipeline_io.PipelineError) as canonical:
            pipeline_io.parse_data(duplicate.read_bytes(), duplicate)
        self.assertIn("Duplicate input key", str(canonical.exception))
        state = self.root / "state"
        before = tree_digest(state)
        completed = self._emit(duplicate)
        self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("Duplicate input key", completed.stderr)
        self.assertEqual(tree_digest(state), before, "a duplicate-key snapshot must not write state")
        self.assertFalse((state / "derived").exists())


if __name__ == "__main__":
    unittest.main()
