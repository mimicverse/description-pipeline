"""Onshape REST 传输层：HMAC/Bearer 签名、重试与稳定错误码。

只负责一次请求的语义；响应缓存由 :class:`~.cache.CachedFetcher` 负责，
因此本模块不读写缓存，也不修改任何第三方模块状态。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from .errors import API_ERROR, API_UNAVAILABLE, CREDENTIALS_MISSING, OnshapeSourceError
from .reference import DEFAULT_STACK

KEY_PATHS = (Path.home() / ".onshape_api_keys.json", Path("onshape_api_keys.json"))
RETRY_BACKOFF_SECONDS = 1.5


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """导出端点会 307 到签名 URL；跟随重定向会丢签名，因此手动处理。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def load_credentials(stack: str = DEFAULT_STACK) -> tuple[str, str, str, str]:
    """返回 ``(stack, access_key, secret_key, bearer)``；只从环境或密钥文件读取。"""

    access = os.environ.get("ONSHAPE_ACCESS_KEY", "")
    secret = os.environ.get("ONSHAPE_SECRET_KEY", "")
    bearer = os.environ.get("ONSHAPE_SECRET_BEARER", "")
    env_stack = os.environ.get("ONSHAPE_API", stack) or stack
    if access and secret:
        return env_stack, access, secret, bearer
    for path in KEY_PATHS:
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        entry = payload.get(stack) or payload.get(env_stack) or {}
        access = access or str(entry.get("access_key", ""))
        secret = secret or str(entry.get("secret_key", ""))
        bearer = bearer or str(entry.get("bearer", ""))
    return env_stack, access, secret, bearer


class OnshapeClient:
    def __init__(
        self,
        stack: str = DEFAULT_STACK,
        *,
        access_key: str = "",
        secret_key: str = "",
        bearer: str = "",
        timeout: float = 60.0,
        offline: bool = False,
    ) -> None:
        self.stack = stack.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.bearer = bearer
        self.timeout = timeout
        self.offline = offline

    @classmethod
    def from_env(cls, stack: str = DEFAULT_STACK, *, timeout: float = 60.0, offline: bool = False) -> OnshapeClient:
        resolved_stack, access, secret, bearer = load_credentials(stack)
        return cls(
            resolved_stack,
            access_key=access,
            secret_key=secret,
            bearer=bearer,
            timeout=timeout,
            offline=offline,
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.bearer or (self.access_key and self.secret_key))

    def headers(self, method: str, path: str, query: dict, ctype: str) -> dict[str, str]:
        """签名头（与 Onshape 文档一致的 HMAC 配方；Bearer 时跳过）。"""

        date = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
        headers = {
            "Content-Type": ctype,
            "Date": date,
            "User-Agent": "mimicverse-description-pipeline-onshape",
        }
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"
            return headers
        nonce = "".join(random.choice(string.digits + string.ascii_letters) for _ in range(25))
        payload = (
            method
            + "\n"
            + nonce
            + "\n"
            + date
            + "\n"
            + ctype
            + "\n"
            + path
            + "\n"
            + urllib.parse.urlencode(query)
            + "\n"
        ).lower()
        digest = hmac.new(self.secret_key.encode(), payload.encode(), hashlib.sha256).digest()
        headers["On-Nonce"] = nonce
        headers["Authorization"] = f"On {self.access_key}:HmacSHA256:{base64.b64encode(digest).decode()}"
        return headers

    def request(
        self,
        method: str,
        path: str,
        query: dict | None = None,
        body=None,
        ctype: str = "application/json",
        *,
        raw: bool = False,
        retries: int = 3,
        timeout: float | None = None,
    ):
        if self.offline:
            raise OnshapeSourceError(API_UNAVAILABLE, f"离线模式请求 {path}", {"path": path, "hint": "先冻结快照"})
        if not self.has_credentials:
            raise OnshapeSourceError(
                CREDENTIALS_MISSING,
                "没有可用的 Onshape 凭据",
                {"hint": "设置 ONSHAPE_ACCESS_KEY/ONSHAPE_SECRET_KEY 或写 ~/.onshape_api_keys.json"},
            )
        query = dict(query or {})
        path_only = path.split("?")[0]
        if "?" in path:
            query = {**dict(urllib.parse.parse_qsl(path.split("?", 1)[1])), **query}
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        url = self.stack + path_only + (f"?{urllib.parse.urlencode(query)}" if query else "")
        request = urllib.request.Request(
            url,
            data=data,
            method=method.upper(),
            headers=self.headers(method.upper(), path_only, query, ctype),
        )
        opener = urllib.request.build_opener(_NoRedirect())
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with opener.open(request, timeout=timeout or self.timeout) as response:
                    payload = response.read()
                    return payload if raw else json.loads(payload or b"null")
            except urllib.error.HTTPError as error:
                detail = error.read().decode(errors="replace")
                hint = ""
                if error.code == 402:
                    hint = "年度 API 配额耗尽；改用已有缓存冻结（cache=…）"
                raise OnshapeSourceError(
                    API_ERROR,
                    f"HTTP {error.code}: {detail[:400]}",
                    {"status": error.code, "path": path_only, "hint": hint},
                ) from None
            except Exception as error:  # noqa: BLE001 - 网络瞬断重试
                last_error = error
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        raise OnshapeSourceError(API_UNAVAILABLE, f"网络错误: {last_error}", {"path": path_only})

    # --- 只读端点（采集用到的全部）----------------------------------------

    def get_assembly(self, ref, configuration: str = "default") -> dict:
        return self.request(
            "GET",
            f"/api/assemblies/d/{ref.document_id}/{ref.wvm}/{ref.wvmid}/e/{ref.element_id}",
            query={"configuration": configuration, "includeMateFeatures": "true"},
        )

    def get_assembly_features(self, ref, configuration: str = "default") -> dict:
        return self.request(
            "GET",
            f"/api/assemblies/d/{ref.document_id}/{ref.wvm}/{ref.wvmid}/e/{ref.element_id}/features",
            query={"configuration": configuration},
        )

    def get_mate_values(self, ref, configuration: str = "default") -> dict:
        return self.request(
            "GET",
            f"/api/assemblies/d/{ref.document_id}/{ref.wvm}/{ref.wvmid}/e/{ref.element_id}/matevalues",
            query={"configuration": configuration},
        )

    def get_studio_mass_properties(self, ref, element_id: str) -> dict:
        return self.request(
            "GET",
            f"/api/partstudios/d/{ref.document_id}/{ref.wvm}/{ref.wvmid}/e/{element_id}/massproperties",
        )

    def get_part_studio_gltf(self, ref, element_id: str) -> dict:
        return self.request(
            "GET",
            f"/api/partstudios/d/{ref.document_id}/{ref.wvm}/{ref.wvmid}/e/{element_id}/gltf",
        )

    def get_part_stl(self, ref, element_id: str, part_id: str) -> bytes:
        return self.request(
            "GET",
            f"/api/parts/d/{ref.document_id}/{ref.wvm}/{ref.wvmid}/e/{element_id}/partid/{part_id}/stl",
            raw=True,
        )

    def get_microversion(self, ref) -> str | None:
        """工作区采集时取当前 microversion，作为"精确修订"写入来源锁。"""

        if ref.version_id:
            return None
        payload = self.request("GET", f"/api/documents/d/{ref.document_id}/w/{ref.workspace_id}")
        microversion = payload.get("microversion") or payload.get("currentMicroversion")
        return str(microversion) if microversion else None
