"""Feishu SSO as the auth manager of the pinned Airflow 3.3.2 environment.

This module is imported only inside the Linux Airflow runtime (``[core] auth_manager``); the
toolkit runtime never imports Airflow and keeps its OAuth/PKCE core in
``description_pipeline.orchestration.feishu_oauth``. One enterprise app signs operators in at
``/auth/feishu/login``; the callback mints the standard Airflow JWT cookie, so every Airflow
audit record already carries the operator's real Feishu name.

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
from urllib.parse import urlencode

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jwt import InvalidTokenError
from sqlalchemy import text

from airflow.api_fastapi.app import AUTH_MANAGER_FASTAPI_APP_PREFIX, get_cookie_path
from airflow.api_fastapi.auth.managers.base_auth_manager import COOKIE_NAME_JWT_TOKEN, BaseAuthManager
from airflow.api_fastapi.auth.managers.models.base_user import BaseUser
from airflow.api_fastapi.auth.managers.models.resource_details import DagAccessEntity, DagDetails, TeamDetails
from airflow.api_fastapi.common.types import MenuItem
from airflow.api_fastapi.core_api.security import is_safe_url
from airflow.configuration import conf

from description_pipeline.orchestration.feishu_oauth import (
    STATE_COOKIE,
    FeishuAuthError,
    FeishuConfigError,
    FeishuIdentity,
    FeishuSettings,
    build_authorize_url,
    exchange_code,
    fetch_identity,
    pkce_pair,
    state_cookie_header,
    state_cookie_matches,
    state_digest,
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
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>飞书登录</title></head><body>"
        "<main style=\"font-family:system-ui;max-width:32rem;margin:15vh auto;line-height:1.7\">"
        f"<h1 style=\"font-size:1.2rem\">飞书登录未完成</h1><p>{message}</p>"
        "<p><a href=\"/\">返回操作平台</a></p></main></body></html>"
    )
    return HTMLResponse(body, status_code=status_code)


@dataclass(frozen=True)
class FeishuUser(BaseUser):
    """One signed-in Feishu identity; the role is derived from configuration, never from the token."""

    open_id: str
    name: str
    avatar_url: str
    tenant_key: str

    def get_id(self) -> str:
        return self.open_id

    def get_name(self) -> str:
        return self.name


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
                    "next_url VARCHAR(2048), "
                    "expires_at DOUBLE PRECISION NOT NULL)"
                )
            )
            session.commit()

    def issue(self, ttl: float = STATE_TTL_SECONDS, next_url: str | None = None) -> tuple[str, str]:
        state = secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair()
        now = time.time()
        with self._session() as session:
            session.execute(text("DELETE FROM feishu_auth_state WHERE expires_at < :now"), {"now": now})
            session.execute(
                text(
                    "INSERT INTO feishu_auth_state (state_hash, verifier, next_url, expires_at) "
                    "VALUES (:state_hash, :verifier, :next_url, :expires_at)"
                ),
                {
                    "state_hash": state_digest(state),
                    "verifier": verifier,
                    "next_url": next_url,
                    "expires_at": now + ttl,
                },
            )
            session.commit()
        return state, challenge

    def consume(self, state: str) -> tuple[str, str | None] | None:
        if not isinstance(state, str) or not state:
            return None
        with self._session() as session:
            row = session.execute(
                text(
                    "DELETE FROM feishu_auth_state WHERE state_hash = :state_hash "
                    "RETURNING verifier, next_url, expires_at"
                ),
                {"state_hash": state_digest(state)},
            ).fetchone()
            session.commit()
        if row is None or float(row[2]) < time.time():
            return None
        return str(row[0]), (str(row[1]) if row[1] else None)


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
            "name": user.name,
            "avatar_url": user.avatar_url,
            "tenant_key": user.tenant_key,
        }

    def deserialize_user(self, token: dict[str, Any]) -> FeishuUser:
        settings = self.settings
        if settings is None:
            raise ValueError("Feishu SSO is not configured")
        open_id = str(token.get("sub") or "").strip()
        tenant_key = str(token.get("tenant_key") or "").strip()
        if not open_id or not tenant_key:
            raise ValueError("token carries no Feishu identity")
        if tenant_key not in settings.tenant_keys:
            raise ValueError("Feishu tenant is no longer approved")
        return FeishuUser(
            open_id=open_id,
            name=str(token.get("name") or open_id).strip(),
            avatar_url=str(token.get("avatar_url") or "").strip(),
            tenant_key=tenant_key,
        )

    def role_of(self, user: FeishuUser) -> str:
        """Administration is an explicit open_id allowlist; everyone else is an operator."""
        settings = self.settings
        if settings is not None and user.open_id in settings.admin_open_ids:
            return ROLE_ADMIN
        return ROLE_OPERATOR

    # -- routes -----------------------------------------------------------

    def get_url_login(self, **kwargs: Any) -> str:
        url = f"{AUTH_MANAGER_FASTAPI_APP_PREFIX}/feishu/login"
        if next_url := kwargs.get("next_url"):
            url += f"?{urlencode({'next': next_url})}"
        return url

    def get_fastapi_app(self) -> FastAPI:
        app = FastAPI(title="Feishu SSO", docs_url=None, redoc_url=None, openapi_url=None)
        router = APIRouter()

        @router.get("/feishu/health")
        def health() -> JSONResponse:
            if self.settings is None:
                return JSONResponse(
                    {
                        "configured": False,
                        "reason": "feishu_sso_unconfigured",
                        "detail": self.configuration_problem,
                    },
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            return JSONResponse({"configured": True})

        @router.get("/feishu/login")
        def login(request: Request, next: str | None = None) -> RedirectResponse:
            if self.settings is None or self._state is None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="feishu_sso_unconfigured",
                )
            target = next if next and is_safe_url(next, request=request) else None
            state, challenge = self._state.issue(STATE_TTL_SECONDS, target)
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
            verifier, next_url = consumed
            try:
                token = exchange_code(self.settings, code, verifier)
                identity = fetch_identity(self.settings, token)
            except FeishuAuthError as failure:
                log.warning("Feishu sign-in refused: %s", failure)
                return _login_page("飞书登录未通过，请联系管理员核对企业应用配置。", status.HTTP_403_FORBIDDEN)
            user = self._user(identity)
            target = (
                next_url
                if next_url and is_safe_url(next_url, request=request)
                else conf.get("api", "base_url", fallback="/")
            )
            response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
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
            except InvalidTokenError as error:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_session") from error
            return JSONResponse(
                {
                    "open_id": user.open_id,
                    "name": user.name,
                    "avatar_url": user.avatar_url,
                    "tenant_key": user.tenant_key,
                    "role": self.role_of(user),
                }
            )

        app.include_router(router)
        return app

    def _user(self, identity: FeishuIdentity) -> FeishuUser:
        return FeishuUser(
            open_id=identity.open_id,
            name=identity.name,
            avatar_url=identity.avatar_url,
            tenant_key=identity.tenant_key,
        )

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
        # Triggering the one workflow creates its DagRun; all other writes stay admin-only.
        return method == "POST" and access_entity in (None, DagAccessEntity.RUN)

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
