from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workflow.storage import RunStore
from agent_workflow.state import RunState


class RunStoreMigrationTest(unittest.TestCase):
    def test_legacy_state_and_analytics_are_imported_once(self) -> None:
        """旧三重保存をcanonical runs系tableへ一度だけ統合する。"""

        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            run_id = "legacy-run"
            run_dir = state_dir / "runs" / run_id
            task_dir = run_dir / "task"
            task_dir.mkdir(parents=True)
            (task_dir / "task.md").write_text("legacy task\n", encoding="utf-8")
            state_path = run_dir / "state.json"
            state_path.write_text(
                json.dumps(self._legacy_state(state_dir, run_id), indent=2) + "\n",
                encoding="utf-8",
            )
            self._create_legacy_database(state_dir / "jobs.sqlite", run_id, run_dir)

            store = RunStore(state_dir)
            self.assertEqual(1, store.initialize())

            loaded = store.load(run_id)
            self.assertEqual("succeeded", loaded.status)
            self.assertEqual("gpt-state", loaded.model)
            self.assertEqual(0, loaded.qc_repair_attempts)
            self.assertEqual(2, loaded.step("run_qc").attempts)
            with sqlite3.connect(state_dir / "jobs.sqlite") as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type = 'table'"
                    ).fetchall()
                }
                attempts = conn.execute(
                    """
                    select attempt, status from step_attempts
                    where run_id = ? and step_name = 'run_qc' order by attempt
                    """,
                    (run_id,),
                ).fetchall()
                attempt_columns = {
                    row[1] for row in conn.execute("pragma table_info(step_attempts)").fetchall()
                }
            self.assertTrue({"runs", "run_steps", "step_attempts"}.issubset(tables))
            self.assertTrue(
                {"jobs", "run_metrics", "analytics_schema_migrations"}.isdisjoint(tables)
            )
            self.assertEqual([(1, "qc_failed"), (2, "succeeded")], attempts)
            self.assertTrue({"stdout_path", "stderr_path"}.issubset(attempt_columns))

            # Legacy artifacts are retained, but the completed marker prevents re-import.
            legacy = json.loads(state_path.read_text(encoding="utf-8"))
            legacy["status"] = "failed"
            state_path.write_text(json.dumps(legacy), encoding="utf-8")
            self.assertEqual(0, store.initialize())
            self.assertEqual("succeeded", store.load(run_id).status)
            self.assertTrue(state_path.exists())

    def test_qc_repair_attempts_round_trip_through_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            run_dir = state_dir / "runs" / "run-with-budget"
            task_dir = run_dir / "task"
            task_dir.mkdir(parents=True)
            (task_dir / "task.md").write_text("task\n", encoding="utf-8")
            store = RunStore(state_dir)
            store.initialize()
            state = self._legacy_state(state_dir, "run-with-budget")
            state["status"] = "running"
            state["qc_repair_attempts"] = 4
            loaded = RunState.from_dict(state)
            store.save(loaded)

            restored = store.load("run-with-budget")
            self.assertEqual(4, restored.qc_repair_attempts)

    def test_candidate_chain_and_attempt_metadata_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            store = RunStore(state_dir)
            store.initialize()
            run_dir = state_dir / "runs" / "run-with-candidates"
            task_dir = run_dir / "task"
            task_dir.mkdir(parents=True)
            (task_dir / "task.md").write_text("task\n", encoding="utf-8")
            state = self._legacy_state(state_dir, "run-with-candidates")
            state["status"] = "failed"
            state["candidate_chain"] = [["provider-a", "gpt-a"], ["provider-b", "gpt-b"]]
            state["candidate_index"] = 1
            state["lineage_id"] = "lineage-xyz"
            state["candidate_checkpoint"] = "lineage-xyz-candidate-1"
            for step in state["steps"]:
                if step["name"] == "run_executor":
                    step["candidate_index"] = 1
                    step["candidate_provider"] = "provider-b"
                    step["candidate_model"] = "gpt-b"
                    step["candidate_execution_id"] = "lineage-xyz:1:provider-b:gpt-b:1"
                    step["status"] = "failed"
                    step["exit_code"] = 12
                    step["timed_out"] = False
                    step["error"] = "provider unavailable"
            loaded = RunState.from_dict(state)
            store.save(loaded)

            restored = store.load("run-with-candidates")
            self.assertEqual([("provider-a", "gpt-a"), ("provider-b", "gpt-b")], restored.candidate_chain)
            self.assertEqual(1, restored.candidate_index)
            self.assertEqual("lineage-xyz", restored.lineage_id)
            self.assertEqual("lineage-xyz-candidate-1", restored.candidate_checkpoint)

            with sqlite3.connect(state_dir / "jobs.sqlite") as conn:
                row = conn.execute(
                    """
                    select candidate_chain, candidate_index, lineage_id, candidate_checkpoint
                    from runs where run_id = ?
                    """,
                    ("run-with-candidates",),
                ).fetchone()
                self.assertIsNotNone(row)
                attempts = conn.execute(
                    """
                    select candidate_index, candidate_provider, candidate_model, candidate_execution_id, failure_category
                    from step_attempts
                    where run_id = ? and step_name = 'run_executor'
                    order by attempt
                    """,
                    ("run-with-candidates",),
                ).fetchall()
            self.assertIsNotNone(row[0])
            self.assertEqual([["provider-a", "gpt-a"], ["provider-b", "gpt-b"]], json.loads(row[0]))
            self.assertEqual("provider-b", attempts[0][1])
            self.assertEqual("gpt-b", attempts[0][2])
            self.assertEqual("lineage-xyz:1:provider-b:gpt-b:1", attempts[0][3])
            self.assertEqual("provider_unavailable", attempts[0][4])

    def test_legacy_row_without_candidate_chain_falls_back_to_provider_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            store = RunStore(state_dir)
            store.initialize()
            state = self._legacy_state(state_dir, "legacy-candidates")
            loaded = RunState.from_dict(state)
            store.save(loaded)
            with sqlite3.connect(state_dir / "jobs.sqlite") as conn:
                conn.execute("update runs set candidate_chain = NULL where run_id = ?", ("legacy-candidates",))
                conn.commit()
            restored = store.load("legacy-candidates")
            self.assertEqual([("openai", "gpt-state")], restored.candidate_chain)

    @staticmethod
    def _legacy_state(state_dir: Path, run_id: str) -> dict[str, object]:
        run_dir = state_dir / "runs" / run_id
        started = "2026-07-01T00:00:00+00:00"
        finished = "2026-07-01T00:00:05+00:00"
        steps = []
        for name in ["load_task", "create_worktree", "run_executor", "run_qc", "write_summary"]:
            steps.append(
                {
                    "name": name,
                    "status": "succeeded",
                    "attempts": 2 if name == "run_qc" else 1,
                    "started_at": started,
                    "finished_at": finished,
                    "exit_code": 0,
                    "timed_out": False,
                    "error": None,
                }
            )
        return {
            "run_id": run_id,
            "status": "succeeded",
            "repo_path": "/tmp/legacy-repo",
            "run_dir": str(run_dir),
            "task_dir": str(run_dir / "task"),
            "workflow": "implementation",
            "verify_command": "pytest",
            "timeout_seconds": 600,
            "executor_bin": "takt",
            "provider": "openai",
            "model": "gpt-state",
            "task_type": "bug_fix",
            "base_ref": "abc123",
            "purpose": "workflow",
            "repair_for_run_id": None,
            "worktree_path": None,
            "summary_path": str(run_dir / "summary.md"),
            "trace_path": str(run_dir / "trace.jsonl"),
            "current_step": None,
            "created_at": started,
            "updated_at": finished,
            "steps": steps,
        }

    @staticmethod
    def _create_legacy_database(db_path: Path, run_id: str, run_dir: Path) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                create table jobs (
                  run_id text primary key, status text not null, current_step text,
                  repo_path text not null, summary_path text not null,
                  created_at text not null, updated_at text not null
                );
                create table analytics_schema_migrations (
                  version integer primary key, applied_at text not null
                );
                create table run_metrics (
                  run_id text primary key, status text not null, purpose text not null,
                  repo_path text not null, workflow text not null, executor_bin text not null,
                  provider text, model text, task_type text not null, base_ref text,
                  qc_profile_hash text not null, task_sha256 text, task_bytes integer,
                  created_at text not null, updated_at text not null, finished_at text,
                  elapsed_seconds real, executor_attempts integer not null,
                  qc_attempts integer not null, first_pass_qc integer, eventual_qc integer,
                  changed_files integer, additions integer, deletions integer
                );
                create table step_attempts (
                  run_id text not null, step_name text not null, attempt integer not null,
                  status text not null, started_at text, finished_at text,
                  duration_seconds real, exit_code integer, timed_out integer not null,
                  error text, failure_category text,
                  primary key(run_id, step_name, attempt)
                );
                """
            )
            conn.execute(
                "insert into analytics_schema_migrations values(1, '2026-07-01T00:00:00+00:00')"
            )
            conn.execute(
                "insert into jobs values(?, 'succeeded', null, ?, ?, ?, ?)",
                (
                    run_id,
                    "/tmp/legacy-repo",
                    str(run_dir / "summary.md"),
                    "2026-07-01T00:00:00+00:00",
                    "2026-07-01T00:00:05+00:00",
                ),
            )
            conn.execute(
                """
                insert into run_metrics values(
                  ?, 'succeeded', 'workflow', '/tmp/legacy-repo', 'implementation',
                  'takt', 'openai', 'gpt-metric', 'bug_fix', 'abc123', 'qc-hash',
                  'task-hash', 12, ?, ?, ?, 5.0, 1, 1, 0, 1, 2, 3, 1
                )
                """,
                (
                    run_id,
                    "2026-07-01T00:00:00+00:00",
                    "2026-07-01T00:00:05+00:00",
                    "2026-07-01T00:00:05+00:00",
                ),
            )
            conn.execute(
                """
                insert into step_attempts values(
                  ?, 'run_qc', 1, 'qc_failed', ?, ?, 1.0, 1, 0, 'QC failed', 'qc_failure'
                )
                """,
                (
                    run_id,
                    "2026-07-01T00:00:02+00:00",
                    "2026-07-01T00:00:03+00:00",
                ),
            )


if __name__ == "__main__":
    unittest.main()
