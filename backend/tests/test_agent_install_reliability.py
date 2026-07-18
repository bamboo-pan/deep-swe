from types import SimpleNamespace

from app.pier_retry_patch.agent_install_reliability import (
    APT_RETRY_MARKER,
    CURL_RETRY_MARKER,
    EXISTING_NODE_MARKER,
    NPM_RETRY_MARKER,
    harden_install_command,
    patch_agent_install_spec,
)


def test_hardens_apt_curl_and_npm_install_steps():
    command = (
        "apt-get update && apt-get install -y curl; "
        "curl -fsSL https://example.test/install.sh | bash; "
        "npm install -g example"
    )

    hardened = harden_install_command(command)

    assert "deepswe_apt_retry update" in hardened
    assert "deepswe_apt_retry install -y curl" in hardened
    assert "Acquire::Retries=5" in hardened
    assert "--retry 8 --retry-all-errors" in hardened
    assert "npm_config_fetch_retries=8" in hardened
    assert hardened.count(APT_RETRY_MARKER) == 1
    assert hardened.count(CURL_RETRY_MARKER) == 1
    assert hardened.count(NPM_RETRY_MARKER) == 1
    assert harden_install_command(hardened) == hardened


def test_leaves_non_network_install_steps_unchanged():
    command = "ln -sf /opt/tool /usr/local/bin/tool"

    assert harden_install_command(command) == command


def test_existing_node_bypasses_codex_nvm_install():
    command = (
        "set -euo pipefail; "
        "if ldd --version 2>&1 | grep -qi musl || "
        "[ -f /etc/alpine-release ]; then"
        "  npm install -g @openai/codex@latest;"
        " else"
        "  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh "
        "| bash && npm install -g @openai/codex@latest;"
        " fi && codex --version"
    )

    hardened = harden_install_command(command)

    assert hardened.count(EXISTING_NODE_MARKER) == 1
    assert "if command -v node >/dev/null 2>&1" in hardened
    assert "node --version && npm --version && npm install -g @openai/codex@latest" in hardened
    assert "elif ldd --version" in hardened
    assert harden_install_command(hardened) == hardened


def test_existing_node_bypasses_claude_installer():
    command = (
        "if command -v apk &> /dev/null; then"
        "  npm install -g @anthropic-ai/claude-code;"
        " else curl -fsSL https://claude.ai/install.sh | bash; fi"
    )

    hardened = harden_install_command(command)

    assert "if command -v node >/dev/null 2>&1" in hardened
    assert "npm install -g @anthropic-ai/claude-code" in hardened
    assert "elif command -v apk" in hardened


def test_install_spec_wrapper_hardens_each_fresh_spec_once():
    class FakeAgent:
        def install_spec(self):
            return SimpleNamespace(
                steps=[SimpleNamespace(run="apt-get update && apt-get install -y curl")]
            )

    patch_agent_install_spec(FakeAgent)
    patch_agent_install_spec(FakeAgent)

    first = FakeAgent().install_spec().steps[0].run
    second = FakeAgent().install_spec().steps[0].run
    assert first == second
    assert first.count(APT_RETRY_MARKER) == 1
    assert "apt-get update" not in first
    assert "apt-get install" not in first
