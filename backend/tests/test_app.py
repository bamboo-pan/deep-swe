import json
import subprocess
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.database import SessionLocal
from app.models import Run, Setting
from app.security import is_safe_job_name, read_credential, redact

def test_command_check_timeout_is_non_blocking_warning(monkeypatch):
    from app import diagnostics
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == diagnostics.COMMAND_CHECK_TIMEOUT_SEC == 30
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(diagnostics.subprocess, "run", timeout)
    result = diagnostics.command_check("pier", ["--version"])
    assert result["status"] == "warning"
    assert "30 秒" in result["message"]

def test_health():
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"

def test_bootstrap_has_seven_tasks():
    with TestClient(app) as client:
        tasks = client.get("/api/bootstrap").json()["task_suite"]["tasks"]
        assert len(tasks) == 7
        assert all(task["suite_id"].startswith("TASK-") for task in tasks if task["available"])

def test_task_catalog_has_stable_management_numbers():
    with TestClient(app) as client:
        tasks = client.get("/api/tasks").json()
        assert tasks[0]["task_number"] == 1
        assert tasks[0]["code"] == "TASK-001"
        assert [task["id"] for task in tasks] == sorted(task["id"] for task in tasks)

def test_credential_and_redaction(tmp_path: Path):
    path = tmp_path / "credential.txt"
    path.write_text("﻿ http://127.0.0.1:9887/v1 \n secret-token \n", encoding="utf-8")
    cred = read_credential(path)
    assert cred.url.endswith("/v1")
    assert "secret-token" not in redact("Bearer secret-token", [cred.token])

def test_concurrency_guard():
    with TestClient(app) as client:
        assert client.get("/api/concurrency/3").json()["level"] == "warning"
        assert client.get("/api/concurrency/5").json()["level"] == "blocked"

def test_missing_task_is_rejected_for_claude_adapter():
    with TestClient(app) as client:
        response=client.post("/api/runs",json={"agent":"claude-code","model":"gpt-5.6-sol","reasoning_effort":"high","tasks":["missing-task"]})
        assert response.status_code == 422

def test_empty_task_list_is_rejected():
    # 空列表会让 pier 失去 -i 过滤、跑满全部任务，必须在 API 层拒绝
    with TestClient(app) as client:
        assert client.post("/api/runs",json={"agent":"codex","tasks":[]}).status_code==422

def test_run_validation_counts_trials_and_rejects_missing_task():
    with TestClient(app) as client:
        good=client.post("/api/runs/validate",json={"tasks":["actionlint-action-pinning-lint"],"attempts_per_task":4})
        assert good.status_code==200 and good.json()["trial_count"]==4
        assert client.post("/api/runs/validate",json={"tasks":["not-a-real-task"]}).status_code==422

def test_run_detail_404_and_cancel_inactive():
    with TestClient(app) as client:
        assert client.get("/api/runs/999999").status_code==404
        assert client.post("/api/runs/999999/cancel").json()=={"cancelled":False}

def test_compare_accepts_independent_run_task_items():
    with TestClient(app) as client:
        response=client.get("/api/compare", params=[("items", "1:task-a"), ("items", "1:task-b")])
        assert response.status_code == 200
        assert response.json()["selections"] == ["1:task-a", "1:task-b"]

def test_compare_accepts_unlimited_items_over_post_and_get():
    items = [f"{900000 + index}:task-{index}" for index in range(12)]
    with TestClient(app) as client:
        get_response = client.get("/api/compare", params=[("items", item) for item in items])
        post_response = client.post("/api/compare", json={"items": items})
        assert get_response.status_code == 200
        assert post_response.status_code == 200
        assert get_response.json()["selections"] == items
        assert post_response.json()["selections"] == items

def test_compare_rejects_invalid_item_key():
    with TestClient(app) as client:
        assert client.get("/api/compare", params={"items":"not-a-pair"}).status_code == 422
        assert client.post("/api/compare", json={"items":["not-a-pair"]}).status_code == 422

def test_delete_terminal_run_and_reject_active_run():
    with TestClient(app) as client:
        with SessionLocal() as db:
            done=Run(status="completed",agent="codex",model="test",reasoning_effort="high",job_name="delete-done",tasks_json="[]")
            active=Run(status="running",agent="codex",model="test",reasoning_effort="high",job_name="delete-active",tasks_json="[]")
            db.add_all([done,active]); db.commit(); db.refresh(done); db.refresh(active)
            done_id,active_id=done.id,active.id
        assert client.delete(f"/api/runs/{active_id}").status_code==409
        body=client.delete(f"/api/runs/{done_id}").json()
        assert body["deleted"] is True
        assert "docker_cleanup" in body  # Docker 清理发生在 artifacts 删除之前并返回摘要
        assert client.get(f"/api/runs/{done_id}").status_code==404

def test_job_name_whitelist():
    assert is_safe_job_name("ui-20260711-123456-abc123")
    assert not is_safe_job_name("..")
    assert not is_safe_job_name("a/../b")
    assert not is_safe_job_name("C:\\Windows")
    assert not is_safe_job_name(".hidden")
    assert not is_safe_job_name("")
    assert not is_safe_job_name(None)

def test_restore_rejects_malicious_rows():
    with TestClient(app) as client:
        payload={
            "version":1,
            "settings":[
                {"key":"jobs_dir","value":"not-json"},          # 非 JSON → 跳过
                {"key":"evil_key","value":"\"x\""},               # 非白名单键 → 跳过
                {"key":"default_agent","value":"\"codex\""},     # 合法 → 接受
            ],
            "runs":[
                {"job_name":"..","status":"completed","agent":"codex","model":"m","reasoning_effort":"high","tasks_json":"[]"},           # 路径遍历 → 跳过
                {"job_name":"restore-zombie","status":"running","agent":"codex","model":"m","reasoning_effort":"high","tasks_json":"[]"},  # 非终态 → 归一 interrupted
                {"job_name":"restore-good","status":"completed","agent":"codex","model":"m","reasoning_effort":"high","tasks_json":"[]","created_at":"2026-07-11T00:00:00Z","finished_at":"bad-timestamp"},
            ],
            "baselines":[],
        }
        body=client.post("/api/restore",json=payload).json()
        assert body["restored"] is True and body["skipped_runs"]==1
        with SessionLocal() as db:
            names={r.job_name:r for r in db.scalars(select(Run)).all()}
        assert ".." not in names
        assert names["restore-zombie"].status=="interrupted"
        assert names["restore-good"].status=="completed"
        # 设置未被毒化：所有偏好仍可读取
        prefs=client.get("/api/settings").json()
        assert prefs["default_agent"]=="codex"
        # 还原默认，避免影响其他测试
        with SessionLocal() as db:
            row=db.get(Setting,"default_agent")
            if row: db.delete(row); db.commit()

def test_docker_storage_endpoint_survives_docker_missing(monkeypatch):
    from app import docker_cleanup as dc
    monkeypatch.setattr(dc.shutil,"which",lambda name:None)
    with TestClient(app) as client:
        body=client.get("/api/docker/storage").json()
        assert body["available"] is False and "active_runs" in body

def test_docker_cleanup_preview_does_not_delete(monkeypatch):
    from app import docker_cleanup as dc
    calls=[]
    def fake_docker(args,timeout=30,input_text=None):
        calls.append(list(args))
        if args[:2]==["image","ls"]: return True,"restore-good__ab12cd9-main:latest\n",""
        if args[:2]==["system","df"]: return True,"",""
        return True,"",""
    monkeypatch.setattr(dc,"_docker",fake_docker)
    with TestClient(app) as client:
        response=client.post("/api/docker/cleanup/preview",json={"scope":"orphaned"})
        assert response.status_code==200
    assert all(call[:2]!=["image","rm"] for call in calls)  # 预览绝不执行删除

def test_docker_cleanup_rejects_active_runs(monkeypatch):
    with TestClient(app) as client:
        with SessionLocal() as db:
            active=Run(status="running",agent="codex",model="m",reasoning_effort="high",job_name="docker-active",tasks_json="[]")
            db.add(active); db.commit(); db.refresh(active); active_id=active.id
        try:
            assert client.post("/api/docker/cleanup",json={"scope":"build_cache"}).status_code==409
            assert client.post("/api/docker/cleanup",json={"scope":"expired"}).status_code==409
            assert client.post("/api/docker/cleanup",json={"scope":"job","run_id":active_id}).status_code==409
        finally:
            with SessionLocal() as db:
                row=db.get(Run,active_id); db.delete(row); db.commit()

def test_docker_cleanup_scope_job_requires_terminal_run(monkeypatch):
    from app import docker_cleanup as dc
    monkeypatch.setattr(dc.shutil,"which",lambda name:None)
    with TestClient(app) as client:
        with SessionLocal() as db:
            done=Run(status="completed",agent="codex",model="m",reasoning_effort="high",job_name="docker-done",tasks_json="[]")
            db.add(done); db.commit(); db.refresh(done); done_id=done.id
        try:
            body=client.post("/api/docker/cleanup",json={"scope":"job","run_id":done_id}).json()
            assert body["scope"]=="job" and isinstance(body["removed_images"],list)
        finally:
            with SessionLocal() as db:
                row=db.get(Run,done_id); db.delete(row); db.commit()
