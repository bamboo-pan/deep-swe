"""Harden network-dependent Pier agent installation steps."""

from __future__ import annotations

from typing import Any


APT_RETRY_MARKER = "DEEPSWE_APT_RETRY_PATCH"
CURL_RETRY_MARKER = "DEEPSWE_CURL_RETRY_PATCH"
NPM_RETRY_MARKER = "DEEPSWE_NPM_RETRY_PATCH"
EXISTING_NODE_MARKER = "DEEPSWE_EXISTING_NODE_PATCH"

_APT_RETRY_HELPER = f'''# {APT_RETRY_MARKER}
deepswe_apt_retry() {{
  attempt=1
  while ! apt-get \
    -o Acquire::Retries=5 \
    -o Acquire::http::Timeout=60 \
    -o Acquire::https::Timeout=60 \
    "$@"; do
    if [ "$attempt" -ge 8 ]; then
      return 1
    fi
    sleep "$((attempt * 2))"
    attempt="$((attempt + 1))"
  done
}}
'''

_NPM_RETRY_ENV = f'''# {NPM_RETRY_MARKER}
export npm_config_fetch_retries=8
export npm_config_fetch_retry_mintimeout=2000
export npm_config_fetch_retry_maxtimeout=120000
'''

_CURL_REPLACEMENTS = {
    "curl -o- ": (
        "curl --retry 8 --retry-all-errors --retry-delay 2 "
        "--connect-timeout 30 -o- "
    ),
    "curl -fsSL ": (
        "curl --retry 8 --retry-all-errors --retry-delay 2 "
        "--connect-timeout 30 -fsSL "
    ),
    "curl -LsSf ": (
        "curl --retry 8 --retry-all-errors --retry-delay 2 "
        "--connect-timeout 30 -LsSf "
    ),
}

_NODE_INSTALL_BRANCHES = (
    (
        "ldd --version 2>&1 | grep -qi musl || [ -f /etc/alpine-release ]",
        "npm install -g @openai/codex",
    ),
    (
        "command -v apk &> /dev/null",
        "npm install -g @anthropic-ai/claude-code",
    ),
)


def _prefer_existing_node(command: str) -> tuple[str, bool]:
    if EXISTING_NODE_MARKER in command:
        return command, False
    for condition, install_prefix in _NODE_INSTALL_BRANCHES:
        branch_start = f"if {condition}; then"
        start = command.find(branch_start)
        if start < 0:
            continue
        install_start = start + len(branch_start)
        branch_end = command.find("; else", install_start)
        if branch_end < 0:
            continue
        install = command[install_start:branch_end].strip()
        if not install.startswith(install_prefix):
            continue
        replacement = (
            "if command -v node >/dev/null 2>&1 && "
            "command -v npm >/dev/null 2>&1; then"
            f"  node --version && npm --version && {install};"
            f" elif {condition}; then  {install}; else"
        )
        return command[:start] + replacement + command[branch_end + len("; else"):], True
    return command, False


def harden_install_command(command: str) -> str:
    """Add bounded retries without changing non-network install steps."""
    hardened = command
    prefixes: list[str] = []

    hardened, existing_node_changed = _prefer_existing_node(hardened)
    if existing_node_changed:
        prefixes.append(f"# {EXISTING_NODE_MARKER}\n")

    if "apt-get " in hardened and APT_RETRY_MARKER not in hardened:
        hardened = hardened.replace("apt-get update", "deepswe_apt_retry update")
        hardened = hardened.replace("apt-get install", "deepswe_apt_retry install")
        prefixes.append(_APT_RETRY_HELPER)

    if CURL_RETRY_MARKER not in hardened:
        curl_changed = False
        for original, replacement in _CURL_REPLACEMENTS.items():
            if original in hardened:
                hardened = hardened.replace(original, replacement)
                curl_changed = True
        if curl_changed:
            prefixes.append(f"# {CURL_RETRY_MARKER}\n")

    if "npm install" in hardened and NPM_RETRY_MARKER not in hardened:
        prefixes.append(_NPM_RETRY_ENV)

    return "".join(prefixes) + hardened


def patch_agent_install_spec(agent_class: type[Any]) -> None:
    """Wrap one Pier installed-agent class exactly once."""
    original_install_spec = agent_class.install_spec
    if getattr(original_install_spec, "_deepswe_install_reliability_patch", False):
        return

    def install_spec(self):
        spec = original_install_spec(self)
        for step in spec.steps:
            step.run = harden_install_command(step.run)
        return spec

    install_spec._deepswe_install_reliability_patch = True
    agent_class.install_spec = install_spec
