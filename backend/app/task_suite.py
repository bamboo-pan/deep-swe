DEFAULT_TASK_SUITE_NAME = "frontier-regression-4"

WORKFLOW_VALIDATION_TASKS = [
    "test-python-slugify-workflow",
    "test-python-summary-workflow",
]

# Ordered from broad algorithm/API probes to the high-pass control task.
REGRESSION_TASKS = [
    "etree-xml-diff-patch",
    "psd-tools-blend-range-api",
    "boa-hierarchical-evaluation-cancellation",
    "sql-formatter-bigquery-pipe-formatting",
]

INTELLIGENCE_LADDER_TASKS = [
    "sql-formatter-bigquery-pipe-formatting",
    "task-task-graph-export",
    "testem-per-launcher-reports",
    "bandit-incremental-cache-control",
    "boa-hierarchical-evaluation-cancellation",
    "koota-composite-trait-aspects",
]

TASK_PRESETS = [
    {
        "id": "workflow-validation",
        "name": "测试任务",
        "description": "验证任务镜像、Agent 提交、Patch 提取和 Verifier 评分链路。",
        "strategy": "自定义 Python · 完整运行链路",
        "attempts_per_task": 1,
        "tasks": WORKFLOW_VALIDATION_TASKS,
    },
    {
        "id": "degradation-regression",
        "name": "降智验证",
        "description": "用于复测 5.6 SOL max/xhigh、Fable 5、Opus 4.8 和 GPT-5.5 等常用模型。",
        "strategy": "Go · Python · Rust · TypeScript",
        "attempts_per_task": 4,
        "tasks": REGRESSION_TASKS,
    },
    {
        "id": "intelligence-ladder",
        "name": "智力梯队快测",
        "description": "按官方通过率从 80% 到 30% 递进，快速区分当前模型所处梯队。",
        "strategy": "五种语言 · 官方难度 80% → 30%",
        "score_guide": "6=S · 5=A · 4=B · 2-3=C · 0-1=D",
        "attempts_per_task": 1,
        "tasks": INTELLIGENCE_LADDER_TASKS,
    },
]

# Backward-compatible name used by regression alerts and older clients.
DEFAULT_TASKS = REGRESSION_TASKS
CONTROL_TASK = "sql-formatter-bigquery-pipe-formatting"
