from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .preferences import credential_path
from .scheduler import queue_database_path
from .security import read_credential

REQUEST_SCHEDULE_KEY = "_provider_rpm_request_schedule"
LAST_LIMIT_EVENT_KEY = "_provider_last_limit_event"
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}
COOLDOWN_RE = re.compile(
    r'"code"\s*:\s*"model_cooldown".*?'
    r'"model"\s*:\s*"(?P<model>[^"]+)".*?'
    r'"provider"\s*:\s*"(?P<provider>[^"]+)".*?'
    r'"reset_seconds"\s*:\s*(?P<seconds>\d+)',
    re.IGNORECASE | re.DOTALL,
)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def reserve_provider_request(now: float | None = None) -> float:
    """Reserve one slot in a global rolling 60-second request window."""
    current = time.time() if now is None else float(now)
    connection = _connect(queue_database_path())
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT value FROM settings WHERE key = 'provider_rpm'"
        ).fetchone()
        try:
            rpm = int(json.loads(row["value"])) if row is not None else 0
        except (json.JSONDecodeError, TypeError, ValueError):
            rpm = 0
        if rpm <= 0:
            connection.commit()
            return 0.0
        state = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (REQUEST_SCHEDULE_KEY,)
        ).fetchone()
        try:
            raw_schedule = json.loads(state["value"]) if state else []
            schedule = sorted(float(value) for value in raw_schedule)
        except (json.JSONDecodeError, TypeError, ValueError):
            schedule = []
        schedule = [value for value in schedule if value > current - 60.0]
        if len(schedule) < rpm:
            scheduled = current
        else:
            scheduled = max(current, schedule[len(schedule) - rpm] + 60.0)
        schedule.append(scheduled)
        connection.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (REQUEST_SCHEDULE_KEY, json.dumps(schedule), datetime.now(UTC).isoformat(" ")),
        )
        connection.commit()
        return max(scheduled - current, 0.0)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _target_url(base_url: str, path: str, query: str) -> str:
    parsed = urlparse(base_url)
    base_path = parsed.path.rstrip("/")
    suffix = "/" + path.lstrip("/")
    if base_path.endswith("/v1") and suffix.startswith("/v1/"):
        suffix = suffix[3:]
    combined = base_path + suffix
    return urlunparse((parsed.scheme, parsed.netloc, combined, "", query, ""))


def _authorized(request: Request, token: str) -> bool:
    bearer = request.headers.get("authorization", "")
    api_key = request.headers.get("x-api-key", "")
    return bearer == f"Bearer {token}" or api_key == token


def record_limit_event(status_code: int, body: bytes) -> None:
    text = body.decode("utf-8", errors="replace")
    match = COOLDOWN_RE.search(text)
    if status_code != 429 and not match:
        return
    now = time.time()
    if match:
        seconds = int(match.group("seconds"))
        provider = match.group("provider")
        model = match.group("model")
        message = (
            f"Provider {provider} 对模型 {model} 触发冷却，"
            f"预计约 {seconds} 秒后恢复；请求已进入重试流程。"
        )
    else:
        seconds = None
        message = "Provider 返回 429 请求限速；请求已进入重试流程，请检查 RPM 和并发设置。"
    event = {
        "key": f"provider-limit:{uuid.uuid4().hex}",
        "kind": "provider_rate_limit",
        "message": message,
        "reset_seconds": seconds,
        "created_at": now,
    }
    connection = _connect(queue_database_path())
    try:
        connection.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (LAST_LIMIT_EVENT_KEY, json.dumps(event, ensure_ascii=False), datetime.now(UTC).isoformat(" ")),
        )
    finally:
        connection.close()


def latest_limit_event(max_age_seconds: float = 600) -> dict | None:
    connection = _connect(queue_database_path())
    try:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (LAST_LIMIT_EVENT_KEY,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    try:
        event = json.loads(row["value"])
        if time.time() - float(event["created_at"]) > max_age_seconds:
            return None
        return event
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def provider_queue_status(now: float | None = None) -> dict:
    current = time.time() if now is None else float(now)
    connection = _connect(queue_database_path())
    try:
        rpm_row = connection.execute(
            "SELECT value FROM settings WHERE key = 'provider_rpm'"
        ).fetchone()
        schedule_row = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (REQUEST_SCHEDULE_KEY,)
        ).fetchone()
    finally:
        connection.close()
    try:
        rpm = int(json.loads(rpm_row["value"])) if rpm_row else 0
    except (json.JSONDecodeError, TypeError, ValueError):
        rpm = 0
    try:
        schedule = sorted(float(value) for value in json.loads(schedule_row["value"])) if schedule_row else []
    except (json.JSONDecodeError, TypeError, ValueError):
        schedule = []
    active = [value for value in schedule if value > current - 60.0]
    sent = [value for value in active if value <= current]
    waiting = [value for value in active if value > current]
    if rpm <= 0:
        available = None
        next_release = 0.0
    else:
        available = max(rpm - len(sent), 0)
        if waiting:
            next_release = max(waiting[0] - current, 0.0)
        elif available == 0 and sent:
            next_release = max(sent[0] + 60.0 - current, 0.0)
        else:
            next_release = 0.0
    return {
        "enabled": rpm > 0,
        "rpm": max(rpm, 0),
        "sent_last_60_seconds": len(sent),
        "queued_requests": len(waiting),
        "available_now": available,
        "next_release_seconds": round(next_release, 1),
    }


async def forward_provider_request(request: Request, path: str):
    credential = read_credential(credential_path())
    if not _authorized(request, credential.token):
        raise HTTPException(401, "Invalid provider proxy credential")
    delay = await asyncio.to_thread(reserve_provider_request)
    if delay:
        await asyncio.sleep(delay)
    body = await request.body()
    headers = {
        key: value for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
    target = _target_url(credential.url, path, request.url.query)
    client = httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=30), follow_redirects=False)
    try:
        upstream = await client.send(
            client.build_request(request.method, target, headers=headers, content=body),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, f"Provider proxy request failed: {exc}") from exc
    response_headers = {
        key: value for key, value in upstream.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
    if upstream.status_code == 429:
        content = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        await asyncio.to_thread(record_limit_event, upstream.status_code, content)
        return Response(content, upstream.status_code, headers=response_headers)

    async def stream():
        captured = bytearray()
        try:
            async for chunk in upstream.aiter_raw():
                if len(captured) < 128_000:
                    captured.extend(chunk[:128_000 - len(captured)])
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()
            if captured:
                await asyncio.to_thread(record_limit_event, upstream.status_code, bytes(captured))

    return StreamingResponse(
        stream(), status_code=upstream.status_code, headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )
