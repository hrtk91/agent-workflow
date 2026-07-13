"""1 runのDB-backed diagnosis payloadとterminal表示を構築する。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_workflow.analytics.reporting import format_duration


def build_run_detail(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """1 runのcurrent stateとattempt履歴をdiagnosis向けに返す。

    処理フロー:
    - [1] run本体を取得する。
    - [2] current stepsと全attemptを実行順で取得する。
    - [3] SQLite値をJSON/text共通payloadへ正規化する。
    """

    # [1] state.jsonの代わりにcanonical runs rowを読む。
    conn.row_factory = sqlite3.Row
    run_row = conn.execute("select * from runs where run_id = ?", (run_id,)).fetchone()
    if run_row is None:
        raise ValueError(f"run not found: {run_id}")
    # [2] resume用current stateと診断用attempt履歴を分けて取得する。
    step_rows = conn.execute(
        "select * from run_steps where run_id = ? order by position",
        (run_id,),
    ).fetchall()
    attempt_rows = conn.execute(
        "select * from step_attempts where run_id = ? order by started_at, step_name, attempt",
        (run_id,),
    ).fetchall()
    # [3] SQLiteのboolean表現と派生logs pathだけを表示用へ整える。
    run = dict(run_row)
    run["logs_dir"] = str(Path(str(run["summary_path"])).parent / "logs")
    steps = [row_payload(row, boolean_fields={"timed_out"}) for row in step_rows]
    attempts = [row_payload(row, boolean_fields={"timed_out"}) for row in attempt_rows]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run": run,
        "steps": steps,
        "attempts": attempts,
    }


def render_run_detail(detail: dict[str, Any]) -> str:
    """DB-backed run detailをterminal向けに表示する。

    処理フロー:
    - [1] runの識別情報とartifact pathをheaderへ整形する。
    - [2] resume用current stepsを固定順で追加する。
    - [3] diagnosis用attempt履歴へ結果詳細を追加する。
    """

    # [1] 最初にrun全体の現在値と調査先を表示する。
    run = detail["run"]
    lines = [
        f"Agent workflow run {run['run_id']}",
        f"status: {run['status']}",
        f"current_step: {run['current_step'] or '-'}",
        f"repo: {run['repo_path']}",
        f"model: {run['model'] or '(default)'}",
        f"task_type: {run['task_type']}",
        f"summary: {run['summary_path']}",
        f"logs: {run['logs_dir']}",
        "",
        "steps:",
    ]
    # [2] current stateはstep単位の試行回数とstatusを簡潔に示す。
    for step in detail["steps"]:
        lines.append(f"  {step['step_name']}\t{step['status']}\tattempts={step['attempts']}")
    lines.extend(["", "attempts:"])
    # [3] 各attemptへduration・exit・failure・log pathを必要な場合だけ付ける。
    for attempt in detail["attempts"]:
        line = (
            f"  {attempt['step_name']}#{attempt['attempt']}\t{attempt['status']}"
            f"\tduration={format_duration(attempt['duration_seconds'])}"
        )
        if attempt["exit_code"] is not None:
            line += f"\texit={attempt['exit_code']}"
        if attempt["timed_out"]:
            line += "\ttimed_out=true"
        if attempt["failure_category"]:
            line += f"\tfailure={attempt['failure_category']}"
        if attempt["error"]:
            line += f"\terror={attempt['error']}"
        lines.append(line)
        if attempt.get("stdout_path"):
            lines.append(f"    stdout: {attempt['stdout_path']}")
        if attempt.get("stderr_path"):
            lines.append(f"    stderr: {attempt['stderr_path']}")
    return "\n".join(lines)


def row_payload(row: sqlite3.Row, *, boolean_fields: set[str]) -> dict[str, Any]:
    data = dict(row)
    for field in boolean_fields:
        data[field] = bool(data[field])
    return data
