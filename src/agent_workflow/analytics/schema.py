"""jobs.sqliteへ追加するanalytics schemaを管理する。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def initialize_schema(conn: sqlite3.Connection) -> None:
    """既存jobs.sqliteへversion管理された分析schemaを初期化する。

    処理フロー:
    - [1] run・attemptテーブルと検索indexを冪等に作成する。
    - [2] 適用済みschema versionを記録する。
    """

    # [1] operational tableを変更せず、分析用tableを同じDBへ追加する。
    conn.executescript(
        """
        create table if not exists analytics_schema_migrations (
          version integer primary key,
          applied_at text not null
        );

        create table if not exists run_metrics (
          run_id text primary key,
          status text not null,
          purpose text not null,
          repo_path text not null,
          workflow text not null,
          executor_bin text not null,
          provider text,
          model text,
          task_type text not null,
          base_ref text,
          qc_profile_hash text not null,
          task_sha256 text,
          task_bytes integer,
          created_at text not null,
          updated_at text not null,
          finished_at text,
          elapsed_seconds real,
          executor_attempts integer not null,
          qc_attempts integer not null,
          first_pass_qc integer,
          eventual_qc integer,
          changed_files integer,
          additions integer,
          deletions integer
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
          primary key(run_id, step_name, attempt)
        );

        create index if not exists idx_run_metrics_model on run_metrics(model);
        create index if not exists idx_run_metrics_task_type on run_metrics(task_type);
        create index if not exists idx_run_metrics_created_at on run_metrics(created_at);
        create index if not exists idx_step_attempts_run_step on step_attempts(run_id, step_name);
        """
    )
    # [2] 再起動時に重複しないversion recordを残す。
    conn.execute(
        "insert or ignore into analytics_schema_migrations(version, applied_at) values(1, ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
