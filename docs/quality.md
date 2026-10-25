# 质量规范

独立验证针对完整、不可变的交付主体建立结论。发布要求所有必需且适用的检查通过；缺失证据、未执行必需检查、无效不适用声明或主体改变均阻断。系统没有质量绕过开关。

## 验证原则

验证器只读取封存输入、原始观察和实际产物，独立重建成员、拓扑、基准、轴线、限位与物理预期，再检查规范化模型、XML、网格及独立进程中的 MuJoCo 加载。生成方的采集计划、模型和报告是待核对的声明，不能成为预期答案。

验证不会重新打开 CAD，也不是第二次物理实验。它证明封存观察与交付的规定一致性；CAD 的实物真实性与规格适用性由[机械规范](mechanical-handoff-spec.md#review)中的工程责任确定。

检查策略定义固定门与按对象展开的门，以及适用条件、容差和证据要求。报告保存策略摘要、完整预期检查集合、实际结果、测量值、单位、阈值和证据引用。预期集合由策略固定门与原始证据的按对象展开形成；报告检查集合必须与其完全相等，缺失、重复和额外未知检查均失败，不能由生成器或调用者删减。

<a id="gates"></a>
## 必需检查

| 检查域 | 独立证据与通过条件 |
|---|---|
| 身份与完整性 | 文件清单闭合、摘要正确、schema 有效；工程、配置、采集计划、原生运行和同版工具绑定一致 |
| 原生来源 | 原生 API/build 与环境已资格化；每个依赖及物化导入件绑定封存原件、所有者和实例；无包外或未知来源 |
| 机械解释 | 独立判定声明存在性；有声明时独立解码，确证无声明时独立重建完整配合集合；模式、成员和结构一致 |
| 对象与拓扑 | 每个纳入实例恰属一个刚体，其余显式未分配；名称唯一且合规；唯一 `base_link`、连通无环树，模型/XML 集合一致 |
| 配置与状态 | 正确引用配置、活动实例和抑制状态；原生重建成功；读取及恢复有效；原件和采集副本的字节保持不变 |
| 坐标与零姿态 | 有限 SI 齐次变换、正交右手基准；独立从原生根基准计算零姿态 FK，实际产物的放置一致 |
| 关节与轴线 | 类型、两端、完整原点、正向、限位一致；轴线绑定正确实例中的独立重读原生参考轴或圆柱面 |
| 运动一致性 | 从原生刚性归属与接口端点独立计算非零姿态的逐叶实例运动；核对随动、组内相对变换及接口连续性，覆盖适用的单关节与关联组合 |
| 物理来源 | 每次读数明确选择范围、配置、单位、坐标轴、质心参考点、张量约定及覆盖标志；未知约定不能采用 |
| 刚体惯性 | 独立重算完整选择集合，核对原生两次直接读数及到刚体基准的变换；质量、COM 和完整惯量与模型一致 |
| 整机核算 | 原生所选刚体与独立测量的未分配部分完整划分整机；质量精确闭合，COM/张量在规定容差内，无重复或未解释缺口 |
| 数值与物理合理性 | 质量正、主惯量正、三角不等式成立；COM 与几何边界合理，惯量不超出几何半径界限 |
| 网格 | 原生覆盖记录完整；实际三角形有限、非空、尺度与位姿一致；无损分片保持顺序、原始记录、header 和数量 |
| 可选独立预算 | 已提供预算必须可解析、有效且适用，整机质量与最大外形尺寸在区间内；未提供时有据标记不适用 |
| 消费者加载 | 新的隔离进程加载实际 URDF 与网格，使用锁定 MuJoCo；保留预期刚体和可动关节，输入前后摘要不变 |
| 报告绑定 | 根据同一主体与策略重算完整确定性报告，保存报告与重算结果完全一致 |

原生读取以每个实例的实际引用配置为准，不借用共享零件文档的当前配置。网格从当前装配实例的实体和曲面取得，保持相关 COM 父接口存活；完整批次前后核对实例路径、配置和抑制状态。临时选择与配置恢复须有可核验结果。不可读活动基准、未知抑制状态或读取失败不能被静默省略，失败保留环境、会话和部分观察。

声明存在性是封存原生观察中的 `present / absent / unreadable / unknown`。`absent` 只在交付配置中成功完成原生声明存储查询并证明不存在时成立，必须保留查询对象、接口、返回状态及结果；缺少记录、读取失败或空的损坏载荷不是不存在。`present` 必须支持格式版本、可完整解码、适用于交付配置，全部持久组件/基准/轴引用可解析，且与原生几何和可比较工作限位一致；否则失败。推导和验证分别执行语义核对，只有存在性契约共享。

运动预期先从当前原生实例、配合端点和刚性/柔性求解事实建立，再检查模型及 XML 的非零运动；生成计划、输出父子表和输出自身的 FK 不能提供预期答案。采样在来源定义的适用范围内覆盖允许的正反方向与关联组合，保留姿态、单位、坐标基准和逐实例结果，采用本规范的位置与方向容差。分析窗口只用于其明确声明的模型运动检查，不补齐工程范围或建立实机安全资格。缺少必需连接见证、配置冲突或不支持的机构仍失败。

检查策略从原始对象、有效范围及独立重建的连接展开确定的姿态和逐实例检查集合，不能由生成计划选择测试对象或姿态。单侧范围只采允许方向，固定关节及有据零范围按策略记录适用性和资格；缺失范围不能伪装为零范围或不适用。故障复现姿态作为附加诊断绑定同一来源，不替代必需样本或改变通过范围。

运动比较使用共同的有据参考系，核对运动叶实例、其余实例应保持的关系、刚体内相对变换及接口连续性。依据规范进行方向重表达时，应按已记录的输入到输出坐标映射比较等价物理运动，并单独验证输出正运动语义；不能要求原始分量或参数符号逐字相同，也不能忽略没有规则依据的符号翻转。

验收包含保持相同零位几何与物理闭合的错误连接反例，须由来源端点或逐件运动门拒绝，并指明受影响实例和原生依据。解析运动检查只建立交付与封存观察的一致性；完整原生机构自由度、驱动可达性及无碰撞资格仍需对应工程依据。

语义反例保持原始证据不变，可一致改写生成计划、模型、URDF 和报告并重新封存；独立验证仍须拒绝错误归属或连接。只证明文件被修改、摘要不符或输出彼此不等，不能证明机械独立性。具体正负例见[开发指南](../CONTRIBUTING.md#mechanical-regression)。

发布对有效载荷及 Git/PR 的验收由[发布契约](contracts.md#publication)定义，不能以生成成功、原生完成或报告绿标代替上述验证。

<a id="physics"></a>
## 物理权威与覆盖

每个刚体的质量、COM 和惯量权威是 SolidWorks 对其完整选择集合的一次直接读取，采集保留连续两次读取作为重复性证据。验证器自行重算选择集合，逐项核对；模型采用第一读数及严格的坐标变换。逐实例组合保留为独立分区审计，不得替换权威读数或用于调整总量。

选择集合由有效父级覆盖提供者与未被覆盖的叶实例组成。父级覆盖只在一个刚体内整体替代子树，不能分摊到运动链。实例有效值保留原生范围、坐标和覆盖标志；无覆盖标志但装配上下文与零件文档读数不同，保留两者和未证实的原因，不推定覆盖来源。

所有有效实例被划分为模型选中集合与显式未分配集合，二者不重叠且覆盖整机。未分配集合由原生独立测量，不能从整机减去模型计算后充当证据。原生刚体直接质量之和加未分配质量，与整机直接读取在规定绝对容差内闭合。完整张量和 COM 在共同坐标系下核算，包含未分配部分；缺少所需物理读数仍失败。

物理数值始终保留非对角项。原生张量声明 `IMassProperty2.GetMomentOfInertia(0)` 的已验收标准带符号约定、关于质心的参考点及表达轴。验证按正确旋转和平行轴定理换算，不采用未知 fallback 数组或参考导出的惯量。

CAD 值、规格值与实测值须明确来源和适用性。材料缺失、有效覆盖或装配上下文差异不能建立物理真实性；数学闭合通过也不能授予该资格。

<a id="numerics"></a>
## 数值判据

容差用于以下特定比较，不能互换或因单个工程失败而放宽。整机闭合是额外的严格守恒门，使用同一采集配置与会话中的第一组权威读数、完整分区和稳定求和；重复性或一般独立比较通过不意味着闭合通过。守恒残差超限仍失败，不以较宽的读取容差豁免。模型与 XML 保留[规定的数值精度](contracts.md#delivery)。张量误差以最大元素绝对差计算，张量尺度取参考张量最大元素绝对值与 `1e-20 kg·m²` 的较大者；一般 `absolute + relative × scale` 是允许误差。

| 比较 | 允许误差 |
|---|---|
| 关节/基准位置、轴线径向偏差 | `5e-5 m` |
| 基准旋转、轴线夹角 | `0.05°` |
| 有向 XML/声明轴分量 | 绝对 `1e-12` |
| 单位轴范数 | `1e-9` |
| 一般独立质量 | 绝对 `1e-12 kg` + 相对 `1e-6` |
| 一般独立 COM 分量 | `5e-5 m` |
| 一般独立完整张量 | 绝对 `1e-15 kg·m²` + 相对 `1e-4` |
| 原生刚体重复读取：质量 / COM / 张量 | `1e-9 kg` / `5e-7 m` / 相对 `1e-6` |
| 直接读数到模型：质量 / COM / 张量 | 相对 `1e-12` / `1e-9 m` / 相对 `1e-9` |
| XML 到规范化模型完整张量 | 绝对 `1e-15 kg·m²` + 相对 `1e-10` |
| 整机质量闭合 | 绝对 `1e-12 kg`，无相对容差 |
| 整机 COM / 完整张量闭合 | `1e-3 m` / 相对 `1e-2` |
| COM 超出刚体几何边界 | `1e-4 m` 的三角剖分余量 |
| 惯量/几何半径界限 | `1%` 的三角剖分余量 |
| 退化三角形 | 面积 `≤1e-14 m²` 定义为退化；逐原生网格及其对应完整交付分片集检查：退化数/总三角形数 `≤1%` 且退化面积/总面积 `≤1e-6`，两者同时通过，禁止跨对象摊平；零分母失败 |
| 交付网格分片 | 每片不超过 `199999` 个三角形，保持全部原始三角形记录 |

有效变换必须具有齐次末行、单位正交旋转与正行列式；schema 和检查策略规定这些数值合法性门。非有限值、零/负质量、非正主惯量和不满足惯量三角不等式始终失败。

近零体积网格须有绑定具体实例的异常几何依据。原生曲面片记录正面积却返回零三角形和零 facets 时，可保留精确警告，包含实例、面及原生面积；其曲面几何完整性不获得资格。实体无网格、三角数据损坏、非空曲面缺失必要事实均失败。

对同一次冻结 CAD、配置和实例，每个 body 由事前原生几何事实选择唯一采集 provider，最终汇入同一 triangle stream、face span 和 STL writer。ITessellation provider 的原生顶点、facet、fin 与边关系共同约束其交付网格；三角形必须由对应 facet 的有向顶点环生成。限定平面 BRep provider 则从原生 plane、完整有向边界与 trim 事实派生网格，不能伪造 ITessellation facet／fin／cofin 身份。两类事实以明确标签分别验证，再共同核对实例归属、完整覆盖、实际 STL 字节、来源 warning 与朝向；不得建立第二工程模型或复制 scene transform 权威。binary32 序列化、坐标转换及派生值保留原值、规则和 warning，由 Linux 从原始事实及实际交付字节独立复算。显示缓存的三角形数量不构成几何保证，也不拼接两套 tessellation；最终 `50 µm` 表面比较及其他必需验收保持。

IEdge 的 GetStartVertex／GetEndVertex 标签不提供可预测的天然方向，原始标签、vertex 身份和读数继续保全。平面 provider 采用 `native-coedge-adjacent-edge-vertex-incidence-v2`：从完整 `GetNext` 闭环及相邻 edge 的原生 vertex 引用交集建立每个 coedge 的有向拓扑顶点 V，不按坐标距离选择身份。参数方向按 `native-curve-sense-vertex-incidence-v1` 独立建立：`ICurveParamData.Sense == ICoEdge.GetSense` 时，curve 参数序与 coedge 拓扑序相同，否则反序。curve 端点、同一 edge 的 `Evaluate2` 端点和 line projection 共用该固定排列；consumer 从原生引用和两个 sense 重算，不用最小距离排列覆盖已知方向。原生 trim、curve 及两端 tangent 的有向点积必须严格为正。依据为官方 [StartPoint/Sense](https://help.solidworks.com/2021/english/api/sldworksapi/SOLIDWORKS.Interop.sldworks~SOLIDWORKS.Interop.sldworks.ICurveParamData~StartPoint.html) 与 [ICoEdge.GetSense](https://help.solidworks.com/2019/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.ICoEdge~GetSense.html)。身份关联和几何构造均保留版本及来源 warning。

`IFace2.GetArea` 按[官方 API 语义](https://help.solidworks.com/2019/english/api/sldworksapi/SOLIDWORKS.Interop.sldworks~SOLIDWORKS.Interop.sldworks.IFace2~GetArea.html)返回近似面积观察值，不承诺与独立边界公式逐位相等。保留其原始值、API 和具体 face 身份，读取失败、非法类型、非有限值或非正值仍拒绝；不得覆盖为重算面积。平面域的几何面积及完整覆盖由冻结 plane／line／trim、精确闭合边界和独立三角形覆盖证明建立，派生面积保留规则与来源 warning。原生近似面积与派生面积分别展示，差值仅作可追溯诊断，不按差值放行几何、不另造面积容差，也不以跨算法逐位相等作为准入条件；同一原始观察在重复记录间仍须精确绑定。

原生曲线端点与 `Evaluate2` 的一致性采用版本化 `native-curve-endpoint-float32-operation-v4`：对参数序端点 `c`、同一 U 的求值点 `v` 和曲线另一端点 `o`，允许界 `v4(c,v,o)` 由 `pair(c,v) + pair(c,o)` 组成，其中 `pair` 是两个有限 XYZ 点逐坐标 binary32 邻接界之和的最大值。必须独立要求 `L∞(c,v) <= v4(c,v,o)`；不得用 tessellation 投影点 `p` 替换 `c`，也不得把 `Evaluate2`-到-`p` 的诊断误差当作该门。producer 与独立 parser 使用同一规则；该界不放宽 U 域、持久引用、拓扑端点、fin/cofin 或同 U 证据，也不允许把任何端点跳过验证。

原生曲线与 `IEdge` 端点采用版本化 `native-curve-topology-endpoint-set-v4`：分别保留参数域 endpoint-set 和实际 edge endpoint-set 的原始点，以 `max_abs_coordinate` 独立复算 direct 与 swapped 两种完整映射误差，二者共用既有 50 µm native tessellation projection 界，并保留两种完整接受状态。该集合检查证明几何覆盖，不通过最近距离或唯一匹配猜测方向；双匹配保留 `complete_match_count=2`。普通开放边须有不同的端点；[GetEndVertex](https://help.solidworks.com/2024/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IEdge~GetEndVertex.html) 允许闭边两端引用同一原生 vertex。共享端点资格只在 scope-local 身份解析完成后建立，不能从 raw alias 猜测；要求该 vertex 读数完全一致、edge-local 参数域可分辨非零、两端 `Evaluate2` 在既有原生 roundtrip 界内闭合，且完整 fins 至少提供两个不同的原生求值点和非 collapsed 参数段。读取失败和不同 vertex 的近似重合不能冒充共享端点。同一 `ICurveParamData.Sense` 用于有据的方向解释和参数提升，不选择或替换原生 closest U。原始参数、端点、sense、实例及拓扑归属各自保全。

方向关系依据 [SOLIDWORKS `ICurveParamData.Sense` 官方契约](https://help.solidworks.com/2020/english/api/sldworksapi/solidworks.interop.sldworks~solidworks.interop.sldworks.icurveparamdata~sense.html)。edge row 中保留的原生 tolerance 元数据必须如实记录来源、原值或读取失败；它不决定 closest U 的资格，不改变投影允许界，也不产生未实际使用 gap 的警告。

原生 tessellation fin 端点采用版本化 `tessellation-edge-projection-v1` 绑定。令 `s` 为原始 tessellation vertex，`p/u` 为同一次 `IEdge.GetClosestPointOn(s)` 的原生返回，`v` 为 `IEdge.Evaluate2(u,1)` 返回点。全部 fin endpoint 共用同一几何条件：`L∞(s,p) <= 5e-5 m`、`L∞(p,v) <= 5e-5 m`、`L∞(s,v) <= 5e-5 m`；三者必须分别由原始点独立复算，不能仅以两个中间界代替第三个整体界。U 有效域、128 ULP 参数身份、原生响应与 packed success、Source scope、拓扑和覆盖保持严格；不按旧 API 的成功状态或 U 是否靠近边界选择另一几何预算。独立曲线参数端点 `c` 与其同 U 求值点的 v4 数值一致性仍单独验收，不能把 `s` 或 `p` 代入 `c`。v3 在同点身份、cofin 反向坐标等有独立必要性的比较中保持，不充当此 closest 来源的 `p/v` 几何门。删除此链的 `p/c <= v4` 普通门、tolerant gap 救援分类、相关 open-edge identity 与未实际使用的 gap warning；不新增回退、默认值或逐 fin 例外。交付网格顶点保持原值；上述 L∞ 点投影检查不等于已证明最终双向 L2 曲面距离，最终 50 µm 曲面上界、拓扑、覆盖和朝向资格仍须另行完整证明。

原生 `GetPersistReference3` 的原始字节可能由同一 owner 的不同实体类型共同返回，因而不能单独充当全局实体身份。正常可区分对象继续使用按实体 kind 域分离的 typed persistent reference；不得把这类原生身份降级为派生值。仅当同一 body 内某一 kind 的全部对象返回同一个 body-scope reference、该 reference 不能识别具体 face/edge/vertex 时，才采用版本化 `native-scope-entity-id-v1`：该 identity 明确标记 `persistent=false`，绑定 source document identity、configuration、occurrence、body、object kind/index，并以原生 area、精确 binary64 bbox、tessellation coordinate/topology 或 edge endpoint/curve/相邻面证据生成唯一摘要。冻结 part 的唯一且可解析 reference 只作为映射 witness，不接管生产几何。原 raw reference、alias 审计和对象级 `cad_component_persistent_reference_aliased`／`cad_scope_local_identity_derived` warning 必须保留；scope-local 不得用于跨运行解析、恢复或宣称 persistent。若同 kind 出现 unique 与 alias 混合、scope 摘要重复/缺失、owner/configuration/body 不符或映射不是双向一一对应，必须 fail closed。跨 kind raw alias 仍须显式审计，不能用 alias 放宽拓扑归属、对象绑定或覆盖检查。其序列化 owner 绑定 kind、component、body scope 与 typed identity；不携带无法由消费端可靠重建的 body-local edge 枚举索引。producer 与 consumer 按同一 typed identity 排序，跨 body 或原始 edge 顺序变化不得改变审计含义。原始网格点到最近点的 50 µm projection 只证明既定几何关系，不建立持久对象身份，也不能替代上述 scope-local 来源、映射和归属核验。

fin endpoint 的唯一 U 来源为版本化 `native-u-closest-point-parameter-v1`：`IEdge.GetClosestPointOn(s)` 同次返回 `[PointX, PointY, PointZ, UParameter, NotUsed]`，前四项必须为有限原生值；最后一项为未使用槽，不作工程判据。严格要求 U 位于同一 edge 的有效参数域，保留负参数和周期参数的原值及既有绑定；不以 clamp、插值、另一次反求或其他对象的 U 替换。`Evaluate2` 必须 packed 成功，并由上述三条既有 50 µm 点投影条件独立接纳；原生曲线端点 v4、cofin/同点 v3、覆盖、Sense、Source scope、有效 warning 和网格字节绑定各保留真实语义。删除 `GetParameter` 二次反求、成功优先／失败 fallback、闲置状态字段与兼容开关；历史失败原件保全，不拼接新旧响应。域外、非有限、错误绑定、packed 失败或任一必要几何条件不闭合均拒绝，不切换另一 U。失败记录须定位具体对象与 predicate，保留原始 s、同次 p/U、域、Sense、edge/face 引用、Evaluate2 原始 payload/状态，以及所检关系的度量、误差、阈值和规则；声明的 tolerance 原生事实另列，不能以派生 evidence 为 null 推断原生状态。该来源依据 [SOLIDWORKS `IEdge.GetClosestPointOn` 官方契约](https://help.solidworks.com/2024/english/api/sldworksapi/solidworks.interop.sldworks~solidworks.interop.sldworks.iedge~getclosestpointon.html)。

native edge 的 U 覆盖按每个实际 face 使用实例分别严格验收。已由原生端点身份或合法缺端点事实证明闭合的 edge 使用 `native-closed-edge-fin-partition-v1`：完整 fin 集合先建立单一无向拓扑 cycle，保留原始 fin 顺序及方向，确定性派生遍历；再用全部已观测 U 节点的循环邻接关系，要求每个相邻参数段恰被一个 fin 覆盖。不能跳节点、重复覆盖、分叉、断环或含第二圈。`u_min`／`u_max` 在已证闭边上表示同一 seam 参数节点；不同拓扑顶点仍分别保留。128 ULP 只用于已观测参数身份：全部观测按两两关系建立连通分量，每个分量须两两等价；检查不能因排序后不相邻或 ULP 跨幂边界而漏掉跨簇关系，非传递桥接拒绝；同一 fin 的原始两端是否 collapsed 必须与分区结果一致，seam 不得抹掉原始非零段。分区不接受实际域外的原始 U，不借边界身份将其钳位；collapsed fin 仍须通过既有几何及 cofin 检查且贡献零 U 覆盖。至少三个可区分参数节点才获得方向无歧义的分区；两节点应报告证据不足，不能断言 CAD 损坏。参数邻接不依赖 U 长度，非均匀参数化中超过半域的合法段仍可通过。开放 edge 仅在独立核验的 edge-local／trimmed 参数域内取直接区间，不因 underlying curve 闭合或周期而选择补区间。底层域到 edge 域必须先通过既有 transform；不得混用或 clamp 原始 U。该分区以 `native_closed_fin_partition_derived` warning 定位具体 component、body、edge、face 与派生字段；它不代替 face／cofin／sense 朝向证明。同一 face 多 coedge occurrence 尚无可独立区分的事实时不合并放行，属于当前支持域限制。

面方向以可读的 `IEdge::EdgeInFaceSense` 为主证据；SolidWorks 2026 中 `IEdgeInFaceSense2` 为只写属性，其带错误记录的不可用结果可保留为诊断，但仅在它成功返回布尔值时才要求与主证据一致，主证据缺失或不合法仍拒绝。fin/cofin 跨面时允许使用各自 face-local 的 tessellation vertex ID；独立验证须确认 reciprocal cofin、不同面、反向 U、端点 U 对应以及反向端点坐标落在 v3 点界内，不得仅凭 vertex ID 相同或不同作结论。

原生 tessellation schema v10 将 `ITessellation.GetFinCoFin` 的严格整数 `-1` 定义为开放 sheet body（`body_type=1`）的 boundary sentinel：记录必须保留原始 `cofin_raw_id=-1`，并将归一化 `cofin_id` 写为 `null`。只有唯一 incident face 的 sheet edge 可包含 boundary fins；该 face 具有 facet 时，edge 必须包含实际 fin，每个 fin 都须携带该 sentinel、指向该唯一 face，并由逐 face 的 U interval 证据证明完整覆盖。solid body（`body_type=0`）不得使用 boundary semantics。具有两个 incident faces 的 edge 不得包含 boundary fin；其实际 fin 都必须指向同一 edge fin set 内的非负 cofin，形成双向 reciprocal pair，并满足不同面、反向 U、端点 U 与坐标对应。允许同一 shared edge 以多段 reciprocal pairs 表示；实际 fin 任一端为 `null`、类型不是严格整数、cofin 不在该 edge set、非 reciprocal、在 shared edge 出现 `-1`、在 open edge 出现非负 cofin，或 incident face 数不为 1 或 2，均 fail closed。

限定平面 BRep provider 采用版本化 `native-planar-brep-v2` 规则，适用域为 sheet body、恰一个原生 face、原生 plane、完整 loop inventory 中恰一个 outer 且无 inner、完整有序有向 coedge 闭环、全部原生直线及合法 trim，以及经有界构造后闭合、简单、严格凸的 polygon。原生 body／face／edge／vertex／curve 与冻结 source、配置和 occurrence 的身份及所有权必须闭合；loop／coedge 不能仅按坐标排序或近似合并生成。`FaceInSurfaceSense=true` 表示 face normal 与 surface normal 相反；`ICoEdge.GetSense=true` 表示与 underlying edge 同向。原值、原生 API、规则版本、派生身份和字段级 warning 全部保留。

provider 选择发生在任何 `Tessellate` 之前。不符合限定几何域且已获原生事实证明时，采用一次 ITessellation；原生读取失败、非法类型、身份冲突或未知不伪装成“不适用”。不能按名称、对象序号、面积、显示三角形数量或一次 tessellation 失败切换 provider。构造或交付精度失败也不能冒充几何域外而触发另一种网格路径。原生 API 明确的未使用槽不充当工程判据；例如 `ICoEdge.GetCurveParams` 的索引 `8..9` 不提供有效 sense 或 curve type，相关事实应由正式 API 读取。

先对完整边集合读取并校验 `ICurve.IsLine`，存在非直边时不进入仅适用于直边的解析。已读事实的非法类型、读取失败、身份冲突和有向矛盾始终硬拒，不能伪装为“不适用”。binary64 观测不严格共面／共线时，不再只因非零残差退出平面域：按下述显式构造及完整上界核验。超过总界、构造后退化或拓扑不成立均拒绝，不切到另一 provider 重试。选择原因及同次已读事实保留在 `scene.notes.geometry_provider_selection`，不作为另一份几何权威。

平面 BRep 证明的主体是本次原生 API 实际观测并封存的 raw binary64 nominal 模型。对拓扑顶点 V，以原始法向绝对值最大分量对应轴作精确有理投影 Q，再显式转换为 Q64 和最终 binary32 STL 顶点 S；选轴不得使用归一化后的舍入法向。分别向外计算并记录原生 plane residual、V→Q 构造位移、Q→Q64 舍入、已定身份对应的跨 API 位移、完整 tangent 段位移及 Q64→S 编码位移，总和须不超过 `50 µm`。原生 plane residual 与构造位移部分重复，保守保留，不用减项放宽。切向投影采用原始非零 tangent 的有理投影，核验整个有限段和两端。原值、构造规则、位移和字段 warning 均保留，consumer 从原始事实独立复算。该界针对 `observed_native_nominal_model`；不宣称厂商未公开的 hidden-kernel 精度或实物误差资格，不新增经验 EPS 或逐对象特例。

平面读取链在调用前把 `IFace2.GetLoops` 的元素显式绑定为 `ILoop2`，把 `GetFirstCoEdge` 和每次 `GetNext` 的结果显式绑定为 `ICoEdge`；`QueryInterface` 失败不退回默认 dispatch。首次方法解析后复用 pywin32 的实际 `mapFuncs` 缓存，要求 `Evaluate2` 的 resolved DISPID 为已核 type library 的 `17`，实际 Invoke 继续使用同一缓存和同一已绑定 dispatch。保存请求 IID、成员、实际 DISPID、调用类别和完整原始响应；独立消费者严格复核，未知／错误 DISPID 不得执行。`native-planar-brep/v3` 与求值规则 v2 记录这一契约。同名 `Evaluate2` 不代表同一接口或返回契约：本次 `ICoEdge.Evaluate2(u,1)` 按官方六项坐标／切向约定校验，不能按 `IEdge` 的七项 packed-status 约定截断或放行。

`ICoEdge.GetCurveParams` 的 U 仅绑定同一 `ICoEdge.Evaluate2`，不直接与 `ICurveParamData` 的 edge-local U 区间比较，也不凭 edge Sense 为它换号。直线覆盖由完整拓扑引用、有向闭环、同一 coedge 两端求值和上述有限段界共同证明；参数必须有限且区分两端，同一参数对应不同端点仍是硬错误。原值、原点和方向不改写；消费者复算同一几何与本地参数对应关系。`native_curve_domain_transform` 和 ITessellation fin 的 edge-local U 规则保持独立。依据为官方 [GetCurveParams](https://help.solidworks.com/2016/English/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.ICoEdge~GetCurveParams.html)、[Evaluate2](https://help.solidworks.com/2023/English/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.ICoEdge~Evaluate2.html) 与 [GetSense](https://help.solidworks.com/2025/English/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.ICoEdge~GetSense.html) 的各自定义。

对合格平面域以确定性有向 fan `[0,i+1,i+2]` 生成 `n-2` 三角形。独立消费者对 Q 和实际 S 分别证明顶点唯一、全边界严格凸、无交叉、有向面积正确、外边界完整一致、内部边反向成对，以及该固定 fan 的完整覆盖；不能只再次调用 producer 的 triangulator。定向、相交和非退化判据采用精确有理数，覆盖三个轴及正负法向。实际 binary32 STL 必须逐顶点、逐有向三角形绑定；配对顶点的最大 L2 位移通过同 barycentric 组合覆盖对应整块三角曲面。L∞ 点界和 U 的 ULP 身份不能冒称 L2 曲面证明。量化后的顶点合并、边界交叉、退化、翻向或拓扑变化独立拒绝，即使距离小于 `50 µm` 也不能通过；不得删除原生边或顶点以制造资格。

采用 ITessellation 的 body 其唯一 `IBody2.GetTessellation` 请求统一使用显式 `VARIANT(VT_EMPTY, None)`，对应 [SOLIDWORKS 官方 VBA 示例](https://help.solidworks.com/2020/english/api/sldworksapi/Create_and_Convert_Non-manifold_Bodies_Example_VB.htm) 的 `GetTessellation(Empty)`。这是一次 whole-body 主请求，不在失败后切换参数。请求原件保留恰好一个参数、`VT_EMPTY` 和空值；不得以裸 Python `None`、零长度 SAFEARRAY 或显式全 face 数组冒称同一 Variant 类型。完整原生 face inventory、引用、body 归属和后续集合闭包分别保留，不将未传入的 face 列表写入请求，也不在身份派生后改写请求。独立 consumer 严格核对请求字段、参数数量的整数类型及空值，拒绝旧请求变体。

采用 ITessellation 的 body 统一进行一次 analysis tessellation：在唯一一次 `Tessellate` 前设置并严格回读 `ImprovedQuality=true`、`NeedErrorList=true`，保留原有 `MatchType=0`、`NeedFaceFacetMap=true`、`NeedEdgeFinMap=true`、`NeedVertexParams=true`。同次请求规则与实际属性保留来源和执行身份；独立 consumer 对顶层及每个 body 的字段集、精确类型和值分别核验，不能因 Python 的布尔／整数相等而混同读数。analysis 模式用于工程读取，不在失败后切换模式再 tessellate，不另猜 chord／angle／width 参数，也不替代最终曲面距离验收。`Tessellate=false` 明确为原生失败，以该 API 为首错停止；可从同次对象记录原始错误列表、face 身份／面积和失败态数量作诊断，但不得把失败后的零数量或空返回解释为无 facet。错误列表不可读或原始返回为 `None` 时如实保全，不推成无错误，不依赖不支持的原生指针接口。

`GetFaceFacets` 与 `GetEdgeFins` 保留同次成功 tessellation、同一 typed face／edge 的原始返回。list／tuple 必须是不含重复项的扁平非负严格整数数组，未经递归展平、去重或排序的原值须封存；`GetFacetFins` 的原始返回同样须为恰好三个不同的非负严格整数，不能先展平再验收。嵌套数组、标量、未知或任一 COM 读取失败均拒绝；不增加另一套 tessellation 或备用网格。

`native-tessellation/v16` 对 ITessellation provider 从完整集合闭包证明狭义空集合。对同一 body 的全部原生 face，分别保留 `GetFaceFacets(face)` 原始 nullable 返回、对象绑定和完整 face inventory；全部成功数组中的 facet IDs 必须跨 face 互不重叠，长度之和与并集大小均严格等于同次原生 `GetFacetCount`。依据原生 API 返回的是对应 face 的 facet 子集，完整无重叠子集达到原生总数，才可证明其余成功返回 `None` 的 face 无 facet。此推导仅适用于严格 `body_type=1`、`Tessellate=true`、全部所需属性真实回读正确的情形；solid、缺失 face、错误归属、重复 ID、总量不足或超出均拒绝。保留原始 `None`，零计数和空数组明确标为按完整 body 闭包派生，记录规则与对象／字段级来源 warning，不能冒称原生逐字直返；consumer 从原始数组、inventory 和原生全局数量独立重算。

仅当严格 `body_type=1`，edge 的一或两个唯一 incident faces 均归属于同一 body，且每个 incident face 已由上述完整闭包证明 facet 数为 `0` 时，才允许该 edge 的空 `fin_ids=[]`。除同次 `Tessellate=true` 与所需 map 属性正确外，全部原始 facet→fin 关系也须完整、唯一归属，并与同次原生 `GetFinCount` 闭合。`GetEdgeFins` 成功返回的直接空数组如实保留；若成功返回 `None`，只有这些原生拓扑与全局闭包事实全部成立，才可依据 fin 必须属于其 incident face 的 facet 推导为空，并保留原始 `None`、具体规则和对象／字段级来源 warning。consumer 从原始 face／facet／fin、全局数量、face span 和实际三角形记录独立复算，不信任自报允许标志。此规则不要求或伪称读取了 `GetFacetFace`／`GetFinEdge`，也不增加反向 API 前置。边、端点、曲线、参数域、sense 及原生身份仍完整保留，不生成 fin、cofin 或虚假 U 区间，也不删边。任一相邻面具有 facet 时缺 fin、solid/未知 body、非法返回类型、COM 读取失败、缺失面、全局总量不闭合或归属矛盾均拒绝。无 facet 的原生曲面片继续保留既有的实例、face、正面积及精确 warning；空边不贡献 U coverage，不赋予该曲面片几何完整性、最终曲面距离或参考拓扑一致资格，其余几何、身份、覆盖及交付验收保持。

原始证据记录 immutable `raw/source_scopes.json`：file source 分开保存原文件与冻结 copy hash/path；virtual source 保存 path-derived identity、owner hash、`VC~~` session document/root 和 PID。包级验证须把 scope 与 discovery、scene、capture、geometry、dependency closure 和实际冻结字节交叉核对；允许 discovery 与 capture 使用不同的虚拟 session、root 和 PID；owner、实例、配置及 `VC~~` 相对身份必须一致，各自 session 的文档路径、root 与 PID 必须内部绑定自洽。producer、独立 parser 与 topology summary 使用同一规则；该规则不放宽 curve endpoint、U、surface、身份或 mesh 验收。

极短 fin 的两个端点经原生 128-ULP U identity 视为同一参数时，可采用版本化 `native-fin-u-collapse-v1`：保留两端原始 U、坐标、投影和求值证据，要求 reciprocal cofin、不同面、反向坐标 v3 一致、主 sense 相反、两端投影/Evaluate2/U 域证据有效，且几何端点跨度严格大于零并不超过 50 µm。collapsed fin 只保留为零测度拓扑段，贡献零 U coverage；其余非 collapsed intervals 仍须对每个实际面独立覆盖完整 edge 域。零跨度、部分坍缩、跨度超过 50 µm、cofin/sense 异常、证据不可读或依赖该 fin 补洞的情形必须 unresolved。fin 输入保持 exact schema；规则名、跨度和 warning 是从上述原始证据复算得到的归一化输出，不属于可注入的原始真值，parser 不得修改调用方输入对象。

<a id="qualification"></a>
## 警告与资格

所有非 CAD 原生直接读取的交付值须按[值来源契约](contracts.md#value-provenance)显示 warning。独立验证从当前 CAD、实际采用的规则和原始依据复算结果，检查规则适用条件、原值、转换和输出值。允许规则修正及其对象映射；禁止手工导出值、照结果硬编码及伪装原生读数。报告／URDF／预览／PR 的字段来源必须一致。

验收须包含删除必需警告、将派生值伪装为原生直读、警告指向错误对象或字段、算法摘要不符，以及向工程来源注入手工 URDF／网格／名称表／照结果硬编码符号或伪装机械限位的负例，并确认均被拒绝。同来源可聚合展示，但不得因折叠丢失字段覆盖。警告不替代缺失、冲突或读取失败等必需检查。

CAD 原名的格式、词根、缩写或重名违规只产生命名 warning，不单独阻断；生成器必须按固定规则修正。验收同时验证：全部输出名称符合对应类别规范；定义唯一；parent／child 等全部引用闭合；material 定义与引用一致；网格 URI、实际文件及清单一致；跨产物引用同名同义。添加非规范名、中文／非法字符、左右缩写、重复名、未知词根、实例重排、重复规范化和重跑用例，证明告警可定位、修正确定且幂等。输出非法名或断裂引用是实现缺陷，不能用 warning 放行；真实原生身份错误与命名违规分别报告。

报告将检查结果与资格结论分开。`passed` 只表示必需检查集合已通过；资格项在质量报告中以策略定义的 `fact_id`、范围及原生 `object_id` 唯一定位，使用 `established / unqualified / out_of_scope`，引用建立或未建立结论的证据；页面和 PR 使用同一身份，不以显示文本作机器键。模型一致性事实与 `engineering_position_range`、`drive_sizing`、`machine_qualification` 分别记录，前两项按关节、末项按交付主体定位。外部实物真实性或批准不能由自动项标记为已建立。阶段、检查和复用状态遵循[状态契约](contracts.md#recovery)。

| 保留事实 | 可继续的条件 | 未建立的资格 |
|---|---|---|
| 显式未分配实例 | 完整划分、独立读数及整机核算通过 | 整机 URDF 覆盖 |
| 显式材料赋值缺失 | 原生物理数值可读、合法且一致 | 材料与密度真实性 |
| 原生覆盖或有效实例差异 | 范围、坐标、来源标志与闭包有效 | 几何推导的物理真实性 |
| 声明的零范围、零 effort/velocity | 值有限、非负、顺序及可比较来源一致 | 可用运动或驱动能力 |
| 原生曲面片无 facets | 精确原生面积及对象事实完整，其余几何门通过 | 该曲面片的几何完整性 |

零值必须有明确来源，缺失不是零。禁止为通过检查编造质量、刚体附着、限位或对象。允许的规则转换应保持物理一致性并覆盖全部相关字段；不得丢弃真实启用的零范围。工程限位、显示／分析范围及缺失状态分别验证，手工数值不得补齐。用户授权的 `engineering_rotation_default_window/v1` 是显式非原生默认窗口：必须逐关节保留原始作者值和 native 缺失或读取错误证据，显示 warning，并将 `engineering_position_range` 保持为 `unqualified`；原生或非零作者范围优先，任何冲突仍阻断。

检查预览遵循同一工程来源与规则验收。对原生范围、方向和名称的有据修正须可复算；分析窗口须与机械范围区分，不能获得实机可达性或安全资格。缺失驱动如实显示；读取失败、冲突或不满足规则适用条件不能被默认值掩盖。规则存放在 CAD 外或产生逐对象结果，本身不构成失败理由。

`inspection_rotation_full_turn/v1` 的正例须证明完整原生观察下确无机械范围、旋转关节的预览窗口为 `[-π, π]`，且原始读数、工程范围缺项及来源警告完整保留。负例覆盖不完整／失败／冲突观察、移动关节、`engineering` 用途、误覆盖有效原生或作者范围（含真实零范围）、将窗口标为原生读数、删除缺项警告以及由该规则补写驱动值。按同一输入独立重算适用性和窗口，再检查实际 URDF 加载与非零运动；预览通过不能建立工程位置范围资格。

可选预算未提供属于不适用，不生成预算、警告或人工确认任务。平台列出实际不在自动覆盖范围内的工程事实，既不代签批准，也不把未读取的既有审批推断为未完成。

<a id="report"></a>
## 报告、预览与复核

质量报告的确定性部分包含主体、工具、策略、检查、测量与资格，禁止加入本次检查时间、运行 UUID、主机临时路径或随机排序。执行时间、活动和运行身份进入独立收据。相同主体与策略重算得到相同报告字节。

验证在只读交付上执行。解析器拒绝外部实体、路径逃逸和远程网格；消费者在新进程中执行，使用明确超时，不继承生成器状态或可影响模型的配置。验证和消费者读取前后均检查主体。

通过的预览加载绑定主体的实际 URDF 与网格，显示报告与资格。验证失败后仅允许显著标注“未验证”的诊断预览，附失败检查；缺乏可安全解析的模型时只展示诊断。预览控件不能产生验证结论。报告、页面、预览与 PR 显示同一用途、范围来源、参数缺失与资格；缺失驱动显示“未提供（未建立驱动能力）”；界面范围不得回写成模型限位或充当验证真值。

最终真实端到端产物必须与固定手工版进行对比，生产结果以 CAD 原值及明确、可复用的规则独立生成。方向符号与手工参考不一致属于非阻塞对比项；保留逐关节差异，不为此回填参考符号。固定参考可以由验收工具读取，但生产生成器不得消费手工名称表、轴值、限位值或网格。比较双方绑定准确版本、主体摘要和原始来源；不能用临时手调或模拟产物代替最终结果。命名与引用逐项精确核对；拓扑、限位、轴线、零位和非零运动仍按同一语义及既有严格容差核对；网格按实际几何、实例覆盖及放置核对，不能只看路径或包围盒。XML 排版、来源 warning 及新增审计证据单独列出，不把字节不同比作机械不一致。

报告保留原始 XML diff 和逐对象语义差异，并为每个差异列明 CAD 原值、参考结果、适用规则、自动输出与独立验证证据。存在明确规范偏差且可由通用规则修正时实际修正；不能仅为消除参考方向差异而增加规则。方向符号差异，以及符合 CAD 来源规则的 `robot.name` 与参考 inspection 标签的差异，单独记录为非阻塞项；其他未解决的一致性项目仍保持未完成。模型名的格式、来源 warning 和从原生声明或冻结顶层装配名独立复算仍须通过；不能因为参考名称差异获准就接受任意自报名称。规则须按对象类型、已证明的机械角色、配置或几何条件适用，禁止由固定对象 ID 查询手工答案。

用户于 2026-10-20 进一步明确放行 visual 名称及展示材质名称与参考不同。比较器必须将名称字段与颜色、纹理、元素存在性、对象绑定及引用完整性分别比较；仅名称差异可记录为非阻塞，不能豁免整个 appearance 或 material 类别。稳定命名、来源 warning 和独立复算仍须通过，最终 diff 保留全部获批名称差异及依据。

差异分类仅用于说明问题性质，不构成放行。除已明确批准的纯轴向反号、模型名、上述两类名称差异，以及满足下述几何等价标准的参考网格表示差异外，其他未解决项仍阻塞验收和发布；不能仅以“展示差异”“遍历序号”或“内容摘要不同”判定通过。须完成有据规则修正及独立验证，或提交明确的参考／规范冲突供设计总师裁定，不能由实现方自行豁免。

方向的独立 CAD 验证与手工参考对比采用不同判定（用户，2026-10-20）：前者仍检查有向轴读取、对象绑定、有效坐标转换、轴线及原生非零运动；后者报告同向／反向及其适用坐标系，不因纯符号差异判定导出流程失败。必要时可在比较器中明确对应的参数符号映射来解释运动差异，但该映射不能进入生成器，也不能掩盖其他几何或运动错误。实际驱动／编码器正向未绑定时记录未验证资格及 warning，不阻塞本次导出流程；不得据此宣称实机正向已验证。

<a id="geometry-equivalence"></a>
### 参考网格几何等价（用户批准，2026-10-20）

本标准只用于最终自动产物与固定手工参考之间的网格表示比较，不改变原生捕获、当前交付网格字节绑定、CAD 唯一工程来源或其他必需质量门。网格文件名、字节、三角形排列或三角化不同，只有按本标准证明等价后，才可作为已解释的参考差异关闭；最终 diff 仍保留双方真实路径和摘要。不得复制参考 STL、伪造摘要或用参考结果修改原生输入。

双方必须绑定相同的冻结 CAD 文件、配置、原生实体及实例归属，保留原生采集接口、工具版本、三角化设置和坐标转换。逐原生网格／实例在同一有据坐标系和米制单位下比较；禁止额外拟合、缩放、镜像或 ICP 对齐来隐藏位姿差异，也禁止跨对象平均误差。

两个方向的点到三角曲面距离上界均须 **≤ `5e-5 m`（50 µm）**。距离定义覆盖全部三角曲面，不能只比较顶点、包围盒、平均值、分位数或有限抽样最大值。有误差界的自适应细分、完整曲面包络或其他可复核算法均可使用；算法版本、数值误差界和停止条件必须留证。已证明完整有向三角面集合相同且重数一致，可使用零距离的精确等价快速检查；仅顶点集合相同不足以证明通过。

同一判定还必须通过原生拓扑、完整覆盖和朝向检查。不得新增或丢失实体、孔洞、开放边界、连通部分或原生面覆盖；合法曲面件不得被任意封口，整体翻面不视为等价。既有自交、非有限值、退化三角形、异常空网格、位姿和独立质量要求仍生效。面积、有符号体积及包围盒差异逐对象报告，但不能抵消其他失败。

报告逐对象保存原生身份、双方文件摘要与路径、比较坐标系、采集设置、三角形数量、两个方向的距离上下界、最坏点／面、面积与体积差、拓扑／覆盖／朝向结果和判定。全部必需条件满足且两个保守上界均在阈值内才为 `PASS`；可信超限见证或任一必需条件失败为 `FAIL`；缺少上界、上下界未收敛、算法超时或证据不足为 `UNRESOLVED`。`FAIL` 和 `UNRESOLVED` 均阻塞发布，不能用来源 warning 替代。

距离算法须通过独立已知答案验证，覆盖顶点、边、面内、面外、退化及混合退化三角形，并验证单位缩放和平移一致性。算法复算应保持固定输入字节不变；名称放行不豁免几何、颜色、引用或来源检查。

拓扑核验可采用有据、版本化的数值表达归一化，但不能把固定焊接距离作为自动放行门。先按原生 body 身份和共享边／顶点的邻接证据确定可关联对象，再从实际坐标类型、单位转换及计算误差推导可审计的误差界；不能由手工结果相同或“增大到闭合”的试验反推规则。仅坐标接近不能证明原生身份相同，禁止跨独立 body 焊接；簇必须满足明确的整体误差界，不能用传递合并把较大的真实间隙串起来。

归一化只用于拓扑关联和 T-junction 分析，不改原生捕获或交付 STL 字节。距离验收仍使用原始三角曲面，不能用归一化后的坐标消除实际距离。报告逐对象保留原始计数、精度和误差依据、规则版本、关联证据、应用后的计数及来源 warning。缺少足够的原生关联或误差证据时保持 `UNRESOLVED`；真实开口、漏面、错误归属、非流形或翻面仍须处理。独立反例覆盖合法量化接缝、刚超误差界的真实间隙、近邻但不同的原生顶点、跨 body 接触、缺面、翻面及 T-junction。

### 本轮冻结参考（2026-10-20）

用户确认以 [description PR #289](https://github.com/mimicverse/description/pull/289) 的固定提交作为本轮结果参照；只读取以下提交，不能随 PR head、分支或工作区变化自动更新。参考包含具体数值及其资格限制，不能只冻结总数或名称。

| 项目 | 固定值 |
|---|---|
| 原生主总装／配置 | `M3.0.3_20261016.SLDASM`／`Default` |
| 原生主总装 SHA-256 | `647b756847bb08f99287b71b86ea55e4cfb554df22f51a3e9f2ed337472799ce` |
| 原始上传清单 SHA-256 | `5f5c1f1a5dd874e8993486e0ddfe5043d422ec272bba63dbe59cb8a7488406db`（71 文件，52,400,231 字节） |
| 手工参考提交 | `d750c18e0c6f0a4bae576e472f5ea0c0d71585c1` |
| 手工参考 tree | `56bfb8c7a8b8f3167498b35d655dcbbdbf68ff4d` |
| 参考 URDF 路径／SHA-256 | `urdf/robot.urdf`／`1496dc09c7110d39486defcbeb58ee6aa8202cddc9f750a3e4970787059bfdc8` |
| 参考内部模型名 | `m303_20261016_inspection` |
| 参考结构 | 24 links、23 个 revolute joints、85 visuals、41 个唯一网格、24 份惯性参数 |
| 参考范围与驱动字段 | 全部位置范围为 `[-π, π]`，effort 与 velocity 均为 `0` |

表中范围是手工配方明确标注的预览策略：`native_cad_ranges=false`、`hardware_qualified=false`；其正向沿用历史检查版约定。上述记录锁定比较对象，不赋予其原生机械资格，也不授权直接复制 `_inspection`、范围或符号。检查预览范围由契约中的 `inspection_rotation_full_turn/v1` 独立产生并验证，不从参考取值。内部模型名仍按顶层 CAD 原生值及命名契约生成；预览策略与机械限位分别比较。参考与原生事实或适用规范的矛盾必须逐项说明并解决，不能隐藏差异或为匹配而改写原件。`urdf/robot.urdf` 始终是固定文件入口。

完整字段参考由该提交的 URDF、网格及来源／配方文件固定；验收报告同时记录它们的摘要、自动产物主体摘要和所用规则版本。逐对象比较覆盖全部名称与引用、拓扑、轴向、零位、非零运动、限位、质量、COM、完整惯量及网格几何，不将相同计数或总质量作为充分证据。来源审计和 CAD 问题警告保留对象／字段、原始值、违反的规范、规则 ID／版本、修正值、证据和未建立的资格；实际 URDF、报告、预览与 PR 的显示一致。

`description check` 只读重算并核对完整报告；额外报告写到交付目录之外。源、工具、策略或文件变化均需重新执行规定阶段与验证。通过结果不能脱离其主体迁移。

模型的工程批准、全行程安全、制造公差、接触/碰撞、仿真、训练和实机控制在各自验收中建立。中性原生夹具资格化其实际覆盖的工具行为，不资格化具体硬件。全部必需检查通过后可以发布绑定所选用途的待评审 PR；工程批准与实机资格未建立不新增自动失败门，也不能被 PR 创建授予；不同用途的候选与发布恢复身份相互隔离。

方向专项验收要求：当前不引入基于右手定则或功能动作的新正向推导，优先验证原生有向轴、有效作者声明和必要坐标表达。CAD 偏离已有规范时 warning 完整且有据可修正项不阻断；转换对正反向、小幅非零姿态及适用组合均成立；斜轴不被强行对齐、镜像不被无条件翻转、零位／边界不同步的错误能够检出。原先报告的七处方向差异分别列明原生证据、CAD 运动语义、适用转换及结果，不预置其必须为负号。最终对比使用实际端到端产物，按相同语义姿态比较手工版，区别表示差异与真实运动差异。

驱动／编码器方向关系须另有可追溯的依据：核对实际执行器身份与 CAD occurrence、命令类型／模式、编码器计数方向、零位、传动及关节物理运动。已有配置和验证记录只证明其明确覆盖的版本和范围，不把电机外观或安装朝向当成正转真值。覆盖执行器错绑、编码器与命令方向相反、版本不符、声明缺失及参考符号回填的反例；缺少证据时明确硬件资格未验证，不能以几何一致或 warning 冒称该资格通过，也不阻塞本次导出流程。查证外部配置不改变 CAD 唯一工程来源边界。

逐项复核所有实际反号均有对象级 warning，原值、参考系、修正值、规则依据及联动字段完整，并在报告、URDF 来源记录、预览和 PR 保持一致。正例包含保持物理运动等价的纯符号变换；删除反号 warning、缺少规则或前后值、仅部分交付面显示 warning，以及用 warning 放行无依据反号，均须被检出并拒绝验收。

须交付覆盖全部实际关节的固定输入核对表：原生身份与配置、有向轴原值及所在参考系、独立重读的参考轴绑定、作者声明及其语义和适用对象、CAD 运动定义、必要坐标转换、原生非零运动核验、输出轴／范围和字段级 warning。作者符号必须按正确绑定消费一次，不能被忽略或重复应用。未证明项保留原值并明确标记；参考方向不一致和未建立的硬件正向资格按上述非阻塞边界报告。必需 CAD 证据缺失仍失败，不得用默认正号填满矩阵或将未验证项标成已验证。

独立反例须覆盖：无向端点冒充正向、忽略或重复应用有效作者负号、作者声明错绑对象、混用参考系、非正交／共线基、用名称或功能投影代替原生正向、观测点不属于运动集合、终端或分支证据不足、斜轴及镜像轴向量、只翻轴而未同步坐标／范围、无依据改变零位与几何。更换同一物理问题的平移旋转参考系后，转换结论与实际运动须保持等价；纯符号映射分别核对姿态、角速度、范围边界及有来源的功率。有限正反步长按有效范围选择，真实零范围不可强行运动；同一假设轴上的公式自洽不能冒充独立 CAD 运动证据。

参考隔离是发布验收项：生产生成与独立 CAD 工程验证在固定手工参考不可访问时仍能完成其规定行为，输出及工程判定不依赖参考。仅最终一致性比较读取参考；扰动参考的名称、轴或范围时，生产输出摘要不变，比较器须检出对应差异。检查路径依赖、规则及测试数据，禁止逐对象答案以角色表、符号表或预期值夹带。每类规则的适用证据与反例先于参考比较固定，不能看比较结果再无工程依据地调参。
