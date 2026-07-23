"""run状態とstep attemptを単一SQLite transactionで保存・復元する。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent_workflow.analytics.constants import TERMINAL_RUN_STATUSES
from agent_workflow.analytics.measurements import collect_change_stats, task_packet_identity
from agent_workflow.analytics.normalization import duration_seconds, failure_category, run_finished_at
from agent_workflow.state import WORKFLOW_STEPS, RunState, StepState
from agent_workflow.storage.schema import (
    finish_legacy_state_migration,
    initialize_schema,
    legacy_state_migration_pending,
)


class RunStore:
    """SQLiteを唯一の正本としてrun lifecycleを永続化する。"""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir.expanduser()
        self.db_path = self.state_dir / "jobs.sqlite"
        self.runs_dir = self.state_dir / "runs"

    def initialize(self) -> int:
        """schemaを初期化し、旧state.jsonを一度だけDBへ移す。

        処理フロー:
        - [1] 新schemaを作り、旧jobs/run_metricsのfactsを移す。
        - [2] 未移行の場合だけ既存state.jsonをrun単位で保存する。
        - [3] 欠けているcurrent stepをattempt履歴から補う。
        - [4] 旧tableを削除し、filesystem migration完了を記録する。
        """

        # [1] DBだけで復元できるtableを先に用意する。
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            initialize_schema(conn)
            pending = legacy_state_migration_pending(conn)
        if not pending:
            return 0

        # [2] 旧versionが残したsnapshotを正規化してupsertする。
        imported = 0
        if self.runs_dir.is_dir():
            for state_path in sorted(self.runs_dir.glob("*/state.json")):
                try:
                    state = RunState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                self.save(state)
                imported += 1

        with self._db() as conn:
            # [3] state fileがない旧runも、attemptの最新値からcurrent stepを復元する。
            self._hydrate_missing_run_steps(conn)
            # [4] 以後はruns/run_stepsだけを正本として使う。
            finish_legacy_state_migration(conn)
        return imported

    def save(self, state: RunState) -> None:
        """run・current steps・attempt履歴を1 transactionで保存する。

        処理フロー:
        - [1] transaction外で入力hashとterminal変更量を計測する。
        - [2] run本体をupsertし、初回計測値を保持する。
        - [3] 全stepのcurrent stateを保存する。
        - [4] 開始済みattemptを履歴tableへupsertする。
        """

        # [1] Git/filesystem I/OでSQLite write lockを長時間保持しない。
        task_sha256, task_bytes = task_packet_identity(Path(state.task_dir))
        change_stats = collect_change_stats(state) if state.status in TERMINAL_RUN_STATUSES else None
        finished_at = run_finished_at(state) if state.status in TERMINAL_RUN_STATUSES else None
        elapsed = duration_seconds(state.created_at, finished_at)
        changed_files, additions, deletions = change_stats or (None, None, None)

        with self._db() as conn:
            # [2] 可変run状態を更新し、入力・変更量の最初の確定値だけを残す。
            conn.execute(
                """
                insert into runs(
                  run_id, status, repo_path, workflow, verify_command, timeout_seconds,
                  executor_bin, provider, model, task_type, base_ref, purpose,
                  repair_for_run_id, queue_job_id, worktree_path, current_step, summary_path, qc_repair_attempts,
                  created_at, updated_at, finished_at, elapsed_seconds,
                  task_sha256, task_bytes, changed_files, additions, deletions
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(run_id) do update set
                  status=excluded.status,
                  repo_path=excluded.repo_path,
                  workflow=excluded.workflow,
                  verify_command=excluded.verify_command,
                  timeout_seconds=excluded.timeout_seconds,
                  executor_bin=excluded.executor_bin,
                  provider=excluded.provider,
                  model=excluded.model,
                  task_type=excluded.task_type,
                  base_ref=excluded.base_ref,
                  purpose=excluded.purpose,
                  queue_job_id=excluded.queue_job_id,
                  repair_for_run_id=excluded.repair_for_run_id,
                  worktree_path=excluded.worktree_path,
                  current_step=excluded.current_step,
                  summary_path=excluded.summary_path,
                  qc_repair_attempts=excluded.qc_repair_attempts,
                  updated_at=excluded.updated_at,
                  finished_at=excluded.finished_at,
                  elapsed_seconds=excluded.elapsed_seconds,
                  task_sha256=coalesce(runs.task_sha256, excluded.task_sha256),
                  task_bytes=coalesce(runs.task_bytes, excluded.task_bytes),
                  changed_files=coalesce(runs.changed_files, excluded.changed_files),
                  additions=coalesce(runs.additions, excluded.additions),
                  deletions=coalesce(runs.deletions, excluded.deletions)
                """,
                (
                    state.run_id,
                    state.status,
                    state.repo_path,
                    state.workflow,
                    state.verify_command,
                    state.timeout_seconds,
                    state.executor_bin,
                    state.provider,
                    state.model,
                    state.task_type,
                    state.base_ref,
                    state.purpose,
                    state.repair_for_run_id,
                    state.queue_job_id,
                    state.worktree_path,
                    state.current_step,
                    state.summary_path,
                    state.qc_repair_attempts,
                    state.created_at,
                    state.updated_at,
                    finished_at,
                    elapsed,
                    task_sha256,
                    task_bytes,
                    changed_files,
                    additions,
                    deletions,
                ),
            )

            # [3] retryによるpending resetも含め、現在値を全step分保存する。
            for position, step in enumerate(state.steps):
                self._upsert_run_step(conn, state.run_id, position, step)
                # [4] pending以外の開始済みattemptだけを履歴へ反映する。
                if step.attempts > 0 and step.status != "pending":
                    self._upsert_step_attempt(conn, state.run_id, step)

    def load(self, run_id: str) -> RunState:
        """runsとrun_stepsからrunner用RunStateを復元する。

        処理フロー:
        - [1] run本体とcurrent step rowsを同じDB snapshotから取得する。
        - [2] step rowsをrunnerのdomain objectへ戻す。
        - [3] file artifactの配置規約を組み合わせてRunStateを構築する。
        """

        # [1] filesystem snapshotを参照せず、canonical tablesだけを読む。
        with self._db() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin")
            row = conn.execute("select * from runs where run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(f"run not found: {run_id}")
            step_rows = conn.execute(
                "select * from run_steps where run_id = ? order by position",
                (run_id,),
            ).fetchall()
        run_dir = self.runs_dir / run_id
        # [2] migration由来でcurrent stepsがないrunには既定step列を補う。
        steps = [self._step_from_row(item) for item in step_rows]
        if not steps:
            steps = [StepState(name=name) for name in WORKFLOW_STEPS]
        # [3] task・summaryなどimmutable artifact pathはrun IDから導出する。
        return RunState(
            run_id=str(row["run_id"]),
            status=str(row["status"]),
            repo_path=str(row["repo_path"]),
            run_dir=str(run_dir),
            task_dir=str(run_dir / "task"),
            workflow=str(row["workflow"]),
            verify_command=str(row["verify_command"]),
            timeout_seconds=float(row["timeout_seconds"]),
            executor_bin=str(row["executor_bin"]),
            provider=str(row["provider"]) if row["provider"] is not None else None,
            model=str(row["model"]) if row["model"] is not None else None,
            task_type=str(row["task_type"]),
            base_ref=str(row["base_ref"]) if row["base_ref"] is not None else None,
            purpose=str(row["purpose"]),
            repair_for_run_id=str(row["repair_for_run_id"]) if row["repair_for_run_id"] is not None else None,
            queue_job_id=str(row["queue_job_id"]) if row["queue_job_id"] is not None else None,
            worktree_path=str(row["worktree_path"]) if row["worktree_path"] is not None else None,
            summary_path=str(row["summary_path"] or run_dir / "summary.md"),
            current_step=str(row["current_step"]) if row["current_step"] is not None else None,
            qc_repair_attempts=int(row["qc_repair_attempts"] or 0),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            steps=steps,
        )

    def purpose(self, run_id: str) -> str:
        with self._db() as conn:
            row = conn.execute("select purpose from runs where run_id = ?", (run_id,)).fetchone()
        return str(row[0]) if row else "workflow"

    def _upsert_run_step(self, conn: sqlite3.Connection, run_id: str, position: int, step: StepState) -> None:
        conn.execute(
            """
            insert into run_steps(
              run_id, position, step_name, status, attempts, started_at, finished_at,
              exit_code, timed_out, error, stdout_path, stderr_path
            ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(run_id, step_name) do update set
              position=excluded.position,
              status=excluded.status,
              attempts=excluded.attempts,
              started_at=excluded.started_at,
              finished_at=excluded.finished_at,
              exit_code=excluded.exit_code,
              timed_out=excluded.timed_out,
              error=excluded.error,
              stdout_path=excluded.stdout_path,
              stderr_path=excluded.stderr_path
            """,
            (
                run_id,
                position,
                step.name,
                step.status,
                step.attempts,
                step.started_at,
                step.finished_at,
                step.exit_code,
                int(step.timed_out),
                step.error,
                step.stdout_path,
                step.stderr_path,
            ),
        )

    def _upsert_step_attempt(self, conn: sqlite3.Connection, run_id: str, step: StepState) -> None:
        category = failure_category(step.name, step.status, step.timed_out)
        conn.execute(
            """
            insert into step_attempts(
              run_id, step_name, attempt, status, started_at, finished_at, duration_seconds,
              exit_code, timed_out, error, failure_category, stdout_path, stderr_path
            ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(run_id, step_name, attempt) do update set
              status=excluded.status,
              started_at=coalesce(step_attempts.started_at, excluded.started_at),
              finished_at=excluded.finished_at,
              duration_seconds=excluded.duration_seconds,
              exit_code=excluded.exit_code,
              timed_out=excluded.timed_out,
              error=excluded.error,
              failure_category=excluded.failure_category,
              stdout_path=excluded.stdout_path,
              stderr_path=excluded.stderr_path
            """,
            (
                run_id,
                step.name,
                step.attempts,
                step.status,
                step.started_at,
                step.finished_at,
                duration_seconds(step.started_at, step.finished_at),
                step.exit_code,
                int(step.timed_out),
                step.error,
                category,
                step.stdout_path,
                step.stderr_path,
            ),
        )

    def _hydrate_missing_run_steps(self, conn: sqlite3.Connection) -> None:
        """旧attempt履歴だけを持つrunへcurrent step rowsを補う。

        処理フロー:
        - [1] current stepを持たないrunを抽出する。
        - [2] 各workflow stepの最新attemptをcurrent stateへ変換する。
        - [3] attemptがないstepもpending rowとして保存する。
        """

        # [1] state.jsonがない旧runだけを対象にする。
        run_ids = [str(row[0]) for row in conn.execute("select run_id from runs").fetchall()]
        for run_id in run_ids:
            existing = conn.execute("select count(*) from run_steps where run_id = ?", (run_id,)).fetchone()
            if existing and int(existing[0]) > 0:
                continue
            for position, name in enumerate(WORKFLOW_STEPS):
                # [2] resume時に必要なcurrent stateとして最新attemptを選ぶ。
                row = conn.execute(
                    """
                    select step_name, status, attempt, started_at, finished_at, exit_code,
                           timed_out, error, stdout_path, stderr_path
                    from step_attempts
                    where run_id = ? and step_name = ?
                    order by attempt desc
                    limit 1
                    """,
                    (run_id, name),
                ).fetchone()
                step = StepState(name=name)
                if row:
                    step = StepState(
                        name=str(row[0]),
                        status=str(row[1]),
                        attempts=int(row[2]),
                        started_at=str(row[3]) if row[3] is not None else None,
                        finished_at=str(row[4]) if row[4] is not None else None,
                        exit_code=int(row[5]) if row[5] is not None else None,
                        timed_out=bool(row[6]),
                        error=str(row[7]) if row[7] is not None else None,
                        stdout_path=str(row[8]) if row[8] is not None else None,
                        stderr_path=str(row[9]) if row[9] is not None else None,
                    )
                # [3] 全stepを固定順で揃え、未実行stepも明示的にpendingとする。
                self._upsert_run_step(conn, run_id, position, step)

    @staticmethod
    def _step_from_row(row: sqlite3.Row) -> StepState:
        return StepState(
            name=str(row["step_name"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            started_at=str(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
            exit_code=int(row["exit_code"]) if row["exit_code"] is not None else None,
            timed_out=bool(row["timed_out"]),
            error=str(row["error"]) if row["error"] is not None else None,
            stdout_path=str(row["stdout_path"]) if row["stdout_path"] is not None else None,
            stderr_path=str(row["stderr_path"]) if row["stderr_path"] is not None else None,
        )

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("pragma journal_mode=wal")
        conn.execute("pragma foreign_keys=on")
        return conn
