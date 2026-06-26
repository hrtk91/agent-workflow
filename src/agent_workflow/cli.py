from __future__ import annotations

import argparse
import sys
from pathlib import Path

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
    except Exception as exc:
        print(f"agent-workflow: {exc}", file=sys.stderr)
        return 2
    return 2
