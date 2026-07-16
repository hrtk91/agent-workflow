"""agent-workflowの標準ライブラリTUI。"""

from typing import TYPE_CHECKING

from .commands import TuiCommand, parse_command
from .constants import (
    COMMAND_HELP,
    FILTER_LABELS,
    MAX_ARTIFACT_BYTES,
    MAX_CONTENT_LINES,
    MAX_LOG_LINE_CHARS,
    MAX_LOG_TAIL_BYTES,
    MENU_ITEMS,
    STATUS_COLOR_PAIRS,
    STATUS_EMOJIS,
    STATUS_LABELS,
    STATUS_SYMBOLS,
    STEP_LABELS,
)
from .content import (
    artifact_label,
    compact_timestamp,
    current_step,
    find_artifact_path,
    format_duration,
    read_artifact_lines,
    status_emoji,
    status_label,
    status_symbol,
    tail_file_lines,
    tail_lines,
    truncate_log_line,
)

if TYPE_CHECKING:
    from .app import TuiApp


def __getattr__(name: str):
    """appを遅延importし、state単独import時の循環を避ける。"""

    if name in {"TuiApp", "run_tui"}:
        from .app import TuiApp, run_tui

        return TuiApp if name == "TuiApp" else run_tui
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "COMMAND_HELP",
    "FILTER_LABELS",
    "MAX_ARTIFACT_BYTES",
    "MAX_CONTENT_LINES",
    "MAX_LOG_LINE_CHARS",
    "MAX_LOG_TAIL_BYTES",
    "MENU_ITEMS",
    "STATUS_COLOR_PAIRS",
    "STATUS_EMOJIS",
    "STATUS_LABELS",
    "STATUS_SYMBOLS",
    "STEP_LABELS",
    "TuiApp",
    "TuiCommand",
    "artifact_label",
    "compact_timestamp",
    "current_step",
    "find_artifact_path",
    "format_duration",
    "parse_command",
    "read_artifact_lines",
    "run_tui",
    "status_emoji",
    "status_label",
    "status_symbol",
    "tail_file_lines",
    "tail_lines",
    "truncate_log_line",
]
