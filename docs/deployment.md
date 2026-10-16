# 部署指南

部署由一台 Linux 平台主机和一个串行 Windows 原生执行端组成。用户仅访问一个 HTTPS 操作台；Airflow、数据库、内部 API 和原生端点不直接向用户开放。

Linux 承担全部平台与可移植工作。Windows 只安装原生运行依赖和已资格化的 SolidWorks，运行在专用登录用户的交互式桌面中；不配置规格库、模型仓库、发布目标、Git 凭据、报告或 MuJoCo。

## 主机准备

| 主机 | 必需条件 |
|---|---|
| Linux | 发行清单支持的 x86_64 Linux、CPython、PostgreSQL、反向代理、Git/GitHub CLI、用户级 systemd；可访问 Windows SSH、飞书与模型仓库 |
| Windows | 发行清单支持的 Windows、CPython x86_64、licensed SolidWorks 与原生 API；OpenSSH Server、专用登录桌面和独立工程暂存目录 |
| 外部服务 | 可信 TLS、飞书企业应用及批准租户、私有模型仓库与存在的集成 base |

发行物的 `release.json` 是支持矩阵与依赖身份的权威：两端源码发行身份一致，角色依赖分别锁定。Linux 消费者验证无渲染，不要求桌面、GPU 或图形驱动。Windows 服务不能运行在 Session 0，不能复用工程师的交互 CAD 会话。

## 配置权威

Linux 使用一份私有 `platform.json`。schema 拒绝未知键、不安全路径、不完整目标和冲突端口；秘密只按文件引用。安装将其渲染为不可变的配置快照，记录摘要；操作台、Airflow 和维护命令读取同一快照。Airflow Connection 只引用该配置身份，不能再维护一份路由或凭据副本。

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

配置、状态、上传、交付、临时工作区、发行安装和规格库使用专用目录，禁止使用主目录/操作系统根、目录重叠、符号链接或 junction。Linux 私有配置和秘密文件权限为 `0600`，私有目录为 `0700`；Windows 使用仅执行用户可读写的 ACL。日志只展示标识、摘要和路径，不展示秘密值。

产品目标、仓库与 base 仅在 Linux 维护。Git 凭据属于 Linux 服务账户；模型仓库来源必须与目标一致，发布使用受管理的干净工作区。规格库只解析 CAD 明确引用的不可变版本，不能被上传内容覆盖。仓库规则须允许服务主体向受管理候选分支追加提交和创建 PR，禁止非 fast-forward；凭据权限限定于配置目标，工程集成 base 保持独立评审。PostgreSQL 的平台与 Airflow 角色只能迁移各自 schema，连接预算及恢复目标在部署验收中固定并验证。

## 安装 Windows 执行端

校验 Windows 离线运行档案与发行清单摘要，提取到独立发行目录并安装其 hash-locked 依赖：

```powershell
py -3.12 -m venv C:\description\runtime
C:\description\runtime\Scripts\python.exe -m pip install --no-index --require-hashes --find-links wheels -r requirements.lock
C:\description\runtime\Scripts\description.exe doctor
```

`doctor` 验证安装身份、最小角色依赖及 SolidWorks/COM 就绪；实际作业仍须检查文件和会话。为端点生成独立随机令牌，以受控方式在 Linux 保存副本。原生端点只绑定 loopback：

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

在专用登录桌面启动：

```powershell
C:\description\runtime\Scripts\description.exe serve --config C:\description\endpoint.json
```

验收后以同一账户的登录 Scheduled Task 启动同一命令，保持桌面登录和主机唤醒。每个执行端只使用安装记录绑定的配置和 state root，执行期间无独立 CAD 活动；就绪检查发现外部会话时显示不可执行原因，不能关闭该会话。保持时钟同步、桌面有效和磁盘容量充足，系统更新/重启使用同一排空流程。所有启动与清理入口共用资源域，遵守[CAD 所有权契约](contracts.md#resources)。

## 安装 Linux 平台

配置服务账户的 Git 身份及 GitHub 认证，确认目标仓库与 base 可读写。配置专用 SSH key 和已核验 Windows host key；传输使用 SSH 隧道和端点令牌，内侧均为 loopback。拒绝跨主机重定向和未认证端点。

校验同版 Linux 离线运行档案、部署档案与全部摘要。在提取的部署目录执行唯一生命周期入口：

```sh
bash deploy/operator/operatorctl.sh validate --config /srv/description/platform.json
bash deploy/operator/operatorctl.sh install --config /srv/description/platform.json
bash deploy/operator/operatorctl.sh start --config /srv/description/platform.json
bash deploy/operator/operatorctl.sh health --config /srv/description/platform.json
```

`validate` 只检查静态配置和发行身份。`install` 在关闭准入的状态下安装锁定运行环境、渲染服务、初始化数据库并校验 DAG；不能改变正在运行的安装。`start` 先验证两端身份、服务及配置，再开启任务准入。数据库、内部服务和隧道不对用户暴露端口。

安装记录明确管理的全部服务与路径，不依赖工作目录或个人工具位置。配置变化先渲染并验证新快照，再按受控切换流程应用；运行中的请求继续使用其固定配置身份。`status/health` 及提交准入核验安装记录、发行文件、配置快照、实际服务、Airflow 配置引用和 Windows 发行/契约/端口身份，端点认证同时证明令牌一致；漂移关闭新准入，健康查询不打开 CAD。

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
7. 演练 stage/promote/rollback 与第二主机切换失败，注入配置漂移，核对仓库规则允许追加且禁止非 fast-forward，完成凭据轮换及备份恢复；全部维护路径使用同一生命周期和资源域。

保留真实原生、身份、授权、失败和恢复证据。mock、健康绿标或能打开页面不能替代完整验收。

验收固定同一个已合入受保护 `main` 的提交。CI 的必需门包含锁定 Airflow 环境中的真实普通操作员提交/调度及发行完整性；模拟端点只覆盖运输与失败阻断。发布完整发行后，校验资产摘要并在两端按同一版本 stage/promote，不对活动发行目录补拷源码或脚本。最终记录绑定源码提交、发行清单、双端安装/配置、原始工程清单、运行、六阶段收据、独立报告、实际 URDF 预览和模型 PR。当前模型仓库为 `mimicverse/description`，集成 base 为 `main`；历史 PR 只作对照。

正式入口须验证普通操作员的必选目标及多行说明，并用浏览器实际加载绑定交付的 URDF/网格、执行关节交互。生成后自动显示预览；独立验证未通过时只提供明确标注的未验证诊断预览，不能使用通过绿标。页面阶段、检查计数和资格与同一封存结果一致。验收中的待执行项保持待执行，不因服务健康或历史运行通过而封板。

<a id="lifecycle"></a>
## 维护、升级与恢复

生命周期命令为 `validate / install / start / drain / stop / status / health / reconcile / stage / promote / rollback`，统一读取同一配置及安装记录。`drain` 关闭新提交和 DAG 准入，保留在途执行；返回全部活动、未确认与发布中操作。`stop` 仅在已排空且状态已知时停止全部管理服务，禁止通过杀进程代替排空。

维护查询账户只获准读取准入与生命周期视图，无平台写入权或 Airflow schema 访问权。关闭与重开准入经平台受控事务入口执行，使用明确的写入授权；只读凭据不能执行这些变更。维护查询采用[架构定义的一致快照](design.md#持久化与一致性)，缺绑定、未知状态或权威不可读时拒绝停止和切换。

平台编排在启动与有界退避重试时对账，维护人员也可调用 `reconcile`。对账查询调度意图、阶段收据、Windows 操作及远端 Git/PR，确认已发生结果后登记或安全继续；不能重复未知副作用。未决状态未消除时，排空不算完成。

缺少调度绑定或平台漏记结果的已登记派发，按[恢复契约](contracts.md#recovery)由同一 `reconcile` 处理。先关闭准入，保全原运行/操作/阶段/事件、生命周期投影、实际 Airflow 全键及端点终态原件；只读预演输出唯一目标、证据摘要和精确预期差异。正式执行重新核验同一事实，仅补缺失绑定并登记已发生结果。先逐笔核对，再重复提交证明幂等，确认实际调度与业务均已结算，`report --require-settled --require-admission-closed` 才能通过。不得手工 SQL 改终态、删除记录、使用空的平行数据库、重跑 CAD 或豁免未知行来排空。

该能力属于固定候选的维护完整性，必须在合并前完成实现、隔离反例与真实数据库验证；生产执行只使用已验证的完整发行或其正式维护路径，不向活动安装目录补拷修复代码。

`stage` 负责两端候选：从校验档案建立独立 Linux/Windows 安装目录和配置，核验身份、角色依赖、DAG、存储 schema 与原生就绪，不改变活动指针或启动第二个 CAD 资源域。

Windows 候选配置由正式 `stage` 按当前端点 schema 确定性生成。现役配置中的已撤销发布路由键 `targets` 仅在候选生成时移除；其他未知键、重复键和不规范值均拒绝。根路径、令牌文件引用、监听配置及其余有效字段的值与含义保持不变，用既有规范 JSON 写者输出候选并记录变换及前后摘要；不要求保留候选的空白、键序或行尾。现役配置原件按字节封存供恢复，运行目录不热改，端点运行时不接收旧路由键。安装工具实测加载候选，`promote` 核对其摘要与 `stage` 记录一致，再以实际 `/health` 的配置和安装身份确认消费结果。

`promote` 依次记录：关闭准入并排空、建立可恢复备份、停止两端执行服务、执行平台和 Airflow 各自的已验证 schema 迁移、切换两端安装/配置指针、启动并核验身份、完成健康和验收、开放准入。每步固定前置条件与实际结果；任一主机或步骤失败，准入保持关闭，不能将部分切换报告为成功。

升级意图和每个切换步骤持久登记。失败记录已完成与未确认步骤，`rollback` 使用该记录恢复两端明确的上一发行和配置，再检查身份与状态；不能猜测目录或最新备份。代码回滚必须兼容当前持久状态，不能回退数据库而丢失已产生的运行、证据或发布记录。涉及存储 schema 的变化须提供备份、迁移验证及明确可恢复方案，准入在完成前保持关闭。

恢复 Windows 任务定义不等于可以启动端点。未知锁属主或观察器失败时保持停止；只有取得可启动的锁及属主证据才启动。恢复核验使用切换前绑定的上一代自身发行身份，不能拿候选身份核验旧版；上一代身份缺证时健康结论保持未知，准入保持关闭。

备份前关闭准入、排空并确认无未决副作用，取得平台与 Airflow schema 的同一数据库快照，再按该快照引用复制冻结原件、交付/诊断、安装记录、配置与密钥并核验清单。备份加密、访问受控，按部署恢复目标保留；升级前须有已验证的恢复演练。恢复在关闭准入状态下核验清单和引用，查询两端作业与远端发布结果后重建投影，先证明完整再开启服务。

端点令牌轮换在排空后更新两端受控副本，验证认证与配置身份再开启准入；飞书和 Git 凭据轮换同样使用受控配置切换与实测登录/访问。秘密值不进入日志、命令行或发行档案。

显示记录删除不回收存储。Linux 封存证据默认不自动删除，显式保留策略只作用于无引用证据；Windows 已确认导入缓存的释放遵循[交换契约](contracts.md#exchange)。已被运行、发布或工程批准引用的文件不得自动清除。日志提供稳定错误码、操作身份、对象与证据位置；日志轮换不删除封存证据。

## 端点停止与重启（settle 规程与锁拒绝）

停止 Windows 端点后等待端口无监听且无自有写入进程残留，超时即停止失败；随后启动任务并做 **≥3 分钟存活校验**：任务 Running、配置端口的监听进程身份连续、经令牌认证的 `/health` ready。启动若因所有权锁不可用被拒绝，先核对锁属主和进程；确认上一自有端点已结束后，等待 **≥60 秒再重试一次**并记录。无法证明锁属主已结束或再次拒绝时停止切换并排查，不能杀死未知进程或删锁强行启动。Windows 离线或隧道重试均如实记为不可用；双端未完成身份与就绪核验时不能开放准入或报告部署成功。
