"""Onshape REST 客户端：HMAC-SHA256 签名，只用标准库；支持本地缓存与离线模式。"""

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
from datetime import datetime, UTC
from pathlib import Path

from .cache import ResponseCache
from .url import DEFAULT_STACK, DocumentRef

KEY_PATHS = (
    Path.home() / ".onshape_api_keys.json",
    Path("onshape_api_keys.json"),
)


class OnshapeError(RuntimeError):
    def __init__(self, status: int, body: str, *, hint: str = ""):
        detail = body[:400].replace("\n", " ")
        message = f"HTTP {status}: {detail}"
        if hint:
            message += f"（{hint}）"
        super().__init__(message)
        self.status = status
        self.body = body
        self.hint = hint


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """导出端点会 307 到签名 URL；跟随重定向会丢签名，因此手动处理。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def load_credentials(stack: str = DEFAULT_STACK) -> tuple[str, str, str, str]:
    """按 env → 用户配置文件的顺序读取密钥；返回 (stack, access, secret, bearer)。"""

    base = (os.getenv("ONSHAPE_API") or stack).rstrip("/")
    bearer = os.getenv("ONSHAPE_SECRET_BEARER", "")
    access = os.getenv("ONSHAPE_ACCESS_KEY", "")
    secret = os.getenv("ONSHAPE_SECRET_KEY", "")
    if bearer or (access and secret):
        return base, access, secret, bearer
    for path in KEY_PATHS:
        if path.is_file():
            entry = json.loads(path.read_text(encoding="utf-8")).get(base, {})
            return (
                base,
                entry.get("access_key", ""),
                entry.get("secret_key", ""),
                entry.get("secret_bearer", ""),
            )
    return base, "", "", ""


class OnshapeClient:
    """只覆盖本工具需要的端点；缓存键由调用方给定，便于审计。"""

    def __init__(
        self,
        stack: str = DEFAULT_STACK,
        access_key: str = "",
        secret_key: str = "",
        bearer: str = "",
        cache: ResponseCache | None = None,
        offline: bool = False,
        timeout: float = 60.0,
    ):
        self.stack = stack.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.bearer = bearer
        self.cache = cache
        self.offline = offline
        self.timeout = timeout

    @classmethod
    def from_env(
        cls, stack: str = DEFAULT_STACK, *, cache_dir: Path | None = None, offline: bool = False
    ) -> OnshapeClient:
        base, access, secret, bearer = load_credentials(stack)
        cache = ResponseCache(cache_dir, read_only=offline) if cache_dir else None
        return cls(base, access, secret, bearer, cache=cache, offline=offline)

    @property
    def has_credentials(self) -> bool:
        return bool(self.bearer or (self.access_key and self.secret_key))

    # --- 传输层 ---------------------------------------------------------

    def _headers(self, method: str, path: str, query: dict, ctype: str) -> dict:
        date = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
        headers = {"Content-Type": ctype, "Date": date, "User-Agent": "mimicverse-onshape-export"}
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
        raw: bool = False,
        retries: int = 3,
        timeout: float | None = None,
    ):
        if self.offline:
            raise OnshapeError(0, f"离线模式请求 {path}", hint="缓存未命中，先运行 fetch")
        if not self.has_credentials:
            raise OnshapeError(
                401,
                "没有可用的 Onshape 凭据",
                hint="设置 ONSHAPE_ACCESS_KEY/ONSHAPE_SECRET_KEY 或写 ~/.onshape_api_keys.json",
            )
        query = dict(query or {})
        path_only = path.split("?")[0]
        if "?" in path:
            query = {**dict(urllib.parse.parse_qsl(path.split("?", 1)[1])), **query}
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        url = self.stack + path_only + (f"?{urllib.parse.urlencode(query)}" if query else "")
        request = urllib.request.Request(
            url, data=data, method=method.upper(), headers=self._headers(method.upper(), path_only, query, ctype)
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
                    hint = "年度 API 配额耗尽，见 docs/onshape_export.md 的离线缓存流程"
                raise OnshapeError(error.code, detail, hint=hint) from None
            except Exception as error:  # noqa: BLE001 - 网络瞬断重试
                last_error = error
                time.sleep(1.5 * (attempt + 1))
        raise OnshapeError(0, f"网络错误: {last_error}")

    # --- 缓存包装 -------------------------------------------------------

    def cached_json(self, name: str, method: str, path: str, query: dict | None = None):
        if self.cache is not None:
            cached = self.cache.load_json(name)
            if cached is not None:
                return cached
        if self.offline:
            raise OnshapeError(0, f"离线缓存缺少 {name}", hint=f"先运行 fetch 生成 {name}.json")
        data = self.request(method, path, query=query)
        if self.cache is not None:
            self.cache.save_json(name, data)
        return data

    def cached_bytes(self, name: str, path: str, query: dict | None = None) -> bytes:
        if self.cache is not None:
            cached = self.cache.load_bytes(name)
            if cached is not None:
                return cached
        if self.offline:
            raise OnshapeError(0, f"离线缓存缺少 {name}", hint=f"先运行 fetch 生成 bytes/{name}")
        data = self.request("get", path, query=query, raw=True)
        if self.cache is not None:
            self.cache.save_bytes(name, data)
        return data

    # --- 端点 -----------------------------------------------------------

    def whoami(self) -> dict:
        return self.cached_json("whoami", "get", "/api/users/sessioninfo")

    def list_elements(self, ref: DocumentRef) -> list[dict]:
        return self.cached_json(
            f"elements_{ref.document_id}_{ref.wvm}_{ref.wvmid}",
            "get",
            f"/api/documents/d/{ref.document_id}/{ref.wvm}/{ref.wvmid}/elements",
        )

    def get_assembly(self, ref: DocumentRef, configuration: str = "default") -> dict:
        return self.cached_json(
            f"assembly_{ref.element_id}",
            "get",
            f"/api/assemblies/d/{ref.document_id}/{ref.wvm}/{ref.wvmid}/e/{ref.element_id}",
            query={
                "includeMateFeatures": "true",
                "includeMateConnectors": "true",
                "includeNonSolids": "true",
                "configuration": configuration,
            },
        )

    def get_assembly_features(self, ref: DocumentRef, configuration: str = "default") -> dict:
        return self.cached_json(
            f"assembly_features_{ref.element_id}",
            "get",
            f"/api/assemblies/d/{ref.document_id}/{ref.wvm}/{ref.wvmid}/e/{ref.element_id}/features",
            query={"configuration": configuration},
        )

    def get_mate_values(self, ref: DocumentRef) -> dict:
        return self.cached_json(
            f"mate_values_{ref.element_id}",
            "get",
            f"/api/assemblies/d/{ref.document_id}/{ref.wvm}/{ref.wvmid}/e/{ref.element_id}/matevalues",
        )

    def get_studio_mass_properties(self, ref: DocumentRef, element_id: str) -> dict:
        return self.cached_json(
            f"mass_properties_{element_id}",
            "get",
            f"/api/partstudios/d/{ref.document_id}/{ref.wvm}/{ref.wvmid}/e/{element_id}/massproperties",
            query={"massAsGroup": "false"},
        )

    def get_part_mass_properties(self, ref: DocumentRef, element_id: str, part_id: str) -> dict:
        return self.cached_json(
            f"mass_properties_{element_id}_{_safe(part_id)}",
            "get",
            f"/api/parts/d/{ref.document_id}/{ref.wvm}/{ref.wvmid}/e/{element_id}/partid/{part_id}/massproperties",
            query={"useMassPropertyOverrides": "true"},
        )

    def get_part_studio_gltf(self, ref: DocumentRef, element_id: str) -> dict:
        """零件工作室 GLTF：网格在零件坐标系，且是同源 JSON，适合作为几何来源。"""

        return self.cached_json(
            f"gltf_{element_id}",
            "get",
            f"/api/partstudios/d/{ref.document_id}/{ref.wvm}/{ref.wvmid}/e/{element_id}/gltf",
        )

    def get_part_stl(self, ref: DocumentRef, element_id: str, part_id: str) -> bytes:
        return self.cached_bytes(
            f"stl_{_safe(part_id)}.stl",
            f"/api/parts/d/{ref.document_id}/{ref.wvm}/{ref.wvmid}/e/{element_id}/partid/{part_id}/stl",
            query={"mode": "binary", "units": "meter"},
        )


def _safe(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value)
