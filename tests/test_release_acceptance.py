"""Accepting a release has to be able to fail, and must not lean on the pipeline it judges.

``tools/accept_release.py`` is the last gate of the release runbook: it reads a published release and
decides whether it is the release it claims to be.  A full acceptance needs the published artifacts,
a fresh environment and the packaged commit, so it runs as a release rehearsal (the v0.3.17 run is
recorded with the release evidence).  What is pinned here are the decisions that need no release: the
checksum contract, extraction safety, the byte comparison of a rebuilt example, the refusals, and
the rule that the tool imports no pipeline code of its own.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from tools import accept_release, verify_distribution

ROOT = Path(__file__).resolve().parents[1]
GIT_CHECKOUT = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=ROOT, capture_output=True).returncode == 0
LINUX = "mimicverse_description-0.3.17-linux-x86_64.zip"
SDIST = "mimicverse_description-0.3.17.tar.gz"
HOSTILE = {
    "parent traversal": "../escape.txt",
    "absolute POSIX path": "/tmp/escape.txt",
    "Windows separator": "..\\escape.txt",
    "drive letter": "C:/escape.txt",
}


def candidate(root: Path) -> dict[str, str]:
    """A candidate directory with two artifacts and the checksum file that describes them."""

    digests = {}
    for name in (LINUX, SDIST):
        (root / name).write_bytes(f"contents of {name}".encode())
        digests[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    (root / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in digests.items()), encoding="utf-8"
    )
    return digests


def archive_of(root: Path, members: list[tuple[str, str]]) -> Path:
    path = root / "bundle.zip"
    with warnings.catch_warnings():  # a deliberately duplicated member warns while it is written
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w") as bundle:
            for name, text in members:
                bundle.writestr(name, text)
    return path


class ChecksumTests(unittest.TestCase):
    def problems(self, root: Path) -> list[str]:
        expected = accept_release.parse_sums(root / "SHA256SUMS")
        return accept_release.artifact_problems(root, expected)[0]

    def test_a_candidate_that_matches_its_checksums_has_no_findings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            digests = candidate(root)
            self.assertEqual(self.problems(root), [])
            self.assertEqual(accept_release.parse_sums(root / "SHA256SUMS"), digests)

    def test_a_changed_artifact_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate(root)
            (root / LINUX).write_bytes(b"tampered after the checksums were written")
            self.assertEqual(self.problems(root), [f"{LINUX} does not match its published SHA-256"])

    def test_a_missing_artifact_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate(root)
            (root / SDIST).unlink()
            self.assertEqual(self.problems(root), [f"missing artifact: {SDIST}"])

    def test_a_file_the_checksum_file_does_not_list_is_reported(self):
        """The runbook writes verification reports beside the artifacts, so this is not a refusal."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate(root)
            (root / "linux-verification.json").write_text("{}", encoding="utf-8")
            expected = accept_release.parse_sums(root / "SHA256SUMS")
            problems, unlisted = accept_release.artifact_problems(root, expected)
            self.assertEqual(problems, [])
            self.assertEqual(unlisted, ["linux-verification.json"])
            # The release page, unlike a candidate directory, has to carry exactly the listed files.
            self.assertEqual(accept_release.page_problems([*expected, "SHA256SUMS"], expected), [])
            self.assertEqual(
                accept_release.page_problems([*expected, "SHA256SUMS", "extra.zip"], expected),
                ["the release page carries extra.zip, which SHA256SUMS does not list"],
            )
            self.assertEqual(
                accept_release.page_problems(list(expected), expected),
                ["SHA256SUMS lists SHA256SUMS, which the release page does not carry"],
            )

    def test_a_malformed_checksum_file_is_refused(self):
        cases = {
            "one space separator": f"{'0' * 64} artifact.zip\n",
            "short digest": "0123456789abcdef  artifact.zip\n",
            "upper case digest": f"{'A' * 64}  artifact.zip\n",
            "no name": f"{'0' * 64}  \n",
            "empty": "\n",
        }
        for label, text in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "SHA256SUMS").write_text(text, encoding="utf-8")
                with self.assertRaises(accept_release.AcceptanceError):
                    accept_release.parse_sums(root / "SHA256SUMS")

    def test_a_candidate_without_a_checksum_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(accept_release.AcceptanceError, "No SHA256SUMS"):
                accept_release.accept("candidate", "0.3.17", root, "0" * 40, root / "work")


class ExtractionTests(unittest.TestCase):
    def test_hostile_members_are_refused_before_extraction(self):
        for label, member in HOSTILE.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                bundle = archive_of(root, [(member, "x")])
                with self.assertRaisesRegex(RuntimeError, "Invalid archive member"):
                    accept_release.extract_bundle(bundle, root / "bundle")
                self.assertFalse((root / "escape.txt").exists())

    def test_duplicate_members_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = archive_of(root, [("wheels/x.whl", "a"), ("wheels/x.whl", "a")])
            with self.assertRaisesRegex(RuntimeError, "Duplicate archive entries"):
                accept_release.extract_bundle(bundle, root / "bundle")

    def test_a_normal_bundle_is_extracted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = archive_of(root, [("install.sh", "set -euo pipefail\n"), ("wheels/x.whl", "a")])
            accept_release.extract_bundle(bundle, root / "bundle")
            self.assertTrue((root / "bundle" / "wheels" / "x.whl").is_file())

    def test_the_extraction_rule_is_the_packaging_verifier_rule(self):
        """One rule for both tools: a second copy of it is a second thing to forget to fix."""

        self.assertIs(accept_release.checked_members, verify_distribution.checked_members)


class ComparisonTests(unittest.TestCase):
    def trees(self, root: Path) -> tuple[Path, Path]:
        reference, rebuilt = root / "committed", root / "rebuilt"
        for base, urdf in ((reference, b"<robot/>"), (rebuilt, b"<robot/>")):
            (base / "urdf").mkdir(parents=True)
            (base / "urdf" / "robot.urdf").write_bytes(urdf)
            (base / "mjcf").mkdir(parents=True)
            (base / "mjcf" / "robot.xml").write_text("<mujoco/>", encoding="utf-8")
        return reference, rebuilt

    def test_an_identical_rebuild_has_no_differences(self):
        with tempfile.TemporaryDirectory() as temporary:
            reference, rebuilt = self.trees(Path(temporary))
            self.assertEqual(accept_release.compare_trees(reference, rebuilt, ("urdf", "mjcf")), ([], 2))

    def test_a_difference_a_missing_file_and_an_extra_file_are_all_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            reference, rebuilt = self.trees(Path(temporary))
            (rebuilt / "urdf" / "robot.urdf").write_bytes(b"<robot name='edited'/>")
            (rebuilt / "mjcf" / "robot.xml").unlink()
            (rebuilt / "mjcf" / "scene.xml").write_text("<scene/>", encoding="utf-8")
            differences, compared = accept_release.compare_trees(reference, rebuilt, ("urdf", "mjcf"))
            self.assertEqual(
                differences,
                [
                    "differs from the release: urdf/robot.urdf",
                    "missing after the rebuild: mjcf/robot.xml",
                    "not part of the release: mjcf/scene.xml",
                ],
            )
            self.assertEqual(compared, 3)

    def test_a_deleted_output_is_a_difference(self):
        with tempfile.TemporaryDirectory() as temporary:
            reference, rebuilt = self.trees(Path(temporary))
            for path in (rebuilt / "mjcf").iterdir():
                path.unlink()
            differences, _ = accept_release.compare_trees(reference, rebuilt, ("urdf", "mjcf"))
            self.assertEqual(differences, ["missing after the rebuild: mjcf/robot.xml"])


class ExcerptTests(unittest.TestCase):
    """A refusal cut in the middle loses the thing it was reporting; keep both ends and say so."""

    def test_a_short_refusal_is_untouched(self):
        self.assertEqual(accept_release.excerpt("boom", width=100), "boom")

    def test_a_long_single_line_keeps_both_ends(self):
        text = "Installed tool identity differs: " + "x" * 5000 + " tail"
        result = accept_release.excerpt(text, width=200)
        self.assertTrue(result.startswith("Installed tool identity differs: "), result)
        self.assertTrue(result.endswith(" tail"), result)
        self.assertIn("characters elided", result)


class ReferenceTests(unittest.TestCase):
    def test_an_archive_has_to_name_the_accepted_commit(self):
        commit = "a" * 40
        self.assertEqual(accept_release.commit_problems("linux", {"toolchain": {"source_commit": commit}}, commit), [])
        self.assertEqual(accept_release.commit_problems("windows", {"source_commit": commit}, commit), [])
        self.assertEqual(
            accept_release.commit_problems("windows", {"source_commit": "b" * 40}, commit),
            [f"the windows archive was built from {'b' * 40}, not {commit[:12]}"],
        )
        self.assertEqual(
            accept_release.commit_problems("linux", {"toolchain": {}}, commit),
            [f"the linux archive was built from an unnamed commit, not {commit[:12]}"],
        )

    def test_the_published_commit_map_resolves_a_pre_publication_identity(self):
        """A history rewritten before publication keeps the old name in the artifacts, not in Git."""

        old, new = "b" * 40, "a" * 40
        archive = {"toolchain": {"source_commit": old}}
        stale = f"the linux archive was built from {old}, not {new[:12]}"
        self.assertEqual(accept_release.commit_problems("linux", archive, new), [stale])
        self.assertEqual(accept_release.commit_problems("linux", archive, new, {old: new}), [])
        self.assertEqual(accept_release.commit_problems("linux", archive, new, {old: "c" * 40}), [stale])
        self.assertEqual(
            accept_release.mapped_identities({"linux": archive}, new, {old: new}),
            {"linux": {"names": old, "resolved": new}},
        )
        self.assertEqual(accept_release.mapped_identities({"linux": {"source_commit": new}}, new, {old: new}), {})
        self.assertEqual(accept_release.mapped_identities({"linux": archive}, new, None), {})

    def test_a_malformed_commit_map_is_refused_instead_of_half_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "commit-map.txt"
            path.write_text("old new\n" + "a" * 40 + " " + "b" * 40 + "\n", encoding="utf-8")
            self.assertEqual(accept_release.read_commit_map(path), {"a" * 40: "b" * 40})
            path.write_text("old new\nnot-a-commit b\n", encoding="utf-8")
            with self.assertRaisesRegex(accept_release.AcceptanceError, "line 2 is not"):
                accept_release.read_commit_map(path)
            path.write_text("old new\n", encoding="utf-8")
            with self.assertRaisesRegex(accept_release.AcceptanceError, "carries no commit pairs"):
                accept_release.read_commit_map(path)
            with self.assertRaisesRegex(accept_release.AcceptanceError, "no commit map at"):
                accept_release.read_commit_map(Path(temporary) / "missing.txt")

    def test_a_pip_artifact_has_to_report_its_version_and_pass_its_doctor(self):
        version = "0.3.17"
        self.assertEqual(
            accept_release.package_findings("wheel", version, {"version": version, "doctor": {"passed": True}}), []
        )
        self.assertEqual(
            accept_release.package_findings("sdist", version, {"version": "0.3.16", "doctor": {"passed": True}}),
            ["the sdist installs a CLI that reports '0.3.16', not '0.3.17'"],
        )
        self.assertEqual(
            accept_release.package_findings(
                "wheel", version, {"version": version, "doctor": {"passed": False, "failed": ["packages"]}}
            ),
            ["the wheel fails its own doctor: {'passed': False, 'failed': ['packages']}"],
        )
        self.assertEqual(
            accept_release.package_findings("wheel", version, {"version": version}),
            ["the wheel fails its own doctor: {}"],
        )

    @unittest.skipUnless(GIT_CHECKOUT, "the acceptance tool resolves the commit in a Git checkout")
    def test_a_tag_or_commit_resolves_to_one_commit(self):
        commit = accept_release.resolve("HEAD")
        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        self.assertEqual(len(commit), 40)

    @unittest.skipUnless(GIT_CHECKOUT, "the acceptance tool resolves the commit in a Git checkout")
    def test_an_unknown_reference_is_refused_with_the_way_out(self):
        with self.assertRaisesRegex(accept_release.AcceptanceError, "git fetch --tags origin"):
            accept_release.resolve("no-such-tag-9d3f")


class CommandTests(unittest.TestCase):
    def test_the_tool_refuses_a_platform_where_the_bundle_cannot_install(self):
        """A native Windows maintainer gets the way out instead of a missing ``venv/bin``."""

        self.assertEqual(accept_release.platform_problems("posix"), [])
        self.assertEqual(accept_release.platform_problems("nt"), [accept_release.POSIX_ONLY])

    def test_a_windows_command_reports_the_wsl_hint_and_downloads_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "acceptance.json"
            with mock.patch.object(accept_release, "PLATFORM", "nt"):
                code = accept_release.main(["v0.3.17", "0.3.17", "--report", str(report)])
            self.assertEqual(code, 1)
            value = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(value["problems"], [accept_release.POSIX_ONLY])
            self.assertFalse(value["passed"])
            self.assertNotIn("checksums", value)

    def test_nothing_that_runs_inside_the_release_sees_the_checkout(self):
        """The rehearsal that found this: with the checkout on ``PYTHONPATH``, pip reported
        ``mimicverse-description`` as already satisfied and never wrote the console script."""

        with mock.patch.dict(os.environ, {"PYTHONPATH": str(ROOT / "src"), "PYTHONHOME": "/nowhere"}):
            environment = accept_release.environment()
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("PYTHONHOME", environment)
        self.assertEqual(environment["PATH"], os.environ["PATH"])

    def test_subprocesses_get_the_scrubbed_environment(self):
        with (
            mock.patch.dict(os.environ, {"PYTHONPATH": str(ROOT / "src")}),
            mock.patch("subprocess.run") as runner,
        ):
            runner.return_value = subprocess.CompletedProcess([], 0, stdout='{"passed": true}', stderr="")
            accept_release.run(["true"])
            value, code = accept_release.reported("description", "check")
        self.assertEqual((value, code), ({"passed": True}, 0))
        for call in runner.call_args_list:
            self.assertNotIn("PYTHONPATH", call.kwargs["env"])

    def test_the_tool_imports_no_pipeline_code(self):
        """A defect in the pipeline must not be able to accept its own release."""

        code = (
            "import sys; import tools.accept_release; "
            "print([name for name in sys.modules if name.split('.')[0] == 'description_pipeline'])"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "[]")

    def test_the_summary_names_every_step(self):
        report = {"tag": "v0.3.17", "version": "0.3.17", "commit": "a" * 40}
        lines = accept_release.summary(report)
        self.assertEqual(lines[0], "accept: v0.3.17 0.3.17 at aaaaaaaaaaaa")
        for _, title in accept_release.STEPS:
            self.assertTrue(any(line.endswith(f"] {title}: not run") for line in lines), title)

    # The tampered-candidate path only exists where the Linux bundle can be installed at all; on a
    # native Windows host the tool refuses before it ever reads a checksum (see the WSL test above),
    # so this is a POSIX test with a Git checkout, not a checkout test.
    @unittest.skipUnless(
        GIT_CHECKOUT and accept_release.PLATFORM == "posix",
        "the acceptance tool installs the Linux bundle, which needs a POSIX host",
    )
    def test_a_tampered_candidate_fails_with_a_report_and_installs_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate(root)
            (root / LINUX).write_bytes(b"tampered")
            report = root / "acceptance.json"
            code = accept_release.main(
                ["candidate", "0.3.17", "--local", str(root), "--ref", "HEAD", "--report", str(report)]
            )
            self.assertEqual(code, 1)
            value = json.loads(report.read_text(encoding="utf-8"))
            self.assertFalse(value["passed"])
            self.assertEqual(value["problems"], [f"checksums: {LINUX} does not match its published SHA-256"])
            self.assertFalse(value["checksums"]["passed"])
            self.assertNotIn("offline_install", value)


if __name__ == "__main__":
    unittest.main()
