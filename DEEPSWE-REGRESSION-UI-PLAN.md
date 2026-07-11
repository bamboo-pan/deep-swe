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

### 前端

- React
- TypeScript
- Vite
- Tailwind CSS
- Recharts 或 ECharts

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
- Codex CLI `0.144.1`
- Claude Code `2.1.198`
- Docker CLI `29.6.1`
- Python `3.12.10`
- Node.js `24.18.0`
- 12 个逻辑 CPU
- 31.5 GB 物理内存
- 当前 DeepSWE 数据集包含 113 个任务

`mini-swe-agent` 没有作为独立命令安装，但 Pier 已内置 `mini-swe-agent` agent 类型。

当前 Docker Desktop daemon 未运行。UI 需要检测该状态，在 Docker 就绪前禁止开始测试，并提供打开 Docker Desktop 的入口。

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
- 并发 4：红色警告并要求手动确认
- 并发大于 4：默认禁止，可在高级设置中解除

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

如果凭据文件中的 URL 使用：

```text
http://127.0.0.1:9887/v1
```

主机侧继续使用该地址，Docker 中自动转换为：

```text
http://host.docker.internal:9887/v1
```

UI 仅展示脱敏信息，例如：

```text
URL：http://127.0.0.1:9887/v1
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

统一接口：

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

使用 OpenAI Responses API，参数目标为：

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
base_url = "http://host.docker.internal:9887/v1"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
```

当前 Pier Codex 运行还需要兼容：

```text
CODEX_FORCE_AUTH_JSON=true
config_toml_file=codex-local-provider.toml
```

### 8.3 Claude Code

通过 Pier 启动：

```text
pier run --agent claude-code
```

Claude Code 使用本地代理提供的 Anthropic Messages API，但模型仍指定为：

```text
gpt-5.6-sol
```

初始化向导需要自动探测 reasoning effort 的实际映射方式，包括：

- Claude Code 原生 effort 设置
- Anthropic thinking 配置
- 本地代理自定义 Header
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

- 用户选择值
- adapter 转换值
- 实际发送值
- 响应或日志中观察到的有效值

这样可以防止 agent 或代理升级后参数映射发生变化而未被发现。

## 10. 默认任务集

默认任务集命名为：

```text
regression-standard-7
```

包含：

1. `dasel-html-document-format`
2. `igel-persist-feature-schema`
3. `csstree-shorthand-expansion-compression`
4. `happy-dom-deterministic-intersectionobserver`
5. `superjson-error-stack-serialization`
6. `dateutil-rfc5545-timezone-interop`
7. `actionlint-action-pinning-lint`

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

如果响应包含 `service_tier`，自动选择对应价格；未返回时默认使用 Standard。

UI 区分展示：

- API 返回的实际费用
- 根据 Token 和官方价格计算的估算费用
- 本地代理报告的额度
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

- Docker、Pier、本地代理健康状态
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
- 官方平均耗时
- 官方通过率
- F2P/P2P 数量
- 本地历史结果
- 最近失败原因

### 12.7 Settings

配置：

- 凭据文件路径
- 默认 agent/model/effort
- 默认并发数
- Jobs 目录
- 价格表和 service tier
- Pier/Codex/Claude Code 路径
- Docker 配置

### 12.8 Diagnostics

检测：

- Docker daemon
- 本地代理 URL
- 容器访问 `host.docker.internal`
- 凭据格式和认证
- OpenAI Responses API
- Anthropic Messages API
- 三种 agent 的模型支持
- 六档 reasoning effort 的映射
- Pier Windows 补丁
- 磁盘和内存

## 13. 运行进度与进程管理

后端启动 Pier 子进程并保存 PID、命令结构和 Job 目录。

进度信息来源：

- Pier stdout/stderr
- `job.log`
- Trial 目录创建情况
- Agent 日志
- `result.json`
- verifier 结果文件

取消运行时：

1. 请求 Pier 正常终止。
2. 超时后终止 Pier 进程树。
3. 只清理当前 Job 创建的 Docker 容器和网络。
4. 保留已经生成的日志和部分结果。
5. 删除临时凭据文件。

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

主要表：

- `settings`
- `agent_profiles`
- `task_suites`
- `runs`
- `trials`
- `trial_metrics`
- `baselines`
- `official_task_stats`
- `environment_snapshots`
- `price_tables`

凭据 Token 不进入任何数据库表。

## 16. 可复现性记录

每个 Job 保存：

- Pier 版本
- Agent CLI 版本
- 模型完整名称
- reasoning effort
- service tier
- DeepSWE Git commit
- Task digest
- Docker image digest
- Provider URL（不含 Token）
- Agent timeout
- Verifier timeout
- 并发数
- 重复次数
- 完整但已脱敏的启动配置
- Credential 指纹
- Pier 原始结果和日志

这些数据用于区分：

- 模型能力变化
- Agent harness 变化
- CLI/Pier 升级
- Prompt 或配置变化
- Docker 环境变化

## 17. 环境预检

开始运行前检查：

- Docker Desktop 是否运行
- `docker info` 是否成功
- 凭据文件是否存在且可读
- 凭据格式是否正确
- 本地代理是否可访问
- OpenAI Responses API 是否可用
- Anthropic Messages API 是否可用
- `gpt-5.6-sol` 是否可用
- 所选 reasoning effort 是否生效
- 容器能否访问 `host.docker.internal`
- Pier、Codex、Claude Code 版本
- 磁盘空间
- 可用内存
- 任务 Docker 镜像状态

还需要检测当前本机 Pier 的 Windows 补丁是否仍然存在：

1. Pier 生成的 Linux Shell 文件使用 LF。
2. Squid 允许访问端口 9887。
3. Codex trajectory 使用 UTF-8 读取。

Pier 升级后如果补丁丢失，UI 应给出明确错误和修复说明。第一版不自动修改 Pier 安装。

## 18. 基线与降智判断

首次建立基线时，建议每题运行 4 次，共 28 trials。

周期测试虽然第一版手动触发，但可以把某次运行设为正式基线。

初始告警条件：

- 总通过率相对基线下降至少 4/28，约 14 个百分点
- 至少两个任务从基线的 3–4/4 降为 0–1/4
- 成功 Trial 的归一化耗时增加超过 25%
- 耗时下降超过 35% 且通过率同时下降，怀疑提前终止
- 控制任务 `actionlint-action-pinning-lint` 通过率下降到 1/4 或更低

一次异常先标记为黄色；相同配置连续两次异常再标记为红色。

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

