"""Classify retryable infrastructure failures without retrying code failures."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar


TRANSIENT_EXCEPTION_TYPE = "TransientAgentInfrastructureError"
TRANSIENT_VERIFIER_EXCEPTION_TYPE = "TransientVerifierInfrastructureError"


class TransientVerifierInfrastructureError(RuntimeError):
    """A verifier infrastructure failure that exhausted local retries."""


_TRANSIENT_MARKERS = (
    "429 too many requests",
    "rate limit exceeded",
    "toomanyrequests",
    "unexpected status 429",
    "unexpected status 500",
    "unexpected status 502",
    "unexpected status 503",
    "unexpected status 504",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "api error: the operation timed out",
    '"terminal_reason":"api_error"',
    "connection reset",
    "connection refused",
    "connection timed out",
    "connection closed by peer",
    "failed to connect",
    "i/o timeout",
    "network is unreachable",
    "no such host",
    "tls handshake timeout",
    "temporary failure resolving",
    "upstream connect error",
    "service unavailable",
    "server overloaded",
    "ssl_error_syscall",
    "curl: (35)",
)

_NETWORK_ERROR_CONTEXT = re.compile(
    r"(?:\b[a-z]*(?:connection|protocol|transport|read|write|timeout|network|api)[a-z]*error\b"
    r"|\b(?:exception|traceback|failed|failure)\b)",
    re.IGNORECASE,
)

_REGISTRY_EOF_CONTEXT = re.compile(
    r"(?:docker compose command failed"
    r"|failed to (?:do request|resolve source metadata|fetch|pull)"
    r"|\b(?:head|get)\s+\"https?://[^\"]+"
    r"|\b(?:registry|manifest|source metadata)\b)",
    re.IGNORECASE,
)

_VERIFIER_INFRASTRUCTURE_CONTEXT = re.compile(
    r"(?:docker compose command failed for environment"
    r"|failed to (?:do request|resolve source metadata|fetch|pull)"
    r"|\b(?:registry|manifest|source metadata)\b)",
    re.IGNORECASE,
)

_T = TypeVar("_T")


def _has_contextual_eof(message: str) -> bool:
    for line in message.splitlines():
        lowered = line.lower()
        if "unexpected eof" in lowered and _NETWORK_ERROR_CONTEXT.search(line):
            return True
        if re.search(r"\beof\b", line, re.IGNORECASE) and _REGISTRY_EOF_CONTEXT.search(line):
            return True
    return False


def _message_is_transient(exception_type: str | None, message: str | None) -> bool:
    if exception_type in {
        TRANSIENT_EXCEPTION_TYPE,
        TRANSIENT_VERIFIER_EXCEPTION_TYPE,
        "ConnectionError",
        "TimeoutError",
    }:
        return True
    if exception_type not in {"NonZeroAgentExitCodeError", "RuntimeError"} or not message:
        return False
    lowered = " ".join(message.lower().split())
    return any(marker in lowered for marker in _TRANSIENT_MARKERS) or _has_contextual_eof(message)


def is_transient_agent_failure(
    exception_type: str | None,
    message: str | None,
    *,
    agent_log_tail: str | None = None,
) -> bool:
    """Return true for known transient infrastructure failures.

    Agent logs are only allowed to explain a non-zero agent CLI exit. A verifier
    or environment RuntimeError must be classified from its own exception text,
    otherwise an unrelated historical 503 in the agent log can hide the cause.
    """
    if _message_is_transient(exception_type, message):
        return True
    return (
        exception_type == "NonZeroAgentExitCodeError"
        and _message_is_transient(exception_type, agent_log_tail)
    )


def is_transient_verifier_failure(
    exception_type: str | None,
    message: str | None,
) -> bool:
    """Return true only when a verifier failure has infrastructure context."""
    if exception_type in {
        TRANSIENT_VERIFIER_EXCEPTION_TYPE,
        "ConnectionError",
        "TimeoutError",
    }:
        return True
    return bool(
        exception_type == "RuntimeError"
        and message
        and _VERIFIER_INFRASTRUCTURE_CONTEXT.search(message)
        and _message_is_transient(exception_type, message)
    )


async def retry_transient_verifier(
    operation: Callable[[], Awaitable[_T]],
    *,
    max_retries: int,
    delays: Sequence[float],
    on_retry: Callable[[int, int, float, Exception], None] | None = None,
) -> _T:
    """Retry transient verifier infrastructure failures without rerunning the agent."""
    retry_limit = max(int(max_retries), 0)
    retry_delays = tuple(max(float(delay), 0.0) for delay in delays) or (1.0,)

    for attempt in range(retry_limit + 1):
        try:
            return await operation()
        except Exception as exc:
            if not is_transient_verifier_failure(type(exc).__name__, str(exc)):
                raise
            if attempt >= retry_limit:
                raise TransientVerifierInfrastructureError(
                    "Verifier infrastructure failed after "
                    f"{attempt + 1} attempt(s): {exc}"
                ) from exc

            delay = retry_delays[min(attempt, len(retry_delays) - 1)]
            if on_retry is not None:
                on_retry(attempt + 1, retry_limit, delay, exc)
            if delay:
                await asyncio.sleep(delay)

    raise AssertionError("unreachable")
