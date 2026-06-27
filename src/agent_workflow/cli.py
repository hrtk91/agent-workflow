from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from agent_workflow.merge import MergeBlocked, MergeGateConfig, run_merge_approved, run_merge_gate
from agent_workflow.runner import RunnerConfig, WorkflowRunner, default_state_dir


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

    worker = sub.add_parser("worker", help="run queued jobs in a loop")
    worker.add_argument("--interval-seconds", type=float, default=60)
    worker.add_argument("--max-runs-per-tick", type=int, default=1)

    resume = sub.add_parser("resume", help="resume a failed or interrupted run")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--verify-command")
    resume.add_argument("--timeout-seconds", type=float)

    retry = sub.add_parser("retry", help="retry one step and all downstream steps")
    retry.add_argument("--run-id", required=True)
    retry.add_argument("--step", required=True)
    retry.add_argument("--verify-command")
    retry.add_argument("--timeout-seconds", type=float)

    status = sub.add_parser("status", help="show run status")
    status.add_argument("--run-id")

    summary = sub.add_parser("summary", help="print summary path")
    summary.add_argument("--run-id", required=True)

    cleanup = sub.add_parser("cleanup", help="remove a run worktree")
    cleanup.add_argument("--run-id", required=True)

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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = WorkflowRunner(args.state_dir)

    try:
        if args.command == "run":
            state = runner.run_new(config_from_args(args))
            print(state.summary_path)
            return 0 if state.status == "succeeded" else 1
        if args.command == "enqueue":
            print(runner.enqueue(config_from_args(args)))
            return 0
        if args.command == "tick":
            results = runner.tick(max_runs=args.max_runs)
            for result in results:
                print("\t".join(str(result.get(key, "")) for key in ["job_id", "status", "run_id", "summary_path", "error"]))
            return 0 if all(result.get("status") == "succeeded" for result in results) else 1
        if args.command == "worker":
            runner.worker(interval_seconds=args.interval_seconds, max_runs_per_tick=args.max_runs_per_tick)
            return 0
        if args.command == "resume":
            state = runner.resume(args.run_id, verify_command=args.verify_command, timeout_seconds=args.timeout_seconds)
            print(state.summary_path)
            return 0 if state.status == "succeeded" else 1
        if args.command == "retry":
            state = runner.retry(args.run_id, args.step, verify_command=args.verify_command, timeout_seconds=args.timeout_seconds)
            print(state.summary_path)
            return 0 if state.status == "succeeded" else 1
        if args.command == "status":
            print(runner.status(args.run_id))
            return 0
        if args.command == "summary":
            print(runner.load_state(args.run_id).summary_path)
            return 0
        if args.command == "cleanup":
            runner.cleanup(args.run_id)
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
