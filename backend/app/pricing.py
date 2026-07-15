"""模型定价表（按 token 估算费用时的单价来源）。

数据来自 https://models.dev（社区维护的 provider/model 定价库，单位 $/1M
tokens）。精简后的快照缓存在 data/model-pricing.json 并随仓库分发，离线
环境也能估价；POST /api/pricing/sync 可手动刷新。未收录的模型回退
DEFAULT_PRICING（gpt-5.6-sol 档，与接入定价表前的硬编码单价一致，保证
历史 Run 的估算口径不变）。

models.dev 对部分模型还提供超长上下文分档价（tiers/context_over_200k），
但 Pier stats 只有累计 token、没有单请求上下文长度，无法分档，统一按基础
档计。
"""
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
import httpx
from .config import settings

MODELS_DEV_API_URL = "https://models.dev/api.json"
# 查不到模型时的兜底单价（$/1M tokens）。
DEFAULT_PRICING = {"input": 5.0, "cache_read": 0.5, "output": 30.0}
# 裸模型名被多个 provider 收录（镜像商）时的取价顺序；列表外的按 id 字母序排最后。
_PROVIDER_PRIORITY = ("openai", "anthropic", "google", "google-vertex", "xai", "deepseek", "mistral")
_COST_KEYS = ("input", "output", "cache_read", "cache_write")

_cache: dict | None = None
_index: dict | None = None
_lock = threading.Lock()

def _cache_path() -> Path:
    return settings.tasks_dir.parent / "data" / "model-pricing.json"

def simplify_api_payload(api: dict) -> dict:
    """models.dev api.json → {provider: {model: {input, output, cache_read?, cache_write?}}}。"""
    providers: dict[str, dict] = {}
    for provider_id, provider in api.items():
        models = provider.get("models") if isinstance(provider, dict) else None
        if not isinstance(models, dict):
            continue
        entries = {}
        for model_id, model in models.items():
            cost = model.get("cost") if isinstance(model, dict) else None
            if not isinstance(cost, dict):
                continue
            entry = {
                key: float(cost[key]) for key in _COST_KEYS
                if isinstance(cost.get(key), (int, float)) and not isinstance(cost.get(key), bool)
            }
            if "input" in entry and "output" in entry:
                entries[str(model_id)] = entry
        if entries:
            providers[str(provider_id)] = entries
    return providers

def _provider_rank(provider: str) -> tuple[int, str]:
    if provider in _PROVIDER_PRIORITY:
        return (_PROVIDER_PRIORITY.index(provider), "")
    return (len(_PROVIDER_PRIORITY), provider)

def _build_index(providers: dict) -> dict:
    exact: dict[str, dict] = {}
    bare: dict[str, tuple[tuple[int, str], dict]] = {}
    for provider_id, models in sorted(providers.items()):
        rank = _provider_rank(provider_id)
        for model_id, cost in models.items():
            exact[f"{provider_id}/{model_id}".lower()] = cost
            key = model_id.lower()
            if key not in bare or rank < bare[key][0]:
                bare[key] = (rank, cost)
    return {"exact": exact, "bare": {key: cost for key, (_, cost) in bare.items()}}

def _load_locked() -> dict:
    global _cache, _index
    if _cache is None:
        try:
            _cache = json.loads(_cache_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _cache = {}
        _index = None
    if _index is None:
        _index = _build_index(_cache.get("providers", {}))
    return _index

def pricing_for_model(model: str | None) -> dict | None:
    """按模型名查单价；接受裸 id（gpt-5.6-sol）或带 provider 前缀（openai/gpt-5.6-sol）。"""
    if not isinstance(model, str) or not model.strip():
        return None
    with _lock:
        index = _load_locked()
    wanted = model.strip().lower()
    return index["exact"].get(wanted) or index["bare"].get(wanted)

def pricing_meta() -> dict:
    with _lock:
        _load_locked()
        providers = _cache.get("providers", {})
        return {
            "source": _cache.get("source"),
            "synced_at": _cache.get("synced_at"),
            "n_providers": len(providers),
            "n_models": sum(len(models) for models in providers.values()),
            "default_pricing": dict(DEFAULT_PRICING),
        }

def sync_pricing(timeout: float = 60.0) -> dict:
    """从 models.dev 重新拉取并精简；成功后更新快照文件与内存缓存。"""
    global _cache, _index
    response = httpx.get(MODELS_DEV_API_URL, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    providers = simplify_api_payload(response.json())
    if not providers:
        raise ValueError("models.dev 返回空定价数据，保留现有快照")
    payload = {
        "source": MODELS_DEV_API_URL,
        "synced_at": datetime.now(UTC).isoformat(),
        "providers": providers,
    }
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    with _lock:
        _cache, _index = payload, None
    return {"synced": True, **pricing_meta()}
