"""SQLite analyticsの公開API。

raw run factsのread-only集計と表示helperを公開する。
"""

from agent_workflow.analytics.measurements import (
    collect_change_stats,
    excluded_metric_path,
    task_packet_identity,
)
from agent_workflow.analytics.constants import (
    GROUP_FIELDS,
    TASK_PACKET_NAMES,
    TERMINAL_RUN_STATUSES,
    TERMINAL_STEP_STATUSES,
)
from agent_workflow.analytics.normalization import (
    duration_seconds,
    failure_category,
    run_finished_at,
)
from agent_workflow.analytics.reporting import (
    display_dimension,
    format_duration,
    format_number,
    format_rate,
    rate,
    render_text_report,
    rounded_median,
    wilson_interval,
)
from agent_workflow.analytics.reporter import AnalyticsReporter
from agent_workflow.analytics.run_detail import build_run_detail, render_run_detail

__all__ = [
    "AnalyticsReporter",
    "GROUP_FIELDS",
    "TASK_PACKET_NAMES",
    "TERMINAL_RUN_STATUSES",
    "TERMINAL_STEP_STATUSES",
    "collect_change_stats",
    "build_run_detail",
    "display_dimension",
    "duration_seconds",
    "excluded_metric_path",
    "failure_category",
    "format_duration",
    "format_number",
    "format_rate",
    "rate",
    "render_run_detail",
    "render_text_report",
    "rounded_median",
    "run_finished_at",
    "task_packet_identity",
    "wilson_interval",
]
