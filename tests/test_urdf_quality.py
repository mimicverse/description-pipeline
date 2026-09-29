"""URDF 合同质检的回归：每条规则都要有一个能触发它的反例。

用例只依赖标准库：临时目录里造最小模型（一个立方体网格 + 两个 link + 两个关节），
再按规则逐条注入缺陷，断言对应编号出现。
"""

import contextlib
import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from urdf_quality import cli  # noqa: E402
from tests import onshape_fixture  # noqa: E402

MODEL = ROOT / "tests" / "fixtures" / "onshape" / "model"
JOINT_NAMES = ROOT / "tests/fixtures/legacy-template/config/joint_names.yaml"

INERTIA = {"ixx": "0.000833333", "ixy": "0", "ixz": "0", "iyy": "0.000833333", "iyz": "0", "izz": "0.000833333"}


def cube_stl(size: float = 0.1) -> bytes:
    """二进制 STL 立方体（12 面，封闭，体积 = size³）。"""

    return stl_bytes([(0.0, 0.0, 0.0)] * 0 + cube_faces(size))


def stl_bytes(faces) -> bytes:
    out = bytearray(b"\0" * 80 + struct.pack("<I", len(faces)))
    for triangle in faces:
        out += struct.pack("<3f", 0.0, 0.0, 1.0)
        for corner in triangle:
            out += struct.pack("<3f", *corner)
        out += b"\0\0"
    return bytes(out)


def box_faces(size_x: float = 0.1, size_y: float | None = None, size_z: float | None = None):
    """长方体的 12 个三角面（默认立方体）。"""

    size_y = size_x if size_y is None else size_y
    size_z = size_x if size_z is None else size_z
    half = (size_x / 2, size_y / 2, size_z / 2)
    corners = [
        (-half[0], -half[1], -half[2]),
        (half[0], -half[1], -half[2]),
        (half[0], half[1], -half[2]),
        (-half[0], half[1], -half[2]),
        (-half[0], -half[1], half[2]),
        (half[0], -half[1], half[2]),
        (half[0], half[1], half[2]),
        (-half[0], half[1], half[2]),
    ]
    faces = [
        (0, 3, 2),
        (0, 2, 1),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (1, 2, 6),
        (1, 6, 5),
        (2, 3, 7),
        (2, 7, 6),
        (3, 0, 4),
        (3, 4, 7),
    ]
    return [tuple(corners[index] for index in face) for face in faces]


def cube_faces(size: float = 0.1):
    return box_faces(size, size, size)


def open_faces() -> list:
    """过原点的一个三角面：有面积、有向体积为 0（不是封闭实体）。"""

    return [((0.0, 0.0, 0.0), (0.1, 0.0, 0.0), (0.0, 0.1, 0.0))]


def degenerate_faces() -> list:
    return [*cube_faces(), ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))]


def link(
    name: str,
    *,
    mesh: str | None = "../meshes/part.stl",
    mass: str | None = "0.5",
    com: str = "0 0 0",
    inertia: dict | None = None,
    collision: bool = True,
    scale: str | None = None,
) -> str:
    inertia = inertia or INERTIA
    tensor = "".join(f'{key}="{value}" ' for key, value in inertia.items())
    text = f'  <link name="{name}">\n'
    if mass is not None:
        text += (
            f'    <inertial>\n      <origin xyz="{com}" rpy="0 0 0"/>\n'
            f'      <mass value="{mass}"/>\n      <inertia {tensor.rstrip()}/>\n    </inertial>\n'
        )
    if mesh:
        scaling = f' scale="{scale}"' if scale else ""
        text += (
            f"    <visual>\n      <geometry>\n"
            f'        <mesh filename="{mesh}"{scaling}/>\n      </geometry>\n    </visual>\n'
        )
        if collision:
            text += (
                f"    <collision>\n      <geometry>\n"
                f'        <mesh filename="{mesh}"{scaling}/>\n      </geometry>\n    </collision>\n'
            )
    return text + "  </link>\n"


def joint(
    name: str,
    kind: str = "revolute",
    *,
    parent: str = "base_link",
    child: str = "arm_link",
    axis: str | None = "0 0 1",
    limits: str | None = "-1 1 10 10",
    origin: str = "0 0 0.1",
) -> str:
    text = f'  <joint name="{name}" type="{kind}">\n'
    text += f'    <parent link="{parent}"/>\n    <child link="{child}"/>\n'
    text += f'    <origin xyz="{origin}" rpy="0 0 0"/>\n'
    if axis is not None:
        text += f'    <axis xyz="{axis}"/>\n'
    if limits is not None:
        lower, upper, effort, velocity = limits.split()
        text += f'    <limit lower="{lower}" upper="{upper}" effort="{effort}" velocity="{velocity}"/>\n'
    return text + "  </joint>\n"


def urdf(*, links: str | None = None, joints: str | None = None, name: str = "robot") -> str:
    links = links if links is not None else (link("base_link") + link("arm_link", mass="0.2"))
    joints = joints if joints is not None else joint("base_arm_joint")
    return f'<?xml version="1.0"?>\n<robot name="{name}">\n{links}{joints}</robot>\n'


class AuditCase:
    """临时模型目录，可覆盖 URDF / MJCF / 台账 / 例外。"""

    def __init__(
        self,
        root: Path,
        urdf_text: str,
        *,
        mjcf: str | None = None,
        joint_names: list[str] | None = None,
        waivers: dict | None = None,
        contract: str = "candidate",
        meshes: dict[str, bytes] | None = None,
    ):
        (root / "urdf").mkdir(parents=True)
        (root / "meshes").mkdir()
        (root / "config").mkdir()
        (root / "urdf" / "robot.urdf").write_text(urdf_text, encoding="utf-8")
        for name, payload in (meshes or {"part.stl": cube_stl()}).items():
            (root / "meshes" / name).write_bytes(payload)
        names = joint_names if joint_names is not None else ["base_arm_joint"]
        ledger = "# 台账\nstructural_joint_names:\n" + "".join(f"  - {item}\n" for item in names)
        (root / "config" / "joint_names.yaml").write_text(ledger, encoding="utf-8")
        (root / "config" / "model_contract.json").write_text(
            json.dumps({"model_id": "robot", "state": contract}), encoding="utf-8"
        )
        (root / "config" / "urdf_quality.json").write_text(
            json.dumps(waivers or {"waivers": [], "massless_links": []}), encoding="utf-8"
        )
        if mjcf is not None:
            (root / "mjcf").mkdir()
            (root / "mjcf" / "robot.xml").write_text(mjcf, encoding="utf-8")


def run(root: Path, *args: str) -> tuple[int, dict]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.main(["--root", str(root), "--json", *args])
    payload = buffer.getvalue()
    return code, (json.loads(payload) if payload.strip() else {})


MUJOCO_AVAILABLE = True
try:  # 编译层用例可选
    import mujoco  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    MUJOCO_AVAILABLE = False


def codes(report: dict) -> set[str]:
    return {finding["code"] for finding in report.get("findings", [])}


# MuJoCo 的 mesh 资源名默认是去掉扩展名的词干（part.stl → part）
MJCF = """<mujoco model="robot">
  <compiler meshdir="../meshes"/>
  <asset><mesh file="part.stl"/></asset>
  <worldbody>
    <body name="base_link"><inertial pos="0 0 0" mass="0.5"
      diaginertia="0.000833333 0.000833333 0.000833333"/>
      <geom type="mesh" mesh="part"/>
      <body name="arm_link"><inertial pos="0 0 0.1" mass="0.2"
        diaginertia="0.000833333 0.000833333 0.000833333"/>
        <joint name="base_arm_joint" type="hinge" axis="0 0 1" range="-1 1"/>
        <geom type="mesh" mesh="part"/>
      </body>
    </body>
  </worldbody>
</mujoco>
"""


# 与 correct_urdf() 逐项一致的 MJCF：位姿写在 body 上（上面那份 MJCF 故意把 0.1 m 写在
# inertial 上，是反例夹具），两侧质量与惯量取 0.5 kg 立方体的均匀密度值。
CORRECT_MJCF = """<mujoco model="robot">
  <compiler meshdir="../meshes"/>
  <asset><mesh file="part.stl"/></asset>
  <worldbody>
    <body name="base_link"><inertial pos="0 0 0" mass="0.5"
      diaginertia="0.000833333 0.000833333 0.000833333"/>
      <geom type="mesh" mesh="part"/>
      <body name="arm_link" {body}>
        <joint name="base_arm_joint" type="hinge" axis="0 0 1" range="-1 1"/>
        <inertial pos="0 0 0" mass="0.5"
          diaginertia="0.000833333 0.000833333 0.000833333"/>
        <geom type="mesh" mesh="part"/>
      </body>
    </body>
  </worldbody>
</mujoco>
"""


def correct_urdf(*, rpy: str = "0 0 0") -> str:
    """两侧同质量的基准 URDF；关节原点姿态可指定，用来比对 MJCF 的 body quat。"""

    text = urdf(links=link("base_link") + link("arm_link"), joints=joint("base_arm_joint"))
    return text.replace('xyz="0 0 0.1" rpy="0 0 0"', f'xyz="0 0 0.1" rpy="{rpy}"', 1)


class SeededDefectTests(unittest.TestCase):
    """每条规则一个反例：编号必须出现在报告里。"""

    def check(self, expectation: str, urdf_text: str, *, args=(), **kwargs) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            AuditCase(root, urdf_text, **kwargs)
            _code, report = run(root, "--policy", "advisory", *args)
            self.assertIn(expectation, codes(report), report.get("findings"))

    def test_structure_rules(self):
        self.check("URDF101", urdf(name=""))
        self.check("URDF102", '<?xml version="1.0"?><robot name="robot"></robot>\n')
        self.check("URDF103", urdf(links=link("base_link") + link("base_link")))
        self.check("URDF104", urdf(joints=joint("base_arm_joint") + joint("base_arm_joint")))
        self.check("URDF105", urdf(joints=joint("base_arm_joint", child="missing_link")))
        self.check(
            "URDF106",
            urdf(links=link("base_link") + link("arm_link") + link("other_link"), joints=joint("base_arm_joint")),
        )
        self.check(
            "URDF107",
            urdf(
                links=link("base_link") + link("arm_link") + link("cycle_link"),
                joints=(
                    joint("base_arm_joint")
                    + joint("arm_cycle_joint", child="cycle_link")
                    + joint("cycle_arm_joint", parent="cycle_link", child="arm_link")
                ),
            ),
        )
        self.check("URDF108", urdf(joints=joint("base_arm_joint", "screw")))
        self.check("URDF109", urdf(joints=joint("base_arm_joint", "floating")))
        self.check("URDF110", urdf(links=link("base_link") + link("arm_link"), joints=joint("base_arm_joint", "fixed")))

    def test_joint_rules(self):
        self.check("URDF201", urdf(joints=joint("base_arm_joint", limits=None)))
        self.check("URDF202", urdf(joints=joint("base_arm_joint", limits="1 -1 10 10")))
        self.check("URDF203", urdf(joints=joint("base_arm_joint", limits="-7 7 10 10")))
        self.check("URDF204", urdf(joints=joint("base_arm_joint", "continuous")))
        self.check("URDF205", urdf(joints=joint("base_arm_joint", axis=None)))
        self.check("URDF206", urdf(joints=joint("base_arm_joint", axis="0 0 2")))
        self.check("URDF207", urdf(joints=joint("base_arm_joint", origin="0 0 40")))
        self.check("URDF208", urdf(), joint_names=[])
        self.check("URDF208", urdf(), joint_names=["base_arm_joint", "ghost_joint"])

    def test_inertia_rules(self):
        self.check("URDF301", urdf(links=link("base_link", mass=None) + link("arm_link")))
        self.check("URDF302", urdf(links=link("base_link") + link("arm_link", mass="0")))
        self.check("URDF303", urdf(links=link("base_link") + link("arm_link", mass="nan")))
        self.check("URDF304", urdf(links=link("base_link") + link("arm_link", inertia={**INERTIA, "izz": "0"})))
        self.check(
            "URDF305",
            urdf(
                links=link("base_link")
                + link("arm_link", inertia={**INERTIA, "ixx": "0.005", "iyy": "0.005", "izz": "0.005"})
            ),
        )
        self.check(
            "URDF306",
            urdf(
                links=link("base_link")
                + link("arm_link", inertia={**INERTIA, "ixx": "0.003", "iyy": "0.003", "izz": "0.003"})
            ),
        )
        self.check("URDF307", urdf(links=link("base_link") + link("arm_link", com="0 0 1")))
        self.check("URDF308", urdf(links=link("base_link") + link("arm_link", mass="50")))
        self.check("URDF309", urdf(links=link("base_link") + link("arm_link", mesh=None)))

    def test_inertia_oracle_rules(self):
        """箱体的解析惯量应当通过；×3 时报 URDF310，主轴换序时报 URDF311。"""

        exact = {"ixx": "5.20833e-04", "ixy": "0", "ixz": "0", "iyy": "1.77083e-03", "iyz": "0", "izz": "2.08333e-03"}
        box = {"part.stl": stl_bytes(box_faces(0.2, 0.1, 0.05))}
        text = urdf(links=link("base_link", inertia=exact) + link("arm_link", inertia=exact))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            AuditCase(root, text, meshes=box)
            _code, report = run(root, "--policy", "advisory")
            self.assertNotIn("URDF310", codes(report), report.get("findings"))
            self.assertNotIn("URDF311", codes(report), report.get("findings"))
        scaled = {key: str(float(value) * 3) for key, value in exact.items()}
        self.check(
            "URDF310", urdf(links=link("base_link", inertia=exact) + link("arm_link", inertia=scaled)), meshes=box
        )
        swapped = {**exact, "ixx": exact["izz"], "izz": exact["ixx"]}
        self.check(
            "URDF311", urdf(links=link("base_link", inertia=exact) + link("arm_link", inertia=swapped)), meshes=box
        )

    def test_geometry_rules(self):
        self.check("URDF401", urdf(links=link("base_link") + link("arm_link", mesh="package://part.stl")))
        self.check("URDF402", urdf(links=link("base_link") + link("arm_link", mesh="missing.stl")))
        self.check(
            "URDF403",
            urdf(links=link("base_link") + link("arm_link")),
            meshes={"part.stl": b"solid broken\nfacet normal 0 0 1\n"},
        )
        self.check("URDF404", urdf(links=link("base_link") + link("arm_link")), meshes={"part.stl": cube_stl(20.0)})
        self.check("URDF405", urdf(links=link("base_link") + link("arm_link", scale="2 2 2")))
        self.check(
            "URDF406",
            urdf(links=link("base_link") + link("arm_link")),
            meshes={"part.stl": stl_bytes(degenerate_faces())},
        )
        self.check("URDF407", urdf(links=link("base_link") + link("arm_link", collision=False)))
        self.check(
            "URDF408",
            urdf(links=link("base_link", mesh="../meshes/one.stl") + link("arm_link", mesh="../meshes/two.stl")),
            meshes={"one.stl": cube_stl(), "two.stl": cube_stl()},
        )
        self.check(
            "URDF409", urdf(links=link("base_link") + link("arm_link")), meshes={"part.stl": stl_bytes(open_faces())}
        )
        self.check(
            "URDF410", urdf(links=link("base_link") + link("arm_link")), meshes={"part.stl": stl_bytes(open_faces())}
        )
        inverted = [tuple(reversed(face)) for face in cube_faces()]
        self.check(
            "URDF411", urdf(links=link("base_link") + link("arm_link")), meshes={"part.stl": stl_bytes(inverted)}
        )

    def test_compiled_layer_without_mujoco_reports_info(self):
        """--mujoco 但没有 MJCF（或没装 mujoco）时必须明确说明跳过，而不是静默。"""

        self.check("URDF510", urdf(), args=("--mujoco",))

    def test_mjcf_rules(self):
        for meshdir in ("/tmp", "\\\\meshes", "C:/meshes", "//server/share"):
            with self.subTest(meshdir=meshdir):
                self.check("URDF502", urdf(), mjcf=MJCF.replace('meshdir="../meshes"', f'meshdir="{meshdir}"'))
        self.check("URDF503", urdf(), mjcf=MJCF.replace('file="part.stl"', 'file="ghost.stl"'))
        self.check("URDF504", urdf(), mjcf=MJCF.replace('name="arm_link"', 'name="ghost_link"'))
        self.check("URDF505", urdf(), mjcf=MJCF.replace('mass="0.2"', 'mass="0.9"'))
        self.check("URDF506", urdf(), mjcf=MJCF.replace('range="-1 1"', 'range="-0.5 0.5"'))

    def test_mirror_rules(self):
        pairs = (
            link("left_arm_link") + link("right_arm_link", mass="1.0") + link("left_leg_link") + link("right_leg_link")
        )
        joints = (
            joint("left_shoulder_joint", child="left_arm_link", parent="base_link")
            + joint("right_shoulder_joint", child="right_arm_link", parent="base_link")
            + joint("left_hip_joint", child="left_leg_link", parent="base_link")
            + joint("right_hip_joint", child="right_leg_link", parent="base_link")
        )
        text = urdf(links=link("base_link") + pairs, joints=joints)
        self.check(
            "URDF601",
            text,
            joint_names=["left_shoulder_joint", "right_shoulder_joint", "left_hip_joint", "right_hip_joint"],
        )
        inertia = {**INERTIA, "izz": "0.002"}
        bumped = text.replace(link("right_leg_link"), link("right_leg_link", inertia=inertia), 1)
        self.check(
            "URDF602",
            bumped,
            joint_names=["left_shoulder_joint", "right_shoulder_joint", "left_hip_joint", "right_hip_joint"],
        )
        shaped = text.replace('lower="-1" upper="1"', 'lower="-1" upper="1.5"', 1)
        self.check(
            "URDF603",
            shaped,
            joint_names=["left_shoulder_joint", "right_shoulder_joint", "left_hip_joint", "right_hip_joint"],
        )

    def test_waiver_rules(self):
        waivers = {
            "waivers": [{"code": "URDF999", "reason": "x", "owner": "y", "date": "2026-01-01"}],
            "massless_links": [],
        }
        self.check("URDF701", urdf(), waivers={"waivers": "nope"})
        self.check("URDF702", urdf(), waivers=waivers)
        self.check(
            "URDF704",
            urdf(links=link("base_link", mass=None) + link("arm_link")),
            waivers={
                "waivers": [{"code": "URDF301", "reason": "x", "owner": "y", "date": "2026-01-01"}],
                "massless_links": [],
            },
        )
        warning = urdf(links=link("base_link") + link("arm_link", mesh=None))
        waivers = {
            "waivers": [
                {
                    "code": "URDF309",
                    "subject": "arm_link",
                    "reason": "x",
                    "owner": "y",
                    "date": "2026-01-01",
                    "review_after": "2026-01-31",
                }
            ],
            "massless_links": [],
        }
        self.check("URDF705", warning, waivers=waivers)

    def test_an_exception_for_a_rule_the_run_skips_is_not_dead(self):
        """开关关掉的规则没有跑，为它写的例外就不是死例外。"""

        skipped = {
            "waivers": [
                {
                    "code": "URDF507",
                    "reason": "the compiled layer is not requested in this run",
                    "owner": "probe",
                    "date": "2026-01-01",
                }
            ],
            "massless_links": [],
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            AuditCase(root, correct_urdf(), mjcf=CORRECT_MJCF.format(body='pos="0 0 0.1"'), waivers=skipped)
            code, report = run(root, "--policy", "advisory")
            self.assertIn(code, (0, 1), report)
            self.assertNotIn("URDF702", codes(report), report.get("findings"))

        evaluated = {
            "waivers": [
                {
                    "code": "URDF407",
                    "reason": "collisions are present, so this one really is dead",
                    "owner": "probe",
                    "date": "2026-01-01",
                }
            ],
            "massless_links": [],
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            AuditCase(root, correct_urdf(), mjcf=CORRECT_MJCF.format(body='pos="0 0 0.1"'), waivers=evaluated)
            code, report = run(root, "--policy", "advisory")
            self.assertIn(code, (0, 1), report)
            self.assertIn("URDF702", codes(report), report.get("findings"))

    def test_every_rule_has_a_seeded_defect(self):
        """规则表与反例表必须一一对应，新增规则却忘了写反例就会失败。"""

        import re

        source = (ROOT / "src/description_pipeline/verification/urdf_quality/rules.py").read_text(encoding="utf-8")
        declared = set(re.findall(r'"(URDF\d{3})"', source))
        declared |= {"URDF701", "URDF702", "URDF704", "URDF705"}  # 台账/例外组在 waivers.py
        covered = {
            "URDF101",
            "URDF102",
            "URDF103",
            "URDF104",
            "URDF105",
            "URDF106",
            "URDF107",
            "URDF108",
            "URDF109",
            "URDF110",
            "URDF201",
            "URDF202",
            "URDF203",
            "URDF204",
            "URDF205",
            "URDF206",
            "URDF207",
            "URDF208",
            "URDF301",
            "URDF302",
            "URDF303",
            "URDF304",
            "URDF305",
            "URDF306",
            "URDF307",
            "URDF308",
            "URDF309",
            "URDF401",
            "URDF402",
            "URDF404",
            "URDF405",
            "URDF406",
            "URDF407",
            "URDF409",
            "URDF502",
            "URDF403",
            "URDF408",
            "URDF310",
            "URDF311",
            "URDF410",
            "URDF411",
            "URDF507",
            "URDF508",
            "URDF509",
            "URDF510",
            "URDF511",
            "URDF512",
            "URDF503",
            "URDF504",
            "URDF505",
            "URDF506",
            "URDF601",
            "URDF602",
            "URDF603",
            "URDF701",
            "URDF702",
            "URDF704",
            "URDF705",
        }
        self.assertEqual(sorted(declared - covered), [], "这些规则没有反例")

    def test_every_rule_is_documented(self):
        """规则也必须写进 docs/urdf_standard.md，避免文档与实现漂移。"""

        import re

        source = "\n".join(
            (ROOT / "src/description_pipeline/verification/urdf_quality" / name).read_text(encoding="utf-8")
            for name in ("rules.py", "waivers.py", "cli.py")
        )
        declared = set(re.findall(r'"(URDF\d{3})"', source))
        document = (ROOT / "docs" / "urdf_standard.md").read_text(encoding="utf-8")
        missing = sorted(code for code in declared if code not in document)
        self.assertEqual(missing, [], "这些规则没有写进 docs/urdf_standard.md")

    def test_every_cli_flag_is_documented(self):
        """命令行参数同样要写进文档（与 onshape 工具的文档漂移检查一致）。"""

        import argparse

        from urdf_quality import cli as audit_cli

        flags: set[str] = set()

        def collect(parser):
            for action in parser._actions:  # noqa: SLF001 - argparse 无公开遍历接口
                flags.update(action.option_strings)
                if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                    for sub in action.choices.values():
                        collect(sub)

        collect(audit_cli._parser())  # noqa: SLF001 - 直接拿真实解析器
        flags -= {"-h", "--help"}
        document = (ROOT / "docs" / "urdf_standard.md").read_text(encoding="utf-8")
        missing = sorted(flag for flag in flags if flag not in document)
        self.assertEqual(missing, [], "这些参数没有写进 docs/urdf_standard.md")


@unittest.skipUnless(MUJOCO_AVAILABLE, "未安装 mujoco")
class CompiledLayerTests(unittest.TestCase):
    """--mujoco：编译后的质量/质心/惯量与 URDF 对照。"""

    def _case(self, root: Path, mjcf: str) -> None:
        AuditCase(root, urdf(), mjcf=mjcf)

    def test_compiled_mass_com_inertia_mismatch(self):
        mjcf = MJCF.replace('mass="0.2"', 'mass="0.9"', 1)
        mjcf = mjcf.replace('pos="0 0 0.1"', 'pos="0 0 0.12"', 1)
        mjcf = mjcf.replace(
            'diaginertia="0.000833333 0.000833333 0.000833333"', 'diaginertia="0.00833333 0.00833333 0.00833333"', 1
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._case(root, mjcf)
            _code, report = run(root, "--policy", "advisory", "--mujoco")
            found = codes(report)
            self.assertIn("URDF507", found, report.get("findings"))
            self.assertIn("URDF508", found, report.get("findings"))
            self.assertIn("URDF509", found, report.get("findings"))
            self.assertGreaterEqual(report["model"]["compiled_bodies_compared"], 2)
            self.assertIn("URDF512", found, report.get("findings"))
            self.assertEqual(len(report["model"]["self_contact_poses"]), 8)

    def test_compiled_forward_kinematics_mismatch(self):
        """关节原点在 URDF 与 MJCF 之间不一致时，FK 比对必须抓到。"""

        mjcf = MJCF.replace('pos="0 0 0.1"', 'pos="0 0 0.12"', 1)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._case(root, mjcf)
            _code, report = run(root, "--policy", "advisory", "--mujoco")
            self.assertIn("URDF511", codes(report), report.get("findings"))
            self.assertGreater(report["model"]["fk_max_error_m"], 1e-4)

    def test_correct_model_has_no_compiled_findings(self):
        """一致的 URDF/MJCF 必须过门禁：hinge 关节被漏检时这里会误报 URDF511。"""

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            AuditCase(root, correct_urdf(), mjcf=CORRECT_MJCF.format(body='pos="0 0 0.1"'))
            code, report = run(root, "--policy", "strict", "--mujoco")
            self.assertEqual(code, 0, report.get("findings"))
            self.assertEqual(report["summary"]["error"], 0)
            self.assertEqual(report["summary"]["warning"], 0)
            self.assertEqual(report["model"]["compiled_bodies_compared"], 2)

    def test_rotation_mismatch_is_caught(self):
        """位置相同、姿态不一致（轴或原点方向错）时，只能由姿态比对发现。"""

        # MJCF 侧绕 x 轴多转 1°：body 位置不变，姿态差 0.0245（旋转矩阵差的 Frobenius 范数）。
        mjcf = CORRECT_MJCF.format(body='pos="0 0 0.1" quat="0.9999619 0.0087265 0 0"')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            AuditCase(root, correct_urdf(), mjcf=mjcf)
            _code, report = run(root, "--policy", "advisory", "--mujoco")
            self.assertIn("URDF511", codes(report), report.get("findings"))
            self.assertLessEqual(report["model"]["fk_max_error_m"], 1e-4)

    def test_decimal_rounding_does_not_trip_the_rotation_check(self):
        """URDF 存 rpy、MJCF 存 quat，各自约 6 位有效数字：这点舍入不得当作不一致。"""

        # rpy=1.5708（≈π/2）与 quat=(0.707107, 0.707107)：差 3.7e-6 rad，位置差为 0。
        mjcf = CORRECT_MJCF.format(body='pos="0 0 0.1" quat="0.707107 0.707107 0 0"')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            AuditCase(root, correct_urdf(rpy="1.5708 0 0"), mjcf=mjcf)
            _code, report = run(root, "--policy", "advisory", "--mujoco")
            self.assertNotIn("URDF511", codes(report), report.get("findings"))


class RepositoryModelTests(unittest.TestCase):
    @onshape_fixture.requires_fixture
    def test_microban_fixture_passes_strict(self):
        code, report = run(MODEL)
        self.assertEqual(code, 0, report.get("findings"))
        self.assertEqual(report["summary"]["error"], 0)
        self.assertEqual(report["summary"]["warning"], 0)
        self.assertEqual(report["model"]["links"], 24)

    @unittest.skipUnless(MUJOCO_AVAILABLE, "未安装 mujoco")
    @onshape_fixture.requires_fixture
    def test_microban_fixture_passes_strict_with_mujoco(self):
        """仓库内的真实模型夹具也要过含编译层的严格门禁（URDF507–512）。"""

        code, report = run(MODEL, "--policy", "strict", "--mujoco")
        self.assertEqual(code, 0, report.get("findings"))
        self.assertEqual(report["summary"]["error"], 0)
        self.assertEqual(report["summary"]["warning"], 0)
        self.assertEqual(report["model"]["compiled_bodies_compared"], 20)
        self.assertLess(report["model"]["fk_max_error_m"], 1e-4)

    def test_report_has_no_absolute_paths(self):
        _code, report = run(MODEL)
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("/home/", text)
        self.assertNotIn(str(MODEL), text)

    def test_strict_and_advisory_policy_differ_on_warnings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            AuditCase(root, urdf(links=link("base_link") + link("arm_link", mesh=None)))
            strict_code, strict = run(root, "--policy", "strict")
            advisory_code, _advisory = run(root, "--policy", "advisory")
            self.assertEqual(strict["summary"]["warning"], 1)
            self.assertEqual(strict_code, 1)
            self.assertEqual(advisory_code, 0)

    def test_waiver_makes_strict_pass_and_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            AuditCase(
                root,
                urdf(links=link("base_link") + link("arm_link", mesh=None)),
                waivers={
                    "waivers": [
                        {
                            "code": "URDF309",
                            "subject": "arm_link",
                            "reason": "纯坐标系 link",
                            "owner": "andy",
                            "date": "2026-09-18",
                        }
                    ],
                    "massless_links": [],
                },
            )
            code, report = run(root)
            self.assertEqual(code, 0, report.get("findings"))
            self.assertEqual(report["summary"]["waived"], 1)
            self.assertEqual(report["waived"][0]["code"], "URDF309")


class ReportGateTests(unittest.TestCase):
    """发布门禁：已提交的报告必须存在、通过、且与当前 URDF 字节一致。"""

    def _model(self, root: Path) -> Path:
        # 两个 link 都用与立方体网格一致的 0.5 kg / 立方体惯量，保证 strict 下无 warning
        AuditCase(root, urdf(links=link("base_link") + link("arm_link")))
        return root

    def _gate(self, root: Path, report: Path) -> tuple[int, dict]:
        return run(root, "--verify-report", str(report))

    def test_valid_report_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self._model(Path(folder))
            report = root / "docs" / "urdf_audit.json"
            code, _ = run(root, "--report", str(report))
            self.assertEqual(code, 0)
            gate, payload = self._gate(root, report)
            self.assertEqual(gate, 0, payload)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["command"], "verify-report")

    def test_missing_report_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self._model(Path(folder))
            code, payload = self._gate(root, root / "docs" / "urdf_audit.json")
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertIn("缺少已提交的质检报告", payload["reason"])

    def test_failed_report_fails_the_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self._model(Path(folder))
            report = root / "docs" / "urdf_audit.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                json.dumps({"passed": False, "summary": {"error": 3}, "model": {"urdf_sha256": "0" * 64}}),
                encoding="utf-8",
            )
            gate, payload = self._gate(root, report)
            self.assertEqual(gate, 1)
            self.assertFalse(payload["ok"])
            self.assertIn("未通过", payload["reason"])

    def test_stale_report_hash_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self._model(Path(folder))
            report = root / "docs" / "urdf_audit.json"
            code, _ = run(root, "--report", str(report))
            self.assertEqual(code, 0)
            (root / "urdf" / "robot.urdf").write_text(urdf(name="changed"), encoding="utf-8")
            gate, payload = self._gate(root, report)
            self.assertEqual(gate, 1)
            self.assertIn("不一致", payload["reason"])

    def test_broken_report_json_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self._model(Path(folder))
            report = root / "docs" / "urdf_audit.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("{not json", encoding="utf-8")
            gate, payload = self._gate(root, report)
            self.assertEqual(gate, 1)
            self.assertIn("不是合法 JSON", payload["reason"])


if __name__ == "__main__":
    unittest.main()
