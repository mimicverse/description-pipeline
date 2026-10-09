"""Portable Feishu OAuth core: settings, PKCE, state cookie, token exchange and identity.

These run in the toolkit runtime, which intentionally has no Airflow dependency; the auth manager
and its metadata-database state live in ``test_feishu_auth.py`` and run in the pinned Airflow
environment.
"""

from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
import urllib.parse
from urllib import error as urlerror
from pathlib import Path

from description_pipeline.orchestration.feishu_oauth import (
    FEISHU_TOKEN_URL,
    STATE_COOKIE,
    FeishuAuthError,
    FeishuConfigError,
    FeishuSettings,
    build_actor_name,
    build_authorize_url,
    exchange_code,
    fetch_identity,
    pkce_pair,
    state_cookie_header,
    state_cookie_matches,
    state_digest,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


class _Recorder:
    def __init__(self, payload: dict, *, status: int = 200, error_body: dict | None = None) -> None:
        self.payload = payload
        self.status = status
        self.error_body = error_body
        self.requests: list[dict] = []

    def __call__(self, request, timeout=None):  # noqa: ANN001
        headers = {key.lower(): value for key, value in request.header_items()}
        body = None
        if request.data:
            raw = request.data.decode("utf-8")
            if "json" in headers.get("content-type", ""):
                body = json.loads(raw)
            else:
                body = {key: values[0] for key, values in urllib.parse.parse_qs(raw).items()}
        self.requests.append(
            {
                "url": request.full_url,
                "headers": headers,
                "body": body,
            }
        )
        if self.status >= 400:
            raise urlerror.HTTPError(
                request.full_url,
                self.status,
                "Bad Request",
                {},
                io.BytesIO(json.dumps(self.error_body or {}).encode("utf-8")),
            )
        return _Response(self.payload)


class FeishuCoreTests(unittest.TestCase):
    def test_actor_identity_rejects_malformed_principals_without_restricting_opaque_ids(self) -> None:
        for principal in ("cli_app:tenant-a:ou_worker", "cli.app:租户:opaque.user"):
            with self.subTest(principal=principal):
                self.assertEqual(build_actor_name(principal, "崔工"), f'{principal}|"崔工"')
        for principal in (
            None,
            123,
            "",
            "not-a-principal",
            "cli:ou",
            "cli:tenant:ou:worker",
            "cli:tenant:ou worker",
            "cli:tenant:ou|worker",
            "cli:tenant:ou\x00worker",
            "cli:tenant:ou\u202eworker",
        ):
            with self.subTest(principal=repr(principal)), self.assertRaises(ValueError):
                build_actor_name(principal, "崔工")

    def settings(self, tmp: str, **overrides) -> FeishuSettings:
        secret = Path(tmp) / "feishu.json"
        secret.write_text(json.dumps({"app_id": "cli_app", "app_secret": "s3cret"}), encoding="utf-8")
        os.chmod(secret, 0o600)
        env = {
            "FEISHU_APP_SECRET_FILE": str(secret),
            "FEISHU_TENANT_KEYS": "tenant-a",
            "FEISHU_REDIRECT_URI": "https://10.0.0.235:8443/auth/feishu/callback",
            **overrides,
        }
        return FeishuSettings.from_environment(env)

    @staticmethod
    def direct(**overrides) -> FeishuSettings:
        """The protocol pieces need no secret file; file semantics are tested separately."""
        values = {
            "app_id": "cli_app",
            "app_secret": "s3cret",
            "tenant_keys": frozenset({"tenant-a"}),
            "redirect_uri": "https://10.0.0.235:8443/auth/feishu/callback",
            "admin_open_ids": frozenset(),
        }
        values.update(overrides)
        return FeishuSettings(**values)

    @unittest.skipIf(os.name == "nt", "POSIX 0600 secret-file semantics")
    def test_settings_fail_closed(self) -> None:
        with self.assertRaises(FeishuConfigError):
            FeishuSettings.from_environment({})
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "feishu.json"
            secret.write_text(json.dumps({"app_id": "a", "app_secret": "b"}), encoding="utf-8")
            os.chmod(secret, 0o644)
            with self.assertRaises(FeishuConfigError):
                FeishuSettings.from_environment(
                    {
                        "FEISHU_APP_SECRET_FILE": str(secret),
                        "FEISHU_TENANT_KEYS": "tenant-a",
                        "FEISHU_REDIRECT_URI": "https://host/auth/feishu/callback",
                    }
                )

    @unittest.skipIf(os.name == "nt", "POSIX 0600 secret-file and symlink semantics")
    def test_settings_are_absolute_regular_non_symlink_and_repr_hides_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(tmp, FEISHU_TOKEN_URL="https://attacker.example/token")
            self.assertEqual(FEISHU_TOKEN_URL, "https://open.feishu.cn/open-apis/authen/v2/oauth/token")
            self.assertNotEqual(FEISHU_TOKEN_URL, "https://accounts.feishu.cn/oauth/v3/token")
            self.assertNotIn("/authen/v1/access_token", FEISHU_TOKEN_URL)
            self.assertEqual(settings.token_url, FEISHU_TOKEN_URL)
            self.assertNotIn("s3cret", repr(settings))
            secret = Path(tmp) / "feishu.json"
            link = Path(tmp) / "link.json"
            link.symlink_to(secret)
            base = {
                "FEISHU_TENANT_KEYS": "tenant-a",
                "FEISHU_REDIRECT_URI": "https://host/auth/feishu/callback",
            }
            with self.assertRaises(FeishuConfigError):
                FeishuSettings.from_environment({**base, "FEISHU_APP_SECRET_FILE": str(link)})
            secret.write_text(json.dumps({"app_id": "a", "app_secret": "b", "extra": "no"}), encoding="utf-8")
            os.chmod(secret, 0o600)
            with self.assertRaises(FeishuConfigError):
                FeishuSettings.from_environment({**base, "FEISHU_APP_SECRET_FILE": str(secret)})
            for coercible in ({"app_id": 5, "app_secret": "b"}, {"app_id": "a", "app_secret": ["x"]}):
                secret.write_text(json.dumps(coercible), encoding="utf-8")
                os.chmod(secret, 0o600)
                with self.subTest(secret=coercible), self.assertRaises(FeishuConfigError):
                    FeishuSettings.from_environment({**base, "FEISHU_APP_SECRET_FILE": str(secret)})

    def test_oauth_state_is_browser_bound(self) -> None:
        header = state_cookie_header("state-1")
        self.assertIn(f"{STATE_COOKIE}=state-1", header)
        self.assertIn("HttpOnly", header)
        self.assertIn("Secure", header)
        self.assertIn("SameSite=Lax", header)
        self.assertTrue(state_cookie_matches("state-1", "state-1"))
        self.assertFalse(state_cookie_matches("state-2", "state-1"))
        self.assertFalse(state_cookie_matches(None, "state-1"))

    def test_authorize_url_carries_s256_challenge_without_secret(self) -> None:
        settings = self.direct()
        _verifier, challenge = pkce_pair()
        state = "state-1"
        url = build_authorize_url(settings, state, challenge)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["client_id"], ["cli_app"])
        self.assertEqual(query["state"], [state])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn("scope", query)
        self.assertNotIn("offline_access", url)
        self.assertNotIn("s3cret", url)

    def test_state_digest_never_stores_the_raw_state(self) -> None:
        digest = state_digest("state-1")
        self.assertEqual(len(digest), 64)
        self.assertNotIn("state-1", digest)
        self.assertEqual(digest, state_digest("state-1"))

    def test_exchange_uses_json_body_and_pkce_without_basic_auth(self) -> None:
        settings = self.direct()
        recorder = _Recorder(
            {"code": 0, "access_token": "u-token", "expires_in": 7200, "token_type": "Bearer", "scope": "openid"}
        )
        token = exchange_code(settings, "code-1", "verifier-1", opener=recorder)
        self.assertEqual(token, "u-token")
        request = recorder.requests[0]
        self.assertEqual(request["url"], settings.token_url)
        self.assertEqual(request["url"], "https://open.feishu.cn/open-apis/authen/v2/oauth/token")
        self.assertEqual(request["headers"]["content-type"], "application/json; charset=utf-8")
        self.assertNotIn("authorization", request["headers"])
        self.assertEqual(request["body"]["grant_type"], "authorization_code")
        self.assertEqual(request["body"]["client_id"], "cli_app")
        self.assertEqual(request["body"]["code"], "code-1")
        self.assertEqual(request["body"]["code_verifier"], "verifier-1")
        self.assertEqual(request["body"]["redirect_uri"], settings.redirect_uri)
        for bad in (
            # the older data-wrapped access-token shape is not accepted
            {"code": 0, "data": {"access_token": "u"}},
            # the integer code envelope is mandatory: an absent or boolean code is not success
            {"access_token": "u2", "expires_in": 7200, "token_type": "Bearer"},
            {"code": True, "access_token": "u", "expires_in": 7200, "token_type": "Bearer"},
            {"code": False, "access_token": "u", "expires_in": 7200, "token_type": "Bearer"},
            {"code": 7, "access_token": "u", "expires_in": 7200, "token_type": "Bearer"},
            {"code": 0, "access_token": "", "expires_in": 7200, "token_type": "Bearer"},
            {"code": 0, "access_token": "u", "token_type": "Bearer"},
            {"code": 0, "access_token": "u", "expires_in": 0, "token_type": "Bearer"},
            {"code": 0, "access_token": "u", "expires_in": 7200, "token_type": "MAC"},
        ):
            with self.subTest(bad=bad), self.assertRaises(FeishuAuthError):
                exchange_code(settings, "code-1", "verifier-1", opener=_Recorder(bad))

    def test_exchange_reports_only_bounded_typed_provider_diagnostics(self) -> None:
        settings = self.direct()
        secret_prose = "code=code-secret verifier=verifier-secret client_secret=s3cret\ninjected"
        failing = _Recorder(
            {},
            status=400,
            error_body={
                "error": "invalid_request",
                "error_description": secret_prose,
                "msg": secret_prose,
                "code": 20049,
                "nested": {"error_description": secret_prose, "code": 999},
            },
        )
        with self.assertRaises(FeishuAuthError) as raised:
            exchange_code(settings, "code-secret", "verifier-secret", opener=failing)
        message = str(raised.exception)
        self.assertIn("HTTP 400", message)
        self.assertIn("error=invalid_request", message)
        self.assertIn("provider_code=20049", message)
        for leaked in ("code-secret", "verifier-secret", "s3cret", "injected", "\n"):
            self.assertNotIn(leaked, message)
        # Unknown enums, boolean "codes" and nested values are dropped entirely.
        noisy = _Recorder(
            {},
            status=400,
            error_body={"error": "custom-error", "code": True, "msg": secret_prose},
        )
        with self.assertRaises(FeishuAuthError) as raised:
            exchange_code(settings, "code-secret", "verifier-secret", opener=noisy)
        self.assertEqual(str(raised.exception), "Feishu token request failed: HTTP 400")
        # A success-status payload without the documented integer code envelope is refused.
        for shapeless in (
            {"access_token": "u", "expires_in": 7200, "token_type": "Bearer"},
            {"code": True, "access_token": "u", "expires_in": 7200, "token_type": "Bearer"},
        ):
            with self.subTest(shapeless=shapeless), self.assertRaises(FeishuAuthError) as raised:
                exchange_code(settings, "code-secret", "verifier-secret", opener=_Recorder(shapeless))
            self.assertEqual(
                str(raised.exception),
                "Feishu token response does not carry the documented integer code envelope",
            )
        # A rejected success-status payload uses the same bounded formatter.
        rejected = _Recorder({"code": 20049, "msg": secret_prose, "error_description": secret_prose})
        with self.assertRaises(FeishuAuthError) as raised:
            exchange_code(settings, "code-secret", "verifier-secret", opener=rejected)
        message = str(raised.exception)
        self.assertIn("HTTP 200", message)
        self.assertIn("provider_code=20049", message)
        self.assertNotIn("s3cret", message)

    def test_identity_requires_open_id_and_allowlisted_tenant(self) -> None:
        settings = self.direct()
        good = _Recorder(
            {"code": 0, "data": {"open_id": "ou_x", "tenant_key": "tenant-a", "name": "崔工", "avatar_url": "u"}}
        )
        identity = fetch_identity(settings, "u-token", opener=good)
        self.assertEqual(identity.open_id, "ou_x")
        self.assertEqual(identity.name, "崔工")
        self.assertEqual(good.requests[0]["headers"]["authorization"], "Bearer u-token")
        self.assertEqual(good.requests[0]["headers"]["content-type"], "application/json; charset=utf-8")
        # en_name is honored and the name is NFC-normalized.
        en_only = _Recorder({"code": 0, "data": {"open_id": "ou_x", "tenant_key": "tenant-a", "en_name": "Cui"}})
        self.assertEqual(fetch_identity(settings, "u-token", opener=en_only).name, "Cui")
        combining = _Recorder({"code": 0, "data": {"open_id": "ou_x", "tenant_key": "tenant-a", "name": "e\u0301"}})
        self.assertEqual(fetch_identity(settings, "u-token", opener=combining).name, "é")
        # A genuine display name is preserved even when it equals the open_id (no heuristic).
        equals = _Recorder({"code": 0, "data": {"open_id": "ou_x", "tenant_key": "tenant-a", "name": "ou_x"}})
        self.assertEqual(fetch_identity(settings, "u-token", opener=equals).name, "ou_x")
        # A missing, control, format or bidi name is refused; the open_id is never substituted.
        for bad_name in (
            {"open_id": "ou_x", "tenant_key": "tenant-a"},
            {"open_id": "ou_x", "tenant_key": "tenant-a", "name": ""},
            {"open_id": "ou_x", "tenant_key": "tenant-a", "name": 123},
            {"open_id": "ou_x", "tenant_key": "tenant-a", "name": "bad\nname"},
            {"open_id": "ou_x", "tenant_key": "tenant-a", "name": "evil\u202egniht"},
        ):
            with self.subTest(bad_name=bad_name.get("name")), self.assertRaises(FeishuAuthError):
                fetch_identity(settings, "u-token", opener=_Recorder({"code": 0, "data": bad_name}))
        for payload in (
            {"code": 0, "data": {"tenant_key": "tenant-a"}},
            {"code": 0, "data": {"open_id": "ou_x", "tenant_key": "other"}},
            {"code": 0, "data": []},
        ):
            with self.subTest(payload=payload), self.assertRaises(FeishuAuthError):
                fetch_identity(settings, "u-token", opener=_Recorder(payload))
