"""Feishu OAuth 2.0 sign-in core for pinned Airflow 3.3.2.

Authorization uses a state cookie and an S256 PKCE challenge. The server exchanges
one authorization code through the JSON token API, validates its integer ``code``
envelope and Bearer token, then reads the basic user profile. The authenticated
identity is the app-scoped ``open_id`` within an allowlisted ``tenant_key``.
Provider error logs contain only HTTP status, numeric code and recognized OAuth
error values; arbitrary provider text is discarded.

This module has no Airflow dependency. ``feishu_auth`` binds it to the platform's
authentication manager; the shared ``feishu_auth_state`` metadata table stores
single-use state and PKCE verifiers across API workers and restarts.

Official references:

* authorize: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/authen-v1/authorize/get
* token: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/get-user-access-token
* profile: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/authen-v1/user_info/get
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import json
import os
import re
import secrets
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

FEISHU_AUTHORIZE_BASE = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
FEISHU_USERINFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
DEFAULT_STATE_TTL = 300.0
STATE_COOKIE = "feishu_oauth_state"
_COMPOUND_PRINCIPAL = re.compile(r"[^:\s|]+(?::[^:\s|]+){2}\Z")
#: Unicode categories that may not appear in a display name: control, format (for example the
#: bidi overrides), surrogate and line/paragraph separators.
UNSAFE_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def sanitize_display_name(value: object) -> str:
    """One usable Feishu display name, NFC-normalized; empty when the value cannot be shown."""
    if not isinstance(value, str):
        return ""
    name = unicodedata.normalize("NFC", value).strip()
    if not name or any(unicodedata.category(character) in UNSAFE_NAME_CATEGORIES for character in name):
        return ""
    return name


#: Delimiter between the stable principal and the JSON-recorded display name stamped into
#: ``DagRun.triggering_user_name``; the JSON quoting keeps a name containing it unambiguous.
TRIGGERING_USER_NAME_DELIMITER = "|"
#: ``DagRun.triggering_user_name`` column length in the pinned Airflow metadata schema (the
#: pinned columns count characters, so any accepted Unicode name fits).
TRIGGERING_USER_NAME_LIMIT = 512


def recorded_display_name(value: object) -> str:
    """Validate one Feishu display name before it is recorded; raises ``ValueError`` otherwise."""
    name = sanitize_display_name(value)
    if not name:
        raise ValueError("Feishu display name is missing or contains unsafe control, format or separator characters")
    return name


def valid_actor_principal(value: object) -> bool:
    """A printable, opaque app/tenant/user identity with exactly three components."""
    return (
        isinstance(value, str)
        and _COMPOUND_PRINCIPAL.fullmatch(value) is not None
        and not any(unicodedata.category(character) in UNSAFE_NAME_CATEGORIES for character in value)
    )


def build_actor_name(principal: str, name: str) -> str:
    """The auth-owned actor value: stable principal plus recorded display name.

    Only a genuine, control-free display name is accepted; anything else raises instead of
    silently recording a run without its submitter named, or an actor value the pinned metadata
    column cannot hold.
    """
    if not valid_actor_principal(principal):
        raise ValueError("Feishu actor requires a valid app, tenant and user identity")
    recorded = recorded_display_name(name)
    envelope = f"{principal}{TRIGGERING_USER_NAME_DELIMITER}{json.dumps(recorded, ensure_ascii=False)}"
    if len(envelope) > TRIGGERING_USER_NAME_LIMIT:
        raise ValueError("the recorded actor identity exceeds the DagRun.triggering_user_name column")
    return envelope


class FeishuConfigError(RuntimeError):
    """The Feishu SSO settings are missing or unsafe; sign-in must fail closed."""


class FeishuAuthError(RuntimeError):
    """The Feishu authorization or token exchange failed."""


def _csv(value: str | None) -> frozenset[str]:
    return frozenset(item.strip() for item in str(value or "").split(",") if item.strip())


@dataclass(frozen=True)
class FeishuIdentity:
    open_id: str
    name: str
    avatar_url: str
    tenant_key: str


@dataclass(frozen=True)
class FeishuSettings:
    app_id: str
    app_secret: str = field(repr=False)
    tenant_keys: frozenset[str]
    redirect_uri: str
    admin_open_ids: frozenset[str]
    authorize_base: str = FEISHU_AUTHORIZE_BASE
    token_url: str = FEISHU_TOKEN_URL
    userinfo_url: str = FEISHU_USERINFO_URL
    state_ttl: float = DEFAULT_STATE_TTL

    @classmethod
    def from_environment(cls, environ: dict | None = None) -> FeishuSettings:
        env = os.environ if environ is None else environ
        required = ("FEISHU_APP_SECRET_FILE", "FEISHU_TENANT_KEYS", "FEISHU_REDIRECT_URI")
        missing = [key for key in required if not env.get(key)]
        if missing:
            raise FeishuConfigError(f"Feishu SSO is not configured; missing {', '.join(missing)}")
        raw_path = Path(str(env["FEISHU_APP_SECRET_FILE"]))
        path = Path(os.path.abspath(raw_path))
        if not raw_path.is_absolute() or path.is_symlink():
            raise FeishuConfigError("Feishu secret file must be an absolute regular, non-symlink path")
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as error:
            raise FeishuConfigError(f"cannot read Feishu secret file: {error}") from error
        if not path.is_file() or mode != 0o600:
            raise FeishuConfigError("Feishu secret file must be a regular file with mode 0600")
        try:
            secret = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise FeishuConfigError("Feishu secret file is not valid JSON") from error
        if not isinstance(secret, dict) or set(secret) != {"app_id", "app_secret"}:
            raise FeishuConfigError('Feishu secret file must be {"app_id", "app_secret"} only')
        app_id = secret["app_id"].strip() if isinstance(secret["app_id"], str) else ""
        app_secret = secret["app_secret"].strip() if isinstance(secret["app_secret"], str) else ""
        if not app_id or not app_secret:
            raise FeishuConfigError('Feishu secret file needs non-empty string "app_id" and "app_secret"')
        tenant_keys = _csv(env["FEISHU_TENANT_KEYS"])
        if not tenant_keys:
            raise FeishuConfigError("FEISHU_TENANT_KEYS must list at least one approved tenant_key")
        redirect_uri = str(env["FEISHU_REDIRECT_URI"]).strip()
        if not redirect_uri.startswith("https://"):
            raise FeishuConfigError("FEISHU_REDIRECT_URI must be an absolute https callback URL")
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            tenant_keys=tenant_keys,
            redirect_uri=redirect_uri,
            admin_open_ids=_csv(env.get("FEISHU_ADMIN_OPEN_IDS")),
        )


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(settings: FeishuSettings, state: str, challenge: str, *, scope: str | None = None) -> str:
    query = {
        "client_id": settings.app_id,
        "redirect_uri": settings.redirect_uri,
        "response_type": "code",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scope is not None:
        query["scope"] = scope
    query = urlparse.urlencode(query)
    return f"{settings.authorize_base}?{query}"


#: Recognized standard OAuth error values (RFC 6749 section 5.2 and its registration registry).
_OAUTH_ERRORS = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
        "access_denied",
        "server_error",
        "temporarily_unavailable",
    }
)
_ERROR_BODY_LIMIT = 8192


def token_error_summary(status: int, payload: object) -> str:
    """Bounded typed summary of a provider token error; untrusted prose is never echoed.

    Only the HTTP status, an integer provider ``code`` (booleans excluded) and a recognized
    standard OAuth ``error`` enum survive. There is no endpoint or content-type fallback: the JSON
    request style is the single supported exchange.
    """
    parts = [f"HTTP {int(status)}"]
    if isinstance(payload, dict):
        code = payload.get("code")
        if isinstance(code, int) and not isinstance(code, bool):
            parts.append(f"provider_code={code}")
        error = payload.get("error")
        if isinstance(error, str) and error in _OAUTH_ERRORS:
            parts.append(f"error={error}")
    return " ".join(parts)


def _http_error_summary(error: urlerror.HTTPError) -> str:
    """Parse a bounded error body and reduce it to the typed summary."""
    try:
        raw = error.read(_ERROR_BODY_LIMIT).decode("utf-8", "replace")
        payload = json.loads(raw or "{}")
    except (ValueError, UnicodeDecodeError):
        payload = None
    return token_error_summary(error.code, payload)


def _post_json(url: str, payload: dict, *, opener=None) -> dict:
    request = urlrequest.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
    )
    open_url = opener or urlrequest.urlopen
    try:
        with open_url(request, timeout=20.0) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
    except urlerror.HTTPError as error:
        raise FeishuAuthError(f"Feishu token request failed: {_http_error_summary(error)}") from error
    except (urlerror.URLError, ValueError) as error:
        raise FeishuAuthError(f"Feishu token request failed: {error}") from error
    if not isinstance(data, dict):
        raise FeishuAuthError("Feishu token response is not a JSON object")
    code = data.get("code")
    if not isinstance(code, int) or isinstance(code, bool):
        raise FeishuAuthError("Feishu token response does not carry the documented integer code envelope")
    if code != 0:
        raise FeishuAuthError(f"Feishu token request was rejected: {token_error_summary(200, data)}")
    return data


def exchange_code(settings: FeishuSettings, code: str, verifier: str, *, opener=None) -> str:
    """Exchange the authorization code at the documented v2 endpoint; exactly one auth style."""
    payload = _post_json(
        settings.token_url,
        {
            "grant_type": "authorization_code",
            "client_id": settings.app_id,
            "client_secret": settings.app_secret,
            "code": code,
            "redirect_uri": settings.redirect_uri,
            "code_verifier": verifier,
        },
        opener=opener,
    )
    token = str(payload.get("access_token") or "").strip()
    expires_in = payload.get("expires_in")
    token_type = str(payload.get("token_type") or "").strip()
    if (
        not token
        or not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or expires_in <= 0
        or token_type.lower() != "bearer"
    ):
        raise FeishuAuthError("Feishu token response does not match the documented v2 success shape")
    return token


def fetch_identity(settings: FeishuSettings, access_token: str, *, opener=None) -> FeishuIdentity:
    request = urlrequest.Request(
        settings.userinfo_url,
        method="GET",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    open_url = opener or urlrequest.urlopen
    try:
        with open_url(request, timeout=20.0) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (urlerror.URLError, urlerror.HTTPError, ValueError) as error:
        raise FeishuAuthError(f"Feishu profile request failed: {error}") from error
    if not isinstance(payload, dict):
        raise FeishuAuthError("Feishu profile response is not a JSON object")
    body = payload.get("data")
    if payload.get("code") != 0 or not isinstance(body, dict):
        raise FeishuAuthError("Feishu profile response does not match the pinned v1 user_info shape")
    open_id = body["open_id"].strip() if isinstance(body.get("open_id"), str) else ""
    tenant_key = body["tenant_key"].strip() if isinstance(body.get("tenant_key"), str) else ""
    if not open_id or not tenant_key:
        raise FeishuAuthError("Feishu profile must carry one explicit open_id and tenant_key")
    if tenant_key not in settings.tenant_keys:
        raise FeishuAuthError("Feishu tenant is not approved for this Airflow deployment")
    # A display name is required and the open_id is never substituted: an identifier must not be
    # shown where a username belongs, and a new run must not silently lack its recorded submitter.
    name = next(
        (
            candidate
            for candidate in (sanitize_display_name(body.get("name")), sanitize_display_name(body.get("en_name")))
            if candidate
        ),
        "",
    )
    if not name:
        raise FeishuAuthError("Feishu profile carries no usable display name (name or en_name)")
    avatar_field = body.get("avatar_url") if isinstance(body.get("avatar_url"), str) else body.get("avatar_thumb")
    avatar = avatar_field.strip() if isinstance(avatar_field, str) else ""
    return FeishuIdentity(open_id=open_id, name=name, avatar_url=avatar, tenant_key=tenant_key)


def state_cookie_header(state: str, *, path: str = "/") -> str:
    """Browser-bound OAuth state cookie; the callback must require this exact value."""
    return f"{STATE_COOKIE}={state}; Path={path}; HttpOnly; Secure; SameSite=Lax"


def state_cookie_matches(cookie_value: str | None, state: str) -> bool:
    return bool(cookie_value) and hmac.compare_digest(str(cookie_value), str(state))


def state_digest(state: str) -> str:
    """The stored key for one state value; the raw state never leaves the browser and the cookie."""
    return hashlib.sha256(str(state).encode("ascii", "replace")).hexdigest()
