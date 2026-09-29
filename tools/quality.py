#!/usr/bin/env python3
"""仓库质量入口：本地、CI 与文档用同一套命令。

    python tools/quality.py all             # lint + format + 类型 + 测试 + 当前仓库角色检查
    python tools/quality.py all --fast      # 跳过测试（改文档/配置时用）
    python tools/quality.py lint            # ruff check（配置见 ruff.toml）
    python tools/quality.py format          # ruff format --check
    python tools/quality.py typecheck       # mypy（配置见 mypy.ini）
    python tools/quality.py test            # unittest（含 SolidWorks 无 Windows 回归）
    python tools/quality.py model --root DIR [--policy strict|advisory] [--mujoco]

退出码：0 全部通过；1 有失败；2 用法错误。
ruff / mypy 没装时对应步骤记为失败，除非显式 `--allow-missing-tools`。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUFF_SPEC = "ruff==0.16.8"
MYPY_SPEC = "mypy==2.3.1"
DEV_TOOLS = {"ruff": RUFF_SPEC, "mypy": MYPY_SPEC}


def tmpdir_problems(environ: dict[str, str] | None = None) -> list[str]:
    """A ``TMPDIR`` the suite cannot use, named as the real cause.

    The full suite writes gigabytes into ``TMPDIR``, and the diagram check hands that path to a
    browser process.  A relative or missing directory therefore surfaces as ``mkdtemp ENOENT``
    inside a Mermaid render, or as temporary directories appearing inside the checkout, which then
    trips the top-level hygiene check - neither message names what to fix.
    """

    value = (environ if environ is not None else os.environ).get("TMPDIR", "")
    if not value:
        return []
    path = Path(value)
    if not path.is_absolute():
        return [f"TMPDIR is relative ({value}): use an absolute directory, for example TMPDIR=/var/tmp/description"]
    if not path.is_dir():
        return [f"TMPDIR does not exist ({value}): create it first, it needs room for a few gigabytes"]
    if not os.access(path, os.W_OK):
        return [f"TMPDIR is not writable ({value})"]
    return []


@dataclass(frozen=True)
class Step:
    name: str
    command: list[str]
    tool: str = ""  # 需要的外部开发工具（缺失时按策略失败或跳过）


def ruff_command() -> list[str] | None:
    """优先用 PATH 里的 ruff，其次用当前解释器的模块入口。"""

    return _module_command("ruff")


def mypy_command() -> list[str] | None:
    return _module_command("mypy")


def _module_command(name: str) -> list[str] | None:
    """优先用 PATH 里的可执行文件，其次用当前解释器的模块入口。"""

    found = shutil.which(name)
    if found:
        return [found]
    try:
        subprocess.run(
            [sys.executable, "-m", name, "--version"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [sys.executable, "-m", name]


def tool_command(tool: str) -> list[str] | None:
    if tool == "ruff":
        return ruff_command()
    if tool == "mypy":
        return mypy_command()
    return None


def plan(
    command: str,
    *,
    root: Path,
    policy: str = "strict",
    mujoco: bool = False,
    fast: bool = False,
    require_report: bool = False,
    ruff: list[str] | None = None,
    mypy: list[str] | None = None,
    platform: str | None = None,
) -> list[Step]:
    """返回要执行的步骤；纯函数，便于测试与文档引用。"""

    platform = platform or os.name
    python = sys.executable
    ruff = ruff if ruff is not None else ruff_command()
    mypy = mypy if mypy is not None else mypy_command()
    lint = [
        Step(
            "lint",
            [*ruff, "check", "--config", "ruff.toml", "src", "tools", "tests", ".github/scripts"],
            "ruff",
        )
        if ruff
        else Step("lint", [], "ruff"),
        Step(
            "format-check",
            [*ruff, "format", "--check", "--config", "ruff.toml", "src", "tools", "tests", ".github/scripts"],
            "ruff",
        )
        if ruff
        else Step("format-check", [], "ruff"),
    ]
    # Two passes on purpose: the second one type-checks against the Windows stubs from Linux, which
    # is the only way this machine sees a Windows-only attribute (`os.geteuid`, `signal.SIGKILL`)
    # before the native Windows run does.  It caught exactly that in tests/pipeline/.
    typecheck = (
        [
            Step("typecheck", [*mypy, "--config-file", "mypy.ini"], "mypy"),
            Step("typecheck-win32", [*mypy, "--config-file", "mypy.ini", "--platform", "win32"], "mypy"),
        ]
        if mypy
        else [Step("typecheck", [], "mypy"), Step("typecheck-win32", [], "mypy")]
    )
    tests = [Step("test", [python, "-m", "unittest", "discover", "-s", "tests"])]
    # The two PowerShell suites exercise the shipped launchers with stand-ins, and they only run on
    # Windows: their fixtures use `C:\...` paths, so pwsh on Linux cannot even resolve the drive.
    # GitHub Actions used to run them; while the organisation's billing keeps jobs from starting they
    # were executed by nobody at all, which is why the Windows gate runs them now.
    launchers = (
        [
            Step(
                f"powershell-{name}",
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "tests" / "windows" / script),
                ],
            )
            for name, script in (("deployment", "test_deployment.ps1"), ("submit", "test_submit.ps1"))
        ]
        if platform == "nt" and (powershell := shutil.which("powershell"))
        else []
    )
    model = [
        Step("layout", [python, str(ROOT / "tools" / "check_layout.py"), "--root", str(root)]),
        Step(
            "urdf-audit",
            [
                python,
                str(ROOT / "tools" / "audit.py"),
                "--root",
                str(root),
                "--policy",
                policy,
                *(["--mujoco"] if mujoco else []),
            ],
        ),
    ]
    if (root / "pyproject.toml").is_file():
        model = [
            Step(
                "layout",
                [python, "-m", "description_pipeline", "model", "layout", "--root", str(root), "--role", "tooling"],
            )
        ]
    elif (root / "config/robot.yaml").is_file():
        model = [Step("qualification", [python, "-m", "description_pipeline", "check", "--root", str(root)])]
    report = root / "docs" / "urdf_audit.json"
    if report.is_file() or require_report:
        model.append(
            Step(
                "urdf-audit-report",
                [
                    python,
                    str(ROOT / "tools" / "audit.py"),
                    "--root",
                    str(root),
                    "--verify-report",
                    str(report),
                ],
            )
        )
    if command == "lint":
        return lint[:1]
    if command == "format":
        return lint[1:]
    if command == "typecheck":
        return typecheck
    if command == "test":
        return tests
    if command == "model":
        return model
    return [*lint, *typecheck, *([] if fast else [*tests, *launchers]), *model]


def run(steps: list[Step], *, allow_missing_tools: bool) -> int:
    # Windows reads configuration files with the ANSI code page unless UTF-8 mode is on, and mypy's
    # configparser then fails on the Chinese comments in `mypy.ini` before it type-checks anything.
    # The gate has to behave the same on the machine the pipeline targets as it does in CI, so every
    # step runs in UTF-8 mode.
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    failed = 0
    for step in steps:
        if step.name == "test":
            problems = tmpdir_problems()
            if problems:
                print(f"[run ] {step.name}: {' '.join(step.command)}")
                for problem in problems:
                    print(f"       TMPDIR: {problem}")
                print(f"[fail] {step.name} 退出码 1")
                failed += 1
                continue
        if step.tool and tool_command(step.tool) is None:
            if allow_missing_tools:
                print(f"[skip] {step.name}：未安装 {step.tool}（--allow-missing-tools）")
                continue
            print(f"[fail] {step.name}：未安装 {step.tool}，先 pip install {DEV_TOOLS[step.tool]}")
            failed += 1
            continue
        print(f"[run ] {step.name}: {' '.join(step.command)}")
        result = subprocess.run(step.command, cwd=ROOT, env=environment)
        if result.returncode != 0:
            print(f"[fail] {step.name} 退出码 {result.returncode}")
            failed += 1
        else:
            print(f"[ok  ] {step.name}")
    print(f"\n{'通过' if not failed else '未通过'}: {len(steps) - failed}/{len(steps)} 步")
    return 0 if not failed else 1


def _pin_utf8_streams() -> None:
    """Windows encodes redirected streams with the ANSI code page; callers read UTF-8."""

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _pin_utf8_streams()
    parser = argparse.ArgumentParser(prog="quality.py", description=__doc__)
    parser.add_argument("command", choices=["all", "lint", "format", "typecheck", "test", "model"])
    parser.add_argument("--root", default=str(ROOT), help="模型工作区（model/all 用）")
    parser.add_argument("--policy", choices=["strict", "advisory"], default="strict")
    parser.add_argument("--mujoco", action="store_true", help="URDF 质检加编译层比对")
    parser.add_argument("--fast", action="store_true", help="all 时跳过测试")
    parser.add_argument(
        "--require-report", action="store_true", help="model/all 时要求已提交 docs/urdf_audit.json（CI 用）"
    )
    parser.add_argument(
        "--allow-missing-tools", action="store_true", help="没装 ruff 时把 lint/format 记成跳过而不是失败"
    )
    args = parser.parse_args(argv)
    steps = plan(
        args.command,
        root=Path(args.root).resolve(),
        policy=args.policy,
        mujoco=args.mujoco,
        fast=args.fast,
        require_report=args.require_report,
    )
    return run(steps, allow_missing_tools=args.allow_missing_tools)


if __name__ == "__main__":
    sys.exit(main())
