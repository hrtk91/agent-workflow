from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from agent_workflow.state import RunState, StepState
from agent_workflow.tracing import TraceRecorder, trace_enabled_hint

STEPS = ["load_task", "create_worktree", "run_executor", "run_qc", "write_summary"]


@dataclass
class RunnerConfig:
    state_dir: Path
    repo_path: Path | None = None
    task_dir: Path | None = None
    task_file: Path | None = None
    task_text: str | None = None
    workflow: str = "default"
    verify_command: str | None = None
    timeout_seconds: float | None = 7200
    executor_bin: str = "takt"
    provider: str | None = None
    model: str | None = None
    base_ref: str | None = None


def default_state_dir() -> Path:
    return Path(os.environ.get("AGENT_WORKFLOW_STATE_DIR", Path.home() / ".local/state/agent-workflow"))


class WorkflowRunner:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir.expanduser()
        self.runs_dir = self.state_dir / "runs"
        self.worktrees_dir = self.state_dir / "worktrees"
        self.db_path = self.state_dir / "jobs.sqlite"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def run_new(self, config: RunnerConfig) -> RunState:
        config = self._normalize_config(config)
        if config.repo_path is None:
            raise ValueError("--repo is required")
        if not config.verify_command:
            raise ValueError("--verify-command is required; QC must be explicit")
        run_id = new_run_id()
        run_dir = self.runs_dir / run_id
        task_dir = run_dir / "task"
        trace_path = run_dir / "trace.jsonl"
        summary_path = run_dir / "summary.md"
        now = utc_now()
        state = RunState(
            run_id=run_id,
            status="queued",
            repo_path=str(config.repo_path.expanduser().resolve()),
            run_dir=str(run_dir),
            task_dir=str(task_dir),
            workflow=config.workflow,
            verify_command=config.verify_command,
            timeout_seconds=float(config.timeout_seconds or 0),
            executor_bin=config.executor_bin,
            provider=config.provider,
            model=config.model,
            base_ref=config.base_ref,
            summary_path=str(summary_path),
            trace_path=str(trace_path),
            created_at=now,
            updated_at=now,
            steps=[StepState(name=name) for name in STEPS],
        )
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "logs").mkdir()
        self._write_task_source(run_dir, config)
        self.save_state(state)
        return self._run_from(state, 0)

    def enqueue(self, config: RunnerConfig) -> str:
        config = self._normalize_config(config)
        if config.repo_path is None:
            raise ValueError("--repo is required")
        if not config.verify_command:
            raise ValueError("--verify-command is required; QC must be explicit")
        job_id = new_run_id()
        now = utc_now()
        with self._db() as conn:
            conn.execute(
                """
                insert into queue(job_id, status, config_json, run_id, summary_path, error, created_at, updated_at)
                values(?, 'queued', ?, null, null, null, ?, ?)
                """,
                (job_id, json.dumps(config_to_dict(config), indent=2, sort_keys=True), now, now),
            )
        return job_id

    def tick(self, max_runs: int = 1) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for _ in range(max_runs):
            job = self._claim_next_job()
            if job is None:
                break
            job_id, config = job
            try:
                state = self.run_new(config)
                self._finish_queue_job(job_id, state.status, state.run_id, state.summary_path, "")
                results.append({"job_id": job_id, "status": state.status, "run_id": state.run_id, "summary_path": state.summary_path})
            except Exception as exc:
                self._finish_queue_job(job_id, "failed", "", "", str(exc))
                results.append({"job_id": job_id, "status": "failed", "run_id": "", "summary_path": "", "error": str(exc)})
        return results

    def worker(self, interval_seconds: float = 60, max_runs_per_tick: int = 1) -> None:
        while True:
            self.tick(max_runs=max_runs_per_tick)
            time.sleep(interval_seconds)

    def resume(self, run_id: str, verify_command: str | None = None, timeout_seconds: float | None = None) -> RunState:
        state = self.load_state(run_id)
        self._apply_overrides(state, verify_command, timeout_seconds)
        index = self._next_step_index(state)
        return self._run_from(state, index)

    def retry(self, run_id: str, step_name: str, verify_command: str | None = None, timeout_seconds: float | None = None) -> RunState:
        state = self.load_state(run_id)
        self._apply_overrides(state, verify_command, timeout_seconds)
        names = [step.name for step in state.steps]
        if step_name not in names:
            raise ValueError(f"unknown step {step_name}; expected one of {', '.join(names)}")
        index = names.index(step_name)
        for step in state.steps[index:]:
            step.status = "pending"
            step.exit_code = None
            step.timed_out = False
            step.error = None
        self.save_state(state)
        return self._run_from(state, index)

    def status(self, run_id: str | None = None) -> str:
        if run_id:
            state = self.load_state(run_id)
            lines = [f"{state.run_id}\t{state.status}\t{state.current_step or '-'}\t{state.summary_path}"]
            lines.extend(f"  {s.name}\t{s.status}\tattempts={s.attempts}" for s in state.steps)
            return "\n".join(lines)
        with self._db() as conn:
            queue_rows = conn.execute(
                "select job_id, status, coalesce(run_id, ''), coalesce(summary_path, '') from queue order by created_at desc limit 20"
            ).fetchall()
            run_rows = conn.execute(
                "select run_id, status, coalesce(current_step, ''), summary_path from jobs order by created_at desc limit 20"
            ).fetchall()
        lines = ["queue:"]
        lines.extend("\t".join(["job", *(str(col) for col in row)]) for row in queue_rows)
        lines.append("runs:")
        lines.extend("\t".join(["run", *(str(col) for col in row)]) for row in run_rows)
        return "\n".join(lines)

    def cleanup(self, run_id: str) -> None:
        state = self.load_state(run_id)
        if state.worktree_path:
            subprocess.run(["git", "-C", state.repo_path, "worktree", "remove", "--force", state.worktree_path], check=False)
            subprocess.run(["git", "-C", state.repo_path, "worktree", "prune"], check=False)
            shutil.rmtree(Path(state.worktree_path).parent, ignore_errors=True)
            state.worktree_path = None
            self._write_summary(state)
            self.save_state(state)

    def load_state(self, run_id: str) -> RunState:
        path = self.runs_dir / run_id / "state.json"
        return RunState.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save_state(self, state: RunState) -> None:
        state.updated_at = utc_now()
        state.run_path.mkdir(parents=True, exist_ok=True)
        (state.run_path / "state.json").write_text(json.dumps(asdict(state), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._upsert_job(state)

    def _run_from(self, state: RunState, start_index: int) -> RunState:
        tracer = TraceRecorder(Path(state.trace_path))
        state.status = "running"
        self.save_state(state)
        for index in range(start_index, len(state.steps)):
            step = state.steps[index]
            if step.status == "succeeded":
                continue
            ok = self._run_step(state, step, tracer)
            if not ok:
                self._finalize_failed_summary(state)
                return state
        state.status = "succeeded"
        state.current_step = None
        self._write_summary(state)
        self.save_state(state)
        return state

    def _run_step(self, state: RunState, step: StepState, tracer: TraceRecorder) -> bool:
        step.status = "running"
        step.attempts += 1
        step.started_at = utc_now()
        step.finished_at = None
        step.error = None
        state.current_step = step.name
        self.save_state(state)
        with tracer.span(
            f"agent_workflow.step.{step.name}",
            run_id=state.run_id,
            repo_path=state.repo_path,
            workflow=state.workflow,
            attempt=step.attempts,
            otel_hint=trace_enabled_hint(),
        ) as span:
            try:
                getattr(self, f"_step_{step.name}")(state, step, span)
                step.status = "succeeded"
                return True
            except StepFailure as exc:
                step.status = exc.status
                step.exit_code = exc.exit_code
                step.timed_out = exc.timed_out
                step.error = str(exc)
                state.status = exc.run_status
                span["attributes"] = {**span["attributes"], "error": str(exc), "exit_code": exc.exit_code, "timed_out": exc.timed_out}
                span["status_code"] = "ERROR"
                span["status_message"] = str(exc)
                return False
            finally:
                step.finished_at = utc_now()
                self.save_state(state)

    def _step_load_task(self, state: RunState, _step: StepState, span: dict[str, object]) -> None:
        task_dir = Path(state.task_dir)
        if (task_dir / "task.md").exists():
            return
        task_dir.mkdir(parents=True, exist_ok=True)
        source_dir = self._pending_task_source(state)
        if source_dir:
            for item in source_dir.iterdir():
                if item.is_file():
                    shutil.copy2(item, task_dir / item.name)
        if not (task_dir / "task.md").exists():
            raise StepFailure("blocked", "blocked", "task.md was not created")
        span["attributes"] = {**span["attributes"], "task_file": str(task_dir / "task.md")}

    def _step_create_worktree(self, state: RunState, _step: StepState, span: dict[str, object]) -> None:
        if state.worktree_path and Path(state.worktree_path).exists():
            return
        repo = Path(state.repo_path)
        ref = state.base_ref or git_output(repo, ["rev-parse", "--verify", "origin/main"], allow_fail=True)
        if not ref:
            ref = git_output(repo, ["rev-parse", "--verify", "HEAD"])
        state.base_ref = ref.strip()
        worktree = self.worktrees_dir / state.run_id / "repo"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        run_checked(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), state.base_ref], cwd=repo)
        state.worktree_path = str(worktree)
        span["attributes"] = {**span["attributes"], "worktree_path": state.worktree_path, "base_ref": state.base_ref}

    def _step_run_executor(self, state: RunState, step: StepState, span: dict[str, object]) -> None:
        task_text = render_task_text(Path(state.task_dir))
        args = [state.executor_bin, "--pipeline", "--skip-git", "--quiet", "--workflow", state.workflow, "--task", task_text]
        if state.provider:
            args.extend(["--provider", state.provider])
        if state.model:
            args.extend(["--model", state.model])
        result = run_logged(args, Path(state.worktree_path or state.repo_path), Path(state.run_dir) / "logs", "run_executor", state.timeout_seconds)
        step.exit_code = result.exit_code
        step.timed_out = result.timed_out
        span["attributes"] = {**span["attributes"], **result.attrs()}
        if result.timed_out:
            raise StepFailure("timed_out", "timed_out", "executor command timed out", result.exit_code, True)
        if result.exit_code != 0:
            raise StepFailure("failed", "failed", f"executor command exited with {result.exit_code}", result.exit_code, False)

    def _step_run_qc(self, state: RunState, step: StepState, span: dict[str, object]) -> None:
        result = run_logged(["bash", "-lc", state.verify_command], Path(state.worktree_path or state.repo_path), Path(state.run_dir) / "logs", "run_qc", state.timeout_seconds)
        step.exit_code = result.exit_code
        step.timed_out = result.timed_out
        span["attributes"] = {**span["attributes"], **result.attrs()}
        if result.timed_out:
            raise StepFailure("timed_out", "timed_out", "QC command timed out", result.exit_code, True)
        if result.exit_code != 0:
            raise StepFailure("qc_failed", "qc_failed", f"QC command exited with {result.exit_code}", result.exit_code, False)

    def _step_write_summary(self, state: RunState, _step: StepState, span: dict[str, object]) -> None:
        self._write_summary(state)
        span["attributes"] = {**span["attributes"], "summary_path": state.summary_path}

    def _finalize_failed_summary(self, state: RunState) -> None:
        self._write_summary(state)
        self.save_state(state)

    def _write_summary(self, state: RunState) -> None:
        task_preview = render_task_text(Path(state.task_dir))[:1200]
        lines = [
            f"# agent-workflow run {state.run_id}",
            "",
            f"- status: `{state.status}`",
            f"- repo: `{state.repo_path}`",
            f"- worktree: `{state.worktree_path or ''}`",
            f"- workflow: `{state.workflow}`",
            f"- base_ref: `{state.base_ref or ''}`",
            f"- trace: `{state.trace_path}`",
            "",
            "## steps",
        ]
        for step in state.steps:
            line = f"- {step.name}: `{step.status}` attempts={step.attempts}"
            if step.exit_code is not None:
                line += f" exit={step.exit_code}"
            if step.timed_out:
                line += " timed_out=true"
            if step.error:
                line += f" error={step.error}"
            lines.append(line)
        lines.extend(["", "## task", "", "```text", task_preview, "```", ""])
        Path(state.summary_path).write_text("\n".join(lines), encoding="utf-8")

    def _pending_task_source(self, state: RunState) -> Path | None:
        marker = state.run_path / "task_source.json"
        if not marker.exists():
            return None
        data = json.loads(marker.read_text(encoding="utf-8"))
        kind = data["kind"]
        if kind == "dir":
            return Path(data["path"])
        task_dir = Path(state.task_dir)
        task_dir.mkdir(parents=True, exist_ok=True)
        if kind == "file":
            shutil.copy2(data["path"], task_dir / "task.md")
        elif kind == "text":
            (task_dir / "task.md").write_text(data["text"].strip() + "\n", encoding="utf-8")
        return None

    def _apply_overrides(self, state: RunState, verify_command: str | None, timeout_seconds: float | None) -> None:
        if verify_command:
            state.verify_command = verify_command
        if timeout_seconds is not None:
            state.timeout_seconds = timeout_seconds
        self.save_state(state)

    def _next_step_index(self, state: RunState) -> int:
        for index, step in enumerate(state.steps):
            if step.status != "succeeded":
                if step.status == "running":
                    step.status = "pending"
                return index
        return len(state.steps) - 1

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.execute(
                """
                create table if not exists jobs (
                  run_id text primary key,
                  status text not null,
                  current_step text,
                  repo_path text not null,
                  summary_path text not null,
                  created_at text not null,
                  updated_at text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists queue (
                  job_id text primary key,
                  status text not null,
                  config_json text not null,
                  run_id text,
                  summary_path text,
                  error text,
                  created_at text not null,
                  updated_at text not null
                )
                """
            )

    def _upsert_job(self, state: RunState) -> None:
        with self._db() as conn:
            conn.execute(
                """
                insert into jobs(run_id, status, current_step, repo_path, summary_path, created_at, updated_at)
                values(?, ?, ?, ?, ?, ?, ?)
                on conflict(run_id) do update set
                  status=excluded.status,
                  current_step=excluded.current_step,
                  repo_path=excluded.repo_path,
                  summary_path=excluded.summary_path,
                  updated_at=excluded.updated_at
                """,
                (state.run_id, state.status, state.current_step, state.repo_path, state.summary_path, state.created_at, state.updated_at),
            )

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("pragma journal_mode=wal")
        return conn

    def _write_task_source(self, run_dir: Path, config: RunnerConfig) -> None:
        if config.task_dir:
            data = {"kind": "dir", "path": str(config.task_dir.expanduser().resolve())}
        elif config.task_file:
            data = {"kind": "file", "path": str(config.task_file.expanduser().resolve())}
        elif config.task_text:
            data = {"kind": "text", "text": config.task_text}
        else:
            raise ValueError("one of --task-dir, --task-file, or --task-text is required")
        (run_dir / "task_source.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _normalize_config(self, config: RunnerConfig) -> RunnerConfig:
        return RunnerConfig(
            state_dir=self.state_dir,
            repo_path=config.repo_path.expanduser().resolve() if config.repo_path else None,
            task_dir=config.task_dir.expanduser().resolve() if config.task_dir else None,
            task_file=config.task_file.expanduser().resolve() if config.task_file else None,
            task_text=config.task_text,
            workflow=config.workflow,
            verify_command=config.verify_command,
            timeout_seconds=config.timeout_seconds,
            executor_bin=config.executor_bin,
            provider=config.provider,
            model=config.model,
            base_ref=config.base_ref,
        )

    def _claim_next_job(self) -> tuple[str, RunnerConfig] | None:
        now = utc_now()
        with self._db() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select job_id, config_json from queue where status = 'queued' order by created_at limit 1"
            ).fetchone()
            if row is None:
                conn.execute("commit")
                return None
            conn.execute(
                "update queue set status = 'running', updated_at = ? where job_id = ?",
                (now, row[0]),
            )
            conn.execute("commit")
        return str(row[0]), config_from_dict(json.loads(row[1]), self.state_dir)

    def _finish_queue_job(self, job_id: str, status: str, run_id: str, summary_path: str, error: str) -> None:
        with self._db() as conn:
            conn.execute(
                """
                update queue
                set status = ?, run_id = ?, summary_path = ?, error = ?, updated_at = ?
                where job_id = ?
                """,
                (status, run_id, summary_path, error, utc_now(), job_id),
            )


class StepFailure(Exception):
    def __init__(self, status: str, run_status: str, message: str, exit_code: int | None = None, timed_out: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.run_status = run_status
        self.exit_code = exit_code
        self.timed_out = timed_out


@dataclass
class CommandResult:
    exit_code: int
    timed_out: bool
    stdout_path: str
    stderr_path: str

    def attrs(self) -> dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
        }


def run_logged(args: Sequence[str], cwd: Path, log_dir: Path, name: str, timeout: float) -> CommandResult:
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{name}.stdout.log"
    stderr_path = log_dir / f"{name}.stderr.log"
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        proc = subprocess.Popen(args, cwd=cwd, stdout=stdout, stderr=stderr, start_new_session=True)
        timed_out = False
        try:
            proc.wait(timeout=timeout if timeout > 0 else None)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    return CommandResult(proc.returncode if proc.returncode is not None else -1, timed_out, str(stdout_path), str(stderr_path))


def run_checked(args: Sequence[str], cwd: Path) -> None:
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise StepFailure("failed", "failed", f"{' '.join(args)} failed: {result.stdout.strip()}", result.returncode)


def git_output(repo: Path, args: Sequence[str], allow_fail: bool = False) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        if allow_fail:
            return ""
        raise StepFailure("failed", "failed", f"git {' '.join(args)} failed: {result.stderr.strip()}", result.returncode)
    return result.stdout.strip()


def render_task_text(task_dir: Path) -> str:
    parts = []
    for name in ["task.md", "acceptance.md", "constraints.md", "context.md"]:
        path = task_dir / name
        if path.exists():
            parts.append(f"# {name}\n\n{path.read_text(encoding='utf-8').strip()}")
    return "\n\n".join(parts).strip()


def config_to_dict(config: RunnerConfig) -> dict[str, object]:
    return {
        "repo_path": str(config.repo_path) if config.repo_path else None,
        "task_dir": str(config.task_dir) if config.task_dir else None,
        "task_file": str(config.task_file) if config.task_file else None,
        "task_text": config.task_text,
        "workflow": config.workflow,
        "verify_command": config.verify_command,
        "timeout_seconds": config.timeout_seconds,
        "executor_bin": config.executor_bin,
        "provider": config.provider,
        "model": config.model,
        "base_ref": config.base_ref,
    }


def config_from_dict(data: dict[str, object], state_dir: Path) -> RunnerConfig:
    return RunnerConfig(
        state_dir=state_dir,
        repo_path=Path(str(data["repo_path"])) if data.get("repo_path") else None,
        task_dir=Path(str(data["task_dir"])) if data.get("task_dir") else None,
        task_file=Path(str(data["task_file"])) if data.get("task_file") else None,
        task_text=str(data["task_text"]) if data.get("task_text") is not None else None,
        workflow=str(data.get("workflow") or "default"),
        verify_command=str(data["verify_command"]) if data.get("verify_command") else None,
        timeout_seconds=float(data.get("timeout_seconds") or 0),
        executor_bin=str(data.get("executor_bin") or "takt"),
        provider=str(data["provider"]) if data.get("provider") else None,
        model=str(data["model"]) if data.get("model") else None,
        base_ref=str(data["base_ref"]) if data.get("base_ref") else None,
    )


def new_run_id() -> str:
    suffix = os.urandom(4).hex()
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{suffix}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
