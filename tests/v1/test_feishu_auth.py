"""Pinned-Airflow tests for the Feishu auth manager and its metadata-table state.

The portable OAuth protocol runs without Airflow in ``test_feishu_oauth.py``; these tests need the
real Airflow 3.3.2 runtime (same guard as ``test_airflow_dag.py``) and run in the pinned
environment. The suite provisions its own isolated metadata database from the real Airflow models
(``revoked_token`` and the manager's own state table), binds ``airflow.settings`` to it for the
duration of the class, and never mutates the ambient Airflow home or process environment; the
production code keeps exactly one storage path.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests.v1._airflow_env import pinned_airflow_home

# Absolute and outside the checkout, fixed before Airflow is imported.
pinned_airflow_home()

AIRFLOW_AVAILABLE = importlib.util.find_spec("airflow") is not None

if AIRFLOW_AVAILABLE:
    import jwt
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from starlette.requests import Request
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from airflow.configuration import conf
    from airflow.api_fastapi.core_api.routes.public.dag_run import (
        bulk_dag_runs,
        clear_dag_run,
        clear_dag_run_partitions,
        delete_dag_run,
        patch_dag_run,
        trigger_dag_run,
    )
    from airflow.models.dagrun import DagRun
    from airflow.models.revoked_token import RevokedToken
    from airflow.models.tasklog import LogTemplate
    from airflow.models.taskinstance import TaskInstance

    from description_pipeline.orchestration import feishu_auth as auth
    from description_pipeline.orchestration import request_context
    from description_pipeline.orchestration import run_ownership
    from description_pipeline.orchestration.feishu_oauth import (
        STATE_COOKIE,
        TRIGGERING_USER_NAME_DELIMITER,
        TRIGGERING_USER_NAME_LIMIT,
        state_digest,
    )

JWT_SECRET = "feishu-auth-manager-tests-" + "0" * 40


class _RoutedRequest:
    """Minimal stand-in for the Request the root middleware binds: scope plus path params."""

    def __init__(
        self,
        endpoint,
        dag_id: str,
        dag_run_id: str | None = None,
        http_method: str = "POST",
        clear_body_safe: bool | None = None,
    ) -> None:
        self.scope = {"type": "http", "endpoint": endpoint, "method": http_method}
        if getattr(endpoint, "__name__", None) == "trigger_dag_run":
            self.scope[request_context.CREATE_BODY_SCOPE_KEY] = {"safe": True, "parent_dag_run_id": None}
        if clear_body_safe is not None:
            self.scope[request_context.CLEAR_BODY_SCOPE_KEY] = {"safe": clear_body_safe}
        self.path_params = {"dag_id": dag_id}
        if dag_run_id is not None:
            self.path_params["dag_run_id"] = dag_run_id


def _environment(secret_dir: Path, **overrides: str) -> dict[str, str]:
    secret = secret_dir / "feishu.json"
    secret.write_text(json.dumps({"app_id": "cli_app", "app_secret": "s3cret"}), encoding="utf-8")
    os.chmod(secret, 0o600)
    return {
        "FEISHU_APP_SECRET_FILE": str(secret),
        "FEISHU_TENANT_KEYS": "tenant-a",
        "FEISHU_REDIRECT_URI": "https://10.0.0.235:8443/auth/feishu/callback",
        "FEISHU_ADMIN_OPEN_IDS": "ou_admin",
        **overrides,
    }


@unittest.skipUnless(AIRFLOW_AVAILABLE, "Airflow is not installed in this interpreter")
@unittest.skipIf(os.name == "nt", "the Feishu auth manager runs in the Linux Airflow deployment")
class FeishuAuthManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.metadata = tempfile.TemporaryDirectory()
        cls.engine = create_engine(f"sqlite:///{cls.metadata.name}/metadata.db")
        cls.sessions = sessionmaker(bind=cls.engine)
        # Real Airflow tables this suite touches, created from the real models.
        RevokedToken.__table__.create(cls.engine, checkfirst=True)
        LogTemplate.__table__.create(cls.engine, checkfirst=True)
        DagRun.__table__.create(cls.engine, checkfirst=True)
        TaskInstance.__table__.create(cls.engine, checkfirst=True)
        # One isolated metadata database for the whole class: the manager's own state table and
        # Airflow's revocation lookup both run against it through the real Session.
        cls._settings = mock.patch.multiple(
            "airflow.settings",
            Session=cls.sessions,
            NonScopedSession=cls.sessions,
            engine=cls.engine,
        )
        cls._settings.start()
        conf.set("api_auth", "jwt_secret", JWT_SECRET)
        cls._clear_token_caches()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._settings.stop()
        cls._clear_token_caches()
        cls.engine.dispose()
        cls.metadata.cleanup()

    @classmethod
    def _clear_token_caches(cls) -> None:
        auth.FeishuAuthManager._get_token_signer.cache_clear()
        auth.FeishuAuthManager._get_token_validator.cache_clear()

    def setUp(self) -> None:
        self.root = Path(self.metadata.name)

    def manager(self, environ: dict[str, str] | None = None) -> auth.FeishuAuthManager:
        """The manager is built exactly as the api-server builds it, against the isolated metadata."""
        manager = auth.FeishuAuthManager()
        with mock.patch.dict(os.environ, environ or {}, clear=True):
            manager.init()
        return manager

    def client(self, manager: auth.FeishuAuthManager) -> TestClient:
        # https keeps the Secure SSO cookies flowing, and redirects stay observable.
        return TestClient(manager.get_fastapi_app(), base_url="https://operator.example", follow_redirects=False)

    def identity(self, *, open_id: str = "ou_worker", tenant: str = "tenant-a") -> auth.FeishuIdentity:
        return auth.FeishuIdentity(open_id=open_id, name="崔工", avatar_url="https://avatar/u", tenant_key=tenant)

    def authenticate(self, manager: auth.FeishuAuthManager, client: TestClient) -> str:
        """Run one complete sign-in and return the raw Airflow JWT the callback issued."""
        login = client.get("/feishu/login")
        self.assertEqual(login.status_code, 302, login.text)
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        client.cookies.set(STATE_COOKIE, state)
        with (
            mock.patch.object(auth, "exchange_code", return_value="u-token"),
            mock.patch.object(auth, "fetch_identity", return_value=self.identity()),
        ):
            callback = client.get(f"/feishu/callback?code=code-1&state={state}")
        self.assertEqual(callback.status_code, 303, callback.text)
        self.assertEqual(callback.headers["location"], "/")
        return callback.cookies[auth.COOKIE_NAME_JWT_TOKEN]

    # -- lifecycle --------------------------------------------------------

    def test_unconfigured_install_keeps_serving_and_reports_unavailable(self) -> None:
        manager = self.manager(environ={})
        client = self.client(manager)
        health = client.get("/feishu/health")
        self.assertEqual(health.status_code, 503)
        self.assertEqual(health.json()["configured"], False)
        self.assertEqual(health.json()["reason"], "feishu_sso_unconfigured")
        self.assertEqual(client.get("/feishu/login").status_code, 503)
        self.assertEqual(client.get("/feishu/callback").status_code, 503)
        self.assertEqual(client.get("/feishu/profile").status_code, 401)

    # -- sign-in ----------------------------------------------------------

    def test_login_sends_pkce_state_cookie_without_the_secret(self) -> None:
        manager = self.manager(_environment(self.root))
        client = self.client(manager)
        health = client.get("/feishu/health").json()
        self.assertTrue(health["configured"])
        # The standalone auth app has no root middleware; the field must still be reported.
        self.assertIn("request_context", health)
        response = client.get("/feishu/login?next=https://evil.example/")
        self.assertEqual(response.status_code, 302)
        location = response.headers["location"]
        query = parse_qs(urlsplit(location).query)
        # A supplied next target is ignored: the callback always lands on the portal page.
        self.assertTrue(location.startswith("https://accounts.feishu.cn/open-apis/authen/v1/authorize?"))
        self.assertEqual(query["client_id"], ["cli_app"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], ["https://10.0.0.235:8443/auth/feishu/callback"])
        self.assertNotIn("scope", query)
        self.assertNotIn("s3cret", location)
        cookie = response.headers["set-cookie"]
        self.assertIn(f"{STATE_COOKIE}={query['state'][0]}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn(f"Path={auth.STATE_COOKIE_PATH}", cookie)

    def test_callback_binds_the_state_cookie_and_burns_the_state_once(self) -> None:
        manager = self.manager(_environment(self.root))
        client = self.client(manager)
        token = self.authenticate(manager, client)
        self.assertTrue(token)
        self.assertEqual(client.get("/feishu/profile").json()["open_id"], "ou_worker")
        # The state row is gone: replaying the same callback (with the same cookie) fails.
        login = client.get("/feishu/login")
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        client.cookies.set(STATE_COOKIE, "other")
        mismatch = client.get(f"/feishu/callback?code=code-1&state={state}")
        self.assertEqual(mismatch.status_code, 400)
        client.cookies.set(STATE_COOKIE, state)
        with (
            mock.patch.object(auth, "exchange_code", return_value="u-token"),
            mock.patch.object(auth, "fetch_identity", return_value=self.identity()),
        ):
            first = client.get(f"/feishu/callback?code=code-1&state={state}")
            replayed = client.get(f"/feishu/callback?code=code-1&state={state}")
        self.assertEqual(first.status_code, 303)
        self.assertEqual(replayed.status_code, 400)

    def test_denied_authorization_stays_a_clear_login_failure(self) -> None:
        manager = self.manager(_environment(self.root))
        client = self.client(manager)
        denied = client.get("/feishu/callback?error=access_denied&state=state-1")
        self.assertEqual(denied.status_code, 400)
        self.assertIn("取消飞书授权", denied.text)

    def test_profile_rejects_missing_and_tampered_tokens(self) -> None:
        manager = self.manager(_environment(self.root))
        client = self.client(manager)
        self.authenticate(manager, client)
        profile = client.get("/feishu/profile")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(
            profile.json(),
            {
                "open_id": "ou_worker",
                "app_id": "cli_app",
                "name": "崔工",
                "avatar_url": "https://avatar/u",
                "tenant_key": "tenant-a",
                "principal": "cli_app:tenant-a:ou_worker",
                "role": "OPERATOR",
            },
        )
        client.cookies.set(auth.COOKIE_NAME_JWT_TOKEN, "not-a-jwt")
        self.assertEqual(client.get("/feishu/profile").status_code, 401)

    def test_profile_refuses_revoked_and_replaced_app_tokens(self) -> None:
        manager = self.manager(_environment(self.root))
        client = self.client(manager)
        token = self.authenticate(manager, client)
        # A token minted by a replaced enterprise app stays invalid even though it is signed.
        foreign = auth.FeishuAuthManager()
        foreign.settings = dataclasses.replace(manager.settings, app_id="cli_replaced")
        client.cookies.set(
            auth.COOKIE_NAME_JWT_TOKEN,
            foreign.generate_jwt(
                auth.FeishuUser(
                    app_id="cli_replaced", open_id="ou_worker", name="崔工", avatar_url="", tenant_key="tenant-a"
                )
            ),
        )
        self.assertEqual(client.get("/feishu/profile").status_code, 401)
        # Revocation is Airflow's real table, not a stub.
        client.cookies.set(auth.COOKIE_NAME_JWT_TOKEN, token)
        self.assertEqual(client.get("/feishu/profile").status_code, 200)
        claims = jwt.decode(token, options={"verify_signature": False})
        RevokedToken.revoke(claims["jti"], datetime.fromtimestamp(claims["exp"], tz=UTC))
        self.assertEqual(client.get("/feishu/profile").status_code, 401)

    # -- authorization ----------------------------------------------------

    def test_operator_reads_and_triggers_only_the_one_workflow(self) -> None:
        manager = self.manager(_environment(self.root))
        operator = auth.FeishuUser(
            app_id="cli_app", open_id="ou_worker", name="崔工", avatar_url="", tenant_key="tenant-a"
        )
        admin = auth.FeishuUser(
            app_id="cli_app", open_id="ou_admin", name="管理员", avatar_url="", tenant_key="tenant-a"
        )
        allowed = auth.DagDetails(id=auth.ALLOWED_DAG_ID)
        other = auth.DagDetails(id="some_other_dag")
        self.assertTrue(manager.is_authorized_dag(method="GET", user=operator, details=allowed))
        # Without the packaged request context every operator RUN write fails closed.
        self.assertFalse(
            manager.is_authorized_dag(
                method="POST", user=operator, details=allowed, access_entity=auth.DagAccessEntity.RUN
            )
        )
        with request_context.use_request(_RoutedRequest(trigger_dag_run, auth.ALLOWED_DAG_ID)):
            self.assertTrue(
                manager.is_authorized_dag(
                    method="POST", user=operator, details=allowed, access_entity=auth.DagAccessEntity.RUN
                )
            )
        self.assertFalse(manager.is_authorized_dag(method="POST", user=operator, details=allowed))
        self.assertFalse(
            manager.is_authorized_dag(
                method="POST", user=operator, details=allowed, access_entity=auth.DagAccessEntity.TASK_INSTANCE
            )
        )
        self.assertFalse(manager.is_authorized_dag(method="DELETE", user=operator, details=allowed))
        self.assertFalse(manager.is_authorized_dag(method="GET", user=operator, details=other))
        self.assertFalse(manager.is_authorized_dag(method="GET", user=operator, details=None))
        self.assertFalse(manager.is_authorized_connection(method="GET", user=operator))
        self.assertFalse(manager.is_authorized_variable(method="GET", user=operator))
        self.assertFalse(manager.is_authorized_view(access_view=None, user=operator))
        self.assertEqual(manager.filter_authorized_menu_items(list(auth.MenuItem), user=operator), [auth.MenuItem.DAGS])
        self.assertTrue(manager.is_authorized_dag(method="DELETE", user=admin, details=other))
        self.assertTrue(manager.is_authorized_connection(method="GET", user=admin))
        self.assertEqual(manager.filter_authorized_menu_items(list(auth.MenuItem), user=admin), list(auth.MenuItem))
        self.assertEqual(manager.role_of(operator), auth.ROLE_OPERATOR)
        self.assertEqual(manager.role_of(admin), auth.ROLE_ADMIN)

    def test_tokens_never_grant_administration_or_another_tenant(self) -> None:
        manager = self.manager(_environment(self.root))
        token = manager.serialize_user(
            auth.FeishuUser(app_id="cli_app", open_id="ou_worker", name="崔工", avatar_url="", tenant_key="tenant-a")
        )
        self.assertEqual(manager.deserialize_user(token).open_id, "ou_worker")
        self.assertEqual(manager.deserialize_user(token).get_id(), "cli_app:tenant-a:ou_worker")
        # Airflow records get_name() as triggering_user_name: the stable principal plus the
        # Feishu-verified display name, as the auth-owned delimiter+JSON envelope.
        envelope = manager.deserialize_user(token).get_name()
        principal, delimiter, encoded = envelope.partition(TRIGGERING_USER_NAME_DELIMITER)
        self.assertEqual(principal, "cli_app:tenant-a:ou_worker")
        self.assertEqual(delimiter, TRIGGERING_USER_NAME_DELIMITER)
        self.assertEqual(json.loads(encoded), "崔工")
        with self.assertRaises(ValueError):
            manager.deserialize_user({**token, "tenant_key": "tenant-b"})
        # A token minted by a replaced enterprise app must not stay valid.
        with self.assertRaises(ValueError):
            manager.deserialize_user({**token, "app_id": "cli_replaced"})
        with self.assertRaises(ValueError):
            manager.deserialize_user({"sub": "ou_worker", "app_id": "cli_app", "tenant_key": ""})
        for field in ("sub", "app_id", "tenant_key"):
            for value in (None, 123, True, [], {}):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    manager.deserialize_user({**token, field: value})
        for open_id in ("ou:worker", "ou worker", "ou|worker", "ou\x00worker"):
            with self.subTest(open_id=open_id), self.assertRaises(ValueError):
                manager.deserialize_user({**token, "sub": open_id})
        # A role claim in the token is ignored; administration is the configured allowlist only.
        impersonation = {**token, "role": "ADMIN"}
        self.assertEqual(manager.role_of(manager.deserialize_user(impersonation)), auth.ROLE_OPERATOR)
        # A token whose claims carry no usable display name is rejected, never shown as an id.
        with self.assertRaises(ValueError):
            manager.deserialize_user({**token, "name": ""})
        with self.assertRaises(ValueError):
            manager.deserialize_user({**token, "name": "evil\u202egniht"})
        # A genuine name equal to the open_id is preserved and an unrecordable one fails here.
        same = manager.deserialize_user({**token, "name": "ou_worker"})
        self.assertEqual(same.get_name(), 'cli_app:tenant-a:ou_worker|"ou_worker"')
        # A malformed, non-string claim is refused instead of being cast into a display name.
        for bad_claim in (123, ["崔工"], {"name": "崔工"}, True, None):
            with self.subTest(claim=bad_claim), self.assertRaises(ValueError):
                manager.deserialize_user({**token, "name": bad_claim})
        # The stored name is the normalized recorded value, always consistent with the profile.
        normalized = manager.deserialize_user({**token, "name": "e\u0301"})
        self.assertEqual(normalized.name, "é")
        self.assertEqual(normalized.get_name(), 'cli_app:tenant-a:ou_worker|"é"')
        # The exact metadata-column boundary: 512 characters accepted, 513 refused.
        boundary = TRIGGERING_USER_NAME_LIMIT - len("cli_app:tenant-a:ou_worker") - 1 - 2
        self.assertEqual(
            len(manager.deserialize_user({**token, "name": "A" * boundary}).get_name()),
            TRIGGERING_USER_NAME_LIMIT,
        )
        with self.assertRaises(ValueError):
            manager.deserialize_user({**token, "name": "A" * (boundary + 1)})

    # -- run ownership ----------------------------------------------------

    def _record_run(
        self,
        run_id: str,
        actor: object,
        *,
        state: str = "failed",
        tasks: dict[str, str] | None = None,
    ) -> None:
        with self.sessions() as session:
            session.add(
                DagRun(
                    dag_id=auth.ALLOWED_DAG_ID,
                    run_id=run_id,
                    run_type="manual",
                    state=state,
                    triggering_user_name=actor,
                )
            )
            for task_id, task_state in (tasks or {}).items():
                session.execute(
                    TaskInstance.__table__.insert().values(
                        dag_id=auth.ALLOWED_DAG_ID,
                        run_id=run_id,
                        task_id=task_id,
                        map_index=-1,
                        state=task_state,
                        pool="default_pool",
                        pool_slots=1,
                    )
                )
            session.commit()

    def test_linked_creation_requires_stored_parent_ownership(self) -> None:
        manager = self.manager(_environment(self.root))
        owner = auth.FeishuUser(
            app_id="cli_app", open_id="ou_worker", name="Owner", avatar_url="", tenant_key="tenant-a"
        )
        other = auth.FeishuUser(
            app_id="cli_app", open_id="ou_other", name="Other", avatar_url="", tenant_key="tenant-a"
        )
        admin = auth.FeishuUser(
            app_id="cli_app", open_id="ou_admin", name="Admin", avatar_url="", tenant_key="tenant-a"
        )
        self._record_run("linked-parent", owner.get_name(), state="success")
        request = _RoutedRequest(trigger_dag_run, auth.ALLOWED_DAG_ID)
        request.scope[request_context.CREATE_BODY_SCOPE_KEY] = {"safe": True, "parent_dag_run_id": "linked-parent"}
        with request_context.use_request(request):
            for user, expected in ((owner, True), (other, False), (admin, True)):
                self.assertEqual(
                    manager.is_authorized_dag(
                        method="POST",
                        user=user,
                        details=auth.DagDetails(id=auth.ALLOWED_DAG_ID),
                        access_entity=auth.DagAccessEntity.RUN,
                    ),
                    expected,
                )
        del request.scope[request_context.CREATE_BODY_SCOPE_KEY]
        with request_context.use_request(request):
            self.assertFalse(
                manager.is_authorized_dag(
                    method="POST",
                    user=owner,
                    details=auth.DagDetails(id=auth.ALLOWED_DAG_ID),
                    access_entity=auth.DagAccessEntity.RUN,
                )
            )

    def test_run_ownership_grants_only_the_routed_single_run_clear(self) -> None:
        manager = self.manager(_environment(self.root))
        owner = auth.FeishuUser(app_id="cli_app", open_id="ou_owner", name="崔工", avatar_url="", tenant_key="tenant-a")
        other = auth.FeishuUser(app_id="cli_app", open_id="ou_other", name="李工", avatar_url="", tenant_key="tenant-a")
        admin = auth.FeishuUser(
            app_id="cli_app", open_id="ou_admin", name="管理员", avatar_url="", tenant_key="tenant-a"
        )
        allowed = auth.DagDetails(id=auth.ALLOWED_DAG_ID)
        transport_failure = {
            "resolve_handoff": "success",
            "start_job": "success",
            "wait_for_job": "failed",
            "confirm_job": "upstream_failed",
        }
        self._record_run(
            "run-own",
            auth.build_actor_name("cli_app:tenant-a:ou_owner", "崔工"),
            tasks=transport_failure,
        )
        self._record_run(
            "run-other",
            auth.build_actor_name("cli_app:tenant-a:ou_other", "李工"),
            tasks=transport_failure,
        )
        self._record_run("run-legacy", "cli_app:tenant-a:ou_owner")
        self._record_run("run-damaged", 'cli_app:tenant-a:ou_owner|"bad\\nname"')
        self._record_run(
            "run-resolve-failed",
            auth.build_actor_name("cli_app:tenant-a:ou_owner", "崔工"),
            tasks={"resolve_handoff": "failed", "start_job": "upstream_failed"},
        )
        self._record_run(
            "run-start-failed",
            auth.build_actor_name("cli_app:tenant-a:ou_owner", "崔工"),
            tasks={"resolve_handoff": "success", "start_job": "failed"},
        )
        self._record_run(
            "run-start-unproven",
            auth.build_actor_name("cli_app:tenant-a:ou_owner", "崔工"),
            tasks={"start_job": "failed"},
        )
        self._record_run(
            "run-mapped",
            auth.build_actor_name("cli_app:tenant-a:ou_owner", "崔工"),
            tasks={"resolve_handoff": "success", "wait_for_job": "failed"},
        )
        with self.sessions() as session:
            session.execute(
                TaskInstance.__table__.insert().values(
                    dag_id=auth.ALLOWED_DAG_ID,
                    run_id="run-mapped",
                    task_id="wait_for_job",
                    map_index=0,
                    state="success",
                    pool="default_pool",
                    pool_slots=1,
                )
            )
            session.commit()
        self._record_run(
            "run-success",
            auth.build_actor_name("cli_app:tenant-a:ou_owner", "崔工"),
            state="success",
            tasks=transport_failure,
        )
        self._record_run(
            "run-running",
            auth.build_actor_name("cli_app:tenant-a:ou_owner", "崔工"),
            state="running",
            tasks={"wait_for_job": "failed"},
        )
        self._record_run(
            "run-no-failed",
            auth.build_actor_name("cli_app:tenant-a:ou_owner", "崔工"),
            tasks={"resolve_handoff": "success"},
        )

        def decide(
            method: str,
            entity,
            endpoint,
            *,
            dag_id: str = auth.ALLOWED_DAG_ID,
            run_id: str | None = None,
            user=owner,
            clear_body_safe: bool | None = True,
        ) -> bool:
            with request_context.use_request(_RoutedRequest(endpoint, dag_id, run_id, clear_body_safe=clear_body_safe)):
                return manager.is_authorized_dag(method=method, access_entity=entity, details=allowed, user=user)

        run = auth.DagAccessEntity.RUN
        # The one owner mutation: the caller's own run through the routed clear endpoint.
        self.assertTrue(decide("PUT", run, clear_dag_run, run_id="run-own"))
        self.assertFalse(decide("PUT", run, clear_dag_run, run_id="run-own", user=other))
        self.assertFalse(decide("PUT", run, clear_dag_run, run_id="run-other"))
        self.assertFalse(decide("PUT", run, clear_dag_run, run_id="run-legacy"))
        self.assertFalse(decide("PUT", run, clear_dag_run, run_id="run-damaged"))
        self.assertFalse(decide("PUT", run, clear_dag_run, run_id="run-resolve-failed"))
        # A failed start_job is retryable only with a positively successful resolution.
        self.assertTrue(decide("PUT", run, clear_dag_run, run_id="run-start-failed"))
        self.assertFalse(decide("PUT", run, clear_dag_run, run_id="run-start-unproven"))
        self.assertFalse(decide("PUT", run, clear_dag_run, run_id="run-mapped"))
        self.assertEqual(
            run_ownership.retry_assessment_for_run(auth.ALLOWED_DAG_ID, "missing-run").reason,
            "run_missing",
        )
        self.assertFalse(decide("PUT", run, clear_dag_run, run_id="run-success"))
        self.assertFalse(decide("PUT", run, clear_dag_run, run_id="run-running"))
        self.assertFalse(decide("PUT", run, clear_dag_run, run_id="run-no-failed"))
        # The exact immutable-retry body is part of the owner grant; admins are exempt.
        self.assertFalse(decide("PUT", run, clear_dag_run, run_id="run-own", clear_body_safe=False))
        self.assertFalse(decide("PUT", run, clear_dag_run, run_id="run-own", clear_body_safe=None))
        self.assertTrue(decide("PUT", run, clear_dag_run, run_id="run-own", user=admin))
        self.assertTrue(decide("PUT", run, clear_dag_run, run_id="run-own", user=admin, clear_body_safe=False))
        # Same (method, entity) but any other route stays administrator-only.
        self.assertFalse(decide("PUT", run, patch_dag_run, run_id="run-own"))
        self.assertFalse(decide("PUT", run, clear_dag_run_partitions, run_id="run-own"))
        self.assertFalse(decide("PUT", run, bulk_dag_runs, run_id="run-own"))
        self.assertFalse(decide("DELETE", run, delete_dag_run, run_id="run-own"))
        # Manual create is the only proven POST route; no run context never grants ownership.
        self.assertTrue(decide("POST", run, trigger_dag_run))
        self.assertTrue(decide("POST", run, trigger_dag_run, clear_body_safe=None))
        self.assertFalse(decide("POST", run, bulk_dag_runs))
        self.assertFalse(decide("PUT", run, clear_dag_run, run_id=None))
        self.assertFalse(decide("PUT", run, clear_dag_run, run_id="~"))
        # The exact HTTP method is part of the grant: same endpoint with another verb denies.
        with request_context.use_request(
            _RoutedRequest(clear_dag_run, auth.ALLOWED_DAG_ID, "run-own", http_method="PATCH")
        ):
            self.assertFalse(manager.is_authorized_dag(method="PUT", access_entity=run, details=allowed, user=owner))
        with request_context.use_request(
            _RoutedRequest(trigger_dag_run, auth.ALLOWED_DAG_ID, None, http_method="PATCH")
        ):
            self.assertFalse(manager.is_authorized_dag(method="POST", access_entity=run, details=allowed, user=owner))
        self.assertFalse(decide("POST", run, trigger_dag_run, dag_id="some_other_dag"))
        # Middleware absent: every operator RUN grant fails closed; GET and admin stay allowed.
        self.assertFalse(manager.is_authorized_dag(method="POST", access_entity=run, details=allowed, user=owner))
        self.assertFalse(manager.is_authorized_dag(method="PUT", access_entity=run, details=allowed, user=owner))
        self.assertTrue(manager.is_authorized_dag(method="GET", details=allowed, user=owner))
        self.assertTrue(manager.is_authorized_dag(method="POST", access_entity=run, details=allowed, user=admin))

    def test_request_context_middleware_binds_and_resets_the_scope(self) -> None:
        seen: list[bool] = []

        async def app(scope, receive, send) -> None:
            seen.append(request_context.bound_request() is not None)

        middleware = request_context.BindRequestMiddleware(app)
        asyncio.run(middleware({"type": "http", "path": "/api/v2", "method": "GET"}, None, None))
        asyncio.run(middleware({"type": "lifespan"}, None, None))
        self.assertEqual(seen, [True, False])
        self.assertIsNone(request_context.bound_request())

    def test_transport_retry_classification_matrix(self) -> None:
        classify = run_ownership.classify_transport_retry

        eligible = classify(
            "failed",
            {
                "resolve_handoff": "success",
                "start_job": "success",
                "wait_for_job": "failed",
                "confirm_job": "upstream_failed",
            },
        )
        self.assertTrue(eligible.eligible)
        self.assertEqual(eligible.reason, "transport_recovery")
        self.assertEqual(eligible.cleared_tasks, ("confirm_job", "wait_for_job"))
        for state, tasks, reason in (
            ("running", {"wait_for_job": "failed"}, "run_active"),
            ("success", {"wait_for_job": "failed"}, "run_succeeded"),
            ("failed", {}, "no_failed_transport_task"),
            ("failed", {"resolve_handoff": "failed"}, "resolution_or_capture_failed"),
            ("failed", {"start_job": "failed"}, "resolution_not_success"),
            (
                "failed",
                {"resolve_handoff": "success", "confirm_job": "failed"},
                "publication_failed",
            ),
            (
                "failed",
                {"resolve_handoff": "success", "other_task": "failed"},
                "unknown_failed_task",
            ),
            (
                "failed",
                {"resolve_handoff": "success", "confirm_job": "upstream_failed"},
                "no_failed_transport_task",
            ),
            (
                "failed",
                {
                    "resolve_handoff": "success",
                    "wait_for_job": run_ownership.AMBIGUOUS_TASK_STATE,
                },
                "unknown_failed_task",
            ),
        ):
            with self.subTest(state=state, tasks=tasks):
                assessment = classify(state, tasks)
                self.assertFalse(assessment.eligible)
                self.assertEqual(assessment.reason, reason)
        start_retry = classify(
            "failed", {"resolve_handoff": "success", "start_job": "failed", "wait_for_job": "upstream_failed"}
        )
        self.assertTrue(start_retry.eligible)
        self.assertEqual(start_retry.cleared_tasks, ("start_job", "wait_for_job"))

    def test_split_transport_retry_preserves_the_engineering_boundary(self) -> None:
        classify = run_ownership.classify_transport_retry
        tasks = dict.fromkeys(run_ownership.KNOWN_TASKS, "upstream_failed")
        tasks.update(resolve_handoff="success", start_job="success", wait_for_job="failed")
        self.assertTrue(classify("failed", tasks).eligible)
        tasks.update(wait_for_job="success", fetch_capture="failed")
        self.assertTrue(classify("failed", tasks).eligible)
        for stage in ("run_generate", "run_verify", "run_publish"):
            with self.subTest(stage=stage):
                changed = {**tasks, "fetch_capture": "success", stage: "failed"}
                self.assertFalse(classify("failed", changed).eligible)

    def test_clear_body_candidate_matrix_and_replay(self) -> None:
        evaluate = request_context.evaluate_clear_candidate
        safe = b'{"dry_run": false, "only_failed": true, "only_new": false, "run_on_latest_version": false}'
        self.assertEqual(evaluate(safe, oversize=False), {"safe": True, "dry_run": False})
        precheck = b'{"dry_run": true, "only_failed": true, "only_new": false, "run_on_latest_version": false}'
        self.assertEqual(evaluate(precheck, oversize=False), {"safe": True, "dry_run": True})
        for raw, label in (
            (b'{"dry_run": false}', "missing fields"),
            (
                b'{"dry_run": false, "only_failed": false, "only_new": false, "run_on_latest_version": false}',
                "only_failed false",
            ),
            (
                b'{"dry_run": false, "only_failed": true, "only_new": true, "run_on_latest_version": false}',
                "only_new true",
            ),
            (
                b'{"dry_run": false, "only_failed": true, "only_new": false, "run_on_latest_version": true}',
                "latest version true",
            ),
            (b'{"dry_run": false, "only_failed": true, "only_new": false}', "latest missing"),
            (
                b'{"dry_run": false, "only_failed": true, "only_new": false, '
                b'"run_on_latest_version": false, "note": "x"}',
                "unknown field",
            ),
            (
                b'{"dry_run": false, "only_failed": true, "only_failed": true, '
                b'"only_new": false, "run_on_latest_version": false}',
                "duplicate key",
            ),
            (
                b'{"dry_run": 1, "only_failed": true, "only_new": false, "run_on_latest_version": false}',
                "non-bool",
            ),
            (b"not json", "malformed"),
        ):
            with self.subTest(case=label):
                self.assertEqual(evaluate(raw, oversize=False), {"safe": False})
        self.assertEqual(evaluate(safe, oversize=True), {"safe": False})

        observed: dict = {}

        async def app(scope, receive, send) -> None:
            chunks = []
            while True:
                message = await receive()
                if message.get("type") != "http.request":
                    break
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            observed["body"] = b"".join(chunks)
            observed["candidate"] = scope.get(request_context.CLEAR_BODY_SCOPE_KEY)

        middleware = request_context.BindRequestMiddleware(app)
        scope = {"type": "http", "method": "POST", "path": "/dags/x/dagRuns/run-1/clear"}
        messages = [
            {"type": "http.request", "body": safe[:20], "more_body": True},
            {"type": "http.request", "body": safe[20:], "more_body": False},
        ]

        async def receive() -> dict:
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        asyncio.run(middleware(scope, receive, None))
        self.assertEqual(observed["body"], safe)
        self.assertEqual(observed["candidate"], {"safe": True, "dry_run": False})

        calls: list = []

        async def other_app(scope, receive, send) -> None:
            calls.append(scope.get(request_context.CLEAR_BODY_SCOPE_KEY))

        async def no_body() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        asyncio.run(
            request_context.BindRequestMiddleware(other_app)(
                {"type": "http", "method": "POST", "path": "/dags/x/dagRuns"}, no_body, None
            )
        )
        self.assertEqual(calls, [None])

        # The real root scope carries the stable API prefix; capture must still select the route.
        prefixed: list = []

        async def prefixed_app(scope, receive, send) -> None:
            prefixed.append(scope.get(request_context.CLEAR_BODY_SCOPE_KEY))

        for scope_variant in (
            {"type": "http", "method": "POST", "path": "/api/v2/dags/x/dagRuns/r1/clear"},
            {
                "type": "http",
                "method": "POST",
                "path": "/dags/x/dagRuns/r1/clear",
                "root_path": "/api/v2",
            },
        ):
            with self.subTest(scope=scope_variant):
                asyncio.run(request_context.BindRequestMiddleware(prefixed_app)(dict(scope_variant), no_body, None))
        self.assertEqual(prefixed, [{"safe": False}, {"safe": False}])

    def test_clear_body_oversize_is_refused_promptly(self) -> None:
        invoked: list = []

        async def app(scope, receive, send) -> None:
            invoked.append(True)

        middleware = request_context.BindRequestMiddleware(app)
        scope = {"type": "http", "method": "POST", "path": "/api/v2/dags/x/dagRuns/r1/clear"}

        async def run_case(messages: list) -> tuple[int, list]:
            sent: list = []
            pending = list(messages)

            async def receive() -> dict:
                return pending.pop(0) if pending else {"type": "http.disconnect"}

            async def send(message: dict) -> None:
                sent.append(message)

            await middleware(scope, receive, send)
            status = next(m["status"] for m in sent if m["type"] == "http.response.start")
            return status, pending

        one_large = [{"type": "http.request", "body": b"x" * 5000, "more_body": False}]
        status, _ = asyncio.run(run_case(one_large))
        self.assertEqual(status, 413)
        chunked = [
            {"type": "http.request", "body": b"x" * 3000, "more_body": True},
            {"type": "http.request", "body": b"x" * 2000, "more_body": True},
            {"type": "http.request", "body": b"x" * 10, "more_body": False},
        ]
        status, remaining = asyncio.run(run_case(chunked))
        self.assertEqual(status, 413)
        # Reading stopped at the limit; the untouched remainder was never consumed.
        self.assertEqual(len(remaining), 1)
        self.assertEqual(invoked, [])

    def test_ownership_actor_parser_rejects_damaged_envelopes(self) -> None:
        parse = run_ownership.recorded_actor_principal
        canonical = auth.build_actor_name("cli_app:tenant-a:ou_owner", "崔工")
        self.assertEqual(parse(canonical), "cli_app:tenant-a:ou_owner")
        self.assertIsNone(parse(f"cli_app:tenant-a:ou_owner|{json.dumps('e\u0301', ensure_ascii=False)}"))
        self.assertIsNone(parse(f"cli_app:tenant-a:ou_owner|{json.dumps(' 崔工')}"))
        self.assertIsNone(parse(canonical + "A" * 600))
        self.assertIsNone(parse("cli_app:tenant-a:ou_owner"))
        self.assertIsNone(parse(None))

    def test_triggering_user_name_is_an_auth_owned_principal_name_envelope(self) -> None:
        manager = self.manager(_environment(self.root))
        worker = auth.FeishuUser(
            app_id="cli_app", open_id="ou_worker", name="崔工", avatar_url="", tenant_key="tenant-a"
        )
        token = manager.serialize_user(worker)

        def decode(value: str) -> str:
            principal, delimiter, encoded = value.partition(TRIGGERING_USER_NAME_DELIMITER)
            self.assertEqual(principal, "cli_app:tenant-a:ou_worker")
            self.assertEqual(delimiter, TRIGGERING_USER_NAME_DELIMITER)
            return json.loads(encoded)

        envelope = manager.deserialize_user(token).get_name()
        self.assertEqual(decode(envelope), "崔工")
        self.assertLessEqual(len(envelope), TRIGGERING_USER_NAME_LIMIT)
        # Long Unicode names fit the character-based column un-truncated.
        for name in ("A" * 128, "崔" * 128, "🙂" * 128, "崔|工🙂"):
            with self.subTest(name=name[:8]):
                long_user = dataclasses.replace(worker, name=name)
                long_envelope = long_user.get_name()
                self.assertLessEqual(len(long_envelope), TRIGGERING_USER_NAME_LIMIT)
                self.assertEqual(decode(long_envelope), name)
        # NFC normalization keeps canonically equivalent names in one recorded form.
        self.assertEqual(decode(dataclasses.replace(worker, name="e\u0301").get_name()), "é")
        # A genuine name equal to the open_id is preserved like any other.
        self.assertEqual(decode(dataclasses.replace(worker, name="ou_worker").get_name()), "ou_worker")
        # The exact column boundary: a 512-character actor value is accepted, 513 is refused.
        boundary = TRIGGERING_USER_NAME_LIMIT - len(worker.get_id()) - 1 - 2
        exact = dataclasses.replace(worker, name="A" * boundary).get_name()
        self.assertEqual(len(exact), TRIGGERING_USER_NAME_LIMIT)
        self.assertEqual(decode(exact), "A" * boundary)
        with self.assertRaises(ValueError):
            dataclasses.replace(worker, name="A" * (boundary + 1)).get_name()
        # Missing, control, bidi and surrogate names are rejected.
        for name in ("", "bad\nname", "evil\u202egniht", "bad\ud800name"):
            with self.subTest(name=repr(name[:12])), self.assertRaises(ValueError):
                dataclasses.replace(worker, name=name).get_name()
        # Sign-in fails closed for the same reasons instead of deferring to trigger time.
        from description_pipeline.orchestration.feishu_oauth import FeishuIdentity

        for name in ("", "A" * (boundary + 1)):
            with self.subTest(sign_in=repr(name[:12])), self.assertRaises(auth.FeishuAuthError):
                manager._user(FeishuIdentity(open_id="ou_worker", name=name, avatar_url="", tenant_key="tenant-a"))
        self.assertEqual(
            len(
                manager._user(
                    FeishuIdentity(open_id="ou_worker", name="A" * boundary, avatar_url="", tenant_key="tenant-a")
                ).get_name()
            ),
            TRIGGERING_USER_NAME_LIMIT,
        )
        # A genuine name equal to the open_id still signs in.
        same_identity = manager._user(
            FeishuIdentity(open_id="ou_worker", name="ou_worker", avatar_url="", tenant_key="tenant-a")
        )
        self.assertEqual(same_identity.get_name(), 'cli_app:tenant-a:ou_worker|"ou_worker"')

    def test_real_rest_dependencies_authorize_only_the_one_dag(self) -> None:
        """The pinned REST dependencies, not the helper alone, gate the operator's routes."""
        from airflow.api_fastapi import app as airflow_app
        from airflow.api_fastapi.core_api import security as core_security
        from airflow.models import DagModel

        manager = self.manager(_environment(self.root))
        previous = airflow_app._AuthManagerState.instance
        airflow_app._AuthManagerState.instance = manager
        self.addCleanup(setattr, airflow_app._AuthManagerState, "instance", previous)
        operator = auth.FeishuUser(
            app_id="cli_app", open_id="ou_worker", name="崔工", avatar_url="", tenant_key="tenant-a"
        )
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "root_path": "",
                "scheme": "https",
                "server": ("operator.example", 443),
                "client": ("127.0.0.1", 12345),
                "headers": [],
                "query_string": b"",
                "path_params": {"dag_id": auth.ALLOWED_DAG_ID},
            }
        )
        with mock.patch.object(DagModel, "get_team_name", return_value=None):
            with request_context.use_request(_RoutedRequest(trigger_dag_run, auth.ALLOWED_DAG_ID)):
                core_security.requires_access_dag("POST", auth.DagAccessEntity.RUN, auth.ALLOWED_DAG_ID)(
                    request, operator
                )
            for entity in (auth.DagAccessEntity.TASK_INSTANCE, auth.DagAccessEntity.XCOM, None):
                core_security.requires_access_dag("GET", entity, auth.ALLOWED_DAG_ID)(request, operator)
            for method, entity in (
                ("DELETE", None),
                ("POST", auth.DagAccessEntity.TASK_INSTANCE),
                ("PUT", auth.DagAccessEntity.RUN),
                ("DELETE", auth.DagAccessEntity.RUN),
                ("POST", None),
            ):
                with self.assertRaises(HTTPException) as caught:
                    core_security.requires_access_dag(method, entity, auth.ALLOWED_DAG_ID)(request, operator)
                self.assertEqual(caught.exception.status_code, 403)
            with self.assertRaises(HTTPException):
                core_security.requires_access_dag("POST", auth.DagAccessEntity.RUN, "other_dag")(request, operator)

    # -- state store ------------------------------------------------------

    def test_metadata_state_is_single_use_expired_and_shared_across_workers(self) -> None:
        first = auth.MetadataStateStore()
        second = auth.MetadataStateStore()
        state, challenge = first.issue(60.0)
        self.assertEqual(len(state), 43)
        self.assertEqual(len(challenge), 43)
        self.assertIsNone(second.consume("never-issued"))
        verifier = second.consume(state)
        self.assertTrue(verifier)
        # Single use, across two workers of the same metadata database.
        self.assertIsNone(second.consume(state))
        expired, _ = first.issue(-1.0)
        self.assertIsNone(second.consume(expired))
        with self.sessions() as session:
            stored = [str(row[0]) for row in session.execute(text("SELECT state_hash FROM feishu_auth_state"))]
        self.assertNotIn(state, stored)
        self.assertNotIn(state_digest(state), stored)


if __name__ == "__main__":
    unittest.main()
