"""`tools/audit.py` 的实现：命令行、策略判定、报告。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from . import SCHEMA, ledger as joint_ledger, model as model_module, rules, waivers as waivers_module
from .findings import ERROR, INFO, WARNING, sort_findings, summarize
from description_pipeline.io import inventory, pin_utf8_streams

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit.py",
        description="URDF 合同质检（URDF###）：结构、关节、惯性、几何、台账与例外。",
    )
    parser.add_argument("--root", default=".", help="模型根目录（默认当前目录）")
    parser.add_argument("--urdf", help="URDF 路径（默认 <root>/urdf/robot.urdf）")
    parser.add_argument("--mjcf", help="MJCF 路径（默认 <root>/mjcf/robot.xml，存在才检查）")
    parser.add_argument("--joint-names", help="结构关节台账（默认 <root>/config/joint_names.yaml）")
    parser.add_argument("--waivers", help="例外台账（默认 <root>/config/urdf_quality.json）")
    parser.add_argument(
        "--policy",
        choices=["strict", "advisory"],
        default="strict",
        help="strict：error 或未豁免 warning 都失败；advisory：只有 error 失败",
    )
    parser.add_argument(
        "--mujoco", action="store_true", help="额外把 MJCF 编译后 MuJoCo 眼里的质量/质心/惯量与 URDF 对照"
    )
    parser.add_argument("--today", help="判定例外是否过期用的日期（默认今天，便于复现）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 报告")
    parser.add_argument("--report", help="把 JSON 报告写到该路径")
    parser.add_argument(
        "--verify-report", metavar="PATH", help="只校验已提交的报告：存在、通过、且与当前 URDF 哈希一致"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    pin_utf8_streams()
    args = _parser().parse_args(argv)
    root = Path(args.root).resolve()
    urdf_path = Path(args.urdf).resolve() if args.urdf else root / "urdf" / "robot.urdf"
    mjcf_path = Path(args.mjcf).resolve() if args.mjcf else root / "mjcf" / "robot.xml"
    joint_names = Path(args.joint_names) if args.joint_names else root / "config" / "joint_names.yaml"
    waiver_path = Path(args.waivers) if args.waivers else root / "config" / "urdf_quality.json"
    today = date.fromisoformat(args.today) if args.today else date.today()

    if args.verify_report:
        return _verify_report(Path(args.verify_report), urdf_path, as_json=args.json)
    try:
        urdf = model_module.load_urdf(urdf_path)
    except model_module.ModelError as error:
        print(f"错误: {error}", file=sys.stderr)
        return EXIT_USAGE
    mjcf = None
    if mjcf_path.is_file():
        try:
            mjcf = model_module.load_mjcf(mjcf_path)
        except model_module.ModelError as error:
            print(f"错误: {error}", file=sys.stderr)
            return EXIT_USAGE

    ledger = waivers_module.load(waiver_path)
    context = rules.Context(
        root=root,
        urdf=urdf,
        mjcf=mjcf,
        joint_ledger=joint_ledger.load(joint_names),
        massless_links=ledger.massless_links,
        template=_is_template(root),
        mujoco=args.mujoco,
    )
    for message in ledger.errors:
        context.add("URDF701", ERROR, f"例外台账格式错误：{message}")
    findings = rules.run(context)
    kept, waived, _unused = waivers_module.apply(findings, ledger, today, not_evaluated=rules.not_evaluated(context))
    kept = sort_findings(kept)
    summary = summarize(kept)
    summary["waived"] = len(waived)
    failed = summary[ERROR] > 0 or (args.policy == "strict" and summary[WARNING] > 0)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "policy": args.policy,
        "input_files": _report_inputs(root),
        "rule_files": {path.name: _sha256(path) for path in Path(__file__).parent.glob("*.py")},
        "model": {
            "name": urdf.name,
            "urdf_sha256": _sha256(urdf_path),
            "links": len(urdf.links),
            "joints": len(urdf.joints),
            "moveable_joints": sum(1 for joint in urdf.joints.values() if joint.moveable),
            "meshes": len({mesh.uri for mesh in urdf.mesh_references()}),
            "total_mass_kg": round(
                sum(
                    link.inertial.mass
                    for link in urdf.links.values()
                    if link.inertial is not None and link.inertial.finite()
                ),
                6,
            ),
            "template": context.template,
            "compiled_bodies_compared": context.compiled_bodies,
            "mujoco_version": context.compiled_version,
            "fk_max_error_m": round(context.compiled_fk_error, 9),
            "self_contact_poses": context.contact_counts,
        },
        "summary": summary,
        "passed": not failed,
        "findings": [finding.as_dict() for finding in kept],
        "waived": waived,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(payload, encoding="utf-8", newline="\n")
    if args.json:
        sys.stdout.write(payload)
    else:
        icon = {ERROR: "error", WARNING: "warning", INFO: "info"}
        for finding in kept:
            if finding.severity == INFO and not args.json:
                continue
            subject = f" [{finding.subject}]" if finding.subject else ""
            print(f"  [{icon[finding.severity]}] {finding.code}{subject} {finding.message}")
        state = "通过" if not failed else "未通过"
        print(
            f"{state}: {summary[ERROR]} error / {summary[WARNING]} warning / "
            f"{summary[INFO]} info / {summary['waived']} 已豁免（策略 {args.policy}）"
        )
        if args.report:
            print(f"报告: {args.report}")
    return EXIT_OK if not failed else EXIT_FINDINGS


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report_inputs(root: Path) -> dict:
    return {
        f"{name}/{relative}": sha
        for name in ("urdf", "mjcf", "meshes", "config")
        for relative, sha in inventory(root / name).items()
    }


def _verify_report(report_path: Path, urdf_path: Path, *, as_json: bool = False) -> int:
    """门禁：报告必须存在、通过，并且对应当前 URDF 的字节。

    ``--json`` 时输出结构化结果（便于 CI 解析），否则打印一行人类可读结论。
    """

    def emit(ok: bool, reason: str, **extra) -> int:
        payload = {"schema": SCHEMA, "command": "verify-report", "ok": ok, "reason": reason, **extra}
        if as_json:
            print(json.dumps(payload, ensure_ascii=False))
        elif ok:
            print(f"报告有效: {report_path}（{extra.get('summary')}）")
        else:
            print(f"错误: {reason}", file=sys.stderr)
        return EXIT_OK if ok else EXIT_FINDINGS

    if not report_path.is_file():
        return emit(
            False,
            f"缺少已提交的质检报告 {report_path}（跑 tools/audit.py --report 后提交）",
            report=str(report_path),
        )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return emit(False, f"{report_path} 不是合法 JSON：{error}", report=str(report_path))
    if not report.get("passed"):
        return emit(
            False,
            f"已提交的报告未通过（{report.get('summary')}）",
            report=str(report_path),
            summary=report.get("summary"),
        )
    if report.get("model", {}).get("urdf_sha256") != _sha256(urdf_path):
        return emit(
            False,
            "URDF 与已提交报告不一致（模型改过？重跑 tools/audit.py 并提交报告）",
            report=str(report_path),
        )
    if report.get("input_files") != _report_inputs(urdf_path.parent.parent):
        return emit(False, "模型/网格/配置与报告不一致；必须重新验算")
    if report.get("rule_files") != {path.name: _sha256(path) for path in Path(__file__).parent.glob("*.py")}:
        return emit(False, "验算规则与报告不一致；必须重新验算")
    return emit(True, "报告有效", report=str(report_path), summary=report.get("summary"))


def _is_template(root: Path) -> bool:
    contract = root / "config" / "model_contract.json"
    if not contract.is_file():
        return False
    try:
        return json.loads(contract.read_text(encoding="utf-8")).get("state") == "template"
    except (OSError, json.JSONDecodeError):
        return False
