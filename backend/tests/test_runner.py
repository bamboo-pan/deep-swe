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
from app.schemas import RunDraft, concurrency_advice, total_parallel_tasks

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

def test_mini_limits_config_sets_native_responses_reasoning(tmp_path: Path):
    # step_limit 是运行配置里的最大步数；cost_limit 走 --agent-kwarg 单独传
    path=runner._mini_limits_config(tmp_path, 180, "max")
    text=path.read_text(encoding="utf-8")
    assert "step_limit: 180" in text and text.startswith("agent:")
    assert 'reasoning:\n      effort: "max"' in text
    assert "reasoning_effort:" not in text
    assert "prompt_cache_key: deepswe-" in text and "prompt_cache_retention: 24h" in text

def test_reasoning_effort_adapter_records_native_mini_mapping():
    assert runner._reasoning_effort_adapter("mini-swe-agent", "max") == "reasoning.effort=max"
    assert runner._reasoning_effort_adapter("codex", "xhigh") == "model_reasoning_effort=xhigh"
    assert runner._reasoning_effort_adapter("claude-code", "none") == "thinking=disabled"

def test_infrastructure_retry_args_are_bounded_and_typed():
    assert runner.INFRASTRUCTURE_RETRY_DELAYS_SEC == (5, 30, 120, 300, 600, 900)
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

def test_unexpected_eof_requires_network_error_context():
    source = 'return nil, fmt.Errorf("unexpected EOF")'
    assert runner._network_failure_summary(source) is None
    assert runner._network_failure_summary("APIConnectionError: unexpected EOF") == "模型代理连接意外中断"

def test_transient_agent_failure_classification_is_selective():
    assert is_transient_agent_failure(
        "NonZeroAgentExitCodeError", "unexpected status 503 Service Unavailable"
    )
    assert is_transient_agent_failure(
        "NonZeroAgentExitCodeError", "API Error: The operation timed out."
    )
    assert is_transient_agent_failure("ConnectionError", "socket closed")
    assert is_transient_agent_failure(
        "NonZeroAgentExitCodeError", "APIConnectionError: unexpected EOF"
    )
    assert not is_transient_agent_failure(
        "NonZeroAgentExitCodeError", 'return nil, fmt.Errorf("unexpected EOF")'
    )
    # 2026-07-12 run-000001 实测漏网的两条镜像构建失败（原样字符串，防回归）：
    # apt 输出 502 与 Bad Gateway 之间是双空格，单空格标记词曾匹配不上
    assert is_transient_agent_failure(
        "RuntimeError",
        "E: Failed to fetch http://deb.debian.org/debian/dists/bookworm/InRelease  502  Bad Gateway [IP: 146.75.46.132 80]",
    )
    assert is_transient_agent_failure(
        "RuntimeError",
        "curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL in connection to github.com:443",
    )
    assert not is_transient_agent_failure(
        "NonZeroAgentExitCodeError", 'terminal_reason":"max_turns"'
    )
    assert not is_transient_agent_failure(
        "NonZeroAgentExitCodeError", "tests failed with exit code 1"
    )

def test_run_draft_exposes_retry_and_agent_limits():
    draft=RunDraft(agent="claude-code", tasks=["actionlint-action-pinning-lint"],
                   infrastructure_max_retries=4, agent_max_steps=150,
                   codex_request_max_retries=7, codex_stream_max_retries=8,
                   codex_stream_idle_timeout_seconds=900)
    assert draft.infrastructure_max_retries == 4
    assert draft.agent_max_steps == 150
    assert draft.codex_request_max_retries == 7
    assert draft.codex_stream_max_retries == 8
    assert draft.codex_stream_idle_timeout_seconds == 900

def test_parallel_task_count_uses_actual_trial_capacity():
    draft = RunDraft(
        tasks=["task-a", "task-b", "task-c"], attempts_per_task=2,
        concurrency=72, parallel_agent_count=3,
    )
    assert total_parallel_tasks(draft) == 18
    assert concurrency_advice(18)["level"] == "warning"
    assert concurrency_advice(19)["requires_confirmation"] is True
    assert concurrency_advice(72)["level"] == "danger"

    allowed = RunDraft(
        tasks=["task-a", "task-b", "task-c"], attempts_per_task=8,
        concurrency=24, parallel_agent_count=3,
    )
    blocked = RunDraft(
        tasks=["task-a", "task-b", "task-c"], attempts_per_task=9,
        concurrency=25, parallel_agent_count=3,
    )
    assert total_parallel_tasks(allowed) == 72
    assert concurrency_advice(total_parallel_tasks(allowed))["level"] == "danger"
    assert total_parallel_tasks(blocked) == 75
    assert concurrency_advice(total_parallel_tasks(blocked))["level"] == "blocked"

def test_create_run_rejects_unconfirmed_high_parallel_count():
    draft = RunDraft(
        tasks=["task-a", "task-b", "task-c"], attempts_per_task=8,
        concurrency=24, parallel_agent_count=3,
    )
    try:
        runner.create_run(draft)
    except ValueError as exc:
        assert "72" in str(exc) and "确认" in str(exc)
    else:
        raise AssertionError("high parallel run must require confirmation")

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
            {"id": "task-a__pass", "task": "task-a", "reward": 1, "duration_seconds": 60.0, "input_tokens": 100, "cached_tokens": 50, "output_tokens": 10, "reported_cost_usd": 1.0, "steps": 10},
            {"id": "task-a__fail", "task": "task-a", "reward": 0, "duration_seconds": 120.0, "input_tokens": 300, "cached_tokens": 150, "output_tokens": 30, "reported_cost_usd": 3.0, "steps": 30},
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

    exact_trial = results.compare_runs([7], [(7, "task-a__pass")])["tasks"][0]["runs"]["7"]
    assert exact_trial["attempts"] == 1 and exact_trial["pass_rate"] == 1
    assert exact_trial["input_tokens"] == 100 and exact_trial["trial_ids"] == ["task-a__pass"]

def test_trial_progress_counts_attempts_instead_of_distinct_tasks(tmp_path: Path, monkeypatch):
    job="test-"+uuid.uuid4().hex; run_id=make_run(job)
    with SessionLocal() as db:
        row=db.get(Run,run_id)
        row.status="cancelled"
        row.tasks_json='["task-a", "task-b"]'
        row.attempts_per_task=2
        db.commit()
    for folder, task, reward in (
        ("task-a__one", "task-a", 1),
        ("task-a__two", "task-a", 0),
        ("task-b__one", "task-b", 1),
        ("task-b__two", "task-b", 1),
    ):
        write_result(tmp_path/job/folder, {
            "task_name": task,
            "verifier_result": {"rewards": {"reward": reward}},
        })
    monkeypatch.setattr(runner.settings,"jobs_dir",tmp_path)
    summary=runner.get_run(run_id)
    assert summary["progress"] == {"completed": 4, "total": 4, "passed": 3, "percent": 100}
    assert summary["task_progress"] == {"passed": 2, "total": 2}
    with SessionLocal() as db:
        detail=results.run_detail(db.get(Run,run_id), include_patches=False)
    assert detail["progress"]["passed"] == 3 and detail["progress"]["total"] == 4

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

def test_trial_usage_reads_inflight_atif_trajectory(tmp_path: Path):
    trial=tmp_path/"task-a__x1"; (trial/"agent").mkdir(parents=True)
    (trial/"agent"/"trajectory.json").write_text(json.dumps({
        "final_metrics":{"total_cost_usd":4.06,"total_steps":52}}), encoding="utf-8")
    assert runner._trial_usage(trial, "standard") == (4.06, 52)
    # 已落盘 Trial 交给 Run 级累计，不再按进行中口径计费
    (trial/"result.json").write_text("{}", encoding="utf-8")
    assert runner._trial_usage(trial, "standard") == (None, None)

def test_trial_usage_estimates_when_gateway_reports_no_cost(tmp_path: Path):
    # 自建网关 litellm 价格表缺失时 cost_usd 为空，按 token 估算兜底
    trial=tmp_path/"task-a__x1"; (trial/"agent").mkdir(parents=True)
    (trial/"agent"/"trajectory.json").write_text(json.dumps({
        "final_metrics":{"total_cost_usd":None,"total_prompt_tokens":1_000_000,
                         "total_cached_tokens":0,"total_completion_tokens":0,"total_steps":9}}), encoding="utf-8")
    assert runner._trial_usage(trial, "standard") == (5.0, 9)

def test_trial_usage_falls_back_to_mini_trajectory(tmp_path: Path):
    trial=tmp_path/"task-a__x1"; (trial/"agent").mkdir(parents=True)
    (trial/"agent"/"mini-swe-agent.trajectory.json").write_text(json.dumps({
        "info":{"model_stats":{"instance_cost":1.75,"api_calls":30}}}), encoding="utf-8")
    assert runner._trial_usage(trial, "standard") == (1.75, 30)

def test_terminate_trial_kills_compose_containers_and_writes_marker(tmp_path: Path, monkeypatch):
    trial=tmp_path/"Task-A__X1"; trial.mkdir()
    calls=[]
    def fake_run(args, **_kwargs):
        calls.append(args)
        if "ps" in args:
            return SimpleNamespace(returncode=0, stdout="abc123\ndef456\n")
        return SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner._terminate_trial(trial, "费用 $9.00 达到单 Trial 上限 $8.00")
    assert any("label=com.docker.compose.project=task-a__x1" in " ".join(args) for args in calls)
    assert calls[-1][-3:] == ["kill", "abc123", "def456"]
    marker=json.loads((trial/"guard.json").read_text(encoding="utf-8"))
    assert "单 Trial 上限" in marker["reason"]

def test_terminate_trial_retries_when_no_container_found(tmp_path: Path, monkeypatch):
    trial=tmp_path/"task-a__x1"; trial.mkdir()
    monkeypatch.setattr(runner.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=""))
    assert not runner._terminate_trial(trial, "any")
    assert not (trial/"guard.json").exists()

def test_trial_surfaces_guard_termination_reason(tmp_path: Path):
    folder=tmp_path/"task-a__x1"; folder.mkdir(parents=True)
    write_result(folder, {"task_name":"task-a","exception_info":{
        "exception_type":"NonZeroAgentExitCodeError",
        "exception_message":"Command failed (exit 137)",
    }})
    (folder/"guard.json").write_text(json.dumps({"reason":"用量护栏终止该 Trial：费用 $9.00 达到单 Trial 上限 $8.00"}, ensure_ascii=False), encoding="utf-8")
    trial=results._trial(folder)
    assert trial["failure_type"] == "UsageGuardTerminated"
    assert "单 Trial 上限" in trial["failure_message"]

def test_verification_failure_has_actionable_reason(tmp_path: Path):
    folder=tmp_path/"task-a__x1"; folder.mkdir(parents=True)
    write_result(folder, {"task_name":"task-a","verifier_result":{"rewards":{
        "reward":0,"f2p_passed":7,"f2p_total":9,"p2p_passed":12,"p2p_total":12,
    }}})
    trial=results._trial(folder)
    assert trial["status"] == "failed"
    assert trial["failure_type"] == "VerificationFailed"
    assert "F2P 7/9" in trial["failure_message"]
    assert trial["failure_summary"].startswith("VerificationFailed:")

def test_cancelled_run_marks_only_unfinished_trials_cancelled(tmp_path: Path, monkeypatch):
    job="test-"+uuid.uuid4().hex; run_id=make_run(job)
    with SessionLocal() as db:
        row=db.get(Run,run_id)
        row.status="cancelled"
        row.error="用量护栏终止 Run"
        row.tasks_json='["task-a", "task-b"]'
        db.commit()
    failed=tmp_path/job/"task-a__failed"; failed.mkdir(parents=True)
    write_result(failed, {"task_name":"task-a","exception_info":{
        "exception_type":"RuntimeError","exception_message":"build failed",
    }})
    unfinished=tmp_path/job/"task-b__unfinished"; unfinished.mkdir(parents=True)
    (unfinished/"config.json").write_text("{}",encoding="utf-8")
    monkeypatch.setattr(runner.settings,"jobs_dir",tmp_path)
    with SessionLocal() as db:
        detail=results.run_detail(db.get(Run,run_id),include_patches=False)
    by_task={trial["task"]:trial for trial in detail["trials"]}
    assert by_task["task-a"]["status"] == "failed"
    assert by_task["task-a"]["failure_type"] == "RuntimeError"
    assert by_task["task-b"]["status"] == "cancelled"
    assert by_task["task-b"]["failure_type"] == "RunCancelled"
    assert by_task["task-b"]["failure_message"] == "用量护栏终止 Run"

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
        infrastructure_max_retries=3, agent_max_steps=140,
        codex_request_max_retries=7, codex_stream_max_retries=8,
        codex_stream_idle_timeout_seconds=900,
    ))
    try:
        assert row.job_name == f"run-{row.id:06d}-codex"
        assert runner.serialize(row)["run_code"] == f"RUN-{row.id:06d}"
        assert row.infrastructure_max_retries == 3
        assert row.agent_max_steps == 140
        assert row.codex_request_max_retries == 7
        assert row.codex_stream_max_retries == 8
        assert row.codex_stream_idle_timeout_seconds == 900
    finally:
        with SessionLocal() as db:
            saved = db.get(Run, row.id)
            if saved:
                db.delete(saved)
                db.commit()
