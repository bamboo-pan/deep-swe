# 模型降智测试工具 Docker 资源清理集成方案

更新时间：2026-07-11

## 1. 结论摘要

现有 DeepSWE Regression UI 每次 Trial 都会通过 Pier 创建带随机后缀的 Docker Compose 项目，因此 Docker Desktop 中会持续出现以下镜像：

```text
<task>__<trial-id>-main
<task>__<trial-id>-pier-egress-proxy
<task>__<trial-id>__verifier__trial-main
```

这些镜像并不是每次完整下载一份。Docker 会复用内容相同的文件系统层，但每个 Trial 仍会留下新的镜像名称和少量独占元数据，BuildKit 也会保留构建缓存。因此需要把资源清理接入 Job/Trial 生命周期，而不是让用户定期执行高风险的全局 `docker system prune -a`。

推荐方案分为三层：

1. **运行结束清理**：Pier 进程退出后，删除该 Run 所属 Trial 的 `main`、`pier-egress-proxy` 和 verifier 临时镜像；保留基础镜像及 BuildKit 缓存。
2. **删除 Job 兜底清理**：用户删除历史 Run 时，在删除 Job artifacts 前定向清理其 Docker 资源。
3. **缓存维护**：在设置页提供预览和手动清理入口，只清理超过保留期且未使用的 BuildKit 缓存。

默认不删除任务声明的 ECR 基础镜像、`ubuntu:24.04` 等公共镜像，不使用 `--force`，不自动执行全局镜像清理。

## 2. 已核实的现状

### 2.1 本机 Docker 实测

2026-07-11 对当前 Docker daemon 的检查结果：

| 项目 | 实测值 |
|---|---:|
| 镜像条目 | 40 |
| 镜像实际总占用 | 10.24 GB |
| 当前可回收镜像空间 | 2.337 GB |
| BuildKit 构建缓存 | 7.217 GB |
| 容器 | 0 |
| 本地卷 | 0 |

三个同批次 `actionlint-action-pinning-lint` 主镜像虽然 Image ID 不同，但 30 个 RootFS layer 完全相同：

| 镜像类型 | Docker Desktop 显示大小 | 共享大小 | 单条独占大小 |
|---|---:|---:|---:|
| `*-main` | 约 4.1 GB | 约 4.103 GB | 约 29.27 KB |
| `*-pier-egress-proxy` | 约 314 MB | 约 314.1 MB | 约 5.9 KB |

因此不能把 Docker Desktop 列表中的每行 Size 直接相加。列表主要问题是条目不断累积和缓存长期增长，而不是每个条目都独占数 GB。

### 2.2 Pier 的实际构建行为

Pier 当前版本在环境启动时会执行 `docker compose build`。只有显式 `force_build=True` 时才追加 `--no-cache`，默认没有 `--pull`：

```python
command = ["build"]
if force_build:
    command.append("--no-cache")
```

任务配置中的预构建镜像作为 Agent 镜像的 `FROM`，随后在其上安装对应 Agent。egress proxy 则基于 `ubuntu:24.04` 构建。只要基础层和安装指纹未变化，Docker/BuildKit 会命中缓存。

Pier 本身支持在 `stop(delete=True)` 时执行：

```text
docker compose down --rmi all --volumes --remove-orphans
```

但当前 UI 的正常运行参数和生命周期没有走该删除路径，所以 Trial 镜像会保留下来。

### 2.3 现有 UI 清理缺口

当前 `backend/app/runner.py::_cleanup_docker_resources()` 只处理：

- 名称匹配当前 Job Trial 的容器；
- 名称匹配当前 Job Trial 的网络。

当前 `DELETE /api/runs/{run_id}` 只处理：

- 数据库中的 Run 和 Baseline；
- Job artifacts 目录；
- supervisor 日志。

两条路径都没有删除 Trial 镜像，也没有管理 BuildKit 缓存。由于删除 Run 时会先删除 artifacts，后续将失去最可靠的 Trial 资源标识，所以 Docker 定向清理必须发生在 artifacts 删除之前。

## 3. 目标与非目标

### 3.1 目标

- 自动清除已结束 Trial 的临时镜像条目。
- 保留共享基础镜像和近期 BuildKit 缓存，避免下一轮重新下载或完整重建。
- 在 UI 中展示 Docker 镜像、共享空间、独占空间和构建缓存占用。
- 清理前可预览，清理结果可审计。
- 严格限定为本工具创建的资源，不影响用户其他 Docker 项目。
- Docker 不可用或清理失败时，不改变模型测试结果状态。

### 3.2 非目标

- 不自动执行 `docker system prune -a`。
- 不自动删除任务 `task.toml` 中声明的基础镜像。
- 不管理 Docker Desktop 的 WSL2/VHDX 压缩。
- 不通过镜像显示大小估算真实物理占用。
- 第一版不修改 Pier 安装包源码，清理逻辑由现有 UI 后端负责。

## 4. 资源所有权识别

### 4.1 资源标识来源

每个 Run 的 Job 目录中包含 Trial 子目录。Pier 使用 Trial/session 名称作为 Docker Compose project name，Compose 默认产生：

```text
<project>-main
<project>-pier-egress-proxy
```

verifier separate mode 还可能创建包含 `__verifier__trial` 的子项目镜像。

后端应复用 Pier 的名称规整规则：

1. 转为小写；
2. 首字符不是字母或数字时加前缀 `0`；
3. 镜像名中非 `[a-z0-9._-]` 字符替换为 `-`；
4. Compose project name中非 `[a-z0-9_-]` 字符替换为 `-`。

### 4.2 第一版候选镜像规则

从 `jobs/<job-name>/` 枚举真实 Trial 标识，规整后只生成允许删除的候选集合：

```text
<trial-project>-main:latest
<trial-project>-pier-egress-proxy:latest
<trial-project>__verifier__trial-main:latest
<trial-project>__verifier__trial-pier-egress-proxy:latest
```

同时允许匹配以已知 Trial project 为前缀、且服务后缀严格属于以下白名单的镜像：

```text
-main
-pier-egress-proxy
```

禁止直接接受前端传入的任意镜像名称，禁止使用只按 `*-main` 匹配的全局正则。

### 4.3 后续增强：资源清单

第二版可在 Run 启动前后记录 `docker image ls --no-trunc` 差异，生成：

```text
jobs/<job-name>/docker-resources.json
```

建议结构：

```json
{
  "version": 1,
  "job_name": "ui-20260711-...",
  "projects": ["task__trial"],
  "images": [
    {
      "repository": "task__trial-main",
      "tag": "latest",
      "image_id": "sha256:...",
      "role": "agent"
    }
  ],
  "created_at": "2026-07-11T...Z"
}
```

清理时优先使用资源清单，Trial 目录推导作为兼容历史 Job 的回退方案。

## 5. 清理策略

### 5.1 运行结束后的轻量清理

触发点：`_execute()` 中 Pier 子进程结束且 `_sync_result()` 完成之后。

动作：

1. 确认 Run 已进入 `completed`、`failed`、`cancelled` 或 `interrupted`。
2. 从 Job artifacts 提取 Trial project。
3. 检查是否仍存在引用候选镜像的运行中或已停止容器。
4. 对未被引用的 Trial 镜像执行 `docker image rm <repository>:<tag>`。
5. 不添加 `--force`。
6. 不调用 builder prune。
7. 将删除、跳过和失败数量写入 supervisor 日志或结构化清理结果。

这样可以立即消除 Docker Desktop 中的随机 Trial 镜像条目，而共享层仍由基础镜像或 BuildKit 缓存持有。

### 5.2 取消运行

取消顺序调整为：

```text
终止 Pier 进程树
  -> 等待进程退出
  -> 清理容器
  -> 清理网络
  -> 清理已生成的 Trial 镜像
  -> 保留日志和部分结果
```

当前 `cancel_run()` 在终止进程后立即执行容器和网络清理。应扩展为统一的 `cleanup_job_resources(job_name, policy)`，并保证镜像删除发生在容器删除之后。

### 5.3 删除历史 Run

`DELETE /api/runs/{run_id}` 的顺序改为：

```text
校验 Run 为终态
  -> 根据尚未删除的 artifacts 生成 Docker 清理预览
  -> 定向删除容器、网络和 Trial 镜像
  -> 删除 Baseline 和 Run 数据
  -> 删除 Job artifacts 和 supervisor 日志
```

Docker 清理失败不应阻止用户删除历史结果，但 API 响应中应返回警告：

```json
{
  "deleted": true,
  "id": 123,
  "docker_cleanup": {
    "removed_images": 4,
    "skipped_images": 0,
    "errors": []
  }
}
```

### 5.4 BuildKit 缓存维护

BuildKit 缓存不在每次 Run 结束时清理，因为它直接影响下一轮测试启动速度。

推荐默认策略：

- 保留最近 7 天缓存；
- 仅手动执行，第一版不启用后台定时任务；
- 命令为 `docker builder prune -a --filter until=<保留期>`；
- 执行前必须展示预计/当前可回收空间并二次确认；
- 有 Run 处于非终态时禁用缓存清理。

可选增强策略：当 BuildKit 缓存超过配置阈值，例如 20 GB，Diagnostics 显示 warning，但仍不自动删除。

### 5.5 基础镜像保护

以下镜像不进入自动删除候选：

- `task.toml` 的 `[environment].docker_image`；
- `ubuntu:24.04`；
- 其他没有明确归属于本工具 Trial 的镜像；
- 被任意容器引用的镜像；
- 用户手动标记为保留的镜像。

不使用 `docker image prune -a`，因为当前所有容器为零时，它会把 ECR 基础镜像也视为 unused，导致下一次重新下载数 GB。

## 6. 后端设计

### 6.1 新增模块

新增 `backend/app/docker_cleanup.py`，职责保持独立：

```python
@dataclass
class DockerCleanupPolicy:
    remove_containers: bool = True
    remove_networks: bool = True
    remove_trial_images: bool = True
    prune_build_cache: bool = False
    cache_retention_hours: int = 168

def docker_storage_summary() -> dict: ...
def discover_job_projects(job_name: str) -> list[str]: ...
def discover_managed_images(job_name: str | None = None) -> list[dict]: ...
def preview_job_cleanup(job_name: str) -> dict: ...
def cleanup_job_resources(job_name: str, policy: DockerCleanupPolicy) -> dict: ...
def preview_builder_cleanup(retention_hours: int) -> dict: ...
def prune_builder_cache(retention_hours: int) -> dict: ...
```

所有 Docker 调用必须满足：

- 使用参数数组和 `subprocess.run()`，不使用 `shell=True`；
- 设置明确超时；
- 捕获 stdout/stderr 并脱敏；
- Docker daemon 不可用时返回结构化错误；
- 删除镜像不使用 `-f`；
- 单个资源失败不影响其他资源的 best-effort 清理。

### 6.2 修改 runner

将现有 `_cleanup_docker_resources()` 替换为新模块调用：

```python
cleanup_job_resources(
    job_name,
    DockerCleanupPolicy(
        remove_containers=True,
        remove_networks=True,
        remove_trial_images=True,
    ),
)
```

接入点：

- `cancel_run()`：进程退出后调用；
- `_execute()`：Pier 正常或异常退出后的 finally 阶段调用轻量镜像清理；
- `DELETE /api/runs/{run_id}`：删除 artifacts 前调用兜底清理。

需要避免重复调用造成错误：资源不存在应视为成功，清理函数必须幂等。

### 6.3 API

新增接口：

```text
GET  /api/docker/storage
POST /api/docker/cleanup/preview
POST /api/docker/cleanup
```

`GET /api/docker/storage` 返回：

```json
{
  "available": true,
  "images": {
    "count": 40,
    "size_bytes": 10995116277,
    "reclaimable_bytes": 2509334118,
    "managed_count": 32
  },
  "build_cache": {
    "count": 106,
    "size_bytes": 7749199872,
    "reclaimable_bytes": 1371537
  },
  "active_runs": 0
}
```

`POST /api/docker/cleanup/preview` 请求示例：

```json
{
  "scope": "expired",
  "retention_hours": 168,
  "include_build_cache": true
}
```

第一版支持 scope：

- `job`：指定终态 Run；
- `orphaned`：属于本工具，但数据库和 Jobs 目录都已不存在；
- `expired`：超过保留期的终态 Run 资源；
- `build_cache`：仅构建缓存。

执行接口不得直接信任前端提交的 image 列表。后端根据 scope 重新计算候选，并返回实际执行结果。

### 6.4 设置项

扩展 `SettingsUpdate`、`preferences.KEYS` 和 Settings UI：

| 设置 | 默认值 | 说明 |
|---|---:|---|
| `docker_cleanup_after_run` | `true` | Run 终态后删除 Trial 专属镜像 |
| `docker_cleanup_on_delete` | `true` | 删除 Run 时执行兜底清理 |
| `docker_cache_retention_hours` | `168` | BuildKit 缓存保留 7 天 |
| `docker_cache_warning_gb` | `20` | 超过后 Diagnostics 警告 |

不建议第一版加入“自动 prune BuildKit 缓存”开关，避免后台任务在用户不知情时导致下次构建变慢。

## 7. 前端设计

### 7.1 Settings 增加 Docker 存储卡片

在“设置与数据”页面新增“Docker 存储与清理”：

- 镜像实际总占用；
- 可回收镜像空间；
- 本工具管理的 Trial 镜像数量；
- BuildKit 缓存占用；
- 缓存保留期；
- “扫描可清理资源”按钮；
- “清理 Trial 镜像”按钮；
- “清理过期构建缓存”按钮。

清理按钮必须先调用 preview，并在确认框中展示：

```text
将删除 18 个 Trial 镜像条目
预计释放的独占镜像空间：2.3 GB
将清理超过 7 天的未使用构建缓存
不会删除任务基础镜像
```

注意：“预计释放”只能采用 Docker 返回的 reclaimable/unique 数据，不得把镜像列表中的 Size 相加。

### 7.2 Results 删除提示

批量删除 Run 的确认文案调整为：

```text
删除选中的运行、日志和结果文件，并清理其未使用的 Trial Docker 镜像。
共享基础镜像和构建缓存将保留。
```

执行完成后显示汇总：

```text
已删除 3 条运行，清理 12 个 Trial 镜像；2 个镜像仍被容器使用，已跳过。
```

### 7.3 Diagnostics 增强

新增检查项：

- `docker-images`：镜像总占用、可回收空间和 managed count；
- `docker-build-cache`：缓存大小，超过阈值显示 warning；
- `docker-orphans`：数据库/Jobs 中没有归属的本工具镜像数量；
- `docker-cleanup-policy`：运行结束自动清理是否启用。

Docker 空间告警只影响 ready 的提示级别，不应阻止创建模型测试 Run。

## 8. 并发、失败与安全边界

### 8.1 并发保护

- 全局使用一个 Docker cleanup lock，避免多个三 Agent Run 同时清理。
- 清理某个 Job 前再次查询数据库状态。
- 任意非终态 Run 存在时，禁止执行全局 BuildKit prune。
- 镜像删除前用 `docker ps -a --filter ancestor=<image>` 检查引用。
- 不清理正在被另一个 Job 使用的 Compose project。

### 8.2 失败策略

- Docker Desktop 未启动：记录 warning，Run 结果仍正常落库。
- 镜像仍被引用：跳过并报告，不强制删除。
- 某个镜像不存在：按幂等成功处理。
- BuildKit prune 超时：停止该操作，不影响 Job 数据。
- artifacts 已被手动删除：只允许根据已保存资源清单清理；没有清单时不做宽泛猜测。

### 8.3 审计信息

清理结果应至少记录：

```json
{
  "job_name": "ui-...",
  "trigger": "run-finished",
  "started_at": "...",
  "finished_at": "...",
  "removed_images": ["..."],
  "skipped_images": [{"name": "...", "reason": "in-use"}],
  "removed_containers": 0,
  "removed_networks": 0,
  "errors": []
}
```

第一版可以写入 `<job-name>.docker-cleanup.json`；后续若需要跨 Job 查询，再增加数据库表。

## 9. 测试方案

### 9.1 单元测试

新增 `backend/tests/test_docker_cleanup.py`：

- Pier/Compose 名称规整与 Windows 路径字符处理；
- 从 Job Trial 目录生成候选镜像；
- 只接受白名单服务后缀；
- 不把 ECR、Ubuntu 或任意用户镜像加入候选；
- active Run 禁止清理；
- `docker image rm` 不包含 `-f`；
- 单个删除失败后继续清理其他候选；
- Docker 不可用时返回结构化错误；
- 重复清理保持幂等；
- artifacts 删除前完成候选发现。

所有 subprocess 在单元测试中 mock，不依赖本机 Docker 状态。

### 9.2 API 测试

扩展 `backend/tests/test_app.py`：

- storage API 的正常和 Docker 不可用响应；
- cleanup preview 不修改任何资源；
- cleanup 拒绝活动 Run；
- 删除终态 Run 时调用定向 Docker 清理；
- Docker 清理失败时 Run 仍可删除且响应包含 warning；
- 前端不能通过请求体注入任意镜像名。

### 9.3 本机集成测试

使用已有小镜像创建仅用于测试的标签：

```powershell
docker tag alpine:3.20 cleanup-test__trial-main:latest
docker tag alpine:3.20 cleanup-test__trial-pier-egress-proxy:latest
```

验证：

1. preview 只列出测试标签；
2. cleanup 删除测试标签；
3. `alpine:3.20` 本身仍存在；
4. 没有运行 `docker system prune -a`；
5. 第二次 cleanup 返回零删除且无错误。

集成测试完成后只删除测试标签，不触碰现有模型任务镜像。

## 10. 实施阶段

### 阶段一：安全的定向清理

- 新增 `docker_cleanup.py`。
- 将容器、网络、镜像清理统一为幂等函数。
- 接入取消 Run 和删除历史 Run。
- 增加单元测试与 API 测试。

验收结果：删除 Job 后 Docker Desktop 中对应随机 Trial 镜像消失，基础镜像仍保留。

### 阶段二：运行结束自动清理

- 在 Pier 进程退出后清理 Trial 镜像标签。
- 写入清理审计 JSON。
- 支持三 Agent 并行运行的 cleanup lock。

验收结果：完成一次 7 题回归后不再新增长期残留的 Trial 镜像条目，再次运行仍命中 BuildKit 缓存。

### 阶段三：存储可视化与手动缓存维护

- 新增 storage/preview/cleanup API。
- Settings 增加 Docker 存储卡片。
- Diagnostics 增加镜像、缓存和孤儿资源告警。
- 提供带保留期的 Builder cache prune。

验收结果：用户能看到真实占用和可回收空间，并在 UI 中安全清理过期缓存。

### 阶段四：资源清单和孤儿恢复

- 每个 Job 写入 `docker-resources.json`。
- 支持识别历史孤儿镜像。
- 支持应用异常退出后的下次启动扫描。

验收结果：即使 UI 或 Pier 异常退出，也能精确恢复并清理本工具资源。

## 11. 验收标准

1. Run 完成、失败或取消后，相关 Trial 镜像标签能够被定向删除。
2. 删除 Run 时，Docker 清理发生在 Job artifacts 删除之前。
3. 共享 ECR 基础镜像和 `ubuntu:24.04` 默认保留。
4. 正常清理命令中不存在 `--force`、`docker system prune -a` 或无范围的 `docker image prune -a`。
5. Docker 不可用或清理失败不会改变已经产生的模型评测结果。
6. 三 Agent 并行模式下不会互相删除正在使用的镜像。
7. UI 显示 Docker 的实际总占用、可回收空间和 BuildKit 缓存，不累加逻辑镜像 Size。
8. 缓存清理必须先预览、二次确认，并在有活动 Run 时禁用。
9. 清理后再次运行相同任务，基础镜像无需重新下载，近期构建步骤能够继续命中缓存。
10. 所有清理操作都有结构化结果，且重复执行安全幂等。

## 12. 推荐默认配置

```json
{
  "docker_cleanup_after_run": true,
  "docker_cleanup_on_delete": true,
  "docker_cache_retention_hours": 168,
  "docker_cache_warning_gb": 20,
  "docker_auto_prune_build_cache": false
}
```

该默认配置优先解决 Docker Desktop 镜像列表污染，同时保留影响模型降智回归效率的基础层和近期构建缓存。全局缓存回收仍由用户在 Settings 页面明确触发。
