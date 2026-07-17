# `3cda408` 后续代码系统审查报告

## 1. 报告信息

| 项目 | 内容 |
| --- | --- |
| 审查日期 | 2026-07-17 |
| 审查分支 | `review/after-3cda408` |
| 基准 Commit | `3cda4081fed96103a6395de39c85e9b20275e307` |
| 原始代码顶点 | `main@2f0d74f` |
| 审查范围 | `3cda408..main`，共 19 个提交 |
| 变更规模 | 112 个文件，114,660 行新增，1 行删除 |
| 暂存区核验 | 暂存 diff 与原提交区间二进制 diff 哈希一致 |
| 当前结论 | **不建议在修复 P1 问题前合并或用于正式运行** |

本报告记录的是审查结论，不代表问题已经修复。当前分支中的源码仍保持被审查时的状态。

## 2. 执行摘要

本次审查覆盖 FastAPI/SQLAlchemy 后端、React 前端、Provider Proxy、全局 Trial 队列、Docker/Pier 生命周期补丁、备份恢复、结果聚合和定向重试链路。

共确认 7 项主要问题：

| 严重级别 | 数量 | 说明 |
| --- | ---: | --- |
| P1 | 3 | 可能造成仓库外目录删除、取消状态失效、持久化数据破坏和全站接口 500 |
| P2 | 4 | 核心基线功能未接线、Provider 策略绕过、前端显示过期结果、Docker 网络随机冲突 |

自动化测试和构建均通过，但这些问题主要位于输入边界、并发时序和跨模块契约处，现有测试未覆盖相应失败路径。

## 3. 审查方法

1. 验证目标 Commit 是当前 `main` 的祖先，并确认其后共有 19 个提交。
2. 新建 `review/after-3cda408`，通过 `git reset --soft` 将目标 Commit 后的全部代码放入暂存区。
3. 对暂存 diff 与 `3cda408..main` 分别计算哈希，确认内容完全一致。
4. 阅读 API 入口、数据库模型、任务调度、Provider Proxy、进程生命周期、重试合并、文件删除和前端状态管理代码。
5. 运行后端测试、前端生产构建、Python 编译、依赖检查和 diff 格式检查。
6. 使用临时 SQLite 数据库执行无破坏性的对抗验证，复现恢复数据投毒和取消竞态。
7. 对照 `PROJECT-PLAN.md` 与实际调用链，检查文档声称的核心功能是否真正接入。

## 4. 主要问题

### CR-01 `[P1]` 恢复数据可把删除目标指向仓库外

**位置**

- [`backend/app/main.py:805`](backend/app/main.py#L805)
- [`backend/app/main.py:402`](backend/app/main.py#L402)
- [`backend/app/results.py:111`](backend/app/results.py#L111)

**问题**

`restore()` 对 Run 的 `jobs_dir` 只检查其是否为字符串。后续 `jobs_root_for()` 直接将该值转换成 `Path`，删除 Run 时再把它作为 `_safe_job_dir()` 的可信根目录。

`_safe_job_dir()` 只能证明目标是“该根目录的直接子目录”，却无法证明这个根目录属于 DeepSWE。恶意备份可以指定任意父目录，再用一个合法的 `job_name` 指向其中的子目录。

**实际验证**

使用临时数据库恢复以下等价数据：

```json
{
  "job_name": "valuable",
  "jobs_dir": "<临时目录>/outside-root",
  "status": "completed"
}
```

结果：

```text
restore_status 200 {'restored': True, 'skipped_runs': 0}
computed_delete_target <临时目录>/outside-root/valuable
outside_configured_jobs_root True
```

验证过程没有执行删除；但按照当前 `delete_run()` 代码，用户之后删除该恢复记录时会对该目标调用 `shutil.rmtree()`。

**影响**

- 导入恶意或损坏的备份后，删除 Run 可能删除仓库外目录。
- `LESSONS-LEARNED.md` 声称 restore 路径逃逸已经修复，但当前修复只覆盖 `job_name`，没有覆盖攻击者控制的根目录。

**建议**

- 恢复时忽略备份中的 `jobs_dir`，或只允许当前设置和明确登记的历史 Jobs 根目录。
- 删除前将 `jobs_root` 与应用允许的根目录白名单逐一比较。
- 不应把恢复文件中的路径直接作为破坏性操作的信任根。
- 增加“恶意 `jobs_dir` + 合法 `job_name`”回归测试，并断言删除端点拒绝执行。

### CR-02 `[P1]` 立即取消会被执行线程反向覆盖为活动状态

**位置**

- [`backend/app/runner.py:794`](backend/app/runner.py#L794)
- [`backend/app/runner.py:800`](backend/app/runner.py#L800)
- [`backend/app/runner.py:864`](backend/app/runner.py#L864)
- [`backend/app/runner.py:1625`](backend/app/runner.py#L1625)
- [`backend/app/runner.py:2004`](backend/app/runner.py#L2004)

**问题**

新 Run 创建后会立即启动后台线程。如果用户在后台线程第一次读取数据库前取消 Run，`cancel_run()` 会把状态写为 `cancelled` 并记录 `_cancel_requested`。随后 `_execute()` 却无条件执行：

```python
run.status = "preflight"
```

在真正创建 Pier 进程前，代码又可能把状态写为 `queued`，最后才检查 `_cancel_requested` 并返回。因此取消标志阻止了模型进程启动，却没有保护数据库终态。

定向重试线程 `_execute_retry()` 存在同类无条件状态覆盖。

**固定时序验证**

使用临时数据库创建一个已取消 Run，预先设置 `_cancel_requested`，再同步执行 `_execute()`：

```text
initial_status cancelled
status_after_execution_thread_observes_cancel queued
```

**影响**

- API 已返回取消成功，但 Run 会重新显示为 `queued` 或 `preflight`。
- 记录无法删除，并会被 `_active_run_count()` 当作活动运行，阻止 Docker 批量清理。
- 后台仍可能执行耗时的凭据绑定、网络检查、镜像拉取和本地镜像构建。
- 用户必须再次取消，服务重启后才可能被收割为 `interrupted`。

**建议**

- 所有状态迁移统一通过一个带条件更新的函数执行。
- 在 `_execute()` 和 `_execute_retry()` 的第一次数据库写入前，在 `_state_lock` 下检查 Run 是否已终态或已请求取消。
- SQL 更新应带 `WHERE status IN (...)` 条件，禁止终态回到活动状态。
- 增加“创建后立即取消”和“提交重试后立即取消”的确定性并发测试。

### CR-03 `[P1]` Restore 未做字段级校验，可持久化破坏整个 UI 的数据

**位置**

- [`backend/app/main.py:781`](backend/app/main.py#L781)
- [`backend/app/main.py:793`](backend/app/main.py#L793)
- [`backend/app/runner.py:2147`](backend/app/runner.py#L2147)
- [`backend/app/results.py:540`](backend/app/results.py#L540)

**问题**

恢复设置时只验证 `value` 能否被 `json.loads()` 解析，没有验证解析后的类型和范围。恢复 Run 时则把所有数据库列直接复制给 SQLAlchemy 模型，仅对少数字段进行处理。

因此下列数据都可能被持久化：

- 非 JSON 的 `tasks_json` 或 `deleted_trials_json`。
- JSON 合法但类型错误的 `max_parallel_tasks`、预算、超时等设置。
- 非法的 `attempts_per_task`、`agent`、`service_tier` 和状态组合。
- 重复或无效的 Baseline 行。

**实际验证**

恢复 `tasks_json="not-json"` 后：

```text
restore_status 200 {'restored': True, 'skipped_runs': 0}
runs_status_after_malformed_restore 500
```

`/api/runs` 在 `serialize()` 中无保护调用 `json.loads(run.tasks_json)`，因此该错误会跨重启持续存在，直到手工修复数据库。

**影响**

- 一次错误恢复即可令运行列表和依赖该列表的 UI 刷新持续失败。
- 合法 JSON 但错误类型的设置可能令调度、Provider 状态和设置端点返回 500。
- 当前测试只覆盖“不是 JSON”的设置值，没有覆盖“JSON 合法但语义无效”的值。

**建议**

- 为备份格式定义独立、严格的 Pydantic Schema。
- 使用现有 `SettingsUpdate`、Run 枚举和数值边界验证恢复内容。
- 在一个数据库事务提交前完成全量验证；任何一行无效时明确选择整批拒绝或逐行报告。
- 读取历史数据时仍应做防御性解析，避免单条坏记录打断整个列表。

### CR-04 `[P2]` 本地基线和降智告警没有接入调用链

**位置**

- [`backend/app/main.py:594`](backend/app/main.py#L594)
- [`backend/app/results.py:977`](backend/app/results.py#L977)
- [`backend/app/results.py:1009`](backend/app/results.py#L1009)
- [`PROJECT-PLAN.md:136`](PROJECT-PLAN.md#L136)

**问题**

项目计划将“建立本地基线并检测模型降智”列为核心目标，并声明五条告警规则已经实现。实际代码中：

- `_regression_reasons()` 没有生产调用点。
- `regression_for()` 没有调用点，而且当前实现只比较官方统计。
- Run 详情只返回 `is_baseline` 和 `baseline_name`，没有返回回归分析结果。
- 前端没有调用 `/api/baselines` 或 `/api/runs/{id}/baseline`。
- UI 中没有设置、取消或查看本地基线的操作入口。

**影响**

用户即使直接调用后端 API 设置 Baseline，也只会改变一个展示字段，不会触发计划中描述的本地降智判定和告警。

**建议**

- 明确本地 Baseline 的匹配规则，例如 Agent、模型、Effort、任务集和关键运行参数必须一致。
- 在 Run 详情或 Dashboard API 中调用本地回归计算，并返回结构化告警。
- 增加设置/取消基线的 UI 控件及对应刷新逻辑。
- 添加端到端测试，证明设置基线后新 Run 能产生五条规则中的预期告警。

### CR-05 `[P2]` AI 对比分析绕过 Provider 并发和统一重试策略

**位置**

- [`backend/app/compare_analysis.py:159`](backend/app/compare_analysis.py#L159)
- [`backend/app/compare_analysis.py:171`](backend/app/compare_analysis.py#L171)
- [`backend/app/provider_proxy.py:452`](backend/app/provider_proxy.py#L452)

**问题**

AI 对比分析调用 `reserve_provider_request()` 预约 RPM 后，直接使用 `httpx.post()` 请求上游。它没有经过 `forward_provider_request()`，因此不受以下配置控制：

- `provider_max_concurrency`
- `provider_max_retries`
- `provider_retry_interval_seconds`
- Provider Proxy 的请求结果和活动连接遥测

遇到 429 或 5xx 时，分析接口会直接返回 502，而不是执行用户配置的请求级重试。

**影响**

- UI 声称 Provider 策略对下一次新请求生效，但 AI 分析是例外。
- 多个分析请求可能越过活动连接上限。
- 瞬时 Provider 故障会令分析失败，与 Agent 请求行为不一致。

**建议**

- 抽取 Provider 请求执行器，让代理转发和内部 AI 分析共享限流、并发、重试及遥测逻辑。
- 或让内部分析请求安全地经过本地 Provider Proxy。
- 增加测试验证 AI 分析会等待并发槽，并按配置重试 429/5xx。

### CR-06 `[P2]` 前端对比和 AI 分析可能展示过期请求结果

**位置**

- [`frontend/src.tsx:729`](frontend/src.tsx#L729)
- [`frontend/src.tsx:2172`](frontend/src.tsx#L2172)
- [`frontend/src.tsx:2183`](frontend/src.tsx#L2183)

**问题**

对比数据的 `useEffect()` 和 `analyzeSelection()` 都没有使用 `AbortController`、请求序号或响应范围校验。

当用户快速改变 Trial 选择或筛选条件时，较早发出的慢请求可能在新请求之后完成，并覆盖当前状态。AI 分析虽然在选择变化时清空旧结果，但正在执行的旧请求仍能在返回后重新写入结果。

如果用户在同一 Run、同一 Task 的不同 attempt 之间切换，客户端按 Run/Task 过滤无法识别返回数据属于旧 attempt，页面会显示错误的指标或 AI 结论。

**影响**

- UI 展示内容可能与当前勾选项不一致。
- 用户可能基于错误的 Trial 范围判断模型提升或退化。

**建议**

- 每次请求保存稳定的 selection key，响应写入前再次比较。
- 在 effect cleanup 中取消旧请求。
- AI 分析响应应带回服务器实际处理的 selection 列表，前端必须核对后再展示。
- 增加延迟反转测试：旧请求晚返回时不得覆盖新选择。

### CR-07 `[P2]` Docker 子网采用哈希直映，无法保证并发 Trial 不冲突

**位置**

- [`backend/app/pier_retry_patch/networking.py:71`](backend/app/pier_retry_patch/networking.py#L71)
- [`backend/app/pier_retry_patch/networking.py:82`](backend/app/pier_retry_patch/networking.py#L82)

**问题**

每个 Trial 通过 SHA-256 哈希对网络池取模，直接选择一对 `/29` 子网。代码没有检测 Docker 中已占用的子网，也没有冲突后的探测或重试。

默认 `10.240.0.0/12` 网络池可容纳 65,536 对 `/29` 网络。在最大 72 个并发 Trial 下，按均匀哈希计算，至少一次碰撞的概率约为：

```text
1 - P(65536, 72) / 65536^72 = 3.826%
```

Docker Bridge 网络不允许地址池重叠，因此碰撞会表现为随机环境创建失败。

**影响**

- 高并发运行可能出现难以复现的 `Pool overlaps` 基础设施错误。
- 清理延迟或残留网络会进一步提高实际碰撞概率。

**建议**

- 在 SQLite 中为活动 Trial 原子分配网络对，并在释放时归还。
- 或在创建网络前检查 Docker 已使用子网，发生冲突时使用确定性探测序列选择下一个空闲槽位。
- 添加高并发唯一性测试和人工构造哈希碰撞测试。

## 5. 自动化验证结果

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| 后端测试 | 通过 | `183 passed in 3.47s` |
| Python 编译 | 通过 | `python -m compileall -q backend/app` |
| 前端依赖安装 | 通过 | `npm ci`，审计输出 0 个已知漏洞 |
| TypeScript 与生产构建 | 通过 | `npm run build`，Vite 构建成功 |
| Python 依赖一致性 | 通过 | `python -m pip check` |
| 暂存 diff 范围 | 通过 | 暂存 diff 与 `3cda408..main` 哈希均为 `54fb45a5ead777f491073e333ab0fcf278d0175c` |
| 未暂存源码变更 | 通过 | 审查结束时不存在未暂存源码修改 |
| `git diff --cached --check` | 未通过 | 存在行尾空格和多余 EOF 空行，见下节 |

### 5.1 Diff 格式告警

`git diff --cached --check` 报告：

- `PROVENANCE.md`：新增 EOF 空行。
- `backend/requirements.txt`：新增 EOF 空行。
- `tasks/test-python-slugify-workflow/solution/solution.patch`：多处行尾空格。
- `tasks/test-python-summary-workflow/solution/solution.patch`：多处行尾空格。

Patch 文件中的空格可能是补丁正文的一部分，修复前需要确认不能改变参考 Patch 的语义。

### 5.2 工程卫生观察

暂存区包含 `.playwright-mcp/` 下的 PNG、控制台日志和超过一万行的页面快照。建议确认这些文件是否属于需要长期版本控制的测试证据；若不是，应移出提交并加入 `.gitignore`。

## 6. 尚未执行的真实测试

以下测试在本轮审查中没有执行，因此不能声称已经通过：

- 浏览器 Playwright UI 交互测试。
- 桌面和移动视口截图检查。
- 浏览器控制台错误、网络请求和 SSE 重连验证。
- 启动真实 FastAPI 服务后的完整 UI 工作流。
- Docker/Pier 真实端到端 Trial。
- 真实 verifier、Patch 提取和 Reward 产出。
- 真实 Provider 或付费模型调用。
- 多 Run、多 Agent、72 Trial 的并发压力测试。
- 服务重启、进程孤儿收割和中断恢复测试。
- Provider 429/5xx、长流中断和连接并发实测。

本轮“真实复现”仅指使用真实应用代码和临时 SQLite 数据库触发缺陷，不包括外部 Docker、Pier、浏览器或付费 Provider。

## 7. 推荐修复顺序

1. 修复 CR-01：建立受信任 Jobs 根目录白名单，阻断仓库外删除。
2. 修复 CR-02：统一状态迁移并保证终态不可逆。
3. 修复 CR-03：为恢复格式建立严格 Schema，并提供坏数据防御读取。
4. 为前三项增加确定性回归测试，再运行全部后端测试。
5. 修复 CR-04：完成本地 Baseline 和回归告警调用链。
6. 修复 CR-05：统一 Provider 请求执行路径。
7. 修复 CR-06：取消或隔离过期前端请求。
8. 修复 CR-07：实现无碰撞网络分配。
9. 启动本地服务，使用 Playwright 执行关键 UI 工作流和响应式截图验证。
10. 在明确费用和凭据授权后，再进行 Docker/Pier/真实 Provider 端到端验证。

## 8. 修复验收标准

### 8.1 P1 验收

- 恶意备份不能让任何删除目标离开允许的 Jobs 根目录。
- 恢复非法设置或 Run 数据时返回可理解的 4xx，不写入部分坏数据。
- 数据库已有坏行时，列表接口应跳过或结构化报告，不能全局 500。
- 创建后立即取消、重试后立即取消均保持 `cancelled`，后台不能写回活动状态。
- 取消竞态测试重复运行至少 100 次无失败。

### 8.2 P2 验收

- 前端可以设置和取消本地 Baseline，新 Run 能返回并展示回归告警。
- AI 分析受 RPM、活动连接并发和请求重试配置控制。
- 快速切换对比选择时，旧响应不能覆盖新状态。
- 最大并发范围内不会分配重叠 Docker 子网；构造碰撞时能够自动选择其他子网。

### 8.3 UI 验收

- Playwright 覆盖创建 Run、立即取消、结果查看、Trial 重试、对比筛选、AI 分析、设置保存、备份恢复和删除流程。
- 桌面与移动视口无内容重叠、截断或不可操作控件。
- 页面加载、SSE 断线重连和错误提示均有明确状态，不永久停留在 Loading。
- 浏览器控制台无未处理 Promise rejection 或 React key/state 警告。

## 9. Git 状态说明

- `main` 仍指向 `2f0d74f`。
- 当前分支 `review/after-3cda408` 的 `HEAD` 指向基准 Commit `3cda408`。
- `3cda408` 之后的原始代码保持在暂存区，便于整体审查或后续拆分修复。
- 本报告创建时未加入暂存区，以避免改变已经核验过的原始审查 diff。
