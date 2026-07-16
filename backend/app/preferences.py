import json
from pathlib import Path
from sqlalchemy import select
from .config import settings
from .database import SessionLocal
from .models import Setting
from .schemas import MAX_PARALLEL_AGENT_COUNT, MAX_PARALLEL_TASKS, SettingsUpdate
from .security import read_credential

CURRENT_KEYS = (
    "credential_file", "jobs_dir", "default_agent", "default_model", "default_effort", "max_parallel_tasks", "provider_rpm", "provider_max_concurrency", "provider_max_retries", "provider_retry_interval_seconds", "squid_read_timeout_seconds", "docker_memory_pause_percent",
    "agent_timeout_seconds", "verifier_timeout_seconds", "infrastructure_max_retries", "agent_max_steps",
    "docker_cleanup_after_run", "docker_cleanup_on_delete", "docker_cache_retention_hours", "docker_cache_warning_gb",
    "run_budget_usd", "trial_budget_usd",
)
LEGACY_KEYS = ("default_concurrency",)
KEYS = CURRENT_KEYS + LEGACY_KEYS

def _defaults() -> dict:
    return {
        "credential_file": str(settings.credential_file),
        "jobs_dir": str(settings.jobs_dir),
        "default_agent": settings.default_agent,
        "default_model": settings.default_model,
        "default_effort": settings.default_effort,
        "max_parallel_tasks": settings.max_parallel_tasks,
        "provider_rpm": settings.provider_rpm,
        "provider_max_concurrency": settings.provider_max_concurrency,
        "provider_max_retries": settings.provider_max_retries,
        "provider_retry_interval_seconds": settings.provider_retry_interval_seconds,
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

def credential_path() -> Path:
    return Path(get_preferences()["credential_file"])

def jobs_path() -> Path:
    return Path(get_preferences()["jobs_dir"])

def current_secrets() -> list[str]:
    """当前凭据 Token，供 redact() 做精确脱敏；读取失败时退回仅正则兜底。"""
    try:
        return [read_credential(credential_path()).token]
    except Exception:
        return []
