import json, subprocess, uuid
import ipaddress
from pathlib import Path
from types import SimpleNamespace
from app.database import SessionLocal, init_db
from app.models import Run
from app import runner
from app import results
from app.pier_retry_patch.transient import is_transient_agent_failure
from app.pier_retry_patch.networking import DEFAULT_NETWORK_POOL, trial_network_subnets
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
    assert "request_max_retries = 6" in text
    assert "stream_max_retries = 6" in text
    assert "stream_idle_timeout_ms = 600000" in text

def test_mini_limits_config_sets_step_limit(tmp_path: Path):
    # cost_limit 对自建网关模型算不出成本，step_limit 是唯一确定性护栏
    path=runner._mini_limits_config(tmp_path)
    text=path.read_text(encoding="utf-8")
    assert f"step_limit: {runner.MINI_STEP_LIMIT}" in text and text.startswith("agent:")
    assert "prompt_cache_key: deepswe-" in text and "prompt_cache_retention: 24h" in text

def test_infrastructure_retry_args_are_bounded_and_typed():
    assert runner.INFRASTRUCTURE_RETRY_DELAYS_SEC == (2, 5, 15, 45, 135, 405)
    enabled=runner._pier_retry_args(True, 3)
    assert enabled[:2] == ["--max-retries", "3"]
    assert enabled.count("--retry-include") == 1
    assert "TransientAgentInfrastructureError" in enabled
    assert runner._pier_retry_args(False, 3) == ["--max-retries", "0"]
    assert runner._pier_retry_args(True, 0) == ["--max-retries", "0"]

def test_trial_docker_networks_avoid_lan_and_are_stable():
    first=trial_network_subnets("run-1/task-a__abc")
    second=trial_network_subnets("run-1/task-a__abc")
    other=trial_network_subnets("run-1/task-b__xyz")
    pool=ipaddress.ip_network(DEFAULT_NETWORK_POOL)
    assert first == second and first != other
    assert first[0] != first[1]
    assert all(ipaddress.ip_network(value).subnet_of(pool) for value in first)
    assert all(ipaddress.ip_address("192.168.0.108") not in ipaddress.ip_network(value)
               for value in first)

def test_docker_proxy_connectivity_checks_from_container(monkeypatch):
    calls = []
    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "C:/docker.exe")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner._docker_proxy_connectivity("http://127.0.0.1:9887/v1")
    command_args=[args for args, _ in calls]
    assert any(args[1:4] == ["network", "create", "--internal"] for args in command_args)
    assert any(args[1:3] == ["network", "create"] and "--internal" not in args
               for args in command_args)
    create=next(args for args in command_args if len(args)>1 and args[1] == "create")
    assert "host.docker.internal" in create and "9887" in create
    assert any(len(args)>2 and args[1:3] == ["start", "-a"] for args in command_args)

def test_docker_proxy_connectivity_reports_target_and_reason(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: "docker")
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1 if args[1:3] == ["start", "-a"] else 0,
            "", "nc: timed out" if args[1:3] == ["start", "-a"] else "",
        )
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    try:
        runner._docker_proxy_connectivity("http://192.168.0.108:9887/v1")
        assert False, "unreachable proxy must fail preflight"
    except RuntimeError as exc:
        assert "192.168.0.108:9887" in str(exc)
        assert "timed out" in str(exc)

def test_sync_nonzero_exit_prefers_agent_network_failure(tmp_path: Path, monkeypatch):
    job="test-"+uuid.uuid4().hex; run_id=make_run(job)
    folder=tmp_path/job
    write_result(folder, {"stats":{"n_running_trials":1,"evals":{}}})
    agent=folder/"task-a__x"/"agent"; agent.mkdir(parents=True)
    (agent/"codex.txt").write_text(
        'Reconnecting... 1/10 (unexpected status 503 Service Unavailable)',
        encoding="utf-8",
    )
    monkeypatch.setattr(runner.settings,"jobs_dir",tmp_path)
    runner._sync_result(run_id,1)
    error=runner.get_run(run_id)["error"] or ""
    assert "HTTP 503 Service Unavailable" in error
    assert "Pier 进程退出码" not in error

def test_squid_connect_failure_summary_is_actionable():
    message = """
    <body id=ERR_CONNECT_FAIL>
    <p><b>Connection to 192.168.0.108 failed.</b></p>
    <p>The system returned: <i>(110) Connection timed out</i></p>
    """
    summary=runner._network_failure_summary(message)
    assert summary == "模型代理连接失败：192.168.0.108（Docker/Squid：Connection timed out）"

def test_transient_agent_failure_classification_is_selective():
    assert is_transient_agent_failure(
        "NonZeroAgentExitCodeError", "unexpected status 503 Service Unavailable"
    )
    assert is_transient_agent_failure(
        "NonZeroAgentExitCodeError", "API Error: The operation timed out."
    )
    assert is_transient_agent_failure("ConnectionError", "socket closed")
    assert not is_transient_agent_failure(
        "NonZeroAgentExitCodeError", 'terminal_reason":"max_turns"'
    )
    assert not is_transient_agent_failure(
        "NonZeroAgentExitCodeError", "tests failed with exit code 1"
    )

def test_run_draft_exposes_retry_and_agent_limits():
    draft=RunDraft(agent="claude-code", tasks=["actionlint-action-pinning-lint"],
                   infrastructure_max_retries=4, claude_max_turns=150,
                   codex_request_max_retries=7, codex_stream_max_retries=8,
                   codex_stream_idle_timeout_seconds=900)
    assert draft.infrastructure_max_retries == 4
    assert draft.claude_max_turns == 150
    assert draft.codex_request_max_retries == 7
    assert draft.codex_stream_max_retries == 8
    assert draft.codex_stream_idle_timeout_seconds == 900

def test_trial_classifies_transient_repository_failure(tmp_path: Path):
    folder=tmp_path/"task-a__x"; folder.mkdir()
    write_result(folder, {"task_name":"task-a","exception_info":{
        "exception_type":"RuntimeError",
        "exception_message":"apt-get update failed: 502 Bad Gateway; Failed to fetch repository metadata",
    }})
    trial=results._trial(folder)
    assert trial["failure_type"] == "InfrastructureNetworkError"

def test_trial_classifies_transient_error_from_agent_log_tail(tmp_path: Path):
    folder=tmp_path/"task-a__x"; (folder/"agent").mkdir(parents=True)
    write_result(folder, {"task_name":"task-a","exception_info":{
        "exception_type":"NonZeroAgentExitCodeError",
        "exception_message":"Command failed (exit 1); stdout truncated",
    }})
    (folder/"agent"/"claude-code.txt").write_text(
        '{"type":"result","result":"API Error: The operation timed out."}',
        encoding="utf-8",
    )
    assert results._trial(folder)["failure_type"] == "InfrastructureNetworkError"

def test_official_regression_contains_baseline_values(monkeypatch):
    current={"trials":[{"task":"task-a","reward":1,"duration_seconds":80.0}]}
    monkeypatch.setattr(results, "load_official_stats", lambda:{"task-a":{
        "pass_rate":.75,"avg_duration_seconds":100.0,"trials":40,
    }})
    regression=results.regression_for(None, current)
    assert regression["current_pass_rate"] == 1
    assert regression["baseline_pass_rate"] == .75
    assert regression["pass_rate_delta"] == .25
    assert regression["current_duration_seconds"] == 80
    assert regression["baseline_duration_seconds"] == 100
    assert regression["baseline_trials"] == 40

def test_compare_uses_exact_configuration_and_task_averages(monkeypatch):
    detail = {
        "id": 7,
        "tasks": ["task-a"],
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "service_tier": "standard",
        "trials": [
            {"task": "task-a", "reward": 1, "duration_seconds": 60.0, "input_tokens": 100, "cached_tokens": 50, "output_tokens": 10, "reported_cost_usd": 1.0, "steps": 10},
            {"task": "task-a", "reward": 0, "duration_seconds": 120.0, "input_tokens": 300, "cached_tokens": 150, "output_tokens": 30, "reported_cost_usd": 3.0, "steps": 30},
        ],
    }
    class FakeSession:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def get(self, _model, run_id): return SimpleNamespace(id=run_id)
    monkeypatch.setattr(results, "SessionLocal", FakeSession)
    monkeypatch.setattr(results, "run_detail", lambda _row, include_patches=False: detail)
    monkeypatch.setattr(results, "task_identity", lambda task: {"task": task, "task_code": "TASK-001", "task_title": task})
    monkeypatch.setattr(results, "load_official_stats", lambda: {"task-a": {
        "trials": 20,
        "pass_rate": 0.4,
        "avg_duration_seconds": 100.0,
        "configurations": [{"model": "gpt-5-6-sol", "reasoning_effort": "xhigh", "trials": 4, "pass_rate": 0.5, "avg_duration_seconds": 90.0}],
    }})
    comparison = results.compare_runs([7], [(7, "task-a")])
    row = comparison["tasks"][0]
    exact = row["official_configurations"][0]
    local = row["runs"]["7"]
    assert exact["available"] is True and exact["pass_rate"] == 0.5
    assert local["passed"] is True and local["pass_rate"] == 0.5
    assert local["duration_seconds"] == 90.0
    assert local["input_tokens"] == 200 and local["total_input_tokens"] == 400
    assert local["cost_usd"] == 2.0 and local["total_cost_usd"] == 4.0

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
    row = runner.create_run(RunDraft(
        agent="codex", tasks=["actionlint-action-pinning-lint"],
        infrastructure_max_retries=3, claude_max_turns=140,
        codex_request_max_retries=7, codex_stream_max_retries=8,
        codex_stream_idle_timeout_seconds=900,
    ))
    try:
        assert row.job_name == f"run-{row.id:06d}-codex"
        assert runner.serialize(row)["run_code"] == f"RUN-{row.id:06d}"
        assert row.infrastructure_max_retries == 3
        assert row.claude_max_turns == 140
        assert row.codex_request_max_retries == 7
        assert row.codex_stream_max_retries == 8
        assert row.codex_stream_idle_timeout_seconds == 900
    finally:
        with SessionLocal() as db:
            saved = db.get(Run, row.id)
            if saved:
                db.delete(saved)
                db.commit()
