# SolidWorks 来源适配器（Windows 采集端）

[English](solidworks.en.md) · 中文

第一次使用请先按[从 SolidWorks 装配到第一个模型 PR](../solidworks-first-use.md)完成安装、连接、定义和提交。本文维护来源格式、部署接口与验收合同。

装配结构、配置、材料、质量属性和几何只能由 SolidWorks 原生 API 读取，因此采集收敛为一个
**固定版本、运行在已登录桌面会话**内的 worker；规范化、构建与验收由统一工具链完成。

## 组成

| 位置 | 作用 |
|---|---|
| `.../solidworks/native.py` | 原生 API：只读打开文档，读组件、质量属性、材料、坐标系与几何 |
| `.../solidworks/isolation.py` | 作业自有 CAD 进程（Windows Job Object），按 PID 绑定 COM 并释放句柄 |
| `.../solidworks/freeze.py` | 依赖收集、读取、几何、证据与快照提交 |
| `.../solidworks/scene.py` | `load_scene` / `normalize_scene`：原始读数 → `description.scene/v1` |
| `.../solidworks/jobs.py` | 持久作业、事件与心跳、attempt、重启恢复、看门狗 |
| `.../solidworks/worker.py` | 仅回环的作业 API（`/health`、`/doctor`、`/jobs`、`/jobs/<id>/*`、`/maintenance`） |
| `.../solidworks/deploy/worker.ps1` | 部署入口（随包分发）：Install / Start / Stop / Doctor / Update / Rollback / Status |

路径前缀均为 `src/description_pipeline/sources/`。

## 配置合同（`config/robot.yaml` 的 `source` 映射）

```yaml
source:
  provider: solidworks
  assembly: "D:/models/robot.SLDASM"   # 顶层装配；绝对路径
  configuration: "Default"             # 必填；不取"当前活动配置"
  allowed_roots: ["D:/models"]         # 依赖必须落在这里，否则拒绝冻结
  require_saved: true                  # 固定为 true；适配器从不保存原件
  geometry: { enabled: true, format: stl_binary }
  coordinate_systems: [CS_head, CS_arm]
  bodies:                              # 原生实体 → link 的显式归属
    - { id: trunk, name: trunk_link, components: ["trunk__1", "battery__1"] }
    - id: head
      name: head_link
      components: ["head__1"]
      frame: { xyz: [-0.00204, 0, 0.126], rpy: [0, 0, 1.5708] }
  joints:                              # 关节几何必须显式给出，不从 mate 推断
    - id: head
      name: head_joint
      type: revolute
      parent: trunk_link
      child: head_link
      xyz: [-0.00204, 0, 0.126]
      rpy: [0, 0, 1.5708]
      axis: [0, 0, 1]
      limits: { lower: -1.5, upper: 1.5, effort: 3.0, velocity: 6.0 }
  frames:
    - { id: imu, parent: trunk_link, xyz: [0.0148, -0.002, 0.0675], rpy: [1.5708, 0, -1.5708] }
```

以上为结构示例，数值不可用于真实机器人。缺少 `bodies` 时快照记录原始读数（每个叶零件一个 link，
`kinematics: not_defined`）；缺少关节坐标/限位/轴直接报错，不编造默认值。采集输入是**磁盘上
已保存的文件**，不读取桌面会话中的未保存编辑。worker 在独立的 SolidWorks 进程中只读打开装配，
不会保存原件。
SolidWorks 的 `GetSaveFlag` 会记录在 `raw/document_state.json`、`raw/dependency_closure.json` 和
`evidence/collection.json` 中，Doctor 会将其作为提示显示。这个标志描述的是 worker 内存中的文档，
不能证明操作者有未保存编辑，也不阻断采集。若要将桌面会话中的修改纳入快照，先保存，再采集。

`body.frame` 是零位时 link 在装配坐标系中的位姿；关节 `xyz/rpy` 是子 link 在父 link 坐标系中的位姿。
两者必须描述同一个零位。漏写非零的 body frame 会使质量和几何被重复变换，独立世界坐标验算将拒绝该模型。

## 采集流程与快照

`freeze` 在临时目录内完成全部工作，验证通过后一次性改名到目标目录：

1. 在独立进程中只读打开原件，递归读取 `GetDocumentDependencies2`，记录完整文件清单与摘要。
2. 复制原始文件，在第二个独立进程中用 `ReplaceReferencedDocument` 重定位全部引用；只改副本。
3. 重开副本，核对顶层配置、每个实例的身份与配置、依赖是否全部落在快照内，再读取质量、
   坐标系与网格。
4. 复核原件与副本摘要，释放采集进程与文件句柄，随后提交快照。任一步失败都保留诊断，
   不发布半成品。

```
snapshot/
  manifest.json          # description.source/v1：identity / evidence_class / 逐文件 sha256
  scene.json             # description.scene/v1（含 provenance.expected_entities）
  raw/                   # 原始读数：scene_raw / mass_properties / coordinate_systems /
                         # document_state / dependency_closure / geometry
  evidence/              # 采集环境（SolidWorks 版本、worker 版本）与采集条件
  source/                # 完整原生文件副本，引用已重定位并独立重开验证
  geometry/*.stl         # 逐组件几何（完整二进制 STL）
```

每个采集进程启动前加入 Windows Job Object，COM 只绑定该 PID；正常结束、失败或超时都只清理
本作业的进程树，不附着、不终止操作者的 SolidWorks。

`raw/mass_closure.json` 记录装配文档自身的读数与叶读数之外，还记录 `component_context`：逐组件
实例的装配上下文质量与三个 override 标志；叶/文档质量仍单列。汇总只使用**互不重叠的顶层
（depth 0）行**，嵌套行只用于发现被"干净父级"掩盖的 override。节点覆盖由叶实例的**全前缀闭包**
推导：祖先行缺失、父行不存在、重复行、类型不符或未知多余行都会被拒绝；纯 CAD 还要求每个节点的
装配上下文质量与其"所选零件文档质量之和"在容差内一致。缺失/重复行、非布尔标志或深度与名称不符
时，独立校验判失败（`source.normalization.mass_closure`）。

## 质量与惯量合同

### 两种材料模式

`source.material_source` 只有两个取值，都不允许"悄悄用默认密度"：

| 模式 | 合同 |
|---|---|
| `cad`（默认） | 每个实体必须有**显式物理材料**（采集时报 `cad_material_provenance_missing`）；独立校验再复查：某组件仍带 `reference.material_assignment.unverified_reason` 且无文档化质量即判失败（默认密度 1000 kg/m³ 是占位值） |
| `documented_table` | `source.documented_masses` 必须**覆盖每个被纳入的组件**；缺一个即失败，表里出现未被纳入的组件（拼错、改名）同样拒绝 |

覆盖检查在三处独立执行：配置校验（`validate_source_config`）、生成侧（`build_scene`，按实际读数）、
独立校验（`source.normalization.mass_provenance`，details 给 `missing_declared` / `unknown_declared` /
`cad_mass_without_verified_material`）；校验侧不读生成器报告。

`material_source: cad` 还要求装配树里没有任何**组件级** override（质量、质心或惯量）：一旦记录到
任一 override，`source.normalization.mass_closure` 判失败——纯 CAD 只读零件文档，无法表达实例
覆盖。`documented_table` 只把它们作为提示，既不自动分摊也不改写声明质量；提示只陈述两个读数并
说明**不做因果推断**。没有该记录的历史快照维持原判。

### 声明质量的惯量模型

文档化质量只替换组件质量，惯量按 `scale = used_mass / CAD_mass` **整体缩放**，保留 CAD 几何的
均匀密度分布形状：

| 字段 | 值 |
|---|---|
| `provenance.mass_sources` | 组件质量来源（`documented` / `cad`） |
| `provenance.inertia_model` | `scaled_cad_uniform_density` |
| `provenance.inertia_model_scope` | 适用范围（打印件可作量级估计；目录件不成立） |
| `provenance.declared_masses[]` | 逐组件 `raw_mass_kg` / `used_mass_kg` / `scale` / `reason` / `evidence` |

独立校验在 `source.normalization.mass_provenance` 记录同一标注（`inertia_model` /
`inertia_model_scope` / `scaled_components`）。来源侧只做整体缩放；实测质量、质心和惯量可通过
模型定义的[公共 `overrides`](../pipeline.md)覆盖，须附依据和证据，由独立校验复核。

### 惯性积符号

`IMassProperty2.GetMomentOfInertia(0)` 返回 `solidworks_positive` 记法的正惯性积：布局
`[[Ixx, Ixy, Izx], [Ixy, Iyy, Iyz], [Izx, Iyz, Izz]]`，交叉项是 `∫xy dm` / `∫zx dm` / `∫yz dm`；
标准惯性张量的非对角项是它们的相反数。合同：

* raw 的 9 个数原样保留，快照不改写；
* 生成端与独立校验端各自在**旋转与质量缩放之前**对交叉项取反（两处实现互不引用）；
* 原生读数缺 `product_convention` 时不猜，直接失败；夹具读数（`used_api == "fixture"`）
  按合同已是标准张量，不转换；
* 声明质量只做整体缩放，作用于已转换的张量，不重复取反。

审阅基准（解析长方体）：base 0.08×0.06×0.04 m / 1.4976 kg / RPY (0.2,-0.3,0.4)，
arm 0.025×0.04×0.10 m / 0.78 kg / RPY (-0.25,0.15,-0.35)；由原始 9 个数复算，与公布张量一致到
~5e-19 kg·m²。

### 证据归档与摘要绑定

```yaml
source:
  material_source: documented_table
  mass_evidence:
    reference: "材料规格（PLA@15% / 钢 / POM / 目录件数据表）"
    file: docs/provenance/mass-spec.json
    sha256: "<小写 64 位十六进制>"
  documented_masses:
    base-1:
      mass_kg: 2.5
      reason: "printed part: PLA@15% effective density"
      evidence: "base-1"                     # 文件内定位锚点
```

1. 只要声明了 `documented_masses`，`source.mass_evidence` 就必须是 `{reference, file, sha256}`
   （可选 `note`）；字符串形式被拒绝。
2. 每条声明质量的 `evidence` 是**该文件内的定位锚点**，独立校验要求该字符串确实出现在文件内容中。
3. 独立校验 `source.normalization.mass_evidence` 从模型仓库原文重算（模型根 = 从快照目录向上找到含
   `config/robot.yaml` 的那层；快照固定在 `<model>/sources/snapshots/<digest>/`），拒绝
   `evidence_not_bound` / `evidence_root_not_found` / `evidence_path_escape` /
   `evidence_file_missing` / `evidence_digest_mismatch` / `evidence_anchor_missing:*` /
   `evidence_anchor_not_found:*` / `uncovered_component:*`，details 给出 claimed/actual 摘要与逐锚点结果。
4. 采集侧（Windows worker）只校验形状（键、路径非空、摘要格式）；文件存在性、越界与摘要一致性
   都在模型侧复核。归档文件属于模型输入，必须随模型提交。

**边界**：锚点是子串检查（证明文件里有这条记录，不证明其物理正确）；摘要只证明内容未变，
不带签名或时间戳；该机制目前只覆盖质量/材料证据。

## 部署与运行

安装包 ZIP 的根目录即部署目录（`worker.ps1`、`worker-host.example.json`、`version.json`、
`requirements.txt`、`src/`、`wheels/`）。先在解压目录内复制并填写主机配置，再调用其中的
`worker.ps1`；`-Bundle` 指向原始 zip。Install/Update 需要 64 位十六进制摘要（主机配置
`bundle_sha256` 或 `-BundleSha256`），缺失或摘要不符时拒绝安装。

解压目录与 `install_root` 分开：日常使用解压目录中的 `submit.ps1` 和同目录的 `submit-host.json`。
`worker-host.json` 的 `assembly`、`configuration` 只供 Doctor 检查；每次采集的真实目标来自模型的 `config/robot.yaml`。
`install_root` 应保持较短，以免 PowerShell 解包触及旧版路径长度限制。

```powershell
$Bundle = '.\description-worker-0.3.24-windows-x86_64.zip'
$Deploy = '.\worker-0.3.24'
$Config = "$Deploy\worker-host.json"
Expand-Archive $Bundle -DestinationPath $Deploy -Force
Copy-Item "$Deploy\worker-host.example.json" $Config
notepad $Config  # 填写 install_root、user、python、jobs_root、bundle_sha256

# 在已登录桌面中安装并诊断（Install 会启动 worker；-Action Start 用于之后手动重启）
powershell -File "$Deploy\worker.ps1" -Action Install -Bundle $Bundle -Config $Config
powershell -File "$Deploy\worker.ps1" -Action Doctor -Config $Config
```

* 版本装在 `install_root\versions\<版本>`（每版本独立 venv），`current.json` 指向生效版本；
  升级 `-Action Update`（新包需带**它那一版的** `SHA256SUMS`，或显式 `-BundleSha256`；`worker-host.json`
  里的摘要属于已安装版本，缺这两者时拒绝升级）、回退 `-Action Rollback`，旧版本按 `keep_versions` 保留。
* **健康后生效**：新版本通过 `/health`（版本号一致）后才切换指针；启动或自检失败自动恢复上一版本。
  生效前必须先进入 `/maintenance`（允许条件见"作业、取消与恢复"）。
* 运行在**已登录桌面会话**：默认注册 `LogonType Interactive` 的 AtLogOn 任务（COM 需要桌面）；
  无权限注册时回退到 Startup 目录并在 `current.json` 记录 `mode`。SSH 新起的进程或服务式
  runner 不能采集 CAD。
* 依赖固定为包内 `.../solidworks/deploy/requirements/win-py312.lock`（公共核心 + 传递闭包 +
  `pywin32`、MuJoCo 及完整传递依赖，可在本机完成构建与验证）。受限网络先下载离线 wheel：

  ```bash
  python -m pip download --only-binary=:all: --platform win_amd64 --python-version 3.12 \
      --implementation cp --abi cp312 -d wheels \
      -r src/description_pipeline/sources/solidworks/deploy/requirements/win-py312.lock
  ```

  `Install` 用 `pip install --no-index --find-links wheels -r requirements.txt` 安装；安装包同时
  携带公共核心源码与部署资源，`tools/build_release.py` 打包前校验其存在（`--deploy-out DIR`
  可一并导出）。
* worker 只监听 `127.0.0.1`，且只回答启动时绑定的地址（`Host`/`Origin` 不匹配即 403，浏览器页面
  无法驱动它）；跨机调用走 SSH 隧道
  （`ssh -L 8765:127.0.0.1:8765 <host>`）。COM 调用全部串行在一条 STA 线程上，HTTP 线程不直接
  触达 COM。
* `Doctor`（CLI 与 `/doctor`）分开报告三态：**安装**（文件、venv、任务）、**worker 存活**
  （`/health`）、**CAD 可读取**（启动独立实例读取文档、组件与质量属性）；同时核验 CPython 3.12
  与必需模块（`yaml`/`jsonschema`/`numpy`，Windows 上还有 `win32com`），缺失即 `installed=false`。

### 一键提交候选：一次性设置 / 日常使用

默认在 SolidWorks 所在的 Windows 电脑完成采集、构建、MuJoCo 验证和 PR 提交。
完整安装与首次建模见[首次接入指南](../solidworks-first-use.md)。Windows 包包含完整公共工具及运行依赖；安装后可直接运行该版本 venv 中的 `python -m description_pipeline`。

在脚本同目录的 `submit-host.json` 中填写本机路径：

```json
{
  "model_root": "C:\\Users\\YourName\\description\\models\\myrobot",
  "profile": "kinematics",
  "message": "Update myrobot model"
}
```

脚本从同目录的 `worker-host.json` 读取 `install_root`，按 `current.json` 找到已安装运行环境；也可显式指定 `python`。
模型的 `source.worker_url` 指向本机 worker（默认 `http://127.0.0.1:8765`）。来源路径、装配配置和端口由模型定义控制，提交脚本不改写它们。
Git、Git LFS、已登录的 `gh` 须可用，远端须已有 `feature/<hardware>`。保存 CAD 后运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\submit.ps1
```

`-Message`、`-ModelRoot`、`-Profile` 可覆盖本次参数，`-Config` 可选择其他配置。
`-DescribeOnly` 显示执行计划，不采集或推送；实际预检与验证由公共 `description model update` 完成。
本机已激活完整工具环境时，也可从模型目录直接运行该命令。

只改定义或证据且来源未变时，两端均可使用 `description model update --reuse-source`，无需连接 CAD。
复用前仍核对来源配置与快照完整性；CAD 或来源配置变化须重新采集。
工具或操作系统变化须显式更新工具锁并重建，验证在锁定的平台与环境中执行。

每个工作区全程加锁。同一审查分支复跑更新同一 PR；构建失败保留诊断，PR 创建失败保留候选分支并给出重试命令。
默认不调用 GitHub CI。创建 PR 后仍须审阅；发布命令从远端重新取得精确候选并独立验收。

### 可选跨机提交

只有需要远程使用另一台电脑时才配置 SSH。

**Linux 发起，Windows 采集：** 配置 `windows-cad` SSH 别名并验证非交互登录，模型的 `source.worker_url` 指向本地转发端口，再运行：

```sh
description model update --worker-host windows-cad
```

命令建立并独占本次隧道，采集完成或失败后关闭连接；已有端口占用时拒绝接管。
`--worker-port` 指定 Windows worker 端口，默认 8765。Linux 端口来自 `source.worker_url`。

**Windows 发起，Linux 构建：** 使用远程配置，保留原有部署的兼容入口：

```json
{
  "build_host": "description-build",
  "remote_python": "/opt/description/venv/bin/python",
  "model_root": "/srv/description/models/myrobot",
  "profile": "kinematics",
  "message": "Update myrobot model",
  "worker_port": 8765,
  "remote_port": 8765
}
```

`build_host` 是 SSH 别名，用户与密钥写入 SSH 配置；`identity_file` 可单独指定私钥路径。
旧配置含 `build_host` 时仍按远程模式运行。模型的 `source.worker_url` 须与 `http://127.0.0.1:<remote_port>` 一致。
一次 SSH 会话同时转发 worker 并执行远程更新，结束时关闭隧道。提交信息经标准输入传递。

## 作业、取消与恢复

每次 `source freeze` 创建新的采集请求身份；传输重试复用同一请求，显式 `job_id` 只恢复原作业。
作业目录保存 `job.json`、`events.log` 与心跳；远端取回时校验文件清单、装配路径、配置与证据类别，
失败包及安全解出的文件留在诊断目录。Linux 侧取回：

```bash
curl -s http://127.0.0.1:8765/jobs/<id>/manifest     # 清单
curl -s http://127.0.0.1:8765/jobs/<id>/files        # 逐文件 sha256
curl -s http://127.0.0.1:8765/jobs/<id>/package -o snapshot.tar
```

* **重启恢复**：尚未开始的队列作业可以恢复；已开始的冻结失去原始现场，记为失败并保留诊断，
  要求重新冻结。请求摘要或采集工具版本不符时拒绝恢复。
* **取消**：只作用于指定作业。尚未开始的作业直接转为终态；已进入 CAD 执行的作业通过回收本作业
  自有进程使其退出，不触碰操作者的 SolidWorks。
* **维护与版本切换**：没有排队或运行中的作业，且 CAD 操作已经退出时，允许
  `/maintenance` 与 `Update`/`Rollback`。未退出的超时操作按下述恢复条件处理。
* **超时/未退出**：CAD 操作超时后，worker 回收本作业自有进程；在该操作真正退出之前，新的 CAD
  工作一律以 `cad_recovery_required` 拒绝（503）。只有所有作业处于终态、队列为空，并且 worker
  明确报告该状态时，才允许以维护重启（含 `Update`/`Rollback`）收尾；否则先等待操作退出。

## 测试与验收

```bash
# Linux 夹具：不接触 CAD，覆盖冻结/读取/篡改拒绝/作业/看门狗/API/doctor
python -m unittest discover -s tests/sources -t .
```

`tests/windows/test_deployment.ps1` 用替身验证状态转换、失败回滚与进程隔离，不操作真实 Windows
任务或 CAD。真实 Windows 验收必须分别记录：安装、连接（Doctor 三态）、原生采集（真实装配的快照
与逐文件摘要）、机器人级验收（消费该快照的下游构建与检查）；夹具通过不等于原生装配或机器人验收。

独立 oracle 从 raw 组件集合、变换与完整张量重算，再与 q=0 运动链在装配世界坐标中对账；
缺少质量证据即阻断。
