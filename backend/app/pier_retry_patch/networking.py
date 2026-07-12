"""Deterministic, non-overlapping Docker subnets for Pier trial networks."""

import hashlib
import ipaddress
import os


DEFAULT_NETWORK_POOL = "10.240.0.0/12"
TRIAL_NETWORK_PREFIX = 29


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
