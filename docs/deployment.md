# 部署指南

部署由一台 Linux 平台主机和一个串行 Windows 原生执行端组成。用户仅访问一个 HTTPS 操作台；Airflow、数据库、内部 API 和原生端点不直接向用户开放。

Linux 承担平台编排、工程模型与 URDF 生成、独立验证、报告和发布。Windows 运行原生 CAD 读取，并在释放 CAD 后完成当前 Capture 的原值投影、惯量规范化、raw/STL 序列化与证据封包；这部分不再占用 CAD 会话，但仍使用 Windows 主机，不能宣称全部可移植计算已迁到 Linux。Windows 只安装原生运行依赖和已资格化的 SolidWorks，运行在专用登录用户的交互式桌面中；不配置模型仓库、发布目标、Git 凭据、报告或 MuJoCo。

## 开发与发行链路

所有发布包必须从已合入受保护 `main` 的固定干净提交构建，开发与发行按以下顺序执行：

1. 设计总师确定设计、要求和验收标准。
2. 实现总工组织实现与测试，提交 PR。
3. 设计总师审核固定 PR head，确认满足要求及必需检查后，亲自合入 `main`。
4. 从该 `main` 提交构建完整发行，记录源码提交、发行清单和各资产摘要。
5. 将同一发行部署到 Linux 与 Windows，核验双端实际运行身份，完成真实端到端验收。

`description-pipeline` 与 `description` 仓库向 `main` 的合并只由当前设计总师根会话执行；实现总工和 workers 不得合并、直接推送 `main` 或开启自动合并。设计审核通过不转授合并权限。开发分支或 PR head 的隔离测试产物不得作为正式发行或部署包；禁止先发布后补合并及线上补拷代码绕过此流程。构建成功、组件测试、服务健康和手动导出不能替代真实端到端验收。

历史参考的方向对比仅用于独立参考测试，不进入生产或发布必经链；实际驱动／编码器正向未建立时如实显示未验证资格及 warning。CAD 轴向、对象绑定、坐标转换、原生运动或来源审计的真实缺陷，以及命名、限位和其他必需检查仍阻塞。不能把该非阻塞条款扩大为免除独立 CAD 验证、同版双端身份核验或真实端到端运行，也不能以手工符号替代生产输入。

## 主机准备

| 主机 | 必需条件 |
|---|---|
| Linux | 发行清单支持的 x86_64 Linux、CPython、正式默认 PostgreSQL 14、反向代理、Git/GitHub CLI、用户级 systemd；可访问 Windows SSH、飞书与模型仓库 |
| Windows | 发行清单支持的 Windows、CPython x86_64、licensed SolidWorks 与原生 API；Windows PowerShell 5.1 Desktop（`powershell.exe`）、OpenSSH Server、专用登录桌面和独立工程暂存目录 |
| 外部服务 | 可信 TLS、飞书企业应用及批准租户、私有模型仓库与存在的集成 base |

发行物的 `release.json` 是支持矩阵与依赖身份的权威：两端源码发行身份一致，角色依赖分别锁定。Linux 消费者验证无渲染，不要求桌面、GPU 或图形驱动。Windows 服务不能运行在 Session 0，不能复用工程师的交互 CAD 会话。

Windows 安装与生命周期脚本须在 Windows PowerShell 5.1 Desktop 上通过语法、API 及实际入口验证，保留严格 JSON 和重复键拒绝语义；Linux 上的 `pwsh` 测试不能代替该兼容性证明。新发行进入部署窗口前，先在目标主机用 `powershell.exe` 完成不操作 CAD、不修改活动服务的兼容性预验。

## 配置权威

Linux 使用一份私有 `platform.json`。schema 拒绝未知键、不安全路径、不完整目标和冲突端口；秘密只按文件引用。安装将其渲染为不可变的配置快照，记录摘要；操作台、Airflow 和维护命令读取同一快照。Airflow Connection 只引用该配置身份，不能再维护一份路由或凭据副本。

`solidworks_windows` 连接的 `host`、`password` 为空，`port` 为 `null`，`extra` 恰含 `config_identity`；该引用只含快照的 `schema_version`、绝对 `path` 和 `sha256`。消费者按同一契约检查受控目录、普通非链接文件、`0600` 权限、快照格式及字节摘要。新接纳再核对当前安装记录；已冻结运行继续读取自己的配置身份，不能回退到当前连接的路由。

安装、晋升和回退在关闭准入期间写入对应配置引用，完整核验后才能开放准入；失败保留原步骤证据。端点地址、目录、产品路由及凭据文件引用均从快照解析，不复制到连接字段。令牌轮换只更新受控凭据文件并验证认证，不重建旧式连接。

Windows target 的生产接纳只读取一次 `PLATFORM_CONFIG_SNAPSHOT` exact bytes，并以同一
组 bytes 校验 snapshot、state root 下的唯一 `install-record.json`、record 的
snapshot 绝对 lexical path/digest/install ID 与 `ends.windows.target_identity`。
`Application` 和 NativeOperationClient 必须使用该 target ID；DAG conf/XCom、trigger
event、当前 `/health`、URL、环境变量、tool/release identity 和平台 claim 都不能自晋升
expected target。`PreparedAdmission.register` 在 admit 前核对 application/client 与
spec target；已存在 request 的重试先复用封存 Spec，不读取 later current registration
改绑。

最小配置如下，路径与标识在安装前替换为实际值：

```json
{
  "schema_version": "solidworks-to-urdf.platform/v1",
  "operator": {
    "origin": "https://operator.example.com:8443",
    "certificate_file": "/srv/description/secrets/tls.crt",
    "private_key_file": "/srv/description/secrets/tls.key"
  },
  "storage": {
    "state_root": "/srv/description/state",
    "handoff_root": "/srv/description/handoffs",
    "artifact_root": "/srv/description/artifacts",
    "workspace_root": "/srv/description/workspaces",
    "release_root": "/srv/description/releases"
  },
  "database": {"dsn_file": "/srv/description/secrets/postgres.dsn"},
  "windows": {
    "ssh_host": "solidworks-worker",
    "endpoint_port": 8765,
    "token_file": "/srv/description/secrets/endpoint.token",
    "instance": {
      "task_name": "description-endpoint",
      "package_root": "C:/description/packages",
      "output_root": "C:/description/results",
      "state_root": "C:/description/state"
    }
  },
  "auth": {
    "feishu_secret_file": "/srv/description/secrets/feishu.json",
    "tenant_keys": ["<approved-tenant-key>"],
    "admin_open_ids": []
  },
  "targets": {
    "arm": {"repository_slug": "<owner>/<model-repository>", "base": "main"}
  },
  "specifications": {"record_roots": ["/srv/description/specifications"]},
  "retention": {"unreferenced_evidence_days": null}
}
```

`retention.unreferenced_evidence_days` 默认为 `null`，不自动删除封存证据；正整数只控制无引用证据保留天数。

`windows.instance` 固定 Windows 执行端的任务名及工程、产物和状态目录，维护命令依据配置和安装记录定位该目标。同一主机上的任务共享 CAD 排他资源约束，新用户或新目录不形成独立资源域。

配置、状态、上传、交付、临时工作区和发行安装使用专用目录，禁止使用主目录/操作系统根、目录重叠、符号链接或 junction。Linux 私有配置和秘密文件权限为 `0600`，私有目录为 `0700`。Windows 受管路径的权限边界由 protected ACL 建立，仅允许 `Administrators`、`SYSTEM` 与受控的本地管理员 service SID 的 `FullControl`；该 service SID 是已批准的运行时身份，不把管理员运行宣称为 least privilege。目录、配置、令牌、安装身份及其他稳定文件必须自身 protected，所有者为 `Administrators`。仅状态根目录直属的可变运行文件 `cad-ownership.json` 和 `cad-ownership.json.lease` 允许另一种等价形式：父状态根目录已核验为 `Administrators` 所有、自身 protected、恰有上述三主体的显式可继承 `FullControl`；文件也由 `Administrators` 所有，恰有同三主体的纯继承 `FullControl`，无显式、额外或拒绝 ACE。文件 ACE 的 inheritance 和 propagation 均为零，路径及其祖先不得为重解析点；文件自身 protected 的严格三主体形式同样有效。该规则允许原子替换后继承已验证的权限边界，部署验收和独立恢复验证使用同一规则；不得扩展到稳定文件或放宽父目录检查。受管根目录和共同父目录的 ACE 必须对新建目录与文件可继承，bootstrap、stage、promote、rollback 在写入前保存完整 ACL，写入和服务访问检查失败时恢复 root、marker、现有子项及本次新建子项的 ACL 后 fail closed；不得让新 generation 或后续目录形成空 DACL。移除 `Authenticated Users`、`Users`、`Everyone` 等普通主体；日志只展示标识、摘要和路径，不展示秘密值。

产品目标、仓库与 base 仅在 Linux 维护。Git 凭据属于 Linux 服务账户；模型仓库来源必须与目标一致，发布使用受管理的干净工作区。模型工程值以封存 CAD 为依据，允许版本化配置承载命名、方向及数值表达规则；配置不得抄入手工结果或覆盖原生事实。仓库规则须允许服务主体向受管理候选分支追加提交和创建 PR，禁止非 fast-forward；凭据权限限定于配置目标，工程集成 base 保持独立评审。PostgreSQL 的平台与 Airflow 角色只能迁移各自 schema，连接预算及恢复目标在部署验收中固定并验证。

## 安装 Windows 执行端

本次发行支持已完成受控初始化的 Windows 端点升级：目标已有受管管理员交互式执行账户、Scheduled Task、目录及 ACL、端点配置、令牌和安装身份。按用户 2026-10-21 决定，端点继续在已受管的管理员账户下运行；部署和恢复核验必须核验账户为本地、启用、属于 `Administrators` 且任务使用最高可用令牌，不得把该运行方式表述为 least privilege。全新 Windows 主机初始化及 Session 0 broker 不在本次支持范围，不能用依赖安装成功或手工启动端点代替完整初始化资格。既有端点的任务、配置、令牌、ACL 和安装身份恢复仍须实际验证。

受管共同父目录必须通过同版 `deploy/windows/bootstrap-managed-root.ps1` 完成一次性初始化。引导入口只在当前管理员执行身份与任务运行账户一致时运行，拒绝盘符根、共享系统/用户目录、重解析路径和缺失或冲突的 marker；它事务性写入唯一的 `.description-managed-root.json`，将父目录和 marker 设为 protected 的 `Administrators`/`SYSTEM` ACL，并在任一步失败时恢复原 ACL、删除本次新建 marker。`stage` 只接受已经核验的 marker，不自行选择或创建共同父目录。缺少 marker、普通组 ACE、服务身份或任务令牌资格不符时，Windows 安装保持拒绝。

内嵌虚拟组件随同版原生端点与 Linux 验证器部署，不增加服务、环境变量或配置字段。Windows 读取受控会话中的组件及所属装配绑定，Linux 按封存契约独立验证；不得将 `VC~~` 临时文档复制成上传来源或配置额外搜索目录。

校验 Windows 离线运行档案与发行清单摘要，由受支持的 `stage` 在新安装代目录建立运行环境并安装 hash-locked 依赖，再通过 `promote` 切换既有受管任务。禁止向活动运行目录直接安装依赖或手工替换代码。`doctor` 验证安装身份、最小角色依赖及 SolidWorks/COM 就绪；实际作业仍须检查文件和会话。端点令牌的两端副本受控保存，原生端点只绑定 loopback：

Windows 端点的新 generation 只使用解释器入口 `runtime\Scripts\python.exe`，参数固定为
`-I -B -m description_pipeline.orchestration.windows serve --config "<generation>\endpoint.json"`；
其中 `-B` 禁止解释器向受管 generation 写入字节码缓存。新目标的配置、发行身份和任务定义
必须与当前 stage 记录一致，不接受替代启动方式。不得依赖 pip 生成的 `description.exe`
launcher，也不得通过关闭 Device Guard/WDAC、添加例外或复制旧可执行文件绕过策略。

当前受管实例的所有权与新目标的启动断言分开核验。既有任务须绑定精确 task name/path、
唯一 action、受管服务账户和 ACL、同一 generation 的解释器与 `endpoint.json`、工作目录、
安装与发行身份，以及即时进程和监听端点。不能仅因当前实例的启动串不同于新目标而拒绝
既有受管对象，也不能接管外来任务、忽略身份冲突或把 UNKNOWN 当作已完成。

在 Stop 或任何替换之前，先持久封存当前原始 task XML、配置、身份、必要权限与 pin、
摘要和操作意图，再统一安装唯一目标状态。封存 `source-state.json` 后，以其原始字节
SHA 绑定同一 `previous`／当前 target 的 pending 与成功 promotion receipt；外部
rollback 必须命中该既有操作记录，缺失或冲突一律拒绝。失败恢复只使用本次绑定的封存源状态；恢复任务
按原始 XML 的安全语义和实际旧端点身份核验，不套用新目标的启动串，也不按历史版本
枚举兼容形式。Reader 在首次 mutation 前校验 receipt authority、XML 原件摘要、配置
摘要、任务归属、生命周期边界及可取得的 runtime／release／install 身份；恢复直接向
受管 Task API 提交原件，再读回核验实际 principal/action、settings、triggers、ACL
及旧端点健康身份。导出格式差异不放宽这些必需语义，也不宣称已对每个 XML 字段独立
复算等价。Rollback 恢复旧失败时，使用 mutation 前已保全的 current snapshot 与同一
authority 恢复当前端点，不生成另一套回填摘要的日志。Windows
统一解释任务和运行状态，Linux operator 只消费源／目标身份明确的
结构化事实；status、health、reconcile、rollback、backup 和 restore 共用此边界。
排空、维护锁、租约、不可变发行归档与 UNKNOWN 对账能力继续有效。

Windows 的 canonical release archive 是不可变 lifecycle 输入，不进入可变 recovery snapshot/state。恢复只重建 runtime、配置和任务身份；archive 缺失、摘要漂移或 ReadOnly/protected ACL 不成立时必须从同一固定 release 重建并重新 stage，promote/rollback 复用前重新核验 ReadOnly、受保护 ACL 与摘要，不能静默恢复成可写归档。

```json
{
  "schema_version": "solidworks-to-urdf.endpoint/v1",
  "package_root": "C:/description/packages",
  "output_root": "C:/description/results",
  "state_root": "C:/description/state",
  "token_file": "C:/description/secrets/endpoint.token",
  "host": "127.0.0.1",
  "port": 8765
}
```

由既有受管 Scheduled Task 在专用账户的交互桌面启动安装记录绑定的入口，保持桌面登录和主机唤醒。每个执行端只使用安装记录绑定的配置和 state root，执行期间无独立 CAD 活动；就绪检查发现外部会话时显示不可执行原因，不能关闭该会话。保持时钟同步、桌面有效和磁盘容量充足，系统更新/重启使用同一排空流程。所有启动与清理入口共用资源域，遵守[CAD 所有权契约](contracts.md#resources)。

## 安装 Linux 平台

配置服务账户的 Git 身份及 GitHub 认证，确认目标仓库与 base 可读写。配置专用 SSH key 和已核验 Windows host key；传输使用 SSH 隧道和端点令牌，内侧均为 loopback。拒绝跨主机重定向和未认证端点。

校验同版 Linux 离线运行档案、部署档案与全部摘要。首次安装须先用受支持的 CPython 3.12 和该发行的 Linux 角色档案建立独立引导环境；后续控制命令也须使用已核验的同版环境，不能假定系统 `python3` 已装有规范验证器。以下路径均为待替换的示例：

```sh
python3.12 -m venv /path/to/bootstrap-venv
/path/to/bootstrap-venv/bin/python -m pip install \
  --no-index --require-hashes \
  --find-links /path/to/linux-runtime/wheels \
  -r /path/to/linux-runtime/requirements.lock
export PATH="/path/to/bootstrap-venv/bin:$PATH"
```

在提取的同版部署目录执行唯一生命周期入口：

```sh
bash deploy/operator/operatorctl.sh validate --config /srv/description/platform.json
python3 deploy/operator/install_record.py endpoint-identity \
  --health /path/to/endpoint-health.json \
  --release-file /path/to/release/release.json \
  --out /path/to/endpoint-identity.json
bash deploy/operator/operatorctl.sh install \
  --config /srv/description/platform.json \
  --install-id NEW_INSTALL_ID \
  --wheel /path/to/release/package.whl \
  --release-file /path/to/release/release.json \
  --endpoint-identity /path/to/endpoint-identity.json
bash deploy/operator/operatorctl.sh start --config /srv/description/platform.json
bash deploy/operator/operatorctl.sh health --config /srv/description/platform.json
```

示例中的路径和安装标识须替换为本次发行的实际值。端点健康原件来自已认证的真实 Windows `/health`，须与本次 `release.json` 校验一致；不能手写身份、沿用旧端点收据或使用开发分支资产。安装使用同一发行目录中的 wheel 和清单；状态根、数据库端口和其他运行路径从当前平台配置及其受管安装记录解析，不从历史运行或个人目录猜测。

`promotion receipt` 是 `stage` 的接受边界。receipt 创建前的 snapshot、derived 和运行准备写入属于未引用隔离产物，不自动清理且不得复用；一旦产生任一类预接受隔离产物，失败后必须使用新的 generation ID。只有 receipt 及其不可变证据完整落盘后，候选才对后续 `promote` 生效。

`validate` 只检查静态配置，不验证发行文件、实际端点身份或运行健康。发行资产须单独核验；`install_record.py endpoint-identity` 将真实端点健康原件与发行清单交叉验证，`install` 再核验绑定输入，并在关闭准入的状态下安装锁定运行环境、渲染服务、初始化数据库及校验 DAG；不能改变正在运行的安装。`start` 先验证两端身份、服务及配置，再开启任务准入。数据库、内部服务和隧道不对用户暴露端口。

需要在服务启动后完成对账或维护验收时，使用 `start --keep-admission-closed --config /srv/description/platform.json`。它执行相同启动检查，但全程保持准入关闭，成功结果明确区分“服务已启动”和“准入已开放”。完成规范对账并通过结算门后，再执行普通 `start` 重验并开闸；不得手工改状态或先开闸再关闭。该选项不能用于其他生命周期命令。

首个受管代绑定真实目标发行的代码、运行环境和双端身份，不能为未受管的旧部署补造安装身份。切换前保存旧配置、服务绑定、原件和数据路径作为回退基线；回退不得丢弃已经发生的业务或对账事实。

安装记录明确管理的全部服务与路径，不依赖工作目录或个人工具位置。配置变化先渲染并验证新快照，再按受控切换流程应用；运行中的请求继续使用其固定配置身份。`status/health` 及提交准入核验安装记录、发行文件、配置快照、实际服务、Airflow 配置引用和 Windows 发行/契约/端口身份，端点认证同时证明令牌一致；漂移关闭新准入，健康查询不打开 CAD。

同版双端核验比较 `version`、`source_commit` 和 `source_sha256`。两端各自的 `install_id`、`config_digest` 必须匹配对应主机的安装记录和实际配置，不要求两端这些值相同；不能用同版发行身份代替每端的配置、角色和运行身份核验。

Windows `/health.install_id` 必须位于顶层。模板的 expected `NativeTargetIdentity` 只来自
已核验部署登记，不从环境变量、当前 health 自报或迁移后的 URL 推导。stage/promote
分别核对身份与 readiness：`unresolved>0` 可使 `ready=false`，但只读 status、
reconciliation 和 recovery 仍必须可用；不能因原 operation 正在运行或未决而拒绝
查询。endpoint preparation 的顶层 `target_identity` 原样进入 install record 的
`ends.windows.target_identity`，并由 record 的 snapshot path/digest 绑定配置；不回写
platform snapshot。每个请求、响应与 original operation 保存并复验同一 target；服务端 upload
同时校验 expected target、operation target 和当前 runtime，错误 target 在 admission
与 create 前拒绝。URL 可迁移，同 install 重启可恢复；state root 变化、404、health
不可达或错路由只保持原 operation UNKNOWN，不推断 absence。tool/release、平台
origin claim 和 native worker owner 分别核验；同一 install 恢复仍排他，复制 install
ID 不构成授权，并继续只使用一个既有部署登记来源。

DAG 验收还须核对活动记录中的绝对路径与发行成员摘要、实际生效的 `core.dags_folder`、当前 `DagVersion` 和 `SerializedDag` 关联，以及序列化的文件和解析目录。唯一支持的 `dags-folder` 本地 bundle 使用空版本；不能因此省略已登记版本 UUID 与实际任务来源核对。真实监督任务日志记录加载的 DAG 路径、SHA-256 和 Python 包来源。历史目录即使存在同名字节，也不能代替当前发行绑定；身份不符时不得开放新准入。

Airflow 发行固定为 3.3.2。DAG 的六个 task 均使用 `StageTaskOperator`，当前固定
`max_active_runs=8`、`max_active_tasks=8`；这些值只限制调度并发，不能替代 physical
CAD ownership、容量为一的 native pool 或 target/effect conflict。operator 的
`execute()` 绑定真实 DagRun/TaskInstance 后调用 `StageApplication.run_stage()`；
`StagePending` 以持久 `StageEffectWait` 调用 `defer()`。恢复由
`StageTaskOperator.execute_complete()` 先核对 wait/event digest 与真实 task
context，再调用 `StageApplication.resume_stage(wait)`。

异步 `EffectPollTrigger` 只读取 canonical wait/effect/outbound 和精确
operation/target，不取得 claim、不创建或上传 operation、不 seal payload、不写
fact/decision、不取消外部操作。401/403、`native_operation_identity_mismatch` 及其他
非 `native_target_mismatch` conflict 必须中止；404、缺 status transport error、
可识别协议错误、5xx 或明确 `native_target_mismatch` 保持同一 wait 并继续只读 poll。
错误 event/wait、foreign operation view、错 target、旧 fence 或 trigger 重启不得造成
重复外发。

发行安装必须包含 Airflow 3.3.2 triggerer，配置中显式设置
`[triggerer] capacity = 64`、`job_heartbeat_sec = 5.0` 和
`triggerer_health_check_threshold = 30.0`；受管单元
`description-airflow-triggerer.service` 与 scheduler/executor 一同 stage、promote、
start、stop、health 和 recovery。triggerer 缺失、版本不符、未运行或 job heartbeat
不健康时部署保持失败；fake trigger、仅改 DAG 或只有同步 TaskFlow 不满足验收。

## 飞书与权限

企业应用启用 Web app，主页使用配置的唯一 HTTPS origin，回调为 `<origin>/auth/feishu/callback`。应用版本向批准用户发布；浏览器须信任证书。配置精确租户 allowlist，管理员使用同一应用的显式 `open_id`，空列表不授予管理员权限。

应用凭据文件包含 `app_id` 和 `app_secret`。登录使用 OAuth state、PKCE S256、服务端 code exchange 和基本用户身份 API；租户与 app/tenant/open_id 共同校验。会话使用安全 cookie、CSRF 防护及明确过期；不申请离线授权或保留 refresh token。代理只信任本机入口并执行固定 origin。

租户成员可提交与查看共享结果；只有发起人和管理员可变更运行、重跑或管理显示记录。工程批准另有责任边界，登录不赋予该批准。[操作指南](operations.md)规定用户行为。

## 部署验收

开放服务前，在已安装发行上验证完整流程：

1. 两端安装身份及 `doctor` 通过，配置一致、TLS 可信、真实飞书登录成功；未批准租户和越权运行管理被拒绝。
2. 上传原生文件夹，选择主装配与产品目标，执行全部六阶段；请求、证据、报告、预览和
   PR 绑定一致。Discover 与 Capture 分别核验本 operation archive、materialize 结果和
   生产身份与同一 SourceSet 原件血缘一致；SourceSet 与 prepared 保持不同摘要。两个
   operation 可以独立传输和物化，单 runtime/CAD owner 与 operation-local workspace
   不变；不得新增跨 operation source cache 或把内容复用当作工程资格。
3. Linux 推导、生成、验证、报告与发布不调用 Windows；验证/发布重跑不创建 CAD 操作，Windows 安装不包含可移植运行依赖。 外观验收须用本次原生事实证明实际交付面的对象、状态、RGBA 与无损分组；覆盖完整 public-QI 分类、Part face 到 body/feature 的原生归属、合法共享 identity/fragments，以及缺失、分歧和接口不兼容的拒绝。零参 COM 方法须按方法调用，live handle 不得进入深复制或封存事实。验证原生 activity 经既有 poll 到 Portal 的真实显示，activity 与阶段资格分别核验。隔离 Capture 与 Linux transfer 通过不能代替正式 Freeze 开始的六阶段验收。
4. 同时提交、重复请求、断连与重启保持幂等；在全新进程中从已登记 command 和封存
   artifact 重建 workspace，不依赖前一阶段内存、缓存或临时目录。native operation
   串行，未知 CAD 会话被拒绝且不被访问或终止。
5. 在锁定 Airflow 3.3.2 的 scheduler、triggerer 与 executor 上证明真实 deferred task
   释放 worker/pool slot，并由新 worker 恢复同一 command；另一无冲突 run 可推进，
   同时 CAD ownership 不重叠。覆盖 worker/triggerer 重启、重复或错身份 event、旧
   fence、丢 ACK、UNKNOWN、timeout 与取消，证明无重复外发或虚假 qualification。
   直接混入 401/403、foreign operation view、`native_operation_identity_mismatch`
   和非 target conflict 必须使 trigger 明确中止；404、transport、5xx 或
   `native_target_mismatch` 保持同一 wait 继续 poll，不得切换到其他 operation。
6. 缺失/包外来源、错误配置、坏证据及验证失败阻断下游；诊断与未验证预览的身份清晰。
   发布失败保留验证结果，未知外部结果通过对账恢复。
7. 新的正式重跑或 Freeze successor 始终从 `freeze` 接纳；只复用父运行已验证
   `freeze_originals`，当前 rules/config/target/Git/native requirement 与双端工具
   重新封存，不继承父 Discover 及后续 decision、成功或 qualification。已登记 command
   的安全重试保留原证据时间、producer、effect 及封存请求身份；当前执行权按
   canonical claim 规则取得新 fence，不改写外部请求中的旧 fence。删除/恢复显示
   台账不移除证据。每次 successor/claim/decision/send 都重算 immediate 与 supporting
   predecessor qualification、result/failure metadata 和 effect ledger；跨运行复用只
   经已验证父 Freeze originals 的新请求进行。
   服务重启后请求、历史、准入和发布状态从 canonical facts 经唯一共同业务 projector
   恢复；单个诊断目标不能计入正式六阶段。
8. 使用实际连接写入端与正式健康/DAG消费者验证同一配置引用，并证明切换后已冻结运行的配置身份不变；演练 stage/promote/rollback 与第二主机切换失败，注入配置漂移，核对仓库规则允许追加且禁止非 fast-forward，完成凭据轮换及备份恢复；全部维护路径使用同一生命周期和资源域。
9. 为 native 输入传输注入一种可恢复窗口：原 operation 已 accepted、input 未 admitted、
   传输响应丢失。验证只有同一 command/effect/envelope/request/payload/archive/tool 且
   祖先、取消和维护门仍允许，且旧 claim 已 yielded/stale 时，共同 engine 才取得
   narrow `transfer_resume` claim；不得接管未过期的 live claim。handler 重建原字节后，
   必须由独立 `authorize_resume` 事务再次核验当前事实并提交发送授权。验证两个事务
   之间的取消、祖先变化、租约到期和物化失败均不外发。服务端对原
   operation 只放行原 archive，input admission 与
   executor 各一次，原外部 request 内 claim/fence、producer 与工具身份不变。覆盖
   worker/triggerer 重启、lost ACK、重复 event 与 timeout；注入 404、observer
   failure、错 archive/tool/effect identity、重复 ACK 和 cancel 竞争，证明前者保持
   未知或拒绝且绝不 create，后者只保真实先后事实；ACK 与 trigger event 均不证明
   native success。另证明 Git ref/PR upsert 的 UNKNOWN 不能取得
   `resume_transfer`、重传或普通 handler 重跑，只由 git/gh 只读 hint 唤醒 worker 的
   权威 reconciliation。
10. 用新 wire/schema 的真实组合验证 NativeTargetIdentity：两个 same tool/release 但
    不同 install target 的端点不能串用 upload、operation、observation 或恢复；同一
    install 在 URL 迁移和合法重启后只能恢复原 operation。注入 health 到 upload
    之间的 target/runtime 切换、错误路由、state root 变化、404、health 不可达及并发
    request，证明服务端而不是客户端执行 expected/operation/runtime 三方核对，错误
    target 不 admit、不 create、不启动 executor，原 operation 保持未决。另证明
    `ready=false` 或 `unresolved>0` 不封锁只读观察、对账和恢复，且 same tool、平台
    origin claim、native worker owner 与 install target 分别核验。
11. 使用固定候选的真实生产方法验证 `native-triangles/v1`：从 occurrence/body/face
    的原生返回读取 raw 三角形、实际 placement、物性与基准值，证明 CAD 释放先于
    离线投影、STL 序列化和封包。质量、质心及两种参考点的惯量须从同一实际原生
    物性对象读取；测试不得用 support fixture 预填缺失原值来替代生产读取。
12. 注入原生读取失败、原始矩阵和惯量缺失或篡改、非法数量、截断/尾随字节、非有限
    三角形、错误 occurrence/configuration/source scope、face span 遗漏或重叠、
    错误 placement 和 STL 派生字节。独立 consumer 必须从 raw 复算并拒绝，保留
    primary 错误及 partial facts；正例覆盖合法原生读取、释放后的离线转换和原值复核。
    raw/STL 一致性不替代当前 CAD 的独立工程验证。

保留真实原生、身份、授权、失败和恢复证据。mock、健康绿标或能打开页面不能替代完整验收。

验收固定同一个已合入受保护 `main` 的提交。CI 的必需门包含锁定 Airflow 环境中的真实普通操作员提交/调度及发行完整性；模拟端点只覆盖运输与失败阻断。发布完整发行后，校验资产摘要并在两端按同一版本 stage/promote，不对活动发行目录补拷源码或脚本。最终记录绑定源码提交、发行清单、双端安装/配置、原始工程清单、运行、六阶段收据、独立报告、实际 URDF 预览和模型 PR。当前模型仓库为 `mimicverse/description`，集成 base 为 `main`；手工与历史 PR 不参与工程生成；历史手工版仅进入独立参考单元测试和回归测试，不作为生产值来源、验证真值或正式发布必经输入。参考测试按语义区分合法表示与真实工程回归；正式验收仍依当前 CAD、独立验证和真实端到端的必需门。

正式入口须验证普通操作员的必选目标及多行说明，并用浏览器实际加载绑定交付的 URDF/网格、执行关节交互。生成后自动显示预览；独立验证未通过时只提供明确标注的未验证诊断预览，不能使用通过绿标。页面阶段、检查计数和资格与同一封存结果一致。验收中的待执行项保持待执行，不因服务健康或历史运行通过而封板。

操作员在同一入口明确选择工程或检查预览用途；冻结记录使用唯一 `request/v2` 并保留选择。逐组核验输入、请求、运行、报告、实际关节预览与 `main` 候选 PR 的绑定，不能跨用途代跑。两种用途均以当前 CAD 和明确的适用规则建立交付；检查预览可按[用途契约](contracts.md#purpose)的 `inspection_rotation_full_turn/v1` 使用全周窗口，必须验证适用条件并显式显示原生范围缺项与策略来源，不得抄入手工结果或冒称机械限位。已有有效 CAD 范围不得被窗口覆盖。缺失驱动不显示为零能力；报告、预览和 PR 一致显示参数来源与资格。必需检查和原生事实要求不因用途减弱。不同用途的候选与发布恢复身份按[发布协议](contracts.md#publication)隔离。

需要从已有运行改用封存 CAD 原件时，`/api/runs/<parent>/attempts` 只接受
`stage=freeze`，调用 `load_production_freeze_successor(...)`。该入口通过
`StageApplication.load_verified_freeze_origins(parent)` 重验父 Freeze
decision/result、`inputs.json`、originals ref 和 CAS bytes，只复用
`spec.originals`；当前 rules、config snapshot、target、RepositorySnapshot、Git
baseline、native requirement、portable/native closure 与六阶段 executor 重新接纳，
随后调用同一 `StageApplication.admit`。返回的 technical dispatch command 交给同一
`StageApplication.dispatch(command_id, adapter)` facade；facade 内的
`TechnicalDispatchService` 先取得 claim/fence、提交 `dispatch_authorized`，再调用
Airflow trigger。实际 Freeze task 只绑定启动后的真实 task context，不授权自己的
创建。Portal 不接收或构造 `VerifiedArtifact`，不实例化第二 Factory，不保留 Discover
及后续阶段起点旁路。

方向验收绑定本次 CAD、规则版本、固定代码提交及实际交付：先核对全部关节的原值、作者声明绑定、必要坐标转换和未解决项，再通过门户预览执行有效范围内的非零运动，验证 CAD 运动语义、零位及范围的一致性。当前不发布基于右手定则或功能动作的新正向推导规则，历史试算不作为通过证据。必须注入共享 `status=local_class_aligned` 而作者正向证据仍缺失的负例，证明 Discovery、Generate、独立验证、模型来源、URDF、报告、预览和 PR 均保留同一
`direction.semantics_unresolved` warning；同时核对 axis／xyz／rpy／limits、封存
Capture bytes 和 `machine_qualification` 不受 warning 影响。生成与独立工程验证只取得
CAD 和规则输入；固定手工参考仅由独立参考测试读取，测试覆盖缺失、错误版本与扰动反例，不进入实际生产或发布必经链。未解决的命名、范围或其他真实一致性缺陷，以及 CAD
轴向、坐标转换和原生运动错误仍阻塞。不得以试算、旧运行或健康状态替代发布验收。

<a id="lifecycle"></a>
## 维护、升级与恢复

生命周期命令为 `validate / install / start / drain / stop / status / health / reconcile / stage / promote / rollback`，统一读取同一配置及安装记录。`drain` 关闭新提交和 DAG 准入，保留在途执行；返回全部活动、未确认与发布中操作。`drain`、`stop` 和成功显示共用唯一共同业务 projector 及真实 Airflow 关联，不得读取独立 SQL reducer 或仅凭 Freeze decision 推断正式运行成功。`stop` 仅在已排空且状态已知时停止全部管理服务，禁止通过杀进程代替排空。

备份维护使用 `stop --keep-database` 停止受支持写者并保留 PostgreSQL，随后核验实际 Linux 服务和 Windows 任务停止状态。备份从 `drain` 前至归档、清单及收据完成一直持有状态根下的 `locks/maintenance.lock`；`start / drain / stop / reconcile / stage / promote / rollback` 使用同一锁。嵌套调用须验证继承的锁及属主，不能仅凭环境变量绕过加锁。

维护查询账户 `operator_ro` 的只读面按共同 projector 的实际依赖固定为：

| 权限 | 对象 | 边界 |
|---|---|---|
| `SELECT` | `lifecycle_projection`、`admission_projection` | 仅真实 Airflow 关联、drain/stop bucket 与准入投影 |
| `SELECT` | `run_admission`、`command`、`stage_decision`、`effect_intent`、`effect_fact`、`execution_claim` | 同一只读 repeatable-read snapshot 供 `read_admitted_runs` 复算共同状态 |
| 禁止 | 六个事实表的写入、`run_display_metadata` 写入及 Airflow schema 直接访问 | `operator_ro` 无 `INSERT`、`UPDATE`、`DELETE` 或 DDL |

这六表只读权限是 `read_admitted_runs` 在同一快照消费 canonical facts 的最小必要
条件；显示 metadata 由受控写者管理，不由 `operator_ro` 写。
`RunControlStore` 通过 `read_admitted_runs` 的
`project_effect_state/project_stage_status/project_run_status` 共同投影读取当前
运行状态，并只写 `run_display_metadata`。SQL `lifecycle_projection` 仅提供真实
Airflow scheduling/DagRun 关联和 drain/stop bucket，不能聚合 stage decision 或
effect。该 store 不能写 command、claim、candidate、decision、effect 或
qualification，不能成为第二 stage 状态权威。显示名、隐藏与恢复只改变显示台账，
不改变运行状态、阶段结论或证据。正式成功必须来自六个 formal stage 的 complete
decision 与满足的 effect ledger；diagnostic 目标与正式六阶段隔离。
关闭与重开准入只经窄 `AdmissionControlStore` 事务 API 执行，使用明确的写入授权；
operator 不构造 `PlatformStore`、artifact reader 或第二状态机。只读凭据不能执行这些
变更。维护查询采用[架构定义的一致快照](design.md#持久化与一致性)，缺绑定、未知状态
或权威不可读时拒绝停止和切换。

旧 `platform.storage` filesystem checkpoint writer/URI resolver、native discovery
reconstruction builder、receipt 和 seal/confirm surface 不属于部署。Historical root
recovery 只导入 `validate_native_discovery_stage` 校验既有纯原生 stage evidence；
显式一次性 Capture migration 使用 `ArtifactStore.seal_tree()`。不得恢复
`store://sealed` 文件权威、旧 Store 写者或为旧 test 保留不可调用接口。

Portal、平台编排启动和后台有界退避重试直接调用
`StageApplication.reconcile_effect(command_id, effect_key)`，不通过 shell。
维护人员使用 `operatorctl reconcile --run-id <run> --stage <stage> --effect-key
<key>`；该 CLI adapter 只解析 canonical run、stage 和显式 effect key，没有
kind-to-observer 映射。同版 `deploy/operator/reconcile-effect.py` 读取唯一的 command
后调用生产 application。该入口按 `command_id`、`effect_key`、不可变 outbound 和
原始 provider identity 选择待结算 effect，只覆盖 native operation、Git ref 与 PR
upsert；technical Airflow dispatch 的 UNKNOWN 由 `StageApplication.dispatch`/
`TechnicalDispatchService` 观察原 DagRun/conf。两类 path 不互相替代，不提供
native-capture、release、stage 或 publisher 专用恢复命令。正式发行必须不包含
`deploy/operator/reconcile-native-capture.py`，且不得为该已删除脚本保留 fallback。
未决状态未消除时，排空和停止都不算完成。

生产 composition 的唯一明确入口为 `application_from_environment()`。它安装
application registry，并由 application 按原 `EffectIntent.effect_kind` 选择
native/Git/PR adapter；Portal、自动重试和维护 adapter 不各自构造 observer、硬编码
NativeObserver、接收 provider 输出或 evidence 旁路，也不建立第二套 effect-kind
映射。

正式执行前先关闭准入并生成只读预演，列出原 command/effect、目标 provider、证据摘要
和唯一预期事实。native observer 只 GET 原 operation，不 create/upload/trigger/cancel
或打开 CAD；Git observer 在临时 clone 中验证原 bundle/base/branch，不 prepare 或
push；PR observer 只按原 repository/base/head/candidate 查询，不 create/update。
精确 known payload 可建立 SAT；普通单次 404、空查询或 `unobservable` 不足以证明
sent effect 不存在。只有完整资源范围、完整原 operation identity 和无遗漏搜索结果均
闭合的精确 absent reconciliation 可建立 `definitely_failed`。multiple、conflict 或
content mismatch 保持未决。完整观察目录、原始响应、metadata 和未封存 workspace 在
service seal/fact 提交成功前始终保留；失败不清理证据，所有结果均不得盲重发。

reconcile 无 claim/fence，不创建 reconcile command，也不发送授权。其短事务只锁定
当前 `effect_intent`/`effect_fact` sequence，并只读重验原 command、outbound/artifact
identity 与 metadata closure；外部观察在事务外执行。普通 successor/claim/send/
decision 则先持当前 command 边界，再从根 predecessor 到叶复核有界 decision/result 与
effect ledger，最后进入当前 claim/fence、cancellation/effect 与 candidate/decision
边界；不得引用已删除的 run 表锁。相同 reconcile 输入返回原结果，冲突事实拒绝。后到
旧 fence response/timeout 只能追加审计事实，不能覆盖 reconcile 终态。不得手工 SQL
改终态、删除记录、使用空的平行数据库、重跑 CAD 或豁免未知行来排空。

该能力属于固定候选的维护完整性，必须在合并前完成实现、隔离反例与真实数据库验证；生产执行只使用已验证的完整发行或其正式维护路径，不向活动安装目录补拷修复代码。

`stage` 负责两端候选：从校验档案建立独立 Linux/Windows 安装目录和配置，核验身份、角色依赖、DAG、存储 schema 与原生就绪，不改变活动指针或启动第二个 CAD 资源域。

维护命令使用待晋升的正式发行目录中的控制脚本，发行清单只能是该目录的 `release.json`。候选的既有晋升收据同时绑定清单的绝对路径与字节摘要；晋升、回退和候选对账先核验同一绑定，再执行任何切换或写入。跨发行目录的清单、清单内容变化或缺失绑定均明确拒绝。历史收据保留原件，不为恢复动作补造绑定；再次前进仍用新的安装标识和收据。

Windows 候选配置由正式 `stage` 按当前端点 schema 确定性生成。现役配置必须已经符合该 schema；`targets` 或其他未知键、重复键及不规范值均在 `stage` 前拒绝，不做旧形状转换。根路径、令牌文件引用、监听配置及其余有效字段的值与含义保持不变，用既有规范 JSON 写者输出候选并记录来源与候选摘要；不要求保留候选的空白、键序或行尾。现役配置原件按字节封存供恢复，运行目录不热改。安装工具实测加载候选，`promote` 核对其摘要与 `stage` 记录一致，再以实际 `/health` 的配置和安装身份确认消费结果。

`promote` 依次记录：关闭准入并排空、建立可恢复备份、停止两端执行服务、执行平台和 Airflow 各自的已验证 schema 迁移、切换两端安装/配置指针、启动并核验身份、完成健康和验收、开放准入。每步固定前置条件与实际结果；任一主机或步骤失败，准入保持关闭，不能将部分切换报告为成功。

`stage` 不切换运行中的 DAG。晋升、回退及再次前进仅在排空且执行服务全部停止的屏障内，按目标安装记录渲染其 DAG 配置；记录指针与配置可以分步更新，但任一步失败都必须返回失败并保持关闭。配置核验成功后才启动执行服务，当前 DAG 序列化身份核验成功后才开放准入。回退读取原代自身绑定；再次前进使用新的安装标识和一次性收据，仍来自同一已合入 `main` 的固定发行。

升级意图和每个切换步骤持久登记。失败记录已完成与未确认步骤，`rollback` 使用该记录恢复两端明确的上一发行和配置，再检查身份与状态；不能猜测目录或最新备份。代码回滚必须兼容当前持久状态，不能回退数据库而丢失已产生的运行、证据或发布记录。涉及存储 schema 的变化须提供备份、迁移验证及明确可恢复方案，准入在完成前保持关闭。

`operatorctl rollback` 不回滚数据库。灾难恢复通过同版 `recovery.py backup / restore` 执行受控备份和同路径空目标恢复；`backup.py` 的加密归档原语不承担完整恢复判定。备份使用新的输出目录和独立保管的外部密钥，恢复使用显式目标计划；复用既存目录或未取得本次归属的失败请求不得改写旧回执、归档或暂存文件。具体边界遵循[灾难恢复契约](contracts.md#灾难恢复)。

每次发行须绑定该 `main` 提交准备并实际演练恢复步骤，覆盖平台与 Airflow 数据库、原件及其引用、发行与工作区重建、安装记录、配置、密钥、既有 Windows 任务与 ACL、双端身份和未决外部结果。命令只完成其中一部分时，报告逐项注明未完成范围；整体通过必须来自真实双端恢复和随后端到端运行。仅解密成功、代码回滚成功、隔离数据库与服务替身测试或服务健康不能作为完整恢复通过的依据。

恢复 Windows 任务定义不等于可以启动端点。未知锁属主或观察器失败时保持停止；只有取得可启动的锁及属主证据才启动。恢复核验使用切换前绑定的上一代自身发行身份，不能拿候选身份核验旧版；上一代身份缺证时健康结论保持未知，准入保持关闭。

备份前关闭准入、排空并确认无未决副作用，在同一维护锁内停止写者，取得平台与 Airflow schema 的同一数据库快照，再按该快照引用复制冻结原件、交付/诊断、安装记录、配置与密钥并核验清单。备份加密、访问受控，按部署恢复目标保留；升级前须有已验证的恢复演练。备份成功或中途失败后均明确报告服务停止与准入状态，由受控生命周期入口恢复运行。恢复在关闭准入状态下核验清单和引用，查询两端作业与远端发布结果后重建投影，先证明完整再开启服务。

恢复 runtime 必须用自身的 Python 3.12 虚拟环境完成 package identity、
`description`／`airflow` entry probe 与 `pip check`。probe 从 caller 仅去除
`PYTHONPATH`、`PYTHONHOME`、`VIRTUAL_ENV`，设置 `PYTHONNOUSERSITE=1`，并保留
`PATH`、`LD_LIBRARY_PATH`、`SSL_CERT_FILE` 等系统运行变量；解释器和
`pip check` 使用 `-I -B`。必须同时验证真实缺失依赖负例失败、caller package shadow
不可见、恢复包 member/source digest 属于该虚拟环境。不得通过增加 test-only package
或放宽 `pip check` 掩盖发行错误。

恢复工具仅消费 v7 清单、v2 selection 及 v3 目标计划，工作进程协议为 v5。旧恢复
清单、来源选择、目标计划及更早协议在状态目录创建、解包和目标写入前拒绝并保留原件。
不得把旧包更名、改版本或移除检查后强制恢复。目标计划除了固定绝对路径和清单摘要，
还必须带同一 typed fact-root 与来源范围的闭包摘要；恢复完成收据和 marker 的绑定必须
一致，finalize 再次核验后才能启动服务。

正式目标计划统一由同版
`recovery.py target-plan --manifest <v7清单> --host-id <目标身份> --postgres-config <目标数据库参数JSON> --out <新计划>`
生成。数据库参数必须显式给出 `bin_dir`、`library_dir`、`socket_dir`、`data_dir`、
`port` 和 `bootstrap_role`。生成器先验证真实清单和外部摘要，再复制 closure、原绝对
root 路径及 Windows 身份，生成 v3 并复验；输出必须是新文件。任何其他 target-plan
脚本或配置副本都不是支持入口。

快照先在持有的数据库事务中发布完整 canonical typed 根及摘要，随后计算和复制精确
闭包。根必须覆盖 `run_admission.request` 与 `command.request` 内嵌 artifact 引用
（包括未来阶段输入和 dispatch conf）、`command_input_artifact`、
`effect_intent` outbound、`effect_fact` payload 与 reconciliation metadata、无
decision 的 `stage_candidate` output/result、已决 `stage_decision` result、
FailureEnvelope evidence，以及未决 claim、未封存故障 workspace 与 typed
`retained_locations`。缺项、摘要漂移或类型不明时不得形成可恢复清单。

CAS artifact root 只复制该 typed 根实际引用的 object；共享 object 只复制一次，只有
完整事实根证明无任何引用的孤儿才可排除。未知 effect、未决 candidate、已授权但响应
未知的 outbound、失败或取消原件和未封存 workspace 都必须保留，不能因没有 decision
而省略。恢复后 ArtifactRef、StageResult、candidate、effect ledger 与 claim/fence
身份必须逐项复验；未知外部结果仍须精确 observation 或 reconciliation，恢复动作不得
重发。状态侧保留固定配置和小型安装元数据，仅按当前/上一代 journal 的 typed 引用
保存 promotions 文件及必要的外部证据，不递归复制整个 promotions，也不复制历史
`backup.tar.enc`。原路径、原权限和摘要均受验证。历史元数据不意味着历史回滚可用；
所需密文或证据缺失时，在关闭准入、切换指针、数据库、Windows 和服务操作之前明确
拒绝。

旧 `request/run/stage/operation/run_event` 五表的删除只能在排空和所有受支持写者停止后
执行。切换前必须在一致快照中绑定真实 source database、platform schema、完整
DDL/ACL/rows、逐成员大小与 SHA-256、aggregate digest、所有未决 command/effect/native
关联及切换操作身份，并从该回执实际完成独立 restore/读取复核。任意文件 hash 或自报
`verified` 不构成证明；错源、缺表、遗漏未决事实、摘要冲突、快照后并发变化或缺少
restore 证据均拒绝。

正式部署依赖 Ubuntu 22.04 的 PostgreSQL 14 发行包和
`deploy/airflow/scripts/install_postgres.sh` 默认布局；`recovery.py` 也固定核对
major 14。PostgreSQL 16 仅用于 CI/pgserver 测试，相关结果必须标为
`postgres16`，不得被解释成正式环境支持。最终部署必须在 PostgreSQL 14 上重新执行
archive、independent restore、错误 target 拒绝、fresh apply 与 cutover。

receipt 还必须绑定 PostgreSQL cluster `system_identifier`、database OID、platform
schema OID、migration role、server version、database encoding、collation provider、
locale 与 ctype；相同 database/schema 名称或相同 locator 不能跨 cluster 或不同
collation 环境复用。archive/evidence 使用 v3 schema，并记录 ICU locale/rules、
collation binding 及 recorded/actual version；旧 v1 receipt 不得自动升级。归档的 plain
schema/ACL SQL 在维护锁内与 live 重新导出比较；PG14 随机外部
`\restrict`/`\unrestrict` token 只允许严格配对后为比较归一化，不改变 dump 内容。
独立 `initdb` restore cluster 必须使用源 encoding/provider/locale/ctype、ICU
locale/rules 与兼容 server version，从 `template0` 建库并核对全部源 locale identity。
relation row evidence 使用显式 `COLLATE "C"` 排序，restore 后另在测试表证明源默认
collation 顺序恢复且不同于 C 顺序；单行、多行和多字节值不得在两种环境改变接受结果。只有
archive、manifest、evidence、receipt、live catalog/rows/ACL 与 restore 全部一致，才
允许在 serializable cutover 事务中重新观察和切换。

只有 `apply.sh --legacy-archive-receipt <receipt>` 按同一 typed contract 验证上述回执
后才能删除。schema migration 在没有有效回执时拒绝；删除不得使用 `CASCADE`，任何遗漏
view、grant 或其他依赖必须显式失败并保留目标。该门不得扩大部署权限，也不得作为永久
保留旧 Store 的理由。

执行前按实际闭包大小及文件系统可用空间检查 staging、密文、数据库和恢复副本的容量与余量；不能采用固定 GB 阈值。归档逐文件完整流式校验，不为验证再生成一份完整解密树。恢复解包只作用于私有目录，并在完整校验 tar header 和 payload 后恢复已登记权限，再执行原路径目标复制。任何来源变化、漏项、额外成员或摘要冲突均失败且保留原件。

Windows 恢复验证继续要求任务 Disabled、端点停止、CAD 无所有权、身份及 ACL 正确。成功回执返回实际 state 对象，Linux 重算 canonical JSON 摘要；任务 semantic hash 保持原算法。只有真实 PS5.1 结构、Unicode、类型和深度往返以及双端实测通过，才能宣称该恢复契约完成。

端点令牌轮换在排空后更新两端受控副本，验证认证与配置身份再开启准入；飞书和 Git 凭据轮换同样使用受控配置切换与实测登录/访问。秘密值不进入日志、命令行或发行档案。

显示记录删除不回收存储。Linux 封存证据默认不自动删除，显式保留策略只作用于无引用
证据。旧 `/v1/jobs` cache import/release、import confirmation 和 cache-release
receipt 接口不进入生产部署；native operation create/upload/get/resume/cancel/ACK、
sealed archive/receipt 消费、独立 `admit_capture`/`verify_transfer` 和资源收口保留。
已被运行、发布或工程批准引用的文件不得自动清除。日志提供稳定错误码、操作身份、对象
与证据位置；日志轮换不删除封存证据。

## 端点停止与重启（settle 规程与锁拒绝）

停止 Windows 端点后等待端口无监听且无自有写入进程残留，超时即停止失败；随后启动任务并做 **≥3 分钟存活校验**：任务 Running、配置端口的监听进程身份连续、经令牌认证的 `/health` ready。启动若因所有权锁不可用被拒绝，先核对锁属主和进程；确认上一自有端点已结束后，等待 **≥60 秒再重试一次**并记录。无法证明锁属主已结束或再次拒绝时停止切换并排查，不能杀死未知进程或删锁强行启动。Windows 离线或隧道重试均如实记为不可用；双端未完成身份与就绪核验时不能开放准入或报告部署成功。
