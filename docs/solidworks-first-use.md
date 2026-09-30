# 在 SolidWorks 电脑上完成建模和提交

[English](solidworks-first-use.en.md) · 中文

**一台 Windows 电脑即可完成：采集 CAD → 生成 URDF/MJCF → 本地验证 → 提交 PR。**
首次安装和机器人定义完成后，日常只运行 `submit.ps1`。默认流程在本机完成，不调用 GitHub CI。

当前原生受测平台为 Windows 11 Home China 25H2（build 26200）x64、SolidWorks 2026 SP3.2
（revision 34.3.2）及 Python 3.12.10。其他版本须先完成 Doctor、原生采集与模型检查后再使用，暂不声明兼容。

本文以 `myrobot` 为硬件标识，装配为 `D:\robots\myrobot\robot.SLDASM`，配置为 `Default`；请替换为实际值。
已有该硬件模型时复用其定义和分支，不重复初始化。首次完成运动学模型后，再补充仿真、训练或实机所需的参数与证据。

## 首跑清单

首次接入按以下五步完成。装配和机器人机械定义必须由设计者核对，所需时间取决于实际硬件。
每步的完整命令与失败处理在对应小节里。

1. **下载安装包**（[第 1 节](#1-保存装配准备工具)）：把分发页的 `description-worker-<版本>-windows-x86_64.zip` 与 `SHA256SUMS` 放进同一目录，例如"下载"。
2. **安装并自检**（[第 2 节](#2-安装并检查本机环境)）：`powershell -ExecutionPolicy Bypass -File "$Deploy\worker.ps1" -Action Setup -Bundle $Bundle -Assembly '<装配路径>' -AssemblyConfiguration Default`
   成功标志：先打印 `install complete`，随后 Doctor 退出 0。
3. **建立模型工作区**（[第 3 节](#3-在本机建立模型工作区)）：`description model init --repository <工具仓库> --root <模型目录> --hardware <机器人名> --provider solidworks --assembly '<装配路径>' --configuration Default`
   成功标志：写出 `config/robot.yaml`，stderr 打印 `next:`。
4. **冻结、补齐定义、构建并检查**（[第 4 节](#4-首次采集补齐机器人定义)）：先采集并核对装配实例，填写刚体、关节和限位；再运行 `description build --root <模型目录> --profile kinematics` 与 `description check --root <模型目录> --profile kinematics`。
   成功标志：检查输出 `qualified_for: ["kinematics"]`。
5. **一键提交**（[第 5 节](#5-配置提交入口日常一键运行)）：`powershell -ExecutionPolicy Bypass -File .\submit.ps1`
   成功标志：候选已推送，PR 创建或更新。

任何一步不确定就先跑 `description doctor --root <模型目录>`：它逐项报告环境与工作区状态，并给出每条失败的修复命令。
命令打印的 `next:` 就是下一步该做什么；其余问题见[故障定位](#故障定位)。

## 1. 保存装配，准备工具

在 SolidWorks 中选定配置，保存顶层装配及全部引用文件。确认引用完整，每个纳入的实体有明确的物理材料；外观颜色不算材料。
worker 会在独立的 SolidWorks 进程中只读打开已保存的装配，不读取桌面会话中的未保存修改，
也不保存原件。采集期间保持 Windows 桌面登录；失败诊断保存在 `build/failed-source/`。
SolidWorks 可能将 worker 只读打开的文档标记为“需要保存”。Doctor 会提示这个标志，采集也会
记录为证据；快照仍以磁盘上已保存的文件为准。若要包含桌面会话中的修改，须先保存再采集。
本文使用 CAD 材料计算质量属性。使用规格或实测质量时，按[质量证据合同](sources/solidworks.md#质量与惯量合同)准备完整声明表与证据。

Windows 需要有效的 SolidWorks 许可、x64 CPython 3.12.10、Git（含 Git LFS）和 GitHub CLI。
本机没有 3.12 时先装一个（3.12.x 均可；3.13/3.14 不受支持）：

```powershell
winget install Python.Python.3.12
py -3.12 --version        # 显示 3.12.x 即可；若 `python --version` 打开 Microsoft Store，请改用 `py` 启动器
```

Windows 11 自带 `python.exe`/`python3.exe` 的"应用执行别名"，未安装解释器时会直接打开 Microsoft Store。
装好后打开**新的** PowerShell，确认 `python --version`、`git --version`、`git lfs version`、`gh --version` 均可运行。
GitHub 账户须有目标仓库写入权限。完成一次登录：

```powershell
gh auth login
gh auth setup-git
gh auth status
```

如果当前网络让 GitHub HTTPS 的协议协商停滞，固定 Git 使用协议 v0 后再继续：

```powershell
git config --global protocol.version 0
```

从 [0.3.24 分发页](https://github.com/mimicverse/description-pipeline/releases/tag/v0.3.24) 下载
`description-worker-0.3.24-windows-x86_64.zip` 和 `SHA256SUMS`，并先查看页面上的验收状态。
此包包含采集、构建和 MuJoCo 验证所需的完整运行环境；离线安装不下载 Python 依赖。

## 2. 安装并检查本机环境

使用已登录用户的普通 PowerShell 完成安装和后续操作，无需管理员权限。将 ZIP 与 `SHA256SUMS` 一起放入“下载”目录，
然后让 `Setup` 一次完成配置、安装与 Doctor（它会用旁边的 `SHA256SUMS` 校验安装包摘要）：

```powershell
$Bundle = "$env:USERPROFILE\Downloads\description-worker-0.3.24-windows-x86_64.zip"
$Deploy = "$env:USERPROFILE\description-setup\0.3.24"
Expand-Archive -LiteralPath $Bundle -DestinationPath $Deploy -Force
powershell -ExecutionPolicy Bypass -File "$Deploy\worker.ps1" -Action Setup -Bundle $Bundle `
    -InstallRoot "$env:USERPROFILE\dw" -Assembly 'D:\robots\myrobot\robot.SLDASM' `
    -AssemblyConfiguration Default
if ($LASTEXITCODE -ne 0) { throw 'Doctor failed' }
$Python = "$env:USERPROFILE\dw\versions\0.3.24\venv\Scripts\python.exe"
& $Python -m description_pipeline --version
```

`Setup` 写出 `worker-host.json`（上述命令安装到 `%USERPROFILE%\dw`，端口 8765，用当前用户与 PATH 上的
CPython 3.12），接着执行 `Install` 并运行 `Doctor`；摘要从旁边的 `SHA256SUMS` 核对，找不到时会把当前文件摘要固定下来
并提示。想自己检查每一项时，可加 `-NoInstall` 只写配置，或改完 `worker-host.json` 后照旧用 `-Action Install` / `-Action Doctor`。
`-InstallRoot` 应保持较短：Windows 发布验收中，自定义的较长路径曾使 PowerShell 解包超过旧版路径长度限制。

`Setup` 可以安全重复：命令行与已装好的一致时会直接跳过安装，只重新检查；只想改设置就加 `-Force`，
再用 `-Action Install` 让新设置生效——它不会重装文件，只会重建启动命令并重启同一版本。
端口或监听地址会改变脚本找到正在运行的 worker 的方式，所以先按安装时的配置停掉它：

```powershell
powershell -ExecutionPolicy Bypass -File "$Deploy\worker.ps1" -Action Stop `
    -Config "$env:LOCALAPPDATA\DescriptionWorker\worker-host.json"
```

Install 会安装完整运行环境并启动本机 worker，成功时输出 `install complete`。
若出现 `using the Startup folder` 后安装成功，表示使用当前用户的登录启动项。
Doctor 应退出 0，采集检查应为：

```text
collection: install=True worker=True solidworks=True collectable=True
```

保留解压目录中的脚本和本机配置。运行环境、作业与日志保存在 `install_root` 中。
首次安装成功后无需重复 Install；以后 worker 停止时执行 `-Action Start`，升级使用 `-Action Update`。
升级要**把新包与它那一版的 `SHA256SUMS` 放在一起**（或显式传 `-BundleSha256 <64 位十六进制>`）：
`worker-host.json` 里的摘要属于**已安装**的那一版，不适用于新包；两者都没有时升级会拒绝，而不是凭文件本身下结论。
重复 Install 本身是安全的：它只用当前配置重启已安装的版本，不会重装文件。
`worker-host.json` 的装配路径供 Doctor 检查；实际采集对象由模型的 `config/robot.yaml` 决定。

## 3. 在本机建立模型工作区

继续使用同一 PowerShell。以下操作仅用于新硬件：

```powershell
$Tools = "$env:USERPROFILE\description\tools"
$Model = "$env:USERPROFILE\description\models\myrobot"
$ModelRepo = '<你的账号>/myrobot-description'  # 换成你有权限的账号或组织
New-Item -ItemType Directory -Force "$env:USERPROFILE\description\models" | Out-Null
git clone -c core.longpaths=true --branch main https://github.com/mimicverse/description-pipeline.git $Tools
gh repo create $ModelRepo --private
git -C $Tools remote rename origin upstream
git -C $Tools remote add origin "https://github.com/$ModelRepo.git"
git -C $Tools push -u origin main
git -C $Tools lfs install --local
git -C $Tools config user.name 'Your Name'
git -C $Tools config user.email 'your-git-email@example.com'
& $Python -m description_pipeline model init --repository $Tools --root $Model --hardware myrobot `
    --provider solidworks --assembly 'D:\robots\myrobot\robot.SLDASM' --configuration Default
if ($LASTEXITCODE -ne 0) { throw 'Model initialization failed' }
git -C $Model add -A
git -C $Model commit -m 'Initialize myrobot model workspace'
git -C $Model push -u origin feature/myrobot
```

`--provider` 形式写出与文档所列键完全一致的 `source` 映射（依赖根目录默认取装配所在目录，worker 默认
`http://127.0.0.1:8765`）；需要额外键（`documented_masses`、`coordinate_systems`、`elements` 等）时，
改用 `--source-config source.yaml` 传入完整文件。初始化后先运行 `& $Python -m description_pipeline doctor --root $Model`
确认工作区状态，再继续下一步。
替换仓库名称和 Git 身份；每条命令成功后再继续。私有仓库的 `main` 保存工具，`feature/myrobot` 与
`release/myrobot` 保存模型；公共仓库保留为 `upstream`，用于取得工具更新。初次推送建立该硬件的开发分支，后续提交通过 PR 更新它。
初始化后只维护 `$Model\config\robot.yaml`（装配路径、配置与来源键都在其中）。

已有模型时，将该硬件分支检出到本机的专用工作区，运行 `git lfs pull` 取得完整资产。必要时设置该工作区的提交身份：

```powershell
git -C $Model config user.name 'Your Name'
git -C $Model config user.email 'your-git-email@example.com'
```

再核对装配路径与配置。
若模型原先锁定 Linux 环境或旧工具，先运行 `& $Python -m description_pipeline tool lock --root $Model`，显式更新工具锁并重建；更改来源配置后还须重新采集。

## 4. 首次采集，补齐机器人定义

worker 在本机回环地址运行，无需 SSH 或端口转发：

```powershell
& $Python -m description_pipeline source freeze --root $Model
if ($LASTEXITCODE -ne 0) { throw 'Source capture failed' }
$Lock = Get-Content "$Model\sources\source.lock.json" -Raw | ConvertFrom-Json
$Scene = Get-Content (Join-Path $Model "$($Lock.snapshot)/raw/scene_raw.json") -Raw | ConvertFrom-Json
$Scene.components | Select-Object -ExpandProperty name
notepad "$Model\config\robot.yaml"
```

此时还没有可用的机器人运动链。由机械设计者编辑 **`config/robot.yaml`**，在已有的 `source` 下补充：

| 字段 | 从结构设计中明确什么 |
|---|---|
| `bodies` | 哪些组件实例刚性连接、归属同一个 link；使用采集得到的实例名，不能只写零件文件名。 |
| `bodies[].frame` | 参考姿态下，该 link 在装配坐标系中的位姿。 |
| `joints` | parent/child link、关节类型、父 link 系中的关节位姿、关节系中的轴向。 |
| `joints[].limits` | 有依据的位置、速度和力矩/力限值。长度用 m、角度用 rad、质量用 kg。 |
| `frames` | IMU、工具等需要交付的参考坐标系。 |

初始化生成的 `.yaml` 文件采用 JSON 写法，两种格式均支持。可继续按 JSON 编辑，也可将整个文件改为 YAML；不要在 JSON 后追加 YAML 片段。
以下是新模型 `config/robot.yaml` 的**完整 YAML 格式示例**。保留自己的硬件标识与来源配置，将实例名、分组、坐标和限值换成实际设计：

```yaml
schema_version: description.definition/v1
hardware_id: myrobot
source:
  provider: solidworks
  worker_url: http://127.0.0.1:8765
  assembly: D:/robots/myrobot/robot.SLDASM
  configuration: Default
  allowed_roots: [D:/robots/myrobot]
  require_saved: true
  geometry: {enabled: true, format: stl_binary}
  material_source: cad
  bodies:
    - id: base
      name: base_link
      components: [base_instance]
      frame: {xyz: [0, 0, 0], rpy: [0, 0, 0]}
    - id: arm
      name: arm_link
      components: [arm_instance]
      frame: {xyz: [0, 0, 0.1], rpy: [0, 0, 0]}
  joints:
    - id: shoulder
      name: shoulder_joint
      type: revolute
      parent: base_link
      child: arm_link
      xyz: [0, 0, 0.1]
      rpy: [0, 0, 0]
      axis: [0, 1, 0]
      limits: {lower: -1.0, upper: 1.0, effort: 1.0, velocity: 1.0}
overrides: []
```

机身、关节零位和参考姿态必须一致，每个纳入的物理实例只能归属一个刚体。
缺失的限值、控制映射或实测数据由作者补齐证据；采集程序不从 SolidWorks 配合关系猜测这些参数。
完整格式见 [SolidWorks 来源合同](sources/solidworks.md#配置合同configrobotyaml-的-source-映射)。

重新采集最终定义并构建：

```powershell
& $Python -m description_pipeline source freeze --root $Model
if ($LASTEXITCODE -ne 0) { throw 'Source capture failed' }
& $Python -m description_pipeline build --root $Model --profile kinematics
if ($LASTEXITCODE -ne 0) { throw 'Model build failed' }
& $Python -m description_pipeline check --root $Model --profile kinematics
if ($LASTEXITCODE -ne 0) { throw 'Model check failed' }
```

检查 `$Model\docs\quality.md`，审阅零位、关节方向、几何放置与质量属性。
产物入口为 `urdf/robot.urdf`、`mjcf/robot.xml`、`mjcf/scene.xml`，网格在 `meshes/`。
失败时修正 CAD 或定义后重建，不直接修改生成的 XML。

## 5. 配置提交入口，日常一键运行

在解压目录创建 `submit-host.json`。只需提供本机模型路径、用途和提交信息：

```powershell
@{
    model_root = $Model
    profile = 'kinematics'
    message = 'Update myrobot model'
} | ConvertTo-Json | Set-Content "$Deploy\submit-host.json" -Encoding UTF8
powershell -ExecutionPolicy Bypass -File "$Deploy\submit.ps1" -DescribeOnly
```

脚本默认从同目录的 `worker-host.json` 找到已安装的运行环境。
`-DescribeOnly` 检查配置并显示执行计划；它不会采集、推送或证明 GitHub 登录有效。

以后保存 CAD，保持桌面登录、电脑唤醒，在任意目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\description-setup\0.3.24\submit.ps1"
```

该命令在本机完成预检、采集、构建、验证、Git 推送和 PR 创建或更新。
成功结果包含 `model_sha`、`pull_request` 和本地验证结果；同一审查分支复跑更新同一 PR。
提交成功不等于已发布；合入对应 feature 后，按[发布流程](pipeline.md#提交验收与发布)重新取回并独立验收候选，再推进 release。

只修改定义或证据、且 `source` 未变时，可离线复用快照：

```powershell
& $Python -m description_pipeline model update --root $Model --reuse-source
```

修改 CAD 或 `source` 中的刚体、关节、路径、配置后必须重新采集。
跨机采集属于可选部署，配置见 [SolidWorks 手册](sources/solidworks.md#可选跨机提交)。

## 故障定位

| 现象 | 检查 |
|---|---|
| Doctor 的 `collectable=False` | 装配路径、已保存配置、缺失引用、许可和桌面会话。 |
| 找不到 Python、Git 或 gh | 安装对应依赖（Python 3.12：`winget install Python.Python.3.12`），重新打开 PowerShell；`git lfs version` 应可运行。 |
| 工具锁不匹配 | 确认已选定工具与平台，再显式更新锁并重建；不手改锁内摘要。 |
| 环境或工作区状态不明 | 运行 `description doctor --root $Model`：逐项列出 Python、依赖、MuJoCo、git/gh 与工具锁、来源快照、用途配置的状态，并给出修复命令。 |
| 来源采集失败 | CAD 保存状态、依赖目录、材料及声明质量证据。 |
| 采集报 `document_not_open` | 核对错误中的 `path` 与已保存的装配及引用文件；worker 会在独立进程中打开装配，可在 `build/failed-source/` 查看失败步骤。 |
| 构建失败 | 查看 `build/` 中诊断，修正实例归属、坐标、关节或参数证据。 |
| 推送成功，PR 创建失败 | 候选分支仍保留；按输出的重试命令创建或更新 PR。 |
| 发布验证失败 | 查看报告中的缺失参数、资产或用途证据，补齐后重新构建。 |
