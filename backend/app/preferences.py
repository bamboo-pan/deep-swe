import json
from pathlib import Path
from sqlalchemy import select
from .config import settings
from .database import SessionLocal
from .models import ACTIVE_STATES, Run, Setting
from .schemas import MAX_PARALLEL_AGENT_COUNT, MAX_PARALLEL_TASKS, SettingsUpdate
from .security import read_credential

CURRENT_KEYS = (
    "credential_file", "jobs_dir", "default_agent", "default_model", "default_effort", "agent_versions", "max_parallel_tasks", "max_parallel_environment_setups", "provider_rpm", "provider_max_concurrency", "provider_max_retries", "provider_stream_max_retries", "provider_retry_interval_seconds", "codex_stream_idle_timeout_seconds", "squid_read_timeout_seconds", "docker_memory_pause_percent",
    "agent_timeout_seconds", "verifier_timeout_seconds", "infrastructure_max_retries", "agent_max_steps",
    "docker_cleanup_after_run", "docker_cleanup_on_delete", "docker_cache_retention_hours", "docker_cache_warning_gb",
    "run_budget_usd", "trial_budget_usd",
)
LEGACY_KEYS = ("default_concurrency",)
AUXILIARY_KEYS = (
    "compare_analysis_prompt",
    "compare_analysis_model",
    "compare_analysis_reasoning_effort",
    "compare_analysis_timeout_seconds",
)
KEYS = CURRENT_KEYS + LEGACY_KEYS + AUXILIARY_KEYS

def _defaults() -> dict:
    return {
        "credential_file": str(settings.credential_file),
        "jobs_dir": str(settings.jobs_dir),
        "default_agent": settings.default_agent,
        "default_model": settings.default_model,
        "default_effort": settings.default_effort,
        "agent_versions": {
            agent: {"mode": "latest", "version": None}
            for agent in ("mini-swe-agent", "codex", "claude-code")
        },
        "max_parallel_tasks": settings.max_parallel_tasks,
        "max_parallel_environment_setups": settings.max_parallel_environment_setups,
        "provider_rpm": settings.provider_rpm,
        "provider_max_concurrency": settings.provider_max_concurrency,
        "provider_max_retries": settings.provider_max_retries,
        "provider_stream_max_retries": settings.provider_stream_max_retries,
        "provider_retry_interval_seconds": settings.provider_retry_interval_seconds,
        "codex_stream_idle_timeout_seconds": settings.codex_stream_idle_timeout_seconds,
        "squid_read_timeout_seconds": settings.squid_read_timeout_seconds,
        "docker_memory_pause_percent": settings.docker_memory_pause_percent,
        "agent_timeout_seconds": settings.agent_timeout_seconds,
        "verifier_timeout_seconds": settings.verifier_timeout_seconds,
        "infrastructure_max_retries": settings.infrastructure_max_retries,
        "agent_max_steps": settings.agent_max_steps,
        "docker_cleanup_after_run": settings.docker_cleanup_after_run,
        "docker_cleanup_on_delete": settings.docker_cleanup_on_delete,
        "docker_cache_retention_hours": settings.docker_cache_retention_hours,
        "docker_cache_warning_gb": settings.docker_cache_warning_gb,
        "run_budget_usd": settings.run_budget_usd,
        "trial_budget_usd": settings.trial_budget_usd,
    }

def get_preferences() -> dict:
    values = _defaults()
    stored = {}
    with SessionLocal() as db:
        for row in db.scalars(select(Setting).where(Setting.key.in_(KEYS))).all():
            try:
                stored[row.key] = json.loads(row.value)
            except (json.JSONDecodeError, TypeError):
                continue
    for key in CURRENT_KEYS:
        if key in stored:
            values[key] = stored[key]
    if "max_parallel_tasks" not in stored and "default_concurrency" in stored:
        legacy = stored["default_concurrency"]
        if isinstance(legacy, int) and not isinstance(legacy, bool):
            # 旧值是每 Agent 配额，按三 Agent 同跑时的总容量迁移。
            values["max_parallel_tasks"] = min(
                legacy * MAX_PARALLEL_AGENT_COUNT,
                MAX_PARALLEL_TASKS,
            )
    values["credential_file"] = str(values["credential_file"])
    values["jobs_dir"] = str(values["jobs_dir"])
    configured_versions = values.get("agent_versions")
    if not isinstance(configured_versions, dict):
        configured_versions = {}
    values["agent_versions"] = {
        agent: {
            "mode": (
                configured_versions.get(agent, {}).get("mode")
                if isinstance(configured_versions.get(agent), dict)
                and configured_versions[agent].get("mode") in {"latest", "local"}
                else "latest"
            ),
            "version": (
                configured_versions.get(agent, {}).get("version")
                if isinstance(configured_versions.get(agent), dict)
                else None
            ),
        }
        for agent in ("mini-swe-agent", "codex", "claude-code")
    }
    return values

def update_preferences(payload: SettingsUpdate) -> dict:
    changes = payload.model_dump(exclude_none=True)
    with SessionLocal() as db:
        for key, value in changes.items():
            row = db.get(Setting, key)
            if row:
                row.value = json.dumps(value, ensure_ascii=False)
            else:
                db.add(Setting(key=key, value=json.dumps(value, ensure_ascii=False)))
        if "max_parallel_tasks" in changes:
            legacy = db.get(Setting, "default_concurrency")
            if legacy:
                db.delete(legacy)
        if "provider_rpm" in changes:
            for key in (
                "_provider_rpm_next_request",
                "_provider_rpm_request_schedule",
            ):
                limiter_state = db.get(Setting, key)
                if limiter_state:
                    db.delete(limiter_state)
        db.commit()
    return get_preferences()

def get_auxiliary_preferences(defaults: dict) -> dict:
    unsupported = set(defaults) - set(AUXILIARY_KEYS)
    if unsupported:
        raise KeyError(f"Unsupported auxiliary preferences: {sorted(unsupported)}")
    values = dict(defaults)
    with SessionLocal() as db:
        rows = db.scalars(select(Setting).where(Setting.key.in_(defaults))).all()
        for row in rows:
            try:
                values[row.key] = json.loads(row.value)
            except (json.JSONDecodeError, TypeError):
                continue
    return values

def set_auxiliary_preferences(values: dict) -> None:
    unsupported = set(values) - set(AUXILIARY_KEYS)
    if unsupported:
        raise KeyError(f"Unsupported auxiliary preferences: {sorted(unsupported)}")
    with SessionLocal() as db:
        for key, value in values.items():
            row = db.get(Setting, key)
            encoded = json.dumps(value, ensure_ascii=False)
            if row:
                row.value = encoded
            else:
                db.add(Setting(key=key, value=encoded))
        db.commit()

def credential_path() -> Path:
    return Path(get_preferences()["credential_file"])

def jobs_path() -> Path:
    return Path(get_preferences()["jobs_dir"])

def current_secrets() -> list[str]:
    """当前与活跃 Run 的 Token，仅用于日志精确脱敏。"""
    paths = set()
    try:
        paths.add(credential_path())
    except Exception:
        pass
    try:
        with SessionLocal() as db:
            active_paths = db.scalars(
                select(Run.credential_file).where(Run.status.in_(ACTIVE_STATES))
            ).all()
        paths.update(Path(path) for path in active_paths if path)
    except Exception:
        pass
    secrets = []
    for path in paths:
        try:
            token = read_credential(path).token
        except Exception:
            continue
        if token not in secrets:
            secrets.append(token)
    return secrets
