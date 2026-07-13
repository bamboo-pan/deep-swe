import json
from collections import Counter
from typing import Any

import httpx

from .official_stats import normalize_model_name
from .preferences import credential_path
from .results import compare_runs
from .security import read_credential, redact

ANALYSIS_MODEL = "gpt-5.6-sol"
ANALYSIS_REASONING_EFFORT = "max"


class CompareAnalysisInputError(ValueError):
    pass


class CompareAnalysisProviderError(RuntimeError):
    pass


def _verdict(local_rate: float | None, official_rate: float | None) -> str:
    if local_rate is None or official_rate is None:
        return "unavailable"
    if abs(local_rate - official_rate) <= 0.0005:
        return "consistent"
    return "better" if local_rate > official_rate else "worse"


def _matching_baseline(row: dict, run: dict) -> dict | None:
    model = normalize_model_name(run.get("model"))
    effort = (run.get("reasoning_effort") or "none").lower()
    return next(
        (
            item
            for item in row.get("official_configurations") or []
            if normalize_model_name(item.get("model")) == model
            and (item.get("reasoning_effort") or "none").lower() == effort
            and item.get("available", True)
        ),
        None,
    )


def build_compare_facts(comparison: dict) -> list[dict[str, Any]]:
    runs = {str(run["id"]): run for run in comparison.get("runs") or []}
    facts: list[dict[str, Any]] = []
    for row in comparison.get("tasks") or []:
        for run_id, local in (row.get("runs") or {}).items():
            run = runs.get(str(run_id))
            if not run or not local.get("trial_ids"):
                continue
            selected_trial_ids = set(local["trial_ids"])
            agent_versions = sorted(
                {
                    str(trial["agent_version"])
                    for trial in run.get("trials") or []
                    if trial.get("id") in selected_trial_ids and trial.get("agent_version")
                }
            )
            official = _matching_baseline(row, run)
            official_rate = official.get("pass_rate") if official else None
            local_rate = local.get("pass_rate")
            verdict = _verdict(local_rate, official_rate)
            strict = run.get("agent") == "mini-swe-agent"
            facts.append(
                {
                    "task": row.get("task"),
                    "task_code": row.get("task_code"),
                    "task_title": row.get("task_title"),
                    "run_id": run.get("id"),
                    "run_code": run.get("run_code"),
                    "agent": run.get("agent"),
                    "agent_versions": agent_versions,
                    "pier_version": run.get("pier_version"),
                    "model": run.get("model"),
                    "reasoning_effort": run.get("reasoning_effort"),
                    "run_configuration": {
                        key: run.get(key)
                        for key in (
                            "reasoning_effort_adapter",
                            "reasoning_effort_effective",
                            "service_tier",
                            "concurrency",
                            "agent_timeout_seconds",
                            "verifier_timeout_seconds",
                            "verification",
                            "retry_infrastructure_errors",
                            "infrastructure_max_retries",
                            "agent_max_steps",
                            "codex_request_max_retries",
                            "codex_stream_max_retries",
                            "codex_stream_idle_timeout_seconds",
                        )
                    },
                    "comparison_scope": "strict" if strict else "reference",
                    "verdict": verdict,
                    "local": {
                        "attempts": local.get("attempts"),
                        "measured_attempts": local.get("measured_attempts"),
                        "pass_rate": local_rate,
                        "avg_duration_seconds": local.get("duration_seconds"),
                        "avg_input_tokens": local.get("input_tokens"),
                        "avg_cache_tokens": local.get("cached_tokens"),
                        "avg_output_tokens": local.get("output_tokens"),
                        "avg_cost_usd": local.get("cost_usd"),
                        "avg_steps": local.get("steps"),
                    },
                    "official": {
                        "available": official is not None,
                        "trials": official.get("trials") if official else None,
                        "pass_rate": official_rate,
                        "avg_duration_seconds": official.get("avg_duration_seconds") if official else None,
                        "avg_input_tokens": official.get("avg_input_tokens") if official else None,
                        "avg_cache_tokens": official.get("avg_cache_tokens") if official else None,
                        "avg_output_tokens": official.get("avg_output_tokens") if official else None,
                        "avg_cost_usd": official.get("avg_cost_usd") if official else None,
                        "avg_steps": official.get("avg_steps") if official else None,
                    },
                }
            )
    return facts


def _response_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"].strip()
    parts: list[str] = []
    for output in payload.get("output") or []:
        if output.get("type") != "message":
            continue
        for content in output.get("content") or []:
            value = content.get("text")
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return "\n\n".join(parts)


def _prompt(facts: list[dict]) -> str:
    return (
        "请用中文分析以下 DeepSWE 实测与官方精确配置基准。\n"
        "硬性规则：\n"
        "1. 每条事实都已尝试按相同 task、相同模型、相同思考强度对齐；official.available=false 表示没有精确基准。verdict 是确定性结论，不得修改。\n"
        "2. consistent=一致，better=变好，worse=变坏，unavailable=缺少官方精确基准，无法判断。\n"
        "3. comparison_scope=strict 仅代表 mini-swe-agent 对官方 mini-swe-agent 的严格比较；reference 表示 Codex/Claude Code 对官方只能参考，不能写成严格提升或退化。\n"
        "4. 不要用官方全部模型汇总替代精确配置基准。\n"
        "5. 逐项分析必须补充平均用时、平均成本和平均步骤的实测/官方差异；这些效率与资源维度只用于解释，不能改变 verdict，主结论仍只依据通过率。数据缺失时明确说明。\n"
        "6. 本地样本较少时必须提示不确定性。\n"
        "7. 只有 agent、agent_versions、pier_version、模型、思考强度和 run_configuration 全部一致时，才允许声称不同本地运行之间存在严格纵向变化；否则不做纵向归因。\n"
        "输出简洁纯文本，依次给出：总体结论、逐项分析、口径与风险。不要输出 Markdown 表格。\n\n"
        f"事实数据：\n{json.dumps(facts, ensure_ascii=False, separators=(',', ':'))}"
    )


def analyze_compare_items(items: list[tuple[int, str]]) -> dict:
    if not items:
        raise CompareAnalysisInputError("没有可分析的筛选结果")
    facts = build_compare_facts(compare_runs([], items))
    if not facts:
        raise CompareAnalysisInputError("当前筛选结果没有可分析的 Trial 数据")

    try:
        credential = read_credential(credential_path())
    except (OSError, ValueError) as exc:
        raise CompareAnalysisProviderError(f"AI 分析凭据不可用：{exc}") from exc

    endpoint = f"{credential.url.rstrip('/')}/responses"
    try:
        response = httpx.post(
            endpoint,
            headers={
                "authorization": f"Bearer {credential.token}",
                "content-type": "application/json",
            },
            json={
                "model": ANALYSIS_MODEL,
                "reasoning": {"effort": ANALYSIS_REASONING_EFFORT},
                "input": _prompt(facts),
                "max_output_tokens": 8000,
                "store": False,
            },
            timeout=180.0,
        )
        response.raise_for_status()
        content = _response_text(response.json())
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        detail = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f"HTTP {exc.response.status_code}: {exc.response.text[:600]}"
        raise CompareAnalysisProviderError(
            f"AI 分析请求失败：{redact(detail, [credential.token])}"
        ) from exc
    if not content:
        raise CompareAnalysisProviderError("AI 分析服务返回了空内容")

    counts = Counter(fact["verdict"] for fact in facts)
    scopes = Counter(fact["comparison_scope"] for fact in facts)
    return {
        "model": ANALYSIS_MODEL,
        "reasoning_effort": ANALYSIS_REASONING_EFFORT,
        "analysis": content,
        "summary": {
            "total": len(facts),
            "consistent": counts["consistent"],
            "better": counts["better"],
            "worse": counts["worse"],
            "unavailable": counts["unavailable"],
            "strict": scopes["strict"],
            "reference": scopes["reference"],
        },
        "comparisons": facts,
    }
