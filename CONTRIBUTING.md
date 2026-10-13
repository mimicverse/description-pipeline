# 开发指南

开发以[架构设计](docs/design.md)、[系统契约](docs/contracts.md)、[机械规范](docs/mechanical-handoff-spec.md)和[质量规范](docs/quality.md)为共同依据。实现只提供一套完整工作流，领域逻辑、契约和展示不得重复定义同一规则。

## 环境

使用发行支持的 CPython 3.12 和仓库锁定依赖。Linux 开发环境包含生成、验证与构建依赖：

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements/linux-py312.lock -r requirements/build-py312.lock
python -m pip install --no-deps --no-build-isolation -e .
```

Windows 使用[工具构建流程](RELEASING.md#构建身份)在 Linux 产生且已核对源码身份的 wheel，只安装原生角色依赖并运行原生适配测试：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements/windows-py312.lock
.venv\Scripts\python.exe -m pip install --no-deps /path/to/mimicverse_description-<version>-py3-none-any.whl
```

锁文件记录完整角色依赖。开发工具链机器记录固定 CPython、Node.js 与构建器的精确版本；Linux 开发与 CI 显式安装并校验该 Node.js 以运行浏览器逻辑回归；缺少必需测试运行时须失败，不能以静默跳过减少覆盖。

## 代码边界

领域入口接受类型化不可变输入，返回结果或带稳定错误码、对象和证据引用的失败。编排负责副作用与收据，适配器负责协议，页面只提交命令和读取投影。禁止隐藏的模块全局运行状态、跨运行缓存和依赖调用顺序的机械结果。

原生 API 调用、会话、document selection 与句柄生命周期显式管理，所有退出路径核验并释放自有资源；未知身份拒绝操作。纯计算和机械判断放在 Linux。Windows 依赖与 import 边界在 CI 中静态检查。

验证器不导入推导、生成或编排领域，不共享机械期望值。通用数学、格式和契约原语保持纯粹；领域解释独立实现。发布统一调用完整验证，不创建第二套绿色判据。

schema、阶段、必需检查、适用条件、数值常量和文件清单各有一个机器定义属主。页面、收据与帮助从这些定义生成；机器定义、命令、资源限制和展示行为由“契约与文档”门测试；工程责任及原生行为由对应验收覆盖。重复定义和未使用的入口随变更移除。

<a id="verification"></a>
## 验证

在 Linux 开发环境执行：

```sh
python -B -m unittest discover -s tests -t .
python -m ruff check src tests tools deploy
```

PR 与 `main` 的 CI 还须执行以下门：

| 范围 | 必需证据 |
|---|---|
| 契约与文档 | schema 正反例、阶段/判据/资源限制与展示一致、资格身份、预览只读边界、链接有效、命令帮助与安装示例一致 |
| 机械与数值 | 解析解、非对角惯量、旋转/平行轴、重复实例/配置、树与轴线的独立反例 |
| 独立性 | 导入边界、生成语义错误及重新封存后仍被验证捕获的对抗测试 |
| 编排与存储 | CAS/事务、重复与冲突请求、断连、每个封存/登记边界崩溃、重启与复用 |
| CAD 所有权 | 未知/不可读会话、活属主、死亡属主残留、PID 重用、并发账本与所有释放入口 |
| 发布 | 远端基线变化、陌生分支、Git 过滤、部分推送/PR 响应及幂等对账 |
| 构建与角色 | 干净源码双构建字节一致、安装身份、完整角色锁及 Windows 最小依赖 |

只模拟输出形状不能证明原生行为；SolidWorks/COM 或支持矩阵变化需实际 Windows 验收。确定性门不得依赖本机恰好安装的其他 checkout 或 ambient 包。

测试按行为维护一套权威回归，不复制整套同义测试。失败必须定位原因；不稳定用例需修复或以明确隔离和责任处理，不能无声重跑直至通过。所需测试运行时与声明跳过范围由 CI 明确核验。

## 文档与评审

文档只描述目标行为，写清责任、来源、前置条件和结果。架构拥有职责，契约拥有协议，机械规范拥有工程要求，质量规范拥有判据；操作与部署引用它们。新增文件必须有独立读者和职责，不能复制已有规则。

行为变化先明确规范与可验收条件，再对齐代码、schema、帮助、示例与测试。合并要求规范和实现一致，工具发行还须完成[发布验收](RELEASING.md)。不能以文档声明代替实现证据。

公开仓库只保留工具、规范、合成中性夹具与测试；真实 CAD、硬件数据、凭据、工作站状态、诊断和构建输出留在相应私有存储。法律声明与第三方许可证按其要求随发行保留。
