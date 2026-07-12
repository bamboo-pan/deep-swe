"""DeepSWE 官方任务统计（pass rate / 平均耗时）。

数据来自官方 trials.json 的全量聚合，口径与官方站点"ALL MODEL EFFORTS"一致
（已对照 actionlint-action-pinning-lint 的 80% / 16m26s 校验）。聚合结果缓存在
data/official-task-stats-<版本>.json 并随仓库分发，避免每次请求拉取 ~30MB 原始
数据；POST /api/tasks/sync-official 可手动刷新。
"""
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
import httpx
from .config import settings

OFFICIAL_VERSION = "v1.1"
OFFICIAL_TRIALS_URL = f"https://deepswe.datacurve.ai/artifacts/{OFFICIAL_VERSION}/trials.json"

_cache: dict | None = None
_lock = threading.Lock()

def _cache_path() -> Path:
    return settings.tasks_dir.parent / "data" / f"official-task-stats-{OFFICIAL_VERSION}.json"

def load_official_stats() -> dict:
    """{task_id: {trials, pass_rate, avg_duration_seconds}}；缓存文件缺失或损坏时返回空。"""
    global _cache
    with _lock:
        if _cache is None:
            try:
                _cache = json.loads(_cache_path().read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                _cache = {}
        return _cache.get("tasks", {})

def official_stats_meta() -> dict:
    load_official_stats()
    return {key: _cache.get(key) for key in ("version", "synced_at", "n_trials")} if _cache else {}

def normalize_model_name(value: str | None) -> str:
    """Normalize local display names (gpt-5.6-sol) to official ids (gpt-5-6-sol)."""
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")

def configuration_stats(task_stats: dict | None, model: str, reasoning_effort: str) -> dict | None:
    wanted_model = normalize_model_name(model)
    wanted_effort = (reasoning_effort or "none").lower()
    for item in (task_stats or {}).get("configurations", []):
        if (
            normalize_model_name(item.get("model")) == wanted_model
            and (item.get("reasoning_effort") or "none").lower() == wanted_effort
        ):
            return item
    return None

def _new_bucket() -> dict:
    return {"n": 0, "passed": 0, "durations": [], "input": [], "cache": [], "output": [], "cost": [], "steps": []}

def _add_trial(entry: dict, row: dict) -> None:
    entry["n"] += 1
    entry["passed"] += bool(row.get("passed"))
    duration = row.get("trial_duration_seconds")
    if isinstance(duration, (int, float)) and duration > 0:
        entry["durations"].append(duration)
    for source, target in (("n_input_tokens", "input"), ("n_cache_tokens", "cache"), ("n_output_tokens", "output"), ("cost_usd", "cost"), ("n_agent_steps", "steps")):
        value = row.get(source)
        if isinstance(value, (int, float)) and value >= 0:
            entry[target].append(value)

def _finish_bucket(entry: dict) -> dict:
    durations = entry["durations"]
    result = {
        "trials": entry["n"],
        "pass_rate": round(entry["passed"] / entry["n"], 4),
        "avg_duration_seconds": round(sum(durations) / len(durations), 1) if durations else None,
    }
    for key, values, digits in (("avg_input_tokens", entry["input"], 0), ("avg_cache_tokens", entry["cache"], 0), ("avg_output_tokens", entry["output"], 0), ("avg_cost_usd", entry["cost"], 6), ("avg_steps", entry["steps"], 1)):
        if values:
            result[key] = round(sum(values) / len(values), digits)
    return result

def aggregate_trials(rows: list[dict]) -> dict:
    stats: dict[str, dict] = {}
    for row in rows:
        task = row.get("task_name")
        if not task:
            continue
        entry = stats.setdefault(task, {"overall": _new_bucket(), "configurations": {}})
        _add_trial(entry["overall"], row)
        model = row.get("model")
        if model:
            effort = row.get("reasoning_effort") or "none"
            bucket = entry["configurations"].setdefault((str(model), str(effort)), _new_bucket())
            _add_trial(bucket, row)
    tasks = {}
    for task, entry in sorted(stats.items()):
        result = _finish_bucket(entry["overall"])
        if entry["configurations"]:
            result["configurations"] = [
                {"model": model, "reasoning_effort": effort, **_finish_bucket(bucket)}
                for (model, effort), bucket in sorted(entry["configurations"].items())
            ]
        tasks[task] = result
    return tasks

def sync_official_stats(timeout: float = 180.0) -> dict:
    """从官方源重新拉取并聚合；成功后更新缓存文件与内存缓存。"""
    global _cache
    response = httpx.get(OFFICIAL_TRIALS_URL, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    rows = response.json().get("rows") or []
    if not rows:
        raise ValueError("官方数据为空，保留现有缓存")
    payload = {
        "version": OFFICIAL_VERSION,
        "source": OFFICIAL_TRIALS_URL,
        "synced_at": datetime.now(UTC).isoformat(),
        "n_trials": len(rows),
        "tasks": aggregate_trials(rows),
    }
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    with _lock:
        _cache = payload
    return {"synced": True, "n_trials": len(rows), "n_tasks": len(payload["tasks"]), "synced_at": payload["synced_at"]}
