# R13 非零逐叶/组合运动 + 错误父图/跨组错配限定负例 — 中性夹具包（D，rev2）

**目的/边界**：为未来 `motion.{joint}` 门（当前未落地）预置**数据夹具 + 解析解**。本包
**不含 checker/验证器代码**（不新增第二验收器/第二模型；`d.motion-fixture` 仅资料）；不替代
最终验收；真实 native 见证留最终窗口。B 落地 motion/IF-14 后，D 按派单对**实现**执行复验。
**产品 motion 断言必须逐条实际执行**（见各夹具 `assertions[]`），不得以“前置失败后总 false”
计覆盖；静态绿只按各夹具 `static_green_scope` 声明的**有限不变量**计，完整静态物理须由产品门
在对应用例上实跑。

## 中性模型（多叶版；`model-correct.json` 及各 case 内嵌副本）

- 刚体链 `b0 → b1 → b2`；组 G0/G1/G2；**每组多叶**：
  G0={L0a}，G1={L1a, L1b}，G2={L2a, L2b}（L1b=L1a+0.015 m·ŷ，L2b=L2a+0.012 m·ŷ）。
- 质量：0.50 + (0.12+0.08) + (0.09+0.06) = **0.85 kg**（整机和不随姿态变化）。
- `J1`：revolute，b0→b1，轴 p₁=(0,0,0.030)、d₁=(0,1,0)；限位 **[−1.2, 0.8]**；驱动 (4.0, 3.0)。
- `J2`：revolute，b1→b2，轴 p₂=(0.010,0,−0.010)、d₂=(0,1,0)；限位 **[−0.5, 1.0]**；驱动 (2.0, 2.0)。
- 接口 `IF-J2`（b1/b2 共轴）；容差 position/rotation 1e-9、接口线距 1e-9 m、平行度 1−1e-6。
- **姿态一律先验在作者限位内**（生成器断言；上一版 case02 越界已改为 θ₂=−0.4，未放宽限位）。

## 解析推导（勘误版）

- 绕线轴旋转：`T(θ; p, d)`：`R(θ,d)` 且平移 `p − R·p`。
- **单驱 J1**：`wb1 = T₁(θ₁)·L1`；`wb2 = wb1·L1⁻¹·T₂(0)·L2 = T₁(θ₁)·T₂(0)·L2`（L1 项相消）。
  旧文档把第二式误写成 `T₁·L1⁻¹·T₂(0)·L2`——**已勘误**；夹具数值一直按链式正确计算。
- **单驱 J2**：`wb2 = T₁·L1⁻¹·T₂(θ₂)·L2`（θ₁=0 时 `wb1=T₁`）。
- **双驱**：`wb2 = T₁(θ₁)·T₂(θ₂)·L2`；逐叶 `world(leaf)=wb_group·(组零位帧⁻¹·leaf零位世界)`。
- **组内相对不变量**：同组任意两叶 `Lx⁻¹·Ly` 在任意姿态下保持不变（解析恒等；容差内断言）。
- 接口：两线距 ≤1e-9 且 |a·b| ≥ 1−1e-6（接受类实测 ≤7e-18、平行度 = 1.000000000000）。

## 用例与夹具（sha256 见 `motion-fixtures.sha256`）

| # | 夹具文件 | 姿态（全在限位内） | 期望 | 断言要点 / 建议 test id |
|---|---|---|---|---|
| 01 | `case-01-single-j1.json` | θ₁=+30° | accept | 逐叶 G1/G2 随动=绕 J1 轴解析位姿；G0 静止；两组相对不变量保持。`…MotionFixtureTests.test_case01_single_j1_follow` |
| 02 | `case-02-single-j2.json` | **θ₂=−0.4 rad（∈[−0.5,1.0]）** | accept | 仅 G2 两叶动；G0/G1 静止；组内不变量保持。`…test_case02_single_j2_follow` |
| 03 | `case-03-combined.json` | θ₁=+30°, θ₂=+45° | accept | 多叶组合：逐叶解析 + 两相对不变量 + 接口。`…test_case03_combined_pose` |
| 04 | `case-04-interface-continuity.json` | θ₁=−60°, θ₂=+30° | accept | 接口两线距 ≤1e-9、平行=1；多叶随动。`…test_case04_interface_continuity` |
| 05 | `case-05-reject-axis-sign.json` | — | reject | 存储方向反置（d₁→−d₁）。`…test_case05_wrong_axis_sign_rejected` |
| 06 | `case-06-reject-axis-offset.json` | — | reject | 轴点偏移 +2 mm。`…test_case06_axis_offset_rejected` |
| 07 | `case-07-reject-limits-order.json` | — | reject | 限位反序。`…test_case07_inverted_limits_rejected` |
| 08 | `case-08-reject-missing-drive.json` | — | reject | 驱动读数缺失（必需证据 fail-closed）。`…test_case08_missing_drive_rejected` |
| 09 | `case-09-static-green-wrong-parent.json` | θ₁=+30° | reject（静态绿/有限不变量） | 错误父图（J1: b2→b1、J2: b0→b2 + 重算原点）：`follow.J1.L2a/L2b` 候选失配 ≈ **24.4 mm**、接口 ≈ **21.3 mm**。`…test_case09_static_green_wrong_parent_rejected` |
| 10 | `case-10-reject-cross-group-leaf.json` | θ₂=+0.6 | reject（静态绿/有限不变量） | **跨组叶归宿错配**（L1b 归 G2、重算局部偏移）：`follow.static.L1b` 失配 ≈ **23.6 mm**；`intra_relative.G1` 候选 `(0.022586,0.015,−0.006987)` ≠ 记录 `(0,0.015,0)`。`…test_case10_cross_group_leaf_rejected` |

## 09/10 静态绿口径（收窄）

- 两例 `static_green_scope` 仅声明：`zero_pose_leaf_world_equal=true`、`mass_sum_equal=true`
  ——即**所建模的有限不变量**在静态下逐位一致；**不声称完整静态物理**（COM/张量/网格等须由
  产品静态门在对应用例上实际执行后判定）。
- 期望的拒绝来自**逐条 motion 断言**（`assertions[]` 已逐项给出期望结论）：逐叶 follow、
  组内相对不变量、接口连续性；测试须逐条执行，不得以组合布尔“总 false”计覆盖。

## 复算与清单

- D 复算：接受类=闭合式（`T₁·T₂·L2`）与链式互算一致（≤1e-17）；组内相对不变量按解析恒等
  核验；**全部姿态先验在作者限位内**（越界即生成失败）；接口 ≤7e-18/平行 1。
- 负例量化：case09 L2a/L2b 差 0.024417 m、接口 0.021343 m；case10 L1b 差 0.023642 m、
  G1 相对偏移 (0.022586,0.015,−0.006987)。
- 清单：`motion-fixtures.sha256`（11 个 JSON：model + 10 用例）；主清单 `../artifacts-sha256.txt`。
- 本包为数据+解析解；落地由 B 在产品测试消费（建议 id 见上表）。
