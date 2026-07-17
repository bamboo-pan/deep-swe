"""Serialize Pier image builds across independent run processes."""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO


POLL_SECONDS = 0.2


def is_compose_build(command: Sequence[str]) -> bool:
    return bool(command and command[0] == "build")


def _lock_path() -> Path:
    configured = os.environ.get("DEEPSWE_DOCKER_BUILD_LOCK")
    return Path(configured) if configured else Path(tempfile.gettempdir()) / (
        "deepswe-pier-docker-build.lock"
    )


def acquire_docker_build_lock() -> BinaryIO:
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()

    if os.name == "nt":
        import msvcrt

        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return handle
            except OSError:
                time.sleep(POLL_SECONDS)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle


def release_docker_build_lock(handle: BinaryIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
