"""Onshape 文档引用与来源身份：URL/显式 ID → 稳定身份 + 精确修订。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .errors import REFERENCE_INVALID, OnshapeSourceError

DEFAULT_STACK = "https://cad.onshape.com"
_URL_ELEMENT = re.compile(r"/e/(?P<element>[^/?#]+)")
_URL_WORKSPACE = re.compile(r"/w/(?P<workspace>[^/?#]+)")
_URL_VERSION = re.compile(r"/v/(?P<version>[^/?#]+)")


@dataclass(frozen=True)
class DocumentRef:
    """一次采集的精确来源身份：站点 + 文档 + (工作区|版本) + 元素。"""

    stack: str
    document_id: str
    element_id: str
    workspace_id: str | None = None
    version_id: str | None = None

    def __post_init__(self) -> None:
        if bool(self.workspace_id) == bool(self.version_id):
            raise OnshapeSourceError(
                REFERENCE_INVALID,
                "必须且只能给出 workspace_id 或 version_id 之一",
                {"workspace_id": self.workspace_id, "version_id": self.version_id},
            )

    @property
    def wvm(self) -> str:
        return "v" if self.version_id else "w"

    @property
    def wvmid(self) -> str:
        return str(self.version_id or self.workspace_id)

    @property
    def url(self) -> str:
        base = f"{self.stack.rstrip('/')}/documents/{self.document_id}/{self.wvm}/{self.wvmid}"
        return f"{base}/e/{self.element_id}"

    def identity(self) -> dict:
        """写入快照的来源身份（不含访问凭据）。"""

        return {
            "stack": self.stack,
            "document_id": self.document_id,
            "element_id": self.element_id,
            "workspace_id": self.workspace_id,
            "version_id": self.version_id,
            "url": self.url,
        }

    def lock(self, *, microversion: str | None, configuration: str) -> dict:
        """来源锁：精确修订（microversion/version）+ 配置。"""

        payload = self.identity()
        payload["configuration"] = configuration
        payload["microversion"] = microversion
        payload["revision_kind"] = "version" if self.version_id else "workspace"
        payload["revision_locked"] = bool(self.version_id or microversion)
        return payload


def parse_reference(
    url: str | None = None,
    *,
    document_id: str | None = None,
    element_id: str | None = None,
    workspace_id: str | None = None,
    version_id: str | None = None,
    stack: str = DEFAULT_STACK,
) -> DocumentRef:
    """显式 ID 优先；只给 URL 时从路径解析（版本 URL 用 ``/v/<id>``）。"""

    parsed_stack = stack
    if url:
        parts = urlparse(url)
        if not parts.scheme or not parts.netloc:
            raise OnshapeSourceError(REFERENCE_INVALID, "URL 缺少站点", {"url": url})
        parsed_stack = f"{parts.scheme}://{parts.netloc}"
        path_parts = [chunk for chunk in parts.path.split("/") if chunk]
        if len(path_parts) >= 2 and path_parts[0] == "documents":
            document_id = document_id or path_parts[1]
        workspace_match, version_match = _URL_WORKSPACE.search(parts.path), _URL_VERSION.search(parts.path)
        if workspace_match and not workspace_id:
            workspace_id = workspace_match.group("workspace")
        if version_match and not version_id:
            version_id = version_match.group("version")
        element_match = _URL_ELEMENT.search(parts.path)
        if element_match and not element_id:
            element_id = element_match.group("element")
    missing = [name for name, value in (("document_id", document_id), ("element_id", element_id)) if not value]
    if missing:
        raise OnshapeSourceError(REFERENCE_INVALID, "缺少文档/元素 ID", {"missing": missing})
    if not workspace_id and not version_id:
        raise OnshapeSourceError(REFERENCE_INVALID, "必须给出工作区或版本（URL 需含 /w/ 或 /v/）")
    return DocumentRef(
        stack=parsed_stack,
        document_id=str(document_id),
        element_id=str(element_id),
        workspace_id=str(workspace_id) if workspace_id else None,
        version_id=str(version_id) if version_id else None,
    )
