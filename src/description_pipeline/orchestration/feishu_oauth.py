"""Portable Feishu OAuth2 sign-in core for pinned Airflow 3.3.2.

Official Feishu web login: authorize at
``https://accounts.feishu.cn/open-apis/authen/v1/authorize`` and exchange the code at the current
v3 token endpoint ``https://accounts.feishu.cn/oauth/v3/token`` with an
``application/x-www-form-urlencoded`` body (``grant_type=authorization_code``, ``client_id``,
``client_secret``, ``code``, ``redirect_uri`` and ``code_verifier``). Never mix a Basic header
with body credentials; Feishu rejects that with error 20070. The v3 response is flat JSON
(``{"code": 0, "access_token": ..., "expires_in": ..., "token_type": "Bearer", "scope": ...}``);
the deprecated v2 shape is not accepted as a fallback. The signed-in identity is one explicit
``open_id`` from ``https://open.feishu.cn/open-apis/authen/v1/user_info`` bound to the configured
App ID and an allowlisted ``tenant_key``; there is no user_id/union_id fallback and no
cross-tenant provisioning.

This module carries no Airflow import: the toolkit runtime stays free of the Airflow dependency,
and ``description_pipeline.orchestration.feishu_auth`` binds these pieces to the pinned
``BaseAuthManager`` contract only inside the Airflow environment. Single-use state and the PKCE
verifier live in one restart-safe store shared by every api-server worker: the
``feishu_auth_state`` table in the Airflow metadata database.

Official references (read 2026-10-07):

* authorize (S256): https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/authen-v1/authorize/get
* token v3: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/get-user-access-token-v3
* user_info v1: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/authen-v1/user_info/get

Sign-in reads only ``open_id``, ``tenant_key``, ``name``/``en_name`` and the avatar from the v1
profile; no sensitive field (user_id, email, mobile, employment) is requested, so this enterprise
app needs no additional contact-directory permission. The authorize request omits ``scope`` and
``offline_access`` entirely: it is a sign-in-only app, not a generic OIDC client. A display name
is required: it is NFC-normalized and must be free of control, format, surrogate and separator
characters, and the open_id is never substituted where a username would be shown.
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
FEISHU_TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
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


def _post_form(url: str, payload: dict, *, opener=None) -> dict:
    request = urlrequest.Request(
        url,
        data=urlparse.urlencode(payload).encode("ascii"),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    open_url = opener or urlrequest.urlopen
    try:
        with open_url(request, timeout=20.0) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
    except (urlerror.URLError, urlerror.HTTPError, ValueError) as error:
        raise FeishuAuthError(f"Feishu token request failed: {error}") from error
    if not isinstance(data, dict):
        raise FeishuAuthError("Feishu token response is not a JSON object")
    if data.get("code") != 0:
        raise FeishuAuthError("Feishu token request was rejected")
    return data


def exchange_code(settings: FeishuSettings, code: str, verifier: str, *, opener=None) -> str:
    """Exchange the authorization code at the current v3 endpoint; exactly one auth style."""
    payload = _post_form(
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
        raise FeishuAuthError("Feishu token response does not match the pinned v3 shape")
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
