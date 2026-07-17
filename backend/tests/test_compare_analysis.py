import json

import httpx

from app import compare_analysis
from app.security import Credential


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


def test_prompt_keeps_facts_when_saved_instructions_are_empty():
    prompt = compare_analysis._prompt([{"task": "task-a", "verdict": "consistent"}], "")

    assert prompt.startswith("事实数据：\n")
    assert '"task":"task-a"' in prompt
    assert "硬性规则" not in prompt


def test_stream_compare_analysis_uses_selected_config_and_emits_deltas(monkeypatch):
    facts = compare_analysis.build_compare_facts(_comparison())
    credential = Credential("http://127.0.0.1:9887/v1", "secret-token", "fingerprint")
    config = {
        "prompt": compare_analysis.DEFAULT_ANALYSIS_PROMPT,
        "model": "gpt-5.6-terra",
        "reasoning_effort": "high",
        "timeout_seconds": 600,
    }
    captured = {}

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_lines(self):
            events = [
                {"type": "response.created", "response": {"status": "in_progress"}},
                {"type": "response.in_progress", "response": {"status": "in_progress"}},
                {"type": "response.output_text.delta", "delta": "分析"},
                {"type": "response.output_text.delta", "delta": "结果"},
                {
                    "type": "response.completed",
                    "response": {
                        "output": [{
                            "type": "message",
                            "content": [{"type": "output_text", "text": "分析结果"}],
                        }],
                    },
                },
            ]
            for event in events:
                yield f"event: {event['type']}"
                yield f"data: {json.dumps(event, ensure_ascii=False)}"
                yield ""

    def fake_stream(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(compare_analysis, "reserve_provider_request", lambda: 0)
    monkeypatch.setattr(compare_analysis.httpx, "stream", fake_stream)

    events = list(compare_analysis.stream_compare_analysis_events(facts, credential, config))

    assert captured["method"] == "POST"
    assert captured["url"] == "http://127.0.0.1:9887/v1/responses"
    assert captured["json"]["model"] == "gpt-5.6-terra"
    assert captured["json"]["reasoning"] == {"effort": "high"}
    assert captured["json"]["max_output_tokens"] == 8000
    assert captured["json"]["store"] is False
    assert captured["json"]["stream"] is True
    assert captured["timeout"].read == 600
    assert "平均用时、平均成本和平均步骤" in captured["json"]["input"]
    assert "主结论仍只依据通过率" in captured["json"]["input"]
    assert [event for event, _data in events] == [
        "start", "status", "status", "status", "delta", "delta", "complete",
    ]
    assert [data["delta"] for event, data in events if event == "delta"] == ["分析", "结果"]
    result = events[-1][1]
    assert result["analysis"] == "分析结果"
    assert result["model"] == "gpt-5.6-terra"
    assert result["reasoning_effort"] == "high"
    assert result["timeout_seconds"] == 600
    assert result["summary"] == {
        "total": 2,
        "consistent": 1,
        "better": 1,
        "worse": 0,
        "unavailable": 0,
        "strict": 1,
        "reference": 1,
    }


def test_stream_compare_analysis_keeps_partial_output_on_read_timeout(monkeypatch):
    facts = compare_analysis.build_compare_facts(_comparison())
    credential = Credential("http://127.0.0.1:9887/v1", "secret-token", "fingerprint")
    config = {
        "prompt": "简洁分析",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "max",
        "timeout_seconds": 30,
    }

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_lines(self):
            yield 'data: {"type":"response.output_text.delta","delta":"部分结果"}'
            yield ""
            raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(compare_analysis, "reserve_provider_request", lambda: 0)
    monkeypatch.setattr(compare_analysis.httpx, "stream", lambda *_args, **_kwargs: Response())

    events = list(compare_analysis.stream_compare_analysis_events(facts, credential, config))

    assert ("delta", {"delta": "部分结果"}) in events
    assert events[-1][0] == "error"
    assert "read timed out" in events[-1][1]["message"]


def test_compare_analysis_config_reads_and_saves_all_fields(monkeypatch):
    stored = {}
    monkeypatch.setattr(
        compare_analysis,
        "get_auxiliary_preferences",
        lambda defaults: {**defaults, **stored},
    )
    monkeypatch.setattr(
        compare_analysis,
        "set_auxiliary_preferences",
        lambda values: stored.update(values),
    )

    saved = compare_analysis.save_compare_analysis_config(
        "自定义提示词", "model-a", "xhigh", 1200,
    )
    loaded = compare_analysis.get_compare_analysis_config()

    assert saved == {
        "prompt": "自定义提示词",
        "model": "model-a",
        "reasoning_effort": "xhigh",
        "timeout_seconds": 1200,
    }
    assert loaded == saved


def test_prepare_compare_analysis_rejects_empty_selection_before_provider_call():
    try:
        compare_analysis.prepare_compare_analysis([])
    except compare_analysis.CompareAnalysisInputError as exc:
        assert "没有可分析" in str(exc)
    else:
        raise AssertionError("empty selections must be rejected")
