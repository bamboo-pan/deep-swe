"""Classify retryable agent CLI failures without retrying model/code failures."""

import re

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
    "ssl_error_syscall",  # curl: (35)，镜像构建下载 GitHub release 时 TLS 被重置
    "curl: (35)",
)

_NETWORK_ERROR_CONTEXT = re.compile(
    r"(?:\b[a-z]*(?:connection|protocol|transport|read|write|timeout|network|api)[a-z]*error\b"
    r"|\b(?:exception|traceback|failed|failure)\b)",
    re.IGNORECASE,
)


def _has_contextual_unexpected_eof(message: str) -> bool:
    return any(
        "unexpected eof" in line.lower() and _NETWORK_ERROR_CONTEXT.search(line)
        for line in message.splitlines()
    )


def is_transient_agent_failure(exception_type: str | None, message: str | None) -> bool:
    """Return true only for known transient network/provider failures."""
    if exception_type in {"ConnectionError", "TimeoutError"}:
        return True
    if exception_type not in {"NonZeroAgentExitCodeError", "RuntimeError"} or not message:
        return False
    # apt 真实输出是「502  Bad Gateway」（双空格），归一空白后再做子串匹配
    lowered = " ".join(message.lower().split())
    return any(marker in lowered for marker in _TRANSIENT_MARKERS) or _has_contextual_unexpected_eof(message)
