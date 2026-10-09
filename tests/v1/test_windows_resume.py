"""Linked-run endpoint behavior: checkpoint reuse, provenance, refusals, diagnostics."""

from __future__ import annotations

import json
import threading
import time
import unittest
import uuid
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from description_pipeline.delivery import subject_inventory
from description_pipeline.io import PipelineError, digest, file_digest, inventory, read_data, write_json
from description_pipeline.orchestration.windows import Jobs, RequestError, handler
from description_pipeline.runtime import tool_record
from .endpoint_support import EndpointFixture
from .protocol_support import protocol_events


def make_delivery(output: Path, *, quality: bool = True) -> str:
    """One minimal subject-complete delivery; returns its subject digest."""
    (output / "input").mkdir(parents=True)
    (output / "input" / "robot.yaml").write_text("hardware_id: arm\n", encoding="utf-8")
    (output / "evidence").mkdir()
    write_json(output / "evidence" / "scene.json", {"links": [], "joints": []})
    write_json(
        output / "evidence" / "manifest.json",
        {
            "schema_version": "description.source/v1",
            "kind": "solidworks",
            "identity": {"hardware_id": "arm"},
            "evidence_class": "cad",
            "scene": "scene.json",
            "files": inventory(output / "evidence", exclude=("manifest.json",)),
        },
    )
    (output / "model").mkdir()
    (output / "model" / "robot.json").write_text("{}\n", encoding="utf-8")
    (output / "urdf").mkdir()
    (output / "urdf" / "robot.urdf").write_text("<robot/>\n", encoding="utf-8")
    (output / "meshes").mkdir()
    (output / "meshes" / "part.stl").write_bytes(b"solid part")
    (output / "reports").mkdir()
    (output / "README.md").write_text("# arm URDF\n", encoding="utf-8")
    write_json(
        output / "reports" / "input.json",
        {"cad_revision": {"revision": "r1"}, "package_files": inventory(output / "input")},
    )
    write_json(output / "reports" / "tool.json", tool_record())
    subject = digest(subject_inventory(output))
    if quality:
        write_json(
            output / "reports" / "quality.json",
            {"passed": True, "subject_sha256": subject, "checks": [{"id": "source.native_discovery", "passed": True}]},
        )
    return subject


def bound_events(output: Path, subject: str, *, handoff: str | None = None) -> list[dict]:
    """Capture..publish events that bind the written delivery like the real runner does."""
    input_files = inventory(output / "input")
    manifest = read_data(output / "evidence" / "manifest.json")
    manifest_hash = file_digest(output / "evidence" / "manifest.json")
    events = protocol_events(stages=("capture", "generate", "verify", "publish"), subject=subject)
    for event in events:
        check = event.get("check")
        if not isinstance(check, dict):
            continue
        if check.get("id") == "input.valid":
            check["details"] = {
                **(check.get("details") or {}),
                "handoff_sha256": handoff,
                "files_sha256": digest(input_files),
                "files": {"input/" + name: checksum for name, checksum in input_files.items()},
            }
        elif check.get("id") == "capture.integrity":
            evidence = {"evidence/manifest.json": manifest_hash}
            evidence.update({"evidence/" + name: checksum for name, checksum in manifest["files"].items()})
            check["details"] = {**(check.get("details") or {}), "manifest_sha256": manifest_hash, "files": evidence}
    return events


def delivery_result(subject: str, *, on_event=None, output: Path | None = None, handoff: str | None = None) -> dict:
    if on_event is not None:
        events = (
            bound_events(output, subject, handoff=handoff)
            if output is not None
            else protocol_events(stages=("capture", "generate", "verify", "publish"), subject=subject)
        )
        for event in events:
            on_event(event)
    return {
        "passed": True,
        "subject_sha256": subject,
        "quality": {
            "passed": True,
            "subject_sha256": subject,
            "checks": [{"id": "source.native_discovery", "passed": True}],
        },
        "submission": {
            "passed": True,
            "subject_sha256": subject,
            "url": "https://github.com/a/b/pull/1",
            "repository_slug": "a/b",
            "base": "feature/arm",
            "branch": "work/solidworks/arm",
            "state": "published",
            "commit": "b" * 40,
        },
    }


class ResumeEndpointTests(EndpointFixture, unittest.TestCase):
    def await_terminal(self, jobs: Jobs, run_id: str, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = jobs.snapshot(run_id)
            if job["status"] in {"passed", "failed"}:
                return job
            time.sleep(0.01)
        raise AssertionError(f"{run_id} did not finish: {jobs.snapshot(run_id)['status']}")

    def finished_parent(self, *, delivery: bool) -> tuple[Jobs, dict, str | None]:
        subject = {"value": None}

        def runner(package, output, **kwargs):
            if delivery:
                subject["value"] = make_delivery(Path(output))
                return delivery_result(
                    subject["value"],
                    on_event=kwargs["on_event"],
                    output=Path(output),
                    handoff=kwargs.get("handoff_sha256"),
                )
            return delivery_result("a" * 64, on_event=kwargs["on_event"])

        jobs = self.jobs(runner)
        request = self.request()
        _created, is_new = jobs.create(request)
        self.assertTrue(is_new)
        parent = self.await_terminal(jobs, request["run_id"])
        self.assertEqual(parent["status"], "passed", parent.get("error"))
        return jobs, parent, subject["value"]

    @staticmethod
    def linked_request(parent: dict, stage: str) -> dict:
        return {
            "run_id": str(uuid.uuid4()),
            "package": parent["request"]["package"],
            "handoff_sha256": parent["request"]["handoff_sha256"],
            "resume": {"parent_run": parent["run_id"], "from_stage": stage},
        }

    def test_capture_resume_reuses_parent_checkpoints_with_provenance(self) -> None:
        jobs, parent, _ = self.finished_parent(delivery=False)
        jobs.close()
        calls: list[dict] = []

        def runner(package, output, **kwargs):
            calls.append({"package": package, **kwargs})
            result = delivery_result("c" * 64, on_event=kwargs["on_event"])
            result["resume"] = kwargs.get("resume")
            return result

        child_jobs = self.jobs(runner, preparer=lambda *args, **kwargs: self.fail("discovery must not rerun"))
        request = self.linked_request(parent, "capture")
        child, created = child_jobs.create(request)
        self.assertTrue(created)
        inherited = [event for event in child["events"] if event.get("reuse")]
        self.assertTrue(inherited)
        self.assertEqual({event["reuse"]["parent_run"] for event in inherited}, {parent["run_id"]})
        self.assertTrue(all(event["reuse"]["reused"] is True for event in inherited))
        parent_at = {event.get("at") for event in parent["events"]}
        for event in inherited:
            self.assertIn(event.get("at"), parent_at)
        self.assertFalse(any(event.get("reuse") for event in parent["events"]))
        finished = self.await_terminal(child_jobs, request["run_id"])
        self.assertEqual(finished["status"], "passed", finished.get("error"))
        self.assertEqual(finished["result"]["resume"], request["resume"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["resume_from"], "capture")
        self.assertIsNone(calls[0]["seed_dir"])
        self.assertEqual(calls[0]["resume"], request["resume"])
        self.assertEqual(Path(calls[0]["package"]), self.config["state_root"] / "prepared" / parent["run_id"])
        reused_stages = {event["stage"] for event in inherited}
        fresh_stages = {event["stage"] for event in finished["events"] if not event.get("reuse")}
        self.assertEqual(reused_stages, {"freeze", "discover"})
        self.assertFalse(fresh_stages & reused_stages)
        self.assertIn("capture", fresh_stages)

    def test_verify_resume_probes_the_delivery_and_refuses_a_changed_target(self) -> None:
        jobs, parent, subject = self.finished_parent(delivery=True)
        self.assertTrue(subject)
        jobs.config["targets"]["arm"]["base"] = "feature/retargeted"
        refused = self.linked_request(parent, "verify")
        with self.assertRaises(RequestError) as caught:
            jobs.create(refused)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.payload["reason"], "target_changed")
        self.assertEqual(caught.exception.payload["earliest_required"], "discover")
        rows = {row["stage"]: row for row in jobs.plan(parent["run_id"])["stage_reruns"]}
        self.assertTrue(rows["publish"]["target_changed"])
        self.assertFalse(rows["verify"]["eligible"])
        self.assertTrue(rows["discover"]["eligible"])
        jobs.config["targets"]["arm"]["base"] = "feature/arm"
        jobs.close()
        calls: list[dict] = []

        def runner(package, output, **kwargs):
            calls.append({"package": package, **kwargs})
            result = delivery_result(subject, on_event=kwargs["on_event"])
            result["resume"] = kwargs.get("resume")
            return result

        child_jobs = self.jobs(runner, preparer=lambda *args, **kwargs: self.fail("discovery must not rerun"))
        accepted = self.linked_request(parent, "verify")
        child_jobs.create(accepted)
        finished = self.await_terminal(child_jobs, accepted["run_id"])
        self.assertEqual(finished["status"], "passed", finished.get("error"))
        self.assertEqual(calls[0]["resume_from"], "verify")
        self.assertEqual(Path(calls[0]["seed_dir"]), self.config["output_root"] / parent["run_id"])
        self.assertEqual(calls[0]["expected_subject"], subject)

    def test_dependency_change_blocks_reuse_only_and_earliest_is_eligible(self) -> None:
        registry = self.root / "frozen-names.json"
        write_json(registry, {})
        jobs, parent, _ = self.finished_parent(delivery=False)
        jobs.config["discovery"] = {"record_roots": [], "frozen_names_file": registry}
        # Re-run the parent under the registry so its dependency snapshot is recorded.
        request = self.request()
        jobs.create(request)
        parent = self.await_terminal(jobs, request["run_id"])
        write_json(registry, {"arm": "base_link"})
        rows = {row["stage"]: row for row in jobs.plan(parent["run_id"])["stage_reruns"]}
        for stage in ("capture", "generate", "verify", "publish"):
            self.assertFalse(rows[stage]["eligible"], stage)
            self.assertEqual(rows[stage]["reason"], "dependency_changed")
            self.assertEqual(rows[stage]["prerequisites"]["earliest_required"], "discover")
        self.assertTrue(rows["discover"]["eligible"])
        with self.assertRaises(RequestError) as caught:
            jobs.create(self.linked_request(parent, "capture"))
        self.assertEqual(caught.exception.payload["reason"], "dependency_changed")
        self.assertEqual(caught.exception.payload["earliest_required"], "discover")
        accepted = self.linked_request(parent, "discover")
        child, created = jobs.create(accepted)
        self.assertTrue(created)
        self.assertFalse(any(event.get("reuse") for event in child["events"] if event["stage"] == "discover"))
        finished = self.await_terminal(jobs, accepted["run_id"])
        self.assertEqual(finished["status"], "passed", finished.get("error"))

    def test_second_linked_job_is_refused_while_one_is_active(self) -> None:
        jobs, parent, _ = self.finished_parent(delivery=False)
        jobs.close()
        started, release = threading.Event(), threading.Event()

        def runner(package, output, **kwargs):
            if kwargs.get("resume") is not None:
                started.set()
                if not release.wait(10):
                    raise PipelineError("release timeout")
            return delivery_result("c" * 64, on_event=kwargs["on_event"])

        child_jobs = self.jobs(runner)
        first = self.linked_request(parent, "capture")
        child_jobs.create(first)
        self.assertTrue(started.wait(5))
        second = self.linked_request(parent, "capture")
        with self.assertRaises(RequestError) as caught:
            child_jobs.create(second)
        self.assertEqual(caught.exception.payload["reason"], "already_active")
        self.assertEqual(caught.exception.payload["active_run_id"], first["run_id"])
        release.set()
        self.await_terminal(child_jobs, first["run_id"])
        third = self.linked_request(parent, "capture")
        _, created = child_jobs.create(third)
        self.assertTrue(created)
        self.await_terminal(child_jobs, third["run_id"])

    def test_failed_linked_job_preserves_the_cad_error_code(self) -> None:
        class CadFailure(PipelineError):
            code = "main_selection_ambiguous"

        jobs, parent, _ = self.finished_parent(delivery=False)
        jobs.close()

        def runner(package, output, **kwargs):
            raise CadFailure("saved main assembly is ambiguous")

        child_jobs = self.jobs(runner)
        request = self.linked_request(parent, "capture")
        child_jobs.create(request)
        finished = self.await_terminal(child_jobs, request["run_id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_code"], "main_selection_ambiguous")
        self.assertIn("ambiguous", finished["error"])

    def test_unrecorded_tool_identity_allows_only_a_freeze_restart(self) -> None:
        jobs, parent, _ = self.finished_parent(delivery=False)
        jobs.jobs[parent["run_id"]].pop("tool")
        rows = {row["stage"]: row for row in jobs.plan(parent["run_id"])["stage_reruns"]}
        for stage in ("discover", "capture", "generate", "verify", "publish"):
            self.assertFalse(rows[stage]["eligible"], stage)
            self.assertEqual(rows[stage]["reason"], "tool_changed")
            self.assertEqual(rows[stage]["prerequisites"]["earliest_required"], "freeze")
        self.assertTrue(rows["freeze"]["eligible"])
        with self.assertRaises(RequestError) as caught:
            jobs.create(self.linked_request(parent, "capture"))
        self.assertEqual(caught.exception.payload["reason"], "tool_changed")
        self.assertEqual(caught.exception.payload["earliest_required"], "freeze")
        restart = self.linked_request(parent, "freeze")
        _, created = jobs.create(restart)
        self.assertTrue(created)
        self.assertEqual(self.await_terminal(jobs, restart["run_id"])["status"], "passed")

    def test_corrupted_evidence_is_not_reusable_for_generation(self) -> None:
        jobs, parent, _ = self.finished_parent(delivery=True)
        scene = self.config["output_root"] / parent["run_id"] / "evidence" / "scene.json"
        scene.write_text('{"links": [{"tampered": true}], "joints": []}\n', encoding="utf-8")
        rows = {row["stage"]: row for row in jobs.plan(parent["run_id"])["stage_reruns"]}
        self.assertEqual(rows["capture"]["prerequisites"]["earliest_required"], None)
        self.assertTrue(rows["capture"]["eligible"])
        self.assertFalse(rows["generate"]["eligible"])
        self.assertEqual(rows["generate"]["reason"], "prerequisite_invalid")
        self.assertEqual(rows["generate"]["prerequisites"]["earliest_required"], "capture")
        with self.assertRaises(RequestError) as caught:
            jobs.create(self.linked_request(parent, "verify"))
        self.assertEqual(caught.exception.payload["reason"], "prerequisite_invalid")
        self.assertEqual(caught.exception.payload["earliest_required"], "capture")

    def test_chained_reruns_track_the_ancestral_prepared_checkpoint(self) -> None:
        jobs, parent, parent_subject = self.finished_parent(delivery=True)
        prepared = Path(jobs.snapshot(parent["run_id"])["prepared_dir"])
        calls: list[tuple[Path, str | None]] = []

        def chain_runner(package, output, **kwargs):
            calls.append((Path(package), kwargs.get("resume_from")))
            subject = make_delivery(Path(output))
            result = delivery_result(
                subject,
                on_event=kwargs["on_event"],
                output=Path(output),
                handoff=kwargs.get("handoff_sha256"),
            )
            result["resume"] = kwargs.get("resume")
            return result

        jobs.close()
        child_jobs = self.jobs(chain_runner, preparer=lambda *args, **kwargs: self.fail("discovery must not rerun"))
        child = self.linked_request(parent, "capture")
        child_jobs.create(child)
        finished_child = self.await_terminal(child_jobs, child["run_id"])
        self.assertEqual(finished_child["status"], "passed", finished_child.get("error"))
        self.assertTrue(parent_subject)
        grandchild = self.linked_request(finished_child, "generate")
        child_jobs.close()
        grandchild_jobs = self.jobs(
            chain_runner, preparer=lambda *args, **kwargs: self.fail("discovery must not rerun")
        )
        grandchild_jobs.create(grandchild)
        finished = self.await_terminal(grandchild_jobs, grandchild["run_id"])
        self.assertEqual(finished["status"], "passed", finished.get("error"))
        self.assertEqual(finished["prepared_dir"], str(prepared))
        self.assertEqual(calls, [(prepared, "capture"), (prepared, "generate")])
        inherited = [event for event in finished["events"] if event.get("reuse")]
        self.assertTrue(inherited)
        self.assertEqual({event["reuse"]["parent_run"] for event in inherited}, {finished_child["run_id"]})
        producer = {event["stage"]: event["reuse"]["source_run"] for event in inherited}
        self.assertEqual(producer["freeze"], parent["run_id"])
        self.assertEqual(producer["discover"], parent["run_id"])
        self.assertEqual(producer["capture"], finished_child["run_id"])

    def test_stale_enqueue_state_is_reprobed_before_reuse(self) -> None:
        registry = self.root / "frozen-names.json"
        write_json(registry, {})
        self.config["discovery"] = {"record_roots": [], "frozen_names_file": registry}
        jobs, parent, _ = self.finished_parent(delivery=False)
        hold_started, hold_release = threading.Event(), threading.Event()

        def runner(package, output, **kwargs):
            if kwargs.get("resume") is None:
                hold_started.set()
                if not hold_release.wait(10):
                    raise PipelineError("release timeout")
            return delivery_result("a" * 64, on_event=kwargs["on_event"])

        jobs.close()
        child_jobs = self.jobs(runner)
        holder = self.request()
        child_jobs.create(holder)
        self.assertTrue(hold_started.wait(5))
        child = self.linked_request(parent, "capture")
        child_jobs.create(child)
        write_json(registry, {"arm": "base_link"})
        hold_release.set()
        finished = self.await_terminal(child_jobs, child["run_id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_code"], "dependency_changed")
        self.assertTrue(finished["error"])
        self.assertEqual(self.await_terminal(child_jobs, holder["run_id"])["status"], "passed")

    def test_reruns_route_serves_the_plan_and_refusals_carry_reason_fields(self) -> None:
        registry = self.root / "frozen-names.json"
        write_json(registry, {"arm": "base_link"})
        jobs, parent, _ = self.finished_parent(delivery=False)
        jobs.config["discovery"] = {"record_roots": [], "frozen_names_file": registry}
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(jobs, self.config["token"]))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        headers = {"Authorization": "Bearer " + self.config["token"], "Content-Type": "application/json"}
        with urllib.request.urlopen(
            urllib.request.Request(base + f"/v1/jobs/{parent['run_id']}/reruns", headers=headers), timeout=5
        ) as response:
            plan = json.loads(response.read())
        self.assertEqual(plan["run_id"], parent["run_id"])
        self.assertEqual(
            [row["stage"] for row in plan["stage_reruns"]],
            ["freeze", "discover", "capture", "generate", "verify", "publish"],
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                urllib.request.Request(
                    base + "/v1/jobs",
                    data=json.dumps(self.linked_request(parent, "capture")).encode(),
                    headers=headers,
                    method="POST",
                ),
                timeout=5,
            )
        self.assertEqual(caught.exception.code, 409)
        body = json.loads(caught.exception.read())
        self.assertEqual(body["reason"], "dependency_changed")
        self.assertTrue(body["reason_zh"])
        self.assertEqual(body["earliest_required"], "discover")


if __name__ == "__main__":
    unittest.main()
