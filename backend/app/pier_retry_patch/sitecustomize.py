"""Process-local DeepSWE customizations for the pier CLI."""

import asyncio
import json
import os
import shutil

from agent_install_reliability import patch_agent_install_spec
from global_queue import release_slot, trial_task_name, try_acquire_slot
from infrastructure_retry import write_retry_status
from local_image_retention import preserve_local_prebuilt_image
from docker_build_lock import (
    acquire_docker_build_lock, is_compose_build, release_docker_build_lock,
)
from networking import (
    add_provider_telemetry_headers, allow_provider_proxy_port,
    provider_proxy_domains, trial_network_subnets,
)
from runtime import install_retry_trial_names, install_safe_metric_display
from transient import (
    CONTEXT_LIMIT_EXCEPTION_TYPE,
    TRANSIENT_EXCEPTION_TYPE,
    TRANSIENT_VERIFIER_EXCEPTION_TYPE,
    is_context_limit_failure,
    is_transient_agent_failure,
    retry_transient_verifier,
)


def _install_agent_install_reliability() -> None:
    """Retry transient package-repository failures for CLI agents."""
    try:
        from pier.agents.installed.claude_code import ClaudeCode
        from pier.agents.installed.codex import Codex
    except ImportError:
        return

    for agent_class in (Codex, ClaudeCode):
        patch_agent_install_spec(agent_class)


_install_agent_install_reliability()


def _install_mini_context_limit_patch() -> None:
    """Stop mini-swe-agent retrying non-retryable HTTP 400 responses."""
    try:
        from pier.agents.installed.mini_swe_agent import MiniSweAgent
    except ImportError:
        return

    original_install_spec = MiniSweAgent.install_spec
    if getattr(original_install_spec, "_deepswe_context_limit_patch", False):
        return

    marker = "DEEPSWE_CONTEXT_LIMIT_PATCH"
    patch_script = f'''\n# {marker}\n"$python_bin" <<'PY'\nfrom pathlib import Path\nimport minisweagent.models.litellm_model as module\npath = Path(module.__file__)\ntext = path.read_text()\nneedle = "        litellm.exceptions.ContextWindowExceededError,\\n"\nreplacement = needle + "        litellm.exceptions.BadRequestError,\\n"\nif replacement not in text:\n    if needle not in text:\n        raise SystemExit("mini-swe-agent context abort hook not found")\n    path.write_text(text.replace(needle, replacement, 1))\nPY\n'''

    def install_spec(self):
        spec = original_install_spec(self)
        for step in spec.steps:
            if step.user == "root" and "DEEPSWE_APT_RETRY_PATCH" not in step.run:
                apt_install = "apt-get update && apt-get install -y"
                reliable_apt_install = (
                    "# DEEPSWE_APT_RETRY_PATCH\n"
                    "apt-get -o Acquire::Retries=5 "
                    "-o Acquire::http::Timeout=60 update && "
                    "apt-get -o Acquire::Retries=5 "
                    "-o Acquire::http::Timeout=60 install -y "
                    "--no-install-recommends"
                )
                step.run = step.run.replace(
                    apt_install,
                    reliable_apt_install,
                    1,
                )
            if step.user == "root" and "DEEPSWE_UV_RETRY_PATCH" not in step.run:
                step.run = (
                    "# DEEPSWE_UV_RETRY_PATCH\n"
                    "export UV_HTTP_RETRIES=10\n"
                    "export UV_HTTP_TIMEOUT=120\n"
                    + step.run.replace(
                        "curl -LsSf ",
                        "curl --retry 8 --retry-all-errors --retry-delay 2 "
                        "--connect-timeout 30 -LsSf ",
                    )
                )
            if step.user == "agent" and marker not in step.run:
                step.run = step.run.replace(
                    "mini-swe-agent --help",
                    patch_script + "mini-swe-agent --help",
                    1,
                )
        return spec

    install_spec._deepswe_context_limit_patch = True
    MiniSweAgent.install_spec = install_spec


_install_mini_context_limit_patch()


def _install_safe_docker_networks() -> None:
    """Keep Pier networks out of Docker's 192.168.* fallback address pools."""
    try:
        from pier.environments import agent_setup
    except ImportError:
        return

    original_write = agent_setup.write_docker_proxy_compose

    def write_docker_proxy_compose(*, path, proxy_dir, allowlist, token):
        # Agent containers only have the internal network and must reach the
        # host-side DeepSWE provider proxy through Pier's authenticated Squid.
        # Add the Docker host alias without opening any other destination.
        allowlist = allowlist.model_copy(update={
            "domains": provider_proxy_domains(allowlist.domains)
        })
        result = original_write(
            path=path, proxy_dir=proxy_dir, allowlist=allowlist, token=token
        )
        squid_script = proxy_dir / "start-squid.sh"
        squid_text = squid_script.read_text(encoding="utf-8")
        try:
            squid_timeout = int(
                os.environ.get("DEEPSWE_SQUID_READ_TIMEOUT_SECONDS", "1800")
            )
        except ValueError:
            squid_timeout = 1800
        squid_text = allow_provider_proxy_port(
            squid_text,
            read_timeout_seconds=squid_timeout,
        )
        raw_run_id = os.environ.get("DEEPSWE_RUN_ID")
        if raw_run_id:
            squid_text = add_provider_telemetry_headers(
                squid_text,
                run_id=int(raw_run_id),
                trial_id=path.parent.name,
            )
        squid_script.write_text(squid_text, encoding="utf-8", newline="\n")
        compose = json.loads(path.read_text(encoding="utf-8"))
        internal_subnet, external_subnet = trial_network_subnets(
            str(path.parent.resolve())
        )
        networks = compose.setdefault("networks", {})
        internal = networks.setdefault("pier-egress-internal", {})
        internal["ipam"] = {"config": [{"subnet": internal_subnet}]}
        networks["default"] = {
            "ipam": {"config": [{"subnet": external_subnet}]}
        }
        path.write_text(json.dumps(compose, indent=2), encoding="utf-8", newline="\n")
        return result

    agent_setup.write_docker_proxy_compose = write_docker_proxy_compose


_install_safe_docker_networks()


def _install_local_prebuilt_image_retention() -> None:
    """Preserve shared ``:local`` images while still deleting trial resources."""
    try:
        from pier.environments.docker.docker import DockerEnvironment
    except ImportError:
        return

    original_run_compose = DockerEnvironment._run_docker_compose_command
    if getattr(original_run_compose, "_deepswe_local_image_retention", False):
        return

    async def run_compose_preserving_local_image(
        self,
        command,
        check=True,
        timeout_sec=None,
        process_env_overrides=None,
    ):
        task_env_config = getattr(self, "task_env_config", None)
        command = preserve_local_prebuilt_image(
            command,
            image=getattr(task_env_config, "docker_image", None),
            use_prebuilt=bool(getattr(self, "_use_prebuilt", False)),
        )
        build_lock = None
        try:
            if is_compose_build(command):
                build_lock = await asyncio.to_thread(acquire_docker_build_lock)
            return await original_run_compose(
                self,
                command,
                check=check,
                timeout_sec=timeout_sec,
                process_env_overrides=process_env_overrides,
            )
        finally:
            if build_lock is not None:
                await asyncio.to_thread(release_docker_build_lock, build_lock)

    run_compose_preserving_local_image._deepswe_local_image_retention = True
    DockerEnvironment._run_docker_compose_command = (
        run_compose_preserving_local_image
    )


_install_local_prebuilt_image_retention()


def _configured_retry_delays() -> tuple[float, ...]:
    raw = os.environ.get("DEEPSWE_PIER_RETRY_DELAYS", "")
    try:
        return tuple(float(value) for value in raw.split(",") if value)
    except ValueError:
        return ()


def _install_retry_backoff() -> None:
    delays = _configured_retry_delays()
    if not delays:
        return

    try:
        from pier.trial.queue import TrialQueue
    except ImportError:
        return

    def calculate_backoff_delay(self, attempt: int) -> float:
        index = min(max(attempt, 0), len(delays) - 1)
        return delays[index]

    TrialQueue._calculate_backoff_delay = calculate_backoff_delay


_install_retry_backoff()


def _install_infrastructure_retry_telemetry() -> None:
    """Persist whole-Trial retry consumption where the Live API can read it."""
    try:
        from pier.trial.queue import TrialQueue
    except ImportError:
        return

    original_execute = TrialQueue._execute_trial_with_retries
    if getattr(original_execute, "_deepswe_retry_telemetry", False):
        return

    async def execute_trial_with_retry_telemetry(self, trial_config):
        from pier.trial.trial import Trial

        maximum = max(int(self._retry_config.max_retries), 0)
        trial_dir = trial_config.trials_dir / trial_config.trial_name
        for attempt in range(maximum + 1):
            write_retry_status(
                trial_dir,
                used=attempt,
                max_retries=maximum,
                state="running",
            )
            try:
                trial = await Trial.create(trial_config)
                self._setup_hooks(trial)
                result = await trial.run()
            except BaseException:
                write_retry_status(
                    trial_dir,
                    used=attempt,
                    max_retries=maximum,
                    state="interrupted",
                )
                raise

            failure_type = (
                result.exception_info.exception_type
                if result.exception_info is not None else None
            )
            if result.exception_info is None:
                write_retry_status(
                    trial_dir,
                    used=attempt,
                    max_retries=maximum,
                    state="completed",
                )
                return result
            if (
                not self._should_retry_exception(failure_type)
                or attempt == maximum
            ):
                write_retry_status(
                    trial_dir,
                    used=attempt,
                    max_retries=maximum,
                    state="exhausted" if attempt == maximum else "not_retryable",
                    failure_type=failure_type,
                )
                return result

            shutil.rmtree(trial.trial_dir, ignore_errors=True)
            write_retry_status(
                trial_dir,
                used=attempt + 1,
                max_retries=maximum,
                state="waiting",
                failure_type=failure_type,
            )
            delay = self._calculate_backoff_delay(attempt)
            self._logger.debug(
                f"Trial {trial_config.trial_name} failed with exception "
                f"{failure_type}. Retrying in {delay:.2f} seconds..."
            )
            await asyncio.sleep(delay)

        raise RuntimeError(
            f"Trial {trial_config.trial_name} produced no result. "
            "This should never happen."
        )

    execute_trial_with_retry_telemetry._deepswe_retry_telemetry = True
    TrialQueue._execute_trial_with_retries = execute_trial_with_retry_telemetry


_install_infrastructure_retry_telemetry()


def _install_verifier_infrastructure_retry() -> None:
    try:
        from pier.trial.trial import Trial
    except ImportError:
        return

    original_verify = getattr(Trial, "_verify_with_retry", None)
    if original_verify is None:
        return

    try:
        max_retries = max(
            int(os.environ.get("DEEPSWE_VERIFIER_INFRA_MAX_RETRIES", "0")),
            0,
        )
    except ValueError:
        max_retries = 0
    delays = _configured_retry_delays() or (1.0,)

    async def verify_with_infrastructure_retry(self):
        def log_retry(retry_number, retry_limit, delay, exc):
            logger = getattr(self, "_logger", None)
            if logger is not None:
                logger.debug(
                    "Verifier infrastructure failure "
                    f"{type(exc).__name__}; retry {retry_number}/{retry_limit} "
                    f"in {delay:.2f} seconds"
                )

        return await retry_transient_verifier(
            lambda: original_verify(self),
            max_retries=max_retries,
            delays=delays,
            on_retry=log_retry,
        )

    Trial._verify_with_retry = verify_with_infrastructure_retry


_install_verifier_infrastructure_retry()


def _install_global_trial_queue() -> None:
    database_path = os.environ.get("DEEPSWE_GLOBAL_QUEUE_DB")
    raw_run_id = os.environ.get("DEEPSWE_RUN_ID")
    if not database_path and not raw_run_id:
        return
    if not database_path or not raw_run_id:
        raise RuntimeError("Incomplete DeepSWE global queue environment")
    try:
        run_id = int(raw_run_id)
        fallback_limit = int(os.environ.get("DEEPSWE_GLOBAL_QUEUE_LIMIT", "1"))
        poll_seconds = float(os.environ.get("DEEPSWE_GLOBAL_QUEUE_POLL_SECONDS", "0.2"))
    except ValueError as exc:
        raise RuntimeError("Invalid DeepSWE global queue environment") from exc
    if run_id < 1 or fallback_limit < 1 or poll_seconds <= 0:
        raise RuntimeError("Invalid DeepSWE global queue limits")

    try:
        from pier.trial.queue import TrialQueue
    except ImportError:
        return

    async def run_trial_with_global_slot(self, trial_config):
        entry_id = None
        async with self._semaphore:
            while entry_id is None:
                entry_id = await asyncio.to_thread(
                    try_acquire_slot,
                    database_path,
                    run_id,
                    trial_task_name(trial_config),
                    trial_config.trial_name,
                    fallback_limit,
                )
                if entry_id is None:
                    await asyncio.sleep(poll_seconds)
            try:
                return await self._execute_trial_with_retries(trial_config)
            finally:
                await asyncio.shield(asyncio.to_thread(
                    release_slot, database_path, run_id, entry_id
                ))

    TrialQueue._run_trial = run_trial_with_global_slot


_install_global_trial_queue()

if os.environ.get("DEEPSWE_VERIFY_GLOBAL_QUEUE_PATCH") == "1":
    print("DEEPSWE_GLOBAL_QUEUE_PATCH_OK", flush=True)
    os._exit(0)


def _install_retry_runtime_guards() -> None:
    try:
        from pier.job import Job
    except ImportError:
        return

    install_safe_metric_display(Job)

    raw_names = os.environ.get("DEEPSWE_RETRY_TRIAL_NAMES")
    expected_job_name = os.environ.get("DEEPSWE_RETRY_JOB_NAME")
    if not raw_names and not expected_job_name:
        return
    if not raw_names or not expected_job_name:
        raise RuntimeError("Incomplete DeepSWE retry identity environment")
    try:
        trial_names = json.loads(raw_names)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid DeepSWE retry trial identity JSON") from exc
    if not isinstance(trial_names, list):
        raise RuntimeError("DeepSWE retry trial identities must be a list")
    install_retry_trial_names(Job, trial_names, expected_job_name)


_install_retry_runtime_guards()


def _install_transient_failure_classification() -> None:
    try:
        from pier.trial.trial import Trial
    except ImportError:
        return

    original_run = Trial.run

    def agent_log_tail(trial) -> str:
        try:
            agent_dir = trial._trial_paths.agent_dir
            chunks = []
            for path in agent_dir.glob("*.txt"):
                with path.open("rb") as handle:
                    handle.seek(0, 2)
                    handle.seek(max(handle.tell() - 200_000, 0))
                    chunks.append(handle.read().decode("utf-8", errors="replace"))
            return "\n".join(chunks)
        except (AttributeError, OSError):
            return ""

    async def run_with_transient_classification(self):
        result = await original_run(self)
        info = result.exception_info
        log_tail = (
            agent_log_tail(self)
            if info and info.exception_type == "NonZeroAgentExitCodeError"
            else None
        )
        if (
            info
            and is_context_limit_failure(
                info.exception_type,
                info.exception_message,
                agent_log_tail=log_tail,
            )
        ):
            info.exception_type = CONTEXT_LIMIT_EXCEPTION_TYPE
            info.exception_message = "Model input exceeded its context window"
            try:
                self._trial_paths.result_path.write_text(
                    result.model_dump_json(indent=4),
                    encoding="utf-8",
                )
            except (AttributeError, OSError) as exc:
                logger = getattr(self, "_logger", None)
                if logger is not None:
                    logger.debug(
                        "Failed to persist context-limit classification: "
                        f"{exc}"
                    )
        if (
            info
            and info.exception_type != TRANSIENT_VERIFIER_EXCEPTION_TYPE
            and is_transient_agent_failure(
                info.exception_type,
                info.exception_message,
                agent_log_tail=log_tail,
            )
        ):
            info.exception_type = TRANSIENT_EXCEPTION_TYPE
            try:
                self._trial_paths.result_path.write_text(
                    result.model_dump_json(indent=4),
                    encoding="utf-8",
                )
            except (AttributeError, OSError) as exc:
                logger = getattr(self, "_logger", None)
                if logger is not None:
                    logger.debug(
                        "Failed to persist transient exception classification: "
                        f"{exc}"
                    )
        return result

    Trial.run = run_with_transient_classification


_install_transient_failure_classification()
