"""分析データの保存・集計で共有する定数。"""

TERMINAL_RUN_STATUSES = {
    "blocked",
    "failed",
    "interrupted",
    "qc_failed",
    "recovery_exhausted",
    "succeeded",
    "timed_out",
}
TERMINAL_STEP_STATUSES = TERMINAL_RUN_STATUSES

GROUP_FIELDS = {
    "model": "model",
    "provider": "provider",
    "task_type": "task_type",
    "workflow": "workflow",
    "repo": "repo_path",
    "status": "status",
}

TASK_PACKET_NAMES = ("task.md", "acceptance.md", "constraints.md", "context.md")
