"""Real local bare-Git tests for the fast-forward single-PR publication boundary."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.delivery import read_source_record, subject_digest, write_delivery_manifest
from description_pipeline.interface_records import check_policy_digest
from description_pipeline.io import digest
from description_pipeline.request_evidence import build_request_evidence, request_evidence_bytes
from description_pipeline.repository import urdf_pr
from description_pipeline.schemas import CHECK_POLICY_SCHEMA, SOURCE_RECORD_SCHEMA
from description_pipeline.stages import CONTRACT

BRANCH = "work/solidworks/m3.0/engineering"
INSPECTION_BRANCH = "work/solidworks/m3.0/inspection_preview"
BODY_SLUG = "example/m3.0"
BODY_SHA256 = "c" * 64


def render_body(
    report: dict,
    *,
    subject: str = "a" * 64,
    commit: str = "b" * 40,
    purpose: str = "engineering",
    slug: str = BODY_SLUG,
    quality_sha256: str = BODY_SHA256,
) -> str:
    return urdf_pr._body(
        subject,
        commit,
        report,
        purpose,
        slug=slug,
        quality_sha256=quality_sha256,
    )


def representative_m3_quality_report() -> dict:
    """A compact canonical report with the audited 24-link/23-joint shape."""

    warning_row_counts = (
        ("direction.semantics_unresolved", 23),
        ("discovery.publication_metadata_incomplete", 0),
        ("discovery.revision_absent", 0),
        ("display.constant", 85),
        ("geometry.derived_from_native", 47),
        ("input.cad_revision_absent", 0),
        ("limits.preview_policy", 23),
        ("naming.content_addressed_mesh", 85),
        ("naming.derived_material", 85),
        ("naming.derived_visual", 85),
        ("naming.formatted_interface", 46),
        ("naming.formatted_robot_name", 1),
        ("naming.motion_unresolved", 7),
        ("physics.derived_from_native", 24),
        ("units.si_conversion", 1),
    )
    diagnostics = []
    for code, count in warning_row_counts:
        objects = [
            {
                "object_type": "record",
                "object": f"{code}:{index:03d}",
                "field": "value",
                "source": f"{code}.source",
                "rule_id": f"{code}.rule",
                "rule_version": "v1",
                "original": index,
                "actual": index + 1,
                "reason": "Representative M3 warning",
            }
            for index in range(count)
        ]
        diagnostics.append(
            {
                "code": code,
                "blocking": False,
                "message": f"{code} recorded",
                "detail": {"objects": objects} if objects else {"recorded": False},
            }
        )
    for index in range(23):
        diagnostics.append(
            {
                "code": "urdf.motion_unqualified",
                "blocking": False,
                "message": "Authored motion values are not qualified",
                "detail": {
                    "joint": f"joint_{index:02d}",
                    "values": ["effort", "velocity"],
                },
            }
        )

    qualification_counts = (
        *((fact_id, "established", 1) for fact_id in (
            "bundle.subject",
            "consumer.urdf",
            "frames.components",
            "frames.native",
            "frames.references",
            "geometry.assets",
            "geometry.coverage",
            "geometry.native_tessellation",
            "input.valid",
            "model.policy",
            "model.schema",
            "physics.independent",
            "physics.mass_closure_equality",
            "provenance.value_warnings",
            "source.carrier_binding",
            "source.coverage",
            "source.dependencies",
            "source.integrity",
            "source.native",
            "source.native_discovery",
            "source.raw",
            "tool.identity",
            "urdf.syntax_names",
            "urdf.topology",
            "verification.complete",
        )),
        ("geometry.expected_extent", "out_of_scope", 1),
        ("physics.expected_mass", "out_of_scope", 1),
        ("geometry", "established", 24),
        ("inertia", "established", 24),
        ("joints", "established", 23),
        ("motion", "established", 23),
        ("shafts", "established", 23),
        ("drive_sizing", "unqualified", 23),
        ("engineering_position_range", "unqualified", 23),
        ("machine_qualification", "unqualified", 1),
    )
    qualifications = [
        {
            "fact_id": fact_id,
            "status": status,
            "scope": "",
            "object_id": f"{fact_id}:{index:03d}",
        }
        for fact_id, status, count in qualification_counts
        for index in range(count)
    ]
    return {"checks": [], "diagnostics": diagnostics, "qualifications": qualifications}


def _request_text(purpose: str = "engineering") -> str:
    return request_evidence_bytes(
        build_request_evidence(
            purpose=purpose,
            request_id="fixture-frozen-request",
            target_hardware="m3.0",
            change_summary="fixture submission",
            main_assembly="M3.0.SLDASM",
            handoff_digest="a" * 64,
            platform_config_digest="c" * 64,
        )
    ).decode("utf-8")


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


class Fixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="urdf-pr-test-")
        self.tmp = Path(self.temp.name)
        self.remote = self.tmp / "remote.git"
        run("git", "init", "--bare", "-b", "feature/m3.0", str(self.remote))
        self.seed = self.tmp / "seed"
        self.seed.mkdir()
        run("git", "init", "-b", "feature/m3.0", cwd=self.seed)
        for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("core.autocrlf", "false")):
            run("git", "config", key, value, cwd=self.seed)
        (self.seed / "README.md").write_bytes(b"base\n")
        run("git", "add", "-A", cwd=self.seed)
        run("git", "commit", "-m", "base", cwd=self.seed)
        run("git", "remote", "add", "origin", str(self.remote), cwd=self.seed)
        run("git", "push", "-u", "origin", "feature/m3.0", cwd=self.seed)
        self.repo = self.tmp / "repo"
        run("git", "clone", "-c", "core.autocrlf=false", str(self.remote), str(self.repo))
        for key, value in (("user.email", "t@example.com"), ("user.name", "T")):
            run("git", "config", key, value, cwd=self.repo)
        self.bundle = self.tmp / "bundle"
        self.purpose = "engineering"
        self.write_bundle("one\n")
        self.seal("r1", None, cad="part-1\n")

    def write_manifest(self) -> None:
        write_delivery_manifest(
            self.bundle,
            contract={"schema_version": CONTRACT["schema_version"], "digest": "a" * 64},
            policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
        )

    def seal(self, revision_id: str | None, parent: str | None, *, cad: str = "part-1\n", fresh: bool = False) -> None:
        """Write the frozen input/source.json content identity for one revision."""

        inputs = self.bundle / "input"
        inputs.mkdir(parents=True, exist_ok=True)
        (inputs / "M3.0.SLDASM").write_text(cad, encoding="utf-8")
        if fresh:
            (inputs / "source.json").unlink(missing_ok=True)
        identity = {
            "kind": "source-revision",
            "handoff_digest": digest(cad),
            "main_assembly": "M3.0.SLDASM",
            "configuration": "Default",
            "hardware_id": "m3.0",
            "records": [],
        }
        source = {
            "schema_version": SOURCE_RECORD_SCHEMA,
            "source_revision_id": digest(identity),
            "identity": identity,
            "selection": {
                "main_assembly": "M3.0.SLDASM",
                "configuration": "Default",
                "hardware_id": "m3.0",
                "hardware_source": "cad",
            },
            "metadata": {
                "revision": revision_id,
                "owner": "t",
                "control": {"system": "handoff", "reference": "local-handoff"},
                "change_summary": "t",
                "change_summary_source": "submission",
            },
        }
        (inputs / "source.json").write_text(
            json.dumps(source, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.write_manifest()

    def write_bundle(self, evidence: str, extra: dict[str, str] | None = None) -> None:
        files = {
            "input/handoff.json": "{}\n",
            "input/request.json": _request_text(self.purpose),
            "evidence/discovery.json": evidence,
            "model/robot.json": "{}\n",
            "urdf/robot.urdf": "<robot name='m3.0'/>\n",
            "meshes/part.stl": "solid\n",
            "tool.json": "{}\n",
            "reports/quality.json": "{}\n",
        }
        files.update(extra or {})
        for rel, text in files.items():
            path = self.bundle / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        if (self.bundle / "input/source.json").is_file():
            self.write_manifest()

    def remote_head(self, branch: str = BRANCH) -> str:
        out = run("git", "ls-remote", str(self.remote), f"refs/heads/{branch}").strip()
        return out.split()[0] if out else ""

    def remote_tree(self, branch: str = BRANCH) -> list[str]:
        run("git", "fetch", "--quiet", "origin", branch, cwd=self.repo)
        return run("git", "ls-tree", "-r", "--name-only", "FETCH_HEAD", cwd=self.repo).splitlines()

    def is_ancestor(self, older: str, newer: str) -> bool:
        return (
            subprocess.run(
                ["git", "-C", str(self.repo), "merge-base", "--is-ancestor", older, newer], capture_output=True
            ).returncode
            == 0
        )


class PublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()
        self.addCleanup(self.fx.temp.cleanup)
        self.gh_calls: list[tuple[str, ...]] = []
        self.rest_payloads: list[dict] = []
        self.gh_mode = "ok"
        self.branch = BRANCH
        self.prs: dict[str, dict] = {}
        self.duplicate_prs: list[dict] = []
        self.pr_list_calls = 0
        self.qualification_reversed = False

        def fake_gh(repository: Path, *args: str) -> str:
            self.gh_calls.append(args)
            if self.gh_mode == "fail" and not (
                args[:2] == ("pr", "list") and self.pr_list_calls == 0
            ):
                raise subprocess.CalledProcessError(1, ["gh", *args], stderr="gh down")
            if args[:2] == ("pr", "list"):
                self.pr_list_calls += 1
                if self.gh_mode == "ambiguous_after_push":
                    if self.pr_list_calls > 1:
                        return json.dumps(self.duplicate_prs)
                    return "[]"
                if self.duplicate_prs:
                    return json.dumps(self.duplicate_prs)
                entry = self.prs.get(self.branch)
                if entry is not None:
                    return json.dumps(
                        [
                            {
                                "url": entry["url"],
                                "number": entry["number"],
                                "baseRefName": "feature/m3.0",
                                "headRefName": self.branch,
                                "headRefOid": self.fx.remote_head(
                                    self.branch
                                ),
                                "state": "OPEN",
                                "isCrossRepository": False,
                            }
                        ]
                    )
                return "[]"
            if args[:2] == ("pr", "create"):
                entry = self.prs.setdefault(self.branch, {"number": len(self.prs) + 1})
                entry["url"] = f"https://github.com/example/m3.0/pull/{entry['number']}"
                return entry["url"] + "\n"
            if args[0] == "api":
                entry = self.prs[self.branch]
                self.assertEqual(args[:4], ("api", "--method", "PATCH", f"repos/example/m3.0/pulls/{entry['number']}"))
                self.rest_payloads.append(json.loads(Path(args[args.index("--input") + 1]).read_text(encoding="utf-8")))
                if self.gh_mode == "fail_update":
                    raise subprocess.CalledProcessError(1, ["gh", *args], stderr="REST update unavailable")
                return "{}"
            if args[:2] == ("pr", "view"):
                entry = self.prs[self.branch]
                if self.gh_mode == "fail_view":
                    raise subprocess.CalledProcessError(1, ["gh", *args], stderr="temporary view failure")
                return json.dumps(
                    {
                        "url": entry["url"],
                        "state": "OPEN",
                        "baseRefName": "feature/m3.0",
                        "headRefName": self.branch,
                        "headRefOid": "0" * 40 if self.gh_mode == "stale_head" else self.fx.remote_head(self.branch),
                        "isCrossRepository": False,
                    }
                )
            raise AssertionError(args)

        def verifier(bundle: Path) -> dict:
            from description_pipeline.io import read_data
            from description_pipeline.verification import solidworks_urdf as _verification

            checks = [{"id": "x", "status": "passed"}]
            definition = {
                "checks": {},
                "source": {
                    "bodies": [],
                    "joints": [
                        {
                            "name": "arm-a",
                            "type": "revolute",
                            "limit_source": "native",
                            "limits": {"lower": -1.0, "upper": 1.0, "effort": 1.0, "velocity": 1.0},
                        },
                        {
                            "name": "arm-b",
                            "type": "revolute",
                            "limit_source": "native",
                            "limits": {"lower": -1.0, "upper": 1.0},
                        },
                    ],
                    "frames": [],
                },
            }
            read_data(bundle / "input/request.json")
            qualifications = _verification.qualifications(bundle, definition, checks)
            if self.qualification_reversed:
                qualifications = list(reversed(qualifications))
            report = {
                "passed": True,
                "subject_sha256": subject_digest(bundle),
                "checks": checks,
                "qualifications": qualifications,
            }
            (bundle / "reports/quality.json").write_text(urdf_pr._serialize(report), encoding="utf-8")
            return report

        self.patchers = [
            mock.patch.object(urdf_pr, "_gh", fake_gh),
            mock.patch.object(urdf_pr, "_load_verifier", lambda: verifier),
            mock.patch.object(urdf_pr, "_origin_slug", lambda repository: "example/m3.0"),
            mock.patch.object(urdf_pr.time, "sleep"),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in self.patchers])

    def test_pr_actual_head_must_equal_verified_pushed_commit(self):
        self.gh_mode = "stale_head"
        result = self.submit()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "pr_identity_mismatch")
        self.assertEqual(self.fx.remote_head(), result["commit"])
        self.assertTrue(result["detail"]["observed"]["url"])

    def test_failure_after_pr_creation_preserves_url_and_pushed_commit(self):
        self.gh_mode = "fail_view"
        result = self.submit()
        self.assertEqual(result["state"], "gh_failed_after_push")
        self.assertEqual(result["url"], "https://github.com/example/m3.0/pull/1")
        self.assertEqual(result["commit"], self.fx.remote_head())

    def _advance_base(self) -> None:
        (self.fx.seed / "README.md").write_text("independent remote base update\n", encoding="utf-8")
        run("git", "add", "README.md", cwd=self.fx.seed)
        run("git", "commit", "-m", "review external base advance", cwd=self.fx.seed)
        run("git", "push", "origin", "feature/m3.0", cwd=self.fx.seed)

    def test_candidate_head_rewind_is_refused_without_overwriting(self):
        """A rewound candidate head must be a CAS conflict, never an overwrite."""

        first = self.submit()
        self.assertIn(first["state"], {"published", "updated"})
        fixed_base = self.fx.remote_head("feature/m3.0")
        fixed_head = self.fx.remote_head()
        self.fx.write_bundle("rewind race evidence\n")
        real_git = urdf_pr._git
        injected = False

        def git_with_race(repository, *args, **kwargs):
            nonlocal injected
            if not injected and args[:1] == ("push",):
                injected = True
                run(
                    "git",
                    "--git-dir",
                    str(self.fx.remote),
                    "update-ref",
                    f"refs/heads/{BRANCH}",
                    fixed_base,
                    fixed_head,
                )
            return real_git(repository, *args, **kwargs)

        with mock.patch.object(urdf_pr, "_git", side_effect=git_with_race):
            result = self.submit()
        self.assertTrue(injected)
        self.assertEqual(result["state"], "unresolved")
        self.assertEqual(result["error"], "candidate_head_changed")
        self.assertIn("retry", result)
        # The rewound remote head stays exactly where the race left it.
        self.assertEqual(self.fx.remote_head(), fixed_base)

    def test_base_advance_after_planning_ends_unresolved_not_updated(self):
        first = self.submit()
        self.assertIn(first["state"], {"published", "updated"})
        self.fx.write_bundle("base race evidence\n")
        real_git = urdf_pr._git
        injected = False

        def git_with_race(repository, *args, **kwargs):
            nonlocal injected
            if not injected and args[:1] == ("push",):
                injected = True
                self._advance_base()
            return real_git(repository, *args, **kwargs)

        with mock.patch.object(urdf_pr, "_git", side_effect=git_with_race):
            result = self.submit()
        self.assertTrue(injected)
        self.assertEqual(result["state"], "unresolved")
        self.assertEqual(result["error"], "base_moved_after_push")
        self.assertIn("retry", result)
        # The candidate write is retained for reconciliation, never a success.
        self.assertEqual(result["commit"], self.fx.remote_head())

    def test_base_advance_before_final_confirmation_ends_unresolved(self):
        first = self.submit()
        self.assertIn(first["state"], {"published", "updated"})
        self.fx.write_bundle("final confirmation race evidence\n")
        real_gh = urdf_pr._gh
        injected = False

        def gh_with_race(repository, *args):
            nonlocal injected
            if not injected and args[:2] == ("pr", "view"):
                injected = True
                self._advance_base()
            return real_gh(repository, *args)

        with mock.patch.object(urdf_pr, "_gh", side_effect=gh_with_race):
            result = self.submit()
        self.assertTrue(injected)
        self.assertEqual(result["state"], "unresolved")
        self.assertEqual(result["error"], "base_moved_after_push")
        self.assertIn("retry", result)
        self.assertEqual(result["url"], "https://github.com/example/m3.0/pull/1")
        self.assertEqual(result["commit"], self.fx.remote_head())

    def _lost_push_case(self, *, advance_base=False, break_queries=False):
        first = self.submit()
        self.assertIn(first["state"], {"published", "updated"})
        fixed_head = self.fx.remote_head()
        self.fx.write_bundle("lost push response evidence\n")
        real_git = urdf_pr._git
        state = {"injected": False}

        def git_with_race(repository, *args, **kwargs):
            if state["injected"] and break_queries and args[:1] == ("ls-remote",):
                raise subprocess.CalledProcessError(128, ["git", *args], stderr="injected unavailable observation")
            if not state["injected"] and args[:1] == ("push",):
                state["injected"] = True
                accepted = real_git(repository, *args, **kwargs)
                self.assertEqual(accepted.returncode, 0)
                if advance_base:
                    self._advance_base()
                raise subprocess.CalledProcessError(
                    128, ["git", *args], stderr="injected response loss after accepted write"
                )
            return real_git(repository, *args, **kwargs)

        with mock.patch.object(urdf_pr, "_git", side_effect=git_with_race):
            result = self.submit()
        self.assertTrue(state["injected"])
        return result, fixed_head

    def test_lost_push_response_retains_the_written_candidate(self):
        result, fixed_head = self._lost_push_case()
        self.assertEqual(result["state"], "unresolved")
        self.assertEqual(result["error"], "push_response_lost")
        remote = self.fx.remote_head()
        self.assertNotEqual(remote, fixed_head)
        self.assertEqual(result["commit"], remote)
        self.assertIn("retained", result["retry"]["note"])

    def test_lost_push_response_with_base_advance_retains_the_candidate(self):
        result, fixed_head = self._lost_push_case(advance_base=True)
        self.assertEqual(result["state"], "unresolved")
        self.assertEqual(result["error"], "push_response_lost")
        remote = self.fx.remote_head()
        self.assertNotEqual(remote, fixed_head)
        self.assertEqual(result["commit"], remote)
        self.assertEqual(result["detail"]["observed_base"], self.fx.remote_head("feature/m3.0"))

    def test_lost_push_with_unobservable_refs_keeps_the_candidate_unresolved(self):
        result, fixed_head = self._lost_push_case(break_queries=True)
        self.assertEqual(result["state"], "unresolved")
        self.assertEqual(result["error"], "push_unverified")
        self.assertEqual(result["commit"], self.fx.remote_head())
        self.assertNotEqual(result["commit"], fixed_head)

    def submit(self, branch: str | None = None, **kwargs):
        return urdf_pr.submit_bundle(
            self.fx.bundle, self.fx.repo, base="feature/m3.0", branch=branch or self.branch, **kwargs
        )

    def test_repeat_runs_fast_forward_then_noop(self) -> None:
        base = self.fx.remote_head("feature/m3.0")
        first = self.submit()
        self.assertIn(first["state"], {"published", "updated"})
        self.assertEqual(self.fx.remote_head(), first["commit"])
        self.assertTrue(self.fx.is_ancestor(base, first["commit"]))
        self.fx.write_bundle("two\n")
        second = self.submit()
        self.assertEqual(second["state"], "updated")
        self.assertTrue(self.fx.is_ancestor(first["commit"], second["commit"]))
        self.assertEqual(self.fx.remote_head(), second["commit"])
        third = self.submit()
        self.assertEqual(third["state"], "noop")
        self.assertEqual(third["commit"], second["commit"])
        self.assertEqual(self.fx.remote_head(), second["commit"])
        self.assertEqual(len([c for c in self.gh_calls if c[:2] == ("pr", "create")]), 1)
        self.assertGreaterEqual(len(self.rest_payloads), 2)

    def test_extra_reports_receipts_are_refused(self) -> None:
        """Only reports/quality.json may sit outside the subject; old receipts move to run storage."""
        self.fx.write_bundle("one\n", extra={"reports/mass-closure.json": "{}\n"})
        result = self.submit()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "bundle_layout")
        self.assertEqual(self.fx.remote_head(), "")

    def test_only_the_quality_report_travels_outside_the_subject(self) -> None:
        self.fx.write_bundle("one\n")
        result = self.submit()
        self.assertIn(result["state"], {"published", "updated"})
        self.assertEqual(
            sorted(path for path in self.fx.remote_tree() if path.startswith("reports/")), ["reports/quality.json"]
        )
        worktree = Path(tempfile.mkdtemp(dir=self.fx.tmp))
        urdf_pr._copy_governed(self.fx.bundle, worktree)
        copied = sorted(
            path.relative_to(worktree).as_posix() for path in (worktree / "reports").rglob("*") if path.is_file()
        )
        self.assertEqual(copied, ["reports/quality.json"])
        self.assertEqual(subject_digest(worktree), subject_digest(self.fx.bundle))

    def test_missing_quality_report_is_refused(self) -> None:
        (self.fx.bundle / "reports/quality.json").unlink()
        result = self.submit()
        self.assertEqual(result["error"], "bundle_incomplete")
        self.assertEqual(self.fx.remote_head(), "")

    def test_original_record_bytes_survive_git_and_fresh_checkout(self):
        name = "input/records/specification.txt"
        payload = b'\xef\xbb\xbf{"drive": {\r\n  "effort": 12,\r\n  "velocity": 2\r\n}}\r\n'
        path = self.fx.bundle / name
        path.parent.mkdir(parents=True)
        path.write_bytes(payload)
        self.fx.write_manifest()
        for setting in ("true", "false", "input"):
            with self.subTest(core_autocrlf=setting):
                run("git", "config", "core.autocrlf", setting, cwd=self.fx.repo)
                before = subject_digest(self.fx.bundle)
                result = self.submit()
                self.assertIn(result["state"], {"published", "noop"})
                self.assertEqual(result["subject"], before)
                run("git", "fetch", "--quiet", "origin", BRANCH, cwd=self.fx.repo)
                committed = subprocess.check_output(["git", "show", f"FETCH_HEAD:{name}"], cwd=self.fx.repo)
                self.assertEqual(committed, payload)
                clone = self.fx.tmp / f"checkout-{setting}"
                run(
                    "git",
                    "clone",
                    "-q",
                    "-c",
                    f"core.autocrlf={setting}",
                    "--branch",
                    BRANCH,
                    str(self.fx.remote),
                    str(clone),
                )
                self.assertEqual((clone / name).read_bytes(), payload)
                self.assertEqual(subject_digest(clone), before)
                self.assertEqual((clone / ".gitattributes").read_bytes(), urdf_pr.GIT_ATTRIBUTES)

    def test_global_filters_and_encoding_do_not_transform_delivery(self):
        attributes = self.fx.tmp / "global-attributes"
        attributes.write_text("input/** text filter=must_not_run working-tree-encoding=UTF-16\n", encoding="ascii")
        run("git", "config", "core.attributesfile", str(attributes), cwd=self.fx.repo)
        run("git", "config", "filter.must_not_run.clean", "description-filter-must-not-run", cwd=self.fx.repo)
        run("git", "config", "filter.must_not_run.required", "true", cwd=self.fx.repo)
        result = self.submit()
        self.assertEqual(result["state"], "published")
        self.assertEqual(self.fx.remote_head(), result["commit"])

    def test_deeper_attributes_block_before_commit_or_push(self):
        self.fx.write_bundle("one\n", extra={"input/.gitattributes": "* text=auto\n"})
        result = self.submit()
        self.assertEqual(result["error"], "git_attributes_conflict")
        self.assertEqual(result["detail"]["attribute"], "text")
        self.assertTrue(result["detail"]["file"].startswith("input/"))
        self.assertEqual(self.fx.remote_head(), "")

    def test_info_attribute_overrides_block_before_commit_or_push(self):
        attributes = self.fx.repo / ".git/info/attributes"
        for rule in ("text=auto", "filter=external", "working-tree-encoding=UTF-16"):
            with self.subTest(attribute=rule):
                attributes.write_text(f"tool.json {rule}\n", encoding="ascii")
                result = self.submit()
                self.assertEqual(result["error"], "git_attributes_conflict")
                self.assertEqual(result["detail"]["file"], "tool.json")
                self.assertEqual(result["detail"]["attribute"], rule.split("=", 1)[0])
                self.assertEqual(self.fx.remote_head(), "")

    def test_bundle_cannot_carry_its_own_git_policy(self):
        self.fx.write_bundle("one\n", extra={".gitattributes": "* text\n"})
        result = self.submit()
        # The generated policy is publisher-owned metadata: the bundle can neither
        # carry nor replace it, and unregistered files are refused outright.
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "bundle_layout")
        self.assertEqual(self.fx.remote_head(), "")

    def test_native_filenames_with_unicode_and_spaces_publish_unchanged(self):
        name = "input/结构 零件.SLDPRT"
        self.fx.write_bundle("one\n", extra={name: "neutral engineering fixture\n"})
        self.fx.seal("r1", None, fresh=True)
        result = self.submit()
        self.assertEqual(result["state"], "published")
        run("git", "fetch", "--quiet", "origin", BRANCH, cwd=self.fx.repo)
        actual = subprocess.check_output(["git", "show", f"FETCH_HEAD:{name}"], cwd=self.fx.repo)
        self.assertEqual(actual, (self.fx.bundle / name).read_bytes())

    def test_nonregular_git_policy_does_not_write_outside_worktree(self):
        outside = self.fx.tmp / "outside-policy"
        outside.write_bytes(b"original\n")
        worktree = self.fx.tmp / "policy-symlink"
        worktree.mkdir()
        try:
            (worktree / ".gitattributes").symlink_to(outside)
        except OSError:
            self.skipTest("symlink creation requires platform privileges")
        with self.assertRaises(urdf_pr.PrError) as caught:
            urdf_pr._preserve_git_bytes(worktree)
        self.assertEqual(caught.exception.code, "git_metadata_nonregular")
        self.assertEqual(outside.read_bytes(), b"original\n")

    def test_committed_policy_tampering_still_blocks_push(self):
        hook = self.fx.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nprintf '* text\\n' > .gitattributes\ngit add .gitattributes\n", encoding="ascii")
        hook.chmod(0o755)
        result = self.submit()
        self.assertIn(result["error"], {"committed_git_metadata_mismatch", "commit_left_dirty"})
        self.assertEqual(self.fx.remote_head(), "")

    def test_existing_pr_update_preserves_utf8_metadata(self):
        self.submit()
        message = 'Update left wrist — 左腕 "$literal" `text`\nDetailed review'
        result = self.submit(message=message)
        self.assertEqual(result["state"], "noop")
        self.assertEqual(set(self.rest_payloads[-1]), {"title", "body"})
        self.assertEqual(self.rest_payloads[-1]["title"], message.splitlines()[0])
        self.assertIn(f"verified commit: `{result['commit']}`", self.rest_payloads[-1]["body"])
        self.assertIn(f"subject: `{result['subject']}`", self.rest_payloads[-1]["body"])
        body = self.rest_payloads[-1]["body"]
        self.assertIn("- purpose: `engineering`", body)
        self.assertIn("`drive_sizing`[`established`]=1", body)
        self.assertIn("`drive_sizing`[`unqualified`]=1", body)
        self.assertIn("`machine_qualification`[`unqualified`]=1", body)
        run("git", "fetch", "--quiet", "origin", BRANCH, cwd=self.fx.repo)
        committed = subprocess.run(
            ["git", "-C", str(self.fx.repo), "show", f"{result['commit']}:reports/quality.json"],
            check=True,
            capture_output=True,
        ).stdout
        committed_report = json.loads(committed)
        qualification_total = len(committed_report["qualifications"])
        self.assertIn(
            f"qualification object rows: total={qualification_total}; shown=0; omitted={qualification_total}",
            body,
        )
        self.assertIn(
            f"https://github.com/example/m3.0/blob/{result['commit']}/reports/quality.json",
            body,
        )
        self.assertIn(f"`sha256:{hashlib.sha256(committed).hexdigest()}`", body)
        self.assertEqual(result["purpose"], "engineering")
        self.assertFalse(any(call[:2] == ("pr", "edit") for call in self.gh_calls))

    def test_failed_existing_pr_update_preserves_url_and_pushed_commit(self):
        first = self.submit()
        self.gh_mode = "fail_update"
        result = self.submit()
        self.assertEqual(result["state"], "gh_failed_after_push")
        self.assertEqual(result["url"], first["url"])
        self.assertEqual(result["commit"], self.fx.remote_head())

    def test_base_advance_merges_preserving_ancestry(self) -> None:
        first = self.submit()
        (self.fx.seed / "feature.txt").write_text("feature\n", encoding="utf-8")
        run("git", "add", "-A", cwd=self.fx.seed)
        run("git", "commit", "-m", "advance base", cwd=self.fx.seed)
        run("git", "push", "origin", "feature/m3.0", cwd=self.fx.seed)
        run("git", "fetch", "--quiet", "origin", "feature/m3.0", cwd=self.fx.repo)
        new_base = run("git", "rev-parse", "FETCH_HEAD", cwd=self.fx.repo).strip()
        self.fx.write_bundle("three\n")
        second = self.submit()
        self.assertTrue(self.fx.is_ancestor(first["commit"], second["commit"]))
        self.assertTrue(self.fx.is_ancestor(new_base, second["commit"]))

    def test_dirty_checkout_preserved(self) -> None:
        user_file = self.fx.repo / "user-notes.txt"
        user_file.write_text("mine\n", encoding="utf-8")
        result = self.submit()
        self.assertEqual(result["error"], "dirty_repository")
        self.assertTrue(user_file.is_file())
        self.assertEqual(self.fx.remote_head(), "")

    def test_foreign_branch_refused_without_overwrite(self) -> None:
        (self.fx.seed / "foreign.txt").write_text("foreign\n", encoding="utf-8")
        run("git", "add", "-A", cwd=self.fx.seed)
        run("git", "commit", "-m", "foreign branch", cwd=self.fx.seed)
        run("git", "push", "origin", "HEAD:refs/heads/" + BRANCH, cwd=self.fx.seed)
        foreign = self.fx.remote_head()
        result = self.submit()
        self.assertEqual(result["error"], "branch_foreign")
        self.assertEqual(self.fx.remote_head(), foreign)

    def _duplicate_prs(self) -> list[dict]:
        return [
            {
                "url": f"https://github.com/example/m3.0/pull/{number}",
                "number": number,
                "baseRefName": "feature/m3.0",
                "headRefName": BRANCH,
                "headRefOid": character * 40,
                "state": "OPEN",
                "isCrossRepository": False,
            }
            for number, character in ((9, "c"), (10, "d"))
        ]

    def test_ambiguous_prs_refuse_before_any_remote_write(self) -> None:
        self.duplicate_prs = self._duplicate_prs()

        result = self.submit()

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "pr_ambiguous_before_write")
        self.assertEqual(self.fx.remote_head(), "")
        self.assertFalse(
            any(
                args[:2] in {("pr", "create"), ("api", "--method")}
                for args in self.gh_calls
            )
        )
        self.assertEqual(
            {item["number"] for item in result["detail"]},
            {9, 10},
        )

    def test_ambiguous_prs_after_push_are_unresolved_with_candidate(
        self,
    ) -> None:
        self.gh_mode = "ambiguous_after_push"
        self.duplicate_prs = self._duplicate_prs()

        result = self.submit()

        self.assertEqual(result["state"], "unresolved")
        self.assertEqual(result["error"], "pr_ambiguous_after_push")
        self.assertEqual(result["commit"], self.fx.remote_head())
        self.assertEqual(
            {item["number"] for item in result["detail"]},
            {9, 10},
        )
        self.assertIn("retry", result)
        self.assertFalse(
            any(args[:2] == ("pr", "create") for args in self.gh_calls)
        )

    def test_bad_verification_and_tamper_never_push(self) -> None:
        with mock.patch.object(urdf_pr, "_load_verifier", lambda: lambda bundle: {"passed": False}):
            self.assertEqual(self.submit()["error"], "verification_failed")
        self.assertEqual(self.fx.remote_head(), "")
        with mock.patch.object(
            urdf_pr, "_load_verifier", lambda: lambda bundle: {"passed": True, "subject_sha256": "0" * 64}
        ):
            self.assertEqual(self.submit()["error"], "verification_subject_mismatch")
        self.assertEqual(self.fx.remote_head(), "")
        good = {"passed": True, "subject_sha256": subject_digest(self.fx.bundle)}
        (self.fx.bundle / "reports/quality.json").write_text('{"passed": true}\n', encoding="utf-8")
        with mock.patch.object(urdf_pr, "_load_verifier", lambda: lambda bundle: good):
            self.assertEqual(self.submit()["error"], "stale_quality_report")
        self.assertEqual(self.fx.remote_head(), "")

    def test_gh_failure_after_push_keeps_pushed_receipt(self) -> None:
        self.gh_mode = "fail"
        result = self.submit()
        self.assertEqual(result["state"], "gh_failed_after_push")
        self.assertEqual(result["commit"], self.fx.remote_head())
        self.assertEqual(result["branch"], BRANCH)
        self.assertTrue(result["subject"])
        self.assertIn("retry", result)

    def test_unregistered_files_are_refused_not_smuggled(self) -> None:
        self.fx.write_bundle("four\n", extra={"secret.txt": "shh\n"})
        result = self.submit()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "bundle_layout")
        self.assertEqual(self.fx.remote_head(), "")

    def test_local_receipts_are_refused_under_reports(self) -> None:
        self.fx.write_bundle("one\n", extra={"reports/rogue.json": '{"run": 1}\n'})
        result = self.submit()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "bundle_layout")
        self.assertEqual(self.fx.remote_head(), "")

    def test_base_advance_with_identical_bundle_pushes_merge(self) -> None:
        first = self.submit()
        (self.fx.seed / "feature.txt").write_text("feature\n", encoding="utf-8")
        run("git", "add", "-A", cwd=self.fx.seed)
        run("git", "commit", "-m", "advance base", cwd=self.fx.seed)
        run("git", "push", "origin", "feature/m3.0", cwd=self.fx.seed)
        run("git", "fetch", "--quiet", "origin", "feature/m3.0", cwd=self.fx.repo)
        new_base = run("git", "rev-parse", "FETCH_HEAD", cwd=self.fx.repo).strip()
        second = self.submit()
        self.assertEqual(second["state"], "updated")
        self.assertNotEqual(second["commit"], first["commit"])
        self.assertEqual(self.fx.remote_head(), second["commit"])
        self.assertTrue(self.fx.is_ancestor(first["commit"], second["commit"]))
        self.assertTrue(self.fx.is_ancestor(new_base, second["commit"]))

    def test_invalid_refs_rejected(self) -> None:
        result = urdf_pr.submit_bundle(self.fx.bundle, self.fx.repo, base="--upload-pack=evil", branch=BRANCH)
        self.assertEqual(result["error"], "invalid_ref")
        self.assertEqual(self.fx.remote_head(), "")

    def test_case_duplicate_paths_rejected(self) -> None:
        (self.fx.bundle / "Tool.json").write_text("dup\n", encoding="utf-8")
        if (self.fx.bundle / "Tool.json").samefile(self.fx.bundle / "tool.json"):
            self.skipTest("Case-distinct files require a case-sensitive filesystem")
        self.assertEqual(self.submit()["error"], "duplicate_path")
        self.assertEqual(self.fx.remote_head(), "")

    def test_inherited_symlink_not_followed(self) -> None:
        outside = self.fx.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
        try:
            os.symlink(outside, self.fx.seed / "input", target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation requires platform privileges")
        run("git", "add", "-A", cwd=self.fx.seed)
        run("git", "commit", "-m", "symlinked input", cwd=self.fx.seed)
        run("git", "push", "origin", "feature/m3.0", cwd=self.fx.seed)
        result = self.submit()
        self.assertIn(result["state"], {"published", "updated"})
        self.assertTrue((outside / "keep.txt").is_file())
        self.assertIn("input/source.json", self.fx.remote_tree())

    def test_commit_hook_tamper_blocks_push(self) -> None:
        hook = self.fx.repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(
            '#!/bin/sh\necho tampered >> "$(git rev-parse --show-toplevel)/urdf/robot.urdf"\n', encoding="utf-8"
        )
        hook.chmod(0o755)
        result = self.submit()
        self.assertEqual(result["state"], "failed")
        self.assertIn(
            result["error"],
            {
                "commit_left_dirty",
                "committed_subject_mismatch",
                "reverification_failed",
                "reverification_binding_mismatch",
            },
        )
        self.assertEqual(self.fx.remote_head(), "")

    def test_same_cad_revision_allows_definition_changes(self) -> None:
        first = self.submit()
        self.fx.write_bundle("two\n")
        second = self.submit()
        self.assertEqual(second["state"], "updated")
        self.assertEqual(self.fx.remote_head(), second["commit"])
        self.assertTrue(self.fx.is_ancestor(first["commit"], second["commit"]))

    def test_same_label_changed_content_is_not_a_rejection(self) -> None:
        # Platform semantics: the CAD label is raw engineering metadata, never a
        # content identity; protection comes from the fixed base/head, the
        # published subject chain and CAS/ff, so a re-sealed label is allowed.
        first = self.submit()
        self.fx.seal("r1", None, cad="part-1-changed\n", fresh=True)
        self.fx.write_bundle("two\n")
        second = self.submit()
        self.assertEqual(second["state"], "updated")
        self.assertEqual(self.fx.remote_head(), second["commit"])
        self.assertTrue(self.fx.is_ancestor(first["commit"], second["commit"]))

    def test_new_revision_label_is_allowed(self) -> None:
        first = self.submit()
        self.fx.seal("r2", "r1", cad="part-2\n", fresh=True)
        self.fx.write_bundle("two\n")
        second = self.submit()
        self.assertEqual(second["state"], "updated")
        run("git", "fetch", "--quiet", "origin", BRANCH, cwd=self.fx.repo)
        source = json.loads(run("git", "show", "FETCH_HEAD:input/source.json", cwd=self.fx.repo))
        self.assertEqual(source["metadata"]["revision"], "r2")
        self.assertTrue(self.fx.is_ancestor(first["commit"], second["commit"]))

    def test_missing_revision_label_publishes_with_content_identity(self) -> None:
        self.submit()
        self.fx.seal(None, None, cad="part-2\n", fresh=True)
        self.fx.write_bundle("two\n")
        second = self.submit()
        self.assertEqual(second["state"], "updated")
        run("git", "fetch", "--quiet", "origin", BRANCH, cwd=self.fx.repo)
        source = json.loads(run("git", "show", "FETCH_HEAD:input/source.json", cwd=self.fx.repo))
        self.assertIsNone(source["metadata"]["revision"])
        previous = urdf_pr.published_revision(self.fx.repo, "m3.0", "engineering")
        self.assertIsNone(previous["revision"])
        self.assertTrue(previous["source_revision_id"])

    def test_published_revision_unknown_is_not_absent(self) -> None:
        # A remote that answers "no such ref" is proven-absent; a transport or
        # read failure is unknown and must not masquerade as absence.
        self.assertIsNone(urdf_pr.published_revision(self.fx.repo, "m3.0", "engineering"))
        failure = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="network is unreachable")
        real_run = subprocess.run

        def fake_run(*args, **kwargs):
            if "fetch" in args[0]:
                return failure
            return real_run(*args, **kwargs)

        with (
            mock.patch.object(urdf_pr.subprocess, "run", side_effect=fake_run),
            self.assertRaises(urdf_pr.PrError) as raised,
        ):
            urdf_pr.published_revision(self.fx.repo, "m3.0", "engineering")
        self.assertEqual(raised.exception.code, "published_revision_unknown")

    def test_w12_probe_1_publisher_accepts_missing_revision(self) -> None:
        """W12-1: a legal source with revision=null plus a re-sealed manifest is accepted."""
        source = read_source_record(self.fx.bundle)
        source["metadata"]["revision"] = None
        (self.fx.bundle / "input/source.json").write_text(json.dumps(source), encoding="utf-8")
        self.fx.write_manifest()
        hardware, _purpose, _, current = urdf_pr._validate(self.fx.bundle)
        self.assertEqual(hardware, "m3.0")
        self.assertIsNone(current["revision"])
        self.assertIn(self.submit()["state"], {"published", "updated"})

    def test_w12_probe_2_previous_revision_keeps_content_identity_without_label(self) -> None:
        """W12-2: the platform content identity survives a missing CAD label."""
        source = read_source_record(self.fx.bundle)
        source["metadata"]["revision"] = None
        (self.fx.bundle / "input/source.json").write_text(json.dumps(source), encoding="utf-8")
        self.fx.write_manifest()
        previous = urdf_pr._previous_revision(self.fx.bundle, "m3.0")
        self.assertIsNotNone(previous)
        self.assertIsNone(previous["revision"])
        self.assertEqual(previous["source_revision_id"], source["source_revision_id"])

    def test_w12_probe_3_same_label_new_content_is_allowed(self) -> None:
        """W12-3: successor judgement is platform-level, not label-vs-content."""
        before = urdf_pr._identity_of(read_source_record(self.fx.bundle))
        after = dict(before, source_revision_id="d" * 64)
        urdf_pr._check_successor(before, before)
        urdf_pr._check_successor(before, after)
        with self.assertRaises(urdf_pr.PrError):
            urdf_pr._check_successor(before, dict(after, hardware_id="other"))

    def test_w12_probe_4_transport_failure_never_reads_old_fetch_head(self) -> None:
        """W12-4: a failed fetch must not surface a stale FETCH_HEAD identity."""
        self.submit()
        stale = urdf_pr.published_revision(self.fx.repo, "m3.0", "engineering")
        self.assertIsNotNone(stale)
        failure = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="review injected transport failure"
        )
        real_run = subprocess.run

        def fail_fetch_only(args, *positional, **kwargs):
            if isinstance(args, list) and args[:2] == ["git", "-C"] and "fetch" in args:
                return failure
            return real_run(args, *positional, **kwargs)

        with (
            mock.patch.object(urdf_pr.subprocess, "run", side_effect=fail_fetch_only),
            self.assertRaises(urdf_pr.PrError) as raised,
        ):
            urdf_pr.published_revision(self.fx.repo, "m3.0", "engineering")
        self.assertEqual(raised.exception.code, "published_revision_unknown")

    def test_w12_probe_5_unavailable_remote_is_not_absent(self) -> None:
        """W12-5: an unreachable origin is unresolved, never a proven absence."""
        run("git", "remote", "set-url", "origin", str(self.fx.tmp / "unavailable.git"), cwd=self.fx.repo)
        with self.assertRaises(urdf_pr.PrError) as raised:
            urdf_pr.published_revision(self.fx.repo, "m3.0", "engineering")
        self.assertEqual(raised.exception.code, "published_revision_unknown")

    def test_missing_frozen_request_refuses_before_any_write(self) -> None:
        (self.fx.bundle / "input/request.json").unlink()
        result = self.submit()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "bundle_incomplete")
        self.assertEqual(self.fx.remote_head(), "")

    def test_an_undeclared_purpose_is_refused(self) -> None:
        record = json.loads(_request_text())
        record["purpose"] = "other"
        (self.fx.bundle / "input/request.json").write_text(json.dumps(record), encoding="utf-8")
        result = self.submit()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "bundle_records_invalid")
        self.assertEqual(self.fx.remote_head(), "")

    def test_a_legacy_hardware_only_branch_is_refused(self) -> None:
        result = self.submit(branch="work/solidworks/m3.0")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "branch_not_deterministic")
        self.assertEqual(self.fx.remote_head(), "")

    def test_dual_purpose_candidates_are_distinct_and_isolated(self) -> None:
        self.assertEqual(urdf_pr.review_branch("m3.0", "engineering"), BRANCH)
        self.assertEqual(urdf_pr.review_branch("m3.0", "inspection_preview"), INSPECTION_BRANCH)
        first = self.submit()
        self.assertIn(first["state"], {"published", "updated"})
        engineering_head = self.fx.remote_head(BRANCH)
        self.fx.purpose = "inspection_preview"
        self.fx.write_bundle("inspection\n")
        self.branch = INSPECTION_BRANCH
        second = self.submit()
        self.assertIn(second["state"], {"published", "updated"})
        self.assertEqual(second["purpose"], "inspection_preview")
        self.assertEqual(self.fx.remote_head(BRANCH), engineering_head)
        inspection_head = self.fx.remote_head(INSPECTION_BRANCH)
        self.assertTrue(inspection_head and inspection_head != engineering_head)
        self.assertNotEqual(self.prs[BRANCH]["number"], self.prs[INSPECTION_BRANCH]["number"])
        third = self.submit()
        self.assertIn(third["state"], {"updated", "noop"})
        self.assertEqual(self.fx.remote_head(BRANCH), engineering_head)
        self.assertEqual(self.fx.remote_head(INSPECTION_BRANCH), third["commit"])
        body = self.rest_payloads[-1]["body"]
        self.assertIn("- purpose: `inspection_preview`", body)
        self.assertIn("`drive_sizing`[`unqualified`]=2", body)
        self.assertIn("`machine_qualification`[`unqualified`]=1", body)
        self.assertNotIn("- drive_sizing[arm-a]", body)

    def test_pr_body_summarizes_qualification_rows_independent_of_order(self) -> None:
        def qualification_summary(body: str) -> str:
            return body[body.index("## Recorded qualifications") :]

        self.qualification_reversed = False
        self.submit()
        self.submit()
        forward = self.rest_payloads[-1]["body"]
        self.assertIn("`drive_sizing`[`established`]=1", forward)
        self.assertIn("`drive_sizing`[`unqualified`]=1", forward)
        self.qualification_reversed = True
        result = self.submit()
        self.assertIn(result["state"], {"updated", "noop"})
        self.assertEqual(
            qualification_summary(self.rest_payloads[-1]["body"]),
            qualification_summary(forward),
        )

    def test_pr_body_summarizes_general_warning_codes_in_stable_safe_order(self) -> None:
        def diagnostic(code: str, object_type: str, object_id: str, field: str) -> dict:
            return {
                "code": code,
                "blocking": False,
                "message": f"{code} recorded",
                "detail": {
                    "objects": [
                        {
                            "object_type": object_type,
                            "object": object_id,
                            "field": field,
                            "original": "<script>alert(1)</script>",
                            "actual": {"value": "<img src=x onerror=alert(1)>"},
                            "source": f"{code}.source",
                            "rule_id": f"{code}.rule",
                            "rule_version": "v1",
                            "reason": "Native value was unavailable; see report.",
                        }
                    ]
                },
            }

        diagnostics = [
            diagnostic("display.material_fallback", "visual", "shell", "material"),
            diagnostic("naming.formatted_interface", "link", "base_link", "name"),
            diagnostic("limits.default_policy", "joint", "waist", "limits"),
        ]
        report = {"checks": [], "qualifications": [], "diagnostics": diagnostics}
        forward = render_body(report, subject="subject", commit="b" * 40)
        report["diagnostics"] = list(reversed(diagnostics))
        reverse = render_body(report, subject="subject", commit="b" * 40)

        def warning_summary(body: str) -> str:
            return body[body.index("## Recorded source warnings") :]

        self.assertEqual(warning_summary(forward), warning_summary(reverse))
        self.assertIn("## Recorded source warnings", forward)
        for code in ("limits.default_policy", "naming.formatted_interface", "display.material_fallback"):
            self.assertIn(f"`{code}` groups=1 rows=1", forward)
        self.assertIn("warning groups: total=3; shown=0; omitted=3", forward)
        self.assertIn("warning object rows: total=3; shown=0; omitted=3", forward)
        self.assertIn("warning codes: total=3; shown=3; omitted=0", forward)
        self.assertNotIn("- general warning ", forward)
        self.assertNotIn("<script>", forward)
        self.assertNotIn("<img ", forward)

        empty = render_body(
            {"checks": [], "qualifications": [], "diagnostics": []},
            subject="subject",
            commit="b" * 40,
        )
        self.assertNotIn("## Recorded source warnings", empty)
        unknown = render_body(
            {
                "checks": [],
                "qualifications": [],
                "diagnostics": [
                    {
                        "code": "quality.unknown",
                        "blocking": False,
                        "detail": {"objects": [{"object": "safe", "unknown": {"nested": True}}]},
                    }
                ],
            },
            subject="subject",
            commit="b" * 40,
        )
        self.assertIn("`quality.unknown` groups=1 rows=1", unknown)
        self.assertNotIn("object=`safe`", unknown)
        self.assertNotIn("nested", unknown)

    def test_pr_body_bounds_large_warning_index_without_silent_loss(self) -> None:
        diagnostics = [
            {
                "code": "limits.default_policy",
                "blocking": False,
                "detail": {
                    "objects": [
                        {
                            "object_type": "joint",
                            "object": f"joint_{index:04d}",
                            "field": "limits",
                            "original": {"lower": 0.0, "upper": 0.0},
                            "actual": {
                                "lower": -3.141592653589793,
                                "upper": 3.141592653589793,
                            },
                            "source": "engineering_rotation_default_window/v1",
                            "rule_id": "engineering_rotation_default_window/v1",
                            "rule_version": "v1",
                            "reason": "Native position limits were unavailable",
                        }
                    ]
                },
            }
            for index in range(2000)
        ]
        report = {"checks": [], "qualifications": [], "diagnostics": diagnostics}
        body = render_body(report)
        self.assertLess(len(body), 20_000)
        self.assertLess(len(body.encode("utf-8")), 20_000)
        self.assertIn("warning groups: total=2000; shown=0; omitted=2000", body)
        self.assertIn("warning object rows: total=2000; shown=0; omitted=2000", body)
        self.assertIn("warning codes: total=1; shown=1; omitted=0", body)
        self.assertIn("`limits.default_policy` groups=2000 rows=2000", body)
        self.assertIn("complete candidate source: [reports/quality.json]", body)
        self.assertIn("Warnings are recorded observations, never passes or publication evidence", body)
        self.assertNotIn("- general warning `limits.default_policy`", body)

        report["diagnostics"] = list(reversed(diagnostics))
        reverse = render_body(report)

        def warning_summary(value: str) -> str:
            return value[value.index("## Recorded source warnings") :]

        self.assertEqual(warning_summary(body), warning_summary(reverse))

    def test_pr_body_bounds_large_qualification_summary_without_silent_loss(self) -> None:
        qualifications = [
            {
                "fact_id": f"quality.fact_{index:04d}",
                "status": "established",
                "object_id": f"object_{index:04d}",
            }
            for index in range(2000)
        ]
        body = render_body(
            {"checks": [], "qualifications": qualifications, "diagnostics": []},
        )
        self.assertLess(len(body), 20_000)
        self.assertLess(len(body.encode("utf-8")), 20_000)
        self.assertIn("qualification object rows: total=2000; shown=0; omitted=2000", body)
        self.assertIn("qualification facts: total=2000; fact/status summaries shown=64; omitted=1936", body)
        self.assertIn("`established`=2000", body)
        self.assertNotIn("[object_0000]", body)

    def test_pr_body_is_bounded_for_representative_m3_quality_shape(self) -> None:
        report = representative_m3_quality_report()
        warning_rows = urdf_pr._general_warning_rows(report)
        warning_diagnostics = urdf_pr._warning_diagnostics(report)
        qualification_rows = report["qualifications"]

        self.assertEqual(len(warning_rows), 512)
        self.assertEqual(len(warning_diagnostics), 38)
        self.assertEqual(
            len({diagnostic["code"] for diagnostic in warning_diagnostics}),
            16,
        )
        self.assertEqual(len(qualification_rows), 191)

        body = render_body(report)
        self.assertLess(len(body), 32_768)
        self.assertLess(len(body.encode("utf-8")), 32_768)
        self.assertIn("warning groups: total=38; shown=0; omitted=38", body)
        self.assertIn("warning object rows: total=512; shown=0; omitted=512", body)
        self.assertIn("warning codes: total=16; shown=16; omitted=0", body)
        self.assertIn("qualification object rows: total=191; shown=0; omitted=191", body)
        self.assertIn(
            "qualification facts: total=35; fact/status summaries shown=35; omitted=0",
            body,
        )
        self.assertIn(
            f"https://github.com/{BODY_SLUG}/blob/{'b' * 40}/reports/quality.json",
            body,
        )
        self.assertIn(f"`sha256:{BODY_SHA256}`", body)
        self.assertEqual(
            body.count(
                f"https://github.com/{BODY_SLUG}/blob/{'b' * 40}/reports/quality.json"
            ),
            1,
        )
        self.assertEqual(body.count("`sha256:"), 1)
        self.assertEqual(body.count("## Recorded source warnings"), 1)
        self.assertEqual(body.count("## Recorded qualifications"), 1)
        self.assertNotIn("- general warning ", body)
        self.assertNotIn("- engineering_position_range[", body)
        self.assertNotIn("direction author sign", body)
        self.assertNotIn("native scope identity", body)


if __name__ == "__main__":
    unittest.main()
