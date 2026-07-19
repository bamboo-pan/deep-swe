from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal
from .models import ACTIVE_STATES, EnvironmentSetupLease, Run, TrialQueueEntry
from .preferences import get_preferences

PENDING_QUEUE_STATES = ("pending", "queued")
RUNNING_QUEUE_STATE = "running"


def queue_database_path() -> Path:
    url = make_url(settings.database_url)
    if not url.drivername.startswith("sqlite") or not url.database:
        raise RuntimeError("全局 Trial 队列当前仅支持 SQLite 数据库")
    return Path(url.database).resolve()


def requested_trial_count(tasks: Iterable[str], attempts: int, run_count: int) -> int:
    return len(list(tasks)) * attempts * run_count


def _queue_counts(db: Session, run_id: int | None = None) -> dict[str, int]:
    statement = select(TrialQueueEntry.state, func.count(TrialQueueEntry.id))
    if run_id is not None:
        statement = statement.where(TrialQueueEntry.run_id == run_id)
    rows = db.execute(statement.group_by(TrialQueueEntry.state)).all()
    return {str(state): int(count) for state, count in rows}


def queue_status(
    run_id: int | None = None,
    *,
    db: Session | None = None,
    limit: int | None = None,
) -> dict:
    owns_session = db is None
    session = db or SessionLocal()
    try:
        counts = _queue_counts(session, run_id)
        running = counts.get(RUNNING_QUEUE_STATE, 0)
        queued = sum(counts.get(state, 0) for state in PENDING_QUEUE_STATES)
        global_running = (
            running
            if run_id is None
            else _queue_counts(session).get(RUNNING_QUEUE_STATE, 0)
        )
        resolved_limit = int(
            limit if limit is not None else get_preferences()["max_parallel_tasks"]
        )
        return {
            "limit": resolved_limit,
            "running": running,
            "queued": queued,
            "available": max(resolved_limit - global_running, 0),
            "total": running + queued,
        }
    finally:
        if owns_session:
            session.close()


def queue_admission(
    requested: int,
    *,
    db: Session | None = None,
    limit: int | None = None,
) -> dict:
    status = queue_status(db=db, limit=limit)
    waiting_ahead = status["queued"]
    immediately_available = max(status["available"] - waiting_ahead, 0)
    immediate = min(requested, immediately_available)
    queued = max(requested - immediate, 0)
    return {
        **status,
        "requested_trials": requested,
        "immediate_trials": immediate,
        "queued_trials": queued,
        "waiting_ahead": waiting_ahead,
        "total_queued_after": max(waiting_ahead + requested - status["available"], 0),
    }


def enqueue_runs(
    db: Session,
    runs: list[Run],
    tasks: list[str],
    attempts_per_task: int,
) -> None:
    queue_order = int(db.scalar(
        select(func.coalesce(func.max(TrialQueueEntry.queue_order), 0))
    ) or 0) + 1
    for attempt in range(1, attempts_per_task + 1):
        for task in tasks:
            for run in runs:
                db.add(TrialQueueEntry(
                    run_id=run.id,
                    task_name=task,
                    attempt=attempt,
                    state="queued",
                    queue_order=queue_order,
                ))
                queue_order += 1


def enqueue_retry_trials(
    db: Session,
    run_id: int,
    specs: list[dict],
    batch_id: str,
) -> None:
    queue_order = int(db.scalar(
        select(func.coalesce(func.max(TrialQueueEntry.queue_order), 0))
    ) or 0) + 1
    for spec in specs:
        db.add(TrialQueueEntry(
            run_id=run_id,
            task_name=spec["task"],
            attempt=int(spec.get("attempt") or 1),
            state="queued",
            queue_order=queue_order,
            batch_id=batch_id,
        ))
        queue_order += 1


def clear_run_queue(run_id: int, *, db: Session | None = None) -> int:
    owns_session = db is None
    session = db or SessionLocal()
    try:
        session.execute(
            delete(EnvironmentSetupLease).where(
                EnvironmentSetupLease.run_id == run_id
            )
        )
        result = session.execute(
            delete(TrialQueueEntry).where(TrialQueueEntry.run_id == run_id)
        )
        if owns_session:
            session.commit()
        return int(result.rowcount or 0)
    finally:
        if owns_session:
            session.close()


def clear_inactive_queue_entries() -> int:
    with SessionLocal() as db:
        active_run_ids = select(Run.id).where(Run.status.in_(ACTIVE_STATES))
        db.execute(
            delete(EnvironmentSetupLease).where(
                EnvironmentSetupLease.run_id.not_in(active_run_ids)
            )
        )
        result = db.execute(
            delete(TrialQueueEntry).where(
                TrialQueueEntry.run_id.not_in(active_run_ids)
            )
        )
        db.commit()
        return int(result.rowcount or 0)
