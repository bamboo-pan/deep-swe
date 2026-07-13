# DeepSWE 工具开发经验总结

更新时间:2026-07-12。本文提炼自原 `CODEX-PIER-WINDOWS-NOTES.md`、`review.md`(2026-07-11 代码审查,15 项 CONFIRMED 缺陷)、`审查报告.md`(2026-07-12 试运行事故复盘)。所有缺陷均已修复,此处保留根因与法则。项目现状见 `PROJECT-PLAN.md`。

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
- pier 把 trial 目录名截断到 32 字符(`task_name[:32].rstrip('_-')`),默认 7 题有 4 题超长 → 用全名对目录名做匹配产生幽灵 trial;匹配前同样归一
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
