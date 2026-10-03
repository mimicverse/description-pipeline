# description

CAD → 规范模型 → URDF / MJCF 的工程流水线：**单一真值、自动派生、验证后发布。**

[设计](docs/design.md) · [运行手册](docs/pipeline.md) · [验收状态](docs/validation.md) · [下载安装包](https://github.com/mimicverse/description-pipeline/releases/latest) · [Apache-2.0](LICENSE)

[English](README.en.md) · 中文

**首次从 SolidWorks 装配接入：** 按五步[首跑清单](docs/solidworks-first-use.md#首跑清单)
在同一台 Windows 电脑安装工具、补齐机器人定义并提交第一个 PR。连杆、关节和物理参数的定义时间取决于结构设计；
具体命令与失败处理见[首次接入指南](docs/solidworks-first-use.md)。

## 先跑一遍（无需 CAD）

一条命令写出离线示例工作区：用手写夹具来源跑通 冻结 → 构建 → 检查，不需要 CAD 账号、网络或 SolidWorks。

```sh
description quickstart --run
```

`--run` 会依次执行 `tool lock → source freeze → build → check` 并打印 `qualified_for: ["kinematics"]`；
不加 `--run` 时只创建工作区（默认 `./demo-arm`），并把接下来要跑的四条命令按顺序打印出来。
克隆仓库的话，同一份示例也在 [`examples/demo-arm`](examples/demo-arm/)。整条链路在普通笔记本上约 5 秒
（含 MuJoCo 编译验收），不需要 CAD、账号或网络。

装好后先自检：`description doctor` 检查本机环境（Python、依赖、MuJoCo、git/git-lfs，`--github` 还会检查 gh），
在模型工作区里加 `--root` 则连同工具锁、来源快照与用途配置一起检查；每条失败都给出修复命令。

```sh
cd demo-arm
description doctor --root .
description tool lock --root .        # 把工具锁重新指向你安装的版本（仅示例需要）
description source freeze --root .
description build --root . --profile kinematics
description check --root . --profile kinematics
```

产物是 `urdf/robot.urdf`、`mjcf/robot.xml`、`mjcf/scene.xml` 与 `docs/quality.*`；检查会从冻结来源重新推导、
用 MuJoCo 编译验收并给出 `qualified_for: ["kinematics"]`。示例是夹具证据（`evidence_class: fixture`），
不代表原生 CAD 采集；想验证流水线会拒绝篡改，按示例 README 改一个关节限位再跑一次检查。
[`examples/mesh-arm`](examples/mesh-arm/) 是第二个离线示例：几何改用二进制 STL 网格，并由独立几何 oracle 复核均匀密度惯量。

## 一键提交

完成首次建模和主机配置后，Windows 与 Linux 均可用一条命令完成：

```text
预检 → 采集并冻结 CAD → 生成 URDF / MJCF / 网格 / 配置
     → 本地验证 → 推送候选、创建或更新 PR
```

**Windows：** 保存 SolidWorks 装配及其依赖，保持电脑唤醒、桌面登录和 worker 可用，在部署目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\submit.ps1
```

脚本从同目录的 `submit-host.json` 读取本机模型工作区和用途，使用已安装的完整运行环境。
SolidWorks 采集、构建、MuJoCo 验证及 Git 提交均在本机完成。
用途设为 `simulation` 且实验定义完整时，入口自动运行实验、更新验收证据并重验，通过后提交。

**Linux：** Linux 分发包包含 `install.sh` 和 `submit.sh`。解压后先运行一次 `bash install.sh`（共享环境可设置
`DESCRIPTION_VENV`），之后从任意目录用一条命令提交：

```sh
bash /path/to/bundle/submit.sh --root /path/to/model --profile kinematics
```

它与 Windows 使用同一个 `description model update` 流程；也可以激活模型锁定的环境后直接运行该命令。
按修改内容选择模式：

| 使用情况 | 命令 |
|---|---|
| Onshape：通过 API 重新采集 | `description model update` |
| 来源未变，只改定义、用途或证据 | `description model update --reuse-source` |
| 可选跨机采集 SolidWorks | `description model update --worker-host windows-cad` |

`windows-cad` 是 SSH 主机别名，连接由命令自动建立和关闭；Windows 同样须保持可采集状态。
`--reuse-source` 校验并复用已有快照，无需连接 CAD；CAD 或来源配置改变时须重新采集。
Linux 默认使用当前目录、`kinematics` 用途和英文提交信息，`--root`、`--profile`、`--message` 可覆盖。

两端复用同一流水线，各自锁定本机模型工作区；在同一审查分支复跑会更新已有 PR。失败时保留诊断和已完成的提交状态；PR 创建失败时给出重试命令。默认流程不调用 GitHub CI。
**一键操作提交的是候选。** 合入模型开发分支后，发布命令从远端重新取得完整候选并独立验收，通过后才能发布；运动学、仿真、训练和实机控制分别验收。

工具环境和提交配置只需设置一次。Linux 可独立处理 Onshape 和已有快照；原生 SolidWorks 采集需要 Windows。新机器按[首次接入指南](docs/solidworks-first-use.md)操作；跨机模式见 [SolidWorks 手册](docs/sources/solidworks.md#可选跨机提交)。

## 从建模到交付

首次建模须确定机械定义、参数证据和用途要求：

CAD 与模型证据应放在你有写入权限的仓库。若模型需要保密，先用本公共工具仓库的 `main` 初始化私有仓库：

```sh
MODEL_REPO=your-account/myrobot-description
gh repo create "$MODEL_REPO" --private
git clone https://github.com/mimicverse/description-pipeline.git /path/to/description
git -C /path/to/description remote rename origin upstream
git -C /path/to/description remote add origin "https://github.com/$MODEL_REPO.git"
git -C /path/to/description push -u origin main
```

随后 `model init --repository /path/to/description` 在这个私有仓库中创建硬件分支；公共仓库只提供工具更新，
不接收 CAD。Windows 的 PowerShell 步骤见[首次接入指南](docs/solidworks-first-use.md)。

1. **确定目标并初始化。** 选择硬件、CAD 配置和用途，按离线包内的 README 安装选定工具。运行
   `description model init --repository /path/to/description --root /path/to/model --hardware myrobot --provider solidworks --assembly D:/robots/myrobot/robot.SLDASM --configuration Default`
   （Onshape 用 `--provider onshape --url <文档 URL>`；需要额外来源键时改用 `--source-config source.yaml`）。
   初始化后运行 `description doctor --root /path/to/model` 核对工作区。
2. **准备运行环境。** 在所选电脑上准备完整工具、专用模型工作区、Git/LFS 和已登录的 `gh`，并确保可写模型仓库可访问。Onshape 配置访问凭据；SolidWorks 在本机 Windows 安装 worker 并通过 Doctor。来源配置见 [Onshape](docs/sources/onshape.md) 和 [SolidWorks](docs/sources/solidworks.md)。
3. **冻结来源。** 运行 `description source freeze --root /path/to/model`，核对修订、配置、实例及排除项。
4. **补充定义。** 在 `config/robot.yaml` 中声明机械语义和有证据的参数，并把每个可动关节登记进 `config/joint_names.yaml`（`model init` 已生成模板；缺失或与 URDF 脱节会被 `URDF208` 拒绝）。在用途配置中确定容差、接触及应用验收要求。若修改了 `source` 中的刚体、关节等来源配置，重新执行 `source freeze`。
5. **构建并审阅。** 运行 `description build --root /path/to/model --profile kinematics`。后续用 `description diff OLD_SHA /path/to/model --repository /path/to/model` 审阅物理语义、输入和用途变化：stderr 先给一行结论（改了哪些区域、多少对象、交付摘要是否变化），完整 JSON 报告在 stdout（重定向到文件即可留档），`--json` 只输出报告。失败诊断保存在 `build/`，修正输入后重建。
6. **应用验收与提交。** 仿真按[仿真验收流程](docs/simulation.md)执行实验并重建。声明机械运动学套件时，按[机械参考流程](docs/mechanical-acceptance.md)取得独立参考，运行 `description model update --reuse-source --mechanical-reference /approved/mechanism.json`，自动验收、重建并提交；已构建候选用 `description model submit` 并传入同一参考选项。训练与实机仍需各自独立证据。本地重放无需 CI，消费端和发布端须重新选择已批准参考。
7. **验收并发布。** 候选进入对应 feature 后，使用模型锁定的工具环境运行下列命令查看计划；加 `--apply` 执行。发布会从远端重新取得完整候选，核验实际字节和用途证据，再快进 release。

   ```sh
   description model promote --root /path/to/description \
     --hardware myrobot --candidate FULL_MODEL_SHA --profile kinematics
   ```

8. **接入与迭代。** 使用端检出已发布 SHA，用 `description check --root /path/to/model --profile kinematics` 核对用途及环境，再加载模型。日常更新使用一键入口；回退时使用仍符合硬件与环境的旧 SHA。

入口固定为 `urdf/robot.urdf`、`mjcf/robot.xml` 和 `mjcf/scene.xml`，网格及配套配置随模型交付。
缺参数、来源不完整、检查未运行或证据不足时，相应用途保持阻断。

## 仓库组织

| 分支 | 职责 |
|---|---|
| `main` | 公共 Python 工具、规范与测试；发布不依赖 GitHub Actions |
| `feature/<hardware>` | 该硬件的定义、来源快照、模型资产和证据 |
| `release/<hardware>` | 最新已验收发布提交，只向前推进 |

模型用 `config/toolchain.lock.json` 固定工具提交、包摘要和运行环境；使用端固定模型提交 SHA。
Windows 采集与提交脚本位于 `src/description_pipeline/sources/solidworks/deploy/`，Linux 安装与提交入口位于
`src/description_pipeline/deploy/linux/`；模型模板与部署资源随工具包分发。

## 工具开发与打包

工具支持 CPython 3.12、MuJoCo 3.13.0；受测环境为 Linux x86_64 / Python 3.12.14，以及 Windows 11 Home China 25H2 x64 / Python 3.12.10 / SolidWorks 2026 SP3.2。其他 SolidWorks 版本尚未实测。
在 main 工具工作区中执行：

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements/linux-py312.lock
python -m pip install --no-deps --no-build-isolation -e .
python tools/quality.py all
```

`python3.12 -m venv` 在 Debian/Ubuntu 上需要先装 `python3.12-venv`；用 `uv` 安装的解释器自带 `venv` 会以
`ensurepip` 失败退出，改用 `uv venv --python 3.12 --seed .venv` 建环境（`--seed` 才有 `pip`）。
用户不需要这一步：发布包里的安装脚本自建环境并给出同样明确的报错。

`python tools/build_release.py --require-clean --offline --out dist/COMMIT` 生成 wheel、源码包及 Linux/Windows 完整离线包；每次使用新的输出目录。
同一提交配合同一份锁文件重复构建会产生逐字节相同的四个产物：构建脚本把构建时间、文件属主与权限统一归一化，
对比两次 `SHA256SUMS` 即可复核。发布记录固定的就是这些字节的 SHA-256，因此分发物可以被独立重建和核对。
`python tools/verify_distribution.py dist/COMMIT/*-linux-*.zip` 在新环境中验证安装、迁移重建及错误检出。
`python tools/verify_distribution.py dist/COMMIT/*-windows-*.zip --windows` 校验 Windows 包内容及离线依赖；原生 CAD 验收在 Windows 上另行执行。

## 工程资料

[工程标准](docs/engineering_standard.md) · [URDF 规则](docs/urdf_standard.md) · [开发与贡献](CONTRIBUTING.md) · [检查入口](tools/quality.py)

[机械运动学验收](docs/mechanical-acceptance.md) · [仿真应用验收](docs/simulation.md) · [模型模板](src/description_pipeline/templates/model/README.md)

[历史 Onshape 工具迁移](docs/onshape_export.md) · [历史 SolidWorks 工具迁移](docs/solidworks_export.md)
