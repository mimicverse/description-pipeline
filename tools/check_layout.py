"""Check repository layout and resource references, not robot physics.

两种角色（由 ``config/model_contract.json`` 的 state 自动判定，也可以显式指定）：

* ``main``：工具、测试、CI 与规范文档只在这里；根目录不放模型；旧格式空模板仅用于兼容回归。
* ``model``（feature/release 分支）：只放资产——模型、网格、契约、质量记录与来源
  证据；出现 ``tools/``、``tests/``、``.github/`` 或 main 专属规范文档即失败。

模型分支的额外证据目录由 ``config/model_contract.json`` 的 ``evidence`` 数组显式
声明（例如 ``["onshape"]``）；未声明的顶层目录会被拒绝，避免工具悄悄回流。

Normal use is read-only: no conversion, simulation or repair. Exit 0 only means
the requested role's interface passed. 工具在 main 时可用 ``--root`` 指向另一个
工作区（模型分支）做检查。
"""

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]

# 资产：main 与模型分支都必须有
ASSET_FILES = (
    "README.md",
    "urdf/robot.urdf",
    "mjcf/robot.xml",
    "mjcf/scene.xml",
    "config/model_contract.json",
    "config/joint_names.yaml",
    "config/urdf_quality.json",
    "docs/quality.md",
    "docs/quality.json",
    "docs/provenance/README.md",
)

# 工具与规范：只放在 main
TOOL_FILES = (
    "docs/engineering_standard.md",
    "docs/urdf_standard.md",
    "docs/onshape_export.md",
    "docs/solidworks_export.md",
    "tools/check_layout.py",
    "tools/audit.py",
    "tools/quality.py",
    "tests/test_layout.py",
    "ruff.toml",
    "mypy.ini",
    ".editorconfig",
    ".pre-commit-config.yaml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/dependabot.yml",
)

# 模型分支允许的顶层名字（其余必须写进 contract 的 evidence）
ASSET_TOP_LEVEL = {"README.md", ".gitignore", ".gitattributes", "urdf", "meshes", "mjcf", "config", "docs"}
TOOLING_TOP_LEVEL = ("tools", "tests", ".github")
MAIN_ONLY_DOCS = (
    "docs/engineering_standard.md",
    "docs/urdf_standard.md",
    "docs/onshape_export.md",
    "docs/solidworks_export.md",
)
# main 专属的顶层隐藏文件（.git/.gitignore/.gitattributes 之外都不该出现在模型分支）
MAIN_ONLY_DOTFILES = (".editorconfig", ".pre-commit-config.yaml")

ROS_DISTRIBUTIONS = {
    "catkin-pkg",
    "urdf-parser-py",
    "rospkg",
    "rosdep",
    "rosdistro",
    "rospy",
    "rclpy",
    "xacro",
    "ament-package",
    "launch-ros",
}
ROS_MODULES = {name.replace("-", "_") for name in ROS_DISTRIBUTIONS}


def check_ros_free(root):
    """Check operational files only; immutable provenance is not a runtime input."""
    for relative in ("package.xml", "CMakeLists.txt", "config/display.rviz"):
        if (root / relative).exists():
            raise ValueError(f"ROS build/display entry is forbidden: {relative}")
    if (root / "launch").exists():
        raise ValueError("ROS launch directory is forbidden")
    for path in (root / "tools").glob("requirements*.txt"):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*([\w.-]+)", line)
            if match and match[1].lower().replace("_", "-") in ROS_DISTRIBUTIONS:
                raise ValueError(f"ROS dependency is forbidden: {path.name}: {line}")
    for path in (root / "tools").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [n.name for n in node.names]
                if isinstance(node, ast.Import)
                else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            )
            if any(name.split(".")[0] in ROS_MODULES for name in names):
                raise ValueError(f"ROS import is forbidden: {path.name}")


def resolve_mesh(root, uri):
    """Canonical paths are relative to urdf/robot.urdf, not to a ROS package."""
    if not uri.startswith("../meshes/"):
        raise ValueError(f"Noncanonical mesh reference: {uri}; use ../meshes/<file>")
    path = (root / "urdf" / uri).resolve()
    if not path.is_relative_to((root / "meshes").resolve()) or not path.is_file():
        raise ValueError(f"Missing or escaped mesh: {uri}")
    return path


def _contract(root: Path) -> dict:
    path = root / "config/model_contract.json"
    if not path.is_file():
        raise ValueError("Missing common entry: config/model_contract.json")
    contract = json.loads(path.read_bytes())
    if contract.get("state") not in ("template", "candidate", "verified"):
        raise ValueError("Unknown model state")
    return contract


def _role(root: Path, branch: str, state: str) -> str:
    if branch == "main":
        return "main"
    if branch.startswith(("feature/", "release/")):
        return "model"
    return "main" if state == "template" else "model"


def check(root=ROOT, branch="", role="auto"):
    root = Path(root).resolve()
    if (root / "pyproject.toml").is_file() or (root / "config/robot.yaml").is_file():
        from description_pipeline.repository import check_layout

        tooling = (root / "pyproject.toml").is_file()
        if (tooling and role == "model") or (not tooling and (role == "main" or branch == "main")):
            raise ValueError("Role mismatch: tooling-only main and asset-only model branches are separate")
        check_layout(root, "tooling" if tooling else "model")
        check_ros_free(root)
        return {
            "role": "main" if tooling else "model",
            "state": "tooling" if tooling else "candidate",
            "candidate": not tooling,
            "layout_passed": True,
        }
    contract = _contract(root)
    state = contract["state"]
    if role == "auto":
        role = _role(root, branch, state)
    required: tuple[str, ...]
    if role == "main":
        if state != "template":
            raise ValueError(
                "main must remain a hardware-free scaffold：工具、测试、CI 与规范文档只放 "
                f"main，模型放 feature/release 分支（当前 state={state}）"
            )
        required = ASSET_FILES + TOOL_FILES
    else:
        if branch == "main":
            raise ValueError("main must remain a hardware-free scaffold")
        if state == "template":
            raise ValueError("A hardware branch must not ship an empty template")
        for name in TOOLING_TOP_LEVEL:
            if (root / name).exists():
                raise ValueError(f"{name}/ 只放 main：feature/release 分支只提交资产与来源证据")
        for relative in MAIN_ONLY_DOCS:
            if (root / relative).is_file():
                raise ValueError(f"{relative} 只放 main（工具与规范文档不随模型分支发布）")
        for name in MAIN_ONLY_DOTFILES:
            if (root / name).is_file():
                raise ValueError(f"{name} 只放 main（工具配置不随模型分支发布）")
        allowed = set(ASSET_TOP_LEVEL)
        evidence = contract.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(name, str) for name in evidence):
            raise ValueError('contract 的 evidence 必须是字符串数组（如 ["onshape"]）')
        allowed |= set(evidence)
        for entry in sorted(root.iterdir()):
            if entry.name in {".git", ".gitignore", ".gitattributes"} or entry.name in allowed:
                continue
            raise ValueError(
                f"模型分支出现未声明的顶层条目 {entry.name}/：资产分支只允许 "
                f"{sorted(ASSET_TOP_LEVEL)} 与 contract.evidence 声明的证据目录"
            )
        required = ASSET_FILES

    check_ros_free(root)
    for relative in required:
        if not (root / relative).is_file():
            raise ValueError(f"Missing common entry: {relative}")
    if not (root / "meshes").is_dir():
        raise ValueError("Missing meshes directory")
    if contract["model_id"] != "robot":
        raise ValueError("Model ID must be robot, independent of hardware version")
    if sorted(p.name for p in (root / "urdf").glob("*.urdf")) != ["robot.urdf"]:
        raise ValueError("Use exactly one public URDF entry: urdf/robot.urdf")
    urdf = ET.parse(root / "urdf/robot.urdf").getroot()
    mjcf = ET.parse(root / "mjcf/robot.xml").getroot()
    if urdf.tag != "robot" or urdf.get("name") != "robot":
        raise ValueError("URDF robot name must be robot")
    if mjcf.tag != "mujoco" or mjcf.get("model") != "robot":
        raise ValueError("MJCF model name must be robot")
    if state == "template":
        if urdf.findall("link") or mjcf.findall(".//body"):
            raise ValueError("Template must not contain invented hardware")
    elif not urdf.findall("link") or not mjcf.findall(".//body"):
        raise ValueError("Non-template model has no links/bodies")
    for mesh in urdf.findall(".//mesh"):
        resolve_mesh(root, mesh.get("filename", ""))
    scene = ET.parse(root / "mjcf/scene.xml").getroot()
    if [e.get("file") for e in scene.findall("include")] != ["robot.xml"]:
        raise ValueError("Scene must include the common robot.xml entry")
    return {
        "state": state,
        "role": role,
        "candidate": state != "template",
        "layout_passed": True,
    }


def _pin_utf8_streams() -> None:
    """Windows encodes redirected streams with the ANSI code page; callers read UTF-8."""

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """命令行入口：检查一个工作区并把结论打到 stdout（CI 用 --github-output）。"""

    _pin_utf8_streams()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", default=str(ROOT), help="要检查的工作区（默认本仓库；工具在 main 时可指向模型分支工作区）"
    )
    parser.add_argument("--role", choices=["auto", "main", "model"], default="auto")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    target = Path(args.root).resolve()
    base = os.environ.get("GITHUB_BASE_REF", "")
    branch = ""
    if target == ROOT:  # 只有检查工具所在的工作区时，CI 分支名才有意义
        branch = (
            base
            if base == "main" or base.startswith(("feature/", "release/"))
            else (os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME", ""))
        )
    result = check(target, branch=branch, role=args.role)
    print(json.dumps(result))
    if args.github_output:
        args.github_output.parent.mkdir(parents=True, exist_ok=True)
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"candidate={str(result['candidate']).lower()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
