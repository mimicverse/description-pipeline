"""Onshape 引用解析：URL 或显式 ID 均可，输出统一的 ``DocumentRef``。"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_STACK = "https://cad.onshape.com"

URL_PATTERN = re.compile(
    r"^https?://(?P<host>[^/]+)/documents/(?P<document>[^/?#]+)"
    r"(?:/(?P<wvm>[wv])/(?P<wvmid>[^/?#]+))?"
    r"(?:/e/(?P<element>[^/?#]+))?/?$"
)


class ReferenceError(ValueError):
    """引用无法解析或信息不完整。"""


@dataclass(frozen=True)
class DocumentRef:
    """一次导出所需的定位信息。``workspace_id`` 与 ``version_id`` 只能有一个。"""

    document_id: str
    element_id: str
    workspace_id: str | None = None
    version_id: str | None = None
    stack: str = DEFAULT_STACK

    @property
    def wvm(self) -> str:
        return "v" if self.version_id else "w"

    @property
    def wvmid(self) -> str:
        value = self.version_id or self.workspace_id
        if not value:
            raise ReferenceError("需要 workspace_id 或 version_id")
        return value

    def url(self) -> str:
        return f"{self.stack}/documents/{self.document_id}/{self.wvm}/{self.wvmid}/e/{self.element_id}"

    def as_dict(self) -> dict:
        return {
            "stack": self.stack,
            "document_id": self.document_id,
            "workspace_id": self.workspace_id,
            "version_id": self.version_id,
            "element_id": self.element_id,
            "url": self.url(),
        }


def parse_reference(
    url: str | None = None,
    *,
    document_id: str | None = None,
    element_id: str | None = None,
    workspace_id: str | None = None,
    version_id: str | None = None,
    stack: str = DEFAULT_STACK,
) -> DocumentRef:
    """接受完整 URL 或显式 ID；两者都给出时以显式 ID 为准（便于覆盖版本）。"""

    from_url: dict[str, str | None] = {}
    if url:
        from_url = _parse_url(url)

    document = document_id or from_url.get("document_id")
    element = element_id or from_url.get("element_id")
    workspace = workspace_id or from_url.get("workspace_id")
    version = version_id or from_url.get("version_id")
    resolved_stack = from_url.get("stack") or stack

    if not document:
        raise ReferenceError("缺少 document_id：给 --url 或 --document-id")
    if not element:
        raise ReferenceError("缺少 element_id：URL 需带 /e/<elementId>，或显式给 --element-id")
    if workspace and version:
        raise ReferenceError("workspace_id 与 version_id 不能同时给出")
    if not workspace and not version:
        raise ReferenceError("缺少工作区/版本：URL 需带 /w/<id> 或 /v/<id>")

    return DocumentRef(
        document_id=document,
        element_id=element,
        workspace_id=workspace,
        version_id=version,
        stack=resolved_stack.rstrip("/"),
    )


def _parse_url(url: str) -> dict[str, str | None]:
    match = URL_PATTERN.match(url.strip())
    if not match:
        raise ReferenceError("无法解析 Onshape URL；期望 https://<host>/documents/<did>/w|v/<id>/e/<eid>")
    parts = match.groupdict()
    return {
        "stack": f"https://{parts['host']}",
        "document_id": parts["document"],
        "workspace_id": parts["wvmid"] if parts["wvm"] == "w" else None,
        "version_id": parts["wvmid"] if parts["wvm"] == "v" else None,
        "element_id": parts["element"],
    }
