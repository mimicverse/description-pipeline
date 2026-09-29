"""仓库卫生回归：顶层文件受控、CI 引用的脚本存在、YAML 配置可解析。"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]

#: Source archives (sdist, ``git archive``, release tarballs) carry no Git metadata.  The checks read
#: the index, so they need a work tree; everywhere else they must be an explicit skip rather than a
#: ``git ls-files`` traceback.  The probe runs the command the checks actually use: a directory inside
#: somebody else's repository satisfies ``git rev-parse`` and then fails ``git ls-files``.
GIT_CHECKOUT = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True).returncode == 0

# main 的顶层条目白名单：新增顶层文件必须是有意识的决定（历史上误入库过
# `102` / `All` / `.coverage` 这类重定向残留与覆盖数据库）。
ALLOWED_TOP_LEVEL = {
    ".editorconfig",
    ".gitattributes",
    ".github",
    ".gitignore",
    ".gitleaks.toml",
    ".pre-commit-config.yaml",
    "CONTRIBUTING.md",
    "CONTRIBUTING.en.md",
    "CHANGELOG.md",
    "README.md",
    "README.en.md",
    "RELEASING.md",
    "SECURITY.md",
    "LICENSE",
    "CODE_OF_CONDUCT.md",
    "config",
    "docs",
    "examples",
    "meshes",
    "mjcf",
    "mypy.ini",
    "ruff.toml",
    "tests",
    "tools",
    "urdf",
    "src",
    "pyproject.toml",
    "deploy",
    "requirements",
    "templates",
}
# 明确禁止的产物/缓存（即使将来进了白名单也不允许）
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".zip", ".coverage")
FORBIDDEN_NAMES = {"__pycache__", ".coverage", "dist", "build"}

try:  # 可选依赖：装了 PyYAML 才做结构解析
    import yaml

    YAML_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    YAML_AVAILABLE = False


def repo_files(root: Path | None = None) -> list[str]:
    """已跟踪 + 未忽略的未跟踪文件：本地也能在提交前发现新顶层文件。

    未跟踪的虚拟环境不算仓库内容；已跟踪的文件仍要接受卫生检查。
    `.gitignore` 只登记了文档推荐的 `.venv`，但贡献者也会使用其他名称。
    """

    base = root if root is not None else ROOT
    tracked = subprocess.run(["git", "ls-files"], cwd=base, capture_output=True, text=True, check=True)
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=base,
        capture_output=True,
        text=True,
        check=True,
    )
    names = {name for name in tracked.stdout.splitlines() if name.strip()}
    names.update(name for name in untracked.stdout.splitlines() if name.strip() and not is_virtualenv_path(name, base))
    return sorted(names)


def is_virtualenv_path(name: str, root: Path | None = None) -> bool:
    """仓库内路径是否位于某个 Python 虚拟环境里（名字任意，看 `pyvenv.cfg`）。"""

    base = root if root is not None else ROOT
    parts = Path(name).parts
    return bool(parts) and (base / parts[0] / "pyvenv.cfg").is_file()


def unexpected_top_level(names: list[str]) -> list[str]:
    """Top-level entries that are not on the intentional whitelist."""

    return sorted({Path(name).parts[0] for name in names} - ALLOWED_TOP_LEVEL)


def missing_workflow_scripts(names: list[tuple[str, str]]) -> list[str]:
    """Scripts a workflow names that are not in this checkout; ``names`` is (file, text)."""

    missing: list[str] = []
    for name, text in names:
        for token in text.split():
            # 带引号的 token 是 unittest 的 -p 模式，不是路径。
            if token.startswith(("'", '"')) or not token.endswith(".py"):
                continue
            candidate = token.strip("'\"")
            # CI 里的 tooling/ 是 main 的二次检出，等价于仓库根。
            for prefix in ("tooling/", "model/", "driver/"):
                if candidate.startswith(prefix):
                    candidate = candidate[len(prefix) :]
            if not (ROOT / candidate).exists():
                missing.append(f"{name}: {token}")
    return missing


@unittest.skipUnless(GIT_CHECKOUT, "top-level hygiene needs a Git work tree")
class TopLevelHygieneTests(unittest.TestCase):
    def test_top_level_entries_are_intentional(self):
        names = repo_files()
        self.assertGreater(len(names), 50, "仓库文件枚举异常：数量过少")
        self.assertEqual(unexpected_top_level(names), [], "顶层出现未登记的条目，请确认是否应入库")

    def test_an_unregistered_top_level_entry_is_reported(self):
        """If the enumeration ever returns nothing, the check above would pass for the wrong reason."""

        self.assertEqual(unexpected_top_level(["src/x.py", "README.md", "stray.txt"]), ["stray.txt"])
        self.assertEqual(unexpected_top_level(["src/x.py", "README.md"]), [])

    def test_no_build_artifacts_are_tracked(self):
        offenders = [
            name
            for name in repo_files()
            if name.endswith(FORBIDDEN_SUFFIXES)
            or (set(Path(name).parts) & (FORBIDDEN_NAMES - {"build"}))
            or Path(name).parts[0] == "build"
        ]
        self.assertEqual(offenders, [], "构建产物/缓存不应入库")

    def test_a_virtualenv_is_repository_content_only_by_name(self):
        """An environment called `.venv312` is still an environment, not a new top-level entry."""

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "env312").mkdir()
            (root / "env312" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
            (root / "stray").mkdir()
            self.assertTrue(is_virtualenv_path("env312/lib/python3.12/site-packages/x.zip", root))
            self.assertTrue(is_virtualenv_path("env312", root))
            self.assertFalse(is_virtualenv_path("stray/x.zip", root))
            self.assertFalse(is_virtualenv_path("", root))

    def test_tracked_virtualenv_files_are_still_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            env = root / "env312"
            env.mkdir()
            (env / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
            (env / "untracked.zip").write_bytes(b"artifact")
            (root / "stray.txt").write_text("stray\n", encoding="utf-8")
            subprocess.run(["git", "add", "env312/pyvenv.cfg"], cwd=root, check=True)
            self.assertEqual(repo_files(root), ["env312/pyvenv.cfg", "stray.txt"])


class WorkflowReferenceTests(unittest.TestCase):
    def test_windows_dev_lock_stays_in_step_with_the_runtime_lock(self):
        """The dev set is the packaged runtime plus tools; a version drift would fake a green gate."""

        def pinned(path: Path) -> dict[str, str]:
            entries: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                name, separator, version = line.partition("==")
                if separator:
                    entries[canonicalize_name(name)] = version.strip()
            return entries

        runtime = pinned(ROOT / "src/description_pipeline/sources/solidworks/deploy/requirements/win-py312.lock")
        dev = pinned(ROOT / "requirements/win-py312-dev.lock")
        self.assertGreaterEqual(len(runtime), 10, "the runtime lock should pin the worker set")
        for name, version in sorted(runtime.items()):
            with self.subTest(package=name):
                self.assertEqual(dev.get(name), version, f"{name} differs between the two Windows locks")
        for tool in ("ruff", "mypy", "build", "setuptools", "wheel"):
            with self.subTest(tool=tool):
                self.assertIn(tool, dev, "the dev lock has to carry every tool the gate runs")

    """CI 里引用的脚本必须存在——重命名工具却忘了改 workflow 时立刻失败。"""

    def test_workflow_script_references_exist(self):
        workflows = [
            (path.name, path.read_text(encoding="utf-8"))
            for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        ]
        self.assertGreaterEqual(len(workflows), 5, "工作流枚举异常")
        missing = missing_workflow_scripts(workflows)
        self.assertEqual(missing, [], "workflow 引用了不存在的脚本")

    def test_a_workflow_script_that_is_not_there_is_reported(self):
        cases = [("planted.yml", "run: python tools/does-not-exist.py")]
        self.assertEqual(missing_workflow_scripts(cases), ["planted.yml: tools/does-not-exist.py"])
        self.assertEqual(missing_workflow_scripts([("ok.yml", "run: python tools/quality.py all")]), [])

    def test_workflows_use_the_shared_quality_entry(self):
        validate = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
        for fragment in (
            "tools/quality.py lint",
            "tools/quality.py format",
            "tools/quality.py typecheck",
            "tools/quality.py test",
            "description model layout",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, validate, "CI 与本地入口不一致")
        model = (ROOT / ".github/workflows/model-validation.yml").read_text(encoding="utf-8")
        self.assertIn("description check --root model", model)
        self.assertIn("inputs.model_sha", model)

    @unittest.skipUnless(YAML_AVAILABLE, "未安装 PyYAML，跳过结构解析")
    def test_yaml_configs_parse(self):
        for path in sorted((ROOT / ".github").rglob("*.yml")):
            with self.subTest(path=path.name):
                self.assertIsInstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)
        for name in (".pre-commit-config.yaml", "ruff.toml"):
            path = ROOT / name
            if name.endswith(".yaml"):
                self.assertIsInstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)


if __name__ == "__main__":
    unittest.main()
