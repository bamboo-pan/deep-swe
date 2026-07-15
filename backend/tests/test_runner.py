import asyncio, json, subprocess, threading, time, uuid
import ipaddress
import pytest
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import delete, select
from app.database import SessionLocal, init_db
from app.models import Run, Setting, TrialQueueEntry
from app import runner
from app import results
from app.pier_retry_patch.global_queue import release_slot, try_acquire_slot
from app.pier_retry_patch.transient import (
    TransientVerifierInfrastructureError,
    is_transient_agent_failure,
    retry_transient_verifier,
)
from app.pier_retry_patch.networking import (
    DEFAULT_NETWORK_POOL, allow_provider_proxy_port, provider_proxy_domains,
    trial_network_subnets,
)
from app.pier_retry_patch.runtime import (
    install_retry_trial_names,
    install_safe_metric_display,
)
from app.schemas import (
    RunBatchDraft, RunDraft, SettingsUpdate, concurrency_advice,
)
from app.scheduler import clear_run_queue, queue_database_path, requested_trial_count

def make_run(job_name: str, **extra) -> int:
    init_db()
    with SessionLocal() as db:
        row=Run(status="running",job_name=job_name,agent="codex",model="gpt-5.6-sol",reasoning_effort="high",tasks_json='["actionlint-action-pinning-lint"]',**extra)
        db.add(row); db.commit(); db.refresh(row); return row.id

def write_result(folder: Path, payload: dict):
    folder.mkdir(parents=True, exist_ok=True)
    (folder/"result.json").write_text(json.dumps(payload), encoding="utf-8")

SUCCESS_PAYLOAD={"stats":{"n_errored_trials":0,"n_input_tokens":100,"n_cache_tokens":80,"n_output_tokens":20,"cost_usd":1.25,"evals":{"x":{"metrics":[{"reward":1.0}]}}}}
REGISTRY_EOF_MESSAGE = """Docker compose command failed for environment datacurve/task.
#3 ERROR: failed to do request: Head "https://public.ecr.aws/v2/example/manifests/v1": EOF
failed to solve: failed to resolve source metadata: failed to do request: EOF
"""

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

def test_cancel_run_removes_queue_before_pier_process_starts(monkeypatch):
    init_db()
    monkeypatch.setattr(runner, "cleanup_job_resources", lambda *args, **kwargs: {})
    with SessionLocal() as db:
        run = Run(
            status="preflight",
            job_name=f"cancel-queued-{uuid.uuid4().hex}",
            agent="codex",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            tasks_json='["task-a"]',
        )
        db.add(run)
        db.flush()
        run_id = run.id
        db.add(TrialQueueEntry(
            run_id=run_id,
            task_name="task-a",
            attempt=1,
            state="queued",
            queue_order=1,
        ))
        db.commit()
    try:
        assert runner.cancel_run(run_id) is True
        with SessionLocal() as db:
            assert db.get(Run, run_id).status == "cancelled"
            assert db.scalar(select(TrialQueueEntry).where(
                TrialQueueEntry.run_id == run_id
            )) is None
    finally:
        with runner._lock:
            runner._cancel_requested.discard(run_id)
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if run:
                db.delete(run)
                db.commit()

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

def test_global_queue_patch_handshake_fails_closed(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: "C:/pier.exe")
    monkeypatch.setattr(runner, "_pier_process_env", lambda run_id: {})
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "0.3.0\n", ""
        ),
    )
    runner._queue_patch_verified = False
    try:
        try:
            runner._verify_global_queue_patch(1)
        except RuntimeError as exc:
            assert "补丁验证失败" in str(exc)
        else:
            raise AssertionError("missing queue patch marker must fail closed")
    finally:
        runner._queue_patch_verified = False

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

def test_registry_eof_is_classified_and_summarized():
    assert is_transient_agent_failure("RuntimeError", REGISTRY_EOF_MESSAGE)
    assert "EOF" in runner._network_failure_summary(REGISTRY_EOF_MESSAGE)
    assert not is_transient_agent_failure(
        "RuntimeError", "parser failed after receiving an EOF token"
    )

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
    assert is_transient_agent_failure(
        "NonZeroAgentExitCodeError",
        "Command failed; stdout truncated",
        agent_log_tail="unexpected status 503 Service Unavailable",
    )
    assert not is_transient_agent_failure(
        "RuntimeError",
        "verifier assertion failed",
        agent_log_tail="unexpected status 503 Service Unavailable",
    )

def test_verifier_infrastructure_retry_preserves_the_agent_run():
    attempts = []
    retries = []

    async def operation():
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError(REGISTRY_EOF_MESSAGE)
        return "verified"

    result = asyncio.run(retry_transient_verifier(
        operation,
        max_retries=2,
        delays=(0,),
        on_retry=lambda number, limit, delay, exc: retries.append(
            (number, limit, delay, type(exc).__name__)
        ),
    ))

    assert result == "verified"
    assert attempts == [1, 2]
    assert retries == [(1, 2, 0.0, "RuntimeError")]

def test_verifier_infrastructure_retry_is_selective_and_bounded():
    non_transient_attempts = 0

    async def non_transient_operation():
        nonlocal non_transient_attempts
        non_transient_attempts += 1
        raise RuntimeError("application returned service unavailable")

    with pytest.raises(RuntimeError, match="service unavailable"):
        asyncio.run(retry_transient_verifier(
            non_transient_operation,
            max_retries=4,
            delays=(0,),
        ))
    assert non_transient_attempts == 1

    transient_attempts = 0

    async def transient_operation():
        nonlocal transient_attempts
        transient_attempts += 1
        raise RuntimeError(REGISTRY_EOF_MESSAGE)

    with pytest.raises(
        TransientVerifierInfrastructureError,
        match=r"after 3 attempt\(s\)",
    ):
        asyncio.run(retry_transient_verifier(
            transient_operation,
            max_retries=2,
            delays=(0,),
        ))
    assert transient_attempts == 3

def test_runtime_retry_and_agent_limits_live_in_settings():
    update=SettingsUpdate(
        max_parallel_tasks=18,
        provider_rpm=30,
        agent_timeout_seconds=7200, verifier_timeout_seconds=2400,
        infrastructure_max_retries=4, agent_max_steps=150,
    )
    draft=RunDraft(agent="claude-code", tasks=["actionlint-action-pinning-lint"],
                   codex_request_max_retries=7, codex_stream_max_retries=8,
                   codex_stream_idle_timeout_seconds=900)
    assert update.agent_timeout_seconds == 7200
    assert update.max_parallel_tasks == 18
    assert update.provider_rpm == 30
    assert update.verifier_timeout_seconds == 2400
    assert update.infrastructure_max_retries == 4
    assert update.agent_max_steps == 150
    assert "infrastructure_max_retries" not in RunDraft.model_fields
    assert "agent_max_steps" not in RunDraft.model_fields
    assert "concurrency" not in RunDraft.model_fields
    assert draft.codex_request_max_retries == 7
    assert draft.codex_stream_max_retries == 8
    assert draft.codex_stream_idle_timeout_seconds == 900

def test_requested_trial_count_and_concurrency_risk_are_separate():
    assert requested_trial_count(["task-a", "task-b", "task-c"], 2, 3) == 18
    assert concurrency_advice(18)["level"] == "warning"
    assert concurrency_advice(19)["requires_confirmation"] is True
    assert concurrency_advice(72)["level"] == "danger"
    assert concurrency_advice(73)["level"] == "blocked"


def test_provider_proxy_is_allowed_through_pier_squid():
    assert provider_proxy_domains(["api.example.com"]) == [
        "api.example.com", "host.docker.internal",
    ]
    config = allow_provider_proxy_port(
        "acl SSL_ports port 443 9887\nacl Safe_ports port 80 443 9887\n"
    )
    assert "SSL_ports port 443 8765 9887" in config
    assert "Safe_ports port 80 443 8765 9887" in config

def test_create_runs_queues_agent_group_larger_than_global_limit(monkeypatch):
    class NoopThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass

    init_db()
    with SessionLocal() as db:
        db.execute(delete(TrialQueueEntry))
        db.commit()
    monkeypatch.setattr(runner.threading, "Thread", NoopThread)
    current_preferences = runner.get_preferences()
    monkeypatch.setattr(runner, "get_preferences", lambda: {
        **current_preferences,
        "max_parallel_tasks": 2,
    })
    draft = RunBatchDraft(
        agents=["mini-swe-agent", "codex", "claude-code"],
        tasks=["task-a"],
    )
    runs, admission = runner.create_runs_with_admission(draft)
    try:
        assert admission["immediate_trials"] == 2
        assert admission["queued_trials"] == 1
        assert [run.concurrency for run in runs] == [2, 2, 2]
        with SessionLocal() as db:
            entries = db.scalars(select(TrialQueueEntry)).all()
            assert len(entries) == 3
    finally:
        with SessionLocal() as db:
            for run in runs:
                clear_run_queue(run.id, db=db)
                saved = db.get(Run, run.id)
                if saved:
                    db.delete(saved)
            db.commit()

def test_global_queue_releases_next_run_when_slot_opens():
    init_db()
    run_ids = []
    original_limit = None
    with SessionLocal() as db:
        db.execute(delete(TrialQueueEntry))
        setting = db.get(Setting, "max_parallel_tasks")
        original_limit = setting.value if setting else None
        if setting:
            setting.value = "1"
        else:
            db.add(Setting(key="max_parallel_tasks", value="1"))
        for index in range(2):
            run = Run(
                status="queued",
                job_name=f"queue-test-{uuid.uuid4().hex}-{index}",
                agent="codex",
                model="gpt-5.6-sol",
                reasoning_effort="high",
                tasks_json='["task-a"]',
            )
            db.add(run)
            db.flush()
            run_ids.append(run.id)
            db.add(TrialQueueEntry(
                run_id=run.id,
                task_name="task-a",
                attempt=1,
                state="queued",
                queue_order=index + 1,
            ))
        db.commit()

    database = queue_database_path()
    acquired: dict[str, int | Exception] = {}

    def acquire_second():
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                entry = try_acquire_slot(
                    database, run_ids[1], "task-a", "task-a__second", 1
                )
                if entry is not None:
                    acquired["entry"] = entry
                    return
                time.sleep(0.02)
            acquired["entry"] = TimeoutError("second Trial never acquired a slot")
        except Exception as exc:
            acquired["entry"] = exc

    waiter = threading.Thread(target=acquire_second, daemon=True)
    waiter.start()
    time.sleep(0.15)
    # 后提交的 Run 即使先进入调度代码，也不能越过更早的队列项。
    assert "entry" not in acquired
    first_entry = try_acquire_slot(
        database, run_ids[0], "task-a", "task-a__first", 1
    )
    assert first_entry is not None
    time.sleep(0.1)
    assert "entry" not in acquired
    release_slot(database, run_ids[0], first_entry)
    waiter.join(timeout=3)
    try:
        assert isinstance(acquired.get("entry"), int), acquired.get("entry")
    finally:
        second_entry = acquired.get("entry")
        if isinstance(second_entry, int):
            release_slot(database, run_ids[1], second_entry)
        with SessionLocal() as db:
            db.execute(delete(TrialQueueEntry))
            for run_id in run_ids:
                run = db.get(Run, run_id)
                if run:
                    db.delete(run)
            setting = db.get(Setting, "max_parallel_tasks")
            if original_limit is None:
                if setting:
                    db.delete(setting)
            elif setting:
                setting.value = original_limit
            else:
                db.add(Setting(key="max_parallel_tasks", value=original_limit))
            db.commit()


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

def test_verifier_failure_is_not_reclassified_from_stale_agent_log(tmp_path: Path):
    folder=tmp_path/"task-a__x"; (folder/"agent").mkdir(parents=True)
    write_result(folder, {"task_name":"task-a","exception_info":{
        "exception_type":"RuntimeError",
        "exception_message":"verifier failed because the Dockerfile is invalid",
    }})
    (folder/"agent"/"mini-swe-agent.txt").write_text(
        "earlier request: unexpected status 503 Service Unavailable",
        encoding="utf-8",
    )

    assert results._trial(folder)["failure_type"] == "RuntimeError"
    assert runner._run_failure_summary(tmp_path).startswith("RuntimeError:")

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

def test_frontier_regression_threshold_scales_to_sixteen_trial_baseline():
    tasks = ["task-a", "task-b", "task-c", results.CONTROL_TASK]
    baseline_trials = [
        {"task": task, "reward": 1, "duration_seconds": 60.0}
        for task in tasks
        for _ in range(4)
    ]
    current_trials = [dict(trial) for trial in baseline_trials]
    current_trials[0]["reward"] = 0
    current_trials[4]["reward"] = 0
    baseline = {"progress": {"total": 16, "passed": 16}, "trials": baseline_trials}
    current = {"progress": {"total": 16, "passed": 14}, "trials": current_trials}

    reasons = results._regression_reasons(current, baseline)

    assert any("总通过率下降 12.5 个百分点" in reason for reason in reasons)

def test_frontier_control_task_collapse_is_reported():
    task = results.CONTROL_TASK
    baseline_trials = [
        {"task": task, "reward": 1, "duration_seconds": 60.0}
        for _ in range(4)
    ]
    current_trials = [dict(trial) for trial in baseline_trials]
    for trial in current_trials[:3]:
        trial["reward"] = 0
    baseline = {"progress": {"total": 4, "passed": 4}, "trials": baseline_trials}
    current = {"progress": {"total": 4, "passed": 1}, "trials": current_trials}

    reasons = results._regression_reasons(current, baseline)

    assert any(task in reason and "1/4" in reason for reason in reasons)

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

def test_preflight_prepares_local_images_after_connectivity(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner.settings, "tasks_dir", tmp_path)
    (tmp_path / "t1").mkdir()
    calls = []
    monkeypatch.setattr(runner, "docker_available", lambda: (True, "ok"))
    monkeypatch.setattr(
        runner, "_docker_proxy_connectivity", lambda url: calls.append(("proxy", url))
    )
    monkeypatch.setattr(
        runner,
        "ensure_local_task_images",
        lambda tasks_dir, tasks, **kwargs: calls.append(
            ("images", tasks_dir, tasks, kwargs["log_dir"])
        ),
    )
    runner._preflight(["t1"], "http://127.0.0.1:9887/v1")
    assert calls[0] == ("proxy", "http://127.0.0.1:9887/v1")
    assert calls[1] == (
        "images",
        tmp_path,
        ["t1"],
        tmp_path.parent / "data" / "local-image-builds",
    )

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

def test_trial_stage_progresses_through_verifier_markers(tmp_path: Path):
    folder = tmp_path / "task-a__x1"
    assert results._trial_stage(folder, {}) == "queued"

    folder.mkdir(parents=True)
    (folder / "config.json").write_text(
        json.dumps({"verifier": {"disable": False}}), encoding="utf-8"
    )
    assert results._trial_stage(folder, {}) == "preparing_environment"

    (folder / "agent").mkdir()
    (folder / "agent" / "trajectory.json").write_text("{}", encoding="utf-8")
    assert results._trial_stage(folder, {}) == "agent_running"

    (folder / "artifacts").mkdir()
    (folder / "artifacts" / "model.patch").write_text("diff", encoding="utf-8")
    assert results._trial_stage(folder, {}) == "preparing_verifier"

    (folder / "verifier").mkdir()
    (folder / "verifier" / "run.log").write_text("running", encoding="utf-8")
    assert results._trial_stage(folder, {}) == "verifier"

    (folder / "verifier" / "reward.json").write_text("{}", encoding="utf-8")
    assert results._trial_stage(folder, {}) == "finalizing"

    final_result = {"task_name": "task-a", "verifier_result": {"rewards": {"reward": 1}}}
    write_result(folder, final_result)
    assert results._trial_stage(folder, final_result) == "completed"

def test_disabled_verifier_moves_to_finalizing_after_patch(tmp_path: Path):
    folder = tmp_path / "task-a__x1"
    (folder / "artifacts").mkdir(parents=True)
    (folder / "artifacts" / "model.patch").write_text("diff", encoding="utf-8")
    (folder / "config.json").write_text(
        json.dumps({"verifier": {"disable": True}}), encoding="utf-8"
    )
    assert results._trial_stage(folder, {}) == "finalizing"

def test_empty_patch_from_cost_limit_is_not_reported_as_verification_failure(tmp_path: Path):
    folder=tmp_path/"task-a__x1"
    (folder/"agent").mkdir(parents=True)
    (folder/"artifacts").mkdir()
    (folder/"artifacts"/"model.patch").write_text("", encoding="utf-8")
    (folder/"agent"/"mini-swe-agent.txt").write_text(
        "working\nExit:\nLimitsExceeded\nSaved trajectory\n", encoding="utf-8"
    )
    write_result(folder, {
        "task_name":"task-a",
        "config":{"agent":{"kwargs":{"cost_limit":10.0}}},
        "agent_result":{"cost_usd":10.05},
        "verifier_result":{"rewards":{
            "reward":0,"f2p_passed":0,"f2p_total":24,"p2p_passed":2,"p2p_total":2,
        }},
    })
    trial=results._trial(folder)
    assert trial["failure_type"] == "CostLimitExceeded"
    assert "$10.05 / $10.00" in trial["failure_message"]
    assert "model.patch" in trial["failure_message"]
    assert "目标失败测试修复（F2P） 0/24" in trial["failure_message"]
    assert "原有通过测试保持（P2P） 2/2" in trial["failure_message"]
    assert trial["patch_bytes"] == 0

def test_empty_patch_from_step_limit_has_readable_reason(tmp_path: Path):
    folder=tmp_path/"task-a__x1"
    (folder/"agent").mkdir(parents=True)
    (folder/"artifacts").mkdir()
    (folder/"artifacts"/"model.patch").write_text("", encoding="utf-8")
    (folder/"agent"/"mini-swe-agent.txt").write_text(
        "working\nExit:\nLimitsExceeded\nSaved trajectory\n", encoding="utf-8"
    )
    write_result(folder, {
        "task_name":"task-a",
        "config":{"agent":{"kwargs":{"cost_limit":20.0}}},
        "agent_result":{"cost_usd":17.004086,"n_agent_steps":120},
        "verifier_result":{"rewards":{
            "reward":0,"f2p_passed":0,"f2p_total":55,"p2p_passed":145,"p2p_total":145,
        }},
    })
    trial=results._trial(folder)
    assert trial["failure_type"] == "AgentLimitExceeded"
    assert "已执行 120 步" in trial["failure_message"]
    assert "达到设置的步数上限后自动停止" in trial["failure_message"]
    assert "目标失败测试修复（F2P） 0/55" in trial["failure_message"]
    assert "原有通过测试保持（P2P） 145/145" in trial["failure_message"]

def test_limit_marker_does_not_hide_failure_of_submitted_patch(tmp_path: Path):
    folder=tmp_path/"task-a__x1"
    (folder/"agent").mkdir(parents=True)
    (folder/"artifacts").mkdir()
    (folder/"artifacts"/"model.patch").write_text("non-empty", encoding="utf-8")
    (folder/"agent"/"mini-swe-agent.txt").write_text(
        "Exit:\nLimitsExceeded\n", encoding="utf-8"
    )
    write_result(folder, {"task_name":"task-a","verifier_result":{"rewards":{
        "reward":0,"f2p_passed":0,"f2p_total":2,"p2p_passed":1,"p2p_total":1,
    }}})
    trial=results._trial(folder)
    assert trial["failure_type"] == "VerificationFailed"
    assert trial["patch_bytes"] == 9

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
    current_preferences = runner.get_preferences()
    monkeypatch.setattr(runner, "get_preferences", lambda: {
        **current_preferences,
        "max_parallel_tasks": 5,
        "agent_timeout_seconds": 7200,
        "verifier_timeout_seconds": 2400,
        "infrastructure_max_retries": 3,
        "agent_max_steps": 140,
    })
    row = runner.create_run(RunDraft(
        agent="codex", tasks=["actionlint-action-pinning-lint"],
        codex_request_max_retries=7, codex_stream_max_retries=8,
        codex_stream_idle_timeout_seconds=900,
    ))
    try:
        assert row.job_name == f"run-{row.id:06d}-codex"
        assert row.concurrency == 5
        assert runner.serialize(row)["run_code"] == f"RUN-{row.id:06d}"
        assert row.agent_timeout_seconds == 7200
        assert row.verifier_timeout_seconds == 2400
        assert row.infrastructure_max_retries == 3
        assert row.agent_max_steps == 140
        assert row.codex_request_max_retries == 7
        assert row.codex_stream_max_retries == 8
        assert row.codex_stream_idle_timeout_seconds == 900
    finally:
        with SessionLocal() as db:
            clear_run_queue(row.id, db=db)
            saved = db.get(Run, row.id)
            if saved:
                db.delete(saved)
                db.commit()

def test_atomic_json_write_retries_windows_replace_contention(tmp_path: Path, monkeypatch):
    path = tmp_path / "retry-state.json"
    original_replace = Path.replace
    attempts = 0

    def flaky_replace(source, target):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(5, "destination temporarily in use")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr(runner.time, "sleep", lambda _delay: None)

    runner._write_json_atomic(path, {"status": "preflight"})

    assert attempts == 3
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "preflight"}
    assert list(tmp_path.glob("*.tmp")) == []

def test_retry_marker_state_write_failure_does_not_abort_execution(tmp_path: Path, monkeypatch):
    task = "actionlint-action-pinning-lint"
    target_id = f"{task}__locked1"
    marker = tmp_path / target_id
    marker.mkdir()
    spec = {
        "trial_id": target_id,
        "target_id": target_id,
        "task": task,
        "attempt": 1,
        "task_config": {"path": str(runner.settings.tasks_dir / task), "source": "tasks"},
    }
    (marker / "config.json").write_text(
        json.dumps(runner._retry_marker_config(tmp_path, spec)), encoding="utf-8"
    )
    monkeypatch.setattr(
        runner,
        "_write_json_atomic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError(5, "locked")),
    )

    failures = runner._set_retry_marker_state(tmp_path, [spec], "preflight")

    assert len(failures) == 1
    assert target_id in failures[0]

def test_pier_retry_runtime_assigns_deterministic_trial_names():
    class FakeJob:
        def __init__(self, job_name):
            self.config = SimpleNamespace(job_name=job_name)
            self._trial_configs = []

        def _init_trial_configs(self):
            self._trial_configs = [
                SimpleNamespace(trial_name="random-a"),
                SimpleNamespace(trial_name="random-b"),
            ]

    names = ["task-a__batch0001", "task-a__batch0002"]
    install_retry_trial_names(FakeJob, names, "retry-run-1-batch")

    retry_job = FakeJob("retry-run-1-batch")
    retry_job._init_trial_configs()
    other_job = FakeJob("run-2")
    other_job._init_trial_configs()

    assert [item.trial_name for item in retry_job._trial_configs] == names
    assert [item.trial_name for item in other_job._trial_configs] == [
        "random-a", "random-b"
    ]

def test_pier_metric_display_guard_never_aborts_trials():
    event = SimpleNamespace(
        config=SimpleNamespace(task=SimpleNamespace(source="tasks"))
    )

    class EmptyMetricJob:
        def __init__(self):
            self._metrics = {"tasks": []}
            self.called = False

        def _update_metric_display(self, *_args):
            self.called = True
            raise IndexError("empty metric list")

    install_safe_metric_display(EmptyMetricJob)
    empty = EmptyMetricJob()
    empty._update_metric_display(event, None, None)
    assert empty.called is False

    class BrokenMetricJob:
        def __init__(self):
            self._metrics = {"tasks": [object()]}

        def _update_metric_display(self, *_args):
            raise ValueError("presentation-only metric failure")

    install_safe_metric_display(BrokenMetricJob)
    BrokenMetricJob()._update_metric_display(event, None, None)

def test_retry_batch_identities_isolate_runs_and_serialise_each_run(tmp_path: Path):
    first_batch = "d" * 32
    second_batch = "e" * 32
    first_run = Run(job_name="run-000201-mini-swe-agent")
    second_run = Run(job_name="run-000202-mini-swe-agent")
    base_spec = {
        "trial_id": "task__old",
        "target_id": "task__old",
        "task": "task",
        "attempt": 1,
        "task_config": {"path": "C:/tasks/task", "source": "tasks"},
    }
    first = runner._bind_retry_specs(first_run, [base_spec], first_batch)[0]
    second = runner._bind_retry_specs(second_run, [base_spec], second_batch)[0]

    assert first["retry_job_name"] != second["retry_job_name"]
    assert first["runtime_trial_id"] != second["runtime_trial_id"]
    own_dir = tmp_path / first["retry_job_name"]
    other_prefix_dir = tmp_path / runner.retry_job_name(
        f"{first_run.job_name}-other", second_batch
    )
    own_dir.mkdir()
    other_prefix_dir.mkdir()
    (tmp_path / f"{first['retry_job_name']}.supervisor.log").write_text(
        "log", encoding="utf-8"
    )
    assert runner.retry_job_dirs(tmp_path, first_run.job_name) == [own_dir]
    assert first["retry_job_name"] in runner.retry_job_names(
        tmp_path, first_run.job_name
    )
    assert runner._reserve_retry_batch(201, first_batch) is True
    assert runner._reserve_retry_batch(201, second_batch) is False
    assert runner._reserve_retry_batch(202, second_batch) is True
    try:
        assert runner._retrying[201] == first_batch
        assert runner._retrying[202] == second_batch
    finally:
        runner._release_retry_batch(201, first_batch)
        runner._release_retry_batch(202, second_batch)

def test_stale_retry_batch_cannot_overwrite_new_marker(tmp_path: Path):
    target_id = "task__target"
    old_batch = "1" * 32
    new_batch = "2" * 32
    base = {
        "trial_id": target_id,
        "target_id": target_id,
        "task": "task",
        "attempt": 1,
        "task_config": {"path": "C:/tasks/task", "source": "tasks"},
    }
    old_spec = {
        **base,
        "retry_batch_id": old_batch,
        "retry_job_name": runner.retry_job_name("run-1", old_batch),
        "runtime_trial_id": "task__old-runtime",
    }
    new_spec = {
        **base,
        "retry_batch_id": new_batch,
        "retry_job_name": runner.retry_job_name("run-1", new_batch),
        "runtime_trial_id": "task__new-runtime",
    }
    marker = tmp_path / target_id
    marker.mkdir()
    (marker / "config.json").write_text(
        json.dumps(runner._retry_marker_config(tmp_path, new_spec)),
        encoding="utf-8",
    )
    initial_state = {"status": "queued", "batch_id": new_batch}
    (marker / "retry-state.json").write_text(
        json.dumps(initial_state), encoding="utf-8"
    )

    runner._set_retry_marker_state(tmp_path, [old_spec], "failed", "old batch")

    assert json.loads((marker / "retry-state.json").read_text(encoding="utf-8")) == initial_state

def test_stale_retry_finalizer_does_not_remove_new_batch_process():
    run_id = 303
    old_batch = "3" * 32
    new_batch = "4" * 32
    old_proc = object()
    new_proc = object()
    with runner._lock:
        runner._retrying[run_id] = new_batch
        runner._processes[run_id] = new_proc
    try:
        runner._release_retry_batch(run_id, old_batch, old_proc)
        assert runner._retrying[run_id] == new_batch
        assert runner._processes[run_id] is new_proc
    finally:
        with runner._lock:
            runner._retrying.pop(run_id, None)
            runner._processes.pop(run_id, None)

def test_retry_config_uses_latest_runtime_settings_and_expands_duplicate_tasks(tmp_path: Path, monkeypatch):
    init_db()
    job = "run-000123-mini-swe-agent"
    job_dir = tmp_path / job
    job_dir.mkdir()
    original = {
        "job_name": job,
        "jobs_dir": str(tmp_path),
        "n_attempts": 2,
        "n_concurrent_trials": 17,
        "retry": {"max_retries": 3, "include_exceptions": ["TransientAgentInfrastructureError"]},
        "environment": {"type": "docker", "delete": True},
        "verifier": {"disable": False},
        "metrics": [],
        "agents": [{
            "name": "mini-swe-agent",
            "model_name": "openai/gpt-5.6-sol",
            "kwargs": {"config_file": "stale.yaml", "cost_limit": 9.5},
            "env": {},
        }],
        "datasets": [{"path": str(runner.settings.tasks_dir)}],
        "tasks": [],
    }
    (job_dir / "config.json").write_text(json.dumps(original), encoding="utf-8")
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    auth = secret_dir / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    run = Run(
        job_name=job, jobs_dir=str(tmp_path), status="completed",
        agent="mini-swe-agent", model="gpt-5.6-sol", reasoning_effort="max",
        tasks_json='["actionlint-action-pinning-lint"]',
        agent_timeout_seconds=7200, verifier_timeout_seconds=2700,
        infrastructure_max_retries=1, agent_max_steps=155,
    )
    monkeypatch.setattr(runner, "_declared_timeouts", lambda _tasks: (3600.0, 900.0))
    task_config = {"path": str(runner.settings.tasks_dir / "actionlint-action-pinning-lint"), "source": "tasks"}
    specs = runner._bind_retry_specs(run, [
        {"trial_id": "old-a", "target_id": "task__old-a", "attempt": 1, "task": "actionlint-action-pinning-lint", "task_config": task_config},
        {"trial_id": "old-b", "target_id": "task__old-b", "attempt": 2, "task": "actionlint-action-pinning-lint", "task_config": task_config},
    ], "a" * 32)

    path, env = runner._prepare_retry_config(
        run, specs, SimpleNamespace(url="http://127.0.0.1:9887/v1", token="secret"),
        secret_dir, auth, tmp_path,
        {"trial_budget_usd": 3.25, "max_parallel_tasks": 17},
    )
    retry = json.loads(path.read_text(encoding="utf-8"))

    assert retry["job_name"] == runner.retry_job_name(job, "a" * 32)
    assert retry["n_attempts"] == 1
    assert retry["n_concurrent_trials"] == 2
    assert retry["agent_timeout_multiplier"] == 2
    assert retry["verifier_timeout_multiplier"] == 3
    assert retry["retry"]["max_retries"] == 1
    assert retry["retry"]["include_exceptions"] == ["TransientAgentInfrastructureError"]
    assert retry["datasets"] == []
    assert retry["tasks"] == [
        {**task_config, "source": None},
        {**task_config, "source": None},
    ]
    assert retry["agents"][0]["kwargs"]["cost_limit"] == 3.25
    assert retry["agents"][0]["kwargs"]["config_file"].endswith("mini-limits.yaml")
    assert "step_limit: 155" in (secret_dir / "mini-limits.yaml").read_text(encoding="utf-8")
    assert env["OPENAI_API_KEY"] == "secret"
    assert env["DEEPSWE_VERIFIER_INFRA_MAX_RETRIES"] == "1"
    assert env["DEEPSWE_RETRY_JOB_NAME"] == retry["job_name"]
    assert json.loads(env["DEEPSWE_RETRY_TRIAL_NAMES"]) == [
        spec["runtime_trial_id"] for spec in specs
    ]

    run.infrastructure_max_retries = 0
    path, disabled_env = runner._prepare_retry_config(
        run, specs, SimpleNamespace(url="http://127.0.0.1:9887/v1", token="secret"),
        secret_dir, auth, tmp_path, {"trial_budget_usd": 0},
    )
    disabled = json.loads(path.read_text(encoding="utf-8"))
    assert disabled_env["DEEPSWE_VERIFIER_INFRA_MAX_RETRIES"] == "0"
    assert disabled["retry"]["max_retries"] == 0
    assert disabled["retry"]["include_exceptions"] == []
    assert "cost_limit" not in disabled["agents"][0]["kwargs"]

def test_retry_merge_replaces_markers_and_rewrites_identity(tmp_path: Path):
    task = "actionlint-action-pinning-lint"
    job = "run-000124-mini-swe-agent"
    batch_id = "b" * 32
    original_dir = tmp_path / job
    original_dir.mkdir()
    (original_dir / "result.json").write_text(json.dumps({"id": "original-job-id"}), encoding="utf-8")
    retry_dir = tmp_path / runner.retry_job_name(job, batch_id)
    retry_dir.mkdir()
    supervisor = tmp_path / f"{retry_dir.name}.supervisor.log"
    supervisor.write_text("retry log", encoding="utf-8")
    run = Run(
        id=124, job_name=job, jobs_dir=str(tmp_path), status="completed",
        agent="mini-swe-agent", model="gpt-5.6-sol", reasoning_effort="max",
        tasks_json=json.dumps([task]), attempts_per_task=2,
    )
    specs = []
    runtime_names = (
        f"{task}__runtime-z",
        f"{task}__runtime-a",
    )
    for index, (suffix, name) in enumerate(
        zip(("olda001", "oldb002"), runtime_names), start=1
    ):
        source_id = f"{task}__{suffix}"
        folder = retry_dir / name
        folder.mkdir()
        task_config = {"path": str(runner.settings.tasks_dir / task), "source": "tasks"}
        trial_config = {"task": task_config, "trial_name": name, "trials_dir": str(retry_dir), "job_id": "retry-job-id"}
        (folder / "config.json").write_text(json.dumps(trial_config), encoding="utf-8")
        (folder / "result.json").write_text(json.dumps({
            "trial_name": name,
            "task_name": f"datacurve/{task}",
            "trial_uri": folder.resolve().as_uri(),
            "config": trial_config,
            "agent_result": {"n_input_tokens": index * 10},
            "verifier_result": {"rewards": {"reward": index - 1}},
        }), encoding="utf-8")
        spec = {
            "trial_id": source_id, "target_id": source_id, "task": task,
            "attempt": index, "task_config": task_config,
            "retry_batch_id": batch_id,
            "retry_job_name": retry_dir.name,
            "runtime_trial_id": name,
        }
        marker = original_dir / source_id
        marker.mkdir()
        (marker / "config.json").write_text(
            json.dumps(runner._retry_marker_config(original_dir, spec)), encoding="utf-8"
        )
        (marker / "retry-state.json").write_text(json.dumps({"status": "running"}), encoding="utf-8")
        specs.append(spec)
    (retry_dir / "job.log").write_text("pier retry", encoding="utf-8")

    moved = runner._merge_retry_trials(run, retry_dir, specs, supervisor)

    assert set(moved) == {spec["target_id"] for spec in specs} and not retry_dir.exists()
    assert sorted(path.name for path in original_dir.iterdir() if "__" in path.name) == sorted(moved)
    merged = [
        json.loads((original_dir / spec["target_id"] / "result.json").read_text(encoding="utf-8"))
        for spec in specs
    ]
    assert all("deepswe_retry_of" not in item for item in merged)
    assert [item["deepswe_attempt"] for item in merged] == [1, 2]
    assert all(item["deepswe_replaced"] is True for item in merged)
    assert [item["trial_name"] for item in merged] == [
        spec["target_id"] for spec in specs
    ]
    assert all(item["config"]["trials_dir"] == str(original_dir) for item in merged)
    assert all(item["config"]["job_id"] == "original-job-id" for item in merged)
    assert all(item["trial_uri"].startswith(original_dir.resolve().as_uri()) for item in merged)
    assert [item["verifier_result"]["rewards"]["reward"] for item in merged] == [0, 1]
    assert [item["deepswe_retry_resource_id"] for item in merged] == list(runtime_names)
    assert list((original_dir / ".retry-logs").glob("*/retry.json"))

def test_retry_results_keep_progress_size_and_replace_visible_usage_totals(tmp_path: Path):
    init_db()
    task = "actionlint-action-pinning-lint"
    job = "run-000125-mini-swe-agent"
    job_dir = tmp_path / job
    job_dir.mkdir()
    (job_dir / "result.json").write_text(json.dumps({
        "stats": {"n_input_tokens": 10, "n_cache_tokens": 1, "n_output_tokens": 2, "cost_usd": 0.1}
    }), encoding="utf-8")
    folder = job_dir / "actionlint-action-pinning-lint__try001"
    write_result(folder, {
        "task_name": task,
        "deepswe_attempt": 1,
        "deepswe_replaced": True,
        "started_at": "2026-07-13T01:00:00Z",
        "finished_at": "2026-07-13T01:01:00Z",
        "agent_result": {
            "n_input_tokens": 200,
            "n_cache_tokens": 20,
            "n_output_tokens": 10,
            "cost_usd": 1.0,
        },
        "verifier_result": {"rewards": {"reward": 1}},
    })
    run = Run(
        id=125, job_name=job, jobs_dir=str(tmp_path), status="completed",
        agent="mini-swe-agent", model="gpt-5.6-sol", reasoning_effort="max",
        tasks_json=json.dumps([task]), attempts_per_task=1,
    )

    detail = results.run_detail(run, include_patches=False)
    aggregate = results.aggregate_trial_results(run)

    assert detail["progress"] == {"completed": 1, "total": 1, "passed": 1, "percent": 100}
    assert [trial["attempt"] for trial in detail["trials"]] == [1]
    assert detail["input_tokens"] == 200 and detail["reported_cost_usd"] == 1.0
    assert results.run_trial_progress(run)["total"] == 1
    assert aggregate["reward"] == 1 and aggregate["passed"] is True

def test_retry_marker_keeps_identity_while_overlaying_live_stage(tmp_path: Path):
    init_db()
    task = "actionlint-action-pinning-lint"
    job = "run-000127-mini-swe-agent"
    batch_id = "c" * 32
    retry_name = runner.retry_job_name(job, batch_id)
    original_dir = tmp_path / job
    original_dir.mkdir()
    first = original_dir / f"{task}__first01"
    write_result(first, {
        "task_name": task,
        "started_at": "2026-07-13T01:00:00Z",
        "verifier_result": {"rewards": {"reward": 1}},
    })
    target_id = f"{task}__second2"
    task_config = {"path": str(runner.settings.tasks_dir / task), "source": "tasks"}
    spec = {
        "trial_id": target_id, "target_id": target_id, "task": task,
        "attempt": 2, "task_config": task_config,
        "retry_batch_id": batch_id,
        "retry_job_name": retry_name,
        "runtime_trial_id": f"{task}__runtime-live",
    }
    marker = original_dir / target_id
    marker.mkdir()
    (marker / "config.json").write_text(
        json.dumps(runner._retry_marker_config(original_dir, spec)), encoding="utf-8"
    )
    (marker / "retry-state.json").write_text(
        json.dumps({"status": "preflight"}), encoding="utf-8"
    )
    live = tmp_path / retry_name / spec["runtime_trial_id"]
    (live / "agent").mkdir(parents=True)
    (live / "config.json").write_text(json.dumps({"task": task_config}), encoding="utf-8")
    (live / "agent" / "mini-swe-agent.txt").write_text("working", encoding="utf-8")
    run = Run(
        id=127, job_name=job, jobs_dir=str(tmp_path), status="running",
        agent="mini-swe-agent", model="gpt-5.6-sol", reasoning_effort="max",
        tasks_json=json.dumps([task]), attempts_per_task=2,
    )

    detail = results.run_detail(run, include_patches=False)
    retried = next(trial for trial in detail["trials"] if trial["id"] == target_id)

    assert retried["attempt"] == 2
    assert retried["status"] == "agent_running"
    assert retried["retrying"] is True and retried["replaced"] is True
    assert detail["progress"]["total"] == 2
    assert results.trial_log(run, target_id) == "working"
    live_detail = results.trial_detail(run, target_id)
    assert live_detail["id"] == target_id
    assert live_detail["status"] == "agent_running"
    assert live_detail["resource_name"] == live.name

def test_retry_submission_deletes_selected_result_and_creates_marker(tmp_path: Path, monkeypatch):
    class NoopThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass

    init_db()
    task = "actionlint-action-pinning-lint"
    job = f"retry-submit-{uuid.uuid4().hex}"
    original_dir = tmp_path / job
    first_id = f"{task}__first01"
    second_id = f"{task}__second2"
    for index, trial_id in enumerate((first_id, second_id), start=1):
        write_result(original_dir / trial_id, {
            "task_name": task,
            "started_at": f"2026-07-13T0{index}:00:00Z",
            "agent_result": {"cost_usd": float(index)},
            "verifier_result": {"rewards": {"reward": index - 1}},
        })
    with SessionLocal() as db:
        run = Run(
            status="completed", job_name=job, jobs_dir=str(tmp_path),
            agent="mini-swe-agent", model="gpt-5.6-sol", reasoning_effort="max",
            tasks_json=json.dumps([task]), attempts_per_task=2,
        )
        db.add(run); db.commit(); db.refresh(run); run_id = run.id
    current_preferences = runner.get_preferences()
    monkeypatch.setattr(runner, "get_preferences", lambda: {
        **current_preferences,
        "run_budget_usd": 100,
        "docker_cleanup_after_run": False,
    })
    monkeypatch.setattr(runner.threading, "Thread", NoopThread)
    response = None
    try:
        response = runner.retry_trials(run_id, [first_id])

        assert response["retry_count"] == 1
        assert not (original_dir / first_id / "result.json").exists()
        marker_config = json.loads((original_dir / first_id / "config.json").read_text(encoding="utf-8"))
        marker_state = json.loads((original_dir / first_id / "retry-state.json").read_text(encoding="utf-8"))
        assert marker_config["deepswe_attempt"] == 1
        assert marker_config["deepswe_retrying"] is True
        assert marker_config["deepswe_retry_batch"] == response["batch_id"]
        assert marker_config["deepswe_retry_job_name"] == response["retry_job_name"]
        assert marker_config["deepswe_retry_resource_id"]
        assert marker_state["status"] == "queued"
        assert marker_state["batch_id"] == response["batch_id"]
        assert (original_dir / second_id / "result.json").exists()
        metadata = json.loads(
            (tmp_path / response["retry_job_name"] / ".deepswe-retry.json").read_text(encoding="utf-8")
        )
        assert metadata["specs"][0]["target_id"] == first_id
        assert metadata["run_id"] == run_id
        assert metadata["batch_id"] == response["batch_id"]
        assert metadata["retry_job_name"] == response["retry_job_name"]
        with SessionLocal() as db:
            saved = db.get(Run, run_id)
            assert saved.status == "queued"
            assert saved.cost_usd == 2.0
    finally:
        if response:
            runner._release_retry_batch(run_id, response["batch_id"])
        with SessionLocal() as db:
            saved = db.get(Run, run_id)
            if saved:
                db.delete(saved)
                db.commit()

def test_retry_submissions_for_multiple_runs_keep_separate_batches(tmp_path: Path, monkeypatch):
    class NoopThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass

    init_db()
    task = "actionlint-action-pinning-lint"
    created = []
    for suffix in ("one", "two"):
        job = f"multi-retry-{suffix}-{uuid.uuid4().hex}"
        trial_id = f"{task}__{suffix}001"
        write_result(tmp_path / job / trial_id, {
            "task_name": task,
            "agent_result": {"cost_usd": 1.0},
            "verifier_result": {"rewards": {"reward": 0}},
        })
        with SessionLocal() as db:
            run = Run(
                status="completed", job_name=job, jobs_dir=str(tmp_path),
                agent="mini-swe-agent", model="gpt-5.6-sol", reasoning_effort="max",
                tasks_json=json.dumps([task]), attempts_per_task=1,
            )
            db.add(run); db.commit(); db.refresh(run)
            created.append((run.id, job, trial_id))
    current_preferences = runner.get_preferences()
    monkeypatch.setattr(runner, "get_preferences", lambda: {
        **current_preferences,
        "run_budget_usd": 100,
        "docker_cleanup_after_run": False,
    })
    monkeypatch.setattr(runner.threading, "Thread", NoopThread)
    responses = []
    try:
        for run_id, _job, trial_id in created:
            responses.append(runner.retry_trials(run_id, [trial_id]))

        assert responses[0]["batch_id"] != responses[1]["batch_id"]
        assert responses[0]["retry_job_name"] != responses[1]["retry_job_name"]
        for response, (run_id, job, _trial_id) in zip(responses, created):
            retry_dir = tmp_path / response["retry_job_name"]
            assert runner.retry_job_dirs(tmp_path, job) == [retry_dir]
            metadata = json.loads(
                (retry_dir / ".deepswe-retry.json").read_text(encoding="utf-8")
            )
            assert metadata["run_id"] == run_id
            assert metadata["batch_id"] == response["batch_id"]
    finally:
        for response, (run_id, _job, _trial_id) in zip(responses, created):
            runner._release_retry_batch(run_id, response["batch_id"])
        with SessionLocal() as db:
            for run_id, _job, _trial_id in created:
                saved = db.get(Run, run_id)
                if saved:
                    db.delete(saved)
            db.commit()

def test_retry_budget_rejection_happens_before_selected_result_is_deleted(tmp_path: Path, monkeypatch):
    class NoopThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass

    init_db()
    task = "actionlint-action-pinning-lint"
    job = f"retry-budget-{uuid.uuid4().hex}"
    original_dir = tmp_path / job
    selected_id = f"{task}__selected"
    retained_id = f"{task}__retained"
    for trial_id, cost in ((selected_id, 1.0), (retained_id, 3.0)):
        write_result(original_dir / trial_id, {
            "task_name": task,
            "agent_result": {"cost_usd": cost},
            "verifier_result": {"rewards": {"reward": 0}},
        })
    with SessionLocal() as db:
        run = Run(
            status="completed", job_name=job, jobs_dir=str(tmp_path),
            agent="mini-swe-agent", model="gpt-5.6-sol", reasoning_effort="max",
            tasks_json=json.dumps([task]), attempts_per_task=2,
        )
        db.add(run); db.commit(); db.refresh(run); run_id = run.id
    current_preferences = runner.get_preferences()
    monkeypatch.setattr(runner, "get_preferences", lambda: {
        **current_preferences, "run_budget_usd": 3.0,
    })
    monkeypatch.setattr(runner.threading, "Thread", NoopThread)
    try:
        try:
            runner.retry_trials(run_id, [selected_id])
            assert False, "retained cost at the Run limit must reject retry"
        except RuntimeError as exc:
            assert "保留 Trial" in str(exc)
        assert (original_dir / selected_id / "result.json").exists()
        assert runner.retry_job_dirs(tmp_path, job) == []
    finally:
        with SessionLocal() as db:
            saved = db.get(Run, run_id)
            if saved:
                db.delete(saved)
                db.commit()

def test_retry_supersedes_only_its_source_execution_error(tmp_path: Path):
    task = "actionlint-action-pinning-lint"
    job = "run-000126-mini-swe-agent"
    job_dir = tmp_path / job
    job_dir.mkdir()
    payloads = {
        "old-error": {"exception_info": {"exception_type": "AgentTimeoutError"}},
        "other-error": {"exception_info": {"exception_type": "RuntimeError"}},
        "retry-success": {
            "deepswe_retry_of": f"{task}__old-error",
            "verifier_result": {"rewards": {"reward": 1}},
        },
    }
    for name, extra in payloads.items():
        write_result(job_dir / f"{task}__{name}", {"task_name": task, **extra})
    run = Run(
        id=126, job_name=job, jobs_dir=str(tmp_path), status="completed",
        agent="mini-swe-agent", model="gpt-5.6-sol", reasoning_effort="max",
        tasks_json=json.dumps([task]), attempts_per_task=2,
    )

    aggregate = results.aggregate_trial_results(run)

    assert aggregate["errored"] == 2
    assert aggregate["effective_errored"] == 1
    assert aggregate["missing_configured"] == 0
