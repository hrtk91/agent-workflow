"""step attemptの正規化保存とtrace artifactからの復元を担当する。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent_workflow.analytics.constants import TERMINAL_RUN_STATUSES, TERMINAL_STEP_STATUSES
from agent_workflow.analytics.normalization import (
    duration_seconds,
    failure_category,
    integer_or_none,
    nanos_to_iso,
    trace_attempt_status,
)
from agent_workflow.state import RunState, StepState


def recover_trace_attempts(conn: sqlite3.Connection, run_id: str, trace_path: Path) -> None:
    """正規化DB導入前のtrace.jsonlからstep attempt履歴を復元する。

    処理フロー:
    - [1] 読み取り可能なtrace.jsonlを行単位で取得する。
    - [2] agent-workflowのstep spanだけを選別する。
    - [3] 旧属性名と新OTel属性名を共通値へ正規化する。
    - [4] 復元できたattemptをSQLiteへupsertする。
    """

    # [1] traceがないrunや読み取れないrunはstateから復元できる範囲に留める。
    if not trace_path.is_file():
        return
    try:
        lines = trace_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return

    for line in lines:
        try:
            # [2] root spanや他形式のrecordを除外し、step名を取り出す。
            record = json.loads(line)
            name = str(record.get("name") or "")
            if not name.startswith("agent_workflow.step."):
                continue
            step_name = name.removeprefix("agent_workflow.step.")
            attrs = record.get("attributes") or {}
            # [3] 旧local keyと正規化済みOTel keyのどちらからでも同じ値を得る。
            attempt = int(attrs.get("agent_workflow.step.attempt") or attrs.get("attempt") or 0)
            if attempt < 1:
                continue
            timed_out = bool(attrs.get("agent_workflow.step.timed_out", attrs.get("timed_out")))
            error = str(
                attrs.get("error.message")
                or attrs.get("error")
                or (record.get("status") or {}).get("message")
                or ""
            ) or None
            status_code = str((record.get("status") or {}).get("code") or "")
            status = trace_attempt_status(step_name, status_code, timed_out, error)
            # [4] trace時刻とstatusを正規化済みattempt rowとして保存する。
            upsert_attempt_values(
                conn,
                run_id=run_id,
                step_name=step_name,
                attempt=attempt,
                status=status,
                started_at=nanos_to_iso(record.get("start_time_unix_nano")),
                finished_at=nanos_to_iso(record.get("end_time_unix_nano")),
                duration=float(record["duration_ms"]) / 1000 if record.get("duration_ms") is not None else None,
                exit_code=integer_or_none(attrs.get("process.exit.code", attrs.get("exit_code"))),
                timed_out=timed_out,
                error=error,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue


def upsert_step_attempt(conn: sqlite3.Connection, run_id: str, step: StepState) -> None:
    """stateが保持する最新step attemptを正規化して保存する。"""

    upsert_attempt_values(
        conn,
        run_id=run_id,
        step_name=step.name,
        attempt=step.attempts,
        status=step.status,
        started_at=step.started_at,
        finished_at=step.finished_at,
        duration=duration_seconds(step.started_at, step.finished_at),
        exit_code=step.exit_code,
        timed_out=step.timed_out,
        error=step.error,
    )


def upsert_attempt_values(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    step_name: str,
    attempt: int,
    status: str,
    started_at: str | None,
    finished_at: str | None,
    duration: float | None,
    exit_code: int | None,
    timed_out: bool,
    error: str | None,
) -> None:
    """安定したrun/step/attempt識別子ごとに1つの可変行を保存する。

    処理フロー:
    - [1] step名と終了状態から失敗カテゴリを正規化する。
    - [2] 開始時刻を保持しつつ、最新の終了結果でattempt行をupsertする。
    """

    # [1] raw statusとは別に、横断集計しやすいfailure categoryを確定する。
    category = failure_category(step_name, status, timed_out)
    # [2] 同じattemptのrunning→terminal更新では初回のstarted_atを維持する。
    conn.execute(
        """
        insert into step_attempts(
          run_id, step_name, attempt, status, started_at, finished_at, duration_seconds,
          exit_code, timed_out, error, failure_category
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(run_id, step_name, attempt) do update set
          status=excluded.status,
          started_at=coalesce(step_attempts.started_at, excluded.started_at),
          finished_at=excluded.finished_at,
          duration_seconds=excluded.duration_seconds,
          exit_code=excluded.exit_code,
          timed_out=excluded.timed_out,
          error=excluded.error,
          failure_category=excluded.failure_category
        """,
        (
            run_id,
            step_name,
            attempt,
            status,
            started_at,
            finished_at,
            duration,
            exit_code,
            int(timed_out),
            error,
            category,
        ),
    )


def qc_outcomes(conn: sqlite3.Connection, state: RunState) -> tuple[int | None, int | None]:
    """attempt履歴から初回QCと最終QCの成否を判定する。

    処理フロー:
    - [1] run_qc attemptを実行順に取得する。
    - [2] attempt 1がterminalならfirst-pass結果を確定する。
    - [3] いずれかの成功、またはrunのterminal失敗からeventual結果を確定する。
    """

    # [1] resume/retryを含む通算attempt番号の順でQC履歴を読む。
    rows = conn.execute(
        "select attempt, status from step_attempts where run_id = ? and step_name = 'run_qc' order by attempt",
        (state.run_id,),
    ).fetchall()
    # [2] 初回attemptが未完了の場合は成功率の分母へ入れない。
    first_pass: int | None = None
    for attempt, status in rows:
        if int(attempt) == 1 and str(status) in TERMINAL_STEP_STATUSES:
            first_pass = int(status == "succeeded")
            break
    # [3] 後続attemptで一度でも成功すればeventual successとする。
    eventual: int | None = None
    if any(str(status) == "succeeded" for _, status in rows):
        eventual = 1
    elif state.status in TERMINAL_RUN_STATUSES and state.step("run_qc").attempts > 0:
        eventual = 0
    return first_pass, eventual


def needs_refresh(conn: sqlite3.Connection, state: RunState) -> bool:
    """state更新時刻とattempt件数からartifact再走査の要否を判定する。

    処理フロー:
    - [1] run_metricsがない、またはupdated_atが異なるrunを更新対象にする。
    - [2] 時刻が同じ場合も、保存済みattempt数がstateより少なければ更新対象にする。
    """

    # [1] 未登録または更新されたstateは詳細比較せずrefreshする。
    row = conn.execute("select updated_at from run_metrics where run_id = ?", (state.run_id,)).fetchone()
    if row is None or str(row[0]) != state.updated_at:
        return True
    # [2] trace backfill途中などattempt履歴が不足するrunだけ再処理する。
    recorded_attempts = int(
        conn.execute("select count(*) from step_attempts where run_id = ?", (state.run_id,)).fetchone()[0]
    )
    return recorded_attempts < sum(step.attempts for step in state.steps)
