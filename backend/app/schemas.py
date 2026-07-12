from pydantic import BaseModel, Field
from typing import Literal

Effort = Literal["none", "low", "medium", "high", "xhigh", "max"]
Agent = Literal["mini-swe-agent", "codex", "claude-code"]

class RunDraft(BaseModel):
    agent: Agent = "mini-swe-agent"
    model: str = "gpt-5.6-sol"
    reasoning_effort: Effort = "high"
    tasks: list[str] = Field(min_length=1)
    attempts_per_task: int = Field(1, ge=1, le=10)
    concurrency: int = Field(2, ge=1, le=8)
    confirm_high_concurrency: bool = False
    agent_timeout_seconds: int = Field(5400, ge=60, le=21600)
    verifier_timeout_seconds: int = Field(1800, ge=60, le=7200)
    retry_infrastructure_errors: bool = True
    verification: bool = True
    service_tier: Literal["standard", "batch", "priority"] = "standard"

class SettingsUpdate(BaseModel):
    credential_file: str | None = None
    jobs_dir: str | None = None
    default_agent: Agent | None = None
    default_model: str | None = None
    default_effort: Effort | None = None
    default_concurrency: int | None = Field(None, ge=1, le=4)
    docker_cleanup_after_run: bool | None = None
    docker_cleanup_on_delete: bool | None = None
    docker_cache_retention_hours: int | None = Field(None, ge=1, le=24 * 365)
    docker_cache_warning_gb: int | None = Field(None, ge=1, le=2048)
    run_budget_usd: float | None = Field(None, ge=0, le=10000)

class BaselineDraft(BaseModel):
    name: str | None = Field(None, max_length=160)

class RestorePayload(BaseModel):
    version: int
    settings: list[dict]
    runs: list[dict]
    baselines: list[dict]

class DockerCleanupRequest(BaseModel):
    scope: Literal["job", "orphaned", "expired", "build_cache"]
    run_id: int | None = None
    retention_hours: int = Field(168, ge=1, le=24 * 365)
    include_build_cache: bool = False

def concurrency_advice(value: int) -> dict:
    if value <= 2: return {"level": "normal", "requires_confirmation": False, "message": "资源配置安全"}
    if value == 3: return {"level": "warning", "requires_confirmation": False, "message": "内存压力可能较高"}
    if value == 4: return {"level": "danger", "requires_confirmation": True, "message": "需要手动确认高资源占用"}
    return {"level": "blocked", "requires_confirmation": True, "message": "并发大于 4 默认禁止"}
