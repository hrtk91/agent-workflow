from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from agent_workflow.merge import MergeBlocked, MergeGateConfig, run_merge_approved, run_merge_gate
from agent_workflow.repair import REPAIR_ACTIONS, REPAIR_CATEGORIES, REPAIR_RISKS, RepairDraftInput, RepairManager
from agent_workflow.runner import (
    AUTO_REPAIR_SCAN_EXISTING_MAX_AGE_SECONDS,
    AutoRepairConfig,
    FAILURE_NOTIFY_STATUSES,
    RunnerConfig,
    WorkflowRunner,
    default_state_dir,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aw", description="Lightweight resumable agent workflow runner")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start a new task run")
    add_run_args(run)

    enqueue = sub.add_parser("enqueue", help="queue a task run and return immediately")
    add_run_args(enqueue)

    tick = sub.add_parser("tick", help="run queued jobs once and exit")
    tick.add_argument("--max-runs", type=int, default=1)
    tick.add_argument(
        "--isolate-job-failures",
        action="store_true",
        help="exit successfully when queued jobs fail but aw records the failure; notification or aw infrastructure errors still fail",
    )
    add_notify_args(tick)
    add_auto_repair_args(tick)

    worker = sub.add_parser("worker", help="run queued jobs in a loop")
    worker.add_argument("--interval-seconds", type=float, default=60)
    worker.add_argument("--max-runs-per-tick", type=int, default=1)
    worker.add_argument("--parallelism", type=int, default=1, help="maximum claimed jobs to run concurrently")
    worker.add_argument("--repo-parallelism", type=int, default=1, help="maximum concurrent jobs per repo path; 0 disables this limit")
    worker.add_argument("--inline", action="store_true", help="run jobs inside the worker process instead of child processes")
    worker.add_argument("--no-recover-stale-running", action="store_true", help="do not mark pre-existing running jobs as failed on worker startup")
    worker.add_argument("--stop-when-idle", action="store_true", help=argparse.SUPPRESS)
    add_notify_args(worker)
    add_auto_repair_args(worker)

    run_claimed = sub.add_parser("run-claimed", help=argparse.SUPPRESS)
    run_claimed.add_argument("--job-id", required=True)
    add_notify_args(run_claimed)
    add_auto_repair_args(run_claimed)

    resume = sub.add_parser("resume", help="resume a failed or interrupted run")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--verify-command")
    resume.add_argument("--timeout-seconds", type=float)
    add_notify_args(resume)

    retry = sub.add_parser("retry", help="retry one step and all downstream steps")
    retry.add_argument("--run-id", required=True)
    retry.add_argument("--step", required=True)
    retry.add_argument("--verify-command")
    retry.add_argument("--timeout-seconds", type=float)
    add_notify_args(retry)

    status = sub.add_parser("status", help="show run status")
    status.add_argument("--run-id")
    status.add_argument("--include-repair", action="store_true", help="include internal repair-diagnosis jobs in recent status output")

    summary = sub.add_parser("summary", help="print summary path")
    summary.add_argument("--run-id", required=True)
    summary.add_argument("--discord", action="store_true", help="print Hermes Discord summary path")

    cleanup = sub.add_parser("cleanup", help="remove a run worktree")
    cleanup.add_argument("--run-id", required=True)

    repair = sub.add_parser("repair", help="create and validate workflow repair drafts")
    repair_sub = repair.add_subparsers(dest="repair_command", required=True)
    repair_draft = repair_sub.add_parser("draft", help="write a validated repair draft artifact")
    repair_draft.add_argument("--failed-run-id", required=True)
    repair_draft.add_argument("--title", required=True)
    repair_draft.add_argument("--category", choices=sorted(REPAIR_CATEGORIES), required=True)
    repair_draft.add_argument("--risk", choices=sorted(REPAIR_RISKS), required=True)
    repair_draft.add_argument("--proposed-action", choices=sorted(REPAIR_ACTIONS), required=True)
    repair_draft.add_argument("--diagnosis-file", type=Path, required=True)
    repair_draft.add_argument("--evidence-file", type=Path, required=True)
    repair_draft.add_argument("--notify-before-file", type=Path, required=True)
    repair_draft.add_argument("--verify-command")
    repair_draft.add_argument("--retry-original", action="store_true")
    repair_draft.add_argument("--environment")
    repair_draft.add_argument("--healthcheck-command")
    repair_draft.add_argument("--rollback-plan-file", type=Path)
    repair_draft.add_argument("--allow-duplicate", action="store_true")

    repair_validate = repair_sub.add_parser("validate", help="validate an existing repair draft")
    target = repair_validate.add_mutually_exclusive_group(required=True)
    target.add_argument("--draft-id")
    target.add_argument("--draft-dir", type=Path)

    watchdog = sub.add_parser("watchdog", help="inspect workflow failures that need repair triage")
    watchdog_sub = watchdog.add_subparsers(dest="watchdog_command", required=True)
    watchdog_scan = watchdog_sub.add_parser("scan", help="list failed runs without a repair draft")
    watchdog_scan.add_argument("--limit", type=int, default=20)
    watchdog_scan.add_argument("--include-repaired", action="store_true")

    merge_gate = sub.add_parser("merge-gate", help="evaluate whether a GitHub PR can be merged")
    merge_gate.add_argument("--repo", required=True, help="GitHub repository slug, for example owner/name")
    merge_gate.add_argument("--pr", type=int, required=True, help="pull request number")
    merge_gate.add_argument("--repo-path", type=Path, help="local git repository used for local QC worktree")
    merge_gate.add_argument("--verify-command", help="local QC command to run in a detached PR-head worktree")
    merge_gate.add_argument("--timeout-seconds", type=float, default=7200)
    merge_gate.add_argument("--base-branch", default="main")
    merge_gate.add_argument("--output-dir", type=Path)
    merge_gate.add_argument("--allow-no-checks", action="store_true", help="allow approval with no GitHub checks and no local QC")
    merge_gate.add_argument("--keep-worktree", action="store_true")

    merge = sub.add_parser("merge", help="merge a PR from a MERGE_APPROVED decision file")
    merge.add_argument("--decision", type=Path, required=True)
    merge.add_argument("--execute", action="store_true", help="execute gh pr merge; default is dry-run")
    merge.add_argument("--max-age-seconds", type=int, default=int(os.environ.get("HERMES_MERGE_DECISION_MAX_AGE_SECONDS", "86400")))
    merge.add_argument("--method", choices=["merge", "squash", "rebase"], default="squash")
    merge.add_argument("--keep-branch", action="store_true")
    merge.add_argument("--allow-no-checks", action="store_true", help="allow live PR with no GitHub checks even without local QC in decision")

    return parser


def add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, required=True)
    task = parser.add_mutually_exclusive_group(required=True)
    task.add_argument("--task-dir", type=Path)
    task.add_argument("--task-file", type=Path)
    task.add_argument("--task-text")
    parser.add_argument("--workflow", default="default")
    parser.add_argument("--verify-command", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=7200)
    parser.add_argument("--executor-bin", default="takt")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--base-ref")
    parser.add_argument("--keep-worktree", action="store_true", default=True)
    add_notify_args(parser)


def add_notify_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--notify-command",
        help="shell command template after a terminal run; placeholders: {job_id} {run_id} {status} {summary} {discord_summary}",
    )
    parser.add_argument(
        "--notify-statuses",
        default=",".join(sorted(FAILURE_NOTIFY_STATUSES)),
        help="comma-separated statuses to notify, or 'all'; default: failure statuses only",
    )


def add_auto_repair_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--auto-repair",
        action="store_true",
        help="enqueue a repair-diagnosis job when a normal workflow run reaches a terminal failure",
    )
    parser.add_argument("--repair-workflow", default="default", help="workflow for auto-repair diagnosis jobs")
    parser.add_argument("--repair-verify-command", help="verifier for auto-repair diagnosis jobs")
    parser.add_argument("--repair-timeout-seconds", type=float, help="timeout for auto-repair diagnosis jobs")
    parser.add_argument("--repair-executor-bin", help="executor binary for auto-repair diagnosis jobs")
    parser.add_argument("--repair-provider", help="executor provider for auto-repair diagnosis jobs")
    parser.add_argument("--repair-model", help="executor model for auto-repair diagnosis jobs")
    parser.add_argument(
        "--repair-scan-existing",
        action="store_true",
        help="also backfill existing failed runs; disabled by default to avoid notification storms",
    )
    parser.add_argument(
        "--repair-scan-existing-max-age-seconds",
        type=float,
        default=AUTO_REPAIR_SCAN_EXISTING_MAX_AGE_SECONDS,
        help=f"only backfill failed runs updated within this many seconds; default: {AUTO_REPAIR_SCAN_EXISTING_MAX_AGE_SECONDS}",
    )


def config_from_args(args: argparse.Namespace) -> RunnerConfig:
    return RunnerConfig(
        state_dir=args.state_dir,
        repo_path=getattr(args, "repo", None),
        task_dir=getattr(args, "task_dir", None),
        task_file=getattr(args, "task_file", None),
        task_text=getattr(args, "task_text", None),
        workflow=getattr(args, "workflow", "default"),
        verify_command=getattr(args, "verify_command", None),
        timeout_seconds=getattr(args, "timeout_seconds", None),
        executor_bin=getattr(args, "executor_bin", "takt"),
        provider=getattr(args, "provider", None),
        model=getattr(args, "model", None),
        base_ref=getattr(args, "base_ref", None),
    )


def notify_statuses_from_args(args: argparse.Namespace) -> set[str] | None:
    raw = getattr(args, "notify_statuses", "")
    if raw == "all":
        return None
    return {item.strip() for item in raw.split(",") if item.strip()}


def auto_repair_from_args(args: argparse.Namespace) -> AutoRepairConfig | None:
    if not getattr(args, "auto_repair", False):
        return None
    return AutoRepairConfig(
        workflow=getattr(args, "repair_workflow", "default"),
        verify_command=getattr(args, "repair_verify_command", None),
        timeout_seconds=getattr(args, "repair_timeout_seconds", None),
        executor_bin=getattr(args, "repair_executor_bin", None),
        provider=getattr(args, "repair_provider", None),
        model=getattr(args, "repair_model", None),
        scan_existing=getattr(args, "repair_scan_existing", False),
        scan_existing_max_age_seconds=getattr(
            args,
            "repair_scan_existing_max_age_seconds",
            AUTO_REPAIR_SCAN_EXISTING_MAX_AGE_SECONDS,
        ),
    )


def notify_result_if_requested(runner: WorkflowRunner, state, args: argparse.Namespace) -> str:
    return runner.notify_state(state, getattr(args, "notify_command", None), notify_statuses_from_args(args))


def tick_exit_code(results: list[dict[str, str]], isolate_job_failures: bool) -> int:
    if any(result.get("notify_error") or result.get("auto_repair_error") for result in results):
        return 1
    if isolate_job_failures:
        return 1 if any(result.get("error") and not result.get("run_id") for result in results) else 0
    return 0 if all(result.get("status") == "succeeded" for result in results) else 1


def run_exit_code(status: str, notify_error: str = "") -> int:
    if notify_error:
        return 1
    if status == "succeeded":
        return 0
    if status == "interrupted":
        return 130
    return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = WorkflowRunner(args.state_dir)

    try:
        if args.command == "run":
            state = runner.run_new(config_from_args(args))
            notify_error = notify_result_if_requested(runner, state, args)
            if notify_error:
                print(f"agent-workflow: notification failed: {notify_error}", file=sys.stderr)
            print(state.summary_path)
            return run_exit_code(state.status, notify_error)
        if args.command == "enqueue":
            print(runner.enqueue(config_from_args(args)))
            return 0
        if args.command == "tick":
            results = runner.tick(
                max_runs=args.max_runs,
                notify_command=args.notify_command,
                notify_statuses=notify_statuses_from_args(args),
                auto_repair=auto_repair_from_args(args),
            )
            for result in results:
                print(
                    "\t".join(
                        str(result.get(key, ""))
                        for key in ["job_id", "status", "run_id", "summary_path", "error", "notify_error", "repair_job_id", "auto_repair_error", "repair_action_job_id", "repair_action_error"]
                    )
                )
            return tick_exit_code(results, args.isolate_job_failures)
        if args.command == "worker":
            runner.worker(
                interval_seconds=args.interval_seconds,
                max_runs_per_tick=args.max_runs_per_tick,
                notify_command=args.notify_command,
                notify_statuses=notify_statuses_from_args(args),
                parallelism=args.parallelism,
                repo_parallelism=args.repo_parallelism,
                spawn_children=not args.inline,
                stop_when_idle=args.stop_when_idle,
                recover_stale_running=not args.no_recover_stale_running,
                auto_repair=auto_repair_from_args(args),
            )
            return 0
        if args.command == "run-claimed":
            result = runner.run_claimed_job(
                args.job_id,
                notify_command=args.notify_command,
                notify_statuses=notify_statuses_from_args(args),
                auto_repair=auto_repair_from_args(args),
            )
            print(
                "\t".join(
                    str(result.get(key, ""))
                    for key in ["job_id", "status", "run_id", "summary_path", "error", "notify_error", "repair_job_id", "auto_repair_error", "repair_action_job_id", "repair_action_error"]
                )
            )
            return tick_exit_code([result], isolate_job_failures=True)
        if args.command == "resume":
            state = runner.resume(args.run_id, verify_command=args.verify_command, timeout_seconds=args.timeout_seconds)
            notify_error = notify_result_if_requested(runner, state, args)
            if notify_error:
                print(f"agent-workflow: notification failed: {notify_error}", file=sys.stderr)
            print(state.summary_path)
            return run_exit_code(state.status, notify_error)
        if args.command == "retry":
            state = runner.retry(args.run_id, args.step, verify_command=args.verify_command, timeout_seconds=args.timeout_seconds)
            notify_error = notify_result_if_requested(runner, state, args)
            if notify_error:
                print(f"agent-workflow: notification failed: {notify_error}", file=sys.stderr)
            print(state.summary_path)
            return run_exit_code(state.status, notify_error)
        if args.command == "status":
            print(runner.status(args.run_id, include_repair=args.include_repair))
            return 0
        if args.command == "summary":
            summary_path = Path(runner.load_state(args.run_id).summary_path)
            print(summary_path.with_name("hermes-discord-summary.md") if args.discord else summary_path)
            return 0
        if args.command == "cleanup":
            runner.cleanup(args.run_id)
            return 0
        if args.command == "repair":
            manager = RepairManager(args.state_dir)
            if args.repair_command == "draft":
                draft = manager.draft(
                    RepairDraftInput(
                        failed_run_id=args.failed_run_id,
                        title=args.title,
                        category=args.category,
                        risk=args.risk,
                        proposed_action=args.proposed_action,
                        diagnosis_file=args.diagnosis_file,
                        evidence_file=args.evidence_file,
                        notify_before_file=args.notify_before_file,
                        verify_command=args.verify_command,
                        retry_original=args.retry_original,
                        environment=args.environment,
                        healthcheck_command=args.healthcheck_command,
                        rollback_plan_file=args.rollback_plan_file,
                    ),
                    allow_duplicate=args.allow_duplicate,
                )
                print(draft.draft_dir)
                return 0
            if args.repair_command == "validate":
                draft = manager.validate(draft_id=args.draft_id, draft_dir=args.draft_dir)
                print(draft.draft_dir)
                return 0
        if args.command == "watchdog":
            manager = RepairManager(args.state_dir)
            if args.watchdog_command == "scan":
                rows = manager.scan_failures(limit=args.limit, include_repaired=args.include_repaired)
                for row in rows:
                    print("\t".join(row[key] for key in ["run_id", "status", "current_step", "summary_path", "repair_status"]))
                return 0
        if args.command == "merge-gate":
            result = run_merge_gate(
                MergeGateConfig(
                    state_dir=args.state_dir,
                    repo=args.repo,
                    pr_number=args.pr,
                    output_dir=args.output_dir,
                    repo_path=args.repo_path,
                    verify_command=args.verify_command,
                    timeout_seconds=args.timeout_seconds,
                    base_branch=args.base_branch,
                    allow_no_checks=args.allow_no_checks,
                    keep_worktree=args.keep_worktree,
                )
            )
            print(result.decision_file)
            return 0 if result.decision["decision"] == "MERGE_APPROVED" else 1
        if args.command == "merge":
            print(
                run_merge_approved(
                    args.decision,
                    execute=args.execute,
                    max_age_seconds=args.max_age_seconds,
                    method=args.method,
                    delete_branch=not args.keep_branch,
                    allow_no_checks=args.allow_no_checks,
                )
            )
            return 0
    except MergeBlocked as exc:
        print(f"agent-workflow: merge blocked: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"agent-workflow: {exc}", file=sys.stderr)
        return 2
    return 2
