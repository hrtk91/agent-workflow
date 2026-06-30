from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import configparser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from agent_workflow.state import RunState, StepState
from agent_workflow.tracing import TraceRecorder, trace_enabled_hint

STEPS = ["load_task", "create_worktree", "run_executor", "run_qc", "write_summary"]
FAILURE_NOTIFY_STATUSES = {"blocked", "failed", "qc_failed", "timed_out"}
AUTO_REPAIR_MAX_ATTEMPTS = 2
QC_REPAIR_MAX_ATTEMPTS = 5


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
    purpose: str = "workflow"
    repair_for_run_id: str | None = None


@dataclass
class AutoRepairConfig:
    workflow: str = "default"
    verify_command: str | None = None
    timeout_seconds: float | None = None
    executor_bin: str | None = None
    provider: str | None = None
    model: str | None = None
    scan_existing: bool = False


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
            purpose=config.purpose,
            repair_for_run_id=config.repair_for_run_id,
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

    def tick(
        self,
        max_runs: int = 1,
        notify_command: str | None = None,
        notify_statuses: set[str] | None = FAILURE_NOTIFY_STATUSES,
        auto_repair: AutoRepairConfig | None = None,
    ) -> list[dict[str, str]]:
        if auto_repair and auto_repair.scan_existing:
            self.enqueue_auto_repairs_for_failures(auto_repair, limit=max_runs)
        results: list[dict[str, str]] = []
        for _ in range(max_runs):
            job = self._claim_next_job()
            if job is None:
                break
            job_id, config = job
            results.append(self.run_claimed_job(job_id, config, notify_command, notify_statuses, auto_repair=auto_repair))
        return results

    def worker(
        self,
        interval_seconds: float = 60,
        max_runs_per_tick: int = 1,
        notify_command: str | None = None,
        notify_statuses: set[str] | None = FAILURE_NOTIFY_STATUSES,
        parallelism: int = 1,
        repo_parallelism: int = 1,
        spawn_children: bool = True,
        stop_when_idle: bool = False,
        recover_stale_running: bool = True,
        auto_repair: AutoRepairConfig | None = None,
    ) -> None:
        if parallelism < 1:
            raise ValueError("parallelism must be positive")
        if max_runs_per_tick < 1:
            raise ValueError("max_runs_per_tick must be positive")
        if recover_stale_running:
            recovered = self.recover_stale_running("worker recovered stale running job on startup")
            if recovered:
                print(f"recovered_stale_running\t{recovered}", flush=True)
        if not spawn_children:
            while True:
                results = self.tick(
                    max_runs=max_runs_per_tick,
                    notify_command=notify_command,
                    notify_statuses=notify_statuses,
                    auto_repair=auto_repair,
                )
                if stop_when_idle and not results:
                    return
                time.sleep(interval_seconds)

        active: dict[str, tuple[subprocess.Popen[bytes], str]] = {}
        while True:
            for job_id, (process, repo_key) in list(active.items()):
                exit_code = process.poll()
                if exit_code is None:
                    continue
                if exit_code != 0:
                    self._fail_running_queue_job(job_id, f"worker child exited with {exit_code}")
                del active[job_id]

            if auto_repair and auto_repair.scan_existing and not active:
                for repair_job_id in self.enqueue_auto_repairs_for_failures(auto_repair, limit=max_runs_per_tick):
                    print(f"auto_repair_queued\t{repair_job_id}", flush=True)

            claimed = 0
            while len(active) < parallelism and claimed < max_runs_per_tick:
                blocked_repos = self._blocked_worker_repos(active, repo_parallelism)
                job = self._claim_next_job(blocked_repo_paths=blocked_repos)
                if job is None:
                    break
                job_id, config = job
                repo_key = self._repo_key(config)
                try:
                    process = self._spawn_claimed_job(job_id, notify_command, notify_statuses, auto_repair)
                except Exception as exc:
                    self._finish_queue_job(job_id, "failed", "", "", str(exc))
                    continue
                active[job_id] = (process, repo_key)
                print(f"started\t{job_id}\tpid={process.pid}\trepo={repo_key}", flush=True)
                claimed += 1

            if stop_when_idle and not active and claimed == 0:
                return
            time.sleep(interval_seconds)

    def run_claimed_job(
        self,
        job_id: str,
        config: RunnerConfig | None = None,
        notify_command: str | None = None,
        notify_statuses: set[str] | None = FAILURE_NOTIFY_STATUSES,
        auto_repair: AutoRepairConfig | None = None,
    ) -> dict[str, str]:
        if config is None:
            config = self._load_claimed_queue_config(job_id)
        try:
            state = self.run_new(config)
            self._finish_queue_job(job_id, state.status, state.run_id, state.summary_path, "")
            result = {"job_id": job_id, "status": state.status, "run_id": state.run_id, "summary_path": state.summary_path}
            try:
                repair_job_id = self.maybe_enqueue_auto_repair(state, config, auto_repair)
                if repair_job_id:
                    result["repair_job_id"] = repair_job_id
            except Exception as exc:
                result["auto_repair_error"] = str(exc)
            try:
                repair_action_job_id = self.maybe_enqueue_repair_action(state, config)
                if repair_action_job_id:
                    result["repair_action_job_id"] = repair_action_job_id
            except Exception as exc:
                result["repair_action_error"] = str(exc)
            notify_error = ""
            if config.purpose != "repair":
                notify_error = self.notify_state(state, notify_command, notify_statuses, job_id=job_id)
            if notify_error:
                result["notify_error"] = notify_error
            return result
        except Exception as exc:
            self._finish_queue_job(job_id, "failed", "", "", str(exc))
            return {"job_id": job_id, "status": "failed", "run_id": "", "summary_path": "", "error": str(exc)}

    def enqueue_auto_repairs_for_failures(self, auto_repair: AutoRepairConfig | None, limit: int = 20) -> list[str]:
        if auto_repair is None:
            return []
        job_ids: list[str] = []
        for state in self._failed_states_without_repair(limit):
            source_config = runner_config_from_state(state, self.state_dir)
            repair_job_id = self.maybe_enqueue_auto_repair(state, source_config, auto_repair)
            if repair_job_id:
                job_ids.append(repair_job_id)
        return job_ids

    def maybe_enqueue_repair_action(self, state: RunState, config: RunnerConfig) -> str:
        if config.purpose != "repair" or state.status != "succeeded" or not config.repair_for_run_id:
            return ""
        draft = self._latest_validated_repair_draft(config.repair_for_run_id)
        if not draft:
            return ""
        draft_dir = Path(draft["draft_dir"])
        marker = draft_dir / "action-enqueued.json"
        if marker.exists():
            return ""
        proposed_action = draft["proposed_action"]
        if proposed_action == "human_needed":
            marker.write_text(
                json.dumps({"skipped": "human_needed", "created_at": utc_now()}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return ""
        failed_state = self.load_state(config.repair_for_run_id)
        action_config = self._repair_action_runner_config(failed_state, draft, config)
        action_job_id = self.enqueue(action_config)
        marker.write_text(
            json.dumps(
                {
                    "failed_run_id": failed_state.run_id,
                    "diagnosis_run_id": state.run_id,
                    "repair_draft_id": draft["draft_id"],
                    "repair_action_job_id": action_job_id,
                    "proposed_action": proposed_action,
                    "created_at": utc_now(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return action_job_id

    def maybe_enqueue_auto_repair(
        self,
        failed_state: RunState,
        source_config: RunnerConfig,
        auto_repair: AutoRepairConfig | None,
    ) -> str:
        if auto_repair is None:
            return ""
        if failed_state.status not in FAILURE_NOTIFY_STATUSES:
            return ""
        if source_config.purpose in {"repair", "repair_action"}:
            return ""
        if self._has_repair_or_repair_job(failed_state.run_id):
            return ""

        repair_config = self._auto_repair_runner_config(failed_state, source_config, auto_repair)
        repair_job_id = self.enqueue(repair_config)
        self._write_auto_repair_marker(failed_state, repair_job_id, repair_config)
        self._write_summary(failed_state)
        self.save_state(failed_state)
        return repair_job_id

    def _failed_states_without_repair(self, limit: int) -> list[RunState]:
        if limit < 1:
            return []
        scan_limit = max(limit * 10, 50)
        with self._db() as conn:
            rows = conn.execute(
                f"""
                select run_id
                from jobs
                where status in ({','.join('?' for _ in FAILURE_NOTIFY_STATUSES)})
                order by updated_at desc
                limit ?
                """,
                (*sorted(FAILURE_NOTIFY_STATUSES), scan_limit),
            ).fetchall()
        states: list[RunState] = []
        for row in rows:
            run_id = str(row[0])
            if self._has_repair_or_repair_job(run_id):
                continue
            try:
                state = self.load_state(run_id)
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            if state.purpose == "repair":
                continue
            states.append(state)
            if len(states) >= limit:
                break
        return states

    def recover_stale_running(self, error: str) -> int:
        recovered = 0
        with self._db() as conn:
            rows = conn.execute("select run_id from jobs where status = 'running'").fetchall()
        for row in rows:
            run_id = str(row[0])
            try:
                state = self.load_state(run_id)
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            state.status = "failed"
            if state.current_step:
                for step in state.steps:
                    if step.name == state.current_step and step.status == "running":
                        step.status = "failed"
                        step.error = error
                        step.finished_at = utc_now()
                        break
            self._finalize_failed_summary(state)
            recovered += 1
        with self._db() as conn:
            cursor = conn.execute(
                """
                update queue
                set status = 'failed', error = ?, updated_at = ?
                where status = 'running'
                """,
                (error, utc_now()),
            )
            recovered += int(cursor.rowcount or 0)
        return recovered

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

    def status(self, run_id: str | None = None, include_repair: bool = False) -> str:
        if run_id:
            state = self.load_state(run_id)
            lines = [f"{state.run_id}\t{state.status}\t{state.current_step or '-'}\t{state.summary_path}"]
            lines.extend(f"  {s.name}\t{s.status}\tattempts={s.attempts}" for s in state.steps)
            return "\n".join(lines)
        with self._db() as conn:
            queue_rows = conn.execute(
                "select job_id, status, coalesce(run_id, ''), coalesce(summary_path, ''), config_json from queue order by created_at desc limit 100"
            ).fetchall()
            run_rows = conn.execute(
                "select run_id, status, coalesce(current_step, ''), summary_path from jobs order by created_at desc limit 100"
            ).fetchall()
        visible_queue_rows = []
        for row in queue_rows:
            if include_repair or queue_config_purpose(str(row[4])) != "repair":
                visible_queue_rows.append(row[:4])
            if len(visible_queue_rows) >= 20:
                break
        visible_run_rows = []
        for row in run_rows:
            if include_repair or self._run_purpose(str(row[0])) != "repair":
                visible_run_rows.append(row)
            if len(visible_run_rows) >= 20:
                break
        lines = ["queue:"]
        lines.extend("\t".join(["job", *(str(col) for col in row)]) for row in visible_queue_rows)
        lines.append("runs:")
        lines.extend("\t".join(["run", *(str(col) for col in row)]) for row in visible_run_rows)
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

    def notify_state(self, state: RunState, command_template: str | None, statuses: set[str] | None, job_id: str = "") -> str:
        if not command_template:
            return ""
        if statuses is not None and state.status not in statuses:
            return ""
        command = render_notification_command(
            command_template,
            {
                "job_id": job_id,
                "run_id": state.run_id,
                "status": state.status,
                "summary": state.summary_path,
                "discord_summary": str(discord_summary_path(state.summary_path)),
            },
        )
        result = subprocess.run(["bash", "-lc", command], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            return result.stdout.strip() or f"notification command exited with {result.returncode}"
        return ""

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
        index = start_index
        qc_repair_attempts = 0
        while index < len(state.steps):
            step = state.steps[index]
            if step.status == "succeeded":
                index += 1
                continue
            ok = self._run_step(state, step, tracer)
            if not ok:
                if step.name == "run_qc" and qc_repair_attempts < QC_REPAIR_MAX_ATTEMPTS:
                    qc_repair_attempts += 1
                    self._prepare_qc_repair_loop(state, qc_repair_attempts)
                    index = STEPS.index("run_executor")
                    continue
                self._finalize_failed_summary(state)
                return state
            index += 1
        state.status = "succeeded"
        state.current_step = None
        self._write_summary(state)
        self.save_state(state)
        return state

    def _prepare_qc_repair_loop(self, state: RunState, attempt: int) -> None:
        qc_step = state.step("run_qc")
        executor_step = state.step("run_executor")
        self._append_qc_repair_context(state, attempt, qc_step.error or "")
        executor_step.status = "pending"
        executor_step.error = None
        executor_step.exit_code = None
        executor_step.timed_out = False
        qc_step.status = "pending"
        state.status = "running"
        state.current_step = "run_executor"
        self.save_state(state)

    def _append_qc_repair_context(self, state: RunState, attempt: int, error: str) -> None:
        task_dir = Path(state.task_dir)
        log_path = Path(state.run_dir) / "logs" / "run_qc.log"
        excerpt = ""
        if log_path.is_file():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            excerpt = "\n".join(lines[-80:])
        context_path = task_dir / "context.md"
        with context_path.open("a", encoding="utf-8") as f:
            f.write(
                "\n\n"
                f"## QC repair loop {attempt}/{QC_REPAIR_MAX_ATTEMPTS}\n\n"
                "The previous implementation attempt did not satisfy QC. Continue fixing in this same run worktree until the configured QC command is green. Do not report completion until QC passes.\n\n"
            )
            if error:
                f.write(f"QC error: {error}\n\n")
            if excerpt:
                f.write("Recent QC log excerpt:\n\n```text\n")
                f.write(excerpt)
                f.write("\n```\n")

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
        started_at = time.time()
        result = run_logged(args, Path(state.worktree_path or state.repo_path), Path(state.run_dir) / "logs", "run_executor", state.timeout_seconds)
        step.exit_code = result.exit_code
        step.timed_out = result.timed_out
        attrs = result.attrs()
        snapshot = self._snapshot_executor_observability(state, started_at - 2)
        if snapshot:
            attrs["executor_observability_path"] = str(snapshot)
        span["attributes"] = {**span["attributes"], **attrs}
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
        elapsed = elapsed_seconds(state.created_at, None if state.status == "running" else state.updated_at)
        lines = [
            f"# agent-workflow run {state.run_id}",
            "",
            f"- status: `{state.status}`",
            f"- repo: `{state.repo_path}`",
            f"- worktree: `{state.worktree_path or ''}`",
            f"- workflow: `{state.workflow}`",
            f"- purpose: `{state.purpose}`",
            f"- repair_for_run_id: `{state.repair_for_run_id or ''}`",
            f"- base_ref: `{state.base_ref or ''}`",
            f"- created_at: `{state.created_at}`",
            f"- updated_at: `{state.updated_at}`",
            f"- elapsed_seconds: `{elapsed}`",
            f"- timeout_seconds: `{format_seconds(state.timeout_seconds)}`",
            f"- trace: `{state.trace_path}`",
            "",
            "## steps",
        ]
        for step in state.steps:
            line = f"- {step.name}: `{step.status}` attempts={step.attempts}"
            duration = step_duration_seconds(step)
            if duration is not None:
                line += f" duration_seconds={duration}"
            remaining = step_timeout_remaining_seconds(step, state.timeout_seconds)
            if remaining is not None:
                line += f" timeout_remaining_seconds={remaining}"
            if step.exit_code is not None:
                line += f" exit={step.exit_code}"
            if step.timed_out:
                line += " timed_out=true"
            if step.error:
                line += f" error={step.error}"
            lines.append(line)
        lines.extend(self._auto_repair_summary(state))
        lines.extend(self._executor_observability_summary(state))
        lines.extend(["", "## task", "", "```text", task_preview, "```", ""])
        Path(state.summary_path).write_text("\n".join(lines), encoding="utf-8")
        discord_summary_path(state.summary_path).write_text(render_run_discord_summary(state, task_preview), encoding="utf-8")

    def _auto_repair_summary(self, state: RunState) -> list[str]:
        marker = auto_repair_marker_path(state)
        if not marker.exists():
            return []
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        job_id = str(data.get("repair_job_id") or "")
        if not job_id:
            return []
        created_at = str(data.get("created_at") or "")
        return ["", "## auto diagnosis", f"- diagnosis_job_id: `{job_id}`", f"- created_at: `{created_at}`"]

    def _auto_repair_runner_config(
        self,
        failed_state: RunState,
        source_config: RunnerConfig,
        auto_repair: AutoRepairConfig,
    ) -> RunnerConfig:
        repair_repo = Path(failed_state.repo_path)
        repair_base_ref = git_output(repair_repo, ["rev-parse", "--verify", "HEAD"], allow_fail=True) or source_config.base_ref
        return RunnerConfig(
            state_dir=self.state_dir,
            repo_path=repair_repo,
            task_text=render_auto_repair_task(failed_state, self.state_dir),
            workflow=auto_repair.workflow or source_config.workflow,
            verify_command=auto_repair.verify_command or render_auto_repair_verify_command(self.state_dir, failed_state.run_id),
            timeout_seconds=auto_repair.timeout_seconds if auto_repair.timeout_seconds is not None else source_config.timeout_seconds,
            executor_bin=auto_repair.executor_bin or source_config.executor_bin,
            provider=auto_repair.provider if auto_repair.provider is not None else source_config.provider,
            model=auto_repair.model if auto_repair.model is not None else source_config.model,
            base_ref=repair_base_ref,
            purpose="repair",
            repair_for_run_id=failed_state.run_id,
        )

    def _repair_action_runner_config(
        self,
        failed_state: RunState,
        draft: dict[str, str],
        diagnosis_config: RunnerConfig,
    ) -> RunnerConfig:
        repo = Path(failed_state.repo_path)
        base_ref = git_output(repo, ["rev-parse", "--verify", "HEAD"], allow_fail=True) or failed_state.base_ref
        verify_command = draft.get("verify_command") or failed_state.verify_command
        return RunnerConfig(
            state_dir=self.state_dir,
            repo_path=repo,
            task_text=render_repair_action_task(failed_state, draft),
            workflow=failed_state.workflow,
            verify_command=verify_command,
            timeout_seconds=failed_state.timeout_seconds,
            executor_bin=diagnosis_config.executor_bin or failed_state.executor_bin,
            provider=diagnosis_config.provider if diagnosis_config.provider is not None else failed_state.provider,
            model=diagnosis_config.model if diagnosis_config.model is not None else failed_state.model,
            base_ref=base_ref,
            purpose="repair_action",
            repair_for_run_id=failed_state.run_id,
        )

    def _latest_validated_repair_draft(self, failed_run_id: str) -> dict[str, str]:
        with self._db() as conn:
            row = conn.execute(
                """
                select draft_id, failed_run_id, category, risk, proposed_action, status, draft_dir
                from repair_drafts
                where failed_run_id = ? and status = 'validated'
                order by updated_at desc
                limit 1
                """,
                (failed_run_id,),
            ).fetchone()
        if not row:
            return {}
        data = {
            "draft_id": str(row[0]),
            "failed_run_id": str(row[1]),
            "category": str(row[2]),
            "risk": str(row[3]),
            "proposed_action": str(row[4]),
            "status": str(row[5]),
            "draft_dir": str(row[6]),
            "verify_command": "",
            "title": "",
        }
        config_path = Path(data["draft_dir"]) / "repair.ini"
        if config_path.exists():
            parser = configparser.ConfigParser()
            parser.read(config_path, encoding="utf-8")
            if "repair" in parser:
                data["verify_command"] = parser["repair"].get("verify_command", "").strip()
                data["title"] = parser["repair"].get("title", "").strip()
        return data

    def _write_auto_repair_marker(self, failed_state: RunState, repair_job_id: str, repair_config: RunnerConfig) -> None:
        marker = auto_repair_marker_path(failed_state)
        marker.write_text(
            json.dumps(
                {
                    "failed_run_id": failed_state.run_id,
                    "repair_job_id": repair_job_id,
                    "created_at": utc_now(),
                    "workflow": repair_config.workflow,
                    "executor_bin": repair_config.executor_bin,
                    "model": repair_config.model,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _has_repair_or_repair_job(self, failed_run_id: str) -> bool:
        repair_job_statuses: list[str] = []
        with self._db() as conn:
            table = conn.execute(
                "select name from sqlite_master where type = 'table' and name = 'repair_drafts'"
            ).fetchone()
            if table is not None:
                draft = conn.execute(
                    "select draft_id from repair_drafts where failed_run_id = ? limit 1",
                    (failed_run_id,),
                ).fetchone()
                if draft is not None:
                    return True
            rows = conn.execute(
                "select status, config_json from queue"
            ).fetchall()
        for status, config_json in rows:
            try:
                data = json.loads(str(config_json))
            except json.JSONDecodeError:
                continue
            if data.get("purpose") == "repair" and data.get("repair_for_run_id") == failed_run_id:
                repair_job_statuses.append(str(status))
        if any(status in {"queued", "running"} for status in repair_job_statuses):
            return True
        return len(repair_job_statuses) >= AUTO_REPAIR_MAX_ATTEMPTS

    def _snapshot_executor_observability(self, state: RunState, started_at: float) -> Path | None:
        if not state.worktree_path:
            return None
        takt_run = self._latest_takt_run(Path(state.worktree_path) / ".takt" / "runs", min_mtime=started_at)
        if takt_run is None:
            return None
        dest = Path(state.run_dir) / "executor_observability" / "takt" / takt_run.name
        if dest.exists():
            shutil.rmtree(dest)
        copied = False
        for rel in [Path("trace.md"), Path("monitor.json")]:
            source = takt_run / rel
            if source.exists():
                (dest / rel.parent).mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest / rel)
                copied = True
        logs = takt_run / "logs"
        if logs.exists():
            for source in sorted([*logs.glob("*-otel-session-shadow.jsonl"), *logs.glob("*-usage-events.phase.jsonl")]):
                target = dest / "logs" / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied = True
        return dest if copied else None

    def _executor_observability_summary(self, state: RunState) -> list[str]:
        takt_run = self._latest_takt_run(Path(state.run_dir) / "executor_observability" / "takt")
        if takt_run is None and state.worktree_path:
            takt_run = self._latest_takt_run(Path(state.worktree_path) / ".takt" / "runs")
        if takt_run is None:
            return []

        lines = ["", "## executor observability", f"- takt_run: `{takt_run}`"]
        trace_md = takt_run / "trace.md"
        if trace_md.exists():
            lines.append(f"- takt_trace: `{trace_md}`")
            lines.extend(self._takt_trace_overview(trace_md))
        monitor_json = takt_run / "monitor.json"
        if monitor_json.exists():
            lines.append(f"- takt_monitor: `{monitor_json}`")
            lines.extend(self._takt_monitor_overview(monitor_json))
        logs = takt_run / "logs"
        if logs.exists():
            for path in sorted(logs.glob("*-otel-session-shadow.jsonl")):
                lines.append(f"- takt_session_shadow: `{path}` lines={count_lines(path)}")
            for path in sorted(logs.glob("*-usage-events.phase.jsonl")):
                lines.append(f"- takt_phase_usage: `{path}` lines={count_lines(path)}")
        return lines

    def _takt_trace_overview(self, trace_md: Path) -> list[str]:
        prefixes = ("- Status:", "- Iterations:", "- Reason:", "- Started:", "- Ended:")
        overview: list[str] = []
        for line in trace_md.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(prefixes):
                overview.append(f"  {line}")
        return overview

    def _takt_monitor_overview(self, monitor_json: Path) -> list[str]:
        try:
            data = json.loads(monitor_json.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return []
        points_by_metric: dict[str, list[dict[str, object]]] = {}
        for scope in data.get("scopeMetrics") or []:
            for metric in scope.get("metrics") or []:
                points_by_metric[str(metric.get("name") or "")] = list(metric.get("points") or [])

        lines: list[str] = []
        for point in points_by_metric.get("takt.workflow.runs", [])[-1:]:
            attrs = point.get("attributes") or {}
            status = attrs.get("takt.workflow.status")
            abort_kind = attrs.get("takt.workflow.abort.kind")
            if status:
                lines.append(f"- takt_workflow_status: `{status}`")
            if abort_kind:
                lines.append(f"- takt_workflow_abort_kind: `{abort_kind}`")

        for point in points_by_metric.get("takt.workflow.duration", [])[-1:]:
            duration = monitor_duration_ms(point)
            if duration is not None:
                lines.append(f"- takt_workflow_duration_ms: `{duration}`")

        for point in points_by_metric.get("takt.workflow.step.duration", []):
            attrs = point.get("attributes") or {}
            duration = monitor_duration_ms(point)
            if duration is None:
                continue
            name = attrs.get("takt.step.name") or "unknown"
            status = attrs.get("takt.step.status") or "unknown"
            model = attrs.get("takt.model.name") or ""
            suffix = f" model={model}" if model else ""
            lines.append(f"- takt_step_duration: `{name}` status=`{status}` duration_ms=`{duration}`{suffix}")

        for point in points_by_metric.get("takt.workflow.phase.duration", [])[-8:]:
            attrs = point.get("attributes") or {}
            duration = monitor_duration_ms(point)
            if duration is None:
                continue
            step = attrs.get("takt.step.name") or "unknown"
            phase = attrs.get("takt.phase.name") or "unknown"
            status = attrs.get("takt.phase.status") or "unknown"
            lines.append(f"- takt_phase_duration: `{step}/{phase}` status=`{status}` duration_ms=`{duration}`")
        return lines

    def _latest_takt_run(self, runs_dir: Path, min_mtime: float | None = None) -> Path | None:
        if not runs_dir.exists():
            return None
        candidates = [path for path in runs_dir.iterdir() if path.is_dir()]
        if min_mtime is not None:
            candidates = [path for path in candidates if path.stat().st_mtime >= min_mtime]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

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
            purpose=config.purpose,
            repair_for_run_id=config.repair_for_run_id,
        )

    def _claim_next_job(self, blocked_repo_paths: set[str] | None = None) -> tuple[str, RunnerConfig] | None:
        now = utc_now()
        blocked_repo_paths = blocked_repo_paths or set()
        with self._db() as conn:
            conn.execute("begin immediate")
            rows = conn.execute(
                "select job_id, config_json from queue where status = 'queued' order by created_at limit 100"
            ).fetchall()
            selected: tuple[str, str] | None = None
            for row in rows:
                config = config_from_dict(json.loads(row[1]), self.state_dir)
                if self._repo_key(config) in blocked_repo_paths:
                    continue
                selected = (str(row[0]), str(row[1]))
                break
            if selected is None:
                conn.execute("commit")
                return None
            conn.execute(
                "update queue set status = 'running', updated_at = ? where job_id = ?",
                (now, selected[0]),
            )
            conn.execute("commit")
        return selected[0], config_from_dict(json.loads(selected[1]), self.state_dir)

    def _load_claimed_queue_config(self, job_id: str) -> RunnerConfig:
        with self._db() as conn:
            row = conn.execute(
                "select status, config_json from queue where job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"queued job not found: {job_id}")
        if row[0] != "running":
            raise ValueError(f"queued job is not claimed/running: {job_id} status={row[0]}")
        return config_from_dict(json.loads(row[1]), self.state_dir)

    def _spawn_claimed_job(
        self,
        job_id: str,
        notify_command: str | None,
        notify_statuses: set[str] | None,
        auto_repair: AutoRepairConfig | None,
    ) -> subprocess.Popen[bytes]:
        args = [
            sys.executable,
            "-m",
            "agent_workflow",
            "--state-dir",
            str(self.state_dir),
            "run-claimed",
            "--job-id",
            job_id,
        ]
        if notify_command:
            args.extend(["--notify-command", notify_command])
        if notify_statuses is None:
            args.extend(["--notify-statuses", "all"])
        elif notify_statuses:
            args.extend(["--notify-statuses", ",".join(sorted(notify_statuses))])
        append_auto_repair_args(args, auto_repair)
        return subprocess.Popen(args)

    def _blocked_worker_repos(self, active: dict[str, tuple[subprocess.Popen[bytes], str]], repo_parallelism: int) -> set[str]:
        if repo_parallelism < 1:
            return set()
        counts: dict[str, int] = {}
        for _job_id, (_process, repo_key) in active.items():
            counts[repo_key] = counts.get(repo_key, 0) + 1
        return {repo_key for repo_key, count in counts.items() if count >= repo_parallelism}

    def _repo_key(self, config: RunnerConfig) -> str:
        if config.repo_path is None:
            return ""
        try:
            return str(config.repo_path.expanduser().resolve())
        except OSError:
            return str(config.repo_path.expanduser())

    def _run_purpose(self, run_id: str) -> str:
        try:
            return self.load_state(run_id).purpose
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return "workflow"

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

    def _fail_running_queue_job(self, job_id: str, error: str) -> None:
        with self._db() as conn:
            conn.execute(
                """
                update queue
                set status = 'failed', error = ?, updated_at = ?
                where job_id = ? and status = 'running'
                """,
                (error, utc_now(), job_id),
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


def discord_summary_path(summary_path: str | Path) -> Path:
    return Path(summary_path).with_name("hermes-discord-summary.md")


def auto_repair_marker_path(state: RunState) -> Path:
    return state.run_path / "auto-repair-enqueued.json"


def render_run_discord_summary(state: RunState, task_preview: str) -> str:
    headline = {
        "succeeded": "✅ workflow succeeded",
        "qc_failed": "🧪 workflow QC failed",
        "timed_out": "⏳ workflow timed out",
        "blocked": "🧱 workflow blocked",
        "failed": "🛑 workflow failed",
        "running": "🏃 workflow running",
        "queued": "📥 workflow queued",
    }.get(state.status, f"🧭 workflow {state.status}")
    step = current_or_failed_step(state)
    lines = [
        f"{headline}: {state.run_id}",
        f"📌 status: `{state.status}`",
        f"📦 repo: `{state.repo_path}`",
        f"🧭 workflow: `{state.workflow}`",
        f"🔗 summary: `{state.summary_path}`",
    ]
    if step:
        lines.append(f"🔎 step: `{step.name}` status=`{step.status}` attempts=`{step.attempts}`")
        if step.error:
            lines.append(f"💬 reason: {step.error}")
        if step.timed_out:
            lines.append("⏱️ timed_out: `true`")

    title = first_task_line(task_preview)
    if title:
        lines.extend(["", f"📝 task: {title}"])

    actions = notification_actions(state, step)
    if actions:
        lines.extend(["", "🔁 next action"])
        lines.extend(f"- {action}" for action in actions)

    marker = auto_repair_marker_path(state)
    if marker.exists():
        try:
            repair_job_id = str(json.loads(marker.read_text(encoding="utf-8")).get("repair_job_id") or "")
        except (OSError, json.JSONDecodeError):
            repair_job_id = ""
        if repair_job_id:
            lines.extend(["", f"🩺 auto diagnosis queued: `{repair_job_id}`"])

    lines.append("")
    return "\n".join(lines)


def current_or_failed_step(state: RunState) -> StepState | None:
    if state.current_step:
        try:
            return state.step(state.current_step)
        except KeyError:
            pass
    for step in state.steps:
        if step.status not in {"succeeded", "pending"}:
            return step
    for step in state.steps:
        if step.status != "succeeded":
            return step
    return None


def notification_actions(state: RunState, step: StepState | None) -> list[str]:
    if state.status == "succeeded":
        return ["No retry needed."]
    if step is None:
        return [f"▶️ resume candidate: `aw resume --run-id {state.run_id}`"]
    actions: list[str] = []
    if state.status == "timed_out":
        next_timeout = max(int(state.timeout_seconds * 2), int(state.timeout_seconds) + 600, 600)
        actions.append(f"▶️ resume with a larger timeout: `aw resume --run-id {state.run_id} --timeout-seconds {next_timeout}`")
    else:
        actions.append(f"▶️ resume candidate: `aw resume --run-id {state.run_id}`")
    actions.append(f"🔁 retry candidate: `aw retry --run-id {state.run_id} --step {step.name}`")
    if state.status == "qc_failed":
        actions.append("🧪 inspect QC logs, fix the worktree, then retry `run_qc`.")
    elif state.status == "blocked":
        actions.append("✋ human input is needed before retrying the blocked step.")
    elif state.status == "failed":
        actions.append("🧯 inspect executor logs before retrying.")
    return actions


def first_task_line(task_preview: str) -> str:
    for line in task_preview.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:160]
    return ""


def render_notification_command(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", shlex.quote(value))
    return rendered


def queue_config_purpose(config_json: str) -> str:
    try:
        data = json.loads(config_json)
    except json.JSONDecodeError:
        return "workflow"
    return str(data.get("purpose") or "workflow")


def render_auto_repair_verify_command(state_dir: Path, failed_run_id: str) -> str:
    python = shlex.quote(sys.executable)
    state = shlex.quote(str(state_dir))
    return (
        f"{python} -m agent_workflow --state-dir {state} watchdog scan --include-repaired --limit 200 "
        f"| awk -F '\\t' '$1 == \"{failed_run_id}\" && $5 == \"\" {{ bad=1 }} END {{ exit bad }}'"
    )


def render_auto_repair_task(failed_state: RunState, state_dir: Path) -> str:
    step = current_or_failed_step(failed_state)
    step_line = f"{step.name} status={step.status} attempts={step.attempts}" if step else "unknown"
    summary = failed_state.summary_path
    trace = failed_state.trace_path
    logs = str(Path(failed_state.run_dir) / "logs")
    worktree = failed_state.worktree_path or ""
    state_arg = shlex.quote(str(state_dir))
    command_prefix = f"{shlex.quote(sys.executable)} -m agent_workflow --state-dir {state_arg}"
    return f"""Diagnose and draft a repair for agent-workflow run {failed_state.run_id}.

Failed run:
- run_id: {failed_state.run_id}
- status: {failed_state.status}
- failed_step: {step_line}
- repo: {failed_state.repo_path}
- worktree: {worktree}
- summary: {summary}
- trace_jsonl: {trace}
- logs_dir: {logs}

Your job is to recover the workflow loop, not to manually finish the product task.

Read the summary, trace, executor logs, QC logs, and any copied executor observability. Classify the failure using the repair CLI choices and create a validated repair draft. Use the typed CLI as the output tool; do not leave an unvalidated free-form answer.

Create concise Markdown files for:
- diagnosis.md: what failed, the likely cause, and the next repair action.
- evidence.md: relevant file paths and short excerpts only.
- notify-before.md: a brief Japanese notification with a friendly emoji explaining the planned repair.

Then run:

{command_prefix} repair draft \\
  --failed-run-id {failed_state.run_id} \\
  --title "<short title>" \\
  --category <one of dependency_missing, deploy_config, deploy_runtime, implementation_failure, repo_config, runtime_env, test_infra_flake, timeout, transient_external, unknown> \\
  --risk <low|medium|high> \\
  --proposed-action <one of dependency_install_or_update, gateway_restart, human_needed, migration_with_healthcheck, redeploy_and_healthcheck, repo_config_patch, resume_original_run, retry_original_run, runtime_environment_patch, worktree_cleanup_and_retry> \\
  --diagnosis-file diagnosis.md \\
  --evidence-file evidence.md \\
  --notify-before-file notify-before.md

Rules:
- If the failure is an incomplete implementation, classify it as implementation_failure and prefer resume_original_run or retry_original_run unless the evidence points to environment/config.
- If the failure is missing packages, tools, writable cache, daemon state, or local service setup, choose the matching dependency/runtime/repo config action.
- Deployment or migration repair must use risk=high and include the required deployment guardrails.
- Do not create a draft that claims success. The draft is the handoff artifact for the next repair step.
"""


def render_repair_action_task(failed_state: RunState, draft: dict[str, str]) -> str:
    draft_dir = draft.get("draft_dir", "")
    title = draft.get("title") or "validated repair action"
    category = draft.get("category", "")
    risk = draft.get("risk", "")
    proposed_action = draft.get("proposed_action", "")
    verify_command = draft.get("verify_command") or failed_state.verify_command
    return f"""Execute the validated repair action for agent-workflow run {failed_state.run_id}.

Repair draft:
- draft_id: {draft.get("draft_id", "")}
- title: {title}
- category: {category}
- risk: {risk}
- proposed_action: {proposed_action}
- draft_dir: {draft_dir}
- diagnosis: {Path(draft_dir) / "diagnosis.md" if draft_dir else ""}
- evidence: {Path(draft_dir) / "evidence.md" if draft_dir else ""}

Failed run:
- run_id: {failed_state.run_id}
- status: {failed_state.status}
- worktree: {failed_state.worktree_path or ""}
- summary: {failed_state.summary_path}
- trace_jsonl: {failed_state.trace_path}

Your job is to perform the repair, not just describe it.

Read the repair draft diagnosis/evidence and the failed run summary/logs. Apply the proposed repair in this fresh AW worktree. If the draft proposes retry_original_run or resume_original_run, inspect the failed run evidence and complete the original task here instead of merely issuing a retry command.

Completion condition:
- The repair is implemented or the original task is completed in this worktree.
- The configured QC command is all green: {verify_command}
- Do not mark complete just because QC was rerun.
- Do not merge, push, deploy, edit GitHub Issues, or notify Discord from this workflow.
"""


def append_auto_repair_args(args: list[str], auto_repair: AutoRepairConfig | None) -> None:
    if auto_repair is None:
        return
    args.append("--auto-repair")
    args.extend(["--repair-workflow", auto_repair.workflow])
    if auto_repair.scan_existing:
        args.append("--repair-scan-existing")
    if auto_repair.verify_command:
        args.extend(["--repair-verify-command", auto_repair.verify_command])
    if auto_repair.timeout_seconds is not None:
        args.extend(["--repair-timeout-seconds", format_seconds(auto_repair.timeout_seconds)])
    if auto_repair.executor_bin:
        args.extend(["--repair-executor-bin", auto_repair.executor_bin])
    if auto_repair.provider:
        args.extend(["--repair-provider", auto_repair.provider])
    if auto_repair.model:
        args.extend(["--repair-model", auto_repair.model])


def count_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_seconds(value: float | int | None) -> str:
    if value is None:
        return ""
    seconds = float(value)
    if seconds.is_integer():
        return str(int(seconds))
    return str(round(seconds, 3))


def elapsed_seconds(start: str | None, end: str | None = None) -> str:
    started_at = parse_time(start)
    if started_at is None:
        return ""
    ended_at = parse_time(end) or datetime.now(timezone.utc)
    return format_seconds(max(0.0, (ended_at - started_at).total_seconds()))


def step_duration_seconds(step: StepState) -> str | None:
    if not step.started_at:
        return None
    return elapsed_seconds(step.started_at, step.finished_at)


def step_timeout_remaining_seconds(step: StepState, timeout_seconds: float) -> str | None:
    if timeout_seconds <= 0 or step.status != "running" or not step.started_at:
        return None
    started_at = parse_time(step.started_at)
    if started_at is None:
        return None
    elapsed = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
    return format_seconds(max(0.0, timeout_seconds - elapsed))


def monitor_duration_ms(point: dict[str, object]) -> int | None:
    value = point.get("value")
    if isinstance(value, dict):
        raw = value.get("sum")
    else:
        raw = value
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


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
        "purpose": config.purpose,
        "repair_for_run_id": config.repair_for_run_id,
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
        purpose=str(data.get("purpose") or "workflow"),
        repair_for_run_id=str(data["repair_for_run_id"]) if data.get("repair_for_run_id") else None,
    )


def runner_config_from_state(state: RunState, state_dir: Path) -> RunnerConfig:
    return RunnerConfig(
        state_dir=state_dir,
        repo_path=Path(state.repo_path),
        task_text=render_task_text(Path(state.task_dir)),
        workflow=state.workflow,
        verify_command=state.verify_command,
        timeout_seconds=state.timeout_seconds,
        executor_bin=state.executor_bin,
        provider=state.provider,
        model=state.model,
        base_ref=state.base_ref,
        purpose=state.purpose,
        repair_for_run_id=state.repair_for_run_id,
    )


def new_run_id() -> str:
    suffix = os.urandom(4).hex()
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{suffix}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
