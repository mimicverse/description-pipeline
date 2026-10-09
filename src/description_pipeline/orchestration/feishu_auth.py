"""Feishu SSO as the auth manager of the pinned Airflow 3.3.2 environment.

This module is imported only inside the Linux Airflow runtime (``[core] auth_manager``); the
toolkit runtime never imports Airflow and keeps its OAuth/PKCE core in
``description_pipeline.orchestration.feishu_oauth``. One enterprise app signs operators in at
``/auth/feishu/login``; the callback mints the standard Airflow JWT cookie, and the manager's
``get_name()`` stamps the stable compound principal ``app_id:tenant_key:open_id`` together with
the Feishu-verified display name into Airflow's ``triggering_user_name``
(``<principal>|<json name>``), so every Airflow run record carries both a durable audit identity
and the authenticated submitter name. The friendly name stays a separate claim and is also
served through ``/auth/feishu/profile`` for the operator page.

Authorization is deliberately one workflow wide: every allowlisted-tenant user is an operator who
may read and trigger ``solidworks_to_urdf`` only, and only the explicit
``FEISHU_ADMIN_OPEN_IDS`` allowlist grants administrative access. An unconfigured installation
keeps serving: ``init()`` records the configuration problem and ``/auth/feishu/health`` reports
``configured: false`` instead of aborting the api-server.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jwt import InvalidTokenError
from sqlalchemy import text

from airflow.api_fastapi.app import AUTH_MANAGER_FASTAPI_APP_PREFIX, get_cookie_path
from airflow.api_fastapi.auth.managers.base_auth_manager import COOKIE_NAME_JWT_TOKEN, BaseAuthManager
from airflow.api_fastapi.auth.managers.models.base_user import BaseUser
from airflow.api_fastapi.auth.managers.models.resource_details import DagAccessEntity, DagDetails, TeamDetails
from airflow.api_fastapi.common.types import MenuItem

from description_pipeline.orchestration.feishu_oauth import (
    STATE_COOKIE,
    FeishuAuthError,
    FeishuConfigError,
    FeishuIdentity,
    FeishuSettings,
    build_actor_name,
    build_authorize_url,
    exchange_code,
    fetch_identity,
    pkce_pair,
    recorded_display_name,
    state_cookie_header,
    state_cookie_matches,
    state_digest,
)
from description_pipeline.orchestration.request_context import bound_request
from description_pipeline.orchestration.run_ownership import (
    MANUAL_CREATE_ENDPOINT,
    SINGLE_RUN_CLEAR_ENDPOINT,
    owner_retry_allowed,
    routed_route,
)

if TYPE_CHECKING:
    from airflow.api_fastapi.auth.managers.base_auth_manager import ResourceMethod
    from airflow.api_fastapi.auth.managers.models.resource_details import (
        AccessView,
        AssetAliasDetails,
        AssetDetails,
        ConfigurationDetails,
        ConnectionDetails,
        PoolDetails,
        VariableDetails,
    )

log = logging.getLogger(__name__)

#: The one workflow operators may read and trigger.
ALLOWED_DAG_ID = "solidworks_to_urdf"
ROLE_ADMIN = "ADMIN"
ROLE_OPERATOR = "OPERATOR"
#: How long a sign-in attempt may stay pending between /login and /callback.
STATE_TTL_SECONDS = 300.0
STATE_COOKIE_PATH = f"{AUTH_MANAGER_FASTAPI_APP_PREFIX}/feishu"


def _login_page(message: str, status_code: int) -> HTMLResponse:
    """A fixed, escaped-by-construction sign-in failure page; no request data is interpolated."""
    body = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        "<title>飞书登录</title></head><body>"
        '<main style="font-family:system-ui;max-width:32rem;margin:15vh auto;line-height:1.7">'
        f'<h1 style="font-size:1.2rem">飞书登录未完成</h1><p>{message}</p>'
        '<p><a href="/">返回操作平台</a></p></main></body></html>'
    )
    return HTMLResponse(body, status_code=status_code)


@dataclass(frozen=True)
class FeishuUser(BaseUser):
    """One signed-in Feishu identity; the role is derived from configuration, never from the token.

    The principal is the compound ``app_id:tenant_key:open_id`` so that replacing the enterprise
    app, or a token outliving its tenant, can never impersonate the previous installation.
    """

    app_id: str
    open_id: str
    name: str
    avatar_url: str
    tenant_key: str

    def get_id(self) -> str:
        return f"{self.app_id}:{self.tenant_key}:{self.open_id}"

    def get_name(self) -> str:
        """Airflow's durable actor value for every run this principal triggers.

        It is minted only from the signed JWT claims: the stable principal plus the
        Feishu-verified display name. A direct API caller therefore can never stamp a name they
        did not authenticate as; a claim without a usable name raises instead of recording a run
        whose submitter is unnamed.
        """
        return build_actor_name(self.get_id(), self.name)


class MetadataStateStore:
    """Single-use OAuth state and its PKCE verifier in the Airflow metadata database.

    Every api-server worker and every restart uses this one table on the pinned ``airflow_meta``
    connection. ``CREATE TABLE IF NOT EXISTS`` is idempotent and needs no migration; consuming a
    state deletes the row, so a replayed callback can never reuse an authorization code.
    """

    def __init__(self, session_factory: Any | None = None) -> None:
        if session_factory is None:
            from airflow.settings import Session

            session_factory = Session
        self._session = session_factory
        with self._session() as session:
            session.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS feishu_auth_state ("
                    "state_hash VARCHAR(64) PRIMARY KEY, "
                    "verifier VARCHAR(128) NOT NULL, "
                    "expires_at DOUBLE PRECISION NOT NULL)"
                )
            )
            session.commit()

    def issue(self, ttl: float = STATE_TTL_SECONDS) -> tuple[str, str]:
        state = secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair()
        expires_at = time.time() + ttl
        with self._session() as session:
            session.execute(text("DELETE FROM feishu_auth_state WHERE expires_at < :now"), {"now": time.time()})
            session.execute(
                text(
                    "INSERT INTO feishu_auth_state (state_hash, verifier, expires_at) "
                    "VALUES (:state_hash, :verifier, :expires_at)"
                ),
                {"state_hash": state_digest(state), "verifier": verifier, "expires_at": expires_at},
            )
            session.commit()
        return state, challenge

    def consume(self, state: str) -> str | None:
        if not isinstance(state, str) or not state:
            return None
        with self._session() as session:
            row = session.execute(
                text("DELETE FROM feishu_auth_state WHERE state_hash = :state_hash RETURNING verifier, expires_at"),
                {"state_hash": state_digest(state)},
            ).fetchone()
            session.commit()
        if row is None or float(row[1]) < time.time():
            return None
        return str(row[0])


class FeishuAuthManager(BaseAuthManager[FeishuUser]):
    """Feishu enterprise sign-in, mapped onto the single SolidWorks-to-URDF workflow."""

    def __init__(self) -> None:
        self.settings: FeishuSettings | None = None
        self.configuration_problem: str | None = None
        self._state: MetadataStateStore | None = None

    def init(self) -> None:
        """Prepare sign-in; a missing enterprise app never aborts the api-server."""
        super().init()
        try:
            settings = FeishuSettings.from_environment()
        except FeishuConfigError as error:
            self.settings = None
            self.configuration_problem = str(error)
            log.error("Feishu SSO is unavailable and stays fail-closed: %s", error)
            return
        self._state = MetadataStateStore()
        self.settings = settings
        log.info("Feishu SSO ready for approved tenants: %s", ", ".join(sorted(settings.tenant_keys)))

    # -- identity ---------------------------------------------------------

    def serialize_user(self, user: FeishuUser) -> dict[str, Any]:
        return {
            "sub": user.open_id,
            "app_id": user.app_id,
            "name": user.name,
            "avatar_url": user.avatar_url,
            "tenant_key": user.tenant_key,
        }

    def deserialize_user(self, token: dict[str, Any]) -> FeishuUser:
        settings = self.settings
        if settings is None:
            raise ValueError("Feishu SSO is not configured")
        identity = (token.get("sub"), token.get("app_id"), token.get("tenant_key"))
        if any(not isinstance(value, str) or not value.strip() for value in identity):
            raise ValueError("token carries no Feishu identity")
        open_id, app_id, tenant_key = (value.strip() for value in identity)
        if app_id != settings.app_id:
            raise ValueError("token was issued for a different Feishu enterprise app")
        if tenant_key not in settings.tenant_keys:
            raise ValueError("Feishu tenant is no longer approved")
        user = FeishuUser(
            app_id=app_id,
            open_id=open_id,
            # The original claim is validated as-is: a non-string claim is never cast into a
            # display name, and the stored value is the normalized recorded one.
            name=recorded_display_name(token.get("name")),
            avatar_url=str(token.get("avatar_url") or "").strip(),
            tenant_key=tenant_key,
        )
        # Fail closed here, not at trigger time, when the actor value cannot be recorded.
        user.get_name()
        return user

    def role_of(self, user: FeishuUser) -> str:
        """Administration is an explicit open_id allowlist; everyone else is an operator."""
        settings = self.settings
        if settings is not None and user.open_id in settings.admin_open_ids:
            return ROLE_ADMIN
        return ROLE_OPERATOR

    # -- routes -----------------------------------------------------------

    def get_url_login(self, **kwargs: Any) -> str:
        # One operator entry point: after sign-in the callback always lands on the portal page.
        return f"{AUTH_MANAGER_FASTAPI_APP_PREFIX}/feishu/login"

    def get_fastapi_app(self) -> FastAPI:
        app = FastAPI(title="Feishu SSO", docs_url=None, redoc_url=None, openapi_url=None)
        router = APIRouter()

        @router.get("/feishu/health")
        def health() -> JSONResponse:
            guarded = bound_request() is not None
            if self.settings is None:
                return JSONResponse(
                    {
                        "configured": False,
                        "reason": "feishu_sso_unconfigured",
                        "detail": self.configuration_problem,
                        "request_context": guarded,
                    },
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            return JSONResponse({"configured": True, "request_context": guarded})

        @router.get("/feishu/login")
        def login() -> RedirectResponse:
            if self.settings is None or self._state is None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="feishu_sso_unconfigured",
                )
            state, challenge = self._state.issue(STATE_TTL_SECONDS)
            response = RedirectResponse(
                build_authorize_url(self.settings, state, challenge),
                status_code=status.HTTP_302_FOUND,
            )
            response.headers["set-cookie"] = state_cookie_header(state, path=STATE_COOKIE_PATH)
            return response

        @router.get("/feishu/callback")
        def callback(
            request: Request,
            code: str | None = None,
            state: str | None = None,
            error: str | None = None,
        ) -> Response:
            if self.settings is None or self._state is None:
                return _login_page("本部署尚未配置飞书企业应用。", status.HTTP_503_SERVICE_UNAVAILABLE)
            if error:
                return _login_page("已取消飞书授权，请重新发起登录。", status.HTTP_400_BAD_REQUEST)
            if not code or not state or not state_cookie_matches(request.cookies.get(STATE_COOKIE), state):
                return _login_page("登录状态校验失败，请重新发起飞书登录。", status.HTTP_400_BAD_REQUEST)
            consumed = self._state.consume(state)
            if consumed is None:
                return _login_page("登录状态已过期或已使用，请重新登录。", status.HTTP_400_BAD_REQUEST)
            try:
                token = exchange_code(self.settings, code, consumed)
                identity = fetch_identity(self.settings, token)
                user = self._user(identity)
            except FeishuAuthError as failure:
                log.warning("Feishu sign-in refused: %s", failure)
                return _login_page("飞书登录未通过，请联系管理员核对企业应用配置。", status.HTTP_403_FORBIDDEN)
            response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
            response.set_cookie(
                COOKIE_NAME_JWT_TOKEN,
                self.generate_jwt(user),
                path=get_cookie_path(),
                # The Feishu callback URL is https-only by configuration, so the Airflow
                # session cookie is always a Secure cookie.
                secure=True,
                httponly=True,
                samesite="lax",
            )
            response.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH)
            return response

        @router.get("/feishu/profile")
        async def profile(request: Request) -> JSONResponse:
            token = request.cookies.get(COOKIE_NAME_JWT_TOKEN)
            if not token:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="not_signed_in")
            try:
                user = await self.get_user_from_token(token)
            except (InvalidTokenError, ValueError) as error:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_session") from error
            return JSONResponse(
                {
                    "open_id": user.open_id,
                    "app_id": user.app_id,
                    "name": user.name,
                    "avatar_url": user.avatar_url,
                    "tenant_key": user.tenant_key,
                    "principal": user.get_id(),
                    "role": self.role_of(user),
                }
            )

        app.include_router(router)
        return app

    def _user(self, identity: FeishuIdentity) -> FeishuUser:
        try:
            user = FeishuUser(
                app_id=self.settings.app_id if self.settings else "",
                open_id=identity.open_id,
                name=recorded_display_name(identity.name),
                avatar_url=identity.avatar_url,
                tenant_key=identity.tenant_key,
            )
            # Fail closed at sign-in when the actor value cannot be recorded for this principal.
            user.get_name()
        except ValueError as error:
            raise FeishuAuthError(str(error)) from error
        return user

    # -- authorization ----------------------------------------------------

    def _is_admin(self, user: FeishuUser) -> bool:
        return self.role_of(user) == ROLE_ADMIN

    def is_authorized_dag(
        self,
        *,
        method: ResourceMethod,
        user: FeishuUser,
        access_entity: DagAccessEntity | None = None,
        details: DagDetails | None = None,
    ) -> bool:
        if self._is_admin(user):
            return True
        if details is None or details.id != ALLOWED_DAG_ID:
            return False
        if method == "GET":
            return True
        if access_entity is not DagAccessEntity.RUN:
            return False
        if bound_request() is None:
            # The packaged request-context middleware is not active: every operator RUN grant
            # fails closed. Without routed context a POST cannot be distinguished from backfill
            # or bulk creation, so even manual creation is refused until the guard is installed.
            return False
        route = routed_route()
        if route is None or route.dag_id != ALLOWED_DAG_ID:
            return False
        if (
            method == "POST"
            and route.endpoint == MANUAL_CREATE_ENDPOINT
            and route.http_method == "POST"
            and route.dag_run_id is None
        ):
            # The one create path with canonically proven context: the manual trigger route.
            return True
        if (
            method == "PUT"
            and route.endpoint == SINGLE_RUN_CLEAR_ENDPOINT
            and route.http_method == "POST"
            and route.dag_run_id
            and route.clear_body_safe
        ):
            # The one owner mutation: retrying the caller's own run. Note/state patches,
            # partitions, bulk, wildcard, backfills and deletion stay administrator-only.
            return owner_retry_allowed(user, route.dag_id, route.dag_run_id)
        return False

    def is_authorized_configuration(
        self, *, method: ResourceMethod, user: FeishuUser, details: ConfigurationDetails | None = None
    ) -> bool:
        return self._is_admin(user)

    def is_authorized_connection(
        self, *, method: ResourceMethod, user: FeishuUser, details: ConnectionDetails | None = None
    ) -> bool:
        # The Windows endpoint token lives in the solidworks_windows Connection; operators never see it.
        return self._is_admin(user)

    def is_authorized_asset(
        self, *, method: ResourceMethod, user: FeishuUser, details: AssetDetails | None = None
    ) -> bool:
        return self._is_admin(user)

    def is_authorized_asset_alias(
        self, *, method: ResourceMethod, user: FeishuUser, details: AssetAliasDetails | None = None
    ) -> bool:
        return self._is_admin(user)

    def is_authorized_pool(
        self, *, method: ResourceMethod, user: FeishuUser, details: PoolDetails | None = None
    ) -> bool:
        return self._is_admin(user)

    def is_authorized_variable(
        self, *, method: ResourceMethod, user: FeishuUser, details: VariableDetails | None = None
    ) -> bool:
        return self._is_admin(user)

    def is_authorized_team(
        self, *, method: ResourceMethod, user: FeishuUser, details: TeamDetails | None = None
    ) -> bool:
        return self._is_admin(user)

    def is_authorized_view(self, *, access_view: AccessView, user: FeishuUser) -> bool:
        return self._is_admin(user)

    def is_authorized_custom_view(self, *, method: ResourceMethod, resource_name: str, user: FeishuUser) -> bool:
        return self._is_admin(user)

    def filter_authorized_menu_items(self, menu_items: list[MenuItem], *, user: FeishuUser) -> list[MenuItem]:
        if self._is_admin(user):
            return menu_items
        return [item for item in menu_items if item is MenuItem.DAGS]
