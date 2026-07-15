import json
import json
import subprocess
from pathlib import Path

import pytest

from app import local_images


def write_task(tasks_dir: Path, task: str = "test-task") -> Path:
    task_dir = tasks_dir / task
    environment = task_dir / "environment"
    tests = task_dir / "tests"
    environment.mkdir(parents=True)
    tests.mkdir()
    (environment / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (environment / "fixture.txt").write_text("base\n", encoding="utf-8")
    (tests / "Dockerfile").write_text(
        "FROM deepswe-test-base:local\n", encoding="utf-8"
    )
    (tests / "test.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (task_dir / "task.toml").write_text(
        """
[verifier]
environment_mode = "separate"

[verifier.environment]
docker_image = "deepswe-test-verifier:local"
build_timeout_sec = 90

[environment]
docker_image = "deepswe-test-base:local"
build_timeout_sec = 120
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return task_dir


def test_discovers_environment_and_verifier_local_images(tmp_path: Path):
    write_task(tmp_path)
    specs = local_images.discover_local_image_specs(tmp_path, ["test-task"])
    assert [(spec.role, spec.image, spec.timeout_sec) for spec in specs] == [
        ("environment", "deepswe-test-base:local", 120.0),
        ("verifier", "deepswe-test-verifier:local", 90.0),
    ]
    assert specs[0].context == tmp_path / "test-task" / "environment"
    assert specs[1].context == tmp_path / "test-task" / "tests"


def test_ignores_registry_images_without_local_suffix(tmp_path: Path):
    task_dir = write_task(tmp_path)
    (task_dir / "task.toml").write_text(
        '[environment]\ndocker_image = "public.ecr.aws/example/task:v1"\n',
        encoding="utf-8",
    )
    assert local_images.discover_local_image_specs(tmp_path, ["test-task"]) == []


def test_rejects_inherited_local_verifier_image(tmp_path: Path):
    task_dir = write_task(tmp_path)
    (task_dir / "task.toml").write_text(
        """
[verifier]
environment_mode = "separate"

[environment]
docker_image = "deepswe-test-base:local"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="distinct"):
        local_images.discover_local_image_specs(tmp_path, ["test-task"])


def test_context_digest_changes_with_build_input_but_ignores_git(tmp_path: Path):
    task_dir = write_task(tmp_path)
    context = task_dir / "environment"
    first = local_images.build_context_digest(context)
    (context / ".git").mkdir()
    (context / ".git" / "HEAD").write_text("ignored", encoding="utf-8")
    assert local_images.build_context_digest(context) == first
    (context / "fixture.txt").write_text("changed\n", encoding="utf-8")
    assert local_images.build_context_digest(context) != first


def test_builds_once_then_reuses_matching_context(tmp_path: Path, monkeypatch):
    task_dir = write_task(tmp_path)
    specs = local_images.discover_local_image_specs(tmp_path, ["test-task"])
    base_digest = local_images.build_context_digest(specs[0].context)
    expected = {
        specs[0].image: base_digest,
        specs[1].image: local_images.build_context_digest(
            specs[1].context,
            dependency_digests=[f"{specs[0].image}={base_digest}"],
        ),
    }
    labels: dict[str, str] = {}
    builds: list[list[str]] = []

    def fake_run(command, **kwargs):
        if command[1:3] == ["image", "inspect"]:
            image = command[3]
            if image not in labels:
                return subprocess.CompletedProcess(command, 1, "", "missing")
            output = json.dumps(
                [{"Config": {"Labels": {local_images.CONTEXT_DIGEST_LABEL: labels[image]}}}]
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        assert command[1] == "build"
        builds.append(command)
        image = command[command.index("--tag") + 1]
        digest_label = command[command.index("--label") + 1]
        labels[image] = digest_label.split("=", 1)[1]
        kwargs["stdout"].write("built\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(local_images.shutil, "which", lambda name: "docker.exe")
    monkeypatch.setattr(local_images.subprocess, "run", fake_run)
    logs = tmp_path / "logs"
    first = local_images.ensure_local_task_images(
        tmp_path, ["test-task"], log_dir=logs
    )
    assert [result.action for result in first] == ["built", "built"]
    assert labels == expected
    assert len(builds) == 2
    assert all(result.log_path and result.log_path.is_file() for result in first)

    second = local_images.ensure_local_task_images(
        tmp_path, ["test-task"], log_dir=logs
    )
    assert [result.action for result in second] == ["reused", "reused"]
    assert len(builds) == 2

    (task_dir / "environment" / "fixture.txt").write_text(
        "changed\n", encoding="utf-8"
    )
    third = local_images.ensure_local_task_images(
        tmp_path, ["test-task"], log_dir=logs
    )
    assert [result.action for result in third] == ["built", "built"]
    assert len(builds) == 4


def test_build_failure_reports_log_tail(tmp_path: Path, monkeypatch):
    write_task(tmp_path)

    def fake_run(command, **kwargs):
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 1, "", "missing")
        kwargs["stdout"].write("registry connection failed\n")
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(local_images.shutil, "which", lambda name: "docker.exe")
    monkeypatch.setattr(local_images.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc:
        local_images.ensure_local_task_images(
            tmp_path, ["test-task"], log_dir=tmp_path / "logs"
        )
    assert "registry connection failed" in str(exc.value)
    assert "deepswe-test-base:local" in str(exc.value)


def test_dependency_image_digest_invalidates_verifier(tmp_path: Path, monkeypatch):
    write_task(tmp_path)
    labels = {
        "deepswe-test-base:local": "base-v1",
        "deepswe-test-verifier:local": "stale",
    }
    builds = []

    def fake_run(command, **kwargs):
        if command[1:3] == ["image", "inspect"]:
            image = command[3]
            output = json.dumps(
                [{"Config": {"Labels": {local_images.CONTEXT_DIGEST_LABEL: labels[image]}}}]
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        builds.append(command)
        image = command[command.index("--tag") + 1]
        label = command[command.index("--label") + 1].split("=", 1)[1]
        labels[image] = label
        kwargs["stdout"].write("built\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(local_images.shutil, "which", lambda name: "docker.exe")
    monkeypatch.setattr(local_images.subprocess, "run", fake_run)
    results = local_images.ensure_local_task_images(
        tmp_path, ["test-task"], log_dir=tmp_path / "logs"
    )
    assert [result.action for result in results] == ["built", "built"]
    assert [command[command.index("--tag") + 1] for command in builds] == [
        "deepswe-test-base:local",
        "deepswe-test-verifier:local",
    ]


def test_missing_local_dependency_fails_closed(tmp_path: Path, monkeypatch):
    task_dir = write_task(tmp_path)
    (task_dir / "task.toml").write_text(
        """
[verifier]
environment_mode = "separate"

[verifier.environment]
docker_image = "deepswe-test-verifier:local"

[environment]
docker_image = "public.ecr.aws/example/task:v1"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (task_dir / "tests" / "Dockerfile").write_text(
        "FROM missing-base:local\n", encoding="utf-8"
    )

    def fake_run(command, **kwargs):
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 1, "", "missing")
        raise AssertionError("dependency validation should fail before building")

    monkeypatch.setattr(local_images.shutil, "which", lambda name: "docker.exe")
    monkeypatch.setattr(local_images.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="depends on"):
        local_images.ensure_local_task_images(
            tmp_path, ["test-task"], log_dir=tmp_path / "logs"
        )
