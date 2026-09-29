# SolidWorks 旧桥接器参考（历史归档）

> 以下保留旧接口、规则和当时记录，不能作为当前安装、导出或验收指南。
> 生产流程见[当前来源说明](../sources/solidworks.md)；旧原生导出和部署入口已停用。

---

> 本页记录历史桥接工具。新的生产来源采集、Windows Python 3.12 部署与统一 URDF/MJCF 构建见 [当前 SolidWorks 来源说明](../sources/solidworks.md) 和 [工程契约](../pipeline.md)。原生 COM 实现已合并到公共安装包，旧入口依赖该包。

# SolidWorks → URDF 导出工具

状态：`tool`。本页说明工具的边界、证据与验证状态，不代替模型质检。

## 职责

输入是**已打开**的 SolidWorks 装配体与一份显式导出配置；输出是 URDF 包
（`robot.urdf` + `meshes/<link>.STL` + CAD 读数证据 + `manifest.sha256`）。
本工具不做 MJCF 转换，也不做模型质检；两者在下游分支执行。

## 分层

| 层 | 位置 | 平台 |
| --- | --- | --- |
| COM 接触层 | `tools/solidworks_export/native_swapi.py` | Windows（SolidWorks 桌面会话） |
| 服务与作业 | `server.py`、`jobs.py`、`com_executor.py` | Windows |
| 导出逻辑 | `exporter.py`、`scene_build.py`、`physics.py`、`urdf.py`、`stl.py` | 任意平台 |
| 客户端 | `cli.py` | 任意平台 |

导出包默认使用自带布局（`robot.urdf` + `meshes/`）。要把结果直接放进本仓库的
`urdf/` + `meshes/`，在配置里设 `mesh.path_prefix = "../meshes/"`，导出后按目录拷贝即可；
前缀在配置校验阶段限定为相对目录（`meshes/` 或 `../meshes/`），绝对路径/URL/反斜杠直接报错。

接触层之外的逻辑只用标准库，因此 150 项回归可以在 Linux CI 上运行；真实 COM
行为由 Windows 侧 `swctl selftest` 和导出作业验证。

## 离线命令

`swctl validate-config <export_config.json>` 不连 CAD，先把配置跑一遍语义校验
（link/joint/frame 划分、轴与限位、网格前缀），失败返回稳定错误码 `invalid_config`；
`swctl verify-package <目录>` 不连 CAD，校验导出包的完整性、证据绑定与 URDF 结构。

## 坐标系 link

不写 `components`、只写 `frame_component` 的 link 是**无质量坐标系**（IMU、足底、工具 frame）：
导出成空的 `<link/>` 加一个 `fixed` 关节，不产出 mesh，也不声称任何质量/惯量；
证据文件用 `kind: "frame"` 标注，`verify-package` 会检查它没有 inertial 块。
有实体的 link 仍然必须有 `components`。

## 证据与门禁

1. **只读**：不保存、不覆盖、不改名源 CAD；只附着已运行的可见 SolidWorks 实例。
2. **显式配置**：link 划分、关节轴、限位、动力学来自 `export_config.json`；
   缺失即报错，不从 mates 推断，不设默认轴。
3. **材料来源**：每个 solid 必须有零件级或实体级物理材料；默认密度不算证据。
   缺失时报 `cad_material_provenance_missing`，导出中止且不产出半成品。
4. **证据可复核**：`cad_evidence.json` 保存逐组件的 CAD 原始读数（体积、质量、
   质心、完整惯量张量、材料）与合并结果；`native_source.json` 记录来源类别
   （`real_cad` / `synthetic`）、环境与 COM 接触点；`manifest.sha256` 覆盖包内全部文件。
5. **失败不静默**：每个作业返回稳定错误码与退出码，日志带时间戳，包不完整即失败。

## 已验证范围

| 项 | 结果 |
| --- | --- |
| 单元回归（无 Windows） | 150 项通过：变换/单位、惯量合并与物理条件、URDF 序列化、配置校验、STL 校验与合并、作业队列与崩溃恢复、HTTP 协议与错误码、只读路径策略、材料门禁、示例配置 |
| 实机接触层 | SolidWorks 2026 SP02.1 上完成 `selftest` 与真实装配导出（双长方体标准件、嵌套装配、命名坐标系、五个 CAD 运动姿态） |
| 失败即阻断 | 材料缺失、实体覆盖漏项、配置错配、质量覆盖、密度与体积不一致等负例均在导出前中止 |

接触点 API 名称记录在 `native_source.json` 的 `com_contacts`，一旦在某台机器上
失败，`selftest` 会给出实际生效的 API 名称与错误详情。

## 不声称

* 不证明 CAD 选区、材料指定与真实硬件一致；
* 不检查惯量物理合理性以外的模型质量（碰撞近似、退化面、自碰撞在下游质检）；
* `evidence_class=synthetic` 的包只用于联调，不能当作 CAD 验收。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `no_pywin32` | Windows 侧缺少 pywin32 |
| `cad_member_missing` | COM 对象上没有预期的成员（SolidWorks 版本差异或 API 名发生变化） |
| `no_active_instance` | 没有可见 SolidWorks 实例 |
| `bridge_unavailable` | 客户端连不上 swbridged |
| `path_not_allowed` | 请求路径超出 `SWBRIDGE_ALLOWED_ROOTS` |
| `cad_material_provenance_missing` | 有 solid 没有物理材料 |
| `cad_mass_override` | 质量属性被手动覆盖，拒绝导出 |
| `modal_dialog_blocked` | CAD 操作超时（多半是模态弹窗未处理），检查 SolidWorks 界面 |
| `job_not_found` | 客户端引用了不存在的作业 ID |
