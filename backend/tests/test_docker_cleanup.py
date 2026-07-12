"""docker_cleanup 单元测试：全部 subprocess 走 mock，不依赖本机 Docker 状态。"""
import json
from pathlib import Path
import pytest
from app import docker_cleanup as dc

class FakeDocker:
    """按 docker 子命令返回预置输出，并记录全部调用参数。"""
    def __init__(self, images=(), containers=(), networks=(), fail_refs=(), in_use_refs=()):
        self.images = list(images)
        self.containers = list(containers)  # (id, name)
        self.networks = list(networks)
        self.fail_refs = set(fail_refs)
        self.in_use_refs = set(in_use_refs)
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout=30, input_text=None):
        self.calls.append(list(args))
        command = args[0]
        if command == "image" and args[1] == "ls":
            return True, "\n".join(self.images) + "\n", ""
        if command == "ps" and "-a" in args and "--format" in args:
            return True, "\n".join(f"{cid}\t{name}" for cid, name in self.containers) + "\n", ""
        if command == "ps" and "-aq" in args:
            ref = args[-1].removeprefix("ancestor=")
            return True, ("abc123\n" if ref in self.in_use_refs else ""), ""
        if command == "network" and args[1] == "ls":
            return True, "\n".join(self.networks) + "\n", ""
        if command == "image" and args[1] == "rm":
            ref = args[2]
            if ref in self.fail_refs:
                return False, "", "cannot remove: backend exploded"
            if ref not in self.images:
                return False, "", f"Error: No such image: {ref}"
            self.images.remove(ref)
            return True, f"Untagged: {ref}\n", ""
        if command == "rm":
            return True, "", ""
        if command == "network" and args[1] == "rm":
            return True, "", ""
        return True, "", ""

@pytest.fixture
def jobs_root(tmp_path: Path) -> Path:
    return tmp_path / "jobs"

def trial_dir(jobs_root: Path, job: str, trial: str) -> Path:
    folder = jobs_root / job / trial
    folder.mkdir(parents=True, exist_ok=True)
    return folder

def patch_docker(monkeypatch, fake: FakeDocker):
    monkeypatch.setattr(dc, "_docker", fake)
    monkeypatch.setattr(dc.shutil, "which", lambda name: "C:/docker.exe" if name == "docker" else None)

def test_sanitize_matches_pier_rules():
    # 小写化 + 首字符非字母数字补 0 + 非法字符替换为 -
    assert dc.sanitize_compose_project_name("Actionlint-Action__AB2cd9Z") == "actionlint-action__ab2cd9z"
    assert dc.sanitize_compose_project_name("_leading") == "0_leading"
    assert dc.sanitize_compose_project_name("has.dots and spaces") == "has-dots-and-spaces"

def test_discover_projects_from_trial_dirs(jobs_root: Path, monkeypatch):
    monkeypatch.setattr(dc, "jobs_path", lambda: jobs_root)
    trial_dir(jobs_root, "ui-job", "actionlint-action-pinning-lint__Ab12Cd9")
    trial_dir(jobs_root, "ui-job", "not-a-trial")  # 无 __，不是 trial 目录
    assert dc.discover_job_projects("ui-job", jobs_root) == ["actionlint-action-pinning-lint__ab12cd9"]
    assert dc.discover_job_projects("missing-job", jobs_root) == []
    assert dc.discover_job_projects("..", jobs_root) == []  # 危险名直接拒绝

def test_candidate_images_only_accepts_service_suffix_whitelist(monkeypatch):
    project = "task__ab12cd9"
    fake = FakeDocker(images=[
        f"{project}-main:latest",
        f"{project}-pier-egress-proxy:latest",
        f"{project}__verifier__trial-main:latest",
        f"{project}-evil:latest",                 # 非白名单后缀
        f"{project}-main-extra:latest",           # 后缀后还有内容
        "public.ecr.aws/d3j8x8q7/swe-bench-202605:v1.1",  # 基础镜像
        "ubuntu:24.04",
        "someones-app-main:latest",               # 用户自己的 -main 镜像
    ])
    patch_docker(monkeypatch, fake)
    refs = dc.candidate_images([project])
    assert sorted(refs) == sorted([
        f"{project}-main:latest",
        f"{project}-pier-egress-proxy:latest",
        f"{project}__verifier__trial-main:latest",
    ])

def test_cleanup_removes_only_managed_and_never_uses_force(jobs_root, monkeypatch):
    trial = "actionlint-action-pinning-lint__Ab12Cd9"
    project = dc.sanitize_compose_project_name(trial)
    trial_dir(jobs_root, "ui-job", trial)
    fake = FakeDocker(
        images=[f"{project}-main:latest", f"{project}-pier-egress-proxy:latest", "ubuntu:24.04"],
        containers=[("c1", f"{project}-main-1"), ("c2", "unrelated-app-1")],
        networks=[f"{project}_default", "bridge"],
    )
    patch_docker(monkeypatch, fake)
    report = dc.cleanup_job_resources("ui-job", jobs_root, trigger="test")
    assert report["removed_containers"] == [f"{project}-main-1"]
    assert report["removed_networks"] == [f"{project}_default"]
    assert sorted(report["removed_images"]) == [f"{project}-main:latest", f"{project}-pier-egress-proxy:latest"]
    assert report["errors"] == []
    for call in fake.calls:
        if call[:2] == ["image", "rm"]:
            assert "-f" not in call and "--force" not in call
        assert "system" not in call[:1] or "prune" not in call  # 永不全局 prune
    assert "ubuntu:24.04" in fake.images
    # 审计文件落盘
    audit = json.loads((jobs_root / "ui-job.docker-cleanup.json").read_text(encoding="utf-8"))
    assert audit["trigger"] == "test"

def test_cleanup_skips_in_use_and_continues_after_failure(jobs_root, monkeypatch):
    trials = ["taskA__ab12cd9", "taskB__zz99yy8"]
    for trial in trials:
        trial_dir(jobs_root, "ui-job", trial)
    fake = FakeDocker(
        images=["taska__ab12cd9-main:latest", "taskb__zz99yy8-main:latest", "taskb__zz99yy8-pier-egress-proxy:latest"],
        fail_refs={"taska__ab12cd9-main:latest"},
        in_use_refs={"taskb__zz99yy8-main:latest"},
    )
    patch_docker(monkeypatch, fake)
    report = dc.cleanup_job_resources("ui-job", jobs_root, trigger="test")
    assert report["skipped_images"] == [{"name": "taskb__zz99yy8-main:latest", "reason": "in-use"}]
    assert report["removed_images"] == ["taskb__zz99yy8-pier-egress-proxy:latest"]  # 失败后继续
    assert len(report["errors"]) == 1 and "taska__ab12cd9-main" in report["errors"][0]

def test_cleanup_is_idempotent(jobs_root, monkeypatch):
    trial = "taskA__ab12cd9"
    trial_dir(jobs_root, "ui-job", trial)
    fake = FakeDocker(images=["taska__ab12cd9-main:latest"])
    patch_docker(monkeypatch, fake)
    first = dc.cleanup_job_resources("ui-job", jobs_root)
    second = dc.cleanup_job_resources("ui-job", jobs_root)
    assert first["removed_images"] == ["taska__ab12cd9-main:latest"]
    assert second["errors"] == []  # 资源已不存在视为成功

def test_cleanup_reports_structured_error_when_docker_missing(jobs_root, monkeypatch):
    trial_dir(jobs_root, "ui-job", "taskA__ab12cd9")
    monkeypatch.setattr(dc.shutil, "which", lambda name: None)
    report = dc.cleanup_job_resources("ui-job", jobs_root)
    assert report["available"] is False
    assert report["errors"] and report["removed_images"] == []

def test_storage_summary_parses_sizes(monkeypatch):
    def fake_docker(args, timeout=30, input_text=None):
        if args[:2] == ["system", "df"]:
            rows = [
                {"Type": "Images", "TotalCount": "40", "Size": "10.24GB", "Reclaimable": "2.337GB (22%)"},
                {"Type": "Build Cache", "TotalCount": "106", "Size": "7.217GB", "Reclaimable": "1.371MB"},
            ]
            return True, "\n".join(json.dumps(r) for r in rows), ""
        if args[:2] == ["image", "ls"]:
            return True, "", ""
        return True, "", ""
    monkeypatch.setattr(dc, "_docker", fake_docker)
    monkeypatch.setattr(dc, "_known_projects", lambda: set())
    summary = dc.docker_storage_summary()
    assert summary["available"] is True
    assert summary["images"]["size_bytes"] == 10_240_000_000
    assert summary["images"]["reclaimable_bytes"] == 2_337_000_000
    assert summary["build_cache"]["size_bytes"] == 7_217_000_000

def test_orphan_pattern_is_strict(monkeypatch):
    fake = FakeDocker(images=[
        "sometask__ab12cd9-main:latest",       # 符合 trial 模式 → 孤儿
        "ubuntu:24.04",                        # 公共镜像
        "myapp-main:latest",                   # 无 __uuid 段
        "task__short1-pier-egress-proxy:latest",  # uuid 段 6 位，不匹配
    ])
    patch_docker(monkeypatch, fake)
    monkeypatch.setattr(dc, "_known_projects", lambda: set())
    inventory = dc.managed_image_inventory()
    assert inventory["orphaned"] == ["sometask__ab12cd9-main:latest"]
    assert inventory["managed"] == []

def test_builder_prune_confirms_via_stdin_not_force(monkeypatch):
    calls = []
    def fake_docker(args, timeout=30, input_text=None):
        calls.append((list(args), input_text))
        return True, "Total reclaimed space: 1.2GB", ""
    monkeypatch.setattr(dc, "_docker", fake_docker)
    result = dc.prune_builder_cache(168)
    assert result["available"] is True and result["reclaimed"] == "1.2GB"
    args, input_text = calls[0]
    assert "--force" not in args and "-f" not in args
    assert input_text == "y\n"
    assert "--filter" in args and "until=168h" in args

def test_builder_prune_zero_retention_clears_all(monkeypatch):
    calls = []
    monkeypatch.setattr(dc, "_docker", lambda args, timeout=30, input_text=None: (calls.append(list(args)) or (True, "Total reclaimed space: 2GB", "")))
    result = dc.prune_builder_cache(0)
    assert result["available"] is True
    assert calls == [["builder", "prune", "--all"]]
