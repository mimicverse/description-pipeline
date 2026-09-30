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
from collections.abc import Callable

from .errors import BridgeError, ConfigError
from .evidence import build_evidence, capture_environment
from .jsonio import digest_json, sha256_file, write_json
from .scene import assembly_leaf_total, build_scene, closure_delta

# The pipeline-wide STL reader owns the acceptance standard for meshes; the
# adapter only writes geometry and then asks that reader what it produced.
from ...geometry.stl import read as read_stl
from ...io import inventory

FREEZE_SCHEMA = "description-pipeline.solidworks-freeze/v1"
SOURCE_KIND = "solidworks"
DEFAULT_ALLOWED_SUFFIXES = (".sldasm", ".sldprt")
# Every key the adapter understands; anything else is a typo or an unimplemented
# feature and must fail instead of being ignored.
SOURCE_KEYS = {
    "provider",
    "assembly",
    "configuration",
    "allowed_roots",
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
    unresolved = [str(entry["path"]) for entry in dependencies if not entry.get("exists", True)]
    outside = [
        entry["path"]
        for entry in dependencies
        if entry.get("exists", True) and cfg["allowed_roots"] and not _under(str(entry["path"]), cfg["allowed_roots"])
    ]
    if outside:
        raise BridgeError(
            "dependency_outside_allowed_roots",
            "the assembly references files outside source.allowed_roots; widen the roots explicitly",
            {"paths": outside[:20], "count": len(outside)},
            exit_code=3,
        )

    inspector = getattr(backend, "inspect_copy", None)
    if not callable(inspector):
        raise BridgeError(
            "dependency_resolution_unverified",
            "the backend cannot re-open the copy to verify component coverage",
            exit_code=3,
        )
    # Bind every saved source file before copying, then recheck after all reads.
    original = dict(inspector(cfg["assembly"]) or {})
    original_files: dict[str, str] = {}
    original_documents = [str(cfg["assembly"])]
    original_documents.extend(str(entry.get("document") or "") for entry in original.get("instances") or [])
    original_documents.extend(str(entry.get("path") or "") for entry in dependencies)
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
    collected = dict(pack(cfg["assembly"], str(source_dir)) or {})
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
        if not path:
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
        "schema_version": "description-pipeline.solidworks-freeze-failure/v1",
        "stage": stage,
        "code": getattr(error, "code", type(error).__name__),
        "message": str(error),
        "detail": _redact(getattr(error, "detail", None), "detail"),
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


def _leaf_volume(mass_properties: dict[str, Any], name: Any) -> float:
    """A leaf's own volume when the capture recorded one; absent or unusable context counts as zero."""

    payload = mass_properties.get(str(name)) or {}
    reference = payload.get("reference") or {}
    try:
        return float(reference.get("volume_m3") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _component_context_record(reading: dict[str, Any], scene: Any, assembly_mass: float | None) -> dict[str, Any]:
    """Align the assembly context's per-instance readings with the leaf/document basis.

    ``document_basis_mass_kg`` is the sum of the existing leaf/document readings under that
    instance (itself, for a leaf).  Totals use only the disjoint depth-0 rows; nested rows are kept
    to detect overrides a clean parent would otherwise hide.  Everything here is recorded for
    comparison only: no total is forced to agree and nothing is distributed into any declared mass.
    """

    leaves = [
        (str(component.name), float((scene.mass_properties.get(str(component.name)) or {}).get("mass") or 0.0))
        for component in scene.components
    ]

    def document_basis(name: str) -> float:
        prefix = name + "/"
        return sum(mass for leaf, mass in leaves if leaf == name or leaf.startswith(prefix))

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
        basis = document_basis(name)
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
                "document_basis_mass_kg": basis,
                "delta_kg": mass - basis,
            }
        )
    leaf_names = {leaf for leaf, _ in leaves}
    covered = {
        leaf
        for leaf in leaf_names
        if any(leaf == entry["name"] or leaf.startswith(entry["name"] + "/") for entry in instances)
    }
    top_level = [entry for entry in instances if entry["depth"] == 0]
    context_total = sum(entry["context_mass_kg"] for entry in top_level)
    document_total = sum(entry["document_basis_mass_kg"] or 0.0 for entry in top_level)
    assembly_kg = None if assembly_mass is None else float(assembly_mass)
    return {
        "schema_version": "description-pipeline.solidworks-component-mass-context/v1",
        "status": "recorded" if not errors else "partial",
        "assembly_mass_kg": assembly_kg,
        "instances": instances,
        "totals": {
            "context_kg": context_total,
            "document_kg": document_total,
            "assembly_kg": assembly_kg,
            "context_minus_document_kg": context_total - document_total,
            "context_minus_assembly_kg": None if assembly_kg is None else context_total - assembly_kg,
        },
        "overridden": {
            "top_level_instances": len(top_level),
            "mass": sum(1 for entry in instances if entry["overrides"].get("OverrideMass")),
            "com": sum(1 for entry in instances if entry["overrides"].get("OverrideCenterOfMass")),
            "inertia": sum(1 for entry in instances if entry["overrides"].get("OverrideMomentsOfInertia")),
            "any": sum(1 for entry in instances if any(entry["overrides"].values())),
            "top_level_overridden": sum(1 for entry in top_level if any(entry["overrides"].values())),
            "examples": [entry["name"] for entry in instances if entry["overrides"].get("OverrideMass")][:50],
        },
        "leaf_documents": len(leaf_names),
        "leaf_documents_covered": len(covered),
        "leaf_documents_uncovered": sorted(leaf_names - covered)[:50],
        "errors": errors,
        "reference": dict(reading.get("reference") or {}),
    }


def _component_mass_context(
    backend: Any, cfg: dict[str, Any], scene: Any, assembly_mass: float | None
) -> dict[str, Any] | None:
    """The assembly context reading when the backend can produce it; best-effort, never fatal."""

    reader = getattr(backend, "assembly_component_mass_properties", None)
    if not callable(reader):
        return None
    try:
        reading = reader(cfg["assembly"])
    except Exception as error:  # noqa: BLE001 - an optional probe must not fail a valid capture
        return {
            "schema_version": "description-pipeline.solidworks-component-mass-context/v1",
            "status": "unavailable",
            "reason": str(getattr(error, "code", "") or type(error).__name__),
            "message": " ".join(str(error).split())[:200],
        }
    if not isinstance(reading, dict):
        return {
            "schema_version": "description-pipeline.solidworks-component-mass-context/v1",
            "status": "unavailable",
            "reason": "cad_component_context_malformed",
            "message": "the backend returned no component mass context object",
        }
    try:
        return _component_context_record(reading, scene, assembly_mass)
    except Exception as error:  # noqa: BLE001 - an optional probe must never abort a capture
        return {
            "schema_version": "description-pipeline.solidworks-component-mass-context/v1",
            "status": "unavailable",
            "reason": "cad_component_context_invalid",
            "message": " ".join(str(error).split())[:200],
        }


def _mass_closure(backend: Any, cfg: dict[str, Any], scene: Any) -> dict[str, Any] | None:
    """The assembly's own reading next to the recombined leaf readings, as capture evidence.

    A backend that cannot read the whole assembly (fixtures, other providers) records nothing; the
    verification side then reports the check as not applicable instead of failing it.  A difference
    between the two readings is evidence about the CAD tree, so it is recorded here and reported as
    an advisory there — this capture never fails because of it.

    The assembly read itself is best-effort: builds where ``CreateMassProperty2`` is unavailable, or
    assemblies that refuse the read, record an explicit ``unavailable`` status.  A capture is never
    aborted by the optional probe; malformed *standard* readings (the leaf data the combination
    needs) still fail the freeze through the code below.
    """

    closure_reader = getattr(backend, "assembly_mass_properties", None)
    context_reader = getattr(backend, "assembly_component_mass_properties", None)
    if not callable(closure_reader) and not callable(context_reader):
        return None
    top_level: dict[str, Any] | None = None
    failure: dict[str, str] | None = None
    if callable(closure_reader):
        try:
            top_level = closure_reader(cfg["assembly"])
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
    component_context = _component_mass_context(backend, cfg, scene, top_mass)
    if top_level is None or top_mass is None:
        unavailable_record: dict[str, Any] = {
            "schema_version": "description-pipeline.solidworks-mass-closure/v1",
            "status": "unavailable",
            **(
                failure
                or {"reason": "cad_mass_property_unavailable", "message": "no assembly mass reading was recorded"}
            ),
        }
        if component_context is not None:
            unavailable_record["component_context"] = component_context
        return unavailable_record
    leaf_total = assembly_leaf_total(scene.components, scene.mass_properties)
    record: dict[str, Any]
    if str(top_level.get("mode") or "") == "mass_only":
        # The legacy API gives a corroborated mass only; volume travels as context.  The 2026-09-29
        # native pairing matched COM and the inertia group too, but one document is not a layout
        # guarantee, so nothing else is taken from the vector here.
        volumes = sum(_leaf_volume(scene.mass_properties, component.name) for component in scene.components)
        leaf_mass = float(leaf_total["mass"])
        record = {
            "schema_version": "description-pipeline.solidworks-mass-closure/v1",
            "status": "recorded",
            "mode": "mass_only",
            "top_level": {
                "mass": top_mass,
                "volume_m3": top_level.get("volume_m3"),
                "reference": dict(top_level.get("reference") or {}),
            },
            "leaf_total": {"mass": leaf_mass, "volume_m3": volumes},
            "leaf_components": len(list(scene.components)),
            "not_inferred": ["com", "inertia"],
            "delta": {
                "mass_abs": abs(top_mass - leaf_mass),
                "mass_rel": abs(top_mass - leaf_mass) / max(abs(top_mass), abs(leaf_mass), 1e-12),
            },
        }
    else:
        record = {
            "schema_version": "description-pipeline.solidworks-mass-closure/v1",
            "status": "recorded",
            "mode": "full",
            "top_level": {
                "mass": top_mass,
                "com": [float(value) for value in top_level["com"]],
                "inertia": [[float(value) for value in row] for row in top_level["inertia"]],
                "reference": dict(top_level.get("reference") or {}),
            },
            "leaf_total": leaf_total,
            "leaf_components": len(list(scene.components)),
            "delta": closure_delta(top_level, leaf_total),
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
    raw = {
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
    return scene, raw


def _verify_originals_unchanged(backend: Any, closure: dict[str, Any]) -> dict[str, Any]:
    """Re-check the working-tree documents the snapshot was collected from.

    The readings come from the copy, so the working tree still has to hold the bytes
    the copy was made from: the file digests catch a source written while the capture
    ran, and the per-document active configuration catches a document that was
    switched to another configuration while we read it.  ``GetSaveFlag`` is recorded
    but never compared - it answers "would SolidWorks prompt to save?", which many
    operations set and which says nothing about the revision on disk.
    """

    recorded = dict(closure.get("original_files") or {})
    states = dict(closure.get("original_states") or {})
    changed: list[dict[str, Any]] = []
    drifted: list[dict[str, Any]] = []
    for path, digest in recorded.items():
        try:
            current = sha256_file(Path(path))
        except OSError:
            current = ""
        if current != digest:
            changed.append({"path": path, "before": digest, "after": current or None})
    checked_states = 0
    for path, before in states.items():
        checked_states += 1
        try:
            state = _document_state(backend, {"assembly": path})
        except Exception as exc:  # noqa: BLE001 - unreadable after the fact is a change
            drifted.append({"path": path, "problem": "unreadable", "error": f"{type(exc).__name__}: {exc}"})
            continue
        after = {"active_configuration": state["active_configuration"]}
        expected = {"active_configuration": before["active_configuration"]}
        if after != expected:
            drifted.append({"path": path, "before": expected, "after": after})
    if changed or drifted:
        raise BridgeError(
            "cad_source_changed",
            "a working-tree document changed while the snapshot was being captured",
            {"changed": changed[:20], "state_drift": drifted[:20], "count": len(changed) + len(drifted)},
            exit_code=3,
        )
    return {"files_checked": len(recorded), "states_checked": checked_states}


def _export_geometry(
    backend: Any, cfg: dict[str, Any], geometry_dir: Path, components: list[str]
) -> list[dict[str, Any]]:
    geometry_dir.mkdir(parents=True, exist_ok=True)
    exported: list[dict[str, Any]] = []
    for index, component in enumerate(components, start=1):
        # Assembly instance paths can exceed filesystem filename limits. The
        # evidence record retains the full identity; filenames stay bounded.
        target = geometry_dir / f"{index:04d}_{digest_json(component)[:24]}.stl"
        info = backend.export_component_mesh(component, str(target))
        stats = read_stl(target)
        exported.append(
            {
                "component": component,
                "path": target.relative_to(geometry_dir.parent).as_posix(),
                "triangles": stats.triangles,
                "volume_m3": stats.volume,
                "area_m2": stats.area,
                "degenerate_triangles": stats.degenerate,
                "boundary_edges": stats.boundary_edges,
                "used_api": info.get("used_api"),
                "sha256": sha256_file(target),
            }
        )
    return exported


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
        stage = "readings"
        scene, raw = _capture_readings(backend, cfg, closure, staging / "source")
        write_json(staging / "raw" / "scene_raw.json", raw)
        write_json(staging / "raw" / "document_state.json", state)
        write_json(staging / "raw" / "dependency_closure.json", closure)
        write_json(staging / "raw" / "coordinate_systems.json", raw["coordinate_systems"])
        write_json(staging / "raw" / "mass_properties.json", raw["mass_properties"])
        mass_closure = _mass_closure(backend, cfg, scene)
        if mass_closure is not None:
            write_json(staging / "raw" / "mass_closure.json", mass_closure)
        # Declared author decisions are recorded as declared input, never as CAD
        # readings: freeze keeps the snapshot raw and load_scene applies them.
        write_json(
            staging / "raw" / "declared_masses.json",
            {
                "schema_version": "description-pipeline.solidworks-declared-masses/v1",
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
        # The readings came from the copy; both the reading session and the working tree
        # still have to hold what they held when the copy was made.
        backend.verify_sources_unchanged()
        originals = _verify_originals_unchanged(backend, closure)
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

        scene_payload = build_scene(cfg, scene, geometry_entries)
        write_json(staging / "scene.json", scene_payload)

        source_inputs = _source_inputs(cfg, closure)
        environment = capture_environment(backend, worker_version)
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
        write_json(staging / "evidence" / "environment.json", environment)

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
        from .remote import freeze_via_worker

        return freeze_via_worker(dict(config), Path(destination))
    if backend is None:
        from .native import SolidWorksBackend

        backend = SolidWorksBackend()
    session = getattr(backend, "session", None)
    with session() if callable(session) else nullcontext():
        return _freeze_local(config, destination, backend=backend, worker_version=worker_version)
