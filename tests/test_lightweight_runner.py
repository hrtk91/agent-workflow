from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AW = ROOT / "scripts" / "aw"


class LightweightRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.repo = self._make_repo()
        self.fake_takt = self._make_fake_takt()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_success_writes_state_summary_trace_and_sqlite(self) -> None:
        result = self._aw(
            "run",
            "--repo",
            str(self.repo),
            "--task-text",
            "Implement the fixture task.",
            "--verify-command",
            "test -f implemented.txt",
            "--takt-bin",
            str(self.fake_takt),
        )

        summary = Path(result.stdout.strip())
        state = self._state(summary)
        self.assertEqual("succeeded", state["status"])
        self.assertTrue(summary.exists())
        self.assertIn("status: `succeeded`", summary.read_text())
        trace_rows = [json.loads(line) for line in Path(state["trace_path"]).read_text().splitlines()]
        self.assertEqual(["OK"] * 5, [row["status"]["code"] for row in trace_rows])

        conn = sqlite3.connect(self.state_dir / "jobs.sqlite")
        row = conn.execute("select status, summary_path from jobs where run_id = ?", (state["run_id"],)).fetchone()
        self.assertEqual(("succeeded", str(summary)), row)

        self._aw("cleanup", "--run-id", state["run_id"])
        cleaned_state = self._state(summary)
        self.assertIsNone(cleaned_state["worktree_path"])
        self.assertIn("worktree: ``", summary.read_text())

    def test_resume_continues_from_failed_qc_step(self) -> None:
        first = self._aw(
            "run",
            "--repo",
            str(self.repo),
            "--task-text",
            "Implement but wait for a human-created QC marker.",
            "--verify-command",
            "test -f qc-pass",
            "--takt-bin",
            str(self.fake_takt),
            check=False,
        )
        self.assertEqual(1, first.returncode)
        state = self._state(Path(first.stdout.strip()))
        self.assertEqual("qc_failed", state["status"])
        Path(state["worktree_path"], "qc-pass").write_text("ok\n")

        resumed = self._aw("resume", "--run-id", state["run_id"])
        resumed_state = self._state(Path(resumed.stdout.strip()))
        attempts = {step["name"]: step["attempts"] for step in resumed_state["steps"]}
        self.assertEqual("succeeded", resumed_state["status"])
        self.assertEqual(1, attempts["run_takt"])
        self.assertEqual(2, attempts["run_qc"])

    def test_timeout_marks_takt_step_and_trace_error(self) -> None:
        result = self._aw(
            "run",
            "--repo",
            str(self.repo),
            "--task-text",
            "This task intentionally sleeps.",
            "--verify-command",
            "test -f implemented.txt",
            "--takt-bin",
            str(self.fake_takt),
            "--timeout-seconds",
            "0.2",
            env={"FAKE_TAKT_SLEEP": "2"},
            check=False,
        )

        self.assertEqual(1, result.returncode)
        state = self._state(Path(result.stdout.strip()))
        self.assertEqual("timed_out", state["status"])
        run_takt = next(step for step in state["steps"] if step["name"] == "run_takt")
        self.assertTrue(run_takt["timed_out"])
        trace_rows = [json.loads(line) for line in Path(state["trace_path"]).read_text().splitlines()]
        self.assertIn("ERROR", [row["status"]["code"] for row in trace_rows])

    def _aw(self, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        result = subprocess.run(
            [str(AW), "--state-dir", str(self.state_dir), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
        )
        if check and result.returncode != 0:
            self.fail(f"aw failed with {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}")
        return result

    def _make_repo(self) -> Path:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "config", "user.name", "agent-workflow-test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "agent-workflow-test@example.invalid"], cwd=repo, check=True)
        (repo / ".takt").mkdir()
        (repo / ".takt" / "README.md").write_text("fixture\n")
        (repo / "README.md").write_text("fixture\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
        return repo

    def _make_fake_takt(self) -> Path:
        path = self.root / "fake-takt"
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$@\" > takt.args\n"
            "if [[ \"${FAKE_TAKT_SLEEP:-}\" != \"\" ]]; then\n"
            "  sleep \"$FAKE_TAKT_SLEEP\"\n"
            "fi\n"
            "if [[ \"${FAKE_TAKT_EXIT:-0}\" != \"0\" ]]; then\n"
            "  exit \"$FAKE_TAKT_EXIT\"\n"
            "fi\n"
            "echo ok > implemented.txt\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def _state(self, summary: Path) -> dict[str, object]:
        return json.loads((summary.parent / "state.json").read_text())


if __name__ == "__main__":
    unittest.main()
