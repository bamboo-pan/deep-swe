from pathlib import Path

from app import compare_analysis


def _comparison() -> dict:
    return {
        "runs": [
            {
                "id": 1,
                "run_code": "RUN-000001",
                "agent": "mini-swe-agent",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "max",
                "pier_version": "pier 1.2.3",
                "service_tier": "standard",
                "trials": [{"id": "task-a__1", "agent_version": "mini-swe-agent 1.0"}],
            },
            {
                "id": 2,
                "run_code": "RUN-000002",
                "agent": "codex",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "max",
                "pier_version": "pier 1.2.3",
                "service_tier": "standard",
                "trials": [{"id": "task-a__1", "agent_version": "codex 2.0"}],
            },
        ],
        "tasks": [
            {
                "task": "task-a",
                "task_code": "TASK-001",
                "task_title": "Task A",
                "official_configurations": [
                    {
                        "model": "gpt-5-6-sol",
                        "reasoning_effort": "max",
                        "available": True,
                        "trials": 10,
                        "pass_rate": 0.5,
                        "avg_duration_seconds": 100,
                    }
                ],
                "runs": {
                    "1": {"trial_ids": ["task-a__1"], "attempts": 1, "measured_attempts": 1, "pass_rate": 0.5},
                    "2": {"trial_ids": ["task-a__1"], "attempts": 1, "measured_attempts": 1, "pass_rate": 1.0},
                },
            }
        ],
    }


def test_build_compare_facts_uses_exact_configuration_and_agent_scope():
    facts = compare_analysis.build_compare_facts(_comparison())
    assert [(fact["verdict"], fact["comparison_scope"]) for fact in facts] == [
        ("consistent", "strict"),
        ("better", "reference"),
    ]
    assert all(fact["official"]["pass_rate"] == 0.5 for fact in facts)
    assert facts[0]["agent_versions"] == ["mini-swe-agent 1.0"]


def test_build_compare_facts_marks_missing_exact_baseline_unavailable():
    comparison = _comparison()
    comparison["tasks"][0]["official_configurations"] = [
        {"model": "another-model", "reasoning_effort": "max", "available": True, "pass_rate": 0.5}
    ]
    facts = compare_analysis.build_compare_facts(comparison)
    assert all(fact["verdict"] == "unavailable" for fact in facts)
    assert all(fact["official"]["available"] is False for fact in facts)


def test_analyze_compare_items_calls_responses_with_default_model_and_max(monkeypatch, tmp_path: Path):
    credential = tmp_path / "credential.txt"
    credential.write_text("http://127.0.0.1:9887/v1\nsecret-token\n", encoding="utf-8")
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "分析结果"}]}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(compare_analysis, "compare_runs", lambda run_ids, items: _comparison())
    monkeypatch.setattr(compare_analysis, "credential_path", lambda: credential)
    monkeypatch.setattr(compare_analysis.httpx, "post", fake_post)

    result = compare_analysis.analyze_compare_items([(1, "task-a__1")])

    assert captured["url"] == "http://127.0.0.1:9887/v1/responses"
    assert captured["json"]["model"] == "gpt-5.6-sol"
    assert captured["json"]["reasoning"] == {"effort": "max"}
    assert captured["json"]["max_output_tokens"] == 8000
    assert captured["json"]["store"] is False
    assert "平均用时、平均成本和平均步骤" in captured["json"]["input"]
    assert "主结论仍只依据通过率" in captured["json"]["input"]
    assert result["analysis"] == "分析结果"
    assert result["summary"] == {
        "total": 2,
        "consistent": 1,
        "better": 1,
        "worse": 0,
        "unavailable": 0,
        "strict": 1,
        "reference": 1,
    }


def test_analyze_compare_items_rejects_empty_selection_before_provider_call():
    try:
        compare_analysis.analyze_compare_items([])
    except compare_analysis.CompareAnalysisInputError as exc:
        assert "没有可分析" in str(exc)
    else:
        raise AssertionError("empty selections must be rejected")
