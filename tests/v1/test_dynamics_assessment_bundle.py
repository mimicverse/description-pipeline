"""Actual archive admission, authorization, limits and cancellation cleanup."""

from __future__ import annotations

import base64
import io
import json
import stat
import unittest
import zipfile

from description_pipeline.dynamics_assessment.api import AssessmentApi
from description_pipeline.dynamics_assessment.contracts import AssessmentConflict, dispatch_conf
from description_pipeline.dynamics_assessment.service import AssessmentAccess
from description_pipeline.pipeline.contracts import ArtifactRef
from tests.v1.test_dynamics_assessment_native import Dispatch, build_native_case
from tests.v1.test_dynamics_assessment_postgres import PostgresCase


def archive_case(case, *, extra=None, change=None):
    _service, _executor, _access, _bundle, copied, *_ = case
    content = {path.relative_to(copied).as_posix(): path.read_bytes() for path in copied.rglob("*") if path.is_file()}
    if change:
        change(content)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in content.items():
            archive.writestr(name, data)
        for name, data in extra or []:
            archive.writestr(name, data)
    return output.getvalue()


def upload_api(case, archive, *, access_override=None, **extra):
    service, _executor, access, *_ = case
    api = AssessmentApi(service, lambda _environ, mutating: (access_override or access, Dispatch()))
    body = json.dumps({"archive_base64": base64.b64encode(archive).decode()}).encode()
    environ = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), **extra}
    return api.handle(environ, action="bundle", identity="source-dag-run")


class BundleDatabaseTests(PostgresCase):
    def test_uploaded_archive_executes_real_native_run_and_independent_verify(self):
        case = build_native_case(self.database)
        service, executor, access, *_ = case
        response = upload_api(case, archive_case(case))
        assert response.status == 201, response.body
        admitted = json.loads(response.body)
        source = access.source("source-dag-run", True)
        assert admitted["source_sha256"] == source.sha256
        bundle = ArtifactRef.from_record(admitted["bundle"])
        public = service.create(access, "source-dag-run", "uploaded-native", bundle, Dispatch())
        attempt_id = public["attempt"]["attempt_id"]
        result = executor.execute(
            attempt_id, dispatch_conf(public["request_id"], attempt_id, service.store.request(public["request_id"]))
        )
        assert result["state"] == "completed"
        assert json.loads(service.read_result(access, attempt_id, "verify.stdout.json")[0])["pass"]
        other = AssessmentAccess("tenant:other", False, access.source)
        with self.assertRaisesRegex(ValueError, "another operator or source"):
            service.create(other, "source-dag-run", "stolen-bundle", bundle, Dispatch())

    def _check_refused_archives_leave_no_staging_or_requests(self, bad):
        case = build_native_case(self.database)
        service, *_ = case
        extra = []
        change = None
        if bad in {"escape", "absolute", "backslash"}:
            extra = [({"escape": "../outside", "absolute": "/absolute", "backslash": "a\\b"}[bad], b"refused")]
        elif bad == "alias":
            extra = [("PLAN.JSON", b"refused")]
        elif bad == "directory_alias":
            extra = [("Inputs/other", b"refused")]
        elif bad == "symlink":
            link = zipfile.ZipInfo("linked")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 511) << 16
            extra = [(link, b"/etc/passwd")]
        elif bad == "reserved":
            extra = [("manifest.json", b"refused")]
        elif bad == "wrong_source":

            def change(content):
                envelope = json.loads(content["assessment.json"])
                envelope["source_sha256"] = "0" * 64
                content["assessment.json"] = json.dumps(envelope).encode()
        elif bad == "wrong_urdf":

            def change(content):
                name = next(name for name in content if name.endswith(".urdf"))
                content[name] += b"\n"

        response = upload_api(case, archive_case(case, extra=extra, change=change))
        assert response.status == 400, response.body
        assert not list(service.work_root.glob("assessment-upload-*"))
        with service.store.transaction() as cursor:
            cursor.execute("SELECT count(*) FROM assessment_request")
            assert cursor.fetchone()[0] == 0

    def test_refused_archives_leave_no_staging_or_requests_case_1(self):
        self._check_refused_archives_leave_no_staging_or_requests("escape")

    def test_refused_archives_leave_no_staging_or_requests_case_2(self):
        self._check_refused_archives_leave_no_staging_or_requests("absolute")

    def test_refused_archives_leave_no_staging_or_requests_case_3(self):
        self._check_refused_archives_leave_no_staging_or_requests("backslash")

    def test_refused_archives_leave_no_staging_or_requests_case_4(self):
        self._check_refused_archives_leave_no_staging_or_requests("alias")

    def test_refused_archives_leave_no_staging_or_requests_case_5(self):
        self._check_refused_archives_leave_no_staging_or_requests("directory_alias")

    def test_refused_archives_leave_no_staging_or_requests_case_6(self):
        self._check_refused_archives_leave_no_staging_or_requests("symlink")

    def test_refused_archives_leave_no_staging_or_requests_case_7(self):
        self._check_refused_archives_leave_no_staging_or_requests("reserved")

    def test_refused_archives_leave_no_staging_or_requests_case_8(self):
        self._check_refused_archives_leave_no_staging_or_requests("wrong_source")

    def test_refused_archives_leave_no_staging_or_requests_case_9(self):
        self._check_refused_archives_leave_no_staging_or_requests("wrong_urdf")

    def test_expanded_archive_budget_is_enforced_before_any_dispatch(self):
        case = build_native_case(self.database)
        service, *_ = case
        archive = archive_case(case, extra=[("compressed-fill.bin", b"x" * 1024**2)])
        service.input_limit = len(archive) + 1024
        response = upload_api(case, archive)
        assert response.status == 400 and b"budget" in response.body
        assert not list(service.work_root.glob("assessment-upload-*"))

    def test_unauthorized_upload_does_not_consume_body(self):
        case = build_native_case(self.database)
        service, _executor, access, *_ = case

        def denied(_dag, _control):
            raise PermissionError("Source upload access denied")

        class Unreadable:
            def read(self, _length):
                unittest.TestCase().fail("Unauthorized source must be rejected before reading input")

        api = AssessmentApi(service, lambda *_args: (AssessmentAccess(access.principal, False, denied), Dispatch()))
        response = api.handle(
            {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": "1", "wsgi.input": Unreadable()},
            action="bundle",
            identity="source-dag-run",
        )
        assert response.status == 403

    def test_upload_capacity_and_cancel_cleanup_use_existing_gate(self):
        case = build_native_case(self.database)
        service, _executor, access, *_ = case
        service.capacity = 1
        archive = archive_case(case)

        def read_archive(_limit):
            with self.assertRaises(Exception) as rejected:
                service.upload_bundle(
                    access, "source-dag-run", lambda _limit: unittest.TestCase().fail("Capacity must precede body IO")
                )
            assert getattr(rejected.exception, "status", None) == 429
            return archive

        assert service.upload_bundle(access, "source-dag-run", read_archive)["bundle"]
        observed = 0

        def cancelled():
            nonlocal observed
            observed += 1
            return observed >= 4

        with self.assertRaisesRegex(AssessmentConflict, "cancelled"):
            service.upload_bundle(access, "source-dag-run", lambda _limit: archive, should_cancel=cancelled)
        assert not list(service.work_root.glob("assessment-upload-*"))
