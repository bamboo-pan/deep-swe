import json
import re
import tomllib
from datetime import datetime
from pathlib import Path
from statistics import mean
from sqlalchemy import select
from .database import SessionLocal
from .config import settings
from .models import Baseline, Run
from .official_stats import configuration_stats, load_official_stats, normalize_model_name
from .preferences import current_secrets, jobs_path
from .pier_retry_patch.transient import is_transient_agent_failure
from .pricing import DEFAULT_PRICING, pricing_for_model
from .scheduler import queue_status
from .security import redact
from .task_suite import CONTROL_TASK

TIER_MULTIPLIER = {"standard": 1.0, "batch": 0.5, "priority": 2.0}
TRIAL_TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}

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

def _agent_log_tail(folder: Path, max_bytes: int = 200_000) -> str:
    chunks = []
    for path in (folder / "agent").glob("*.txt"):
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                handle.seek(max(handle.tell() - max_bytes, 0))
                chunks.append(handle.read().decode("utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)

def _agent_hit_execution_limit(log_tail: str) -> bool:
    lines = [line.strip() for line in log_tail.splitlines()]
    return any(
        current == "Exit:" and following == "LimitsExceeded"
        for current, following in zip(lines, lines[1:])
    )

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

def estimate_cost(input_tokens: int | None, cached_tokens: int | None, output_tokens: int | None, tier: str = "standard", model: str | None = None) -> float | None:
    # 单价取自 models.dev 快照（见 pricing.py），未收录的模型退回 DEFAULT_PRICING。
    # Pier stats 不区分 cache-write token，因此估算不含 cache-write 项，
    # 对应字段在结果结构中恒为 null。
    if input_tokens is None and output_tokens is None:
        return None
    pricing = pricing_for_model(model) or DEFAULT_PRICING
    total_input, cached, output = input_tokens or 0, cached_tokens or 0, output_tokens or 0
    uncached = max(total_input - cached, 0)
    cache_read = pricing.get("cache_read", pricing["input"])  # 无缓存折扣价的模型按原价计
    amount = uncached * pricing["input"] + cached * cache_read + output * pricing["output"]
    return amount / 1_000_000 * TIER_MULTIPLIER.get(tier, 1.0)

def _pricing_details(model: str | None) -> dict:
    """估算所用单价及其来源，供 UI/API 消费者核对估算口径。"""
    found = pricing_for_model(model)
    return {**(found or DEFAULT_PRICING), "source": "models.dev" if found else "default"}

def pier_trial_prefix(task: str) -> str:
    """pier 生成 trial 目录名时把任务名截断到 32 字符并去尾部 _-。"""
    return task[:32].rstrip("_-")

def _trial_stage(folder: Path, result: dict) -> str:
    if result:
        rewards = (result.get("verifier_result") or {}).get("rewards") or {}
        return "failed" if result.get("exception_info") or (rewards.get("reward") is not None and rewards.get("reward") < 1) else "completed"
    retry_status = _json(folder / "retry-state.json").get("status")
    if isinstance(retry_status, str) and retry_status:
        return retry_status
    verifier = folder / "verifier"
    if (verifier / "reward.json").exists() or (verifier / "reward.txt").exists():
        return "finalizing"
    if (verifier / "run.log").exists():
        return "verifier"
    if (folder / "artifacts" / "model.patch").exists():
        config = _json(folder / "config.json")
        if ((config.get("verifier") or {}).get("disable") is True):
            return "finalizing"
        return "preparing_verifier"
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
    config = _json(folder / "config.json")
    retry_state = _json(folder / "retry-state.json") if not data else {}
    rewards = (data.get("verifier_result") or {}).get("rewards") or {}
    agent = data.get("agent_result") or {}
    info = data.get("agent_info") or {}
    task_name = _canonical_task_name(folder, data, expected_tasks)
    patch_path = folder / "artifacts" / "model.patch"
    patch_bytes = patch_path.stat().st_size if patch_path.exists() else 0
    patch = patch_path.read_text(encoding="utf-8", errors="replace") if include_patch and patch_path.exists() else ""
    exception = data.get("exception_info") or {}
    agent_execution = data.get("agent_execution") or {}
    failure_message = exception.get("exception_message") or retry_state.get("failure_message")
    failure_type = exception.get("exception_type") or retry_state.get("failure_type")
    reward = rewards.get("reward")
    score_parts = []
    baseline_score_parts = []
    score_descriptions = {
        "f2p": "目标失败测试修复（F2P）",
        "p2p": "原有通过测试保持（P2P）",
    }
    for label in ("f2p", "p2p"):
        passed, total = rewards.get(f"{label}_passed"), rewards.get(f"{label}_total")
        if passed is not None and total is not None:
            score_parts.append(f"{label.upper()} {passed}/{total}")
            baseline_score_parts.append(f"{score_descriptions[label]} {passed}/{total}")
    score_summary = "，".join(score_parts)
    baseline_score_summary = "，".join(baseline_score_parts)
    agent_log_tail = ""
    if failure_type or (reward is not None and reward < 1 and patch_bytes == 0):
        agent_log_tail = _agent_log_tail(folder)
    if is_transient_agent_failure(
        failure_type,
        failure_message,
        agent_log_tail=agent_log_tail,
    ):
        failure_type = "InfrastructureNetworkError"
    # 守护线程按单 Trial 限额掐容器时会留下 guard.json；容器被杀的报错样子像
    # 基础设施故障，必须让护栏原因盖过 transient 归类，避免误导排障
    guard_reason = _json(folder / "guard.json").get("reason")
    if guard_reason:
        failure_type = "UsageGuardTerminated"
        failure_message = guard_reason + (f"；{failure_message}" if failure_message else "")
    if (
        not failure_type
        and reward is not None
        and reward < 1
        and patch_bytes == 0
        and _agent_hit_execution_limit(agent_log_tail)
    ):
        kwargs = (((data.get("config") or {}).get("agent") or {}).get("kwargs") or {})
        cost, cost_limit = agent.get("cost_usd"), kwargs.get("cost_limit")
        numeric_cost = isinstance(cost, (int, float)) and not isinstance(cost, bool)
        numeric_limit = isinstance(cost_limit, (int, float)) and not isinstance(cost_limit, bool)
        if numeric_cost and numeric_limit and cost >= cost_limit:
            failure_type = "CostLimitExceeded"
            failure_message = (
                f"本次 Agent 费用达到单 Trial 上限（${cost:.2f} / ${cost_limit:.2f}），任务自动停止；"
                "评分系统未收到代码补丁（model.patch 为空）"
            )
        else:
            failure_type = "AgentLimitExceeded"
            steps = data.get("n_agent_steps") or agent.get("n_agent_steps")
            step_detail = (
                f"已执行 {steps} 步，"
                if isinstance(steps, int) and not isinstance(steps, bool) and steps > 0
                else ""
            )
            failure_message = (
                f"本次 Agent {step_detail}达到设置的步数上限后自动停止；"
                "评分系统未收到代码补丁（model.patch 为空）"
            )
        if baseline_score_summary:
            failure_message += f"；因此使用原始代码评测：{baseline_score_summary}"
    if not failure_type and reward is not None and reward < 1:
        failure_type = "VerificationFailed"
        failure_message = "验证未通过" + (f"：{score_summary}" if score_summary else f"（reward {reward}）")
    redacted_failure = redact(str(failure_message), current_secrets())[:4000] if failure_message else None
    collapsed_failure = " ".join(redacted_failure.split()) if redacted_failure else None
    retry_of = data.get("deepswe_retry_of") or config.get("deepswe_retry_of")
    retry_batch = data.get("deepswe_retry_batch") or config.get("deepswe_retry_batch")
    retry_job_name = data.get("deepswe_retry_job_name") or config.get("deepswe_retry_job_name")
    retry_resource_id = data.get("deepswe_retry_resource_id") or config.get("deepswe_retry_resource_id")
    attempt = data.get("deepswe_attempt") or config.get("deepswe_attempt")
    retry_status = retry_state.get("status")
    retrying = bool(
        config.get("deepswe_retrying")
        and retry_status not in TRIAL_TERMINAL_STATES
    )
    replaced = bool(data.get("deepswe_replaced") or config.get("deepswe_replaced"))
    return {
        "id": folder.name,
        **task_identity(task_name),
        "status": _trial_stage(folder, data),
        "attempt": attempt if isinstance(attempt, int) and attempt > 0 else None,
        "retrying": retrying,
        "replaced": replaced,
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "duration_seconds": _seconds(data.get("started_at"), data.get("finished_at")),
        "agent_duration_seconds": _seconds(agent_execution.get("started_at"), agent_execution.get("finished_at")),
        "reward": reward,
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
        "failure_message": redacted_failure,
        "failure_summary": (
            f"{failure_type}: {collapsed_failure}"[:320]
            if failure_type and collapsed_failure else failure_type or collapsed_failure
        ),
        "retry_of": retry_of,
        "retry_batch": retry_batch,
        "retry_job_name": retry_job_name,
        "retry_resource_id": retry_resource_id,
        "patch": patch[-100000:],
        "patch_bytes": patch_bytes,
    }

def _enrich_trials(run: Run, trials: list[dict]) -> list[dict]:
    attempts: dict[str, int] = {}
    for trial in trials:
        task = trial["task"]
        attempt = trial.get("attempt")
        if not isinstance(attempt, int) or attempt < 1:
            attempt = attempts.get(task, 0) + 1
            trial["attempt"] = attempt
        attempts[task] = max(attempts.get(task, 0), attempt)
        trial["run_code"] = run_code(run.id)
        trial["trial_code"] = f"{run_code(run.id)} / {trial['task_code']} / A{attempt:02d}"
        trial["resource_name"] = trial["id"] if "#" not in trial["id"] else None
    return trials

def deleted_trial_entries(run: Run) -> list[dict]:
    """Validated tombstones for Trial rows intentionally removed from a terminal Run."""
    try:
        raw = json.loads(run.deleted_trials_json or "[]")
        expected_tasks = set(json.loads(run.tasks_json))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    result, seen_ids, seen_slots = [], set(), set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        trial_id, task, attempt = item.get("id"), item.get("task"), item.get("attempt")
        if (
            not isinstance(trial_id, str) or not trial_id or len(trial_id) > 300
            or task not in expected_tasks or not isinstance(attempt, int)
            or attempt < 1 or attempt > run.attempts_per_task
            or trial_id in seen_ids or (task, attempt) in seen_slots
        ):
            continue
        result.append({"id": trial_id, "task": task, "attempt": attempt})
        seen_ids.add(trial_id)
        seen_slots.add((task, attempt))
    return result

def trial_folder(run: Run, trial_id: str) -> Path | None:
    root = jobs_root_for(run) / run.job_name
    if not root.exists():
        return None
    return next((p for p in root.iterdir() if p.is_dir() and p.name == trial_id), None)

def _retry_live_entries(
    run: Run,
    markers: list[dict],
    expected_tasks: list[str],
    include_patches: bool,
) -> dict[str, dict]:
    if not markers:
        return {}
    jobs_root = jobs_root_for(run).resolve()
    retry_base = f"retry-{run.job_name}"
    mapping = {}
    remaining = []
    for marker in markers:
        retry_name = marker.get("retry_job_name")
        resource_id = marker.get("retry_resource_id")
        retry_batch = marker.get("retry_batch")
        expected_retry_name = (
            f"{retry_base}-{retry_batch[:12]}"
            if isinstance(retry_batch, str)
            and re.fullmatch(r"[0-9a-f]{32}", retry_batch)
            else retry_base
        )
        if not (
            isinstance(retry_name, str)
            and Path(retry_name).name == retry_name
            and retry_name == expected_retry_name
            and isinstance(resource_id, str)
            and Path(resource_id).name == resource_id
            and "__" in resource_id
        ):
            remaining.append(marker)
            continue
        retry_root = (jobs_root / retry_name).resolve()
        folder = (retry_root / resource_id).resolve()
        if (
            retry_root.parent != jobs_root
            or folder.parent != retry_root
            or not folder.is_dir()
        ):
            remaining.append(marker)
            continue
        mapping[marker["id"]] = {
            "folder": folder,
            "trial": _trial(
                folder,
                include_patch=include_patches,
                expected_tasks=expected_tasks,
            ),
        }

    retry_root = jobs_root / retry_base
    if not remaining or not retry_root.is_dir():
        return mapping
    live_entries = [
        {
            "folder": folder,
            "trial": _trial(
                folder,
                include_patch=include_patches,
                expected_tasks=expected_tasks,
            ),
        }
        for folder in sorted(
            (path for path in retry_root.iterdir() if path.is_dir() and "__" in path.name),
            key=lambda path: path.name,
        )
    ]
    markers_by_task: dict[str, list[dict]] = {}
    live_by_task: dict[str, list[dict]] = {}
    for marker in remaining:
        markers_by_task.setdefault(marker["task"], []).append(marker)
    for entry in live_entries:
        live_by_task.setdefault(entry["trial"]["task"], []).append(entry)

    for task, task_markers in markers_by_task.items():
        task_markers.sort(key=lambda trial: (trial.get("attempt") or 0, trial["id"]))
        task_live = sorted(
            live_by_task.get(task, []),
            key=lambda entry: (
                entry["trial"].get("started_at") is None,
                str(entry["trial"].get("started_at") or ""),
                entry["trial"]["id"],
            ),
        )
        for marker, entry in zip(task_markers, task_live):
            mapping[marker["id"]] = entry
    return mapping

def _retry_markers(run: Run, expected_tasks: list[str]) -> list[dict]:
    root = jobs_root_for(run) / run.job_name
    if not root.is_dir():
        return []
    return [
        marker
        for marker in (
            _trial(folder, include_patch=False, expected_tasks=expected_tasks)
            for folder in root.iterdir()
            if folder.is_dir() and "__" in folder.name
        )
        if marker.get("retrying")
    ]

def trial_detail(run: Run, trial_id: str) -> dict | None:
    summary = next((trial for trial in run_detail(run, include_patches=False)["trials"] if trial["id"] == trial_id), None)
    if not summary:
        return None
    if summary.get("retrying"):
        expected_tasks = json.loads(run.tasks_json)
        live = _retry_live_entries(
            run,
            _retry_markers(run, expected_tasks),
            expected_tasks,
            include_patches=True,
        ).get(trial_id)
        if not live:
            return summary
        detailed = live["trial"]
        for key in (
            "id", "task", "task_slug", "task_number", "task_code", "task_title",
            "attempt", "run_code", "trial_code", "retrying", "replaced",
            "retry_batch", "retry_job_name", "retry_resource_id",
        ):
            detailed[key] = summary.get(key)
        detailed["resource_name"] = live["folder"].name
        return detailed
    folder = trial_folder(run, trial_id)
    if not folder:
        return summary
    detailed = _trial(folder, include_patch=True, expected_tasks=json.loads(run.tasks_json))
    for key in ("attempt", "run_code", "trial_code", "resource_name"):
        detailed[key] = summary.get(key)
    return detailed

def trial_log(run: Run, trial_id: str) -> str:
    if trial_id in {item["id"] for item in deleted_trial_entries(run)}:
        return ""
    folder = trial_folder(run, trial_id)
    if not folder:
        return ""
    expected_tasks = json.loads(run.tasks_json)
    marker = _trial(folder, include_patch=False, expected_tasks=expected_tasks)
    if marker.get("retrying"):
        live = _retry_live_entries(
            run,
            _retry_markers(run, expected_tasks),
            expected_tasks,
            include_patches=False,
        ).get(trial_id)
        if live:
            folder = live["folder"]
    candidates = [folder / "trial.log", folder / "agent" / "codex.txt", folder / "agent" / "claude-code.txt", folder / "agent" / "mini-swe-agent.txt", folder / "verifier" / "run.log"]
    chunks = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return redact("\n\n".join(chunks), current_secrets())[-300000:]

def run_detail(run: Run, include_patches: bool = True) -> dict:
    root = jobs_root_for(run) / run.job_name
    job = _json(root / "result.json")
    stats = job.get("stats") or {}
    expected_tasks = json.loads(run.tasks_json)
    deleted_entries = deleted_trial_entries(run)
    deleted_ids = {item["id"] for item in deleted_entries}
    deleted_attempts = {(item["task"], item["attempt"]) for item in deleted_entries}
    trial_folders = sorted(
        (p for p in root.iterdir() if p.is_dir() and "__" in p.name and p.name not in deleted_ids),
        key=lambda p: p.name,
    ) if root.exists() else []
    trials = [_trial(folder, include_patch=include_patches, expected_tasks=expected_tasks) for folder in trial_folders]
    terminal_defaults = {
        "failed": ("failed", "RunFailed", "Run 失败，Trial 未完成"),
        "cancelled": ("cancelled", "RunCancelled", "Run 已取消，Trial 未完成"),
        "interrupted": ("interrupted", "RunInterrupted", "Run 被中断，Trial 未完成"),
        "completed": ("failed", "ResultMissing", "Run 已结束，但 Trial 结果缺失"),
    }
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
    retry_markers = [trial for trial in trials if trial.get("retrying")]
    live_entries = _retry_live_entries(
        run,
        retry_markers,
        expected_tasks,
        include_patches,
    )
    for marker in retry_markers:
        entry = live_entries.get(marker["id"])
        if not entry:
            continue
        preserved = {
            key: marker.get(key)
            for key in (
                "id", "task", "task_slug", "task_number", "task_code",
                "task_title", "attempt", "retrying", "replaced",
                "retry_batch", "retry_job_name", "retry_resource_id",
            )
        }
        marker.update(entry["trial"])
        marker.update(preserved)
    if run.status in terminal_defaults:
        status, failure_type, default_message = terminal_defaults[run.status]
        for trial in trials:
            if trial["status"] not in TRIAL_TERMINAL_STATES:
                trial["status"] = status
                trial["failure_type"] = trial.get("failure_type") or failure_type
                trial["failure_message"] = trial.get("failure_message") or run.error or default_message
                trial["failure_summary"] = f"{trial['failure_type']}: {trial['failure_message']}"[:320]
    for task in expected_tasks:
        task_trials = sorted(
            (trial for trial in trials if trial["task"] == task),
            key=lambda trial: (
                trial.get("started_at") is None,
                str(trial.get("started_at") or ""),
                trial["id"],
            ),
        )
        available_attempts = [
            attempt for attempt in range(1, run.attempts_per_task + 1)
            if (task, attempt) not in deleted_attempts
        ]
        claimed_attempts: set[int] = set()
        unassigned = []
        for trial in task_trials:
            attempt = trial.get("attempt")
            if isinstance(attempt, int) and attempt > 0 and attempt not in claimed_attempts:
                claimed_attempts.add(attempt)
            else:
                trial.pop("attempt", None)
                unassigned.append(trial)
        remaining_attempts = [
            attempt for attempt in available_attempts if attempt not in claimed_attempts
        ]
        next_extra_attempt = max(
            [run.attempts_per_task, *claimed_attempts],
            default=run.attempts_per_task,
        ) + 1
        for trial in unassigned:
            if remaining_attempts:
                attempt = remaining_attempts.pop(0)
            else:
                attempt = next_extra_attempt
                next_extra_attempt += 1
            trial["attempt"] = attempt
            claimed_attempts.add(attempt)
        for attempt in available_attempts:
            if attempt in claimed_attempts:
                continue
            if run.status in terminal_defaults:
                status, failure_type, default_message = terminal_defaults[run.status]
                failure_message = run.error or default_message
            else:
                status, failure_type, failure_message = "queued", None, None
            trials.append({
                "id": f"{task}#{attempt}", **task_identity(task), "attempt": attempt,
                "status": status, "reward": None, "partial": None, "f2p": None, "p2p": None,
                "duration_seconds": None, "agent_duration_seconds": None,
                "input_tokens": None, "cached_tokens": None, "output_tokens": None,
                "reported_cost_usd": None, "steps": None, "patch": "", "patch_bytes": 0,
                "failure_type": failure_type, "failure_message": failure_message,
                "failure_summary": f"{failure_type}: {failure_message}"[:320] if failure_type and failure_message else failure_type,
                "retrying": False, "replaced": False,
            })
    task_order = {task: index for index, task in enumerate(expected_tasks)}
    trials.sort(key=lambda trial: (task_order.get(trial["task"], len(task_order)), trial.get("attempt", 0), trial["id"]))
    _enrich_trials(run, trials)
    completed = sum(t["status"] in TRIAL_TERMINAL_STATES for t in trials)
    passed = sum(t.get("reward") == 1 for t in trials)
    remaining_tasks = {trial["task"] for trial in trials}
    passed_tasks = sum(
        any(t.get("reward") == 1 for t in trials if t["task"] == task)
        for task in expected_tasks if task in remaining_tasks
    )
    queue = queue_status(run.id)
    if run.status not in terminal_defaults and not queue["running"] and queue["queued"]:
        stage = "queued"
    else:
        stage = run.status if run.status in terminal_defaults else next((t["status"] for t in trials if t["status"] not in {"queued", "completed", "failed"}), run.status)
    total = len(trials)
    configured_visible_total = max(
        len(expected_tasks) * run.attempts_per_task - len(deleted_entries), 0
    )
    has_targeted_retries = (
        len(trial_folders) > configured_visible_total
        or any(trial.get("retry_of") for trial in trials)
        or any(trial.get("retrying") or trial.get("replaced") for trial in trials)
    )
    if deleted_entries or has_targeted_retries:
        def visible_total(field: str):
            values = [trial.get(field) for trial in trials if trial.get(field) is not None]
            return sum(values) if values else None
        input_tokens = visible_total("input_tokens")
        cached_tokens = visible_total("cached_tokens")
        output_tokens = visible_total("output_tokens")
        reported_cost = visible_total("reported_cost_usd")
    else:
        input_tokens = stats.get("n_input_tokens", run.input_tokens)
        cached_tokens = stats.get("n_cache_tokens", run.cached_tokens)
        output_tokens = stats.get("n_output_tokens", run.output_tokens)
        reported_cost = stats.get("cost_usd", run.cost_usd)
    with SessionLocal() as db:
        baseline = db.scalar(select(Baseline).where(Baseline.run_id == run.id))
    return {
        "id": run.id, "run_code": run_code(run.id), "job_name": run.job_name, "status": run.status, "stage": stage,
        "agent": run.agent, "model": run.model, "reasoning_effort": run.reasoning_effort,
        "reasoning_effort_adapter": run.reasoning_effort_adapter,
        "reasoning_effort_effective": run.reasoning_effort_effective,
        "pier_version": run.pier_version,
        "tasks": expected_tasks, "attempts_per_task": run.attempts_per_task, "concurrency": run.concurrency,
        "queue": queue,
        "agent_timeout_seconds": run.agent_timeout_seconds, "verifier_timeout_seconds": run.verifier_timeout_seconds,
        "verification": run.verification, "retry_infrastructure_errors": run.retry_infrastructure_errors,
        "infrastructure_max_retries": run.infrastructure_max_retries,
        "agent_max_steps": run.agent_max_steps,
        "codex_request_max_retries": run.codex_request_max_retries,
        "codex_stream_max_retries": run.codex_stream_max_retries,
        "codex_stream_idle_timeout_seconds": run.codex_stream_idle_timeout_seconds,
        "service_tier": run.service_tier,
        "created_at": run.created_at, "finished_at": run.finished_at, "passed": run.passed, "reward": run.reward,
        "input_tokens": input_tokens, "cached_tokens": cached_tokens, "uncached_input_tokens": max((input_tokens or 0) - (cached_tokens or 0), 0),
        "cache_write_tokens": None,  # Pier 未返回该字段，按约定存 null
        "output_tokens": output_tokens, "reported_cost_usd": reported_cost,
        "estimated_cost_usd": estimate_cost(input_tokens, cached_tokens, output_tokens, run.service_tier, run.model),
        "pricing": _pricing_details(run.model),
        "error": run.error, "progress": {"completed": completed, "total": total, "passed": passed, "percent": round(completed / total * 100) if total else 0},
        "task_progress": {"passed": passed_tasks, "total": len(remaining_tasks)},
        "deleted_trials": len(deleted_entries),
        "job_stats": {key: stats.get(key) for key in ("n_completed_trials", "n_errored_trials", "n_running_trials", "n_pending_trials", "n_cancelled_trials", "n_retries")},
        "trials": trials, "is_baseline": bool(baseline), "baseline_name": baseline.name if baseline else None,
    }

def run_task_progress(run: Run) -> dict:
    """Return task-level pass counts without building the full run detail payload."""
    expected_tasks = json.loads(run.tasks_json)
    root = jobs_root_for(run) / run.job_name
    deleted_entries = deleted_trial_entries(run)
    deleted_ids = {item["id"] for item in deleted_entries}
    deleted_slots = {(item["task"], item["attempt"]) for item in deleted_entries}
    remaining_tasks = {
        task for task in expected_tasks
        if any((task, attempt) not in deleted_slots for attempt in range(1, run.attempts_per_task + 1))
    }
    passed_tasks: set[str] = set()
    if root.exists():
        for folder in (p for p in root.iterdir() if p.is_dir() and "__" in p.name and p.name not in deleted_ids):
            data = _json(folder / "result.json")
            reward = ((data.get("verifier_result") or {}).get("rewards") or {}).get("reward")
            if reward == 1:
                passed_tasks.add(_canonical_task_name(folder, data, expected_tasks))
    return {"passed": sum(task in passed_tasks for task in remaining_tasks), "total": len(remaining_tasks)}

def run_trial_progress(run: Run) -> dict:
    """Return Trial-level progress, including in-place targeted replacements."""
    expected_tasks = json.loads(run.tasks_json)
    deleted_ids = {item["id"] for item in deleted_trial_entries(run)}
    configured_total = max(len(expected_tasks) * run.attempts_per_task - len(deleted_ids), 0)
    root = jobs_root_for(run) / run.job_name
    passed = 0
    completed = 0
    actual = 0
    if root.exists():
        for folder in (p for p in root.iterdir() if p.is_dir() and "__" in p.name and p.name not in deleted_ids):
            actual += 1
            result_path = folder / "result.json"
            data = _json(result_path)
            reward = ((data.get("verifier_result") or {}).get("rewards") or {}).get("reward")
            passed += reward == 1
            completed += result_path.exists()
    total = max(configured_total, actual)
    if run.status in {"failed", "cancelled", "interrupted", "completed"}:
        completed = total
    completed = min(completed, total)
    return {
        "completed": completed,
        "total": total,
        "passed": passed,
        "percent": round(completed / total * 100) if total else 0,
    }

def aggregate_trial_results(run: Run) -> dict:
    """Aggregate persisted Trial result files, including targeted replacements."""
    root = jobs_root_for(run) / run.job_name
    deleted_entries = deleted_trial_entries(run)
    deleted_ids = {item["id"] for item in deleted_entries}
    expected_tasks = json.loads(run.tasks_json)
    deleted_per_task: dict[str, int] = {}
    for item in deleted_entries:
        deleted_per_task[item["task"]] = deleted_per_task.get(item["task"], 0) + 1
    rewards: list[float] = []
    input_tokens: list[int] = []
    cached_tokens: list[int] = []
    output_tokens: list[int] = []
    costs: list[float] = []
    trial_count = 0
    result_count = 0
    errored = 0
    result_rows: list[tuple[str, bool, str | None]] = []
    results_per_task: dict[str, int] = {}

    if root.exists():
        for folder in (
            path for path in root.iterdir()
            if path.is_dir() and "__" in path.name and path.name not in deleted_ids
        ):
            trial_count += 1
            result_path = folder / "result.json"
            data = _json(result_path)
            if not data:
                continue
            result_count += 1
            has_error = bool(data.get("exception_info"))
            errored += has_error
            retry_of = data.get("deepswe_retry_of")
            result_rows.append((folder.name, has_error, retry_of if isinstance(retry_of, str) else None))
            task = _canonical_task_name(folder, data, expected_tasks)
            results_per_task[task] = results_per_task.get(task, 0) + 1
            reward = ((data.get("verifier_result") or {}).get("rewards") or {}).get("reward")
            if isinstance(reward, (int, float)) and not isinstance(reward, bool):
                rewards.append(float(reward))
            agent = data.get("agent_result") or {}
            for values, key in (
                (input_tokens, "n_input_tokens"),
                (cached_tokens, "n_cache_tokens"),
                (output_tokens, "n_output_tokens"),
                (costs, "cost_usd"),
            ):
                value = agent.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values.append(value)

    superseded_ids = {retry_of for _trial_id, _error, retry_of in result_rows if retry_of}
    effective_errored = sum(
        has_error for trial_id, has_error, _retry_of in result_rows
        if trial_id not in superseded_ids
    )
    missing_configured = sum(
        max(
            run.attempts_per_task - deleted_per_task.get(task, 0) - results_per_task.get(task, 0),
            0,
        )
        for task in expected_tasks
    )
    return {
        "trial_count": trial_count,
        "result_count": result_count,
        "errored": errored,
        "effective_errored": effective_errored,
        "missing_configured": missing_configured,
        "reward": sum(rewards) / len(rewards) if rewards else None,
        "passed": bool(trial_count) and len(rewards) == trial_count and all(value == 1 for value in rewards),
        "input_tokens": sum(input_tokens) if input_tokens else None,
        "cached_tokens": sum(cached_tokens) if cached_tokens else None,
        "output_tokens": sum(output_tokens) if output_tokens else None,
        "cost_usd": sum(costs) if costs else None,
    }

def list_details() -> list[dict]:
    with SessionLocal() as db:
        rows = db.scalars(select(Run).order_by(Run.id.desc())).all()
        return [run_detail(row, include_patches=False) for row in rows]

def compare_runs(run_ids: list[int], selections: list[tuple[int, str]] | None = None) -> dict:
    selection_pairs = list(dict.fromkeys(selections or []))
    selected_pairs = set(selection_pairs)
    if selection_pairs:
        run_ids = list(dict.fromkeys(run_id for run_id, _ in selection_pairs))
    with SessionLocal() as db:
        rows = [db.get(Run, run_id) for run_id in run_ids]
    details = [run_detail(row, include_patches=False) for row in rows if row]
    selected_trials: dict[int, list[dict]] = {}
    if selected_pairs:
        for detail in details:
            by_id = {trial["id"]: trial for trial in detail["trials"]}
            chosen = []
            seen_ids: set[str] = set()
            for run_id, item_id in selection_pairs:
                if run_id != detail["id"]:
                    continue
                # New selections identify one exact Trial. Task ids remain accepted so
                # old URLs/API clients continue to select every attempt for that task.
                candidates = [by_id[item_id]] if item_id in by_id else [
                    trial for trial in detail["trials"] if trial["task"] == item_id
                ]
                for trial in candidates:
                    if trial["id"] not in seen_ids:
                        seen_ids.add(trial["id"])
                        chosen.append(trial)
            selected_trials[detail["id"]] = chosen
    task_names = (
        sorted({trial["task"] for trials in selected_trials.values() for trial in trials})
        if selected_pairs
        else sorted(set.intersection(*[set(detail["tasks"]) for detail in details])) if details else []
    )
    official = load_official_stats()
    matrix = []
    for task in task_names:
        task_official = official.get(task) or None
        item = {**task_identity(task), "runs": {}, "official": task_official, "official_configurations": []}
        seen_configurations: set[tuple[str, str]] = set()
        for detail in details:
            task_trials = (
                [trial for trial in selected_trials.get(detail["id"], []) if trial["task"] == task]
                if selected_pairs
                else [trial for trial in detail["trials"] if trial["task"] == task]
            )
            if selected_pairs and not task_trials:
                continue
            configuration_key = (normalize_model_name(detail["model"]), detail["reasoning_effort"].lower())
            if configuration_key not in seen_configurations:
                seen_configurations.add(configuration_key)
                exact = configuration_stats(task_official, detail["model"], detail["reasoning_effort"])
                item["official_configurations"].append({
                    "model": detail["model"],
                    "reasoning_effort": detail["reasoning_effort"],
                    "available": exact is not None,
                    **(exact or {}),
                })
            values = [trial for trial in task_trials if trial.get("reward") is not None]
            durations = [t["duration_seconds"] for t in values if t.get("duration_seconds") is not None]
            def average(field: str) -> float | None:
                present = [t[field] for t in values if t.get(field) is not None]
                return mean(present) if present else None
            estimated_costs = [
                estimate_cost(t.get("input_tokens"), t.get("cached_tokens"), t.get("output_tokens"), detail["service_tier"], detail["model"])
                for t in values
            ]
            estimated_costs = [value for value in estimated_costs if value is not None]
            item["runs"][str(detail["id"])] = {
                "passed": any(t["reward"] == 1 for t in values) if values else None,
                "attempts": len(task_trials),
                "measured_attempts": len(values),
                "trial_ids": [trial["id"] for trial in task_trials],
                "pass_rate": mean(t["reward"] == 1 for t in values) if values else None,
                "reward": mean(t["reward"] for t in values) if values else None,
                "duration_seconds": mean(durations) if durations else None,
                "input_tokens": average("input_tokens"),
                "cached_tokens": average("cached_tokens"),
                "output_tokens": average("output_tokens"),
                "cost_usd": average("reported_cost_usd"),
                "steps": average("steps"),
                "total_input_tokens": sum(t.get("input_tokens") or 0 for t in values) if values else None,
                "total_cost_usd": sum(t.get("reported_cost_usd") or 0 for t in values) if values else None,
                "total_estimated_cost_usd": sum(estimated_costs) if estimated_costs else None,
            }
        matrix.append(item)
    return {"runs": details, "tasks": matrix, "selections": [f"{run_id}:{item_id}" for run_id, item_id in selection_pairs]}

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
    baseline_trials = sum(
        trial.get("reward") is not None for trial in baseline.get("trials", [])
    )
    significant_losses = max(1, round(baseline_trials / 7)) if baseline_trials else 1
    if baseline_trials and baseline_rate - current_rate >= significant_losses / baseline_trials:
        reasons.append(f"总通过率下降 {(baseline_rate - current_rate) * 100:.1f} 个百分点")
    base_tasks, current_tasks = _task_pass_rates(baseline), _task_pass_rates(current)
    collapsed = sum(
        1 for task, base_value in base_tasks.items()
        if base_value >= .75 and current_tasks.get(task) is not None and current_tasks[task] <= .25
    )
    collapse_threshold = max(1, round(len(base_tasks) * 2 / 7)) if base_tasks else 1
    if collapsed >= collapse_threshold:
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
