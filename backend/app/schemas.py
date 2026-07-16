from pydantic import BaseModel, Field, field_validator
from typing import Literal

Effort = Literal["none", "low", "medium", "high", "xhigh", "max"]
Agent = Literal["mini-swe-agent", "codex", "claude-code"]

MAX_PARALLEL_TASKS = 72
MAX_PARALLEL_AGENT_COUNT = 3
PARALLEL_TASK_WARNING_THRESHOLD = 12
PARALLEL_TASK_CONFIRM_THRESHOLD = 19

class RunOptions(BaseModel):
    model: str = "gpt-5.6-sol"
    reasoning_effort: Effort = "high"
    tasks: list[str] = Field(min_length=1)
    attempts_per_task: int = Field(1, ge=1, le=10)
    codex_stream_idle_timeout_seconds: int = Field(600, ge=30, le=1800)
    verification: bool = True
    service_tier: Literal["standard", "batch", "priority"] = "standard"

class RunDraft(RunOptions):
    agent: Agent = "mini-swe-agent"

class RunBatchDraft(RunOptions):
    agents: list[Agent] = Field(min_length=1, max_length=MAX_PARALLEL_AGENT_COUNT)

    @field_validator("agents")
    @classmethod
    def agents_must_be_unique(cls, value: list[Agent]) -> list[Agent]:
        if len(set(value)) != len(value):
            raise ValueError("Agent 不能重复")
        return value

class SettingsUpdate(BaseModel):
    credential_file: str | None = None
    jobs_dir: str | None = None
    default_agent: Agent | None = None
    default_model: str | None = None
    default_effort: Effort | None = None
    max_parallel_tasks: int | None = Field(None, ge=1, le=MAX_PARALLEL_TASKS)
    provider_rpm: int | None = Field(None, ge=0, le=100000)
    provider_max_concurrency: int | None = Field(None, ge=0, le=1000)
    provider_max_retries: int | None = Field(None, ge=0, le=300)
    provider_retry_interval_seconds: int | None = Field(None, ge=0, le=3600)
    squid_read_timeout_seconds: int | None = Field(None, ge=900, le=7200)
    docker_memory_pause_percent: float | None = Field(None, ge=0, le=95)
    agent_timeout_seconds: int | None = Field(None, ge=60, le=21600)
    verifier_timeout_seconds: int | None = Field(None, ge=60, le=7200)
    infrastructure_max_retries: int | None = Field(None, ge=0, le=6)
    agent_max_steps: int | None = Field(None, ge=10, le=500)
    docker_cleanup_after_run: bool | None = None
    docker_cleanup_on_delete: bool | None = None
    docker_cache_retention_hours: int | None = Field(None, ge=1, le=24 * 365)
    docker_cache_warning_gb: int | None = Field(None, ge=1, le=2048)
    run_budget_usd: float | None = Field(None, ge=0, le=10000)
    trial_budget_usd: float | None = Field(None, ge=0, le=1000)

class BaselineDraft(BaseModel):
    name: str | None = Field(None, max_length=160)

class CompareRequest(BaseModel):
    items: list[str] = Field(default_factory=list)

class CompareAnalysisRequest(BaseModel):
    items: list[str] = Field(default_factory=list)

class RetryTrialsDraft(BaseModel):
    trial_ids: list[str] = Field(min_length=1, max_length=1000)

class RestorePayload(BaseModel):
    version: int
    settings: list[dict]
    runs: list[dict]
    baselines: list[dict]

class DockerCleanupRequest(BaseModel):
    scope: Literal["job", "orphaned", "expired", "build_cache"]
    run_id: int | None = None
    retention_hours: int = Field(168, ge=0, le=24 * 365)
    include_build_cache: bool = False

def concurrency_advice(value: int) -> dict:
    if value <= PARALLEL_TASK_WARNING_THRESHOLD:
        return {"level": "normal", "requires_confirmation": False, "message": "总并行 Trial 数处于正常范围"}
    if value < PARALLEL_TASK_CONFIRM_THRESHOLD:
        return {"level": "warning", "requires_confirmation": False, "message": "构建与验证阶段可能出现资源峰值"}
    if value <= MAX_PARALLEL_TASKS:
        return {"level": "danger", "requires_confirmation": True, "message": "需要确认高负载并行运行"}
    return {"level": "blocked", "requires_confirmation": True, "message": f"最大并行 Task 数不能超过 {MAX_PARALLEL_TASKS}"}
