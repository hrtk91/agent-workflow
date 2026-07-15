from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.pipeline import PipelineSnapshotReader, pipeline_items
from agent_workflow.runner import RunnerConfig, WorkflowRunner
from agent_workflow.tui import TuiCommand, parse_command


class PipelineSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.runner = WorkflowRunner(self.state_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reader_returns_queue_and_run_pipeline_without_writing(self) -> None:
        job_id = self.runner.enqueue(
            RunnerConfig(
                state_dir=self.state_dir,
                repo_path=self.root / "repo",
                task_text="queued task",
                verify_command="true",
            )
        )
        self._insert_run("run-1", purpose="workflow")
        db_path = self.state_dir / "jobs.sqlite"
        before = self._database_shape(db_path)

        snapshot = PipelineSnapshotReader(db_path).snapshot()

        after = self._database_shape(db_path)
        self.assertEqual(before, after)
        self.assertEqual([job_id], [job.job_id for job in snapshot.jobs])
        self.assertEqual(["run-1"], [run.run_id for run in snapshot.runs])
        run = snapshot.runs[0]
        self.assertEqual("run_executor", run.current_step)
        self.assertEqual("running", run.steps[2].status)
        self.assertGreaterEqual(run.steps[2].duration_seconds or 0, 1.0)
        self.assertEqual("run-1", pipeline_items(snapshot, "running")[0].item_id)
        self.assertEqual(job_id, pipeline_items(snapshot, "queued")[0].item_id)

        encoded = snapshot.to_dict()
        self.assertEqual("run-1", encoded["runs"][0]["run_id"])
        self.assertIsInstance(encoded["runs"][0]["steps"], list)

    def test_reader_hides_repair_runs_unless_requested(self) -> None:
        self.runner.enqueue(
            RunnerConfig(
                state_dir=self.state_dir,
                repo_path=self.root / "repo",
                task_text="repair task",
                verify_command="true",
                purpose="repair",
            )
        )
        self._insert_run("repair-run", purpose="repair")

        reader = PipelineSnapshotReader(self.state_dir / "jobs.sqlite")

        hidden = reader.snapshot()
        visible = reader.snapshot(include_repair=True)
        self.assertEqual((), hidden.jobs)
        self.assertEqual((), hidden.runs)
        self.assertEqual(["repair-run"], [run.run_id for run in visible.runs])
        self.assertEqual(1, len(visible.jobs))

    def test_missing_database_returns_empty_snapshot(self) -> None:
        snapshot = PipelineSnapshotReader(self.root / "missing.sqlite").snapshot()

        self.assertEqual((), snapshot.jobs)
        self.assertEqual((), snapshot.runs)

    def _insert_run(self, run_id: str, *, purpose: str) -> None:
        now = "2026-07-15T00:00:00+00:00"
        with sqlite3.connect(self.state_dir / "jobs.sqlite") as conn:
            conn.execute(
                """
                insert into runs(
                  run_id, status, repo_path, workflow, verify_command, timeout_seconds,
                  executor_bin, task_type, purpose, current_step, summary_path,
                  created_at, updated_at
                ) values(?, 'running', ?, 'default', 'true', 60, 'takt', 'bug_fix', ?, ?, ?, ?, ?)
                """,
                (run_id, str(self.root / "repo"), purpose, "run_executor", str(self.root / f"{run_id}.md"), now, now),
            )
            for position, name in enumerate(["load_task", "create_worktree", "run_executor", "run_qc", "write_summary"]):
                status = "running" if name == "run_executor" else ("succeeded" if position < 2 else "pending")
                started_at = "2026-07-14T23:59:58+00:00" if name == "run_executor" else None
                conn.execute(
                    """
                    insert into run_steps(
                      run_id, position, step_name, status, attempts, started_at, finished_at,
                      exit_code, timed_out, error, stdout_path, stderr_path
                    ) values(?, ?, ?, ?, ?, ?, null, null, 0, null, null, null)
                    """,
                    (run_id, position, name, status, 1 if position < 3 else 0, started_at),
                )

    @staticmethod
    def _database_shape(db_path: Path) -> tuple[tuple[str, ...], int, int]:
        with sqlite3.connect(db_path) as conn:
            tables = tuple(
                row[0]
                for row in conn.execute(
                    "select name from sqlite_master where type = 'table' order by name"
                ).fetchall()
            )
            jobs = int(conn.execute("select count(*) from queue").fetchone()[0])
            runs = int(conn.execute("select count(*) from runs").fetchone()[0])
        return tables, jobs, runs


class TuiCommandTest(unittest.TestCase):
    def test_parse_command_supports_menu_commands_and_aliases(self) -> None:
        self.assertEqual(TuiCommand("filter", ("running",)), parse_command(":filter running"))
        self.assertEqual(TuiCommand("filter", ("attention",)), parse_command("f attention"))
        self.assertEqual(TuiCommand("detail"), parse_command("d"))
        self.assertEqual(TuiCommand("quit"), parse_command("q"))

    def test_parse_command_rejects_unknown_or_invalid_arguments(self) -> None:
        with self.assertRaises(ValueError):
            parse_command(":filter unknown")
        with self.assertRaises(ValueError):
            parse_command(":refresh now")
        with self.assertRaises(ValueError):
            parse_command(":does-not-exist")


if __name__ == "__main__":
    unittest.main()
