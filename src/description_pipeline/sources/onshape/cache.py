"""响应缓存：``json/`` 存 JSON、``bytes/`` 存二进制，并把每项绑定到请求身份。

缓存文件名（``assembly_<element>`` 之类）本身**不含**文档/修订/配置，所以：

* 新写入的缓存会在 ``json/_requests.json`` 里记录 ``(path, query, sha256)``，读取时逐项核对——
  同一文件名对应不同请求会被拒绝，而不是把别的字节重新贴标签；
* 旧 ``tools/onshape_export`` 的缓存没有索引，仍可离线重放，但每一项都会被标成
  ``unbound``（来源层据此降低证据等级并写明局限）。

读写都是显式路径操作，不改任何第三方模块状态。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .errors import CACHE_IDENTITY_MISMATCH, CACHE_MISS, OnshapeSourceError

INDEX_NAME = "_requests"


def content_digest(value, *, raw: bool) -> str:
    """JSON 用规范化内容摘要（与文件排版无关），二进制用字节摘要。"""

    if raw:
        return hashlib.sha256(value).hexdigest()
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def immutable_path(path: str) -> bool:
    """``/v/<id>`` 或 ``/m/<microversion>`` 是不可变修订；``/w/<id>`` 是可变工作区头。"""

    parts = [chunk for chunk in path.split("/") if chunk]
    return "m" in parts or "v" in parts


class ResponseCache:
    def __init__(self, root: Path, *, read_only: bool = False) -> None:
        self.root = Path(root)
        self.read_only = read_only
        self.json_dir = self.root / "json"
        self.bytes_dir = self.root / "bytes"
        if not read_only:
            self.json_dir.mkdir(parents=True, exist_ok=True)
            self.bytes_dir.mkdir(parents=True, exist_ok=True)
        self._index = self.load_json(INDEX_NAME) or {}

    def json_path(self, name: str) -> Path:
        return self.json_dir / f"{name}.json"

    def bytes_path(self, name: str) -> Path:
        return self.bytes_dir / name

    def load_json(self, name: str):
        path = self.json_path(name)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def save_json(self, name: str, value) -> Path:
        path = self.json_path(name)
        if not self.read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Cached responses are replayed into frozen snapshots, so their bytes must not depend on
            # the platform that captured them; pin LF even though today's payload has no newlines.
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8", newline="\n")
        return path

    def load_bytes(self, name: str) -> bytes | None:
        path = self.bytes_path(name)
        return path.read_bytes() if path.is_file() else None

    def save_bytes(self, name: str, value: bytes) -> Path:
        path = self.bytes_path(name)
        if not self.read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        return path

    def require_json(self, name: str):
        value = self.load_json(name)
        if value is None:
            raise OnshapeSourceError(CACHE_MISS, f"缓存缺少 {name}.json", {"cache": str(self.root), "name": name})
        return value

    # --- 请求身份索引 -------------------------------------------------

    def request_entry(self, name: str) -> dict | None:
        entry = (self._index or {}).get(name)
        return dict(entry) if isinstance(entry, dict) else None

    def record_request(self, name: str, *, path: str, query: dict | None, digest: str, raw: bool) -> dict:
        entry = {
            "path": path,
            "query": {str(key): str(value) for key, value in (query or {}).items()},
            "sha256": digest,
            "kind": "bytes" if raw else "json",
        }
        self._index[name] = entry
        if not self.read_only:
            self.save_json(INDEX_NAME, self._index)
        return entry

    @property
    def index(self) -> dict:
        return dict(self._index or {})


class CachedFetcher:
    """JSON/二进制读取器：优先缓存；缺失时用 client 拉取并（可选）回写缓存。

    ``origin`` 记录本次采集的证据来源：全部命中缓存是 ``cache_replay``，任何一次
    真实请求都会把整次采集标成 ``live_api``（不混称）。
    ``unbound`` 列出没有请求身份记录的缓存项；``bindings`` 记录每项请求身份与摘要。
    """

    def __init__(self, client=None, cache: ResponseCache | None = None) -> None:
        self.client = client
        self.cache = cache
        self.used_network = False
        self.unbound: set[str] = set()
        self.bindings: dict[str, dict] = {}

    @property
    def origin(self) -> str:
        return "live_api" if self.used_network else "cache_replay"

    @property
    def network_names(self) -> list[str]:
        """本次真的从 API 取到的项。"""

        return sorted(name for name, entry in self.bindings.items() if entry.get("source") == "network")

    @property
    def cache_names(self) -> list[str]:
        """本次从缓存读到的项（可能混合：一部分网络、一部分历史缓存）。"""

        return sorted(name for name, entry in self.bindings.items() if entry.get("source") == "cache")

    @property
    def immutable_requests(self) -> bool:
        """本次取数的每一项是否都用了不可变修订标识。"""

        return bool(self.bindings) and all(
            entry.get("immutable") is True and entry.get("verified") is True for entry in self.bindings.values()
        )

    def _verify_entry(self, name: str, path: str, query: dict | None, value, *, raw: bool) -> dict:
        entry = self.cache.request_entry(name) if self.cache else None
        digest = content_digest(value, raw=raw)
        if entry is None:
            self.unbound.add(name)
            return {
                "path": path,
                "sha256": digest,
                "immutable": immutable_path(path),
                "verified": False,
                "source": "cache",
            }
        expected_query = {str(key): str(item) for key, item in (query or {}).items()}
        if entry.get("path") != path or dict(entry.get("query") or {}) != expected_query:
            raise OnshapeSourceError(
                CACHE_IDENTITY_MISMATCH,
                "缓存项与请求身份不符（同一文件名被用于不同 path/query）",
                {
                    "name": name,
                    "request": {"path": path, "query": expected_query},
                    "index": {"path": entry.get("path"), "query": entry.get("query")},
                },
            )
        if entry.get("sha256") != digest:
            raise OnshapeSourceError(
                CACHE_IDENTITY_MISMATCH,
                "缓存项内容与索引摘要不符",
                {"name": name, "index_sha256": entry.get("sha256"), "actual_sha256": digest},
            )
        return {
            "path": path,
            "query": expected_query,
            "sha256": digest,
            "immutable": immutable_path(path),
            "verified": True,
            "source": "cache",
        }

    def _fetch(self, name: str, path: str, query: dict | None, *, raw: bool):
        cached = (
            self.cache.load_bytes(name)
            if raw and self.cache is not None
            else (self.cache.load_json(name) if self.cache else None)
        )
        # A workspace URL denotes its current head. Only an explicit offline
        # replay may reuse that probe; otherwise a later freeze could silently
        # capture yesterday's revision without contacting CAD at all.
        refresh_head = self.client is not None and "/w/" in path
        if cached is not None and not refresh_head:
            binding = self._verify_entry(name, path, query, cached, raw=raw)
            self.bindings[name] = binding
            return cached
        if self.client is None:
            raise OnshapeSourceError(
                CACHE_MISS,
                f"缓存缺少 {name}，且未提供 API client",
                {"cache": str(self.cache.root) if self.cache else None, "name": name},
            )
        value = self.client.request("GET", path, query=query, raw=raw)
        self.used_network = True
        digest = content_digest(value, raw=raw)
        if self.cache is not None and not self.cache.read_only:
            if raw:
                self.cache.save_bytes(name, value)
            else:
                self.cache.save_json(name, value)
        if self.cache is not None:
            self.cache.record_request(name, path=path, query=query, digest=digest, raw=raw)
        self.bindings[name] = {
            "path": path,
            "query": {str(key): str(item) for key, item in (query or {}).items()},
            "sha256": digest,
            "immutable": immutable_path(path),
            "verified": True,
            "source": "network",
        }
        return value

    def json(self, name: str, path: str, query: dict | None = None) -> dict:
        return self._fetch(name, path, query, raw=False)

    def bytes(self, name: str, path: str, query: dict | None = None) -> bytes:
        return self._fetch(name, path, query, raw=True)
