# DeepSWE Regression UI 代码审查报告

审查日期：2026-07-11
审查范围：本次未提交改动 —— `backend/`（FastAPI 后端，约 850 行）、`frontend/`（单文件 React 前端 src.tsx 1365 行）、`run-ui.ps1`、`.gitattributes`、`.gitignore` 与 `codex-local-provider.toml` 修改；对照文档 `DEEPSWE-REGRESSION-UI-PLAN.md` 与 `DEEPSWE-REGRESSION-UI-PROGRESS.md`。

审查方法：通读全部新代码与两份文档后，并行运行 10 个独立审查角度（逐行扫描、文档承诺对照、前后端契约、语言陷阱、pier 接口核对、复用、简化、效率、altitude、约定），随后对关键候选做对抗性验证：直接阅读本机安装的 pier 0.3.0 源码（`%APPDATA%\uv\tools\datacurve-pier\Lib\site-packages\pier\`）、在临时沙盒复现 pathlib 路径逃逸、实测 Starlette 响应头与开发数据库内容，最后进行一轮查漏 sweep。下列发现除特别注明外均为 CONFIRMED（有代码级证据或已复现）。

---

## 总体结论

骨架与文档描述一致，多项安全承诺确实兑现（见「PROGRESS 核实」一节）。但 15 项已确认缺陷集中在对这个工具最致命的三条线上：

1. **测量记录失真** —— 数据库记录的运行元数据与实际执行不符；对一个以「检测模型降智」为目的的测量工具，这比普通 bug 严重得多。
2. **付费资源泄漏** —— 取消/重启后 pier 进程与 Docker 容器继续调用付费 API。
3. **对 pier 真实产物形态的错误假设** —— null 字段、目录名截断、指标键名导致端点 500 或数据失真。

---

## 一、已确认的 15 项主要缺陷（按严重程度排序）

### 1. Codex 分支从未传递 reasoning effort，数据库却记录为「有效值」
- 位置：`backend/app/runner.py:69`（分支）、`runner.py:46-47`（伪造记录）、`runner.py:36-39`（`_codex_config`）
- pier 源码证据：codex adapter 接受 `--agent-kwarg reasoning_effort=<v>`（CliFlag，默认 `high`，展开为 `-c model_reasoning_effort=<v>`），但代码只传 `CODEX_AUTH_JSON_PATH` 与 `config_toml_file`；生成的 TOML 也没有 Plan §8.2 要求的 `model` / `model_reasoning_effort` 行。
- 后果：用户跑 codex low vs high 对比时两次都实际以 high 执行；`reasoning_effort_effective` 在创建时直接复制用户所选值（并未观测），所有跨 effort 比较、基线、降智判断对 codex 全部失真且不可事后发现 —— 这正是 Plan §9 四值记录机制要防止的事故。UI 默认 high 与 pier 默认 high 恰好一致，因此现有冒烟测试永远发现不了。
- 修复：codex 分支追加 `--agent-kwarg reasoning_effort={effort}`；`reasoning_effort_effective` 改为运行后观测值或明确置 null。

### 2. 取消运行后的 Docker 定向清理约 98% 失效（大小写不匹配）
- 位置：`backend/app/runner.py:112-125`（`_cleanup_docker_resources`，:120 容器匹配、:123 网络匹配）
- pier 源码证据：trial 目录名 = `{task_name[:32].rstrip('_-')}__{ShortUUID(7)}`（`pier/models/trial/config.py:279`），ShortUUID 字母表含大写；而 compose 项目名经 `_sanitize_docker_compose_project_name` 执行 `name.lower()`（`pier/environments/docker/docker.py:64-65`），容器名 `<小写project>-main-1`、网络 `<小写project>_default`。代码用原始（含大写）目录名做区分大小写的 `key in name` 匹配 → 仅约 2%（纯小写后缀 (33/57)^7）能命中。
- 后果：点取消后 taskkill 只杀 pier 进程树；容器内 agent 继续调用付费 API，孤儿容器（每个声明 2 CPU / 8 GB）与网络持续累积。与 PROGRESS「取消后定向清理匹配容器和网络（已完成）」直接矛盾。
- 修复：匹配两侧统一 `.lower()`。

### 3. `verifier_result: null` 使四类端点对该运行永久 500
- 位置：`backend/app/results.py:52`（`_trial`）、`results.py:37`（`_trial_stage`）；姊妹问题 `results.py:82`
- 机制（已复现）：`data.get("verifier_result", {}).get("rewards", {})` —— dict.get 的默认值只在键**缺失**时生效；pier 用 `model_dump_json`（不排除 None）序列化，禁用验证或验证前出错时必然写出 `"verifier_result": null` → `None.get` 抛 AttributeError。`exception_message` 为 null 时 `redact(None)` 抛 TypeError 同理。
- 后果：GET `/api/runs/{id}`、SSE `/events`、`/api/compare`、`/api/tasks`（经 `list_details` 遍历全部历史）对含该 trial 的运行永久 500。
- 修复：`(data.get("verifier_result") or {})` 模式统一替换；`exception_message` 取值后 `or ""`。

### 4. 禁用 verifier 的运行记录 reward=0.0 / passed=False 且状态 completed
- 位置：`backend/app/runner.py:104-109`（`_sync_result`）
- pier 源码证据（已在安装的 pier 上执行验证）：全部 trial 无验证结果时聚合走 `aggregate_reward_dicts` 的 `<=1` 键分支，`Mean().compute([None,None,None])` → `{"mean": 0.0}`，**没有 `reward` 键** → `m.get("reward", 0)` 全取 0。
- 后果：「未测量」与「全部失败」在数据上无法区分；此类运行可被设为基线毒害回归比较。补充：即使启用验证，单键 rewards（如仅 `{"reward": x}`）也会被改名为 `{"mean": ...}`——DeepSWE 任务因多键 rewards（reward/partial/f2p/p2p）不受影响，但这是潜伏约定。
- 修复：verification=False 时 reward/passed 记 null；解析时兼容 `mean` 键或按 evals 结构判断。

### 5. `_sync_result` 完全忽略 returncode —— pier 中途崩溃仍标记 completed
- 位置：`backend/app/runner.py:98`（签名）、:109（判定）
- 机制：函数体从未引用 `returncode` 参数。pier 非零退出（如 Docker daemon 崩溃、CLI 报错）时，只要部分 result.json 有 metrics 且 `n_errored_trials` 为 0，运行即记 completed / passed=True / reward 按残缺 trial 均值。
- 恶化因素：`backend/tests/test_runner.py:16` 传入 `returncode=1` 并断言 completed —— 把缺陷固化为规范。
- 修复：returncode 非零时至少标记 failed 或 interrupted，并保留部分统计。

### 6. 取消与完成的双向状态竞态，最后写者赢
- 位置：`backend/app/runner.py:93`（宽泛 except）、:127-135（`cancel_run`）、:95（finally 才弹出 `_processes`）
- 场景 A：taskkill /F 截断正在写入的 result.json → `_sync_result` 的裸 `json.loads`（:101，未复用 `results._json` 的守卫）抛 JSONDecodeError → 宽泛 except 无条件写 `failed`，覆盖刚提交的 `cancelled`，用户取消被误报为失败。
- 场景 B（反向）：`_processes` 在 finally 中才弹出（晚于 `_sync_result`），运行刚结束的约 1 秒窗口内点取消 → 对已死 PID taskkill 后仍把 completed 改写为 cancelled 并覆盖 finished_at，破坏基线资格。
- 修复：状态迁移加锁/条件更新（`UPDATE ... WHERE status='running'`），`_sync_result` 复用 `_json`。

### 7. SSE 推送缺 `regression` 字段，回归告警横幅被抹除
- 位置：`backend/app/main.py:120`（SSE 只发 `parsed_run_detail`）vs `main.py:81`（GET 附加 `regression`）；前端 `frontend/src.tsx:226`（`setDetail(next)` 整体替换）、:953（横幅依赖 `detail.regression`）
- 后果：打开设有基线的已完成运行时，初始 fetch（带 regression）与 SSE 首条消息（不带）竞态，SSE 常后到 → **工具的核心输出（降智/回归告警）闪现后消失或从不出现**，直到用户切走再切回。
- 修复：SSE payload 同样附加 regression，或前端合并而非整体替换。

### 8. pier 将 trial 目录名截断到 32 字符 → Live 视图出现幽灵 trial
- 位置：`backend/app/results.py:105`（占位补齐用全名计数）、:55（回退到目录名）
- pier 源码证据：`task_name[:32].rstrip('_-')__<uuid>`；默认 7 题中 4 题超长：csstree-shorthand-expansion-compression(39)、happy-dom-deterministic-intersectionobserver(44)、superjson-error-stack-serialization(35)、dateutil-rfc5545-timezone-interop(33)。
- 后果：运行中（result.json 尚未写出，pier 在 trial 结束才写）`_trial` 回退到截断名，与全名计数不匹配 → 真实运行行 + 幽灵 queued 占位行并存，7 个 trial 最多显示 11 个 tile，进度百分比失真；崩溃到未写 result.json 的 trial 永久保留截断名，compare 矩阵与 Tasks 历史出现无法归属的假任务行。
- 修复：占位匹配时对 expected 任务名同样做 `[:32].rstrip('_-')` 归一，或改用 job_stats 的 n_pending/n_running 计数。

### 9. `/api/restore` 对备份行零验证：路径遍历删除、僵尸行、设置毒化
- 位置：`backend/app/main.py:193`（`Run(**values)`）、:189-190（settings 原样写入）、:107-109（delete 使用 job_name）；`backend/app/schemas.py:32-36`（RestorePayload 是 `list[dict]`）
- 已沙盒复现：`Path(jobs)/'..'` 经 rmtree 确实删除上级目录；pathlib join 绝对路径会整体替换 base。三重后果：
  - **路径遍历**：恶意/手改备份中 `job_name: ".."` 或绝对路径 → 在 UI 删除该行时 `shutil.rmtree(jobs_dir/job_name)` 删除任意目录（`.supervisor.log` 的 unlink 同样可注入）；
  - **僵尸行**：`status: "running"` 原样入库 → 不可取消（cancel 返回 False 且不改状态）、不可删除（409「请先取消」）、SSE 无限轮询，直到重启才被 init_db 清扫；
  - **设置毒化**：settings value 非 JSON → `get_preferences()`（preferences.py:25）每次 `json.loads` 崩溃 → bootstrap/settings/diagnostics/运行详情全部 500 且跨重启持续（settings 页自身也打不开，只能再导入一份合法备份或手改 SQLite 修复）。
- 触发面：本地单用户工具，需用户主动导入恶意/损坏备份 —— 机制已确认，暴露度中等。
- 修复：restore 前逐行验证（job_name 白名单字符、status 归一为终态、settings value JSON 校验）。

### 10. 服务重启后 pier 进程树与容器成为孤儿，继续烧钱
- 位置：`backend/app/database.py:35`（只改 DB 状态）、`backend/app/main.py:22`（lifespan 无 shutdown 清理）、`runner.py:85`（CREATE_NEW_PROCESS_GROUP）
- 机制：CREATE_NEW_PROCESS_GROUP 使 pier 不接收控制台 Ctrl+C；uvicorn 退出不杀子进程；`run.pid` 只写从不读（无收割逻辑）。重启后 UI 显示「服务重启后检测到运行已中断」，而 pier + 容器内 agent 实际继续运行、继续调用付费 API，且此时「取消」按钮已消失，只能手动 taskkill + docker rm。
- 修复：lifespan shutdown 时终止 `_processes`；启动时按存量 pid/job 目录收割并清理容器。

### 11. `tasks: []` 未被拒绝 → pier 无 `-i` 过滤运行全部 113 个任务
- 位置：`backend/app/schemas.py:11`（缺 `min_length=1`）；`main.py:69` 的缺失检查对空列表真空通过；`runner.py:65` 空循环不产生 `-i`
- 已复现：POST `/api/runs` `{"tasks": []}` 被接受。后果：113 个任务（每个声明 2 CPU/8 GB）全部调度进 Docker，笔记本被打满且大量付费调用；同时 expected total=0，进度显示 0% 无从观察。前端有守卫，但 API 层放行。
- 修复：`tasks: list[str] = Field(min_length=1)`。

### 12. 前端取消不校验结果 + 「preflight」只是标签
- 位置：`frontend/src.tsx:294-306`（忽略 `{cancelled:false}`）；`backend/app/runner.py:56`（preflight 无真实检查）
- 机制:在 preflight 窗口（读凭据/建临时目录/icacls，Popen 之前）或重启后点取消 → 后端返回 `cancelled:false` 且不改状态，前端仍显示「运行已取消，Docker 容器和网络清理已触发」；实际运行继续烧钱。叠加：状态机的「preflight」阶段没有执行 Plan §17 的任何预检（docker、模型 API、host.docker.internal、磁盘内存均不检查，`run_checks()` 从未被运行链路调用），UI 展示的安全流程均未发生。
- 修复：前端根据响应体提示「尚无法取消/取消失败」；create_run 前调用诊断检查并阻断。

### 13. `redact()` 从不接收真实 token —— 仅靠正则兜底
- 位置：`backend/app/runner.py:151`（run_log）、`runner.py:93`（error）、`results.py:82`（failure_message）、`results.py:93`（trial_log）
- 机制：`redact(value, secrets)` 支持替换已知密钥（测试 test_security.py:28 也传入 `[secret]` 验证过该路径），但四个生产调用点均不传 token，只剩 `Bearer ...` / `api_key=...` 两条正则。agent/pier 日志以其他形态回显 token（URL 查询串、配置 dump、`Authorization: token X`、报错内嵌）时原样返回浏览器。
- 后果：违背 Plan §7 与验收标准第 12 条「Token 不出现在日志或前端 API 响应中」。
- 修复：调用处读取当前凭据并传入 `secrets=[cred.token]`。

### 14. EventSource `onerror` 一律永久关闭，Live 视图冻结
- 位置：`frontend/src.tsx:230`
- 机制：`source.onerror = () => source.close()` 对仍在运行的 run 也禁用了 EventSource 内建自动重连，且无轮询降级；detail 此后不再更新（初始 fetch 之外 detail 只来自该流）。
- 后果：数小时运行期间任何瞬断（休眠、uvicorn 重启、网络抖动）→ 进度条/阶段/trial 网格冻结在旧值，`detail.status` 停在 running 使取消按钮留在可能已结束的运行上（喂给第 12 条的假取消路径）。
- 修复：非终态时允许自动重连（onerror 里检查当前状态再决定 close），或降级为轮询。

### 15. 效率：SSE 每秒全量重读磁盘；单次详情请求解析 5 遍
- 位置：`backend/app/main.py:115-125`（SSE 循环）、`backend/app/results.py:157-164`（regression_for 内再调 compare_runs）
- 量化：SSE 每秒调用 `run_detail` → 重读全部 trial 的 result.json 与**完整 model.patch**（每个至多 100KB）+ 一次 settings 查询，仅为 json.dumps ~700KB 后与上次字符串比对；1 小时 7-trial 运行 ≈ 5.4 万次文件打开、累计 ~2.5GB 读取与序列化 —— 与被测 benchmark 抢磁盘/CPU，直接干扰「归一化耗时」这一回归判据。前端确认 SSE 里的 patch 从未被渲染（TrialDetail 只用点击时快照），纯属死重量。GET `/api/runs/{id}` 在有基线时执行 `run_detail` 5 次（endpoint 1 次 + regression_for 2 次 + 两个单元素 compare_runs 各 1 次）。
- 修复：SSE payload 剔除 patch（保留 patch_bytes，点击时按需拉取）；用 result.json mtime 指纹做变化检测；regression_for 直接复用已算出的 detail 做每任务通过率 groupby。

---

## 二、其余已确认问题（未进前 15，仍建议处理）

### 潜伏与边缘正确性
- `runner.py:62` timeout 换算用硬编码除数 5400/1800，而 pier 的 multiplier 乘的是**每个任务自身 task.toml 声明的超时**；当前 113 个任务恰好全部声明 5400/1800 所以数值正确（已全量核对），任何新任务/改动即失准；任务缺省时 pier 默认（verifier 600、agent 无限）会让偏差更大。
- `runner.py:44` job_name 用本地时间（`datetime.now()`），created_at/finished_at 用 UTC —— 同一记录两个时钟差 8 小时（如 finished_at `08:36:19` vs job_name `ui-20260711-163619`），人工对照 jobs 目录与导出数据时极易误判。
- `main.py:50` bootstrap 的 task.toml 解析无守卫（task_catalog 的副本有守卫）：一个损坏的 task.toml → bootstrap 500 → 前端无 .catch，整个 UI 永远停在加载页。当前 7 个默认任务解析正常（已核对），属潜伏项。
- `results.py:25` `_seconds` 只捕 ValueError：naive/aware 相减抛 TypeError、非字符串抛 AttributeError，同样导致详情/SSE/compare 500。
- `runner.py:131` POSIX 取消路径 `os.killpg(proc.pid,...)`：Popen 未用 `start_new_session=True`，killpg 必抛 ProcessLookupError（当前仅 Windows 使用，属死代码缺陷）。
- `runner.py:20` `_docker_url` 用 urlparse 小写化的 hostname 做区分大小写 replace：凭据 URL 写 `http://LOCALHOST:9887/v1` 时映射被跳过，容器内连不上且无提示。
- `main.py:107` 删除/读取 artifacts 全部经「当前」jobs_dir 偏好解析而非按 run 存储：改设置或恢复含 jobs_dir 的备份后，历史运行全部失联（trial 全显示 queued 占位），删除静默清不到旧目录却返回 `deleted:true`。
- 前端：`src.tsx:220` detail fetch 无过期守卫（快速切换 run 时旧响应覆盖新选择）；`src.tsx:678/803` Live 切换器不清 activeTrial（A 运行的 patch/日志挂在 B 运行下）且 activeTrial 是点击时快照、不随 SSE 更新；`src.tsx:251` 三 Agent 同跑 Promise.all 无部分失败处理（已创建的 run 不被导航，重试造成重复 run）；`src.tsx:546` 等 `+e.target.value` 清空输入变 0；`src.tsx:128` 非 JSON 错误响应时 `response.json()` 的 SyntaxError 吞掉真实错误；`src.tsx:1333` restore 无错误处理（失败无任何提示）且 file input 不重置（同名文件二次选择无反应）；`src.tsx:125` apiBase 仅识别端口 5173（vite 回退 5174 或 preview 4173 时全部 API 打空）。
- `index.html` 无 DOCTYPE/`<html>`/`<title>` → quirks mode 渲染（charset 无碍：Starlette 响应头带 `charset=utf-8`，已实测）。

### 承诺缺口（Plan/PROGRESS 对照）
- 回归告警只实现 Plan §18 五条规则中的三条：缺「耗时下降超 35% 且通过率下降（疑似提前终止）」与「控制任务 actionlint 降到 1/4 以下」两条；level 恒为 warning（`"warning" if reasons else "ok"`），承诺的黄→红两次异常升级状态机不存在。
- `results.py:33` estimate_cost 只有 3 个 token 价格类，缺 cache-write（$6.25/1M）；响应中的 `service_tier` 从未读取（Plan §11 承诺自动选层）。部分归因于 pier stats 无 cache-write 数据源，但 §14 要求的「未返回字段存 null」也未做。
- 诊断页缺 Plan §12.8/§17 的大部分检查：无容器→host.docker.internal 探测、无 Responses/Anthropic 双协议探测、无六档 effort 映射验证、三个 Pier Windows 补丁只检查 1 个（且靠精确字符串匹配 pier 源码，pier 升级重排代码即永久误报）。
- 每次运行未记录 pier 版本 / Task digest / image digest 等 Plan §16 可复现性字段 —— 基线与当前运行之间的 harness 变化无据可查，正是工具要解决的「模型 vs harness」歧义。
- `src.tsx:263` 并发=4 时 `confirm_high_concurrency` 由前端自动填 true，无任何确认对话框；Plan §6 的「红色警告并要求手动确认」以及「>4 高级设置解锁」均不存在（后端对 >4 硬拒绝）。前端还自行复刻了阈值文案，后端 `/api/concurrency/{v}` 与 `/api/runs/validate` 两个专用端点是死代码。

### 测试与工程卫生
- `backend/tests/conftest.py` 只隔离了 DATABASE_URL 和 JOBS_DIR，未设 DEEPSWE_UI_CREDENTIAL_FILE：test_bootstrap 会 glob 并**逐个读取开发者真实 `~/Documents/github/*.txt` 凭据文件**——测试触碰真实密钥且机器相关。
- conftest 的 `pytest_sessionfinish` rmtree 清不掉 test.db（SQLAlchemy engine 从未 dispose，Windows 文件锁），临时目录在 %TEMP% 每次泄漏。
- `frontend/package.json` 全部依赖 `"latest"` 且构建工具混在 dependencies —— 不可复现构建，与工具自身强调的可复现性相悖；`tsconfig.json` 缺 `noEmit`，`tsc -b` 在源码旁生成 src.js/tsbuildinfo（.gitignore 为此打补丁），杂散 `vite-smoke.stdout.log` 未被忽略。
- `run-ui.ps1`：PowerShell 5.1 下 `$ErrorActionPreference='Stop'` 对原生命令无效，pip/npm 失败后脚本继续；浏览器在 uvicorn 启动前打开（必见连接失败）；每次启动无条件 pip install + 全量前端构建（15-35 秒纯等待）。
- `main.py:178-183` CSV 导出无 BOM（中文 Windows Excel 默认按 ANSI/GBK 解码显示乱码），自由文本字段未做公式转义（`=HYPERLINK(...)` 形式的 model/error 值在 Excel 中被求值）。

### 结构性重复与死代码
- `runner.py:138` serialize() 与 `results.py:115` run_detail() 两套约 20 键手写序列化已在 token 来源与 cost 键上漂移（列表页与详情页可显示不同数字）；serialize 同值双键 `cost_usd`/`reported_cost_usd`。
- 终态集合 `{completed,failed,cancelled,interrupted}` 在 main.py×2、results.py、database.py（反向 SQL 列表）、src.tsx 共 5 处复制，跨两种语言。
- `config.py:12` `codex_config_file` 是死配置：运行时从不读取；根目录 `codex-local-provider.toml` 本次被改为硬编码局域网 IP `http://192.168.0.108:9887/v1`（原为 host.docker.internal），仅供手册命令使用，换网络即失效 —— 纯误导项，建议删除设置并在 NOTES 中注明。
- `results._json` 守卫版 JSON 读取被 runner.py:101 与 diagnostics.py:16 各自重写（runner 版缺守卫，即第 6 条事故源）；`fromisoformat(x.replace("Z","+00:00"))` 4 处复制（restore 两处无 try 守卫，坏时间戳 → restore 500）；task.toml 元数据读取 bootstrap/task_catalog 双写（bootstrap 版无守卫，见上）。
- Agent 适配知识散布 5 文件 9 处（schemas Literal、main agents 列表、runner 三处、results 日志文件名表、diagnostics 别名表）——Plan §8 的 AgentAdapter 接口未落地，新增 agent 需同步改 5 个文件且漏改均为静默失败；codex effort 缺失正是该散布的第一次事故。
- `diagnostics.py:10` 死赋值（credential_path().parents[0] 立即被覆盖）+ 函数内重复 import + 未用的 `settings` 导入；`main.py:35` 函数内重复 `from pathlib import Path`、`Body` 导入未用。
- 效率補充：`/api/tasks` 对每个历史 run 全量磁盘解析（含全部 patch）只为 7 张卡片；compare 8 个 run 返回 ~5.6MB patch 全文而热力图只用通过率；diagnostics 每次 rglob 整个 jobs 树并解析所有 result.json 无提前终止；`get_preferences()` 每调用一次 DB 查询，被 SSE 每秒放大；前端 4 秒轮询 + SSE 每条消息再触发一次全列表刷新，页面隐藏时不暂停。

---

## 三、PROGRESS 文档核实结论

属实的声明（已验证）：
- 「后端测试 15 passed」—— 15 个测试函数确实存在（8+3+4）；
- 「测试使用独立临时 SQLite」—— conftest 确实隔离 DB 与 jobs 目录（但凭据路径未隔离，见上）；
- 「Token 不进入数据库」—— 已查开发库 settings/runs/baselines 三张表结构与内容，无 token；
- 「删除同步清理索引、基线、artifacts、supervisor 日志」「运行中必须先取消（409）」—— 代码与测试一致；
- 「Diagnostics 改名 model-api、401 标注」—— 属实；
- `.gitattributes` 的 `*.sh text eol=lf` 与 Plan §17 的 LF 补丁要求一致（test.sh 的 M 状态即行尾归一化，diff 为空）。

言过其实的「已完成」：
- 「Docker 清理（已完成第一版）」—— 实际约 98% 失效（发现 2）；
- 「记录 effort 请求值、adapter 映射值和有效值」—— 有效值为创建时复制，codex 的映射值记录的是从未发送的参数（发现 1）；
- 「基线与回归告警（已完成第一版）」—— 5 条规则缺 2 条、无升级状态机（文档第 5 节只自认了状态机一项）；
- 「支持取消 Pier 进程树…并定向清理」—— 进程树属实，定向清理失效，且存在取消/完成双向竞态（发现 2、6、12）。

综合：文档声称 92% 完成度；就「可点通的 UI 链路」而言大体属实，但「取消/清理」「记录真实性」「禁用验证路径」「restore 健壮性」四个模块的完成判定应显著下调。

---

## 四、建议修复顺序

1. **Codex effort 透传**（一行 `--agent-kwarg reasoning_effort={effort}`）+ effective 字段改为观测值或 null —— 否则 codex 所有历史 effort 数据作废；
2. **Docker 清理大小写归一**（两侧 `.lower()`）+ 重启时按 pid 收割孤儿 pier/容器 —— 止血付费泄漏；
3. **null 守卫**（verifier_result / exception_message / `_sync_result` 复用 `_json`）+ **returncode 判定** —— 消除端点 500 与假 completed；
4. **SSE 补 regression 字段** + 前端 SSE 断线重连 + cancel 响应校验 —— 让核心告警可见、状态可信；
5. **restore 输入验证**（job_name 字符白名单、status 归一终态、settings JSON 校验）—— 关闭路径遍历与毒化；
6. `tasks` min_length=1、timeout 换算语义修正（或 UI 改为直接暴露 multiplier）、trial 名 32 字符归一；
7. 效率与结构项按上文清单酌情批量处理（SSE 剔除 patch、regression_for 去重解析、serialize/run_detail 合并、终态集合常量化、AgentAdapter 收拢）。

---

*审查产出：15 项主要缺陷全部 CONFIRMED（含 pier 0.3.0 源码引用、复现记录或多角度交叉验证）；其余问题均有代码位置与触发条件。本文件为本次审查的完整存档。*
