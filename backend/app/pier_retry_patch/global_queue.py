"""Cross-process Trial slots backed by the DeepSWE SQLite database."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

ACTIVE_RUN_STATES = ("queued", "preflight", "running")
MAX_PARALLEL_TASKS = 72
DEFAULT_ENVIRONMENT_SETUP_LIMIT = 6
_MEMORY_CACHE_TTL_SECONDS = 5.0
_memory_cache_lock = threading.Lock()
_memory_cache: tuple[float, float | None] = (0.0, None)


class GlobalQueueCancelled(RuntimeError):
    pass


def _memory_bytes(value: str) -> int | None:
    match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgtpe]?i?b)?", value, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    multipliers = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "tb": 1000**4,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
        "pib": 1024**5,
        "eib": 1024**6,
    }
    multiplier = multipliers.get(unit)
    return int(number * multiplier) if multiplier else None


def docker_memory_usage_percent() -> float | None:
    """Return aggregate running-container memory as a percentage of Docker VM memory.

    Docker Desktop exposes the VM memory through ``docker info``.  If Docker is
    unavailable, return ``None`` so queue admission fails open rather than
    blocking all work because diagnostics are unavailable.
    """
    global _memory_cache
    now = time.monotonic()
    with _memory_cache_lock:
        cached_at, cached_value = _memory_cache
        if now - cached_at < _MEMORY_CACHE_TTL_SECONDS:
            return cached_value
        # Keep one Docker probe in flight. Without this, every waiting Trial can
        # launch its own pair of Docker CLI processes when the cache expires.
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            info = subprocess.run(
                ["docker", "info", "--format", "{{.MemTotal}}"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=creationflags,
                check=True,
            )
            total = int(info.stdout.strip())
            stats = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=creationflags,
                check=True,
            )
            used = 0
            for line in stats.stdout.splitlines():
                raw = line.split("/", 1)[0].strip()
                parsed = _memory_bytes(raw)
                if parsed is not None:
                    used += parsed
            value = max(0.0, min(100.0, used / total * 100)) if total > 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            value = None
        _memory_cache = (time.monotonic(), value)
        return value


def _memory_admission_allowed() -> bool:
    try:
        threshold = float(os.environ.get("DEEPSWE_DOCKER_MEMORY_PAUSE_PERCENT", "80"))
    except ValueError:
        threshold = 80.0
    if not math.isfinite(threshold):
        threshold = 80.0
    if threshold <= 0:
        return True
    usage = docker_memory_usage_percent()
    return usage is None or usage < threshold


def _connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(database_path), timeout=30, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _ensure_environment_setup_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS environment_setup_leases (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL,
            entry_id INTEGER NOT NULL UNIQUE,
            trial_name VARCHAR(300) NOT NULL,
            owner_pid INTEGER NOT NULL,
            acquired_at DATETIME NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_environment_setup_leases_run_id "
        "ON environment_setup_leases (run_id)"
    )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(" ")


def _global_limit(connection: sqlite3.Connection, fallback_limit: int) -> int:
    row = connection.execute(
        "SELECT value FROM settings WHERE key = 'max_parallel_tasks'"
    ).fetchone()
    if row is not None:
        try:
            value = json.loads(row["value"])
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return min(value, MAX_PARALLEL_TASKS)
        except (json.JSONDecodeError, TypeError):
            pass
    return min(max(int(fallback_limit), 1), MAX_PARALLEL_TASKS)


def _environment_setup_limit(
    connection: sqlite3.Connection,
    fallback_limit: int,
) -> int:
    row = connection.execute(
        "SELECT value FROM settings "
        "WHERE key = 'max_parallel_environment_setups'"
    ).fetchone()
    if row is not None:
        try:
            value = json.loads(row["value"])
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return min(value, MAX_PARALLEL_TASKS)
        except (json.JSONDecodeError, TypeError):
            pass
    return min(max(int(fallback_limit), 1), MAX_PARALLEL_TASKS)


def _active_run_status(connection: sqlite3.Connection, run_id: int) -> str:
    row = connection.execute(
        "SELECT status FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None or row["status"] not in ACTIVE_RUN_STATES:
        raise GlobalQueueCancelled(f"Run {run_id} 已不在活动状态")
    return str(row["status"])


def _assign_entry(
    connection: sqlite3.Connection,
    run_id: int,
    task_name: str,
    trial_name: str,
) -> sqlite3.Row:
    assigned = connection.execute(
        """
        SELECT id, state, queue_order
        FROM trial_queue_entries
        WHERE run_id = ? AND trial_name = ?
        ORDER BY id
        LIMIT 1
        """,
        (run_id, trial_name),
    ).fetchone()
    if assigned is not None:
        return assigned

    entry = connection.execute(
        """
        SELECT id
        FROM trial_queue_entries
        WHERE run_id = ? AND task_name = ? AND state IN ('pending', 'queued')
          AND trial_name IS NULL
        ORDER BY COALESCE(queue_order, id), id
        LIMIT 1
        """,
        (run_id, task_name),
    ).fetchone()
    if entry is None:
        attempt = connection.execute(
            """
            SELECT COALESCE(MAX(attempt), 0) + 1
            FROM trial_queue_entries
            WHERE run_id = ? AND task_name = ?
            """,
            (run_id, task_name),
        ).fetchone()[0]
        cursor = connection.execute(
            """
            INSERT INTO trial_queue_entries
                (run_id, task_name, attempt, state, queue_order, created_at)
            VALUES (?, ?, ?, 'queued', ?, ?)
            """,
            (
                run_id,
                task_name,
                int(attempt),
                int(connection.execute(
                    "SELECT COALESCE(MAX(queue_order), 0) + 1 FROM trial_queue_entries"
                ).fetchone()[0]),
                _timestamp(),
            ),
        )
        entry_id = int(cursor.lastrowid)
    else:
        entry_id = int(entry["id"])

    current = connection.execute(
        "SELECT queue_order FROM trial_queue_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    queue_order = current["queue_order"] if current is not None else None
    if queue_order is None:
        queue_order = int(connection.execute(
            "SELECT COALESCE(MAX(queue_order), 0) + 1 FROM trial_queue_entries"
        ).fetchone()[0])
    connection.execute(
        """
        UPDATE trial_queue_entries
        SET state = 'queued', trial_name = ?, queue_order = ?, queued_at = ?
        WHERE id = ?
        """,
        (trial_name, int(queue_order), _timestamp(), entry_id),
    )
    return connection.execute(
        "SELECT id, state, queue_order FROM trial_queue_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()


def try_acquire_slot(
    database_path: str | Path,
    run_id: int,
    task_name: str,
    trial_name: str,
    fallback_limit: int,
) -> int | None:
    if not _memory_admission_allowed():
        return None
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _active_run_status(connection, run_id)
        entry = _assign_entry(connection, run_id, task_name, trial_name)
        entry_id = int(entry["id"])
        if entry["state"] == "running":
            connection.commit()
            return entry_id

        limit = _global_limit(connection, fallback_limit)
        running = int(connection.execute(
            "SELECT COUNT(*) FROM trial_queue_entries WHERE state = 'running'"
        ).fetchone()[0])
        head = connection.execute(
            """
            SELECT id
            FROM trial_queue_entries
            WHERE state = 'queued'
            ORDER BY queue_order, id
            LIMIT 1
            """
        ).fetchone()
        if running < limit and head is not None and int(head["id"]) == entry_id:
            connection.execute(
                """
                UPDATE trial_queue_entries
                SET state = 'running', owner_pid = ?, started_at = ?
                WHERE id = ? AND state = 'queued'
                """,
                (os.getpid(), _timestamp(), entry_id),
            )
            connection.execute(
                """
                UPDATE runs SET status = 'running'
                WHERE id = ? AND status IN ('queued', 'preflight', 'running')
                """,
                (run_id,),
            )
            connection.commit()
            return entry_id
        connection.commit()
        return None
    except sqlite3.OperationalError as exc:
        connection.rollback()
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return None
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def release_slot(database_path: str | Path, run_id: int, entry_id: int) -> None:
    for attempt in range(20):
        connection = _connect(database_path)
        try:
            _ensure_environment_setup_table(connection)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM environment_setup_leases WHERE entry_id = ?",
                (entry_id,),
            )
            connection.execute(
                "DELETE FROM trial_queue_entries WHERE id = ? AND run_id = ?",
                (entry_id, run_id),
            )
            running = int(connection.execute(
                """
                SELECT COUNT(*) FROM trial_queue_entries
                WHERE run_id = ? AND state = 'running'
                """,
                (run_id,),
            ).fetchone()[0])
            waiting = int(connection.execute(
                """
                SELECT COUNT(*) FROM trial_queue_entries
                WHERE run_id = ? AND state IN ('pending', 'queued')
                """,
                (run_id,),
            ).fetchone()[0])
            if running:
                next_status = "running"
            elif waiting:
                next_status = "queued"
            else:
                next_status = None
            if next_status:
                connection.execute(
                    """
                    UPDATE runs SET status = ?
                    WHERE id = ? AND status IN ('queued', 'preflight', 'running')
                    """,
                    (next_status, run_id),
                )
            connection.commit()
            return
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if attempt == 19 or (
                "locked" not in str(exc).lower() and "busy" not in str(exc).lower()
            ):
                raise
            time.sleep(0.05)
        finally:
            connection.close()


def try_acquire_environment_setup_slot(
    database_path: str | Path,
    run_id: int,
    trial_name: str,
    fallback_limit: int = DEFAULT_ENVIRONMENT_SETUP_LIMIT,
) -> int | None:
    """Acquire a cross-process slot for one Docker environment startup."""
    connection = _connect(database_path)
    try:
        _ensure_environment_setup_table(connection)
        connection.execute("BEGIN IMMEDIATE")
        _active_run_status(connection, run_id)
        connection.execute(
            """
            DELETE FROM environment_setup_leases
            WHERE NOT EXISTS (
                SELECT 1 FROM trial_queue_entries AS queue
                WHERE queue.id = environment_setup_leases.entry_id
                  AND queue.run_id = environment_setup_leases.run_id
                  AND queue.state = 'running'
            )
            """
        )
        entry = connection.execute(
            """
            SELECT id FROM trial_queue_entries
            WHERE run_id = ? AND trial_name = ? AND state = 'running'
            ORDER BY id
            LIMIT 1
            """,
            (run_id, trial_name),
        ).fetchone()
        if entry is None:
            connection.rollback()
            raise GlobalQueueCancelled(
                f"Trial {trial_name} no longer owns a global execution slot"
            )
        entry_id = int(entry["id"])
        existing = connection.execute(
            "SELECT id FROM environment_setup_leases WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        if existing is not None:
            connection.commit()
            return int(existing["id"])

        limit = _environment_setup_limit(connection, fallback_limit)
        active = int(connection.execute(
            "SELECT COUNT(*) FROM environment_setup_leases"
        ).fetchone()[0])
        if active >= limit:
            connection.commit()
            return None
        cursor = connection.execute(
            """
            INSERT INTO environment_setup_leases
                (run_id, entry_id, trial_name, owner_pid, acquired_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, entry_id, trial_name, os.getpid(), _timestamp()),
        )
        connection.commit()
        return int(cursor.lastrowid)
    except sqlite3.OperationalError as exc:
        connection.rollback()
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return None
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def release_environment_setup_slot(
    database_path: str | Path,
    run_id: int,
    lease_id: int,
) -> None:
    for attempt in range(20):
        connection = _connect(database_path)
        try:
            _ensure_environment_setup_table(connection)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM environment_setup_leases
                WHERE id = ? AND run_id = ?
                """,
                (lease_id, run_id),
            )
            connection.commit()
            return
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if attempt == 19 or (
                "locked" not in str(exc).lower()
                and "busy" not in str(exc).lower()
            ):
                raise
            time.sleep(0.05)
        finally:
            connection.close()


def trial_task_name(trial_config) -> str:
    task_id = trial_config.task.get_task_id()
    return task_id.get_name().split("/")[-1]
