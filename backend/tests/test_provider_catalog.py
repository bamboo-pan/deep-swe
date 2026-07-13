from pathlib import Path

from app import provider_catalog


def test_provider_catalog_uses_provider_models_and_reasoning_levels(monkeypatch, tmp_path: Path):
    credential = tmp_path / "credential.txt"
    credential.write_text("http://provider.test/v1\nsecret-token\n", encoding="utf-8")
    monkeypatch.setattr(provider_catalog, "credential_path", lambda: credential)
    calls: list[bool] = []

    def fake_fetch(_credential, *, client_catalog: bool):
        calls.append(client_catalog)
        if not client_catalog:
            return {
                "data": [
                    {"id": "deepseek-v4-flash", "owned_by": "deepseek"},
                    {"id": "gpt-image-2", "owned_by": "openai"},
                    {"id": "gpt-5.6-sol", "owned_by": "openai"},
                ]
            }
        return {
            "models": [
                {
                    "slug": "deepseek-v4-flash",
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [
                        {"effort": "low"},
                        {"effort": "medium"},
                        {"effort": "high"},
                    ],
                    "visibility": "list",
                },
                {
                    "slug": "gpt-image-2",
                    "supported_reasoning_levels": [{"effort": "high"}],
                    "visibility": "hide",
                },
                {
                    "slug": "gpt-5.6-sol",
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [
                        {"effort": "low"},
                        {"effort": "medium"},
                        {"effort": "high"},
                        {"effort": "xhigh"},
                        {"effort": "max"},
                    ],
                    "visibility": "list",
                },
            ]
        }

    monkeypatch.setattr(provider_catalog, "_fetch_json", fake_fetch)
    provider_catalog.clear_provider_catalog_cache()

    catalog = provider_catalog.get_provider_catalog("gpt-5.6-sol", "high")
    again = provider_catalog.get_provider_catalog("gpt-5.6-sol", "high")
    refreshed = provider_catalog.get_provider_catalog("gpt-5.6-sol", "high", force_refresh=True)

    assert [model["id"] for model in catalog["models"]] == ["deepseek-v4-flash", "gpt-5.6-sol"]
    assert catalog["models"][0]["reasoning_efforts"] == ["low", "medium", "high"]
    assert catalog["models"][1]["reasoning_efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert catalog["models"][1]["reasoning_efforts_known"] is True
    assert again == catalog
    assert refreshed == catalog
    assert calls == [False, True, False, True]


def test_provider_catalog_falls_back_without_credentials(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(provider_catalog, "credential_path", lambda: tmp_path / "missing.txt")
    provider_catalog.clear_provider_catalog_cache()

    catalog = provider_catalog.get_provider_catalog("saved-model", "xhigh")

    assert catalog["source"] == "fallback"
    assert catalog["models_authoritative"] is False
    assert catalog["models"][0]["id"] == "saved-model"
    assert catalog["models"][0]["reasoning_efforts"] == ["xhigh"]
