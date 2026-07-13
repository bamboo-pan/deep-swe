# DeepSWE 模型降智测试工具 — 项目计划

更新时间:2026-07-12。本文合并原 `DEEPSWE-REGRESSION-UI-PLAN.md`、`DEEPSWE-REGRESSION-UI-PROGRESS.md`、`DOCKER-RESOURCE-CLEANUP-INTEGRATION-PLAN.md`,内容已对照当前代码校准。经验教训见 `LESSONS-LEARNED.md`。

## 1. 目标

本地 Windows Web UI,用于配置、执行、比较 DeepSWE 测试,建立本地基线并检测模型降智:

- 支持三种 Agent:`mini-swe-agent`、Codex、Claude Code(均由 Pier adapter 在 Docker 任务容器内安装执行,Windows 本地 CLI 版本不代表运行版本)
- 模型与六档 reasoning effort(`none/low/medium/high/xhigh/max`)可配,默认 `gpt-5.6-sol / high`
- 实时查看进度、日志、Token、费用;跨 Agent/模型/effort/日期比较;对照 DeepSWE 官方数据
- 第一版仅手动触发,无定时调度

## 2. 架构与技术栈

```text
浏览器 UI → FastAPI(SQLite:settings/runs/baselines)
              ├─ docker_cleanup(定向清理)
              ├─ pier_retry_patch(经 PYTHONPATH 注入 pier 子进程)
              └─ Pier 子进程 → 三种 Agent adapter → DeepSWE Docker 容器 → 本地模型入口
```

- 后端:Python 3.12 / FastAPI / Pydantic / SQLAlchemy / SQLite / SSE / psutil
- 前端:React / TypeScript / Vite / 原生 CSS(比较视图为 CSS Grid 热力矩阵,未引入图表库)
- 启动:`.\run-ui.ps1` → `http://127.0.0.1:8765`(仅监听本机;逐步检查退出码,健康检查通过后开浏览器,源码未变跳过前端构建)
- 本机环境:Pier 0.3.0、Docker CLI 29.6.1、Python 3.12.10、Node 24.18.0、12 逻辑 CPU、31.5 GB 内存、数据集 113 任务

## 3. 默认运行配置

| 项 | 值 |
|---|---|
| 凭据文件 | `C:\Users\bamboo2026\Documents\github\codex1.txt`(两行:URL、Token) |
| agent / model / effort | mini-swe-agent / gpt-5.6-sol / high |
| 任务集 / 重复 / 并发 | regression-standard-7 / 1 / 2 |
| verifier / tier | 开启 / standard |
| 运行预算 | `trial_budget_usd` 单 Trial 默认 $8;`run_budget_usd` Run 级兜底默认 $10(均 0 = 禁用) |

并发限制按实际总并行 Trial 数计算:`Agent 数 × min(每 Agent 并发数,任务数 × 每题次数)`。每 Agent 并发输入最大 72;总并行 1–12 正常,13–18 黄色资源峰值警告,19–72 红色警告 + 前后端确认(`confirm_high_concurrency`),>72 禁止。任务声明的 2 CPU / 8 GB 是容器上限而非固定占用,不再直接相乘作为阈值依据。

## 4. 凭据与安全

- 两行凭据文件:UTF-8 读取、去 BOM、去首尾空格;URL 必须 HTTP(S)
- Token 不进 SQLite、不进命令行、不进日志与前端响应;每次运行生成临时认证文件(严格 Windows ACL,结束后删除);`redact()` 注入当前真实 Token 做精确脱敏,正则仅兜底
- 模型地址完全由凭据第一行决定;宿主机原样使用,容器内自动把 host 映射为 `host.docker.internal`
- UI 只展示脱敏信息(URL、文件名、Token 尾 4 位、验证状态)

## 5. Agent 适配

统一 `AgentAdapter` 接口是后续重构方向;当前适配逻辑内联在 `runner.py` 分支中,新增 Agent 需同步修改 schemas、runner、results、diagnostics。

- **mini-swe-agent**:OpenAI 兼容接口;经 `config_file` 注入 `agent.step_limit`(取 Run 的 `agent_max_steps`);`trial_budget_usd` > 0 时另传 `cost_limit`(litellm 对自建网关算不出成本时恒 0 不触发,由守护线程按 token 估算兜底)
- **Codex**:每次运行生成临时 provider TOML(含 `model`、`model_reasoning_effort`、`supports_websockets = false`)与临时 `auth.json`,经 `CODEX_AUTH_JSON_PATH` / `config_toml_file` 注入;reasoning effort 必须同时显式传 `--agent-kwarg reasoning_effort=<v>` —— pier 该参数默认 `high`,缺省会静默覆盖用户所选档位;无任何原生费用/步数限额,完全依赖守护线程逐 Trial 兜底
- **Claude Code**:Anthropic Messages 兼容接口;`thinking=disabled`、`max_turns`(取 Run 的 `agent_max_steps`);`trial_budget_usd` > 0 时传 `max_budget_usd`;`disallowed_tools=EnterPlanMode,Task,Agent` 禁用容器内 subagent(基准需要单 agent 可比口径,也是防用量失控的关键)

effort 三值记录:请求值 / adapter 映射值 / 有效值(`reasoning_effort_effective` 只能来自运行后观测,观测机制未落地前如实存 `null`,不复制请求值伪装)。

## 6. 默认任务集 regression-standard-7

| ID | 任务 | 角色 |
|---|---|---|
| T01 | dasel-html-document-format | 区分 |
| T02 | igel-persist-feature-schema | 区分 |
| T03 | csstree-shorthand-expansion-compression | 区分 |
| T04 | happy-dom-deterministic-intersectionobserver | 区分 |
| T05 | superjson-error-stack-serialization | 区分 |
| T06 | dateutil-rfc5545-timezone-interop | 区分 |
| T07 | actionlint-action-pinning-lint | 控制 |

UI 同时展示套件 ID、真实标题、目录名、数据集 ext_id。快捷模式:快速检查(每题 1 次,7 trials,并发 2 约 30–45 分钟)/ 建立基线(每题 4 次,28 trials,约 2–3 小时)。

## 7. 成本计算

GPT-5.6-SOL 官方价(每 1M Token):

| Tier | 输入 | 缓存读 | 缓存写 | 输出 |
|---|---:|---:|---:|---:|
| Standard | $5.00 | $0.50 | $6.25 | $30.00 |
| Batch/Flex | $2.50 | $0.25 | $3.125 | $15.00 |
| Priority | $10.00 | $1.00 | $12.50 | $60.00 |

按运行创建时所选 tier 计算。Pier stats 不区分 cache-write,估算暂不含该项,`cache_write_tokens` 存 `null`。UI 区分 API 报告费用与按 Token 的估算费用。价格来源:https://developers.openai.com/api/docs/pricing

## 8. 结果与数据

- 三 Agent 结果统一结构(agent/版本/模型/effort 三值/passed/reward/partial/f2p/p2p/双耗时/四类 Token/双费用/steps);不支持或未返回的字段存 `null`,不推测
- 禁用 verifier 的运行 reward/passed 存 `null`("未测量"与"全部失败"必须可区分);Pier 非零退出码强制 `failed` 并记录退出码
- SQLite 仅三表:`settings`、`runs`(含 `jobs_dir`、`pier_version`)、`baselines`;Trial 指标直接从 Job artifacts 解析,不重复入库
- 可复现性字段已落库:Pier 版本、Jobs 目录、模型、effort 请求/映射值、tier、timeout、并发、重复次数;待补:DeepSWE Git commit、Task digest、Docker image digest、按 Run 的 Provider URL 与凭据指纹

## 9. 基线与降智判定

基线建议 28 trials(每题 4 次)。五条告警规则(全部已实现):

1. 总通过率相对基线下降 ≥ 4/28(约 14 个百分点)
2. 至少两个任务从 3–4/4 降为 0–1/4
3. 成功 Trial 归一化耗时增加 > 25%
4. 耗时下降 > 35% 且通过率同时下降(疑似提前终止)
5. 控制任务 actionlint 通过率降到 ≤ 1/4

一次异常黄色(warning);相同配置(agent+模型+effort)连续两次异常升级红色(danger)。

## 10. 用量护栏与故障韧性

- **preflight(阻断式)**:Docker 可用、任务目录存在、凭据可读且格式正确、所选任务 `*.sh` 无 CRLF(CRLF 会使 verifier 必挂、agent 费用全废)
- **运行中熔断**(`_wait_with_guard`,每 20 秒):单 Trial 实时费用/步数超限只掐该 Trial 的容器(读进行中 `agent/trajectory.json` 的 ATIF `final_metrics`,写 `guard.json` 标记,其余任务继续;mini/claude 有原生限额,兜底留 1.5×/+30 步裕量,codex 无原生限额按限额即触发);累计费用(含进行中)达 `run_budget_usd` 终止整个 Run;失控硬线(不可关):单 Trial agent 日志 ≥ 30MB 或 subagent 会话 ≥ 60 个;Run 级触发走取消+清理链路,Run 记 `cancelled` 并写明原因
- **pier_retry_patch**(`backend/app/pier_retry_patch/`,经 PYTHONPATH sitecustomize 注入 pier 进程):
  - 瞬时故障分类:503/429/连接类失败重分类为 `TransientAgentInfrastructureError` 触发 pier 重试,模型/代码失败不重试;退避延迟经 `DEEPSWE_PIER_RETRY_DELAYS` 配置
  - Docker 子网固定:trial 网络按路径哈希从 `10.240.0.0/12` 池分配 /29 对(`DEEPSWE_DOCKER_NETWORK_POOL` 可改),避开 Docker 的 192.168.* fallback 池与局域网冲突
  - Run 相关字段:`retry_infrastructure_errors`、`infrastructure_max_retries`、`agent_max_steps`(原 `claude_max_turns`,现对全部 agent 生效)
- **进程生命周期**:启动时收割上次残留非终态运行(按 PID 终止 pier 进程树,psutil 校验命令行防 PID 复用误杀;Docker 定向清理;标记 `interrupted`);退出时终止仍在运行的 pier 子进程;取消/落库共用状态锁,终态不可反向覆盖

## 11. Docker 资源清理(阶段一至三已实现)

每个 Trial 经 Pier 产生随机后缀 Compose 项目,镜像条目持续累积(共享层被复用,单条独占仅 KB 级,主要问题是条目污染与 BuildKit 缓存增长,不能把列表 Size 相加)。

- **候选识别**:只从 Job artifacts 的 Trial 目录名推导,按 pier 规整规则(小写化、首字符补 `0`、非法字符替换)归一后做前缀 + 服务后缀白名单(`-main` / `-pier-egress-proxy` / verifier 变体)匹配;永不接受前端传入镜像名,永不用全局正则
- **三个接入点**:取消运行后、Pier 退出后(`docker_cleanup_after_run`)、删除历史 Run 时(清理先于 artifacts 删除,响应携带摘要)
- **安全边界**:镜像删除无 `--force`;被引用镜像跳过并报告;任务基础镜像与 `ubuntu:24.04` 等公共镜像不进候选;不执行 `docker system prune -a` 或无范围 `docker image prune -a`;全局清理锁防三 Agent 互扰;清理失败不改变评测结果状态;幂等;审计写 `<job>.docker-cleanup.json`
- **API**:`GET /api/docker/storage`、`POST /api/docker/cleanup/preview`、`POST /api/docker/cleanup`(scope:job/orphaned/expired/build_cache;后端按 scope 重算候选;有活动 Run 时批量与缓存清理返回 409)
- **BuildKit 缓存**:不随运行清理(直接影响下轮启动速度);Settings 按保留期(`docker_cache_retention_hours` 默认 168h)手动清理,先预览再二次确认,`builder prune` 经 stdin 确认非 `-f`;超 `docker_cache_warning_gb`(默认 20)时 Diagnostics 告警
- **设置项**:`docker_cleanup_after_run`(默认开)、`docker_cleanup_on_delete`(默认开)、上述两项缓存参数
- **待做(阶段四)**:每 Job 写 `docker-resources.json` 资源清单;artifacts 已被手动删除时按清单精确清理孤儿

## 12. UI 页面

- **Dashboard**:Docker/Pier/模型 API 健康、当前 Job、最近结果与基线、通过率/耗时/费用变化、降智告警摘要
- **New Run**:agent(单个或三 Agent 同跑,三 Agent 创建三条独立 Run)、模型、effort、任务选择(展示官方通过率/耗时)、重复次数、并发、双 timeout、基础设施重试、verifier 开关、tier
- **Live**:SSE 按 `result.json` mtime 指纹推送(5 秒兜底;含 regression 字段、不含 Patch 正文),断线自动重连;阶段流转 queued→preflight→environment→agent→patch→verifier→终态;点击 Trial 按需拉取 Patch/错误/日志;多 Agent 切换;取消
- **Results**:通过率、Reward、Partial、F2P/P2P、双耗时、Token/费用、steps、失败原因、Patch/日志;多选批量删除(运行中必须先取消;删除先清 Docker 再清索引/基线/artifacts/日志)
- **Compare**:最多 8 次运行,Task × Run 通过率矩阵;口径区分——官方严格可比仅 mini-swe-agent ↔ 官方 mini-swe-agent,Codex/Claude Code 对官方为参考比较,严格纵向比较要求同 agent/版本/模型/配置
- **Tasks**:113 任务的 task.toml 解析;官方通过率与平均耗时已实现(聚合自官方 `artifacts/v1.1/trials.json`,口径同官方站点 "ALL MODEL EFFORTS",缓存分发于 `data/official-task-stats-v1.1.json`,可手动同步刷新)
- **Settings**:凭据路径、Jobs 目录、默认配置、运行预算、Docker 存储与清理卡片、价格表/tier
- **Diagnostics**:Docker daemon、模型 API 可达性(不携带 Token)、Pier 版本与补丁(`pier-secret-env`、`pier-claude-utf8`,特征检测防升级误报)、容器 Agent 实际版本(Trial 完成后取 `agent_info.version`)、磁盘内存、Docker 存储四项(镜像/缓存/孤儿/清理策略)

## 13. 当前状态

第一版验收链路全部完成,约 95%+。经历三轮:第一轮交付 → 第二轮修复代码审查确认的 15 项缺陷 + Docker 清理集成 → 第三轮(2026-07-12)修复试运行暴露的护栏缺失与网络问题(CRLF 预检、subagent 禁用、用量熔断、瞬时重试、子网固定、官方统计)。

验证现状:后端 pytest 66 项、前端 `tsc --noEmit` 与 Vite 构建、本机 Docker 清理集成测试(alpine 标签五项)、pier 0.3.0 源码核对均通过。

真实付费验证:

| Agent | Reward | 输入/缓存/输出 Token | 费用 |
|---|---|---|---|
| Codex 0.144.1 | 1.0(F2P 55/55,P2P 145/145) | 2,074,213 / 1,915,904 / 14,827 | $2.194 |
| mini-swe-agent | 1.0(同上) | 2,058,320 / 1,491,456 / 26,871 | $4.386 |
| Claude Code | 执行链已全部打通,最终 verifier 重跑待授权(产生真实费用) | — | — |

## 14. 剩余工作

1. Claude Code 一次授权付费正式重跑(取得最终 verifier Reward)
2. `reasoning_effort_effective` 运行后观测机制(从 agent 日志/响应解析实际档位)
3. 可复现性待补字段落库:DeepSWE Git commit、Task digest、Docker image digest、按 Run 的 Provider URL/凭据指纹
4. Diagnostics 增强:容器内 `host.docker.internal` 探测、Responses/Anthropic 双协议探测、六档 effort 映射验证
5. AgentAdapter 接口收拢;`serialize()` 与 `run_detail()` 序列化合并
6. Alembic 迁移(当前为应用内增量补列)
7. Docker 清理阶段四(资源清单与孤儿恢复)
8. JSON 备份不含 Job artifacts,Jobs 目录需单独备份
9. Settings 内安全的模型 URL 编辑与连通性验证
10. 运维约定(代码外):网关侧每日配额与消费告警;新配置先 low/medium 单题冒烟(含 verifier 出 reward)再上 xhigh/全量

## 15. 验收标准(第一版)

三 Agent 可选、六档 effort、两行凭据安全读取、默认 7 题或任选、并发默认 2 可改、实时 Trial 状态/日志、可取消、通过率/F2P/P2P/耗时/Token/费用可见、多运行比较、基线+回归告警、官方价格计费、Token 与凭据零泄漏(数据库/日志/前端)。
