"""Deterministic, non-overlapping Docker subnets for Pier trial networks."""

import hashlib
import ipaddress
import os
import re


DEFAULT_NETWORK_POOL = "10.240.0.0/12"
TRIAL_NETWORK_PREFIX = 29
PROVIDER_PROXY_HOST = "host.docker.internal"
PROVIDER_PROXY_PORT = 8765
DEFAULT_SQUID_READ_TIMEOUT_SECONDS = 1800
RUN_ID_HEADER = "X-DeepSWE-Run-ID"
TRIAL_ID_HEADER = "X-DeepSWE-Trial-ID"


def provider_proxy_domains(domains: list[str]) -> list[str]:
    return sorted(set([*domains, PROVIDER_PROXY_HOST]))


def allow_provider_proxy_port(
    squid_config: str,
    *,
    read_timeout_seconds: int = DEFAULT_SQUID_READ_TIMEOUT_SECONDS,
) -> str:
    timeout = max(int(read_timeout_seconds), 60)
    updated = squid_config.replace(
        "acl SSL_ports port 443 9887",
        f"acl SSL_ports port 443 {PROVIDER_PROXY_PORT} 9887",
    ).replace(
        "acl Safe_ports port 80 443 9887",
        f"acl Safe_ports port 80 443 {PROVIDER_PROXY_PORT} 9887",
    )
    directive = f"read_timeout {timeout} seconds"
    if "read_timeout " in updated:
        return re.sub(r"(?m)^read_timeout\s+[^\n]+$", directive, updated)
    if "cache deny all" in updated:
        return updated.replace("cache deny all", f"{directive}\ncache deny all", 1)
    return updated.rstrip() + f"\n{directive}\n"


def add_provider_telemetry_headers(
    squid_config: str,
    *,
    run_id: int,
    trial_id: str,
) -> str:
    """Tag HTTP requests leaving a Trial's private Squid proxy."""
    if int(run_id) < 1 or not trial_id or len(trial_id) > 300:
        raise ValueError("Invalid Provider telemetry identity")
    if re.search(r"[\r\n]", trial_id):
        raise ValueError("Invalid Provider telemetry Trial id")
    directives = (
        f"acl deepswe_provider dstdomain {PROVIDER_PROXY_HOST}\n"
        f"acl deepswe_provider_port port {PROVIDER_PROXY_PORT}\n"
        f"request_header_add {RUN_ID_HEADER} {int(run_id)} "
        "deepswe_provider deepswe_provider_port\n"
        f"request_header_add {TRIAL_ID_HEADER} {trial_id} "
        "deepswe_provider deepswe_provider_port"
    )
    if RUN_ID_HEADER in squid_config or TRIAL_ID_HEADER in squid_config:
        return squid_config
    if "cache deny all" in squid_config:
        return squid_config.replace(
            "cache deny all", f"{directives}\ncache deny all", 1
        )
    return squid_config.rstrip() + f"\n{directives}\n"


def trial_network_subnets(identity: str) -> tuple[str, str]:
    """Return stable internal/external subnets without using Docker's default pool."""
    pool = ipaddress.ip_network(
        os.environ.get("DEEPSWE_DOCKER_NETWORK_POOL", DEFAULT_NETWORK_POOL),
        strict=True,
    )
    if pool.version != 4 or pool.prefixlen > TRIAL_NETWORK_PREFIX:
        raise ValueError(
            f"DEEPSWE_DOCKER_NETWORK_POOL must be an IPv4 /{TRIAL_NETWORK_PREFIX} or larger pool"
        )
    subnet_size = 1 << (32 - TRIAL_NETWORK_PREFIX)
    pair_count = pool.num_addresses // (subnet_size * 2)
    if pair_count < 1:
        raise ValueError("DEEPSWE_DOCKER_NETWORK_POOL is too small for a network pair")
    digest = hashlib.sha256(identity.encode("utf-8", errors="replace")).digest()
    pair_index = int.from_bytes(digest[:8], "big") % pair_count
    start = int(pool.network_address) + pair_index * subnet_size * 2
    internal = ipaddress.ip_network((start, TRIAL_NETWORK_PREFIX))
    external = ipaddress.ip_network((start + subnet_size, TRIAL_NETWORK_PREFIX))
    return str(internal), str(external)
