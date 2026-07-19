from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .database import SessionLocal
from .models import Run
from .preferences import credential_path, get_preferences
from .scheduler import queue_database_path
from .security import Credential, read_credential, token_fingerprint

logger = logging.getLogger(__name__)
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
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
SUCCESS_STATUS_MIN = 200
SUCCESS_STATUS_MAX = 299
SSE_MEDIA_TYPE = "text/event-stream"
SSE_KEEPALIVE_INTERVAL_SECONDS = 15.0
STREAM_BUFFER_MEMORY_BYTES = 8 * 1024 * 1024
SSE_TERMINAL_EVENT_TYPES = {
    "message_stop",
    "response.completed",
    "response.failed",
    "response.incomplete",
}
RUN_ID_HEADER = "x-deepswe-run-id"
TRIAL_ID_HEADER = "x-deepswe-trial-id"
PROVIDER_TELEMETRY_RETENTION_SECONDS = 24 * 60 * 60

_PROVIDER_TELEMETRY_LOCK = threading.Lock()
_ACTUAL_PROVIDER_REQUESTS: deque[float] = deque()
_PROVIDER_RESPONSE_OUTCOMES: deque[tuple[float, int]] = deque()
_PROVIDER_STREAM_FAILURES: deque[float] = deque()
_TRIAL_PROVIDER_TELEMETRY: dict[tuple[int, str], dict] = {}


class IncompleteProviderStreamError(RuntimeError):
    """A successful SSE response ended without its protocol terminal event."""


class _ProviderConcurrencyLimiter:
    """Process-wide cap for active upstream Provider connections."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active = 0
        self._waiting = 0

    async def acquire(self, limit: int) -> None:
        normalized_limit = max(int(limit), 0)
        async with self._condition:
            if normalized_limit > 0:
                self._waiting += 1
                try:
                    while self._active >= normalized_limit:
                        await self._condition.wait()
                finally:
                    self._waiting -= 1
            self._active += 1

    async def release(self) -> None:
        async with self._condition:
            self._active = max(self._active - 1, 0)
            self._condition.notify_all()

    def snapshot(self) -> tuple[int, int]:
        return self._active, self._waiting


_PROVIDER_CONCURRENCY = _ProviderConcurrencyLimiter()


def _provider_request_policy() -> tuple[int, int, int, int]:
    preferences = get_preferences()
    return (
        max(int(preferences["provider_max_concurrency"]), 0),
        max(int(preferences["provider_max_retries"]), 0),
        max(int(preferences["provider_stream_max_retries"]), 0),
        max(int(preferences["provider_retry_interval_seconds"]), 0),
    )


def _provider_request_identity(request: Request) -> tuple[int, str] | None:
    raw_run_id = request.headers.get(RUN_ID_HEADER)
    trial_id = request.headers.get(TRIAL_ID_HEADER)
    if not raw_run_id or not trial_id or len(trial_id) > 300:
        return None
    try:
        run_id = int(raw_run_id)
    except ValueError:
        return None
    if run_id < 1 or Path(trial_id).name != trial_id:
        return None
    return run_id, trial_id


def _prune_provider_telemetry(now: float) -> None:
    cutoff = now - 60.0
    while _ACTUAL_PROVIDER_REQUESTS and _ACTUAL_PROVIDER_REQUESTS[0] <= cutoff:
        _ACTUAL_PROVIDER_REQUESTS.popleft()
    while _PROVIDER_RESPONSE_OUTCOMES and _PROVIDER_RESPONSE_OUTCOMES[0][0] <= cutoff:
        _PROVIDER_RESPONSE_OUTCOMES.popleft()
    while _PROVIDER_STREAM_FAILURES and _PROVIDER_STREAM_FAILURES[0] <= cutoff:
        _PROVIDER_STREAM_FAILURES.popleft()
    stale_cutoff = now - PROVIDER_TELEMETRY_RETENTION_SECONDS
    stale = [
        key for key, value in _TRIAL_PROVIDER_TELEMETRY.items()
        if float(value.get("updated_at") or 0) <= stale_cutoff
    ]
    for key in stale:
        _TRIAL_PROVIDER_TELEMETRY.pop(key, None)


def record_actual_provider_attempt(
    identity: tuple[int, str] | None,
    attempt: int,
    max_retries: int,
    *,
    stream_retry: bool = False,
    stream_retry_number: int = 0,
    stream_max_retries: int = 0,
    now: float | None = None,
) -> None:
    current = time.time() if now is None else float(now)
    with _PROVIDER_TELEMETRY_LOCK:
        _ACTUAL_PROVIDER_REQUESTS.append(current)
        _prune_provider_telemetry(current)
        if identity is None:
            return
        telemetry = _TRIAL_PROVIDER_TELEMETRY.setdefault(identity, {
            "provider_response_code": None,
            "provider_request_count": 0,
            "provider_retries_used": 0,
            "provider_max_retries": max(int(max_retries), 0),
            "provider_stream_retries_used": 0,
            "provider_stream_max_retries": max(int(stream_max_retries), 0),
            "updated_at": current,
        })
        telemetry["provider_request_count"] += 1
        if attempt <= 0 and not stream_retry:
            # A new Agent request supersedes the previous request's failure
            # state. Keep the lifetime request count, but make the card show
            # only the current request's response/retry state.
            telemetry["provider_response_code"] = None
            telemetry["provider_retries_used"] = 0
            telemetry["provider_stream_retries_used"] = 0
        else:
            telemetry["provider_retries_used"] = int(attempt)
        telemetry["provider_max_retries"] = max(int(max_retries), 0)
        if stream_retry:
            telemetry["provider_stream_retries_used"] = min(
                max(int(stream_retry_number), 0),
                max(int(stream_max_retries), 0),
            )
        telemetry["provider_stream_max_retries"] = max(
            int(stream_max_retries), 0
        )
        telemetry["updated_at"] = current


def record_provider_response(
    identity: tuple[int, str] | None,
    status_code: int,
    *,
    now: float | None = None,
) -> None:
    current = time.time() if now is None else float(now)
    with _PROVIDER_TELEMETRY_LOCK:
        _PROVIDER_RESPONSE_OUTCOMES.append((current, int(status_code)))
        _prune_provider_telemetry(current)
        if identity is None:
            return
        telemetry = _TRIAL_PROVIDER_TELEMETRY.setdefault(identity, {
            "provider_response_code": None,
            "provider_request_count": 0,
            "provider_retries_used": 0,
            "provider_max_retries": 0,
            "provider_stream_retries_used": 0,
            "provider_stream_max_retries": 0,
            "updated_at": current,
        })
        telemetry["provider_response_code"] = int(status_code)
        if int(status_code) < 400:
            telemetry["provider_retries_used"] = 0
        telemetry["updated_at"] = current


def record_provider_stream_failure(
    identity: tuple[int, str] | None,
    retry_number: int,
    max_retries: int,
    *,
    now: float | None = None,
) -> None:
    current = time.time() if now is None else float(now)
    with _PROVIDER_TELEMETRY_LOCK:
        _PROVIDER_STREAM_FAILURES.append(current)
        _prune_provider_telemetry(current)
        if identity is None:
            return
        telemetry = _TRIAL_PROVIDER_TELEMETRY.setdefault(identity, {
            "provider_response_code": None,
            "provider_request_count": 0,
            "provider_retries_used": 0,
            "provider_max_retries": 0,
            "provider_stream_retries_used": 0,
            "provider_stream_max_retries": max(int(max_retries), 0),
            "updated_at": current,
        })
        telemetry["provider_stream_retries_used"] = min(
            max(int(retry_number), 0), max(int(max_retries), 0)
        )
        telemetry["provider_stream_max_retries"] = max(int(max_retries), 0)
        telemetry["updated_at"] = current


def provider_trial_status(run_id: int, trial_id: str) -> dict:
    with _PROVIDER_TELEMETRY_LOCK:
        value = _TRIAL_PROVIDER_TELEMETRY.get((int(run_id), trial_id))
        return dict(value) if value else {}


def actual_provider_requests_last_60_seconds(now: float | None = None) -> int:
    current = time.time() if now is None else float(now)
    with _PROVIDER_TELEMETRY_LOCK:
        _prune_provider_telemetry(current)
        return len(_ACTUAL_PROVIDER_REQUESTS)


def provider_response_stats_last_60_seconds(now: float | None = None) -> dict:
    current = time.time() if now is None else float(now)
    with _PROVIDER_TELEMETRY_LOCK:
        _prune_provider_telemetry(current)
        completed = len(_PROVIDER_RESPONSE_OUTCOMES)
        response_code_counts: dict[str, int] = {}
        for _timestamp, status_code in _PROVIDER_RESPONSE_OUTCOMES:
            code = str(status_code)
            response_code_counts[code] = response_code_counts.get(code, 0) + 1
        failed = sum(
            1 for _timestamp, status_code in _PROVIDER_RESPONSE_OUTCOMES
            if status_code >= 400
        )
        stream_failures = len(_PROVIDER_STREAM_FAILURES)
    return {
        "completed_requests_last_60_seconds": completed,
        "failed_requests_last_60_seconds": failed,
        "failure_rate_last_60_seconds": (
            round(failed / completed * 100, 1) if completed else None
        ),
        "response_code_counts_last_60_seconds": response_code_counts,
        "stream_failures_last_60_seconds": stream_failures,
    }


def _reset_provider_telemetry() -> None:
    """Clear process-local telemetry; used by tests and startup isolation."""
    with _PROVIDER_TELEMETRY_LOCK:
        _ACTUAL_PROVIDER_REQUESTS.clear()
        _PROVIDER_RESPONSE_OUTCOMES.clear()
        _PROVIDER_STREAM_FAILURES.clear()
        _TRIAL_PROVIDER_TELEMETRY.clear()


async def _close_upstream_and_release(upstream) -> None:
    try:
        await upstream.aclose()
    finally:
        await _PROVIDER_CONCURRENCY.release()


def _is_success_status(status_code: int) -> bool:
    return SUCCESS_STATUS_MIN <= int(status_code) <= SUCCESS_STATUS_MAX


def _is_sse_response(headers) -> bool:
    content_type = str(headers.get("content-type") or "").lower()
    return content_type.split(";", 1)[0].strip() == SSE_MEDIA_TYPE


def _sse_lines_have_terminal_event(lines) -> bool:
    event_name = ""
    data_lines: list[str] = []

    def is_terminal() -> bool:
        if event_name in SSE_TERMINAL_EVENT_TYPES:
            return True
        data = "\n".join(data_lines).strip()
        if data == "[DONE]":
            return True
        if not data:
            return False
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("type") in SSE_TERMINAL_EVENT_TYPES
        )

    for line in lines:
        if not line:
            if is_terminal():
                return True
            event_name = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    return is_terminal()


def _sse_has_terminal_event(content: bytes) -> bool:
    return _sse_lines_have_terminal_event(
        content.decode("utf-8", errors="replace").splitlines()
    )


def _spool_has_sse_terminal_event(spool) -> bool:
    spool.seek(0)
    terminal = _sse_lines_have_terminal_event(
        raw.decode("utf-8", errors="replace").rstrip("\r\n")
        for raw in spool
    )
    spool.seek(0)
    return terminal


async def _buffer_upstream_response(upstream):
    spool = tempfile.SpooledTemporaryFile(max_size=STREAM_BUFFER_MEMORY_BYTES)
    captured = bytearray()
    try:
        async for chunk in upstream.aiter_raw():
            spool.write(chunk)
            if len(captured) < 128_000:
                captured.extend(chunk[:128_000 - len(captured)])
        spool.seek(0)
        return spool, bytes(captured)
    except BaseException:
        spool.close()
        raise


def _iter_spooled_response(spool):
    try:
        while chunk := spool.read(64 * 1024):
            yield chunk
    finally:
        spool.close()


async def _record_limit_event_safely(status_code: int, body: bytes) -> None:
    try:
        await asyncio.to_thread(record_limit_event, status_code, body)
    except Exception:
        logger.warning("Failed to record Provider limit event", exc_info=True)


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


def _request_token(request: Request) -> str:
    bearer = request.headers.get("authorization", "")
    if bearer.startswith("Bearer "):
        return bearer.removeprefix("Bearer ")
    return request.headers.get("x-api-key", "")


def _provider_credential(request: Request) -> Credential:
    """Resolve a Run's immutable Provider before falling back to current settings."""
    raw_run_id = request.headers.get(RUN_ID_HEADER)
    if raw_run_id:
        try:
            run_id = int(raw_run_id)
        except ValueError:
            run_id = 0
        if run_id > 0:
            with SessionLocal() as db:
                run = db.get(Run, run_id)
                if run and run.provider_url and run.credential_fingerprint:
                    token = _request_token(request)
                    if not token or not hmac.compare_digest(
                        token_fingerprint(token), run.credential_fingerprint
                    ):
                        raise HTTPException(401, "Invalid provider proxy credential")
                    return Credential(
                        run.provider_url, token, run.credential_fingerprint
                    )
    credential = read_credential(credential_path())
    if not _authorized(request, credential.token):
        raise HTTPException(401, "Invalid provider proxy credential")
    return credential


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
    (
        max_concurrency,
        max_retries,
        stream_max_retries,
        retry_interval_seconds,
    ) = _provider_request_policy()
    active_requests, waiting_for_concurrency = _PROVIDER_CONCURRENCY.snapshot()
    response_stats = provider_response_stats_last_60_seconds(current)
    return {
        "enabled": rpm > 0,
        "rpm": max(rpm, 0),
        "sent_last_60_seconds": len(sent),
        "queued_requests": len(waiting),
        "available_now": available,
        "next_release_seconds": round(next_release, 1),
        "max_concurrency": max_concurrency,
        "active_requests": active_requests,
        "waiting_for_concurrency": waiting_for_concurrency,
        "max_retries": max_retries,
        "stream_max_retries": stream_max_retries,
        "retry_interval_seconds": retry_interval_seconds,
        "actual_requests_last_60_seconds": actual_provider_requests_last_60_seconds(current),
        **response_stats,
    }


async def _open_provider_response(
    *,
    client,
    method: str,
    target: str,
    headers: dict,
    body: bytes,
    identity: tuple[int, str] | None,
    max_concurrency: int,
    max_retries: int,
    stream_max_retries: int,
    retry_interval_seconds: int,
    stream_retry: bool,
    stream_retry_number: int,
    require_success: bool,
):
    for attempt in range(max_retries + 1):
        delay = await asyncio.to_thread(reserve_provider_request)
        if delay:
            await asyncio.sleep(delay)
        await _PROVIDER_CONCURRENCY.acquire(max_concurrency)
        record_actual_provider_attempt(
            identity,
            attempt,
            max_retries,
            stream_retry=stream_retry,
            stream_retry_number=stream_retry_number,
            stream_max_retries=stream_max_retries,
        )
        try:
            upstream = await client.send(
                client.build_request(method, target, headers=headers, content=body),
                stream=True,
            )
        except httpx.HTTPError as exc:
            await _PROVIDER_CONCURRENCY.release()
            record_provider_response(identity, 502)
            if attempt >= max_retries:
                raise HTTPException(
                    502, f"Provider proxy request failed: {exc}"
                ) from exc
            if retry_interval_seconds:
                await asyncio.sleep(retry_interval_seconds)
            continue
        except BaseException:
            await _PROVIDER_CONCURRENCY.release()
            raise

        response_headers = {
            key: value for key, value in upstream.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
        }
        record_provider_response(identity, upstream.status_code)
        if upstream.status_code not in RETRYABLE_STATUS_CODES:
            if require_success and not _is_success_status(upstream.status_code):
                try:
                    content = await upstream.aread()
                finally:
                    await _close_upstream_and_release(upstream)
                return None, response_headers, (upstream.status_code, content)
            return upstream, response_headers, None

        read_error = None
        try:
            content = await upstream.aread()
        except httpx.HTTPError as exc:
            read_error = exc
            content = b""
        except BaseException:
            await _close_upstream_and_release(upstream)
            raise
        await _close_upstream_and_release(upstream)
        status_code = upstream.status_code
        if status_code == 429:
            await _record_limit_event_safely(status_code, content)
        if attempt >= max_retries:
            if read_error is not None and not content:
                raise HTTPException(
                    502, f"Provider proxy response failed: {read_error}"
                ) from read_error
            return None, response_headers, (status_code, content)
        if retry_interval_seconds:
            await asyncio.sleep(retry_interval_seconds)

    raise HTTPException(502, "Provider proxy request failed after retries")


async def _buffer_and_close_upstream(upstream):
    spool = None
    try:
        spool, captured = await _buffer_upstream_response(upstream)
    except BaseException:
        await _close_upstream_and_release(upstream)
        raise
    try:
        await _close_upstream_and_release(upstream)
    except BaseException:
        spool.close()
        raise
    return spool, captured


async def forward_provider_request(request: Request, path: str):
    credential = _provider_credential(request)
    body = await request.body()
    headers = {
        key: value for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS | {RUN_ID_HEADER, TRIAL_ID_HEADER}
    }
    # SSE completeness is validated from the raw upstream bytes. Explicitly
    # disable content compression so all three agent protocols remain parseable.
    headers["accept-encoding"] = "identity"
    identity = _provider_request_identity(request)
    target = _target_url(credential.url, path, request.url.query)
    (
        max_concurrency,
        max_retries,
        stream_max_retries,
        retry_interval_seconds,
    ) = _provider_request_policy()
    client = httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=30), follow_redirects=False)
    try:
        upstream, response_headers, terminal_response = await _open_provider_response(
            client=client,
            method=request.method,
            target=target,
            headers=headers,
            body=body,
            identity=identity,
            max_concurrency=max_concurrency,
            max_retries=max_retries,
            stream_max_retries=stream_max_retries,
            retry_interval_seconds=retry_interval_seconds,
            stream_retry=False,
            stream_retry_number=0,
            require_success=False,
        )
    except BaseException:
        await client.aclose()
        raise
    if terminal_response is not None:
        status_code, content = terminal_response
        await client.aclose()
        return Response(content, status_code, headers=response_headers)
    if upstream is None:
        await client.aclose()
        raise HTTPException(502, "Provider proxy request failed after retries")

    if not _is_success_status(upstream.status_code):
        async def passthrough_stream():
            captured = bytearray()
            try:
                async for chunk in upstream.aiter_raw():
                    if len(captured) < 128_000:
                        captured.extend(chunk[:128_000 - len(captured)])
                    yield chunk
            finally:
                try:
                    await _close_upstream_and_release(upstream)
                finally:
                    await client.aclose()
                if captured:
                    await _record_limit_event_safely(
                        upstream.status_code, bytes(captured)
                    )

        return StreamingResponse(
            passthrough_stream(),
            status_code=upstream.status_code,
            headers=response_headers,
            media_type=upstream.headers.get("content-type"),
        )

    async def stream():
        current_upstream = upstream
        downstream_is_sse = _is_sse_response(upstream.headers)
        last_error: Exception | None = None
        try:
            for stream_attempt in range(stream_max_retries + 1):
                if stream_attempt > 0:
                    if retry_interval_seconds:
                        remaining = float(retry_interval_seconds)
                        while remaining > 0:
                            wait = min(
                                remaining, SSE_KEEPALIVE_INTERVAL_SECONDS
                            )
                            await asyncio.sleep(wait)
                            remaining -= wait
                            if downstream_is_sse and remaining > 0:
                                yield b": deepswe waiting to retry provider stream\n\n"

                    open_task = asyncio.create_task(
                        _open_provider_response(
                            client=client,
                            method=request.method,
                            target=target,
                            headers=headers,
                            body=body,
                            identity=identity,
                            max_concurrency=max_concurrency,
                            max_retries=max_retries,
                            stream_max_retries=stream_max_retries,
                            retry_interval_seconds=retry_interval_seconds,
                            stream_retry=True,
                            stream_retry_number=stream_attempt,
                            require_success=True,
                        )
                    )
                    try:
                        if downstream_is_sse:
                            while not open_task.done():
                                done, _pending = await asyncio.wait(
                                    {open_task},
                                    timeout=SSE_KEEPALIVE_INTERVAL_SECONDS,
                                )
                                if not done:
                                    yield b": deepswe retrying provider stream\n\n"
                        (
                            current_upstream,
                            _headers,
                            terminal_response,
                        ) = await open_task
                        if terminal_response is not None:
                            status_code, _content = terminal_response
                            raise RuntimeError(
                                "Provider stream retry exhausted request retries "
                                f"with HTTP {status_code}"
                            )
                        if current_upstream is None:
                            raise RuntimeError(
                                "Provider stream retry produced no response"
                            )
                    except asyncio.CancelledError:
                        if not open_task.done():
                            open_task.cancel()
                            try:
                                await open_task
                            except BaseException:
                                pass
                        raise
                    except Exception as exc:
                        last_error = exc
                        record_provider_stream_failure(
                            identity,
                            stream_attempt,
                            stream_max_retries,
                        )
                        logger.warning(
                            "Provider stream resend %s/%s failed for %s: %s",
                            stream_attempt,
                            stream_max_retries,
                            identity,
                            exc,
                        )
                        continue

                if (
                    downstream_is_sse
                    and not _is_sse_response(current_upstream.headers)
                ):
                    await _close_upstream_and_release(current_upstream)
                    last_error = RuntimeError(
                        "Provider stream retry changed the response content type"
                    )
                    record_provider_stream_failure(
                        identity,
                        stream_attempt,
                        stream_max_retries,
                    )
                    continue

                is_sse = _is_sse_response(current_upstream.headers)
                read_task = asyncio.create_task(
                    _buffer_and_close_upstream(current_upstream)
                )
                try:
                    if is_sse:
                        while not read_task.done():
                            done, _pending = await asyncio.wait(
                                {read_task},
                                timeout=SSE_KEEPALIVE_INTERVAL_SECONDS,
                            )
                            if not done:
                                yield b": deepswe buffering provider response\n\n"
                    spool, captured = await read_task
                    if is_sse and not _spool_has_sse_terminal_event(spool):
                        spool.close()
                        raise IncompleteProviderStreamError(
                            "Provider SSE stream ended before a terminal event"
                        )
                    if captured:
                        await _record_limit_event_safely(
                            current_upstream.status_code, captured
                        )
                    for chunk in _iter_spooled_response(spool):
                        yield chunk
                    return
                except asyncio.CancelledError:
                    if not read_task.done():
                        read_task.cancel()
                        try:
                            await read_task
                        except BaseException:
                            pass
                    raise
                except Exception as exc:
                    last_error = exc
                    record_provider_stream_failure(
                        identity,
                        stream_attempt,
                        stream_max_retries,
                    )
                    logger.warning(
                        "Provider stream attempt %s/%s failed for %s: %s",
                        stream_attempt + 1,
                        stream_max_retries + 1,
                        identity,
                        exc,
                    )
                    continue
            if last_error is not None:
                raise last_error
            raise RuntimeError("Provider stream retries produced no response")
        finally:
            await client.aclose()

    return StreamingResponse(
        stream(), status_code=upstream.status_code, headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )
