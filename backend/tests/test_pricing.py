"""模型定价表与按模型估算费用的测试。"""
import json
import pytest
from app import pricing
from app.pricing import DEFAULT_PRICING, pricing_for_model, simplify_api_payload, sync_pricing
from app.results import estimate_cost
from app import results

def _use_providers(monkeypatch, providers):
    """用内存快照替换缓存，隔离仓库 data/model-pricing.json。"""
    monkeypatch.setattr(pricing, "_cache", {"providers": providers})
    monkeypatch.setattr(pricing, "_index", None)

def test_simplify_keeps_fully_priced_models_only():
    api = {
        "openai": {"models": {
            "good": {"cost": {"input": 1, "output": 2, "cache_read": 0.1, "extra": "ignored"}},
            "no-output": {"cost": {"input": 3}},
            "no-cost": {},
        }},
        "empty-provider": {"models": {}},
        "junk": "not-a-dict",
    }
    assert simplify_api_payload(api) == {
        "openai": {"good": {"input": 1.0, "output": 2.0, "cache_read": 0.1}},
    }

def test_lookup_accepts_bare_name_prefix_and_case(monkeypatch):
    cost = {"input": 1.0, "output": 2.0}
    _use_providers(monkeypatch, {"openai": {"gpt-x": cost}})
    assert pricing_for_model("gpt-x") == cost
    assert pricing_for_model("openai/gpt-x") == cost
    assert pricing_for_model("GPT-X") == cost
    assert pricing_for_model("unknown-model") is None
    assert pricing_for_model(None) is None
    assert pricing_for_model("  ") is None

def test_lookup_prefers_priority_provider_for_bare_names(monkeypatch):
    official = {"input": 1.0, "output": 2.0}
    mirror = {"input": 99.0, "output": 99.0}
    _use_providers(monkeypatch, {"a-mirror": {"gpt-x": mirror}, "openai": {"gpt-x": official}})
    assert pricing_for_model("gpt-x") == official  # 裸名按优先级取官方价
    assert pricing_for_model("a-mirror/gpt-x") == mirror  # 显式前缀仍可取镜像价

def test_estimate_cost_uses_model_pricing(monkeypatch):
    _use_providers(monkeypatch, {"openai": {"cheap": {"input": 1.0, "cache_read": 0.1, "output": 2.0}}})
    # uncached 0.5M*$1 + cached 0.5M*$0.1 + output 1M*$2 = 2.55
    assert estimate_cost(1_000_000, 500_000, 1_000_000, "standard", "cheap") == pytest.approx(2.55)

def test_estimate_cost_falls_back_to_default_pricing(monkeypatch):
    _use_providers(monkeypatch, {})
    expected = (600_000 * 5 + 400_000 * 0.5 + 1_000_000 * 30) / 1_000_000
    assert estimate_cost(1_000_000, 400_000, 1_000_000) == pytest.approx(expected)
    assert estimate_cost(1_000_000, 400_000, 1_000_000, "batch", "unknown") == pytest.approx(expected * 0.5)

def test_estimate_cost_charges_cache_at_input_price_without_discount(monkeypatch):
    _use_providers(monkeypatch, {"openai": {"flat": {"input": 2.0, "output": 4.0}}})
    assert estimate_cost(1_000_000, 400_000, 0, "standard", "flat") == pytest.approx(2.0)

def test_estimate_cost_returns_none_without_tokens(monkeypatch):
    _use_providers(monkeypatch, {"openai": {"cheap": {"input": 1.0, "output": 2.0}}})
    assert estimate_cost(None, None, None, "standard", "cheap") is None

def test_trial_backfills_missing_reported_cost_from_tokens(tmp_path, monkeypatch):
    _use_providers(monkeypatch, {"openai": {"cheap": {"input": 1.0, "cache_read": 0.1, "output": 2.0}}})
    trial = tmp_path / "task__one"
    trial.mkdir()
    (trial / "result.json").write_text(json.dumps({
        "agent_result": {
            "n_input_tokens": 1_000_000,
            "n_cache_tokens": 500_000,
            "n_output_tokens": 1_000_000,
        },
        "verifier_result": {"rewards": {"reward": 1}},
    }), encoding="utf-8")

    parsed = results._trial(trial, model="cheap")

    assert parsed["reported_cost_usd"] is None
    assert parsed["estimated_cost_usd"] == pytest.approx(2.55)
    assert parsed["cost_usd"] == pytest.approx(2.55)
    assert parsed["cost_source"] == "estimated"

def test_repo_snapshot_is_valid_and_covers_current_model(monkeypatch):
    # 仓库自带快照必须可解析，且当前默认模型 gpt-5.6-sol 的单价与历史硬编码口径一致
    monkeypatch.setattr(pricing, "_cache", None)
    monkeypatch.setattr(pricing, "_index", None)
    cost = pricing_for_model("gpt-5.6-sol")
    assert cost and cost["input"] == 5.0 and cost["output"] == 30.0 and cost["cache_read"] == 0.5
    meta = pricing.pricing_meta()
    assert meta["n_models"] > 1000 and meta["default_pricing"] == DEFAULT_PRICING

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload

def test_sync_pricing_writes_snapshot_and_refreshes_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(pricing.settings, "tasks_dir", tmp_path / "tasks")
    monkeypatch.setattr(pricing, "_cache", None)
    monkeypatch.setattr(pricing, "_index", None)
    payload = {"openai": {"models": {"gpt-x": {"cost": {"input": 1, "output": 2}}}}}
    monkeypatch.setattr(pricing.httpx, "get", lambda *args, **kwargs: _FakeResponse(payload))
    result = sync_pricing()
    assert result["synced"] and result["n_models"] == 1
    stored = json.loads((tmp_path / "data" / "model-pricing.json").read_text(encoding="utf-8"))
    assert stored["providers"]["openai"]["gpt-x"] == {"input": 1.0, "output": 2.0}
    assert pricing_for_model("gpt-x") == {"input": 1.0, "output": 2.0}

def test_sync_pricing_rejects_empty_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(pricing.settings, "tasks_dir", tmp_path / "tasks")
    monkeypatch.setattr(pricing.httpx, "get", lambda *args, **kwargs: _FakeResponse({}))
    with pytest.raises(ValueError):
        sync_pricing()
    assert not (tmp_path / "data" / "model-pricing.json").exists()

def test_pricing_api_meta_and_failed_sync(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(pricing, "_cache", None)
    monkeypatch.setattr(pricing, "_index", None)
    def unreachable(*args, **kwargs):
        raise RuntimeError("network down")
    monkeypatch.setattr(pricing.httpx, "get", unreachable)
    with TestClient(app) as client:
        meta = client.get("/api/pricing/meta").json()
        assert meta["n_models"] > 1000
        assert client.post("/api/pricing/sync").status_code == 502
