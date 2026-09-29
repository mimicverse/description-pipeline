"""本地响应缓存：让检查/导出可复现，也让配额耗尽时仍能工作。"""

from __future__ import annotations

import json
from pathlib import Path


class ResponseCache:
    """``<root>/json/<name>.json`` 存 JSON，``<root>/bytes/<name>`` 存二进制。"""

    def __init__(self, root: Path, read_only: bool = False):
        self.root = Path(root)
        self.read_only = read_only
        self.json_dir = self.root / "json"
        self.bytes_dir = self.root / "bytes"
        if not read_only:
            self.json_dir.mkdir(parents=True, exist_ok=True)
            self.bytes_dir.mkdir(parents=True, exist_ok=True)

    def json_path(self, name: str) -> Path:
        return self.json_dir / f"{name}.json"

    def bytes_path(self, name: str) -> Path:
        return self.bytes_dir / name

    def load_json(self, name: str):
        path = self.json_path(name)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save_json(self, name: str, value) -> Path:
        path = self.json_path(name)
        if not self.read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
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
            raise FileNotFoundError(f"缓存缺少 {name}.json（{self.json_path(name)}）")
        return value
