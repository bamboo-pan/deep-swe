from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


LOCAL_IMAGE_SUFFIX = ":local"
CONTEXT_DIGEST_LABEL = "io.deepswe.local-context-sha256"
TASK_LABEL = "io.deepswe.task"
ROLE_LABEL = "io.deepswe.image-role"
_HASH_IGNORED_PARTS = frozenset({".DS_Store", ".git", "__pycache__"})
_build_lock = threading.Lock()


@dataclass(frozen=True)
class LocalImageSpec:
    task: str
    role: str
    image: str
    context: Path
    timeout_sec: float


@dataclass(frozen=True)
class LocalImageResult:
    spec: LocalImageSpec
    digest: str
    action: str
    log_path: Path | None = None


def _is_local_image(value: object) -> bool:
    return isinstance(value, str) and value.strip().endswith(LOCAL_IMAGE_SUFFIX)


def _timeout(value: object, default: float = 600.0) -> float:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return default


def discover_local_image_specs(
    tasks_dir: Path, tasks: Iterable[str] | None = None
) -> list[LocalImageSpec]:
    selected = list(tasks) if tasks is not None else sorted(
        path.name for path in tasks_dir.iterdir() if path.is_dir()
    )
    specs: list[LocalImageSpec] = []
    for task in selected:
        task_dir = tasks_dir / task
        config_path = task_dir / "task.toml"
        if not config_path.is_file():
            continue
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeError(f"Cannot read local image config for task {task}: {exc}") from exc

        environment = config.get("environment") or {}
        image = environment.get("docker_image")
        if _is_local_image(image):
            specs.append(
                LocalImageSpec(
                    task=task,
                    role="environment",
                    image=image.strip(),
                    context=task_dir / "environment",
                    timeout_sec=_timeout(environment.get("build_timeout_sec")),
                )
            )

        verifier = config.get("verifier") or {}
        verifier_environment = verifier.get("environment") or {}
        verifier_image = verifier_environment.get("docker_image")
        verifier_mode = verifier.get("environment_mode")
        if (
            verifier_image is None
            and verifier_mode == "separate"
            and _is_local_image(image)
            and not verifier_environment
        ):
            raise RuntimeError(
                f"Task {task} uses a :local environment image with a separate "
                "verifier, but has no distinct [verifier.environment].docker_image"
            )
        if _is_local_image(verifier_image):
            specs.append(
                LocalImageSpec(
                    task=task,
                    role="verifier",
                    image=verifier_image.strip(),
                    context=task_dir / "tests",
                    timeout_sec=_timeout(
                        verifier_environment.get("build_timeout_sec")
                    ),
                )
            )
    return specs


def build_context_digest(
    context: Path, *, dependency_digests: Iterable[str] = ()
) -> str:
    if not context.is_dir():
        raise RuntimeError(f"Local Docker build context does not exist: {context}")
    dockerfile = context / "Dockerfile"
    if not dockerfile.is_file():
        raise RuntimeError(f"Local Docker build context has no Dockerfile: {context}")

    digest = hashlib.sha256()
    entries = sorted(
        context.rglob("*"), key=lambda path: path.relative_to(context).as_posix()
    )
    for path in entries:
        relative = path.relative_to(context)
        if _HASH_IGNORED_PARTS & set(relative.parts):
            continue
        encoded_path = relative.as_posix().encode("utf-8")
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        if path.is_symlink():
            digest.update(b"L")
            target = os.readlink(path).encode("utf-8")
            digest.update(len(target).to_bytes(4, "big"))
            digest.update(target)
        elif path.is_file():
            digest.update(b"F")
            data = path.read_bytes()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        elif path.is_dir():
            digest.update(b"D")
    for dependency in sorted(dependency_digests):
        encoded = dependency.encode("utf-8")
        digest.update(b"I")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _local_from_images(dockerfile: Path) -> set[str]:
    try:
        text = dockerfile.read_text(encoding="utf-8")
    except OSError:
        return set()
    references = re.findall(r"(?im)^\s*FROM\s+([^\s]+)", text)
    return {reference for reference in references if _is_local_image(reference)}


def _inspect_image_digest(docker: str, image: str) -> str | None:
    try:
        result = subprocess.run(
            [docker, "image", "inspect", image],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
        labels = ((payload[0].get("Config") or {}).get("Labels") or {})
    except (IndexError, TypeError, ValueError, AttributeError):
        return None
    value = labels.get(CONTEXT_DIGEST_LABEL)
    return value if isinstance(value, str) else None


def _log_name(spec: LocalImageSpec) -> str:
    image = re.sub(r"[^A-Za-z0-9_.-]+", "-", spec.image).strip(".-")
    return f"{image}-{spec.role}.log"


def _tail(path: Path, limit: int = 12_000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace").strip()


def _build_image(
    docker: str,
    spec: LocalImageSpec,
    digest: str,
    log_dir: Path,
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / _log_name(spec)
    command = [
        docker,
        "build",
        "--label",
        f"{CONTEXT_DIGEST_LABEL}={digest}",
        "--label",
        f"{TASK_LABEL}={spec.task}",
        "--label",
        f"{ROLE_LABEL}={spec.role}",
        "--tag",
        spec.image,
        str(spec.context.resolve()),
    ]
    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as log:
            result = subprocess.run(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=spec.timeout_sec,
            )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Timed out building local image {spec.image} after "
            f"{spec.timeout_sec:g}s; see {log_path}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Cannot start Docker build for {spec.image}: {exc}") from exc
    if result.returncode != 0:
        detail = _tail(log_path)
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"Failed to build local image {spec.image}; see {log_path}{suffix}"
        )
    if _inspect_image_digest(docker, spec.image) != digest:
        raise RuntimeError(
            f"Built local image {spec.image} is missing the expected context label; "
            f"see {log_path}"
        )
    return log_path


def ensure_local_task_images(
    tasks_dir: Path,
    tasks: Iterable[str] | None = None,
    *,
    log_dir: Path | None = None,
    force: bool = False,
) -> list[LocalImageResult]:
    specs = discover_local_image_specs(tasks_dir, tasks)
    if not specs:
        return []
    docker = shutil.which("docker") or "docker"
    logs = log_dir or tasks_dir.parent / "data" / "local-image-builds"
    results: list[LocalImageResult] = []
    configured: dict[str, str] = {}
    image_digests: dict[str, str] = {}

    with _build_lock:
        for spec in specs:
            dependency_digests = []
            for image in sorted(_local_from_images(spec.context / "Dockerfile")):
                dependency = image_digests.get(image)
                if dependency is None:
                    dependency = _inspect_image_digest(docker, image)
                if not dependency:
                    raise RuntimeError(
                        f"Local image {spec.image} depends on {image}, but that image "
                        "is missing or has no managed context checksum; configure a "
                        "managed local build context for the dependency"
                    )
                dependency_digests.append(f"{image}={dependency}")
            digest = build_context_digest(
                spec.context, dependency_digests=dependency_digests
            )
            previous = configured.get(spec.image)
            if previous is not None and previous != digest:
                raise RuntimeError(
                    f"Local image tag {spec.image} is assigned to multiple build contexts"
                )
            configured[spec.image] = digest
            if not force and _inspect_image_digest(docker, spec.image) == digest:
                image_digests[spec.image] = digest
                results.append(LocalImageResult(spec, digest, "reused"))
                continue
            log_path = _build_image(docker, spec, digest, logs)
            image_digests[spec.image] = digest
            results.append(LocalImageResult(spec, digest, "built", log_path))
    return results


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Build or reuse task Docker images whose tags end in :local."
    )
    parser.add_argument("--tasks-dir", type=Path, default=root / "tasks")
    parser.add_argument("--task", action="append", dest="tasks")
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        results = ensure_local_task_images(
            args.tasks_dir,
            args.tasks,
            log_dir=args.log_dir,
            force=args.force,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not results:
        print("No :local task images are configured.")
        return 0
    for result in results:
        detail = f" log={result.log_path}" if result.log_path else ""
        print(
            f"{result.action}: {result.spec.image} "
            f"({result.spec.task}/{result.spec.role}, {result.digest[:12]}){detail}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
