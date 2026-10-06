"""Frozen inputs, deterministic construction and content-bound qualification."""

from __future__ import annotations

import copy
import importlib
import importlib.metadata
import math
import os
import platform
import shutil
import subprocess
import tempfile
import re
from pathlib import Path
from packaging.requirements import Requirement

from .. import __version__
from .. import pipeline
from ..backends import generate
from ..io import PipelineError, confined, digest, file_digest, inventory, read_data, write_json
from ..model import Robot, validate_scene_schema
from ..model.contact import validate_contact
from ..sources.snapshot import load_scene, verify_snapshot
from ..verification import environment, inspect, result
from ..verification import poses
from .publication import JOURNAL, publish
from . import cache

PROFILE = {
    "schema_version": "description.profile/v1",
    "purpose": "kinematics",
    "root_mode": "fixed",
    "seed": 0,
    "random_poses": 12,
    "steps": 100,
    "timestep": 0.002,
    "gravity": [0, 0, -9.81],
    "ground": False,
    "contact": None,
    "position_atol": 1e-7,
    "rotation_atol": 1e-7,
    "inertia_atol": 1e-10,
    "inertia_rtol": 1e-6,
    "uniform_density_rtol": 1e-3,
    "uniform_density_com_atol_m": 1e-6,
    "dynamics_atol": 1e-8,
    "dynamics_rtol": 1e-6,
    "penetration_m": 0.001,
    "validation_poses": None,
    "require_native_source": False,
    "acceptance_suites": [],
    "consumer_environment": {},
}

#: The robot's object categories.  The model, its diff and ``summary.robot_objects`` all count
#: exactly these, so the CLI can name them without carrying a second copy of the list.
OBJECT_FIELDS = ("links", "joints", "frames", "actuators", "sensors")
INTERFACE_FIELDS = {"frames", "actuators", "sensors", "control", "contact_excludes", "mechanical_drives"}


def profile_for(root: Path, name: str) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
        raise PipelineError("Invalid profile identifier")
    if not (root / "config/robot.yaml").is_file():
        # Running a workspace command in the wrong directory is the first thing a new operator does,
        # and "Expected one explicit profile …" sends them looking for a profile file in a directory
        # that is not a workspace at all.  Name the missing contract, and the command that writes it.
        raise PipelineError(
            f"Not a model workspace: no config/robot.yaml under {root}; "
            "run description model init to create one, or pass the workspace as --root"
        )
    candidates = [root / "config/profiles" / (name + suffix) for suffix in (".json", ".yaml")]
    found = [path for path in candidates if path.is_file()]
    if len(found) != 1:
        raise PipelineError(f"Expected one explicit profile config/profiles/{name}.json or .yaml")
    value = read_data(confined(root, found[0].relative_to(root).as_posix()))
    if not isinstance(value, dict) or set(value) - set(PROFILE):
        raise PipelineError("Unknown profile fields")
    profile = {**PROFILE, **value}
    if profile["contact"] is not None:
        validate_contact(profile["contact"])
    poses.validate_path(profile["validation_poses"])
    if profile["schema_version"] != PROFILE["schema_version"] or profile["purpose"] not in {
        "kinematics",
        "simulation",
        "training",
        "hardware",
    }:
        raise PipelineError("Unknown profile schema or purpose")
    if profile["root_mode"] not in {"fixed", "floating"}:
        raise PipelineError("Unsupported root mode")
    for field in (
        "position_atol",
        "rotation_atol",
        "inertia_atol",
        "inertia_rtol",
        "uniform_density_rtol",
        "uniform_density_com_atol_m",
        "dynamics_atol",
        "dynamics_rtol",
        "penetration_m",
        "timestep",
    ):
        if type(profile[field]) not in (int, float) or not math.isfinite(profile[field]) or not 0 < profile[field] < 1:
            raise PipelineError(f"Invalid profile tolerance: {field}")
    for field in ("random_poses", "steps"):
        if type(profile[field]) is not int or not 1 <= profile[field] <= 10000:
            raise PipelineError(f"Invalid sample count: {field}")
    if type(profile["seed"]) is not int or profile["seed"] < 0:
        raise PipelineError("Seed must be a nonnegative integer")
    for name in ("ground", "require_native_source"):
        if type(profile[name]) is not bool:
            raise PipelineError(f"Profile {name} must be boolean")
    gravity = profile["gravity"]
    if (
        not isinstance(gravity, list)
        or len(gravity) != 3
        or any(type(value) not in (int, float) or not math.isfinite(value) for value in gravity)
    ):
        raise PipelineError("Gravity must be three finite SI numbers")
    suites = profile["acceptance_suites"]
    if not isinstance(suites, list) or not all(isinstance(name, str) and name for name in suites):
        raise PipelineError("Acceptance suites must be nonempty identifiers")
    if len(suites) != len(set(suites)):
        raise PipelineError("Acceptance suite identifiers must be unique")
    declared_environment = profile["consumer_environment"]
    if not isinstance(declared_environment, dict) or any(
        not isinstance(key, str) or not key or not isinstance(value, str) or not value
        for key, value in declared_environment.items()
    ):
        raise PipelineError("Consumer environment must map software/component names to explicit versions")
    return profile


def definition(root: Path) -> dict:
    value = read_data(confined(root, "config/robot.yaml"))
    allowed = {"schema_version", "hardware_id", "source", "overrides", "robot", "interfaces", "pipeline_id"}
    if (
        not isinstance(value, dict)
        or set(value) - allowed
        or value.get("schema_version") != "description.definition/v1"
    ):
        raise PipelineError("Unsupported robot definition or unknown fields")
    if not value.get("hardware_id") or not isinstance(value.get("source"), dict):
        raise PipelineError("Definition requires hardware_id and source")
    if not isinstance(value.get("overrides", []), list):
        raise PipelineError("Overrides must be a list")
    interfaces = value.get("interfaces", {})
    if not isinstance(interfaces, dict) or set(interfaces) - INTERFACE_FIELDS:
        raise PipelineError("Unsupported author interface fields")
    declared = value.get("pipeline_id")
    if declared is not None:
        pipeline.get(declared)  # unknown ids name the published pipeline ids
        kind = pipeline.effective_source_kind(value.get("source"), value.get("robot"))
        if kind in pipeline.NATIVE_KINDS:
            # A native authoring entry point is unambiguous, so reject a mismatch before any capture.
            # Wrapper entry points (fixture/snapshot/imported) can carry any provider's data, and
            # freeze re-checks the declaration against the kind the snapshot actually carries.
            pipeline.resolve_identity(declared, source=value.get("source"), robot=value.get("robot"))
    return value


def tool_identity() -> dict:
    package = Path(__file__).resolve().parents[1]
    files = {
        p.relative_to(package).as_posix(): file_digest(p)
        for p in sorted(package.rglob("*"))
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.name != "tool-release.json"
        and p.suffix not in {".pyc", ".pyo"}
    }
    commit = None
    repository = package.parents[1]
    dirty = True
    if (repository / ".git").exists():
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True, encoding="utf-8", check=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--", "src", "pyproject.toml"],
                cwd=repository,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            ).stdout.strip()
        )
    elif (package / "tool-release.json").is_file():
        release = read_data(package / "tool-release.json")
        if release["package_digest"] != digest(files) or release["version"] != __version__:
            raise PipelineError("Installed tool release metadata does not match its code")
        commit = release["source_commit"]
        dirty = release.get("development", True)
    return {
        "schema_version": "description.toolchain/v1",
        "version": __version__,
        "package_digest": digest(files),
        "source_commit": commit,
        "development": dirty,
        "python": platform.python_version(),
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "implementation": platform.python_implementation(),
        },
        "dependencies": runtime_dependencies(),
    }


def runtime_dependencies() -> dict:
    """Bind the complete installed runtime dependency closure, excluding unrelated developer tools."""
    pending: list[tuple[str, tuple[str, ...]]] = [
        (name, ()) for name in ("numpy", "PyYAML", "jsonschema", "mujoco", "packaging")
    ]
    if platform.system() == "Windows":
        pending.append(("pywin32", ()))
    visited = set()
    versions = {}
    while pending:
        name, extras = pending.pop()
        key = (name.lower().replace("_", "-"), extras)
        if key in visited:
            continue
        visited.add(key)
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            versions[key[0]] = "unavailable"
            continue
        versions[key[0]] = distribution.version
        for spec in distribution.requires or []:
            requirement = Requirement(spec)
            if requirement.marker is None or any(
                requirement.marker.evaluate({"extra": extra}) for extra in ("", *extras)
            ):
                pending.append((requirement.name, tuple(sorted(requirement.extras))))
    return dict(sorted(versions.items()))


def lock_toolchain(root: Path) -> dict:
    if not (root / "config/robot.yaml").is_file():
        raise PipelineError(f"Tool lock needs a model workspace; no config/robot.yaml under {root}")
    value = tool_identity()
    write_json(root / "config/toolchain.lock.json", value)
    return value


def _json_object(root: Path, relative: str, fix: str) -> dict:
    """Read a file that must hold a JSON object, naming the command that writes it again.

    A hand-edited or half-restored lock or bundle artifact used to reach ``.get`` on whatever JSON it
    held, so a list or a string raised AttributeError inside build and check instead of a diagnostic.
    """

    try:
        data = read_data(confined(root, relative))
    except PipelineError as error:
        raise PipelineError(f"{error}; {fix}") from None
    if not isinstance(data, dict):
        raise PipelineError(f"{relative} must contain a JSON object, found {type(data).__name__}; {fix}")
    return data


def verify_toolchain(root: Path) -> dict:
    locked = _json_object(
        root,
        "config/toolchain.lock.json",
        "restore the file from the model branch, or run `description tool lock --root .`",
    )
    current = tool_identity()
    for key in (
        "schema_version",
        "version",
        "package_digest",
        "source_commit",
        "development",
        "python",
        "platform",
        "dependencies",
    ):
        if locked.get(key) != current[key]:
            raise PipelineError(f"Toolchain differs from lock ({key}); explicit tool upgrade and rebuild required")
    return locked


def failure_record(error: Exception) -> dict:
    """The failure package a user is pointed at has to carry the reason, not only the headline.

    A worker refuses a capture with a code and a detail — ``document_not_open`` names the assembly
    the adapter's own CAD session could not find — and the CLI prints both.  The file written beside the captured
    tree used to say only "worker job ended in state failed", so the one artifact a user opens to
    understand a failure was the one place the reason was missing.
    """

    record: dict = {"error": type(error).__name__, "message": str(error)}
    for field in ("code", "detail"):
        value = getattr(error, field, None)
        if value is not None:
            record[field] = value
    return record


def freeze(root: Path) -> dict:
    config = definition(root)
    source = copy.deepcopy(config["source"])
    provider = source.get("provider")
    sources = root / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    capture = Path(tempfile.mkdtemp(prefix=".freeze-", dir=sources))
    # Keep adapter-owned partial directories inside the capture, out of author inputs.
    temporary = capture / "snapshot"
    temporary.mkdir()
    try:
        if provider in {"snapshot", "fixture", "imported"}:
            original = Path(source.get("path", ""))
            if not original.is_absolute():
                original = root / original
            manifest = verify_snapshot(original)
            if provider == "fixture" and manifest["evidence_class"] != "fixture":
                raise PipelineError("Fixture source must be explicitly marked fixture")
            shutil.copytree(original, temporary, dirs_exist_ok=True)
        elif provider in {"onshape", "solidworks"}:
            adapter = importlib.import_module(f"description_pipeline.sources.{provider}")
            adapter.freeze(source, temporary)
        else:
            raise PipelineError(f"Unsupported source provider: {provider}")
        manifest = verify_snapshot(temporary)
        validate_scene_schema(load_scene(temporary))
        fingerprint = digest(manifest)
        final = sources / "snapshots" / fingerprint
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            if digest(verify_snapshot(final)) != fingerprint:
                raise PipelineError("Existing snapshot cache is corrupt")
        else:
            os.replace(temporary, final)
        locked = {
            "schema_version": "description.source-lock/v1",
            "provider": manifest["kind"],
            "snapshot": final.relative_to(root).as_posix(),
            "manifest_digest": fingerprint,
            "source_config_digest": digest(config["source"]),
            "evidence_class": manifest["evidence_class"],
            "pipeline": pipeline.resolve_identity(
                config.get("pipeline_id"),
                source=config["source"],
                robot=config.get("robot"),
                frozen_kind=manifest["kind"],
                identity_provider=(
                    manifest.get("identity", {}).get("provider") if isinstance(manifest.get("identity"), dict) else None
                ),
            ),
        }
        locked["pipeline_id"] = locked["pipeline"]["id"]
        write_json(sources / "source.lock.json", locked)
        return locked
    except Exception as error:
        failed_root = root / "build/failed-source"
        failed_root.mkdir(parents=True, exist_ok=True)
        failed = Path(tempfile.mkdtemp(prefix="capture-", dir=failed_root))
        write_json(capture / "failure.json", failure_record(error))
        # ``mkdtemp`` creates the placeholder.  Windows cannot replace an
        # existing directory, so remove it before moving the captured tree;
        # otherwise the original error and its diagnostic are lost.
        failed.rmdir()
        os.replace(capture, failed)
        vars(error)["diagnostic_path"] = str(failed)
        raise
    finally:
        if capture.exists():
            shutil.rmtree(capture)


def inputs(root: Path) -> tuple[dict, dict, Path, dict]:
    config = definition(root)
    locked = _json_object(root, "sources/source.lock.json", "run `description source freeze --root .`")
    if locked.get("schema_version") != "description.source-lock/v1" or locked.get("source_config_digest") != digest(
        config["source"]
    ):
        raise PipelineError("Source definition changed; freeze it before building")
    path = confined(root, locked["snapshot"] + "/manifest.json").parent
    manifest = verify_snapshot(path)
    if locked.get("manifest_digest") != digest(manifest):
        raise PipelineError("Source lock does not match snapshot")
    pipeline.verify_lock(locked, config, manifest)
    return config, locked, path, manifest


def normalize(scene: dict, config: dict, root: Path, snapshot_root: Path | None = None) -> dict:
    data = copy.deepcopy(scene)
    provider = config["source"].get("provider")
    if provider in {"snapshot", "fixture", "imported"}:
        provider = config.get("robot", {}).get("provider", provider)
    if provider in {"onshape", "solidworks"}:
        adapter = importlib.import_module(f"description_pipeline.sources.{provider}")
        hook = getattr(adapter, "normalize_scene", None)
        if hook is not None:
            data = hook(data, config, snapshot_root)
    interfaces = copy.deepcopy(config.get("interfaces", {}))
    if not isinstance(interfaces, dict) or set(interfaces) - INTERFACE_FIELDS:
        raise PipelineError("Unsupported author interface fields")
    # Part lists are sets; normalise their order before resolving competing definitions.
    for drives in (data.get("mechanical_drives"), interfaces.get("mechanical_drives")):
        if not isinstance(drives, dict):
            continue
        for drive in drives.values():
            if isinstance(drive, dict) and drive.get("kind") == "active":
                for key in ("stator", "rotor"):
                    if isinstance(drive.get(key), list) and all(isinstance(v, str) for v in drive[key]):
                        drive[key] = sorted(drive[key])
    for field, value in interfaces.items():
        if data.get(field) and data[field] != value:
            raise PipelineError(
                f"Competing {field} definitions in source/robot and interfaces; choose one author entry"
            )
        data[field] = copy.deepcopy(value)
    if interfaces:
        data["provenance"]["author_interfaces"] = sorted(interfaces)
    applied = []
    selected: set[tuple[str, str, str]] = set()
    for override in config.get("overrides", []):
        if set(override) != {"kind", "id", "field", "value", "reason", "evidence"}:
            raise PipelineError("Override requires kind/id/field/value/reason/evidence")
        if override["kind"] not in {"links", "joints", "frames", "actuators", "sensors"} or not override["reason"]:
            raise PipelineError("Invalid override target or missing reason")
        identity = (override["kind"], override["id"], override["field"])
        for kind, object_id, field in selected:
            if identity[:2] == (kind, object_id) and (
                field == identity[2] or field.startswith(identity[2] + ".") or identity[2].startswith(field + ".")
            ):
                raise PipelineError("Overlapping author overrides: each field must have one effective definition")
        selected.add(identity)
        evidence = confined(root, override["evidence"])
        targets = [item for item in data[override["kind"]] if item["id"] == override["id"]]
        if len(targets) != 1:
            raise PipelineError("Override target no longer uniquely matches source")
        target = targets[0]
        parts = override["field"].split(".")
        if parts[0] in {"id", "name", "provenance"}:
            raise PipelineError("Identity/provenance cannot be overwritten by numeric override")
        for part in parts[:-1]:
            if part not in target or not isinstance(target[part], dict):
                raise PipelineError("Override path is absent in source")
            target = target[part]
        existed = parts[-1] in target
        previous = copy.deepcopy(target.get(parts[-1]))
        target[parts[-1]] = copy.deepcopy(override["value"])
        applied.append(
            {**override, "previous": previous, "previously_present": existed, "evidence_sha256": file_digest(evidence)}
        )
    data["provenance"] = {**data["provenance"], "applied_overrides": applied}
    Robot.from_dict(data)
    return data


def subject_files(root: Path) -> dict:
    names = {}
    for directory in ("config", "sources", "model", "urdf", "mjcf", "meshes"):
        path = root / directory
        if path.is_dir():
            names.update({f"{directory}/{name}": value for name, value in inventory(path).items()})
    # Author evidence is a build input; acceptance evidence refers back to this subject
    # and is bound separately by the delivery manifest to avoid a circular digest.
    for name, value in inventory(root / "docs").items():
        if name not in {"quality.json", "quality.md"} and not name.startswith("acceptance/"):
            names[f"docs/{name}"] = value
    return names


def _derivation(root: Path, source: Path, expected: dict, actual: dict) -> dict:
    reference = copy.deepcopy(expected)
    produced = copy.deepcopy(actual)
    input_schema = reference["schema_version"]
    output_schema = produced["schema_version"]
    reference["schema_version"] = "description.robot/v1"
    differences = []
    for key in ("links", "joints", "frames", "actuators", "sensors"):
        if [item["id"] for item in reference[key]] != [item["id"] for item in produced[key]]:
            return result("source.derivation", False, details=f"{key} identity/coverage mismatch")
    for original, link in zip(reference["links"], produced["links"], strict=True):
        for role in ("visuals", "collisions"):
            if len(original[role]) != len(link[role]):
                return result("source.derivation", False, details="Geometry count changed")
            for before, after in zip(original[role], link[role], strict=True):
                if before["kind"] == "mesh" and after["kind"] == "mesh":
                    if file_digest(confined(source, before["filename"])) != file_digest(
                        confined(root, after["filename"])
                    ):
                        differences.append(f"mesh:{link['name']}/{role}")
                    before["filename"] = after["filename"]
    if reference != produced:
        differences.append("canonical_semantics")
    return result(
        "source.derivation",
        not differences,
        details={
            "differences": differences,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "fields": field_changes(reference, produced),
        },
    )


def field_changes(before, after, path: str = "") -> list[dict]:
    if isinstance(before, dict) and isinstance(after, dict):
        return [
            item
            for key in sorted(before.keys() | after.keys())
            for item in field_changes(before.get(key), after.get(key), f"{path}/{key}")
        ]
    if (
        isinstance(before, list)
        and isinstance(after, list)
        and all(isinstance(item, dict) and "id" in item for item in before + after)
    ):
        return field_changes({item["id"]: item for item in before}, {item["id"]: item for item in after}, path)
    if before == after:
        return []
    value = {"path": path, "before": before, "after": after}
    if isinstance(before, (float, int)) and isinstance(after, (float, int)) and before != 0:
        value["relative_change"] = (after - before) / abs(before)
    return [value]


def assess(
    root: Path, profile_name: str, *, verify_manifest: bool = True, mechanical_reference: Path | None = None
) -> dict:
    if (root / JOURNAL).exists():
        raise PipelineError("Workspace publication is unfinished; run description recover before checking")
    profile = profile_for(root, profile_name)
    if mechanical_reference is not None:
        from ..verification.mechanics import reference_path

        if profile["purpose"] != "kinematics" or not profile["acceptance_suites"]:
            raise PipelineError("--mechanical-reference requires kinematics with a declared acceptance suite")
        mechanical_reference = reference_path(mechanical_reference, root)
    config, source_lock, source, source_manifest = inputs(root)
    tool = verify_toolchain(root)
    data = read_data(confined(root, "model/robot.json"))
    raw_scene = load_scene(source)
    expected = normalize(raw_scene, config, root, source)
    checks = [_derivation(root, source, expected, data)]
    pipeline_identity = pipeline.verify_lock(source_lock, config, source_manifest)
    checks.append(
        result(
            "source.pipeline",
            True,
            expected=[pipeline_identity["id"]],
            checked=[pipeline_identity["id"]],
            details={
                "pipeline": pipeline_identity,
                "legacy_lock": pipeline_identity["resolved_from"] == "legacy_lock",
            },
        )
    )
    provider = source_manifest["kind"]
    if provider == "onshape":
        identity = source_manifest["identity"]
        bindings = identity.get("request_bindings", {})
        verified_revision = (
            identity.get("revision_locked") is True
            and identity.get("revision_evidence", {}).get("locked") is True
            and bool(bindings.get("bindings"))
            and not bindings.get("unbound")
            and not bindings.get("unverified")
        )
        checks.append(
            result(
                "source.revision",
                verified_revision,
                details={"revision_evidence": identity.get("revision_evidence"), "capture": identity.get("capture")},
                status="not_applicable" if source_manifest["evidence_class"] == "fixture" else None,
            )
        )
    if provider in {"onshape", "solidworks"}:
        adapter = importlib.import_module(f"description_pipeline.sources.{provider}")
        independent = getattr(adapter, "verify_normalization", None)
        if independent is not None:
            # The raw oracle establishes the CAD baseline. Generic author overrides
            # then form a separate, evidence-bound derivation to the delivered model.
            baseline = normalize(raw_scene, {**config, "overrides": []}, root, source)
            checks.extend(independent(raw_scene, config, source, baseline))
            checks.append(
                result(
                    "source.author_decisions",
                    True,
                    details={
                        "oracle_scope": "CAD baseline before generic overrides",
                        "baseline_digest": digest(baseline),
                        "applied_overrides": expected["provenance"]["applied_overrides"],
                        "final_model_check": "source.derivation",
                    },
                )
            )
        elif source_manifest["evidence_class"] == "cad":
            checks.append(
                result(
                    "source.independent_verification",
                    False,
                    status="not_run",
                    details="Native source requires an independent normalization oracle",
                )
            )
    declared = raw_scene["provenance"].get("expected_entities")
    covered = [
        entity
        for link in expected["links"]
        if link["provenance"].get("kind") != "reference_frame"
        for entity in link["provenance"].get("source_entities", [])
    ]
    native_required = profile["require_native_source"] or profile["purpose"] == "hardware"
    exclusions = expected["provenance"].get("excluded_entities", [])
    excluded = [item["id"] for item in exclusions if item.get("id") and item.get("reason") and item.get("evidence")]
    coverage_ok = (
        declared is not None
        and set(covered + excluded) == set(declared)
        and len(covered + excluded) == len(set(covered + excluded))
        and len(excluded) == len(exclusions)
    )
    checks.append(
        result(
            "source.coverage",
            coverage_ok,
            expected=declared or [],
            checked=covered + excluded,
            details={"declared": declared is not None, "excluded": exclusions},
            status="not_applicable" if source_manifest["evidence_class"] != "cad" and declared is None else None,
        )
    )
    checks.append(
        result(
            "source.native",
            source_manifest["evidence_class"] == "cad",
            details={"evidence_class": source_manifest["evidence_class"]},
            status="not_applicable" if not native_required else None,
        )
    )
    if profile["purpose"] != "kinematics":
        movable = [item for item in data["joints"] if item["type"] != "fixed"]
        valid = [
            item["name"]
            for item in movable
            if item.get("limits", {}).get("effort", 0) > 0 and item.get("limits", {}).get("velocity", 0) > 0
        ]
        checks.append(
            result(
                "simulation.limits",
                len(valid) == len(movable),
                expected=[item["name"] for item in movable],
                checked=valid,
            )
        )
        collision_links = [link["name"] for link in data["links"] if link["collisions"]]
        physical_links = [link["name"] for link in data["links"] if link["inertial"] is not None]
        checks.append(result("simulation.collision_coverage", True, expected=physical_links, checked=collision_links))
    files = subject_files(root)
    checks.extend(inspect(root, profile))
    if profile["acceptance_suites"] or profile["purpose"] != "kinematics":
        from ..verification.acceptance import verify_acceptance

        checks.append(
            verify_acceptance(
                root,
                digest(files),
                profile,
                input_hashes=set(files.values()),
                mechanical_reference=mechanical_reference,
            )
        )
    if verify_manifest:
        manifest = _json_object(root, "manifest.json", "run `description build --root .`")
        checks.append(
            result(
                "bundle.identity",
                manifest.get("schema_version") == "description.bundle/v1"
                and manifest.get("subject") == digest(files)
                and manifest.get("files") == files
                and manifest.get("profile") == profile_name
                and manifest.get("pipeline_id") == pipeline_identity["id"],
                details={"expected": manifest.get("subject"), "actual": digest(files)},
            )
        )
        checks.append(
            result("bundle.provenance", manifest.get("source") == source_lock and manifest.get("toolchain") == tool)
        )
        reports = manifest.get("reports", {})
        if not isinstance(reports, dict):
            raise PipelineError(
                f"manifest.json must list its reports as a JSON object, found {type(reports).__name__}; "
                "run `description build --root .`"
            )
        required_reports = {"docs/quality.json", "docs/quality.md"}
        checks.append(result("bundle.report_coverage", True, expected=required_reports, checked=reports))
        for name, value in reports.items():
            checks.append(result("bundle.report", file_digest(confined(root, name)) == value, details={"file": name}))
        committed_report = _json_object(root, "docs/quality.json", "run `description build --root .`")
        checks.append(
            result(
                "bundle.report_binding",
                committed_report.get("subject") == digest(files)
                and committed_report.get("profile_digest") == digest(profile)
                and committed_report.get("pipeline_id") == pipeline_identity["id"]
                and committed_report.get("passed") is True,
            )
        )
        acceptance_files = {
            f"docs/acceptance/{name}": value for name, value in inventory(root / "docs/acceptance").items()
        }
        checks.append(result("bundle.acceptance_binding", manifest.get("acceptance", {}) == acceptance_files))
    passed = all(check["status"] in {"passed", "not_applicable"} for check in checks)
    report = {
        "schema_version": "description.quality/v1",
        "hardware_id": config["hardware_id"],
        "pipeline_id": pipeline_identity["id"],
        "pipeline": pipeline_identity,
        "subject": digest(files),
        "files": files,
        "source": source_lock,
        "evidence_scope": {
            "source_identity": source_manifest["identity"],
            "source_evidence_class": source_manifest["evidence_class"],
            "execution": "frozen_snapshot_replay",
            "live_cad_connection_tested": False,
            "sampling": "Conclusions cover recorded samples and conditions, not the continuous workspace",
        },
        "toolchain": tool,
        "environment": environment(),
        "profile_name": profile_name,
        "profile": profile,
        "profile_digest": digest(profile),
        "checks": checks,
        "passed": passed,
        "qualified_for": [profile["purpose"]] if passed else [],
        "blockers": [check["id"] for check in checks if check["status"] not in {"passed", "not_applicable"}],
    }
    advisories = report_advisories(checks)
    if advisories:
        report["advisories"] = advisories
    return report


def report_advisories(checks: list[dict]) -> list[dict]:
    """The non-blocking notes the checks recorded; the CLI prints them as `note:` lines."""

    return [
        {"code": str(check["id"]), "message": str(check["details"]["advisory"])}
        for check in checks
        if isinstance(check.get("details"), dict) and check["details"].get("advisory")
    ]


def build(
    root: Path, profile_name: str, destination: Path | None = None, *, mechanical_reference: Path | None = None
) -> dict:
    root = root.resolve()
    if mechanical_reference is not None:
        from ..verification.mechanics import reference_path

        # Validate against the caller's workspace before creating a staging directory.
        mechanical_reference = reference_path(mechanical_reference, root)
    target = (destination or root).resolve()
    staging = Path(tempfile.mkdtemp(prefix=".description-build-", dir=target.parent))
    try:
        config, _locked, source, _manifest = inputs(root)
        tool = verify_toolchain(root)
        profile = profile_for(root, profile_name)
        canonical = normalize(load_scene(source), config, root, source)
        for name in ("config", "sources", "docs"):
            shutil.copytree(root / name, staging / name)
        key = digest(
            {
                "stage": "generate/v1",
                "canonical": canonical,
                "source": _locked["manifest_digest"],
                "profile": profile,
                "tool": tool,
            }
        )
        generation_cache = root / "build/cache/generate"
        reused = cache.restore(generation_cache, key, staging)
        if not reused:
            generate(Robot.from_dict(canonical), source, staging, profile)
            cache.save(generation_cache, key, staging)
        report = assess(staging, profile_name, verify_manifest=False, mechanical_reference=mechanical_reference)
        report["execution"] = {
            "generation": "cache_reuse" if reused else "executed",
            "generation_key": key,
            "verification": "executed",
        }
        write_json(staging / "docs/quality.json", report)
        lines = [
            f"# Model qualification: {profile_name}",
            "",
            f"Subject: `{report['subject']}`",
            "",
            f"Pipeline: `{report.get('pipeline_id') or 'unmapped'}`",
            "",
            f"Passed: {report['passed']}",
            "",
            "| Check | Status | Missing |",
            "|---|---|---|",
        ]
        lines.extend(
            f"| {check['id']} | {check['status']} | {', '.join(check['missing'])} |" for check in report["checks"]
        )
        (staging / "docs/quality.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        write_json(
            staging / "manifest.json",
            {
                "schema_version": "description.bundle/v1",
                "subject": report["subject"],
                "files": report["files"],
                "pipeline_id": report.get("pipeline_id"),
                "reports": {name: file_digest(staging / name) for name in ("docs/quality.json", "docs/quality.md")},
                "profile": profile_name,
                "source": _locked,
                "toolchain": tool,
                "acceptance": {
                    f"docs/acceptance/{name}": value for name, value in inventory(staging / "docs/acceptance").items()
                },
            },
        )
        if not report["passed"]:
            # Keep the diagnostic root short on Windows.  The frozen snapshot
            # deliberately retains its content-addressed 64-character directory;
            # putting another full digest in front of it can push CAD files past
            # the default Win32 MAX_PATH limit before replay can inspect them.
            failed_root = root / "build/failed"
            failed_root.mkdir(parents=True, exist_ok=True)
            failed = Path(tempfile.mkdtemp(prefix="candidate-", dir=failed_root))
            failed.rmdir()
            shutil.move(str(staging), str(failed))
            report["diagnostic_path"] = str(failed)
            return report
        if author_files(root) != author_files(staging):
            raise PipelineError("Author inputs changed during build; freeze the new inputs and rebuild")
        if destination is not None:
            if target.exists():
                raise PipelineError("Build destination exists; publish an immutable new directory")
            os.replace(staging, target)
        else:
            publish(staging, root)
        return report
    except Exception as error:
        failed_root = root / "build/failed"
        failed_root.mkdir(parents=True, exist_ok=True)
        failed = Path(tempfile.mkdtemp(prefix="interrupted-", dir=failed_root))
        write_json(staging / "failure.json", {"error": type(error).__name__, "message": str(error)})
        failed.rmdir()
        shutil.move(str(staging), str(failed))
        vars(error)["diagnostic_path"] = str(failed)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def author_files(root: Path) -> dict:
    """Inputs that must remain unchanged while a candidate is constructed."""
    files = {}
    generated = {"config/consumer.json", "docs/quality.json", "docs/quality.md"}
    for directory in ("config", "sources", "docs"):
        for name, value in inventory(root / directory).items():
            path = f"{directory}/{name}"
            if path not in generated:
                files[path] = value
    return files


def _candidate_object(directory: Path, relative: str) -> dict:
    """Read an artifact of a built candidate; a broken one names how to produce it again."""

    fix = f"pass a built candidate (run `description build --root {directory}`)"
    try:
        data = read_data(directory / relative)
    except FileNotFoundError:
        raise PipelineError(f"{directory} has no {relative}; {fix}") from None
    except PipelineError as error:
        raise PipelineError(f"{error}; {fix}") from None
    if not isinstance(data, dict):
        raise PipelineError(f"{relative} must contain an object, found {type(data).__name__}: {directory}; {fix}")
    return data


def _robot_index(data: dict, field: str, directory: Path) -> dict:
    """Index one robot field by id, naming the candidate that is not a built model."""

    try:
        return {item["id"]: item for item in data[field]}
    except (KeyError, TypeError) as error:
        raise PipelineError(
            f"{directory}/model/robot.json is not a robot model ({field}: {error}); "
            f"pass a built candidate (run `description build --root {directory}`)"
        ) from None


def _diff_area_changed(area: str, detail) -> bool:
    """Whether one area carries something.

    The five object categories always appear in ``changes`` so that shape stays stable for machine
    readers; empty ``added``/``removed``/``modified`` lists mean the category did not change.
    """

    if area in OBJECT_FIELDS:
        return any(detail[kind] for kind in ("added", "removed", "modified"))
    return bool(detail)


def semantic_diff(before: Path, after: Path) -> dict:
    """Compare two built candidates: the machine report, plus the one-line answer a reviewer reads
    first (``summary``) — a full report of two real candidates can run to megabytes."""

    left = _candidate_object(before, "model/robot.json")
    right = _candidate_object(after, "model/robot.json")
    changes: dict = {}
    objects = {"added": 0, "removed": 0, "modified": 0}
    for field in OBJECT_FIELDS:
        a = _robot_index(left, field, before)
        b = _robot_index(right, field, after)
        changes[field] = {
            "added": sorted(b.keys() - a.keys()),
            "removed": sorted(a.keys() - b.keys()),
            "modified": [
                {"id": key, "before": a[key], "after": b[key]}
                for key in sorted(a.keys() & b.keys())
                if a[key] != b[key]
            ],
        }
        for kind in objects:
            objects[kind] += len(changes[field][kind])
    for field in ("constraints", "control", "contact_excludes", "provenance"):
        if left[field] != right[field]:
            changes[field] = {"before": left[field], "after": right[field]}
    for file in ("config/robot.yaml", "config/toolchain.lock.json", "sources/source.lock.json"):
        a = _candidate_object(before, file)
        b = _candidate_object(after, file)
        if a != b:
            changes[file] = field_changes(a, b)
    for field in ("qualified_for", "profile", "environment"):
        before_value = _candidate_object(before, "docs/quality.json").get(field)
        after_value = _candidate_object(after, "docs/quality.json").get(field)
        if before_value != after_value:
            changes[field] = {"before": before_value, "after": after_value}
    changed_areas = [area for area, detail in changes.items() if _diff_area_changed(area, detail)]
    before_subject = digest(subject_files(before))
    after_subject = digest(subject_files(after))
    return {
        "schema_version": "description.diff/v1",
        # ``changed`` is about the compared areas; ``subject_changed`` is about the delivery as a
        # whole.  They can disagree — a mesh or an author document is delivered but not compared
        # field by field — and a reviewer has to be able to tell the two apart.
        "summary": {
            "changed": bool(changed_areas),
            "changed_areas": changed_areas,
            "robot_objects": objects,
            "subject_changed": before_subject != after_subject,
        },
        "before": before_subject,
        "after": after_subject,
        "changes": changes,
    }
