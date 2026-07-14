"""run・step状態を保存するSQLite schemaと旧table migrationを管理する。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any


def initialize_schema(conn: sqlite3.Connection) -> None:
    """raw run factsを保持するtableを作り、旧index/analytics行を移す。

    処理フロー:
    - [1] runs・run_steps・step_attemptsとmigration tableを作成する。
    - [2] 旧run_metricsのrun factsをrunsへ移す。
    - [3] 旧jobsの最新status/index値をrunsへ重ねる。
    - [4] schema作成済みversionを記録する。
    """

    # [1] 可変run状態とattempt履歴を同じtransactionで保存できるschemaを作る。
    conn.executescript(
        """
        create table if not exists storage_schema_migrations (
          version integer primary key,
          applied_at text not null
        );

        create table if not exists runs (
          run_id text primary key,
          status text not null,
          repo_path text not null,
          workflow text not null,
          verify_command text not null,
          timeout_seconds real not null,
          executor_bin text not null,
          provider text,
          model text,
          task_type text not null,
          base_ref text,
          purpose text not null,
          repair_for_run_id text,
          worktree_path text,
          current_step text,
          summary_path text not null,
          qc_repair_attempts integer not null default 0,
          created_at text not null,
          updated_at text not null,
          finished_at text,
          elapsed_seconds real,
          task_sha256 text,
          task_bytes integer,
          changed_files integer,
          additions integer,
          deletions integer
        );

        create table if not exists run_steps (
          run_id text not null,
          position integer not null,
          step_name text not null,
          status text not null,
          attempts integer not null,
          started_at text,
          finished_at text,
          exit_code integer,
          timed_out integer not null,
          error text,
          stdout_path text,
          stderr_path text,
          primary key(run_id, step_name),
          foreign key(run_id) references runs(run_id) on delete cascade
        );

        create table if not exists step_attempts (
          run_id text not null,
          step_name text not null,
          attempt integer not null,
          status text not null,
          started_at text,
          finished_at text,
          duration_seconds real,
          exit_code integer,
          timed_out integer not null,
          error text,
          failure_category text,
          stdout_path text,
          stderr_path text,
          primary key(run_id, step_name, attempt),
          foreign key(run_id) references runs(run_id) on delete cascade
        );

        create index if not exists idx_runs_model on runs(model);
        create index if not exists idx_runs_task_type on runs(task_type);
        create index if not exists idx_runs_created_at on runs(created_at);
        create index if not exists idx_runs_updated_at on runs(updated_at);
        create index if not exists idx_run_steps_run_position on run_steps(run_id, position);
        create index if not exists idx_step_attempts_run_step on step_attempts(run_id, step_name);
        """
    )

    # [1] merged schemaへ列を足すupgradeでも既存attempt履歴を作り直さない。
    ensure_column(conn, "run_steps", "stdout_path", "text")
    ensure_column(conn, "run_steps", "stderr_path", "text")
    ensure_column(conn, "step_attempts", "stdout_path", "text")
    ensure_column(conn, "step_attempts", "stderr_path", "text")
    ensure_column(conn, "runs", "qc_repair_attempts", "integer not null default 0")

    # [2] 分析tableしか残っていないrunも参照可能なraw runへ移す。
    if table_exists(conn, "run_metrics"):
        for row in table_rows(conn, "run_metrics"):
            upsert_legacy_metric(conn, row)

    # [3] status表示用indexの方が新しい可能性があるため運用値を最後に重ねる。
    if table_exists(conn, "jobs"):
        for row in table_rows(conn, "jobs"):
            upsert_legacy_job(conn, row)

    # [4] filesystem migrationとは分けて、DB schema作成済みを記録する。
    conn.execute(
        "insert or ignore into storage_schema_migrations(version, applied_at) values(1, ?)",
        (utc_now(),),
    )


def legacy_state_migration_pending(conn: sqlite3.Connection) -> bool:
    row = conn.execute("select 1 from storage_schema_migrations where version = 2").fetchone()
    return row is None


def finish_legacy_state_migration(conn: sqlite3.Connection) -> None:
    """旧state import完了後に重複tableを削除してversionを確定する。

    処理フロー:
    - [1] 役割をruns系tableへ統合した旧tableを削除する。
    - [2] filesystem import完了versionを同じtransactionへ記録する。
    """

    # [1] executescriptの暗黙commitを避け、version記録まで原子的に扱う。
    for table in ("jobs", "run_metrics", "analytics_schema_migrations"):
        conn.execute(f"drop table if exists {table}")
    # [2] 次回起動ではfilesystemを再走査しない完了markerを残す。
    conn.execute(
        "insert or ignore into storage_schema_migrations(version, applied_at) values(2, ?)",
        (utc_now(),),
    )


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (name,),
    ).fetchone() is not None


def table_rows(conn: sqlite3.Connection, name: str) -> list[dict[str, Any]]:
    cursor = conn.execute(f"select * from {name}")
    columns = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row[1]) for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")


def upsert_legacy_metric(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        insert into runs(
          run_id, status, repo_path, workflow, verify_command, timeout_seconds,
          executor_bin, provider, model, task_type, base_ref, purpose,
          repair_for_run_id, worktree_path, current_step, summary_path,
          created_at, updated_at, finished_at, elapsed_seconds,
          task_sha256, task_bytes, changed_files, additions, deletions
        ) values(?, ?, ?, ?, '', 0, ?, ?, ?, ?, ?, ?, null, null, null, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(run_id) do update set
          status=excluded.status,
          repo_path=excluded.repo_path,
          workflow=excluded.workflow,
          executor_bin=excluded.executor_bin,
          provider=excluded.provider,
          model=excluded.model,
          task_type=excluded.task_type,
          base_ref=excluded.base_ref,
          purpose=excluded.purpose,
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
            row["run_id"],
            row["status"],
            row["repo_path"],
            row["workflow"],
            row["executor_bin"],
            row.get("provider"),
            row.get("model"),
            row.get("task_type") or "unspecified",
            row.get("base_ref"),
            row.get("purpose") or "workflow",
            row["created_at"],
            row["updated_at"],
            row.get("finished_at"),
            row.get("elapsed_seconds"),
            row.get("task_sha256"),
            row.get("task_bytes"),
            row.get("changed_files"),
            row.get("additions"),
            row.get("deletions"),
        ),
    )


def upsert_legacy_job(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        insert into runs(
          run_id, status, repo_path, workflow, verify_command, timeout_seconds,
          executor_bin, provider, model, task_type, base_ref, purpose,
          repair_for_run_id, worktree_path, current_step, summary_path,
          created_at, updated_at
        ) values(?, ?, ?, 'default', '', 0, 'takt', null, null, 'unspecified', null, 'workflow',
                 null, null, ?, ?, ?, ?)
        on conflict(run_id) do update set
          status=excluded.status,
          current_step=excluded.current_step,
          repo_path=excluded.repo_path,
          summary_path=excluded.summary_path,
          updated_at=excluded.updated_at
        """,
        (
            row["run_id"],
            row["status"],
            row["repo_path"],
            row.get("current_step"),
            row.get("summary_path") or "",
            row["created_at"],
            row["updated_at"],
        ),
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
