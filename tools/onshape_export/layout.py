"""把引擎输出归一化为本仓库固定入口：urdf/robot.urdf、meshes/、mjcf/。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from collections.abc import Sequence

from . import contacts, stl

# 引擎写的是 package://assets/<file>；这里同时接受带/不带包前缀的两种历史写法。
URDF_MESH = re.compile(r'filename="(?:package://)?(?:assets/)?(?P<name>[^"/]+\.stl)"')
URDF_NAME = re.compile(r'<robot\s+name="[^"]*"')
MJCF_MESH_DIR = re.compile(r'meshdir="assets"')
MJCF_NAME = re.compile(r'<mujoco\s+model="[^"]*"')
MJCF_TEMP_CLASS = re.compile(r"onshape-export-[A-Za-z0-9_]+")
MJCF_ASSET_BLOCK = re.compile(r"<asset>.*?</asset>", re.DOTALL)
MJCF_ASSET_MESH = re.compile(r"<mesh\b[^>]*/>")
MESH_NAME = re.compile(r'<mesh[^>]*\bname="(?P<name>[^"]+)"')


class LayoutError(RuntimeError):
    pass


def normalize(
    engine_dir: Path,
    root: Path,
    *,
    contact_excludes: Sequence[tuple[str, str]] | None = None,
) -> dict:
    """搬运并改写引用；同名不同内容时报错，避免静默覆盖既有网格。"""

    engine_dir, root = Path(engine_dir), Path(root)
    manifest: dict = {"meshes": {}, "urdf": None, "mjcf": None, "parts": 0}
    assets = engine_dir / "assets"
    meshes_dir = root / "meshes"
    meshes_dir.mkdir(parents=True, exist_ok=True)

    if assets.is_dir():
        for path in sorted(assets.glob("*.stl")):
            target = meshes_dir / path.name
            digest = stl.sha256(path)
            if target.exists() and stl.sha256(target) != digest:
                raise LayoutError(f"meshes/{path.name} 已存在且内容不同，拒绝覆盖")
            target.write_bytes(path.read_bytes())
            manifest["meshes"][path.name] = digest
        parts_dir = root / "onshape" / "parts"
        part_files = sorted(assets.glob("*.part"))
        if part_files:
            parts_dir.mkdir(parents=True, exist_ok=True)
            for path in part_files:
                (parts_dir / f"{path.name}.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            manifest["parts"] = len(part_files)

    urdf_source = engine_dir / "robot.urdf"
    if urdf_source.is_file():
        text = URDF_MESH.sub(
            lambda match: f'filename="../meshes/{match.group("name")}"', urdf_source.read_text(encoding="utf-8")
        )
        text = URDF_NAME.sub('<robot name="robot"', text, count=1)
        target = root / "urdf" / "robot.urdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        manifest["urdf"] = "urdf/robot.urdf"

    for name in ("robot.xml", "scene.xml"):
        source = engine_dir / name
        if not source.is_file():
            continue
        text = MJCF_MESH_DIR.sub('meshdir="../meshes"', source.read_text(encoding="utf-8"))
        # 引擎按 set 遍历网格，顺序随 PYTHONHASHSEED 变化；排序后导出可字节复现。
        text = _sort_asset_meshes(text)
        if name == "robot.xml":
            text = MJCF_NAME.sub('<mujoco model="robot"', text, count=1)
            if contact_excludes:
                text = contacts.apply(text, contact_excludes)
                manifest["contact_excludes"] = [list(pair) for pair in contact_excludes]
            manifest["mjcf"] = "mjcf/robot.xml"
        # 引擎用导出临时目录名当默认 class 名，会随每次运行变化；入库前改成稳定值
        text = MJCF_TEMP_CLASS.sub("robot", text)
        (root / "mjcf").mkdir(parents=True, exist_ok=True)
        (root / "mjcf" / name).write_text(text, encoding="utf-8")

    if manifest["urdf"] is None and manifest["mjcf"] is None:
        raise LayoutError(f"{engine_dir} 里没有引擎输出（robot.urdf / robot.xml）")
    return manifest


def _sort_asset_meshes(text: str) -> str:
    """排序 ``<asset>`` 里的网格声明；只改顺序，不改内容。"""

    block = MJCF_ASSET_BLOCK.search(text)
    if block is None:
        return text
    tags = MJCF_ASSET_MESH.findall(block.group(0))
    if len(tags) < 2:
        return text
    ordered = sorted(tags)
    if tags == ordered:
        return text
    replacement = iter(ordered)
    sorted_block = MJCF_ASSET_MESH.sub(lambda _match: next(replacement), block.group(0))
    return text[: block.start()] + sorted_block + text[block.end() :]


def mesh_names(xml_path: Path) -> list[str]:
    """列出 XML 里引用的网格名（URDF 用 ../meshes/x.stl，MJCF 用 mesh 资源名）。"""

    text = Path(xml_path).read_text(encoding="utf-8")
    if xml_path.suffix == ".urdf":
        return sorted({match.group("name") for match in URDF_MESH.finditer(text)})
    names = set()
    for match in MESH_NAME.finditer(text):
        names.add(match.group("name"))
    return sorted(names)


def write_manifest(root: Path, manifest: dict) -> Path:
    path = Path(root) / "onshape" / "layout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
