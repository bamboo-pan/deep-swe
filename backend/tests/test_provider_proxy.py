import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app import provider_proxy
from app.database import SessionLocal, init_db
from app.models import Run, Setting
from app.security import token_fingerprint
from app.provider_proxy import (
    REQUEST_SCHEDULE_KEY, RUN_ID_HEADER, TRIAL_ID_HEADER,
    _ProviderConcurrencyLimiter, _reset_provider_telemetry, _target_url,
    actual_provider_requests_last_60_seconds, provider_queue_status,
    provider_response_stats_last_60_seconds, provider_trial_status,
    reserve_provider_request,
)


def test_target_url_handles_openai_and_anthropic_paths():
    assert _target_url("http://provider.example/v1", "responses", "") == (
        "http://provider.example/v1/responses"
    )
    assert _target_url("http://provider.example/v1", "v1/messages", "beta=1") == (
        "http://provider.example/v1/messages?beta=1"
    )


def test_provider_proxy_keeps_run_provider_after_global_credential_switch(monkeypatch):
    init_db()
    with SessionLocal() as db:
        run = Run(
            status="running",
            job_name=f"provider-snapshot-{uuid.uuid4().hex}",
            agent="codex",
            model="model-a",
            reasoning_effort="high",
            tasks_json='["task-a"]',
            provider_url="https://provider-a.example/v1",
            credential_fingerprint=token_fingerprint("token-a"),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id

    monkeypatch.setattr(
        provider_proxy,
        "read_credential",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("Run-scoped request must not read global credentials")
        ),
    )
    request = Request({
        "type": "http",
        "headers": [
            (b"authorization", b"Bearer token-a"),
            (RUN_ID_HEADER.encode(), str(run_id).encode()),
            (TRIAL_ID_HEADER.encode(), b"task-a__one"),
        ],
    })
    try:
        credential = provider_proxy._provider_credential(request)
        assert credential.url == "https://provider-a.example/v1"
        assert credential.token == "token-a"

        switched_request = Request({
            "type": "http",
            "headers": [
                (b"authorization", b"Bearer token-b"),
                (RUN_ID_HEADER.encode(), str(run_id).encode()),
            ],
        })
        with pytest.raises(provider_proxy.HTTPException) as exc:
            provider_proxy._provider_credential(switched_request)
        assert exc.value.status_code == 401
    finally:
        with SessionLocal() as db:
            saved = db.get(Run, run_id)
            if saved:
                db.delete(saved)
                db.commit()


def test_provider_rpm_reserves_every_http_request():
    init_db()
    keys = ("provider_rpm", REQUEST_SCHEDULE_KEY)
    with SessionLocal() as db:
        original = {
            key: row.value
            for key in keys
            if (row := db.get(Setting, key)) is not None
        }
        for key in keys:
            row = db.get(Setting, key)
            if row:
                db.delete(row)
        db.add(Setting(key="provider_rpm", value=json.dumps(30)))
        db.commit()
    try:
        for _ in range(30):
            assert reserve_provider_request(now=1000.0) == 0
        status = provider_queue_status(now=1000.0)
        assert status["enabled"] is True
        assert status["rpm"] == 30
        assert status["sent_last_60_seconds"] == 30
        assert status["queued_requests"] == 0
        assert status["available_now"] == 0
        assert status["next_release_seconds"] == 60.0
        assert status["active_requests"] == 0
        assert status["waiting_for_concurrency"] == 0
        assert reserve_provider_request(now=1000.0) == pytest.approx(60.0)
        status = provider_queue_status(now=1030.0)
        assert status["queued_requests"] == 1
        assert status["next_release_seconds"] == 30.0
        assert reserve_provider_request(now=1030.0) == pytest.approx(30.0)
        assert reserve_provider_request(now=1060.0) == 0
    finally:
        with SessionLocal() as db:
            for key in keys:
                row = db.get(Setting, key)
                if row:
                    db.delete(row)
            for key, value in original.items():
                db.add(Setting(key=key, value=value))
            db.commit()


def test_provider_concurrency_limiter_caps_active_requests():
    limiter = _ProviderConcurrencyLimiter()

    async def scenario():
        await limiter.acquire(1)
        acquired = asyncio.Event()

        async def acquire_second():
            await limiter.acquire(1)
            acquired.set()

        task = asyncio.create_task(acquire_second())
        await asyncio.sleep(0.05)
        assert not acquired.is_set()
        assert limiter.snapshot() == (1, 1)
        await limiter.release()
        await asyncio.wait_for(task, timeout=1)
        assert acquired.is_set()
        assert limiter.snapshot() == (1, 0)
        await limiter.release()
        assert limiter.snapshot() == (0, 0)

    asyncio.run(scenario())


def test_cancelled_provider_waiter_does_not_leak_a_slot():
    limiter = _ProviderConcurrencyLimiter()

    async def scenario():
        await limiter.acquire(1)
        waiter = asyncio.create_task(limiter.acquire(1))
        while limiter.snapshot()[1] == 0:
            await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert limiter.snapshot() == (1, 0)
        await limiter.release()
        assert limiter.snapshot() == (0, 0)

    asyncio.run(scenario())


def test_provider_proxy_retries_429_before_returning_stream(monkeypatch):
    _reset_provider_telemetry()
    responses = [
        (429, b'{"error":{"message":"rate limited"}}'),
        (200, b'{"ok":true}'),
    ]
    clients = []

    class FakeUpstream:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self.body = body
            self.headers = {"content-type": "application/json"}

        async def aread(self):
            return self.body

        async def aclose(self):
            return None

        async def aiter_raw(self):
            yield self.body

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.send_count = 0
            clients.append(self)

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, stream=True):
            self.send_count += 1
            status, body = responses.pop(0)
            return FakeUpstream(status, body)

        async def aclose(self):
            return None

    monkeypatch.setattr(provider_proxy.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        provider_proxy, "read_credential",
        lambda _path: SimpleNamespace(token="secret", url="http://provider.example/v1"),
    )
    monkeypatch.setattr(provider_proxy, "credential_path", lambda: None)
    monkeypatch.setattr(provider_proxy, "reserve_provider_request", lambda: 0)
    monkeypatch.setattr(provider_proxy, "record_limit_event", lambda *args: None)
    monkeypatch.setattr(provider_proxy, "_provider_request_policy", lambda: (1, 2, 3, 0))

    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/provider/responses",
        "raw_path": b"/api/provider/responses",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer secret"),
            (RUN_ID_HEADER.encode(), b"42"),
            (TRIAL_ID_HEADER.encode(), b"task-a__abc"),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8765),
    }, receive)

    async def scenario():
        response = await provider_proxy.forward_provider_request(request, "responses")
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return response.status_code, b"".join(chunks)

    status_code, body = asyncio.run(scenario())
    assert status_code == 200
    assert body == b'{"ok":true}'
    assert clients[0].send_count == 2
    assert actual_provider_requests_last_60_seconds() == 2
    assert provider_trial_status(42, "task-a__abc") == {
        "provider_response_code": 200,
        "provider_request_count": 2,
        "provider_retries_used": 0,
        "provider_max_retries": 2,
        "provider_stream_retries_used": 0,
        "provider_stream_max_retries": 3,
        "updated_at": provider_trial_status(42, "task-a__abc")["updated_at"],
    }
    assert provider_proxy._PROVIDER_CONCURRENCY.snapshot() == (0, 0)


def test_new_or_successful_request_clears_previous_failure_telemetry():
    _reset_provider_telemetry()
    identity = (7, "task-a__xyz")
    provider_proxy.record_actual_provider_attempt(identity, 0, 4, now=1000)
    provider_proxy.record_provider_response(identity, 429, now=1001)
    provider_proxy.record_actual_provider_attempt(identity, 4, 4, now=1002)
    assert provider_trial_status(*identity)["provider_retries_used"] == 4

    provider_proxy.record_actual_provider_attempt(identity, 0, 4, now=1003)
    pending = provider_trial_status(*identity)
    assert pending["provider_response_code"] is None
    assert pending["provider_retries_used"] == 0

    provider_proxy.record_provider_response(identity, 200, now=1004)
    successful = provider_trial_status(*identity)
    assert successful["provider_response_code"] == 200
    assert successful["provider_retries_used"] == 0


def test_provider_response_stats_report_failed_request_share():
    _reset_provider_telemetry()
    provider_proxy.record_provider_response(None, 200, now=1000)
    provider_proxy.record_provider_response(None, 429, now=1001)
    provider_proxy.record_provider_response(None, 502, now=1002)

    assert provider_response_stats_last_60_seconds(now=1002) == {
        "completed_requests_last_60_seconds": 3,
        "failed_requests_last_60_seconds": 2,
        "failure_rate_last_60_seconds": 66.7,
        "response_code_counts_last_60_seconds": {"200": 1, "429": 1, "502": 1},
        "stream_failures_last_60_seconds": 0,
    }
    assert provider_response_stats_last_60_seconds(now=1061) == {
        "completed_requests_last_60_seconds": 1,
        "failed_requests_last_60_seconds": 1,
        "failure_rate_last_60_seconds": 100.0,
        "response_code_counts_last_60_seconds": {"502": 1},
        "stream_failures_last_60_seconds": 0,
    }


def test_limit_event_storage_failure_does_not_abort_retry(monkeypatch):
    responses = [429, 200]

    class FakeUpstream:
        headers = {"content-type": "application/json"}

        def __init__(self, status_code):
            self.status_code = status_code

        async def aread(self):
            return b'{"error":{"message":"rate limited"}}'

        async def aclose(self):
            return None

        async def aiter_raw(self):
            yield b'{}'

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, stream=True):
            return FakeUpstream(responses.pop(0))

        async def aclose(self):
            return None

    monkeypatch.setattr(provider_proxy.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        provider_proxy, "read_credential",
        lambda _path: SimpleNamespace(token="secret", url="http://provider.example/v1"),
    )
    monkeypatch.setattr(provider_proxy, "credential_path", lambda: None)
    monkeypatch.setattr(provider_proxy, "reserve_provider_request", lambda: 0)
    monkeypatch.setattr(
        provider_proxy, "record_limit_event",
        lambda *args: (_ for _ in ()).throw(RuntimeError("sqlite unavailable")),
    )
    monkeypatch.setattr(provider_proxy, "_provider_request_policy", lambda: (1, 1, 3, 0))

    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/provider/responses",
        "raw_path": b"/api/provider/responses", "query_string": b"",
        "headers": [(b"authorization", b"Bearer secret")],
        "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8765),
    }, receive)

    async def scenario():
        response = await provider_proxy.forward_provider_request(request, "responses")
        body = b"".join([chunk async for chunk in response.body_iterator])
        return response.status_code, body

    assert asyncio.run(scenario()) == (200, b"{}")
    assert provider_proxy._PROVIDER_CONCURRENCY.snapshot() == (0, 0)


def test_provider_stream_close_error_still_releases_concurrency(monkeypatch):
    class FakeUpstream:
        status_code = 200
        headers = {"content-type": "application/json"}

        async def aiter_raw(self):
            yield b'{}'

        async def aclose(self):
            raise RuntimeError("close failed")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, stream=True):
            return FakeUpstream()

        async def aclose(self):
            return None

    monkeypatch.setattr(provider_proxy.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        provider_proxy, "read_credential",
        lambda _path: SimpleNamespace(token="secret", url="http://provider.example/v1"),
    )
    monkeypatch.setattr(provider_proxy, "credential_path", lambda: None)
    monkeypatch.setattr(provider_proxy, "reserve_provider_request", lambda: 0)
    monkeypatch.setattr(provider_proxy, "record_limit_event", lambda *args: None)
    monkeypatch.setattr(provider_proxy, "_provider_request_policy", lambda: (1, 0, 0, 0))

    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/provider/responses",
        "raw_path": b"/api/provider/responses",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer secret")],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8765),
    }, receive)

    async def scenario():
        response = await provider_proxy.forward_provider_request(request, "responses")
        with pytest.raises(RuntimeError, match="close failed"):
            async for _chunk in response.body_iterator:
                pass

    asyncio.run(scenario())
    assert provider_proxy._PROVIDER_CONCURRENCY.snapshot() == (0, 0)


@pytest.mark.parametrize(
    ("path", "partial", "complete", "raise_transport_error"),
    [
        (
            "responses",
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta"}\n\n',
            b'event: response.completed\ndata: {"type":"response.completed"}\n\n',
            True,
        ),
        (
            "v1/messages",
            b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            False,
        ),
        (
            "chat/completions",
            b'data: {"choices":[{"delta":{"content":"half"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"done"}}]}\n\ndata: [DONE]\n\n',
            True,
        ),
    ],
)
def test_successful_sse_stream_is_retried_before_any_agent_sees_partial_output(
    monkeypatch, path, partial, complete, raise_transport_error,
):
    """Responses, Claude Messages and OpenAI Chat share the same proxy policy."""
    _reset_provider_telemetry()
    clients = []

    class FakeUpstream:
        status_code = 200
        headers = {"content-type": "text/event-stream; charset=utf-8"}

        def __init__(self, body, fail):
            self.body = body
            self.fail = fail

        async def aiter_raw(self):
            yield self.body
            if self.fail:
                raise provider_proxy.httpx.ReadError("stream disconnected")

        async def aclose(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.send_count = 0
            clients.append(self)

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, stream=True):
            self.send_count += 1
            if self.send_count == 1:
                return FakeUpstream(partial, raise_transport_error)
            return FakeUpstream(complete, False)

        async def aclose(self):
            return None

    monkeypatch.setattr(provider_proxy.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        provider_proxy, "read_credential",
        lambda _path: SimpleNamespace(token="secret", url="http://provider.example/v1"),
    )
    monkeypatch.setattr(provider_proxy, "credential_path", lambda: None)
    monkeypatch.setattr(provider_proxy, "reserve_provider_request", lambda: 0)
    monkeypatch.setattr(provider_proxy, "record_limit_event", lambda *args: None)
    monkeypatch.setattr(
        provider_proxy, "_provider_request_policy", lambda: (1, 0, 3, 0)
    )

    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": f"/api/provider/{path}",
        "raw_path": f"/api/provider/{path}".encode(), "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer secret"),
            (RUN_ID_HEADER.encode(), b"51"),
            (TRIAL_ID_HEADER.encode(), b"task-stream__one"),
        ],
        "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8765),
    }, receive)

    async def scenario():
        response = await provider_proxy.forward_provider_request(request, path)
        return b"".join([chunk async for chunk in response.body_iterator])

    assert asyncio.run(scenario()) == complete
    assert clients[0].send_count == 2
    telemetry = provider_trial_status(51, "task-stream__one")
    assert telemetry["provider_request_count"] == 2
    assert telemetry["provider_stream_retries_used"] == 1
    assert telemetry["provider_stream_max_retries"] == 3
    assert provider_proxy._PROVIDER_CONCURRENCY.snapshot() == (0, 0)


def test_stream_retry_exhaustion_raises_after_three_resends(monkeypatch):
    _reset_provider_telemetry()
    clients = []

    class FakeUpstream:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_raw(self):
            yield b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta"}\n\n'

        async def aclose(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.send_count = 0
            self.closed = False
            clients.append(self)

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, stream=True):
            self.send_count += 1
            return FakeUpstream()

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(provider_proxy.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        provider_proxy, "read_credential",
        lambda _path: SimpleNamespace(token="secret", url="http://provider.example/v1"),
    )
    monkeypatch.setattr(provider_proxy, "credential_path", lambda: None)
    monkeypatch.setattr(provider_proxy, "reserve_provider_request", lambda: 0)
    monkeypatch.setattr(provider_proxy, "record_limit_event", lambda *args: None)
    monkeypatch.setattr(
        provider_proxy, "_provider_request_policy", lambda: (1, 0, 3, 0)
    )

    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/provider/responses",
        "raw_path": b"/api/provider/responses", "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer secret"),
            (RUN_ID_HEADER.encode(), b"52"),
            (TRIAL_ID_HEADER.encode(), b"task-stream__exhausted"),
        ],
        "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8765),
    }, receive)

    async def scenario():
        response = await provider_proxy.forward_provider_request(request, "responses")
        with pytest.raises(
            provider_proxy.IncompleteProviderStreamError,
            match="terminal event",
        ):
            async for _chunk in response.body_iterator:
                pass

    asyncio.run(scenario())
    assert clients[0].send_count == 4
    assert clients[0].closed is True
    telemetry = provider_trial_status(52, "task-stream__exhausted")
    assert telemetry["provider_stream_retries_used"] == 3
    assert telemetry["provider_stream_max_retries"] == 3
    assert provider_response_stats_last_60_seconds()[
        "stream_failures_last_60_seconds"
    ] == 4
    assert provider_proxy._PROVIDER_CONCURRENCY.snapshot() == (0, 0)


def test_sse_buffering_emits_keepalive_comments(monkeypatch):
    class FakeUpstream:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_raw(self):
            await asyncio.sleep(0.03)
            yield b'event: response.completed\ndata: {"type":"response.completed"}\n\n'

        async def aclose(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, stream=True):
            return FakeUpstream()

        async def aclose(self):
            return None

    monkeypatch.setattr(provider_proxy.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        provider_proxy, "read_credential",
        lambda _path: SimpleNamespace(token="secret", url="http://provider.example/v1"),
    )
    monkeypatch.setattr(provider_proxy, "credential_path", lambda: None)
    monkeypatch.setattr(provider_proxy, "reserve_provider_request", lambda: 0)
    monkeypatch.setattr(provider_proxy, "record_limit_event", lambda *args: None)
    monkeypatch.setattr(
        provider_proxy, "_provider_request_policy", lambda: (1, 0, 0, 0)
    )
    monkeypatch.setattr(provider_proxy, "SSE_KEEPALIVE_INTERVAL_SECONDS", 0.005)

    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/provider/responses",
        "raw_path": b"/api/provider/responses", "query_string": b"",
        "headers": [(b"authorization", b"Bearer secret")],
        "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8765),
    }, receive)

    async def scenario():
        response = await provider_proxy.forward_provider_request(request, "responses")
        return b"".join([chunk async for chunk in response.body_iterator])

    body = asyncio.run(scenario())
    assert b": deepswe buffering provider response\n\n" in body
    assert body.endswith(
        b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
    )
    assert provider_proxy._PROVIDER_CONCURRENCY.snapshot() == (0, 0)


def test_provider_rpm_zero_disables_waiting():
    init_db()
    with SessionLocal() as db:
        row = db.get(Setting, "provider_rpm")
        original = row.value if row else None
        if row:
            row.value = "0"
        else:
            db.add(Setting(key="provider_rpm", value="0"))
        db.commit()
    try:
        assert reserve_provider_request(now=1000.0) == 0
        assert reserve_provider_request(now=1000.0) == 0
    finally:
        with SessionLocal() as db:
            row = db.get(Setting, "provider_rpm")
            if original is None:
                if row:
                    db.delete(row)
            elif row:
                row.value = original
            else:
                db.add(Setting(key="provider_rpm", value=original))
            db.commit()
