"""Create and verify the one-way legacy lifecycle cutover archive."""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Any

ARCHIVE_SCHEMA = "solidworks-to-urdf.legacy-lifecycle-archive/v1"
EVIDENCE_SCHEMA = "solidworks-to-urdf.legacy-lifecycle-evidence/v1"
LEGACY_RELATIONS = ("request", "run", "stage", "operation", "run_event")
CANONICAL_TABLES = {
    "admission",
    "run_admission",
    "artifact",
    "command",
    "command_input_artifact",
    "execution_claim",
    "stage_decision",
    "stage_candidate",
    "effect_intent",
    "effect_fact",
    "run_display_metadata",
}
SAFE_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
SAFE_DATABASE = re.compile(r"\A[A-Za-z0-9_.-]+\Z")
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
MEMBER_KEYS = {"bytes", "kind", "sha256"}
RECEIPT_KEYS = {
    "archive_sha256",
    "cluster_system_identifier",
    "database_name",
    "database_oid",
    "evidence_sha256",
    "manifest_sha256",
    "migration_role",
    "platform_schema",
    "platform_schema_oid",
    "schema_version",
}


class ArchiveError(RuntimeError):
    pass


def canonical(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def quote_ident(value: str) -> str:
    if SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ArchiveError(f"unsafe PostgreSQL identifier: {value!r}")
    return '"' + value.replace('"', '""') + '"'


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def run(
    command: list[str],
    *,
    input_bytes: bytes | None = None,
) -> bytes:
    completed = subprocess.run(
        command,
        input=input_bytes,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ArchiveError(
            f"command failed ({completed.returncode}): {command[0]}: {detail[:4096]}"
        )
    return completed.stdout


def connection_args(args) -> list[str]:
    values: list[str] = []
    option_names = {"h": "-h", "p": "-p", "U": "-U"}
    for item in args.libpq:
        key, separator, value = item.partition("=")
        if not separator or key not in option_names:
            raise ArchiveError("the libpq connection option is malformed")
        values.extend((option_names[key], value))
    return values


def psql(args, query: str) -> bytes:
    command = [
        args.psql,
        *connection_args(args),
        "-d",
        args.database,
        "-X",
        "-q",
        "-A",
        "-t",
        "-v",
        "ON_ERROR_STOP=1",
    ]
    for setting in (
        "SET search_path = pg_catalog",
        "SET TIME ZONE 'UTC'",
        "SET DateStyle = 'ISO, YMD'",
        "SET IntervalStyle = 'postgres'",
        "SET bytea_output = 'hex'",
        "SET extra_float_digits = 3",
    ):
        command.extend(("-c", setting))
    command.extend(("-c", query))
    return run(command)


def psql_json(args, query: str) -> Any:
    raw = psql(args, query).strip()
    if not raw:
        raise ArchiveError("PostgreSQL returned an empty evidence value")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArchiveError("PostgreSQL returned malformed evidence JSON") from error


def current_identity(args) -> dict[str, Any]:
    schema_literal = sql_literal(args.platform_schema)
    value = psql_json(
        args,
        f"""
        SELECT jsonb_build_object(
            'cluster_system_identifier', (
                SELECT system_identifier::text FROM pg_control_system()
            ),
            'database_name', current_database(),
            'database_oid', (SELECT oid::text FROM pg_database WHERE datname = current_database()),
            'migration_role', session_user,
            'superuser', (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
            'platform_schema', {schema_literal},
            'platform_schema_oid', (
                SELECT oid::text FROM pg_namespace WHERE nspname = {schema_literal}
            ),
            'server_version_num', current_setting('server_version_num')
        )
        """,
    )
    admission_exists = psql_json(
        args,
        f"SELECT to_jsonb(to_regclass({sql_literal(args.platform_schema + '.admission')}) IS NOT NULL)",
    )
    if admission_exists:
        value["admission_state"] = psql_json(
            args,
            f"""
            SELECT to_jsonb(state::text)
            FROM {quote_ident(args.platform_schema)}.admission
            WHERE id = 1
            """,
        )
    else:
        value["admission_state"] = "absent"
    if value.get("platform_schema_oid") is None:
        raise ArchiveError("the requested platform schema does not exist")
    return value


def relation_rows(args, schema_name: str, name: str) -> list[str]:
    schema = quote_ident(schema_name)
    relation = quote_ident(name)
    value = psql_json(
        args,
        f"""
        SELECT COALESCE(
            jsonb_agg(row_text ORDER BY row_text),
            '[]'::jsonb
        )
        FROM (
            SELECT to_jsonb(relation_value)::text AS row_text
            FROM {schema}.{relation} AS relation_value
        ) AS rows
        """,
    )
    if (
        not isinstance(value, list)
        or any(not isinstance(row, str) for row in value)
    ):
        raise ArchiveError(f"the relation row evidence is malformed: {schema_name}.{name}")
    return value


def relation_catalog(args, name: str) -> dict[str, Any]:
    schema = sql_literal(args.platform_schema)
    relation = sql_literal(name)
    value = psql_json(
        args,
        f"""
        WITH selected AS (
            SELECT c.*
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = {schema} AND c.relname = {relation}
        )
        SELECT jsonb_build_object(
            'name', (SELECT relname FROM selected),
            'kind', (SELECT relkind::text FROM selected),
            'owner', (SELECT pg_get_userbyid(relowner) FROM selected),
            'acl', (SELECT relacl FROM selected),
            'options', (SELECT reloptions FROM selected),
            'comment', (
                SELECT obj_description(oid, 'pg_class') FROM selected
            ),
            'columns', (
                SELECT COALESCE(jsonb_agg(jsonb_build_object(
                    'name', a.attname,
                    'type', format_type(a.atttypid, a.atttypmod),
                    'not_null', a.attnotnull,
                    'identity', a.attidentity,
                    'generated', a.attgenerated,
                    'default', pg_get_expr(d.adbin, d.adrelid),
                    'collation', CASE
                        WHEN a.attcollation = t.typcollation THEN NULL
                        ELSE (SELECT collname FROM pg_collation WHERE oid = a.attcollation)
                    END,
                    'comment', col_description(a.attrelid, a.attnum)
                ) ORDER BY a.attnum), '[]'::jsonb)
                FROM selected AS c
                JOIN pg_attribute AS a ON a.attrelid = c.oid
                JOIN pg_type AS t ON t.oid = a.atttypid
                LEFT JOIN pg_attrdef AS d
                    ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                WHERE a.attnum > 0 AND NOT a.attisdropped
            ),
            'constraints', (
                SELECT COALESCE(jsonb_agg(jsonb_build_object(
                    'name', conname,
                    'type', contype::text,
                    'definition', pg_get_constraintdef(oid)
                ) ORDER BY conname), '[]'::jsonb)
                FROM pg_constraint
                WHERE conrelid = (SELECT oid FROM selected)
            ),
            'indexes', (
                SELECT COALESCE(jsonb_agg(jsonb_build_object(
                    'name', index_class.relname,
                    'definition', pg_get_indexdef(index_class.oid)
                ) ORDER BY index_class.relname), '[]'::jsonb)
                FROM pg_index AS index_row
                JOIN pg_class AS index_class ON index_class.oid = index_row.indexrelid
                WHERE index_row.indrelid = (SELECT oid FROM selected)
            ),
            'triggers', (
                SELECT COALESCE(jsonb_agg(jsonb_build_object(
                    'name', tgname,
                    'enabled', tgenabled::text,
                    'definition', pg_get_triggerdef(oid)
                ) ORDER BY tgname), '[]'::jsonb)
                FROM pg_trigger
                WHERE tgrelid = (SELECT oid FROM selected) AND NOT tgisinternal
            ),
            'policies', (
                SELECT COALESCE(
                    jsonb_agg(to_jsonb(policy_row) ORDER BY policyname),
                    '[]'::jsonb
                )
                FROM pg_policies AS policy_row
                WHERE schemaname = {schema} AND tablename = {relation}
            ),
            'view_definition', (
                SELECT pg_get_viewdef(oid, true)
                FROM selected
                WHERE relkind IN ('v', 'm')
            )
        )
        """,
    )
    return value


def schema_relation_rows(args, schema_name: str) -> dict[str, dict[str, Any]]:
    schema = sql_literal(schema_name)
    relations = psql_json(
        args,
        f"""
        SELECT COALESCE(
            jsonb_agg(jsonb_build_object(
                'name', c.relname,
                'kind', c.relkind::text
            ) ORDER BY c.relname),
            '[]'::jsonb
        )
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = {schema}
          AND c.relkind IN ('r', 'p')
        """,
    )
    if not isinstance(relations, list):
        raise ArchiveError(f"the {schema_name} schema relation inventory is malformed")
    evidence: dict[str, dict[str, Any]] = {}
    for relation in relations:
        name = relation.get("name")
        kind = relation.get("kind")
        if not isinstance(name, str) or SAFE_IDENTIFIER.fullmatch(name) is None:
            raise ArchiveError(f"the {schema_name} schema contains an unsafe relation name")
        evidence[name] = {
            "kind": kind,
            "rows": relation_rows(args, schema_name, name),
        }
    return evidence


def live_evidence(args) -> dict[str, Any]:
    identity = current_identity(args)
    schema = sql_literal(args.platform_schema)
    relations = psql_json(
        args,
        f"""
        SELECT jsonb_agg(jsonb_build_object(
            'name', c.relname,
            'kind', c.relkind::text
        ) ORDER BY c.relname)
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = {schema}
          AND c.relkind IN ('r', 'p', 'v', 'm')
        """,
    )
    if not isinstance(relations, list):
        raise ArchiveError("the platform schema relation inventory is malformed")
    relation_evidence = {}
    for relation in relations:
        name = relation.get("name")
        kind = relation.get("kind")
        if not isinstance(name, str) or SAFE_IDENTIFIER.fullmatch(name) is None:
            raise ArchiveError("the platform schema contains an unsafe relation name")
        entry = {"kind": kind, "catalog": relation_catalog(args, name)}
        if kind in {"r", "p"}:
            entry["rows"] = relation_rows(args, args.platform_schema, name)
        relation_evidence[name] = entry
    schema_catalog = psql_json(
        args,
        f"""
        SELECT jsonb_build_object(
            'owner', pg_get_userbyid(nspowner),
            'acl', nspacl,
            'comment', obj_description(oid, 'pg_namespace')
        )
        FROM pg_namespace
        WHERE nspname = {schema}
        """,
    )
    function_catalog = psql_json(
        args,
        f"""
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'identity', p.oid::regprocedure::text,
            'kind', p.prokind::text,
            'definition', CASE
                WHEN p.prokind = 'f' AND l.lanname NOT IN ('c', 'internal')
                    THEN pg_get_functiondef(p.oid)
                ELSE NULL
            END,
            'owner', pg_get_userbyid(p.proowner),
            'acl', p.proacl
        ) ORDER BY p.oid::regprocedure::text), '[]'::jsonb)
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        JOIN pg_language AS l ON l.oid = p.prolang
        WHERE n.nspname = {schema}
        """,
    )
    type_catalog = psql_json(
        args,
        f"""
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'name', t.typname,
            'kind', t.typtype::text,
            'category', t.typcategory::text,
            'owner', pg_get_userbyid(t.typowner),
            'acl', t.typacl,
            'enum', CASE WHEN t.typtype = 'e' THEN (
                SELECT COALESCE(jsonb_agg(enumlabel ORDER BY enumsortorder), '[]'::jsonb)
                FROM pg_enum WHERE enumtypid = t.oid
            ) ELSE NULL END,
            'domain', CASE WHEN t.typtype = 'd' THEN (
                SELECT jsonb_build_object(
                    'base_type', format_type(t.typbasetype, t.typtypmod),
                    'not_null', t.typnotnull,
                    'default', t.typdefault,
                    'constraints', (
                        SELECT COALESCE(jsonb_agg(jsonb_build_object(
                            'name', conname,
                            'definition', pg_get_constraintdef(oid)
                        ) ORDER BY conname), '[]'::jsonb)
                        FROM pg_constraint WHERE contypid = t.oid
                    )
                )
            ) ELSE NULL END
        ) ORDER BY t.typname), '[]'::jsonb)
        FROM pg_type AS t
        JOIN pg_namespace AS n ON n.oid = t.typnamespace
        WHERE n.nspname = {schema}
          AND (
              t.typtype IN ('d', 'e', 'r', 'm')
              OR (t.typtype = 'b' AND t.typarray <> 0 AND t.typcategory <> 'A')
          )
        """,
    )
    sequence_catalog = psql_json(
        args,
        f"""
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'name', c.relname,
            'owner', pg_get_userbyid(c.relowner),
            'acl', c.relacl,
            'type', format_type(s.seqtypid, NULL),
            'start', s.seqstart,
            'increment', s.seqincrement,
            'minimum', s.seqmin,
            'maximum', s.seqmax,
            'cache', s.seqcache,
            'cycle', s.seqcycle,
            'last_value', (
                SELECT last_value FROM pg_sequences WHERE schemaname = {schema} AND sequencename = c.relname
            )
        ) ORDER BY c.relname), '[]'::jsonb)
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_sequence AS s ON s.seqrelid = c.oid
        WHERE n.nspname = {schema} AND c.relkind = 'S'
        """,
    )
    unresolved = psql_json(
        args,
        f"""
        SELECT jsonb_build_object(
            'legacy_run', CASE WHEN to_regclass({sql_literal(args.platform_schema + '.run')}) IS NULL
                THEN '{{}}'::jsonb ELSE (
                    SELECT jsonb_build_object(
                        'unresolved', count(*) FILTER (WHERE state = 'unresolved'),
                        'active', count(*) FILTER (WHERE state IN ('queued', 'running'))
                    ) FROM {quote_ident(args.platform_schema)}.run
                ) END,
            'legacy_operation', CASE WHEN to_regclass({sql_literal(args.platform_schema + '.operation')}) IS NULL
                THEN '{{}}'::jsonb ELSE (
                    SELECT jsonb_build_object(
                        'unresolved', count(*) FILTER (WHERE state = 'unresolved'),
                        'active', count(*) FILTER (WHERE state IN ('queued', 'running'))
                    ) FROM {quote_ident(args.platform_schema)}.operation
                ) END
        )
        """,
    )
    return {
        "schema_version": EVIDENCE_SCHEMA,
        "identity": identity,
        "schema_catalog": schema_catalog,
        "functions": function_catalog,
        "types": type_catalog,
        "sequences": sequence_catalog,
        "relations": relation_evidence,
        "airflow_relations": schema_relation_rows(args, args.airflow_schema),
        "unresolved": unresolved,
    }


def legacy_present(evidence: dict[str, Any]) -> bool:
    return any(name in evidence["relations"] for name in LEGACY_RELATIONS)


def _write_member(tar: tarfile.TarFile, path: str, value: bytes) -> None:
    member = tarfile.TarInfo(path)
    member.size = len(value)
    member.mode = 0o600
    member.mtime = 0
    member.uid = 0
    member.gid = 0
    member.uname = ""
    member.gname = ""
    tar.addfile(member, io.BytesIO(value))


def _archive_members(
    args,
    evidence_bytes: bytes,
    dump_bytes: bytes,
    schema_sql: bytes,
    toc: bytes,
) -> dict[str, tuple[str, bytes]]:
    return {
        "evidence.json": ("evidence", evidence_bytes),
        "platform.dump": ("postgres-custom-dump", dump_bytes),
        "schema.sql": ("schema-only-sql", schema_sql),
        "toc.txt": ("pg-restore-toc", toc),
    }


def _manifest(members: dict[str, tuple[str, bytes]], evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": ARCHIVE_SCHEMA,
        "cluster_system_identifier": evidence["identity"]["cluster_system_identifier"],
        "database_name": evidence["identity"]["database_name"],
        "database_oid": evidence["identity"]["database_oid"],
        "platform_schema": evidence["identity"]["platform_schema"],
        "platform_schema_oid": evidence["identity"]["platform_schema_oid"],
        "members": {
            path: {
                "kind": kind,
                "bytes": len(value),
                "sha256": sha256_bytes(value),
            }
            for path, (kind, value) in sorted(members.items())
        },
    }


def _write_archive(path: Path, members: dict[str, tuple[str, bytes]], manifest: dict[str, Any]) -> None:
    raw = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        _write_member(tar, "manifest.json", canonical(manifest))
        for name, (_kind, value) in sorted(members.items()):
            _write_member(tar, name, value)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw.getvalue())
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o600)
        if path.exists():
            raise ArchiveError(f"archive already exists: {path}")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _read_archive(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    values: dict[str, bytes] = {}
    with tarfile.open(path, mode="r:gz") as tar:
        members = tar.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ArchiveError("the archive contains duplicate members")
        for member in members:
            if not member.isfile() or member.name.startswith("/") or ".." in Path(member.name).parts:
                raise ArchiveError(f"the archive member is unsafe: {member.name!r}")
            stream = tar.extractfile(member)
            if stream is None:
                raise ArchiveError(f"the archive member cannot be read: {member.name!r}")
            values[member.name] = stream.read()
    manifest_bytes = values.pop("manifest.json", None)
    if manifest_bytes is None:
        raise ArchiveError("the archive has no manifest")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as error:
        raise ArchiveError("the archive manifest is malformed") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != ARCHIVE_SCHEMA
        or not isinstance(manifest.get("members"), dict)
        or set(manifest["members"]) != set(values)
    ):
        raise ArchiveError("the archive manifest shape is invalid")
    for name, metadata in manifest["members"].items():
        value = values[name]
        if (
            not isinstance(metadata, dict)
            or set(metadata) != MEMBER_KEYS
            or metadata.get("bytes") != len(value)
            or metadata.get("sha256") != sha256_bytes(value)
        ):
            raise ArchiveError(f"the archive member differs from its manifest: {name}")
    return manifest, values


def validate_maintenance_lock() -> None:
    owner = os.environ.get("OPERATORCTL_MAINTENANCE_LOCK_OWNER", "")
    descriptor_text = os.environ.get("OPERATORCTL_MAINTENANCE_LOCK_FD", "")
    path_text = os.environ.get("OPERATORCTL_MAINTENANCE_LOCK_PATH", "")
    if not owner or not descriptor_text.isdigit() or not path_text:
        raise ArchiveError("legacy archive requires an inherited maintenance lock")
    descriptor = int(descriptor_text)
    path = Path(path_text)
    if path.is_symlink() or not path.is_file():
        raise ArchiveError("the inherited maintenance lock is not a regular file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ArchiveError("the inherited maintenance lock owner is unreadable") from error
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "owner_id", "acquired_at", "pid"}
        or document.get("schema_version") != "solidworks-to-urdf.maintenance-lock/v1"
        or document.get("owner_id") != owner
    ):
        raise ArchiveError("the inherited maintenance lock owner is invalid")
    inherited = os.fstat(descriptor)
    current = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode) or (
        inherited.st_dev,
        inherited.st_ino,
    ) != (current.st_dev, current.st_ino):
        raise ArchiveError("the inherited maintenance descriptor names another file")
    try:
        fdinfo = Path(f"/proc/self/fdinfo/{descriptor}").read_text(encoding="utf-8")
    except OSError as error:
        raise ArchiveError("the maintenance lock descriptor cannot be inspected") from error
    if re.search(
        r"^lock:\s+\d+:\s+FLOCK\s+ADVISORY\s+WRITE\b",
        fdinfo,
        flags=re.MULTILINE,
    ) is None:
        raise ArchiveError("the inherited descriptor does not hold the write lock")


def _run_dump(args, dump_path: Path) -> None:
    run(
        [
            args.pg_dump,
            *connection_args(args),
            "-d",
            args.database,
            "--format=custom",
            f"--schema={args.platform_schema}",
            f"--schema={args.airflow_schema}",
            "--compress=9",
            f"--file={dump_path}",
        ]
    )


def _dump_schema(args) -> bytes:
    return run(
        [
            args.pg_dump,
            *connection_args(args),
            "-d",
            args.database,
            "--schema-only",
            "--format=plain",
            f"--schema={args.platform_schema}",
            f"--schema={args.airflow_schema}",
        ]
    )


def _restore_text(args, dump_path: Path, *options: str) -> bytes:
    return run(
        [
            args.pg_restore,
            "--file=-",
            *options,
            str(dump_path),
        ]
    )


def _first_difference(expected: Any, actual: Any, path: str = "$") -> str:
    if type(expected) is not type(actual):
        return f"{path}: type {type(expected).__name__} != {type(actual).__name__}"
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                return f"{path}.{key}: unexpected"
            if key not in actual:
                return f"{path}.{key}: missing"
            if expected[key] != actual[key]:
                return _first_difference(expected[key], actual[key], f"{path}.{key}")
        return path
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}: length {len(expected)} != {len(actual)}"
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            if left != right:
                return _first_difference(left, right, f"{path}[{index}]")
        return path
    return f"{path}: {expected!r} != {actual!r}"


def _restore_full(
    args,
    dump_path: Path,
    schema_sql: bytes,
    evidence: dict[str, Any],
) -> None:
    socket_handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    socket_handle.bind(("127.0.0.1", 0))
    port = socket_handle.getsockname()[1]
    socket_handle.close()
    with tempfile.TemporaryDirectory(prefix="legacy-cutover-restore-") as directory:
        root = Path(directory)
        data = root / "data"
        sockets = root / "socket"
        sockets.mkdir()
        log = root / "postgres.log"
        run(
            [
                args.initdb,
                "-D",
                str(data),
                "--auth=trust",
                "--encoding=UTF8",
                "--no-locale",
                "--username=restore_admin",
            ]
        )
        started = False
        try:
            run(
                [
                    args.pg_ctl,
                    "-D",
                    str(data),
                    "-l",
                    str(log),
                    "-o",
                    f"-F -k {sockets} -p {port} -c listen_addresses=''",
                    "-w",
                    "start",
                ]
            )
            started = True
            database = "legacy_restore"
            connection = [
                "-h",
                str(sockets),
                "-p",
                str(port),
                "-U",
                "restore_admin",
            ]
            run(
                [
                    args.createdb,
                    *connection,
                    database,
                ]
            )
            source_roles = psql_json(
                args,
                """
                SELECT jsonb_agg(rolname ORDER BY rolname)
                FROM pg_roles
                WHERE rolname !~ '^pg_' AND rolname <> 'restore_admin'
                """,
            )
            for role in source_roles or []:
                if not isinstance(role, str):
                    raise ArchiveError("the source role inventory is malformed")
                run(
                    [
                        args.psql,
                        *connection,
                        "-d",
                        database,
                        "-X",
                        "-q",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-c",
                        f"CREATE ROLE {quote_ident(role)} NOLOGIN;",
                    ]
                )
            run(
                [
                    args.pg_restore,
                    *connection,
                    "--dbname",
                    database,
                    "--exit-on-error",
                    "--single-transaction",
                    str(dump_path),
                ]
            )
            restored_args = argparse.Namespace(
                **{
                    **vars(args),
                    "libpq": [
                        "h=" + str(sockets),
                        "p=" + str(port),
                        "U=restore_admin",
                    ],
                    "database": database,
                }
            )
            restored = live_evidence(restored_args)
            for field in (
                "schema_catalog",
                "functions",
                "types",
                "sequences",
                "relations",
                "airflow_relations",
                "unresolved",
            ):
                if restored[field] != evidence[field]:
                    difference = _first_difference(evidence[field], restored[field])
                    raise ArchiveError(
                        f"the independently restored archive differs in {field}: {difference}"
                    )
            restored_schema_sql = _dump_schema(restored_args)
            if restored_schema_sql != schema_sql:
                raise ArchiveError(
                    "the independently restored archive differs in complete schema or ACL SQL"
                )
        finally:
            if started:
                subprocess.run(
                    [
                        args.pg_ctl,
                        "-D",
                        str(data),
                        "-m",
                        "immediate",
                        "-w",
                        "stop",
                    ],
                    check=False,
                    capture_output=True,
                )


def _verify_relation_data_toc(toc: bytes, relations: dict[str, Any], schema: str) -> None:
    text = toc.decode("utf-8", errors="replace")
    for name, relation in relations.items():
        if relation["kind"] not in {"r", "p"} or not relation["rows"]:
            continue
        pattern = re.compile(
            rf"\bTABLE DATA\s+\"?{re.escape(schema)}\"?\s+\"?{re.escape(name)}\"?\b"
        )
        if pattern.search(text) is None:
            raise ArchiveError(f"the dump TOC lacks table data: {name}")


def _load_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ArchiveError("the archive receipt is not valid JSON") from error
    if not isinstance(value, dict) or set(value) != RECEIPT_KEYS:
        raise ArchiveError("the archive receipt has an invalid shape")
    if value.get("schema_version") != ARCHIVE_SCHEMA:
        raise ArchiveError("the archive receipt schema is unsupported")
    for key in (
        "archive_sha256",
        "evidence_sha256",
        "manifest_sha256",
    ):
        if not isinstance(value.get(key), str) or HEX64.fullmatch(value[key]) is None:
            raise ArchiveError(f"the archive receipt field is invalid: {key}")
    for key in ("cluster_system_identifier", "database_oid", "platform_schema_oid"):
        if not isinstance(value.get(key), str) or not value[key].isdigit():
            raise ArchiveError(f"the archive receipt field is invalid: {key}")
    return value


def _verify_existing(args, receipt_path: Path, archive_path: Path, evidence: dict[str, Any]):
    if (
        receipt_path.is_symlink()
        or not receipt_path.is_file()
        or archive_path.is_symlink()
        or not archive_path.is_file()
    ):
        raise ArchiveError("the archive receipt or archive is not a regular file")
    receipt = _load_receipt(receipt_path)
    identity = evidence["identity"]
    if (
        receipt["database_name"],
        receipt["database_oid"],
        receipt["cluster_system_identifier"],
        receipt["migration_role"],
        receipt["platform_schema"],
        receipt["platform_schema_oid"],
    ) != (
        identity["database_name"],
        identity["database_oid"],
        identity["cluster_system_identifier"],
        identity["migration_role"],
        identity["platform_schema"],
        identity["platform_schema_oid"],
    ):
        raise ArchiveError(
            "the archive receipt belongs to another cluster, database, or schema"
        )
    if sha256_file(archive_path) != receipt["archive_sha256"]:
        raise ArchiveError("the archive digest differs from its receipt")
    manifest, values = _read_archive(archive_path)
    if sha256_bytes(canonical(manifest)) != receipt["manifest_sha256"]:
        raise ArchiveError("the archive manifest differs from its receipt")
    evidence_bytes = values["evidence.json"]
    if sha256_bytes(evidence_bytes) != receipt["evidence_sha256"]:
        raise ArchiveError("the archived evidence differs from its receipt")
    try:
        archived_evidence = json.loads(evidence_bytes)
    except json.JSONDecodeError as error:
        raise ArchiveError("the archived evidence is malformed") from error
    if not isinstance(archived_evidence, dict):
        raise ArchiveError("the archived evidence has an invalid shape")
    archived_identity = archived_evidence.get("identity", {})
    if not isinstance(archived_identity, dict):
        raise ArchiveError("the archived evidence identity is malformed")
    if any(
        manifest.get(key) != archived_identity.get(key)
        for key in (
            "cluster_system_identifier",
            "database_name",
            "database_oid",
            "platform_schema",
            "platform_schema_oid",
        )
    ):
        raise ArchiveError("the archive manifest identity differs from its evidence")
    if _dump_schema(args) != values["schema.sql"]:
        raise ArchiveError(
            "the live legacy schema or ACL SQL differs from the archived snapshot"
        )
    if archived_evidence != evidence:
        raise ArchiveError("live legacy facts differ from the archived snapshot")
    _verify_relation_data_toc(values["toc.txt"], evidence["relations"], args.platform_schema)
    _verify_relation_data_toc(
        values["toc.txt"],
        evidence["airflow_relations"],
        args.airflow_schema,
    )
    with tempfile.TemporaryDirectory(prefix="legacy-cutover-restore-") as directory:
        dump_path = Path(directory) / "platform.dump"
        dump_path.write_bytes(values["platform.dump"])
        _restore_full(
            args,
            dump_path,
            values["schema.sql"],
            archived_evidence,
        )
    return receipt


def ensure(args) -> tuple[str, dict[str, Any] | None]:
    initial = live_evidence(args)
    if not legacy_present(initial):
        return "no-legacy", None
    if initial["identity"]["admission_state"] not in {"absent", "closed"}:
        raise ArchiveError("legacy archive requires the admission gate to be closed")
    validate_maintenance_lock()
    receipt_path = Path(args.receipt)
    if receipt_path.is_symlink():
        raise ArchiveError("the archive receipt may not be a symbolic link")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    if (
        not receipt_path.parent.is_dir()
        or receipt_path.parent.is_symlink()
        or any(parent.is_symlink() for parent in receipt_path.parent.parents)
    ):
        raise ArchiveError("the archive receipt parent is not a real directory")
    archive_path = receipt_path.with_name(receipt_path.name + ".archive.tar.gz")
    if receipt_path.exists() != archive_path.exists():
        raise ArchiveError("the archive and receipt must exist together")
    if receipt_path.exists():
        receipt = _verify_existing(args, receipt_path, archive_path, initial)
        current = live_evidence(args)
        if current != initial:
            raise ArchiveError("legacy facts changed while the archive was verified")
        return "verified", receipt
    if archive_path.exists():
        raise ArchiveError("an archive exists without its receipt")
    with tempfile.TemporaryDirectory(prefix="legacy-cutover-dump-", dir=receipt_path.parent) as directory:
        dump_path = Path(directory) / "platform.dump"
        _run_dump(args, dump_path)
        dump_bytes = dump_path.read_bytes()
        toc = _restore_text(args, dump_path, "--list")
        schema_sql = _dump_schema(args)
    final = live_evidence(args)
    if final != initial:
        raise ArchiveError("legacy facts changed while the archive was created")
    evidence_bytes = canonical(final)
    members = _archive_members(
        args,
        evidence_bytes,
        dump_bytes,
        schema_sql,
        toc,
    )
    manifest = _manifest(members, final)
    _write_archive(archive_path, members, manifest)
    _verify_relation_data_toc(toc, final["relations"], args.platform_schema)
    _verify_relation_data_toc(toc, final["airflow_relations"], args.airflow_schema)
    with tempfile.TemporaryDirectory(prefix="legacy-cutover-restore-") as directory:
        restored_dump = Path(directory) / "platform.dump"
        restored_dump.write_bytes(dump_bytes)
        _restore_full(args, restored_dump, schema_sql, final)
    receipt = {
        "archive_sha256": sha256_file(archive_path),
        "cluster_system_identifier": final["identity"]["cluster_system_identifier"],
        "database_name": final["identity"]["database_name"],
        "database_oid": final["identity"]["database_oid"],
        "evidence_sha256": sha256_bytes(evidence_bytes),
        "manifest_sha256": sha256_bytes(canonical(manifest)),
        "migration_role": final["identity"]["migration_role"],
        "platform_schema": final["identity"]["platform_schema"],
        "platform_schema_oid": final["identity"]["platform_schema_oid"],
        "schema_version": ARCHIVE_SCHEMA,
    }
    temporary = receipt_path.with_name(f".{receipt_path.name}.{uuid.uuid4().hex}")
    temporary.write_bytes(canonical(receipt))
    temporary.chmod(0o600)
    if receipt_path.exists():
        raise ArchiveError("the archive receipt appeared concurrently")
    os.replace(temporary, receipt_path)
    _verify_existing(args, receipt_path, archive_path, final)
    after = live_evidence(args)
    if after != final:
        raise ArchiveError("legacy facts changed after the archive was verified")
    return "created", receipt


def _db_connection(args):
    import psycopg2

    values: dict[str, str] = {"dbname": args.database}
    names = {"h": "host", "p": "port", "U": "user"}
    for item in args.libpq:
        key, separator, value = item.partition("=")
        if not separator or key not in names:
            raise ArchiveError("the libpq connection option is malformed")
        values[names[key]] = value
    return psycopg2.connect(**values)


def _render_schema_sql(path: Path, args) -> str:
    text = path.read_text(encoding="utf-8")
    return (
        text.replace(':"platform_schema"', quote_ident(args.platform_schema))
        .replace(':"airflow_schema"', quote_ident(args.airflow_schema))
        .replace(':"platform_database"', quote_ident(args.database))
    )


def lock_cutover_relations(cursor, platform_schema: str, airflow_schema: str) -> None:
    cursor.execute("SET LOCAL lock_timeout = '30s'")
    cursor.execute(
        """
        LOCK TABLE
            pg_catalog.pg_class,
            pg_catalog.pg_namespace,
            pg_catalog.pg_type,
            pg_catalog.pg_proc,
            pg_catalog.pg_rewrite
        IN SHARE ROW EXCLUSIVE MODE
        """
    )
    cursor.execute(
        """
        SELECT n.nspname, c.relname
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname IN (%s, %s) AND c.relkind IN ('r', 'p')
        ORDER BY n.nspname, c.relname
        """,
        (platform_schema, airflow_schema),
    )
    table_names = cursor.fetchall()
    if not any(schema == platform_schema for schema, _name in table_names):
        raise ArchiveError("the platform schema has no tables to freeze")
    sealed = ", ".join(
        f"{quote_ident(schema)}.{quote_ident(name)}"
        for schema, name in table_names
    )
    cursor.execute(f"LOCK TABLE {sealed} IN EXCLUSIVE MODE")
    cursor.execute(
        """
        SELECT c.oid
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = %s
        ORDER BY c.oid
        FOR UPDATE OF c
        """,
        (platform_schema,),
    )
    cursor.fetchall()
    cursor.execute(
        """
        SELECT n.oid
        FROM pg_namespace AS n
        WHERE n.nspname = %s
        FOR UPDATE
        """,
        (platform_schema,),
    )
    cursor.fetchone()
    cursor.execute(
        """
        SELECT r.oid
        FROM pg_rewrite AS r
        JOIN pg_class AS c ON c.oid = r.ev_class
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = %s
        ORDER BY r.oid
        FOR UPDATE OF r
        """,
        (platform_schema,),
    )
    cursor.fetchall()
    cursor.execute(
        """
        SELECT p.oid
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname = %s
        ORDER BY p.oid
        FOR UPDATE OF p
        """,
        (platform_schema,),
    )
    cursor.fetchall()


def verify_canonical_cutover_state(cursor, platform_schema: str) -> None:
    cursor.execute(
        """
        SELECT c.relname, c.relkind::text
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm')
        ORDER BY c.relname
        """,
        (platform_schema,),
    )
    relations = dict(cursor.fetchall())
    expected = dict.fromkeys(CANONICAL_TABLES, "r")
    expected.update(
        {
            "admission_projection": "v",
            "lifecycle_projection": "v",
        }
    )
    if relations != expected:
        raise ArchiveError(
            "transactional cutover did not produce the exact canonical relation set: "
            f"unexpected={sorted(set(relations) - set(expected))}, "
            f"missing={sorted(set(expected) - set(relations))}"
        )
    cursor.execute(
        """
        SELECT COALESCE(jsonb_agg(p.proname ORDER BY p.proname), '[]'::jsonb)
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname = %s
        """,
        (platform_schema,),
    )
    functions = cursor.fetchone()[0]
    if functions != ["reject_immutable_fact_mutation"]:
        raise ArchiveError(
            "transactional cutover did not preserve the exact canonical function set"
        )
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM pg_type AS t
        JOIN pg_namespace AS n ON n.oid = t.typnamespace
        WHERE n.nspname = %s
          AND (
              t.typtype IN ('d', 'e', 'r', 'm')
              OR (t.typtype = 'b' AND t.typarray <> 0 AND t.typcategory <> 'A')
          )
        """,
        (platform_schema,),
    )
    if cursor.fetchone()[0] != 0:
        raise ArchiveError(
            "transactional cutover retained an unexpected user-defined type"
        )
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind = 'S'
        """,
        (platform_schema,),
    )
    if cursor.fetchone()[0] != 0:
        raise ArchiveError(
            "transactional cutover retained an unexpected sequence"
        )


def apply_cutover(args, receipt: dict[str, Any]) -> None:
    connection = _db_connection(args)
    try:
        with connection:
            cursor = connection.cursor()
            cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            cursor.execute(
                "SELECT set_config('search_path', %s, true)",
                (
                    f"{quote_ident(args.platform_schema)},{quote_ident(args.airflow_schema)},pg_catalog",
                ),
            )
            cursor.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
            )
            superuser = cursor.fetchone()
            if superuser is None or superuser[0] is not True:
                raise ArchiveError(
                    "legacy cutover requires the one-time database administrator identity"
                )
            lock_cutover_relations(
                cursor,
                args.platform_schema,
                args.airflow_schema,
            )
            locked_evidence = live_evidence(args)
            _verify_existing(
                args,
                Path(args.receipt),
                Path(args.receipt + ".archive.tar.gz"),
                locked_evidence,
            )
            for name, value in (
                ("description.legacy_receipt_sha256", sha256_file(Path(args.receipt))),
                ("description.legacy_archive_sha256", receipt["archive_sha256"]),
                ("description.legacy_evidence_sha256", receipt["evidence_sha256"]),
                (
                    "description.legacy_archive_cluster_system_identifier",
                    receipt["cluster_system_identifier"],
                ),
                ("description.legacy_archive_database", receipt["database_name"]),
                ("description.legacy_archive_database_oid", receipt["database_oid"]),
                ("description.legacy_archive_platform_schema", receipt["platform_schema"]),
                (
                    "description.legacy_archive_platform_schema_oid",
                    receipt["platform_schema_oid"],
                ),
                (
                    "description.legacy_archive_migration_role",
                    receipt["migration_role"],
                ),
            ):
                cursor.execute(
                    "SELECT set_config(%s, %s, true)",
                    (name, value),
                )
            cursor.execute("SET LOCAL ROLE platform_migrate")
            for path in (
                Path(args.schema_file),
                Path(args.views_file),
                Path(args.roles_file),
            ):
                cursor.execute(_render_schema_sql(path, args))
            verify_canonical_cutover_state(cursor, args.platform_schema)
    except Exception as error:
        try:
            connection.rollback()
        finally:
            connection.close()
        if isinstance(error, ArchiveError):
            raise
        raise ArchiveError(
            f"transactional cutover failed: {type(error).__name__}: {error}"
        ) from error
    else:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("ensure", "prepare"))
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--psql", required=True)
    parser.add_argument("--pg-dump", required=True)
    parser.add_argument("--pg-restore", required=True)
    parser.add_argument("--initdb", required=True)
    parser.add_argument("--pg-ctl", required=True)
    parser.add_argument("--createdb", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--platform-schema", required=True)
    parser.add_argument("--airflow-schema", required=True)
    parser.add_argument("--schema-file", required=True)
    parser.add_argument("--views-file", required=True)
    parser.add_argument("--roles-file", required=True)
    parser.add_argument("--libpq", action="append", default=[])
    args = parser.parse_args()
    if SAFE_DATABASE.fullmatch(args.database) is None:
        raise ArchiveError("the database name is unsafe")
    if SAFE_IDENTIFIER.fullmatch(args.platform_schema) is None:
        raise ArchiveError("the platform schema name is unsafe")
    if SAFE_IDENTIFIER.fullmatch(args.airflow_schema) is None:
        raise ArchiveError("the airflow schema name is unsafe")
    try:
        status, receipt = ensure(args)
        if receipt is not None:
            if SAFE_IDENTIFIER.fullmatch(receipt["migration_role"]) is None:
                raise ArchiveError("the migration role name is unsafe")
            if args.mode == "ensure":
                apply_cutover(args, receipt)
                status += "-cutover"
            else:
                status += "-ready"
    except (ArchiveError, OSError) as error:
        print(f"legacy cutover archive rejected: {error}", file=sys.stderr)
        return 2
    if receipt is None:
        print("\t".join(("no-legacy", *("" for _ in range(10)))))
    else:
        print(
            "\t".join(
                (
                    status,
                    receipt["evidence_sha256"],
                    receipt["archive_sha256"],
                    receipt["database_name"],
                    receipt["database_oid"],
                    receipt["cluster_system_identifier"],
                    receipt["platform_schema"],
                    receipt["platform_schema_oid"],
                    receipt["manifest_sha256"],
                    receipt["migration_role"],
                    sha256_file(Path(args.receipt)),
                )
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
