# [DeepSWE](https://deep-swe.vercel.app)

A benchmark of 113 long-horizon software engineering tasks, drawn from 91 active open-source repositories across five languages (TypeScript, Go, Python, JavaScript, Rust). Each task ships a natural-language prompt, an isolated execution environment, and a program-based verifier that scores observable behavior and admits a range of valid solutions.

## Task format

Each `tasks/<task-id>/` directory follows the [Harbor](https://www.harborframework.com/docs/tasks) convention:

```text
task.toml         Metadata: repository, base commit, language, prebuilt image, resource limits
instruction.md    The prompt the agent sees
environment/      Dockerfile that reproduces the prebuilt image (fallback if the image is unavailable)
tests/            Verifier: test.sh (entry point) + test.patch (test additions, applied at grading time)
solution/         Reference solution (held out from the agent; for human and AI reviewers)
```

The verifier exercises the behavior the prompt describes. It accepts any solution whose observable behavior is correct, regardless of internal symbol names or structure.
The reference patch in `solution/` is never used at grading time; it exists so reviewers can spot-check correctness offline.

## Quickstart

Each `tasks/<task-id>/` directory follows the [Harbor](https://www.harborframework.com/docs/tasks) convention (`task.toml`, `instruction.md`, `environment/`, `tests/`). Use [Pier](https://github.com/datacurve-ai/pier) to run the benchmark:

```bash
git clone https://github.com/datacurve-ai/deep-swe
uv tool install datacurve-pier

# Claude Opus 4.7 via Claude Code
export ANTHROPIC_API_KEY=...
pier run -p deep-swe/tasks --agent mini-swe-agent --model anthropic/claude-opus-4-7

# GPT-5.5 via Codex
export OPENAI_API_KEY=...
pier run -p deep-swe/tasks --agent mini-swe-agent --model openai/gpt-5.5
```

## What is Pier

[Pier](https://github.com/datacurve-ai/pier) is a [Harbor](https://www.harborframework.com/docs/tasks)-compatible framework for sandboxed coding-agent evals. It began as a fork of Harbor to support CLI agents in air-gapped tasks: Harbor blocks all outbound traffic in `allow_internet = false` tasks, including dependency installs and LLM API calls. Pier adds per-agent network allowlists, giving agents only the network access they need while keeping the task environment isolated.

Pier also adds more complete trajectory metadata, a better trajectory viewer, and `pier critique run` for analyzing agent trajectories. All leaderboard scores were produced with Pier running `mini-swe-agent` on Modal.

### Agents and models

`mini-swe-agent` is model-agnostic. Pier also drives `claude-code`, `codex`, `gemini-cli`, and `opencode` directly. Pass `--env modal` to run in parallel sandboxes on Modal.

### Subsets and single tasks

Deterministic random subset of the 113-task corpus:

```bash
pier run -p deep-swe/tasks --agent mini-swe-agent --n-tasks 10 --sample-seed 0
```

Single task:

```bash
pier run -p deep-swe/tasks/<task-id> --agent mini-swe-agent
```
