"""Small, process-local Pier runtime guards used by ``sitecustomize``."""

from __future__ import annotations

import logging
from pathlib import Path


LOGGER = logging.getLogger("deepswe.pier_patch")


def valid_trial_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and "__" in value
        and 1 <= len(value) <= 300
        and value not in {".", ".."}
        and Path(value).name == value
    )


def install_retry_trial_names(
    job_class,
    trial_names: list[str],
    expected_job_name: str,
) -> None:
    """Assign deterministic names to the trials in one DeepSWE retry job."""
    if not trial_names or not all(valid_trial_name(name) for name in trial_names):
        raise RuntimeError("Invalid DeepSWE retry trial names")
    if not isinstance(expected_job_name, str) or Path(expected_job_name).name != expected_job_name:
        raise RuntimeError("Invalid DeepSWE retry job name")

    original = job_class._init_trial_configs

    def init_trial_configs(self):
        original(self)
        if str(self.config.job_name) != expected_job_name:
            return
        if len(self._trial_configs) != len(trial_names):
            raise RuntimeError(
                "DeepSWE retry trial count mismatch: "
                f"expected {len(trial_names)}, got {len(self._trial_configs)}"
            )
        for trial_config, trial_name in zip(self._trial_configs, trial_names):
            trial_config.trial_name = trial_name

    job_class._init_trial_configs = init_trial_configs


def install_safe_metric_display(job_class) -> None:
    """Keep progress-only metric failures from cancelling sibling trials."""
    original = job_class._update_metric_display

    def update_metric_display(self, event, loading_progress, loading_progress_task):
        try:
            dataset_name = event.config.task.source or "adhoc"
            metrics = getattr(self, "_metrics", None)
            if not metrics or not metrics.get(dataset_name):
                return None
            return original(self, event, loading_progress, loading_progress_task)
        except Exception:
            LOGGER.warning(
                "Pier metric progress display failed; trial results remain valid",
                exc_info=True,
            )
            return None

    job_class._update_metric_display = update_metric_display
