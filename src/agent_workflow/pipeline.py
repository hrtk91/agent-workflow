"""ワークフロー可視化用のSQLite read model。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_workflow.state import WORKFLOW_STEPS


PIPELINE_FILTERS = ("all", "running", "queued", "attention", "succeeded")
ATTENTION_STATUSES = frozenset({"blocked", "failed", "interrupted", "qc_failed", "timed_out"})


@dataclass(frozen=True)
class PipelineStep:
    name: str
    status: str
    attempts: int
    started_at: str | None
    finished_at: str | None
    duration_seconds: float | None
    exit_code: int | None
    timed_out: bool
    error: str | None
    stdout_path: str | None
    stderr_path: str | None


@dataclass(frozen=True)
class PipelineRun:
    run_id: str
    status: str
    repo_path: str
    workflow: str
    purpose: str
    current_step: str | None
    summary_path: str
    qc_repair_attempts: int
    created_at: str
    updated_at: str
    steps: tuple[PipelineStep, ...]


@dataclass(frozen=True)
class PipelineJob:
    job_id: str
    status: str
    run_id: str | None
    repo_path: str
    workflow: str
    purpose: str
    summary_path: str | None
    error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PipelineItem:
    """TUIの一覧に表示できるrunまたは、run開始前のqueue job。"""

    item_id: str
    kind: str
    status: str
    repo_path: str
    workflow: str
    purpose: str
    updated_at: str
    run: PipelineRun | None = None
    job: PipelineJob | None = None


@dataclass(frozen=True)
class PipelineSnapshot:
    generated_at: str
    jobs: tuple[PipelineJob, ...]
    runs: tuple[PipelineRun, ...]

    @classmethod
    def empty(cls) -> "PipelineSnapshot":
        return cls(generated_at=utc_now(), jobs=(), runs=())

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["jobs"] = list(data["jobs"])
        data["runs"] = list(data["runs"])
        for run in data["runs"]:
            run["steps"] = list(run["steps"])
        return data


class PipelineSnapshotReader:
    """SQLiteを変更せず、TUI/GUI共通の現在状態snapshotを作る。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser()

    def snapshot(self, *, limit: int = 100, include_repair: bool = False) -> PipelineSnapshot:
        if limit < 1 or not self.db_path.is_file():
            return PipelineSnapshot.empty()

        try:
            with self._read_db() as conn:
                tables = {
                    str(row[0])
                    for row in conn.execute("select name from sqlite_master where type = 'table'").fetchall()
                }
                jobs = self._read_jobs(conn, limit, include_repair) if "queue" in tables else ()
                runs = self._read_runs(conn, limit, include_repair) if "runs" in tables else ()
        except (OSError, sqlite3.Error):
            return PipelineSnapshot.empty()
        return PipelineSnapshot(generated_at=utc_now(), jobs=jobs, runs=runs)

    def _read_jobs(
        self,
        conn: sqlite3.Connection,
        limit: int,
        include_repair: bool,
    ) -> tuple[PipelineJob, ...]:
        rows = conn.execute(
            """
            select job_id, status, run_id, summary_path, error, config_json, created_at, updated_at
            from queue
            order by created_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
        jobs: list[PipelineJob] = []
        for row in rows:
            config = parse_config(str(row[5]))
            purpose = str(config.get("purpose") or "workflow")
            if not include_repair and purpose in {"repair", "repair_action"}:
                continue
            jobs.append(
                PipelineJob(
                    job_id=str(row[0]),
                    status=str(row[1]),
                    run_id=str(row[2]) if row[2] else None,
                    repo_path=str(config.get("repo_path") or ""),
                    workflow=str(config.get("workflow") or "default"),
                    purpose=purpose,
                    summary_path=str(row[3]) if row[3] else None,
                    error=str(row[4]) if row[4] else None,
                    created_at=str(row[6]),
                    updated_at=str(row[7]),
                )
            )
        return tuple(jobs)

    def _read_runs(
        self,
        conn: sqlite3.Connection,
        limit: int,
        include_repair: bool,
    ) -> tuple[PipelineRun, ...]:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select run_id, status, repo_path, workflow, purpose, current_step,
                   summary_path, qc_repair_attempts, created_at, updated_at
            from runs
            order by updated_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
        selected = [row for row in rows if include_repair or str(row["purpose"]) not in {"repair", "repair_action"}]
        if not selected:
            return ()
        run_ids = [str(row["run_id"]) for row in selected]
        placeholders = ",".join("?" for _ in run_ids)
        has_steps = conn.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'run_steps'"
        ).fetchone()
        step_rows = (
            conn.execute(
                f"""
                select run_id, position, step_name, status, attempts, started_at, finished_at,
                       exit_code, timed_out, error, stdout_path, stderr_path
                from run_steps
                where run_id in ({placeholders})
                order by run_id, position
                """,
                run_ids,
            ).fetchall()
            if has_steps
            else []
        )
        steps_by_run: dict[str, list[PipelineStep]] = {run_id: [] for run_id in run_ids}
        for step_row in step_rows:
            run_id = str(step_row["run_id"])
            steps_by_run.setdefault(run_id, []).append(step_from_row(step_row))

        runs: list[PipelineRun] = []
        for row in selected:
            run_id = str(row["run_id"])
            steps = tuple(steps_by_run.get(run_id) or default_steps())
            runs.append(
                PipelineRun(
                    run_id=run_id,
                    status=str(row["status"]),
                    repo_path=str(row["repo_path"]),
                    workflow=str(row["workflow"]),
                    purpose=str(row["purpose"]),
                    current_step=str(row["current_step"]) if row["current_step"] else None,
                    summary_path=str(row["summary_path"]),
                    qc_repair_attempts=int(row["qc_repair_attempts"] or 0),
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                    steps=steps,
                )
            )
        return tuple(runs)

    def _read_db(self) -> sqlite3.Connection:
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("pragma query_only=on")
        return conn


def pipeline_items(snapshot: PipelineSnapshot, filter_name: str = "all") -> tuple[PipelineItem, ...]:
    """run開始前のjobと、既存runを同じ一覧へ並べる。"""

    if filter_name not in PIPELINE_FILTERS:
        raise ValueError(f"unknown pipeline filter: {filter_name}")
    items: list[PipelineItem] = []
    for job in snapshot.jobs:
        if job.status not in {"queued", "running"}:
            continue
        item = PipelineItem(
            item_id=job.job_id,
            kind="job",
            status=job.status,
            repo_path=job.repo_path,
            workflow=job.workflow,
            purpose=job.purpose,
            updated_at=job.updated_at,
            job=job,
        )
        if matches_filter(item.status, filter_name):
            items.append(item)
    for run in snapshot.runs:
        item = PipelineItem(
            item_id=run.run_id,
            kind="run",
            status=run.status,
            repo_path=run.repo_path,
            workflow=run.workflow,
            purpose=run.purpose,
            updated_at=run.updated_at,
            run=run,
        )
        if matches_filter(item.status, filter_name):
            items.append(item)
    items.sort(key=lambda item: item.updated_at, reverse=True)
    return tuple(items)


def matches_filter(status: str, filter_name: str) -> bool:
    if filter_name == "all":
        return True
    if filter_name == "attention":
        return status in ATTENTION_STATUSES
    return status == filter_name


def default_steps() -> list[PipelineStep]:
    return [
        PipelineStep(
            name=name,
            status="pending",
            attempts=0,
            started_at=None,
            finished_at=None,
            duration_seconds=None,
            exit_code=None,
            timed_out=False,
            error=None,
            stdout_path=None,
            stderr_path=None,
        )
        for name in WORKFLOW_STEPS
    ]


def step_from_row(row: sqlite3.Row) -> PipelineStep:
    started_at = str(row["started_at"]) if row["started_at"] else None
    finished_at = str(row["finished_at"]) if row["finished_at"] else None
    status = str(row["status"])
    return PipelineStep(
        name=str(row["step_name"]),
        status=status,
        attempts=int(row["attempts"]),
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds(started_at, finished_at) if status != "pending" else None,
        exit_code=int(row["exit_code"]) if row["exit_code"] is not None else None,
        timed_out=bool(row["timed_out"]),
        error=str(row["error"]) if row["error"] else None,
        stdout_path=str(row["stdout_path"]) if row["stdout_path"] else None,
        stderr_path=str(row["stderr_path"]) if row["stderr_path"] else None,
    )


def parse_config(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def duration_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    started = parse_time(started_at)
    if started is None:
        return None
    finished = parse_time(finished_at) or datetime.now(timezone.utc)
    return round(max(0.0, (finished - started).total_seconds()), 3)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
