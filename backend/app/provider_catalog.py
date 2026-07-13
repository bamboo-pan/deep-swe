from __future__ import annotations

import copy
import re
import threading
import time
from typing import Any

import httpx

from .preferences import credential_path
from .security import Credential, read_credential, redact

EFFORT_ORDER = ("none", "low", "medium", "high", "xhigh", "max")
_EFFORT_SET = set(EFFORT_ORDER)
_IMAGE_MODEL_RE = re.compile(r"(?:^|[-_.])(image|images|imagine)(?:$|[-_.])", re.IGNORECASE)
_CACHE_TTL_SECONDS = 60.0
_REQUEST_TIMEOUT_SECONDS = 8.0
_cache_lock = threading.Lock()
_catalog_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def clear_provider_catalog_cache() -> None:
    with _cache_lock:
        _catalog_cache.clear()


def _payload_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in ("models", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _model_id(model: dict[str, Any]) -> str:
    value = model.get("slug") or model.get("id") or model.get("name")
    if not isinstance(value, str):
        return ""
    return value.removeprefix("models/").strip()


def _normalize_efforts(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    found: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            value = value.get("effort") or value.get("level") or value.get("id")
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in _EFFORT_SET:
                found.add(normalized)
    return [effort for effort in EFFORT_ORDER if effort in found]


def _reasoning_efforts(*models: dict[str, Any]) -> list[str]:
    for model in models:
        for key in (
            "supported_reasoning_levels",
            "supported_reasoning_efforts",
            "reasoning_efforts",
            "supported_efforts",
        ):
            efforts = _normalize_efforts(model.get(key))
            if efforts:
                return efforts
        thinking = model.get("thinking")
        if isinstance(thinking, dict):
            efforts = _normalize_efforts(thinking.get("levels"))
            if efforts:
                return efforts
    return []


def _default_effort(efforts: list[str], *models: dict[str, Any]) -> str | None:
    for model in models:
        value = model.get("default_reasoning_level") or model.get("default_reasoning_effort")
        if isinstance(value, str) and value.lower() in efforts:
            return value.lower()
    if "medium" in efforts:
        return "medium"
    return efforts[0] if efforts else None


def _is_image_model(model_id: str, *models: dict[str, Any]) -> bool:
    lowered = model_id.lower()
    if lowered.startswith("dall-e") or _IMAGE_MODEL_RE.search(lowered):
        return True
    for model in models:
        model_type = str(model.get("type") or "").lower()
        if model_type in {"openai-image", "image", "image-generation"}:
            return True
        output_modalities = (
            model.get("output_modalities")
            or model.get("supportedOutputModalities")
            or model.get("supported_output_modalities")
        )
        if isinstance(output_modalities, list):
            normalized = {str(item).lower() for item in output_modalities}
            if "image" in normalized and "text" not in normalized:
                return True
    return False


def _fetch_json(credential: Credential, *, client_catalog: bool) -> Any:
    response = httpx.get(
        credential.url.rstrip("/") + "/models",
        params={"client_version": ""} if client_catalog else None,
        headers={
            "Authorization": f"Bearer {credential.token}",
            "User-Agent": "deepswe-regression-ui/0.1",
        },
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def _safe_error(exc: Exception, credential: Credential | None = None) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        detail = f"provider returned HTTP {exc.response.status_code}"
    else:
        detail = str(exc)
    return redact(detail, [credential.token] if credential else [])


def _fetch_provider_catalog(credential: Credential) -> dict[str, Any]:
    available_rows = _payload_rows(_fetch_json(credential, client_catalog=False))
    if not available_rows:
        raise ValueError("provider /models returned no models")

    capabilities_error: str | None = None
    try:
        capability_rows = _payload_rows(_fetch_json(credential, client_catalog=True))
    except Exception as exc:
        capability_rows = []
        capabilities_error = _safe_error(exc, credential)

    capabilities = {
        model_id: row
        for row in capability_rows
        if (model_id := _model_id(row))
    }
    models: list[dict[str, Any]] = []
    for available in available_rows:
        model_id = _model_id(available)
        if not model_id:
            continue
        capability = capabilities.get(model_id, {})
        visibility = str(capability.get("visibility") or available.get("visibility") or "list").lower()
        if visibility == "hide" or _is_image_model(model_id, capability, available):
            continue
        efforts = _reasoning_efforts(capability, available)
        models.append(
            {
                "id": model_id,
                "owned_by": capability.get("owned_by") or available.get("owned_by"),
                "reasoning_efforts": efforts,
                "default_reasoning_effort": _default_effort(efforts, capability, available),
                "reasoning_efforts_known": bool(efforts),
            }
        )

    if not models:
        raise ValueError("provider returned no selectable non-image models")
    return {
        "source": "provider",
        "models_authoritative": True,
        "models": models,
        "error": capabilities_error,
    }


def _cached_provider_catalog(credential: Credential, *, force_refresh: bool) -> dict[str, Any]:
    key = (credential.url, credential.fingerprint)
    now = time.monotonic()
    if not force_refresh:
        with _cache_lock:
            cached = _catalog_cache.get(key)
            if cached and cached[0] > now:
                return copy.deepcopy(cached[1])

    catalog = _fetch_provider_catalog(credential)
    with _cache_lock:
        _catalog_cache[key] = (now + _CACHE_TTL_SECONDS, copy.deepcopy(catalog))
    return catalog


def get_provider_catalog(
    default_model: str,
    default_effort: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    fallback_effort = default_effort if default_effort in _EFFORT_SET else "medium"
    try:
        credential = read_credential(credential_path())
        catalog = _cached_provider_catalog(credential, force_refresh=force_refresh)
    except Exception as exc:
        return {
            "source": "fallback",
            "models_authoritative": False,
            "models": [
                {
                    "id": default_model,
                    "owned_by": None,
                    "reasoning_efforts": [fallback_effort],
                    "default_reasoning_effort": fallback_effort,
                    "reasoning_efforts_known": False,
                }
            ],
            "error": _safe_error(exc),
        }

    for model in catalog["models"]:
        if model["reasoning_efforts"]:
            continue
        model["reasoning_efforts"] = [fallback_effort]
        model["default_reasoning_effort"] = fallback_effort
    return catalog
