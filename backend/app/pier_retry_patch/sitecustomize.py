"""Process-local DeepSWE customizations for the pier CLI."""

import json
import os

from networking import trial_network_subnets
from runtime import install_retry_trial_names, install_safe_metric_display
from transient import TRANSIENT_EXCEPTION_TYPE, is_transient_agent_failure


def _install_safe_docker_networks() -> None:
    """Keep Pier networks out of Docker's 192.168.* fallback address pools."""
    try:
        from pier.environments import agent_setup
    except ImportError:
        return

    original_write = agent_setup.write_docker_proxy_compose

    def write_docker_proxy_compose(*, path, proxy_dir, allowlist, token):
        result = original_write(
            path=path, proxy_dir=proxy_dir, allowlist=allowlist, token=token
        )
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


def _install_retry_backoff() -> None:
    raw = os.environ.get("DEEPSWE_PIER_RETRY_DELAYS", "")
    try:
        delays = tuple(float(value) for value in raw.split(",") if value)
    except ValueError:
        return
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
        if info and is_transient_agent_failure(
            info.exception_type,
            f"{info.exception_message}\n{agent_log_tail(self)}",
        ):
            info.exception_type = TRANSIENT_EXCEPTION_TYPE
        return result

    Trial.run = run_with_transient_classification


_install_transient_failure_classification()
