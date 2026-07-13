"""SQLiteのraw run factsをread-onlyで集計・参照する。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from agent_workflow.analytics.reporting import build_empty_report, build_report
from agent_workflow.analytics.run_detail import build_run_detail


class AnalyticsReporter:
    """runnerを初期化せず、SQLiteへSELECTだけを実行する。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def report(
        self,
        group_by: Iterable[str],
        repo_path: str | None = None,
        since: str | None = None,
        include_repair: bool = False,
    ) -> dict[str, Any]:
        """完了runを指定軸で集計する。

        処理フロー:
        - [1] DB未作成ならfileを作らず空payloadを返す。
        - [2] DBをread-onlyで開き、runs schemaの有無を確認する。
        - [3] raw factsの集計をreporting moduleへ委譲する。
        """

        # [1] 初回run前のreportもread-onlyのまま扱う。
        if not self.db_path.is_file():
            return build_empty_report(group_by, repo_path, since, include_repair)
        # [2] SQLiteにもwriteを拒否させ、schema migrationを起動しない。
        with self._read_db() as conn:
            if not self._has_runs(conn):
                return build_empty_report(group_by, repo_path, since, include_repair)
            # [3] CLIとOTelが共有する集計payloadをraw factsから生成する。
            return build_report(conn, group_by, repo_path, since, include_repair)

    def run_detail(self, run_id: str) -> dict[str, Any]:
        """1 runのcurrent stepsとattempt履歴をread-onlyで返す。

        処理フロー:
        - [1] DB・canonical schemaがなければnot foundとして扱う。
        - [2] query-only connectionでrun detailを構築する。
        """

        # [1] reportを契機にDBやschemaを作らない。
        if not self.db_path.is_file():
            raise ValueError(f"run not found: {run_id}")
        with self._read_db() as conn:
            if not self._has_runs(conn):
                raise ValueError(f"run not found: {run_id}")
            # [2] current stateとattempt履歴のSELECTだけをreporting層へ委譲する。
            return build_run_detail(conn, run_id)

    @staticmethod
    def _has_runs(conn: sqlite3.Connection) -> bool:
        return conn.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'runs'"
        ).fetchone() is not None

    def _read_db(self) -> sqlite3.Connection:
        uri = f"{self.db_path.expanduser().resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("pragma query_only=on")
        conn.execute("begin")
        return conn
