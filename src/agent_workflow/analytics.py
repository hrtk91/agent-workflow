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
        """Snapshot state while preserving first-input and final-change invariants."""

        # Finish filesystem and Git inspection before opening a write transaction so
        # parallel workers are not blocked by comparatively slow local I/O.
        task_sha256, task_bytes = durable_task_packet_identity(state, create=allow_task_identity_create)
        change_stats = collect_change_stats(state) if state.status in TERMINAL_RUN_STATUSES else None

        with self._db() as conn:
            for step in state.steps:
                if step.attempts > 0 and step.status != "pending":
                    self._upsert_step_attempt(conn, state.run_id, step)

            first_pass_qc, eventual_qc = self._qc_outcomes(conn, state)
            finished_at = run_finished_at(state) if state.status in TERMINAL_RUN_STATUSES else None
            elapsed = duration_seconds(state.created_at, finished_at)
            executor_attempts = state.step("run_executor").attempts
            qc_attempts = state.step("run_qc").attempts
            qc_profile_hash = hashlib.sha256(state.verify_command.encode("utf-8")).hexdigest()
            changed_files, additions, deletions = change_stats or (None, None, None)

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
        """Backfill missing analytics rows from immutable state and trace artifacts."""

        refreshed = 0
        if not runs_dir.exists():
            return refreshed
        for state_path in sorted(runs_dir.glob("*/state.json")):
            try:
                state = RunState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if not self._needs_refresh(state):
                continue
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
        """Aggregate completed runs; QC rates use only runs that executed QC."""

        groups = tuple(group_by)
        invalid = [field for field in groups if field not in GROUP_FIELDS]
        if invalid:
            raise ValueError(f"unsupported report group: {', '.join(invalid)}")
        if not groups:
            raise ValueError("--group-by must contain at least one field")

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

        with self._db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"select * from run_metrics where {' and '.join(where)} order by created_at",
                params,
            ).fetchall()

        grouped: dict[tuple[str, ...], list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            key = tuple(display_dimension(row[GROUP_FIELDS[field]], field) for field in groups)
            grouped[key].append(row)

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
        """Create analytics tables alongside the existing operational tables."""

        with self._db() as conn:
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
            conn.execute(
                "insert or ignore into analytics_schema_migrations(version, applied_at) values(1, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )

    def _record_trace_attempts(self, run_id: str, trace_path: Path) -> None:
        """Recover per-attempt rows that predate normalized SQLite persistence."""

        if not trace_path.is_file():
            return
        try:
            lines = trace_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        with self._db() as conn:
            for line in lines:
                try:
                    record = json.loads(line)
                    name = str(record.get("name") or "")
                    if not name.startswith("agent_workflow.step."):
                        continue
                    step_name = name.removeprefix("agent_workflow.step.")
                    attrs = record.get("attributes") or {}
                    attempt = int(attrs.get("attempt") or 0)
                    if attempt < 1:
                        continue
                    timed_out = bool(attrs.get("timed_out"))
                    error = str(attrs.get("error") or (record.get("status") or {}).get("message") or "") or None
                    status_code = str((record.get("status") or {}).get("code") or "")
                    status = trace_attempt_status(step_name, status_code, timed_out, error)
                    self._upsert_attempt_values(
                        conn,
                        run_id=run_id,
                        step_name=step_name,
                        attempt=attempt,
                        status=status,
                        started_at=nanos_to_iso(record.get("start_time_unix_nano")),
                        finished_at=nanos_to_iso(record.get("end_time_unix_nano")),
                        duration=float(record["duration_ms"]) / 1000 if record.get("duration_ms") is not None else None,
                        exit_code=integer_or_none(attrs.get("exit_code")),
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
        rows = conn.execute(
            "select attempt, status from step_attempts where run_id = ? and step_name = 'run_qc' order by attempt",
            (state.run_id,),
        ).fetchall()
        first_pass: int | None = None
        for attempt, status in rows:
            if int(attempt) == 1 and str(status) in TERMINAL_STEP_STATUSES:
                first_pass = int(status == "succeeded")
                break
        eventual: int | None = None
        if any(str(status) == "succeeded" for _, status in rows):
            eventual = 1
        elif state.status in TERMINAL_RUN_STATUSES and state.step("run_qc").attempts > 0:
            eventual = 0
        return first_pass, eventual

    def _needs_refresh(self, state: RunState) -> bool:
        """Skip unchanged runs whose expected attempt history is already complete."""

        with self._db() as conn:
            row = conn.execute("select updated_at from run_metrics where run_id = ?", (state.run_id,)).fetchone()
            if row is None or str(row[0]) != state.updated_at:
                return True
            recorded_attempts = int(
                conn.execute("select count(*) from step_attempts where run_id = ?", (state.run_id,)).fetchone()[0]
            )
        return recorded_attempts < sum(step.attempts for step in state.steps)

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("pragma journal_mode=wal")
        return conn


def render_text_report(report: dict[str, Any]) -> str:
    """Render a compact terminal table from the structured report payload."""

    rows = list(report["rows"])
    if not rows:
        return "No completed workflow runs matched."

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
    lines = ["Agent workflow report", "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    for row in values:
        lines.append("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    return "\n".join(lines)


def task_packet_identity(task_dir: Path) -> tuple[str | None, int | None]:
    """Hash the ordered task packet so equivalent inputs have one identity."""

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
    return (digest.hexdigest(), total) if found else (None, None)


def durable_task_packet_identity(state: RunState, *, create: bool) -> tuple[str | None, int | None]:
    """Read or create the immutable identity captured before QC mutates context.md."""

    identity_path = Path(state.run_dir) / TASK_IDENTITY_NAME
    if identity_path.is_file():
        try:
            data = json.loads(identity_path.read_text(encoding="utf-8"))
            sha256 = str(data["sha256"])
            size = int(data["bytes"])
            return sha256, size
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None, None
    if not create:
        return None, None
    sha256, size = task_packet_identity(Path(state.task_dir))
    if sha256 is None or size is None:
        return None, None
    identity_path.write_text(
        json.dumps({"bytes": size, "sha256": sha256}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return sha256, size


def collect_change_stats(state: RunState) -> tuple[int, int, int] | None:
    """Count tracked and untracked worktree changes against the run base ref."""

    if not state.worktree_path or not state.base_ref:
        return None
    worktree = Path(state.worktree_path)
    if not worktree.is_dir():
        return None
    try:
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
