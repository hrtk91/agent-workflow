"""run/attempt値をSQLite保存用の安定した表現へ正規化する。"""

from __future__ import annotations

from datetime import datetime

from agent_workflow.state import RunState


def failure_category(
    step_name: str,
    status: str,
    timed_out: bool,
    error: str | None = None,
    exit_code: int | None = None,
) -> str | None:
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
        if exit_code in {10, 51}:
            return "provider_rate_limit"
        if exit_code == 11:
            return "provider_auth"
        if exit_code in {12, 13}:
            return "provider_unavailable"
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
