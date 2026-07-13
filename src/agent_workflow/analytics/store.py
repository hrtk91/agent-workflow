"""分析データの保存と集計を統括する公開store。"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from agent_workflow.analytics.attempts import qc_outcomes, upsert_step_attempt
from agent_workflow.analytics.constants import TERMINAL_RUN_STATUSES
from agent_workflow.analytics.measurements import collect_change_stats, task_packet_identity
from agent_workflow.analytics.normalization import duration_seconds, run_finished_at
from agent_workflow.analytics.reporting import build_empty_report, build_report
from agent_workflow.analytics.schema import initialize_schema
from agent_workflow.state import RunState


class AnalyticsStore:
    """外部serviceに依存せず、正規化したrun factsを保存・集計する。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        """分析値を書き込む実行系からschemaを初期化する。"""

        with self._db() as conn:
            initialize_schema(conn)

    def record_state(self, state: RunState) -> None:
        """run状態を、初期入力と完了時変更量を壊さず分析DBへ反映する。

        処理フロー:
        - [1] DB transaction外でタスク識別子と完了時のGit変更量を取得する。
        - [2] 現在までに開始されたstep attemptを正規化して保存する。
        - [3] QC結果・終了時刻・試行回数などrun単位の集計値を算出する。
        - [4] 初期入力と最初の完了時変更量を保持しながらrun_metricsを更新する。
        """

        # [1] 比較的遅いfilesystem/Git処理で並列workerのwrite transactionを占有しない。
        task_sha256, task_bytes = task_packet_identity(Path(state.task_dir))
        change_stats = collect_change_stats(state) if state.status in TERMINAL_RUN_STATUSES else None

        with self._db() as conn:
            # [2] state.jsonが保持する各stepの最新attemptを、安定した複合keyで保存する。
            for step in state.steps:
                if step.attempts > 0 and step.status != "pending":
                    upsert_step_attempt(conn, state.run_id, step)

            # [3] attempt履歴と現在stateから、run単位で比較する値を確定する。
            first_pass_qc, eventual_qc = qc_outcomes(conn, state)
            finished_at = run_finished_at(state) if state.status in TERMINAL_RUN_STATUSES else None
            elapsed = duration_seconds(state.created_at, finished_at)
            executor_attempts = state.step("run_executor").attempts
            qc_attempts = state.step("run_qc").attempts
            qc_profile_hash = hashlib.sha256(state.verify_command.encode("utf-8")).hexdigest()
            changed_files, additions, deletions = change_stats or (None, None, None)

            # [4] runningへ戻った場合だけ変更量を未確定へ戻し、terminal値は最初のsnapshotを保持する。
            conn.execute(
                """
                insert into run_metrics(
                  run_id, status, purpose, repo_path, workflow, executor_bin, provider, model,
                  task_type, base_ref, qc_profile_hash, task_sha256, task_bytes,
                  created_at, updated_at, finished_at, elapsed_seconds,
                  executor_attempts, qc_attempts, first_pass_qc, eventual_qc,
                  changed_files, additions, deletions
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(run_id) do update set
                  status=excluded.status,
                  purpose=excluded.purpose,
                  repo_path=excluded.repo_path,
                  workflow=excluded.workflow,
                  executor_bin=excluded.executor_bin,
                  provider=excluded.provider,
                  model=excluded.model,
                  task_type=excluded.task_type,
                  base_ref=excluded.base_ref,
                  qc_profile_hash=excluded.qc_profile_hash,
                  task_sha256=coalesce(run_metrics.task_sha256, excluded.task_sha256),
                  task_bytes=coalesce(run_metrics.task_bytes, excluded.task_bytes),
                  updated_at=excluded.updated_at,
                  finished_at=excluded.finished_at,
                  elapsed_seconds=excluded.elapsed_seconds,
                  executor_attempts=excluded.executor_attempts,
                  qc_attempts=excluded.qc_attempts,
                  first_pass_qc=excluded.first_pass_qc,
                  eventual_qc=excluded.eventual_qc,
                  changed_files=case
                    when excluded.finished_at is null then null
                    else coalesce(run_metrics.changed_files, excluded.changed_files)
                  end,
                  additions=case
                    when excluded.finished_at is null then null
                    else coalesce(run_metrics.additions, excluded.additions)
                  end,
                  deletions=case
                    when excluded.finished_at is null then null
                    else coalesce(run_metrics.deletions, excluded.deletions)
                  end
                """,
                (
                    state.run_id,
                    state.status,
                    state.purpose,
                    state.repo_path,
                    state.workflow,
                    state.executor_bin,
                    state.provider,
                    state.model,
                    state.task_type,
                    state.base_ref,
                    qc_profile_hash,
                    task_sha256,
                    task_bytes,
                    state.created_at,
                    state.updated_at,
                    finished_at,
                    elapsed,
                    executor_attempts,
                    qc_attempts,
                    first_pass_qc,
                    eventual_qc,
                    changed_files,
                    additions,
                    deletions,
                ),
            )

    def report(
        self,
        group_by: Iterable[str],
        repo_path: str | None = None,
        since: str | None = None,
        include_repair: bool = False,
    ) -> dict[str, Any]:
        """完了runを指定軸で集計し、QC通過率と中央値を返す。

        処理フロー:
        - [1] 分析DBが未作成なら、fileを作らず空payloadを返す。
        - [2] DBをread-onlyで開き、分析schemaの有無を確認する。
        - [3] query・group集計・統計値生成をreporting moduleへ委譲する。
        """

        # [1] DB未作成時もfileを新規作成せず、同じ形式の空payloadを返す。
        if not self.db_path.is_file():
            return build_empty_report(
                group_by=group_by,
                repo_path=repo_path,
                since=since,
                include_repair=include_repair,
            )
        # [2] SQLite自体にもwriteを拒否させ、旧DBにも分析schemaを追加しない。
        with self._read_db() as conn:
            has_metrics = conn.execute(
                "select 1 from sqlite_master where type = 'table' and name = 'run_metrics'"
            ).fetchone()
            if has_metrics is None:
                return build_empty_report(
                    group_by=group_by,
                    repo_path=repo_path,
                    since=since,
                    include_repair=include_repair,
                )
            # [3] CLIとOTelが共有するpayload構築を一箇所へ集約する。
            return build_report(
                conn,
                group_by=group_by,
                repo_path=repo_path,
                since=since,
                include_repair=include_repair,
            )

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("pragma journal_mode=wal")
        return conn

    def _read_db(self) -> sqlite3.Connection:
        uri = f"{self.db_path.expanduser().resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("pragma query_only=on")
        return conn
