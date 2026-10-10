"""Endpoint contract: staged selection rounds seal before any native discovery.

Written against the integration contract (``Jobs`` with ``selection_reader_factory``
and ``native_reader`` plus the ``/v1/jobs/<id>/(files|seal)`` routes).  The
selection worker itself belongs to root's endpoint wiring, so the loop-driven
tests fail (fast, with the actual error) until that wiring lands; the
synchronous refusal and restart tests do not depend on it.

No CAD, no Windows and no network beyond an ephemeral loopback server.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import unittest
import uuid
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from description_pipeline.io import digest, write_json
from description_pipeline.orchestration.windows import Jobs, RequestError, handler, read_config
from description_pipeline.sources.solidworks.selection import SelectionError, seal_selection

MAIN = "assembly.SLDASM"
FRAME = "parts/frame.SLDASM"
LINK = "parts/link.SLDPRT"
TOOL_SHA = "f" * 64
SENTINEL = "native discovery must not run during selection"


class SentinelNativeReader:
    """Selection-only tests: reaching discovery is a contract violation sentinel.

    The stated contract is ``read_native_record(stage, settings, on_activity=...)``;
    the endpoint snapshot under test currently calls ``native_reader(frozen,
    settings, on_activity=...)``, so this fake supports both shapes until the
    integration settles (reported as an API mismatch).
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def read_native_record(self, stage, settings, on_activity=None):
        self.calls.append({"stage": str(stage), "settings": settings})
        raise RuntimeError(SENTINEL)

    def __call__(self, stage, settings, on_activity=None):
        return self.read_native_record(stage, settings, on_activity=on_activity)


class FakeSelectionReader:
    """Same-thread closed-query reader: exactly the pending names, nothing else."""

    def __init__(self, documents: dict[str, list[str]], log: list, refuse: bool = False):
        self.documents = documents
        self.log = log
        self.refuse = refuse

    def __enter__(self):
        self.log.append(("enter", threading.get_ident()))
        return self

    def __exit__(self, *exc_info):
        self.log.append(("exit", threading.get_ident()))
        return False

    def observe(self, stage, pending, upload_files):
        self.log.append(("observe", threading.get_ident(), Path(stage).name, tuple(sorted(pending))))
        if self.refuse:
            raise SelectionError(
                "selection_dependencies_refused",
                "Stored references cannot be admitted into the staged selection",
                problems=[
                    {
                        "document": MAIN,
                        "name": "link",
                        "location": "C:\\elsewhere\\link.SLDPRT",
                        "problem": "outside_stage",
                    }
                ],
                count=1,
            )
        return {
            name: {"sha256": upload_files[name], "references": sorted(self.documents.get(name, []))}
            for name in pending
        }


class SelectionEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.packages = self.root / "packages"
        self.engineering = self.root / "engineering"
        for path in (self.packages, self.engineering, self.root / "outputs", self.root / "state"):
            path.mkdir(parents=True)
        token = self.root / "token"
        token.write_text("t" * 64, encoding="utf-8")
        config_path = self.root / "endpoint.json"
        write_json(
            config_path,
            {
                "schema_version": "solidworks-to-urdf.endpoint/v1",
                "package_root": str(self.packages),
                "output_root": str(self.root / "outputs"),
                "state_root": str(self.root / "state"),
                "token_file": str(token),
                "targets": {"arm": {"repository_slug": "a/b", "base": "feature/arm"}},
            },
        )
        self.config = read_config(config_path)
        identity = patch(
            "description_pipeline.orchestration.windows._native_tool_record",
            return_value={
                "name": "native-selection-control",
                "version": "test",
                "source_sha256": TOOL_SHA,
                "runtime": {"role": "native", "python": "3.12.10", "packages": {}},
            },
        )
        identity.start()
        self.addCleanup(identity.stop)

        self.data = {
            MAIN: b"synthetic assembly bytes",
            FRAME: b"synthetic frame bytes",
            LINK: b"synthetic link bytes",
        }
        self.documents = {MAIN: [FRAME], FRAME: [LINK], LINK: []}
        self.upload_files = {name: hashlib.sha256(value).hexdigest() for name, value in self.data.items()}
        self.upload_sha = digest(self.upload_files)
        self.selected_files = {name: self.upload_files[name] for name in self.documents}
        self.reader_log: list = []
        self.sentinel = SentinelNativeReader()
        self.runner_calls: list = []
        self.refusing = False
        self.jobs = self.new_jobs()

    def new_jobs(self) -> Jobs:
        previous = getattr(self, "_current_jobs", None)
        if previous is not None:
            previous.close()

        def runner(*args, **kwargs):
            self.runner_calls.append(kwargs.get("run_id"))
            return {"passed": False, "error": "The selection-only contract must not reach the runner"}

        jobs = Jobs(
            self.config,
            runner=runner,
            selection_reader_factory=lambda: FakeSelectionReader(
                self.documents, self.reader_log, refuse=self.refusing
            ),
            native_reader=self.sentinel,
        )
        self.addCleanup(jobs.close)
        self._current_jobs = jobs
        return jobs

    # -- helpers -------------------------------------------------------------

    def request(self, run_id: str) -> dict:
        return {
            "run_id": run_id,
            "upload_files": self.upload_files,
            "upload_sha256": self.upload_sha,
            "main_assembly": MAIN,
        }

    def create(self, jobs: Jobs | None = None, run_id: str | None = None) -> str:
        identifier = run_id or str(uuid.uuid4())
        snapshot, created = (jobs or self.jobs).create(self.request(identifier))
        self.assertTrue(created)
        self.assertEqual(snapshot["run_id"], identifier)
        return identifier

    def archive(self, names) -> Path:
        path = self.root / f"batch-{uuid.uuid4().hex}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as output:
            for name in names:
                output.writestr(name, self.data[name])
        return path

    def start_round(self, jobs: Jobs, identifier: str) -> str:
        snapshot = self.wait_state(jobs, identifier, {"awaiting_files"})
        round_id = snapshot["selection"]["round_id"]
        requested = snapshot["selection"]["requested_files"]
        jobs.submit_files(identifier, self.archive(requested), round_id)
        return round_id

    def wait_state(self, jobs: Jobs, identifier: str, states, *, round_id: str | None = None, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        while True:
            snapshot = jobs.snapshot(identifier)
            if snapshot["status"] == "failed":
                raise AssertionError(
                    "native job failed during selection: "
                    f"{snapshot.get('error')!r} (error_code={snapshot.get('error_code')!r})"
                )
            selection = snapshot.get("selection") or {}
            if selection.get("state") in states and (round_id is None or selection.get("round_id") != round_id):
                return snapshot
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out waiting for {sorted(states)}; last state {selection.get('state')!r}")
            time.sleep(0.02)

    def finish_round(self, jobs: Jobs, identifier: str, previous_round: str) -> dict:
        return self.wait_state(jobs, identifier, {"awaiting_files", "ready"}, round_id=previous_round)

    def wait_failed(self, jobs: Jobs, identifier: str, timeout: float = 8.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            snapshot = jobs.snapshot(identifier)
            if snapshot["status"] == "failed":
                return snapshot
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out waiting for the refusal; last status {snapshot['status']!r}")
            time.sleep(0.02)

    def complete_selection(self, jobs: Jobs, identifier: str) -> dict:
        for _ in range(4):
            previous = self.start_round(jobs, identifier)
            snapshot = self.finish_round(jobs, identifier, previous)
            if snapshot["selection"]["state"] == "ready":
                return snapshot
        raise AssertionError("the selection did not reach ready")

    def manifest(self, jobs: Jobs, identifier: str) -> dict:
        observations = jobs.snapshot(identifier)["selection"]["observations"]
        return seal_selection(
            self.upload_files, MAIN, observations, selected_files=self.selected_files, reader_sha256=TOOL_SHA
        )

    def observe_events(self) -> list:
        return [entry for entry in self.reader_log if entry[0] == "observe"]

    # -- synchronous contract -------------------------------------------------

    def test_initial_stage_requests_only_the_main_assembly(self) -> None:
        identifier = self.create()
        snapshot = self.jobs.snapshot(identifier)
        self.assertEqual(snapshot["status"], "selecting")
        self.assertEqual(snapshot["selection"]["state"], "awaiting_files")
        self.assertEqual(snapshot["selection"]["requested_files"], {MAIN: self.upload_files[MAIN]})
        self.assertRegex(snapshot["selection"]["round_id"], r"^[0-9a-f]{64}$")
        self.assertNotIn("manifest", snapshot["selection"])
        # No native work runs before any staged bytes arrive.
        self.assertEqual(self.observe_events(), [])
        self.assertEqual(self.sentinel.calls, [])
        self.assertEqual(self.jobs.queue.qsize(), 0)

    def test_partial_extra_and_wrong_hash_batches_are_refused(self) -> None:
        identifier = self.create()
        round_id = self.jobs.snapshot(identifier)["selection"]["round_id"]

        extra = self.root / "extra-member.zip"
        with zipfile.ZipFile(extra, "w", zipfile.ZIP_DEFLATED) as output:
            output.writestr(MAIN, self.data[MAIN])
            output.writestr("decoy.SLDPRT", b"unrelated native document")
        partial = self.archive([FRAME])  # missing the requested main assembly
        invalid = self.root / "not-a-zip.zip"
        invalid.write_bytes(b"not a zip archive")
        wrong = self.root / "wrong-hash.zip"
        with zipfile.ZipFile(wrong, "w") as output:
            output.writestr(MAIN, b"different bytes")
        cases = {
            "selection_batch_inventory": [extra, partial],
            "selection_batch_invalid": [invalid],
            "selection_source_changed": [wrong],
        }

        for expected_code, archives in cases.items():
            for archive in archives:
                with self.subTest(code=expected_code, archive=archive.name):
                    with self.assertRaises(SelectionError) as caught:
                        self.jobs.submit_files(identifier, archive, round_id)
                    self.assertEqual(caught.exception.code, expected_code)
        with self.assertRaises(SelectionError) as caught:
            self.jobs.submit_files(identifier, self.archive([MAIN]), None)
        self.assertEqual(caught.exception.code, "selection_round_stale")

        snapshot = self.jobs.snapshot(identifier)
        self.assertEqual(snapshot["selection"]["state"], "awaiting_files")
        self.assertEqual(snapshot["selection"]["staged_files"], {})
        self.assertEqual(self.observe_events(), [])
        self.assertEqual(self.jobs.queue.qsize(), 0)

    def test_close_and_restart_fail_unfinished_states_without_replay(self) -> None:
        identifier = self.create()
        self.jobs.close()
        restarted = self.new_jobs()
        snapshot = restarted.snapshot(identifier)
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("restart", snapshot["error"].lower())
        self.assertEqual(self.observe_events(), [])
        self.assertEqual(self.sentinel.calls, [])
        with self.assertRaises(RequestError) as caught:
            restarted.submit_files(identifier, self.archive([MAIN]), snapshot["selection"]["round_id"])
        self.assertEqual(caught.exception.status, 409)

    # -- loop-driven contract (fails until the selection worker is wired) -----

    def test_happy_path_seals_and_never_runs_discovery_before_seal(self) -> None:
        identifier = self.create()
        ready = self.complete_selection(self.jobs, identifier)
        self.assertEqual(ready["selection"]["state"], "ready")
        self.assertEqual(self.sentinel.calls, [])
        self.assertGreaterEqual(len(self.observe_events()), 2)
        sealed = self.jobs.seal_selection(identifier, {"selection": self.manifest(self.jobs, identifier)})
        self.assertEqual(sealed["selection"]["state"], "sealed")
        self.assertIn("manifest", sealed["selection"])
        self.assertIn(sealed["status"], {"running", "failed"})
        # Only after the seal may the native reader be reached; the sentinel
        # then proves discovery was the next step, not geometry-free selection.
        failed = self.wait_failed(self.jobs, identifier)
        self.assertTrue(self.sentinel.calls, f"the sealed job never reached the native reader: {failed['error']!r}")
        self.assertIn(SENTINEL, failed["error"])
        self.assertEqual(self.runner_calls, [])

    def test_identical_batch_retry_does_not_requery(self) -> None:
        identifier = self.create()
        previous = self.start_round(self.jobs, identifier)
        archive = self.archive([MAIN])
        self.finish_round(self.jobs, identifier, previous)
        observed = len(self.observe_events())
        self.jobs.submit_files(identifier, archive, previous)
        time.sleep(0.3)
        self.assertEqual(len(self.observe_events()), observed)
        ready = self.complete_selection(self.jobs, identifier)
        self.assertEqual(ready["selection"]["state"], "ready")

    def test_staged_missing_extra_and_drift_refuse_sealing(self) -> None:
        for mutation in ("missing", "extra", "drift"):
            with self.subTest(mutation=mutation):
                self.reader_log.clear()
                jobs = self.new_jobs()
                identifier = self.create(jobs)
                self.complete_selection(jobs, identifier)
                stage = jobs.selections[identifier].stage
                if mutation == "missing":
                    (stage / LINK).unlink()
                elif mutation == "extra":
                    (stage / "extra.SLDASM").write_bytes(b"unrelated")
                else:
                    (stage / LINK).write_bytes(b"drifted bytes")
                with self.assertRaises(SelectionError) as caught:
                    jobs.seal_selection(identifier, {"selection": self.manifest(jobs, identifier)})
                self.assertEqual(caught.exception.code, "selection_inventory_changed")

    def test_false_graph_seal_is_rejected(self) -> None:
        forgeries = (
            ("empty_observations", "selection_incomplete"),
            ("dropped_file", "selection_manifest_changed"),
        )
        for forge, expected in forgeries:
            with self.subTest(forge=forge):
                self.reader_log.clear()
                jobs = self.new_jobs()
                identifier = self.create(jobs)
                self.complete_selection(jobs, identifier)
                manifest = copy.deepcopy(self.manifest(jobs, identifier))
                if forge == "empty_observations":
                    manifest["observations"] = {}
                else:
                    manifest["files"] = {name: sha for name, sha in manifest["files"].items() if name != LINK}
                with self.assertRaises(SelectionError) as caught:
                    jobs.seal_selection(identifier, {"selection": manifest})
                self.assertEqual(caught.exception.code, expected)

    def test_structured_reader_refusal_is_retained(self) -> None:
        self.refusing = True
        jobs = self.new_jobs()
        identifier = self.create(jobs)
        self.start_round(jobs, identifier)
        snapshot = self.wait_failed(jobs, identifier)
        self.assertEqual(snapshot.get("error_code"), "selection_dependencies_refused")
        self.assertEqual(snapshot["detail"]["problems"][0]["location"], "C:\\elsewhere\\link.SLDPRT")
        self.assertEqual(snapshot["detail"]["problems"][0]["problem"], "outside_stage")
        self.assertEqual(snapshot["selection"]["state"], "reading")
        self.assertEqual(self.sentinel.calls, [])
        self.assertEqual(self.runner_calls, [])

    def test_two_jobs_serialize_native_contexts(self) -> None:
        jobs = self.new_jobs()
        first = self.create(jobs, run_id=str(uuid.uuid4()))
        second = self.create(jobs, run_id=str(uuid.uuid4()))
        self.start_round(jobs, first)
        second_round = jobs.snapshot(second)["selection"]["round_id"]
        self.start_round(jobs, second)
        # The single native worker may only observe the second job after the
        # first job's context closed, which happens once its selection seals.
        ready = self.complete_selection(jobs, first)
        self.assertEqual(ready["selection"]["state"], "ready")
        observed = [entry[2] for entry in self.observe_events()]
        self.assertTrue(observed)
        self.assertEqual(set(observed), {first})
        jobs.seal_selection(first, {"selection": self.manifest(jobs, first)})
        self.wait_state(jobs, second, {"awaiting_files", "ready"}, round_id=second_round)
        self.assertTrue(
            any(entry[2] == second for entry in self.observe_events()), "the second job was never observed"
        )
        depth = 0
        for entry in self.reader_log:
            if entry[0] == "enter":
                depth += 1
            elif entry[0] == "exit":
                depth -= 1
            self.assertGreaterEqual(depth, 0)
            self.assertLessEqual(depth, 1, "native reader contexts overlapped across jobs")

    # -- loopback HTTP adapter -------------------------------------------------

    def http_call(self, base: str, method: str, path: str, body=None, headers=None):
        request = Request(base + path, data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310 - ephemeral loopback only
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_http_routes_and_selection_round_header(self) -> None:
        token = self.jobs.config["token"]
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.jobs, token))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        auth = {"Authorization": "Bearer " + token}
        json_headers = {**auth, "Content-Type": "application/json"}

        status, body = self.http_call(base, "GET", "/health", headers=auth)
        self.assertEqual(status, 200)
        self.assertTrue(body["ready"])

        anonymous = json.dumps(self.request(str(uuid.uuid4()))).encode()
        status, _body = self.http_call(base, "POST", "/v1/jobs", anonymous, {"Content-Type": "application/json"})
        self.assertEqual(status, 401)

        identifier = str(uuid.uuid4())
        created_body = json.dumps(self.request(identifier)).encode()
        status, body = self.http_call(base, "POST", "/v1/jobs", created_body, json_headers)
        self.assertEqual(status, 202)
        self.assertEqual(body["selection"]["requested_files"], {MAIN: self.upload_files[MAIN]})

        status, body = self.http_call(base, "GET", f"/v1/jobs/{identifier}", headers=auth)
        self.assertEqual(status, 200)
        self.assertEqual(body["run_id"], identifier)

        status, _body = self.http_call(
            base, "POST", f"/v1/jobs/{identifier}/files", b"xxxx", {**auth, "Content-Type": "text/plain"}
        )
        self.assertEqual(status, 400)

        archive = self.archive([MAIN]).read_bytes()
        status, body = self.http_call(
            base, "POST", f"/v1/jobs/{identifier}/files", archive, {**auth, "Content-Type": "application/zip"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error_code"), "selection_round_stale")

        snapshot = self.jobs.snapshot(identifier)["selection"]["round_id"]
        status, body = self.http_call(
            base,
            "POST",
            f"/v1/jobs/{identifier}/files",
            archive,
            {**auth, "Content-Type": "application/zip", "X-Selection-Round": snapshot},
        )
        self.assertEqual(status, 202)
        self.assertIn("selection", body)


if __name__ == "__main__":
    unittest.main()
