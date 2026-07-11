# Windows 下 Pier + Codex 跑 DeepSWE 任务笔记

这份笔记记录本次把 `pier run --agent codex` 在 Windows 上跑通的完整排障过程。当前场景是：Codex 需要从 Docker 容器里访问宿主机上的本地模型入口，宿主机入口是 `http://127.0.0.1:9887/v1`。

## 最终可用命令

在仓库根目录执行：

```powershell
pier.exe run -p .\tasks\adaptix-name-mapping-aliases --agent codex --model openai/gpt-5.5 --agent-env CODEX_FORCE_AUTH_JSON=true --agent-kwarg config_toml_file=.\codex-local-provider.toml -n 1
```

跑其他任务时，只替换任务路径：

```powershell
pier.exe run -p .\tasks\<task-id> --agent codex --model openai/gpt-5.5 --agent-env CODEX_FORCE_AUTH_JSON=true --agent-kwarg config_toml_file=.\codex-local-provider.toml -n 1
```

这条命令依赖仓库根目录的 `codex-local-provider.toml`：

```toml
model_provider = "local_proxy"

[model_providers.local_proxy]
name = "Local Proxy"
base_url = "http://host.docker.internal:9887/v1"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
```

## 改了哪些东西

### 1. 修复 Pier 生成代理脚本的 Windows 换行问题

修改的是本机 Pier 安装目录里的文件，不是仓库源码：

```text
C:\Users\bamboo2026\AppData\Roaming\uv\tools\datacurve-pier\Lib\site-packages\pier\environments\agent_setup.py
```

核心改动：

```python
(proxy_dir / "Dockerfile").write_text(..., newline="\n")
(proxy_dir / "start-squid.sh").write_text(..., newline="\n")
```

为什么要改：

- Windows 上 Python `write_text()` 默认可能写出 CRLF 换行。
- Pier 会生成一个 Linux 容器里执行的 `start-squid.sh`。
- 这个脚本如果是 CRLF，容器里的 Bash 会把 `\r` 当成命令字符。
- 结果是 Squid 代理启动失败，Docker Compose 报 `pier-egress-proxy` 容器退出。
- 最开始看到的 `RuntimeError` 就是这个原因。

验证命令：

```powershell
$py = Join-Path $env:APPDATA 'uv\tools\datacurve-pier\Scripts\python.exe'
& $py -c "from pathlib import Path; import tempfile; from pier.environments.agent_setup import squid_bootstrap_command; p=Path(tempfile.mkdtemp())/'start-squid.sh'; p.write_text(squid_bootstrap_command(), newline='\n'); b=p.read_bytes(); print('CRLF=', b'\r\n' in b, 'LF=', b'\n' in b)"
```

期望输出：

```text
CRLF= False LF= True
```

### 2. 允许 Pier 的 Squid 代理访问本机模型端口 `9887`

同样修改本机 Pier 安装目录里的文件：

```text
C:\Users\bamboo2026\AppData\Roaming\uv\tools\datacurve-pier\Lib\site-packages\pier\environments\agent_setup.py
```

生成的 Squid 配置里增加了 `9887`：

```text
acl SSL_ports port 443 9887
acl Safe_ports port 80 443 9887
```

为什么要改：

- Pier 给 agent 容器套了一层 egress proxy。
- 默认 Squid 只允许 `80` 和 `443`。
- 本机模型入口跑在 `9887`。
- 即使 URL 改成了 Docker 可访问的 `host.docker.internal:9887`，没有这个改动也可能被 Pier 代理层拦掉。

### 3. 修复 Pier 在 Windows 上读取 Codex 轨迹文件的编码问题

修改的是本机 Pier 安装目录里的文件：

```text
C:\Users\bamboo2026\AppData\Roaming\uv\tools\datacurve-pier\Lib\site-packages\pier\agents\installed\codex.py
```

核心改动：

```python
with open(session_file, "r", encoding="utf-8") as handle:
```

为什么要改：

- Windows 默认文本编码可能是 GBK。
- Codex 的 JSONL session 文件是 UTF-8。
- Pier 转换 Codex events 到 trajectory 时遇到过 `UnicodeDecodeError: 'gbk' codec can't decode...`。
- 这个不是最初网络失败的根因，但会影响后续结果解析和排错。

验证命令：

```powershell
$py = Join-Path $env:APPDATA 'uv\tools\datacurve-pier\Scripts\python.exe'
& $py -m py_compile "$env:APPDATA\uv\tools\datacurve-pier\Lib\site-packages\pier\agents\installed\codex.py" "$env:APPDATA\uv\tools\datacurve-pier\Lib\site-packages\pier\environments\agent_setup.py"
```

期望结果：无输出，退出码为 `0`。

### 4. 新增仓库内的 Codex provider 配置

新增文件：

```text
codex-local-provider.toml
```

为什么要加：

- 容器里不能用 `127.0.0.1:9887` 访问 Windows 宿主机。
- 必须用 `host.docker.internal:9887`。
- 单独传 `OPENAI_BASE_URL=http://host.docker.internal:9887/v1` 后，Codex 确实打到了正确宿主机地址。
- 但 Codex 默认先走 Responses WebSocket，访问的是 `ws://host.docker.internal:9887/v1/responses`。
- 本机模型入口对这个 WebSocket 请求返回了 `400 Bad Request`。
- Codex provider 配置支持 `supports_websockets = false`，可以禁用 WebSocket，改走普通 HTTP Responses 请求。

验证命令：

```powershell
$py = Join-Path $env:APPDATA 'uv\tools\datacurve-pier\Scripts\python.exe'
& $py -c "import toml; toml.load('codex-local-provider.toml'); print('toml ok')"
```

期望输出：

```text
toml ok
```

## 原先为什么跑不通

### 失败 1：还没进入 agent，就出现 `RuntimeError`

现象：

```text
Trials: 0
Exceptions: 1
RuntimeError
dependency failed to start: container ...-pier-egress-proxy-1 exited (1)
```

原因：

- Pier 生成的 `start-squid.sh` 是 Windows CRLF 换行。
- Linux 容器里的 Bash 解析失败。
- Squid egress proxy 容器启动失败。
- agent 容器还没来得及跑 Codex。

解决：

- 修改 Pier，让生成的 Docker proxy 文件强制使用 LF 换行。

### 失败 2：`ValueError: Model name is required`

现象：

```text
Trial failed: Model name is required
```

原因：

- `pier run --agent codex` 必须显式指定模型。
- 不加 `--model` 时，Pier 的 Codex agent 会直接报错。

解决：

```powershell
--model openai/gpt-5.5
```

### 失败 3：请求打到了 `api.openai.com`，返回 `401 Unauthorized`

`agent/codex.txt` 里的现象：

```text
unexpected status 401 Unauthorized: Incorrect API key provided
url: https://api.openai.com/v1/responses
```

原因：

- 那次运行没有正确使用本机模型入口。
- Codex 仍然在走默认 OpenAI endpoint。
- 另一次运行里传了 `OPENAI_API_KEY=local`，导致本机入口收到 `Bearer local`，也会被拒绝。

解决：

- 如果真实 token 在 `~\.codex\auth.json`，不要传 `OPENAI_API_KEY=local` 这种占位值。
- 使用 `--agent-env CODEX_FORCE_AUTH_JSON=true`，让 Pier 把宿主机上的 `auth.json` 上传进容器。

### 失败 4：URL 对了，但 WebSocket 返回 `400 Bad Request`

最新 `agent/codex.txt` 里的现象：

```text
failed to connect to websocket: HTTP error: 400 Bad Request
url: ws://host.docker.internal:9887/v1/responses
```

原因：

- 这时 URL 已经对了。
- `host.docker.internal` 已经能从容器访问 Windows 宿主机。
- 但本机模型入口不支持 Codex 的 Responses WebSocket transport。

解决：

- 新增 `codex-local-provider.toml`。
- 在 provider 里设置 `supports_websockets = false`。
- 通过 `--agent-kwarg config_toml_file=.\codex-local-provider.toml` 传给 Pier 的 Codex agent。

## 关键概念

### Docker 里的 `127.0.0.1` 不是 Windows 宿主机

在 Windows 宿主机上，本机入口是：

```text
http://127.0.0.1:9887/v1
```

但在 Docker 容器里，要访问 Windows 宿主机，需要用：

```text
http://host.docker.internal:9887/v1
```

如果容器里写 `127.0.0.1`，它指向的是容器自己。

### token 在 `auth.json` 时，不要乱传 `OPENAI_API_KEY`

真实 token 在这里：

```text
C:\Users\bamboo2026\.codex\auth.json
```

运行 Pier 时使用：

```powershell
--agent-env CODEX_FORCE_AUTH_JSON=true
```

不要再传：

```powershell
--agent-env OPENAI_API_KEY=local
```

否则 Codex 会把 `local` 当成 bearer token 发给本机入口。

### 一定要看最新 job 的日志

每次 Pier 运行都会生成一个新目录：

```text
jobs\YYYY-MM-DD__HH-MM-SS\
```

最重要的文件：

```text
jobs\<job>\result.json
jobs\<job>\job.log
jobs\<job>\<trial>\agent\codex.txt
jobs\<job>\<trial>\config.json
jobs\<job>\<trial>\docker-compose-egress-proxy.json
```

不要拿旧的 `codex.txt` 判断当前状态。旧日志可能还显示 `api.openai.com`，但新运行可能已经改成 `host.docker.internal`。

## 后续跑任务完整指南

### 1. 确认本机模型入口已启动

```powershell
Test-NetConnection 127.0.0.1 -Port 9887 | Select-Object ComputerName,RemotePort,TcpTestSucceeded
```

期望：

```text
TcpTestSucceeded : True
```

### 2. 确认宿主机 Codex 可用

在 Pier 外直接运行：

```powershell
codex
```

输入一个小 prompt，例如：

```text
hi
```

如果能正常回复，说明宿主机 Codex 认证可用。

### 3. 确认 provider 配置可解析

```powershell
$py = Join-Path $env:APPDATA 'uv\tools\datacurve-pier\Scripts\python.exe'
& $py -c "import toml; toml.load('codex-local-provider.toml'); print('toml ok')"
```

### 4. 跑单个任务

```powershell
pier.exe run -p .\tasks\adaptix-name-mapping-aliases --agent codex --model openai/gpt-5.5 --agent-env CODEX_FORCE_AUTH_JSON=true --agent-kwarg config_toml_file=.\codex-local-provider.toml -n 1
```

跑其他任务：

```powershell
pier.exe run -p .\tasks\<task-id> --agent codex --model openai/gpt-5.5 --agent-env CODEX_FORCE_AUTH_JSON=true --agent-kwarg config_toml_file=.\codex-local-provider.toml -n 1
```

### 5. 查看失败原因

找最新 job：

```powershell
Get-ChildItem .\jobs -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 1 FullName
```

看结果摘要：

```powershell
Get-Content .\jobs\<job>\result.json
```

看 Pier job 日志：

```powershell
Get-Content .\jobs\<job>\job.log
```

看 Codex 输出：

```powershell
Get-Content .\jobs\<job>\<trial>\agent\codex.txt
```

### 6. 常见错误对照

```text
RuntimeError + pier-egress-proxy exited
```

大概率是 Pier egress proxy 启动失败。检查 LF 换行补丁和 Docker 日志。

```text
Model name is required
```

命令里缺 `--model openai/gpt-5.5`。

```text
401 Unauthorized + api.openai.com
```

说明本机 provider 配置没有生效，或者认证方式不对。确认命令包含：

```powershell
--agent-env CODEX_FORCE_AUTH_JSON=true --agent-kwarg config_toml_file=.\codex-local-provider.toml
```

```text
401 Unauthorized + host.docker.internal
```

请求已经到了本机入口，但 `auth.json` 里的 token 被本机入口拒绝。

```text
400 Bad Request + ws://host.docker.internal:9887/v1/responses
```

Codex 还在走 WebSocket。确认 `codex-local-provider.toml` 包含：

```toml
supports_websockets = false
```

并确认 Pier 命令传了这个配置文件。

## 关于本机 Pier 补丁的注意事项

这些路径下的改动是本机安装补丁，不是仓库文件：

```text
C:\Users\bamboo2026\AppData\Roaming\uv\tools\datacurve-pier\...
```

如果以后升级或重装 `datacurve-pier`，这些改动可能被覆盖。升级后要重新检查：

1. 生成的 `start-squid.sh` 是否是 LF 换行。
2. 生成的 Squid 配置是否允许端口 `9887`。
3. Pier 是否用 UTF-8 读取 Codex JSONL session。

## 快速检查清单

- `Test-NetConnection 127.0.0.1 -Port 9887` 返回 `True`。
- 宿主机直接运行 `codex` 可以正常回复。
- `codex-local-provider.toml` 存在并且能解析。
- Pier 命令包含 `CODEX_FORCE_AUTH_JSON=true`。
- Pier 命令包含 `config_toml_file=.\codex-local-provider.toml`。
- 最新 `agent\codex.txt` 不再出现 `api.openai.com`。
- 最新 `agent\codex.txt` 不再出现 `ws://host.docker.internal:9887`，因为 `supports_websockets = false` 应该让它走普通 HTTP。