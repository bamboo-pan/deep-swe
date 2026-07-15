from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]

class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEEPSWE_UI_")
    database_url: str = f"sqlite:///{(ROOT / 'data' / 'deepswe-ui.db').as_posix()}"
    credential_file: Path = Path.home() / "Documents/github/codex1.txt"
    tasks_dir: Path = ROOT / "tasks"
    jobs_dir: Path = ROOT / "jobs"
    default_agent: str = "mini-swe-agent"
    default_model: str = "gpt-5.6-sol"
    default_effort: str = "high"
    max_parallel_tasks: int = 6
    agent_timeout_seconds: int = 5400
    verifier_timeout_seconds: int = 1800
    infrastructure_max_retries: int = 4
    agent_max_steps: int = 120
    docker_cleanup_after_run: bool = True
    docker_cleanup_on_delete: bool = True
    docker_cache_retention_hours: int = 168
    docker_cache_warning_gb: int = 20
    run_budget_usd: float = 10.0  # 整个 Run 累计费用的兜底熔断，0 表示禁用（失控体量护栏不受此开关影响）
    trial_budget_usd: float = 8.0  # 单个 Trial（一个任务的一次执行）的费用熔断，0 表示禁用

settings = AppSettings()
