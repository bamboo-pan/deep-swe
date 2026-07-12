import json, uuid
from pathlib import Path
from app.database import SessionLocal, init_db
from app.models import Run
from app import runner
from app import results
from app.schemas import RunDraft

def make_run(job_name: str, **extra) -> int:
    init_db()
    with SessionLocal() as db:
        row=Run(status="running",job_name=job_name,agent="codex",model="gpt-5.6-sol",reasoning_effort="high",tasks_json='["actionlint-action-pinning-lint"]',**extra)
        db.add(row); db.commit(); db.refresh(row); return row.id

def write_result(folder: Path, payload: dict):
    folder.mkdir(parents=True, exist_ok=True)
    (folder/"result.json").write_text(json.dumps(payload), encoding="utf-8")

SUCCESS_PAYLOAD={"stats":{"n_errored_trials":0,"n_input_tokens":100,"n_cache_tokens":80,"n_output_tokens":20,"cost_usd":1.25,"evals":{"x":{"metrics":[{"reward":1.0}]}}}}

def test_sync_success_result(tmp_path: Path, monkeypatch):
    job="test-"+uuid.uuid4().hex; run_id=make_run(job)
    write_result(tmp_path/job, SUCCESS_PAYLOAD)
    monkeypatch.setattr(runner.settings,"jobs_dir",tmp_path); runner._sync_result(run_id,0)
    result=runner.get_run(run_id)
    assert result["status"]=="completed" and result["passed"] is True
    assert result["reward"]==1.0 and result["cost_usd"]==1.25

def test_sync_nonzero_returncode_marks_failed(tmp_path: Path, monkeypatch):
    # Pier 中途崩溃（退出码非零）不允许记为 completed
    job="test-"+uuid.uuid4().hex; run_id=make_run(job)
    write_result(tmp_path/job, SUCCESS_PAYLOAD)
    monkeypatch.setattr(runner.settings,"jobs_dir",tmp_path); runner._sync_result(run_id,1)
    result=runner.get_run(run_id)
    assert result["status"]=="failed"
    assert "退出码" in (result["error"] or "")

def test_sync_error_result(tmp_path: Path, monkeypatch):
    job="test-"+uuid.uuid4().hex; run_id=make_run(job)
    write_result(tmp_path/job, {"stats":{"n_errored_trials":1,"evals":{}}})
    monkeypatch.setattr(runner.settings,"jobs_dir",tmp_path); runner._sync_result(run_id,0)
    result=runner.get_run(run_id)
    assert result["status"]=="failed" and result["passed"] is False

def test_sync_without_verification_keeps_reward_null(tmp_path: Path, monkeypatch):
    # 禁用 Verifier 时聚合器产出 {"mean": 0.0}，「未测量」不能被记成「全部失败」
    job="test-"+uuid.uuid4().hex; run_id=make_run(job, verification=False)
    write_result(tmp_path/job, {"stats":{"n_errored_trials":0,"evals":{"x":{"metrics":[{"mean":0.0}]}}}})
    monkeypatch.setattr(runner.settings,"jobs_dir",tmp_path); runner._sync_result(run_id,0)
    result=runner.get_run(run_id)
    assert result["status"]=="completed"
    assert result["reward"] is None and result["passed"] is None

def test_sync_does_not_override_cancelled(tmp_path: Path, monkeypatch):
    # 取消已提交后，迟到的结果落库不得把 cancelled 改写成 completed
    job="test-"+uuid.uuid4().hex; run_id=make_run(job)
    with SessionLocal() as db:
        row=db.get(Run,run_id); row.status="cancelled"; db.commit()
    write_result(tmp_path/job, SUCCESS_PAYLOAD)
    monkeypatch.setattr(runner.settings,"jobs_dir",tmp_path); runner._sync_result(run_id,0)
    assert runner.get_run(run_id)["status"]=="cancelled"

def test_sync_survives_truncated_result_json(tmp_path: Path, monkeypatch):
    # taskkill 截断 result.json 时不允许抛异常连锁改写状态
    job="test-"+uuid.uuid4().hex; run_id=make_run(job)
    folder=tmp_path/job; folder.mkdir(); (folder/"result.json").write_text('{"stats": {"n_i', encoding="utf-8")
    monkeypatch.setattr(runner.settings,"jobs_dir",tmp_path); runner._sync_result(run_id,0)
    assert runner.get_run(run_id)["status"]=="failed"

def test_codex_run_passes_reasoning_effort(tmp_path: Path):
    # codex 分支必须显式透传 effort，否则 pier 默认 high 导致对比数据失真
    config=runner._codex_config("http://127.0.0.1:9887/v1", tmp_path, "gpt-5.6-sol", "low")
    text=config.read_text(encoding="utf-8")
    assert 'model_reasoning_effort = "low"' in text and 'model = "gpt-5.6-sol"' in text
    assert "host.docker.internal" in text

def test_mini_limits_config_sets_step_limit(tmp_path: Path):
    # cost_limit 对自建网关模型算不出成本，step_limit 是唯一确定性护栏
    path=runner._mini_limits_config(tmp_path)
    text=path.read_text(encoding="utf-8")
    assert f"step_limit: {runner.MINI_STEP_LIMIT}" in text and text.startswith("agent:")

def test_preflight_rejects_crlf_scripts(tmp_path: Path, monkeypatch):
    # CRLF 的 shebang 在容器内无法执行，verifier 必然失败，agent 费用全部报废（2026-07-12 事故）
    monkeypatch.setattr(runner.settings, "tasks_dir", tmp_path)
    scripts=tmp_path/"t1"/"tests"; scripts.mkdir(parents=True)
    (scripts/"test.sh").write_bytes(b"#!/bin/bash\r\necho ok\r\n")
    try:
        runner._preflight(["t1"]); assert False, "CRLF 脚本必须被拦截"
    except RuntimeError as exc:
        assert "CRLF" in str(exc) and "t1/tests/test.sh" in str(exc)
    (scripts/"test.sh").write_bytes(b"#!/bin/bash\necho ok\n")
    monkeypatch.setattr(runner, "docker_available", lambda: (True, "ok"))
    runner._preflight(["t1"])

def test_completed_trials_cost_prefers_reported_then_estimates(tmp_path: Path):
    t1=tmp_path/"task-a__x1"; t1.mkdir(parents=True)
    (t1/"result.json").write_text(json.dumps({"agent_result":{"cost_usd":2.5}}), encoding="utf-8")
    t2=tmp_path/"task-b__x2"; t2.mkdir()
    (t2/"result.json").write_text(json.dumps({"agent_result":{"n_input_tokens":1_000_000,"n_cache_tokens":0,"n_output_tokens":0}}), encoding="utf-8")
    total=runner._completed_trials_cost(tmp_path, "standard")
    assert total == 2.5 + 5.0  # 报告值 + 按 $5/1M 未缓存输入估算

def test_runaway_reason_detects_log_size_and_subagent_count(tmp_path: Path):
    agent_dir=tmp_path/"task-a__x1"/"agent"; agent_dir.mkdir(parents=True)
    assert runner._runaway_reason(tmp_path) is None
    with (agent_dir/"claude-code.txt").open("wb") as fh:
        fh.seek(runner.RUNAWAY_AGENT_LOG_MB*1024*1024); fh.write(b"0")
    assert "agent 日志" in (runner._runaway_reason(tmp_path) or "")
    (agent_dir/"claude-code.txt").write_bytes(b"small")
    subagents=agent_dir/"sessions"/"projects"/"-app"/"session-1"/"subagents"; subagents.mkdir(parents=True)
    for i in range(runner.RUNAWAY_SUBAGENT_FILES):
        (subagents/f"agent-{i}.jsonl").write_bytes(b"{}")
    assert "subagent" in (runner._runaway_reason(tmp_path) or "")

def test_log_is_bounded_and_missing_run_is_safe(tmp_path: Path, monkeypatch):
    job="test-"+uuid.uuid4().hex; run_id=make_run(job); monkeypatch.setattr(runner.settings,"jobs_dir",tmp_path)
    (tmp_path/f"{job}.supervisor.log").write_text("x"*210000,encoding="utf-8")
    assert len(runner.run_log(run_id))==200000
    assert runner.run_log(999999)==""

def test_trial_detail_uses_full_task_name_from_config(tmp_path: Path):
    task = "ofetch-per-origin-circuit-breaker"
    folder = tmp_path / "job" / "ofetch-per-origin-circuit-breake__abc123"
    folder.mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps({"task": {"path": str(tmp_path / "tasks" / task)}}), encoding="utf-8")
    assert results._trial(folder, expected_tasks=[task])["task"] == task

def test_new_pier_job_name_uses_public_run_identity(monkeypatch):
    class NoopThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
    monkeypatch.setattr(runner.threading, "Thread", NoopThread)
    row = runner.create_run(RunDraft(agent="codex", tasks=["actionlint-action-pinning-lint"]))
    try:
        assert row.job_name == f"run-{row.id:06d}-codex"
        assert runner.serialize(row)["run_code"] == f"RUN-{row.id:06d}"
    finally:
        with SessionLocal() as db:
            saved = db.get(Run, row.id)
            if saved:
                db.delete(saved)
                db.commit()
