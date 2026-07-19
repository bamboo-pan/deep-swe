from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from .config import settings


AGENTS = ("mini-swe-agent", "codex", "claude-code")
CATALOG_MAX_AGE = timedelta(hours=6)
REMOTE_TIMEOUT_SECONDS = 8.0
LOCAL_SCAN_TIMEOUT_SECONDS = 30

_LATEST_URLS = {
    "mini-swe-agent": "https://pypi.org/pypi/mini-swe-agent/json",
    "codex": "https://registry.npmjs.org/@openai%2Fcodex/latest",
    "claude-code": "https://registry.npmjs.org/@anthropic-ai%2Fclaude-code/latest",
}
_LOCAL_VERSION_PATTERNS = {
    "mini-swe-agent": re.compile(r"mini-swe-agent==([0-9][A-Za-z0-9.+-]*)"),
    "codex": re.compile(r"@openai/codex@([0-9][A-Za-z0-9.+-]*)"),
    "claude-code": re.compile(
        r"@anthropic-ai/claude-code@([0-9][A-Za-z0-9.+-]*)"
    ),
}
_GENERIC_CACHE_PATTERNS = {
    "mini-swe-agent": re.compile(r"uv tool install mini-swe-agent(?!==)"),
    "codex": re.compile(r"@openai/codex@latest"),
    "claude-code": re.compile(r"@anthropic-ai/claude-code(?:[;\s]|$)(?!@)"),
}

_lock = threading.Lock()
_local_cache: tuple[datetime, dict[str, list[str]]] | None = None


def _cache_path() -> Path:
    return settings.tasks_dir.parent / "data" / "agent-version-catalog.json"


def _version_key(value: str) -> tuple:
    parts = re.split(r"([0-9]+)", value)
    return tuple(int(part) if part.isdigit() else part.lower() for part in parts)


def _sorted_versions(values) -> list[str]:
    return sorted(
        {str(value).strip() for value in values if str(value).strip()},
        key=_version_key,
        reverse=True,
    )


def _read_cache() -> dict:
    try:
        payload = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"agents": {}}
    return payload if isinstance(payload, dict) else {"agents": {}}


def _write_cache(payload: dict) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _cache_is_fresh(payload: dict) -> bool:
    value = payload.get("checked_at")
    if not isinstance(value, str):
        return False
    try:
        checked_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - checked_at <= CATALOG_MAX_AGE


def _fetch_latest(agent: str) -> str:
    response = httpx.get(
        _LATEST_URLS[agent],
        timeout=REMOTE_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    if agent == "mini-swe-agent":
        version = (payload.get("info") or {}).get("version")
    else:
        version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{agent} registry response has no version")
    return version.strip()


def _refresh_latest(existing: dict) -> tuple[dict, dict[str, str]]:
    latest = {
        agent: str((existing.get(agent) or {}).get("latest") or "").strip() or None
        for agent in AGENTS
    }
    errors: dict[str, str] = {}
    # Keep this sequential: runner tests replace threading.Thread with a no-op
    # scheduler stub, which would also stall ThreadPoolExecutor worker startup.
    for agent in AGENTS:
        try:
            latest[agent] = _fetch_latest(agent)
        except Exception as exc:
            errors[agent] = str(exc)
    return latest, errors


def _scan_local_versions() -> dict[str, list[str]]:
    global _local_cache
    now = datetime.now(UTC)
    if _local_cache and now - _local_cache[0] < timedelta(seconds=60):
        return _local_cache[1]

    versions: dict[str, set[str]] = {agent: set() for agent in AGENTS}
    docker = shutil.which("docker") or "docker"
    try:
        result = subprocess.run(
            [docker, "buildx", "du", "--verbose"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=LOCAL_SCAN_TIMEOUT_SECONDS,
        )
        text = result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        text = ""
    generic_cached = {
        agent: bool(pattern.search(text))
        for agent, pattern in _GENERIC_CACHE_PATTERNS.items()
    }
    for agent, pattern in _LOCAL_VERSION_PATTERNS.items():
        versions[agent].update(pattern.findall(text))
    observed = _latest_observed_versions()
    for agent, present in generic_cached.items():
        if present and observed.get(agent):
            versions[agent].add(observed[agent])
    resolved = {agent: _sorted_versions(values) for agent, values in versions.items()}
    _local_cache = (now, resolved)
    return resolved


def _latest_observed_versions() -> dict[str, str]:
    """Map legacy unpinned BuildKit layers to the newest actual Trial version."""
    candidates = []
    try:
        candidates = sorted(
            settings.jobs_dir.glob("*/**/result.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return {}
    resolved: dict[str, str] = {}
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        info = data.get("agent_info") or {}
        version = info.get("version")
        configured = ((data.get("config") or {}).get("agent") or {}).get("name")
        name = info.get("name") or configured
        if name not in AGENTS or not isinstance(version, str) or not version.strip():
            continue
        resolved.setdefault(name, version.strip())
        if len(resolved) == len(AGENTS):
            break
    return resolved


def agent_version_catalog(
    force_refresh: bool = False,
    *,
    include_local: bool = True,
) -> dict:
    """Return remote latest versions and exact versions present in BuildKit cache."""
    with _lock:
        payload = _read_cache()
        existing = payload.get("agents") if isinstance(payload.get("agents"), dict) else {}
        errors: dict[str, str] = {}
        if force_refresh or not _cache_is_fresh(payload):
            latest, errors = _refresh_latest(existing)
            payload = {
                "checked_at": datetime.now(UTC).isoformat(),
                "agents": {
                    agent: {"latest": latest.get(agent)} for agent in AGENTS
                },
            }
            _write_cache(payload)
        local = _scan_local_versions() if include_local else {
            agent: [] for agent in AGENTS
        }
        agents = {}
        for agent in AGENTS:
            latest = ((payload.get("agents") or {}).get(agent) or {}).get("latest")
            agents[agent] = {
                "latest": latest if isinstance(latest, str) and latest else None,
                "local_versions": local.get(agent, []),
                "error": errors.get(agent),
            }
        return {
            "checked_at": payload.get("checked_at"),
            "agents": agents,
        }


def resolve_agent_version(
    agent: str,
    preference: dict | None,
    *,
    catalog: dict | None = None,
) -> dict:
    if agent not in AGENTS:
        raise ValueError(f"Unsupported agent: {agent}")
    preference = preference if isinstance(preference, dict) else {}
    mode = preference.get("mode") or "latest"
    if mode not in {"latest", "local"}:
        raise ValueError(f"Unsupported agent version mode: {mode}")
    catalog = catalog or agent_version_catalog(force_refresh=mode == "latest")
    entry = catalog["agents"][agent]
    if mode == "local":
        version = str(preference.get("version") or "").strip()
        if not version:
            raise ValueError(f"{agent} 未选择本地版本")
        if version not in entry["local_versions"]:
            raise ValueError(
                f"{agent} {version} 当前不在本地 Docker 构建缓存中；"
                "请改为自动使用最新版本，或刷新 Agent 版本目录"
            )
        return {"mode": mode, "version": version, "source": "local-cache"}

    version = entry.get("latest")
    if not version:
        raise RuntimeError(
            f"无法获取 {agent} 最新版本"
            + (f"：{entry['error']}" if entry.get("error") else "")
        )
    return {"mode": mode, "version": version, "source": "registry"}
