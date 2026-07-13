"""Durable run analytics and report aggregation backed by jobs.sqlite."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from agent_workflow.state import RunState, StepState


TERMINAL_RUN_STATUSES = {"blocked", "failed", "interrupted", "qc_failed", "succeeded", "timed_out"}
TERMINAL_STEP_STATUSES = TERMINAL_RUN_STATUSES
GROUP_FIELDS = {
    "model": "model",
    "provider": "provider",
    "task_type": "task_type",
    "workflow": "workflow",
    "repo": "repo_path",
    "status": "status",
}
TASK_PACKET_NAMES = ("task.md", "acceptance.md", "constraints.md", "context.md")
TASK_IDENTITY_NAME = "task-identity.json"


class AnalyticsStore:
    """Persist normalized run facts and build reports without external services."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_schema()

    def record_state(self, state: RunState, *, allow_task_identity_create: bool = True) -> None:
        """run状態を、初期入力と完了時変更量を壊さず分析DBへ反映する。

        処理フロー:
        - [1] DB transaction外でタスク識別子と完了時のGit変更量を取得する。
        - [2] 現在までに開始されたstep attemptを正規化して保存する。
        - [3] QC結果・終了時刻・試行回数などrun単位の集計値を算出する。
        - [4] 初期入力と最初の完了時変更量を保持しながらrun_metricsを更新する。
        """

        # [1] 比較的遅いfilesystem/Git処理で並列workerのwrite transactionを占有しない。
        task_sha256, task_bytes = durable_task_packet_identity(state, create=allow_task_identity_create)
        change_stats = collect_change_stats(state) if state.status in TERMINAL_RUN_STATUSES else None

        with self._db() as conn:
            # [2] state.jsonが保持する各stepの最新attemptを、安定した複合keyで保存する。
            for step in state.steps:
                if step.attempts > 0 and step.status != "pending":
                    self._upsert_step_attempt(conn, state.run_id, step)

            # [3] attempt履歴と現在stateから、run単位で比較する値を確定する。
            first_pass_qc, eventual_qc = self._qc_outcomes(conn, state)
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

    def refresh_from_runs(self, runs_dir: Path) -> int:
        """run成果物から不足・更新分だけを分析DBへ復元する。

        処理フロー:
        - [1] run directoryの有無を確認し、対象state.jsonを列挙する。
        - [2] 読み取れるstateだけを復元する。
        - [3] DBが最新ならfilesystemやtraceの再走査を省略する。
        - [4] 過去traceのattempt履歴を戻してからrun状態を再記録する。
        """

        # [1] 初回利用などrun directoryがない場合は何も更新しない。
        refreshed = 0
        if not runs_dir.exists():
            return refreshed
        for state_path in sorted(runs_dir.glob("*/state.json")):
            # [2] 破損・途中書き込みなど、復元できないstateは他のrunを妨げない。
            try:
                state = RunState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            # [3] updated_atとattempt件数が揃うrunは追加I/Oなしでskipする。
            if not self._needs_refresh(state):
                continue
            # [4] 旧runのattempt履歴を先に戻し、そこからQC結果を再計算する。
            self._record_trace_attempts(state.run_id, Path(state.trace_path))
            self.record_state(state, allow_task_identity_create=False)
            refreshed += 1
        return refreshed

    def report(
        self,
        group_by: Iterable[str],
        repo_path: str | None = None,
        since: str | None = None,
        include_repair: bool = False,
    ) -> dict[str, Any]:
        """完了runを指定軸で集計し、QC通過率と中央値を返す。

        処理フロー:
        - [1] group-by指定を検証する。
        - [2] terminal run・purpose・repository・期間の検索条件を組み立てる。
        - [3] SQLiteから対象runを取得する。
        - [4] 指定されたdimension値でrunをグルーピングする。
        - [5] QC実行runだけを分母に通過率と各中央値を算出する。
        - [6] 生成時刻・条件・集計行を構造化して返す。
        """

        # [1] SQL列名へ変換できる既知dimensionだけを受け付ける。
        groups = tuple(group_by)
        invalid = [field for field in groups if field not in GROUP_FIELDS]
        if invalid:
            raise ValueError(f"unsupported report group: {', '.join(invalid)}")
        if not groups:
            raise ValueError("--group-by must contain at least one field")

        # [2] repair runを既定で除外し、指定された絞り込みだけをparameter化する。
        where = [f"status in ({','.join('?' for _ in TERMINAL_RUN_STATUSES)})"]
        params: list[object] = []
        params.extend(sorted(TERMINAL_RUN_STATUSES))
        if not include_repair:
            where.append("purpose = 'workflow'")
        if repo_path:
            where.append("repo_path = ?")
            params.append(repo_path)
        if since:
            where.append("created_at >= ?")
            params.append(since)

        # [3] report対象となる完了runを時系列順で取得する。
        with self._db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"select * from run_metrics where {' and '.join(where)} order by created_at",
                params,
            ).fetchall()

        # [4] nullや空値を表示用の安定したdimension値へ正規化してまとめる。
        grouped: dict[tuple[str, ...], list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            key = tuple(display_dimension(row[GROUP_FIELDS[field]], field) for field in groups)
            grouped[key].append(row)

        # [5] QC未実行runを通過率の分母から外し、比較用の統計値を作る。
        report_rows: list[dict[str, Any]] = []
        for key, members in sorted(grouped.items()):
            first_pass = [int(row["first_pass_qc"]) for row in members if row["first_pass_qc"] is not None]
            eventual = [int(row["eventual_qc"]) for row in members if row["eventual_qc"] is not None]
            attempts = [int(row["qc_attempts"]) for row in members if row["first_pass_qc"] is not None]
            elapsed = [float(row["elapsed_seconds"]) for row in members if row["elapsed_seconds"] is not None]
            changed = [int(row["additions"]) + int(row["deletions"]) for row in members if row["additions"] is not None and row["deletions"] is not None]
            first_successes = sum(first_pass)
            eventual_successes = sum(eventual)
            report_rows.append(
                {
                    "group": dict(zip(groups, key, strict=True)),
                    "runs": len(members),
                    "qc_runs": len(first_pass),
                    "first_pass_qc_rate": rate(first_successes, len(first_pass)),
                    "first_pass_qc_ci95": wilson_interval(first_successes, len(first_pass)),
                    "eventual_qc_rate": rate(eventual_successes, len(eventual)),
                    "eventual_qc_ci95": wilson_interval(eventual_successes, len(eventual)),
                    "qc_attempts_p50": rounded_median(attempts),
                    "elapsed_seconds_p50": rounded_median(elapsed),
                    "changed_lines_p50": rounded_median(changed),
                }
            )

        # [6] text表示とOTel exportが同じpayloadを利用できる形で返す。
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "group_by": list(groups),
            "filters": {
                "repo_path": repo_path,
                "since": since,
                "include_repair": include_repair,
            },
            "rows": report_rows,
        }

    def _init_schema(self) -> None:
        """既存jobs.sqliteへversion管理された分析schemaを初期化する。

        処理フロー:
        - [1] run・attemptテーブルと検索indexを冪等に作成する。
        - [2] 適用済みschema versionを記録する。
        """

        with self._db() as conn:
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

    def _record_trace_attempts(self, run_id: str, trace_path: Path) -> None:
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
        with self._db() as conn:
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
                    self._upsert_attempt_values(
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

    def _upsert_step_attempt(self, conn: sqlite3.Connection, run_id: str, step: StepState) -> None:
        self._upsert_attempt_values(
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

    def _upsert_attempt_values(
        self,
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
        """Keep one mutable row for each stable run/step/attempt identity."""

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
                failure_category(step_name, status, timed_out),
            ),
        )

    def _qc_outcomes(self, conn: sqlite3.Connection, state: RunState) -> tuple[int | None, int | None]:
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

    def _needs_refresh(self, state: RunState) -> bool:
        """state更新時刻とattempt件数からartifact再走査の要否を判定する。

        処理フロー:
        - [1] run_metricsがない、またはupdated_atが異なるrunを更新対象にする。
        - [2] 時刻が同じ場合も、保存済みattempt数がstateより少なければ更新対象にする。
        """

        # [1] 未登録または更新されたstateは詳細比較せずrefreshする。
        with self._db() as conn:
            row = conn.execute("select updated_at from run_metrics where run_id = ?", (state.run_id,)).fetchone()
            if row is None or str(row[0]) != state.updated_at:
                return True
            # [2] trace backfill途中などattempt履歴が不足するrunだけ再処理する。
            recorded_attempts = int(
                conn.execute("select count(*) from step_attempts where run_id = ?", (state.run_id,)).fetchone()[0]
            )
        return recorded_attempts < sum(step.attempts for step in state.steps)

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("pragma journal_mode=wal")
        return conn


def render_text_report(report: dict[str, Any]) -> str:
    """構造化reportをterminal向けの固定幅tableへ変換する。

    処理フロー:
    - [1] 空結果を短いmessageとして返す。
    - [2] 各集計行を表示文字列へ変換し、列幅を算出する。
    - [3] header・区切り・data行を組み立てる。
    """

    # [1] headerだけのtableを出さず、絞り込み結果が空であることを明示する。
    rows = list(report["rows"])
    if not rows:
        return "No completed workflow runs matched."

    # [2] 数値とdurationを表示用へ整形し、内容が切れない最大列幅を求める。
    headers = ["group", "runs", "qc", "first-pass", "eventual", "attempts p50", "elapsed p50", "changed p50"]
    values: list[list[str]] = []
    for row in rows:
        group = ",".join(f"{key}={value}" for key, value in row["group"].items())
        values.append(
            [
                group,
                str(row["runs"]),
                str(row["qc_runs"]),
                format_rate(row["first_pass_qc_rate"]),
                format_rate(row["eventual_qc_rate"]),
                format_number(row["qc_attempts_p50"]),
                format_duration(row["elapsed_seconds_p50"]),
                format_number(row["changed_lines_p50"]),
            ]
        )
    widths = [max(len(headers[index]), *(len(row[index]) for row in values)) for index in range(len(headers))]
    # [3] すべての行を同じ列幅で左寄せして出力する。
    lines = ["Agent workflow report", "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    for row in values:
        lines.append("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    return "\n".join(lines)


def task_packet_identity(task_dir: Path) -> tuple[str | None, int | None]:
    """順序固定のtask packetから再現可能なhashとbyte数を計算する。

    処理フロー:
    - [1] 対象ファイルを定義順に読み、ファイル名と内容をhashへ加える。
    - [2] 1ファイル以上読めた場合だけ識別子を返す。
    """

    # [1] 同じ内容でもfile境界が異なるpacketを別入力として扱う。
    digest = hashlib.sha256()
    total = 0
    found = False
    for name in TASK_PACKET_NAMES:
        path = task_dir / name
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        found = True
        total += len(data)
        digest.update(name.encode("utf-8") + b"\0" + data + b"\0")
    # [2] task生成前など入力が存在しない状態をnullで表す。
    return (digest.hexdigest(), total) if found else (None, None)


def durable_task_packet_identity(state: RunState, *, create: bool) -> tuple[str | None, int | None]:
    """QCがcontext.mdを変更する前のtask識別子を永続化して再利用する。

    処理フロー:
    - [1] 保存済みtask-identity.jsonがあれば検証して返す。
    - [2] backfill時など新規作成を許可しない場合は未取得として返す。
    - [3] 現在のtask packetを計算し、初回値だけをartifactへ保存する。
    """

    # [1] live taskの現在値より、run開始時に固定したartifactを常に優先する。
    identity_path = Path(state.run_dir) / TASK_IDENTITY_NAME
    if identity_path.is_file():
        try:
            data = json.loads(identity_path.read_text(encoding="utf-8"))
            sha256 = str(data["sha256"])
            size = int(data["bytes"])
            return sha256, size
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None, None
    # [2] 過去runに初期値の証拠がない場合、現在の変異済みtaskから推測しない。
    if not create:
        return None, None
    # [3] 入力が揃った最初の時点でhashとbyte数を固定する。
    sha256, size = task_packet_identity(Path(state.task_dir))
    if sha256 is None or size is None:
        return None, None
    identity_path.write_text(
        json.dumps({"bytes": size, "sha256": sha256}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return sha256, size


def collect_change_stats(state: RunState) -> tuple[int, int, int] | None:
    """run開始refに対するtracked/untracked変更量を集計する。

    処理フロー:
    - [1] 比較可能なworktreeとbase refがあることを確認する。
    - [2] git diff --numstatからtracked fileの増減を集計する。
    - [3] untracked text fileを追加行として重複なく集計する。
    - [4] file数・追加行・削除行を返し、Git失敗時は未計測とする。
    """

    # [1] worktree未作成・cleanup済み・base未確定のrunは計測できない。
    if not state.worktree_path or not state.base_ref:
        return None
    worktree = Path(state.worktree_path)
    if not worktree.is_dir():
        return None
    try:
        # [2] binaryと内部TAKT成果物を除き、tracked差分のfile数と行数を数える。
        diff = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--numstat", state.base_ref],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if diff.returncode != 0:
            return None
        seen: set[str] = set()
        additions = 0
        deletions = 0
        for line in diff.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            if excluded_metric_path(path):
                continue
            seen.add(path)
            if added.isdigit():
                additions += int(added)
            if deleted.isdigit():
                deletions += int(deleted)

        # [3] tracked側で数えたpathを除外し、text fileだけを追加行として扱う。
        untracked = subprocess.run(
            ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if untracked.returncode != 0:
            return None
        for raw_path in untracked.stdout.split(b"\0"):
            if not raw_path:
                continue
            path = raw_path.decode("utf-8", errors="replace")
            if path in seen or excluded_metric_path(path):
                continue
            seen.add(path)
            try:
                data = (worktree / path).read_bytes()
            except OSError:
                continue
            if b"\0" not in data:
                additions += len(data.splitlines())
        # [4] 途中のGit/filesystem失敗では不正確な0を保存せずnull扱いにする。
        return len(seen), additions, deletions
    except OSError:
        return None


def excluded_metric_path(path: str) -> bool:
    return path == ".takt/runs" or path.startswith(".takt/runs/")


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


def display_dimension(value: object, field: str) -> str:
    if value is None or str(value) == "":
        return "(default)" if field in {"model", "provider"} else "unspecified"
    return str(value)


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


def rate(successes: int, total: int) -> float | None:
    if total == 0:
        return None
    return round(successes / total * 100, 1)


def wilson_interval(successes: int, total: int) -> list[float] | None:
    if total == 0:
        return None
    z = 1.96
    observed = successes / total
    denominator = 1 + z * z / total
    center = (observed + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(observed * (1 - observed) / total + z * z / (4 * total * total)) / denominator
    return [round(max(0.0, center - margin) * 100, 1), round(min(1.0, center + margin) * 100, 1)]


def rounded_median(values: list[int] | list[float]) -> float | None:
    if not values:
        return None
    return round(float(median(values)), 1)


def format_rate(value: Any) -> str:
    return "-" if value is None else f"{float(value):.1f}%"


def format_number(value: Any) -> str:
    if value is None:
        return "-"
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def format_duration(value: Any) -> str:
    if value is None:
        return "-"
    seconds = float(value)
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"
