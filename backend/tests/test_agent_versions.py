from types import SimpleNamespace

import pytest

from app import agent_versions


def test_local_catalog_reads_pinned_and_legacy_latest_build_cache(monkeypatch):
    output = """
Description: mount / from exec /bin/bash -c npm install -g @openai/codex@0.144.6
Description: mount / from exec /bin/bash -c uv tool install mini-swe-agent
Description: mount / from exec /bin/bash -c npm install -g @anthropic-ai/claude-code;
"""
    monkeypatch.setattr(agent_versions, "_local_cache", None)
    monkeypatch.setattr(
        agent_versions.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output),
    )
    monkeypatch.setattr(
        agent_versions,
        "_latest_observed_versions",
        lambda: {"mini-swe-agent": "2.4.5", "claude-code": "2.1.214"},
    )

    assert agent_versions._scan_local_versions() == {
        "mini-swe-agent": ["2.4.5"],
        "codex": ["0.144.6"],
        "claude-code": ["2.1.214"],
    }


def test_resolve_local_version_refuses_missing_cache():
    catalog = {
        "agents": {
            "codex": {
                "latest": "0.144.6",
                "local_versions": ["0.144.5"],
                "error": None,
            }
        }
    }
    with pytest.raises(ValueError, match="不在本地 Docker 构建缓存"):
        agent_versions.resolve_agent_version(
            "codex",
            {"mode": "local", "version": "0.144.6"},
            catalog=catalog,
        )


def test_resolve_latest_freezes_registry_version():
    catalog = {
        "agents": {
            "codex": {
                "latest": "0.144.6",
                "local_versions": ["0.144.5"],
                "error": None,
            }
        }
    }
    assert agent_versions.resolve_agent_version(
        "codex", {"mode": "latest", "version": None}, catalog=catalog
    ) == {"mode": "latest", "version": "0.144.6", "source": "registry"}
