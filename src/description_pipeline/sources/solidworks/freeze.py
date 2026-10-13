"""Capture a complete SolidWorks source into an immutable snapshot.

The adapter never saves over the original design: it reads a *copy* produced by
SolidWorks' own dependency collector, proves that the copy's references resolve
inside the snapshot, records the raw API readings and geometry, and only then
commits the snapshot.

Nothing here derives values from a final URDF or from hand-written XML: every
number in ``raw/`` comes from the CAD API during this capture.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import contextlib
from pathlib import Path
from typing import Any
from collections.abc import Callable, Sequence

from .errors import BridgeError, ConfigError
from .native import session_materialization_path
from .evidence import build_evidence, capture_environment
from .jsonio import digest_json, sha256_file, write_json
# The native adapter binds exported bytes; full mesh analysis runs on Linux.
from ...io import inventory
from ...schemas import EFFECTIVE_INSTANCES_SCHEMA, LINK_GROUP_MASS_SCHEMA

#: Freeze/capture record ids now live in the ``solidworks-to-urdf.*`` family.
#: Retained evidence written before the XC-01 rename carries the retired
#: ``description-pipeline.*`` ids; readers that assert one of these ids must
#: accept both spellings (mapping recorded in the rename PR).
FREEZE_SCHEMA = "solidworks-to-urdf.freeze/v1"
SOURCE_KIND = "solidworks"
DEFAULT_ALLOWED_SUFFIXES = (".sldasm", ".sldprt")
# Every key the adapter understands; anything else is a typo or an unimplemented
# feature and must fail instead of being ignored.
SOURCE_KEYS = {
    "provider",
    "assembly",
    "configuration",
    "allowed_roots",
    "closure",
    "document_suffixes",
    "geometry",
    "coordinate_systems",
    "require_saved",
    "bodies",
    "joints",
    "frames",
    "robot_name",
    "material_source",
    "documented_masses",
    "mass_evidence",
    "geometry_exclusions",
    "expected_mass_kg",
    "expected_extent_m",
    "evidence_class",
    "worker_url",
    "allow_remote_worker",
    "worker_http_timeout_seconds",
    "worker_poll_seconds",
    "worker_job_timeout_seconds",
    "job_id",
}
EVIDENCE_CLASSES = ("cad", "fixture", "imported")
FRAME_KEYS = {"xyz", "rpy", "coordinate_system"}
DOCUMENTED_MASS_KEYS = {"mass_kg", "reason", "evidence"}
MASS_EVIDENCE_KEYS = {"reference", "file", "sha256", "note"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _mass_evidence_binding(value: Any, *, required: bool) -> dict[str, str] | str | None:
    """质量证据必须归档成模型仓库里的文件并按内容摘要绑定。

    合同（``documented_masses`` 非空时 ``required=True``）：

    * ``source.mass_evidence`` 是对象：``reference``（人读说明）+ ``file``（模型仓库内相对
      路径）+ ``sha256``（小写 64 位十六进制）；只允许这四个键；
    * 每条 ``documented_masses[].evidence`` 是**该文件内的定位锚点**——独立校验会确认
      这个字符串确实出现在归档文件里；
    * 不声明文档化质量时，仍接受一段说明字符串（此时材料来自 CAD，不是声明值）。

    文件是否存在、是否越界、摘要是否一致由独立校验在模型仓库根目录上核对：本函数运行在
    采集侧（Windows worker），那里没有模型仓库，因此只校验形状。
    """

    if value is None or value == "":
        _require(
            not required,
            "documented masses require source.mass_evidence with file + sha256",
            {"mass_evidence": value},
        )
        return None
    if isinstance(value, str):
        _require(
            not required,
            "source.mass_evidence must bind an archived file (file + sha256), not a bare string",
            {"mass_evidence": value},
        )
        _require(value.strip() != "", "source.mass_evidence must be a non-empty reference when given", value)
        return value
    _require(isinstance(value, dict), "source.mass_evidence must be a mapping or a note string", value)
    assert isinstance(value, dict)
    unknown = sorted(set(value) - MASS_EVIDENCE_KEYS)
    _require(not unknown, "source.mass_evidence has unknown fields", {"unknown": unknown})
    reference = value.get("reference")
    _require(
        isinstance(reference, str) and reference.strip() != "",
        "source.mass_evidence.reference must be a non-empty description",
        {"reference": reference},
    )
    file = value.get("file")
    _require(
        isinstance(file, str) and file.strip() != "",
        "source.mass_evidence.file must be a non-empty model-repo-relative path",
        {"file": file},
    )
    checksum = value.get("sha256")
    _require(
        isinstance(checksum, str) and SHA256_RE.match(checksum) is not None,
        "source.mass_evidence.sha256 must be a lowercase 64-hex digest",
        {"sha256": checksum},
    )
    return {key: str(value[key]) for key in ("reference", "file", "sha256")} | (
        {"note": str(value["note"])} if isinstance(value.get("note"), str) else {}
    )


def _shared_manifest_writer() -> Callable[..., dict[str, Any]]:
    """The pipeline-wide snapshot helper; owned by the integration task."""

    from ..snapshot import write_manifest

    return write_manifest


def _require(condition: bool, message: str, detail: object | None = None) -> None:
    if not condition:
        raise ConfigError(message, detail)


def _normalise_path(value: str) -> str:
    return os.path.normcase(os.path.abspath(value))


def _expected_range(value: Any, where: str) -> list[float] | None:
    """Validate one explicit design expectation ``[min, max]`` in SI units."""

    if value is None:
        return None
    _require(
        isinstance(value, (list, tuple)) and len(value) == 2,
        f"{where} must be a two-element [min, max] range",
        value,
    )
    assert isinstance(value, (list, tuple))
    low, high = value
    _require(
        all(
            isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item))
            for item in (low, high)
        ),
        f"{where} must contain finite numbers",
        value,
    )
    _require(float(low) > 0.0 and float(high) > 0.0, f"{where} must be positive", value)
    _require(float(low) <= float(high), f"{where} min must not exceed max", value)
    return [float(low), float(high)]


def _prepare_destination(destination: Path) -> bool:
    """Accept a missing or *empty* destination directory; refuse anything else.

    The shared pipeline hands over a directory it already created (for example
    ``tempfile.mkdtemp``), so "exists" is not an error by itself - a non-empty
    directory, a file or a symlink is.

    Returns True when the caller already owned an empty directory.
    """

    if destination.is_symlink():
        raise BridgeError(
            "destination_is_symlink",
            "refusing to write a snapshot through a symlink",
            {"destination": str(destination)},
            exit_code=1,
        )
    if not destination.exists():
        return False
    if not destination.is_dir():
        raise BridgeError(
            "destination_not_a_directory",
            "the snapshot destination exists and is not a directory",
            {"destination": str(destination)},
            exit_code=1,
        )
    if any(destination.iterdir()):
        raise BridgeError(
            "destination_not_empty",
            "the snapshot destination must be empty",
            {"destination": str(destination)},
            exit_code=1,
        )
    return True


def _under(path: str, roots: list[str]) -> bool:
    candidate = _normalise_path(path)
    for root in roots:
        root_path = _normalise_path(root)
        if candidate == root_path or candidate.startswith(root_path.rstrip(os.sep) + os.sep):
            return True
    return False


def validate_source_config(config: object) -> dict[str, Any]:
    """Validate the ``source`` mapping before anything touches SolidWorks."""

    _require(isinstance(config, dict), "source config must be a mapping")
    assert isinstance(config, dict)
    unknown = sorted(set(config) - SOURCE_KEYS)
    _require(not unknown, "source config has unknown keys", unknown)
    _require(config.get("require_saved", True) is True, "source.require_saved cannot be disabled for a frozen snapshot")
    provider = str(config.get("provider") or SOURCE_KIND)
    _require(provider == SOURCE_KIND, f"this adapter only serves provider=solidworks, got {provider!r}")
    assembly = config.get("assembly")
    _require(
        isinstance(assembly, str) and assembly.strip() != "",
        "source.assembly must be the absolute path of the top-level SLDASM",
    )
    suffixes = tuple(str(item).lower() for item in config.get("document_suffixes") or DEFAULT_ALLOWED_SUFFIXES)
    _require(".sldasm" in suffixes, "source.document_suffixes must include .sldasm")
    configuration = config.get("configuration")
    _require(
        isinstance(configuration, str) and configuration.strip() != "",
        "source.configuration must be explicit; the adapter never guesses the active configuration",
    )
    allowed_roots = config.get("allowed_roots") or []
    _require(isinstance(allowed_roots, list), "source.allowed_roots must be a list of directories")
    closure = config.get("closure")
    if closure is not None:
        # Internal carrier: the sealed native closure travels with the source
        # config so the freeze stage can bind the admitted resolution (graph
        # owner documents, cached materializations, suppressed placements)
        # instead of pre-listing saved author paths.
        _require(isinstance(closure, dict), "source.closure must be the sealed native closure mapping")
        _require(
            closure.get("schema_version") in (None, "solidworks-to-urdf.native-closure/v1"),
            "source.closure must carry the native closure schema",
        )
        for name in ("graph", "documents", "imports", "suppressed"):
            if name in closure:
                _require(isinstance(closure[name], (dict, list)), f"source.closure.{name} has the wrong shape")
    geometry = config.get("geometry") or {}
    _require(isinstance(geometry, dict), "source.geometry must be a mapping")
    if geometry.get("enabled"):
        _require(
            str(geometry.get("format", "stl_binary")) == "stl_binary",
            "source.geometry.format must be stl_binary",
        )
    coordinate_systems = config.get("coordinate_systems") or []
    _require(isinstance(coordinate_systems, list), "source.coordinate_systems must be a list of names")
    material_source = str(config.get("material_source") or "cad")
    _require(
        material_source in ("cad", "documented_table"),
        "source.material_source must be 'cad' or 'documented_table'",
        material_source,
    )
    mass_evidence = _mass_evidence_binding(config.get("mass_evidence"), required=bool(config.get("documented_masses")))
    raw_masses = config.get("documented_masses") or {}
    _require(isinstance(raw_masses, dict), "source.documented_masses must be a mapping of component to kg")
    documented: dict[str, dict] = {}
    for name, value in raw_masses.items():
        # A documented mass is an author decision: it needs a mass and a stated
        # reason.  Bare numbers and misspelled keys are rejected instead of
        # quietly turning into "declared by the model author" with no evidence.
        _require(
            isinstance(value, dict),
            "each documented mass must be an object with mass_kg and reason",
            {"component": name, "value": value},
        )
        assert isinstance(value, dict)
        unknown_fields = sorted(set(value) - DOCUMENTED_MASS_KEYS)
        _require(
            not unknown_fields, "documented mass has unknown fields", {"component": name, "fields": unknown_fields}
        )
        mass: object = value.get("mass_kg")
        _require(
            isinstance(mass, (int, float)) and not isinstance(mass, bool) and float(mass) > 0.0,
            "documented mass needs a positive mass_kg",
            {"component": name, "value": mass},
        )
        assert isinstance(mass, (int, float)) and not isinstance(mass, bool)
        reason = value.get("reason")
        _require(
            isinstance(reason, str) and reason.strip() != "",
            "documented mass needs a stated reason",
            {"component": name},
        )
        # 证据文件由 source.mass_evidence 绑定（file + sha256）；每条质量必须给出
        # **该文件内的定位锚点**，独立校验会确认锚点确实出现在文件内容里。
        evidence = value.get("evidence")
        _require(
            isinstance(evidence, str) and evidence.strip() != "",
            "documented mass needs a non-empty evidence anchor inside the bound evidence file",
            {"component": name, "evidence": evidence},
        )
        documented[str(name)] = {
            "mass_kg": float(mass),
            "reason": reason,
            "evidence": evidence,
        }
    _require(
        material_source != "documented_table" or bool(documented),
        "material_source=documented_table requires a non-empty source.documented_masses table",
    )
    # 质量模式合同（配置层，能查就早查）：
    #  * 任何声明了质量的组件都必须真的被某个 body 纳入——拼错/改名的键不能静默失效；
    #  * documented_table 模式必须为每个被纳入的组件给出质量，缺一个就直接拒绝，
    #    不允许落回 CAD 的默认密度占位值。
    # bodies 为空（自动一组件一 link）时纳入集合要到采集后才知道，由 build_scene 用实际读数复核。
    if documented:
        declared_bodies = [item for item in (config.get("bodies") or []) if isinstance(item, dict)]
        included = {
            str(component) for body in declared_bodies for component in (body.get("components") or []) if component
        }
        if included:
            unknown = sorted(set(documented) - included)
            _require(
                not unknown,
                "documented_masses contains components that no body includes",
                {"unknown": unknown[:20]},
            )
            missing = sorted(included - set(documented))
            if material_source == "documented_table":
                _require(
                    not missing,
                    "documented_table requires a mass for every included component",
                    {"missing": missing[:20]},
                )
    # 物理上已计入的几何排除项：只允许"组件 + 原因 + 结构化证据"的对象，
    # 不允许用裸字符串或空原因悄悄跳过几何。
    raw_exclusions = config.get("geometry_exclusions") or {}
    _require(
        isinstance(raw_exclusions, dict),
        "source.geometry_exclusions must be a mapping of component to exclusion",
        raw_exclusions,
    )
    exclusions: dict[str, dict] = {}
    for name, value in raw_exclusions.items():
        _require(
            isinstance(value, dict),
            "each geometry exclusion must be an object with reason and evidence",
            {"component": name, "value": value},
        )
        assert isinstance(value, dict)
        unknown_fields = sorted(set(value) - {"component", "reason", "evidence"})
        _require(
            not unknown_fields,
            "geometry exclusion has unknown fields",
            {"component": name, "fields": unknown_fields},
        )
        reason = value.get("reason")
        _require(
            isinstance(reason, str) and reason.strip() != "",
            "geometry exclusion needs a stated reason",
            {"component": name},
        )
        evidence = value.get("evidence")
        _require(
            isinstance(evidence, dict),
            "geometry exclusion needs a structured evidence binding (file, sha256, anchor)",
            {"component": name, "evidence": evidence},
        )
        assert isinstance(evidence, dict)
        unknown_evidence = sorted(set(evidence) - {"file", "sha256", "anchor"})
        _require(
            not unknown_evidence,
            "geometry exclusion evidence has unknown fields",
            {"component": name, "fields": unknown_evidence},
        )
        for key in ("file", "sha256", "anchor"):
            item = evidence.get(key)
            _require(
                isinstance(item, str) and item.strip() != "",
                f"geometry exclusion evidence.{key} must be a non-empty string",
                {"component": name, "evidence": evidence},
            )
        exclusions[str(name)] = {
            "component": str(value.get("component") or name),
            "reason": reason,
            "evidence": {key: str(evidence[key]) for key in ("file", "sha256", "anchor")},
        }
    expected_mass = _expected_range(config.get("expected_mass_kg"), "source.expected_mass_kg")
    expected_extent = _expected_range(config.get("expected_extent_m"), "source.expected_extent_m")
    evidence_class = config.get("evidence_class")
    if evidence_class is not None:
        _require(
            evidence_class in EVIDENCE_CLASSES, "source.evidence_class must be cad, fixture or imported", evidence_class
        )
    return {
        "provider": provider,
        "assembly": str(assembly),
        "configuration": str(configuration),
        "allowed_roots": [str(root) for root in allowed_roots],
        "closure": dict(closure) if isinstance(closure, dict) else None,
        "document_suffixes": suffixes,
        "geometry": dict(geometry),
        "coordinate_systems": [str(name) for name in coordinate_systems],
        "require_saved": True,
        "bodies": list(config.get("bodies") or []),
        "joints": list(config.get("joints") or []),
        "frames": list(config.get("frames") or []),
        "robot_name": str(config.get("robot_name") or "robot"),
        "material_source": material_source,
        "documented_masses": documented,
        "mass_evidence": mass_evidence,
        "geometry_exclusions": exclusions,
        "expected_mass_kg": expected_mass,
        "expected_extent_m": expected_extent,
        "evidence_class": evidence_class,
        "raw": dict(config),
    }


def _evidence_class(backend: Any, cfg: dict[str, Any]) -> str:
    """What kind of evidence this capture really is.

    A fixture backend must never be published as ``cad``: the class is derived
    from the contact layer, and a configuration may only make it *weaker*
    (a real CAD capture may be declared ``fixture`` or ``imported`` on purpose).
    """

    natural = getattr(backend, "evidence_class", None)
    if natural is None:
        natural = "cad" if getattr(backend, "name", "") == SOURCE_KIND else "fixture"
    declared = cfg.get("evidence_class")
    if declared is None:
        return str(natural)
    if declared == "cad" and natural != "cad":
        raise ConfigError(
            "a fixture or imported backend cannot be declared as cad evidence",
            {"backend": getattr(backend, "name", ""), "declared": declared},
        )
    return str(declared)


def _document_state(backend: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    reader = getattr(backend, "document_state", None)
    if not callable(reader):
        raise BridgeError("cad_document_state_unreadable", "the backend cannot read document state", exit_code=3)
    state = dict(reader(cfg["assembly"]) or {})
    if (
        type(state.get("saved")) is not bool
        or not isinstance(state.get("active_configuration"), str)
        or not state["active_configuration"].strip()
    ):
        raise BridgeError(
            "cad_document_state_unreadable",
            "saved state and active configuration must both be known",
            {"document": cfg["assembly"], "state": state},
            exit_code=3,
        )
    return state


def _native_lock_files(source_dir: Path) -> set[Path]:
    """SolidWorks session locks are transient siblings of native documents."""
    return {
        path
        for path in source_dir.rglob("~$*")
        if path.is_file()
        and path.suffix.lower() in DEFAULT_ALLOWED_SUFFIXES
        and path.with_name(path.name[2:]).is_file()
    }


def _copy_inventory(source_dir: Path) -> dict[str, str]:
    locks = tuple(path.relative_to(source_dir).as_posix() for path in _native_lock_files(source_dir))
    return {f"source/{path}": digest for path, digest in inventory(source_dir, exclude=locks).items()}


def _document_parts(value: str) -> tuple[str, list[str]]:
    """A Windows or POSIX document path as (drive letter, path segments).

    ``os.path.relpath`` only understands the separators of the host it runs on, and it
    raises on Windows paths that live on different drives.  A snapshot's identity has to
    be recomputable on any host - a Linux verifier reading a Windows snapshot must get the
    digest the capture recorded - so the separators are handled here instead.
    """

    text = str(value).replace("\\", "/")
    drive, sep, rest = text.partition(":")
    if not sep or len(drive) != 1:
        drive, rest = "", text
    return drive.lower(), [segment for segment in rest.split("/") if segment not in ("", ".")]


def _relative_document_name(path: str, root: str) -> str:
    """A captured document named relative to the assembly's directory."""

    drive, parts = _document_parts(path)
    root_drive, root_parts = _document_parts(root)
    if drive and root_drive and drive != root_drive:
        # Different drives cannot be relative to each other (Windows raises), so the
        # volume is part of the name - the same convention the collector uses.
        return f"volume-{drive}/{'/'.join(parts)}"
    common = 0
    while common < len(parts) and common < len(root_parts) and parts[common].lower() == root_parts[common].lower():
        common += 1
    return "/".join([".."] * (len(root_parts) - common) + parts[common:])


def _document_parent(value: str) -> str:
    """The directory of a document, parsed with the same rules as its name."""

    drive, parts = _document_parts(value)
    if not parts:
        return str(value)
    joined = "/".join(parts[:-1])
    return f"{drive}:{joined}" if drive else joined


def _source_inputs(cfg: dict[str, Any], closure: dict[str, Any]) -> dict[str, str]:
    """The captured revision: source documents by relative name and SHA-256.

    A capture's input identity has to survive the job directory it ran in.  The paths
    inside a job (``staging/snapshot.partial/source/...``) are ephemeral - two captures
    of identical CAD bytes in different roots produced different digests on 2026-09-24 -
    while each document's name relative to the assembly's own directory and its SHA-256
    do not depend on where the snapshot was assembled.

    Names are compared the way the capture's own filesystem does: case-insensitively
    (this backend is Windows-only).  The model's ``source.assembly`` and SolidWorks'
    ``GetPathName`` need not agree on case, and recording both spellings would make the
    identity depend on how a path happened to be written.  A relative name that carries
    two different digests cannot be expressed in a case-insensitive namespace at all, so
    it fails closed instead of dropping a document from the identity.
    """

    root = _document_parent(str(cfg["assembly"]))
    inputs: dict[str, str] = {}
    for path, digest in sorted((closure.get("original_files") or {}).items()):
        name = _relative_document_name(str(path), root).casefold()
        if not name:
            raise BridgeError(
                "dependency_identity_ambiguous",
                "a source document has no name relative to the assembly",
                {"path": str(path)},
                exit_code=3,
            )
        recorded = inputs.get(name)
        if recorded is None:
            inputs[name] = str(digest)
        elif recorded != str(digest):
            raise BridgeError(
                "dependency_identity_ambiguous",
                "two source documents share a relative name with different contents",
                {"name": name, "first": recorded, "second": str(digest)},
                exit_code=3,
            )
    return inputs


def _dependency_closure(backend: Any, cfg: dict[str, Any], source_dir: Path) -> dict[str, Any]:
    """Collect the source with its dependencies and prove the closure.

    The pre-listing is advisory, so the decision is made on the copy SolidWorks
    itself produced: it must name the exact top-level, every path it resolves must
    exist inside the snapshot, and re-opening that top-level must show the same
    component instances and configurations as the original.
    """

    lister = getattr(backend, "list_dependencies", None)
    dependencies = list(lister(cfg["assembly"]) or []) if callable(lister) else []
    # Suppressed placements are omitted from the sealed closure graph; a stored
    # reference that only exists because of a suppressed placement must not fail
    # the capture later.  Paths recorded as suppressed by the closure are
    # skipped here (and in the collector) and reported as evidence.
    closure = cfg.get("closure") or {}
    suppressed_paths = {
        os.path.normcase(os.path.abspath(str(entry.get("resolved_path"))))
        for entry in closure.get("suppressed") or []
        if isinstance(entry, dict) and entry.get("resolved_path")
    }
    # Session materializations recorded by the closure walk (imported
    # occurrences): they are evidence, not snapshot members, and are excluded
    # from the required file graph by the manifest itself -- never by a path
    # string heuristic.
    cached_import_paths = {
        os.path.normcase(os.path.abspath(str(entry.get("materialization"))))
        for entry in closure.get("imports") or []
        if isinstance(entry, dict) and entry.get("materialization")
    }
    # Exact-occurrence binding: the closure records which occurrences were
    # session materializations.  The current session's materialization paths
    # differ from the selection session's, so the stable key is the exact
    # (owner, occurrence) pair: the owning document's canonical name, or -- for
    # occurrences inside a materialization subtree -- the anchoring
    # materialization occurrence.  Never a name alone, never a path pattern.
    def _closure_owner_token(item: dict) -> tuple[str, str] | None:
        anchor = item.get("owner_anchor")
        if isinstance(anchor, str) and anchor:
            return ("anchor", anchor)
        owner = item.get("owner") if isinstance(item.get("owner"), str) and item.get("owner") else item.get("document")
        if not isinstance(owner, str) or not owner:
            return None
        return ("document", owner.replace("\\", "/").casefold())

    materialization_keys: set[tuple[str, str]] = set()
    for entry in closure.get("imports") or []:
        if not isinstance(entry, dict) or entry.get("state") != "cached_materialization":
            continue
        instance = entry.get("instance")
        token = _closure_owner_token(entry)
        if isinstance(instance, str) and instance and token is not None:
            materialization_keys.add((token, instance))
    # Stale author source links recorded by the closure (import metadata): the
    # exact author path may still be listed by the engine, but after the
    # coverage proof below it is excluded from the required graph and copied
    # graph as recorded evidence -- never a broad STEP or name rule.
    # Only genuinely external author links are excluded: an author path that is
    # itself inside the admitted roots is an ordinary delivered dependency and
    # is never dropped (e.g. NP-F550.step resolving inside the flat package).
    def _author_link_is_external(value) -> bool:
        roots = cfg.get("allowed_roots") or []
        return bool(roots) and not _under(str(value), roots)

    author_metadata_external = {
        os.path.normcase(os.path.abspath(str(entry.get("author_imported_path"))))
        for entry in closure.get("imports") or []
        if isinstance(entry, dict)
        and entry.get("state") == "cached_materialization"
        and entry.get("author_imported_path")
        and _author_link_is_external(entry.get("author_imported_path"))
    }
    inspector = getattr(backend, "inspect_copy", None)
    if not callable(inspector):
        raise BridgeError(
            "dependency_resolution_unverified",
            "the backend cannot re-open the copy to verify component coverage",
            exit_code=3,
        )
    original = dict(inspector(cfg["assembly"]) or {})
    # Occurrence coverage gate: every current occurrence must be covered by the
    # admitted closure (a resolved edge, a recorded cached materialization or a
    # recorded suppressed placement) by exact owner+occurrence identity.  Only
    # then may session materializations or stale author links be excluded from
    # the required/copied graph; otherwise nothing is skipped.
    covered_keys: set[tuple[str, str]] = set()
    for name, entry in (closure.get("documents") or {}).items():
        if not isinstance(entry, dict):
            continue
        token = ("document", str(name).replace("\\", "/").casefold())
        for edge in entry.get("resolved_edges") or []:
            if isinstance(edge, dict) and isinstance(edge.get("instance"), str) and edge["instance"]:
                covered_keys.add((token, edge["instance"]))
    for item in closure.get("imports") or []:
        if not isinstance(item, dict) or not isinstance(item.get("instance"), str) or not item["instance"]:
            continue
        token = _closure_owner_token(item)
        if token is not None:
            covered_keys.add((token, item["instance"]))
    for item in closure.get("suppressed") or []:
        if not isinstance(item, dict) or not isinstance(item.get("instance"), str) or not item["instance"]:
            continue
        token = _closure_owner_token(item)
        if token is not None:
            covered_keys.add((token, item["instance"]))

    def _actual_owner_token(entry: dict) -> tuple[str, str]:
        anchor = entry.get("defining_anchor")
        if isinstance(anchor, str) and anchor:
            return ("anchor", anchor)
        defining = str(entry.get("defining") or "")
        for root in cfg.get("allowed_roots") or []:
            try:
                relative = Path(defining).resolve().relative_to(Path(str(root)).resolve()).as_posix()
                return ("document", relative.casefold())
            except (OSError, ValueError):
                continue
        return ("document", os.path.normcase(os.path.abspath(defining)) if defining else "")

    actual_keys = {
        (_actual_owner_token(entry), str(entry.get("instance") or ""))
        for entry in original.get("instances") or []
        if entry.get("instance")
    }
    materializations_verified = False
    if closure:
        if actual_keys != covered_keys:
            raise BridgeError(
                "dependency_materialization_uncovered",
                "the source occurrence inventory differs from the admitted closure; a session "
                "materialization can never be dropped without a recorded owner/occurrence",
                {
                    "missing_from_closure": sorted(str(item) for item in (actual_keys - covered_keys))[:20],
                    "missing_from_session": sorted(str(item) for item in (covered_keys - actual_keys))[:20],
                    "instances": len(actual_keys),
                },
                exit_code=3,
            )
        materializations_verified = True
    suppressed_skipped: list[str] = []
    session_temp_dependencies: list[str] = []
    stale_source_links: list[str] = []
    unresolved = []
    outside = []
    for entry in dependencies:
        path = str(entry.get("path") or "")
        if path and os.path.normcase(os.path.abspath(path)) in suppressed_paths:
            suppressed_skipped.append(path)
            continue
        normalized = os.path.normcase(os.path.abspath(path)) if path else ""
        if path and materializations_verified and normalized in author_metadata_external:
            stale_source_links.append(path)
            continue
        if path and materializations_verified and session_materialization_path(path):
            session_temp_dependencies.append(path)
            continue
        if not entry.get("exists", True):
            unresolved.append(path)
        elif cfg["allowed_roots"] and not _under(path, cfg["allowed_roots"]):
            outside.append(path)
    if outside:
        raise BridgeError(
            "dependency_outside_allowed_roots",
            "the assembly references files outside the admitted CAD source roots; "
            "collect the required references into the controlled assembly delivery",
            {"paths": outside[:20], "count": len(outside)},
            exit_code=3,
        )

    # Bind every saved source file before copying, then recheck after all reads.
    original_files: dict[str, str] = {}
    original_documents = [str(cfg["assembly"])]
    import_temp_documents: list[str] = []
    for entry in original.get("instances") or []:
        document = str(entry.get("document") or "")
        if not document:
            continue
        key = (_actual_owner_token(entry), str(entry.get("instance") or ""))
        if (
            key in materialization_keys
            or os.path.normcase(os.path.abspath(document)) in cached_import_paths
        ):
            import_temp_documents.append(document)
            continue
        original_documents.append(document)
    for entry in dependencies:
        path = str(entry.get("path") or "")
        if not path or os.path.normcase(os.path.abspath(path)) in suppressed_paths:
            continue
        if materializations_verified and session_materialization_path(path):
            continue
        if materializations_verified and os.path.normcase(os.path.abspath(path)) in author_metadata_external:
            continue
        original_documents.append(path)
    original_states: dict[str, dict[str, Any]] = {}
    unreadable: list[dict[str, Any]] = []
    save_flags: list[str] = []
    for path in dict.fromkeys(original_documents):
        if not path:
            continue
        try:
            original_files[path] = sha256_file(Path(path))
            if Path(path).suffix.lower() not in {*DEFAULT_ALLOWED_SUFFIXES, ".slddrw"}:
                continue
            state = _document_state(backend, {"assembly": path})
        except Exception as exc:  # noqa: BLE001 - a source we cannot read blocks
            unreadable.append({"path": path, "error": f"{type(exc).__name__}: {exc}"})
            continue
        original_states[path] = {
            "saved": state.get("saved"),
            "active_configuration": state.get("active_configuration"),
            "read_only": state.get("read_only"),
        }
        if state.get("saved") is False:
            save_flags.append(path)
    if unreadable:
        raise BridgeError(
            "cad_document_state_unreadable",
            "the state of a source document could not be read; the closure cannot be proven",
            {"unreadable": unreadable[:20], "count": len(unreadable)},
            exit_code=3,
        )
    # A document SolidWorks would prompt to save is recorded and reported, never a
    # refusal: the flag is not evidence about the operator's session, and the bytes
    # this capture copies are the revision it names (see _verify_originals_unchanged).

    pack = getattr(backend, "collect_dependencies", None)
    if not callable(pack):
        raise BridgeError(
            "dependency_collection_unsupported",
            "the backend cannot collect dependencies; a verified copy is required to freeze a source",
            exit_code=3,
        )
    # The collector contract is explicit: skipped/author-excluded paths are
    # always passed, empty when the coverage proof did not authorize a skip.
    closure_documents: dict[str, dict] = {}
    for name, entry in (closure.get("documents") or {}).items():
        if not isinstance(entry, dict):
            continue
        closure_documents[str(name)] = {
            "stored_locations": [
                str(item.get("location"))
                for item in entry.get("stored_references") or []
                if isinstance(item, dict) and isinstance(item.get("location"), str)
            ],
            "resolved_targets": [
                str(edge.get("resolved"))
                for edge in entry.get("resolved_edges") or []
                if isinstance(edge, dict) and isinstance(edge.get("resolved"), str)
            ],
            "resolved_paths": [
                str(edge.get("resolved_path"))
                for edge in entry.get("resolved_edges") or []
                if isinstance(edge, dict) and isinstance(edge.get("resolved_path"), str)
            ],
        }
    collected = dict(
        pack(
            cfg["assembly"],
            str(source_dir),
            skip_missing=sorted(suppressed_paths),
            skip_session_temp=materializations_verified,
            excluded_paths=sorted(author_metadata_external) if materializations_verified else (),
            required_names=sorted((closure.get("graph") or {}).keys()),
            admitted_roots=[str(source_dir), *(cfg.get("allowed_roots") or [])],
            closure_documents=closure_documents,
            required_paths=sorted(original_files),
        )
        or {}
    )
    stale_stored: list[dict] = [dict(item) for item in collected.get("skipped_stale") or [] if isinstance(item, dict)]
    stale_resolved_normalized = {
        os.path.normcase(os.path.abspath(str(item.get("resolved_path"))))
        for item in stale_stored
        if item.get("resolved_path")
    }
    source_mapping = collected.get("mapping") or {}
    if {os.path.normcase(os.path.abspath(path)) for path in source_mapping} != {
        os.path.normcase(os.path.abspath(path)) for path in original_files
    }:
        raise BridgeError(
            "dependency_collection_incomplete",
            "the collector file graph differs from the original dependency closure",
            {"expected": sorted(original_files), "collected": sorted(source_mapping)},
            exit_code=3,
        )

    top = str(collected.get("top_level") or "")
    if not top or not os.path.isfile(top):
        raise BridgeError(
            "dependency_collection_incomplete",
            "the collector did not name a collected top-level assembly",
            {"top_level": top, "destination": str(source_dir), "files": len(collected.get("files") or [])},
            exit_code=3,
        )
    if not _under(top, [str(source_dir)]):
        raise BridgeError(
            "dependency_escape",
            "the collected top-level lies outside the snapshot",
            {"top_level": top, "source_dir": str(source_dir)},
            exit_code=3,
        )

    resolved = getattr(backend, "resolve_dependencies", None)
    if not callable(resolved):
        raise BridgeError(
            "dependency_resolution_unverified",
            "the backend cannot prove where the collected copy resolves its references",
            exit_code=3,
        )
    entries = list(resolved(top) or [])
    if not entries:
        raise BridgeError(
            "dependency_resolution_unverified",
            "the copy reported no dependencies at all; the closure cannot be proven",
            {"top_level": top},
            exit_code=3,
        )
    problems: list[dict[str, Any]] = []
    for entry in entries:
        path = str(entry.get("path") or "")
        if path and os.path.normcase(os.path.abspath(path)) in suppressed_paths:
            suppressed_skipped.append(path)
            continue
        if path and os.path.normcase(os.path.abspath(path)) in stale_resolved_normalized:
            # The copy still stores an evidence-covered unused reference; it
            # was never ingested and is recorded (no name matching involved).
            if not any(item.get("resolved_path") == path for item in stale_stored):
                stale_stored.append(
                    {"document": None, "location": None, "resolved_path": path, "exists": os.path.isfile(path)}
                )
            continue
        if path and materializations_verified and os.path.normcase(os.path.abspath(path)) in author_metadata_external:
            stale_source_links.append(path)
            continue
        if path and materializations_verified and session_materialization_path(path):
            session_temp_dependencies.append(path)
            continue
        if not path or not _under(path, [str(source_dir)]):
            problems.append({"path": path, "problem": "outside_snapshot"})
        elif not os.path.isfile(path):
            problems.append({"path": path, "problem": "missing_in_snapshot"})
    if problems:
        raise BridgeError(
            "dependency_escape",
            "the collected copy resolves references that are missing or outside the snapshot",
            {"problems": problems[:20], "count": len(problems), "top_level": top},
            exit_code=3,
        )

    copy = dict(inspector(top) or {})

    # A copy that opened in a different configuration is a different model, so the
    # instance comparison below would be meaningless.
    original_config = original.get("configuration")
    copy_config = copy.get("configuration")
    requested_config = str(cfg["configuration"])
    if not original_config or not copy_config or str(original_config) != str(copy_config):
        raise BridgeError(
            "dependency_configuration_mismatch",
            "the collected copy does not reproduce the source top-level configuration",
            {"original": original_config, "copy": copy_config, "top_level": top},
            exit_code=3,
        )
    # The active configuration *is* the identity of the snapshot, so a request that
    # names another configuration of the same document cannot be satisfied by
    # reading whatever happens to be active - and switching the user's document
    # configuration is not something this adapter does.
    if str(copy_config) != requested_config:
        raise BridgeError(
            "cad_configuration_not_active",
            "the requested configuration is not the active configuration of the document",
            {
                "requested": requested_config,
                "active": str(copy_config),
                "assembly": cfg["assembly"],
                "hint": "activate the configuration in SolidWorks before freezing",
            },
            exit_code=3,
        )
    if original.get("unresolved"):
        raise BridgeError(
            "dependency_unresolved_in_original",
            "the source assembly has component instances SolidWorks cannot resolve",
            {"unresolved": original["unresolved"][:20], "count": len(original["unresolved"])},
            exit_code=3,
        )
    if copy.get("unresolved"):
        raise BridgeError(
            "dependency_escape",
            "the copy has component instances that resolve outside the snapshot",
            {"unresolved": copy["unresolved"][:20], "count": len(copy["unresolved"]), "top_level": top},
            exit_code=3,
        )
    if int(copy.get("components") or 0) != int(original.get("components") or 0):
        raise BridgeError(
            "dependency_collection_incomplete",
            "the copy does not contain the same number of component instances as the source",
            {"original": original.get("components"), "copy": copy.get("components"), "top_level": top},
            exit_code=3,
        )

    def signatures(report: dict[str, Any]) -> dict[str, tuple[Any, Any, bool]]:
        return {
            str(entry["instance"]): (
                entry.get("document_name"),
                entry.get("configuration"),
                bool(entry.get("suppressed")),
            )
            for entry in report.get("instances") or []
        }

    original_instances = signatures(original)
    copy_instances = signatures(copy)
    # Identity has to be unambiguous per instance, so a duplicate instance path
    # means the report cannot prove coverage and is not accepted.
    for report, table, reported in (
        ("original", original_instances, original),
        ("copy", copy_instances, copy),
    ):
        listed = len(list(reported.get("instances") or []))
        if len(table) != listed:
            raise BridgeError(
                "dependency_instance_identity_ambiguous",
                f"the {report} report reuses component instance paths",
                {"report": report, "unique": len(table), "listed": listed, "top_level": top},
                exit_code=3,
            )
    missing = sorted(set(original_instances) - set(copy_instances))
    extra = sorted(set(copy_instances) - set(original_instances))
    if missing or extra:
        raise BridgeError(
            "dependency_collection_incomplete",
            "the copy does not reproduce the component instance list of the source",
            {
                "missing": missing[:20],
                "extra": extra[:20],
                "original_instances": len(original_instances),
                "copy_instances": len(copy_instances),
            },
            exit_code=3,
        )
    changed = sorted(
        (name, original_instances[name], copy_instances[name])
        for name in original_instances
        if original_instances[name] != copy_instances[name]
    )
    if changed:
        raise BridgeError(
            "dependency_instance_mismatch",
            "the copy resolves component instances to different documents or configurations",
            {
                "changed": [
                    {"instance": name, "original": list(before), "copy": list(after)}
                    for name, before, after in changed[:20]
                ],
                "count": len(changed),
                "top_level": top,
            },
            exit_code=3,
        )

    outside_copy: list[str] = []
    absent_copy: list[str] = []
    for entry in copy.get("instances") or []:
        path = str(entry.get("document") or "")
        if not path:
            continue  # suppressed instances are tracked, not resolved
        key = (_actual_owner_token(entry), str(entry.get("instance") or ""))
        if (
            key in materialization_keys
            or os.path.normcase(os.path.abspath(path)) in cached_import_paths
            or (materializations_verified and session_materialization_path(path))
        ):
            import_temp_documents.append(path)
            continue  # session materializations are evidence, not snapshot members
        if not _under(path, [str(source_dir)]):
            outside_copy.append(path)
        elif not os.path.isfile(path):
            absent_copy.append(path)

    # SolidWorks resolves a reference to an already-open document that shares its
    # file name, so a copy opened next to an open working tree can bind to the
    # original.  That is never fixed by closing the user's document here; the
    # capture has to run against an isolated instance or a renamed mapping, and the
    # conflict is reported as such instead of as a generic escape.
    open_documents = []
    lister = getattr(backend, "list_documents", None)
    if callable(lister):
        try:
            open_documents = [str(item) for item in (lister() or []) if item]
        except Exception:  # noqa: BLE001 - the gate below does not depend on it
            open_documents = []
    conflicts = []
    for path in set(outside_copy):
        name = os.path.basename(path).lower()
        for open_path in open_documents:
            if os.path.basename(open_path).lower() == name:
                conflicts.append(
                    {
                        "snapshot_reference": path,
                        "open_document": open_path,
                        "is_the_open_file": _same_document(open_path, path),
                    }
                )
    if conflicts:
        raise BridgeError(
            "cad_same_name_conflict",
            "the copy resolved a same-named document that is already open in this "
            "SolidWorks session; capture it from an isolated session or with a "
            "renamed mapping instead of closing the user's document",
            {"conflicts": conflicts[:20], "count": len(conflicts), "top_level": top},
            exit_code=3,
        )
    if outside_copy or absent_copy:
        raise BridgeError(
            "dependency_escape",
            "the re-opened copy loads documents that are outside the snapshot or missing from it",
            {
                "outside": sorted(set(outside_copy))[:20],
                "missing": sorted(set(absent_copy))[:20],
                "count": len(outside_copy) + len(absent_copy),
                "top_level": top,
            },
            exit_code=3,
        )

    # Native reference collection legitimately rewrites internal references and serialisation
    # metadata, so the copy is *not* expected to be byte-identical to its source.
    # What the snapshot relies on is the recorded mapping, the re-opened instance
    # and configuration comparison above, and every copied file's own digest,
    # rechecked after readings and geometry export.
    mapping: dict[tuple[str, str], str] = dict.fromkeys(source_mapping.items(), "dependency")
    if os.path.isfile(top):
        mapping[(str(cfg["assembly"]), top)] = "top_level"
    for entry in copy.get("instances") or []:
        path = str(entry.get("document") or "")
        key = (_actual_owner_token(entry), str(entry.get("instance") or ""))
        if (
            not path
            or key in materialization_keys
            or os.path.normcase(os.path.abspath(path)) in cached_import_paths
            or (materializations_verified and session_materialization_path(path))
        ):
            continue
        for source_entry in original.get("instances") or []:
            if str(source_entry.get("instance")) == str(entry.get("instance")) and source_entry.get("document"):
                mapping[(str(source_entry["document"]), path)] = str(entry.get("instance"))
                break
    # Recorded relative to the snapshot root: the staging tree is renamed into
    # place when the capture succeeds, so absolute staging paths would go stale.
    snapshot_root = source_dir.parent
    copy_files = _copy_inventory(source_dir)

    return {
        "method": collected.get("method", "unknown"),
        "source_dir": str(source_dir),
        "top_level": top,
        "declared_dependencies": dependencies,
        "declared_unresolved": unresolved,
        "suppressed_skipped": sorted(set(suppressed_skipped)),
        "session_temp_dependencies": sorted(set(session_temp_dependencies)),
        "stale_source_links": sorted(set(stale_source_links)),
        "stale_stored_references": sorted(
            stale_stored,
            key=lambda item: (str(item.get("document")), str(item.get("location")), str(item.get("resolved_path"))),
        ),
        "seeded_required_leaves": sorted(collected.get("seeded_required_leaves") or []),
        "materializations_verified": materializations_verified,
        "materialization_keys": sorted([token[0], token[1], instance] for token, instance in materialization_keys),
        "import_temp_documents": sorted(set(import_temp_documents)),
        "resolved_inside_snapshot": len(entries),
        "collected_files": len(collected.get("files") or []),
        "configuration": copy_config,
        "components_verified": copy.get("components"),
        "component_instances_inside": sum(1 for entry in copy.get("instances") or [] if entry.get("document")),
        "component_instances_compared": len(copy_instances),
        "suppressed_instances": len(copy.get("suppressed") or []),
        "original_files": original_files,
        "original_states": original_states,
        # Documents SolidWorks reported as needing a save.  Evidence for the reader,
        # deliberately not a refusal: see the note above.
        "save_flag_documents": sorted(save_flags),
        "copy_files": copy_files,
        "mapping": [
            {
                "source": source,
                "copy": os.path.relpath(copy_path, snapshot_root).replace(os.sep, "/"),
                "instance": label,
                "copy_sha256": copy_files.get(os.path.relpath(copy_path, snapshot_root).replace(os.sep, "/")),
                "source_sha256": original_files.get(source),
            }
            for (source, copy_path), label in sorted(mapping.items(), key=lambda item: item[1])
        ],
        "collector": {"configured": collected.get("configured"), "reference_edges": collected.get("reference_edges")},
    }


def _same_document(left: str, right: str) -> bool:
    """Whether two native paths name the same document file."""

    if not left or not right:
        return False
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


_CREDENTIAL_MARKERS = ("token", "password", "secret", "credential", "authorization", "api_key", "apikey")


def _redact(value: Any, key: str = "") -> Any:
    """Copy request input with credentials removed before it is written down."""

    if isinstance(value, dict):
        return {str(name): _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, key) for item in value]
    if key and any(marker in key.lower() for marker in _CREDENTIAL_MARKERS):
        return "<redacted>" if value not in (None, "") else value
    if isinstance(value, str) and "://" in value:
        # credentials in the authority and tokens in the query string
        return re.sub(r"//[^/@]*@", "//", value).split("?", 1)[0]
    return value


def _retain_failure(
    staging: Path, destination: Path, error: BaseException, stage: str, request: dict[str, Any]
) -> str | None:
    """Keep a failed attempt's staging tree instead of deleting the evidence.

    The staging tree holds the collected CAD copy, the raw readings and the
    re-opened-copy evidence, so removing it on failure would destroy the only
    material that explains the failure.  Every attempt gets its own directory, so
    a retry never overwrites an earlier diagnosis, and the request is recorded
    without credentials.
    """

    if not staging.exists():
        return None
    payload = {
        "schema_version": "solidworks-to-urdf.freeze-failure/v1",
        "stage": stage,
        "code": getattr(error, "code", type(error).__name__),
        "message": _redact(str(error)),
        "detail": _redact(getattr(error, "detail", None), "detail"),
        "exceptions": _redact(_exception_trace(error)),
        "exit_code": getattr(error, "exit_code", None),
        "destination": str(destination),
        "request": _redact(request),
    }
    for index in range(1, 1001):
        candidate = destination.with_name(f"{destination.name}.failed-{index:03d}")
        try:
            candidate.mkdir(parents=True)
        except FileExistsError:
            continue
        except OSError:
            return None
        moved = staging
        try:
            moved = candidate / "partial"
            os.replace(staging, moved)
        except OSError:
            moved = staging
        with contextlib.suppress(OSError):
            write_json(candidate / "failure.json", {**payload, "partial": str(moved)})
        return str(candidate)
    return None


def _exception_trace(error: BaseException) -> list[dict[str, Any]]:
    """Retain native call sites without inspecting or calling any COM object."""

    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        frames = []
        trace = error.__traceback__
        while trace is not None:
            frame = trace.tb_frame
            module = frame.f_globals.get("__name__", "")
            if isinstance(module, str) and module.startswith("description_pipeline.sources.solidworks."):
                site = {"module": module, "function": frame.f_code.co_name, "line": trace.tb_lineno}
                if module.endswith(".native") and frame.f_code.co_name in ("_member", "_method"):
                    member = frame.f_locals.get("name")
                    if isinstance(member, str):
                        site["member"] = member
                frames.append(site)
            trace = trace.tb_next
        record = {"type": type(error).__name__, "message": str(error), "frames": frames}
        hresult = getattr(error, "hresult", None)
        if isinstance(hresult, int):
            record["hresult"] = hresult
        chain.append(record)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return chain


def _component_context_record(reading: dict[str, Any], assembly_mass: float | None) -> dict[str, Any]:
    """Native component-context observations, verbatim; no derived math.

    Windows records only what CAD answered: per-instance identity, context
    mass/volume, override flags, effective readings and their failures.
    Document basis, deltas, totals, coverage and the effective recombination
    are rebuilt on Linux (``closure.derive_mass_closure``); there is no
    compatibility format and no computed field survives here.
    """

    instances = []
    errors = list(reading.get("errors") or [])
    for entry in reading.get("instances") or []:
        name = str(entry.get("name"))
        try:
            if not name:
                raise ValueError("component instance row has no name")
            raw_depth = entry.get("depth")
            if not isinstance(raw_depth, int) or isinstance(raw_depth, bool):
                raise ValueError(f"row for {name} has no integer depth")
            depth = raw_depth
            expected_depth = name.count("/")
            if depth != expected_depth:
                raise ValueError(f"row for {name} has depth {depth}, expected {expected_depth}")
            parent = name.rsplit("/", 1)[0] if "/" in name else None
            if entry.get("parent") != parent:
                raise ValueError(f"row for {name} has parent {entry.get('parent')!r}, expected {parent!r}")
            mass = float(entry["context_mass_kg"])
            if not math.isfinite(mass) or mass <= 0.0:
                raise ValueError("component mass is not finite and positive")
            volume = entry.get("context_volume_m3")
            volume = None if volume is None else float(volume)
            if volume is not None and not math.isfinite(volume):
                raise ValueError("component volume is not finite")
            raw_overrides = entry.get("overrides")
            if not isinstance(raw_overrides, dict):
                raise ValueError("component row has no override flags")
            if not {"OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia"} <= set(raw_overrides):
                raise ValueError("component row is missing override flags")
            overrides: dict[str, bool] = {}
            for key, value in raw_overrides.items():
                if not isinstance(value, bool):
                    raise ValueError(f"override flag {key!r} is not a boolean")
                overrides[str(key)] = value
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({"name": name, "error": "cad_component_context_invalid", "message": str(exc)[:200]})
            continue
        instances.append(
            {
                "name": name,
                "parent": parent,
                "depth": depth,
                "document": entry.get("document"),
                "document_type": str(entry.get("document_type") or ""),
                "context_mass_kg": mass,
                "context_volume_m3": volume,
                "overrides": overrides,
                "effective": entry.get("effective"),
                "effective_error": entry.get("effective_error"),
            }
        )
    return {
        "schema_version": "solidworks-to-urdf.component-mass-context/v1",
        "status": "recorded" if not errors else "partial",
        "assembly_mass_kg": None if assembly_mass is None else float(assembly_mass),
        "instances": instances,
        "errors": errors,
        "reference": dict(reading.get("reference") or {}),
    }


def _component_mass_context(
    backend: Any,
    cfg: dict[str, Any],
    assembly_mass: float | None,
    document: str | None = None,
) -> dict[str, Any] | None:
    """The assembly context reading when the backend can produce it; best-effort, never fatal."""

    reader = getattr(backend, "assembly_component_mass_properties", None)
    if not callable(reader):
        return None
    try:
        # The closure must describe the document the capture actually read: the
        # collected copy, never the author's working tree.
        reading = reader(str(document or cfg["assembly"]))
    except Exception as error:  # noqa: BLE001 - an optional probe must not fail a valid capture
        return {
            "schema_version": "solidworks-to-urdf.component-mass-context/v1",
            "status": "unavailable",
            "reason": str(getattr(error, "code", "") or type(error).__name__),
            "message": " ".join(str(error).split())[:200],
        }
    if not isinstance(reading, dict):
        return {
            "schema_version": "solidworks-to-urdf.component-mass-context/v1",
            "status": "unavailable",
            "reason": "cad_component_context_malformed",
            "message": "the backend returned no component mass context object",
        }
    try:
        return _component_context_record(reading, assembly_mass)
    except Exception as error:  # noqa: BLE001 - an optional probe must never abort a capture
        return {
            "schema_version": "solidworks-to-urdf.component-mass-context/v1",
            "status": "unavailable",
            "reason": "cad_component_context_invalid",
            "message": " ".join(str(error).split())[:200],
        }


def _mass_closure(backend: Any, cfg: dict[str, Any], document: str | None = None) -> dict[str, Any] | None:
    """Native closure observations only: the assembly reading and instance context.

    The Windows boundary records what CAD answered (whole-assembly reading,
    per-instance context with overrides and effective readings) plus explicit
    unavailable/error receipts.  All comparison math - leaf totals, deltas,
    totals, coverage and the effective recombination - lives on Linux
    (``closure.derive_mass_closure``); no computed field survives here.
    """

    closure_reader = getattr(backend, "assembly_mass_properties", None)
    context_reader = getattr(backend, "assembly_component_mass_properties", None)
    if not callable(closure_reader) and not callable(context_reader):
        return None
    measured = str(document or cfg["assembly"])
    top_level: dict[str, Any] | None = None
    failure: dict[str, str] | None = None
    if callable(closure_reader):
        try:
            top_level = closure_reader(measured)
        except Exception as error:  # noqa: BLE001 - an optional probe must not fail a valid capture
            failure = {
                "reason": str(getattr(error, "code", "") or type(error).__name__),
                "message": " ".join(str(error).split())[:200],
            }
    top_mass: float | None = None
    if isinstance(top_level, dict):
        try:
            top_mass = float(top_level["mass"])
        except (KeyError, TypeError, ValueError):
            top_level = None
            failure = failure or {
                "reason": "cad_mass_property_invalid",
                "message": "the assembly reading carried no usable mass",
            }
    elif top_level is not None:
        top_level = None
        failure = failure or {
            "reason": "cad_mass_property_invalid",
            "message": "the assembly reading is not an object",
        }
    # The override evidence is required for a pure-CAD claim, so it is collected even when the
    # optional assembly reading failed (or the record would earn cad equivalence by omission).
    component_context = _component_mass_context(backend, cfg, top_mass, measured)
    if top_level is None or top_mass is None:
        unavailable_record: dict[str, Any] = {
            "schema_version": "solidworks-to-urdf.mass-closure/v1",
            "status": "unavailable",
            **(
                failure
                or {"reason": "cad_mass_property_unavailable", "message": "no assembly mass reading was recorded"}
            ),
        }
        if component_context is not None:
            unavailable_record["component_context"] = component_context
        return unavailable_record
    record = {
        "schema_version": "solidworks-to-urdf.mass-closure/v1",
        "status": "recorded",
        "mode": "full",
        "top_level": {
            "mass": top_mass,
            "com": [float(value) for value in top_level["com"]],
            "inertia": [[float(value) for value in row] for row in top_level["inertia"]],
            "reference": dict(top_level.get("reference") or {}),
        },
    }
    if component_context is not None:
        record["component_context"] = component_context
    return record


def _capture_readings(
    backend: Any, cfg: dict[str, Any], closure: dict[str, Any], source_root: Path
) -> tuple[Any, dict[str, Any]]:
    """Read the raw scene from the collected copy, never from the working tree.

    The closure proved the copy resolves every component inside ``source_dir``, so
    reading the copy is what makes the recorded masses and tessellation describe
    the bytes that travel with the snapshot.  The reported document is checked
    instead of trusted: a backend that quietly kept the working-tree document
    open would otherwise pass its readings off as the copy's.
    """

    document = str(closure.get("top_level") or "")
    if not document or not os.path.isfile(document):
        raise BridgeError(
            "dependency_collection_incomplete",
            "the closure left no collected copy to read",
            {"top_level": document, "source_root": str(source_root)},
            exit_code=3,
        )
    scene = backend.collect_scene(
        document,
        cfg["coordinate_systems"],
        require_material=cfg["material_source"] == "cad",
    )
    reported = str(getattr(scene, "document", "") or "")
    if not _same_document(reported, document):
        raise BridgeError(
            "capture_source_mismatch",
            "the backend read a working-tree document instead of the collected copy",
            {"requested": document, "reported": reported, "assembly": cfg["assembly"]},
            exit_code=3,
        )
    return scene, _raw_scene_payload(scene, cfg, source_root)


def _raw_scene_payload(scene: Any, cfg: dict[str, Any], source_root: Path) -> dict[str, Any]:
    """The raw/scene_raw.json projection of the captured scene.

    Rebuilt after the component mass context runs, because the effective
    instance readings are injected into ``scene.notes`` there and the earlier
    file write cannot see them.
    """

    return {
        "document": scene.document,
        "source_document": cfg["assembly"],
        "source_root": str(source_root),
        "components": [
            {
                "name": component.name,
                "path": component.path,
                "transform": list(component.transform),
                "is_fixed": component.is_fixed,
                "document_type": component.document_type,
            }
            for component in scene.components
        ],
        "coordinate_systems": {name: list(matrix) for name, matrix in scene.coordinate_systems.items()},
        "mass_properties": scene.mass_properties,
        "notes": scene.notes,
    }


def _material_provenance_record(scene: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """The stable warning record for CAD masses read from unverified materials.

    A pure projection of ``raw/mass_properties.json``: each entry carries the verbatim
    material assignment the reader produced next to the measured CAD values it came
    with.  Missing explicit material is a warnable deficiency - capture, model and
    delivery continue - but no consumer may mistake the document's default density for
    evidence.  The record is written for every capture, with an empty ``warnings`` list
    when every included solid carries a verified assignment.
    """

    warnings: list[dict[str, Any]] = []
    for name, payload in sorted((scene.mass_properties or {}).items()):
        reference = payload.get("reference") if isinstance(payload, dict) else None
        assignment = reference.get("material_assignment") if isinstance(reference, dict) else None
        if not isinstance(assignment, dict) or not assignment.get("unverified_reason"):
            continue
        reference = reference if isinstance(reference, dict) else {}
        warnings.append(
            {
                "code": str(assignment["unverified_reason"]),
                "message": str(assignment.get("message") or "material assignment is not verified"),
                "blocking": False,
                "component": str(name),
                "document": reference.get("part_document"),
                "configuration": reference.get("configuration") or assignment.get("configuration"),
                "missing_body_indices": assignment.get("missing_body_indices"),
                "material_assignment": assignment,
                "measured": {
                    "source": "cad_api",
                    "scope": reference.get("scope"),
                    "mass_kg": payload.get("mass"),
                    "center_of_mass_m": payload.get("com"),
                    "inertia_kg_m2": payload.get("inertia"),
                    "volume_m3": reference.get("volume_m3"),
                    "density_kg_m3": reference.get("density_kg_m3"),
                },
            }
        )
    return {
        "schema_version": "solidworks-to-urdf.material-provenance/v1",
        "assembly": str(cfg.get("assembly") or ""),
        "configuration": str(cfg.get("configuration") or ""),
        "material_source": str(cfg.get("material_source") or "cad"),
        "measured_source": "cad_api",
        "warnings": warnings,
    }


def _effective_instance_record(
    scene: Any, cfg: dict[str, Any], readings: dict[str, Any], cache_error: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The stable record for CAD-authored effective instance measurements.

    A surface-only occurrence whose part document cannot answer mass questions,
    and any overridden instance, are read through the assembly instance under
    the ``assembly_component_group`` scope (including CAD-authored overrides).
    The record is written for EVERY capture - with empty readings/warnings when
    none was needed - and states plainly that these values are CAD-authored
    effective data: not geometry-derived and not independently physically
    qualified.  Nothing is rescaled, substituted or distributed here.
    """

    readings_payload = {
        str(name): dict(reading)
        for name, reading in sorted((readings or {}).items())
        if isinstance(reading, dict)
    }
    warnings: list[dict[str, Any]] = []
    for name, reading in sorted(readings_payload.items()):
        reference = dict(reading.get("reference") or {})
        flags = dict(reference.get("overrides") or {})
        instance_difference = reference.get("measurement_basis") == "cad_assembly_instance_measurement"
        if not any(bool(value) for value in flags.values()) and not instance_difference:
            continue
        warnings.append(
            {
                "code": (
                    "cad_instance_measurement_differs_from_part"
                    if instance_difference else "cad_instance_override_effective"
                ),
                "blocking": False,
                "message": (
                    "the native assembly-instance measurement differs from the part-document measurement; "
                    "the cause is unproven and both are retained"
                    if instance_difference else
                    "the instance carries CAD-authored mass-property overrides; these are effective CAD values, "
                    "not geometry qualification"
                ),
                "component": name,
                "role": reading.get("role"),
                "document_type": reading.get("document_type"),
                "configuration": reference.get("configuration"),
                "part_document": reference.get("part_document"),
                "assembly_document": reference.get("assembly_document"),
                "assembly_configuration": reference.get("assembly_configuration"),
                "selected_components": reference.get("selected_components"),
                "overrides": flags,
                "qualification": (
                    "cad_assembly_instance_measurement; cause_of_part_difference_unproven; "
                    "not geometry-derived, not independently physically qualified"
                    if instance_difference else
                    "cad_authored_effective_instance; not geometry-derived, not independently physically qualified"
                ),
                **({"part_measurement": reading.get("part_measurement")} if instance_difference else {}),
                "measurement": {
                    "scope": reference.get("scope"),
                    "axes": reference.get("axes"),
                    "used_api": reference.get("used_api"),
                    "selection_api": reference.get("selection_api"),
                    "override_api": reference.get("override_api"),
                    "mass_kg": reading.get("mass"),
                    "center_of_mass_m": reading.get("com"),
                    "inertia_kg_m2": reading.get("inertia"),
                    "volume_m3": reference.get("volume_m3"),
                    "density_kg_m3": reference.get("density_kg_m3"),
                },
            }
        )
    for name, payload in sorted((getattr(scene, "mass_properties", None) or {}).items()):
        if not isinstance(payload, dict) or payload.get("mode") != "effective_instance":
            continue
        reference = dict(payload.get("reference") or {})
        warnings.append(
            {
                "code": "cad_surface_only_effective_instance",
                "blocking": False,
                "message": (
                    "the part is surface-only (no solid bodies); its mass properties come from the assembly "
                    "instance measurement, not from geometric qualification"
                ),
                "component": str(name),
                "document": reference.get("part_document"),
                "configuration": reference.get("configuration"),
                "assembly_document": reference.get("assembly_document"),
                "assembly_configuration": reference.get("assembly_configuration"),
                "selected_components": reference.get("selected_components"),
                "part_document_read": "unavailable",
                "fallback_reason": reference.get("fallback_reason"),
                "fallback_error_code": reference.get("fallback_error_code"),
                "solid_bodies": reference.get("solid_bodies"),
                "sheet_bodies": reference.get("sheet_bodies"),
                "overrides": dict(reference.get("overrides") or {}),
                "qualification": (
                    "cad_authored_effective_instance; not geometry-derived, not independently physically qualified"
                ),
                "measurement": {
                    "state": payload.get("state"),
                    "measurement_basis": payload.get("measurement_basis"),
                    "scope": reference.get("scope"),
                    "axes": reference.get("axes"),
                    "used_api": reference.get("used_api"),
                    "selection_api": reference.get("selection_api"),
                    "override_api": reference.get("override_api"),
                    "mass_kg": payload.get("mass"),
                    "center_of_mass_m": payload.get("com"),
                    "inertia_kg_m2": payload.get("inertia"),
                    "volume_m3": reference.get("volume_m3"),
                    "density_kg_m3": reference.get("density_kg_m3"),
                },
            }
        )
    return {
        "schema_version": EFFECTIVE_INSTANCES_SCHEMA,
        "assembly": str(cfg.get("assembly") or ""),
        "configuration": str(cfg.get("configuration") or ""),
        "cache_error": cache_error,
        "readings": readings_payload,
        "warnings": warnings,
    }


def _verify_originals_unchanged(closure: dict[str, Any]) -> dict[str, Any]:
    """Rehash saved inputs without reopening the retired source application.

    Original configuration/save observations describe the initial source session,
    not current state. The disk revision supplied the copy; all live configuration
    and occurrence guards apply to that copy, which supplies the measurements.
    """

    recorded = dict(closure.get("original_files") or {})
    changed: list[dict[str, Any]] = []
    for path, digest in recorded.items():
        try:
            current = sha256_file(Path(path))
        except OSError:
            current = ""
        if current != digest:
            changed.append({"path": path, "before": digest, "after": current or None})
    if changed:
        raise BridgeError(
            "cad_source_changed",
            "a working-tree document changed while the snapshot was being captured",
            {"changed": changed[:20], "count": len(changed)},
            exit_code=3,
        )
    return {
        "files_checked": len(recorded),
        "states_recorded": len(closure.get("original_states") or {}),
        "state_scope": "initial_source_observation",
    }


def _capture_axis_references(backend: Any, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve every structured joint axis reference to a native line.

    A joint that names a native component/face must yield numeric evidence
    (point + direction + identity); a backend that cannot resolve it is a
    capture failure, never a silently skipped record.
    """

    reader = getattr(backend, "capture_axis_reference", None)
    captured: list[dict[str, Any]] = []
    for joint in cfg.get("joints") or []:
        reference = joint.get("axis_reference")
        if not isinstance(reference, dict):
            continue
        joint_name = str(joint.get("name") or joint.get("id") or "")
        if not callable(reader):
            raise BridgeError(
                "cad_axis_reference_unsupported",
                "this backend cannot resolve a structured joint axis reference",
                {"joint": joint_name, "axis_reference": reference},
                exit_code=3,
            )
        captured.append({"joint": joint_name, **reader(reference)})
    return captured


def _export_geometry(
    backend: Any, cfg: dict[str, Any], geometry_dir: Path, components: list[str]
) -> list[dict[str, Any]]:
    geometry_dir.mkdir(parents=True, exist_ok=True)
    # Keep one native geometry phase alive across all occurrence reads.
    # Instance identities stay in the evidence; filenames remain bounded.
    destinations = {
        component: str(geometry_dir / f"{index:04d}_{digest_json(component)[:24]}.stl")
        for index, component in enumerate(sorted(components), start=1)
    }
    if len(destinations) != len(components):
        raise BridgeError("cad_mesh_export_failed", "geometry contains duplicate occurrence identities", exit_code=3)
    entries = backend.export_component_meshes(destinations)
    if not isinstance(entries, dict) or set(entries) != set(destinations):
        raise BridgeError("cad_mesh_export_failed", "geometry export returned a different occurrence set", exit_code=3)
    exported: list[dict[str, Any]] = []
    for component, destination in destinations.items():
        target = Path(destination)
        info = entries[component]
        if not isinstance(info, dict) or info.get("component") != component or str(info.get("written")) != destination:
            raise BridgeError(
                "cad_mesh_export_failed",
                "geometry export returned a different occurrence or path",
                {"component": component},
                exit_code=3,
            )
        # Raw identity plus the binary-header triangle count only: area, volume,
        # topology and degeneracy stay with the independent Linux verifier.
        with target.open("rb") as handle:
            header = handle.read(84)
        if len(header) < 84:
            raise BridgeError(
                "cad_mesh_invalid",
                "geometry export produced no binary STL header",
                {"component": component},
                exit_code=3,
            )
        triangles = int.from_bytes(header[80:84], "little")
        if triangles <= 0 or target.stat().st_size != 84 + 50 * triangles:
            raise BridgeError(
                "cad_mesh_invalid",
                "geometry export produced an inconsistent mesh size",
                {"component": component},
                exit_code=3,
            )
        exported.append(
            {
                "component": component,
                "path": target.relative_to(geometry_dir.parent).as_posix(),
                "triangles": triangles,
                "used_api": info.get("used_api"),
                "bodies": info.get("bodies"),
                "tessellation": info.get("tessellation") or {"faces": 0, "no_facets_faces": []},
                "sha256": sha256_file(target),
            }
        )
    return exported


LINK_GROUP_SCHEMA = LINK_GROUP_MASS_SCHEMA
LINK_GROUP_USED_API = "IMassProperty2 (SelectedItems = component set)"
LINK_GROUP_SELECTION_RULE = "top_most_providers+uncovered_leaves"
#: Marker for the whole-assembly and direct link reads taken in the reopened,
#: never-touched readout window (probe v5: close+reopen restores the opened
#: state bit-identically; any earlier instance read in the session shifts the
#: direct group reads and the whole-assembly read - probe v4).
PRISTINE_WINDOW = "pristine-reopen"


def _link_group_selections(cfg: dict[str, Any], scene: Any, effective: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Compose each body's selection from the captured scene's per-instance evidence.

    Provider names come from the recorded evidence exactly as the verifier
    recomputes them (notes effective instances and effective-instance rows);
    the shared core validates and collapses them.
    """

    leaves = [str(component.name) for component in (scene.components or [])]
    provider_names = {str(name) for name in effective}
    provider_names |= {
        str(name)
        for name, row in (getattr(scene, "mass_properties", None) or {}).items()
        if isinstance(row, dict) and row.get("mode") == "effective_instance"
    }
    return _compose_link_selections(cfg, leaves, provider_names)


def _compose_link_selections(
    cfg: dict[str, Any], leaves: Sequence[str], provider_names: set[str]
) -> dict[str, dict[str, Any]]:
    """Compose each rigid body's direct-read selection (top-most providers + uncovered leaves).

    Mirrors the verifier's independent recomputation: providers are effective
    instances (any override flag or a flagless instance measurement) whose whole
    leaf subtree lies inside one body; a nested provider collapses into the
    top-most one; every remaining body leaf is selected individually.  Unknown
    members, duplicated membership, providers that span or only partially cover
    bodies, and empty selections fail the capture closed.
    """

    leaves = [str(name) for name in leaves]
    if len(set(leaves)) != len(leaves):
        raise BridgeError("link_group_occurrence_duplicate", "the captured scene repeats an occurrence", exit_code=3)
    leaf_set = set(leaves)
    bodies = list(cfg.get("bodies") or [])
    if not bodies:
        raise BridgeError("link_group_body_missing", "native discovery defines no rigid bodies", exit_code=3)
    body_of: dict[str, str] = {}
    for body in bodies:
        body_name = str(body.get("name") or body.get("id") or "")
        if not body_name:
            raise BridgeError("link_group_body_unnamed", "every body needs a name", exit_code=3)
        for member in body.get("components") or []:
            member = str(member)
            if member not in leaf_set:
                raise BridgeError(
                    "link_group_member_unknown",
                    "a body references an unknown occurrence",
                    {"body": body_name, "component": member},
                    exit_code=3,
                )
            if member in body_of:
                raise BridgeError(
                    "link_group_member_duplicate",
                    "an occurrence belongs to two bodies",
                    {"component": member, "bodies": [body_of[member], body_name]},
                    exit_code=3,
                )
            body_of[member] = body_name
    providers: dict[str, set[str]] = {}
    for provider in sorted(provider_names):
        subtree = {leaf for leaf in leaves if leaf == provider or leaf.startswith(provider + "/")}
        if not subtree:
            raise BridgeError(
                "link_group_provider_unknown",
                "an effective instance is not a captured occurrence",
                {"provider": provider},
                exit_code=3,
            )
        owners = {body_of.get(leaf) for leaf in subtree}
        named = {owner for owner in owners if owner}
        if len(named) > 1:
            raise BridgeError(
                "link_group_provider_spans_bodies",
                "an effective instance spans several bodies",
                {"provider": provider, "bodies": sorted(named), "descendants": sorted(subtree)[:50]},
                exit_code=3,
            )
        if named and len(owners) != 1:
            raise BridgeError(
                "link_group_provider_partial",
                "an effective instance is only partially assigned to a body",
                {"provider": provider, "body": sorted(named)[0], "descendants": sorted(subtree)[:50]},
                exit_code=3,
            )
        if not named:
            # Entirely unassigned subtree: it cannot be a provider for any body;
            # the closure record already accounts for the unassigned mass.
            continue
        providers[provider] = subtree
    selections: dict[str, dict[str, Any]] = {}
    for body in bodies:
        body_name = str(body.get("name") or body.get("id") or "")
        members = {str(item) for item in body.get("components") or []}
        inner = [provider for provider, subtree in providers.items() if subtree <= members]
        top_most = [
            provider
            for provider in inner
            if not any(other != provider and provider.startswith(other + "/") for other in inner)
        ]
        covered: set[str] = set()
        for provider in top_most:
            covered |= providers[provider]
        uncovered = sorted(members - covered)
        selection = sorted(top_most) + uncovered
        if not selection:
            raise BridgeError(
                "link_group_empty_selection",
                "a body has no selectable occurrence",
                {"body": body_name, "members": sorted(members)},
                exit_code=3,
            )
        selections[body_name] = {"selection": selection, "providers": sorted(top_most), "uncovered_leaves": uncovered}
    return selections


def _link_group_record_from_payload(
    payload: Any, selections: dict[str, dict[str, Any]], document: str | None
) -> dict[str, Any]:
    """Validate one native link group payload against the composed selections."""

    if (
        not isinstance(payload, dict)
        or payload.get("used_api") != LINK_GROUP_USED_API
        or payload.get("selection_rule") != LINK_GROUP_SELECTION_RULE
    ):
        raise BridgeError(
            "cad_link_group_payload_invalid",
            "the backend returned an unexpected link group payload",
            exit_code=3,
        )
    links = payload.get("links")
    if not isinstance(links, dict) or set(links) != set(selections):
        raise BridgeError(
            "cad_link_group_payload_invalid",
            "the link group payload does not cover the composed bodies",
            {"expected": sorted(selections), "received": sorted(links or [])},
            exit_code=3,
        )
    for name, entry in selections.items():
        row = links[name]
        reads = row.get("reads") if isinstance(row, dict) else None
        if not isinstance(row, dict) or row.get("selection") != entry["selection"]:
            raise BridgeError(
                "cad_link_group_payload_invalid",
                "the link group payload echoes another selection",
                {"body": name},
                exit_code=3,
            )
        if not isinstance(reads, list) or len(reads) != 2:
            raise BridgeError(
                "cad_link_group_payload_invalid",
                "the link group payload must carry two reads",
                {"body": name},
                exit_code=3,
            )
        for index, read in enumerate(reads):
            _link_group_read_valid(name, index, read)
        reproducibility = row.get("reproducibility")
        if not isinstance(reproducibility, dict) or set(reproducibility) != {
            "mass_delta_kg",
            "com_delta_norm_m",
            "tensor_delta_max_abs",
        }:
            raise BridgeError(
                "cad_link_group_payload_invalid",
                "the link group payload carries no reproducibility record",
                {"body": name},
                exit_code=3,
            )
        for key, value in reproducibility.items():
            if type(value) not in (int, float) or not math.isfinite(float(value)) or float(value) < 0.0:
                raise BridgeError(
                    "cad_link_group_payload_invalid",
                    "the reproducibility record is malformed",
                    {"body": name, "field": key},
                    exit_code=3,
                )
    assembly = str(payload.get("assembly") or "")
    if document and not _same_document(assembly, str(document)):
        raise BridgeError(
            "capture_source_mismatch",
            "the link group read names another document",
            {"requested": str(document), "reported": assembly},
            exit_code=3,
        )
    return {
        "schema_version": LINK_GROUP_SCHEMA,
        "assembly": assembly,
        "configuration": str(payload.get("configuration") or ""),
        "used_api": LINK_GROUP_USED_API,
        "selection_rule": LINK_GROUP_SELECTION_RULE,
        "links": {
            name: {
                "selection": list(links[name]["selection"]),
                "reads": links[name]["reads"],
                "reproducibility": links[name]["reproducibility"],
            }
            for name in selections
        },
    }


def _link_group_read_valid(body: str, index: int, read: Any) -> None:
    def invalid(field: str) -> None:
        raise BridgeError(
            "cad_link_group_payload_invalid",
            "a link group read is malformed",
            {"body": body, "read": index, "field": field},
            exit_code=3,
        )

    if not isinstance(read, dict):
        invalid("read")
    mass = read.get("mass_kg")
    if type(mass) not in (int, float) or not math.isfinite(float(mass)) or float(mass) <= 0.0:
        invalid("mass_kg")
    com = read.get("com_m")
    if (
        not isinstance(com, list)
        or len(com) != 3
        or any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in com)
    ):
        invalid("com_m")
    tensor = read.get("inertia_tensor_about_com")
    if (
        not isinstance(tensor, list)
        or len(tensor) != 3
        or any(
            not isinstance(row, list)
            or len(row) != 3
            or any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in row)
            for row in tensor
        )
    ):
        invalid("inertia_tensor_about_com")
    overrides = read.get("overrides")
    if (
        not isinstance(overrides, dict)
        or set(overrides) != {"OverrideMass", "OverrideCenterOfMass", "OverrideMomentsOfInertia"}
        or any(type(value) is not bool for value in overrides.values())
    ):
        invalid("overrides")


def _top_level_record(reading: dict[str, Any]) -> dict[str, Any]:
    """The mass_closure ``top_level`` projection of one whole-assembly reading."""

    return {
        "mass": float(reading["mass"]),
        "com": [float(value) for value in reading["com"]],
        "inertia": [[float(value) for value in row] for row in reading["inertia"]],
        "reference": dict(reading.get("reference") or {}),
    }


def _pristine_readout_window(
    backend: Any,
    cfg: dict[str, Any],
    closure: dict[str, Any],
    selections: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Whole-assembly and direct link reads in the pristine reopen window.

    Probe v4: any component-instance mass read contaminates the later direct
    group reads and the whole-assembly read of the same session.  Probe v5:
    closing the read-only probe session and reopening the same captured copy
    restores the opened state bit-identically (deltas exactly zero, 61/61
    documents).  Closing the real capture needs ``CloseAllDocuments(True)``:
    the collected copy was opened read-write and rebuilt/recalculated, so the
    dirty documents only release with close-without-save (native16 left 61
    documents open with False); nothing after this window saves and freeze
    re-hashes the post-window copy to prove no writes.  This runs after every
    other CAD-facing step; inside the window only the two readers run, in the
    v5-proven order (link groups first, whole assembly second), and nothing
    else touches the reopened document.
    """

    document = str((closure or {}).get("top_level") or cfg["assembly"])
    closer = getattr(backend, "close_all_documents", None)
    opener = getattr(backend, "open_document", None)
    reader = getattr(backend, "link_group_mass_properties", None)
    if not callable(closer) or not callable(opener) or not callable(reader):
        raise BridgeError(
            "cad_pristine_readout_unavailable",
            "the backend cannot reopen a pristine readout window",
            exit_code=3,
        )
    closer()
    prepare = getattr(backend, "prepare_source", None)
    if callable(prepare):
        prepare(document, cfg["configuration"])
    opened = opener(document)
    if not isinstance(opened, dict) or not _same_document(str(opened.get("path") or ""), document):
        raise BridgeError(
            "capture_source_mismatch",
            "the pristine reopen named another document",
            {
                "requested": document,
                "reported": opened.get("path") if isinstance(opened, dict) else opened,
            },
            exit_code=3,
        )
    payload = reader(document, {name: entry["selection"] for name, entry in selections.items()})
    record = _link_group_record_from_payload(payload, selections, document)
    record["readout_window"] = PRISTINE_WINDOW
    assembly = getattr(backend, "assembly_mass_properties", None)
    top_level = None
    if callable(assembly):
        try:
            top_level = assembly(document)
        except Exception:  # noqa: BLE001 - mirrors _mass_closure's optional whole-assembly reader
            top_level = None
    closer()
    return {"top_level": top_level, "record": record}


def _freeze_local(
    config: dict[str, Any],
    destination: Path,
    *,
    backend: Any,
    worker_version: str = "unknown",
) -> dict[str, Any]:
    """Capture ``config['assembly']`` into ``destination`` and return the manifest.

    With ``source.worker_url`` set, the capture runs on a Windows worker over the
    loopback tunnel instead of calling COM in this process.
    """

    # Check the path the caller gave us *before* resolving: resolving a symlink
    # would hide the fact that the destination is a link.
    destination = Path(destination)
    existing_empty = _prepare_destination(destination)
    destination = destination.resolve()
    cfg = validate_source_config(config)
    if not os.path.isfile(cfg["assembly"]):
        raise BridgeError(
            "assembly_missing", "the configured assembly does not exist", {"assembly": cfg["assembly"]}, exit_code=3
        )
    extension = os.path.splitext(cfg["assembly"])[1].lower()
    if extension not in cfg["document_suffixes"]:
        raise ConfigError("source.assembly must point at a supported CAD document", {"extension": extension})
    staging = destination.with_name(destination.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    request = {
        "assembly": cfg["assembly"],
        "configuration": cfg["configuration"],
        "allowed_roots": cfg["allowed_roots"],
        "coordinate_systems": cfg["coordinate_systems"],
        "geometry": cfg["geometry"],
        "material_source": cfg["material_source"],
        "require_saved": cfg["require_saved"],
        "bodies": len(cfg["bodies"]),
        "joints": len(cfg["joints"]),
        "declared_masses": len(cfg.get("documented_masses") or {}),
    }
    stage = "document_state"
    try:
        prepare = getattr(backend, "prepare_source", None)
        if callable(prepare):
            prepare(cfg["assembly"], cfg["configuration"])
        # `state["saved"]` is SolidWorks' own "would prompt to save" answer.  It is
        # recorded in raw/document_state.json, and it never blocks: the capture copies
        # the revision from disk in a session the adapter owns, so the flag cannot
        # say anything about the operator's session (SolidWorksBackend._record_save_flag).
        state = _document_state(backend, cfg)
        if state.get("configurations") and cfg["configuration"] not in state["configurations"]:
            raise BridgeError(
                "cad_configuration_missing",
                "the requested configuration does not exist in the document",
                {"requested": cfg["configuration"], "available": state["configurations"]},
                exit_code=3,
            )

        stage = "dependency_closure"
        closure = _dependency_closure(backend, cfg, staging / "source")
        # The source is retired and the copy is active. Persist their identities before
        # native reads so a failed geometry capture retains its environment.
        stage = "environment"
        environment = capture_environment(backend, worker_version)
        write_json(staging / "evidence" / "environment.json", environment)
        stage = "readings"
        scene, raw = _capture_readings(backend, cfg, closure, staging / "source")
        write_json(staging / "raw" / "scene_raw.json", raw)
        write_json(staging / "raw" / "document_state.json", state)
        write_json(staging / "raw" / "dependency_closure.json", closure)
        write_json(staging / "raw" / "coordinate_systems.json", raw["coordinate_systems"])
        write_json(staging / "raw" / "mass_properties.json", raw["mass_properties"])
        write_json(
            staging / "raw" / "material_provenance.json",
            _material_provenance_record(scene, cfg),
        )
        axis_references = _capture_axis_references(backend, cfg)
        if axis_references:
            write_json(staging / "raw" / "axis_references.json", axis_references)
        # Computed here so `component_context` observes exactly the walked
        # session; `top_level` is replaced by the pristine window reading below.
        mass_closure = _mass_closure(backend, cfg, getattr(scene, "document", None))
        # Effective instance readings (overridden instances, sheet-only
        # fallback) come from the SAME component-context traversal; they are
        # injected into the scene notes and the raw scene file is rewritten so
        # notes, canonical record and closure rows carry exactly the same
        # reading.  The canonical record is written for every capture.
        backend_readings: dict[str, Any] = {}
        cache_error: dict[str, Any] | None = None
        collector = getattr(backend, "effective_instance_readings", None)
        if callable(collector):
            try:
                backend_readings = collector()
            except Exception as error:  # noqa: BLE001 - preserved as evidence, never fatal
                cache_error = {
                    "reason": str(getattr(error, "code", "") or type(error).__name__),
                    "message": " ".join(str(error).split())[:200],
                }
        if cache_error is not None:
            scene.notes["effective_instances_error"] = cache_error
        scene.notes["effective_instances"] = backend_readings
        write_json(staging / "raw" / "scene_raw.json", _raw_scene_payload(scene, cfg, staging / "source"))
        write_json(
            staging / "raw" / "effective_instances.json",
            _effective_instance_record(scene, cfg, backend_readings, cache_error),
        )
        # Declared author decisions are recorded as declared input, never as CAD
        # readings: freeze keeps the snapshot raw and load_scene applies them.
        write_json(
            staging / "raw" / "declared_masses.json",
            {
                "schema_version": "solidworks-to-urdf.declared-masses/v1",
                "evidence": cfg.get("mass_evidence"),
                "items": cfg["documented_masses"],
            },
        )

        geometry_entries: list[dict[str, Any]] = []
        if cfg["geometry"].get("enabled", True):
            stage = "geometry"
            components = [component.name for component in scene.components]
            geometry_entries = _export_geometry(backend, cfg, staging / "geometry", components)
            write_json(staging / "raw" / "geometry.json", geometry_entries)

        stage = "verify_sources"
        # Verify the live copy state and saved original bytes independently.
        backend.verify_sources_unchanged()
        originals = _verify_originals_unchanged(closure)
        copied = _copy_inventory(staging / "source")
        if copied != closure["copy_files"]:
            changed = sorted(
                path
                for path in set(copied) | set(closure["copy_files"])
                if copied.get(path) != closure["copy_files"].get(path)
            )
            raise BridgeError(
                "cad_copy_changed",
                "collected files changed while readings were captured",
                {"changed": changed},
                exit_code=3,
            )

        # Pristine readout window: every CAD-facing step above may have
        # contaminated the session's direct group reads (probe v4), so close
        # all documents and reopen the captured copy; the whole-assembly read
        # and all direct link-group reads then come from a never-touched window
        # (probe v5: close+reopen restores the opened state bit-identically).
        # This is the last CAD activity of the capture; only file-level steps
        # follow.  The record selections are the late composition of the walked
        # per-instance evidence and the payload echo must match them exactly.
        stage = "pristine_readout"
        selections = _link_group_selections(cfg, scene, backend_readings)
        window = _pristine_readout_window(backend, cfg, closure, selections)
        if isinstance(mass_closure, dict) and mass_closure.get("status") == "recorded":
            if not isinstance(window["top_level"], dict):
                raise BridgeError(
                    "cad_pristine_readout_unavailable",
                    "the reopened window produced no whole-assembly reading",
                    exit_code=3,
                )
            mass_closure["top_level"] = _top_level_record(window["top_level"])
            mass_closure["top_level"]["reference"]["readout_window"] = PRISTINE_WINDOW
        if mass_closure is not None:
            write_json(staging / "raw" / "mass_closure.json", mass_closure)
        write_json(staging / "raw" / "link_group_mass_properties.json", window["record"])

        # Scene construction is portable work. Seal its exact inputs here;
        # Linux derives the scene without rewriting this native snapshot.
        write_json(staging / "raw" / "source_config.json", cfg)

        source_inputs = _source_inputs(cfg, closure)
        identity = {
            "provider": SOURCE_KIND,
            "assembly": cfg["assembly"],
            "document_id": cfg["assembly"],
            "configuration": cfg["configuration"],
            "worker_version": worker_version,
            "solidworks_revision": (environment.get("solidworks") or {}).get("revision"),
            "dependency_digest": digest_json(sorted(source_inputs.items())),
            "capture": {
                "evidence_class": _evidence_class(backend, cfg),
                "coordinate_systems": cfg["coordinate_systems"],
                "geometry": cfg["geometry"],
                "require_saved": cfg["require_saved"],
            },
        }
        evidence = build_evidence(
            identity=identity,
            environment=environment,
            dependency_closure=closure,
            capture={
                "components": len(scene.components),
                "mass_properties": len(scene.mass_properties),
                "geometry_files": len(geometry_entries),
                # The revision read from disk, by the same names the digest uses.  The
                # snapshot's own copies are bound by manifest.json; naming them here with
                # job paths would point at directories that no longer exist.
                "source_hashes": {name: {"sha256": digest} for name, digest in sorted(source_inputs.items())},
                "collected_document": closure.get("top_level"),
                "originals_unchanged": originals,
                # SolidWorks' own save flags, kept for the reader: the snapshot names the
                # bytes it copied, and a flag never decided whether the capture ran.
                "save_flag_documents": list(closure.get("save_flag_documents") or []),
            },
            limits={
                "native_cad": True,
                "kinematics_defined_by": "source.bodies/source.joints" if cfg["joints"] else "not_defined",
                "material_source": cfg["material_source"],
            },
        )
        write_json(staging / "evidence" / "collection.json", evidence)

        # Windows cannot rename a directory while CAD owns file handles inside
        # it. Release the owned applications before atomically publishing.
        stage = "release"
        release = getattr(backend, "release", None)
        if callable(release):
            release()
        # Job termination can leave session locks behind. Only discard locks
        # inside our staged copy, after all owned CAD handles have been released.
        for lock in _native_lock_files(staging / "source"):
            lock.unlink()
        if _copy_inventory(staging / "source") != closure["copy_files"]:
            raise BridgeError("cad_source_changed", "collected files changed during CAD cleanup", exit_code=3)

        stage = "publish"
        write_manifest = _shared_manifest_writer()
        manifest = write_manifest(
            staging,
            kind=SOURCE_KIND,
            identity=identity,
            evidence_class=_evidence_class(backend, cfg),
        )
        write_json(staging / "manifest.json", manifest)
        if existing_empty:
            # The caller owns the directory; replace it only once the snapshot
            # is complete, so an interrupted freeze never leaves a half snapshot.
            destination.rmdir()
        os.replace(staging, destination)
        return manifest
    except BaseException as error:
        release = getattr(backend, "release", None)
        if callable(release):
            with contextlib.suppress(Exception):
                release()
        _retain_failure(staging, destination, error, stage, request)
        raise


def freeze(
    config: dict[str, Any],
    destination: Path,
    *,
    backend: Any = None,
    worker_version: str = "unknown",
) -> dict[str, Any]:
    """Freeze from saved files in a job-owned STA session, or submit to its worker."""
    from contextlib import nullcontext

    if backend is None and isinstance(config, dict) and config.get("worker_url"):
        raise ConfigError("Remote capture was removed; run the complete pipeline on the native Windows endpoint")
    if backend is None:
        from .native import SolidWorksBackend

        backend = SolidWorksBackend()
    session = getattr(backend, "session", None)
    with session() if callable(session) else nullcontext():
        return _freeze_local(config, destination, backend=backend, worker_version=worker_version)
