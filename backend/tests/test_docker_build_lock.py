import threading
import time

from app.pier_retry_patch import docker_build_lock


def test_recognizes_only_compose_build_commands():
    assert docker_build_lock.is_compose_build(["build"])
    assert docker_build_lock.is_compose_build(["build", "--pull"])
    assert not docker_build_lock.is_compose_build(["up", "--detach"])
    assert not docker_build_lock.is_compose_build([])


def test_build_lock_serializes_callers(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSWE_DOCKER_BUILD_LOCK", str(tmp_path / "build.lock"))
    monkeypatch.setattr(docker_build_lock, "POLL_SECONDS", 0.01)
    first = docker_build_lock.acquire_docker_build_lock()
    acquired = threading.Event()
    second_handle = []

    def acquire_second():
        second_handle.append(docker_build_lock.acquire_docker_build_lock())
        acquired.set()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    time.sleep(0.05)
    assert not acquired.is_set()

    docker_build_lock.release_docker_build_lock(first)
    assert acquired.wait(1)
    docker_build_lock.release_docker_build_lock(second_handle[0])
    thread.join(timeout=1)
