import json
import tomllib
from datetime import datetime
from pathlib import Path
from statistics import mean
from sqlalchemy import select
from .database import SessionLocal
from .config import settings
from .models import Baseline, Run
from .official_stats import load_official_stats
from .preferences import current_secrets, jobs_path
from .pier_retry_patch.transient import is_transient_agent_failure
from .security import redact

TIER_MULTIPLIER = {"standard": 1.0, "batch": 0.5, "priority": 2.0}
CONTROL_TASK = "actionlint-action-pinning-lint"
def run_code(run_id: int) -> str:
    return f"RUN-{run_id:06d}"

def task_identity(task: str) -> dict:
    """Canonical user-facing identity shared by task, run, trial and comparison views."""
    folders = sorted((p for p in settings.tasks_dir.iterdir() if p.is_dir()), key=lambda p: p.name) if settings.tasks_dir.exists() else []
    names = [folder.name for folder in folders]
    number = names.index(task) + 1 if task in names else None
    metadata = {}
    path = settings.tasks_dir / task / "task.toml"
    try:
        metadata = tomllib.loads(path.read_text(encoding="utf-8")).get("metadata", {})
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return {
        "task": task,
        "task_slug": task,
        "task_number": number,
        "task_code": f"TASK-{number:03d}" if number else "TASK-UNKNOWN",
        "task_title": metadata.get("display_title") or task,
    }

def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def parse_timestamp(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

def jobs_root_for(run: Run) -> Path:
    """按创建时记录的 jobs 目录解析 artifacts；改设置不影响历史运行。"""
    return Path(run.jobs_dir) if run.jobs_dir else jobs_path()

def _seconds(start, finish) -> float | None:
    begin, end = parse_timestamp(start), parse_timestamp(finish)
    if not begin or not end:
        return None
    try:
        return max(0.0, (end - begin).total_seconds())
    except TypeError:  # naive 与 aware 混用
        return None

def estimate_cost(input_tokens: int | None, cached_tokens: int | None, output_tokens: int | None, tier: str = "standard") -> float | None:
    # Pier stats 不区分 cache-write token，因此估算不含 cache-write（$6.25/1M）项，
    # 对应字段在结果结构中恒为 null。
    if input_tokens is None and output_tokens is None:
        return None
    total_input, cached, output = input_tokens or 0, cached_tokens or 0, output_tokens or 0
    uncached = max(total_input - cached, 0)
    return (uncached * 5 + cached * .5 + output * 30) / 1_000_000 * TIER_MULTIPLIER.get(tier, 1.0)

def pier_trial_prefix(task: str) -> str:
    """pier 生成 trial 目录名时把任务名截断到 32 字符并去尾部 _-。"""
    return task[:32].rstrip("_-")

def _trial_stage(folder: Path, result: dict) -> str:
    if result:
        rewards = (result.get("verifier_result") or {}).get("rewards") or {}
        return "failed" if result.get("exception_info") or (rewards.get("reward") is not None and rewards.get("reward") < 1) else "completed"
    if (folder / "verifier" / "run.log").exists():
        return "verifier"
    if (folder / "artifacts" / "model.patch").exists():
        return "extracting_patch"
    agent = folder / "agent"
    if agent.exists() and any(p.is_file() for p in agent.rglob("*")):
        return "agent_running"
    if (folder / "config.json").exists():
        return "preparing_environment"
    return "queued"

def _canonical_task_name(folder: Path, data: dict, expected_tasks: list[str] | None = None) -> str:
    """Resolve Pier's truncated trial folder prefix back to the repository task id."""
    result_name = data.get("task_name")
    if isinstance(result_name, str) and result_name:
        return result_name.split("/")[-1]

    config = _json(folder / "config.json")
    task_path = ((config.get("task") or {}).get("path"))
    if isinstance(task_path, str) and task_path:
        configured_name = Path(task_path).name
        if configured_name:
            return configured_name

    prefix = folder.name.split("__", 1)[0]
    matches = [task for task in (expected_tasks or []) if pier_trial_prefix(task) == prefix]
    return matches[0] if len(matches) == 1 else prefix

def _trial(folder: Path, include_patch: bool = True, expected_tasks: list[str] | None = None) -> dict:
    data = _json(folder / "result.json")
    rewards = (data.get("verifier_result") or {}).get("rewards") or {}
    agent = data.get("agent_result") or {}
    info = data.get("agent_info") or {}
    task_name = _canonical_task_name(folder, data, expected_tasks)
    patch_path = folder / "artifacts" / "model.patch"
    patch = patch_path.read_text(encoding="utf-8", errors="replace") if include_patch and patch_path.exists() else ""
    exception = data.get("exception_info") or {}
    agent_execution = data.get("agent_execution") or {}
    failure_message = exception.get("exception_message")
    failure_type = exception.get("exception_type")
    agent_log_tail = ""
    if failure_type:
        chunks = []
        for path in (folder / "agent").glob("*.txt"):
            try:
                with path.open("rb") as handle:
                    handle.seek(0, 2)
                    handle.seek(max(handle.tell() - 200_000, 0))
                    chunks.append(handle.read().decode("utf-8", errors="replace"))
            except OSError:
                continue
        agent_log_tail = "\n".join(chunks)
    if is_transient_agent_failure(
        failure_type, f"{failure_message or ''}\n{agent_log_tail}"
    ):
        failure_type = "InfrastructureNetworkError"
    return {
        "id": folder.name,
        **task_identity(task_name),
        "status": _trial_stage(folder, data),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "duration_seconds": _seconds(data.get("started_at"), data.get("finished_at")),
        "agent_duration_seconds": _seconds(agent_execution.get("started_at"), agent_execution.get("finished_at")),
        "reward": rewards.get("reward"),
        "partial": rewards.get("partial"),
        "f2p": rewards.get("f2p"),
        "f2p_passed": rewards.get("f2p_passed"),
        "f2p_total": rewards.get("f2p_total"),
        "p2p": rewards.get("p2p"),
        "p2p_passed": rewards.get("p2p_passed"),
        "p2p_total": rewards.get("p2p_total"),
        "input_tokens": agent.get("n_input_tokens"),
        "cached_tokens": agent.get("n_cache_tokens"),
        "output_tokens": agent.get("n_output_tokens"),
        "reported_cost_usd": agent.get("cost_usd"),
        "steps": data.get("n_agent_steps") or agent.get("n_agent_steps"),
        "agent_version": info.get("version"),
        "failure_type": failure_type,
        "failure_message": (redact(str(failure_message), current_secrets())[:4000] if failure_message else None),
        "patch": patch[-100000:],
        "patch_bytes": patch_path.stat().st_size if patch_path.exists() else 0,
    }

def _enrich_trials(run: Run, trials: list[dict]) -> list[dict]:
    attempts: dict[str, int] = {}
    for trial in trials:
        task = trial["task"]
        attempts[task] = attempts.get(task, 0) + 1
        trial["attempt"] = attempts[task]
        trial["run_code"] = run_code(run.id)
        trial["trial_code"] = f"{run_code(run.id)} / {trial['task_code']} / A{attempts[task]:02d}"
        trial["resource_name"] = trial["id"] if "#" not in trial["id"] else None
    return trials

def trial_folder(run: Run, trial_id: str) -> Path | None:
    root = jobs_root_for(run) / run.job_name
    if not root.exists():
        return None
    return next((p for p in root.iterdir() if p.is_dir() and p.name == trial_id), None)

def trial_detail(run: Run, trial_id: str) -> dict | None:
    folder = trial_folder(run, trial_id)
    if not folder:
        return None
    expected_tasks = json.loads(run.tasks_json)
    root = jobs_root_for(run) / run.job_name
    folders = sorted((p for p in root.iterdir() if p.is_dir() and "__" in p.name), key=lambda p: p.name)
    trials = [_trial(item, include_patch=item == folder, expected_tasks=expected_tasks) for item in folders]
    _enrich_trials(run, trials)
    return next((trial for trial in trials if trial["id"] == trial_id), None)

def trial_log(run: Run, trial_id: str) -> str:
    folder = trial_folder(run, trial_id)
    if not folder:
        return ""
    candidates = [folder / "trial.log", folder / "agent" / "codex.txt", folder / "agent" / "claude-code.txt", folder / "agent" / "mini-swe-agent.txt", folder / "verifier" / "run.log"]
    return redact("\n\n".join(p.read_text(encoding="utf-8", errors="replace") for p in candidates if p.exists()), current_secrets())[-300000:]

def run_detail(run: Run, include_patches: bool = True) -> dict:
    root = jobs_root_for(run) / run.job_name
    job = _json(root / "result.json")
    stats = job.get("stats") or {}
    trial_folders = sorted((p for p in root.iterdir() if p.is_dir() and "__" in p.name), key=lambda p: p.name) if root.exists() else []
    expected_tasks = json.loads(run.tasks_json)
    trials = [_trial(folder, include_patch=include_patches, expected_tasks=expected_tasks) for folder in trial_folders]
    if run.status in {"failed", "cancelled", "interrupted"}:
        for trial in trials:
            if trial["status"] not in {"completed", "failed"}:
                trial["status"] = "failed"
                trial["failure_type"] = trial.get("failure_type") or "RunTerminated"
                trial["failure_message"] = trial.get("failure_message") or run.error
    # result.json 尚未写出时 trial 任务名回退到目录名（32 字符截断），归一回全名，
    # 否则占位补齐会产生幽灵 trial、compare 出现假任务行
    prefix_map = {}
    for task in expected_tasks:
        prefix_map.setdefault(pier_trial_prefix(task), task)
    for trial in trials:
        if trial["task"] not in expected_tasks:
            full = prefix_map.get(trial["task"])
            if full:
                trial.update(task_identity(full))
    expected = [(task, attempt) for task in expected_tasks for attempt in range(1, run.attempts_per_task + 1)]
    existing_tasks = [trial["task"] for trial in trials]
    for task, attempt in expected:
        if existing_tasks.count(task) < attempt:
            terminal = run.status in {"failed", "cancelled", "interrupted"}
            trials.append({"id": f"{task}#{attempt}", **task_identity(task), "status": "failed" if terminal else "queued", "reward": None, "partial": None, "duration_seconds": None, "agent_duration_seconds": None, "input_tokens": None, "cached_tokens": None, "output_tokens": None, "reported_cost_usd": None, "steps": None, "patch": "", "patch_bytes": 0, "failure_type": "RunTerminated" if terminal else None, "failure_message": run.error if terminal else None})
    _enrich_trials(run, trials)
    completed = sum(t["status"] in {"completed", "failed"} for t in trials)
    passed = sum(t.get("reward") == 1 for t in trials)
    stage = "completed" if run.status == "completed" else "failed" if run.status in {"failed", "cancelled", "interrupted"} else next((t["status"] for t in trials if t["status"] not in {"queued", "completed", "failed"}), run.status)
    total = len(expected)
    input_tokens = stats.get("n_input_tokens", run.input_tokens)
    cached_tokens = stats.get("n_cache_tokens", run.cached_tokens)
    output_tokens = stats.get("n_output_tokens", run.output_tokens)
    with SessionLocal() as db:
        baseline = db.scalar(select(Baseline).where(Baseline.run_id == run.id))
    return {
        "id": run.id, "run_code": run_code(run.id), "job_name": run.job_name, "status": run.status, "stage": stage,
        "agent": run.agent, "model": run.model, "reasoning_effort": run.reasoning_effort,
        "reasoning_effort_adapter": run.reasoning_effort_adapter,
        "reasoning_effort_effective": run.reasoning_effort_effective,
        "pier_version": run.pier_version,
        "tasks": expected_tasks, "attempts_per_task": run.attempts_per_task, "concurrency": run.concurrency,
        "agent_timeout_seconds": run.agent_timeout_seconds, "verifier_timeout_seconds": run.verifier_timeout_seconds,
        "verification": run.verification, "retry_infrastructure_errors": run.retry_infrastructure_errors,
        "infrastructure_max_retries": run.infrastructure_max_retries,
        "claude_max_turns": run.claude_max_turns,
        "codex_request_max_retries": run.codex_request_max_retries,
        "codex_stream_max_retries": run.codex_stream_max_retries,
        "codex_stream_idle_timeout_seconds": run.codex_stream_idle_timeout_seconds,
        "service_tier": run.service_tier,
        "created_at": run.created_at, "finished_at": run.finished_at, "passed": run.passed, "reward": run.reward,
        "input_tokens": input_tokens, "cached_tokens": cached_tokens, "uncached_input_tokens": max((input_tokens or 0) - (cached_tokens or 0), 0),
        "cache_write_tokens": None,  # Pier 未返回该字段，按约定存 null
        "output_tokens": output_tokens, "reported_cost_usd": stats.get("cost_usd", run.cost_usd),
        "estimated_cost_usd": estimate_cost(input_tokens, cached_tokens, output_tokens, run.service_tier),
        "error": run.error, "progress": {"completed": completed, "total": total, "passed": passed, "percent": round(completed / total * 100) if total else 0},
        "job_stats": {key: stats.get(key) for key in ("n_completed_trials", "n_errored_trials", "n_running_trials", "n_pending_trials", "n_cancelled_trials", "n_retries")},
        "trials": trials, "is_baseline": bool(baseline), "baseline_name": baseline.name if baseline else None,
    }

def list_details() -> list[dict]:
    with SessionLocal() as db:
        rows = db.scalars(select(Run).order_by(Run.id.desc())).all()
        return [run_detail(row, include_patches=False) for row in rows]

def compare_runs(run_ids: list[int], selections: list[tuple[int, str]] | None = None) -> dict:
    selected_pairs = set(selections or [])
    if selected_pairs:
        run_ids = list(dict.fromkeys(run_id for run_id, _ in selections or []))
    with SessionLocal() as db:
        rows = [db.get(Run, run_id) for run_id in run_ids]
    details = [run_detail(row, include_patches=False) for row in rows if row]
    task_names = (sorted({task for _, task in selected_pairs}) if selected_pairs
                  else sorted(set.intersection(*[set(detail["tasks"]) for detail in details])) if details else [])
    official = load_official_stats()
    matrix = []
    for task in task_names:
        item = {**task_identity(task), "runs": {}, "official": official.get(task) or None}
        for detail in details:
            if selected_pairs and (detail["id"], task) not in selected_pairs:
                continue
            values = [t for t in detail["trials"] if t["task"] == task and t.get("reward") is not None]
            durations = [t["duration_seconds"] for t in values if t.get("duration_seconds") is not None]
            item["runs"][str(detail["id"])] = {"pass_rate": mean(t["reward"] == 1 for t in values) if values else None, "reward": mean(t["reward"] for t in values) if values else None, "duration_seconds": mean(durations) if durations else None, "input_tokens": sum(t.get("input_tokens") or 0 for t in values) or None, "cached_tokens": sum(t.get("cached_tokens") or 0 for t in values) or None, "output_tokens": sum(t.get("output_tokens") or 0 for t in values) or None, "cost_usd": sum(t.get("reported_cost_usd") or 0 for t in values) or None, "steps": sum(t.get("steps") or 0 for t in values) or None}
        matrix.append(item)
    return {"runs": details, "tasks": matrix, "selections": [f"{run_id}:{task}" for run_id, task in selections or []]}

def _pass_rate(detail: dict) -> float:
    total = detail["progress"]["total"]
    return detail["progress"]["passed"] / total if total else 0

def _task_pass_rates(detail: dict) -> dict[str, float]:
    grouped: dict[str, list[bool]] = {}
    for trial in detail["trials"]:
        if trial.get("reward") is None:
            continue
        grouped.setdefault(trial["task"], []).append(trial["reward"] == 1)
    return {task: mean(values) for task, values in grouped.items()}

def _regression_reasons(current: dict, baseline: dict) -> list[str]:
    """Plan §18 的五条告警规则。"""
    reasons = []
    current_rate, baseline_rate = _pass_rate(current), _pass_rate(baseline)
    if baseline_rate - current_rate >= 4 / 28:
        reasons.append(f"总通过率下降 {(baseline_rate - current_rate) * 100:.1f} 个百分点")
    base_tasks, current_tasks = _task_pass_rates(baseline), _task_pass_rates(current)
    collapsed = sum(
        1 for task, base_value in base_tasks.items()
        if base_value >= .75 and current_tasks.get(task) is not None and current_tasks[task] <= .25
    )
    if collapsed >= 2:
        reasons.append(f"{collapsed} 个任务通过率显著下降")
    base_success = [t["duration_seconds"] for t in baseline["trials"] if t.get("reward") == 1 and t.get("duration_seconds")]
    current_success = [t["duration_seconds"] for t in current["trials"] if t.get("reward") == 1 and t.get("duration_seconds")]
    if base_success and current_success and mean(current_success) > mean(base_success) * 1.25:
        reasons.append("成功 Trial 平均耗时增加超过 25%")
    base_all = [t["duration_seconds"] for t in baseline["trials"] if t.get("duration_seconds")]
    current_all = [t["duration_seconds"] for t in current["trials"] if t.get("duration_seconds")]
    if base_all and current_all and mean(current_all) < mean(base_all) * 0.65 and current_rate < baseline_rate:
        reasons.append("平均耗时下降超过 35% 且通过率下降，疑似提前终止")
    control_current = current_tasks.get(CONTROL_TASK)
    control_base = base_tasks.get(CONTROL_TASK)
    if control_current is not None and control_current <= .25 and (control_base is None or control_base > .25):
        reasons.append(f"控制任务 {CONTROL_TASK} 通过率降至 1/4 或更低")
    return reasons

def regression_for(run: Run, current: dict | None = None) -> dict | None:
    current = current or run_detail(run, include_patches=False)
    official = load_official_stats()
    comparable = [(trial, official.get(trial["task"])) for trial in current["trials"] if official.get(trial["task"], {}).get("pass_rate") is not None]
    if not comparable: return None
    local_rate = mean(t.get("reward") == 1 for t, _ in comparable)
    official_rate = mean(s["pass_rate"] for _, s in comparable)
    delta = local_rate - official_rate
    reasons = [] if delta >= 0 else [f"通过率低于 DeepSWE 官方基线 {-delta * 100:.1f} 个百分点"]
    current_durations = [trial["duration_seconds"] for trial, _ in comparable if trial.get("duration_seconds") is not None]
    official_durations = [stats.get("avg_duration_seconds") for _, stats in comparable if stats.get("avg_duration_seconds") is not None]
    return {
        "baseline_run_id": None,
        "baseline_name": "DeepSWE 官方基线",
        "baseline_type": "official",
        "level": "warning" if reasons else "ok",
        "reasons": reasons,
        "pass_rate_delta": delta,
        "current_pass_rate": local_rate,
        "baseline_pass_rate": official_rate,
        "current_duration_seconds": mean(current_durations) if current_durations else None,
        "baseline_duration_seconds": mean(official_durations) if official_durations else None,
        "baseline_trials": sum(stats.get("trials") or 0 for _, stats in comparable),
    }

def task_catalog(tasks_dir: Path) -> list[dict]:
    history = list_details()
    official = load_official_stats()
    result = []
    for task_number, folder in enumerate(sorted(p for p in tasks_dir.iterdir() if p.is_dir()), 1):
        metadata = {}
        try:
            metadata = tomllib.loads((folder / "task.toml").read_text(encoding="utf-8")).get("metadata", {})
        except (OSError, tomllib.TOMLDecodeError):
            pass
        trials = [trial for run in history for trial in run["trials"] if trial["task"] == folder.name and trial.get("reward") is not None]
        stats = official.get(folder.name) or {}
        result.append({"id": folder.name, "task_number": task_number, "code": f"TASK-{task_number:03d}", "title": metadata.get("display_title") or folder.name, "description": metadata.get("display_description") or "", "language": metadata.get("language"), "category": metadata.get("category"), "repository_url": metadata.get("repository_url"), "local_trials": len(trials), "local_pass_rate": mean(t["reward"] == 1 for t in trials) if trials else None, "last_failure": next((t.get("failure_type") or t.get("failure_message") for t in reversed(trials) if t.get("reward") != 1), None), "official_pass_rate": stats.get("pass_rate"), "official_avg_duration_seconds": stats.get("avg_duration_seconds"), "official_trials": stats.get("trials")})
    return result
