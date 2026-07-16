"""Small on-disk status file for whole-Trial infrastructure retries."""

from __future__ import annotations

import json
import os
from pathlib import Path

STATUS_FILE = "infrastructure-retry.json"


def retry_status_path(trial_dir: str | Path) -> Path:
    return Path(trial_dir) / STATUS_FILE


def write_retry_status(
    trial_dir: str | Path,
    *,
    used: int,
    max_retries: int,
    state: str,
    failure_type: str | None = None,
) -> dict:
    maximum = max(int(max_retries), 0)
    consumed = min(max(int(used), 0), maximum)
    value = {
        "used": consumed,
        "max": maximum,
        "remaining": max(maximum - consumed, 0),
        "state": state,
        "failure_type": failure_type,
    }
    path = retry_status_path(trial_dir)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        # Observability must never turn a healthy/retryable Trial into a failure.
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return value


def read_retry_status(
    trial_dir: str | Path,
    *,
    max_retries: int = 0,
) -> dict:
    maximum = max(int(max_retries), 0)
    try:
        value = json.loads(retry_status_path(trial_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        value = {}
    try:
        maximum = max(int(value.get("max", maximum)), 0)
        used = min(max(int(value.get("used", 0)), 0), maximum)
    except (TypeError, ValueError):
        used = 0
    return {
        "infrastructure_retries_used": used,
        "infrastructure_retries_max": maximum,
        "infrastructure_retries_remaining": max(maximum - used, 0),
        "infrastructure_retry_state": value.get("state"),
    }
