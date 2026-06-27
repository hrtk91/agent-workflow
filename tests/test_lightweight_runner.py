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
            "--executor-bin",
            str(self.fake_takt),
        )

        summary = Path(result.stdout.strip())
        state = self._state(summary)
        self.assertEqual("succeeded", state["status"])
        self.assertTrue(summary.exists())
        summary_text = summary.read_text()
        self.assertIn("status: `succeeded`", summary_text)
        discord_summary = summary.with_name("hermes-discord-summary.md")
        discord_text = discord_summary.read_text()
        self.assertIn("✅ workflow succeeded", discord_text)
        self.assertIn("No retry needed.", discord_text)
        self.assertIn("## executor observability", summary_text)
        self.assertIn("- takt_trace: `", summary_text)
        self.assertIn("- takt_monitor: `", summary_text)
        self.assertIn("- takt_workflow_status: `succeeded`", summary_text)
        self.assertIn("- takt_workflow_duration_ms: `1000`", summary_text)
        self.assertIn("- takt_step_duration: `implement` status=`done` duration_ms=`1000`", summary_text)
        self.assertIn("- timeout_seconds: `7200`", summary_text)
        self.assertIn("duration_seconds=", summary_text)
        self.assertIn("- takt_session_shadow: `", summary_text)
        self.assertIn("- takt_phase_usage: `", summary_text)
        trace_rows = [json.loads(line) for line in Path(state["trace_path"]).read_text().splitlines()]
        self.assertEqual(["OK"] * 5, [row["status"]["code"] for row in trace_rows])

        conn = sqlite3.connect(self.state_dir / "jobs.sqlite")
        row = conn.execute("select status, summary_path from jobs where run_id = ?", (state["run_id"],)).fetchone()
        self.assertEqual(("succeeded", str(summary)), row)

        self._aw("cleanup", "--run-id", state["run_id"])
        cleaned_state = self._state(summary)
        self.assertIsNone(cleaned_state["worktree_path"])
        cleaned_summary_text = summary.read_text()
        self.assertIn("worktree: ``", cleaned_summary_text)
        self.assertIn("## executor observability", cleaned_summary_text)

    def test_resume_continues_from_failed_qc_step(self) -> None:
        first = self._aw(
            "run",
            "--repo",
            str(self.repo),
            "--task-text",
            "Implement but wait for a human-created QC marker.",
            "--verify-command",
            "test -f qc-pass",
            "--executor-bin",
            str(self.fake_takt),
            check=False,
        )
        self.assertEqual(1, first.returncode)
        state = self._state(Path(first.stdout.strip()))
        self.assertEqual("qc_failed", state["status"])
        discord_text = Path(state["summary_path"]).with_name("hermes-discord-summary.md").read_text()
        self.assertIn("🧪 workflow QC failed", discord_text)
        self.assertIn(f"aw resume --run-id {state['run_id']}", discord_text)
        self.assertIn(f"aw retry --run-id {state['run_id']} --step run_qc", discord_text)
        Path(state["worktree_path"], "qc-pass").write_text("ok\n")

        resumed = self._aw("resume", "--run-id", state["run_id"])
        resumed_state = self._state(Path(resumed.stdout.strip()))
        attempts = {step["name"]: step["attempts"] for step in resumed_state["steps"]}
        self.assertEqual("succeeded", resumed_state["status"])
        self.assertEqual(1, attempts["run_executor"])
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
            "--executor-bin",
            str(self.fake_takt),
            "--timeout-seconds",
            "0.2",
            env={"FAKE_TAKT_SLEEP": "2"},
            check=False,
        )

        self.assertEqual(1, result.returncode)
        state = self._state(Path(result.stdout.strip()))
        self.assertEqual("timed_out", state["status"])
        summary_text = Path(state["summary_path"]).read_text()
        discord_text = Path(state["summary_path"]).with_name("hermes-discord-summary.md").read_text()
        self.assertIn("- timeout_seconds: `0.2`", summary_text)
        self.assertIn("timed_out=true", summary_text)
        self.assertIn("⏳ workflow timed out", discord_text)
        self.assertIn("--timeout-seconds 600", discord_text)
        run_executor = next(step for step in state["steps"] if step["name"] == "run_executor")
        self.assertTrue(run_executor["timed_out"])
        trace_rows = [json.loads(line) for line in Path(state["trace_path"]).read_text().splitlines()]
        self.assertIn("ERROR", [row["status"]["code"] for row in trace_rows])

    def test_enqueue_then_tick_runs_job(self) -> None:
        enqueued = self._aw(
            "enqueue",
            "--repo",
            str(self.repo),
            "--task-text",
            "Queue this fixture task.",
            "--verify-command",
            "test -f implemented.txt",
            "--executor-bin",
            str(self.fake_takt),
        )
        job_id = enqueued.stdout.strip()
        self.assertRegex(job_id, r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")

        status_before = self._aw("status")
        self.assertIn(f"job\t{job_id}\tqueued", status_before.stdout)

        ticked = self._aw("tick", "--max-runs", "1")
        self.assertIn(f"{job_id}\tsucceeded", ticked.stdout)

        status_after = self._aw("status")
        self.assertIn(f"job\t{job_id}\tsucceeded", status_after.stdout)

    def test_tick_can_notify_failed_runs_with_discord_summary(self) -> None:
        enqueued = self._aw(
            "enqueue",
            "--repo",
            str(self.repo),
            "--task-text",
            "Queue this fixture task and fail QC.",
            "--verify-command",
            "test -f qc-pass",
            "--executor-bin",
            str(self.fake_takt),
        )
        job_id = enqueued.stdout.strip()
        notify_log = self.root / "notify.log"
        notify_script = self.root / "notify"
        notify_script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$@\" > \"$FAKE_NOTIFY_LOG\"\n",
            encoding="utf-8",
        )
        notify_script.chmod(0o755)

        ticked = self._aw(
            "tick",
            "--max-runs",
            "1",
            "--notify-command",
            f"{notify_script} {{status}} {{run_id}} {{discord_summary}}",
            env={"FAKE_NOTIFY_LOG": str(notify_log)},
            check=False,
        )

        self.assertEqual(1, ticked.returncode)
        self.assertIn(f"{job_id}\tqc_failed", ticked.stdout)
        notify_args = notify_log.read_text().splitlines()
        self.assertEqual("qc_failed", notify_args[0])
        self.assertRegex(notify_args[1], r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
        discord_summary = Path(notify_args[2])
        self.assertTrue(discord_summary.exists())
        self.assertIn("🔁 retry candidate", discord_summary.read_text())

    def test_tick_can_isolate_job_failures_for_cron_dispatch(self) -> None:
        enqueued = self._aw(
            "enqueue",
            "--repo",
            str(self.repo),
            "--task-text",
            "Queue this fixture task and fail QC without failing the dispatcher.",
            "--verify-command",
            "test -f qc-pass",
            "--executor-bin",
            str(self.fake_takt),
        )
        job_id = enqueued.stdout.strip()

        ticked = self._aw(
            "tick",
            "--max-runs",
            "1",
            "--isolate-job-failures",
        )

        self.assertEqual(0, ticked.returncode)
        self.assertIn(f"{job_id}\tqc_failed", ticked.stdout)

        status_after = self._aw("status")
        self.assertIn(f"job\t{job_id}\tqc_failed", status_after.stdout)

    def test_auto_repair_enqueue_after_failed_tick(self) -> None:
        enqueued = self._aw(
            "enqueue",
            "--repo",
            str(self.repo),
            "--task-text",
            "Queue this fixture task and auto-enqueue repair when QC fails.",
            "--verify-command",
            "test -f qc-pass",
            "--executor-bin",
            str(self.fake_takt),
        )
        job_id = enqueued.stdout.strip()

        ticked = self._aw(
            "tick",
            "--max-runs",
            "1",
            "--auto-repair",
            "--repair-executor-bin",
            str(self.fake_takt),
            "--isolate-job-failures",
        )

        self.assertEqual(0, ticked.returncode)
        self.assertIn(f"{job_id}\tqc_failed", ticked.stdout)
        fields = ticked.stdout.strip().split("\t")
        repair_job_id = fields[6]
        self.assertRegex(repair_job_id, r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")

        original_state = self._state(Path(fields[3]))
        marker = Path(original_state["run_dir"]) / "auto-repair-enqueued.json"
        self.assertEqual(repair_job_id, json.loads(marker.read_text())["repair_job_id"])
        discord_text = Path(original_state["summary_path"]).with_name("hermes-discord-summary.md").read_text()
        self.assertIn("🩺 auto repair queued", discord_text)

        conn = sqlite3.connect(self.state_dir / "jobs.sqlite")
        row = conn.execute("select status, config_json from queue where job_id = ?", (repair_job_id,)).fetchone()
        self.assertEqual("queued", row[0])
        repair_config = json.loads(row[1])
        self.assertEqual("repair", repair_config["purpose"])
        self.assertEqual(original_state["run_id"], repair_config["repair_for_run_id"])
        self.assertIn(str(original_state["summary_path"]), repair_config["task_text"])

    def test_auto_repair_does_not_recurse_when_repair_job_fails(self) -> None:
        enqueued = self._aw(
            "enqueue",
            "--repo",
            str(self.repo),
            "--task-text",
            "Queue this fixture task and create one repair job.",
            "--verify-command",
            "test -f qc-pass",
            "--executor-bin",
            str(self.fake_takt),
        )
        job_id = enqueued.stdout.strip()

        first = self._aw(
            "tick",
            "--max-runs",
            "1",
            "--auto-repair",
            "--repair-executor-bin",
            str(self.fake_takt),
            "--isolate-job-failures",
        )
        self.assertIn(f"{job_id}\tqc_failed", first.stdout)
        repair_job_id = first.stdout.strip().split("\t")[6]

        second = self._aw(
            "tick",
            "--max-runs",
            "1",
            "--auto-repair",
            "--repair-executor-bin",
            str(self.fake_takt),
            "--isolate-job-failures",
        )
        self.assertIn(f"{repair_job_id}\tqc_failed", second.stdout)

        conn = sqlite3.connect(self.state_dir / "jobs.sqlite")
        queue_rows = conn.execute("select job_id, status, config_json from queue order by created_at").fetchall()
        repair_jobs = [
            row
            for row in queue_rows
            if json.loads(row[2]).get("purpose") == "repair"
        ]
        self.assertEqual(1, len(repair_jobs))
        self.assertEqual(repair_job_id, repair_jobs[0][0])
        self.assertEqual("qc_failed", repair_jobs[0][1])

    def test_tick_auto_repair_scans_existing_failed_runs(self) -> None:
        failed = self._aw(
            "run",
            "--repo",
            str(self.repo),
            "--task-text",
            "Create an existing failed run before auto-repair is enabled.",
            "--verify-command",
            "test -f qc-pass",
            "--executor-bin",
            str(self.fake_takt),
            check=False,
        )
        failed_state = self._state(Path(failed.stdout.strip()))
        self.assertEqual("qc_failed", failed_state["status"])

        ticked = self._aw(
            "tick",
            "--max-runs",
            "1",
            "--auto-repair",
            "--repair-executor-bin",
            str(self.fake_takt),
            "--isolate-job-failures",
        )

        self.assertEqual(0, ticked.returncode)
        conn = sqlite3.connect(self.state_dir / "jobs.sqlite")
        queue_rows = conn.execute("select status, config_json from queue order by created_at").fetchall()
        repair_jobs = [
            row
            for row in queue_rows
            if json.loads(row[1]).get("repair_for_run_id") == failed_state["run_id"]
        ]
        self.assertEqual(1, len(repair_jobs))
        self.assertEqual("qc_failed", repair_jobs[0][0])

    def test_tick_auto_repair_skips_failed_repair_job_and_scans_next_failure(self) -> None:
        first = self._aw(
            "run",
            "--repo",
            str(self.repo),
            "--task-text",
            "Create the first existing failed run.",
            "--verify-command",
            "test -f qc-pass",
            "--executor-bin",
            str(self.fake_takt),
            check=False,
        )
        first_state = self._state(Path(first.stdout.strip()))

        second = self._aw(
            "run",
            "--repo",
            str(self.repo),
            "--task-text",
            "Create a later normal failed run that must not be starved by the failed repair job.",
            "--verify-command",
            "test -f qc-pass",
            "--executor-bin",
            str(self.fake_takt),
            check=False,
        )
        second_state = self._state(Path(second.stdout.strip()))

        self._aw(
            "tick",
            "--max-runs",
            "1",
            "--auto-repair",
            "--repair-executor-bin",
            str(self.fake_takt),
            "--isolate-job-failures",
        )

        ticked = self._aw(
            "tick",
            "--max-runs",
            "1",
            "--auto-repair",
            "--repair-executor-bin",
            str(self.fake_takt),
            "--isolate-job-failures",
        )
        self.assertEqual(0, ticked.returncode)

        conn = sqlite3.connect(self.state_dir / "jobs.sqlite")
        queue_rows = conn.execute("select config_json from queue order by created_at").fetchall()
        repair_targets = [
            json.loads(row[0]).get("repair_for_run_id")
            for row in queue_rows
            if json.loads(row[0]).get("purpose") == "repair"
        ]
        self.assertIn(first_state["run_id"], repair_targets)
        self.assertIn(second_state["run_id"], repair_targets)

    def test_worker_runs_queued_job_in_child_process(self) -> None:
        enqueued = self._aw(
            "enqueue",
            "--repo",
            str(self.repo),
            "--task-text",
            "Queue this fixture task for the child worker.",
            "--verify-command",
            "test -f implemented.txt",
            "--executor-bin",
            str(self.fake_takt),
        )
        job_id = enqueued.stdout.strip()

        worker = self._aw(
            "worker",
            "--interval-seconds",
            "0.01",
            "--max-runs-per-tick",
            "1",
            "--parallelism",
            "1",
            "--stop-when-idle",
        )

        self.assertIn(f"started\t{job_id}\tpid=", worker.stdout)
        status_after = self._aw("status")
        self.assertIn(f"job\t{job_id}\tsucceeded", status_after.stdout)

    def test_worker_child_enqueues_auto_repair(self) -> None:
        enqueued = self._aw(
            "enqueue",
            "--repo",
            str(self.repo),
            "--task-text",
            "Queue this fixture task for child auto-repair.",
            "--verify-command",
            "test -f qc-pass",
            "--executor-bin",
            str(self.fake_takt),
        )
        job_id = enqueued.stdout.strip()

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
            str(self.fake_takt),
            "--stop-when-idle",
        )

        self.assertIn(f"started\t{job_id}\tpid=", worker.stdout)
        conn = sqlite3.connect(self.state_dir / "jobs.sqlite")
        rows = conn.execute("select job_id, status, config_json, run_id from queue order by created_at").fetchall()
        self.assertEqual("qc_failed", rows[0][1])
        repair_rows = [row for row in rows if json.loads(row[2]).get("purpose") == "repair"]
        self.assertEqual(1, len(repair_rows))
        self.assertEqual("qc_failed", repair_rows[0][1])
        self.assertNotEqual(job_id, repair_rows[0][0])

    def test_worker_recovers_stale_running_queue_job_on_startup(self) -> None:
        enqueued = self._aw(
            "enqueue",
            "--repo",
            str(self.repo),
            "--task-text",
            "Queue this fixture task but leave it stale.",
            "--verify-command",
            "test -f implemented.txt",
            "--executor-bin",
            str(self.fake_takt),
        )
        job_id = enqueued.stdout.strip()
        conn = sqlite3.connect(self.state_dir / "jobs.sqlite")
        conn.execute("update queue set status = 'running' where job_id = ?", (job_id,))
        conn.commit()
        conn.close()

        worker = self._aw(
            "worker",
            "--interval-seconds",
            "0.01",
            "--stop-when-idle",
        )

        self.assertIn("recovered_stale_running\t1", worker.stdout)
        status_after = self._aw("status")
        self.assertIn(f"job\t{job_id}\tfailed", status_after.stdout)

    def test_watchdog_scan_and_repair_draft_validate_failed_run(self) -> None:
        failed = self._aw(
            "run",
            "--repo",
            str(self.repo),
            "--task-text",
            "Implement but fail QC for repair triage.",
            "--verify-command",
            "test -f qc-pass",
            "--executor-bin",
            str(self.fake_takt),
            check=False,
        )
        state = self._state(Path(failed.stdout.strip()))
        self.assertEqual("qc_failed", state["status"])

        scan = self._aw("watchdog", "scan")
        self.assertIn(f"{state['run_id']}\tqc_failed\trun_qc", scan.stdout)

        diagnosis = self.root / "diagnosis.md"
        evidence = self.root / "evidence.md"
        notify_before = self.root / "notify-before.md"
        diagnosis.write_text("QC expects qc-pass, but the worktree lacks the marker.\n", encoding="utf-8")
        evidence.write_text(f"- summary: {state['summary_path']}\n", encoding="utf-8")
        notify_before.write_text("🩺 repair draft: retry after adding the missing marker.\n", encoding="utf-8")

        drafted = self._aw(
            "repair",
            "draft",
            "--failed-run-id",
            str(state["run_id"]),
            "--title",
            "QC marker missing",
            "--category",
            "implementation_failure",
            "--risk",
            "low",
            "--proposed-action",
            "repo_config_patch",
            "--diagnosis-file",
            str(diagnosis),
            "--evidence-file",
            str(evidence),
            "--notify-before-file",
            str(notify_before),
            "--verify-command",
            "test -f qc-pass",
        )
        draft_dir = Path(drafted.stdout.strip())
        repair_ini = draft_dir / "repair.ini"
        self.assertTrue(repair_ini.exists())
        self.assertEqual("QC expects qc-pass, but the worktree lacks the marker.\n", (draft_dir / "diagnosis.md").read_text())
        self.assertIn("status = validated", repair_ini.read_text())
        self.assertIn("proposed_action = repo_config_patch", repair_ini.read_text())

        validated = self._aw("repair", "validate", "--draft-id", draft_dir.name)
        self.assertEqual(str(draft_dir), validated.stdout.strip())

        duplicate = self._aw(
            "repair",
            "draft",
            "--failed-run-id",
            str(state["run_id"]),
            "--title",
            "QC marker missing again",
            "--category",
            "implementation_failure",
            "--risk",
            "low",
            "--proposed-action",
            "repo_config_patch",
            "--diagnosis-file",
            str(diagnosis),
            "--evidence-file",
            str(evidence),
            "--notify-before-file",
            str(notify_before),
            "--verify-command",
            "test -f qc-pass",
            check=False,
        )
        self.assertEqual(2, duplicate.returncode)
        self.assertIn("repair draft already exists", duplicate.stderr)

        scan_after = self._aw("watchdog", "scan")
        self.assertEqual("", scan_after.stdout)

    def test_repair_draft_requires_deploy_guardrails(self) -> None:
        failed = self._aw(
            "run",
            "--repo",
            str(self.repo),
            "--task-text",
            "Implement but fail QC before deploy repair draft.",
            "--verify-command",
            "test -f qc-pass",
            "--executor-bin",
            str(self.fake_takt),
            check=False,
        )
        state = self._state(Path(failed.stdout.strip()))
        diagnosis = self.root / "deploy-diagnosis.md"
        evidence = self.root / "deploy-evidence.md"
        notify_before = self.root / "deploy-notify-before.md"
        rollback = self.root / "rollback.md"
        diagnosis.write_text("Production deploy failed after config drift.\n", encoding="utf-8")
        evidence.write_text("deploy log excerpt\n", encoding="utf-8")
        notify_before.write_text("🚦 repair draft: deploy needs healthcheck and rollback.\n", encoding="utf-8")
        rollback.write_text("Revert to previous deployment and re-run healthcheck.\n", encoding="utf-8")

        missing_guardrails = self._aw(
            "repair",
            "draft",
            "--failed-run-id",
            str(state["run_id"]),
            "--title",
            "Deploy config drift",
            "--category",
            "deploy_config",
            "--risk",
            "medium",
            "--proposed-action",
            "redeploy_and_healthcheck",
            "--diagnosis-file",
            str(diagnosis),
            "--evidence-file",
            str(evidence),
            "--notify-before-file",
            str(notify_before),
            check=False,
        )
        self.assertEqual(2, missing_guardrails.returncode)
        self.assertIn("requires --risk high", missing_guardrails.stderr)

        drafted = self._aw(
            "repair",
            "draft",
            "--failed-run-id",
            str(state["run_id"]),
            "--title",
            "Deploy config drift",
            "--category",
            "deploy_config",
            "--risk",
            "high",
            "--proposed-action",
            "redeploy_and_healthcheck",
            "--diagnosis-file",
            str(diagnosis),
            "--evidence-file",
            str(evidence),
            "--notify-before-file",
            str(notify_before),
            "--environment",
            "production",
            "--healthcheck-command",
            "curl -fsS https://example.invalid/health",
            "--rollback-plan-file",
            str(rollback),
        )
        draft_dir = Path(drafted.stdout.strip())
        self.assertTrue((draft_dir / "rollback-plan.md").exists())
        self.assertIn("environment = production", (draft_dir / "repair.ini").read_text())

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
            "mkdir -p .takt/runs/fake-run/logs\n"
            "cat > .takt/runs/fake-run/trace.md <<'TRACE'\n"
            "# Execution Trace: default\n"
            "- Started: 2026-01-01T00:00:00.000Z\n"
            "- Ended: 2026-01-01T00:00:01.000Z\n"
            "- Status: succeeded\n"
            "- Iterations: 1\n"
            "- Reason: complete\n"
            "TRACE\n"
            "cat > .takt/runs/fake-run/monitor.json <<'MONITOR'\n"
            "{\n"
            "  \"schemaVersion\": 1,\n"
            "  \"scopeMetrics\": [\n"
            "    {\n"
            "      \"metrics\": [\n"
            "        {\n"
            "          \"name\": \"takt.workflow.runs\",\n"
            "          \"points\": [\n"
            "            {\"attributes\": {\"takt.workflow.status\": \"succeeded\"}, \"value\": 1}\n"
            "          ]\n"
            "        },\n"
            "        {\n"
            "          \"name\": \"takt.workflow.duration\",\n"
            "          \"points\": [\n"
            "            {\"value\": {\"sum\": 1000}}\n"
            "          ]\n"
            "        },\n"
            "        {\n"
            "          \"name\": \"takt.workflow.step.duration\",\n"
            "          \"points\": [\n"
            "            {\"attributes\": {\"takt.step.name\": \"implement\", \"takt.step.status\": \"done\"}, \"value\": {\"sum\": 1000}}\n"
            "          ]\n"
            "        },\n"
            "        {\n"
            "          \"name\": \"takt.workflow.phase.duration\",\n"
            "          \"points\": [\n"
            "            {\"attributes\": {\"takt.step.name\": \"implement\", \"takt.phase.name\": \"execute\", \"takt.phase.status\": \"done\"}, \"value\": {\"sum\": 900}}\n"
            "          ]\n"
            "        }\n"
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n"
            "MONITOR\n"
            "printf '{\"type\":\"workflow_start\"}\\n' > .takt/runs/fake-run/logs/session-otel-session-shadow.jsonl\n"
            "printf '{\"type\":\"phase_usage\"}\\n' > .takt/runs/fake-run/logs/session-usage-events.phase.jsonl\n"
            "echo ok > implemented.txt\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def _state(self, summary: Path) -> dict[str, object]:
        return json.loads((summary.parent / "state.json").read_text())


if __name__ == "__main__":
    unittest.main()
