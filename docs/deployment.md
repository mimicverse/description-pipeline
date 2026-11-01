# 部署指南

可选评估由 `platform.json` 的 `operator.assessment` 控制，未配置时关闭。启用须完整提供私有 `python`、`capacity`（1–8）以及 `limits`，不从模型缺失值推断执行参数。派生环境显式记录关闭状态与空值，环境文件、数据读取和 shell 读取使用同一编码，不继承宿主的旧开关或管理员列表。

安装创建主原生池及已启用的评估池，健康检查只读核验相同容量，并包含 deferred 占用。启动从已核验 snapshot 选择 DAG，先暂停全部启用 DAG，完成双端身份和健康门后才开启；部分 unpause 失败会重新暂停全部 DAG，admission 保持关闭。辅助 DAG 文件及其实际 serialized identity 同样绑定当前发行，不能只凭主 DAG 通过。

启用评估的 consumer 首次启动时原子创建 `state_root/assessments`，要求当前服务用户所有、0700 且无链接祖先；默认关闭不创建资源。该目录是可重建的工作区，灾备保留数据库事实与完整 CAS 引用，不复制 scratch 作为业务状态。drain/stop/backup 同时核验辅助业务和原始调度证据，UNKNOWN 或未归还资源阻塞清场；恢复到空目标后由同一 consumer 重建私有工作区。

部署由一台 Linux 平台主机和一个串行 Windows 原生执行端组成。用户仅访问一个 HTTPS 操作台；Airflow、数据库、内部 API 和原生端点不直接向用户开放。

Linux 承担平台编排、工程模型、URDF/MJCF 生成、独立验证、报告和发布。Windows 运行原生 CAD 读取，释放 CAD 后完成原值序列化与证据封包。Linux Capture 核验传输与绑定，Generate 一次执行语义投影、惯量规范化和 STL 生成，Verify 从 raw 独立复算；MJCF 在 Linux 生成组件碰撞代理、编码并独立验证，Publish 组合两种格式。Windows 只安装原生运行依赖和已资格化的 SolidWorks，运行在专用登录用户的交互式桌面中；不配置模型仓库、发布目标、Git 凭据、报告或 MuJoCo。

## 开发与发行链路

所有发布包必须从已合入受保护 `main` 的固定干净提交构建，开发与发行按以下顺序执行：

1. 设计总师确定设计、要求和验收标准。
2. 实现总工组织实现与测试，提交 PR。
3. 设计总师审核固定 PR head，确认满足要求及必需检查后，亲自合入 `main`。
4. 从该 `main` 提交构建完整发行，记录源码提交、发行清单和各资产摘要。
5. 将同一发行部署到 Linux 与 Windows，核验双端实际运行身份，完成真实端到端验收。

`description-pipeline` 与 `description` 仓库向 `main` 的合并只由项目指定的唯一发布执行者执行；负责人和 workers 不得代为合并、直接推送 `main` 或开启自动合并。设计审核通过不转授合并权限。开发分支或 PR head 的隔离测试产物不得作为正式发行或部署包；禁止先发布后补合并及线上补拷代码绕过此流程。构建成功、组件测试、服务健康和手动导出不能替代真实端到端验收。

历史参考的方向对比仅用于独立参考测试，不进入生产或发布必经链；实际驱动／编码器正向未建立时如实显示未验证资格及 warning。CAD 轴向、对象绑定、坐标转换、原生运动或来源审计的真实缺陷，以及命名、限位和其他必需检查仍阻塞。不能把该非阻塞条款扩大为免除独立 CAD 验证、同版双端身份核验或真实端到端运行，也不能以手工符号替代生产输入。

## 主机准备

| 主机 | 必需条件 |
|---|---|
| Linux | 发行清单支持的 x86_64 Linux、CPython、正式默认 PostgreSQL 14（14.19 或更新的发行维护版本）、反向代理、Git/GitHub CLI、用户级 systemd；可访问 Windows SSH、飞书与模型仓库 |
| Windows | 发行清单支持的 Windows、CPython x86_64、licensed SolidWorks 与原生 API；Windows PowerShell 5.1 Desktop（`powershell.exe`）、OpenSSH Server、专用登录桌面和独立工程暂存目录 |
| 外部服务 | 可信 TLS、飞书企业应用及批准租户、私有模型仓库与存在的集成 base |

发行物的 `release.json` 是支持矩阵与依赖身份的权威：两端源码发行身份一致，角色依赖分别锁定。Linux 消费者验证无渲染，不要求桌面、GPU 或图形驱动。Windows 服务不能运行在 Session 0，不能复用工程师的交互 CAD 会话。

Linux 安装先校验同一发行的角色 ZIP，再将其中 wheels 提取到本次命令独占的临时目录；Airflow 与产品依赖安装使用 `--no-index` 和锁定摘要，成功或失败均清理该临时目录。CPython、PostgreSQL、代理、Git 与 SolidWorks 属于上表的主机准备条件，不包含在 Python wheelhouse 中。

Windows 安装与生命周期脚本须在 Windows PowerShell 5.1 Desktop 上通过语法、API 及实际入口验证，保留严格 JSON 和重复键拒绝语义；Linux 上的 `pwsh` 测试不能代替该兼容性证明。新发行进入部署窗口前，先在目标主机用 `powershell.exe` 完成不操作 CAD、不修改活动服务的兼容性预验。

## 配置权威

Linux 使用一份私有 `platform.json`。schema 拒绝未知键、不安全路径、不完整目标和冲突端口；租户与管理员列表的单项不得包含逗号或空白，避免环境变量序列化改变授权主体。秘密只按文件引用。安装将配置渲染为不可变快照并记录摘要；操作台、Airflow 和维护命令读取同一快照。Airflow Connection 只引用该配置身份，不能再维护一份路由或凭据副本。

环境派生统一产生 `repositories.json`、`targets.json`、`discovery.json` 和 `operator.env` 的精确内容；只有 emit 写入，安装态核验逐文件与快照派生字节比较，不修改文件，也不替换写入函数。三份 JSON 与 `resolved.env` 自洽不能替代对快照来源的核验。

该快照的唯一派生环境固定包含 `PYTHONDONTWRITEBYTECODE=1`，由 Airflow 和 Portal 的 systemd 服务直接加载；维护环境库在加载时同样设置该变量，防止直接诊断调用在发行目录生成缓存。服务不能依赖调用方 shell 的环境继承；发行目录仍执行完整成员与摘要核验，不豁免缓存文件。

`solidworks_windows` 连接的 `host`、`password` 为空，`port` 为 `null`，`extra` 恰含 `config_identity`；该引用只含快照的 `schema_version`、绝对 `path` 和 `sha256`。消费者按同一契约检查受控目录、普通非链接文件、`0600` 权限、快照格式及字节摘要。新接纳再核对当前安装记录；已冻结运行继续读取自己的配置身份，不能回退到当前连接的路由。

首次安装在关闭准入期间写入配置引用，完整核验后才能开放准入；失败保留本次证据并保持准入关闭。同版业务恢复沿用同一配置身份。端点地址、目录、产品路由及凭据文件引用均从快照解析，不复制到连接字段。令牌轮换只更新受控凭据文件并验证认证，不重建旧式连接。

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

配置、状态、上传、交付、临时工作区和发行安装使用专用目录，禁止使用主目录/操作系统根、目录重叠、符号链接或 junction。Linux 私有配置和秘密文件权限为 `0600`，私有目录为 `0700`。Windows 受管路径的权限边界由 protected ACL 建立，仅允许 `Administrators`、`SYSTEM` 与受控的本地管理员 service SID 的 `FullControl`；该 service SID 是已批准的运行时身份，不把管理员运行宣称为 least privilege。目录、配置、令牌、安装身份及其他稳定文件必须自身 protected，所有者为 `Administrators`。仅状态根目录直属的可变运行文件 `cad-ownership.json` 和 `cad-ownership.json.lease` 允许另一种等价形式：父状态根目录已核验为 `Administrators` 所有、自身 protected、恰有上述三主体的显式可继承 `FullControl`；文件也由 `Administrators` 所有，恰有同三主体的纯继承 `FullControl`，无显式、额外或拒绝 ACE。文件 ACE 的 inheritance 和 propagation 均为零，路径及其祖先不得为重解析点；文件自身 protected 的严格三主体形式同样有效。该规则允许原子替换后继承已验证的权限边界，部署验收和独立恢复验证使用同一规则；不得扩展到稳定文件或放宽父目录检查。受管根目录和共同父目录的 ACE 必须对新建目录与文件可继承，安装 helper 在写入前保存完整 ACL，写入和服务访问检查失败时恢复 root、marker、现有子项及本次新建子项的 ACL 后 fail closed；不得让新 generation 或后续目录形成空 DACL。移除 `Authenticated Users`、`Users`、`Everyone` 等普通主体；日志只展示标识、摘要和路径，不展示秘密值。

产品目标、仓库与 base 仅在 Linux 维护。Git 凭据属于 Linux 服务账户；模型仓库来源必须与目标一致，发布使用受管理的干净工作区。模型工程值以封存 CAD 为依据，允许版本化配置承载命名、方向及数值表达规则；配置不得抄入手工结果或覆盖原生事实。GitHub 普通 Git 仓库拒绝超过 100 MiB 的单个 blob；Publish 在创建出站 effect 前检查真实候选相对固定 base 的新增可达 blob，并报告超限对象、路径和字节，不能用压缩 bundle 大小替代单文件门。原生事实采用统一无损 gzip 封装，完整依据随交付保留，不引入 Git LFS 或包外 locator。仓库规则须允许服务主体向受管理候选分支追加提交和创建 PR，禁止非 fast-forward；凭据权限限定于配置目标，工程集成 base 保持独立评审。PostgreSQL 的平台与 Airflow 角色只能迁移各自 schema，连接预算及恢复目标在部署验收中固定并验证。

## 安装 Windows 执行端

Windows 使用同版 `deploy/windows/instance-lifecycle.ps1` 唯一公开入口：`install/start/drain/stop/status/health -Binding FILE`。执行账户为受控、启用的本地管理员账户，任务使用对应交互式身份；不把管理员运行宣称为最小权限。首次安装创建当前任务，不要求存在旧任务或旧发行。

Binding 只引用必要执行输入：`endpoint_config`、`release_file`、`task_name`、`task_path`、`service_account`、`python_executable`。端点配置和发行清单分别保持唯一权威，不复制它们的字段。安装从 release.json 定位并核验同目录 Windows 角色档案，使用 CPython 3.12 建立独立 runtime、安装 hash-locked 依赖并验证实际包身份。目标已有任务、安装或不明残留时拒绝覆盖。

`bootstrap-managed-root.ps1` 仅作为安装内部的提权 ACL helper，建立受管根及其权限标记；拒绝共享系统/用户根、重解析路径和身份冲突。端点令牌的两端副本受控保存，秘密不进入发行包和日志。端点仅监听 loopback。

当前控制脚本固定为 `<state_root>/instance-lifecycle.ps1`，输入引用固定为 `<state_root>/windows-binding.json`，由本次安装收据绑定实际字节。Linux 维护只调用这一入口。

首次 install 注册停止状态的任务；显式 start 才启动端点服务，不打开 CAD。任务 action 使用安装 runtime 的 Python，以 `-I -B -m description_pipeline.orchestration.windows serve --config` 启动。status/health 核验实际任务、执行账户、配置、发行与 listener 归属，不以端口有人监听替代身份。venv launcher 与 base Python 子进程按真实父子关系核验，不能把任意同参 Python 当作本任务。

任务定义身份覆盖完整 XML，仅排除由启停控制的直接 `Settings/Enabled`；ACL 独立绑定，原始 XML 摘要保留为安装证据。启用后、启动前再次核验定义与 ACL。安装收据使用 v2，不提供旧收据兼容；新发行须干净安装。

drain/stop 使用当前作业和 CAD 所有权事实；未决状态或外部占用不得当作已清场。停止需核验本任务进程和监听确已退出，不杀未知进程、不删除锁强行启动。失败保存本次执行证据，清理只作用于本次创建且可确认归属的资源。

停止成功要求任务为 Disabled，且监听、受管写入进程和 CAD 均已退出；非运行 status 同样拒绝受管写入进程残留。完整 `operatorctl health` 同时核验 Windows 生命周期与 Linux 服务健康，任一失败即失败，不能只凭端点 HTTP 健康或命令退出码认定整套部署成功。

内嵌虚拟组件随同版原生端点和 Linux 验证器部署，不增加服务、环境变量或搜索目录。Windows 读取组件及所属装配绑定，Linux 按封存契约独立验证，不将 `VC~~` 临时文档复制成工程输入。

## 安装 Linux 平台

配置服务账户的 GitHub 认证，确认目标仓库与 base 可读写。开放接纳前，按已核平台 snapshot 的 `derived.repositories` 为每个目标准备完整 Git 工作区；不得使用浅克隆，因为仓库封存必须能独立还原全部所需对象。核对实际 fetch/push remote、存在的 base ref/commit、干净工作树，并在该仓库的 local config 设置服务账户的 `user.name` 和 `user.email`。使用当前发行的仓库封存与还原入口完成隔离往返，核对还原的 base commit/tree；同时核算工作区、封存临时文件及上传所需空间。服务健康不代表这些业务资源已就绪。

配置专用 SSH key 和已核验 Windows host key；传输使用 SSH 隧道和端点令牌，内侧均为 loopback。拒绝跨主机重定向和未认证端点。

校验同版 Linux 离线运行档案、部署档案与全部摘要。首次安装须先用受支持的 CPython 3.12 和该发行的 Linux 角色档案建立独立引导环境；后续控制命令也须使用已核验的同版环境，不能假定系统 `python3` 已装有规范验证器。以下路径均为待替换的示例：

```sh
python3.12 -m venv /path/to/bootstrap-venv
/path/to/bootstrap-venv/bin/python -m pip install \
  --no-index --require-hashes \
  --find-links /path/to/linux-runtime/wheels \
  -r /path/to/linux-runtime/requirements.lock
export PATH="/path/to/bootstrap-venv/bin:$PATH"
```

启用 PICO 或 assessment 前，从同一 Linux 档案保留 `consumers/wheels/`，使用本次已安装产品的引导环境执行部署档案中的安装器。将同一 `release.json` 原字节复制到解包的部署根目录，与 `scripts/`、`requirements/` 同级；原六资产目录独立保留以供完整校验。安装器核对产品、部署资源、角色 lock、模型 wheel 和实际原生环境的身份。按实际启用角色分别执行：

```sh
/path/to/bootstrap-venv/bin/python /path/to/deployment/scripts/install_teleop_worker.py \
  --role pico --wheel /path/to/fixed-models/robot_teleop-0.3.0-cp312-cp312-linux_x86_64.whl \
  --wheelhouse /path/to/linux-runtime/consumers/wheels --destination /path/to/pico-runtime
/path/to/bootstrap-venv/bin/python /path/to/deployment/scripts/install_teleop_worker.py \
  --role assessment --wheel /path/to/fixed-models/robot_teleop-0.3.0-cp312-cp312-linux_x86_64.whl \
  --actuator-wheel /path/to/fixed-models/actuator_model-1.3.0-py3-none-any.whl \
  --wheelhouse /path/to/linux-runtime/consumers/wheels --destination /path/to/assessment-runtime
```

文件名与目标路径须替换为本次固定资产及全新私有目录。在 `operator.teleop.python` 或 `operator.assessment.python` 中引用对应环境；未启用的角色不安装。轻量设备 link 使用相同安装器的 `--role pico-link` 和 consumer 池，不提供模型 wheel。主平台的临时 wheelhouse 只用于主运行闭包，不混入或替代 consumer 池。失败环境保留供定位，不能直接复用未知残留目录。

在提取的同版部署目录执行唯一生命周期入口：

```sh
bash deploy/operator/operatorctl.sh validate --config /srv/description/platform.json
bash deploy/operator/operatorctl.sh install \
  --config /srv/description/platform.json \
  --install-id NEW_INSTALL_ID \
  --wheel /path/to/release/package.whl \
  --release-file /path/to/release/release.json
bash deploy/operator/operatorctl.sh start --config /srv/description/platform.json
bash deploy/operator/operatorctl.sh health --config /srv/description/platform.json
```

示例中的路径和安装标识须替换为本次发行的实际值。Linux 安装通过固定 Windows 控制入口读取实际 `status`，核验本次发行身份并直接生成安装记录所需身份，不接受手工提供的端点身份文件。Windows 须先完成同版安装；随后 Linux `start` 启动双端服务并核验真实 `/health`。不能手写身份、沿用旧端点收据或使用开发分支资产。安装使用同一发行目录中的 wheel 和清单；状态根、数据库端口和其他运行路径从当前平台配置及其受管安装记录解析，不从历史运行或个人目录猜测。

安装记录只在当前 runtime、数据库、DAG、配置和双端身份均核验后登记。安装先在私有临时目录形成同一记录，绑定并回读 Airflow Connection，成功后才发布正式安装记录；临时目录随命令退出清理。连接失败不能留下可用的正式安装身份，失败证据保全且准入保持关闭；未完成安装不得通过补写成功记录激活。

`validate` 只检查静态配置，不验证发行文件、实际端点身份或运行健康。`install` 在创建安装状态前，依据发行清单核验资产和完整部署文件集合、类型及摘要；随后在关闭准入的状态下安装锁定运行环境、渲染服务、初始化数据库及校验 DAG，并绑定实际 Windows `status` 身份。当前运行继续核验部署文件，不要求临时安装 wheelhouse 常驻。

`start` 先关闭准入并保持 DAG 暂停，验证 Airflow Connection、Linux 完整健康和 Windows 健康后才解除 DAG 暂停并开放准入。开放失败时尝试恢复 DAG 暂停，失败结果如实保留。HTTPS 健康检查以配置的证书为显式信任锚，校验证书有效性及配置 origin 的主机名；经本机回环连接也不关闭 TLS 验证。数据库、内部服务和隧道不对用户暴露端口。

需要在服务启动后完成对账或维护验收时，使用 `start --keep-admission-closed --config /srv/description/platform.json`。它执行相同双端健康与连接检查，全程保持 DAG 暂停和准入关闭；成功表示服务健康，不表示已开放新任务。完成规范对账并通过结算门后，再执行普通 `start` 重验并开放准入；不得手工改状态或先开放再关闭。该选项不能用于其他生命周期命令。

首次安装绑定本次发行的实际代码、运行环境与双端身份；旧现场的输入和失败证据由发布者单独保全，不导入历史运行包或为旧部署补造新身份。

安装记录明确管理的全部服务与路径，不依赖工作目录或个人工具位置。配置快照不可改写，当前安装拒绝配置漂移；运行中的请求继续使用其固定配置身份。`status/health` 及提交准入核验安装记录、发行文件、配置快照、实际服务、Airflow 配置引用和 Windows 发行/契约/端口身份，端点认证同时证明令牌一致；漂移关闭新准入，健康查询不打开 CAD。

同版双端核验比较 `version`、`source_commit` 和 `source_sha256`。两端各自的 `install_id`、`config_digest` 必须匹配对应主机的安装记录和实际配置，不要求两端这些值相同；不能用同版发行身份代替每端的配置、角色和运行身份核验。

Windows `/health.install_id` 必须位于顶层。模板的 expected `NativeTargetIdentity` 只来自
已核验部署登记，不从环境变量或当前 health 自报推导。安装和启动
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

Airflow 发行固定为 3.3.2。DAG 的七个 task 均使用 `StageTaskOperator`，当前固定
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
`description-airflow-triggerer.service` 与 scheduler/executor 一同安装、启动、停止、健康检查和同版恢复。triggerer 缺失、版本不符、未运行或 job heartbeat
不健康时部署保持失败；fake trigger、仅改 DAG 或只有同步 TaskFlow 不满足验收。

## 飞书与权限

企业应用启用 Web app，主页使用配置的唯一 HTTPS origin，回调为 `<origin>/auth/feishu/callback`。应用版本向批准用户发布；浏览器须信任证书。配置精确租户 allowlist，管理员使用同一应用的显式 `open_id`，空列表不授予管理员权限。

应用凭据文件包含 `app_id` 和 `app_secret`。登录使用 OAuth state、PKCE S256、服务端 code exchange 和基本用户身份 API；租户与 app/tenant/open_id 共同校验。会话使用安全 cookie、CSRF 防护及明确过期；不申请离线授权或保留 refresh token。代理只信任本机入口并执行固定 origin。

租户成员可提交与查看共享结果；只有发起人和管理员可变更运行、重跑或管理显示记录。工程批准另有责任边界，登录不赋予该批准。[操作指南](operations.md)规定用户行为。

## 部署验收

开放服务前，在已安装发行上验证完整流程：

1. 两端安装身份及 `doctor` 通过，配置一致、TLS 可信、真实飞书登录成功；未批准租户和越权运行管理被拒绝。
2. 上传原生文件夹，选择主装配与产品目标，执行全部七阶段；请求、证据、报告、预览和
   PR 绑定一致。Discover 与 Capture 分别核验本 operation archive、materialize 结果和
   生产身份与同一 SourceSet 原件血缘一致；SourceSet 与 prepared 保持不同摘要。两个
   operation 可以独立传输和物化，单 runtime/CAD owner 与 operation-local workspace
   不变；不得新增跨 operation source cache 或把内容复用当作工程资格。
3. Linux 推导、生成、验证、报告与发布不调用 Windows；验证/发布重跑不创建 CAD 操作，Windows 安装不包含可移植运行依赖。 外观验收须用本次原生事实证明实际交付面的对象、状态、RGBA 与无损分组；覆盖完整 public-QI 分类、Part face 到 body/feature 的原生归属、合法共享 identity/fragments，以及缺失、分歧和接口不兼容的拒绝。零参 COM 方法须按方法调用，live handle 不得进入深复制或封存事实。验证原生 activity 经既有 poll 到 Portal 的真实显示，activity 与阶段资格分别核验。隔离 Capture 与 Linux transfer 通过不能代替正式 Freeze 开始的七阶段验收。
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
6. 必要来源缺失、未授权包外来源、错误配置、坏证据及验证失败阻断下游；获准的已知缺失引用须保留详细 warning 后继续，诊断与未验证预览的身份清晰。
   发布失败保留验证结果，未知外部结果通过对账恢复。
7. 新 CAD 输入按 Freeze→Publish 完整接纳；七阶段均可通过统一的有界后继独立重跑，默认只执行所选阶段。新请求封存 `from_stage/through_stage`，复核必要前缀的原件、command/decision、producer、证书和完整 effect；不复制本次范围及其下游的旧成功。输入变化从受影响的最早阶段重新建立结果，独立新结果可继续后续验证及发布。已有 command 的安全恢复保留原 tool、target、effect 与原失败；UNKNOWN 先精确对账。服务重启仍从 canonical facts 与共同 projector 恢复，界面区分范围成功与完整交付，不以 skipped 或 diagnostic 取得正式资格。
8. 使用实际连接写入端与正式健康/DAG消费者验证同一配置引用，并证明切换后已冻结运行的配置身份不变；演练首次安装、启停和第二主机安装失败，注入配置漂移，核对仓库规则允许追加且禁止非 fast-forward，完成凭据轮换及备份恢复；全部维护路径使用同一生命周期和资源域。
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
    封包，语义投影、惯量规范化和 STL 物化只在 Linux Generate 执行一次；Windows
    不执行这些计算职责，Capture 接纳不重复投影。质量、质心及两种参考点的惯量须从同一实际原生
    物性对象读取；测试不得用 support fixture 预填缺失原值来替代生产读取。
12. 注入原生读取失败、原始矩阵和惯量缺失或篡改、非法数量、截断/尾随字节、非有限
    三角形、错误 occurrence/configuration/source scope、face span 遗漏或重叠、
    错误 placement 和 STL 派生字节。独立 consumer 必须从 raw 复算并拒绝，保留
    primary 错误及 partial facts；正例覆盖合法原生读取、释放后的离线转换和原值复核。
    raw/STL 一致性不替代当前 CAD 的独立工程验证。

保留真实原生、身份、授权、失败和恢复证据。mock、健康绿标或能打开页面不能替代完整验收。

验收固定同一个已合入受保护 `main` 的提交。CI 的必需门包含锁定 Airflow 环境中的真实普通操作员提交/调度及发行完整性；模拟端点只覆盖运输与失败阻断。发布完整发行后，校验资产摘要并在两端干净安装同一版本，不对活动发行目录补拷源码或脚本。最终记录绑定源码提交、发行清单、双端安装/配置、原始工程清单、运行、七阶段收据、独立报告、实际 URDF/MJCF 预览和双格式模型 PR。当前模型仓库为 `mimicverse/description`，集成 base 为 `main`；手工与历史 PR 不参与工程生成；历史手工版仅进入独立参考单元测试和回归测试，不作为生产值来源、验证真值或正式发布必经输入。参考测试按语义区分合法表示与真实工程回归；正式验收仍依当前 CAD、独立验证和真实端到端的必需门。

正式入口须验证普通操作员的必选目标及多行说明，并用浏览器实际加载绑定交付的 URDF/网格、执行关节交互。生成后自动显示预览；独立验证未通过时只提供明确标注的未验证诊断预览，不能使用通过绿标。页面阶段、检查计数和资格与同一封存结果一致。验收中的待执行项保持待执行，不因服务健康或历史运行通过而封板。

操作员在同一入口明确选择工程或检查预览用途；冻结记录使用唯一 `request/v2` 并保留选择。逐组核验输入、原工程请求、本次转换/发布请求、运行、两份独立证书、实际关节预览与 `main` 候选 PR 的绑定，不能跨用途代跑。MJCF 后继保留原工程请求字节，当前说明从本次 Publish intent 核对，不要求与父 `input/request.json.change_summary` 相等。两种用途均以当前 CAD 和明确的适用规则建立交付；检查预览可按[用途契约](contracts.md#purpose)的 `inspection_rotation_full_turn/v1` 使用全周窗口，必须验证适用条件并显式显示原生范围缺项与策略来源，不得抄入手工结果或冒称机械限位。已有有效 CAD 范围不得被窗口覆盖。缺失驱动按 `drive.missing_zero/v1` 填零占位，明确显示平台默认来源及驱动能力未建立；报告、URDF、预览和 PR 一致显示参数来源与资格。必需检查和原生事实要求不因用途减弱。不同用途的候选与发布恢复身份按[发布协议](contracts.md#publication)隔离。

从已有运行重跑阶段时，`/api/runs/<parent>/attempts` 提交 `from_stage/through_stage`，调用唯一 `load_production_stage_rerun(...)`。Freeze 只复用已核 originals；其他阶段沿同一来源职责核验实际必要前缀及 producer。所有入口仍经 `StageApplication.admit/dispatch`、共同 claim/fence 与 technical dispatch 授权。Portal 不构造 `VerifiedArtifact`，不创建第二 Factory，不把 GUI 的 eligible 或调度 skipped 当作资格。提交响应未知时保留同一正文、幂等键和原 DagRun，刷新后继续精确对账；不能编辑待确认正文并另建执行。

Discover/Capture 完整保留已接纳原件，不按组件图删减底层支持文件。输入覆盖需要重新建立时，从最早受影响的 Discover 发起新不可变请求，可复用已核实的完整 Freeze/CAS 原件，无需重新上传；旧失败、输入和证据保持不变。新的原生执行仍由唯一 CAD 操作者按当次 exact runner 授权执行，离线保全探针不能代替真实观察。

方向验收绑定本次 CAD、规则版本、固定代码提交及实际交付：先核对全部关节的原值、作者声明绑定、必要坐标转换和未解决项，再通过门户预览执行有效范围内的非零运动，验证 CAD 运动语义、零位及范围的一致性。当前不发布基于右手定则或功能动作的新正向推导规则，历史试算不作为通过证据。必须注入共享 `status=local_class_aligned` 而作者正向证据仍缺失的负例，证明 Discovery、Generate、独立验证、模型来源、URDF、报告、预览和 PR 均保留同一
`direction.semantics_unresolved` warning；同时核对 axis／xyz／rpy／limits、封存
Capture bytes 和 `machine_qualification` 不受 warning 影响。生成与独立工程验证只取得
CAD 和规则输入；固定手工参考仅由独立参考测试读取，测试覆盖缺失、错误版本与扰动反例，不进入实际生产或发布必经链。未解决的命名、范围或其他真实一致性缺陷，以及 CAD
轴向、坐标转换和原生运动错误仍阻塞。不得以试算、旧运行或健康状态替代发布验收。

<a id="lifecycle"></a>
## 运行维护与数据恢复

生命周期命令为 `validate / install / start / drain / stop / status / health / reconcile`，统一读取同一配置及安装记录。`drain` 关闭新提交和 DAG 准入，保留在途执行；返回全部活动、未确认与发布中操作。`drain`、`stop` 和成功显示共用唯一共同业务 projector 及真实 Airflow 关联，不得读取独立 SQL reducer 或仅凭 Freeze decision 推断正式运行成功。`stop` 仅在已排空且状态已知时停止全部管理服务，禁止通过杀进程代替排空。

备份维护使用 `stop --keep-database` 停止受支持写者并保留 PostgreSQL，随后核验实际 Linux 服务和 Windows 任务停止状态。备份从 `drain` 前至归档、清单及收据完成一直持有状态根下的 `locks/maintenance.lock`；`start / drain / stop / reconcile` 使用同一锁。嵌套调用须验证继承的锁及属主，不能仅凭环境变量绕过加锁。

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
不改变运行状态、阶段结论或证据。正式成功必须来自本次执行范围内的 formal stage 的 complete
decision 与满足的 effect ledger；diagnostic 目标与正式七阶段隔离。
关闭与重开准入只经窄 `AdmissionControlStore` 事务 API 执行，使用明确的写入授权；
operator 不构造 `PlatformStore`、artifact reader 或第二状态机。只读凭据不能执行这些
变更。维护查询采用[架构定义的一致快照](design.md#持久化与一致性)，缺绑定、未知状态
或权威不可读时拒绝停止和切换。



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

当前发行通过干净首次安装交付，不提供历史包升级和回滚。实际安装身份复用 runtime inventory，维护和 health 验证当前配置与派生值后执行。恢复仅处理同一发行的业务数据，通过 `recovery.py backup / restore` 使用独立保管的外部密钥和显式空目标计划；失败保全原件并保持准入关闭，不能覆盖已有业务数据或以重装代替对账。

备份前关闭准入、排空并确认无未决副作用，在同一维护锁内停止写者，取得平台与 Airflow schema 的同一数据库快照，再按该快照引用复制冻结原件、交付/诊断、安装记录、配置与密钥并核验清单。备份加密、访问受控，按部署恢复目标保留；同版恢复能力须经过实际演练。备份成功或中途失败后均明确报告服务停止与准入状态，由受控生命周期入口恢复运行。恢复在关闭准入状态下核验清单和引用，查询两端作业与远端发布结果后重建投影，先证明完整再开启服务。

首次安装与灾备恢复共用一份内部运行时安装脚本，只重建虚拟环境、安装锁定依赖和发行 wheel，并验证运行时。恢复复用归档中的配置与数据库事实，不执行首次安装的配置渲染、数据库迁移、pool 或登录表初始化，也不将临时解包目录写入运行配置。

恢复 runtime 必须用自身的 Python 3.12 虚拟环境完成 package identity、
`description`／`airflow` entry probe 与 `pip check`。Airflow CLI 探测使用自动清理的私有临时 home，不能在已恢复目录生成日志或配置。probe 从 caller 去除
`PYTHONPATH`、`PYTHONHOME`、`VIRTUAL_ENV`、`AIRFLOW_CONFIG` 和 `AIRFLOW__*` 覆盖，设置 `PYTHONNOUSERSITE=1`，并保留
`PATH`、`LD_LIBRARY_PATH`、`SSL_CERT_FILE` 等系统运行变量；解释器和
`pip check` 使用 `-I -B`。必须同时验证真实缺失依赖负例失败、caller package shadow
不可见、恢复包 member/source digest 属于该虚拟环境。不得通过增加 test-only package
或放宽 `pip check` 掩盖发行错误。

恢复工具只消费当前发行定义的清单、来源选择和目标计划，不提供历史格式转换。
在状态目录创建、解包和目标写入前验证输入契约与发行身份。目标计划除了固定绝对路径和清单摘要，
还必须带同一 typed fact-root 与来源范围的闭包摘要；恢复完成收据和 marker 的绑定必须
一致，finalize 再次核验后才能启动服务。

正式目标计划统一由同版
`recovery.py target-plan --manifest <当前发行清单> --host-id <目标身份> --postgres-config <目标数据库参数JSON> --out <新计划>`
生成。数据库参数必须显式给出 `bin_dir`、`library_dir`、`socket_dir`、`data_dir`、
`port` 和 `bootstrap_role`。生成器先验证真实清单和外部摘要，再复制 closure、原绝对
root 路径及 Windows 身份，生成当前契约的计划并复验；输出必须是新文件。任何其他 target-plan
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
重发。状态侧只保存当前安装记录、配置 snapshot 与必要派生文件，核验原路径、权限和摘要；不保存历史晋升链和运行包。

正式部署使用 PostgreSQL 14 和发行内的安装脚本布局；同版数据备份与恢复在真实 PostgreSQL 14 上验证。CI 中 PostgreSQL 16 的结果只证明其测试范围，不替代正式环境验收。

执行前按实际闭包大小及文件系统可用空间检查 staging、密文、数据库和恢复副本的容量与余量；不能采用固定 GB 阈值。归档逐文件完整流式校验，不为验证再生成一份完整解密树。恢复解包只作用于私有目录，并在完整校验 tar header 和 payload 后恢复已登记权限，再执行原路径目标复制。任何来源变化、漏项、额外成员或摘要冲突均失败且保留原件。

Windows 数据恢复由 `recovery.py` 负责，生命周期脚本不另设恢复命令。恢复到空目标后，通过同版 `install` 重建运行环境和 Disabled 任务，保留已验证的安装身份与配置，再由 `status` 核验端点停止、CAD 无所有权、任务状态、身份及 ACL；不自动启动。安装身份文件和安装收据必须同时存在且匹配，或同时不存在用于新装；单边残留须拒绝并保全。只有真实 Windows PowerShell 5.1 与双端实测通过，才能宣称该恢复契约完成。

PowerShell 与 SCP 的完成、非零退出及运输异常保留原始输出、真实返回码和本次命令身份；超时不推断远端结果。失败备份清场以本次独占 Windows 下载目录的实际内容为依据：已有数据或目录读取状态不明时保留原件，不以异常中缺少某个标记作为删除依据。

端点令牌轮换在排空后更新两端受控凭据文件，验证认证与配置身份再开启准入；飞书和 Git 凭据轮换同样保留既有配置引用并实测登录或访问。秘密值不进入日志、命令行或发行档案。

显示记录删除不回收存储。Linux 封存证据默认不自动删除，显式保留策略只作用于无引用
证据。旧 `/v1/jobs` cache import/release、import confirmation 和 cache-release
receipt 接口不进入生产部署；native operation create/upload/get/resume/cancel/ACK、
sealed archive/receipt 消费、独立 `admit_capture`/`verify_transfer` 和资源收口保留。
已被运行、发布或工程批准引用的文件不得自动清除。日志提供稳定错误码、操作身份、对象
与证据位置；日志轮换不删除封存证据。

## 端点停止与重启（settle 规程与锁拒绝）

停止 Windows 端点须证明端口无监听、自有写入进程退出，超时即停止失败。原生 worker 关闭使用已有有界等待；worker 未结束时返回未决错误，不提前释放进程仍持有的所有权。随后由正常 `start` 核验任务、监听进程身份、令牌认证和双端健康后开放准入。锁不可用时核对实际属主和进程，只有原持有者已结束且锁已释放才可受控重试；等待时长不能代替释放证据，也不能杀死未知进程或删除锁强行启动。Windows 离线或隧道重试如实记为不可用；双端未完成身份与就绪核验时不能报告部署成功。

## MJCF 开发、资产与正式验收差量

Linux 发行必须包含锁定的 MuJoCo 运行依赖、官方 WASM/JS、许可证及完整浏览器资源，逐文件绑定实际包与服务字节。Windows 不新增 MuJoCo 或碰撞依赖。转换和独立检查各在有 CPU、内存及时限的子进程执行。默认组件凸包策略为 config schema 2、256顶点预算、相对源组件凸包 `0.00025 m` 质量门，每次独立计算；不冒称凹 CAD 表面完整等价。

正式验收补充：新 Freeze 完成七阶段并交付 URDF/MJCF；七阶段各自在独立进程和实际 Airflow 中按 from=through 执行，核验正确复用前缀、未执行后缀与真实 producer。实际走通独立 Generate→Verify→MJCF→Publish 链，及明确延续到下游终点的范围；Generate 以后的重算不需要 CAD endpoint/token。覆盖坏来源、篡改后重绑、失败、取消、UNKNOWN、错误 skipped、错 tool/target、并发和丢回执；旧下游成功不能放行新发布。GUI、CLI、receipt 与共同 execution_scope 一致，单范围完成不冒称交付完成。

浏览器对同一交付实际加载 URDF 与 MJCF，核验 visual/collision/overlay、非零关节调姿、播放/暂停、单步、复位、下载和摘要拒绝；碰撞显示为引擎实际接触代理，交互不回写结果。独立 worktree 的 8878 开发实例使用离线夹具、独立 PostgreSQL/Airflow/CAS/cookie 和受控 Git/PR，仅建立开发证据；不能代替固定 main 同发行的真实 CAD、正式调度器、双端身份、独立审查及发布验收。
