# DeepSWE 工具开发经验总结

更新时间:2026-07-15。本文提炼自原 `CODEX-PIER-WINDOWS-NOTES.md`、`review.md`(2026-07-11 代码审查,15 项 CONFIRMED 缺陷)、`审查报告.md`(2026-07-12 试运行事故复盘)及两个 Python 自定义 Task 的端到端实测。所有缺陷均已修复,此处保留根因与法则。项目现状见 `PROJECT-PLAN.md`。

## 1. Windows 宿主机 × Linux 容器的三类必踩坑

### 1.1 CRLF —— 三次独立踩坑,同一根因

| 场景 | 症状 | 修复与防线 |
|---|---|---|
| Pier 生成 `start-squid.sh`(Windows `write_text()` 默认 CRLF) | egress proxy 容器退出,`RuntimeError: dependency failed to start`,agent 根本没启动 | 本机 pier 补丁:`write_text(..., newline="\n")` |
| `tasks/**/*.sh` 共 338 个被 `core.autocrlf=true` 在 checkout 时污染 | verifier 报 `bash: /tests/test.sh: cannot execute: required file not found`(shebang 变成 `#!/bin/bash\r`)→ 无 reward 文件 → 全军覆没 | 全部转 LF + `git add --renormalize`;`.gitattributes` 加 `*.sh text eol=lf` |
| —— 防线纵深 —— | | `.gitattributes` 只约束**未来的 checkout,不会重写工作区现有文件**;所以另有 preflight 在启动前扫描所选任务 `*.sh`,发现 CRLF 直接拒绝运行 |

核心教训:CRLF 造成的是"agent 成功、钱花了、verifier 必挂、分拿不到"的最坏失败模式,一次三 Agent 试运行因此白烧全部 agent 费用。**任何要进 Linux 容器执行的文本,生成与检出两条路径都必须强制 LF,并在花钱之前用零成本检查拦截。**

### 1.2 GBK —— Windows 默认编码读 UTF-8 文件

pier 用系统默认编码读 agent 的 JSONL 会话文件,在中文 Windows 上是 GBK,遇 UTF-8 多字节即 `UnicodeDecodeError`,导致 trajectory/token 统计解析失败(钱花了、数据拿不到)。两处本机 pier 补丁:`agents/installed/codex.py` 与 `agents/installed/claude_code.py` 均强制 `encoding="utf-8"`。教训:**Windows 上凡 `open()` 读跨系统产物,一律显式 `encoding="utf-8"`。**

### 1.3 网络 —— 容器视角与宿主机视角不同

- 容器内 `127.0.0.1` 指容器自己;访问 Windows 宿主机必须用 `host.docker.internal`(UI 已自动映射凭据 URL 的 host)
- Pier 的 Squid egress 代理默认只放行 80/443,本地模型端口需加入 `SSL_ports`/`Safe_ports`(本机 pier 补丁)
- Docker 网络会从 `192.168.*` fallback 地址池分配子网,与家用局域网冲突导致容器断网 —— 解法:经 sitecustomize 补丁把 trial 网络固定到 `10.240.0.0/12` 池按路径哈希分配(`pier_retry_patch/networking.py`)
- `urlparse().hostname` 会小写化,拿它对原始 URL 做区分大小写的 replace 会静默跳过(凭据写 `http://LOCALHOST:9887` 时映射失效)

## 2. Codex 接入四连败(排障链路范式)

依次遇到并解决,每步日志特征不同,可作对照表:

1. `RuntimeError + pier-egress-proxy exited` → CRLF(见 §1.1),agent 未启动
2. `ValueError: Model name is required` → `pier run --agent codex` 必须显式 `--model`
3. `401 Unauthorized + api.openai.com` → provider 配置未生效仍走官方端点;或传了 `OPENAI_API_KEY=local` 占位值被当真 bearer token 发出。真实 token 在 `~\.codex\auth.json` 时,用 `--agent-env CODEX_FORCE_AUTH_JSON=true` 上传,**不要传占位 API key**
4. `400 Bad Request + ws://...:9887/v1/responses` → URL 已对,但 Codex 默认走 Responses WebSocket 而本地网关不支持 → provider TOML 里 `supports_websockets = false`

手工运行(现由 UI 自动生成临时配置,`codex-local-provider.toml` 仅供手册命令):

```powershell
pier.exe run -p .\tasks\<task-id> --agent codex --model openai/gpt-5.5 `
  --agent-env CODEX_FORCE_AUTH_JSON=true `
  --agent-kwarg config_toml_file=.\codex-local-provider.toml -n 1
```

排障纪律:**只看最新 `jobs\<job>\` 目录**(`result.json`、`job.log`、`<trial>\agent\*.txt`),旧日志的 endpoint/错误可能早已不是现状。

## 3. pier 本机补丁清单(升级即丢,靠诊断护栏)

改动均在 `%APPDATA%\uv\tools\datacurve-pier\Lib\site-packages\pier\`,非仓库文件,升级/重装 pier 会被覆盖:

| 文件 | 改动 | 丢失后的症状 |
|---|---|---|
| `environments/agent_setup.py` | 生成的 Dockerfile/start-squid.sh 强制 LF;Squid 放行模型端口 | egress proxy 容器退出 / 请求被代理拦截 |
| `agents/installed/codex.py` | UTF-8 读会话 | GBK 解码错误,轨迹解析失败 |
| `agents/installed/claude_code.py` | UTF-8 读会话 | 同上,token 统计丢失 |
| secret-env 补丁 | Token 不进 docker 命令行 | 凭据泄漏到进程列表 |

Diagnostics 用**特征标识检测**(如 `process_env_overrides`)而非精确字符串匹配 —— pier 升级重排代码时精确匹配会永久误报。`pier_retry_patch/` 走的是另一条更稳的路:经 PYTHONPATH sitecustomize 在运行时 monkey-patch,不改安装文件,随仓库分发。

## 4. 测量工具的正确性法则(15 项审查缺陷的提炼)

对"检测模型降智"的测量工具,缺陷严重度排序与普通应用不同,三条生命线:

### 4.1 测量记录必须与实际执行一致

- **默认值巧合是最阴险的失真**:codex 分支从未透传 reasoning effort,而 pier 默认 `high` 恰与 UI 默认一致 → 冒烟永远发现不了,用户跑 low vs high 对比时两次实际都是 high。法则:**关键参数必须显式传递,不依赖任何一层的默认值**;修复后 effort 走双通道(TOML + `--agent-kwarg`)
- **"观测值"只能来自观测**:`reasoning_effort_effective` 曾在创建时复制用户所选值 —— 伪造观测比没有观测更糟。未观测就存 `null`
- **"未测量"与"全部失败"必须可区分**:禁用 verifier 的运行曾记 reward=0.0/passed=False,可被设为基线毒害比较;现存 `null`
- **退出码必须参与判定**:pier 非零退出曾仍按残缺 result.json 标 completed;更糟的是有测试传 `returncode=1` 断言 completed —— **测试断言过缺陷行为,等于把缺陷固化为规范**
- 时钟统一:job_name 用本地时间而 created_at 用 UTC,同一记录差 8 小时,人工对照极易误判

### 4.2 付费资源不能泄漏

- 取消后的 Docker 清理曾约 98% 失效:pier trial 目录名含 ShortUUID **大写**字母,而 compose project 名被 pier `lower()`,区分大小写的子串匹配几乎必不命中 → 取消后容器内 agent 继续烧钱。法则:**名称匹配必须复刻对方系统的规整规则(以源码为准),并在真实资源上验证命中率**
- 服务重启后 pier 进程树与容器成为孤儿继续调付费 API,且 UI 取消按钮已消失 → 启动时按 PID 收割(psutil 校验命令行仍指向 pier,防 PID 复用误杀)、退出时终止子进程
- 取消接口返回 `cancelled:false` 时前端曾照样显示"清理已触发" —— 状态提示必须以后端真实结果为准
- 取消与结果落库存在双向竞态(取消被误报失败 / completed 被改写 cancelled),需状态锁 + 条件更新,终态不可反向覆盖

### 4.3 对上游产物形态的假设必须以源码/实测为准

审查中最有效的方法是直接读本机安装的 pier 0.3.0 源码逐条核对,而不是按文档或直觉假设:

- `data.get("verifier_result", {})` 挡不住 `"verifier_result": null` —— dict.get 默认值只在键**缺失**时生效,pier 序列化不排除 None;统一 `(x or {})` 模式
- pier 把 trial 目录名截断到 32 字符(`task_name[:32].rstrip('_-')`),旧默认 7 题有 4 题超长 → 用全名对目录名做匹配产生幽灵 trial;匹配前同样归一
- pier 聚合器对单键 rewards 会把键改名为 `mean`(DeepSWE 多键 rewards 不受影响,属潜伏约定)

### 4.4 测量工具自身的开销也会污染测量

SSE 曾每秒全量重读全部 trial 的 result.json 与完整 patch(1 小时 ≈ 5.4 万次文件打开、2.5GB 读取),与被测 benchmark 抢磁盘/CPU,直接干扰"归一化耗时"这一回归判据。现用 mtime 指纹检测变化、推送剔除 Patch 正文、点击按需拉取。

### 4.5 输入验证(即使是本地单用户工具)

- restore 备份未验证时:`job_name: ".."` 经 `rmtree` 可删任意目录(pathlib join 绝对路径会整体替换 base,已沙盒复现);`status: "running"` 产生不可取消不可删除的僵尸行;settings 非 JSON 值使全站 500 且跨重启持续。现逐行白名单验证 + 删除路径 `resolve()` 逃逸检查
- `tasks: []` 曾被接受 → pier 失去 `-i` 过滤跑满全部 113 题(每题 2 CPU/8GB 的付费调用)

## 5. $235 试运行事故(2026-07-12)—— 用量失控的解剖

三 Agent × 2 任务 × xhigh 并发试跑,三个 Run 全 failed,**没有任何一个失败是模型做不出题**,三条独立故障叠加:

1. **CRLF verifier 必挂**(§1.1):codex 正常完成两题编码(输入 1442 万 token、$10.578),verifier 拿不到 reward,全部报废
2. **Claude Code subagent 风暴**(用量主体,≈ $225):`gpt-5.6-sol` 在 Claude Code harness 中写完代码后陷入"自我审查循环",反复以相同角度批量 spawn 审查 subagent 永不收敛 —— 43 分钟内 2687 个 subagent 会话、4058 次 API 请求、输入 66,630,573 token。**`--max-turns` 只限主对话轮数,对 subagent 数量与消耗完全无约束**
3. **503 连锁**:6 个 agent 容器并发打同一网关,过载 503 又被"Claude Code 内建 10 次重试 × pier 基础设施重试 1 次"放大

由此落地的防线(均已实现,见 PROJECT-PLAN §10):preflight CRLF 拦截;claude-code 禁 `Task`/`Agent` 工具;`run_budget_usd` 费用熔断 + 日志 30MB/60 会话失控硬线(按事故写入速率推算可在 1–5 分钟内拦停,损失 $2 以内);瞬时故障分类重试。**运维纪律:新配置先 low/medium 单题单 trial 冒烟,端到端确认 verifier 产出 reward 后再上 xhigh/全量 —— xhigh 是一切用量的全局放大器。**

## 6. 评测口径的坚持

- **容器无 git 身份是评测设计,不是缺陷,不要"修"**:任务统一要求 commit 工作,评分管道用 `git diff <起始commit> HEAD` 取**已提交**的工作(commit 失败 → 空 patch → reward 必为 0)。codex 撞上后自行 `git config` 越过(官方该两题通过率 91.46%/87.2%,证明官方同样在无身份环境校准);预置身份 = 变相放水,偏离官方口径。处理不完美环境本身是考察点
- **控制任务**(actionlint)与区分任务分开,控制任务坍塌是强降智信号(判定规则之一)
- **可比性分级**:官方严格可比只有 mini-swe-agent ↔ 官方 mini-swe-agent;Codex/Claude Code 对官方数据只是参考;严格纵向比较要求同 agent/版本/模型/配置 —— 这正是每次运行落库 pier 版本、agent 版本、Jobs 目录等可复现性字段的原因(区分"模型降智"与"harness 变化")

## 7. 危险操作的安全边界(可移植原则)

Docker 清理模块沉淀的通用原则:

- 删除候选**只从自有 artifacts 推导 + 白名单后缀匹配**,永不信任前端传入的资源名,永不用宽泛全局正则
- 无 `--force`、无 `docker system prune -a`、无无范围 `image prune -a`(容器为零时后者会把 ECR 基础镜像判为 unused,下轮重拉数 GB)
- 被引用的跳过并报告;不存在视为成功(幂等);单个失败不中断其余;结果写审计 JSON
- 破坏性批量操作:先预览(只用 Docker 返回的 reclaimable/独占空间,**不能把镜像列表 Size 相加** —— 共享层被复用,单条独占仅 KB 级)、二次确认、有活动 Run 时禁用
- 清理失败绝不改变已产生的评测结果状态

## 8. 流程经验

- **文档的"已完成"必须与代码核实**:第一轮进度文档声称"Docker 清理已完成",实际约 98% 失效;"记录 effort 三值",实际有效值是创建时复制的。审查(多角度并行扫描 + 对抗性验证 + 上游源码核对 + 实测复现)把完成度判定拉回真实
- backend 重启会孤儿化 running 的 run(UI 标 `interrupted`),重启前确认无活动运行;启动收割只是兜底
- 工程卫生的实际代价:前端依赖 `latest` = 不可复现构建(与工具自身主张矛盾);测试未隔离凭据路径 → pytest 读开发者真实密钥文件;CSV 无 BOM → 中文 Excel 乱码,未转义 `=` 前缀 → 公式注入

## 9. 自定义 Task 制作与实测

`test-python-slugify-workflow` 与 `test-python-summary-workflow` 证明了新增 Task 可以很小，但框架不能缩水。测试用途 Task 也必须完整经过 Agent、提交、Patch 提取、独立 verifier 和隐藏评分链路，否则只能验证局部脚本，不能验证真实流程。

### 9.1 完整结构与命名

最小可复用结构:

```text
tasks/<test-task-id>/
  task.toml
  instruction.md
  pre_artifacts.sh
  environment/{Dockerfile,repo/}
  solution/{solve.sh,solution.patch}
  tests/{Dockerfile,config.json,test.sh,test.patch,grader.py,base_repo/}
```

- 测试用途必须显式命名:目录和 `task_id` 以 `test-` 开头，`display_title` 以 `[TEST]` 开头，`category = "test_validation"`，`language` 填真实语言
- `task.toml` 采用 DeepSWE `schema_version = "1.1"`，声明 `/logs/artifacts/model.patch`、独立 verifier、资源与超时；测试 Task 留在 catalog 中按需选择，不混入正式回归 suite
- `grader.py` 使用 `tools/verifier/grader.py` 的统一版本，题目差异只放在 `tests/config.json` 和 task-local `test.sh`；无必要不要分叉评分器
- Agent 镜像负责提供可工作的起始仓库，verifier 镜像负责提供 pristine base、隐藏测试和评分依赖；两者能分别构建才算结构完整

### 9.2 Base commit 与 Patch 是同一条契约

- 为 fixture 建一个确定性的本地 Git base commit；`environment/repo` 与 `tests/base_repo` 必须来自同一 commit
- 同一 SHA 至少出现在 `task.toml` 的 `base_commit_hash`、`tests/config.json` 的 `base_commit` 和 `pre_artifacts.sh` 的 `git diff <base> HEAD` 中，修改任何一处都要同步其余位置
- 两个 Docker 镜像构建后都要在容器内执行 `git rev-parse HEAD`，不能只相信复制目录或配置文本
- `solution.patch`、隐藏 `test.patch` 与真实 `model.patch` 都必须能从同一 base 干净应用；先做 apply check，再进入付费运行
- Agent 必须 commit。评分链只读取 `git diff <base> HEAD`，工作区里“代码已经改好但未提交”仍会得到空 Patch 和 `Reward=0`

### 9.3 P2P/F2P 的设计原则

- P2P 是原有行为保护线，在 pristine base 上先通过，修复后仍通过；它不是为了提高测试数量
- F2P 是修复证据，每一项都应在 pristine base 上失败、在 oracle 上通过。优先提供“存在但实现不完整”的函数，让失败落在断言层；缺少 import、模块或函数造成的 collection error 只证明测试跑不起来，不是高质量 F2P 证据
- `nop` 的理想边界是 P2P 全过、F2P 全败；本次两个 Task 均为 P2P `3/3`、F2P `0/5`、`Reward=0`
- oracle 或等价生成 Patch 应为 P2P `3/3`、F2P `5/5`、`Reward=1`。如果 nop/oracle 不能形成清晰的 0/1 边界，先修题目，不要调用模型
- JUnit 白名单 node ID 必须逐字等于报告中的 `classname.name`。评分器把报告里缺失、跳过或拼错的 ID 都按失败处理，不能靠测试进程退出码推断通过

### 9.4 固定验证阶梯

以后新增 Task 按固定顺序执行:

1. 解析 `task.toml` 与 `tests/config.json`，检查字段、路径和 `.sh` 的 LF
2. 对比 Agent/verifier 两份 base repo，确认内容与 commit SHA 相同
3. 分别构建 Agent 和 verifier Docker 镜像，区分构建失败与题目失败
4. 在两个镜像内核对 `git rev-parse HEAD` 与配置 SHA
5. 检查 `solution.patch`、`test.patch`，并实际运行 `pre_artifacts.sh` 生成非空 `model.patch`
6. 跑 `nop`，预期 `Reward=0` 且 P2P 全过、F2P 全败
7. 跑 oracle/参考解，预期 `Reward=1` 且 P2P/F2P 全过
8. 最后跑一次真实模型 Trial，检查 trajectory、commit、Patch、verifier 与费用证据

`nop` 和 oracle 只验证评分上下界，不会调用模型。一个 Task 只有同时留下以下证据，才能写成“真实模型实测通过”:

- `agent_result` 非空，trajectory 中存在真实模型交互，steps、token 和费用均非零
- 模型完成 Git commit，而不是只修改工作区
- `pre_artifacts.sh` 生成了非空 `/logs/artifacts/model.patch`
- verifier 生成 `/logs/verifier/reward.json`；流程验证 Task 的预期结果为 `Reward=1`

`agent_result = null`、没有 trajectory/API 调用、只有环境构建日志，或者仅跑了 `nop` / oracle，都不能计为真实模型验证。

### 9.5 基础设施失败不能算模型失败

- Windows checkout 后首先检查 LF；Rich/Pier CLI 在 GBK 终端输出异常时用 `PYTHONUTF8=1` 或显式 UTF-8
- Docker Compose Bake 卡住时可用 `COMPOSE_BAKE=false` 排除 Bake 路径；Debian 包下载等瞬时网络错误应重试或换用已预装 `curl/git/gcc/make` 的 Agent 基础镜像
- GitHub release 下载返回 403 时，可为镜像构建配置代理/镜像地址；这属于构建环境修复，不应改变 verifier 的离线约束或把失败归因给模型
- PowerShell 调 Pier 时使用参数数组，确保 `extra_python_packages=["litellm[proxy]"]` 仍是列表；字符串被错误展开后会在 Dockerfile 中按字符拆包
- 新 Task 先用 low/medium、单 Task、单 Trial 冒烟，确认 `reward.json` 后再提高 effort 或并发；环境失败与模型解题失败必须分别统计

### 9.6 镜像重建、缓存与 Bake

看到 `preparing_environment` 时先区分“正在正常构建”和“构建已停滞”。该阶段不只创建容器，还包括基础镜像元数据解析/拉取、Task 环境、Agent 安装层、egress proxy 与健康检查；首次运行新 Task 时持续数分钟并不代表模型或题目卡死。模型 API 调用从 `agent_running` 才开始。Patch 生成后的阶段应依次理解为 `preparing_verifier → verifier → finalizing`，而不是把整个等待窗口都叫作 Patch 提取。

Pier 可能为每个 Trial 执行 `docker compose build` 并生成新的随机镜像标签，但 Dockerfile 和上下文没有变化时，BuildKit 会复用已有层。判断是否“真正重建”应看构建日志中步骤是 `CACHED` 还是重新执行，而不是只看是否出现 build 命令。

| 变化 | 是否需要重建 | 原因 |
|---|---|---|
| 首次运行、镜像/BuildKit 缓存被清理、Docker Desktop 重置或切换 daemon/context/平台 | 是 | 本机没有可复用镜像层 |
| `environment/Dockerfile`、基础镜像 digest、系统/Python 依赖 | 是 | Agent 环境层输入变化 |
| `environment/repo` 中被 `COPY` 的代码或 base fixture | 是 | `COPY` 层及其后续层失效 |
| `tests/Dockerfile`、`grader.py`、`test.sh`、`test.patch`、`config.json`、`base_repo` | 是，至少 verifier | verifier 构建上下文或评分内容变化 |
| Agent 类型/版本/Pier 安装指纹、`extra_python_packages` | 是，至少 Agent 安装层 | 容器内 Agent 工具链变化 |
| 模型、effort、凭据、模型 URL、并发、重复、预算、重试、timeout | 通常否 | 运行时配置，不进入镜像 |
| `instruction.md`、`solution/`、`pre_artifacts.sh`、展示 metadata | 通常否 | 本身不改变镜像内容，但仍会创建新 Trial 并检查缓存 |

Docker Compose v5 可能默认把 Compose 构建交给 Buildx Bake。Bake 是构建编排入口，不是 Task 或模型设置；Windows 上多 Trial 并行时可能长时间无进度输出。`run-ui.ps1` 默认设置 `COMPOSE_BAKE=false`（保留用户显式值），手动启动时可这样设置:

```powershell
$env:COMPOSE_BAKE = "false"
.\run-ui.ps1
```

环境变量必须在启动 UI/backend 前设置，子进程才能继承；UI 已运行时需停止后重启。它只改为普通逐服务构建，不会跳过镜像构建，也不会关闭 BuildKit 层缓存。若根因是 Docker Hub、apt 或 GitHub release 的 403/超时，仍需单独解决网络或镜像源。

排查顺序:

1. 看 Trial 目录是否持续生成/更新 `agent-build-context`、`trial.log` 与 Compose 文件
2. 用 `docker buildx history ls` 查看 build 是 `Running`、`Completed` 还是 `Error`
3. 用 `docker buildx history logs <BUILD_ID>` 定位卡在基础镜像、apt、uv/Agent 安装还是镜像导出
4. build 已完成且容器已启动后，后端 `/api/runs/<id>` 应进入 `agent_running`；Patch、verifier 日志、reward 和最终 result 出现时应逐步进入 `preparing_verifier`、`verifier`、`finalizing`、`completed`；UI 仍显示旧阶段时刷新页面或检查 SSE 连接

全局 FIFO 队列没有“批次栅栏”。Pier 的 Trial wrapper 在每个完整 Trial 返回后立即 `release_slot`，等待中的下一个 Trial 约每 `0.2s` 重新申请；因此 Agent 提前结束但 verifier 未结束时不能释放，完整结果落盘后则不必等待同批次其他 Trial。批次看起来同时变化，通常只是多个 verifier 完成时间接近或前端在同一 SSE 更新窗口收到多个结果。

### 9.6.1 本地稳定镜像的实现经验

新建自定义 Task 时，如果希望后续运行跳过重复的 Docker build，不要把镜像归档提交到 Git。给 Task 和独立 verifier 使用 `:local` 标签，由 `backend/app/local_images.py` 按构建上下文 checksum 管理即可:

- 首次运行会构建；后续只要 Dockerfile、`environment/repo`、`tests` 内容和依赖本地镜像 digest 没变，就直接复用。
- 仅把 `docker_image` 写进 `task.toml` 还不够：没有镜像时 Pier 会尝试拉取 registry；本项目的 preflight 必须先执行本地按需构建。
- 本地镜像准备只有一个入口：后台 preflight。没有必要再维护一个可选的包装脚本；新机器首次运行和构建输入变化都由同一条自动路径处理，避免文档、参数与行为出现两套口径。
- verifier 的 checksum 要包含基础 Agent/Task 镜像 digest，否则基础镜像更新后可能错误复用旧隐藏测试镜像。
- 独立 verifier 不能继承同一个本地 Agent 镜像标签；应给 `[verifier.environment].docker_image` 单独命名并从 Task 基础镜像构建，否则 Pier 的 prebuilt 路径不会执行 `tests/Dockerfile`。
- Docker label 比单独的旁车 manifest 更可靠，因为镜像和它的输入摘要一起移动、检查和清理；构建日志单独落盘，便于定位首次网络等待或构建失败。
- `:local` 适用于单机开发和流程验证；跨机器、CI 或团队共享应使用 registry digest，并保留同一 Dockerfile 作为可重建来源。2026-07-15 实测四个本地镜像首次准备约 188 秒，第二次全部命中约 0.4 秒；`nop` 的环境准备约 2 秒、`oracle` 两题均 Reward=1。Pier 仍会按 Agent 类型在运行时安装 Agent 工具，因此这套优化主要消除 Task/verifier 基础镜像构建等待，不等于把 Agent 安装也预烘焙进 Task 镜像。

- **共享本地 verifier 镜像不能被 Trial 清理掉**：Pier 的 separate verifier 在结束时默认执行 `docker compose down --rmi all`。如果 verifier 直接使用 `deepswe-*:local`，第一个 trial 成功后标签会被删除，后续 trial 会误尝试从 Docker Hub 拉取并报 `pull access denied`。`pier_retry_patch/sitecustomize.py` 现在只对 `use_prebuilt + :local` 的清理命令移除 `--rmi all`，仍会删除容器、卷和孤立资源；registry 镜像及每个 trial 自己构建的镜像继续按 Pier 原策略清理。
- 这条防线必须用并发的真实 verifier 运行验证，不能只看单个 trial：2026-07-15 的 `RUN-000009` 两个 Task 各 1 个真实 `mini-swe-agent` trial 均 `Reward=1`、F2P `5/5`、P2P `3/3`、无错误；Run 结束后两个 `:local` verifier 标签仍可 `docker image inspect`。环境准备分别为约 12.1 秒和 11.9 秒（模型执行耗时另计），说明基础镜像和 Agent 安装层缓存均已命中。

### 9.7 本次真实调用证据

2026-07-15 使用 `mini-swe-agent 2.4.5` 和 `openai/gpt-5.6-sol`（`xhigh`）完成两次真实付费 Trial:

| Task | Steps | 输入 / 缓存 / 输出 Token | 费用 | 结果 |
|---|---:|---:|---:|---|
| `test-python-slugify-workflow` | 9 | 64,492 / 46,592 / 3,500 | $0.217796 | `Reward=1`，F2P 5/5，P2P 3/3 |
| `test-python-summary-workflow` | 12 | 99,683 / 70,144 / 5,471 | $0.346897 | `Reward=1`，F2P 5/5，P2P 3/3 |

合计费用 `$0.564693`。两个 Trial 都有模型 trajectory、提交记录、`model.patch` 与 verifier `reward.json`，因此可确认是实际模型调用后的端到端通过，不是由参考 Patch 代替。原始产物路径记录在 `PROJECT-PLAN.md` §6.1。
