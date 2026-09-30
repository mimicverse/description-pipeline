"""Public command line: every automation calls the same application functions."""

from __future__ import annotations

import argparse
import json
import posixpath
import sys
import subprocess
from pathlib import Path, PureWindowsPath

from . import __version__
from .build import OBJECT_FIELDS, assess, build, freeze, lock_toolchain
from .build.publication import recover
from .sources.onshape.errors import OnshapeSourceError
from .sources.solidworks.errors import BridgeError
from .io import PipelineError, confined, pin_utf8_streams, read_data, write_json
from .quickstart import DEMO, PROFILE as QUICKSTART_PROFILE, run as run_demo, scaffold
from .repository import (
    check_layout,
    compare_models,
    dispatch,
    init_model,
    pending_candidates,
    promote,
    promotion_plan,
    submit,
    update,
    validate_commit,
)


def _commit_message(args) -> str:
    """The commit message comes from exactly one source; '-' means standard input.

    Reading it from a file or stdin is what lets the Windows entry pass a message
    without interpolating it into a shell or ssh command line.
    """

    if (args.message is not None) == (args.message_file is not None):
        raise PipelineError("Provide exactly one of --message or --message-file")
    if args.message_file is not None:
        text = (
            sys.stdin.read() if str(args.message_file) == "-" else Path(args.message_file).read_text(encoding="utf-8")
        )
    else:
        text = args.message
    if not text.strip():
        raise PipelineError("A commit message is required")
    return text


def _failure_message(error: BaseException) -> str:
    """Keep what the failing command said, so the caller can show the real cause.

    ``str(CalledProcessError)`` names the command and exit status but drops stderr;
    without it a wrapper such as the Windows submission entry can only report
    "returned non-zero exit status 128" and the actual cause has to be reproduced by
    hand.  Whitespace is collapsed so the value stays a single JSON string.
    """

    stderr = getattr(error, "stderr", None) or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    detail = " ".join(str(stderr).split())
    return f"{error}: {detail[:300]}" if detail else str(error)


#: Windows reports "path too long" as one of these codes; the message itself names a staging path
#: that means nothing to the operator.  Long paths are off by default, so this is common enough to
#: deserve the fix rather than the symptom.
WINDOWS_PATH_ERRORS = {3, 206}


def _path_advice(error: BaseException) -> str:
    """The fix for a Windows MAX_PATH failure, or an empty string."""

    if getattr(error, "winerror", None) in WINDOWS_PATH_ERRORS:
        # One source of truth with `description doctor --root .`, which warns before the failure.
        from .doctor import WINDOWS_LONG_PATH_ADVICE

        return WINDOWS_LONG_PATH_ADVICE + " The workspace was not modified."
    return ""


DEFAULT_WORKER_URL = "http://127.0.0.1:8765"


def _absolute_cad_path(value: str) -> bool:
    """A CAD path is absolute on Windows or on POSIX, whichever machine authored it.

    A model may be prepared on Linux for a Windows workstation (or the other way round), so a
    ``D:/...`` path has to be accepted on Linux too; ``Path.is_absolute`` only knows the local rules.
    """

    return posixpath.isabs(value) or bool(PureWindowsPath(value).drive)


def _source_mapping(args) -> dict:
    """Build the ``source`` mapping from command-line options.

    This is the novice path: the first-use guide used to ask for a hand-written YAML file, which is
    the first place a new user can go wrong.  The options mirror the documented keys exactly, and the
    SolidWorks mapping is validated with the same function the freeze uses, so a typo fails now
    rather than after SolidWorks has been opened.
    """

    if args.provider == "solidworks":
        if args.assembly is None:
            raise PipelineError("--provider solidworks needs --assembly D:/robots/myrobot/robot.SLDASM")
        assembly = str(args.assembly).replace("\\", "/")
        if not _absolute_cad_path(assembly):
            raise PipelineError("--assembly must be an absolute path, like D:/robots/myrobot/robot.SLDASM")
        allowed = args.allowed_roots or [Path(assembly).parent]
        for root in allowed:
            if not _absolute_cad_path(str(root).replace("\\", "/")):
                raise PipelineError(f"--allowed-roots must be absolute directories, got {root}")
        if not args.configuration.strip():
            raise PipelineError("--configuration is required; the adapter never guesses the active configuration")
        mapping = {
            "provider": "solidworks",
            "assembly": assembly,
            "configuration": args.configuration,
            "allowed_roots": [str(path).replace("\\", "/").rstrip("/") for path in allowed],
            "worker_url": args.worker_url or DEFAULT_WORKER_URL,
            "require_saved": True,
            "geometry": {"enabled": True, "format": "stl_binary"},
        }
        from .sources.solidworks.freeze import validate_source_config

        validate_source_config(mapping)
        return mapping

    if args.url:
        mapping = {"provider": "onshape", "url": args.url}
    elif args.document_id and args.element_id and (args.workspace_id or args.version_id):
        mapping = {"provider": "onshape", "document_id": args.document_id, "element_id": args.element_id}
        if args.workspace_id:
            mapping["workspace_id"] = args.workspace_id
        else:
            mapping["version_id"] = args.version_id
    else:
        raise PipelineError(
            "--provider onshape needs --url, or --document-id/--element-id together with --workspace-id or --version-id"
        )
    if args.configuration.strip():
        mapping["configuration"] = args.configuration
    return mapping


def _purpose_hints(value: dict, root: Path, profile: str) -> list[str]:
    """Hints for blockers that need more than "fix the blockers above".

    ``consumer.application`` means the application acceptance suites were never run against this
    build. The check reports the twenty suite names it is missing and nothing about how to produce
    them, so a user following the report's own advice (run `check` again) learns nothing new.

    Which way out exists depends on the purpose, and they are not interchangeable: `description model
    accept` records the *simulation* acceptance (`docs/acceptance/simulation.json`), so pointing a
    `hardware` build at it would send the user to a command that cannot clear the blocker.
    """

    blockers = value.get("blockers") or []
    if "consumer.application" not in blockers:
        return []
    purpose = ""
    try:
        purpose = str(read_data(confined(root, f"config/profiles/{profile}.json")).get("purpose") or "")
    except (PipelineError, OSError, ValueError):
        purpose = ""
    purpose = purpose or profile
    if purpose != "simulation":
        return [
            f"a {purpose} build needs application evidence this tool does not record: "
            "`description model accept` writes the `simulation` acceptance only — see the "
            "qualification boundaries in `docs/validation.md`"
        ]
    return [
        "the application acceptance has not been recorded for this build: run "
        f"`description model accept --root {root} --profile {profile} --out <result directory>` "
        "and keep its record with the delivery"
    ]


def _with_next(value: dict, hints: list[str]) -> dict:
    """Attach the next commands to a result so the CLI can show a new user the way forward."""

    if isinstance(value, dict) and hints:
        value.setdefault("next", hints)
    return value


def _diff_reference(identity: dict) -> str:
    """Name one side of a diff the way the report names it: a commit prefix, or the directory."""

    if identity.get("commit"):
        return str(identity["commit"])[:12]
    return str(identity.get("directory", ""))


def _diff_area(area: str, detail) -> str:
    """Describe one changed area in a few words; the JSON report keeps every field of it."""

    if area in OBJECT_FIELDS:
        counts = [f"{len(detail[kind])} {kind}" for kind in ("added", "removed", "modified") if detail[kind]]
        return f"{area}: {' + '.join(counts)}"
    if isinstance(detail, list):
        return f"{area}: {len(detail)} field{'s' if len(detail) != 1 else ''}"
    return area


def _diff_line(value: dict) -> str:
    """The sentence a review starts with, in front of a report that can run to megabytes."""

    summary = value["summary"]
    left, right = (_diff_reference(item) for item in value["references"])
    before, after = str(value["before"])[:12], str(value["after"])[:12]
    subject = f"subject {before} -> {after}" if summary["subject_changed"] else f"subject {before} unchanged"
    if not summary["changed"]:
        if summary["subject_changed"]:
            return (
                f"diff: {left} -> {right}: no changes in the compared areas, but the delivery digest moved "
                f"({before} -> {after}); a delivered file the report does not compare has changed"
            )
        return f"diff: {left} -> {right}: no changes; {subject}"
    changes = value["changes"]
    areas = [_diff_area(area, changes[area]) for area in summary["changed_areas"]]
    listed = ", ".join(areas[:5]) + (f", +{len(areas) - 5} more" if len(areas) > 5 else "")
    return f"diff: {left} -> {right}: {len(areas)} area{'s' if len(areas) != 1 else ''} changed ({listed}); {subject}"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="description",
        description=__doc__,
        epilog=(
            "common tasks:\n"
            "  description quickstart --run           create and run the offline demo (no CAD needed)\n"
            "  description doctor --root .            check this installation and workspace\n"
            "  description tool lock --root . && description source freeze --root . && description build --root .\n"
            "  description model update               capture, build, validate and submit a model in one command\n"
            "  description check --root .             re-derive a delivered model and qualify it\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=__version__)
    commands = p.add_subparsers(dest="command", required=True)
    quickstart = commands.add_parser(
        "quickstart",
        help="create the offline demo workspace and, with --run, build and check it",
    )
    quickstart.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path(DEMO),
        help=f"directory to write; it must be missing or empty (default: ./{DEMO})",
    )
    quickstart.add_argument("--run", action="store_true", help="also run tool lock, source freeze, build and check")
    doctor = commands.add_parser("doctor", help="check this installation, and a model workspace when given")
    doctor.add_argument("--root", type=Path, help="model workspace to inspect")
    doctor.add_argument("--json", action="store_true", help="print the machine-readable report")
    doctor.add_argument("--github", action="store_true", help="also check that the GitHub CLI is installed")
    recovery = commands.add_parser("recover")
    recovery.add_argument("--root", type=Path, required=True)
    for name in ("build", "check"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--root", type=Path, required=True)
        cmd.add_argument("--profile", default="kinematics")
        cmd.add_argument("--report", type=Path)
        if name == "build":
            cmd.add_argument("--output", type=Path)
    source = commands.add_parser("source").add_subparsers(dest="operation", required=True)
    source.add_parser("freeze").add_argument("--root", type=Path, required=True)
    tool = commands.add_parser("tool").add_subparsers(dest="operation", required=True)
    tool.add_parser("lock").add_argument("--root", type=Path, required=True)
    diff = commands.add_parser("diff")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    diff.add_argument(
        "--repository", type=Path, default=Path.cwd(), help="Repository used to resolve commit or branch arguments"
    )
    diff.add_argument(
        "--json", action="store_true", help="print only the JSON report; omit the summary sentence on stderr"
    )
    model = commands.add_parser("model").add_subparsers(dest="operation", required=True)
    accept = model.add_parser("accept", help="Run declared simulation acceptance on a built candidate")
    accept.add_argument("--root", type=Path, required=True, help="Read-only built candidate")
    accept.add_argument("--profile", default="simulation")
    accept.add_argument("--config", type=Path, default=Path("config/simulation-acceptance.json"))
    accept.add_argument("--out", type=Path, required=True, help="New output directory outside the candidate")
    init = model.add_parser("init")
    init.add_argument("--root", type=Path, required=True)
    init.add_argument("--hardware", required=True)
    init.add_argument(
        "--source-config",
        type=Path,
        help="source mapping to copy; alternatively describe it with --provider and its options",
    )
    init.add_argument("--provider", choices=("solidworks", "onshape"), help="build the source mapping from options")
    init.add_argument("--assembly", type=Path, help="solidworks: absolute path of the top-level SLDASM")
    init.add_argument("--configuration", default="", help="CAD configuration to capture (never guessed)")
    init.add_argument(
        "--allowed-roots",
        type=Path,
        action="append",
        help="solidworks: directories the dependencies must stay in (repeatable; defaults to the assembly directory)",
    )
    init.add_argument("--worker-url", default="", help="solidworks: local worker URL (default http://127.0.0.1:8765)")
    init.add_argument("--url", help="onshape: document URL (workspace or version)")
    init.add_argument("--document-id", default="", help="onshape: document id when no URL is given")
    init.add_argument("--element-id", default="", help="onshape: element id when no URL is given")
    init.add_argument("--workspace-id", default="", help="onshape: workspace id when no URL is given")
    init.add_argument("--version-id", default="", help="onshape: version id when no URL is given")
    init.add_argument("--repository", type=Path)
    init.add_argument("--base", default="HEAD")
    for name in ("submit", "update", "dispatch", "validate", "pending", "promote", "layout"):
        cmd = model.add_parser(name)
        cmd.add_argument(
            "--root", type=Path, required=name != "update", default=Path.cwd() if name == "update" else None
        )
        cmd.add_argument("--profile", default="kinematics")
        if name in {"submit", "update"}:
            cmd.add_argument("--message")
            cmd.add_argument("--message-file", type=Path, help="read the commit message from a file; '-' reads stdin")
        if name in {"submit", "update", "promote", "pending"}:
            cmd.add_argument(
                "--ci", action="store_true", help="Also request or require GitHub CI (disabled by default)"
            )
        if name == "update":
            cmd.add_argument(
                "--reuse-source",
                action="store_true",
                help="Rebuild definition/evidence edits from the verified frozen source; do not contact CAD",
            )
            cmd.add_argument(
                "--worker-host",
                help="Linux: tunnel to a Windows SSH alias; source.worker_url must use http://127.0.0.1:<port>",
            )
            cmd.add_argument("--worker-port", type=int, help="Windows worker port behind --worker-host (default: 8765)")
            cmd.add_argument(
                "--expect-worker-url",
                help="Fail unless source.worker_url already equals this value (guards a stale tunnel port)",
            )
        if name in {"dispatch", "validate", "promote"}:
            cmd.add_argument("--candidate", required=True)
        if name == "validate":
            cmd.add_argument("--report", type=Path)
            cmd.add_argument("--remote", action="store_true", help="Fetch the candidate into a fresh Git/LFS store")
        if name == "promote":
            cmd.add_argument("--hardware", required=True)
            cmd.add_argument("--tag", help="Optional immutable alias; consumers pin the model commit SHA")
            cmd.add_argument("--review-evidence")
            cmd.add_argument("--apply", action="store_true")
        if name == "layout":
            cmd.add_argument("--role", choices=("model", "tooling"), default="model")
    worker = commands.add_parser("worker").add_subparsers(dest="operation", required=True)
    doctor = worker.add_parser("doctor")
    doctor.add_argument("--target", required=True, help="HTTP worker endpoint, usually a local SSH tunnel")
    doctor.add_argument("--assembly")
    doctor.add_argument("--configuration")
    return p


def main(argv: list[str] | None = None) -> int:
    # The CLI exchanges JSON and commit messages with launchers on both platforms.
    # Windows redirected streams otherwise use the active ANSI code page.
    pin_utf8_streams()
    args = parser().parse_args(argv)
    report_path = getattr(args, "report", None)
    try:
        root_argument = getattr(args, "root", None)
        if root_argument is not None and root_argument.exists() and not root_argument.is_dir():
            raise PipelineError(f"--root must be a directory: {root_argument}")
        if report_path and getattr(args, "root", None):
            resolved = report_path.resolve()
            root = args.root.resolve()
            if resolved.is_relative_to(root) and not resolved.is_relative_to(root / "build"):
                raise PipelineError("Write check reports outside the immutable bundle or under build/")
        if args.command == "recover":
            value = recover(args.root.resolve())
        elif args.command == "doctor":
            from . import doctor as doctor_module

            value = doctor_module.run(args.root, github=args.github)
            if not args.json:
                for line in doctor_module.lines(value):
                    print(line)
                return 0 if value["passed"] else 1
        elif args.command == "quickstart":
            value = scaffold(args.directory)
            workspace = Path(value["root"])
            if args.run:
                value.update(run_demo(workspace))
                if value["passed"]:
                    value = _with_next(
                        value,
                        [
                            f"read {workspace / 'docs/quality.md'} to see what was checked",
                            f"change one joint limit in {workspace / 'urdf/robot.urdf'}, then run the check "
                            "again to watch the pipeline reject it",
                        ],
                    )
                else:
                    value = _with_next(value, ["fix the blockers above, then run description check --root . again"])
            else:
                value = _with_next(
                    value,
                    [
                        f"cd {workspace}",
                        "description tool lock --root .",
                        "description source freeze --root .",
                        f"description build --root . --profile {QUICKSTART_PROFILE}",
                        f"description check --root . --profile {QUICKSTART_PROFILE}",
                    ],
                )
        elif args.command == "build":
            value = build(args.root, args.profile, args.output)
            value = _with_next(
                value,
                [
                    f"description check --root {args.root} --profile {args.profile}",
                    *_purpose_hints(value, args.root, args.profile),
                ],
            )
        elif args.command == "check":
            value = assess(args.root.resolve(), args.profile)
            if value.get("passed"):
                value = _with_next(
                    value,
                    [f'description model submit --root {args.root} --profile {args.profile} --message "Update model"'],
                )
            else:
                value = _with_next(
                    value,
                    [
                        "fix the blockers above (or the files they name), then run this command again",
                        *_purpose_hints(value, args.root, args.profile),
                    ],
                )
        elif args.command == "source":
            value = freeze(args.root.resolve())
            value = _with_next(
                value,
                [
                    f"description doctor --root {args.root}",
                    f"description build --root {args.root} --profile kinematics",
                ],
            )
        elif args.command == "tool":
            value = lock_toolchain(args.root.resolve())
        elif args.command == "diff":
            value = compare_models(args.repository, args.before, args.after)
            if not args.json:
                print(_diff_line(value), file=sys.stderr)
        elif args.command == "worker":
            from .sources.solidworks.remote import WorkerClient

            value = WorkerClient(args.target).doctor(args.assembly, args.configuration)
            value["passed"] = (
                value.get("installed", False)
                and value.get("worker_alive", False)
                and value.get("solidworks_reachable", False)
                and (not args.assembly or value.get("cad_collectable", False))
            )
        elif args.operation == "accept":
            from .verification.simulation import run_acceptance

            record = run_acceptance(args.root, args.profile, args.config, args.out)
            value = {
                "passed": bool(record["results"]) and all(item["passed"] for item in record["results"]),
                "subject": record["subject"],
                "tests": {item["suite"]: item["passed"] for item in record["results"]},
                "output": str(args.out.resolve()),
                "release_qualified": False,
            }
        elif args.operation == "init":
            if (args.source_config is None) == (args.provider is None):
                raise PipelineError(
                    "Describe the source with --source-config FILE, or with --provider solidworks|onshape "
                    "and its options"
                )
            if args.source_config is not None:
                if not args.source_config.is_file():
                    raise PipelineError(f"Source config not found: {args.source_config}")
                source = read_data(args.source_config)
            else:
                source = _source_mapping(args)
            value = init_model(
                args.root.resolve(),
                args.hardware,
                source,
                repository=args.repository,
                base=args.base,
            )
            root = args.root.resolve()
            value = _with_next(
                value,
                [
                    f"edit {root}/config/robot.yaml to declare bodies, joints, frames and limits",
                    f"description source freeze --root {root}",
                ],
            )
        elif args.operation == "layout":
            value = check_layout(args.root, args.role)
        elif args.operation == "submit":
            value = submit(args.root, args.profile, _commit_message(args), ci=args.ci)
        elif args.operation == "update":
            value = update(
                args.root,
                args.profile,
                None if args.message is None and args.message_file is None else _commit_message(args),
                expect_worker_url=args.expect_worker_url,
                reuse_source=args.reuse_source,
                worker_host=args.worker_host,
                worker_port=args.worker_port,
                ci=args.ci,
            )
        elif args.operation == "dispatch":
            value = dispatch(args.root, args.candidate, args.profile)
        elif args.operation == "validate":
            value = validate_commit(args.root, args.candidate, args.profile, remote=args.remote)
        elif args.operation == "pending":
            value = {"dispatched" if args.ci else "pending": pending_candidates(args.root, args.profile, ci=args.ci)}
        else:
            value = promotion_plan(args.root, args.hardware, args.candidate, args.profile, args.tag, ci=args.ci)
            if args.apply:
                value["review_evidence"] = args.review_evidence
                value = promote(args.root, value)
        if report_path:
            write_json(report_path, value)
        for hint in value.get("next") or []:
            print(f"next: {hint}", file=sys.stderr)
        for note in value.get("advisories") or []:
            print(f"note: {note.get('message', note)}", file=sys.stderr)
            for command in note.get("commands") or []:
                print(f"      {command}", file=sys.stderr)
            if note.get("then"):
                print(f"      {note['then']}", file=sys.stderr)
        print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if value.get("passed", value.get("ok", True)) else 1
    except (
        PipelineError,
        OSError,
        ValueError,
        KeyError,
        subprocess.SubprocessError,
        OnshapeSourceError,
        BridgeError,
        KeyboardInterrupt,
    ) as error:
        value = {
            "passed": False,
            "error": type(error).__name__,
            "message": " ".join(part for part in (_failure_message(error), _path_advice(error)) if part)
            or "Operation interrupted",
        }
        block = None
        if isinstance(error, OSError):
            from .doctor import app_control_message, app_control_rejection

            block = app_control_rejection(error)
        if block is not None:
            value["code"] = block["code"]
            value["winerror"] = block["winerror"]
            value["library"] = block.get("library")
            value["cause"] = block["error"]
            value["message"] = app_control_message(block)
        if vars(error).get("diagnostic_path"):
            value["diagnostic_path"] = str(vars(error)["diagnostic_path"])
        if isinstance(error, (OnshapeSourceError, BridgeError)):
            value.update(code=error.code, detail=error.detail)
        if report_path and (
            not report_path.resolve().is_relative_to(args.root.resolve())
            or report_path.resolve().is_relative_to(args.root.resolve() / "build")
        ):
            write_json(report_path, value)
        print(
            json.dumps(value, ensure_ascii=False),
            file=sys.stderr,
        )
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
