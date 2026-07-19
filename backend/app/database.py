from datetime import datetime
from pathlib import Path
import sqlite3
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings

sqlite3.register_adapter(datetime, lambda value: value.isoformat(" "))

class Base(DeclarativeBase): pass

engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

RUN_COLUMNS = {
    "reasoning_effort_adapter": "VARCHAR(80)",
    "reasoning_effort_effective": "VARCHAR(20)",
    "agent_version_mode": "VARCHAR(20) NOT NULL DEFAULT 'latest'",
    "agent_version_requested": "VARCHAR(80)",
    "agent_version_source": "VARCHAR(30)",
    "agent_timeout_seconds": "INTEGER NOT NULL DEFAULT 5400",
    "verifier_timeout_seconds": "INTEGER NOT NULL DEFAULT 1800",
    "retry_infrastructure_errors": "BOOLEAN NOT NULL DEFAULT 1",
    "infrastructure_max_retries": "INTEGER NOT NULL DEFAULT 4",
    "agent_max_steps": "INTEGER NOT NULL DEFAULT 120",
    "codex_request_max_retries": "INTEGER NOT NULL DEFAULT 6",
    "codex_stream_max_retries": "INTEGER NOT NULL DEFAULT 6",
    "codex_stream_idle_timeout_seconds": "INTEGER NOT NULL DEFAULT 600",
    "verification": "BOOLEAN NOT NULL DEFAULT 1",
    "service_tier": "VARCHAR(20) NOT NULL DEFAULT 'standard'",
    "jobs_dir": "VARCHAR(500)",
    "credential_file": "VARCHAR(500)",
    "provider_url": "VARCHAR(1000)",
    "credential_fingerprint": "VARCHAR(20)",
    "pier_version": "VARCHAR(60)",
    "deleted_trials_json": "TEXT NOT NULL DEFAULT '[]'",
}

def init_db():
    # 遗留非终态运行的收割与状态标记由 runner.reap_orphaned_runs() 在启动时执行，
    # 这里只负责建表和加列，保证先能读到残留运行的 pid/job_name。
    from . import models
    settings_path = settings.database_url.removeprefix("sqlite:///")
    Path(settings_path).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    existing = {column["name"] for column in inspect(engine).get_columns("runs")}
    with engine.begin() as connection:
        for name, definition in RUN_COLUMNS.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE runs ADD COLUMN {name} {definition}"))
        # 「Claude 最大轮数」升级为全 agent 通用的「最大步数」，历史值原样迁移
        if "agent_max_steps" not in existing and "claude_max_turns" in existing:
            connection.execute(text("UPDATE runs SET agent_max_steps = claude_max_turns"))
