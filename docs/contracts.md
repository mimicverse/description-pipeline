# 系统契约

本契约定义领域对象及跨边界行为。每种对象只有一个当前 schema，标识使用 `solidworks-to-urdf` 命名空间；未知字段、未知版本和缺失必需字段拒绝消费。发行物包含 schema、阶段定义和检查策略，三者的摘要进入执行与验证记录。

<a id="identity"></a>
## 身份与来源

身份分为工程内容、执行和发布三个层次，不能相互替代。

CAD 提供工程事实，版本化规则负责规范化、派生与有依据的修正。处理优先级固定为“读取并保留 CAD 原值 → 按适用规范和原生对象校验 → 沿用正确值或按确定规则校正 → 统一写出与独立复核”。CAD 已有名称、方向、坐标、零位和限位时，不能绕过原值直接另行生成；值正确则沿用，只有偏差有依据时才校正，确证缺失时才允许契约规定的推导。读取失败、无效或相互冲突不能假装缺失。原值、检查结论、适用规则、修正值及影响字段须可追溯，修正或推导值显示 warning。手工导出的 URDF、网格和历史结果不得作为生产输入或直接补值来源。按用户 2026-10-26 最新要求，固定手工版仅作独立参考单元测试和回归测试数据，不进入生产采集、生成、验证真值或发布必经链。测试绑定适用的 CAD 原件、配置和修订，按工程语义及有依据的容差核对；不锁死 XML 排版、元素顺序、网格字节或三角形顺序，合法表示和已获准的名称、纯轴符号差异须正确分类。参考与 CAD 或规范冲突时查明原因，不能为固定答案改写生产逻辑或倒填逐对象值。允许根据当前 CAD 的对象、配置、几何和原生语义应用明确规则；不能为匹配手工结果编写逐对象例外。平台行政元数据与工程事实分别记录。

| 对象 | 定义与不变量 |
|---|---|
| `handoff_digest` | 封存上传文件清单的摘要；主装配相对路径另行固定 |
| 原生传输 `handoff_sha256` | 已封存选择的原生依赖闭包摘要；有未选文件时不等于完整上传摘要，不等于含生成身份与规格文件的制备包摘要 |
| 阶段收据 `handoff_sha256` | `freeze` 绑定完整上传索引；原生阶段及其生成、验证、发布后继绑定原生传输选择身份。复用保留原生产者的同一绑定 |
| `source_revision_id` | 工程内容身份：完整上传清单摘要、主装配、交付配置、选定硬件标识及实际引用的受控记录摘要的规范对象之 SHA-256；使用完整 64 位十六进制值 |
| 工程修订 | CAD/PDM 编写的版本标签及受控原件引用；保持原值与缺项，不用内容摘要冒充工程修订 |
| `request_id` | 一次认证提交的稳定标识；请求封存后不可变，绑定用途、目标配置、六阶段 executor、native tool source/record、契约/检查/资源策略摘要与发布前置基线 |
| `run_id` | 一次工程执行的 UUID；正式新接纳固定从 `freeze` 开始，关联运行保留父运行但形成新的准入与下游身份 |
| `NativeTargetIdentity` | 既有部署登记证明的 Windows runtime 目标；精确字段为 `schema_version/install_id/pipeline_id/target_id`，不由 URL、health readiness、tool、release 或 worker owner 合成 |
| `target_binding_digest` | 单个 native effect target 对 operation identity、main assembly、tool source digest、完整 target identity 与 target ID 的规范摘要；不替代 command、request、archive、tool record 或 payload 摘要 |
| `command_id` | 一个不可变 stage、dispatch 或 cancel command；reconciliation 是 claim-free 操作，不创建 reconcile command |
| `claim_id` / `fence` | 一个命令的一次执行权；owner 是具体进程身份，executor 是工具发行身份，旧 fence 不能提交或关闭新执行 |
| `effect_id` / `effect_key` | 一个已封存外部副作用及其 outbound；effect facts 只追加，UNKNOWN 只能由精确只读观察或 reconciliation 结算 |
| `StageEffectWait` / `StagePending` | 一个未决 effect 的非秘密 locator 与要求调用方 defer 的控制结果；不是 StageResult、failure、decision、checkpoint 或第二状态权威 |
| `artifact_id` | ArtifactStore 中一个不可变 tree；绑定 manifest、member set、producer run/stage/tool 身份 |
| `stage_decision` / `StageQualification` | 每个 run/stage 唯一终态及由其 command、admission、result/failure 重算出的 formal 或 diagnostic 资格 |
| `subject_digest` | 一份完整交付的不可变文件主体摘要；验证只针对该主体 |
| 发布身份 | 仓库、硬件、base、候选分支、前置提交、交付主体及最终 commit/PR |

内容摘要对象的固定字段为 `kind="source-revision"`、`handoff_digest`、`main_assembly`、`configuration`、`hardware_id`、`records`。`records` 是实际引用的 `{reference, file_sha256}` 列表，按完整引用排序且唯一，无引用时为 `[]`；路径是包内相对路径，字符串保持封存值，不以临时绝对路径替代。按照本契约的规范 JSON 序列化求摘要，未引用规格的变化不影响结果。

`source_revision_id` 不含运行 UUID、时间、显示名或工具版本。重复运行相同工程内容保持同一内容身份；新工具与新原生观察仍可产生不同交付主体。完整摘要用于机器判断，页面可展示短前缀；分支与文件名不得依赖拼接工程标签形成无界长度。

摘要必须按对象域分别解释。`target_id` 只摘要
`{schema_version,install_id,pipeline_id}`；`target_binding_digest` 只摘要该 native
effect 的 operation/tool/target 绑定；command 的 `request_digest`、effect 的
`expected_state_digest`/`intent_identity_digest`、artifact 的 `set_sha256`、operation
的 request/archive/payload 摘要、tool 的 `source_sha256` 与完整
`native_tool_record_sha256` 均各有独立对象。禁止把 health、release、URL 或一个摘要
强等为另一个；完整 tool record 与 source digest 也不能跨域比较。规范 JSON 的 metadata
编码由所属写端契约决定，编码正确或改变不改变已定义 semantic digest 的字段集合。

字段按下表解析。Windows 只返回原生值、属性查询状态和实际配置；来源合并、冲突判定及发布就绪在 Linux 完成。缺失与读取失败分别记录，不以空值推定成功读取且不存在。

| 字段 | 权威与缺失处理 | 阶段与发布要求 |
|---|---|---|
| 硬件标识 `dp.hardware_id` | CAD 已有值必须与所选 Linux 配置目标一致；确证缺失时采用配置目标并标记 `platform_configuration` | 目标必选；已知冲突使 `discover` 失败，未读明的身份不能被提交值覆盖并获得发布资格 |
| 工程修订 `dp.revision` | CAD/PDM 原值；缺失为缺失 | 不以提交日期、摘要、运行号派生工程修订；缺失列入评审说明，不单独阻断测量或评审 PR |
| 结构负责人 `dp.owner` | CAD/PDM 已记录责任人；缺失为缺失 | 认证提交人另行保存；缺失不单独阻断测量或评审 PR，不授予工程责任或批准资格 |
| 工程变更说明 `dp.change_summary` | 有效 CAD 原值优先，否则采用必填提交说明，分别记录 `cad` / `submission` | 行尾统一为 LF，保留多行；没有有效说明不能发布 |
| 原件控制 `dp.control.system` / `dp.control.reference` | 原有系统及固定记录成对保留；缺失项保持缺失 | 平台上传索引与内容摘要另行追溯，不冒充 CAD/PDM 控制记录；缺失列入评审说明，不单独阻断测量或评审 PR |
| 交付配置 `dp.delivery_configuration` | 必须是原生读取可确认存在的已保存配置；属性有值时须与实际配置一致；无属性时保留原生保存配置及来源 | 未知、未保存、不存在或冲突均阻断 `discover`；不能使用任意当前配置、提交文本或平台默认值补齐 |
| 模型名 `dp.robot_name` | 读取顶层 CAD 有效原生模型名称；确证缺失时仅可按统一规则格式化原生顶层装配名，详见[命名契约](#naming) | 保留源名称、来源对象和转换规则；行政登记及发布目标不能补齐内部名称，技术名不代表工程修订 |

评审 PR 是待批准候选。缺少工程行政标签不等于机械证据缺失；前者如实展示，后者仍按质量规范阻断。原件、完整上传清单和内容身份必须可追溯。字段存在但无效、读取失败或来源冲突时保留原值和诊断，不静默当作缺失回退。产品名称匹配只提供目标选择建议。更改主装配、目标或原件须形成新请求。

提交的变更说明为必填（可多行，行尾统一为 LF）。候选 PR 的行政说明优先采用 CAD 已录说明（来源记为 `cad`），缺失时采用提交说明（来源记为 `submission`）；两者均缺失时 `discover` 记为 `discovery.summary_missing`，发布就绪不成立。

请求固定发布目标、其配置摘要和基线读取结果。读取失败明确标记为未解析，不能当作首次交付；发布前须在新请求中固定已解析基线。新请求从 Freeze 接纳，只允许按下述规则复用父运行原件，不继承旧下游成功。请求变更、主装配变更或目标变更形成新请求。显示名修改不改变请求。

`freeze` 在调用推导或生成器之前，将已接纳请求封存为 `input/request.json`，schema 为 `solidworks-to-urdf.request/v2`。记录包含 `schema_version`、`purpose`、`request_id`、`target_hardware`、`change_summary`、`main_assembly`、`handoff_digest` 和 `platform_config_digest`；分别固定记录版本、用途、本次请求、经配置校验的所选硬件、规范 LF 的原始提交说明、明确的主装配、完整上传索引与实际平台配置快照身份。记录不包含秘密或凭据，冻结收据绑定其文件摘要，后续阶段原字节沿用，完整交付必须携带该件。

记录恰为上述八字段，全部必填，不接收缺失、`null`、未知字段、重复 JSON 键或旧版记录。`purpose` 仅为 `engineering` 或 `inspection_preview`，写端与读端不得补默认值；页面可在新建时预选工程用途，但提交和冻结必须显式携带选择。请求标识、目标、主装配和说明均为非空文本，主装配为工程内明确的相对路径；两项摘要均为规范 SHA-256。写入与消费共用唯一记录校验和既有严格 JSON 读者，不另建宽容读取或平行 schema。携件与结构校验不取决于生成声明是否填写硬件或说明。离线验证不携带含秘密的私有平台快照；快照实物及其摘要的一致性由冻结写端与数据库接纳/复用边界核验并留证，离线读者核验记录格式、原生来源、主装配与完整上传摘要，不能声称已独立重读私有平台配置。

配置引用遵守单一边界：新接纳请求校验当前安装快照；已接纳请求按其 `platform_config_digest` 解析并校验原快照，不因连接或当前安装切换而改写身份。引用缺失、文件越界、符号链接、权限错误、摘要不符或旧式路由副本均拒绝，不回退到环境中的另一组业务参数。凭据只保留受控文件引用，不封存秘密值。

`AdmissionSpec` 当前为 v3，并封存 exact `NativeTargetIdentity`。生产 factory 从
`PLATFORM_CONFIG_SNAPSHOT` 首次读取并校验的同一组 bytes 定位
`<state_root>/install-record.json`，核对 snapshot lexical path/digest/install ID、
Linux config digest、state root 与 `ends.windows.target_identity`；该 target 写入
Freeze、Discover、Capture command 的 request projection、native effect、operation
wire/view/expectation 和入站 upload admission。不得在 `run_stage`、trigger、Portal
展示或 retry 时重读当前 health/env 改绑已接纳 target。`PreparedAdmission.register`
还须在 admit 前拒绝 application/client target 与 spec target 不一致；现有 request
重试先复用已封存 spec，再考虑当前登记。

正式新运行只从 `freeze` 起点开始。需要复用父运行 CAD 原件时，接纳对象携带
`parent_run_id`，由 `ProductionFreezeSuccessorRequest` 表达新运行、请求、主装配、
目标、用途、说明和提交主体，并调用
`load_production_freeze_successor(...)`。该入口必须通过
`StageApplication.load_verified_freeze_origins(parent_run_id)` 读取父 Freeze
decision/result、`inputs.json`、`freeze_originals` 引用及 ArtifactStore 实际字节，
并只原样复用 `spec.originals`。当前 rules、configuration snapshot、target、
RepositorySnapshot、Git actor/base、native requirement、portable/native release
与六阶段 executor 必须重新接纳并封存；历史 producer 与本次消费者身份分别保留。

新运行不得引用父 Discover、Capture、Generate、Verify 或 Publish decision，
不得复制任何下游成功、qualification 或 effect 状态。父上传目录和 work root 可以
删除，父来源收据或字节缺失、漂移、错绑时拒绝。相同 `run_id` 的同类请求只在完整
行政意图与 originals artifact ref 完全一致时返回原接纳；改变父运行、主装配、目标、
用途、说明、主体或原件身份均拒绝。普通上传接纳不接受 `parent_run_id`，以
Discover 或更后阶段为起点的旧 resume 不进入生产；已登记不可变 command 的安全重试
仍走共同 engine，UNKNOWN effect 必须先只读 reconciliation。

隔离诊断仍可使用显式 `DiagnosticReplayScope` 执行一个目标阶段，但它只引用完整
输入 closure，不伪造上游 decision，不登记正式 checkpoint，也不产生可被普通后继
消费的 formal qualification。诊断输出必须保留 scope 与原 producer 身份，不能晋升为正式下游输入。
后续正式运行重新从 Freeze 接纳并建立自己的完整祖先事实。

`platform_config` artifact 恰含 `config-snapshot.json`。Freeze 与 Discover 从
artifact 校验快照结构并复算 canonical snapshot digest，再与 request 和 command 的
`platform_config_sha256` 比较；CAS member-set 摘要证明 bytes 完整性，但不代替
快照自身身份。

同一六资产 release 可包含不同的 Linux 与 Windows artifact digest。每个阶段只使用
`AdmissionSpec.executors.for_stage(stage)` 中自己的 executor；`native_tool_source_sha256`
和 `native_tool_record_sha256` 表达 Windows 原生闭包，不替代任何 Linux stage
executor。各 role 只与自己的冻结 requirement 比较，禁止假设跨 role 摘要相等。

独立验证从原生读数与该请求证据重算硬件和说明来源，并核对主装配及上传摘要；不能以 `input/source.json` 与发现结果两份生成声明相等替代来源证明。CAD 属性确证缺失才允许对应回退，读取失败、无效值或冲突保持拒绝。即使生成声明被同时修改并重新封装，原请求不变时仍须检出差异。

同一已登记 command 的恢复保留原请求与证据；新的 run/request 则重新生成并封存本次请求记录，不能沿用旧交付的 `request_id` 或下游验证结论。Freeze successor 保留父 originals 的 producer 身份，同时记录本次消费者；原始字节复用不等于请求、阶段结果或资格复用。用途、发布基线及其他输入发生变化时，均通过新的 Freeze 请求建立完整下游，不在既有交付上改值后沿用旧证书。

<a id="naming"></a>
## 工程交付标识与模型命名

工程交付标识采用 `<model>-<total>dof-<head><arm><waist><leg>-r<revision>`；配置码按头、单侧手臂、腰、单侧腿排列，对称结构满足 `total = head + 2 × arm + waist + 2 × leg`。型号和工程修订来自明确登记，不能从文件日期、执行号或代码版本猜测。重复导出或仅修复导出器不递增工程修订。

本次用户于 2026-10-20 确认的初始基线为 `M3-23dof-2515-r01`，对应原主装配 `M3.0.3_20261016.SLDASM`，主装配 SHA-256 为 `647b756847bb08f99287b71b86ea55e4cfb554df22f51a3e9f2ed337472799ce`，原生交付选择摘要为 `5f5c1f1a5dd874e8993486e0ddfe5043d422ec272bba63dbe59cb8a7488406db`。登记必须绑定完整冻结来源和配置，并与本次请求核对；不得把登记值伪装为 CAD 内已有的属性。原件名称、原件字节、原始声明和失败证据不变，缺失的原生 `dp.revision` 仍记录为缺失。

工程交付标识是行政登记，与 CAD 内部名称分别保存，不能补齐原生模型名或机械修订。URDF 文件入口固定为 `urdf/robot.urdf`。XML 内部模型名读取顶层装配中的有效原生声明或属性；确证缺失时，允许对本次原生顶层装配名作统一、确定的格式化，绑定原始文档名、配置和摘要，显示派生来源 warning。格式化不得猜测型号、自由度或修订，也不得引入登记名、发布目标或手工名称。原生属性读取失败、无效或相互冲突不得按缺失回退。

参考测试允许 XML `robot.name` 与适用手工 fixture 的 inspection 标签不同，并在测试 diff 中列出；该标签不进入生产或发布判定。生产模型名仍须符合当前 CAD 来源规则、格式、来源 warning 和独立复算要求，不能任意生成或伪造，也不改变其他对象名称、引用及固定文件入口的要求。

参考测试允许两类字符串差异：从 CAD 实例稳定派生的 visual 名称可不同于 fixture 的 `native_N` 序号，带来源 warning 的展示材质名称可不同于 fixture 的 `cad_gray`，测试 diff 仍须展示。生产不读取这些参考名称；名称稳定、格式合法、对象绑定、定义与引用一致、颜色和纹理、几何、来源及独立复算要求不变。不允许用名称放行掩盖材质缺失、颜色变化、错误绑定或未验证来源，也不放宽 link、joint、collision、frame 或网格的既有要求。

模型接口统一遵循[机械交付规范](mechanical-handoff-spec.md#名称与原生引用)：唯一根为 `base_link`，其他刚体使用 `_link` 后缀，运动关节使用 `_joint` 后缀，方位使用完整 `left/right/front/rear/upper/lower`，接口按实际用途使用 `_mount/_tcp/_optical/_frame`。声明模式与配合推导模式执行同一输出命名要求；仅通过 snake_case 语法检查不能视为符合语义规范。

原生名称与输出规范不一致时，允许根据 CAD 对象及既定命名规则进行格式化、词根展开、后缀统一及有依据的语义修正。规则可以按原生对象类型、安装侧或其他已证明的 CAD 特征分支；同样条件使用同样规则。保留原生持久身份、原名称、应用规则及输出名。由此生成的对象映射表可作为证据保存，映射表本身不是禁止对象；不得由手工导出抄得目标名称再倒填映射。CAD 名称不规范、词根未识别或规范化后重名均给出可定位的 warning，按版本化规则修正和消歧，不因名称违规本身阻断。不能从旧名称猜测机械性质；原生对象无法唯一绑定、对象归属矛盾或引用断裂属于独立的结构／证据错误。命名不得改变连接、归属、位姿、正向、质量或几何；独立验证须从原始证据复算规则及实际产物。

网格几何从本次 CAD 采集取得；输出文件名和路径可按明确的统一规则生成或规范化，绑定当前原生对象和实际内容摘要。不得复制手工网格、沿用手工网格别名来达成对齐或将历史哈希当作当前内容身份。当前清单记录真实字节摘要及实例绑定，独立几何验证以本次 CAD 证据为依据。

参考回归测试采用用户批准的[网格几何等价标准](quality.md#geometry-equivalence)：同源同配置的每个原生对象，在同一坐标系中证明双向曲面距离上界均不超过 `5e-5 m`，且拓扑、覆盖、朝向及既有质量门通过后，可关闭参考网格表示差异。此规则只判定适用测试 fixture 的参考几何关系；未证明上界、超限或结构不一致不得把该测试标为通过。测试报告保留真实路径和摘要，不将手工比较结果设为生产输入、验证真值或独立发布门；生产几何仍严格核验当前 CAD 原始事实与实际交付。

### 输入命名警告与输出一致性

命名依据为[《MimicVerse URDF 工程标准 v1》](https://hcnd2fgke1pv.feishu.cn/docx/N3iSdpYxGo151UxgBz6csCthnSb)第 3、6、7 节及本项目的自动导出补充。该文档当前标为 RFC／v1.0 草案；它规定规范输出，不将现有 CAD 名称违规设为本流水线的阻断条件。按用户 2026-10-20 要求，CAD 名称不规范时保留原名、发出 warning、执行确定性修正，继续处理；最终输出仍须规范、唯一且引用闭合。

所有实际输出的 `name` 与名称引用由同一份“原生对象身份 → 规范名称”映射生成。映射是 CAD 和版本化规则的派生证据，不是手工填入的第二模型。独立验证按原始对象和规则重算。至少覆盖以下类别：

| 类别 | 规范及一致性要求 |
|---|---|
| `robot` | 稳定模型技术名；按前述原生模型名及格式化规则处理，与固定入口 `urdf/robot.urdf` 分别管理 |
| 物理 `link` | 根为 `base_link`；其他为 `{side_}{physical_body}_link` |
| 运动 `joint` | `{side_}{body}_{motion}_joint`；机械功能来自 CAD 语义，不能由名称反推轴向 |
| 固定 `joint` | `{parent}_to_{child}_joint` 或 `{component}_mount_joint`；引用实际规范化后的两端 |
| 附加 frame | 按用途采用 `_mount`、`_tcp`、`_optical` 或 `_frame` |
| `visual`、`collision` | 由所属 link、原生组件／几何身份和类别生成稳定名称，分别以 `_visual`、`_collision` 结尾；多个几何不得用遍历顺序区分 |
| `material` | 根据原生外观语义或明确的显示策略生成稳定名称，以 `_material` 结尾；定义与引用一致，同名不能绑定不同内容 |
| 网格文件与 URI | 按几何类别和稳定资产身份生成 ASCII 路径；basename 与实际文件、URDF URI 和清单完全一致，不采用手工别名 |
| 其他已输出引用 | `parent`、`child`、`mimic`、传动／控制／传感器引用及关节顺序等存在时，同步使用目标对象的规范名，不保留旧名或另建别名链 |

格式化顺序固定：绑定原生身份与对象类别；按版本化词表识别 CAD 语义；转换大小写、分词和分隔符；按对象类别补齐一次规范后缀；展开已确定的 `l`／`r` 安装侧为 `left`／`right`；统一校验和消歧；最后统一写入定义、引用及文件路径。名字满足 ASCII 小写 `snake_case` 和 `^[a-z][a-z0-9_]*$`。不能把某个关节名直接用作物理部件名，也不能根据任意字母片段猜左右。REP 明确要求的互操作 frame 单独按对应规则保留。

未识别的 CAD 词项保留在来源记录。只有名称表达不足时，使用已证明的所属 link／组件类别等中性语义及稳定原生身份生成技术名并 warning，不捏造部件功能；不得以 `link1`、遍历序号、随机值、运行号或手工结果作为命名依据。重名优先使用已证明的安装侧、模块或父级区分，仍同名时按固定原生身份规则生成稳定区分项并记录原因。规则表及必要区分项的编码必须固定版本，同输入同规则重跑不得改名，且再次规范化不改变结果。

每个修正警告须记录对象类型、稳定原生身份、原名称、规范名称、规则 ID／版本和原因；报告、URDF、预览与 PR 使用同一记录。原名仅作为来源数据保留，不再作为输出引用。最终输出仍含非法名、重复定义、断裂引用或文件路径不一致时是生成／验证缺陷，必须修复；不能把这类缺陷误说成 CAD 名称不规范而要求工程师改 CAD。

<a id="value-provenance"></a>
## 值来源与警告

所有模型及工程交付中使用的值，都须能够区分“CAD 原生直接读取”与“非原生直接读取”。原生直读绑定实际 API、主装配、配置、原生对象、封存证据位置及摘要；存在于 CAD 声明中的作者值，须同时注明其声明来源，不能冒称已经独立测量。来源未知、读取失败和冲突继续按原有验收门拒绝，不能改成警告后放行。

任何非原生直接读取的值必须产生明确来源 warning，包括规则修正、名称规范化、单位换算、坐标变换、RPY 分解、轴向重表达、质量或惯量汇总、受控分析策略及平台元数据。允许版本化配置和记录承载规则、适用条件与来源；不能仅因规则存放在 CAD 外就禁止。规则须能从当前 CAD 输入复算，记录原值、结果和依据，不得由手工结果补值或掩盖原生冲突。计算通过不等于原生直读，warning 也不等于工程批准。

警告定位到稳定对象和字段，引用实际交付值及其原始依据，记录非原生来源类别和原因；计算派生值另引用算法／工具版本及必要常量来源。同一来源或算法可以聚合展示，但必须能展开到全部受影响字段。复用现有来源记录和诊断，不另外保存一份模型或数值状态；以已绑定交付中的精确字段引用取得实际值即可。

质量报告、页面预览及 PR 使用同一组来源警告；URDF 文件也须包含可见的英文警告摘要及详细来源清单位置。英文提示明确值并非从 CAD 直接读取。工程登记 `M3-23dof-2515-r01` 只作行政记录，不能用来填内部模型名。警告说明来源，不授权手工或外部值补齐，也不授予工程或实机资格。

<a id="purpose"></a>
## 用途与资格

`engineering` 与 `inspection_preview` 均要求 CAD 唯一工程来源和各自必需校验。检查预览可如实展示缺项，并使用下述受控分析窗口，但不能借用途编造工程范围、正向或名称。两者均拒绝原生读取失败、损坏声明、不可比较来源和冲突；原始观察不得写入平台派生窗口。用途只从已绑定的 `input/request.json` 消费，发现、采集和生成声明不另立用途权威。

位置范围逐关节绑定 CAD 对象、配置、零位、正向、单位、启用状态和原始读数。允许按明确规则换算单位、重表达坐标与符号，并同步转换上下界、零位偏置和采样。不能从手工结果补机械限位、把未知当作无限，或覆盖真实启用的零范围。机械范围与显示／分析窗口分别记录；分析窗口规则须明示用途、适用条件、数值依据和未建立的资格，不能冒充机械范围或反写原生观察。缺少必需工程依据仍按用途与质量门处理，不因采用规则就自动失败或通过。

导出器保存的 `lower = upper = effort = velocity = 0` 本身不能证明启用了机械行程限制，也不能授权默认范围。角度和距离配合须保留原生普通／限位状态及对应 API 读数；`IsAdvancedMate` 为真时保留真实上下界，包含零范围；为假时不能把普通配合尺寸当作工作限位。未知或失败不得当作假。配合清单缺失、相关端点无法绑定、跨子树归属不明或观察不完整均不能证明工程范围缺失。原始作者读数及来源摘要完整保留。

检查预览采用版本化规则 `inspection_rotation_full_turn/v1`：仅在完整原生观察证明没有启用的机械位置限位，且没有有效作者工程范围时，为旋转关节提供弧度制 `[-π, π]` 全周显示／分析窗口。该规则独立于手工参考定义，不按工程或对象 ID 查表，不适用于移动关节或 `engineering` 用途。已有有效 CAD 范围优先，包括已建立来源的真实零范围；缺失、不可读、冲突或未证明的适用条件不能用窗口掩盖。全零作者记录按原样保留，其本身不建立机械限位，也不能仅由没有原生限位推断作者意图。

若检查预览 URDF 的 `lower`／`upper` 承载该窗口，其来源必须明确标为 `preview_policy`，逐字段引用规则版本、原生缺项证据和原始作者记录，不能标为 `native` 或 `declared_author`。工程范围保持缺失或未建立；复用现有来源／资格记录，表达 `native_cad_ranges=false`、`hardware_qualified=false`，不新增第二份数值状态或模型。URDF、报告、预览及 PR 显示 CAD 工程范围缺失和分析窗口来源警告，明确这不是机械行程、可达性或安全保证。独立验证从原始观察重算适用性与窗口，而非信任生成的策略标签。`effort`／`velocity` 不由此规则生成。

用户明确授权工程用途在旋转关节读不到可用原生或非零作者位置范围时，使用独立版本化规则 `engineering_rotation_default_window/v1`，数值为弧度制 `[-π, π]`，来源标为 `default_policy`。原生读数以及非零作者声明优先；多值冲突或原生／作者非零冲突仍 fail-closed。默认规则必须保留原始作者值和原生缺失或读取错误证据，仅适用于旋转关节，不能应用于移动关节、fixed 或 continuous 关节。`engineering_position_range_qualified=false`、`hardware_qualified=false`，`engineering_position_range` 资格保持 `unqualified`。URDF、模型来源、质量报告、预览和 PR 必须显示同一来源 warning，明确默认窗口不是 CAD 原生机械限位。

驱动规格缺失时，规范化事实保持缺失，URDF 省略 `effort` 和 `velocity`，不能填零占位或编造能力。明确读取的作者零值仍按原值序列化并保留既有资格警告；零值不等于缺失。检查预览的省略编码须通过同版锁定消费者的实际加载检查；加载通过只建立该消费者的模型一致性，不扩大到其他消费者或工程用途。

独立验证从冻结请求、本次 CAD 原始实例、连接、轴线、原生声明和限位观察，以及实际采用的版本化规则重建预期。核验规则适用条件、原值到结果的完整变换和物理一致性，不能只比较两份生成声明。逐实例、单关节和适用组合的非零运动、来源、配置、几何、物理闭合、消费者加载和报告绑定均须执行。分析采样不建立机械可达性或实机安全资格，手工结果不得提供验证预期。

资格复用 `established / unqualified / out_of_scope` 与 `(fact_id, scope, object_id)` 键。模型一致性事实与 `engineering_position_range`、`drive_sizing`、`machine_qualification` 分别记录；前两项按关节定位，末项针对交付主体。证据引用绑定请求及原生／确证缺失记录，不能从生成声明或检查通过推断外部工程批准。检查预览交付的工程范围、驱动和实机资格明确为 `unqualified`；有效工程数值仍如实展示并核验。

报告、页面、预览与 PR 显示同一用途、原生范围来源、参数缺失及资格。程序中的英文提示须明确说明缺失驱动参数不建立驱动能力。全部必需检查通过后可以发布绑定用途的待评审 PR；工程批准和实机资格未建立本身不新增自动失败门，也不能被 PR 创建授予。工程参数缺失、冲突或来源不符合 CAD 唯一来源要求时，仍按相应必需门拒绝。

<a id="stages"></a>
## 六阶段契约

| 阶段 | 执行 | 输入与输入检查 | 封存输出与输出检查 |
|---|---|---|---|
| `freeze` 冻结输入 | Linux | originals、ruleset、platform configuration、认证主体、主装配、配置目标、用途及必填说明；artifact closure、路径、文件类型、大小和选择有效 | `stage-request.json`、`request.json`、`inputs.json`、`ruleset.json`、`configuration.json`、`checks.json`；清单闭合、字节及摘要一致；不启动 CAD |
| `discover` 解析结构 | Windows 原生观察 + Linux 推导 | 已验证 Freeze 输出、SourceSet、ruleset/configuration identity 和准入的 native tool source/record identity；artifact 与工具身份一致 | `native-observation/`、`prepared/`、`request.json`、`checks.json`、`result.json`；原生观察、selection、ruleset 和 prepared bytes 一致 |
| `capture` 采集证据 | Windows 原生读取 + Linux 传输核验 | 已验证 Freeze/Discover 输出和同版 native tool requirement；prepared bytes、source inventory、tool identity 与对象归属一致 | admitted tree 的 `input/`、`evidence/`、`reports/native-tool.json`、`reports/native-stages.json`、`transfer-manifest.json`；原生事实、对象和配置绑定、几何覆盖、失败保全和资源结算通过 |
| `generate` 生成 URDF | Linux | 精确 committed Capture artifact；snapshot、schema、provenance、source warning、source binding 与 tool identity 有效 | `model/robot.json`、`urdf/robot.urdf`、`meshes/`、`tool.json`、`reports/generate.json`；确定性、完整并绑定 Capture |
| `verify` 独立验证 | Linux | 精确 committed Generate/Capture pair；原始证据和实际生成字节未变 | `verification-certificate.json`、`verification-execution.json`；全部必需且适用的检查通过，并绑定 pair、verifier、contract、policy 与 subject |
| `publish` 提交评审 PR | Linux | Generate/Capture/Verify artifacts 与 sealed RepositorySnapshot；重验、配置目标、前置基线和干净工作区有效 | `publication-receipt.json` 与 fenced Git/PR effects；copied bytes、committed tree、remote head/base、PR 和 verified subject 一致 |

Airflow 的任务标识依次为 `freeze / discover / capture / generate / verify / publish`，与此表一一对应，手动触发，不设周期调度或 catchup。六个 task 都实例化
`StageTaskOperator`，其 `execute()` 绑定真实 DagRun/TaskInstance 后调用
`StageApplication.run_stage()`；`StagePending` 经 `defer()` 交给
`EffectPollTrigger`，`execute_complete()` 校验持久 wait 与 event digest 后调用
`StageApplication.resume_stage()`。Airflow 固定为 3.3.2，并运行受管 triggerer。
上传、原生操作派发、结果等待和传输核验均位于所属工程任务内，不作为另一套阶段。
原生操作使用容量为一的资源池，任务重试保留操作身份；阶段重做只通过关联运行命令
发起，不用 backfill 或直接 clear 改写工程历史。DAG 当前设置
`max_active_runs=8` 与 `max_active_tasks=8`，只约束调度并发；CAD 排他由 physical
ownership、native pool 与 target/effect 冲突分别执行，不能把 scheduler 并发字段
当作 CAD mutex。页面、日志与机器收据消费同一阶段定义。

冻结以数据库已接纳请求为比较基准，核对原件摘要、文件数量与总字节数、主装配、目标、用途和平台配置身份；不能从待检查的目录重新计算一份期望值并据此自行证明接纳。实际冻结检查事件与封存收据一并登记，已确认阶段的同键重放不重复产生检查事件。Windows 只执行 `discover` 与 `capture` 的原生部分，不另行执行或确认冻结。

初始接纳在同一事务登记 `RunAdmission`、Freeze `StageCommand`、technical
`DispatchCommand`/conf 与 prepared dispatch effect。Portal 调用
`StageApplication.dispatch(command_id, adapter)` facade；facade 内部由既有
`TechnicalDispatchService` 先取得 technical command claim/fence 并提交
`dispatch_authorized`，再调用 Airflow trigger。真实 Freeze task 只在实际启动后绑定
`dag_id/dag_run_id/task_id/map_index`，不能首次授权自己的创建。响应或超时未知时按
dispatch effect 进行 reconciliation，禁止绕过该顺序重新 trigger。

### 唯一阶段事实与祖先资格

`StageApplication` 是六阶段的唯一生产入口。`RunAdmission` 是不可变接纳事实；
`StageCommand` 绑定完整请求、输入 ArtifactRef、配置、契约、策略、executor 和
immediate predecessor；`ExecutionClaim` 提供 owner/executor/fence；external
work 由 append-only `EffectIntent`/`EffectFact` 表达；handler 只产出 candidate，
唯一 `StageDecision` 才形成阶段终态。ArtifactStore 保存不可变 bytes 和 manifest，
平台数据库只保存其引用与结构化 metadata。

每个 successor builder 必须从同一 admission 与当前已验证 predecessor 集合重建
command；`FactStore.create_command` 在同一 `run_admission` 行锁内逐项重读 command、
decision、sealed StageResult、输出 artifact 与 supporting predecessors，并调用
`qualify_stage/require_formal_qualification` 重算资格。全部祖先的 admission
request digest、command/request digest、result/failure digest、executor、artifact
ref、diagnostic scope 和 effect ledger 必须与当前事实一致。缓存、页面摘要、
`formal_eligible` 布尔或证书自报值不能替代本次重算。
当前六阶段只沿同一 run 的 bounded command/predecessor decision 链验证 immediate 与
supporting predecessors；`command_input_artifact.position` 保留输入顺序。跨运行复用
只允许通过父 Freeze originals 建立新的正式运行，不定义通用的多跳 checkpoint 或
producer lineage 兼容链。不得仅凭相同 artifact 摘要省略 command、decision、
metadata 或 producer 身份。
该复核在 successor command 创建、claim 获取、已决结果读取/提交和 effect
authorization 边界分别执行，防止另一个进程已使祖先冲突后仍由缓存继续外发。

阶段事务只保存事实和引用：result/failure artifact、candidate output/result、
effect outbound、payload、reconciliation metadata 与 FailureEnvelope evidence
各自保留 producer 身份和摘要。任何 metadata artifact 都与 payload 分开登记并进入
恢复根；inline failure 同时保存 canonical bytes 的 digest。文件存在、Airflow task
成功或 endpoint job 完成不能单独建立 stage qualification。

阶段间不传递进程内对象、隐藏缓存、可变临时目录或调度器内存作为业务输入。阶段只从
command、ArtifactRef、StageResult/FailureEnvelope 与 canonical facts 重建本阶段
workspace；重启后的同一 command、独立复跑和新组合测试都必须得到相同输入闭包。
上一阶段的任务成功或同进程残留不能替代当前输入登记与资格复核。

等待外部 effect 的统一协议为：只有完整不可变 command/effect、sealed 可重建 outbound
和当前 claim/fence 可进入 pending-effect yield。共同 engine 返回
`StagePending(StageEffectWait)`；这不是 `StageResult`、failure、decision、checkpoint
或第二状态权威。让渡必须原子关闭旧 owner 的发送、owner 事实追加和 decision 权限。
`StageTaskOperator` 在真实 Airflow task context 中执行普通 stage 或恢复，并把
`StagePending` 转成 defer；`EffectPollTrigger` 只按精确
effect/operation/request/outbound/tool/target identity 观察并给出 wake-up hint。
trigger 不取得 claim、不发送命令、不 seal payload、不写平台 fact/decision。

native trigger 的 401/403、同 target 但错 operation 的
`native_operation_identity_mismatch` 及其他非 `native_target_mismatch` conflict
必须中止；404、缺 status 的 transport error、可识别协议错误、5xx 或明确的
`native_target_mismatch` 保持同一 wait 且 `changed=false` 继续观察。错误 target、
错误 operation 或过期 wait 不能被吞成仍可唤醒。trigger 重启、迁移、超时或清理不得
取消外部操作或释放仍工作的 CAD lease；event 内容、XCom、serialized kwargs、工作目录
及 trigger 自身失败均不建立工程资格。

`StageTaskOperator.execute_complete()` 必须核对 persisted wait 与 trigger event
digest，随后使用新的真实 Airflow task context 调用
`StageApplication.resume_stage(wait)`。application 重验同一 command、effect、request、
outbound artifact、工具、外部 target 与当前祖先资格；无 claim 的权威 observation
只在 worker 边界封存 payload/metadata/fact。terminal effect 随后进入独立 handler
`resume_stage()`；只有 canonical `input_transfer_pending` 才进入窄
`resume_transfer()`。重复、陈旧、错绑或伪造 event、丢失 ACK、UNKNOWN 和 observer
outage 均不得造成重复 native/Git/PR 外发或伪造成功。原 external request 内的旧
claim/fence、native producer 与工具身份不变。即时完成的 Git/PR 可以直接结束，不强制
等待。

### 受限 transfer continuation

`EffectResumePhase.external_pending` 与 `input_transfer_pending` 不是新的阶段状态、
effect 状态、持久业务 enum 或补偿队列；它们只由 canonical facts 和权威只读观察推导，
用于选择 worker 内部 resume 路由。`external_pending` 只走 terminal
`resume_stage()`；只有 `input_transfer_pending` 可选择 narrow
`resume_transfer()`。该 phase 不进入 `StageEffectWait` 的持久字段；wait 仍只绑定原
command/effect/request/outbound，phase 在恢复时从当前 canonical facts 重新推导。
生产判定至少同时要求：

1. 只针对 `native_operation` effect，权威正观察证明原 operation 已 accepted；
2. 原 effect 的 input 尚未 admitted，且没有返回 absence 或从空结果猜出的终态；
3. 原 command、effect、envelope、request、payload、archive 与 tool identity 精确
   绑定，封存 outbound 闭包 byte-for-byte 可复原；
4. 当前 admission、祖先资格、cancel、维护和 owner/resource 条件仍允许此命令；
5. 原 claim 已让渡，或过期后由 claim 事务核验并关闭；新的受限 claim 绑定同一
   command/effect 并递增 fence。未过期的 live claim 不可接管，不建立第二 command
   或 effect。取得 claim 本身不授予发送权。

共同 engine 取得受限 claim 后，在维护共享锁内调用 `resume_transfer()`。handler
先重建并核对原 outbound/archive，再调用 `authorize_resume()`；后者以独立短事务
重读当前祖先资格、cancel、claim/fence、租约、effect 与 outbound，追加
`resume_authorized`。该事务提交后才能外发；claim 取得后发生的取消、祖先冲突或
租约到期仍须拒绝，物化失败也不能外发。两个事务之间崩溃保留原 claim 和证据，
恢复须重新走同一核验边界。该端口只提供对原 operation 上传原 archive 的能力，
不能新建 effect、普通发送、创建
candidate、提交 decision/failure 或扩大成 general execution。handler 成功返回只能是
`StageDeferral`；上传 ACK 后由 engine 再次 yield 同一 effect。原 external request 内的
platform claim/fence、producer 与实际工具身份保持原字节；新 fence 只授权该传输
continuation，不扩大为普通 handler、CAD、Git 或 PR 发送权限。trigger、event、
phase、日志、XCom 或页面观察均不授权 transfer。

404、空查询、observer failure、错误 operation/archive/tool/effect
identity 或其他不可唯一核定的结果保持 UNKNOWN 或明确拒绝，不得触发 create、重新
接纳 operation 或构造新 archive。上传可能激活 native 执行，因而必须通过服务端原子
幂等保证 input admission 与 executor 各一次。ACK 只是传输事实，不证明 native
success；ACK 后必须再次 yield，由后续 observer/admission 形成终态。后续取得的瞬时
终态若已有完整原始证据，可直接走权威 settlement。

未授权的 cancel 不能执行；`resume_authorized` 提交前发现已登记 cancel 时必须拒绝。
发送授权与 cancel/observation 竞争时，只追加真实顺序和原件，不以较晚事件覆盖已发生
权限；cancel ACK 只结算 cancel effect，target native effect 仍需 claim-free
canonical reconciliation。Git ref 与 PR upsert 的
UNKNOWN 永远不能使用 `resume_transfer`、重传或普通 handler 重跑解决；git/gh 只读
probe 的 wake hint 同样不授予权限。

Discover 的 `request.json`/`source.json` 与 Capture 的 `outbound.json` 使用各自既有的
metadata 写端编码。当前 Discover 两件均为 canonical JSON 加恰一个 LF，Capture
`outbound.json` 为 canonical JSON 无尾随 LF；consumer 逐字节核对，不执行 `strip()`、
任意 whitespace、key 重排或双读宽松。该 encoding 差异不改变 HTTP wire 或 semantic
digest，但错误编码不得被解释为不同语义后继续外发。

接纳事务以 admission gate 与 `run_admission` 为边界。已存在当前 command 时，
successor、claim、send 或 decision 操作先持有 command 边界，再从根 predecessor 到叶
重验有界 decision/result 与 effect ledger，随后进入当前 claim/fence、
cancellation/effect、candidate/decision 边界。不得引用已删除的 run 表锁。
immutable intent/fact/decision 只追加，不原地改义。外部 CAD、Airflow、Git、PR、
HTTP 调用不得持有数据库事务；dispatch authorization 已提交后才允许发送。
reconciliation 不取得 claim/fence，也不创建 command；它只锁定当前
`effect_intent`/`effect_fact` sequence，并重验原 command、outbound、artifact 与
metadata closure。同一 command/effect 恢复复用原 intent 与 outbound；外部请求中
已封存的旧 claim/fence 保持原字节，新执行权则由 canonical claim 规则产生新的 fence，
不能把历史请求身份当作当前发送权，也不能并行建立第二套状态。

<a id="recovery"></a>
## 状态与恢复

`project_stage_status` 与 `project_run_status` 是唯一业务状态投影。它们只消费
canonical facts 的真实阶段 decision/effect 以及实际 Airflow task/DagRun 关联；
SQL view、Portal、operator 和 display metadata 均不得另写状态 reducer。正式运行
要求六个 formal stage decision 按序全部 `completed` 且全部 effect 为 `satisfied`；
任一当前未收口的 UNKNOWN/CONFLICT 为 `unresolved`；合法 reconciliation 可以按原
effect identity 追加 settlement，历史 fact 不得使已收口结果永久冲突。缺少任一
formal stage 为 `incomplete`。
`DiagnosticReplayScope` 只产生目标 stage 的 diagnostic 结果，不能填入这六个 formal
outcome，也不能让正式运行或下游取得成功资格。

`read_admitted_runs` 从 canonical command、decision 与 effect facts 调用
`project_effect_state/project_stage_status/project_run_status`。SQL
`lifecycle_projection` 只提供真实 Airflow scheduling/DagRun 关联和 drain/stop
bucket，不是 run、stage 或 effect reducer。`RunControlStore` 只提供运行查询与显示
控制：通过上述共同业务投影读取当前运行状态及必要 Airflow 关联，只写
`run_display_metadata`。其显示名、隐藏、恢复方法不得写 command、claim、candidate、
decision、effect 或 qualification，不能作为第二状态权威。隐藏台账只控制页面可见性，
不删除、停用或改变原运行、证据、资格及生命周期关联。

维护 `operator_ro` 仅可 `SELECT` `run_admission`、`command`、`stage_decision`、
`effect_intent`、`effect_fact`、`execution_claim`，以及
`lifecycle_projection`/`admission_projection`。六表用于同一只读
repeatable-read snapshot 中的 `read_admitted_runs` 复算；该角色不得写这些事实、
`run_display_metadata`、其他平台对象或直接访问 Airflow schema。

| 状态对象 | 规则 |
|---|---|
| 运行 | 由共同 projector 读取六个 formal stage decision、完整 effect ledger 和真实 Airflow 关联，投影为 `incomplete → succeeded / failed / cancelled / unresolved`；不保存第二份可变 run 状态 |
| 阶段 | 由 command、claim、effect 与 decision 投影为 `waiting / active / blocked / completed / failed / cancelled / unresolved`；每个 run/stage 最多一个终态 decision |
| 外部 effect | `prepared / unknown / satisfied / definitely_failed / conflict`；只有 `prepared` 可在当前 claim/fence 下首次授权发送 |
| command/candidate | immutable command 绑定输入；sealed candidate 在 decision 前保留 output/result；decision 不得从未封存文件重建 |
| 检查 | `passed / failed / not_run / not_applicable`；不适用必须有策略定义的条件和证据 |
| 警告 | 独立诊断，包含对象、事实、来源及资格影响；不改变必需检查的判定 |
| 执行范围 | `executed / reused / out_of_scope`；复用只从原 decision/result artifact 与其完整祖先事实重建 |

运行和阶段显示状态是只读投影，不能作为可写业务权威。无 decision 且存在当前 open
claim 为 `active`；无 open claim 且前置完成但尚未创建/执行后继为 `waiting`；
已登记 effect 为 UNKNOWN/CONFLICT 或未封存 candidate 保留为 `unresolved`；已证明
前置失败且后继未执行为 `blocked`。completed `StageDecision` 必须绑定 completed
`StageResult`、满足的完整 effect ledger 与相同 executor；failed/cancelled decision
必须保留 failure/result 原件，未知 effect 不得被隐藏为确定失败。终态不复写。

裁决提交在同一短事务内重读 command、cancellation、claim/fence、完整 effect ledger、
candidate 与所有祖先资格。completed decision 要求 ledger 与 result 声明完全相等且
全部 SAT；failed/cancelled decision 拒绝 UNKNOWN/CONFLICT。迟到的 response/timeout
只能追加审计事实，不能把已提交终态改回成功。首次外部调用必须发生在 dispatch
authorization 事务提交之后，数据库锁不得跨 HTTP、CAD、Git 或 PR 调用。

失败归属实际执行阶段。原生主装配打开或选择失败登记为 `discover` 失败，保留已完成的冻结收据。未执行后继由共同 projector 根据前置失败投影为 `blocked`，不补造后继执行或第二份阻断状态。对账保留已有 decision 和诊断，并核验尚未结算的真实 effect。

关闭准入阻止新请求与新运行；已准入运行的合法后继、同键重试、取消、对账和释放继续按原身份与前提执行。已知前提失败的 `blocked` 阶段不是活动操作。停止服务要求准入已关闭，且业务与实际调度均无活动、未决或未知；仅有业务终态或暂停 DAG 不满足此条件。

`start --keep-admission-closed` 复用普通启动的来源、双端身份和服务检查，但成功后仍保持平台准入关闭，过程中不得短暂开闸。该选项只适用于 `start`，不形成配置文件中的另一份准入状态；任一失败或未知状态都不得开放。对账和维护验收完成后，再由不带该选项的 `start` 重新通过同一检查并开放准入。

部署生命周期调用同时核对 SSH 退出状态与动作匹配的结构化回执：只有退出为 `0` 且 `passed=true` 才是成功；退出为 `0` 或 `1`，且回执动作一致、`passed=false` 并有明确错误码时，记录确定拒绝。连接错误、缺失或损坏回执、动作不匹配、退出状态与回执矛盾均记为未知；不能把非零退出一律当作运输中断，也不能用断连前的回执残片确认结果。原始输出、退出状态和分类分别留证；确定拒绝本身不授予不受控重试权限。

同一 command 的传输重试复用原 command、effect intent 与封存外部请求；当前执行权
按 claim/fence 规则重新核验。相同请求返回已登记事实，不同参数拒绝。Linux 重启后从 canonical facts 恢复并查询原外部
operation，不重复打开 CAD。原生执行器重启后，未封存的在途操作终止为失败并保留
原始响应、outbound 和诊断；未知 COM 状态不能续算或获得清理授权。

同一 command 已有成功决定时，只有原 `StageDecision`、sealed StageResult、output
artifact、完整祖先及 effect 仍通过核验，才可返回原结果。新的正式请求及 Freeze
successor 均从 Freeze 开始，只复用父 originals，不引用父下游 decision。父失败、
取消、未决证据和原 producer 不得覆盖；工具、CAD 输入、规则、配置、目标或 native
requirement 变化必须形成新请求及其下游执行。

传输断连后恢复同一 effect。同一不可变 command 可在当前输入、工具、祖先资格和
owner/fence 约束下安全重试或恢复；已有终态决定时只复核并复用，不新建决定。新的
正式重做只能创建从 Freeze 开始的新运行；显式指定 target 的诊断只产生非 formal
结果，不能续成一个正式旧阶段。对已发送的 native 或 Git/PR effect，UNKNOWN 状态下
原 handler 不得取得普通 execution claim 或自行重发；先通过
`StageApplication.reconcile_effect` 的精确观察处理。唯一传输例外是已证明
`input_transfer_pending` 的原生 operation，按受限续传协议取得窄 claim 与独立发送授权。
technical dispatch UNKNOWN 仅由
`StageApplication.dispatch`/`TechnicalDispatchService` 观察原 DagRun/conf。

恢复判断使用当前阶段及其受控操作，不保留已删除任务的状态兼容表。工程失败、运输中断、产物损坏和外部结果未知分别记录；运输恢复不得改写工程失败或重复原生执行。门户、重跑授权、维护和后台对账读取同一平台权威及实际调度关联。

共同程序 API 固定为
`StageApplication.reconcile_effect(command_id, effect_key)`；Portal、后台自动重试和
其他进程内调用直接使用该 API，不调用 shell。维护人员使用同版
`operatorctl reconcile --run-id <run> --stage <stage> --effect-key <key>` 作为唯一
外部 CLI adapter；它通过 `FactStore.load_stage_command(run_id, stage)` 取得唯一
command，再调用上述 API。该 API 只处理 native operation、Git ref 与 PR upsert；
technical Airflow dispatch 由 `StageApplication.dispatch` 的独立 path 观察原
DagRun/conf。不得新增 native-capture、release、stage 或 publisher 专用对账命令。
正式发行只携带 `deploy/operator/reconcile-effect.py` 这一应用包装入口，必须删除
`deploy/operator/reconcile-native-capture.py`；旧脚本不得作为 fallback。只读预演
输出原 command/effect、目标 provider、证据摘要和唯一预期变更；正式执行重新读取同一
事实。

公共调用面固定为 `EffectReconciliationAdapter.observe(command, intent,
outbound_root, workspace_root)`。adapter 只返回完整 domain payload 或已有
ArtifactRef，不接收 authorize/send/fact-write 能力；`EffectReconciliationService`
负责验证 outbound、封存 payload/metadata、追加 fact 和重新投影 settlement。
生产 composition 有唯一明确入口 `application_from_environment()`；它安装
`native_operation`、`git_ref_update` 与 `pr_upsert` 的 reconciliation adapter，并由
application 按原 intent 的 `effect_kind` 选择。该 registry 不接受
`airflow_dispatch`。Portal 与自动重试持有该 application 并直接调用
`reconcile_effect`；operator CLI adapter 只解析 run/stage 与显式 effect key。各调用方
不各自构造 observer、持有 kind 映射或复制第二套 reconciliation lifecycle。

生产面不保留旧 `PlatformStore` 或其 eager export、`WindowsEndpoint` 的
`get_job()`/`rerun_plan()`、旧 job schema/state/client，也不保留 Portal/query 的
`checkpoint_uri`、`reused_from_run` 或 `store://sealed` 资格。stage report 的来源和
bytes 只从当前 `by_stage` 的 immutable `ArtifactRef.producer_run_id`、member record
与 materialized CAS bytes 得到。旧 `platform.storage` filesystem writer/URI
resolver、discovery reconstruction builder、receipt 与 seal/confirm API 已删除。
Historical recovery 只保留 `validate_native_discovery_stage` 纯 validator；
`tools/migrate_historical_capture_input.py` 通过 `ArtifactStore.seal_tree()` 生成受控
diagnostic artifacts，不读取或写入旧 filesystem checkpoint 表示。

三类外部 observer 均只做查询：

- native observer 按原 operation/effect identity 执行精确 GET，不 create、upload、
  trigger、cancel 或打开 CAD；
- Git observer 在干净临时 clone 中验证原 candidate bundle、plan、base 和 branch，
  不 prepare、authorize 或 push；
- PR observer 仅以原 repository/base/head/candidate identity 调用 `observe`，
  不 create、update、reopen 或 comment。

observer 返回的完整 domain payload 必须沿用原 outbound 布局，使真实 handler 在
effect SAT 后直接消费而不重算或重发。精确 known 可建立 SAT；普通单次 404、空查询
或 `unobservable` 不证明 sent effect 不存在，仍为 UNKNOWN。只有适配器给出完整资源
范围、完整原 operation identity 和无遗漏搜索结果时，精确 absent reconciliation 才可
建立 `definitely_failed`。multiple、content mismatch、conflict 或其他不可唯一核定的
结果保持未决；所有情况均禁止盲重发。

reconciliation 追加 fact 与 metadata 前重读原 effect ledger；相同证据重复提交返回
原结果，不覆盖终态。它无 claim/fence、不创建 command、不授权发送；事务边界只覆盖
当前 `effect_intent`/`effect_fact` sequence，并对原 command、outbound/artifact identity
和 metadata closure 做只读重验。外部观察在事务外执行。失败保留原始响应、outbound、
payload 和 workspace，不能因解析、seal、metadata 或 fact 写入失败而清理。完成后原
command 仅在尚无可复用唯一 `StageDecision` 时作为中断命令由共同 engine 安全恢复，
并在 unresolved effect 消除后取得普通 execution claim；精确 input-transfer-pending
观察只允许上述受限续传 claim。已有终态决定则按当前事实复核复用，不强制新 claim。后到的旧 fence response/timeout 只能追加审计证据，不能改写 reconcile
已建立的终态。

### 灾难恢复

备份绑定固定发行、配置、安装身份及源目录清单。整个维护窗口持有同一排他锁：先关闭准入、排空并停止受支持的 Linux/Airflow 写者和 Windows 原生任务，再对保持运行的 PostgreSQL 建立一致快照，完成文件封存、加密归档、清单核验和收据。受支持的启动、对账及切代操作不能在该窗口内重新启用写者；前后状态相同或数据库 advisory lock 本身不能证明这一点。

恢复清单只接受 `solidworks-to-urdf.recovery-manifest/v7`，来源选择只接受
`solidworks-to-urdf.recovery-selection/v2`，目标计划只接受
`solidworks-to-urdf.recovery-target/v3`，快照工作进程使用 v5 协议。旧恢复清单、
来源选择、目标计划及更早协议在创建状态目录、解包或写入目标之前明确拒绝；保留原件，
不迁移、不兼容重解释。目标计划必须绑定清单摘要，以及同一 selection、canonical
fact-root 和来源范围摘要。

平台与 Airflow 共用数据库的全表、完整目录、角色、成员关系、权限、配置和 sequence
状态，以及冻结原件、发行、工作区重建材料与秘密文件，均须进入同一可核验恢复计划。
数据库先建立并持有同一只读可重复读快照，再从该快照生成 canonical typed 根闭包。
根闭包至少包括 `run_admission.request` 与 `command.request` 内嵌的完整 artifact
引用（含尚未创建 command 的未来阶段输入和 dispatch conf）、
`command_input_artifact`、`effect_intent` 的 outbound、`effect_fact` 的 payload 与
reconciliation metadata、尚无 decision 的 `stage_candidate` output/result、已决
`stage_decision` 的 result、FailureEnvelope evidence，以及未决 claim、未封存故障
workspace 和 typed `retained_locations`。每个引用都必须命中原不可变 CAS object；
缺项、摘要漂移或引用类型不明时 fail closed。

同一选择驱动来源前后清单、复制、封存和恢复复核。共享 CAS object 只复制一次；只有
完整 typed 根闭包证明没有任何事实引用的孤儿才可排除。未知 effect、未决 candidate、
已授权但响应未知的 outbound、失败或取消证据以及未封存 workspace 均不得当作垃圾删除，
也不得因尚无 StageDecision 而省略。恢复后必须保持原 ArtifactRef、StageResult、
candidate、effect ledger 与 claim/fence 身份；未知外部结果仍须经过精确 observation
或 reconciliation，不能由恢复动作重发。未决活动的排空门仍然有效，不能借归档绕过对账。

旧 `request/run/stage/operation/run_event` 五表的删除只能在维护排空与所有支持写者
停止形成的一致性边界内执行。切换回执不得由任意文件 hash 或自报 `verified` 代替：
它须绑定真实 source database、platform schema、精确 DDL/ACL、每张表的完整 rows、
未决 command/effect/native 关联、逐成员大小与 SHA-256、成员摘要集合的 aggregate
digest，以及同一回执对应的独立 restore/读取复核结果和切换操作身份。错源、缺表、
遗漏未决事实、rows/DDL/ACL/digest 不符、维护快照后并发变化、缺失 restore 证据或
回执自报与原件矛盾均拒绝。

固定 archive schema 与 evidence schema 分别为
`solidworks-to-urdf.legacy-lifecycle-archive/v3` 和
`solidworks-to-urdf.legacy-lifecycle-evidence/v3`。receipt 拒绝缺项、额外字段与旧
v1 数据，精确字段为
`schema_version/archive_sha256/cluster_system_identifier/database_name/database_oid/
migration_role/platform_schema/platform_schema_oid/evidence_sha256/manifest_sha256/
server_version_num/database_encoding/database_locale_metadata_version/
database_locale_provider/database_locale_provider_source/database_collate/database_ctype/
database_icu_locale/database_icu_rules/database_collation_binding/
database_collation_binding_source/database_collation_version/
database_collation_version_source/database_collation_actual_version/
database_collation_actual_version_source`。
cluster 身份取 `pg_control_system().system_identifier`，database 与 schema 身份取 OID，
host、socket 和 port 只作为连接 locator。archive manifest 逐成员记录 `kind/bytes/sha256`
并覆盖 `evidence.json/platform.dump/schema.sql/toc.txt`，且 manifest、evidence 和 archive
分别与 receipt 的对应摘要绑定。

PostgreSQL 14 从源 `pg_database` 读取 encoding、`datcollate` 与 `datctype`，provider
明确记录为从 PG14 database-default libc 语义推导的 `libc`。非 C locale 必须绑定精确
`pg_collation` libc 定义，并使 recorded 与 actual collation version 相等；稳定 C-family
locale 无精确 named row 时，才可使用 PG14 default catalog collation OID 100 并记录该
推导。PostgreSQL 16 的 provider、ICU locale/rules、binding、recorded version 与 actual
version 也必须按 catalog 原值记录。未知 provider、缺项、版本漂移、非稳定 C 的缺失版本或
无法复现的源 locale 均拒绝。

归档和复核使用 `search_path=pg_catalog` 以及固定 UTC、ISO/YMD、postgres interval、
hex bytea 和三位额外浮点输出。`schema.sql` 是 platform 与 Airflow schema 的完整 plain
schema-only dump；receipt verification 必须在维护窗口内重新生成 live schema-only SQL，
与归档逐字节相同，再比较整个 evidence。SQL literal、comment、default、constraint、
index、trigger、policy 与 view definition 不得经 schema 名替换或普通化；schema 名统一
由 `pg_control_system` search path 下 PostgreSQL 自身 deparse。PG14 plain dump 外部
随机 `\restrict`/`\unrestrict` token 只允许在严格配对和文档边界核验后作比较规范化，SQL
literal、comment、default、constraint、index、trigger、policy、view definition 与 dump
内容不得改写。独立 restore 的 `initdb` 与
`createdb --template=template0` 必须使用源 encoding、provider、locale、ctype、ICU
locale/rules 及版本参数，不能硬编码 UTF8/C 或 `--no-locale`；restored identity 的全部
locale 字段与 actual version 必须等于源，否则拒绝。比较 schema catalog、functions、
types、sequences、relations、Airflow relations、unresolved facts 及完整 schema/ACL SQL。
relation row evidence 使用显式 `COLLATE "C"` 排序以获得稳定摘要；测试另行证明源默认
collation 与显式 C 排序确实不同且 restore 后源默认顺序恢复，单行、多行与多字节值不得在
两种环境间改变接受结果。archive 前后 live evidence 变化或 restore 仅在单行/多行之间
出现不同接受结果均拒绝。

`apply.sh --legacy-archive-receipt <receipt>` 必须先由同一合同验证该 typed receipt，
再允许 schema 删除；仅检查“receipt 文件 hash 是 64 hex”不构成保全。删除不得使用
`CASCADE`，任何遗漏 view、grant 或其他依赖必须显式失败并保留目标。该规则不允许为
迁移方便保留永久旧 Store 或静默遗失未决事实，也不扩大部署及现场权限。

真正切换在一个 serializable 事务内完成：持有维护 `flock`，锁住 PostgreSQL
catalog、platform/Airflow 全部表以及相关 namespace/rewrite/procedure 行，再次以
locked live evidence 验证 receipt、archive 与 independent restore。事务把 receipt 身份
写入本地 `description.legacy_*` settings，只在事务内收窄到 migration role，然后执行
schema/views/roles 并核对精确 canonical relations、唯一允许函数、无额外 user type 或
sequence；任一错误整体回滚。archive 创建、receipt 发布和 cutover 均是本次操作的新文件
与原子替换，不覆盖另一 cluster/database/schema 的已有 receipt。

正式部署数据库版本由 `install_postgres.sh` 和 `recovery.py` 的默认 PostgreSQL 14
约束决定。PostgreSQL 16 的 CI/pgserver 结果只证明带 `postgres16` 标签的测试范围，
不扩展正式支持版本；任何仅依赖 16 的 catalog 行为、目录结构或 SQL 必须拒绝。最终
发行必须在部署目标的 PostgreSQL 14 上重新执行 archive、independent restore、
wrong-target rejection、fresh apply 与 cutover，并以实际 `PG_VERSION`、server version、
encoding 和 collation identity 绑定证据。

状态闭包包含固定配置、全部小型 install-records、snapshots 和 derived，以及当前和直接上一代 journal 实际引用的精确文件。journal 必须通过正式记录契约，逐条追踪 promotion receipt、from/to、release manifest、步骤 evidence/digest 和 evidence-map 的 source/snapshot；校验身份、路径、摘要、类型及权限并按原路径恢复。promotions 不递归复制，历史密文 `backup.tar.enc` 不归档。更早代记录仅作为元数据保留，不授予历史运行或回滚资格；缺少证据不得激活，普通回滚需要已排除密文时必须在副作用前拒绝。finalize 启动服务前重新核验完整 typed journal 与清单、目标计划、恢复收据和 marker 的同源绑定。

MVCC 快照不能代替非事务 sequence 和文件的一致性保护。秘密保存在受控加密归档中，外部解密密钥独立保管；日志不输出秘密值。封存验证逐成员读取完整解密流并校验路径、类型、大小、模式和 SHA-256，不创建第三份完整解密树；重复、缺失、额外、链接、特殊类型、截断及解密失败均拒绝。容量按实际选中数据、数据库估计、目标文件系统及安全余量计算，备份与恢复分别核算，不能写死现场容量。

备份输出必须是本次操作原子取得的新目录。创建动作绑定操作身份与目录身份，后续写入和清理均重新核验该归属。既存目录、链接、早期校验失败、并发竞争失败和缺少归属的请求，不得改写回执或删除任何已有文件。已封存结果不因后续日志、标准输出或清理失败而改写为未执行；收据分别保留备份结果、清理结果及维护停止状态。

恢复先验证归档、清单、引用及目标计划，再向符合契约的空目标恢复数据库与文件。部分恢复失败保持关闭准入，保留已发生动作与失败证据；不能把部分目标当作全新目标重试。成功后的只读核验须幂等，不重复导入、覆盖文件或启动任务。

恢复 runtime 的解释器、包身份、依赖角色入口、`description` 入口与
`pip check` 必须使用专用 probe environment。该环境从 caller 复制非 Python 运行上下文，
只删除 `PYTHONPATH`、`PYTHONHOME`、`VIRTUAL_ENV` 并设置
`PYTHONNOUSERSITE=1`；`PATH`、`LD_LIBRARY_PATH`、`SSL_CERT_FILE` 等保留。解释器和
`pip check` 使用 `-I -B`，并要求 executable/prefix/base_prefix/CPython 3.12 身份、
安装包 source digest、wheel/member inventory、Airflow 与 description entry 均来自恢复
出的虚拟环境。真实缺包必须使 `pip check` 非零；不得因 caller `PYTHONPATH`、user-site、
test fixture 添加的依赖或环境清理而放宽。

完整恢复成功须证明真实数据库消费者、Airflow 配置与密钥、发行与工作区、既有 Windows
任务定义、ACL、令牌、配置和安装身份均可用，并核对未决 native operation 与外部发布
事实。文件复制、数据库导入、服务替身测试或健康检查单独通过，均不能替代真实双端恢复
与端到端验收。

Windows 恢复回传实际读取的 state 对象，由 Linux 按唯一排序紧凑 UTF-8 JSON 加 LF 规则重算摘要；不信任回传的期望摘要，不改变任务语义身份哈希。状态传输保留嵌套结构和类型，bundle 与传输 JSON 都拒绝重复键。

<a id="exchange"></a>
## 原生交换协议

Linux 是工程任务的唯一发起者。Windows 接口只提供健康查询，以及 canonical native
operation 的 create、upload、get/status、resume、cancel 与 ACK；Capture 仍以
prepared/Discover producer 校验、sealed archive/receipt 消费和独立
`admit_capture`/`verify_transfer` 完成产物与传输资格。旧
`POST /v1/jobs/<run_id>/cache/release`、cache import confirmation、cache-release
receipt 与旧 job confirmation 不属于生产接口，必须删除且不得保留 fallback。接口不能
接受任意代码、主机路径、仓库或机械参数默认值。

有副作用调用绑定 operation/effect identity、参数摘要、发行身份与契约摘要；查询只返回
已确认身份与状态。契约领域拥有 `solidworks-to-urdf.resource-policy` 机器定义，限定
上传文件数量、单文件/总长度、解包大小、容量和超时，摘要进入请求；页面、Linux 准入与
Windows 传输消费同一固定策略。工程传输包含闭合文件清单、长度及 SHA-256；Windows
验证完整性后原子接纳。Discover result 绑定工程清单、主装配、实际配置、native
observation、selection、ruleset 与 prepared bytes；Capture result 绑定该 result、
同版 native requirement、prepared inventory 与原生运行环境。Linux 对这些绑定逐层复核。
Discover 与 Capture 可以各自传输并 materialize 自己的封存输入 closure。前者使用
SourceSet archive，后者使用由同一 SourceSet 原件血缘派生的 prepared archive；
SourceSet 与 prepared 分别保存不同摘要。每个 operation 必须绑定原请求、上游
artifacts、同版 native tool 和本次 archive；同一 operation 重试先观察原结果，
UNKNOWN 不得触发第二次 CAD 执行或静默创建新 operation。Windows 继续只有一个 native
operation runtime 和一个 CAD resource owner，workspace 是 operation-local 临时物化；
本版不新增共享 source cache、兼容链或第二生命周期。未来内容复用只能是可丢弃字节
优化，不能改变工程资格或替代阶段独立 admission。

### 原生三角形事实

当前 Capture 使用唯一的 `native-triangles/v1` 事实契约。原生会话沿
`occurrence.GetBodies2 → GetFaces → GetTessTriangles(True)` 读取已保存配置的实际
body、face 和三角形；每个 face 的直接返回按原始次序保留，不切换备用几何链。
旧几何与准备收据按原 schema 保全，不能重标版本、填充新字段或自动晋升为当前输入。

原始三角形每项为九个有限 `binary64` 数值，单位为米、坐标位于 component-local。
封存字节保留原值与顺序，严格核验格式版本、数量和文件长度，拒绝截断、尾随字节、
非有限值和无效三角形。body/face inventory 与连续、正数量的 face spans 必须完整
覆盖同一 raw stream，不允许重叠、遗漏、未知归属或把读取失败转为空几何。

每份事实绑定 occurrence、配置、原始 placement、body/face、source scope 和冻结
字节。由本次 source scope、实体类型及原生遍历索引派生的局部对象身份，必须保留
规则和字段级来源 warning；不能冒称原生持久引用，也不能要求所有对象具有辅助
persistent reference。scene 保留实际 placement，不能为简化转换替换为单位矩阵。

`prepare_prepared_capture` 只绑定已封存 prepared 的 root/configuration，不打开
COM；Freeze 保留初次准备职责。Capture 读取 scene 与内存几何事实后，按唯一会话
所有权协议释放 CAD，再在 Windows 离线执行原值投影、惯量规范化、raw/STL 序列化
和封包。这部分纯计算不再持有 CAD，会话释放不代表 Windows 作业已经结束。STL 从同一 raw
三角形派生，保留 component-local 坐标，明确法向量和浮点编码的派生来源，不能重复
应用 occurrence placement。原生错误及已取得的 partial facts 保全，不被离线异常替代。

原生事实只在 `evidence/raw/native_capture_facts.json` 封存一份，由 source manifest 和
Capture artifact 绑定其字节；`evidence/capture.json` 保留必要投影与其他证据块，不再
内嵌完整原生事实。Generate 与独立 Verify 读取同一原始成员，各自执行匹配、派生与
复算；原始事实缺失、被改写或与投影冲突时，按既有工程和完整性规则拒绝。

独立 consumer 从 raw 字节、face spans、原始 placement、物性和基准读数复算实际
输出，核验源绑定、单位、坐标、对象归属、覆盖、朝向及派生 warning，不能只比较生产者的投影。
raw/STL 一致性证明本次原始三角事实到交付的派生一致性，不自动建立原生 BRep 或
光滑曲面误差界；几何资格按实际证据陈述，不借用历史证明或未验证预览。
`docs/quality.md#geometry-equivalence` 的 50 µm 双向距离标准仅用于声明适用 CAD 的
独立参考回归，不是生产或正式发布前置；当前 CAD 的独立工程检查继续执行。

### 原生外观读数与交付

`appearance_raw` 保留 document、occurrence、body、face、feature 和 display-state 的原生观察、实际 API 与参数、原值、配置、对象绑定及读取错误。document 与 face 的无参 active-configuration 属性只在实际 active 与实例 referenced configuration 相同时采用；各顶层、祖先与 leaf owner 使用自己的配置和显示状态。不得以多个旧层颜色相同替代未读覆盖层。

`GetRenderMaterials2(3, names)` 与 `GetRenderMaterialsCount2(3, names)` 使用同一个表示 exact owner display state 的 typed `SAFEARRAY(BSTR)`。NULL 与计数分开保留；只有成功计数为零才证明该状态无 assignment，正数或未知计数不能解释为 absent。成功枚举的行数必须等于同状态计数，不使用旧无参 Count 或 fallback。

文档实体通过真实 `IModelDoc2` QI、实际 `GetPathName` 与 active configuration 建立身份，不要求文档具有实体 persistent reference。其他材质实体与同次几何遍历的 `native_reference_index` 使用实际材质所属文档的原生持久引用，并绑定 exact occurrence、配置和显示状态；Generate 与 Verify 各自从原始事实独立 join。`component_document` 的材质与索引使用同一真实 Part owner extension。组件 API 返回的 occurrence-context 面先由目标 Part extension 的 `GetCorrespondingEntity2` 取得 underlying Part face，body 由同一 extension 的 `GetCorresponding2` 取得 underlying Part body；feature 从该 Part face 取得，再由同一 Part extension 读取唯一持久引用。这里只适配原生身份，实际 occurrence 几何、坐标和 face spans 不换源，首读结果复用。 该引用须在同一 Part extension 通过 `GetObjectByPersistReference3` 原生解析，状态为 `swPersistReferencedObject_Ok`（0）且对象支持预期 `IFace2`、`IBody2` 或 `IFeature`，再作为有效索引；无效、抑制、删除、空结果或错误类型保留原生错误，不登记成功绑定。解析按 Part 文档、引用和类型复用一次，不增加 Part 几何遍历。Generate 与 Verify 对参与实体独立核对该次原生解析及引用关联，并执行 raw join；测试专用标签、`ISldWorks.IsSame` 或自报“属于 Part”均不能建立资格。输入对象的装配上下文不决定方法 receiver；不新增 parent/root assembly receiver 选择，也不转换到装配引用或保留第二套备用匹配。按官方外观层级，assembly-owned 材质使用实际 assembly owner 上下文中的组件实例；face、feature、body 和 part assignment 使用 Part owner。装配材质指向非 occurrence target 时明确拒绝，不转换、提升或猜测其作用域。不能以非空引用、颜色相同或索引覆盖全组件证明身份，不保留无消费者的转换及重复引用副本。参与对象绑定的原生读取或 mapping 错误、错误 component／source document、未绑定或真实歧义均拒绝；没有参与该绑定的辅助诊断不另设零错误资格。同一 native feature identity 可有多个面的完整 `faces[]` footprint，重复记录的完整 footprint 不一致须拒绝，不对不同对象或不同覆盖取并集染色。

有效外观按有文档依据的对象层级解释，不任意规定继承。已证明参与的单色现代 material 必须绑定 exact owner/state 与实际交付面，`ColorForm=1`，无参与纹理或 surface finish。通用规则 `appearance.primary_color_colorref` 版本 `v1` 对非布尔、范围为 `0..0xffffff` 的 `PrimaryColor` 整数计算 `R=(v&255)/255`、`G=((v>>8)&255)/255`、`B=((v>>16)&255)/255`，`alpha=1-Transparency`，不取最高字节作为 alpha。九值属性的 alpha 同样由其原生透明度派生。保留原值、规则版本、结果和字段级 derived warning；独立验证自行计算。位顺序依据固定成熟导出器的同 API 实现交叉证据，不能冒称官方文档明确给出了 byte packing，也不从手工参考回填。

若真实文档身份、exact display state 和完整面覆盖证明现代单色 material 参与当前交付，可将配置范围内但不参与该状态的 legacy `GetTexture` pointer 保留为诊断事实，无须读取无关图像与 UV。空 `MaterialName` 本身不证明 absent。真正参与交付的纹理仍须有资产字节、UV、对象绑定和消费能力；缺证、颜色冲突、对象或配置错绑、必要读取失败均明确失败，不能默认色或 warning 放行。辅助诊断不另设所有 API 零错误资格。

交付按已采集 face triangle spans 无损分组为一个或多个 visual/material，完整覆盖原始面且不增删几何。Verify 从原始读数和实际输出独立核验绑定、RGBA、覆盖、网格及来源 warning。visual 或展示材质名称参考差异的非阻塞许可不扩大到颜色、纹理或实际对象归属。

### Native target identity

端点 `/health` 的顶层 `install_id` 是 Windows 安装目标身份的一部分；`ready` 只描述
当前调度就绪，不是身份，也不得因 `unresolved>0` 而禁止对原 operation 的只读观察、
对账或恢复。expected `NativeTargetIdentity` 只从已核验部署登记取得，不能由环境变量、
当前 health 自报或迁移后的 URL 自行晋升为新的权威。系统继续消费该既有登记，不新增
target registry。

经过核验的 endpoint preparation 在 `endpoint-identity.json` 顶层记录
`target_identity={schema_version,install_id,pipeline_id,target_id}`，并保留原有
`install_id`、`config_digest` 与 release identity。写入 install record 时将该完整
record 原样放入 `ends.windows.target_identity`；`target_identity.install_id` 必须与
Windows end install ID 相同，`pipeline_id` 固定为 `solidworks-to-urdf`，`target_id`
由 common typed schema 重算。`schema_version` 固定为
`solidworks-to-urdf.native-target-identity/v1`，`target_id` 是
`{schema_version,install_id,pipeline_id}` canonical record 的 SHA-256。runtime target
不回写 platform snapshot；install record 的
`snapshot.path/digest` 继续逐字节绑定既有 platform snapshot。

每个原生请求携带 expected target；每个响应回带同一 target；original operation 在
服务端保存 admitted target。服务端在入站 upload 时同时核对请求 expected target、
operation 已保存 target 与当前运行 runtime target。只在客户端 health 查询之后再发起
upload 不能关闭 TOCTOU；任一不一致都必须在 input admission、executor 启动及所有
create 之前拒绝。错误 target 不产生新 operation，也不能被归为原 target 的 absence。

端点 URL 可以合法迁移；同一 install 在重启后仍可按原 operation 恢复。变更 state root、
错误路由、health 不可达或 404 都只使原 operation 保持 UNKNOWN/未决，不能据此创建、
重派或把原 target 标成 absent。tool/release identity、平台 origin claim/admission 与
Windows native worker owner 分别记录和验证，任一相同或不同都不能替代其他身份。
同一 install 的恢复仍受单一资源所有权与排他锁约束；复制或自报 install ID 不自动取得
启动、上传、恢复或 native execution 权限。

状态、健康及漂移查询不得安装、下载或修复持久工具链文件。已绑定的工具缺失或不可执行时如实报告，不通过自动安装掩盖漂移。

选择闭包的 `virtual_components` 单独记录内嵌虚拟组件：`state=embedded_virtual_component`、`binding=owner_embedded_geometry`、`kind=native_embedded_virtual_component`。每项保留 `IComponent2.GetPathName/GetImportedPath/IsVirtual` 的直接读取依据，要求 `is_virtual=true`，并绑定可达上传图中的 `owner` 及其 SHA-256、精确 `instance`、可空的 `owner_anchor`、引用 `configuration`、`session_document_path`、`session_temp_root` 和正整数 `native_api_pid`。原生端从实际 SolidWorks API 读取进程 ID，并核对它与会话根 `swxNNNN` 的进程编号一致；文档须位于该受控会话的 `VC~~` 内，路径形状本身不是归属证明。不得携带非空作者导入路径或读取错误，不得与 `imports` 中的 `IC~~` 缓存共享实例身份。后续会话可以产生不同临时路径，但须重新读取原生对象，并以相同所属装配字节、实例与配置对应封存记录；原生采集和独立验证双向核对当前对象与封存清单，拒绝遗漏、额外对象和冲突绑定。临时文档不进入上传文件图，Linux 只验证封存证据，不访问 Windows 临时目录。

Discover 由 Windows 执行一次精确 native observation，再由 Linux 依据已绑定 ruleset
形成 prepared source package 与 `result.json`。Capture 核验并消费该 result 与 prepared
inventory，只将受约束的相对路径定位到已登记工程副本，不重新推导结构。Discover result
只保存 selection、原生对象、规则、证据和 prepared bytes 身份，不另存完整机器人模型；
不得在采集结束后从配置或结果倒造新的计划。

原生记录保留直接读数、API 状态、原生引用、配置、保存状态、环境及原始几何。厂商矩阵布局、单位和原始数值先如实保全；释放 CAD 后从固定事实离线投影到 SI，变换使用行优先 4×4 齐次矩阵及列向量乘法，保留原值到结果的规则和来源 warning。工程模型生成、闭包比较、独立工程验证、诊断解释与报告渲染在 Linux 完成；格式校验和封存摘要计算属于传输职责。

当前方向处理不引入基于右手定则的新正向选择或符号推导规则，也不引入屈曲、外展等功能动作推导正向。输出遵循 URDF 既定格式和坐标约定，并按[《MimicVerse URDF 工程标准 v1》](https://hcnd2fgke1pv.feishu.cn/docx/N3iSdpYxGo151UxgBz6csCthnSb)检查表达一致性；这些约定不能用来绕过 CAD 已有有向轴及有效作者声明。斜轴保留真实机械轴，不吸附到主轴。名称、全局向量分量或镜像标记不能单独决定正向。

方向规则的 `status`／`direction_relation` 是共享审计结果，不是独立的作者正向证据。
v3 审计未建立作者正运动语义时，即使该共享值为 `local_class_aligned`，Discovery、
scene、Generate、独立声明验证和质量 warning gate 仍必须产生唯一的对象级
`direction.semantics_unresolved`。旧基于
`status/direction_relation != local_class_aligned` 的 success-warning 抑制旁路必须实际
删除，不得以共享状态短路或跨模块互相核销 warning。
warning 记录原始 CAD 轴、运动类别、规则、delivered axis 和未解决状态；它是当前非阻塞
来源 warning，不改变已交付 axis／xyz／rpy／limits，也不改变
`machine_qualification=unqualified`。模型来源、URDF、质量报告、预览和 PR 使用同一
warning；只有独立、可复算的作者正向证据才能关闭该 warning。

本次导出的方向以已保存 CAD 有向轴及有据的坐标转换为依据。参考测试保留与适用手工 fixture 的符号差异，不强求通过规则生成相同符号，也不得复制参考值。原生轴缺失、读取失败、无效向量、坐标转换或轴线／对象绑定错误，以及不符合原生非零运动证据仍是阻塞缺陷。

实际驱动器／编码器方向定义属于单独的硬件资格。驱动正命令、编码器正计数与关节物理转向分别记录，不默认三者同号；命令类型、控制模式、零位和适用配置须明确。若声称已经验证实物方向，必须绑定 CAD 执行器 occurrence、原生机械轴、传动关系及实际执行器身份，并提供方向声明的来源、版本和验证依据；本次导出流程不以补齐这项硬件资格为前提。电机安装、局部坐标正轴、轴伸出方向或定转子中心关系本身不能证明驱动／编码器极性。

CAD 唯一工程来源的边界保持：本次冻结 CAD 缺少实际驱动／编码器方向定义时，保留已有原生轴，并以来源 warning 明确该硬件语义关系未验证，不因此阻塞本次导出及端到端交付。外部驱动配置及既有硬件验证记录可只读用于查证和提出补充方案，不能未经确认就成为生成器的新工程输入。若需补入方向声明，须依据独立工程证据建立受控的新 CAD 输入并重新冻结；不得从参考 URDF 抄入符号，也不得以修改硬件配置或原件来伪装已有原生事实。

必须分别记录无向轴线、有向轴原值和 CAD 作者的运动定义。参考轴端点次序或某一局部轴分量不自动证明作者的正动作；已有原生有向轴及运动定义则必须先读取、检查并保留。几何轴本身无向而 CAD 提供有效的作者方向声明时，按已定义的声明语义、原生对象和轴绑定还原正向；例如有效 `dp.joint.axis_sign=-1` 不能被一律忽略，也不能被重复应用。对作者声明的读取解释与对输出表达的规则修正分别记录，不能把二者混成一次任意翻转。

参考系的原点、轴、正负侧和运动类别须有绑定到本次 CAD 对象及配置的证据。当前仅使用已读取并验证的 CAD 参考系及必要的坐标表达转换，不从名称、髋部连线、质心或几何主轴另造人体前／左／上并据此重选方向。输入不正交、共线、近退化、语义不明或超差时明确诊断，不能强行补轴或用任意新基准替代。任何确需的坐标修正仍须符合已有契约、保留原值与规则，并经独立复核。

非零运动观察用于独立验证 CAD 运动语义、对象归属和坐标转换，不用来另行定义哪个动作应当为正。当前不以观测点速度投影、任意质心、名称分类或试算符号表推导输出方向。此前的右手定则及功能投影试算保留为历史分析，不进入生产规则，也不作为方向验收通过依据。

原生方向或基准表达偏离已有规范时，发出对象级 warning，按有依据的明确规则纠正导出表达；可确定修正的规范偏差本身不阻断。标为 `undirected_axis_line` 的参考轴只证明轴线位置；缺少有效原生有向轴或已绑定作者声明时，当前保留为方向未解决，不通过新增正向推导规则补齐。缺少必需 CAD 对象绑定、真实轴线或 CAD 运动证据仍是独立验证缺陷，不能任意选符号或选择相反方向后仅给 warning 放行；这与单独的硬件正向资格未建立不同。

每项方向修正记录原生对象、配置、原始轴／基准／零位、适用正运动语义、规则 ID 和版本、偏离原因、转换关系与结果。坐标重表达或符号变化必须联动处理轴、适用的 frame／origin、零位偏置、位置边界及实际受影响的速度／力矩和采样语义；保留原始 CAD 读数，不能只翻 XML axis。独立验证按 CAD 和规则重建预期，证明转换前后相应物理姿态及运动一致，且输出正向满足规范。规则不得由手工差异反推成逐关节常量；应如实表达经确认的驱动／编码器与机械运动映射，不能只改 URDF 轴以掩盖未知或错误的硬件极性。

仅改变广义坐标符号且保持同一零姿态时，映射明确为 `q_out = s * q_cad`、`u_out = s * u_cad`，其中 `s` 为 +1 或 -1；负号情形的范围为 `[-upper, -lower]`。有来源的速度、加速度和广义力矩标量相应变号，功率和物理运动保持不变；非负幅值上限不因符号变化而取负，不补造缺失驱动参数。此重参数化不需要旋转网格、移动原点或改变零姿态。若另有坐标或零位转换，必须单独给出完整映射并验证，不能混入一个符号。旋转线性关系 `R^-1 * (s * u) = s * (R^-1 * u)` 必须成立，不能用“符号在不同坐标系相乘”解释相反的物理方向。

每次方向反号（`s = -1`）必须给出可定位到具体关节的显式 warning，即使它是有据且保持物理运动等价的规范化，也不能静默处理。warning 记录原生身份、原始轴及其参考系、输出轴及其参考系、反号原因、规则 ID／版本／适用证据，以及坐标、范围和其他实际受影响字段的前后值；不适用或缺失的字段如实注明。报告、URDF 来源记录、预览和 PR 中的反号 warning 必须一致。该 warning 描述实际发生的规则变换，不能仅因反号就宣称 CAD 存在工程错误，也不能代替方向依据或掩盖未解决冲突。

自动规范化作用于导出解释，封存 CAD 原件和失败证据保持不变。确需修复原生 CAD 工程时，在保留旧版的受控新副本上按具体工程依据修改并重新冻结；修改后再取得完整原生证据，不能把旧运行解释为新结果。参考单元测试和回归测试按适用 fixture 比较命名、方向、限位、位姿和非零运动，保留 diff 与语义分类，不将固定手工结果作为真实端到端或发布的必经输入。生产验收独立依据当前 CAD 和规范，坐标变换、拓扑、零位、几何或原生运动的真实错误仍阻塞，不能归为可忽略的表示差异。

交付配置的证明要求：封存选择中的原件摘要与读取后的原件摘要完整相等，顶层原件读取前后均为只读，实际 `IConfiguration.Name` 与已选交付配置一致。采集读取冻结 CAD 已保存配置中的原生事实，不保存或修改原件，不以原生状态标志归零作为接纳资格。`GetSaveFlag` 保留原始诊断值，不作为原件是否已保存的判据；不得用它掩盖原件变化或配置证据缺失。推导与独立验证分别从原始证据检验这些条件。

原生采集从当前配置的实际实例拓扑建立参与装配清单，分别保留实例和共享文档身份。读取前核对文件范围、配置、session 文档所属进程、受控临时根和对象绑定；归属未知、冲突或不可读均拒绝。一次采集保存可供后续离线复用的原始几何、轴线、配合、位姿、物性及必要归属事实，同时保留实际读取错误。所有读数绑定本次原件字节、配置、原生对象、工具和自有会话，不能以生成结果或手工参考补齐失败读数。

读取完成后核对实际拓扑中的实例、路径、文档类型、配置、深度、抑制状态、存储及来源分类，并核对源文件字节与 session 依赖集合。合法文档复用和作者抑制保留既有语义；按文档去重不能丢失实例证明，也不能将合法复用误判为拓扑冲突。依赖文档保留实际读取的只读状态，不为制造一致读数改写其状态。实际读取失败、原件变化、配置冲突、无效几何或对象绑定错误均如实拒绝，不能用诊断标志、warning 或历史成功替代必要工程事实。

原生采集封存原始事实与失败原件，并按既有排他和所有权协议完成资源结算、释放 CAD；未知外部结果继续通过原 operation 对账，不能登记虚假成功。后续坐标转换、模型生成、报告和独立工程验证从固定事实离线执行，保留原值、规则、来源 warning 与字节绑定；纯计算或验证修复复用同一份原始事实，不因此再次启动 CAD。独立验证对照原始发现、选择闭包、源摘要及实际字节复算，仍核验必需且适用的工程约束，不另建工程模型或状态权威。

原生 API 错误保留调用名、返回值和完整整数位码、可解析的枚举名、SolidWorks/API 版本、文档/配置、失败操作及已取得的引用信息。位码不是错误数量。缺少具体引用绑定时明确记为未定位，不能据此宣称缺失文件数、原件损坏或工程师交错材料。路径诊断记录主装配、工作目录、搜索路径以及原生返回的存储/解析路径，秘密与无关工程信息不得外泄。阶段按实际执行归属记录；原生打开位于 `discover`，不能记成纯文件冻结失败。

完成状态只能在结果封存并可读取后公布。所有结果文件只能通过已登记清单访问。缺失、多余、损坏、跨运行或不完整文件组均拒绝；临时文件和部分采集不能伪装成成功证据。活动含操作、对象、可用计数、序号和时间，只表达已观测事实。

接纳前按机器策略预留磁盘容量，容量不足明确拒绝。Windows 缓存只在 Linux 确认完整导入相同清单、无在途引用和自有资源已释放后清理，保留终态和导入确认收据。Linux 封存原件、交付与诊断默认不自动删除；显式保留策略也不得清理仍被运行、发布或批准引用的证据。

<a id="resources"></a>
## 资源所有权

Windows 端点以操作系统互斥锁保护一个 state root，以独占租约串行化所有 CAD 操作。发现与采集共用该资源域。重试、健康查询、独立脚本与服务重启不能另建所有权域绕过互斥。

每个自建 CAD 进程在原生读取前登记：主机与启动身份、会话、PID、进程创建时间、可执行文件身份、创建者进程身份、随机所有权令牌与操作身份。创建时间统一为 Windows FILETIME epoch 起的 UTC 100 ns 整数；读取或换算失败不能生成可匹配身份。PID 或程序路径单独不能证明所有权；禁止连接现有交互式 CAD 会话。COM 工程读取前核验实际绑定的服务器 PID 与创建身份等于登记进程，绑定无法证明或转移时立即停止，不因已启动进程而信任 COM 注册。

存在未知、仍被活属主持有或无法核验的 CAD 进程时拒绝获取资源，诊断保留身份核验失败的原因。清理残留要求持久记录匹配、原属主确证退出、目标进程身份完整相符。终止前在同一进程句柄上重新核验创建时间和身份，终止及等待退出均使用该句柄，避免 PID 重用。

原生 API 工作在受监督的独立进程中。取消先请求有界协作退出；COM 阻塞且超时后，监督器只能终止已证属的工作进程，确证其退出后按账本释放其 CAD 残留，不能跨线程强行中断 COM 或按进程树/程序名批量终止。

超时、取消和进程退出走同一释放逻辑。不能证明资源身份时停止自动清理并报告；租约过期不能代替所有权证据。账本更新持锁、原子持久化，崩溃后先核验再恢复。

<a id="delivery"></a>
## 交付格式与摘要

完整交付是自包含目录：

```text
manifest.json                  # 主体清单、主体摘要、契约与策略身份
input/                         # Freeze/Capture 封存的请求、来源与原件闭包
evidence/                      # Discover/Capture 原生观察与实际证据
model/robot.json               # 唯一规范化模型
urdf/robot.urdf
meshes/                        # 交付视觉网格
tool.json                      # 源码、包文件、依赖及执行角色身份
reports/verification-certificate.json
reports/verification-execution.json
```

每个原生文件、观察、实例、物理读数和网格都必须能够追溯到主装配、配置和对应来源。完整上传清单留存所有上传字节的身份；模型交付只携带经原生绑定核验的依赖闭包。完整上传原件另外保存在平台冻结存储中。

`input/` 与 `evidence/` 由 Capture admitted tree 原字节提供；Generate 只增加 `model/`、
`urdf/`、`meshes/` 和 `tool.json`，Verify 只增加两个 verification 文件。Publish
在复制后重验这些 bytes 并生成 `manifest.json`，不得从当前 worktree、手工目录或
第二模型重建内容。原生收据和 transfer manifest 在封存后不改写；完整制备包清单、
原生选择清单和平台 ArtifactRef 分别核验，不能互相代替。

主体清单覆盖 `input/`、`evidence/`、`model/`、`urdf/`、`meshes/` 与 `tool.json`
的全部文件。`manifest.json` 和阶段自有的 `reports/` 收据不进入主体；它们由各自
schema、receipt 与 artifact identity 验证，不能把自身纳入摘要形成循环。Verify
certificate 绑定 Generate/Capture pair、verifier、contract、policy 与 subject；
execution receipt 绑定该 certificate。Publish receipt 再绑定 publication subject、
publication plan、候选 commit/tree 与 Git/PR effect。

运行投影、活动、维护 reconciliation metadata 和 PR effect observation 保存在
平台 facts/CAS 中，引用交付摘要，不进入模型主体。所有阶段的失败诊断由该阶段生产、
Linux 编排登记，包含可用输入/产物摘要、失败检查、原生环境及部分证据清单。验证失败
保留失败的 verification certificate/execution receipt 和可安全读取产物用于未验证
预览；这些文件位于运行的独立诊断目录，不是成功检查点，不能占用完整交付的命名空间。

原生传输与发布前重验使用同一个收据及字节验证边界：工具原始字节、原生阶段事件、
transfer manifest、run/Freeze identity、源记录、原生发现和实际 input/evidence
逐项闭合。拒绝缺失或部分收据、未知 schema、非法运行身份、错误事件顺序/时间/检查
内容、artifact manifest/字节冲突，以及重新封存后仍自洽但属于另一输入的声明。
原始收据由 ArtifactStore 读取或由平台传入不可变 bytes，不产生两套验证逻辑。

清单路径为相对 POSIX 路径，按 UTF-8 字节排序。JSON 使用 UTF-8、键排序、无多余空白、禁止 NaN/Infinity；模型 JSON、报告与 URDF 数值使用与 locale 无关、可精确往返 binary64 的最短十进制，统一零值格式，不截断有效位；每条文件记录包含路径、字节长度和 SHA-256。摘要对规范序列化对象求 SHA-256，并以对象类型区分用途。清单及其父目录不允许符号链接、junction、路径逃逸或大小写冲突。

Linux 维护人员对已登记 Verify command 使用
`description stage <verify-command-id>`，执行同一正式 StageApplication 流程。
尚无阶段决定时，入口取得执行权，从 canonical facts 物化 Generate/Capture artifact
closure，执行验证并提交正式决定、verification certificate 与 execution receipt；
这些结果按既有契约参与后续发布。已有决定时，入口核验绑定并返回原封存结果，不自动
重新执行原始验证算法。验证读取不改写输入证据，但命令执行会写入阶段业务事实。

真正隔离复算须沿既有受控诊断入口绑定 `DiagnosticReplayScope`，分别记录历史生产者、
本次工具及输入闭包，不登记正式资格，也不能由普通后继消费；不能把正式命令描述为
只读诊断。不提供直接 check/rebuild 任意目录的第二 CLI。新建候选从 Freeze 重新接纳；
发布和恢复始终走同一 StageApplication/effect/fact 流程。

<a id="publication"></a>
## 发布协议

每个 Linux 配置目标固定硬件标识、仓库和 base。同一仓库、硬件目标和用途使用一个受管理的候选分支与 PR；不同用途分别登记候选，不能用检查预览交付覆盖工程候选。候选分支只由平台更新。base 是工程批准后的集成基线，平台不自动合并 PR、创建模型发行 tag 或宣称批准。

发布意图在外部写入前持久登记，绑定运行、用途、目标、前置 base/head、上一候选主体及本次有效载荷。读取失败、分支来源不明、远端主体不匹配、用途冲突或前置基线变化均拒绝更新。首次交付必须确证不存在候选；未知不能等同于不存在。

发布在独立干净工作区执行，核验仓库身份及配置目标，完整重验交付；复制后再次重验。Git 属性必须保持交付字节，实际 staged 和 committed blobs 必须等于验证有效载荷。远端候选 head 更新同时要求已确认前置提交的条件写入与 fast-forward，禁止非 fast-forward 写入或覆盖人工提交。base 在写前和最终确认时重检；base 变化时，即使候选 commit 已推送也不能报告发布成功，须保留已发生结果并对账。仓库检出 refspec 不影响按明确远端 ref 获取基线。

Publish 使用两个独立 effect：`publish.branch / git_ref_update` 与
`publish.pull-request / pr_upsert`。每个 effect 在调用前封存完整 outbound、
target、expected state 与 expected digest，取得当前 command/claim/fence 的 dispatch
authorization 后才执行。Git outbound 保存 candidate bundle、publication plan 和
exact request；PR outbound 保存 repository/base/head、candidate、subject、title、
body digest 与已有 PR identity。

推送后只读确认真实 remote commit/base，再创建或更新 PR，并核验 repository、head、
base、commit、verified subject、certificate、title 与 body。全部确认后封存
`publication-receipt.json`；任一 effect UNKNOWN 时 stage 保持未决。恢复通过原
effect intent/outbound 和只读 Git/PR observer 完成，不重建 publication、不重新
push、不重复 create/update。candidate bundle 与 publication plan 是原恢复证据，
不得作为垃圾删除，也不得替换为从当前 scratch 重新生成的文件。

已合并 PR 的后续交付从确认包含该候选的当前 base 建立新基线，沿受管理分支 fast-forward 并创建下一评审 PR。已关闭且未合并的候选禁止自动重新开启，由目标负责人处理撤回原因。

同一已登记 Publish command 的恢复可在重验原 effect 与证据后复用已确认结果。新运行按自身已验证有效载荷和固定基线发布；远端基线变化必须形成绑定新基线的新 Freeze 请求，最多复用父 originals，不能继承旧下游成功。原发布收据与已发生的远端结果完整保留。
