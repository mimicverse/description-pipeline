"""命令行入口：check / fetch / fetch-geometry / export / verify。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, UTC
from pathlib import Path
from typing import Literal, overload

from . import SCHEMA_VERSION, checks, engine, geometry, layout, verify
from .api import OnshapeClient, OnshapeError
from .assembly import Assembly
from .cache import ResponseCache
from .offline import safe_name
from .url import DEFAULT_STACK, DocumentRef, ReferenceError, parse_reference

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_DEPRECATED = 3


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return _command_check(args)
        if args.command == "fetch":
            return _command_fetch(args)
        if args.command == "fetch-geometry":
            return _command_fetch_geometry(args)
        if args.command == "export":
            return _command_export(args)
        if args.command == "verify":
            return _command_verify(args)
    except (
        ReferenceError,
        OnshapeError,
        engine.EngineError,
        layout.LayoutError,
        verify.VerifyError,
        geometry.GeometryError,
        FileNotFoundError,
        ValueError,
    ) as error:
        print(f"错误: {error}", file=sys.stderr)
        return EXIT_USAGE
    parser.print_help()
    return EXIT_USAGE


# --- 子命令 -------------------------------------------------------------


def _command_check(args) -> int:
    ref = _reference(args)
    client = _client(args)
    assembly, payload = _load_assembly(client, ref, args, with_mass=args.mass)
    findings = checks.run_checks(
        assembly,
        mass_properties=payload.get("mass"),
        allow_unlimited=args.allow_unlimited,
        auto_dof=getattr(args, "auto_dof", False),
    )
    summary = checks.summarize(findings)
    report = {
        "schema": SCHEMA_VERSION,
        "command": "check",
        "source": ref.as_dict(),
        "assembly": _assembly_summary(assembly),
        "summary": summary,
        "findings": [finding.as_dict() for finding in findings],
    }
    _emit(report, args, default_path=args.report)
    return EXIT_OK if summary["passed"] else EXIT_FINDINGS


def _command_fetch(args) -> int:
    ref = _reference(args)
    client = _client(args, cache=ResponseCache(args.cache))
    assembly, payload = _load_assembly(client, ref, args, with_mass=True)
    for element_id, _configuration in assembly.part_studios():
        client.get_studio_mass_properties(ref, element_id)
    _write_json(Path(args.cache) / "source.json", {**ref.as_dict(), "fetched_at": _now()})
    summary = {
        "schema": SCHEMA_VERSION,
        "command": "fetch",
        "source": ref.as_dict(),
        "cache": str(Path(args.cache).resolve()),
        "instances": len(assembly.instances),
        "mates": len(assembly.active_mates),
        "studios": [element for element, _ in assembly.part_studios()],
        "mass_missing": payload.get("mass_missing", []),
    }
    _emit(summary, args)
    return EXIT_OK


def _command_fetch_geometry(args) -> int:
    cache = ResponseCache(args.cache)
    ref, assembly = _assembly_from_cache(cache, args)
    client = _client(args, cache=cache)
    produced, unresolved = _write_geometry(client, cache, ref, assembly, tolerance=args.tolerance)
    report = {
        "schema": SCHEMA_VERSION,
        "command": "fetch-geometry",
        "source": ref.as_dict(),
        "parts_written": produced,
        "unresolved": unresolved,
        "summary": {"passed": not unresolved, "unresolved": len(unresolved)},
    }
    _emit(report, args)
    return EXIT_OK if not unresolved else EXIT_FINDINGS


def _write_geometry(client, cache: ResponseCache, ref, assembly, *, tolerance: float, ignore=None):
    """把装配体用到的零件网格写进缓存：优先 GLTF 拆分，缺失的回退到逐零件 STL。"""

    produced, unresolved = 0, []
    skipped = _ignored_part_ids(assembly, ignore or {})
    for element_id, _configuration in assembly.part_studios():
        bodies = cache.require_json(f"mass_properties_{element_id}").get("bodies", {})
        files: dict[str, bytes] = {}
        try:
            gltf = client.get_part_studio_gltf(ref, element_id)
            files, pending = geometry.split_gltf(gltf, bodies, tolerance=tolerance)
            unresolved += [{**item, "element_id": element_id} for item in pending]
        except (OnshapeError, geometry.GeometryError) as error:
            print(f"提示: GLTF 拆分不可用（{error}），回退到逐零件 STL", file=sys.stderr)
        for part_id, body in bodies.items():
            if part_id in files or part_id in skipped or not body.get("hasMass", True):
                continue
            if cache.load_bytes(f"stl_{safe_name(part_id)}.stl") is not None:
                continue
            files[part_id] = client.get_part_stl(ref, element_id, part_id)
        for part_id, data in files.items():
            cache.save_bytes(f"stl_{safe_name(part_id)}.stl", data)
            produced += 1
    return produced, unresolved


def _ignored_part_ids(assembly: Assembly, ignore: dict[str, str]) -> set[str]:
    """按引擎的 ignore 规则（零件实例名，去掉 <n> 后缀，fnmatch）筛出不需要网格的零件。"""

    import fnmatch

    if not ignore:
        return set()
    ignored: set[str] = set()
    entries = [(pattern.lower(), scope) for pattern, scope in ignore.items()]

    def matches(name: str) -> bool:
        base = "<".join(name.split("<")[:-1]).strip().lower()
        for pattern, scope in entries:
            if scope in {"all", "visual", "collision"} and fnmatch.fnmatch(base, pattern):
                return True
        return False

    for item in assembly.instances:
        if item.part_id and matches(item.name):
            ignored.add(item.part_id)
    for sub in assembly.raw.get("subAssemblies", []) or []:
        for item in sub.get("instances", []) or []:
            if item.get("partId") and matches(item.get("name", "")):
                ignored.add(item["partId"])
    return ignored


def _command_export(args) -> int:
    """旧引擎写通道：明确弃用，不做任何请求或写入。"""

    del args
    print(f"错误: {engine.DEPRECATED_MESSAGE}", file=sys.stderr)
    print(
        "迁移：description source freeze --root <模型仓库> → description build --root <模型仓库>；"
        "来源侧语义规范化在 description_pipeline.sources.onshape.normalize_scene。",
        file=sys.stderr,
    )
    print("本工具保留 check / fetch / fetch-geometry / verify（只读与历史回归），不再产生新模型。", file=sys.stderr)
    return EXIT_DEPRECATED


def _command_verify(args) -> int:
    root = Path(args.out)
    assembly = None
    source = Path(args.source) if args.source else root / "onshape" / "assembly.json"
    if source.is_file():
        source_data = json.loads(source.read_text(encoding="utf-8"))
        features = _read_or_empty(root / "onshape" / "assembly_features.json")
        mate_values = _read_or_empty(root / "onshape" / "mate_values.json")
        try:
            assembly = Assembly.from_responses(
                _reference(args, required=False) or _source_ref(root), source_data, features, mate_values
            )
        except Exception as error:  # noqa: BLE001 - 源数据缺失时退化为纯模型核验
            print(f"提示: 跳过与源装配体比对（{error}）", file=sys.stderr)
    report = verify.verify(
        root,
        assembly=assembly,
        reference=Path(args.reference) if args.reference else None,
        reference_mjcf=_reference_mjcf(args),
        use_mujoco=args.mujoco,
    )
    _emit(report, args, default_path=root / "onshape" / "verification.json")
    return EXIT_OK if report["summary"]["passed"] else EXIT_FINDINGS


# --- 公共步骤 -----------------------------------------------------------


def _load_assembly(client: OnshapeClient, ref: DocumentRef, args, *, with_mass: bool):
    assembly_json = client.get_assembly(ref)
    if "rootAssembly" not in assembly_json:
        elements = client.list_elements(ref)
        found = next((item for item in elements if item.get("id") == ref.element_id), {})
        kind = found.get("elementType", "UNKNOWN")
        raise ValueError(f"元素 {ref.element_id} 不是装配体（elementType={kind}）；URDF 需要 ASSEMBLY 元素")
    features = client.get_assembly_features(ref)
    mate_values = client.get_mate_values(ref)
    assembly = Assembly.from_responses(ref, assembly_json, features, mate_values)
    payload: dict = {}
    if with_mass:
        mass: dict[str, dict] = {}
        missing: list[str] = []
        for element_id, _configuration in assembly.part_studios():
            bodies = client.get_studio_mass_properties(ref, element_id).get("bodies", {})
            mass[element_id] = bodies
            if any(not body.get("hasMass", True) for body in bodies.values()):
                missing.append(element_id)
        payload = {"mass": mass, "mass_missing": missing}
    return assembly, payload


def _assembly_from_cache(cache: ResponseCache, args) -> tuple[DocumentRef, Assembly]:
    names = sorted(path.stem for path in cache.json_dir.glob("assembly_*.json"))
    if not names:
        raise FileNotFoundError("缓存里没有 assembly_*.json；先运行 fetch")
    element_id = names[0][len("assembly_") :]
    source = _read_or_empty(cache.root / "source.json")
    ref = parse_reference(
        source.get("url") if source else None,
        document_id=source.get("document_id") if source else None,
        element_id=element_id,
        workspace_id=source.get("workspace_id") if source else None,
        version_id=source.get("version_id") if source else None,
    )
    assembly = Assembly.from_responses(
        ref,
        cache.require_json(f"assembly_{element_id}"),
        cache.require_json(f"assembly_features_{element_id}"),
        cache.require_json(f"mate_values_{element_id}"),
    )
    return ref, assembly


@overload
def _reference(args, required: Literal[True] = True) -> DocumentRef: ...


@overload
def _reference(args, required: Literal[False] = False) -> DocumentRef | None: ...


def _reference(args, required: bool = True) -> DocumentRef | None:
    given = any(getattr(args, name, None) for name in ("url", "document_id", "element_id"))
    if not given:
        cached = _reference_from_cache(getattr(args, "cache", None))
        if cached is not None:
            return cached
        if not required:
            return None
    return parse_reference(
        getattr(args, "url", None),
        document_id=getattr(args, "document_id", None),
        element_id=getattr(args, "element_id", None),
        workspace_id=getattr(args, "workspace_id", None),
        version_id=getattr(args, "version_id", None),
        stack=getattr(args, "stack", DEFAULT_STACK) or DEFAULT_STACK,
    )


def _reference_from_cache(cache_dir) -> DocumentRef | None:
    """离线跑 check/export 时，引用可来自缓存里的 source.json。"""

    if not cache_dir:
        return None
    source_path = Path(cache_dir) / "source.json"
    if not source_path.is_file():
        return None
    source = json.loads(source_path.read_text(encoding="utf-8"))
    return parse_reference(
        source.get("url"),
        document_id=source.get("document_id"),
        element_id=source.get("element_id"),
        workspace_id=source.get("workspace_id"),
        version_id=source.get("version_id"),
        stack=source.get("stack", DEFAULT_STACK),
    )


def _source_ref(root: Path) -> DocumentRef:
    source = json.loads((root / "onshape" / "source.json").read_text(encoding="utf-8"))
    return parse_reference(
        source.get("url"),
        document_id=source.get("document_id"),
        element_id=source.get("element_id"),
        workspace_id=source.get("workspace_id"),
        version_id=source.get("version_id"),
    )


def _client(args, cache: ResponseCache | None = None) -> OnshapeClient:
    cache_dir = Path(args.cache) if getattr(args, "cache", None) else None
    if cache is None and cache_dir is not None:
        cache = ResponseCache(cache_dir, read_only=getattr(args, "offline", False))
    return OnshapeClient.from_env(
        getattr(args, "stack", DEFAULT_STACK) or DEFAULT_STACK,
        cache_dir=cache.root if cache else None,
        offline=bool(getattr(args, "offline", False)),
    )


def _assembly_summary(assembly: Assembly) -> dict:
    tree = assembly.tree_report()
    return {
        "instances": len(assembly.instances),
        "top_level_instances": [item.name for item in assembly.instances],
        "mates": len(assembly.active_mates),
        "dof_mates": len(assembly.dof_mates),
        "frame_mates": len(assembly.frame_mates),
        "tree": {key: value for key, value in tree.items() if key != "components"},
        "root_instance": assembly.root_instance.name if assembly.root_instance else None,
    }


def _reference_mjcf(args) -> Path | None:
    """参考 MJCF：显式给出，或取 --reference 同目录下的 robot.xml。"""

    explicit = getattr(args, "reference_mjcf", None)
    if explicit:
        return Path(explicit)
    reference = getattr(args, "reference", None)
    if not reference:
        return None
    candidate = Path(reference).parent / "robot.xml"
    return candidate if candidate.is_file() else None


MJCF_ONLY_KEYS = {"type", "damping", "armature", "frictionloss", "stiffness", "kp", "kv", "dampratio", "forcerange"}
URDF_ONLY_KEYS = {"max_effort", "max_velocity"}


def _joint_properties_for(properties: dict | None, output_format: str) -> dict | None:
    """引擎的 URDF 与 MJCF 共用 joint_properties，但键含义不同，需要按格式过滤。

    URDF 只认 type / max_effort / max_velocity 等；MJCF 只认 type / damping /
    armature / frictionloss / kp / dampratio / forcerange 等。不过滤会让 MJCF 的
    执行器类型（如 motor）写进 URDF 的关节类型。
    """

    if not properties:
        return None
    drop = MJCF_ONLY_KEYS if output_format == "urdf" else URDF_ONLY_KEYS
    filtered = {
        pattern: {key: value for key, value in entry.items() if key not in drop}
        for pattern, entry in properties.items()
    }
    return {pattern: entry for pattern, entry in filtered.items() if entry}


def _emit(report: dict, args, default_path: Path | None = None) -> None:
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if getattr(args, "json", False):
        print(text)
        return
    summary = report.get("summary", {})
    print(json.dumps({key: summary.get(key) for key in ("passed", "errors", "warnings", "infos")}, ensure_ascii=False))
    for finding in report.get("findings", []):
        print(f"  [{finding['severity']}] {finding['code']} {finding['message']}")
    target = getattr(args, "report", None) or default_path
    if target:
        _write_json(Path(target), report)
        print(f"报告: {target}")


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_or_empty(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if Path(path).is_file() else {}


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _relative(path, root: Path) -> str | None:
    """尽量写成相对导出目录的路径；实在在外部时只留末两级，避免机器相关绝对路径入库。"""

    if not path:
        return None
    target = Path(path)
    try:
        return str(target.resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return "/".join(target.parts[-2:])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="onshape_to_urdf",
        description="Onshape 装配体 → URDF/MJCF：导出前诊断、转换、导出后独立核验。",
    )
    parser.add_argument("--stack", default=DEFAULT_STACK, help="Onshape 站点，默认生产环境")
    subparsers = parser.add_subparsers(dest="command")

    def add_source(target: argparse.ArgumentParser, *, required: bool) -> None:
        target.add_argument("--url", help="Onshape 文档 URL（可带 /w|v/<id>/e/<eid>）")
        target.add_argument("--document-id")
        target.add_argument("--workspace-id")
        target.add_argument("--version-id")
        target.add_argument("--element-id")
        target.add_argument("--cache", help="缓存目录：读写 API 响应，可用 --offline 只读")
        target.add_argument("--offline", action="store_true", help="只用缓存，不发网络请求")
        target.add_argument("--json", action="store_true", help="只输出 JSON")
        if not required:
            target.add_argument("--report", help="把报告写到指定路径")

    check = subparsers.add_parser("check", help="导出前诊断（不写模型）")
    add_source(check, required=True)
    check.add_argument("--assembly-name", help="文档里有多个装配体时按名字选择")
    check.add_argument("--mass", action="store_true", help="额外检查零件质量是否缺失")
    check.add_argument("--allow-unlimited", action="store_true", help="允许无限制关节")
    check.add_argument("--auto-dof", action="store_true", help="把未按 dof_ 命名的转动/平动 mate 视为关节（见 OSX010）")
    check.add_argument("--report", help="把 JSON 报告写到该路径（默认只打印摘要）")

    fetch = subparsers.add_parser("fetch", help="抓取 JSON 数据到缓存（离线导出用）")
    add_source(fetch, required=True)
    fetch.add_argument("--assembly-name")

    fetch_geometry = subparsers.add_parser("fetch-geometry", help="抓取 GLTF 并拆成 per-part STL")
    add_source(fetch_geometry, required=True)
    fetch_geometry.add_argument("--tolerance", type=float, default=0.05, help="体积+质心匹配阈值")

    export = subparsers.add_parser("export", help="执行导出并做导出后核验")
    add_source(export, required=True)
    export.add_argument("--out", required=True, help="输出根目录（写 urdf/ meshes/ mjcf/ onshape/）")
    export.add_argument("--format", choices=["urdf", "mjcf", "both"], default="both")
    export.add_argument("--assembly-name", help="文档里有多个装配体时按名字选择")
    export.add_argument("--color", nargs=4, type=float, metavar=("R", "G", "B", "A"), default=[0.72, 0.72, 0.74, 1.0])
    export.add_argument(
        "--ignore", action="append", default=[], metavar="PATTERN", help="忽略的零件（可重复，如 --ignore 'part 1'）"
    )
    export.add_argument("--allow-unlimited", action="store_true")
    export.add_argument("--auto-dof", action="store_true", help="按 mate 类型自动判定关节/固定（未按约定命名的文档用）")
    export.add_argument(
        "--density-map", metavar="FILE", help="分类等效密度文件（JSON：parts/names → kg/m³），走缓存通道重算质量"
    )
    export.add_argument(
        "--joint-properties", metavar="FILE", help="关节/执行器属性（damping/armature/frictionloss/type…），透传给引擎"
    )
    export.add_argument(
        "--contact-exclude",
        metavar="FILE",
        help="接触排除表（JSON：[[body1, body2], …]），写进 MJCF <contact><exclude>",
    )
    export.add_argument("--force", action="store_true", help="检查有 error 时仍继续导出")
    export.add_argument("--mujoco", action="store_true", help="额外用 mujoco 真加载核验")
    export.add_argument("--reference", help="权威 URDF：逐关节比对位置/轴向/可达区间")
    export.add_argument("--reference-mjcf", metavar="FILE", help="权威 MJCF：比对执行器类型与关节阻尼/摩擦/转子惯量")
    export.add_argument("--report")

    verify_cmd = subparsers.add_parser("verify", help="只核验已有导出目录")
    verify_cmd.add_argument("--out", required=True)
    verify_cmd.add_argument("--source", help="源装配体 JSON（默认 <out>/onshape/assembly.json）")
    verify_cmd.add_argument("--mujoco", action="store_true")
    verify_cmd.add_argument("--reference", help="权威 URDF：逐关节比对位置/轴向/可达区间")
    verify_cmd.add_argument(
        "--reference-mjcf", metavar="FILE", help="权威 MJCF：比对执行器类型与关节阻尼/摩擦/转子惯量"
    )
    verify_cmd.add_argument("--json", action="store_true")
    verify_cmd.add_argument("--report")
    return parser
