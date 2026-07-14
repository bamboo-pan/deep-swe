import asyncio
import csv
import io
import json
import os
import shutil
import time
import tomllib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, select
from .config import settings
from .compare_analysis import (
    CompareAnalysisInputError, CompareAnalysisProviderError, analyze_compare_items,
)
from .database import SessionLocal, init_db
from .diagnostics import run_checks
from .docker_cleanup import (
    DockerCleanupPolicy, cleanup_job_resources, docker_storage_summary, expired_jobs,
    image_unique_sizes, managed_image_inventory, preview_builder_cleanup, preview_job_cleanup,
    prune_builder_cache, remove_orphaned_images, sanitize_compose_project_name,
)
from .models import ACTIVE_STATES, TERMINAL_STATES, Baseline, Run, Setting
from .official_stats import load_official_stats, official_stats_meta, sync_official_stats
from .preferences import KEYS as PREFERENCE_KEYS, get_preferences, update_preferences
from .provider_catalog import EFFORT_ORDER, get_provider_catalog
from .results import (
    compare_runs, deleted_trial_entries, jobs_root_for, list_details, parse_timestamp,
    run_detail as parsed_run_detail, task_catalog, trial_detail, trial_folder, trial_log,
)
from .runner import (
    cancel_run, create_run, get_run, list_runs, reap_orphaned_runs,
    retry_job_names, retry_trials, run_log, shutdown_processes,
)
from .schemas import (
    MAX_CONCURRENCY_PER_RUN, BaselineDraft, CompareAnalysisRequest, CompareRequest,
    DockerCleanupRequest, RestorePayload, RetryTrialsDraft, RunDraft, SettingsUpdate,
    concurrency_advice, total_parallel_tasks,
)
from .security import is_safe_job_name, read_credential
from .task_suite import DEFAULT_TASKS, DEFAULT_TASK_SUITE_NAME

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if os.environ.get("DEEPSWE_SKIP_STARTUP_REAP") != "1":
        reap_orphaned_runs()
    yield
    shutdown_processes()

app = FastAPI(title="DeepSWE Regression UI", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])

@app.get("/api/health")
def health(): return {"status": "ok", "version": app.version}

def _provider_bootstrap(preferences: dict, *, force_refresh: bool = False) -> dict:
    catalog = get_provider_catalog(
        preferences["default_model"],
        preferences["default_effort"],
        force_refresh=force_refresh,
    )
    model_entries = catalog["models"]
    model_ids = [entry["id"] for entry in model_entries]
    default_model = preferences["default_model"] if preferences["default_model"] in model_ids else model_ids[0]
    model_efforts = {entry["id"]: entry["reasoning_efforts"] for entry in model_entries}
    model_defaults = {
        entry["id"]: entry["default_reasoning_effort"] or entry["reasoning_efforts"][0]
        for entry in model_entries
    }
    selected_efforts = model_efforts[default_model]
    preferred_effort = preferences["default_effort"]
    default_effort = preferred_effort if preferred_effort in selected_efforts else model_defaults[default_model]
    available_efforts = {
        effort
        for efforts in model_efforts.values()
        for effort in efforts
    }
    return {
        "models": model_ids,
        "model_efforts": model_efforts,
        "model_defaults": model_defaults,
        "efforts": [effort for effort in EFFORT_ORDER if effort in available_efforts],
        "default_model": default_model,
        "default_effort": default_effort,
        "provider_catalog": {
            "source": catalog["source"],
            "models_authoritative": catalog["models_authoritative"],
            "error": catalog.get("error"),
        },
    }

def _provider_selection_error(draft: RunDraft) -> str | None:
    preferences = get_preferences()
    catalog = get_provider_catalog(preferences["default_model"], preferences["default_effort"])
    if not catalog["models_authoritative"]:
        return None
    selected = next((entry for entry in catalog["models"] if entry["id"] == draft.model), None)
    if not selected:
        return f"模型 {draft.model} 当前不在 provider 的可用模型列表中"
    if selected["reasoning_efforts_known"] and draft.reasoning_effort not in selected["reasoning_efforts"]:
        supported = ", ".join(selected["reasoning_efforts"])
        return f"模型 {draft.model} 不支持 reasoning effort {draft.reasoning_effort}；支持：{supported}"
    return None

@app.get("/api/bootstrap")
def bootstrap():
    task_folders = sorted((p for p in settings.tasks_dir.iterdir() if p.is_dir()), key=lambda p: p.name) if settings.tasks_dir.exists() else []
    available = {p.name for p in task_folders}
    task_numbers = {folder.name: index for index, folder in enumerate(task_folders, 1)}
    preferences = get_preferences()
    credential_root = Path(preferences["credential_file"]).parent
    credential_options = []
    for path in sorted(credential_root.glob("*.txt")) if credential_root.exists() else []:
        try:
            read_credential(path); credential_options.append(str(path))
        except Exception:
            pass
    if preferences["credential_file"] not in credential_options: credential_options.insert(0, preferences["credential_file"])
    job_options = list(dict.fromkeys([preferences["jobs_dir"], str(settings.jobs_dir)]))
    task_choices = []
    official_stats = load_official_stats()
    for index, task in enumerate(DEFAULT_TASKS, 1):
        metadata = {}
        path = settings.tasks_dir / task / "task.toml"
        try:
            if path.exists(): metadata = tomllib.loads(path.read_text(encoding="utf-8")).get("metadata", {})
        except (OSError, tomllib.TOMLDecodeError):
            pass
        stats = official_stats.get(task) or {}
        task_number = task_numbers.get(task)
        task_choices.append({"id": task, "task_number": task_number, "suite_id": f"TASK-{task_number:03d}" if task_number else f"T{index:02d}", "external_id": metadata.get("ext_id"), "title": metadata.get("display_title") or task, "language": metadata.get("language"), "category": metadata.get("category"), "available": task in available, "official_pass_rate": stats.get("pass_rate"), "official_avg_duration_seconds": stats.get("avg_duration_seconds")})
    provider = _provider_bootstrap(preferences, force_refresh=True)
    return {
        "defaults": {
            "agent": preferences["default_agent"],
            "model": provider["default_model"],
            "reasoning_effort": provider["default_effort"],
            "concurrency": preferences["default_concurrency"],
        },
        "agents": ["mini-swe-agent", "codex", "claude-code"],
        "models": provider["models"],
        "model_efforts": provider["model_efforts"],
        "model_defaults": provider["model_defaults"],
        "efforts": provider["efforts"],
        "provider_catalog": provider["provider_catalog"],
        "service_tiers": ["standard", "batch", "priority"],
        "setting_options": {
            "credential_files": credential_options,
            "jobs_dirs": job_options,
            "concurrency": list(range(1, MAX_CONCURRENCY_PER_RUN + 1)),
        },
        "task_suite": {"name": DEFAULT_TASK_SUITE_NAME, "tasks": task_choices},
    }

@app.get("/api/diagnostics")
def diagnostics(): return run_checks()

@app.get("/api/concurrency/{value}")
def concurrency(value: int): return concurrency_advice(value)

@app.post("/api/runs/validate")
def validate_run(draft: RunDraft):
    missing = [task for task in draft.tasks if not (settings.tasks_dir / task).is_dir()]
    parallel_tasks = total_parallel_tasks(draft)
    advice = concurrency_advice(parallel_tasks)
    if missing: raise HTTPException(422, detail={"missing_tasks": missing})
    if selection_error := _provider_selection_error(draft):
        raise HTTPException(422, detail=selection_error)
    return {"valid": advice["level"] != "blocked", "trial_count": len(draft.tasks) * draft.attempts_per_task, "total_parallel_tasks": parallel_tasks, "concurrency": advice}

@app.post("/api/runs")
def start_run(draft: RunDraft):
    missing=[task for task in draft.tasks if not (settings.tasks_dir/task).is_dir()]
    if missing: raise HTTPException(422,detail={"missing_tasks":missing})
    if selection_error := _provider_selection_error(draft):
        raise HTTPException(422, detail=selection_error)
    try: return create_run(draft)
    except ValueError as exc: raise HTTPException(422,detail=str(exc))

@app.get("/api/runs")
def runs(): return list_runs()

@app.get("/api/runs/{run_id}")
def run_detail(run_id:int):
    with SessionLocal() as db: row=db.get(Run,run_id)
    if not row: raise HTTPException(404,"run not found")
    return parsed_run_detail(row, include_patches=False)

@app.get("/api/runs/{run_id}/log")
def log(run_id:int): return {"log":run_log(run_id)}

@app.get("/api/runs/{run_id}/trials/{trial_id}")
def get_trial(run_id:int, trial_id:str):
    with SessionLocal() as db: row=db.get(Run,run_id)
    if not row: raise HTTPException(404,"run not found")
    value=trial_detail(row,trial_id)
    if not value: raise HTTPException(404,"trial not found")
    return value

@app.get("/api/runs/{run_id}/trials/{trial_id}/log")
def get_trial_log(run_id:int, trial_id:str):
    with SessionLocal() as db: row=db.get(Run,run_id)
    if not row: raise HTTPException(404,"run not found")
    value=trial_log(row,trial_id)
    if not value: raise HTTPException(404,"trial log not found")
    return {"log":value}

@app.post("/api/runs/{run_id}/cancel")
def cancel(run_id:int): return {"cancelled":cancel_run(run_id)}

@app.post("/api/runs/{run_id}/trials/retry", status_code=202)
def retry_run_trials(run_id: int, draft: RetryTrialsDraft):
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if not run:
            raise HTTPException(404, "run not found")
        if run.status not in TERMINAL_STATES:
            raise HTTPException(409, "Run 正在执行，不能同时提交 Trial 重试")
    try:
        return retry_trials(run_id, draft.trial_ids)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

@app.delete("/api/runs/{run_id}/trials/{trial_id}")
def delete_trial(run_id: int, trial_id: str):
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if not run:
            raise HTTPException(404, "run not found")
        if run.status not in TERMINAL_STATES:
            raise HTTPException(409, "运行中的 Trial 不允许删除，请先取消 Run")
        trial = next(
            (item for item in parsed_run_detail(run, include_patches=False)["trials"] if item["id"] == trial_id),
            None,
        )
        if not trial:
            raise HTTPException(404, "trial not found")
        folder = trial_folder(run, trial_id)
        job_name = run.job_name
        jobs_root = jobs_root_for(run)

    docker_report = None
    if folder and get_preferences().get("docker_cleanup_on_delete", True):
        try:
            docker_report = cleanup_job_resources(
                job_name, jobs_root, DockerCleanupPolicy(), trigger="delete-trial",
                projects=[sanitize_compose_project_name(trial_id)],
            )
        except Exception as exc:
            docker_report = {"available": False, "errors": [str(exc)]}
    if folder:
        try:
            shutil.rmtree(folder)
        except OSError as exc:
            raise HTTPException(500, f"Trial 文件删除失败：{exc}") from exc

    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if not run:
            raise HTTPException(404, "run not found")
        deleted = deleted_trial_entries(run)
        deleted.append({"id": trial_id, "task": trial["task"], "attempt": trial["attempt"]})
        run.deleted_trials_json = json.dumps(deleted, ensure_ascii=False)
        db.commit()

    response = {"deleted": True, "run_id": run_id, "trial_id": trial_id}
    if docker_report is not None:
        response["docker_cleanup"] = {
            "removed_images": docker_report.get("removed_images", []),
            "skipped_images": docker_report.get("skipped_images", []),
            "errors": docker_report.get("errors", []),
        }
    return response

def _safe_job_dir(root: Path, job_name: str) -> Path | None:
    """rmtree 只允许作用于 jobs 根目录内的直接子目录。"""
    if not is_safe_job_name(job_name):
        return None
    try:
        resolved = (root / job_name).resolve()
        root_resolved = root.resolve()
    except OSError:
        return None
    if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
        return None
    return resolved

@app.delete("/api/runs/{run_id}")
def delete_run(run_id:int):
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run: raise HTTPException(404,"run not found")
        if run.status not in TERMINAL_STATES:
            raise HTTPException(409,"运行中任务请先取消，再删除")
        job_name=run.job_name
        jobs_root=jobs_root_for(run)
    retry_names = retry_job_names(jobs_root, job_name)
    # Docker 定向清理必须先于 artifacts 删除，否则失去 Trial 资源标识
    docker_report=None
    if get_preferences().get("docker_cleanup_on_delete", True):
        docker_report={"removed_images": [], "skipped_images": [], "errors": []}
        for cleanup_name in (job_name, *retry_names):
            try:
                report=cleanup_job_resources(
                    cleanup_name,
                    jobs_root,
                    DockerCleanupPolicy(),
                    trigger="delete-run",
                )
                docker_report["removed_images"].extend(report.get("removed_images", []))
                docker_report["skipped_images"].extend(report.get("skipped_images", []))
                docker_report["errors"].extend(report.get("errors", []))
            except Exception as exc:
                docker_report["errors"].append(str(exc))
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if run:
            db.execute(delete(Baseline).where(Baseline.run_id==run_id))
            db.delete(run); db.commit()
    target=_safe_job_dir(jobs_root, job_name)
    if target:
        shutil.rmtree(target, ignore_errors=True)
        (jobs_root/f"{job_name}.supervisor.log").unlink(missing_ok=True)
        (jobs_root/f"{job_name}.docker-cleanup.json").unlink(missing_ok=True)
    for retry_name in retry_names:
        retry_target = _safe_job_dir(jobs_root, retry_name)
        if retry_target:
            shutil.rmtree(retry_target, ignore_errors=True)
        (jobs_root/f"{retry_name}.supervisor.log").unlink(missing_ok=True)
        (jobs_root/f"{retry_name}.docker-cleanup.json").unlink(missing_ok=True)
    response={"deleted":True,"id":run_id}
    if docker_report is not None:
        response["docker_cleanup"]={
            "removed_images": docker_report.get("removed_images", []),
            "skipped_images": docker_report.get("skipped_images", []),
            "errors": docker_report.get("errors", []),
        }
    return response

def _run_fingerprint(run: Run) -> tuple:
    """轻量变化指纹：状态 + result.json mtime + 5 秒兜底刷新（阶段探测依赖的其他文件不逐个 stat）。"""
    parts = [run.status, str(run.finished_at), int(time.monotonic() // 5)]
    root = jobs_root_for(run) / run.job_name
    if root.exists():
        for path in sorted(root.rglob("result.json")):
            try:
                parts.append(f"{path.name}:{path.stat().st_mtime_ns}")
            except OSError:
                pass
    return tuple(parts)

@app.get("/api/runs/{run_id}/events")
async def run_events(run_id:int):
    if not get_run(run_id): raise HTTPException(404,"run not found")
    async def stream():
        last_fingerprint = None
        last_payload = None
        while True:
            with SessionLocal() as db: row=db.get(Run,run_id)
            if not row: break
            fingerprint = _run_fingerprint(row)
            if fingerprint != last_fingerprint:
                last_fingerprint = fingerprint
                detail = parsed_run_detail(row, include_patches=False)
                payload = json.dumps(detail, default=str, ensure_ascii=False)
                if payload != last_payload:
                    yield f"data: {payload}\n\n"; last_payload = payload
            if row.status in TERMINAL_STATES: break
            await asyncio.sleep(1)
    return StreamingResponse(stream(),media_type="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.get("/api/compare")
def compare(ids:list[int]=Query(default=[]), items:list[str]=Query(default=[])):
    return compare_runs(ids, _parse_compare_items(items) or None)

@app.get("/api/compare/options")
def compare_options():
    return list_details()

@app.post("/api/compare")
def compare_selected(payload: CompareRequest):
    return compare_runs([], _parse_compare_items(payload.items) or None)

@app.post("/api/compare/analyze")
def analyze_selected_comparison(payload: CompareAnalysisRequest):
    selections = _parse_compare_items(payload.items)
    try:
        return analyze_compare_items(selections)
    except CompareAnalysisInputError as exc:
        raise HTTPException(422, str(exc)) from exc
    except CompareAnalysisProviderError as exc:
        raise HTTPException(502, str(exc)) from exc

def _parse_compare_items(items: list[str]) -> list[tuple[int, str]]:
    selections=[]
    for item in items:
        run_id, separator, task = item.partition(":")
        if not separator or not run_id.isdigit() or not task:
            raise HTTPException(422,"无效的比较结果标识")
        selections.append((int(run_id), task))
    return selections

@app.get("/api/baselines")
def baselines():
    with SessionLocal() as db:
        rows=db.scalars(select(Baseline).order_by(Baseline.created_at.desc())).all()
        return [{"id":row.id,"run_id":row.run_id,"name":row.name,"created_at":row.created_at} for row in rows]

@app.post("/api/runs/{run_id}/baseline")
def set_baseline(run_id:int,draft:BaselineDraft):
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run: raise HTTPException(404,"run not found")
        if run.status != "completed": raise HTTPException(422,"只有已完成运行可以设为基线")
        row=db.scalar(select(Baseline).where(Baseline.run_id==run_id))
        if row: row.name=draft.name or row.name
        else: db.add(Baseline(run_id=run_id,name=draft.name or f"{run.agent} · {run.model} · {run.reasoning_effort}"))
        db.commit()
    return {"saved":True}

@app.delete("/api/runs/{run_id}/baseline")
def remove_baseline(run_id:int):
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run: raise HTTPException(404,"run not found")
        matching=db.scalars(select(Run.id).where(Run.agent==run.agent,Run.model==run.model,Run.reasoning_effort==run.reasoning_effort)).all()
        db.execute(delete(Baseline).where(Baseline.run_id.in_(matching))); db.commit()
    return {"deleted":True}

@app.get("/api/tasks")
def tasks(): return task_catalog(settings.tasks_dir)

@app.get("/api/tasks/official-meta")
def tasks_official_meta(): return official_stats_meta()

@app.post("/api/tasks/sync-official")
def sync_official():
    try:
        return sync_official_stats()
    except Exception as exc:
        raise HTTPException(502, f"官方统计同步失败：{exc}")

@app.get("/api/settings")
def read_settings(): return get_preferences()

@app.put("/api/settings")
def save_settings(payload: SettingsUpdate):
    preferences = update_preferences(payload)
    provider = _provider_bootstrap(preferences, force_refresh=True)
    corrections = {}
    if preferences["default_model"] != provider["default_model"]:
        corrections["default_model"] = provider["default_model"]
    if preferences["default_effort"] != provider["default_effort"]:
        corrections["default_effort"] = provider["default_effort"]
    return update_preferences(SettingsUpdate(**corrections)) if corrections else preferences

def _active_run_count() -> int:
    with SessionLocal() as db:
        return len(db.scalars(select(Run.id).where(Run.status.in_(ACTIVE_STATES))).all())

def _docker_scope_targets(payload: DockerCleanupRequest) -> list[dict]:
    """按 scope 在后端重新计算目标，不信任前端提交的资源名。"""
    if payload.scope == "job":
        if payload.run_id is None:
            raise HTTPException(422, "scope=job 需要 run_id")
        with SessionLocal() as db:
            run = db.get(Run, payload.run_id)
        if not run:
            raise HTTPException(404, "run not found")
        if run.status not in TERMINAL_STATES:
            raise HTTPException(409, "只允许清理终态运行的 Docker 资源")
        return [{"job_name": run.job_name, "jobs_root": jobs_root_for(run)}]
    if payload.scope == "expired":
        return expired_jobs(payload.retention_hours)
    return []

@app.get("/api/docker/storage")
def docker_storage():
    return docker_storage_summary()

@app.post("/api/docker/cleanup/preview")
def docker_cleanup_preview(payload: DockerCleanupRequest):
    if payload.scope == "build_cache":
        preview = preview_builder_cleanup(payload.retention_hours)
        preview["active_runs"] = _active_run_count()
        return preview
    if payload.scope == "orphaned":
        inventory = managed_image_inventory()
        unique = image_unique_sizes()
        images = inventory["orphaned"]
        return {"scope": "orphaned", "images": images, "image_count": len(images),
                "reclaimable_bytes": sum(unique.get(ref, 0) for ref in images) or None}
    targets = _docker_scope_targets(payload)
    unique = image_unique_sizes()
    jobs, image_count, reclaimable = [], 0, 0
    for target in targets:
        preview = preview_job_cleanup(target["job_name"], target["jobs_root"])
        jobs.append(preview)
        image_count += len(preview["images"])
        reclaimable += sum(unique.get(ref, 0) for ref in preview["images"])
    return {"scope": payload.scope, "jobs": jobs, "image_count": image_count, "reclaimable_bytes": reclaimable or None}

@app.post("/api/docker/cleanup")
def docker_cleanup(payload: DockerCleanupRequest):
    active = _active_run_count()
    if payload.scope == "build_cache":
        if active:
            raise HTTPException(409, "存在未结束的运行，禁止清理构建缓存")
        return prune_builder_cache(payload.retention_hours)
    if payload.scope == "orphaned":
        if active:
            raise HTTPException(409, "存在未结束的运行，禁止批量清理")
        return remove_orphaned_images()
    if payload.scope == "expired" and active:
        raise HTTPException(409, "存在未结束的运行，禁止批量清理")
    targets = _docker_scope_targets(payload)
    results = [cleanup_job_resources(t["job_name"], t["jobs_root"], DockerCleanupPolicy(), trigger=f"manual-{payload.scope}") for t in targets]
    return {
        "scope": payload.scope,
        "results": results,
        "removed_images": [ref for report in results for ref in report.get("removed_images", [])],
        "skipped_images": [item for report in results for item in report.get("skipped_images", [])],
        "errors": [message for report in results for message in report.get("errors", [])],
    }

def _backup_payload() -> dict:
    with SessionLocal() as db:
        settings_rows=[{"key":r.key,"value":r.value} for r in db.scalars(select(Setting)).all()]
        runs_rows=[]
        for r in db.scalars(select(Run)).all():
            runs_rows.append({column.name:getattr(r,column.name) for column in Run.__table__.columns})
        baseline_rows=[{column.name:getattr(r,column.name) for column in Baseline.__table__.columns} for r in db.scalars(select(Baseline)).all()]
    return {"version":1,"created_at":datetime.now(UTC),"settings":settings_rows,"runs":runs_rows,"baselines":baseline_rows}

@app.get("/api/export.json")
def export_json():
    content=json.dumps(_backup_payload(),default=str,ensure_ascii=False,indent=2)
    return Response(content,media_type="application/json",headers={"Content-Disposition":"attachment; filename=deepswe-ui-backup.json"})

def _formula_safe(value):
    # 防 Excel 公式注入：文本以 = + - @ 或控制字符开头时加单引号前缀
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value

@app.get("/api/export.csv")
def export_csv():
    rows=list_runs(); output=io.StringIO(); fields=list(rows[0].keys()) if rows else ["id","job_name","status"]
    writer=csv.DictWriter(output,fieldnames=fields,extrasaction="ignore"); writer.writeheader()
    for row in rows: writer.writerow({key:_formula_safe(json.dumps(value,ensure_ascii=False) if isinstance(value,(list,dict)) else value) for key,value in row.items()})
    # BOM 让中文 Windows Excel 正确按 UTF-8 解码
    return Response("﻿"+output.getvalue(),media_type="text/csv; charset=utf-8",headers={"Content-Disposition":"attachment; filename=deepswe-ui-runs.csv"})

@app.post("/api/restore")
def restore(payload:RestorePayload):
    if payload.version != 1: raise HTTPException(422,"不支持的备份版本")
    skipped_runs=0
    with SessionLocal() as db:
        for item in payload.settings:
            key, value = item.get("key"), item.get("value")
            # 白名单键 + JSON 可解析，防设置毒化导致所有端点 500
            if key not in PREFERENCE_KEYS or not isinstance(value, str):
                continue
            try:
                json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            db.merge(Setting(key=key,value=value))
        existing_runs=db.scalars(select(Run)).all(); used_ids={r.id for r in existing_runs}; by_job={r.job_name:r for r in existing_runs}; restored_ids={}
        for item in payload.runs:
            allowed={column.name for column in Run.__table__.columns}; values={k:v for k,v in item.items() if k in allowed}
            old_id=values.get("id"); job_name=values.get("job_name")
            # job_name 进入文件删除路径，必须白名单；status 归一为终态防僵尸行
            if not is_safe_job_name(job_name):
                skipped_runs+=1; continue
            if values.get("status") not in TERMINAL_STATES:
                values["status"]="interrupted"
            values.pop("pid",None)
            for key in ("created_at","finished_at"):
                parsed=parse_timestamp(values.get(key))
                if parsed is None: values.pop(key,None)
                else: values[key]=parsed
            if values.get("jobs_dir") is not None and not isinstance(values.get("jobs_dir"),str):
                values.pop("jobs_dir",None)
            if job_name in by_job:
                restored_ids[old_id]=by_job[job_name].id; continue
            if values.get("id") in used_ids: values.pop("id",None)
            row=Run(**values); db.add(row); db.flush(); restored_ids[old_id]=row.id; used_ids.add(row.id)
        db.flush()
        existing={b.run_id for b in db.scalars(select(Baseline)).all()}
        valid_runs={r.id for r in db.scalars(select(Run)).all()}
        for item in payload.baselines:
            run_id=restored_ids.get(item.get("run_id"),item.get("run_id"))
            if run_id in valid_runs and run_id not in existing:
                created=parse_timestamp(item.get("created_at"))
                db.add(Baseline(run_id=run_id,name=str(item.get("name") or "Restored baseline")[:160],created_at=created or datetime.now(UTC)))
        db.commit()
    return {"restored":True,"skipped_runs":skipped_runs}

frontend_dist = settings.tasks_dir.parent / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
