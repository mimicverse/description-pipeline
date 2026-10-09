"""Pinned-Airflow tests for the Feishu auth manager and its metadata-table state.

The portable OAuth protocol runs without Airflow in ``test_feishu_oauth.py``; these tests need the
real Airflow 3.3.2 runtime (same guard as ``test_airflow_dag.py``) and run in the pinned
environment. The suite provisions its own isolated metadata database from the real Airflow models
(``revoked_token`` and the manager's own state table), binds ``airflow.settings`` to it for the
duration of the class, and never mutates the ambient Airflow home or process environment; the
production code keeps exactly one storage path.
"""

from __future__ import annotations

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
    from airflow.models.revoked_token import RevokedToken

    from description_pipeline.orchestration import feishu_auth as auth
    from description_pipeline.orchestration.feishu_oauth import STATE_COOKIE, state_digest

JWT_SECRET = "feishu-auth-manager-tests-" + "0" * 40


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
        self.assertEqual(client.get("/feishu/health").json(), {"configured": True})
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
        self.assertTrue(manager.is_authorized_dag(method="POST", user=operator, details=allowed))
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
        principal, delimiter, encoded = envelope.partition(auth.TRIGGERING_USER_NAME_DELIMITER)
        self.assertEqual(principal, "cli_app:tenant-a:ou_worker")
        self.assertEqual(delimiter, auth.TRIGGERING_USER_NAME_DELIMITER)
        self.assertEqual(json.loads(encoded), "崔工")
        with self.assertRaises(ValueError):
            manager.deserialize_user({**token, "tenant_key": "tenant-b"})
        # A token minted by a replaced enterprise app must not stay valid.
        with self.assertRaises(ValueError):
            manager.deserialize_user({**token, "app_id": "cli_replaced"})
        with self.assertRaises(ValueError):
            manager.deserialize_user({"sub": "ou_worker", "app_id": "cli_app", "tenant_key": ""})
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
        boundary = auth.TRIGGERING_USER_NAME_LIMIT - len("cli_app:tenant-a:ou_worker") - 1 - 2
        self.assertEqual(
            len(manager.deserialize_user({**token, "name": "A" * boundary}).get_name()),
            auth.TRIGGERING_USER_NAME_LIMIT,
        )
        with self.assertRaises(ValueError):
            manager.deserialize_user({**token, "name": "A" * (boundary + 1)})

    def test_triggering_user_name_is_an_auth_owned_principal_name_envelope(self) -> None:
        manager = self.manager(_environment(self.root))
        worker = auth.FeishuUser(
            app_id="cli_app", open_id="ou_worker", name="崔工", avatar_url="", tenant_key="tenant-a"
        )
        token = manager.serialize_user(worker)

        def decode(value: str) -> str:
            principal, delimiter, encoded = value.partition(auth.TRIGGERING_USER_NAME_DELIMITER)
            self.assertEqual(principal, "cli_app:tenant-a:ou_worker")
            self.assertEqual(delimiter, auth.TRIGGERING_USER_NAME_DELIMITER)
            return json.loads(encoded)

        envelope = manager.deserialize_user(token).get_name()
        self.assertEqual(decode(envelope), "崔工")
        self.assertLessEqual(len(envelope), auth.TRIGGERING_USER_NAME_LIMIT)
        # Long Unicode names fit the character-based column un-truncated.
        for name in ("A" * 128, "崔" * 128, "🙂" * 128, "崔|工🙂"):
            with self.subTest(name=name[:8]):
                long_user = dataclasses.replace(worker, name=name)
                long_envelope = long_user.get_name()
                self.assertLessEqual(len(long_envelope), auth.TRIGGERING_USER_NAME_LIMIT)
                self.assertEqual(decode(long_envelope), name)
        # NFC normalization keeps canonically equivalent names in one recorded form.
        self.assertEqual(decode(dataclasses.replace(worker, name="e\u0301").get_name()), "é")
        # A genuine name equal to the open_id is preserved like any other.
        self.assertEqual(decode(dataclasses.replace(worker, name="ou_worker").get_name()), "ou_worker")
        # The exact column boundary: a 512-character actor value is accepted, 513 is refused.
        boundary = auth.TRIGGERING_USER_NAME_LIMIT - len(worker.get_id()) - 1 - 2
        exact = dataclasses.replace(worker, name="A" * boundary).get_name()
        self.assertEqual(len(exact), auth.TRIGGERING_USER_NAME_LIMIT)
        self.assertEqual(decode(exact), "A" * boundary)
        with self.assertRaises(ValueError):
            dataclasses.replace(worker, name="A" * (boundary + 1)).get_name()
        # Missing, control, bidi and surrogate names are rejected.
        for name in ("", "bad\nname", "evil\u202egniht", "bad\ud800name"):
            with self.subTest(name=repr(name[:12])):
                with self.assertRaises(ValueError):
                    dataclasses.replace(worker, name=name).get_name()
        # Sign-in fails closed for the same reasons instead of deferring to trigger time.
        from description_pipeline.orchestration.feishu_oauth import FeishuIdentity

        for name in ("", "A" * (boundary + 1)):
            with self.subTest(sign_in=repr(name[:12])), self.assertRaises(auth.FeishuAuthError):
                manager._user(
                    FeishuIdentity(open_id="ou_worker", name=name, avatar_url="", tenant_key="tenant-a")
                )
        self.assertEqual(
            len(
                manager._user(
                    FeishuIdentity(
                        open_id="ou_worker", name="A" * boundary, avatar_url="", tenant_key="tenant-a"
                    )
                ).get_name()
            ),
            auth.TRIGGERING_USER_NAME_LIMIT,
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
            core_security.requires_access_dag("POST", auth.DagAccessEntity.RUN, auth.ALLOWED_DAG_ID)(request, operator)
            for entity in (auth.DagAccessEntity.TASK_INSTANCE, auth.DagAccessEntity.XCOM, None):
                core_security.requires_access_dag("GET", entity, auth.ALLOWED_DAG_ID)(request, operator)
            for method, entity in (("DELETE", None), ("POST", auth.DagAccessEntity.TASK_INSTANCE)):
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
