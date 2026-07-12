# DeepSWE Regression UI 开发验证进度

更新时间：2026-07-11（第二轮：审查修复与 Docker 资源清理集成）

## 1. 当前结论

第一版 Web UI 的核心验收链路已经完成：可以配置并启动单个 Agent 或同时启动三种 Agent，查看实时 Trial 进度、日志和 Patch，管理和批量删除历史结果，比较运行，设置本地基线并显示回归告警，还可管理设置及导出、备份和恢复数据。

在第一轮交付后进行了一次全面代码审查（见 `review.md`），确认了 15 项主要缺陷和一批次要问题，集中在测量记录失真、付费资源泄漏和对 Pier 产物形态的错误假设三条线上。**本轮已将 15 项主要缺陷全部修复**，并按 `DOCKER-RESOURCE-CLEANUP-INTEGRATION-PLAN.md` 完成了 Docker 资源清理的阶段一至三集成。后端 37 项测试、前端类型检查与构建、本机 Docker 集成测试（方案 §9.3）全部通过。

当前完成度约为 **95%**。剩余工作主要是需要真实付费调用的 Claude Code 最终 verifier 重跑、reasoning effort 有效值的运行后观测机制、DeepSWE 官方统计同步，以及若干结构性重构（AgentAdapter 接口收拢、序列化合并）；这些不影响当前 UI 的本地使用与测量正确性。

| 模块 | 状态 | 说明 |
|---|---|---|
| FastAPI 后端 | 已完成第一版 | 健康检查、配置、诊断、运行、实时事件、结果、比较、基线、任务、导出恢复与 Docker 清理 API |
| React/Vite 前端 | 已完成第一版 | Dashboard、New Run、Live、Results、Compare、Tasks、Settings（含 Docker 存储卡片）、Diagnostics |
| SQLite | 已完成兼容迁移 | 保留旧数据，启动时增量补列（本轮新增 `jobs_dir`、`pier_version`） |
| 凭据安全 | 已完成 | BOM、ACL、脱敏、临时文件清理；本轮起 `redact()` 注入真实 Token 做精确脱敏，不再仅靠正则兜底 |
| Agent 执行链 | 已接通 | 三种 Agent 均由 Pier 在容器内执行；本轮修复 Codex reasoning effort 未透传的缺陷（此前一律以 pier 默认 high 执行） |
| Trial 实时状态 | 已完成 | SSE 按 mtime 指纹推送（含回归告警字段），断线自动重连；多 Agent 可切换，取消后立即同步 |
| Trial 详情 | 已完成 | Reward、Partial、F2P/P2P、耗时、Token、费用、steps、失败原因、Patch 和日志；Patch 改为点击时按需拉取 |
| 结果比较 | 已完成 | 最多比较 8 次运行，展示运行摘要和 Task × Run 通过率矩阵 |
| 基线与回归告警 | 已完成 | 方案 §18 五条规则全覆盖；一次异常黄色、相同配置连续两次异常升级红色 |
| Tasks | 已完成 | 解析 113 个任务的 `task.toml`；标准 7 题展示 T01–T07、标题、目录名和数据集 ext_id |
| 结果管理 | 已完成 | 多选、全选、批量删除；运行中强制先取消；删除前先做 Docker 定向清理，再清索引、基线、artifacts 与日志 |
| Docker 资源清理 | 已完成阶段一至三 | 取消/运行结束/删除三个生命周期点定向清理容器、网络与 Trial 镜像；存储可视化、预览确认、按保留期的缓存维护 |
| 进程生命周期 | 已完成 | 服务启动时收割上次残留的 pier 进程与容器（校验命令行防 PID 复用误杀），服务退出时终止子进程 |
| Settings | 已完成 | 凭据路径、Jobs 目录、默认配置，及 4 项 Docker 清理设置 |
| 导出/备份/恢复 | 已完成 | CSV（UTF-8 BOM + 公式转义）、无凭据 JSON 备份；恢复对 job_name/status/settings 逐行验证 |
| 一键启动 | 已完成 | 每步检查退出码，健康检查通过后再打开浏览器，源码未变时跳过前端构建 |

## 2. 第一版交付能力（第一轮）

### New Run

- 七个默认任务可以单独勾选、全选或清空。
- 标准任务显示固定套件 ID（T01–T07）、真实标题、本地目录名和数据集 ext_id。
- 支持"单 Agent"和"三 Agent 同时跑"；三 Agent 模式创建三条独立 Run，并显示总 worker 上限。
- 支持每题重复次数、并发数、Agent timeout、Verifier timeout。
- 支持基础设施错误自动重试、启用/禁用 verifier。
- 支持 Standard、Batch 和 Priority 费用层级。
- 记录 reasoning effort 请求值和 adapter 映射值；有效值在取得运行后观测机制前存 `null`，不再复制请求值伪装成观测结果。
- 创建后自动进入实时进度页。

### Live Progress

- 使用 SSE 推送变化，不需要整页刷新。
- 展示 queued、preflight、environment、agent、patch、verifier、completed/failed 等阶段。
- 展示每个 Trial 的状态、Reward 和耗时。
- 点击 Trial 可直接在实时页展开 Patch、失败原因和日志。
- 三 Agent 同跑时可在仍运行的 Agent 间切换。
- 取消后立即重新拉取运行列表并自动切换到其他活动 Run。
- 支持取消 Pier 进程树，保留已生成的日志和部分结果，并定向清理当前 Job 的 Docker 容器、网络和 Trial 镜像。

### Results

- 从 Pier Job 汇总和 Trial 独立 `result.json` 解析真实数据。
- 区分总输入、缓存输入、未缓存输入和输出 Token。
- 区分 Pier 报告费用与按官方价格计算的估算费用。
- 展示 Agent steps、失败类型、脱敏失败信息、Patch 和日志。
- 兼容本次功能上线前已经存在的历史 Job。
- 支持 Job 勾选、全选和批量删除，删除后汇总展示 Docker 镜像清理结果。

### Diagnostics

- 不再把 Windows 本地 Codex/Claude CLI 版本误认为容器运行版本。
- 容器 Agent 未完成运行前显示安装策略 `latest`；完成后从 Trial `agent_info.version` 展示实际版本。
- `local-proxy` 已改名为 `model-api`，显示凭据文件中配置的真实 URL；HTTP 401 明确标注为"探测请求未携带 Token"。
- 新增 4 项 Docker 存储检查：镜像占用、构建缓存（超阈值告警）、孤儿 Trial 镜像、自动清理策略状态。

### Compare 与 Baseline

- 最多选择 8 次历史运行，展示通过数、Reward、费用和 Task 通过率矩阵。
- 已完成运行可设为本地基线。
- 回归规则全量覆盖方案 §18：总通过率显著下降、至少两个任务坍塌、成功 Trial 耗时增加超 25%、耗时骤降且通过率下降（疑似提前终止）、控制任务 actionlint 降至 1/4 或更低。
- 一次异常标记黄色；相同配置连续两次异常升级红色。

### 数据管理

- 测试使用独立临时 SQLite 数据库和隔离的凭据路径，不触碰开发数据库与真实凭据。
- JSON 备份恢复按 `job_name` 合并，主键冲突时重新映射基线关系；恢复前逐行验证。
- JSON 备份不包含 Token 或完整凭据内容。

## 3. 第二轮：审查修复与 Docker 资源清理集成

### 测量记录正确性（review.md 发现 1、4、5、8）

- Codex 分支显式透传 `--agent-kwarg reasoning_effort=<v>`，临时 TOML 同时写入 `model` 与 `model_reasoning_effort`（已对照 pier 0.3.0 codex adapter 源码确认参数名）。此前所有 codex 运行实际均以 pier 默认 `high` 执行。
- `reasoning_effort_effective` 不再在创建时复制用户所选值，改存 `null`。
- 禁用 Verifier 的运行 reward/passed 记 `null`，"未测量"与"全部失败"在数据上可区分；兼容 pier 聚合器对单键 rewards 的 `mean` 改名。
- Pier 进程非零退出码强制标记 `failed` 并记录退出码，不再依据残缺 result.json 伪装 completed。
- Trial 目录名 32 字符截断归一回全名，Live 视图不再出现幽灵占位行，compare 不再出现假任务行。
- 每次运行记录 Pier 版本与创建时的 Jobs 目录（历史运行不因改设置失联）。
- timeout 换算从硬编码 5400/1800 改为按所选任务 `task.toml` 声明值动态计算。

### 付费资源泄漏（发现 2、10、12）

- Docker 定向清理的名称匹配复刻 pier 的 Compose project 规整规则（小写化等），修复原实现约 98% 失效的大小写不匹配。
- 服务启动时收割上次会话残留：按 PID 终止 pier 进程树（psutil 校验命令行含 pier，防 PID 复用误杀）、清理容器与网络、标记 interrupted。
- 服务退出时终止仍在运行的 pier 子进程。
- preflight 阶段执行真实检查（Docker 可用性、任务目录、凭据），不再只是状态标签。
- 前端取消校验后端响应，`cancelled:false` 时如实提示，不再谎报"清理已触发"。

### 端点健壮性与竞态（发现 3、6、7、14）

- `verifier_result: null`、`exception_message: null` 等 Pier 真实产物形态不再导致四类端点 500。
- 取消与结果落库共用状态锁，终态不可被反向覆盖（取消不误报失败、完成不被改写为取消）。
- SSE 推送附带 `regression` 字段，回归告警横幅不再闪现后消失。
- 前端 EventSource 断线自动重连，服务器拒绝时延迟重建并补拉详情；快速切换运行时丢弃过期响应。

### 输入安全（发现 9、11、13）

- `/api/restore` 逐行验证：job_name 白名单（拒绝 `..`、绝对路径、非法字符）、status 归一为终态（不再产生不可取消的僵尸行）、settings 白名单键 + JSON 校验（不再可毒化偏好导致全站 500）、时间戳解析守卫。
- 删除路径统一经 `resolve()` 逃逸检查，`rmtree` 只允许作用于 jobs 根目录内的直接子目录。
- `tasks: []` 在 API 层拒绝（否则 pier 失去 `-i` 过滤会跑满全部 113 题）。
- 四个日志/错误出口的 `redact()` 全部传入当前凭据 Token。

### 效率（发现 15）

- SSE 用 result.json mtime 指纹检测变化（5 秒兜底刷新），不再每秒全量解析全部 Trial 与 Patch。
- 列表、比较、SSE 路径均剔除 Patch 正文；新增 `GET /api/runs/{id}/trials/{trial_id}` 供点击时按需拉取。
- 回归计算复用已解析的运行详情，单次详情请求不再重复解析 5 遍。

### Docker 资源清理集成（阶段一至三）

- 新增 `backend/app/docker_cleanup.py`：候选镜像仅从 Job artifacts 的 Trial 目录推导（前缀 + `-main`/`-pier-egress-proxy`/verifier 变体白名单后缀），永不接受前端传入的镜像名；镜像删除不使用 `--force`；builder prune 经 stdin 确认，命令行无 `-f`；全局清理锁防三 Agent 并行互扰；资源不存在视为成功（幂等）；结果写入 `<job>.docker-cleanup.json` 审计。
- 三个生命周期接入点：取消运行后、运行结束后（`docker_cleanup_after_run` 控制）、删除历史 Run 时（先清理后删 artifacts，响应携带清理摘要）。
- 新增 API：`GET /api/docker/storage`、`POST /api/docker/cleanup/preview`、`POST /api/docker/cleanup`（scope：job / orphaned / expired / build_cache；后端按 scope 重新计算目标；存在活动 Run 时批量清理返回 409）。
- Settings 新增"Docker 存储与清理"卡片：实际总占用、可回收空间、本工具 Trial 镜像数、构建缓存与保留期；扫描→预览→二次确认→清理；"预计释放"只采用 `docker system df -v` 的独占空间数据，不累加逻辑 Size。
- 新增 4 项设置：`docker_cleanup_after_run`（默认开）、`docker_cleanup_on_delete`（默认开）、`docker_cache_retention_hours`（默认 168）、`docker_cache_warning_gb`（默认 20）。
- 基础镜像保护：任务声明的 ECR 镜像、`ubuntu:24.04` 等公共镜像和 BuildKit 缓存不进入自动删除候选；正常清理命令不含 `--force`、`docker system prune -a` 或无范围 `docker image prune -a`。

### 工程卫生

- 测试隔离补齐凭据路径（不再读取开发者真实凭据），会话结束 dispose 引擎释放 SQLite 文件锁。
- 前端依赖从 `latest` 固定为确定版本，构建工具移入 devDependencies；`tsc --noEmit` 不再在源码旁生成产物。
- `index.html` 补 DOCTYPE/lang/title，脱离 quirks mode。
- CSV 导出带 UTF-8 BOM（中文 Excel 兼容）并做公式注入转义。
- `run-ui.ps1`：每步检查原生命令退出码、健康检查通过后再开浏览器、前端源码未变化时跳过构建。
- `codex-local-provider.toml` 恢复为 `host.docker.internal`（仅供手册命令使用），删除运行时从不读取的 `codex_config_file` 死配置。

## 4. 真实付费验证记录

> 注：以下 Codex 记录在 effort 透传修复之前产生。当时请求值与 pier 默认值均为 `high`，实际执行档位一致，数据仍然有效；但此前若选择其他档位则不会生效，修复后的运行不受此限制。

### Codex

- Codex `0.144.1`
- 模型：`gpt-5.6-sol`
- Reasoning effort：`high`
- Reward：`1.0`
- F2P：`55/55`
- P2P：`145/145`
- 输入 Token：`2,074,213`
- 缓存 Token：`1,915,904`
- 输出 Token：`14,827`
- 费用：`$2.194307`

### mini-swe-agent

- 模型映射：`openai/gpt-5.6-sol`
- Reasoning effort：`high`
- Reward：`1.0`
- F2P：`55/55`
- P2P：`145/145`
- 输入 Token：`2,058,320`
- 缓存 Token：`1,491,456`
- 输出 Token：`26,871`
- 费用：`$4.386178`

### Claude Code

已确认 Anthropic Messages API、`gpt-5.6-sol`、认证、URL 映射、原生 effort、`thinking=disabled` 和 `max_turns=80` 配置可以进入真实执行链。

尚未完成一次新的、受限且最终 verifier Reward 为 1.0 的正式重跑。该操作会产生额外模型费用，留待明确授权后执行。

## 5. 自动化与验证（第二轮）

- 后端测试：`37 passed`（新增 10 项 docker_cleanup 单元测试、6 项 runner 语义测试、5 项 API 安全测试；全部 subprocess mock，不依赖本机 Docker）
- 前端 `tsc --noEmit`：通过
- Vite 生产构建：通过
- 本机 Docker 集成测试（清理方案 §9.3，alpine 测试标签）：5 项全部通过 —— preview 只列测试标签、定向删除、`alpine:3.20` 完好、未执行任何全局 prune、二次清理幂等零错误
- 孤儿识别在本机真实镜像上验证：36 个 Trial 残留镜像全部命中；`ubuntu:24.04`、ECR 基础镜像、`alpine`、无 uuid 段镜像全部正确排除
- uvicorn 冒烟：health / bootstrap / docker storage / 运行详情（含 regression 与 `cache_write_tokens`、`pier_version` 字段）/ SSE 首条消息（含 regression、不含 Patch 正文）/ restore 验证 / 删除（含 docker_cleanup 摘要）全部通过
- pier 0.3.0 源码核对：codex adapter `reasoning_effort` CliFlag 参数名、Compose project 名称规整规则、Trial 目录 32 字符截断规则

## 6. 尚未完成或可继续增强

1. Claude Code 需要一次明确授权的付费正式重跑，以取得最终 verifier Reward。
2. reasoning effort 有效值（`reasoning_effort_effective`）的运行后观测机制：需从 agent 日志或响应中解析实际生效档位，当前如实存 `null`。
3. 可复现性记录中的 DeepSWE Git commit、Task digest、Docker image digest 尚未落库（Pier 版本、Jobs 目录已在本轮补齐）。
4. Diagnostics 的容器内 `host.docker.internal` 探测、Responses/Anthropic 双协议探测、六档 effort 映射验证仍属后续（当前预检覆盖 Docker、凭据、模型 API、磁盘内存、Pier 补丁与 Docker 存储）。
5. AgentAdapter 统一接口尚未落地（适配逻辑仍内联在 runner 中）；`serialize()` 与 `run_detail()` 两套序列化可进一步合并。
6. 当前使用应用内兼容迁移；后续可引入 Alembic 管理长期 schema 版本。
7. 尚未自动同步 DeepSWE 官方榜单和官方 task statistics。
8. JSON 备份不打包完整 Job artifacts；Jobs 目录仍应单独备份。
9. 模型 API URL 通过两行凭据文件配置；后续可在 Settings 中增加安全的 URL 编辑与连通性验证。
10. Docker 清理阶段四（`docker-resources.json` 资源清单、按清单恢复清理无 artifacts 的历史孤儿）留待后续。

## 7. 运行方式

```powershell
.\run-ui.ps1
```

访问：

```text
http://127.0.0.1:8765
```
