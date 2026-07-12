# DeepSWE 模型降智测试工具方案

## 1. 项目目标

构建一个运行在本地 Windows 主机上的 Web UI 工具，用于方便地配置、执行和比较 DeepSWE 测试。

工具需要支持：

- `mini-swe-agent`
- Codex
- Claude Code
- 可配置模型及 reasoning effort
- 默认使用 `gpt-5.6-sol / high`
- 使用本地 Docker 执行 DeepSWE 任务
- 实时查看任务进度、日志、Token 和费用
- 对比不同 agent、模型、effort 和运行日期的结果
- 与 DeepSWE 官方榜单进行参考比较
- 建立本地基线并检测模型是否降智

第一版只支持手动运行，不实现定时调度。

## 2. 总体架构

```text
浏览器 UI
   │
   ▼
本地 FastAPI 服务 ─── SQLite
   │                    ├─ 运行配置
   │                    ├─ Trial 指标
   │                    └─ 历史基线
   ├─ Docker 资源清理（docker_cleanup，见 §21）
   ▼
Pier Job Supervisor
   ├─ mini-swe-agent adapter
   ├─ Codex adapter
   └─ Claude Code adapter
          │
          ▼
DeepSWE Docker 环境
          │
          ▼
本地模型入口
```

UI 和调度服务运行在 Windows 主机，DeepSWE agent 和 verifier 继续运行在 Docker 容器中。

## 3. 技术栈

### 后端

- Python 3.12
- FastAPI
- Pydantic
- SQLAlchemy
- SQLite
- SSE 或 WebSocket 实时推送
- 后台子进程管理 Pier
- psutil（残留进程收割与磁盘/内存检查）

### 前端

- React
- TypeScript
- Vite
- 原生 CSS（深色响应式界面）
- 当前比较视图使用 CSS Grid 热力矩阵，尚未引入图表库

### 启动方式

提供 PowerShell 启动脚本：

```powershell
.\run-ui.ps1
```

启动后自动打开：

```text
http://127.0.0.1:8765
```

默认只监听本机，不开放局域网访问。

## 4. 当前本机环境

已确认：

- Pier `0.3.0`
- 容器 Agent：由 Pier adapter 在任务容器内安装；未指定版本时使用 `latest`
- 容器实际 Agent 版本：在 Trial 完成后从 `agent_info.version` 记录和展示
- Docker CLI `29.6.1`
- Python `3.12.10`
- Node.js `24.18.0`
- 12 个逻辑 CPU
- 31.5 GB 物理内存
- 当前 DeepSWE 数据集包含 113 个任务

`mini-swe-agent`、Codex 和 Claude Code 均由 Pier adapter 在隔离的任务容器内安装和执行。Windows 本地 CLI 版本不作为实际运行版本展示，避免与容器版本混淆。

Docker Desktop daemon 当前可用。UI 通过 `docker info` 持续检测其状态。

## 5. 默认运行配置

```yaml
credential_file: C:\Users\bamboo2026\Documents\github\codex1.txt

agent: mini-swe-agent
model: gpt-5.6-sol
reasoning_effort: high

reasoning_effort_options:
  - none
  - low
  - medium
  - high
  - xhigh
  - max

task_suite: regression-standard-7
attempts_per_task: 1
concurrency: 2
environment: docker
verification: true
service_tier: standard
```

三种 agent 都默认使用：

```text
gpt-5.6-sol / high
```

## 6. 并发策略

筛选出的 7 个任务均声明：

- 2 CPU
- 8192 MB 内存

本机具有 12 个逻辑 CPU 和 31.5 GB 内存。默认并发设为 2：

```text
2 trials × 2 CPU = 4 CPU
2 trials × 8 GB = 16 GB
```

这样可以给 Windows、Docker Desktop、Pier、Web UI 和本地模型服务保留足够资源。

UI 限制：

- 并发 1–2：正常
- 并发 3：黄色内存警告
- 并发 4：红色警告并要求手动确认（前端确认对话框 + 后端 `confirm_high_concurrency` 双重把关）
- 并发大于 4：禁止；第一版不提供解除入口

## 7. 凭据处理

凭据文件路径：

```text
C:\Users\bamboo2026\Documents\github\codex1.txt
```

文件格式：

```text
第一行：API URL
第二行：Token
```

解析规则：

- 使用 UTF-8 读取
- 去除 UTF-8 BOM
- 去除每行首尾空格
- 忽略末尾空行
- 必须成功解析出一个 URL 和一个 Token
- URL 必须为 HTTP 或 HTTPS
- Token 不写入 SQLite
- Token 不出现在命令行参数中
- Token 不出现在日志或前端 API 响应中

模型 API 地址完全由凭据文件第一行配置，不由 UI 固定端口。如果凭据文件中的 URL 使用：

```text
http://127.0.0.1:<port>/v1
```

主机侧继续使用该地址，Docker 中自动转换为：

```text
http://host.docker.internal:<port>/v1
```

UI 仅展示脱敏信息，例如：

```text
URL：http://127.0.0.1:<port>/v1
凭据：codex1.txt
Token：••••••••abcd
状态：已验证
```

每次运行时：

1. 后端读取凭据文件。
2. 根据 agent 生成临时环境变量或临时认证文件。
3. 为临时文件设置严格的 Windows ACL。
4. Job 完成或取消后删除临时文件。
5. 对 Bearer Token、API Key 和认证 JSON 进行日志脱敏。

## 8. Agent 适配层

目标统一接口（后续重构方向；当前第一版的适配逻辑内联在 `runner.py` 的分支中，新增 Agent 时需同步修改 schemas、runner、results 与 diagnostics）：

```python
class AgentAdapter:
    def validate_config(...): ...
    def prepare_credentials(...): ...
    def probe_capabilities(...): ...
    def build_pier_command(...): ...
    def parse_progress(...): ...
    def normalize_result(...): ...
    def cleanup(...): ...
```

### 8.1 mini-swe-agent

通过 Pier 启动：

```text
pier run --agent mini-swe-agent
```

使用 mini-swe-agent/Pier 的 OpenAI 兼容模型接口；这不限定必须连接 OpenAI 官方服务，兼容该协议的模型 API 均可使用。参数目标为：

```json
{
  "model": "gpt-5.6-sol",
  "reasoning": {
    "effort": "high"
  }
}
```

### 8.2 Codex

通过 Pier 启动：

```text
pier run --agent codex
```

每次运行生成临时 Codex 配置：

```toml
model = "gpt-5.6-sol"
model_reasoning_effort = "high"
model_provider = "local_proxy"

[model_providers.local_proxy]
name = "Local Proxy"
base_url = "http://host.docker.internal:<port>/v1"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
```

当前实现每次运行生成临时 Codex provider 配置和临时 `auth.json`，并通过以下参数注入容器：

```text
CODEX_AUTH_JSON_PATH=<temporary-auth.json>
config_toml_file=<temporary-provider.toml>
reasoning_effort=<用户所选档位>
```

reasoning effort 通过双通道透传：临时 TOML 写入 `model` 与 `model_reasoning_effort`，同时显式传递 `--agent-kwarg reasoning_effort=<v>`（pier codex adapter 展开为 `-c model_reasoning_effort=<v>`）。必须显式传递——pier 的该参数默认值为 `high`，缺省时用户所选档位会被静默覆盖。

### 8.3 Claude Code

通过 Pier 启动：

```text
pier run --agent claude-code
```

Claude Code 使用模型 API 提供的 Anthropic Messages 兼容接口，但模型仍指定为：

```text
gpt-5.6-sol
```

初始化向导需要自动探测 reasoning effort 的实际映射方式，包括：

- Claude Code 原生 effort 设置
- Anthropic thinking 配置
- 模型 API 自定义 Header
- 模型别名，例如 `gpt-5.6-sol-high`

探测成功后保存能力映射。未确认支持的档位必须在 UI 中标记为不可用，不能静默忽略或假装已应用。

## 9. Reasoning effort

UI 提供 GPT-5.6 官方支持的全部档位：

```text
none / low / medium / high / xhigh / max
```

默认值：

```text
high
```

每次运行同时记录：

- 用户选择值（`reasoning_effort`）
- adapter 转换值（`reasoning_effort_adapter`，即实际发送给 agent 的参数形式）
- 响应或日志中观察到的有效值（`reasoning_effort_effective`）

有效值只能来自运行后的观测；第一版尚无观测机制，该字段如实存 `null`，不复制请求值伪装成观测结果。

这样可以防止 agent 或代理升级后参数映射发生变化而未被发现。

## 10. 默认任务集

默认任务集命名为：

```text
regression-standard-7
```

包含，并在 UI 中使用固定套件 ID 对照：

1. `T01` — `dasel-html-document-format`
2. `T02` — `igel-persist-feature-schema`
3. `T03` — `csstree-shorthand-expansion-compression`
4. `T04` — `happy-dom-deterministic-intersectionobserver`
5. `T05` — `superjson-error-stack-serialization`
6. `T06` — `dateutil-rfc5545-timezone-interop`
7. `T07` — `actionlint-action-pinning-lint`

每个任务同时展示：套件 ID、真实标题、本地目录名和数据集 `ext_id`，避免显示名称与实际任务目录难以对应。

其中前 6 个是区分任务，`actionlint-action-pinning-lint` 是控制任务。

提供两种快捷模式：

- 快速检查：每题运行 1 次，共 7 trials
- 建立基线：每题运行 4 次，共 28 trials

根据 DeepSWE 官方 mini-swe-agent 的 GPT-5.6-SOL High 数据：

- 7 题顺序执行约 55 分钟
- 本机并发 2 的预计墙钟时间约 30–45 分钟
- 28-trial 基线模式预计约 2–3 小时

Codex 和 Claude Code 需要首次实测后建立各自的时间与通过率基线。

## 11. 官方成本计算

GPT-5.6-SOL Standard API 的官方价格按每 100 万 Token 计算：

| Token 类型 | 价格 |
|---|---:|
| 未缓存输入 | $5.00 |
| 缓存读取 | $0.50 |
| 缓存写入 | $6.25 |
| 输出 | $30.00 |

计算公式：

```text
cost =
  uncached_input_tokens × $5.00 / 1M
+ cached_read_tokens    × $0.50 / 1M
+ cache_write_tokens    × $6.25 / 1M
+ output_tokens         × $30.00 / 1M
```

支持的价格层级：

| Service Tier | 输入 | 缓存读取 | 缓存写入 | 输出 |
|---|---:|---:|---:|---:|
| Standard | $5.00 | $0.50 | $6.25 | $30.00 |
| Batch/Flex | $2.50 | $0.25 | $3.125 | $15.00 |
| Priority | $10.00 | $1.00 | $12.50 | $60.00 |

价格按运行创建时用户选择的 service tier 计算（Pier stats 未返回响应级 `service_tier`，自动选层留待后续）。

Pier stats 不区分 cache-write Token，因此估算费用暂不含 cache-write（$6.25/1M）项，`cache_write_tokens` 字段按 §14 约定存 `null`。

UI 区分展示：

- API 返回的实际费用
- 根据 Token 和官方价格计算的估算费用
- 模型 API 报告的额度
- 各数据之间的差异

Responses API 使用字段可能包括：

- `input_tokens`
- `cached_tokens`
- `cache_write_tokens`
- `output_tokens`

Anthropic Messages API 使用字段可能包括：

- `input_tokens`
- `cache_read_input_tokens`
- `cache_creation_input_tokens`
- `output_tokens`

adapter 必须先规范化 Token 类型，再计算费用，避免重复计算缓存 Token。

官方价格来源：

https://developers.openai.com/api/docs/pricing

## 12. UI 页面

### 12.1 Dashboard

展示：

- Docker、Pier、模型 API 健康状态
- 当前运行的 Job
- 最近一次测试结果
- 最近基线
- 通过率变化
- 耗时和费用变化
- 降智告警摘要

### 12.2 New Run

配置：

- Agent
- 模型
- Reasoning effort
- 任务集
- 自定义任务选择
- 每题重复次数
- 并发数
- Agent timeout
- Verifier timeout
- 是否自动重试基础设施错误
- 是否启用 verifier
- 单 Agent 运行或三 Agent 同时运行

“三 Agent 同时跑”会分别创建 mini-swe-agent、Codex、Claude Code 三条独立 Run。配置中的并发数是每条 Run 的 worker 数，总 worker 上限为 `Agent 数 × 每 Agent 并发数`。

### 12.3 Live Progress

阶段：

```text
等待
→ 凭据与环境预检
→ 准备环境
→ 构建/启动容器
→ Agent 运行
→ 提取 Patch
→ Verifier
→ 解析结果
→ 完成
```

展示：

- 总进度
- 每个 task/trial 的状态
- 当前阶段
- 实时日志
- 已用时间
- Token
- 费用
- Agent steps
- Docker 容器状态
- Patch 预览
- 取消按钮
- 多 Agent 同跑时的运行切换器
- 点击 Trial 后直接在实时页查看 Patch、错误和日志

### 12.4 Results

展示：

- 总通过率
- 每题通过率
- Reward
- Partial score
- F2P/P2P
- Trial duration
- Agent duration
- Token 和费用
- Agent steps
- 失败原因
- Patch 和日志
- Job 多选、全选和批量删除
- 删除时同步清理数据库索引、基线、Job artifacts 和 supervisor 日志
- 运行中的 Job 必须先取消，不能直接删除

### 12.5 Compare

支持：

- 同模型不同日期
- 同模型不同 effort
- mini-swe-agent vs Codex vs Claude Code
- 不同模型比较
- 当前运行 vs 本地基线
- 当前运行 vs DeepSWE 官方 mini-swe-agent 数据

需要区分：

```text
官方严格可比：mini-swe-agent ↔ 官方 mini-swe-agent
参考比较：Codex/Claude Code ↔ 官方 mini-swe-agent
严格纵向比较：同一 agent、版本、模型和配置之间
```

图表包括：

- Task × Agent/模型热力图
- 通过率趋势
- 任务耗时趋势
- Token/费用趋势
- Effort 对比
- 失败类型分布

### 12.6 Tasks

展示：

- 任务说明
- 语言和仓库
- 官方平均耗时（已实现：来自官方 `artifacts/v1.1/trials.json` 全模型全档位聚合，口径与官方站点"ALL MODEL EFFORTS"一致，已对照控制任务 80% / 16m26s 校验；聚合缓存随仓库分发于 `data/official-task-stats-v1.1.json`，Tasks 页可手动"同步官方统计"刷新）
- 官方通过率（已实现，同上；New Run 任务选择器同样展示，作为挑选任务的快速依据）
- F2P/P2P 数量
- 本地历史结果
- 最近失败原因

### 12.7 Settings

配置：

- 凭据文件路径
- 默认 agent/model/effort
- 默认并发数
- Jobs 目录
- Docker 存储与清理卡片（实际总占用、可回收空间、Trial 镜像数、构建缓存；扫描/预览/确认清理入口；`docker_cleanup_after_run`、`docker_cleanup_on_delete`、`docker_cache_retention_hours`、`docker_cache_warning_gb` 四项设置，见 §21）
- 价格表和 service tier
- Pier 路径和容器 Agent 安装策略

### 12.8 Diagnostics

第一版已实现的检测：

- Docker daemon（`docker info`）
- Pier CLI 版本与 Windows secret-env 补丁
- 三种容器 Agent 的实际版本（Trial 完成后从 `agent_info.version` 读取）
- 凭据格式与脱敏展示
- 模型 API URL 可达性（来自凭据文件，不固定端口；探测不携带 Token）
- 磁盘和内存
- Docker 存储：镜像占用与可回收空间、构建缓存（超阈值告警）、孤儿 Trial 镜像、自动清理策略状态

后续增强：

- 容器内访问 `host.docker.internal` 的探测
- OpenAI Responses API 与 Anthropic Messages API 双协议探测
- `gpt-5.6-sol` 可用性与六档 reasoning effort 的映射验证
- 任务 Docker 镜像状态

## 13. 运行进度与进程管理

后端启动 Pier 子进程并保存 PID、命令结构和 Job 目录。POSIX 上以独立会话启动（`start_new_session`），Windows 上使用新进程组，保证可整树终止。

进度信息来源：

- Pier stdout/stderr
- `job.log`
- Trial 目录创建情况
- Agent 日志
- `result.json`
- verifier 结果文件

SSE 推送以 `result.json` mtime 指纹检测变化（附 5 秒兜底刷新），不逐秒全量解析 artifacts；推送内容不含 Patch 正文，Patch 由前端点击 Trial 时按需拉取。

取消运行时：

1. 请求 Pier 正常终止；当前 Windows 实现终止 Pier 进程树。
2. 等待进程退出（最长 20 秒）。
3. 状态迁移加锁做条件更新：运行已先行进入终态时不把 completed 改写为 cancelled，反之取消已提交后迟到的结果落库也不得覆盖 cancelled。
4. 从当前 Job 的 Trial 目录提取资源标识，按 §21 定向清理匹配的 Docker 容器、网络和 Trial 镜像。
5. 保留已经生成的日志和部分结果。
6. 删除临时凭据文件。

结果落库时校验 Pier 退出码：非零退出码强制标记 `failed` 并记录退出码，不依据残缺 `result.json` 判定成功。

服务生命周期：

- 启动时收割上次会话残留的非终态运行——按记录的 PID 终止 pier 进程树（psutil 校验目标命令行仍指向 pier，防止 PID 被无关进程复用后误杀）、执行 Docker 定向清理、标记 `interrupted`。
- 退出时终止仍在运行的 pier 子进程，防止孤儿进程继续调用付费 API。

## 14. 统一结果结构

三种 agent 的结果统一为：

```json
{
  "agent": "codex",
  "agent_version": "0.144.1",
  "model": "gpt-5.6-sol",
  "reasoning_effort_requested": "high",
  "reasoning_effort_effective": "high",
  "task": "dasel-html-document-format",
  "passed": true,
  "reward": 1.0,
  "partial": 1.0,
  "f2p": 1.0,
  "p2p": 1.0,
  "trial_duration_seconds": 512,
  "agent_duration_seconds": 460,
  "uncached_input_tokens": 120000,
  "cached_read_tokens": 800000,
  "cache_write_tokens": 30000,
  "output_tokens": 25000,
  "estimated_cost_usd": 1.7875,
  "reported_cost_usd": null,
  "steps": 27
}
```

不支持或未返回的字段保存为 `null`，不进行推测。

## 15. 数据库设计

当前第一版实际表：

- `settings`
- `runs`（含 `jobs_dir` —— 创建时的 Jobs 目录，保证改设置后历史运行的 artifacts 仍可定位；`pier_version` —— 运行时的 Pier 版本）
- `baselines`

Trial 和指标直接从 Pier Job artifacts、Trial `result.json` 和日志中解析，不重复写入 SQLite。禁用 verifier 的运行 reward/passed 存 `null`（"未测量"与"全部失败"必须可区分）。`agent_profiles`、`task_suites`、`official_task_stats`、`environment_snapshots` 和 `price_tables` 仍属于后续扩展设计。

凭据 Token 不进入任何数据库表。

## 16. 可复现性记录

每个 Job 保存（标注为"待补"的字段尚未落库）：

- Pier 版本（已实现，运行创建时记录）
- Agent CLI 版本（Trial 完成后从 `agent_info.version` 读取展示）
- 模型完整名称（已实现）
- reasoning effort（已实现：请求值 + adapter 映射值；有效值观测待补）
- service tier（已实现）
- DeepSWE Git commit（待补）
- Task digest（待补）
- Docker image digest（待补）
- Provider URL（不含 Token）（诊断页展示，未按 Run 落库，待补）
- Agent timeout / Verifier timeout（已实现）
- 并发数、重复次数（已实现）
- 创建时的 Jobs 目录（已实现）
- Credential 指纹（诊断页展示，未按 Run 落库，待补）
- Pier 原始结果和日志（已实现，Job artifacts 原样保留）

这些数据用于区分：

- 模型能力变化
- Agent harness 变化
- CLI/Pier 升级
- Prompt 或配置变化
- Docker 环境变化

## 17. 环境预检

开始运行前的阻断式检查（已实现，失败时运行标记 failed 并给出明确错误）：

- `docker info` 是否成功
- 所选任务目录是否存在
- 凭据文件是否存在、可读且格式正确

Diagnostics 页的非阻断检查（已实现）：

- Docker daemon 与版本
- 模型 API 是否可访问（探测不携带 Token）
- Pier 版本与三种容器 Agent 的真实版本
- 磁盘空间与可用内存
- Docker 镜像占用、构建缓存、孤儿 Trial 镜像与清理策略状态

后续增强：

- OpenAI Responses API / Anthropic Messages API 双协议可用性
- `gpt-5.6-sol` 是否可用
- 所选 reasoning effort 是否生效
- 容器能否访问 `host.docker.internal`
- 任务 Docker 镜像状态

还需要检测当前本机 Pier 的 Windows 补丁是否仍然存在：

1. Pier 生成的 Linux Shell 文件使用 LF。
2. Squid 允许访问凭据文件 URL 中配置的模型 API 端口。
3. Codex trajectory 使用 UTF-8 读取。

当前实现检测 secret-env 补丁的特征标识（`process_env_overrides`），避免精确字符串匹配在 Pier 升级重排代码后永久误报。Pier 升级后如果补丁丢失，UI 给出明确错误；第一版不自动修改 Pier 安装。

## 18. 基线与降智判断

首次建立基线时，建议每题运行 4 次，共 28 trials。

周期测试虽然第一版手动触发，但可以把某次运行设为正式基线。

初始告警条件（五条规则均已实现）：

- 总通过率相对基线下降至少 4/28，约 14 个百分点
- 至少两个任务从基线的 3–4/4 降为 0–1/4
- 成功 Trial 的归一化耗时增加超过 25%
- 耗时下降超过 35% 且通过率同时下降，怀疑提前终止
- 控制任务 `actionlint-action-pinning-lint` 通过率下降到 1/4 或更低

一次异常先标记为黄色（warning）；相同配置连续两次异常再标记为红色（danger）。该升级逻辑已实现：对同配置（agent + 模型 + effort）中本次之前最近一次已完成运行按同一基线复算规则，两次均异常时升级。

## 19. 实施阶段

### 第一阶段：基础架构

- 创建 FastAPI、React 和 SQLite 项目结构
- 实现配置模型和数据库迁移
- 实现凭据安全读取与日志脱敏
- 实现环境预检
- 实现默认并发判断

### 第二阶段：Agent 适配

- 跑通 mini-swe-agent 单任务
- 跑通 Codex 单任务
- 探测 Claude Code 的 GPT 模型与 effort 映射
- 统一命令生成、进度和结果格式

### 第三阶段：运行 UI

- New Run 页面
- 实时进度
- 日志查看
- Token 和成本
- 取消运行
- Job 历史

### 第四阶段：结果与比较

- 解析 Pier、verifier 和 trajectory 文件
- 同步 DeepSWE 官方数据
- 结果详情
- Agent/模型/effort 对比
- 基线管理
- 降智告警

### 第五阶段：完善与交付

- Windows 一键启动脚本
- 错误恢复
- CSV/JSON 导出
- 数据备份和恢复
- 使用文档
- 完整端到端测试

## 20. 第一版验收标准

第一版完成时应满足：

1. 可以从本地 Web UI 选择三种 agent。
2. 可以选择 `gpt-5.6-sol` 和六档 reasoning effort。
3. 可以安全读取指定的两行凭据文件。
4. 可以运行默认 7 题或其中任意任务。
5. 默认并发为 2，并能修改。
6. 可以实时查看每个 Trial 的状态和日志。
7. 可以取消正在运行的 Job。
8. 可以查看通过率、F2P/P2P、耗时、Token 和费用。
9. 可以比较两个或多个历史运行。
10. 可以把一次运行设为基线并显示回归告警。
11. Token 成本使用 OpenAI 官方价格计算。
12. Token、认证文件和完整凭据不会泄漏到数据库、日志或前端。

## 21. Docker 资源清理

详细设计见 `DOCKER-RESOURCE-CLEANUP-INTEGRATION-PLAN.md`。阶段一至三已实现，摘要如下。

背景：每个 Trial 经 Pier 产生带随机后缀的 Compose 项目，Docker Desktop 中持续累积 `<task>__<trial-id>-main`、`*-pier-egress-proxy` 及 verifier 变体镜像条目。共享层由 Docker 复用，主要问题是条目污染与 BuildKit 缓存增长，而非每条目独占数 GB。

已实现（`backend/app/docker_cleanup.py`）：

- 资源识别：候选只从 Job artifacts 的 Trial 目录名推导，按 Pier 的 Compose project 规整规则（小写化、首字符补 `0`、非法字符替换）归一后做前缀 + 服务后缀白名单匹配；永不接受前端传入的镜像名，永不使用只按 `*-main` 匹配的全局正则。
- 三个生命周期接入点：取消运行后、Pier 进程退出后（`docker_cleanup_after_run` 控制）、删除历史 Run 时（清理先于 artifacts 删除，响应携带清理摘要）。
- 安全边界：镜像删除不使用 `--force`；被容器引用的镜像跳过并报告；任务声明的基础镜像、`ubuntu:24.04` 等公共镜像不进入候选；不执行 `docker system prune -a` 或无范围 `docker image prune -a`；全局清理锁防三 Agent 并行互扰；Docker 不可用或清理失败不改变评测结果状态；全部操作幂等，结果写入 `<job>.docker-cleanup.json` 审计。
- API：`GET /api/docker/storage`、`POST /api/docker/cleanup/preview`、`POST /api/docker/cleanup`（scope：job / orphaned / expired / build_cache；存在活动 Run 时批量清理与缓存清理返回 409）。
- BuildKit 缓存：不随运行自动清理；Settings 页提供按保留期（默认 7 天）的手动清理，先预览可回收空间并二次确认，`builder prune` 经 stdin 确认而非 `-f`。
- UI：Settings 的"Docker 存储与清理"卡片展示实际总占用、可回收空间、Trial 镜像数与构建缓存（"预计释放"只采用 `docker system df -v` 的独占空间数据，不累加逻辑 Size）；删除运行的确认文案与完成汇总包含镜像清理结果；Diagnostics 增加镜像、缓存、孤儿与清理策略四项检查。
- 服务启动时的孤儿收割：残留 pier 进程按 PID 终止（命令行校验防误杀）并清理其 Docker 资源。

留待阶段四：每个 Job 写入 `docker-resources.json` 资源清单，artifacts 已被手动删除时按清单精确清理。
