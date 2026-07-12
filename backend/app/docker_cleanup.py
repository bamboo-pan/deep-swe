"""Docker 资源定向清理。

只处理本工具经 Pier 创建的 Trial 级资源：Compose 容器、网络与默认命名的镜像
（<project>-main / <project>-pier-egress-proxy 及 verifier 子项目变体）。
任务声明的基础镜像、ubuntu 等公共镜像和 BuildKit 缓存不进入自动删除候选；
缓存只按保留期手动清理。所有操作幂等，Docker 不可用时返回结构化错误，
不影响已产生的评测结果。
"""
import json
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlalchemy import select
from .database import SessionLocal
from .models import ACTIVE_STATES, Run
from .preferences import jobs_path
from .security import is_safe_job_name

# 串行化清理，防止三 Agent 并行运行结束时互相干扰
_cleanup_lock = threading.Lock()

# Compose 默认镜像名 = <project><服务后缀>；verifier separate mode 会派生
# <project>__verifier__trial 子项目。只允许这四种后缀，禁止宽泛的 *-main 正则。
SERVICE_SUFFIXES = ("-main", "-pier-egress-proxy")
VERIFIER_INFIX = "__verifier__trial"
IMAGE_SUFFIXES = tuple(
    [f"{VERIFIER_INFIX}{suffix}" for suffix in SERVICE_SUFFIXES] + list(SERVICE_SUFFIXES)
)
# 孤儿识别：trial 目录名为 {task[:32]}__{ShortUUID(7)}，小写化后 uuid 段是 [a-z0-9]{7}
_ORPHAN_IMAGE_RE = re.compile(
    r"^[a-z0-9][a-z0-9_.-]*__[a-z0-9]{7}(?:__verifier__trial)?(?:-main|-pier-egress-proxy)$"
)

@dataclass
class DockerCleanupPolicy:
    remove_containers: bool = True
    remove_networks: bool = True
    remove_trial_images: bool = True
    prune_build_cache: bool = False
    cache_retention_hours: int = 168

def sanitize_compose_project_name(name: str) -> str:
    """复刻 pier._sanitize_docker_compose_project_name：trial 目录名 → Compose project。"""
    value = name.lower()
    if not re.match(r"^[a-z0-9]", value):
        value = "0" + value
    return re.sub(r"[^a-z0-9_-]", "-", value)

def _docker(args: list[str], timeout: int = 30, input_text: str | None = None) -> tuple[bool, str, str]:
    executable = shutil.which("docker")
    if not executable:
        return False, "", "未找到 docker 命令"
    try:
        result = subprocess.run([executable, *args], capture_output=True, text=True, timeout=timeout, input=input_text)
        return result.returncode == 0, result.stdout or "", result.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "", str(exc)

def docker_available() -> tuple[bool, str]:
    ok, out, err = _docker(["info", "--format", "{{.ServerVersion}}"], timeout=15)
    return ok, (out.strip() if ok else err.strip() or "Docker daemon 不可用")

def discover_job_projects(job_name: str, jobs_root: Path | None = None) -> list[str]:
    """从 Job artifacts 的 Trial 子目录推导 Compose project 名（清理候选的唯一可信来源）。"""
    if not is_safe_job_name(job_name):
        return []
    root = (jobs_root or jobs_path()) / job_name
    if not root.is_dir():
        return []
    return sorted({sanitize_compose_project_name(p.name) for p in root.iterdir() if p.is_dir() and "__" in p.name})

def _split_managed(repository: str, projects: set[str]) -> str | None:
    for project in projects:
        if repository.startswith(project) and repository[len(project):] in IMAGE_SUFFIXES:
            return project
    return None

def _list_images() -> list[str]:
    ok, out, _ = _docker(["image", "ls", "--format", "{{.Repository}}:{{.Tag}}"])
    if not ok:
        return []
    return [line.strip() for line in out.splitlines() if line.strip() and not line.startswith("<none>")]

def candidate_images(projects: list[str]) -> list[str]:
    """现存镜像中属于给定 project 的条目（前缀 + 白名单后缀，永不接受外部传入的镜像名）。"""
    wanted = set(projects)
    result = []
    for ref in _list_images():
        repository, _, tag = ref.rpartition(":")
        if repository and _split_managed(repository, wanted):
            result.append(f"{repository}:{tag}")
    return result

def _matching_containers(projects: list[str]) -> list[tuple[str, str]]:
    ok, out, _ = _docker(["ps", "-a", "--format", "{{.ID}}\t{{.Names}}"])
    if not ok:
        return []
    rows = []
    for line in out.splitlines():
        container_id, _, name = line.partition("\t")
        lowered = name.strip().lower()
        if any(lowered.startswith(project + "-") or lowered.startswith(project + "_") for project in projects):
            rows.append((container_id.strip(), name.strip()))
    return rows

def _matching_networks(projects: list[str]) -> list[str]:
    ok, out, _ = _docker(["network", "ls", "--format", "{{.Name}}"])
    if not ok:
        return []
    return [name.strip() for name in out.splitlines() if any(name.strip().lower().startswith(project + "_") for project in projects)]

def _image_in_use(ref: str) -> bool:
    ok, out, _ = _docker(["ps", "-aq", "--filter", f"ancestor={ref}"])
    return ok and bool(out.strip())

def _is_idempotent_miss(stderr: str) -> bool:
    lowered = stderr.lower()
    return "no such" in lowered or "not found" in lowered

def preview_job_cleanup(job_name: str, jobs_root: Path | None = None, projects: list[str] | None = None) -> dict:
    projects = projects if projects is not None else discover_job_projects(job_name, jobs_root)
    return {
        "job_name": job_name,
        "projects": projects,
        "containers": [name for _, name in _matching_containers(projects)] if projects else [],
        "networks": _matching_networks(projects) if projects else [],
        "images": candidate_images(projects) if projects else [],
    }

def cleanup_job_resources(job_name: str, jobs_root: Path | None = None, policy: DockerCleanupPolicy | None = None, trigger: str = "manual", projects: list[str] | None = None) -> dict:
    """幂等清理一个 Job 的 Trial 级 Docker 资源，返回结构化审计结果。"""
    policy = policy or DockerCleanupPolicy()
    jobs_root = jobs_root or jobs_path()
    report = {
        "job_name": job_name, "trigger": trigger, "available": True,
        "started_at": datetime.now(UTC).isoformat(),
        "removed_containers": [], "removed_networks": [],
        "removed_images": [], "skipped_images": [], "errors": [],
    }
    projects = projects if projects is not None else discover_job_projects(job_name, jobs_root)
    report["projects"] = projects
    if not projects:
        report["finished_at"] = datetime.now(UTC).isoformat()
        return report
    if not shutil.which("docker"):
        report["available"] = False
        report["errors"].append("未找到 docker 命令")
        report["finished_at"] = datetime.now(UTC).isoformat()
        return report
    with _cleanup_lock:
        if policy.remove_containers:
            for container_id, name in _matching_containers(projects):
                ok, _, err = _docker(["rm", "-f", container_id], timeout=60)
                if ok or _is_idempotent_miss(err):
                    report["removed_containers"].append(name)
                else:
                    report["errors"].append(f"容器 {name}: {err.strip()[:200]}")
        if policy.remove_networks:
            for network in _matching_networks(projects):
                ok, _, err = _docker(["network", "rm", network], timeout=30)
                if ok or _is_idempotent_miss(err):
                    report["removed_networks"].append(network)
                else:
                    report["errors"].append(f"网络 {network}: {err.strip()[:200]}")
        if policy.remove_trial_images:
            for ref in candidate_images(projects):
                if _image_in_use(ref):
                    report["skipped_images"].append({"name": ref, "reason": "in-use"})
                    continue
                ok, _, err = _docker(["image", "rm", ref], timeout=120)
                if ok or _is_idempotent_miss(err):
                    report["removed_images"].append(ref)
                elif "image is being used" in err.lower() or "conflict" in err.lower():
                    report["skipped_images"].append({"name": ref, "reason": "in-use"})
                else:
                    report["errors"].append(f"镜像 {ref}: {err.strip()[:200]}")
    report["finished_at"] = datetime.now(UTC).isoformat()
    _write_audit(jobs_root, job_name, report)
    return report

def _write_audit(jobs_root: Path, job_name: str, report: dict) -> None:
    if not is_safe_job_name(job_name):
        return
    try:
        jobs_root.mkdir(parents=True, exist_ok=True)
        (jobs_root / f"{job_name}.docker-cleanup.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

_SIZE_UNITS = {"b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12}

def _parse_size(value: object) -> int | None:
    """docker system df 的人类可读值（如 "2.337GB (22%)"）→ 字节。"""
    if not isinstance(value, str) or not value.strip():
        return None
    match = re.match(r"^\s*([\d.]+)\s*([kMGT]?B)", value.strip(), re.IGNORECASE)
    if not match:
        return None
    try:
        return int(float(match.group(1)) * _SIZE_UNITS[match.group(2).lower()])
    except (ValueError, KeyError):
        return None

def _known_projects() -> set[str]:
    """数据库与 Jobs 目录中所有可归属的 Trial project。"""
    projects: set[str] = set()
    roots: set[Path] = {jobs_path()}
    try:
        with SessionLocal() as db:
            rows = db.scalars(select(Run)).all()
        for run in rows:
            root = Path(run.jobs_dir) if run.jobs_dir else jobs_path()
            roots.add(root)
            projects.update(discover_job_projects(run.job_name, root))
    except Exception:
        pass
    for root in roots:
        if not root.is_dir():
            continue
        for job_dir in root.iterdir():
            if job_dir.is_dir():
                projects.update(discover_job_projects(job_dir.name, root))
    return projects

def managed_image_inventory() -> dict:
    """现存镜像里：可归属到已知 Job 的 managed 列表 + 无法归属的孤儿列表。"""
    known = _known_projects()
    managed, orphaned = [], []
    for ref in _list_images():
        repository, _, _tag = ref.rpartition(":")
        if not repository:
            continue
        if _split_managed(repository, known):
            managed.append(ref)
        elif _ORPHAN_IMAGE_RE.fullmatch(repository):
            orphaned.append(ref)
    return {"managed": managed, "orphaned": orphaned}

def remove_orphaned_images() -> dict:
    """删除无法归属到任何已知 Job 的本工具模式镜像（先 preview 后调用）。"""
    removed, skipped, errors = [], [], []
    with _cleanup_lock:
        for ref in managed_image_inventory()["orphaned"]:
            if _image_in_use(ref):
                skipped.append({"name": ref, "reason": "in-use"})
                continue
            ok, _, err = _docker(["image", "rm", ref], timeout=120)
            if ok or _is_idempotent_miss(err):
                removed.append(ref)
            else:
                errors.append(f"镜像 {ref}: {err.strip()[:200]}")
    return {"scope": "orphaned", "removed_images": removed, "skipped_images": skipped, "errors": errors}

def active_run_count() -> int:
    try:
        with SessionLocal() as db:
            return len(db.scalars(select(Run.id).where(Run.status.in_(ACTIVE_STATES))).all())
    except Exception:
        return 0

def docker_storage_summary() -> dict:
    ok, out, err = _docker(["system", "df", "--format", "{{json .}}"], timeout=60)
    if not ok:
        return {"available": False, "error": err.strip() or "Docker daemon 不可用", "active_runs": active_run_count()}
    images_row, cache_row = {}, {}
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("Type") == "Images":
            images_row = row
        elif row.get("Type") == "Build Cache":
            cache_row = row
    inventory = managed_image_inventory()
    return {
        "available": True,
        "images": {
            "count": int(images_row.get("TotalCount") or 0),
            "size_bytes": _parse_size(images_row.get("Size")),
            "reclaimable_bytes": _parse_size(images_row.get("Reclaimable")),
            "managed_count": len(inventory["managed"]),
            "orphaned_count": len(inventory["orphaned"]),
        },
        "build_cache": {
            "count": int(cache_row.get("TotalCount") or 0),
            "size_bytes": _parse_size(cache_row.get("Size")),
            "reclaimable_bytes": _parse_size(cache_row.get("Reclaimable")),
        },
        "active_runs": active_run_count(),
    }

def image_unique_sizes() -> dict[str, int]:
    """docker system df -v 提供每镜像独占空间；预览「预计释放」只允许用这个，不许累加 Size。"""
    ok, out, _ = _docker(["system", "df", "-v", "--format", "{{json .}}"], timeout=120)
    if not ok:
        return {}
    sizes: dict[str, int] = {}
    for line in out.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        for row in data.get("Images") or []:
            repository, tag = row.get("Repository"), row.get("Tag")
            unique = _parse_size(row.get("UniqueSize"))
            if repository and tag and unique is not None:
                sizes[f"{repository}:{tag}"] = unique
    return sizes

def expired_jobs(retention_hours: int) -> list[dict]:
    """终态且结束时间超过保留期的 Run（含各自的 jobs 根目录）。"""
    cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
    result = []
    with SessionLocal() as db:
        rows = db.scalars(select(Run).where(Run.status.notin_(ACTIVE_STATES))).all()
    for run in rows:
        finished = run.finished_at
        if finished is None:
            continue
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=UTC)
        if finished < cutoff:
            result.append({"job_name": run.job_name, "jobs_root": Path(run.jobs_dir) if run.jobs_dir else jobs_path()})
    return result

def preview_builder_cleanup(retention_hours: int) -> dict:
    summary = docker_storage_summary()
    if not summary.get("available"):
        return {"available": False, "error": summary.get("error")}
    return {
        "available": True,
        "retention_hours": retention_hours,
        "build_cache": summary["build_cache"],
        "note": f"将清理超过 {retention_hours} 小时未使用的构建缓存；实际释放量以 Docker 返回为准",
    }

def prune_builder_cache(retention_hours: int) -> dict:
    """带保留期的 BuildKit 缓存清理。经 stdin 确认，命令行不使用 --force。"""
    command = ["builder", "prune", "--all"]
    if retention_hours > 0:
        command += ["--filter", f"until={retention_hours}h"]
    ok, out, err = _docker(
        command,
        timeout=600, input_text="y\n")
    if not ok:
        return {"available": False, "error": err.strip() or "builder prune 失败"}
    reclaimed = None
    for line in out.splitlines():
        if "reclaimed space" in line.lower():
            reclaimed = line.split(":", 1)[-1].strip()
    return {"available": True, "reclaimed": reclaimed, "retention_hours": retention_hours}
