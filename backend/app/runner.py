import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
import psutil
from sqlalchemy import select
from .config import settings
from .database import SessionLocal
from .docker_cleanup import DockerCleanupPolicy, cleanup_job_resources, docker_available
from .models import ACTIVE_STATES, TERMINAL_STATES, Run
from .preferences import credential_path, current_secrets, get_preferences, jobs_path
from .results import _json as read_json, estimate_cost, jobs_root_for, run_code
from .schemas import RunDraft
from .security import read_credential, redact

_processes: dict[int, subprocess.Popen] = {}
_lock = threading.Lock()
# 取消与结果落库都要做「读状态→写状态」，用同一把锁避免最后写者赢
_state_lock = threading.Lock()

# 2026-07-12 试运行事故（单 Trial 烧掉约 $225）后引入的用量护栏，见 审查报告.md
MINI_STEP_LIMIT = 250
GUARD_CHECK_INTERVAL_SEC = 20
RUNAWAY_AGENT_LOG_MB = 30       # 正常 Trial 的 agent 日志在个位数 MB；事故 Trial 43 分钟写了 278MB
RUNAWAY_SUBAGENT_FILES = 60     # 禁用 Task/Agent 工具后应恒为 0；事故 Trial 产生了 5374 个会话文件

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

def _write_secret_auth(token: str) -> tuple[Path, Path]:
    folder = Path(tempfile.mkdtemp(prefix="deepswe-ui-"))
    auth = folder / "auth.json"
    auth.write_text(json.dumps({"OPENAI_API_KEY": token}), encoding="utf-8", newline="\n")
    if os.name == "nt":
        account = f"{os.environ.get('USERDOMAIN')}\\{os.environ.get('USERNAME')}"
        subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F"], capture_output=True)
    return folder, auth

def _codex_config(base_url: str, folder: Path, model: str, effort: str) -> Path:
    path = folder / "codex-provider.toml"
    path.write_text(
        f'model = "{model}"\nmodel_reasoning_effort = "{effort}"\nmodel_provider = "local_proxy"\n\n'
        '[model_providers.local_proxy]\nname = "Local Proxy"\nbase_url = "' + _docker_url(base_url) + '"\n'
        'wire_api = "responses"\nrequires_openai_auth = true\nsupports_websockets = false\n',
        encoding="utf-8", newline="\n")
    return path

def _mini_limits_config(folder: Path) -> Path:
    """mini-swe-agent 的 cost_limit 走 litellm 价格表，自建网关模型算不出成本（恒为 0），
    只有 step_limit 是确定性护栏；pier adapter 会把该文件内容写进容器再以 -c 追加。"""
    path = folder / "mini-limits.yaml"
    path.write_text(f"agent:\n  step_limit: {MINI_STEP_LIMIT}\n", encoding="utf-8", newline="\n")
    return path

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

def create_run(draft: RunDraft) -> Run:
    if draft.concurrency > 4 or (draft.concurrency == 4 and not draft.confirm_high_concurrency):
        raise ValueError("并发配置未获允许")
    with SessionLocal() as db:
        mapping = "thinking=disabled" if draft.agent == "claude-code" and draft.reasoning_effort == "none" else ("model_reasoning_effort=" + draft.reasoning_effort if draft.agent == "codex" else "reasoning_effort=" + draft.reasoning_effort)
        run = Run(
            status="queued", job_name=f"pending-{uuid.uuid4().hex}", jobs_dir=str(jobs_path()), agent=draft.agent, model=draft.model,
            reasoning_effort=draft.reasoning_effort, reasoning_effort_adapter=mapping,
            reasoning_effort_effective=None,  # 有效值只能来自运行后的观测，创建时未知
            tasks_json=json.dumps(draft.tasks), attempts_per_task=draft.attempts_per_task,
            concurrency=draft.concurrency, agent_timeout_seconds=draft.agent_timeout_seconds,
            verifier_timeout_seconds=draft.verifier_timeout_seconds,
            retry_infrastructure_errors=draft.retry_infrastructure_errors,
            verification=draft.verification, service_tier=draft.service_tier)
        db.add(run); db.flush()
        run.job_name = f"run-{run.id:06d}-{draft.agent}"
        db.commit(); db.refresh(run); run_id = run.id
    threading.Thread(target=_execute, args=(run_id,), daemon=True).start()
    with SessionLocal() as db: return db.get(Run, run_id)

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

def _preflight(tasks: list[str]) -> None:
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

def _completed_trials_cost(job_dir: Path, service_tier: str) -> float:
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
                                 agent_result.get("n_output_tokens"), service_tier)
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

def _wait_with_guard(proc: subprocess.Popen, job_dir: Path, service_tier: str) -> str | None:
    """等待 pier 结束，期间周期核查用量；触发护栏时终止进程树并返回原因。"""
    try:
        budget = float(get_preferences().get("run_budget_usd") or 0)
    except (TypeError, ValueError):
        budget = 0.0
    while True:
        try:
            proc.wait(timeout=GUARD_CHECK_INTERVAL_SEC)
            return None
        except subprocess.TimeoutExpired:
            pass
        reason = _runaway_reason(job_dir)
        if not reason and budget > 0:
            spent = _completed_trials_cost(job_dir, service_tier)
            if spent >= budget:
                reason = f"已落盘 Trial 累计费用 ${spent:.2f} 达到预算上限 ${budget:.2f}"
        if reason:
            _terminate_tree(proc.pid)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            return f"用量护栏自动终止：{reason}"

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
        _preflight(tasks)
        cred = read_credential(credential_path())
        secret_dir, auth = _write_secret_auth(cred.token)
        command_model = f"openai/{model}" if agent == "mini-swe-agent" and "/" not in model else model
        agent_divisor, verifier_divisor = _declared_timeouts(tasks)
        args = [shutil.which("pier") or "pier", "run", "-p", str(settings.tasks_dir), "--agent", agent, "--model", command_model, "-n", str(concurrency), "-k", str(attempts), "-y", "--job-name", job_name, "--jobs-dir", str(jobs_root), "--agent-timeout-multiplier", str(run.agent_timeout_seconds / agent_divisor), "--verifier-timeout-multiplier", str(run.verifier_timeout_seconds / verifier_divisor), "--max-retries", "1" if run.retry_infrastructure_errors else "0"]
        if not run.verification:
            args.append("--disable-verification")
        for task in tasks: args += ["-i", task]
        process_env = os.environ.copy()
        # pier 未显式指定编码：GBK locale 下读 UTF-8 trajectory.json（trial.py read_text）
        # 与 rich 向控制台打印 '•' 都会 UnicodeError 崩溃，强制子进程走 UTF-8 模式
        process_env["PYTHONUTF8"] = "1"
        if agent == "codex":
            config = _codex_config(cred.url, secret_dir, model, effort)
            args += ["--agent-env", f"CODEX_AUTH_JSON_PATH={auth}", "--agent-kwarg", f"config_toml_file={config}", "--agent-kwarg", f"reasoning_effort={effort}"]
        elif agent == "mini-swe-agent":
            process_env.update({"OPENAI_API_KEY": cred.token, "OPENAI_BASE_URL": _docker_url(cred.url)})
            limits = _mini_limits_config(secret_dir)
            # litellm_response（Responses API 桥）顶层 import litellm.proxy，
            # 需要完整 proxy extras（fastapi/orjson/pyjwt 等），agent 容器默认没装
            args += ["--agent-kwarg", f"reasoning_effort={effort}", "--agent-kwarg", f"config_file={limits}",
                     "--agent-kwarg", 'extra_python_packages=["litellm[proxy]"]']
        elif agent == "claude-code":
            process_env.update({"ANTHROPIC_API_KEY": cred.token, "ANTHROPIC_BASE_URL": _anthropic_url(cred.url)})
            # 覆盖 pier 默认的 disallowed_tools=EnterPlanMode，追加 Task/Agent 禁用容器内 subagent：
            # 2026-07-12 事故中主 agent 43 分钟 spawn 2687 个 subagent（6663 万输入 token），
            # max_turns 只约束主对话轮数，对 subagent 无效
            args += ["--agent-kwarg", "max_turns=80", "--agent-kwarg", "disallowed_tools=EnterPlanMode,Task,Agent"]
            if effort == "none":
                args += ["--agent-kwarg", "thinking=disabled"]
            else:
                args += ["--agent-kwarg", f"reasoning_effort={effort}"]
        else:
            raise ValueError(f"不支持的 agent: {agent}")
        jobs_root.mkdir(parents=True, exist_ok=True)
        log_path = jobs_root / f"{job_name}.supervisor.log"
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                args, cwd=settings.tasks_dir.parent, env=process_env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=os.name != "nt")
            with _lock: _processes[run_id] = proc
            with SessionLocal() as db:
                run = db.get(Run, run_id); run.status = "running"; run.pid = proc.pid; db.commit()
            guard_error = _wait_with_guard(proc, jobs_root / job_name, service_tier)
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
        with _lock: _processes.pop(run_id, None)
        if secret_dir: shutil.rmtree(secret_dir, ignore_errors=True)
        _cleanup_after_run(run_id, job_name, jobs_root)

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
            if not success and not run.error and returncode != 0:
                run.error = f"Pier 进程退出码 {returncode}"
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
    with _lock: proc = _processes.get(run_id)
    if not proc: return False
    _terminate_tree(proc.pid)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        pass
    with _state_lock, SessionLocal() as db:
        run = db.get(Run, run_id)
        if not run:
            return False
        job_name, jobs_root = run.job_name, jobs_root_for(run)
        if run.status in TERMINAL_STATES:
            # 进程恰好已正常结束并落库，不把 completed 改写成 cancelled
            return False
        run.status = "cancelled"; run.finished_at = datetime.now(UTC); db.commit()
    try:
        cleanup_job_resources(job_name, jobs_root, DockerCleanupPolicy(), trigger="cancelled")
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
        try:
            cleanup_job_resources(job_name, jobs_root, DockerCleanupPolicy(), trigger="startup-reap")
        except Exception:
            pass
        with _state_lock, SessionLocal() as db:
            run = db.get(Run, run_id)
            if run and run.status in ACTIVE_STATES:
                run.status = "interrupted"
                run.finished_at = run.finished_at or datetime.now(UTC)
                run.error = run.error or "服务重启后检测到运行已中断，已回收残留进程与容器"
                db.commit()

def shutdown_processes() -> None:
    """服务退出时终止仍在运行的 pier 子进程，防止孤儿进程继续调用付费 API。"""
    with _lock: items = list(_processes.items())
    for _run_id, proc in items:
        if proc.poll() is None:
            _terminate_tree(proc.pid)

def serialize(run: Run) -> dict:
    return {"id":run.id,"run_code":run_code(run.id),"job_name":run.job_name,"status":run.status,"agent":run.agent,"model":run.model,"reasoning_effort":run.reasoning_effort,"reasoning_effort_adapter":run.reasoning_effort_adapter,"reasoning_effort_effective":run.reasoning_effort_effective,"pier_version":run.pier_version,"tasks":json.loads(run.tasks_json),"attempts_per_task":run.attempts_per_task,"concurrency":run.concurrency,"created_at":run.created_at,"finished_at":run.finished_at,"passed":run.passed,"reward":run.reward,"input_tokens":run.input_tokens,"cached_tokens":run.cached_tokens,"uncached_input_tokens":max((run.input_tokens or 0)-(run.cached_tokens or 0),0),"output_tokens":run.output_tokens,"cost_usd":run.cost_usd,"reported_cost_usd":run.cost_usd,"estimated_cost_usd":estimate_cost(run.input_tokens,run.cached_tokens,run.output_tokens,run.service_tier),"service_tier":run.service_tier,"error":run.error}

def list_runs() -> list[dict]:
    with SessionLocal() as db: return [serialize(r) for r in db.scalars(select(Run).order_by(Run.id.desc())).all()]

def get_run(run_id:int) -> dict|None:
    with SessionLocal() as db:
        run=db.get(Run,run_id); return serialize(run) if run else None

def run_log(run_id:int) -> str:
    with SessionLocal() as db: run=db.get(Run,run_id)
    if not run:return ""
    root=jobs_root_for(run); paths=[root/f"{run.job_name}.supervisor.log",root/run.job_name/"job.log"]
    return redact("\n".join(p.read_text(encoding="utf-8",errors="replace") for p in paths if p.exists()), current_secrets())[-200000:]
