"""Classify retryable agent CLI failures without retrying model/code failures."""

TRANSIENT_EXCEPTION_TYPE = "TransientAgentInfrastructureError"

_TRANSIENT_MARKERS = (
    "429 too many requests",
    "rate limit exceeded",
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
    "failed to connect",
    "tls handshake timeout",
    "temporary failure resolving",
    "upstream connect error",
    "service unavailable",
    "server overloaded",
    "unexpected eof",
)


def is_transient_agent_failure(exception_type: str | None, message: str | None) -> bool:
    """Return true only for known transient network/provider failures."""
    if exception_type in {"ConnectionError", "TimeoutError"}:
        return True
    if exception_type not in {"NonZeroAgentExitCodeError", "RuntimeError"} or not message:
        return False
    lowered = message.lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)
