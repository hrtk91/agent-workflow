"""step attemptの正規化保存とQC結果の判定を担当する。"""

from __future__ import annotations

import sqlite3

from agent_workflow.analytics.constants import TERMINAL_RUN_STATUSES, TERMINAL_STEP_STATUSES
from agent_workflow.analytics.normalization import duration_seconds, failure_category
from agent_workflow.state import RunState, StepState


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
