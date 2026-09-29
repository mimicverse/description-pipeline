"""Accept a release independently of the pipeline that built it.

    python tools/accept_release.py v0.3.17 0.3.17
    python tools/accept_release.py candidate 0.3.17 --local dist/88eb77f-a --ref 88eb77f

``tools/verify_distribution.py`` judges what is inside one built archive; this tool judges the
*release* around it: every file ``SHA256SUMS`` names matches the published bytes, the release page
carries nothing else, the Linux bundle installs offline into an empty environment whose CLI reports
the version it claims, both archives pass the packaging checker, the wheel and the sdist install
offline and pass their own doctor, both shipped examples rebuild byte-identically from the commit the
artifacts were built from, and the documented first run (``quickstart --run``) qualifies on the
released runtime.

The release page is read over HTTPS.  ``GH_TOKEN`` or ``GITHUB_TOKEN`` is needed while the project is
private; ``--local`` accepts the candidate directory ``tools/build_release.py`` wrote, which is the
pre-publication rehearsal.  Nothing here imports ``description_pipeline`` — a defect in the pipeline
must not be able to accept its own release — and nothing writes to the checkout, so the tool runs
unchanged on a delivery branch.

A repository that removed third-party material before publication carries a rewritten history, so an
artifact built before that names a commit the published history no longer contains.  Such a
repository publishes ``git filter-repo``'s map as ``docs/history/commit-map.txt``; when it is
present, an artifact that names a pre-publication commit is checked against its successor and the
report says which identities leaned on the map.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # ``tools`` is a namespace package beside the checkout root
    sys.path.insert(0, str(ROOT))

from tools.verify_distribution import checked_members, verify, verify_windows  # noqa: E402

REPOSITORY = "mimicverse/description-pipeline"
#: The shipped examples and the outputs a rebuild has to reproduce.  The mesh example carries the
#: mesh pipeline (STL in, mesh assets out), so its contract includes ``meshes/``.
EXAMPLES = {
    "demo-arm": ("examples/demo-arm", ("urdf", "mjcf")),
    "mesh-arm": ("examples/mesh-arm", ("urdf", "mjcf", "meshes")),
}
#: The example the packaged demo (``quickstart``) is a copy of.
DEMO = "examples/demo-arm"
#: Where a repository whose history was rewritten before publication keeps ``git filter-repo``'s
#: ``commit-map``; when the checkout carries it, pre-publication identities are resolved through it.
COMMIT_MAP = ROOT / "docs" / "history" / "commit-map.txt"
PROFILE = "kinematics"
SUMS = "SHA256SUMS"

#: The two complete distributions.  The wheel and the sdist are judged through ``SHA256SUMS``.
BUNDLES = {
    "linux": "mimicverse_description-{version}-linux-x86_64.zip",
    "windows": "description-worker-{version}-windows-x86_64.zip",
}

#: The two artifacts pip installs.  A bundle is verified by unpacking it; these are verified by using
#: them, because that is the only way a missing dependency or a data file left out of the sdist shows.
PACKAGES = {
    "wheel": "mimicverse_description-{version}-py3-none-any.whl",
    "sdist": "mimicverse_description-{version}.tar.gz",
}

#: Directories of the released example that a rebuild has to reproduce byte-identically.
OUTPUTS = ("urdf", "mjcf")

#: Variables that put the running checkout back on the import path of everything this tool spawns.
IMPORT_PATH_VARIABLES = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"})

#: The acceptance installs the Linux bundle: it needs ``bash`` and the POSIX ``venv/bin`` layout.
POSIX_ONLY = "the release acceptance installs the Linux bundle; run it on Linux, macOS or WSL"

#: The platform this process runs on, read once so a test can name another one.
PLATFORM = os.name

#: Report keys in the order the steps run, with the heading the summary prints for each.
STEPS = (
    ("checksums", "checksums"),
    ("distributions", "packaging checks"),
    ("offline_install", "offline install"),
    ("packages", "python packages"),
    ("example_rebuild", "example rebuild"),
    ("first_run", "first run"),
)


class AcceptanceError(RuntimeError):
    """The release cannot be accepted on its own terms."""


def say(message: str) -> None:
    """Progress goes to stdout immediately: a two-minute step must not look like a hang."""

    print(message, flush=True)


def excerpt(text: str, *, lines: int = 12, width: int = 2000) -> str:
    """The end of a long refusal: the first lines of a traceback are rarely the useful ones.

    A refusal whose payload is one long line loses its point when only the tail is kept — the v0.3.21
    acceptance printed `the linux archive was refused: t):` — so a truncated excerpt keeps both ends
    and says how much it dropped.
    """

    tail = "\n".join(text.strip().splitlines()[-lines:])
    if len(tail) <= width:
        return tail
    half = width // 2
    return f"{tail[:half]}\n… ({len(tail) - width} characters elided) …\n{tail[-half:]}"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def parse_sums(path: Path) -> dict[str, str]:
    """``SHA256SUMS`` as artifact name -> digest; a malformed line is a refusal, not a guess."""

    if not path.is_file():
        raise AcceptanceError(f"No {path.name} in {path.parent}")
    expected: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, separator, name = line.partition("  ")
        if separator != "  " or not re.fullmatch(r"[0-9a-f]{64}", digest) or not name.strip():
            raise AcceptanceError(f"Malformed checksum line in {path.name}: {line!r}")
        expected[name.strip()] = digest
    if not expected:
        raise AcceptanceError(f"{path.name} lists no artifacts")
    return expected


def artifact_problems(artifacts: Path, expected: dict[str, str]) -> tuple[list[str], list[str]]:
    """Checksum findings, and the files in ``artifacts`` that ``SHA256SUMS`` does not list.

    ``SHA256SUMS`` cannot list itself, and the release runbook writes the verification reports beside
    the artifacts, so unlisted files are reported instead of refused.
    """

    problems: list[str] = []
    for name, digest in sorted(expected.items()):
        path = artifacts / name
        if not path.is_file():
            problems.append(f"missing artifact: {name}")
        elif sha256(path) != digest:
            problems.append(f"{name} does not match its published SHA-256")
    listed = {*expected, SUMS}
    unlisted = sorted(path.name for path in artifacts.iterdir() if path.is_file() and path.name not in listed)
    return problems, unlisted


def page_problems(published: Sequence[str], expected: dict[str, str]) -> list[str]:
    """The release page and the checksum file have to describe the same set of files."""

    listed = {*expected, SUMS}
    problems = [
        f"the release page carries {name}, which {SUMS} does not list" for name in sorted(set(published) - listed)
    ]
    problems += [
        f"{SUMS} lists {name}, which the release page does not carry" for name in sorted(listed - set(published))
    ]
    return problems


def extract_bundle(bundle: Path, destination: Path) -> None:
    """Extract a bundle after the same member checks the packaging verifier applies."""

    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle) as archive:
        checked_members(archive)
        archive.extractall(destination)


def git(*arguments: str) -> bytes:
    """Read the local repository; accepting a release must not need a second checkout."""

    return subprocess.run(["git", "-C", str(ROOT), *arguments], check=True, capture_output=True).stdout


def environment() -> dict[str, str]:
    """The environment for everything that runs inside the release.

    This tool is run from a checkout, and a checkout is often reachable through ``PYTHONPATH`` (this
    repository's own gate sets it).  The release would then be judged against the unreleased code
    beside it: pip decides ``mimicverse-description`` is already satisfied and never writes the
    console script, and the installed CLI can import the working tree.  Nothing that runs inside the
    release may see it.
    """

    return {name: value for name, value in os.environ.items() if name not in IMPORT_PATH_VARIABLES}


def resolve(reference: str) -> str:
    """The commit an artifact set is accepted against, from a tag or any other commit-ish."""

    try:
        return git("rev-parse", "--verify", f"{reference}^{{commit}}").decode().strip()
    except subprocess.CalledProcessError as error:
        raise AcceptanceError(
            f"Unknown tag or commit: {reference}. Run `git fetch --tags origin` first, or pass --ref."
        ) from error


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command and keep enough of its output for the operator to act on."""

    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=environment())
    if completed.returncode:
        raise AcceptanceError(
            f"{command[0]} failed ({completed.returncode}): {excerpt(completed.stdout + completed.stderr)}"
        )
    return completed


def report_from(text: str) -> dict[str, Any]:
    """The pipeline logs before it prints its report, so the report starts at the first ``{``."""

    start = text.find("{")
    if start < 0:
        raise AcceptanceError(f"No report in the output: {excerpt(text)}")
    try:
        value = json.loads(text[start:])
    except json.JSONDecodeError as error:
        raise AcceptanceError(f"Unreadable report ({error}): {excerpt(text)}") from error
    if not isinstance(value, dict):
        raise AcceptanceError(f"The report is not an object: {excerpt(text)}")
    return value


def reported(*arguments: str) -> tuple[dict[str, Any], int]:
    """Run a command that prints a report; a failing exit code is data, not an exception."""

    completed = subprocess.run([*arguments], capture_output=True, text=True, encoding="utf-8", env=environment())
    text = completed.stdout if "{" in completed.stdout else completed.stderr
    return report_from(text), completed.returncode


def copy_artifacts(source: Path, destination: Path) -> None:
    """Take a candidate directory as it is; ``SHA256SUMS`` decides what is checked."""

    if not source.is_dir():
        raise AcceptanceError(f"--local is not a directory: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.iterdir()):
        if path.is_file():
            shutil.copyfile(path, destination / path.name)


def token() -> str:
    """The credential for a release page that is not public; empty means anonymous access."""

    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""


def platform_problems(platform: str | None = None) -> list[str]:
    """The platforms this tool can run on, decided before it downloads a hundred megabytes.

    The Linux bundle is installed with ``bash`` and reports its command at ``venv/bin/description``;
    neither exists on a native Windows install, and the failure would surface as a missing file three
    steps later.  WSL is a POSIX platform here, so it is the way out the message names.
    """

    return [] if (platform or PLATFORM) == "posix" else [POSIX_ONLY]


def _open(url: str, credential: str, accept: str) -> Any:
    headers = {"User-Agent": "description-accept-release"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    if accept:
        headers["Accept"] = accept
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120)
    except urllib.error.HTTPError as error:
        hint = ""
        if error.code == 404:
            hint = " (no such release or asset; a private project answers 404 until GH_TOKEN is set)"
        elif error.code == 403:
            hint = " (the anonymous API allows 60 requests an hour; set GH_TOKEN to authenticate)"
        raise AcceptanceError(f"HTTP {error.code} for {url}{hint}") from error
    except urllib.error.URLError as error:
        raise AcceptanceError(f"Cannot reach {url}: {error.reason}") from error


def fetch_bytes(url: str, credential: str = "", accept: str = "") -> bytes:
    with _open(url, credential, accept) as response:
        return response.read()


def fetch_file(url: str, destination: Path, credential: str = "", accept: str = "") -> int:
    with _open(url, credential, accept) as response, destination.open("wb") as sink:
        shutil.copyfileobj(response, sink)
    return destination.stat().st_size


def release_assets(tag: str, credential: str) -> dict[str, str]:
    """The published asset names and where to fetch them, straight from the release page."""

    metadata = json.loads(
        fetch_bytes(
            f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{tag}",
            credential,
            "application/vnd.github+json",
        )
    )
    return {asset["name"]: asset["url"] for asset in metadata["assets"]}


def download_release(tag: str, destination: Path, credential: str) -> list[str]:
    """Fetch the release assets and return the names the release page carries.

    The metadata comes from the API, because it is what makes the "no file the checksum file does not
    list" check possible; the bytes come from the public asset URLs, or from the API with a credential.
    """

    destination.mkdir(parents=True, exist_ok=True)
    assets = release_assets(tag, credential)
    for name in sorted(assets):
        url = assets[name] if credential else f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
        size = fetch_file(url, destination / name, credential, "application/octet-stream" if credential else "")
        say(f"    {name}: {size / 1_000_000:.1f} MB")
    return sorted(assets)


def export_examples(commit: str, destination: Path) -> None:
    """The shipped examples exactly as the commit carried them, without a checkout or a network."""

    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "examples.tar"
    archive.write_bytes(git("archive", commit, *(path for path, _ in EXAMPLES.values())))
    with tarfile.open(archive) as tar:
        tar.extractall(destination, filter="data")


def compare_trees(reference: Path, rebuilt: Path, directories: Sequence[str]) -> tuple[list[str], int]:
    """Byte-compare the committed outputs of ``directories`` with the rebuilt ones."""

    differences: list[str] = []
    compared = 0
    for name in directories:
        expected = {
            path.relative_to(reference).as_posix(): path for path in (reference / name).rglob("*") if path.is_file()
        }
        actual = {path.relative_to(rebuilt).as_posix(): path for path in (rebuilt / name).rglob("*") if path.is_file()}
        for relative in sorted(set(expected) | set(actual)):
            compared += 1
            if relative not in actual:
                differences.append(f"missing after the rebuild: {relative}")
            elif relative not in expected:
                differences.append(f"not part of the release: {relative}")
            elif expected[relative].read_bytes() != actual[relative].read_bytes():
                differences.append(f"differs from the release: {relative}")
    return differences, compared


def named_commit(result: dict[str, Any]) -> str:
    """The commit an artifact says it was built from, wherever its identity keeps it."""

    toolchain = result.get("toolchain")
    if isinstance(toolchain, dict):
        return str(toolchain.get("source_commit", ""))
    return str(result.get("source_commit", ""))


def read_commit_map(path: Path) -> dict[str, str]:
    """``git filter-repo``'s ``commit-map``, one ``<old> <new>`` pair per rewritten commit.

    A map that is half-read would turn "this artifact came from that commit" into a claim nobody can
    check, so a line that is not a pair of full commit ids is refused rather than skipped.
    """

    if not path.is_file():
        raise AcceptanceError(f"no commit map at {path}")
    mapping: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        fields = line.split()
        if not fields or fields == ["old", "new"]:
            continue
        if len(fields) != 2 or not all(re.fullmatch(r"[0-9a-f]{40}", field) for field in fields):
            raise AcceptanceError(f"{path} line {number} is not '<old> <new>': {line.strip()!r}")
        mapping[fields[0]] = fields[1]
    if not mapping:
        raise AcceptanceError(f"{path} carries no commit pairs")
    return mapping


def mapped_identities(
    distributions: dict[str, Any], commit: str, commit_map: dict[str, str] | None
) -> dict[str, dict[str, str]]:
    """The identities that hold only through the published map, with both commits named.

    A report has to say which identities leaned on the map: a reader who does not trust the map can
    see exactly which archives would fail without it.
    """

    if not commit_map:
        return {}
    translated: dict[str, dict[str, str]] = {}
    for label, result in distributions.items():
        named = named_commit(result)
        if named != commit and commit_map.get(named) == commit:
            translated[label] = {"names": named, "resolved": commit}
    return translated


def commit_problems(
    label: str, result: dict[str, Any], commit: str, commit_map: dict[str, str] | None = None
) -> list[str]:
    """An archive has to name the commit it was built from, and it has to be the accepted one.

    The Linux bundle reports it inside its toolchain identity, the Windows archive as a field of its
    own; both are written from the packaged commit, and neither is worth anything unread.  In a
    repository whose history was rewritten before publication the accepted commit is the successor of
    the one the artifact names, and ``commit_map`` is what proves the two are the same delivery.
    """

    named = named_commit(result)
    if named == commit or (commit_map and commit_map.get(named) == commit):
        return []
    return [f"the {label} archive was built from {named or 'an unnamed commit'}, not {commit[:12]}"]


def package_findings(label: str, version: str, observed: dict[str, Any]) -> list[str]:
    """A pip-installable artifact has to report the claimed version and pass its own doctor.

    ``doctor`` is what a user runs first, and it fails when a declared dependency is missing from the
    artifact; the report is the artifact's own, so this is the one place the release is allowed to
    judge itself — the version comes from the CLI, not from the packaging metadata it was built with.
    """

    findings: list[str] = []
    if observed.get("version") != version:
        findings.append(f"the {label} installs a CLI that reports {observed.get('version')!r}, not {version!r}")
    doctor = observed.get("doctor") or {}
    if not doctor.get("passed"):
        findings.append(f"the {label} fails its own doctor: {excerpt(str(doctor))}")
    return findings


def record(report: dict[str, Any], problems: list[str], name: str, findings: list[str], **details: Any) -> None:
    """Keep one step's findings beside the data a reader needs to reproduce the judgement."""

    report[name] = {"passed": not findings, "problems": findings, **details}
    # The flat list names the step that found it, so a reader can go straight to the detail above.
    problems += [f"{name}: {finding}" for finding in findings]


def accept(
    tag: str,
    version: str,
    artifacts: Path,
    commit: str,
    work: Path,
    *,
    published: Sequence[str] | None = None,
    commit_map: dict[str, str] | None = None,
    note: Callable[[str], None] = lambda message: None,
) -> dict[str, Any]:
    """Judge one release; every step records its own result, so one failure still reports the rest."""

    report: dict[str, Any] = {"tag": tag, "version": version, "commit": commit, "artifacts": str(artifacts)}
    problems: list[str] = []

    note(f"checking every artifact against {SUMS}")
    expected = parse_sums(artifacts / SUMS)
    findings, unlisted = artifact_problems(artifacts, expected)
    if published is not None:
        findings += page_problems(published, expected)
    record(
        report,
        problems,
        "checksums",
        findings,
        artifacts=dict(sorted(expected.items())),
        unlisted=unlisted,
        published=sorted(published) if published is not None else "not listed",
    )
    if findings:
        # Installing bytes that do not match their published digests is what this tool exists to
        # refuse, so nothing downstream runs on them.
        report["problems"] = problems
        report["passed"] = False
        return report

    note("checking both distributions with tools/verify_distribution.py")
    distributions: dict[str, Any] = {}
    findings = []
    for label, checker in (("linux", verify), ("windows", verify_windows)):
        try:
            distributions[label] = checker(artifacts / BUNDLES[label].format(version=version))
            findings += commit_problems(label, distributions[label], commit, commit_map)
        except Exception as error:  # a refusal is a finding; the other distribution is still checked
            distributions[label] = {"passed": False, "error": excerpt(str(error))}
            findings.append(f"the {label} archive was refused: {excerpt(str(error))}")
    record(
        report,
        problems,
        "distributions",
        findings,
        **distributions,
        commit_map=mapped_identities(distributions, commit, commit_map),
    )

    note("installing the Linux bundle offline into a fresh environment")
    venv = work / "venv"
    description = venv / "bin" / "description"
    findings = []
    installed = ""
    try:
        extract_bundle(artifacts / BUNDLES["linux"].format(version=version), work / "bundle")
        run([sys.executable, "-m", "venv", str(venv)])
        run(["bash", str(work / "bundle" / "install.sh"), "--venv", str(venv)])
        if not description.is_file():
            raise AcceptanceError(f"the Linux bundle installed no command at {description}")
        installed = run([str(description), "--version"]).stdout.strip()
        if installed != version:
            findings.append(f"the released CLI reports {installed!r}, not {version!r}")
    except Exception as error:  # the finding is the message; a missing install is reported, not raised
        findings.append(excerpt(str(error)))
    record(report, problems, "offline_install", findings, version=installed, venv=str(venv))

    note("installing the wheel and the sdist offline")
    findings = []
    packages: dict[str, Any] = {}
    for label, name in PACKAGES.items():
        package = artifacts / name.format(version=version)
        environment_root = work / f"{label}-venv"
        observed: dict[str, Any] = {}
        try:
            run([sys.executable, "-m", "venv", str(environment_root)])
            run(
                [
                    str(environment_root / "bin" / "python"),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--find-links",
                    str(work / "bundle" / "wheels"),
                    str(package),
                ]
            )
            console = environment_root / "bin" / "description"
            observed["version"] = run([str(console), "--version"]).stdout.strip()
            doctor, _ = reported(str(console), "doctor", "--json")
            observed["doctor"] = doctor
            findings += package_findings(label, version, observed)
        except Exception as error:
            findings.append(f"the {label} could not be installed offline: {excerpt(str(error))}")
        packages[label] = {
            "version": observed.get("version"),
            "doctor": (observed.get("doctor") or {}).get("passed", False),
        }
    record(report, problems, "packages", findings, **packages)

    note(f"exporting the shipped examples from {commit[:12]}")
    committed = work / "committed"
    try:
        export_examples(commit, committed)
    except Exception as error:
        for name in ("example_rebuild", "first_run"):
            record(report, problems, name, [f"the commit does not carry the examples: {excerpt(str(error))}"])
        report["problems"] = problems
        report["passed"] = False
        return report

    note("rebuilding the shipped examples with the released runtime")
    findings = []
    details: dict[str, Any] = {}
    for label, (path, outputs) in EXAMPLES.items():
        reference = committed / path
        workspace = work / f"workspace-{label}"
        qualified: list[str] = []
        compared = 0
        try:
            shutil.copytree(reference, workspace)
            for command in (
                [str(description), "tool", "lock", "--root", str(workspace)],
                [str(description), "source", "freeze", "--root", str(workspace)],
                [str(description), "build", "--root", str(workspace), "--profile", PROFILE],
            ):
                run(command)
            check, code = reported(str(description), "check", "--root", str(workspace), "--profile", PROFILE)
            qualified = list(check.get("qualified_for") or [])
            if code or not check.get("passed"):
                findings.append(f"{label}: the released tool did not qualify it: {excerpt(str(check))}")
            differences, compared = compare_trees(reference, workspace, outputs)
            findings += [f"{label}: {item}" for item in differences]
        except Exception as error:
            findings.append(f"{label}: {excerpt(str(error))}")
        details[label] = {"files_compared": compared, "qualified_for": qualified}
    record(report, problems, "example_rebuild", findings, **details)

    note("running the documented first run (quickstart --run)")
    findings = []
    first: dict[str, Any] = {}
    try:
        value, code = reported(str(description), "quickstart", str(work / "quickstart"), "--run")
        first = {"qualified_for": list(value.get("qualified_for") or [])}
        if code or not value.get("passed"):
            findings.append(f"quickstart --run did not qualify: {excerpt(str(value))}")
        packaged, _ = compare_trees(committed / DEMO, work / "quickstart", EXAMPLES["demo-arm"][1])
        first["matches_commit"] = not packaged
        if packaged:
            findings.append(f"the packaged demo is not the example in {commit[:12]}: {packaged[0]}")
    except Exception as error:
        findings.append(excerpt(str(error)))
    record(report, problems, "first_run", findings, **first)

    report["problems"] = problems
    report["passed"] = not problems
    return report


def describe(name: str, step: dict[str, Any]) -> str:
    """What a passed step proved, in the words the release record can quote."""

    if name == "checksums":
        return f"{len(step['artifacts'])} artifacts match {SUMS}"
    if name == "distributions":
        mapped = sorted((step.get("commit_map") or {}).keys())
        translated = f" (identity resolved through {COMMIT_MAP.relative_to(ROOT)} for {', '.join(mapped)})"
        return "linux and windows distributions verified" + (translated if mapped else "")
    if name == "offline_install":
        return f"the released CLI reports {step['version']}"
    if name == "packages":
        return f"{', '.join(PACKAGES)} install offline and report {step['wheel']['version']}"
    if name == "example_rebuild":
        files = sum(step[label]["files_compared"] for label in EXAMPLES)
        qualified = sorted({purpose for label in EXAMPLES for purpose in step[label]["qualified_for"]})
        return f"{' and '.join(EXAMPLES)} rebuilt byte-identically ({files} files) for {', '.join(qualified)}"
    return f"quickstart --run qualified for {', '.join(step['qualified_for'])}"


def summary(report: dict[str, Any]) -> list[str]:
    """One line per step, then the verdict."""

    lines = [f"accept: {report.get('tag')} {report.get('version')} at {str(report.get('commit') or 'unknown')[:12]}"]
    for name, title in STEPS:
        step = report.get(name)
        if not isinstance(step, dict):
            lines.append(f"[--  ] {title}: not run")
        elif step["passed"]:
            lines.append(f"[ok  ] {title}: {describe(name, step)}")
        else:
            lines.append(f"[fail] {title}: {step['problems'][0] if step['problems'] else step.get('error', '')}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tag", help="published release tag, or any label when --local is used")
    parser.add_argument("version", help="version the artifacts claim, for example 0.3.17")
    parser.add_argument("--local", type=Path, help="candidate directory to accept instead of the release page")
    parser.add_argument("--ref", help="commit the artifacts were built from (defaults to the tag)")
    parser.add_argument(
        "--commit-map",
        type=Path,
        help="rewritten-history map; default: docs/history/commit-map.txt when the checkout carries one",
    )
    parser.add_argument("--report", type=Path, help="write the acceptance report to this file")
    parser.add_argument("--work", type=Path, help="directory for the temporary workspace (default: $TMPDIR)")
    parser.add_argument("--keep-work", action="store_true", help="keep the artifacts and the environment")
    args = parser.parse_args(argv)

    report: dict[str, Any] = {"tag": args.tag, "version": args.version}
    work = Path(tempfile.mkdtemp(prefix=f"accept-{args.tag}-", dir=args.work))
    try:
        if problems := platform_problems():
            raise AcceptanceError(problems[0])
        map_path = args.commit_map or (COMMIT_MAP if COMMIT_MAP.is_file() else None)
        commit_map = read_commit_map(map_path) if map_path else None
        if commit_map:
            say(f"accept: reading the published commit map {map_path}")
        reference = resolve(args.ref or args.tag)
        report["commit"] = reference
        artifacts = work / "artifacts"
        published: list[str] | None = None
        if args.local:
            say(f"accept: {args.tag} {args.version}; candidate {args.local}; commit {reference[:12]}")
            copy_artifacts(args.local, artifacts)
        else:
            say(f"accept: {args.tag} {args.version}; release page {REPOSITORY}; commit {reference[:12]}")
            published = download_release(args.tag, artifacts, token())
        report.update(
            accept(
                args.tag,
                args.version,
                artifacts,
                reference,
                work,
                published=published,
                commit_map=commit_map,
                note=say,
            )
        )
        if args.keep_work:
            report["work"] = str(work)
    except AcceptanceError as error:
        report["problems"] = [str(error)]
        report["passed"] = False
    finally:
        if not args.keep_work:
            shutil.rmtree(work, ignore_errors=True)

    for line in summary(report):
        say(line)
    if report.get("passed"):
        say(f"PASS: {args.tag} accepted" + (f" (work kept in {report['work']})" if args.keep_work else ""))
    else:
        say(f"FAIL: {args.tag} was not accepted")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
