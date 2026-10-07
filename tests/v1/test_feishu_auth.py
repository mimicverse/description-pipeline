"""Pinned-Airflow tests for the Feishu auth manager and its metadata-table state.

The portable OAuth protocol runs without Airflow in ``test_feishu_oauth.py``; these tests need the
real Airflow 3.3.2 runtime (same guard as ``test_airflow_dag.py``) and run in the pinned
environment. The state store is exercised through its real SQL against a temporary SQLAlchemy
engine, so no separate test-only storage mode exists in production code.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

# conf reads this at import time; keep the test JWTs deterministic and independent of AIRFLOW_HOME.
os.environ.setdefault("AIRFLOW__API_AUTH__JWT_SECRET", "feishu-auth-manager-tests-" + "0" * 40)

AIRFLOW_AVAILABLE = importlib.util.find_spec("airflow") is not None

if AIRFLOW_AVAILABLE:
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from description_pipeline.orchestration import feishu_auth as auth
    from description_pipeline.orchestration.feishu_oauth import STATE_COOKIE


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
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = create_engine(f"sqlite:///{self.root / 'metadata.db'}")
        self.sessions = sessionmaker(bind=self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmp.cleanup()

    def manager(self, environ: dict[str, str] | None = None) -> auth.FeishuAuthManager:
        manager = auth.FeishuAuthManager()
        real_store = auth.MetadataStateStore
        store = self.sessions
        with (
            mock.patch.dict(os.environ, environ or {}, clear=True),
            mock.patch.object(auth, "MetadataStateStore", lambda: real_store(session_factory=store)),
        ):
            manager.init()
        return manager

    def client(self, manager: auth.FeishuAuthManager) -> "TestClient":
        # https keeps the Secure SSO cookies flowing, and redirects stay observable.
        return TestClient(manager.get_fastapi_app(), base_url="https://operator.example", follow_redirects=False)

    def identity(self, *, open_id: str = "ou_worker", tenant: str = "tenant-a") -> auth.FeishuIdentity:
        return auth.FeishuIdentity(open_id=open_id, name="崔工", avatar_url="https://avatar/u", tenant_key=tenant)

    def authenticate(self, manager: auth.FeishuAuthManager, client: TestClient, *, next_url: str = "/ui/dags") -> str:
        """Run one complete sign-in and return the raw Airflow JWT the callback issued."""
        login = client.get(f"/feishu/login?next={next_url}")
        self.assertEqual(login.status_code, 302, login.text)
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        client.cookies.set(STATE_COOKIE, state)
        with (
            mock.patch.object(auth, "exchange_code", return_value="u-token"),
            mock.patch.object(auth, "fetch_identity", return_value=self.identity()),
        ):
            callback = client.get(f"/feishu/callback?code=code-1&state={state}")
        self.assertEqual(callback.status_code, 303, callback.text)
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
        response = client.get("/feishu/login?next=/ui/dags/solidworks_to_urdf")
        self.assertEqual(response.status_code, 302)
        location = response.headers["location"]
        query = parse_qs(urlsplit(location).query)
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
        token = self.authenticate(manager, client, next_url="/ui/dags/solidworks_to_urdf")
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
                "name": "崔工",
                "avatar_url": "https://avatar/u",
                "tenant_key": "tenant-a",
                "role": "OPERATOR",
            },
        )
        client.cookies.set(auth.COOKIE_NAME_JWT_TOKEN, "not-a-jwt")
        self.assertEqual(client.get("/feishu/profile").status_code, 401)

    # -- authorization ----------------------------------------------------

    def test_operator_reads_and_triggers_only_the_one_workflow(self) -> None:
        manager = self.manager(_environment(self.root))
        operator = auth.FeishuUser(open_id="ou_worker", name="崔工", avatar_url="", tenant_key="tenant-a")
        admin = auth.FeishuUser(open_id="ou_admin", name="管理员", avatar_url="", tenant_key="tenant-a")
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
            auth.FeishuUser(open_id="ou_worker", name="崔工", avatar_url="", tenant_key="tenant-a")
        )
        self.assertEqual(manager.deserialize_user(token).open_id, "ou_worker")
        with self.assertRaises(ValueError):
            manager.deserialize_user({**token, "tenant_key": "tenant-b"})
        with self.assertRaises(ValueError):
            manager.deserialize_user({"sub": "ou_worker", "name": "崔工", "avatar_url": "", "tenant_key": ""})
        # A role claim in the token is ignored; administration is the configured allowlist only.
        impersonation = {**token, "role": "ADMIN"}
        self.assertEqual(manager.role_of(manager.deserialize_user(impersonation)), auth.ROLE_OPERATOR)

    # -- state store ------------------------------------------------------

    def test_metadata_state_is_single_use_expired_and_shared_across_workers(self) -> None:
        first = auth.MetadataStateStore(session_factory=self.sessions)
        second = auth.MetadataStateStore(session_factory=self.sessions)
        state, challenge = first.issue(60.0, "/ui/dags")
        self.assertEqual(len(challenge), 43)
        self.assertIsNone(second.consume("never-issued"))
        verifier, next_url = second.consume(state)
        self.assertTrue(verifier)
        self.assertEqual(next_url, "/ui/dags")
        self.assertIsNone(second.consume(state))
        expired, _ = first.issue(-1.0)
        self.assertIsNone(second.consume(expired))
        with self.sessions() as session:
            stored = [str(row[0]) for row in session.execute(text("SELECT state_hash FROM feishu_auth_state"))]
        self.assertNotIn(state, stored)


if __name__ == "__main__":
    unittest.main()
