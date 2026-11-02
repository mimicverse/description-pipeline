# Robot Model Studio

将已保存的 SolidWorks 原生工程交付为可追溯、经独立验证的 URDF、MJCF 模型与评审 PR。

唯一工作流是：**工程文件夹 → Airflow → 独立验证 → URDF / MJCF 预览与 PR**。机械工程师维护原生设计和工程事实；平台生成模型、证据与报告；工程负责人批准模型的适用用途。

## 使用

1. 按[机械交付规范](docs/mechanical-handoff-spec.md)准备完整、已保存的原生工程。
2. 在操作台通过飞书登录，上传工程文件夹，选择主装配，确认发布目标，选择用途（工程或检查预览）并填写变更说明（必填，可多行）。
3. 查看七个阶段的检查结果，定位并修正源头问题；验证通过后查看实际 URDF、MJCF、网格与关节限位，进入对应 PR 评审。缺失驱动以零占位并明确警告“未建立驱动能力”；检查预览的分析窗口标注“检查/预览分析窗口，非机械限位”。

工程师不编写流水线配置，也不手工导出 URDF。完整操作、重跑与问题处理见[操作指南](docs/operations.md)。

七个阶段均可从封存输入独立重跑，默认只运行所选阶段，也可明确继续到下游终点。真实上游来源保留，旧下游成功不继承；Generate、Verify、MJCF、Publish 不重复操作 CAD。完整交付包含两种格式；转换、碰撞代理和预览见 [MJCF 集成](docs/mjcf-integration.md)。

## 系统边界

Linux 负责上传冻结、受控规格、结构推导、模型生成、独立验证、报告、预览和发布。Windows 只在自有只读会话中一次读取 CAD 原生几何、轴线、配合、物性及对象归属，随后释放 CAD。Windows 只完成原值序列化和封包；Linux Capture 核验传输与绑定，Generate 一次完成坐标与物性投影、惯量规范化和 STL 生成，Verify 从原始事实独立复算。整个流程不修改 CAD。

外观读取在同一个只读会话中保留 document/face 的无参 active-configuration 属性（仅当 active 与 referenced configuration 相同）、各 owner 自己的 configuration/display-state wrapper、同一 exact state 的 `GetRenderMaterialsCount2` 总数与 NULL 原值，以及同 owner/context 的持久引用和对象观察索引。Generate 与 Verify 分别从这些 raw 事实重建绑定；原生总数确证为 0 不等于把 NULL 改写成空数组，无法绑定或无法证明无关的实体不会被静默忽略。

part 内材质按 `face > feature > body > part document` 解析；document target 不会提升为 assembly occurrence override。真实 assembly occurrence override 仍优先于 part 内层级。

历史手工 URDF 只作为隔离的参考单元测试和回归测试 fixture，不进入生产采集、Generate、Verify 真值或发布必经链。测试只在 fixture 声明兼容的 CAD 原件、配置和修订上运行，按拓扑、几何和运动语义及有依据的容差比较，不锁定 XML／STL 字节、元素或三角形顺序、合法三角化或已批准的纯表示差异；不同版本不得硬匹配旧 URDF。

每份交付绑定输入、原生证据、工具身份、模型和实际文件。独立验证从原始证据重新建立预期值，并检查交付的 XML、网格与 MuJoCo 加载结果。发布再次核对 Git 中的字节和远端 PR。

验证通过表示[规定检查](docs/quality.md)通过。工程批准、仿真、训练与实机控制分别验收；候选 PR 不赋予这些资格，且绑定所选用途——不同用途的候选与发布恢复身份相互隔离。模型交付包含视觉网格和物理属性，MJCF 包含按版本化策略派生并独立验证的碰撞代理；其相对源组件凸包的简化误差不证明源凹表面或实物接触等价，控制接口与用途资格仍由相应工程流程负责。

## 文档

本文档集规定系统的目标行为。实现、机器契约、命令帮助和验收必须与其一致。

| 文档 | 读者与职责 |
|---|---|
| [架构设计](docs/design.md) | 系统职责、数据权威、依赖方向与执行边界 |
| [系统契约](docs/contracts.md) | 身份、阶段、主机交换、交付和发布协议 |
| [机械交付规范](docs/mechanical-handoff-spec.md) | 原生工程要求及工程责任 |
| [质量规范](docs/quality.md) | 独立验证、数值判据与资格边界 |
| [操作指南](docs/operations.md) | 提交、检查、修正、重跑与模型评审 |
| [部署指南](docs/deployment.md) | 首次安装、配置、运行与灾难恢复 |
| [开发指南](CONTRIBUTING.md) | 代码组织、测试与文档维护 |
| [工具发布](RELEASING.md) | 构建、原生验收与发行物发布 |

[Apache License 2.0](LICENSE) · [行为准则](CODE_OF_CONDUCT.md)
