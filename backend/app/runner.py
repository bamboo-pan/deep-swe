import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
import psutil
from sqlalchemy import select, text
from .config import settings
from .database import SessionLocal
from .docker_cleanup import (
    DockerCleanupPolicy, cleanup_job_resources, docker_available,
    sanitize_compose_project_name,
)
from .models import ACTIVE_STATES, TERMINAL_STATES, Run
from .preferences import credential_path, current_secrets, get_preferences
from .pier_retry_patch.networking import trial_network_subnets
from .results import (
    _json as read_json, aggregate_trial_results, estimate_cost, jobs_root_for,
    pier_trial_prefix, run_code, run_detail as parsed_run_detail,
    run_task_progress, run_trial_progress, trial_folder,
)
from .scheduler import (
    clear_run_queue, enqueue_retry_trials, enqueue_runs, queue_admission,
    queue_database_path, queue_status, requested_trial_count,
)
from .schemas import MAX_PARALLEL_TASKS, RunBatchDraft, RunDraft
from .security import read_credential, redact

_processes: dict[int, subprocess.Popen] = {}
_retrying: dict[int, str] = {}
_cancel_requested: set[int] = set()
_lock = threading.Lock()
_creation_lock = threading.Lock()
_queue_patch_verify_lock = threading.Lock()
_queue_patch_verified = False
# 取消与结果落库都要做「读状态→写状态」，用同一把锁避免最后写者赢
_state_lock = threading.Lock()

# 2026-07-12 试运行事故（单 Trial 烧掉约 $225）后引入的用量护栏，见 LESSONS-LEARNED.md §5
GUARD_CHECK_INTERVAL_SEC = 20
# mini（cost_limit/step_limit）与 claude-code（max_budget_usd/max_turns）有原生限额，
# 兜底留出裕量避免和原生限额抢跑（原生触发是优雅停止，verifier 仍可产出 reward）；
# codex 没有任何原生限额，兜底就是唯一防线，不留裕量
TRIAL_GUARD_COST_MARGIN = 1.5
TRIAL_GUARD_STEP_MARGIN = 30
NATIVE_LIMIT_AGENTS = ("mini-swe-agent", "claude-code")
RUNAWAY_AGENT_LOG_MB = 30       # 正常 Trial 的 agent 日志在个位数 MB；事故 Trial 43 分钟写了 278MB
RUNAWAY_SUBAGENT_FILES = 60     # 禁用 Task/Agent 工具后应恒为 0；事故 Trial 产生了 5374 个会话文件
# 首档保持秒级应付瞬时抖动（apt 偶发 502），后段拉到分钟级覆盖 GitHub TLS
# 封锁这类持续数分钟的故障（run-000001 四个镜像构建失败的教训）
INFRASTRUCTURE_RETRY_DELAYS_SEC = (5, 30, 120, 300, 600, 900)
DOCKER_CONNECTIVITY_IMAGE = "alpine:3.20"
DOCKER_CONNECTIVITY_TIMEOUT_SEC = 8
ATOMIC_REPLACE_RETRY_DELAYS_SEC = (0.01, 0.025, 0.05, 0.1, 0.2, 0.4, 0.8, 1.0)

def _pier_retry_args(enabled: bool, max_retries: int) -> list[str]:
    """Retry trial-level infrastructure failures without retrying verifier/result errors."""
    if not enabled or max_retries <= 0:
        return ["--max-retries", "0"]
    return [
        "--max-retries", str(max_retries),
        "--retry-include", "TransientAgentInfrastructureError",
    ]

def _docker_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname in {"127.0.0.1", "localhost"}:
        host_port = parsed.netloc.rsplit("@", 1)[-1]
        port = f":{parsed.port}" if parsed.port else ""
        return url.replace(host_port, f"host.docker.internal{port}", 1)
    return url

def _anthropic_url(url: str) -> str:
    mapped = _docker_url(url).rstrip("/")
    return mapped[:-3] if mapped.endswith("/v1") else mapped

def _docker_proxy_connectivity(url: str) -> None:
    """Verify the proxy from the same dual-network topology used by Pier."""
    parsed = urlparse(_docker_url(url))
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        raise RuntimeError("模型代理地址无效，无法执行 Docker 连通性检查")
    docker = shutil.which("docker") or "docker"
    identity = f"connectivity-{uuid.uuid4().hex}"
    internal_subnet, external_subnet = trial_network_subnets(identity)
    suffix = uuid.uuid4().hex[:10]
    internal_network = f"deepswe-check-int-{suffix}"
    external_network = f"deepswe-check-ext-{suffix}"
    container = f"deepswe-check-{suffix}"
    created_networks: list[str] = []
    container_created = False

    def checked(args: list[str], timeout: int = 45) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                [docker, *args], capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Docker 内模型代理连通性检查超时：{host}:{port}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"无法启动 Docker 连通性检查：{exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            reason = detail[-1][:500] if detail else "Docker 命令失败"
            raise RuntimeError(reason)
        return result

    try:
        checked(["network", "create", "--internal", "--subnet", internal_subnet, internal_network])
        created_networks.append(internal_network)
        checked(["network", "create", "--subnet", external_subnet, external_network])
        created_networks.append(external_network)
        checked([
            "create", "--name", container, "--network", internal_network,
            "--pull=missing", DOCKER_CONNECTIVITY_IMAGE,
            "sh", "-c", 'nc -z -w "$3" "$1" "$2"',
            "deepswe-connectivity", host, str(port), str(DOCKER_CONNECTIVITY_TIMEOUT_SEC),
        ])
        container_created = True
        checked(["network", "connect", external_network, container])
        try:
            checked(["start", "-a", container], timeout=DOCKER_CONNECTIVITY_TIMEOUT_SEC + 15)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Docker 容器无法连接模型代理 {host}:{port}：{exc}"
            ) from exc
    finally:
        if container_created:
            subprocess.run([docker, "rm", "-f", container], capture_output=True, timeout=20)
        for network in reversed(created_networks):
            subprocess.run([docker, "network", "rm", network], capture_output=True, timeout=20)

def _write_secret_auth(token: str) -> tuple[Path, Path]:
    folder = Path(tempfile.mkdtemp(prefix="deepswe-ui-"))
    auth = folder / "auth.json"
    auth.write_text(json.dumps({"OPENAI_API_KEY": token}), encoding="utf-8", newline="\n")
    if os.name == "nt":
        account = f"{os.environ.get('USERDOMAIN')}\\{os.environ.get('USERNAME')}"
        subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F"], capture_output=True)
    return folder, auth

def _codex_config(
    base_url: str,
    folder: Path,
    model: str,
    effort: str,
    request_max_retries: int = 6,
    stream_max_retries: int = 6,
    stream_idle_timeout_seconds: int = 600,
) -> Path:
    path = folder / "codex-provider.toml"
    path.write_text(
        f'model = "{model}"\nmodel_reasoning_effort = "{effort}"\nmodel_provider = "local_proxy"\n\n'
        '[model_providers.local_proxy]\nname = "Local Proxy"\nbase_url = "' + _docker_url(base_url) + '"\n'
        'wire_api = "responses"\nrequires_openai_auth = true\nsupports_websockets = false\n'
        f'request_max_retries = {request_max_retries}\n'
        f'stream_max_retries = {stream_max_retries}\n'
        f'stream_idle_timeout_ms = {stream_idle_timeout_seconds * 1000}\n',
        encoding="utf-8", newline="\n")
    return path

def _mini_limits_config(folder: Path, step_limit: int, reasoning_effort: str) -> Path:
    """mini-swe-agent 的 step_limit 是确定性护栏；cost_limit 走 --agent-kwarg 单独传，
    自建网关模型 litellm 算不出成本时恒为 0、不会触发，由守护线程按 token 估算兜底。
    pier adapter 会把该文件内容写进容器再以 -c 追加。

    LiteLLM Responses 的字符串 reasoning_effort 映射不认识 provider 扩展档位 max，
    会在 drop_params=true 时静默丢弃。直接配置原生 reasoning.effort，确保请求体透传。"""
    path = folder / "mini-limits.yaml"
    # mini-swe-agent/litellm otherwise creates a fresh prompt_cache_key for
    # every turn.  Keep one key for the whole trial so growing conversation
    # prefixes can hit the provider cache.
    cache_key = f"deepswe-{uuid.uuid4().hex}"
    path.write_text(
        f"agent:\n  step_limit: {step_limit}\n"
        "model:\n  model_kwargs:\n"
        f"    reasoning:\n      effort: {json.dumps(reasoning_effort)}\n"
        f"    prompt_cache_key: {cache_key}\n"
        "    prompt_cache_retention: 24h\n",
        encoding="utf-8", newline="\n")
    return path

def _reasoning_effort_adapter(agent: str, effort: str) -> str:
    if agent == "codex":
        return f"model_reasoning_effort={effort}"
    if agent == "mini-swe-agent":
        return f"reasoning.effort={effort}"
    if agent == "claude-code" and effort == "none":
        return "thinking=disabled"
    return f"reasoning_effort={effort}"

def _pier_version() -> str | None:
    executable = shutil.which("pier")
    if not executable:
        return None
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10)
        value = (result.stdout or result.stderr).strip().splitlines()
        return value[0][:60] if value else None
    except (OSError, subprocess.TimeoutExpired):
        return None

def _declared_timeouts(tasks: list[str]) -> tuple[float, float]:
    """pier 的 timeout multiplier 乘的是每个任务 task.toml 声明的超时；
    取所选任务声明值的最大值做除数，保证任何任务的有效超时不超过用户请求值。"""
    agent_values, verifier_values = [], []
    for task in tasks:
        try:
            data = tomllib.loads((settings.tasks_dir / task / "task.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        agent_timeout = (data.get("agent") or {}).get("timeout_sec")
        verifier_timeout = (data.get("verifier") or {}).get("timeout_sec")
        if isinstance(agent_timeout, (int, float)) and agent_timeout > 0:
            agent_values.append(float(agent_timeout))
        if isinstance(verifier_timeout, (int, float)) and verifier_timeout > 0:
            verifier_values.append(float(verifier_timeout))
    return max(agent_values, default=5400.0), max(verifier_values, default=1800.0)

def create_runs_with_admission(draft: RunBatchDraft) -> tuple[list[Run], dict]:
    preferences = get_preferences()
    max_parallel_tasks = int(preferences["max_parallel_tasks"])
    infrastructure_max_retries = int(preferences["infrastructure_max_retries"])
    requested = requested_trial_count(
        draft.tasks, draft.attempts_per_task, len(draft.agents)
    )
    with _creation_lock:
        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            admission = queue_admission(
                requested, db=db, limit=max_parallel_tasks
            )
            runs = []
            for agent in draft.agents:
                mapping = _reasoning_effort_adapter(agent, draft.reasoning_effort)
                run = Run(
                    status="queued", job_name=f"pending-{uuid.uuid4().hex}",
                    jobs_dir=str(preferences["jobs_dir"]), agent=agent, model=draft.model,
                    reasoning_effort=draft.reasoning_effort, reasoning_effort_adapter=mapping,
                    reasoning_effort_effective=None,  # 有效值只能来自运行后的观测，创建时未知
                    tasks_json=json.dumps(draft.tasks), attempts_per_task=draft.attempts_per_task,
                    concurrency=max_parallel_tasks,
                    agent_timeout_seconds=int(preferences["agent_timeout_seconds"]),
                    verifier_timeout_seconds=int(preferences["verifier_timeout_seconds"]),
                    retry_infrastructure_errors=infrastructure_max_retries > 0,
                    infrastructure_max_retries=infrastructure_max_retries,
                    agent_max_steps=int(preferences["agent_max_steps"]),
                    codex_request_max_retries=draft.codex_request_max_retries,
                    codex_stream_max_retries=draft.codex_stream_max_retries,
                    codex_stream_idle_timeout_seconds=draft.codex_stream_idle_timeout_seconds,
                    verification=draft.verification, service_tier=draft.service_tier,
                )
                db.add(run)
                db.flush()
                run.job_name = f"run-{run.id:06d}-{agent}"
                runs.append(run)
            enqueue_runs(db, runs, draft.tasks, draft.attempts_per_task)
            db.commit()
            for run in runs:
                db.refresh(run)
            run_ids = [run.id for run in runs]
    for run_id in run_ids:
        try:
            threading.Thread(target=_execute, args=(run_id,), daemon=True).start()
        except Exception as exc:
            clear_run_queue(run_id)
            with SessionLocal() as db:
                run = db.get(Run, run_id)
                if run:
                    run.status = "failed"
                    run.error = f"无法启动 Run 执行线程：{exc}"
                    run.finished_at = datetime.now(UTC)
                    db.commit()
    with SessionLocal() as db:
        return [db.get(Run, run_id) for run_id in run_ids], admission

def create_runs(draft: RunBatchDraft) -> list[Run]:
    runs, _admission = create_runs_with_admission(draft)
    return runs

def create_run(draft: RunDraft) -> Run:
    batch = RunBatchDraft(
        agents=[draft.agent],
        **draft.model_dump(exclude={"agent"}),
    )
    return create_runs(batch)[0]

def _crlf_scripts(tasks: list[str]) -> list[str]:
    bad = []
    for task in tasks:
        for script in sorted((settings.tasks_dir / task).rglob("*.sh")):
            try:
                if b"\r" in script.read_bytes():
                    bad.append(script.relative_to(settings.tasks_dir).as_posix())
            except OSError:
                continue
    return bad

def _preflight(tasks: list[str], proxy_url: str | None = None) -> None:
    missing = [task for task in tasks if not (settings.tasks_dir / task).is_dir()]
    if missing:
        raise RuntimeError(f"任务目录缺失: {', '.join(missing)}")
    crlf = _crlf_scripts(tasks)
    if crlf:
        # CRLF 的 shebang 在容器内变成 /bin/bash\r，verifier 必然拿不到 reward，agent 费用全部报废
        shown = ", ".join(crlf[:5]) + (f" 等 {len(crlf)} 个" if len(crlf) > 5 else "")
        raise RuntimeError(f"任务脚本为 CRLF 行尾，容器内无法执行: {shown}；请转为 LF 后重试")
    ok, message = docker_available()
    if not ok:
        raise RuntimeError(f"Docker 不可用: {message}")
    if proxy_url:
        _docker_proxy_connectivity(proxy_url)

def _tail_text(path: Path, limit: int = 200_000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(handle.tell() - limit, 0))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""

def _network_failure_summary(text: str) -> str | None:
    if not text:
        return None
    connection = re.search(r"Connection to\s+([^\s<]+)\s+failed", text, re.IGNORECASE)
    if connection or "ERR_CONNECT_FAIL" in text:
        host = connection.group(1) if connection else "模型代理"
        reason_match = re.search(
            r"The system returned:\s*<i>\([^)]*\)\s*([^<]+)", text, re.IGNORECASE
        )
        reason = reason_match.group(1).strip() if reason_match else "连接超时或被拒绝"
        return f"模型代理连接失败：{host}（Docker/Squid：{reason}）"
    status = re.search(
        r"unexpected status\s+(429|5\d\d)(?:\s+([^:\n<]+))?", text, re.IGNORECASE
    )
    if status:
        label = (status.group(2) or "上游服务错误").strip()
        return f"模型代理请求失败：HTTP {status.group(1)} {label}"
    status = re.search(r'"error_status"\s*:\s*(429|5\d\d)', text)
    if status:
        return f"模型代理请求失败：HTTP {status.group(1)} server_error"
    lowered = text.lower()
    if "api error: the operation timed out" in lowered:
        return "模型 API 请求超时"
    if re.search(r"\beof\b", lowered) and re.search(
        r"failed to (?:do request|resolve source metadata|fetch|pull)"
        r"|\b(?:head|get)\s+\"https?://[^\"]+"
        r"|\b(?:registry|manifest|source metadata)\b",
        lowered,
    ):
        return "Docker/镜像仓库连接意外中断（EOF）"
    error_context = re.compile(
        r"(?:\b[a-z]*(?:connection|protocol|transport|read|write|timeout|network|api)[a-z]*error\b"
        r"|\b(?:exception|traceback|failed|failure)\b)",
        re.IGNORECASE,
    )
    contextual_markers = (
        ("connection timed out", "模型代理连接超时"),
        ("connection refused", "模型代理拒绝连接"),
        ("connection reset", "模型代理连接被重置"),
        ("unexpected eof", "模型代理连接意外中断"),
    )
    for line in text.splitlines():
        lowered_line = line.lower()
        if not error_context.search(line):
            continue
        for marker, message in contextual_markers:
            if marker in lowered_line:
                return message
    return None

def _run_failure_summary(job_dir: Path, supervisor_log: Path | None = None) -> str | None:
    """Extract an actionable failure instead of exposing only Pier's exit code."""
    if job_dir.is_dir():
        for folder in sorted(path for path in job_dir.iterdir() if path.is_dir()):
            data = read_json(folder / "result.json")
            exception = data.get("exception_info") or {}
            message = exception.get("exception_message")
            summary = _network_failure_summary(str(message or ""))
            if summary:
                return summary
            if exception.get("exception_type") in {
                None,
                "NonZeroAgentExitCodeError",
                "TransientAgentInfrastructureError",
            }:
                for path in sorted((folder / "agent").glob("*.txt")):
                    summary = _network_failure_summary(_tail_text(path))
                    if summary:
                        return summary
        for path in (job_dir / "job.log",):
            summary = _network_failure_summary(_tail_text(path))
            if summary:
                return summary
        for folder in sorted(path for path in job_dir.iterdir() if path.is_dir()):
            exception = (read_json(folder / "result.json").get("exception_info") or {})
            message = exception.get("exception_message")
            if message:
                failure_type = exception.get("exception_type") or "TrialError"
                return f"{failure_type}: {str(message).strip()[:3500]}"
    if supervisor_log:
        text = _tail_text(supervisor_log)
        summary = _network_failure_summary(text)
        if summary:
            return summary
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return f"Pier: {lines[-1][:3500]}"
    return None

def _completed_trials_cost(job_dir: Path, service_tier: str, model: str | None = None) -> float:
    """累计已落盘 Trial 的费用；pier 报告值优先，缺失时按 token 估算。"""
    total = 0.0
    if not job_dir.is_dir():
        return total
    for trial in job_dir.iterdir():
        if not trial.is_dir():
            continue
        agent_result = read_json(trial / "result.json").get("agent_result") or {}
        cost = agent_result.get("cost_usd")
        if not isinstance(cost, (int, float)):
            cost = estimate_cost(agent_result.get("n_input_tokens"), agent_result.get("n_cache_tokens"),
                                 agent_result.get("n_output_tokens"), service_tier, model)
        if isinstance(cost, (int, float)):
            total += cost
    return total

def _runaway_reason(job_dir: Path) -> str | None:
    """进行中 Trial 的失控体征：agent 日志体量异常或 subagent 会话数异常。"""
    if not job_dir.is_dir():
        return None
    for trial in job_dir.iterdir():
        agent_dir = trial / "agent"
        if not agent_dir.is_dir():
            continue
        for log in agent_dir.glob("*.txt"):
            try:
                size_mb = log.stat().st_size // (1024 * 1024)
            except OSError:
                continue
            if size_mb >= RUNAWAY_AGENT_LOG_MB:
                return f"Trial {trial.name} 的 agent 日志已达 {size_mb}MB（阈值 {RUNAWAY_AGENT_LOG_MB}MB）"
        for pattern in ("sessions/projects/*/subagents", "sessions/projects/*/*/subagents"):
            for subagents in agent_dir.glob(pattern):
                try:
                    count = sum(1 for _ in subagents.iterdir())
                except OSError:
                    continue
                if count >= RUNAWAY_SUBAGENT_FILES:
                    return f"Trial {trial.name} 已产生 {count} 个 subagent 会话（阈值 {RUNAWAY_SUBAGENT_FILES}）"
    return None

def _terminate_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

def _trial_usage(trial: Path, service_tier: str, model: str | None = None) -> tuple[float | None, int | None]:
    """进行中 Trial 的实时费用与步数；已落盘（有 result.json）的返回空，由 Run 级累计覆盖。
    容器内 agent 会在运行期间持续更新 ATIF 格式的 agent/trajectory.json（三个 agent 通用）；
    cost_usd 缺失（自建网关无价格表）时按 token 估算，mini 原生 trajectory 作最后兜底。"""
    if (trial / "result.json").exists():
        return None, None
    data = read_json(trial / "agent" / "trajectory.json")
    final = data.get("final_metrics") or {}
    steps = data.get("steps") or []
    total_steps = final.get("total_steps")
    n_steps = total_steps if isinstance(total_steps, int) else (len(steps) or None)
    cost = final.get("total_cost_usd")
    if not isinstance(cost, (int, float)):
        cost = estimate_cost(final.get("total_prompt_tokens"), final.get("total_cached_tokens"),
                             final.get("total_completion_tokens"), service_tier, model)
    if cost is None:
        stats = ((read_json(trial / "agent" / "mini-swe-agent.trajectory.json").get("info") or {})
                 .get("model_stats") or {})
        raw = stats.get("instance_cost")
        if isinstance(raw, (int, float)) and raw > 0:
            cost = float(raw)
        if n_steps is None and isinstance(stats.get("api_calls"), int):
            n_steps = stats["api_calls"]
    return (float(cost) if isinstance(cost, (int, float)) else None), n_steps

def _terminate_trial(trial: Path, reason: str) -> bool:
    """只掐超限 Trial 的容器（compose project 即小写 trial 名），
    pier 会把该 Trial 记为失败并继续其余任务；掐失败则下个周期重试。"""
    docker = shutil.which("docker") or "docker"
    try:
        listed = subprocess.run(
            [docker, "ps", "-q", "--filter", f"label=com.docker.compose.project={trial.name.lower()}"],
            capture_output=True, text=True, timeout=20)
        containers = listed.stdout.split()
        if listed.returncode != 0 or not containers:
            return False
        killed = subprocess.run([docker, "kill", *containers], capture_output=True, timeout=60)
        if killed.returncode != 0:
            return False
    except (OSError, subprocess.TimeoutExpired):
        return False
    try:
        (trial / "guard.json").write_text(json.dumps(
            {"reason": f"用量护栏终止该 Trial：{reason}",
             "terminated_at": datetime.now(UTC).isoformat()}, ensure_ascii=False),
            encoding="utf-8", newline="\n")
    except OSError:
        pass
    return True

def _wait_with_guard(proc: subprocess.Popen, job_dir: Path, service_tier: str,
                     agent: str, max_steps: int, base_cost_usd: float = 0.0,
                     model: str | None = None) -> str | None:
    """等待 pier 结束，期间周期核查用量。单 Trial 超限只掐该 Trial 的容器，
    其余任务继续；Run 级累计超限或失控体征才终止整个进程树。"""
    prefs = get_preferences()
    def _budget(key: str) -> float:
        try:
            return float(prefs.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0
    run_budget, trial_budget = _budget("run_budget_usd"), _budget("trial_budget_usd")
    native = agent in NATIVE_LIMIT_AGENTS
    trial_cost_line = trial_budget * (TRIAL_GUARD_COST_MARGIN if native else 1.0)
    trial_step_line = max_steps + (TRIAL_GUARD_STEP_MARGIN if native else 0)
    terminated: set[str] = set()
    while True:
        try:
            proc.wait(timeout=GUARD_CHECK_INTERVAL_SEC)
            return None
        except subprocess.TimeoutExpired:
            pass
        inflight_cost = 0.0
        if job_dir.is_dir():
            for trial in job_dir.iterdir():
                if not trial.is_dir() or trial.name in terminated:
                    continue
                cost, steps = _trial_usage(trial, service_tier, model)
                inflight_cost += cost or 0.0
                trial_reason = None
                if trial_budget > 0 and cost is not None and cost >= trial_cost_line:
                    trial_reason = f"费用 ${cost:.2f} 达到单 Trial 上限 ${trial_budget:.2f}"
                elif max_steps > 0 and steps is not None and steps >= trial_step_line:
                    trial_reason = f"已执行 {steps} 步，达到最大步数 {max_steps}"
                if trial_reason and _terminate_trial(trial, trial_reason):
                    terminated.add(trial.name)
        reason = _runaway_reason(job_dir)
        if not reason and run_budget > 0:
            spent = base_cost_usd + _completed_trials_cost(job_dir, service_tier, model) + inflight_cost
            if spent >= run_budget:
                reason = f"累计费用 ${spent:.2f}（含进行中 Trial）达到 Run 预算上限 ${run_budget:.2f}"
        if reason:
            _terminate_tree(proc.pid)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            return f"用量护栏自动终止：{reason}"

def _pier_process_env(
    run_id: int,
    verifier_infrastructure_max_retries: int = 0,
    global_queue_limit: int | None = None,
) -> dict[str, str]:
    process_env = os.environ.copy()
    # Windows 的默认 locale 可能无法读取 UTF-8 trajectory 或输出 Unicode 状态字符。
    process_env["PYTHONUTF8"] = "1"
    retry_patch_dir = Path(__file__).with_name("pier_retry_patch")
    process_env["PYTHONPATH"] = os.pathsep.join(
        [str(retry_patch_dir), process_env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    process_env["DEEPSWE_PIER_RETRY_DELAYS"] = ",".join(
        str(delay) for delay in INFRASTRUCTURE_RETRY_DELAYS_SEC
    )
    process_env["DEEPSWE_VERIFIER_INFRA_MAX_RETRIES"] = str(
        max(int(verifier_infrastructure_max_retries), 0)
    )
    process_env["DEEPSWE_GLOBAL_QUEUE_DB"] = str(queue_database_path())
    process_env["DEEPSWE_RUN_ID"] = str(run_id)
    process_env["DEEPSWE_GLOBAL_QUEUE_LIMIT"] = str(
        max(
            int(
                global_queue_limit
                if global_queue_limit is not None
                else get_preferences()["max_parallel_tasks"]
            ),
            1,
        )
    )
    return process_env

def _verify_global_queue_patch(run_id: int) -> None:
    global _queue_patch_verified
    if _queue_patch_verified:
        return
    with _queue_patch_verify_lock:
        if _queue_patch_verified:
            return
        executable = shutil.which("pier")
        if not executable:
            raise RuntimeError("未找到 Pier，无法验证全局 Trial 队列补丁")
        process_env = _pier_process_env(run_id)
        process_env["DEEPSWE_VERIFY_GLOBAL_QUEUE_PATCH"] = "1"
        result = subprocess.run(
            [executable, "--version"],
            cwd=settings.tasks_dir.parent,
            env=process_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 or "DEEPSWE_GLOBAL_QUEUE_PATCH_OK" not in result.stdout:
            detail = (result.stderr or result.stdout or "Pier 未返回补丁握手标记").strip()
            raise RuntimeError(f"全局 Trial 队列补丁验证失败：{detail[:1000]}")
        _queue_patch_verified = True

def _execute(run_id: int):
    secret_dir = None
    job_name = None
    jobs_root = None
    try:
        with SessionLocal() as db:
            run = db.get(Run, run_id); run.status = "preflight"; run.pier_version = _pier_version(); db.commit()
            tasks, agent, model, effort, attempts, concurrency, job_name = json.loads(run.tasks_json), run.agent, run.model, run.reasoning_effort, run.attempts_per_task, run.concurrency, run.job_name
            service_tier = run.service_tier
            jobs_root = jobs_root_for(run)
        _verify_global_queue_patch(run_id)
        cred = read_credential(credential_path())
        _preflight(tasks, cred.url)
        secret_dir, auth = _write_secret_auth(cred.token)
        command_model = f"openai/{model}" if agent == "mini-swe-agent" and "/" not in model else model
        agent_divisor, verifier_divisor = _declared_timeouts(tasks)
        local_concurrency = min(MAX_PARALLEL_TASKS, len(tasks) * attempts)
        args = [shutil.which("pier") or "pier", "run", "-p", str(settings.tasks_dir), "--agent", agent, "--model", command_model, "-n", str(local_concurrency), "-k", str(attempts), "-y", "--job-name", job_name, "--jobs-dir", str(jobs_root), "--agent-timeout-multiplier", str(run.agent_timeout_seconds / agent_divisor), "--verifier-timeout-multiplier", str(run.verifier_timeout_seconds / verifier_divisor)]
        args += _pier_retry_args(
            run.retry_infrastructure_errors, run.infrastructure_max_retries
        )
        if not run.verification:
            args.append("--disable-verification")
        for task in tasks: args += ["-i", task]
        process_env = _pier_process_env(
            run_id,
            run.infrastructure_max_retries
            if run.retry_infrastructure_errors else 0,
        )
        try:
            trial_budget = float(get_preferences().get("trial_budget_usd") or 0)
        except (TypeError, ValueError):
            trial_budget = 0.0
        if agent == "codex":
            config = _codex_config(
                cred.url, secret_dir, model, effort,
                request_max_retries=run.codex_request_max_retries,
                stream_max_retries=run.codex_stream_max_retries,
                stream_idle_timeout_seconds=run.codex_stream_idle_timeout_seconds,
            )
            args += ["--agent-env", f"CODEX_AUTH_JSON_PATH={auth}", "--agent-kwarg", f"config_toml_file={config}", "--agent-kwarg", f"reasoning_effort={effort}"]
        elif agent == "mini-swe-agent":
            process_env.update({"OPENAI_API_KEY": cred.token, "OPENAI_BASE_URL": _docker_url(cred.url)})
            limits = _mini_limits_config(secret_dir, run.agent_max_steps, effort)
            # litellm_response（Responses API 桥）顶层 import litellm.proxy，
            # 需要完整 proxy extras（fastapi/orjson/pyjwt 等），agent 容器默认没装
            args += ["--agent-kwarg", f"config_file={limits}",
                     "--agent-kwarg", 'extra_python_packages=["litellm[proxy]"]']
            if trial_budget > 0:
                # mini 原生单 Trial 费用限额（agent.cost_limit，0 = 禁用），到限优雅停止
                args += ["--agent-kwarg", f"cost_limit={trial_budget}"]
        elif agent == "claude-code":
            process_env.update({"ANTHROPIC_API_KEY": cred.token, "ANTHROPIC_BASE_URL": _anthropic_url(cred.url)})
            # 覆盖 pier 默认的 disallowed_tools=EnterPlanMode，追加 Task/Agent 禁用容器内 subagent：
            # 2026-07-12 事故中主 agent 43 分钟 spawn 2687 个 subagent（6663 万输入 token），
            # max_turns 只约束主对话轮数，对 subagent 无效
            args += ["--agent-kwarg", f"max_turns={run.agent_max_steps}", "--agent-kwarg", "disallowed_tools=EnterPlanMode,Task,Agent"]
            if trial_budget > 0:
                # claude-code 原生单 Trial 费用限额（--max-budget-usd），到限优雅停止
                args += ["--agent-kwarg", f"max_budget_usd={trial_budget}"]
            if effort == "none":
                args += ["--agent-kwarg", "thinking=disabled"]
            else:
                args += ["--agent-kwarg", f"reasoning_effort={effort}"]
        else:
            raise ValueError(f"不支持的 agent: {agent}")
        jobs_root.mkdir(parents=True, exist_ok=True)
        log_path = jobs_root / f"{job_name}.supervisor.log"
        with SessionLocal() as db:
            queued_run = db.get(Run, run_id)
            if queued_run and queued_run.status not in TERMINAL_STATES:
                queued_run.status = "queued"
                db.commit()
        with log_path.open("w", encoding="utf-8") as log:
            with _lock:
                if run_id in _cancel_requested:
                    return
                proc = subprocess.Popen(
                    args, cwd=settings.tasks_dir.parent, env=process_env, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                    start_new_session=os.name != "nt")
                _processes[run_id] = proc
            with SessionLocal() as db:
                run = db.get(Run, run_id); run.pid = proc.pid; db.commit()
            guard_error = _wait_with_guard(proc, jobs_root / job_name, service_tier, agent, run.agent_max_steps, model=model)
        if guard_error:
            with _state_lock, SessionLocal() as db:
                run = db.get(Run, run_id)
                if run and run.status not in TERMINAL_STATES:
                    run.status = "cancelled"; run.error = guard_error; run.finished_at = datetime.now(UTC)
                    db.commit()
        _sync_result(run_id, proc.returncode)
    except Exception as exc:
        with _state_lock, SessionLocal() as db:
            run = db.get(Run, run_id)
            if run and run.status not in TERMINAL_STATES:
                run.status = "failed"; run.error = redact(str(exc), current_secrets()); run.finished_at = datetime.now(UTC)
                db.commit()
    finally:
        with _lock:
            _processes.pop(run_id, None)
            _cancel_requested.discard(run_id)
        try:
            clear_run_queue(run_id)
        except Exception:
            pass
        if secret_dir: shutil.rmtree(secret_dir, ignore_errors=True)
        _cleanup_after_run(run_id, job_name, jobs_root)

def _valid_retry_batch_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None

def retry_job_name(job_name: str, batch_id: str | None = None) -> str:
    base = f"retry-{job_name}"
    if batch_id is None:
        return base
    if not _valid_retry_batch_id(batch_id):
        raise ValueError("Invalid retry batch id")
    return f"{base}-{batch_id[:12]}"

def _matches_retry_job_name(job_name: str, value: str) -> bool:
    base = retry_job_name(job_name)
    return value == base or re.fullmatch(
        rf"{re.escape(base)}-[0-9a-f]{{12}}", value
    ) is not None

def retry_job_names(jobs_root: Path, job_name: str) -> list[str]:
    names = {retry_job_name(job_name)}
    try:
        entries = list(jobs_root.resolve().iterdir())
    except OSError:
        return sorted(names)
    for path in entries:
        candidate = path.name
        for suffix in (".supervisor.log", ".docker-cleanup.json"):
            if candidate.endswith(suffix):
                candidate = candidate.removesuffix(suffix)
                break
        if _matches_retry_job_name(job_name, candidate):
            names.add(candidate)
    return sorted(names)

def retry_job_dirs(jobs_root: Path, job_name: str) -> list[Path]:
    root = jobs_root.resolve()
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    return sorted(
        (
            path
            for path in entries
            if path.is_dir()
            and path.resolve().parent == root
            and _matches_retry_job_name(job_name, path.name)
        ),
        key=lambda path: path.name,
    )

def _retry_dir(jobs_root: Path, job_name: str, batch_id: str | None = None) -> Path:
    root = jobs_root.resolve()
    target = (jobs_root / retry_job_name(job_name, batch_id)).resolve()
    if target.parent != root:
        raise RuntimeError("重试工作目录不在 Jobs 根目录内")
    return target

def _retry_runtime_trial_id(task: str, batch_id: str, index: int) -> str:
    if not _valid_retry_batch_id(batch_id) or index < 1:
        raise ValueError("Invalid retry runtime identity")
    return f"{pier_trial_prefix(task)}__{batch_id[:12]}{index:03d}"

def _reserve_retry_batch(run_id: int, batch_id: str) -> bool:
    with _lock:
        if run_id in _retrying or run_id in _processes:
            return False
        _cancel_requested.discard(run_id)
        _retrying[run_id] = batch_id
        return True

def _register_retry_process(
    run_id: int,
    batch_id: str,
    proc: subprocess.Popen,
) -> bool:
    with _lock:
        if _retrying.get(run_id) != batch_id:
            return False
        if run_id in _cancel_requested:
            return False
        current = _processes.get(run_id)
        if current is not None and current is not proc:
            return False
        _processes[run_id] = proc
        return True

def _release_retry_batch(
    run_id: int,
    batch_id: str,
    proc: subprocess.Popen | None = None,
) -> None:
    with _lock:
        if _retrying.get(run_id) != batch_id:
            return
        current = _processes.get(run_id)
        if proc is None or current is proc:
            _processes.pop(run_id, None)
        _retrying.pop(run_id, None)
        _cancel_requested.discard(run_id)

def _retry_specs(run: Run, trial_ids: list[str]) -> list[dict]:
    unique_ids = list(dict.fromkeys(trial_ids))
    if len(unique_ids) != len(trial_ids):
        raise ValueError("重试列表中存在重复 Trial")
    detail = parsed_run_detail(run, include_patches=False)
    by_id = {trial["id"]: trial for trial in detail["trials"]}
    missing = [trial_id for trial_id in unique_ids if trial_id not in by_id]
    if missing:
        raise ValueError(f"Trial 不存在：{missing[0]}")

    specs = []
    for trial_id in unique_ids:
        trial = by_id[trial_id]
        folder = trial_folder(run, trial_id)
        config = read_json(folder / "config.json") if folder else {}
        task_config = config.get("task")
        if not isinstance(task_config, dict):
            task_config = {
                "path": str(settings.tasks_dir / trial["task"]),
                "source": settings.tasks_dir.name,
            }
        target_id = trial_id if "__" in trial_id else ""
        while not target_id:
            candidate = f"{pier_trial_prefix(trial['task'])}__{uuid.uuid4().hex[:7]}"
            if not (jobs_root_for(run) / run.job_name / candidate).exists():
                target_id = candidate
        specs.append({
            "trial_id": trial_id,
            "target_id": target_id,
            "task": trial["task"],
            "attempt": trial["attempt"],
            "reported_cost_usd": trial.get("reported_cost_usd"),
            "task_config": task_config,
        })
    return specs

def _bind_retry_specs(
    run: Run,
    specs: list[dict],
    batch_id: str,
) -> list[dict]:
    retry_name = retry_job_name(run.job_name, batch_id)
    return [
        {
            **spec,
            "retry_batch_id": batch_id,
            "retry_job_name": retry_name,
            "runtime_trial_id": _retry_runtime_trial_id(
                spec["task"], batch_id, index
            ),
        }
        for index, spec in enumerate(specs, start=1)
    ]

def _prepare_retry_config(
    run: Run,
    specs: list[dict],
    credential,
    secret_dir: Path,
    auth_path: Path,
    jobs_root: Path,
    preferences: dict | None = None,
) -> tuple[Path, dict[str, str]]:
    original_path = jobs_root / run.job_name / "config.json"
    config = read_json(original_path)
    if not config:
        raise RuntimeError("原 Run 缺少 Pier config.json，无法按原参数重试")
    agents = config.get("agents")
    if not isinstance(agents, list) or not agents:
        raise RuntimeError("原 Run 的 Agent 配置无效")
    retry_names = {spec.get("retry_job_name") for spec in specs}
    runtime_trial_ids = [spec.get("runtime_trial_id") for spec in specs]
    if (
        len(retry_names) != 1
        or not isinstance(next(iter(retry_names)), str)
        or not all(_valid_retry_target_id(value) for value in runtime_trial_ids)
        or len(set(runtime_trial_ids)) != len(runtime_trial_ids)
    ):
        raise RuntimeError("重试批次身份无效")
    retry_name = next(iter(retry_names))

    config["job_name"] = retry_name
    config["jobs_dir"] = str(jobs_root)
    config["n_attempts"] = 1
    config["datasets"] = []
    retry_tasks = []
    for spec in specs:
        task_config = dict(spec["task_config"])
        # Explicit tasks are ad-hoc inputs. Keeping the dataset source here makes
        # Pier create an empty metric bucket and its progress hook can abort peers.
        task_config["source"] = None
        retry_tasks.append(task_config)
    config["tasks"] = retry_tasks
    tasks = list(dict.fromkeys(spec["task"] for spec in specs))
    agent_divisor, verifier_divisor = _declared_timeouts(tasks)
    config["agent_timeout_multiplier"] = run.agent_timeout_seconds / agent_divisor
    config["verifier_timeout_multiplier"] = run.verifier_timeout_seconds / verifier_divisor
    retry_config = config.setdefault("retry", {})
    retry_config["max_retries"] = run.infrastructure_max_retries
    retry_config["include_exceptions"] = (
        ["TransientAgentInfrastructureError"]
        if run.infrastructure_max_retries > 0 else []
    )
    current_preferences = {**get_preferences(), **(preferences or {})}
    process_env = _pier_process_env(
        run.id,
        run.infrastructure_max_retries,
        global_queue_limit=int(current_preferences["max_parallel_tasks"]),
    )
    process_env["DEEPSWE_RETRY_JOB_NAME"] = retry_name
    process_env["DEEPSWE_RETRY_TRIAL_NAMES"] = json.dumps(runtime_trial_ids)
    config["n_concurrent_trials"] = min(MAX_PARALLEL_TASKS, len(specs))
    try:
        trial_budget = float(current_preferences.get("trial_budget_usd") or 0)
    except (TypeError, ValueError):
        trial_budget = 0.0

    matching_agents = [agent for agent in agents if agent.get("name") == run.agent]
    if not matching_agents:
        raise RuntimeError(f"原 Run 中找不到 Agent 配置：{run.agent}")
    for agent_config in matching_agents:
        kwargs = agent_config.setdefault("kwargs", {})
        agent_env = agent_config.setdefault("env", {})
        if run.agent == "mini-swe-agent":
            process_env.update({
                "OPENAI_API_KEY": credential.token,
                "OPENAI_BASE_URL": _docker_url(credential.url),
            })
            kwargs["config_file"] = str(
                _mini_limits_config(secret_dir, run.agent_max_steps, run.reasoning_effort)
            )
            if trial_budget > 0:
                kwargs["cost_limit"] = trial_budget
            else:
                kwargs.pop("cost_limit", None)
        elif run.agent == "codex":
            kwargs["config_toml_file"] = str(_codex_config(
                credential.url,
                secret_dir,
                run.model,
                run.reasoning_effort,
                request_max_retries=run.codex_request_max_retries,
                stream_max_retries=run.codex_stream_max_retries,
                stream_idle_timeout_seconds=run.codex_stream_idle_timeout_seconds,
            ))
            agent_env["CODEX_AUTH_JSON_PATH"] = str(auth_path)
            process_env["CODEX_AUTH_JSON_PATH"] = str(auth_path)
        elif run.agent == "claude-code":
            process_env.update({
                "ANTHROPIC_API_KEY": credential.token,
                "ANTHROPIC_BASE_URL": _anthropic_url(credential.url),
            })
            kwargs["max_turns"] = run.agent_max_steps
            if trial_budget > 0:
                kwargs["max_budget_usd"] = trial_budget
            else:
                kwargs.pop("max_budget_usd", None)
        else:
            raise RuntimeError(f"不支持的 Agent：{run.agent}")

    path = secret_dir / "retry-job.json"
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return path, process_env

def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        for attempt in range(len(ATOMIC_REPLACE_RETRY_DELAYS_SEC) + 1):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == len(ATOMIC_REPLACE_RETRY_DELAYS_SEC):
                    raise
                time.sleep(ATOMIC_REPLACE_RETRY_DELAYS_SEC[attempt])
    finally:
        temporary.unlink(missing_ok=True)

def _retry_marker_config(original_dir: Path, spec: dict) -> dict:
    return {
        "task": spec["task_config"],
        "trial_name": spec["target_id"],
        "trials_dir": str(original_dir),
        "deepswe_retrying": True,
        "deepswe_replaced": True,
        "deepswe_attempt": spec["attempt"],
        "deepswe_original_trial_id": spec["trial_id"],
        "deepswe_retry_batch": spec.get("retry_batch_id"),
        "deepswe_retry_job_name": spec.get("retry_job_name"),
        "deepswe_retry_resource_id": spec.get("runtime_trial_id"),
    }

def _valid_retry_target_id(value) -> bool:
    return (
        isinstance(value, str)
        and "__" in value
        and 1 <= len(value) <= 300
        and value not in {".", ".."}
        and Path(value).name == value
    )

def _retry_target_path(original_dir: Path, target_id: str) -> Path:
    if not _valid_retry_target_id(target_id):
        raise RuntimeError(f"重试 Trial 标识无效：{target_id}")
    root = original_dir.resolve()
    target = (original_dir / target_id).resolve()
    if target.parent != root:
        raise RuntimeError("重试 Trial 目录不在原 Run 内")
    return target

def _retry_failure_type(status: str) -> str:
    return {
        "cancelled": "RetryCancelled",
        "interrupted": "RetryInterrupted",
    }.get(status, "RetryFailed")

def _set_retry_marker_state(
    original_dir: Path,
    specs: list[dict],
    status: str,
    message: str | None = None,
) -> list[str]:
    failures = []
    for spec in specs:
        marker = _retry_target_path(original_dir, spec["target_id"])
        config = read_json(marker / "config.json")
        if not config.get("deepswe_retrying"):
            continue
        expected_batch = spec.get("retry_batch_id")
        if expected_batch and config.get("deepswe_retry_batch") != expected_batch:
            # A stale batch must never overwrite the marker created by a newer retry.
            continue
        payload = {
            "status": status,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if expected_batch:
            payload["batch_id"] = expected_batch
        if status in {"failed", "cancelled", "interrupted"}:
            payload["failure_type"] = _retry_failure_type(status)
            payload["failure_message"] = message or {
                "failed": "Trial 重试未产生可替换结果",
                "cancelled": "Trial 重试已取消",
                "interrupted": "Trial 重试因服务中断未完成",
            }[status]
        try:
            _write_json_atomic(marker / "retry-state.json", payload)
        except OSError as exc:
            # 状态文件只是 UI 遥测，Windows 短暂占用不能中止真正的 Trial 执行。
            failures.append(f"{spec['target_id']}: {exc}")
    return failures

def _copy_retry_diagnostics(source: Path, marker: Path) -> None:
    for name in ("agent", "verifier", "artifacts"):
        source_path = source / name
        target_path = marker / name
        if source_path.is_dir():
            shutil.copytree(source_path, target_path, dirs_exist_ok=True)
    for name in ("trial.log", "guard.json"):
        source_path = source / name
        if source_path.is_file():
            shutil.copy2(source_path, marker / name)

def _retry_request_metadata(retry_dir: Path) -> dict:
    payload = read_json(retry_dir / ".deepswe-retry.json")
    if not payload:
        return {}
    batch_id = payload.get("batch_id")
    if batch_id is not None and not _valid_retry_batch_id(batch_id):
        return {}
    declared_name = payload.get("retry_job_name")
    if declared_name is not None and declared_name != retry_dir.name:
        return {}
    return payload

def _retry_request_specs(retry_dir: Path) -> list[dict]:
    payload = _retry_request_metadata(retry_dir)
    specs = payload.get("specs")
    if not isinstance(specs, list):
        return []
    batch_id = payload.get("batch_id")
    retry_name = payload.get("retry_job_name") or retry_dir.name
    valid = []
    for spec in specs:
        if not (
            isinstance(spec, dict)
            and isinstance(spec.get("trial_id"), str)
            and isinstance(spec.get("task"), str)
            and isinstance(spec.get("task_config"), dict)
        ):
            continue
        normalized = dict(spec)
        target_id = normalized.get("target_id")
        if not _valid_retry_target_id(target_id):
            trial_id = normalized["trial_id"]
            if not _valid_retry_target_id(trial_id):
                continue
            normalized["target_id"] = trial_id
        attempt = normalized.get("attempt")
        if not isinstance(attempt, int) or attempt < 1:
            normalized["attempt"] = 1
        if batch_id:
            if normalized.get("retry_batch_id") not in {None, batch_id}:
                continue
            normalized["retry_batch_id"] = batch_id
            normalized["retry_job_name"] = retry_name
            runtime_trial_id = normalized.get("runtime_trial_id")
            if not _valid_retry_target_id(runtime_trial_id):
                continue
        valid.append(normalized)
    return valid

def _retry_folder_task(folder: Path) -> str:
    data = read_json(folder / "result.json")
    task_name = data.get("task_name")
    if isinstance(task_name, str) and task_name:
        return task_name.split("/")[-1]
    config = read_json(folder / "config.json")
    task_path = ((config.get("task") or {}).get("path"))
    return Path(task_path).name if isinstance(task_path, str) else folder.name.split("__", 1)[0]

def _merge_retry_trials(
    run: Run,
    retry_dir: Path,
    specs: list[dict],
    supervisor_log: Path | None,
    missing_status: str = "failed",
    missing_message: str | None = None,
) -> list[str]:
    original_dir = jobs_root_for(run) / run.job_name
    original_dir.mkdir(parents=True, exist_ok=True)
    original_job_id = read_json(original_dir / "result.json").get("id")
    source_specs: dict[str, deque[dict]] = defaultdict(deque)
    runtime_specs: dict[str, dict] = {}
    moved: list[str] = []
    moved_ids: set[str] = set()
    batch_ids = {
        spec.get("retry_batch_id")
        for spec in specs
        if _valid_retry_batch_id(spec.get("retry_batch_id"))
    }
    if len(batch_ids) > 1:
        raise RuntimeError("重试批次包含冲突的 batch id")
    batch_id = next(iter(batch_ids), uuid.uuid4().hex)
    declared_retry_names = {
        spec.get("retry_job_name")
        for spec in specs
        if isinstance(spec.get("retry_job_name"), str)
    }
    if declared_retry_names and declared_retry_names != {retry_dir.name}:
        raise RuntimeError("重试目录与批次 job name 不匹配")
    for spec in sorted(specs, key=lambda item: (item["task"], item["attempt"], item["target_id"])):
        target = _retry_target_path(original_dir, spec["target_id"])
        target_config = read_json(target / "config.json")
        if (
            (target / "result.json").exists()
            and target_config.get("deepswe_replaced")
            and not target_config.get("deepswe_retrying")
        ):
            moved.append(spec["target_id"])
            moved_ids.add(spec["target_id"])
            continue
        runtime_trial_id = spec.get("runtime_trial_id")
        if _valid_retry_target_id(runtime_trial_id):
            if runtime_trial_id in runtime_specs:
                raise RuntimeError(f"重试运行时 Trial 重复：{runtime_trial_id}")
            runtime_specs[runtime_trial_id] = spec
        else:
            source_specs[spec["task"]].append(spec)

    trial_dirs = sorted(
        (path for path in retry_dir.iterdir() if path.is_dir() and "__" in path.name),
        key=lambda path: path.name,
    ) if retry_dir.is_dir() else []
    for source in trial_dirs:
        spec = runtime_specs.pop(source.name, None)
        if spec is None:
            task = _retry_folder_task(source)
            if not source_specs[task]:
                raise RuntimeError(f"重试产生了无法映射的 Trial：{source.name}")
            spec = source_specs[task].popleft()
        target = _retry_target_path(original_dir, spec["target_id"])
        marker_config = read_json(target / "config.json")
        if not (
            target.is_dir()
            and marker_config.get("deepswe_retrying")
            and marker_config.get("deepswe_replaced")
        ):
            raise RuntimeError(f"重试替换占位无效：{spec['target_id']}")
        expected_batch = spec.get("retry_batch_id")
        if expected_batch and marker_config.get("deepswe_retry_batch") != expected_batch:
            raise RuntimeError(f"重试替换批次不匹配：{spec['target_id']}")
        if not (source / "result.json").exists():
            _copy_retry_diagnostics(source, target)
            continue

        trial_config = read_json(source / "config.json") or {
            "task": spec["task_config"],
        }
        trial_config.update({
            "trial_name": spec["target_id"],
            "trials_dir": str(original_dir),
            "deepswe_attempt": spec["attempt"],
            "deepswe_replaced": True,
            "deepswe_original_trial_id": spec["trial_id"],
            "deepswe_retry_batch": batch_id,
            "deepswe_retry_job_name": retry_dir.name,
            "deepswe_retry_resource_id": source.name,
        })
        trial_config.pop("deepswe_retrying", None)
        trial_config.pop("deepswe_retry_of", None)
        if original_job_id:
            trial_config["job_id"] = original_job_id
        _write_json_atomic(source / "config.json", trial_config)
        trial_result = read_json(source / "result.json")
        trial_result.update({
            "trial_name": spec["target_id"],
            "trial_uri": target.resolve().as_uri(),
            "config": trial_config,
            "deepswe_attempt": spec["attempt"],
            "deepswe_replaced": True,
            "deepswe_original_trial_id": spec["trial_id"],
            "deepswe_retry_batch": batch_id,
            "deepswe_retry_job_name": retry_dir.name,
            "deepswe_retry_resource_id": source.name,
        })
        trial_result.pop("deepswe_retrying", None)
        trial_result.pop("deepswe_retry_of", None)
        _write_json_atomic(source / "result.json", trial_result)
        shutil.rmtree(target)
        shutil.move(str(source), str(target))
        moved.append(spec["target_id"])
        moved_ids.add(spec["target_id"])

    missing_specs = [spec for spec in specs if spec["target_id"] not in moved_ids]
    _set_retry_marker_state(
        original_dir,
        missing_specs,
        missing_status,
        missing_message,
    )

    archive = original_dir / ".retry-logs" / (
        datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + f"-{batch_id[:8]}"
    )
    archive.mkdir(parents=True, exist_ok=True)
    for source, name in (
        (supervisor_log, "supervisor.log"),
        (retry_dir / "job.log", "job.log"),
        (retry_dir / "result.json", "result.json"),
    ):
        if source and source.exists():
            shutil.copy2(source, archive / name)
    _write_json_atomic(archive / "retry.json", {
        "batch_id": batch_id,
        "run_id": run.id,
        "retry_job_name": retry_dir.name,
        "requested_trial_ids": [spec["trial_id"] for spec in specs],
        "replaced_trial_ids": moved,
        "unreplaced_trial_ids": [spec["target_id"] for spec in missing_specs],
        "finished_at": datetime.now(UTC).isoformat(),
    })
    shutil.rmtree(retry_dir, ignore_errors=True)
    if supervisor_log:
        supervisor_log.unlink(missing_ok=True)
    (jobs_root_for(run) / f"{retry_dir.name}.docker-cleanup.json").unlink(missing_ok=True)
    return moved

def _apply_trial_aggregate(run: Run) -> dict:
    aggregate = aggregate_trial_results(run)
    if run.verification:
        run.reward = aggregate["reward"]
        run.passed = aggregate["passed"]
    else:
        run.reward = None
        run.passed = None
    run.input_tokens = aggregate["input_tokens"]
    run.cached_tokens = aggregate["cached_tokens"]
    run.output_tokens = aggregate["output_tokens"]
    run.cost_usd = aggregate["cost_usd"]
    return aggregate

def _budget_value(preferences: dict, key: str) -> float:
    try:
        return float(preferences.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0

def _visible_trial_cost(trial: dict, service_tier: str, model: str | None = None) -> float:
    reported = trial.get("reported_cost_usd")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool):
        return float(reported)
    estimated = estimate_cost(
        trial.get("input_tokens"),
        trial.get("cached_tokens"),
        trial.get("output_tokens"),
        service_tier,
        model,
    )
    return float(estimated or 0)

def _sync_retry_result(
    run_id: int,
    returncode: int,
    retry_result: dict,
    moved_count: int,
    requested_count: int,
    failure_summary: str | None,
) -> None:
    stats = retry_result.get("stats") or {}
    with _state_lock, SessionLocal() as db:
        run = db.get(Run, run_id)
        if not run:
            return
        aggregate = _apply_trial_aggregate(run)
        batch_succeeded = (
            returncode == 0
            and moved_count == requested_count
            and not stats.get("n_errored_trials")
        )
        success = (
            batch_succeeded
            and not aggregate["effective_errored"]
            and not aggregate["missing_configured"]
        )
        if run.status not in TERMINAL_STATES:
            run.status = "completed" if success else "failed"
            if success:
                run.error = None
            elif failure_summary:
                run.error = redact(f"重试失败：{failure_summary}", current_secrets())[:4000]
            elif returncode != 0:
                run.error = f"重试 Pier 进程退出码 {returncode}"
            elif stats.get("n_errored_trials"):
                run.error = f"重试中有 {stats.get('n_errored_trials')} 个 Trial 执行失败"
            elif aggregate["effective_errored"] or aggregate["missing_configured"]:
                parts = []
                if aggregate["effective_errored"]:
                    parts.append(f"{aggregate['effective_errored']} 个 Trial 仍为执行错误")
                if aggregate["missing_configured"]:
                    parts.append(f"{aggregate['missing_configured']} 个配置 Trial 尚无结果")
                run.error = "重试已完成，但 Run 仍未完整：" + "，".join(parts)
            else:
                run.error = "重试未产生可合并的 Trial 结果"
        run.pid = None
        run.finished_at = run.finished_at or datetime.now(UTC)
        db.commit()

def _execute_retry(run_id: int, specs: list[dict], batch_id: str) -> None:
    secret_dir = None
    retry_dir = None
    supervisor_log = None
    proc = None
    original_dir = None
    preferences = None
    try:
        preferences = get_preferences()
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if not run:
                return
            infrastructure_max_retries = int(preferences["infrastructure_max_retries"])
            run.agent_timeout_seconds = int(preferences["agent_timeout_seconds"])
            run.verifier_timeout_seconds = int(preferences["verifier_timeout_seconds"])
            run.concurrency = int(preferences["max_parallel_tasks"])
            run.retry_infrastructure_errors = infrastructure_max_retries > 0
            run.infrastructure_max_retries = infrastructure_max_retries
            run.agent_max_steps = int(preferences["agent_max_steps"])
            run.status = "preflight"
            run.pid = None
            db.commit()
            db.refresh(run)
            jobs_root = jobs_root_for(run)
            retry_dir = _retry_dir(jobs_root, run.job_name, batch_id)
            retry_name = retry_job_name(run.job_name, batch_id)
            service_tier = run.service_tier
            agent = run.agent
            model = run.model
            max_steps = run.agent_max_steps
            original_dir = jobs_root / run.job_name
        _verify_global_queue_patch(run_id)
        if not retry_dir.is_dir():
            raise RuntimeError("重试工作目录缺失")
        metadata = _retry_request_metadata(retry_dir)
        if (
            metadata.get("run_id") != run_id
            or metadata.get("batch_id") != batch_id
            or metadata.get("retry_job_name") != retry_name
        ):
            raise RuntimeError("重试工作目录与当前批次不匹配")
        _set_retry_marker_state(original_dir, specs, "preflight")
        if preferences.get("docker_cleanup_after_run", True):
            try:
                cleanup_job_resources(
                    run.job_name,
                    jobs_root,
                    DockerCleanupPolicy(),
                    trigger="retry-replace-old",
                    projects=[
                        sanitize_compose_project_name(spec["target_id"])
                        for spec in specs
                    ],
                )
            except Exception:
                pass

        base_cost_usd = _completed_trials_cost(original_dir, service_tier, model)
        run_budget = _budget_value(preferences, "run_budget_usd")
        if run_budget > 0 and base_cost_usd >= run_budget:
            raise RuntimeError(
                f"保留 Trial 的累计费用 ${base_cost_usd:.2f} 已达到 Run 预算上限 ${run_budget:.2f}"
            )

        credential = read_credential(credential_path())
        _preflight(list(dict.fromkeys(spec["task"] for spec in specs)), credential.url)
        secret_dir, auth_path = _write_secret_auth(credential.token)
        config_path, process_env = _prepare_retry_config(
            run, specs, credential, secret_dir, auth_path, jobs_root, preferences
        )
        jobs_root.mkdir(parents=True, exist_ok=True)
        supervisor_log = jobs_root / f"{retry_name}.supervisor.log"
        args = [shutil.which("pier") or "pier", "run", "--config", str(config_path), "-y"]
        _set_retry_marker_state(original_dir, specs, "queued")
        with SessionLocal() as db:
            current = db.get(Run, run_id)
            if current and current.status not in TERMINAL_STATES:
                current.status = "queued"
                db.commit()
        with supervisor_log.open("w", encoding="utf-8") as log:
            with _lock:
                if run_id in _cancel_requested:
                    return
            proc = subprocess.Popen(
                args,
                cwd=settings.tasks_dir.parent,
                env=process_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            if not _register_retry_process(run_id, batch_id, proc):
                _terminate_tree(proc.pid)
                raise RuntimeError("重试批次已被替换，拒绝登记旧进程")
            with SessionLocal() as db:
                current = db.get(Run, run_id)
                if current:
                    current.pid = proc.pid
                    db.commit()
            guard_error = _wait_with_guard(
                proc,
                retry_dir,
                service_tier,
                agent,
                max_steps,
                base_cost_usd,
                model,
            )

        if guard_error:
            with _state_lock, SessionLocal() as db:
                current = db.get(Run, run_id)
                if current and current.status not in TERMINAL_STATES:
                    current.status = "cancelled"
                    current.error = guard_error
                    current.finished_at = datetime.now(UTC)
                    db.commit()

        retry_result = read_json(retry_dir / "result.json")
        failure_summary = _run_failure_summary(retry_dir, supervisor_log)
        if preferences.get("docker_cleanup_after_run", True):
            try:
                cleanup_job_resources(
                    retry_name, jobs_root, DockerCleanupPolicy(), trigger="retry-finished"
                )
            except Exception:
                pass
        with SessionLocal() as db:
            current = db.get(Run, run_id)
            current_status = current.status if current else "failed"
            current_error = current.error if current else None
        missing_status = (
            current_status
            if current_status in {"cancelled", "interrupted"}
            else "failed"
        )
        moved = _merge_retry_trials(
            run,
            retry_dir,
            specs,
            supervisor_log,
            missing_status=missing_status,
            missing_message=current_error or guard_error or failure_summary,
        )
        _sync_retry_result(
            run_id,
            proc.returncode,
            retry_result,
            len(moved),
            len(specs),
            failure_summary,
        )
    except Exception as exc:
        if original_dir:
            with SessionLocal() as db:
                current = db.get(Run, run_id)
                current_status = current.status if current else "failed"
            marker_status = (
                current_status
                if current_status in {"cancelled", "interrupted"}
                else "failed"
            )
            _set_retry_marker_state(original_dir, specs, marker_status, str(exc))
        with _state_lock, SessionLocal() as db:
            run = db.get(Run, run_id)
            if run:
                _apply_trial_aggregate(run)
                if run.status not in TERMINAL_STATES:
                    run.status = "failed"
                    run.error = redact(f"Trial 重试失败：{exc}", current_secrets())[:4000]
                    run.finished_at = datetime.now(UTC)
                run.pid = None
                db.commit()
    finally:
        _release_retry_batch(run_id, batch_id, proc)
        try:
            clear_run_queue(run_id)
        except Exception:
            pass
        if secret_dir:
            shutil.rmtree(secret_dir, ignore_errors=True)
        if (
            retry_dir
            and retry_dir.exists()
            and (preferences or get_preferences()).get("docker_cleanup_after_run", True)
        ):
            try:
                cleanup_job_resources(
                    retry_dir.name,
                    retry_dir.parent,
                    DockerCleanupPolicy(),
                    trigger="retry-finalize",
                )
            except Exception:
                pass

def retry_trials(run_id: int, trial_ids: list[str]) -> dict:
    batch_id = uuid.uuid4().hex
    reserved = False
    try:
        with _state_lock, SessionLocal() as db:
            run = db.get(Run, run_id)
            if not run:
                raise LookupError("run not found")
            if run.status not in TERMINAL_STATES:
                raise RuntimeError("Run 正在执行，不能同时提交 Trial 重试")
            specs = _bind_retry_specs(run, _retry_specs(run, trial_ids), batch_id)
            preferences = get_preferences()
            selected_ids = {spec["trial_id"] for spec in specs}
            detail = parsed_run_detail(run, include_patches=False)
            retained_cost = sum(
                _visible_trial_cost(trial, run.service_tier, run.model)
                for trial in detail["trials"]
                if trial["id"] not in selected_ids
            )
            run_budget = _budget_value(preferences, "run_budget_usd")
            if run_budget > 0 and retained_cost >= run_budget:
                raise RuntimeError(
                    f"保留 Trial 的累计费用 ${retained_cost:.2f} 已达到 Run 预算上限 ${run_budget:.2f}"
                )
            if not _reserve_retry_batch(run_id, batch_id):
                raise RuntimeError("上一批 Trial 重试仍在收尾，暂不能提交新重试")
            reserved = True
            clear_run_queue(run_id, db=db)
            admission = queue_admission(
                len(specs),
                db=db,
                limit=int(preferences["max_parallel_tasks"]),
            )
            enqueue_retry_trials(db, run_id, specs, batch_id)
            jobs_root = jobs_root_for(run)
            original_dir = jobs_root / run.job_name
            retry_name = retry_job_name(run.job_name, batch_id)
            retry_dir = _retry_dir(jobs_root, run.job_name, batch_id)
            for stale_dir in retry_job_dirs(jobs_root, run.job_name):
                try:
                    cleanup_job_resources(
                        stale_dir.name,
                        jobs_root,
                        DockerCleanupPolicy(),
                        trigger="retry-stale",
                    )
                except Exception:
                    pass
                shutil.rmtree(stale_dir, ignore_errors=True)
                (jobs_root / f"{stale_dir.name}.supervisor.log").unlink(missing_ok=True)
                (jobs_root / f"{stale_dir.name}.docker-cleanup.json").unlink(missing_ok=True)
            retry_dir.mkdir(parents=True)
            _write_json_atomic(retry_dir / ".deepswe-retry.json", {
                "run_id": run_id,
                "batch_id": batch_id,
                "retry_job_name": retry_name,
                "created_at": datetime.now(UTC).isoformat(),
                "specs": specs,
            })
            for spec in specs:
                target = _retry_target_path(original_dir, spec["target_id"])
                if target.exists():
                    shutil.rmtree(target)
                target.mkdir(parents=True)
                _write_json_atomic(
                    target / "config.json",
                    _retry_marker_config(original_dir, spec),
                )
            _set_retry_marker_state(original_dir, specs, "queued")
            run.status = "queued"
            run.error = None
            run.finished_at = None
            run.pid = None
            _apply_trial_aggregate(run)
            db.commit()
    except Exception:
        if reserved:
            _release_retry_batch(run_id, batch_id)
        raise
    try:
        threading.Thread(
            target=_execute_retry,
            args=(run_id, specs, batch_id),
            daemon=True,
        ).start()
    except Exception:
        _release_retry_batch(run_id, batch_id)
        clear_run_queue(run_id)
        with _state_lock, SessionLocal() as db:
            run = db.get(Run, run_id)
            if run:
                run.status = "failed"
                run.error = "无法启动 Trial 重试线程"
                run.finished_at = datetime.now(UTC)
                _set_retry_marker_state(
                    jobs_root_for(run) / run.job_name,
                    specs,
                    "failed",
                    run.error,
                )
                db.commit()
        raise
    return {
        "started": True,
        "run_id": run_id,
        "batch_id": batch_id,
        "retry_job_name": retry_name,
        "trial_ids": [spec["trial_id"] for spec in specs],
        "retry_count": len(specs),
        "admission": admission,
    }

def _cleanup_after_run(run_id: int, job_name: str | None, jobs_root: Path | None) -> None:
    """运行结束（含失败/取消）后的轻量 Docker 清理；失败不影响结果状态。"""
    if not job_name:
        return
    try:
        if not get_preferences().get("docker_cleanup_after_run", True):
            return
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if not run or run.status not in TERMINAL_STATES:
                return
        cleanup_job_resources(job_name, jobs_root, DockerCleanupPolicy(), trigger="run-finished")
    except Exception:
        pass

def _metric_reward(metric: dict):
    # pier 聚合器把单键 rewards 改名为 mean；多键（DeepSWE：reward/partial/f2p/p2p）保留原键
    if "reward" in metric:
        return metric.get("reward")
    return metric.get("mean")

def _sync_result(run_id: int, returncode: int):
    with _state_lock, SessionLocal() as db:
        run = db.get(Run, run_id)
        if not run:
            return
        result_path = jobs_root_for(run) / run.job_name / "result.json"
        data = read_json(result_path)
        stats = data.get("stats") or {}
        metrics = []
        for value in (stats.get("evals") or {}).values():
            metrics += value.get("metrics") or []
        if run.verification:
            rewards = [value for value in (_metric_reward(m) for m in metrics) if isinstance(value, (int, float))]
            run.reward = sum(rewards) / len(rewards) if rewards else None
            run.passed = bool(rewards) and len(rewards) == len(metrics) and all(value == 1 for value in rewards)
        else:
            # 禁用 Verifier 时没有测量结果，「未测量」必须与「全部失败」区分
            run.reward = None
            run.passed = None
        run.input_tokens = stats.get("n_input_tokens"); run.cached_tokens = stats.get("n_cache_tokens")
        run.output_tokens = stats.get("n_output_tokens"); run.cost_usd = stats.get("cost_usd")
        has_result = bool(metrics) if run.verification else bool(data)
        success = returncode == 0 and has_result and not stats.get("n_errored_trials")
        if run.status not in TERMINAL_STATES:
            run.status = "completed" if success else "failed"
            if not success and not run.error:
                summary = _run_failure_summary(
                    result_path.parent,
                    jobs_root_for(run) / f"{run.job_name}.supervisor.log",
                )
                if summary:
                    run.error = redact(summary, current_secrets())[:4000]
                elif returncode != 0:
                    run.error = f"Pier 进程退出码 {returncode}"
                elif stats.get("n_errored_trials"):
                    run.error = f"{stats.get('n_errored_trials')} 个 Trial 执行失败"
            run.finished_at = datetime.now(UTC)
        db.commit()

def _kill_process_tree(pid: int, require_pier: bool = False) -> None:
    try:
        proc = psutil.Process(pid)
        if require_pier:
            # 重启后 PID 可能被无关进程复用，仅当命令行仍指向 pier 时才收割
            command = " ".join(proc.cmdline()).lower()
            if "pier" not in command:
                return
        children = proc.children(recursive=True)
        for child in children:
            try: child.kill()
            except psutil.Error: pass
        proc.kill()
    except psutil.Error:
        pass

def cancel_run(run_id: int) -> bool:
    with _lock:
        proc = _processes.get(run_id)
        retry_batch_id = _retrying.get(run_id)
        if proc is None:
            _cancel_requested.add(run_id)
    with _state_lock, SessionLocal() as db:
        run = db.get(Run, run_id)
        if not run:
            with _lock:
                _cancel_requested.discard(run_id)
            return False
        job_name, jobs_root = run.job_name, jobs_root_for(run)
        if run.status in TERMINAL_STATES:
            # 进程恰好已正常结束并落库，不把 completed 改写成 cancelled
            with _lock:
                _cancel_requested.discard(run_id)
            return False
        run.status = "cancelled"; run.finished_at = datetime.now(UTC); db.commit()
    if proc is not None:
        _terminate_tree(proc.pid)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
    clear_run_queue(run_id)
    cleanup_name = (
        retry_job_name(job_name, retry_batch_id)
        if retry_batch_id else job_name
    )
    try:
        cleanup_job_resources(cleanup_name, jobs_root, DockerCleanupPolicy(), trigger="cancelled")
    except Exception:
        pass
    return True

def reap_orphaned_runs() -> None:
    """服务重启后收割上次会话残留的 pier 进程与 Docker 资源，再标记 interrupted。"""
    with SessionLocal() as db:
        rows = db.scalars(select(Run).where(Run.status.in_(ACTIVE_STATES))).all()
        stale = [(row.id, row.pid, row.job_name, jobs_root_for(row)) for row in rows]
    for run_id, pid, job_name, jobs_root in stale:
        if pid:
            _kill_process_tree(pid, require_pier=True)
        original_dir = jobs_root / job_name
        marker_batches = set()
        if original_dir.is_dir():
            for path in original_dir.iterdir():
                if not path.is_dir() or "__" not in path.name:
                    continue
                config = read_json(path / "config.json")
                batch_id = config.get("deepswe_retry_batch")
                if config.get("deepswe_retrying") and _valid_retry_batch_id(batch_id):
                    marker_batches.add(batch_id)
        retry_candidates = []
        for candidate in retry_job_dirs(jobs_root, job_name):
            metadata = _retry_request_metadata(candidate)
            if not metadata or metadata.get("run_id") in {None, run_id}:
                retry_candidates.append(candidate)
            try:
                cleanup_job_resources(
                    candidate.name,
                    jobs_root,
                    DockerCleanupPolicy(),
                    trigger="startup-reap",
                )
            except Exception:
                pass
        matching_candidates = [
            candidate
            for candidate in retry_candidates
            if _retry_request_metadata(candidate).get("batch_id") in marker_batches
        ]
        retry_dir = max(
            matching_candidates or retry_candidates,
            key=lambda path: path.stat().st_mtime,
            default=None,
        )
        is_retry = retry_dir is not None
        if not is_retry:
            try:
                cleanup_job_resources(
                    job_name,
                    jobs_root,
                    DockerCleanupPolicy(),
                    trigger="startup-reap",
                )
            except Exception:
                pass
        recovered = 0
        recovery_error = None
        if is_retry:
            specs = _retry_request_specs(retry_dir)
            if specs:
                with SessionLocal() as db:
                    retry_run = db.get(Run, run_id)
                if retry_run:
                    try:
                        recovered = len(_merge_retry_trials(
                            retry_run,
                            retry_dir,
                            specs,
                            jobs_root / f"{retry_dir.name}.supervisor.log",
                            missing_status="interrupted",
                            missing_message="服务重启时重试尚未完成",
                        ))
                    except Exception as exc:
                        recovery_error = redact(str(exc), current_secrets())[:1000]
                        _set_retry_marker_state(
                            jobs_root / job_name,
                            specs,
                            "interrupted",
                            recovery_error,
                        )
        has_replacements = original_dir.is_dir() and any(
            read_json(path / "config.json").get("deepswe_replaced")
            for path in original_dir.iterdir()
            if path.is_dir() and "__" in path.name
        )
        with _state_lock, SessionLocal() as db:
            run = db.get(Run, run_id)
            if run and run.status in ACTIVE_STATES:
                if recovered or has_replacements:
                    _apply_trial_aggregate(run)
                run.status = "interrupted"
                run.finished_at = run.finished_at or datetime.now(UTC)
                message = "服务重启后检测到运行已中断，已回收残留进程与容器"
                if recovered:
                    message += f"；已将 {recovered} 个重试 Trial 合并回原 Run"
                if recovery_error:
                    message += f"；重试结果恢复失败：{recovery_error}"
                run.error = run.error or message
                db.commit()
        clear_run_queue(run_id)

def shutdown_processes() -> None:
    """服务退出时终止仍在运行的 pier 子进程，防止孤儿进程继续调用付费 API。"""
    with _lock: items = list(_processes.items())
    for run_id, proc in items:
        if proc.poll() is None:
            _terminate_tree(proc.pid)
        clear_run_queue(run_id)

def serialize(run: Run) -> dict:
    progress = run_trial_progress(run)
    queue = queue_status(run.id)
    return {"id":run.id,"run_code":run_code(run.id),"job_name":run.job_name,"status":run.status,"agent":run.agent,"model":run.model,"reasoning_effort":run.reasoning_effort,"reasoning_effort_adapter":run.reasoning_effort_adapter,"reasoning_effort_effective":run.reasoning_effort_effective,"pier_version":run.pier_version,"tasks":json.loads(run.tasks_json),"attempts_per_task":run.attempts_per_task,"concurrency":run.concurrency,"queue":queue,"agent_timeout_seconds":run.agent_timeout_seconds,"verifier_timeout_seconds":run.verifier_timeout_seconds,"retry_infrastructure_errors":run.retry_infrastructure_errors,"infrastructure_max_retries":run.infrastructure_max_retries,"agent_max_steps":run.agent_max_steps,"codex_request_max_retries":run.codex_request_max_retries,"codex_stream_max_retries":run.codex_stream_max_retries,"codex_stream_idle_timeout_seconds":run.codex_stream_idle_timeout_seconds,"created_at":run.created_at,"finished_at":run.finished_at,"passed":run.passed,"reward":run.reward,"progress":progress,"task_progress":run_task_progress(run),"input_tokens":run.input_tokens,"cached_tokens":run.cached_tokens,"uncached_input_tokens":max((run.input_tokens or 0)-(run.cached_tokens or 0),0),"output_tokens":run.output_tokens,"cost_usd":run.cost_usd,"reported_cost_usd":run.cost_usd,"estimated_cost_usd":estimate_cost(run.input_tokens,run.cached_tokens,run.output_tokens,run.service_tier,run.model),"service_tier":run.service_tier,"error":run.error}

def list_runs() -> list[dict]:
    with SessionLocal() as db: return [serialize(r) for r in db.scalars(select(Run).order_by(Run.id.desc())).all()]

def get_run(run_id:int) -> dict|None:
    with SessionLocal() as db:
        run=db.get(Run,run_id); return serialize(run) if run else None

def run_log(run_id:int) -> str:
    with SessionLocal() as db: run=db.get(Run,run_id)
    if not run:return ""
    root=jobs_root_for(run); job_dir=root/run.job_name
    paths=[root/f"{run.job_name}.supervisor.log",job_dir/"job.log"]
    paths += sorted((job_dir/".retry-logs").glob("*/*.log")) if (job_dir/".retry-logs").is_dir() else []
    return redact("\n".join(p.read_text(encoding="utf-8",errors="replace") for p in paths if p.exists()), current_secrets())[-200000:]
