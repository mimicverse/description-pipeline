# 开发约定

[English](CONTRIBUTING.en.md) · 中文

公共仓库的 main 接收工具变更；硬件 feature/release 分支放在可写的模型仓库中，模型仓库可以保持私有。分支与发布合同见 [README](README.md) 和
[工程契约](docs/pipeline.md)。

## 工具改动

从 main 创建 `work/tooling/<change>`，安装 `requirements/linux-py312.lock` 和可编辑包。
统一入口是 `python tools/quality.py all`；它执行 ruff、格式、类型（Linux 与 Windows 两套类型桩）、回归和工具目录合同。
只改文档时可用 `--fast`，正式交付仍需完整门禁。默认在本机检查 PR 候选工具代码，GitHub CI 可选。
回归里还包含一组文档与契约护栏：本地链接与锚点、中英两份的命令与章节结构对等、Markdown 表格
列数与代码块闭合、Mermaid 图可渲染、来源与工具目录合同，`submit.ps1`/`worker.ps1` 读取的每个
JSON 字段仍由工具产出，`.github/workflows` 的静态引用在 Actions 停用时也能被发现，以及发布契约：
验收工具与依赖审计要检查什么、发布页与支持版本只能指向已发布的版本、产物自带的身份必须等于 tag 指向的提交、
密钥扫描的白名单不得扩大、原生彩排的字段与护栏不得消失。Windows 上另有两套 PowerShell 替身套件（见下文）。
覆盖率不在门禁里，但随时可量（`coverage` 不在锁里，先 `python -m pip install coverage`）：
`python -m coverage run --source=src/description_pipeline -m unittest discover -s tests`
之后接 `python -m coverage report`；基线（2026-09-28 在 main `77dc3f5` 的全新 clone 上实测）是 **88%**（11,136 语句，缺 1,389 行）。新增代码不应降低它，**拒绝分支
（fail-closed 校验）必须被真正执行过**——没被跑过的校验等于没有校验。
完整门禁在普通笔记本上约 **4 分钟**（2026-09-28 在 main `77dc3f5` 的全新 clone 上实测：1,200 项测试，测试步骤 207 s、整场 223 s；机器繁忙时更慢），需要 **≥2 GB** 临时空间
（`TMPDIR`，必须是**已存在的绝对目录**；门禁在跑测试前先检查它并说明怎么修）。临时空间不足会以
`Disk quota exceeded` 的形式出现在某个测试里，而不是报"空间不够"。

Windows 上按同一份门禁开发（已在 Windows 11 + CPython 3.12.10 原生实测）：同一提交、同一六步，
加两套 PowerShell 套件，2026-09-28 在 `77dc3f5` 的干净检出上实测 1,200 项测试、`OK (skipped=17)`、
测试步骤 428 s、整场 434 s、8/8 步（Linux 上同一套件跳过 3 项，Windows 多出的跳过是
仅 POSIX 的用例），离线环境来自 `requirements/win-py312-dev.lock`。Windows 上额外多两步：
`powershell-deployment` 与 `powershell-submit` 跑 `tests/windows/*.ps1` 的替身套件——
它们用 `C:\...` 夹具路径，只能在 Windows 上跑；GitHub Actions 停摆期间没有任何其他地方执行它们。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements\win-py312-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe tools\quality.py all
```

`requirements/win-py312-dev.lock` 是原生 worker 的运行时集合（含 MuJoCo）加门禁所需工具，一次装齐；
`tools/quality.py` 让每一步都在 UTF-8 模式下运行，机器代码页不再影响结果。机器上装了多个版本时，
把第一行换成 `py -3.12 -m venv .venv`。

修改来源适配只接触取数、身份、原始读数与来源语义；不得在来源里另造 URDF/MJCF 生成器、
通用惯量判定器或发布门禁。通用代码位于 `src/description_pipeline`，兼容入口只做转发。
新消费者语义必须同时具备规范字段、后端投影、独立消费者检查和错误注入回归。
无法投影的约束应明确失败。

## 模型改动

已有硬件从 `feature/<hardware>` 建立 `work/model/<hardware>/<change>`。
输入是作者定义、带证据的覆盖和来源快照。不要手工改生成 XML；修正输入后重建。
工具升级需要显式更新工具锁并重建。

从模型根目录运行 `description model update`，一条命令完成 preflight → freeze → build → submit；
SolidWorks 跨机采集加 `--worker-host SSH_ALIAS`，仅改定义或证据加 `--reuse-source`。
`--root`、`--profile`、`--message-file` 可覆盖默认值。更新全程持有工作区锁；`description model submit` 是同一套
提交语义的公开入口（同样取锁，可与 update 并发互斥）。

提交只发生在审查分支上：在 `feature/<hardware>` 上运行时先创建
`work/model/<hardware>/<时间戳>-<随机>` 再提交，**不会推进 feature**；在该审查分支上再次运行时
提交落在同一分支并 PATCH 已有的 PR（同一审查分支复跑更新同一 PR），不会另开一条线程。
提交端必须已安装并登录 `gh`，preflight 在采集之前就会拒绝缺失或未认证的 CLI。
simulation 用途已声明完整实验时，update 自动执行实验、更新证据并重验；实验失败阻断提交。
本地验证通过后创建或更新 PR，默认不调用 Actions。建 PR 失败时，候选仍留在已推送分支上并给出复跑命令。
显式加 `--ci` 才调度托管校验；调度失败保留 PR，并给出 `description model dispatch` 重试命令。
未通过验证的设计可附诊断人工提交 draft PR；一键入口不会提交，发布也保持阻断。
首次建立 feature 使用 `description model init`；已有 feature 的初始化不可覆盖原工作树。
原生 CAD 文件使用 Git LFS，消费者与发布验证必须检出 LFS 实际字节。
模型模板关闭 Git 自动换行转换，保证输入与产物的字节摘要跨机一致；归档证据保持原样。

## 工具与模型发布

工具版本只写在包 `__version__`，分发元数据从中读取；模型锁定工具提交、包摘要与运行环境。
发布脚本拒绝脏源码，并在 wheel/sdist 中写入包摘要和源提交。
`tool-release.yml` 接收 main 历史中的精确提交，重新验证后生成带校验和的制品；指定可选标签时发布 GitHub Release。
工具发版的逐步清单（可复现性证明、两类分发包校验、Windows 原生演练、验收记录与发布后核对）见 [RELEASING.md](RELEASING.md)。

模型发布以 `description model promote` 的精确候选为准：同一硬件 feature 接受、
同一用途重新验算、固定工具、CAD 证据、远端完整交付和 release 快进。默认本机执行；`--ci` 额外要求该 SHA 的托管校验通过。
既有仓库/组织规则不得关闭。历史 release 有额外 PR 限制时会明确失败，不能强制绕过。

Windows worker 改动还需离线回归与 Windows 实机验收。测试夹具、已安装、进程存活、
真实 CAD 可采集和整机可仿真是不同结论，分别记录证据。
