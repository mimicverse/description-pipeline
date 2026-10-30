"""Read and seal one prepared SolidWorks source.

Freeze and Discover own the first dependency closure and prepared package.
Capture consumes those sealed bytes, opens one owned read-only session, records
the native facts once, releases CAD, and writes only the raw observation,
triangle streams, dependency closure, and runtime receipts.

Nothing here derives values from a final URDF or from hand-written XML: every
CAD value in ``raw/`` comes from the prepared native session.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import contextlib
from pathlib import Path, PurePosixPath
from typing import Any
from collections.abc import Callable, Mapping

from .errors import BridgeError, ConfigError
from .evidence import capture_environment
from .jsonio import digest_json, sha256_file, write_json

# The native adapter binds exported bytes; full mesh analysis runs on Linux.
from ...io import _filesystem_path, _physical_path, confined, inventory
from ...schemas import NATIVE_CAPTURE_FACTS

#: Freeze/capture records use the current solidworks-to-urdf contract.
FREEZE_SCHEMA = "solidworks-to-urdf.freeze/v1"
SOURCE_KIND = "solidworks"
DEFAULT_ALLOWED_SUFFIXES = (".sldasm", ".sldprt")
# Every key the adapter understands; anything else is a typo or an unimplemented
# feature and must fail instead of being ignored.
SOURCE_KEYS = {
    "provider",
    "assembly",
    "configuration",
    "geometry",
    "coordinate_systems",
    "groups",
    "axis_requests",
    "robot_name",
    "geometry_exclusions",
    "expected_mass_kg",
    "expected_extent_m",
    "evidence_class",
}
EVIDENCE_CLASSES = ("cad", "fixture", "imported")
FRAME_KEYS = {"xyz", "rpy", "coordinate_system"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _shared_manifest_writer() -> Callable[..., dict[str, Any]]:
    """The pipeline-wide snapshot helper; owned by the integration task."""

    from ..snapshot import write_manifest

    return write_manifest


def _require(condition: bool, message: str, detail: object | None = None) -> None:
    if not condition:
        raise ConfigError(message, detail)


def _capture_note(activity, action: str, **fields) -> None:
    """One best-effort capture activity note; the emitter is inert without a callback."""

    if activity is not None:
        activity.emit(action, phase="capture", **fields)


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


def validate_source_config(config: object) -> dict[str, Any]:
    """Validate the ``source`` mapping before anything touches SolidWorks."""

    _require(isinstance(config, dict), "source config must be a mapping")
    assert isinstance(config, dict)
    unknown = sorted(set(config) - SOURCE_KEYS)
    _require(not unknown, f"source config has unknown keys: {', '.join(unknown)}", unknown)
    provider = str(config.get("provider") or SOURCE_KIND)
    _require(provider == SOURCE_KIND, f"this adapter only serves provider=solidworks, got {provider!r}")
    assembly = config.get("assembly")
    _require(
        isinstance(assembly, str) and assembly.strip() != "",
        "source.assembly must be the absolute path of the top-level SLDASM",
    )
    configuration = config.get("configuration")
    _require(
        isinstance(configuration, str) and configuration.strip() != "",
        "source.configuration must be explicit; the adapter never guesses the active configuration",
    )
    geometry = config.get("geometry") or {}
    _require(isinstance(geometry, dict), "source.geometry must be a mapping")
    groups = config.get("groups")
    _require(isinstance(groups, list) and groups, "source.groups must be the native link-group selections")
    axis_requests = config.get("axis_requests")
    _require(isinstance(axis_requests, list), "source.axis_requests must be the native axis requests")
    _require(geometry.get("enabled", True) is True, "source.geometry capture is required")
    _require(
        geometry.get("format", "stl_binary") == "stl_binary",
        "source.geometry.format must be stl_binary",
    )
    coordinate_systems = config.get("coordinate_systems") or []
    _require(isinstance(coordinate_systems, list), "source.coordinate_systems must be a list of names")
    # Geometry exclusions already charged in the physics model must be objects with
    # component, reason, and structured evidence; bare strings cannot skip geometry.
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
        "geometry": dict(geometry),
        "coordinate_systems": [str(name) for name in coordinate_systems],
        "groups": [dict(item) for item in (config.get("groups") or [])],
        "axis_requests": [dict(item) for item in (config.get("axis_requests") or [])],
        "robot_name": str(config.get("robot_name") or "robot"),
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
        not isinstance(state.get("active_configuration"), str)
        or not state["active_configuration"].strip()
    ):
        raise BridgeError(
            "cad_document_state_unreadable",
            "the active configuration must be known",
            {"document": cfg["assembly"], "state": state},
            exit_code=3,
        )
    return state


def _native_lock_files(source_dir: Path) -> set[Path]:
    """SolidWorks session locks are transient siblings of native documents."""
    physical_root = Path(_physical_path(source_dir))
    return {
        source_dir / path.relative_to(physical_root)
        for path in physical_root.rglob("~$*")
        if path.is_file()
        and path.suffix.lower() in DEFAULT_ALLOWED_SUFFIXES
        and path.with_name(path.name[2:]).is_file()
    }


def _copy_inventory(source_dir: Path) -> dict[str, str]:
    locks = tuple(path.relative_to(source_dir).as_posix() for path in _native_lock_files(source_dir))
    return {f"source/{path}": digest for path, digest in inventory(source_dir, exclude=locks).items()}


def _normalized_posix_path(value: object) -> str | None:
    """Return one bounded relative snapshot path for exact matching."""

    if not isinstance(value, str) or not value:
        return None
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix().casefold()


def _bind_source_scope_origins(
    source_scopes: dict[str, dict[str, Any]],
    closure: dict[str, Any],
    source_root: Path,
) -> dict[str, dict[str, Any]]:
    """Bind each occurrence scope to one exact collected copy and original file."""

    mapping_by_copy: dict[str, list[dict[str, Any]]] = {}
    for row in closure.get("mapping") or []:
        if not isinstance(row, dict):
            raise BridgeError(
                "cad_source_scope_mapping",
                "a dependency mapping row is malformed",
                exit_code=3,
            )
        copy_path = _normalized_posix_path(row.get("copy"))
        if copy_path is None:
            raise BridgeError(
                "cad_source_scope_mapping",
                "a dependency mapping copy path is malformed",
                {"mapping": row},
                exit_code=3,
            )
        mapping_by_copy.setdefault(copy_path, []).append(row)
    copy_files = {
        path: checksum
        for value, checksum in (closure.get("copy_files") or {}).items()
        if (path := _normalized_posix_path(value)) is not None
    }
    original_files = {
        os.path.normcase(os.path.abspath(str(value))): checksum
        for value, checksum in (closure.get("original_files") or {}).items()
    }
    root = Path(source_root).resolve()
    bound: dict[str, dict[str, Any]] = {}
    for component, raw_scope in source_scopes.items():
        scope = dict(raw_scope)
        if scope.get("source_document_identity_kind") != "file_sha256":
            bound[component] = scope
            continue
        try:
            relative = Path(scope["source_document_path"]).resolve().relative_to(
                root
            )
        except (KeyError, OSError, ValueError) as error:
            raise BridgeError(
                "cad_source_scope_mapping",
                "a source scope is outside the collected source root",
                {
                    "component": component,
                    "source_document_path": scope.get(
                        "source_document_path"
                    ),
                    "source_root": str(root),
                },
                exit_code=3,
            ) from error
        copy_path = (PurePosixPath("source") / relative.as_posix()).as_posix()
        normalized_copy = _normalized_posix_path(copy_path)
        mapping_rows = (
            mapping_by_copy.get(normalized_copy, [])
            if normalized_copy is not None
            else []
        )
        mapping_source = (
            mapping_rows[0].get("source")
            if len(mapping_rows) == 1
            else None
        )
        original_sha256 = (
            original_files.get(
                os.path.normcase(os.path.abspath(str(mapping_source)))
            )
            if isinstance(mapping_source, str)
            else None
        )
        if (
            scope.get("source_copy_path") not in (None, copy_path)
            or len(mapping_rows) != 1
            or not isinstance(original_sha256, str)
            or SHA256_RE.fullmatch(original_sha256) is None
            or mapping_rows[0].get("copy_sha256")
            != scope.get("source_copy_sha256")
            or copy_files.get(normalized_copy) != scope.get("source_copy_sha256")
        ):
            raise BridgeError(
                "cad_source_scope_mapping",
                "a source scope lacks one exact dependency mapping",
                {
                    "component": component,
                    "source_copy_path": copy_path,
                    "scope_copy_sha256": scope.get("source_copy_sha256"),
                    "mapping_rows": mapping_rows,
                    "mapped_copy_sha256": (
                        copy_files.get(normalized_copy)
                        if normalized_copy is not None
                        else None
                    ),
                },
                exit_code=3,
            )
        scope["source_copy_path"] = copy_path
        scope["source_document_sha256"] = original_sha256
        scope["source_document_identity_sha256"] = original_sha256
        bound[component] = scope
    return bound


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


def _sealed_capture_files(
    package: Path,
    cfg: dict[str, Any],
    native_files: Mapping[str, str],
) -> tuple[str, dict[str, str]]:
    """Return the prepared main's relative path and sealed CAD-file digests."""

    package = Path(package).resolve()
    if not isinstance(native_files, Mapping) or not native_files:
        raise BridgeError(
            "sealed_capture_incomplete",
            "the sealed prepared source carries no CAD file inventory",
            exit_code=3,
        )
    files: dict[str, str] = {}
    for relative, checksum in native_files.items():
        if (
            not isinstance(relative, str)
            or not relative
            or not isinstance(checksum, str)
            or SHA256_RE.fullmatch(checksum) is None
        ):
            raise BridgeError(
                "sealed_capture_incomplete",
                "the sealed prepared source carries an invalid CAD file entry",
                {"path": relative},
                exit_code=3,
            )
        files[relative] = checksum
    assembly = Path(str(cfg["assembly"])).resolve()
    try:
        main_relative = assembly.relative_to(package).as_posix()
    except ValueError as error:
        raise BridgeError(
            "sealed_capture_scope",
            "the prepared main assembly is outside its sealed package",
            {"assembly": str(assembly), "package": str(package)},
            exit_code=3,
        ) from error
    if main_relative not in files:
        raise BridgeError(
            "sealed_capture_incomplete",
            "the sealed prepared source does not inventory its main assembly",
            {"main_assembly": main_relative},
            exit_code=3,
        )
    return main_relative, files


def _copy_sealed_capture_source(
    prepared_root: Path,
    source_root: Path,
    files: Mapping[str, str],
) -> dict[str, str]:
    """Copy the immutable prepared CAD files without collecting or rewriting."""

    prepared_root = Path(_physical_path(prepared_root)).resolve()
    source_root = Path(_physical_path(source_root)).resolve()
    copied: dict[str, str] = {}
    for relative, expected in sorted(files.items()):
        source = confined(prepared_root, relative)
        if source.is_symlink() or not source.is_file():
            raise BridgeError(
                "sealed_capture_incomplete",
                "a sealed prepared CAD file is absent or linked",
                {"path": relative},
                exit_code=3,
            )
        target = confined(source_root, relative, exists=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        actual = sha256_file(target)
        if actual != expected:
            raise BridgeError(
                "sealed_capture_changed",
                "a sealed prepared CAD file changed before capture",
                {"path": relative, "expected": expected, "actual": actual},
                exit_code=3,
            )
        copied[relative] = actual
    return copied


def _sealed_dependency_closure(
    cfg: dict[str, Any],
    *,
    package: Path,
    source_root: Path,
    main_relative: str,
    copied: Mapping[str, str],
) -> dict[str, Any]:
    """Project the frozen inventory into the evidence closure shape.

    This is a byte mapping, not dependency collection: the prepared package was
    already closed and verified by Freeze and Discover.  Capture copies those
    exact bytes once and never reopens author references or rewrites a CAD
    document.
    """

    package = Path(package).resolve()
    source_root = Path(source_root).resolve()
    original_files: dict[str, str] = {}
    copy_files: dict[str, str] = {}
    mapping: list[dict[str, Any]] = []
    for relative, checksum in sorted(copied.items()):
        source = str(package / relative)
        copy_relative = f"source/{relative}"
        original_files[source] = checksum
        copy_files[copy_relative] = checksum
        mapping.append(
            {
                "source": source,
                "copy": copy_relative,
                "instance": "top_level" if relative == main_relative else relative,
                "copy_sha256": checksum,
                "source_sha256": checksum,
            }
        )
    return {
        "schema_version": "solidworks-to-urdf.native-closure/v1",
        "method": "sealed_prepared_inventory",
        "source_dir": str(source_root),
        "top_level": str(source_root / main_relative),
        "configuration": str(cfg["configuration"]),
        "original_files": original_files,
        "copy_files": copy_files,
        "mapping": mapping,
    }


def _verify_sealed_capture_copy(closure: dict[str, Any]) -> dict[str, Any]:
    """Rehash the read-only prepared copy after CAD release."""

    source_dir = Path(str(closure.get("source_dir") or ""))
    actual = _copy_inventory(source_dir)
    expected = dict(closure.get("copy_files") or {})
    if actual != expected:
        changed = sorted(
            path
            for path in set(actual) | set(expected)
            if actual.get(path) != expected.get(path)
        )
        raise BridgeError(
            "cad_copy_changed",
            "the prepared CAD bytes changed during capture",
            {"changed": changed[:20], "count": len(changed)},
            exit_code=3,
        )
    return {
        "files_checked": len(actual),
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
    if isinstance(value, float) and not math.isfinite(value):
        return {"native_non_finite_float": value.hex()}
    if key and any(marker in key.lower() for marker in _CREDENTIAL_MARKERS):
        return "<redacted>" if value not in (None, "") else value
    if isinstance(value, str) and "://" in value:
        # credentials in the authority and tokens in the query string
        return re.sub(r"//[^/@]*@", "//", value).split("?", 1)[0]
    return value


def _retain_failure(
    staging: Path, destination: Path, error: BaseException, stage: str, request: dict[str, Any],
    observations: dict[str, Any] | None = None,
) -> str | None:
    """Keep a failed attempt's staging tree instead of deleting the evidence.

    The staging tree holds the prepared CAD bytes and raw readings, so
    removing it on failure would destroy the only
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
        with contextlib.suppress(OSError, TypeError, ValueError):
            write_json(candidate / "failure.json", {**payload, "partial": str(moved)})
        if observations:
            with contextlib.suppress(OSError, TypeError, ValueError):
                write_json(candidate / "native_observations.json", _redact(observations))
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
                if module.endswith(".native") and frame.f_code.co_name in (
                    "_indexed_property",
                    "_property",
                    "_method",
                ):
                    member = frame.f_locals.get("name")
                    if isinstance(member, str):
                        site["member"] = member
                frames.append(site)
            trace = trace.tb_next
        record = {"type": type(error).__name__, "message": str(error), "frames": frames}
        if getattr(error, "__notes__", None):
            record["notes"] = list(error.__notes__)
        hresult = getattr(error, "hresult", None)
        if isinstance(hresult, int):
            record["hresult"] = hresult
        chain.append(record)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return chain


def _capture_axis_references(backend: Any, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Read raw facts for each structured joint axis reference.

    Axis line projection and numeric validation run after CAD release.
    Unreadable references remain failures rather than silently skipped rows.
    """

    reader = getattr(backend, "capture_axis_reference", None)
    captured: list[dict[str, Any]] = []
    for joint in cfg.get("axis_requests") or []:
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
        try:
            captured.append({"joint": joint_name, **reader(reference)})
        except Exception as error:
            error.partial_axis_facts = captured
            raise
    return captured


def _prepared_document_state(backend: Any, document: str, configuration: str) -> dict[str, Any]:
    """Observe the one prepared session without making rebuild state a gate."""

    state = _document_state(backend, {"assembly": document})
    if (
        not _same_document(str(state.get("path") or ""), document)
        or state.get("read_only") is not True
        or state.get("active_configuration") != configuration
    ):
        raise BridgeError(
            "cad_prepared_capture_state",
            "the prepared capture document is not the requested read-only configuration",
            {
                "document": document,
                "configuration": configuration,
                "state": state,
            },
            exit_code=3,
        )
    return state


def _capture_prepared_local(
    config: dict[str, Any],
    destination: Path,
    *,
    package: Path,
    prepared_root: Path,
    native_files: Mapping[str, str],
    backend: Any,
    worker_version: str,
    expected_session_sources: Mapping[str, str] | None = None,
    on_activity=None,
) -> dict[str, Any]:
    """Capture one already sealed prepared assembly in a single owned session."""

    destination = Path(destination)
    existing_empty = _prepare_destination(destination)
    destination = destination.resolve()
    cfg = validate_source_config(config)
    package = Path(package).resolve()
    prepared_root = Path(prepared_root).resolve()
    if not Path(_physical_path(prepared_root)).is_dir():
        raise BridgeError(
            "sealed_capture_incomplete",
            "the archived prepared CAD source is unavailable",
            {"prepared_root": str(prepared_root)},
            exit_code=3,
        )
    staging = destination.with_name(destination.name + ".partial")
    if staging.exists():
        shutil.rmtree(_physical_path(staging))
    staging.mkdir(parents=True)
    source_root = staging / "source"
    request = {
        "assembly": cfg["assembly"],
        "configuration": cfg["configuration"],
        "coordinate_systems": cfg["coordinate_systems"],
        "geometry": cfg["geometry"],
        "bodies": len(cfg.get("groups") or []),
        "joints": len(cfg.get("axis_requests") or []),
    }

    from .native import ACTIVITY_KEEPALIVE_SECONDS, _ActivityEmitter

    activity = _ActivityEmitter(on_activity, keepalive_seconds=ACTIVITY_KEEPALIVE_SECONDS)
    activity.start_keepalive()
    _capture_note(activity, "start", force=True)
    stage = "prepared_source"
    released = False
    raw_facts = None
    raw_geometry: dict[str, Any] = {}
    axis_references: list[dict[str, Any]] = []
    try:
        main_relative, sealed_files = _sealed_capture_files(
            package,
            cfg,
            native_files,
        )
        _capture_note(activity, "prepared_source")
        copied = _copy_sealed_capture_source(prepared_root, source_root, sealed_files)
        closure = _sealed_dependency_closure(
            cfg,
            package=package,
            source_root=source_root,
            main_relative=main_relative,
            copied=copied,
        )
        document = str(closure["top_level"])
        write_json(staging / "raw" / "dependency_closure.json", closure)

        prepare_capture = getattr(backend, "prepare_prepared_capture", None)
        collect_scene = getattr(backend, "collect_prepared_scene", None)
        capture_geometry = getattr(backend, "capture_prepared_geometry", None)
        opener = getattr(backend, "open_document", None)
        if (
            not callable(prepare_capture)
            or not callable(collect_scene)
            or not callable(capture_geometry)
            or not callable(opener)
        ):
            raise BridgeError(
                "cad_prepared_capture_unavailable",
                "the backend does not provide the prepared Capture seam",
                exit_code=3,
            )

        stage = "open_prepared"
        _capture_note(activity, "open_prepared")
        prepared_policy = prepare_capture(
            str(source_root),
            document,
            cfg["configuration"],
        )
        if not isinstance(prepared_policy, dict):
            raise BridgeError(
                "cad_prepared_capture_policy",
                "the backend returned no prepared capture policy evidence",
                {"returned": type(prepared_policy).__name__},
                exit_code=3,
            )
        opened = opener(document)
        if (
            not isinstance(opened, dict)
            or not _same_document(str(opened.get("path") or ""), document)
            or opened.get("read_only") is not True
        ):
            raise BridgeError(
                "cad_prepared_capture_open",
                "the prepared top assembly did not open read-only in the owned capture session",
                {"document": document, "opened": opened},
                exit_code=3,
            )
        state_before = _prepared_document_state(
            backend,
            document,
            str(cfg["configuration"]),
        )

        stage = "environment"
        environment = capture_environment(backend, worker_version)
        environment["prepared_capture"] = {
            "policy": prepared_policy,
            "opened": opened,
        }

        stage = "readings"
        _capture_note(activity, "readings")
        raw_facts = collect_scene(
            document,
            cfg["coordinate_systems"],
            groups=cfg["groups"],
            expected_session_sources=expected_session_sources or {},
            native_files=native_files,
            on_activity=activity.publish,
            require_material=True,
        )

        stage = "axis_references"
        axis_references = _capture_axis_references(backend, cfg)

        stage = "geometry_facts"
        _capture_note(activity, "geometry_facts")
        source_scopes_raw = (
            raw_facts.get("source_scopes")
            if isinstance(raw_facts, dict)
            else None
        )
        raw_geometry = capture_geometry(
            source_scopes_raw,
            on_activity=activity.publish,
        )

        stage = "document_state_after_reads"
        state_after = _prepared_document_state(
            backend,
            document,
            str(cfg["configuration"]),
        )

        stage = "release"
        _capture_note(activity, "release")
        release = getattr(backend, "release", None)
        if not callable(release):
            raise BridgeError(
                "cad_release_unavailable",
                "the prepared capture backend cannot release its owned session",
                exit_code=3,
            )
        release()
        released = True

        stage = "raw_observation"
        _capture_note(activity, "raw_observation")
        source_scopes = _bind_source_scope_origins(
            raw_facts["source_scopes"],
            closure,
            source_root,
        )
        from .capture_geometry import seal_native_geometry
        from .capture_projection import build_native_capture_observation

        geometry_raw = seal_native_geometry(
            raw_geometry,
            source_scopes,
            staging,
        )

        for lock in _native_lock_files(source_root):
            Path(_filesystem_path(lock)).unlink()
        originals = _verify_sealed_capture_copy(closure)
        source_inputs = _source_inputs(cfg, closure)
        shutil.rmtree(_physical_path(source_root))
        raw_facts = {
            **raw_facts,
            "source_scopes": source_scopes,
        }
        observation = build_native_capture_observation(
            facts=raw_facts,
            geometry_raw=geometry_raw,
            axis_references_raw=axis_references,
            document_state={
                "before": state_before,
                "after": state_after,
            },
            input_stability={
                "originals_unchanged": originals,
                "source_hashes": {
                    name: {"sha256": checksum}
                    for name, checksum in sorted(source_inputs.items())
                },
            },
        )
        write_json(
            staging / NATIVE_CAPTURE_FACTS,
            observation,
        )
        write_json(staging / "native-runtime.json", environment)

        stage = "seal"
        _capture_note(activity, "seal")
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
                "mode": "sealed_prepared_single_read_only_pass",
            },
        }

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
            destination.rmdir()
        os.replace(staging, destination)
        return manifest
    except BaseException as error:
        if not released:
            release = getattr(backend, "release", None)
            if callable(release):
                try:
                    release()
                except Exception as cleanup_error:
                    error.add_note(f"CAD cleanup also failed: {cleanup_error}")
        observations = {
            "scene": raw_facts if raw_facts is not None else getattr(error, "partial_native_facts", None),
            "geometry": raw_geometry,
            "axis_references": axis_references or getattr(error, "partial_axis_facts", []),
        }
        with contextlib.suppress(Exception):
            _retain_failure(staging, destination, error, stage, request, observations)
        raise
    finally:
        activity.stop_keepalive()

def capture_prepared(
    config: dict[str, Any],
    destination: Path,
    *,
    package: Path,
    prepared_root: Path,
    native_files: Mapping[str, str],
    backend: Any,
    worker_version: str,
    expected_session_sources: Mapping[str, str] | None = None,
    on_activity=None,
    resource_settlement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture one sealed prepared package through the single production path."""

    with backend.session(resource_settlement=resource_settlement):
        return _capture_prepared_local(
            config,
            destination,
            package=package,
            prepared_root=prepared_root,
            native_files=native_files,
            backend=backend,
            worker_version=worker_version,
            expected_session_sources=expected_session_sources,
            on_activity=on_activity,
        )
