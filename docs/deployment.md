# 部署指南

部署由一台 Linux 平台主机和一个串行 Windows 原生执行端组成。用户仅访问一个 HTTPS 操作台；Airflow、数据库、内部 API 和原生端点不直接向用户开放。

Linux 承担全部平台与可移植工作。Windows 只安装原生运行依赖和已资格化的 SolidWorks，运行在专用登录用户的交互式桌面中；不配置模型仓库、发布目标、Git 凭据、报告或 MuJoCo。

## 开发与发行链路

所有发布包必须从已合入受保护 `main` 的固定干净提交构建，开发与发行按以下顺序执行：

1. 设计总师确定设计、要求和验收标准。
2. 实现总工组织实现与测试，提交 PR。
3. 设计总师审核固定 PR head，确认满足要求及必需检查后，亲自合入 `main`。
4. 从该 `main` 提交构建完整发行，记录源码提交、发行清单和各资产摘要。
5. 将同一发行部署到 Linux 与 Windows，核验双端实际运行身份，完成真实端到端验收。

`description-pipeline` 与 `description` 仓库向 `main` 的合并只由当前设计总师根会话执行；实现总工和 workers 不得合并、直接推送 `main` 或开启自动合并。设计审核通过不转授合并权限。开发分支或 PR head 的隔离测试产物不得作为正式发行或部署包；禁止先发布后补合并及线上补拷代码绕过此流程。构建成功、组件测试、服务健康和手动导出不能替代真实端到端验收。

本次真实端到端须保留与固定手工参考的方向对比，但仅方向符号不一致不阻塞发行、部署或验收；实际驱动／编码器正向未建立时如实显示未验证资格及 warning。CAD 轴向、对象绑定、坐标转换、原生运动或来源审计的真实缺陷，以及命名、限位和其他必需检查仍阻塞。不能把该非阻塞条款扩大为免除独立 CAD 验证、同版双端身份核验或真实端到端运行，也不能以手工符号替代生产输入。

## 主机准备

| 主机 | 必需条件 |
|---|---|
| Linux | 发行清单支持的 x86_64 Linux、CPython、PostgreSQL、反向代理、Git/GitHub CLI、用户级 systemd；可访问 Windows SSH、飞书与模型仓库 |
| Windows | 发行清单支持的 Windows、CPython x86_64、licensed SolidWorks 与原生 API；Windows PowerShell 5.1 Desktop（`powershell.exe`）、OpenSSH Server、专用登录桌面和独立工程暂存目录 |
| 外部服务 | 可信 TLS、飞书企业应用及批准租户、私有模型仓库与存在的集成 base |

发行物的 `release.json` 是支持矩阵与依赖身份的权威：两端源码发行身份一致，角色依赖分别锁定。Linux 消费者验证无渲染，不要求桌面、GPU 或图形驱动。Windows 服务不能运行在 Session 0，不能复用工程师的交互 CAD 会话。

Windows 安装与生命周期脚本须在 Windows PowerShell 5.1 Desktop 上通过语法、API 及实际入口验证，保留严格 JSON 和重复键拒绝语义；Linux 上的 `pwsh` 测试不能代替该兼容性证明。新发行进入部署窗口前，先在目标主机用 `powershell.exe` 完成不操作 CAD、不修改活动服务的兼容性预验。

## 配置权威

Linux 使用一份私有 `platform.json`。schema 拒绝未知键、不安全路径、不完整目标和冲突端口；秘密只按文件引用。安装将其渲染为不可变的配置快照，记录摘要；操作台、Airflow 和维护命令读取同一快照。Airflow Connection 只引用该配置身份，不能再维护一份路由或凭据副本。

`solidworks_windows` 连接的 `host`、`password` 为空，`port` 为 `null`，`extra` 恰含 `config_identity`；该引用只含快照的 `schema_version`、绝对 `path` 和 `sha256`。消费者按同一契约检查受控目录、普通非链接文件、`0600` 权限、快照格式及字节摘要。新接纳再核对当前安装记录；已冻结运行继续读取自己的配置身份，不能回退到当前连接的路由。

安装、晋升和回退在关闭准入期间写入对应配置引用，完整核验后才能开放准入；失败保留原步骤证据。端点地址、目录、产品路由及凭据文件引用均从快照解析，不复制到连接字段。令牌轮换只更新受控凭据文件并验证认证，不重建旧式连接。

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

配置、状态、上传、交付、临时工作区和发行安装使用专用目录，禁止使用主目录/操作系统根、目录重叠、符号链接或 junction。Linux 私有配置和秘密文件权限为 `0600`，私有目录为 `0700`。Windows 受管路径使用 protected ACL，仅允许 `Administrators`、`SYSTEM` 与受控的本地管理员 service SID 的 `FullControl`；该 service SID 是已批准的运行时身份，不把管理员运行宣称为 least privilege。受管根目录和共同父目录的 ACE 必须对新建目录与文件可继承，bootstrap、stage、promote、rollback 在写入前保存完整 ACL，写入和服务访问检查失败时恢复 root、marker、现有子项及本次新建子项的 ACL 后 fail closed；不得让新 generation 或后续目录形成空 DACL。移除 `Authenticated Users`、`Users`、`Everyone` 等普通主体；日志只展示标识、摘要和路径，不展示秘密值。

产品目标、仓库与 base 仅在 Linux 维护。Git 凭据属于 Linux 服务账户；模型仓库来源必须与目标一致，发布使用受管理的干净工作区。模型工程值以封存 CAD 为依据，允许版本化配置承载命名、方向及数值表达规则；配置不得抄入手工结果或覆盖原生事实。仓库规则须允许服务主体向受管理候选分支追加提交和创建 PR，禁止非 fast-forward；凭据权限限定于配置目标，工程集成 base 保持独立评审。PostgreSQL 的平台与 Airflow 角色只能迁移各自 schema，连接预算及恢复目标在部署验收中固定并验证。

## 安装 Windows 执行端

本次发行支持已完成受控初始化的 Windows 端点升级：目标已有受管管理员交互式执行账户、Scheduled Task、目录及 ACL、端点配置、令牌和安装身份。按用户 2026-10-21 决定，端点继续在已受管的管理员账户下运行；部署和恢复核验必须核验账户为本地、启用、属于 `Administrators` 且任务使用最高可用令牌，不得把该运行方式表述为 least privilege。全新 Windows 主机初始化及 Session 0 broker 不在本次支持范围，不能用依赖安装成功或手工启动端点代替完整初始化资格。既有端点的任务、配置、令牌、ACL 和安装身份恢复仍须实际验证。

受管共同父目录必须通过同版 `deploy/windows/bootstrap-managed-root.ps1` 完成一次性初始化。引导入口只在当前管理员执行身份与任务运行账户一致时运行，拒绝盘符根、共享系统/用户目录、重解析路径和缺失或冲突的 marker；它事务性写入唯一的 `.description-managed-root.json`，将父目录和 marker 设为 protected 的 `Administrators`/`SYSTEM` ACL，并在任一步失败时恢复原 ACL、删除本次新建 marker。`stage` 只接受已经核验的 marker，不自行选择或创建共同父目录。缺少 marker、普通组 ACE、服务身份或任务令牌资格不符时，Windows 安装保持拒绝。

内嵌虚拟组件随同版原生端点与 Linux 验证器部署，不增加服务、环境变量或配置字段。Windows 读取受控会话中的组件及所属装配绑定，Linux 按封存契约独立验证；不得将 `VC~~` 临时文档复制成上传来源或配置额外搜索目录。

校验 Windows 离线运行档案与发行清单摘要，由受支持的 `stage` 在新安装代目录建立运行环境并安装 hash-locked 依赖，再通过 `promote` 切换既有受管任务。禁止向活动运行目录直接安装依赖或手工替换代码。`doctor` 验证安装身份、最小角色依赖及 SolidWorks/COM 就绪；实际作业仍须检查文件和会话。端点令牌的两端副本受控保存，原生端点只绑定 loopback：

Windows 端点的新 generation 只使用解释器入口 `runtime\Scripts\python.exe`，参数固定为 `-I -B -m description_pipeline serve --config "<generation>\endpoint.json"`；其中 `-B` 禁止解释器向受管 generation 写入字节码缓存。stage receipt 必须声明 `launch_contract=python_module_v1`，promote 必须拒绝缺失或不同契约的候选。为完成从已部署 `140d39e5` generation 的一次性安全迁移，验证只可接受该固定 source commit 与 version 1.5.0 的精确旧形式 `-I -m description_pipeline serve --config "<generation>\endpoint.json"`；该兼容不得扩展到新 generation、其他来源或其他 action。不得依赖 pip 生成的 `description.exe` launcher；该 launcher 的代码完整性身份可能被主机 Device Guard/WDAC 策略拒绝，不能通过关闭策略、添加例外或复制旧可执行文件绕过。为完成从更早 generation 的一次性安全回滚，恢复只可接受无该契约历史 generation 的精确 `description.exe + serve --config "<generation>\endpoint.json"` 形式，并须记录 `legacy_pip_launcher_v0`；该过渡形式不得用于新 stage 或 promote。

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

`validate` 只检查静态配置，不验证发行文件、实际端点身份或运行健康。发行资产须单独核验；`install_record.py endpoint-identity` 将真实端点健康原件与发行清单交叉验证，`install` 再核验绑定输入，并在关闭准入的状态下安装锁定运行环境、渲染服务、初始化数据库及校验 DAG；不能改变正在运行的安装。`start` 先验证两端身份、服务及配置，再开启任务准入。数据库、内部服务和隧道不对用户暴露端口。

需要在服务启动后完成对账或维护验收时，使用 `start --keep-admission-closed --config /srv/description/platform.json`。它执行相同启动检查，但全程保持准入关闭，成功结果明确区分“服务已启动”和“准入已开放”。完成规范对账并通过结算门后，再执行普通 `start` 重验并开闸；不得手工改状态或先开闸再关闭。该选项不能用于其他生命周期命令。

首个受管代绑定真实目标发行的代码、运行环境和双端身份，不能为未受管的旧部署补造安装身份。切换前保存旧配置、服务绑定、原件和数据路径作为回退基线；回退不得丢弃已经发生的业务或对账事实。

安装记录明确管理的全部服务与路径，不依赖工作目录或个人工具位置。配置变化先渲染并验证新快照，再按受控切换流程应用；运行中的请求继续使用其固定配置身份。`status/health` 及提交准入核验安装记录、发行文件、配置快照、实际服务、Airflow 配置引用和 Windows 发行/契约/端口身份，端点认证同时证明令牌一致；漂移关闭新准入，健康查询不打开 CAD。

同版双端核验比较 `version`、`source_commit` 和 `source_sha256`。两端各自的 `install_id`、`config_digest` 必须匹配对应主机的安装记录和实际配置，不要求两端这些值相同；不能用同版发行身份代替每端的配置、角色和运行身份核验。

DAG 验收还须核对活动记录中的绝对路径与发行成员摘要、实际生效的 `core.dags_folder`、当前 `DagVersion` 和 `SerializedDag` 关联，以及序列化的文件和解析目录。唯一支持的 `dags-folder` 本地 bundle 使用空版本；不能因此省略已登记版本 UUID 与实际任务来源核对。真实监督任务日志记录加载的 DAG 路径、SHA-256 和 Python 包来源。历史目录即使存在同名字节，也不能代替当前发行绑定；身份不符时不得开放新准入。

## 飞书与权限

企业应用启用 Web app，主页使用配置的唯一 HTTPS origin，回调为 `<origin>/auth/feishu/callback`。应用版本向批准用户发布；浏览器须信任证书。配置精确租户 allowlist，管理员使用同一应用的显式 `open_id`，空列表不授予管理员权限。

应用凭据文件包含 `app_id` 和 `app_secret`。登录使用 OAuth state、PKCE S256、服务端 code exchange 和基本用户身份 API；租户与 app/tenant/open_id 共同校验。会话使用安全 cookie、CSRF 防护及明确过期；不申请离线授权或保留 refresh token。代理只信任本机入口并执行固定 origin。

租户成员可提交与查看共享结果；只有发起人和管理员可变更运行、重跑或管理显示记录。工程批准另有责任边界，登录不赋予该批准。[操作指南](operations.md)规定用户行为。

## 部署验收

开放服务前，在已安装发行上验证完整流程：

1. 两端安装身份及 `doctor` 通过，配置一致、TLS 可信、真实飞书登录成功；未批准租户和越权运行管理被拒绝。
2. 上传原生文件夹，选择主装配与产品目标，执行全部六阶段；请求、证据、报告、预览和 PR 绑定一致。
3. Linux 推导、生成、验证、报告与发布不调用 Windows；验证/发布重跑不创建 CAD 操作，Windows 安装不包含可移植运行依赖。
4. 同时提交、重复请求、断连与重启保持幂等；原生作业串行，未知 CAD 会话被拒绝且不被访问或终止。
5. 缺失/包外来源、错误配置、坏证据及验证失败阻断下游；诊断与未验证预览的身份清晰。发布失败保留验证结果，未知外部结果通过对账恢复。
6. 阶段重跑正确复用完整上游，保留原时间和生产身份；删除/恢复显示记录不移除证据。服务重启后请求、历史、准入和发布状态可恢复。
7. 使用实际连接写入端与正式健康/DAG消费者验证同一配置引用，并证明切换后已冻结运行的配置身份不变；演练 stage/promote/rollback 与第二主机切换失败，注入配置漂移，核对仓库规则允许追加且禁止非 fast-forward，完成凭据轮换及备份恢复；全部维护路径使用同一生命周期和资源域。

保留真实原生、身份、授权、失败和恢复证据。mock、健康绿标或能打开页面不能替代完整验收。

验收固定同一个已合入受保护 `main` 的提交。CI 的必需门包含锁定 Airflow 环境中的真实普通操作员提交/调度及发行完整性；模拟端点只覆盖运输与失败阻断。发布完整发行后，校验资产摘要并在两端按同一版本 stage/promote，不对活动发行目录补拷源码或脚本。最终记录绑定源码提交、发行清单、双端安装/配置、原始工程清单、运行、六阶段收据、独立报告、实际 URDF 预览和模型 PR。当前模型仓库为 `mimicverse/description`，集成 base 为 `main`；手工与历史 PR 不参与工程生成；本次正式产物还须按用户要求完成与固定手工版的对比，纯方向符号差异作为非阻塞项保留，其余一致性要求不变。参考结果不能代替 CAD、独立验证和真实端到端的必需门。

正式入口须验证普通操作员的必选目标及多行说明，并用浏览器实际加载绑定交付的 URDF/网格、执行关节交互。生成后自动显示预览；独立验证未通过时只提供明确标注的未验证诊断预览，不能使用通过绿标。页面阶段、检查计数和资格与同一封存结果一致。验收中的待执行项保持待执行，不因服务健康或历史运行通过而封板。

操作员在同一入口明确选择工程或检查预览用途；冻结记录使用唯一 `request/v2` 并保留选择。逐组核验输入、请求、运行、报告、实际关节预览与 `main` 候选 PR 的绑定，不能跨用途代跑。两种用途均以当前 CAD 和明确的适用规则建立交付；检查预览可按[用途契约](contracts.md#purpose)的 `inspection_rotation_full_turn/v1` 使用全周窗口，必须验证适用条件并显式显示原生范围缺项与策略来源，不得抄入手工结果或冒称机械限位。已有有效 CAD 范围不得被窗口覆盖。缺失驱动不显示为零能力；报告、预览和 PR 一致显示参数来源与资格。必需检查和原生事实要求不因用途减弱。不同用途的候选与发布恢复身份按[发布协议](contracts.md#publication)隔离。

方向验收绑定本次 CAD、规则版本、固定代码提交及实际交付：先核对全部关节的原值、作者声明绑定、必要坐标转换和未解决项，再通过门户预览执行有效范围内的非零运动，验证 CAD 运动语义、零位及范围的一致性。当前不发布基于右手定则或功能动作的新正向推导规则，历史试算不作为通过证据。生成与独立工程验证只取得 CAD 和规则输入，固定手工参考由最终对比单独读取，并保留参考不可访问及扰动反例。方向与手工参考的纯符号差异不阻塞发布；未解决的命名、范围或其他真实一致性缺陷，以及 CAD 轴向、坐标转换和原生运动错误仍阻塞。不得以试算、旧运行或健康状态替代发布验收。

<a id="lifecycle"></a>
## 维护、升级与恢复

生命周期命令为 `validate / install / start / drain / stop / status / health / reconcile / stage / promote / rollback`，统一读取同一配置及安装记录。`drain` 关闭新提交和 DAG 准入，保留在途执行；返回全部活动、未确认与发布中操作。`stop` 仅在已排空且状态已知时停止全部管理服务，禁止通过杀进程代替排空。

备份维护使用 `stop --keep-database` 停止受支持写者并保留 PostgreSQL，随后核验实际 Linux 服务和 Windows 任务停止状态。备份从 `drain` 前至归档、清单及收据完成一直持有状态根下的 `locks/maintenance.lock`；`start / drain / stop / reconcile / stage / promote / rollback` 使用同一锁。嵌套调用须验证继承的锁及属主，不能仅凭环境变量绕过加锁。

维护查询账户只获准读取准入与生命周期视图，无平台写入权或 Airflow schema 访问权。关闭与重开准入经平台受控事务入口执行，使用明确的写入授权；只读凭据不能执行这些变更。维护查询采用[架构定义的一致快照](design.md#持久化与一致性)，缺绑定、未知状态或权威不可读时拒绝停止和切换。

平台编排在启动与有界退避重试时对账，维护人员也可调用 `reconcile`。对账查询调度意图、阶段收据、Windows 操作及远端 Git/PR，确认已发生结果后登记或安全继续；不能重复未知副作用。未决状态未消除时，排空不算完成。

缺少调度绑定或平台漏记结果的已登记派发，按[恢复契约](contracts.md#recovery)由同一 `reconcile` 处理。先关闭准入，保全原运行/操作/阶段/事件、生命周期投影、实际 Airflow 全键及适用的原始故障证据；冻结失败且后继从未派发时保全 Airflow 原始诊断，已有原生执行时保全端点终态原件；只读预演输出唯一目标、证据摘要和精确预期差异。正式执行重新核验同一事实，仅补缺失绑定并登记已发生结果。先逐笔核对，再重复提交证明幂等，确认实际调度与业务均已结算，`report --require-settled --require-admission-closed` 才能通过。不得手工 SQL 改终态、删除记录、使用空的平行数据库、重跑 CAD 或豁免未知行来排空。

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

端点令牌轮换在排空后更新两端受控副本，验证认证与配置身份再开启准入；飞书和 Git 凭据轮换同样使用受控配置切换与实测登录/访问。秘密值不进入日志、命令行或发行档案。

显示记录删除不回收存储。Linux 封存证据默认不自动删除，显式保留策略只作用于无引用证据；Windows 已确认导入缓存的释放遵循[交换契约](contracts.md#exchange)。已被运行、发布或工程批准引用的文件不得自动清除。日志提供稳定错误码、操作身份、对象与证据位置；日志轮换不删除封存证据。

## 端点停止与重启（settle 规程与锁拒绝）

停止 Windows 端点后等待端口无监听且无自有写入进程残留，超时即停止失败；随后启动任务并做 **≥3 分钟存活校验**：任务 Running、配置端口的监听进程身份连续、经令牌认证的 `/health` ready。启动若因所有权锁不可用被拒绝，先核对锁属主和进程；确认上一自有端点已结束后，等待 **≥60 秒再重试一次**并记录。无法证明锁属主已结束或再次拒绝时停止切换并排查，不能杀死未知进程或删锁强行启动。Windows 离线或隧道重试均如实记为不可用；双端未完成身份与就绪核验时不能开放准入或报告部署成功。
