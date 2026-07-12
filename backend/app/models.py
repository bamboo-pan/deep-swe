from datetime import UTC, datetime
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base

TERMINAL_STATES = ("completed", "failed", "cancelled", "interrupted")
ACTIVE_STATES = ("queued", "preflight", "running")

class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    agent: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100))
    reasoning_effort: Mapped[str] = mapped_column(String(20))
    reasoning_effort_adapter: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reasoning_effort_effective: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    job_name: Mapped[str] = mapped_column(String(160), unique=True)
    jobs_dir: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pier_version: Mapped[str | None] = mapped_column(String(60), nullable=True)
    tasks_json: Mapped[str] = mapped_column(Text)
    attempts_per_task: Mapped[int] = mapped_column(Integer, default=1)
    concurrency: Mapped[int] = mapped_column(Integer, default=2)
    agent_timeout_seconds: Mapped[int] = mapped_column(Integer, default=5400)
    verifier_timeout_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    retry_infrastructure_errors: Mapped[bool] = mapped_column(Boolean, default=True)
    infrastructure_max_retries: Mapped[int] = mapped_column(Integer, default=2)
    claude_max_turns: Mapped[int] = mapped_column(Integer, default=120)
    codex_request_max_retries: Mapped[int] = mapped_column(Integer, default=6)
    codex_stream_max_retries: Mapped[int] = mapped_column(Integer, default=6)
    codex_stream_idle_timeout_seconds: Mapped[int] = mapped_column(Integer, default=600)
    verification: Mapped[bool] = mapped_column(Boolean, default=True)
    service_tier: Mapped[str] = mapped_column(String(20), default="standard")
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reward: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

class Baseline(Base):
    __tablename__ = "baselines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
