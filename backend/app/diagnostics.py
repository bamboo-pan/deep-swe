import json
import shutil
import subprocess
from pathlib import Path
import httpx
import psutil
from .docker_cleanup import docker_storage_summary
from .preferences import credential_path, get_preferences, jobs_path
from .security import mask_token, read_credential

COMMAND_CHECK_TIMEOUT_SEC = 30

def _container_agent_versions() -> list[dict]:
    latest: dict[str, str] = {}
    try:
        jobs_dir = jobs_path()
        results = sorted(jobs_dir.glob("*/**/result.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in results:
            data = json.loads(path.read_text(encoding="utf-8"))
            info = data.get("agent_info") or {}
            name = info.get("name") or data.get("agent_name")
            version = info.get("version")
            if name and version and name not in latest:
                latest[name] = str(version)
            if len(latest) >= 3:
                break
    except Exception:
        pass
    aliases = {"mini-swe-agent": "mini-swe-agent", "codex": "codex", "claude-code": "claude-code"}
    return [{"name": f"{label} (容器)", "status": "ok", "message": latest.get(key, "latest · 首次运行完成后显示实际版本")} for label, key in aliases.items()]

def command_check(name: str, args: list[str]) -> dict:
    executable = shutil.which(name)
    if not executable:
        return {"name": name, "status": "error", "message": "未找到命令"}
    try:
        result = subprocess.run([executable, *args], capture_output=True, text=True, timeout=COMMAND_CHECK_TIMEOUT_SEC)
        text = (result.stdout or result.stderr).strip().splitlines()
        return {"name": name, "status": "ok" if result.returncode == 0 else "error", "message": text[0] if text else "可用"}
    except subprocess.TimeoutExpired:
        return {"name": name, "status": "warning", "message": f"版本检查超过 {COMMAND_CHECK_TIMEOUT_SEC} 秒；命令可能仍可用"}
    except Exception as exc:
        return {"name": name, "status": "error", "message": str(exc)}

def credential_check() -> tuple[dict, str | None]:
    try:
        path = credential_path()
        cred = read_credential(path)
        return {"name": "credential", "status": "ok", "message": f"{path.name} · {mask_token(cred.token)}", "url": cred.url, "fingerprint": cred.fingerprint}, cred.url
    except Exception as exc:
        return {"name": "credential", "status": "error", "message": str(exc)}, None

def _format_bytes(value: int | None) -> str:
    if value is None:
        return "未知"
    if value >= 10**9:
        return f"{value / 10**9:.2f} GB"
    if value >= 10**6:
        return f"{value / 10**6:.1f} MB"
    return f"{value / 10**3:.0f} KB"

def _docker_storage_checks() -> list[dict]:
    preferences = get_preferences()
    summary = docker_storage_summary()
    if not summary.get("available"):
        return [{"name": "docker-images", "status": "warning", "message": summary.get("error", "Docker 不可用，跳过存储检查")}]
    images, cache = summary["images"], summary["build_cache"]
    warning_bytes = float(preferences.get("docker_cache_warning_gb", 20)) * 10**9
    cache_size = cache.get("size_bytes") or 0
    orphaned = images.get("orphaned_count", 0)
    checks = [
        {"name": "docker-images", "status": "ok",
         "message": f"{images['count']} 个镜像 · 实际占用 {_format_bytes(images['size_bytes'])} · 可回收 {_format_bytes(images['reclaimable_bytes'])} · 本工具 {images['managed_count']} 个"},
        {"name": "docker-build-cache", "status": "warning" if cache_size > warning_bytes else "ok",
         "message": f"构建缓存 {_format_bytes(cache.get('size_bytes'))}（可在设置页按保留期清理）"},
        {"name": "docker-orphans", "status": "warning" if orphaned else "ok",
         "message": f"{orphaned} 个无法归属的 Trial 镜像" if orphaned else "无孤儿 Trial 镜像"},
        {"name": "docker-cleanup-policy",
         "status": "ok" if preferences.get("docker_cleanup_after_run", True) else "warning",
         "message": "运行结束自动清理 Trial 镜像已启用" if preferences.get("docker_cleanup_after_run", True) else "运行结束自动清理已关闭，Trial 镜像会持续累积"},
    ]
    return checks

def run_checks() -> dict:
    credential, url = credential_check()
    checks = [command_check("docker", ["info", "--format", "{{.ServerVersion}}"]), command_check("pier", ["--version"]), *_container_agent_versions(), credential]
    if url:
        try:
            response = httpx.get(url.rstrip("/") + "/models", timeout=3)
            checks.append({"name": "model-api", "status": "ok" if response.status_code < 500 else "error", "message": f"{url} · 可连接 · HTTP {response.status_code}（探测未携带 Token）", "url": url})
        except Exception as exc:
            checks.append({"name": "model-api", "status": "error", "message": str(exc), "url": url})
    disk = psutil.disk_usage(str(Path.cwd()))
    memory = psutil.virtual_memory()
    checks += [{"name": "disk", "status": "ok" if disk.free > 20*1024**3 else "warning", "message": f"可用 {disk.free/1024**3:.1f} GB"}, {"name": "memory", "status": "ok" if memory.available > 12*1024**3 else "warning", "message": f"可用 {memory.available/1024**3:.1f} GB"}]
    try:
        uv = shutil.which("uv")
        tool_root = Path(subprocess.run([uv, "tool", "dir"], capture_output=True, text=True, timeout=5, check=True).stdout.strip()) if uv else None
        candidates = list(tool_root.glob("*/Lib/site-packages/pier/environments/docker/docker.py")) if tool_root and tool_root.exists() else []
        if not candidates:
            raise FileNotFoundError("未找到 Pier Python 安装目录")
        source = candidates[0].read_text(encoding="utf-8")
        patched = "process_env_overrides" in source
        checks.append({"name":"pier-secret-env","status":"ok" if patched else "error","message":"Token 不进入 docker 命令行" if patched else "Pier 需应用 secret env 补丁"})
        claude_adapter = candidates[0].parents[2] / "agents" / "installed" / "claude_code.py"
        # Windows 默认 GBK 读 UTF-8 会话文件会让 trajectory 转换失败，token/费用统计随之丢失
        utf8_ok = 'open(session_file, "r", encoding="utf-8"' in claude_adapter.read_text(encoding="utf-8")
        checks.append({"name":"pier-claude-utf8","status":"ok" if utf8_ok else "error","message":"Claude 会话按 UTF-8 解析" if utf8_ok else "Pier 需应用 claude_code UTF-8 补丁，否则拿不到 token 统计"})
    except Exception as exc:
        checks.append({"name":"pier-secret-env","status":"error","message":str(exc)})
    checks += _docker_storage_checks()
    # Docker 空间告警只降级提示，不算不可运行
    return {"checks": checks, "ready": all(c["status"] != "error" for c in checks)}
