"""run/attempt値をSQLite保存用の安定した表現へ正規化する。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_workflow.state import RunState


def trace_attempt_status(step_name: str, status_code: str, timed_out: bool, error: str | None) -> str:
    if status_code == "OK":
        return "succeeded"
    if timed_out:
        return "timed_out"
    message = (error or "").lower()
    if "interrupt" in message:
        return "interrupted"
    if "blocked" in message:
        return "blocked"
    if step_name == "run_qc":
        return "qc_failed"
    return "failed"


def failure_category(step_name: str, status: str, timed_out: bool) -> str | None:
    if status in {"running", "succeeded"}:
        return None
    if timed_out or status == "timed_out":
        return "timeout"
    if status == "blocked":
        return "blocked"
    if status == "interrupted":
        return "interrupted"
    if step_name == "run_qc":
        return "qc_failure"
    if step_name == "run_executor":
        return "executor_failure"
    return status


def duration_seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        started = datetime.fromisoformat(start.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (finished - started).total_seconds())


def run_finished_at(state: RunState) -> str:
    step_finishes = [step.finished_at for step in state.steps if step.finished_at]
    return max(step_finishes, default=state.updated_at)


def nanos_to_iso(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1_000_000_000, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def integer_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
