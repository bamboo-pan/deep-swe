"""官方任务统计聚合与合并测试。"""
import json
from app import official_stats
from app.official_stats import aggregate_trials, configuration_stats, load_official_stats

def test_aggregate_matches_official_site_semantics():
    rows = [
        {"task_name": "a", "passed": True, "trial_duration_seconds": 60.0},
        {"task_name": "a", "passed": False, "trial_duration_seconds": 120.0},
        {"task_name": "a", "passed": True, "trial_duration_seconds": None},  # 无耗时不进均值但计入通过率
        {"task_name": "b", "passed": False, "trial_duration_seconds": 30.0},
        {"task_name": None, "passed": True},  # 脏行忽略
    ]
    stats = aggregate_trials(rows)
    assert stats["a"] == {"trials": 3, "pass_rate": round(2 / 3, 4), "avg_duration_seconds": 90.0}
    assert stats["b"]["pass_rate"] == 0.0 and stats["b"]["trials"] == 1

def test_load_returns_empty_when_cache_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(official_stats.settings, "tasks_dir", tmp_path / "tasks")
    monkeypatch.setattr(official_stats, "_cache", None)
    assert load_official_stats() == {}

def test_aggregate_includes_model_and_reasoning_configuration():
    rows = [
        {"task_name": "a", "model": "gpt-5-6-sol", "reasoning_effort": "high", "passed": True, "trial_duration_seconds": 60.0, "cost_usd": 1.0},
        {"task_name": "a", "model": "gpt-5-6-sol", "reasoning_effort": "high", "passed": False, "trial_duration_seconds": 120.0, "cost_usd": 3.0},
        {"task_name": "a", "model": "gpt-5-5", "reasoning_effort": "low", "passed": True, "trial_duration_seconds": 30.0, "cost_usd": 0.5},
    ]
    stats = aggregate_trials(rows)["a"]
    exact = configuration_stats(stats, "gpt-5.6-sol", "high")
    assert exact is not None
    assert exact["trials"] == 2
    assert exact["pass_rate"] == 0.5
    assert exact["avg_duration_seconds"] == 90.0
    assert exact["avg_cost_usd"] == 2.0

def test_load_reads_cache_file(tmp_path, monkeypatch):
    monkeypatch.setattr(official_stats.settings, "tasks_dir", tmp_path / "tasks")
    monkeypatch.setattr(official_stats, "_cache", None)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    payload = {"version": "v1.1", "tasks": {"demo-task": {"trials": 4, "pass_rate": 0.75, "avg_duration_seconds": 420.0}}}
    (data_dir / f"official-task-stats-{official_stats.OFFICIAL_VERSION}.json").write_text(json.dumps(payload), encoding="utf-8")
    stats = load_official_stats()
    assert stats["demo-task"]["pass_rate"] == 0.75

def test_repo_cache_file_is_valid_and_matches_site():
    # 仓库自带的缓存必须可解析，且 actionlint 控制任务与官方站点口径一致（80% / ~16m26s）
    monkey_free = load_official_stats()
    control = monkey_free.get("actionlint-action-pinning-lint")
    assert control, "仓库缓存缺少控制任务数据"
    assert abs(control["pass_rate"] - 0.799) < 0.02
    assert abs(control["avg_duration_seconds"] - 986) < 30
