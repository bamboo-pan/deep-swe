import json
import time
from collections import Counter
from typing import Any, Iterator

import httpx

from .official_stats import normalize_model_name
from .preferences import (
    credential_path, get_auxiliary_preferences, set_auxiliary_preferences,
)
from .provider_catalog import EFFORT_ORDER
from .provider_proxy import record_limit_event, reserve_provider_request
from .results import compare_runs
from .security import Credential, read_credential, redact

DEFAULT_ANALYSIS_MODEL = "gpt-5.6-sol"
DEFAULT_ANALYSIS_REASONING_EFFORT = "max"
DEFAULT_ANALYSIS_TIMEOUT_SECONDS = 900
MIN_ANALYSIS_TIMEOUT_SECONDS = 30
MAX_ANALYSIS_TIMEOUT_SECONDS = 7200
ANALYSIS_PROMPT_SETTING_KEY = "compare_analysis_prompt"
ANALYSIS_MODEL_SETTING_KEY = "compare_analysis_model"
ANALYSIS_REASONING_EFFORT_SETTING_KEY = "compare_analysis_reasoning_effort"
ANALYSIS_TIMEOUT_SETTING_KEY = "compare_analysis_timeout_seconds"
MAX_ANALYSIS_PROMPT_LENGTH = 20_000
DEFAULT_ANALYSIS_PROMPT = (
    "请用中文分析以下 DeepSWE 实测与官方精确配置基准。\n"
    "硬性规则：\n"
    "1. 每条事实都已尝试按相同 task、相同模型、相同思考强度对齐；official.available=false 表示没有精确基准。verdict 是确定性结论，不得修改。\n"
    "2. consistent=一致，better=变好，worse=变坏，unavailable=缺少官方精确基准，无法判断。\n"
    "3. comparison_scope=strict 仅代表 mini-swe-agent 对官方 mini-swe-agent 的严格比较；reference 表示 Codex/Claude Code 对官方只能参考，不能写成严格提升或退化。\n"
    "4. 不要用官方全部模型汇总替代精确配置基准。\n"
    "5. 逐项分析必须补充平均用时、平均成本和平均步骤的实测/官方差异；这些效率与资源维度只用于解释，不能改变 verdict，主结论仍只依据通过率。数据缺失时明确说明。\n"
    "6. 本地样本较少时必须提示不确定性。\n"
    "7. 只有 agent、agent_versions、pier_version、模型、思考强度和 run_configuration 全部一致时，才允许声称不同本地运行之间存在严格纵向变化；否则不做纵向归因。\n"
    "输出简洁纯文本，依次给出：总体结论、逐项分析、口径与风险。不要输出 Markdown 表格。"
)


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


def get_compare_analysis_config() -> dict:
    values = get_auxiliary_preferences(
        {
            ANALYSIS_PROMPT_SETTING_KEY: DEFAULT_ANALYSIS_PROMPT,
            ANALYSIS_MODEL_SETTING_KEY: DEFAULT_ANALYSIS_MODEL,
            ANALYSIS_REASONING_EFFORT_SETTING_KEY: DEFAULT_ANALYSIS_REASONING_EFFORT,
            ANALYSIS_TIMEOUT_SETTING_KEY: DEFAULT_ANALYSIS_TIMEOUT_SECONDS,
        }
    )
    prompt = values[ANALYSIS_PROMPT_SETTING_KEY]
    if not isinstance(prompt, str) or len(prompt) > MAX_ANALYSIS_PROMPT_LENGTH:
        prompt = DEFAULT_ANALYSIS_PROMPT
    model = values[ANALYSIS_MODEL_SETTING_KEY]
    if not isinstance(model, str) or not model.strip() or len(model) > 100:
        model = DEFAULT_ANALYSIS_MODEL
    effort = values[ANALYSIS_REASONING_EFFORT_SETTING_KEY]
    if effort not in EFFORT_ORDER:
        effort = DEFAULT_ANALYSIS_REASONING_EFFORT
    timeout_seconds = values[ANALYSIS_TIMEOUT_SETTING_KEY]
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not MIN_ANALYSIS_TIMEOUT_SECONDS <= timeout_seconds <= MAX_ANALYSIS_TIMEOUT_SECONDS
    ):
        timeout_seconds = DEFAULT_ANALYSIS_TIMEOUT_SECONDS
    return {
        "prompt": prompt,
        "model": model.strip(),
        "reasoning_effort": effort,
        "timeout_seconds": timeout_seconds,
    }


def save_compare_analysis_config(
    prompt: str,
    model: str,
    reasoning_effort: str,
    timeout_seconds: int,
) -> dict:
    if len(prompt) > MAX_ANALYSIS_PROMPT_LENGTH:
        raise ValueError(f"AI 分析提示词不能超过 {MAX_ANALYSIS_PROMPT_LENGTH} 个字符")
    normalized_model = model.strip()
    if not normalized_model or len(normalized_model) > 100:
        raise ValueError("AI 分析模型不能为空且不能超过 100 个字符")
    if reasoning_effort not in EFFORT_ORDER:
        raise ValueError(f"不支持的 AI 分析思考强度：{reasoning_effort}")
    if not MIN_ANALYSIS_TIMEOUT_SECONDS <= timeout_seconds <= MAX_ANALYSIS_TIMEOUT_SECONDS:
        raise ValueError(
            f"AI 分析超时需为 {MIN_ANALYSIS_TIMEOUT_SECONDS}-{MAX_ANALYSIS_TIMEOUT_SECONDS} 秒"
        )
    config = {
        "prompt": prompt,
        "model": normalized_model,
        "reasoning_effort": reasoning_effort,
        "timeout_seconds": timeout_seconds,
    }
    set_auxiliary_preferences(
        {
            ANALYSIS_PROMPT_SETTING_KEY: prompt,
            ANALYSIS_MODEL_SETTING_KEY: normalized_model,
            ANALYSIS_REASONING_EFFORT_SETTING_KEY: reasoning_effort,
            ANALYSIS_TIMEOUT_SETTING_KEY: timeout_seconds,
        }
    )
    return config


def _prompt(facts: list[dict], instructions: str = DEFAULT_ANALYSIS_PROMPT) -> str:
    facts_text = f"事实数据：\n{json.dumps(facts, ensure_ascii=False, separators=(',', ':'))}"
    normalized = instructions.strip()
    return f"{normalized}\n\n{facts_text}" if normalized else facts_text


def prepare_compare_analysis(items: list[tuple[int, str]]) -> tuple[list[dict[str, Any]], Credential]:
    if not items:
        raise CompareAnalysisInputError("没有可分析的筛选结果")
    facts = build_compare_facts(compare_runs([], items))
    if not facts:
        raise CompareAnalysisInputError("当前筛选结果没有可分析的 Trial 数据")

    try:
        credential = read_credential(credential_path())
    except (OSError, ValueError) as exc:
        raise CompareAnalysisProviderError(f"AI 分析凭据不可用：{exc}") from exc
    return facts, credential


def _analysis_result(facts: list[dict], config: dict, content: str) -> dict:
    counts = Counter(fact["verdict"] for fact in facts)
    scopes = Counter(fact["comparison_scope"] for fact in facts)
    return {
        "model": config["model"],
        "reasoning_effort": config["reasoning_effort"],
        "timeout_seconds": config["timeout_seconds"],
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


def _iter_provider_sse(response: httpx.Response) -> Iterator[dict]:
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            data_lines = []
            if data == "[DONE]":
                return
            yield json.loads(data)
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        data = "\n".join(data_lines)
        if data != "[DONE]":
            yield json.loads(data)


def _provider_event_error(event: dict) -> str:
    response = event.get("response") if isinstance(event.get("response"), dict) else {}
    error = response.get("error") if isinstance(response.get("error"), dict) else {}
    top_error = event.get("error") if isinstance(event.get("error"), dict) else {}
    return str(
        event.get("message")
        or top_error.get("message")
        or error.get("message")
        or response.get("incomplete_details")
        or "Provider 未说明失败原因"
    )


def stream_compare_analysis_events(
    facts: list[dict[str, Any]],
    credential: Credential,
    config: dict,
) -> Iterator[tuple[str, dict]]:
    yield "start", _analysis_result(facts, config, "")
    endpoint = f"{credential.url.rstrip('/')}/responses"
    deltas: list[str] = []
    fallback_texts: list[str] = []
    try:
        delay = reserve_provider_request()
        if delay:
            yield "status", {"message": f"等待 Provider 配额 {delay:.1f} 秒"}
            time.sleep(delay)
        yield "status", {"message": "正在连接分析模型"}
        timeout = httpx.Timeout(
            float(config["timeout_seconds"]),
            connect=min(float(config["timeout_seconds"]), 30.0),
        )
        with httpx.stream(
            "POST",
            endpoint,
            headers={
                "authorization": f"Bearer {credential.token}",
                "content-type": "application/json",
            },
            json={
                "model": config["model"],
                "reasoning": {"effort": config["reasoning_effort"]},
                "input": _prompt(facts, config["prompt"]),
                "max_output_tokens": 8000,
                "store": False,
                "stream": True,
            },
            timeout=timeout,
        ) as response:
            if response.status_code >= 400:
                content = response.read()
                if response.status_code == 429:
                    record_limit_event(response.status_code, content)
                detail = content.decode("utf-8", errors="replace")[:600]
                raise CompareAnalysisProviderError(
                    f"AI 分析请求失败：HTTP {response.status_code}: "
                    f"{redact(detail, [credential.token])}"
                )
            for event in _iter_provider_sse(response):
                event_type = event.get("type")
                if event_type == "response.created":
                    yield "status", {"message": "模型已接收请求"}
                elif event_type == "response.in_progress":
                    yield "status", {"message": "模型正在推理"}
                elif event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str) and delta:
                        deltas.append(delta)
                        yield "delta", {"delta": delta}
                elif event_type == "response.output_text.done":
                    text = event.get("text")
                    if isinstance(text, str) and text:
                        fallback_texts.append(text)
                elif event_type == "response.completed":
                    response_payload = event.get("response")
                    final_text = _response_text(response_payload) if isinstance(response_payload, dict) else ""
                    content = final_text or "".join(deltas) or "\n\n".join(fallback_texts)
                    if not content:
                        raise CompareAnalysisProviderError("AI 分析服务返回了空内容")
                    yield "complete", _analysis_result(facts, config, content)
                    return
                elif event_type in {"response.failed", "response.incomplete", "error"}:
                    raise CompareAnalysisProviderError(
                        f"AI 分析请求失败：{_provider_event_error(event)}"
                    )
        raise CompareAnalysisProviderError("AI 分析流在完成事件前已断开")
    except CompareAnalysisProviderError as exc:
        yield "error", {"message": redact(str(exc), [credential.token])}
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        detail = str(exc)
        yield "error", {
            "message": f"AI 分析请求失败：{redact(detail, [credential.token])}"
        }
