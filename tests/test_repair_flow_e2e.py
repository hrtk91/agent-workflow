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


class RepairFlowE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.repo = self._make_repo()
        self.fake_executor = self._make_fake_executor()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_failure_diagnosis_draft_action_and_qc_green(self) -> None:
        original_job = self._aw(
            "enqueue",
            "--repo",
            str(self.repo),
            "--task-text",
            "Original task intentionally fails before repair flow.",
            "--verify-command",
            "test -f original-pass",
            "--executor-bin",
            str(self.fake_executor),
        ).stdout.strip()

        worker = self._aw(
            "worker",
            "--interval-seconds",
            "0.01",
            "--max-runs-per-tick",
            "1",
            "--parallelism",
            "1",
            "--auto-repair",
            "--repair-executor-bin",
            str(self.fake_executor),
            "--stop-when-idle",
        )

        self.assertIn(f"started\t{original_job}\tpid=", worker.stdout)
        rows = self._queue_rows()
        purposes = [json.loads(row["config_json"]).get("purpose") for row in rows]
        self.assertIn("repair", purposes)
        self.assertIn("repair_action", purposes)

        by_purpose = {json.loads(row["config_json"]).get("purpose"): row for row in rows}
        self.assertEqual("qc_failed", by_purpose["workflow"]["status"])
        self.assertEqual("succeeded", by_purpose["repair"]["status"])
        self.assertEqual("succeeded", by_purpose["repair_action"]["status"])

        action_config = json.loads(by_purpose["repair_action"]["config_json"])
        self.assertIn("Execute the validated repair action", action_config["task_text"])
        self.assertEqual(by_purpose["workflow"]["run_id"], action_config["repair_for_run_id"])

    def test_diagnosis_failure_does_not_enqueue_repair_action(self) -> None:
        original_job = self._aw(
            "enqueue",
            "--repo",
            str(self.repo),
            "--task-text",
            "Original task intentionally fails before diagnosis failure.",
            "--verify-command",
            "test -f original-pass",
            "--executor-bin",
            str(self.fake_executor),
        ).stdout.strip()

        worker = self._aw(
            "worker",
            "--interval-seconds",
            "0.01",
            "--max-runs-per-tick",
            "1",
            "--parallelism",
            "1",
            "--auto-repair",
            "--repair-executor-bin",
            str(self.fake_executor),
            "--stop-when-idle",
            env={"FAKE_DIAGNOSIS_MODE": "fail"},
        )

        self.assertIn(f"started\t{original_job}\tpid=", worker.stdout)
        rows = self._queue_rows()
        purposes = [json.loads(row["config_json"]).get("purpose") for row in rows]
        self.assertIn("repair", purposes)
        self.assertNotIn("repair_action", purposes)
        by_purpose = {json.loads(row["config_json"]).get("purpose"): row for row in rows}
        self.assertEqual("qc_failed", by_purpose["workflow"]["status"])
        self.assertEqual("failed", by_purpose["repair"]["status"])

    def test_human_needed_diagnosis_does_not_enqueue_repair_action(self) -> None:
        original_job = self._aw(
            "enqueue",
            "--repo",
            str(self.repo),
            "--task-text",
            "Original task intentionally fails before human handoff.",
            "--verify-command",
            "test -f original-pass",
            "--executor-bin",
            str(self.fake_executor),
        ).stdout.strip()

        worker = self._aw(
            "worker",
            "--interval-seconds",
            "0.01",
            "--max-runs-per-tick",
            "1",
            "--parallelism",
            "1",
            "--auto-repair",
            "--repair-executor-bin",
            str(self.fake_executor),
            "--stop-when-idle",
            env={"FAKE_DIAGNOSIS_MODE": "human_needed"},
        )

        self.assertIn(f"started\t{original_job}\tpid=", worker.stdout)
        rows = self._queue_rows()
        purposes = [json.loads(row["config_json"]).get("purpose") for row in rows]
        self.assertIn("repair", purposes)
        self.assertNotIn("repair_action", purposes)
        by_purpose = {json.loads(row["config_json"]).get("purpose"): row for row in rows}
        self.assertEqual("succeeded", by_purpose["repair"]["status"])

    def test_repair_action_qc_failure_stays_visible(self) -> None:
        original_job = self._aw(
            "enqueue",
            "--repo",
            str(self.repo),
            "--task-text",
            "Original task intentionally fails before action failure.",
            "--verify-command",
            "test -f original-pass",
            "--executor-bin",
            str(self.fake_executor),
        ).stdout.strip()

        worker = self._aw(
            "worker",
            "--interval-seconds",
            "0.01",
            "--max-runs-per-tick",
            "1",
            "--parallelism",
            "1",
            "--auto-repair",
            "--repair-executor-bin",
            str(self.fake_executor),
            "--stop-when-idle",
            env={"FAKE_ACTION_MODE": "fail"},
        )

        self.assertIn(f"started\t{original_job}\tpid=", worker.stdout)
        rows = self._queue_rows()
        by_purpose = {json.loads(row["config_json"]).get("purpose"): row for row in rows}
        self.assertEqual("succeeded", by_purpose["repair"]["status"])
        self.assertEqual("qc_failed", by_purpose["repair_action"]["status"])

    def test_qc_repair_loop_can_complete_without_diagnosis(self) -> None:
        job = self._aw(
            "enqueue",
            "--repo",
            str(self.repo),
            "--task-text",
            "Original task becomes green during the QC repair loop.",
            "--verify-command",
            "test -f original-pass",
            "--executor-bin",
            str(self.fake_executor),
        ).stdout.strip()

        worker = self._aw(
            "worker",
            "--interval-seconds",
            "0.01",
            "--max-runs-per-tick",
            "1",
            "--parallelism",
            "1",
            "--auto-repair",
            "--repair-executor-bin",
            str(self.fake_executor),
            "--stop-when-idle",
            env={"FAKE_ORIGINAL_PASS_ON_ATTEMPT": "3"},
        )

        self.assertIn(f"started\t{job}\tpid=", worker.stdout)
        rows = self._queue_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("succeeded", rows[0]["status"])

    def _aw(self, *args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        merged_env["AGENT_WORKFLOW_STATE_DIR"] = str(self.state_dir)
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

    def _queue_rows(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.state_dir / "jobs.sqlite")
        conn.row_factory = sqlite3.Row
        return list(conn.execute("select job_id, status, run_id, config_json from queue order by created_at"))

    def _make_repo(self) -> Path:
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "config", "user.name", "agent-workflow-e2e"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "agent-workflow-e2e@example.invalid"], cwd=repo, check=True)
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
        return repo

    def _make_fake_executor(self) -> Path:
        path = self.root / "fake-executor"
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "task=''\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = \"--task\" ]; then\n"
            "    shift\n"
            "    task=\"${1:-}\"\n"
            "  fi\n"
            "  shift || true\n"
            "done\n"
            "if [[ \"$task\" == *\"Diagnose and draft a repair\"* ]]; then\n"
            "  if [[ \"${FAKE_DIAGNOSIS_MODE:-}\" == \"fail\" ]]; then\n"
            "    exit 3\n"
            "  fi\n"
            "  failed_run_id=\"$(printf '%s\\n' \"$task\" | sed -n 's/^- run_id: \\([^ ]*\\)$/\\1/p' | head -1)\"\n"
            "  printf 'diagnose %s\\n' \"$failed_run_id\" > diagnosis.md\n"
            "  printf -- '- failed_run_id: %s\\n' \"$failed_run_id\" > evidence.md\n"
            "  printf 'repair action will be queued\\n' > notify-before.md\n"
            "  proposed_action=repo_config_patch\n"
            "  verify_args=(--verify-command 'test -f repaired.txt')\n"
            "  if [[ \"${FAKE_DIAGNOSIS_MODE:-}\" == \"human_needed\" ]]; then\n"
            "    proposed_action=human_needed\n"
            "    verify_args=()\n"
            "  fi\n"
            "  python3 -m agent_workflow --state-dir \"$AGENT_WORKFLOW_STATE_DIR\" repair draft \\\n"
            "    --failed-run-id \"$failed_run_id\" \\\n"
            "    --title 'Dummy repo config repair' \\\n"
            "    --category repo_config \\\n"
            "    --risk low \\\n"
            "    --proposed-action \"$proposed_action\" \\\n"
            "    --diagnosis-file diagnosis.md \\\n"
            "    --evidence-file evidence.md \\\n"
            "    --notify-before-file notify-before.md \\\n"
            "    \"${verify_args[@]}\"\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$task\" == *\"Execute the validated repair action\"* ]]; then\n"
            "  if [[ \"${FAKE_ACTION_MODE:-}\" == \"fail\" ]]; then\n"
            "    touch attempted-repair-action.txt\n"
            "    exit 0\n"
            "  fi\n"
            "  touch repaired.txt\n"
            "  exit 0\n"
            "fi\n"
            "count_file=.fake-original-attempt\n"
            "attempt=1\n"
            "if [[ -f \"$count_file\" ]]; then\n"
            "  attempt=$(( $(cat \"$count_file\") + 1 ))\n"
            "fi\n"
            "printf '%s\\n' \"$attempt\" > \"$count_file\"\n"
            "if [[ \"${FAKE_ORIGINAL_PASS_ON_ATTEMPT:-}\" != \"\" && \"$attempt\" -ge \"$FAKE_ORIGINAL_PASS_ON_ATTEMPT\" ]]; then\n"
            "  touch original-pass\n"
            "fi\n"
            "touch attempted-original.txt\n"
            "exit 0\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path


if __name__ == "__main__":
    unittest.main()
