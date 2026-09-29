"""Microban 导出模型的动力学 sanity 用例（需要 mujoco，否则整体跳过）。

零位姿态指 URDF/MJCF 的 ``qpos=0``（CAD 零位），不是官方控制器里的 ``NEUTRAL_POSE``。
用例锁定三件事：零位没有自接触、零位静载力矩在 XL330 能力内、落地静置不发散。

台架参数（测试约定，不是官方控制参数）：``kp=50`` N·m/rad、``kd=2.2`` N·m·s/rad，
力矩上限 ``0.6405`` N·m = XL330 固件电流上限 1.75 A × 0.366 N·m/A
（``Rhoban/microban src/constants.py``，官方 RL 档增益只有 ~0.277 N·m/rad）。
"""

import unittest
from pathlib import Path
from typing import ClassVar
from tests import onshape_fixture  # noqa: E402

try:  # MuJoCo（及其依赖 numpy）是可选依赖：没装就整体跳过
    import mujoco
    import numpy as np

    MUJOCO_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - 取决于环境
    MUJOCO_AVAILABLE = False

MODEL = Path(__file__).resolve().parent / "fixtures" / "onshape" / "model"
ROBOT_XML = MODEL / "mjcf" / "robot.xml"
SCENE_XML = MODEL / "mjcf" / "scene.xml"

KP = 50.0  # N·m/rad
KD = 2.2  # N·m·s/rad，临界阻尼附近
TORQUE_LIMIT = 0.6405  # N·m，XL330 力矩上限
DROP_HEIGHT = 0.005  # m，初始离地间隙
FEET = ("foot", "foot__1")


def _lowest_collision_point(model, data) -> float:
    """零位姿态下最低的碰撞点高度（相对刚体树原点）。"""

    lowest = float("inf")
    for geom in range(model.ngeom):
        if model.geom_bodyid[geom] == 0:  # 世界/地面
            continue
        if model.geom_contype[geom] == 0 and model.geom_conaffinity[geom] == 0:
            continue
        if model.geom_type[geom] == mujoco.mjtGeom.mjGEOM_MESH:
            data_id = model.geom_dataid[geom]
            first, count = model.mesh_vertadr[data_id], model.mesh_vertnum[data_id]
            rotation = data.geom_xmat[geom].reshape(3, 3)
            points = model.mesh_vert[first : first + count] @ rotation.T
            lowest = min(lowest, float((points + data.geom_xpos[geom])[:, 2].min()))
        else:
            lowest = min(lowest, float(data.geom_xpos[geom][2] - model.geom_rbound[geom]))
    return lowest


def _zero_pose(model):
    """零位姿态、脚底离地 ``DROP_HEIGHT`` 的数据对象。"""

    data = mujoco.MjData(model)
    data.qpos[3] = 1.0  # freejoint 四元数
    mujoco.mj_forward(model, data)
    data.qpos[2] = -_lowest_collision_point(model, data) + DROP_HEIGHT
    mujoco.mj_forward(model, data)
    return data


def _stance(model, *, seconds: float = 1.0, torque: bool = True):
    """零位落地后保持（或完全不加力矩）若干秒，逐步记录最大关节速度。"""

    data = _zero_pose(model)
    peak = 0.0
    for _ in range(round(seconds / model.opt.timestep)):
        data.ctrl[:] = (
            np.clip(KP * (0.0 - data.qpos[7:]) - KD * data.qvel[6:], -TORQUE_LIMIT, TORQUE_LIMIT) if torque else 0.0
        )
        mujoco.mj_step(model, data)
        peak = max(peak, float(np.abs(data.qvel).max()))
    return data, peak


def _bodies_in_contact(model, data) -> set[str]:
    names = set()
    for index in range(data.ncon):
        contact = data.contact[index]
        names.add(mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, model.geom_bodyid[contact.geom1]))
        names.add(mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, model.geom_bodyid[contact.geom2]))
    return names - {None, "world"}


@unittest.skipUnless(MUJOCO_AVAILABLE, "未安装 mujoco")
@onshape_fixture.requires_fixture
class MicrobanDynamicsTests(unittest.TestCase):
    robot: ClassVar[mujoco.MjModel]
    scene: ClassVar[mujoco.MjModel]

    @classmethod
    def setUpClass(cls):
        cls.robot = mujoco.MjModel.from_xml_path(str(ROBOT_XML))
        cls.scene = mujoco.MjModel.from_xml_path(str(SCENE_XML))

    def test_zero_pose_has_no_self_contacts(self):
        """接触排除必须真的生效：不带地面时零位接触点应为 0。"""

        data = mujoco.MjData(self.robot)
        mujoco.mj_forward(self.robot, data)
        self.assertEqual(data.ncon, 0, _bodies_in_contact(self.robot, data))

    def test_zero_pose_static_torque_is_within_the_actuator_limit(self):
        """零位静载力矩：重力矩必须落在 XL330 力矩上限内，且确实有载荷。"""

        data = mujoco.MjData(self.robot)
        data.qpos[3] = 1.0
        data.qpos[2] = 0.5  # 悬空，排除地面反力
        mujoco.mj_forward(self.robot, data)
        torques = {
            mujoco.mj_id2name(self.robot, mujoco.mjtObj.mjOBJ_JOINT, joint): float(
                data.qfrc_bias[self.robot.jnt_dofadr[joint]]
            )
            for joint in range(1, self.robot.njnt)
        }
        peak = max(abs(value) for value in torques.values())
        heaviest = max(torques, key=lambda name: abs(torques[name]))
        self.assertGreater(peak, 0.05, "零位静载力矩异常偏小，模型可能没有质量")
        self.assertLessEqual(
            peak,
            TORQUE_LIMIT,
            f"{heaviest} 需要 {peak:.4f} N·m，超过 XL330 上限 {TORQUE_LIMIT} N·m",
        )

    def test_stance_lands_and_holds_without_drift(self):
        """落地静置 1 s：双脚承重、姿态与力都在预算内。"""

        data, _ = _stance(self.scene)
        self.assertTrue(np.all(np.isfinite(data.qpos)), "静置出现 NaN/Inf")
        contacts = _bodies_in_contact(self.scene, data)
        for foot in FEET:
            self.assertIn(foot, contacts, f"{foot} 没有与地面接触：{contacts}")
        self.assertAlmostEqual(float(data.qpos[2]), 0.168, delta=0.010)
        self.assertLessEqual(float(np.abs(data.qpos[0])), 0.005, "基座水平漂移超预算")
        self.assertLessEqual(float(np.abs(data.qpos[1])), 0.005, "基座水平漂移超预算")
        drift = float(np.abs(data.qpos[7:]).max())
        speeds = float(np.abs(data.qvel[6:]).max())
        torque = float(np.abs(data.qfrc_actuator[6:]).max())
        self.assertLessEqual(drift, 0.05, f"关节姿态漂移 {drift:.4f} rad 超预算")
        self.assertLessEqual(speeds, 0.05, f"静置未收敛：|qd|={speeds:.4f} rad/s")
        self.assertLessEqual(torque, TORQUE_LIMIT, f"峰值力矩 {torque:.4f} N·m 超上限")

    def test_repeat_runs_are_bit_identical(self):
        """同一模型两次运行必须逐位一致，报告才可复现。"""

        first, _ = _stance(self.scene, seconds=0.5)
        second, _ = _stance(self.scene, seconds=0.5)
        self.assertTrue(np.array_equal(first.qpos, second.qpos))
        self.assertTrue(np.array_equal(first.qvel, second.qvel))

    def test_unactuated_drop_stays_bounded(self):
        """完全不加力矩地倒下也必须收敛：没有 NaN，能量不发散。"""

        start = _zero_pose(self.scene)
        data, peak = _stance(self.scene, seconds=1.0, torque=False)
        self.assertTrue(np.all(np.isfinite(data.qpos)), "自由倒下出现 NaN/Inf")
        self.assertLessEqual(peak, 50.0, f"自由倒下出现发散：|qvel| 峰值 {peak:.2f} rad/s")
        self.assertLessEqual(float(np.abs(data.qpos[0])), 0.5, "自由倒下位移超预算")
        self.assertLess(float(data.qpos[2]), float(start.qpos[2]), "落地后高度没有下降")


if __name__ == "__main__":
    unittest.main()
