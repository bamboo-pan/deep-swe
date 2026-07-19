# [DeepSWE](https://deepswe.datacurve.ai/)

DeepSWE is a benchmark for measuring frontier coding agents on original, long-horizon software engineering tasks drawn from active open-source repositories. The benchmark includes 113 tasks across TypeScript, Go, Python, JavaScript, and Rust, with isolated environments and program-based verifiers.

## Task format

DeepSWE tasks use the [Harbor](https://www.harborframework.com/docs/tasks) task format:

```text
task.toml         Metadata (repo, base commit, language, image, limits)
instruction.md    The prompt the agent sees
pre_artifacts.sh  Captures the agent's committed work as a patch
environment/      Dockerfile reproducing the prebuilt image
tests/            Verifier entry point, held-out tests, and grader config
solution/         Reference solution (held out from the agent)
```

The verifier exercises the behavior the prompt describes. It accepts any solution whose observable behavior is correct, regardless of internal symbol names or structure.
The reference patch in `solution/` is never used at grading time; it exists so reviewers can spot-check correctness offline.

Since v1.1, grading uses Harbor's [separate verifier environment](https://www.harborframework.com/docs/tasks#verifier-environment-shared-vs-separate), requiring [Pier >=0.3.0](https://pypi.org/project/datacurve-pier/). The agent works in an isolated environment and commits its work upon completion. Pier then runs a `pre_artifacts.sh` script to extract these commits as a patch, which is applied and graded in a pristine container.

The verifier produces the following outputs for each run:

```text
verifier/
    reward.json      Structured scores (binary reward + pass fractions)
    ctrf.json        Machine-readable test report with failure messages
    test-stdout.txt  Raw suite output and a list of failure reasons
    run.log          Raw stdout/stderr captured during the run
    reports/         Framework-native report/log files from the grader
```

## Quickstart

Use [Pier](https://github.com/datacurve-ai/pier) to run the benchmark:

```bash
git clone https://github.com/datacurve-ai/deep-swe
uv tool install datacurve-pier

# Claude Opus 4.8
export ANTHROPIC_API_KEY=...
pier run -p deep-swe/tasks --agent mini-swe-agent --model anthropic/claude-opus-4-8

# GPT-5.5
export OPENAI_API_KEY=...
pier run -p deep-swe/tasks --agent mini-swe-agent --model openai/gpt-5.5
```

### Local test-task images

The two `[TEST]` Python tasks use stable Docker tags ending in `:local`. Before
each UI run, the backend checks the Dockerfile/build-context checksum and
automatically builds missing or stale environment and verifier images. Build
logs are written to `data/local-image-builds/`. No separate image-preparation
command is required.

Later runs reuse an image while its checksum matches; changing a Dockerfile,
fixture, verifier file, or a referenced local base image triggers the affected
rebuild. Docker images stay in the local Docker daemon and should not be
committed as `.tar` archives.

The `:local` tags are machine-local. On a fresh checkout, the first selected UI
run performs the initial build before any model call; normal reruns use the
existing images and avoid remote base-image metadata delays.
For a separate verifier, the runtime keeps a shared `:local` verifier tag after
each trial (containers and temporary resources are still removed), so concurrent
or later trials do not try to pull that image from a registry. Pier may still
build the per-agent installation layer on the first run; BuildKit reuses that
layer on later runs while the agent fingerprint and task base image are unchanged.

### Provider request limits and retries

Every model HTTP request from Codex, Claude Code, and mini-swe-agent is routed
through the local DeepSWE provider proxy. Settings provide two independent
limits:

- **Provider RPM**: a shared rolling 60-second request window. Requests are sent
  immediately while the window has capacity and are queued after it fills.
- **Provider max active requests**: a cap on simultaneous upstream Provider
  connections. `0` disables either limit.

The proxy also owns the application-configured retry policy. The retry count
does not include the initial request; retryable connection failures and HTTP
`429`, `500`, `502`, `503`, and `504` responses wait for the configured fixed
interval and then make a new request. Each retry consumes RPM quota. A request
releases its concurrency slot before entering the retry wait, so sleeping
retries do not occupy active Provider connections.

For new runs, the configurable mini-swe-agent/LiteLLM and Codex request retries
are disabled to avoid multiplying the proxy retry count. Pier's whole-trial
infrastructure retry remains a separate recovery layer. A successful SSE stream
is buffered by the proxy until its protocol terminal event is seen; if the
stream ends early, the incomplete response is discarded and the original
request is retried up to `provider_stream_max_retries` times (default: 3). The
proxy emits SSE comments while buffering so Codex, Claude Code, and
mini-swe-agent do not time out. Only the complete response is delivered to the
agent, preventing duplicate tool calls; after stream retries are exhausted,
Pier's whole-trial infrastructure retry takes over.

The Live page shows RPM usage and queueing together with active Provider
requests, concurrency waiters, and the configured retry policy. Provider `429`
and `model_cooldown` responses also appear as automatic UI notifications.

For network-isolated Pier trials, the agent reaches the host-side proxy through
Pier's authenticated Squid service. `host.docker.internal` and port `8765` are
added narrowly to that proxy policy; the task container does not receive general
internet access. If this exception is missing, Squid returns `ERR_ACCESS_DENIED`
before the request reaches DeepSWE or the upstream provider.

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

