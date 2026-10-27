"""operatorctl unified lifecycle surface over platform.json (config only).

All frozen lifecycle verbs (``validate/install/start/drain/stop/status/health/reconcile/stage/
promote/rollback``) take ``--config`` as the single entry; every one of them passes the same
static ``platform_config.py`` gate first.  The operational verbs load the active install
record and its derived per-generation environment; each of them refuses on its real
preconditions with zero side effects instead of pretending.  ``--env-file`` does not exist as
an entry point.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from description_pipeline import schemas

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ROOT / "deploy" / "operator"
CONTROL = OPERATOR / "operatorctl.sh"
HEREDOC_REDIRECT = re.compile(
    r"(?<!<)<<(?!<)-?\s*(?:'[^'\n]+'|\"[^\"\n]+\"|[A-Za-z0-9_][A-Za-z0-9_.:+/-]*)"
)

ALL_VERBS = (
    "validate",
    "install",
    "start",
    "drain",
    "stop",
    "status",
    "health",
    "reconcile",
    "stage",
    "promote",
    "rollback",
)
OPERATIONAL_VERBS = tuple(verb for verb in ALL_VERBS if verb not in {"validate", "install"})


def has_here_document(source: str) -> bool:
    return HEREDOC_REDIRECT.search(source) is not None


def here_document_command_substitution_violations(source: str) -> list[str]:
    violations = set()
    for name in re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) \{", source, re.M):
        marker = f"{name}() {{"
        body = source.split(marker, 1)[1].split("\n}\n", 1)[0]
        if not has_here_document(body):
            continue
        invocation = (
            rf"\$\(\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*=[^\n]*)\s+)*"
            rf"{re.escape(name)}\b"
        )
        if re.search(invocation, source):
            violations.add(name)
    return sorted(violations)


def run(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess:
    merged = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "CONTROL_PYTHON": sys.executable,
    }
    merged.update(env or {})
    return subprocess.run(
        ["bash", str(CONTROL), *args],
        capture_output=True,
        text=True,
        env=merged,
        cwd=str(ROOT),
        pass_fds=pass_fds,
    )


def platform_document(root: Path) -> dict:
    return {
        "schema_version": schemas.PLATFORM_CONFIG_SCHEMA,
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


class OperatorctlSurfaceTests(unittest.TestCase):
    def test_control_python_guard_fails_before_config_or_state_writes(self) -> None:
        result = run(
            ["validate", "--config", "/nonexistent/platform.json"],
            env={"CONTROL_PYTHON": "/bin/false"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("CONTROL_PYTHON must be CPython 3.12", result.stderr)
        self.assertNotIn("config file not found", result.stderr)

    def test_health_uses_the_same_control_python_guard(self) -> None:
        result = subprocess.run(
            ["bash", str(OPERATOR / "health.sh"), "--static"],
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(Path.home()),
                "CONTROL_PYTHON": "/bin/false",
                "OPERATOR_STATE": "/nonexistent",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("health: CONTROL_PYTHON must be CPython 3.12", result.stderr)
        self.assertNotIn("missing /nonexistent/resolved.env", result.stdout)

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="operatorctl-config-")
        self.root = Path(self._temporary.name)
        self.config = self.root / "platform.json"
        self.config.write_text(json.dumps(platform_document(self.root)), encoding="utf-8")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_validate_verb_accepts_a_valid_configuration(self):
        result = run(["validate", "--config", str(self.config)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("configuration valid", result.stdout)

    def test_install_over_config_requires_its_bound_inputs(self):
        missing_identity = run(["install", "--config", str(self.config)])
        self.assertEqual(missing_identity.returncode, 1)
        self.assertIn("--endpoint-identity", missing_identity.stderr)
        identity = self.root / "endpoint-identity.json"
        identity.write_text("{}", encoding="utf-8")
        missing_release = run(["install", "--config", str(self.config), "--endpoint-identity", str(identity)])
        self.assertEqual(missing_release.returncode, 1)
        self.assertIn("release manifest", missing_release.stderr)

    def test_every_operational_verb_requires_an_installed_generation(self):
        # Every verb — the maintenance switch included — fails closed on its real first
        # precondition (the active generation) before any side effect, with no transitional
        # "not implemented" state left anywhere.
        for verb in OPERATIONAL_VERBS:
            with self.subTest(verb):
                result = run([verb, "--config", str(self.config)])
                self.assertEqual(result.returncode, 1)
                self.assertIn("not installed", result.stderr)

    def test_mutating_verbs_reject_source_config_replacement_during_lock(self):
        state = self.root / "state"
        state.mkdir()
        marker = self.root / "identity-loaded"
        (state / "resolved.env").write_text(
            f"touch {marker}\n",
            encoding="utf-8",
        )
        original = self.config.read_bytes()
        replacement_document = platform_document(self.root)
        replacement_document["database"]["dsn_file"] = str(
            self.root / "secrets" / "replacement.dsn"
        )
        replacement_document["windows"]["ssh_host"] = "replacement-windows"
        replacement = self.root / "replacement-platform.json"
        replacement.write_text(
            json.dumps(replacement_document),
            encoding="utf-8",
        )

        bindir = self.root / "bin"
        bindir.mkdir()
        flock = bindir / "flock"
        flock.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'cp -- "$FIXTURE_REPLACEMENT" "$FIXTURE_REPLACEMENT_NEXT"\n'
            'mv -f -- "$FIXTURE_REPLACEMENT_NEXT" "$FIXTURE_SOURCE"\n'
            'exec /usr/bin/flock "$@"\n',
            encoding="utf-8",
        )
        flock.chmod(0o755)
        temporary_root = self.root / "tmp"
        temporary_root.mkdir()
        environment = {
            "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "TMPDIR": str(temporary_root),
            "FIXTURE_REPLACEMENT": str(replacement),
            "FIXTURE_REPLACEMENT_NEXT": str(self.root / "replacement.next"),
            "FIXTURE_SOURCE": str(self.config),
        }
        for verb in ("drain", "stop", "reconcile", "stage", "promote", "rollback"):
            with self.subTest(verb=verb):
                self.config.write_bytes(original)
                marker.unlink(missing_ok=True)
                result = run(
                    [verb, "--config", str(self.config)],
                    env=environment,
                )
                self.assertEqual(
                    result.returncode,
                    1,
                    result.stdout + result.stderr,
                )
                self.assertIn(
                    "configuration changed while acquiring the maintenance lock",
                    result.stderr,
                )
                self.assertFalse(
                    marker.exists(),
                    "identity was read before the changed configuration was refused",
                )
                self.assertEqual(list(temporary_root.iterdir()), [])

    def test_mutating_verbs_fail_before_identity_when_the_lock_is_held(self):
        state = self.root / "state"
        lock_dir = state / "locks"
        lock_dir.mkdir(parents=True)
        marker = self.root / "identity-loaded"
        (state / "resolved.env").write_text(
            f"touch {marker}\n",
            encoding="utf-8",
        )
        lock_path = lock_dir / "maintenance.lock"
        lock_path.write_text("held by fixture\n", encoding="utf-8")
        temporary_root = self.root / "tmp"
        temporary_root.mkdir()
        with lock_path.open("rb") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            for verb in ("drain", "stop", "reconcile", "stage", "promote", "rollback"):
                with self.subTest(verb=verb):
                    marker.unlink(missing_ok=True)
                    result = run(
                        [verb, "--config", str(self.config)],
                        env={"TMPDIR": str(temporary_root)},
                    )
                    self.assertEqual(
                        result.returncode,
                        1,
                        result.stdout + result.stderr,
                    )
                    self.assertIn(
                        "another supported lifecycle operation owns the maintenance lock",
                        result.stderr,
                    )
                    self.assertFalse(
                        marker.exists(),
                        "identity or side effects ran after the lock refusal",
                    )
                    for path in (
                        state / "promotions",
                        state / "backups",
                        state / "health-gate",
                    ):
                        self.assertFalse(path.exists())
                    self.assertEqual(list(temporary_root.iterdir()), [])

    def test_source_change_after_lock_check_never_switches_the_snapshot(self):
        state = self.root / "state"
        state.mkdir()
        venv = self.root / "venv"
        (venv / "bin").mkdir(parents=True)
        fake_python = venv / "bin" / "python"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'install_id=gen-1\\n'\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        (state / "resolved.env").write_text(
            f"AIRFLOW_HOME={self.root / 'airflow'}\n"
            f"AIRFLOW_VENV={venv}\n"
            f"OPERATOR_STATE={state}\n",
            encoding="utf-8",
        )
        (state / "derived" / "gen-1").mkdir(parents=True)
        (state / "derived" / "gen-1" / "operator.env").write_text(
            "",
            encoding="utf-8",
        )

        switched_state = self.root / "switched-state"
        switched_state.mkdir()
        switched_marker = self.root / "switched-state-loaded"
        (switched_state / "resolved.env").write_text(
            f"touch {switched_marker}\n",
            encoding="utf-8",
        )
        replacement_document = platform_document(self.root)
        replacement_document["storage"]["state_root"] = str(switched_state)
        replacement = self.root / "replacement-platform.json"
        replacement.write_text(
            json.dumps(replacement_document),
            encoding="utf-8",
        )

        bindir = self.root / "bin"
        bindir.mkdir()
        counter = self.root / "cmp-count"
        cmp_stub = bindir / "cmp"
        cmp_stub.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "/usr/bin/cmp \"$@\"\n"
            "count=\"$(cat \"$FIXTURE_COUNTER\" 2>/dev/null || printf '0')\"\n"
            "count=$((count + 1))\n"
            "printf '%s\\n' \"$count\" > \"$FIXTURE_COUNTER\"\n"
            "if [ \"$count\" -eq 2 ]; then\n"
            '  cp -- "$FIXTURE_REPLACEMENT" "$FIXTURE_REPLACEMENT_NEXT"\n'
            '  mv -f -- "$FIXTURE_REPLACEMENT_NEXT" "$FIXTURE_SOURCE"\n'
            "fi\n",
            encoding="utf-8",
        )
        cmp_stub.chmod(0o755)
        temporary_root = self.root / "tmp"
        temporary_root.mkdir()
        result = run(
            [
                "stage",
                "--config",
                str(self.config),
                "--install-id",
                "gen-2",
            ],
            env={
                "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
                "TMPDIR": str(temporary_root),
                "FIXTURE_COUNTER": str(counter),
                "FIXTURE_REPLACEMENT": str(replacement),
                "FIXTURE_REPLACEMENT_NEXT": str(self.root / "replacement.next"),
                "FIXTURE_SOURCE": str(self.config),
            },
        )
        self.assertNotIn(
            "configuration changed while acquiring the maintenance lock",
            result.stderr,
        )
        self.assertFalse(
            switched_marker.exists(),
            "the operator switched to the configuration's replacement state root",
        )
        self.assertEqual(list(temporary_root.iterdir()), [])

    def test_escape_install_id_is_refused_before_promotion_paths(self):
        escape_name = f"escape-{self.root.name}"
        state_root = self.root / "state"
        actual_escape = state_root / escape_name
        actual_escape.mkdir(parents=True)
        sentinel = actual_escape / "sentinel"
        sentinel.write_text("preserve\n", encoding="utf-8")
        outside_state = self.root / "outside-state-sentinel"
        outside_state.write_text("outside\n", encoding="utf-8")
        malicious_values = (f"../{escape_name}", "bad\nid", "bad\tid", "bad/id")
        for malicious in malicious_values:
            for verb in ("stage", "promote", "rollback", "reconcile"):
                with self.subTest(verb=verb, malicious=repr(malicious)):
                    result = run(
                        [
                            verb,
                            "--config",
                            str(self.config),
                            "--install-id",
                            malicious,
                        ]
                    )
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("install-id is malformed", result.stderr)
                    self.assertNotIn("bad", result.stderr)
                    self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")
                    self.assertEqual(
                        outside_state.read_text(encoding="utf-8"),
                        "outside\n",
                    )
        promotion = subprocess.run(
            [
                sys.executable,
                str(OPERATOR / "promotion.py"),
                "show",
                "--state-root",
                str(self.root / "state"),
                "--install-id",
                f"../{escape_name}",
            ],
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        self.assertEqual(promotion.returncode, 1, promotion.stdout + promotion.stderr)
        self.assertIn("install_id is malformed", promotion.stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")
        self.assertEqual(
            outside_state.read_text(encoding="utf-8"),
            "outside\n",
        )

    def test_install_id_rule_has_one_canonical_owner(self):
        owners = []
        for path in OPERATOR.glob("*.py"):
            if "_INSTALL_ID = re.compile" in path.read_text(encoding="utf-8"):
                owners.append(path.name)
        self.assertEqual(owners, ["platform_config.py"])
        for name in ("install_record.py", "platform_env.py", "promotion.py"):
            source = (OPERATOR / name).read_text(encoding="utf-8")
            self.assertIn("valid_install_id", source)

    def test_keep_database_is_a_stop_only_maintenance_switch(self):
        accepted = run(
            [
                "stop",
                "--config",
                str(self.config),
                "--keep-database",
            ]
        )
        self.assertEqual(accepted.returncode, 1)
        self.assertIn("not installed", accepted.stderr)
        for verb in ("start", "drain", "promote"):
            with self.subTest(verb):
                refused = run(
                    [
                        verb,
                        "--config",
                        str(self.config),
                        "--keep-database",
                    ]
                )
                self.assertEqual(refused.returncode, 2)
                self.assertIn("only accepted by stop", refused.stderr)

    def test_keep_database_stop_branch_retains_postgres(self):
        source = CONTROL.read_text(encoding="utf-8")
        branch = source.split('if [ -n "$KEEP_DATABASE" ]; then', 1)[1].split(
            "else",
            1,
        )[0]
        self.assertIn("airflow_lifecycle stop-executors", branch)
        self.assertNotIn("airflow_lifecycle stop\n", branch)
        self.assertIn('"${DATABASE_UNITS[0]}.service"', branch)
        self.assertIn("PostgreSQL remains active", branch)

    def test_start_resumes_windows_after_linux_and_before_admission(self):
        source = CONTROL.read_text(encoding="utf-8")
        start_branch = source.rsplit("  start)\n", 1)[1].split("  drain)\n", 1)[0]
        linux_services = start_branch.index("operator_units_enable")
        windows_resume = start_branch.index('instance_lifecycle_resume "$ACTIVE_ID" "$STATE_ROOT/install-record.json"')
        dag_unpause = start_branch.index("airflow_unpause_dag")
        admission_open = start_branch.index("platform_open_admission")
        self.assertLess(linux_services, windows_resume)
        self.assertLess(windows_resume, dag_unpause)
        self.assertLess(dag_unpause, admission_open)

    def test_private_windows_resume_uses_authenticated_identity_and_health(self):
        source = CONTROL.read_text(encoding="utf-8")
        resume = source.split("instance_lifecycle_resume() {", 1)[1].split("\n# The Windows end of the unified", 1)[0]
        transport = (OPERATOR / "windows_transport.py").read_text(encoding="utf-8")
        self.assertIn('"release_identity_sha256": hashlib.sha256', resume)
        self.assertIn('"install_id": end["install_id"]', resume)
        self.assertIn('"config_digest": end["config_digest"]', resume)
        self.assertIn('"script_sha256": hashlib.sha256', resume)
        self.assertIn("-o StrictHostKeyChecking=yes", resume)
        self.assertIn(
            'windows_lifecycle "$output" "$remote_script" "$remote_binding" resume',
            resume,
        )
        self.assertIn('windows_transport "$WINDOWS_LIFECYCLE_TIMEOUT"', source)
        self.assertNotIn('ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "$SOLIDWORKS_SSH_HOST"', resume)
        self.assertNotIn('powershell.exe -NoProfile -ExecutionPolicy Bypass -File', resume)
        self.assertIn('"framing": "single-line-lf"', transport)
        self.assertIn("input=payload", transport)
        self.assertNotIn("input=script", transport)
        self.assertNotIn("endpoint.token", resume)
        self.assertNotIn("password", resume.lower())
        self.assertIn(
            'stamp="$(date -u +%Y%m%dT%H%M%S%NZ)-$$-$RANDOM"',
            resume,
        )

    def test_rollback_status_is_a_read_only_existing_generation_probe(self):
        source = CONTROL.read_text(encoding="utf-8")
        status = source.split("instance_lifecycle_status() {", 1)[1].split("\ninstance_lifecycle() {", 1)[0]
        self.assertIn(
            'windows_lifecycle "$output" "$remote_script" "$remote_binding" status',
            status,
        )
        self.assertIn('"$output.transport.json"', source)
        self.assertNotIn("scp ", status)
        self.assertNotIn("New-Item", status)
        self.assertIn(
            'instance_lifecycle_status "$ROLLBACK_ID" \\\n'
            '        > "$RECEIPT_DIR/windows-status.path"',
            source,
        )
        self.assertNotIn('WINDOWS_STATUS="$(instance_lifecycle_status', source)

    def test_no_here_document_is_wrapped_by_command_substitution(self):
        source = CONTROL.read_text(encoding="utf-8")
        lines = source.splitlines()
        for index, line in enumerate(lines):
            if line.lstrip().startswith("#") or "$(" not in line:
                continue
            has_here_document = False
            for candidate in lines[index:]:
                if "<<" in candidate:
                    has_here_document = True
                if ")" in candidate:
                    self.assertFalse(
                        has_here_document,
                        f"here-document command substitution starts at line {index + 1}",
                    )
                    break
        self.assertEqual(here_document_command_substitution_violations(source), [])
        negative = """
reader() {
  cat <<EOF >/dev/null
value
EOF
}
result="$(reader)"
"""
        self.assertEqual(here_document_command_substitution_violations(negative), ["reader"])
        multiline = """
multiline() {
  cat <<EOF >/dev/null
value
EOF
}
result="$(
  multiline
)"
"""
        self.assertEqual(here_document_command_substitution_violations(multiline), ["multiline"])

    def test_here_document_scanner_covers_all_delimiter_forms(self):
        for body in (
            "cat <<EOF\nvalue\nEOF\n",
            "cat <<'EOF'\nvalue\nEOF\n",
            'cat <<"EOF"\nvalue\nEOF\n',
            "cat <<'END MARK'\nvalue\nEND MARK\n",
            "cat <<123\nvalue\n123\n",
            "cat <<-EOF\n\tvalue\n\tEOF\n",
        ):
            with self.subTest(body=body):
                self.assertTrue(has_here_document(body))
        self.assertFalse(has_here_document("cat <<< value\n"))

    def test_lifecycle_functions_with_here_documents_are_not_command_substituted(self):
        source = CONTROL.read_text(encoding="utf-8")
        self.assertIn("read_output_path() {", source)
        self.assertIn('mapfile -t lines < "$source"', source)
        self.assertIn('printf -v "$variable"', source)
        helper = source.split("read_output_path() {", 1)[1].split("\n}\n", 1)[0]
        executable_helper = "\n".join(
            line for line in helper.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn('"$@"', executable_helper)
        self.assertEqual(source.count("> \"$archive_probe\" <<'PY'"), 2)
        self.assertEqual(
            source.count('windows_transport 60 "$output.transport.json" < "$archive_probe"'),
            2,
        )
        for name in (
            "generation_env_declared_value",
            "airflow_record_dag",
            "airflow_here_for_record",
        ):
            body = source.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]
            self.assertFalse(has_here_document(body), name)
        for expression in (
            'WINDOWS_STAGE="$(instance_lifecycle',
            'WINDOWS_PROMOTE="$(instance_lifecycle',
            'WINDOWS_HEALTH="$(instance_lifecycle_health',
            'WINDOWS_STATUS="$(instance_lifecycle_status',
            'WINDOWS_ROLLBACK="$(WINDOWS_RELEASE_OVERRIDE="$PREVIOUS_RELEASE_TRIPLE" \\\n',
        ):
            self.assertNotIn(expression, source)
        self.assertIn(
            'instance_lifecycle stage "$CANDIDATE_ID" \\\n'
            '      > "$RECEIPT_DIR/windows-stage.path"',
            source,
        )
        self.assertIn(
            'instance_lifecycle promote "$CANDIDATE_ID" \\\n'
            '      > "$RECEIPT_DIR/windows-promote.path"',
            source,
        )
        self.assertIn(
            '2> "$EVIDENCE_DIR/windows-health-promote-verify.path.stderr"',
            source,
        )
        self.assertIn(
            '2> "$EVIDENCE_DIR/windows-health-rollback-verify.path.stderr"',
            source,
        )
        self.assertIn(
            'read_output_path "$EVIDENCE_DIR/windows-health-promote-verify.path" WINDOWS_HEALTH \\\n'
            '            || WINDOWS_HEALTH_STATUS=2',
            source,
        )
        self.assertIn(
            'read_output_path "$EVIDENCE_DIR/windows-health-rollback-verify.path" WINDOWS_HEALTH \\\n'
            '            || WINDOWS_HEALTH_STATUS=2',
            source,
        )
        self.assertIn(
            'WINDOWS_RELEASE_OVERRIDE="$PREVIOUS_RELEASE_TRIPLE" \\\n'
            '        instance_lifecycle rollback "$ROLLBACK_ID" "$PREVIOUS_GENERATION_DIR" \\\n'
            '        > "$RECEIPT_DIR/windows-rollback.path" 2> "$RECEIPT_DIR/windows-rollback.path.stderr"',
            source,
        )

    def test_lifecycle_failures_record_out_path_and_stderr_evidence(self):
        source = CONTROL.read_text(encoding="utf-8")
        normalized = re.sub(r"\\\n\s*", " ", source)
        normalized = re.sub(r"\s+", " ", normalized)
        for call in (
            'promotion_step "$CANDIDATE_ID" promote windows-generation failed '
            '"$RECEIPT_DIR/windows-stage.out" "$RECEIPT_DIR/windows-stage.path"',
            'promotion_step "$CANDIDATE_ID" promote windows-generation unknown '
            '"$RECEIPT_DIR/windows-stage.out" "$RECEIPT_DIR/windows-stage.path"',
            'promotion_step "$CANDIDATE_ID" promote windows-switch unknown '
            '"$RECEIPT_DIR/windows-promote.out" "$RECEIPT_DIR/windows-promote.path"',
            'promotion_step "$CANDIDATE_ID" promote windows-switch failed '
            '"$RECEIPT_DIR/windows-promote.out" "$RECEIPT_DIR/windows-promote.path"',
            'promotion_step "$CANDIDATE_ID" promote endpoint-identity failed '
            '"$RECEIPT_DIR/windows-health-endpoint-identity.out" '
            '"$RECEIPT_DIR/windows-health-endpoint-identity.path"',
            'promotion_step "$CANDIDATE_ID" promote endpoint-identity unknown '
            '"$RECEIPT_DIR/windows-health-endpoint-identity.out" '
            '"$RECEIPT_DIR/windows-health-endpoint-identity.path"',
            'promotion_step "$CANDIDATE_ID" promote verify failed '
            '"$EVIDENCE_DIR/verify.out" "$EVIDENCE_DIR/windows-health-promote-verify.path" '
            '"$RECEIPT_DIR/windows-health-promote-verify.out"',
            'promotion_step "$CANDIDATE_ID" promote verify completed '
            '"$EVIDENCE_DIR/verify.out" "$EVIDENCE_DIR/windows-health-promote-verify.path" '
            '"$EVIDENCE_DIR/final-health.json" '
            '"$RECEIPT_DIR/windows-health-promote-verify.out"',
            'promotion_step "$ROLLBACK_ID" rollback rollback-windows unknown '
            '"$RECEIPT_DIR/windows-status.out" "$RECEIPT_DIR/windows-status.path"',
            'promotion_step "$ROLLBACK_ID" rollback rollback-windows unknown '
            '"$RECEIPT_DIR/windows-rollback.out" "$RECEIPT_DIR/windows-rollback.path"',
            'promotion_step "$ROLLBACK_ID" rollback rollback-windows failed '
            '"$RECEIPT_DIR/windows-rollback.out" "$RECEIPT_DIR/windows-rollback.path"',
            'promotion_step "$ROLLBACK_ID" rollback rollback-verify failed '
            '"$EVIDENCE_DIR/rollback-verify.out" '
            '"$EVIDENCE_DIR/windows-health-rollback-verify.path" '
            '"$EVIDENCE_DIR/rollback-health.json" '
            '"$EVIDENCE_DIR/windows-health-rollback-verify-$ROLLBACK_ID.out"',
            'promotion_step "$ROLLBACK_ID" rollback rollback-verify unknown '
            '"$EVIDENCE_DIR/rollback-verify.out" '
            '"$EVIDENCE_DIR/windows-health-rollback-verify.path" '
            '"$EVIDENCE_DIR/rollback-health.json" '
            '"$EVIDENCE_DIR/windows-health-rollback-verify-$ROLLBACK_ID.out"',
            'promotion_step "$ROLLBACK_ID" rollback rollback-verify completed '
            '"$EVIDENCE_DIR/rollback-verify.out" '
            '"$EVIDENCE_DIR/windows-health-rollback-verify.path" '
            '"$EVIDENCE_DIR/rollback-health.json" '
            '"$EVIDENCE_DIR/windows-health-rollback-verify-$ROLLBACK_ID.out"',
        ):
            with self.subTest(call=call):
                self.assertIn(call, normalized)
        for path in (
            "$RECEIPT_DIR/windows-stage.out",
            "$RECEIPT_DIR/windows-stage.path",
            "$RECEIPT_DIR/windows-promote.out",
            "$RECEIPT_DIR/windows-promote.path",
            "$RECEIPT_DIR/windows-health-endpoint-identity.out",
            "$RECEIPT_DIR/windows-health-endpoint-identity.path",
            "$RECEIPT_DIR/windows-health-promote-verify.out",
            "$EVIDENCE_DIR/windows-health-promote-verify.path",
            "$EVIDENCE_DIR/windows-health-rollback-verify.path",
            "$RECEIPT_DIR/windows-status.out",
            "$RECEIPT_DIR/windows-status.path",
            "$RECEIPT_DIR/windows-rollback.out",
            "$RECEIPT_DIR/windows-rollback.path",
        ):
            with self.subTest(path=path):
                self.assertIn(path, source)
        self.assertEqual(
            source.count('2> "$RECEIPT_DIR/windows-stage.path.stderr"'),
            1,
        )
        self.assertEqual(
            source.count('2> "$RECEIPT_DIR/windows-promote.path.stderr"'),
            1,
        )
        self.assertEqual(
            source.count('2> "$RECEIPT_DIR/windows-health-endpoint-identity.path.stderr"'),
            1,
        )
        self.assertEqual(
            source.count('2> "$RECEIPT_DIR/windows-status.path.stderr"'),
            1,
        )
        self.assertEqual(
            source.count('2> "$RECEIPT_DIR/windows-rollback.path.stderr"'),
            1,
        )

    def test_start_refuses_while_the_shared_maintenance_lock_is_held(self):
        state_root = self.root / "state"
        state_root.mkdir()
        marker = self.root / "start-env-loaded"
        (state_root / "resolved.env").write_text(
            f"touch {marker}\n",
            encoding="utf-8",
        )
        lock_dir = state_root / "locks"
        lock_dir.mkdir()
        lock_path = lock_dir / "maintenance.lock"
        lock_path.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.maintenance-lock/v1",
                    "owner_id": "a" * 64,
                }
            ),
            encoding="utf-8",
        )
        descriptor = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run(["start", "--config", str(self.config)])
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "another supported lifecycle operation owns the maintenance lock",
            result.stderr,
        )
        self.assertFalse(marker.exists())

    def test_inherited_lock_requires_the_actual_held_file_descriptor(self):
        state_root = self.root / "state"
        state_root.mkdir()
        (state_root / "resolved.env").write_text("", encoding="utf-8")
        lock_dir = state_root / "locks"
        lock_dir.mkdir()
        lock_path = lock_dir / "maintenance.lock"
        owner = "a" * 64
        lock_path.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.maintenance-lock/v1",
                    "owner_id": owner,
                    "acquired_at": "2026-10-21T00:00:00Z",
                    "pid": os.getpid(),
                }
            ),
            encoding="utf-8",
        )
        held = os.open(lock_path, os.O_RDWR)
        unlocked = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            missing_fd = run(
                ["start", "--config", str(self.config)],
                env={
                    "OPERATORCTL_MAINTENANCE_LOCK_OWNER": owner,
                    "OPERATORCTL_MAINTENANCE_LOCK_PATH": str(lock_path),
                },
            )
            self.assertIn("must carry the caller's file descriptor", missing_fd.stderr)

            unlocked_result = run(
                ["start", "--config", str(self.config)],
                env={
                    "OPERATORCTL_MAINTENANCE_LOCK_OWNER": owner,
                    "OPERATORCTL_MAINTENANCE_LOCK_FD": str(unlocked),
                    "OPERATORCTL_MAINTENANCE_LOCK_PATH": str(lock_path),
                },
                pass_fds=(unlocked,),
            )
            self.assertIn(
                "does not itself hold the write lock",
                unlocked_result.stderr,
            )

            inherited = run(
                ["start", "--config", str(self.config)],
                env={
                    "OPERATORCTL_MAINTENANCE_LOCK_OWNER": owner,
                    "OPERATORCTL_MAINTENANCE_LOCK_FD": str(held),
                    "OPERATORCTL_MAINTENANCE_LOCK_PATH": str(lock_path),
                },
                pass_fds=(held,),
            )
            self.assertIn("maintenance_lock=inherited:", inherited.stdout)
            self.assertNotIn("inherited maintenance lock", inherited.stderr)

            replaced = self.root / "maintenance.lock.replaced"
            os.replace(lock_path, replaced)
            lock_path.write_text(
                json.dumps(
                    {
                        "schema_version": "solidworks-to-urdf.maintenance-lock/v1",
                        "owner_id": owner,
                        "acquired_at": "2026-10-21T00:00:01Z",
                        "pid": os.getpid(),
                    }
                ),
                encoding="utf-8",
            )
            replaced_result = run(
                ["start", "--config", str(self.config)],
                env={
                    "OPERATORCTL_MAINTENANCE_LOCK_OWNER": owner,
                    "OPERATORCTL_MAINTENANCE_LOCK_FD": str(held),
                    "OPERATORCTL_MAINTENANCE_LOCK_PATH": str(lock_path),
                },
                pass_fds=(held,),
            )
            self.assertIn(
                "descriptor is not the held state-root lock",
                replaced_result.stderr,
            )
        finally:
            os.close(unlocked)
            os.close(held)

    def test_lock_acquisition_replaces_stale_owner_metadata(self):
        state_root = self.root / "state"
        state_root.mkdir()
        (state_root / "resolved.env").write_text("", encoding="utf-8")
        lock_dir = state_root / "locks"
        lock_dir.mkdir()
        lock_path = lock_dir / "maintenance.lock"
        lock_path.write_text(
            json.dumps(
                {
                    "schema_version": "solidworks-to-urdf.maintenance-lock/v1",
                    "owner_id": "e50" + "a" * 61,
                    "acquired_at": "2026-10-22T00:00:00Z",
                    "pid": 1,
                }
            ),
            encoding="utf-8",
        )

        result = run(["drain", "--config", str(self.config)])

        self.assertNotEqual(result.returncode, 0)
        metadata = json.loads(lock_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(metadata),
            {"schema_version", "owner_id", "acquired_at", "pid"},
        )
        self.assertEqual(
            metadata["schema_version"],
            "solidworks-to-urdf.maintenance-lock/v1",
        )
        self.assertRegex(metadata["owner_id"], r"\A[0-9a-f]{64}\Z")
        self.assertNotEqual(
            metadata["owner_id"],
            "e50" + "a" * 61,
            result.stdout + result.stderr,
        )
        self.assertRegex(
            metadata["acquired_at"],
            r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z",
        )
        self.assertIs(type(metadata["pid"]), int)
        self.assertGreater(metadata["pid"], 0)

    def test_invalid_config_fails_before_any_verb_behavior(self):
        bad = self.root / "bad.json"
        document = platform_document(self.root)
        document["storage"]["state_root"] = "relative/state"
        bad.write_text(json.dumps(document), encoding="utf-8")
        for verb in ("validate", "install", "promote"):
            with self.subTest(verb):
                result = run([verb, "--config", str(bad)])
                self.assertEqual(result.returncode, 1)
                self.assertIn("platform_config:", result.stderr)

    def test_usage_errors(self):
        self.assertEqual(run([]).returncode, 2)
        self.assertEqual(run(["frobnicate", "--config", str(self.config)]).returncode, 2)
        self.assertEqual(run(["validate"]).returncode, 2)
        self.assertEqual(run(["validate", "--config", str(self.config), "--env-file", str(self.config)]).returncode, 2)
        self.assertEqual(run(["drain", "--env-file", str(self.config)]).returncode, 2)
        self.assertEqual(run(["install", "--env-file", str(self.config)]).returncode, 2)
        self.assertEqual(run(["status", "--config", str(self.config), "--install-id", "x"]).returncode, 2)
        missing = run(["install", "--config", str(self.root / "missing.json")])
        self.assertEqual(missing.returncode, 1)
        self.assertIn("config file not found", missing.stderr)


if __name__ == "__main__":
    unittest.main()
