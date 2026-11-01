"""Private database dispatch reconciliation and authenticated API operations."""

import io
import json

from description_pipeline.dynamics_assessment.api import AssessmentApi
from description_pipeline.dynamics_assessment.contracts import DAG_ID, AssessmentConflict, SourceNotReady
from description_pipeline.dynamics_assessment.service import AssessmentAccess, AssessmentService
from tests.v1.test_dynamics_assessment_contracts import AssessmentCase
from tests.v1.test_dynamics_assessment_postgres import PostgresCase


class Transport:
    def __init__(self, *, accepted=True):
        self.accepted = accepted
        self.payload = None
        self.sends = 0

    def trigger(self, dag_id, dag_run_id, conf):
        self.sends += 1
        if self.accepted:
            self.payload = {"dag_id": dag_id, "dag_run_id": dag_run_id, "conf": conf, "state": "queued"}
        raise TimeoutError("Dispatch reply was lost")

    def observe(self, dag_id, dag_run_id):
        assert dag_id == DAG_ID
        if self.payload:
            assert self.payload["dag_run_id"] == dag_run_id
        return self.payload


def configured(database):
    store, request, root = database
    work = root / "service-work"
    work.mkdir()
    service = AssessmentService(
        store, enabled=True, runtime=request.runtime, executor_sha256=request.record["executor_sha256"], work_root=work
    )

    def source(dag, _control):
        assert dag == "source-dag-run"
        return request.source

    return (service, AssessmentAccess(request.record["principal"], False, source))


class ServiceDatabaseTests(PostgresCase):
    def test_accepted_dispatch_cannot_be_forgotten_after_repeated_authoritative_absence(self):
        service, access = configured(self.database)
        transport = Transport()
        public = service.create(access, "source-dag-run", "monotonic-acceptance", None, transport)
        attempt_id = public["attempt"]["attempt_id"]
        matched = service.reconcile_dispatch(access, attempt_id, transport)
        self.assertEqual(matched["attempt"]["state"], "queued")
        self.assertTrue(matched["attempt"]["dispatch_accepted"])
        transport.payload = None
        for _ in range(3):
            missing = service.reconcile_dispatch(access, attempt_id, transport)
            self.assertEqual(missing["attempt"]["state"], "unknown")
            self.assertTrue(missing["attempt"]["dispatch_accepted"])
            self.assertFalse(service.store.dispatch_absent(attempt_id))
            again = service.create(access, "source-dag-run", "monotonic-acceptance", None, transport)
            self.assertEqual(again["attempt"]["attempt_id"], attempt_id)
            self.assertEqual(transport.sends, 1)
        with self.assertRaisesRegex(AssessmentConflict, "never-accepted"):
            service.dispatch_registered(access, attempt_id, transport)
        with self.assertRaisesRegex(ValueError, "cannot replace"):
            service.store.unknown(attempt_id, {"dispatch_accepted": False})
        cancelled = service.cancel(access, attempt_id)
        self.assertTrue(cancelled["attempt"]["dispatch_accepted"])
        self.assertEqual(cancelled["attempt"]["state"], "cancelled")
        self.assertEqual(transport.sends, 1)

    def test_explicit_original_dispatch_requires_absence_and_preserves_attempt_conf(self):
        service, access = configured(self.database)
        transport = Transport(accepted=False)
        public = service.create(access, "source-dag-run", "controlled-original", None, transport)
        attempt_id = public["attempt"]["attempt_id"]
        original_request = service.store.request(public["request_id"]).to_record()
        original_dag = public["attempt"]["dag_run_id"]
        api = AssessmentApi(service, lambda *_args: (access, transport))
        request = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO()}
        self.assertEqual(api.handle(request, action="dispatch", identity=attempt_id).status, 409)
        service.reconcile_dispatch(access, attempt_id, transport)
        transport.accepted = True
        response = api.handle(request, action="dispatch", identity=attempt_id)
        self.assertEqual(response.status, 202, response.body)
        self.assertEqual(transport.sends, 2)
        self.assertEqual(transport.payload["dag_run_id"], original_dag)
        self.assertEqual(service.store.request(public["request_id"]).to_record(), original_request)
        self.assertEqual(api.handle(request, action="dispatch", identity=attempt_id).status, 409)
        service.reconcile_dispatch(access, attempt_id, transport)
        self.assertTrue(service.get(access, public["request_id"])["attempt"]["dispatch_accepted"])
        self.assertEqual(api.handle(request, action="dispatch", identity=attempt_id).status, 409)
        self.assertEqual(transport.sends, 2)

    def test_original_dispatch_reuses_source_authorization_and_rejects_client_identity(self):
        service, access = configured(self.database)
        transport = Transport(accepted=False)
        public = service.create(access, "source-dag-run", "controlled-access", None, transport)
        attempt_id = public["attempt"]["attempt_id"]
        service.reconcile_dispatch(access, attempt_id, transport)
        other = AssessmentAccess("other-operator", False, access.source)
        with self.assertRaises(PermissionError):
            service.dispatch_registered(other, attempt_id, transport)
        api = AssessmentApi(service, lambda *_args: (access, transport))
        data = b'{"attempt_id":"another-attempt"}'
        result = api.handle(
            {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(data)), "wsgi.input": io.BytesIO(data)},
            action="dispatch",
            identity=attempt_id,
        )
        self.assertEqual(result.status, 400)
        self.assertEqual(transport.sends, 1)

    def test_lost_dispatch_reply_reconciles_same_run_without_duplicate_spawn(self):
        database = self.database
        service, access = configured(database)
        transport = Transport()
        public = service.create(access, "source-dag-run", "lost-reply", None, transport)
        assert public["attempt"]["state"] == "unknown" and transport.sends == 1
        again = service.create(access, "source-dag-run", "lost-reply", None, transport)
        assert again["request_id"] == public["request_id"] and transport.sends == 1
        resolved = service.reconcile_dispatch(access, public["attempt"]["attempt_id"], transport)
        assert resolved["attempt"]["state"] == "queued" and transport.sends == 1

    def test_absence_observation_does_not_automatically_repeat_dispatch(self):
        database = self.database
        service, access = configured(database)
        transport = Transport(accepted=False)
        public = service.create(access, "source-dag-run", "absent-reply", None, transport)
        resolved = service.reconcile_dispatch(access, public["attempt"]["attempt_id"], transport)
        assert resolved["attempt"]["state"] == "registered" and transport.sends == 1
        repeated = service.create(access, "source-dag-run", "absent-reply", None, transport)
        assert repeated["attempt"]["attempt_id"] == public["attempt"]["attempt_id"]
        assert transport.sends == 2

    def test_wrong_observed_conf_stays_unknown_and_cannot_create_new_attempt(self):
        database = self.database
        service, access = configured(database)
        transport = Transport()
        public = service.create(access, "source-dag-run", "wrong-conf", None, transport)
        transport.payload["conf"] = {"foreign": "request"}
        resolved = service.reconcile_dispatch(access, public["attempt"]["attempt_id"], transport)
        assert resolved["attempt"]["state"] == "unknown"
        with self.assertRaises(AssessmentConflict):
            service.retry(access, public["request_id"], transport)

    def test_api_uses_root_mutation_context_and_rejects_source_fields(self):
        database = self.database
        service, access = configured(database)
        transport = Transport()
        calls = []

        def context(_environ, mutating):
            calls.append(mutating)
            return (access, transport)

        api = AssessmentApi(service, context)
        data = json.dumps({"bundle": None, "source": {"fake": "certificate"}}).encode()
        response = api.handle(
            {
                "REQUEST_METHOD": "POST",
                "CONTENT_LENGTH": str(len(data)),
                "wsgi.input": io.BytesIO(data),
                "HTTP_IDEMPOTENCY_KEY": "bad-source",
            },
            action="create",
            identity="source-dag-run",
        )
        assert response.status == 400 and calls == [True] and (transport.sends == 0)
        read = api.handle({"REQUEST_METHOD": "GET"}, action="list", identity="source-dag-run")
        assert read.status == 200 and calls == [True, False]
        assert json.loads(read.body)["assessments"] == []

    def test_authorized_incomplete_engineering_source_is_not_a_pipeline_failure(self):
        database = self.database
        service, access = configured(database)

        def pending(_dag, _control):
            raise SourceNotReady("Engineering MJCF has not completed")

        access = AssessmentAccess(access.principal, False, pending)
        value = service.list_for_source(access, "source-dag-run")
        assert value["enabled"] and value["source_ready"] is False and (value["assessments"] == [])
        assert value["publish_dependency"] is False
        with service.store.transaction() as cursor:
            cursor.execute("SELECT count(*) FROM assessment_request")
            assert cursor.fetchone()[0] == 0


class ServiceTests(AssessmentCase):
    def test_disabled_feature_has_no_database_source_or_dispatch_side_effects(self):

        def forbidden(*_args):
            raise AssertionError("A disabled assessment must not admit, dispatch or read a model")

        service = AssessmentService(None)
        access = AssessmentAccess("owner", False, forbidden)
        assert service.list_for_source(access, "not-loaded")["enabled"] is False
        with self.assertRaisesRegex(ValueError, "disabled"):
            service.create(access, "not-loaded", "key", None, None)

    def test_api_preserves_existing_portal_authentication_error_protocol(self):

        class PortalError(Exception):
            status = 401
            message = "Session expired"

        def context(*_args):
            raise PortalError()

        response = AssessmentApi(AssessmentService(None), context).handle(
            {"REQUEST_METHOD": "GET"}, action="list", identity="source-dag-run"
        )
        assert response.status == 401 and json.loads(response.body)["error"] == "Session expired"
